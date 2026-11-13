"""claude-as native-login enrollment, reserve flags and protected mode, with fakes only.

Every case runs in a temporary HOME with a fake claude (tests/fixtures/claude-reserve) and, for
protected mode, loopback fixtures; nothing reaches a real account, credential or provider.
"""

import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
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

    def terminal(self, args, answers=(), env=None, timeout=60):
        """claude-as on a fresh controlling terminal; ANSWERS are (prompt, reply) pairs, in order.
        Returns (exit code, everything written to the terminal)."""
        pid, master = pty.fork()
        if pid == 0:
            try:
                os.chdir(self.cwd)
                os.execve(str(self.kit / "bin/claude-as"), [str(self.kit / "bin/claude-as"), *args], env or self.env)
            finally:
                os._exit(127)
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
        self.assertEqual(self.recorded()["env"]["CLAUDE_CONFIG_DIR"], str(self.native_config("N")))
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
        self.record.unlink()
        self.write_config('[claude_auth.accounts.N]\nmode = "native-login"\n')
        result = self.run_kit(["N"], env=dict(self.env, ANTHROPIC_API_KEY="x"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ANTHROPIC_API_KEY", result.stderr)
        result = self.run_kit(["N", "--bare"])
        self.assertEqual(result.returncode, 1)
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


if __name__ == "__main__":
    unittest.main()
