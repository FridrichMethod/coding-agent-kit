"""KLX composed gate: QX lease admission, PC4 loss latch and a launcher-held port in one process.

Research code only (not product runtime). It runs as its own process outside the boundary:

    python3 -I -B gate.py CONFIG.json

The launcher binds the loopback gate port and keeps its own copy of the listening descriptor for
the whole invocation, so nobody can take the port while this process is dead or restarting. It
passes the descriptor here with two private socketpairs: the launcher channel (labels, trusted
continuation and its cancellation after a failed check, status, rebind requests) and the monitor
channel (heartbeats only).

What it adds to the QX prototype (the QX report's "requirements on K"):
- Default-deny route table. While running it forwards the model-free reads native sent in QX and
  admits POST /backend-api/codex/responses through the lease. While paused it forwards only the
  usage read of the expected account; once the monitor is lost it forwards nothing.
- Lock to first byte. The upstream connection is opened outside the admission lock, and the route
  and lease are re-checked and the request bytes sent under the lock (model POSTs, forwarded GETs
  and the gate's own rebind read); model streams are also registered there. A latch shuts down
  every registered client and upstream socket under the same lock.
- Monitor. A tick thread drives Lease.tick() and the heartbeat lease, which starts at the first
  heartbeat. Monitor EOF, a stalled heartbeat or launcher EOF latches this gate instance for
  good; recovery is a restarted gate.
- A restarted gate starts latched ("gate-restarted"): only a trusted continuation plus a fresh
  passing usage read received after it reopens admission.
- CONNECT is refused except to the configured refresh host:port while not paused (K case 8a).
- WebSocket upgrades are refused; foreign-account requests latch identity before any upstream byte.
Credentials are named only by labels the launcher registers; bearer values are never logged.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import sys
import threading
import time

HERE = Path(__file__).resolve().parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


binding = _load('klx_qx_binding', HERE.parent / 'qx' / 'binding.py')
qx_gate = _load('klx_qx_gate', HERE.parent / 'qx' / 'gate.py')

USAGE_PATHS = qx_gate.USAGE_PATHS
MODEL_POSTS = qx_gate.MODEL_POSTS
RUNNING_GETS = qx_gate.FORWARDED_GETS  # model-free reads native 0.167.2 sent in QX
HOP = qx_gate.HOP | {'proxy-authenticate'}
LOSS = ('monitor-eof', 'monitor-stale', 'launcher-eof')


def describe(body):
    """(marker, kind) of a Responses request for evidence only: the latest user turn decides."""
    try:
        items = json.loads(body).get('input') or []
    except (ValueError, AttributeError):
        return None, None
    text, kind = '', 'turn'
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get('role') == 'user':
            parts = item.get('content')
            text = ' '.join(part.get('text', '') for part in parts if isinstance(part, dict)) \
                if isinstance(parts, list) else str(parts)
            kind = 'turn'
        elif item.get('type') == 'function_call_output':
            kind = 'tool_result'
    if text.startswith('Generate a concise'):
        kind = 'title'
    marker = 'KLX-' + text.split('KLX-', 1)[1].split()[0].strip('.,"\'') if 'KLX-' in text else None
    return marker, kind


class KLease(binding.Lease):
    """The QX lease plus a public latch for conditions the gate observes itself."""

    def latch(self, reason):
        self._pause(reason)

    def cancel_continuation(self, reason):
        """The launcher reported a failed check: no later read may answer that request."""
        self._disarm(reason)


def route(method, path, headers, *, paused, lost):
    """Default-deny route decision before any upstream byte -> (action, reason)."""
    if headers.get('Upgrade'):
        return 'refuse', 'websocket-refused'
    if lost:
        return 'refuse', 'paused:' + lost
    if method == 'GET' and path in USAGE_PATHS:
        return 'usage', None
    if method == 'GET' and path in RUNNING_GETS:
        return ('refuse', 'paused-non-usage') if paused else ('forward', None)
    if method == 'POST' and path in MODEL_POSTS:
        return 'admit', None
    return 'refuse', 'unlisted-path'


def connect_allowed(target, allow, *, paused, lost):
    """CONNECT is refused unless it names the exact allowed host:port and the gate is running."""
    if lost or paused:
        return False
    return target in allow


class Gate:
    def __init__(self, lease, upstream, listener, *, record, labels=None, connect_allow=None,
                 upstream_proxy=None, hold=0.0, rebind=None, hb_max=None, tick=0.05, timeout=15, notify=None,
                 self_read=False):
        self.lease, self.record_sink, self.timeout = lease, record, timeout
        self.notify = notify  # callable(message) to the launcher channel, or None
        self.upstream = upstream  # (host, port) of the fake backend
        self.upstream_proxy = upstream_proxy  # (host, port) captured once at startup, or None
        self.connect_allow = dict(connect_allow or {})  # 'host:port' -> (host, port) fixture target
        self.hold, self.rebind = hold, rebind
        # Variant (QX alternative 3): on a new same-account bearer the gate itself sends one usage
        # GET upstream with the held request's own Authorization and account headers.
        self.self_read = self_read
        self.hb_max = hb_max
        self.labels = dict(labels or {})
        self.lock = threading.RLock()  # admission lock: decision, first byte and registration
        self.streams = {}
        self.lost = None
        self.last_outcome = None  # the lease outcome of the latest forwarded usage read
        self.last_hb = None  # the heartbeat lease starts at the first heartbeat
        self.done = threading.Event()
        lease.on_close = self._on_pause
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler(), bind_and_activate=False)
        self.server.socket.close()
        self.server.socket = listener
        self.server.server_address = listener.getsockname()
        self.server.daemon_threads = True
        self.port = listener.getsockname()[1]

    # -- bookkeeping -------------------------------------------------------------------------
    def record(self, event, **data):
        self.record_sink({'at': time.monotonic(), 'event': event, **data})

    def label(self, bearer):
        if not bearer:
            return None
        digest = self.lease.digest(bearer)
        with self.lock:
            return self.labels.setdefault(digest, 'unlabelled-%d' % (len(self.labels) + 1))

    def add_label(self, bearer, name):
        with self.lock:
            self.labels[self.lease.digest(bearer)] = name

    def _close_streams(self, reason):
        # Called with the admission lock held (lease methods run under it): nothing can register.
        closed = []
        for key, sockets in list(self.streams.items()):
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            closed.append(key)
        self.record('streams_closed', reason=reason, count=len(closed), streams=sorted(closed))

    def _on_pause(self, reason):
        self._close_streams(reason)
        if self.notify is not None:
            self.notify({'event': 'paused', 'reason': reason, 'at': time.monotonic()})

    def lose(self, reason):
        with self.lock:
            if self.lost is not None:
                return
            self.lost = reason
            self.record('loss', reason=reason)
            already = self.lease.paused is not None
            self.lease.latch(reason)  # a first pause closes every registered stream itself
            if already:
                self._close_streams(reason)

    def heartbeat(self):
        with self.lock:
            self.last_hb = time.monotonic()

    def status(self):
        with self.lock:
            self.lease.tick()
            return {'paused': self.lease.paused, 'blocking': self.lease.blocking, 'lost': self.lost,
                    'epoch': self.lease.epoch, 'ready': self.lease.observation is not None
                    and self.lease.paused is None and self.lost is None and self.lease.ready(),
                    'streams': len(self.streams), 'last_outcome': self.last_outcome,
                    'heartbeat': self.last_hb is not None}

    def request_continuation(self):
        with self.lock:
            if self.lost is not None:
                self.record('continuation_refused', reason=self.lost)
                return False
            accepted = self.lease.request_continuation(trusted=True)
            self.record('continuation_requested', accepted=accepted, paused=self.lease.paused)
            return accepted

    def cancel_continuation(self):
        with self.lock:
            armed = self.lease.continuation is not None
            self.lease.cancel_continuation('launcher-reported-failure')
            self.record('continuation_cancelled', armed=armed, paused=self.lease.paused)
            return armed

    def tick_loop(self, interval):
        while not self.done.wait(interval):
            with self.lock:
                if self.lost is None and self.hb_max is not None and self.last_hb is not None \
                        and time.monotonic() - self.last_hb > self.hb_max:
                    self.lose('monitor-stale')
                self.lease.tick()

    def serve(self, tick=0.05):
        threading.Thread(target=self.tick_loop, args=(tick,), daemon=True).start()
        self.server.serve_forever(poll_interval=0.05)

    def shutdown(self):
        self.done.set()
        with self.lock:
            self._close_streams('gate-shutdown')
        self.server.shutdown()

    # -- upstream ------------------------------------------------------------------------------
    def read_with(self, headers):
        """One usage GET with the held request's own credential headers, fed to the lease."""
        started = self.lease.clock()
        bearer = (headers.get('Authorization') or '')[7:]
        account = headers.get('ChatGPT-Account-ID')
        try:
            connection = self.open_upstream()
            with self.lock:  # lock to first byte: a loss latched meanwhile sends nothing
                if self.lost is not None:
                    connection.close()
                    self.record('self_read_refused', reason=self.lost)
                    return None
                connection.request('GET', USAGE_PATHS[0], headers={'Authorization': headers.get('Authorization'),
                                                                   'ChatGPT-Account-ID': account,
                                                                   'Accept': 'application/json'})
            response = connection.getresponse()
            data = response.read()
            connection.close()
        except OSError as error:
            self.record('self_read_error', error=type(error).__name__)
            return None
        with self.lock:
            outcome = self.lease.observe_usage(bearer, account, response.status, data, started=started)
            self.last_outcome = outcome
            self.record('self_read', credential=self.label(bearer), status=response.status, lease=outcome,
                        epoch=self.lease.epoch, seconds=round(time.monotonic() - started, 6))
        return outcome

    def open_upstream(self):
        if self.upstream_proxy:
            connection = http.client.HTTPConnection(*self.upstream_proxy, timeout=self.timeout)
            connection.set_tunnel(*self.upstream)
        else:
            connection = http.client.HTTPConnection(*self.upstream, timeout=self.timeout)
        connection.connect()  # TCP (and proxy CONNECT) only: no request byte yet
        return connection

    def _handler(self):
        gate = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def info(self):
                value = self.headers.get('Authorization') or ''
                bearer = value[7:] if value.startswith('Bearer ') else None
                return bearer, {'method': self.command, 'path': self.path.split('?')[0],
                                'credential': gate.label(bearer),
                                'account': self.headers.get('ChatGPT-Account-ID'),
                                'upgrade': bool(self.headers.get('Upgrade'))}

            def refuse(self, reason, code=503):
                data = json.dumps({'error': {'type': 'server_error', 'code': 'quota_reserve_gate',
                                             'message': 'quota reserve gate refused: ' + reason}}).encode()
                try:
                    self.send_response(code)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def relay_head(self, response, length=None):
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() not in HOP:
                        self.send_header(name, value)
                if length is not None:
                    self.send_header('Content-Length', str(length))
                self.end_headers()

            def headers_out(self, body=None):
                headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP}
                if body is not None:
                    headers['Content-Length'] = str(len(body))
                return headers

            def foreign(self, bearer, info):
                account = info['account']
                if account is not None and account != gate.lease.expected_account:
                    with gate.lock:
                        gate.lease.observe_usage(bearer, account, 0, b'', started=gate.lease.clock())
                    gate.record('refused', **info, reason='identity-changed')
                    self.refuse('paused:identity-changed')
                    return True
                return False

            def do_CONNECT(self):
                target = self.path
                with gate.lock:
                    allowed = connect_allowed(target, gate.connect_allow, paused=gate.lease.paused,
                                              lost=gate.lost)
                    if not allowed:
                        gate.record('connect_refused', destination=target, paused=gate.lease.paused,
                                    lost=gate.lost)
                if not allowed:
                    return self.refuse('connect-refused', 403)
                try:
                    upstream = socket.create_connection(gate.connect_allow[target], timeout=gate.timeout)
                except OSError as error:
                    gate.record('connect_failed', destination=target, error=type(error).__name__)
                    return self.refuse('connect-failed', 502)
                key = 'connect-%d' % id(upstream)
                with gate.lock:
                    # Re-check under the lock before the tunnel carries any byte.
                    if not connect_allowed(target, gate.connect_allow, paused=gate.lease.paused,
                                           lost=gate.lost):
                        upstream.close()
                        gate.record('connect_refused', destination=target, late=True)
                        return self.refuse('connect-refused', 403)
                    gate.streams[key] = (self.connection, upstream)
                    gate.record('connect_tunnel', destination=target, stream=key)
                try:
                    self.send_response(200, 'Connection Established')
                    self.end_headers()
                    self.wfile.flush()
                    pair = {self.connection: upstream, upstream: self.connection}
                    open_ends = [self.connection, upstream]
                    while open_ends:
                        ready, _, _ = select.select(open_ends, [], [], 1.0)
                        for sock in ready:
                            data = sock.recv(65536)
                            if not data:
                                open_ends = []
                                break
                            pair[sock].sendall(data)
                except OSError:
                    pass
                finally:
                    with gate.lock:
                        gate.streams.pop(key, None)
                    upstream.close()
                    gate.record('connect_closed', destination=target, stream=key)

            def do_GET(self):
                started = gate.lease.clock()  # freshness and continuation order use receipt time
                bearer, info = self.info()
                with gate.lock:
                    action, reason = route('GET', info['path'], self.headers,
                                           paused=gate.lease.paused, lost=gate.lost)
                if action == 'refuse':
                    gate.record('refused', **info, reason=reason)
                    return self.refuse(reason)
                if self.foreign(bearer, info):
                    return
                try:
                    connection = gate.open_upstream()
                    with gate.lock:
                        # Lock to first byte: a pause or loss latched since the decision may change
                        # the route (a paused gate forwards only usage; a lost one nothing).
                        again, late = route('GET', info['path'], self.headers, paused=gate.lease.paused,
                                            lost=gate.lost)
                        if again == action:
                            connection.request('GET', self.path, headers=self.headers_out())
                    if again != action:
                        connection.close()
                        gate.record('refused', **info, reason=late or 'route-changed', late=True)
                        return self.refuse(late or 'route-changed')
                    response = connection.getresponse()
                    data = response.read()
                except OSError as error:
                    gate.record('upstream_error', **info, error=type(error).__name__)
                    return self.refuse('upstream-unavailable')
                outcome = None
                with gate.lock:
                    if action == 'usage':
                        outcome = gate.lease.observe_usage(bearer, info['account'], response.status, data,
                                                           started=started)
                        gate.last_outcome = outcome
                    gate.record('get', **info, status=response.status, lease=outcome,
                                epoch=gate.lease.epoch, paused=gate.lease.paused,
                                blocking=gate.lease.blocking, received=started,
                                seconds=round(time.monotonic() - started, 4))
                try:
                    self.relay_head(response, len(data))
                    self.wfile.write(data)
                except OSError:
                    pass
                connection.close()

            def do_POST(self):
                received = time.monotonic()
                bearer, info = self.info()
                body = self.rfile.read(int(self.headers.get('Content-Length', '0') or 0))
                with gate.lock:
                    action, reason = route('POST', info['path'], self.headers,
                                           paused=gate.lease.paused, lost=gate.lost)
                if action == 'refuse':
                    gate.record('refused', **info, reason=reason)
                    return self.refuse(reason)
                if self.foreign(bearer, info):
                    return
                try:
                    model = json.loads(body).get('model')
                except (ValueError, AttributeError):
                    model = None
                marker, kind = describe(body)
                info['request'] = kind
                key = 'model-%d' % id(self)

                def decide():
                    with gate.lock:
                        if gate.lost is not None:
                            return False, 'paused:' + gate.lost
                        return gate.lease.admit(bearer, info['account'], model)

                admitted, reason = decide()
                if reason == 'rebind-required' and gate.self_read:
                    began = time.monotonic()
                    gate.record('rebind_hold', **info, marker=marker, mode='gate-read')
                    gate.read_with(self.headers)
                    gate.record('rebind_hold_ended', credential=info['credential'], mode='gate-read',
                                seconds=round(time.monotonic() - began, 6))
                    admitted, reason = decide()
                elif reason == 'rebind-required' and gate.rebind is not None and gate.hold > 0:
                    digest = gate.lease.digest(bearer)
                    began = time.monotonic()
                    gate.record('rebind_hold', **info, marker=marker)
                    gate.rebind(info['credential'])
                    deadline = began + gate.hold
                    while time.monotonic() < deadline:
                        time.sleep(.02)
                        with gate.lock:
                            lease = gate.lease
                            if lease.paused or gate.lost or (lease.credential == digest
                                                             and lease.observation is not None):
                                break
                    gate.record('rebind_hold_ended', credential=info['credential'],
                                seconds=round(time.monotonic() - began, 6))
                    admitted, reason = decide()
                if not admitted:
                    gate.record('admission', **info, model=model, marker=marker, admitted=False, reason=reason,
                                epoch=gate.lease.epoch)
                    return self.refuse(reason)  # never 401: that would start native refresh
                try:
                    connection = gate.open_upstream()
                except OSError as error:
                    gate.record('upstream_error', **info, error=type(error).__name__)
                    return self.refuse('upstream-unavailable')
                with gate.lock:
                    # Lock to first byte: re-check the lease, then send and register atomically.
                    admitted, reason = decide()
                    if admitted:
                        connection.request('POST', self.path, body=body, headers=self.headers_out(body))
                        gate.streams[key] = (self.connection, connection.sock)
                    gate.record('admission', **info, model=model, marker=marker, admitted=admitted,
                                reason=reason, epoch=gate.lease.epoch, stream=key if admitted else None,
                                received=received, sent=time.monotonic())
                if not admitted:
                    connection.close()
                    return self.refuse(reason)
                try:
                    response = connection.getresponse()
                    with gate.lock:
                        if response.status == 200:
                            gate.lease.observe_headers(bearer, info['account'], dict(response.getheaders()))
                        gate.record('model_upstream', status=response.status, credential=info['credential'],
                                    marker=marker, stream=key)
                    self.relay_head(response)
                    while True:
                        chunk = response.read1(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    gate.record('stream_completed', stream=key, marker=marker)
                except (OSError, http.client.HTTPException) as error:
                    gate.record('stream_ended', stream=key, marker=marker, error=type(error).__name__)
                finally:
                    with gate.lock:
                        gate.streams.pop(key, None)
                    connection.close()

        return Handler


class Channel:
    """JSON lines over an inherited socket; the reader thread calls `handle` and `eof`."""

    def __init__(self, fd, handle, eof):
        self.sock = socket.socket(fileno=fd)
        self.lock = threading.Lock()
        self.handle, self.eof = handle, eof

    def send(self, message):
        data = (json.dumps(message) + '\n').encode()
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def loop(self):
        buffer = b''
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError:
                data = b''
            if not data:
                self.eof()
                return
            buffer += data
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                try:
                    self.handle(json.loads(line))
                except ValueError:
                    pass


def main(config_path):
    config = json.loads(Path(config_path).read_text())
    log = open(config['events'], 'a', buffering=1)
    log_lock = threading.Lock()

    def record(row):
        with log_lock:
            log.write(json.dumps(row, sort_keys=True) + '\n')

    policy = binding.Policy(config['five_hour'], config['weekly'], config['max_age'])
    lease = KLease(expected_account=config['account'], expected_user=config['user'], policy=policy,
                   clock=time.monotonic, wall=time.time, models=set(config['models']) if config.get('models') else None,
                   continuation_window=config.get('continuation_window', 30.0))
    listener = socket.socket(fileno=config['listen_fd'])
    launcher = None

    def rebind(label):
        launcher.send({'event': 'rebind', 'credential': label})

    gate = Gate(lease, tuple(config['upstream']), listener, record=record,
                connect_allow={key: tuple(value) for key, value in (config.get('connect_allow') or {}).items()},
                upstream_proxy=tuple(config['upstream_proxy']) if config.get('upstream_proxy') else None,
                hold=config.get('hold', 0.0), rebind=rebind if config.get('hold') else None,
                hb_max=config.get('hb_max'), notify=lambda message: launcher.send(message),
                self_read=bool(config.get('self_read')))

    def on_launcher(message):
        command = message.get('cmd')
        if command == 'label':
            gate.add_label(message['bearer'], message['label'])
            launcher.send({'reply': 'label', 'id': message.get('id')})
        elif command == 'continue':
            launcher.send({'reply': 'continue', 'id': message.get('id'), 'accepted': gate.request_continuation()})
        elif command == 'status':
            launcher.send({'reply': 'status', 'id': message.get('id'), **gate.status()})
        elif command == 'disarm':
            launcher.send({'reply': 'disarm', 'id': message.get('id'), 'armed': gate.cancel_continuation()})
        elif command == 'shutdown':
            gate.record('shutdown_requested')
            threading.Thread(target=gate.shutdown, daemon=True).start()

    def on_monitor(message):
        if 'hb' in message:
            gate.heartbeat()

    launcher = Channel(config['launcher_fd'], on_launcher, lambda: gate.lose('launcher-eof'))
    threading.Thread(target=launcher.loop, daemon=True).start()
    if config.get('monitor_fd') is not None:
        monitor = Channel(config['monitor_fd'], on_monitor, lambda: gate.lose('monitor-eof'))
        threading.Thread(target=monitor.loop, daemon=True).start()
    if config.get('restart'):
        with gate.lock:
            lease.latch('gate-restarted')
    gate.record('gate_started', pid=os.getpid(), restart=bool(config.get('restart')),
                connect_allow=sorted(gate.connect_allow), upstream_proxy=bool(gate.upstream_proxy),
                hold=gate.hold, hb_max=gate.hb_max, policy=[config['five_hour'], config['weekly'], config['max_age']])
    launcher.send({'event': 'ready', 'port': gate.port, 'pid': os.getpid()})
    try:
        gate.serve()
    finally:
        record({'at': time.monotonic(), 'event': 'gate_stopped'})
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
