"""KM composed gate: the only exit of one protected Claude or Codex invocation on macOS.

Research code only, not a product gateway. It runs outside the Seatbelt boundary as its own
process and composes the reviewed F1 contracts, imported read-only by path:
- Claude: QC's AdmissionController (same-bearer usage plus profile identity, memory-only
  bearer read from the account's credential file, rotation hold, latch, one-use continuation).
- Codex: QX's Lease (gate-observed binding of bearer and account to a forwarded usage read),
  with hold-and-rebind by a gate read: on a new same-account bearer the gate sends one usage GET
  upstream with the held request's own bearer before any model byte.
K adds: an inherited listening socket set owned by the launcher (port holder; a restarted gate
serves the same port and starts paused), a monitor heartbeat whose EOF or staleness latches this
gate instance for good, a control channel to the trusted launcher (status, continuation, latch
notifications), an independent timer for lease age, and an exact-host CONNECT allowlist for the
native refresh hosts while admission is open.

Invariant: every byte sent upstream (model request or tunnel payload) is written while holding
the admission lock and only for a stream still registered under open admission; a latch takes
that lock, closes every registered client and upstream socket, and so leaves no later byte.

python3 -I -B gate.py CONFIG.json --listen-fds A,B --monitor-fd N --control-fd N
"""
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(os.path.realpath(__file__)).parent
BODY_LIMIT = 8 * 1024 * 1024
TICK = 0.05
HOP = frozenset(('host', 'connection', 'content-length', 'keep-alive', 'proxy-authorization',
                 'proxy-connection', 'te', 'trailer', 'transfer-encoding', 'upgrade', 'accept-encoding'))
CODEX_USAGE = ('/backend-api/wham/usage', '/api/codex/usage')
CODEX_MODEL = ('/backend-api/codex/responses',)
CODEX_READS = CODEX_USAGE + ('/backend-api/codex/models', '/backend-api/wham/accounts/check',
                             '/backend-api/wham/settings/user', '/backend-api/wham/rate-limit-reset-credits',
                             '/api/codex/settings/user', '/api/codex/rate-limit-reset-credits')
CLAUDE_MODEL = ('/v1/messages',)
LOSS = ('monitor_eof', 'monitor_stale', 'launcher_eof')
MARKER = re.compile(r'KM-[A-Za-z0-9_-]+')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def route(provider, method, path):
    """Default-deny route table: 'model', 'usage', 'read', 'connect' or None (refused)."""
    if method == 'CONNECT':
        return 'connect'
    if provider == 'claude':
        return 'model' if method == 'POST' and path in CLAUDE_MODEL else None
    if method == 'POST' and path in CODEX_MODEL:
        return 'model'
    if method == 'GET' and path in CODEX_USAGE:
        return 'usage'
    if method == 'GET' and path in CODEX_READS:
        return 'read'
    return None


def connect_allowed(target, allow, admission_open, lost):
    """Exact host:port only, only while admission is open and the monitor is alive."""
    return target in allow and admission_open and not lost


def shut(sock):
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def read_response(sock, limit=1 << 20):
    """Read a close-delimited HTTP response; return (status, body bytes)."""
    data = b''
    while len(data) <= limit:
        chunk = sock.recv(65536)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b'\r\n\r\n')
    try:
        status = int(head.split(b' ', 2)[1])
    except (IndexError, ValueError):
        status = 0
    return status, body


class Emitter:
    def __init__(self, stream):
        self.stream, self.lock = stream, threading.Lock()

    def __call__(self, event, **fields):
        line = json.dumps({'event': event, 't': time.monotonic(), **fields}, sort_keys=True, default=str)
        with self.lock:
            try:
                self.stream.write(line + '\n')
                self.stream.flush()
            except (OSError, ValueError):
                pass


class Labels:
    """Fixture labels for evidence; matched by constant-time comparison, values never logged."""

    def __init__(self, mapping):
        self.items = [(label, token.encode()) for label, token in (mapping or {}).items()]

    def __call__(self, token):
        if not token:
            return None
        found = 'unlabelled'
        for label, value in self.items:
            if hmac.compare_digest(value, token.encode()):
                found = label
        return found


