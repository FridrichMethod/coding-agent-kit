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
# A goal run continues on its own without a prompt, so a denied tool call would repeat without end;
# a guarded launch turns goals off. --disable checks the feature name and comes after every -c value,
# so neither a caller's -c features.goals=true nor --enable goals turns them on again.
GOALS_OFF = ("--disable", "goals")
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
RERAISED = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGKILL, signal.SIGPIPE}


class Supervision:
    """From just before native Codex starts until post-run repair ends: forward termination
    to it and ignore Ctrl-C, which the terminal already delivers to its whole process group.
    Termination after it exits waits for the repair to finish, then ends the launcher."""

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
        for signum, handler in ((signal.SIGINT, lambda signum, frame: None),
                                (signal.SIGTERM, self.forward), (signal.SIGHUP, self.forward)):
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


def remember(config, layout, raw=None, plan=None, started=None):
    """Best effort, for --account-info: this login's email and plan, and after a run the
    last rate limits its rollouts show. Never fails a launch and writes no token. Returns
    whether this run's rollouts showed a snapshot (a model request); False before a run."""
    seen = None
    try:
        if raw is None:
            raw = guard.credential(layout.target)
        email, kind = storage.profile(raw)
        seen = None if plan is None else usage.latest(layout.root, plan.continued, storage.owner(raw), started, time.time())
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
    return seen is not None


def post_run(layout, baseline):
    """Repair or report the account link after native Codex exits; never changes the exit code."""
    replaced = "Codex replaced the account auth.json link with a regular file; "
    try:
        if not os.path.lexists(layout.auth):
            guard.warn(f"Codex removed the account auth.json link, as a logout or new login inside Codex does; both "
                       f"revoke the tokens in {layout.target}. To log in again, move that file aside under a "
                       f"name that does not match codex-NAME.json, then run: codex-as {layout.name}")
            return
        unlinked = not layout.auth.is_symlink()
        if layout.acquire():
            layout.repair(baseline)
            current = layout.validate()
            if unlinked:
                guard.warn(replaced + "credentials were imported into secrets and the link restored.")
        elif unlinked:
            guard.warn(replaced + "the last client of this account to exit repairs it.")
            return
        else:
            current = guard.credential(layout.target)
        if baseline is not None and not storage.same_identity(current, baseline):
            guard.warn(f"{layout.target} now holds a different ChatGPT user than when this launch started; "
                       f"codex-as {layout.name} uses that user from now on.")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, RuntimeError) as error:
        guard.report(error)
    finally:
        layout.close()


def refuse(account, reason):
    guard.fail(f"{account}: {reason}" + WITHOUT.format(account=account))


def unguardable(account, plan):
    """Refuses, with a reserve on, a launch form or a caller's option that the guard's hooks cannot cover."""
    if plan.form == "refused":
        name = "debug app-server" if plan.command == "debug" else plan.command
        refuse(account, f"codex {name} runs model work where the usage reserve guard's hooks cannot see or stop it")
    if plan.hooks_override:
        refuse(account, "a -c or --config value under hooks would meet the usage reserve guard's hooks, which "
                        "codex-as passes as -c values of its own")
    if plan.hooks_disabled:
        refuse(account, "--disable hooks or a -c value under features turns off the hooks that enforce usage reserves")
    if plan.ephemeral:
        refuse(account, "--ephemeral keeps no session rollout, from which the usage reserve guard reads the "
                        "session's usage")
    if plan.ignore_user_config:
        refuse(account, "--ignore-user-config skips the configuration that holds Codex's trust of the usage reserve "
                        "guard's hooks")


def guarded(config, account, plan, layout):
    """The checks of a guarded launch that read the Codex configuration and the guard's directory, all
    before a missing account is offered a login. Returns (guard directory, marker, account record)."""
    off = storage.hooks_off(guard, layout.root, plan.cwd, plan.profile)
    if off is not None:
        refuse(account, f"{off} turns the hooks feature off under [features], so Codex would not run the usage "
                        "reserve guard's hooks")
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
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        raw = stream.read(BLOCKED_LIMIT)
    return CONTROL.sub("", raw.decode("utf-8", "replace")).strip() or None


def unenforced(layout, plan, state, snapshot, owner, started, ended):
    """Which of the guard's hooks this run shows that Codex did not run: "the guard's hooks", "the
    guard's tool hook" or None. A tool call needs the tool hook; a model request (SNAPSHOT, a
    rate-limit snapshot in this run's rollouts) needs either hook."""
    prompt, tool = (os.path.lexists(f"{state}.ran.{kind}") for kind in ("prompt", "tool"))
    if tool:
        return None
    if usage.ran_tools(layout.root, plan.continued, owner, started, ended):
        return "the guard's tool hook" if prompt else "the guard's hooks"
    return "the guard's hooks" if snapshot and not prompt else None


def after_guarded(layout, plan, account, state, marker, snapshot, baseline, started):
    """After a guarded run: the guard's latest block message of a headless run, which Codex itself
    does not show; then, when the run shows that Codex did not run the guard's hooks, the warning,
    and the marker goes, so headless runs wait until an interactive launch has trusted them again."""
    ended = time.time()
    if plan.form == "headless":
        message = blocked_message(state)
        if message:
            print(guard.styled(message, "31", 2), file=sys.stderr)
    try:
        raw = guard.credential(layout.target)
    except (OSError, ValueError):
        raw = baseline
    try:
        missing = unenforced(layout, plan, state, snapshot, storage.owner(raw) if raw else None, started, ended)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        return  # what the run did cannot be told
    if missing:
        guard.warn(f"{account}: the usage reserve was not enforced in this run: Codex did not run {missing} (not "
                   f"trusted, disabled, or managed hooks only). Start codex-as {account} interactively and trust "
                   "them in \"Hooks need review\".")
        try:
            os.unlink(marker)
        except OSError:
            pass


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
        if layout.acquire():
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
        # One start time for the guard, the record of this run and the check after it.
        started, state = int(time.time()), None
        if guarding:
            guard_launch.prune(directory, started)
            state = guard_launch.prepare(env, directory, "codex-as", account, policy, record, started, marker=marker)
            print(guard.styled("codex-as: " + policy.describe() + "; hooks refuse new prompts and tool calls at a "
                               "reserve.", "36", 2), file=sys.stderr)
    except BaseException:
        layout.close()
        raise
    with Supervision():
        try:
            try:
                result = run_native(command, env, layout.fd)
            finally:
                layout.close()
            # Use a fresh open-file description: surviving descendants still hold SH.
            post_run(layout, baseline)
            snapshot = os.path.lexists(layout.target) and remember(config, layout, plan=plan, started=started)
            if state is not None:
                after_guarded(layout, plan, account, state, marker, snapshot, baseline, started)
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
