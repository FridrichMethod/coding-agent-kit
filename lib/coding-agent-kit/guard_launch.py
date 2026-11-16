"""coding-agent-kit guarded launches: what both launchers need to start a launch with a usage reserve.

Public API (load this file by path; it is never on sys.path). It loads
reserve.py beside it by path, reads no configuration file and no environment
of its own, and takes every path from its caller:

    VARIABLES
        The launcher's channel to its guard (guard.py), never an interface for
        other programs: CODING_AGENT_KIT_GUARD (the launch, compact ASCII
        JSON), CODING_AGENT_KIT_GUARD_SCRIPT (the path of guard.py) and
        CODING_AGENT_KIT_GUARD_PYTHON (the launcher's own interpreter).
    script(lib) -> str
        The absolute path of guard.py of the kit whose lib directory is LIB.
        When the resolved kit root is <M>/releases/<release> of a managed
        install and <M>/current resolves to it, the path goes through
        <M>/current, so a session that outlives an update of the kit still
        finds a guard; otherwise it is the path under the resolved root.
        FileNotFoundError when that root has no guard.py.
    clear(env) -> None
        Removes VARIABLES from the mapping ENV. Every launch does this first,
        so a launch without a reserve never passes a guard on.
    prepare(env, directory, tool, account, policy, usage, started, *, marker=None,
            statusline=None) -> str
        clear(env), then creates STATE.launched (owned 0600, never through a
        link, holding this host's name, os.uname().nodename) and sets
        VARIABLES for the launch; returns STATE, the launch's prefix
        DIRECTORY/TOOL-ACCOUNT-PID-STARTED (PID is this process, which
        claude-as replaces by the native and codex-as keeps). DIRECTORY is the
        absolute guard directory (accounts.directory(config, create=True,
        name="guard")); POLICY is reserve.resolve()'s Policy of TOOL and ACCOUNT
        with at least one window on; USAGE is the absolute path of the Claude
        usage file or the Codex account record; STARTED the launch's epoch
        second. MARKER (codex-as, required) is the absolute path of the
        host-wide "the tool hook has run" marker; STATUSLINE (claude-as,
        optional) is the user's own status line command. The JSON:

            {"v":1,"tool":TOOL,"account":ACCOUNT,"five_hour":PERCENT|null,
             "weekly":PERCENT|null,"started":STARTED,"usage":USAGE,"state":STATE,
             "marker":MARKER,"statusline":STATUSLINE}

        with marker and statusline only when given, and PERCENT as
        reserve.percent() writes it, which guard.launch_of() reads back to the
        same Decimal. ValueError (with ENV cleared and nothing created) for
        anything the guard could not read; OSError when STATE.launched cannot
        be created.
    forget(state) -> None
        Removes the state files of the launch with prefix STATE; best effort.
    prune(directory, now) -> None
        Removes the launch state of every launch in DIRECTORY that started
        more than KEEP seconds (30 days) before NOW, judged by the start time
        in its name; the Codex markers and anything else stay. Best effort.
    unguarded(directory, tool, account, recorded_at) -> bool
        True when an earlier launch of TOOL and ACCOUNT left STATE.launched,
        no STATE.ran.* and a sign of a reply: STATE.seen, or, for a launch
        whose STATE.launched names this host once its PID no longer runs
        here, RECORDED_AT (the seen_at of the account's usage data, or None)
        at or after that launch's start. While a launch runs, another launch
        of the account may have recorded that data, and this host cannot see
        a launch of another host (login nodes share the home directory) run,
        so such a launch counts only by its own STATE.seen. Each launch
        counted has its state removed, so it is reported once. A launch of
        this process (the current one) is never counted.

State files in the guard directory, named PREFIX.SUFFIX (guard.py writes all
but .launched): launched (the launch's host name), ran.KIND, reply, seen,
blocked, and blocked.HEX.tmp that a cancelled write of blocked leaves. Markers
are named codex-hooks-FINGERPRINT.
"""

import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import sys


VARIABLES = ("CODING_AGENT_KIT_GUARD", "CODING_AGENT_KIT_GUARD_SCRIPT", "CODING_AGENT_KIT_GUARD_PYTHON")
TOOLS = ("claude-as", "codex-as")
ACCOUNT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")  # the names guard.py accepts
LAST = 253402300800  # 10000-01-01 UTC: no calendar time from here on
KEEP = 30 * 86400  # seconds a launch's state is kept after its start
STATE = re.compile(r"(?P<prefix>(?P<tool>claude-as|codex-as)-(?P<account>[A-Za-z0-9_][A-Za-z0-9_-]*)"
                   r"-(?P<pid>[0-9]+)-(?P<started>[0-9]+))"
                   r"\.(?P<suffix>launched|ran\.[a-z]+|reply|seen|blocked|blocked\.[0-9a-f]+\.tmp)")

# python3 -I puts no script directory on sys.path: siblings load by path from the real file.
LIB = Path(os.path.realpath(__file__)).parent.parent
_RESERVE = []