class ClaudeAdapter:
    """QC admission contract plus K's latch, tick, restart and CONNECT rules."""

    def __init__(self, config, emit, notify):
        qc = _load('km_qc_admission', HERE.parent / 'qc' / 'admission.py')
        self.qc, self.bearer_mod, self.usage_mod = qc, qc.bearer_mod, qc.usage_mod
        self.config, self.emit = config, emit
        usage = self.usage_mod.Endpoint('http', '127.0.0.1', int(config['usage_port']))
        profile = self.usage_mod.Endpoint('http', '127.0.0.1', int(config['usage_port']), self.usage_mod.PROFILE_PATH)

        def timed(name, call):
            def run(observed):
                began = time.monotonic()
                result = call(observed)
                ok = (result is not None) if name == 'profile' else (
                    result.outcome == 'response' and result.http_status == 200)
                emit('fetch', what=name, ms=round((time.monotonic() - began) * 1000, 3), ok=ok)
                return result
            return run

        quota = timed('usage', lambda b: self.usage_mod.fetch(b, usage))
        prof = timed('profile', lambda b: self.usage_mod.fetch_profile_identity(b, profile))
        identity = qc.BearerProfileIdentity(lambda: qc.read_local_identity(config['config_dir']), prof)
        p = config['policy']
        policy = qc.Policy(p['reserves'], max_age_s=p['max_age_s'],
                           revalidations_per_minute=p.get('revalidations_per_minute', 4),
                           allowed_models=tuple(p['allowed_models']))
        adapter = self

        class Events(list):
            def append(self, item):
                super().append(item)
                emit('controller', **item)
                if item.get('kind') == 'latched':
                    notify('latched', reason=item.get('reason'), state=item.get('state'))
                elif item.get('kind') == 'lease' and item.get('cause') == 'manual_continuation':
                    notify('resumed')

        self.controller = qc.AdmissionController(tuple(config['registered']), policy, quota, identity,
                                                 events=Events())
        self.lock = self.controller.lock
        del adapter

    def store_bearer(self):
        d = self.config['config_dir']
        return self.bearer_mod.read_linux_bearer(d, {'CLAUDE_CONFIG_DIR': d})

    def startup(self):
        try:
            observed = self.store_bearer()
        except self.bearer_mod.BearerRefused as error:
            return False, error.reason
        decision = self.controller.startup(observed)
        if decision.ok and self.controller.lease.unproved_rows:
            with self.lock:
                self.controller.state, self.controller.lease = 'checking', None
                self.controller._pause('quota_row_unproved')
            return False, 'quota_row_unproved'
        return decision.ok, decision.reason

    def restart_paused(self):
        c = self.controller
        with self.lock:
            c.state, c.reason, c.lease = 'paused', 'gate_restart', None
            c.paused_monotonic = c.clock.monotonic()
            c._continuations.clear()
            c._event('latched', state='paused', reason='gate_restart', closed=0)

    def is_open(self):
        c = self.controller
        return c.state == 'open' and c.lease is not None and c.clock.monotonic() < c.lease.deadline

    def latch(self, reason):
        with self.lock:
            if self.controller.state in ('open', 'checking'):
                self.controller._pause(reason)
            else:
                self.controller.reason = reason

    def tick(self):
        c = self.controller
        with self.lock:
            if c.state == 'open' and c.clock.monotonic() >= c.lease.deadline:
                c._pause('freshness_expired')

    def admit(self, headers, body, close):
        try:
            model = json.loads(body).get('model')
        except (ValueError, AttributeError):
            model = None
        decision = self.controller.admit(headers, model, close)
        return decision.ok, decision.reason, decision.stream_id

    def live(self, stream):
        return self.controller.state == 'open' and stream in self.controller.streams

    def register(self, close):
        return self.controller._register(close)

    def release(self, stream):
        self.controller.release(stream)

    def poll(self):
        return self.controller.monitor().reason

    def continue_check(self):
        token = self.controller.issue_continuation()
        try:
            observed = self.store_bearer()
        except self.bearer_mod.BearerRefused as error:
            return {'ok': False, 'reason': error.reason}
        decision = self.controller.continue_manually(token, observed)
        return {'ok': decision.ok, 'reason': decision.reason}

    def status(self):
        c = self.controller
        with self.lock:
            return {'state': c.state, 'reason': c.reason, 'epoch': c.lease.epoch if c.lease else None,
                    'streams': len(c.streams), 'ready': c.state == 'open'}


