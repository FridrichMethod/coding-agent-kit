"""Fabricated-only PTU tests; actual child entries have independent net denial."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load("usage_permission_probe", HERE / "permission_probe.py")
old = load("ptp_test_helpers", HERE.parent / "claude_token" / "test_permission_probe.py")
TOKEN = old.TOKEN
FakeConnection, FakeResponse = old.FakeConnection, old.FakeResponse
RESET = "2026-11-13T12:30:00.123456+00:00"
WINDOW = {"utilization": 12.3456789012345, "resets_at": RESET}


class UsagePermissionTests(unittest.TestCase):
    def setUp(self):
        old.PermissionProbeTests.setUp(self)
        patch = mock.patch.object(old, "probe", probe)
        patch.start()
        self.addCleanup(patch.stop)
        FakeConnection.response.body = json.dumps({"five_hour": WINDOW, "seven_day": WINDOW}).encode()

    request = old.PermissionProbeTests.request
    test_owned_token_one_read_and_account_association = old.PermissionProbeTests.test_owned_token_one_read_and_account_association
    test_unsafe_token_paths_rejected_before_read = old.PermissionProbeTests.test_unsafe_token_paths_rejected_before_read
    test_wrong_owner_size_and_syntax_rejected = old.PermissionProbeTests.test_wrong_owner_size_and_syntax_rejected
    test_wire_header_budget_including_interim_response = old.PermissionProbeTests.test_wire_header_budget_including_interim_response
    test_chunk_trailers_share_header_budget = old.PermissionProbeTests.test_chunk_trailers_share_header_budget
    test_chunk_data_crlf_is_outside_line_metadata_budget = old.PermissionProbeTests.test_chunk_data_crlf_is_outside_line_metadata_budget
    test_valid_wire_response_keeps_body_read_bounded = old.PermissionProbeTests.test_valid_wire_response_keeps_body_read_bounded
    test_deadline_terminates_and_reaps_only_owned_child = old.PermissionProbeTests.test_deadline_terminates_and_reaps_only_owned_child
    test_threaded_parent_does_not_inherit_open_listener = old.PermissionProbeTests.test_threaded_parent_does_not_inherit_open_listener
    test_transport_error_not_retried_or_exposed = old.PermissionProbeTests.test_transport_error_not_retried_or_exposed
    test_malformed_and_oversize_body_never_retries = old.PermissionProbeTests.test_malformed_and_oversize_body_never_retries

    def test_fixed_get_no_body_and_verified_tls(self):
        result = self.request()
        self.assertEqual(len(FakeConnection.calls), 1)
        host, kwargs, method, path, body, headers = FakeConnection.calls[0]
        self.assertEqual((host, method, path, body), ("api.anthropic.com", "GET", "/api/oauth/usage", None))
        self.assertEqual(kwargs["timeout"], 5)
        self.assertTrue(kwargs["context"].check_hostname)
        self.assertEqual(kwargs["context"].verify_mode, probe.ssl.CERT_REQUIRED)
        self.assertEqual(headers, {"Authorization": "Bearer " + TOKEN, "Accept": "application/json", "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-cli/2.1.322 (external, cli)", "Cache-Control": "no-cache", "Connection": "close"})
        self.assertTrue(FakeConnection.closed)
        self.assertTrue(result["windows_complete"])
        self.assertEqual(result["windows"], {"five_hour": WINDOW, "seven_day": WINDOW})
        self.assertIsInstance(result["duration_seconds"], float)

    def test_errors_redirects_never_retry(self):
        for status in (301, 302, 307, 308, 401, 403, 429, 500, 501):
            FakeConnection.calls = []
            FakeConnection.response.status = status
            FakeConnection.response.headers = [("Location", "https://other.invalid/v1/messages")]
            result = self.request()
            self.assertEqual(result["http_status"], status)
            self.assertEqual([c[2:5] for c in FakeConnection.calls], [("GET", "/api/oauth/usage", None)])
            self.assertFalse(result["windows_complete"])
            self.assertNotIn("other.invalid", json.dumps(result))

    def test_window_schema_missing_malformed_duplicates_fail_closed(self):
        malformed = [None, [], {}, {"utilization": 1}, {"resets_at": RESET}]
        for value in (True, False, "1", -1, 101, float("nan"), float("inf"), None, {}, []):
            malformed.append({"utilization": value, "resets_at": RESET})
        for value in (None, "", TOKEN, "2026-11-31T12:00:00Z", "2026-11-13", "2026-11-13T12:00:00", "2026-11-13T12:00:00+99:99", "2026-11-13T12:00:00+01:60", "2026-11-13T12:00:60Z", 1800000000):
            malformed.append({"utilization": 1, "resets_at": value})
        for window in ("five_hour", "seven_day"):
            for value in malformed:
                with self.subTest(window=window, value=value):
                    data = {"five_hour": WINDOW, "seven_day": WINDOW, window: value}
                    FakeConnection.response.body = json.dumps(data).encode()
                    result = self.request()
                    self.assertFalse(result["windows_complete"])
                    self.assertNotIn(window, result["windows"])
                    self.assertEqual(result["window_states"][window], "malformed")
        FakeConnection.response.body = b'{}'
        self.assertEqual(self.request()["window_states"], {"five_hour": "missing", "seven_day": "missing"})
        for body in (b'{"five_hour":{},"five_hour":{}}', b'{"five_hour":{"utilization":1,"utilization":2}}', b'[]'):
            FakeConnection.response.body = body
            self.assertEqual(self.request()["outcome"], "response_invalid")

    def test_numeric_boundaries_no_boolean_or_rounding(self):
        for value in (0, 100, 0.000000000001, 99.999999999999):
            window = {"utilization": value, "resets_at": "2026-11-13T12:00:00Z"}
            FakeConnection.response.body = json.dumps({"five_hour": window, "seven_day": window}).encode()
            self.assertEqual(self.request()["windows"], {"five_hour": window, "seven_day": window})

    def test_json_decimal_overflow_and_precision_do_not_round_into_validity(self):
        for number in ("100.000000000000001", "99.999999999999999", "1e-400", "1e400"):
            FakeConnection.response.body = ('{"five_hour":{"utilization":' + number + ',"resets_at":"' + RESET + '"}}').encode()
            self.assertEqual(self.request()["window_states"]["five_hour"], "malformed")

    def test_socket_timeout_does_not_retry(self):
        FakeConnection.failure = TimeoutError(TOKEN)
        result = self.request()
        self.assertEqual(result["outcome"], "transport_error")
        self.assertEqual(len(FakeConnection.calls), 1)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_allowlisted_output_no_identity_echo_or_unknown_family_names(self):
        FakeConnection.response.headers = [("Date", "Thu, 12 Nov 2026 12:00:00 GMT"), ("Age", "0"), ("Cache-Control", "no-cache, max-age=0"), ("request-id", "req_safe123"), ("retry-after", "5"), ("Set-Cookie", TOKEN), ("x-account-id", "secret-account")]
        data = {"five_hour": WINDOW, "seven_day": WINDOW, "seven_day_private_model": {"utilization": 81}, "account": "secret-account", "input_tokens": 9988, "extra_usage": {"cost": 9988}, "error": {"type": "authentication_error", "message": TOKEN}}
        FakeConnection.response.body = json.dumps(data).encode()
        result = self.request()
        self.assertEqual(result["error_type"], "authentication_error")
        self.assertTrue(result["other_model_windows_present"])
        self.assertFalse(result["windows_complete"])
        self.assertEqual(result["headers"]["request-id"], "req_safe123")
        for forbidden in (TOKEN, "secret-account", "9988", "private_model", "input_tokens", "extra_usage", "81"):
            self.assertNotIn(forbidden, json.dumps(result))
        for name in ("Date", "Age", "Cache-Control", "request-id", "retry-after"):
            FakeConnection.response.headers = [(name, TOKEN)]
            self.assertNotIn(TOKEN, json.dumps(self.request()))
        FakeConnection.response.headers = [("Age", "0"), ("Age", "1")]
        self.assertEqual(self.request()["header_states"]["age"], "malformed")

    def test_defaults_help_and_exact_execution_gate(self):
        for args in ([], ["--describe"], ["--help"]):
            with mock.patch.object(probe, "_read_token", side_effect=AssertionError), mock.patch.object(probe, "_run_owned", side_effect=AssertionError), mock.patch.object(socket, "socket", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
                try:
                    self.assertEqual(probe.main(args), 0)
                except SystemExit as exc:
                    self.assertEqual(exc.code, 0)
        for args in (["--usage"], ["--count-once"], ["--usage-once"], ["--account", "fixture"], ["--usage-once", "--account", "fixture", "--token-file", str(self.path), "--describe"]):
            with mock.patch.object(probe, "_run_owned", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(args), 2)

    def test_unsafe_environment_refuses_before_child(self):
        for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "NODE_EXTRA_CA_CERTS", "AWS_PROFILE", "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_TOKEN"):
            with mock.patch.dict(os.environ, {key: "fixture"}), mock.patch.object(probe, "_run_owned", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(["--usage-once", "--account", "fixture", "--token-file", str(self.path)]), 2)

    def guarded_child(self, *, remove_primary=False):
        original_popen = subprocess.Popen
        guard = old.CHILD_GUARD
        if remove_primary:
            guard = guard.replace("sys.addaudithook(offline_guard)\n", "")
        # Pin the attempted HTTP method independently of the fake transport.
        guard = guard.replace('original_worker = namespace["_worker"]', '''original_worker = namespace["_worker"]
    requests = []
    original_request = namespace["http"].client.HTTPSConnection.request
    def tracked_request(self, method, url, body=None, headers={}, **kwargs):
        requests.append([method, url, body])
        return original_request(self, method, url, body, headers, **kwargs)
    namespace["http"].client.HTTPSConnection.request = tracked_request''')
        guard = guard.replace('result["offline_guard_events"] = blocked_events', 'result["offline_requests"] = requests\n        result["offline_guard_events"] = blocked_events')

        def launch(command, **kwargs):
            self.assertEqual(command[:4], [sys.executable, "-I", "-B", "-c"])
            self.assertEqual(command[5:7], [str(HERE / "permission_probe.py"), "--usage-once"])
            self.assertNotIn(TOKEN, repr(command))
            self.assertEqual(kwargs["env"], {"LANG": "C", "LC_ALL": "C"})
            guarded = list(command)
            guarded[4] = guard + "\n" + command[4]
            return original_popen(guarded, **kwargs)

        with mock.patch.object(probe.subprocess, "Popen", side_effect=launch):
            return probe._run_owned("fixture", str(self.path))

    def test_actual_usage_child_valid_token_denied_independently(self):
        for remove_primary in (False, True):
            result = self.guarded_child(remove_primary=remove_primary)
            self.assertEqual(result["outcome"], "transport_error")
            self.assertEqual(result["offline_requests"], [["GET", "/api/oauth/usage", None]])
            self.assertEqual(result["offline_guard_events"], [] if remove_primary else ["socket.getaddrinfo"])
            self.assertEqual(result["offline_backstop_events"], ["socket.getaddrinfo"] if remove_primary else [])
            self.assertNotIn(TOKEN, json.dumps(result))
        self.path.write_text("fabricated-invalid", encoding="ascii")
        result = self.guarded_child()
        self.assertEqual(result["outcome"], "probe_failed")
        self.assertEqual(result["offline_requests"], [])
        self.assertEqual(result["offline_guard_events"], [])

    def test_worker_suppresses_core_and_exception_details(self):
        with mock.patch.object(probe.ptp.resource, "setrlimit") as limit, mock.patch.object(probe, "_read_token", side_effect=RuntimeError(TOKEN)), mock.patch.object(probe, "_request_once", side_effect=AssertionError):
            result = probe._worker("fixture", str(self.path))
        limit.assert_called_once_with(probe.ptp.resource.RLIMIT_CORE, (0, 0))
        self.assertEqual(result, {"outcome": "probe_failed", "windows_complete": False})


if __name__ == "__main__":
    unittest.main()
