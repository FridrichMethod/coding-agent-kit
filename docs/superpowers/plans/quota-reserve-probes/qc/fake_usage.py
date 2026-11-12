"""Loopback fake of the observed /api/oauth/usage and /api/oauth/profile shapes.

Fabricated accounts only. Bearers are matched by constant-time comparison and
recorded by fixture label, never by value. Nothing is forwarded anywhere.
"""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


def windows(five=12.5, week=35.5, now_ms=None, extra=None):
    """Observed response shape: general rows plus optional model/overage rows."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms

    def iso(ms):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))

    body = {"five_hour": None if five is None else {"utilization": five, "resets_at": iso(now_ms + 3_600_000)},
            "seven_day": None if week is None else {"utilization": week, "resets_at": iso(now_ms + 86_400_000)}}
    body.update(extra or {})
    return body


class FakeAccount:
    def __init__(self, label, account_uuid, organization_uuid, body):
        self.label, self.account_uuid, self.organization_uuid = label, account_uuid, organization_uuid
        self.body, self.mode = body, "normal"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def date_time_string(self, timestamp=None):
        # send_response() adds Date itself; serve the fixture's clock there.
        return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(self.server.fake.server_time()))

    def _send(self, status, payload, content_type="application/json", extra_headers=()):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.server.fake.record(self.command, self.path, None)
        self._send(404, {"error": {"type": "not_found_error"}})

    def do_GET(self):
        fake = self.server.fake
        header = self.headers.get_all("Authorization") or []
        account = fake.lookup(header[0]) if len(header) == 1 else None
        fake.record("GET", self.path, account.label if account else ("none" if not header else "unknown"),
                    beta=self.headers.get("anthropic-beta"))
        if self.path not in ("/api/oauth/usage", "/api/oauth/profile"):
            return self._send(404, {"error": {"type": "not_found_error"}})
        if account is None:
            return self._send(401, {"error": {"type": "authentication_error", "message": "fixture"}})
        if self.path == "/api/oauth/profile":
            return self._send(200, {"account": {"uuid": account.account_uuid, "email": "fixture@example.invalid"},
                                    "organization": {"uuid": account.organization_uuid}})
        mode = account.mode
        if mode.startswith("status:"):
            code = int(mode.split(":", 1)[1])
            kinds = {401: "authentication_error", 403: "permission_error", 429: "rate_limit_error"}
            return self._send(code, {"error": {"type": kinds.get(code, "api_error")}})
        if mode == "redirect":
            return self._send(302, b"", extra_headers=(("Location", "/api/oauth/usage?redirected=1"),))
        if mode == "malformed":
            return self._send(200, b"{not json")
        if mode == "text":
            return self._send(200, json.dumps(account.body).encode(), content_type="text/plain")
        if mode == "oversize":
            return self._send(200, {"five_hour": None, "pad": "x" * 70000})
        if mode == "huge-header":
            return self._send(200, account.body, extra_headers=(("X-Pad", "y" * 70000),))
        if mode == "slow":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "1000")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass
            return
        return self._send(200, account.body)


class FakeUsageServer:
    def __init__(self, server_time=time.time):
        self._tokens = []
        self.requests = []
        self.server_time = server_time
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.fake = self
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)

    def register(self, token, account):
        with self._lock:
            self._tokens.append((token.encode(), account))
        return account

    def revoke(self, token):
        with self._lock:
            self._tokens = [(t, a) for t, a in self._tokens if not hmac.compare_digest(t, token.encode())]

    def lookup(self, header):
        if not header.startswith("Bearer "):
            return None
        presented = header[7:].encode("ascii", "replace")
        found = None
        with self._lock:
            for token, account in self._tokens:
                if hmac.compare_digest(token, presented):
                    found = account
        return found

    def record(self, method, path, label, beta=None):
        with self._lock:
            self.requests.append({"method": method, "path": path, "account": label, "beta": beta})

    def labels(self, path="/api/oauth/usage"):
        with self._lock:
            return [r["account"] for r in self.requests if r["path"] == path]