class CodexAdapter:
    """QX lease plus gate-read rebind, K's latch, tick, restart and CONNECT rules."""

    def __init__(self, config, emit, notify, gate):
        qx = _load('km_qx_binding', HERE.parent / 'qx' / 'binding.py')
        self.qx, self.config, self.emit, self.notify, self.gate = qx, config, emit, notify, gate
        p = config['policy']
        policy = qx.Policy(five_hour=p['reserves'].get('five_hour'), weekly=p['reserves'].get('weekly'),
                           max_age=p['max_age_s'])
        self.lease = qx.Lease(expected_account=config['expected_account'], expected_user=config.get('expected_user'),
                              policy=policy, clock=time.monotonic, wall=lambda: int(time.time()),
                              on_close=self._on_close)
        self.lock = threading.RLock()
        self.streams, self.ids = {}, iter(range(1, 1 << 30))
        self.poll_bearer = None  # memory-only: the bearer of the newest passing read
        self.reads = []          # recent rebind read times (budget)
        self.budget = int(p.get('revalidations_per_minute', 4))

    def _on_close(self, reason):
        closers, self.streams = list(self.streams.values()), {}
        self.emit('controller', kind='latched', reason=reason, closed=len(closers))
        self.notify('latched', reason=reason)
        for close in closers:
            close()

    def startup(self):
        return True, 'awaiting_read'  # the launcher's in-boundary reader binds the first epoch

    def restart_paused(self):
        with self.lock:
            self.lease._pause('gate_restart')

    def is_open(self):
        return self.lease.paused is None and self.lease.observation is not None and \
            not self.lease.tick()

    def latch(self, reason):
        with self.lock:
            self.lease._pause(reason)

    def tick(self):
        with self.lock:
            self.lease.tick()

    def gate_read(self, bearer, account, cause):
        """One model-free usage GET upstream with this exact bearer; feeds the lease."""
        began = time.monotonic()
        try:
            sock = self.gate.open_upstream(self.gate.upstream)
            sock.sendall(('GET /backend-api/wham/usage HTTP/1.1\r\nHost: upstream.fixture\r\n'
                          'Authorization: Bearer %s\r\nChatGPT-Account-ID: %s\r\nConnection: close\r\n\r\n'
                          % (bearer, account)).encode())
            sock.settimeout(5)
            status, body = read_response(sock)
            sock.close()
        except OSError:
            status, body = 0, b''
        outcome = self.observe_forwarded(bearer, account, status, body, began)
        self.emit('fetch', what='usage', cause=cause, ms=round((time.monotonic() - began) * 1000, 3),
                  ok=status == 200, outcome=outcome, label=self.gate.labels(bearer))
        return outcome

    def admit(self, headers, body, close):
        auth = [v for k, v in headers if k.lower() == 'authorization']
        bearer = auth[0][7:] if len(auth) == 1 and auth[0].startswith('Bearer ') else None
        account = next((v for k, v in headers if k.lower() == 'chatgpt-account-id'), None)
        try:
            model = json.loads(body).get('model')
        except (ValueError, AttributeError):
            model = None
        if bearer is None:
            self.latch('auth_selector_conflict')
            return False, 'auth_selector_conflict', None
        with self.lock:
            ok, reason = self.lease.admit(bearer, account, model)
        if reason == 'rebind-required':
            now = time.monotonic()
            self.reads = [t for t in self.reads if now - t < 60]
            if len(self.reads) >= self.budget:
                self.latch('revalidation_budget')
                return False, 'revalidation_budget', None
            self.reads.append(now)
            self.emit('controller', kind='rotation_hold', label=self.gate.labels(bearer))
            self.gate_read(bearer, account, 'rebind')
            with self.lock:
                ok, reason = self.lease.admit(bearer, account, model)
        with self.lock:
            if not ok or self.gate.lost:
                self.emit('controller', kind='refused', reason=reason)
                return False, reason if not self.gate.lost else self.gate.lost, None
            stream = next(self.ids)
            self.streams[stream] = close
            self.emit('controller', kind='admitted', epoch=self.lease.epoch, stream=stream)
            return True, 'admitted', stream

    def live(self, stream):
        return self.lease.paused is None and stream in self.streams

    def register(self, close):
        stream = next(self.ids)
        self.streams[stream] = close
        return stream

    def release(self, stream):
        with self.lock:
            self.streams.pop(stream, None)

    def observe_forwarded(self, bearer, account, status, body, started):
        with self.lock:
            before = self.lease.paused
            outcome = self.lease.observe_usage(bearer, account, status, body, started=started)
            if outcome == 'fresh' and self.lease.paused is None:
                self.poll_bearer = bearer
                if before is not None:
                    self.emit('controller', kind='lease', cause='manual_continuation', epoch=self.lease.epoch)
                    self.notify('resumed')
            return outcome

    def poll(self):
        bearer = self.poll_bearer
        if bearer is None:
            return 'no_bearer'
        return self.gate_read(bearer, self.config['expected_account'], 'poll')

    def continue_check(self):
        with self.lock:
            armed = self.lease.request_continuation(True)
        return {'ok': armed, 'reason': 'armed' if armed else 'not_paused', 'needs_read': armed}

    def status(self):
        with self.lock:
            lease = self.lease
            return {'state': 'paused' if lease.paused else ('open' if lease.observation else 'checking'),
                    'reason': lease.reason() if lease.paused else None, 'paused': lease.paused,
                    'epoch': lease.epoch, 'streams': len(self.streams), 'ready': lease.ready()}


