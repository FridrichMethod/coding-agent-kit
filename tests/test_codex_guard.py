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
127.0.0.1, with every proxy at a closed loopback port, or for one case at a
loopback recorder that answers 502 and connects nowhere; it skips otherwise. Of
this host's user it reads only the codex install's own files where they lie
(Sandbox says which).
"""

import base64
import dataclasses
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
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
                         ["exec", "--", "help"], ["review", "help"], ["exec", "go", "help"],
                         # exec's spaced -i takes every word up to an option: help is an image, the prompt stdin's.
                         ["exec", "-i", "a.png", "help"], ["exec", "--image", "a.png", "help"],
                         ["e", "-i", "a.png", "help", "resume"], ["exec", "-i", "a.png", "resume", UUID, "go"]),
            # remote-control runs an app server that remote clients drive, and debug app-server
            # send-message-v2 runs a turn through one: model work as in app-server.
            "refused": (["cloud"], ["cloud-tasks"], ["cloud", "exec", "--env", "x", "go"], ["app-server"],
                        ["exec-server"], ["remote-control"], ["remote-control", "--no-daemon"],
                        ["debug", "app-server", "send-message-v2", "hi"], ["-c", "model=x", "app-server"],
                        ["app-server", "proxy"], ["app-server", "--listen", "stdio://"],
                        # help after another word is that word's argument: a cloud task's query, say.
                        ["cloud", "exec", "--env", "x", "help"], ["app-server", "proxy", "help"],
                        ["debug", "app-server", "send-message-v2", "help"],
                        # An option this scan does not know may take the next word as its value, as
                        # --cloud-skills-snapshot PATH does: Codex would run the app server.
                        ["app-server", "--cloud-skills-snapshot", "generate-ts"],
                        ["app-server", "--future", "generate-json-schema", "-o", "out"],
                        # A generator's name after another word is that word's argument.
                        ["app-server", "proxy", "generate-ts"], ["debug", "app-server", "generate-ts"],
                        # Codex 0.161.0 to 0.168 have no git-credential: without an OPERATION as its first word it is
                        # their root prompt, the TUI's, or one clap drops for a subcommand after it, which then runs.
                        ["git-credential"], ["-c", "model=x", "git-credential"], ["git-credential", "-c", "model=x"],
                        ["git-credential", "exec", "go"], ["git-credential", "resume", "--last"],
                        ["git-credential", "review"], ["git-credential", "-m", "x", "exec", "go"],
                        ["git-credential", "--"], ["git-credential", "--", "exec"]),
            "plain": (["mcp", "list"], ["mcp"], ["plugin"], ["features", "list"], ["login", "status"],
                      ["apply"], ["a"], ["sandbox", "linux", "--", "exec"], ["debug"], ["debug", "models"],
                      ["debug", "prompt-input", "hi"], ["completion", "bash"], ["update"], ["doctor"],
                      ["archive", UUID], ["delete", UUID], ["unarchive", UUID], ["migrate-rollouts"],
                      ["migrate-nfs-db"], ["--no-daemon", "migrate-nfs-db"],
                      # git-credential hands the account's login to Git's credential helper protocol. Its store and
                      # erase read Git's request and return, changing no login: run_git_credential in
                      # codex-rs/cli/src/git_credential.rs at rust-v0.169.0 (check.COMMANDS has the evidence).
                      ["git-credential", "get"], ["git-credential", "store"], ["git-credential", "erase"],
                      ["-c", "model=x", "git-credential", "capability"], ["git-credential", "--help"],
                      ["git-credential", "-c", "model=x", "erase"], ["git-credential", "--", "get"],
                      ["-c", "model=x", "git-credential", "--", "capability"],
                      # Before 0.169.0 another word after the prompt is clap's error, help its help subcommand.
                      ["git-credential", "help"], ["git-credential", "get", "exec", "go"],
                      # app-server's schema generators write protocol bindings and start no app server.
                      ["app-server", "generate-ts", "-o", "out"], ["app-server", "generate-ts", "--out=out", "-p", "x"],
                      ["app-server", "generate-ts", "--out", "out", "--prettier", "x", "--experimental"],
                      ["app-server", "generate-json-schema", "-o", "out", "--experimental"],
                      ["app-server", "generate-internal-json-schema", "--out", "out"],
                      ["-c", "model=x", "app-server", "--listen", "stdio://", "generate-ts", "-oout"],
                      ["app-server", "--strict-config", "generate-json-schema", "-o", "out"],
                      ["app-server", "-c", "model=x", "generate-internal-json-schema", "-o", "out"],
                      # A root option the scan does not know is read as a switch, as before every other command.
                      ["--future", "app-server", "generate-ts", "-o", "out"], ["--future", "mcp", "list"],
                      # A word after the generator is its own (a directory named daemon), never app-server's daemon.
                      ["app-server", "generate-ts", "--out", "daemon"], ["app-server", "generate-json-schema", "--out", "daemon"],
                      ["responses-api-proxy"], ["stdio-to-uds", "x"], ["execpolicy"],
                      # Help and version print and run nothing.
                      ["--help"], ["-h"], ["-V"], ["--version"], ["help"], ["help", "exec"],
                      ["resume", "--help"], ["exec", "--help"], ["exec", "go", "-V"], ["review", "-h"],
                      # exec has subcommands, so clap reads help after it as its help subcommand.
                      ["exec", "help"], ["e", "help", "resume"], ["exec", "-m", "x", "help"], ["exec", "help", "fork"],
                      ["exec", "--image=a.png", "help"], ["exec", "-ia.png", "help"], ["exec", "-i", "a.png", "-m", "x", "help"],
                      ["cloud", "--help"], ["app-server", "--help"], ["remote-control", "-h"],
                      ["debug", "app-server", "--help"], ["login", "--help"],
                      # These have subcommands too, so clap reads help as the first word after them as theirs.
                      ["cloud", "help"], ["cloud-tasks", "help"], ["cloud", "help", "exec"], ["cloud", "-c", "model=x", "help"],
                      ["app-server", "help"], ["app-server", "help", "proxy"], ["exec-server", "help"],
                      ["debug", "app-server", "help"]),
        }
        for form, argument_lists in cases.items():
            for args in argument_lists:
                with self.subTest(args=args):
                    self.assertEqual(self.plan(args).form, form)

    def test_exec_s_image_option_takes_every_word_after_it(self):
        """codex-cli 0.169.0: exec's -i/--image takes every following word up to an option (`<FILE>...`), so a
        help, resume or fork word after it is an image and Codex reads the prompt from stdin, in a new session;
        exec resume's and exec fork's take one value (`<FILE>`), and the session id after it stays theirs."""
        plan = self.plan(["exec", "-i", "a.png", "resume", UUID, "go"])
        self.assertEqual((plan.command, plan.form, plan.continued, plan.recover_id), ("exec", "headless", (), None))
        plan = self.plan(["exec", "resume", "-i", "a.png", UUID, "go"])
        self.assertEqual((plan.command, plan.form, plan.continued), ("exec-resume", "headless", (UUID,)))
        plan = self.plan(["exec", "fork", "--image", "a.png", UUID, "go"])
        self.assertEqual((plan.command, plan.forked, plan.recover_id), ("exec-resume", True, UUID))

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
                     ["remote-control", "start"], ["remote-control", "help"], ["app-server", "daemon"],
                     # An option the scan does not know may take the generator's name: daemon stays app-server's.
                     ["app-server", "--cloud-skills-snapshot", "generate-ts", "daemon", "start"],
                     ["app-server", "--startup-sign-out", "--print-startup-config"],
                     ["--enable", "use_agent_identity"]):
            with self.subTest(args=args):
                with self.assertRaises(ValueError):
                    self.plan(args)

    def test_app_server_s_startup_sign_out_is_refused_as_logout_is(self):
        """codex-cli 0.169.0: app-server --startup-sign-out (which needs --print-startup-config) signs the account out
        as logout does, removing its auth.json and revoking its tokens. clap takes the switch only before app-server's
        subcommand, exactly as written (no prefix, no value), so it is refused wherever it follows app-server: a word
        before it may be the value of an option the scan does not know."""
        for args in (["app-server", "--startup-sign-out", "--print-startup-config"],
                     ["app-server", "--print-startup-config", "--startup-sign-out"],
                     ["-c", "model=x", "app-server", "-c", "model=y", "--startup-sign-out", "--print-startup-config"],
                     ["app-server", "--cloud-skills-snapshot", "s.json", "--startup-sign-out", "--print-startup-config"],
                     ["app-server", "--startup-sign-out=true", "--print-startup-config"]):
            with self.subTest(args=args):
                with self.assertRaisesRegex(ValueError, r"^unsupported option; app-server --startup-sign-out signs the "
                                                        r"account out and revokes its tokens, as logout does\.$"):
                    self.plan(args)
        # Without it the form is app-server's as before; after -- or elsewhere it is no such switch.
        for args in (["app-server", "--print-startup-config"], ["app-server", "--", "--startup-sign-out"],
                     ["exec", "go", "--startup-sign-out"], ["debug", "app-server", "--startup-sign-out"],
                     ["--startup-sign-out", "go"]):
            with self.subTest(args=args):
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
        managed = (self.system[1], self.base / "etc/codex/requirements.toml")
        for name, value in (("SYSTEM_CONFIG", self.system), ("MANAGED_CONFIG", managed)):
            patcher = patch.object(layout, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_system_layers(self):
        """The system and managed layers; requirements.toml's [features] pins a feature."""
        self.assertEqual(tuple(map(str, self.host_system)), ("/etc/codex/config.toml", "/etc/codex/managed_config.toml"))
        for path in (*self.system, self.base / "etc/codex/requirements.toml"):
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

    def test_exec_resume_s_own_session_not_found_leaves_older_sessions_without_a_turn_unsure(self):
        """exec resume without an id continues an older session in which it begins a turn in its directory, or starts
        one there. When the run has neither (as when a Codex release stops recording a turn's directory), an older
        session of the account written during the run in which no turn began may be its own: MAYBE, so the check
        tells of it. Never when the run's own session is found, never one whose turn began elsewhere, and never for
        a run that continues no session it does not name."""
        old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.t0 - 3600))
        rollout = lambda thread, lines, **meta: self.rollout(thread, lines, timestamp=old, **meta)
        untold = rollout(self.THREAD, [CALL])
        resume = ["exec", "resume", "--last", "go"]
        self.assertEqual(self.threads(resume, self.t0 - 1, self.t0 + 10), ([], [], [[untold]]))
        self.assertEqual(self.threads(["exec", "go"], self.t0 - 1, self.t0 + 10), ([], [], []))
        self.assertEqual(self.threads(["exec", "resume", UUID, "go"], self.t0 - 1, self.t0 + 10), ([], [], []))
        self.assertEqual(self.threads(resume, self.t0 + 1, self.t0 + 10), ([], [], []), "written before the run")
        turned = rollout(self.OTHER, [turn(self.cwd), ANSWER])
        self.assertEqual(self.threads(resume, self.t0 - 1, self.t0 + 10), ([], [[turned]], []))
        turned.unlink()
        new = self.rollout(self.OTHER, [ANSWER])
        self.assertEqual(self.threads(resume, self.t0 - 1, self.t0 + 10), ([], [[new]], []))
        new.unlink()
        untold.unlink()
        rollout(self.THREAD, [turn(self.base), CALL])
        self.assertEqual(self.threads(resume, self.t0 - 1, self.t0 + 10), ([], [], []))

    def test_a_session_meta_line_longer_than_one_read_is_read(self):
        path = self.rollout(self.THREAD, [CALL], base_instructions={"text": "x" * (3 * launch.CHUNK)})
        self.assertEqual(self.threads(["exec", "go"], self.t0 - 1, self.t0 + 10), ([], [[path]], []))
        self.assertEqual(launch.session_meta(path)["id"], self.THREAD)

    def test_a_call_followed_by_more_output_than_one_read_counts(self):
        output = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1",
                                                       "output": "x" * (2 * launch.usage.TAIL)}}
        path = self.rollout(self.THREAD, [CALL, output, ANSWER])
        self.assertEqual(launch.activity([path], self.t0 - 1, self.t0 + 10), (True, True, True))

    def test_a_rollout_is_read_back_only_to_the_lines_before_the_run(self):
        """activity() reads each rollout back from its end, usage.TAIL bytes at a time, and stops at the read that
        reaches the lines from before the run, so its cost is what the run wrote plus one read; a call stamped
        before the run, in that read, never counts."""
        usage, old = launch.usage, time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.t0 - 3600))
        reasoning = {"type": "response_item", "payload": {"type": "reasoning", "summary": [], "encrypted_content": ""}}

        def lines(stamp, fill, count):
            line = {**reasoning, "payload": {**reasoning["payload"], "encrypted_content": fill * 4096}}
            return "".join(json.dumps({"timestamp": stamp, **line}) + "\n" for _ in range(count))

        path = self.rollout(self.THREAD, [])
        with path.open("a") as stream:
            stream.write(lines(old, "y", 1200) + json.dumps({"timestamp": old, **CALL}) + "\n"
                         + lines(self.stamp, "z", 600))
        self.assertGreater(path.stat().st_size, 7 * usage.TAIL)
        read, real = [], usage.opened

        class Counted:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.stream.close()

            def read(self, size=-1):
                data = self.stream.read(size)
                read.append(len(data))
                return data

            def __getattr__(self, name):
                return getattr(self.stream, name)

        with patch.object(usage, "opened", lambda path: Counted(real(path))):
            self.assertEqual(launch.activity([path], self.t0 - 1, self.t0 + 10), (True, True, False))
        # The lines of the run and the one read that reaches back before it.
        self.assertLessEqual(sum(read), 4 * usage.TAIL)

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

    def test_a_launch_that_holds_the_lease_alone_bounds_the_lease_file(self):
        """Layout.arrive: a launch whose acquire() got the lease exclusively, so that no run in progress compares
        sizes, empties a lease file of LEASE_LIMIT bytes or more before it appends; one that shares the lease never
        empties it, so a run in progress still reads another size once a launch arrived."""
        lease, other = (layout.Layout(check, "A", self.config) for _ in range(2))
        for item in (lease, other):
            self.addCleanup(item.close)
        path = self.root / "accounts/.locks/A.lock"
        self.assertTrue(lease.acquire())
        path.write_bytes(b"\n" * (layout.LEASE_LIMIT - 1))
        lease.close()
        self.assertTrue(lease.acquire())
        self.assertEqual(lease.arrive(True), layout.LEASE_LIMIT, "below the limit it only grows")
        lease.close()
        self.assertTrue(lease.acquire())
        self.assertEqual(lease.arrive(True), 1)
        fcntl.flock(lease.fd, fcntl.LOCK_SH)  # as a launch holds it while Codex runs
        path.write_bytes(b"\n" * layout.LEASE_LIMIT)
        self.assertFalse(other.acquire())
        self.assertEqual(other.arrive(False), layout.LEASE_LIMIT + 1)
        info = path.lstat()
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
import json, os, pathlib, re, signal, subprocess, sys, time, tomllib
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
          "hooks": [], "events": []}
