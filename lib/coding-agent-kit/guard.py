"""coding-agent-kit usage reserve guard: the hook command of a launch with a usage reserve.

The launchers inject it per launch, only when a reserve is on: claude-as as the
command of Claude Code's SessionStart, UserPromptSubmit, PreToolUse and Stop
hooks and as its status line command, codex-as as the command of Codex's
UserPromptSubmit and PreToolUse hooks:

    "$CODING_AGENT_KIT_GUARD_PYTHON" -I -B -S "$CODING_AGENT_KIT_GUARD_SCRIPT" KIND

KIND is start, prompt, tool, stop or status. One short process per event: it
reads the event on stdin and the launch from the environment, reads the usage
data the native already has, decides with reserve.decide() and answers in the
native's hook contract. It loads reserve.py and accounts.py beside it, and for
codex-as ../codex-as/usage.py, by path; it reads no configuration file and no
other environment variable: every path comes from the launch.

The launch (CODING_AGENT_KIT_GUARD, compact JSON the launcher writes):

    {"v": 1, "tool": "claude-as" | "codex-as", "account": NAME,
     "five_hour": PERCENT | null, "weekly": PERCENT | null, "started": EPOCH,
     "usage": ABSOLUTE PATH, "state": ABSOLUTE PREFIX,
     "marker": ABSOLUTE PATH (codex-as), "statusline": COMMAND (claude-as, optional)}

    PERCENT is a reserve as reserve.percent() writes it ("10", "12.5"); null is
    off. usage is the Claude usage file or the Codex account record; state is
    this launch's prefix in the guard directory. Without the variable the guard
    allows and prints nothing. Any other "v", or a value that does not parse as
    above, is a guard failure named "launch format" (unknown keys are ignored).

State files beside the prefix, each created once, owned and 0600:

    STATE.ran.KIND  at each event of KIND: the native runs that hook in this launch
    STATE.reply     Claude, at the first tool or stop event: that moment in epoch
                    seconds (the content counts, never the file's time)
    STATE.seen      Claude, by the status line: rate limits reached this launch
    STATE.blocked   the latest block message, replaced at each block
    MARKER          Codex, at a tool event: the tool hook has run on this host

The decision (reserve.decide) per launch:

- claude-as reads the account's usage file (accounts.read; a window needs a
  reset time; an unusable file is no reading). The launch is blind when the
  moment in STATE.reply is at least BLIND_AFTER seconds ago and STATE.seen does
  not exist: the session has had a reply and its status line has reported no
  rate limits since.
- codex-as reads the session's rollout (usage.session on the event's
  transcript_path, since the launch started): its newest snapshot, each window
  named by its length (300 minutes five_hour, 10080 weekly, others ignored)
  with the snapshot's limit family; without one, the account record's usage
  from earlier runs, minus windows without a reset time. The launch is blind
  without a snapshot when the rollout has a token_count line (accounted), and
  on a prompt also when it shows a model response. No readable transcript is
  never blind.

Answers, as (exit code, stdout, stderr); a message goes to stderr with one newline:

    claude-as prompt  allow (0, "", ""); block (2, "", MESSAGE)
    claude-as tool    allow (0, "", ""); block with STOP_TURN: (0, {"continue": false,
                      "stopReason", "hookSpecificOutput": PreToolUse deny}, ""),
                      else (2, "", MESSAGE)
    claude-as stop    allow (0, "", ""); block with END_AT_STOP: (0, {"continue": false,
                      "stopReason"}, ""), else (0, "", "")
    codex-as prompt, tool  allow (0, "", ""); block (2, "", MESSAGE)
    start, codex-as stop   (0, "", ""): only STATE.ran.KIND

The status line (claude-as, status): Claude Code passes it the rate limits it
derives from its replies, and no hook input carries them, so the guard is the
launch's status line. With a valid window (a finite percentage from 0 through
100 and a whole reset time) it merges the windows into the account's usage file,
the file and format helpers/statusline.cjs writes ({"five_hour", "seven_day":
{"used_percentage", "resets_at"} | null, "seen_at"}), through a 0600 temporary
file and a rename, only in an owned 0700 directory, and then creates STATE.seen.
A reading never goes back: a stored window that has not reset stays when the
incoming one has the same reset and a lower percentage, an earlier reset, or is
absent; unchanged windows are rewritten only RECORD_AGAIN seconds apart. Then it
runs the user's own status line command (the launch's "statusline") through
/bin/sh -c with the same input and the caller's environment, and prints its
output unchanged as the footer: nothing when it fails, prints nothing or runs
longer than STATUS_TIMEOUT seconds. When the input carries rate limits (the
session had a reply) and no STATE.ran.* exists, the first footer line is
WARNING: the native has not run the injected hooks.

Any error on a prompt or tool event (an event that is not a JSON object, a
launch format, an unwritable state file, a missing module, an exception) is a
block with reserve.failure_text() and the error's class name, in the block shape
above. On start, stop and status every error exits 0 without output. A Python
older than 3.11 blocks with one line on prompt and tool, and is silent elsewhere.

Public API: Launch, launch_of(env), answer(kind, event, launch, now),
record(path, limits, now) (True when it wrote), status(raw, launch, now) (the
footer), handle(kind, raw, env, now) (an event's answer from its raw stdin;
never raises), main(argv=None). Constants: STOP_TURN and END_AT_STOP (probe
results: a PreToolUse continue false ends the turn, a Stop one ends it beside
another Stop hook), BLIND_AFTER, RECORD_AGAIN and STATUS_TIMEOUT (seconds),
WARNING, VARIABLE.
"""

