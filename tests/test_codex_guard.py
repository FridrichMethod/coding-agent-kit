"""codex-as with usage reserves: launch forms, refused options, hook settings and the guarded launch.

LaunchForms, Version and HooksOff run check.arguments() and layout.hooks_off()
in process against a temporary HOME, Codex home and working directory. Hooks
pins the hook definitions in process. GuardedLaunch runs codex-as from a kit
copy under a path with a space and a quote, in a temporary HOME, against a
fake codex that records its argv and environment, writes rollouts as native
Codex does and runs the guard's hooks the way Codex runs them: the command of
the -c value through a shell, with the event on stdin. No native, provider,
credential or network is involved. RealCodex runs the real codex, when it is on
PATH, in a temporary HOME against tests/fixtures/guard/codex_backend.py on
127.0.0.1, with every proxy at a closed loopback port; it skips otherwise.
"""

import base64
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
UUID = "12345678-1234-4234-8234-123456789abc"
# The kit's own knobs: a caller's value never reaches a case, which sets what it needs.
KIT_KNOBS = ("CODING_AGENT_KIT_CONFIG", "CODING_AGENT_KIT_COLOR", "CODING_AGENT_KIT_USAGE_FILE", "CODING_AGENT_KIT_TOKEN_ID",
             "CODING_AGENT_KIT_CLAUDE_PID", "CODING_AGENT_KIT_REPO_URL", "CODING_AGENT_KIT_GUARD",
             "CODING_AGENT_KIT_GUARD_SCRIPT", "CODING_AGENT_KIT_GUARD_PYTHON", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "NO_COLOR")
VARIABLES = ("CODING_AGENT_KIT_GUARD", "CODING_AGENT_KIT_GUARD_SCRIPT", "CODING_AGENT_KIT_GUARD_PYTHON")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load("codex_guard_check", "lib/codex-as/check.py")
layout = load("codex_guard_layout", "lib/codex-as/layout.py")
settings = load("codex_guard_config", "lib/coding-agent-kit/config.py")
launch = load("codex_guard_launch", "lib/codex-as/launch.py")


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex-guard-test-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / "home"
        self.root = self.home / ".codex"
        self.root.mkdir(parents=True, mode=0o700)
        self.cwd = self.base / "project" / "sub"
        self.cwd.mkdir(parents=True)
        env = {name: value for name, value in os.environ.items() if name not in KIT_KNOBS}
        env["HOME"] = str(self.home)
        with patch.dict(os.environ, env, clear=True):
            self.config = settings.load()

    def plan(self, args):
        return check.arguments(args, self.config)


class LaunchForms(Fixture):
    """The form decides what a launch with a reserve on does: inject the hooks (interactive and
    headless), refuse (model work the hooks cannot see or stop), or run as without reserves."""

    def test_forms(self):
        cases = {
            "interactive": ([], ["fix the build"], ["resume"], ["resume", "--last"], ["resume", UUID],
                            ["fork"], ["fork", UUID], ["-m", "o3", "go"], ["--model", "resume", "go"],
                            ["-C", "/tmp", "go"], ["exec this"], ["mcp-server"]),
            "headless": (["exec"], ["exec", "go"], ["e", "go"], ["exec", "resume", "--last"],
                         ["exec", "resume", UUID, "go"], ["exec", "fork", UUID, "go"], ["exec", "review"],
                         ["review"], ["review", "--uncommitted"], ["-C", "/tmp", "exec", "go"],
                         ["exec", "-m", "o3", "go"]),
            # remote-control runs an app server that remote clients drive, and debug app-server
            # send-message-v2 runs a turn through one: model work as in app-server.
            "refused": (["cloud"], ["cloud-tasks"], ["cloud", "exec", "--env", "x", "go"], ["app-server"],
                        ["exec-server"], ["remote-control"], ["remote-control", "--no-daemon"],
                        ["debug", "app-server", "send-message-v2", "hi"], ["-c", "model=x", "app-server"]),
            "plain": (["mcp", "list"], ["mcp"], ["plugin"], ["features", "list"], ["login", "status"],
                      ["apply"], ["a"], ["sandbox", "linux", "--", "exec"], ["debug"], ["debug", "models"],
                      ["debug", "prompt-input", "hi"], ["completion", "bash"], ["update"], ["doctor"],
                      ["archive", UUID], ["delete", UUID], ["unarchive", UUID], ["migrate-rollouts"],
                      ["responses-api-proxy"], ["stdio-to-uds", "x"], ["execpolicy"],
                      # Help and version print and run nothing.
                      ["--help"], ["-h"], ["-V"], ["--version"], ["help"], ["help", "exec"],
                      ["resume", "--help"], ["exec", "--help"], ["exec", "go", "-V"], ["review", "-h"],
                      ["cloud", "--help"], ["app-server", "--help"], ["remote-control", "-h"],
                      ["debug", "app-server", "--help"], ["login", "--help"]),
        }
        for form, argument_lists in cases.items():
            for args in argument_lists:
                with self.subTest(args=args):
                    self.assertEqual(self.plan(args).form, form)

    def test_app_is_a_prompt_without_the_desktop_app(self):
        if check.DESKTOP:
            self.skipTest("codex app is the Desktop app here, which codex-as refuses")
        self.assertEqual(self.plan(["app"]).form, "interactive")

    def test_hooks_override(self):
        # Attached spellings collide with the injection like the spaced ones (Review Focus 4).
        for args in (["-c", "hooks.x=1"], ["-chooks.x=1"], ["-c=hooks.x=1"], ["--config", "hooks.x=1"],
                     ["--config=hooks.x=1"], ["-c", "hooks={}"], ["-c", '"hooks".PreToolUse=[]'],
                     ["-c", " hooks . Stop = [] "], ["-c", "hooks"], ["exec", "-c", "hooks.x=1", "go"],
                     ["exec", "go", "--config=hooks.x=1"], ["resume", "--last", "-chooks.x=1"],
                     ["-c", "model=x", "-c", "hooks.x=1"]):
            with self.subTest(args=args):
                self.assertTrue(self.plan(args).hooks_override)
        for args in ([], ["-c", "model=x"], ["fix the hooks"], ["hooks"], ["exec", "hooks.x=1"],
                     ["-c", "features.hooks=true"], ["-c", "hooks_dir=x"], ["-m", "-chooks.x=1", "go"],
                     ["--", "-c", "hooks.x=1"], ["exec", "go", "--", "--config=hooks.x=1"]):
            with self.subTest(args=args):
                self.assertFalse(self.plan(args).hooks_override)

    def test_hooks_disabled(self):
        # codex_hooks is the feature's legacy name; Codex refuses a value that is not a boolean.
        for args in (["--disable", "hooks"], ["--disable=hooks"], ["--disable", "codex_hooks"],
                     ["--disable", " hooks "], ["-c", "features.hooks=false"], ["-cfeatures.hooks=false"],
                     ["--config=features.codex_hooks=false"], ["-c", "features={hooks=false}"],
                     ["-c", 'features."hooks"=false'], ["-c", 'features.hooks="false"'],
                     ["--enable", "hooks", "--disable", "hooks"], ["--disable", "hooks", "--enable", "hooks"],
                     ["exec", "--disable", "hooks", "go"], ["exec", "go", "-c", "features.hooks=false"]):
            with self.subTest(args=args):
                self.assertTrue(self.plan(args).hooks_disabled)
        for args in ([], ["--enable", "hooks"], ["--enable=codex_hooks"], ["-c", "features.hooks=true"],
                     ["-c", "features={hooks=true}"], ["--disable", "goals"], ["-c", "features.goals=false"],
                     ["--disable", "plugin_hooks"], ["disable hooks"], ["-m", "--disable", "hooks"],
                     ["--", "--disable", "hooks"], ["exec", "go", "--", "-c", "features.hooks=false"]):
            with self.subTest(args=args):
                self.assertFalse(self.plan(args).hooks_disabled)

    def test_goals_on(self):
        """codex-as turns goals off with -c features.goals=false and --disable goals. clap keeps one
        --disable list, which a --disable after a subcommand replaces, so the -c value has to hold
        alone: a caller's --enable goals or -c value that sets features.goals to anything but false,
        or that replaces the whole features table, would turn goals on again."""
        for args in (["--enable", "goals"], ["--enable=goals"], ["--enable", " goals "],
                     ["exec", "--enable", "goals", "go"], ["resume", "--last", "--enable=goals"],
                     ["-c", "features.goals=true"], ["-cfeatures.goals=true"], ["--config=features.goals=true"],
                     ["-c", 'features."goals"=true'], ["-c", "features.goals.enabled=false"],
                     ["-c", 'features.goals="false"'], ["-c", "features={goals=true}"], ["-c", "features={}"],
                     ["-c", "features={hooks=true}"], ["exec", "go", "-c", "features.goals=1"],
                     ["--disable", "goals", "--enable", "goals"]):
            with self.subTest(args=args):
                self.assertTrue(self.plan(args).goals_on)
        for args in ([], ["--disable", "goals"], ["--disable=goals"], ["-c", "features.goals=false"],
                     ["-c", "features={goals=false}"], ["-c", "features={goals=false,hooks=true}"],
                     ["-c", "features.hooks=true"], ["-c", "goals=true"], ["--enable", "hooks"],
                     ["exec", "--disable", "plugin_hooks", "go"], ["resume", "--last", "--disable", "plugin_hooks"],
                     ["fix the goals"], ["-m", "--enable", "goals"], ["--", "--enable", "goals"],
                     ["exec", "go", "--", "-c", "features.goals=true"]):
            with self.subTest(args=args):
                self.assertFalse(self.plan(args).goals_on)

    def test_ephemeral_and_ignore_user_config(self):
        for args, ephemeral, ignore in ((["exec", "--ephemeral", "go"], True, False),
                                        (["exec", "go", "--ephemeral"], True, False),
                                        (["--ephemeral"], True, False),
                                        (["exec", "--ignore-user-config", "go"], False, True),
                                        (["--ignore-user-config", "exec", "--ephemeral", "go"], True, True),
                                        (["exec", "-m", "--ephemeral", "go"], False, False),
                                        (["exec", "-o", "--ignore-user-config", "go"], False, False),
                                        (["exec", "go", "--", "--ephemeral"], False, False),
                                        (["exec", "--ephemeral go"], False, False),
                                        ([], False, False), (["exec", "go"], False, False)):
            with self.subTest(args=args):
                plan = self.plan(args)
                self.assertEqual((plan.ephemeral, plan.ignore_user_config), (ephemeral, ignore))

    def test_plain_launches_keep_their_plan(self):
        """The new fields add nothing to what a launch without reserves reads from its plan."""
        plan = self.plan(["resume", UUID])
        self.assertEqual((plan.mode, plan.command, plan.resume_id, plan.form), ("interactive", "resume", UUID, "interactive"))
        plan = self.plan(["exec", "fork", UUID, "go"])
        self.assertEqual((plan.mode, plan.command, plan.recover_id, plan.forked, plan.form),
                         ("exec", "exec-resume", UUID, True, "headless"))
        for args in ([], ["exec", "go"], ["-V"]):
            with self.subTest(args=args):
                plan = self.plan(args)
                self.assertEqual((plan.hooks_override, plan.hooks_disabled, plan.goals_on, plan.ephemeral,
                                  plan.ignore_user_config), (False, False, False, False, False))

    def test_refused_options_still_refuse(self):
        """A guarded form never relaxes the checks every launch has."""
        for args in (["--oss", "exec", "go"], ["exec", "-c", "model_provider=x", "go"], ["agents"],
                     ["remote-control", "start"], ["app-server", "daemon"], ["--enable", "use_agent_identity"]):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.plan(args)


