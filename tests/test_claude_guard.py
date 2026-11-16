"""claude-as with a usage reserve: the guarded launch and the launch without one.

A launch with a reserve on passes Claude Code the guard's hooks and status line
in one --settings argument, exports the launch to the guard, keeps the session
out of Claude Code's background process and refuses every form the guard cannot
cover. A launch with both reserves off stays exactly as it was. The real
launcher runs from a kit copy in a temporary HOME with a fake claude that
records its argv and environment; a guarded launch needs a terminal on standard
output, so those launches run with standard output on a pseudo-terminal.

RealClaude runs the guard's hooks in the real Claude Code when one at or above
the guard's minimum is on PATH (it skips with the reason otherwise): offline,
against the loopback fake tests/fixtures/guard/claude_api.py, in a temporary
HOME and configuration directory with an environment built from scratch, git
and ssh shims first on PATH and every proxy at a closed loopback port.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

from test_codex_as import ENV_CONFLICTS, KIT_KNOBS


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "sk-ant-oat01-" + "fabricated-guard-claude-" * 3
VARIABLES = ("CODING_AGENT_KIT_GUARD", "CODING_AGENT_KIT_GUARD_SCRIPT", "CODING_AGENT_KIT_GUARD_PYTHON")
USAGE_VARIABLES = ("CODING_AGENT_KIT_USAGE_FILE", "CODING_AGENT_KIT_TOKEN_ID", "CODING_AGENT_KIT_CLAUDE_PID")
CLOSED = {"CLAUDE_DISABLE_ADOPT": "1", "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1"}  # set by a guarded launch only
RESUME = "CLAUDE_CODE_RESUME_INTERRUPTED_TURN"  # removed by a guarded launch only
LAUNCH_LINE = "; hooks refuse new prompts and tool calls at a reserve.\n"
WITHOUT = ("; to launch without usage reserves, run claude-as --reserve-5h off --reserve-weekly off {account} "
           "with the same arguments.\n")
EARLIER = ("claude-as: work: an earlier launch ran without its usage reserve guard: "
           "Claude Code did not run the injected hooks.\n")
DAY = 86400
GONE = 4_194_400  # a process id above every host's limit: a launch that has ended
MARKER = "codex-hooks-90aeb3ec484a2ca6"
# Records argv and environment, never a token; one JSON line per run. --version prints
# CLAUDE_FAKE_VERSION and leaves a line in FAKE_VERSIONS.
FAKE = r'''
import hashlib, json, os, sys
if sys.argv[1:] == ["--version"]:
    with open(os.environ["FAKE_VERSIONS"], "a") as stream:
        stream.write("--version\n")
    print(os.environ.get("CLAUDE_FAKE_VERSION", "2.1.296 (Claude Code)"))
    sys.exit(0)
env = dict(os.environ)
token = env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
record = {"argv": sys.argv, "env": env, "pid": os.getpid(), "tty": os.isatty(1),
          "token": token and hashlib.sha256(token.encode()).hexdigest()}
with open(os.environ["FAKE_RECORD"], "a") as stream:
    stream.write(json.dumps(record) + "\n")
print("stdout sentinel")
'''


def load_launch():
    spec = importlib.util.spec_from_file_location("claude_as_launch_under_test", ROOT / "lib/claude-as/launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launch = load_launch()


def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def drain(fd):
    """Everything still readable from the pseudo-terminal master FD."""
    os.set_blocking(fd, False)
    output = bytearray()
    while True:
        try:
            chunk = os.read(fd, 4096)
        except (BlockingIOError, OSError):
            break
        if not chunk:
            break
        output.extend(chunk)
    return output.decode("utf-8", "replace")


def settings_of(command, padding=0):
    """The --settings JSON of a guarded launch whose hook command is COMMAND (C3)."""
    def hook(kind, block):
        handler = {"type": "command", "command": f"{command} {kind}", "timeout": 20}
        return handler | ({"onFailure": "block"} if block else {})
    return {"statusLine": {"type": "command", "command": f"{command} status", "padding": padding},
            "hooks": {"SessionStart": [{"hooks": [hook("start", False)]}],
                      "UserPromptSubmit": [{"hooks": [hook("prompt", True)]}],
                      "PreToolUse": [{"matcher": "*", "hooks": [hook("tool", True)]}],
                      "Stop": [{"hooks": [hook("stop", False)]}]}}


class Fixture(unittest.TestCase):
    def setUp(self):
        # The loader checks the directories on the way to a configuration file.
        self.addCleanup(os.umask, os.umask(0o022))
        self.tmp = tempfile.TemporaryDirectory(prefix="claude-guard-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.kit = self.install(self.base / "kit with spaces")
        self.home = self.base / "home with spaces"
        self.home.mkdir(mode=0o700)
        (self.home / ".claude").mkdir(mode=0o700)
        (self.home / ".secrets").mkdir(mode=0o700)
        write(self.home / ".secrets/claude-work", TOKEN + "\n")
        self.fakes = self.base / "fake bin"
        write(self.fakes / "claude", f"#!{sys.executable}\n" + FAKE, 0o700)
        self.cwd = self.base / "project dir"
        self.cwd.mkdir()
        self.record = self.base / "record.jsonl"
        self.versions = self.base / "versions"
        self.state = self.home / ".local/state/coding-agent-kit"
        self.guard_dir = self.state / "guard"
        self.usage = self.state / "accounts/claude-work.usage.json"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("ANTHROPIC_", "CLAUDE"))}
        for name in (*KIT_KNOBS, *ENV_CONFLICTS, "PYTHONPATH", "BASH_ENV", "ENV", "ZDOTDIR"):
            self.env.pop(name, None)
        self.env.update(HOME=str(self.home), FAKE_RECORD=str(self.record), FAKE_VERSIONS=str(self.versions),
                        PATH=os.pathsep.join((str(self.fakes), self.env.get("PATH", "/usr/bin:/bin"))))

    @staticmethod
    def install(target):
        for part in ("bin", "lib", "docs"):
            shutil.copytree(ROOT / part, target / part)
        return target

    def run_launcher(self, args, env=None, tty=True, kit=None):
        """(exit code, stdout, stderr) of claude-as ARGS; standard output on a pseudo-terminal when TTY.
        The launcher runs in a session of its own, without a controlling terminal."""
        command = [str((kit or self.kit) / "bin/claude-as"), *args]
        master = slave = None
        if tty:
            master, slave = pty.openpty()
        try:
            result = subprocess.run(command, cwd=self.cwd, env=self.env | (env or {}), stdin=subprocess.DEVNULL,
                                    stdout=slave if tty else subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
                                    start_new_session=True)
            # Read while this end of the terminal is still open: some systems drop what is buffered once
            # every slave descriptor has closed. The fake writes far less than a terminal buffers.
            out = drain(master) if tty else result.stdout.decode("utf-8", "replace")
        finally:
            if tty:
                os.close(slave)
                os.close(master)
        err = result.stderr.decode("utf-8", "replace")
        self.assertNotIn(TOKEN, out + err)
        return result.returncode, out, err

    def records(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def version_runs(self):
        return self.versions.read_text().count("--version\n") if self.versions.exists() else 0

    def configure(self, text):
        path = write(self.home / ".config/coding-agent-kit/config.toml", text)
        for directory in (path.parent, path.parent.parent):
            directory.chmod(0o700)
        return path

    def user_settings(self, data):
        return write(self.home / ".claude/settings.json", json.dumps(data))

    def project_settings(self, data, local=False):
        return write(self.cwd / ".claude" / ("settings.local.json" if local else "settings.json"), json.dumps(data))

    def guarded(self, args, env=None, kit=None):
        """A guarded launch that must succeed: its record, its launch JSON and its stderr."""
        count = len(self.records())
        before = int(time.time())
        code, out, err = self.run_launcher(args, env, kit=kit)
        after = int(time.time())
        self.assertEqual((code, out.replace("\r\n", "\n")), (0, "stdout sentinel\n"), err)
        records = self.records()
        self.assertEqual(len(records), count + 1, err)
        record = records[-1]
        self.assertTrue(record["tty"])
        value = json.loads(record["env"]["CODING_AGENT_KIT_GUARD"])
        self.assertTrue(before <= value["started"] <= after, (before, value["started"], after))
        return record, value, err


class Unguarded(Fixture):
    """A launch with both reserves off is the launch of a kit without reserves."""

    def test_no_guard_is_passed_on_and_nothing_else_changes(self):
        inherited = {name: "inherited" for name in VARIABLES}
        for config, flags in ((None, []), ('[usage_reserve]\nfive_hour = "off"\nweekly = "off"\n', []),
                              ('[usage_reserve]\nfive_hour = 10\nweekly = 10\n',
                               ["--reserve-5h", "off", "--reserve-weekly", "off"])):
            for tty in (True, False):
                with self.subTest(config=config, flags=flags, tty=tty):
                    if config is not None:
                        self.configure(config)
                    plain = self.run_launcher([*flags, "work", "--model", "x", "-p", "hi"], {RESUME: "1"}, tty=tty)
                    with_guard = self.run_launcher([*flags, "work", "--model", "x", "-p", "hi"],
                                                   {RESUME: "1", **inherited}, tty=tty)
                    self.assertEqual(plain[0], 0, plain[2])
                    self.assertEqual(plain, with_guard)
                    self.assertEqual(plain[2], "")
                    first, second = self.records()[-2:]
                    self.assertEqual(first["argv"], [str(self.fakes / "claude"), "--model", "x", "-p", "hi"])
                    for record in (first, second):
                        self.assertEqual([name for name in (*VARIABLES, *CLOSED) if name in record["env"]], [])
                        self.assertEqual(record["env"][RESUME], "1")
                        self.assertEqual(record["env"]["CODING_AGENT_KIT_USAGE_FILE"], str(self.usage))
                        self.assertEqual(record["env"]["CODING_AGENT_KIT_CLAUDE_PID"], str(record["pid"]))
                    del first["env"]["CODING_AGENT_KIT_CLAUDE_PID"], second["env"]["CODING_AGENT_KIT_CLAUDE_PID"]
                    del first["pid"], second["pid"]
                    self.assertEqual(first, second)
                    self.assertFalse(self.guard_dir.exists())
                    self.assertEqual(self.version_runs(), 0, "a launch without reserves ran claude --version")

    def test_without_reserves_the_forms_a_guard_refuses_stay_as_they_were(self):
        """-p and a piped standard output launch; --init-only, --bg and --background stay unsupported options."""
        code, out, err = self.run_launcher(["work", "agents"], tty=False)
        self.assertEqual((code, out, err), (0, "stdout sentinel\n", ""))
        for word in ("--init-only", "--bg", "--background"):
            with self.subTest(word=word):
                code, out, err = self.run_launcher(["work", word])
                self.assertEqual((code, out, err), (1, "", "claude-as: unsupported/combined option; authentication "
                                                    "safety is only audited for the supported local CLI options.\n"))
        self.user_settings({"disableAllHooks": True, "env": {RESUME: "1"}})
        code, out, err = self.run_launcher(["work", "--settings", "{}"], tty=False)
        self.assertEqual((code, out, err), (0, "stdout sentinel\n", ""))
        self.assertEqual(self.records()[-1]["argv"][1:], ["--settings", "{}"])


class Guarded(Fixture):
    """A launch with a reserve on: hooks injected, the launch exported, the background closed."""

    def assert_guarded(self, record, value, args, five_hour, weekly, kit=None):
        kit = kit or self.kit
        python, script = record["env"]["CODING_AGENT_KIT_GUARD_PYTHON"], record["env"]["CODING_AGENT_KIT_GUARD_SCRIPT"]
        self.assertEqual(script, str(kit / "lib/coding-agent-kit/guard.py"))
        self.assertTrue(os.path.isabs(python))
        self.assertEqual(os.path.realpath(python), os.path.realpath(shutil.which("python3", path=self.env["PATH"])))
        command = shlex.join([python, "-I", "-B", "-S", script])
        self.assertEqual(record["argv"][:2], [str(self.fakes / "claude"), "--settings"])
        self.assertEqual(record["argv"][3:], args)
        settings = json.loads(record["argv"][2])
        self.assertEqual(settings, settings_of(command, settings["statusLine"]["padding"]))
        commands = [settings["statusLine"]["command"]] + [hook["command"] for groups in settings["hooks"].values()
                                                          for group in groups for hook in group["hooks"]]
        self.assertEqual([shlex.split(text) for text in commands],
                         [[python, "-I", "-B", "-S", script, kind] for kind in ("status", "start", "prompt", "tool", "stop")])
        state = self.guard_dir / f"claude-as-work-{record['pid']}-{value['started']}"
        expected = {"v": 1, "tool": "claude-as", "account": "work", "five_hour": five_hour, "weekly": weekly,
                    "started": value["started"], "usage": str(self.usage), "state": str(state)}
        self.assertEqual({key: value[key] for key in value if key != "statusline"}, expected)
        self.assertEqual(value["usage"], record["env"]["CODING_AGENT_KIT_USAGE_FILE"])
        self.assertEqual(stat.S_IMODE(self.guard_dir.lstat().st_mode), 0o700)
        self.assertTrue(Path(str(state) + ".launched").is_file())
        for name, text in CLOSED.items():
            self.assertEqual(record["env"][name], text)
        self.assertNotIn(RESUME, record["env"])
        self.assertEqual(record["token"], hashlib.sha256(TOKEN.encode()).hexdigest())
        return settings

    def test_a_reserve_by_flag(self):
        args = ["--model", "x", "--continue"]
        record, value, err = self.guarded(["--reserve-5h", "10", "work", *args], {RESUME: "1"})
        self.assert_guarded(record, value, args, "10", None)
        self.assertNotIn("statusline", value)
        self.assertEqual(err, "claude-as: usage reserves for work: five-hour 10% (--reserve-5h), weekly off (default)"
                         + LAUNCH_LINE)
        # Inherited guard variables are replaced by this launch's.
        inherited = {name: "/inherited" for name in VARIABLES} | CLOSED | {"CLAUDE_DISABLE_ADOPT": "0"}
        record, value, err = self.guarded(["--reserve-weekly", "0", "work"], inherited)
        self.assert_guarded(record, value, [], None, "0")

    def test_a_reserve_by_configuration(self):
        self.configure('[usage_reserve]\nfive_hour = 20\n[usage_reserve.claude_accounts.work]\nweekly = 12.5\n')
        record, value, err = self.guarded(["work"])
        self.assert_guarded(record, value, [], "20", "12.5")
        self.assertEqual(err, "claude-as: usage reserves for work: five-hour 20% (usage_reserve), "
                         "weekly 12.5% (usage_reserve.claude_accounts.work)" + LAUNCH_LINE)
        # A flag that turns one window off leaves the other guarded.
        record, value, err = self.guarded(["--reserve-5h", "off", "work", "-c"])
        self.assert_guarded(record, value, ["-c"], None, "12.5")

    def test_the_users_status_line_is_chained_with_its_padding(self):
        user = "printf '%s' \"it's the user's\""
        cases = (({"statusLine": {"type": "command", "command": user, "padding": 2}}, None, None, user, 2),
                 ({"statusLine": {"type": "command", "command": user}},
                  {"statusLine": {"type": "command", "command": "project status", "padding": 1}}, None,
                  "project status", 1),
                 ({"statusLine": {"type": "command", "command": user, "padding": 2}},
                  {"statusLine": {"type": "command", "command": "project status", "padding": 1}},
                  {"statusLine": {"type": "command", "command": "local status"}}, "local status", 0),
                 ({}, None, None, None, 0))
        for user_data, project, local, command, padding in cases:
            with self.subTest(command=command):
                self.user_settings(user_data)
                for data, is_local in ((project, False), (local, True)):
                    path = self.cwd / ".claude" / ("settings.local.json" if is_local else "settings.json")
                    path.unlink(missing_ok=True)
                    if data is not None:
                        self.project_settings(data, local=is_local)
                record, value, _ = self.guarded(["--reserve-weekly", "10", "work"])
                settings = self.assert_guarded(record, value, [], None, "10")
                self.assertEqual(value.get("statusline"), command)
                self.assertEqual(settings["statusLine"]["padding"], padding)

    def test_a_kit_path_with_a_space_and_a_quote_runs_the_hooks(self):
        """Review Focus 1: the injected commands run through /bin/sh, as Claude Code runs them."""
        kit = self.install(self.base / "kit o'clock dir")
        record, value, _ = self.guarded(["--reserve-5h", "10", "work"], kit=kit)
        settings = self.assert_guarded(record, value, [], "10", None, kit=kit)
        env = {name: record["env"][name] for name in (*VARIABLES, "PATH", "HOME")}
        state = value["state"]
        for kind, event in (("start", {"hook_event_name": "SessionStart"}),
                            ("prompt", {"hook_event_name": "UserPromptSubmit", "prompt": "hi"})):
            group = "SessionStart" if kind == "start" else "UserPromptSubmit"
            command = settings["hooks"][group][0]["hooks"][0]["command"]
            result = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(event).encode(), env=env,
                                    capture_output=True, timeout=30)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""), kind)
            self.assertTrue(Path(f"{state}.ran.{kind}").is_file(), kind)
        result = subprocess.run(["/bin/sh", "-c", settings["statusLine"]["command"]], input=b"{}", env=env,
                                capture_output=True, timeout=30)
        self.assertEqual((result.returncode, result.stderr), (0, b""))

    def test_help_and_version_are_neither_refused_nor_injected(self):
        self.configure('[usage_reserve]\nfive_hour = 10\nweekly = 10\n')
        for args in (["work", "--help"], ["work", "-h"], ["--reserve-5h", "5", "work", "-v"]):
            for tty in (True, False):
                with self.subTest(args=args, tty=tty):
                    code, out, err = self.run_launcher(args, {**{name: "x" for name in VARIABLES}, RESUME: "1"}, tty=tty)
                    self.assertEqual((code, err), (0, ""))
                    record = self.records()[-1]
                    self.assertEqual(record["argv"], [str(self.fakes / "claude"), args[-1]])
                    self.assertEqual([name for name in (*VARIABLES, *CLOSED) if name in record["env"]], [])
                    self.assertEqual(record["env"][RESUME], "1")
                    self.assertIsNone(record["token"])
        for args in (["work", "--version"], ["--reserve-weekly", "10", "missing", "--version"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_launcher(args, tty=False), (0, "2.1.296 (Claude Code)\n", ""))
        self.assertFalse(self.guard_dir.exists())
        self.assertFalse((self.home / ".secrets/claude-missing").exists())

    def test_a_kit_without_guard_py_is_incomplete_only_for_a_launch_with_a_reserve(self):
        (self.kit / "lib/coding-agent-kit/guard.py").unlink()
        for args in (["--reserve-5h", "10", "work"], ["--reserve-weekly", "10", "missing"]):
            with self.subTest(args=args):
                self.assertEqual(self.run_launcher(args), (1, "", f"claude-as: incomplete install at {self.kit}\n"))
        self.assertEqual(self.records(), [])
        self.assertFalse(self.guard_dir.exists())
        self.assertFalse((self.home / ".secrets/claude-missing").exists())
        code, out, err = self.run_launcher(["work"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.records()[-1]["argv"], [str(self.fakes / "claude")])

    def test_old_launch_state_is_pruned_and_markers_stay(self):
        now = int(time.time())
        self.guard_dir.mkdir(mode=0o700, parents=True)
        old = [f"claude-as-work-123-{now - 31 * DAY}.launched", f"claude-as-work-123-{now - 31 * DAY}.ran.prompt",
               f"codex-as-A-124-{now - 40 * DAY}.launched", f"claude-as-other-125-{now - 31 * DAY}.seen"]
        kept = [f"claude-as-work-126-{now - 29 * DAY}.launched", f"claude-as-work-126-{now - 29 * DAY}.ran.start",
                MARKER, "unrelated file"]
        for name in (*old, *kept):
            write(self.guard_dir / name, "x")
        record, value, err = self.guarded(["--reserve-5h", "10", "work"])
        names = set(os.listdir(self.guard_dir))
        self.assertEqual(names & set(old), set())
        self.assertLessEqual(set(kept), names)
        self.assertNotIn(EARLIER, err)

    def test_an_earlier_launch_without_hooks_is_reported_once(self):
        started = int(time.time()) - 600
        self.guard_dir.mkdir(mode=0o700, parents=True)
        earlier = f"claude-as-work-{GONE}-{started}"
        write(self.guard_dir / f"{earlier}.launched", os.uname().nodename)
        write(self.guard_dir / f"{earlier}.seen", "")
        # Another account's and a guarded earlier launch are not reported.
        write(self.guard_dir / f"claude-as-other-{GONE}-{started}.launched", os.uname().nodename)
        write(self.guard_dir / f"claude-as-other-{GONE}-{started}.seen", "")
        write(self.guard_dir / f"claude-as-work-{GONE - 1}-{started}.launched", os.uname().nodename)
        write(self.guard_dir / f"claude-as-work-{GONE - 1}-{started}.seen", "")
        write(self.guard_dir / f"claude-as-work-{GONE - 1}-{started}.ran.start", "")
        _, _, err = self.guarded(["--reserve-5h", "10", "work"])
        self.assertTrue(err.startswith(EARLIER), err)
        self.assertTrue(err.endswith(LAUNCH_LINE), err)
        self.assertEqual(err.count("\n"), 2)
        self.assertFalse((self.guard_dir / f"{earlier}.launched").exists())
        self.assertTrue((self.guard_dir / f"claude-as-other-{GONE}-{started}.seen").exists())
        _, _, err = self.guarded(["--reserve-5h", "10", "work"])
        self.assertNotIn(EARLIER, err)

    def test_a_guard_directory_that_others_could_change_refuses(self):
        self.guard_dir.mkdir(mode=0o700, parents=True)
        self.guard_dir.chmod(0o755)
        code, out, err = self.run_launcher(["--reserve-5h", "10", "work"])
        self.assertEqual((code, out, err),
                         (1, "", f"claude-as: {self.guard_dir}: must be an owned, real directory with permissions 0700.\n"))
        self.assertEqual(self.records(), [])


class Refusals(Fixture):
    """Every form the guard cannot cover is refused before Claude Code starts, naming the way out."""

    def assert_refused(self, args, reason, env=None, tty=True, account="work"):
        count = len(self.records())
        code, out, err = self.run_launcher(args, env, tty=tty)
        self.assertEqual((code, out, err), (1, "", f"claude-as: {account}: {reason}" + WITHOUT.format(account=account)))
        self.assertEqual(len(self.records()), count, "a refused launch started Claude Code")

    def test_forms_outside_the_interactive_session(self):
        outside = "runs Claude Code outside its interactive session, where the usage reserve guard cannot stop it"
        background = ("hands the session to Claude Code's background process, which runs it with the environment and "
                      "credential of whichever launch started that process")
        elsewhere = "runs or joins sessions in Claude Code's background process, outside this launch's usage reserve guard"
        cases = ((["-p", "hi"], f"-p {outside}"), (["--print", "hi"], f"--print {outside}"),
                 (["--model", "x", "--verbose", "-p", "hi"], f"-p {outside}"), (["--init-only"], f"--init-only {outside}"),
                 (["--bg"], f"--bg {background}"), (["--background", "task"], f"--background {background}"),
                 (["--model", "x", "--bg"], f"--bg {background}"), (["--verbose", "--background"], f"--background {background}"),
                 (["--bg=x"], f"--bg {background}"),
                 (["agents"], f"claude agents {elsewhere}"), (["attach", "id"], f"claude attach {elsewhere}"),
                 (["--verbose", "respawn"], f"claude respawn {elsewhere}"), (["--", "attach"], f"claude attach {elsewhere}"),
                 (["--settings", "x"], "a --settings of your own cannot be combined with usage reserves, whose hooks "
                                       "claude-as passes in its own --settings"),
                 (["--settings={}"], "a --settings of your own cannot be combined with usage reserves, whose hooks "
                                     "claude-as passes in its own --settings"))
        for flags in (["--reserve-5h", "10"], ["--reserve-weekly", "0"]):
            for args, reason in cases:
                with self.subTest(flags=flags, args=args):
                    self.assert_refused([*flags, "work", *args], reason)
        self.configure("[usage_reserve]\nweekly = 10\n")
        self.assert_refused(["work", "-p", "hi"], f"-p {outside}")
        self.assert_refused(["work"], "standard output is not a terminal, so Claude Code would run outside its "
                            "interactive session, where the usage reserve guard cannot stop it", tty=False)
        self.assertEqual(self.version_runs(), 0)
        self.assertFalse(self.guard_dir.exists())

    def test_settings_that_stop_the_hooks(self):
        self.configure("[usage_reserve]\nfive_hour = 10\n")
        user = self.home / ".claude/settings.json"
        project = self.cwd / ".claude/settings.json"
        local = self.cwd / ".claude/settings.local.json"
        for path, data, reason in (
                (user, {"disableAllHooks": True}, f"{user} sets disableAllHooks"),
                (project, {"disableAllHooks": True}, f"{project} sets disableAllHooks"),
                (local, {"disableAllHooks": True}, f"{local} sets disableAllHooks")):
            with self.subTest(path=path):
                write(path, json.dumps(data))
                self.assert_refused(["work"], reason + ", so Claude Code would not run the usage reserve guard's hooks")
                path.unlink()
        # A value Claude Code does not read as on (its isEnvTruthy: 1, true, yes or on) reopens the background.
        for name, text in ((RESUME, "1"), ("CLAUDE_DISABLE_ADOPT", "0"), ("CLAUDE_CODE_DISABLE_AGENT_VIEW", ""),
                           ("CLAUDE_DISABLE_ADOPT", "maybe"), ("CLAUDE_DISABLE_ADOPT", 0),
                           ("CLAUDE_CODE_DISABLE_AGENT_VIEW", False), ("CLAUDE_DISABLE_ADOPT", None)):
            with self.subTest(name=name, text=text):
                write(user, json.dumps({"env": {name: text}}))
                self.assert_refused(["work"], f"{user} sets {name} in its env, which would reopen what a launch with "
                                    "usage reserves closes")
        # Values that keep the hooks and the background closed launch.
        write(user, json.dumps({"disableAllHooks": False, "allowManagedHooksOnly": True, "env": CLOSED}))
        self.guarded(["work"])
        for text in ("true", " ON ", "Yes", 1, True):
            with self.subTest(closed=text):
                write(user, json.dumps({"env": {name: text for name in CLOSED}}))
                self.guarded(["work"])

    def test_safe_mode_and_bare_mode_stop_the_hooks(self):
        """Claude Code runs none of the --settings hooks in its safe mode or its bare mode (RealClaude pins it)."""
        self.configure("[usage_reserve]\nweekly = 10\n")
        user = self.home / ".claude/settings.json"
        project = self.cwd / ".claude/settings.json"
        local = self.cwd / ".claude/settings.local.json"
        on = ("1", "true", " Yes ", "on", "maybe", 1, True, ["1"])
        off = ("", "0", "false", "No", "off", 0, False, None)
        for name, mode in (("CLAUDE_CODE_SAFE_MODE", "safe mode"), ("CLAUDE_CODE_SIMPLE", "bare mode")):
            reason = f"the switch of Claude Code's {mode}, which runs none of the usage reserve guard's hooks"
            for text in on:
                if isinstance(text, str):
                    with self.subTest(name=name, environment=text):
                        self.assert_refused(["work"], f"the environment sets {name}, {reason}", {name: text})
                for path in (user, project, local):
                    with self.subTest(name=name, path=path, text=text):
                        write(path, json.dumps({"env": {name: text}}))
                        self.assert_refused(["work"], f"{path} sets {name} in its env, {reason}")
                        path.unlink()
            for text in off:
                with self.subTest(name=name, off=text):
                    write(user, json.dumps({"env": {name: text}}))
                    if isinstance(text, str):
                        record, _, _ = self.guarded(["work"], {name: text})
                        self.assertEqual(record["env"][name], text)
                    else:
                        self.guarded(["work"])
        self.assertEqual(self.version_runs(), len(off) * 2)
        # Without reserves both still launch, as they did.
        code, out, err = self.run_launcher(["--reserve-weekly", "off", "work"], {"CLAUDE_CODE_SAFE_MODE": "1"})
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.records()[-1]["env"]["CLAUDE_CODE_SAFE_MODE"], "1")

    def test_the_cached_server_policy_is_not_checked(self):
        """remote-settings.json describes whichever account ran last; a policy that stops the hooks shows as no .ran.*"""
        write(self.home / ".claude/remote-settings.json",
              json.dumps({"allowManagedHooksOnly": True, "disableAllHooks": True, "env": {RESUME: "1"}}))
        record, value, _ = self.guarded(["--reserve-5h", "10", "work"])
        self.assertNotIn(RESUME, record["env"])

    def test_a_claude_code_older_than_the_guard_minimum(self):
        self.assertEqual(launch.GUARD_MINIMUM, (2, 1, 296))
        for version, reason in (("2.1.295 (Claude Code)", "Claude Code 2.1.295 is older than 2.1.296, the first "
                                                          "release whose hooks can enforce usage reserves"),
                                ("1.9.999", "Claude Code 1.9.999 is older than 2.1.296, the first release whose hooks "
                                            "can enforce usage reserves"),
                                ("Claude Code", "claude --version printed no Claude Code version"),
                                ("", "claude --version printed no Claude Code version")):
            with self.subTest(version=version):
                self.assert_refused(["--reserve-5h", "10", "work"], reason, {"CLAUDE_FAKE_VERSION": version})
        # Before a missing account is offered for enrolment.
        self.assert_refused(["--reserve-5h", "10", "missing"], "Claude Code 2.1.295 is older than 2.1.296, the first "
                            "release whose hooks can enforce usage reserves",
                            {"CLAUDE_FAKE_VERSION": "2.1.295 (Claude Code)"}, account="missing")
        self.assertFalse((self.home / ".secrets/claude-missing").exists())
        self.assertFalse(self.guard_dir.exists())
        for version in ("2.1.296 (Claude Code)", "2.1.325 (Claude Code)", "2.2.0 (Claude Code)", "10.0.0"):
            with self.subTest(version=version):
                self.guarded(["--reserve-5h", "10", "work"], {"CLAUDE_FAKE_VERSION": version})


class Settings(unittest.TestCase):
    """The pure parts of the guarded launch, in process."""

    def test_hook_keys_of_one_settings_source(self):
        self.assertEqual(launch.hook_keys({"disableAllHooks": True}), ["disableAllHooks"])
        self.assertEqual(launch.hook_keys({"disableAllHooks": True}, managed=True), ["disableAllHooks"])
        self.assertEqual(launch.hook_keys({"allowManagedHooksOnly": True}), [])
        self.assertEqual(launch.hook_keys({"allowManagedHooksOnly": True}, managed=True), ["allowManagedHooksOnly"])
        self.assertEqual(launch.hook_keys({"disableAllHooks": True, "allowManagedHooksOnly": True}, managed=True),
                         ["disableAllHooks", "allowManagedHooksOnly"])
        for value in (False, "true", 1, None, [True]):
            self.assertEqual(launch.hook_keys({"disableAllHooks": value, "allowManagedHooksOnly": value}, managed=True), [])
        self.assertEqual(launch.hook_keys({}), [])
        # A managed strictPluginOnlyCustomization that locks the hooks surface: true, or a list naming hooks.
        strict = "strictPluginOnlyCustomization"
        for value in (True, ["hooks"], ["skills", "hooks"]):
            self.assertEqual(launch.hook_keys({strict: value}, managed=True), [strict], value)
            self.assertEqual(launch.hook_keys({strict: value}), [], value)
        for value in (False, ["skills"], [], "hooks", {"hooks": True}, None):
            self.assertEqual(launch.hook_keys({strict: value}, managed=True), [], value)
        self.assertEqual(launch.hook_keys({"disableAllHooks": True, "allowManagedHooksOnly": True, strict: True},
                                          managed=True), ["disableAllHooks", "allowManagedHooksOnly", strict])

    def test_the_managed_files(self):
        for path, managed in (("/etc/claude-code/managed-settings.json", True),
                              ("/Library/Application Support/ClaudeCode/managed-settings.json", True),
                              ("/etc/claude-code/managed-settings.d/10-org.json", True),
                              ("/Library/Application Support/ClaudeCode/managed-settings.d/10-org.json", True),
                              ("/etc/claude-code/other.json", False), ("/home/u/.claude/settings.json", False),
                              ("/Library/Application Support/ClaudeCode/other.json", False)):
            self.assertEqual(launch.managed(Path(path)), managed, path)

    def test_the_managed_drop_in_directories_a_guarded_launch_reads(self):
        """Both platforms' drop-in directories for a launch with a reserve; a launch without one reads the one it read."""
        with tempfile.TemporaryDirectory(prefix="claude-guard-preflight-") as name:
            base = Path(name).resolve()
            linux, macos = base / "etc/managed-settings.d", base / "Library/managed-settings.d"
            for folder in (linux, macos):
                write(folder / "10-org.json", json.dumps({"allowManagedHooksOnly": True}))
                write(folder / "notes.txt", "{")
            home, cwd = base / "home", base / "project"
            cwd.mkdir()
            kit = type("Kit", (), {"home": home, "expand": staticmethod(Path)})()
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                with unittest.mock.patch.object(launch, "MANAGED_DIRECTORIES", (linux, macos)), \
                        unittest.mock.patch.object(launch, "MANAGED_FILES", ()), \
                        unittest.mock.patch.dict(os.environ, {"PATH": os.environ.get("PATH", "")}, clear=True):
                    guarded = launch.preflight([], kit, enrollment=False, guarded=True)
                    plain = launch.preflight([], kit, enrollment=False)
                    self.assertTrue(launch.managed(macos / "10-org.json"))
            finally:
                os.chdir(previous)
        self.assertEqual(sorted(path for path in guarded.sources if base in path.parents),
                         [macos / "10-org.json", linux / "10-org.json"])
        self.assertEqual([path for path in plain.sources if base in path.parents], [linux / "10-org.json"])

    def check_form(self, sources, environ=None):
        """launch.check_form of a Form with SOURCES and a terminal on standard output, in ENVIRON: the reason
        it refuses with, or None when it lets the launch go on."""
        form = launch.Form(sources={Path(path): data for path, data in sources.items()})
        with unittest.mock.patch.object(launch.os, "isatty", lambda fd: True), \
                unittest.mock.patch.dict(os.environ, environ or {}, clear=True):
            try:
                launch.check_form(form, "work")
            except ValueError as error:
                prefix, suffix = "work: ", WITHOUT.format(account="work")[:-1]
                text = str(error)
                self.assertTrue(text.startswith(prefix) and text.endswith(suffix), text)
                return text[len(prefix):-len(suffix)]
        return None

    def test_managed_settings_that_stop_the_hooks_refuse(self):
        hooks = "so Claude Code would not run the usage reserve guard's hooks"
        drop_ins = [f"{folder}/10-org.json" for folder in ("/etc/claude-code/managed-settings.d",
                                                           "/Library/Application Support/ClaudeCode/managed-settings.d")]
        for path in (*map(str, launch.MANAGED_FILES), *drop_ins):
            for data, key in (({"allowManagedHooksOnly": True}, "allowManagedHooksOnly"),
                              ({"disableAllHooks": True}, "disableAllHooks"),
                              ({"strictPluginOnlyCustomization": True}, "strictPluginOnlyCustomization"),
                              ({"strictPluginOnlyCustomization": ["hooks"]}, "strictPluginOnlyCustomization")):
                with self.subTest(path=path, key=key):
                    self.assertEqual(self.check_form({path: data}), f"{path} sets {key}, {hooks}")
            # A managed status line outranks the guard's --settings one, so the guard would never see usage.
            for line in ({"type": "command", "command": "org status"}, None):
                with self.subTest(path=path, statusLine=line):
                    self.assertEqual(self.check_form({path: {"statusLine": line}}),
                                     f"{path} sets statusLine, which Claude Code runs in place of the usage reserve "
                                     "guard's status line, so the guard would never see this account's usage")
            with self.subTest(path=path, env=True):
                self.assertEqual(self.check_form({path: {"env": {"CLAUDE_CODE_SAFE_MODE": "1"}}}),
                                 f"{path} sets CLAUDE_CODE_SAFE_MODE in its env, the switch of Claude Code's safe mode, "
                                 "which runs none of the usage reserve guard's hooks")
            self.assertIsNone(self.check_form({path: {"strictPluginOnlyCustomization": ["skills"], "env": CLOSED}}))
        # The managed-only keys and the status line count only in a managed file.
        user = "/home/u/.claude/settings.json"
        self.assertIsNone(self.check_form({user: {"allowManagedHooksOnly": True, "strictPluginOnlyCustomization": True,
                                                  "statusLine": {"type": "command", "command": "mine"}}}))
        self.assertEqual(self.check_form({user: {"disableAllHooks": True}}), f"{user} sets disableAllHooks, {hooks}")
        # The environment of the launch, too.
        self.assertEqual(self.check_form({}, {"CLAUDE_CODE_SIMPLE": "1"}),
                         "the environment sets CLAUDE_CODE_SIMPLE, the switch of Claude Code's bare mode, which runs "
                         "none of the usage reserve guard's hooks")
        self.assertIsNone(self.check_form({}, {"CLAUDE_CODE_SIMPLE": "0", "CLAUDE_CODE_SAFE_MODE": ""}))

    def test_how_claude_code_reads_a_flag(self):
        """Its isEnvTruthy: falsy values are off, a boolean is itself, else String(value) trimmed and lower-cased
        is one of 1, true, yes and on."""
        for value in ("1", "true", " TRUE ", "Yes", "on", 1, 1.0, True, ["1"], [["on"]]):
            self.assertTrue(launch.env_on(value), value)
        for value in ("", "0", "false", "no", "off", "maybe", "2", "null", 0, 2, 0.5, False, None, [], ["1", "1"],
                      {}, {"1": 1}):
            self.assertFalse(launch.env_on(value), value)

    def test_own_status_line(self):
        user = {"statusLine": {"type": "command", "command": "user", "padding": 3}}
        project = {"statusLine": {"type": "command", "command": "project"}}
        self.assertEqual(launch.own_status_line([user, None, {}]), {"command": "user", "padding": 3})
        self.assertEqual(launch.own_status_line([user, project, None]), {"command": "project", "padding": 0})
        self.assertIsNone(launch.own_status_line([user, {"statusLine": None}]))
        self.assertIsNone(launch.own_status_line([None, {}, {"other": 1}]))
        for line in ({"type": "other", "command": "x"}, {"command": "x"}, {"type": "command", "command": ""},
                     {"type": "command", "command": 7}, "text"):
            self.assertIsNone(launch.own_status_line([{"statusLine": line}]), line)
        for padding in (-1, True, 1.5, "2", None):
            self.assertEqual(launch.own_status_line([{"statusLine": {"type": "command", "command": "x", "padding": padding}}]),
                             {"command": "x", "padding": 0}, padding)

    def test_guard_settings(self):
        script = "/kit o'clock/lib/coding-agent-kit/guard.py"
        command = shlex.join([sys.executable, "-I", "-B", "-S", script])
        self.assertEqual(json.loads(launch.guard_settings(script)), settings_of(command))
        self.assertEqual(json.loads(launch.guard_settings(script, 4)), settings_of(command, 4))
        self.assertEqual(launch.guard_settings(script), json.dumps(settings_of(command), separators=(",", ":")))