from dataclasses import dataclass
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time


VARIABLE = "CODING_AGENT_KIT_GUARD"
STOP_TURN = True  # a PreToolUse answer of continue false ends the turn, not the process
END_AT_STOP = True  # a Stop answer of continue false ends the turn beside a Stop hook that blocks
BLIND_AFTER = 120  # seconds after the moment in STATE.reply
RECORD_AGAIN = 300  # unchanged readings are rewritten only this many seconds apart
TOOLS = ("claude-as", "codex-as")
KINDS = ("start", "prompt", "tool", "stop")
QUIET = ("start", "stop", "status")  # events that never block and never print an error
USAGE_KEYS = {"five_hour": "five_hour", "seven_day": "weekly"}  # Claude usage file key: window
MINUTES = {300: "five_hour", 10080: "weekly"}  # Codex window length: window
ACCOUNT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")
LAST = 253402300800  # 10000-01-01 UTC: no calendar time from here on
LAUNCH_FORMAT = "launch format"
OK = (0, "", "")
STATUS_TIMEOUT = 5  # seconds the user's own status line command may take
WARNING = "reserve guard not running"  # the first footer line after a reply when no hook has run
USAGE_FILE = re.compile(r"claude-[A-Za-z0-9_][A-Za-z0-9_-]*\.usage\.json")  # the name the status line helper writes
STALE = 60  # seconds after which a temporary file of a cancelled status line run is removed

# python3 -I puts no script directory on sys.path: the siblings load by path from
# the real file, so a guard reached through a link uses the kit it belongs to.
LIB = Path(os.path.realpath(__file__)).parent.parent
PATHS = {"reserve": "coding-agent-kit/reserve.py", "accounts": "coding-agent-kit/accounts.py",
         "usage": "codex-as/usage.py"}
MODULES = {}


def module(name):
    """The sibling module NAME, loaded once; its loading errors reach the caller."""
    if name not in MODULES:
        spec = importlib.util.spec_from_file_location("coding_agent_kit_guard_" + name, LIB / PATHS[name])
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        MODULES[name] = loaded
    return MODULES[name]


# -- the launch ------------------------------------------------------------------


@dataclass(frozen=True)
class Launch:
    tool: str
    account: str
    enabled: dict  # window: Decimal reserve, only the windows that are on
    started: int
    usage: str
    state: str
    marker: object = None
    statusline: object = None


class LaunchError(ValueError):
    """A launch the guard cannot read; TOOL and ACCOUNT are kept when they could be read."""

    def __init__(self, message, tool=None, account=None):
        super().__init__(message)
        self.tool, self.account = tool, account


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def finite(_):
    raise ValueError("non-finite number")


def absolute(value):
    return value if isinstance(value, str) and "\0" not in value and os.path.isabs(value) else None


