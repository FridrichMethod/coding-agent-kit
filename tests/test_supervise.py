"""Protected-mode supervisor: port holder, PTY interposition, continuation, restart and sweep.

Pure parts run in process. The process cases run a real Supervisor (tests/fixtures/gate/
run_supervised.py) on a PTY the test owns as the user's terminal, with the fixture gate on
127.0.0.1 and a fake native; every process the case started is gone when it ends.
"""

import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty
import unittest


HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures/gate"
sys.path.insert(0, str(FIXTURES))
import fakes  # noqa: E402

supervise = fakes.load("kit_supervise", HERE.parent / "lib/coding-agent-kit/supervise.py")
F12 = b"\x1b[24~"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    table = supervise.process_table()
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/%d/stat" % pid, "rb") as stream:
                return stream.read().rsplit(b")", 1)[1].split()[0] != b"Z"
        except OSError:
            return False
    return pid in table


def kill_tree(pid):
    table = supervise.process_table()
    for victim in [pid, *supervise.descendants(pid, table)]:
        try:
            os.kill(victim, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class Pure(unittest.TestCase):
    def test_pause_message_is_deterministic(self):
        fields = {"account": "borrowed", "window": "five_hour", "remaining": 9.5, "reserve": 10,
                  "reset": "2026-11-12T18:00:00Z", "last_fresh": "2026-11-12T13:00:01Z"}
        text = supervise.pause_message("threshold", fields)
        self.assertEqual(text, supervise.pause_message("threshold", dict(reversed(list(fields.items())))))
        self.assertEqual(text, "quota reserve paused (threshold); account borrowed; window five_hour; "
                               "remaining 9.5% (reserve 10%); resets 2026-11-12T18:00:00Z; last fresh check "
                               "2026-11-12T13:00:01Z; press F12, then type the code shown and Enter, to request "
                               "a fresh check")
        self.assertEqual(supervise.pause_message("monitor_stale", {}, "F9"),
                         "quota reserve paused (monitor_stale); press F9, then type the code shown and Enter, "
                         "to request a fresh check")

    def test_capture_proxy(self):
        parse = fakes.gate.parse_proxy
        self.assertIsNone(supervise.capture_proxy({}, parse))
        self.assertEqual(supervise.capture_proxy({"https_proxy": "http://p.example:3128"}, parse), ("p.example", 3128))
        self.assertEqual(supervise.capture_proxy({"HTTPS_PROXY": "http://p:1", "https_proxy": "http://p:1"}, parse),
                         ("p", 1))
        for env in ({"HTTPS_PROXY": "http://p:1", "https_proxy": "http://q:1"}, {"HTTPS_PROXY": "https://p:1"}):
            with self.subTest(env=env), self.assertRaises(supervise.Refused):
                supervise.capture_proxy(env, parse)

    def test_descendants(self):
        table = {2: (1, "a"), 3: (2, "b"), 4: (3, "c"), 5: (1, "d"), 6: (5, "e")}
        self.assertEqual(supervise.descendants(2, table), {3, 4})
        self.assertEqual(supervise.descendants(1, table), {2, 3, 4, 5, 6})
        self.assertEqual(supervise.descendants(9, table), set())
        self.assertIn(os.getpid(), supervise.process_table())

    def test_the_native_gets_no_proxy_but_the_gate(self):
        env = {"HTTPS_PROXY": "http://proxy.example:3128", "https_proxy": "http://127.0.0.1:50001",
               "HTTP_PROXY": "http://127.0.0.1:50001/", "http_proxy": "http://127.0.0.1:50002",
               "ALL_PROXY": "socks5://proxy.example:1080", "all_proxy": "http://user:pw@127.0.0.1:50001",
               "NO_PROXY": "localhost", "PATH": "/bin"}
        self.assertEqual(supervise.native_environment(env, 50001),
                         {"https_proxy": "http://127.0.0.1:50001", "HTTP_PROXY": "http://127.0.0.1:50001/",
                          "NO_PROXY": "localhost", "PATH": "/bin"})
        self.assertEqual(env["HTTPS_PROXY"], "http://proxy.example:3128")  # the caller's mapping is untouched

    def test_finish_keeps_the_exit_code(self):
        self.assertEqual(supervise.finish(7 << 8), 7)
        self.assertEqual(supervise.finish(0), 0)


class Requests(unittest.TestCase):
    def test_a_late_reply_never_answers_a_newer_request(self):
        sup = supervise.Supervisor(["gate"], {})
        sup.control, peer = socket.socketpair()
        self.addCleanup(peer.close)
        sup.sent = 4  # request 4 timed out; its reply arrives late
        peer.sendall(b'{"op":"reply","to":"status","id":4,"state":"late"}\n{"op":"paused","reason":"x"}\n'
                     b'{"op":"reply","to":"status","id":5,"state":"current"}\n')
        self.assertEqual(sup.request("status", 2)["state"], "current")
        self.assertEqual(json.loads(peer.recv(4096)), {"op": "status", "id": 5})
        self.assertEqual(sup.events, [{"op": "paused", "reason": "x"}])
        peer.close()
        self.assertEqual(sup.request("status", 2), {"ok": False, "reason": "gate_unavailable"})
        self.assertIsNone(sup.control)
        self.assertEqual(sup.request("continue"), {"ok": False, "reason": "gate_unavailable"})


class Restart(unittest.TestCase):
    def test_restarts_are_limited_per_window_and_ask_for_a_relaunch_once(self):
        now = [1000.0]
        sup = supervise.Supervisor(["gate"], {}, restart_limit=2, restart_window=600, restart_delay=0.5,
                                   clock=lambda: now[0])
        said, started = [], []
        sup.say, sup._stop_gate, sup.wait_heard = said.append, lambda: None, lambda: {}
        sup.start_gate = started.append

        def gate_dies():
            sup._gate_down()
            now[0] += 1.0
            sup._restart()

        for _ in range(4):
            gate_dies()
        self.assertEqual(started, ["restart", "restart"])
        relaunch = [text for text in said if "relaunch" in text]
        self.assertEqual(len(relaunch), 1)
        self.assertIn("exit and relaunch to continue", relaunch[0])
        now[0] += 600
        sup._restart()
        self.assertEqual(started, ["restart"] * 3)
        self.assertIsNone(sup.down_since)


class Install(unittest.TestCase):
    def test_a_missing_gate_script_refuses_as_an_incomplete_install(self):
        sup = supervise.Supervisor([sys.executable, "-I", "-B", "/nonexistent/provider_gate.py"], {})
        with self.assertRaises(supervise.Refused) as raised:
            sup.start_gate()
        self.assertIn("incomplete install at", raised.exception.reason)
        self.assertIsNone(sup.holder)

    def test_a_missing_gate_core_refuses_as_an_incomplete_install(self):
        with tempfile.TemporaryDirectory() as directory:
            lib = Path(directory) / "lib/coding-agent-kit"
            lib.mkdir(parents=True)
            for name in ("supervise.py", "monitor.py"):
                shutil.copy(HERE.parent / "lib/coding-agent-kit" / name, lib / name)
            partial = fakes.load("kit_supervise_partial", lib / "supervise.py")
            sup = partial.Supervisor([sys.executable, "-I", "-B", str(FIXTURES / "provider_gate.py")], {})
            with self.assertRaises(partial.Refused) as raised:
                sup.start_gate()
            self.assertEqual(raised.exception.reason, "incomplete install at %s" % Path(directory).resolve())


class Sweep(unittest.TestCase):
    def test_the_sweep_compares_start_times_not_pid_numbers(self):
        sup = supervise.Supervisor(["gate"], {})
        table = supervise.process_table()
        sup.baseline = {pid: table[pid][1] for pid in supervise.descendants(os.getpid(), table)}
        sup.prepared = True
        child = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL)
        self.addCleanup(lambda: alive(child.pid) and os.kill(child.pid, signal.SIGKILL))
        sup.exited[child.pid] = 0  # an earlier process with this number was reaped
        self.assertEqual(sup.sweep(grace=0.5), 1)
        self.assertTrue(fakes_wait(lambda: not alive(child.pid)))


