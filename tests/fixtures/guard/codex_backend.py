"""A loopback fake of the Responses API that a Codex custom model provider streams from (fabricated data only).

Run:  python3 -I -B -S codex_backend.py --log FILE [--limits JSON] [--shell CMD]

It listens on 127.0.0.1 only, prints its port on the first line of standard
output, and serves until standard input closes or it gets SIGTERM. Point Codex
at it with a model provider whose base_url is http://127.0.0.1:PORT/v1 and
whose wire_api is "responses"; no request needs a credential.

POST .../responses answers a scripted conversation chosen by the newest
keyword in the request's user and developer messages:

  PLAIN   one text message, PLAIN-ANSWER
  TOOL    a call of the shell tool the request offers, running CMD (default:
          true); after its output a second such call; after that output,
          TOOL-DONE. Codex writes a response's token_count line after the
          response's tool outputs, so only the second call's PreToolUse hook
          sees the rate limits of an earlier response in the rollout.
  SPAWN   a call of collaboration.spawn_agent (offered with --enable
          multi_agent_v2) that starts a sub-agent on a copy of the history;
          after its output a call of collaboration.wait_agent; after that
          output, SPAWN-DONE.
  other   one text message, FIXTURE-ANSWER

A request whose x-codex-turn-metadata header names an agent other than /root
is a sub-agent's: whatever its messages hold, it is answered with one call of
the shell tool running CMD, and after that call's output with CHILD-DONE.

Every answer carries the rate limits of --limits the way the real backend
sends them, as response headers that Codex turns into the rollout's
token_count line: x-codex-primary-used-percent, -window-minutes and -reset-at,
the same for secondary. --limits is JSON {"primary": [USED, MINUTES, RESET_IN],
"secondary": [...]}, RESET_IN in seconds from the answer; a slot left out sends
no headers. A GET of .../models answers an empty list; any other request a 404.

Every request is appended to --log as one JSON line: the method, the path, the
script chosen (CHILD for a sub-agent's), the names of the tools the request
offers, and the text of the tool output it ends with (null when it ends with
something else). No header value and no other part of the body is logged.
"""

import argparse
import gzip
import http.server
import json
import os
import re
import signal
import sys
import threading
import time

KEYWORD = re.compile(r"\b(PLAIN|TOOL|SPAWN)\b")
OUTPUTS = ("function_call_output", "custom_tool_call_output")
SHELLS = (("exec_command", lambda cmd: {"cmd": cmd}), ("shell_command", lambda cmd: {"command": cmd}),
          ("shell", lambda cmd: {"command": ["/bin/sh", "-c", cmd]}))
# The calls SPAWN makes, one per answer, each after the output of the one before: (namespace, name, arguments).
SPAWNS = (("collaboration", "spawn_agent", {"task_name": "helper", "message": "run the command", "fork_turns": "all"}),
          ("collaboration", "wait_agent", {"timeout_ms": 20000}))
CHILD = "call_child_"  # the call ids of a sub-agent's own calls, which its copy of the history never holds


def texts(item):
    content = item.get("content")
    if isinstance(content, str):
        return [content]
    return [part["text"] for part in content or [] if isinstance(part, dict) and isinstance(part.get("text"), str)]


def script_of(items):
    """The newest keyword in a user or developer message, or None."""
    found = None
    for item in items:
        if item.get("type") == "message" and item.get("role") in ("user", "developer"):
            for text in texts(item):
                for match in KEYWORD.finditer(text):
                    found = match.group(1)
    return found


def offered(request):
    """The function tools the request offers, by name, with namespaced ones as NAMESPACE.NAME."""
    tools = [tool for tool in request.get("tools") or [] if isinstance(tool, dict)]
    for item in request.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "additional_tools":
            tools.extend(tool for tool in item.get("tools") or [] if isinstance(tool, dict))
    names = {}
    for tool in tools:
        if tool.get("type") == "namespace":
            for inner in tool.get("tools") or []:
                if isinstance(inner, dict):
                    names[f"{tool.get('name')}.{inner.get('name')}"] = inner
        else:
            names[str(tool.get("name") or tool.get("type"))] = tool
    return names


def output_text(item):
    output = item.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        return "".join(part.get("text", "") for part in output if isinstance(part, dict))
    return json.dumps(output)


def message(text, number):
    return {"type": "message", "role": "assistant", "id": f"msg_fixture_{number}",
            "content": [{"type": "output_text", "text": text}]}


def call(name, arguments, call_id, number, namespace=None):
    item = {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": call_id,
            "id": f"fc_fixture_{number}"}
    return {**item, "namespace": namespace} if namespace else item


def shell_call(tools, shell, number, prefix="call_fixture_"):
    for name, arguments in SHELLS:
        if tools.get(name, {}).get("type") == "function":
            return call(name, arguments(shell), f"{prefix}{number}", number)
    return message("NO-SHELL-OFFERED", number)


