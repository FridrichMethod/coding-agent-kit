"""LX loopback gate for one reviewed live Codex usage read (research probe, not product code).

Based on qx/gate.py. One 127.0.0.1 listener serves both chatgpt_base_url and the
forced model provider of one owned `codex app-server`.

- It forwards only `GET /backend-api/wham/usage` (no query, no body), and only
  while the controller has armed it for one account/rateLimits/read: at most one
  forward per arm and MAX_FORWARDS in total. A forward is a new verified TLS
  connection to the backend host (production: the system trust store and a
  hostname check), carrying only the allowlisted request headers. It never
  follows or relays a redirect, never falls back to plain HTTP or unverified TLS
  (this module has no plain-HTTP upstream at all) and is bounded in time and size.
- Every other path and method, every model request and every unarmed read is
  refused locally with 503 before any upstream byte. Never 401: that would start
  native refresh.
- CONNECT (native's proxy route) is tunnelled only to TUNNEL_HOSTS, the refresh
  host the live approval allows, as an opaque byte relay counted in bytes; every
  other destination is refused and recorded by host:port.
- Account scope: before any upstream byte, a usage read is refused locally unless
  the in-transit bearer's account claim is present and equals the codex-as owner's
  account (from the owner auth.json, read at preflight), and any ChatGPT-Account-ID
  header equals it too. The refusal reason is recorded; the read's arm is cleared.
- The gate decodes the in-transit bearer's JWT claims in memory only, to enforce the
  account scope and compute identity booleans. It records no token, id, raw body or
  header value.

Only live_codex.main builds production_upstream() and production_tunnel. Tests
pass a loopback Upstream whose context trusts a temporary CA (the test-only
context hook) and a loopback or refusing tunnel connector.
"""
import base64
from collections import Counter
import gzip
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import math
import os
import re
import selectors
import socket
import ssl
import threading
import time

USAGE_PATH = '/backend-api/wham/usage'
PRODUCTION_HOST = 'chatgpt.com'
MAX_FORWARDS = 2
CONNECT_TIMEOUT = 5.0
TOTAL_TIMEOUT = 15.0
BODY_LIMIT = 64 * 1024
MAX_ADDRESSES = 2
# Request headers a forward may carry; native 0.167.2 sends exactly these on usage (QX section 1).
FORWARD_HEADERS = ('authorization', 'chatgpt-account-id', 'accept', 'user-agent')
HOP = {'connection', 'keep-alive', 'proxy-connection', 'proxy-authenticate', 'proxy-authorization', 'te',
       'trailer', 'transfer-encoding', 'upgrade', 'content-length'}
TUNNEL_HOSTS = ('auth.openai.com:443',)
MAX_TUNNELS = 3
TUNNEL_IDLE = 30.0
TUNNEL_TOTAL = 60.0
MAX_DISTINCT = 16
TARGET = re.compile(r'[A-Za-z0-9.-]{1,253}:[0-9]{1,5}')
PLAN = re.compile(r'[a-z0-9_]{1,48}')
# Fixed request categories; anything else is counted as "unlisted" (paths may carry ids).
CATEGORIES = (
    ('/backend-api/wham/usage', 'usage'),
    ('/backend-api/wham/accounts/check', 'accounts-check'),
    ('/backend-api/wham/rate-limit-reset-credits', 'reset-credits'),
    ('/backend-api/wham/settings/user', 'settings-user'),
    ('/backend-api/codex/responses', 'model-request'),
    ('/backend-api/codex/models', 'models'),
    ('/backend-api/codex/memories/', 'model-adjacent'),
    ('/backend-api/codex/alpha/', 'model-adjacent'),
    ('/backend-api/codex/images/', 'model-adjacent'),
    ('/backend-api/ps/plugins/', 'plugins'),
    ('/backend-api/plugins/', 'plugins'),
)
# Optional usage fields whose presence (never value) is recorded.
OPTIONAL_FIELDS = ('account_id', 'user_id', 'credits', 'spend_control', 'additional_rate_limits',
                   'rate_limit_reset_credits', 'rate_limit_upsell')
AUTH_CLAIMS = 'https://api.openai.com/auth'


class Upstream:
    """Where a forward goes: host (SNI, Host header and certificate name), port, a TLS context
    factory and a resolver. There is deliberately no plain-HTTP variant."""

    def __init__(self, host, port, context_factory, resolver=socket.getaddrinfo):
        self.host, self.port, self.context_factory, self.resolver = host, port, context_factory, resolver


