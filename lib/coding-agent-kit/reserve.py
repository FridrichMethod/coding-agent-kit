"""coding-agent-kit usage reserves: launch flags, per-window policy, the guard's decision and formatting.

Pure: no I/O, no environment, no clock of its own. Load this file by path (it is
never on sys.path), like config.py; it does not import config.py and reads the
configuration only from the frozen Config the launcher passes in. The reserve
guard (guard.py) takes its decision and its messages from here.

Flags (before NAME, either order, exact words, the value as the next argument):

    split_flags(args) -> (Overrides, rest)
        Takes leading --reserve-5h V and --reserve-weekly V off the launcher's
        arguments. V is "off" or ASCII decimal digits with an optional fraction,
        from 0 through 100. A repeated flag, a missing or invalid value, the
        --flag=V form, or no account name after the flags raise ReserveError.
        Without reserve flags the arguments come back unchanged, as a new list.
    flag_value(text) -> OFF, a Decimal from 0 through 100, or None for anything else.
    Overrides(five_hour=None, weekly=None): the launch-only layer; never persisted.
    ReserveError(ValueError): one line without the "<tool>: " prefix, never
        quoting the argument; own_message is True.

Policy (each window on its own, never combined):

    resolve(tool, account, overrides, usage_reserve) -> Policy
        tool is "codex-as" or "claude-as"; usage_reserve is Config.usage_reserve.
        Per window: invocation, then [usage_reserve.<tool>_accounts.NAME], then
        [usage_reserve], then off. The first layer that sets the window wins,
        "off" included, so an explicit "off" overrides an inherited percentage.
        Each value is checked again (checked()): ValueError when it is neither
        OFF nor a finite Decimal from 0 through 100.
    Policy(tool, account, five_hour, weekly): Setting per window
        settings() -> (Setting, Setting); enabled() -> {window: Decimal}
        off: True when both windows are off; the launch is then unchanged.
        describe() -> the effective policy line, each window with its source.
    Setting(window, reserve, source): reserve is a Decimal from 0 through 100 or
        None (off); source is one of SOURCES: "invocation", "account", "global"
        or "default".

Decision (the same for both natives; the caller gathers the readings):

    decide(enabled, readings, now, blind=False) -> Decision
        enabled is Policy.enabled(); readings is any iterable of Reading values,
        read once. Per enabled window, in WINDOWS order, only current readings
        count: a finite percent used of at least 0 (above 100 counts as 100)
        whose reset time is None or after NOW (wall-clock epoch seconds); usage
        never falls inside a window, so a reading stays current until its reset
        however old it is. The most used one decides: the window is at its
        reserve when used + reserve >= 100, compared exactly. Any window at its reserve blocks with reason
        "threshold" (blind or not); else BLIND blocks with reason "blind"; else
        the decision allows. A window without a current reading has nothing to
        check. ValueError when NOW is not a number or a reserve is not one that
        checked() accepts.
    Reading(window, used, resets_at, family=None): one window's reading.
    WindowStatus(window, reserve, status, remaining, resets_at, family=None):
        status is OK or AT_RESERVE; remaining is 100 - used, rounded down.
    Decision(allow, reason, windows=()): reason is None, "threshold" or "blind";
        windows are the WindowStatus of the windows at their reserve.

Messages (tool is "claude-as" or "codex-as"; event is "prompt", "tool" or "stop"):

    block_text(tool, account, decision, event, now) -> the message of a blocking
        decision: each window at its reserve with its reset, or the blindness
        cause, what was stopped, and the relaunch that goes on.
    failure_text(tool, account, event, name) -> the message of a guard that
        failed, NAME being the exception's class name or "launch format".
    relaunch(tool, account, windows) -> the command that relaunches with the
        reserve of each of WINDOWS off and continues the session.
    span(seconds) -> "45m", "3h 5m" or "2d 3h", rounded down; never negative.
    Each raises ValueError for an unknown tool or event, or a decision that allows.

Formatting:

    percent(value, floor=False) -> str: a Decimal percentage without trailing
        zeros; floor rounds it down to one decimal first.
    timestamp(seconds) -> str: epoch seconds as UTC ISO 8601 ("...T...Z").
    number(value) -> Decimal | None: a finite Decimal for an int, float or
        Decimal (never a bool), else None.

Constants: WINDOWS ("five_hour", "weekly"), LABELS (their names in messages),
DURATIONS (their lengths in seconds), FLAGS (flag to window), OFF, SOURCES,
NUMBER (the flag value pattern), FLOOR (a Decimal context that rounds down),
OK and AT_RESERVE (window statuses), TAILS (what each launcher appends to
continue the session), STOPPED (what an event's block stopped) and CAUSES (why
each launcher's guard can be blind).
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
NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")
FLOOR = Context(rounding=ROUND_FLOOR)  # rounds down, so a sum compared with 100 keeps its order
OK, AT_RESERVE = "ok", "at_reserve"
TAILS = {"claude-as": "--continue", "codex-as": "resume --last"}
STOPPED = {
    "prompt": "This prompt was not sent.",
    "tool": "This tool call was stopped. Stop working now and tell the user why.",
    "stop": "The work stops here.",
}
CAUSES = {
    "claude-as": "the status line has reported no rate limits: the workspace is not trusted yet, "
                 "or this account reports none",
    "codex-as": "the session's rollout shows no rate limits",
}


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


# -- decision -----------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    window: str  # "five_hour" or "weekly"
    used: object  # percent used as read: int, float or Decimal
    resets_at: object  # wall-clock epoch seconds, or None for a reading the caller vouches is current
    family: object = None  # a limit family other than the general one, else None


@dataclass(frozen=True)
class WindowStatus:
    window: str
    reserve: Decimal
    status: str  # OK or AT_RESERVE
    remaining: Decimal
    resets_at: object
    family: object = None


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: object  # None, "threshold" or "blind"
    windows: tuple = ()  # WindowStatus of the enabled windows at their reserve, in WINDOWS order


def current(reading, now):
    """READING's percent used as a Decimal from 0 through 100 while it is current, else None."""
    used = number(reading.used)
    if used is None or used < 0:
        return None
    if reading.resets_at is not None:
        reset = number(reading.resets_at)
        if reset is None or reset <= now:
            return None
    return min(used, Decimal(100))


