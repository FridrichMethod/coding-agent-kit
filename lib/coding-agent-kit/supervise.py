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
    Supervisor(...).refresh(...)   before run(): the D1 pre-launch native refresh, confined, behind
                                   a refresh-mode gate that serves only the exact CONNECT targets
                                   (no startup or restart gate serves a CONNECT).
    Supervisor(...).confined(...)  one confined command with the live gate as its only exit (an
                                   adapter's in-boundary preflight or continuation read).

The confine hook is confine(port, argv) -> argv, called in the forked child after setsid and the
PTY setup and before exec: it applies the platform boundary in process (Linux) and/or returns the
argv to exec (macOS sandbox-exec), or raises to refuse; the refusal reaches the launcher and no
native runs. The boundary modules' confine(gate_port, writable_paths, provider) returns an argv
prefix, so an adapter passes lambda port, argv: module.confine(port, paths, name) + argv. The hook
may close every descriptor above 2. On Linux the launcher is a child subreaper, so detached
descendants stay its own and the end-of-invocation sweep finds them. Everywhere the sweep also
ends every process still in the session of the native or of a confined command (the launcher
starts each in a session of its own), so an orphaned descendant that never called setsid is found
on macOS too; there a descendant that calls setsid is found only by periodic snapshots, which one
that detaches between two snapshots escapes.

Boundary children get no proxy but the gate: native_environment() drops every proxy variable that
does not name the gate. The caller's own proxy, the one the native would use for HTTPS
(capture_proxy()), belongs to the gate's upstream connections.

Nothing the launcher starts outside the boundary resolves through a name the boundary could replace.
The gate and monitor start in / with only gate_env; their interpreter is the caller's choice
(python=), and an adapter passes interpreter() after checking with code_sources() and unreachable()
that neither it, its prefixes, its module path nor the kit lies in the boundary's reach. Without
/proc (macOS) the process table comes from ps run by absolute path (system_command(PS): a
candidate is used only when root alone can change every name on the way to it), in /, with a fixed
environment; a host with neither refuses. launcher_path() pins the launcher's own PATH to the system
directories for the protected run, so that no later command run by name resolves in a directory of
the caller's PATH.

Job control: the native leads its own session on the launcher's PTY, so its process group is
orphaned. A SIGTSTP it sends itself (a TUI's own suspend key) is discarded by the kernel, a native
that stops itself with SIGSTOP is continued at once, and the launcher itself is never suspended:
in raw mode Ctrl-Z reaches the native as a key.

Continuation is a trusted-terminal action: after a pause, the trigger key (F12 unless the adapter
chooses another) shows a fresh one-use code; typing it and Enter asks the gate for a fresh check.
Input typed before the pause is flushed, a bracketed paste never confirms, native output never
reaches this parser, and nothing but terminal input starts a check.
"""

import collections
import contextlib
import ctypes
import errno
import fcntl
import json
import mmap
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import stat
import subprocess
import sys
import termios
import time
import tty
from urllib.parse import urlsplit


HERE = Path(os.path.realpath(__file__)).parent
ROOT = HERE.parent.parent
MONITOR, GATE_CORE = HERE / "monitor.py", HERE / "gate.py"
F12 = b"\x1b[24~"
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
PORTS = (49152, 60999)  # inside both the Linux and the macOS ephemeral ranges
STARTUP_TIMEOUT = 60.0
HEARD_TIMEOUT = 10.0
CONTROL_TIMEOUT = 20.0
# A continuation's reply comes after the provider's check: for Claude Code a store read (macOS
# security, up to 10 s) and two reads of up to 10 s each, so the launcher waits longer for it.
CONTINUE_TIMEOUT = 40.0
RESTART_LIMIT, RESTART_WINDOW, RESTART_DELAY = 5, 600.0, 0.5  # at most 5 gate restarts per 10 minutes
SWEEP_GRACE = 1.0
SWEEP_LIMIT = 5.0  # the end-of-invocation sweep keeps ending processes for this long at most
SNAPSHOT_EVERY = 1.0
PR_SET_CHILD_SUBREAPER = 36
PREFIX = "[coding-agent-kit] "
LOOPBACK = ("127.0.0.1", "::1")
PROXY_VARIABLES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
# The variables a native reads for its HTTPS connections, group by group in precedence order (both
# spellings of a name form one group): Codex (reqwest) falls back from HTTPS_PROXY to ALL_PROXY,
# Claude Code from HTTPS_PROXY to HTTP_PROXY and then to ALL_PROXY (its OAuth client, which sends
# the bearer, keeps the environment proxy when only ALL_PROXY is set). Without a provider either
# fallback counts, so the gate never goes direct where one native would use a proxy.
PROXY_ORDER = {
    "codex": (("HTTPS_PROXY", "https_proxy"), ("ALL_PROXY", "all_proxy")),
    "claude": (("HTTPS_PROXY", "https_proxy"), ("HTTP_PROXY", "http_proxy"), ("ALL_PROXY", "all_proxy")),
    None: (("HTTPS_PROXY", "https_proxy"), ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")),
}
NO_PROXY_VARIABLES = ("NO_PROXY", "no_proxy")
NO_PROXY_ENTRY = re.compile(r"[a-z0-9._-]{1,253}")  # the names gate.bypasses can exempt
# What a forked child reports through a shared anonymous page, which survives a confine hook that
# closes every descriptor: the hook is running, exec is next, the hook refused, exec failed.
HOOK, EXEC, REFUSED, FAILED = b"H", b"X", b"R", b"E"
REPORT = 1024
FORWARDED = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT)
# Python ignores these at startup and an ignored disposition survives exec; children get defaults.
RESTORED = tuple(getattr(signal, name) for name in ("SIGPIPE", "SIGXFSZ", "SIGXFZ") if hasattr(signal, name))
PROC = "/proc"
PS = ("/bin/ps", "/usr/bin/ps")  # macOS and Linux; used only when system_command() trusts it
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
PS_ENV = {"PATH": SYSTEM_PATH, "LC_ALL": "C"}
CODE_PROBE = ("import json, sys; print(json.dumps([sys.prefix, sys.exec_prefix, sys.base_prefix, "
              "sys.base_exec_prefix, sys.path]))")
CODE_PROBE_TIMEOUT = 10.0


class Refused(Exception):
    """Protected mode refuses this invocation before any native ran under the boundary."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def capture_proxy(env, parse, provider=None):
    """The caller's proxy for the gate's upstream connections, captured once at startup: None (the
    gate connects directly) or (host, port, no_proxy), the gate configuration's proxy value.

    parse is gate.parse_proxy. The proxy is the one the PROVIDER's native ("codex", "claude", or
    None for either) would use for HTTPS (PROXY_ORDER): the first group with a value set, whose set
    values must agree. An unsupported value refuses instead of going direct, naming the variable but
    never its value. no_proxy holds one entry list per set spelling of NO_PROXY (gate.bypasses).
    Boundary children never get the proxy: run() and confined() pass them native_environment(),
    whose only proxy is the gate."""
    for names in PROXY_ORDER[provider]:
        found = [(name, env[name]) for name in names if env.get(name)]
        if not found:
            continue
        if len({value for _, value in found}) > 1:
            raise Refused("%s differ" % " and ".join(name for name, _ in found))
        name, value = found[0]
        try:
            host, port = parse(value)
        except (ValueError, TypeError):
            raise Refused("unsupported %s (only a plain http://HOST:PORT proxy)" % name) from None
        return host, port, no_proxy_lists(env)
    return None


def no_proxy_lists(env):
    """One tuple of lower-case entries per set spelling of NO_PROXY, in NO_PROXY_VARIABLES order,
    split on commas only, as Codex splits them, or ("*",) for a value of exactly "*". An entry that
    gate.bypasses would not exempt a host by (a port, a range, "*.NAME", "*" among other entries, two
    names in one entry) is dropped, which only keeps that host on the proxy; a set spelling whose
    entries are all dropped still counts."""
    lists = []
    for name in NO_PROXY_VARIABLES:
        text = env.get(name)
        if text == "*":
            lists.append(("*",))
        elif text:
            entries = (entry.strip() for entry in text.lower().split(","))
            lists.append(tuple(entry for entry in entries if NO_PROXY_ENTRY.fullmatch(entry))[:1024])
    return tuple(lists)


def native_environment(env, port):
    """ENV for a boundary child: every proxy variable is dropped unless it names the gate itself
    (http://127.0.0.1:PORT), because the launcher's proxy belongs to the gate alone."""
    result = dict(env)
    for name in PROXY_VARIABLES:
        value = result.get(name)
        if value is None:
            continue
        try:
            parts = urlsplit(value)
            gate = (parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port == port
                    and parts.username is None and parts.path in ("", "/") and not parts.query)
        except ValueError:
            gate = False
        if not gate:
            del result[name]
    return result


class Holder:
    """The gate port: listening loopback sockets plus reservations, held until close()."""

    def __init__(self, port, listeners, reserved):
        self.port, self.listeners, self.reserved = port, listeners, reserved

    def close(self):
        for sock in self.listeners + self.reserved:
            sock.close()
        self.listeners, self.reserved = [], []


def _reservation(entry):
    """(family, address) of an extra holder address: "ADDRESS" or ("inet" | "inet6", "ADDRESS")."""
    if isinstance(entry, (tuple, list)):
        kind, address = entry
        return (socket.AF_INET6 if kind == "inet6" else socket.AF_INET), str(address)
    return (socket.AF_INET6 if ":" in entry else socket.AF_INET), entry


def hold_port(extra=(), port=None, attempts=64):
    """Hold one random port on loopback (and the extra addresses) for the whole invocation.

    extra takes plain addresses or the boundary module's ("inet" | "inet6", address) pairs; the
    loopback addresses are already held by the listeners and are skipped."""
    extra = [item for item in map(_reservation, extra) if item[1] not in LOOPBACK]
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
            for family, address in extra:
                sock = socket.socket(family, socket.SOCK_STREAM)
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


def within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def resolution(path, hops=40):
    """Every absolute name that resolving PATH looks up, in order: each leading directory and,
    through each link, the names of its target, ending with the real path. A link anywhere on the
    way can be replaced when its directory is writable, so every name counts, not only the real
    path. Like the kernel: physical "..", relative and absolute targets, at most 40 links."""
    path = str(path)
    names, current = [], "/"
    parts = list(Path(path if os.path.isabs(path) else os.path.join(os.getcwd(), path)).parts[1:])
    while parts:
        part = parts.pop(0)
        if part == "..":
            current = os.path.dirname(current)
            continue
        name = os.path.join(current, part)
        names.append(name)
        try:
            target = Path(os.readlink(name)) if hops and os.path.islink(name) else None
        except OSError:
            target = None
        if target is None:
            current = name
            continue
        hops -= 1
        parts[:0] = target.parts[1:] if target.is_absolute() else target.parts
        current = "/" if target.is_absolute() else current
    return names


def system_command(candidates):
    """The first of CANDIDATES (absolute paths) that only root can change, or None: / and every name
    its resolution looks up belong to root, none but links is writable by group or other, and the
    command is an executable regular file."""
    for path in candidates:
        try:
            for name in ["/"] + resolution(path):
                info = os.lstat(name)
                if info.st_uid != 0 or (not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022):
                    break
            else:
                info = os.stat(path)
                if stat.S_ISREG(info.st_mode) and info.st_mode & 0o111:
                    return path
        except OSError:
            continue
    return None


def has_proc():
    return os.path.isdir(os.path.join(PROC, "self"))


@contextlib.contextmanager
def launcher_path():
    """The launcher's own PATH while boundary processes may live: a command it runs by name outside
    the boundary never resolves in a directory of the caller's PATH, which the boundary may be able
    to write. Boundary children keep the environment the adapter passes them."""
    saved = os.environ.get("PATH")
    os.environ["PATH"] = SYSTEM_PATH
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved


def interpreter():
    """The Python for kit processes outside the boundary, as a real path: the base installation of
    the running one, so a virtual environment's site-packages and .pth files, which a venv in the
    workspace puts in reach of the boundary, are never processed outside it."""
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


def code_sources(python, env, kit_dirs):
    """(what, path, holds code) for everything a kit process outside the boundary loads code from:
    PYTHON, its prefixes, the module path it starts with as `PYTHON -I -B` (site-packages and what
    their .pth files add included) and KIT_DIRS. Refused when PYTHON cannot report them."""
    try:
        done = subprocess.run([python, "-I", "-B", "-c", CODE_PROBE], env=env, cwd="/", stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=CODE_PROBE_TIMEOUT)
        *prefixes, entries = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        prefixes = entries = None
    if not (isinstance(entries, list) and len(prefixes) == 4
            and all(isinstance(path, str) and os.path.isabs(path) for path in prefixes + entries)):
        raise Refused("the Python at %s did not report where it loads code from" % python)
    return ([("the Python", python, False)] + [("the Python prefix", path, False) for path in prefixes]
            + [("the Python module path entry", path, True) for path in entries]
            + [("the kit directory", str(path), True) for path in kit_dirs])


def unreachable(sources, writable_paths, hint):
    """Refused (reason ending in HINT) when a code source (code_sources), or a name on the way to
    it, lies inside a writable path, or a writable path lies inside a directory of code."""
    for what, path, holds in sources:
        names, real = resolution(path), os.path.realpath(path)
        for root in writable_paths:
            if any(within(name, root) for name in names):
                raise Refused("%s %s lies inside %s, which the boundary can write%s" % (what, path, root, hint))
            if holds and within(root, real):
                raise Refused("%s, which the boundary can write, lies inside %s %s%s" % (root, what, path, hint))


def proc_entry(pid):
    """(ppid, start) of PID from /proc, or None when it is not there."""
    try:
        with open(os.path.join(PROC, str(pid), "stat"), "rb") as stream:
            fields = stream.read().rsplit(b")", 1)[1].split()
        return int(fields[1]), fields[19].decode()
    except (OSError, IndexError, ValueError):
        return None


def process_table():
    """{pid: (ppid, start)} of every visible process; start tells a reused pid apart. Without /proc
    it runs the system ps by absolute path, or returns {} when there is none (prepare() refuses
    such a host)."""
    table = {}
    if has_proc():
        for name in os.listdir(PROC):
            if name.isdigit():
                entry = proc_entry(name)
                if entry is not None:
                    table[int(name)] = entry
        return table
    command = system_command(PS)
    if command is None:
        return table
    try:
        ps = subprocess.Popen([command, "-A", "-o", "pid=", "-o", "ppid=", "-o", "lstart="], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd="/",
                              env=dict(PS_ENV))
    except OSError:
        return table
    try:
        out = ps.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        ps.kill()
        ps.communicate()
        return table
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2].strip())
    table.pop(ps.pid, None)  # ps lists itself, as a child of the caller that is gone once read
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