class Spawn(unittest.TestCase):
    def test_children_get_default_signal_dispositions_and_no_stray_descriptors(self):
        sup = supervise.Supervisor(["gate"], {})
        held = socket.socket()
        self.addCleanup(held.close)
        script = "kill -PIPE $$; exit 9"
        pid = sup.spawn(["/bin/sh", "-c", script], {"PATH": "/usr/bin:/bin"})
        status = sup._wait(pid, 10)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGPIPE, status)
        out_read, out_write = os.pipe()
        self.addCleanup(os.close, out_read)
        script = "for fd in 3 4 5 %d %d; do (: <&$fd) 2>/dev/null && echo open $fd; done; echo done" % (
            held.fileno(), out_read)
        pid = sup.spawn(["/bin/sh", "-c", script], {"PATH": "/usr/bin:/bin"}, stdout=out_write)
        os.close(out_write)
        self.assertEqual(os.waitstatus_to_exitcode(sup._wait(pid, 10)), 0)
        self.assertEqual(os.read(out_read, 4096), b"done\n")

    def test_exec_failure_and_hook_refusal(self):
        sup = supervise.Supervisor(["gate"], {}, confine=lambda port, argv: argv)
        with self.assertRaises(OSError):
            sup.spawn(["/nonexistent/native"], {})
        sup.confine = lambda port, argv: (_ for _ in ()).throw(supervise.Refused("no boundary here"))
        with self.assertRaises(supervise.Refused) as raised:
            sup.spawn(["/bin/sh", "-c", "exit 0"], {}, confine=True)
        self.assertEqual(raised.exception.reason, "no boundary here")


