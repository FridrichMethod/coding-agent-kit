"""A loopback fake of the Anthropic Messages API for tests of the real Claude Code (fabricated data only).

Run:  python3 -I -B -S claude_api.py --log FILE [--bash CMD] [--cross PATH JSON]

It listens on 127.0.0.1 only, prints its port on the first line of standard
output, and serves until standard input closes or it gets SIGTERM.

POST /v1/messages answers a scripted conversation chosen by the text of the
latest user prompt (the newest user message with a text block that is neither a
tool result nor a system reminder):

  PLAIN   one text message, PLAIN-ANSWER
  TOOL    one Bash tool call running CMD (default: true); after its result, TOOL-DONE
  other   one text message, FIXTURE-ANSWER

A request without tools (a session title and other side requests) gets
FIXTURE-ANSWER. POST /v1/messages/count_tokens answers a token count; any other
path gets a 404 JSON error. Answers stream when the request asks for it.

--cross PATH JSON writes JSON to PATH (an owned 0600 file, replaced atomically)
right before the fake answers TOOL with its tool call, so a usage file can cross
a reserve between the turn's two requests.

Every request is appended to --log as one JSON line: the method, the path, the
header names, the parsed JSON body and the scenario chosen. No header value is
logged, so no credential reaches the log.
"""

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time

SCENARIOS = ("TOOL", "PLAIN")


def text_blocks(content):
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]


def has_tool_result(content):
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def latest_prompt(messages):
    """(index, text) of the newest user message that carries typed text."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        typed = [t for t in text_blocks(message.get("content"))
                 if t.strip() and not t.lstrip().startswith("<system-reminder>")]
        if typed:
            return index, "\n".join(typed)
    return None, ""


def scenario_of(text):
    for name in SCENARIOS:
        if name in text:
            return name
    return "OTHER"


def replace(path, text):
    temporary = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(text)
    os.replace(temporary, path)


class Fake:
    def __init__(self, log, bash="true", cross=None):
        self.log_path, self.bash, self.cross = log, bash, cross
        self.lock = threading.Lock()
        self.counter = 0
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]

    def start(self):
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(sock,), daemon=True).start()

    def record(self, entry):
        with self.lock:
            self.counter += 1
            entry["n"] = self.counter
            with open(self.log_path, "a") as handle:
                handle.write(json.dumps(entry) + "\n")

    @staticmethod
    def read_request(sock):
        raw = b""
        while b"\r\n\r\n" not in raw:
            data = sock.recv(65536)
            if not data:
                return None
            raw += data
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        while len(body) < length:
            data = sock.recv(65536)
            if not data:
                break
            body += data
        return method, target, headers, body

    def serve(self, sock):
        try:
            sock.settimeout(30)
            while True:
                request = self.read_request(sock)
                if request is None:
                    return
                method, target, headers, body = request
                self.answer(sock, method, target, headers, body)
                if headers.get("connection", "").lower() == "close":
                    return
        except (OSError, ValueError):
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def answer(self, sock, method, target, headers, body):
        path = target.split("?")[0]
        try:
            document = json.loads(body) if body else None
        except ValueError:
            document = None
        entry = {"at": time.time(), "method": method, "path": path, "headers": sorted(headers), "body": document}
        if method == "POST" and path == "/v1/messages/count_tokens":
            self.record(entry)
            return self.send_json(sock, 200, {"input_tokens": 10})
        if method == "POST" and path == "/v1/messages" and isinstance(document, dict):
            events, scenario = self.conversation(document)
            entry["scenario"] = scenario
            self.record(entry)
            if scenario == "TOOL:call" and self.cross:
                replace(*self.cross)
            return self.send_message(sock, document, events)
        self.record(entry)
        return self.send_json(sock, 404, {"type": "error", "error": {"type": "not_found_error", "message": path}})

    def conversation(self, request):
        model = request.get("model", "claude-fixture")
        tools = request.get("tools") or []
        messages = request.get("messages") or []
        index, prompt = latest_prompt(messages)
        scenario = scenario_of(prompt)
        results = sum(1 for m in messages[(index or 0) + 1:]
                      if isinstance(m, dict) and m.get("role") == "user" and has_tool_result(m.get("content")))
        if not tools:
            return message_events(model, text="FIXTURE-ANSWER"), "no-tools"
        if scenario == "PLAIN":
            return message_events(model, text="PLAIN-ANSWER"), scenario
        if scenario == "TOOL":
            offered = {t.get("name") for t in tools if isinstance(t, dict)}
            if results == 0 and "Bash" in offered:
                return message_events(model, tool=("Bash", {"command": self.bash, "description": "fixture call"})), \
                    "TOOL:call"
            return message_events(model, text="TOOL-DONE"), "TOOL:done"
        return message_events(model, text="FIXTURE-ANSWER"), scenario

    @staticmethod
    def send_message(sock, request, events):
        if request.get("stream"):
            data = b"".join(b"event: %s\ndata: %s\n\n" % (e["type"].encode(), json.dumps(e).encode()) for e in events)
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nrequest-id: req_fixture\r\n"
                         b"Content-Length: %d\r\n\r\n" % len(data) + data)
            return
        body = json.dumps(assemble(events)).encode()
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nrequest-id: req_fixture\r\n"
                     b"Content-Length: %d\r\n\r\n" % len(body) + body)

    @staticmethod
    def send_json(sock, status, payload):
        body = json.dumps(payload).encode()
        sock.sendall(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                     % (status, len(body)) + body)


def message_events(model, text=None, tool=None):
    stamp = int(time.time() * 1000)
    events = [{"type": "message_start", "message": {
        "id": "msg_fixture_%d" % stamp, "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 0, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0}}}]
    if tool is None:
        events += [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}]
        stop = "end_turn"
    else:
        name, value = tool
        events += [{"type": "content_block_start", "index": 0, "content_block": {
                       "type": "tool_use", "id": "toolu_fixture_%d" % stamp, "name": name, "input": {}}},
                   {"type": "content_block_delta", "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)}}]
        stop = "tool_use"
    return events + [{"type": "content_block_stop", "index": 0},
                     {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                      "usage": {"output_tokens": 5}},
                     {"type": "message_stop"}]


def assemble(events):
    message = dict(events[0]["message"])
    block = dict(events[1]["content_block"])
    delta = events[2]["delta"]
    if block["type"] == "text":
        block["text"] = delta["text"]
    else:
        block["input"] = json.loads(delta["partial_json"])
    message["content"] = [block]
    message["stop_reason"] = events[4]["delta"]["stop_reason"]
    message["usage"] = dict(message["usage"], output_tokens=5)
    return message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--bash", default="true")
    parser.add_argument("--cross", nargs=2, metavar=("PATH", "JSON"))
    args = parser.parse_args()
    fake = Fake(os.path.abspath(args.log), args.bash, args.cross)
    fake.start()
    print(fake.port, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try:
        sys.stdin.read()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
