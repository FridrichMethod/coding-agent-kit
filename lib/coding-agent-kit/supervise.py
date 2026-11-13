"""coding-agent-kit protected mode: the launcher side of one protected invocation.

Load this file by path; it is never on sys.path. The launcher stays outside the boundary, owns the
user's terminal and the gate port for the whole invocation, and runs the native on a PTY of its own
instead of exec'ing it:

    holder = hold_port(extra)      loopback listeners on one random port (127.0.0.1 and ::1 where
                                   available) plus non-listening binds of the same port on every
                                   extra address (macOS, with SO_REUSEADDR: the wildcard and
                                   interface addresses the boundary module reports)
    Supervisor(...).run(argv, env) start the gate (the provider's gate script, see gate.py) on the
                                   held port, then the monitor; wait for its first heartbeat; run
                                   the adapter's preflight; start the native on its own PTY with
                                   the confine hook applied in the child just before exec; relay the
                                   terminal; offer trusted-terminal continuation; restart a dead or
                                   lost gate paused; at the end sweep every boundary descendant,
                                   stop the gate and monitor and release the port.

The confine hook is confine(port, argv) -> argv, called in the forked child after setsid and the
PTY setup and before exec: it applies the platform boundary in process (Linux) and/or returns the
argv to exec (macOS sandbox-exec), or raises to refuse; the refusal reaches the launcher and no
native runs. On Linux the launcher is a child subreaper, so detached descendants stay its own and
the end-of-invocation sweep finds them; elsewhere it tracks descendants by periodic snapshots.

Continuation is a trusted-terminal action: after a pause, the trigger key (F12 unless the adapter
chooses another) shows a fresh one-use code; typing it and Enter asks the gate for a fresh check.
Input typed before the pause is flushed, a bracketed paste never confirms, native output never
reaches this parser, and nothing but terminal input starts a check.
"""

import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty


HERE = Path(os.path.realpath(__file__)).parent
MONITOR = HERE / "monitor.py"
F12 = b"\x1b[24~"
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
PORTS = (49152, 60999)  # inside both the Linux and the macOS ephemeral ranges
STARTUP_TIMEOUT = 60.0
HEARD_TIMEOUT = 10.0
CONTROL_TIMEOUT = 20.0
RESTART_LIMIT, RESTART_DELAY = 5, 0.5
SWEEP_GRACE = 1.0
SNAPSHOT_EVERY = 1.0
PR_SET_CHILD_SUBREAPER = 36
PREFIX = "[coding-agent-kit] "
FORWARDED = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT)


