"""QC native-path observation: which requests native Claude emits at startup,
for get_usage and for OAuth refresh, with fabricated full-login records.

Run ONLY through ../pc/network_guard.py (loopback TCP and Unix sockets only):

  python3 -I -B ../pc/network_guard.py python3 -I -B native_observe.py "$(command -v claude)"

Research aid only (PA1F TLS fixture technique); production TLS interception is
not proposed. The proxy terminates TLS for two fixture hosts with a disposable
CA trusted only by the child (NODE_EXTRA_CA_CERTS); every other CONNECT is
refused and recorded by host name. Nothing is forwarded. No user message or
model request is sent; model-path attempts are counted and must be zero.
Header values are never recorded except equality with literal fake bearers.
A tunnel that ends without a complete request is recorded by host, stage,
error class and, once the request line was read, method and path only
(unrecorded_connections). exit_status and model_events are
measured from the child's exit.json and stdout written by the PA1F helper.
"""

import atexit
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

HERE = Path(__file__).resolve().parent
PROBES = HERE.parent
TERMINATED = ("api.anthropic.com", "platform.claude.com")
ACCESS_A = "QC-NATIVE-FABRICATED-ACCESS-A-NOT-A-CREDENTIAL"
ACCESS_A2 = "QC-NATIVE-FABRICATED-ACCESS-A2-NOT-A-CREDENTIAL"
REFRESH_A = "QC-NATIVE-FABRICATED-REFRESH-A-NOT-A-CREDENTIAL"
REFRESH_A2 = "QC-NATIVE-FABRICATED-REFRESH-A2-NOT-A-CREDENTIAL"
ACCOUNT, ORG = "fixture-account-a", "fixture-org-a"
FUTURE_MS, PAST_MS = 4102444800000, 946684800000
LIMIT = 65536
MODEL_EVENT_TYPES = ("user", "assistant", "stream_event")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TLS = load("pa1f_tls", PROBES / "claude_freshness" / "tls_probe.py")
OFFLINE = TLS.OFFLINE


def usage_body():
    now = time.time()

    def iso(offset):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + offset))

    return {"five_hour": {"utilization": 12.5, "resets_at": iso(3600)},
            "seven_day": {"utilization": 35.5, "resets_at": iso(86400)}}


def auth_class(value):
    if value is None:
        return "none"
    for label, token in (("bearer-A", ACCESS_A), ("bearer-A2", ACCESS_A2)):
        if value == "Bearer " + token:
            return label
    return "other-bearer" if value.startswith("Bearer ") else "other-scheme"


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _read_head(stream):
    first = stream.readline(8192).decode("ascii", "replace").split()
    headers, total = {}, 0
    while True:
        line = stream.readline(8192)
        total += len(line)
        if total > 32768:
            raise ValueError("header budget")
        if line in (b"\r\n", b"\n", b""):
            break
        key, _, value = line.decode("latin-1").partition(":")
        headers.setdefault(key.strip().lower(), []).append(value.strip())
    return first, headers


