"""Offline tests for the LC live driver.

Temporary HOME, secrets and result directories, the reviewed QC fake native and
loopback usage/profile fixtures only. No real account, credential or network.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
QC = HERE.parent / "qc"
KIT = HERE.parents[4]
DRIVER = HERE / "live_claude.py"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live = load(DRIVER, "lc_live_claude")
fake_usage = load(QC / "fake_usage.py", "lc_fake_usage")

ACCESS = "LC-FABRICATED-ACCESS-NOT-A-CREDENTIAL-0001"
REFRESH = "LC-FABRICATED-REFRESH-NOT-A-CREDENTIAL-0001"
UNKNOWN = "LC-FABRICATED-UNKNOWN-NOT-A-CREDENTIAL-0001"
NATIVE_ECHO = "QC-FAKE-NATIVE-ECHO-ACCESS-NOT-A-CREDENTIAL"
ACCOUNT = "lc-fixture-account-7f3a"
ORG = "lc-fixture-organization-9c1d"
SECRETS = (ACCESS, REFRESH, UNKNOWN, NATIVE_ECHO, ACCOUNT, ORG)
FAR_MS = 4_102_444_800_000
KIT_VARIABLES = ("CODING_AGENT_KIT_CONFIG", "CODING_AGENT_KIT_COLOR", "CODING_AGENT_KIT_USAGE_FILE",
                 "CODING_AGENT_KIT_TOKEN_ID", "CODING_AGENT_KIT_CLAUDE_PID", "CODING_AGENT_KIT_REPO_URL",
                 "XDG_CONFIG_HOME", "XDG_STATE_HOME", "NO_COLOR")


def windows(**extra):
    return fake_usage.windows(extra=extra)


def loopback_resolver(host, port, *args):
    if host not in ("127.0.0.1", "::1"):
        raise OSError("tests resolve loopback only")
    return socket.getaddrinfo(host, port, *args)


class EchoServer:
    """Stands in for the tunnel's upstream: echoes bytes, records nothing else."""

    def __init__(self):
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(client,), daemon=True).start()

    @staticmethod
    def _echo(client):
        with client:
            client.settimeout(5)
            try:
                while True:
                    data = client.recv(4096)
                    if not data:
                        return
                    client.sendall(data)
            except OSError:
                return

    def close(self):
        self.sock.close()