class Refused(Exception):
    """Protected mode refuses this invocation before any native ran under the boundary."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def capture_proxy(env, parse):
    """The launcher's HTTPS_PROXY for the gate's upstream connections, captured once.

    parse is gate.parse_proxy. HTTPS_PROXY and https_proxy must agree; an unsupported proxy refuses
    instead of going direct. The native never gets it: its only proxy is the gate."""
    values = {env[name] for name in ("HTTPS_PROXY", "https_proxy") if env.get(name)}
    if len(values) > 1:
        raise Refused("HTTPS_PROXY and https_proxy differ")
    try:
        return parse(values.pop()) if values else None
    except ValueError:
        raise Refused("unsupported HTTPS_PROXY (only a plain http://HOST:PORT proxy)") from None


class Holder:
    """The gate port: listening loopback sockets plus reservations, held until close()."""

    def __init__(self, port, listeners, reserved):
        self.port, self.listeners, self.reserved = port, listeners, reserved

    def close(self):
        for sock in self.listeners + self.reserved:
            sock.close()
        self.listeners, self.reserved = [], []


def _family(address):
    return socket.AF_INET6 if ":" in address else socket.AF_INET


def hold_port(extra=(), port=None, attempts=64):
    """Hold one random port on loopback (and the extra addresses) for the whole invocation."""
    for _ in range(attempts):
        candidate = port or PORTS[0] + secrets.randbelow(PORTS[1] - PORTS[0] + 1)
        held = []
        try:
            l4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            held.append(l4)
            l4.bind(("127.0.0.1", candidate))
            listeners = [l4]
            try:
                l6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            except OSError:
                l6 = None
            if l6 is not None:
                held.append(l6)
                l6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    l6.bind(("::1", candidate))
                    listeners.append(l6)
                except OSError as error:
                    if error.errno == errno.EADDRINUSE:
                        raise
                    held.remove(l6)
                    l6.close()
            for sock in listeners:
                sock.listen(128)
            reserved = []
            for address in extra:
                sock = socket.socket(_family(address), socket.SOCK_STREAM)
                held.append(sock)
                if sys.platform == "darwin":
                    # BSD lets a SO_REUSEADDR wildcard coexist with the loopback listeners and still
                    # refuses squatters (KM); Linux refuses squatters only without it.
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if sock.family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    sock.bind(socket.getaddrinfo(address, candidate, socket.AF_INET6, socket.SOCK_STREAM)[0][4])
                else:
                    sock.bind((address, candidate))
                reserved.append(sock)
            return Holder(candidate, listeners, reserved)
        except OSError as error:
            for sock in held:
                sock.close()
            if error.errno != errno.EADDRINUSE or port:
                raise Refused("cannot hold the gate port: %s" % (os.strerror(error.errno) if error.errno else error))
    raise Refused("cannot hold the gate port: no free port")


def enable_subreaper():
    """Linux: make this process the child subreaper; False where unsupported."""
    if not sys.platform.startswith("linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0


def process_table():
    """{pid: (ppid, start)} of every visible process; start tells a reused pid apart."""
    table = {}
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % name, "rb") as stream:
                    fields = stream.read().rsplit(b")", 1)[1].split()
                table[int(name)] = (int(fields[1]), fields[19].decode())
            except (OSError, IndexError, ValueError):
                continue
        return table
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "lstart="], capture_output=True,
                             text=True, timeout=10, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return table
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2].strip())
    return table


def descendants(root, table):
    """Every pid below root in table."""
    children = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    found, stack = set(), list(children.get(root, ()))
    while stack:
        pid = stack.pop()
        if pid not in found:
            found.add(pid)
            stack.extend(children.get(pid, ()))
    return found


def _value(value):
    if isinstance(value, float):
        return ("%.2f" % value).rstrip("0").rstrip(".")
    return str(value)


def pause_message(reason, fields, key="F12"):
    """The deterministic local pause message: account, window, remaining, reserve, reset, last
    fresh observation, and how to request continuation; failures name the problem."""
    fields = fields or {}
    parts = ["quota reserve paused (%s)" % reason]
    if fields.get("account") is not None:
        parts.append("account " + _value(fields["account"]))
    if fields.get("window") is not None:
        parts.append("window " + _value(fields["window"]))
    if fields.get("remaining") is not None:
        text = "remaining %s%%" % _value(fields["remaining"])
        if fields.get("reserve") is not None:
            text += " (reserve %s%%)" % _value(fields["reserve"])
        parts.append(text)
    if fields.get("reset") is not None:
        parts.append("resets " + _value(fields["reset"]))
    if fields.get("last_fresh") is not None:
        parts.append("last fresh check " + _value(fields["last_fresh"]))
    parts.append("press %s, then type the code shown and Enter, to request a fresh check" % key)
    return "; ".join(parts)


class TerminalControl:
    """The trusted-terminal parser: forwards user input to the native and owns continuation.

    Only bytes read from the user's terminal reach feed(). pause() flushes unread terminal input;
    the trigger then shows a fresh code; Enter after the exact code calls check() once. A paste,
    an old code, a repeated trigger or any other key grants nothing."""

    def __init__(self, input_fd, forward, display, check, trigger=F12, key_name="F12", clock=time.monotonic):
        self.input_fd, self.forward, self.display, self.check = input_fd, forward, display, check
        self.trigger, self.key_name, self.clock = trigger, key_name, clock
        self.sequences = (trigger, PASTE_START, PASTE_END)
        self.paused, self.challenge, self.pasting = False, None, False
        self.answer, self.pending, self.pending_since = bytearray(), bytearray(), 0.0

    def pause(self):
        self.paused, self.challenge = True, None
        self.answer.clear()
        self.pending.clear()
        try:
            termios.tcflush(self.input_fd, termios.TCIFLUSH)
        except (termios.error, OSError):
            pass

    def resume(self):
        self.paused, self.challenge = False, None
        self.answer.clear()

    def tick(self):
        """Release a lone escape prefix after 50 ms (it was a key, not a sequence)."""
        if self.pending and self.clock() - self.pending_since >= 0.05:
            data = bytes(self.pending)
            self.pending.clear()
            for byte in data:
                self._ordinary(byte)

    def feed(self, data):
        for byte in data:
            if not self.pending:
                self.pending_since = self.clock()
            self.pending.append(byte)
            while self.pending:
                value = bytes(self.pending)
                if value in self.sequences:
                    self.pending.clear()
                    self._sequence(value)
                    break
                if any(sequence.startswith(value) for sequence in self.sequences):
                    break
                self._ordinary(self.pending.pop(0))

    def _sequence(self, value):
        if value == PASTE_START:
            self.pasting, self.challenge = True, None
            self.answer.clear()
            self.forward(value)
        elif value == PASTE_END:
            self.pasting = False
            self.forward(value)
        elif self.pasting or not self.paused:
            self.forward(value)
        elif self.challenge is None:
            self.challenge = secrets.token_hex(4)
            self.answer.clear()
            self.display("type %s then Enter to request a fresh quota check" % self.challenge)

    def _ordinary(self, byte):
        if self.pasting or self.challenge is None:
            self.forward(bytes([byte]))
            return
        if byte in (10, 13):
            accepted = bytes(self.answer) == self.challenge.encode("ascii")
            self.challenge = None
            self.answer.clear()
            if accepted and self.check():
                self.paused = False
                self.display("fresh quota check passed; model requests resume")
            elif not accepted:
                self.display("still paused: the code did not match; press %s for a new one" % self.key_name)
        elif byte in (8, 127):
            del self.answer[-1:]
        elif byte == 3:
            self.challenge = None
            self.answer.clear()
            self.forward(bytes([byte]))
        elif 32 <= byte < 127 and len(self.answer) < 64:
            self.answer.append(byte)
        else:
            self.challenge = None
            self.answer.clear()


def _child_fds(fds, stdio, report):
    """In a forked child: stdio to 0-2, fds to 3.., REPORT (close-on-exec) right after them, every
    other descriptor closed. Returns REPORT's new number."""
    limit = os.sysconf("SC_OPEN_MAX")
    limit = 65536 if limit <= 0 else min(limit, 65536)
    base = max(limit - len(fds) - 8, 64)
    moved = [fcntl.fcntl(fd, fcntl.F_DUPFD, base) for fd in [*stdio, *fds, report]]
    for target, fd in enumerate(moved[:-1]):
        os.dup2(fd, target, inheritable=True)
    slot = len(moved) - 1
    os.dup2(moved[-1], slot, inheritable=False)
    os.closerange(slot + 1, limit)
    return slot