class Version(unittest.TestCase):
    def test_reserves_need_only_the_launch_minimum(self):
        """A launch with a reserve needs no newer Codex than every codex-as launch: 0.161.0."""
        self.assertEqual(check.codex_version("codex-cli 0.161.0\n"), (0, 161, 0))
        self.assertEqual(check.codex_version("codex-cli 0.169.0"), (0, 169, 0))
        for text in ("codex-cli 0.160.9", "codex-cli 0.161.0-alpha.1"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, r"0\.161\.0 or newer"):
                check.codex_version(text)


class HooksOff(Fixture):
    """hooks = false (or the legacy codex_hooks) under [features] of a configuration layer Codex
    loads for this launch turns the guard's hooks off."""

    def setUp(self):
        super().setUp()
        # The host's /etc/codex never counts here.
        self.host_system = layout.SYSTEM_CONFIG
        self.system = (self.base / "etc/codex/config.toml", self.base / "etc/codex/managed_config.toml")
        patcher = patch.object(layout, "SYSTEM_CONFIG", self.system)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_system_layers(self):
        self.assertEqual(tuple(map(str, self.host_system)), ("/etc/codex/config.toml", "/etc/codex/managed_config.toml"))
        for path in self.system:
            with self.subTest(path=path.name):
                self.write(path, "[features]\nhooks = false\n")
                self.assertTrue(self.off())
                path.write_text("[features]\nhooks = true\n")
                self.assertFalse(self.off())

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def off(self, profile=None, cwd=None):
        found = layout.hooks_off(check, self.root, self.cwd if cwd is None else cwd, profile)
        self.assertTrue(found is None or isinstance(found, Path), found)
        return found is not None

    def test_the_file_is_named(self):
        self.write(self.root / "config.toml", "[features]\nhooks = true\n")
        profile = self.write(self.root / "p.config.toml", "[features]\nhooks = false\n")
        self.assertIsNone(layout.hooks_off(check, self.root, self.cwd))
        self.assertEqual(layout.hooks_off(check, self.root, self.cwd, "p"), profile)

    def test_shared_config(self):
        self.assertFalse(self.off(), "no config.toml")
        for text, expected in (('model = "x"\n', False), ("[features]\n", False), ("[features]\ngoals = false\n", False),
                               ("[features]\nhooks = true\n", False), ("[features]\ncodex_hooks = true\n", False),
                               ("[features]\nhooks = false\n", True), ("[features]\ncodex_hooks = false\n", True),
                               ("[features]\nhooks = true\ncodex_hooks = false\n", True),
                               ('[features]\nhooks = "false"\n', True), ("features = { hooks = false }\n", True),
                               ("[features.hooks]\nenabled = true\n", True),
                               ('[hooks.state."x"]\ntrusted_hash = "y"\n[features]\nhooks = true\n', False)):
            self.write(self.root / "config.toml", text)
            with self.subTest(text=text):
                self.assertEqual(self.off(), expected)
                self.assertEqual(self.off("p"), expected, "a profile layers on the shared file")

    def test_selected_profile(self):
        self.write(self.root / "config.toml", "[features]\nhooks = true\n")
        self.write(self.root / "p.config.toml", "[features]\nhooks = false\n")
        self.write(self.root / "q.config.toml", "[features]\ncodex_hooks = false\n")
        self.write(self.root / "r.config.toml", "[features]\nhooks = true\n")
        self.assertTrue(self.off("p"))
        self.assertTrue(self.off("q"))
        self.assertFalse(self.off("r"))
        self.assertFalse(self.off(), "another profile's file is not loaded")
        self.assertFalse(self.off("missing"))

    def test_project_layers(self):
        """A trusted project's .codex/config.toml turns hooks off too; trust is not read, so any
        project layer counts."""
        self.assertFalse(self.off())
        project = self.write(self.base / "project/.codex/config.toml", "[features]\nhooks = false\n")
        self.assertTrue(self.off())
        self.assertTrue(self.off(cwd=self.base / "project"))
        self.assertFalse(self.off(cwd=self.base), "a layer below the working directory is not loaded")
        project.write_text("[features]\nhooks = true\n")
        self.assertFalse(self.off())
        # A linked working directory: its lexical and its resolved parents.
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "link").symlink_to(self.cwd)
        self.write(self.base / "elsewhere/.codex/config.toml", "[features]\ncodex_hooks = false\n")
        self.assertTrue(self.off(cwd=elsewhere / "link"))
        project.write_text("[features]\nhooks = false\n")
        (elsewhere / ".codex/config.toml").unlink()
        self.assertTrue(self.off(cwd=elsewhere / "link"))