def production_context():
    """The system trust store with hostname checking; refuses to return anything weaker."""
    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise RuntimeError('default TLS context does not verify')
    return context


def production_upstream():
    return Upstream(PRODUCTION_HOST, 443, production_context)


def resolve(host, port, timeout, resolver):
    """getaddrinfo bounded by timeout (it ignores socket timeouts), at most MAX_ADDRESSES."""
    box = {}

    def work():
        try:
            box['infos'] = list(resolver(host, port, 0, socket.SOCK_STREAM, socket.IPPROTO_TCP))
        except (OSError, UnicodeError, ValueError) as error:
            box['error'] = error

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(max(0.0, timeout))
    if worker.is_alive():
        raise TimeoutError('resolution deadline')
    if 'error' in box or not box.get('infos'):
        raise OSError('resolution failed')
    return box['infos'][:MAX_ADDRESSES]


def connect(host, port, timeout, resolver=socket.getaddrinfo):
    """A bounded TCP connection (resolution plus connect within timeout)."""
    deadline = time.monotonic() + timeout
    last = None
    for family, kind, proto, _name, address in resolve(host, port, timeout, resolver):
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('connect deadline')
        candidate = socket.socket(family, kind, proto)
        try:
            candidate.settimeout(left)
            candidate.connect(address)
            return candidate
        except OSError as error:
            candidate.close()
            last = error
    raise last or OSError('no address')


def production_tunnel(host, port, timeout):
    return connect(host, port, timeout)