def decide(enabled, readings, now, blind=False):
    now = number(now)
    if now is None:
        raise ValueError("now is a number of epoch seconds")
    readings = tuple(readings)  # each window reads them all: an iterator would serve only the first
    at_reserve = []
    for window in WINDOWS:
        if enabled.get(window) is None:
            continue
        reserve = checked(enabled[window])
        found = [(used, reading) for reading in readings if reading.window == window
                 for used in (current(reading, now),) if used is not None]
        if not found:
            continue  # nothing to check: the 0.8 rule for a window the report does not name
        used, reading = max(found, key=lambda pair: pair[0])  # the most used; the first of equals
        # remaining <= reserve is used + reserve >= 100; rounding down keeps that comparison
        # exact at any precision, and the remaining value shown is never above the true one.
        if FLOOR.add(used, reserve) >= 100:
            remaining = FLOOR.subtract(Decimal(100), used) or Decimal(0)  # never a negative zero
            at_reserve.append(WindowStatus(window, reserve, AT_RESERVE, remaining, reading.resets_at, reading.family))
    if at_reserve:
        return Decision(False, "threshold", tuple(at_reserve))
    return Decision(False, "blind") if blind else Decision(True, None)


# -- messages -----------------------------------------------------------------


def span(seconds):
    days, rest = divmod(max(0, int(seconds)), 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def relaunch(tool, account, windows):
    if tool not in TAILS:
        raise ValueError(f"unknown launcher {tool}")
    flags = [f"{flag} {OFF}" for flag, window in FLAGS.items() if window in windows]
    if not flags:
        raise ValueError("a relaunch turns at least one reserve off")
    return " ".join([tool, *flags, account, TAILS[tool]])


def stopped(tool, event):
    """What a block on EVENT stopped; ValueError for an unknown launcher or event."""
    if tool not in TAILS:
        raise ValueError(f"unknown launcher {tool}")
    if event not in STOPPED:
        raise ValueError(f"unknown event {event}")
    return STOPPED[event]


def sentence(status, now):
    """One window at its reserve: remaining, reserve, the reset when known, a family other than the general one."""
    text = (f"{LABELS[status.window]} window {percent(status.remaining, floor=True)}% remaining, "
            f"at or below its {percent(status.reserve)}% reserve")
    reset = None if status.resets_at is None else number(status.resets_at)
    if reset is not None:
        text += f"; resets in {span(reset - now)} ({timestamp(reset)})"
    if status.family is not None:
        text += f" (limit {status.family})"
    return text + "."


def block_text(tool, account, decision, event, now):
    what = stopped(tool, event)
    if decision.allow:
        raise ValueError("an allowing decision has no block message")
    if decision.reason == "threshold":
        now = number(now)
        return "\n".join((f"{tool}: {account}: " + " ".join(sentence(status, now) for status in decision.windows),
                          what, "To use the reserve, exit and run:",
                          "  " + relaunch(tool, account, [status.window for status in decision.windows])))
    if decision.reason == "blind":
        return "\n".join((f"{tool}: {account}: no usage data since this session started, so its usage reserve "
                          f"cannot be checked ({CAUSES[tool]}).", what, "To go on without reserves, exit and run:",
                          "  " + relaunch(tool, account, WINDOWS),
                          f'To keep them off for this account, set them to "{OFF}" under '
                          f"[usage_reserve.{tool.split('-')[0]}_accounts.{account}]."))
    raise ValueError(f"unknown reason {decision.reason}")


def failure_text(tool, account, event, name):
    return "\n".join((f"{tool}: {account}: the usage reserve guard failed ({name}).", stopped(tool, event),
                      "To go on without reserves, exit and run:", "  " + relaunch(tool, account, WINDOWS)))


# -- formatting ---------------------------------------------------------------


def percent(value, floor=False):
    """A Decimal percentage without trailing zeros; FLOOR rounds down to one decimal."""
    if floor:
        value = value.quantize(Decimal("0.1"), rounding=ROUND_FLOOR)
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


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
