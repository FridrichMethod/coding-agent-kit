"""Fake-only HTTPS usage fixture. Run ONLY through ../pc/network_guard.py.

The proxy never forwards a request. Its disposable CA is trusted only by the
fabricated native child. No system trust, hosts files or real accounts change.
"""

import atexit
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
FAKE = "PA1F-FABRICATED-ACCESS-NOT-A-CREDENTIAL"
FULL = {"five_hour": {"utilization": 12.5, "resets_at": "2100-01-01T00:00:00Z"},
        "seven_day": {"utilization": 35.5, "resets_at": "2100-01-02T00:00:00Z"}}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PA0 = load("pa0", HERE.parent / "claude_auth_probe.py")
OFFLINE = load("offline", HERE / "probe.py")


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = False


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(3)
        try:
            stream = self.request.makefile("rb")
            first = stream.readline(8192).decode("ascii").split()
            total = 0
            while True:
                line = stream.readline(8192)
                total += len(line)
                if total > 32768:
                    return
                if line in (b"\r\n", b"\n", b""):
                    break
            if first != ["CONNECT", "api.anthropic.com:443", "HTTP/1.1"]:
                self.server.rejected_connects += 1
                if len(first) == 3 and first[:2] == ["CONNECT", "platform.claude.com:443"]:
                    self.server.rejected_refresh_host_connects += 1
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            stream.close()
            with self.server.context.wrap_socket(self.request, server_side=True) as tls:
                tls.settimeout(3)
                with tls.makefile("rb") as request:
                    first = request.readline(8192).decode("ascii").split()
                    headers = {}
                    total = 0
                    while True:
                        line = request.readline(8192)
                        total += len(line)
                        if total > 32768:
                            return
                        if line in (b"\r\n", b"\n", b""):
                            break
                        key, value = line.decode("ascii").split(":", 1)
                        headers[key.lower()] = value.strip()
                    if first != ["GET", "/api/oauth/usage", "HTTP/1.1"]:
                        self.server.rejected_paths += 1
                        if len(first) == 3 and first[0] == "POST" and first[1].split("?")[0] in (
                                "/v1/messages", "/v1/complete"):
                            self.server.model_attempts += 1
                        status, payload = 403, {"error": {"type": "fixture_refused"}}
                    else:
                        self.server.requests.append({"method": "GET", "path": "/api/oauth/usage",
                                                     "fake_bearer_matches": headers.get("authorization")
                                                     == "Bearer " + FAKE,
                                                     "received_monotonic": time.monotonic()})
                        status, payload = self.server.reply
                    body = json.dumps(payload).encode()
                    tls.sendall((f"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\n"
                                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
                                + body)
        except (OSError, ValueError, UnicodeError):
            self.server.connection_errors += 1


def native(binary, root, env):
    requests = [{"type": "control_request", "request_id": "init",
                 "request": {"subtype": "initialize", "hooks": {}}}]
    requests += [{"type": "control_request", "request_id": f"quota-{index}",
                  "request": {"subtype": "get_usage", "skip_behaviors": True}}
                 for index in range(2)]
    events, raw, pending, buffer = [], bytearray(), list(requests), bytearray()
    timings = []
    with (root / "native.stderr").open("wb") as err:
        child = subprocess.Popen(
            [str(binary), "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--safe-mode", "--no-session-persistence"],
            env=env, cwd=root / "workspace", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=err, start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 45
        awaiting = None
        next_request_at = 0
        try:
            os.set_blocking(child.stdout.fileno(), False)
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                eof = False
                while not eof:
                    if pending and awaiting is None and time.monotonic() >= next_request_at:
                        message = pending.pop(0)
                        awaiting = message["request_id"]
                        timings.append({"request_id": awaiting, "sent_monotonic": time.monotonic()})
                        snapshot = json.loads((root / "config" / ".claude.json").read_text()).get(
                            "cachedUsageUtilization")
                        timings[-1]["cache_before_send_ms"] = snapshot.get("fetchedAtMs") if snapshot else None
                        child.stdin.write((json.dumps(message) + "\n").encode())
                        child.stdin.flush()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("native fixture exceeded 45 seconds")
                    for key, _ in selector.select(0.1):
                        data = os.read(key.fd, 65536)
                        if not data:
                            eof = True
                            break
                        raw.extend(data)
                        buffer.extend(data)
                        if len(raw) > PA0.LIMIT:
                            raise ValueError("output bound exceeded")
                        while b"\n" in buffer:
                            line, _, rest = buffer.partition(b"\n")
                            buffer = bytearray(rest)
                            if not line:
                                continue
                            event = json.loads(line)
                            events.append(event)
                            reply = event.get("response", {})
                            if event.get("type") == "control_response" and reply.get("request_id") == awaiting:
                                assert reply["subtype"] == "success", reply
                                timings[-1]["received_monotonic"] = time.monotonic()
                                cache = json.loads((root / "config" / ".claude.json").read_text()).get(
                                    "cachedUsageUtilization")
                                timings[-1]["cache_fetched_at_ms"] = cache.get("fetchedAtMs") if cache else None
                                # Observe persistence while native remains alive; no cache mutation.
                                if awaiting == "quota-0":
                                    next_request_at = time.monotonic() + 2
                                awaiting = None
                                if not pending:
                                    child.stdin.close()
                    if (root / "native.stderr").stat().st_size > PA0.LIMIT:
                        raise ValueError("diagnostic bound exceeded")
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
                assert child.returncode == 0 and not pending and awaiting is None
        finally:
            PA0.stop(child)
            child.stdout.close()
            if not child.stdin.closed:
                child.stdin.close()
            (root / "native.stdout").write_bytes(raw)
            OFFLINE.write(root / "exit.json", {"exit_status": child.returncode})
    assert not any(e.get("type") in {"user", "assistant", "stream_event"} for e in events)
    replies = [e["response"]["response"] for e in events if e.get("type") == "control_response"
               and e["response"]["request_id"].startswith("quota-")]
    for reply in replies:
        assert reply["session"]["total_cost_usd"] == 0
        assert reply["session"]["total_api_duration_ms"] == 0
        assert reply["session"]["model_usage"] == {}
    for event in events:
        if event.get("type") == "result":
            assert event.get("total_cost_usd", 0) == 0 and event.get("modelUsage", {}) == {}
    return replies, timings


def main():
    # Prove this invocation and its exec descendants are contained before native starts.
    load("guard_check", HERE.parent / "pc" / "guard_check.py")
    binary = Path(sys.argv[1]).resolve(strict=True)
    for name in (".oauth_token", ".api_key"):
        try:
            (Path("/home/claude/.claude/remote") / name).lstat()
        except FileNotFoundError:
            continue
        raise SystemExit("hosted credential path exists; refusing probe")
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix="kit-pa1f-tls-", dir="/tmp"))
    cache = HERE.parents[4] / ".cache" / "pa1f"
    cache.mkdir(parents=True, exist_ok=True)
    print(f"private_probe_root={root}", flush=True)

    def retain():
        OFFLINE.write(root / "root.json", {"temporary_root": str(root)})
        OFFLINE.write(root / "inventory.json", [{"path": str(p.relative_to(root)),
                      "mode": oct(p.lstat().st_mode & 0o777), "size": p.lstat().st_size,
                      "symlink": p.is_symlink()} for p in sorted(root.rglob("*"))])
        shutil.copytree(root, cache / root.name)

    atexit.register(retain)
    cert, key = root / "fixture-ca.pem", root / "fixture-key.pem"
    result = subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                             "-keyout", str(key), "-out", str(cert), "-days", "1",
                             "-subj", "/CN=PA1F disposable fixture",
                             "-addext", "subjectAltName=DNS:api.anthropic.com",
                             "-addext", "basicConstraints=critical,CA:TRUE"],
                            capture_output=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
    assert result.returncode == 0
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    cases = [("fresh", 200, FULL, False), ("unchanged-new-fetch", 200, FULL, True),
             ("missing-weekly", 200, {"five_hour": FULL["five_hour"]}, True),
             ("forbidden-seed", 403, {"error": {"type": "permission_error"}}, True),
             ("unauthorized-seed", 401, {"error": {"type": "authentication_error"}}, True),
             ("rate-limited-seed", 429, {"error": {"type": "rate_limit_error"}}, True),
             ("empty-seed", 200, {}, True), ("expired-access", 200, FULL, False)]
    rows = []
    for name, status, payload, seeded in cases:
        case = root / name
        case.mkdir(mode=0o700)
        for directory in ("home", "config", "anthropic", "cache", "state", "data", "tmp", "workspace", "xdg"):
            (case / directory).mkdir(mode=0o700)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(case / "home"),
               "CLAUDE_CONFIG_DIR": str(case / "config"), "ANTHROPIC_CONFIG_DIR": str(case / "anthropic"),
               "XDG_CONFIG_HOME": str(case / "xdg"), "XDG_CACHE_HOME": str(case / "cache"),
               "XDG_STATE_HOME": str(case / "state"), "XDG_DATA_HOME": str(case / "data"),
               "TMPDIR": str(case / "tmp"), "USER": "pa1f-fixture", "LOGNAME": "pa1f-fixture",
               "TERM": "dumb", "LANG": "C.UTF-8", "NO_COLOR": "1", "DISABLE_AUTOUPDATER": "1",
               "NODE_EXTRA_CA_CERTS": str(cert)}
        credential = case / "config" / ".credentials.json"
        OFFLINE.write(credential, {"claudeAiOauth": {"accessToken": FAKE,
                      "refreshToken": "PA1F-FABRICATED-REFRESH-NOT-A-CREDENTIAL",
                      "expiresAt": 946684800000 if name == "expired-access" else 4102444800000,
                      "scopes": ["user:inference", "user:profile"],
                      "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}})
        before = OFFLINE.digest(credential)
        config = case / "config" / ".claude.json"
        document = {"hasCompletedOnboarding": True, "oauthAccount": {
            "accountUuid": "fixture-a", "organizationUuid": "fixture-org-a", "emailAddress": "a@example.invalid"}}
        seed_time = int(time.time() * 1000) - 120000 if seeded else None
        if seeded:
            document["cachedUsageUtilization"] = {"fetchedAtMs": seed_time,
                                                  "accountUuid": "fixture-a", "utilization": FULL}
        OFFLINE.write(config, document)
        with Proxy(("127.0.0.1", 0), Handler) as proxy:
            proxy.context, proxy.reply, proxy.requests = context, (status, payload), []
            proxy.rejected_connects = proxy.rejected_paths = proxy.connection_errors = 0
            proxy.rejected_refresh_host_connects = proxy.model_attempts = 0
            env["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.server_address[1]}"
            thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            thread.start()
            try:
                replies, timings = native(binary, case, env)
            finally:
                proxy.shutdown()
                thread.join(timeout=3)
            assert not thread.is_alive()
            after = json.loads(config.read_text()).get("cachedUsageUtilization")
            assert OFFLINE.digest(credential) == before
            assert proxy.requests and all(r["fake_bearer_matches"] for r in proxy.requests)
            assert proxy.model_attempts == 0
            row = {"case": name, "fixture_status": status, "usage_requests": proxy.requests,
                   "rejected_connects": proxy.rejected_connects, "rejected_paths": proxy.rejected_paths,
                   "rejected_refresh_host_connects": proxy.rejected_refresh_host_connects,
                   "model_attempts": proxy.model_attempts,
                   "connection_errors": proxy.connection_errors,
                   "cache_timestamp_advanced": after is not None and
                   (seed_time is None or after["fetchedAtMs"] > seed_time),
                   "cache_utilization": after["utilization"] if after else None,
                   "credentials_unchanged": True, "model_events": 0, "exit_status": 0,
                   "control_timings": timings, "reply_keys": sorted(replies[0]),
                   "replies": [{key: reply[key] for key in
                                ("subscription_type", "rate_limits_available", "rate_limits")}
                               for reply in replies]}
            rows.append(row)
            OFFLINE.write(case / "sanitized.json", row)
            print(json.dumps(row), flush=True)
    OFFLINE.write(root / "observed.json", {"binary_sha256": OFFLINE.digest(binary),
                  "probe_sha256": OFFLINE.digest(Path(__file__)), "offline_probe_sha256": OFFLINE.digest(HERE / "probe.py"),
                  "guard_sha256": OFFLINE.digest(HERE.parent / "pc" / "network_guard.py"),
                  "runner_sha256": OFFLINE.digest(HERE.parent / "claude_auth_probe.py"),
                  "classification": "loopback_fixture_not_real_provider_authorization", "cases": rows})


if __name__ == "__main__":
    main()