def launch_of(env):
    if VARIABLE not in env:
        return None
    try:
        data = json.loads(env[VARIABLE], object_pairs_hook=unique, parse_constant=finite)
    except (ValueError, RecursionError):
        raise LaunchError(f"{VARIABLE} is not a JSON object") from None
    if not isinstance(data, dict):
        raise LaunchError(f"{VARIABLE} is not a JSON object")
    tool = data.get("tool") if data.get("tool") in TOOLS else None
    account = data.get("account")
    account = account if isinstance(account, str) and ACCOUNT.fullmatch(account) else None

    def bad(field):
        return LaunchError(f"{VARIABLE}: no usable {field}", tool, account)

    version = data.get("v")
    if type(version) is not int or version != 1:
        raise bad("version 1")
    if tool is None:
        raise bad("tool")
    if account is None:
        raise bad("account")
    enabled = {}
    for window in ("five_hour", "weekly"):
        if window not in data:
            raise bad(window)
        if data[window] is None:
            continue
        value = module("reserve").flag_value(data[window]) if isinstance(data[window], str) else None
        if value is None or value == module("reserve").OFF:
            raise bad(window)
        enabled[window] = value
    started = data.get("started")
    if type(started) is not int or not 0 < started < LAST:
        raise bad("started")
    usage, state = absolute(data.get("usage")), absolute(data.get("state"))
    if usage is None:
        raise bad("usage")
    if state is None:
        raise bad("state")
    marker = data.get("marker")
    if (tool == "codex-as" or marker is not None) and absolute(marker) is None:
        raise bad("marker")
    statusline = data.get("statusline")
    if statusline is not None and not isinstance(statusline, str):
        raise bad("statusline")
    return Launch(tool, account, enabled, started, usage, state, marker, statusline)


# -- state files -----------------------------------------------------------------


def mark(path, content=""):
    """Create PATH as an owned 0600 file holding CONTENT unless it exists; True when this call created it."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return True


def note(launch, text):
    """Best effort: TEXT as the launch's latest block message."""
    path = launch.state + ".blocked"
    temporary = f"{path}.{os.urandom(6).hex()}.tmp"
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600),
                       "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def ran(launch):
    """Whether any hook of the launch has run."""
    return any(os.path.lexists(launch.state + ".ran." + kind) for kind in KINDS)


def stored(path):
    """The metadata file at PATH as accounts.read() reads it, or None when it is missing or unusable."""
    accounts = module("accounts")  # an incomplete install fails here, outside the best effort below
    try:
        return accounts.read(Path(path))
    except (OSError, ValueError):
        return None


# -- Claude Code ------------------------------------------------------------------


def claude_readings(launch):
    """The windows of the account's usage file that have a reset time."""
    data, readings = stored(launch.usage), []
    for key, window in USAGE_KEYS.items():
        value = data.get(key) if isinstance(data, dict) else None
        resets = value.get("resets_at") if isinstance(value, dict) else None
        if type(resets) is int and 0 < resets < LAST:
            readings.append(module("reserve").Reading(window, value.get("used_percentage"), resets))
    return readings