names = {"prompt": "UserPromptSubmit", "tool": "PreToolUse"}


def run_hook(kind, event):
    """The guard's hook as Codex runs it: the command of the -c value through a shell, the event on stdin.
    Without that -c value (a launch without reserves) Codex runs no such hook."""
    value = next((arg for arg in args if arg.startswith(f"hooks.{names[kind]}=")), None)
    if value is None:
        return
    command = tomllib.loads("v = " + value.partition("=")[2])["v"][0]["hooks"][0]["command"]
    done = subprocess.run(["/bin/sh", "-c", command], input=json.dumps(event), capture_output=True, text=True)
    record["hooks"].append([kind, done.returncode, done.stdout, done.stderr])
    record["events"].append(event)
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


def stamped(then):
    """Milliseconds, truncated, as native Codex stamps its lines."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(then)) + ".%03dZ" % (int(then * 1000) % 1000)


for item in json.loads(os.environ.get("FAKE_ROLLOUTS", "[]")):
    now = time.time()
    # "offset" seconds later for a writer whose clock runs ahead.
    stamp = stamped(now + item.get("offset", 0))
    if "append" in item:  # a resumed thread: Codex appends to its rollout, wherever it lies
        path, lines = pathlib.Path(item["append"]), item["lines"]
        found = re.search(r"-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?:_[0-9a-f-]{36})?\.jsonl$", path.name)
        try:  # what the hooks of the session that continues it name, from its session_meta line
            with open(path) as stream:
                existing = json.loads(stream.readline())["payload"]
        except (OSError, ValueError, KeyError, TypeError):
            existing = {}
        existing = existing if isinstance(existing, dict) else {}
        thread = item.get("thread") or (found[1] if found else None)
        session = item.get("session_id") or existing.get("session_id") or thread
        spawned = item.get("source", existing.get("source"))
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
        thread, session, spawned = item["thread"], meta["session_id"], meta["source"]
    # A step {"hook": KIND, ...} among the lines: Codex has written the lines before it, then runs that hook for
    # this rollout and the turn of its last task_started line, as codex-cli 0.171.0 does; the step's other keys
    # replace fields of the event (null removes one). The lines after it are stamped when they are written.
    turn, written = None, []
    for line in [*lines, None]:
        if line is not None and "hook" not in line:
            written.append(line)
            payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
            if line.get("type") == "event_msg" and payload.get("type") == "task_started":
                attribution = payload.get("turn_attribution")
                turn = payload.get("turn_id", attribution.get("turn_id") if isinstance(attribution, dict) else None)
            continue
        with open(path, "a") as stream:
            for row in written:
                stream.write(json.dumps({"timestamp": stamp, **row}) + "\n")
        written = []
        if line is None:
            break
        event = {"hook_event_name": names[line["hook"]], "cwd": os.getcwd(), "transcript_path": str(path),
                 "session_id": session, "turn_id": turn, "model": "fixture-model", "permission_mode": "default"}
        if line["hook"] == "tool":
            event.update(tool_name="Bash", tool_input={"command": "true"}, tool_use_id="call-1")
        else:
            event["prompt"] = "go"
        if isinstance(spawned, dict):  # a sub-agent: its own thread, beside the root's session_id
            event.update(agent_id=thread, agent_type="default")
        for key, value in line.items():
            if key != "hook":
                event[key] = value
        run_hook(line["hook"], {key: value for key, value in event.items() if value is not None})
        stamp = stamped(time.time() + item.get("offset", 0))
    if "mtime" in item:  # the file's time as a file server sets it whose clock is "mtime" seconds off
        os.utime(path, (now + item["mtime"], now + item["mtime"]))
    transcripts.append(str(path))
# The guard's hooks as Codex runs them after every rollout is written, with events that name no turn.
for kind in json.loads(os.environ.get("FAKE_HOOKS", "[]")):
    event = {"hook_event_name": names[kind], "cwd": os.getcwd(),
             "transcript_path": transcripts[0] if transcripts else None}
    if kind == "tool":
        event.update(tool_name="Bash", tool_input={"command": "true"})
    run_hook(kind, event)
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
# The launch's notes of the turns its hooks were called for, as Codex leaves them when it exits.
try:
    record["turns"] = pathlib.Path(state + ".turns").read_text() if state else None
except OSError:
    record["turns"] = None
# The account's attested usage as the guard's hooks left it, before codex-as writes it after the run.
try:
    record["attested"] = json.loads(pathlib.Path(os.path.dirname(state), "codex-usage-" + json.loads(launch)["account"]
                                                 + ".json").read_text()) if state else None
except (OSError, ValueError):
    record["attested"] = None
pathlib.Path(os.environ["FAKE_RECORD"]).write_text(json.dumps(record))
print("fake codex ran")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''
USER, WORKSPACE = "user-fixture-A", "workspace-fixture-A"
THREAD = "019a0000-0000-7000-8000-00000000000a"
OTHER = "019a0000-0000-7000-8000-0000000000bb"  # another session of the account
# Turn ids as Codex makes them, one per submission.
TURN_1, TURN_2, TURN_3 = (f"019a0000-0000-7000-9000-00000000000{digit}" for digit in "123")
LAUNCH_LINE = "codex-as: usage reserves for A: {policy}; hooks refuse new prompts and tool calls at a reserve.\n"
WITHOUT = "; to launch without usage reserves, run codex-as --reserve-5h off --reserve-weekly off {account} with the same arguments.\n"
# After a run whose own sessions show that Codex did not run a hook, and after one that may have written a session
# showing it, which another client of the account may have written instead.
WARNING = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run {} (not trusted, disabled, "
           "or managed hooks only). Start codex-as A interactively and trust them in \"Hooks need review\".\n")
NOTICE = ("codex-as: A: the usage reserve may not have been enforced in this run: a session of A written during it, by "
          "this run or by another client of A, shows that Codex did not run {} (not trusted, disabled, or managed hooks "
          "only), and the check after the run cannot tell which client wrote it. If it was this run, start codex-as A "
          "interactively and trust them in \"Hooks need review\".\n")
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


def login(name, user, workspace, email="alice@example.com"):
    """The credential file's text of account NAME: ChatGPT user USER in workspace WORKSPACE (fabricated tokens)."""
    claims = {"sub": f"fixture-{name}", "email": email,
              "https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_user_id": user}}
    return json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-10-01T00:00:00Z",
                       "tokens": {"id_token": id_token(claims), "access_token": "fabricated-sensitive-access",
                                  "refresh_token": "fabricated-sensitive-refresh", "account_id": workspace}})


def tag(user, workspace):
    """The tag of a login in a launch's notes and the attested usage: 16 hex digits of the SHA-256 of its user and
    workspace."""
    return hashlib.sha256(f"{user}\n{workspace}".encode()).hexdigest()[:16]


TAG, TAG_B = tag(USER, WORKSPACE), tag("user-fixture-B", "workspace-fixture-B")


def stamp_of(moment):
    """A rollout line's timestamp for the epoch time MOMENT, as Codex writes one (milliseconds, truncated)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(moment)) + ".%03dZ" % (int(moment * 1000) % 1000)


def at(moment, lines):
    """LINES stamped at MOMENT; a hook step is left as it is."""
    return [line if "hook" in line else {"timestamp": stamp_of(moment), **line} for line in lines]


def aged(moment, lines):
    """LINES with every line before the last hook step stamped at MOMENT: the reply after that step is newer, by its own
    second, than any reading a hook took before it, so the one recorded after the run replaces it."""
    last = max((index for index, line in enumerate(lines) if "hook" in line), default=-1)
    return at(moment, lines[:last]) + lines[last:]


def usage_of(used, seen_at, minutes=300):
    """A stored usage object: USED percent of a window of MINUTES that resets in an hour, read at SEEN_AT."""
    return {"primary": {"used_percent": used, "window_minutes": minutes, "resets_at": int(time.time()) + 3600},
            "secondary": None, "seen_at": seen_at}


def reply_time(path, turn):
    """The whole second of the last token_usage_record line of TURN in the rollout at PATH: a snapshot's time."""
    rows = [json.loads(line) for line in Path(path).read_text().splitlines()]
    stamps = [row["timestamp"] for row in rows if row["type"] == "token_usage_record" and row["payload"]["turn_id"] == turn]
    return int(launch.usage.moment(stamps[-1]))


# The lines of a turn as codex-cli 0.171.0 writes them. task_started, turn_context, token_usage_record, task_complete
# and turn_aborted name the turn they stand in; item_completed names the turn its item began in, which can be a turn
# that has ended. A step {"hook": KIND} is where Codex runs that hook (the fake: FAKE).
PROMPT_STEP, TOOL_STEP = {"hook": "prompt"}, {"hook": "tool"}
WORLD = {"type": "world_state", "payload": {}}
OUTPUT = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1", "output": "done"}}
INHERITED = {"metadata": {"inherited_user_message": True}}


def started(turn):
    """The line that opens a turn; the id stands in it twice."""
    return {"type": "event_msg", "payload": {
        "type": "task_started", "turn_id": turn, "root_turn_id": turn, "model_context_window": 258400,
        "collaboration_mode_kind": "default",
        "turn_attribution": {"turn_id": turn, "root_turn_id": turn, "turn_trigger": "exec"}}}


def context(turn, cwd=None):
    """The turn_context line of a turn: its id, and the session's working directory at that turn."""
    payload = {"turn_id": turn, "root_turn_id": turn, "model": "fixture-model"}
    return {"type": "turn_context", "payload": payload if cwd is None else {**payload, "cwd": str(cwd)}}


def usage_record(turn, thread, root=None):
    """The token_usage_record line Codex writes right after a response's items; ROOT is the session's root thread."""
    return {"type": "token_usage_record", "payload": {"thread_id": thread, "turn_id": turn, "session_id": root or thread,
                                                      "root_turn_id": turn, "response_id": "resp_fixture"}}


def completed(turn):
    return {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn, "root_turn_id": turn,
                                             "last_agent_message": "done"}}


def aborted(turn):
    return {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": turn, "reason": "interrupted"}}


def item_completed(turn, thread):
    return {"type": "event_msg", "payload": {"type": "item_completed", "thread_id": thread, "turn_id": turn}}


def settings_applied(thread):
    """The line a client writes when it takes up a thread, before its turn starts; it names no turn."""
    return {"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_id": thread}}


def said(role, text):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
                                                 "content": [{"type": "input_text", "text": text}]}}


def literal_turn(turn, thread, used, calls=2, hooks=("prompt", "tool"), resumed=False, root=None, cwd=None):
    """One turn as codex-cli 0.171.0 writes it: in a new session its head and the thread_settings_applied line, in a
    resumed one the thread_settings_applied line of the client that takes the thread up, then task_started and
    turn_context; the prompt hook (a step, when "prompt" is in HOOKS); the prompt; CALLS tool calls, each response's
    token_usage_record before the call's tool hook (when "tool" is in HOOKS), and its token_count after the call's
    output; the answer, its record and its token_count; task_complete. USED is the five-hour percentage every
    token_count shows, or a list of CALLS + 1, one per response; None is a reply without rate limits."""
    readings = list(used) if isinstance(used, (list, tuple)) else [used] * (calls + 1)
    counts = [NO_LIMITS if percent is None else snapshot(percent) for percent in readings]
    if resumed:
        lines = [settings_applied(thread), started(turn), context(turn, cwd)]
    else:
        lines = [started(turn), said("developer", "instructions"), said("user", "<environment_context>"), WORLD,
                 context(turn, cwd), settings_applied(thread)]
    lines += [PROMPT_STEP] if "prompt" in hooks else []
    lines += [said("user", "go"), item_completed(turn, thread)]
    for count in counts[:-1]:
        lines += [CALL, usage_record(turn, thread, root), *([TOOL_STEP] if "tool" in hooks else []),
                  item_completed(turn, thread), OUTPUT, count]
    return lines + [item_completed(turn, thread), ANSWER, usage_record(turn, thread, root), counts[-1], completed(turn)]


def refused_turn(turn, thread, cwd=None):
    """A turn whose prompt the prompt hook refused: its head, the hook, and task_complete with no reply."""
    lines = literal_turn(turn, thread, None, calls=0, hooks=("prompt",), cwd=cwd)
    return lines[:lines.index(PROMPT_STEP) + 1] + [completed(turn)]


def copied_head(turn):
    """What a sub-agent's rollout starts with (fork_turns all): a copy of its parent's history, whose turn lines name
    the parent's TURN unmarked, a copied turn_aborted without an id, response items marked inherited, no token line."""
    return [started(turn), {**said("user", "go"), **INHERITED}, context(turn), {**CALL, **INHERITED},
            {**ANSWER, **INHERITED}, completed(turn), {"type": "event_msg", "payload": {"type": "turn_aborted",
                                                                                     "turn_id": None}}]


