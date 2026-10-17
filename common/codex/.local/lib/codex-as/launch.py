"""Supervise native Codex with fixed account authentication and shared state."""

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile


def load_module(name):
    spec = importlib.util.spec_from_file_location("codex_as_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_module("check")
storage = load_module("layout")
handoff = load_module("handoff")


def enroll(layout, codex):
    if os.path.lexists(layout.target):
        guard.credential(layout.target)
        return
    tty = guard.terminal()
    try:
        tty.write("Existing accounts: " + (", ".join(guard.accounts()) or "(none)") + "\n")
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


def main(args):
    if not args or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", args[0]):
        guard.fail("invalid account name (ASCII letters, digits, _ or -; no leading -).")
    account, *native_args = args
    plan = guard.arguments(native_args)
    codex = shutil.which("codex")
    if codex is None:
        guard.fail("Codex is not installed on PATH.")
    if plan.rootless:
        # Help, version, update and completion are the same for every login; an update
        # installs under CODEX_HOME, so it goes to the root home like plain codex update.
        env = os.environ.copy()
        env["CODEX_HOME"] = str(Path(os.environ["HOME"]) / ".codex")
        env.pop("CODEX_SQLITE_HOME", None)
        with Supervision():
            return run_native([codex, *native_args], env)
    guard.preflight(account, plan)
    layout = storage.Layout(guard, account)
    sqlite = storage.shared_sqlite(guard, layout.root, plan.cwd, plan.profile)
    layout.root_check()
    version = subprocess.run([codex, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             text=True, check=False)
    if version.returncode:
        return version.returncode
    guard.codex_version(version.stdout)
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
    with Supervision():
        try:
            result = run_native(command, env, layout.fd)
        finally:
            layout.close()
        # Use a fresh open-file description: surviving descendants still hold SH.
        post_run(layout, baseline)
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
