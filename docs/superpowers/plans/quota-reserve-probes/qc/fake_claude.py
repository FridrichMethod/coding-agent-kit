"""Fake native Claude for N1 harness tests. Fabricated data only; loopback only.

Invoked as: python3 -I -B fake_claude.py WRAPPER_PATH NATIVE_ARGS...
Behavior comes from behavior.json beside the wrapper; records go there too.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

FIXTURE_TOKEN = "QC-FAKE-NATIVE-ECHO-ACCESS-NOT-A-CREDENTIAL"


def main():
    here = Path(sys.argv[1]).resolve().parent
    behavior = json.loads((here / "behavior.json").read_text())
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    if behavior.get("record_env"):
        (here / "env.json").write_text(json.dumps({"keys": sorted(os.environ),
                                                   "config": os.environ.get("CLAUDE_CONFIG_DIR"),
                                                   "args": sys.argv[2:]}))
    if behavior.get("record_fds"):
        (here / "fds.json").write_text(json.dumps(sorted(int(fd) for fd in os.listdir("/proc/self/fd"))))
    if behavior.get("grandchild"):
        child = subprocess.Popen(["/bin/sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        (here / "grandchild.pid").write_text(str(child.pid))
    mode = behavior.get("mode", "ok")
    queries = 0

    def emit(event):
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

    def write_cache():
        path = config / ".claude.json"
        document = json.loads(path.read_text())
        document["cachedUsageUtilization"] = {"fetchedAtMs": int(time.time() * 1000),
                                              "accountUuid": behavior.get("cache_account", "acct-a"),
                                              "utilization": behavior.get("rate_limits")}
        staging = config / ".claude.json.fake-staging"
        staging.write_text(json.dumps(document))
        os.rename(staging, path)

    for line in sys.stdin:
        request = json.loads(line)
        request_id, body = request["request_id"], request["request"]
        if mode == "hang":
            time.sleep(600)
        if mode == "early-exit":
            return 3
        if mode == "flood":
            emit({"type": "system", "pad": "x" * (5 * 1024 * 1024)})
        if body["subtype"] == "initialize":
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": request_id,
                                                            "response": {"commands": []}}})
            continue
        if mode == "model":
            emit({"type": "assistant", "message": {"content": "fabricated"}})
        if mode == "error":
            emit({"type": "control_response", "response": {"subtype": "error", "request_id": request_id,
                                                            "error": "fabricated"}})
            continue
        if mode == "bad-response":
            emit({"type": "control_response", "response": ["fabricated"]})
            continue
        if mode == "bad-reply":
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": request_id,
                                                            "response": "fabricated"}})
            continue
        if mode == "bad-session":
            emit({"type": "control_response", "response": {"subtype": "success", "request_id": request_id,
                                                            "response": {"session": ["fabricated"]}}})
            continue
        if behavior.get("write_server_policy"):
            # Simulates native caching server-managed policy fetched during the run.
            (config / "remote-settings.json").write_text("{}")
        for target in behavior.get("connect", []):
            proxy = os.environ.get("HTTPS_PROXY", "")
            result = "no_proxy"
            if proxy.startswith("http://127.0.0.1:"):
                with socket.create_connection(("127.0.0.1", int(proxy.rsplit(":", 1)[1])), timeout=5) as s:
                    s.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
                    status = s.recv(1024).split(b"\r\n", 1)[0]
                    result = status.decode()
                    if b" 200 " in status:
                        s.sendall(b"fake-tls-hello")
                        result += " echo=" + s.recv(64).decode()
            with (here / "connect.log").open("a") as log:
                log.write(target + " " + result + "\n")
        if behavior.get("switch_identity"):
            path = config / ".claude.json"
            document = json.loads(path.read_text())
            document["oauthAccount"]["accountUuid"] = "acct-b"
            path.write_text(json.dumps(document))
        queries += 1
        skip = behavior.get("cache_first_query_skip") and queries == 1
        if behavior.get("write_cache") and not skip:
            delay = behavior.get("cache_delay", 0)
            if delay:
                threading.Timer(delay, write_cache).start()
            else:
                write_cache()
        if behavior.get("echo_token"):
            emit({"type": "system", "subtype": "fixture", "leak": FIXTURE_TOKEN})
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": request_id, "response": {
            "session": {"total_cost_usd": behavior.get("cost", 0), "total_api_duration_ms": 0, "model_usage": {}},
            "subscription_type": behavior.get("subscription_type", "max"),
            "rate_limits_available": behavior.get("available", True),
            "rate_limits": behavior.get("rate_limits"), "behaviors": [], "leak": FIXTURE_TOKEN}}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
