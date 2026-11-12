"""QC research: wrapper-side model-free usage GET with an exact in-memory bearer.

Research code, exercised only against loopback fakes. Production intent is one
GET https://api.anthropic.com/api/oauth/usage (the native BASE_API_URL path,
not ANTHROPIC_BASE_URL), no body, standard TLS verification, no redirects, no
retries, no refresh, bounded headers/body/time. The response is normalized by
window name and duration; model-specific, overage and unknown rows are counted
but never substitute for the general five-hour and seven-day windows. Rows
whose admission semantics are unproved are reported separately so a gate can
fail closed while they are present.

Time bound: one watchdog armed before name resolution covers resolution,
connect, TLS handshake, request and response. Resolution runs in a daemon
thread joined with the remaining time; each connect attempt uses the remaining
time as its timeout; no header byte is sent once the deadline is spent.
"""

from datetime import datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
import http.client
import json
import math
import re
import socket
import ssl
import threading
import time

PRODUCTION_HOST = "api.anthropic.com"
USAGE_PATH = "/api/oauth/usage"
PROFILE_PATH = "/api/oauth/profile"
OAUTH_BETA = "oauth-2025-04-20"
SOCKET_TIMEOUT = 5.0
TOTAL_DEADLINE = 10.0
BODY_LIMIT = 65536
HEADER_LIMIT = 65536
RESET_SKEW_MS = 120000
GENERAL = {"five_hour": 5 * 3600 * 1000, "seven_day": 7 * 86400 * 1000}
POLICY_WINDOWS = {"five_hour": "five_hour", "weekly": "seven_day"}
# Native 2.1.322 usage row names (static), other than the two general windows.
OTHER_FAMILY = ("seven_day_oauth_apps", "seven_day_opus", "seven_day_sonnet", "cinder_cove",
                "wattle_ember", "extra_usage", "limits")
NON_ROWS = ("error", "type")
# Rows with a known scope (native labels them "Opus limit" and "Current week
# (Sonnet only)"): they restrict only models of that family. The mapping from
# an API model id to the family is itself unproved (a G0 input).
MODEL_ROWS = ("seven_day_opus", "seven_day_sonnet")
# Overage credit pool: billing after the windows, not a model or surface scope (I).
NON_SCOPING = ("extra_usage",)
MAX_ADDRESSES = 2
_RFC3339 = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?"
                      r"(?:Z|[+-][0-9]{2}:[0-9]{2})")
_LOOPBACK = ("127.0.0.1", "::1")


class Endpoint:
    __slots__ = ("scheme", "host", "port", "path")

    def __init__(self, scheme, host, port, path=USAGE_PATH):
        if scheme == "http" and host not in _LOOPBACK:
            raise ValueError("plain HTTP is allowed only for loopback fixtures")
        if scheme not in ("http", "https") or path not in (USAGE_PATH, PROFILE_PATH):
            raise ValueError("unsupported endpoint")
        self.scheme, self.host, self.port, self.path = scheme, host, port, path


PRODUCTION = Endpoint("https", PRODUCTION_HOST, 443)
PRODUCTION_PROFILE = Endpoint("https", PRODUCTION_HOST, 443, PROFILE_PATH)


def production_context():
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    return context


class Window:
    __slots__ = ("name", "duration_ms", "utilization", "resets_at_ms")

    def __init__(self, name, utilization, resets_at_ms):
        self.name, self.duration_ms = name, GENERAL[name]
        self.utilization, self.resets_at_ms = utilization, resets_at_ms

    def remaining(self):
        return 100.0 - self.utilization

    def as_dict(self):
        return {"name": self.name, "utilization": self.utilization, "resets_at_ms": self.resets_at_ms}


