"""claude-as native-login enrollment, reserve flags and protected mode, with fakes only.

Every case runs in a temporary HOME with a fake claude (tests/fixtures/claude-reserve) and, for
protected mode, loopback fixtures; nothing reaches a real account, credential or provider.
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
        order. Returns (exit code, everything written to the terminal)."""
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
                    output = output.replace(prompt.encode(), b"<" + prompt.encode() + b">", 1)
                    os.write(master, reply.encode() + b"\r")
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
        self.record.unlink(missing_ok=True)
        self.write_config('[claude_auth.accounts.N]\nmode = "native-login"\n')
        for name in ("lib/claude-as/bearer.py", "lib/coding-agent-kit/reserve.py"):
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
upstream_fixture = load("test_claude_upstream", FIXTURES / "upstream.py")
E0_LIMITS = {  # the shape of the stanford policy-limits.json (E0: key names only; values fabricated)
    "artifact_create_notice": {"text": "fixture notice"},
    "compliance_taints": ["fixture_regime"],
    "defaults": {"remote_control_at_startup": True},
    "monitoring_notice": {"text": "fixture monitoring", "url": "https://example.invalid/notice"},
    "restrictions": {name: {"allowed": False} for name in (
        "allow_cobalt_plinth_alkanet", "allow_connector_file_downloads", "allow_drive_file_references",
        "allow_gmail_file_references", "allow_product_feedback", "allow_quick_web_setup",
        "enforce_web_search_mcp_isolation")},
}


class ServerManagedPolicy(unittest.TestCase):
    def reasons(self, findings):
        return sorted({(f.name, f.native, f.bearer, f.reason) for f in findings})

    def test_the_enterprise_limits_seen_in_e0_and_an_empty_settings_document_are_accepted(self):
        self.assertEqual(policy.judge_policy_limits(E0_LIMITS), [])
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
            config, work = self.files(base, {}, E0_LIMITS)
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
            config, work = self.files(base, {"forceLoginOrgUUID": "org-sensitive-value"}, E0_LIMITS)
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
        for path in (self.config, self.config.parent, self.config.parent.parent):
            path.chmod(0o700)
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
        for path in (self.config_dir, self.config_dir.parent, self.config_dir.parent.parent):
            path.chmod(0o700)
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

    def make(self, mode="startup", **extra):
        prov, decide = provider.make(self.config(**extra))
        g = gate.Gate(prov, decide, max_age=self.config(**extra)["max_age_s"], mode=mode, notify=self.messages.append,
                      emit=lambda event, **fields: self.events.append((event, fields)))
        self.addCleanup(g.shutdown)
        return prov, g

    def started(self, **extra):
        prov, g = self.make(**extra)
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

    def test_an_account_native_fetches_organization_policy_for_needs_both_caches(self):
        for subscription in ("enterprise", "team", None, "unknown_plan"):
            with self.subTest(subscription):
                self.store(upstream_fixture.BEARER_A, subscription=subscription)
                prov, g = self.make()
                with self.assertRaises(gate.Refusal) as caught:
                    prov.startup(g)
                self.assertEqual(caught.exception.reason, "policy_cache_missing")
        (self.config_dir / "remote-settings.json").write_text("{}")
        for change in (lambda: None, lambda: (self.config_dir / "policy-limits.json").mkdir()):  # one, or no file
            change()
            prov, g = self.make()
            with self.assertRaises(gate.Refusal):
                prov.startup(g)
        self.assertEqual(self.upstream.requests, [])
        (self.config_dir / "policy-limits.json").rmdir()
        (self.config_dir / "policy-limits.json").write_text("{}")
        _, g = self.started()
        self.assertEqual(g.state, "running", g.status())
        for name in provider.POLICY_CACHES:
            (self.config_dir / name).unlink()
        for subscription in ("pro", "max"):  # personal plans: native fetches no organization policy
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

    @unittest.expectedFailure
    def test_gate_fetch_survives_a_connection_close_reply(self):
        """Known defect of the shared gate core (lib/coding-agent-kit/gate.py, Gate.fetch): http.client
        closes the connection's socket when a reply says Connection: close, and the read loop's next
        settimeout() fails with EBADF, so every such quota read is upstream_unavailable. Real servers
        commonly answer a request that says Connection: close this way. Remove expectedFailure once the
        core is fixed."""
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
                               (["--dangerously-skip-permissions", "--no-chrome"], [])):
            with self.subTest(args):
                self.assertEqual(protect.screen_arguments(args).add_dirs, add_dirs)

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

    def test_store_and_proxy_selectors_in_the_environment_refuse_without_values(self):
        protect.screen_environment({"PATH": "/bin", "HTTPS_PROXY": "http://127.0.0.1:1"})
        for name in protect.ENV_REFUSED:
            with self.subTest(name):
                message = self.refused(protect.screen_environment, {name: "hidden-value"})
                self.assertIn(name, message)
                self.assertNotIn("hidden-value", message)

    def test_home_its_ancestors_and_its_login_items_cannot_be_writable(self):
        home = "/home/fixture"
        protect.check_writable(["/home/fixture/project", "/tmp/x", "/home/fixture/Library/Caches"], home)
        for path in ("/home/fixture", "/home", "/", "/home/fixture/Library", "/home/fixture/Library/LaunchAgents",
                     "/home/fixture/Library/LaunchAgents/sub"):
            with self.subTest(path):
                self.assertIn("cannot be writable in protected mode", self.refused(protect.check_writable, [path], home))

    def test_only_a_reviewed_native_version_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            native = Path(temp) / "claude"
            native.write_text(f"#!{sys.executable}\nimport os, sys\nprint(os.environ['V'])\n"
                              "sys.exit(int(os.environ.get('C', '0')))\n")
            native.chmod(0o700)
            env = dict(os.environ, V="2.1.322 (Claude Code)")
            self.assertEqual(protect.native_version(str(native), env), "2.1.322")
            for change, text in (({"V": "2.1.323 (Claude Code)"}, "Claude Code 2.1.323 is not a version"),
                                 ({"V": "2.1.322"}, "did not print a Claude Code version"),
                                 ({"C": "1"}, "did not print a Claude Code version")):
                with self.subTest(change):
                    self.assertIn(text, self.refused(protect.native_version, str(native), dict(env, **change)))
            self.assertIn("did not answer", self.refused(protect.native_version, str(Path(temp) / "absent"), env))


