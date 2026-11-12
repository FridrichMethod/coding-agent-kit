"""Bounded loopback-only HTTP research relay, not a product proxy/protocol.

One inherited monitor socket sends experiment heartbeats. No reconnect or resume
path exists. A lock covers lease state, forwarding admission and stream ownership.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import select
import socket
import sys
import threading
import time

LEASE = 0.8  # Experiment timing only, not a proposed product default.


def emit(event, **data):
    print(json.dumps({'event': event, 'when': time.monotonic(), **data}), flush=True)


def shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


def gate(fd, upstream_port):
    monitor = socket.socket(fileno=fd)
    lock = threading.RLock()
    active = {}
    state = {'deadline': None, 'latched': False, 'next_id': 0}

    def latch(reason):
        with lock:
            if state['latched']:
                return
            state['latched'] = True
            emit('gate_latched', reason=reason, active_ids=sorted(active))
            for client, upstream in active.values():
                shutdown(client)
                shutdown(upstream)

    def watch():
        while True:
            with lock:
                deadline = state['deadline']
                if deadline is not None and time.monotonic() >= deadline:
                    latch('lease_expired')
            if not select.select([monitor], [], [], .02)[0]:
                continue
            pulse = monitor.recv(64)
            if not pulse:
                latch('monitor_eof')
                return
            with lock:
                deadline = state['deadline']
                if deadline is not None and time.monotonic() >= deadline:
                    latch('lease_expired')
                if state['latched']:
                    emit('heartbeat_after_latch', ignored=True)
                else:
                    first = state['deadline'] is None
                    state['deadline'] = time.monotonic() + LEASE
                    if first:
                        emit('gate_open', lease_seconds=LEASE)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if not self.path.startswith('/backend-api/codex/models?'):
                self.send_error(503)
                return
            data = b'{"models": []}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != '/backend-api/codex/responses':
                self.send_error(503)
                return
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            upstream = None
            ident = None
            try:
                with lock:
                    deadline = state['deadline']
                    if deadline is not None and time.monotonic() >= deadline:
                        latch('lease_expired')
                    if deadline is None or state['latched']:
                        data = json.loads(body)
                        text = json.dumps(data.get('input', []))
                        emit('gate_reject', old_queue='Fixture previously queued prompt' in text,
                             programmatic='Fixture programmatic continuation after loss' in text)
                        self.send_error(503)
                        return
                    state['next_id'] += 1
                    ident = state['next_id']
                    upstream = socket.socket()
                    upstream.settimeout(2)
                    active[ident] = (self.connection, upstream)
                    upstream.connect(('127.0.0.1', upstream_port))
                    headers = ''.join(f'{key}: {self.headers[key]}\r\n' for key in
                                      ('Authorization', 'ChatGPT-Account-ID', 'session-id')
                                      if key in self.headers)
                    upstream.sendall((f'POST {self.path} HTTP/1.0\r\nHost: 127.0.0.1\r\n'
                                      f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n'
                                      f'X-Fixture-Request: {ident}\r\n{headers}\r\n').encode() + body)
                    emit('gate_forward', request_id=ident)
                # Raw finite fixture HTTP/SSE bytes, no CONNECT/WS/credential service.
                self.connection.settimeout(2)
                while chunk := upstream.recv(65536):
                    self.connection.sendall(chunk)
            except OSError:
                pass
            finally:
                with lock:
                    if ident is not None:
                        active.pop(ident, None)
                        emit('gate_stream_closed', request_id=ident)
                    if upstream is not None:
                        shutdown(upstream)
                    shutdown(self.connection)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=watch, daemon=True).start()
    emit('gate_ready', port=server.server_port, admission='closed')
    server.serve_forever()


def monitor(fd):
    sock = socket.socket(fileno=fd)
    automatic = True
    connected = True
    while True:
        if select.select([sys.stdin], [], [], .1)[0]:
            command = sys.stdin.readline().strip()
            if not command:
                return
            if command == 'quiet':
                automatic = False
            elif command == 'eof':
                sock.close()
                connected = False
                automatic = False
            elif command == 'pulse':
                sock.sendall(b'P')
        elif automatic and connected:
            try:
                sock.sendall(b'P')
            except OSError:
                return


if __name__ == '__main__':
    if sys.argv[1] == 'monitor':
        monitor(int(sys.argv[2]))
    else:
        gate(int(sys.argv[2]), int(sys.argv[3]))
