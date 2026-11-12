"""KM fixtures: fake upstream, fake usage, TLS refresh hosts and fabricated stores (research only).

Everything here is a loopback fixture with fabricated values. The fake upstream serves both
providers' model routes and Codex usage, answers scripted turns by a KM-* marker in the latest
user turn, and records every request it receives with a system-wide monotonic receipt time, so
a probe can prove that no upstream byte arrived after a gate latch. Nothing is forwarded
anywhere; no real host, credential or store is touched.
"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import ssl
import subprocess
import threading
import time

HERE = Path(os.path.realpath(__file__)).parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NMGATE = _load('km_nm_gate', HERE.parent / 'nm' / 'gate.py')  # SSE builders only
QXFIX = _load('km_qx_fixtures', HERE.parent / 'qx' / 'fixtures.py')
QCUSAGE = _load('km_qc_fake_usage', HERE.parent / 'qc' / 'fake_usage.py')

MARKER = re.compile(r'KM-[A-Za-z0-9_-]+')
CLAUDE_MODEL = 'claude-sonnet-4-6'
REFRESH_HOSTS = ('platform.claude.com', 'auth.openai.com')


def wait_for(predicate, seconds, step=0.1):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def claude_token(label):
    """A fabricated Claude access or refresh token that QC's TOKEN_RE accepts."""
    return 'KMFAKE-%s-%s' % (label, secrets.token_hex(12))


