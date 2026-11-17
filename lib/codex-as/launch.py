"""Supervise native Codex with fixed account authentication and shared state.

With a usage reserve on, a launch that runs Codex's model work in this process
is guarded: it gets the reserve guard's hooks (GUARD_HOOKS, as -c values before
the caller's arguments) and goals off (GOALS_OFF), the guard's variables
(guard_launch.prepare) and, after Codex exits, a check that Codex ran them.
"""

import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time


# python3 -I puts no script directory on sys.path, so the modules load by path,
# from the real file: a linked launcher uses the kit it belongs to.
LIB = Path(os.path.realpath(__file__)).parent.parent


def load_module(name, path):
    spec = importlib.util.spec_from_file_location("codex_as_" + name, LIB / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    guard = load_module("check", "codex-as/check.py")  # ends the run first on Python before 3.11
    storage = load_module("layout", "codex-as/layout.py")
    handoff = load_module("handoff", "codex-as/handoff.py")
    usage = load_module("usage", "codex-as/usage.py")
    settings = load_module("config", "coding-agent-kit/config.py")
    metadata = load_module("accounts", "coding-agent-kit/accounts.py")
    reserve = load_module("reserve", "coding-agent-kit/reserve.py")
    guard_launch = load_module("guard_launch", "coding-agent-kit/guard_launch.py")
except (OSError, ImportError, SyntaxError):
    message = f"codex-as: incomplete install at {LIB.parent}"
    print(guard.styled(message, "31", 2) if "guard" in globals() else message, file=sys.stderr)
    sys.exit(1)


# The 0.7 names and short forms of the launcher's own queries, which count only
# as the first argument and by exact match; the older spellings stay accepted.
QUERIES = {"--list": "--list-accounts", "-l": "--list-accounts", "--labels": "--account-labels",
           "--info": "--account-info", "-i": "--account-info"}
OPTIONS = {*QUERIES, *QUERIES.values(), "--show-config", "--help", "-h", "--version", "-V", "-v"}
# A dash word that is safe to echo back in the hint, as bin/codex-as checks it.
PLAIN = re.compile(r"-[A-Za-z0-9_=.,:/@+-]*")

# The usage reserve guard's hooks, one -c value each: UserPromptSubmit, and PreToolUse for every tool.
# Codex trusts a hook by a hash of its definition, under a key that names the session flags, the event
# and the hook's place, so these strings are the same on every launch, account, host and kit release:
# a change asks every host to trust the hooks again. The hook's shell takes the interpreter and the
# guard's path from the launch's environment (CODING_AGENT_KIT_GUARD_PYTHON and _SCRIPT).
GUARD_HOOKS = (
    r'hooks.UserPromptSubmit=[{hooks=[{type="command",command="\"$CODING_AGENT_KIT_GUARD_PYTHON\" -I -B -S \"$CODING_AGENT_KIT_GUARD_SCRIPT\" prompt",timeout=20}]}]',
    r'hooks.PreToolUse=[{hooks=[{type="command",command="\"$CODING_AGENT_KIT_GUARD_PYTHON\" -I -B -S \"$CODING_AGENT_KIT_GUARD_SCRIPT\" tool",timeout=20}]}]',
)
# The [hooks.state] keys of GUARD_HOOKS, by source, event and place; enabled = false there skips a hook.
HOOK_KEYS = ("/<session-flags>/config.toml:user_prompt_submit:0:0", "/<session-flags>/config.toml:pre_tool_use:0:0")
# A goal run continues on its own without a prompt, so a denied tool call would repeat without end;
# a guarded launch turns goals off. --disable checks the feature name, and native Codex applies it after
# every -c value, root or subcommand, and after --enable. clap keeps one --disable list, which a
# --disable after a subcommand replaces, so the -c value has to hold on its own: a caller's --enable
# goals and a -c value that could turn goals on (check.enables_goals) are refused.
GOALS_OFF = ("-c", "features.goals=false", "--disable", "goals")
# What a guarded launch adds to its message when it refuses.
WITHOUT = ("; to launch without usage reserves, run codex-as --reserve-5h off --reserve-weekly off {account} "
           "with the same arguments.")


def guard_fingerprint():
    """The first 16 hex digits of the SHA-256 of GUARD_HOOKS joined by newlines: the name of the
    host-wide marker that the guard's tool hook has run under Codex with these definitions."""
    return hashlib.sha256("\n".join(GUARD_HOOKS).encode("ascii")).hexdigest()[:16]


def shown(word):
    """What the hint echoes of a plain dash word: the part before the first =, since a value or a
    secret may follow it, or None when that part holds sk- or names an own option."""
    word = word.partition("=")[0]
    return None if "sk-" in word.lower() or word in OPTIONS else word


def enroll(layout, codex):
    if os.path.lexists(layout.target):
        guard.credential(layout.target)
        return
    tty = guard.terminal()
    try:
        tty.write("Existing accounts: " + (", ".join(guard.accounts(layout.secrets)) or "(none)") + "\n")
        guard.confirm(tty, f"Account {layout.name} does not exist. Login and save a new account?")
        with tempfile.TemporaryDirectory(prefix="codex-as-login-") as temp:
            env = os.environ.copy()
            env["CODEX_HOME"] = temp
            # Login must not pick up the shared runtime database.
            env.pop("CODEX_SQLITE_HOME", None)
            result = subprocess.run([codex, "-c", 'cli_auth_credentials_store="file"', "login", "--device-auth"],
                                    env=env, stdin=tty, stdout=tty, stderr=tty)
            if result.returncode:
                guard.fail("native device login failed/cancelled; no credentials imported. Device login must be enabled by the account/workspace.")
            raw = guard.credential(Path(temp) / "auth.json")
            layout.secrets.mkdir(mode=0o700, exist_ok=True)
            guard.directory(layout.secrets, private=True)
            guard.exclusive(layout.target, raw)
            tty.write(f"Credentials saved for account {layout.name}.\n", "32")
    finally:
        tty.close()


# Set from the caller in __main__: the launcher itself creates files under umask 077.
CALLER_UMASK = -1
# A child killed by one of these is re-raised, so the caller sees what native Codex shows.
RERAISED = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGKILL, signal.SIGPIPE}


