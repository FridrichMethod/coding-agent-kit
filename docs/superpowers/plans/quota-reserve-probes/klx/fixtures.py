"""KLX loopback fixtures: fake ChatGPT backend, TLS refresh host, CONNECT proxy, static server.

Research code only; every account, token, quota value and model reply is fabricated. The fake
backend derives identity from the bearer (it issued every token), keeps one usage state per
account (so consumption by any client counts for every invocation of that account), scripts model
replies by KLX-* prompt markers and timestamps every request it receives with the host monotonic
clock, which the gate process shares, so a checker can prove that nothing reached it after a latch.
"""
import base64
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import socket
import ssl
import subprocess
import threading
import time

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qx = _load('klx_qx_fixtures', HERE.parent / 'qx' / 'fixtures.py')
token, auth_json, usage_body = qx.token, qx.auth_json, qx.usage_body
TITLE = 'Generate a concise'


def accounts_check(account):
    """The PC3 sourced discovery shape; fixture.invalid is never contacted."""
    return {'accounts': [{'id': account, 'name': 'Fixture', 'workspace_backend_origin': 'https://fixture.invalid',
                          'account_routing_override': 'NO_CONSTRAINT'}]}


def sse(events):
    return b''.join(('data: ' + json.dumps(event) + '\n\n').encode() for event in events)


