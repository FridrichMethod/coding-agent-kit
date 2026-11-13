"""coding-agent-kit usage reserves: launch flags, per-window policy and the quota evaluator.

Pure: no I/O, no environment, no clock of its own. Load this file by path (it is
never on sys.path), like config.py; it does not import config.py and reads the
configuration only from the frozen Config the launcher passes in.

Flags (before NAME, either order, exact words, the value as the next argument):

    split_flags(args) -> (Overrides, rest)
        Takes leading --reserve-5h V and --reserve-weekly V off the launcher's
        arguments. V is "off" or ASCII decimal digits with an optional fraction,
        from 0 through 100. A repeated flag, a missing or invalid value, the
        --flag=V form, or no account name after the flags raise ReserveError.
        Without reserve flags the arguments come back unchanged, as a new list.
    Overrides(five_hour=None, weekly=None): the launch-only layer; never persisted.
    ReserveError(ValueError): one line without the "<tool>: " prefix, never
        quoting the argument; own_message is True.

Policy (each window on its own, never combined):

    resolve(tool, account, overrides, usage_reserve) -> Policy
        tool is "codex-as" or "claude-as"; usage_reserve is Config.usage_reserve.
        Per window: invocation, then [usage_reserve.<tool>_accounts.NAME], then
        [usage_reserve], then off. The first layer that sets the window wins,
        "off" included, so an explicit "off" overrides an inherited percentage.
    Policy(tool, account, five_hour, weekly): Setting per window
        settings() -> (Setting, Setting); enabled() -> {window: Decimal}
        off: True when both windows are off; the launch is then unprotected and
             unchanged. describe() -> the effective policy line for a protected start.
    Setting(window, reserve, source): reserve is a Decimal from 0 through 100 or
        None (off); source is "invocation", "account", "global" or "default".

Evaluator (one per gate instance; not thread-safe, the gate serializes it):

    Evaluator(policy, clock, *, paused=None, max_age=MAX_AGE,
              continuation_window=CONTINUATION_WINDOW)
        clock() returns monotonic seconds; every time below is on that clock,
        except wall-clock epoch seconds (wall_at, resets_at, last_fresh).
        max_age and continuation_window are finite seconds above 0.
        paused: a reason word starts paused with that cause instead of checking.
        A restarted gate builds Evaluator(policy, clock, paused="gate_restarted"),
        never a checking one.
    Inputs, each returning the Verdict after it:
        observe(Observation)    a NEW provider response the gate received
        failure(reason, at=None) a query that produced no response to evaluate
                                 (transport, HTTP status, parse); at: when it was sent
        fault(reason)           a condition of the binding that pauses at once
                                 (identity_changed, unsupported_plan, ...); while
                                 paused, a NEW attributed response with every enabled
                                 window valid, received after it, clears it, so never
                                 report attributed=True while the condition persists
        control_down(reason)    monitor or control channel lost (monitor_lost,
                                 monitor_stale, launcher_lost, ...): final for this
                                 evaluator. The first loss stays; control_up() never
                                 clears it. Recovery is a restarted gate, paused.
        control_up()            the monitor attached and a heartbeat was heard: clears
                                 the control_pending a new evaluator starts with; safe
                                 to report on every heard heartbeat
        continuation()          a verified trusted-terminal request to continue
        give_up()               the launcher's startup bound ran out: refuse
        tick()                  the timer: freshness and continuation deadlines
    Reasons are lower-case identifiers. fault and control_down refuse (ValueError)
    the evaluator's own condition words (WAITING, CAUSE_TEXT) and each other's
    (CONTROL_TEXT, FAULT_TEXT).
    verdict() -> Verdict; admits() -> bool, the admission test; next_poll(jitter=None)
        -> seconds until the next quota read.

States: "checking" until the first fresh passing reading with healthy control
("run"), or a refusal ("refuse"); "running"; "paused". While checking, a
threshold, an unavailable or unattributed response, a fault or control loss
refuses at once, before or after the first heartbeat alike; only a pending
monitor, no reading yet or a stale one waits (until give_up). A pause starts
when any enabled window has remaining <= reserve (remaining = 100 - used,
decided exactly for the numbers as the provider wrote them), when the latest
response lacks a valid enabled window or is not attributed, when the last
valid fresh reading is older than max_age (also the recovery budget:
transient failures never extend it), on a fault, or on control loss. Leaving
a pause needs healthy control, a continuation and then a NEW response,
received after it and within continuation_window, that passes; any other
answer uses the continuation up. A window reset or a passing poll never
resumes by itself, and a control loss never leaves this evaluator: the
restarted gate starts a new one paused. Unavailable data is never read as
zero usage.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_FLOOR
import math
import re


WINDOWS = ("five_hour", "weekly")
LABELS = {"five_hour": "five-hour", "weekly": "weekly"}
DURATIONS = {"five_hour": 5 * 3600, "weekly": 7 * 86400}  # seconds
FLAGS = {"--reserve-5h": "five_hour", "--reserve-weekly": "weekly"}
OFF = "off"
SOURCES = ("invocation", "account", "global", "default")

# Tunables (G0 section 7). Seconds unless noted; nothing else sets them.
POLL_RUNNING = 30.0           # quota read interval while running
POLL_NEAR = 15.0              # ... when a window is within NEAR_POINTS of its reserve
NEAR_POINTS = Decimal(10)     # percentage points
POLL_PAUSED = 60.0            # paused polls start here and double
POLL_PAUSED_MAX = 300.0       # ... up to this, for as long as the pause lasts
BACKOFF_MIN = 5.0             # retry after a failed read, jittered and doubling
BACKOFF_MAX = 60.0
MAX_AGE = 120.0               # oldest reading that still admits model work
RECOVERY_BUDGET = MAX_AGE     # the same deadline, not a second setting: Evaluator(max_age=)
                              # bounds recovery from the last fresh reading; failures never restart it
RESET_SKEW = 120.0            # tolerated clock skew around a reported reset time
QUERY_TIMEOUT = 10.0          # one quota read in total
CONNECT_TIMEOUT = 5.0         # each connect or TLS handshake within it
CONTINUATION_WINDOW = 30.0    # a continuation is answered only by a read within this
HEARTBEAT = 0.25              # monitor liveness interval, decoupled from polls
HEARTBEAT_LOSS = 1.5          # silence that counts as monitor loss
REBIND_HOLD = 3.0             # Codex: hold a request while its new bearer is bound
REVALIDATIONS_PER_MINUTE = 4  # credential revalidations, one per rotation
USAGE_READS_PER_MINUTE = 10   # usage-read cap per invocation
SELF_TEST_BOUND = 20.0        # macOS startup self-test

NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
OK, AT_RESERVE = "ok", "at_reserve"  # WindowStatus.status for valid readings
CHECKING, RUNNING, PAUSED = "checking", "running", "paused"
# Conditions that only wait while checking; every other one refuses the start.
WAITING = ("control_pending", "no_reading", "stale")
FLOOR = Context(rounding=ROUND_FLOOR)  # rounds down, so a sum compared with 100 keeps its order
SHOWN_DECIMALS = FLOOR.prec    # at most this many decimals in a running window's remaining value
LATEST_WALL = 253_400_000_000  # 9999-12-05: every reset accepted against it still formats
CONTROL_TEXT = {
    "control_pending": "the quota monitor is not running yet",
    "monitor_lost": "the quota monitor stopped",
    "monitor_stale": "the quota monitor stopped answering",
    "launcher_lost": "the launcher's control channel closed",
    "gate_unavailable": "the request gate is unavailable",
    "gate_restarted": "the request gate restarted",
}
FAULT_TEXT = {
    "identity_changed": "the signed-in account changed",
    "unsupported_plan": "the account's plan is not supported",
    "unsupported_family": "a request used a quota family the reserves do not cover",
    "revalidation_budget": "the credential changed too often",
}
CAUSE_TEXT = {  # what paused, once the latest check passes
    "threshold": "a usage window reached its reserve",
    "window_unavailable": "a usage window was unavailable",
    "unattributed": "a quota reading was not attributed to this account",
    "stale": "no fresh quota reading arrived in time",
}
OWN_CONDITIONS = frozenset(WAITING) | frozenset(CAUSE_TEXT)  # never a fault or a control loss


class ReserveError(ValueError):
    own_message = True


# -- flags --------------------------------------------------------------------


@dataclass(frozen=True)
class Overrides:
    five_hour: object = None
    weekly: object = None


def flag_value(text):
    """OFF, or a Decimal from 0 through 100 for ASCII digits with an optional fraction; else None."""
    if text == OFF:
        return OFF
    if not NUMBER.fullmatch(text):
        return None
    number = Decimal(text)
    return number if number <= 100 else None


def split_flags(args):
    args, found, index = list(args), {}, 0
    while index < len(args):
        word = args[index]
        window = FLAGS.get(word)
        if window is None:
            for flag in FLAGS:
                if word.startswith(flag + "="):
                    raise ReserveError(f"{flag}=VALUE is not accepted; write {flag} VALUE.")
            break
        if window in found:
            raise ReserveError(f"{word} is given twice.")
        found[window] = flag_value(args[index + 1]) if index + 1 < len(args) else None
        if found[window] is None:
            raise ReserveError(f"{word} takes a number from 0 through 100, or off, as the next argument.")
        index += 2
    rest = args[index:]
    if found and (not rest or rest[0].startswith("-")):
        raise ReserveError("an account name must follow --reserve-5h and --reserve-weekly.")
    return Overrides(**found), rest


# -- policy -------------------------------------------------------------------


@dataclass(frozen=True)
class Setting:
    window: str
    reserve: object
    source: str


@dataclass(frozen=True)
class Policy:
    tool: str
    account: str
    five_hour: Setting
    weekly: Setting

    def settings(self):
        return (self.five_hour, self.weekly)

    def enabled(self):
        return {setting.window: setting.reserve for setting in self.settings() if setting.reserve is not None}

    @property
    def off(self):
        return not self.enabled()

    def describe(self):
        parts = []
        for setting in self.settings():
            value = "off" if setting.reserve is None else percent(setting.reserve) + "%"
            parts.append(f"{LABELS[setting.window]} {value} ({self.source_name(setting)})")
        return f"usage reserves for {self.account}: " + ", ".join(parts)

    def source_name(self, setting):
        if setting.source == "invocation":
            return next(flag for flag, window in FLAGS.items() if window == setting.window)
        if setting.source == "account":
            return f"usage_reserve.{self.tool.split('-')[0]}_accounts.{self.account}"
        return "usage_reserve" if setting.source == "global" else "default"


def checked(value):
    """A layer's value as resolved: None for OFF, the Decimal itself when finite and 0..100."""
    if isinstance(value, str) and value == OFF:
        return None
    if isinstance(value, Decimal) and value.is_finite() and 0 <= value <= 100:
        return value
    raise ValueError("a reserve is a Decimal from 0 through 100 or \"off\"")


