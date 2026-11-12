"""PA1F: full-login-shaped records and cached usage, with all network denied.

Research only. Usage: python3 -I -B probe.py /absolute/path/to/claude
Native credentials are fabricated; no login or model input is submitted.
"""

import atexit
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def main():
    if sys.platform != "linux" or not Path("/usr/bin/strace").is_file():
        raise SystemExit("requires Linux and strace; no uncontained fallback")
    here = Path(__file__).resolve().parent
    source = here.parent / "claude_auth_probe.py"
    spec = importlib.util.spec_from_file_location("pa0", source)
    pa0 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pa0)
    binary = Path(sys.argv[1]).resolve(strict=True)
    for name in (".oauth_token", ".api_key"):
        try:
            (Path("/home/claude/.claude/remote") / name).lstat()
        except FileNotFoundError:
            continue
        raise SystemExit("hosted credential path exists; refusing probe")
    os.umask(0o077)
    cache = here.parents[4] / ".cache" / "pa1f"
    cache.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="kit-pa1f-freshness-", dir="/tmp"))
    print(f"private_probe_root={root}", flush=True)

    def retain():
        inventory = []
        for path in sorted(root.rglob("*")):
            stat = path.lstat()
            inventory.append({"path": str(path.relative_to(root)),
                              "mode": oct(stat.st_mode & 0o777),
                              "size": stat.st_size, "symlink": path.is_symlink()})
        write(root / "inventory.json", inventory)
        write(root / "root.json", {"temporary_root": str(root)})
        shutil.copytree(root, cache / root.name)

    atexit.register(retain)
    rows = []
    cases = [("recent", 0), ("seed-two-minutes", 120000),
             ("seed-fifty-nine-minutes", 3540000), ("older-than-hour", 3660000),
             ("future", -600000), ("wrong-account", 0),
             ("same-account-other-org", 0), ("missing-weekly", 0),
             ("past-reset", 120000), ("no-cache", None),
             ("no-profile-scope", 0)]
    for name, age_ms in cases:
        case_root = root / name
        case_root.mkdir(mode=0o700)
        for directory in ("home", "config", "anthropic", "cache", "state",
                          "data", "tmp", "workspace", "xdg"):
            (case_root / directory).mkdir(mode=0o700)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(case_root / "home"),
               "CLAUDE_CONFIG_DIR": str(case_root / "config"),
               "ANTHROPIC_CONFIG_DIR": str(case_root / "anthropic"),
               "XDG_CONFIG_HOME": str(case_root / "xdg"),
               "XDG_CACHE_HOME": str(case_root / "cache"),
               "XDG_STATE_HOME": str(case_root / "state"),
               "XDG_DATA_HOME": str(case_root / "data"),
               "TMPDIR": str(case_root / "tmp"), "USER": "pa1f-fixture",
               "LOGNAME": "pa1f-fixture", "LANG": "C.UTF-8", "TERM": "dumb",
               "NO_COLOR": "1", "DISABLE_AUTOUPDATER": "1"}
        credential = case_root / "config" / ".credentials.json"
        scopes = ["user:inference"]
        if name != "no-profile-scope":
            scopes.append("user:profile")
        write(credential, {"claudeAiOauth": {
            "accessToken": "PA1F-FABRICATED-ACCESS-NOT-A-CREDENTIAL",
            "refreshToken": "PA1F-FABRICATED-REFRESH-NOT-A-CREDENTIAL",
            "expiresAt": 4102444800000, "scopes": scopes,
            "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}})
        credentials_before = digest(credential)
        now = int(time.time() * 1000)
        windows = {"five_hour": {"utilization": 12.5, "resets_at": "2100-01-01T00:00:00Z"},
                   "seven_day": {"utilization": 35.5, "resets_at": "2100-01-02T00:00:00Z"}}
        if name == "missing-weekly":
            del windows["seven_day"]
        if name == "past-reset":
            for window in windows.values():
                window["resets_at"] = "2000-01-01T00:00:00Z"
        document = {"hasCompletedOnboarding": True, "oauthAccount": {
            "accountUuid": "fixture-a", "organizationUuid": "fixture-org-b"
            if name == "same-account-other-org" else "fixture-org-a",
            "emailAddress": "a@example.invalid"}}
        seed = None if age_ms is None else {
            "accountUuid": "fixture-b" if name == "wrong-account" else "fixture-a",
            "fetchedAtMs": now - age_ms, "utilization": windows}
        if seed is not None:
            document["cachedUsageUtilization"] = seed
        config = case_root / "config" / ".claude.json"
        write(config, document)
        requests = [{"type": "control_request", "request_id": "init",
                     "request": {"subtype": "initialize", "hooks": {}}}]
        requests += [{"type": "control_request", "request_id": f"quota-{index}",
                      "request": {"subtype": "get_usage", "skip_behaviors": True}}
                     for index in range(2)]
        started = time.monotonic()
        try:
            _, events, denied = pa0.run(
                binary, case_root, env, "usage",
                ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                 "--verbose", "--safe-mode", "--no-session-persistence"], requests)
        except BaseException:
            write(case_root / "exit.json", {"completed": False})
            raise
        elapsed = time.monotonic() - started
        write(case_root / "exit.json", {"completed": True, "exit_status": 0,
                                       "elapsed_seconds": elapsed})
        replies = {e["response"]["request_id"]: e["response"]["response"] for e in events
                   if e.get("type") == "control_response"}
        usage = [replies[f"quota-{index}"] for index in range(2)]
        assert not any(e.get("type") in {"assistant", "stream_event", "user"} for e in events)
        for item in usage:
            assert item["session"]["total_cost_usd"] == 0
            assert item["session"]["total_api_duration_ms"] == 0
            assert item["session"]["model_usage"] == {}
        for event in events:
            if event.get("type") == "result":
                assert event.get("total_cost_usd", 0) == 0
                assert event.get("modelUsage", {}) == {}
        assert digest(credential) == credentials_before
        after = json.loads(config.read_text()).get("cachedUsageUtilization")
        row = {"case": name, "input_age_ms": age_ms,
               "elapsed_seconds": round(elapsed, 3), "exit_status": 0,
               "denied_network_calls": denied, "model_events": 0,
               "credentials_unchanged": True, "cache_unchanged": after == seed,
               "cache_removed": seed is not None and after is None,
               "cache_timestamp_unchanged": after is not None and seed is not None
               and after.get("fetchedAtMs") == seed["fetchedAtMs"],
               "age_at_completion_ms": None if seed is None else
               int(time.time() * 1000) - seed["fetchedAtMs"],
               "replies": [{key: item[key] for key in
                            ("subscription_type", "rate_limits_available", "rate_limits")}
                           for item in usage],
               "reply_keys": sorted(usage[0])}
        rows.append(row)
        write(case_root / "sanitized.json", row)
        print(json.dumps(row), flush=True)
    version, _, denied = pa0.run(binary, case_root, env, "version", ["--version"])
    result = {"version": version.strip(), "binary_sha256": digest(binary),
              "runner_sha256": digest(source), "probe_sha256": digest(Path(__file__)),
              "denied_syscalls": list(pa0.DENIED), "version_denials": denied,
              "classification": "offline_cache_counterexamples_not_fresh_server_quota",
              "login_invocations": 0, "model_inputs": 0, "cases": rows}
    write(root / "observed.json", result)
    print("completed_cases=" + str(len(rows)), flush=True)


if __name__ == "__main__":
    main()
