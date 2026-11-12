"""PC5 finite loopback relay with owner-only grants; no network control endpoint.

PC4 established independent process-loss behavior. This experiment only adds
repeatable lease grants behind trusted terminal input and fresh quota checks.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time


def shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


class FixtureGate:
    def __init__(self, upstream_port, record, route='/backend-api/codex/responses'):
        self.lock = threading.RLock()
        self.deadline = None
        self.active = {}
        self.next_id = 0
        self.grants = 0
        self.record = record
        self.done = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                self.send_error(503)
                owner.record('alternate_rejected', method='CONNECT', path=self.path)

            def do_GET(self):
                if self.headers.get('Upgrade') or '/models?' not in self.path:
                    owner.record('alternate_rejected', method='GET', path=self.path)
                    self.send_error(503)
                    return
                data = b'{"models": []}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.path != route:
                    owner.record('alternate_rejected', method='POST', path=self.path)
                    self.send_error(503)
                    return
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                upstream = None
                ident = None
                try:
                    with owner.lock:
                        owner.expire()
                        if owner.deadline is None:
                            owner.record('gate_reject', old_queue=b'Fixture previously queued prompt' in body,
                                         programmatic=b'Fixture manually continued' in body)
                            self.send_error(503)
                            return
                        owner.next_id += 1
                        ident = owner.next_id
                        upstream = socket.socket()
                        upstream.settimeout(2)
                        owner.active[ident] = (self.connection, upstream)
                        upstream.connect(('127.0.0.1', upstream_port))
                        headers = ''.join(f'{key}: {value}\r\n' for key, value in self.headers.items()
                                          if key.lower() not in ('host', 'connection', 'content-length'))
                        upstream.sendall((f'POST {route} HTTP/1.0\r\nHost: 127.0.0.1\r\n'
                            f'Content-Length: {len(body)}\r\nX-Fixture-Request: {ident}\r\n'
                            f'{headers}\r\n').encode() + body)
                        owner.record('gate_forward', request_id=ident, grant=owner.grants)
                    while chunk := upstream.recv(65536):
                        self.connection.sendall(chunk)
                except OSError:
                    pass
                finally:
                    with owner.lock:
                        if ident is not None:
                            owner.active.pop(ident, None)
                        if upstream is not None:
                            shutdown(upstream)
                        shutdown(self.connection)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.watchdog = threading.Thread(target=self.watch, daemon=True)
        self.watchdog.start()

    @property
    def port(self):
        return self.server.server_port

    def grant(self, seconds=20):
        with self.lock:
            if self.deadline is not None:
                raise RuntimeError('duplicate healthy lease grant')
            self.grants += 1
            self.deadline = time.monotonic() + seconds
            self.record('gate_grant', grant=self.grants, seconds=seconds)

    def pause(self, reason='quota'):
        with self.lock:
            self.deadline = None
            self.record('gate_pause', reason=reason, active_ids=sorted(self.active))
            for client, upstream in self.active.values():
                shutdown(client)
                shutdown(upstream)

    def expire(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.pause('lease_expired')

    def watch(self):
        while not self.done.wait(.02):
            with self.lock:
                self.expire()

    def close(self):
        self.done.set()
        self.pause('cleanup')
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.watchdog.join(timeout=2)
