"""codex-as with usage reserves: launch forms, refused options, hook settings and the guarded launch.

LaunchForms, Version, HooksOff, HookState and ManagedGoals run
check.arguments() and layout's hooks_off(), hook_off() and goals_on() in
process against a temporary HOME, Codex home and working directory. Hooks
pins the hook definitions in process, and RunAttribution the check after a run
at edges a launch cannot set (exact times, .blocked, a native's signal).
HookSelection checks the cases codex-guard.sh --hook (the pre-commit hook)
selects. GuardedLaunch runs codex-as from a kit
copy under a path with a space and a quote, whose /etc/codex layers lie in the
test's directory, in a temporary HOME, against a
fake codex that records its argv and environment, writes rollouts as native
Codex does and runs the guard's hooks the way Codex runs them: the command of
the -c value through a shell, with the event on stdin. No native, provider,
credential or network is involved. RealCodex runs the real codex, when it is on
PATH, in a temporary HOME against tests/fixtures/guard/codex_backend.py on
127.0.0.1, with every proxy at a closed loopback port; it skips otherwise. Of
this host's user it reads only the codex install's own files where they lie
(Sandbox says which).
"""

import base64
import dataclasses
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
        # Project layers above the test's directory (/tmp/.codex, or the real home's under a TMPDIR there)
        # never count here.
        patcher = patch.object(check, "PROJECT_TOP", self.base)
        patcher.start()
        self.addCleanup(patcher.stop)

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
                         ["exec", "-m", "o3", "go"],
                         # After -- help is a prompt, and review has no subcommands: help is its prompt.
                         ["exec", "--", "help"], ["review", "help"], ["exec", "go", "help"]),
            # remote-control runs an app server that remote clients drive, and debug app-server
            # send-message-v2 runs a turn through one: model work as in app-server.
            "refused": (["cloud"], ["cloud-tasks"], ["cloud", "exec", "--env", "x", "go"], ["app-server"],
                        ["exec-server"], ["remote-control"], ["remote-control", "--no-daemon"],
                        ["debug", "app-server", "send-message-v2", "hi"], ["-c", "model=x", "app-server"]),
            "plain": (["mcp", "list"], ["mcp"], ["plugin"], ["features", "list"], ["login", "status"],
                      ["apply"], ["a"], ["sandbox", "linux", "--", "exec"], ["debug"], ["debug", "models"],
                      ["debug", "prompt-input", "hi"], ["completion", "bash"], ["update"], ["doctor"],
                      ["archive", UUID], ["delete", UUID], ["unarchive", UUID], ["migrate-rollouts"],
                      ["migrate-nfs-db"], ["--no-daemon", "migrate-nfs-db"],
                      # git-credential hands the account's login to Git's credential helper protocol.
                      ["git-credential", "get"], ["git-credential", "erase"], ["-c", "model=x", "git-credential", "capability"],
                      ["responses-api-proxy"], ["stdio-to-uds", "x"], ["execpolicy"],
                      # Help and version print and run nothing.
                      ["--help"], ["-h"], ["-V"], ["--version"], ["help"], ["help", "exec"],
                      ["resume", "--help"], ["exec", "--help"], ["exec", "go", "-V"], ["review", "-h"],
                      # exec has subcommands, so clap reads help after it as its help subcommand.
                      ["exec", "help"], ["e", "help", "resume"], ["exec", "-m", "x", "help"], ["exec", "help", "fork"],
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

    def test_worktree(self):
        """--worktree moves the session into a Git worktree that Codex manages under CODEX_HOME, in any
        position where Codex takes it (root, exec, resume, fork)."""
        for args in (["--worktree"], ["--worktree", "go"], ["exec", "--worktree", "go"], ["--worktree", "exec", "go"],
                     ["fork", UUID, "--worktree"], ["resume", "--last", "--worktree"],
                     ["exec", "resume", "--last", "--worktree", "go"], ["review", "--worktree"]):
            with self.subTest(args=args):
                self.assertTrue(self.plan(args).worktree)
        for args in ([], ["exec", "go"], ["exec", "--worktree go"], ["exec", "go", "--", "--worktree"],
                     ["exec", "-m", "--worktree", "go"]):
            with self.subTest(args=args):
                self.assertFalse(self.plan(args).worktree)
        self.assertEqual(self.plan(["exec", "--worktree", "go"]).form, "headless")

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
                                  plan.ignore_user_config, plan.worktree), (False, False, False, False, False, False))

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

    def test_the_account_home_s_profile(self):
        """Native -p reads $CODEX_HOME/NAME.config.toml, the account home's: the launch links the shared
        file there when it exists, and otherwise the account home's own file is the one Codex loads."""
        home = self.root / "accounts/A"
        own = self.write(home / "q.config.toml", "[features]\nhooks = false\n")
        self.assertEqual(layout.hooks_off(check, self.root, self.cwd, "q", home), own)
        self.assertIsNone(layout.hooks_off(check, self.root, self.cwd, "q"), "without the home only the shared file")
        self.assertIsNone(layout.hooks_off(check, self.root, self.cwd, "r", home))
        # The managed link to the shared file: the shared file is named.
        own.unlink()
        shared = self.write(self.root / "q.config.toml", "[features]\nhooks = false\n")
        own.symlink_to(shared)
        self.assertEqual(layout.hooks_off(check, self.root, self.cwd, "q", home), shared)
        # A link elsewhere is followed, as Codex follows it.
        shared.unlink()
        own.unlink()
        elsewhere = self.write(self.base / "elsewhere.toml", "[features]\ncodex_hooks = false\n")
        own.symlink_to(elsewhere)
        self.assertEqual(layout.hooks_off(check, self.root, self.cwd, "q", home), own)

    def test_project_layers_end_at_the_top(self):
        """check.PROJECT_TOP is / for a launch, so every parent's layer counts; above a lower top none does."""
        self.assertEqual(launch.guard.PROJECT_TOP, Path("/"))
        self.assertIn(Path("/.codex/config.toml"), launch.guard.project_layers(self.cwd))
        self.write(self.base / ".codex/config.toml", "[features]\nhooks = false\n")
        self.assertTrue(self.off())
        with patch.object(check, "PROJECT_TOP", self.base / "project"):
            self.assertEqual(check.project_layers(self.cwd), {self.cwd / ".codex/config.toml",
                                                              self.base / "project/.codex/config.toml"})
            self.assertFalse(self.off(), "a layer above the top counts")

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


class HookState(Fixture):
    """enabled under [hooks.state."KEY"] of the shared config.toml or the selected profile's file, for the
    key of one of the guard's hooks, set to anything but true: native Codex skips that hook even when it
    is trusted or its trust is bypassed (measured with codex-cli 0.169.0)."""

    KEYS = ("/<session-flags>/config.toml:user_prompt_submit:0:0", "/<session-flags>/config.toml:pre_tool_use:0:0")

    def off(self, profile=None):
        return layout.hook_off(check, self.root, self.KEYS, profile)

    def test_the_shared_file(self):
        path = self.root / "config.toml"
        self.assertIsNone(self.off(), "no config.toml")
        for key in self.KEYS:
            for text, off in (('enabled = false\n', True), ('enabled = "false"\n', True), ("enabled = 0\n", True),
                              ("enabled = true\n", False), ('trusted_hash = "sha256:00"\n', False),
                              ('trusted_hash = "sha256:00"\nenabled = false\n', True)):
                with self.subTest(key=key, text=text):
                    path.write_text(f"[hooks.state.{json.dumps(key)}]\n{text}")
                    self.assertEqual(self.off(), path if off else None)
                    self.assertEqual(self.off("p"), path if off else None, "a profile layers on the shared file")
        for text in ('[hooks.state."/<session-flags>/config.toml:pre_tool_use:2:0"]\nenabled = false\n',
                     '[hooks.state."/<session-flags>/config.toml:stop:0:0"]\nenabled = false\n',
                     "[features]\nhooks = true\n", 'hooks = "x"\n', '[hooks]\nstate = "x"\n',
                     f'[hooks]\nstate = {{ {json.dumps(self.KEYS[0])} = "x" }}\n'):
            with self.subTest(text=text):
                path.write_text(text)
                self.assertIsNone(self.off())

    def test_the_selected_profile(self):
        (self.root / "config.toml").write_text(f"[hooks.state.{json.dumps(self.KEYS[1])}]\nenabled = true\n")
        profile = self.root / "p.config.toml"
        profile.write_text(f"[hooks.state.{json.dumps(self.KEYS[1])}]\nenabled = false\n")
        self.assertEqual(self.off("p"), profile)
        self.assertIsNone(self.off(), "another profile's file is not loaded")
        self.assertIsNone(self.off("q"))

    def test_the_account_home_s_profile(self):
        """The account home's own NAME.config.toml, which native -p reads when the shared one is absent."""
        home = self.root / "accounts/A"
        home.mkdir(parents=True)
        own = home / "q.config.toml"
        own.write_text(f"[hooks.state.{json.dumps(self.KEYS[0])}]\nenabled = false\n")
        self.assertEqual(layout.hook_off(check, self.root, self.KEYS, "q", home), own)
        self.assertIsNone(layout.hook_off(check, self.root, self.KEYS, "q"))
        self.assertIsNone(layout.hook_off(check, self.root, self.KEYS, "p", home))
        own.unlink()
        shared = self.root / "q.config.toml"
        shared.write_text(f"[hooks.state.{json.dumps(self.KEYS[1])}]\nenabled = false\n")
        own.symlink_to(shared)
        self.assertEqual(layout.hook_off(check, self.root, self.KEYS, "q", home), shared)