class Gate:
    def __init__(self, config, emit):
        self.config, self.emit = config, emit
        self.provider = config['provider']
        self.upstream = ('127.0.0.1', int(config['upstream_port']))
        self.allow = {t: ('127.0.0.1', int(p)) for t, p in (config.get('connect_allow') or {}).items()}
        proxy = config.get('upstream_proxy')
        self.proxy = (proxy[0], int(proxy[1])) if proxy else None
        self.labels = Labels(config.get('labels'))
        self.lost = None
        self.control = None
        self.control_lock = threading.Lock()
        self.last_beat = time.monotonic()
        self.beat_max = float(config.get('heartbeat_max_age_s', 3.0))
        self.polling = threading.Lock()
        if self.provider == 'claude':
            self.adapter = ClaudeAdapter(config, emit, self.notify)
        else:
            self.adapter = CodexAdapter(config, emit, self.notify, self)

    def notify(self, op, **fields):
        message = dict(op=op, **fields)
        self.emit('notify', **message)
        with self.control_lock:
            if self.control is None:
                return
            try:
                self.control.sendall((json.dumps(message) + '\n').encode())
            except OSError:
                pass

    def lose(self, reason):
        if self.lost is None:
            self.lost = reason
            self.emit('lost', reason=reason)
        self.adapter.latch(reason)

    def open_upstream(self, address):
        """Wrapper-originated connection, through the launcher's proxy captured at startup if any."""
        if self.proxy is None:
            return socket.create_connection(address, timeout=5)
        sock = socket.create_connection(self.proxy, timeout=5)
        sock.sendall(('CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n' % (address * 2)).encode())
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = sock.recv(1)
            if not chunk or len(head) > 4096:
                raise OSError('proxy refused')
            head += chunk
        if head.split(b' ', 2)[1:2] != [b'200']:
            raise OSError('proxy refused')
        return sock

    def heartbeat(self):
        self.last_beat = time.monotonic()
        if self.lost is None and self.polling.acquire(blocking=False):
            def run():
                try:
                    began = time.monotonic()
                    reason = self.adapter.poll()
                    self.emit('poll', reason=reason, ms=round((time.monotonic() - began) * 1000, 3))
                finally:
                    self.polling.release()
            threading.Thread(target=run, daemon=True).start()

    def tick_loop(self):
        while True:
            time.sleep(TICK)
            self.adapter.tick()
            if self.lost is None and time.monotonic() - self.last_beat > self.beat_max:
                self.lose('monitor_stale')

    def status(self):
        row = self.adapter.status()
        row['lost'] = self.lost
        return row


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
        gate = self.server.gate
        message = ('coding-agent-kit quota gate: paused or refused (%s); continue only from the trusted '
                   'terminal after a fresh check' % reason)
        if gate.provider == 'claude':
            return self._reply(403, {'type': 'error', 'error': {'type': 'permission_error', 'message': message}})
        return self._reply(503, {'error': {'type': 'server_error', 'code': 'quota_reserve_gate',
                                           'message': message}})

    def _dispatch(self):
        gate = self.server.gate
        path = self.path.split('?')[0]
        kind = route(gate.provider, self.command, path)
        if self.headers.get('Upgrade'):
            gate.emit('route_refused', method=self.command, path=path[:64], why='upgrade')
            return self._refuse('websocket_refused')
        if kind is None:
            gate.emit('route_refused', method=self.command, path=path[:64])
            return self._reply(404, {'type': 'error', 'error': {'type': 'not_found_error'}})
        if kind == 'model':
            return self._model(path)
        return self._read(path, kind)

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_CONNECT = _dispatch

    def _body(self):
        if self.headers.get('Transfer-Encoding'):
            return None
        length = self.headers.get('Content-Length', '0') or '0'
        if not length.isdigit() or int(length) > BODY_LIMIT:
            return None
        return self.rfile.read(int(length))

    def _head(self, path, body):
        head = ['%s %s HTTP/1.1' % (self.command, self.path), 'Host: upstream.fixture', 'Connection: close',
                'Content-Length: %d' % len(body)]
        head += ['%s: %s' % (k, v) for k, v in self.headers.items() if k.lower() not in HOP]
        return ('\r\n'.join(head) + '\r\n\r\n').encode('latin-1') + body

    def _model(self, path):
        gate, adapter = self.server.gate, self.server.gate.adapter
        began = time.monotonic()
        body = self._body()
        if body is None:
            return self._reply(413, {'type': 'error', 'error': {'type': 'request_too_large'}})
        holder = {'client': self.connection, 'upstream': None}

        def close():
            shut(holder['client'])
            shut(holder['upstream'])

        if gate.lost:
            gate.emit('decision', ok=False, reason=gate.lost, path=path)
            return self._refuse(gate.lost)
        auth = self.headers.get('Authorization') or ''
        label = gate.labels(auth[7:] if auth.startswith('Bearer ') else None)
        ok, reason, stream = adapter.admit(list(self.headers.items()), body, close)
        markers = MARKER.findall(body[-200000:].decode('utf-8', 'replace'))  # evidence only
        gate.emit('decision', ok=ok, reason=reason, stream=stream, label=label, path=path,
                  marker=markers[-1] if markers else None, child=b'KM sdk child' in body,
                  admit_ms=round((time.monotonic() - began) * 1000, 3))
        if not ok:
            return self._refuse(reason)
        upstream = None
        try:
            upstream = gate.open_upstream(gate.upstream)
            holder['upstream'] = upstream
            with adapter.lock:
                if gate.lost or not adapter.live(stream):
                    gate.emit('aborted_before_upstream', stream=stream)
                    return self._refuse(gate.lost or 'closed')
                upstream.sendall(self._head(path, body))
                gate.emit('forwarded', stream=stream, label=label, bytes=len(body),
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
            adapter.release(stream)
            if upstream is not None:
                shut(upstream)
                upstream.close()
            gate.emit('stream_closed', stream=stream)
            self.close_connection = True

    def _read(self, path, kind):
        gate = self.server.gate
        if kind == 'connect':
            return self._connect()
        adapter = gate.adapter
        started = time.monotonic()
        account = self.headers.get('ChatGPT-Account-ID')
        auth = self.headers.get('Authorization') or ''
        bearer = auth[7:] if auth.startswith('Bearer ') else None
        paused = adapter.lease.paused is not None
        allowed = (gate.lost is None and account == gate.config['expected_account'] and bearer
                   and (kind == 'usage' or not paused))
        if not allowed:
            gate.emit('read_refused', path=path, kind=kind, lost=gate.lost, paused=paused)
            return self._refuse(gate.lost or ('paused' if paused else 'foreign_account'))
        try:
            upstream = gate.open_upstream(gate.upstream)
            upstream.sendall(self._head(path, b''))
            upstream.settimeout(5)
            data = b''
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                data += chunk
            upstream.close()
        except OSError:
            return self._refuse('upstream_unavailable')
        outcome = None
        if kind == 'usage':
            head, _, payload = data.partition(b'\r\n\r\n')
            try:
                status = int(head.split(b' ', 2)[1])
            except (IndexError, ValueError):
                status = 0
            outcome = adapter.observe_forwarded(bearer, account, status, payload, started)
        gate.emit('read', path=path, kind=kind, outcome=outcome, label=gate.labels(bearer))
        try:
            self.connection.sendall(data)
        except OSError:
            pass
        self.close_connection = True

    def _connect(self):
        gate, adapter = self.server.gate, self.server.gate.adapter
        target = self.path
        holder = {'client': self.connection, 'upstream': None}

        def close():
            shut(holder['client'])
            shut(holder['upstream'])

        with adapter.lock:
            ok = connect_allowed(target, gate.allow, adapter.is_open(), gate.lost)
            stream = adapter.register(close) if ok else None
        gate.emit('connect', target=target[:96], ok=ok, stream=stream)
        if not ok:
            try:
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.send_header('Connection', 'close')
                self.end_headers()
            except OSError:
                pass
            self.close_connection = True
            return
        sent = {'up': 0, 'down': 0}
        upstream = None
        try:
            upstream = socket.create_connection(gate.allow[target], timeout=5)
            holder['upstream'] = upstream
            self.send_response(200, 'Connection established')
            self.end_headers()
            self.wfile.flush()

            def pump():
                try:
                    while True:
                        chunk = self.rfile.read1(65536)
                        if not chunk:
                            break
                        with adapter.lock:
                            if gate.lost or not adapter.live(stream):
                                break
                            upstream.sendall(chunk)
                            sent['up'] += len(chunk)
                except (OSError, ValueError):
                    pass
                shut(upstream)
            thread = threading.Thread(target=pump, daemon=True)
            thread.start()
            upstream.settimeout(None)
            while True:
                chunk = upstream.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
                sent['down'] += len(chunk)
            thread.join(5)
        except OSError:
            pass
        finally:
            adapter.release(stream)
            if upstream is not None:
                shut(upstream)
                upstream.close()
            gate.emit('tunnel_closed', stream=stream, bytes_up=sent['up'], bytes_down=sent['down'])
            self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, listener, gate):
        family = listener.family
        self.address_family = family
        super().__init__(('127.0.0.1', 0) if family == socket.AF_INET else ('::1', 0), Handler,
                         bind_and_activate=False)
        self.socket.close()
        self.socket = listener
        self.server_name, self.server_port = 'gate', listener.getsockname()[1]
        self.gate = gate


