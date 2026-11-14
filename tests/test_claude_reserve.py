"""claude-as native-login enrollment, reserve flags and protected mode.

Every case runs in a temporary HOME with a fake claude (tests/fixtures/claude-reserve) and, for
protected mode, loopback fixtures; nothing reaches a real account, credential or provider. RealClaude
runs the reviewed Claude Code itself (KIT_REAL_CLAUDE, a path, or claude on PATH, used only at a
version protect.REVIEWED_VERSIONS names; skipped otherwise, and always on macOS, where it would use the
login Keychain) with a fabricated credential against the same loopback fixtures.
"""

import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/claude-reserve"
KIT_VARIABLES = ("CODING_AGENT_KIT_CONFIG", "CODING_AGENT_KIT_COLOR", "CODING_AGENT_KIT_USAGE_FILE",
                 "CODING_AGENT_KIT_TOKEN_ID", "CODING_AGENT_KIT_CLAUDE_PID", "CODING_AGENT_KIT_REPO_URL",
                 "XDG_CONFIG_HOME", "XDG_STATE_HOME", "NO_COLOR")
CLAUDE_VARIABLES = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                    "CLAUDE_CODE_USE_FOUNDRY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                    "ALL_PROXY", "all_proxy", "PYTHONPATH")
SETUP_TOKEN = "sk-ant-oat01-" + "fabricated-setup-token-" * 3
ACCOUNT_UUID, ORGANIZATION_UUID = "acct-0000-fixture", "org-0000-fixture"


# Makes its stdin (a PTY) the controlling terminal of the new session it was started in, then execs.
CONTROLLING = "import fcntl, os, sys, termios; fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execve(sys.argv[1], sys.argv[1:], os.environ)"


def spawn_on_terminal(argv, env, cwd, size=None):
    """(pid, master fd) of ARGV on a new PTY as its controlling terminal, in a session of its own.
    subprocess starts it without running Python code in a forked copy of this process, whose gate and
    fixture threads would make a pty.fork() child unsafe."""
    master, slave = pty.openpty()
    if size:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
    try:
        child = subprocess.Popen([sys.executable, "-I", "-c", CONTROLLING, *argv], env=env, cwd=cwd, stdin=slave,
                                 stdout=slave, stderr=slave, start_new_session=True)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    child.returncode = 0  # waited for with os.waitpid by the caller; keeps Popen from waiting again
    return child.pid, master