def eligible_message(fields, key="F12"):
    """The deterministic line for a pause whose latest quota check passes: only a continuation is
    missing. Nothing resumes until the user asks."""
    fields = fields or {}
    parts = ["quota reserve: the latest quota check passes"]
    if fields.get("account") is not None:
        parts.append("account " + _value(fields["account"]))
    if fields.get("last_fresh") is not None:
        parts.append("last fresh check " + _value(fields["last_fresh"]))
    parts.append("press %s, then type the code shown and Enter, to continue" % key)
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

    gate_argv: the provider's gate entry ([python3, -I, -B, script]): its last word is the provider's
        gate script, and gate.py's options follow it. A missing script, gate.py or monitor.py refuses
        with "incomplete install at ROOT".
    gate_config: the JSON object written to the stdin of a startup or restart gate (gate.py documents
        the core keys).
    refresh_config: the JSON object for the refresh-mode gate of refresh() (default gate_config); the
        provider's connect_allow belongs here, since only a refresh gate serves a CONNECT.
    python: the interpreter of the monitor (default sys.executable). The gate and the monitor run
        outside the boundary: an adapter passes interpreter() here and as gate_argv's first word
        once unreachable() has accepted its code_sources().
    gate_env: the whole environment of the gate and the monitor (default empty). Nothing of the
        launcher's environment reaches them otherwise, so a CA bundle named by SSL_CERT_FILE or
        SSL_CERT_DIR is not used unless passed here; the gate then refuses an upstream it cannot
        verify.
    monitor_reader: None (the gate polls) or the reader argv the monitor runs for each poll. The
        monitor runs outside the boundary, so this command must confine itself before it starts
        anything that talks to the provider.
    reader: callable() -> bool that drives one fresh in-boundary read when a continuation needs it.
    preflight: callable(supervisor) run after the first heartbeat, before the native; may raise
        Refused. The gate must then report running.
    confine: callable(port, argv) -> argv, run in the native's child before exec.
    pid_env: name of an environment variable set to the native's own pid in its child.
    umask: None, or the file mode creation mask the native and every confined command get, set in
        the child just before exec (codex-as passes the caller's: its launcher runs under 077); the
        monitor's poll readers get it through --reader-umask. The gate, the monitor and the
        launcher keep the launcher's own, so the kit's files stay private.
    style: callable(text) -> text for the launcher's terminal messages (the kit colour policy).
    restart_limit, restart_window: at most this many gate restarts per window seconds; past it the
        launcher asks once for a relaunch and stays paused until the window frees a restart.
    """

    def __init__(self, gate_argv, gate_config, *, python=sys.executable, monitor_reader=None,
                 monitor_reader_timeout=None, reader=None, preflight=None, confine=None, pid_env=None,
                 holder_extra=(), gate_env=None, trigger=F12, key_name="F12", restart_limit=RESTART_LIMIT,
                 restart_window=RESTART_WINDOW, restart_delay=RESTART_DELAY, startup_timeout=STARTUP_TIMEOUT,
                 terminal=(0, 1), heartbeat=None, style=None, refresh_config=None, umask=None,
                 clock=time.monotonic):
        if umask is not None and (isinstance(umask, bool) or not isinstance(umask, int) or not 0 <= umask <= 0o777):
            raise ValueError("umask must be None or a mode from 0 through 0o777")
        self.gate_argv, self.gate_config = list(gate_argv), dict(gate_config)
        self.refresh_config = dict(gate_config if refresh_config is None else refresh_config)
        self.python, self.monitor_reader, self.reader = python, monitor_reader, reader
        self.monitor_reader_timeout, self.heartbeat = monitor_reader_timeout, heartbeat
        self.preflight, self.confine, self.pid_env, self.umask = preflight, confine, pid_env, umask
        self.holder_extra, self.gate_env = tuple(holder_extra), dict(gate_env or {})
        self.trigger, self.key_name = trigger, key_name
        self.restart_limit, self.restart_delay, self.startup_timeout = restart_limit, restart_delay, startup_timeout
        self.restart_window, self.clock = restart_window, clock
        self.term_in, self.term_out = terminal
        self.style = style or (lambda text: text)
        self.holder = None
        self.gate_pid = self.monitor_pid = self.native_pid = None
        self.control, self.buffer, self.events, self.sent = None, b"", [], 0
        self.exited, self.starts, self.prepared, self.baseline = {}, {}, False, {}
        self.watched, self.sessions = set(), {}  # spawned pids; boundary session id -> leader start
        self.restarts, self.down_since, self.gave_up = collections.deque(), None, False
        self.subreaper, self.tracked, self.snapshot_at = False, {}, 0.0
        self.ui, self.master, self.to_native = None, None, bytearray()

    # -- children ----------------------------------------------------------------------------------
    def spawn(self, argv, env, *, fds=(), stdin=None, stdout=None, stderr=None, cwd=None, tty_fd=None,
              confine=False, pid_env=False):
        """fork and exec ARGV in a new session with fds at 3.. and nothing else inherited.

        tty_fd becomes the controlling terminal and stdio. confine applies the confine hook in the
        child just before exec, and then the supervisor's umask when it has one; pid_env sets the
        pid variable to the child's own pid. Returns the pid; raises Refused when the hook refuses
        and OSError when exec fails.

        The child reports on a shared anonymous page, because the confine hook may close every
        descriptor above 2 (the boundary modules do); the close-on-exec pipe only wakes the parent.
        When the hook closed the pipe, an exec failure after a passing hook can only show as exit
        status 127; start_failure() then names it."""
        read_end, write_end = os.pipe()
        devnull = os.open(os.devnull, os.O_RDWR)
        page = mmap.mmap(-1, REPORT)
        if tty_fd is not None:
            stdio = [tty_fd] * 3
        else:
            stdio = [devnull if fd is None else fd for fd in (stdin, stdout, stderr)]
        hook, port, variable = self.confine if confine else None, self.holder.port if self.holder else None, \
            self.pid_env if pid_env else None
        mask = self.umask if confine else None
        pid = os.fork()
        if pid == 0:
            kind = FAILED
            try:
                os.close(read_end)
                for number in RESTORED:
                    signal.signal(number, signal.SIG_DFL)
                os.setsid()
                if tty_fd is not None:
                    fcntl.ioctl(tty_fd, termios.TIOCSCTTY, 0)
                _child_fds(fds, stdio, write_end)
                child_env = dict(env)
                if variable:
                    child_env[variable] = str(os.getpid())
                if cwd is not None:
                    os.chdir(cwd)
                final = list(argv)
                if hook is not None:
                    kind = REFUSED
                    page[0:1] = HOOK
                    final = list(hook(port, final))
                    kind = FAILED
                if mask is not None:
                    os.umask(mask)
                page[0:1] = EXEC
                os.execve(final[0], final, child_env)
            except BaseException as error:
                try:
                    number = getattr(error, "errno", None)
                    detail = getattr(error, "reason", None) or (os.strerror(number) if number else str(error)) or \
                        type(error).__name__
                    data = str(detail).encode("utf-8", "replace")[:REPORT - 3]
                    page[1:3] = len(data).to_bytes(2, "big")
                    page[3:3 + len(data)] = data
                    page[0:1] = kind
                finally:
                    os._exit(3 if kind == REFUSED else 127)
        self.exited.pop(pid, None)  # a reused pid is a new process
        self.watched.add(pid)
        os.close(write_end)
        os.close(devnull)
        while os.read(read_end, 4096):
            pass
        os.close(read_end)
        # EOF: exec ran, the child died, or the hook closed the pipe and is still at work.
        status = None
        while page[0:1] == HOOK and status is None:
            done, raw = os.waitpid(pid, os.WNOHANG)
            if done:
                status = raw
            else:
                time.sleep(0.002)
        stage = page[0:1]
        if stage == EXEC:
            if status is not None:
                self.watched.discard(pid)
                self.exited[pid] = status
            elif confine:  # a boundary child: its session is swept at the end
                self.sessions[pid] = process_table().get(pid, (None, None))[1]
            if hook is None:
                page.close()  # the pipe stayed open until exec closed it: exec ran
            else:
                self.starts[pid] = page
            return pid
        if status is None:
            os.waitpid(pid, 0)
        self.watched.discard(pid)
        length = int.from_bytes(page[1:3], "big")
        text = page[3:3 + length].decode("utf-8", "replace") or "the child ended before exec"
        page.close()
        if stage in (REFUSED, HOOK):
            raise Refused(text if stage == REFUSED else "the boundary could not be applied")
        raise OSError(errno.ENOEXEC, text)

    def start_failure(self, pid):
        """Why PID, which passed the confine hook, did not exec, or None (also once it did)."""
        page = self.starts.pop(pid, None)
        if page is None:
            return None
        try:
            if page[0:1] != FAILED:
                return None
            return page[3:3 + int.from_bytes(page[1:3], "big")].decode("utf-8", "replace")
        finally:
            page.close()

    def reap(self):
        """Reap every ended child; only the ones this supervisor spawned keep a status (the
        subreaper's orphans need none, and their numbers may come back as new processes)."""
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            if pid in self.watched:
                self.watched.discard(pid)
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

    def prepare(self):
        """Become the child subreaper (Linux, where protected mode needs it) and hold the gate port;
        returns the port. Idempotent; refresh(), confined() and run() call it."""
        if not self.prepared:
            if not has_proc() and system_command(PS) is None:
                raise Refused("cannot list the invocation's processes (no %s and no ps in a root-owned system "
                              "directory)" % PROC)
            self.subreaper = enable_subreaper()
            if sys.platform.startswith("linux") and not self.subreaper:
                raise Refused("cannot become the child subreaper of the invocation")
            table = process_table()
            self.baseline = {pid: table[pid][1] for pid in descendants(os.getpid(), table)}
            self.prepared = True
        return self.hold()

    def start_gate(self, mode="startup"):
        """Start a gate on the held port and wait for ready; raises Refused on a startup refusal.
        A refresh gate needs no monitor, but the run after it does: a missing monitor refuses
        before the pre-launch refresh rotates any credential."""
        needed = [Path(self.gate_argv[-1]), GATE_CORE, MONITOR]
        if not all(path.is_file() for path in needed):
            raise Refused("incomplete install at %s" % ROOT)
        self.prepare()
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
            pid = self.spawn(argv, self.gate_env, fds=fds, stdin=config_read, cwd="/")
        except OSError:
            for sock in (control, gate_control) + (pair or ()):
                sock.close()
            raise Refused("incomplete install at %s (gate not startable)" % ROOT)
        finally:
            os.close(config_read)
        gate_control.close()
        if pair:
            pair[1].close()
        os.write(config_write, json.dumps(self.refresh_config if mode == "refresh" else self.gate_config).encode())
        os.close(config_write)
        self.gate_pid, self.control, self.buffer = pid, control, b""
        deadline = time.monotonic() + self.startup_timeout
        while True:
            for message in self._messages(0.05):
                if message.get("op") == "ready":
                    if message.get("port") != self.holder.port:
                        self._stop_gate()
                        if pair:
                            pair[0].close()
                        raise Refused("the gate reported another port")
                    if pair:
                        try:
                            self.start_monitor(pair[0])
                        except (OSError, Refused) as error:
                            self._stop_gate()
                            raise Refused(getattr(error, "reason", None) or "the monitor did not start") from None
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
            raise Refused("incomplete install at %s" % ROOT)
        argv = [self.python, "-I", "-B", str(MONITOR), "--fd", "3"]
        if self.heartbeat is not None:
            argv += ["--heartbeat", str(self.heartbeat)]
        if self.monitor_reader:
            argv += ["--reader-argv", json.dumps(self.monitor_reader)]
            if self.monitor_reader_timeout:
                argv += ["--reader-timeout", str(self.monitor_reader_timeout)]
            if self.umask is not None:  # a poll reader is a confined command too
                argv += ["--reader-umask", "%03o" % self.umask]
        try:
            self.monitor_pid = self.spawn(argv, self.gate_env, fds=[sock.fileno()], cwd="/")
        finally:
            sock.close()

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
        """One control round trip; a dead, restarting or silent gate answers gate_unavailable (with
        "timeout": true when the gate is still connected but did not answer in time)."""
        if self.control is None:
            return {"ok": False, "reason": "gate_unavailable"}
        self.sent += 1
        number = self.sent
        try:
            self.control.sendall((json.dumps({"op": op, "id": number}) + "\n").encode())
        except OSError:
            self.control.close()
            self.control = None
            return {"ok": False, "reason": "gate_unavailable"}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.control is not None:
            for message in self._messages(0.05):
                if message.get("op") == "reply":
                    if message.get("to") == op and message.get("id") == number:
                        return message
                    continue  # the late reply to a request that already timed out
                self.events.append(message)
        if self.control is None:
            return {"ok": False, "reason": "gate_unavailable"}
        return {"ok": False, "reason": "gate_unavailable", "timeout": True}

    def post(self, op):
        """Send one control request without waiting; its reply is dropped as a late one."""
        if self.control is None:
            return
        self.sent += 1
        try:
            self.control.sendall((json.dumps({"op": op, "id": self.sent}) + "\n").encode())
        except OSError:
            self.control.close()
            self.control = None

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
        self.say("requesting a fresh quota check")
        reply = self.request("continue", CONTINUE_TIMEOUT)
        if reply.get("timeout"):
            self.post("disarm")  # a check still running must not resume after "still paused" below
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
        elif op == "notice" and (self.ui is None or self.ui.paused):
            # The pause line is out of date: a fresh check now passes, or no longer does. It
            # neither flushes input nor resumes, so a code being typed stays valid.
            if message.get("eligible") is True:
                self.say(eligible_message(message.get("fields"), self.key_name))
            else:
                self.say(pause_message(message.get("reason"), message.get("fields"), self.key_name))
        elif op == "resumed" and self.ui is not None:
            if self.ui.paused:  # a check the launcher had stopped waiting for passed after all
                self.say("fresh quota check passed; model requests resume")
            self.ui.resume()

    def _gate_down(self, announce=True):
        """A dead, lost or silent gate: paused until a restarted gate passes a fresh check. A lost
        monitor is announced by the gate's own latched message when the gate is still alive."""
        if self.down_since is None:
            self.down_since = self.clock()
            if self.ui is not None and not self.ui.paused:
                self.ui.pause()
                if announce:
                    self.say(pause_message("gate_unavailable", {}, self.key_name))

    def _restart(self):
        now = self.clock()
        if self.down_since is None or now - self.down_since < self.restart_delay:
            return
        while self.restarts and now - self.restarts[0] >= self.restart_window:
            self.restarts.popleft()
        if len(self.restarts) >= self.restart_limit:
            if not self.gave_up:
                self.gave_up = True
                self.say("the gate stopped %d times within %d minutes and stays down for now; exit and relaunch "
                         "to continue" % (self.restart_limit, round(self.restart_window / 60)))
            return
        self.gave_up = False
        self.restarts.append(now)
        self._stop_gate()
        try:
            self.start_gate("restart")
            self.wait_heard()
            self.down_since = None
            self.say("gate restarted paused; press %s to request a fresh check" % self.key_name)
        except Refused:
            self._stop_gate()
            self.down_since = self.clock()

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
        """The run loop's check, once per SNAPSHOT_EVERY: forget the boundary sessions seen empty
        (_prune_sessions) and, without a subreaper, remember every descendant seen, so reparenting
        cannot hide it."""
        now = time.monotonic()
        if now - self.snapshot_at < SNAPSHOT_EVERY:
            return
        self.snapshot_at = now
        if self.subreaper:
            self._prune_sessions()
            return
        table = process_table()
        self._prune_sessions(table)
        self.tracked = {pid: start for pid, start in self.tracked.items() if table.get(pid, (None, None))[1] == start}
        for pid in descendants(os.getpid(), table) - self._kit_tree(table):
            self.tracked[pid] = table[pid][1]

    def _kit_tree(self, table):
        kit = {pid for pid in (self.gate_pid, self.monitor_pid) if pid is not None}
        for pid in list(kit):
            kit |= descendants(pid, table)
        return kit

    def _prune_sessions(self, table=None):
        """Forget every boundary session seen empty: its leader is gone and neither its process
        group nor a member is left, or its number is now another process's (start time differs).
        TABLE is a process_table(); without one the leaders are read from /proc. A session id is
        never a new process's pid while the session or its group has members, so a session checked
        this way by the run loop and in every sweep round is signalled only while it has existed
        without a break, and once forgotten its number is never signalled again."""
        if not self.sessions or (table is not None and not table):  # an empty table is a failed ps
            return
        if table is None and not has_proc():
            table = process_table()
            if not table:
                return
        in_use = None
        for sid, start in list(self.sessions.items()):
            entry = table.get(sid) if table is not None else proc_entry(sid)
            if entry is not None:
                if entry[1] != start:
                    del self.sessions[sid]
                continue
            try:
                os.killpg(sid, 0)
                continue
            except PermissionError:
                continue
            except ProcessLookupError:
                pass
            if in_use is None:
                in_use = set()
                pids = table if table is not None else [int(name) for name in os.listdir(PROC) if name.isdigit()]
                for pid in pids:
                    try:
                        in_use.add(os.getsid(pid))
                    except OSError:
                        continue
            if sid not in in_use:
                del self.sessions[sid]

    def _session_members(self, table):
        """Processes still in a boundary child's session. A session id is never a new process's
        pid while the session has members; one whose number now leads a session of another
        process (start time differs) is skipped, and sweep() forgets the sessions seen empty
        before it looks (_prune_sessions)."""
        live = {sid for sid, start in self.sessions.items() if table.get(sid, (None, start))[1] == start}
        found = set()
        if live:
            for pid in table:
                try:
                    if os.getsid(pid) in live:
                        found.add(pid)
                except OSError:
                    continue
        return found

    def _signal_session_groups(self, table, sig):
        """Signal whole process groups of the boundary sessions; returns how many were signalled.
        A process that forks and exits at once is gone before any snapshot can name it, but the
        group its child inherits stays: every boundary session's own group (its id is the session
        id, which _prune_sessions keeps only while the session has existed without a break), and on
        Linux every group a member of such a session is in, read from /proc as that member is. An
        exited member counts while it is a zombie: a fork-and-exit chain in a group of its own (one
        setpgid, as a job-control shell does for `cmd &`) leaves no live member long enough to be
        read, but under the subreaper each member that exits stays the launcher's zombie, in that
        group, until reap(). A zombie holds its group number, and a group never spans sessions, so
        only boundary processes are signalled. A group seen with zombie members only is signalled
        but not counted, so it alone keeps no sweep round going. A chain that changes its group on
        every fork, or one in a group of its own without /proc (macOS), can still outrun this."""
        live = {sid for sid, start in self.sessions.items() if table.get(sid, (None, start))[1] == start}
        own = {os.getpgrp(), os.getsid(0)}
        live -= own
        done, counted = set(), set()

        def end(group, count=True):
            if group in done or group in own:
                return
            try:
                os.killpg(group, sig)
                done.add(group)
                if count:
                    counted.add(group)
            except (ProcessLookupError, PermissionError):
                pass

        for sid in live:
            end(sid)
        if live and has_proc():
            running = {}  # group -> whether a member that is not a zombie was seen in it
            for name in os.listdir(PROC):
                if not name.isdigit():
                    continue
                try:
                    with open(os.path.join(PROC, name, "stat"), "rb") as stream:
                        fields = stream.read().rsplit(b")", 1)[1].split()
                    state, group, session = fields[0], int(fields[2]), int(fields[3])
                except (OSError, IndexError, ValueError):
                    continue
                if session in live:
                    running[group] = running.get(group, False) or state != b"Z"
            for group, member in running.items():
                end(group, count=member)
        return len(counted)

    def sweep(self, include_kit=False, grace=SWEEP_GRACE, limit=SWEEP_LIMIT):
        """End every boundary descendant (TERM, then KILL after grace, round after round until none
        is left or LIMIT seconds passed); with include_kit also the gate and monitor trees. A
        descendant is a process below the launcher, one a snapshot saw below it (same start time),
        or a member of a boundary child's session, whose process groups are also signalled whole
        (_signal_session_groups). Processes that were already the launcher's descendants before
        prepare() are left alone. Processes are told apart by start time, never by pid alone.
        Returns how many processes (or groups) were signalled."""
        signalled, groups = set(), 0
        if not self.prepared:
            return 0
        stop, round_ = time.monotonic() + limit, 0
        while True:
            self.reap()
            table = process_table()
            self._prune_sessions(table)
            found = descendants(os.getpid(), table) | self._session_members(table)
            found |= {pid for pid, start in self.tracked.items() if table.get(pid, (None, None))[1] == start}
            if not include_kit:
                found -= self._kit_tree(table)
            victims = {pid: table[pid][1] for pid in found if pid in table and pid != os.getpid()
                       and self.baseline.get(pid, ()) != table[pid][1]}
            sig = signal.SIGTERM if round_ == 0 else signal.SIGKILL
            hit = self._signal_session_groups(table, sig)
            groups += hit
            if not victims and not hit:
                break
            for pid in victims:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            signalled |= set(victims)
            if time.monotonic() >= stop:
                break
            deadline = min(stop, time.monotonic() + (grace if round_ == 0 else 0.2))
            while time.monotonic() < deadline:
                self.reap()
                table = process_table()
                if not any(table.get(pid, (None, None))[1] == start for pid, start in victims.items()):
                    break
                time.sleep(0.02)
            round_ += 1
        self.reap()
        return len(signalled) + groups

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
            self._unstop()
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

    def _unstop(self):
        """Continue a native that stopped itself: nothing could resume it on its orphaned PTY."""
        try:
            pid, status = os.waitpid(self.native_pid, os.WNOHANG | os.WUNTRACED)
        except ChildProcessError:
            return
        if pid and os.WIFSTOPPED(status):
            self._kill(self.native_pid, signal.SIGCONT)
        elif pid:
            self.watched.discard(pid)
            self.exited[pid] = status

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
        wake_read = wake_write = old_wake = outer = None
        previous = {}
        try:
            if not os.isatty(self.term_in):
                raise Refused("protected mode needs an interactive terminal for continuation")
            self.prepare()
            self.start_gate("startup")
            status = self.wait_heard()
            if self.preflight is not None:
                self.preflight(self)
                status = self.request("status", 10)
            if not status.get("running"):
                raise Refused("preflight: %s" % (status.get("reason") or "no fresh passing quota check"))
            # From here on the launcher relays signals to the native instead of acting on them.
            wake_read, wake_write = os.pipe()
            for fd in (wake_read, wake_write):
                os.set_blocking(fd, False)
            handled = (signal.SIGCHLD, signal.SIGWINCH) + FORWARDED
            previous = {number: signal.getsignal(number) for number in handled}
            old_wake = signal.set_wakeup_fd(wake_write, warn_on_full_buffer=False)
            for number in handled:
                signal.signal(number, lambda *args: None)
            self.native_pid, self.master = self._spawn_native(argv, native_environment(env, self.holder.port), cwd)
            os.set_blocking(self.master, False)

            def forward(data):
                self.to_native += data

            self.ui = TerminalControl(self.term_in, forward, self.say, self.check, self.trigger, self.key_name)
            outer = termios.tcgetattr(self.term_in)
            tty.setraw(self.term_in)
            self._winch()
            status = self._relay(wake_read)
            failure = self.start_failure(self.native_pid)
            if failure:
                self.say("cannot start the native: %s" % failure)
            return status
        finally:
            if outer is not None:
                termios.tcsetattr(self.term_in, termios.TCSADRAIN, outer)
            if self.native_pid is not None and self.native_pid not in self.exited:
                self._kill(self.native_pid, signal.SIGKILL)
                self._wait(self.native_pid, 2.0)
            self.close()
            if wake_read is not None:
                signal.set_wakeup_fd(old_wake)
                for number, handler in previous.items():
                    signal.signal(number, handler)
                os.close(wake_read)
                os.close(wake_write)

    def confined(self, argv, env, timeout, cwd=None, stdout=None, stderr=None):
        """Run ARGV once under the confine hook, so that the gate port is its only exit: the D1
        pre-launch refresh, or an adapter's in-boundary preflight, poll or continuation read.
        Returns its exit code, or None when it ran past timeout (its process group is killed);
        raises Refused when the hook refuses or ARGV cannot start."""
        port = self.prepare()
        try:
            pid = self.spawn(argv, native_environment(env, port), cwd=cwd, stdout=stdout, stderr=stderr, confine=True)
        except OSError as error:
            raise Refused("cannot start %s: %s" % (os.path.basename(argv[0]), error.strerror or error)) from None
        status = self._wait(pid, timeout)
        if status is None:
            self._kill(pid)
            self._wait(pid, 2.0)
            self.start_failure(pid)
            return None
        failure = self.start_failure(pid)
        if failure:
            raise Refused("cannot start %s: %s" % (os.path.basename(argv[0]), failure))
        return os.waitstatus_to_exitcode(status)

    def refresh(self, argv, env, timeout=30.0, cwd=None, stdout=None, stderr=None):
        """The pre-launch native refresh: ARGV runs confined, while a refresh-mode gate (started
        with refresh_config) serves only the provider's exact CONNECT allowlist; returns its exit
        code, or None on timeout. Nothing the refresh started outlives it."""
        self.start_gate("refresh")
        try:
            return self.confined(argv, env, timeout, cwd=cwd, stdout=stdout, stderr=stderr)
        finally:
            self.request("shutdown", 5)
            self._stop_gate()
            self.sweep()

    def close(self):
        """End of invocation: sweep boundary descendants, stop the gate and monitor, sweep again,
        release the port."""
        self.sweep()
        if self.control is not None:
            self.request("shutdown", 2)
        self._stop_gate()
        self.sweep(include_kit=True)
        for page in self.starts.values():
            page.close()
        self.starts = {}
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