CLAUDE_API = ROOT / "tests/fixtures/guard/claude_api.py"
CLOSED_PORT = ("127.0.0.1", 9)  # the discard port: nothing listens there unless a host runs that service
API_KEY = "fixture-api-key-accepted-only-by-the-loopback-fake"
REFUSED_GIT = ("clone", "fetch", "pull", "push", "ls-remote", "remote-*", "*://*", "*@*:*")
GIT_SHIM = """#!/bin/sh
# Test shim for git: local commands pass to the real git, network forms are refused; every call is logged.
printf 'git %s\\n' "$*" >> {log}
for word in "$@"; do
    case "$word" in
        {refused}) printf 'REFUSED git %s\\n' "$*" >> {log}; exit 128 ;;
    esac
done
{real}
"""
SSH_SHIM = """#!/bin/sh
# Test shim for ssh, scp and sftp: OpenSSH reads ~/.ssh from the password database, not from HOME.
printf 'REFUSED ssh %s\\n' "$*" >> {log}
exit 255
"""


def load_kit_module(kit, name):
    spec = importlib.util.spec_from_file_location(f"claude_guard_test_{name}", kit / f"lib/coding-agent-kit/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


class Sandbox:
    """A Claude Code that reaches nothing of this host's user: a temporary HOME and configuration directory,
    an environment built from scratch, git and ssh shims first on PATH, every proxy at a closed loopback
    port, no session bus, and its background features closed (probe results R0, R8, R8D and R20)."""

    def __init__(self, base, bindir):
        self.base, self.bindir = base, bindir
        self.home, self.work, self.shims = base / "home", base / "work", base / "shims"
        self.config = self.home / ".claude"
        for path in (self.config, self.work, self.shims, base / "tmp", base / "run",
                     *(base / "xdg" / name for name in ("config", "cache", "data", "state"))):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.netlog = base / "net.log"
        self.netlog.touch()
        log = shlex.quote(str(self.netlog))
        real = shutil.which("git")
        write(self.shims / "git", GIT_SHIM.format(log=log, refused="|".join(REFUSED_GIT),
                                                  real=f"exec {shlex.quote(real)} \"$@\"" if real else "exit 127"), 0o700)
        for name in ("ssh", "scp", "sftp"):
            write(self.shims / name, SSH_SHIM.format(log=log), 0o700)
        # Onboarding done, theme chosen, the work directory trusted, the key approved (by its last 20
        # characters) and the official plugin marketplace's auto-install settled, so nothing asks or clones.
        write(self.config / ".claude.json", json.dumps({
            "hasCompletedOnboarding": True, "theme": "dark", "numStartups": 5,
            "officialMarketplaceAutoInstallAttempted": True, "officialMarketplaceAutoInstalled": False,
            "officialMarketplaceAutoInstallFailReason": "policy_blocked",
            "customApiKeyResponses": {"approved": [API_KEY[-20:]], "rejected": []},
            "projects": {str(self.work): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))

    def env(self, port=None, extra=None):
        closed = "http://%s:%d" % CLOSED_PORT
        # The directory of claude last, for an install whose claude is a node script.
        env = {"PATH": f"{self.shims}:/usr/local/bin:/usr/bin:/bin:{self.bindir}", "HOME": str(self.home), "LANG": "C.UTF-8",
               "TERM": "dumb", "SHELL": "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh",
               "TMPDIR": str(self.base / "tmp"), "XDG_RUNTIME_DIR": str(self.base / "run"),
               **{f"XDG_{name.upper()}_HOME": str(self.base / "xdg" / name) for name in ("config", "cache", "data", "state")},
               "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.base / 'run/no-bus'}",
               "CLAUDE_CONFIG_DIR": str(self.config), "ANTHROPIC_API_KEY": API_KEY,
               "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port or CLOSED_PORT[1]}",
               "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
               "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
               **CLOSED,
               **{name: closed for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")},
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
               "GIT_SSH_COMMAND": str(self.shims / "ssh"), "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
        return env | (extra or {})

    def run(self, claude, args, env, timeout=90):
        """(exit code, stdout, stderr) of CLAUDE ARGS in a session of its own, which is killed afterwards."""
        child = subprocess.Popen([claude, *args], cwd=self.work, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(child.pid)
            out, err = child.communicate()
            raise AssertionError(f"claude {args} ran longer than {timeout} s: {out[-2000:]!r} {err[-2000:]!r}")
        finally:
            kill_group(child.pid)
        return child.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def strays(self):
        """Processes still running with this sandbox's configuration directory, killed; Linux only."""
        if not os.path.isdir("/proc/self"):
            return []
        needle, found = f"CLAUDE_CONFIG_DIR={self.config}".encode(), []
        for entry in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{entry}/environ", "rb") as stream:
                    if needle in stream.read().split(b"\0"):
                        found.append(int(entry))
            except OSError:
                pass
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return found


class RealClaude(unittest.TestCase):
    """The guard's hook contract against the real Claude Code, offline: the C3 settings as claude-as builds
    them, a launch as claude-as exports it, and a loopback fake of the Messages API that answers from a
    script. claude -p is run directly on the native (only the launcher refuses headless forms); the status
    line and the terminal session are left to the live acceptance."""

    @classmethod
    def setUpClass(cls):
        found = shutil.which("claude")
        if not found:
            raise unittest.SkipTest("Claude Code (claude) is not on PATH")
        try:
            socket.create_connection(CLOSED_PORT, timeout=2).close()
        except OSError:
            pass
        else:
            raise unittest.SkipTest("127.0.0.1:9 accepts connections, so it cannot stand in for a closed proxy")
        cls.claude, cls.bindir = os.path.realpath(found), os.path.dirname(os.path.abspath(found))
        with tempfile.TemporaryDirectory(prefix="claude-guard-real-version-") as base:
            sandbox = Sandbox(Path(base).resolve(), cls.bindir)
            code, out, _ = sandbox.run(cls.claude, ["--version"], sandbox.env(), timeout=60)
            sandbox.strays()
        match = None if code else launch.VERSION.match(out)
        if not match:
            raise unittest.SkipTest(f"claude --version printed no Claude Code version: {out.strip()[:80]!r}")
        version = tuple(int(part) for part in match.groups())
        if version < launch.GUARD_MINIMUM:
            raise unittest.SkipTest("Claude Code %s is older than %s" % (".".join(map(str, version)),
                                                                         ".".join(map(str, launch.GUARD_MINIMUM))))

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        tmp = tempfile.TemporaryDirectory(prefix="claude-guard-real-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.sandbox = Sandbox(base, self.bindir)
        # The guard runs from a kit whose path needs quoting, through Claude Code's /bin/sh (Review Focus 1).
        self.kit = base / "kit o'clock dir"
        shutil.copytree(ROOT / "lib", self.kit / "lib")
        self.reserve = load_kit_module(self.kit, "reserve")
        state = base / "state/coding-agent-kit"
        self.accounts, self.guard_dir = state / "accounts", state / "guard"
        for directory in (state, self.accounts, self.guard_dir):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.usage = self.accounts / "claude-work.usage.json"
        self.marker = base / "tool ran"
        self.requests = base / "requests.jsonl"

    def usage_text(self, used):
        """The usage file with the weekly window USED percent used, reset a day ahead."""
        now = int(time.time())
        return json.dumps({"five_hour": None, "seven_day": {"used_percentage": used, "resets_at": now + DAY},
                           "seen_at": now})

    def run_claude(self, prompt, used, cross=None, extra=None):
        """claude -p PROMPT under the guard with the weekly reserve at 10 and USED percent of the week used;
        CROSS, a percentage, replaces the usage file when the fake answers with its tool call. EXTRA is added
        to Claude Code's environment."""
        write(self.usage, self.usage_text(used))
        command = [sys.executable, "-I", "-B", "-S", str(CLAUDE_API), "--log", str(self.requests),
                   "--bash", shlex.join(["touch", str(self.marker)])]
        if cross is not None:
            command += ["--cross", str(self.usage), self.usage_text(cross)]
        fake = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env={},
                                start_new_session=True)
        for cleanup in (fake.stdout.close, fake.stdin.close, lambda: fake.wait(10), lambda: kill_group(fake.pid)):
            self.addCleanup(cleanup)
        port = int(fake.stdout.readline())
        guard_launch = load_kit_module(self.kit, "guard_launch")
        policy = self.reserve.Policy("claude-as", "work", self.reserve.Setting("five_hour", None, "default"),
                                     self.reserve.Setting("weekly", self.reserve.flag_value("10"), "invocation"))
        env = self.sandbox.env(port, extra)
        started = int(time.time())
        state = guard_launch.prepare(env, str(self.guard_dir), "claude-as", "work", policy, str(self.usage), started)
        self.assertEqual(env["CODING_AGENT_KIT_GUARD_SCRIPT"], str(self.kit / "lib/coding-agent-kit/guard.py"))
        settings = launch.guard_settings(env["CODING_AGENT_KIT_GUARD_SCRIPT"])
        # Claude Code's own permission rules allow every Bash call, so only the guard can stop one.
        code, out, err = self.sandbox.run(self.claude, ["--settings", settings, "--allowedTools", "Bash", "-p", prompt],
                                          env)
        self.assertEqual(self.sandbox.strays(), [], "Claude Code left a process behind")
        refused = [line for line in self.sandbox.netlog.read_text().splitlines() if line.startswith("REFUSED")]
        self.assertEqual(refused, [], "Claude Code tried to reach the network through git or ssh")
        lines = self.requests.read_text().splitlines() if self.requests.exists() else []
        messages = [entry for entry in map(json.loads, lines) if entry["path"] == "/v1/messages"]
        return code, out, err, state, messages

    def assert_state(self, state, present, absent):
        for name in present:
            self.assertTrue(os.path.exists(state + name), f"{name} is missing")
        for name in absent:
            self.assertFalse(os.path.exists(state + name), f"{name} exists")

    def test_a_prompt_at_the_reserve_is_never_sent(self):
        code, out, err, state, messages = self.run_claude("PLAIN say hello", 95)
        self.assertEqual(messages, [], "the blocked prompt reached the API")
        blocked = Path(state + ".blocked").read_text()
        self.assertIn("claude-as: work: weekly window 5% remaining, at or below its 10% reserve; resets in ", blocked)
        self.assertIn("\nThis prompt was not sent.\nTo use the reserve, exit and run:\n"
                      "  claude-as --reserve-weekly off work --continue", blocked)
        # Claude Code shows the guard's message behind its own prefix (A8, probe result R6).
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"^UserPromptSubmit operation blocked by hook:\s\[")
        self.assertIn(f"guard.py' prompt]: {blocked}\n", out)
        self.assertNotIn("PLAIN-ANSWER", out)
        self.assert_state(state, (".launched", ".ran.start", ".ran.prompt"), (".reply", ".ran.tool", ".seen"))

    def test_a_prompt_below_the_reserve_gets_its_reply(self):
        code, out, err, state, messages = self.run_claude("PLAIN say hello", 50)
        self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
        self.assertEqual([entry.get("scenario") for entry in messages if entry["body"].get("tools")], ["PLAIN"])
        self.assert_state(state, (".ran.start", ".ran.prompt", ".ran.stop", ".reply"), (".blocked", ".seen"))

    def test_a_tool_call_below_the_reserve_runs(self):
        code, out, err, state, messages = self.run_claude("TOOL run the command", 50)
        self.assertEqual((code, out.strip()), (0, "TOOL-DONE"), err)
        self.assertTrue(self.marker.exists(), "the tool call did not run")
        self.assertEqual([entry.get("scenario") for entry in messages if entry["body"].get("tools")],
                         ["TOOL:call", "TOOL:done"])
        self.assert_state(state, (".ran.prompt", ".ran.tool", ".ran.stop", ".reply"), (".blocked",))

    def test_a_tool_call_after_the_reserve_is_crossed_is_not_run(self):
        code, out, err, state, messages = self.run_claude("TOOL run the command", 50, cross=95)
        self.assertFalse(self.marker.exists(), "the tool call ran at the reserve")
        # The turn ends at the denied call: no request carries its result (STOP_TURN, probe result R3).
        self.assertEqual([entry.get("scenario") for entry in messages if entry["body"].get("tools")], ["TOOL:call"])
        self.assertEqual((code, out.strip()), (0, ""), err)
        blocked = Path(state + ".blocked").read_text()
        self.assertIn("claude-as: work: weekly window 5% remaining, at or below its 10% reserve; resets in ", blocked)
        self.assertIn("\nThis tool call was stopped. Stop working now and tell the user why.\n", blocked)
        # The denying tool event records the reply; no Stop hook runs for a turn it stopped.
        self.assert_state(state, (".ran.prompt", ".ran.tool", ".reply"), (".ran.stop",))

    def test_safe_mode_and_bare_mode_run_none_of_the_hooks(self):
        """Why claude-as refuses CLAUDE_CODE_SAFE_MODE and CLAUDE_CODE_SIMPLE with a reserve on: either one,
        in the environment or in a settings env, lets a prompt at the reserve through unchecked."""
        cases = (("CLAUDE_CODE_SAFE_MODE", "environment"), ("CLAUDE_CODE_SIMPLE", "environment"),
                 ("CLAUDE_CODE_SAFE_MODE", "settings"))
        for name, where in cases:
            with self.subTest(name=name, where=where):
                self.requests.unlink(missing_ok=True)
                for leftover in self.guard_dir.iterdir():
                    leftover.unlink()
                settings = self.sandbox.config / "settings.json"
                if where == "settings":
                    write(settings, json.dumps({"env": {name: "1"}}))
                code, out, err, state, messages = self.run_claude("PLAIN say hello", 95,
                                                                  extra={name: "1"} if where == "environment" else None)
                settings.unlink(missing_ok=True)
                self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
                self.assertEqual([entry.get("scenario") for entry in messages if entry["body"].get("tools")], ["PLAIN"])
                self.assert_state(state, (".launched",), (".ran.start", ".ran.prompt", ".ran.stop", ".blocked"))


if __name__ == "__main__":
    unittest.main()
