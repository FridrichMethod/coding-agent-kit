"""Check retained native counterexamples; no native execution or network."""

import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def check():
    offline = json.loads((HERE / "observed-offline.json").read_text())
    tls = json.loads((HERE / "observed-tls.json").read_text())
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    assert offline["probe_sha256"] == digest(HERE / "probe.py")
    assert tls["probe_sha256"] == digest(HERE / "tls_probe.py")
    assert tls["offline_probe_sha256"] == offline["probe_sha256"]
    assert tls["guard_sha256"] == digest(HERE.parent / "pc" / "network_guard.py")
    assert tls["runner_sha256"] == offline["runner_sha256"] == digest(HERE.parent / "claude_auth_probe.py")
    assert tls["binary_sha256"] == offline["binary_sha256"]
    assert offline["login_invocations"] == offline["model_inputs"] == 0
    assert offline["denied_syscalls"] == ["socket", "connect", "sendto", "sendmsg", "sendmmsg", "io_uring_setup"]
    a = {row["case"]: row for row in offline["cases"]}
    b = {row["case"]: row for row in tls["cases"]}
    assert len(a) == len(offline["cases"]) == 11
    assert len(b) == len(tls["cases"]) == 8
    keys = ["behaviors", "rate_limits", "rate_limits_available", "session", "subscription_type"]
    for row in [*a.values(), *b.values()]:
        assert row["credentials_unchanged"] and row["model_events"] == row["exit_status"] == 0
        assert row["reply_keys"] == keys and len(row["replies"]) == 2
        assert row["replies"][0] == row["replies"][1]
    full = a["recent"]["replies"][0]
    for name in ("seed-two-minutes", "seed-fifty-nine-minutes", "same-account-other-org"):
        assert a[name]["replies"][0] == full
        assert a[name]["cache_timestamp_unchanged"]
    assert a["seed-fifty-nine-minutes"]["age_at_completion_ms"] > 3540000
    for name in ("future", "older-than-hour", "wrong-account", "no-cache"):
        assert a[name]["replies"][0]["rate_limits"] is None
        assert a[name]["replies"][0]["rate_limits_available"] is True
    assert a["wrong-account"]["cache_removed"]
    assert a["no-profile-scope"]["replies"][0]["rate_limits_available"] is False
    assert a["past-reset"]["replies"][0]["rate_limits"]["five_hour"]["resets_at"].startswith("2000-")
    for row in a.values():
        assert row["denied_network_calls"] > 0 and row["elapsed_seconds"] < 45
    for row in b.values():
        assert row["model_attempts"] == row["connection_errors"] == 0
        assert row["usage_requests"] and all(r["fake_bearer_matches"] for r in row["usage_requests"])
        assert all(r["method"] == "GET" and r["path"] == "/api/oauth/usage" for r in row["usage_requests"])
        timings = row["control_timings"]
        assert [t["request_id"] for t in timings] == ["init", "quota-0", "quota-1"]
        assert timings[2]["sent_monotonic"] - timings[1]["received_monotonic"] >= 2
        assert timings[-1]["received_monotonic"] - timings[0]["sent_monotonic"] < 45
    for name in ("fresh", "unchanged-new-fetch", "missing-weekly", "expired-access"):
        assert b[name]["cache_timestamp_advanced"]
        assert len(b[name]["usage_requests"]) == 1
    for name in ("forbidden-seed", "unauthorized-seed", "rate-limited-seed", "empty-seed"):
        assert not b[name]["cache_timestamp_advanced"]
        assert b[name]["replies"][0] == full
    for name in ("fresh", "unchanged-new-fetch", "expired-access"):
        assert b[name]["replies"][0] == full
    for group in (a, b):
        assert "seven_day" not in group["missing-weekly"]["replies"][0]["rate_limits"]
    assert b["expired-access"]["rejected_refresh_host_connects"] > 0
    print("PA1F: 11 denied-network cases and 8 loopback TLS cases verified")


if __name__ == "__main__":
    check()