class Kit(unittest.TestCase):
    """A copied kit, a temporary HOME and a fake claude on PATH."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="claude-reserve-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.secrets = self.home / ".secrets"
        self.secrets.mkdir(mode=0o700)
        self.kit = self.base / "kit"
        for part in ("lib/claude-as", "lib/coding-agent-kit"):
            shutil.copytree(ROOT / part, self.kit / part, ignore=shutil.ignore_patterns("__pycache__"))
        (self.kit / "bin").mkdir()
        shutil.copy2(ROOT / "bin/claude-as", self.kit / "bin/claude-as")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(f"#!{sys.executable}\n" + (FIXTURES / "fake_claude.py").read_text())
        fake.chmod(0o700)
        self.cwd = self.base / "work"
        self.cwd.mkdir()
        self.config = self.base / "config.toml"
        self.record = self.base / "record.json"
        self.env = {key: value for key, value in os.environ.items()
                    if key not in KIT_VARIABLES + CLAUDE_VARIABLES}
        self.env.update(HOME=str(self.home), PATH=os.pathsep.join((str(self.bin), os.environ.get("PATH", ""))),
                        FAKE_RECORD=str(self.record), CODING_AGENT_KIT_CONFIG=str(self.config),
                        CLAUDE_FAKE_LOGIN=f"{ACCOUNT_UUID},{ORGANIZATION_UUID}", CODING_AGENT_KIT_COLOR="never")
        self.write_config("")

    def write_config(self, text):
        self.config.write_text(text)
        self.config.chmod(0o600)

    def native_config(self, name):
        return self.secrets / "native-claude-accounts" / name / "config"

    def run_kit(self, args, env=None, timeout=60):
        return subprocess.run([str(self.kit / "bin/claude-as"), *args], cwd=self.cwd, env=env or self.env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def terminal(self, args, answers=(), env=None, timeout=60, argv=None):
        """claude-as (or ARGV) on a fresh controlling terminal; ANSWERS are (prompt, reply) pairs, in
        order: a text reply is typed with Enter, a callable one gets the output so far and its bytes are
        written as they are. Returns (exit code, everything written to the terminal)."""
        argv = argv or [str(self.kit / "bin/claude-as"), *args]
        pid, master = spawn_on_terminal(argv, env or self.env, self.cwd)
        output, pending, deadline = b"", list(answers), time.monotonic() + timeout
        status = None
        try:
            while time.monotonic() < deadline:
                ready = select.select([master], [], [], 0.05)[0]
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b""
                    if not data:  # EOF or EIO: every holder of the terminal has gone
                        while status is None and time.monotonic() < deadline:
                            done, raw = os.waitpid(pid, os.WNOHANG)
                            status = raw if done else None
                            time.sleep(0.01)
                        break
                    output += data
                while pending and pending[0][0].encode() in output:
                    prompt, reply = pending.pop(0)
                    data = reply(output) if callable(reply) else reply.encode() + b"\r"
                    output = output.replace(prompt.encode(), b"<" + prompt.encode() + b">", 1)
                    if data:
                        os.write(master, data)
                done, raw = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = raw
                    while select.select([master], [], [], 0.1)[0]:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        output += data
                    break
            if status is None:
                os.kill(pid, signal.SIGKILL)
                status = os.waitpid(pid, 0)[1]
                self.fail("claude-as did not finish: " + output.decode("utf-8", "replace"))
        finally:
            os.close(master)
        return os.waitstatus_to_exitcode(status), output.decode("utf-8", "replace").replace("\r\n", "\n")

    def recorded(self):
        return json.loads(self.record.read_text())

    def enroll(self, name="N"):
        """A ready native-login enrollment through the real launcher and the fake native login."""
        first = self.run_kit(["--enroll-native", name])
        self.assertEqual(first.returncode, 0, first.stderr)
        env = dict(self.env, CLAUDE_CONFIG_DIR=str(self.native_config(name)))
        subprocess.run([str(self.bin / "claude"), "auth", "login"], env=env, check=True)
        code, output = self.terminal(["--enroll-native", name], [("as " + name + "? [y/N]", "y")])
        self.assertEqual(code, 0, output)
        return json.loads((self.native_config(name).parent / "account.json").read_text())


class Enrollment(Kit):
    def test_first_run_creates_a_private_pending_registration_and_prints_the_login_commands(self):
        result = self.run_kit(["--enroll-native", "N"])
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.native_config("N")
        for path in (config.parent.parent, config.parent, config):
            info = path.lstat()
            self.assertTrue(stat.S_ISDIR(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        registration = config.parent / "account.json"
        self.assertEqual(stat.S_IMODE(registration.stat().st_mode), 0o600)
        record = json.loads(registration.read_text())
        self.assertEqual({k: v for k, v in record.items() if k != "enrollment_id"},
                         {"schema_version": 1, "mode": "native-login", "state": "pending",
                          "config_directory": str(config), "identity": None})
        self.assertIn(f"CLAUDE_CONFIG_DIR={config} claude auth login", result.stderr)
        self.assertIn(f"CLAUDE_CONFIG_DIR={config} claude auth status", result.stderr)
        self.assertNotIn(str(config) + "/ ", result.stderr)  # the exact spelling, no trailing slash
        self.assertIn('[claude_auth.accounts.N]', result.stderr)
        self.assertFalse(self.record.exists())  # the kit runs no native command on the first step

    def test_confirmation_needs_a_finished_native_login_and_a_yes(self):
        self.run_kit(["--enroll-native", "N"])
        early = self.run_kit(["--enroll-native", "N"])
        self.assertEqual(early.returncode, 1)
        self.assertIn("native login of N is not complete yet", early.stderr)
        env = dict(self.env, CLAUDE_CONFIG_DIR=str(self.native_config("N")))
        subprocess.run([str(self.bin / "claude"), "auth", "login"], env=env, check=True)
        code, output = self.terminal(["--enroll-native", "N"], [("as N? [y/N]", "n")])
        self.assertEqual(code, 1)
        self.assertIn("Logged in to the fixture account", output)
        record = json.loads((self.native_config("N").parent / "account.json").read_text())
        self.assertEqual(record["state"], "pending")
        code, output = self.terminal(["--enroll-native", "N"], [("as N? [y/N]", "y")])
        self.assertEqual(code, 0, output)
        record = json.loads((self.native_config("N").parent / "account.json").read_text())
        self.assertEqual(record["state"], "ready")
        self.assertEqual(record["identity"], {"account_uuid": ACCOUNT_UUID, "organization_uuid": ORGANIZATION_UUID})
        refresh = json.loads(Path(str(self.record) + ".auth").read_text())
        self.assertEqual(refresh["env"]["CLAUDE_CONFIG_DIR"], str(self.native_config("N")))
        again = self.run_kit(["--enroll-native", "N"])
        self.assertEqual(again.returncode, 0)
        self.assertIn("already enrolled", again.stderr)

    def test_a_failing_auth_status_keeps_the_enrollment_pending(self):
        self.run_kit(["--enroll-native", "N"])
        env = dict(self.env, CLAUDE_CONFIG_DIR=str(self.native_config("N")))
        subprocess.run([str(self.bin / "claude"), "auth", "login"], env=env, check=True)
        code, output = self.terminal(["--enroll-native", "N"], env=dict(self.env, CLAUDE_FAKE_AUTH_STATUS="1"))
        self.assertEqual(code, 1)
        self.assertIn("claude auth status failed", output)
        self.assertEqual(json.loads((self.native_config("N").parent / "account.json").read_text())["state"], "pending")

    def test_conflicting_environment_and_invalid_names_refuse_before_anything_is_created(self):
        for env, text in ((dict(self.env, CLAUDE_CODE_OAUTH_TOKEN="x"), "CLAUDE_CODE_OAUTH_TOKEN"),
                          (dict(self.env, CLAUDE_CONFIG_DIR=str(self.base)), "CLAUDE_CONFIG_DIR")):
            result = self.run_kit(["--enroll-native", "N"], env=env)
            self.assertEqual(result.returncode, 1)
            self.assertIn(text, result.stderr)
        for name in ("-x", "a/b", "..", ""):
            result = self.run_kit(["--enroll-native", name])
            self.assertEqual(result.returncode, 1)
            self.assertIn("invalid account name", result.stderr)
        result = self.run_kit(["--enroll-native", "a" * 65])  # a native-login name has at most 64 characters
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid account name for native login (ASCII letters, digits, _ or -; no leading -; at most "
                      "64 characters).", result.stderr)
        self.assertFalse((self.secrets / "native-claude-accounts").exists())

    def test_a_tampered_registration_or_symlinked_directory_refuses(self):
        self.enroll()
        registration = self.native_config("N").parent / "account.json"
        registration.chmod(0o644)
        result = self.run_kit(["--enroll-native", "N"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("permissions 0600", result.stderr)
        registration.chmod(0o600)
        record = json.loads(registration.read_text())
        registration.write_text(json.dumps(dict(record, config_directory=str(self.native_config("N")) + "/")))
        result = self.run_kit(["--enroll-native", "N"])
        self.assertIn("names another configuration directory", result.stderr)
        root = self.secrets / "native-claude-accounts"
        moved = self.base / "elsewhere"
        root.rename(moved)
        root.symlink_to(moved)
        result = self.run_kit(["--enroll-native", "N"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a symlink", result.stderr)

    def test_the_wrong_number_of_words_prints_the_usage(self):
        for args in (["--enroll-native"], ["--enroll-native", "N", "extra"]):
            result = self.run_kit(args)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn("usage: claude-as --enroll-native NAME", result.stderr)
        self.assertFalse((self.secrets / "native-claude-accounts").exists())

    def test_an_enrolled_account_without_the_mode_key_points_at_it_instead_of_a_setup_token(self):
        self.enroll()
        self.record.unlink(missing_ok=True)
        result = self.run_kit(["N", "hello"])
        self.assertEqual(result.returncode, 1)
        self.assertIn('N is enrolled with native login; select it in', result.stderr)
        self.assertIn('[claude_auth.accounts.N] mode = "native-login"', result.stderr)
        self.assertFalse(self.record.exists())
        self.assertFalse((self.secrets / "claude-N").exists())

    def test_list_names_enrolled_native_accounts_beside_setup_token_ones(self):
        self.enroll("N")
        token = self.secrets / "claude-T"
        token.write_text(SETUP_TOKEN + "\n")
        token.chmod(0o600)
        self.assertEqual(self.run_kit(["--list"]).stdout.split(), ["N", "T"])


class NativeLaunch(Kit):
    def test_a_native_login_account_runs_claude_with_its_directory_and_no_injected_token(self):
        record = self.enroll()
        self.write_config('[claude_auth.accounts.N]\nmode = "native-login"\n')
        result = self.run_kit(["N", "--model", "x", "hello"])
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = self.recorded()
        self.assertEqual(seen["args"], ["--model", "x", "hello"])
        env = seen["env"]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.native_config("N")))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertEqual(env["CODING_AGENT_KIT_TOKEN_ID"],
                         hashlib.sha256(("native-login:" + record["enrollment_id"]).encode()).hexdigest()[:16])
        self.assertEqual(env["CODING_AGENT_KIT_CLAUDE_PID"], str(seen["pid"]))  # exec keeps the pid
        self.assertTrue(env["CODING_AGENT_KIT_USAGE_FILE"].endswith("/claude-N.usage.json"))

    def test_unenrolled_pending_and_misdirected_native_accounts_refuse(self):
        self.write_config('[claude_auth.accounts.N]\nmode = "native-login"\n')
        result = self.run_kit(["N"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("not enrolled; run claude-as --enroll-native N", result.stderr)
        self.run_kit(["--enroll-native", "N"])
        result = self.run_kit(["N"])
        self.assertIn("enrollment of N is pending", result.stderr)
        self.assertFalse(self.record.exists())
        env = dict(self.env, CLAUDE_CONFIG_DIR=str(self.base))
        result = self.run_kit(["N"], env=env)
        self.assertIn("CLAUDE_CONFIG_DIR is set", result.stderr)

    def test_native_login_refuses_injected_credentials_and_auth_flags(self):
        self.enroll()
        self.record.unlink(missing_ok=True)
        self.write_config('[claude_auth.accounts.N]\nmode = "native-login"\n')
        result = self.run_kit(["N"], env=dict(self.env, ANTHROPIC_API_KEY="x"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ANTHROPIC_API_KEY", result.stderr)
        result = self.run_kit(["N", "--bare"])
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.record.exists())

    def test_a_missing_gate_module_is_an_incomplete_install_before_anything_starts(self):
        self.enroll()
        for name in ("", ".auth"):
            Path(str(self.record) + name).unlink(missing_ok=True)
        self.write_config('[claude_auth.accounts.N]\nmode = "native-login"\n')
        for name in ("lib/claude-as/bearer.py", "lib/coding-agent-kit/reserve.py", "lib/coding-agent-kit/monitor.py"):
            with self.subTest(name):
                saved = (self.kit / name).read_bytes()
                (self.kit / name).unlink()
                try:
                    result = self.run_kit(["--reserve-5h", "10", "N"])
                finally:
                    (self.kit / name).write_bytes(saved)
                self.assertEqual(result.returncode, 1)
                self.assertIn(f"incomplete install at {self.kit}", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.record.exists())
                self.assertFalse(Path(str(self.record) + ".auth").exists())  # before the pre-launch refresh

    def test_a_setup_token_launch_needs_no_native_module_before_any_native_login_account_exists(self):
        (self.kit / "lib/claude-as/native.py").unlink()
        result = self.run_kit(["newone"])  # a new setup-token enrollment, which needs a terminal here
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("incomplete install", result.stderr)
        (self.secrets / "native-claude-accounts").mkdir(mode=0o700)
        result = self.run_kit(["newone"])
        self.assertIn(f"incomplete install at {self.kit}", result.stderr)

    def test_version_after_a_native_name_is_claude_codes_own(self):
        self.write_config('[claude_auth.accounts.N]\nmode = "native-login"\n')
        result = self.run_kit(["N", "--version"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "2.1.322 (Claude Code)")


class SetupTokenAndFlags(Kit):
    def setUp(self):
        super().setUp()
        token = self.secrets / "claude-T"
        token.write_text(SETUP_TOKEN + "\n")
        token.chmod(0o600)

    def launch_record(self, args):
        if self.record.exists():
            self.record.unlink()
        result = self.run_kit(args)
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = self.recorded()
        seen["env"].pop("CODING_AGENT_KIT_CLAUDE_PID")
        return seen["args"], seen["env"], result.stdout, result.stderr

    def test_disabled_reserves_launch_exactly_as_without_them(self):
        plain = self.launch_record(["T", "--model", "x", "hi"])
        self.assertEqual(plain[1]["CLAUDE_CODE_OAUTH_TOKEN"], SETUP_TOKEN)
        self.assertEqual(self.launch_record(["--reserve-5h", "off", "--reserve-weekly", "off", "T", "--model", "x",
                                             "hi"]), plain)
        self.assertEqual(self.launch_record(["--reserve-weekly", "off", "T", "--model", "x", "hi"]), plain)
        self.write_config('[usage_reserve]\nfive_hour = "off"\nweekly = "off"\n')
        self.assertEqual(self.launch_record(["T", "--model", "x", "hi"]), plain)
        self.write_config('[usage_reserve]\nfive_hour = 20\n[usage_reserve.claude_accounts.T]\nfive_hour = "off"\n')
        self.assertEqual(self.launch_record(["T", "--model", "x", "hi"]), plain)

    def test_reserves_on_a_setup_token_account_refuse_without_running_claude(self):
        for args, config in ((["--reserve-5h", "10", "T"], ""), (["T"], "[usage_reserve]\nweekly = 5\n"),
                             (["T"], "[usage_reserve.claude_accounts.T]\nfive_hour = 0\n")):
            self.write_config(config)
            result = self.run_kit(args)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn("usage reserves need a native-login account, and T uses a setup-token", result.stderr)
            self.assertNotIn(SETUP_TOKEN, result.stderr)
            self.assertFalse(self.record.exists())

    def test_reserve_flag_errors_are_the_reserve_modules(self):
        cases = {("--reserve-5h", "10"): "an account name must follow --reserve-5h and --reserve-weekly.",
                 ("--reserve-5h=10", "T"): "--reserve-5h=VALUE is not accepted; write --reserve-5h VALUE.",
                 ("--reserve-5h", "101", "T"): "--reserve-5h takes a number from 0 through 100, or off",
                 ("--reserve-5h", "1", "--reserve-5h", "2", "T"): "--reserve-5h is given twice.",
                 ("--reserve-weekly", "5", "--list"): "an account name must follow"}
        for args, text in cases.items():
            result = self.run_kit(list(args))
            self.assertEqual(result.returncode, 1, args)
            self.assertIn(text, result.stderr)

    def test_query_after_reserve_flags_still_reaches_claude_code(self):
        result = self.run_kit(["--reserve-5h", "10", "T", "--version"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "2.1.322 (Claude Code)")

    def test_unknown_dash_words_keep_their_hint(self):
        result = self.run_kit(["--reserve", "10", "T"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("--reserve is not a claude-as option", result.stderr)

    def test_help_lists_the_reserve_flags_and_native_enrollment(self):
        result = self.run_kit(["--help"])
        self.assertEqual(result.returncode, 0)
        for text in ("--enroll-native NAME", "--reserve-5h V", "--reserve-weekly V"):
            self.assertIn(text, result.stdout)



def load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("test_claude_policy", ROOT / "lib/claude-as/policy.py")
bearer = load("test_claude_bearer", ROOT / "lib/claude-as/bearer.py")
provider = load("test_claude_provider", ROOT / "lib/claude-as/provider.py")
gate = provider.gate
reserve = provider.reserve
supervise = load("test_claude_supervise", ROOT / "lib/coding-agent-kit/supervise.py")
upstream_fixture = load("test_claude_upstream", FIXTURES / "upstream.py")
ORG_LIMITS = {  # an organization's policy-limits.json in the 2.1.322 schema; keys and values fabricated
    "artifact_create_notice": {"text": "fixture notice"},
    "compliance_taints": ["fixture_regime"],
    "defaults": {"remote_control_at_startup": True},
    "monitoring_notice": {"text": "fixture monitoring", "url": "https://example.invalid/notice"},
    "restrictions": {name: {"allowed": False} for name in (
        "allow_fixture_feature_a", "allow_fixture_feature_b", "enforce_fixture_isolation")},
}


class ServerManagedPolicy(unittest.TestCase):
    def setUp(self):
        # The evaluator refuses group- or world-writable policy files; the files these tests create
        # must not depend on the caller's umask (a 002 umask would make them group-writable).
        self.addCleanup(os.umask, os.umask(0o022))

    def reasons(self, findings):
        return sorted({(f.name, f.native, f.bearer, f.reason) for f in findings})

    def test_an_organization_limits_document_and_an_empty_settings_document_are_accepted(self):
        self.assertEqual(policy.judge_policy_limits(ORG_LIMITS), [])
        self.assertEqual(policy.judge_remote_settings({}, ORGANIZATION_UUID), [])
        self.assertEqual(policy.judge_policy_limits({"core_skills": ["a"], "upcoming_minimum_version": None,
                                                     "restrictions": {"allow_x": True}}), [])

    def test_unreviewed_limits_and_shapes_refuse_both_scopes(self):
        cases = {
            "new_limit": ({"new_limit": 1}, ("new_limit", True, True, "policy_limits_unreviewed_keys")),
            "defaults": ({"defaults": {"other": True}}, ("defaults.other", True, True, "policy_limits_unreviewed_keys")),
            "defaults type": ({"defaults": {"remote_control_at_startup": "yes"}},
                              ("defaults.remote_control_at_startup", True, True, "policy_shape_unrecognized")),
            "restrictions": ({"restrictions": ["allow_x"]}, ("restrictions", True, True, "policy_shape_unrecognized")),
            "taints": ({"compliance_taints": "x"}, ("compliance_taints", True, True, "policy_shape_unrecognized")),
            "notice": ({"monitoring_notice": "x"}, ("monitoring_notice", True, True, "policy_shape_unrecognized")),
        }
        for label, (document, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.reasons(policy.judge_policy_limits(document)), [expected])
        self.assertEqual(self.reasons(policy.judge_policy_limits([])),
                         [("<document>", True, True, "policy_shape_unrecognized")])

    def test_settings_review_scopes(self):
        org = ORGANIZATION_UUID
        cases = {
            "apiKeyHelper": ({"apiKeyHelper": "x"}, [("apiKeyHelper", True, True, "policy_conflicting_auth")]),
            "policyHelper": ({"policyHelper": {}}, [("policyHelper", True, True, "policy_helper_unevaluable")]),
            "hooks": ({"hooks": {"Stop": []}}, [("hooks", True, False, "policy_command_unevaluable")]),
            "empty hooks": ({"hooks": {}, "statusLine": None}, []),
            "refresh": ({"forceRemoteSettingsRefresh": True}, [("forceRemoteSettingsRefresh", True, False,
                                                                "policy_refresh_forced")]),
            "method": ({"forceLoginMethod": "console"}, [("forceLoginMethod", True, True,
                                                          "policy_login_method_incompatible")]),
            "claudeai": ({"forceLoginMethod": "claudeai", "forceLoginOrgUUID": org}, []),
            "org list": ({"forceLoginOrgUUID": ["other", org]}, []),
            "org": ({"forceLoginOrgUUID": "org-elsewhere"}, [("forceLoginOrgUUID", True, True,
                                                              "policy_organization_mismatch")]),
            "env": ({"env": {"ANTHROPIC_BASE_URL": "x", "FOO": "1"}}, [("env", True, True, "policy_conflicting_env")]),
            "plain env": ({"env": {"FOO": "1"}, "permissions": {"deny": ["Bash"]}, "model": "x"}, []),
            "envelope": ({"settings": {"apiKeyHelper": "x"}}, [("settings", True, True, "policy_shape_unrecognized")]),
        }
        for label, (document, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.reasons(policy.judge_remote_settings(document, org)), sorted(expected))

    def test_settings_env_refuses_only_auth_provider_endpoint_proxy_and_store_names(self):
        allowed = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                   "CLAUDE_CODE_IDLE_TOKEN_THRESHOLD", "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS",
                   "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                   "ANTHROPIC_DEFAULT_HAIKU_MODEL", "BASH_DEFAULT_TIMEOUT_MS", "DISABLE_TELEMETRY", "NODE_OPTIONS")
        refused = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS",
                   "anthropic_base_url", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "NO_PROXY", "ALL_PROXY",
                   "AWS_PROFILE", "GOOGLE_APPLICATION_CREDENTIALS", "VERTEX_REGION_CLAUDE_3_5_HAIKU",
                   "CLOUD_ML_REGION", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_OAUTH_TOKEN",
                   "CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_HTTPS_PROXY",
                   "CLAUDE_CODE_PROCESS_WRAPPER", "CLAUDE_CODE_TMPDIR", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CONFIG_DIR",
                   "CLAUDE_CODE_GATEWAY_TOKEN", "CLAUDE_CODE_MEMORY_API_TOKEN4", "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
                   "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_CUSTOM_OAUTH_URL", "CLAUDE_CODE_REMOTE_SDK_URL",
                   "CLAUDE_CODE_METRICS_ENDPOINT",
                   "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_LOCAL_OAUTH_API_BASE")
        for name in allowed:
            with self.subTest(name):
                self.assertEqual(policy.judge_remote_settings({"env": {name: "1"}}, "o"), [])
        for name in refused:
            with self.subTest(name):
                self.assertEqual(self.reasons(policy.judge_local("s", {"env": {name: "1"}}, "o")),
                                 [("env", True, True, "policy_conflicting_env")])

    def test_managed_settings_may_be_roots_but_caches_and_project_files_must_be_yours(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            config, work = self.files(base, {}, ORG_LIMITS)
            managed = base / "managed"
            (managed / "managed-settings.d").mkdir(parents=True)
            (Path(work) / ".claude").mkdir()
            files = {managed / "managed-settings.json": {"permissions": {"deny": ["WebFetch"]}},
                     managed / "managed-settings.d" / "10-org.json": {"env": {"NODE_EXTRA_CA_CERTS": "/x"}},
                     Path(work) / ".claude" / "settings.json": {"model": "x"}}
            for path, document in files.items():
                path.write_text(json.dumps(document))
                path.chmod(0o644)
            self.assertFalse(policy.evaluate(config, ORGANIZATION_UUID, work, platform="linux",
                                             managed_dirs=(str(managed),)).refused())
            # The files belong to the test user; let them stand for root's, and the user be someone else.
            with mock.patch.object(policy, "MANAGED_OWNER", os.getuid()), \
                    mock.patch.object(policy.os, "getuid", return_value=os.getuid() + 1):
                verdict = policy.evaluate(config, ORGANIZATION_UUID, work, platform="linux",
                                          managed_dirs=(str(managed),))
            unsafe = sorted(f.source for f in verdict.findings if f.reason == "policy_file_unsafe")
            self.assertEqual(unsafe, sorted(["remote-settings.json", "policy-limits.json",
                                             str(Path(work) / ".claude" / "settings.json")]))
            (managed / "managed-settings.json").chmod(0o664)
            verdict = policy.evaluate(config, ORGANIZATION_UUID, work, platform="linux", managed_dirs=(str(managed),))
            self.assertEqual([f.source for f in verdict.findings], [str(managed / "managed-settings.json")])

    def test_a_managed_sign_in_organization_pin_is_reported_whatever_its_value(self):
        """Native makes every subscription eligible for organization policy when a managed settings file
        sets forceLoginOrgUUID (S); a user or project file with it does not count."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            config, work = self.files(base)
            managed = base / "managed"
            managed.mkdir()
            (Path(work) / ".claude").mkdir()
            (Path(work) / ".claude/settings.json").write_text(json.dumps({"forceLoginOrgUUID": ORGANIZATION_UUID}))
            evaluate = lambda: policy.evaluate(config, ORGANIZATION_UUID, work, platform="linux",
                                               managed_dirs=(str(managed),))
            self.assertFalse(evaluate().pins_organization)
            for value in (ORGANIZATION_UUID, [ORGANIZATION_UUID], None):
                with self.subTest(value):
                    (managed / "managed-settings.json").write_text(json.dumps({"forceLoginOrgUUID": value}))
                    verdict = evaluate()
                    self.assertEqual((verdict.refused(), verdict.pins_organization), (False, True))
            (managed / "managed-settings.json").unlink()
            (managed / "managed-settings.d").mkdir()
            (managed / "managed-settings.d" / "10-org.json").write_text(json.dumps({"forceLoginOrgUUID": "org-other"}))
            verdict = evaluate()
            self.assertEqual((verdict.native, verdict.pins_organization), (("policy_organization_mismatch",), True))

    def test_local_sources_keep_the_users_own_hooks_but_not_auth_pins(self):
        self.assertEqual(policy.judge_local("s", {"hooks": {"Stop": [1]}, "statusLine": {"command": "x"}}, "o"), [])
        self.assertEqual(self.reasons(policy.judge_local("s", {"apiKeyHelper": "x"}, "o")),
                         [("apiKeyHelper", True, True, "policy_conflicting_auth")])

    def files(self, base, remote=None, limits=None, mode=0o600):
        config = base / "config"
        config.mkdir(mode=0o700)
        for name, document in (("remote-settings.json", remote), ("policy-limits.json", limits)):
            if document is not None:
                path = config / name
                path.write_text(document if isinstance(document, str) else json.dumps(document))
                path.chmod(mode)
        work = base / "work"
        work.mkdir()
        return str(config), str(work)

    def test_evaluate_reads_the_caches_safely_and_reports_names_never_values(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            config, work = self.files(base, {"forceLoginOrgUUID": "org-sensitive-value"}, ORG_LIMITS)
            verdict = policy.evaluate(config, ORGANIZATION_UUID, work, platform="linux")
            self.assertEqual((verdict.native, verdict.bearer), (("policy_organization_mismatch",),) * 2)
            self.assertIn("remote-settings.json: forceLoginOrgUUID", verdict.describe())
            self.assertNotIn("org-sensitive-value", verdict.describe() + repr(verdict))
        for label, change, reason in (
                ("mode", lambda c: os.chmod(c / "policy-limits.json", 0o644), "policy_file_unsafe"),
                ("symlink", lambda c: (c / "remote-settings.json").symlink_to(c / "policy-limits.json"),
                 "policy_file_unsafe"),
                ("size", lambda c: (c / "policy-limits.json").write_text(" " * (1 << 20) + "{}"), "policy_file_unsafe"),
                ("json", lambda c: (c / "policy-limits.json").write_text("{"), "policy_unparsed"),
                ("consent", lambda c: (c / "remote-settings-helper-consent").write_text("1"),
                 "policy_helper_unevaluable")):
            with self.subTest(label), tempfile.TemporaryDirectory() as temp:
                base = Path(temp).resolve()
                config, work = self.files(base, None, {})
                change(Path(config))
                verdict = policy.evaluate(config, ORGANIZATION_UUID, work, platform="linux")
                self.assertIn(reason, verdict.native)
                self.assertTrue(verdict.refused())

    def test_absent_caches_and_a_project_hook_are_accepted_but_a_project_auth_pin_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            config, work = self.files(base)
            self.assertFalse(policy.evaluate(config, ORGANIZATION_UUID, work, platform="linux").refused())
            (Path(work) / ".claude").mkdir()
            (Path(work) / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [1]}}))
            self.assertFalse(policy.evaluate(config, ORGANIZATION_UUID, work, platform="linux").refused())
            (Path(work) / ".claude/settings.local.json").write_text(json.dumps({"env": {"HTTPS_PROXY": "x"}}))
            verdict = policy.evaluate(config, ORGANIZATION_UUID, work, platform="linux")
            self.assertEqual(verdict.native, ("policy_conflicting_env",))

    def test_macos_managed_preferences_for_claude_refuse(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "com.example.other.plist").write_text("")
            self.assertEqual(policy.os_managed(str(root)), [])
            (root / "com.anthropic.claudecode.plist").write_text("")
            self.assertEqual(len(policy.os_managed(str(root))), 1)