class Base(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.root = Path(tempfile.mkdtemp(prefix="quota-lc-test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        for name in ("home", "home/.secrets", "out", "run", "bin"):
            (self.root / name).mkdir(mode=0o700)
        self.home, self.secrets = self.root / "home", self.root / "home/.secrets"
        self.out, self.bin = self.root / "out", self.root / "bin"
        self.config = self.secrets / "native-claude-accounts/alpha/config"
        self.addCleanup(self.assertClean)  # runs before the tree is removed: every test is scanned
        self.env = {"HOME": str(self.home), "PATH": os.path.dirname(sys.executable) + ":/usr/bin:/bin",
                    "LANG": "C.UTF-8", "USER": "fixture", "XDG_RUNTIME_DIR": str(self.root / "run")}
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))
        self.native = self.bin / "claude"
        # The wrapper records whether the attempt file existed when native was spawned.
        self.native.write_text("#!/bin/sh\nif [ -e '%s/n1.attempt' ]; then : > '%s/attempt-seen'; fi\n"
                               "exec '%s' -I -B '%s' \"$0\" \"$@\"\n"
                               % (self.out, self.bin, sys.executable, QC / "fake_claude.py"))
        self.native.chmod(0o700)
        self.native_sha = hashlib.sha256(self.native.read_bytes()).hexdigest()
        self.echo = EchoServer()
        self.addCleanup(self.echo.close)
        self.connected = []
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT)
        self.wiring = self.make_wiring()

    def connect(self, host, port):
        self.connected.append("%s:%d" % (host, port))
        return socket.create_connection(("127.0.0.1", self.echo.port), timeout=5)

    def make_wiring(self, **overrides):
        attrs = {"expected_native_sha256": self.native_sha,
                 "n1_options": {"managed_dirs": [str(self.root / "managed")], "platform": "linux",
                                "deadlines": {"persistence": 1.0, "operation": 10.0, "total": 30.0,
                                              "shortcut": 0.2}},
                 "tunnel_connector": staticmethod(self.connect),
                 "resolver": staticmethod(loopback_resolver),
                 "usage_endpoint": live.Wiring.usage_endpoint, "profile_endpoint": live.Wiring.profile_endpoint}
        attrs.update(overrides)
        return type("FixtureWiring", (live.Wiring,), attrs)

    def behave(self, **behavior):
        (self.bin / "behavior.json").write_text(json.dumps(behavior))

    def call(self, *argv, wiring=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = live.main([str(a) for a in argv], self.wiring if wiring is None else wiring)
        self.outputs = getattr(self, "outputs", []) + [stdout.getvalue(), stderr.getvalue()]
        self.assertEqual(stderr.getvalue(), "")
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["exit_code"], code)
        self.assertTrue(stdout.getvalue().isascii())
        return code, document

    def step(self, name, *extra, wiring=None):
        return self.call(name, "--alias", "alpha", "--out", self.out, *extra, wiring=wiring)

    def native_login(self, account=ACCOUNT, organization=ORG):
        account_record = {"emailAddress": "fixture@example.invalid"}
        if account is not None:
            account_record["accountUuid"] = account
        if organization is not None:
            account_record["organizationUuid"] = organization
        path = self.config / ".claude.json"
        path.write_text(json.dumps({"oauthAccount": account_record}))
        path.chmod(0o600)

    def credential(self, token=ACCESS, expires=FAR_MS):
        path = self.config / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "refreshToken": REFRESH,
                                                       "expiresAt": expires,
                                                       "scopes": ["user:inference", "user:profile"],
                                                       "subscriptionType": "max"}}))
        path.chmod(0o600)

    def ready(self):
        self.assertEqual(self.step("pending")[0], 0)
        self.native_login()
        self.assertEqual(self.step("ready", "--confirm")[0], 0)

    def registration(self):
        return json.loads((self.config.parent / "account.json").read_text())

    def mode(self, path):
        return os.stat(path).st_mode & 0o777

    def alias_marker(self, step):
        return self.config.parent / ("lc-%s.attempt" % step)

    def assertClean(self):
        """No fabricated token or UUID in any output, result file or alias marker."""
        texts = list(getattr(self, "outputs", []))
        for path in list(self.out.iterdir()) + list(self.config.parent.glob("lc-*.attempt")):
            texts.append(path.read_bytes().decode("ascii"))
        for text in texts:
            for secret in SECRETS:
                self.assertNotIn(secret, text)

    def subprocess_env(self, **extra):
        env = dict(self.env)
        env.update(extra)
        for name in KIT_VARIABLES:
            if name not in extra:
                env.pop(name, None)
        return env

    def run_cli(self, *argv, env=None):
        done = subprocess.run([sys.executable, "-I", "-B", str(DRIVER), *[str(a) for a in argv]],
                              env=self.subprocess_env() if env is None else env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
        self.outputs = getattr(self, "outputs", []) + [done.stdout, done.stderr]
        return done.returncode, json.loads(done.stdout), done.stderr


class ConfigResolutionTests(Base):
    def show_config(self, env):
        done = subprocess.run([str(KIT / "bin/claude-as"), "--show-config"], env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = dict(line.split(" = ", 1) for line in done.stdout.splitlines())
        return lines["secrets_dir"]

    def expect_commands(self, document, config_dir, claude="claude"):
        self.assertEqual(document["user_commands"], [
            "CLAUDE_CONFIG_DIR=%s %s auth login" % (config_dir, claude),
            "CLAUDE_CONFIG_DIR=%s %s auth status" % (config_dir, claude)])
        for command in document["user_commands"]:
            self.assertNotIn("config/", command)
            self.assertNotIn("//", command)

    def test_default_matches_launcher(self):
        env = self.subprocess_env()
        code, document, stderr = self.run_cli("pending", "--alias", "alpha", "--out", self.out, env=env)
        self.assertEqual((code, stderr, document["config_source"]), (0, "", "not found; defaults"))
        secrets = self.show_config(env)
        self.assertEqual(secrets, str(self.secrets))
        self.expect_commands(document, secrets + "/native-claude-accounts/alpha/config")
        self.assertTrue(self.config.is_dir())

    def test_explicit_config_matches_launcher(self):
        custom = self.root / "custom"
        custom.mkdir(mode=0o700)
        (custom / "secrets").mkdir(mode=0o700)
        claude = self.root / "tools/claude"
        config_file = self.root / "kit.toml"
        config_file.write_text('secrets_dir = "%s//secrets/"\nclaude = "%s"\n' % (custom, claude))
        env = self.subprocess_env(CODING_AGENT_KIT_CONFIG=str(config_file))
        code, document, _ = self.run_cli("pending", "--alias", "alpha", "--out", self.out, env=env)
        self.assertEqual((code, document["config_source"]), (0, "from CODING_AGENT_KIT_CONFIG"))
        secrets = self.show_config(env)
        self.assertEqual(secrets, str(custom / "secrets"))
        self.expect_commands(document, secrets + "/native-claude-accounts/alpha/config", str(claude))
        self.assertFalse((self.secrets / "native-claude-accounts").exists())

    def test_xdg_config_home_matches_launcher(self):
        (self.root / "xdg/coding-agent-kit").mkdir(mode=0o700, parents=True)
        (self.home / "alt-secrets").mkdir(mode=0o700)
        (self.root / "xdg/coding-agent-kit/config.toml").write_text('secrets_dir = "~/alt-secrets/"\n')
        env = self.subprocess_env(XDG_CONFIG_HOME=str(self.root / "xdg"))
        code, document, _ = self.run_cli("pending", "--alias", "alpha", "--out", self.out, env=env)
        self.assertEqual((code, document["config_source"]), (0, "found"))
        secrets = self.show_config(env)
        self.assertEqual(secrets, str(self.home / "alt-secrets"))
        self.expect_commands(document, secrets + "/native-claude-accounts/alpha/config")

    def test_home_spellings(self):
        env = self.subprocess_env(HOME=str(self.home) + "/")
        code, document, _ = self.run_cli("pending", "--alias", "alpha", "--out", self.out, env=env)
        self.assertEqual(code, 0)
        self.expect_commands(document, str(self.config))
        shutil.rmtree(self.secrets / "native-claude-accounts")
        env = self.subprocess_env(HOME="/" + str(self.home))
        code, document, _ = self.run_cli("pending", "--alias", "alpha", "--out", self.out, env=env)
        self.assertEqual((code, document["reason"]), (3, "secrets_dir_not_canonical"))
        self.assertNotIn("user_commands", document)
        self.assertFalse((self.secrets / "native-claude-accounts").exists())

    def test_config_errors_refuse(self):
        for value, reason in (("relative/kit.toml", "config_error"), (str(self.root / "absent.toml"), "config_error")):
            with self.subTest(value=value):
                env = self.subprocess_env(CODING_AGENT_KIT_CONFIG=value)
                code, document, _ = self.run_cli("pending", "--alias", "alpha", "--out", self.out, env=env)
                self.assertEqual((code, document["reason"]), (3, reason))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_launchers_ignore_the_registration_root(self):
        token = self.secrets / "claude-beta"
        token.write_text("fixture-not-a-token\n")
        token.chmod(0o600)
        self.assertEqual(self.step("pending")[0], 0)
        done = subprocess.run([str(KIT / "bin/claude-as"), "--list-accounts"], env=self.subprocess_env(),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "beta\n", ""))


class PendingTests(Base):
    def test_creates_pending_registration(self):
        code, document = self.step("pending")
        self.assertEqual((code, document["status"], document["registration_state"]), (0, "pending_created", "pending"))
        self.assertFalse(document["config_directory_trailing_slash"])
        record = self.registration()
        self.assertEqual((record["state"], record["identity"], record["config_directory"]),
                         ("pending", None, str(self.config)))
        for path in (self.config.parent.parent, self.config.parent, self.config):
            self.assertEqual(self.mode(path), 0o700)
        result = self.out / "pending.result.json"
        self.assertEqual(self.mode(result), 0o600)
        written = json.loads(result.read_text())
        self.assertNotIn("user_commands", written)
        self.assertEqual(written["status"], "pending_created")
        self.assertNotIn(str(self.home), result.read_text())

    def test_rerun_refuses_and_prints_no_login(self):
        self.step("pending")
        for number in (1, 2):
            code, document = self.step("pending")
            self.assertEqual((code, document["reason"], document["registration_state"]),
                             (3, "account_directory_exists", "pending"))
            self.assertNotIn("user_commands", document)
            rerun = json.loads((self.out / ("pending.rerun-%d.json" % number)).read_text())
            self.assertEqual(rerun["reason"], "account_directory_exists")
        self.assertEqual(json.loads((self.out / "pending.result.json").read_text())["status"], "pending_created")
        self.assertEqual(sorted(path.name for path in self.out.iterdir()),
                         ["pending.rerun-1.json", "pending.rerun-2.json", "pending.result.json"])

    def test_result_directory_rules(self):
        repo = self.root / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "sub/out").mkdir(parents=True, mode=0o700)
        (self.root / "wide").mkdir()
        (self.root / "wide").chmod(0o750)
        (self.root / "file").write_text("")
        os.symlink(self.out, self.root / "link")
        (self.secrets / "results").mkdir(mode=0o700)
        cases = [("relative/out", "out_dir_not_absolute"), (self.root / "absent", "out_dir_missing"),
                 (self.root / "wide", "out_dir_not_private"), (self.root / "file", "out_dir_not_directory"),
                 (self.root / "link", "out_dir_not_directory"), (repo / "sub/out", "out_dir_in_git_worktree"),
                 (self.secrets / "results", "out_dir_in_secrets")]
        for out, reason in cases:
            with self.subTest(out=out):
                code, document = self.call("pending", "--alias", "alpha", "--out", out)
                self.assertEqual((code, document["reason"]), (3, reason))
        self.assertFalse((self.secrets / "native-claude-accounts").exists())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_invalid_alias(self):
        for alias in ("../x", "-x", "a/b", ""):
            with self.subTest(alias=alias):
                code, document = self.call("pending", "--alias=" + alias, "--out", self.out)
                self.assertEqual((code, document["reason"]), (3, "invalid_account_name"))
        self.assertFalse((self.secrets / "native-claude-accounts").exists())