class RunAttribution(Fixture):
    """The check after a run in process, at the edges a launch cannot set: exact times around the run's
    start and end, and what codex-as reads of .blocked and does with a native's signal."""

    THREAD, OTHER = "019a0000-0000-7000-8000-00000000000a", "019a0000-0000-7000-8000-0000000000bb"
    OWNER = ("user-fixture-A", "workspace-fixture-A")

    def setUp(self):
        super().setUp()
        milliseconds = int(time.time() * 1000)
        # A moment as Codex stamps it, to the millisecond, and that stamp.
        self.t0 = milliseconds / 1000
        self.stamp = (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(milliseconds // 1000))
                      + ".%03dZ" % (milliseconds % 1000))
        self.plan_of = lambda args: dataclasses.replace(self.plan(args), cwd=self.cwd)

    def rollout(self, thread, lines, **meta):
        day = self.root / "sessions/2026/11/16"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-2026-11-16T10-00-00-{thread}.jsonl"
        payload = {"id": thread, "session_id": thread, "timestamp": self.stamp, "cwd": str(self.cwd), "source": "exec",
                   "thread_source": "user", "creator_user_id": self.OWNER[0], "creator_account_id": self.OWNER[1], **meta}
        path.write_text("".join(json.dumps({"timestamp": self.stamp, **line}) + "\n"
                                for line in ({"type": "session_meta", "payload": payload}, *lines)))
        return path

    def threads(self, args, begun, ended, alone=True):
        return launch.run_threads(self.root, self.plan_of(args), self.OWNER, begun, ended, alone)

    def test_a_session_stamped_in_the_run_s_first_millisecond_is_the_run_s(self):
        """Codex truncates its stamps to the millisecond: a session and a line written half a millisecond
        after the run began are stamped before it, and still count."""
        path = self.rollout(self.THREAD, [CALL])
        begun = self.t0 + 0.0005
        self.assertEqual(self.threads(["exec", "go"], begun, begun + 10), ([], [[path]], []))
        self.assertEqual(launch.activity([path], begun, begun + 10), (True, True, True))
        self.assertEqual(launch.activity([path], self.t0 + 0.002, self.t0 + 10), (False, False, False))

    def test_a_session_that_started_after_the_run_ended_is_not_the_run_s(self):
        """As a host on the same home directory whose clock runs ahead writes it."""
        path = self.rollout(self.THREAD, [CALL])
        self.assertEqual(self.threads(["exec", "go"], self.t0 - 10, self.t0 - 0.5), ([], [], []))
        self.assertEqual(self.threads([], self.t0 - 10, self.t0 - 0.5), ([], [], []))
        self.assertEqual(self.threads([], self.t0 - 10, self.t0), ([], [], []), "source exec is not the TUI's")
        self.assertEqual(self.threads(["exec", "go"], self.t0 - 10, self.t0), ([], [[path]], []))

    def test_a_sub_agent_is_attached_by_session_id_or_by_parent(self):
        main = self.rollout(self.THREAD, [ANSWER])
        spawned = {"subagent": {"thread_spawn": {"parent_thread_id": self.THREAD, "depth": 1}}}
        by_parent = self.rollout(self.OTHER, [CALL], source=spawned, session_id="019a0000-0000-7000-8000-0000000000cc",
                                 parent_thread_id=self.THREAD)
        self.rollout("019a0000-0000-7000-8000-0000000000dd", [CALL], source=spawned,
                     session_id="019a0000-0000-7000-8000-0000000000cc", parent_thread_id=UUID)
        [[group]] = self.threads(["exec", "go"], self.t0 - 1, self.t0 + 10)[1:2]
        self.assertEqual(sorted(group), sorted([main, by_parent]))

    def test_a_tui_session_elsewhere_that_names_none_of_the_run_s_is_unsure(self):
        """A new TUI session in another directory that names no session of the run, also by a forked_from_id
        that is no id or a directory with a NUL in it, may be another client's: MAYBE, and never an error,
        which would leave the check after the run nothing to tell."""
        main = self.rollout(self.THREAD, [ANSWER], source="cli")
        odd = self.rollout(self.OTHER, [CALL], source="cli", cwd=str(self.base), forked_from_id=["x"])
        nul = self.rollout("019a0000-0000-7000-8000-0000000000cc", [CALL], source="cli", cwd=str(self.cwd) + "\0x")
        linked = self.rollout("019a0000-0000-7000-8000-0000000000dd", [CALL], source="cli", cwd=str(self.base),
                              forked_from_id=self.THREAD.upper())
        sure, either, maybe = self.threads([], self.t0 - 1, self.t0 + 10)
        self.assertEqual((sorted(sure), either, sorted(maybe)), (sorted([[main], [linked]]), [], sorted([[odd], [nul]])))
        sure, either, maybe = self.threads([], self.t0 - 1, self.t0 + 10, alone=False)
        self.assertEqual((sure, either, sorted(maybe)), ([], [], sorted([[main], [linked], [odd], [nul]])))

    def test_a_session_meta_line_longer_than_one_read_is_read(self):
        path = self.rollout(self.THREAD, [CALL], base_instructions={"text": "x" * (3 * launch.CHUNK)})
        self.assertEqual(self.threads(["exec", "go"], self.t0 - 1, self.t0 + 10), ([], [[path]], []))
        self.assertEqual(launch.session_meta(path)["id"], self.THREAD)

    def test_only_a_regular_file_at_blocked_is_read(self):
        state = str(self.base / "codex-as-A-1-1")
        Path(state + ".blocked").mkdir()
        self.assertIsNone(launch.blocked_message(state))
        os.rmdir(state + ".blocked")
        os.mkfifo(state + ".blocked")
        try:
            writer = os.open(state + ".blocked", os.O_RDWR | os.O_NONBLOCK)  # holds data, never blocks (Linux, macOS)
        except OSError as error:
            self.skipTest(f"a FIFO cannot be held open here: {error}")
        try:
            os.write(writer, b"codex-as: A: not the guard's\n")
            self.assertIsNone(launch.blocked_message(state))
        finally:
            os.close(writer)
        os.unlink(state + ".blocked")
        Path(state + ".blocked").write_text("codex-as: A: stopped\n")
        self.assertEqual(launch.blocked_message(state), "codex-as: A: stopped")

    def test_a_launch_that_arrives_in_between_changes_the_lease_file_s_size(self):
        """Layout.arrive: each launch of the account appends to the lease file it holds, so the size a later
        acquire() reads differs once another launch arrived, also one that ended again meanwhile."""
        run, end, second = (layout.Layout(check, "A", self.config) for _ in range(3))
        for lease in (run, end, second):
            self.addCleanup(lease.close)
        self.assertTrue(run.acquire())
        fcntl.flock(run.fd, fcntl.LOCK_SH)  # as a launch holds it while Codex runs
        mark = run.arrive()
        self.assertEqual(mark, run.size + 1)
        self.assertFalse(end.acquire())
        self.assertEqual(end.size, mark, "nothing arrived")
        end.close()
        self.assertFalse(second.acquire())
        second.arrive()
        second.close()
        self.assertFalse(end.acquire())
        self.assertEqual(end.size, mark + 1)
        info = (self.root / "accounts/.locks/A.lock").lstat()
        self.assertEqual((info.st_mode & 0o7777, info.st_nlink), (0o600, 1))

    def test_a_native_killed_by_a_signal_is_re_raised(self):
        """SIGQUIT among them: codex-as forwards it, and a native whose default action it ended ends codex-as
        the same way (here with os.kill and signal.signal stubbed, so nothing dumps a core)."""
        for signum in (signal.SIGQUIT, signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGPIPE):
            with self.subTest(signal=signum.name), patch.object(launch.os, "kill") as kill, \
                    patch.object(launch.signal, "signal") as handler:
                self.assertEqual(launch.exit_status(-signum), 128 + signum)
                kill.assert_called_once_with(os.getpid(), signum)
                handler.assert_called_once_with(signum, signal.SIG_DFL)
        with patch.object(launch.os, "kill") as kill, patch.object(launch.signal, "signal") as handler:
            self.assertEqual(launch.exit_status(-signal.SIGKILL), 128 + signal.SIGKILL)
            kill.assert_called_once_with(os.getpid(), signal.SIGKILL)
            handler.assert_not_called()
            kill.reset_mock()
            self.assertEqual(launch.exit_status(-signal.SIGUSR1), 128 + signal.SIGUSR1)
            kill.assert_not_called()
            self.assertEqual(launch.exit_status(3), 3)


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
                self.assertIn(key, launch.HOOK_KEYS)
                command = f'"$CODING_AGENT_KIT_GUARD_PYTHON" -I -B -S "$CODING_AGENT_KIT_GUARD_SCRIPT" {kind}'
                self.assertEqual(tomllib.loads("v = " + raw)["v"],
                                 [{"hooks": [{"type": "command", "command": command, "timeout": 20}]}])
                # The launcher's own value would be a caller's hooks override: only the caller's are scanned.
                self.assertTrue(check.arguments(["-c", value], None).hooks_override)


class HookSelection(unittest.TestCase):
    """The pre-commit hook runs codex-guard.sh --hook: every in-process class, and of GuardedLaunch one case
    per launch form; ./tests/run.sh runs every case. unittest's -k matches a substring of each test's name."""

    # The GuardedLaunch case the hook runs for each launch form.
    FORMS = {"interactive": "test_an_interactive_launch_gets_the_hooks_the_guard_and_goals_off",
             "headless": "test_a_headless_run_prints_the_guard_s_block_after_codex_exits",
             "refused": "test_forms_the_hooks_cannot_see_are_refused",
             "plain": "test_a_plain_form_launches_as_without_reserves"}

    def test_the_hook_runs_every_in_process_class_and_named_launch_cases(self):
        text = (ROOT / "tests/codex-guard.sh").read_text()
        block = re.search(r'(?ms)^if \[\[ "\$\{1:-\}" == --hook \]\]; then$(.*?)^fi$', text)
        self.assertIsNotNone(block, "codex-guard.sh has no --hook selection")
        patterns = re.findall(r"-k (\S+)", block[1])
        loader = unittest.TestLoader()
        classes = [item for item in globals().values() if isinstance(item, type) and issubclass(item, unittest.TestCase)]
        names = {cls.__name__: [f"test_codex_guard.{cls.__name__}.{name}" for name in loader.getTestCaseNames(cls)]
                 for cls in classes}
        every = [name for group in names.values() for name in group]
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                found = [name for name in every if pattern in name]
                self.assertTrue(found, "the hook names a test that does not exist")
                if pattern.startswith(".GuardedLaunch."):
                    self.assertEqual(len(found), 1, "a launch case pattern selects more than one case")
        chosen = lambda name: any(pattern in name for pattern in patterns)
        for cls, group in names.items():
            with self.subTest(cls=cls):
                if cls == "RealCodex":
                    self.assertFalse(any(map(chosen, group)), "the hook runs the real Codex")
                elif cls == "GuardedLaunch":
                    self.assertEqual(sorted(name.rpartition(".")[2] for name in group if chosen(name)),
                                     sorted(self.FORMS.values()), "one launch case per form")
                else:
                    self.assertTrue(all(map(chosen, group)), "an in-process case is left out of the hook")


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
# FAKE_PAUSE: [READY, GO]: says it runs, then waits for GO before it does anything else.
pause = json.loads(os.environ.get("FAKE_PAUSE", "null"))
if pause:
    pathlib.Path(pause[0]).touch()
    deadline = time.monotonic() + 30
    while not os.path.exists(pause[1]) and time.monotonic() < deadline:
        time.sleep(0.02)
# FAKE_NESTED: {"argv", "env"}: another program this session runs, such as a second codex-as.
nested = json.loads(os.environ.get("FAKE_NESTED", "null"))
if nested:
    env = {key: value for key, value in os.environ.items() if key not in ("FAKE_NESTED", "FAKE_PAUSE")}
    done = subprocess.run(nested["argv"], env={**env, **nested["env"]}, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=60)
    record["nested"] = [done.returncode, done.stdout, done.stderr]
home = pathlib.Path(os.environ["CODEX_HOME"])
# As native Codex writes them: the TUI's threads come from "cli", exec's and review's from "exec".
source = "exec" if {"exec", "e", "review"} & set(args) else "cli"
transcripts = []
for item in json.loads(os.environ.get("FAKE_ROLLOUTS", "[]")):
    now = time.time()
    # Milliseconds, truncated, as native Codex stamps its lines; "offset" seconds later for a writer whose clock runs ahead.
    then = now + item.get("offset", 0)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(then)) + ".%03dZ" % (int(then * 1000) % 1000)
    if "append" in item:  # a resumed thread: Codex appends to its rollout, wherever it lies
        path, lines = pathlib.Path(item["append"]), item["lines"]
    else:
        day = home / "sessions" / time.strftime("%Y/%m/%d", time.localtime(now))
        day.mkdir(parents=True, exist_ok=True)
        path = day / ("rollout-" + time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(now)) + "-" + item["thread"] + ".jsonl")
        meta = {"id": item["thread"], "session_id": item.get("session_id", item["thread"]), "timestamp": stamp,
                "cwd": item.get("cwd", os.getcwd()), "originator": "codex_exec" if source == "exec" else "codex-tui",
                "source": item.get("source", source), "thread_source": item.get("thread_source", "user"),
                "creator_user_id": item["creator"][0], "creator_account_id": item["creator"][1]}
        if "parent" in item:
            meta["parent_thread_id"] = item["parent"]
        if "forked_from" in item:  # a fork's session names the one it was forked from
            meta["forked_from_id"] = item["forked_from"]
        if "padding" in item:  # long base instructions: a session_meta line of more than one read
            meta["base_instructions"] = {"text": "x" * item["padding"]}
        lines = [{"type": "session_meta", "payload": meta}, *item["lines"]]
    with open(path, "a") as stream:
        for line in lines:
            stream.write(json.dumps({"timestamp": stamp, **line}) + "\n")
    if "mtime" in item:  # the file's time as a file server sets it whose clock is "mtime" seconds off
        os.utime(path, (now + item["mtime"], now + item["mtime"]))
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
# A block message as the guard leaves it, or something else in its place.
blocked = json.loads(os.environ.get("FAKE_BLOCKED", "null"))
if blocked is not None:
    if blocked["kind"] == "bytes":
        pathlib.Path(state + ".blocked").write_bytes(bytes.fromhex(blocked["hex"]))
    elif blocked["kind"] == "link":
        os.symlink(blocked["target"], state + ".blocked")
    elif blocked["kind"] == "dir":
        os.mkdir(state + ".blocked")
    else:
        os.mkfifo(state + ".blocked")