class CredentialRead(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.config = base / "s" / "native-claude-accounts" / "N" / "config"
        self.config.mkdir(parents=True)
        for path in (self.config, self.config.parent, self.config.parent.parent, self.config.parent.parent.parent):
            path.chmod(0o700)  # every created level, so the caller's umask cannot make an ancestor shared
        self.now = 1_800_000_000_000

    def store(self, token=upstream_fixture.BEARER_A, expires=None, scopes=("user:inference", "user:profile"), mode=0o600,
              raw=None, subscription="max"):
        path = self.config / ".credentials.json"
        path.write_text(raw if raw is not None else json.dumps({"claudeAiOauth": {
            "accessToken": token, "refreshToken": "fabricated-refresh-secret", "expiresAt": expires or self.now + 3600_000,
            "scopes": list(scopes), "subscriptionType": subscription}}))
        path.chmod(mode)
        return path

    def test_the_subscription_type_is_kept_only_as_a_short_word(self):
        for value, expected in (("enterprise", "enterprise"), ("team", "team"), (None, None), ("Max Plan", None),
                                (7, None), ("x" * 40, None)):
            with self.subTest(value):
                self.store(subscription=value)
                self.assertEqual(self.read().subscription, expected)

    def read(self, **kw):
        return bearer.read(str(self.config), platform="linux", wrap=gate.Secret, now_ms=self.now, **kw)

    def test_only_the_access_token_and_expiry_are_kept_and_never_shown(self):
        self.store()
        credential = self.read()
        self.assertTrue(credential.secret.matches(gate.Secret(upstream_fixture.BEARER_A)))
        self.assertEqual(credential.expires_at_ms, self.now + 3600_000)
        self.assertEqual(set(bearer.Credential.__dataclass_fields__), {"secret", "expires_at_ms", "subscription"})
        self.assertEqual(credential.subscription, "max")
        for text in (repr(credential), str(credential.secret)):
            self.assertNotIn(upstream_fixture.BEARER_A, text)
            self.assertNotIn("fabricated-refresh-secret", text)

    def test_unsafe_or_unusable_stores_refuse_with_a_fixed_reason(self):
        cases = (("mode", dict(mode=0o640), "credential_not_private"),
                 ("scope", dict(scopes=("user:profile",)), "inference_scope_missing"),
                 ("expired", dict(expires=1), "token_expired"),
                 ("json", dict(raw="{"), "document_malformed"),
                 ("duplicate", dict(raw='{"claudeAiOauth": {}, "claudeAiOauth": {}}'), "document_malformed"),
                 ("record", dict(raw='{"other": 1}'), "oauth_record_missing"),
                 ("token", dict(token="short"), "access_token_malformed"))
        for label, change, reason in cases:
            with self.subTest(label):
                self.store(**change)
                with self.assertRaises(bearer.Refused) as caught:
                    self.read()
                self.assertEqual(caught.exception.reason, reason)
                self.assertNotIn("fabricated", str(caught.exception))
        (self.config / ".credentials.json").unlink()
        with self.assertRaises(bearer.Refused) as caught:
            self.read()
        self.assertEqual(caught.exception.reason, "credential_absent")
        target = self.config.parent / "elsewhere.json"
        self.store()
        (self.config / ".credentials.json").rename(target)
        (self.config / ".credentials.json").symlink_to(target)
        with self.assertRaises(bearer.Refused) as caught:
            self.read()
        self.assertEqual(caught.exception.reason, "credential_unusable")

    def test_another_spelling_or_an_open_directory_refuses(self):
        self.store()
        with self.assertRaises(bearer.Refused) as caught:
            bearer.read(str(self.config) + "/", platform="linux", wrap=gate.Secret, now_ms=self.now)
        self.assertEqual(caught.exception.reason, "path_not_canonical")
        self.config.chmod(0o750)
        with self.assertRaises(bearer.Refused) as caught:
            self.read()
        self.assertEqual(caught.exception.reason, "account_directory_unusable")

    def fake_security(self, code, output):
        script = Path(self.tmp.name) / "security"
        log = Path(self.tmp.name) / "security.log"
        script.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                          f"open({str(log)!r}, 'w').write(json.dumps({{'argv': sys.argv[1:], 'env': dict(os.environ)}}))\n"
                          f"sys.stdout.write({output!r})\nsys.exit({code})\n")
        script.chmod(0o700)
        return str(script), log

    def test_the_macos_keychain_item_of_the_exact_directory_and_its_file_fallback(self):
        document = json.dumps({"claudeAiOauth": {"accessToken": upstream_fixture.BEARER_A2, "expiresAt": self.now + 9000,
                                                 "scopes": ["user:inference"]}})
        security, log = self.fake_security(0, document.encode().hex() + "\n")
        credential = bearer.read(str(self.config), platform="darwin", wrap=gate.Secret, now_ms=self.now,
                                 keychain_user="fixture-user", home="/fixture/home", security=security)
        self.assertTrue(credential.secret.matches(gate.Secret(upstream_fixture.BEARER_A2)))
        seen = json.loads(log.read_text())
        service = "Claude Code-credentials-" + hashlib.sha256(str(self.config).encode()).hexdigest()[:8]
        self.assertEqual(seen["argv"], ["find-generic-password", "-a", "fixture-user", "-s", service, "-w"])
        self.assertEqual({k: v for k, v in seen["env"].items() if k in ("PATH", "HOME")},
                         {"PATH": "/usr/bin:/bin", "HOME": "/fixture/home"})
        self.assertEqual(bearer.keychain_account("bad name"), "claude-code-user")
        security, _ = self.fake_security(44, "")
        self.store()
        credential = bearer.read(str(self.config), platform="darwin", wrap=gate.Secret, now_ms=self.now,
                                 keychain_user="u", security=security)
        self.assertTrue(credential.secret.matches(gate.Secret(upstream_fixture.BEARER_A)))
        security, _ = self.fake_security(51, "")
        with self.assertRaises(bearer.Refused) as caught:
            bearer.read(str(self.config), platform="darwin", wrap=gate.Secret, now_ms=self.now, security=security)
        self.assertEqual(caught.exception.reason, "keychain_read_failed")



