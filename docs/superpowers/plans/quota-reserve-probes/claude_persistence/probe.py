"""PA2 Linux-only offline persistence matrix; no real credentials or login.

Usage: python3 -I -B probe.py /absolute/path/to/claude
Retains private traces/inventory beneath this worktree's ignored .cache.
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


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def write(path, value):
    path.write_text(json.dumps(value) + "\n")
    path.chmod(0o600)


def main():
    if sys.platform != "linux" or not Path("/usr/bin/strace").is_file():
        raise SystemExit("requires Linux and strace; no uncontained fallback")
    here = Path(__file__).resolve().parent
    source = here.parent / "claude_auth_probe.py"
    spec = importlib.util.spec_from_file_location("pa0", source)
    pa0 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pa0)
    original_command = pa0.command

    def file_traced_command(binary, trace, args):
        command = original_command(binary, trace, args)
        # Extend observation only; retain every original network injection.
        index = command.index("trace=" + ",".join(pa0.DENIED))
        command[index] += ",%file"
        return command

    pa0.command = file_traced_command
    binary = Path(sys.argv[1]).resolve(strict=True)
    os.umask(0o077)
    cache = here.parents[4] / ".cache" / "pa2"
    cache.mkdir(parents=True, exist_ok=True)
    # Native probes these hosted credential paths even with a private HOME.
    # Refuse before execution if either exists; never inspect its contents.
    for name in (".oauth_token", ".api_key"):
        hosted = Path("/home/claude/.claude/remote") / name
        try:
            hosted.lstat()
        except FileNotFoundError:
            continue
        raise SystemExit("hosted credential path exists; no isolated probe")
    root = Path(tempfile.mkdtemp(prefix="kit-pa2-persistence-", dir="/tmp"))
    print(f"private_probe_root={root}", flush=True)

    def retain():
        inventory = []
        for path in sorted(root.rglob("*")):
            stat = path.lstat()
            inventory.append({"path": str(path.relative_to(root)), "mode": oct(stat.st_mode & 0o777),
                              "size": stat.st_size, "symlink": path.is_symlink()})
        write(root / "inventory.json", inventory)
        shutil.copytree(root, cache / root.name)

    atexit.register(retain)
    results = []
    legacy = {"fixture-a": {"privateKeyPkcs8B64": "invalid", "rowPk": "fixture-row"}}
    for case in ("absent-device", "shared-device", "legacy-device"):
        case_root = root / case
        case_root.mkdir(mode=0o700)
        for name in ("home", "a", "b", "cache", "state", "data", "tmp", "workspace", "anthropic", "xdg"):
            (case_root / name).mkdir(mode=0o700)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(case_root / "home"),
               "ANTHROPIC_CONFIG_DIR": str(case_root / "anthropic"),
               "XDG_CONFIG_HOME": str(case_root / "xdg"),
               "XDG_CACHE_HOME": str(case_root / "cache"),
               "XDG_STATE_HOME": str(case_root / "state"),
               "XDG_DATA_HOME": str(case_root / "data"),
               "TMPDIR": str(case_root / "tmp"), "USER": "pa2-fixture", "LOGNAME": "pa2-fixture",
               "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
               "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        for account in ("a", "b"):
            config = case_root / account
            record = {"claudeAiOauth": {
                "accessToken": f"PA2-FABRICATED-ACCESS-{account}",
                "refreshToken": f"PA2-FABRICATED-REFRESH-{account}",
                "expiresAt": 4102444800000,
                "scopes": ["user:profile", "user:inference"],
                "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}}
            if case == "legacy-device" and account == "a":
                record["coworkRemoteDevice"] = legacy
            write(config / ".credentials.json", record)
            write(config / ".claude.json", {"hasCompletedOnboarding": True, "oauthAccount": {
                "accountUuid": f"fixture-{account}", "organizationUuid": f"fixture-org-{account}",
                "emailAddress": f"{account}@example.invalid", "organizationName": f"Fixture {account}"}})
        device = case_root / "home" / ".claude" / ".device-keys.json"
        if case != "absent-device":
            device.parent.mkdir(mode=0o700)
            write(device, {"keys": legacy})
        device_before = digest(device)
        credential_before = {a: digest(case_root / a / ".credentials.json") for a in ("a", "b")}
        rows = []
        for name, account, operation in (("a-first", "a", "status"), ("b-first", "b", "status"),
                                         ("a-return", "a", "status"), ("a-logout", "a", "logout"),
                                         ("b-after", "b", "status")):
            env["CLAUDE_CONFIG_DIR"] = str(case_root / account)
            output, events, denied = pa0.run(binary, case_root, env, name, ["auth", operation])
            assert not events
            trace = (case_root / (name + ".trace")).read_text()
            device_calls = [line for line in trace.splitlines() if ".device-keys" in line]
            other = "b" if account == "a" else "a"
            other_calls = [line for line in trace.splitlines()
                           if str(case_root / other) + "/" in line
                           or '"' + str(case_root / other) + '"' in line]
            assert not other_calls, (case, name, "cross-account path access")
            assert digest(device) == device_before, (case, name, "device content changed")
            assert not device_calls, (case, name, "device path accessed")
            assert digest(case_root / "b" / ".credentials.json") == credential_before["b"]
            row = {"name": name, "account": account, "operation": operation,
                   "exit_status": 0, "denied_network_calls": denied,
                   "device_path_calls": len(device_calls), "other_account_path_calls": len(other_calls)}
            if operation == "status":
                status = json.loads(output)
                assert status["loggedIn"] is True
                assert status["authMethod"] == "claude.ai"
                assert status["configDirectory"] == str(case_root / account)
                assert status["email"] == f"{account}@example.invalid"
                assert digest(case_root / account / ".credentials.json") == credential_before[account]
                row.update({"loggedIn": status["loggedIn"], "authMethod": status["authMethod"],
                            "email": status["email"], "credentials_unchanged": True})
            rows.append(row)
        remaining = case_root / "a" / ".credentials.json"
        after = json.loads(remaining.read_text()) if remaining.exists() else {}
        assert "claudeAiOauth" not in after
        assert after.get("coworkRemoteDevice") == (legacy if case == "legacy-device" else None)
        results.append({"case": case, "operations": rows, "a_oauth_removed": True,
                        "legacy_device_preserved": case == "legacy-device",
                        "shared_device_unchanged": True,
                        "shared_device_exists": device.exists()})
    version, _, denied = pa0.run(binary, case_root, env, "version", ["--version"])
    result = {"version": version.strip(), "binary_sha256": digest(binary),
              "runner_sha256": digest(source), "probe_sha256": digest(Path(__file__)),
              "denied_syscalls": list(pa0.DENIED), "version_denials": denied,
              "classification": "local_fabricated_persistence_not_server_authentication",
              "login_invocations": 0, "model_invocations": 0, "cases": results}
    write(root / "observed.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