class Supervision:
    """From just before native Codex starts until post-run repair ends: forward termination
    (SIGTERM, SIGHUP, SIGQUIT) to it and ignore Ctrl-C, which the terminal already delivers to
    its whole process group. Termination after it exits waits for the repair to finish, then
    ends the launcher."""

    def __init__(self):
        self.child, self.pending, self.late, self.previous = None, [], [], {}

    def forward(self, signum, frame):
        if self.child is None:
            self.pending.append(signum)
        elif self.child.poll() is None:
            self.child.send_signal(signum)
        else:
            self.late.append(signum)

    def __enter__(self):
        for signum, handler in ((signal.SIGINT, lambda signum, frame: None), (signal.SIGTERM, self.forward),
                                (signal.SIGHUP, self.forward), (signal.SIGQUIT, self.forward)):
            self.previous[signum] = signal.signal(signum, handler)
        SUPERVISION.append(self)
        return self

    def __exit__(self, *error):
        SUPERVISION.remove(self)
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)
        if self.late:
            exit_status(-self.late[0])


SUPERVISION = []


def run_native(command, env, fd=None):
    """Run native Codex; the result is negative for a signal, like Popen.returncode."""
    child = subprocess.Popen(command, env=env, pass_fds=() if fd is None else (fd,), umask=CALLER_UMASK)
    for supervision in SUPERVISION:
        supervision.child = child
        for signum in supervision.pending:
            supervision.forward(signum, None)
    return child.wait()


def exit_status(result):
    if result >= 0:
        return result
    if -result in RERAISED:
        try:
            if -result != signal.SIGKILL:  # its disposition cannot change, and it always kills
                signal.signal(-result, signal.SIG_DFL)
            os.kill(os.getpid(), -result)
        except OSError:
            pass
    return 128 - result


