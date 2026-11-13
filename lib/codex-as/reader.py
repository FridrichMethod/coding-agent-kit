"""codex-as protected mode: one fresh native App Server for a model-free quota read or the
pre-launch credential refresh.

    python3 -I -B reader.py SPEC_FILE                      (already confined by the supervisor)
    python3 -I -B reader.py --confine BOUNDARY SPEC_FILE   (the monitor's polls)

A --no-daemon TUI runs its App Server in process, and a long-lived one keeps the bearer it loaded,
so every controller read starts its own `codex app-server` (G0 D2): it loads the account's current
credential, and the usage request it sends passes through the gate, which observes it. The reader
itself sees only whether the request was answered.

SPEC_FILE is JSON that protect.py writes into the invocation's private directory, which lies
outside every path the boundary can write: command (the codex argv, ending in app-server and its
options), env, cwd, mode ("read": account/rateLimits/read; "refresh": account/read with
refreshToken, native's own refresh) and timeout (seconds for the whole exchange). The supervisor
confines a reader it runs itself. The monitor runs outside the boundary, so its reader argv, which
the launcher builds, carries --confine and BOUNDARY, a JSON object {"port", "writable", "profile"}
(the gate port, the writable paths and, on macOS, the self-tested profile text): the reader enters
the boundary first and reads SPEC_FILE only inside it, so nothing in that file can widen the
boundary.

Exit status: 0 the request was answered, 2 the App Server did not start, 3 initialize failed, 4 the
request failed or timed out, 5 the boundary refused, 6 unusable arguments or specification, and
7 to 10 the request failed with one of native's four application network policy denials (POLICY:
the exact denial text, which the App Server's error message contains after its own prefix; no other
error text is kept).
"""

import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time


HERE = Path(os.path.realpath(__file__)).parent
KIT = HERE.parent / "coding-agent-kit"
# The App Server builds the User-Agent of its upstream requests from this name: it names no
# project, user or host.
CLIENT_INFO = {"name": "quota-probe", "version": "1"}
REQUESTS = {"read": ("account/rateLimits/read", {"excludeResetCreditDetails": True}),
            "refresh": ("account/read", {"refreshToken": True})}
# codex 0.167.2 http-client NetworkPolicyDenied: unavailable, destination, revoked, unsupported transport.
# The App Server wraps the denial in a message of its own ("failed to fetch codex rate limits: ..."),
# so the exact denial text is looked for inside the message, never a looser pattern.
POLICY = {"application network policy is unavailable": 7,
          "destination denied by application network policy": 8,
          "application network permission was revoked": 9,
          "this SDK transport is disabled by application network restrictions": 10}
LINE_LIMIT = 1024 * 1024
CLOSE_GRACE = 5.0


def boundary(text):
    """The BOUNDARY argument as (port, writable, profile), or None when it is unusable."""
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if not isinstance(value, dict) or set(value) != {"port", "writable", "profile"}:
        return None
    port, writable, profile = value["port"], value["writable"], value["profile"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return None
    if not isinstance(writable, list) or not all(isinstance(path, str) for path in writable):
        return None
    if profile is not None and not isinstance(profile, str):
        return None
    return port, writable, profile


def sandbox_module():
    name = "sandbox_macos" if sys.platform == "darwin" else "sandbox_linux"
    module_spec = importlib.util.spec_from_file_location("codex_as_reader_" + name, KIT / (name + ".py"))
    sandbox = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(sandbox)
    return sandbox


def confine(port, writable, profile, spec_file):
    """Enter the boundary: in process on Linux; on macOS by exec'ing this reader again, without
    --confine, under the self-tested profile. Returns only on Linux, or never on macOS."""
    sandbox = sandbox_module()
    if sys.platform == "darwin":
        tested = sandbox.Tested(profile=profile, answers={})
        prefix = sandbox.confine(port, writable, "codex", tested=tested)
        os.execv(prefix[0], prefix + [sys.executable, "-I", "-B", str(Path(__file__).resolve()), spec_file])
    sandbox.confine(port, writable, "codex")


class Server:
    """Newline-delimited JSON-RPC over the App Server's stdio; server requests are refused."""

    def __init__(self, spec):
        self.process = subprocess.Popen(spec["command"], env=spec["env"], cwd=spec["cwd"], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.buffer, self.next_id = b"", 0

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method, params, deadline):
        self.next_id += 1
        ident = self.next_id
        self.send({"id": ident, "method": method, "params": params})
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if isinstance(message.get("method"), str) and "id" in message:
                    self.send({"id": message["id"], "error": {"code": -32000, "message": "not supported"}})
                elif message.get("id") == ident and "method" not in message:
                    return message
            left = deadline - time.monotonic()
            if left <= 0 or len(self.buffer) > LINE_LIMIT:
                return None
            if select.select([self.process.stdout], [], [], min(left, 0.2))[0]:
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    return None
                self.buffer += data

    def close(self):
        """Close stdin (the App Server exits on EOF), then send the App Server SIGTERM and SIGKILL if
        it lingers."""
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=CLOSE_GRACE)
        except subprocess.TimeoutExpired:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    self.process.send_signal(sig)
                    self.process.wait(timeout=2)
                    break
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    continue


def exchange(spec):
    deadline = time.monotonic() + float(spec["timeout"])
    try:
        server = Server(spec)
    except OSError:
        return 2
    try:
        answer = server.request("initialize", {"clientInfo": CLIENT_INFO}, deadline)
        if not answer or "result" not in answer:
            return 3
        server.send({"method": "initialized", "params": {}})
        method, params = REQUESTS[spec["mode"]]
        answer = server.request(method, params, deadline)
        if answer and "result" in answer:
            return 0
        error = answer.get("error") if isinstance(answer, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        return next((code for text, code in POLICY.items() if isinstance(message, str) and text in message), 4)
    except (OSError, ValueError):
        return 4
    finally:
        server.close()


def main(argv):
    limits = None
    if argv[:1] == ["--confine"]:
        if len(argv) != 3:
            return 6
        limits = boundary(argv[1])
        if limits is None:
            return 6
        argv = argv[2:]
    if len(argv) != 1:
        return 6
    if limits is not None:
        try:
            confine(*limits, argv[0])
        except Exception:
            return 5
    try:
        with open(argv[0], "rb") as stream:
            spec = json.load(stream)
        if spec["mode"] not in REQUESTS:
            return 6
    except (OSError, ValueError, KeyError, TypeError):
        return 6
    return exchange(spec)


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        raise SystemExit(6)
    raise SystemExit(main(sys.argv[1:]))