def _reserve():
    """reserve.py beside this file, loaded once; its loading errors reach the caller."""
    if not _RESERVE:
        spec = importlib.util.spec_from_file_location("coding_agent_kit_guard_launch_reserve",
                                                      LIB / "coding-agent-kit/reserve.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RESERVE.append(module)
    return _RESERVE[0]


def script(lib):
    root = Path(os.path.realpath(lib)).parent
    target = root / "lib/coding-agent-kit/guard.py"
    if not target.is_file():
        raise FileNotFoundError(2, "the usage reserve guard is missing", str(target))
    if root.parent.name == "releases":
        current = root.parent.parent / "current"
        if os.path.realpath(current) == str(root):
            return str(current / "lib/coding-agent-kit/guard.py")
    return str(target)


def clear(env):
    for name in VARIABLES:
        env.pop(name, None)


def _absolute(value, what):
    if isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str) or "\0" in value or not os.path.isabs(value):
        raise ValueError(f"the {what} must be an absolute path")
    return value


def prepare(env, directory, tool, account, policy, usage, started, *, marker=None, statusline=None):
    clear(env)
    if tool not in TOOLS:
        raise ValueError(f"unknown launcher {tool!r}")
    if not isinstance(account, str) or not ACCOUNT.fullmatch(account):
        raise ValueError("the account name is not one the guard accepts")
    if (policy.tool, policy.account) != (tool, account):
        raise ValueError("the policy is not this launch's")
    rules, enabled = _reserve(), policy.enabled()
    if not enabled:
        raise ValueError("no usage reserve is on")
    reserves = {}
    for window in rules.WINDOWS:
        if window not in enabled:
            reserves[window] = None
            continue
        value = rules.checked(enabled[window])
        text = rules.percent(value)
        if rules.flag_value(text) != value:
            raise ValueError(f"the {window} reserve cannot be passed on exactly")
        reserves[window] = text
    if type(started) is not int or not 0 < started < LAST:
        raise ValueError("the start time must be whole epoch seconds")
    directory, usage = _absolute(directory, "guard directory"), _absolute(usage, "usage path")
    if tool == "codex-as":
        marker = _absolute(marker, "Codex hook marker")
    elif marker is not None:
        raise ValueError("only codex-as has a hook marker")
    if statusline is not None and (tool != "claude-as" or not isinstance(statusline, str)):
        raise ValueError("only claude-as has a status line command, as a string")
    state = os.path.join(directory, f"{tool}-{account}-{os.getpid()}-{started}")
    launch = {"v": 1, "tool": tool, "account": account, **reserves, "started": started, "usage": usage,
              "state": state}
    if marker is not None:
        launch["marker"] = marker
    if statusline is not None:
        launch["statusline"] = statusline
    value = json.dumps(launch, separators=(",", ":"), ensure_ascii=True)
    guard = script(LIB)
    python = sys.executable
    if not python or not os.path.isabs(python):
        raise ValueError("the launcher's interpreter has no absolute path")
    fd = os.open(state + ".launched", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)  # whatever the umask took: unguarded() reads the host back
        os.write(fd, _host().encode("utf-8", "surrogateescape"))
    except BaseException:
        os.unlink(state + ".launched")
        raise
    finally:
        os.close(fd)
    env["CODING_AGENT_KIT_GUARD"], env["CODING_AGENT_KIT_GUARD_SCRIPT"] = value, guard
    env["CODING_AGENT_KIT_GUARD_PYTHON"] = python
    return state


def _launches(directory):
    """{prefix: (match, [names])} of the launch state files in DIRECTORY; empty when it cannot be listed."""
    try:
        names = os.listdir(directory)
    except OSError:
        return {}
    launches = {}
    for name in names:
        parsed = STATE.fullmatch(name)
        if parsed:
            launches.setdefault(parsed["prefix"], (parsed, []))[1].append(name)
    return launches


def _remove(directory, names):
    for name in names:
        path = os.path.join(directory, name)
        try:
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                os.unlink(path)
        except OSError:
            pass


def forget(state):
    directory, prefix = os.path.split(state)
    entry = _launches(directory).get(prefix)
    if entry:
        _remove(directory, entry[1])


def prune(directory, now):
    for parsed, names in _launches(directory).values():
        if now - int(parsed["started"]) > KEEP:
            _remove(directory, names)


def _host():
    """This host's name, which a launch's .launched holds."""
    return os.uname().nodename


def _here(path):
    """Whether the .launched at PATH names this host; False when it cannot be read."""
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC), "rb") as stream:
            return stream.read(256).decode("utf-8", "surrogateescape") == _host()
    except OSError:
        return False


def _running(pid):
    """Whether process PID runs on this host: the native (claude-as) or the launcher (codex-as) of a launch."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def unguarded(directory, tool, account, recorded_at):
    if type(recorded_at) not in (int, float) or not math.isfinite(recorded_at):
        recorded_at = None
    found = False
    for parsed, names in _launches(directory).values():
        pid = int(parsed["pid"])
        if parsed["tool"] != tool or parsed["account"] != account or pid == os.getpid():
            continue
        suffixes = {STATE.fullmatch(name)["suffix"] for name in names}
        if "launched" not in suffixes or any(suffix.startswith("ran.") for suffix in suffixes):
            continue
        if "seen" in suffixes or (recorded_at is not None and recorded_at >= int(parsed["started"])
                                  and _here(os.path.join(directory, parsed["prefix"] + ".launched"))
                                  and not _running(pid)):
            _remove(directory, names)
            found = True
    return found