def remember(config, layout, raw=None, plan=None, started=None, threads=()):
    """Best effort, for --account-info: this login's email and plan, and after a run the
    last rate limits its rollouts show (THREADS: the session ids of a guarded run, see run_threads).
    Never fails a launch and writes no token. The rollouts are looked for from SKEW before STARTED,
    as run_threads does, since a file server's clock may lag this host's: a snapshot of this account's
    from up to that long before the run is a reading of it all the same, and the record takes the
    newest by its own time."""
    try:
        if raw is None:
            raw = guard.credential(layout.target)
        email, kind = storage.profile(raw)
        seen = None if plan is None else usage.latest(layout.root, {*plan.continued, *threads}, storage.owner(raw),
                                                      started - SKEW, time.time())
        # The old record is read only after the scan, just before the write, so a run of
        # this account that finished meanwhile keeps its newer snapshot.
        path = metadata.directory(config, create=True) / f"codex-{layout.name}.json"
        try:
            old = metadata.read(path) or {}
        except ValueError:
            old = {}
        last = old.get("usage") if isinstance(old.get("usage"), dict) else None
        known = last.get("seen_at") if last else None
        # Concurrent runs of one account: the newest snapshot wins, by its own time.
        if seen and (not isinstance(known, int) or seen["seen_at"] > known):
            last = seen
        record = {"email": email, "plan": kind, "usage": last}
        metadata.write(path, {key: value for key, value in record.items() if value is not None})
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        pass


def post_run(layout, baseline):
    """Repair or report the account link after native Codex exits; never changes the exit code.
    Returns whether this launch was the account's last client (False when that was not told)."""
    replaced = "Codex replaced the account auth.json link with a regular file; "
    last = False
    try:
        if not os.path.lexists(layout.auth):
            guard.warn(f"Codex removed the account auth.json link, as a logout or new login inside Codex does; both "
                       f"revoke the tokens in {layout.target}. To log in again, move that file aside under a "
                       f"name that does not match codex-NAME.json, then run: codex-as {layout.name}")
            return False
        unlinked = not layout.auth.is_symlink()
        last = layout.acquire()
        if last:
            layout.repair(baseline)
            current = layout.validate()
            if unlinked:
                guard.warn(replaced + "credentials were imported into secrets and the link restored.")
        elif unlinked:
            guard.warn(replaced + "the last client of this account to exit repairs it.")
            return False
        else:
            current = guard.credential(layout.target)
        if baseline is not None and not storage.same_identity(current, baseline):
            guard.warn(f"{layout.target} now holds a different ChatGPT user than when this launch started; "
                       f"codex-as {layout.name} uses that user from now on.")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, RuntimeError) as error:
        guard.report(error)
    finally:
        layout.close()
    return last


def refuse(account, reason):
    guard.fail(f"{account}: {reason}" + WITHOUT.format(account=account))


def unguardable(account, plan):
    """Refuses, with a reserve on, a launch form or a caller's option that the guard's hooks cannot cover."""
    if plan.form == "refused":
        name = "debug app-server" if plan.command == "debug" else plan.command
        # Each is refused as a whole, also a subcommand of it that runs no model work itself.
        refuse(account, f"codex {name} can run model work where the usage reserve guard's hooks cannot see or stop it")
    if plan.hooks_override:
        refuse(account, "a -c or --config value under hooks would meet the usage reserve guard's hooks, which "
                        "codex-as passes as -c values of its own")
    if plan.hooks_disabled:
        refuse(account, "--disable hooks or a -c value under features turns off the hooks that enforce usage reserves")
    if plan.goals_on:
        refuse(account, "--enable goals, or a -c value that sets features.goals or replaces the features table, would "
                        "turn on the goals that a launch with usage reserves keeps off: a goal continues on its own "
                        "after a denied tool call")
    if plan.ephemeral:
        refuse(account, "--ephemeral keeps no session rollout, from which the usage reserve guard reads the "
                        "session's usage")
    if plan.ignore_user_config:
        refuse(account, "--ignore-user-config skips the configuration that holds Codex's trust of the usage reserve "
                        "guard's hooks")
    if plan.worktree:
        # Codex records such a session's cwd as CODEX_HOME/worktrees/ID/DIR, never the launch directory.
        refuse(account, "--worktree runs the session in a Git worktree that Codex manages elsewhere, where the check "
                        "after the run cannot find it to tell whether Codex ran the usage reserve guard's hooks")