def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class GateProvider(unittest.TestCase):
    """The Claude provider and its evaluator inside a real gate, against the loopback upstream."""

    def setUp(self):
        self.upstream = upstream_fixture.ClaudeUpstream()
        self.addCleanup(self.upstream.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.config_dir = base / "s" / "native-claude-accounts" / "N" / "config"
        self.config_dir.mkdir(parents=True)
        for path in (self.config_dir, self.config_dir.parent, self.config_dir.parent.parent,
                     self.config_dir.parent.parent.parent):
            path.chmod(0o700)  # every created level, so the caller's umask cannot make an ancestor shared
        self.store(upstream_fixture.BEARER_A)
        self.messages, self.events = [], []

    def store(self, token, expires_in=3600, subscription="max"):
        path = self.config_dir / ".credentials.json"
        oauth = {"accessToken": token, "refreshToken": "fabricated-refresh",
                 "expiresAt": int(time.time() * 1000) + expires_in * 1000, "scopes": ["user:inference", "user:profile"]}
        if subscription is not None:
            oauth["subscriptionType"] = subscription
        path.write_text(json.dumps({"claudeAiOauth": oauth}))
        path.chmod(0o600)

    def config(self, **extra):
        config = {"max_age_s": 120, "account": "N", "reserves": {"five_hour": "10", "weekly": "5"},
                  "sources": {"five_hour": "invocation", "weekly": "global"},
                  "identity": list(upstream_fixture.IDENTITY_A), "config_dir": str(self.config_dir),
                  "platform": "linux", "upstream": {"host": "127.0.0.1", "port": self.upstream.port, "tls": False}}
        config.update(extra)
        return config

    def make(self, mode="startup", gate_options=None, **extra):
        prov, decide = provider.make(self.config(**extra))
        g = gate.Gate(prov, decide, max_age=self.config(**extra)["max_age_s"], mode=mode, notify=self.messages.append,
                      emit=lambda event, **fields: self.events.append((event, fields)), **(gate_options or {}))
        self.addCleanup(g.shutdown)
        return prov, g

    def started(self, gate_options=None, **extra):
        prov, g = self.make(gate_options=gate_options, **extra)
        prov.startup(g)
        g.attach()
        g.heartbeat()
        self.assertTrue(wait_for(lambda: g.state != "checking"), g.status())
        return prov, g

    def body(self, model="claude-sonnet-4-5-20250929"):
        return json.dumps({"model": model, "max_tokens": 1}).encode()

    def admit(self, g, token, model="claude-sonnet-4-5-20250929"):
        g.heartbeat()
        return g.admit(gate.Presented(gate.Secret(token)), self.body(model), lambda: None)

    def test_startup_reads_profile_then_usage_with_the_bearer_and_a_neutral_agent(self):
        prov, g = self.started()
        self.assertEqual(g.state, "running", g.status())
        reads = [r for r in self.upstream.requests if r["method"] == "GET"]
        self.assertEqual([r["path"] for r in reads], ["/api/oauth/profile", "/api/oauth/usage"])
        for request in reads:
            headers = {k.lower(): v for k, v in request["headers"]}
            self.assertEqual(headers["authorization"], "Bearer " + upstream_fixture.BEARER_A)
            self.assertEqual(headers["user-agent"], "quota-probe/1")
            self.assertEqual(headers["anthropic-beta"], "oauth-2025-04-20")
            self.assertNotIn("x-api-key", headers)
        self.assertEqual(g.lease.identity, upstream_fixture.IDENTITY_A)

    def test_a_window_at_its_reserve_or_an_unproved_row_refuses_the_start(self):
        self.upstream.used["five_hour"] = 90
        _, g = self.started()
        self.assertEqual((g.state, g.reason), ("paused", "threshold"))
        self.assertEqual(g.fields["window"], "five-hour")
        self.assertEqual((g.fields["remaining"], g.fields["reserve"]), ("10", "10"))
        self.upstream.used["five_hour"] = 10
        self.upstream.extra = {"cinder_cove": {"utilization": 1}}
        _, g = self.started()
        self.assertEqual((g.state, g.reason), ("paused", "unproved_quota_row"))
        self.upstream.extra = {}
        self.upstream.identities[upstream_fixture.BEARER_A] = upstream_fixture.IDENTITY_B
        _, g = self.started()
        self.assertEqual(g.state, "paused")
        self.assertIn(g.reason, ("identity_mismatch", "identity_changed"))

    def test_a_token_inside_natives_refresh_window_refuses_before_any_read(self):
        self.store(upstream_fixture.BEARER_A, expires_in=200)
        prov, g = self.make()
        with self.assertRaises(gate.Refusal) as caught:
            prov.startup(g)
        self.assertEqual(caught.exception.reason, "native_refresh_window")
        (self.config_dir / ".credentials.json").chmod(0o644)
        with self.assertRaises(gate.Refusal) as caught:
            prov.startup(g)
        self.assertEqual(caught.exception.reason, "credential_credential_not_private")
        self.assertEqual(self.upstream.requests, [])

    def test_the_d1_refresh_must_leave_a_token_valid_for_a_minimum_session(self):
        """Native renews its token only inside its five-minute window, and the run gate refuses every
        renewal once the session runs, so the gate's own read after the refresh is the check that the
        refresh worked: a token that needed it is still inside the window when it failed, and a token
        just outside the window, which native left alone, would end the session within minutes."""
        window, minimum = provider.NATIVE_REFRESH_WINDOW_MS // 1000, provider.MIN_SESSION_MS // 1000
        self.assertEqual((window, minimum), (300, 600))
        for expires_in, reason in ((window - 30, "native_refresh_window"), (window + 30, "token_expires_soon"),
                                   (window + minimum - 30, "token_expires_soon")):
            with self.subTest(expires_in):
                self.store(upstream_fixture.BEARER_A, expires_in=expires_in)
                prov, g = self.make()
                with self.assertRaises(gate.Refusal) as caught:
                    prov.startup(g)
                self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.upstream.requests, [])
        self.store(upstream_fixture.BEARER_A, expires_in=window + minimum + 60)
        expires = json.loads((self.config_dir / ".credentials.json").read_text())["claudeAiOauth"]["expiresAt"]
        _, g = self.started()
        self.assertEqual(g.state, "running", g.status())
        # The launcher shows when the session will need a relaunch: the expiry, never a bearer-derived value.
        self.assertEqual(g.status()["fields"]["token_expires"], reserve.timestamp(expires // 1000))

    def test_models_outside_the_families_or_with_their_own_row_are_refused(self):
        self.upstream.extra = {"seven_day_opus": {"utilization": 3, "resets_at": None}}
        _, g = self.started()
        self.assertEqual(self.admit(g, upstream_fixture.BEARER_A)[:2], (True, "admitted"))
        self.assertEqual(self.admit(g, upstream_fixture.BEARER_A, "claude-haiku-4-5")[:2], (True, "admitted"))
        for model, reason in (("gpt-4", "model_unproved"), ("claude-opus-4-1", "model_has_own_limit"),
                              ("claude-opus-sonnet-x", "model_unproved"), (None, "model_unproved")):
            self.assertEqual(self.admit(g, upstream_fixture.BEARER_A, model)[:2], (False, reason))
        for body in (b'{"model": "claude-sonnet-4-5", "model": "claude-opus-4-1"}',
                     b'{"model": "claude-opus-4-1", "model": "claude-sonnet-4-5"}'):
            g.heartbeat()
            self.assertEqual(g.admit(gate.Presented(gate.Secret(upstream_fixture.BEARER_A)), body, lambda: None)[:2],
                             (False, "model_unproved"))
        self.assertEqual(g.state, "running")

    def test_an_organization_account_refuses_before_any_read_even_with_policy_caches(self):
        """Native applies no organization policy behind the gate (custom ANTHROPIC_BASE_URL, S), so every
        subscription it would apply one to refuses, whatever is cached."""
        for name in ("remote-settings.json", "policy-limits.json"):
            (self.config_dir / name).write_text("{}")
            (self.config_dir / name).chmod(0o600)
        for subscription in ("enterprise", "team", None, "unknown_plan"):
            with self.subTest(subscription):
                self.store(upstream_fixture.BEARER_A, subscription=subscription)
                prov, g = self.make()
                with self.assertRaises(gate.Refusal) as caught:
                    prov.startup(g)
                self.assertEqual(caught.exception.reason, "organization_policy")
        self.assertEqual(self.upstream.requests, [])
        for subscription in ("pro", "max"):  # personal plans: native applies no organization policy in any mode
            with self.subTest(subscription):
                self.store(upstream_fixture.BEARER_A, subscription=subscription)
                _, g = self.started()
                self.assertEqual(g.state, "running", g.status())

    def test_the_refresh_tunnel_reaches_only_the_reviewed_address(self):
        config = self.config(connect_allow={"platform.claude.com:443": ["127.0.0.1", 9], "evil.example:443": ["x", 1]})
        del config["upstream"]
        self.assertEqual(provider.ClaudeProvider(config).connect_allow,
                         {"platform.claude.com:443": ("platform.claude.com", 443)})
        config = self.config(connect_allow={"platform.claude.com:443": ["127.0.0.1", 9]})
        self.assertEqual(provider.ClaudeProvider(config).connect_allow, {"platform.claude.com:443": ("127.0.0.1", 9)})

    def test_a_rotated_bearer_of_the_same_account_is_held_rebound_and_admitted(self):
        _, g = self.started()
        ok, _, stream = self.admit(g, upstream_fixture.BEARER_A)
        self.assertTrue(ok)
        ok, reason, _ = self.admit(g, upstream_fixture.BEARER_A2)
        self.assertEqual((ok, reason), (True, "admitted"))
        self.assertEqual(g.epoch, 2)
        self.assertIn(stream, g.streams)  # a same-identity rotation keeps admitted streams
        profiles = [r for r in self.upstream.requests if r["path"] == "/api/oauth/profile"]
        self.assertIn(("Authorization", "Bearer " + upstream_fixture.BEARER_A2), profiles[-1]["headers"])

    def test_a_bearer_of_another_account_pauses_and_closes_streams(self):
        _, g = self.started()
        closed = []
        g.heartbeat()
        ok, _, _ = g.admit(gate.Presented(gate.Secret(upstream_fixture.BEARER_A)), self.body(), lambda: closed.append(1))
        self.assertTrue(ok)
        ok, reason, _ = self.admit(g, upstream_fixture.BEARER_B)
        self.assertEqual((ok, reason), (False, "bearer_identity_mismatch"))
        self.assertEqual((g.state, closed), ("paused", [1]))
        self.assertEqual(self.upstream.model_requests(), [])
        self.assertEqual(self.admit(g, upstream_fixture.BEARER_A)[:2], (False, "bearer_identity_mismatch"))

    def test_after_another_account_signs_in_polls_reprove_the_store_bearer_and_never_say_a_check_passes(self):
        """Once the native presents another account's bearer (a /login inside the session), the old
        lease no longer proves the enrolled account: paused polls check the store's bearer with both
        reads, never the signed-out one, and no notice says that a check passes until the store holds
        the enrolled account again; then a continuation resumes on the re-proved bearer."""
        prov, g = self.started(gate_options={"notice_every": 0.01})
        self.assertEqual(g.state, "running", g.status())
        self.store(upstream_fixture.BEARER_B)
        self.assertEqual(self.admit(g, upstream_fixture.BEARER_B)[:2], (False, "bearer_identity_mismatch"))
        start = len(self.upstream.requests)

        def poll():
            time.sleep(0.02)
            g.heartbeat()
            prov.poll(g)
            g.tick()

        for _ in range(3):
            poll()
        reads = self.upstream.requests[start:]
        self.assertEqual([r["path"] for r in reads], ["/api/oauth/profile", "/api/oauth/usage"] * 3)
        self.assertEqual({dict((k.lower(), v) for k, v in r["headers"])["authorization"] for r in reads},
                         {"Bearer " + upstream_fixture.BEARER_B})
        self.assertEqual((g.state, g.reason), ("paused", "bearer_identity_mismatch"))
        self.assertIn("the signed-in account changed", g.fields["message"])
        g.heartbeat()
        self.assertEqual(g.arm_continuation(), {"ok": False, "reason": "identity_mismatch"})
        poll()
        self.assertEqual([m for m in self.messages if m["op"] == "notice"], [])
        self.store(upstream_fixture.BEARER_A2)  # signed in to the enrolled account again
        poll()
        self.assertEqual((g.state, g.reason), ("paused", "awaiting_continuation"))
        self.assertEqual([(m["eligible"], m["reason"]) for m in self.messages if m["op"] == "notice"],
                         [(True, "awaiting_continuation")])
        g.heartbeat()
        self.assertEqual(g.arm_continuation(), {"ok": True, "reason": "resumed"})
        self.assertEqual(self.admit(g, upstream_fixture.BEARER_A2)[:2], (True, "admitted"))

    def test_a_pause_resumes_only_through_a_continuation_and_a_fresh_pass(self):
        prov, g = self.started()
        self.upstream.used["seven_day"] = 96
        prov.poll(g)
        self.assertEqual((g.state, g.reason), ("paused", "threshold"))
        self.upstream.used["seven_day"] = 20
        prov.poll(g)
        self.assertEqual((g.state, g.reason), ("paused", "awaiting_continuation"))
        g.heartbeat()
        reply = g.arm_continuation()
        self.assertEqual(reply, {"ok": True, "reason": "resumed"})
        self.assertEqual(g.state, "running")
        self.assertTrue(any(m["op"] == "resumed" for m in self.messages))
        self.upstream.used["seven_day"] = 99
        prov.poll(g)
        g.heartbeat()
        reply = g.arm_continuation()
        self.assertEqual((reply["ok"], g.state), (False, "paused"))

    def test_monitor_loss_latches_and_refuses_every_continuation(self):
        _, g = self.started()
        g.lose("monitor_eof")
        self.assertEqual((g.state, g.reason), ("paused", "monitor_eof"))
        self.assertTrue(any(m["op"] == "latched" for m in self.messages))
        self.assertEqual(g.arm_continuation(), {"ok": False, "reason": "monitor_lost"})
        self.assertEqual(self.admit(g, upstream_fixture.BEARER_A)[:2], (False, "monitor_eof"))

    def test_a_restarted_gate_starts_paused_and_needs_a_continuation(self):
        prov, g = self.make(mode="restart")
        self.assertEqual((g.state, g.reason), ("paused", "gate_restart"))
        g.attach()
        self.assertEqual(g.arm_continuation(), {"ok": False, "reason": "monitor_unheard"})
        g.heartbeat()
        self.assertEqual(g.arm_continuation(), {"ok": True, "reason": "resumed"})
        self.assertEqual(self.admit(g, upstream_fixture.BEARER_A)[:2], (True, "admitted"))

    def test_a_restarted_gate_reads_quota_with_the_store_bearer_but_never_resumes_by_itself(self):
        prov, g = self.make(mode="restart")
        g.attach()
        g.heartbeat()
        prov.poll(g)
        self.assertEqual([r["path"] for r in self.upstream.requests], ["/api/oauth/profile", "/api/oauth/usage"])
        self.assertEqual((g.state, g.reason), ("paused", "awaiting_continuation"))
        self.upstream.used["five_hour"] = 95
        prov.poll(g)  # the lease the read bound polls on: usage only
        self.assertEqual(self.upstream.paths("GET")[2:], ["/api/oauth/usage"])
        self.assertEqual((g.state, g.reason), ("paused", "threshold"))
        self.upstream.used["five_hour"] = 10
        prov.poll(g)
        g.heartbeat()
        self.assertEqual(g.arm_continuation(), {"ok": True, "reason": "resumed"})

    def test_rebind_and_continuation_reads_fit_the_bounds_their_waiters_use(self):
        """No read is cut shorter than a poll's: each GET of a rebind or a continuation gets the gate's
        READ_TIMEOUT. A rebind's GETs fit what its held requests wait for (rebind_reads per-fetch
        bounds plus the slack), and a continuation's store read and GETs fit the launcher's wait for
        it, so a slow link that lets polls pass lets a continuation pass too."""
        prov, _ = self.make()
        seen = []

        class Fetching:
            lease = None

            def fetch(self, method, path, **options):
                seen.append((path, options["timeout"]))
                body = {"account": {"uuid": "a"}, "organization": {"uuid": "o"}} if "profile" in path else {}
                return 200, [("Content-Type", "application/json")], json.dumps(body).encode()

        for cause in ("startup", "rebind", "continuation"):
            prov.read_quota(Fetching(), gate.Presented(gate.Secret(upstream_fixture.BEARER_A)), cause)
        timeouts = {cause: [timeout for _, timeout in seen[index * 2:index * 2 + 2]]
                    for index, cause in enumerate(("startup", "rebind", "continuation"))}
        bound = {timeout or gate.READ_TIMEOUT for times in timeouts.values() for timeout in times}
        self.assertEqual(bound, {gate.READ_TIMEOUT})
        self.assertEqual(timeouts["rebind"], [prov.rebind_timeout] * 2)
        self.assertLessEqual(len(timeouts["rebind"]), prov.rebind_reads)  # each waiter allows this many fetches
        continuation = bearer.KEYCHAIN_TIMEOUT + len(timeouts["continuation"]) * gate.READ_TIMEOUT
        self.assertLessEqual(continuation + 1.0, supervise.CONTINUE_TIMEOUT)

    def test_a_rejected_lease_bearer_is_reread_from_the_store_and_rebound(self):
        prov, g = self.started()
        self.store(upstream_fixture.BEARER_A2)
        del self.upstream.identities[upstream_fixture.BEARER_A]
        prov.poll(g)
        self.assertEqual(g.state, "running", g.status())
        self.assertTrue(g.lease.secret.matches(gate.Secret(upstream_fixture.BEARER_A2)))
        self.assertEqual(g.epoch, 2)

    def test_an_expired_store_token_refuses_the_continuation(self):
        prov, g = self.started()
        g.latch("threshold")
        self.store(upstream_fixture.BEARER_A, expires_in=-5)
        g.heartbeat()
        reply = g.arm_continuation()
        self.assertEqual(reply, {"ok": False, "reason": "credential_token_expired"})

    def test_stale_readings_pause_on_the_timer(self):
        prov, g = self.started(max_age_s=0.5)
        self.assertEqual(g.state, "running")
        time.sleep(0.6)
        g.heartbeat()
        g.tick()
        self.assertEqual((g.state, g.reason), ("paused", "stale"))

    def test_refusals_are_400_errors_without_a_login_hint_and_carry_no_bearer(self):
        prov, g = self.started()
        for reason in ("threshold", "model_unproved", "model_has_own_limit"):
            status, body = prov.refusal(reason)
            payload = json.loads(body)
            self.assertEqual((status, payload["type"], payload["error"]["type"]), (400, "error", "invalid_request_error"))
            self.assertIn("quota reserve", payload["error"]["message"])
            self.assertNotIn("login", payload["error"]["message"])
        self.admit(g, upstream_fixture.BEARER_B)
        text = json.dumps([self.messages, [list(e) for e in self.events], g.status()], default=str)
        for token in (upstream_fixture.BEARER_A, upstream_fixture.BEARER_A2, upstream_fixture.BEARER_B,
                      "fabricated-refresh"):
            self.assertNotIn(token, text)

    def test_gate_fetch_survives_a_connection_close_reply(self):
        """A quota read whose reply says Connection: close, as real servers commonly answer, starts the gate."""
        self.upstream.close_header = True
        _, g = self.started()
        self.assertEqual(g.state, "running", g.status())

    def test_usage_normalization(self):
        readings, own, unproved = provider.normalize_usage({
            "five_hour": {"utilization": 12.5, "resets_at": "2026-11-12T10:00:00Z"}, "seven_day": None,
            "seven_day_sonnet": {"utilization": 1}, "extra_usage": {"is_enabled": True}, "limits": [],
            "wattle_ember": {"x": 1}, "type": "usage"})
        self.assertEqual([(r.name, r.used) for r in readings], [("five_hour", 12.5)])
        self.assertEqual(readings[0].resets_at, 1794477600)
        self.assertEqual((own, unproved), (frozenset({"sonnet"}), ("wattle_ember",)))
        self.assertIsNone(provider.normalize_profile({"account": {"uuid": "a b"}, "organization": {"uuid": "o"}}))
        self.assertEqual(provider.family("claude-3-5-haiku-20241022"), "haiku")
        self.assertIsNone(provider.family("claude-sonnet-4-5[1m]"))



def boundary_problem():
    """Why this host cannot run the protected-mode boundary, or None."""
    name = "sandbox_macos.py" if sys.platform == "darwin" else "sandbox_linux.py"
    module = load("test_claude_sandbox", ROOT / "lib/coding-agent-kit" / name)
    try:
        module.probe()
    except module.Refusal as error:
        return str(error)
    return None


protect = load("test_claude_protect", ROOT / "lib/claude-as/protect.py")


class ProtectScreens(unittest.TestCase):
    """protect.py's pure screens in process, on every host (the launcher-level cases below need the
    boundary, whose probe runs first)."""

    def refused(self, call, *args):
        with self.assertRaises(protect.Refused) as caught:
            call(*args)
        return str(caught.exception)

    def test_the_interactive_forms_pass_and_report_their_add_dirs(self):
        for args, add_dirs in (([], []), (["hello world"], []), (["--model", "claude-sonnet-4-5", "hi"], []),
                               (["--model=opus", "--effort", "high"], []), (["-d"], []), (["-d", "api", "hi"], []),
                               (["--add-dir", "a", "b", "--verbose", "go"], ["a", "b"]), (["--add-dir=c"], ["c"]),
                               (["--allowedTools", "Bash", "Read", "--", "-x"], []), (["fix", "the", "mcp"], []),
                               (["--", "fix", "auth"], []), (["--permission-mode", "plan", "-"], []),
                               (["--dangerously-skip-permissions", "--verbose"], [])):
            with self.subTest(args):
                self.assertEqual(protect.screen_arguments(args).add_dirs, add_dirs)

    def test_the_gate_tables_are_those_of_the_one_reviewed_version(self):
        """provider.py's routes, families and policy shapes are one table, so adding a release to
        REVIEWED_VERSIONS needs those tables keyed by release first."""
        self.assertEqual(protect.REVIEWED_VERSIONS, (provider.VERSION,))

    def test_every_form_protected_mode_allows_passes_the_launchers_own_screen(self):
        """launch.py's preflight runs first on every native-login launch; a form only protect.py allowed
        would be refused there anyway."""
        launch = load("test_claude_launch", ROOT / "lib/claude-as/launch.py")
        forms = [[flag, "x"] for flag in sorted(protect.VALUES | protect.VARIADIC)]
        forms += [[flag] for flag in sorted(protect.OPTIONAL | protect.SWITCHES)]
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):  # no caller credential or provider variable
            for form in forms:
                with self.subTest(form):
                    launch.preflight(form, None, enrollment=False, config_dir="/nonexistent/config")
                    self.assertEqual(protect.screen_arguments(form).add_dirs, ["x"] if form[0] == "--add-dir" else [])

    def test_every_other_form_or_a_command_word_refuses(self):
        for args, text in ((["-p", "hi"], "-p is not an interactive form"), (["--print"], "--print is not"),
                           (["-pc"], "-pc is not"), (["-c"], "-c is not"), (["--continue"], "--continue is not"),
                           (["-r"], "-r is not"), (["--resume", "x"], "--resume is not"),
                           (["--fork-session"], "--fork-session is not"), (["--session-id", "x"], "--session-id is not"),
                           (["--settings", "{}"], "--settings is not"), (["--setting-sources=user"], "--setting-sources is"),
                           (["--mcp-config", "x"], "--mcp-config is not"), (["--plugin-dir", "x"], "--plugin-dir is"),
                           (["--remote"], "--remote is not"), (["--bg"], "--bg is not"), (["--betas", "x"], "--betas is"),
                           (["--sk-ant-oat01-secret"], "this option is not"), (["--model"], "--model needs a value"),
                           (["mcp", "list"], "the claude mcp command"), (["--model", "x", "auth"], "the claude auth command"),
                           (["--", "update"], "the claude update command"), (["--verbose", "doctor"], "claude doctor")):
            with self.subTest(args):
                message = self.refused(protect.screen_arguments, args)
                self.assertIn(text, message)
                self.assertTrue(message.startswith("protected mode refused: "))
                self.assertNotIn("secret", message)

    def test_every_top_level_command_of_the_reviewed_version_refuses(self):
        """Every top-level command Claude Code 2.1.322 registers, visible and hidden, with its aliases (S),
        plus the hidden login words and the two other names its root handling treats as commands."""
        reviewed = {"agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import", "install", "logs", "mcp",
                    "plugin", "plugins", "purge", "respawn", "rm", "setup-token", "stop", "kill", "ultrareview",
                    "update", "upgrade", "remote-control", "rc", "project", "sandbox", "edit-hook",
                    "edit-chrome-settings", "edit-memory-settings", "edit-permission-rules", "edit-sandbox-settings",
                    "edit-skill-overrides", "design-login", "drop-worktree-registrations", "import-conversations",
                    "apply-project-settings", "rollback", "forward-home-settings", "login", "logout", "config", "api"}
        self.assertEqual(protect.SUBCOMMANDS, reviewed)
        for word in sorted(reviewed):
            with self.subTest(word):
                self.assertIn(f"the claude {word} command does not run in protected mode",
                              self.refused(protect.screen_arguments, [word, "--json"]))
                self.assertIn(f"the claude {word} command", self.refused(protect.screen_arguments, ["--", word]))
        self.assertEqual(protect.screen_arguments(["edit the hook", "project"]).add_dirs, [])  # prompts stay prompts

    def test_a_refusing_policy_verdict_or_a_machine_organization_pin_refuses(self):
        paths = types.SimpleNamespace(name="N")
        protect.judge_policy(policy.Verdict((), (), ()), paths)
        verdict = policy.Verdict(("policy_conflicting_auth",), ("policy_conflicting_auth",),
                                 (policy.Finding("policy-limits.json", "apiKeyHelper", True, True,
                                                 "policy_conflicting_auth"),))
        self.assertIn("Claude Code policy for this account: a credential helper",
                      self.refused(protect.judge_policy, verdict, paths))
        message = self.refused(protect.judge_policy, policy.Verdict((), (), (), True), paths)
        self.assertIn("this machine's managed settings pin the sign-in organization (forceLoginOrgUUID)", message)
        self.assertIn("launch without reserves (claude-as N)", message)
        self.assertIn("N is an organization account", protect.organization_message(paths))

    def test_store_and_proxy_selectors_in_the_environment_refuse_without_values(self):
        protect.screen_environment({"PATH": "/bin", "HTTPS_PROXY": "http://127.0.0.1:1"})
        for name in protect.ENV_REFUSED:
            with self.subTest(name):
                message = self.refused(protect.screen_environment, {name: "hidden-value"})
                self.assertIn(name, message)
                self.assertNotIn("hidden-value", message)

    def test_the_gate_takes_the_proxy_claude_code_would_use_for_https(self):
        """Claude Code reads HTTPS_PROXY, then HTTP_PROXY, then ALL_PROXY (either spelling) for its HTTPS
        requests, the bearer-carrying ones included; the gate's upstream connections follow it."""
        for env, expected in (({}, None),
                              ({"HTTPS_PROXY": "http://a.test:1", "HTTP_PROXY": "http://c.test:3"}, ("a.test", 1, ())),
                              ({"http_proxy": "http://c.test:3", "ALL_PROXY": "socks5h://b.test:2"}, ("c.test", 3, ())),
                              ({"ALL_PROXY": "http://b.test:2", "no_proxy": "api.anthropic.com"},
                               ("b.test", 2, (("api.anthropic.com",),)))):
            with self.subTest(env=env):
                self.assertEqual(protect.gate_proxy(supervise, gate.parse_proxy, env), expected)
        for env, reason in (({"HTTP_PROXY": "socks5h://c.test:3"}, "unsupported HTTP_PROXY"),
                            ({"all_proxy": "socks5h://b.test:2"}, "unsupported all_proxy"),
                            ({"HTTP_PROXY": "http://c.test:3", "http_proxy": "http://d.test:4"},
                             "HTTP_PROXY and http_proxy differ")):
            with self.subTest(env=env):
                message = self.refused(protect.gate_proxy, supervise, gate.parse_proxy, env)
                self.assertIn(reason, message)
                self.assertTrue(message.startswith("protected mode refused: "))
                self.assertNotIn(".test", message)

    def test_the_launchers_own_python_must_lie_out_of_reach_by_the_name_it_ran(self):
        """bin/claude-as runs the python3 it finds on PATH, before any check; a protected session that
        could replace it, a prefix or a module path entry would run the next launch unconfined."""
        sources = protect.launcher_sources()
        kinds = {}
        for what, path, holds in sources:
            kinds.setdefault(what, []).append((path, holds))
        self.assertEqual(kinds["the launcher's Python"], [(sys.executable, False)])
        self.assertEqual(len(kinds["the launcher's Python prefix"]), 4)
        self.assertEqual(kinds["the launcher's module path entry"], [(path, True) for path in sys.path])
        from unittest import mock
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            work = base / "work"
            (work / ".venv/bin").mkdir(parents=True)
            (base / "bin").mkdir()
            supervise.unreachable(protect.launcher_sources(), [str(work)], protect.LAUNCHER_HINT)
            python = work / ".venv/bin/python3"
            linked = base / "bin/python3"
            linked.symlink_to(python)  # a link outside to an interpreter inside
            for patch in ({"executable": str(python)}, {"executable": str(linked)},
                          {"prefix": str(work / ".venv")}, {"path": [*sys.path, str(work / "lib")]},
                          {"path": [*sys.path, str(base)]}):
                with self.subTest(patch=patch), mock.patch.multiple(sys, **patch):
                    with self.assertRaises(supervise.Refused) as caught:
                        supervise.unreachable(protect.launcher_sources(), [str(work)], protect.LAUNCHER_HINT)
                    self.assertIn("; start claude-as with a python3 outside the working directory, every --add-dir "
                                  "and the account directory first on PATH", caught.exception.reason)
            for patch in ({"executable": ""}, {"executable": None}, {"path": ["relative"]}):
                with self.subTest(patch=patch), mock.patch.multiple(sys, **patch):
                    self.assertIn("does not report where it loads code from", self.refused(protect.launcher_sources))

    def test_the_reach_checked_for_code_is_everything_a_boundary_process_can_write(self):
        sandbox = types.SimpleNamespace(DEVICES=("/dev/null", "/dev/pts", "/dev/shm"),
                                        claude_temp=lambda uid: "/private/tmp/claude-%d" % uid)
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            (base / "work").mkdir()
            (base / "link").symlink_to(base / "work")
            writable = [str(base / "link"), str(base / "work")]
            self.assertEqual(protect.reach(writable, sandbox, 501, darwin=False),
                             [str(base / "work")] + [os.path.realpath(path) for path in sandbox.DEVICES])
            self.assertEqual(protect.reach(writable, sandbox, 501, darwin=True),
                             [str(base / "work"), "/dev", "/private/tmp/claude-501"])

    def test_home_its_ancestors_and_its_login_items_cannot_be_writable(self):
        home = "/home/fixture"
        protect.check_writable(["/home/fixture/project", "/tmp/x", "/home/fixture/Library/Caches"], home)
        for path in ("/home/fixture", "/home", "/", "/home/fixture/Library", "/home/fixture/Library/LaunchAgents",
                     "/home/fixture/Library/LaunchAgents/sub"):
            with self.subTest(path):
                self.assertIn("cannot be writable in protected mode", self.refused(protect.check_writable, [path], home))

    def test_only_a_reviewed_native_version_passes(self):
        """native_version judges what `claude --version` printed; the launcher runs it inside the
        boundary (confined_version), never outside."""
        self.assertEqual(protect.native_version(lambda: (0, b"2.1.322 (Claude Code)\n")), "2.1.322")
        for result, text in (((0, b"2.1.323 (Claude Code)\n"), "Claude Code 2.1.323 is not a version"),
                             ((0, b"2.1.322\n"), "did not print a Claude Code version"),
                             ((1, b"2.1.322 (Claude Code)\n"), "did not print a Claude Code version"),
                             ((None, b""), "did not answer")):
            with self.subTest(result):
                self.assertIn(text, self.refused(protect.native_version, lambda: result))

    def test_everything_running_the_native_executes_is_checked_for_reach(self):
        """native_sources lists the native by the name the launcher runs, so every link on the way
        counts, its directory, and each interpreter a #! line names: an absolute one, or the one
        `#!/usr/bin/env NAME` finds on the native's PATH, through nested scripts. A boundary process
        that could replace any of them would have the next launch run its code."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            work, lib, bin_, tools = base / "work", base / "lib/claude-code", base / "bin", base / "tools"
            for path in (work / "node_modules/.bin", lib, bin_, tools):
                path.mkdir(parents=True)

            def script(path, first_line, mode=0o755):
                path.write_text(first_line + "\nexit 0\n")
                path.chmod(mode)
                return path

            node = script(tools / "node", f"#!{sys.executable}")
            cli = script(lib / "cli.js", "#!/usr/bin/env node")
            (bin_ / "claude").symlink_to("../lib/claude-code/cli.js")
            env = {"PATH": f"{work / 'node_modules/.bin'}:{tools}:/usr/bin:/bin"}
            sources = protect.native_sources(str(bin_ / "claude"), env)
            self.assertEqual(sources[:5], [
                ("the Claude Code program", str(bin_ / "claude"), False),
                ("the Claude Code directory", str(lib), True),
                ("the Claude Code interpreter", "/usr/bin/env", False),
                ("the Claude Code interpreter", str(node), False),
                ("the Claude Code interpreter", sys.executable, False)])
            for first_line, found in (("#!/usr/bin/env -S node --no-warnings", str(node)),
                                      ("#! /usr/bin/env -S NODE_NO_WARNINGS=1 node", str(node)),
                                      (f"#!{node} --flag", str(node))):
                with self.subTest(first_line):
                    script(cli, first_line)
                    self.assertIn(("the Claude Code interpreter", found, False),
                                  protect.native_sources(str(bin_ / "claude"), env))
            script(cli, "#!/usr/bin/env node")
            for root in (str(work), str(lib), str(bin_), str(tools), str(lib / "vendor")):
                with self.subTest(root=root):
                    if root == str(work):
                        script(work / "node_modules/.bin/node", "#!/bin/sh")  # first on PATH, in reach
                    with self.assertRaises(supervise.Refused) as caught:
                        supervise.unreachable(protect.native_sources(str(bin_ / "claude"), env), [root],
                                              protect.NATIVE_HINT)
                    self.assertTrue(caught.exception.reason.endswith(protect.NATIVE_HINT), caught.exception.reason)
                    (work / "node_modules/.bin/node").unlink(missing_ok=True)
            supervise.unreachable(protect.native_sources(str(bin_ / "claude"), env), [str(work)], protect.NATIVE_HINT)
            self.assertTrue(protect.NATIVE_HINT.startswith("; run claude-as with a Claude Code installed outside the "
                                                           "working directory, every --add-dir and the account directory"))
            binary = bin_ / "native"
            binary.write_bytes(b"\x7fELF\x02\x01\x01" + bytes(64))
            binary.chmod(0o755)
            self.assertEqual(protect.native_sources(str(binary), env), [
                ("the Claude Code program", str(binary), False), ("the Claude Code directory", str(bin_), True)])
            for first_line, text in (("#!/usr/bin/env -i node", "an env option"), ("#!node", "not an absolute path"),
                                     ("#!/usr/bin/env nodejs-absent", "not on the native's PATH"),
                                     ("#!/usr/bin/env", "names no command"),
                                     ("#!/usr/bin/env -S 'node' x", "not one protected mode can follow"),
                                     ("#!/usr/bin/env " + "x" * 300, "not one protected mode can follow")):
                with self.subTest(first_line):
                    script(cli, first_line)
                    message = self.refused(protect.native_sources, str(bin_ / "claude"), env)
                    self.assertIn("the #! line of", message)
                    self.assertIn(text, message)
            loop = script(tools / "loop", f"#!{tools / 'loop'}")
            script(cli, f"#!{loop}")
            self.assertIn("more than 4 interpreters", self.refused(protect.native_sources, str(bin_ / "claude"), env))


class MacosTemporaryCleanup(unittest.TestCase):
    """The end-of-invocation cleanup of Claude's fixed /private/tmp/claude-UID, which a boundary process
    can replace (its subpath rule covers the entry itself). The launcher runs outside the boundary, so it
    removes only this invocation's entry, by descriptor, beneath the very directory it recorded before
    the native started; these cases use a temporary root on any host."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "claude-501"
        self.cwd = "/Users/me/proj.x"
        self.name = "-Users-me-proj-x"
        self.outside = self.base / "home/.claude/projects"  # what a link would expose: another session's history
        (self.outside / self.name).mkdir(parents=True)
        (self.outside / self.name / "session.jsonl").write_text("history")

    def snapshot(self):
        return protect.macos_temp_snapshot(str(self.root))

    def clean(self, snapshot):
        protect.clean_macos_temp(str(self.root), snapshot, self.cwd)

    def assertOutsideIntact(self):
        self.assertEqual((self.outside / self.name / "session.jsonl").read_text(), "history")

    def test_only_this_invocations_new_entry_is_removed(self):
        self.assertEqual(protect.temp_entry_name(self.cwd), self.name)
        snapshot = self.snapshot()  # an absent root is created as native creates it, so it can be recorded
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        (self.root / self.name / "sub").mkdir(parents=True)
        (self.root / self.name / "sub" / "out.txt").write_text("x")
        (self.root / self.name / "link").symlink_to(self.outside)  # a link inside is removed, never followed
        (self.root / "-other-session").mkdir()
        self.clean(snapshot)
        self.assertEqual(sorted(os.listdir(self.root)), ["-other-session"])
        self.assertOutsideIntact()

    def test_an_entry_that_existed_before_the_native_started_stays(self):
        (self.root / self.name).mkdir(parents=True)
        snapshot = self.snapshot()
        (self.root / self.name / "new.txt").write_text("x")
        self.clean(snapshot)
        self.assertTrue((self.root / self.name / "new.txt").exists())

    def test_a_root_replaced_by_a_link_is_never_followed(self):
        snapshot = self.snapshot()
        shutil.rmtree(self.root)
        self.root.symlink_to(self.outside)  # what `rm -rf ROOT; ln -s ~/.claude/projects ROOT` leaves
        self.clean(snapshot)
        self.assertOutsideIntact()

    def test_a_swapped_root_is_left_alone(self):
        snapshot = self.snapshot()
        self.root.rename(self.base / "claude-501.old")  # keeps the recorded directory, so the inode differs
        (self.root / self.name).mkdir(parents=True)
        self.clean(snapshot)
        self.assertTrue((self.root / self.name).is_dir())

    def test_an_entry_replaced_by_a_link_is_never_followed(self):
        snapshot = self.snapshot()
        (self.root / self.name).symlink_to(self.outside / self.name)
        self.clean(snapshot)
        self.assertOutsideIntact()
        self.assertTrue((self.root / self.name).is_symlink())

    def test_a_root_that_is_a_link_before_the_native_starts_records_nothing(self):
        self.root.symlink_to(self.outside)
        self.assertIsNone(self.snapshot())
        self.clean(None)
        self.assertOutsideIntact()
        self.assertEqual(os.listdir(self.outside), [self.name])  # nothing was created through the link


class ProtectedRefusals(Kit):
    """Refusals of the real launcher before the session starts (the version check and the pre-launch
    refresh, where a case reaches them, run confined)."""

    def setUp(self):
        super().setUp()
        self.enroll()
        self.write_config('[claude_auth.accounts.N]\nmode = "native-login"\n')
        for name in (".auth", ""):
            Path(str(self.record) + name).unlink(missing_ok=True)

    def refused(self, args, env=None, protected=True):
        code, output = self.terminal(["--reserve-5h", "10", "N", *args], env=env)
        self.assertEqual(code, 1, output)
        if protected:
            self.assertIn("protected mode refused", output)
        self.assertFalse(self.record.exists())
        return output

    def test_unsupported_native_forms_refuse(self):
        if boundary_problem():
            self.skipTest(boundary_problem())
        for args, text in ((["-p", "hi"], "-p is not an interactive form"), (["--resume"], "--resume is not"),
                           (["--settings", "{}"], "--settings is not"), (["mcp", "list"], "claude mcp command"),
                           (["--model", "x", "--", "doctor"], "claude doctor command"),
                           (["--mcp-config=x"], "--mcp-config is not"), (["--fork-session"], "--fork-session is not")):
            with self.subTest(args):
                self.assertIn(text, self.refused(args))
        # The launcher's own screen refuses these before protected mode looks at them.
        for args, text in ((["-pc"], "unsupported/combined option"), (["--bg"], "unsupported/combined option"),
                           (["--remote-control"], "CLI option changes authentication")):
            with self.subTest(args):
                self.assertIn(text, self.refused(args, protected=False))

    def test_unreviewed_native_versions_refuse(self):
        if boundary_problem():
            self.skipTest(boundary_problem())
        output = self.refused([], env=dict(self.env, CLAUDE_FAKE_VERSION="2.1.323 (Claude Code)"))
        self.assertIn("Claude Code 2.1.323 is not a version this kit release reviewed", output)

    def test_store_and_proxy_selectors_in_the_environment_refuse(self):
        if boundary_problem():
            self.skipTest(boundary_problem())
        self.assertIn("CLAUDE_SECURESTORAGE_CONFIG_DIR",
                      self.refused([], env=dict(self.env, CLAUDE_SECURESTORAGE_CONFIG_DIR="")))
        self.assertIn("unsupported HTTPS_PROXY", self.refused([], env=dict(self.env, HTTPS_PROXY="socks5://x:1")))

    def test_unreviewed_server_managed_policy_refuses_with_key_names_only(self):
        if boundary_problem():
            self.skipTest(boundary_problem())
        limits = self.native_config("N") / "policy-limits.json"
        limits.write_text(json.dumps(dict(ORG_LIMITS, secret_limit={"value": "org-sensitive-value"})))
        limits.chmod(0o600)
        output = self.refused([])
        self.assertIn("policy-limits.json: secret_limit", output)
        self.assertNotIn("org-sensitive-value", output)

    def test_the_launchers_own_lines_are_styled_for_stderr_not_for_the_terminal_on_stdout(self):
        if boundary_problem():
            self.skipTest(boundary_problem())
        log = self.base / "stderr.log"  # no credential is stored: the gate refuses after the start line
        argv = ["/bin/sh", "-c", 'exec "$@" 2>"$0"', str(log), str(self.kit / "bin/claude-as"), "--reserve-5h", "10", "N"]
        code, output = self.terminal([], env=dict(self.env, CODING_AGENT_KIT_COLOR="auto", TERM="xterm"), argv=argv)
        self.assertEqual(code, 1, output)
        text = log.read_text()
        self.assertIn("claude-as: usage reserves for N: five-hour 10% (--reserve-5h)", text)
        self.assertIn("protected mode refused", text)
        self.assertNotIn("\x1b[", text)  # a redirected log stays plain

    def test_the_native_gets_the_same_umask_as_in_an_unprotected_launch(self):
        """claude-as runs under umask 077 in both modes, so with the caller's umask at 022 a file the
        native writes is 0600 in a protected launch as in an unprotected one. The protected native run
        here is the pre-launch refresh (`claude auth status`, confined like the TUI); no credential is
        stored, so the gate refuses right after it, before any read."""
        if boundary_problem():
            self.skipTest(boundary_problem())
        record = self.cwd / "record.json"  # in the working directory, which the boundary can write
        env = dict(self.env, FAKE_RECORD=str(record))
        old = os.umask(0o022)
        try:
            code, output = self.terminal(["--reserve-5h", "10", "N"], env=env)
            unprotected = self.run_kit(["N"], env=env)
        finally:
            os.umask(old)
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused", output)
        refreshed = Path(str(record) + ".auth")
        self.assertEqual(stat.S_IMODE(refreshed.stat().st_mode), 0o600)
        self.assertEqual(unprotected.returncode, 0, unprotected.stderr)
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)

    def test_the_version_check_runs_the_native_inside_the_boundary_only(self):
        """The kit never runs the native outside the boundary: its --version check runs confined like
        the pre-launch refresh, so a native a protected session replaced would run confined too. The
        refresh runs here (the version check passed), then the gate refuses: no credential is stored."""
        if boundary_problem():
            self.skipTest(boundary_problem())
        outside = self.base / "outside.log"  # not writable inside the boundary
        env = dict(self.env, FAKE_OUTSIDE_MARK=str(outside), FAKE_RECORD=str(self.cwd / "record.json"))
        code, output = self.terminal(["--reserve-5h", "10", "N"], env=env)
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused", output)
        self.assertTrue((self.cwd / "record.json.auth").exists(), output)  # the refresh ran: the version passed
        self.assertFalse(outside.exists(), outside.read_text() if outside.exists() else "")
        self.assertEqual(self.run_kit(["N", "--version"], env=env).returncode, 0)  # unprotected: runs outside
        self.assertEqual(outside.read_text(), '["--version"]\n')

    def test_a_native_the_boundary_can_replace_refuses_before_it_runs(self):
        """A Claude Code program in reach of the boundary (by any name on its way, or the interpreter of
        its #! line) refuses before anything runs: a protected session could replace it under the next
        launch, protected or not."""
        if boundary_problem():
            self.skipTest(boundary_problem())
        source = (self.bin / "claude").read_text()
        outside = self.base / "outside.log"
        local = self.native_config("N") / "local"  # Claude Code's older local install, in the account directory
        local.mkdir()
        (local / "claude").write_text(source)
        (local / "claude").chmod(0o700)
        linked = self.base / "linked"
        linked.mkdir()
        (linked / "claude").symlink_to(local / "claude")
        project = self.cwd / "node_modules/.bin"  # a project-local install, first on PATH
        project.mkdir(parents=True)
        shutil.copy2(self.bin / "claude", project / "claude")
        node = self.base / "node"  # a #!/usr/bin/env interpreter: in reach first, then outside
        node.mkdir()
        interpreter = f"#!{sys.executable}\nimport os, sys\nos.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
        enveloped = self.base / "enveloped"
        enveloped.mkdir()
        (enveloped / "claude").write_text("#!/usr/bin/env fakenode\n" + source.split("\n", 1)[1])
        (enveloped / "claude").chmod(0o700)
        for path in (project / "fakenode", node / "fakenode"):
            path.write_text(interpreter)
            path.chmod(0o700)
        cases = ((linked, str(linked / "claude"), str(self.native_config("N"))),  # named as run, linked inside
                 (project, str(project / "claude"), str(self.cwd)),
                 (f"{enveloped}:{project}:{node}", str(project / "fakenode"), str(self.cwd)))
        for path, inside, root in cases:
            with self.subTest(path=path):
                env = dict(self.env, PATH=f"{path}:{self.env['PATH']}", FAKE_OUTSIDE_MARK=str(outside))
                output = self.refused([], env=env).replace("\n", " ")
                self.assertIn("lies inside %s, which the boundary can write; run claude-as with a Claude Code installed "
                              "outside the working directory" % root, output)
                self.assertIn(inside, output)
                self.assertFalse(outside.exists())
                self.assertFalse((self.cwd / "record.json.auth").exists())
        env = dict(self.env, PATH=f"{enveloped}:{node}:{self.env['PATH']}", FAKE_OUTSIDE_MARK=str(outside),
                   FAKE_RECORD=str(self.cwd / "record.json"))
        code, output = self.terminal(["--reserve-5h", "10", "N"], env=env)  # the interpreter out of reach
        self.assertEqual(code, 1, output)
        self.assertNotIn("which the boundary can write", output)
        self.assertTrue((self.cwd / "record.json.auth").exists(), output)
        self.assertFalse(outside.exists())

    def test_home_as_the_working_directory_refuses(self):
        if boundary_problem():
            self.skipTest(boundary_problem())
        self.cwd = self.home
        self.assertIn("cannot be writable in protected mode", self.refused([]))

    def test_a_kit_inside_the_working_directory_refuses_before_anything_starts(self):
        if boundary_problem():
            self.skipTest(boundary_problem())
        kit = self.cwd / "kit"
        shutil.copytree(self.kit, kit)
        code, output = self.terminal([], argv=[str(kit / "bin/claude-as"), "--reserve-5h", "10", "N"])
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused: the kit directory %s lies inside %s, which the boundary can write; run "
                      "claude-as with a Python and a kit installed outside the working directory, every --add-dir and "
                      "the account directory" % (kit / "lib/claude-as", self.cwd), output.replace("\n", " "))
        self.assertFalse(self.record.exists())

    def test_an_add_dir_holding_the_kit_or_the_python_refuses(self):
        if boundary_problem():
            self.skipTest(boundary_problem())
        probe = "import os, sys; print(os.path.realpath(getattr(sys, '_base_executable', None) or sys.executable))"
        python = subprocess.run(["python3", "-I", "-c", probe], env=self.env, capture_output=True, text=True,
                                check=True).stdout.strip()  # the interpreter the launcher's kit processes run
        sub = self.kit / "lib/coding-agent-kit/sub"
        sub.mkdir()
        for add_dir, text in ((self.kit, "the kit directory %s lies inside %s" % (self.kit / "lib/claude-as", self.kit)),
                              (sub, "%s, which the boundary can write, lies inside the kit directory %s"
                               % (sub, self.kit / "lib/coding-agent-kit")),
                              (Path(python).parent, "the Python %s lies inside %s" % (python, Path(python).parent))):
            with self.subTest(add_dir=add_dir):
                self.assertIn(text, self.refused(["--add-dir", str(add_dir)]).replace("\n", " "))


