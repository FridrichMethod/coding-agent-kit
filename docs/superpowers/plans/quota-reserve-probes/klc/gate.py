"""KLC research gate: the only exit of one protected Claude invocation.

Runs outside the boundary. It composes the reviewed QC admission contract
(imported read-only from ../qc) with PA1C's stream-closing rule and adds what
QC left to K: an independent lease timer, a monitor channel whose EOF latches,
a trusted control channel for manual continuation, an exact-host CONNECT
allowlist for the native refresh host, and an inherited listening socket that
the invocation owns (the port holder), so a restarted gate serves the same
port and starts paused.

Invariant: every byte sent upstream (Messages request or tunnel payload) is
written while holding the admission lock, and only for a stream that is still
registered under an open lease. A latch that takes the lock first therefore
leaves zero upstream bytes. A pause closes every registered client and
upstream socket. The bearer lives only in this process, memory-only.

python3 -I -B gate.py CONFIG.json --listen-fd N --monitor-fd N --control-fd N
Research code only: not a production gateway, no TLS interception.
"""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(os.path.realpath(__file__)).parent
QC = HERE.parent / 'qc'
BODY_LIMIT = 8 * 1024 * 1024
TICK = 0.02
HOP = frozenset(('host', 'connection', 'content-length', 'keep-alive', 'proxy-authorization',
                 'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade'))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


admission = _load('klc_qc_admission', QC / 'admission.py')
bearer_mod, usage_mod = admission.bearer_mod, admission.usage_mod


class EventLog(list):
    """QC's controller appends dicts to its event list; mirror each one to the emitter."""

    def __init__(self, emit):
        super().__init__()
        self.emit = emit

    def append(self, item):
        super().append(item)
        self.emit('controller', **item)


class ComposedController(admission.AdmissionController):
    """QC AdmissionController plus the K-owned timer, latch, restart and tunnel rules."""

    def latch(self, reason):
        """Monitor loss or another outside trigger: close everything now."""
        with self.lock:
            if self.state in ('open', 'checking'):
                self._pause(reason)
                return True
            return False

    def tick(self):
        """Independent of admit(): close every registered stream at the lease deadline."""
        with self.lock:
            if self.state == 'open' and self.clock.monotonic() >= self.lease.deadline:
                self._pause('freshness_expired')
                return True
        return False

    def restart_paused(self, reason='gate_restart'):
        """A restarted gate never inherits admission: it starts paused with no lease.
        QC's continue_manually() already accepts a pause without a lease."""
        with self.lock:
            self.state, self.reason, self.lease = 'paused', reason, None
            self.paused_monotonic = self.clock.monotonic()
            self._continuations.clear()
            self._event('latched', state='paused', reason=reason, closed=0)

    def startup(self, observed):
        decision = super().startup(observed)
        if decision.ok and self.lease.unproved_rows:
            # Composition choice: unproved rows refuse at startup, not per request.
            with self.lock:
                self.state, self.lease = 'checking', None
                self._pause('quota_row_unproved')
            return admission.Decision(False, 'quota_row_unproved')
        return decision

    def admit_connect(self, target, allow, close):
        """CONNECT only to an exact allowlisted host:port, only while a fresh lease is open."""
        with self.lock:
            if target not in allow:
                self._event('connect_refused', reason='connect_unlisted')
                return admission.Decision(False, 'connect_unlisted')
            if self.state != 'open':
                self._event('connect_refused', reason=self.reason or self.state)
                return admission.Decision(False, self.reason or self.state)
            if self.clock.monotonic() >= self.lease.deadline:
                self._pause('freshness_expired')
                return admission.Decision(False, 'freshness_expired')
            stream_id = self._register(close)
            self._event('connect_admitted', stream=stream_id)
            return admission.Decision(True, 'tunnel', stream_id)

    def live(self, stream_id):
        """Caller holds self.lock: the stream is still registered under an open lease."""
        return self.state == 'open' and stream_id in self.streams


