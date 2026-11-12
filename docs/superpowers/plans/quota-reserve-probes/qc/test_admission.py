"""Required QC admission/rotation sequences with a deterministic clock.

Fabricated bearers and accounts only; quota comes from the loopback fake via
the real exact-bearer client. "upstream" is a recorder that receives a request
only after the controller admitted it.
"""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location("qc_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bearer = load("bearer")
usage = load("usage")
fake = load("fake_usage")
admission = load("admission")
gate_server = load("gate_server")

A = "QC-FABRICATED-ACCESS-A-NOT-A-CREDENTIAL"
A2 = "QC-FABRICATED-ACCESS-A2-NOT-A-CREDENTIAL"
B = "QC-FABRICATED-ACCESS-B-NOT-A-CREDENTIAL"
LABELS = {A: "A", A2: "A2", B: "B"}
MODEL = "claude-fixture-general"
IDENT_A, IDENT_B = ("acct-a", "org-a"), ("acct-b", "org-b")


class Account:
    """One private native account directory with .credentials.json and .claude.json."""

    def __init__(self, root, name):
        self.config = root / "secrets" / "native-claude-accounts" / name / "config"
        self.config.mkdir(parents=True, mode=0o700)
        for path in (root / "secrets", self.config.parent.parent, self.config.parent, self.config):
            path.chmod(0o700)
        self.env = {"CLAUDE_CONFIG_DIR": str(self.config)}

    def _replace(self, name, document):
        staging = self.config / (".staging-" + name)
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle)
        os.rename(staging, self.config / name)

    def native_login(self, token, ident, stale_cache=None):
        self._replace(".credentials.json", {"claudeAiOauth": {
            "accessToken": token, "refreshToken": "QC-FABRICATED-REFRESH-NOT-A-CREDENTIAL",
            "expiresAt": 4_102_444_800_000, "scopes": ["user:inference", "user:profile"]}})
        document = {"oauthAccount": {"accountUuid": ident[0], "organizationUuid": ident[1],
                                     "emailAddress": "fixture@example.invalid"}}
        if stale_cache is not None:
            document["cachedUsageUtilization"] = stale_cache
        self._replace(".claude.json", document)

    def read_bearer(self, clock):
        return bearer.read_linux_bearer(str(self.config), self.env, now_ms=clock.wall_ms())

    def identity(self):
        return admission.read_local_identity(str(self.config))


