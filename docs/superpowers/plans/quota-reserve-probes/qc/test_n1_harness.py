"""Offline N1 harness tests with a fake native binary. No real account, no network."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("qc_n1", HERE / "n1_harness.py")
n1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(n1)
FIXTURE_TOKEN = "QC-FAKE-NATIVE-ECHO-ACCESS-NOT-A-CREDENTIAL"
IDENT = {"account_uuid": "acct-a", "organization_uuid": "org-a"}


def windows(five=12.5, week=35.5):
    now = time.time()
    iso = lambda offset: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + offset))  # noqa: E731
    out = {}
    if five is not None:
        out["five_hour"] = {"utilization": five, "resets_at": iso(3600)}
    if week is not None:
        out["seven_day"] = {"utilization": week, "resets_at": iso(86400)}
    return out


class Base(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.root = Path(tempfile.mkdtemp(prefix="quota-qc-n1test-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.secrets = self.root / "secrets"
        self.secrets.mkdir(mode=0o700)
        self.paths = n1.AccountPaths(str(self.secrets), "alpha")
        self.bin = self.root / "bin"
        self.bin.mkdir(mode=0o700)
        self.native = self.bin / "claude"
        self.native.write_text('#!/bin/sh\nexec "%s" -I -B "%s" "$0" "$@"\n' % (sys.executable, HERE / "fake_claude.py"))
        self.native.chmod(0o700)
        self.env = {"HOME": str(self.root / "home"), "PATH": "/usr/bin:/bin", "USER": "fixture",
                    "LANG": "C.UTF-8", "SECRET_UNRELATED": "must-not-pass"}
        self.behave(write_cache=True, rate_limits=windows())

    def behave(self, **behavior):
        (self.bin / "behavior.json").write_text(json.dumps(behavior))

    def native_login(self, ident=IDENT):
        document = {"oauthAccount": {"accountUuid": ident["account_uuid"],
                                     "organizationUuid": ident["organization_uuid"],
                                     "emailAddress": "fixture@example.invalid"}}
        path = Path(self.paths.config) / ".claude.json"
        path.write_text(json.dumps(document))
        path.chmod(0o600)

    def ready(self):
        n1.create_pending(self.paths)
        self.native_login()
        n1.mark_ready(self.paths, IDENT)

    def run_n1(self, **kwargs):
        kwargs.setdefault("policy_paths", [])
        kwargs.setdefault("deadlines", {"persistence": 1.0, "operation": 10.0, "total": 30.0, "shortcut": 0.2})
        kwargs.setdefault("workspace_parent", str(self.root))
        return n1.run_n1(str(self.native), self.paths, self.env, **kwargs)

    def record(self, name):
        return json.loads((self.bin / name).read_text())

    def assertRefused(self, reason, function, *args):
        with self.assertRaises(n1.HarnessRefused) as caught:
            function(*args)
        self.assertEqual(caught.exception.reason, reason)


class RegistrationTests(Base):
    def test_fixed_paths_without_enumeration(self):
        self.assertEqual(self.paths.config, str(self.secrets) + "/native-claude-accounts/alpha/config")
        for name in ("-x", "a/b", "", "a" * 65, "..", "a b", "café"):
            with self.subTest(name=name):
                self.assertRefused("invalid_account_name", n1.AccountPaths, str(self.secrets), name)
        self.assertRefused("secrets_dir_not_canonical", n1.AccountPaths, str(self.secrets) + "/", "alpha")

    def test_pending_registration(self):
        enrollment = n1.create_pending(self.paths)
        record = n1.load_registration(self.paths)
        self.assertEqual((record["state"], record["identity"], record["enrollment_id"]), ("pending", None, enrollment))
        self.assertEqual(record["config_directory"], self.paths.config)
        for path in (self.paths.root, self.paths.account, self.paths.config):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.paths.registration).st_mode & 0o777, 0o600)
        self.assertRefused("account_directory_exists", n1.create_pending, self.paths)

    def test_create_refuses_unsafe_roots(self):
        self.secrets.chmod(0o770)
        self.assertRefused("account_directory_not_private", n1.create_pending, self.paths)
        self.secrets.chmod(0o700)
        target = self.root / "elsewhere"
        target.mkdir(mode=0o700)
        os.symlink(target, self.paths.root)
        self.assertRefused("path_component_unusable", n1.create_pending, self.paths)

    def test_ready_needs_login_metadata_and_confirmation(self):
        n1.create_pending(self.paths)
        self.assertRefused("metadata_unusable", n1.mark_ready, self.paths, IDENT)
        self.native_login({"account_uuid": "acct-b", "organization_uuid": "org-b"})
        self.assertRefused("identity_not_confirmed", n1.mark_ready, self.paths, IDENT)
        self.native_login()
        record = n1.mark_ready(self.paths, IDENT)
        self.assertEqual((record["state"], record["identity"]), ("ready", IDENT))
        self.assertEqual(n1.load_registration(self.paths)["identity"], IDENT)
        self.assertRefused("registration_not_pending", n1.mark_ready, self.paths, IDENT)
        self.assertEqual(sorted(os.listdir(self.paths.account)), ["account.json", "config"])

    def test_registration_strictness(self):
        n1.create_pending(self.paths)
        good = json.loads(Path(self.paths.registration).read_text())
        mutations = [dict(good, extra=1), dict(good, mode="setup-token"), dict(good, config_directory="/x"),
                     dict(good, identity=IDENT), dict(good, state="ready"), dict(good, enrollment_id="1234"),
                     dict(good, state="ready", identity={"account_uuid": "a"})]
        for document in mutations:
            with self.subTest(document=document):
                Path(self.paths.registration).write_text(json.dumps(document))
                with self.assertRaises(n1.HarnessRefused):
                    n1.load_registration(self.paths)
        Path(self.paths.registration).write_text(json.dumps(good))
        Path(self.paths.registration).chmod(0o644)
        self.assertRefused("registration_not_private", n1.load_registration, self.paths)


class SelectorAndPolicyTests(Base):
    def test_environment_refusals(self):
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "HTTPS_PROXY",
                     "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK",
                     "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "NODE_EXTRA_CA_CERTS"):
            with self.subTest(name=name):
                self.assertRefused("conflicting_environment", n1.check_environment, dict(self.env, **{name: ""}))
        self.assertRefused("home_missing", n1.check_environment, {"PATH": "/bin"})

    def test_policy_refusals_and_preservation(self):
        registration = {"identity": IDENT}
        policy = self.root / "policy.json"
        cases = [({"env": {"ANTHROPIC_BASE_URL": "http://x"}}, "policy_conflicting_env"),
                 ({"env": {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}}, "policy_conflicting_env"),
                 ({"apiKeyHelper": "/bin/true"}, "policy_conflicting_auth"),
                 ({"forceLoginMethod": "console"}, "policy_login_method_incompatible"),
                 ({"forceLoginOrgUUID": "org-other"}, "policy_organization_mismatch"),
                 ([], "policy_malformed")]
        for document, reason in cases:
            with self.subTest(reason=reason):
                policy.write_text(json.dumps(document))
                self.assertRefused(reason, n1.check_policy, [str(policy)], registration)
        policy.write_text(json.dumps({"forceLoginMethod": "claudeai", "forceLoginOrgUUID": ["org-a"],
                                      "permissions": {"deny": ["Bash"]}}))
        n1.check_policy([str(policy), str(self.root / "absent.json")], registration)
        self.assertEqual(n1.default_policy_paths("/c", "linux")[1], "/etc/claude-code/managed-settings.json")

    def test_child_environment_is_allowlisted(self):
        env = n1.build_child_env(self.env, self.paths, 4242)
        self.assertNotIn("SECRET_UNRELATED", env)
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.paths.config)
        self.assertEqual(env["HOME"], self.env["HOME"])  # real HOME preserved, not substituted
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:4242")


class RunTests(Base):
    def test_fresh_windows_and_sanitized_output(self):
        self.ready()
        self.behave(write_cache=True, rate_limits=windows(), record_env=True, record_fds=True, echo_token=True)
        stray_r, stray_w = os.pipe()
        os.set_inheritable(stray_w, True)
        self.addCleanup(os.close, stray_r)
        self.addCleanup(os.close, stray_w)
        result = self.run_n1()
        self.assertEqual(result["classification"], "fresh_windows", result)
        self.assertEqual(result["queries"], 1)
        self.assertTrue(result["cache"]["advanced"] and result["cache"]["account_matches"])
        self.assertEqual(result["replies"][0]["windows"]["five_hour"]["utilization"], 12.5)
        text = json.dumps(result)
        for forbidden in (FIXTURE_TOKEN, str(self.root), "acct-a", "org-a", "must-not-pass"):
            self.assertNotIn(forbidden, text)
        env = self.record("env.json")
        self.assertEqual(env["config"], self.paths.config)
        self.assertEqual(env["args"], list(n1.NATIVE_ARGS))
        self.assertNotIn("SECRET_UNRELATED", env["keys"])
        self.assertEqual(self.record("fds.json"), [0, 1, 2, 3])  # 3 is listdir's own descriptor
        self.assertTrue(result["native"]["group_empty"])
        self.assertEqual(result["model_events"], 0)
        self.assertEqual([p for p in self.root.iterdir() if p.name.startswith("quota-qc-n1-")], [])

    def test_pending_or_conflicts_refuse_before_spawn(self):
        n1.create_pending(self.paths)
        self.native_login()
        self.behave(record_env=True)
        self.assertEqual(self.run_n1()["reason"], "registration_pending")
        n1.mark_ready(self.paths, IDENT)
        self.env["HTTPS_PROXY"] = "http://corp.example:3128"
        self.assertEqual(self.run_n1()["reason"], "conflicting_environment")
        del self.env["HTTPS_PROXY"]
        policy = self.root / "managed.json"
        policy.write_text(json.dumps({"apiKeyHelper": "/bin/echo"}))
        self.assertEqual(self.run_n1(policy_paths=[str(policy)])["reason"], "policy_conflicting_auth")
        self.native = Path("claude")
        self.assertEqual(self.run_n1()["reason"], "native_binary_unusable")
        self.assertFalse((self.bin / "env.json").exists())

    def test_cache_shortcut_inconclusive_unless_requery_approved(self):
        self.ready()
        self.behave(write_cache=True, rate_limits=windows(), cache_first_query_skip=True)
        result = self.run_n1()
        self.assertEqual((result["classification"], result["queries"]), ("inconclusive", 1))
        slept = []
        result = self.run_n1(allow_requery=True, sleep=lambda s: (slept.append(s), time.sleep(min(s, 0.1))))
        self.assertEqual((result["classification"], result["queries"]), ("fresh_windows", 2))
        self.assertIn(0.2, slept)
        self.behave(write_cache=False, rate_limits=windows())
        result = self.run_n1(allow_requery=True)
        self.assertEqual((result["classification"], result["queries"]), ("inconclusive", 2))

    def test_asynchronous_persistence_window(self):
        self.ready()
        self.behave(write_cache=True, rate_limits=windows(), cache_delay=0.3)
        self.assertEqual(self.run_n1()["classification"], "fresh_windows")
        self.behave(write_cache=True, rate_limits=windows(), cache_delay=2.0)
        self.assertEqual(self.run_n1(deadlines={"persistence": 0.5, "operation": 10, "total": 30,
                                                "shortcut": 0.2})["classification"], "inconclusive")

    def test_unavailable_results(self):
        self.ready()
        for behavior, reason in (({"rate_limits": None}, "no_windows"),
                                 ({"rate_limits": windows(), "available": False}, "no_windows"),
                                 ({"rate_limits": windows(week=None), "write_cache": True}, "windows_incomplete"),
                                 ({"rate_limits": windows(), "write_cache": True, "cost": 0.5}, "windows_incomplete")):
            with self.subTest(behavior=behavior):
                self.behave(**behavior)
                result = self.run_n1()
                self.assertEqual((result["classification"], result["reason"]), ("unavailable", reason))

    def test_failures_are_bounded_and_reaped(self):
        self.ready()
        for mode, reason in (("model", "model_event"), ("error", "control_error"), ("flood", "output_limit"),
                             ("early-exit", "native_exited_early")):
            with self.subTest(mode=mode):
                self.behave(mode=mode, rate_limits=windows())
                result = self.run_n1()
                self.assertEqual((result["classification"], result["reason"]), ("failed", reason))
                self.assertTrue(result["native"]["group_empty"])
        self.behave(mode="hang")
        start = time.monotonic()
        result = self.run_n1(deadlines={"operation": 1.0, "total": 5.0, "persistence": 0.2, "shortcut": 0.2})
        self.assertEqual(result["reason"], "operation_deadline")
        self.assertLess(time.monotonic() - start, 10)
        self.assertTrue(result["native"]["group_signalled"] and result["native"]["group_empty"])

    def test_grandchild_is_reaped_with_the_group(self):
        self.ready()
        self.behave(write_cache=True, rate_limits=windows(), grandchild=True)
        result = self.run_n1()
        pid = int((self.bin / "grandchild.pid").read_text())
        self.assertTrue(result["native"]["group_empty"])
        with self.assertRaises(ProcessLookupError):
            for _ in range(50):
                os.kill(pid, 0)
                time.sleep(0.05)

    def test_identity_switch_during_run_refuses(self):
        self.ready()
        self.behave(write_cache=True, rate_limits=windows(), switch_identity=True)
        result = self.run_n1()
        self.assertEqual((result["classification"], result["reason"]), ("refused", "identity_changed"))

    def test_auxiliary_traffic_accounting(self):
        self.ready()
        echo = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(echo.close)
        opened = []

        def serve():
            while True:
                try:
                    conn, _ = echo.accept()
                except OSError:
                    return
                with conn:
                    conn.sendall(conn.recv(64))

        threading.Thread(target=serve, daemon=True).start()

        def connector(host, port):
            opened.append((host, port))
            return socket.create_connection(echo.getsockname(), timeout=5)

        self.behave(write_cache=True, rate_limits=windows(), record_env=True,
                    connect=["api.anthropic.com:443", "evil.example:443"])
        result = self.run_n1(tunnel_connector=connector)
        self.assertEqual(result["classification"], "fresh_windows")
        self.assertEqual(result["traffic"]["connect"], {"api.anthropic.com:443": 1})
        self.assertEqual(result["traffic"]["refused"], 1)
        self.assertGreater(result["traffic"]["bytes_up"], 0)
        self.assertEqual(opened, [("api.anthropic.com", 443)])
        self.assertIn("HTTPS_PROXY", self.record("env.json")["keys"])
        log = (self.bin / "connect.log").read_text()
        self.assertIn("api.anthropic.com:443 HTTP/1.1 200", log)
        self.assertIn("evil.example:443 HTTP/1.1 403", log)


if __name__ == "__main__":
    unittest.main()