def channel(sock, on_line, on_eof):
    buffer = b''
    while True:
        try:
            data = sock.recv(4096)
        except OSError:
            data = b''
        if not data:
            on_eof()
            return
        buffer += data
        while b'\n' in buffer:
            line, buffer = buffer.split(b'\n', 1)
            on_line(line)


def main(argv):
    config = json.loads(Path(argv[0]).read_text())
    options = dict(zip(argv[1::2], argv[2::2]))
    emit = Emitter(sys.stdout)
    listeners = [socket.socket(fileno=int(fd)) for fd in options['--listen-fds'].split(',')]
    monitor = socket.socket(fileno=int(options['--monitor-fd']))
    control = socket.socket(fileno=int(options['--control-fd']))
    gate = Gate(config, emit)
    gate.control = control
    began = time.monotonic()
    if config.get('mode', 'startup') == 'restart':
        gate.adapter.restart_paused()
    else:
        ok, reason = gate.adapter.startup()
        if not ok:
            emit('startup_refused', reason=reason)
            return 3
        emit('preflight', ms=round((time.monotonic() - began) * 1000, 3), reason=reason)

    def on_control(line):
        try:
            op = json.loads(line).get('op')
        except (ValueError, AttributeError):
            op = None
        if op == 'continue':
            if gate.lost:
                reply = {'ok': False, 'reason': gate.lost}
            else:
                reply = gate.adapter.continue_check()
        elif op == 'status':
            reply = gate.status()
        else:
            reply = {'ok': False, 'reason': 'unknown_op'}
        reply['op'] = 'reply:' + str(op)
        emit('control', **reply)
        with gate.control_lock:
            try:
                control.sendall((json.dumps(reply) + '\n').encode())
            except OSError:
                pass

    def on_beat(line):
        if line == b'poll':
            gate.heartbeat()

    servers = [Server(listener, gate) for listener in listeners]
    threading.Thread(target=gate.tick_loop, daemon=True).start()
    threading.Thread(target=channel, args=(monitor, on_beat, lambda: gate.lose('monitor_eof')), daemon=True).start()
    threading.Thread(target=channel, args=(control, on_control, lambda: gate.lose('launcher_eof')),
                     daemon=True).start()
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True).start()
    emit('gate_ready', port=servers[0].server_port, pid=os.getpid(), mode=config.get('mode', 'startup'),
         listeners=len(servers))
    servers[0].serve_forever(poll_interval=0.1)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