def guarded(config, account, plan, layout):
    """The checks of a guarded launch that read the Codex configuration and the guard's directory, all
    before a missing account is offered a login. Returns (guard directory, marker, account record)."""
    off = storage.hooks_off(guard, layout.root, plan.cwd, plan.profile, layout.home)
    if off is not None:
        refuse(account, f"{off} turns the hooks feature off under [features], so Codex would not run the usage "
                        "reserve guard's hooks")
    off = storage.hook_off(guard, layout.root, HOOK_KEYS, plan.profile, layout.home)
    if off is not None:
        refuse(account, f"{off} turns a usage reserve guard hook off under [hooks.state], so Codex would not run it")
    managed = storage.goals_on(guard)
    if managed is not None:
        refuse(account, f"{managed} sets goals under [features], which native Codex applies over the goals off of a "
                        "launch with usage reserves: a goal continues on its own after a denied tool call")
    # A missing account logs in with a device code, which always gives a ChatGPT user and workspace.
    if os.path.lexists(layout.target):
        raw = guard.credential(layout.target)  # refused as for every launch when it is unusable
        try:
            owner = storage.owner(raw)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, UnicodeError, RecursionError):
            owner = None
        if owner is None:
            refuse(account, "this login names no ChatGPT user and workspace, as an API key login does: it reports no "
                            "usage windows for the usage reserve guard, and its sessions cannot be told from other "
                            "accounts'")
    directory = metadata.directory(config, create=True, name="guard")
    marker = directory / ("codex-hooks-" + guard_fingerprint())
    if plan.form == "headless" and not os.path.lexists(marker):
        refuse(account, "Codex has not run the usage reserve guard's tool hook on this host yet, and a headless run "
                        "skips hooks that are not trusted without a word: start codex-as "
                        f"{account} once interactively, trust the hooks in \"Hooks need review\" and let it run one "
                        "tool call")
    return directory, marker, metadata.directory(config, create=True) / f"codex-{account}.json"


