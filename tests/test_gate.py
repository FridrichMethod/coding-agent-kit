"""Protected-mode gate core and monitor: lease, routes, admission lock, latch, continuation.

Most cases run a Gate in process against fakes on 127.0.0.1 with fabricated bearers; the process
cases run the gate and monitor entry points with inherited sockets, as the supervisor does.
"""

from dataclasses import replace
import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import pickle
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURES = HERE / "fixtures/gate"
sys.path.insert(0, str(FIXTURES))
import fakes  # noqa: E402

gate = fakes.gate
MONITOR = ROOT / "lib/coding-agent-kit/monitor.py"
MODEL_BODY = json.dumps({"model": "fixture-model", "input": "hello"}).encode()
FOREVER_BODY = json.dumps({"model": "fixture-model", "forever": True}).encode()
BIG_BODY = json.dumps({"identity": fakes.IDENTITY_A, "used": {"five_hour": 10.0, "weekly": 20.0},
                       "pad": "x" * 150000}).encode()  # several reads of 64 KiB
FRAMINGS = ("length", "chunked", "eof")


def reply(body, framing, close=True):
    """An HTTP/1.1 200 reply carrying BODY framed by Content-Length ("length"), by chunks of 40000
    bytes ("chunked") or by the end of the connection ("eof"); close adds Connection: close."""
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + (b"Connection: close\r\n" if close else b"")
    if framing == "length":
        return head + b"Content-Length: %d\r\n\r\n" % len(body) + body
    if framing == "chunked":
        parts = (body[start:start + 40000] for start in range(0, len(body), 40000))
        return (head + b"Transfer-Encoding: chunked\r\n\r\n"
                + b"".join(b"%x\r\n%s\r\n" % (len(part), part) for part in parts) + b"0\r\n\r\n")
    return head + b"\r\n" + body