def _read_body(stream, headers):
    if headers.get("transfer-encoding", [""])[0].lower() == "chunked":
        body = b""
        while True:
            size = int(stream.readline(64).split(b";")[0].strip() or b"0", 16)
            if size == 0:
                stream.readline(64)
                return body
            body += stream.read(size)
            stream.readline(8)
            if len(body) > LIMIT:
                raise ValueError("body budget")
    length = int(headers.get("content-length", ["0"])[0] or 0)
    if length > LIMIT:
        raise ValueError("body budget")
    return stream.read(length) if length else b""


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        self.request.settimeout(5)
        host, stage, method, path = None, "connect_line", None, None
        try:
            stream = self.request.makefile("rb")
            first, _ = _read_head(stream)
            target = first[1] if len(first) == 3 and first[0] == "CONNECT" else None
            host = target.rsplit(":", 1)[0] if target else None
            server.log("connect", host=host, port=target.rsplit(":", 1)[1] if target else None)
            if host not in TERMINATED or not target.endswith(":443"):
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            stream.close()
            stage = "tls_handshake"
            with server.context.wrap_socket(self.request, server_side=True) as tls:
                tls.settimeout(5)
                with tls.makefile("rb") as request:
                    stage = "request_head"
                    first, headers = _read_head(request)
                    if not first:
                        raise ConnectionAbortedError("closed before a request line")
                    if len(first) == 3:
                        method, path = first[0], urlsplit(first[1]).path
                    stage = "request_body"
                    body = _read_body(request, headers)
                    stage = "response"
                    status, payload = self.respond(host, first, headers, body)
                    data = json.dumps(payload).encode()
                    tls.sendall((f"HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\n"
                                 f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data)
        except (OSError, ValueError, UnicodeError, ssl.SSLError) as error:
            server.log("connection_error", host=host, stage=stage, error=type(error).__name__,
                       method=method, path=path)

    def respond(self, host, first, headers, body):
        server, case = self.server, self.server.case
        method, target = (first[0], first[1]) if len(first) == 3 else ("?", "?")
        parts = urlsplit(target)
        auth = (headers.get("authorization") or [None])[0]
        entry = {"host": host, "method": method, "path": parts.path,
                 "query_keys": sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)}),
                 "auth": auth_class(auth), "x_api_key_present": "x-api-key" in headers,
                 "anthropic_beta_present": "anthropic-beta" in headers}
        if method == "POST" and parts.path.startswith(("/v1/messages", "/v1/complete")):
            server.model_attempts += 1
            server.log("request", **entry)
            return 403, {"error": {"type": "fixture_refused_model"}}
        if host == "platform.claude.com" and method == "POST" and parts.path == "/v1/oauth/token":
            try:
                fields = json.loads(body or b"{}")
            except ValueError:
                fields = {}
            entry["body_keys"] = sorted(fields) if isinstance(fields, dict) else []
            entry["grant_type"] = fields.get("grant_type") if isinstance(fields, dict) else None
            entry["refresh_token_is_fixture_A"] = isinstance(fields, dict) and fields.get("refresh_token") == REFRESH_A
            server.log("request", **entry)
            if case["refresh"] == "success":
                return 200, {"access_token": ACCESS_A2, "refresh_token": REFRESH_A2, "expires_in": 3600,
                             "scope": "user:inference user:profile user:sessions:claude_code",
                             "account": {"uuid": ACCOUNT, "email_address": "a@example.invalid"},
                             "organization": {"uuid": ORG}}
            return 400, {"error": "invalid_grant", "error_description": "fixture"}
        server.log("request", **entry)
        if host == "api.anthropic.com" and method == "GET" and parts.path == "/api/oauth/usage":
            if entry["auth"] in case["usage_ok_for"]:
                return 200, usage_body()
            return 401, {"error": {"type": "authentication_error", "message": "fixture"}}
        if host == "api.anthropic.com" and method == "GET" and parts.path == "/api/oauth/profile":
            if entry["auth"] in ("bearer-A", "bearer-A2"):
                return 200, {"account": {"uuid": ACCOUNT, "email": "a@example.invalid"},
                             "organization": {"uuid": ORG, "organization_type": "claude_max"}}
            return 401, {"error": {"type": "authentication_error"}}
        return 404, {"error": {"type": "not_found_error"}}


