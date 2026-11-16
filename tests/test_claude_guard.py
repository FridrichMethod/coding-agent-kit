"""claude-as with a usage reserve: the guarded launch and the launch without one.

A launch with a reserve on passes Claude Code the guard's hooks and status line
in one --settings argument, exports the launch to the guard, keeps the session
out of Claude Code's background process and refuses every form the guard cannot
cover. A launch with both reserves off stays exactly as it was. The real
launcher runs from a kit copy in a temporary HOME with a fake claude that
records its argv and environment; a guarded launch needs a terminal on standard
output, so those launches run with standard output on a pseudo-terminal.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

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
        for name, text in ((RESUME, "1"), ("CLAUDE_DISABLE_ADOPT", "0"), ("CLAUDE_CODE_DISABLE_AGENT_VIEW", "")):
            with self.subTest(name=name):
                write(user, json.dumps({"env": {name: text}}))
                self.assert_refused(["work"], f"{user} sets {name} in its env, which would reopen what a launch with "
                                    "usage reserves closes")
        # Values that keep the hooks and the background closed launch.
        write(user, json.dumps({"disableAllHooks": False, "allowManagedHooksOnly": True, "env": CLOSED}))
        self.guarded(["work"])

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

    def test_the_managed_files(self):
        for path, managed in (("/etc/claude-code/managed-settings.json", True),
                              ("/Library/Application Support/ClaudeCode/managed-settings.json", True),
                              ("/etc/claude-code/managed-settings.d/10-org.json", True),
                              ("/etc/claude-code/other.json", False), ("/home/u/.claude/settings.json", False)):
            self.assertEqual(launch.managed(Path(path)), managed, path)

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


if __name__ == "__main__":
    unittest.main()