def claude_blind(launch, now):
    """A reply BLIND_AFTER seconds ago or more, and no rate limits on this launch's status line since."""
    try:
        fd = os.open(launch.state + ".reply", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as stream:
        content = stream.read(64).strip()
    moment = int(content) if re.fullmatch(rb"[0-9]{1,15}", content) else now  # unreadable: created now
    return now - moment >= BLIND_AFTER and not os.path.lexists(launch.state + ".seen")


# -- Codex ------------------------------------------------------------------------


def windows(slots, family=None, reset_required=False):
    """Readings of the windows in SLOTS ({"primary", "secondary"}) that have a known length."""
    readings, usage = [], module("usage")
    for key in ("primary", "secondary"):
        try:
            value = usage.window(slots.get(key))
        except ValueError:
            continue
        window = MINUTES.get(value["window_minutes"]) if value else None
        if window and not (reset_required and value["resets_at"] is None):
            readings.append(module("reserve").Reading(window, value["used_percent"], value["resets_at"], family))
    return readings


def codex_view(kind, event, launch):
    """(readings, blind) from the session's rollout, else from the account record."""
    usage = module("usage")  # loaded whatever the event says, so an incomplete install always fails
    if not isinstance(event, dict):
        raise ValueError("the event is not a JSON object")
    transcript = event.get("transcript_path")
    if transcript is not None and not isinstance(transcript, str):
        raise ValueError("transcript_path is not a string")
    seen = usage.session(transcript, launch.started) if transcript else None
    if seen and seen["snapshot"]:
        return windows(seen["snapshot"], seen["family"]), False
    record = stored(launch.usage)
    last = record.get("usage") if isinstance(record, dict) else None
    readings = windows(last, reset_required=True) if isinstance(last, dict) else []
    return readings, bool(seen) and (seen["accounted"] or kind == "prompt" and seen["responded"])


# -- the status line ----------------------------------------------------------------


def entry(value):
    """A rate-limit window as the status line helper keeps one, or None: a finite percentage from 0
    through 100 and a positive whole reset time before the year 10000."""
    if not isinstance(value, dict):
        return None
    used, resets = value.get("used_percentage"), value.get("resets_at")
    if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
        return None
    if type(resets) is float and resets.is_integer():
        resets = int(resets)
    if type(resets) is not int or not 0 < resets < LAST:
        return None
    return {"used_percentage": used, "resets_at": resets}


def merge(kept, incoming, now):
    """The window to store: a reading never goes back while the stored window has not reset."""
    if kept is None or kept["resets_at"] <= now:
        return incoming
    if incoming is None or incoming["resets_at"] < kept["resets_at"]:
        return kept
    if incoming["resets_at"] == kept["resets_at"] and incoming["used_percentage"] < kept["used_percentage"]:
        return kept
    return incoming


def sweep(path, now):
    """Best effort: remove temporary files of PATH that a cancelled run left more than STALE seconds ago."""
    prefix = "." + path.name + "."
    try:
        names = os.listdir(path.parent)
    except OSError:
        return
    for name in names:
        if name.startswith(prefix) and name.endswith(".tmp"):
            try:
                info = os.lstat(path.parent / name)
                if stat.S_ISREG(info.st_mode) and now - info.st_mtime > STALE:
                    os.unlink(path.parent / name)
            except OSError:
                pass


def record(path, limits, now):
    """Merge the status line's rate limits into the account's usage file; True when it wrote.

    Nothing is written without a valid window, or when the merged windows equal the stored
    ones and were stored less than RECORD_AGAIN seconds ago. ValueError (or OSError) when
    PATH is not an absolute claude-NAME.usage.json in an owned, real 0700 directory, or the
    write fails."""
    incoming = {key: entry(limits.get(key)) for key in USAGE_KEYS} if isinstance(limits, dict) else {}
    if not any(incoming.values()):
        return False
    path = Path(path)
    if not path.is_absolute() or not USAGE_FILE.fullmatch(path.name):
        raise ValueError(f"{path}: not a Claude usage file")
    folder = os.lstat(path.parent)
    if not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.getuid() or stat.S_IMODE(folder.st_mode) != 0o700:
        raise ValueError(f"{path.parent}: not an owned, real directory with permissions 0700")
    old = stored(path) or {}
    kept = {key: entry(old.get(key)) for key in USAGE_KEYS}
    merged = {key: merge(kept[key], incoming[key], now) for key in USAGE_KEYS}
    seen, last = int(now), old.get("seen_at")
    if merged == kept and type(last) is int and 0 <= seen - last < RECORD_AGAIN:
        return False
    sweep(path, now)
    return module("accounts").write(path, {**merged, "seen_at": seen})


def chained(command, raw):
    """The output of the user's own status line COMMAND given RAW, or "" when it fails, prints
    nothing or runs longer than STATUS_TIMEOUT seconds (its whole process group is killed)."""
    if not command:
        return ""
    import signal  # only the status line runs a command: the hook events stay light
    import subprocess
    try:
        child = subprocess.Popen(["/bin/sh", "-c", command], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, process_group=0)
    except (OSError, ValueError):
        return ""
    try:
        out, _ = child.communicate(raw, timeout=STATUS_TIMEOUT)
    except Exception:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
        child.wait()
        child.stdout.close()
        return ""
    return out.decode("utf-8", "surrogateescape") if child.returncode == 0 else ""


def status(raw, launch, now):
    """The footer of a Claude Code status line run whose input is RAW (bytes).

    With a valid rate-limit window it records the account's usage (record) and, once that
    holds the data, creates STATE.seen; when no hook of the launch has run yet, the first
    line is WARNING. Then the user's own status line command runs on the same input. Never
    raises and never blocks."""
    raw = raw if isinstance(raw, bytes) else b""
    lines = []
    if launch.tool == "claude-as":
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        limits = data.get("rate_limits") if isinstance(data, dict) else None
        if isinstance(limits, dict) and any(entry(limits.get(key)) for key in USAGE_KEYS):
            try:
                record(launch.usage, limits, now)
                mark(launch.state + ".seen")
            except Exception:
                pass
            if not ran(launch):
                lines.append(WARNING)
    footer = chained(launch.statusline, raw)
    return "\n".join(lines + [footer]) if footer else "\n".join(lines)


# -- answers ----------------------------------------------------------------------


def blocked(kind, tool, text):
    """A block of KIND in TOOL's hook contract."""
    if tool == "claude-as" and kind == "tool" and STOP_TURN:
        return 0, json.dumps({"continue": False, "stopReason": text,
                              "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": text}}), ""
    if kind == "stop":
        return (0, json.dumps({"continue": False, "stopReason": text}), "") if tool == "claude-as" and END_AT_STOP else OK
    return 2, "", text