class ConnectProxy:
    """A plain HTTP CONNECT proxy on 127.0.0.1 that tunnels to loopback ports only and records the
    targets it was asked for."""

    def __init__(self):
        import threading
        self.threading = threading
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.port = self.listener.getsockname()[1]
        self.targets = []
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.listener.close()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            self.threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        upstream = None
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                data = client.recv(1)
                if not data:
                    return
                head += data
            method, target = head.split(b" ")[:2]
            self.targets.append(target.decode())
            host, _, port = target.decode().rpartition(":")
            if method != b"CONNECT" or host != "127.0.0.1":
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            upstream = socket.create_connection((host, int(port)), timeout=10)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.threading.Thread(target=self._pump, args=(upstream, client), daemon=True).start()
            self._pump(client, upstream)
        except (OSError, ValueError):
            pass
        finally:
            client.close()
            if upstream is not None:
                upstream.close()

    @staticmethod
    def _pump(source, sink):
        try:
            while True:
                data = source.recv(65536)
                if not data:
                    break
                sink.sendall(data)
        except OSError:
            pass
        finally:
            for sock in (source, sink):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class Protected(Kit):
    """A protected launch end to end: the real boundary, supervisor, gate and monitor, a fake native
    and the loopback Claude upstream."""

    def setUp(self):
        problem = boundary_problem()
        if problem:
            self.skipTest("protected-mode boundary unavailable: " + problem)
        super().setUp()
        self.enroll()
        self.record = self.cwd / "record.json"
        self.env["FAKE_RECORD"] = str(self.record)
        self.store()
        self.upstream = upstream_fixture.ClaudeUpstream()
        self.addCleanup(self.upstream.close)

    def store(self, subscription="max", expires_in=3600):
        credential = self.native_config("N") / ".credentials.json"
        credential.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": upstream_fixture.BEARER_A, "refreshToken": "fabricated-refresh",
            "expiresAt": int(time.time() * 1000) + expires_in * 1000, "scopes": ["user:inference", "user:profile"],
            "subscriptionType": subscription}}))
        credential.chmod(0o600)

    def protected(self, args=(), env=None, reserves=None, timeout=90, answers=(), python=sys.executable):
        security = self.base / "security"  # macOS: no Keychain item, so the store file is read
        security.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(44)\n")
        security.chmod(0o700)
        spec = {"kit": str(self.kit), "account": "N", "reserves": reserves or {"five_hour": "10", "weekly": "5"},
                "args": list(args), "upstream_port": self.upstream.port, "security": str(security)}
        argv = [python, "-I", "-B", str(FIXTURES / "run_protected.py"), json.dumps(spec)]
        return self.terminal([], answers, env=dict(self.env, **(env or {})), argv=argv, timeout=timeout)

    def test_a_passing_check_runs_the_native_behind_the_gate_with_its_own_pid(self):
        code, output = self.protected(["--model", "claude-sonnet-4-5"], env={
            "FAKE_MESSAGES": "2", "FAKE_BEARER": upstream_fixture.BEARER_A, "FAKE_DIRECT": str(self.upstream.port)})
        self.assertEqual(code, 0, output)
        self.assertIn("fake claude sentinel", output)
        seen = self.recorded()
        self.assertEqual([status for status, _ in seen["statuses"]], [200, 200])
        env = seen["env"]
        base = env["ANTHROPIC_BASE_URL"]
        self.assertRegex(base, r"^http://127\.0\.0\.1:[0-9]+$")
        self.assertEqual(env["HTTPS_PROXY"], base)
        self.assertEqual(env["CODING_AGENT_KIT_CLAUDE_PID"], str(seen["pid"]))
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(self.native_config("N")))
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)
        self.assertNotIn("CODING_AGENT_KIT_USAGE_FILE", env)  # the helper cannot write it inside the boundary
        self.assertEqual(env["CLAUDE_DISABLE_ADOPT"], "1")  # no "Move to background and exit"
        self.assertEqual((env["NO_PROXY"], env["no_proxy"]), ("127.0.0.1", "127.0.0.1"))  # the gate, not by proxy
        self.assertFalse(os.path.exists(env["TMPDIR"]))  # the private temporary directory is gone
        self.assertEqual(seen["args"][0], "--settings")
        settings = json.loads(seen["args"][1])
        self.assertEqual(settings, {"sandbox": {"enabled": False}, "remoteControlAtStartup": False,
                                    "env": {"ANTHROPIC_BASE_URL": base}})
        self.assertEqual(seen["args"][2:], ["--model", "claude-sonnet-4-5"])
        self.assertNotEqual(seen["direct"], "connected")  # only the gate port is reachable
        refresh = json.loads(Path(str(self.record) + ".auth").read_text())
        self.assertEqual(refresh["env"]["HTTPS_PROXY"].rsplit(":", 1)[1], base.rsplit(":", 1)[1])
        models = self.upstream.model_requests()
        self.assertEqual(len(models), 2)
        for request in models:
            self.assertIn(("Authorization", "Bearer " + upstream_fixture.BEARER_A), request["headers"])
        reads = self.upstream.paths("GET")
        self.assertEqual(reads[:2], ["/api/oauth/profile", "/api/oauth/usage"])
        self.assertIn("usage reserves for N: five-hour 10% (--reserve-5h), weekly 5% (--reserve-weekly)", output)
        expires = json.loads((self.native_config("N") / ".credentials.json").read_text())["claudeAiOauth"]["expiresAt"]
        # A relaunch renews the token only once it is due (native's refresh window) or gone, so the line
        # says when the session stops working and when relaunching helps, never "relaunch before then".
        self.assertIn("claude-as: the account's access token expires at %s, and Claude Code cannot renew it inside "
                      "protected mode, so model requests stop then unless another session of this account renews "
                      "it first; to go on after that, exit and launch again, whose pre-launch refresh renews the "
                      "token within its last five minutes or once it expired."
                      % reserve.timestamp(expires // 1000), output.replace("\r\n", " ").replace("\n", " "))

    def test_a_window_at_its_reserve_refuses_before_the_native_starts(self):
        self.upstream.used["five_hour"] = 95
        code, output = self.protected()
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused", output)
        self.assertIn("five-hour window 5% remaining, at or below its 10% reserve", output)
        self.assertFalse(self.record.exists())
        self.assertTrue(Path(str(self.record) + ".auth").exists())  # the refresh ran first
        self.assertEqual(self.upstream.model_requests(), [])

    def test_another_accounts_bearer_is_refused_with_zero_model_bytes(self):
        code, output = self.protected(env={"FAKE_MESSAGES": "2", "FAKE_BEARER": upstream_fixture.BEARER_B})
        self.assertEqual(code, 0, output)
        statuses = self.recorded()["statuses"]
        self.assertEqual([status for status, _ in statuses], [400, 400])
        self.assertIn("bearer_identity_mismatch", statuses[0][1])
        self.assertEqual(self.upstream.model_requests(), [])
        self.assertIn("quota reserve paused (bearer_identity_mismatch)", output)

    def test_an_organization_account_refuses_before_the_native_even_with_policy_caches(self):
        self.store("enterprise")
        for name, document in (("remote-settings.json", {}), ("policy-limits.json", ORG_LIMITS)):
            path = self.native_config("N") / name
            path.write_text(json.dumps(document))
            path.chmod(0o600)
        code, output = self.protected()
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused: N is an organization account (its subscription is not Pro or Max, "
                      "or not known), so Claude Code would apply the organization's server-managed policy, but it "
                      "applies none while its requests go through the request gate", output)
        self.assertIn("launch without reserves (claude-as N)", output)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.upstream.paths(), [])

    def test_a_threshold_poll_pauses_mid_run_and_f12_with_the_code_continues(self):
        """The composed pause and continuation: a near-reserve poll (15 s) finds the window at its reserve,
        the native's next request gets the 400 refusal, and F12 plus the shown code on the launcher's
        terminal runs a continuation check with the store's bearer, after which requests pass again."""
        self.upstream.used["five_hour"] = 85  # 15% left against a 10% reserve: polls every 15 s
        go, state = self.cwd / "paused", {}

        def crossed(output):
            self.upstream.used["five_hour"] = 95
            return b""

        def paused(output):
            go.write_text("")
            return b""

        def trigger(output):
            self.upstream.used["five_hour"] = 50
            state["reads"] = len(self.upstream.paths("GET"))
            return b"\x1b[24~"

        def code(output):
            return re.findall(rb"type ([0-9a-f]{8}) then Enter to request a fresh quota check", output)[-1] + b"\r"

        status, output = self.protected(env={"FAKE_PAUSE_CYCLE": str(go), "FAKE_BEARER": upstream_fixture.BEARER_A},
                                        answers=[("fake claude admitted", crossed),
                                                 ("quota reserve paused (threshold)", paused),
                                                 ("fake claude refused", trigger),
                                                 ("then Enter to request a fresh quota check", code)], timeout=120)
        self.assertEqual(status, 0, output)
        self.assertIn("<quota reserve paused (threshold)>; account N; window five-hour; remaining 5% (reserve 10%)",
                      output)  # <...> marks where an answer was matched
        self.assertIn("fresh quota check passed; model requests resume", output)
        cycle = self.recorded()["statuses"]
        self.assertEqual(([s for s, _ in cycle[:2]], cycle[-1][0]), ([200, 400], 200))
        self.assertTrue(all(s == 400 and "quota reserve" in body for s, body in cycle[1:-1]), cycle)
        self.assertIn("threshold", cycle[1][1])
        self.assertEqual(len(self.upstream.model_requests()), 2)  # the refused requests sent no model bytes
        continuation = [r for r in self.upstream.requests if r["method"] == "GET"][state["reads"]:]
        self.assertEqual([r["path"] for r in continuation][:2], ["/api/oauth/profile", "/api/oauth/usage"])
        self.assertIn(("Authorization", "Bearer " + upstream_fixture.BEARER_A), continuation[0]["headers"])

    def test_a_native_killed_by_a_signal_ends_the_launcher_by_it_after_the_cleanup(self):
        code, output = self.protected(env={"FAKE_SIGNAL": str(int(signal.SIGTERM))})
        self.assertEqual(code, -signal.SIGTERM, output)
        self.assertFalse(os.path.exists(self.recorded()["env"]["TMPDIR"]))

    def test_a_failing_pre_launch_refresh_refuses(self):
        code, output = self.protected(env={"CLAUDE_FAKE_AUTH_STATUS": "1"})
        self.assertEqual(code, 1, output)
        self.assertIn("the pre-launch native refresh (claude auth status) failed (1)", output)
        self.assertEqual(self.upstream.paths(), [])

    def test_a_token_the_refresh_did_not_renew_refuses_before_any_read(self):
        """claude auth status exits 0 whether or not it renewed the token; the gate's read decides."""
        self.store(expires_in=120)
        code, output = self.protected()
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused: the pre-launch refresh (claude auth status) did not renew the "
                      "account's access token, which expires within five minutes; run CLAUDE_CONFIG_DIR=%s claude "
                      "auth status yourself to see why." % self.native_config("N"),
                      output.replace("\r\n", " ").replace("\n", " "))
        self.assertTrue(Path(str(self.record) + ".auth").exists())  # the refresh ran
        self.assertFalse(self.record.exists())
        self.assertEqual(self.upstream.paths(), [])

    def test_a_token_too_close_to_its_expiry_for_a_session_refuses(self):
        self.store(expires_in=600)  # outside native's window, so the refresh leaves it alone
        code, output = self.protected()
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused: the account's access token expires within fifteen minutes, too soon "
                      "for a protected session, and Claude Code renews it only in its last five minutes; launch again "
                      "in up to ten minutes, when the pre-launch refresh renews it, or launch without reserves",
                      output.replace("\r\n", " ").replace("\n", " "))
        self.assertFalse(self.record.exists())
        self.assertEqual(self.upstream.paths(), [])

    def test_a_refresh_that_renews_a_near_expiry_token_lets_the_launch_run(self):
        self.store(expires_in=120)
        code, output = self.protected(env={"CLAUDE_FAKE_RENEW": "3600", "CLAUDE_FAKE_RENEW_TOKEN":
                                           upstream_fixture.BEARER_A2, "FAKE_MESSAGES": "1",
                                           "FAKE_BEARER": upstream_fixture.BEARER_A2})
        self.assertEqual(code, 0, output)
        self.assertEqual([status for status, _ in self.recorded()["statuses"]], [200])
        profile = [r for r in self.upstream.requests if r["path"] == "/api/oauth/profile"][0]
        self.assertIn(("Authorization", "Bearer " + upstream_fixture.BEARER_A2), profile["headers"])

    def test_an_unreadable_store_refuses_at_gate_startup(self):
        (self.native_config("N") / ".credentials.json").chmod(0o644)
        code, output = self.protected()
        self.assertEqual(code, 1, output)
        self.assertIn("credential_credential_not_private", output)
        self.assertEqual(self.upstream.paths(), [])

    def test_the_gate_reaches_upstream_through_http_proxy_as_claude_code_would(self):
        """With no HTTPS_PROXY, Claude Code uses HTTP_PROXY for HTTPS and only then ALL_PROXY, so the gate
        does too (an unusable ALL_PROXY would refuse under any other order); the native never sees the
        caller's proxy."""
        proxy = ConnectProxy()
        self.addCleanup(proxy.close)
        for name in ("NO_PROXY", "no_proxy"):  # a caller's loopback exemption would send the gate direct
            self.env.pop(name, None)
        code, output = self.protected(env={"HTTP_PROXY": "http://127.0.0.1:%d" % proxy.port,
                                           "ALL_PROXY": "socks5h://127.0.0.1:1", "FAKE_MESSAGES": "1",
                                           "FAKE_BEARER": upstream_fixture.BEARER_A})
        self.assertEqual(code, 0, output)
        seen = self.recorded()
        self.assertEqual([status for status, _ in seen["statuses"]], [200])
        self.assertIn("127.0.0.1:%d" % self.upstream.port, proxy.targets)  # quota reads and the model request
        self.assertEqual(sorted(name for name in seen["env"] if name.lower().endswith("_proxy")),
                         ["HTTPS_PROXY", "NO_PROXY", "https_proxy", "no_proxy"])
        self.assertEqual(seen["env"]["HTTPS_PROXY"], seen["env"]["ANTHROPIC_BASE_URL"])  # only the gate

    def workspace_venv(self, venv, mark):
        """A virtual environment at VENV whose .pth file and ps record in MARK every run outside the
        boundary of a process other than the launcher; the PATH that puts it first."""
        made = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], capture_output=True,
                              timeout=120)
        if made.returncode:
            self.skipTest("python3 -m venv is unavailable")
        (site,) = venv.glob("lib/python*/site-packages")
        # What a boundary process can write into a workspace venv. The launcher (the test driver) has
        # FAKE_RECORD set; a kit process outside the boundary (it gets gate_env only) that processed this
        # file would add a line to the mark.
        (site / "planted.pth").write_text('import os; "FAKE_RECORD" in os.environ or open(%r, "a").write("pth %%d\\n" '
                                          '%% os.getpid())\n' % str(mark))
        ps = venv / "bin/ps"  # what a launcher on macOS would run every second if it ran ps by name
        ps.write_text("#!/bin/sh\necho ps >> '%s'\nexec /bin/ps \"$@\"\n" % mark)
        ps.chmod(0o755)
        subprocess.run([str(venv / "bin/python3"), "-I", "-B", "-c", "pass"], env={}, check=True, timeout=30)
        self.assertTrue(mark.exists())  # the venv's own interpreter runs it
        mark.unlink()
        return os.pathsep.join((str(venv / "bin"), self.env["PATH"]))

    def test_a_workspace_venv_first_on_path_refuses_before_anything_starts(self):
        """A python3 the boundary can write could take over the next launch outside every boundary, before
        any check, so a launch through one refuses."""
        venv = self.cwd / ".venv"
        mark = self.base / "planted.log"  # outside every writable path: only an unconfined process can write it
        path = self.workspace_venv(venv, mark)
        code, output = self.protected(env={"PATH": path}, python=str(venv / "bin/python3"))
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused: the launcher's Python %s lies inside %s, which the boundary can write; "
                      "start claude-as with a python3 outside the working directory, every --add-dir and the account "
                      "directory first on PATH (deactivate a workspace virtual environment)." % (venv / "bin/python3",
                                                                                                self.cwd),
                      output.replace("\n", " "))
        self.assertFalse(self.record.exists())
        self.assertFalse(Path(str(self.record) + ".auth").exists())  # no pre-launch refresh ran
        self.assertEqual(self.upstream.paths(), [])
        self.assertFalse(mark.exists(), mark.read_text() if mark.exists() else "")

    def test_a_venv_first_on_path_never_runs_planted_code_outside_the_boundary(self):
        """With a virtual environment outside the boundary's reach first on PATH, the gate and the monitor
        (the base interpreter) never process its .pth files, and nothing runs its ps."""
        venv = self.base / "venv"
        mark = self.base / "planted.log"
        path = self.workspace_venv(venv, mark)
        code, output = self.protected(env={"PATH": path, "FAKE_MESSAGES": "1", "FAKE_BEARER": upstream_fixture.BEARER_A},
                                      python=str(venv / "bin/python3"))
        self.assertEqual(code, 0, output)
        seen = self.recorded()
        self.assertEqual([status for status, _ in seen["statuses"]], [200])
        self.assertEqual(seen["env"]["PATH"], path)  # the native keeps the caller's PATH
        self.assertFalse(mark.exists(), mark.read_text() if mark.exists() else "")