def write_private(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + '.km-tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    os.chmod(str(tmp), 0o600)
    os.replace(str(tmp), str(path))


def claude_store(config_dir, access, refresh, expires_ms):
    write_private(Path(config_dir) / '.credentials.json', json.dumps({'claudeAiOauth': {
        'accessToken': access, 'refreshToken': refresh, 'expiresAt': int(expires_ms),
        'scopes': ['user:inference', 'user:profile', 'user:sessions:claude_code'],
        'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_5x'}}))


def claude_metadata(config_dir, identity, workspaces):
    write_private(Path(config_dir) / '.claude.json', json.dumps({
        'hasCompletedOnboarding': True, 'theme': 'dark', 'bypassPermissionsModeAccepted': True,
        'projects': {str(w): {'hasTrustDialogAccepted': True, 'hasCompletedProjectOnboarding': True}
                     for w in workspaces},
        'oauthAccount': {'accountUuid': identity[0], 'organizationUuid': identity[1],
                         'emailAddress': 'km@example.invalid'}}))


def codex_auth(codex_home, access, refresh, account, user):
    write_private(Path(codex_home) / 'auth.json', QXFIX.auth_json(access, refresh, account, user))


def codex_config(codex_home, gate_url, workspace, sandbox_mode='danger-full-access'):
    (Path(codex_home) / 'config.toml').write_text(f'''model = "gpt-5.4"
model_provider = "gate"
chatgpt_base_url = "{gate_url}/backend-api"
cli_auth_credentials_store = "file"
approval_policy = "never"
sandbox_mode = "{sandbox_mode}"
web_search = "disabled"
check_for_update_on_startup = false
[model_providers.gate]
name = "gate"
base_url = "{gate_url}/backend-api/codex"
requires_openai_auth = true
supports_websockets = false
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
[features]
plugins = false
recommended_plugins = false
apps = false
enable_mcp_apps = false
memories = false
shell_snapshot = false
shell_snapshot_v2 = false
network_proxy = false
code_mode_prewarm = false
[memories]
generate_memories = false
use_memories = false
[analytics]
enabled = false
[projects."{workspace}"]
trust_level = "trusted"
''')


def latest_turn(provider, body):
    """(kind, text) of the newest input item: 'tool_result' or 'turn' with the user text."""
    if provider == 'claude':
        messages = body.get('messages') or []
        last = messages[-1] if messages else {}
        content = last.get('content')
        if isinstance(content, list):
            if any(isinstance(c, dict) and c.get('type') == 'tool_result' for c in content):
                return 'tool_result', ''
            return 'turn', ' '.join(c.get('text', '') for c in content if isinstance(c, dict))
        return 'turn', content if isinstance(content, str) else ''
    items = body.get('input') or []
    if items and isinstance(items[-1], dict) and items[-1].get('type') == 'function_call_output':
        return 'tool_result', ''
    text = ''
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get('role') == 'user':
            parts = item.get('content')
            text = ' '.join(p.get('text', '') for p in parts if isinstance(p, dict)) \
                if isinstance(parts, list) else str(parts or '')
    return 'turn', text


def marker_of(text):
    found = MARKER.findall(text or '')
    return found[-1] if found else None


class Upstream:
    """Fake provider upstream for both natives; scripted by marker, every request recorded."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.script = {}          # marker -> list of actions, consumed per main-loop turn
        self.labels = {}          # bearer -> label
        self.codex = {}           # bearer -> {'account','user','plan','revoked'}
        self.usage = {}           # account -> {'five': x, 'weekly': y}
        self.release = threading.Event()
        self.held = []            # markers of streams currently held open
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def label(self, header):
        token = header[7:] if (header or '').startswith('Bearer ') else None
        with self.lock:
            return self.labels.get(token, 'none' if token is None else 'unknown')

    def add_codex(self, label, account, user, five=20, weekly=30, lifetime=3600):
        access, refresh = QXFIX.token(account, user, lifetime), 'km-fixture-refresh-' + secrets.token_hex(6)
        with self.lock:
            self.labels[access] = label
            self.codex[access] = {'account': account, 'user': user, 'plan': 'pro', 'revoked': False}
            self.usage.setdefault(account, {'five': five, 'weekly': weekly})
        return access, refresh

    def set_usage(self, account, five=None, weekly=None):
        with self.lock:
            row = self.usage.setdefault(account, {'five': 20, 'weekly': 30})
            if five is not None:
                row['five'] = five
            if weekly is not None:
                row['weekly'] = weekly

    def snapshot(self):
        with self.lock:
            return list(self.requests)

    def after(self, t):
        return [r for r in self.snapshot() if r['t'] > t]

    def _handler(self):
        up = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def note(self, size, **data):
                row = {'t': time.monotonic(), 'method': self.command, 'path': self.path.split('?')[0],
                       'bytes': size, 'label': up.label(self.headers.get('Authorization'))}
                row.update(data)
                with up.lock:
                    up.requests.append(row)
                return row

            def reply(self, status, body, ctype='application/json'):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split('?')[0]
                self.note(0)
                if path.endswith(('/wham/usage', '/api/codex/usage')):
                    token = (self.headers.get('Authorization') or '')[7:]
                    with up.lock:
                        ident = up.codex.get(token)
                        ident = dict(ident) if ident and not ident['revoked'] else None
                        row = dict(up.usage.get(ident['account'], {})) if ident else {}
                    if ident is None:
                        return self.reply(401, {'error': {'code': 'token_expired'}})
                    if row.get('mode') == 'malformed':
                        return self.reply(200, b'{not json')
                    wall = int(time.time()) + int(row.get('reset_offset', 0))
                    return self.reply(200, QXFIX.usage_body(ident['account'], ident['user'], five=row['five'],
                                                            weekly=row['weekly'], plan=ident['plan'], wall=wall))
                if path.endswith('/models'):
                    return self.reply(200, {'models': []})
                if path.endswith('/accounts/check'):
                    return self.reply(200, {'accounts': []})
                return self.reply(404, {'error': 'fixture has no such endpoint'})

            def do_POST(self):
                size = int(self.headers.get('Content-Length', '0') or 0)
                raw = self.rfile.read(size)
                path = self.path.split('?')[0]
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                provider = 'claude' if path == '/v1/messages' else 'codex' if path.endswith('/responses') else None
                if provider is None:
                    self.note(size)
                    return self.reply(404, {'error': 'fixture has no such endpoint'})
                kind, text = latest_turn(provider, body)
                marker = marker_of(text) if kind == 'turn' else None
                main = bool(body.get('tools'))
                action = None
                if kind == 'turn' and main and marker:
                    with up.lock:
                        queue = up.script.get(marker)
                        if queue:
                            action = queue.pop(0)
                self.note(size, provider=provider, kind=kind, marker=marker, main=main,
                          model=body.get('model'), action=(action or ('text',))[0])
                if action and action[0] == 'hold':
                    return self.hold(provider, marker)
                if action and action[0] == 'tool':
                    rows = (NMGATE.claude_bash(body.get('model'), action[1]) if provider == 'claude'
                            else NMGATE.codex_call(secrets.token_hex(3), body.get('tools', []), action[1]))
                else:
                    reply = 'KM fixture reply' + (' after tool' if kind == 'tool_result' else '')
                    rows = (NMGATE.claude_text(body.get('model'), reply) if provider == 'claude'
                            else NMGATE.codex_text(secrets.token_hex(3), reply))
                try:
                    NMGATE._sse(self, rows, provider == 'claude')
                except OSError:
                    pass

            def hold(self, provider, marker):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                first = (NMGATE.claude_text(CLAUDE_MODEL, '')[:1] if provider == 'claude'
                         else [{'type': 'response.created', 'response': {'id': 'km-held'}}])
                ping = (b'event: ping\ndata: {"type": "ping"}\n\n' if provider == 'claude'
                        else b': km heartbeat\n\n')
                with up.lock:
                    up.held.append(marker)
                try:
                    for row in first:
                        prefix = ('event: ' + row['type'] + '\n') if provider == 'claude' else ''
                        self.wfile.write((prefix + 'data: ' + json.dumps(row) + '\n\n').encode())
                    self.wfile.flush()
                    deadline = time.monotonic() + 120
                    while not up.release.wait(0.2) and time.monotonic() < deadline:
                        self.wfile.write(ping)
                        self.wfile.flush()
                    outcome = 'released'
                except OSError:
                    outcome = 'client_gone'
                with up.lock:
                    if marker in up.held:
                        up.held.remove(marker)
                    up.requests.append({'t': time.monotonic(), 'event': 'hold_end', 'marker': marker,
                                        'outcome': outcome, 'bytes': 0})

        return Handler


class ClaudeUsage:
    """QC's fake /api/oauth/usage and /api/oauth/profile server, driven by fixture labels."""

    def __init__(self):
        self.server = QCUSAGE.FakeUsageServer().__enter__()
        self.port = self.server.port
        self.accounts = {}

    def add(self, label, token, identity, five=20, week=30):
        account = self.accounts.get(identity)
        if account is None:
            account = QCUSAGE.FakeAccount(label, identity[0], identity[1], QCUSAGE.windows(five, week))
            self.accounts[identity] = account
        # All bearers of one identity share one usage body (one account's windows).
        self.server.register(token, _Shared(label, account))
        return account

    def set(self, identity, five=None, week=None):
        account = self.accounts[identity]
        body = QCUSAGE.windows(five if five is not None else account.body['five_hour']['utilization'],
                               week if week is not None else account.body['seven_day']['utilization'])
        account.body = body

    def revoke(self, token):
        self.server.revoke(token)

    def labels(self, path='/api/oauth/usage'):
        return self.server.labels(path)

    def close(self):
        self.server.__exit__(None, None, None)


class _Shared:
    """A per-bearer label over one shared account record (usage body and mode)."""

    def __init__(self, label, account):
        self.label, self._account = label, account

    def __getattr__(self, name):
        return getattr(self._account, name)


TLS_CONFIG = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = KM Fabricated Test CA
[ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = %s
authorityKeyIdentifier = keyid
"""


def make_ca(directory, hosts=REFRESH_HOSTS):
    """Fabricated CA and one leaf for the refresh host names, inside the temp root only.

    The leaf names real refresh hosts so a native's TLS to the CONNECT target verifies; it is
    trusted only through one process's NODE_EXTRA_CA_CERTS or CODEX_CA_CERTIFICATE and is never
    added to any keychain or trust store.
    """
    d = Path(directory)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (d / 'leaf.pem').exists():
        return d / 'ca.pem', d / 'leaf.pem', d / 'leaf.key'
    (d / 'ext.cnf').write_text(TLS_CONFIG % ','.join('DNS:' + h for h in hosts))
    openssl = '/usr/bin/openssl'
    for argv in ([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.pem',
                  '-days', '2', '-config', 'ext.cnf', '-extensions', 'ca'],
                 [openssl, 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'leaf.key', '-out', 'leaf.csr',
                  '-subj', '/CN=' + hosts[0]],
                 [openssl, 'x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial', '2',
                  '-out', 'leaf.pem', '-days', '2', '-extfile', 'ext.cnf', '-extensions', 'leaf']):
        subprocess.run(argv, cwd=str(d), check=True, capture_output=True, timeout=60)
    return d / 'ca.pem', d / 'leaf.pem', d / 'leaf.key'


class RefreshServer:
    """Loopback TLS stand-in for both refresh hosts; the gate's CONNECT allowlist maps to it.

    grants: refresh token -> callable() returning the JSON grant, or None to refuse.
    """

    def __init__(self, leaf, key):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(leaf), str(key))
        self.grants, self.events, self.lock = {}, [], threading.Lock()
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                owner.handle(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def record(self, **row):
        with self.lock:
            self.events.append({'t': time.monotonic(), **row})

    def handle(self, raw):
        raw.settimeout(10)
        try:
            with self.context.wrap_socket(raw, server_side=True) as tls, tls.makefile('rb') as stream:
                first = stream.readline(8192).decode('latin-1').split()
                headers = {}
                while True:
                    line = stream.readline(8192)
                    if line in (b'\r\n', b'\n', b''):
                        break
                    key, _, value = line.decode('latin-1').partition(':')
                    headers[key.strip().lower()] = value.strip()
                body = stream.read(int(headers.get('content-length', '0') or 0))
                path = first[1].split('?')[0] if len(first) > 1 else ''
                try:
                    fields = json.loads(body)
                except ValueError:
                    from urllib.parse import parse_qs
                    fields = {k: v[0] for k, v in parse_qs(body.decode('latin-1')).items()}
                grant = None
                if path.endswith('/oauth/token') and isinstance(fields, dict) \
                        and fields.get('grant_type') == 'refresh_token':
                    with self.lock:
                        maker = self.grants.pop(fields.get('refresh_token'), None)
                    grant = maker() if maker else None
                self.record(event='refresh_request', host=headers.get('host', '').split(':')[0], path=path,
                            granted=grant is not None)
                status, payload = (200, grant) if grant else (400, {'error': 'invalid_grant'})
                data = json.dumps(payload).encode()
                tls.sendall(('HTTP/1.1 %d Fixture\r\nContent-Type: application/json\r\nContent-Length: %d\r\n'
                             'Connection: close\r\n\r\n' % (status, len(data))).encode() + data)
        except (OSError, ssl.SSLError, ValueError) as error:
            self.record(event='refresh_error', error=type(error).__name__)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def b64json(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()


def free_listener():
    """An outside loopback TCP listener (alternate port) that counts connections."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(16)
    hits = []

    def run():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            hits.append(time.monotonic())
            conn.close()
    threading.Thread(target=run, daemon=True).start()
    return sock, hits