def failure_text(tool, account, kind, name):
    try:
        return module("reserve").failure_text(tool, account, kind, name)
    except Exception:
        who = f"{tool}: {account}: " if tool in TOOLS and account else "coding-agent-kit: "
        return f"{who}the usage reserve guard failed ({name})."


def failed(kind, tool, account, name, launch=None):
    """The answer of a guard that failed: a block on prompt, tool and unknown events, silence elsewhere."""
    if kind in QUIET:
        return OK
    text = failure_text(tool, account, kind, name)
    if launch is not None:
        note(launch, text)
    return blocked(kind, tool, text)


def answer(kind, event, launch, now):
    """The answer to one hook event of KIND; EVENT is its parsed stdin, NOW the wall clock."""
    try:
        if kind not in KINDS:
            raise ValueError(f"unknown event {kind}")
        mark(launch.state + ".ran." + kind)
        if kind == "start" or launch.tool == "codex-as" and kind == "stop":
            return OK
        if launch.tool == "codex-as":
            if kind == "tool" and launch.marker:
                mark(launch.marker)
            readings, blind = codex_view(kind, event, launch)
        else:
            if kind != "prompt":
                mark(launch.state + ".reply", str(int(now)))
            readings, blind = claude_readings(launch), claude_blind(launch, now)
        rules = module("reserve")
        decision = rules.decide(launch.enabled, readings, now, blind)
        if decision.allow:
            return OK
        text = rules.block_text(launch.tool, launch.account, decision, kind, now)
    except Exception as error:
        return failed(kind, launch.tool, launch.account, type(error).__name__, launch)
    note(launch, text)
    return blocked(kind, launch.tool, text)


def handle(kind, raw, env, now):
    """The answer to one event from its raw stdin and the environment ENV; never raises."""
    try:
        launch = launch_of(env)
    except LaunchError as error:
        return failed(kind, error.tool, error.account, LAUNCH_FORMAT)
    except Exception as error:
        return failed(kind, None, None, type(error).__name__)
    if launch is None:
        return OK
    if kind == "status":
        try:
            return 0, status(raw, launch, now), ""
        except Exception:
            return OK
    if kind not in KINDS:
        return failed(kind, launch.tool, launch.account, "unknown event", launch)
    try:
        event = json.loads(raw)
        if not isinstance(event, dict):
            raise ValueError("the event is not a JSON object")
    except Exception as error:
        if kind not in QUIET:
            return failed(kind, launch.tool, launch.account, type(error).__name__, launch)
        event = None
    return answer(kind, event, launch, now)


def emit(stream, text):
    """Best effort: TEXT, as bytes, on STREAM; a closed pipe silences the stream for the rest of the run."""
    if not text:
        return
    try:
        target = getattr(stream, "buffer", None)
        if target is None:
            stream.write(text)
        else:
            target.write(text.encode("utf-8", "surrogateescape"))
        stream.flush()
    except Exception:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), stream.fileno())
        except Exception:
            pass


def kind_of(argv):
    argv = sys.argv if argv is None else argv
    return argv[1] if len(argv) == 2 else None


def old_python(kind):
    """The answer under a Python older than 3.11: one line and a block, or silence on a quiet event."""
    if kind in QUIET:
        return 0
    print("coding-agent-kit: the usage reserve guard needs Python 3.11 or newer.", file=sys.stderr)
    return 2


def main(argv=None):
    if sys.version_info < (3, 11):
        return old_python(kind_of(argv))
    kind = kind_of(argv)
    try:
        raw = sys.stdin.buffer.read()  # always drained, so the native never writes into a closed pipe
    except Exception:
        raw = None
    code, out, err = handle(kind, raw, os.environ, time.time())
    emit(sys.stdout, out)
    emit(sys.stderr, err and err + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