class ReadyTests(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.step("pending")[0], 0)

    def test_before_login(self):
        code, document = self.step("ready", "--confirm")
        self.assertEqual((code, document["reason"]), (3, "metadata_unusable"))
        self.assertEqual(self.registration()["state"], "pending")

    def test_without_confirmation_changes_nothing(self):
        self.native_login()
        code, document = self.step("ready")
        self.assertEqual((code, document["reason"], document["marked_ready"]), (3, "confirmation_required", False))
        self.assertEqual((document["account_uuid_present"], document["organization_uuid_present"]), (True, True))
        self.assertIs(document["server_policy_present"], False)
        self.assertEqual(self.registration()["state"], "pending")
        self.assertClean()

    def test_missing_uuid_is_never_marked(self):
        self.native_login(organization=None)
        code, document = self.step("ready", "--confirm")
        self.assertEqual((code, document["reason"]), (3, "identity_missing"))
        self.assertEqual((document["account_uuid_present"], document["organization_uuid_present"]), (True, False))
        self.assertEqual(self.registration()["state"], "pending")

    def test_confirmation_marks_ready(self):
        self.native_login()
        code, document = self.step("ready", "--confirm")
        self.assertEqual((code, document["status"], document["marked_ready"], document["registration_state"]),
                         (0, "marked_ready", True, "ready"))
        self.assertEqual(self.registration()["identity"], {"account_uuid": ACCOUNT, "organization_uuid": ORG})
        code, document = self.step("ready", "--confirm")
        self.assertEqual((code, document["reason"], document["registration_state"]),
                         (3, "registration_not_pending", "ready"))
        self.assertEqual(json.loads((self.out / "ready.result.json").read_text())["status"], "marked_ready")
        self.assertEqual(json.loads((self.out / "ready.rerun-1.json").read_text())["reason"],
                         "registration_not_pending")
        self.assertClean()

    def test_preview_then_confirm_keeps_every_record(self):
        self.native_login()
        self.assertEqual(self.step("ready")[0], 3)
        self.assertEqual(self.step("ready", "--confirm")[0], 0)
        self.assertEqual(self.step("ready", "--confirm")[0], 3)
        records = {path.name: json.loads(path.read_text()) for path in self.out.glob("ready.*")}
        self.assertEqual({name: (record["status"], record["reason"]) for name, record in records.items()},
                         {"ready.result.json": ("refused", "confirmation_required"),
                          "ready.rerun-1.json": ("marked_ready", None),
                          "ready.rerun-2.json": ("refused", "registration_not_pending")})
        self.assertEqual(self.registration()["state"], "ready")

    def test_reports_server_managed_policy(self):
        self.native_login()
        (self.config / "remote-settings.json").write_text("{}")
        code, document = self.step("ready")
        self.assertIs(document["server_policy_present"], True)


class MissingRegistrationTests(Base):
    def test_every_later_step_refuses(self):
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, record_env=True)
        for argv in (("ready", "--confirm"), ("n1", "--binary", self.native, "--tunnel"), ("bearer",)):
            with self.subTest(step=argv[0]):
                code, document = self.step(*argv)
                self.assertEqual((code, document["reason"], document["attempt_consumed"]),
                                 (3, "path_component_unusable", False))
        self.assertEqual([path.name for path in self.out.iterdir()], ["ready.result.json"])
        self.assertFalse((self.secrets / "native-claude-accounts").exists())
        self.assertFalse((self.bin / "env.json").exists())