# The guard's block message is its own text; what is printed of it is bounded and plain.
BLOCKED_LIMIT = 16 * 1024
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def blocked_message(state):
    """The guard's latest block message in the launch with prefix STATE, or None."""
    try:
        fd = os.open(state + ".blocked", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    # Checked on the descriptor itself: Python refuses to wrap a directory's, and a FIFO is never read.
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        regular = False
    if not regular:
        os.close(fd)
        return None
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(BLOCKED_LIMIT)
    return CONTROL.sub("", raw.decode("utf-8", "replace")).strip() or None


# Codex stamps rollout lines in milliseconds, truncated: a line written at or after a moment is stamped
# no more than this before it.
STAMPED = 0.001
# The session_meta source of the sessions each guarded form starts: the TUI's, and exec's and review's.
SOURCES = {"interactive": "cli", "headless": "exec"}
HEADS = 1024  # session_meta lines read after a run
RELEVANT = (b"token_count", b"response_item", b"token_usage_record")  # the lines activity() parses
# A rollout is opened after a run when its file time is at most this many seconds before the run: on a
# network home directory the file's time comes from the file server's clock, which may lag this host's
# (Kerberos, under which NFSv4 often runs, refuses a skew above 5 minutes). The lines' own times, which
# Codex writes with this host's clock, decide what is the run's.
SKEW = 300
CHUNK = 1 << 16


def session_meta(path):
    """The payload of the session_meta line that starts the rollout at PATH, or None; reads up to that
    line's end only (at most usage.FIRST_LINE bytes), never through a link and never from a FIFO."""
    with usage.opened(path) as stream:
        head = b""
        while b"\n" not in head and len(head) < usage.FIRST_LINE:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            head += chunk
    try:
        line = json.loads(head.split(b"\n", 1)[0])
    except (ValueError, RecursionError):
        return None
    payload = line.get("payload") if isinstance(line, dict) and line.get("type") == "session_meta" else None
    return payload if isinstance(payload, dict) else None


def same_place(path, cwd):
    try:
        return isinstance(path, str) and os.path.isabs(path) and os.path.realpath(path) == os.path.realpath(cwd)
    except ValueError:  # a NUL in the recorded path
        return False


def inside(path, directory):
    """Whether PATH, an absolute path, lies below DIRECTORY once both are resolved."""
    try:
        if not (isinstance(path, str) and os.path.isabs(path)):
            return False
        path, directory = os.path.realpath(path), os.path.realpath(directory)
    except ValueError:  # a NUL in the recorded path
        return False
    return path != directory and os.path.commonpath((path, directory)) == directory


def span(stream, begun, ended):
    """The lines of a rollout stamped between BEGUN and ENDED: read back from the end of STREAM,
    usage.TAIL bytes at a time, to the lines from before BEGUN; a line whose time cannot be read never
    counts."""
    for lines in usage.backwards(stream):
        earlier = False
        for raw in lines:
            stamp = usage.STAMP.match(raw)
            when = usage.moment(stamp[1].decode("ascii", "replace")) if stamp else None
            if when is None or when > ended:
                continue
            if when < begun - STAMPED:
                earlier = True
                continue
            yield raw
        if earlier:
            break


def turned(path, cwd, begun, ended):
    """Whether a turn of the session at PATH began in CWD between BEGUN and ENDED: Codex starts each
    turn with a turn_context line that holds the session's working directory, which exec resume sets
    to its own, whatever directory the session started in (measured with codex-cli 0.169.0)."""
    try:
        with usage.opened(path) as stream:
            for raw in span(stream, begun, ended):
                found = usage.parsed(raw) if b"turn_context" in raw else None
                if found and found[1] == "turn_context" and same_place(found[2].get("cwd"), cwd):
                    return True
    except OSError:
        pass
    return False


def run_threads(root, plan, owner, begun, ended, alone):
    """The sessions this guarded run may have written, as (SURE, EITHER, MAYBE): lists of groups, each
    the rollout paths of one session and of its sub-agents (whose session_meta names it as session_id,
    or as parent). The run wrote every group of SURE, one group of EITHER if it wrote any, which one
    cannot be told, and any of MAYBE, perhaps none: the others are other clients' of the account.

    They are the sessions the run resumed or recovered by id (SURE), and the sessions of this account
    (OWNER, the creator in each rollout's session_meta; None, when the login no longer tells it, counts
    only those named by id) that started in this run's span, BEGUN to ENDED, in its directory, from its
    kind of Codex: source cli for the TUI, resume and fork, exec for exec and review. So a codex-as exec
    run from a TUI is not the TUI's, nor a TUI the exec's, nor a run in another directory either's; a
    guardian review runs no user hook and never counts. A headless run starts one session (exec resume
    by id none; exec resume without an id one when it finds no session of the directory to continue), so
    when more started here, other headless runs of the account did too: they are EITHER. A TUI may start
    several: SURE when ALONE, MAYBE when another codex-as client of the account ran at the run's start or
    end or arrived during it (not ALONE), which may be a second TUI here.

    A TUI also moves its work to new sessions of its own elsewhere (measured with codex-cli 0.169.0): /cd
    forks the conversation into the new directory, in a session that names the one it left as its
    forked_from_id; /worktree starts one in a worktree that Codex manages under CODEX_HOME/worktrees
    (ROOT/worktrees, which every account home links); fork starts one from the session it forks, in that
    session's directory by default. These go where the run's sessions here go (a /cd fork where the
    session it names went, along a chain of them). Any other new session of the account elsewhere that
    the TUI's kind of Codex started may be its own, after /cd and /new, or another client's: MAYBE.

    A form that can switch to an older session it does not name appends to one of this account's older
    sessions, of any day: an interactive form (the TUI, resume and fork) to one of any directory, so
    every such session written during the run is SURE when ALONE and MAYBE otherwise; exec resume
    without an id to one in which it begins a turn in its directory (turned), whatever directory the
    session started in, so each such session is EITHER.

    Only rollouts whose file time is at most SKEW before BEGUN are opened, at most HEADS session_meta
    lines, the oldest start first, whatever account wrote them."""
    named = set(plan.continued)
    found = []
    for path in (Path(root) / "sessions").glob("*/*/*/rollout-*.jsonl"):
        match = usage.NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_mtime >= begun - SKEW:
            found.append((match[1] not in named, path.name, match[1], path))
    resumes = plan.command == "exec-resume" and not plan.forked  # appends to a session
    interactive = plan.form == "interactive"
    sure, either, maybe, agents, moved = {}, {}, {}, [], []
    # Where the run's sessions that it does not name go, new or older alike.
    unnamed = either if not interactive else sure if alone else maybe
    for _, _, thread, path in sorted(found)[:HEADS]:
        try:
            meta = session_meta(path)
        except OSError:
            continue
        if thread in named:
            sure.setdefault(thread, []).append(path)
            continue
        if meta is None:
            continue
        source = meta.get("source")
        if isinstance(source, dict):  # a sub-agent: a guardian review runs no user hook
            if meta.get("thread_source") != "guardian_review":
                agents.append((meta, path))
            continue
        if owner is None or usage.created_by(meta) != owner:
            continue
        start = usage.moment(meta.get("timestamp"))
        if start is None:
            continue
        if start >= begun - STAMPED:
            if start > ended or (resumes and named) or source != SOURCES[plan.form]:
                continue
            parent = guard.session_id(meta.get("forked_from_id"))
            if same_place(meta.get("cwd"), plan.cwd) or interactive and (
                    inside(meta.get("cwd"), Path(root) / "worktrees")
                    or plan.forked and parent is not None and plan.recover_id in (None, parent)):
                unnamed.setdefault(thread, []).append(path)
            elif interactive:
                moved.append((parent, thread, path))
        elif interactive or (resumes and not named and turned(path, plan.cwd, begun, ended)):
            unnamed.setdefault(thread, []).append(path)
    # A /cd fork goes where the session it names went, along a chain; any other session elsewhere is MAYBE.
    while follow := [item for item in moved if item[0] in sure or item[0] in maybe]:
        for item in follow:
            moved.remove(item)
            (sure if item[0] in sure else maybe).setdefault(item[1], []).append(item[2])
    for _, thread, path in moved:
        maybe.setdefault(thread, []).append(path)
    groups = {**sure, **either, **maybe}
    for meta, path in agents:
        parent = next((key for key in (meta.get("session_id"), meta.get("parent_thread_id"))
                       if isinstance(key, str) and key in groups), None)
        if parent is not None:
            groups[parent].append(path)
    return list(sure.values()), list(either.values()), list(maybe.values())


def this_run(layout, plan, baseline, begun, ended, alone):
    """run_threads for this login, or None when they cannot be told."""
    try:
        raw = guard.credential(layout.target)
    except (OSError, ValueError):
        raw = baseline
    try:
        return run_threads(layout.root, plan, storage.owner(raw) if raw else None, begun, ended, alone)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, UnicodeError, RecursionError):
        return None


