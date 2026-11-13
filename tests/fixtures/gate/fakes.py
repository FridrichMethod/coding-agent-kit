"""Fakes for the gate, monitor and supervisor tests: 127.0.0.1 only, fabricated bearers.

FakeUpstream records every byte it receives, per connection and with its arrival time, and answers
GET /usage (identity and used percentages per bearer), POST /model (an endless or bounded event
stream), GET /read and GET /bundle. FakeProvider is a small provider on that upstream: Codex-like
(an optional X-Account selector, the native's own usage read binds) or Claude-like (the gate holds
the bearer and checks itself). FakeEvaluator follows the reserve semantics closely enough for the
gate: pause when remaining <= reserve, resume only after a continuation and a passing observation.
"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time


ROOT = Path(os.path.realpath(__file__)).parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("kit_gate", ROOT / "lib/coding-agent-kit/gate.py")

BEARER_A = "fixture-bearer-A-0001"
BEARER_A2 = "fixture-bearer-A-0002"
BEARER_B = "fixture-bearer-B-0001"
IDENTITY_A = ["acct-A", "user-A"]
IDENTITY_B = ["acct-B", "user-B"]


class Connection:
    def __init__(self):
        self.raw, self.arrivals, self.request, self.closed_at = bytearray(), [], None, None


class FakeUpstream:
    """An HTTP/1.1 upstream on 127.0.0.1, optionally TLS, recording raw request bytes."""

    def __init__(self, tls=None):
        self.tls = tls
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.connections, self.lock = [], threading.Lock()
        self.identities = {BEARER_A: IDENTITY_A, BEARER_A2: IDENTITY_A, BEARER_B: IDENTITY_B}
        self.used = {"five_hour": 10.0, "weekly": 20.0}
        self.usage_status = 200
        self.usage_delay = 0.0
        self.stream_chunks = 3  # a body with "forever": true streams until the client goes away
        self.stream_interval = 0.02
        self.read_body = True
        self.closing = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.closing = True
        self.listener.close()

    def requests(self, path=None):
        with self.lock:
            done = [c.request for c in self.connections if c.request]
        return [r for r in done if path is None or r["path"] == path]

    def total_bytes(self):
        with self.lock:
            return sum(len(c.raw) for c in self.connections)

    def _accept(self):
        while not self.closing:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            connection = Connection()
            with self.lock:
                self.connections.append(connection)
            threading.Thread(target=self._serve, args=(sock, connection), daemon=True).start()

    def _recv(self, sock, connection):
        data = sock.recv(65536)
        if data:
            connection.raw += data
            connection.arrivals.append((time.monotonic(), len(data)))
        return data

    def _serve(self, sock, connection):
        try:
            if self.tls is not None:
                sock = self.tls.wrap_socket(sock, server_side=True)
            sock.settimeout(10)
            while b"\r\n\r\n" not in connection.raw:
                if not self.read_body:
                    time.sleep(0.05)
                    continue
                if not self._recv(sock, connection):
                    return
            head, _, body = bytes(connection.raw).partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = [tuple(part.strip() for part in line.split(":", 1)) for line in lines[1:]]
            length = int(dict((k.lower(), v) for k, v in headers).get("content-length", "0"))
            while len(body) < length:
                data = self._recv(sock, connection)
                if not data:
                    return
                body += data
            connection.request = {"method": method, "path": target, "headers": headers, "body": body}
            self._answer(sock, connection, method, target, headers, body)
        except OSError:
            pass
        finally:
            connection.closed_at = time.monotonic()
            try:
                sock.close()
            except OSError:
                pass

    def _answer(self, sock, connection, method, target, headers, body):
        auth = [v for k, v in headers if k.lower() == "authorization"]
        bearer = auth[0][7:] if auth and auth[0].startswith("Bearer ") else None
        if target.split("?")[0] == "/usage":
            time.sleep(self.usage_delay)
            identity = self.identities.get(bearer)
            if identity is None or self.usage_status != 200:
                return self._send(sock, self.usage_status if identity else 401, b'{"error":"unauthorized"}')
            payload = json.dumps({"identity": identity, "used": self.used}).encode()
            return self._send(sock, 200, payload, chunked=True)
        if target == "/model" and method == "POST":
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
            count = 0
            forever = b'"forever": true' in body
            while forever or count < self.stream_chunks:
                event = b"data: %d\n\n" % count
                sock.sendall(b"%x\r\n%s\r\n" % (len(event), event))
                count += 1
                time.sleep(self.stream_interval)
            sock.sendall(b"0\r\n\r\n")
            return
        if target in ("/read", "/bundle"):
            return self._send(sock, 200, b'{"ok":true,"path":"%s"}' % target.encode())
        return self._send(sock, 404, b"{}")

    def _send(self, sock, status, body, chunked=False):
        if chunked:
            half = len(body) // 2
            parts = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in (body[:half], body[half:]) if p) + b"0\r\n\r\n"
            sock.sendall(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                         % status + parts)
        else:
            sock.sendall(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                         % (status, len(body)) + body)


class EchoServer:
    """A raw TCP echo on 127.0.0.1 standing in for a refresh host behind a CONNECT tunnel."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.received, self.closed = bytearray(), threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(sock,), daemon=True).start()

    def _echo(self, sock):
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                self.received += data
                sock.sendall(data)
        except OSError:
            pass
        finally:
            self.closed.set()
            sock.close()

    def close(self):
        self.listener.close()