class _GateRecorder(BaseHTTPRequestHandler):
    """Plain-HTTP loopback stand-in for an ANTHROPIC_BASE_URL gate: records, denies."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _record(self):
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        if 0 < length <= LIMIT:
            self.rfile.read(length)
        model = self.command == "POST" and parts.path.startswith(("/v1/messages", "/v1/complete"))
        if model:
            self.server.proxy.model_attempts += 1
        self.server.proxy.log("gate_request", host="ANTHROPIC_BASE_URL", method=self.command, path=parts.path,
                              query_keys=sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)}),
                              auth=auth_class(self.headers.get("Authorization")),
                              x_api_key_present="x-api-key" in self.headers,
                              anthropic_beta_present="anthropic-beta" in self.headers)
        body = b'{"error":{"type":"fixture_gate_denied"}}'
        self.send_response(403 if model else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _record


CASES = [
    {"case": "startup-valid", "expires": FUTURE_MS, "refresh": "success", "usage_ok_for": ["bearer-A"],
     "nonessential_off": False},
    {"case": "startup-valid-nonessential-off", "expires": FUTURE_MS, "refresh": "success",
     "usage_ok_for": ["bearer-A"], "nonessential_off": True},
    {"case": "expired-refresh-success", "expires": PAST_MS, "refresh": "success",
     "usage_ok_for": ["bearer-A2"], "nonessential_off": False},
    {"case": "expired-refresh-invalid-grant", "expires": PAST_MS, "refresh": "invalid_grant",
     "usage_ok_for": ["bearer-A"], "nonessential_off": False},
    {"case": "usage-401-refresh-success", "expires": FUTURE_MS, "refresh": "success",
     "usage_ok_for": ["bearer-A2"], "nonessential_off": False},
    {"case": "startup-valid-base-url-gate", "expires": FUTURE_MS, "refresh": "success",
     "usage_ok_for": ["bearer-A"], "nonessential_off": False, "base_url_gate": True},
]


def file_state(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return {"ino": info.st_ino, "mode": oct(info.st_mode & 0o7777), "nlink": info.st_nlink,
            "regular": path.is_file() and not path.is_symlink()}


def credential_summary(path):
    """Equality with literal fixtures only; never prints any value."""
    document = json.loads(path.read_text())
    oauth = document.get("claudeAiOauth", {})
    return {"access": {ACCESS_A: "A", ACCESS_A2: "A2", "": "empty"}.get(oauth.get("accessToken"), "other"),
            "refresh": {REFRESH_A: "A", REFRESH_A2: "A2", "": "empty"}.get(oauth.get("refreshToken"), "other"),
            "expires_changed": oauth.get("expiresAt") not in (FUTURE_MS, PAST_MS),
            "expires_zero": oauth.get("expiresAt") == 0,
            "keys": sorted(oauth), "top_level_keys": sorted(document)}


def run_case(binary, root, cert, context, spec):
    case = root / spec["case"]
    case.mkdir(mode=0o700)
    for directory in ("home", "config", "anthropic", "cache", "state", "data", "tmp", "workspace", "xdg"):
        (case / directory).mkdir(mode=0o700)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(case / "home"), "CLAUDE_CONFIG_DIR": str(case / "config"),
           "ANTHROPIC_CONFIG_DIR": str(case / "anthropic"), "XDG_CONFIG_HOME": str(case / "xdg"),
           "XDG_CACHE_HOME": str(case / "cache"), "XDG_STATE_HOME": str(case / "state"),
           "XDG_DATA_HOME": str(case / "data"), "TMPDIR": str(case / "tmp"), "USER": "qc-fixture",
           "LOGNAME": "qc-fixture", "TERM": "dumb", "LANG": "C.UTF-8", "NO_COLOR": "1",
           "DISABLE_AUTOUPDATER": "1", "NODE_EXTRA_CA_CERTS": str(cert)}
    if spec["nonessential_off"]:
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    credential = case / "config" / ".credentials.json"
    OFFLINE.write(credential, {"claudeAiOauth": {
        "accessToken": ACCESS_A, "refreshToken": REFRESH_A, "expiresAt": spec["expires"],
        "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"],
        "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}})
    OFFLINE.write(case / "config" / ".claude.json", {"hasCompletedOnboarding": True, "oauthAccount": {
        "accountUuid": ACCOUNT, "organizationUuid": ORG, "emailAddress": "a@example.invalid"}})
    before = file_state(credential)
    config_before = sorted(p.name for p in (case / "config").iterdir())
    events, lock = [], threading.Lock()
    with Proxy(("127.0.0.1", 0), Handler) as proxy:
        proxy.context, proxy.case, proxy.model_attempts = context, spec, 0

        def log(kind, **fields):
            with lock:
                events.append(dict(kind=kind, t=time.monotonic(), **fields))

        proxy.log = log
        env["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.server_address[1]}"
        gate = None
        if spec.get("base_url_gate"):
            gate = ThreadingHTTPServer(("127.0.0.1", 0), _GateRecorder)
            gate.daemon_threads, gate.proxy = True, proxy
            env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{gate.server_address[1]}"
            env["NO_PROXY"] = "127.0.0.1"
            threading.Thread(target=gate.serve_forever, daemon=True).start()
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            replies, timings = TLS.native(binary, case, env)
        finally:
            proxy.shutdown()
            thread.join(timeout=3)
            if gate is not None:
                gate.shutdown()
                gate.server_close()
        model_attempts = proxy.model_attempts
    quota_sent = next(t["sent_monotonic"] for t in timings if t["request_id"] == "quota-0")
    after = file_state(credential)
    requests = []
    for event in events:
        if event["kind"] in ("request", "gate_request"):
            row = {k: v for k, v in event.items() if k not in ("kind", "t")}
            row["phase"] = "startup" if event["t"] < quota_sent else "after_get_usage"
            requests.append(row)
    connects = sorted({e["host"] for e in events if e["kind"] == "connect" and e["host"]})
    refused_hosts = sorted({e["host"] for e in events if e["kind"] == "connect"
                            and e["host"] and e["host"] not in TERMINATED})
    unrecorded = sorted(({"host": e["host"], "stage": e["stage"], "error": e["error"], "method": e["method"],
                          "path": e["path"], "phase": "startup" if e["t"] < quota_sent else "after_get_usage"}
                         for e in events if e["kind"] == "connection_error"),
                        key=lambda r: (r["phase"], r["host"] or "", r["stage"], r["error"]))
    exit_status = json.loads((case / "exit.json").read_text())["exit_status"]
    model_events = unparsed = 0
    for line in (case / "native.stdout").read_bytes().splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            unparsed += 1
            continue
        model_events += isinstance(event, dict) and event.get("type") in MODEL_EVENT_TYPES
    cache = json.loads((case / "config" / ".claude.json").read_text())
    row = {"case": spec["case"], "nonessential_off": spec["nonessential_off"],
           "base_url_gate": bool(spec.get("base_url_gate")),
           "fixture_refresh": spec["refresh"], "initial_expired": spec["expires"] == PAST_MS,
           "requests": requests, "connect_hosts": connects, "refused_connect_hosts": refused_hosts,
           "connection_errors": len(unrecorded), "unrecorded_connections": unrecorded,
           "model_attempts": model_attempts, "model_events": model_events, "stdout_unparsed_lines": unparsed,
           "exit_status": exit_status,
           "credential_before": {"mode": before["mode"], "nlink": before["nlink"]},
           "credential_after": None if after is None else {"mode": after["mode"], "nlink": after["nlink"],
                                                           "regular": after["regular"],
                                                           "inode_replaced": after["ino"] != before["ino"]},
           "credential_contents": credential_summary(credential) if after else None,
           "config_entries_before": config_before,
           "config_entries_after": sorted(p.name for p in (case / "config").iterdir()),
           "oauth_account_keys_after": sorted(cache.get("oauthAccount", {})),
           "oauth_account_ids_unchanged": cache.get("oauthAccount", {}).get("accountUuid") == ACCOUNT
           and cache.get("oauthAccount", {}).get("organizationUuid") == ORG,
           "usage_cache_written": "cachedUsageUtilization" in cache,
           "replies": [{k: r[k] for k in ("subscription_type", "rate_limits_available", "rate_limits")}
                       for r in replies]}
    OFFLINE.write(case / "sanitized.json", row)
    return row


def main():
    load("guard_check", PROBES / "pc" / "guard_check.py")
    binary = Path(sys.argv[1]).resolve(strict=True)
    for name in (".oauth_token", ".api_key"):
        try:
            (Path("/home/claude/.claude/remote") / name).lstat()
        except FileNotFoundError:
            continue
        raise SystemExit("hosted credential path exists; refusing probe")
    os.umask(0o077)
    cache = HERE.parents[4] / ".cache" / "qc"
    cache.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="quota-qc-native-", dir="/tmp"))
    with (cache / "temp-roots.txt").open("a") as roots:
        roots.write(str(root) + "\n")
    print(f"private_probe_root={root}", flush=True)

    def retain():
        OFFLINE.write(root / "inventory.json", [{"path": str(p.relative_to(root)),
                      "mode": oct(p.lstat().st_mode & 0o777), "size": p.lstat().st_size,
                      "symlink": p.is_symlink()} for p in sorted(root.rglob("*"))])
        shutil.copytree(root, cache / root.name, symlinks=True,
                        ignore=lambda d, names: [n for n in names if (Path(d) / n).is_socket()])

    atexit.register(retain)
    cert, key = root / "fixture-ca.pem", root / "fixture-key.pem"
    result = subprocess.run(["/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                             "-keyout", str(key), "-out", str(cert), "-days", "1",
                             "-subj", "/CN=QC disposable fixture",
                             "-addext", "subjectAltName=DNS:api.anthropic.com,DNS:platform.claude.com",
                             "-addext", "basicConstraints=critical,CA:TRUE"],
                            capture_output=True, timeout=10, env={"PATH": "/usr/bin:/bin"},
                            stdin=subprocess.DEVNULL)
    assert result.returncode == 0
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    rows = [run_case(binary, root, cert, context, spec) for spec in CASES]
    for row in rows:
        print(json.dumps({k: row[k] for k in ("case", "model_attempts", "connect_hosts")}), flush=True)
    digest = OFFLINE.digest
    observed = {"binary_sha256": digest(binary), "version": "2.1.322",
                "probe_sha256": digest(Path(__file__)), "tls_probe_sha256": digest(PROBES / "claude_freshness" / "tls_probe.py"),
                "guard_sha256": digest(PROBES / "pc" / "network_guard.py"),
                "runner_sha256": digest(PROBES / "claude_auth_probe.py"),
                "classification": "loopback_fixture_not_real_provider_behavior", "cases": rows}
    text = json.dumps(observed, indent=2).replace(str(root), "<tmp>") + "\n"
    # The feature-evaluation path embeds a public client key; keep only its shape.
    text = re.sub(r"/api/eval/sdk-[A-Za-z0-9]+", "/api/eval/sdk-<redacted>", text)
    (root / "observed-native.json").write_text(text)


if __name__ == "__main__":
    main()