class Holder(unittest.TestCase):
    def test_holds_a_random_loopback_port_for_its_lifetime(self):
        extra = ("127.0.0.2",) if sys.platform.startswith("linux") else ("0.0.0.0",)
        holder = supervise.hold_port(extra)
        self.addCleanup(holder.close)
        self.assertTrue(supervise.PORTS[0] <= holder.port <= supervise.PORTS[1])
        self.assertEqual(holder.listeners[0].getsockname(), ("127.0.0.1", holder.port))
        self.assertEqual(len(holder.reserved), 1)
        client = socket.create_connection(("127.0.0.1", holder.port), timeout=2)
        client.close()
        for address in ("127.0.0.1",) + extra:
            for reuse in (0, 1):
                squatter = socket.socket()
                squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, reuse)
                try:
                    squatter.bind((address, holder.port))
                    squatter.listen(1)
                    result = "bound"
                except OSError as error:
                    result = error.errno
                finally:
                    squatter.close()
                with self.subTest(address=address, reuse=reuse):
                    self.assertEqual(result, errno.EADDRINUSE)
        other = supervise.hold_port()
        self.addCleanup(other.close)
        self.assertNotEqual(other.port, holder.port)

    def test_boundary_module_pairs_and_loopback_duplicates(self):
        extra = (("inet", "127.0.0.1"), ("inet6", "::1"), ("inet", "127.0.0.3")) \
            if sys.platform.startswith("linux") else (("inet", "127.0.0.1"), ("inet", "0.0.0.0"))
        holder = supervise.hold_port(extra)
        self.addCleanup(holder.close)
        self.assertEqual([sock.getsockname()[0] for sock in holder.reserved], [extra[-1][1]])
        squatter = socket.socket()
        self.addCleanup(squatter.close)
        with self.assertRaises(OSError) as raised:
            squatter.bind((extra[-1][1], holder.port))
        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)

    def test_a_taken_port_is_refused_when_pinned(self):
        taken = socket.socket()
        taken.bind(("127.0.0.1", 0))
        self.addCleanup(taken.close)
        with self.assertRaises(supervise.Refused):
            supervise.hold_port(port=taken.getsockname()[1])


