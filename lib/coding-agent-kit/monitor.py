"""coding-agent-kit protected mode: the monitor, a separate process that keeps a gate alive.

It holds no bearer and never touches the terminal, so killing it leaves the native's terminal
alone. Every HEARTBEAT seconds it sends {"op": "hb"} to the gate on its inherited socket, from its
own thread, so a slow quota read never delays liveness (the gate latches when the heartbeat stops
for longer than its liveness lease, or at EOF). Quota polls are separate: when one is due it asks
the gate to poll ({"op": "poll"}) or, with --reader-argv, runs the provider's reader command (its
own session, no terminal, the umask --reader-umask gives in octal or else the monitor's own,
killed with its group after --reader-timeout) and then reports {"op": "polled", "ok": B}. The gate
answers each with {"op": "next", "after": SECONDS}, which sets the next poll; until it does, polls
follow --poll. EOF from the gate ends the monitor.

    python3 -I -B monitor.py --fd N [--heartbeat S] [--poll S] [--reader-argv JSON]
                             [--reader-timeout S] [--reader-umask OCTAL]
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time


HEARTBEAT = 0.25
POLL = 30.0
READER_TIMEOUT = 45.0  # native retries of a stalled config bundle can take up to 40 s
AFTER_RANGE = (1.0, 3600.0)
LINE_LIMIT = 64 * 1024


def parse(argv):
    options = {"--heartbeat": HEARTBEAT, "--poll": POLL, "--reader-timeout": READER_TIMEOUT,
               "--reader-argv": None, "--reader-umask": None}
    seen, index = set(), 0
    while index < len(argv):
        name = argv[index]
        if name not in ("--fd", *options) or name in seen or index + 1 >= len(argv):
            raise ValueError("usage")
        seen.add(name)
        value = argv[index + 1]
        if name == "--fd":
            options[name] = int(value)
        elif name == "--reader-argv":
            reader = json.loads(value)
            if not isinstance(reader, list) or not reader or not all(isinstance(word, str) for word in reader):
                raise ValueError("usage")
            options[name] = reader
        elif name == "--reader-umask":
            if not 1 <= len(value) <= 4 or not all(digit in "01234567" for digit in value):
                raise ValueError("usage")
            options[name] = int(value, 8)
            if options[name] > 0o777:
                raise ValueError("usage")
        else:
            options[name] = float(value)
            if not 0 < options[name] <= 3600:
                raise ValueError("usage")
        index += 2
    if "--fd" not in options:
        raise ValueError("usage")
    return options


def run_reader(argv, timeout, stop, umask=None):
    """Run one reader command, with UMASK when given; True when it exits 0 within timeout. Its
    group dies on timeout."""
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True,
                                   umask=-1 if umask is None else umask)
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline and not stop.is_set():
        time.sleep(0.05)
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait()
        return False
    return process.returncode == 0


def main(argv):
    if sys.version_info < (3, 11):
        print("monitor: protected mode needs Python 3.11 or later.", file=sys.stderr)
        return 2
    try:
        options = parse(argv)
    except (ValueError, TypeError):
        print("usage: monitor.py --fd N [--heartbeat S] [--poll S] [--reader-argv JSON] "
              "[--reader-timeout S] [--reader-umask OCTAL]", file=sys.stderr)
        return 2
    sock = socket.socket(fileno=options["--fd"])
    stop, changed = threading.Event(), threading.Event()
    lock = threading.Lock()
    schedule = {"after": options["--poll"]}

    def send(message):
        with lock:
            try:
                sock.sendall((json.dumps(message, separators=(",", ":")) + "\n").encode())
            except OSError:
                stop.set()

    def beat():
        while not stop.is_set():
            send({"op": "hb"})
            stop.wait(options["--heartbeat"])

    def listen():
        buffer = b""
        while not stop.is_set():
            try:
                data = sock.recv(65536)
            except OSError:
                data = b""
            if not data or len(buffer) > LINE_LIMIT:
                stop.set()
                changed.set()
                return
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                after = message.get("after") if isinstance(message, dict) and message.get("op") == "next" else None
                if isinstance(after, (int, float)) and not isinstance(after, bool):
                    schedule["after"] = min(max(float(after), AFTER_RANGE[0]), AFTER_RANGE[1])
                    changed.set()

    threading.Thread(target=beat, daemon=True).start()
    threading.Thread(target=listen, daemon=True).start()
    due = time.monotonic() + schedule["after"]
    while not stop.is_set():
        if changed.wait(max(0.0, due - time.monotonic())):
            changed.clear()
            due = time.monotonic() + schedule["after"]
            continue
        if stop.is_set():
            break
        if options["--reader-argv"]:
            ok = run_reader(options["--reader-argv"], options["--reader-timeout"], stop, options["--reader-umask"])
            send({"op": "polled", "ok": ok})
        else:
            send({"op": "poll"})
        due = time.monotonic() + schedule["after"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
