"""Offline tests: exact-bearer usage GET against a loopback fake, and normalization."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location("qc_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bearer = load("bearer")
usage = load("usage")
fake = load("fake_usage")

ACCESS_A = "QC-FABRICATED-ACCESS-A-NOT-A-CREDENTIAL"
ACCESS_B = "QC-FABRICATED-ACCESS-B-NOT-A-CREDENTIAL"
FAR = 4_102_444_800_000


def held(token):
    return bearer.AccessBearer(token, FAR, True, True, "fixture")


def body(**rows):
    return json.dumps(rows).encode()


class NormalizeTests(unittest.TestCase):
    NOW = 1_800_000_000_000

    def norm(self, data, status=200, content_type="application/json", source="direct"):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        return usage.normalize(status, content_type, raw, started_wall_ms=self.NOW - 50,
                               received_wall_ms=self.NOW, received_monotonic=10.0, source=source)

    def full(self, **extra):
        return fake.windows(now_ms=self.NOW, extra=extra)

    def test_general_windows_by_name_and_duration(self):
        result = self.norm(self.full())
        self.assertTrue(result.valid_for({"five_hour", "weekly"}))
        self.assertEqual(result.windows["five_hour"].remaining(), 87.5)
        self.assertEqual(result.windows["seven_day"].duration_ms, 7 * 86400 * 1000)

    def test_other_families_never_substitute(self):
        opus = {"utilization": 1, "resets_at": "2030-01-01T00:00:00Z"}
        data = {"five_hour": self.full()["five_hour"], "seven_day_opus": opus, "seven_day_sonnet": opus,
                "seven_day_oauth_apps": opus, "extra_usage": {"is_enabled": True},
                "limits": [{"kind": "weekly_scoped", "percent": 3, "scope": {"model": {"display_name": "X"}}}],
                "five_hour_fixture_model": opus, "brand_new_row": {}}
        result = self.norm(data)
        self.assertNotIn("seven_day", result.windows)
        self.assertEqual(result.window_states["seven_day"], "missing")
        self.assertFalse(result.valid_for({"weekly"}))
        self.assertTrue(result.valid_for({"five_hour"}))
        self.assertEqual(set(result.other_families), {"seven_day_opus", "seven_day_sonnet", "seven_day_oauth_apps",
                                                      "extra_usage", "limits", "five_hour_fixture_model"})
        self.assertEqual(result.unknown_rows, 1)
        # Present rows of unproved admission semantics; known model rows, the
        # overage pool and empty rows are not in this set.
        self.assertEqual(result.unproved_rows, ("five_hour_fixture_model", "limits", "seven_day_oauth_apps"))

    def test_unproved_rows_ignore_null_and_empty(self):
        data = self.full(seven_day_oauth_apps=None, cinder_cove=None, wattle_ember=None, limits=[],
                         seven_day_opus={"utilization": 1, "resets_at": None}, extra_usage={"is_enabled": True},
                         brand_new_row=None)
        self.assertEqual(self.norm(data).unproved_rows, ())
        for row, value in (("cinder_cove", {"utilization": 1}), ("wattle_ember", {"label": "x"}),
                           ("limits", [{"kind": "k"}]), ("limits", {}), ("limits", "x"), ("brand_new_row", 0),
                           ("seven_day_fixture", {"utilization": 1})):
            with self.subTest(row=row, value=value):
                expected = () if value == {} else (row,)
                self.assertEqual(self.norm(self.full(**{row: value})).unproved_rows, expected)

    def test_window_anomalies_are_unavailable_not_zero(self):
        week = self.full()["seven_day"]
        cases = {
            "null": (None, "missing"),
            "bool": (dict(week, utilization=True), "malformed"),
            "string": (dict(week, utilization="12"), "malformed"),
            "over": (dict(week, utilization=100.5), "malformed"),
            "negative": (dict(week, utilization=-1), "malformed"),
            "nan-string": (dict(week, utilization="NaN"), "malformed"),
            "no-reset": (dict(week, resets_at=None), "reset_unknown"),
            "bad-reset": (dict(week, resets_at="tomorrow"), "malformed"),
            "bad-offset": (dict(week, resets_at="2027-01-15T08:00:00+25:00"), "malformed"),
            "past-reset": (dict(week, resets_at="2000-01-01T00:00:00Z"), "reset_in_past"),
            "beyond": (dict(week, resets_at="2099-01-01T00:00:00Z"), "reset_beyond_duration"),
            "list": ([week], "malformed"),
        }
        for name, (value, state) in cases.items():
            with self.subTest(case=name):
                data = self.full()
                data["seven_day"] = value
                result = self.norm(data)
                self.assertEqual(result.window_states["seven_day"], state)
                self.assertNotIn("seven_day", result.windows)
                self.assertFalse(result.valid_for({"weekly"}))
        five_hour_too_far = dict(self.full()["five_hour"], resets_at="2027-01-20T00:00:00Z")
        result = self.norm(dict(self.full(), five_hour=five_hour_too_far))
        self.assertEqual(result.window_states["five_hour"], "reset_beyond_duration")

    def test_document_anomalies(self):
        for raw in (b"{", b"[]", b'{"five_hour": 1, "five_hour": 2}', b'{"five_hour": NaN}', b"\xff"):
            with self.subTest(raw=raw):
                self.assertEqual(self.norm(raw).outcome, "response_invalid")
        self.assertEqual(self.norm(self.full(), content_type="text/html").outcome, "response_invalid")
        self.assertEqual(self.norm({}).window_states, {"five_hour": "missing", "seven_day": "missing"})
        error = self.norm({"error": {"type": "permission_error", "message": "secret text"}}, status=403)
        self.assertEqual((error.outcome, error.error_type), ("http_status", "permission_error"))
        self.assertNotIn("secret", json.dumps(error.summary()))
        self.assertFalse(error.valid_for(set()))
        odd = self.norm({"error": {"type": "Weird Type!"}}, status=500)
        self.assertIsNone(odd.error_type)
        self.assertEqual(self.norm(dict(self.full(), error={"type": "x"})).outcome, "error_body")

    def test_cache_sourced_data_is_never_fresh(self):
        result = self.norm(self.full(), source="native-cache")
        self.assertFalse(result.fresh)
        self.assertFalse(result.valid_for({"five_hour"}))

    def test_empty_policy_still_requires_fresh_success(self):
        self.assertTrue(self.norm(self.full()).valid_for(set()))
        self.assertFalse(self.norm({"error": {"type": "x"}}, status=401).valid_for(set()))


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.server = fake.FakeUsageServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.a = self.server.register(ACCESS_A, fake.FakeAccount("A", "acct-a", "org-a", fake.windows()))
        self.b = self.server.register(ACCESS_B, fake.FakeAccount("B", "acct-b", "org-b", fake.windows(50, 60)))
        self.endpoint = usage.Endpoint("http", "127.0.0.1", self.server.port)

    def get(self, token, **kwargs):
        return usage.fetch(held(token), self.endpoint, **kwargs)

    def test_exact_bearer_attribution(self):
        first = self.get(ACCESS_A)
        second = self.get(ACCESS_B)
        self.assertTrue(first.valid_for({"five_hour", "weekly"}))
        self.assertEqual(first.windows["five_hour"].utilization, 12.5)
        self.assertEqual(second.windows["five_hour"].utilization, 50.0)
        self.assertEqual(self.server.labels(), ["A", "B"])
        self.assertTrue(all(r["beta"] == usage.OAUTH_BETA for r in self.server.requests))
        self.assertIsNotNone(first.server_date_ms)
        self.assertNotIn(ACCESS_A, json.dumps(first.summary()))

    def test_unknown_or_revoked_bearer_is_unavailable(self):
        self.server.revoke(ACCESS_A)
        result = self.get(ACCESS_A)
        self.assertEqual((result.outcome, result.http_status, result.fresh), ("http_status", 401, False))
        self.assertEqual(self.server.labels(), ["unknown"])

    def test_error_statuses_never_fresh(self):
        for code in (401, 403, 429, 500):
            with self.subTest(code=code):
                self.a.mode = "status:%d" % code
                result = self.get(ACCESS_A)
                self.assertEqual(result.http_status, code)
                self.assertFalse(result.fresh)
                self.assertFalse(result.valid_for(set()))

    def test_no_redirect_no_retry(self):
        self.a.mode = "redirect"
        result = self.get(ACCESS_A)
        self.assertEqual((result.outcome, result.http_status), ("redirect_refused", 302))
        self.assertEqual(len(self.server.requests), 1)
        self.a.mode = "status:503"
        self.get(ACCESS_A)
        self.assertEqual(len(self.server.requests), 2)

    def test_bounds(self):
        self.a.mode = "oversize"
        self.assertEqual(self.get(ACCESS_A).outcome, "response_too_large")
        self.a.mode = "huge-header"
        self.assertEqual(self.get(ACCESS_A).outcome, "transport_error")
        self.a.mode = "malformed"
        self.assertEqual(self.get(ACCESS_A).outcome, "response_invalid")
        self.a.mode = "text"
        self.assertEqual(self.get(ACCESS_A).outcome, "response_invalid")
        self.a.mode = "slow"
        start = time.monotonic()
        result = self.get(ACCESS_A, socket_timeout=0.2, total_deadline=0.6)
        self.assertEqual(result.outcome, "deadline_exceeded")
        self.assertLess(time.monotonic() - start, 1.5)

    def test_proxy_environment_is_not_consulted(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
                                          "ALL_PROXY": "http://127.0.0.1:9"}):
            self.assertTrue(self.get(ACCESS_A).valid_for({"five_hour"}))

    def test_endpoint_policy(self):
        with self.assertRaises(ValueError):
            usage.Endpoint("http", "api.anthropic.com", 80)
        with self.assertRaises(ValueError):
            usage.Endpoint("https", "api.anthropic.com", 443, "/v1/messages")
        self.assertEqual((usage.PRODUCTION.scheme, usage.PRODUCTION.host, usage.PRODUCTION.port,
                          usage.PRODUCTION.path), ("https", "api.anthropic.com", 443, "/api/oauth/usage"))
        context = usage.production_context()
        self.assertTrue(context.check_hostname)

    def test_profile_identity_option_retains_two_uuids(self):
        profile = usage.Endpoint("http", "127.0.0.1", self.server.port, usage.PROFILE_PATH)
        self.assertEqual(usage.fetch_profile_identity(held(ACCESS_A), profile), ("acct-a", "org-a"))
        self.assertEqual(usage.fetch_profile_identity(held(ACCESS_B), profile), ("acct-b", "org-b"))
        self.assertIsNone(usage.fetch_profile_identity(held("QC-FABRICATED-UNKNOWN-NOT-A-CREDENTIAL"), profile))
        self.assertEqual(self.server.labels(usage.PROFILE_PATH), ["A", "B", "unknown"])
        with self.assertRaises(ValueError):
            usage.fetch(held(ACCESS_A), profile)
        with self.assertRaises(ValueError):
            usage.fetch_profile_identity(held(ACCESS_A), self.endpoint)

    def test_hung_resolver_is_bounded_and_sends_nothing(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def hung(*args):
            release.wait(10)
            return socket.getaddrinfo(*args)

        start = time.monotonic()
        result = self.get(ACCESS_A, socket_timeout=5, total_deadline=0.5, resolver=hung)
        self.assertEqual(result.outcome, "deadline_exceeded")
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(self.server.requests, [])

    def test_deadline_spent_before_connect_opens_nothing(self):
        now = [0.0]

        def slow_resolution(*args):
            infos = socket.getaddrinfo(*args)
            now[0] = 50.0  # resolution consumed the whole budget
            return infos

        result = self.get(ACCESS_A, total_deadline=10, monotonic=lambda: now[0], resolver=slow_resolution)
        self.assertEqual(result.outcome, "deadline_exceeded")
        self.assertEqual(self.server.requests, [])

    def test_deadline_spent_after_connect_sends_no_header_byte(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        received, done = [], threading.Event()

        def serve():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(1.0)
                try:
                    while True:
                        data = conn.recv(4096)
                        if not data:
                            break
                        received.append(data)
                except OSError:
                    pass
            done.set()

        threading.Thread(target=serve, daemon=True).start()
        now = [0.0]
        original = socket.socket

        class JumpAfterConnect(original):
            def connect(self, address):
                super().connect(address)
                now[0] = 50.0  # connect consumed the whole budget

        endpoint = usage.Endpoint("http", "127.0.0.1", listener.getsockname()[1])
        with mock.patch.object(usage.socket, "socket", JumpAfterConnect):
            result = usage.fetch(held(ACCESS_A), endpoint, total_deadline=10, monotonic=lambda: now[0])
        self.assertEqual(result.outcome, "deadline_exceeded")
        self.assertTrue(done.wait(5))
        self.assertEqual(b"".join(received), b"")

    def test_closed_port_is_transport_error(self):
        dead = usage.Endpoint("http", "127.0.0.1", 9)
        result = usage.fetch(held(ACCESS_A), dead, socket_timeout=0.5, total_deadline=1)
        self.assertIn(result.outcome, ("transport_error", "deadline_exceeded"))
        self.assertFalse(result.fresh)



@unittest.skipUnless(os.access("/usr/bin/openssl", os.X_OK), "needs /usr/bin/openssl for a loopback certificate")
class TLSFetchTests(unittest.TestCase):
    """The HTTPS path: handshake under the same watchdog, verified certificate and host name."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="quota-qc-tls-"))
        cls.cert, cls.key = cls.root / "cert.pem", cls.root / "key.pem"
        subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                        "-keyout", str(cls.key), "-out", str(cls.cert)],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.server = fake.FakeUsageServer()
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(self.cert, self.key)
        self.server.httpd.socket = server_context.wrap_socket(self.server.httpd.socket, server_side=True)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.server.register(ACCESS_A, fake.FakeAccount("A", "acct-a", "org-a", fake.windows()))
        self.endpoint = usage.Endpoint("https", "127.0.0.1", self.server.port)

    def test_verified_tls_fetch(self):
        context = ssl.create_default_context(cafile=str(self.cert))
        result = usage.fetch(held(ACCESS_A), self.endpoint, context=context)
        self.assertTrue(result.valid_for({"five_hour", "weekly"}), result.summary())
        self.assertEqual(self.server.labels(), ["A"])

    def test_untrusted_certificate_sends_nothing(self):
        result = usage.fetch(held(ACCESS_A), self.endpoint, context=ssl.create_default_context(),
                             total_deadline=3)
        self.assertEqual(result.outcome, "transport_error")
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