class Control(unittest.TestCase):
    """The trusted-terminal parser on a PTY the test owns (ported from the PC5 research checks)."""

    def setUp(self):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.checks, self.forwarded, self.messages, self.allowed = [], [], [], True

        def check():
            self.checks.append(1)
            return self.allowed

        self.ui = supervise.TerminalControl(self.slave, self.forwarded.append, self.messages.append, check)
        self.addCleanup(os.close, self.master)
        self.addCleanup(os.close, self.slave)

    def send(self, data):
        os.write(self.master, data)
        while select.select([self.slave], [], [], 0.05)[0]:
            self.ui.feed(os.read(self.slave, 4096))

    def code(self):
        return re.search(r"type ([0-9a-f]{8}) then", self.messages[-1]).group(1).encode()

    def test_input_typed_before_the_pause_is_flushed(self):
        os.write(self.master, F12)
        self.ui.pause()
        self.send(b"")
        self.assertIsNone(self.ui.challenge)
        self.assertEqual(self.checks, [])

    def test_running_input_goes_to_the_native(self):
        self.send(b"hello" + F12 + b"\r")
        self.assertEqual(b"".join(self.forwarded), b"hello" + F12 + b"\r")
        self.assertEqual(self.checks, [])

    def test_fresh_code_checks_once_and_old_codes_never_grant(self):
        self.ui.pause()
        self.send(F12)
        old = self.code()
        self.send(old + b"\r")
        self.assertEqual((len(self.checks), self.ui.paused), (1, False))
        self.ui.pause()
        self.send(old + b"\r")
        self.send(F12)
        self.assertNotEqual(self.code(), old)
        self.send(old + b"\r")
        self.assertEqual((len(self.checks), self.ui.paused), (1, True))

    def test_paste_and_repeated_trigger_never_confirm(self):
        self.ui.pause()
        self.send(F12)
        code = self.code()
        for part in (b"\x1b[20", b"0~", code + b"\r", b"\x1b[201~"):
            self.send(part)
        self.send(F12 * 6)
        self.assertEqual(self.checks, [])

    def test_failed_check_needs_another_deliberate_action(self):
        self.allowed = False
        self.ui.pause()
        self.send(F12)
        self.send(self.code() + b"\r")
        self.assertEqual((len(self.checks), self.ui.paused), (1, True))
        self.allowed = True
        self.send(b"\r")
        self.assertEqual(len(self.checks), 1)
        self.send(F12)
        self.send(self.code() + b"\r")
        self.assertEqual((len(self.checks), self.ui.paused), (2, False))

    def test_ctrl_c_cancels_the_challenge_and_reaches_the_native(self):
        self.ui.pause()
        self.send(F12)
        code = self.code()
        self.send(code[:3] + b"\x03" + code[3:] + b"\r")
        self.assertEqual(self.checks, [])
        self.assertIn(b"\x03", b"".join(self.forwarded))


class Terminal:
    """The user's terminal: a PTY whose slave is the supervisor's stdin, stdout and stderr."""

    def __init__(self, test):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        self.output, self.cursor = b"", 0
        test.addCleanup(self.close)

    def close(self):
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass

    def write(self, data):
        os.write(self.master, data)

    def pump(self, timeout):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                self.output += os.read(self.master, 65536)
            except OSError:
                pass

    def expect_all(self, patterns, timeout=15.0):
        """Wait until every pattern appears after the cursor, in any order; returns their matches."""
        regexes = [re.compile(p.encode() if isinstance(p, str) else p) for p in patterns]
        deadline = time.monotonic() + timeout
        while True:
            matches = [regex.search(self.output, self.cursor) for regex in regexes]
            if all(matches):
                self.cursor = max(match.end() for match in matches)
                return matches
            if time.monotonic() > deadline:
                raise AssertionError("not all of %r in terminal output: %r" % (patterns, self.output[-2000:]))
            self.pump(0.05)

    def expect(self, pattern, timeout=15.0):
        regex = re.compile(pattern.encode() if isinstance(pattern, str) else pattern)
        deadline = time.monotonic() + timeout
        while True:
            match = regex.search(self.output, self.cursor)
            if match:
                self.cursor = match.end()
                return match
            if time.monotonic() > deadline:
                raise AssertionError("no %r in terminal output: %r" % (pattern, self.output[-2000:]))
            self.pump(0.05)


