"""LX test fixtures: a temporary CA, a loopback TLS usage server, a fake codex-as account.

Test-only. Every value here is fabricated; every server binds 127.0.0.1. The CA
and leaf certificates are made with the host's `openssl` command (skip cleanly
without it). Temporary roots are named quota-lx-* and listed in
.cache/lx/temp-roots.txt before use; tests remove them.
"""
import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
from unittest import mock

HERE = Path(os.path.realpath(__file__)).parent
REPO = HERE.parents[4]
CACHE = REPO / '.cache/lx'
ALIAS = 'lxfixture'
ACCOUNT, USER = 'fixture-account-lx-a', 'fixture-user-lx-a'
OTHER_ACCOUNT, OTHER_USER = 'fixture-account-lx-b', 'fixture-user-lx-b'
# Variables tests must never inherit (the kit's knobs and anything that changes TLS or native routing).
DROPPED = ('CODING_AGENT_KIT_CONFIG', 'CODING_AGENT_KIT_COLOR', 'CODING_AGENT_KIT_USAGE_FILE', 'CODING_AGENT_KIT_TOKEN_ID',
           'CODING_AGENT_KIT_CLAUDE_PID', 'CODING_AGENT_KIT_REPO_URL', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'NO_COLOR')


def load(name, path):
    spec = importlib.util.spec_from_file_location('lxtest_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qx = load('qx_fixtures', HERE.parent / 'qx/fixtures.py')
live = load('live_codex', HERE / 'live_codex.py')
gates = live.gates
guard, storage, settings = live.guard, live.storage, live.settings


def temp_root(tag):
    """A private quota-lx-* directory, recorded before use."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix='quota-lx-' + tag + '-'))
    with (CACHE / 'temp-roots.txt').open('a') as inventory:
        inventory.write(str(path) + '\n')
    return path


def openssl():
    return shutil.which('openssl')


def make_ca(directory):
    """A temporary CA and two leaves: localhost/127.0.0.1 and wrong.invalid. Returns paths."""
    tool = openssl()
    directory = Path(directory)
    directory.mkdir(mode=0o700, exist_ok=True)
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin')}

    def run(*args):
        subprocess.run([tool, *args], cwd=directory, env=env, check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    ec = ('-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes')
    run('req', '-x509', *ec, '-keyout', 'ca.key', '-out', 'ca.pem', '-days', '2', '-subj', '/CN=lx-test-ca',
        '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
    paths = {'ca': directory / 'ca.pem'}
    for name, san in (('leaf', 'DNS:localhost,IP:127.0.0.1'), ('wrong', 'DNS:wrong.invalid')):
        (directory / (name + '.ext')).write_text(
            'subjectAltName=%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'
            'extendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n' % san)
        run('req', *ec, '-keyout', name + '.key', '-out', name + '.csr', '-subj', '/CN=lx-test-leaf')
        run('x509', '-req', '-in', name + '.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial',
            '0x' + os.urandom(8).hex(), '-out', name + '.pem', '-days', '2', '-extfile', name + '.ext')
        paths[name] = (directory / (name + '.pem'), directory / (name + '.key'))
    return paths


def loopback_resolver(host, port, *args):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))]


def claims_of(bearer):
    part = bearer.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))


class TlsUsageServer:
    """Loopback TLS server standing in for the backend. By default it answers usage for the
    bearer's own account and user, like a server deriving identity from the token."""

    def __init__(self, cert, key):
        self.lock = threading.Lock()
        self.requests = []  # (method, path, sorted header names, bearer) - test memory only
        self.handshake_failures = 0
        self.respond = None  # callable(bearer) -> (status, [(name, value)], body bytes)
        self.delay = 0.0
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server_ref = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass  # a client the gate aborted (deadline, size) is expected here

            def finish_request(self, request, client_address):
                try:
                    request.settimeout(5)
                    tls = context.wrap_socket(request, server_side=True)
                except (ssl.SSLError, OSError):
                    with server_ref.lock:
                        server_ref.handshake_failures += 1
                    return
                try:
                    self.RequestHandlerClass(tls, client_address, self)
                finally:
                    tls.close()

        self.server = Server(('127.0.0.1', 0), self._handler())
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def count(self, path=None):
        with self.lock:
            return sum(1 for item in self.requests if path is None or item[1] == path)

    def _handler(self):
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                value = self.headers.get('Authorization') or ''
                bearer = value[7:] if value.startswith('Bearer ') else None
                with upstream.lock:
                    upstream.requests.append(('GET', self.path, sorted(k.lower() for k in self.headers.keys()), bearer))
                if upstream.delay:
                    threading.Event().wait(upstream.delay)
                if upstream.respond is not None:
                    status, headers, body = upstream.respond(bearer)
                elif self.path == '/backend-api/wham/usage' and bearer:
                    auth = claims_of(bearer).get('https://api.openai.com/auth', {})
                    status, headers = 200, [('Content-Type', 'application/json')]
                    body = json.dumps(qx.usage_body(auth.get('chatgpt_account_id'), auth.get('chatgpt_user_id'))).encode()
                else:
                    status, headers, body = 404, [('Content-Type', 'application/json')], b'{"detail":"not found"}'
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

        return Handler


def test_upstream(server, ca, host='localhost'):
    """The test-only context hook: a verifying context that trusts the temporary CA."""
    return gates.Upstream(host, server.port, lambda: ssl.create_default_context(cafile=str(ca)),
                          resolver=loopback_resolver)


class Home:
    """A temporary HOME with the kit's configuration and a codex-as account made by the real
    lib/codex-as layout code, holding fabricated credentials."""

    def __init__(self, root, codex, alias=ALIAS, account=ACCOUNT, user=USER, lifetime=3600):
        self.root, self.alias = Path(root), alias
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        directory = self.home / '.config/coding-agent-kit'
        directory.mkdir(parents=True, mode=0o700)
        (directory / 'config.toml').write_text('codex = %s\n' % json.dumps(str(codex)))
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
        with self.environment():
            self.config = settings.load()
        self.access = qx.token(account, user, lifetime=lifetime)
        self.refresh = 'fixture-refresh-' + os.urandom(6).hex()
        self.raw = qx.auth_json(self.access, self.refresh, account, user)
        self.config.secrets_dir.mkdir(mode=0o700)
        self.secret = self.config.secrets_dir / ('codex-%s.json' % alias)
        guard.exclusive(self.secret, self.raw.encode())
        layout = storage.Layout(guard, alias, self.config)
        layout.root_check()
        assert layout.acquire()
        layout.prepare()
        layout.close()
        self.layout = layout
        self.id_token = json.loads(self.raw)['tokens']['id_token']

    @contextmanager
    def environment(self, **extra):
        values = dict(self.env, **extra)
        with mock.patch.dict(os.environ, values, clear=True):
            yield values

    def secrets(self):
        """Every fabricated secret or id a leak scan must not find."""
        return [self.access, self.refresh, self.id_token, ACCOUNT, USER, OTHER_ACCOUNT, OTHER_USER, str(self.home),
                str(self.root)]

    def lock_path(self):
        return self.config.codex_home / 'accounts/.locks' / (self.alias + '.lock')


def fake_codex(directory, name='codex', **env):
    """An executable wrapper that runs fake_codex.py with this interpreter. The probe passes
    native only an allowlisted environment, so test knobs are baked into the wrapper."""
    path = Path(directory) / name
    lines = ['#!/bin/sh'] + ['export %s=%s' % (key, shlex.quote(str(value))) for key, value in sorted(env.items())]
    lines.append('exec %s -I -B %s "$@"' % (shlex.quote(sys.executable), shlex.quote(str(HERE / 'fake_codex.py'))))
    path.write_text('\n'.join(lines) + '\n')
    path.chmod(0o700)
    return path


def scan(texts, secrets, allowed=()):
    """Leak findings over output texts: known values plus the probe's own shape checks."""
    findings = []
    for name, text in texts.items():
        for value in secrets:
            if value and value in text:
                findings.append((name, 'known-value'))
        for reason in live.leaks(text, allowed=allowed):
            findings.append((name, reason))
    return findings


def outputs(out, *streams):
    texts = {}
    if Path(out).is_dir():
        for path in sorted(Path(out).rglob('*')):
            if path.is_file():
                texts[path.name] = path.read_text(errors='replace')
    for index, stream in enumerate(streams):
        texts['stream-%d' % index] = stream
    return texts