pathlib.Path(os.environ["FAKE_RECORD"]).write_text(json.dumps(record))
print("fake codex ran")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''
USER, WORKSPACE = "user-fixture-A", "workspace-fixture-A"
THREAD = "019a0000-0000-7000-8000-00000000000a"
OTHER = "019a0000-0000-7000-8000-0000000000bb"  # another session of the account
LAUNCH_LINE = "codex-as: usage reserves for A: {policy}; hooks refuse new prompts and tool calls at a reserve.\n"
WITHOUT = "; to launch without usage reserves, run codex-as --reserve-5h off --reserve-weekly off {account} with the same arguments.\n"
# After a run whose own sessions show that Codex did not run a hook, and after one that may have written a session
# showing it, which another client of the account may have written instead.
WARNING = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run {} (not trusted, disabled, "
           "or managed hooks only). Start codex-as A interactively and trust them in \"Hooks need review\".\n")
NOTICE = ("codex-as: A: the usage reserve may not have been enforced in this run: Codex did not run {} in a session of "
          "A that this run or another one wrote during it (not trusted, disabled, or managed hooks only). Start "
          "codex-as A interactively and trust them in \"Hooks need review\".\n")
# Rollout lines as native Codex writes them (the fake stamps them).
ANSWER = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}}
CALL = {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "{}",
                                             "call_id": "call-1"}}


def turn(cwd):
    """The turn_context line Codex starts a turn with: the session's working directory at that turn."""
    return {"type": "turn_context", "payload": {"cwd": str(cwd), "model": "fixture-model"}}