def activity(paths, begun, ended):
    """(written, requested, tools) of the rollouts at PATHS between BEGUN and ENDED: a line, a model
    request (a line of a model response, usage.responded, or a usable rate-limit snapshot, which a
    reply without rate-limit headers does not leave), and a call that a tool hook sees. Each rollout is
    read as span() reads it; inherited lines never count. These lines are the only evidence of a model
    request: nothing else of the run, such as its exit status or output, is read."""
    written = requested = tools = False
    for path in paths:
        try:
            with usage.opened(path) as stream:
                for raw in span(stream, begun, ended):
                    written = True
                    if not any(word in raw for word in RELEVANT):
                        continue
                    found = usage.parsed(raw)
                    if found is None or usage.inherited(found[0]):
                        continue
                    _, kind, payload, ptype = found
                    if kind == "event_msg" and ptype == "token_count":
                        requested = requested or usage.usable(payload.get("rate_limits")) is not None
                    requested = requested or usage.responded(kind, ptype, payload)
                    tools = tools or usage.hooked(kind, ptype, payload)
        except OSError:
            continue
    return written, requested, tools


def missed(views, prompt):
    """What VIEWS (activity() results) show of the guard's hooks that Codex did not run, PROMPT telling
    whether the prompt hook ran: a tool call needs the tool hook, a model request either hook."""
    if any(view[2] for view in views):
        return "the guard's tool hook" if prompt else "the guard's hooks"
    return "the guard's hooks" if not prompt and any(view[1] for view in views) else None


def unenforced(state, threads, begun, ended):
    """What this run shows of the guard's hooks that Codex did not run, as (OWN, UNSURE), each "the
    guard's hooks", "the guard's tool hook" or None. THREADS is run_threads(). OWN is what the run's own
    sessions show: every group of SURE, and of the groups of EITHER written during the run what every
    one shows. UNSURE, when OWN is None, is what any group of EITHER or MAYBE written during the run
    shows, a session that may be this run's or another client's of the account."""
    prompt, tool = (os.path.lexists(f"{state}.ran.{kind}") for kind in ("prompt", "tool"))
    if tool:
        return None, None
    sure, either, maybe = threads
    own = [activity(group, begun, ended) for group in sure]
    either, maybe = ([view for view in (activity(group, begun, ended) for group in groups) if view[0]]
                     for groups in (either, maybe))
    if either:
        own.append(tuple(all(view[index] for view in either) for index in range(3)))
    found = missed(own, prompt)
    return found, None if found else missed([*either, *maybe], prompt)


