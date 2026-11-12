"""Minimal loopback Messages gate around AdmissionController (research fixture).

Admission happens after the request head and body are read and BEFORE any
upstream connection is opened, so a refused request produces zero upstream
bytes. The Authorization header is forwarded unchanged; the gate never
substitutes a bearer. Not a production gateway: no SSE relay semantics beyond
copying bytes, no CONNECT, no TLS, no retries.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading

BODY_LIMIT = 8 * 1024 * 1024
FORWARDED = ("authorization", "content-type", "anthropic-version", "anthropic-beta", "accept")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        gate = self.server.gate
        if self.path.split("?")[0] not in ("/v1/messages",):
            return self._reply(404, {"type": "error", "error": {"type": "not_found_error"}})
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > BODY_LIMIT:
            return self._reply(413, {"type": "error", "error": {"type": "request_too_large"}})
        body = self.rfile.read(int(length))
        try:
            model = json.loads(body).get("model")
        except (ValueError, AttributeError):
            model = None
        client = self.connection

        def close():
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        decision = gate.controller.admit(list(self.headers.items()), model, close)
        if not decision.ok:
            return self._reply(403, {"type": "error", "error": {"type": "quota_gate_closed",
                                                                 "message": decision.reason}})
        try:
            upstream = socket.create_connection(gate.upstream, timeout=5)
            try:
                head = ["POST %s HTTP/1.1" % self.path, "Host: upstream.fixture", "Connection: close",
                        "Content-Length: %d" % len(body)]
                head += ["%s: %s" % (k, v) for k, v in self.headers.items() if k.lower() in FORWARDED]
                upstream.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
                while True:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        break
                    client.sendall(chunk)
            finally:
                upstream.close()
        except OSError:
            pass
        finally:
            gate.controller.release(decision.stream_id)
        self.close_connection = True


class GateServer:
    def __init__(self, controller, upstream):
        self.controller, self.upstream = controller, upstream
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.gate = self
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


class FakeUpstream:
    """Counts connections and records which fabricated bearer label reached it."""

    def __init__(self, labels):
        self.labels, self.connections, self.received = labels, 0, []
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.address = self.sock.getsockname()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        with conn:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
            auth = [line.split(":", 1)[1].strip() for line in head if line.lower().startswith("authorization:")]
            label = next((name for token, name in self.labels.items() if auth == ["Bearer " + token]), "other")
            with self._lock:
                self.received.append(label)
            body = b'{"type":"message","content":[]}'
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n%s" % (len(body), body))

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.sock.close()
