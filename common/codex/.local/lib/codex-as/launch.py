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


def run_native(command, env, fd=None):
    child = subprocess.Popen(command, env=env, pass_fds=() if fd is None else (fd,))
    previous = {}

    def forward(signum, frame):
        if child.poll() is None:
            child.send_signal(signum)

    try:
        # Terminal Ctrl-C reaches both processes; do not send it twice.
        for signum, handler in ((signal.SIGINT, lambda signum, frame: None),
                                (signal.SIGTERM, forward), (signal.SIGHUP, forward)):
            previous[signum] = signal.signal(signum, handler)
        result = child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return result if result >= 0 else 128 - result


def main(args):
    operation = None
    if args and args[0] in {"--migrate", "--recover"}:
        operation = args.pop(0)
        if len(args) != 1:
            guard.fail("usage: codex-as --migrate NAME | --recover NAME.")
    if not args or not re.fullmatch(r"[A-Za-z0-9_-]+", args[0]):
        guard.fail("invalid account name (use ASCII letters, digits, _ or -).")
    account, *native_args = args
    mode, cwd, resume_id = guard.preflight(account, native_args)
    layout = storage.Layout(guard, account)
    sqlite = storage.shared_sqlite(guard, layout.root, cwd)
    layout.root_check(initializing=operation == "--migrate")
    codex = shutil.which("codex")
    if codex is None:
        guard.fail("Codex is not installed on PATH.")
    version = subprocess.run([codex, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             text=True, check=False)
    if version.returncode:
        return version.returncode
    guard.codex_version(version.stdout)
    if native_args:
        # The static guard ran above; also refuse subcommands this release adds.
        guard.arguments(native_args, guard.native_commands(codex))
    query = native_args in (["--help"], ["-h"], ["--version"], ["-V"])
    env = os.environ.copy()
    env["CODEX_HOME"] = str(layout.root if query else layout.home)
    env["CODEX_SQLITE_HOME"] = str(sqlite)
    command = [codex, "-c", 'cli_auth_credentials_store="file"']
    command += ["-c", "sqlite_home=" + json.dumps(str(sqlite), ensure_ascii=False)]
    if mode == "interactive":
        command.append("--no-daemon")
    command.extend(native_args)
    if query:
        return run_native(command, env)
    if operation != "--migrate":
        layout.layout_check()
    # Reject malformed existing secrets before creating runtime metadata.
    if os.path.lexists(layout.target):
        guard.credential(layout.target)
    baseline = None
    try:
        exclusive = layout.acquire(exclusive=operation is not None)
        if operation == "--migrate":
            layout.migrate()
            return 0
        if operation == "--recover":
            layout.recover()
            return 0
        if exclusive:
            enroll(layout, codex)
            layout.prepare()
            fcntl.flock(layout.fd, fcntl.LOCK_SH)
        else:
            if not layout.auth.is_symlink():
                guard.fail("account has active clients and authentication needs repair; close all account clients first.")
        # Revalidate after EX->SH, whose conversion may release the lock briefly.
        baseline = layout.validate()
        handoff.prepare(guard, layout.root, account, resume_id)
        result = run_native(command, env, layout.fd)
    finally:
        layout.close()
    # Use a fresh open-file description: surviving descendants still hold SH.
    replaced = "Codex replaced the account auth.json link with a regular file; "
    try:
        layout.acquire(exclusive=True)
        unlinked = os.path.lexists(layout.auth) and not layout.auth.is_symlink()
        layout.repair(baseline)
        layout.validate()
        if unlinked:
            guard.warn(replaced + "credentials were imported into secrets and the link restored.")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as error:
        if not (type(error) is ValueError and "active clients" in str(error)):
            guard.report(error)
        elif os.path.lexists(layout.auth) and not layout.auth.is_symlink():
            guard.warn(replaced + "the last client of this account to exit repairs it.")
    finally:
        layout.close()
    return result


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as error:
        guard.report(error)
        sys.exit(1)
    except KeyboardInterrupt:
        print(guard.styled("codex-as: cancelled; credentials preserved.", "33", 2), file=sys.stderr)
        sys.exit(130)