class Recording(Fixture):
    """The account's usage after a run, in process: launch.noted_turns() reads a launch's notes of its turns, and
    launch.remember() takes the run's snapshot from the noted turns of the rollouts its hooks named (recorded(),
    usage.witnessed), whoever created the thread, writing it to the account record and to the attested usage under
    the login's own tag. A guarded run that viewed none of those rollouts is scanned as by every earlier release
    (usage.latest) when a hook ran, and records nothing when none ran; a run without a guard is scanned."""

    STATE = "codex-as-A-123-1700000000"

    def setUp(self):
        super().setUp()
        self.guard_dir = launch.metadata.directory(self.config, create=True, name="guard")
        self.record = launch.metadata.directory(self.config, create=True) / "codex-A.json"
        self.attested = self.guard_dir / "codex-usage-A.json"
        self.layout = layout.Layout(check, "A", self.config)
        self.raw = login("A", USER, WORKSPACE)
        self.now = time.time()
        self.begun = self.now - 60
        # Today's day directory, as Codex names it from the clock: the scan of a run reads the days around it only.
        self.day = time.strftime("%Y/%m/%d", time.localtime(self.now))

    def rollout(self, thread, lines, creator=(USER, WORKSPACE), seconds=0, days=0):
        """The rollout of THREAD below sessions/ of a session that started DAYS ago, in the day directory of that day
        (today's by default), its session_meta line written when it is new, with LINES (hook steps left out) stamped
        SECONDS from now; (its path, its path below sessions/)."""
        begun = self.now - days * 86400
        day = time.strftime("%Y/%m/%d", time.localtime(begun))
        (self.root / "sessions" / day).mkdir(parents=True, exist_ok=True)
        name = f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', time.localtime(begun))}-{thread}.jsonl"
        path = self.root / "sessions" / day / name
        meta = {"id": thread, "session_id": thread, "timestamp": stamp_of(begun + seconds), "cwd": str(self.cwd),
                "source": "exec", "thread_source": "user", "creator_user_id": creator[0], "creator_account_id": creator[1]}
        rows = ([] if path.exists() else [{"type": "session_meta", "payload": meta}]) + at(self.now + seconds, lines)
        with path.open("a") as stream:
            for row in rows:
                if "hook" not in row:
                    stream.write(json.dumps(row) + "\n")
        return path, f"{day}/{path.name}"

    def notes(self, text, ran=()):
        """The launch's state prefix in the guard directory, its notes file holding TEXT (None: no notes file), and a
        STATE.ran.KIND file for each KIND of RAN, the hooks that ran, as the guard leaves them."""
        state = str(self.guard_dir / self.STATE)
        for name in (".turns", ".ran.prompt", ".ran.tool"):
            Path(state + name).unlink(missing_ok=True)
        if text is not None:
            Path(state + ".turns").write_text(text)
        for kind in ran:
            Path(state + ".ran." + kind).touch()
        return state

    def remember(self, state, raw=None, args=("exec", "go")):
        """launch.remember() after a run of codex-as A ARGS with the launch state STATE; the account record then."""
        launch.remember(self.config, self.layout, raw or self.raw, self.plan(list(args)), self.begun, state=state)
        return json.loads(self.record.read_text()) if self.record.exists() else None

    def test_noted_turns_reads_a_regular_bounded_file_only(self):
        """The notes are read as .blocked is: never through a link, from a FIFO or from a directory, and at most
        usage.NOTES + 1 bytes, of which the piece after the last line feed counts for nothing."""
        rel = f"2026/11/22/rollout-2026-11-22T10-00-00-{THREAD}.jsonl"
        first, second = f"t {TURN_1} {rel}\n", f"t {TURN_2} {rel}\n"
        self.assertIsNone(launch.noted_turns(None))
        state = self.notes(None)
        self.assertIsNone(launch.noted_turns(state))
        self.notes(f"o {TAG}\n" + first + second)
        self.assertEqual(launch.noted_turns(state), (TAG, {rel: {TURN_1, TURN_2}}))
        with patch.object(launch.usage, "NOTES", len(f"o {TAG}\n" + first)):
            self.assertEqual(launch.noted_turns(state), (TAG, {rel: {TURN_1}}))
        elsewhere = self.base / "notes elsewhere"
        os.replace(state + ".turns", elsewhere)
        os.symlink(elsewhere, state + ".turns")
        self.assertIsNone(launch.noted_turns(state))
        os.unlink(state + ".turns")
        os.mkfifo(state + ".turns")
        # Read in a thread: a reader that waits on the FIFO fails the case instead of hanging the suite.
        found = []
        reader = threading.Thread(target=lambda: found.append(launch.noted_turns(state)), daemon=True)
        reader.start()
        reader.join(10)
        waited = reader.is_alive()
        if waited:  # a writer's open lets a reader that waits in open() go
            try:
                os.close(os.open(state + ".turns", os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass
        self.assertFalse(waited, "the notes were waited on as a FIFO")
        self.assertEqual(found, [None])
        os.unlink(state + ".turns")
        os.mkdir(state + ".turns")
        self.assertIsNone(launch.noted_turns(state))

    def test_a_run_whose_hooks_named_turns_reads_only_those_rollouts_and_writes_both_files(self):
        """The noted turn of a thread another account created counts; the turns of other clients before and after it
        in that rollout do not, nor a newer snapshot of this account's in a rollout no hook named, which the scan of
        a run that viewed no rollout would take. The record and the attested usage get the same snapshot, stamped with
        the time of its reply, and the attested usage names the login's tag, the rollout and the launch."""
        other = ("user-fixture-B", "workspace-fixture-B")
        _, rel = self.rollout(THREAD, literal_turn(TURN_2, THREAD, 90.0), creator=other, seconds=-30)
        self.rollout(THREAD, literal_turn(TURN_1, THREAD, [30.0, 30.5, 31.0], resumed=True), seconds=-20)
        self.rollout(THREAD, literal_turn(TURN_3, THREAD, 95.0, resumed=True), seconds=-10)
        self.rollout(OTHER, literal_turn(TURN_3, OTHER, 50.0), seconds=-5)
        reading = self.remember(self.notes(f"o {TAG}\nt {TURN_1} {rel}\n"))["usage"]
        self.assertEqual((reading["primary"]["used_percent"], reading["primary"]["window_minutes"], reading["secondary"],
                          reading["seen_at"]), (31.0, 300, None, int(self.now - 20)))
        self.assertEqual(json.loads(self.attested.read_text()),
                         {"owner": TAG, "usage": reading, "rollout": rel, "launch": self.STATE})
        self.assertEqual(oct(self.attested.stat().st_mode & 0o777), "0o600")

    def test_a_run_s_record_follows_what_its_hooks_named(self):
        """One sub-case per rule of launch.remember() after a run; none writes the attested usage. Without a guard, the
        scan of every earlier release (usage.latest), here this account's newest snapshot. After a guarded run that
        viewed a rollout its hooks named, whose last MiB names a turn, the noted turns alone: a tail that names only
        other clients' turns leaves the stored usage as it is, also when the noted turn lies more than the tail before
        the rollout's end, in a thread this account created and in one another account created that the run resumed by
        id. After a guarded run that viewed none, the scan when a hook ran (events without a turn id, a rollout without
        turn ids in UUID form, a noted rollout that does not exist), and nothing when no hook ran, not even this
        account's own snapshot. The scan never reads an older session this account created in which another account
        wrote during the run."""
        stored = usage_of(20.0, int(self.now) - 3600)
        other = ("user-fixture-B", "workspace-fixture-B")
        older, theirs = "019a0000-0000-7000-8000-0000000000cc", "019a0000-0000-7000-8000-0000000000dd"

        def after(state, args=("exec", "go")):
            """The record's usage after launch.remember() over a record that holds STORED: "stored" when it stays."""
            self.attested.unlink(missing_ok=True)
            launch.metadata.write(self.record, {"email": "alice@example.com", "plan": "plus", "usage": stored})
            reading = self.remember(state, args=args)["usage"]
            self.assertFalse(self.attested.exists())
            return "stored" if reading == stored else reading["primary"]["used_percent"]

        _, own = self.rollout(OTHER, [CALL, snapshot(50.0)], seconds=-10)
        _, plain = self.rollout(THREAD, literal_turn("1", THREAD, 45.0), seconds=-20)  # turn ids not in UUID form
        self.rollout(older, literal_turn(TURN_2, older, 90.0, hooks=(), resumed=True), days=3, seconds=-5)
        missing = f"{self.day}/rollout-2026-11-22T10-00-00-{UUID}.jsonl"
        with self.subTest(rule="no guard: the scan"):
            self.assertEqual(after(None), 50.0)
        with self.subTest(rule="guarded, viewed nothing, a hook ran: the scan"):
            for text, ran in ((f"o {TAG}\n", ("prompt",)), (f"o {TAG}\nt {TURN_1} {plain}\n", ("prompt", "tool")),
                              (f"o {TAG}\nt {TURN_1} {missing}\n", ("tool",))):
                self.assertEqual(after(self.notes(text, ran)), 50.0, text)
        with self.subTest(rule="guarded, viewed nothing, no hook ran: nothing"):
            for text in (None, f"o {TAG}\n"):
                self.assertEqual(after(self.notes(text)), "stored", text)
        shutil.rmtree(self.root / "sessions")
        with self.subTest(rule="guarded, viewed: the noted turns alone"):
            # The noted turn's thread holds only another client's turn: what stands there is not this launch's.
            _, own = self.rollout(OTHER, [CALL, snapshot(50.0)], seconds=-10)
            self.rollout(OTHER, literal_turn(TURN_3, OTHER, 52.0, resumed=True), seconds=-8)
            # Another account's turn of more than the part read after the noted turn, in a thread this account created
            # and in one another account created, which the run resumed by id.
            cases = [(f"o {TAG}\nt {TURN_1} {own}\n", ("exec", "go"))]
            for thread, creator, args in ((THREAD, (USER, WORKSPACE), ("exec", "go")), (theirs, other, ("resume", theirs))):
                if creator == other:
                    self.rollout(thread, literal_turn(TURN_3, thread, 70.0), creator=other, seconds=-120)
                _, rel = self.rollout(thread, literal_turn(TURN_1, thread, [30.0, 30.5, 31.0], resumed=creator == other),
                                      creator=creator, seconds=-40)
                foreign = literal_turn(TURN_2, thread, 90.0, resumed=True)
                foreign[3:3] = [said("assistant", "p" * 32768)] * (launch.usage.TAIL // 32768 + 1)
                self.rollout(thread, foreign, seconds=-5)
                cases.append((f"o {TAG}\nt {TURN_1} {rel}\n", args))
            for text, args in cases:
                self.assertEqual(after(self.notes(text, ("prompt", "tool")), args), "stored", (text, args))

    def test_the_scan_of_a_guarded_run_reads_by_id_only_the_thread_it_resumed(self):
        """A guarded run whose hooks ran but named no turn is scanned as a run without a guard: by id it reads the
        thread it resumed by id and nothing else, neither an older session of this account in the date directory of
        its own day, which resume --last continued, nor a sub-agent's rollout that another account created, also when
        the run resumed that sub-agent's session by id."""
        older = self.root / "sessions/2026/09/01"
        older.mkdir(parents=True)
        spawned = {"subagent": {"thread_spawn": {"parent_thread_id": UUID, "depth": 1}}}
        for thread, creator, source, used in ((THREAD, (USER, WORKSPACE), "cli", 40.0),
                                              (OTHER, ("user-fixture-B", "workspace-fixture-B"), spawned, 90.0)):
            meta = {"id": thread, "session_id": UUID if isinstance(source, dict) else thread, "source": source,
                    "timestamp": stamp_of(self.now - 80 * 86400), "cwd": str(self.cwd), "thread_source": "user",
                    "creator_user_id": creator[0], "creator_account_id": creator[1]}
            (older / f"rollout-2026-09-01T10-00-00-{thread}.jsonl").write_text("".join(
                json.dumps(row) + "\n" for row in at(self.now - 10, [{"type": "session_meta", "payload": meta}, ANSWER,
                                                                      snapshot(used)])))
        state = self.notes(f"o {TAG}\n", ("prompt",))
        for args, used in ((("resume", "--last"), None), (("resume", UUID), None), (("resume", THREAD), 40.0)):
            with self.subTest(args=args, used=used):
                self.record.unlink(missing_ok=True)
                reading = self.remember(state, args=args).get("usage")
                self.assertEqual(reading and reading["primary"]["used_percent"], used)

    def test_a_viewed_rollout_without_a_snapshot_keeps_the_scan_out(self):
        """A prompt interrupted before its reply, in a thread another account created that the run resumed by id: the
        prompt hook named the launch's turn, so the rollout is viewed, and what that account wrote there since the run
        began (92 percent) is neither recorded nor attested, although the scan, which reads a thread resumed by id
        whoever wrote it, would take it."""
        other = ("user-fixture-B", "workspace-fixture-B")
        self.rollout(THREAD, literal_turn(TURN_2, THREAD, 92.0, calls=0, hooks=()), creator=other, seconds=-20)
        mine =literal_turn(TURN_1, THREAD, None, calls=0, hooks=("prompt",), resumed=True)
        _, rel = self.rollout(THREAD, mine[:mine.index(PROMPT_STEP) + 1] + [aborted(TURN_1)], seconds=-10)
        record = self.remember(self.notes(f"o {TAG}\nt {TURN_1} {rel}\n", ("prompt",)), args=("resume", THREAD))
        self.assertEqual(record, {"email": "alice@example.com", "plan": "plus"})
        self.assertFalse(self.attested.exists())

    def test_the_noted_rollouts_are_read_with_the_file_server_s_clock_allowance(self):
        """launch.recorded() passes SKEW to usage.witnessed: a noted rollout whose file time lies a minute before the
        reading found in another noted rollout, as a file server whose clock lags this host's sets it, is still read,
        and its newer reading is the run's."""
        newer, first = self.rollout(THREAD, literal_turn(TURN_1, THREAD, 50.0, calls=0), seconds=-5)
        older, second = self.rollout(OTHER, literal_turn(TURN_2, OTHER, 30.0, calls=0), seconds=-10)
        lagging = older.stat().st_mtime - 70
        os.utime(newer, (lagging, lagging))
        reading = self.remember(self.notes(f"o {TAG}\nt {TURN_1} {first}\nt {TURN_2} {second}\n"))["usage"]
        self.assertEqual((reading["primary"]["used_percent"], reading["seen_at"]), (50.0, int(self.now - 5)))

    def test_the_attested_usage_is_written_only_under_the_login_s_own_tag(self):
        """The record takes the noted turns' snapshot whatever the notes' owner; the attested usage only when the login
        read after the run has the tag the launcher noted at the start: not under another login's tag, nor without an
        owner line, nor when the account's login changed during the run."""
        _, rel = self.rollout(THREAD, literal_turn(TURN_1, THREAD, 33.0))
        note = f"t {TURN_1} {rel}\n"
        changed = login("A", "user-fixture-B", "workspace-fixture-B")
        for text, raw in ((f"o {TAG_B}\n" + note, None), (note, None), (f"o {TAG}\n" + note, changed)):
            with self.subTest(notes=text, changed=raw is not None):
                self.record.unlink(missing_ok=True)
                self.assertEqual(self.remember(self.notes(text), raw=raw)["usage"]["primary"]["used_percent"], 33.0)
                self.assertFalse(self.attested.exists())
        self.remember(self.notes(f"o {TAG}\n" + note))
        self.assertEqual(json.loads(self.attested.read_text())["usage"]["primary"]["used_percent"], 33.0)

    def test_a_failed_attested_write_still_writes_the_account_record(self):
        """The attested usage is best effort on its own: a guard directory that is not 0700, or a directory in the
        file's place, leaves it unwritten and the record still takes the snapshot."""
        _, rel = self.rollout(THREAD, literal_turn(TURN_1, THREAD, 34.0))
        state = self.notes(f"o {TAG}\nt {TURN_1} {rel}\n")
        for case in ("mode 0755", "a directory"):
            with self.subTest(case=case):
                self.record.unlink(missing_ok=True)
                if case == "mode 0755":
                    self.guard_dir.chmod(0o755)
                else:
                    self.guard_dir.chmod(0o700)
                    self.attested.mkdir()
                self.assertEqual(self.remember(state)["usage"]["primary"]["used_percent"], 34.0)
                self.assertFalse(self.attested.is_file())


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
        self.credential("A", USER, WORKSPACE)
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

    def credential(self, name, user, workspace, email="alice@example.com"):
        """The login of account NAME: ChatGPT user USER in workspace WORKSPACE (fabricated tokens)."""
        path = self.home / f".secrets/codex-{name}.json"
        path.write_text(login(name, user, workspace, email))
        path.chmod(0o600)
        return path

    def attested(self, name="A"):
        """The account's attested usage in the guard directory, or None while there is none."""
        path = self.guard_dir / f"codex-usage-{name}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def seed(self, path, data):
        """DATA as a metadata file of the kit at PATH: 0600, in a 0700 directory made when missing."""
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def rel(self, path):
        """A rollout's path below sessions/, as a launch's notes and the attested usage name it."""
        return "/".join(Path(path).parts[-4:])

    def launch_name(self):
        """The launch state's name of the last launch the fake Codex recorded."""
        return os.path.basename(json.loads(self.recorded()["env"]["CODING_AGENT_KIT_GUARD"])["state"])

    def configure(self, text):
        path = self.home / ".config/coding-agent-kit/config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        for directory in (path.parent, path.parent.parent):
            directory.chmod(0o700)
        path.write_text(text)
        path.chmod(0o600)

    def run_codex(self, *args, env=None, preexec_fn=None):
        self.record.unlink(missing_ok=True)
        result = subprocess.run([str(self.kit / "bin/codex-as"), *args], cwd=self.cwd, env=self.env | (env or {}),
                                stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=60,
                                start_new_session=True, preexec_fn=preexec_fn)
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

    def test_the_lease_file_stays_bounded(self):
        """Each launch that can write sessions adds a byte to the account's lease file, with reserves or without; one
        that holds the lease alone empties it first once it has reached LEASE_LIMIT bytes."""
        lease = self.root / "accounts/.locks/A.lock"
        for flags, size in (((), 1), ((), 2), (("--reserve-5h", "off"), 3)):
            with self.subTest(flags=flags, size=size):
                self.assertEqual(self.run_codex(*flags, "A", "exec", "go").returncode, 0)
                self.assertEqual(lease.stat().st_size, size)
        lease.write_bytes(b"\n" * layout.LEASE_LIMIT)
        for flags, size in (((), 1), (("--reserve-5h", "off", "--reserve-weekly", "off"), 2)):
            with self.subTest(flags=flags, size=size, full=True):
                self.assertEqual(self.run_codex(*flags, "A").returncode, 0)
                self.assertEqual(lease.stat().st_size, size)

    def test_a_launch_whose_state_cannot_be_written_names_the_guard_directory(self):
        """A write to the guard directory that fails, as on a full disk or a home directory at its quota, refuses the
        launch naming that directory and the reason, not as a credential problem; Codex is not started and the guard
        directory holds no launch state. Every file write fails here (RLIMIT_FSIZE 0) once a first launch has set the
        account up: the lease's byte and the account record are best effort, so the first write that fails is the
        launch's state file."""
        self.trusted()
        self.assertEqual(self.run_codex("A", "exec", "go").returncode, 0)

        def no_file_writes():
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))

        for args in (["A", "exec", "go"], ["A", "fix it"]):
            with self.subTest(args=args):
                result = self.run_codex("--reserve-5h", "10", *args, preexec_fn=no_file_writes)
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"codex-as: {self.guard_dir}: cannot record this launch's usage reserve state "
                                         f"({os.strerror(errno.EFBIG)}).\n"))
                self.assertFalse(self.record.exists(), "Codex started")
                self.assertEqual(self.launches(), [])

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
                               # help is an image here, and the prompt comes from stdin.
                               (["A", "exec", "-i", "shot.png", "help"], [*self.guarded(False), "exec", "-i", "shot.png", "help"]),
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
                            ["A", "git-credential", "get"], ["A", "cloud", "help"], ["A", "app-server", "help"],
                            ["A", "git-credential", "store"], ["A", "git-credential", "erase"],
                            ["A", "git-credential", "--", "get"], ["A", "exec-server", "help"], ["A", "debug", "app-server", "help"],
                            ["A", "app-server", "generate-ts", "-o", "out"],
                            ["A", "app-server", "generate-json-schema", "--out", "out", "--experimental"],
                            ["A", "app-server", "generate-internal-json-schema", "-o", "out"])

    def refused(self, args, reason, account="A", env=None):
        result = self.run_codex("--reserve-weekly", "10", *args, env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", f"codex-as: {account}: {reason}" + WITHOUT.format(account=account)))
        self.assertFalse(self.record.exists(), "a refused launch started Codex")
        self.assertFalse((self.root / f"accounts/{account}").exists(), "a refused launch prepared the account")

    def test_forms_the_hooks_cannot_see_are_refused(self):
        self.trusted()
        for args, name in ((["cloud"], "cloud"), (["cloud-tasks"], "cloud"), (["cloud", "list"], "cloud"),
                           (["app-server"], "app-server"), (["app-server", "proxy"], "app-server"),
                           (["app-server", "--cloud-skills-snapshot", "generate-ts"], "app-server"),
                           (["exec-server"], "exec-server"), (["remote-control"], "remote-control"),
                           (["cloud", "exec", "--env", "x", "help"], "cloud"),
                           (["debug", "app-server", "send-message-v2", "hi"], "debug app-server")):
            with self.subTest(args=args):
                # Refused as a whole (designer's call 4), also where a subcommand runs no model work itself (cloud
                # list); app-server's schema generators alone launch plain.
                self.refused(["A", *args], f"codex {name} can run model work where the usage reserve guard's hooks "
                                           "cannot see or stop it")
        # Git always passes an OPERATION; without one as its first word, a Codex before 0.169.0 takes git-credential as
        # its root prompt: the TUI's, or one dropped for a subcommand after it (exec), which then runs.
        for args in (["git-credential"], ["git-credential", "exec", "go"]):
            with self.subTest(args=args):
                self.refused(["A", *args], "codex git-credential with no OPERATION (get, store, erase or capability) as "
                                           "its first word is a prompt to a Codex before 0.169.0, which has no "
                                           "git-credential command, and would run model work without the usage reserve "
                                           "guard's hooks")
        # Signing the account out is refused in every launch, as logout is, before the form counts.
        result = self.run_codex("--reserve-weekly", "10", "A", "app-server", "--startup-sign-out", "--print-startup-config")
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", "codex-as: unsupported option; app-server --startup-sign-out signs the account out and "
                                 "revokes its tokens, as logout does.\n"))
        self.assertFalse(self.record.exists(), "a refused launch started Codex")

    def test_options_the_hooks_cannot_cover_are_refused(self):
        self.trusted()
        # The session's cwd is a worktree under CODEX_HOME (measured with codex-cli 0.169.0). The launch checks read
        # the launch directory's project layers, and the check after a headless run, which finds its new session by
        # its directory, would not see it there (a TUI's sessions under CODEX_HOME/worktrees are followed).
        worktree = ("--worktree runs the session in a Git worktree that Codex creates under its home, whose project "
                    "configuration the launch checks do not read, and where the check after a headless run does not "
                    "look for the session to tell whether Codex ran the usage reserve guard's hooks")
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
        for name in ("config.toml", "managed_config.toml", "requirements.toml"):
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

    def old_rollout(self, thread, seconds, source="cli", cwd=None, lines=(ANSWER,), **meta):
        """A rollout of this account's THREAD that started SECONDS ago (in CWD, by default the work
        directory), as Codex left it then: LINES after its session_meta line, whose fields META replaces."""
        then = time.time() - seconds
        day = self.root / "sessions" / time.strftime("%Y/%m/%d", time.localtime(then))
        day.mkdir(parents=True, exist_ok=True)
        path = day / ("rollout-" + time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(then)) + f"-{thread}.jsonl")
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(then))
        meta = {"id": thread, "session_id": thread, "timestamp": stamp, "cwd": str(cwd or self.cwd), "originator": "codex-tui",
                "source": source, "thread_source": "user", "creator_user_id": USER, "creator_account_id": WORKSPACE, **meta}
        path.write_text("".join(json.dumps({"timestamp": stamp, **line}) + "\n"
                                for line in ({"type": "session_meta", "payload": meta}, *lines)))
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
        """The account record gets the run's snapshot also from a session it resumed by --last whose rollout lies in an
        older date directory, through the turn the prompt hook named there; the same run in which Codex ran no hook
        records nothing, and the check after the run warns."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        record = self.state / "accounts/codex-A.json"
        for hooks in (("prompt",), ()):
            with self.subTest(hooks=hooks):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                record.unlink(missing_ok=True)
                self.trusted()
                older = self.old_rollout(THREAD, 2 * 86400)
                lines = literal_turn(TURN_1, THREAD, 30.0, calls=0, hooks=hooks, resumed=True, cwd=self.cwd)
                result = self.run_codex("--reserve-5h", "10", "A", "resume", "--last",
                                        env={"FAKE_ROLLOUTS": json.dumps([{"append": str(older), "lines": lines}])})
                if hooks:
                    self.assertEqual((result.returncode, result.stderr), (0, launch_line))
                    self.assertEqual(self.recorded()["turns"], f"o {TAG}\nt {TURN_1} {self.rel(older)}\n")
                    usage = json.loads(record.read_text())["usage"]
                    self.assertEqual((usage["primary"]["used_percent"], usage["primary"]["window_minutes"]), (30.0, 300))
                else:
                    self.assertEqual((result.returncode, result.stderr),
                                     (0, launch_line + WARNING.format("the guard's hooks")))
                    self.assertEqual(self.recorded()["turns"], f"o {TAG}\n")
                    self.assertEqual(json.loads(record.read_text()), {"email": "alice@example.com", "plan": "plus"})

    def test_the_record_never_takes_what_another_account_left_in_a_resumed_session(self):
        """The snapshot that the account which ran a session before left in a sub-agent's rollout of it, a minute
        before this run resumed the session by id, is that account's reading, although the rollout's file time lies
        within what a file server's clock may lag: after a run that got no reply, the account record's usage stays
        absent, and an older reading of this account's stays as it is. A run whose hook names no turn reads that
        rollout, which another account created, not at all; one whose hook names a turn there takes that turn's
        snapshot."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        record = self.state / "accounts/codex-A.json"
        spawned = {"subagent": {"thread_spawn": {"parent_thread_id": THREAD, "depth": 1}}}
        self.old_rollout(THREAD, 120, creator_user_id="user-fixture-B")
        self.old_rollout(OTHER, 60, source=spawned, lines=[ANSWER, snapshot(90.0)], session_id=THREAD,
                         thread_source="subagent", creator_user_id="user-fixture-B")
        result = self.run_codex("--reserve-5h", "10", "A", "resume", THREAD)
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        stored = json.loads(record.read_text())
        self.assertEqual(stored, {"email": "alice@example.com", "plan": "plus"})
        kept = {"primary": {"used_percent": 20.0, "window_minutes": 300, "resets_at": int(time.time()) + 7200},
                "secondary": None, "seen_at": int(time.time()) - 3600}
        record.write_text(json.dumps({**stored, "usage": kept}))
        result = self.run_codex("--reserve-5h", "10", "A", "resume", THREAD)
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        self.assertEqual(json.loads(record.read_text())["usage"], kept)
        # A prompt hook whose event names no turn: the scan reads the resumed thread and this account's rollouts.
        [sub] = (self.root / "sessions").glob(f"*/*/*/rollout-*-{OTHER}.jsonl")
        rollouts = [{"append": str(sub), "lines": [ANSWER, snapshot(32.0)]}]
        result = self.run_codex("--reserve-5h", "10", "A", "resume", THREAD,
                                env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": '["prompt"]'})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        self.assertEqual(json.loads(record.read_text())["usage"], kept)
        # What the run itself gets in a turn of that sub-agent's rollout that its hook named is this account's.
        rollouts = [{"append": str(sub), "lines": [settings_applied(OTHER), started(TURN_1), context(TURN_1), PROMPT_STEP,
                                                   ANSWER, usage_record(TURN_1, OTHER, THREAD), snapshot(31.0),
                                                   completed(TURN_1)]}]
        result = self.run_codex("--reserve-5h", "10", "A", "resume", THREAD, env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        self.assertEqual(json.loads(record.read_text())["usage"]["primary"]["used_percent"], 31.0)
        self.assertEqual(self.recorded()["turns"], f"o {TAG}\nt {TURN_1} {self.rel(sub)}\n")

    def test_a_thread_of_another_creator_feeds_the_account_that_ran_it(self):
        """After a guarded run whose hooks named turns, the record and the attested usage come from those turns,
        whoever created the thread: here one another account created, resumed by id or by --last (which names no
        thread), with the prompt and tool hooks, or with a prompt only. The reading carries the time of its reply,
        and the attested usage names the login's tag, the rollout and the launch."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        record = self.state / "accounts/codex-A.json"
        for form in (["resume", THREAD], ["resume", "--last"]):
            for calls, hooks in ((2, ("prompt", "tool")), (0, ("prompt",))):
                with self.subTest(form=form, hooks=hooks):
                    if (self.root / "sessions").exists():
                        shutil.rmtree(self.root / "sessions")
                    record.unlink(missing_ok=True)
                    (self.guard_dir / "codex-usage-A.json").unlink(missing_ok=True)
                    path = self.old_rollout(THREAD, 600, creator_user_id="user-fixture-B",
                                            creator_account_id="workspace-fixture-B")
                    used = [31.0, 32.0, 33.0][-calls - 1:]
                    lines = aged(time.time() - 3, literal_turn(TURN_1, THREAD, used, calls=calls, hooks=hooks, resumed=True,
                                                               cwd=self.cwd))
                    result = self.run_codex("--reserve-5h", "10", "A", *form,
                                            env={"FAKE_ROLLOUTS": json.dumps([{"append": str(path), "lines": lines}])})
                    self.assertEqual((result.returncode, result.stderr), (0, launch_line))
                    self.assertEqual([hook[:2] for hook in self.recorded()["hooks"]],
                                     [["prompt", 0], *[["tool", 0]] * (calls if "tool" in hooks else 0)])
                    self.assertEqual(self.recorded()["turns"], f"o {TAG}\nt {TURN_1} {self.rel(path)}\n")
                    usage = json.loads(record.read_text())["usage"]
                    self.assertEqual((usage["primary"]["used_percent"], usage["seen_at"]), (33.0, reply_time(path, TURN_1)))
                    self.assertEqual(self.attested(), {"owner": TAG, "usage": usage, "rollout": self.rel(path),
                                                       "launch": self.launch_name()})

    def test_a_thread_another_account_continues_never_feeds_its_creator(self):
        """A guarded launch of A whose hooks ran, in a thread A created that another account's client continues
        before and after A's turn in it: the guard reads A's turn alone (the other account's 90 percent a moment
        before the prompt would refuse it at a five-hour reserve of 10), and the record and the attested usage get
        A's own reading, never the other account's newer one. The same in the shape of the evidence: A runs a session
        of its own while the other account continues, during A's run, an earlier session that A created; the other
        account's newer reading there reaches neither A's record nor its attested usage. A run of A in which Codex ran
        no hook records nothing; one whose hooks ran with events that name no turn is scanned as by every earlier
        release and takes it, the known gap of runs whose turns cannot be read."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        record = self.state / "accounts/codex-A.json"
        path = self.old_rollout(THREAD, 600)
        theirs = lambda path, thread, turn, used: {
            "append": str(path), "lines": literal_turn(turn, thread, used, hooks=(), resumed=True)}
        own = aged(time.time() - 3, literal_turn(TURN_1, THREAD, [28.0, 29.0, 30.0], resumed=True, cwd=self.cwd))
        rollouts = [theirs(path, THREAD, TURN_2, 90.0), {"append": str(path), "lines": own}, theirs(path, THREAD, TURN_3, 95.0)]
        result = self.run_codex("--reserve-5h", "10", "A", "resume", THREAD, env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        self.assertEqual([hook[:2] for hook in self.recorded()["hooks"]], [["prompt", 0], ["tool", 0], ["tool", 0]])
        usage = json.loads(record.read_text())["usage"]
        self.assertEqual((usage["primary"]["used_percent"], usage["seen_at"]), (30.0, reply_time(path, TURN_1)))
        self.assertEqual(self.attested()["usage"], usage)
        shutil.rmtree(self.root / "sessions")
        record.unlink()
        (self.guard_dir / "codex-usage-A.json").unlink()
        older = self.old_rollout(OTHER, 600)
        moment = time.time() - 3
        for hooks, events in ((("prompt", "tool"), []), ((), []), ((), ["prompt"])):
            with self.subTest(hooks=hooks, events=events):
                # Without hook steps A's lines are stamped before the run, so the scan finds only the other account's.
                lines = literal_turn(TURN_1, THREAD, [36.0, 37.0, 38.0], hooks=hooks, cwd=self.cwd)
                lines = aged(moment, lines) if hooks else at(moment, lines)
                rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": lines},
                            theirs(older, OTHER, TURN_2, 90.0)]
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go",
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": json.dumps(events)})
                self.assertEqual(result.returncode, 0, result.stderr)
                [path] = (self.root / "sessions").glob(f"*/*/*/rollout-*-{THREAD}.jsonl")
                usage = json.loads(record.read_text()).get("usage")
                if hooks:
                    self.assertEqual(result.stderr, launch_line)
                    self.assertEqual((usage["primary"]["used_percent"], usage["seen_at"]), (38.0, reply_time(path, TURN_1)))
                    self.assertEqual(self.attested()["usage"], usage)
                else:
                    self.assertEqual(self.recorded()["turns"], f"o {TAG}\n")
                    self.assertEqual(usage and usage["primary"]["used_percent"], 90.0 if events else None)
                path.unlink()
                record.unlink()

    def test_a_resume_without_a_reply_records_nothing(self):
        """The shape of the false block: a session another account created, whose sub-agent's rollout names it as
        session_id and holds that account's snapshots, from a minute before this run and from during it, resumed by
        id by a run that sends no prompt. No hook runs, so nothing is recorded: the record's usage stays absent and
        nothing is attested (the check after the run reports the hooks the sub-agent's reply went without). With a
        prompt that is interrupted before its reply, the hook names this run's turn, and what the other account wrote
        in the session and in the sub-agent's rollout as this launch began, in the launch's own second and after the
        moment Codex started, lies in turns that no hook of this launch named: the prompt is not judged on it (its 92
        percent would refuse it at a five-hour reserve of 10, as the lines since the launch started show), and
        nothing of it is recorded or attested."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        record = self.state / "accounts/codex-A.json"
        theirs = {"creator_user_id": "user-fixture-B", "creator_account_id": "workspace-fixture-B"}
        spawned = {"subagent": {"thread_spawn": {"parent_thread_id": THREAD, "depth": 1}}}
        main = self.old_rollout(THREAD, 120, **theirs)
        sub = self.old_rollout(OTHER, 60, source=spawned, lines=[ANSWER, snapshot(90.0)], session_id=THREAD,
                               thread_source="subagent", **theirs)
        rollouts = [{"append": str(sub), "lines": [ANSWER, snapshot(91.0)]}]
        result = self.run_codex("--reserve-5h", "10", "A", "resume", THREAD, env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line + WARNING.format("the guard's hooks")))
        self.assertEqual(json.loads(record.read_text()), {"email": "alice@example.com", "plan": "plus"})
        self.assertEqual(self.recorded()["turns"], f"o {TAG}\n")
        self.assertIsNone(self.attested())
        mine = literal_turn(TURN_1, THREAD, None, calls=0, hooks=("prompt",), resumed=True, cwd=self.cwd)
        rollouts = [{"append": str(main), "lines": literal_turn(TURN_2, THREAD, 92.0, calls=0, hooks=(), resumed=True)},
                    {"append": str(sub), "lines": [ANSWER, snapshot(93.0)]},
                    {"append": str(main), "lines": mine[:mine.index(PROMPT_STEP) + 1] + [aborted(TURN_1)]}]
        result = self.run_codex("--reserve-5h", "10", "A", "resume", THREAD, env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        self.assertEqual([hook[:2] for hook in self.recorded()["hooks"]], [["prompt", 0]])
        started = json.loads(self.recorded()["env"]["CODING_AGENT_KIT_GUARD"])["started"]
        self.assertEqual(launch.usage.session(str(main), started)["snapshot"]["primary"]["used_percent"], 92.0)
        self.assertEqual(json.loads(record.read_text()), {"email": "alice@example.com", "plan": "plus"})
        self.assertEqual(self.recorded()["turns"], f"o {TAG}\nt {TURN_1} {self.rel(main)}\n")
        self.assertIsNone(self.attested())

    def test_the_last_reply_is_recorded_when_the_run_exits(self):
        """A turn's last token_count follows its last tool call, so no hook sees it: the guard attests what its last
        event read, and codex-as, after the run, the turn's last reply, in the record and the attested usage."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        lines = aged(time.time() - 3, literal_turn(TURN_1, THREAD, [20.0, 25.0, 40.0], cwd=self.cwd))
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={
            "FAKE_ROLLOUTS": json.dumps([{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": lines}])})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        [path] = (self.root / "sessions").glob(f"*/*/*/rollout-*-{THREAD}.jsonl")
        self.assertEqual(self.recorded()["attested"]["usage"]["primary"]["used_percent"], 20.0)
        usage = json.loads((self.state / "accounts/codex-A.json").read_text())["usage"]
        self.assertEqual((usage["primary"]["used_percent"], usage["seen_at"]), (40.0, reply_time(path, TURN_1)))
        self.assertEqual(self.attested()["usage"], usage)

    def test_a_sub_agent_s_snapshot_counts_once_its_hook_named_a_turn(self):
        """A sub-agent's rollout starts with a copy of the root's history, whose turn lines name the root's turn, and
        its tool event names its own rollout and turn with the root's session_id. Once a tool hook named the
        sub-agent's turn, its newer snapshot is the run's reading; a sub-agent turn without a hooked call is not
        read, and the root's turn decides."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        record = self.state / "accounts/codex-A.json"
        spawned = {"subagent": {"thread_spawn": {"parent_thread_id": THREAD, "depth": 1}}}
        for hooks, expected in ((("tool",), 45.0), ((), 20.0)):
            with self.subTest(hooks=hooks):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                record.unlink(missing_ok=True)
                (self.guard_dir / "codex-usage-A.json").unlink(missing_ok=True)
                early = time.time() - 3
                root = at(early, literal_turn(TURN_1, THREAD, 20.0, calls=1, cwd=self.cwd))
                own = literal_turn(TURN_2, OTHER, [35.0, 45.0], calls=1, hooks=hooks, resumed=True, root=THREAD, cwd=self.cwd)
                rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": root},
                            {"thread": OTHER, "creator": [USER, WORKSPACE], "session_id": THREAD, "parent": THREAD,
                             "source": spawned, "thread_source": "subagent", "lines": [*at(early, copied_head(TURN_1)), *own]}]
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line))
                [main] = (self.root / "sessions").glob(f"*/*/*/rollout-*-{THREAD}.jsonl")
                [sub] = (self.root / "sessions").glob(f"*/*/*/rollout-*-{OTHER}.jsonl")
                notes = {f"o {TAG}", f"t {TURN_1} {self.rel(main)}", *([f"t {TURN_2} {self.rel(sub)}"] if hooks else [])}
                self.assertEqual(set(self.recorded()["turns"].splitlines()), notes)
                usage = json.loads(record.read_text())["usage"]
                self.assertEqual(usage["primary"]["used_percent"], expected)
                self.assertEqual(self.attested()["rollout"], self.rel(sub if hooks else main))

    def test_the_first_prompt_is_checked_against_the_attested_usage(self):
        """The false block of 0.9.0: the account record names a foreign reading of a window used up, which no longer
        refuses a session's first prompt while nothing is attested, as the guard reads the attested usage instead; an
        attested reading of this login at the reserve refuses it, beside an account record of a fresh window. The
        guard neither blocks nor allows on the account record."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        record = self.state / "accounts/codex-A.json"
        self.seed(record, {"email": "alice@example.com", "plan": "plus", "usage": usage_of(100.0, int(time.time()) - 60)})
        rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE],
                     "lines": literal_turn(TURN_1, THREAD, 20.0, calls=0, hooks=("prompt",), cwd=self.cwd)}]
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        self.assertEqual([hook[:2] for hook in self.recorded()["hooks"]], [["prompt", 0]])
        self.assertIsNone(self.recorded()["attested"])
        self.seed(record, {"email": "alice@example.com", "plan": "plus", "usage": usage_of(0.0, int(time.time()))})
        self.seed(self.guard_dir / "codex-usage-A.json",
                  {"owner": TAG, "usage": usage_of(95.0, int(time.time()) - 60), "rollout": self.rel(next(
                      (self.root / "sessions").glob("*/*/*/rollout-*.jsonl"))), "launch": "codex-as-A-1-1700000000"})
        rollouts = [{"thread": OTHER, "creator": [USER, WORKSPACE], "lines": refused_turn(TURN_2, OTHER, self.cwd)}]
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        [[kind, code, out, message]] = self.recorded()["hooks"]
        self.assertEqual((kind, code, out), ("prompt", 2, ""))
        self.assertRegex(message, r"^codex-as: A: five-hour window 5% remaining, at or below its 10% reserve; resets in ")
        self.assertEqual((result.returncode, result.stderr), (0, launch_line + message))

    def test_a_run_whose_hooks_never_ran_records_nothing(self):
        """A guarded run in which Codex ran no hook leaves the notes with their owner line only: the record keeps its
        stored usage, not even the run's own snapshot replaces it, the attested usage stays byte for byte, and the
        check after the run warns."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        path = self.guard_dir / "codex-usage-A.json"
        self.seed(path, {"owner": TAG, "usage": usage_of(15.0, int(time.time()) - 60),
                         "rollout": f"2026/11/22/rollout-2026-11-22T10-00-00-{UUID}.jsonl", "launch": "codex-as-A-1-1"})
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        stored = usage_of(25.0, int(time.time()) - 3600)
        record = self.state / "accounts/codex-A.json"
        self.seed(record, {"email": "alice@example.com", "plan": "plus", "usage": stored})
        rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE],
                     "lines": literal_turn(TURN_1, THREAD, 33.0, hooks=(), cwd=self.cwd)}]
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        self.assertEqual((result.returncode, result.stderr), (0, launch_line + WARNING.format("the guard's hooks")))
        self.assertEqual(self.recorded()["turns"], f"o {TAG}\n")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(json.loads(record.read_text())["usage"], stored)

    def test_a_hook_event_without_a_turn_id_feeds_only_the_account_record(self):
        """Hook events whose turn_id is absent or no UUID ("1", a number) are read by time as 0.9.1 reads them: nothing
        is noted or attested, and the record takes the scan's snapshot."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        record = self.state / "accounts/codex-A.json"
        for turn_id in (None, "1", 7):
            with self.subTest(turn_id=turn_id):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                record.unlink(missing_ok=True)
                lines = [{**line, "turn_id": turn_id} if "hook" in line else line
                         for line in literal_turn(TURN_1, THREAD, 33.0, cwd=self.cwd)]
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={
                    "FAKE_ROLLOUTS": json.dumps([{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": lines}])})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line))
                self.assertEqual([hook[:2] for hook in self.recorded()["hooks"]], [["prompt", 0], ["tool", 0], ["tool", 0]])
                self.assertEqual(self.recorded()["turns"], f"o {TAG}\n")
                self.assertIsNone(self.attested())
                self.assertEqual(json.loads(record.read_text())["usage"]["primary"]["used_percent"], 33.0)

    def test_two_launches_of_one_account_record_the_newest(self):
        """A codex-as exec of the account that a guarded TUI runs ends first, with the newer reading; the TUI's own
        turn, whose readings are older, changes neither the attested usage nor the record when it exits."""
        self.trusted()
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        inner = self.base / "inner.json"
        nested = {"argv": [str(self.kit / "bin/codex-as"), "--reserve-5h", "10", "A", "exec", "go"],
                  "env": {"FAKE_ROLLOUTS": json.dumps([{"thread": OTHER, "creator": [USER, WORKSPACE], "lines": aged(
                      time.time() - 3, literal_turn(TURN_2, OTHER, [38.0, 39.0, 40.0], cwd=self.cwd))}]),
                          "FAKE_HOOKS": "[]", "FAKE_RECORD": str(inner)}}
        own = at(time.time() - 30, literal_turn(TURN_1, THREAD, [28.0, 29.0, 30.0], cwd=self.cwd))
        result = self.run_codex("--reserve-5h", "10", "A", env={
            "FAKE_NESTED": json.dumps(nested),
            "FAKE_ROLLOUTS": json.dumps([{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": own}])})
        self.assertEqual(self.recorded()["nested"], [0, "fake codex ran\n", launch_line])
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        self.assertEqual([hook[:2] for hook in self.recorded()["hooks"]], [["prompt", 0], ["tool", 0], ["tool", 0]])
        [path] = (self.root / "sessions").glob(f"*/*/*/rollout-*-{OTHER}.jsonl")
        usage = json.loads((self.state / "accounts/codex-A.json").read_text())["usage"]
        self.assertEqual((usage["primary"]["used_percent"], usage["seen_at"]), (40.0, reply_time(path, TURN_2)))
        inner_launch = os.path.basename(json.loads(json.loads(inner.read_text())["env"]["CODING_AGENT_KIT_GUARD"])["state"])
        self.assertEqual(self.attested(), {"owner": TAG, "usage": usage, "rollout": self.rel(path), "launch": inner_launch})

    def test_a_launch_without_reserves_touches_nothing_in_the_guard_directory(self):
        """Without a reserve no hook runs and the guard directory is neither made nor opened: an attested usage there
        stays byte for byte, and the record takes the scan's snapshot as before."""
        record = self.state / "accounts/codex-A.json"
        rollouts = lambda used: json.dumps([{"thread": THREAD, "creator": [USER, WORKSPACE],
                                             "lines": literal_turn(TURN_1, THREAD, used, cwd=self.cwd)}])
        result = self.run_codex("A", "exec", "go", env={"FAKE_ROLLOUTS": rollouts(50.0)})
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual((self.recorded()["hooks"], self.recorded()["turns"], self.recorded()["attested"]), ([], None, None))
        self.assertFalse(self.guard_dir.exists())
        self.assertEqual(json.loads(record.read_text())["usage"]["primary"]["used_percent"], 50.0)
        path = self.guard_dir / "codex-usage-A.json"
        self.seed(path, {"owner": TAG, "usage": usage_of(15.0, int(time.time()) - 60),
                         "rollout": f"2026/11/22/rollout-2026-11-22T10-00-00-{UUID}.jsonl", "launch": "codex-as-A-1-1"})
        before = (sorted(os.listdir(self.guard_dir)), path.read_bytes(), path.stat().st_mtime_ns)
        for number, flags in enumerate(((), ("--reserve-5h", "off", "--reserve-weekly", "off"))):
            with self.subTest(flags=flags):
                record.unlink()
                result = self.run_codex(*flags, "A", "exec", "go", env={"FAKE_ROLLOUTS": rollouts(51.0 + number)})
                self.assertEqual((result.returncode, result.stderr), (0, ""))
                self.assertEqual(json.loads(record.read_text())["usage"]["primary"]["used_percent"], 51.0 + number)
                self.assertEqual((sorted(os.listdir(self.guard_dir)), path.read_bytes(), path.stat().st_mtime_ns), before)

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
        """A second codex-as launch of the account that can write sessions, with reserves or without, and that starts
        and ends inside a TUI's run (a second terminal tab: here, in another repository, or continuing an older
        session elsewhere) holds the lease only meanwhile, where neither of the run's lease checks sees it; its
        arrival in the lease file makes the run not alone. What its sessions show gets the notice, never the
        warning: the marker stays and the next headless run runs. That includes a second guarded TUI in another
        repository whose tool hook ran, beside this TUI that only chatted: the check cannot tell whose hooks ran."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        elsewhere, repository = self.base / "elsewhere", self.base / "other repo"
        elsewhere.mkdir()
        repository.mkdir()
        codex_as, inner = str(self.kit / "bin/codex-as"), self.base / "inner.json"
        guarded, unguarded = ["--reserve-5h", "10"], ["--reserve-5h", "off", "--reserve-weekly", "off"]
        tab = lambda cwd=self.cwd: [{"thread": UUID, "creator": [USER, WORKSPACE], "cwd": str(cwd),
                                     "lines": [ANSWER, CALL, snapshot(20.0)]}]
        chat = [ANSWER, snapshot(20.0)]
        cases = (  # this TUI's own lines and hooks, the second launch's flags, arguments, sessions and hooks, what is missed
            ([], [], guarded, [], tab, ["prompt", "tool"], "the guard's hooks"),
            (chat, ["prompt"], guarded, [], tab, ["prompt", "tool"], "the guard's tool hook"),
            (chat, ["prompt"], guarded, ["-C", str(repository)], lambda: tab(repository), ["prompt", "tool"],
             "the guard's tool hook"),
            ([], [], guarded, ["-C", str(elsewhere), "exec", "resume", "--last", "go"],
             lambda: [{"append": str(self.old_rollout(OTHER, 86400, cwd=elsewhere)),
                       "lines": [turn(elsewhere), ANSWER, CALL, snapshot(20.0)]}], ["prompt", "tool"], "the guard's hooks"),
            # A tab without reserves runs no hook at all.
            ([], [], unguarded, [], tab, [], "the guard's hooks"),
            (chat, ["prompt"], unguarded, [], tab, [], "the guard's tool hook"),
        )
        for own, hooks, flags, args, rollouts, nested_hooks, missing in cases:
            with self.subTest(own=len(own), flags=flags, args=args):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                nested = {"argv": [codex_as, *flags, "A", *args],
                          "env": {"FAKE_ROLLOUTS": json.dumps(rollouts()), "FAKE_HOOKS": json.dumps(nested_hooks),
                                  "FAKE_RECORD": str(inner)}}
                own_rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": own}] if own else []
                result = self.run_codex("--reserve-5h", "10", "A", env={
                    "FAKE_NESTED": json.dumps(nested), "FAKE_ROLLOUTS": json.dumps(own_rollouts),
                    "FAKE_HOOKS": json.dumps(hooks)})
                self.assertEqual(self.recorded()["nested"],
                                 [0, "fake codex ran\n", launch_line if flags == guarded else ""])
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + NOTICE.format(missing)))
                self.assertTrue(self.marker.exists())
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
                self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(self.launches(), [])

    def test_a_plain_launch_of_the_account_during_a_run_leaves_it_alone(self):
        """A launch of a plain form writes no session and does not arrive in the lease file, with a reserve or
        without: a TUI whose own session shows that Codex did not run the guard's hooks still gets the warning,
        the marker goes and the next headless run is refused."""
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        lease, inner = self.root / "accounts/.locks/A.lock", self.base / "inner.json"
        own = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": [ANSWER, CALL, snapshot(20.0)]}]
        for flags, args in (([], ["mcp", "list"]), (["--reserve-5h", "10"], ["login", "status"]),
                            (["--reserve-5h", "10"], ["exec", "--help"])):
            with self.subTest(flags=flags, args=args):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                self.trusted()
                size = lease.stat().st_size if lease.exists() else 0
                nested = {"argv": [str(self.kit / "bin/codex-as"), *flags, "A", *args],
                          "env": {"FAKE_ROLLOUTS": "[]", "FAKE_HOOKS": "[]", "FAKE_RECORD": str(inner)}}
                result = self.run_codex("--reserve-5h", "10", "A", env={
                    "FAKE_NESTED": json.dumps(nested), "FAKE_ROLLOUTS": json.dumps(own), "FAKE_HOOKS": "[]"})
                self.assertEqual(self.recorded()["nested"], [0, "fake codex ran\n", ""])
                self.assertEqual((result.returncode, result.stderr),
                                 (0, launch_line + WARNING.format("the guard's hooks")))
                self.assertEqual(lease.stat().st_size, size + 1, "only the TUI arrived")
                self.assertFalse(self.marker.exists())
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "go")
                self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.launches(), [])

    def test_exec_resume_without_an_id_is_told_by_the_turn_it_begins_here(self):
        """exec resume --last continues the newest session whose latest turn was in its directory, and begins
        its own turn there, or starts a session there when it finds none (measured with codex-cli 0.169.0,
        RealCodex): a new session it starts is the run's (the warning, the marker removed, the next headless
        run refused), an older session counts only with a turn begun here during the run, and an older
        session of the account that another client continued elsewhere meanwhile is not the run's. Lines
        written without a turn begun may be the run's only when its own session is not found: the notice."""
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
            # Lines without a turn begun are not a turn of this run, whose own session is here.
            ([{"thread": UUID, "creator": [USER, WORKSPACE], "lines": [turn(self.cwd), ANSWER, snapshot(20.0)]}],
             [{"append": str(other), "lines": [CALL, snapshot(20.0)]}], ""),
            # Without a session of the run found, they may be its own, as with a Codex that records no turn's directory.
            ([], [{"append": str(other), "lines": [CALL, snapshot(20.0)]}], NOTICE),
        )
        for own, others, printed in cases:
            with self.subTest(own=len(own), others=len(others), printed=printed[:20]):
                self.trusted()
                result = self.run_codex("--reserve-5h", "10", "A", "exec", "resume", "--last", "go",
                                        env={"FAKE_ROLLOUTS": json.dumps([*own, *others]), "FAKE_HOOKS": '["prompt"]'})
                self.assertEqual((result.returncode, result.stderr),
                                 (0, launch_line + printed.format("the guard's tool hook")))
                self.assertEqual(self.marker.exists(), printed != WARNING)
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
        lags this host's by 30 seconds still has its lines, stamped by Codex on this host, read, and the
        account record takes its snapshot: through the scan of a run whose prompt hook ran with an event that
        names no turn (exec, the TUI), and through the turn the prompt hook named in a session that resume --last
        continued in an older date directory."""
        warning = ("codex-as: A: the usage reserve was not enforced in this run: Codex did not run the guard's tool hook "
                   "(not trusted, disabled, or managed hooks only). Start codex-as A interactively and trust them in "
                   "\"Hooks need review\".\n")
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        for number, args in enumerate((["exec", "go"], [], ["resume", "--last"])):
            with self.subTest(args=args):
                if (self.root / "sessions").exists():
                    shutil.rmtree(self.root / "sessions")
                # A snapshot replaces the record's only when newer by its own second.
                (self.state / "accounts/codex-A.json").unlink(missing_ok=True)
                self.trusted()
                if args[:1] == ["resume"]:
                    lines = literal_turn(TURN_1, THREAD, 21.0 + number, calls=1, hooks=("prompt",), resumed=True,
                                         cwd=self.cwd)
                    rollouts = [{"append": str(self.old_rollout(THREAD, 2 * 86400)), "lines": lines, "mtime": -30}]
                    events = []
                else:
                    lines = [CALL, snapshot(21.0 + number)]
                    rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": lines, "mtime": -30}]
                    events = ["prompt"]
                result = self.run_codex("--reserve-5h", "10", "A", *args,
                                        env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_HOOKS": json.dumps(events)})
                self.assertEqual((result.returncode, result.stderr), (0, launch_line + warning))
                self.assertFalse(self.marker.exists())
                usage = json.loads((self.state / "accounts/codex-A.json").read_text())["usage"]
                self.assertEqual(usage["primary"]["used_percent"], 21.0 + number)

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
                        self.assertEqual([name.rpartition(".")[2] for name in self.launches()], ["launched", "turns"])
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

    def test_the_fake_runs_each_hook_step_for_the_turn_it_stands_in(self):
        """The fixture: at a hook step the fake runs the guard's hook for the rollout it writes, with the turn of the
        last task_started line before the step and the session's id; a sub-agent's event carries the root's
        session_id beside its own thread as agent_id, as codex-cli 0.171.0 sends them. A launch without reserves
        runs no hook."""
        self.trusted()
        spawned = {"subagent": {"thread_spawn": {"parent_thread_id": THREAD, "depth": 1}}}
        rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "lines": literal_turn(TURN_1, THREAD, 20.0, calls=1)},
                    {"thread": OTHER, "creator": [USER, WORKSPACE], "session_id": THREAD, "source": spawned,
                     "thread_source": "subagent", "lines": [*copied_head(TURN_1), *literal_turn(
                         TURN_2, OTHER, 20.0, calls=1, hooks=("tool",), resumed=True, root=THREAD)]}]
        result = self.run_codex("--reserve-5h", "10", "A", "exec", "go", env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        launch_line = LAUNCH_LINE.format(policy="five-hour 10% (--reserve-5h), weekly off (default)")
        self.assertEqual((result.returncode, result.stderr), (0, launch_line))
        record = self.recorded()
        self.assertEqual([hook[:2] for hook in record["hooks"]], [["prompt", 0], ["tool", 0], ["tool", 0]])
        [main] = (self.root / "sessions").glob(f"*/*/*/rollout-*-{THREAD}.jsonl")
        [sub] = (self.root / "sessions").glob(f"*/*/*/rollout-*-{OTHER}.jsonl")
        # Codex names the rollout through its home, the account's, whose sessions directory links the shared one.
        self.assertEqual([(event["hook_event_name"], os.path.realpath(event["transcript_path"]), event["turn_id"],
                           event["session_id"], event.get("agent_id"), event.get("agent_type"))
                          for event in record["events"]],
                         [("UserPromptSubmit", str(main), TURN_1, THREAD, None, None),
                          ("PreToolUse", str(main), TURN_1, THREAD, None, None),
                          ("PreToolUse", str(sub), TURN_2, THREAD, OTHER, "default")])
        self.assertEqual(len(main.read_text().splitlines()), 1 + len(literal_turn(TURN_1, THREAD, 20.0, calls=1)) - 2)
        result = self.run_codex("A", "exec", "go", env={"FAKE_ROLLOUTS": json.dumps(rollouts[:1])})
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual((self.recorded()["hooks"], self.recorded()["events"]), ([], []))

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
PROXIES = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
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


class RecordingProxy:
    """A loopback listener to put in every proxy variable where a closed port would hide an attempt: it records
    the request line of each connection (an HTTPS client's CONNECT host:443, an empty line for a connection that
    sends none) and answers each 502 Bad Gateway, connecting nowhere."""

    def __init__(self):
        self.lines = lines = []

        class Handler(socketserver.StreamRequestHandler):
            timeout = 10

            def handle(self):
                try:
                    line = self.rfile.readline(4096)
                except OSError:
                    line = b""
                lines.append(line.decode("latin-1").rstrip("\r\n"))
                try:
                    self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.address = self.server.server_address
        self.url = "http://%s:%d" % self.address
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


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
        self.sessions = set()  # the sessions run() started: each one's leader's process id
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
                **dict.fromkeys(PROXIES, closed),
                "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                # Codex runs /usr/bin/git by its path, past the shim: git itself may use local repositories only.
                "GIT_SSH_COMMAND": str(self.shims / "ssh"), "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_ALLOW_PROTOCOL": "file", "GIT_CEILING_DIRECTORIES": str(self.base)}

    def run(self, codex, args, env, timeout=90):
        """(exit code, stdout, stderr) of CODEX ARGS in a session of its own, which is killed afterwards."""
        child = subprocess.Popen([codex, *args], cwd=self.work, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.sessions.add(child.pid)
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
        """Processes of this sandbox still running, killed; Linux only. A process of the sandbox is one in a
        session that run() started, or one of this user's whose working directory lies in the sandbox (a
        process that left the session; Codex runs in the work directory). Only /proc/PID/stat and this user's
        /proc/PID/cwd links are read: never another process's environment, command line or files."""
        if not os.path.isdir("/proc/self"):
            return []
        found, uid, base = [], os.getuid(), str(self.base)
        for entry in filter(str.isdigit, os.listdir("/proc")):
            pid = int(entry)
            try:
                with open(f"/proc/{entry}/stat", "rb") as stream:
                    # pid (comm) state ppid pgrp session ...: the fields after the command name.
                    state, _, _, session = stream.read().rpartition(b")")[2].split()[:4]
                if state == b"Z" or pid == os.getpid():
                    continue
                if int(session) in self.sessions:
                    found.append(pid)
                elif os.stat(f"/proc/{entry}").st_uid == uid:
                    cwd = os.readlink(f"/proc/{entry}/cwd")
                    if cwd == base or cwd.startswith(base + "/"):
                        found.append(pid)
            except (OSError, ValueError):
                pass
        for pid in found:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return found


# What RealCodex puts in place of guard.py: it writes down each hook event the real Codex hands it (fabricated data
# only), and what the transcript the event names holds at that moment, then runs the real guard with the same input
# and passes its answer on.
WRAPPER = r'''
import json, os, subprocess, sys
LOG, GUARD = {log!r}, {guard!r}
raw = sys.stdin.buffer.read()
entry = {{"kind": sys.argv[1]}}
try:
    event = json.loads(raw)
    entry["event"] = event
    path = event.get("transcript_path")
    if isinstance(path, str) and os.path.isfile(path):
        with open(path, "rb") as stream:
            lines = stream.read().split(b"\n")[:-1]  # complete lines only
        entry["lines"] = len(lines)
        rows = [json.loads(line) for line in lines]
        entry["started"] = [row["payload"].get("turn_id") for row in rows
                            if row.get("type") == "event_msg" and row.get("payload", {{}}).get("type") == "task_started"]
except Exception as error:
    entry["error"] = repr(error)
done = subprocess.run([sys.executable, "-I", "-B", "-S", GUARD, sys.argv[1]], input=raw, capture_output=True)
entry["answer"] = [done.returncode, done.stderr.decode("utf-8", "replace")]
with open(LOG, "a") as stream:
    stream.write(json.dumps(entry) + "\n")
sys.stdout.buffer.write(done.stdout)
sys.stderr.buffer.write(done.stderr)
sys.exit(done.returncode)
'''


def load_kit_module(kit, name):
    spec = importlib.util.spec_from_file_location(f"codex_guard_real_{name}", kit / f"lib/coding-agent-kit/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SandboxPath(unittest.TestCase):
    """The real Codex's sandbox, with fakes: what it puts on PATH, the processes it finds left running, and the
    proxy recorder."""

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

    def test_strays_are_found_by_session_and_working_directory(self):
        """Sandbox.strays() finds and kills what a run left running without reading any process's environment: a
        process in a session that run() started, with a process group of its own so the group kill misses it, and
        one that left the session with its working directory in the sandbox. A process outside the sandbox, in a
        directory whose path only begins with the sandbox's, stays."""
        if not os.path.isdir("/proc/self"):
            self.skipTest("Sandbox.strays() reads /proc, which this host does not have")
        with tempfile.TemporaryDirectory(prefix="codex-guard-sandbox-") as temp:
            base = Path(temp).resolve()
            write(base / "codex", "#!/bin/sh\n", 0o700)
            sandbox = Sandbox(base / "sandbox", base / "codex")
            beside = base / "sandbox-other"
            beside.mkdir()
            outside = subprocess.Popen(["sleep", "60"], cwd=beside, stdin=subprocess.DEVNULL, start_new_session=True)
            self.addCleanup(outside.wait)
            self.addCleanup(outside.kill)
            program = ("import subprocess, sys\n"
                       "quiet = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}\n"
                       "grouped = subprocess.Popen(['sleep', '60'], process_group=0, **quiet)\n"
                       "detached = subprocess.Popen(['sleep', '60'], start_new_session=True, **quiet)\n"
                       "print(grouped.pid, detached.pid)\n")
            code, out, err = sandbox.run(sys.executable, ["-I", "-c", program], {"PATH": "/usr/bin:/bin"})
            self.assertEqual(code, 0, err)
            expected = sorted(map(int, out.split()))
            opened, real_open = [], open

            def recording_open(file, *args, **kwargs):
                opened.append(str(file))
                return real_open(file, *args, **kwargs)

            with patch("builtins.open", recording_open):
                self.assertEqual(sorted(sandbox.strays()), expected)
            self.assertEqual([name for name in opened if not name.endswith("/stat")], [])
            deadline = time.monotonic() + 10
            while (left := sandbox.strays()) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(left, [])
            self.assertIsNone(outside.poll())

    def test_the_proxy_recorder_sees_each_attempt_and_forwards_none(self):
        """RecordingProxy, which a RealCodex case puts in every proxy variable, records the request line of each
        connection (an HTTPS client's CONNECT, a plain request's absolute URL, nothing for one that sends none)
        and answers each 502 Bad Gateway without connecting anywhere."""
        proxy = RecordingProxy()
        self.addCleanup(proxy.close)
        self.assertEqual(proxy.url, "http://127.0.0.1:%d" % proxy.address[1])
        for request in (b"CONNECT auth.openai.com:443 HTTP/1.1\r\nHost: auth.openai.com:443\r\n\r\n",
                        b"GET http://example.invalid/ HTTP/1.1\r\nHost: example.invalid\r\n\r\n", b""):
            with socket.create_connection(proxy.address, timeout=10) as stream:
                stream.sendall(request)
                stream.shutdown(socket.SHUT_WR)
                answer = stream.makefile("rb").read()
            self.assertTrue(answer.startswith(b"HTTP/1.1 502 Bad Gateway\r\n"), answer)
        self.assertEqual(proxy.lines, ["CONNECT auth.openai.com:443 HTTP/1.1", "GET http://example.invalid/ HTTP/1.1", ""])


class RealCodex(unittest.TestCase):
    """The guard's hook contract against the real Codex, offline: the two hook values and goals off as
    codex-as passes them, a launch as codex-as exports it, and a loopback fake of the Responses API that
    answers from a script and sends rate limits as the real backend does. codex exec runs directly on the
    native, with --dangerously-bypass-hook-trust standing in for the user's trust in "Hooks need review";
    the trust prompt and the terminal session are left to the live acceptance. One case runs codex-as itself,
    from the kit copy with a fictitious login, for forms it launches plain. Codex reads its own install's files
    where they lie (Sandbox), nothing else of the user's."""

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
        self.attested = self.guard_dir / "codex-usage-A.json"
        self.marker = self.guard_dir / f"codex-hooks-{FINGERPRINT}"
        self.calls = base / "shell calls"
        self.requests = base / "requests.jsonl"
        self.hook_log = base / "hook events.jsonl"

    def run_codex(self, prompt, recorded=None, sent=None, options=(), tail=(), root_options=(), started=None,
                  account="A", tag=TAG):
        """codex ROOT_OPTIONS exec OPTIONS --skip-git-repo-check TAIL PROMPT under the guard with the five-hour
        reserve at 10, as a launch of ACCOUNT by the login TAG whose notes of its turns codex-as made: RECORDED
        percent of the five-hour window used in account A's attested usage of earlier runs, SENT percent in the rate
        limits the fake sends with every answer (None: no attested usage, no rate limits). STARTED is the launch's
        start in epoch seconds, now by default. The guard runs behind WRAPPER (events()); the requests returned are
        this run's."""
        now = int(time.time())
        seen = len(self.requests.read_text().splitlines()) if self.requests.exists() else 0
        self.serve({} if sent is None else {"primary": [sent, 300, 3600], "secondary": [20.0, 10080, 86400]})
        if recorded is not None:
            window = {"used_percent": float(recorded), "window_minutes": 300, "resets_at": now + 3600}
            load_kit_module(self.kit, "accounts").write(self.attested, {
                "owner": TAG, "usage": {"primary": window, "secondary": None, "seen_at": now - 60},
                "rollout": f"2026/11/22/rollout-2026-11-22T10-00-00-{UUID}.jsonl", "launch": "codex-as-A-1-1700000000"})
        guard_launch = load_kit_module(self.kit, "guard_launch")
        policy = self.reserve.Policy("codex-as", account,
                                     self.reserve.Setting("five_hour", self.reserve.flag_value("10"), "invocation"),
                                     self.reserve.Setting("weekly", None, "default"))
        env = self.sandbox.env()
        state = guard_launch.prepare(env, str(self.guard_dir), "codex-as", account, policy,
                                     str(self.accounts / f"codex-{account}.json"),
                                     int(time.time()) if started is None else started, marker=str(self.marker), turns=tag)
        self.assertEqual(env["CODING_AGENT_KIT_GUARD_SCRIPT"], str(self.kit / "lib/coding-agent-kit/guard.py"))
        wrapper = self.sandbox.base / "guard wrapper.py"
        wrapper.write_text(WRAPPER.format(log=str(self.hook_log), guard=env["CODING_AGENT_KIT_GUARD_SCRIPT"]))
        env["CODING_AGENT_KIT_GUARD_SCRIPT"] = str(wrapper)
        # The values codex-as passes before the caller's arguments, and the stand-in for the user's trust.
        args = [*(word for value in launch.GUARD_HOOKS for word in ("-c", value)), *launch.GOALS_OFF, *root_options,
                "--dangerously-bypass-hook-trust", "exec", *options, "--skip-git-repo-check", *tail, prompt]
        code, out, err = self.sandbox.run(self.codex, args, env)
        self.assertEqual(self.sandbox.strays(), [], "Codex left a process behind")
        refused = [line for line in self.sandbox.netlog.read_text().splitlines() if line.startswith("REFUSED ssh")]
        self.assertEqual(refused, [], "Codex tried to reach a host through ssh")
        lines = self.requests.read_text().splitlines()[seen:] if self.requests.exists() else []
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

    def events(self):
        """What WRAPPER wrote down of each hook event, oldest first."""
        return [json.loads(line) for line in self.hook_log.read_text().splitlines()] if self.hook_log.exists() else []

    def rollouts(self):
        """{path: [rows]} of every rollout in the sandbox's Codex home."""
        found = {}
        for path in (self.sandbox.codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"):
            found[path] = [json.loads(line) for line in path.read_text().splitlines()]
        return found

    def creator(self, path, user, workspace):
        """Name the ChatGPT user USER in WORKSPACE as the creator in the session_meta line of the rollout at PATH: the
        sandbox holds no credential, so Codex names none, and the scan of every earlier release goes by the creator."""
        rows = path.read_text().splitlines(keepends=True)
        line = json.loads(rows[0])
        self.assertEqual(line["type"], "session_meta")
        line["payload"].update(creator_user_id=user, creator_account_id=workspace)
        path.write_text(json.dumps(line) + "\n" + "".join(rows[1:]))

    def remember(self, state, begun, args):
        """launch.remember() as codex-as A ARGS runs it after Codex exits: the login of USER in WORKSPACE, the
        sandbox's Codex home, the launch state STATE in the guard directory and the moment BEGUN before Codex started;
        the account record then."""
        env = {name: value for name, value in os.environ.items() if name not in KIT_KNOBS}
        env.update(HOME=str(self.sandbox.home), XDG_STATE_HOME=str(self.sandbox.base / "state"),
                   XDG_CONFIG_HOME=str(self.sandbox.base / "xdg/config"))
        with patch.dict(os.environ, env, clear=True):
            config = settings.load()
        self.assertEqual((config.codex_home, config.state_dir), (self.sandbox.codex_home, self.accounts.parent))
        launch.remember(config, layout.Layout(check, "A", config), login("A", USER, WORKSPACE),
                        check.arguments(list(args), config), begun, state=state)
        return json.loads(self.record.read_text())

    def noted(self, state, tag=TAG):
        """[(turn, rollout path)] of the notes of the launch with STATE, below their owner line, which must be TAG."""
        owner, *notes = Path(state + ".turns").read_text().splitlines()
        self.assertEqual(owner, f"o {tag}")
        return [(note.split(" ")[1], self.sandbox.codex_home / "sessions" / note.split(" ")[2]) for note in notes]

    def test_a_session_another_login_ran_gives_this_login_nothing(self):
        """Path A of the false block, against the real Codex: login B's launch runs a session whose sub-agent gets
        B's readings (90 percent of the five-hour window); then login A's launch, whose start lies before all of that,
        resumes the session by id with a prompt whose reply carries no rate limits. The lines since A's launch
        started, in the session and in its sub-agent's rollout, hold B's reading, which would refuse A's prompt at a
        five-hour reserve of 10. A's prompt is judged by its own turn and sent, and after the run nothing of B's
        reaches A's record or attested usage."""
        if "multi_agent_v2" not in self.features:
            self.skipTest("this Codex has no multi_agent_v2 feature, which offers collaboration.spawn_agent")
        earlier = int(time.time()) - 1
        code, out, err, theirs, _ = self.run_codex("SPAWN a helper", sent=90, root_options=("--enable", "multi_agent_v2"),
                                                   account="B", tag=TAG_B)
        self.assertEqual((code, out.strip()), (0, "SPAWN-DONE"), err)
        found = self.rollouts()
        [root] = [path for path, rows in found.items() if rows[0]["payload"].get("thread_source") == "user"]
        [sub] = [path for path, rows in found.items() if rows[0]["payload"].get("thread_source") == "subagent"]
        thread = found[root][0]["payload"]["id"]
        begun = time.time()
        code, out, err, state, models = self.run_codex("PLAIN go on", tail=("resume", thread), started=earlier)
        self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
        self.assertEqual([entry["script"] for entry in models], ["PLAIN"])
        self.assertFalse(os.path.exists(state + ".blocked"))
        for path in (root, sub):
            self.creator(path, "user-fixture-B", "workspace-fixture-B")
            self.assertEqual(launch.usage.session(str(path), earlier)["snapshot"]["primary"]["used_percent"], 90.0)
        [(turn, path)] = self.noted(state)
        self.assertEqual(path, root)
        self.assertNotIn(turn, {note[0] for note in self.noted(theirs, TAG_B)})
        record = self.remember(state, begun, ["exec", "--skip-git-repo-check", "resume", thread, "PLAIN go on"])
        self.assertEqual(record, {"email": "alice@example.com", "plan": "plus"})
        self.assertFalse(self.attested.exists())

    def test_only_this_login_s_turns_feed_its_run_whoever_created_the_thread(self):
        """Path B, against the real Codex: login A's launch continues a thread that login B created, and B continues a
        thread that A created, after A's turn and before A's run is recorded. A's record and attested usage take A's
        own reply in B's thread (40 percent of the five-hour window), with the time of that reply, and never B's
        newer reading in A's thread (90 percent), which the scan of a run whose hooks named no turn takes as A's,
        since A created that thread."""
        def created(prompt, **launch):
            before = set(self.rollouts())
            code, out, err, state, _ = self.run_codex(prompt, **launch)
            self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
            [path] = set(self.rollouts()) - before
            return path
        mine = created("PLAIN one", sent=30)
        yours = created("PLAIN two", sent=70, account="B", tag=TAG_B)
        thread = self.rollouts()[yours][0]["payload"]["id"]
        begun = time.time()
        code, out, err, state, _ = self.run_codex("PLAIN three", sent=40, tail=("resume", thread))
        self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
        code, out, err, _, _ = self.run_codex("PLAIN four", sent=90, tail=("resume", self.rollouts()[mine][0]["payload"]["id"]),
                                              account="B", tag=TAG_B)
        self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
        self.creator(mine, USER, WORKSPACE)
        self.creator(yours, "user-fixture-B", "workspace-fixture-B")
        [(turn, path)] = self.noted(state)
        self.assertEqual(path, yours)
        record = self.remember(state, begun, ["exec", "--skip-git-repo-check", "resume", thread, "PLAIN three"])
        reading = record["usage"]
        self.assertEqual((reading["primary"]["used_percent"], reading["primary"]["window_minutes"],
                          reading["secondary"]["used_percent"]), (40.0, 300, 20.0))
        replies = [row["timestamp"] for row in self.rollouts()[yours]
                   if row["type"] == "token_usage_record" and row["payload"]["turn_id"] == turn]
        self.assertEqual(reading["seen_at"], int(launch.usage.moment(replies[-1])))
        self.assertEqual(json.loads(self.attested.read_text()), {"owner": TAG, "usage": reading,
                                                                 "rollout": launch.usage.relative(yours),
                                                                 "launch": os.path.basename(state)})
        scanned = launch.usage.latest(self.sandbox.codex_home, set(), (USER, WORKSPACE), begun - launch.STAMPED,
                                      time.time(), skew=launch.SKEW)
        self.assertEqual(scanned["primary"]["used_percent"], 90.0)

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

    def test_app_server_s_schema_generators_launch_plain_with_a_reserve(self):
        """codex-as with a reserve on launches app-server generate-ts, generate-json-schema and
        generate-internal-json-schema, with their options, as with --reserve-5h off: no launch line and no guard,
        and the real Codex writes the same files, starts nothing that outlives it, starts no session and tries no
        host through a proxy (every proxy is a RecordingProxy), git or ssh; no model provider is configured."""
        shutil.copytree(ROOT / "bin", self.kit / "bin")
        home = self.sandbox.home
        home.chmod(0o700)
        (home / ".secrets").mkdir(mode=0o700)
        claims = {"sub": "fixture-A", "email": "alice@example.com",
                  "https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_user_id": USER}}
        write(home / ".secrets/codex-A.json", json.dumps({
            "auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-10-01T00:00:00Z",
            "tokens": {"id_token": id_token(claims), "access_token": "fabricated-sensitive-access",
                       "refresh_token": "fabricated-sensitive-refresh", "account_id": WORKSPACE}}))
        proxy = RecordingProxy()
        self.addCleanup(proxy.close)
        env = self.sandbox.env() | dict.fromkeys(PROXIES, proxy.url)
        del env["CODEX_HOME"]  # codex-as sets it to the account's home
        out = self.sandbox.base / "out"
        for args in (["generate-ts", "-o", str(out)], ["generate-ts", "--out", str(out), "--experimental"],
                     ["generate-json-schema", "--out", str(out), "--experimental"],
                     ["generate-internal-json-schema", "-o", str(out)]):
            with self.subTest(args=args):
                runs = []
                for reserve in ("off", "10"):
                    shutil.rmtree(out, ignore_errors=True)
                    code, stdout, stderr = self.sandbox.run(str(self.kit / "bin/codex-as"),
                                                            ["--reserve-5h", reserve, "A", "app-server", *args], env)
                    files = sorted((str(path.relative_to(out)), hashlib.sha256(path.read_bytes()).hexdigest())
                                   for path in out.rglob("*") if path.is_file()) if out.is_dir() else []
                    runs.append((code, stdout, stderr, files))
                self.assertEqual(runs[0][0], 0, runs[0][2])
                self.assertTrue(runs[0][3], "Codex wrote no file")
                self.assertEqual(runs[1], runs[0])
        # strays() finds what the runs left by the session of each and by a working directory in the sandbox.
        self.assertEqual(self.sandbox.strays(), [], "Codex left a process behind")
        self.assertEqual(list(home.rglob("rollout-*.jsonl*")), [], "a schema generator started a session")
        self.assertEqual(proxy.lines, [], "Codex tried to reach a host through a proxy")
        self.assertEqual([line for line in self.sandbox.netlog.read_text().splitlines()
                          if line.startswith("REFUSED")], [], "Codex tried to reach a host through git or ssh")
        self.assertFalse((self.sandbox.base / "xdg/state/coding-agent-kit/guard").exists(), "the launch was guarded")

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

    def test_the_guard_notes_the_turn_and_records_the_run_s_snapshots(self):
        """After a run with two tool calls the launch's notes name its one turn in the rollout the events named, and
        the attested usage holds what the second call's tool hook read in that turn (the backend's reading, here at
        the reserve, which stops that call), with the login's tag and the time of a token_usage_record line of that
        turn. A second launch, whose start lies before the first launch's, resumes the thread with exec resume --last
        while nothing is attested: its prompt is judged by its own turn, which holds no reading yet, and never by
        the first launch's turn, so it is sent."""
        earlier = int(time.time()) - 5
        code, out, err, state, models = self.run_codex("TOOL run the command", sent=95)
        self.assertEqual((code, out.strip()), (0, "TOOL-DONE"), err)
        self.assertEqual(self.shell_calls(), 1)
        events = self.events()
        [turn] = {entry["event"]["turn_id"] for entry in events}
        [transcript] = {entry["event"]["transcript_path"] for entry in events}
        rel = launch.usage.relative(transcript)
        self.assertEqual(rel, "/".join(Path(transcript).parts[-4:]))
        self.assertEqual(Path(state + ".turns").read_text(), f"o {TAG}\nt {turn} {rel}\n")
        rows = [json.loads(line) for line in Path(transcript).read_text().splitlines()]
        replies = {int(launch.usage.moment(row["timestamp"])) for row in rows
                   if row["type"] == "token_usage_record" and row["payload"]["turn_id"] == turn}
        attested = json.loads(self.attested.read_text())
        self.assertEqual((attested["owner"], attested["rollout"], attested["launch"]), (TAG, rel, os.path.basename(state)))
        reading = attested["usage"]
        self.assertEqual((reading["primary"]["used_percent"], reading["primary"]["window_minutes"],
                          reading["secondary"]["used_percent"], reading["secondary"]["window_minutes"]),
                         (95.0, 300, 20.0, 10080))
        self.assertIn(reading["seen_at"], replies)
        os.replace(self.attested, self.attested.with_name("moved away.json"))
        code, out, err, second, models = self.run_codex("PLAIN again", sent=40, tail=("resume", "--last"), started=earlier)
        self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
        self.assertEqual([entry["script"] for entry in models], ["PLAIN"])
        self.assertFalse(os.path.exists(second + ".blocked"))
        [prompt] = [entry for entry in self.events() if entry["event"]["turn_id"] != turn]
        self.assertEqual(Path(second + ".turns").read_text(), f"o {TAG}\nt {prompt['event']['turn_id']} {rel}\n")

    def turns(self, rows):
        """{turn: [index of each row of that turn]}: a turn runs from its task_started to its task_complete or
        turn_aborted (or the next task_started)."""
        found, current = {}, None
        for index, row in enumerate(rows):
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if row["type"] == "event_msg" and payload.get("type") == "task_started":
                current = payload.get("turn_id")
            if current is not None:
                found.setdefault(current, []).append(index)
            if row["type"] == "event_msg" and payload.get("type") in ("task_complete", "turn_aborted"):
                current = None
        return found

    def test_the_hook_events_name_the_turn_and_an_existing_transcript(self):
        """Each prompt and tool event names its turn (a UUID), the session and the rollout it writes, which lies
        below sessions/ in a day directory; when the hook runs, that rollout already holds the turn's task_started
        line; and task_started (also in turn_attribution), turn_context, token_usage_record and task_complete
        carry the same id. For a root session session_id is its thread. The guard tells the launch's turns by
        these facts."""
        code, out, err, state, models = self.run_codex("TOOL run the command", sent=50)
        self.assertEqual((code, out.strip()), (0, "TOOL-DONE"), err)
        events = self.events()
        self.assertEqual([entry["kind"] for entry in events], ["prompt", "tool", "tool"])
        [(path, rows)] = self.rollouts().items()
        thread = rows[0]["payload"]["id"]
        common = {"session_id", "turn_id", "transcript_path", "cwd", "hook_event_name", "model", "permission_mode"}
        for entry in events:
            with self.subTest(kind=entry["kind"]):
                event = entry["event"]
                extra = {"prompt"} if entry["kind"] == "prompt" else {"tool_name", "tool_input", "tool_use_id"}
                self.assertLessEqual(common | extra, set(event))
                self.assertNotIn("agent_id", event)
                self.assertRegex(event["turn_id"], f"^{TURN_ID.pattern}$")
                self.assertEqual((event["transcript_path"], event["session_id"]), (str(path), thread))
                self.assertEqual(entry["started"][-1], event["turn_id"], "the turn's task_started was not written yet")
                parts = Path(event["transcript_path"]).parts
                self.assertEqual(parts[-5], "sessions")
                self.assertRegex("/".join(parts[-4:]), f"^{REL_PATH.pattern}$")
        [turn] = {entry["event"]["turn_id"] for entry in events}
        self.assertEqual(list(self.turns(rows)), [turn])
        named = [row for row in rows if row["type"] in ("turn_context", "token_usage_record") or row["type"] == "event_msg"
                 and row["payload"].get("type") in ("task_started", "task_complete", "turn_aborted")]
        self.assertEqual([row["payload"].get("turn_id") for row in named], [turn] * len(named))
        self.assertEqual([row["payload"]["turn_attribution"]["turn_id"] for row in named
                          if row["payload"].get("type") == "task_started"], [turn])
        self.assertEqual(sorted({row["type"] for row in named}), ["event_msg", "token_usage_record", "turn_context"])

    def test_the_last_token_count_precedes_the_turn_s_end(self):
        """A token_count line carries no turn id: it stands inside its turn, after a token_usage_record of that
        turn, and the turn's last one comes right before its task_complete; none stands between two turns, also
        when a second launch resumes the thread. So the turn a snapshot belongs to is told by its place."""
        code, out, err, *_ = self.run_codex("TOOL run the command", sent=50)
        self.assertEqual((code, out.strip()), (0, "TOOL-DONE"), err)
        code, out, err, *_ = self.run_codex("PLAIN again", sent=40, tail=("resume", "--last"))
        self.assertEqual((code, out.strip()), (0, "PLAIN-ANSWER"), err)
        [(path, rows)] = self.rollouts().items()
        turns = self.turns(rows)
        self.assertEqual(len(turns), 2)
        inside = {index for indexes in turns.values() for index in indexes}
        counts = [index for index, row in enumerate(rows)
                  if row["type"] == "event_msg" and row["payload"].get("type") == "token_count"]
        self.assertEqual(len(counts), 4)
        self.assertLessEqual(set(counts), inside, "a token_count line between turns")
        for turn, indexes in turns.items():
            with self.subTest(turn=turn):
                own = [index for index in counts if index in indexes]
                records = [index for index in indexes if rows[index]["type"] == "token_usage_record"]
                self.assertTrue(own and records and records[0] < own[0], "no response before the turn's token_count")
                self.assertEqual(rows[indexes[-1]]["payload"].get("type"), "task_complete")
                self.assertEqual(own[-1], indexes[-1] - 1, "the turn's last token_count is not right before its end")

    def test_a_sub_agent_s_hook_names_its_own_rollout_and_turn(self):
        """A sub-agent that the model spawns (collaboration.spawn_agent, --enable multi_agent_v2): its tool events name
        its own rollout and its own turn, whose task_started is there at the hook, with the root's session_id and
        its own thread as agent_id; no prompt event runs for its turn. Its rollout starts with a copy of the root's
        history, whose turn lines name the root's turn unmarked and hold no token line, so a turn id stands in two
        rollouts and the guard counts a noted turn for one rollout only."""
        if "multi_agent_v2" not in self.features:
            self.skipTest("this Codex has no multi_agent_v2 feature, which offers collaboration.spawn_agent")
        code, out, err, state, models = self.run_codex("SPAWN a helper", sent=50, root_options=("--enable", "multi_agent_v2"))
        self.assertEqual((code, out.strip()), (0, "SPAWN-DONE"), err)
        self.assertEqual(sorted({entry["script"] for entry in models}), ["CHILD", "SPAWN"])
        found = self.rollouts()
        roots = [path for path, rows in found.items() if rows[0]["payload"].get("thread_source") == "user"]
        subs = [path for path, rows in found.items() if rows[0]["payload"].get("thread_source") == "subagent"]
        self.assertEqual((len(roots), len(subs)), (1, 1), sorted(found))
        root, sub = found[roots[0]], found[subs[0]]
        root_thread, sub_thread = root[0]["payload"]["id"], sub[0]["payload"]["id"]
        events = self.events()
        mine = [entry for entry in events if entry["event"]["transcript_path"] == str(subs[0])]
        theirs = [entry for entry in events if entry["event"]["transcript_path"] == str(roots[0])]
        self.assertEqual(len(mine) + len(theirs), len(events))
        self.assertEqual([entry["kind"] for entry in mine], ["tool"], "the sub-agent's events")
        [root_turn] = {entry["event"]["turn_id"] for entry in theirs}
        event = mine[0]["event"]
        self.assertEqual((event["session_id"], event["agent_id"]), (root_thread, sub_thread))
        self.assertIn("agent_type", event)
        self.assertRegex(event["turn_id"], f"^{TURN_ID.pattern}$")
        self.assertNotEqual(event["turn_id"], root_turn)
        self.assertEqual(mine[0]["started"][-1], event["turn_id"])
        self.assertEqual(sub[0]["payload"]["session_id"], root_thread)
        turns = self.turns(sub)
        self.assertEqual(list(turns), [root_turn, event["turn_id"]], "the copied head names the root's turn first")
        head = turns[root_turn]
        self.assertFalse([index for index in head if sub[index]["type"] == "token_usage_record"
                          or sub[index]["type"] == "event_msg" and sub[index]["payload"].get("type") == "token_count"],
                         "the copied head holds a token line")
        self.assertTrue([index for index in turns[event["turn_id"]] if sub[index]["type"] == "event_msg"
                         and sub[index]["payload"].get("type") == "token_count"], "the sub-agent's turn has no token_count")


# A turn id as Codex makes it (a UUID in lower case), and the part of a rollout's path below sessions/.
TURN_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
REL_PATH = re.compile(r"[0-9]{4}/[0-9]{2}/[0-9]{2}/rollout-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}-"
                      r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:_[0-9a-f-]{36})?\.jsonl")


if __name__ == "__main__":
    unittest.main()