def cut(body, framing):
    """The reply of BODY in FRAMING up to the middle of its body."""
    full = reply(body, framing)
    return full[:len(full) - len(body) // 2]


def wait_for(predicate, timeout=5.0, step=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def auth(bearer=fakes.BEARER_A, account="acct-A"):
    headers = [("Authorization", "Bearer " + bearer), ("User-Agent", "fixture-native/1")]
    return headers + ([("X-Account", account)] if account else [])


class Clock:
    """A monotonic test clock that moves only when told to (plus 1 us per reading)."""

    def __init__(self):
        self.now, self.lock = 1000.0, threading.Lock()

    def __call__(self):
        with self.lock:
            self.now += 1e-6
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


class Harness:
    def __init__(self, test, name="codex", holds_bearer=False, mode="startup", upstream=None, provider=None,
                 evaluator=None, **kwargs):
        self.upstream = upstream or fakes.FakeUpstream()
        self.provider = provider or fakes.FakeProvider(self.upstream.port, name=name, holds_bearer=holds_bearer)
        self.evaluator = evaluator or fakes.FakeEvaluator()
        self.notes, self.events = [], []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.gate = gate.Gate(self.provider, self.evaluator, max_age=kwargs.pop("max_age", 120), mode=mode,
                              notify=self.notes.append, emit=lambda event, **f: self.events.append((event, f)),
                              **kwargs)
        self.server = gate.Server(self.listener, self.gate)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        test.addCleanup(self.close)

    def close(self):
        self.gate.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()

    def alive(self):
        self.gate.attach()
        self.gate.heartbeat()

    def ready(self):
        """Monitor heard, then the native's own usage read binds bearer A (Codex-like)."""
        self.alive()
        if self.provider.holds_bearer:
            self.provider.startup(self.gate)
        else:
            status, _ = self.request("GET", "/usage", auth())
            assert status == 200, status
        assert self.gate.status()["running"], self.gate.status()

    def request(self, method, path, headers=(), body=b"", timeout=5):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            if body:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body or None)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def raw(self, data):
        """Send raw bytes; return (socket, first response bytes)."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(data)
        return sock

    def stream(self, bearer=fakes.BEARER_A):
        """Open a model stream and wait for its first event; returns the client socket."""
        head = ("POST /model HTTP/1.1\r\nHost: gate\r\nAuthorization: Bearer %s\r\nX-Account: acct-A\r\n"
                "Content-Length: %d\r\n\r\n" % (bearer, len(FOREVER_BODY))).encode() + FOREVER_BODY
        sock = self.raw(head)
        data = b""
        while b"data: 0" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                raise AssertionError("stream closed early: %r" % data)
            data += chunk
        return sock


def read_all(sock, timeout=5.0):
    sock.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    return data


def eof_within(sock, timeout=3.0):
    sock.settimeout(timeout)
    try:
        while True:
            if not sock.recv(65536):
                return True
    except (TimeoutError, socket.timeout):
        return False
    except OSError:
        return True
    finally:
        sock.close()


class Routes(unittest.TestCase):
    def test_default_deny_refuses_before_any_upstream_byte(self):
        h = Harness(self)
        h.ready()
        before = len(h.upstream.connections)
        for method, path in (("GET", "/other"), ("POST", "/usage"), ("GET", "/read?x=1"), ("GET", "/Model"),
                             ("PUT", "/model"), ("GET", "/usage/../model"), ("GET", "http://x/usage")):
            with self.subTest(method=method, path=path):
                status, body = h.request(method, path, auth(), b"{}" if method != "GET" else b"")
                self.assertIn(status, (400, 404, 503))
        self.assertEqual(len(h.upstream.connections), before)
        self.assertEqual(h.request("GET", "/usage?x=1", auth())[0], 200)

    def test_websocket_upgrades_are_refused(self):
        h = Harness(self)
        h.ready()
        before = len(h.upstream.connections)
        status, body = h.request("GET", "/usage", auth() + [("Upgrade", "websocket"), ("Connection", "Upgrade")])
        self.assertEqual(status, 503)
        self.assertIn(b"websocket_refused", body)
        self.assertEqual(len(h.upstream.connections), before)

    def test_route_table_is_exact_and_rejects_malformed_routes(self):
        with self.assertRaises(ValueError):
            gate.RouteTable("v", (gate.Route("GET", "/a", gate.READ), gate.Route("GET", "/a", gate.USAGE)))
        for args in (("GET", "/a", "other"), ("POST", "/a", gate.READ), ("GET", "/a", gate.MODEL),
                     ("GET", "a", gate.READ), ("GET", "/a?b", gate.READ)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                gate.Route(*args)
        table = gate.RouteTable("v", (gate.Route("GET", "/a", gate.READ, query=True),))
        self.assertEqual(table.match("GET", "/a?x=1")[1], "x=1")
        self.assertEqual(table.match("GET", "/a/")[0], None)
        self.assertEqual(table.match("GET", "/a b")[0], None)

    def test_upstream_plain_http_only_to_loopback(self):
        self.assertEqual(gate.Upstream("127.0.0.1", 8080, tls=False).authority, "127.0.0.1:8080")
        self.assertEqual(gate.Upstream("api.example", 443).authority, "api.example")
        with self.assertRaises(ValueError):
            gate.Upstream("api.example", 80, tls=False)


class Admission(unittest.TestCase):
    def test_running_admits_and_forwards_the_native_request_unchanged(self):
        h = Harness(self)
        h.ready()
        status, body = h.request("POST", "/model", auth(), MODEL_BODY)
        self.assertEqual(status, 200)
        self.assertIn(b"data: 0", body)
        forwarded = h.upstream.requests("/model")[-1]
        names = [name.lower() for name, _ in forwarded["headers"]]
        self.assertEqual(dict(forwarded["headers"])["Host"], "127.0.0.1:%d" % h.upstream.port)
        self.assertEqual(dict(forwarded["headers"])["User-Agent"], "fixture-native/1")
        self.assertEqual(names.count("authorization"), 1)
        self.assertEqual(forwarded["body"], MODEL_BODY)

    def test_nothing_is_admitted_before_a_heartbeat_or_a_fresh_pass(self):
        h = Harness(self)
        status, body = h.request("POST", "/model", auth(), MODEL_BODY)
        self.assertEqual((status, b"monitor_unheard" in body), (503, True))
        h.alive()
        status, body = h.request("POST", "/model", auth(), MODEL_BODY)
        self.assertEqual((status, b"checking" in body), (503, True))
        self.assertEqual(h.upstream.requests("/model"), [])

    def test_duplicate_or_extra_credential_headers_latch_before_upstream(self):
        cases = (auth() + [("Authorization", "Bearer " + fakes.BEARER_A)],
                 auth() + [("X-Account", "acct-A")],
                 auth() + [("X-Api-Key", "fixture-key")],
                 auth(account="acct-B"))
        for headers in cases:
            with self.subTest(headers=[name for name, _ in headers]):
                h = Harness(self)
                h.ready()
                status, body = h.request("POST", "/model", headers, MODEL_BODY)
                self.assertEqual(status, 503)
                self.assertEqual(h.gate.status()["state"], "paused")
                self.assertEqual(h.upstream.requests("/model"), [])

    def test_unproved_model_is_refused_without_latching(self):
        h = Harness(self)
        h.ready()
        status, body = h.request("POST", "/model", auth(), json.dumps({"model": "other"}).encode())
        self.assertEqual(status, 503)
        self.assertIn(b"model_unproved", body)
        self.assertEqual(h.gate.status()["state"], "running")

    def test_malformed_bodies_are_refused(self):
        h = Harness(self)
        h.ready()
        for head in (b"POST /model HTTP/1.1\r\nAuthorization: Bearer %s\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
                     b"POST /model HTTP/1.1\r\nAuthorization: Bearer %s\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
                     b"GET /usage HTTP/1.1\r\nAuthorization: Bearer %s\r\nContent-Length: 2\r\n\r\n{}"):
            with self.subTest(head=head[:40]):
                sock = h.raw(head % fakes.BEARER_A.encode())
                reply = read_all(sock)
                self.assertIn(b"request_malformed", reply)
        self.assertEqual(h.upstream.requests("/model"), [])

    def test_refusal_status_per_provider_never_401(self):
        for name, status, kind in (("codex", 503, b"server_error"), ("claude", 400, b"invalid_request_error")):
            with self.subTest(name=name):
                h = Harness(self, name=name)
                h.alive()
                code, body = h.request("POST", "/model", auth(), MODEL_BODY)
                self.assertEqual(code, status)
                self.assertIn(kind, body)
                self.assertNotIn(b"login", body.lower())

        class Bad(fakes.FakeProvider):
            def __init__(self, status, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.status = status

            def refusal(self, reason):
                return self.status, b"{}"

        for name, status in (("codex", 401), ("claude", 401), ("claude", 403), ("claude", 429), ("codex", 200)):
            with self.subTest(name=name, status=status), self.assertRaises(ValueError):
                gate.Gate(Bad(status, 1, name=name), fakes.FakeEvaluator(), max_age=120)


class Latch(unittest.TestCase):
    def test_latch_closes_every_stream(self):
        upstream = fakes.FakeUpstream()
        h = Harness(self, upstream=upstream)
        h.ready()
        streams = [h.stream() for _ in range(3)]
        self.assertEqual(h.gate.status()["streams"], 3)
        h.gate.latch("threshold")
        for sock in streams:
            self.assertTrue(eof_within(sock), "a registered stream stayed open after the latch")
            sock.close()
        self.assertTrue(wait_for(lambda: all(c.closed_at for c in upstream.connections)))
        self.assertEqual(h.gate.status()["streams"], 0)
        self.assertEqual([n["op"] for n in h.notes], ["paused"])
        self.assertEqual(h.notes[0]["reason"], "threshold")

    def test_no_request_byte_reaches_upstream_after_a_latch(self):
        h = Harness(self)
        h.ready()
        h.gate.latch("threshold")
        before = h.upstream.total_bytes()
        connections = len(h.upstream.connections)
        for path, method, body in (("/model", "POST", MODEL_BODY), ("/read", "GET", b"")):
            status, _ = h.request(method, path, auth(), body)
            self.assertEqual(status, 503)
        self.assertEqual((h.upstream.total_bytes(), len(h.upstream.connections)), (before, connections))

    def test_latch_between_connect_and_first_byte_sends_nothing(self):
        h = Harness(self)
        h.ready()
        h.server.after_connect = lambda stream: h.gate.latch("threshold")
        try:
            status = h.request("POST", "/model", auth(), MODEL_BODY)[0]
        except http.client.RemoteDisconnected:
            status = None  # the latch closed the admitted client stream
        self.assertIn(status, (None, 503))
        self.assertTrue(wait_for(lambda: len(h.upstream.connections) == 2))
        connection = h.upstream.connections[1]
        self.assertTrue(wait_for(lambda: connection.closed_at is not None))
        self.assertEqual(bytes(connection.raw), b"")
        self.assertIn(("aborted_before_upstream", {"stream": 1}), h.events)

    def test_request_chunks_are_written_only_while_the_stream_is_live(self):
        h = Harness(self)
        h.ready()
        ok, _, stream = h.gate.admit(gate.Presented(gate.Secret(fakes.BEARER_A), ("acct-A",)), MODEL_BODY, lambda: None)
        self.assertTrue(ok)
        states = []

        class Sock:
            def settimeout(self, value):
                pass

            def sendall(self, chunk):
                states.append(h.gate.state)
                if len(states) == 2:
                    threading.Thread(target=h.gate.latch, args=("threshold",)).start()
                    time.sleep(0.1)  # the latch waits for the lock this chunk holds

        with self.assertRaises(gate.Closed):
            h.gate.send(stream, Sock(), b"x" * (gate.CHUNK * 8))
        self.assertEqual(set(states), {"running"})
        self.assertLess(len(states), 8)
        self.assertEqual(h.gate.status()["state"], "paused")


class MonitorLoss(unittest.TestCase):
    def test_heartbeats_during_a_slow_upstream_write_keep_the_monitor_alive(self):
        clock = Clock()
        h = Harness(self, clock=clock)
        h.ready()
        ok, _, stream = h.gate.admit(gate.Presented(gate.Secret(fakes.BEARER_A), ("acct-A",)), MODEL_BODY, lambda: None)
        self.assertTrue(ok)
        waited = []

        class Sock:
            def settimeout(self, value):
                pass

            def sendall(self, chunk):  # 2 s per chunk, past the 1.5 s liveness lease
                for _ in range(4):
                    clock.advance(0.5)
                    beat = threading.Thread(target=h.gate.heartbeat)
                    beat.start()
                    beat.join(0.5)
                    waited.append(beat.is_alive())

        h.gate.send(stream, Sock(), b"x" * (gate.CHUNK * 2))
        h.gate.tick()
        self.assertEqual(waited, [False] * 8, "a heartbeat waited for the admission lock")
        self.assertEqual((h.gate.status()["lost"], h.gate.status()["state"]), (None, "running"))

    def test_silent_monitor_latches_for_good(self):
        clock = Clock()
        h = Harness(self, clock=clock)
        h.ready()
        sock = h.stream()
        clock.advance(1.0)
        h.gate.tick()
        self.assertIsNone(h.gate.status()["lost"])
        clock.advance(0.6)
        h.gate.tick()
        self.assertEqual(h.gate.status()["lost"], "monitor_stale")
        self.assertTrue(eof_within(sock))
        self.assertEqual(h.notes[-1]["op"], "latched")
        h.gate.heartbeat()
        self.assertEqual(h.gate.status()["lost"], "monitor_stale")
        self.assertEqual(h.gate.arm_continuation(), {"ok": False, "reason": "monitor_lost"})
        self.assertEqual(h.request("GET", "/usage", auth())[0], 503)

    def test_eof_and_launcher_loss_latch_and_a_paused_gate_still_reports_them(self):
        h = Harness(self)
        h.ready()
        h.gate.latch("threshold")
        h.gate.lose("monitor_eof")
        self.assertEqual([n["op"] for n in h.notes], ["paused", "latched"])
        h.gate.lose("launcher_eof")
        self.assertEqual(h.gate.status()["lost"], "monitor_eof")

    def test_first_heartbeat_has_an_attach_grace(self):
        clock = Clock()
        h = Harness(self, clock=clock, attach_grace=5.0)
        h.gate.attach()
        clock.advance(4.0)
        h.gate.tick()
        self.assertIsNone(h.gate.status()["lost"])
        clock.advance(1.5)
        h.gate.tick()
        self.assertEqual(h.gate.status()["lost"], "monitor_stale")

    def test_independent_timer_enforces_the_lease_deadline(self):
        clock = Clock()
        h = Harness(self, clock=clock, max_age=10)
        h.ready()
        sock = h.stream()
        for _ in range(9):
            clock.advance(1.0)
            h.gate.heartbeat()
            h.gate.tick()
        self.assertEqual(h.gate.status()["state"], "running")
        clock.advance(1.1)
        h.gate.heartbeat()
        h.gate.tick()
        self.assertEqual((h.gate.status()["state"], h.gate.status()["reason"]), ("paused", "stale"))
        self.assertTrue(eof_within(sock))

    def test_a_failed_poll_never_renews_freshness(self):
        clock = Clock()
        h = Harness(self, clock=clock, max_age=10, holds_bearer=True)
        h.ready()
        clock.advance(8)
        h.gate.heartbeat()
        h.upstream.usage_status = 500
        self.assertEqual(h.gate.check(h.gate.lease.presented, "poll"), "http_500")
        self.assertEqual(h.gate.status()["state"], "running")
        clock.advance(2.5)
        h.gate.heartbeat()
        h.gate.tick()
        self.assertEqual(h.gate.status()["reason"], "stale")


class Rotation(unittest.TestCase):
    def test_same_identity_rotation_is_held_rebound_and_keeps_streams(self):
        h = Harness(self)
        h.ready()
        sock = h.stream(fakes.BEARER_A)
        status, body = h.request("POST", "/model", auth(fakes.BEARER_A2), MODEL_BODY)
        self.assertEqual(status, 200)
        self.assertEqual(h.gate.status()["epoch"], 2)
        reads = h.upstream.requests("/usage")
        self.assertEqual(dict(reads[-1]["headers"])["Authorization"], "Bearer " + fakes.BEARER_A2)
        self.assertEqual(dict(reads[-1]["headers"])["User-Agent"], gate.USER_AGENT)
        sock.settimeout(2)
        self.assertTrue(sock.recv(65536))  # the epoch-1 stream is still streaming
        sock.close()

    def test_identity_change_pauses_with_zero_bytes_for_the_new_bearer(self):
        h = Harness(self)
        h.ready()
        sock = h.stream(fakes.BEARER_A)
        status, body = h.request("POST", "/model", auth(fakes.BEARER_B), MODEL_BODY)
        self.assertEqual(status, 503)
        self.assertEqual(h.gate.status()["reason"], "bearer_identity_mismatch")
        self.assertTrue(eof_within(sock))
        bodies = [r for r in h.upstream.requests("/model")
                  if dict(r["headers"]).get("Authorization") == "Bearer " + fakes.BEARER_B]
        self.assertEqual(bodies, [])

    def test_concurrent_requests_share_one_rebind_read(self):
        h = Harness(self)
        h.ready()
        h.upstream.usage_delay = 0.3
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            h.request("POST", "/model", auth(fakes.BEARER_A2), MODEL_BODY)[0])) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(results, [200, 200, 200])
        rebinds = [r for r in h.upstream.requests("/usage") if dict(r["headers"])["User-Agent"] == gate.USER_AGENT]
        self.assertEqual(len(rebinds), 1)

    def test_a_poll_for_a_replaced_bearer_never_rebinds_the_lease(self):
        h = Harness(self)
        h.ready()
        old = h.gate.lease.presented
        self.assertEqual(h.request("POST", "/model", auth(fakes.BEARER_A2), MODEL_BODY)[0], 200)
        self.assertEqual(h.gate.status()["epoch"], 2)
        self.assertEqual(h.gate.check(old, "poll"), "superseded")
        self.assertEqual(h.gate.status()["epoch"], 2)
        self.assertTrue(h.gate.lease.secret.matches(gate.Secret(fakes.BEARER_A2)))
        holds = [event for event, _ in h.events].count("rotation_hold")
        self.assertEqual(h.request("POST", "/model", auth(fakes.BEARER_A2), MODEL_BODY)[0], 200)
        self.assertEqual([event for event, _ in h.events].count("rotation_hold"), holds)

    def test_revalidation_budget_pauses(self):
        h = Harness(self, revalidations=(2, 60.0))
        h.ready()
        for bearer in (fakes.BEARER_A2, fakes.BEARER_A):
            self.assertEqual(h.request("POST", "/model", auth(bearer), MODEL_BODY)[0], 200)
        status, body = h.request("POST", "/model", auth(fakes.BEARER_A2), MODEL_BODY)
        self.assertEqual(status, 503)
        self.assertEqual(h.gate.status()["reason"], "revalidation_budget")


class Continuation(unittest.TestCase):
    def test_refused_while_monitor_unheard_stale_or_not_paused(self):
        clock = Clock()
        h = Harness(self, clock=clock, holds_bearer=True)
        self.assertEqual(h.gate.arm_continuation()["reason"], "monitor_unheard")
        h.ready()
        self.assertEqual(h.gate.arm_continuation()["reason"], "not_paused")
        h.gate.latch("threshold")
        clock.advance(2.0)
        reads = len(h.upstream.requests("/usage"))
        self.assertEqual(h.gate.arm_continuation()["reason"], "monitor_stale")
        self.assertEqual(len(h.upstream.requests("/usage")), reads)

    def test_gate_side_check_needs_a_fresh_pass_and_polls_never_resume(self):
        h = Harness(self, holds_bearer=True)
        h.upstream.used = {"five_hour": 96.0, "weekly": 20.0}
        h.alive()
        h.provider.startup(h.gate)
        self.assertEqual((h.gate.status()["state"], h.gate.status()["reason"]), ("paused", "threshold"))
        h.gate.lease = h.gate.lease  # bound to A while paused
        self.assertEqual(h.gate.arm_continuation(), {"ok": False, "reason": "threshold"})
        h.upstream.used = {"five_hour": 10.0, "weekly": 20.0}
        h.gate.poll()
        self.assertEqual((h.gate.status()["state"], h.gate.status()["reason"]), ("paused", "awaiting_continuation"))
        self.assertEqual(h.gate.arm_continuation(), {"ok": True, "reason": "resumed"})
        self.assertEqual(h.notes[-1]["op"], "resumed")
        self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY)[0], 200)

    def test_launcher_driven_read_answers_only_after_arming(self):
        clock = Clock()
        h = Harness(self, clock=clock)
        h.ready()
        h.gate.latch("threshold")
        self.assertEqual(h.request("GET", "/usage", auth())[0], 200)  # before arming: answers nothing
        self.assertEqual(h.gate.status()["reason"], "awaiting_continuation")
        self.assertEqual(h.gate.arm_continuation(), {"ok": False, "needs_read": True, "reason": "armed"})
        self.assertEqual(h.request("GET", "/usage", auth())[0], 200)
        self.assertEqual(h.gate.status()["state"], "running")
        h.gate.latch("threshold")
        h.gate.arm_continuation()
        h.gate.disarm()
        h.request("GET", "/usage", auth())
        self.assertEqual(h.gate.status()["state"], "paused")
        h.gate.arm_continuation()
        clock.advance(31)
        h.gate.heartbeat()
        h.request("GET", "/usage", auth())
        self.assertEqual(h.gate.status()["state"], "paused")
        self.assertIn(("continuation_expired", {}), h.events)

    def test_a_failed_check_uses_up_the_continuation(self):
        h = Harness(self)
        h.ready()
        h.gate.latch("threshold")
        h.gate.arm_continuation()
        h.upstream.usage_status = 500
        h.request("GET", "/usage", auth())
        h.upstream.usage_status = 200
        h.request("GET", "/usage", auth())
        self.assertEqual(h.gate.status()["state"], "paused")

    def test_restarted_gate_starts_paused_and_needs_a_heartbeat(self):
        h = Harness(self, mode="restart", holds_bearer=True)
        self.assertEqual((h.gate.status()["state"], h.gate.status()["reason"]), ("paused", "gate_restart"))
        self.assertEqual(h.evaluator.events[0].name, "gate_restart")
        self.assertEqual(h.gate.arm_continuation()["reason"], "monitor_unheard")
        h.alive()
        self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY)[0], 503)
        self.assertEqual(h.gate.arm_continuation(), {"ok": True, "reason": "resumed"})


class Upstreams(unittest.TestCase):
    def test_gate_fetch_is_neutral_and_capped(self):
        h = Harness(self, holds_bearer=True, usage_reads=(3, 60.0))
        h.ready()
        read = h.upstream.requests("/usage")[0]
        headers = dict(read["headers"])
        self.assertEqual(headers["User-Agent"], gate.USER_AGENT)
        text = json.dumps(read["headers"]).lower()
        for word in ("coding-agent-kit", "codex-as", "claude-as", os.uname().nodename.lower(),
                     os.environ.get("USER", "nobody-here").lower()):
            self.assertNotIn(word, text)
        for _ in range(2):
            h.gate.fetch("GET", "/usage", secret=gate.Secret(fakes.BEARER_A))
        with self.assertRaises(gate.Unavailable) as caught:
            h.gate.fetch("GET", "/usage", secret=gate.Secret(fakes.BEARER_A))
        self.assertEqual(caught.exception.reason, "usage_read_cap")
        self.assertEqual(len(h.upstream.requests("/usage")), 3)
        with self.assertRaises(ValueError):
            h.gate.fetch("GET", "/usage", headers=[("User-Agent", "x")])

    def test_upstream_connections_use_the_captured_proxy(self):
        proxy = fakes.FakeProxy()
        self.addCleanup(proxy.close)
        h = Harness(self, proxy=("127.0.0.1", proxy.port))
        h.ready()
        self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY)[0], 200)
        self.assertEqual(proxy.targets, ["127.0.0.1:%d" % h.upstream.port] * 2)
        self.assertIn("User-Agent: " + gate.USER_AGENT, proxy.heads[0])
        refusing = fakes.FakeProxy(refuse=True)
        self.addCleanup(refusing.close)
        h.gate.proxy = ("127.0.0.1", refusing.port)
        self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY)[0], 503)

    def test_no_proxy_hosts_reach_the_upstream_directly(self):
        proxy = fakes.FakeProxy()
        self.addCleanup(proxy.close)
        h = Harness(self, proxy=("127.0.0.1", proxy.port), no_proxy=(("localhost", "127.0.0.1"),))
        h.ready()
        self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY)[0], 200)
        self.assertEqual(proxy.targets, [])
        self.assertEqual(len(h.upstream.requests("/model")), 1)
        h.gate.no_proxy = (("127.0.0.1",), ("other.example",))  # every set spelling must exempt it
        self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY)[0], 200)
        self.assertEqual(proxy.targets, ["127.0.0.1:%d" % h.upstream.port])

    def test_no_proxy_entries_match_as_the_natives_read_them(self):
        cases = (
            ("*", "api.anthropic.com", 443, True),
            ("anthropic.com", "api.anthropic.com", 443, True),
            (".anthropic.com", "api.anthropic.com", 443, True),
            ("*.anthropic.com", "api.anthropic.com", 443, True),
            (".chatgpt.com", "chatgpt.com", 443, True),
            ("chatgpt.com", "chatgpt.com", 443, True),
            ("chatgpt.com.", "chatgpt.com", 443, True),
            ("gpt.com", "chatgpt.com", 443, False),
            ("chatgpt.com:443", "chatgpt.com", 443, True),
            ("chatgpt.com:8443", "chatgpt.com", 443, False),
            ("10.0.0.0/8", "10.1.2.3", 443, True),
            ("10.0.0.0/8", "api.anthropic.com", 443, False),
            ("127.0.0.1", "127.0.0.1", 443, True),
            ("127.0.0.1:8080", "127.0.0.1", 443, False),
            ("[::1]:443", "::1", 443, True),
            ("::1", "::1", 443, True),
            ("localhost", "api.anthropic.com", 443, False),
        )
        for entry, host, port, expected in cases:
            with self.subTest(entry=entry, host=host):
                self.assertEqual(gate.bypasses(((entry,),), host, port), expected)
        self.assertFalse(gate.bypasses((), "chatgpt.com", 443))
        self.assertFalse(gate.bypasses(((),), "chatgpt.com", 443))
        self.assertFalse(gate.bypasses((("chatgpt.com",), ("localhost",)), "chatgpt.com", 443))

    def test_the_proxy_configuration_value(self):
        self.assertEqual(gate.proxy_setting(None), (None, ()))
        self.assertEqual(gate.proxy_setting(["p.example", 3128]), (("p.example", 3128), ()))
        self.assertEqual(gate.proxy_setting(["p", 1, [["localhost", ".example.com"], ["*"]]]),
                         (("p", 1), (("localhost", ".example.com"), ("*",))))
        for bad in (["p"], ["p", "x"], ["p", 1, "localhost"], ["p", 1, [[1]]], ["p", 1, [], []], "p:1"):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                gate.proxy_setting(bad)

    def test_parse_proxy_accepts_plain_http_host_port_only(self):
        self.assertIsNone(gate.parse_proxy(""))
        self.assertEqual(gate.parse_proxy("http://proxy.example:3128"), ("proxy.example", 3128))
        self.assertEqual(gate.parse_proxy("http://127.0.0.1:8080/"), ("127.0.0.1", 8080))
        for url in ("https://proxy.example:3128", "http://user:pw@proxy.example:3128", "http://proxy.example",
                    "socks5://proxy.example:1080", "http://proxy.example:3128/path", "proxy.example:3128",
                    "http://proxy.example:99999"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                gate.parse_proxy(url)

    def test_verified_tls_to_the_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            made = fakes.make_ca(directory)
            if made is None:
                self.skipTest("openssl is not available to make a fixture CA")
            ca_file, context = made
            upstream = fakes.FakeUpstream(tls=context)
            provider = fakes.FakeProvider(upstream.port, holds_bearer=True, tls=True, ca_file=ca_file)
            h = Harness(self, upstream=upstream, provider=provider)
            h.ready()
            self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY)[0], 200)
            stranger = fakes.FakeProvider(upstream.port, holds_bearer=True, tls=True, ca_file=None)
            other = Harness(self, upstream=fakes.FakeUpstream(tls=context), provider=stranger)
            other.alive()
            stranger.startup(other.gate)
            self.assertEqual(other.gate.status()["reason"], "upstream_unavailable")

    def tunnel_harness(self, mode):
        echo = fakes.EchoServer()
        self.addCleanup(echo.close)
        upstream = fakes.FakeUpstream()
        provider = fakes.FakeProvider(upstream.port, connect_allow={"auth.fixture:443": ("127.0.0.1", echo.port)})
        return Harness(self, upstream=upstream, provider=provider, mode=mode), echo

    def connect(self, h, target):
        sock = h.raw(b"CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target))
        reply = sock.recv(4096)
        sock.close()
        return reply.split(b" ")[1]

    def test_a_running_or_paused_gate_refuses_connect_even_to_listed_targets(self):
        h, echo = self.tunnel_harness("startup")
        h.alive()
        self.assertEqual(self.connect(h, b"auth.fixture:443"), b"403")  # checking
        h.ready()
        self.assertEqual(self.connect(h, b"auth.fixture:443"), b"403")  # running: no opaque tunnel
        h.gate.latch("threshold")
        self.assertEqual(self.connect(h, b"auth.fixture:443"), b"403")
        restarted, _ = self.tunnel_harness("restart")
        restarted.alive()
        self.assertEqual(self.connect(restarted, b"auth.fixture:443"), b"403")
        self.assertFalse(echo.closed.is_set())
        self.assertEqual(bytes(echo.received), b"")

    def test_refresh_mode_serves_only_the_exact_tunnel_allowlist(self):
        h, echo = self.tunnel_harness("refresh")
        h.gate.attach()
        sock = h.raw(b"CONNECT auth.fixture:443 HTTP/1.1\r\n\r\n")
        self.assertIn(b" 200 ", sock.recv(4096))
        sock.sendall(b"refresh")
        self.assertEqual(sock.recv(4096), b"refresh")
        sock.close()
        for target in (b"auth.fixture:444", b"AUTH.fixture:443", b"other.fixture:443", b"127.0.0.1:%d" % echo.port):
            with self.subTest(target=target):
                self.assertEqual(self.connect(h, target), b"403")
        self.assertEqual(h.request("GET", "/usage", auth())[0], 503)
        self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY)[0], 503)
        self.assertEqual(h.upstream.connections, [])

    def test_launcher_loss_closes_a_refresh_tunnel(self):
        h, echo = self.tunnel_harness("refresh")
        h.gate.attach()
        tunnel = h.raw(b"CONNECT auth.fixture:443 HTTP/1.1\r\n\r\n")
        self.assertIn(b" 200 ", tunnel.recv(4096))
        tunnel.sendall(b"ping")
        self.assertEqual(tunnel.recv(4096), b"ping")
        self.assertEqual(h.gate.status()["streams"], 1)
        h.gate.lose("launcher_eof")
        self.assertTrue(eof_within(tunnel))
        self.assertTrue(echo.closed.wait(3))
        self.assertEqual(h.gate.status()["streams"], 0)
        self.assertEqual(self.connect(h, b"auth.fixture:443"), b"403")

    def test_a_tunnel_survives_upstream_silence_after_a_client_write(self):
        h, echo = self.tunnel_harness("refresh")
        h.gate.attach()
        with mock.patch.object(gate, "WRITE_TIMEOUT", 0.2):
            tunnel = h.raw(b"CONNECT auth.fixture:443 HTTP/1.1\r\n\r\n")
            self.addCleanup(tunnel.close)
            self.assertIn(b" 200 ", tunnel.recv(4096))
            tunnel.sendall(b"ping")
            self.assertEqual(tunnel.recv(4096), b"ping")
            time.sleep(0.6)  # upstream silence well past the write timeout
            tunnel.sendall(b"pong")
            tunnel.settimeout(3)
            self.assertEqual(tunnel.recv(4096), b"pong")

    def test_gate_fetch_is_bounded_in_total(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        self.addCleanup(listener.close)
        held = []

        def serve():  # answers every connection with a header that drips one byte per 0.1 s
            while True:
                try:
                    sock, _ = listener.accept()
                except OSError:
                    return
                held.append(sock)
                threading.Thread(target=drip, args=(sock,), daemon=True).start()

        def drip(sock):
            try:
                sock.recv(65536)
                sock.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                while True:
                    sock.sendall(b"a")
                    time.sleep(0.1)
            except OSError:
                pass

        threading.Thread(target=serve, daemon=True).start()
        silent = socket.socket()  # a proxy that accepts and never answers the CONNECT
        silent.bind(("127.0.0.1", 0))
        silent.listen(8)
        self.addCleanup(silent.close)
        provider = fakes.FakeProvider(listener.getsockname()[1])
        for proxy in (None, ("127.0.0.1", silent.getsockname()[1])):
            with self.subTest(proxy=proxy):
                h = Harness(self, provider=provider, proxy=proxy)
                outcome = []

                def fetch():
                    began = time.monotonic()
                    try:
                        h.gate.fetch("GET", "/usage", timeout=0.6)
                        outcome.append(("ok", time.monotonic() - began))
                    except gate.Unavailable as error:
                        outcome.append((error.reason, time.monotonic() - began))

                thread = threading.Thread(target=fetch, daemon=True)
                thread.start()
                thread.join(5)
                self.assertFalse(thread.is_alive(), "the fetch outlived its total timeout")
                self.assertEqual(outcome[0][0], "timeout")
                self.assertLess(outcome[0][1], 2.0)
        for sock in held:
            sock.close()


class ConnectionClose(unittest.TestCase):
    """Upstream replies that say Connection: close, as real servers commonly answer the gate's own
    Connection: close, in every body framing and on every upstream path of the gate."""

    def scripted(self, data, hold=False):
        upstream = fakes.ScriptedUpstream(data, hold=hold)
        self.addCleanup(upstream.close)
        return upstream

    def proxy(self):
        proxy = fakes.FakeProxy()
        self.addCleanup(proxy.close)
        return proxy

    def fetch(self, port, proxy=None, **kwargs):
        """(outcome, seconds) of one Gate.fetch: (status, body), or the Unavailable reason."""
        built = gate.Gate(fakes.FakeProvider(port), fakes.FakeEvaluator(), max_age=120,
                          proxy=None if proxy is None else ("127.0.0.1", proxy.port))
        outcome, began = [], time.monotonic()

        def run():
            try:
                status, _, body = built.fetch("GET", "/usage", secret=gate.Secret(fakes.BEARER_A), **kwargs)
                outcome.append((status, body))
            except gate.Unavailable as error:
                outcome.append(error.reason)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "the fetch outlived its total timeout")
        return outcome[0], time.monotonic() - began

    def test_gate_fetch_reads_a_connection_close_reply_in_every_framing(self):
        proxy = self.proxy()
        for framing in FRAMINGS:
            for close in (True, False):
                for via in (None, proxy):
                    with self.subTest(framing=framing, close=close, proxy=via is not None):
                        upstream = self.scripted(reply(BIG_BODY, framing, close))
                        self.assertEqual(self.fetch(upstream.port, via)[0], (200, BIG_BODY))
                        self.assertIn("\r\nConnection: close\r\n", upstream.heads[0])
                        if via is not None:
                            self.assertEqual(proxy.targets[-1], "127.0.0.1:%d" % upstream.port)

    def test_gate_fetch_deadline_fires_when_a_connection_close_body_stalls(self):
        proxy = self.proxy()
        for framing in ("length", "chunked"):
            for via in (None, proxy):
                with self.subTest(framing=framing, proxy=via is not None):
                    upstream = self.scripted(cut(BIG_BODY, framing), hold=True)
                    outcome, seconds = self.fetch(upstream.port, via, timeout=0.6)
                    self.assertEqual(outcome, "timeout")
                    self.assertLess(seconds, 2.0)

    def test_gate_fetch_releases_its_socket_on_every_outcome(self):
        opened, original = [], gate.Gate.open

        def spy(self_, upstream, deadline=None):
            opened.append(original(self_, upstream, deadline))
            return opened[-1]

        cases = (("close", reply(BIG_BODY, "length"), False, {}, (200, BIG_BODY)),
                 ("keep-alive", reply(BIG_BODY, "length", close=False), True, {}, (200, BIG_BODY)),
                 ("cut short", cut(BIG_BODY, "length"), False, {}, "upstream_unavailable"),
                 ("too large", reply(BIG_BODY, "length"), False, {"limit": 1000}, "response_too_large"),
                 ("stalled", cut(BIG_BODY, "chunked"), True, {"timeout": 0.5}, "timeout"))
        with mock.patch.object(gate.Gate, "open", spy):
            for name, data, hold, kwargs, expected in cases:
                with self.subTest(case=name):
                    upstream = self.scripted(data, hold=hold)
                    self.assertEqual(self.fetch(upstream.port, **kwargs)[0], expected)
                    self.assertEqual(opened[-1].fileno(), -1, "the fetch left its socket open")

    def test_gate_fetch_refuses_a_body_cut_short_by_the_close(self):
        for framing in ("length", "chunked"):
            with self.subTest(framing=framing):
                upstream = self.scripted(cut(BIG_BODY, framing))
                self.assertEqual(self.fetch(upstream.port)[0], "upstream_unavailable")

    def test_a_holding_gate_checks_and_polls_through_connection_close_replies(self):
        proxy = self.proxy()
        for framing in FRAMINGS:
            for via in (None, proxy):
                with self.subTest(framing=framing, proxy=via is not None):
                    upstream = fakes.FakeUpstream()
                    upstream.close_header, upstream.usage_framing = True, framing
                    h = Harness(self, holds_bearer=True, upstream=upstream,
                                proxy=None if via is None else ("127.0.0.1", via.port))
                    h.ready()
                    self.assertEqual(h.gate.check(h.gate.lease.presented, "poll"), "fresh")
                    self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY)[0], 200)

    def test_verified_tls_fetch_reads_a_connection_close_reply(self):
        with tempfile.TemporaryDirectory() as directory:
            made = fakes.make_ca(directory)
            if made is None:
                self.skipTest("openssl is not available to make a fixture CA")
            ca_file, context = made
            for framing in FRAMINGS:
                with self.subTest(framing=framing):
                    upstream = fakes.FakeUpstream(tls=context)
                    upstream.close_header, upstream.usage_framing = True, framing
                    provider = fakes.FakeProvider(upstream.port, holds_bearer=True, tls=True, ca_file=ca_file)
                    h = Harness(self, upstream=upstream, provider=provider)
                    h.ready()
                    self.assertEqual(h.gate.check(h.gate.lease.presented, "poll"), "fresh")

    def test_forwarded_reads_and_model_streams_relay_connection_close_replies(self):
        for framing in FRAMINGS:
            with self.subTest(framing=framing):
                upstream = fakes.FakeUpstream()
                upstream.close_header, upstream.usage_framing = True, framing
                h = Harness(self, upstream=upstream)
                h.ready()  # the native's own usage read, relayed and observed, binds the lease
                self.assertEqual(h.request("GET", "/read", auth()), (200, b'{"ok":true,"path":"/read"}'))
                self.assertEqual(h.request("POST", "/model", auth(), MODEL_BODY),
                                 (200, b"data: 0\n\ndata: 1\n\ndata: 2\n\n"))

    def test_a_tunnel_passes_on_the_target_closing_its_end(self):
        echo = fakes.EchoServer(once=True)
        self.addCleanup(echo.close)
        upstream = fakes.FakeUpstream()
        provider = fakes.FakeProvider(upstream.port, connect_allow={"auth.fixture:443": ("127.0.0.1", echo.port)})
        h = Harness(self, upstream=upstream, provider=provider, mode="refresh")
        h.gate.attach()
        tunnel = h.raw(b"CONNECT auth.fixture:443 HTTP/1.1\r\n\r\n")
        self.addCleanup(tunnel.close)
        self.assertIn(b" 200 ", tunnel.recv(4096))
        tunnel.sendall(b"ping")
        tunnel.settimeout(gate.CONNECT_TIMEOUT / 2)  # less than the pump's join: the end itself must arrive
        data, began = b"", time.monotonic()
        try:
            while True:
                chunk = tunnel.recv(4096)
                if not chunk:
                    break
                data += chunk
        except (TimeoutError, socket.timeout):
            self.fail("the target closed its end but the client saw no end of the tunnel")
        self.assertEqual(data, b"ping")
        self.assertLess(time.monotonic() - began, gate.CONNECT_TIMEOUT / 2)
        tunnel.close()
        self.assertTrue(wait_for(lambda: h.gate.status()["streams"] == 0, timeout=3))
        self.assertTrue(wait_for(lambda: "tunnel_closed" in [event for event, _ in h.events], timeout=3))


class EvaluatorOutput(unittest.TestCase):
    MALFORMED = ({"reset": datetime.datetime(2026, 11, 12, 18, 0)}, ["not", "a", "dict"], {1: "x"},
                 {"remaining": float("nan")}, {"window": {"nested": 1}}, None)

    def harness(self, fields, **kwargs):
        base = fakes.FakeEvaluator()

        def decide(event):
            decision = base(event)
            return decision if decision.running else replace(decision, fields=fields)

        h = Harness(self, evaluator=decide, **kwargs)
        h.encoded = []
        h.gate.notify = lambda message: h.encoded.append(json.dumps(message))  # as the control channel does
        return h

    def admitted(self, h, closed):
        ok, _, _ = h.gate.admit(gate.Presented(gate.Secret(fakes.BEARER_A), ("acct-A",)), MODEL_BODY,
                                lambda: closed.append(1))
        self.assertTrue(ok)

    def test_malformed_fields_from_the_timer_pause_and_close_streams(self):
        for fields in self.MALFORMED:
            with self.subTest(fields=fields):
                clock, closed = Clock(), []
                h = self.harness(fields, clock=clock, max_age=10)
                h.ready()
                self.admitted(h, closed)
                clock.advance(11)
                h.gate.heartbeat()
                h.gate.tick()
                status = h.gate.status()
                self.assertEqual((status["state"], status["reason"], status["fields"]), ("paused", "stale", {}))
                self.assertEqual(closed, [1])
                self.assertEqual(json.loads(h.encoded[-1])["op"], "paused")

    def test_malformed_fields_from_an_observation_pause_with_evaluator_error(self):
        for fields in self.MALFORMED:
            with self.subTest(fields=fields):
                closed = []
                h = self.harness(fields, holds_bearer=True)
                h.ready()
                self.admitted(h, closed)
                h.upstream.used = {"five_hour": 97.0, "weekly": 20.0}
                h.gate.poll()
                status = h.gate.status()
                self.assertEqual((status["state"], status["reason"]), ("paused", "evaluator_error"))
                self.assertEqual(closed, [1])
                self.assertEqual(json.loads(h.encoded[-1])["reason"], "evaluator_error")

    def test_other_malformed_decisions_are_evaluator_errors(self):
        for decision in (gate.Decision(1, None), gate.Decision(False, 7), gate.Decision(False, "x" * 300),
                         gate.Decision(True, None, {"k" * 10: "v" * 2000})):
            with self.subTest(decision=decision):
                h = Harness(self, evaluator=lambda event, decision=decision: decision)
                self.assertEqual(h.gate._decide(gate.Event("health", 0.0, name="x")).reason, "evaluator_error")

    def test_streams_close_before_a_failing_notification(self):
        h = Harness(self)
        h.ready()
        closed = []
        self.admitted(h, closed)

        def broken(message):
            raise TypeError("cannot encode")

        h.gate.notify = broken
        h.gate.latch("threshold")
        self.assertEqual(closed, [1])
        self.assertEqual(h.gate.status()["state"], "paused")

    def test_a_timer_error_latches_and_ends_the_gate(self):
        class Broken:
            def __init__(self):
                self.lost = []

            def tick(self):
                raise RuntimeError("boom")

            def lose(self, reason):
                self.lost.append(reason)

        broken, stop = Broken(), threading.Event()
        thread = threading.Thread(target=gate.run_timer, args=(broken, stop, 0.001), daemon=True)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(stop.is_set())
        self.assertEqual(broken.lost, ["gate_error"])

    def test_tunables_come_from_the_provider(self):
        upstream = fakes.FakeUpstream()
        self.addCleanup(upstream.close)
        provider = fakes.FakeProvider(upstream.port, holds_bearer=True)
        provider.usage_reads, provider.revalidations = (2, 60.0), (1, 30)
        provider.continuation_window, provider.attach_grace = 45.0, 3
        built = gate.Gate(provider, fakes.FakeEvaluator(), max_age=120)
        self.assertEqual((built.usage_reads, built.revalidations, built.continuation_window, built.attach_grace),
                         ((2, 60.0), (1, 30.0), 45.0, 3.0))
        for name, value in (("usage_reads", (0, 60.0)), ("usage_reads", (True, 60.0)), ("revalidations", (1,)),
                            ("continuation_window", 0), ("attach_grace", "10")):
            with self.subTest(name=name, value=value):
                bad = fakes.FakeProvider(upstream.port)
                setattr(bad, name, value)
                with self.assertRaises(ValueError):
                    gate.Gate(bad, fakes.FakeEvaluator(), max_age=120)


class Privacy(unittest.TestCase):
    def test_no_bearer_or_bearer_derived_value_leaves_the_gate(self):
        h = Harness(self, holds_bearer=True)
        h.ready()
        replies = [h.gate.status()]
        h.request("POST", "/model", auth(fakes.BEARER_A2), MODEL_BODY)
        h.request("POST", "/model", auth(fakes.BEARER_B), MODEL_BODY)
        replies += [h.gate.arm_continuation(), h.gate.status(), repr(h.gate.lease), repr(h.gate.lease.presented)]
        text = json.dumps([h.notes, [(e, f) for e, f in h.events], replies], default=repr)
        for bearer in (fakes.BEARER_A, fakes.BEARER_A2, fakes.BEARER_B):
            self.assertNotIn(bearer, text)
            digest = hashlib.sha256(bearer.encode()).hexdigest()
            self.assertNotIn(digest[:16], text)
            self.assertNotIn(gate.Secret(bearer)._digest.hex()[:16], text)
        secret = gate.Secret(fakes.BEARER_A)
        self.assertEqual((repr(secret), str(secret), "%s" % secret), ("Secret(<redacted>)",) * 3)
        with self.assertRaises(TypeError):
            pickle.dumps(secret)
        with self.assertRaises(TypeError):
            json.dumps({"bearer": secret})
        self.assertTrue(secret.matches(gate.Secret(fakes.BEARER_A)))
        self.assertFalse(secret.matches(gate.Secret(fakes.BEARER_A2)))
        for bad in ("", "has space", "x" * 16385, "café", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                gate.Secret(bad)


class GateProcess(unittest.TestCase):
    """The gate and monitor entry points with inherited sockets, as the supervisor runs them."""

    def start(self, mode="startup", monitor=True, heartbeat=0.25, monitor_args=(), **config):
        upstream = getattr(self, "upstream", None) or fakes.FakeUpstream()
        self.upstream = upstream
        self.addCleanup(upstream.close)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        self.port = listener.getsockname()[1]
        control, gate_control = socket.socketpair()
        fds = [listener.fileno(), gate_control.fileno()]
        argv = [sys.executable, "-I", "-B", str(FIXTURES / "provider_gate.py"), "--listen-fds",
                str(listener.fileno()), "--control-fd", str(gate_control.fileno()), "--mode", mode]
        self.monitor = None
        if monitor:
            monitor_side, gate_monitor = socket.socketpair()
            fds.append(gate_monitor.fileno())
            argv += ["--monitor-fd", str(gate_monitor.fileno())]
        payload = dict({"max_age_s": 120, "upstream_port": upstream.port, "heartbeat_max_s": 1.5}, **config)
        process = subprocess.Popen(argv, pass_fds=fds, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, start_new_session=True)
        self.addCleanup(self.stop, process)
        process.stdin.write(json.dumps(payload).encode())
        process.stdin.close()
        gate_control.close()
        listener.close()
        if monitor:
            gate_monitor.close()
            self.monitor = subprocess.Popen([sys.executable, "-I", "-B", str(MONITOR), "--fd",
                                             str(monitor_side.fileno()), "--heartbeat", str(heartbeat),
                                             *monitor_args],
                                            pass_fds=[monitor_side.fileno()], start_new_session=True)
            self.addCleanup(self.stop, self.monitor)
            monitor_side.close()
        self.addCleanup(control.close)
        self.control, self.buffer, self.process = control, b"", process
        return process

    def stop(self, process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):  # macOS: a group of one zombie is EPERM
                pass
        process.wait(5)
        if process.stderr:
            process.stderr.close()

    def messages(self, until, timeout=10.0):
        """Read control messages until until(message) holds; returns them."""
        seen, deadline = [], time.monotonic() + timeout
        self.control.settimeout(0.1)
        while time.monotonic() < deadline:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                message = json.loads(line)
                seen.append(message)
                if until(message):
                    return seen
            try:
                data = self.control.recv(65536)
            except (TimeoutError, socket.timeout):
                continue
            if not data:
                break
            self.buffer += data
        raise AssertionError("no matching control message in %r" % seen)

    def ask(self, op):
        self.control.sendall((json.dumps({"op": op}) + "\n").encode())
        return self.messages(lambda m: m.get("op") == "reply" and m.get("to") == op)[-1]

    def test_ready_status_and_shutdown(self):
        process = self.start(holds_bearer=True)
        ready = self.messages(lambda m: m["op"] == "ready")[-1]
        self.assertEqual((ready["port"], ready["pid"], ready["mode"]), (self.port, process.pid, "startup"))
        self.assertTrue(wait_for(lambda: self.ask("status")["running"]))
        self.assertEqual(self.ask("shutdown")["ok"], True)
        self.assertEqual(process.wait(5), 0)

    def test_replies_echo_the_request_id(self):
        self.start(holds_bearer=True)
        self.messages(lambda m: m["op"] == "ready")
        for number in (7, True, "8"):
            self.control.sendall((json.dumps({"op": "status", "id": number}) + "\n").encode())
            reply = self.messages(lambda m: m.get("op") == "reply")[-1]
            with self.subTest(number=number):
                self.assertEqual(reply.get("id"), 7 if number == 7 else None)

    def test_startup_refusal_exits_3_with_a_reason(self):
        process = self.start(holds_bearer=True, store_bearer="fixture-unknown-bearer")
        refused = self.messages(lambda m: m["op"] == "refused")[-1]
        self.assertEqual(refused["reason"], "http_401")
        self.assertEqual(process.wait(5), 3)

    def test_startup_check_reads_a_connection_close_reply(self):
        self.upstream = fakes.FakeUpstream()
        self.upstream.close_header, self.upstream.usage_framing = True, "length"
        self.start(holds_bearer=True)
        self.messages(lambda m: m["op"] == "ready")
        self.assertTrue(wait_for(lambda: self.ask("status")["running"]))

    def test_bad_configuration_refuses(self):
        process = self.start(max_age_s=True)
        self.assertEqual(self.messages(lambda m: m["op"] == "refused")[-1]["reason"], "config_invalid")
        self.assertEqual(process.wait(5), 3)
        process = self.start(holds_bearer=True, proxy=["127.0.0.1", 1, "localhost"])
        self.assertEqual(self.messages(lambda m: m["op"] == "refused")[-1]["reason"], "config_invalid")
        self.assertEqual(process.wait(5), 3)

    def test_the_captured_proxy_and_its_no_proxy_hosts_reach_the_gate(self):
        proxy = fakes.FakeProxy()
        self.addCleanup(proxy.close)
        process = self.start(holds_bearer=True, proxy=["127.0.0.1", proxy.port, [["127.0.0.1"]]])
        self.messages(lambda m: m["op"] == "ready")
        self.assertTrue(wait_for(lambda: self.ask("status")["running"]))
        self.assertEqual(proxy.targets, [])  # NO_PROXY: the startup check went direct
        self.assertEqual(self.ask("shutdown")["ok"], True)
        self.assertEqual(process.wait(5), 0)
        self.upstream = None
        process = self.start(holds_bearer=True, proxy=["127.0.0.1", proxy.port])
        self.messages(lambda m: m["op"] == "ready")
        self.assertTrue(wait_for(lambda: self.ask("status")["running"]))
        self.assertEqual(proxy.targets, ["127.0.0.1:%d" % self.upstream.port])

    def test_monitor_kill_and_stall_latch(self):
        for how, reason in ((signal.SIGKILL, "monitor_eof"), (signal.SIGSTOP, "monitor_stale")):
            with self.subTest(how=how):
                self.start(holds_bearer=True)
                self.messages(lambda m: m["op"] == "ready")
                self.assertTrue(wait_for(lambda: self.ask("status")["running"]))
                began = time.monotonic()
                os.kill(self.monitor.pid, how)
                latched = self.messages(lambda m: m["op"] == "latched")[-1]
                self.assertEqual(latched["reason"], reason)
                self.assertLess(time.monotonic() - began, 4.0)
                self.assertEqual(self.ask("continue")["reason"], "monitor_lost")
                os.kill(self.monitor.pid, signal.SIGKILL)

    def test_restart_mode_starts_paused(self):
        self.start(mode="restart", holds_bearer=True)
        self.messages(lambda m: m["op"] == "ready")
        status = self.ask("status")
        self.assertEqual((status["state"], status["reason"]), ("paused", "gate_restart"))
        self.assertTrue(wait_for(lambda: self.ask("status")["heard"]))
        self.assertEqual(self.ask("continue")["ok"], True)

    def test_launcher_eof_ends_the_gate(self):
        process = self.start(holds_bearer=True)
        self.messages(lambda m: m["op"] == "ready")
        self.control.close()
        self.assertEqual(process.wait(5), 0)
        self.assertEqual(self.monitor.wait(5), 0)

    def test_monitor_drives_gate_polls_on_the_gate_cadence(self):
        self.start(holds_bearer=True, next_poll=1.0)
        self.messages(lambda m: m["op"] == "ready")
        self.assertTrue(wait_for(lambda: len(self.upstream.requests("/usage")) >= 3, timeout=6))

    def test_monitor_runs_a_reader_command(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "reads"
            reader = [sys.executable, "-I", "-B", "-c",
                      "import sys; open(sys.argv[1], 'a').write('x')", str(marker)]
            self.start(holds_bearer=True, next_poll=1.0,
                       monitor_args=["--reader-argv", json.dumps(reader), "--poll", "1"])
            self.messages(lambda m: m["op"] == "ready")
            self.assertTrue(wait_for(lambda: marker.exists() and len(marker.read_text()) >= 2, timeout=6))
            self.assertEqual(len(self.upstream.requests("/usage")), 1)  # only the startup check


class MonitorUnit(unittest.TestCase):
    def setUp(self):
        self.monitor = fakes.load("kit_monitor", MONITOR)

    def test_options(self):
        options = self.monitor.parse(["--fd", "5", "--reader-argv", '["a", "b"]'])
        self.assertEqual((options["--fd"], options["--reader-argv"], options["--heartbeat"]), (5, ["a", "b"], 0.25))
        for argv in ([], ["--fd"], ["--fd", "5", "--fd", "6"], ["--fd", "5", "--poll", "0"],
                     ["--fd", "5", "--reader-argv", "[]"], ["--fd", "5", "--other", "1"]):
            with self.subTest(argv=argv), self.assertRaises((ValueError, TypeError)):
                self.monitor.parse(argv)

    def test_reader_timeout_kills_the_reader_group(self):
        stop = threading.Event()
        began = time.monotonic()
        ok = self.monitor.run_reader([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"], 0.3, stop)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - began, 5)
        self.assertTrue(self.monitor.run_reader([sys.executable, "-I", "-B", "-c", "pass"], 10, stop))
        self.assertFalse(self.monitor.run_reader(["/nonexistent/reader"], 1, stop))

    def test_heartbeat_continues_while_a_reader_runs(self):
        mine, theirs = socket.socketpair()
        reader = json.dumps([sys.executable, "-I", "-B", "-c", "import time; time.sleep(3)"])
        process = subprocess.Popen([sys.executable, "-I", "-B", str(MONITOR), "--fd", str(theirs.fileno()),
                                    "--poll", "0.2", "--reader-argv", reader, "--reader-timeout", "5"],
                                   pass_fds=[theirs.fileno()], start_new_session=True)
        theirs.close()
        try:
            mine.settimeout(5)
            data, began = b"", time.monotonic()
            while time.monotonic() - began < 1.5:
                data += mine.recv(65536)
            beats = data.count(b'"hb"')
            self.assertGreaterEqual(beats, 4)
            self.assertNotIn(b"polled", data)
        finally:
            mine.close()
            self.assertEqual(process.wait(10), 0)


if __name__ == "__main__":
    unittest.main()