def resolve(tool, account, overrides, usage_reserve):
    if tool not in ("codex-as", "claude-as"):
        raise ValueError(f"unknown launcher {tool}")
    layers = (("invocation", overrides), ("account", usage_reserve.account(tool, account)),
              ("global", usage_reserve.defaults))
    settings = {}
    for window in WINDOWS:
        settings[window] = Setting(window, None, "default")
        for source, layer in layers:
            value = getattr(layer, window)
            if value is not None:
                settings[window] = Setting(window, checked(value), source)
                break
    return Policy(tool, account, **settings)


# -- evaluator ----------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """One window of one response. used: unrounded percent used. resets_at: wall-clock
    epoch seconds. name ("five_hour" or "weekly") or, without a name, duration (seconds)
    identifies the window. A reading with any other name (another quota family, such as
    a per-model weekly limit) is not ours whatever its duration, nor is one with neither
    or with another length; a window name with another length is a mismatch."""
    used: object
    resets_at: object = None
    name: object = None
    duration: object = None


@dataclass(frozen=True)
class Observation:
    """A NEW response. fresh_at: the evaluator clock when the gate received it (a cache
    re-read hands back the old value and changes nothing). wall_at: wall-clock epoch
    seconds at that moment. attributed: verified for the expected identity and quota
    family; never True while a reported fault persists, because a valid attributed
    response clears it. Submit only complete snapshots: a missing enabled window pauses."""
    readings: tuple
    fresh_at: float
    wall_at: float
    attributed: bool


@dataclass(frozen=True)
class WindowStatus:
    """An enabled window in the latest response. status: "ok", "at_reserve", or why it is
    unavailable: missing, malformed, out_of_range, reset_unknown, reset_anomaly,
    ambiguous (reported twice) or mismatch (name and duration disagree)."""
    window: str
    reserve: Decimal
    status: str
    remaining: object = None
    resets_at: object = None


@dataclass(frozen=True)
class Verdict:
    """decision: "check" (no model work yet), "run", "pause" or "refuse".
    reason: the condition that blocks now (None while running, or while paused once a
    fresh check passes and only a continuation is missing: eligible). cause: the
    condition that paused or refused. windows: WindowStatus per enabled window from the
    latest attributed response. last_fresh: wall-clock time of the last valid reading;
    age: seconds since it. failure: the latest failed read since the latest response.
    continuation: "armed" while a continuation waits for its check."""
    account: str
    state: str
    decision: str
    reason: object
    cause: object
    windows: tuple
    last_fresh: object
    age: object
    failure: object
    continuation: object
    eligible: bool

    def message(self, how=""):
        """One deterministic line; HOW (for example the continuation key) follows it."""
        word = {"run": "running", "check": "checking", "pause": "paused", "refuse": "refused"}[self.decision]
        if self.decision == "run":
            detail = ", ".join(f"{LABELS[w.window]} {above_reserve(w)}% remaining "
                               f"(reserve {percent(w.reserve)}%)" for w in self.windows)
        elif self.reason is None:
            detail = f"{cause_text(self.cause)}; the latest quota check passes; a continuation resumes"
        else:
            detail = self.explain(self.reason)
        return f"{self.account}: {word}: {detail}." + (f" {how}" if how else "")

    def explain(self, reason):
        last = f" (last error: {self.failure})" if self.failure else ""
        if reason == "threshold":
            return "; ".join(f"{LABELS[w.window]} window {percent(w.remaining, floor=True)}% remaining, "
                             f"at or below its {percent(w.reserve)}% reserve, resets {timestamp(w.resets_at)}"
                             for w in self.windows if w.status == AT_RESERVE)
        if reason == "stale":
            return f"no fresh quota reading for {int(self.age)} s, last at {timestamp(self.last_fresh)}{last}"
        if reason == "no_reading":
            return f"no quota reading yet{last}"
        if reason == "window_unavailable":
            text = "; ".join(f"{LABELS[w.window]} window unavailable ({w.status.replace('_', ' ')})"
                             for w in self.windows if w.status not in (OK, AT_RESERVE))
        elif reason == "unattributed":
            text = "the quota reading is not attributed to this account"
        else:
            text = CONTROL_TEXT.get(reason) or FAULT_TEXT.get(reason) or reason.replace("_", " ")
        if self.last_fresh is None:
            return text
        return f"{text}; last valid reading at {timestamp(self.last_fresh)}"


def cause_text(cause):
    return CAUSE_TEXT.get(cause) or CONTROL_TEXT.get(cause) or FAULT_TEXT.get(cause) or cause.replace("_", " ")


def percent(value, floor=False):
    """A Decimal percentage without trailing zeros; FLOOR rounds down to one decimal."""
    if floor:
        value = value.quantize(Decimal("0.1"), rounding=ROUND_FLOOR)
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def above_reserve(status):
    """A running window's remaining value as shown: rounded down to one decimal, or to as
    many more as it takes to stay above the reserve, so it is never above the value
    observed nor shown at the reserve; "over RESERVE" past SHOWN_DECIMALS decimals."""
    remaining, reserve = status.remaining, status.reserve
    if remaining > reserve:
        # The gap rounded down is positive, so 10**-decimals <= gap and rounding remaining
        # down to that many decimals stays above remaining - gap = reserve.
        decimals = max(1, -FLOOR.subtract(remaining, reserve).adjusted())
        if decimals <= SHOWN_DECIMALS:
            return percent(remaining.quantize(Decimal(f"1E-{decimals}"), ROUND_FLOOR, Context(prec=decimals + 3)))
    return "over " + percent(reserve)


def timestamp(seconds):
    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def number(value):
    """A finite Decimal for an int, float or Decimal (never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if isinstance(value, float):
        return Decimal(repr(value)) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    return Decimal(value)


def windows_of(reading):
    """{window: problem or None} for the enabled-or-not windows this reading claims."""
    if reading.name is not None and not (isinstance(reading.name, str) and reading.name in DURATIONS):
        return {}  # another family's or window's name: not ours, whatever its duration
    named, timed = reading.name, None
    if reading.duration is not None:
        seconds = number(reading.duration)
        timed = next((window for window, length in DURATIONS.items() if seconds == length), None)
        if named is not None and timed != named:
            return {window: "mismatch" for window in (named, timed) if window is not None}
    window = named or timed
    return {} if window is None else {window: None}


def judge(reading, window, reserve, wall_at):
    """WindowStatus of the one reading for WINDOW."""
    used = number(reading.used)
    if used is None:
        return WindowStatus(window, reserve, "malformed")
    if not 0 <= used <= 100:
        return WindowStatus(window, reserve, "out_of_range")
    if reading.resets_at is None:
        return WindowStatus(window, reserve, "reset_unknown")
    reset = number(reading.resets_at)
    if reset is None:
        return WindowStatus(window, reserve, "malformed")
    wall = Decimal(repr(float(wall_at)))
    if reset <= wall - Decimal(repr(RESET_SKEW)) or reset > wall + DURATIONS[window] + Decimal(repr(RESET_SKEW)):
        return WindowStatus(window, reserve, "reset_anomaly")
    # remaining <= reserve is used + reserve >= 100; rounding down keeps that comparison
    # exact at any precision, and the remaining value shown is never above the true one.
    at_reserve = FLOOR.add(used, reserve) >= 100
    remaining = FLOOR.subtract(Decimal(100), used)
    return WindowStatus(window, reserve, AT_RESERVE if at_reserve else OK, remaining, reading.resets_at)


def reason_word(reason, reserved=frozenset()):
    if not isinstance(reason, str) or not REASON.fullmatch(reason):
        raise ValueError("a reason is a lower-case identifier of at most 64 characters")
    if reason in reserved:
        raise ValueError(f"{reason} names another kind of condition")
    return reason


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


class Evaluator:
    def __init__(self, policy, clock, *, paused=None, max_age=MAX_AGE, continuation_window=CONTINUATION_WINDOW):
        self.reserves = policy.enabled()
        if not self.reserves:
            raise ValueError("no enabled reserve: an unprotected launch needs no evaluator")
        for name, seconds in (("max_age", max_age), ("continuation_window", continuation_window)):
            if not finite(seconds) or seconds <= 0:
                raise ValueError(f"{name} is a finite number of seconds above 0")
        self.account, self.clock = policy.account, clock
        self.max_age, self.continuation_window = max_age, continuation_window
        self.state, self.cause, self.refused = CHECKING, None, None
        if paused is not None:
            self.state, self.cause = PAUSED, reason_word(paused)
        self.control = "control_pending"
        self.fault_reason, self.fault_at = None, None
        self.latest_at = None        # receipt time of the latest response taken
        self.problem = None          # None, "unattributed" or "window_unavailable" for it
        self.windows = ()            # its WindowStatus per enabled window
        self.last_fresh = None       # receipt time of the latest valid response ...
        self.last_fresh_wall = None  # ... and its wall-clock time
        self.failure_reason, self.failures = None, 0
        self.armed = None            # when a continuation was requested
        self.paused_polls = 0

    # -- inputs -------------------------------------------------------------

    def observe(self, observation):
        if not isinstance(observation, Observation):
            raise TypeError("observe() takes an Observation")
        now = self._tick()
        fresh_at = observation.fresh_at
        if not (finite(fresh_at) and finite(observation.wall_at) and 0 <= observation.wall_at <= LATEST_WALL
                and isinstance(observation.attributed, bool) and isinstance(observation.readings, (tuple, list))
                and all(isinstance(reading, Reading) for reading in observation.readings)):
            return self._failed("invalid_observation", now, now)
        if fresh_at > now:
            return self._failed("invalid_time", now, now)
        if self.latest_at is not None and fresh_at <= self.latest_at:
            return self._verdict(now)  # a repeat or an older response: freshness is unchanged
        if now - fresh_at > self.max_age:
            return self._failed("stale_response", now, fresh_at)
        answered = self._answers(fresh_at)
        self._count_paused_poll(answered)
        self.latest_at, self.failure_reason, self.failures = fresh_at, None, 0
        if not observation.attributed:
            self.problem, self.windows = "unattributed", ()
        else:
            self.windows = self._judge(observation)
            if any(status.status not in (OK, AT_RESERVE) for status in self.windows):
                self.problem = "window_unavailable"
            else:
                self.problem = None
                self.last_fresh, self.last_fresh_wall = fresh_at, observation.wall_at
                if self.fault_reason is not None and fresh_at > self.fault_at and self.state != CHECKING:
                    self.fault_reason = self.fault_at = None
        self._update(now, resume=answered)
        return self._verdict(now)

    def failure(self, reason, at=None):
        reason = reason_word(reason)
        now = self._tick()
        return self._failed(reason, now, now if at is None or not finite(at) else min(at, now))

    def fault(self, reason):
        reason = reason_word(reason, OWN_CONDITIONS | CONTROL_TEXT.keys())
        now = self._tick()
        self.fault_reason, self.fault_at, self.armed = reason, now, None
        self._update(now)
        return self._verdict(now)

    def control_down(self, reason):
        reason = reason_word(reason, OWN_CONDITIONS | FAULT_TEXT.keys())
        now = self._tick()
        if self.control in (None, "control_pending"):
            self.control = reason  # final: the first loss stays for this evaluator
        self.armed = None
        self._update(now)
        return self._verdict(now)

    def control_up(self):
        now = self._tick()
        if self.control == "control_pending":
            self.control = None  # a loss is never cleared here
        self._update(now)
        return self._verdict(now)

    def continuation(self):
        now = self._tick()
        if self.state == PAUSED and self.control is None:
            self.armed = now
        return self._verdict(now)

    def give_up(self):
        if self.state != CHECKING:
            raise ValueError("only a start that is still checking can be given up")
        now = self._tick()
        if self.refused is None and self.state == CHECKING:
            self.refused = self.cause = self._condition(now) or "no_reading"
        return self._verdict(now)

    def tick(self):
        return self._verdict(self._tick())

    def verdict(self):
        return self.tick()

    def admits(self):
        self._tick()
        return self.state == RUNNING

    def next_poll(self, jitter=None):
        """Seconds until the next quota read. JITTER(low, high) picks the backoff; by
        default the upper bound. Paused reads are rate-bounded, never stopped."""
        now = self._tick()
        if self.state == PAUSED:
            if self.armed is not None:
                return 0.0
            return min(POLL_PAUSED_MAX, POLL_PAUSED * 2 ** min(self.paused_polls, 8))
        if self.failures:
            cap = min(BACKOFF_MAX, BACKOFF_MIN * 2 ** min(self.failures - 1, 8))
            chosen = cap if jitter is None else jitter(BACKOFF_MIN, cap)
            return max(BACKOFF_MIN, min(cap, chosen))
        if self.state == CHECKING and self.refused is None and (self.last_fresh is None or self._stale(now)):
            return 0.0  # no usable reading yet
        near = any(status.remaining is not None and status.remaining - status.reserve <= NEAR_POINTS
                   for status in self.windows)
        return POLL_NEAR if near else POLL_RUNNING

    # -- internals ----------------------------------------------------------

    def _judge(self, observation):
        claims = {window: [] for window in self.reserves}
        for reading in observation.readings:
            for window, problem in windows_of(reading).items():
                if window in claims:
                    claims[window].append((reading, problem))
        statuses = []
        for window in WINDOWS:
            if window not in claims:
                continue
            reserve, found = self.reserves[window], claims[window]
            if not found:
                statuses.append(WindowStatus(window, reserve, "missing"))
            elif any(problem for _, problem in found):
                statuses.append(WindowStatus(window, reserve, "mismatch"))
            elif len(found) > 1:
                statuses.append(WindowStatus(window, reserve, "ambiguous"))
            else:
                statuses.append(judge(found[0][0], window, reserve, observation.wall_at))
        return tuple(statuses)

    def _failed(self, reason, now, at):
        answered = self._answers(at)
        self._count_paused_poll(answered)
        self.failure_reason, self.failures = reason, self.failures + 1
        self._update(now, resume=False)
        return self._verdict(now)

    def _answers(self, at):
        """Whether something received at AT answers the armed continuation (using it up)."""
        if self.armed is None or not finite(at) or at <= self.armed:
            return False
        if at - self.armed > self.continuation_window:
            self.armed = None
            return False
        self.armed = None
        return True

    def _count_paused_poll(self, answered):
        if self.state == PAUSED and self.armed is None and not answered:
            self.paused_polls += 1

    def _condition(self, now):
        """What blocks model work now. Definitive conditions come before the WAITING ones,
        so a start refuses whatever order its events arrive in. (Stale precedes threshold:
        a threshold decides as its response arrives, and an old one is shown as stale.)"""
        if self.control not in (None, "control_pending"):
            return self.control
        if self.fault_reason is not None:
            return self.fault_reason
        if self.problem is not None:
            return self.problem
        if self.last_fresh is not None and self._stale(now):
            return "stale"
        if any(status.status == AT_RESERVE for status in self.windows):
            return "threshold"
        if self.control is not None:
            return self.control
        return "no_reading" if self.latest_at is None else None

    def _stale(self, now):
        return now - self.last_fresh > self.max_age

    def _update(self, now, resume=False):
        condition = self._condition(now)
        if self.state == CHECKING:
            if self.refused is not None:
                return
            if condition is None:
                self.state = RUNNING
            elif condition not in WAITING:
                self.refused = self.cause = condition
        elif self.state == RUNNING:
            if condition is not None:
                self.state, self.cause, self.armed, self.paused_polls = PAUSED, condition, None, 0
        elif resume and condition is None:
            self.state, self.cause, self.paused_polls = RUNNING, None, 0

    def _tick(self):
        now = self.clock()
        if self.armed is not None and now - self.armed > self.continuation_window:
            self.armed = None
        self._update(now)
        return now

    def _verdict(self, now):
        condition = self._condition(now)
        decision = {RUNNING: "run", PAUSED: "pause"}.get(self.state, "refuse" if self.refused else "check")
        reason = self.refused if self.refused is not None else (None if self.state == RUNNING else condition)
        age = None if self.last_fresh is None else now - self.last_fresh
        return Verdict(self.account, self.state, decision, reason, self.cause, self.windows, self.last_fresh_wall,
                       age, self.failure_reason, "armed" if self.armed is not None else None,
                       self.state == PAUSED and condition is None)
