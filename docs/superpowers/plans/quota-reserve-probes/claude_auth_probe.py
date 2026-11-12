"""PA0 Linux-only, fabricated-token native probe. Not runtime code or a CI test.

Usage: python3 -I -B claude_auth_probe.py /absolute/path/to/claude
All child network creation/transmission syscalls are fault-injected by strace.
The private root is retained and printed for inspection and canonical cleanup.
"""

import hashlib
import json
import os
from pathlib import Path
import selectors
import re
import signal
import subprocess
import sys
import tempfile
import time

DENIED = ("socket", "connect", "sendto", "sendmsg", "sendmmsg", "io_uring_setup")
LIMIT = 4 * 1024 * 1024


def command(binary, trace, args):
    return ["/usr/bin/strace", "-f", "-e", "trace=" + ",".join(DENIED),
            *[arg for call in DENIED for arg in ("-e", f"inject={call}:error=ENETUNREACH")],
            "-o", str(trace), str(binary), *args]


def stop(child):
    # The tracer and every tracee start in our new group. Kill that group even
    # when its leader has exited; a grandchild may still hold the output pipe.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            continue
        # Do not omit SIGKILL for remaining group members after leader exit.
    child.wait(timeout=2)


def run(binary, root, env, name, args, requests=()):
    events = []
    trace = root / (name + ".trace")
    stderr = root / (name + ".stderr")
    raw = bytearray()
    with stderr.open("wb") as err:
        child = subprocess.Popen(command(binary, trace, args), env=env,
                                 cwd=root / "workspace", stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=err,
                                 start_new_session=True, close_fds=True)
        try:
            os.set_blocking(child.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                deadline = time.monotonic() + 45
                pending = list(requests)
                awaiting = None
                buffer = bytearray()
                eof = False
                if not pending:
                    child.stdin.close()
                while not eof:
                    if pending and awaiting is None:
                        request = pending.pop(0)
                        awaiting = request["request_id"]
                        child.stdin.write((json.dumps(request) + "\n").encode())
                        child.stdin.flush()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"{name}: 45 second total deadline")
                    for key, _ in selector.select(min(remaining, 0.2)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            eof = True
                            break
                        raw.extend(chunk)
                        buffer.extend(chunk)
                        if len(raw) > LIMIT:
                            raise ValueError("native output exceeded bounded capture")
                        if requests:
                            while b"\n" in buffer:
                                line, _, rest = buffer.partition(b"\n")
                                buffer = bytearray(rest)
                                if not line:
                                    continue
                                event = json.loads(line)
                                events.append(event)
                                response = event.get("response", {})
                                if (event.get("type") == "control_response"
                                        and response.get("request_id") == awaiting):
                                    if response.get("subtype") != "success":
                                        raise ValueError("native control request failed")
                                    awaiting = None
                                    if not pending:
                                        child.stdin.close()
                    if stderr.stat().st_size > LIMIT or trace.stat().st_size > LIMIT:
                        raise ValueError("native diagnostic output exceeded capture limit")
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
                if child.returncode:
                    raise ValueError(f"{name}: native exit {child.returncode}")
                if awaiting is not None or pending:
                    raise ValueError("missing control responses")
        finally:
            stop(child)
            child.stdout.close()
            if not child.stdin.closed:
                child.stdin.close()
            (root / (name + ".stdout")).write_bytes(raw)
    lines = trace.read_text().splitlines()
    # socket is denied too: no new socket can be transmitted through write(),
    # and close_fds prevents inherited network descriptors. io_uring is denied.
    calls = "|".join(DENIED)
    completed = [line for line in lines
                 if re.search(rf"\b(?:{calls})\(|<\.\.\. (?:{calls}) resumed>", line)
                 and "<unfinished ...>" not in line]
    if any("= -1 ENETUNREACH" not in line or "(INJECTED)" not in line
           for line in completed):
        raise ValueError("network syscall escaped expected fault injection")
    attempts = completed
    return raw.decode(), events, len(attempts)


def main():
    if sys.platform != "linux" or not Path("/usr/bin/strace").is_file():
        raise SystemExit("requires Linux and /usr/bin/strace; no uncontained fallback")
    binary = Path(sys.argv[1]).resolve(strict=True)
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="kit-pa0-claude-"))
    print(f"private_probe_root={root}", flush=True)
    for name in ("home", "config", "cache", "state", "data", "tmp", "workspace", "anthropic"):
        (root / name).mkdir(mode=0o700)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"),
           "CLAUDE_CONFIG_DIR": str(root / "config"),
           "ANTHROPIC_CONFIG_DIR": str(root / "anthropic"),
           "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
           "XDG_STATE_HOME": str(root / "state"), "XDG_DATA_HOME": str(root / "data"),
           "TMPDIR": str(root / "tmp"), "USER": "pa0-fixture", "LOGNAME": "pa0-fixture",
           "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
           "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    # Literal fake, never taken from the caller's environment or disk.
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "sk-ant-oat01-PA0-FABRICATED-NOT-A-CREDENTIAL"
    version, _, vblocked = run(binary, root, env, "version", ["--version"])
    status, _, sblocked = run(binary, root, env, "status", ["auth", "status"])
    requests = [
        {"type": "control_request", "request_id": "init",
         "request": {"subtype": "initialize", "hooks": {}}},
        {"type": "control_request", "request_id": "quota",
         "request": {"subtype": "get_usage", "skip_behaviors": True}},
    ]
    _, events, blocked = run(binary, root, env, "usage",
                            ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
                             "--verbose", "--safe-mode", "--no-session-persistence"], requests)
    replies = {e["response"]["request_id"]: e["response"]["response"] for e in events
               if e.get("type") == "control_response"}
    usage = replies["quota"]
    assert usage["rate_limits_available"] is False, usage
    assert usage["rate_limits"] is None, usage
    assert usage["session"]["total_cost_usd"] == 0, usage
    assert usage["session"]["total_api_duration_ms"] == 0, usage
    assert usage["session"]["model_usage"] == {}, usage
    assert not any(e.get("type") in {"assistant", "stream_event", "user"} for e in events), events
    for event in events:
        if event.get("type") == "result":
            assert event.get("total_cost_usd", 0) == 0, event
            assert event.get("modelUsage", {}) == {}, event
    result = {"version": version.strip(), "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
              "auth_status": json.loads(status), "usage": usage,
              "event_types": [e.get("type") for e in events],
              "denied_syscalls": list(DENIED),
              "injected_denials": {"version": vblocked, "status": sblocked, "usage": blocked},
              "model_events": 0, "classification": "unsupported_not_zero_usage"}
    sanitized = json.loads(json.dumps(result).replace(str(root), "<PRIVATE_PROBE_ROOT>"))
    (root / "sanitized-result.json").write_text(json.dumps(sanitized, indent=2) + "\n")
    print(json.dumps(sanitized, indent=2))


if __name__ == "__main__":
    main()
