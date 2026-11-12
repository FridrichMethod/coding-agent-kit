"""QX loopback gate prototype: one listener for chatgpt_base_url and the model provider.

Research fixture only, not a production relay: plain HTTP to a loopback upstream,
no TLS, no WebSocket. It forwards native usage reads and feeds the exchange to the
Lease, decides each model request before any upstream byte, passes admitted SSE
through, and closes registered streams when the lease pauses. CONNECT is refused
and recorded. Records name credentials only by fixture label, never by value.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from urllib.parse import urlsplit

USAGE_PATHS = ('/backend-api/wham/usage', '/api/codex/usage')
HOP = {'host', 'connection', 'keep-alive', 'proxy-connection', 'proxy-authorization', 'te', 'trailer',
       'transfer-encoding', 'upgrade', 'content-length', 'accept-encoding'}
# Non-secret header values worth keeping; every other header is recorded by name only.
SHOWN = {'chatgpt-account-id', 'x-openai-fedramp', 'originator', 'openai-beta', 'accept', 'content-type'}


class Gate:
    def __init__(self, lease, upstream, labels=None, timeout=10, rebind=None, hold=0.0):
        self.lease, self.timeout = lease, timeout
        # Optional hold-and-rebind: on a new same-account bearer, ask the controller for a native
        # usage read and hold the request (no upstream byte) for at most `hold` seconds.
        self.rebind, self.hold = rebind, hold
        target = urlsplit(upstream)
        self.upstream = (target.hostname, target.port)
        self.labels = dict(labels or {})  # credential digest -> fixture label
        self.lock = threading.Lock()  # admission lock: decision and registration are atomic
        self.streams = {}
        self.events = []
        lease.on_close = self.close_streams
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def label(self, bearer):
        digest = self.lease.digest(bearer)
        with self.lock:
            return self.labels.setdefault(digest, 'unlabelled-%d' % (len(self.labels) + 1))

    def record(self, event, **data):
        with self.lock:
            self.events.append({'event': event, **data})

    def close_streams(self, reason):
        # Called by the lease while the admission lock is held: no new stream can register.
        for connection in list(self.streams.values()):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.events.append({'event': 'streams_closed', 'reason': reason, 'count': len(self.streams)})

    def close(self):
        with self.lock:
            self.close_streams('gate-shutdown')
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def summary(self):
                value = self.headers.get('Authorization') or ''
                bearer = value[7:] if value.startswith('Bearer ') else None
                agent = (self.headers.get('User-Agent') or '').split(' ')[0]
                return bearer, {'method': self.command, 'path': self.path.split('?')[0],
                                'query': '?' in self.path,
                                'credential': gate.label(bearer) if bearer else None,
                                'authorization_scheme': value.split(' ')[0] if value else None,
                                'user_agent_product': agent,
                                'headers': sorted(name.lower() for name in self.headers.keys()),
                                'shown': {name.lower(): value for name, value in self.headers.items()
                                          if name.lower() in SHOWN}}

            def forward(self, body=None):
                connection = http.client.HTTPConnection(*gate.upstream, timeout=gate.timeout)
                headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP}
                if body is not None:
                    headers['Content-Length'] = str(len(body))
                connection.request(self.command, self.path, body=body, headers=headers)
                return connection, connection.getresponse()

            def relay_head(self, response, length=None):
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() not in HOP:
                        self.send_header(name, value)
                if length is not None:
                    self.send_header('Content-Length', str(length))
                self.end_headers()

            def refuse(self, reason):
                data = json.dumps({'error': {'type': 'server_error', 'code': 'quota_reserve_gate',
                                             'message': 'quota reserve gate refused: ' + reason}}).encode()
                self.send_response(503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_CONNECT(self):
                gate.record('connect_refused', destination=self.path)
                self.send_error(403)

            def do_GET(self):
                bearer, info = self.summary()
                try:
                    connection, response = self.forward()
                    data = response.read()
                except OSError as error:
                    gate.record('upstream_error', **info, error=type(error).__name__)
                    return self.refuse('upstream unavailable')
                outcome = None
                if info['path'] in USAGE_PATHS:
                    with gate.lock:
                        outcome = gate.lease.observe_usage(bearer, self.headers.get('ChatGPT-Account-ID'),
                                                           response.status, data)
                gate.record('get', **info, status=response.status, lease=outcome,
                            epoch=gate.lease.epoch, paused=gate.lease.paused)
                self.relay_head(response, len(data))
                self.wfile.write(data)
                connection.close()

            def do_POST(self):
                bearer, info = self.summary()
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if not info['path'].endswith('/responses'):
                    gate.record('post_other', **info)
                    connection, response = self.forward(body)
                    data = response.read()
                    self.relay_head(response, len(data))
                    self.wfile.write(data)
                    connection.close()
                    return
                try:
                    model = json.loads(body).get('model')
                except (ValueError, AttributeError):
                    model = None
                account = self.headers.get('ChatGPT-Account-ID')
                key = object()

                def decide():
                    with gate.lock:
                        admitted, reason = gate.lease.admit(bearer, account, model)
                        gate.events.append({'event': 'admission', **info, 'model': model, 'admitted': admitted,
                                            'reason': reason, 'epoch': gate.lease.epoch})
                        if admitted:
                            gate.streams[key] = self.connection
                    return admitted, reason
                admitted, reason = decide()
                if reason == 'rebind-required' and gate.rebind is not None and gate.hold > 0:
                    digest = gate.lease.digest(bearer)
                    gate.rebind()
                    deadline = time.monotonic() + gate.hold
                    while time.monotonic() < deadline:
                        time.sleep(.02)
                        with gate.lock:
                            lease = gate.lease
                            if lease.paused or (lease.credential == digest and lease.observation is not None):
                                break
                    gate.record('rebind_hold_ended', credential=info['credential'])
                    admitted, reason = decide()
                if not admitted:
                    return self.refuse(reason)  # never 401: that would start native refresh
                try:
                    connection, response = self.forward(body)
                    with gate.lock:
                        if response.status == 200:
                            gate.lease.observe_headers(bearer, account, dict(response.getheaders()))
                        gate.events.append({'event': 'model_upstream', 'status': response.status,
                                            'credential': info['credential']})
                    self.relay_head(response)
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    connection.close()
                except OSError as error:
                    gate.record('stream_ended', error=type(error).__name__)
                finally:
                    with gate.lock:
                        gate.streams.pop(key, None)

        return Handler