class Observation:
    """Normalized result of one fetch attempt. Holds no token and no raw body."""

    __slots__ = ("outcome", "http_status", "fresh", "windows", "window_states", "other_families",
                 "unknown_rows", "unproved_rows", "started_wall_ms", "received_wall_ms",
                 "received_monotonic", "server_date_ms", "error_type", "source")

    def __init__(self, outcome, **fields):
        self.outcome = outcome
        self.http_status = fields.get("http_status")
        self.fresh = fields.get("fresh", False)
        self.windows = fields.get("windows", {})
        self.window_states = fields.get("window_states", {})
        self.other_families = fields.get("other_families", ())
        self.unknown_rows = fields.get("unknown_rows", 0)
        self.unproved_rows = fields.get("unproved_rows", ())
        self.started_wall_ms = fields.get("started_wall_ms")
        self.received_wall_ms = fields.get("received_wall_ms")
        self.received_monotonic = fields.get("received_monotonic")
        self.server_date_ms = fields.get("server_date_ms")
        self.error_type = fields.get("error_type")
        self.source = fields.get("source", "direct")

    def valid_for(self, enabled):
        """True only for fresh direct 200 data with every enabled window valid."""
        if self.outcome != "response" or self.http_status != 200 or not self.fresh or self.source != "direct":
            return False
        return all(POLICY_WINDOWS[name] in self.windows for name in enabled)

    def summary(self):
        return {"outcome": self.outcome, "http_status": self.http_status, "fresh": self.fresh,
                "windows": {k: v.as_dict() for k, v in sorted(self.windows.items())},
                "window_states": dict(sorted(self.window_states.items())),
                "other_families": list(self.other_families), "unknown_rows": self.unknown_rows,
                "unproved_rows": list(self.unproved_rows), "error_type": self.error_type, "source": self.source}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError("non-finite number")


def _epoch_ms(text):
    if not isinstance(text, str) or not _RFC3339.fullmatch(text):
        return None
    if text[-1] != "Z" and (int(text[-5:-3]) > 23 or int(text[-2:]) > 59):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def _window(name, value, received_wall_ms):
    if value is None:
        return None, "missing"
    if not isinstance(value, dict):
        return None, "malformed"
    utilization, reset = value.get("utilization"), value.get("resets_at")
    if type(utilization) not in (int, Decimal):
        return None, "malformed"
    number = float(utilization)
    if not math.isfinite(number) or not 0 <= number <= 100:
        return None, "malformed"
    if reset is None:
        return None, "reset_unknown"
    reset_ms = _epoch_ms(reset)
    if reset_ms is None:
        return None, "malformed"
    if reset_ms <= received_wall_ms - RESET_SKEW_MS:
        return None, "reset_in_past"
    if reset_ms > received_wall_ms + GENERAL[name] + RESET_SKEW_MS:
        return None, "reset_beyond_duration"
    return Window(name, number, reset_ms), "valid"


