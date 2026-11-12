"""Check the committed PA2 matrix against its fixture and reviewed runner."""

import hashlib
import json
from pathlib import Path


def main():
    here = Path(__file__).resolve().parent
    observed = json.loads((here / "observed.json").read_text())
    for key, source in (("probe_sha256", here / "probe.py"),
                        ("runner_sha256", here.parent / "claude_auth_probe.py")):
        assert observed[key] == hashlib.sha256(source.read_bytes()).hexdigest(), key
    assert observed["classification"] == "local_fabricated_persistence_not_server_authentication"
    assert observed["login_invocations"] == observed["model_invocations"] == 0
    assert observed["denied_syscalls"] == ["socket", "connect", "sendto", "sendmsg", "sendmmsg", "io_uring_setup"]
    assert [c["case"] for c in observed["cases"]] == ["absent-device", "shared-device", "legacy-device"]
    for case in observed["cases"]:
        assert case["a_oauth_removed"] and case["shared_device_unchanged"]
        assert case["shared_device_exists"] == (case["case"] != "absent-device")
        assert case["legacy_device_preserved"] == (case["case"] == "legacy-device")
        assert [r["name"] for r in case["operations"]] == ["a-first", "b-first", "a-return", "a-logout", "b-after"]
        for row in case["operations"]:
            assert row["exit_status"] == row["device_path_calls"] == row["other_account_path_calls"] == 0
            assert row["denied_network_calls"] > 0
            if row["operation"] == "status":
                assert row["credentials_unchanged"] and row["loggedIn"]
                assert row["authMethod"] == "claude.ai"
                assert row["email"] == row["account"] + "@example.invalid"
    print("PASS: 3 cases, 15 isolated native operations; evidence/fixture hashes match")


if __name__ == "__main__":
    main()