# A token_count line as Codex writes it when a reply carries no rate-limit headers.
NO_LIMITS = {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": {
    "limit_id": "codex", "limit_name": None, "primary": None, "secondary": None, "credits": None, "plan_type": None}}}


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
        # The kit copy's system and managed layers lie in the test's directory, so a host's /etc/codex
        # never changes a case, and /etc is never written.
        self.etc = self.base / "etc codex"
        for name, count in (("check.py", 3), ("layout.py", 4)):
            source = self.kit / "lib/codex-as" / name
            text = source.read_text()
            self.assertEqual(text.count('Path("/etc/codex/'), count, name)
            text = text.replace('Path("/etc/codex/', f"Path({str(self.etc)!r} + \"/")
            if name == "check.py":
                # Nor does a project layer above the test's directory (/tmp/.codex, say).
                self.assertEqual(text.count('\nPROJECT_TOP = Path("/")\n'), 1)
                text = text.replace('\nPROJECT_TOP = Path("/")\n', f"\nPROJECT_TOP = Path({str(self.base)!r})\n")
            source.write_text(text)
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

    def launches_plain(self, *argument_lists):
        """With a reserve configured, each launch is the one --reserve-5h off gives, without the guard."""
        self.configure("[usage_reserve]\nfive_hour = 10\n")
        for args in argument_lists:
            with self.subTest(args=args):
                result = self.run_codex("--reserve-5h", "off", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                plain = self.recorded()
                result = self.run_codex(*args, env=dict.fromkeys(VARIABLES, "inherited"))
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(self.recorded(), plain)
        self.assertFalse(self.guard_dir.exists())

    def test_a_plain_form_launches_as_without_reserves(self):
        self.launches_plain(["A", "mcp", "list"])

    def test_plain_forms_launch_as_without_reserves(self):
        self.launches_plain(["A", "features", "list"], ["A", "login", "status"], ["A", "--help"], ["A", "exec", "--help"],
                            ["A", "exec", "help"], ["A", "e", "help", "resume"], ["A", "completion", "bash"],
                            ["A", "git-credential", "get"])

    def refused(self, args, reason, account="A", env=None):
        result = self.run_codex("--reserve-weekly", "10", *args, env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"codex-as: {account}: {reason}" + WITHOUT.format(account=account)))
        self.assertFalse(self.record.exists(), "a refused launch started Codex")
        self.assertFalse((self.root / f"accounts/{account}").exists(), "a refused launch prepared the account")

    def test_forms_the_hooks_cannot_see_are_refused(self):
        self.trusted()
        for args, name in ((["cloud"], "cloud"), (["cloud-tasks"], "cloud"), (["cloud", "list"], "cloud"),
                           (["app-server"], "app-server"), (["app-server", "generate-ts"], "app-server"),
                           (["exec-server"], "exec-server"), (["remote-control"], "remote-control"),
                           (["debug", "app-server", "send-message-v2", "hi"], "debug app-server")):
            with self.subTest(args=args):
                # Refused as a whole (designer's call 4), also where a subcommand runs no model work itself.
                self.refused(["A", *args], f"codex {name} can run model work where the usage reserve guard's hooks "
                                           "cannot see or stop it")

    def test_options_the_hooks_cannot_cover_are_refused(self):
        self.trusted()
        # The session's cwd is a worktree under CODEX_HOME (measured with codex-cli 0.169.0): the check after
        # the run, which finds the run's new sessions by their directory, would see none of them.
        worktree = ("--worktree runs the session in a Git worktree that Codex manages elsewhere, where the check after "
                    "the run cannot find it to tell whether Codex ran the usage reserve guard's hooks")
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
                                                        "Codex's trust of the usage reserve guard's hooks"),
                             *((args, worktree) for args in (["--worktree"], ["exec", "--worktree", "go"],
                                                             ["--worktree", "exec", "go"], ["fork", UUID, "--worktree"],
                                                             ["resume", "--last", "--worktree"]))):
            with self.subTest(args=args):
                self.refused(["A", *args], reason)
        # Before a missing account is offered a login.
        self.refused(["missing", "exec", "--ephemeral", "go"], "--ephemeral keeps no session rollout, from which the "
                     "usage reserve guard reads the session's usage", account="missing")
        # Without a reserve, the same arguments reach Codex.
        for args in (["exec", "--ephemeral", "go"], ["exec", "--worktree", "go"]):
            with self.subTest(args=args, reserves="off"):
                result = self.run_codex("A", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.recorded()["args"], [*self.head(False), *args])

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
        # A system layer, from the test's directory in place of /etc/codex (setUp).
        self.etc.mkdir()
        for name in ("config.toml", "managed_config.toml"):
            with self.subTest(layer=name):
                system = self.etc / name
                system.write_text("[features]\nhooks = false\n")
                self.refused(["A", "exec", "go"], message.format(path=system))
                system.unlink()
        result = self.run_codex("--reserve-weekly", "10", "A", "-p", "q", "go")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_profile_codex_loads_from_the_account_home_is_read(self):
        """With no shared q.config.toml, -p q makes Codex load the account home's own file, which turns the
        hooks or a guard hook off as well (the real codex-cli 0.169.0 ran no hook with hooks = false there)."""
        self.trusted()
        result = self.run_codex("A", "mcp", "list")  # prepares the account home
        self.assertEqual(result.returncode, 0, result.stderr)
        own = self.root / "accounts/A/q.config.toml"
        for text, reason in (("[features]\nhooks = false\n",
                              "{path} turns the hooks feature off under [features], so Codex would not run the usage "
                              "reserve guard's hooks"),
                             (f"[hooks.state.{json.dumps(list(TRUST_RECORDS)[1])}]\nenabled = false\n",
                              "{path} turns a usage reserve guard hook off under [hooks.state], so Codex would not run it")):
            with self.subTest(text=text):
                own.write_text(text)
                result = self.run_codex("--reserve-5h", "10", "A", "-p", "q", "exec", "go")
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"codex-as: A: {reason.format(path=own)}" + WITHOUT.format(account="A")))
                self.assertFalse(self.record.exists(), "a refused launch started Codex")
        result = self.run_codex("--reserve-5h", "10", "A", "-p", "r", "exec", "go")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_login_without_a_chatgpt_user_and_workspace_is_refused(self):
        """An API-key login reports no usage windows, and its sessions name no creator, so neither the guard
        nor the check after the run could do its work."""
        self.trusted()
        credential = self.home / ".secrets/codex-A.json"
        reason = ("this login names no ChatGPT user and workspace, as an API key login does: it reports no usage "
                  "windows for the usage reserve guard, and its sessions cannot be told from other accounts'")
        logins = ({"auth_mode": "apikey", "OPENAI_API_KEY": "fabricated-sensitive-key", "tokens": None},
                  {**json.loads(credential.read_text()), "tokens": {
                      **json.loads(credential.read_text())["tokens"],
                      "id_token": id_token({"sub": "fixture-A", "https://api.openai.com/auth": {}})}})
        for login in logins:
            with self.subTest(login=login["auth_mode"]):
                credential.write_text(json.dumps(login))
                for args in (["A"], ["A", "exec", "go"]):
                    self.refused(args, reason)
        for login in logins:
            with self.subTest(login=login["auth_mode"], reserves="off"):
                credential.write_text(json.dumps(login))
                result = self.run_codex("A", "exec", "go")
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "fake codex ran\n", ""))

    def test_a_guard_hook_turned_off_under_hooks_state_is_refused(self):
        self.trusted()
        message = "{path} turns a usage reserve guard hook off under [hooks.state], so Codex would not run it"
        shared = self.root / "config.toml"
        for key in TRUST_RECORDS:
            with self.subTest(key=key):
                shared.write_text(f'model = "fixture-model"\n[hooks.state.{json.dumps(key)}]\n'
                                  f'trusted_hash = {json.dumps(TRUST_RECORDS[key])}\nenabled = false\n')
                self.refused(["A"], message.format(path=shared))
                self.refused(["A", "exec", "go"], message.format(path=shared))
        shared.write_text('model = "fixture-model"\n')
        profile = self.root / "q.config.toml"
        profile.write_text(f'[hooks.state.{json.dumps(list(TRUST_RECORDS)[1])}]\nenabled = false\n')
        self.refused(["A", "-p", "q", "exec", "go"], message.format(path=profile))
        for args in ((), ("-p", "r")):
            with self.subTest(args=args):
                result = self.run_codex("--reserve-weekly", "10", "A", *args, "exec", "go")
                self.assertEqual(result.returncode, 0, result.stderr)
        # Without a reserve, Codex's own settings are not read.
        result = self.run_codex("A", "-p", "q", "exec", "go")
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_goals_on_in_a_managed_layer_are_refused(self):
        """The kit copy's managed layers lie in the test's directory (setUp)."""
        managed, requirements = self.etc / "managed_config.toml", self.etc / "requirements.toml"
        self.etc.mkdir()
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

    def test_the_block_message_is_printed_plain_and_bounded(self):
        """What codex-as prints of .blocked is the guard's own text: control characters (C0 but tab and
        newline, DEL, C1) are left out, at most 16 KiB is read, and a link or a FIFO in its place is not
        read at all, nor waited on."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        elsewhere = self.base / "elsewhere"
        elsewhere.write_text("codex-as: A: text that is not the guard's\n")
        text = "codex-as: A: stopped\x1b[2J\x07\x00\r\x7f\u009b31m here\n\tand there\x1b]0;title\x07 \ufffd\n"
        cases = (({"kind": "bytes", "hex": text.encode("utf-8").hex()},
                  "codex-as: A: stopped[2J31m here\n\tand there]0;title \ufffd\n"),
                 ({"kind": "bytes", "hex": (b"\xff\xfe stopped \x9b").hex()}, "\ufffd\ufffd stopped \ufffd\n"),
                 ({"kind": "bytes", "hex": (b"y" * (20 * 1024)).hex()}, "y" * (16 * 1024) + "\n"),
                 ({"kind": "bytes", "hex": b" \n\x1b\x00 ".hex()}, ""),
                 ({"kind": "link", "target": str(elsewhere)}, ""), ({"kind": "fifo"}, ""))
        for blocked, printed in cases:
            with self.subTest(blocked=blocked["kind"], printed=printed[:40]):
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={"FAKE_BLOCKED": json.dumps(blocked)})
                self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
                self.assertEqual(result.stderr, launch_line + printed)
                self.assertEqual(self.launches(), [])
        # An interactive session shows its own blocks: nothing is printed after it.
        result = self.run_codex("--reserve-5h", "10", "A", env={"FAKE_BLOCKED": json.dumps(cases[0][0])})
        self.assertEqual(result.stderr, launch_line)
        # A directory in its place: nothing printed, Codex's exit status (the launch's state keeps only it).
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={"FAKE_BLOCKED": '{"kind": "dir"}',
                                                                            "FAKE_EXIT": "3"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "fake codex ran\n", launch_line))
        [left] = self.launches()
        self.assertTrue(left.endswith(".blocked"), left)
        (self.guard_dir / left).rmdir()

    def test_a_run_whose_hooks_did_not_run_is_reported_and_the_marker_goes(self):
        warning = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run {} (not trusted, "
                   "disabled, or managed hooks only). Start codex-as A interactively and trust them in "
                   "\"Hooks need review\".\n")
        cases = (  # rollout lines, hooks Codex ran, what the warning names
            ([ANSWER, snapshot(20.0)], [], "the guard's hooks"),
            ([CALL, snapshot(20.0)], [], "the guard's hooks"),
            ([CALL], [], "the guard's hooks"),  # a tool call shows a model request without rate limits too
            # A model response is a model request, whether or not its rate limits reached the rollout.
            ([ANSWER, NO_LIMITS], [], "the guard's hooks"),
            ([ANSWER], [], "the guard's hooks"),
            ([{"type": "response_item", "payload": {"type": "reasoning", "summary": []}}], [], "the guard's hooks"),
            ([NO_LIMITS], [], None),  # no response: nothing the model returned
            ([{**ANSWER, "metadata": {"inherited_user_message": True}}], [], None),  # a copy of another's
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

    def test_a_fork_s_new_session_is_the_run_s(self):
        """exec fork and the TUI's fork start a session of their own, from the forked one: it is the run's,
        so its tool call without the tool hook gives the warning, the marker goes and the next headless run
        is refused. The same holds for a session_meta line longer than one read (long base instructions)."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        refusal = ("codex-as: A: Codex has not run the usage reserve guard's tool hook on this host yet, and a headless "
                   "run skips hooks that are not trusted without a word: start codex-as A once interactively, trust the "
                   "hooks in \"Hooks need review\" and let it run one tool call" + WITHOUT.format(account="A"))
        cases = ((["exec", "fork", UUID, "go"], {}, [], "the guard's hooks"),
                 (["exec", "fork", UUID, "go"], {}, ["prompt"], "the guard's tool hook"),
                 (["fork", UUID], {}, [], "the guard's hooks"),
                 (["fork", UUID], {}, ["prompt"], "the guard's tool hook"),
                 (["exec", "go"], {"padding": 200 * 1024}, [], "the guard's hooks"),
                 ([], {"padding": 200 * 1024}, ["prompt"], "the guard's tool hook"))
        for args, extra, hooks, missing in cases:
            with self.subTest(args=args, extra=bool(extra), hooks=hooks):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [CALL, snapshot(20.0)], **extra}]
                result = self.run_codex("--reserve-5h", "10", "A", *args,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": json.dumps(hooks)})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + WARNING.format(missing)))
                self.assertFalse(self.marker.exists())
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
                self.assertEqual((result.returncode, result.stderr), (1, refusal))
        self.assertEqual(self.launches(), [])

    def test_a_tui_s_work_is_followed_into_the_sessions_it_moves_to(self):
        """A TUI moves its work to new sessions of its own elsewhere (measured with codex-cli 0.169.0): /cd forks
        the conversation into the new directory, naming the session it left (forked_from_id, also along a
        chain); /worktree starts one in a worktree under CODEX_HOME/worktrees; fork starts one in the forked
        session's directory by default. Each is the run's: what it shows gives the warning and removes the
        marker, and the next headless run is refused; with another client of the account at the run's start
        or end, the notice. A new session elsewhere that names none of the run's (after /cd and /new, or
        another client's) gets the notice."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        refusal = ("codex-as: A: Codex has not run the usage reserve guard's tool hook on this host yet, and a headless "
                   "run skips hooks that are not trusted without a word: start codex-as A once interactively, trust the "
                   "hooks in \"Hooks need review\" and let it run one tool call" + WITHOUT.format(account="A"))
        here, chat, work = str(self.cwd), [ANSWER, snapshot(20.0)], [CALL, snapshot(20.0)]
        elsewhere, further = str(self.base / "elsewhere"), str(self.base / "further")
        worktree = str(self.root / "accounts/A/worktrees/738e/work dir")
        first, second = "019a0000-0000-7000-8000-0000000000c1", "019a0000-0000-7000-8000-0000000000c2"
        session = lambda thread, cwd, lines, **extra: {"thread": thread, "creator": [USER, WORKSPACE], "cwd": cwd,
                                                       "lines": lines, **extra}
        cases = (  # the arguments, the sessions the run writes, the hooks it ran, the message, what it names
            # /cd: the conversation forked into the new directory, and again from there.
            ([], [session(THREAD, here, chat), session(first, elsewhere, work, forked_from=THREAD)], ["prompt"],
             WARNING, "the guard's tool hook"),
            ([], [session(THREAD, here, chat), session(first, elsewhere, chat, forked_from=THREAD),
                  session(second, further, work, forked_from=first)], ["prompt"], WARNING, "the guard's tool hook"),
            # /cd, then /new: a session elsewhere that names none of the run's.
            ([], [session(THREAD, here, chat), session(first, elsewhere, work)], ["prompt"], NOTICE,
             "the guard's tool hook"),
            ([], [session(first, elsewhere, [ANSWER, *work])], [], NOTICE, "the guard's hooks"),
            # /worktree: the session lies in a worktree that Codex manages.
            ([], [session(THREAD, worktree, work)], [], WARNING, "the guard's hooks"),
            # fork UUID, and the picker: the session the fork starts in the forked one's directory.
            (["fork", UUID], [session(THREAD, elsewhere, work, forked_from=UUID)], [], WARNING, "the guard's hooks"),
            (["fork"], [session(THREAD, elsewhere, work, forked_from=OTHER)], [], WARNING, "the guard's hooks"),
            (["fork", UUID], [session(THREAD, elsewhere, work, forked_from=OTHER)], [], NOTICE, "the guard's hooks"),
        )
        for args, rollouts, hooks, message, missing in cases:
            with self.subTest(args=args, sessions=len(rollouts), message=message[:30], missing=missing):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                result = self.run_codex("--reserve-5h", "10", "A", *args,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": json.dumps(hooks)})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + message.format(missing)))
                self.assertEqual(self.marker.exists(), message == NOTICE)
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
                self.assertEqual(result.returncode, 0 if message == NOTICE else 1, result.stderr)
                if message == WARNING:
                    self.assertEqual(result.stderr, refusal)
        self.hold_the_account()
        for rollouts in ([session(THREAD, here, chat), session(first, elsewhere, work, forked_from=THREAD)],
                         [session(THREAD, worktree, work)]):
            with self.subTest(held=True, sessions=len(rollouts)):
                for child in (self.root / "sessions").iterdir():
                    shutil.rmtree(child)
                self.trusted()
                result = self.run_codex("--reserve-5h", "10", "A",
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + NOTICE.format("the guard's tool hook")))
                self.assertTrue(self.marker.exists())
        self.assertEqual(self.launches(), [])

    def test_a_sub_agent_named_by_its_parent_only_is_the_run_s(self):
        """A sub-agent whose session_id names no session of the run still belongs to it by its
        parent_thread_id; one that names neither is another session's."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        spawned = {"subagent": {"thread_spawn": {"parent_thread_id": THREAD, "depth": 1}}}
        main = {"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [ANSWER, snapshot(20.0)]}
        sub = {"thread": OTHER, "creator": [USER, WORKSPACE], "source": spawned, "thread_source": "subagent",
               "lines": [CALL, snapshot(20.0)], "session_id": "019a0000-0000-7000-8000-0000000000cc"}
        for parent, printed in ((THREAD, WARNING.format("the guard's tool hook")), (UUID, "")):
            with self.subTest(parent=parent):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                rollouts = [main, {**sub, "parent": parent}]
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go",
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + printed))
                self.assertEqual(self.marker.exists(), not printed)

    def old_rollout(self, thread, seconds, source="cli", cwd=None):
        """A rollout of this account's THREAD that started SECONDS ago (in CWD, by default the work
        directory), as Codex left it then."""
        then = time.time() - seconds
        day = self.root / "sessions" / time.strftime("%Y/%m/%d", time.localtime(then))
        day.mkdir(parents=True, exist_ok=True)
        path = day / ("rollout-" + time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(then)) + f"-{thread}.jsonl")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(then))
        meta = {"id": thread, "session_id": thread, "timestamp": stamp, "cwd": str(cwd or self.cwd), "originator": "codex-tui",
                "source": source, "thread_source": "user", "creator_user_id": USER, "creator_account_id": WORKSPACE}
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
        rollout lies in the date directory of the day it started; exec resume begins its turn here."""
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
                    rollouts = [{"append": str(path), "lines": [turn(self.cwd), CALL, snapshot(20.0)]}]
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
        other = self.old_rollout(OTHER, 600)
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

    def test_another_launch_of_the_account_leaves_an_older_session_of_a_session_unsure(self):
        """While another codex-as client of the account runs at the run's start or end, an older session
        written during a TUI, resume --last or picker run may be that client's: what it shows gets the
        notice, never the warning, and the marker stays. One that shows nothing the hooks missed gets
        nothing."""
        self.hold_the_account()
        other = self.old_rollout(OTHER, 600)
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        for form in ([], ["resume", "--last"], ["resume"]):
            for lines, hooks, printed in (([CALL, snapshot(20.0)], [], NOTICE.format("the guard's hooks")),
                                          ([CALL, snapshot(20.0)], ["prompt"], NOTICE.format("the guard's tool hook")),
                                          ([ANSWER, snapshot(20.0)], ["prompt"], "")):
                with self.subTest(form=form, hooks=hooks, lines=len(lines)):
                    self.trusted()
                    rollouts = [{"append": str(other), "lines": lines}]
                    result = self.run_codex("--reserve-5h", "10", "A", *form,
                                            env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": json.dumps(hooks)})
                    self.assertEqual((result.returncode, result.stderr), (0, launch_line + printed))
                    self.assertTrue(self.marker.exists())
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
        self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(self.launches(), [])

    def test_another_tui_of_the_account_here_leaves_a_tui_s_new_sessions_unsure(self):
        """While another codex-as client of the account runs at the run's start or end, a session started
        in the directory during a TUI run may be a second TUI's: a chat-only TUI next to one that makes
        tool calls gets the notice, not the warning, and the marker stays, so the next headless run is not
        refused. Without the other client the TUI's own sessions give the warning (the next case)."""
        self.hold_the_account()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        cases = (  # this TUI's session, the second TUI's, the hooks this TUI ran, what is printed
            ([ANSWER, snapshot(20.0)], [CALL, snapshot(20.0)], ["prompt"], NOTICE.format("the guard's tool hook")),
            ([CALL, snapshot(20.0)], [CALL, snapshot(20.0)], [], NOTICE.format("the guard's hooks")),
            ([ANSWER, snapshot(20.0)], [ANSWER, snapshot(20.0)], ["prompt"], ""),
        )
        for own, other, hooks, printed in cases:
            with self.subTest(hooks=hooks, printed=printed[:60]):
                for child in (self.root / "sessions").iterdir():  # the held account's home links this directory
                    shutil.rmtree(child)
                self.trusted()
                rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": own},
                            {"thread": OTHER, "creator": [USER, WORKSPACE], "lines": other}]
                result = self.run_codex("--reserve-5h", "10", "A",
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": json.dumps(hooks)})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + printed))
                self.assertTrue(self.marker.exists())
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
                self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(self.launches(), [])

    def test_another_client_at_the_run_s_start_or_end_alone_leaves_older_sessions_unsure(self):
        """The lease is checked at the run's start and again at its end: a client that held it only when the
        run started, or took it only after, leaves an older session written during the TUI unsure (the
        notice, the marker kept); alone, it is the TUI's (the warning, the marker removed)."""
        result = self.run_codex("A", "mcp", "list")  # prepares the account home and its lease
        self.assertEqual(result.returncode, 0, result.stderr)
        lease = self.root / "accounts/.locks/A.lock"
        other = self.old_rollout(OTHER, 600)
        ready, go = self.base / "ready", self.base / "go"
        rollouts = json.dumps([{"append": str(other), "lines": [CALL, snapshot(20.0)]}])
        for moment in ("start", "end", None):
            with self.subTest(moment=moment):
                self.trusted()
                ready.unlink(missing_ok=True)
                go.unlink(missing_ok=True)
                fd = os.open(lease, os.O_RDWR)
                try:
                    if moment == "start":
                        fcntl.flock(fd, fcntl.LOCK_SH)
                    child = subprocess.Popen([str(self.kit / "bin/codex-as"), "--reserve-5h", "10", "A"], cwd=self.cwd,
                                             env=self.env | {"FAKE_PAUSE": json.dumps([str(ready), str(go)]),
                                                             "FAKE_ROLLOUTS": rollouts, "FAKE_HOOKS": "[]"},
                                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             text=True, start_new_session=True)
                    try:
                        deadline = time.monotonic() + 30
                        while not ready.exists() and time.monotonic() < deadline and child.poll() is None:
                            time.sleep(0.02)
                        self.assertTrue(ready.exists(), "Codex did not start")
                        fcntl.flock(fd, fcntl.LOCK_UN if moment == "start" else fcntl.LOCK_SH if moment else fcntl.LOCK_UN)
                        go.touch()
                        _, err = child.communicate(timeout=60)
                    finally:
                        kill_group(child.pid)
                finally:
                    os.close(fd)
                self.assertEqual(child.returncode, 0, err)
                launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
                printed = WARNING if moment is None else NOTICE
                self.assertEqual(err, launch_line + printed.format("the guard's hooks"))
                self.assertEqual(self.marker.exists(), moment is not None)
        self.assertEqual(self.launches(), [])

    def test_the_record_takes_the_snapshot_of_a_resumed_older_session(self):
        """The account record of the guard's fallback reading gets the run's snapshot also from a session
        it resumed by --last whose rollout lies in an older date directory."""
        self.trusted()
        older = self.old_rollout(THREAD, 2 * 86400)
        rollouts = [{"append": str(older), "lines": [ANSWER, snapshot(30.0)]}]
        result = self.run_codex("--reserve-5h", "10", "A", "resume", "--last",
                                env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
        self.assertEqual(result.returncode, 0, result.stderr)
        usage = json.loads((self.state / "accounts/codex-A.json").read_text())["usage"]
        self.assertEqual((usage["primary"]["used_percent"], usage["primary"]["window_minutes"]), (30.0, 300))

    def test_another_launch_of_the_account_never_hides_what_a_headless_run_wrote(self):
        """A headless run's own sessions are told by what wrote them (exec, this directory, a start during
        the run), so another codex-as client of the account running meanwhile keeps neither the warning
        nor the marker's removal away: after trust is lost, the next headless run is refused."""
        self.hold_the_account()
        warning = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run the guard's hooks "
                   "(not trusted, disabled, or managed hooks only). Start codex-as A interactively and trust them in "
                   "\"Hooks need review\".\n")
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        refusal = ("codex-as: A: Codex has not run the usage reserve guard's tool hook on this host yet, and a headless "
                   "run skips hooks that are not trusted without a word: start codex-as A once interactively, trust the "
                   "hooks in \"Hooks need review\" and let it run one tool call" + WITHOUT.format(account="A"))
        older = self.old_rollout(THREAD, 2 * 86400)
        runs = ((["exec", "resume", "--last", "go"], [{"append": str(older), "lines": [turn(self.cwd), CALL,
                                                                                       snapshot(20.0)]}]),
                (["exec", "go"], [{"thread": OTHER, "creator": [USER, WORKSPACE], "lines": [CALL, snapshot(95.0)]}]),
                (["review"], [{"thread": UUID, "creator": [USER, WORKSPACE], "lines": [ANSWER, snapshot(20.0)]}]))
        for args, rollouts in runs:
            with self.subTest(args=args):
                self.trusted()
                result = self.run_codex("--reserve-5h", "10", "A", *args,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": "[]"})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + warning))
                self.assertFalse(self.marker.exists(), "another launch of the account ran, and the marker stayed")
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
                self.assertEqual((result.returncode, result.stderr), (1, refusal))
        self.assertEqual(self.launches(), [])

    def test_an_earlier_run_s_last_lines_are_not_this_run_s(self):
        """Only lines written after the run started count, to the millisecond: an exec that ends with a tool
        call, followed at once by exec resume --last and by another exec whose hooks ran, does not make
        those runs look unguarded."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        first = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [ANSWER, CALL, snapshot(20.0)]}]
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "first",
                                env={"FAKE_ROLLOUTS": json.dumps(first), "FAKE_HOOKS": '["prompt", "tool"]'})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        [path] = (self.root / "sessions").glob("*/*/*/rollout-*.jsonl")
        for args, rollouts in ((["exec", "resume", "--last", "next"],
                                [{"append": str(path), "lines": [turn(self.cwd), ANSWER, snapshot(21.0)]}]),
                               (["exec", "next"], [{"thread": OTHER, "creator": [USER, WORKSPACE], "lines": [ANSWER, snapshot(21.0)]}]),
                               ([], [{"thread": UUID, "creator": [USER, WORKSPACE], "lines": [ANSWER, snapshot(21.0)]}])):
            with self.subTest(args=args):
                result = self.run_codex("--reserve-5h", "10", "A", *args,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line))
                self.assertTrue(self.marker.exists())
        self.assertEqual(self.launches(), [])

    def test_a_concurrent_launch_of_the_account_is_not_this_run_s(self):
        """A session another program started during the run is this run's only when this run's kind of
        Codex wrote it here: the TUI leaves out an exec's, an exec a TUI's and one of another directory,
        while a TUI session of another directory may be the TUI's own (after /cd and /new) and gets the
        notice. Of several exec sessions started here during a headless run, one is the run's: what
        every one of them shows gives the warning, and what only some show the notice, with the marker
        kept, since that session may be another run's."""
        tool_hook = WARNING.format("the guard's tool hook")
        unsure = NOTICE.format("the guard's tool hook")
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        own, others = [ANSWER, snapshot(20.0)], [CALL, snapshot(20.0)]
        elsewhere = str(self.base)
        cases = (  # the run, its own session's lines, the other sessions, what is printed
            (["exec", "go"], own, [{"lines": others}], unsure),
            (["review"], own, [{"lines": others}], unsure),
            (["exec", "go"], own, [{"lines": others, "source": "cli"}], ""),
            (["exec", "go"], own, [{"lines": others, "cwd": elsewhere}], ""),
            ([], own, [{"lines": others, "source": "exec"}], ""),
            # A TUI session elsewhere may be this TUI's after /cd and /new, or another client's.
            ([], own, [{"lines": others, "cwd": elsewhere}], unsure),
            (["exec", "go"], others, [{"lines": others}, {"lines": others}], tool_hook),
            (["exec", "go"], others, [{"lines": own}], unsure),
            (["exec", "go"], others, [{"lines": own, "source": "cli"}], tool_hook),
            (["exec", "go"], others, [{"lines": own, "cwd": elsewhere}], tool_hook),
            (["exec", "go"], own, [{"lines": own}], ""),
            # Alone, a TUI takes a session started here during its run for its own; a second codex-as launch
            # arriving makes it not alone (test_a_launch_of_the_account_that_arrives_during_a_run_leaves_it_unsure).
            ([], own, [{"lines": others}], tool_hook),
        )
        for args, lines, concurrent, printed in cases:
            warned = printed == tool_hook
            with self.subTest(args=args, concurrent=concurrent, printed=printed[:60]):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": lines}]
                for number, item in enumerate(concurrent):
                    rollouts.append({"thread": f"019a0000-0000-7000-8000-{number + 1:012x}", "creator": [USER, WORKSPACE],
                                     **item})
                result = self.run_codex("--reserve-5h", "10", "A", *args,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + printed))
                self.assertEqual(self.marker.exists(), not warned)
        # A session another exec started a moment before this run, and wrote to during it, is not one this run started.
        shutil.rmtree(self.root / "sessions")
        self.trusted()
        just = self.old_rollout(OTHER, 2, source="exec")
        rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": others}, {"append": str(just), "lines": own}]
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go",
                                env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line + tool_hook))
        self.assertFalse(self.marker.exists())
        # exec resume by id starts no session of its own: an exec session started here meanwhile is another run's.
        shutil.rmtree(self.root / "sessions")
        self.trusted()
        older = self.old_rollout(THREAD, 600)
        rollouts = [{"append": str(older), "lines": own}, {"thread": OTHER, "creator": [USER, WORKSPACE], "lines": others}]
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "resume", THREAD, "go",
                                env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.launches(), [])

    def test_a_run_next_to_another_of_the_account_is_never_silent(self):
        """A run whose tool hook did not run, beside another session of the account that may be its own
        and made no tool call, gets the notice on every run, not silence: an exec next to another exec in
        its directory, and exec resume --last next to an older session of the account, begun elsewhere,
        in which another client took a turn here. The marker stays, since either session may be another
        run's whose hooks ran."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        unsure = NOTICE.format("the guard's tool hook")
        for number in range(3):
            with self.subTest(run=number, args="exec"):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [CALL, snapshot(20.0)]},
                            {"thread": OTHER, "creator": [USER, WORKSPACE], "lines": [ANSWER, snapshot(20.0)]}]
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go",
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + unsure))
                self.assertTrue(self.marker.exists())
        shutil.rmtree(self.root / "sessions")
        older = self.old_rollout(THREAD, 2 * 86400)
        elsewhere = self.old_rollout(OTHER, 86400)
        meta = json.loads(elsewhere.read_text().splitlines()[0])
        meta["payload"]["cwd"] = str(self.base)
        elsewhere.write_text(json.dumps(meta) + "\n" + "".join(elsewhere.read_text().splitlines(True)[1:]))
        for number in range(3):
            with self.subTest(run=number, args="exec resume --last"):
                self.trusted()
                rollouts = [{"append": str(older), "lines": [turn(self.cwd), CALL, snapshot(20.0)]},
                            {"append": str(elsewhere), "lines": [turn(self.cwd), ANSWER, snapshot(20.0)]}]
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "resume", "--last", "go",
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + unsure))
                self.assertTrue(self.marker.exists())
        # Without the other session's turn here the run's own show it: the warning, and the marker goes.
        for other in ([], [{"append": str(elsewhere), "lines": [turn(self.base), ANSWER, snapshot(20.0)]}]):
            with self.subTest(other=bool(other)):
                self.trusted()
                rollouts = [{"append": str(older), "lines": [turn(self.cwd), CALL, snapshot(20.0)]}, *other]
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "resume", "--last", "go",
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr),
                                 (0, launch_line + WARNING.format("the guard's tool hook")))
                self.assertFalse(self.marker.exists())
        self.assertEqual(self.launches(), [])

    def test_a_launch_of_the_account_that_arrives_during_a_run_leaves_it_unsure(self):
        """A second codex-as launch of the account that starts and ends inside a TUI's run (a second terminal
        tab, here or continuing an older session elsewhere) holds the lease only meanwhile, where neither of
        the run's lease checks sees it; its arrival in the lease file makes the run not alone. What its
        sessions show gets the notice, never the warning: the marker stays and the next headless run runs."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        second = [str(self.kit / "bin/codex-as"), "--reserve-5h", "10", "A"]
        inner = self.base / "inner.json"
        tab = [{"thread": UUID, "creator": [USER, WORKSPACE], "lines": [ANSWER, CALL, snapshot(20.0)]}]
        cases = (  # this TUI's own lines, the hooks it ran, the second launch's arguments, what it names
            ([], [], [], "the guard's hooks"),
            ([ANSWER, snapshot(20.0)], ["prompt"], [], "the guard's tool hook"),
            ([], [], ["-C", str(elsewhere), "exec", "resume", "--last", "go"], "the guard's hooks"),
        )
        for own, hooks, args, missing in cases:
            with self.subTest(own=len(own), args=args):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                older = self.old_rollout(OTHER, 86400, cwd=elsewhere)
                rollouts = ([{"append": str(older), "lines": [turn(elsewhere), ANSWER, CALL, snapshot(20.0)]}]
                            if args else tab)
                nested = {"argv": [*second, *args], "env": {"FAKE_ROLLOUTS": json.dumps(rollouts),
                                                            "FAKE_HOOKS": '["prompt", "tool"]', "FAKE_RECORD": str(inner)}}
                own_rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": own}] if own else []
                result = self.run_codex("--reserve-5h", "10", "A", env={
                    "FAKE_NESTED": json.dumps(nested), "FAKE_ROLLOUTS": json.dumps(own_rollouts),
                    "FAKE_HOOKS": json.dumps(hooks)})
                self.assertEqual(self.recorded()["nested"], [0, "fake codex ran\n", launch_line])
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + NOTICE.format(missing)))
                self.assertTrue(self.marker.exists())
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
                self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(self.launches(), [])

    def test_exec_resume_without_an_id_is_told_by_the_turn_it_begins_here(self):
        """exec resume --last continues the newest session whose latest turn was in its directory, and begins
        its own turn there, or starts a session there when it finds none (measured with codex-cli 0.169.0,
        RealCodex): a new session it starts is the run's (the warning, the marker removed, the next headless
        run refused), an older session counts only with a turn begun here during the run, and an older
        session of the account that another client continued elsewhere meanwhile is not the run's."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        refusal = ("codex-as: A: Codex has not run the usage reserve guard's tool hook on this host yet, and a headless "
                   "run skips hooks that are not trusted without a word: start codex-as A once interactively, trust the "
                   "hooks in \"Hooks need review\" and let it run one tool call" + WITHOUT.format(account="A"))
        for number, hooks in enumerate(([], ["prompt"], [])):
            fresh = self.base / f"fresh {number}"
            fresh.mkdir()
            with self.subTest(fresh=fresh.name, hooks=hooks):
                self.trusted()
                rollouts = [{"thread": f"019a0000-0000-7000-8000-{number + 1:012x}", "creator": [USER, WORKSPACE],
                             "cwd": str(fresh), "lines": [turn(fresh), ANSWER, CALL, snapshot(20.0)]}]
                result = self.run_codex("--reserve-5h", "10", "A", "-C", str(fresh), "exec", "resume", "--last", "go",
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": json.dumps(hooks)})
                missing = "the guard's tool hook" if hooks else "the guard's hooks"
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + WARNING.format(missing)))
                self.assertFalse(self.marker.exists())
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
                self.assertEqual((result.returncode, result.stderr), (1, refusal))
        shutil.rmtree(self.root / "sessions")
        elsewhere = self.base / "elsewhere"
        older, other = self.old_rollout(THREAD, 86400), self.old_rollout(OTHER, 2 * 86400, cwd=elsewhere)
        cases = (  # what this run writes, what another client writes meanwhile, what is printed
            # A session that started elsewhere, continued here: the run's.
            ([{"append": str(other), "lines": [turn(self.cwd), CALL, snapshot(20.0)]}], [], WARNING),
            # The run's new session here, chat only, beside an older session continued elsewhere.
            ([{"thread": UUID, "creator": [USER, WORKSPACE], "lines": [turn(self.cwd), ANSWER, snapshot(20.0)]}],
             [{"append": str(other), "lines": [turn(elsewhere), CALL, snapshot(20.0)]}], ""),
            # The run's older session, chat only, beside the same.
            ([{"append": str(older), "lines": [turn(self.cwd), ANSWER, snapshot(20.0)]}],
             [{"append": str(other), "lines": [turn(elsewhere), CALL, snapshot(20.0)]}], ""),
            # Lines without a turn begun here are not a turn of this run.
            ([], [{"append": str(other), "lines": [CALL, snapshot(20.0)]}], ""),
        )
        for own, others, printed in cases:
            with self.subTest(own=len(own), others=len(others), printed=printed[:20]):
                self.trusted()
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "resume", "--last", "go",
                                        env={"FAKE_ROLLOUTS": json.dumps([*own, *others]), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr),
                                 (0, launch_line + printed.format("the guard's tool hook")))
                self.assertEqual(self.marker.exists(), not printed)
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
        self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(self.launches(), [])

    def test_an_exec_of_the_account_inside_a_session_is_not_the_session_s(self):
        """A codex-as exec of the same account that a guarded TUI runs (from its shell, say) starts and ends
        inside the TUI's run, where neither lease check sees it."""
        self.trusted()
        inner = self.base / "inner.json"
        nested = {"argv": [str(self.kit / "bin/codex-as"), "--reserve-5h", "10", "A", "exec", "go"],
                  "env": {"FAKE_ROLLOUTS": json.dumps([{"thread": OTHER, "creator": [USER, WORKSPACE],
                                                        "lines": [CALL, snapshot(20.0)]}]),
                          "FAKE_HOOKS": '["prompt", "tool"]', "FAKE_RECORD": str(inner)}}
        result = self.run_codex("--reserve-5h", "10", "A", env={"FAKE_NESTED": json.dumps(nested), "FAKE_HOOKS": '["prompt"]'})
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        self.assertEqual(self.recorded()["nested"], [0, "fake codex ran\n", launch_line])
        self.assertEqual([hook[:2] for hook in json.loads(inner.read_text())["hooks"]], [["prompt", 0], ["tool", 0]])
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.launches(), [])

    def test_the_run_s_sub_agents_count_and_its_reviews_do_not(self):
        """A sub-agent's session names the run's session; a guardian review runs no user hook. A caller's
        exec --thread-source guardian_review tags the run's own session, which still counts."""
        tool_hook = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run the guard's tool hook "
                     "(not trusted, disabled, or managed hooks only). Start codex-as A interactively and trust them in "
                     "\"Hooks need review\".\n")
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        main = {"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [ANSWER, snapshot(20.0)]}
        sub = {"thread": OTHER, "creator": [USER, WORKSPACE], "session_id": THREAD, "parent": THREAD,
               "lines": [CALL, snapshot(20.0)]}
        spawned = {"subagent": {"thread_spawn": {"parent_thread_id": THREAD, "depth": 1}}}
        cases = (
            (["exec", "go"], [main, {**sub, "source": spawned, "thread_source": "subagent", "cwd": str(self.base)}], True),
            ([], [main, {**sub, "source": spawned, "thread_source": "subagent"}], True),
            (["exec", "go"], [main, {**sub, "source": {"subagent": "other"}, "thread_source": "guardian_review"}], False),
            (["exec", "--thread-source", "guardian_review", "go"],
             [{**main, "thread_source": "guardian_review", "lines": [CALL, snapshot(20.0)]}], True),
        )
        for args, rollouts, warned in cases:
            with self.subTest(args=args, warned=warned):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                result = self.run_codex("--reserve-5h", "10", "A", *args,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + (tool_hook if warned else "")))
                self.assertEqual(self.marker.exists(), not warned)

    def test_only_the_run_s_own_calls_that_a_tool_hook_sees_count(self):
        """Calls no tool hook sees (write_stdin, web search), call outputs, a sub-agent's inherited copy of its
        parent's call, and a line stamped after the run ended are not tool calls of the run."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        unhooked = [{"type": "response_item", "payload": {"type": "function_call", "name": "write_stdin",
                                                          "arguments": "{}", "call_id": "call-2"}},
                    {"type": "response_item", "payload": {"type": "web_search_call", "status": "completed"}},
                    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1",
                                                          "output": "done"}}]
        inherited = {**CALL, "metadata": {"inherited_user_message": True}}
        spawned = {"subagent": {"thread_spawn": {"parent_thread_id": THREAD, "depth": 1}}}
        older = self.old_rollout(OTHER, 2 * 86400)
        cases = (
            (["exec", "go"], [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [ANSWER, *unhooked, snapshot(20.0)]}]),
            (["exec", "go"], [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [ANSWER, snapshot(20.0)]},
                              {"thread": UUID, "creator": [USER, WORKSPACE], "session_id": THREAD, "source": spawned,
                               "thread_source": "subagent", "lines": [inherited, ANSWER, snapshot(20.0)]}]),
            (["resume", "--last"], [{"append": str(older), "lines": [CALL], "offset": 3600},
                                    {"append": str(older), "lines": [ANSWER, snapshot(20.0)]}]),
        )
        for args, rollouts in cases:
            with self.subTest(args=args, lines=len(rollouts)):
                self.trusted()
                result = self.run_codex("--reserve-5h", "10", "A", *args,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line))
                self.assertTrue(self.marker.exists())

    def test_a_file_server_s_clock_never_hides_the_run_s_rollouts(self):
        """On a network home directory a rollout's file time comes from the file server's clock: one that
        lags this host's by 30 seconds still has its lines, stamped by Codex on this host, read."""
        warning = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run the guard's hooks "
                   "(not trusted, disabled, or managed hooks only). Start codex-as A interactively and trust them in "
                   "\"Hooks need review\".\n")
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        for args in (["exec", "go"], [], ["resume", "--last"]):
            with self.subTest(args=args):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                if args[:1] == ["resume"]:
                    rollouts = [{"append": str(self.old_rollout(THREAD, 2 * 86400)), "lines": [CALL, snapshot(20.0)],
                                 "mtime": -30}]
                else:
                    rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [CALL, snapshot(20.0)],
                                 "mtime": -30}]
                result = self.run_codex("--reserve-5h", "10", "A", *args,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": "[]"})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + warning))
                self.assertFalse(self.marker.exists())

    def test_other_accounts_newer_sessions_never_crowd_out_the_run_s(self):
        """However many sessions other accounts write after this run's own (one guardian review per
        reviewed tool call, say), the run's own are read."""
        self.trusted()
        warning = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run the guard's hooks "
                   "(not trusted, disabled, or managed hooks only). Start codex-as A interactively and trust them in "
                   "\"Hooks need review\".\n")
        rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [CALL, snapshot(20.0)]}]
        rollouts += [{"thread": f"019a0001-0000-7000-8000-{number:012x}", "creator": ["user-fixture-B", "workspace-fixture-B"],
                      "lines": [ANSWER, snapshot(20.0)]} for number in range(80)]
        result = self.run_codex("--reserve-5h", "10", "A", env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": "[]"})
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        self.assertEqual((result.returncode, result.stderr), (0, launch_line + warning))
        self.assertFalse(self.marker.exists())

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

    def test_a_stderr_gone_after_the_launch_line_loses_only_the_text(self):
        """A stderr whose reader closed while Codex ran (a pipe, or a terminal after a hang-up): the run that
        shows Codex skipped the guard's hooks still removes the marker and the launch's state, and codex-as
        exits with Codex's status, the block message and the warning lost."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        ready, go = self.base / "ready", self.base / "go"
        rollouts = json.dumps([{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [CALL, snapshot(20.0)]}])
        for form in (["exec", "go"], []):
            with self.subTest(form=form):
                self.trusted()
                ready.unlink(missing_ok=True)
                go.unlink(missing_ok=True)
                blocked = {"kind": "bytes", "hex": b"codex-as: A: stopped\n".hex()}
                child = subprocess.Popen([str(self.kit / "bin/codex-as"), "--reserve-5h", "10", "A", *form], cwd=self.cwd,
                                         env=self.env | {"FAKE_PAUSE": json.dumps([str(ready), str(go)]),
                                                         "FAKE_ROLLOUTS": rollouts, "FAKE_HOOKS": "[]", "FAKE_EXIT": "3",
                                                         "FAKE_BLOCKED": json.dumps(blocked)},
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         text=True, start_new_session=True)
                try:
                    self.assertEqual(child.stderr.readline(), launch_line)
                    deadline = time.monotonic() + 30
                    while not ready.exists() and time.monotonic() < deadline and child.poll() is None:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), "Codex did not start")
                    child.stderr.close()
                    go.touch()
                    self.assertEqual(child.wait(timeout=60), 3, "the exit status is Codex's")
                finally:
                    kill_group(child.pid)
                self.assertFalse(self.marker.exists(), "the marker stayed after a run whose hooks did not run")
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


# The git the shim passes local commands to: a system one, never a wrapper the caller's PATH finds first.
SYSTEM_GIT = ("/usr/bin/git", "/usr/local/bin/git")


def interpreter(path):
    """The interpreter a script's #! line names through env (node for an npm install of codex), or None."""
    try:
        with open(path, "rb") as stream:
            line = stream.readline(256)
    except OSError:
        return None
    words = line[2:].decode("utf-8", "replace").split() if line.startswith(b"#!") else []
    return words[1] if len(words) == 2 and os.path.basename(words[0]) == "env" else None


class Sandbox:
    """A Codex that reaches nothing of this host's user: a temporary HOME and CODEX_HOME, an environment
    built from scratch, git and ssh shims first on PATH, every proxy at a closed loopback port (Codex
    runs git ls-remote for its plugin marketplace at every start) and no session bus. PATH holds the
    system directories and a directory of the sandbox's own with a link to CODEX (and to the
    interpreter its #! line names, for a codex that is a node script), never the directory codex was
    found in. The model provider is the loopback fake; nothing holds a credential.

    The one exception is the codex install itself, which runs and reads its own files from where it
    lies, under the real home for a standalone install (~/.codex/packages/standalone/releases/VERSION):
    the binary, codex-package.json, codex-resources, and the codex-path directory that Codex puts on
    its children's PATH. They are read, never written; no configuration, login, session or trust
    record of the user is touched."""

    def __init__(self, base, codex):
        self.base = base
        self.home, self.work, self.shims, self.bin = base / "home", base / "work", base / "shims", base / "bin"
        self.codex_home = self.home / ".codex"
        for path in (self.codex_home, self.work, self.shims, self.bin, base / "tmp", base / "run",
                     *(base / "xdg" / name for name in ("config", "cache", "data", "state"))):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        (self.bin / "codex").symlink_to(codex)
        name = interpreter(codex)
        found = shutil.which(name) if name else None
        if found:
            (self.bin / name).symlink_to(os.path.realpath(found))
        self.netlog = base / "net.log"
        self.netlog.touch()
        log = shlex.quote(str(self.netlog))
        real = next((path for path in SYSTEM_GIT if os.access(path, os.X_OK)), None)
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
        return {"PATH": f"{self.shims}:/usr/local/bin:/usr/bin:/bin:{self.bin}", "HOME": str(self.home),
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


class SandboxPath(unittest.TestCase):
    """What the real Codex's sandbox puts on PATH, with fakes for codex and node."""

    def test_only_codex_and_its_interpreter_join_the_system_directories(self):
        with tempfile.TemporaryDirectory(prefix="codex-guard-sandbox-") as temp:
            base = Path(temp).resolve()
            tools = base / "user bin"
            tools.mkdir()
            for name, text in (("codex", "#!/usr/bin/env node\n"), ("node", "#!/bin/sh\n"), ("git", "#!/bin/sh\n"),
                               ("binary", "\x7fELF")):
                write(tools / name, text, 0o700)
            with patch.dict(os.environ, {"PATH": f"{tools}:/usr/bin:/bin"}):
                sandbox = Sandbox(base / "script", tools / "codex")
                plain = Sandbox(base / "binary", tools / "binary")
            directories = sandbox.env()["PATH"].split(":")
            self.assertEqual(directories, [str(sandbox.shims), "/usr/local/bin", "/usr/bin", "/bin", str(sandbox.bin)])
            self.assertEqual(sorted(os.listdir(sandbox.bin)), ["codex", "node"])
            self.assertEqual(os.readlink(sandbox.bin / "node"), str(tools / "node"))
            self.assertEqual(os.listdir(plain.bin), ["codex"])
            shim = (sandbox.shims / "git").read_text()
            self.assertNotIn(str(tools), shim, "the git shim runs the caller's git")
            self.assertRegex(shim, r"(?m)^(exec /usr(/local)?/bin/git \"\$@\"|exit 127)$")


class RealCodex(unittest.TestCase):
    """The guard's hook contract against the real Codex, offline: the two hook values and goals off as
    codex-as passes them, a launch as codex-as exports it, and a loopback fake of the Responses API that
    answers from a script and sends rate limits as the real backend does. codex exec runs directly on the
    native, with --dangerously-bypass-hook-trust standing in for the user's trust in "Hooks need review";
    the trust prompt and the terminal session are left to the live acceptance. Codex reads its own
    install's files where they lie (Sandbox), nothing else of the user's."""

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
        cls.codex = os.path.realpath(found)
        with tempfile.TemporaryDirectory(prefix="codex-guard-real-version-") as base:
            sandbox = Sandbox(Path(base).resolve(), cls.codex)
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
        self.sandbox = Sandbox(base, self.codex)
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
        self.serve({} if sent is None else {"primary": [sent, 300, 3600], "secondary": [20.0, 10080, 86400]})
        if recorded is not None:
            window = {"used_percent": float(recorded), "window_minutes": 300, "resets_at": now + 3600}
            load_kit_module(self.kit, "accounts").write(self.record, {
                "email": "alice@example.com", "plan": "plus",
                "usage": {"primary": window, "secondary": None, "seen_at": now - 60}})
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

    def serve(self, limits):
        """The loopback fake as Codex's model provider, its answers carrying the rate limits LIMITS."""
        fake = subprocess.Popen([sys.executable, "-I", "-B", "-S", str(CODEX_BACKEND), "--log", str(self.requests),
                                 "--limits", json.dumps(limits), "--shell", f"echo call >> {shlex.quote(str(self.calls))}"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env={}, start_new_session=True)
        for cleanup in (fake.stdout.close, fake.stdin.close, lambda: fake.wait(10), lambda: kill_group(fake.pid)):
            self.addCleanup(cleanup)
        self.sandbox.configure(int(fake.stdout.readline()))

    def shell_calls(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def assert_state(self, state, present, absent):
        for name in present:
            self.assertTrue(os.path.exists(state + name), f"{name} is missing")
        for name in absent:
            self.assertFalse(os.path.exists(state + name), f"{name} exists")

    def test_exec_resume_begins_its_turn_here_or_starts_a_session_here(self):
        """What the check after a run takes as exec resume's own session: exec resume --last with no session of
        its directory starts one there, and a session it continues, from whatever directory that session
        started in (with --all), records this directory in the turn_context of its new turn."""
        self.serve({"primary": [20.0, 300, 3600]})
        other = self.sandbox.base / "other"
        other.mkdir()
        with open(self.sandbox.codex_home / "config.toml", "a") as stream:
            stream.write(f"[projects.{json.dumps(str(other))}]\ntrust_level = \"trusted\"\n")
        env = self.sandbox.env()
        for args in (["exec", "--skip-git-repo-check", "resume", "--last", "PLAIN here"],
                     ["exec", "-C", str(other), "--skip-git-repo-check", "PLAIN there"],
                     ["exec", "--skip-git-repo-check", "resume", "--last", "--all", "PLAIN back here"]):
            with self.subTest(args=args):
                code, out, err = self.sandbox.run(self.codex, args, env)
                self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
        self.assertEqual(self.sandbox.strays(), [], "Codex left a process behind")
        refused = [line for line in self.sandbox.netlog.read_text().splitlines() if line.startswith("REFUSED ssh")]
        self.assertEqual(refused, [], "Codex tried to reach a host through ssh")
        found = {}
        for path in (self.sandbox.codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"):
            lines = [json.loads(line) for line in path.read_text().splitlines()]
            found[lines[0]["payload"]["cwd"]] = (lines[0]["payload"]["source"],
                                                 [line["payload"]["cwd"] for line in lines if line["type"] == "turn_context"])
        work = str(self.sandbox.work)
        self.assertEqual(found, {work: ("exec", [work]), str(other): ("exec", [str(other), work])})

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
