"""PT setup-token native control probe; fabricated credentials, no network.

Run with python3 -I -B probe.py /absolute/path/to/claude. Research only.
Reuses the reviewed PA0 syscall-denied runner without changing that artifact.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile


def main():
    if sys.platform != "linux" or not Path("/usr/bin/strace").is_file():
        raise SystemExit("requires Linux and strace; no network-enabled fallback")
    source = Path(__file__).resolve().parent.parent / "claude_auth_probe.py"
    spec = importlib.util.spec_from_file_location("pa0", source)
    pa0 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pa0)
    binary = Path(sys.argv[1]).resolve(strict=True)
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="kit-pt-claude-token-"))
    print(f"private_probe_root={root}", flush=True)
    for name in ("home", "config", "cache", "state", "data", "tmp", "workspace", "anthropic"):
        (root / name).mkdir(mode=0o700)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
           "CLAUDE_CONFIG_DIR": str(root / "config"),
           "ANTHROPIC_CONFIG_DIR": str(root / "anthropic"),
           "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
           "XDG_STATE_HOME": str(root / "state"), "XDG_DATA_HOME": str(root / "data"),
           "TMPDIR": str(root / "tmp"), "USER": "pt-fixture", "LOGNAME": "pt-fixture",
           "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
           "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
           "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-PT-FABRICATED-NOT-A-CREDENTIAL"}
    version, _, vdenied = pa0.run(binary, root, env, "version", ["--version"])
    requests = [{"type": "control_request", "request_id": "init",
                 "request": {"subtype": "initialize", "hooks": {}}}]
    requests.extend({"type": "control_request", "request_id": name,
                     "request": {"subtype": "get_usage", "skip_behaviors": skip}}
                    for name, skip in (("quota1", True), ("quota2", True), ("quota3", False)))
    _, events, denied = pa0.run(
        binary, root, env, "usage",
        ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
         "--verbose", "--safe-mode", "--no-session-persistence"], requests)
    replies = {e["response"]["request_id"]: e["response"]["response"] for e in events
               if e.get("type") == "control_response"}
    summaries = []
    for name in ("quota1", "quota2", "quota3"):
        usage = replies[name]
        assert usage["rate_limits_available"] is False, usage
        assert usage["rate_limits"] is None, usage
        assert usage["session"]["total_cost_usd"] == 0, usage
        assert usage["session"]["total_api_duration_ms"] == 0, usage
        assert usage["session"]["model_usage"] == {}, usage
        summaries.append({"request_id": name, "rate_limits_available": False,
                          "rate_limits": None, "total_cost_usd": 0,
                          "total_api_duration_ms": 0, "model_usage": {}})
    assert not any(e.get("type") in {"assistant", "stream_event", "user"} for e in events)
    result = {"version": version.strip(),
              "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "runner_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "requests": summaries, "denied_syscalls": list(pa0.DENIED),
              "injected_denials": {"version": vdenied, "usage": denied},
              "event_types": [e.get("type") for e in events],
              "classification": "native_token_quota_unavailable_not_server_permission_test"}
    (root / "sanitized-result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