class ManagedGoals(Fixture):
    """goals under [features] of a managed layer, which native Codex applies over the command line's -c
    values and flags, set to anything but false keeps goals on in a launch that turns them off."""

    def setUp(self):
        super().setUp()
        self.host_managed = layout.MANAGED_CONFIG
        self.managed = (self.base / "etc/codex/managed_config.toml", self.base / "etc/codex/requirements.toml")
        patcher = patch.object(layout, "MANAGED_CONFIG", self.managed)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_managed_layers(self):
        self.assertEqual(tuple(map(str, self.host_managed)),
                         ("/etc/codex/managed_config.toml", "/etc/codex/requirements.toml"))
        self.assertIsNone(layout.goals_on(check))
        for path in self.managed:
            for text, on in (("[features]\ngoals = true\n", True), ('[features]\ngoals = "false"\n', True),
                             ("[features.goals]\nenabled = false\n", True), ("features = { goals = 1 }\n", True),
                             ("[features]\ngoals = false\n", False), ("[features]\nhooks = true\n", False),
                             ("goals = true\n", False), ('model = "x"\n', False)):
                with self.subTest(path=path.name, text=text):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text)
                    self.assertEqual(layout.goals_on(check), path if on else None)
            path.unlink()

    def test_other_layers_do_not_count(self):
        """The command line's -c features.goals=false wins over the shared, a profile's, a project's and
        the system config.toml (measured with codex-cli 0.169.0)."""
        for path in (self.root / "config.toml", self.root / "p.config.toml", self.cwd / ".codex/config.toml",
                     self.base / "etc/codex/config.toml"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("[features]\ngoals = true\n")
        self.assertIsNone(layout.goals_on(check))


# The two hook values, as settled for every release: a change asks every host to trust the hooks again.
PROMPT_HOOK = (r'hooks.UserPromptSubmit=[{hooks=[{type="command",command="\"$CODING_AGENT_KIT_GUARD_PYTHON\" -I -B -S '
               r'\"$CODING_AGENT_KIT_GUARD_SCRIPT\" prompt",timeout=20}]}]')
TOOL_HOOK = (r'hooks.PreToolUse=[{hooks=[{type="command",command="\"$CODING_AGENT_KIT_GUARD_PYTHON\" -I -B -S '
             r'\"$CODING_AGENT_KIT_GUARD_SCRIPT\" tool",timeout=20}]}]')
FINGERPRINT = "90aeb3ec484a2ca6"
# The trust records Codex writes on "Trust all" for exactly these two values, on any host (Codex keeps
# them in the root config.toml under [hooks.state."KEY"] trusted_hash): measured with codex-cli
# 0.169.0, and the hook key and hash code is the same from 0.161.0 on.
TRUST_RECORDS = {
    "/<session-flags>/config.toml:user_prompt_submit:0:0":
        "sha256:c80a71faf46fb72216d9c2f64df47b9d71695077111da55efa23e7add88d05b6",
    "/<session-flags>/config.toml:pre_tool_use:0:0":
        "sha256:3171e9d47605b7a9d46f6bbd57941f1de4cbec6880ccf75955321f963ed35733",
}


class Hooks(unittest.TestCase):
    """The hook definitions are constants: Codex trusts each by a hash of its definition."""

    def test_the_definitions_and_their_fingerprint_are_pinned(self):
        self.assertEqual(launch.GUARD_HOOKS, (PROMPT_HOOK, TOOL_HOOK))
        self.assertEqual(launch.guard_fingerprint(), FINGERPRINT)
        # The -c value holds when a --disable after a subcommand replaces the launcher's --disable list.
        self.assertEqual(launch.GOALS_OFF, ("-c", "features.goals=false", "--disable", "goals"))

    def test_each_value_is_one_hook_at_the_place_its_trust_record_names(self):
        """A trust record's key names the session flags, the event, the matcher group and the handler:
        each value holds one group without a matcher (every tool) and one command handler."""
        for value, (key, digest), kind in zip(launch.GUARD_HOOKS, TRUST_RECORDS.items(), ("prompt", "tool")):
            with self.subTest(kind=kind):
                name, _, raw = value.partition("=")
                event = re.sub(r"(?<!^)(?=[A-Z])", "_", name.removeprefix("hooks.")).lower()
                self.assertEqual(key, f"/<session-flags>/config.toml:{event}:0:0")
                self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
                command = f'"$CODING_AGENT_KIT_GUARD_PYTHON" -I -B -S "$CODING_AGENT_KIT_GUARD_SCRIPT" {kind}'
                self.assertEqual(tomllib.loads("v = " + raw)["v"],
                                 [{"hooks": [{"type": "command", "command": command, "timeout": 20}]}])
                # The launcher's own value would be a caller's hooks override: only the caller's are scanned.
                self.assertTrue(check.arguments(["-c", value], None).hooks_override)


FAKE = r'''
import json, os, pathlib, signal, subprocess, sys, time, tomllib
if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_VERSION", "codex-cli 0.169.0"))
    if os.environ.get("FAKE_BREAK"):
        os.chmod(sys.argv[0], 0o600)  # the launch that follows cannot execute it
    sys.exit(0)
args = sys.argv[1:]
if os.environ.get("FAKE_READY"):
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # ends by the signal, as native Codex does
    signal.signal(signal.SIGQUIT, lambda signum, frame: os._exit(131))  # as a shell reports a quit, without a core
    pathlib.Path(os.environ["FAKE_READY"]).touch()
    time.sleep(60)
launch = os.environ.get("CODING_AGENT_KIT_GUARD")
state = json.loads(launch)["state"] if launch else None
record = {"args": args, "env": dict(os.environ), "launched": bool(state) and os.path.isfile(state + ".launched"),
          "hooks": []}
home = pathlib.Path(os.environ["CODEX_HOME"])
transcripts = []
for item in json.loads(os.environ.get("FAKE_ROLLOUTS", "[]")):
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
    if "append" in item:  # a resumed thread: Codex appends to its rollout, wherever it lies
        path, lines = pathlib.Path(item["append"]), item["lines"]
    else:
        day = home / "sessions" / time.strftime("%Y/%m/%d", time.localtime(now))
        day.mkdir(parents=True, exist_ok=True)
        path = day / ("rollout-" + time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(now)) + "-" + item["thread"] + ".jsonl")
        meta = {"id": item["thread"], "timestamp": stamp, "creator_user_id": item["creator"][0],
                "creator_account_id": item["creator"][1]}
        lines = [{"type": "session_meta", "payload": meta}, *item["lines"]]
    with open(path, "a") as stream:
        for line in lines:
            stream.write(json.dumps({"timestamp": stamp, **line}) + "\n")
    transcripts.append(str(path))
# The guard's hooks as Codex runs them: the command of the -c value through a shell, the event on stdin.
names = {"prompt": "UserPromptSubmit", "tool": "PreToolUse"}
for kind in json.loads(os.environ.get("FAKE_HOOKS", "[]")):
    value = next(arg for arg in args if arg.startswith(f"hooks.{names[kind]}="))
    command = tomllib.loads("v = " + value.partition("=")[2])["v"][0]["hooks"][0]["command"]
    event = {"hook_event_name": names[kind], "cwd": os.getcwd(),
             "transcript_path": transcripts[0] if transcripts else None}
    if kind == "tool":
        event.update(tool_name="Bash", tool_input={"command": "true"})
    done = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(event), capture_output=True, text=True)
    record["hooks"].append([kind, done.returncode, done.stdout, done.stderr])
pathlib.Path(os.environ["FAKE_RECORD"]).write_text(json.dumps(record))
print("fake codex ran")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''
USER, WORKSPACE = "user-fixture-A", "workspace-fixture-A"
THREAD = "019a0000-0000-7000-8000-00000000000a"
LAUNCH_LINE = "codex-as: usage reserves for A: {policy}; hooks refuse new prompts and tool calls at a reserve.\n"
WITHOUT = "; to launch without usage reserves, run codex-as --reserve-5h off --reserve-weekly off {account} with the same arguments.\n"
# Rollout lines as native Codex writes them (the fake stamps them).
ANSWER = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}}
CALL = {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "{}",
                                             "call_id": "call-1"}}


def snapshot(used):
    window = {"used_percent": used, "window_minutes": 300, "resets_at": int(time.time()) + 3600}
    return {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {
        "limit_id": "codex", "limit_name": None, "primary": window, "secondary": None, "credits": None,
        "plan_type": None}}}


def id_token(claims):
    return "fake." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".fake"


class GuardedLaunch(unittest.TestCase):
    """codex-as with a reserve on, from a kit copy, against a fake codex."""

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        temp = tempfile.TemporaryDirectory(prefix="codex-guard-launch-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        # A kit root with a space and a quote: the hooks' shell gets the guard's path unchanged.
        self.kit = self.base / "kit o'clock dir"
        for part in ("bin", "lib", "docs"):
            shutil.copytree(ROOT / part, self.kit / part)
        self.home = self.base / "home with spaces"
        self.root = self.home / ".codex"
        self.root.mkdir(parents=True, mode=0o700)
        self.home.chmod(0o700)
        (self.root / "config.toml").write_text('model = "fixture-model"\n')
        (self.home / ".secrets").mkdir(mode=0o700)
        credential = self.home / ".secrets/codex-A.json"
        claims = {"sub": "fixture-A", "email": "alice@example.com",
                  "https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_user_id": USER}}
        credential.write_text(json.dumps({
            "auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-10-01T00:00:00Z",
            "tokens": {"id_token": id_token(claims), "access_token": "fabricated-sensitive-access",
                       "refresh_token": "fabricated-sensitive-refresh", "account_id": WORKSPACE}}))
        credential.chmod(0o600)
        fakes = self.base / "fake bin"
        fakes.mkdir()
        (fakes / "codex").write_text(f"#!{sys.executable}\n" + FAKE)
        (fakes / "codex").chmod(0o700)
        self.cwd = self.base / "work dir"
        self.cwd.mkdir()
        self.record = self.base / "record.json"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("ANTHROPIC_", "CLAUDE_", "CODEX_", "OPENAI_", "CODING_AGENT_KIT_", "XDG_"))
                    and key not in ("NO_COLOR", "PYTHONPATH", "BASH_ENV", "ENV", "ZDOTDIR")}
        self.env.update(HOME=str(self.home), FAKE_RECORD=str(self.record),
                        PATH=os.pathsep.join((str(fakes), self.env.get("PATH", "/usr/bin:/bin"))))
        self.state = self.home / ".local/state/coding-agent-kit"
        self.guard_dir = self.state / "guard"
        self.marker = self.guard_dir / f"codex-hooks-{FINGERPRINT}"
        self.store = 'cli_auth_credentials_store="file"'
        self.sqlite = "sqlite_home=" + json.dumps(str(self.root))

    def configure(self, text):
        path = self.home / ".config/coding-agent-kit/config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        for directory in (path.parent, path.parent.parent):
            directory.chmod(0o700)
        path.write_text(text)
        path.chmod(0o600)

    def run_codex(self, *args, env=None):
        self.record.unlink(missing_ok=True)
        result = subprocess.run([str(self.kit / "bin/codex-as"), *args], cwd=self.cwd, env=self.env | (env or {}),
                                stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=60,
                                start_new_session=True)
        self.assertNotIn("fabricated-sensitive", result.stdout + result.stderr)
        return result

    def recorded(self):
        return json.loads(self.record.read_text())

    def trusted(self):
        """The marker of an earlier launch whose tool hook ran under Codex on this host."""
        self.guard_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.marker.touch(mode=0o600)

    def launches(self):
        """State files of this account's launches left in the guard directory."""
        if not self.guard_dir.exists():
            return []
        return sorted(name for name in os.listdir(self.guard_dir) if name.startswith("codex-as-"))

    def head(self, interactive=True):
        """The launcher's own values of a launch without reserves."""
        return ["-c", self.store, "-c", self.sqlite,
                *(["-c", "check_for_update_on_startup=false", "--no-daemon"] if interactive else [])]

    def guarded(self, interactive=True):
        return [*self.head(interactive), "-c", PROMPT_HOOK, "-c", TOOL_HOOK, "-c", "features.goals=false",
                "--disable", "goals"]

    def test_without_reserves_a_launch_is_unchanged_and_passes_no_guard(self):
        inherited = dict.fromkeys(VARIABLES, "inherited")
        for args, expected in ((["A", "exec", "go"], [*self.head(False), "exec", "go"]),
                               (["A"], self.head()), (["A", "fix it"], [*self.head(), "fix it"])):
            with self.subTest(args=args):
                result = self.run_codex(*args)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "fake codex ran\n", ""))
                plain = self.recorded()
                self.assertEqual(plain["args"], expected)
                for flags in ((), ("--reserve-5h", "off", "--reserve-weekly", "off")):
                    result = self.run_codex(*flags, *args, env=inherited)
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "fake codex ran\n", ""))
                    self.assertEqual(self.recorded(), plain, "the inherited guard variables reached Codex")
        self.assertFalse(self.guard_dir.exists(), "a launch without reserves made the guard directory")

    def test_an_interactive_launch_gets_the_hooks_the_guard_and_goals_off(self):
        before = int(time.time())
        result = self.run_codex("--reserve-5h", "12.5", "A", "fix it", env=dict.fromkeys(VARIABLES, "inherited"))
        after = int(time.time())
        self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(result.stderr, LAUNCH_LINE.format(policy="five-hour 12.5% (--reserve-5h), weekly off (default)"))
        record = self.recorded()
        # The launcher's own values come first, then the hooks and goals off, then the caller's arguments.
        self.assertEqual(record["args"], [*self.guarded(), "fix it"])
        self.assertTrue(record["launched"], "the launch's .launched did not exist while Codex ran")
        env = record["env"]
        self.assertEqual(env["CODING_AGENT_KIT_GUARD_SCRIPT"], str(self.kit / "lib/coding-agent-kit/guard.py"))
        python = env["CODING_AGENT_KIT_GUARD_PYTHON"]
        self.assertTrue(os.path.isabs(python) and os.access(python, os.X_OK), python)
        launch = json.loads(env["CODING_AGENT_KIT_GUARD"])
        self.assertTrue(env["CODING_AGENT_KIT_GUARD"].isascii())
        self.assertTrue(before <= launch["started"] <= after)
        self.assertRegex(launch["state"], "^" + re.escape(f"{self.guard_dir}/codex-as-A-") + rf"[1-9][0-9]*-{launch['started']}$")
        self.assertEqual(launch, {"v": 1, "tool": "codex-as", "account": "A", "five_hour": "12.5", "weekly": None,
                                  "started": launch["started"], "usage": str(self.state / "accounts/codex-A.json"),
                                  "state": launch["state"], "marker": str(self.marker)})
        # Everything else in the environment is what a launch without reserves passes.
        self.run_codex("A", "fix it")
        self.assertEqual({key: value for key, value in env.items() if key not in VARIABLES}, self.recorded()["env"])
        self.assertEqual(self.launches(), [], "the launch's state outlived it")
        self.assertEqual(oct(self.guard_dir.stat().st_mode & 0o777), "0o700")

    def test_the_guarded_forms_and_the_caller_s_own_values(self):
        self.trusted()
        self.configure("[usage_reserve]\nweekly = 10\n")
        for args, expected in ((["A"], self.guarded()), (["A", "fork"], [*self.guarded(), "fork"]),
                               (["A", "review"], [*self.guarded(), "review"]),
                               (["A", "exec", "go"], [*self.guarded(False), "exec", "go"]),
                               (["A", "-m", "o3", "exec", "go"], [*self.guarded(False), "-m", "o3", "exec", "go"]),
                               # A caller's --no-daemon is not given twice; goals stay off after the caller's values.
                               (["A", "--no-daemon", "go"], [*self.head()[:-1], "-c", PROMPT_HOOK, "-c", TOOL_HOOK,
                                                             "-c", "features.goals=false", "--disable", "goals",
                                                             "--no-daemon", "go"]),
                               (["A", "--disable", "goals", "go"], [*self.guarded(), "--disable", "goals", "go"]),
                               # A --disable after a subcommand replaces the launcher's; the -c value stays.
                               (["A", "exec", "--disable", "plugin_hooks", "go"],
                                [*self.guarded(False), "exec", "--disable", "plugin_hooks", "go"]),
                               (["A", "resume", "--last", "--disable", "plugin_hooks"],
                                [*self.guarded(), "resume", "--last", "--disable", "plugin_hooks"])):
            with self.subTest(args=args):
                result = self.run_codex(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, LAUNCH_LINE.format(policy="five-hour off (default), weekly 10% (usage_reserve)"))
                self.assertEqual(self.recorded()["args"], expected)
        self.assertEqual(self.launches(), [])

    def test_plain_forms_launch_as_without_reserves(self):
        self.configure("[usage_reserve]\nfive_hour = 10\n")
        for args in (["A", "mcp", "list"], ["A", "features", "list"], ["A", "login", "status"], ["A", "--help"],
                     ["A", "exec", "--help"], ["A", "completion", "bash"]):
            with self.subTest(args=args):
                result = self.run_codex("--reserve-5h", "off", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                plain = self.recorded()
                result = self.run_codex(*args, env=dict.fromkeys(VARIABLES, "inherited"))
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(self.recorded(), plain)
        self.assertFalse(self.guard_dir.exists())

    def refused(self, args, reason, account="A", env=None):
        result = self.run_codex("--reserve-weekly", "10", *args, env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"codex-as: {account}: {reason}" + WITHOUT.format(account=account)))
        self.assertFalse(self.record.exists(), "a refused launch started Codex")
        self.assertFalse((self.root / f"accounts/{account}").exists(), "a refused launch prepared the account")

    def test_forms_and_options_the_hooks_cannot_cover_are_refused(self):
        self.trusted()
        for args, name in ((["cloud"], "cloud"), (["cloud-tasks"], "cloud"), (["app-server"], "app-server"),
                           (["exec-server"], "exec-server"), (["remote-control"], "remote-control"),
                           (["debug", "app-server", "send-message-v2", "hi"], "debug app-server")):
            with self.subTest(args=args):
                self.refused(["A", *args], f"codex {name} runs model work where the usage reserve guard's hooks "
                                           "cannot see or stop it")
        override = ("a -c or --config value under hooks would meet the usage reserve guard's hooks, which codex-as "
                    "passes as -c values of its own")
        disabled = "--disable hooks or a -c value under features turns off the hooks that enforce usage reserves"
        goals = ("--enable goals, or a -c value that sets features.goals or replaces the features table, would turn "
                 "on the goals that a launch with usage reserves keeps off: a goal continues on its own after a denied "
                 "tool call")
        for args, reason in ((["-c", "hooks.Stop=[]"], override), (["-chooks.x=1", "go"], override),
                             (["exec", "--config=hooks.x=1", "go"], override),
                             (["--disable", "hooks"], disabled), (["exec", "-c", "features.hooks=false", "go"], disabled),
                             (["--enable", "goals"], goals), (["resume", "--last", "--enable=goals"], goals),
                             (["exec", "-c", "features.goals=true", "go"], goals),
                             (["-c", "features={hooks=true}", "exec", "--disable", "plugin_hooks", "go"], goals),
                             (["exec", "--ephemeral", "go"], "--ephemeral keeps no session rollout, from which the "
                                                             "usage reserve guard reads the session's usage"),
                             (["--ignore-user-config"], "--ignore-user-config skips the configuration that holds "
                                                        "Codex's trust of the usage reserve guard's hooks")):
            with self.subTest(args=args):
                self.refused(["A", *args], reason)
        # Before a missing account is offered a login.
        self.refused(["missing", "exec", "--ephemeral", "go"], "--ephemeral keeps no session rollout, from which the "
                     "usage reserve guard reads the session's usage", account="missing")
        # Without a reserve, the same arguments reach Codex.
        result = self.run_codex("A", "exec", "--ephemeral", "go")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hooks_turned_off_in_a_configuration_file_are_refused(self):
        self.trusted()
        message = "{path} turns the hooks feature off under [features], so Codex would not run the usage reserve guard's hooks"
        shared = self.root / "config.toml"
        shared.write_text('model = "fixture-model"\n[features]\nhooks = false\n')
        self.refused(["A"], message.format(path=shared))
        shared.write_text('model = "fixture-model"\n[features]\nhooks = true\n')
        profile = self.root / "p.config.toml"
        profile.write_text("[features]\ncodex_hooks = false\n")
        self.refused(["A", "-p", "p", "exec", "go"], message.format(path=profile))
        project = self.base / ".codex/config.toml"
        project.parent.mkdir()
        project.write_text("[features]\nhooks = false\n")
        self.refused(["A", "go"], message.format(path=project))
        project.unlink()
        result = self.run_codex("--reserve-weekly", "10", "A", "-p", "q", "go")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_goals_on_in_a_managed_layer_are_refused(self):
        """The kit copy's managed layers lie in the test's directory: /etc is never written."""
        managed = self.base / "etc/codex/managed_config.toml"
        requirements = self.base / "etc/codex/requirements.toml"
        source = self.kit / "lib/codex-as/layout.py"
        text = source.read_text()
        line = 'MANAGED_CONFIG = (Path("/etc/codex/managed_config.toml"), Path("/etc/codex/requirements.toml"))'
        self.assertEqual(text.count(line), 1)
        source.write_text(text.replace(line, f"MANAGED_CONFIG = (Path({str(managed)!r}), Path({str(requirements)!r}))"))
        managed.parent.mkdir(parents=True)
        self.trusted()
        message = ("{path} sets goals under [features], which native Codex applies over the goals off of a launch with "
                   "usage reserves: a goal continues on its own after a denied tool call")
        for path in (managed, requirements):
            with self.subTest(path=path.name):
                path.write_text("[features]\ngoals = true\n")
                self.refused(["A", "exec", "go"], message.format(path=path))
                path.write_text("[features]\ngoals = false\n")
        result = self.run_codex("--reserve-weekly", "10", "A", "exec", "go")
        self.assertEqual(result.returncode, 0, result.stderr)
        managed.write_text("[features]\ngoals = true\n")
        result = self.run_codex("A", "exec", "go")
        self.assertEqual((result.returncode, result.stderr), (0, ""), "a launch without reserves read the managed layer")

    def test_a_headless_run_waits_for_an_interactive_launch_whose_tool_hook_ran(self):
        reason = ("Codex has not run the usage reserve guard's tool hook on this host yet, and a headless run skips "
                  "hooks that are not trusted without a word: start codex-as A once interactively, trust the hooks "
                  "in \"Hooks need review\" and let it run one tool call")
        for args in (["exec", "go"], ["review"], ["exec", "resume", "--last"]):
            with self.subTest(args=args):
                self.refused(["A", *args], reason)
        # The guard's tool hook, run by Codex in an interactive launch, makes the marker.
        result = self.run_codex("--reserve-weekly", "10", "A", env={"FAKE_HOOKS": '["tool"]'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded()["hooks"], [["tool", 0, "", ""]])
        self.assertTrue(self.marker.is_file())
        self.assertEqual(oct(self.marker.stat().st_mode & 0o777), "0o600")
        result = self.run_codex("--reserve-weekly", "10", "A", "exec", "go")
        self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(self.recorded()["args"], [*self.guarded(False), "exec", "go"])

    def test_a_headless_run_prints_the_guard_s_block_after_codex_exits(self):
        self.trusted()
        rollouts = json.dumps([{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [ANSWER, snapshot(95.0)]}])
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go",
                                env={"FAKE_ROLLOUTS": rollouts, "FAKE_HOOKS": '["prompt"]', "FAKE_EXIT": "0"})
        self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        [[kind, code, out, message]] = self.recorded()["hooks"]
        self.assertEqual((kind, code, out), ("prompt", 2, ""))
        self.assertRegex(message, r"^codex-as: A: five-hour window 5% remaining, at or below its 10% reserve; resets in ")
        self.assertIn("\nThis prompt was not sent.\nTo use the reserve, exit and run:\n"
                      "  codex-as --reserve-5h off A resume --last\n", message)
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        self.assertEqual(result.stderr, launch_line + message, "the block, after Codex exits, and no other line")
        self.assertEqual(self.launches(), [])
        self.assertTrue(self.marker.exists())
        # An interactive session shows its own blocks: nothing is printed after it.
        result = self.run_codex("--reserve-5h", "10", "A", env={"FAKE_ROLLOUTS": rollouts, "FAKE_HOOKS": '["prompt"]'})
        self.assertEqual(self.recorded()["hooks"][0][:2], ["prompt", 2])
        self.assertEqual(result.stderr, launch_line)
        self.assertEqual(self.launches(), [])

    def test_a_run_whose_hooks_did_not_run_is_reported_and_the_marker_goes(self):
        warning = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run {} (not trusted, "
                   "disabled, or managed hooks only). Start codex-as A interactively and trust them in "
                   "\"Hooks need review\".\n")
        cases = (  # rollout lines, hooks Codex ran, what the warning names
            ([ANSWER, snapshot(20.0)], [], "the guard's hooks"),
            ([CALL, snapshot(20.0)], [], "the guard's hooks"),
            ([CALL], [], "the guard's hooks"),  # a tool call shows a model request without rate limits too
            ([CALL, snapshot(20.0)], ["prompt"], "the guard's tool hook"),
            ([CALL, snapshot(20.0)], ["prompt", "tool"], None),
            ([CALL, snapshot(20.0)], ["tool"], None),
            ([ANSWER, snapshot(20.0)], ["prompt"], None),
            ([], [], None),  # no model request
        )
        sessions = self.root / "sessions"
        for form in (["exec", "go"], []):
            for lines, hooks, missing in cases:
                with self.subTest(form=form, lines=len(lines), hooks=hooks):
                    if sessions.exists():
                        for child in sessions.iterdir():
                            shutil.rmtree(child)
                    self.trusted()
                    rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": lines}] if lines else []
                    result = self.run_codex("--reserve-5h", "10", "A", *form, env={
                        "FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": json.dumps(hooks), "FAKE_EXIT": "3"})
                    self.assertEqual(result.returncode, 3, "the exit status is Codex's")
                    self.assertEqual([hook[:2] for hook in self.recorded()["hooks"]], [[kind, 0] for kind in hooks])
                    launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
                    self.assertEqual(result.stderr, launch_line + (warning.format(missing) if missing else ""))
                    self.assertEqual(self.marker.exists(), missing is None)
                    self.assertEqual(self.launches(), [], "the launch's state outlived it")

    def old_rollout(self, thread, seconds):
        """A rollout of this account's THREAD that started SECONDS ago, as Codex left it then."""
        then = time.time() - seconds
        day = self.root / "sessions" / time.strftime("%Y/%m/%d", time.localtime(then))
        day.mkdir(parents=True, exist_ok=True)
        path = day / ("rollout-" + time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(then)) + f"-{thread}.jsonl")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(then))
        meta = {"id": thread, "timestamp": stamp, "creator_user_id": USER, "creator_account_id": WORKSPACE}
        path.write_text("".join(json.dumps({"timestamp": stamp, **line}) + "\n"
                                for line in ({"type": "session_meta", "payload": meta}, ANSWER)))
        os.utime(path, (then, then))
        return path

    def hold_the_account(self):
        """Another codex-as client of A, which holds the account lease as long as the test runs."""
        result = self.run_codex("A", "mcp", "list")  # prepares the account home and its lease
        self.assertEqual(result.returncode, 0, result.stderr)
        fd = os.open(self.root / "accounts/.locks/A.lock", os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_SH)

    def test_a_resumed_session_of_any_day_is_this_run_s(self):
        """resume --last, the picker and exec resume --last append to a thread they do not name, whose
        rollout lies in the date directory of the day it started."""
        warning = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run the guard's hooks "
                   "(not trusted, disabled, or managed hooks only). Start codex-as A interactively and trust them in "
                   "\"Hooks need review\".\n")
        for form in (["resume", "--last"], ["resume"], ["exec", "resume", "--last"], ["resume", THREAD]):
            for days in (2, 40):
                with self.subTest(form=form, days=days):
                    if (self.root / "sessions").exists():
                        shutil.rmtree(self.root / "sessions")
                    self.trusted()
                    path = self.old_rollout(THREAD, days * 86400)
                    rollouts = [{"append": str(path), "lines": [CALL, snapshot(20.0)]}]
                    result = self.run_codex("--reserve-5h", "10", "A", *form,
                                            env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": "[]"})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
                    self.assertEqual(result.stderr, launch_line + warning)
                    self.assertFalse(self.marker.exists())
                    self.assertEqual(self.launches(), [])

    def test_another_session_of_the_account_is_not_this_run_s(self):
        """A session of the same account that started before this headless run and wrote during it is
        not this run's: exec writes a thread of its own."""
        self.trusted()
        other = self.old_rollout("019a0000-0000-7000-8000-0000000000bb", 600)
        rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [ANSWER, snapshot(20.0)]},
                    {"append": str(other), "lines": [CALL, snapshot(20.0)]}]
        for form in (["exec", "go"], ["review"], ["exec", "fork", UUID, "go"]):
            with self.subTest(form=form):
                result = self.run_codex("--reserve-5h", "10", "A", *form,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("not enforced", result.stderr)
                self.assertTrue(self.marker.exists())
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_another_launch_of_the_account_keeps_its_older_sessions_and_the_marker(self):
        """While another codex-as client of the account runs at this run's start or end, an older session
        written during the run may be that client's: it is left out, and the marker stays even when the
        run's own sessions show that the hooks did not run, since those may be that client's too."""
        self.hold_the_account()
        other = self.old_rollout("019a0000-0000-7000-8000-0000000000bb", 600)
        for form in ([], ["resume", "--last"]):
            with self.subTest(form=form):
                self.trusted()
                rollouts = [{"append": str(other), "lines": [CALL, snapshot(20.0)]}]
                result = self.run_codex("--reserve-5h", "10", "A", *form,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": "[]"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("not enforced", result.stderr)
                self.assertTrue(self.marker.exists())
        rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [CALL, snapshot(20.0)]}]
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go",
                                env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": "[]"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the usage reserve was not enforced in this run", result.stderr)
        self.assertTrue(self.marker.exists(), "another launch of the account ran, yet the marker went")
        self.assertEqual(self.launches(), [])

    def test_another_account_s_tool_calls_are_not_this_run_s(self):
        self.trusted()
        rollouts = [{"thread": THREAD, "creator": ["user-fixture-B", "workspace-fixture-B"], "lines": [CALL]}]
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("not enforced", result.stderr)
        self.assertTrue(self.marker.exists())

    def test_the_launch_state_goes_on_every_exit(self):
        """A signal to codex-as reaches Codex, and the launch's state goes before codex-as ends: SIGTERM
        and SIGHUP end it by the same signal, Ctrl-C reaches the whole process group, and SIGQUIT, whose
        default action would leave the state behind, ends it with Codex's status."""
        self.trusted()
        ready = self.base / "ready"
        for signum, group, expected in ((signal.SIGTERM, False, -signal.SIGTERM), (signal.SIGHUP, False, -signal.SIGHUP),
                                        (signal.SIGINT, True, -signal.SIGINT), (signal.SIGQUIT, False, 131)):
            for form in ([], ["exec", "go"]):
                with self.subTest(signal=signum.name, form=form):
                    ready.unlink(missing_ok=True)
                    child = subprocess.Popen([str(self.kit / "bin/codex-as"), "--reserve-5h", "10", "A", *form],
                                             cwd=self.cwd, env=self.env | {"FAKE_READY": str(ready)},
                                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             text=True, start_new_session=True)
                    try:
                        deadline = time.monotonic() + 30
                        while not ready.exists() and time.monotonic() < deadline and child.poll() is None:
                            time.sleep(0.05)
                        self.assertTrue(ready.exists(), "Codex did not start")
                        self.assertEqual(len(self.launches()), 1)
                        (os.killpg if group else os.kill)(child.pid, signum)
                        out, err = child.communicate(timeout=60)
                    finally:
                        kill_group(child.pid)
                    self.assertEqual(child.returncode, expected, err)
                    self.assertNotIn("Traceback", err)
                    self.assertEqual(self.launches(), [], "the launch's state outlived it")
        # A native that cannot be started: one message and no state.
        result = self.run_codex("--reserve-5h", "10", "A", env={"FAKE_BREAK": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.launches(), [], "the launch's state outlived it")

    def test_old_launch_state_is_pruned_and_markers_stay(self):
        self.trusted()
        now = int(time.time())
        old, recent = f"codex-as-A-1-{now - 31 * 86400}", f"codex-as-B-2-{now - 86400}"
        for name in (old + ".launched", old + ".ran.prompt", recent + ".launched", "codex-hooks-0123456789abcdef"):
            (self.guard_dir / name).touch()
        result = self.run_codex("--reserve-5h", "10", "A")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(os.listdir(self.guard_dir)),
                         sorted(["codex-hooks-0123456789abcdef", self.marker.name, recent + ".launched"]))

    def test_the_minimum_codex_is_the_same_with_reserves(self):
        result = self.run_codex("--reserve-5h", "10", "A", env={"FAKE_VERSION": "codex-cli 0.161.0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded()["args"], self.guarded())
        result = self.run_codex("--reserve-5h", "10", "A", env={"FAKE_VERSION": "codex-cli 0.160.9"})
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", "codex-as: Codex version 0.161.0 or newer is required; found codex-cli 0.160.9.\n"))
        self.assertFalse(self.record.exists())

    def test_a_kit_without_the_guard_is_an_incomplete_install(self):
        (self.kit / "lib/coding-agent-kit/guard.py").unlink()
        result = self.run_codex("--reserve-5h", "10", "A")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"codex-as: incomplete install at {self.kit}\n"))
        self.assertFalse(self.record.exists())
        result = self.run_codex("A", "exec", "go")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "fake codex ran\n", ""))


CODEX_BACKEND = ROOT / "tests/fixtures/guard/codex_backend.py"
CLOSED_PORT = ("127.0.0.1", 9)  # the discard port: nothing listens there unless a host runs that service
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


def kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def write(path, text, mode=0o600):
    path.write_text(text)
    path.chmod(mode)


class Sandbox:
    """A Codex that reaches nothing of this host's user: a temporary HOME and CODEX_HOME, an environment
    built from scratch, git and ssh shims first on PATH, every proxy at a closed loopback port (Codex
    runs git ls-remote for its plugin marketplace at every start) and no session bus.
    The model provider is the loopback fake; nothing holds a credential."""

    def __init__(self, base, bindir):
        self.base, self.bindir = base, bindir
        self.home, self.work, self.shims = base / "home", base / "work", base / "shims"
        self.codex_home = self.home / ".codex"
        for path in (self.codex_home, self.work, self.shims, base / "tmp", base / "run",
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

    def configure(self, port):
        """The loopback fake as the model provider; no approval asked, no sandbox helper (bubblewrap
        cannot create namespaces on every host), and the work directory trusted."""
        write(self.codex_home / "config.toml", "\n".join((
            'model = "gpt-5.5"', 'model_provider = "fixture"', "check_for_update_on_startup = false",
            'approval_policy = "never"', 'sandbox_mode = "danger-full-access"', "",
            "[model_providers.fixture]", 'name = "fixture"', f'base_url = "http://127.0.0.1:{port}/v1"',
            'wire_api = "responses"', "request_max_retries = 0", "stream_max_retries = 0", "",
            f"[projects.{json.dumps(str(self.work))}]", 'trust_level = "trusted"', "")))

    def env(self):
        closed = "http://%s:%d" % CLOSED_PORT
        return {"PATH": f"{self.shims}:/usr/local/bin:/usr/bin:/bin:{self.bindir}", "HOME": str(self.home),
                "LANG": "C.UTF-8", "TERM": "dumb", "SHELL": "/bin/sh", "TMPDIR": str(self.base / "tmp"),
                "XDG_RUNTIME_DIR": str(self.base / "run"),
                **{f"XDG_{name.upper()}_HOME": str(self.base / "xdg" / name) for name in ("config", "cache", "data", "state")},
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.base / 'run/no-bus'}", "CODEX_HOME": str(self.codex_home),
                **{name: closed for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")},
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                # Codex runs /usr/bin/git by its path, past the shim: git itself may use local repositories only.
                "GIT_SSH_COMMAND": str(self.shims / "ssh"), "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_ALLOW_PROTOCOL": "file", "GIT_CEILING_DIRECTORIES": str(self.base)}

    def run(self, codex, args, env, timeout=90):
        """(exit code, stdout, stderr) of CODEX ARGS in a session of its own, which is killed afterwards."""
        child = subprocess.Popen([codex, *args], cwd=self.work, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            out, err = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(child.pid)
            out, err = child.communicate()
            raise AssertionError(f"codex {args} ran longer than {timeout} s: {out[-2000:]!r} {err[-2000:]!r}")
        finally:
            kill_group(child.pid)
        return child.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def strays(self):
        """Processes still running with this sandbox's CODEX_HOME, killed; Linux only."""
        if not os.path.isdir("/proc/self"):
            return []
        needle, found = f"CODEX_HOME={self.codex_home}".encode(), []
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


def load_kit_module(kit, name):
    spec = importlib.util.spec_from_file_location(f"codex_guard_real_{name}", kit / f"lib/coding-agent-kit/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RealCodex(unittest.TestCase):
    """The guard's hook contract against the real Codex, offline: the two hook values and goals off as
    codex-as passes them, a launch as codex-as exports it, and a loopback fake of the Responses API that
    answers from a script and sends rate limits as the real backend does. codex exec runs directly on the
    native, with --dangerously-bypass-hook-trust standing in for the user's trust in "Hooks need review";
    the trust prompt and the terminal session are left to the live acceptance."""

    @classmethod
    def setUpClass(cls):
        found = shutil.which("codex")
        if not found:
            raise unittest.SkipTest("Codex (codex) is not on PATH")
        try:
            socket.create_connection(CLOSED_PORT, timeout=2).close()
        except OSError:
            pass
        else:
            raise unittest.SkipTest("127.0.0.1:9 accepts connections, so it cannot stand in for a closed proxy")
        # The directory codex was found in goes last on PATH, for an install whose codex is a node script.
        cls.codex, cls.bindir = os.path.realpath(found), os.path.dirname(os.path.abspath(found))
        with tempfile.TemporaryDirectory(prefix="codex-guard-real-version-") as base:
            sandbox = Sandbox(Path(base).resolve(), cls.bindir)
            code, out, _ = sandbox.run(cls.codex, ["--version"], sandbox.env(), timeout=60)
            sandbox.strays()
            if code:
                raise unittest.SkipTest(f"codex --version exited {code}")
            try:
                check.codex_version(out)  # the minimum of every codex-as launch, with reserves too
            except ValueError as error:
                raise unittest.SkipTest(f"codex --version: {error}") from None
            code, out, _ = sandbox.run(cls.codex, ["features", "list"], sandbox.env(), timeout=60)
            sandbox.strays()
        cls.features = {line.split()[0] for line in out.splitlines() if line.strip()} if code == 0 else set()

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        tmp = tempfile.TemporaryDirectory(prefix="codex-guard-real-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.sandbox = Sandbox(base, self.bindir)
        # The guard runs from a kit whose path needs quoting, through the shell Codex runs hooks with.
        self.kit = base / "kit o'clock dir"
        shutil.copytree(ROOT / "lib", self.kit / "lib")
        self.reserve = load_kit_module(self.kit, "reserve")
        state = base / "state/coding-agent-kit"
        self.accounts, self.guard_dir = state / "accounts", state / "guard"
        for directory in (state, self.accounts, self.guard_dir):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.record = self.accounts / "codex-A.json"
        self.marker = self.guard_dir / f"codex-hooks-{FINGERPRINT}"
        self.calls = base / "shell calls"
        self.requests = base / "requests.jsonl"

    def run_codex(self, prompt, recorded=None, sent=None, options=()):
        """codex exec OPTIONS PROMPT under the guard with the five-hour reserve at 10: RECORDED percent of
        the five-hour window used in the account record of earlier runs, SENT percent in the rate limits
        the fake sends with every answer (None: no record, no rate limits)."""
        now = int(time.time())
        if recorded is not None:
            window = {"used_percent": float(recorded), "window_minutes": 300, "resets_at": now + 3600}
            load_kit_module(self.kit, "accounts").write(self.record, {
                "email": "alice@example.com", "plan": "plus",
                "usage": {"primary": window, "secondary": None, "seen_at": now - 60}})
        limits = {} if sent is None else {"primary": [sent, 300, 3600], "secondary": [20.0, 10080, 86400]}
        fake = subprocess.Popen([sys.executable, "-I", "-B", "-S", str(CODEX_BACKEND), "--log", str(self.requests),
                                 "--limits", json.dumps(limits), "--shell", f"echo call >> {shlex.quote(str(self.calls))}"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env={}, start_new_session=True)
        for cleanup in (fake.stdout.close, fake.stdin.close, lambda: fake.wait(10), lambda: kill_group(fake.pid)):
            self.addCleanup(cleanup)
        self.sandbox.configure(int(fake.stdout.readline()))
        guard_launch = load_kit_module(self.kit, "guard_launch")
        policy = self.reserve.Policy("codex-as", "A",
                                     self.reserve.Setting("five_hour", self.reserve.flag_value("10"), "invocation"),
                                     self.reserve.Setting("weekly", None, "default"))
        env = self.sandbox.env()
        state = guard_launch.prepare(env, str(self.guard_dir), "codex-as", "A", policy, str(self.record), int(time.time()),
                                     marker=str(self.marker))
        self.assertEqual(env["CODING_AGENT_KIT_GUARD_SCRIPT"], str(self.kit / "lib/coding-agent-kit/guard.py"))
        # The values codex-as passes before the caller's arguments, and the stand-in for the user's trust.
        args = [*(word for value in launch.GUARD_HOOKS for word in ("-c", value)), *launch.GOALS_OFF,
                "--dangerously-bypass-hook-trust", "exec", *options, "--skip-git-repo-check", prompt]
        code, out, err = self.sandbox.run(self.codex, args, env)
        self.assertEqual(self.sandbox.strays(), [], "Codex left a process behind")
        refused = [line for line in self.sandbox.netlog.read_text().splitlines() if line.startswith("REFUSED ssh")]
        self.assertEqual(refused, [], "Codex tried to reach a host through ssh")
        lines = self.requests.read_text().splitlines() if self.requests.exists() else []
        models = [entry for entry in map(json.loads, lines) if entry["path"].endswith("/responses")]
        # Without the flags, codex-cli 0.169.0 offers get_goal, create_goal and update_goal here.
        for entry in models:
            self.assertEqual([name for name in entry["tools"] if "goal" in name.lower()], [],
                             "goals are on despite codex-as's goals off")
        self.assertNotRegex(err, r"(?im)^.*\b(goals?|unknown feature|feature flag)\b.*$",
                            "Codex said something about the goals flag")
        return code, out, err, state, models

    def shell_calls(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def assert_state(self, state, present, absent):
        for name in present:
            self.assertTrue(os.path.exists(state + name), f"{name} is missing")
        for name in absent:
            self.assertFalse(os.path.exists(state + name), f"{name} exists")

    def test_a_prompt_at_the_reserve_is_never_sent(self):
        code, out, err, state, models = self.run_codex("PLAIN say hello", recorded=95)
        self.assertEqual(models, [], "the blocked prompt reached the model provider")
        blocked = Path(state + ".blocked").read_text()
        self.assertRegex(blocked, r"^codex-as: A: five-hour window 5% remaining, at or below its 10% reserve; resets in ")
        self.assertIn("\nThis prompt was not sent.\nTo use the reserve, exit and run:\n"
                      "  codex-as --reserve-5h off A resume --last", blocked)
        # codex exec shows no reason and exits 0, so codex-as prints .blocked after it exits.
        self.assertEqual((code, out), (0, ""), err)
        self.assert_state(state, (".launched", ".ran.prompt"), (".ran.tool",))
        self.assertFalse(self.marker.exists(), "the marker without a tool event")

    def test_goals_stay_off_when_a_subcommand_repeats_disable(self):
        """clap keeps one --disable list, and a --disable after the subcommand replaces the launcher's
        --disable goals; with it alone codex-cli 0.169.0 offers the goal tools here. The launcher's
        -c features.goals=false holds."""
        if "plugin_hooks" not in self.features:
            self.skipTest("this Codex has no plugin_hooks feature to disable")
        code, out, err, state, models = self.run_codex("PLAIN say hello", recorded=50,
                                                       options=("--disable", "plugin_hooks"))
        self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
        self.assertEqual([entry["script"] for entry in models], ["PLAIN"])

    def test_a_prompt_below_the_reserve_gets_its_reply(self):
        code, out, err, state, models = self.run_codex("PLAIN say hello", recorded=50)
        self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
        self.assertEqual([entry["script"] for entry in models], ["PLAIN"])
        self.assert_state(state, (".ran.prompt",), (".blocked", ".ran.tool"))

    def test_tool_calls_below_the_reserve_run(self):
        code, out, err, state, models = self.run_codex("TOOL run the command", sent=50)
        self.assertEqual((code, out.strip()), (0, "TOOL-DONE"), err)
        self.assertEqual(self.shell_calls(), 2)
        self.assertEqual(len(models), 3)
        self.assert_state(state, (".ran.prompt", ".ran.tool"), (".blocked",))
        self.assertTrue(self.marker.is_file(), "the tool hook made no marker")

    def test_a_snapshot_at_the_reserve_stops_the_next_tool_call(self):
        """The record of earlier runs is below the reserve; the first answer's rate limits reach the
        rollout after its call ran, and the second call's tool hook reads them there."""
        code, out, err, state, models = self.run_codex("TOOL run the command", recorded=50, sent=95)
        self.assertEqual(self.shell_calls(), 1, "the call after the snapshot at the reserve ran")
        blocked = Path(state + ".blocked").read_text()
        self.assertRegex(blocked, r"^codex-as: A: five-hour window 5% remaining, at or below its 10% reserve; resets in ")
        self.assertIn("\nThis tool call was stopped. Stop working now and tell the user why.\nTo use the reserve, "
                      "exit and run:\n  codex-as --reserve-5h off A resume --last", blocked)
        # Codex gives the model the guard's reason as the call's output; the model then answers.
        self.assertEqual(len(models), 3)
        self.assertIn(blocked.strip(), models[2]["output"])
        self.assertEqual((code, out.strip()), (0, "TOOL-DONE"), err)
        self.assert_state(state, (".ran.prompt", ".ran.tool"), ())
        self.assertTrue(self.marker.is_file(), "the tool hook made no marker")


if __name__ == "__main__":
    unittest.main()