def reply(text, ident):
    return sse([{'type': 'response.created', 'response': {'id': ident}},
                {'type': 'response.output_item.done',
                 'item': {'type': 'message', 'role': 'assistant', 'id': 'msg-' + ident,
                          'content': [{'type': 'output_text', 'text': text}]}},
                {'type': 'response.completed',
                 'response': {'id': ident, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}])


def tool_call(command, ident, yield_ms=2000):
    return sse([{'type': 'response.created', 'response': {'id': ident}},
                {'type': 'response.output_item.done',
                 'item': {'type': 'function_call', 'call_id': 'call-' + ident, 'name': 'exec_command',
                          'arguments': json.dumps({'cmd': command, 'yield_time_ms': yield_ms})}},
                {'type': 'response.completed',
                 'response': {'id': ident, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}])


def last_user_turn(body):
    """(latest user text, whether a tool output follows it) from a Responses request body."""
    items = body.get('input') or []
    text, after = '', False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get('role') == 'user':
            parts = item.get('content') or []
            text = ' '.join(part.get('text', '') for part in parts if isinstance(part, dict)) \
                if isinstance(parts, list) else str(parts)
            after = False
        elif item.get('type') == 'function_call_output':
            after = True
    return text, after


def marker_of(text):
    if 'KLX-' not in text:
        return None
    return 'KLX-' + text.split('KLX-', 1)[1].split()[0].strip('.,"\'')


class Backend:
    """Fake chatgpt.com plus its token endpoint; the only upstream any gate forwards to."""

    def __init__(self):
        self.lock = threading.Lock()
        self.identities = {}  # access token -> {'account', 'user', 'plan', 'label', 'revoked'}
        self.refreshes = {}  # refresh token -> (account, user, label)
        self.usage = {}  # account -> {'five': percent used, 'weekly': percent used}
        self.usage_override = None  # callable(identity) -> (status, body) or None
        self.tools = {}  # KLX-TOOL-<name> marker -> shell command for exec_command
        self.tool_yield = {}  # marker -> exec_command yield_time_ms (default 2000)
        self.events = []
        self.on_issue = []  # callbacks(access, label) so a launcher can label credentials at its gate
        self.release = threading.Event()
        self.hold_titles = False  # True: native title requests stay open like a long stream
        self.consume = 0.0  # percent of the five-hour window each KLX-OUTSIDE request uses
        self.refresh_count = 0
        self.counter = 0
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.url = 'http://127.0.0.1:%d' % self.port
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()

    def note(self, kind, **data):
        with self.lock:
            self.events.append({'at': time.monotonic(), 'kind': kind, **data})

    def count(self, kind, since=None):
        with self.lock:
            return sum(e['kind'] == kind and (since is None or e['at'] > since) for e in self.events)

    def set_usage(self, account, five, weekly):
        with self.lock:
            self.usage[account] = {'five': five, 'weekly': weekly}
        self.note('usage_set', account=account, five=five, weekly=weekly)

    def issue(self, account, user, label, lifetime=3600):
        access = token(account, user, lifetime)
        refresh = 'fixture-refresh-' + os.urandom(6).hex()
        with self.lock:
            self.identities[access] = {'account': account, 'user': user, 'plan': qx.PLAN, 'label': label,
                                       'revoked': False}
            self.refreshes[refresh] = (account, user, label)
            self.usage.setdefault(account, {'five': 25, 'weekly': 40})
        for callback in list(self.on_issue):
            callback(access, label)
        return access, refresh

    def revoke(self, access):
        with self.lock:
            self.identities[access]['revoked'] = True
        self.note('revoked', label=self.identities[access]['label'])

    def identity(self, headers):
        value = headers.get('Authorization') or ''
        access = value[7:] if value.startswith('Bearer ') else None
        with self.lock:
            found = self.identities.get(access)
            return dict(found) if found else None

    def refresh(self, raw):
        """The token endpoint, shared by the plain route and the TLS refresh host."""
        try:
            request = json.loads(raw)
        except ValueError:
            request = {}
        if not isinstance(request, dict):
            request = {}
        if not request.get('refresh_token'):  # some clients send a form body
            fields = dict(part.split('=', 1) for part in raw.decode(errors='replace').split('&') if '=' in part)
            request = fields
        with self.lock:
            owner = self.refreshes.pop(request.get('refresh_token'), None)
        if owner is None:
            return 401, {'error': 'invalid_grant'}, None
        account, user, label = owner
        base = label.split('r')[0]
        with self.lock:
            self.refresh_count += 1
            new_label = '%sr%d' % (base, self.refresh_count)
        access, refresh = self.issue(account, user, new_label)
        return 200, {'access_token': access, 'refresh_token': refresh, 'id_token': token(account, user),
                     'expires_in': 3600, 'token_type': 'Bearer'}, new_label

    def usage_headers(self, account):
        with self.lock:
            state = dict(self.usage.get(account, {'five': 25, 'weekly': 40}))
        wall = int(time.time())
        return [('x-codex-primary-used-percent', str(state['five'])),
                ('x-codex-primary-window-minutes', '300'), ('x-codex-primary-reset-at', str(wall + 9000)),
                ('x-codex-secondary-used-percent', str(state['weekly'])),
                ('x-codex-secondary-window-minutes', '10080'),
                ('x-codex-secondary-reset-at', str(wall + 302400))]

    def _handler(self):
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, body, content_type='application/json', headers=()):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(data)))
                    for name, value in headers:
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_GET(self):
                path = self.path.split('?')[0]
                identity = backend.identity(self.headers)
                label = identity and identity['label']
                if self.headers.get('Upgrade'):
                    backend.note('upgrade', path=path)
                    return self.send(503, {'error': 'fixture refuses upgrades'})
                if path.endswith(('/wham/usage', '/api/codex/usage')):
                    live = identity is not None and not identity['revoked']
                    backend.note('usage', label=label, live=live)
                    if not live:
                        return self.send(401, {'error': {'code': 'token_expired'}})
                    override = backend.usage_override and backend.usage_override(identity)
                    if override:
                        return self.send(*override)
                    with backend.lock:
                        state = dict(backend.usage[identity['account']])
                    return self.send(200, usage_body(identity['account'], identity['user'], five=state['five'],
                                                     weekly=state['weekly']))
                if path.endswith('/accounts/check'):
                    backend.note('accounts_check', label=label)
                    if identity is None:
                        return self.send(401, {'error': 'fixture'})
                    return self.send(200, accounts_check(identity['account']))
                if path.endswith('/models'):
                    backend.note('models', label=label)
                    return self.send(200, {'models': []})
                backend.note('other_get', path=path, label=label)
                self.send(503, {'error': 'fixture has no such endpoint'})

            def do_POST(self):
                path = self.path.split('?')[0]
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0') or 0))
                if path.endswith('/oauth/token'):
                    status, body, label = backend.refresh(raw)
                    backend.note('refresh', accepted=status == 200, label=label, host='plain')
                    return self.send(status, body)
                if path.endswith('/oauth/revoke'):
                    backend.note('revoke', host='plain')
                    return self.send(200, {})
                if not path.endswith('/responses'):
                    backend.note('other_post', path=path)
                    return self.send(503, {'error': 'fixture has no such endpoint'})
                identity = backend.identity(self.headers)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                text, after_tool = last_user_turn(body)
                marker = marker_of(text)
                kind = 'title' if text.startswith(TITLE) else 'tool_result' if after_tool else 'turn'
                live = identity is not None and not identity['revoked']
                with backend.lock:
                    backend.counter += 1
                    ident = 'klx-%d' % backend.counter
                backend.note('model', label=identity and identity['label'], live=live, marker=marker, request=kind,
                             ident=ident, model=body.get('model'))
                if not live:
                    return self.send(401, {'error': {'code': 'token_expired', 'message': 'fixture'}})
                if backend.consume and marker and marker.startswith('KLX-OUTSIDE'):
                    with backend.lock:
                        state = backend.usage[identity['account']]
                        state['five'] = min(100, state['five'] + backend.consume)
                headers = backend.usage_headers(identity['account'])
                if kind == 'turn' and marker and marker.startswith('KLX-TOOL-') and marker in backend.tools:
                    return self.send(200, tool_call(backend.tools[marker], ident, backend.tool_yield.get(marker, 2000)),
                                     'text/event-stream', headers)
                if kind == 'turn' and marker and marker.startswith('KLX-HOLD'):
                    return self.held(ident, marker, headers)
                if kind == 'title' and backend.hold_titles:
                    return self.held(ident, 'KLX-HOLD-title', headers)
                return self.send(200, reply('KLX fixture reply %s' % (marker or kind), ident), 'text/event-stream',
                                 headers)

            def held(self, ident, marker, headers):
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    for name, value in headers:
                        self.send_header(name, value)
                    self.end_headers()
                    self.wfile.write(('data: ' + json.dumps({'type': 'response.created',
                                                             'response': {'id': ident}}) + '\n\n').encode())
                    self.wfile.flush()
                    backend.note('stream_open', ident=ident, marker=marker)
                    while not backend.release.wait(.05):
                        self.wfile.write(b': klx heartbeat\n\n')
                        self.wfile.flush()
                    self.wfile.write(reply('KLX released ' + marker, ident))
                    backend.note('stream_released', ident=ident, marker=marker)
                except OSError:
                    backend.note('stream_lost', ident=ident, marker=marker)

            def do_CONNECT(self):
                backend.note('connect', destination=self.path)
                self.send_error(403)

        return Handler