def answer(request, number, shell, agent=None):
    """(script, the items of the answer) for one model request; AGENT is the agent its turn metadata names."""
    items = [item for item in request.get("input") or [] if isinstance(item, dict)]
    script = script_of(items)
    tools = offered(request)
    if agent not in (None, "", "/root"):  # a sub-agent's request
        if any(item.get("type") in OUTPUTS and str(item.get("call_id")).startswith(CHILD) for item in items):
            return "CHILD", [message("CHILD-DONE", number)]
        return "CHILD", [shell_call(tools, shell, number, CHILD)]
    if script == "PLAIN":
        return script, [message("PLAIN-ANSWER", number)]
    if script not in ("TOOL", "SPAWN"):
        return script, [message("FIXTURE-ANSWER", number)]
    start = max((index for index, item in enumerate(items) if script_of([item]) == script), default=0)
    done = sum(item.get("type") in OUTPUTS for item in items[start:])
    if script == "SPAWN":
        if done >= len(SPAWNS):
            return script, [message("SPAWN-DONE", number)]
        namespace, name, arguments = SPAWNS[done]
        if f"{namespace}.{name}" not in tools:
            return script, [message("NO-SPAWN-OFFERED", number)]
        return script, [call(name, arguments, f"call_fixture_{number}", number, namespace)]
    if done >= 2:
        return script, [message("TOOL-DONE", number)]
    return script, [shell_call(tools, shell, number)]


def events(items, number):
    response = {"id": f"resp_fixture_{number}"}
    stream = [("response.created", {"type": "response.created", "response": response})]
    for item in items:
        stream.append(("response.output_item.added", {"type": "response.output_item.added", "item": item}))
        stream.append(("response.output_item.done", {"type": "response.output_item.done", "item": item}))
    usage = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 10,
             "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 110}
    stream.append(("response.completed", {"type": "response.completed", "response": dict(response, usage=usage)}))
    return b"".join(b"event: %s\ndata: %s\n\n" % (name.encode(), json.dumps(data).encode()) for name, data in stream)


def limit_headers(limits, now):
    headers = []
    for slot in ("primary", "secondary"):
        if limits.get(slot):
            used, minutes, reset_in = limits[slot]
            headers += [(f"x-codex-{slot}-used-percent", repr(float(used))),
                        (f"x-codex-{slot}-window-minutes", str(int(minutes))),
                        (f"x-codex-{slot}-reset-at", str(int(now + reset_in)))]
    return headers


class Fake:
    def __init__(self, log, limits, shell):
        self.log, self.limits, self.shell = log, limits, shell
        self.lock, self.count = threading.Lock(), 0
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def send(self, status, payload, kind, headers=()):
                self.send_response(status)
                for name, value in (("Content-Type", kind), ("Content-Length", str(len(payload))),
                                    ("Connection", "close"), *headers):
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)
                self.close_connection = True

            def handle_any(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                if (self.headers.get("content-encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                path = self.path.split("?")[0]
                with fake.lock:
                    fake.count += 1
                    number = fake.count
                entry = {"method": self.command, "path": path}
                if self.command == "POST" and path.endswith("/responses"):
                    try:
                        request = json.loads(raw or b"{}")
                    except ValueError:
                        request = {}
                    request = request if isinstance(request, dict) else {}
                    try:
                        turn = json.loads(self.headers.get("x-codex-turn-metadata") or "{}")
                    except ValueError:
                        turn = {}
                    agent = turn.get("agent_name") if isinstance(turn, dict) else None
                    script, items = answer(request, number, fake.shell, agent)
                    inputs = [item for item in request.get("input") or [] if isinstance(item, dict)]
                    last = inputs[-1] if inputs else {}
                    entry.update(script=script, tools=sorted(offered(request)),
                                 output=output_text(last) if last.get("type") in OUTPUTS else None)
                    fake.record(entry)
                    self.send(200, events(items, number), "text/event-stream", limit_headers(fake.limits, time.time()))
                    return
                fake.record(entry)
                if self.command == "GET" and path.endswith("/models"):
                    self.send(200, b'{"object":"list","data":[],"models":[]}', "application/json")
                else:
                    self.send(404, b"{}", "application/json")

            do_GET = do_POST = do_PUT = do_DELETE = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    def record(self, entry):
        with self.lock, open(self.log, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")

    def start(self):
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--limits", default="{}")
    parser.add_argument("--shell", default="true")
    args = parser.parse_args()
    fake = Fake(os.path.abspath(args.log), json.loads(args.limits), args.shell)
    fake.start()
    print(fake.port, flush=True)
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    try:
        sys.stdin.read()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