class N1Tests(Base):
    def setUp(self):
        super().setUp()
        self.ready()

    def n1(self, *extra, wiring=None):
        tunnel = () if "--no-tunnel" in extra else ("--tunnel",)
        return self.step("n1", "--binary", self.native, *tunnel, *extra, wiring=wiring)

    def assertNothingConsumed(self):
        self.assertFalse((self.out / "n1.attempt").exists())
        self.assertFalse(self.alias_marker("n1").exists())
        self.assertFalse((self.out / "n1.result.json").exists())
        self.assertFalse((self.bin / "attempt-seen").exists())
        self.assertFalse((self.bin / "env.json").exists())

    def assertGone(self, pid):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self.fail("process %d still alive" % pid)

    def spawn_child(self, deadlines):
        """The driver in its own process (fixture wiring), so real signals can reach it."""
        spec = self.root / "child.json"
        spec.write_text(json.dumps({"sha": self.native_sha, "managed": str(self.root / "managed"),
                                    "deadlines": deadlines,
                                    "argv": ["n1", "--alias", "alpha", "--out", str(self.out), "--binary",
                                             str(self.native), "--no-tunnel"]}))
        return subprocess.Popen([sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--child", str(spec)],
                                env=self.subprocess_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def signal_child(self, child, delay):
        """SIGTERM `delay` seconds after the fake native's grandchild exists; return the parsed output."""
        try:
            deadline = time.monotonic() + 20
            while not (self.bin / "grandchild.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.bin / "grandchild.pid").exists())
            grandchild = int((self.bin / "grandchild.pid").read_text())
            native = int(Path("/proc/%d/stat" % grandchild).read_text().rsplit(")", 1)[1].split()[1])
            time.sleep(delay)
            child.send_signal(signal.SIGTERM)
            stdout, stderr = child.communicate(timeout=40)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
        self.outputs = getattr(self, "outputs", []) + [stdout, stderr]
        self.assertEqual(stderr, "")
        return child.returncode, json.loads(stdout), native, grandchild

    def test_happy_path(self):
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, echo_token=True, record_env=True,
                    connect=["api.anthropic.com:443", "elsewhere.example:443"])
        code, document = self.n1()
        self.assertEqual((code, document["status"], document["reason"]), (0, "fresh_windows", None), document)
        self.assertTrue(document["attempt_consumed"] and document["tunnel"] and document["requery_allowed"])
        self.assertTrue(document["native_sha256_matches"])
        self.assertTrue((self.bin / "attempt-seen").exists(), "attempt file must exist before spawn")
        for path in (self.out / "n1.attempt", self.out / "n1.result.json", self.alias_marker("n1")):
            self.assertEqual(self.mode(path), 0o600)
        self.assertEqual((document["alias"], document["enrollment_tag"]),
                         ("alpha", live._enrollment_tag(self.registration())))
        self.assertFalse(document["signal_received"])
        harness = document["harness"]
        self.assertEqual(harness["traffic"]["connect"], {"api.anthropic.com:443": 1})
        self.assertEqual(harness["traffic"]["refused"], 1)
        self.assertEqual(self.connected, ["api.anthropic.com:443"])
        self.assertEqual((harness["queries"], harness["model_events"]), (1, 0))
        self.assertTrue(harness["native"]["group_empty"])
        recorded = json.loads((self.bin / "env.json").read_text())
        self.assertEqual(recorded["config"], str(self.config))
        self.assertEqual(recorded["args"], list(live._Modules().n1.NATIVE_ARGS))
        self.assertEqual(document["bounds"], {"total_s": 30.0, "operation_s": 10.0, "persistence_s": 1.0,
                                              "shortcut_wait_s": 0.2, "max_get_usage": 2})
        self.assertEqual(json.loads((self.out / "n1.result.json").read_text()), document)
        self.assertClean()

    def test_one_shot(self):
        self.assertEqual(self.n1()[0], 0)
        os.unlink(self.bin / "attempt-seen")
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, record_env=True)
        code, document = self.n1()
        self.assertEqual((code, document["reason"], document["attempt_consumed"]), (4, "result_exists", False))
        os.unlink(self.out / "n1.result.json")  # a crash after the attempt: the marker alone refuses
        code, document = self.n1()
        self.assertEqual((code, document["reason"]), (4, "attempt_exists"))
        os.unlink(self.out / "n1.attempt")  # a lost DIR: the alias marker still refuses
        code, document = self.n1()
        self.assertEqual((code, document["reason"], document["attempt_consumed"]), (4, "alias_attempt_exists", False))
        self.assertFalse((self.bin / "env.json").exists())
        self.assertFalse((self.out / "n1.result.json").exists())
        self.assertFalse((self.out / "n1.attempt").exists())

    def test_second_directory_refuses(self):
        self.assertEqual(self.n1()[0], 0)
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, record_env=True)
        other = self.root / "other-out"
        other.mkdir(mode=0o700)
        code, document = self.call("n1", "--alias", "alpha", "--out", other, "--binary", self.native, "--tunnel")
        self.assertEqual((code, document["reason"], document["attempt_consumed"]), (4, "alias_attempt_exists", False))
        self.assertEqual(list(other.iterdir()), [])
        self.assertFalse((self.bin / "env.json").exists())

    def test_tunnel_choice_is_required(self):
        for extra in ((), ("--tunnel", "--no-tunnel")):
            with self.subTest(extra=extra):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    live.main(["n1", "--alias", "alpha", "--out", str(self.out), "--binary", str(self.native),
                               *extra], self.wiring)
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("tunnel", stderr.getvalue())
        self.assertNothingConsumed()

    def test_preflight_refusals_consume_nothing(self):
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, record_env=True)

        def restore():
            os.environ.clear()
            os.environ.update(self.env)
            self.native_login()
            shutil.rmtree(self.root / "managed", ignore_errors=True)

        cases = [
            ("native_version_unreviewed", lambda: None, {"wiring": self.make_wiring(expected_native_sha256="0" * 64)}),
            ("conflicting_environment", lambda: os.environ.update(ANTHROPIC_API_KEY=""), {}),
            ("conflicting_environment", lambda: os.environ.update(CLAUDE_CODE_ENTRYPOINT="cli"), {}),
            ("xdg_runtime_dir_not_private", lambda: os.environ.pop("XDG_RUNTIME_DIR"), {}),
            ("xdg_runtime_dir_not_private", lambda: os.environ.update(XDG_RUNTIME_DIR=str(self.root)), {}),
            ("identity_mismatch_before", lambda: self.native_login(account="lc-other-account"), {}),
            ("policy_conflicting_auth", lambda: ((self.root / "managed").mkdir(),
                                                 (self.root / "managed/managed-settings.json").write_text(
                                                     '{"apiKeyHelper": "/bin/true"}')), {}),
            ("native_binary_unusable", lambda: None, {"binary": "bin/claude"}),
        ]
        os.chmod(self.root, 0o750)  # XDG_RUNTIME_DIR=root is then not private
        self.addCleanup(os.chmod, self.root, 0o700)
        for reason, mutate, options in cases:
            with self.subTest(reason=reason):
                mutate()
                try:
                    binary = options.get("binary", self.native)
                    code, document = self.step("n1", "--binary", binary, "--tunnel", wiring=options.get("wiring"))
                finally:
                    restore()
                self.assertEqual((code, document["reason"], document["attempt_consumed"]), (3, reason, False))
                if reason == "conflicting_environment":
                    self.assertEqual(len(document["conflicting_names"]), 1)
                self.assertNothingConsumed()

    def test_pending_registration_refuses(self):
        shutil.rmtree(self.config.parent)
        self.assertEqual(self.step("pending")[0], 0)
        self.native_login()
        code, document = self.n1()
        self.assertEqual((code, document["reason"]), (3, "registration_pending"))
        self.assertNothingConsumed()

    def test_cli_pins_the_reviewed_native(self):
        n1 = live._Modules().n1
        try:  # the CLI checks the host's real policy sources before the hash
            n1.check_effective_policy(str(self.config), n1.load_registration(n1.AccountPaths(str(self.secrets), "alpha")),
                                      n1.detect_platform())
        except n1.HarnessRefused as error:
            self.skipTest("this host's policy refuses before the hash check: " + error.reason)
        code, document, stderr = self.run_cli("n1", "--alias", "alpha", "--out", self.out, "--binary", self.native,
                                              "--tunnel")
        self.assertEqual((code, document["reason"], stderr), (3, "native_version_unreviewed", ""))
        self.assertNothingConsumed()

    def test_bounded_deadline_propagates(self):
        self.behave(mode="hang", grandchild=True)
        wiring = self.make_wiring(n1_options={"managed_dirs": [str(self.root / "managed")], "platform": "linux",
                                              "deadlines": {"operation": 1.0, "total": 3.0}})
        start = time.monotonic()
        code, document = self.n1(wiring=wiring)
        self.assertLess(time.monotonic() - start, 15)
        harness = document["harness"]
        self.assertEqual((code, harness["classification"], harness["reason"]), (1, "failed", "operation_deadline"))
        self.assertEqual((document["bounds"]["operation_s"], document["bounds"]["total_s"]), (1.0, 3.0))
        self.assertTrue(harness["native"]["group_empty"])
        pid = int((self.bin / "grandchild.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_server_policy_voids_the_result(self):
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, write_server_policy=True)
        code, document = self.n1()
        self.assertEqual((code, document["status"], document["reason"]), (1, "refused", "server_policy_fetched"))
        self.assertNotIn("replies", document["harness"])

    def test_without_tunnel(self):
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, connect=["api.anthropic.com:443"])
        code, document = self.n1("--no-tunnel")
        self.assertEqual((code, document["tunnel"]), (0, False))
        self.assertNotIn("traffic", document["harness"])
        self.assertEqual((self.bin / "connect.log").read_text(), "api.anthropic.com:443 no_proxy\n")
        self.assertEqual(self.connected, [])

    def test_termination_reaps_native_and_records_the_attempt(self):
        self.behave(mode="hang", grandchild=True)
        child = self.spawn_child({"operation": 25.0, "total": 30.0, "persistence": 1.0, "shortcut": 0.2})
        start = time.monotonic()
        code, document, native, grandchild = self.signal_child(child, 0.5)
        self.assertLess(time.monotonic() - start, 20)  # the signal, not the 25 s bound, ended the request
        harness = document["harness"]
        self.assertEqual((code, document["status"], document["attempt_consumed"], document["signal_received"]),
                         (1, "interrupted", True, True))
        self.assertEqual((harness["classification"], harness["reason"]), ("failed", "operation_deadline"))
        self.assertTrue(harness["native"]["group_empty"])
        self.assertEqual(json.loads((self.out / "n1.result.json").read_text())["status"], "interrupted")
        for pid in (native, grandchild):
            self.assertGone(pid)
        self.assertEqual(os.listdir(self.root / "run"), [])

    def test_signal_during_cleanup_still_reaps(self):
        """A signal inside native.close()'s 5 s wait no longer cuts run_n1's cleanup short."""
        self.behave(mode="hang", grandchild=True)  # native ignores stdin EOF: close() waits its full 5 s
        child = self.spawn_child({"operation": 1.0, "total": 3.0, "persistence": 1.0, "shortcut": 0.2})
        code, document, native, grandchild = self.signal_child(child, 2.5)
        harness = document["harness"]
        self.assertEqual((code, document["status"], document["attempt_consumed"]), (1, "interrupted", True))
        self.assertEqual((harness["classification"], harness["reason"]), ("failed", "operation_deadline"))
        self.assertTrue(harness["native"]["group_signalled"] and harness["native"]["group_empty"])
        self.assertGreaterEqual(harness["elapsed_s"], 5.0)  # close() ran to its end after the signal
        for pid in (native, grandchild):
            self.assertGone(pid)
        self.assertEqual(os.listdir(self.root / "run"), [])  # workspace removed

    def test_signal_during_cache_wait_sends_no_second_query(self):
        self.behave(write_cache=True, cache_first_query_skip=True, rate_limits=windows(), cache_account=ACCOUNT)
        wiring = self.make_wiring(n1_options={"managed_dirs": [str(self.root / "managed")], "platform": "linux",
                                              "deadlines": {"persistence": 1.0, "operation": 10.0, "total": 40.0,
                                                            "shortcut": 20.0}})
        timer = threading.Timer(4.0, live._on_signal, (signal.SIGTERM, None))  # the handler as a signal runs it
        self.addCleanup(timer.cancel)
        start = time.monotonic()
        timer.start()
        code, document = self.n1(wiring=wiring)
        self.assertLess(time.monotonic() - start, 15)
        harness = document["harness"]
        self.assertEqual((code, document["status"], harness["classification"], harness["reason"], harness["queries"]),
                         (1, "interrupted", "failed", "interrupted", 1))
        self.assertTrue(harness["native"]["group_empty"])
        self.assertLess(harness["elapsed_s"], 15)
        self.assertEqual(os.listdir(self.root / "run"), [])

    def test_signal_before_the_attempt_consumes_nothing(self):
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, record_env=True)
        original = live.Run.check_unused

        def check(run, *args):
            original(run, *args)
            live._on_signal(signal.SIGTERM, None)

        with mock.patch.object(live.Run, "check_unused", check):
            code, document = self.n1()
        self.assertEqual((code, document["reason"], document["attempt_consumed"]), (3, "interrupted", False))
        self.assertNothingConsumed()

    def test_signal_before_run_n1_takes_its_start_still_ends_the_run(self):
        """A flag set after the driver's last check but before run_n1's first clock reading."""
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, record_env=True)
        original = live._Clock.__init__

        def init(clock, refused):
            original(clock, refused)
            live._on_signal(signal.SIGTERM, None)

        start = time.monotonic()
        with mock.patch.object(live._Clock, "__init__", init):
            code, document = self.n1()
        self.assertLess(time.monotonic() - start, 15)
        harness = document["harness"]
        self.assertEqual((code, document["status"], document["attempt_consumed"]), (1, "interrupted", True))
        # start was real time, so every deadline had passed: no get_usage was sent
        self.assertEqual((harness["classification"], harness["reason"], harness["queries"]),
                         ("failed", "operation_deadline", 0))
        self.assertTrue(harness["native"]["group_empty"])
        self.assertEqual(os.listdir(self.root / "run"), [])

    def test_signal_after_the_attempt_spawns_nothing(self):
        self.behave(write_cache=True, rate_limits=windows(), cache_account=ACCOUNT, record_env=True)
        original = live.Run.create_attempt

        def create(run, *args):
            created = original(run, *args)
            live._on_signal(signal.SIGTERM, None)
            return created

        with mock.patch.object(live.Run, "create_attempt", create):
            code, document = self.n1()
        self.assertEqual((code, document["status"], document["attempt_consumed"], document["harness"]),
                         (1, "interrupted", True, None))
        self.assertFalse((self.bin / "env.json").exists())
        self.assertEqual(json.loads((self.out / "n1.result.json").read_text())["status"], "interrupted")
        self.assertTrue(self.alias_marker("n1").exists())


