"""A fake native TUI for the supervisor tests; it runs on the supervisor's PTY.

It prints READY with its pid and the pid variable, reports SIGWINCH sizes, exits 42 on SIGTERM
and reads one command per line: request (a model POST to the gate on GATE_PORT), connect TARGET
(a CONNECT through the gate; prints its status), env NAME, spawn [quick|slow|nosid] (a grandchild
that sleeps, its pid in SPAWN_FILE: quick detaches at once with setsid, slow detaches with setsid
only after its parent lingered 2 s, nosid is orphaned at once but stays in the session), suspend
(SIGTSTP to itself), stop (SIGSTOP to itself), exit N, kill SIG.
"""

import fcntl
import http.client
import json
import os
import signal
import socket
import struct
import sys
import termios
import time


def out(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def size():
    rows, cols = struct.unpack("HHHH", fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))[:2]
    return "%dx%d" % (rows, cols)


def request():
    connection = http.client.HTTPConnection("127.0.0.1", int(os.environ["GATE_PORT"]), timeout=10)
    try:
        body = json.dumps({"model": "fixture-model"})
        connection.request("POST", "/model", body, {"Authorization": "Bearer fixture-bearer-A-0001",
                                                      "X-Account": "acct-A"})
        response = connection.getresponse()
        response.read()
        return response.status
    except OSError:
        return 0
    finally:
        connection.close()


def connect(target):
    try:
        sock = socket.create_connection(("127.0.0.1", int(os.environ["GATE_PORT"])), timeout=10)
    except OSError:
        return 0
    try:
        sock.sendall(b"CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target.encode(), target.encode()))
        head = sock.recv(4096).split(b" ")
        return int(head[1]) if len(head) > 1 and head[1].isdigit() else 0
    except OSError:
        return 0
    finally:
        sock.close()


def spawn(mode):
    path = os.environ["SPAWN_FILE"]
    if os.fork() == 0:
        if mode == "quick":
            os.setsid()
        if os.fork() == 0:
            if mode == "slow":
                os.setsid()
            with open(path + ".new", "w") as stream:
                stream.write(str(os.getpid()))
            os.replace(path + ".new", path)
            for fd in (0, 1, 2):
                os.close(fd)
            time.sleep(60)
            os._exit(0)
        if mode == "slow":
            time.sleep(2.0)  # long enough for a descendant snapshot to see the grandchild
        os._exit(0)
    os.wait()
    deadline = time.monotonic() + 5
    while not os.path.exists(path) and time.monotonic() < deadline:
        time.sleep(0.01)


def main():
    signal.signal(signal.SIGWINCH, lambda *args: out("WINCH " + size()))
    signal.signal(signal.SIGTERM, lambda *args: (out("TERM"), os._exit(42)))
    if os.environ.get("NATIVE_MARKER"):
        with open(os.environ["NATIVE_MARKER"], "a") as stream:
            stream.write("ran\n")
    if sys.argv[1:2] == ["--confined"]:
        out("CONFINED " + sys.argv[2])
    out("READY pid=%d env=%s size=%s" % (os.getpid(), os.environ.get("CODING_AGENT_KIT_CLAUDE_PID"), size()))
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        words = line.split()
        if not words:
            continue
        if words[0] == "request":
            out("STATUS %d" % request())
        elif words[0] == "connect":
            out("CONNECT %d" % connect(words[1]))
        elif words[0] == "env":
            out("ENV %s=%s" % (words[1], os.environ.get(words[1], "<unset>")))
        elif words[0] == "spawn":
            spawn(words[1] if len(words) > 1 else "quick")
            out("SPAWNED")
        elif words[0] in ("suspend", "stop"):
            os.kill(os.getpid(), signal.SIGTSTP if words[0] == "suspend" else signal.SIGSTOP)
            out("AFTER " + words[0].upper())
        elif words[0] == "exit":
            return int(words[1])
        elif words[0] == "kill":
            os.kill(os.getpid(), int(words[1]))


if __name__ == "__main__":
    raise SystemExit(main())
