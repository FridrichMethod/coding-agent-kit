"""coding-agent-kit protected mode: the request gate, the only exit of one protected invocation.

Provider-neutral core shared by codex-as and claude-as (load this file by path; it is never on
sys.path). A gate runs outside the boundary as its own process. It serves plain HTTP on the
loopback listening sockets that the launcher holds for the whole invocation and originates every
upstream connection itself, over verified TLS (HTTP/1.1, TLS 1.2 or later), through the caller's
plain http:// proxy when the launcher captured one at startup (the one the native would use for
HTTPS; a host goes direct only where every native's reading of NO_PROXY exempts it).

Providers plug in through four interfaces; BaseProvider documents each method:
    a default-deny RouteTable per native version (Route kinds: model, usage, read, paused_read);
    an identity binder: present() turns request headers into a Presented (one in-memory Secret
        plus selectors) or raises Refusal;
    a quota reader: read_quota() (gate-originated, through Gate.fetch) and observe_forwarded()
        (the native's own usage read) return Observations;
    the evaluator decision callback decide(Event) -> Decision, wired by the adapters to the reserve
        evaluator; this module never imports it.

Invariants:
- Every upstream request byte is written under the admission lock, in chunks of at most CHUNK
  bytes, each after live() confirms that the stream is still admitted, with a WRITE_TIMEOUT per
  chunk. A latch takes the same lock, closes every registered stream and tunnel first and only
  then reports, so no request byte follows it (a TLS or proxy handshake opened before it still
  may). Connects happen outside the lock, and no quota fetch ever runs under it; every lease
  method takes it. Monitor heartbeats are recorded without it, so a slow write never reads as a
  silent monitor.
- CPython's lock is not fair: a writer that releases and retakes it between chunks could keep
  every other user out until its whole request is written. So the admission lock (AdmissionLock)
  makes a chunk writer yield to every user already waiting for it before it takes the next chunk:
  a pause decided under the lock (a quota threshold, another identity or family, a failed read,
  an admission refusal), a timer step, a poll or an admission waits on chunk writers at most for
  the chunk each one is writing, or has already been let through to write, when it starts to
  wait.
- A close of admission (a latch, a monitor or launcher loss, the timer's deadline or monitor
  silence, shutdown) is also announced before it waits for the lock. A chunk writer checks for an
  announcement before each chunk, without the lock and again once it holds it, and stops with
  Closed: no chunk starts after its writer has seen an announcement, and a close announced while
  a writer holds the lock waits for that writer's current chunk, which finishes or times out.
- A Secret stays in memory: no event, status, message, reply or file carries a bearer or a value
  derived from one. Matching uses a keyed digest whose key never leaves this process.
- Monitor loss (EOF, silence past HEARTBEAT_MAX, or launcher EOF) is final for a gate instance;
  a restarted gate starts paused ("gate_restart").
- The lease goes when an observation fails to prove the expected identity (another identity, or
  none) or a rebind read fails: polls never read the old bearer's quota after that, and only a read
  that proves the identity again binds a new lease.
- CONNECT tunnels exist only in refresh mode (the D1 pre-launch refresh): a startup or restart
  gate refuses every CONNECT, so no opaque tunnel is open while model work runs.
- Evaluator output is checked: a Decision whose running flag, reason or fields (a dict of str keys
  to JSON scalars) is malformed counts as Decision(False, "evaluator_error").
- Leaving a pause needs a continuation armed through the launcher's control channel, while the
  monitor is alive and has been heard, answered within CONTINUATION_WINDOW by the first quota
  observation whose request started after it and that the gate applies (available, of the
  expected identity and family), and a passing evaluator decision on that observation. Any other
  observation uses the continuation up only when it is the continuation's own check. Polls and
  resets never resume work.
- Model families paused alone (Decision.families; Claude: a family whose own weekly limit is at its
  reserve): check_model() names the family of each model request, and a request of a paused family
  is refused with that family's reason, while the session and the other families go on. Every
  decision states the whole set; when a family joins it, the gate closes that family's registered
  streams under the admission lock before it reports, as a pause decided under the lock does (a
  chunk writer yields to it and stops at its next chunk), so no stream of that family stays open
  and no chunk is half written. A continuation is accepted while the session or any family is
  paused; one continuation answers every pause, and the evaluator decides which it lifts.

Process entry (a provider script loads this file and calls main(argv, make)):
    python3 -I -B PROVIDER_GATE --listen-fds A[,B...] --control-fd N [--monitor-fd N]
                                --mode startup|restart|refresh
    stdin: one JSON object, at most CONFIG_LIMIT bytes, then EOF. Core keys: max_age_s,
        heartbeat_max_s (optional), proxy (null, [host, port] or [host, port, NO_PROXY lists]: see
        proxy_setting); make(config) reads the rest and may set the provider's tunables
        (BaseProvider lists them).

Control channel (JSON lines on the inherited socket, section 10 of the reviewed design):
    launcher -> gate: {"op": "continue" | "disarm" | "status" | "shutdown", "id": N (optional)}
    gate -> launcher: {"op": "reply", "to": OP, "id": N (when given), ...};
        {"op": "ready", "port": P, "pid": N, "mode": M} once serving;
        {"op": "paused", "reason": R, "at": T, "fields": {...}} on a pause;
        {"op": "latched", "reason": R, "at": T, "fields": {...}} on monitor or launcher loss;
        {"op": "notice", "eligible": B, "reason": R, "at": T, "fields": {...}} while paused, when the
            latest quota check starts (B true, R "awaiting_continuation") or stops passing, at most
            once per notice_every seconds (a notice never pauses or resumes);
        {"op": "resumed", "at": T} after a verified continuation;
        {"op": "family_paused", "family": F, "reason": R, "at": T, "fields": {...}} when a model family
            is paused alone or its reason changes; {"op": "family_resumed", "family": F, "at": T} when
            a continuation lifted its pause;
        {"op": "refused", "reason": R} and exit status 3 when startup refuses.
    The reply to "continue" says "ok": true once the session runs with no family paused; while it
    runs with a family paused it says "ok": false, "running": true; and whenever a family was or is
    paused it carries "families": {F: R} (those still paused).
Monitor channel: monitor -> gate {"op": "hb"} | {"op": "poll"} | {"op": "polled", "ok": B};
    gate -> monitor {"op": "next", "after": SECONDS}.
"""

import collections
import contextlib
from dataclasses import dataclass, field, replace
import hashlib
import hmac
import http.client
import io
import ipaddress
import itertools
import json
import math
import os
import queue
import re
import socket
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


BODY_LIMIT = 8 * 1024 * 1024
READ_LIMIT = 1024 * 1024
CONFIG_LIMIT = 1024 * 1024
LINE_LIMIT = 64 * 1024
CHUNK = 64 * 1024
CONNECT_TIMEOUT = 5.0     # TCP connect, proxy tunnel and TLS handshake, each
READ_TIMEOUT = 10.0       # one whole gate-originated or forwarded read
WRITE_TIMEOUT = 5.0       # one request chunk written under the admission lock
CLIENT_TIMEOUT = 30.0     # a local client's request head and body
STREAM_IDLE = 900.0       # silence on an admitted model stream or tunnel
TICK = 0.02               # the independent deadline timer
HEARTBEAT_MAX = 1.5       # monitor liveness lease
ATTACH_GRACE = 10.0       # until the first heartbeat after the gate is ready
CONTINUATION_WINDOW = 30.0
NOTICE_EVERY = 30.0       # at most one paused-state notice to the launcher per this many seconds
REBIND_SLACK = CONNECT_TIMEOUT  # a request held behind a rebind waits for its reads plus this
USAGE_READS = (10, 60.0)  # gate-originated reads per sliding window (the usage-read cap)
REVALIDATIONS = (4, 60.0)  # hold-and-rebind checks per sliding window
POLL_RUNNING, POLL_PAUSED = 30.0, 60.0  # used when the evaluator names no next poll
USER_AGENT = "quota-probe/1"  # names no project, user or host
MODEL, USAGE, READ, PAUSED_READ = "model", "usage", "read", "paused_read"
KINDS = (MODEL, USAGE, READ, PAUSED_READ)
NEEDS_READ = "needs_read"
# Claude renders 403 as a /login hint and retries 429, so it gets 400; a Codex 401 would start a
# native refresh, so it gets 503.
REFUSAL_STATUS = {"codex": 503, "claude": 400}
FORBIDDEN_STATUS = {"codex": frozenset({401}), "claude": frozenset({401, 403, 429})}
HOP = frozenset(("host", "connection", "content-length", "keep-alive", "proxy-authorization",
                 "proxy-authenticate", "proxy-connection", "te", "trailer", "transfer-encoding",
                 "upgrade"))
