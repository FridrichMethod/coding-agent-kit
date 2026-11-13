"""Loopback fixtures for protected codex-as: a TLS backend that answers as chatgpt.com and
auth.openai.com, and the launcher's HTTPS_PROXY that tunnels those names to it.

The CA is made with the host's openssl (make_ca returns None without it) and reaches the gate only
through SSL_CERT_FILE, the way a corporate CA would. Every request is recorded with its arrival
time; nothing listens beyond 127.0.0.1 and no credential is real.
"""

import base64
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading
import time


def jwt(claims):
    """An unsigned three-part JWT for fabricated claims."""
    encode = lambda data: base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return "%s.%s.fixture" % (encode({"alg": "none"}), encode(claims))


def credential(account="acct-fixture-A", user="user-fixture-A", plan="pro", access="A1", fedramp=False):
    auth = {"chatgpt_account_id": account, "chatgpt_user_id": user, "chatgpt_plan_type": plan}
    if fedramp:
        auth["chatgpt_account_is_fedramp"] = True
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"id_token": jwt({"https://api.openai.com/auth": auth, "email": "fixture@example.invalid"}),
                       "access_token": jwt({"https://api.openai.com/auth": {"chatgpt_account_id": account},
                                            "fixture": access}),
                       "refresh_token": "fixture-refresh-" + access, "account_id": account},
            "last_refresh": "2026-10-01T00:00:00Z"}


def make_ca(directory):
    """(ca.pem, server context) for chatgpt.com and auth.openai.com, or None without openssl."""
    openssl = shutil.which("openssl")
    if openssl is None:
        return None
    directory = Path(directory)
    (directory / "ca.cnf").write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n[dn]\nCN=fixture-ca\n"
        "[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    (directory / "leaf.ext").write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:chatgpt.com,DNS:auth.openai.com\nsubjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n")
    steps = (
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
         "-config", "ca.cnf"],
        [openssl, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr", "-subj",
         "/CN=chatgpt.com"],
        [openssl, "x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out",
         "leaf.pem", "-days", "2", "-extfile", "leaf.ext"],
    )
    for step in steps:
        if subprocess.run(step, cwd=directory, capture_output=True, timeout=60).returncode:
            return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "leaf.pem", directory / "leaf.key")
    return str(directory / "ca.pem"), context


def window(used, seconds, wall=None):
    wall = time.time() if wall is None else wall
    return {"used_percent": used, "limit_window_seconds": seconds, "reset_after_seconds": 3600,
            "reset_at": int(wall) + 3600}


