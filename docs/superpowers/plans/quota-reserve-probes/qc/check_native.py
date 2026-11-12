"""Acceptance checks for observed-native.json (sanitized native observation).

Usage: python3 -I -B check_native.py [observed-native.json]
"""

import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
BINARY_SHA256 = "bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478"
CASES = ("startup-valid", "startup-valid-nonessential-off", "expired-refresh-success",
         "expired-refresh-invalid-grant", "usage-401-refresh-success", "startup-valid-base-url-gate")


def _paths(row, **match):
    return [r for r in row["requests"] if all(r.get(k) == v for k, v in match.items())]


def check(observed, probe_digest=None):
    errors = []

    def need(condition, message):
        if not condition:
            errors.append(message)

    need(observed.get("binary_sha256") == BINARY_SHA256, "binary digest")
    if probe_digest is not None:
        need(observed.get("probe_sha256") == probe_digest, "probe digest does not match native_observe.py")
    rows = {row["case"]: row for row in observed.get("cases", [])}
    need(tuple(rows) == CASES, "case list")
    if errors:
        return errors
    for name, row in rows.items():
        need(row["model_attempts"] == 0 and row["model_events"] == 0, name + ": model attempt")
        need(row["exit_status"] == 0, name + ": exit")
        need(all(r["method"] != "POST" or not r["path"].startswith("/v1/messages") for r in row["requests"]),
             name + ": messages path")
        need(not row["refused_connect_hosts"], name + ": unexpected CONNECT host")
        for request in row["requests"]:
            if request["path"] == "/v1/oauth/token":
                need(request["host"] == "platform.claude.com" and request["auth"] == "none",
                     name + ": refresh host/auth")
            elif request["host"] != "ANTHROPIC_BASE_URL":
                need(request["host"] == "api.anthropic.com", name + ": non-refresh host")
        after = row["credential_after"]
        need(after is not None and after["mode"] == "0o600" and after["nlink"] == 1 and after["regular"],
             name + ": credential file mode/link")
    valid = rows["startup-valid"]
    need(len(_paths(valid, path="/api/oauth/usage", auth="bearer-A")) == 1, "valid: one usage GET with A")
    need(_paths(valid, path="/api/oauth/profile", auth="bearer-A", phase="startup") != [],
         "valid: startup profile GET")
    need(not valid["credential_after"]["inode_replaced"], "valid: credential rewritten")
    need(all(r["rate_limits"] for r in valid["replies"]), "valid: windows")
    quiet = rows["startup-valid-nonessential-off"]
    need(not _paths(quiet, path="/api/oauth/usage"), "nonessential-off: usage fetched")
    need(all(r["rate_limits"] is None for r in quiet["replies"]), "nonessential-off: windows present")
    refreshed = rows["expired-refresh-success"]
    first = refreshed["requests"][0] if refreshed["requests"] else {}
    need(first.get("path") == "/v1/oauth/token" and first.get("grant_type") == "refresh_token"
         and first.get("phase") == "startup" and first.get("refresh_token_is_fixture_A") is True,
         "expired: refresh first")
    need(first.get("body_keys") == ["client_id", "grant_type", "refresh_token", "scope"], "expired: body keys")
    need(not _paths(refreshed, auth="bearer-A"), "expired: old bearer used after refresh")
    need(len(_paths(refreshed, path="/api/oauth/usage", auth="bearer-A2")) == 1, "expired: usage with A2")
    need(refreshed["credential_after"]["inode_replaced"]
         and refreshed["credential_contents"]["access"] == "A2"
         and refreshed["credential_contents"]["refresh"] == "A2", "expired: atomic rewrite to A2")
    invalid = rows["expired-refresh-invalid-grant"]
    need(_paths(invalid, path="/v1/oauth/token") != [], "invalid-grant: no refresh attempt")
    need(invalid["credential_contents"]["access"] == "empty" and invalid["credential_contents"]["expires_zero"],
         "invalid-grant: native did not clear stored access token")
    need(all(r["rate_limits_available"] is False for r in invalid["replies"]), "invalid-grant: availability")
    rotated = rows["usage-401-refresh-success"]
    order = [(r["path"], r["auth"]) for r in rotated["requests"]
             if r["path"] in ("/api/oauth/usage", "/v1/oauth/token")]
    need(order == [("/api/oauth/usage", "bearer-A"), ("/v1/oauth/token", "none"),
                   ("/api/oauth/usage", "bearer-A2")], "usage-401: rotation order")
    need(rotated["credential_contents"]["access"] == "A2", "usage-401: store not rotated")
    gated = rows["startup-valid-base-url-gate"]
    need(gated["base_url_gate"] and not _paths(gated, host="ANTHROPIC_BASE_URL"),
         "base-url: auxiliary traffic reached the gate")
    need(len(_paths(gated, path="/api/oauth/usage", host="api.anthropic.com")) == 1,
         "base-url: usage did not use BASE_API_URL")
    return errors


def main(argv):
    path = Path(argv[1]) if len(argv) > 1 else HERE / "observed-native.json"
    digest = hashlib.sha256((HERE / "native_observe.py").read_bytes()).hexdigest()
    errors = check(json.loads(path.read_text()), digest)
    for error in errors:
        print("FAIL:", error)
    print("PASS" if not errors else "FAILED (%d)" % len(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