class Base(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        self.root = Path(tempfile.mkdtemp(prefix="quota-qc-admission-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.clock = admission.FakeClock()
        self.server = fake.FakeUsageServer(server_time=lambda: self.clock.wall_ms() / 1000)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        now = self.clock.wall_ms()
        self.acct = {
            "A": self.server.register(A, fake.FakeAccount("A", *IDENT_A, fake.windows(10, 20, now_ms=now))),
            "A2": self.server.register(A2, fake.FakeAccount("A2", *IDENT_A, fake.windows(10, 20, now_ms=now))),
            "B": self.server.register(B, fake.FakeAccount("B", *IDENT_B, fake.windows(5, 5, now_ms=now))),
        }
        self.usage_endpoint = usage.Endpoint("http", "127.0.0.1", self.server.port)
        self.profile_endpoint = usage.Endpoint("http", "127.0.0.1", self.server.port, usage.PROFILE_PATH)
        self.log = []
        self.upstream = []
        self.native = Account(self.root, "alpha")
        self.native.native_login(A, IDENT_A)

    def quota(self, observed):
        return usage.fetch(observed, self.usage_endpoint, wall_ms=self.clock.wall_ms)

    def profile(self, observed):
        return usage.fetch_profile_identity(observed, self.profile_endpoint, wall_ms=self.clock.wall_ms)

    def controller(self, reserves=None, verifier="local", log=None, **policy):
        reserves = {"five_hour": 10, "weekly": 30} if reserves is None else reserves
        reader = self.native.identity
        identity = (admission.LocalMetadataIdentity(reader) if verifier == "local"
                    else admission.BearerProfileIdentity(reader, self.profile))
        return admission.AdmissionController(IDENT_A, admission.Policy(reserves, **policy), self.quota,
                                             identity, self.clock, self.log if log is None else log)

    def send(self, gate, token, model=MODEL, close=None):
        decision = gate.admit([("Authorization", "Bearer " + token)], model, close or (lambda: None))
        if decision.ok:
            self.upstream.append(LABELS[token])
            self.log.append({"kind": "upstream", "bearer": LABELS[token]})
        return decision

    def fetches(self):
        return self.server.labels()

    def order(self):
        """Compact (kind, detail) list of quota fetch / upstream events."""
        return [(e["kind"], e.get("bearer", e.get("status"))) for e in self.log
                if e["kind"] in ("quota_fetch", "upstream")]


class StartupTests(Base):
    def test_startup_uses_memory_only_bearer_from_store(self):
        gate = self.controller()
        self.assertTrue(gate.startup(self.native.read_bearer(self.clock)).ok)
        self.assertEqual(self.fetches(), ["A"])
        self.assertTrue(self.send(gate, A).ok)
        self.assertEqual(self.upstream, ["A"])
        self.assertNotIn(A, repr(gate.lease))

    def test_startup_refusals_send_no_model_request(self):
        now = self.clock.wall_ms()
        week = fake.windows(10, 20, now_ms=now)["seven_day"]
        cases = {
            "missing-weekly": fake.windows(10, None, now_ms=now),
            "unsupported-family-only": {"five_hour": week | {"resets_at": week["resets_at"]},
                                        "seven_day": None, "seven_day_opus": week},
            "malformed": {"five_hour": {"utilization": "10"}, "seven_day": week},
            "stale-reset": {"five_hour": {"utilization": 1, "resets_at": "2000-01-01T00:00:00Z"},
                            "seven_day": week},
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                self.acct["A"].body = body
                gate = self.controller()
                decision = gate.startup(self.native.read_bearer(self.clock))
                self.assertFalse(decision.ok)
                self.assertEqual(gate.state, "refused")
                self.assertFalse(self.send(gate, A).ok)
        for mode, reason in (("status:403", "quota_403"), ("status:401", "quota_401"),
                             ("malformed", "quota_200"), ("status:429", "quota_429")):
            with self.subTest(mode=mode):
                self.acct["A"].mode = mode
                gate = self.controller()
                decision = gate.startup(self.native.read_bearer(self.clock))
                self.assertEqual((decision.ok, decision.reason), (False, reason))
        self.assertEqual(self.upstream, [])

    def test_threshold_and_equality_refuse_startup(self):
        gate = self.controller(reserves={"five_hour": 90, "weekly": None})
        self.assertEqual(gate.startup(self.native.read_bearer(self.clock)).reason, "threshold_five_hour")
        gate = self.controller(reserves={"five_hour": None, "weekly": 80})  # remaining 80 == reserve
        self.assertEqual(gate.startup(self.native.read_bearer(self.clock)).reason, "threshold_weekly")
        gate = self.controller(reserves={"five_hour": 0, "weekly": None})
        self.assertTrue(gate.startup(self.native.read_bearer(self.clock)).ok)

    def test_server_clock_anomaly_refuses(self):
        self.server.server_time = lambda: self.clock.wall_ms() / 1000 + 3600
        gate = self.controller()
        self.assertEqual(gate.startup(self.native.read_bearer(self.clock)).reason, "clock_anomaly")

    def test_stale_native_cache_is_never_consulted(self):
        self.native.native_login(A, IDENT_A, stale_cache={
            "fetchedAtMs": self.clock.wall_ms() - 10_000, "accountUuid": "acct-a",
            "utilization": {"five_hour": {"utilization": 1, "resets_at": "2100-01-01T00:00:00Z"}}})
        self.acct["A"].mode = "status:403"
        gate = self.controller()
        self.assertEqual(gate.startup(self.native.read_bearer(self.clock)).reason, "quota_403")


class RotationTests(Base):
    def started(self, **kwargs):
        gate = self.controller(**kwargs)
        self.assertTrue(gate.startup(self.native.read_bearer(self.clock)).ok)
        return gate

    def test_A_then_native_refresh_A2_same_account(self):
        gate = self.started()
        closed = []
        self.assertTrue(self.send(gate, A, close=lambda: closed.append("A-stream")).ok)
        self.native.native_login(A2, IDENT_A)  # native refresh writes A2 (rename)
        self.server.revoke(A)
        self.assertTrue(self.send(gate, A2).ok)
        self.assertEqual(self.order(), [("quota_fetch", 200), ("upstream", "A"),
                                        ("quota_fetch", 200), ("upstream", "A2")])
        self.assertEqual(self.fetches(), ["A", "A2"])
        self.assertTrue(gate.lease.bearer.matches(A2))
        self.assertEqual(closed, [])  # same-identity rotation keeps admitted streams
        # A straggler with the revoked old bearer is never forwarded.
        self.assertFalse(self.send(gate, A).ok)
        self.assertEqual(gate.state, "paused")
        self.assertEqual(self.upstream, ["A", "A2"])

    def test_A_to_B_switch_with_stale_A_cache(self):
        gate = self.started()
        closed = []
        self.assertTrue(self.send(gate, A, close=lambda: closed.append("A")).ok)
        stale = {"fetchedAtMs": self.clock.wall_ms(), "accountUuid": "acct-a",
                 "utilization": fake.windows(10, 20, now_ms=self.clock.wall_ms())}
        self.native.native_login(B, IDENT_B, stale_cache=stale)
        decision = self.send(gate, B)
        self.assertEqual((decision.ok, decision.reason), (False, "identity_mismatch"))
        self.assertEqual(self.fetches(), ["A", "B"])  # fresh fetch used the observed B bearer
        self.assertEqual(self.upstream, ["A"])  # no B byte under A's lease
        self.assertEqual(closed, ["A"])
        self.assertFalse(self.send(gate, A).ok)  # latch; no automatic return to A

    def test_switch_where_B_quota_fetch_fails(self):
        gate = self.started()
        self.acct["B"].mode = "status:403"
        self.native.native_login(B, IDENT_B)
        self.assertEqual(self.send(gate, B).reason, "quota_403")
        self.assertEqual(self.upstream, [])

    def test_bearer_B_with_unchanged_local_metadata_is_the_local_verifier_gap(self):
        local = self.started()
        self.assertTrue(self.send(local, B).ok)  # quota is B's own (same bearer) but identity unverified
        self.assertEqual(self.upstream, ["B"])
        self.upstream.clear()
        profile = self.started(verifier="profile")
        decision = self.send(profile, B)
        self.assertEqual((decision.ok, decision.reason), (False, "bearer_identity_mismatch"))
        self.assertEqual(self.upstream, [])
        self.assertEqual(self.server.labels(usage.PROFILE_PATH), ["A", "B"])

    def test_A_B_A(self):
        gate = self.started()
        self.native.native_login(B, IDENT_B)
        self.assertFalse(self.send(gate, B).ok)
        self.native.native_login(A, IDENT_A)
        self.assertFalse(self.send(gate, A).ok)  # paused: the earlier lease is not reused
        before = len(self.fetches())
        token = gate.issue_continuation()
        self.assertTrue(gate.continue_manually(token, self.native.read_bearer(self.clock)).ok)
        self.assertEqual(len(self.fetches()), before + 1)  # fresh A observation required
        self.assertTrue(self.send(gate, A).ok)
        self.assertEqual(self.upstream, ["A"])

    def test_metadata_flip_during_check_refuses(self):
        gate = self.started()
        original = self.quota

        def flipping(observed):
            result = original(observed)
            self.native.native_login(B, IDENT_B)
            return result

        gate.quota = flipping
        self.assertEqual(self.send(gate, A2).reason, "identity_changed_during_check")
        self.assertEqual(self.upstream, [])

    def test_two_concurrent_same_account_sessions_rotate(self):
        log1, log2 = [], []
        s1, s2 = self.controller(log=log1), self.controller(log=log2)
        for gate in (s1, s2):
            self.assertTrue(gate.startup(self.native.read_bearer(self.clock)).ok)
        self.native.native_login(A2, IDENT_A)
        results, barrier = [], threading.Barrier(10)

        def request(gate):
            barrier.wait()
            decision = gate.admit([("Authorization", "Bearer " + A2)], MODEL)
            results.append((gate is s1, decision.ok))

        threads = [threading.Thread(target=request, args=(gate,)) for gate in (s1, s2) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(len(results), 10)
        self.assertTrue(all(ok for _, ok in results))
        self.assertEqual(sorted(self.fetches()), ["A", "A", "A2", "A2"])  # one A2 check per session
        for log in (log1, log2):
            kinds = [e["kind"] for e in log]
            self.assertEqual(kinds.count("rotation_hold"), 1)
            self.assertLess(kinds.index("rotation_hold"), kinds.index("admitted"))

    def test_auth_selector_conflicts_pause(self):
        for headers in ([], [("Authorization", "Bearer " + A), ("Authorization", "Bearer " + A)],
                        [("Authorization", "Bearer " + A), ("x-api-key", "k")],
                        [("Authorization", "Basic abc")]):
            with self.subTest(headers=headers):
                gate = self.started()
                self.assertEqual(gate.admit(headers, MODEL).reason, "auth_selector_conflict")
                self.assertEqual(gate.state, "paused")

    def test_model_family(self):
        gate = self.started(allowed_models=(MODEL, "claude-fixture-opus"))
        self.assertEqual(self.send(gate, A, model="claude-other").reason, "model_family_unproved")
        self.assertTrue(self.send(gate, A, model="claude-fixture-opus").ok)
        now = self.clock.wall_ms()
        self.acct["A2"].body = fake.windows(10, 20, now_ms=now, extra={"seven_day_opus": {
            "utilization": 99, "resets_at": fake.windows(now_ms=now)["seven_day"]["resets_at"]}})
        self.native.native_login(A2, IDENT_A)
        self.assertEqual(self.send(gate, A2, model="claude-fixture-opus").reason, "model_family_unproved")
        self.assertTrue(self.send(gate, A2).ok)

    def test_unproved_rows_refuse_every_model_without_latching(self):
        now = self.clock.wall_ms()
        reset = fake.windows(now_ms=now)["seven_day"]["resets_at"]
        benign = {"seven_day_oauth_apps": None, "cinder_cove": None, "limits": [],
                  "extra_usage": {"is_enabled": True, "monthly_limit": None, "used_credits": None,
                                  "utilization": None}}
        self.acct["A"].body = fake.windows(10, 20, now_ms=now, extra=benign)
        gate = self.started()
        self.assertTrue(self.send(gate, A).ok)
        scoped = [{"kind": "weekly", "group": "g", "percent": 5, "resets_at": reset, "severity": "ok",
                   "is_active": True, "scope": {"model": {"display_name": "Fixture"}}}]
        for row, value in (("limits", scoped), ("seven_day_oauth_apps", {"utilization": 1, "resets_at": reset}),
                           ("wattle_ember", {"label": "credit"}), ("brand_new_row", {"utilization": 1})):
            with self.subTest(row=row):
                self.acct["A2"].body = fake.windows(10, 20, now_ms=now, extra={row: value})
                self.native.native_login(A2, IDENT_A)
                gate = self.started(log=[])
                self.assertEqual(self.send(gate, A2).reason, "quota_row_unproved")
                self.assertEqual(gate.state, "open")  # a refusal, not a latch
        self.assertEqual(self.upstream, ["A"])

    def test_revalidation_budget(self):
        gate = self.started(revalidations_per_minute=2)
        self.assertTrue(self.send(gate, A2).ok)
        self.assertTrue(self.send(gate, A).ok)
        self.assertEqual(self.send(gate, A2).reason, "revalidation_budget")
        self.assertEqual(gate.state, "paused")


class FreshnessTests(Base):
    def started(self, **kwargs):
        gate = self.controller(**kwargs)
        self.assertTrue(gate.startup(self.native.read_bearer(self.clock)).ok)
        return gate

    def test_unchanged_fresh_data_extends_but_failures_do_not(self):
        gate = self.started(max_age_s=100)
        first = gate.lease.deadline
        self.clock.advance(60)
        self.assertEqual(gate.monitor().reason, "extended")  # identical values, new fetch
        second = gate.lease.deadline
        self.assertEqual(second, first + 60)
        for mode in ("status:429", "status:401", "status:403", "malformed", "redirect"):
            with self.subTest(mode=mode):
                self.acct["A"].mode = mode
                self.clock.advance(5)
                self.assertFalse(gate.monitor().ok)
                self.assertEqual(gate.lease.deadline, second)
                self.assertEqual(gate.state, "open")
        self.acct["A"].mode = "normal"
        port = self.server.port
        self.usage_endpoint = usage.Endpoint("http", "127.0.0.1", 9)
        self.assertFalse(gate.monitor().ok)
        self.assertEqual(gate.lease.deadline, second)
        self.usage_endpoint = usage.Endpoint("http", "127.0.0.1", port)
        cache_only = usage.normalize(200, "application/json", json.dumps(self.acct["A"].body).encode(),
                                     started_wall_ms=self.clock.wall_ms(), received_wall_ms=self.clock.wall_ms(),
                                     received_monotonic=0.0, source="native-cache")
        gate.quota = lambda observed: cache_only
        self.assertFalse(gate.monitor().ok)
        self.assertEqual(gate.lease.deadline, second)
        self.clock.advance(100)
        self.assertEqual(gate.monitor().reason, "freshness_expired")
        self.assertEqual(gate.state, "paused")

    def test_expired_freshness_pauses_at_admission(self):
        gate = self.started(max_age_s=30)
        closed = []
        self.assertTrue(self.send(gate, A, close=lambda: closed.append(1)).ok)
        self.clock.advance(30)
        self.assertEqual(self.send(gate, A).reason, "freshness_expired")
        self.assertEqual(closed, [1])
        self.assertEqual(self.upstream, ["A"])

    def test_clock_anomaly_pauses(self):
        gate = self.started()
        self.clock.jump_wall(-10_000)
        self.assertEqual(self.send(gate, A).reason, "clock_anomaly")

    def test_threshold_during_monitoring_pauses_and_closes(self):
        gate = self.started()
        closed = []
        self.send(gate, A, close=lambda: closed.append(1))
        self.acct["A"].body = fake.windows(95, 20, now_ms=self.clock.wall_ms())
        self.assertEqual(gate.monitor().reason, "threshold_five_hour")
        self.assertEqual(closed, [1])


class RecoveryTests(Base):
    def paused(self):
        gate = self.controller()
        self.assertTrue(gate.startup(self.native.read_bearer(self.clock)).ok)
        self.acct["A"].body = fake.windows(95, 20, now_ms=self.clock.wall_ms())
        self.assertFalse(gate.monitor().ok)
        self.assertEqual(gate.state, "paused")
        return gate

    def test_recovery_never_auto_resumes(self):
        stale = None
        gate = self.controller()
        self.assertTrue(gate.startup(self.native.read_bearer(self.clock)).ok)
        stale = gate.issue_continuation()  # issued before the pause
        self.acct["A"].body = fake.windows(95, 20, now_ms=self.clock.wall_ms())
        gate.monitor()
        self.acct["A"].body = fake.windows(1, 1, now_ms=self.clock.wall_ms())  # reset happened
        self.clock.advance(70)
        self.assertFalse(gate.monitor().ok)  # fresh passing poll while paused
        self.assertEqual(gate.state, "paused")
        self.assertFalse(self.send(gate, A).ok)  # old queued request
        self.assertEqual(gate.continue_manually(stale, self.native.read_bearer(self.clock)).reason,
                         "continuation_rejected")
        forged = admission.TrustedContinuation(self.clock.monotonic())
        self.assertEqual(gate.continue_manually(forged, self.native.read_bearer(self.clock)).reason,
                         "continuation_rejected")
        token = gate.issue_continuation()
        self.assertTrue(gate.continue_manually(token, self.native.read_bearer(self.clock)).ok)
        self.assertEqual(gate.continue_manually(token, self.native.read_bearer(self.clock)).reason, "not_paused")
        self.assertTrue(self.send(gate, A).ok)

    def test_continuation_with_failing_check_stays_paused(self):
        gate = self.paused()
        token = gate.issue_continuation()
        self.assertEqual(gate.continue_manually(token, self.native.read_bearer(self.clock)).reason,
                         "threshold_five_hour")
        self.assertEqual(gate.state, "paused")
        again = gate.issue_continuation()
        self.acct["A"].mode = "status:403"
        self.acct["A"].body = fake.windows(1, 1, now_ms=self.clock.wall_ms())
        self.assertEqual(gate.continue_manually(again, self.native.read_bearer(self.clock)).reason, "quota_403")
        self.assertEqual(token.used and again.used, True)
        self.assertFalse(self.send(gate, A).ok)
        self.assertEqual(self.upstream, [])


class GateSocketTests(Base):
    """Socket-level check: a refused request opens no upstream connection at all."""

    def post(self, port, token, model=MODEL):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            body = json.dumps({"model": model, "max_tokens": 1, "messages": []})
            connection.request("POST", "/v1/messages", body, {"Authorization": "Bearer " + token,
                                                              "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_rotation_and_switch_through_a_real_gate(self):
        controller = self.controller()
        self.assertTrue(controller.startup(self.native.read_bearer(self.clock)).ok)
        with gate_server.FakeUpstream(LABELS) as upstream, gate_server.GateServer(controller,
                                                                                 upstream.address) as gate:
            self.assertEqual(self.post(gate.port, A)[0], 200)
            self.native.native_login(A2, IDENT_A)
            self.assertEqual(self.post(gate.port, A2)[0], 200)
            self.assertEqual(self.fetches(), ["A", "A2"])
            self.native.native_login(B, IDENT_B)
            status, payload = self.post(gate.port, B)
            self.assertEqual((status, payload["error"]["message"]), (403, "identity_mismatch"))
            self.assertEqual(self.post(gate.port, A2)[0], 403)
            self.assertEqual(upstream.received, ["A", "A2"])
            self.assertEqual(upstream.connections, 2)  # B and later requests: zero upstream bytes


if __name__ == "__main__":
    unittest.main()
