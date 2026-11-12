"""KLC loopback fixtures, all outside the boundary: fake Messages upstream,
fake platform.claude.com refresh endpoint (disposable CA), counting CONNECT
proxy and a git/package index server. Fabricated tokens and accounts only;
nothing is forwarded off the host. Header values are never recorded, only
equality with literal fixture bearers.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer

MARKERS = (('KLC grandchild', 'grandchild'), ('KLC child', 'child'), ('KLC sdk', 'sdk'))


def _texts(message):
    content = message.get('content')
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text']
    return []


def classify(body):
    """Kind of one Messages request, from the fixture prompt markers only."""
    messages = body.get('messages') or []
    users = [m for m in messages if isinstance(m, dict) and m.get('role') == 'user']
    first = ' '.join(_texts(users[0])) if users else ''
    every = ' '.join(t for m in users for t in _texts(m))
    results = [c for m in users if isinstance(m.get('content'), list) for c in m['content']
               if isinstance(c, dict) and c.get('type') == 'tool_result']
    blocks = [t.strip() for t in _texts(users[0])] if users else []
    kind = next((name for marker, name in MARKERS if any(b.startswith(marker) for b in blocks)), None)
    if kind is None:
        if not body.get('tools'):
            kind = 'probe' if body.get('max_tokens') == 1 else 'aux'
        elif 'KLC root' in every:
            kind = 'root'
        else:
            kind = 'other'
    tag = re.search(r'KLC root (\w+)', every)
    return {'kind': kind, 'tag': tag.group(1) if tag else None, 'tool_results': len(results),
            'later': 'KLC later' in every,
            'continued': 'KLC continued' in every, 'tools': sorted(t.get('name') for t in body.get('tools') or []),
            'first': first[:48], 'result_text': json.dumps(results[-1].get('content'))[:300] if results else None}


def frames(model, ident, action):
    start = {'type': 'message_start', 'message': {'id': 'msg_klc_%d' % ident, 'type': 'message',
             'role': 'assistant', 'model': model, 'content': [], 'stop_reason': None, 'stop_sequence': None,
             'usage': {'input_tokens': 10, 'output_tokens': 0}}}
    if action[0] == 'tool':
        _, name, args = action
        block = [{'type': 'content_block_start', 'index': 0, 'content_block': {
                  'type': 'tool_use', 'id': 'toolu_klc_%d' % ident, 'name': name, 'input': {}}},
                 {'type': 'content_block_delta', 'index': 0, 'delta': {
                  'type': 'input_json_delta', 'partial_json': json.dumps(args)}}]
        stop = 'tool_use'
    else:
        block = [{'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                 {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': action[1]}}]
        stop = 'end_turn'
    tail = [{'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None},
             'usage': {'output_tokens': 5}}, {'type': 'message_stop'}]
    return [start, *block] if action[0] == 'hold' else [start, *block, *tail]


def sse(items):
    return b''.join(('event: %s\ndata: %s\n\n' % (f['type'], json.dumps(f))).encode() for f in items)


class Upstream:
    """Fake Anthropic Messages endpoint behind the gate. `script(info)` returns
    ('text', s), ('tool', name, input) or ('hold', s): a held stream pings every
    100 ms until the client goes away or the fixture closes."""

    def __init__(self, labels, script, record):
        self.labels, self.script, self.record = labels, script, record
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.count = 0
        self.active = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *args):
                return

            def do_POST(self):
                owner.handle(self)

            def do_GET(self):
                owner.record('upstream_other', method='GET', path=self.path[:64])
                self.send_error(404)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def label(self, header):
        return next((name for token, name in self.labels.items() if header == 'Bearer ' + token), 'other')

    def handle(self, request):
        raw = request.rfile.read(int(request.headers.get('Content-Length', 0) or 0))
        with self.lock:
            self.count += 1
            ident = self.count
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        info = classify(body)
        info.update(id=ident, label=self.label(request.headers.get('Authorization', '')),
                    model=body.get('model'), stream=body.get('stream') is True, max_tokens=body.get('max_tokens'),
                    path=request.path.split('?')[0])
        self.record('upstream_request', **info)
        if not info['stream']:
            data = json.dumps({'id': 'msg_klc_%d' % ident, 'type': 'message', 'role': 'assistant',
                               'model': body.get('model'), 'content': [{'type': 'text', 'text': 'ok'}],
                               'stop_reason': 'end_turn', 'stop_sequence': None,
                               'usage': {'input_tokens': 1, 'output_tokens': 1}}).encode()
            request.send_response(200)
            request.send_header('Content-Type', 'application/json')
            request.send_header('Content-Length', str(len(data)))
            request.end_headers()
            request.wfile.write(data)
            return
        action = self.script(info) or ('text', 'KLC fixture reply')
        request.send_response(200)
        request.send_header('Content-Type', 'text/event-stream')
        request.end_headers()
        try:
            request.wfile.write(sse(frames(body.get('model'), ident, action)))
            request.wfile.flush()
            if action[0] != 'hold':
                return
            with self.lock:
                self.active[ident] = info['kind']
            self.record('stream_held', id=ident, kind=info['kind'], label=info['label'])
            while not self.done.wait(0.1):
                request.wfile.write(b'event: ping\ndata: {"type": "ping"}\n\n')
                request.wfile.flush()
        except OSError:
            self.record('stream_disconnected', id=ident, kind=info['kind'])
        finally:
            with self.lock:
                self.active.pop(ident, None)

    def held(self, kinds=None):
        with self.lock:
            return sorted(i for i, k in self.active.items() if kinds is None or k in kinds)

    def close(self):
        self.done.set()
        self.server.shutdown()
        self.server.server_close()


def make_ca(directory, hosts=('platform.claude.com',)):
    """Disposable self-signed CA/leaf for the refresh fixture; trusted only by the child
    through NODE_EXTRA_CA_CERTS. Requires /usr/bin/openssl."""
    cert, key = Path(directory) / 'fixture-ca.pem', Path(directory) / 'fixture-key.pem'
    result = subprocess.run(['/usr/bin/openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                             '-keyout', str(key), '-out', str(cert), '-days', '1',
                             '-subj', '/CN=KLC disposable fixture',
                             '-addext', 'subjectAltName=' + ','.join('DNS:' + h for h in hosts),
                             '-addext', 'basicConstraints=critical,CA:TRUE'],
                            capture_output=True, timeout=20, env={'PATH': '/usr/bin:/bin'}, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise RuntimeError('openssl failed')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return cert, context


class RefreshServer:
    """Loopback TLS stand-in for platform.claude.com POST /v1/oauth/token.
    Grants `grant` (new access, new refresh, same account) for the expected refresh token."""

    def __init__(self, context, expected_refresh, grant, record):
        self.context, self.expected, self.grant, self.record = context, expected_refresh, grant, record
        self.requests = 0
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

    def handle(self, raw):
        raw.settimeout(5)
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
                try:
                    fields = json.loads(body or b'{}')
                except ValueError:
                    fields = {}
                self.requests += 1
                ok = (len(first) == 3 and first[0] == 'POST' and first[1].split('?')[0] == '/v1/oauth/token'
                      and isinstance(fields, dict) and fields.get('grant_type') == 'refresh_token'
                      and fields.get('refresh_token') == self.expected)
                self.record('refresh_request', method=first[0] if first else None,
                            path=first[1].split('?')[0] if len(first) > 1 else None,
                            grant_type=fields.get('grant_type') if isinstance(fields, dict) else None,
                            expected_refresh=ok, authorization_present='authorization' in headers)
                status, payload = (200, self.grant) if ok else (400, {'error': 'invalid_grant'})
                data = json.dumps(payload).encode()
                tls.sendall(('HTTP/1.1 %d Fixture\r\nContent-Type: application/json\r\nContent-Length: %d\r\n'
                             'Connection: close\r\n\r\n' % (status, len(data))).encode() + data)
        except (OSError, ssl.SSLError, ValueError) as error:
            self.record('refresh_error', error=type(error).__name__)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ConnectProxy:
    """Counting loopback CONNECT proxy that models the launcher's HTTPS_PROXY for the
    gate's own upstream connections (provisional decision); loopback targets only."""

    def __init__(self, record):
        self.record, self.tunnels = record, 0
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

    def handle(self, client):
        head = b''
        while b'\r\n\r\n' not in head and len(head) < 4096:
            chunk = client.recv(1)
            if not chunk:
                return
            head += chunk
        parts = head.split(b'\r\n', 1)[0].decode('latin-1').split()
        host, _, port = parts[1].rpartition(':') if len(parts) == 3 and parts[0] == 'CONNECT' else ('', '', '')
        if host != '127.0.0.1' or not port.isdigit():
            client.sendall(b'HTTP/1.1 403 Forbidden\r\n\r\n')
            return
        self.tunnels += 1
        self.record('proxy_tunnel', port_is_upstream=True)
        upstream = socket.create_connection((host, int(port)), timeout=5)
        client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')

        def pump(a, b):
            try:
                while True:
                    data = a.recv(65536)
                    if not data:
                        break
                    b.sendall(data)
            except OSError:
                pass
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()
        pump(client, upstream)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FileServer:
    """Serves a directory over loopback HTTP (dumb git protocol and a simple index);
    records each request path class. Outside every boundary."""

    def __init__(self, directory, record, name):
        owner = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(directory), **kwargs)

            def log_message(self, *args):
                return

            def do_GET(self):
                owner.hits += 1
                record('file_server_request', server=name, path=self.path.split('?')[0][:80])
                super().do_GET()

        self.hits = 0
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_index(directory):
    """A one-package simple index: /simple/klc-fixture/ links one wheel-named file."""
    payload = b'KLC fixture package payload\n'
    digest = hashlib.sha256(payload).hexdigest()
    files = Path(directory) / 'files'
    files.mkdir(parents=True)
    (files / 'klc_fixture-1.0-py3-none-any.whl').write_bytes(payload)
    page = Path(directory) / 'simple' / 'klc-fixture'
    page.mkdir(parents=True)
    (page / 'index.html').write_text('<a href="/files/klc_fixture-1.0-py3-none-any.whl#sha256=%s">'
                                     'klc_fixture-1.0-py3-none-any.whl</a>\n' % digest)
    return digest


def make_git_repo(directory, env):
    """A bare repository served by the dumb HTTP protocol (update-server-info)."""
    work = Path(directory) / 'work'
    bare = Path(directory) / 'repo.git'
    work.mkdir(parents=True)
    run = lambda *args, cwd=None: subprocess.run(['git', *args], cwd=cwd, env=env, check=True,
                                                 capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    run('init', '-q', '-b', 'main', str(work))
    (work / 'README').write_text('KLC fixture repository\n')
    run('add', 'README', cwd=work)
    run('-c', 'user.name=KLC', '-c', 'user.email=klc@example.invalid', 'commit', '-q', '-m', 'fixture', cwd=work)
    run('clone', '-q', '--bare', str(work), str(bare))
    run('update-server-info', cwd=bare)
    return bare


def wait_for(predicate, timeout, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def private_tree(root, names):
    for name in names:
        Path(root, name).mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