class Supervisor:
    """One protected invocation: gate, monitor, native on a PTY, continuation, restart, sweep.

    gate_argv: the provider's gate entry ([python3, -I, -B, script]); gate.py's options follow.
    gate_config: the JSON object written to the gate's stdin (gate.py documents the core keys).
    monitor_reader: None (the gate polls) or the reader argv the monitor runs for each poll.
    reader: callable() -> bool that drives one fresh in-boundary read when a continuation needs it.
    preflight: callable(supervisor) run after the first heartbeat, before the native; may raise
        Refused. The gate must then report running.
    confine: callable(port, argv) -> argv, run in the native's child before exec.
    pid_env: name of an environment variable set to the native's own pid in its child.
    style: callable(text) -> text for the launcher's terminal messages (the kit colour policy).
    """

    def __init__(self, gate_argv, gate_config, *, python=sys.executable, monitor_reader=None,
                 monitor_reader_timeout=None, reader=None, preflight=None, confine=None, pid_env=None,
                 holder_extra=(), gate_env=None, trigger=F12, key_name="F12", restart_limit=RESTART_LIMIT,
                 restart_delay=RESTART_DELAY, startup_timeout=STARTUP_TIMEOUT, terminal=(0, 1), heartbeat=None,
                 style=None):
        self.gate_argv, self.gate_config = list(gate_argv), dict(gate_config)
        self.python, self.monitor_reader, self.reader = python, monitor_reader, reader
        self.monitor_reader_timeout, self.heartbeat = monitor_reader_timeout, heartbeat
        self.preflight, self.confine, self.pid_env = preflight, confine, pid_env
        self.holder_extra, self.gate_env = tuple(holder_extra), dict(gate_env or {})
        self.trigger, self.key_name = trigger, key_name
        self.restart_limit, self.restart_delay, self.startup_timeout = restart_limit, restart_delay, startup_timeout
        self.term_in, self.term_out = terminal
        self.style = style or (lambda text: text)
        self.holder = None
        self.gate_pid = self.monitor_pid = self.native_pid = None
        self.control, self.buffer, self.events = None, b"", []
        self.exited, self.kit = {}, set()
        self.restarts, self.down_since = 0, None
        self.subreaper, self.tracked, self.snapshot_at = False, {}, 0.0
        self.ui, self.master, self.to_native = None, None, bytearray()

    # -- children ----------------------------------------------------------------------------------
    def spawn(self, argv, env, *, fds=(), stdin=None, stdout=None, stderr=None, cwd=None, tty_fd=None,
              confine=False, pid_env=False):
        """fork and exec ARGV in a new session with fds at 3.. and nothing else inherited.

        tty_fd becomes the controlling terminal and stdio. confine applies the confine hook in the
        child just before exec; pid_env sets the pid variable to the child's own pid. Returns the
        pid; raises Refused when the hook refuses and OSError when exec fails."""
        read_end, write_end = os.pipe()
        devnull = os.open(os.devnull, os.O_RDWR)
        if tty_fd is not None:
            stdio = [tty_fd] * 3
        else:
            stdio = [devnull if fd is None else fd for fd in (stdin, stdout, stderr)]
        hook, port, variable = self.confine if confine else None, self.holder.port if self.holder else None, \
            self.pid_env if pid_env else None
        pid = os.fork()
        if pid == 0:
            kind, report = b"E", write_end
            try:
                os.close(read_end)
                os.setsid()
                if tty_fd is not None:
                    fcntl.ioctl(tty_fd, termios.TIOCSCTTY, 0)
                report = _child_fds(fds, stdio, write_end)
                child_env = dict(env)
                if variable:
                    child_env[variable] = str(os.getpid())
                if cwd is not None:
                    os.chdir(cwd)
                final = list(argv)
                if hook is not None:
                    kind = b"R"
                    final = list(hook(port, final))
                    kind = b"E"
                os.execve(final[0], final, child_env)
            except BaseException as error:
                number = getattr(error, "errno", None)
                detail = getattr(error, "reason", None) or (os.strerror(number) if number else str(error)) or \
                    type(error).__name__
                try:
                    os.write(report, kind + str(detail)[:400].encode("utf-8", "replace"))
                finally:
                    os._exit(3 if kind == b"R" else 127)
        os.close(write_end)
        os.close(devnull)
        chunks = []
        while True:
            data = os.read(read_end, 4096)
            if not data:
                break
            chunks.append(data)
        os.close(read_end)
        report = b"".join(chunks)
        if report:
            os.waitpid(pid, 0)
            text = report[1:].decode("utf-8", "replace")
            if report[:1] == b"R":
                raise Refused(text)
            raise OSError(errno.ENOEXEC, "cannot start %s: %s" % (os.path.basename(argv[0]), text))
        return pid

    def reap(self):
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            self.exited[pid] = status

    def _kill(self, pid, sig=signal.SIGKILL):
        if pid is None or pid in self.exited:
            return
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def _wait(self, pid, timeout):
        deadline = time.monotonic() + timeout
        while pid not in self.exited and time.monotonic() < deadline:
            self.reap()
            time.sleep(0.01)
        return self.exited.get(pid)

    # -- gate and monitor ------------------------------------------------------------------------------
    def hold(self):
        if self.holder is None:
            self.holder = hold_port(self.holder_extra)
        return self.holder.port

    def start_gate(self, mode="startup"):
        """Start a gate on the held port and wait for ready; raises Refused on a startup refusal."""
        self.hold()
        control, gate_control = socket.socketpair()
        pair = socket.socketpair() if mode != "refresh" else None
        listen = [sock.fileno() for sock in self.holder.listeners]
        fds = listen + [gate_control.fileno()] + ([pair[1].fileno()] if pair else [])
        numbers = [str(3 + index) for index in range(len(fds))]
        argv = self.gate_argv + ["--listen-fds", ",".join(numbers[:len(listen)]), "--control-fd",
                                 numbers[len(listen)], "--mode", mode]
        if pair:
            argv += ["--monitor-fd", numbers[-1]]
        config_read, config_write = os.pipe()
        try:
            pid = self.spawn(argv, self.gate_env, fds=fds, stdin=config_read)
        except OSError:
            for sock in (control, gate_control) + (pair or ()):
                sock.close()
            raise Refused("incomplete install at %s (gate not startable)" % HERE.parent.parent)
        finally:
            os.close(config_read)
        gate_control.close()
        if pair:
            pair[1].close()
        os.write(config_write, json.dumps(self.gate_config).encode())
        os.close(config_write)
        self.gate_pid, self.control, self.buffer = pid, control, b""
        self.kit.add(pid)
        deadline = time.monotonic() + self.startup_timeout
        while True:
            for message in self._messages(0.05):
                if message.get("op") == "ready":
                    if pair:
                        self.start_monitor(pair[0])
                    return message
                if message.get("op") == "refused":
                    self._stop_gate()
                    if pair:
                        pair[0].close()
                    raise Refused(str(message.get("reason"))[:200])
            self.reap()
            if pid in self.exited or self.control is None or time.monotonic() > deadline:
                self._stop_gate()
                if pair:
                    pair[0].close()
                raise Refused("gate did not start")

    def start_monitor(self, sock):
        if not MONITOR.is_file():
            sock.close()
            raise Refused("incomplete install at %s" % HERE.parent.parent)
        argv = [self.python, "-I", "-B", str(MONITOR), "--fd", "3"]
        if self.heartbeat is not None:
            argv += ["--heartbeat", str(self.heartbeat)]
        if self.monitor_reader:
            argv += ["--reader-argv", json.dumps(self.monitor_reader)]
            if self.monitor_reader_timeout:
                argv += ["--reader-timeout", str(self.monitor_reader_timeout)]
        try:
            self.monitor_pid = self.spawn(argv, self.gate_env, fds=[sock.fileno()])
        finally:
            sock.close()
        self.kit.add(self.monitor_pid)

    def _stop_gate(self):
        if self.control is not None:
            self.control.close()
            self.control = None
        for pid in (self.gate_pid, self.monitor_pid):
            self._kill(pid)
        for pid in (self.gate_pid, self.monitor_pid):
            if pid is not None:
                self._wait(pid, 2.0)
        self.gate_pid = self.monitor_pid = None

    def _messages(self, timeout):
        """Gate control messages that arrive within timeout; EOF drops the channel."""
        if self.control is None:
            time.sleep(timeout)
            return []
        if not select.select([self.control], [], [], timeout)[0]:
            return []
        try:
            data = self.control.recv(65536)
        except OSError:
            data = b""
        if not data:
            self.control.close()
            self.control = None
            return []
        self.buffer += data
        found = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                found.append(message)
        return found

    def request(self, op, timeout=CONTROL_TIMEOUT):
        """One control round trip; a dead, restarting or silent gate answers gate_unavailable."""
        if self.control is None:
            return {"ok": False, "reason": "gate_unavailable"}
        try:
            self.control.sendall((json.dumps({"op": op}) + "\n").encode())
        except OSError:
            return {"ok": False, "reason": "gate_unavailable"}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.control is not None:
            for message in self._messages(0.05):
                if message.get("op") == "reply" and message.get("to") == op:
                    return message
                self.events.append(message)
        return {"ok": False, "reason": "gate_unavailable"}

    def wait_heard(self, timeout=HEARD_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.request("status", 5)
            if status.get("heard"):
                return status
            time.sleep(0.05)
        raise Refused("the monitor did not start")

    # -- terminal --------------------------------------------------------------------------------------
    def say(self, text):
        try:
            os.write(self.term_out, ("\r\n" + self.style(PREFIX + text) + "\r\n").encode())
        except OSError:
            pass

    def check(self):
        """TerminalControl's check: one verified continuation through the gate."""
        reply = self.request("continue")
        if reply.get("needs_read"):
            ran = False
            if self.reader is not None:
                try:
                    ran = bool(self.reader())
                except Exception:
                    ran = False
            status = self.request("status", 5)
            if status.get("running"):
                return True
            self.request("disarm", 5)
            self.say("still paused: %s" % (status.get("reason") or ("check_failed" if ran else "read_failed")))
            return False
        if reply.get("ok"):
            return True
        self.say("still paused: %s" % reply.get("reason"))
        return False

    def _handle(self, message):
        op = message.get("op")
        if op in ("paused", "latched"):
            if self.ui is not None:
                self.ui.pause()
            self.say(pause_message(message.get("reason"), message.get("fields"), self.key_name))
            if op == "latched":
                self._gate_down()
        elif op == "resumed" and self.ui is not None:
            self.ui.resume()

    def _gate_down(self, announce=True):
        """A dead, lost or silent gate: paused until a restarted gate passes a fresh check. A lost
        monitor is announced by the gate's own latched message when the gate is still alive."""
        if self.down_since is None:
            self.down_since = time.monotonic()
            if self.ui is not None and not self.ui.paused:
                self.ui.pause()
                if announce:
                    self.say(pause_message("gate_unavailable", {}, self.key_name))

    def _restart(self):
        if self.down_since is None or time.monotonic() - self.down_since < self.restart_delay:
            return
        if self.restarts >= self.restart_limit:
            return
        self.restarts += 1
        self._stop_gate()
        try:
            self.start_gate("restart")
            self.wait_heard()
            self.down_since = None
            self.say("gate restarted paused; press %s to request a fresh check" % self.key_name)
        except Refused:
            self._stop_gate()
            self.down_since = time.monotonic()

    # -- native --------------------------------------------------------------------------------------
    def _spawn_native(self, argv, env, cwd):
        master, slave = os.openpty()
        if os.isatty(self.term_in):
            try:
                termios.tcsetattr(slave, termios.TCSANOW, termios.tcgetattr(self.term_in))
                fcntl.ioctl(slave, termios.TIOCSWINSZ, fcntl.ioctl(self.term_in, termios.TIOCGWINSZ, b"\0" * 8))
            except (termios.error, OSError):
                pass
        try:
            pid = self.spawn(argv, env, cwd=cwd, tty_fd=slave, confine=True, pid_env=True)
        except OSError as error:
            os.close(master)
            raise Refused("cannot start the native: %s" % error.strerror) from None
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        return pid, master

    def _snapshot(self):
        """Without a subreaper: remember every descendant seen, so reparenting cannot hide it."""
        now = time.monotonic()
        if self.subreaper or now - self.snapshot_at < SNAPSHOT_EVERY:
            return
        self.snapshot_at = now
        table = process_table()
        for pid in descendants(os.getpid(), table) - self._kit_tree(table):
            self.tracked[pid] = table[pid][1]

    def _kit_tree(self, table):
        kit = {pid for pid in (self.gate_pid, self.monitor_pid) if pid is not None}
        for pid in list(kit):
            kit |= descendants(pid, table)
        return kit

    def sweep(self, include_kit=False, grace=SWEEP_GRACE):
        """End every boundary descendant (TERM, then KILL after grace); with include_kit also the
        gate and monitor trees. Returns how many processes were signalled."""
        signalled = set()
        for round_ in range(6):
            self.reap()
            table = process_table()
            victims = descendants(os.getpid(), table)
            victims |= {pid for pid, start in self.tracked.items() if table.get(pid, (None, None))[1] == start}
            if not include_kit:
                victims -= self._kit_tree(table)
            victims = {pid for pid in victims if pid not in self.exited and pid != os.getpid()}
            if not victims:
                break
            sig = signal.SIGTERM if round_ == 0 else signal.SIGKILL
            for pid in victims:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            signalled |= victims
            deadline = time.monotonic() + (grace if round_ == 0 else 0.2)
            while time.monotonic() < deadline:
                self.reap()
                table = process_table()
                if not any(pid in table and pid not in self.exited for pid in victims):
                    break
                time.sleep(0.02)
        self.reap()
        return len(signalled)

    def _winch(self):
        if self.master is None:
            return
        for fd in (self.term_in, self.term_out):
            try:
                size = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
            except OSError:
                continue
            try:
                fcntl.ioctl(self.master, termios.TIOCSWINSZ, size)
            except OSError:
                pass
            return

    def _relay(self, wake):
        """The single-threaded relay loop; returns the native's wait status."""
        ui, master = self.ui, self.master
        terminal_open = True
        while self.native_pid not in self.exited:
            readers = [master, wake] + ([self.term_in] if terminal_open else []) + \
                ([self.control] if self.control is not None else [])
            writers = [master] if self.to_native else []
            try:
                readable, writable, _ = select.select(readers, writers, [], 0.05)
            except InterruptedError:
                readable, writable = [], []
            if wake in readable:
                for number in os.read(wake, 512):
                    if number == signal.SIGWINCH:
                        self._winch()
                    elif number in FORWARDED:
                        self._kill(self.native_pid, number)
            self.reap()
            if master in writable and self.to_native:
                try:
                    written = os.write(master, self.to_native)
                    del self.to_native[:written]
                except BlockingIOError:
                    pass
                except OSError:
                    self.to_native.clear()
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except BlockingIOError:
                    data = None
                except OSError:
                    data = b""
                if data:
                    try:
                        os.write(self.term_out, data)
                    except OSError:
                        pass
            if terminal_open and self.term_in in readable:
                try:
                    data = os.read(self.term_in, 4096)
                except OSError:
                    data = b""
                if data:
                    ui.feed(data)
                else:
                    terminal_open = False
                    self._kill(self.native_pid, signal.SIGHUP)
            ui.tick()
            if self.control is not None and self.control in readable:
                for message in self._messages(0):
                    self._handle(message)
            for message in self.events:
                self._handle(message)
            self.events = []
            if self.gate_pid in self.exited or self.control is None:
                self._gate_down()
            elif self.monitor_pid is not None and self.monitor_pid in self.exited:
                self._gate_down(announce=False)
            self._restart()
            self._snapshot()
        self._drain(master)
        return self.exited[self.native_pid]

    def _drain(self, master, timeout=0.2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and select.select([master], [], [], 0.02)[0]:
            try:
                data = os.read(master, 65536)
            except OSError:
                return
            if not data:
                return
            try:
                os.write(self.term_out, data)
            except OSError:
                return

    def run(self, argv, env, cwd=None):
        """The whole protected invocation; returns the native's wait status. Raises Refused before
        any native runs under the boundary."""
        if not os.isatty(self.term_in):
            self.close()
            raise Refused("protected mode needs an interactive terminal for continuation")
        self.subreaper = enable_subreaper()
        if sys.platform.startswith("linux") and not self.subreaper:
            self.close()
            raise Refused("cannot become the child subreaper of the invocation")
        wake_read, wake_write = os.pipe()
        for fd in (wake_read, wake_write):
            os.set_blocking(fd, False)
        handled = (signal.SIGCHLD, signal.SIGWINCH) + FORWARDED
        previous = {number: signal.getsignal(number) for number in handled}
        old_wake = signal.set_wakeup_fd(wake_write, warn_on_full_buffer=False)
        for number in handled:
            signal.signal(number, lambda *args: None)
        outer = None
        try:
            self.start_gate("startup")
            status = self.wait_heard()
            if self.preflight is not None:
                self.preflight(self)
                status = self.request("status", 10)
            if not status.get("running"):
                raise Refused("preflight: %s" % (status.get("reason") or "no fresh passing quota check"))
            self.native_pid, self.master = self._spawn_native(argv, env, cwd)
            os.set_blocking(self.master, False)

            def forward(data):
                self.to_native += data

            self.ui = TerminalControl(self.term_in, forward, self.say, self.check, self.trigger, self.key_name)
            outer = termios.tcgetattr(self.term_in)
            tty.setraw(self.term_in)
            self._winch()
            return self._relay(wake_read)
        finally:
            if outer is not None:
                termios.tcsetattr(self.term_in, termios.TCSADRAIN, outer)
            if self.native_pid is not None and self.native_pid not in self.exited:
                self._kill(self.native_pid, signal.SIGKILL)
                self._wait(self.native_pid, 2.0)
            self.close()
            signal.set_wakeup_fd(old_wake)
            for number, handler in previous.items():
                signal.signal(number, handler)
            os.close(wake_read)
            os.close(wake_write)

    def refresh(self, argv, env, timeout=30.0, cwd=None, stdout=None, stderr=None):
        """The pre-launch native refresh: ARGV runs confined, while a refresh-mode gate serves only
        the provider's exact CONNECT allowlist; returns its exit status, or None on timeout."""
        self.start_gate("refresh")
        try:
            try:
                pid = self.spawn(argv, env, cwd=cwd, stdout=stdout, stderr=stderr, confine=True)
            except OSError as error:
                raise Refused("cannot start the native refresh: %s" % error.strerror) from None
            status = self._wait(pid, timeout)
            if status is None:
                self._kill(pid)
                self._wait(pid, 2.0)
                return None
            return os.waitstatus_to_exitcode(status)
        finally:
            self.request("shutdown", 5)
            self._stop_gate()

    def close(self):
        """End of invocation: sweep boundary descendants, stop the gate and monitor, sweep again,
        release the port."""
        self.sweep()
        if self.control is not None:
            self.request("shutdown", 2)
        self._stop_gate()
        self.sweep(include_kit=True)
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        if self.holder is not None:
            self.holder.close()
            self.holder = None


def finish(status):
    """Leave the launcher as the native left: the same exit code, or the same signal."""
    if os.WIFSIGNALED(status):
        number = os.WTERMSIG(status)
        try:
            signal.signal(number, signal.SIG_DFL)
        except (OSError, ValueError):
            pass  # SIGKILL and SIGSTOP keep their default anyway
        os.kill(os.getpid(), number)
        return 128 + number
    return os.waitstatus_to_exitcode(status)