def forward(upstream, path, headers, *, total=TOTAL_TIMEOUT, connect_timeout=CONNECT_TIMEOUT,
            limit=BODY_LIMIT, clock=time.monotonic):
    """One bounded GET over verified TLS. Returns a dict with outcome 'relayed' (status, headers,
    body, tls_version) or a failure outcome and the phase it failed in. No retry, no redirect."""
    start = clock()
    expired = threading.Event()
    guard = threading.Lock()
    tracked = []
    phase = 'resolve'
    status = None

    def remaining(cap):
        left = total - (clock() - start)
        if left <= 0 or expired.is_set():
            raise TimeoutError('total deadline')
        return min(cap, left)

    def expire():
        with guard:
            expired.set()
            items = list(tracked)
        for item in items:
            try:
                item.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def track(item):
        with guard:
            if expired.is_set():
                item.close()
                raise TimeoutError('total deadline')
            tracked.append(item)
        return item

    watchdog = threading.Timer(total, expire)
    watchdog.daemon = True
    watchdog.start()
    connection = None
    try:
        context = upstream.context_factory()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            return {'outcome': 'unverified_context_refused', 'phase': 'tls', 'status': None}
        sock = None
        last = None
        for family, kind, proto, _name, address in resolve(upstream.host, upstream.port, remaining(connect_timeout),
                                                           upstream.resolver):
            phase = 'connect'
            candidate = track(socket.socket(family, kind, proto))
            try:
                candidate.settimeout(remaining(connect_timeout))
                candidate.connect(address)
            except OSError as error:
                candidate.close()
                last = error
                continue
            sock = candidate
            break
        if sock is None:
            raise last or OSError('no address')
        phase = 'tls'
        sock = track(context.wrap_socket(sock, server_hostname=upstream.host, do_handshake_on_connect=False))
        sock.settimeout(remaining(connect_timeout))
        sock.do_handshake()
        tls_version = sock.version()
        phase = 'request'
        connection = http.client.HTTPSConnection(upstream.host, upstream.port, timeout=connect_timeout,
                                                 context=context)
        connection.sock = sock  # connect() is never called: no second resolution, no other route
        sock.settimeout(remaining(connect_timeout))
        request_headers = {'Host': upstream.host if upstream.port == 443 else '%s:%d' % (upstream.host, upstream.port),
                           'Accept-Encoding': 'identity', 'Connection': 'close'}
        request_headers.update(headers)
        connection.putrequest('GET', path, skip_host=True, skip_accept_encoding=True)
        for name, value in request_headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        phase = 'response'
        sock.settimeout(remaining(connect_timeout))
        response = connection.getresponse()
        status = response.status
        relayed = response.getheaders()
        if 300 <= status < 400:
            return {'outcome': 'redirect_refused', 'phase': phase, 'status': status, 'headers': relayed}
        declared = response.getheader('Content-Length')
        if declared is not None and (not declared.isdigit() or int(declared) > limit):
            return {'outcome': 'response_too_large', 'phase': phase, 'status': status, 'headers': relayed}
        chunks, size = [], 0
        while not response.isclosed():  # http.client closes the socket after the last byte
            sock.settimeout(remaining(connect_timeout))
            chunk = response.read1(min(16384, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                return {'outcome': 'response_too_large', 'phase': phase, 'status': status, 'headers': relayed}
        remaining(connect_timeout)
        return {'outcome': 'relayed', 'phase': 'done', 'status': status, 'headers': relayed,
                'body': b''.join(chunks), 'tls_version': tls_version}
    except ssl.SSLCertVerificationError:
        return {'outcome': 'tls_verification_failed', 'phase': phase, 'status': status}
    except ssl.SSLError:
        return {'outcome': 'deadline_exceeded' if expired.is_set() else 'tls_error', 'phase': phase,
                'status': status}
    except TimeoutError:
        return {'outcome': 'deadline_exceeded', 'phase': phase, 'status': status}
    except (OSError, http.client.HTTPException, ValueError):
        return {'outcome': 'deadline_exceeded' if expired.is_set() else 'transport_error', 'phase': phase,
                'status': status}
    finally:
        watchdog.cancel()
        if connection is not None:
            connection.close()
        with guard:
            items = list(tracked)
        for item in items:
            item.close()


def category(method, path):
    for prefix, name in CATEGORIES:
        if path == prefix or (prefix.endswith('/') and path.startswith(prefix)):
            return method + ' ' + name
    return method + ' unlisted'


def bearer_claims(bearer):
    """Claims of the in-transit JWT bearer (decoded, never verified) or None. Memory only."""
    try:
        part = bearer.split('.')[1]
        payload = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    except (IndexError, ValueError, UnicodeError, AttributeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    auth = payload.get(AUTH_CLAIMS)
    auth = auth if isinstance(auth, dict) else {}
    text = lambda value: value if isinstance(value, str) and value else None
    exp = payload.get('exp')
    return {'account': text(auth.get('chatgpt_account_id')),
            # native's rule (login/src/token_data.rs): chatgpt_user_id, else user_id
            'user': text(auth.get('chatgpt_user_id')) or text(auth.get('user_id')),
            'has_chatgpt_user_id': text(auth.get('chatgpt_user_id')) is not None,
            'exp': exp if isinstance(exp, int) and not isinstance(exp, bool) else None}


def _same(left, right):
    return (isinstance(left, str) and isinstance(right, str) and bool(left) and bool(right)
            and hmac.compare_digest(left.encode('utf-8', 'replace'), right.encode('utf-8', 'replace')))


def identity(data, claims, header_account, expected, wall):
    """Booleans only: presence of the response identity and its equality with the bearer claims,
    the request header and codex-as's expected (user, account) from the login record."""
    data = data if isinstance(data, dict) else {}
    account, user = data.get('account_id'), data.get('user_id')
    claims = claims or {}
    owner_user, owner_account = expected if expected else (None, None)
    present = lambda value: isinstance(value, str) and bool(value)
    exp = claims.get('exp')
    return {
        'bearer_claims_decoded': bool(claims),
        'bearer_has_account_claim': present(claims.get('account')),
        'bearer_has_chatgpt_user_id_claim': bool(claims.get('has_chatgpt_user_id')),
        'bearer_exp_in_future': None if exp is None else exp > wall,
        'bearer_exp_within_300s': None if exp is None else exp <= wall + 300,
        'request_account_header_present': present(header_account),
        'request_account_header_equals_bearer_account': _same(header_account, claims.get('account')),
        'request_account_header_equals_codex_as_account': _same(header_account, owner_account),
        'bearer_account_equals_codex_as_account': _same(claims.get('account'), owner_account),
        'bearer_user_equals_codex_as_user': _same(claims.get('user'), owner_user),
        'response_account_id_present': present(account),
        'response_user_id_present': present(user),
        'response_account_id_equals_bearer_account': _same(account, claims.get('account')),
        'response_user_id_equals_bearer_user': _same(user, claims.get('user')),
        'response_account_id_equals_request_header': _same(account, header_account),
        'response_account_id_equals_codex_as_account': _same(account, owner_account),
        'response_user_id_equals_codex_as_user': _same(user, owner_user),
    }


def _scalar(value):
    """Numbers and booleans pass; anything else is reduced to its JSON type name."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) or (isinstance(value, float) and math.isfinite(value)):
        return value
    return {'type': type(value).__name__}


def _window(raw):
    if raw is None or not isinstance(raw, dict):
        return _scalar(raw) if raw is None else {'type': type(raw).__name__}
    view = {key: _scalar(raw.get(key)) for key in ('used_percent', 'limit_window_seconds', 'reset_after_seconds',
                                                   'reset_at')}
    seconds = raw.get('limit_window_seconds')
    view['minutes'] = -(-seconds // 60) if isinstance(seconds, int) and not isinstance(seconds, bool) else None
    used = raw.get('used_percent')
    view['used_percent_is_integer'] = isinstance(used, int) and not isinstance(used, bool)
    return view


def _rate(raw):
    if raw is None or not isinstance(raw, dict):
        return None if raw is None else {'type': type(raw).__name__}
    return {'allowed': _scalar(raw.get('allowed')), 'limit_reached': _scalar(raw.get('limit_reached')),
            'primary_window': _window(raw.get('primary_window')),
            'secondary_window': _window(raw.get('secondary_window'))}


def plan(value):
    if value is None:
        return None
    return value if isinstance(value, str) and PLAN.fullmatch(value) else '<unrecognized>'


def normalize_usage(data):
    """Approved fields only: plan, window durations, percentages and reset times, plus the
    presence (never the value or name) of optional fields and additional rows."""
    if not isinstance(data, dict):
        return {'object': False}
    view = {'object': True, 'plan_type': plan(data.get('plan_type')), 'rate_limit': _rate(data.get('rate_limit')),
            'present': {key: data.get(key) is not None for key in OPTIONAL_FIELDS}}
    rows = data.get('additional_rate_limits')
    if isinstance(rows, list):
        view['additional_rate_limits_count'] = len(rows)
        view['additional_rate_limits'] = [
            {'rate_limit': _rate(row.get('rate_limit')),
             'limit_name_present': isinstance(row.get('limit_name'), str),
             'metered_feature_present': isinstance(row.get('metered_feature'), str),
             'normal_model_slug_present': isinstance(row.get('normal_model_slug'), str)}
            if isinstance(row, dict) else {'type': type(row).__name__} for row in rows[:8]]
    return view


def decode_body(body, headers):
    """Parsed JSON of a relayed body (gzip tolerated), or (None, reason)."""
    encoding = next((value for name, value in headers if name.lower() == 'content-encoding'), 'identity')
    try:
        if encoding.strip().lower() == 'gzip':
            body = gzip.decompress(body)
            if len(body) > 4 * BODY_LIMIT:
                return None, 'decoded-too-large'
        elif encoding.strip().lower() not in ('identity', ''):
            return None, 'unsupported-encoding'
        return json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())), None
    except (ValueError, UnicodeError, OSError, EOFError, RecursionError):
        return None, 'not-json'


class Gate:
    def __init__(self, upstream, *, tunnel=None, expected=None, verdict=None, clock=time.monotonic,
                 wall=time.time, total=TOTAL_TIMEOUT):
        self.upstream, self.tunnel, self.expected, self.verdict = upstream, tunnel, expected, verdict
        self.clock, self.wall, self.total = clock, wall, total
        self.t0 = clock()
        self.lock = threading.Lock()
        self.armed = 0
        self.forwards = 0
        self.exchanges = []
        self.refused = Counter()
        self.reasons = Counter()
        self.tunnels = {'tunnelled': 0, 'failed': 0, 'bytes_up': 0, 'bytes_down': 0, 'refused': Counter()}
        self.secrets = set()  # ids seen in transit, kept in memory only for the output scan
        self.key = os.urandom(32)  # in-memory only: compares bearers across forwards
        self.first_bearer = None
        self.closed = threading.Event()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def arm(self):
        """Permit exactly one forward for the next read (none once MAX_FORWARDS were used)."""
        with self.lock:
            self.armed = 1 if self.forwards < MAX_FORWARDS else 0
            return bool(self.armed)

    def disarm(self):
        with self.lock:
            self.armed = 0

    def close(self):
        self.closed.set()
        self.server.shutdown()
        self.server.server_close()

    def summary(self):
        with self.lock:
            return {'forwarded': self.forwards, 'max_forwards': MAX_FORWARDS,
                    'refused_by_category': dict(sorted(self.refused.items())),
                    'refused_by_reason': dict(sorted(self.reasons.items())),
                    'connect': {'tunnelled': self.tunnels['tunnelled'], 'failed': self.tunnels['failed'],
                                'bytes_up': self.tunnels['bytes_up'], 'bytes_down': self.tunnels['bytes_down'],
                                'refused': dict(sorted(self.tunnels['refused'].items())),
                                'allowlist': list(TUNNEL_HOSTS) if self.tunnel else []},
                    'exchanges': [dict(item) for item in self.exchanges]}

    def scope_refusal(self, claims, header_account):
        """None when this read stays within the codex-as owner's account, else a fixed reason.
        Compares in memory only; nothing about the values is recorded."""
        owner = self.expected[1] if self.expected else None
        if not (isinstance(owner, str) and owner):
            return 'owner-account-unknown'
        account = (claims or {}).get('account')
        if account is None:
            return 'bearer-account-absent'
        if not _same(account, owner):
            return 'bearer-account-mismatch'
        if header_account is not None and not _same(header_account, owner):
            return 'account-header-mismatch'
        return None

    def _note_secret(self, *values):
        for value in values:
            if isinstance(value, str) and len(value) >= 4:
                self.secrets.add(value)

    def _handler(self):
        gate = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def path_parts(self):
                path, sep, _query = self.path.partition('?')
                return path, bool(sep)

            def refuse(self, reason, status=503):
                path, _ = self.path_parts()
                with gate.lock:
                    gate.refused[category(self.command, path)] += 1
                    gate.reasons[reason] += 1
                data = json.dumps({'error': {'type': 'server_error', 'code': 'quota_reserve_lx_gate',
                                             'message': 'quota reserve LX gate refused: ' + reason}}).encode()
                self.close_connection = True
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Connection', 'close')
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)

            def drain(self):
                length = self.headers.get('Content-Length')
                if length and length.isdigit() and int(length) <= 1 << 20:
                    self.rfile.read(int(length))

            def do_POST(self):
                self.drain()
                path, _ = self.path_parts()
                self.refuse('model-request' if path == '/backend-api/codex/responses' else 'unlisted-path')

            def do_PUT(self):
                self.drain()
                self.refuse('unlisted-method')

            do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_PUT

            def do_GET(self):
                path, query = self.path_parts()
                if path != USAGE_PATH:
                    return self.refuse('unlisted-path')
                if (query or self.headers.get('Content-Length') not in (None, '0')
                        or self.headers.get('Transfer-Encoding') or self.headers.get('Upgrade')):
                    return self.refuse('unexpected-request-shape')
                value = self.headers.get('Authorization') or ''
                bearer = value[7:] if value.startswith('Bearer ') and len(value) > 7 else None
                if bearer is None:
                    return self.refuse('missing-bearer')
                header_account = self.headers.get('ChatGPT-Account-ID')
                claims = bearer_claims(bearer)
                scope = gate.scope_refusal(claims, header_account)
                if scope:  # before the cap, the arm and any upstream byte
                    with gate.lock:
                        gate.armed = 0
                        gate._note_secret(*((claims or {}).get(key) for key in ('account', 'user')), header_account)
                    return self.refuse(scope)
                with gate.lock:
                    if gate.forwards >= MAX_FORWARDS:
                        reason = 'forward-cap'
                    elif not gate.armed:
                        reason = 'unarmed'
                    else:
                        reason = None
                        gate.armed -= 1
                        gate.forwards += 1
                        index = gate.forwards
                if reason:
                    return self.refuse(reason)
                headers = {name: value for name, value in self.headers.items() if name.lower() in FORWARD_HEADERS}
                digest = hmac.new(gate.key, bearer.encode('utf-8', 'replace'), hashlib.sha256).digest()
                with gate.lock:
                    if gate.first_bearer is None:
                        gate.first_bearer = digest
                    same = hmac.compare_digest(gate.first_bearer, digest)
                    if claims:
                        gate._note_secret(claims.get('account'), claims.get('user'))
                    gate._note_secret(header_account)
                started = gate.clock()
                result = forward(gate.upstream, USAGE_PATH, headers, total=gate.total, clock=gate.clock)
                record = {'index': index, 'outcome': result['outcome'], 'phase': result['phase'],
                          'status': result['status'], 'elapsed_ms': int((gate.clock() - started) * 1000),
                          'received_ms': int((started - gate.t0) * 1000),
                          'received_wall': int(gate.wall()),
                          'request_header_names': sorted({name.lower() for name in self.headers.keys()}),
                          'forwarded_header_names': sorted(name.lower() for name in headers),
                          'bearer_same_as_first_forward': same}
                if 'headers' in result:
                    record['response_header_names'] = sorted({name.lower() for name, _ in result['headers']})
                    record['content_type_is_json'] = any(
                        name.lower() == 'content-type' and value.split(';')[0].strip().lower() == 'application/json'
                        for name, value in result['headers'])
                if result['outcome'] == 'relayed':
                    record['tls_version'] = result['tls_version']
                    data, problem = decode_body(result['body'], result['headers'])
                    record['body_json'] = problem is None
                    if problem:
                        record['body_problem'] = problem
                    else:
                        record['usage'] = normalize_usage(data)
                        if isinstance(data, dict):
                            gate._note_secret(data.get('account_id'), data.get('user_id'))
                    record['identity'] = identity(data, claims, header_account, gate.expected, gate.wall())
                    if gate.verdict is not None:
                        record['qx_lease_verdict'] = gate.verdict(result['status'], result['body'], header_account)
                with gate.lock:
                    gate.exchanges.append(record)
                if result['outcome'] != 'relayed':
                    return self.refuse('upstream-' + result['outcome'].replace('_', '-'), 502)
                body = result['body']
                self.close_connection = True
                self.send_response(result['status'])
                for name, value in result['headers']:
                    if name.lower() not in HOP:
                        self.send_header(name, value)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(body)

            def do_CONNECT(self):
                self.close_connection = True
                target = self.path.lower() if TARGET.fullmatch(self.path) else '<malformed>'
                with gate.lock:
                    allowed = (gate.tunnel is not None and target in TUNNEL_HOSTS
                               and gate.tunnels['tunnelled'] + gate.tunnels['failed'] < MAX_TUNNELS)
                    if allowed:
                        gate.tunnels['tunnelled'] += 1
                    else:
                        refused = gate.tunnels['refused']
                        key = target if target in refused or len(refused) < MAX_DISTINCT else '<other>'
                        refused[key] += 1
                if not allowed:
                    self.send_response(403)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                host, port = target.rsplit(':', 1)
                try:
                    remote = gate.tunnel(host, int(port), CONNECT_TIMEOUT)
                except OSError:
                    with gate.lock:
                        gate.tunnels['tunnelled'] -= 1
                        gate.tunnels['failed'] += 1
                    self.send_response(502)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                try:
                    self.send_response(200, 'Connection established')
                    self.end_headers()
                    self.wfile.flush()
                    up, down = pump(self.connection, remote, gate.closed)
                    with gate.lock:
                        gate.tunnels['bytes_up'] += up
                        gate.tunnels['bytes_down'] += down
                finally:
                    remote.close()

        return Handler


def pump(client, remote, closed, idle=TUNNEL_IDLE, total=TUNNEL_TOTAL):
    """Opaque byte relay until either side closes, idle or total time passes, or the gate closes."""
    up = down = 0
    deadline = time.monotonic() + total
    quiet_until = time.monotonic() + idle
    selector = selectors.DefaultSelector()
    try:
        client.setblocking(False)
        remote.setblocking(False)
        selector.register(client, selectors.EVENT_READ, remote)
        selector.register(remote, selectors.EVENT_READ, client)
        while not closed.is_set() and time.monotonic() < min(deadline, quiet_until):
            events = selector.select(timeout=0.25)
            if not events:
                continue
            for key, _ in events:
                try:
                    data = key.fileobj.recv(65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    return up, down
                if not data:
                    return up, down
                key.data.setblocking(True)
                key.data.settimeout(5)
                try:
                    key.data.sendall(data)
                except OSError:
                    return up, down
                finally:
                    key.data.setblocking(False)
                if key.fileobj is client:
                    up += len(data)
                else:
                    down += len(data)
            quiet_until = time.monotonic() + idle
    finally:
        selector.close()
    return up, down