def shut(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class Emitter:
    def __init__(self, stream):
        self.stream, self.lock = stream, threading.Lock()

    def __call__(self, event, **fields):
        line = json.dumps({'event': event, 't': time.monotonic(), **fields}, sort_keys=True)
        with self.lock:
            try:
                self.stream.write(line + '\n')
                self.stream.flush()
            except (OSError, ValueError):
                pass


class Gate:
    def __init__(self, config, controller, emit):
        self.config, self.controller, self.emit = config, controller, emit
        self.upstream = ('127.0.0.1', int(config['upstream_port']))
        self.allow = {target: ('127.0.0.1', int(port)) for target, port in config.get('connect_allow', {}).items()}
        proxy = config.get('upstream_proxy')
        self.proxy = (proxy[0], int(proxy[1])) if proxy else None

    def open_upstream(self, address):
        """Wrapper-originated connection; via the launcher's proxy captured at startup, if any."""
        if self.proxy is None:
            return socket.create_connection(address, timeout=5)
        sock = socket.create_connection(self.proxy, timeout=5)
        sock.sendall(('CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n' % (address * 2)).encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = sock.recv(1)
            if not chunk:
                raise OSError('proxy closed')
            head += chunk
            if len(head) > 4096:
                raise OSError('proxy head too large')
        if head.split(b' ', 2)[1:2] != [b'200']:
            raise OSError('proxy refused')
        return sock


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        return

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass
        self.close_connection = True

    def _refuse(self, reason):
        message = ('coding-agent-kit quota gate: paused or refused (%s); continue only from the '
                   'trusted terminal after a fresh check' % reason)
        self._reply(403, {'type': 'error', 'error': {'type': 'permission_error', 'message': message}})

    def _other(self):
        gate = self.server.gate
        gate.emit('route_refused', method=self.command, path=self.path.split('?')[0][:64])
        self._reply(404, {'type': 'error', 'error': {'type': 'not_found_error'}})

    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _other

    def do_POST(self):
        gate = self.server.gate
        began = time.monotonic()
        if self.path.split('?')[0] != '/v1/messages':
            return self._other()
        if self.headers.get('Transfer-Encoding'):
            return self._reply(411, {'type': 'error', 'error': {'type': 'length_required'}})
        length = self.headers.get('Content-Length', '')
        if not length.isdigit() or int(length) > BODY_LIMIT:
            return self._reply(413, {'type': 'error', 'error': {'type': 'request_too_large'}})
        body = self.rfile.read(int(length))
        try:
            parsed = json.loads(body)
            model = parsed.get('model')
            shape = {'max_tokens': parsed.get('max_tokens'), 'sse': parsed.get('stream') is True,
                     'tools': len(parsed.get('tools') or [])}
        except (ValueError, AttributeError):
            model, shape = None, {}
        holder = {'client': self.connection, 'upstream': None}

        def close():
            shut(holder['client'])
            shut(holder['upstream'])

        controller = gate.controller
        decision = controller.admit(list(self.headers.items()), model, close)
        admitted_at = time.monotonic()
        gate.emit('decision', ok=decision.ok, reason=decision.reason, stream=decision.stream_id,
                  admit_ms=round((admitted_at - began) * 1000, 3), **shape)
        if not decision.ok:
            return self._refuse(decision.reason)
        upstream = None
        try:
            with controller.lock:
                if not controller.live(decision.stream_id):
                    gate.emit('aborted_before_upstream', stream=decision.stream_id)
                    return self._refuse(controller.reason or 'closed')
                upstream = gate.open_upstream(gate.upstream)
                holder['upstream'] = upstream
                head = ['POST %s HTTP/1.1' % self.path, 'Host: upstream.fixture', 'Connection: close',
                        'Content-Length: %d' % len(body)]
                head += ['%s: %s' % (k, v) for k, v in self.headers.items() if k.lower() not in HOP]
                upstream.sendall(('\r\n'.join(head) + '\r\n\r\n').encode('latin-1') + body)
                # Emitted inside the lock: every forwarded event precedes any later latch event.
                gate.emit('forwarded', stream=decision.stream_id, epoch=controller.lease.epoch,
                          gate_ms=round((time.monotonic() - began) * 1000, 3))
            upstream.settimeout(None)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
        except OSError:
            pass
        finally:
            controller.release(decision.stream_id)
            if upstream is not None:
                shut(upstream)
                upstream.close()
            gate.emit('stream_closed', stream=decision.stream_id)
            self.close_connection = True

    def do_CONNECT(self):
        gate = self.server.gate
        target = self.path
        holder = {'client': self.connection, 'upstream': None}

        def close():
            shut(holder['client'])
            shut(holder['upstream'])

        controller = gate.controller
        decision = controller.admit_connect(target, gate.allow, close)
        gate.emit('connect', target=target[:96], ok=decision.ok, reason=decision.reason, stream=decision.stream_id)
        if not decision.ok:
            try:
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.send_header('Connection', 'close')
                self.end_headers()
            except OSError:
                pass
            self.close_connection = True
            return
        upstream = None
        sent = {'up': 0, 'down': 0}
        try:
            with controller.lock:
                if not controller.live(decision.stream_id):
                    self.close_connection = True
                    return
                upstream = gate.open_upstream(gate.allow[target])
                holder['upstream'] = upstream
            self.send_response(200, 'Connection established')
            self.end_headers()
            self.wfile.flush()

            def client_to_upstream():
                try:
                    while True:
                        chunk = self.rfile.read1(65536)
                        if not chunk:
                            break
                        with controller.lock:
                            if not controller.live(decision.stream_id):
                                break
                            upstream.sendall(chunk)
                            sent['up'] += len(chunk)
                except (OSError, ValueError):
                    pass
                shut(upstream)

            pump = threading.Thread(target=client_to_upstream, daemon=True)
            pump.start()
            upstream.settimeout(None)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
                sent['down'] += len(chunk)
            pump.join(5)
        except OSError:
            pass
        finally:
            controller.release(decision.stream_id)
            if upstream is not None:
                shut(upstream)
                upstream.close()
            gate.emit('tunnel_closed', stream=decision.stream_id, bytes_up=sent['up'], bytes_down=sent['down'])
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, listener, gate):
        super().__init__(('127.0.0.1', 0), Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = listener
        self.server_name, self.server_port = '127.0.0.1', listener.getsockname()[1]
        self.gate = gate


def build_controller(config, emit, clock=None):
    port, config_dir = int(config['usage_port']), config['config_dir']
    usage_ep = usage_mod.Endpoint('http', '127.0.0.1', port)
    profile_ep = usage_mod.Endpoint('http', '127.0.0.1', port, usage_mod.PROFILE_PATH)

    def timed(name, call):
        def run(observed):
            began = time.monotonic()
            result = call(observed)
            emit('fetch', what=name, ms=round((time.monotonic() - began) * 1000, 3),
                 ok=(result is not None) if name == 'profile' else result.outcome == 'response'
                 and result.http_status == 200)
            return result
        return run

    quota = timed('usage', lambda b: usage_mod.fetch(b, usage_ep))
    profile = timed('profile', lambda b: usage_mod.fetch_profile_identity(b, profile_ep))
    identity = admission.BearerProfileIdentity(lambda: admission.read_local_identity(config_dir), profile)
    p = config['policy']
    policy = admission.Policy(p['reserves'], max_age_s=p['max_age_s'],
                              revalidations_per_minute=p.get('revalidations_per_minute', 4),
                              allowed_models=tuple(p['allowed_models']))
    return ComposedController(tuple(config['registered']), policy, quota, identity, clock=clock,
                              events=EventLog(emit))


def store_bearer(config):
    return bearer_mod.read_linux_bearer(config['config_dir'], {'CLAUDE_CONFIG_DIR': config['config_dir']})


def run_monitor(sock, controller, emit):
    busy = threading.Lock()

    def poll():
        began = time.monotonic()
        decision = controller.monitor()
        emit('poll', ok=decision.ok, reason=decision.reason, ms=round((time.monotonic() - began) * 1000, 3))
        busy.release()

    buffer = b''
    while True:
        try:
            data = sock.recv(256)
        except OSError:
            data = b''
        if not data:
            emit('monitor_eof')
            controller.latch('monitor_eof')
            return
        buffer += data
        while b'\n' in buffer:
            line, buffer = buffer.split(b'\n', 1)
            if line == b'poll' and busy.acquire(blocking=False):
                threading.Thread(target=poll, daemon=True).start()


def run_control(sock, config, controller, emit):
    buffer = b''
    while True:
        try:
            data = sock.recv(4096)
        except OSError:
            return
        if not data:
            emit('control_eof')
            return
        buffer += data
        while b'\n' in buffer:
            line, buffer = buffer.split(b'\n', 1)
            try:
                op = json.loads(line).get('op')
            except (ValueError, AttributeError):
                op = None
            if op == 'continue':
                token = controller.issue_continuation()
                try:
                    observed = store_bearer(config)
                except bearer_mod.BearerRefused as error:
                    reply = {'op': op, 'ok': False, 'reason': error.reason}
                else:
                    decision = controller.continue_manually(token, observed)
                    reply = {'op': op, 'ok': decision.ok, 'reason': decision.reason}
            elif op == 'status':
                with controller.lock:
                    reply = {'op': op, 'state': controller.state, 'reason': controller.reason,
                             'epoch': controller.lease.epoch if controller.lease else None,
                             'streams': len(controller.streams)}
            else:
                reply = {'op': op, 'ok': False, 'reason': 'unknown_op'}
            emit('control', **reply)
            try:
                sock.sendall((json.dumps(reply) + '\n').encode())
            except OSError:
                return


def main(argv):
    config = json.loads(Path(argv[0]).read_text())
    fds = dict(zip(argv[1::2], (int(x) for x in argv[2::2])))
    emit = Emitter(sys.stdout)
    listener = socket.socket(fileno=fds['--listen-fd'])
    monitor = socket.socket(fileno=fds['--monitor-fd'])
    control = socket.socket(fileno=fds['--control-fd'])
    controller = build_controller(config, emit)
    if config.get('mode', 'startup') == 'restart':
        controller.restart_paused('gate_restart')
    else:
        began = time.monotonic()
        try:
            observed = store_bearer(config)
        except bearer_mod.BearerRefused as error:
            emit('startup_refused', reason=error.reason)
            return 3
        decision = controller.startup(observed)
        observed = None
        if not decision.ok:
            emit('startup_refused', reason=decision.reason)
            return 3
        emit('preflight', ms=round((time.monotonic() - began) * 1000, 3))
    gate = Gate(config, controller, emit)
    server = Server(listener, gate)

    def timer():
        while True:
            time.sleep(TICK)
            controller.tick()

    for target, args in ((timer, ()), (run_monitor, (monitor, controller, emit)),
                         (run_control, (control, config, controller, emit))):
        threading.Thread(target=target, args=args, daemon=True).start()
    emit('gate_ready', port=server.server_port, pid=os.getpid(), mode=config.get('mode', 'startup'))
    server.serve_forever(poll_interval=0.1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