FETCH_RESERVED = frozenset(("host", "user-agent", "authorization", "connection", "content-length",
                            "transfer-encoding", "accept-encoding", "proxy-authorization"))
TARGET = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*(\?[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?")
CONNECT_TARGET = re.compile(r"[A-Za-z0-9.-]{1,253}:[0-9]{1,5}")
FAMILY = re.compile(r"[a-z0-9_]{1,64}")  # a model family's name
FAMILY_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")


class Refusal(Exception):
    """A provider refuses a request or a startup; latch=True also pauses the gate."""

    def __init__(self, reason, latch=False):
        super().__init__(reason)
        self.reason, self.latch = reason, latch


class Unavailable(Exception):
    """Quota data could not be obtained; never read as zero usage."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Closed(Exception):
    """The stream is no longer admitted; nothing more may be written upstream."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class Secret:
    """A bearer held in memory only.

    It never prints, cannot be serialized, and compares in constant time through a keyed digest
    whose key never leaves this process. authorization() is the one way to the value, for the
    Authorization header of a request this gate sends upstream.
    """

    __slots__ = ("_value", "_digest")
    _key = os.urandom(32)

    def __init__(self, value):
        if (not isinstance(value, str) or not 0 < len(value) <= 16384
                or not all(33 <= ord(c) < 127 for c in value)):
            raise ValueError("malformed bearer")
        self._value = value
        self._digest = hmac.new(Secret._key, value.encode("ascii"), hashlib.sha256).digest()

    def matches(self, other):
        return isinstance(other, Secret) and hmac.compare_digest(self._digest, other._digest)

    def authorization(self):
        return "Bearer " + self._value

    def __repr__(self):
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __reduce__(self):
        raise TypeError("a Secret cannot be serialized")


@dataclass(frozen=True)
class Presented:
    """What one request presents: its bearer and the provider's identity selectors.

    Admission matches the bearer only; the selectors (for example ChatGPT-Account-ID) are carried
    for the provider. present() must therefore refuse any selector other than the expected
    identity's, and a missing one wherever the upstream would otherwise pick an account itself.
    """

    secret: object
    selectors: tuple = ()


@dataclass(frozen=True)
class Observation:
    """One quota response as the provider normalized it.

    received_at is the monotonic time the gate received or started the request whose NEW
    response this is (the gate sets it). identity and family are what the response proves for the
    presented bearer; data is the provider's window record for the evaluator, opaque here. An
    observation with ok=False is unavailable data: never zero usage. permanent=True pauses at once.
    """

    received_at: float
    ok: bool
    identity: object = None
    family: object = None
    reason: object = None
    permanent: bool = False
    data: object = None


@dataclass(frozen=True)
class Event:
    """An evaluator input: "observation", "continuation" (verified, armed at `at`) or "health"."""

    kind: str
    at: float
    name: object = None
    observation: object = None


@dataclass(frozen=True)
class Decision:
    """The evaluator's answer: running or paused(reason), message fields, the next poll, and the
    model families paused alone: (family, reason) pairs, the whole set at this decision."""

    running: bool
    reason: object = None
    fields: dict = field(default_factory=dict)
    next_poll: object = None
    families: tuple = ()


class Lease:
    """(presented bearer, identity, family, fresh_at, deadline, epoch); the bearer is redacted."""

    __slots__ = ("presented", "identity", "family", "fresh_at", "deadline", "epoch")

    def __init__(self, presented, identity, family, fresh_at, deadline, epoch):
        self.presented, self.identity, self.family = presented, identity, family
        self.fresh_at, self.deadline, self.epoch = fresh_at, deadline, epoch

    @property
    def secret(self):
        return self.presented.secret

    def __repr__(self):
        return "Lease(epoch=%d, <bearer redacted>)" % self.epoch


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class Upstream:
    """Where forwarded and gate-originated requests go. Plain HTTP only to a loopback literal."""

    host: str
    port: int = 443
    tls: bool = True
    ca_file: object = None

    def __post_init__(self):
        if not isinstance(self.host, str) or not self.host or not 0 < int(self.port) < 65536:
            raise ValueError("malformed upstream")
        if not self.tls and not _loopback(self.host):
            raise ValueError("a plain HTTP upstream must be a loopback address")

    @property
    def authority(self):
        host = "[%s]" % self.host if ":" in self.host else self.host
        return host if self.port == (443 if self.tls else 80) else "%s:%d" % (host, self.port)


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    kind: str
    query: bool = False
    limit: int = READ_LIMIT
    upstream: object = None

    def __post_init__(self):
        if (self.kind not in KINDS or self.method not in ("GET", "POST")
                or (self.kind == MODEL) != (self.method == "POST")
                or not TARGET.fullmatch(self.path) or "?" in self.path):
            raise ValueError("malformed route")


class RouteTable:
    """Default deny: only an exact (method, path) of the table, with a query only where allowed."""

    def __init__(self, version, routes):
        self.version, self._routes = version, {}
        for route in routes:
            key = (route.method, route.path)
            if key in self._routes:
                raise ValueError("duplicate route")
            self._routes[key] = route

    def match(self, method, target):
        if not isinstance(target, str) or not TARGET.fullmatch(target):
            return None, None
        path, mark, query = target.partition("?")
        route = self._routes.get((method, path))
        if route is None or (mark and not route.query):
            return None, None
        return route, (query if mark else None)


def parse_proxy(url):
    """The caller's proxy URL as (host, port), or None when unset.

    Only a plain http://HOST:PORT proxy without credentials or a path is supported; anything else
    raises ValueError, and protected mode refuses rather than going direct.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError("unsupported proxy") from None
    if (parts.scheme != "http" or not parts.hostname or not port or parts.username is not None
            or parts.password is not None or parts.path not in ("", "/") or parts.query
            or parts.fragment):
        raise ValueError("unsupported proxy")
    return (parts.hostname, port)


def bypasses(no_proxy, host):
    """Whether NO_PROXY exempts HOST from the proxy, which it does only where every native would.
    NO_PROXY is one entry list per set spelling (supervise.no_proxy_lists), and every list must
    exempt HOST. A list that is just "*" exempts every host; an entry NAME exempts the host NAME
    itself, and ".NAME" the hosts below NAME but not NAME. Any other entry ("*.NAME", a ":PORT"
    suffix, an address range, an IPv6 address, "*" among other entries) exempts nothing, because
    Claude Code or Codex reads it differently or not at all."""
    host = host.lower()
    return bool(no_proxy) and all(_exempts(tuple(entries), host) for entries in no_proxy)


NO_PROXY_NAME = re.compile(r"[a-z0-9._-]{1,253}")


def _exempts(entries, host):
    if entries == ("*",):
        return True
    return any(host.endswith(entry) if entry.startswith(".") else host == entry
               for entry in entries if NO_PROXY_NAME.fullmatch(entry))


def proxy_setting(value):
    """(proxy, no_proxy) of the configuration's proxy value: null, [host, port] or [host, port,
    [[entry, ...], ...]] (supervise.capture_proxy). Anything else raises ValueError."""
    if value is None:
        return None, ()
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise ValueError("malformed proxy")
    host, port = value[0], value[1]
    if not isinstance(host, str) or not host or isinstance(port, bool) or not isinstance(port, int) \
            or not 0 < port < 65536:
        raise ValueError("malformed proxy")
    lists = value[2] if len(value) == 3 else ()
    if not isinstance(lists, (list, tuple)) or not all(
            isinstance(entries, (list, tuple)) and all(isinstance(entry, str) for entry in entries)
            for entries in lists):
        raise ValueError("malformed no_proxy")
    return (host, port), tuple(tuple(entries) for entries in lists)


def values(headers, name):
    """Every value of header NAME (case-insensitive) in a list of (name, value) pairs."""
    name = name.lower()
    return [value for key, value in headers if key.lower() == name]


def _shut(sock):
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _close(sock):
    if sock is not None:
        _shut(sock)
        try:
            sock.close()
        except OSError:
            pass


def _quietly(call):
    try:
        call()
    except Exception:
        pass


def _left(deadline, clock, cap=CONNECT_TIMEOUT):
    """Seconds for the next blocking step: at most cap and never past deadline (None: cap)."""
    if deadline is None:
        return cap
    left = deadline - clock()
    if left <= 0:
        raise socket.timeout("deadline passed")
    return min(left, cap)


def open_tcp(host, port, proxy, timeout=CONNECT_TIMEOUT, deadline=None, clock=time.monotonic):
    """A TCP stream to host:port, through an HTTP CONNECT tunnel when a proxy is given.

    Each step waits at most timeout seconds, and none starts or waits past deadline (a clock()
    value) when one is given. Name resolution is bounded by the system resolver only."""
    if proxy is None:
        return socket.create_connection((host, port), timeout=_left(deadline, clock, timeout))
    sock = socket.create_connection(proxy, timeout=_left(deadline, clock, timeout))
    try:
        authority = "%s:%d" % ("[%s]" % host if ":" in host else host, port)
        sock.settimeout(_left(deadline, clock, timeout))
        sock.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n\r\n"
                      % (authority, authority, USER_AGENT)).encode("ascii"))
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            sock.settimeout(_left(deadline, clock, timeout))
            byte = sock.recv(1)  # one byte at a time: no tunnel byte is consumed here
            if not byte or len(head) > 8192:
                raise OSError("proxy refused the tunnel")
            head += byte
        status = head.split(b"\r\n", 1)[0].split(b" ", 2)
        if len(status) < 2 or status[1] != b"200":
            raise OSError("proxy refused the tunnel")
        return sock
    except BaseException:
        sock.close()
        raise


def parse_response(raw):
    """(status, headers, body) of one complete HTTP/1.1 response in memory, or (0, [], b"")."""
    class Source:
        def makefile(self, *args, **kwargs):
            return io.BytesIO(raw)

    try:
        response = http.client.HTTPResponse(Source())
        response.begin()
        return response.status, response.getheaders(), response.read()
    except (http.client.HTTPException, ValueError, OSError):
        return 0, [], b""


def _read_all(sock, limit, timeout, clock=time.monotonic):
    """Bytes until EOF, or None past limit or after timeout seconds in total."""
    deadline, data = clock() + timeout, bytearray()
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sock.settimeout(min(remaining, CONNECT_TIMEOUT))
        chunk = sock.recv(65536)
        if not chunk:
            return bytes(data)
        data += chunk
        if len(data) > limit:
            return None


def refusal_response(provider, reason):
    """(status, JSON body) of a refusal in the provider's own error format."""
    message = ("quota reserve: model requests are paused (%s); continue from the launcher terminal "
               "after a fresh quota check" % reason)
    if provider == "claude":
        payload = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    else:
        payload = {"error": {"type": "server_error", "code": "quota_reserve_paused", "message": message}}
    return REFUSAL_STATUS.get(provider, 503), json.dumps(payload).encode()


class BaseProvider:
    """Defaults for a provider; X and A subclass it.

    Attributes: name ("codex" or "claude"); routes (RouteTable); upstream (Upstream);
    expected_identity (what Observation.identity must equal); family (None or what
    Observation.family must equal); connect_allow ({"host:port": (host, port)}, the exact targets
    a CONNECT may reach, served by a refresh-mode gate only; empty by default); rebind_timeout
    (seconds each gate fetch of a rebind may take) and rebind_reads (the most fetches one rebind
    makes, 2 by default: a request held behind it waits rebind_reads x rebind_timeout plus
    REBIND_SLACK). Tunables, which make(config) may set per provider:
    usage_reads and revalidations ((count, window seconds) budgets), continuation_window,
    attach_grace and notice_every (seconds).
    """

    name = "codex"
    routes = RouteTable("none", ())
    upstream = None
    expected_identity = None
    family = None
    connect_allow = {}
    rebind_timeout = READ_TIMEOUT
    rebind_reads = 2
    usage_reads = USAGE_READS
    revalidations = REVALIDATIONS
    continuation_window = CONTINUATION_WINDOW
    attach_grace = ATTACH_GRACE
    notice_every = NOTICE_EVERY

    def present(self, route, headers):
        """Presented for a request, or raise Refusal (latch=True pauses). Default: exactly one
        "Authorization: Bearer X" and no other credential header; a model route latches.

        Admission compares the bearer alone, so an override that returns selectors must refuse
        every selector other than the expected identity's (see Presented)."""
        auth = values(headers, "authorization")
        if (len(auth) != 1 or not auth[0].startswith("Bearer ") or values(headers, "x-api-key")
                or values(headers, "proxy-authorization")):
            raise Refusal("selector_conflict", route.kind == MODEL)
        try:
            return Presented(Secret(auth[0][7:]))
        except ValueError:
            raise Refusal("selector_conflict", route.kind == MODEL) from None

    def check_model(self, presented, body):
        """Raise Refusal when the request body names a model or feature the lease cannot govern;
        return None, or the name of the model family ([a-z0-9_]{1,64}) the request belongs to, which
        the gate refuses and closes alone while the evaluator pauses that family."""

    def check_read(self, route, presented):
        """Raise Refusal when a forwarded read is out of scope (for example a foreign account)."""

    def read_quota(self, gate, presented, cause):
        """One gate-originated model-free quota read through gate.fetch() with presented.secret;
        returns an Observation (received_at is replaced by the gate). cause: startup, poll,
        rebind or continuation. A "poll" applies only while its bearer is still the lease's, so it
        never rebinds the lease; a provider that re-reads a rotated store bearer checks it with
        cause "rebind"."""
        raise Unavailable("no_reader")

    def observe_forwarded(self, route, presented, status, headers, body, received_at):
        """The Observation of the native's own forwarded usage read, or None to ignore it."""
        return None

    def startup(self, gate):
        """Before the gate serves: raise Refusal to refuse startup (exit 3). A gate that holds the
        bearer reads it here and runs gate.check(); otherwise the launcher's preflight read binds."""

    def continue_check(self, gate):
        """After a continuation is armed: return gate.check()'s outcome, or NEEDS_READ so that the
        launcher drives a fresh read whose usage request passes through the gate."""
        return NEEDS_READ

    def refusal(self, reason):
        return refusal_response(self.name, reason)


def _seconds(value, name, top=3600):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= top:
        raise ValueError(name + " out of range")
    return float(value)


def _budget(value, name):
    """A (count, window seconds) budget."""
    if (not isinstance(value, (tuple, list)) or len(value) != 2 or isinstance(value[0], bool)
            or not isinstance(value[0], int) or not 0 < value[0] <= 1000):
        raise ValueError(name + " out of range")
    return value[0], _seconds(value[1], name)


def _scalar(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return len(value) <= 1024
    return value is None or isinstance(value, (bool, int))


def _families_well_formed(families):
    """At most 64 (family, reason) pairs, of distinct family names and reason words."""
    if not isinstance(families, (tuple, list)) or len(families) > 64:
        return False
    names = [entry[0] for entry in families if isinstance(entry, (tuple, list)) and len(entry) == 2]
    return (len(names) == len(families) == len(set(names))
            and all(isinstance(name, str) and FAMILY.fullmatch(name) and isinstance(reason, str)
                    and FAMILY_REASON.fullmatch(reason) for name, reason in families))


def _well_formed(decision):
    """A Decision the gate can act on and report: a bool running flag, a short str reason (or
    None), at most 64 message fields of str keys to JSON scalars and well-formed families."""
    if not isinstance(decision, Decision) or not isinstance(decision.running, bool):
        return False
    if decision.reason is not None and (not isinstance(decision.reason, str) or len(decision.reason) > 200):
        return False
    fields = decision.fields
    return (isinstance(fields, dict) and len(fields) <= 64
            and all(isinstance(key, str) and len(key) <= 64 and _scalar(value) for key, value in fields.items())
            and _families_well_formed(decision.families))


class AdmissionLock:
    """A gate's admission lock: re-entrant, and chunk writers yield to its other users.

    A user other than a chunk writer (`with lock:`) registers before it waits for the lock. A chunk
    writer (`with lock.chunk():`) first waits until every user registered before it asked has had
    the lock, and only then asks for it. So a user waits on writers at most for the chunk each one
    is writing, or has already been let through to write, when it registers; and a writer waits
    only for the users ahead of it, never for those that come later. LOCK is the re-entrant lock it
    orders access to: a threading.RLock unless a test passes an instrumented one."""

    def __init__(self, lock=None):
        self._lock = threading.RLock() if lock is None else lock
        self._turns = threading.Condition(threading.Lock())  # a leaf: nothing is taken while held
        self._tickets, self._waiting = itertools.count(), set()

    def __enter__(self):
        ticket = None
        try:
            with self._turns:
                ticket = next(self._tickets)
                self._waiting.add(ticket)
            self._lock.acquire()
        finally:
            if ticket is not None:
                with self._turns:
                    self._waiting.discard(ticket)
                    self._turns.notify_all()
        return self

    def __exit__(self, *exc):
        self._lock.release()

    @contextlib.contextmanager
    def chunk(self):
        """Hold the lock to write one chunk, once every user registered before this call has had it.
        Never call it with the lock held."""
        with self._turns:
            mark = next(self._tickets)
            self._turns.wait_for(lambda: not any(ticket < mark for ticket in self._waiting))
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()


class Gate:
    """Lease, routes, admission lock, stream registry and latch of one gate instance."""

    def __init__(self, provider, decide, *, max_age, mode="startup", proxy=None, no_proxy=(),
                 heartbeat_max=HEARTBEAT_MAX, attach_grace=None, continuation_window=None,
                 usage_reads=None, revalidations=None, notice_every=None, clock=time.monotonic,
                 wall=time.time, notify=None, emit=None):
        """Tunables left as None come from the provider's attributes (BaseProvider lists them).
        proxy is (host, port) or None; no_proxy the hosts that bypass it (bypasses())."""
        if mode not in ("startup", "restart", "refresh"):
            raise ValueError("unknown gate mode")
        status, _ = provider.refusal("probe")
        if status in FORBIDDEN_STATUS.get(provider.name, ()) or not 400 <= status < 600:
            raise ValueError("refusal status not allowed for this provider")

        def tunable(value, name):
            return getattr(provider, name, getattr(BaseProvider, name)) if value is None else value

        self.provider, self._decider, self.mode = provider, decide, mode
        self.max_age, self.proxy, self.no_proxy = _seconds(max_age, "max_age"), proxy, tuple(no_proxy)
        self.heartbeat_max = _seconds(heartbeat_max, "heartbeat_max")
        self.attach_grace = _seconds(tunable(attach_grace, "attach_grace"), "attach_grace")
        self.notice_every = _seconds(tunable(notice_every, "notice_every"), "notice_every")
        self.continuation_window = _seconds(tunable(continuation_window, "continuation_window"),
                                            "continuation_window")
        self.usage_reads = _budget(tunable(usage_reads, "usage_reads"), "usage_reads")
        self.revalidations = _budget(tunable(revalidations, "revalidations"), "revalidations")
        self.clock, self.wall = clock, wall
        self.notify = notify or (lambda message: None)
        self.emit = emit or (lambda event, **fields: None)
        self.lock = AdmissionLock()
        self._beat_lock, self._beat = threading.Lock(), None  # heartbeats take only this lock
        # Reasons of the closes announced and not yet through the lock (_closing); chunk writers read
        # the tuple without any lock. _closers_lock is a leaf: nothing else is taken while it is held.
        self._closers_lock, self._closers = threading.Lock(), ()
        self.state = {"startup": "checking", "restart": "paused", "refresh": "refresh"}[mode]
        self.reason = "gate_restart" if mode == "restart" else None
        self.fields, self.lost, self.closed = {}, None, False
        self.lease, self.last_fresh, self.epoch = None, None, 0
        self.streams, self._ids = {}, itertools.count(1)
        # Model families paused alone (family -> reason), the family of each admitted stream that has
        # one, and why a family pause closed a stream, until the stream is released.
        self.paused_families, self.stream_families, self._ended = {}, {}, {}
        self.armed, self._answered = None, None
        # Whether the latest attributed quota observation of a pause passes (a failed read leaves
        # it as it was, unless the launcher has not yet been told that it passes), and what the
        # launcher's terminal last said about it: False once a pause line (or the restart line)
        # said paused, True once a notice said it passes; and when.
        self._eligible, self._shown, self._shown_at = False, False, clock()
        self.attached, self.next_poll = None, None
        self.rebinding = None
        self._reads, self._checks = collections.deque(), collections.deque()
        self._contexts = {}
        if mode == "restart":
            self._decide(Event("health", clock(), name="gate_restart"))

    # -- evaluator and transitions (lock held) --------------------------------------------------
    def _decide(self, event):
        try:
            decision = self._decider(event)
        except Exception:
            decision = None
        if not _well_formed(decision):
            return Decision(False, "evaluator_error")  # the paused families stay as they were
        if isinstance(decision.next_poll, (int, float)) and not isinstance(decision.next_poll, bool):
            self.next_poll = float(decision.next_poll)
        self._families(dict(decision.families), decision.fields)
        return decision

    def _families(self, paused, fields):
        """Make PAUSED (family -> reason) the families paused alone. A family that joins has its
        registered streams closed first; then each lift and each new pause or reason is reported."""
        before, self.paused_families = self.paused_families, paused
        if paused == before:
            return
        closed = {}
        for family in sorted(set(paused) - set(before)):
            streams = [stream for stream, owner in self.stream_families.items() if owner == family]
            closed[family] = 0
            for stream in streams:
                del self.stream_families[stream]
                close = self.streams.pop(stream, None)
                self._ended[stream] = paused[family]
                if close is not None:
                    _quietly(close)
                    closed[family] += 1
        at = round(self.wall(), 3)
        for family in sorted(set(before) - set(paused)):
            _quietly(lambda: self.emit("family_resumed", family=family))
            _quietly(lambda: self.notify({"op": "family_resumed", "family": family, "at": at}))
        for family in sorted(paused):
            if before.get(family) == paused[family]:
                continue
            reason = paused[family]
            _quietly(lambda: self.emit("family_paused", family=family, reason=reason, closed=closed.get(family, 0)))
            _quietly(lambda: self.notify({"op": "family_paused", "family": family, "reason": reason, "at": at,
                                          "fields": dict(fields)}))

    def _pause(self, reason, fields=None, op="paused"):
        """Pause, or update the reason and fields of a pause. An armed continuation survives an
        update (an observation that did not answer it, a refused request): only a new pause or a
        loss voids it."""
        if self.state != "paused" or op == "latched":
            self.armed = None
        if fields is not None:
            self.fields = dict(fields)
        self.reason = reason if isinstance(reason, str) and reason else "paused"
        if self.state == "paused" and op != "latched":
            self._notice()
            return
        self.state = "paused"
        closers, self.streams, self.stream_families = list(self.streams.values()), {}, {}
        for close in closers:  # first: no report below may leave a stream open
            _quietly(close)
        message = {"op": op, "reason": self.reason, "at": round(self.wall(), 3), "fields": dict(self.fields)}
        self._eligible, self._shown, self._shown_at = False, False, self.clock()
        _quietly(lambda: self.emit(op, reason=self.reason, closed=len(closers)))
        _quietly(lambda: self.notify(message))

    def _notice(self):
        """While paused (and not lost): tell the launcher when the latest attributed quota
        observation starts or stops passing, so its line shows when a continuation can succeed. A
        change is sent at most once per notice_every seconds after the previous line; tick() sends
        a deferred one, and a change undone in between sends nothing, as does a deferred "passes"
        after a failed read. It never resumes anything."""
        if self.state != "paused" or self.lost is not None or self.closed:
            return
        eligible = self._eligible
        now = self.clock()
        if eligible == self._shown or now - self._shown_at < self.notice_every:
            return
        self._shown, self._shown_at = eligible, now
        message = {"op": "notice", "eligible": eligible, "reason": self.reason, "at": round(self.wall(), 3),
                   "fields": dict(self.fields)}
        _quietly(lambda: self.emit("notice", eligible=eligible))
        _quietly(lambda: self.notify(message))

    def _resume(self, fields):
        self.state, self.reason, self.fields = "running", None, dict(fields)
        _quietly(lambda: self.emit("resumed"))
        _quietly(lambda: self.notify({"op": "resumed", "at": round(self.wall(), 3)}))

    def _take(self, times, budget):
        limit, window = budget
        now = self.clock()
        while times and now - times[0] >= window:
            times.popleft()
        if len(times) >= limit:
            return False
        times.append(now)
        return True

    def _refusal(self):
        if self.closed:
            return "gate_closed"
        if self.lost:
            return self.lost
        if self.last_beat is None:
            return "monitor_unheard"
        if self.state != "running":
            return self.reason or self.state
        now = self.clock()
        if self.last_fresh is None or now >= self.last_fresh + self.max_age:
            self._pause("stale", self._decide(Event("health", now, name="stale")).fields)
            return "stale"
        return None

    def _open(self):
        if self.closed:
            return False
        if self.mode == "refresh":
            return self.state == "refresh"
        return self.lost is None and self.last_beat is not None and self.state == "running"

    def live(self, stream):
        """Whether STREAM may still send upstream; the caller holds the lock."""
        return self._open() and stream in self.streams

    # -- liveness, latch and timer ---------------------------------------------------------------
    def attach(self):
        """The gate is ready: the first heartbeat must arrive within attach_grace."""
        with self.lock:
            self.attached = self.clock()

    @property
    def last_beat(self):
        with self._beat_lock:
            return self._beat

    def heartbeat(self):
        """Record one monitor heartbeat. It never waits for the admission lock, so beats that
        arrive while a request chunk is being written count when the timer next runs."""
        now = self.clock()
        with self._beat_lock:
            self._beat = now if self._beat is None else max(self._beat, now)

    @contextlib.contextmanager
    def _closing(self, reason):
        """Hold the admission lock for a close of admission, announced before waiting for it. A
        chunk writer that has seen the announcement starts no further chunk (send), so the close
        waits at most for the chunk a writer was already writing, never for whole requests. The
        announcement is withdrawn under the lock once the close is done, or at once if waiting for
        the lock ends in an exception. Every announced close leaves admission closed (paused, lost
        or shut down), so a writer it stops is one whose stream the close removes."""
        with self._closers_lock:
            self._closers += (reason,)
        held = False
        try:
            with self.lock:
                held = True
                try:
                    yield
                finally:
                    self._withdraw(reason)
        finally:
            if not held:
                self._withdraw(reason)

    def _withdraw(self, reason):
        with self._closers_lock:
            closers = list(self._closers)
            closers.remove(reason)
            self._closers = tuple(closers)

    def _stop_if_closing(self):
        closers = self._closers
        if closers:
            raise Closed(closers[0])

    def lose(self, reason):
        """Monitor or launcher loss: sticky for this gate instance; pauses and closes streams."""
        with self._closing(reason):
            if self.lost is not None or self.closed:
                return
            self.lost = reason
            decision = self._decide(Event("health", self.clock(), name=reason))
            self._pause(reason, decision.fields, op="latched")

    def latch(self, reason):
        with self._closing(reason):
            decision = self._decide(Event("health", self.clock(), name=reason))
            self._pause(reason, decision.fields)

    def _due(self, now, beat):
        """The close the timer owes at NOW, given the last heartbeat BEAT: "monitor_stale", "stale"
        or None. It reads the state without the lock, which a chunk writer may hold."""
        if self.closed:
            return None
        if self.mode != "refresh" and self.lost is None and self.attached is not None:
            silent = (now - self.attached > self.attach_grace) if beat is None else (now - beat > self.heartbeat_max)
            if silent:
                return "monitor_stale"
        if self.state == "running" and (self.last_fresh is None or now >= self.last_fresh + self.max_age):
            return "stale"
        return None

    def tick(self):
        """One step of the independent timer: monitor silence, the lease deadline and a deferred
        paused-state notice. It decides on one reading of the clock and the last heartbeat, taken
        before the lock, and announces the close it owes (_closing), so waiting for the lock
        neither delays the close behind a chunked write nor changes what it decides. A close it
        decided stands even when an observation applied while it waited renewed the lease: that
        fails closed, and leaving the pause takes a continuation."""
        now, beat = self.clock(), self.last_beat
        due = self._due(now, beat)
        with self._closing(due) if due else self.lock:
            if self.closed:
                return
            if due == "monitor_stale" and self.lost is None:
                self.lose("monitor_stale")
            if self.state == "running" and (due == "stale" or self.last_fresh is None
                                            or now >= self.last_fresh + self.max_age):
                self._pause("stale", self._decide(Event("health", now, name="stale")).fields)
            elif self.state == "paused":
                self._notice()

    def poll_delay(self):
        with self.lock:
            if self.next_poll is not None:
                return self.next_poll
            return POLL_RUNNING if self.state == "running" else POLL_PAUSED

    # -- observations ----------------------------------------------------------------------------
    def _answers(self, at):
        if self.armed is None or at < self.armed:  # program order: a read started after arming
            return False
        armed, self.armed = self.armed, None
        if at - armed > self.continuation_window:
            self.emit("continuation_expired")
            return False
        self._answered = armed
        return True

    def observe(self, presented, observation, cause):
        """Apply one quota observation for PRESENTED's bearer; returns an outcome label."""
        with self.lock:
            if self.closed or self.lost is not None:
                return "lost"
            if cause == "poll" and (self.lease is None or not self.lease.secret.matches(presented.secret)):
                return "superseded"  # a poll for a bearer the lease no longer holds never rebinds it
            at = observation.received_at
            if self.last_fresh is not None and at < self.last_fresh:
                return "superseded"  # an older read answered late; newer data stands
            # Only an observation the gate applies below answers an armed continuation. A failed one,
            # or one of another identity or family, proves nothing and leaves it armed for the read
            # that answers it, unless it is the continuation's own check, which any answer uses up (a
            # read the launcher drove for it ends with the launcher's disarm instead).
            own = cause == "continuation"
            if not observation.ok:
                if own:
                    self._answers(at)
                reason = observation.reason or "quota_unavailable"
                if self._eligible and not self._shown:  # a held-back "passes" notice is stale now
                    self._eligible = False
                decision = self._decide(Event("health", at, name="quota_unavailable", observation=observation))
                self.emit("unavailable", cause=cause, reason=reason)
                forced = observation.permanent or cause in ("rebind", "startup")
                if cause == "rebind":
                    self.lease = None  # a new bearer went unverified: the old one may be signed out
                if forced or not decision.running:
                    self._pause(reason if forced else (decision.reason or reason), decision.fields)
                return reason
            if observation.identity is None or observation.identity != self.provider.expected_identity:
                if own:
                    self._answers(at)
                reason = ("identity_unattributed" if observation.identity is None else
                          "bearer_identity_mismatch" if cause == "rebind" else "identity_mismatch")
                # The lease no longer proves the expected identity (another account signed in, or a
                # read did not attribute the bearer): no poll may read the old bearer's quota or make
                # the pause continuable with it, so only a read that proves the identity again binds.
                self.lease, self._eligible = None, False
                self._pause(reason, self._decide(Event("health", at, name=reason)).fields)
                return reason
            if self.provider.family is not None and observation.family != self.provider.family:
                if own:
                    self._answers(at)
                self._eligible = False
                self._pause("unsupported_family", self._decide(Event("health", at, name="unsupported_family")).fields)
                return "unsupported_family"
            answers = self._answers(at)
            if answers:
                self._decide(Event("continuation", self._answered))
            decision = self._decide(Event("observation", at, observation=observation))
            if self.lease is None or not self.lease.secret.matches(presented.secret):
                self.epoch += 1
            self.lease = Lease(presented, observation.identity, observation.family, at, at + self.max_age, self.epoch)
            self.last_fresh = at
            self.emit("fresh", cause=cause, epoch=self.epoch, running=decision.running, answers=answers)
            self._eligible = decision.running or decision.reason == "awaiting_continuation"
            if not decision.running:
                self._pause(decision.reason or "paused", decision.fields)
            elif self.state == "checking":
                self.state, self.reason, self.fields = "running", None, dict(decision.fields)
            elif self.state == "paused":
                if answers:
                    self._resume(decision.fields)
                else:
                    self._pause("awaiting_continuation", decision.fields)
            else:
                self.fields = dict(decision.fields)
            return "fresh"

    def check(self, presented, cause):
        """One gate-originated quota read with PRESENTED's bearer: the fetch runs outside the lock,
        its observation is applied under it."""
        started = self.clock()
        try:
            observation = self.provider.read_quota(self, presented, cause)
            if not isinstance(observation, Observation):
                raise Unavailable("reader_error")
        except Unavailable as error:
            observation = Observation(started, False, reason=error.reason)
        except Exception:
            observation = Observation(started, False, reason="reader_error")
        return self.observe(presented, replace(observation, received_at=started), cause)

    def poll(self):
        """One poll the monitor asked for: the provider's own poll, or a check with the lease."""
        own = getattr(self.provider, "poll", None)
        if own is not None:
            own(self)
            return
        with self.lock:
            lease = None if self.lost or self.closed else self.lease
        if lease is not None:
            self.check(lease.presented, "poll")

    # -- admission -------------------------------------------------------------------------------
    def admit(self, presented, body, closer):
        """Decide one model request before any upstream byte: (ok, reason, stream id).

        A new bearer is held while one gate-read with that bearer (outside the lock) rebinds the
        lease; a same-identity pass keeps admitted streams, anything else pauses and closes them."""
        for _ in range(3):
            with self.lock:
                refusal = self._refusal()
                if refusal:
                    return False, refusal, None
                try:
                    family = self.provider.check_model(presented, body)
                except Refusal as error:
                    if error.latch:
                        self._pause(error.reason, self._decide(Event("health", self.clock(), name=error.reason)).fields)
                    return False, error.reason, None
                if family is not None and not (isinstance(family, str) and FAMILY.fullmatch(family)):
                    return False, "family_unproved", None
                if family in self.paused_families:
                    return False, self.paused_families[family], None
                if presented.secret is None:
                    self._pause("selector_conflict")
                    return False, "selector_conflict", None
                if self.lease is not None and self.lease.secret.matches(presented.secret):
                    stream = next(self._ids)
                    self.streams[stream] = closer
                    if family is not None:
                        self.stream_families[stream] = family
                    self.emit("admitted", stream=stream, epoch=self.epoch)
                    return True, "admitted", stream
                wait, mine = self.rebinding, False
                if wait is None:
                    if not self._take(self._checks, self.revalidations):
                        self._pause("revalidation_budget",
                                    self._decide(Event("health", self.clock(), name="revalidation_budget")).fields)
                        return False, "revalidation_budget", None
                    self.rebinding = wait = threading.Event()
                    mine = True
                    self.emit("rotation_hold", epoch=self.epoch)
            if mine:
                try:
                    self.check(presented, "rebind")
                finally:
                    with self.lock:
                        self.rebinding = None
                    wait.set()
            elif not wait.wait(self.provider.rebind_reads * self.provider.rebind_timeout + REBIND_SLACK):
                return False, "rebind_timeout", None
        return False, "rebind_unresolved", None

    def release(self, stream):
        with self.lock:
            self.streams.pop(stream, None)
            self.stream_families.pop(stream, None)
            self._ended.pop(stream, None)

    def send(self, stream, sock, data):
        """Write DATA upstream for an admitted stream, chunk by chunk under the admission lock. Each
        chunk waits for the users already waiting for the lock (AdmissionLock.chunk), and an
        announced close (_closing) stops the write before its next chunk without the lock."""
        view, offset = memoryview(data), 0
        while offset < len(view):
            self._stop_if_closing()
            with self.lock.chunk():
                self._stop_if_closing()  # announced while this writer waited for the lock
                if not self.live(stream):
                    raise Closed(self.lost or self._ended.get(stream) or self.reason or "closed")
                sock.settimeout(WRITE_TIMEOUT)
                chunk = view[offset:offset + CHUNK]
                sock.sendall(chunk)
                offset += len(chunk)

    def read_allowed(self, kind):
        """Forwarded reads: none once lost; READ only while running; USAGE and PAUSED_READ also
        while checking or paused."""
        if self.closed or self.lost is not None or self.mode == "refresh":
            return False
        return self.state == "running" if kind == READ else True

    def send_read(self, kind, sock, data):
        with self.lock:
            if not self.read_allowed(kind):
                raise Closed(self.lost or self.reason or "paused")
            sock.settimeout(WRITE_TIMEOUT)
            sock.sendall(data)

    def open_tunnel(self, target, closer):
        """Register a CONNECT tunnel to an exact allowlisted target; stream id or None. Only a
        refresh-mode gate serves tunnels, and only until it is lost or shut down."""
        with self.lock:
            if self.mode != "refresh" or target not in self.provider.connect_allow or not self._open():
                return None
            stream = next(self._ids)
            self.streams[stream] = closer
            return stream

    # -- continuation ----------------------------------------------------------------------------
    def arm_continuation(self):
        """The launcher's trusted continue: refused while the monitor is lost, unheard or silent;
        otherwise, while the session or a model family is paused, arm one check and let the provider
        run it (or ask the launcher for a read)."""
        with self.lock:
            now = self.clock()
            if self.closed:
                return {"ok": False, "reason": "gate_closed"}
            if self.lost is not None:
                return {"ok": False, "reason": "monitor_lost"}
            if self.last_beat is None:
                return {"ok": False, "reason": "monitor_unheard"}
            if now - self.last_beat > self.heartbeat_max:
                return {"ok": False, "reason": "monitor_stale"}
            if self.state != "paused" and not (self.state == "running" and self.paused_families):
                return {"ok": False, "reason": "not_paused"}
            had = bool(self.paused_families)
            self.armed = now
            self.emit("continuation_armed")
        try:
            outcome = self.provider.continue_check(self)
        except Refusal as error:
            outcome = error.reason
        except Exception:
            outcome = "continuation_error"
        with self.lock:
            if outcome == NEEDS_READ and self.armed is not None:
                return {"ok": False, "needs_read": True, "reason": "armed"}
            self.armed = None
            families = dict(self.paused_families)
            extra = {"families": families} if had or families else {}
            failed = isinstance(outcome, str) and outcome not in (NEEDS_READ, "fresh")
            if self.state == "running" and not families:
                return dict({"ok": True, "reason": "resumed"}, **extra)
            if self.state == "running":
                return dict({"ok": False, "running": True, "reason": outcome if failed else "family_paused"}, **extra)
            return dict({"ok": False, "reason": self.lost or (outcome if failed else self.reason)}, **extra)

    def disarm(self):
        with self.lock:
            self.armed = None
        return {"ok": True}

    def status(self):
        with self.lock:
            return {"state": self.state, "reason": self.reason, "lost": self.lost,
                    "heard": self.last_beat is not None, "running": self._open() and self.mode != "refresh",
                    "streams": len(self.streams), "epoch": self.epoch, "armed": self.armed is not None,
                    "fields": dict(self.fields), "families": dict(self.paused_families)}

    def shutdown(self):
        with self._closing("gate_closed"):
            self.closed = True
            closers, self.streams, self.stream_families = list(self.streams.values()), {}, {}
        for close in closers:
            _quietly(close)

    # -- upstream --------------------------------------------------------------------------------
    def proxy_for(self, host, port):
        """The proxy for a connection to HOST:PORT: the captured one, unless NO_PROXY exempts HOST."""
        return None if self.proxy is None or bypasses(self.no_proxy, host) else self.proxy

    def open(self, upstream, deadline=None):
        """A connected upstream socket (TLS verified when upstream.tls); raises OSError. Each step
        takes at most CONNECT_TIMEOUT, and none runs past deadline (a clock() value) if given."""
        sock = open_tcp(upstream.host, upstream.port, self.proxy_for(upstream.host, upstream.port),
                        deadline=deadline, clock=self.clock)
        if not upstream.tls:
            return sock
        try:
            context = self._contexts.get(upstream.ca_file)
            if context is None:
                context = ssl.create_default_context(cafile=upstream.ca_file)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.set_alpn_protocols(["http/1.1"])
                self._contexts[upstream.ca_file] = context
            sock.settimeout(_left(deadline, self.clock))
            return context.wrap_socket(sock, server_hostname=upstream.host)
        except (OSError, ValueError) as error:
            sock.close()
            raise OSError("TLS to the upstream failed") from error

    def fetch(self, method, path, *, secret=None, headers=(), upstream=None, limit=READ_LIMIT, timeout=None):
        """One gate-originated model-free request: (status, headers, body), or raise Unavailable.

        It carries the neutral User-Agent, counts against the usage-read cap, and is bounded by
        timeout (READ_TIMEOUT) in total: connect, proxy tunnel and TLS steps never wait past the
        deadline, and a watchdog shuts the socket when it passes, whatever is blocking then (name
        resolution is bounded by the system resolver only). A body shorter than its Content-Length
        is unavailable. Never call it with the lock held.

        The fetch owns its socket until it returns: the reply is parsed by an HTTPResponse of its
        own rather than HTTPConnection.getresponse(), which hands the socket to a reply that says
        Connection: close, so that reading its body to the end would close the socket under the
        read loop."""
        if any(name.lower() in FETCH_RESERVED for name, _ in headers):
            raise ValueError("reserved header in a gate fetch")
        with self.lock:
            if self.closed:
                raise Unavailable("gate_closed")
            if not self._take(self._reads, self.usage_reads):
                self.emit("usage_read_cap")
                raise Unavailable("usage_read_cap")
        upstream = upstream or self.provider.upstream
        deadline = self.clock() + (timeout or READ_TIMEOUT)
        sock = connection = response = watchdog = None
        expired = threading.Event()
        try:
            sock = self.open(upstream, deadline)

            def expire():
                expired.set()
                _shut(sock)

            watchdog = threading.Timer(max(0.0, deadline - self.clock()), expire)
            watchdog.daemon = True
            watchdog.start()
            sock.settimeout(_left(deadline, self.clock))
            connection = http.client.HTTPConnection(upstream.host, upstream.port)
            connection.sock = sock
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", upstream.authority)
            connection.putheader("User-Agent", USER_AGENT)
            connection.putheader("Accept-Encoding", "identity")
            connection.putheader("Connection", "close")
            if secret is not None:
                connection.putheader("Authorization", secret.authorization())
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders()
            response = http.client.HTTPResponse(sock, method=method)
            response.begin()
            body = bytearray()
            while True:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise Unavailable("timeout")
                sock.settimeout(min(remaining, CONNECT_TIMEOUT))
                chunk = response.read1(65536)
                if not chunk:
                    break
                body += chunk
                if len(body) > limit:
                    raise Unavailable("response_too_large")
            if expired.is_set():
                raise Unavailable("timeout")
            if response.length:  # the upstream closed before the Content-Length was reached
                raise Unavailable("upstream_unavailable")
            return response.status, response.getheaders(), bytes(body)
        except (OSError, http.client.HTTPException, ValueError):
            late = expired.is_set() or self.clock() >= deadline
            raise Unavailable("timeout" if late else "upstream_unavailable") from None
        finally:
            if watchdog is not None:
                watchdog.cancel()
                watchdog.join()  # a running expire() shuts the socket before it is closed below
            for part in (response, connection):
                if part is not None:
                    _quietly(part.close)
            _close(sock)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gate"
    sys_version = ""
    timeout = CLIENT_TIMEOUT

    def log_message(self, *args):
        return

    def _reply(self, status, body, content_type="application/json"):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    def _refuse(self, reason):
        status, body = self.server.gate.provider.refusal(reason)
        self.server.gate.emit("refused", reason=reason, status=status)
        self._reply(status, body)

    def _dispatch(self):
        gate = self.server.gate
        self.close_connection = True
        if self.command == "CONNECT":
            return self._connect()
        if values(self.headers.items(), "upgrade"):
            return self._refuse("websocket_refused")
        route, query = gate.provider.routes.match(self.command, self.path)
        if route is None:
            gate.emit("route_refused", method=self.command[:16])
            return self._reply(404, b'{"error":{"type":"not_found","message":"quota reserve: route not allowed"}}')
        body = self._body(route)
        if body is None:
            return self._refuse("request_malformed")
        try:
            presented = gate.provider.present(route, list(self.headers.items()))
        except Refusal as error:
            if error.latch:
                gate.latch(error.reason)
            return self._refuse(error.reason)
        if route.kind == MODEL:
            return self._model(route, query, presented, body)
        return self._read(route, query, presented)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_CONNECT = _dispatch

    def _body(self, route):
        if self.headers.get_all("Transfer-Encoding"):
            return None
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) > 1:
            return None
        if not lengths:
            return b""
        value = lengths[0].strip()
        if not value.isdigit() or int(value) > BODY_LIMIT or (int(value) and route.kind != MODEL):
            return None
        try:
            body = self.rfile.read(int(value))
        except OSError:
            return None
        return body if len(body) == int(value) else None

    def _request(self, route, query, upstream, body, strip=()):
        target = route.path + ("" if query is None else "?" + query)
        lines = ["%s %s HTTP/1.1" % (self.command, target), "Host: " + upstream.authority]
        for name, value in self.headers.items():
            if name.lower() not in HOP and name.lower() not in strip:
                lines.append("%s: %s" % (name, value))
        lines.append("Connection: close")
        if route.kind == MODEL:
            lines.append("Content-Length: %d" % len(body))
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body

    def _model(self, route, query, presented, body):
        gate = self.server.gate
        holder = {"client": self.connection, "upstream": None}

        def close():
            _shut(holder["client"])
            _shut(holder["upstream"])

        ok, reason, stream = gate.admit(presented, body, close)
        if not ok:
            return self._refuse(reason)
        upstream_info = route.upstream or gate.provider.upstream
        sock = None
        try:
            try:
                sock = gate.open(upstream_info)
            except OSError:
                return self._refuse("upstream_unavailable")
            holder["upstream"] = sock
            self.server.after_connect(stream)
            try:
                gate.send(stream, sock, self._request(route, query, upstream_info, body))
            except Closed as closed:
                gate.emit("aborted_before_upstream", stream=stream)
                return self._refuse(closed.reason)
            gate.emit("forwarded", stream=stream, bytes=len(body))
            sock.settimeout(STREAM_IDLE)
            self.connection.settimeout(STREAM_IDLE)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
        except (OSError, ValueError):
            pass
        finally:
            gate.release(stream)
            _close(sock)
            gate.emit("stream_closed", stream=stream)

    def _read(self, route, query, presented):
        gate = self.server.gate
        try:
            gate.provider.check_read(route, presented)
        except Refusal as error:
            if error.latch:
                gate.latch(error.reason)
            return self._refuse(error.reason)
        received = gate.clock()
        with gate.lock:
            if not gate.read_allowed(route.kind):
                return self._refuse(gate.lost or gate.reason or "paused")
        upstream_info = route.upstream or gate.provider.upstream
        sock = None
        try:
            sock = gate.open(upstream_info)
            strip = ("accept-encoding",) if route.kind == USAGE else ()
            gate.send_read(route.kind, sock, self._request(route, query, upstream_info, b"", strip))
            raw = _read_all(sock, route.limit, READ_TIMEOUT, gate.clock)
        except Closed as closed:
            return self._refuse(closed.reason)
        except OSError:
            raw = None
        finally:
            _close(sock)
        if raw is None:
            return self._refuse("upstream_unavailable")
        if route.kind == USAGE:
            status, headers, payload = parse_response(raw)
            try:
                observation = gate.provider.observe_forwarded(route, presented, status, headers, payload, received)
            except Exception:
                observation = Observation(received, False, reason="reader_error")
            if isinstance(observation, Observation):
                gate.observe(presented, replace(observation, received_at=received), "forwarded")
        gate.emit("read", kind=route.kind)
        try:
            self.connection.sendall(raw)
        except OSError:
            pass

    def _connect(self):
        gate = self.server.gate
        target = self.path
        holder = {"client": self.connection, "upstream": None}

        def close():
            _shut(holder["client"])
            _shut(holder["upstream"])

        stream = gate.open_tunnel(target, close) if CONNECT_TARGET.fullmatch(target) else None
        gate.emit("connect", ok=stream is not None)
        if stream is None:
            return self._reply(403, b"", "text/plain")
        sock = writer = None
        try:
            host, port = gate.provider.connect_allow[target]
            try:
                sock = open_tcp(host, port, gate.proxy_for(host, port))
                writer = sock.dup()  # its own timeout: the writes never change the reader's
            except OSError:
                _close(sock)
                return self._reply(502, b"", "text/plain")
            holder["upstream"] = sock
            with gate.lock:
                if not gate.live(stream):
                    return self._reply(403, b"", "text/plain")
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            self.connection.settimeout(STREAM_IDLE)

            def pump():
                try:
                    while True:
                        chunk = self.rfile.read1(65536)
                        if not chunk:
                            break
                        gate.send(stream, writer, chunk)
                except (Closed, OSError, ValueError):
                    pass
                _shut(sock)

            thread = threading.Thread(target=pump, daemon=True)
            thread.start()
            sock.settimeout(STREAM_IDLE)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                self.connection.sendall(chunk)
            # The target ended its side: pass the end on now, not after the pump's join below.
            _quietly(lambda: self.connection.shutdown(socket.SHUT_WR))
            thread.join(CONNECT_TIMEOUT)
        except (OSError, ValueError):
            pass
        finally:
            gate.release(stream)
            _close(sock)
            if writer is not None:
                _quietly(writer.close)
            gate.emit("tunnel_closed", stream=stream)


class Server(ThreadingHTTPServer):
    """One HTTP server on an inherited, already listening loopback socket."""

    daemon_threads = True

    def __init__(self, listener, gate):
        self.address_family = listener.family
        super().__init__(listener.getsockname()[:2], _Handler, bind_and_activate=False)
        self.socket.close()
        self.socket = listener
        self.server_name, self.server_port = "gate", listener.getsockname()[1]
        self.gate = gate

    def after_connect(self, stream):
        """Test seam between the upstream connect and the first request byte."""


class Channel:
    """JSON lines out on a socket from a writer thread, so no caller blocks on the peer."""

    def __init__(self, sock):
        self.sock, self.queue = sock, queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def send(self, message):
        self.queue.put(json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n")

    def _run(self):
        while True:
            line = self.queue.get()
            if line is None:
                return
            try:
                self.sock.sendall(line.encode())
            except OSError:
                return

    def close(self, timeout=2.0):
        self.queue.put(None)
        self.thread.join(timeout)


def read_lines(sock, on_message, on_eof):
    """Call on_message(dict) for each JSON line; on_eof() at EOF, error or an oversized line."""
    buffer = b""
    while True:
        try:
            data = sock.recv(65536)
        except OSError:
            data = b""
        if not data:
            return on_eof()
        buffer += data
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            if isinstance(message, dict):
                on_message(message)
        if len(buffer) > LINE_LIMIT:
            return on_eof()


def _options(argv):
    options, index = {}, 0
    while index < len(argv):
        name = argv[index]
        if name not in ("--listen-fds", "--control-fd", "--monitor-fd", "--mode") or index + 1 >= len(argv) \
                or name in options:
            raise ValueError("usage")
        options[name] = argv[index + 1]
        index += 2
    mode = options.get("--mode", "startup")
    listen = [int(fd) for fd in options["--listen-fds"].split(",")]
    monitor = options.get("--monitor-fd")
    if mode not in ("startup", "restart", "refresh") or not listen or (monitor is None) != (mode == "refresh"):
        raise ValueError("usage")
    return listen, int(options["--control-fd"]), (None if monitor is None else int(monitor)), mode


def run_timer(gate, stop, tick=TICK):
    """The independent timer loop. An error in it latches the gate and ends it, so the launcher
    sees a dead gate and restarts it paused, instead of losing the timer silently."""
    while not stop.wait(tick):
        try:
            gate.tick()
        except Exception:
            _quietly(lambda: gate.lose("gate_error"))
            stop.set()


def _read_config(stream):
    raw = stream.read(CONFIG_LIMIT + 1)
    if len(raw) > CONFIG_LIMIT:
        raise ValueError("oversized configuration")
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError("configuration is not an object")
    return config


def main(argv, make, stdin=None):
    """Run one gate process. make(config) -> (provider, decide). Returns the exit status."""
    try:
        listen, control_fd, monitor_fd, mode = _options(argv)
    except (KeyError, ValueError):
        print("usage: PROVIDER_GATE --listen-fds A[,B] --control-fd N [--monitor-fd N] --mode MODE",
              file=sys.stderr)
        return 2
    channel = Channel(socket.socket(fileno=control_fd))
    control = channel.sock

    def refuse(reason):
        channel.send({"op": "refused", "reason": reason})
        channel.close()
        return 3

    try:
        config = _read_config(stdin or sys.stdin.buffer)
        provider, decide = make(config)
        proxy, no_proxy = proxy_setting(config.get("proxy"))
        gate = Gate(provider, decide, max_age=config["max_age_s"], mode=mode, proxy=proxy, no_proxy=no_proxy,
                    heartbeat_max=float(config.get("heartbeat_max_s", HEARTBEAT_MAX)), notify=channel.send)
    except Refusal as error:
        return refuse(error.reason)
    except Exception:
        return refuse("config_invalid")
    if mode == "startup":
        try:
            provider.startup(gate)
        except Refusal as error:
            return refuse(error.reason)
        except Exception:
            return refuse("startup_failed")
        if gate.state == "paused":
            return refuse(gate.reason or "startup_failed")
    stop = threading.Event()
    listeners = [socket.socket(fileno=fd) for fd in listen]
    servers = [Server(listener, gate) for listener in listeners]
    for server in servers:
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def on_control(message):
        op = message.get("op")
        number = message.get("id")
        tag = {"id": number} if isinstance(number, int) and not isinstance(number, bool) else {}

        def answer():
            try:
                if op == "continue":
                    reply = gate.arm_continuation()
                elif op == "disarm":
                    reply = gate.disarm()
                elif op == "status":
                    reply = gate.status()
                elif op == "shutdown":
                    reply = {"ok": True}
                else:
                    reply = {"ok": False, "reason": "unknown_op"}
            except Exception:
                reply = {"ok": False, "reason": "gate_error"}
            channel.send(dict(reply, op="reply", to=str(op)[:32], **tag))
            if op == "shutdown":
                stop.set()

        threading.Thread(target=answer, daemon=True).start()

    def on_control_eof():
        gate.lose("launcher_eof")
        stop.set()

    polling = threading.Lock()

    def on_monitor(message):
        op = message.get("op")
        if op == "hb":
            gate.heartbeat()
        elif op in ("poll", "polled") and polling.acquire(blocking=False):
            def run():
                try:
                    if op == "poll":
                        gate.poll()
                finally:
                    polling.release()
                    monitor_channel.send({"op": "next", "after": gate.poll_delay()})
            threading.Thread(target=run, daemon=True).start()

    if monitor_fd is not None:
        monitor_channel = Channel(socket.socket(fileno=monitor_fd))
        threading.Thread(target=read_lines, args=(monitor_channel.sock, on_monitor,
                                                  lambda: gate.lose("monitor_eof")), daemon=True).start()
    threading.Thread(target=read_lines, args=(control, on_control, on_control_eof), daemon=True).start()
    gate.attach()
    threading.Thread(target=run_timer, args=(gate, stop), daemon=True).start()
    channel.send({"op": "ready", "port": servers[0].server_port, "pid": os.getpid(), "mode": mode})
    if monitor_fd is not None:
        monitor_channel.send({"op": "next", "after": gate.poll_delay()})
    stop.wait()
    gate.shutdown()
    for server in servers:
        server.shutdown()
    channel.close()
    return 0