class Supervised(unittest.TestCase):
    def setUp(self):
        self.upstream = fakes.FakeUpstream()
        self.addCleanup(self.upstream.close)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)

    def launch(self, **spec):
        self.terminal = Terminal(self)
        self.pids = self.directory / "pids"
        native = [sys.executable, "-I", "-B", str(FIXTURES / "fake_native.py")]
        spec = dict({"upstream_port": self.upstream.port, "pids": str(self.pids), "native": native}, **spec)
        spec["env"] = dict({"SPAWN_FILE": str(self.directory / "spawned"),
                            "NATIVE_MARKER": str(self.directory / "marker")}, **spec.get("env", {}))
        self.process = subprocess.Popen([sys.executable, "-I", "-B", str(FIXTURES / "run_supervised.py"),
                                         json.dumps(spec)], stdin=self.terminal.slave, stdout=self.terminal.slave,
                                        stderr=self.terminal.slave, start_new_session=True)
        self.addCleanup(self.reap)
        return self.process

    def reap(self):
        if self.process.poll() is None:
            kill_tree(self.process.pid)
        self.process.wait(10)

    def kit_pids(self):
        if self.pids.exists():
            self.pids.unlink()
        os.kill(self.process.pid, signal.SIGUSR1)
        deadline = time.monotonic() + 5
        while not self.pids.exists() or not self.pids.read_text():
            if time.monotonic() > deadline:
                raise AssertionError("no pid report")
            time.sleep(0.02)
        return json.loads(self.pids.read_text())

    def ready(self):
        return self.terminal.expect(r"READY pid=(\d+) env=(\S+) size=(\d+x\d+)")

    def command(self, text):
        self.terminal.write(text.encode() + b"\r")

    def wait_exit(self, timeout=15):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None:
            self.terminal.pump(0.05)
            if time.monotonic() > deadline:
                raise AssertionError("the supervisor did not exit: %r" % self.terminal.output[-1500:])
        return self.process.returncode

    def continue_(self):
        self.terminal.write(F12)
        code = self.terminal.expect(r"type ([0-9a-f]{8}) then").group(1)
        self.terminal.write(code + b"\r")

    def test_native_runs_on_its_own_pty_with_its_own_pid_variable_and_exit_code(self):
        self.launch(env={"HTTPS_PROXY": "http://proxy.example:3128", "ALL_PROXY": "socks5://proxy.example:1",
                         "HTTP_PROXY": "http://127.0.0.1:{port}"})
        ready = self.ready()
        pid, variable, size = int(ready.group(1)), ready.group(2).decode(), ready.group(3).decode()
        self.assertEqual(variable, str(pid))
        self.assertNotEqual(pid, self.process.pid)
        self.assertEqual(size, "30x100")
        self.command("request")
        self.terminal.expect(r"STATUS 200")
        port = self.kit_pids()[2]
        for name, value in (("HTTPS_PROXY", "<unset>"), ("ALL_PROXY", "<unset>"),
                            ("HTTP_PROXY", "http://127.0.0.1:%d" % port)):
            self.command("env " + name)
            self.terminal.expect(re.escape("ENV %s=%s" % (name, value)))
        self.command("exit 7")
        self.assertEqual(self.wait_exit(), 7)

    def test_signals_and_window_size_reach_the_native(self):
        self.launch()
        self.ready()
        fcntl.ioctl(self.terminal.master, termios.TIOCSWINSZ, struct.pack("HHHH", 41, 132, 0, 0))
        os.kill(self.process.pid, signal.SIGWINCH)
        self.terminal.expect(r"WINCH 41x132")
        os.kill(self.process.pid, signal.SIGTERM)
        self.terminal.expect(r"TERM")
        self.assertEqual(self.wait_exit(), 42)

    def test_a_native_killed_by_a_signal_ends_the_launcher_the_same_way(self):
        self.launch()
        self.ready()
        self.command("kill %d" % signal.SIGKILL)
        self.assertEqual(self.wait_exit(), -signal.SIGKILL)

    def test_confine_hook_runs_in_the_child_before_exec(self):
        self.launch(confine="wrap")
        confined = int(self.terminal.expect(r"CONFINED (\d+)").group(1))
        self.ready()
        self.assertEqual(confined, self.kit_pids()[2])
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)

    def test_a_hook_that_closes_every_descriptor_still_reports(self):
        self.launch(confine="close")
        self.terminal.expect(r"CONFINED \d+")
        self.ready()
        self.command("exit 5")
        self.assertEqual(self.wait_exit(), 5)
        (self.directory / "marker").unlink()
        self.launch(confine="close-refuse")
        self.terminal.expect(r"protected mode refused: boundary enforcement failed in the fixture")
        self.assertEqual(self.wait_exit(), 3)
        self.assertFalse((self.directory / "marker").exists())
        # Seen before the relay starts (a refusal) or only at its exit status 127, by timing.
        self.launch(confine="close-missing")
        self.terminal.expect(r"cannot start the native: No such file or directory")
        self.assertIn(self.wait_exit(), (3, 127))
        self.assertFalse((self.directory / "marker").exists())

    def test_confine_refusal_starts_no_native(self):
        self.launch(confine="refuse")
        self.terminal.expect(r"protected mode refused: boundary_refused_fixture")
        self.assertEqual(self.wait_exit(), 3)
        self.assertFalse((self.directory / "marker").exists())

    def test_startup_refusal_starts_no_native(self):
        self.launch(store_bearer="fixture-unknown-bearer")
        self.terminal.expect(r"protected mode refused: http_401")
        self.assertEqual(self.wait_exit(), 3)
        self.assertFalse((self.directory / "marker").exists())
        self.assertEqual(self.upstream.requests("/model"), [])

    def spawn(self, mode):
        self.command("spawn " + mode)
        self.terminal.expect(r"SPAWNED")
        grandchild = int((self.directory / "spawned").read_text())
        self.addCleanup(lambda: alive(grandchild) and os.kill(grandchild, signal.SIGKILL))
        self.assertTrue(alive(grandchild))
        return grandchild

    def test_orphaned_session_members_and_snapshot_seen_descendants_are_swept(self):
        self.launch()
        self.ready()
        orphaned = self.spawn("nosid")  # reparented at once, still in the native's session
        detached = self.spawn("slow")  # setsid, but seen under its lingering parent first
        gate, monitor, _ = self.kit_pids()
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)
        for pid in (orphaned, detached, gate, monitor):
            self.assertTrue(fakes_wait(lambda: not alive(pid)), "pid %d outlived the invocation" % pid)

    @unittest.skipUnless(sys.platform.startswith("linux"),
                         "macOS has no child subreaper: a descendant that calls setsid and is orphaned "
                         "between two snapshots escapes the sweep (a documented gap)")
    def test_quickly_detached_descendants_are_swept(self):
        self.launch()
        self.ready()
        grandchild = self.spawn("quick")
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)
        self.assertTrue(fakes_wait(lambda: not alive(grandchild)), "the detached grandchild outlived the invocation")

    def test_a_native_self_suspend_or_stop_does_not_hang_the_terminal(self):
        self.launch()
        self.ready()
        self.command("suspend")  # its group is orphaned, so the kernel discards the SIGTSTP
        self.terminal.expect(r"AFTER SUSPEND")
        self.command("stop")  # the launcher continues a stopped native at once
        self.terminal.expect(r"AFTER STOP")
        self.command("request")
        self.terminal.expect(r"STATUS 200")
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)

    def test_processes_started_before_the_supervisor_are_not_swept(self):
        bystander_file = self.directory / "bystander"
        self.launch(bystander=str(bystander_file))
        self.ready()
        bystander = int(bystander_file.read_text())
        self.addCleanup(kill_tree, bystander)
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)
        self.assertTrue(alive(bystander))

    def test_launcher_death_ends_the_gate_monitor_and_native(self):
        self.launch()
        native = int(self.ready().group(1))
        gate, monitor, _ = self.kit_pids()
        os.kill(self.process.pid, signal.SIGKILL)
        self.process.wait(10)
        for pid in (gate, monitor, native):
            self.assertTrue(fakes_wait(lambda: not alive(pid)), "pid %d outlived the launcher" % pid)

    def test_pause_shows_the_message_and_only_a_fresh_trusted_continuation_resumes(self):
        self.launch(next_poll=1.0)
        self.ready()
        self.command("request")
        self.terminal.expect(r"STATUS 200")
        self.upstream.used = {"five_hour": 97.5, "weekly": 20.0}
        self.terminal.expect(r"quota reserve paused \(threshold\); account A; window five_hour; remaining 2.5% "
                             r"\(reserve 5%\); press F12")
        self.command("request")
        self.terminal.expect(r"STATUS 400")
        self.terminal.write(b"0123abcd\r")  # a guess typed without the trigger goes to the native
        self.terminal.write(F12)
        self.terminal.expect(r"type ([0-9a-f]{8}) then")
        self.terminal.write(b"ffffffff\r")
        self.terminal.expect(r"code did not match")
        self.continue_()
        self.terminal.expect(r"still paused: threshold")
        self.upstream.used = {"five_hour": 10.0, "weekly": 20.0}
        time.sleep(1.5)  # polls see the reset; they never resume
        self.command("request")
        self.terminal.expect(r"STATUS 400")
        self.continue_()
        self.terminal.expect(r"fresh quota check passed")
        self.command("request")
        self.terminal.expect(r"STATUS 200")
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)

    def test_gate_death_keeps_the_port_and_a_restarted_gate_starts_paused(self):
        self.launch(restart_delay=1.5)
        self.ready()
        gate, monitor, port = self.kit_pids()
        os.kill(gate, signal.SIGKILL)
        self.terminal.expect(r"paused \((gate_unavailable|monitor_eof|launcher_eof)\)")
        self.command("request")  # waits in the held listener's backlog until the restarted gate refuses it
        for address in ("127.0.0.1", "::1") if socket.has_ipv6 else ("127.0.0.1",):
            squatter = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET)
            try:
                squatter.bind((address, port))
                squatter.listen(1)
                result = "bound"
            except OSError as error:
                result = error.errno
            finally:
                squatter.close()
            if result != errno.EADDRNOTAVAIL:  # a host without ::1
                self.assertEqual(result, errno.EADDRINUSE, "a squatter bound %s during the gap" % address)
        self.terminal.expect_all([r"gate restarted paused", r"STATUS 400"])
        new_gate, new_monitor, new_port = self.kit_pids()
        self.assertEqual(new_port, port)
        self.assertNotIn(new_gate, (gate, None))
        self.assertTrue(fakes_wait(lambda: not alive(monitor)))
        self.assertEqual(self.upstream.requests("/model"), [])
        self.command("request")
        self.terminal.expect(r"STATUS 400")
        self.continue_()
        self.terminal.expect(r"fresh quota check passed")
        self.command("request")
        self.terminal.expect(r"STATUS 200")
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)

    def test_monitor_loss_latches_and_restarts_the_gate_paused(self):
        self.launch()
        self.ready()
        gate, monitor, _ = self.kit_pids()
        os.kill(monitor, signal.SIGKILL)
        self.terminal.expect(r"paused \(monitor_eof\)")
        self.terminal.expect(r"gate restarted paused")
        self.command("request")
        self.terminal.expect(r"STATUS 400")
        self.continue_()
        self.terminal.expect(r"fresh quota check passed")
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)

    def test_a_stalled_monitor_latches_and_restarts_the_gate_paused(self):
        self.launch()
        self.ready()
        gate, monitor, _ = self.kit_pids()
        os.kill(monitor, signal.SIGSTOP)
        self.addCleanup(lambda: alive(monitor) and os.kill(monitor, signal.SIGKILL))
        self.terminal.expect(r"paused \(monitor_stale\)")
        self.terminal.expect(r"gate restarted paused")
        self.assertTrue(fakes_wait(lambda: not alive(gate) and not alive(monitor)))
        self.command("request")
        self.terminal.expect(r"STATUS 400")
        self.continue_()
        self.terminal.expect(r"fresh quota check passed")
        self.command("request")
        self.terminal.expect(r"STATUS 200")
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)

    def test_refresh_runs_confined_behind_the_exact_tunnel_only(self):
        echo = fakes.EchoServer()
        self.addCleanup(echo.close)
        client = [sys.executable, "-I", "-B", str(FIXTURES / "refresh_client.py")]
        allow = {"auth.fixture:443": ["127.0.0.1", echo.port]}
        self.launch(confine="wrap", native=None, connect_allow=allow, refresh=client + ["auth.fixture:443"])
        self.terminal.expect(r"REFRESH 0")
        self.assertEqual(self.wait_exit(), 0)
        self.assertEqual(bytes(echo.received), b"refresh\n")
        self.assertEqual(self.upstream.connections, [])
        self.launch(confine="wrap", native=None, connect_allow=allow, refresh=client + ["other.fixture:443"])
        self.terminal.expect(r"REFRESH 4")
        self.assertEqual(self.wait_exit(), 1)
        # The documented wiring: refresh, then run on the same supervisor. The run gate's provider
        # lists the auth host too, and still no tunnel opens while the native runs.
        received = bytes(echo.received)
        self.launch(confine="wrap", connect_allow=allow, refresh=client + ["auth.fixture:443"])
        self.terminal.expect(r"REFRESH 0")
        self.ready()
        self.command("connect auth.fixture:443")
        self.terminal.expect(r"CONNECT 403")
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)
        self.assertEqual(bytes(echo.received), received + b"refresh\n")

    def test_continuation_with_the_gate_down_stays_paused(self):
        self.launch(restart_delay=60)
        self.ready()
        gate, monitor, _ = self.kit_pids()
        os.kill(gate, signal.SIGKILL)
        self.terminal.expect(r"paused \(gate_unavailable\)")
        self.continue_()
        self.terminal.expect(r"still paused: gate_unavailable")
        self.command("exit 0")
        self.assertEqual(self.wait_exit(), 0)
        self.assertTrue(fakes_wait(lambda: not alive(monitor)))


def fakes_wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


if __name__ == "__main__":
    unittest.main()