def after_guarded(layout, plan, account, state, marker, threads, begun, ended):
    """After a guarded run: the guard's latest block message of a headless run, which Codex itself
    does not show; then, when this run's THREADS show that Codex did not run the guard's hooks, the
    warning, and the marker goes, so headless runs wait until an interactive launch has trusted them
    again; when only a session that may be another client's shows it, a notice, and the marker stays.
    The marker goes before anything is printed, and printing never fails (check.emit)."""
    missing = unsure = None
    if threads is not None:  # else what the run did cannot be told
        try:
            missing, unsure = unenforced(state, threads, begun, ended)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
            pass
    if missing:
        try:
            os.unlink(marker)
        except OSError:
            pass
    if plan.form == "headless":
        try:
            message = blocked_message(state)
        except OSError:
            message = None
        if message:
            guard.emit(guard.styled(message, "31", 2))
    if missing:
        guard.warn(f"{account}: the usage reserve was not enforced in this run: Codex did not run {missing} (not "
                   f"trusted, disabled, or managed hooks only). Start codex-as {account} interactively and trust "
                   "them in \"Hooks need review\".")
    elif unsure:
        # Never silent; the marker stays, since the session may be another client's whose hooks ran.
        guard.warn(f"{account}: the usage reserve may not have been enforced in this run: a session of {account} "
                   f"written during it, by this run or by another client of {account}, shows that Codex did not run "
                   f"{unsure} (not trusted, disabled, or managed hooks only), and the check after the run cannot tell "
                   f"which client wrote it. If it was this run, start codex-as {account} interactively and trust them "
                   "in \"Hooks need review\".")


