"""codex-as with usage reserves: launch forms, refused options and hook settings.

The cases run check.arguments() and layout.hooks_off() in process against a
temporary HOME, Codex home and working directory. No native, provider,
credential or network is involved.
"""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
UUID = "12345678-1234-4234-8234-123456789abc"
# The kit's own knobs: a caller's value never reaches a case, which sets what it needs.
KIT_KNOBS = ("CODING_AGENT_KIT_CONFIG", "CODING_AGENT_KIT_COLOR", "CODING_AGENT_KIT_USAGE_FILE", "CODING_AGENT_KIT_TOKEN_ID",
             "CODING_AGENT_KIT_CLAUDE_PID", "CODING_AGENT_KIT_REPO_URL", "CODING_AGENT_KIT_GUARD",
             "CODING_AGENT_KIT_GUARD_SCRIPT", "CODING_AGENT_KIT_GUARD_PYTHON", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "NO_COLOR")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load("codex_guard_check", "lib/codex-as/check.py")
layout = load("codex_guard_layout", "lib/codex-as/layout.py")
settings = load("codex_guard_config", "lib/coding-agent-kit/config.py")


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
                self.assertEqual((plan.hooks_override, plan.hooks_disabled, plan.ephemeral, plan.ignore_user_config),
                                 (False, False, False, False))

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
        return layout.hooks_off(check, self.root, self.cwd if cwd is None else cwd, profile)

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


if __name__ == "__main__":
    unittest.main()
