"""Offline contract tests. All credentials are fabricated; transports are fakes."""
import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import threading
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("permission_probe", HERE / "permission_probe.py")
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
TOKEN = "sk-ant-oat01-" + "fabricated_offline_only_" * 3


class FakeResponse:
    status = 200
    headers = []
    body = b'{"input_tokens":999,"account":"must not survive"}'

    def getheaders(self):
        return self.headers

    def read(self, limit):
        return self.body[:limit]


class FakeConnection:
    calls = []
    response = FakeResponse()
    failure = None
    closed = False

    def __init__(self, host, **kwargs):
        self.host, self.kwargs = host, kwargs
        type(self).closed = False

    def request(self, method, path, body, headers):
        type(self).calls.append((self.host, self.kwargs, method, path, body, headers))
        if type(self).failure:
            raise type(self).failure

    def getresponse(self):
        return type(self).response

    def close(self):
        type(self).closed = True


class PermissionProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ptp-offline-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "claude-fixture"
        self.path.write_text(TOKEN + "\n", encoding="ascii")
        self.path.chmod(0o600)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.root)}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        FakeConnection.calls = []
        FakeConnection.failure = None
        FakeConnection.response = FakeResponse()

    def request(self):
        with mock.patch.object(probe.http.client, "HTTPSConnection", FakeConnection):
            return probe._request_once(TOKEN)

    def test_defaults_describe_help_never_read_or_connect(self):
        for args in ([], ["--describe"], ["--help"], ["--account", "fixture"]):
            with self.subTest(args=args), mock.patch.object(probe, "_read_token", side_effect=AssertionError), mock.patch.object(probe, "_run_owned", side_effect=AssertionError), mock.patch.object(socket, "socket", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    probe.main(args)
                except SystemExit as exc:
                    self.assertIn(exc.code, (0, 2))

    def test_execution_flag_must_be_exact(self):
        with mock.patch.object(probe, "_run_owned", return_value={}) as worker, contextlib.redirect_stdout(io.StringIO()):
            result = probe.main(["--count", "--account", "fixture", "--token-file", str(self.path)])
        self.assertEqual(result, 2)
        worker.assert_not_called()

    def test_exact_count_payload_tls_and_no_generation(self):
        result = self.request()
        self.assertEqual(len(FakeConnection.calls), 1)
        host, kwargs, method, path, body, headers = FakeConnection.calls[0]
        self.assertEqual((host, method, path), ("api.anthropic.com", "POST", "/v1/messages/count_tokens"))
        self.assertEqual(json.loads(body), {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "quota metadata probe"}]})
        self.assertEqual(kwargs["timeout"], 5)
        self.assertTrue(kwargs["context"].check_hostname)
        self.assertEqual(kwargs["context"].verify_mode, probe.ssl.CERT_REQUIRED)
        self.assertEqual(headers, {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json", "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20", "Cache-Control": "no-cache", "Connection": "close"})
        self.assertTrue(FakeConnection.closed)
        self.assertTrue(result["input_tokens_present"])
        self.assertNotIn("999", json.dumps(result))
        self.assertFalse(result["windows_complete"])

    def test_errors_redirects_501_never_retry_or_generate(self):
        for status in (301, 302, 307, 308, 401, 403, 429, 501, 500):
            with self.subTest(status=status):
                FakeConnection.calls = []
                FakeConnection.response.status = status
                FakeConnection.response.headers = [("Location", "https://attacker.invalid/v1/messages")]
                result = self.request()
                self.assertEqual(result["http_status"], status)
                self.assertEqual([call[3] for call in FakeConnection.calls], ["/v1/messages/count_tokens"])
                self.assertFalse(result["windows_complete"])
                self.assertNotIn("attacker", json.dumps(result))

    def test_transport_error_not_retried_or_exposed(self):
        FakeConnection.failure = OSError(TOKEN)
        result = self.request()
        self.assertEqual(result["outcome"], "transport_error")
        self.assertEqual(len(FakeConnection.calls), 1)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertTrue(FakeConnection.closed)

    def test_owned_token_one_read_and_account_association(self):
        with mock.patch.object(os, "read", wraps=os.read) as read:
            self.assertEqual(probe._read_token("fixture", str(self.path)), TOKEN)
            self.assertEqual(read.call_count, 1)
        for account, path in (("other", self.path), ("../fixture", self.path), ("fixture", Path("claude-fixture"))):
            with self.subTest(account=account), self.assertRaises(probe.ProbeError):
                probe._read_token(account, str(path))

    def test_unsafe_token_paths_rejected_before_read(self):
        for mode in (0o644, 0o400, 0o660, 0o1600):
            self.path.chmod(mode)
            with self.subTest(mode=mode), mock.patch.object(os, "read", side_effect=AssertionError), self.assertRaises(probe.ProbeError):
                probe._read_token("fixture", str(self.path))
        self.path.chmod(0o600)
        hardlink = self.root / "hardlink"
        os.link(self.path, hardlink)
        with self.assertRaises(probe.ProbeError):
            probe._read_token("fixture", str(self.path))
        hardlink.unlink()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(probe.ProbeError):
            probe._read_token("fixture", str(alias / self.path.name))
        self.path.unlink()
        self.path.symlink_to(hardlink)
        with self.assertRaises(probe.ProbeError):
            probe._read_token("fixture", str(self.path))
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(probe.ProbeError):
            probe._read_token("fixture", str(self.path))

    def test_wrong_owner_size_and_syntax_rejected(self):
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1), self.assertRaises(probe.ProbeError):
            probe._read_token("fixture", str(self.path))
        for value in ("", "not a token", TOKEN + "\nsecond line", TOKEN + " " + TOKEN, "x" * 9000):
            self.path.write_text(value, encoding="ascii")
            with self.subTest(value_length=len(value)), self.assertRaises(probe.ProbeError):
                probe._read_token("fixture", str(self.path))

    def test_network_environment_rejected_before_credential_read(self):
        for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "NODE_EXTRA_CA_CERTS"):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: "test"}), mock.patch.object(probe, "_run_owned", side_effect=AssertionError), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(["--count-once", "--account", "fixture", "--token-file", str(self.path)]), 2)

    def test_missing_malformed_duplicate_windows_unusable(self):
        complete = [("anthropic-ratelimit-unified-" + window + "-" + field, value) for window in ("5h", "7d") for field, value in (("utilization", "0.25"), ("reset", "1800000000"), ("status", "allowed"))]
        FakeConnection.response.headers = complete
        result = self.request()
        self.assertTrue(result["windows_complete"])
        for replacement in ([], complete[:-2], complete + [complete[0]], [(name, "NaN" if name.endswith("utilization") else value) for name, value in complete]):
            FakeConnection.response.headers = replacement
            self.assertFalse(self.request()["windows_complete"])

    def test_allowlist_redacts_echo_and_discards_identity(self):
        FakeConnection.response.headers = [("Date", "Wed, 11 Nov 2026 12:00:00 GMT"), ("Age", "0"), ("Cache-Control", "no-cache, max-age=0"), ("request-id", "req_safe123"), ("retry-after", "5"), ("Set-Cookie", TOKEN), ("x-account-id", "account-secret"), ("anthropic-ratelimit-unified-7d-sonnet-utilization", "0.12"), ("x-oauth-scopes", "user:profile")]
        FakeConnection.response.body = json.dumps({"input_tokens": 999, "error": {"type": "authentication_error", "message": TOKEN}, "account": "account-secret"}).encode()
        result = self.request()
        output = json.dumps(result)
        for forbidden in (TOKEN, "account-secret", "user:profile", "0.12", "999"):
            self.assertNotIn(forbidden, output)
        self.assertEqual(result["headers"]["request-id"], "req_safe123")
        self.assertEqual(result["error_type"], "authentication_error")
        self.assertTrue(result["other_model_family_headers_present"])
        self.assertTrue(result["other_scope_headers_present"])
        for name in ("Date", "Age", "Cache-Control", "request-id", "retry-after", "anthropic-ratelimit-unified-5h-utilization"):
            FakeConnection.response.headers = [(name, TOKEN)]
            self.assertNotIn(TOKEN, json.dumps(self.request()))

    def test_malformed_and_oversize_body_never_retries(self):
        for body in (b"not JSON", b"x" * 65537, b'{"error":{"type":"untrusted_secret"}}'):
            FakeConnection.response.body = body
            FakeConnection.calls = []
            result = self.request()
            self.assertNotIn("untrusted_secret", json.dumps(result))
            self.assertEqual(len(FakeConnection.calls), 1)
            self.assertFalse(result["windows_complete"])

    def test_wire_header_budget_including_interim_response(self):
        for wire in (b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * 65536 + b"\r\n\r\n", b"HTTP/1.1 100 Continue\r\nX: " + b"x" * 40000 + b"\r\n\r\nHTTP/1.1 200 OK\r\nX: " + b"x" * 40000 + b"\r\n\r\n"):
            fake_socket = mock.Mock()
            fake_socket.makefile.return_value = io.BytesIO(wire)
            response = probe._BoundedResponse(fake_socket)
            with self.assertRaises((probe.ProbeError, probe.http.client.HTTPException)):
                response.begin()

    def test_chunk_trailers_share_header_budget(self):
        wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx\r\n0\r\nX: " + b"x" * 40000 + b"\r\nY: " + b"x" * 40000 + b"\r\n\r\n"
        fake_socket = mock.Mock()
        fake_socket.makefile.return_value = io.BytesIO(wire)
        response = probe._BoundedResponse(fake_socket)
        response.begin()
        with self.assertRaises(probe.ProbeError):
            response.read(65537)

    def test_valid_wire_response_keeps_body_read_bounded(self):
        wire = b'HTTP/1.1 200 OK\r\nContent-Length: 18\r\n\r\n{"input_tokens":1}'
        fake_socket = mock.Mock()
        fake_socket.makefile.return_value = io.BytesIO(wire)
        response = probe._BoundedResponse(fake_socket)
        response.begin()
        self.assertEqual(response.read(65537), b'{"input_tokens":1}')

    def test_deadline_terminates_and_reaps_only_owned_child(self):
        original_popen = subprocess.Popen
        owned = []

        def launch(command, **kwargs):
            self.assertTrue(kwargs["close_fds"])
            self.assertEqual(kwargs["env"], {"LANG": "C", "LC_ALL": "C"})
            child = original_popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"], **kwargs)
            owned.append(child)
            return child

        unrelated = original_popen([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"])
        try:
            start = time.monotonic()
            with mock.patch.object(probe.subprocess, "Popen", side_effect=launch), mock.patch.object(probe, "TOTAL_SECONDS", 0.1):
                result = probe._run_owned("fixture", str(self.path))
            self.assertEqual(result["outcome"], "deadline_exceeded")
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(len(owned), 1)
            self.assertIsNotNone(owned[0].returncode)
            self.assertIsNone(unrelated.poll())
            with self.assertRaises(ChildProcessError):
                os.waitpid(owned[0].pid, os.WNOHANG)
        finally:
            unrelated.kill()
            unrelated.wait()

    def test_threaded_parent_does_not_inherit_open_listener(self):
        original_popen = subprocess.Popen
        stop = threading.Event()
        thread = threading.Thread(target=stop.wait)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        os.set_inheritable(listener.fileno(), True)
        check = "import os,json;\ntry: os.fstat(" + str(listener.fileno()) + "); closed=False\nexcept OSError: closed=True\nprint(json.dumps({'closed':closed}))"

        def launch(command, **kwargs):
            return original_popen([sys.executable, "-I", "-B", "-c", check], **kwargs)

        thread.start()
        try:
            with mock.patch.object(probe.subprocess, "Popen", side_effect=launch):
                self.assertEqual(probe._run_owned("fixture", str(self.path)), {"closed": True})
        finally:
            listener.close()
            stop.set()
            thread.join()

    def test_child_sanitizes_failures_and_returns_metadata(self):
        with mock.patch.object(probe.http.client, "HTTPSConnection", FakeConnection):
            result = probe._worker("fixture", str(self.path))
        self.assertEqual(result["http_status"], 200)
        with mock.patch.object(probe, "_read_token", side_effect=RuntimeError(TOKEN)):
            result = probe._worker("fixture", str(self.path))
        self.assertEqual(result["outcome"], "probe_failed")
        self.assertNotIn(TOKEN, json.dumps(result))
        # Exercise the actual isolated interpreter without any valid credential.
        self.path.write_text("fabricated-invalid", encoding="ascii")
        result = probe._run_owned("fixture", str(self.path))
        self.assertEqual(result["outcome"], "probe_failed")


if __name__ == "__main__":
    unittest.main()