def make_ca(root, host='auth.openai.com'):
    """A fixture CA and a leaf certificate for `host` (host openssl). Returns paths or None."""
    openssl = shutil.which('openssl')
    if not openssl:
        return None
    root = Path(root)
    root.mkdir(mode=0o700, exist_ok=True)
    ext = root / 'leaf.ext'
    ext.write_text('basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n'
                   'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:%s\n'
                   'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n' % host)
    steps = [
        [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-subj', '/CN=KLX fixture CA',
         '-keyout', str(root / 'ca.key'), '-out', str(root / 'ca.pem'),
         '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign'],
        [openssl, 'req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=' + host,
         '-keyout', str(root / 'leaf.key'), '-out', str(root / 'leaf.csr')],
        [openssl, 'x509', '-req', '-in', str(root / 'leaf.csr'), '-CA', str(root / 'ca.pem'),
         '-CAkey', str(root / 'ca.key'), '-CAcreateserial', '-days', '2', '-extfile', str(ext),
         '-out', str(root / 'leaf.pem')],
    ]
    for step in steps:
        result = subprocess.run(step, capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        if result.returncode:
            return None
    return {'ca': str(root / 'ca.pem'), 'cert': str(root / 'leaf.pem'), 'key': str(root / 'leaf.key')}


class TlsRefreshHost:
    """Loopback TLS server presenting the fixture certificate for auth.openai.com."""

    def __init__(self, backend, certs):
        self.backend = backend
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certs['cert'], certs['key'])
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                path = self.path.split('?')[0]
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0') or 0))
                if path.endswith('/oauth/token'):
                    status, body, label = owner.backend.refresh(raw)
                    owner.backend.note('refresh', accepted=status == 200, label=label, host='auth.openai.com')
                elif path.endswith('/oauth/revoke'):
                    status, body = 200, {}
                    owner.backend.note('revoke', host='auth.openai.com')
                else:
                    status, body = 404, {'error': 'fixture'}
                    owner.backend.note('refresh_host_other', path=path)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ConnectProxy:
    """A loopback CONNECT proxy standing in for a launcher HTTPS_PROXY; records every tunnel."""

    def __init__(self, allowed):
        self.allowed = set(allowed)  # 'host:port' strings it will tunnel to
        self.events = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                owner.events.append({'at': time.monotonic(), 'destination': self.path,
                                     'allowed': self.path in owner.allowed})
                if self.path not in owner.allowed:
                    return self.send_error(403)
                host, port = self.path.rsplit(':', 1)
                upstream = socket.create_connection((host, int(port)), timeout=10)
                self.send_response(200, 'Connection Established')
                self.end_headers()
                self.wfile.flush()
                ends = [self.connection, upstream]
                pair = {self.connection: upstream, upstream: self.connection}
                try:
                    while True:
                        ready, _, _ = select.select(ends, [], [], 1.0)
                        for sock in ready:
                            data = sock.recv(65536)
                            if not data:
                                return
                            pair[sock].sendall(data)
                except OSError:
                    pass
                finally:
                    upstream.close()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class StaticServer:
    """Serves a directory over plain HTTP (dumb git remote, pip-style simple index); records paths."""

    def __init__(self, directory):
        self.events = []
        owner = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(directory), **kwargs)

            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.events.append({'at': time.monotonic(), 'path': self.path.split('?')[0]})
                super().do_GET()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def b64(data):
    return base64.b64encode(data).decode()