class LiveWiringTests(unittest.TestCase):
    """The CLI path uses the reviewed production values unchanged."""

    def test_values(self):
        modules = live._Modules()
        wiring = live.Wiring
        self.assertEqual(wiring.expected_native_sha256,
                         "bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478")
        self.assertEqual(wiring.user_agent, "quota-probe/1")
        self.assertEqual((wiring.socket_timeout, wiring.total_deadline), (5.0, 10.0))
        self.assertEqual(modules.usage.BODY_LIMIT, 65536)
        self.assertIs(wiring.resolver, socket.getaddrinfo)
        self.assertIsNone(wiring.usage_endpoint)
        self.assertIsNone(wiring.profile_endpoint)
        self.assertIsNone(wiring.tls_context)
        self.assertIsNone(wiring.workspace_parent)
        self.assertEqual(wiring.n1_options, {})
        self.assertEqual((modules.n1.TOTAL_DEADLINE, modules.n1.OPERATION_DEADLINE, modules.n1.CACHE_SHORTCUT_WAIT),
                         (120.0, 45.0, 65.0))
        self.assertEqual(set(live._LIVE_TUNNEL_HOSTS), set(modules.n1.TUNNEL_HOSTS))
        production = (modules.usage.PRODUCTION, modules.usage.PRODUCTION_PROFILE)
        self.assertEqual([(e.scheme, e.host, e.port, e.path) for e in production],
                         [("https", "api.anthropic.com", 443, "/api/oauth/usage"),
                          ("https", "api.anthropic.com", 443, "/api/oauth/profile")])

    def test_live_connector_refuses_unlisted_hosts_before_connecting(self):
        with mock.patch.object(live.socket, "create_connection") as connect:
            for host, port in (("example.com", 443), ("api.anthropic.com", 80), ("127.0.0.1", 443)):
                with self.subTest(host=host, port=port):
                    with self.assertRaises(OSError):
                        live._live_connector(host, port)
            connect.assert_not_called()