def normalize(status, content_type, body, *, started_wall_ms, received_wall_ms,
              received_monotonic, server_date_ms=None, source="direct"):
    base = dict(http_status=status, started_wall_ms=started_wall_ms, received_wall_ms=received_wall_ms,
                received_monotonic=received_monotonic, server_date_ms=server_date_ms, source=source)
    try:
        data = json.loads(body.decode("utf-8"), object_pairs_hook=_unique,
                          parse_float=Decimal, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return Observation("response_invalid", **base)
    if not isinstance(data, dict):
        return Observation("response_invalid", **base)
    error = data.get("error")
    error_type = error.get("type") if isinstance(error, dict) else None
    if not isinstance(error_type, str) or not re.fullmatch(r"[a-z_]{1,64}", error_type):
        error_type = None
    if status != 200 or error is not None:
        return Observation("http_status" if status != 200 else "error_body", error_type=error_type, **base)
    if not (content_type or "").split(";")[0].strip().lower() == "application/json":
        return Observation("response_invalid", **base)
    windows, states = {}, {}
    for name in GENERAL:
        window, state = _window(name, data.get(name), received_wall_ms)
        states[name] = state if name in data else "missing"
        if window is not None:
            windows[name] = window
    others = tuple(name for name in OTHER_FAMILY if name in data)
    others += tuple(sorted(name for name in data if name.startswith(("five_hour_", "seven_day_"))
                           and name not in OTHER_FAMILY))
    unknown = sum(1 for name in data if name not in GENERAL and name not in others and name not in NON_ROWS)
    # Present (non-null, non-empty) rows that may scope a model or surface with
    # semantics this research has not proved: seven_day_oauth_apps, the credit
    # pools, any limits[] entry (native: per-model buckets by display name),
    # unknown five_hour_*/seven_day_* rows and unknown rows.
    unproved = tuple(sorted(name for name, value in data.items()
                            if name not in GENERAL and name not in NON_ROWS and name not in MODEL_ROWS
                            and name not in NON_SCOPING and value is not None and value != {} and value != []))
    return Observation("response", fresh=source == "direct", windows=windows, window_states=states,
                       other_families=others, unknown_rows=unknown, unproved_rows=unproved, **base)


class _Budget:
    def __init__(self, stream):
        self.stream, self.remaining = stream, HEADER_LIMIT

    def readline(self, limit=-1):
        size = self.remaining + 1 if limit < 0 else min(self.remaining + 1, limit)
        line = self.stream.readline(size)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise ValueError("header budget exceeded")
        return line

    def __getattr__(self, name):
        return getattr(self.stream, name)


class _BoundedResponse(http.client.HTTPResponse):
    def begin(self):
        if not isinstance(self.fp, _Budget):
            self.fp = _Budget(self.fp)
        super().begin()


def _server_date(value):
    if not value or len(value) > 64:
        return None
    try:
        return int(parsedate_to_datetime(value).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None


def _resolve(host, port, timeout, resolver):
    """Name resolution bounded by timeout. getaddrinfo ignores socket timeouts,
    so it runs in a daemon thread; a hung resolver thread may outlive the call
    (it holds no token) but never delays the caller past the deadline."""
    box = {}

    def work():
        try:
            box["infos"] = list(resolver(host, port, 0, socket.SOCK_STREAM, socket.IPPROTO_TCP))
        except (OSError, UnicodeError, ValueError) as error:
            box["error"] = error

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError("resolution deadline")
    if "error" in box:
        raise OSError("resolution failed") from None
    return box["infos"][:MAX_ADDRESSES]


def _bounded_get(bearer, endpoint, context, socket_timeout, total_deadline, user_agent, monotonic, resolver):
    """One bounded GET; returns ("ok", status, content_type, body, date) or ("fail", outcome, status).

    The watchdog is armed before resolution and shuts down every tracked socket
    at total_deadline, so a slow-drip peer cannot extend it. Every blocking
    step takes min(socket_timeout, time left) as its timeout, and the deadline
    is checked again immediately before the request (the first header byte).
    """
    start = monotonic()
    connection = None
    expired = threading.Event()
    guard = threading.Lock()
    tracked = []

    def remaining():
        left = total_deadline - (monotonic() - start)
        if left <= 0 or expired.is_set():
            raise TimeoutError("total deadline")
        return min(socket_timeout, left)

    def expire():
        with guard:
            expired.set()
            sockets = list(tracked)
        for item in sockets:
            try:
                item.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def track(item):
        with guard:
            if expired.is_set():
                item.close()
                raise TimeoutError("total deadline")
            tracked.append(item)
        return item

    watchdog = threading.Timer(max(0.0, total_deadline), expire)
    watchdog.daemon = True
    watchdog.start()
    status = None
    try:
        sock, last = None, None
        for family, kind, proto, _name, address in _resolve(endpoint.host, endpoint.port, remaining(), resolver):
            timeout = remaining()
            candidate = track(socket.socket(family, kind, proto))
            try:
                candidate.settimeout(timeout)
                candidate.connect(address)
            except OSError as error:
                candidate.close()
                last = error
                continue
            sock = candidate
            break
        if sock is None:
            raise last if last is not None else OSError("no address")
        if endpoint.scheme == "https":
            sock = track((context or production_context()).wrap_socket(
                sock, server_hostname=endpoint.host, do_handshake_on_connect=False))
            sock.settimeout(remaining())
            sock.do_handshake()
            connection = http.client.HTTPSConnection(endpoint.host, endpoint.port, timeout=socket_timeout,
                                                     context=context or production_context())
        else:
            connection = http.client.HTTPConnection(endpoint.host, endpoint.port, timeout=socket_timeout)
        connection.sock = sock  # connect() is never called: no second resolution or connect
        connection.response_class = _BoundedResponse
        sock.settimeout(remaining())  # raises before any header byte once the deadline is spent
        connection.request("GET", endpoint.path, None, {
            "Authorization": bearer.authorization_value(), "Accept": "application/json",
            "anthropic-beta": OAUTH_BETA, "Cache-Control": "no-cache", "Connection": "close",
            "User-Agent": user_agent})
        sock.settimeout(remaining())
        response = connection.getresponse()
        status = response.status
        if 300 <= status < 400:
            return ("fail", "redirect_refused", status)
        declared = response.getheader("Content-Length")
        if declared is not None and (not declared.isdigit() or int(declared) > BODY_LIMIT):
            return ("fail", "response_too_large", status)
        chunks, size = [], 0
        while not response.isclosed():
            sock.settimeout(remaining())
            chunk = response.read1(min(16384, BODY_LIMIT + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > BODY_LIMIT:
                return ("fail", "response_too_large", status)
        remaining()
        return ("ok", status, response.getheader("Content-Type"), b"".join(chunks), response.getheader("Date"))
    except TimeoutError:
        return ("fail", "deadline_exceeded", status)
    except (OSError, http.client.HTTPException, ValueError):
        return ("fail", "deadline_exceeded" if expired.is_set() else "transport_error", status)
    finally:
        watchdog.cancel()
        if connection is not None:
            connection.close()
        with guard:
            sockets = list(tracked)
        for item in sockets:
            item.close()


def fetch(bearer, endpoint=PRODUCTION, *, context=None, socket_timeout=SOCKET_TIMEOUT,
          total_deadline=TOTAL_DEADLINE, user_agent="coding-agent-kit-quota-research",
          monotonic=time.monotonic, wall_ms=lambda: int(time.time() * 1000), resolver=socket.getaddrinfo):
    """One bounded usage GET. No retry, no redirect, no proxy discovery, no refresh."""
    if endpoint.path != USAGE_PATH:
        raise ValueError("not the usage endpoint")
    started_wall = wall_ms()
    result = _bounded_get(bearer, endpoint, context, socket_timeout, total_deadline, user_agent,
                          monotonic, resolver)
    if result[0] == "fail":
        return Observation(result[1], http_status=result[2], started_wall_ms=started_wall,
                           received_wall_ms=wall_ms(), received_monotonic=monotonic())
    _, status, content_type, body, date = result
    return normalize(status, content_type, body, started_wall_ms=started_wall, received_wall_ms=wall_ms(),
                     received_monotonic=monotonic(), server_date_ms=_server_date(date))


_IDENT = re.compile(r"[A-Za-z0-9-]{1,64}")


def fetch_profile_identity(bearer, endpoint=PRODUCTION_PROFILE, *, context=None,
                           socket_timeout=SOCKET_TIMEOUT, total_deadline=TOTAL_DEADLINE,
                           user_agent="coding-agent-kit-quota-research", monotonic=time.monotonic,
                           wall_ms=lambda: int(time.time() * 1000), resolver=socket.getaddrinfo):
    """G0 option only: server-asserted (account_uuid, organization_uuid) for this bearer.

    Native uses this endpoint at login/refresh to bind a token to its account.
    Only the two UUIDs survive; email, names and plan fields are dropped.
    Returns None on any failure or anomaly.
    """
    if endpoint.path != PROFILE_PATH:
        raise ValueError("not the profile endpoint")
    result = _bounded_get(bearer, endpoint, context, socket_timeout, total_deadline, user_agent,
                          monotonic, resolver)
    if result[0] != "ok" or result[1] != 200:
        return None
    if not (result[2] or "").split(";")[0].strip().lower() == "application/json":
        return None
    try:
        data = json.loads(result[3].decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant)
        account = data["account"]["uuid"]
        organization = data["organization"]["uuid"]
    except (UnicodeDecodeError, ValueError, RecursionError, KeyError, TypeError):
        return None
    finally:
        data = None
    if not all(isinstance(v, str) and _IDENT.fullmatch(v) for v in (account, organization)):
        return None
    return (account, organization)
