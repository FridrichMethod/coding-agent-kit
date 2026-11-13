"""A Claude-shaped HTTP upstream on 127.0.0.1 for the claude-as reserve tests (fabricated data only).

GET /api/oauth/profile and /api/oauth/usage answer per bearer (identities, utilization, extra rows,
status overrides); POST /v1/messages and /v1/messages/count_tokens answer a short SSE stream or, with
"forever" in the body, stream until the client goes away. With messages=True they answer as the
Messages API does, for a real Claude Code: count_tokens a token count, and /v1/messages a message
stream whose text is FIXTURE-ANSWER, or, when the last user turn says "fixture background" and the
request offers tools, a background Bash tool call running `bash`, then FIXTURE-DONE for its result.
A bearer without an identity gets 401 everywhere. Every request is recorded with its headers and
arrival time. Run as a script it serves until stdin closes and prints its port first.
"""

import json
import socket
import sys
import threading
import time

BEARER_A = "fixture-claude-bearer-A-0001"
BEARER_A2 = "fixture-claude-bearer-A-0002"
BEARER_B = "fixture-claude-bearer-B-0001"
IDENTITY_A = ("acct-0000-fixture", "org-0000-fixture")
IDENTITY_B = ("acct-1111-other", "org-1111-other")


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def message_events(model, text=None, bash=None):
    """The server-sent events of one assistant message: TEXT, or a background Bash call of BASH."""
    events = [{"type": "message_start", "message": {
        "id": "msg_fixture", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}]
    if bash is None:
        events += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}]
    else:
        call = {"command": bash, "run_in_background": True, "description": "fixture background"}
        events += [{"type": "content_block_start", "index": 0, "content_block": {
                       "type": "tool_use", "id": "toolu_fixture", "name": "Bash", "input": {}}},
                   {"type": "content_block_delta", "index": 0, "delta": {
                       "type": "input_json_delta", "partial_json": json.dumps(call)}}]
    return events + [{"type": "content_block_stop", "index": 0},
                     {"type": "message_delta", "delta": {"stop_reason": "end_turn" if bash is None else "tool_use",
                                                         "stop_sequence": None}, "usage": {"output_tokens": 5}},
                     {"type": "message_stop"}]


class ClaudeUpstream:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        self.identities = {BEARER_A: IDENTITY_A, BEARER_A2: IDENTITY_A, BEARER_B: IDENTITY_B}
        self.used = {"five_hour": 10, "seven_day": 20}
        self.extra = {}            # additional usage rows, e.g. {"seven_day_opus": {...}}
        self.status = {}           # path -> forced status
        self.close_header = False  # answer reads with "Connection: close", as many real servers do
        self.messages = False      # answer Messages requests as the Messages API does (a real native)
        self.bash = "true"         # the background command of a "fixture background" turn
        self.requests, self.lock = [], threading.Lock()
        self.closing = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.closing = True
        self.listener.close()

    def paths(self, method=None):
        with self.lock:
            return [r["path"] for r in self.requests if method is None or r["method"] == method]

    def model_requests(self):
        with self.lock:
            return [r for r in self.requests if r["path"].startswith("/v1/messages")]

    def _accept(self):
        while not self.closing:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        try:
            sock.settimeout(10)
            raw = b""
            while b"\r\n\r\n" not in raw:
                data = sock.recv(65536)
                if not data:
                    return
                raw += data
            head, _, body = raw.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = [tuple(part.strip() for part in line.split(":", 1)) for line in lines[1:] if ":" in line]
            length = int(dict((k.lower(), v) for k, v in headers).get("content-length", "0"))
            while len(body) < length:
                data = sock.recv(65536)
                if not data:
                    return
                body += data
            record = {"method": method, "path": target, "headers": headers, "body": body, "at": time.monotonic()}
            with self.lock:
                self.requests.append(record)
            self._answer(sock, method, target.split("?")[0], headers, body)
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _answer(self, sock, method, path, headers, body):
        auth = [v for k, v in headers if k.lower() == "authorization"]
        bearer = auth[0][7:] if auth and auth[0].startswith("Bearer ") else None
        identity = self.identities.get(bearer)
        if path in self.status:
            return self._send(sock, self.status[path], {"type": "error", "error": {"type": "api_error"}})
        if identity is None:
            return self._send(sock, 401, {"type": "error", "error": {"type": "authentication_error"}})
        if method == "GET" and path == "/api/oauth/profile":
            return self._send(sock, 200, {"account": {"uuid": identity[0], "email": "fixture@example.invalid"},
                                          "organization": {"uuid": identity[1], "name": "fixture"}})
        if method == "GET" and path == "/api/oauth/usage":
            now = time.time()
            usage = {"five_hour": {"utilization": self.used["five_hour"], "resets_at": stamp(now + 3600)},
                     "seven_day": {"utilization": self.used["seven_day"], "resets_at": stamp(now + 86400)},
                     "seven_day_opus": None, "seven_day_oauth_apps": None, "extra_usage": {"is_enabled": False}}
            usage.update(self.extra)
            return self._send(sock, 200, usage)
        if method == "POST" and path in ("/v1/messages", "/v1/messages/count_tokens") and self.messages:
            return self._message(sock, path, json.loads(body or b"{}"))
        if method == "POST" and path in ("/v1/messages", "/v1/messages/count_tokens"):
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
            forever, count = b'"forever"' in body, 0
            while forever or count < 3:
                event = b"event: ping\ndata: {}\n\n"
                sock.sendall(b"%x\r\n%s\r\n" % (len(event), event))
                count += 1
                time.sleep(0.02)
            sock.sendall(b"0\r\n\r\n")
            return
        return self._send(sock, 404, {"type": "error", "error": {"type": "not_found_error"}})

    def _message(self, sock, path, request):
        if path.endswith("/count_tokens"):
            return self._send(sock, 200, {"input_tokens": 10})
        model, last = request.get("model", "claude-fixture"), json.dumps(request.get("messages", [])[-1:])
        if request.get("tools") and "tool_result" in last:
            events = message_events(model, "FIXTURE-DONE")
        elif request.get("tools") and "fixture background" in last:
            events = message_events(model, bash=self.bash)
        else:
            events = message_events(model, "FIXTURE-ANSWER")
        data = b"".join(b"event: %s\ndata: %s\n\n" % (event["type"].encode(), json.dumps(event).encode())
                        for event in events)
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: %d\r\n\r\n"
                     % len(data) + data)

    def _send(self, sock, status, payload):
        body = json.dumps(payload).encode()
        close = b"Connection: close\r\n" if self.close_header else b""
        sock.sendall(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\n%s\r\n"
                     % (status, len(body), close) + body)


if __name__ == "__main__":
    upstream = ClaudeUpstream()
    if len(sys.argv) > 1:
        upstream.used = json.loads(sys.argv[1])
    print(upstream.port, flush=True)
    sys.stdin.read()