class BearerTests(Base):
    def setUp(self):
        super().setUp()
        self.sent_headers = []  # every request's headers except Authorization (test memory only)
        original, sent = fake_usage._Handler.do_GET, self.sent_headers

        def do_get(handler):
            sent.append([(name, value) for name, value in handler.headers.items() if name.lower() != "authorization"])
            return original(handler)

        self.enterContext(mock.patch.object(fake_usage._Handler, "do_GET", do_get))
        self.server = fake_usage.FakeUsageServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.account = self.server.register(ACCESS, fake_usage.FakeAccount("A", ACCOUNT, ORG, windows()))
        self.wiring = self.loopback_wiring()
        self.ready()
        self.credential()

    def loopback_wiring(self, scheme="http", **overrides):
        base = self.server.port
        return self.make_wiring(usage_endpoint=live._Modules().usage.Endpoint(scheme, "127.0.0.1", base),
                                profile_endpoint=live._Modules().usage.Endpoint(
                                    scheme, "127.0.0.1", base, "/api/oauth/profile"), **overrides)

    def n1_done(self):
        code, document = self.step("n1", "--binary", self.native, "--tunnel")
        self.assertEqual(code, 0, document)

    def bearer(self, wiring=None, *extra):
        return self.step("bearer", *extra, wiring=wiring)

    def test_happy_path(self):
        self.n1_done()
        code, document = self.bearer()
        self.assertEqual((code, document["status"], document["n1_classification"], document["n1_gate"]),
                         (0, "verified", "fresh_windows", "fresh_windows"))
        self.assertTrue(0 <= document["n1_age_s"] < 120)
        self.assertFalse(document["signal_received"])
        self.assertEqual(document["credential"], {"read": True, "reason": None})
        usage = document["usage"]
        self.assertEqual((usage["outcome"], usage["http_status"], usage["valid_general"]), ("response", 200, True))
        self.assertEqual(set(usage["windows"]), {"five_hour", "seven_day"})
        self.assertEqual(document["profile"], {"available": True, "account_uuid_matches": True,
                                               "organization_uuid_matches": True})
        self.assertEqual(self.server.labels(), ["A"])
        self.assertEqual(self.server.labels("/api/oauth/profile"), ["A"])
        self.assertEqual(document["bounds"]["socket_timeout_s"], 5.0)
        self.assertEqual(document["bounds"]["total_deadline_s"], 10.0)
        self.assertEqual(document["bounds"]["body_limit_bytes"], 65536)
        for path in (self.out / "bearer.attempt", self.out / "bearer.result.json", self.alias_marker("bearer")):
            self.assertEqual(self.mode(path), 0o600)
        self.assertClean()

    def test_requests_carry_no_project_user_or_host_identifier(self):
        self.n1_done()
        code, document = self.bearer()
        self.assertEqual((code, document["user_agent"]), (0, "quota-probe/1"))
        self.assertEqual(len(self.sent_headers), 2)  # usage and profile
        account = pwd.getpwuid(os.getuid())
        forbidden = ["coding-agent-kit", os.uname().nodename, account.pw_dir, str(self.home), str(self.root)]
        forbidden += [account.pw_name] if len(account.pw_name) >= 3 else []
        for headers in self.sent_headers:
            self.assertEqual([value for name, value in headers if name.lower() == "user-agent"], ["quota-probe/1"])
            text = "\n".join("%s: %s" % item for item in headers).lower()
            for value in forbidden:
                self.assertNotIn(value.lower(), text)
        self.assertClean()

    def test_pending_registration_refuses(self):
        shutil.rmtree(self.config.parent)
        self.assertEqual(self.step("pending")[0], 0)
        self.native_login()
        self.credential()
        code, document = self.bearer()
        self.assertEqual((code, document["reason"], document["attempt_consumed"]), (3, "registration_pending", False))
        self.assertFalse((self.out / "bearer.attempt").exists())
        self.assertEqual(self.server.requests, [])

    def test_rerun_after_success_reports_the_result_before_a_stale_n1(self):
        self.n1_done()
        self.assertEqual(self.bearer()[0], 0)
        stale = json.loads((self.out / "n1.result.json").read_text())
        stale["attempt_created_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                     time.gmtime(time.time() - 2 * live.N1_MAX_AGE_S))
        (self.out / "n1.result.json").unlink()
        (self.out / "n1.result.json").write_text(json.dumps(stale))
        (self.out / "n1.result.json").chmod(0o600)
        code, document = self.bearer()
        self.assertEqual((code, document["reason"], document["attempt_consumed"]), (4, "result_exists", False))
        self.assertEqual(len(self.server.requests), 2)

    def test_attempt_exists_before_any_network(self):
        self.n1_done()
        seen = []

        def resolver(host, port, *args):
            seen.append((self.out / "bearer.attempt").exists())
            return loopback_resolver(host, port, *args)

        code, _ = self.bearer(self.loopback_wiring(resolver=staticmethod(resolver)))
        self.assertEqual((code, seen), (0, [True, True]))

    def test_one_shot(self):
        self.n1_done()
        self.assertEqual(self.bearer()[0], 0)
        code, document = self.bearer()
        self.assertEqual((code, document["reason"]), (4, "result_exists"))
        os.unlink(self.out / "bearer.result.json")
        code, document = self.bearer()
        self.assertEqual((code, document["reason"]), (4, "attempt_exists"))
        os.unlink(self.out / "bearer.attempt")
        code, document = self.bearer()
        self.assertEqual((code, document["reason"]), (4, "alias_attempt_exists"))
        self.assertEqual(len(self.server.requests), 2)

    def test_second_directory_refuses(self):
        self.n1_done()
        self.assertEqual(self.bearer()[0], 0)
        other = self.root / "other-out"
        other.mkdir(mode=0o700)
        shutil.copy2(self.out / "n1.result.json", other / "n1.result.json")
        code, document = self.call("bearer", "--alias", "alpha", "--out", other)
        self.assertEqual((code, document["reason"], document["attempt_consumed"]), (4, "alias_attempt_exists", False))
        self.assertEqual([path.name for path in other.iterdir()], ["n1.result.json"])
        self.assertEqual(len(self.server.requests), 2)

    def test_signal_after_the_attempt_reads_nothing(self):
        self.n1_done()
        original = live.Run.create_attempt

        def create(run, *args):
            created = original(run, *args)
            live._on_signal(signal.SIGTERM, None)
            return created

        with mock.patch.object(live.Run, "create_attempt", create):
            code, document = self.bearer()
        self.assertEqual((code, document["status"], document["attempt_consumed"], document["credential"]),
                         (1, "interrupted", True, {"read": False, "reason": None}))
        self.assertEqual(self.server.requests, [])

    def test_signal_between_requests_skips_the_profile(self):
        self.n1_done()

        def resolver(host, port, *args):
            live._on_signal(signal.SIGTERM, None)
            return loopback_resolver(host, port, *args)

        code, document = self.bearer(self.loopback_wiring(resolver=staticmethod(resolver)))
        self.assertEqual((code, document["status"], document["profile"], document["usage"]["http_status"]),
                         (1, "interrupted", None, 200))
        self.assertEqual((self.server.labels(), self.server.labels("/api/oauth/profile")), (["A"], []))
        self.assertClean()

    def forge_n1(self, mode=0o600, **changes):
        document = {"schema": live.SCHEMA, "step": "n1", "status": "fresh_windows", "native_sha256_matches": True,
                    "alias": "alpha", "enrollment_tag": live._enrollment_tag(self.registration()),
                    "attempt_created_utc": live._utc(),
                    "harness": {"classification": "fresh_windows", "identity_matches_before": True,
                                "identity_matches_after": True, "server_policy_written": False,
                                "model_events": 0, "replies": [{"cost_zero": True}],
                                "native": {"group_empty": True},
                                "cache": {"advanced": True, "advance_ms": 1, "account_matches": True}}}
        harness = changes.pop("harness", {})
        document.update(changes)
        document["harness"].update(harness)
        path = self.out / "n1.result.json"
        if path.exists():
            path.unlink()
        path.write_text(json.dumps(document))
        path.chmod(mode)

    def test_requires_a_completed_n1(self):
        def utc(offset):
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))

        inconclusive = {"status": "inconclusive", "harness": {"classification": "inconclusive", "cache": {
            "advanced": False, "advance_ms": None, "account_matches": True}}}
        cases = [("n1_result_missing", None),
                 ("n1_not_completed", {"status": "failed", "harness": {"classification": "failed"}}),
                 ("n1_not_completed", {"status": "refused", "harness": {"classification": "refused"}}),
                 ("n1_not_completed", {"status": "interrupted"}),
                 ("n1_not_completed", {"status": "inconclusive"}),  # status and classification disagree
                 ("n1_not_completed", {"harness": {"server_policy_written": None}}),
                 ("n1_not_completed", {"harness": {"identity_matches_after": False}}),
                 ("n1_not_completed", {"native_sha256_matches": False}),
                 ("n1_not_completed", {"schema": "lc-live-claude-1"}),
                 ("n1_not_completed", {"harness": {"model_events": 1}}),
                 ("n1_not_completed", {"status": "inconclusive", "harness": {
                     "classification": "inconclusive", "replies": [{"cost_zero": True}, {"cost_zero": False}]}}),
                 ("n1_not_completed", {"harness": {"replies": []}}),
                 ("n1_not_completed", {"harness": {"native": {"group_empty": False}}}),
                 ("n1_not_completed", {"harness": {"cache": None}}),
                 ("n1_alias_mismatch", {"alias": "beta"}),
                 ("n1_enrollment_mismatch", {"enrollment_tag": "0" * 16}),
                 ("n1_result_stale", {"attempt_created_utc": utc(-2 * live.N1_MAX_AGE_S)}),
                 ("n1_result_stale", {"attempt_created_utc": utc(2 * live.N1_CLOCK_SKEW_S)}),
                 ("n1_result_stale", {"attempt_created_utc": None}),
                 ("n1_incomplete", inconclusive),
                 ("n1_incomplete", {"status": "unavailable", "harness": {"classification": "unavailable"}}),
                 ("n1_cache_account_mismatch", {"status": "inconclusive", "harness": {
                     "classification": "inconclusive", "cache": {"advanced": True, "advance_ms": 5,
                                                                 "account_matches": False}}}),
                 ("n1_result_not_private", {"mode": 0o644})]
        for reason, changes in cases:
            with self.subTest(reason=reason, changes=changes):
                if changes is not None:
                    changes = json.loads(json.dumps(changes))
                    self.forge_n1(changes.pop("mode", 0o600), **changes)
                code, document = self.bearer()
                self.assertEqual((code, document["reason"], document["attempt_consumed"]), (3, reason, False))
                self.assertFalse((self.out / "bearer.attempt").exists())
                self.assertFalse(self.alias_marker("bearer").exists())
        self.assertEqual(self.server.requests, [])
        self.forge_n1(**json.loads(json.dumps(inconclusive)))
        code, document = self.bearer(None, "--accept-incomplete-n1")
        self.assertEqual((code, document["n1_classification"], document["n1_gate"]), (0, "inconclusive", "completed"))
        self.assertEqual(len(self.server.requests), 2)

    def test_broader_gate_still_refuses_a_foreign_cache_stamp(self):
        self.forge_n1(status="unavailable", harness={"classification": "unavailable", "cache": {
            "advanced": True, "advance_ms": 5, "account_matches": False}})
        code, document = self.bearer(None, "--accept-incomplete-n1")
        self.assertEqual((code, document["reason"]), (3, "n1_cache_account_mismatch"))
        self.assertEqual(self.server.requests, [])

    def test_registration_and_environment_refusals(self):
        self.n1_done()
        cases = [("conflicting_environment", {"CLAUDE_CODE_OAUTH_TOKEN": ""}),
                 ("conflicting_environment", {"HTTPS_PROXY": "http://127.0.0.1:9"}),
                 ("config_dir_mismatch", {"CLAUDE_CONFIG_DIR": str(self.config) + "/"})]
        for reason, extra in cases:
            with self.subTest(reason=reason, extra=extra):
                with mock.patch.dict(os.environ, extra):
                    code, document = self.bearer()
                self.assertEqual((code, document["reason"]), (3, reason))
        (self.config / "remote-settings.json").write_text("{}")
        code, document = self.bearer()
        self.assertEqual((code, document["reason"]), (3, "policy_server_managed_present"))
        os.unlink(self.config / "remote-settings.json")
        self.native_login(account="lc-other-account")
        code, document = self.bearer()
        self.assertEqual((code, document["reason"]), (3, "identity_mismatch_before"))
        self.assertFalse((self.out / "bearer.attempt").exists())
        self.assertEqual(self.server.requests, [])

    def test_credential_refusal_consumes_the_attempt_without_network(self):
        self.n1_done()
        os.unlink(self.config / ".credentials.json")
        code, document = self.bearer()
        self.assertEqual((code, document["status"], document["credential"]),
                         (1, "credential_refused", {"read": False, "reason": "credential_file_absent"}))
        self.assertTrue(document["attempt_consumed"])
        self.assertEqual(self.server.requests, [])
        self.assertEqual(json.loads((self.out / "bearer.result.json").read_text())["status"], "credential_refused")

    def test_expired_credential_is_left_to_native(self):
        self.n1_done()
        self.credential(expires=int(time.time() * 1000) + 1000)
        code, document = self.bearer()
        self.assertEqual((code, document["credential"]["reason"]), (1, "expired_native_refresh_required"))
        self.assertEqual(self.server.requests, [])
        self.assertClean()

    def test_rejected_bearer_makes_exactly_one_request_each(self):
        self.n1_done()
        self.credential(UNKNOWN)
        code, document = self.bearer()
        self.assertEqual((code, document["usage"]["http_status"], document["usage"]["error_type"]),
                         (1, 401, "authentication_error"))
        self.assertEqual(document["profile"], {"available": False, "account_uuid_matches": None,
                                               "organization_uuid_matches": None})
        self.assertEqual((self.server.labels(), self.server.labels("/api/oauth/profile")), (["unknown"], ["unknown"]))
        self.assertClean()

    def test_profile_mismatch(self):
        self.server.revoke(ACCESS)
        self.server.register(ACCESS, fake_usage.FakeAccount("B", "lc-other-account", ORG, windows()))
        self.n1_done()
        code, document = self.bearer()
        self.assertEqual((code, document["profile"]), (1, {"available": True, "account_uuid_matches": False,
                                                           "organization_uuid_matches": True}))
        self.assertClean()

    def test_unlisted_row_names_are_counted_not_kept(self):
        row = {"utilization": 1, "resets_at": None}
        self.account.body = windows(seven_day_opus=row, seven_day_lcfixturemodel=row, lc_brand_new_row=row)
        self.n1_done()
        code, document = self.bearer()
        usage = document["usage"]
        self.assertEqual((usage["other_families"], usage["other_family_rows_unlisted"], usage["unknown_rows"]),
                         (["seven_day_opus"], 1, 1))
        self.assertEqual((usage["unproved_rows"], usage["unproved_rows_unlisted"]), ([], 2))
        for text in self.outputs + [(self.out / "bearer.result.json").read_text()]:
            self.assertNotIn("lcfixturemodel", text)
            self.assertNotIn("lc_brand_new_row", text)

    def test_bounded_deadline_propagates(self):
        self.account.mode = "slow"
        self.n1_done()
        start = time.monotonic()
        code, document = self.bearer(self.loopback_wiring(socket_timeout=0.2, total_deadline=0.6))
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual((code, document["usage"]["outcome"]), (1, "deadline_exceeded"))
        self.assertEqual((document["bounds"]["socket_timeout_s"], document["bounds"]["total_deadline_s"]), (0.2, 0.6))