class ProtectedRefusals(Kit):
    """Refusals of the real launcher before any port, gate or native under the boundary."""

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
        limits.write_text(json.dumps(dict(E0_LIMITS, secret_limit={"value": "org-sensitive-value"})))
        limits.chmod(0o600)
        output = self.refused([])
        self.assertIn("policy-limits.json: secret_limit", output)
        self.assertNotIn("org-sensitive-value", output)

    def test_home_as_the_working_directory_refuses(self):
        if boundary_problem():
            self.skipTest(boundary_problem())
        self.cwd = self.home
        self.assertIn("cannot be writable in protected mode", self.refused([]))


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

    def store(self, subscription="max"):
        credential = self.native_config("N") / ".credentials.json"
        credential.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": upstream_fixture.BEARER_A, "refreshToken": "fabricated-refresh",
            "expiresAt": int(time.time() * 1000) + 3600_000, "scopes": ["user:inference", "user:profile"],
            "subscriptionType": subscription}}))
        credential.chmod(0o600)

    def protected(self, args=(), env=None, reserves=None, timeout=90):
        security = self.base / "security"  # macOS: no Keychain item, so the store file is read
        security.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(44)\n")
        security.chmod(0o700)
        spec = {"kit": str(self.kit), "account": "N", "reserves": reserves or {"five_hour": "10", "weekly": "5"},
                "args": list(args), "upstream_port": self.upstream.port, "security": str(security)}
        argv = [sys.executable, "-I", "-B", str(FIXTURES / "run_protected.py"), json.dumps(spec)]
        return self.terminal([], env=dict(self.env, **(env or {})), argv=argv, timeout=timeout)

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
        self.assertEqual(env["CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF"], "1")
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

    def test_an_organization_account_without_policy_caches_refuses_with_how_to_create_them(self):
        self.store("enterprise")
        code, output = self.protected()
        self.assertEqual(code, 1, output)
        self.assertIn("protected mode refused: the organization policy of N is not cached yet", output)
        self.assertIn("start one session without reserves (claude-as N)", output)
        self.assertFalse(self.record.exists())
        self.assertEqual(self.upstream.paths(), [])
        for name, document in (("remote-settings.json", {}), ("policy-limits.json", E0_LIMITS)):
            path = self.native_config("N") / name
            path.write_text(json.dumps(document))
            path.chmod(0o600)
        code, output = self.protected()
        self.assertEqual(code, 0, output)
        self.assertIn("fake claude sentinel", output)

    def test_a_native_killed_by_a_signal_ends_the_launcher_by_it_after_the_cleanup(self):
        code, output = self.protected(env={"FAKE_SIGNAL": str(int(signal.SIGTERM))})
        self.assertEqual(code, -signal.SIGTERM, output)
        self.assertFalse(os.path.exists(self.recorded()["env"]["TMPDIR"]))

    def test_a_failing_pre_launch_refresh_refuses(self):
        code, output = self.protected(env={"CLAUDE_FAKE_AUTH_STATUS": "1"})
        self.assertEqual(code, 1, output)
        self.assertIn("the pre-launch native refresh (claude auth status) failed (1)", output)
        self.assertEqual(self.upstream.paths(), [])

    def test_an_unreadable_store_refuses_at_gate_startup(self):
        (self.native_config("N") / ".credentials.json").chmod(0o644)
        code, output = self.protected()
        self.assertEqual(code, 1, output)
        self.assertIn("credential_credential_not_private", output)
        self.assertEqual(self.upstream.paths(), [])


if __name__ == "__main__":
    unittest.main()