def reviewed_claude():
    """The real Claude Code to run, or None: KIT_REAL_CLAUDE (a path) or claude on PATH, used only when
    its --version (asked with a scratch HOME) is one protect.REVIEWED_VERSIONS names."""
    candidate = os.environ.get("KIT_REAL_CLAUDE") or shutil.which("claude")
    if not candidate or not os.access(candidate, os.X_OK):
        return None
    with tempfile.TemporaryDirectory() as home:
        try:
            result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=30, check=False,
                                    stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "HOME": home,
                                                                   "DISABLE_AUTOUPDATER": "1"})
        except (OSError, subprocess.TimeoutExpired):
            return None
    match = protect.VERSION_LINE.fullmatch(result.stdout.strip())
    return os.path.realpath(candidate) if match and match.group(1) in protect.REVIEWED_VERSIONS else None


ANSI = re.compile(rb"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78]")


class Screen:
    """A real TUI on a PTY of its own: answers the terminal queries Claude Code sends at startup, and
    matches text with escape sequences and whitespace removed."""

    ANSWERS = ((b"\x1b]10;?", b"\x1b]10;rgb:cccc/cccc/cccc\x1b\\"), (b"\x1b]11;?", b"\x1b]11;rgb:0000/0000/0000\x1b\\"),
               (b"\x1b[6n", b"\x1b[1;1R"), (b"\x1b[c", b"\x1b[?62;22c"), (b"\x1b[?u", b"\x1b[?0u"))

    def __init__(self, argv, env, cwd):
        self.pid, self.fd = spawn_on_terminal(argv, env, cwd, size=(40, 120))
        self.raw, self.status = b"", None

    def read(self, timeout):
        if not select.select([self.fd], [], [], timeout)[0]:
            return True
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            data = b""
        self.raw += data
        for query, reply in self.ANSWERS:
            for _ in range(data.count(query)):
                os.write(self.fd, reply)
        return bool(data)

    def text(self):
        return re.sub(r"\s+", "", ANSI.sub(b" ", self.raw).decode("utf-8", "replace"))

    def expect(self, words, timeout=60):
        wanted = re.sub(r"\s+", "", words)
        deadline = time.monotonic() + timeout
        while wanted not in self.text():
            if time.monotonic() > deadline or not self.read(0.1):
                raise AssertionError("expected %r; terminal text:\n%s" % (words, self.text()[-3000:]))

    def type(self, line):
        os.write(self.fd, line.encode())
        time.sleep(0.5)
        os.write(self.fd, b"\r")

    def wait(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = status
                while self.read(0.05) and select.select([self.fd], [], [], 0)[0]:
                    pass
                return os.waitstatus_to_exitcode(status)
            self.read(0.05)
        raise AssertionError("the launcher did not exit; terminal text:\n%s" % self.text()[-3000:])

    def close(self):
        if self.status is None:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
        os.close(self.fd)


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


REAL_CLAUDE = None if sys.platform == "darwin" else reviewed_claude()
DENIED = (errno.EACCES, errno.EPERM)


@unittest.skipIf(sys.platform == "darwin", "a real Claude Code reads and writes the login Keychain on macOS")
@unittest.skipUnless(REAL_CLAUDE, "needs a reviewed Claude Code (%s) as KIT_REAL_CLAUDE or claude on PATH"
                     % ", ".join(protect.REVIEWED_VERSIONS))
class RealClaude(Kit):
    """The reviewed Claude Code itself in protected mode: the real boundary, gate and monitor, a
    fabricated credential and the loopback Messages upstream; no account and no real provider."""

    def setUp(self):
        problem = boundary_problem()
        if problem:
            self.skipTest("protected-mode boundary unavailable: " + problem)
        super().setUp()
        self.enroll()  # with the fake native, which records the login; the real one runs from here on
        (self.bin / "claude").unlink()
        (self.bin / "claude").symlink_to(REAL_CLAUDE)
        for name in ("cache", "data"):
            (self.base / name).mkdir()
        self.env.update(DISABLE_AUTOUPDATER="1", LANG="C.UTF-8", TERM="xterm-256color",
                        XDG_CACHE_HOME=str(self.base / "cache"), XDG_DATA_HOME=str(self.base / "data"))
        self.config_dir = self.native_config("N")
        self.upstream = upstream_fixture.ClaudeUpstream()
        self.upstream.messages = True
        self.addCleanup(self.upstream.close)
        # The refresh gate's tunnel leads to a bound loopback port that never listens, so a refresh native
        # might start fails with a refused connection instead of reaching platform.claude.com.
        self.closed = socket.socket()
        self.addCleanup(self.closed.close)
        self.closed.bind(("127.0.0.1", 0))
        inside = [sys.executable, "-I", str(FIXTURES / "inside.py")]
        probe = lambda kind: [*inside, kind, str(self.cwd), str(self.upstream.port), str(self.home)]
        self.private(".claude.json", {
            "hasCompletedOnboarding": True, "theme": "dark",
            "projects": {str(self.cwd): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}},
            "mcpServers": {"fixture": {"type": "stdio", "command": probe("mcp")[0], "args": probe("mcp")[1:]}},
            "oauthAccount": {"accountUuid": ACCOUNT_UUID, "organizationUuid": ORGANIZATION_UUID,
                             "emailAddress": "fixture@example.invalid"}})
        self.private("settings.json", {"permissions": {"allow": ["Bash"]}, "hooks": {
            event: [{"hooks": [{"type": "command", "command": " ".join(probe(kind))}]}]
            for event, kind in (("SessionStart", "session"), ("UserPromptSubmit", "prompt"))}})

    def private(self, name, document):
        path = self.config_dir / name
        path.write_text(json.dumps(document))
        path.chmod(0o600)

    def store(self, token, subscription, hours=1):
        self.private(".credentials.json", {"claudeAiOauth": {
            "accessToken": token, "refreshToken": "fabricated-refresh-" + token[-4:],
            "expiresAt": int(time.time() * 1000 + hours * 3600_000),
            "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"], "subscriptionType": subscription}})

    def spec(self, args=()):
        spec = {"kit": str(self.kit), "account": "N", "reserves": {"five_hour": "10", "weekly": "5"},
                "args": ["--debug-file", str(self.cwd / "native-debug.log"), "--permission-mode", "default", *args],
                "upstream_port": self.upstream.port,
                "connect_allow": {"platform.claude.com:443": ["127.0.0.1", self.closed.getsockname()[1]]}}
        return [sys.executable, "-I", "-B", str(FIXTURES / "run_protected.py"), json.dumps(spec)]

    def launch(self, args=()):
        screen = Screen(self.spec(args), self.env, self.cwd)
        self.addCleanup(screen.close)
        screen.expect("Claude Code", 90)
        screen.expect("v" + protect.REVIEWED_VERSIONS[0], 30)
        return screen

    def models(self, since=0):
        found = []
        for request in self.upstream.model_requests()[since:]:
            body = json.loads(request["body"])
            bearer = dict((k.lower(), v) for k, v in request["headers"])["authorization"][7:]
            found.append((body.get("model"), body.get("max_tokens"), bool(body.get("tools")), bearer))
        return found

    def inside(self, kind):
        self.assertTrue(wait_for(lambda: (self.cwd / ("inside-%s.json" % kind)).exists(), 30), kind)
        return json.loads((self.cwd / ("inside-%s.json" % kind)).read_text())

    def test_a_token_the_real_refresh_does_not_renew_refuses_before_the_native_starts(self):
        """The real `claude auth status` exits 0 either way: inside its five-minute window it tries to
        renew (the tunnel leads to a closed port, so it fails); just outside the window it renews
        nothing. The gate's read after it refuses both, and no native session starts."""
        for minutes, text in ((2, "the pre-launch refresh (claude auth status) did not renew the account's access "
                                  "token, which expires within five minutes"),
                              (10, "the account's access token expires within fifteen minutes, too soon for a "
                                   "protected session")):
            with self.subTest(minutes=minutes):
                self.store(upstream_fixture.BEARER_A, "max", hours=minutes / 60)
                code, output = self.terminal([], argv=self.spec(), timeout=120)
                self.assertEqual(code, 1, output)
                self.assertIn("protected mode refused: " + text, output.replace("\n", " "))
                self.assertEqual(self.upstream.model_requests(), [])

    def test_an_organization_account_is_refused_before_the_native_starts(self):
        """The real `claude auth status` runs behind the refresh gate; the gate then refuses the
        enterprise subscription, so no native session starts and no model request is made."""
        self.store(upstream_fixture.BEARER_A, "enterprise")
        for name, document in (("remote-settings.json", {"permissions": {"deny": ["Bash"]}}),
                               ("policy-limits.json", ORG_LIMITS)):
            self.private(name, document)
        spec = {"kit": str(self.kit), "account": "N", "reserves": {"five_hour": "10", "weekly": "5"}, "args": [],
                "upstream_port": self.upstream.port,
                "connect_allow": {"platform.claude.com:443": ["127.0.0.1", self.closed.getsockname()[1]]}}
        code, output = self.terminal([], argv=[sys.executable, "-I", "-B", str(FIXTURES / "run_protected.py"),
                                               json.dumps(spec)], timeout=90)
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused: N is an organization account", output)
        self.assertEqual(self.upstream.requests, [])

    def test_the_native_with_hooks_mcp_and_a_background_tool_reaches_only_the_gate(self):
        self.store(upstream_fixture.BEARER_A, "max")
        self.upstream.bash = "echo $$ > %s/bash-pid.txt; echo $TMPDIR > %s/bash-tmpdir.txt; exec sleep 120" % (
            self.cwd, self.cwd)
        screen = self.launch()
        self.assertEqual(self.upstream.paths("GET")[:2], ["/api/oauth/profile", "/api/oauth/usage"])
        for kind in ("session", "mcp"):  # started by native at startup, inside the boundary
            seen = self.inside(kind)
            self.assertIn(seen["direct"], DENIED, kind)
            self.assertIn(seen["home_write"], DENIED, kind)
            private_tmp = seen["tmpdir"]
            self.assertTrue(os.path.basename(private_tmp).startswith("claude-as-"), private_tmp)
        screen.type("fixture prompt one")
        screen.expect("FIXTURE-ANSWER")
        seen = self.inside("prompt")
        self.assertEqual((seen["direct"] in DENIED, seen["home_write"] in DENIED, seen["tmpdir"]), (True, True, private_tmp))
        screen.type("fixture background")
        screen.expect("FIXTURE-DONE")
        self.assertTrue(wait_for(lambda: (self.cwd / "bash-tmpdir.txt").exists(), 20))
        self.assertEqual((self.cwd / "bash-tmpdir.txt").read_text().strip(), private_tmp)
        shell = int((self.cwd / "bash-pid.txt").read_text())
        screen.type("/exit")
        screen.expect("Background work is running")
        screen.expect("Exit and stop tasks")
        screen.expect("Stay")
        self.assertNotIn("Movetobackground", screen.text())  # CLAUDE_DISABLE_ADOPT=1
        os.write(screen.fd, b"\r")
        self.assertEqual(screen.wait(60), 0, screen.text()[-2000:])
        models = self.models()
        self.assertTrue(any(tokens == 1 and not tools for _, tokens, tools, _ in models), models)  # startup probe
        self.assertTrue(any(not tools and tokens != 1 for _, tokens, tools, _ in models), models)  # session title
        self.assertTrue(any(tools for _, _, tools, _ in models), models)  # the main thread, default model
        for model, _, _, bearer in models:
            self.assertIn(provider.family(model), provider.FAMILIES, model)
            self.assertEqual(bearer, upstream_fixture.BEARER_A)
        log = (self.cwd / "native-debug.log").read_text(errors="replace")
        self.assertNotIn("quota reserve:", log)  # the gate refused no Messages request
        self.assertIn("proxy refused the connection", log)  # auxiliary CONNECTs refused, native kept going
        self.assertIn('MCP server "fixture": Successfully connected', log)
        self.assertFalse(os.path.exists(private_tmp))
        self.assertTrue(wait_for(lambda: gone(shell), 10), "the background shell outlived the invocation")

    def test_the_running_native_adopts_a_rotated_store_token_through_a_rebind(self):
        """D1 recovery (2), offline: what a launcher-side refresh would rely on."""
        self.store(upstream_fixture.BEARER_A, "max")
        screen = self.launch()
        screen.type("fixture prompt one")
        screen.expect("FIXTURE-ANSWER")
        before = len(self.upstream.model_requests())
        profiles = len([r for r in self.upstream.requests if r["path"] == "/api/oauth/profile"])
        self.store(upstream_fixture.BEARER_A2, "max", hours=2)
        del self.upstream.identities[upstream_fixture.BEARER_A]  # the old token is no longer accepted
        screen.type("fixture prompt two")
        self.assertTrue(wait_for(lambda: screen.read(0.05) and screen.text().count("FIXTURE-ANSWER") >= 2, 60),
                        screen.text()[-2000:])
        after = self.models(before)
        self.assertTrue(after and all(bearer == upstream_fixture.BEARER_A2 for *_, bearer in after), after)
        rebinds = [r for r in self.upstream.requests if r["path"] == "/api/oauth/profile"][profiles:]
        self.assertIn(("Authorization", "Bearer " + upstream_fixture.BEARER_A2), rebinds[0]["headers"])
        screen.type("/exit")
        self.assertEqual(screen.wait(60), 0, screen.text()[-2000:])


if __name__ == "__main__":
    unittest.main()
