"""Claude Messages-only research adaptation of PC4's lease/stream fixture.

No credential discovery, refresh, CONNECT, alternative providers or reopen API.
The PC4 monitor is imported unchanged. This is not a production gateway.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import select
import socket
import sys
import threading
import time

spec = importlib.util.spec_from_file_location('pc4_gate', Path(__file__).resolve().parent.parent / 'pc_loss/gate.py')
pc4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc4)


def gate(fd, port):
    monitor = socket.socket(fileno=fd)
    lock = threading.RLock()
    active = {}
    state = {'deadline': None, 'latched': False, 'next': 0}

    def latch(reason):
        with lock:
            if state['latched']:
                return
            state['latched'] = True
            pc4.emit('gate_latched', reason=reason, active_ids=sorted(active))
            for pair in active.values():
                for sock in pair:
                    pc4.shutdown(sock)

    def watch():
        while True:
            with lock:
                if state['deadline'] is not None and time.monotonic() >= state['deadline']:
                    latch('lease_expired')
            if not select.select([monitor], [], [], .02)[0]:
                continue
            pulse = monitor.recv(64)
            if not pulse:
                latch('monitor_eof')
                return
            with lock:
                if state['deadline'] is not None and time.monotonic() >= state['deadline']:
                    latch('lease_expired')
                if state['latched']:
                    pc4.emit('heartbeat_after_latch', ignored=True)
                else:
                    first = state['deadline'] is None
                    state['deadline'] = time.monotonic() + pc4.LEASE
                    if first:
                        pc4.emit('gate_open')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path not in ('/v1/messages', '/v1/messages?beta=true'):
                self.send_error(503)
                return
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            upstream = None
            ident = None
            try:
                with lock:
                    if state['deadline'] is not None and time.monotonic() >= state['deadline']:
                        latch('lease_expired')
                    if state['deadline'] is None or state['latched']:
                        text = json.dumps(json.loads(body).get('messages', []))
                        pc4.emit('gate_reject', old_queue='Fixture previously queued' in text,
                                 later_prompt='Fixture later prompt' in text)
                        self.send_error(503)
                        return
                    state['next'] += 1
                    ident = state['next']
                    upstream = socket.socket()
                    upstream.settimeout(2)
                    active[ident] = (self.connection, upstream)
                    upstream.connect(('127.0.0.1', port))
                    headers = ''.join(f'{key}: {self.headers[key]}\r\n' for key in
                                      ('Authorization', 'x-api-key', 'anthropic-version', 'anthropic-beta')
                                      if key in self.headers)
                    upstream.sendall((f'POST {self.path} HTTP/1.0\r\nHost: 127.0.0.1\r\n'
                                      f'Content-Type: application/json\r\nContent-Length: {len(body)}\r\n'
                                      f'X-Fixture-Request: {ident}\r\n{headers}\r\n').encode() + body)
                    pc4.emit('gate_forward', request_id=ident)
                self.connection.settimeout(2)
                while chunk := upstream.recv(65536):
                    self.connection.sendall(chunk)
            except OSError:
                pass
            finally:
                with lock:
                    if ident is not None:
                        active.pop(ident, None)
                        pc4.emit('gate_stream_closed', request_id=ident)
                    if upstream is not None:
                        pc4.shutdown(upstream)
                    pc4.shutdown(self.connection)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=watch, daemon=True).start()
    pc4.emit('gate_ready', port=server.server_port)
    server.serve_forever()


if __name__ == '__main__':
    if sys.argv[1] == 'monitor':
        pc4.monitor(int(sys.argv[2]))
    else:
        gate(int(sys.argv[2]), int(sys.argv[3]))