@unittest.skipUnless(os.access("/usr/bin/openssl", os.X_OK), "needs /usr/bin/openssl for a loopback certificate")
class BearerTLSTests(BearerTests):
    """The same driver over verified TLS: an injected CA verifies; system trust refuses."""

    @classmethod
    def setUpClass(cls):
        cls.tls = Path(tempfile.mkdtemp(prefix="quota-lc-tls-"))
        cls.cert, cls.key = cls.tls / "cert.pem", cls.tls / "key.pem"
        subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(cls.key), "-out", str(cls.cert)],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tls, ignore_errors=True)

    def setUp(self):
        original = fake_usage.FakeUsageServer.__enter__
        cert, key = self.cert, self.key

        def enter(server):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            server.httpd.socket = context.wrap_socket(server.httpd.socket, server_side=True)
            return original(server)

        with mock.patch.object(fake_usage.FakeUsageServer, "__enter__", enter):
            super().setUp()

    def loopback_wiring(self, scheme="https", **overrides):
        overrides.setdefault("tls_context", ssl.create_default_context(cafile=str(self.cert)))
        return super().loopback_wiring(scheme, **overrides)

    def test_system_trust_refuses_and_sends_nothing(self):
        self.n1_done()
        code, document = self.bearer(self.loopback_wiring(tls_context=None))
        self.assertEqual((code, document["usage"]["outcome"], document["profile"]["available"]),
                         (1, "transport_error", False))
        self.assertEqual(self.server.requests, [])


def _child(spec_path):
    """SIGTERM test helper: the driver in its own process with fixture wiring."""
    spec = json.loads(Path(spec_path).read_text())
    wiring = type("ChildWiring", (live.Wiring,), {
        "expected_native_sha256": spec["sha"],
        "n1_options": {"managed_dirs": [spec["managed"]], "platform": "linux", "deadlines": spec["deadlines"]}})
    sys.exit(live.main(spec["argv"], wiring))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _child(sys.argv[2])
    unittest.main()