class FakeProxy:
    """A plain HTTP CONNECT proxy on 127.0.0.1 that records the tunnels it opened."""

    def __init__(self, refuse=False):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.targets, self.heads, self.refuse = [], [], refuse
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
            self.heads.append(head.decode("latin-1"))
            target = head.split(b" ")[1].decode()
            self.targets.append(target)
            if self.refuse:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            host, port = target.rsplit(":", 1)
            upstream = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
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
            thread.join(5)
        except OSError:
            pass
        finally:
            client.close()
            if upstream is not None:
                upstream.close()


class FakeProvider(gate.BaseProvider):
    """Codex-like (holds_bearer False) or Claude-like (holds_bearer True) fixture provider."""

    def __init__(self, upstream, name="codex", holds_bearer=False, store_bearer=BEARER_A, models=("fixture-model",),
                 connect_allow=None, tls=False, ca_file=None):
        self.name = name
        self.upstream = gate.Upstream("127.0.0.1", upstream, tls=tls, ca_file=ca_file)
        self.routes = gate.RouteTable("fixture-1", (
            gate.Route("POST", "/model", gate.MODEL),
            gate.Route("GET", "/usage", gate.USAGE, query=True),
            gate.Route("GET", "/read", gate.READ),
            gate.Route("GET", "/bundle", gate.PAUSED_READ, limit=4096),
        ))
        self.expected_identity = tuple(IDENTITY_A)
        self.holds_bearer, self.store_bearer, self.models = holds_bearer, store_bearer, set(models)
        self.connect_allow = dict(connect_allow or {})
        self.rebind_timeout = 3.0

    def present(self, route, headers):
        presented = super().present(route, headers)
        accounts = gate.values(headers, "x-account")
        if len(accounts) > 1:
            raise gate.Refusal("selector_conflict", route.kind == gate.MODEL)
        if accounts and accounts[0] != IDENTITY_A[0]:
            raise gate.Refusal("foreign_account", latch=route.kind == gate.MODEL)
        return gate.Presented(presented.secret, tuple(accounts))

    def check_model(self, presented, body):
        try:
            model = json.loads(body).get("model")
        except (ValueError, AttributeError):
            model = None
        if model not in self.models:
            raise gate.Refusal("model_unproved")

    def _observation(self, status, body, at):
        if status != 200:
            return gate.Observation(at, False, reason="http_%d" % status)
        try:
            payload = json.loads(body)
            return gate.Observation(at, True, identity=tuple(payload["identity"]), data=payload["used"])
        except (ValueError, KeyError, TypeError):
            return gate.Observation(at, False, reason="malformed")

    def read_quota(self, gate_, presented, cause):
        status, _, body = gate_.fetch("GET", "/usage", secret=presented.secret,
                                      timeout=self.rebind_timeout if cause == "rebind" else None)
        return self._observation(status, body, 0.0)

    def observe_forwarded(self, route, presented, status, headers, body, received_at):
        return self._observation(status, body, received_at)

    def _store(self):
        return gate.Presented(gate.Secret(self.store_bearer))

    def startup(self, gate_):
        if self.holds_bearer:
            gate_.check(self._store(), "startup")

    def continue_check(self, gate_):
        if self.holds_bearer:
            return gate_.check(self._store(), "continuation")
        return gate.NEEDS_READ


class FakeEvaluator:
    """Pause when any enabled window's remaining <= reserve; resume needs continuation + pass."""

    def __init__(self, reserves=None, next_poll=None):
        self.reserves = {"five_hour": 5.0, "weekly": 5.0} if reserves is None else reserves
        self.next_poll = next_poll
        self.state, self.continued, self.events = "checking", False, []

    def __call__(self, event):
        self.events.append(event)
        if event.kind == "continuation":
            self.continued = True
            return gate.Decision(False, "continuation", next_poll=self.next_poll)
        if event.kind == "health":
            if event.name == "quota_unavailable" and self.state == "running" and not event.observation.permanent:
                return gate.Decision(True, None, next_poll=self.next_poll)
            self.state, self.continued = "paused", False
            return gate.Decision(False, event.name, {"reason": event.name}, next_poll=self.next_poll)
        used = event.observation.data
        for window, reserve in sorted(self.reserves.items()):
            remaining = 100.0 - used[window]
            if remaining <= reserve:
                self.state, self.continued = "paused", False
                return gate.Decision(False, "threshold", {"account": "A", "window": window, "remaining": remaining,
                                                          "reserve": reserve}, next_poll=self.next_poll)
        if self.state == "paused" and not self.continued:
            return gate.Decision(False, "awaiting_continuation", next_poll=self.next_poll)
        self.state, self.continued = "running", False
        return gate.Decision(True, None, {"account": "A"}, next_poll=self.next_poll)


def make_ca(directory):
    """(ca.pem, server ssl context) for 127.0.0.1 made with the host's openssl, or None without it."""
    openssl = shutil.which("openssl")
    if openssl is None:
        return None
    directory = Path(directory)
    (directory / "ca.cnf").write_text(
        "[req]\ndistinguished_name=dn\nx509_extensions=ca\nprompt=no\n[dn]\nCN=fixture-ca\n"
        "[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n")
    (directory / "leaf.ext").write_text(
        "basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\nsubjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n")
    steps = (
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
         "-days", "2", "-config", "ca.cnf"],
        [openssl, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr",
         "-subj", "/CN=127.0.0.1"],
        [openssl, "x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
         "-out", "leaf.pem", "-days", "2", "-extfile", "leaf.ext"],
    )
    for step in steps:
        if subprocess.run(step, cwd=directory, capture_output=True, timeout=60).returncode:
            return None
    import ssl
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(directory / "leaf.pem", directory / "leaf.key")
    return str(directory / "ca.pem"), context