class Backend:
    """chatgpt.com and auth.openai.com on one TLS listener; a request per connection."""

    def __init__(self, context):
        self.context = context
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.lock, self.requests = threading.Lock(), []
        self.account, self.user, self.plan = "acct-fixture-A", "user-fixture-A", "pro"
        self.used = [10, 20]
        self.usage_status = 200
        self.bundle = None          # a bundle document, or None for 404
        self.refreshes = 0
        self.refresh_status = 200
        self.closing = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.closing = True
        self.listener.close()

    def seen(self, path=None, method=None):
        with self.lock:
            return [r for r in self.requests if (path is None or r["path"].split("?")[0] == path)
                    and (method is None or r["method"] == method)]

    def usage_document(self):
        return {"plan_type": self.plan, "account_id": self.account, "user_id": self.user,
                "rate_limit": {"allowed": True, "limit_reached": False,
                               "primary_window": window(self.used[0], 18000),
                               "secondary_window": window(self.used[1], 604800)}}

    def stream(self, body):
        """A model reply: JSON for the fake codex, a Responses event stream for a streaming request
        (real codex), whose assistant message names the turn's number."""
        try:
            streaming = json.loads(body).get("stream") is True
        except (ValueError, AttributeError):
            streaming = False
        if not streaming:
            return b'{"output":"fixture"}'
        with self.lock:
            number = len([r for r in self.requests if r["path"].split("?")[0] == "/backend-api/codex/responses"])
        item = {"type": "message", "role": "assistant", "id": "msg_fixture_%d" % number,
                "content": [{"type": "output_text", "text": "fixture reply %d" % number}]}
        events = (("response.created", {"type": "response.created", "response": {"id": "resp_fixture_%d" % number}}),
                  ("response.output_item.done", {"type": "response.output_item.done", "item": item}),
                  ("response.completed", {"type": "response.completed", "response": {"id": "resp_fixture_%d" % number}}))
        return b"".join(b"event: %s\ndata: %s\n\n" % (name.encode(), json.dumps(data).encode()) for name, data in events)

    def _accept(self):
        while not self.closing:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        try:
            sock = self.context.wrap_socket(sock, server_side=True)
            sock.settimeout(10)
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = sock.recv(65536)
                if not chunk:
                    return
                raw += chunk
            head, _, body = raw.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = [tuple(part.strip() for part in line.split(":", 1)) for line in lines[1:]]
            length = int(dict((k.lower(), v) for k, v in headers).get("content-length", "0"))
            while len(body) < length:
                chunk = sock.recv(65536)
                if not chunk:
                    return
                body += chunk
            with self.lock:
                self.requests.append({"method": method, "path": target, "headers": headers, "body": body,
                                      "at": time.monotonic()})
            status, payload = self.answer(method, target.split("?")[0], body)
            kind = b"text/event-stream" if payload.startswith(b"event: ") else b"application/json"
            sock.sendall(b"HTTP/1.1 %d X\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n" % (status, kind, len(payload)) + payload)
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def answer(self, method, path, body):
        if path == "/backend-api/wham/usage":
            if self.usage_status != 200:
                return self.usage_status, b'{"error":"fixture"}'
            return 200, json.dumps(self.usage_document()).encode()
        if path == "/backend-api/wham/config/bundle":
            return (404, b"{}") if self.bundle is None else (200, json.dumps(self.bundle).encode())
        if path == "/backend-api/codex/responses" and method == "POST":
            return 200, self.stream(body)
        if path == "/oauth/token" and method == "POST":
            if self.refresh_status != 200:
                return self.refresh_status, b'{"error":"invalid_grant"}'
            self.refreshes += 1
            access = "A1r%d" % self.refreshes
            return 200, json.dumps({"access_token": jwt({"https://api.openai.com/auth": {
                "chatgpt_account_id": self.account}, "fixture": access}),
                "refresh_token": "fixture-refresh-" + access}).encode()
        if path == "/backend-api/wham/accounts/check":  # the TUI needs its workspace listed (PC2)
            return 200, json.dumps({"accounts": [{"id": self.account, "name": "Fixture",
                                                  "workspace_backend_origin": "https://chatgpt.com",
                                                  "account_routing_override": "NO_CONSTRAINT"}]}).encode()
        if path == "/backend-api/codex/models":
            return 200, b'{"models":[]}'
        if path == "/backend-api/wham/settings/user":
            return 200, b"{}"
        return 404, b"{}"


class Proxy:
    """A plain HTTP CONNECT proxy that tunnels the two fixture names to the backend."""

    def __init__(self, backend_port):
        self.backend_port = backend_port
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.port = self.listener.getsockname()[1]
        self.targets = []
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.listener.close()

    def _accept(self):
        while True:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, client):
        upstream = None
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                data = client.recv(1)
                if not data:
                    return
                head += data
            target = head.split(b" ")[1].decode()
            self.targets.append(target)
            if target not in ("chatgpt.com:443", "auth.openai.com:443"):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            upstream = socket.create_connection(("127.0.0.1", self.backend_port), timeout=10)
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

            def pump(source, sink):
                try:
                    while True:
                        data = source.recv(65536)
                        if not data:
                            break
                        sink.sendall(data)
                except OSError:
                    pass
                for sock in (source, sink):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

            thread = threading.Thread(target=pump, args=(upstream, client), daemon=True)
            thread.start()
            pump(client, upstream)
            thread.join(10)
        except OSError:
            pass
        finally:
            client.close()
            if upstream is not None:
                upstream.close()