def main(args):
    config = settings.load()
    for warning in config.warnings:
        guard.warn(warning)
    # Only a guarded launch passes a guard on: inherited guard variables never reach a child.
    guard_launch.clear(os.environ)
    # Reserve flags come before NAME; without them the arguments come back unchanged.
    reserves, args = reserve.split_flags(args)
    args = [QUERIES.get(args[0], args[0]), *args[1:]] if args else args
    if args in (["--list-accounts"], ["--show-config"], ["--account-labels"]) or (
            len(args) == 2 and args[0] == "--account-info"):
        # Metadata only: no preflight, no directories, no native CLI and never a credential read.
        if args[0] == "--account-info":
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", args[1]):
                guard.fail("invalid account name (ASCII letters, digits, _ or -; no leading -).")
            if args[1] not in guard.accounts(config.secrets_dir):
                guard.fail(f"no account {args[1]}.")
            text = metadata.info("codex-as", args[1], config, time.time())
        elif args[0] == "--account-labels":
            text = "\n".join(metadata.labels("codex-as", guard.accounts(config.secrets_dir), config))
        else:
            text = "\n".join(guard.accounts(config.secrets_dir)) if args[0] == "--list-accounts" else config.show("codex-as")
        if text:
            print(text)
        return 0
    if args and args[0] not in OPTIONS and PLAIN.fullmatch(args[0]) and (word := shown(args[0])):
        guard.fail(f"{word} is not a codex-as option; native options go after NAME (codex-as NAME {word} ...)")
    if not args or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", args[0]):
        guard.fail("invalid account name (ASCII letters, digits, _ or -; no leading -).")
    account, *native_args = args
    policy = reserve.resolve("codex-as", account, reserves, config.usage_reserve)
    # Only the caller's arguments are scanned: the launcher's own values never count as theirs.
    plan = guard.arguments(native_args, config)
    # With a reserve on, the forms that run model work here get the guard; the plain ones (no model
    # work, help and version) launch as without reserves.
    guarding = not policy.off and plan.form != "plain"
    if guarding:
        unguardable(account, plan)
        try:
            guard_launch.script(LIB)
        except OSError:
            guard.fail(f"incomplete install at {LIB.parent}")
    codex = config.which("codex")
    if codex is None:
        guard.fail("Codex is not installed on PATH." if config.codex == "codex"
                   else f"the configured codex executable {config.codex} was not found.")
    if plan.rootless:
        # Help, version, update and completion are the same for every login; an update
        # installs under CODEX_HOME, so it goes to the root home like plain codex update.
        env = os.environ.copy()
        env["CODEX_HOME"] = str(config.codex_home)
        env.pop("CODEX_SQLITE_HOME", None)
        with Supervision():
            return run_native([codex, *native_args], env)
    guard.preflight(account, plan, config)
    layout = storage.Layout(guard, account, config)
    sqlite = storage.shared_sqlite(guard, layout.root, plan.cwd, plan.profile)
    layout.root_check()
    version = subprocess.run([codex, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             text=True, check=False)
    if version.returncode:
        return version.returncode
    guard.codex_version(version.stdout)
    if guarding:
        directory, marker, record = guarded(config, account, plan, layout)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(layout.home)
    env["CODEX_SQLITE_HOME"] = str(sqlite)
    command = [codex, "-c", 'cli_auth_credentials_store="file"']
    command += ["-c", "sqlite_home=" + json.dumps(str(sqlite), ensure_ascii=False)]
    if plan.mode == "interactive":
        # Its "Update now" installer would write under the account CODEX_HOME.
        command += ["-c", "check_for_update_on_startup=false"]
        if not plan.no_daemon:
            command.append("--no-daemon")
    if guarding:
        for hook in GUARD_HOOKS:
            command += ["-c", hook]
        command.extend(GOALS_OFF)
    command.extend(native_args)
    # Reject malformed existing secrets before creating runtime metadata.
    if os.path.lexists(layout.target):
        guard.credential(layout.target)
    baseline = None
    try:
        alone = layout.acquire()
        # Every launch that can write sessions arrives, with reserves or without; a plain form writes none.
        try:
            arrived = layout.arrive() if plan.form != "plain" else None
        except OSError:
            arrived = None  # a launch arriving later cannot be told: this one is never alone
        if alone:
            enroll(layout, codex)
            layout.prepare()
            fcntl.flock(layout.fd, fcntl.LOCK_SH)
        elif not layout.auth.is_symlink():
            guard.fail("account has active clients and authentication needs repair; close all account clients first.")
        # Revalidate after EX->SH, whose conversion may release the lock briefly.
        baseline = layout.validate()
        remember(config, layout, baseline)
        if plan.profile is not None:
            layout.link_profile(plan.profile)
        if plan.unknown and plan.command == "resume":
            # A typo fails in clap only after a takeover would have closed sessions.
            guard.warn("unrecognized option " + ", ".join(plan.unknown) + "; no takeover performed.")
        else:
            handoff.prepare(guard, layout.root, account, plan.resume_id)
            handoff.prepare(guard, layout.root, account, plan.recover_id, take=False)
            if plan.scope is not None:
                handoff.release(guard, layout.root, account, None if plan.scope == "all" else plan.scope,
                                last=plan.last, others=plan.include_non_interactive)
    except BaseException:
        layout.close()
        raise
    # One start time for the guard and the record of this run.
    started, state = int(time.time()), None
    # The launch's state exists only under Supervision, so a signal never leaves it behind.
    with Supervision():
        try:
            try:
                if guarding:
                    guard_launch.prune(directory, started)
                    state = guard_launch.prepare(env, directory, "codex-as", account, policy, record, started,
                                                 marker=marker)
                    print(guard.styled("codex-as: " + policy.describe() + "; hooks refuse new prompts and tool calls "
                                       "at a reserve.", "36", 2), file=sys.stderr)
                # The span of this run's own lines, for the check after it.
                begun = time.time()
                result = run_native(command, env, layout.fd)
                ended = time.time()
            finally:
                layout.close()
            # Use a fresh open-file description: surviving descendants still hold SH. Alone: no other
            # client of the account at the run's start or end, and none arrived meanwhile.
            alone = post_run(layout, baseline) and alone and layout.size == arrived
            threads = None if state is None else this_run(layout, plan, baseline, begun, ended, alone)
            groups = [group for kind in threads for group in kind] if threads else ()
            if os.path.lexists(layout.target):
                remember(config, layout, plan=plan, started=started,
                         threads={usage.NAME.fullmatch(path.name)[1] for group in groups for path in group})
            if state is not None:
                after_guarded(layout, plan, account, state, marker, threads, begun, ended)
        finally:
            if state is not None:
                guard_launch.forget(state)
    return result


if __name__ == "__main__":
    CALLER_UMASK = os.umask(0o077)
    try:
        sys.exit(exit_status(main(sys.argv[1:])))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, RuntimeError) as error:
        guard.report(error)
        sys.exit(1)
    except KeyboardInterrupt:
        print(guard.styled("codex-as: cancelled; credentials preserved.", "33", 2), file=sys.stderr)
        sys.exit(130)
