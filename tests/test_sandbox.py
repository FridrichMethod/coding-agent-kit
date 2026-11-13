"""Protected-mode boundaries: the Linux Landlock and seccomp layer, the macOS Seatbelt profile.

Pure logic runs in process on every host: host refusal classification, the
Landlock ruleset plan, the seccomp program (evaluated by a small classic-BPF
interpreter over synthetic seccomp_data), the SBPL profile, interface address
parsing and the self-test verdict. Nothing in process ever confines the test
runner: every call of confine() runs in a child process.

Enforcement cases run the real boundary in a child against 127.0.0.1
listeners that the test owns outside it, and skip cleanly where the host
cannot carry it (Linux without Landlock ABI 6, seccomp or x86_64; a host
without /usr/bin/sandbox-exec). Every listener, fixture and temporary
directory is removed again.
"""

import errno
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
LINUX_MODULE = ROOT / "lib/coding-agent-kit/sandbox_linux.py"
MACOS_MODULE = ROOT / "lib/coding-agent-kit/sandbox_macos.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


linux = load("kit_sandbox_linux_under_test", LINUX_MODULE)
X86 = linux.SYSCALLS["x86_64"]
ARM = linux.SYSCALLS["aarch64"]
CLOEXEC, NONBLOCK = 0o2000000, 0o4000


def run_bpf(rows, nr, args=(), arch=X86.audit_arch):
    """Evaluate a classic-BPF seccomp program on one synthetic seccomp_data."""
    args = (tuple(args) + (0,) * 6)[:6]
    data = struct.pack("<IIQ6Q", nr, arch, 0, *[value & (2**64 - 1) for value in args])
    acc, pc = 0, 0
    for _ in range(len(rows) + 1):
        code, jt, jf, k = rows[pc]
        if code == linux.BPF_LD_W_ABS:
            acc = struct.unpack_from("<I", data, k)[0]
        elif code == linux.BPF_ALU_AND_K:
            acc &= k
        elif code == linux.BPF_RET_K:
            return k
        else:
            taken = {linux.BPF_JEQ_K: acc == k, linux.BPF_JGE_K: acc >= k, linux.BPF_JSET_K: bool(acc & k)}[code]
            pc += jt if taken else jf
        pc += 1
    raise AssertionError("the program did not return")


def decision(value):
    if value == linux.RET_ALLOW:
        return "allow"
    if value == linux.RET_KILL_PROCESS:
        return "kill"
    if value & 0xFFFF0000 != linux.RET_ERRNO:
        return hex(value)
    # errno.errorcode names 95 ENOTSUP on Linux; EOPNOTSUPP is the same value.
    return {errno.EOPNOTSUPP: "EOPNOTSUPP"}.get(value & 0xFFFF, errno.errorcode[value & 0xFFFF])


def linux_facts(**changes):
    facts = {"system": "Linux", "machine": "x86_64", "bits": 64, "wsl": False, "abi": 8, "abi_errno": None,
             "seccomp_errno": None}
    facts.update(changes)
    return facts


class LinuxHost(unittest.TestCase):
    """Every NL refusal condition is an explicit Refusal with a message, before anything changes."""

    def refused(self, kind, **changes):
        with self.assertRaises(linux.Refusal) as caught:
            linux.assess(linux_facts(**changes))
        self.assertEqual(caught.exception.kind, kind)
        message = str(caught.exception)
        self.assertTrue(message and message.isascii() and message.isprintable(), message)
        return message

    def test_supported_hosts(self):
        self.assertEqual(linux.assess(linux_facts()), linux.Host(abi=8, arch="x86_64"))
        self.assertEqual(linux.assess(linux_facts(abi=6)), linux.Host(abi=6, arch="x86_64"))
        self.assertEqual(linux.assess(linux_facts(abi=10)), linux.Host(abi=10, arch="x86_64"))

    def test_other_systems_and_architectures(self):
        self.assertIn("Linux", self.refused("platform", system="Darwin"))
        self.refused("architecture", bits=32)
        self.assertIn("aarch64", self.refused("architecture", machine="aarch64"))
        self.refused("architecture", machine="riscv64")
        self.refused("architecture", machine="i686", bits=32)

    def test_wsl(self):
        self.assertIn("WSL", self.refused("wsl", wsl=True))

    def test_landlock_missing_disabled_or_too_old(self):
        self.assertIn("disabled", self.refused("landlock", abi=None, abi_errno=errno.EOPNOTSUPP))
        self.assertIn("no Landlock", self.refused("landlock", abi=None, abi_errno=errno.ENOSYS))
        self.refused("landlock", abi=None, abi_errno=errno.EPERM)
        self.refused("landlock", abi=None, abi_errno=None)
        for abi in (1, 4, 5):
            with self.subTest(abi=abi):
                message = self.refused("landlock", abi=abi)
                self.assertIn("ABI %d" % abi, message)
                self.assertIn("ABI 6", message)

    def test_seccomp_unavailable(self):
        self.assertIn("seccomp", self.refused("seccomp", seccomp_errno=errno.ENOSYS))
        self.refused("seccomp", seccomp_errno=errno.EOPNOTSUPP)

    def test_order_follows_the_cheapest_explanation(self):
        self.refused("platform", system="Darwin", bits=32, abi=None)
        self.refused("architecture", machine="aarch64", wsl=True, abi=4)
        self.refused("wsl", wsl=True, abi=4, seccomp_errno=errno.ENOSYS)
        self.refused("landlock", abi=5, seccomp_errno=errno.ENOSYS)

    def test_probe_is_side_effect_free(self):
        before = sorted(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
        try:
            host = linux.probe()
        except linux.Refusal as refusal:
            self.assertIn(refusal.kind, linux.KINDS)
        else:
            self.assertGreaterEqual(host.abi, linux.MIN_ABI)
            self.assertIn(host.arch, linux.VALIDATED_ARCHES)
        if before is not None:
            self.assertEqual(sorted(os.listdir("/proc/self/fd")), before)
        socket.socket(socket.AF_UNIX).close()  # this process is not confined


class LinuxInputs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-sandbox-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_ports_and_providers(self):
        for port in (0, 65536, -1, True, "4100", None, 4100.0):
            with self.subTest(port=port), self.assertRaises(linux.Refusal) as caught:
                linux.check_inputs(port, [], "codex")
            self.assertEqual(caught.exception.kind, "input")
        for provider in ("", "Codex", "openai", None):
            with self.subTest(provider=provider), self.assertRaises(linux.Refusal) as caught:
                linux.check_inputs(4100, [], provider)
            self.assertEqual(caught.exception.kind, "input")
        for paths in ("/w", b"/w", [b"/w"], [None]):
            with self.subTest(paths=paths), self.assertRaises(linux.Refusal) as caught:
                linux.check_inputs(4100, paths, "codex")
            self.assertEqual(caught.exception.kind, "input")
        linux.check_inputs(1, [], "claude")
        linux.check_inputs(65535, ("/w",), "codex")

    def test_writable_rules_by_kind_with_the_devices(self):
        (self.base / "work").mkdir()
        (self.base / "config.json").write_text("{}")
        (self.base / "link").symlink_to(self.base / "work")
        rules = dict(linux.writable_rules([str(self.base / "work"), str(self.base / "config.json"),
                                           str(self.base / "link"), str(self.base / "work") + "/"]))
        self.assertEqual(rules.pop(str(self.base / "work")), linux.DIR_RIGHTS)
        self.assertEqual(rules.pop(str(self.base / "config.json")), linux.FILE_RIGHTS)
        expected = {}
        for device in linux.DEVICES:
            if os.path.exists(device):
                expected[os.path.realpath(device)] = linux.DIR_RIGHTS if os.path.isdir(device) else linux.FILE_RIGHTS
        self.assertEqual(rules, expected)
        self.assertIn("/dev/null", expected)

    def test_writable_rule_refusals(self):
        for path in ("relative", "", str(self.base / "missing"), "/", str(self.base / "x\0y")):
            with self.subTest(path=path), self.assertRaises(linux.Refusal) as caught:
                linux.writable_rules([path])
            self.assertEqual(caught.exception.kind, "path")
        (self.base / "root").symlink_to("/")
        with self.assertRaises(linux.Refusal):
            linux.writable_rules([str(self.base / "root")])

    def test_rights_never_cover_reads_execution_or_device_ioctl(self):
        unhandled = (1 << 0) | (1 << 2) | (1 << 3) | (1 << 15)
        self.assertEqual(linux.DIR_RIGHTS & unhandled, 0)
        self.assertEqual(linux.DIR_RIGHTS, 0b111111111110010)
        self.assertEqual(linux.FILE_RIGHTS, (1 << 1) | (1 << 14))

    def test_stdio_sockets_detected(self):
        first, second = socket.socketpair()
        reader, writer = os.pipe()
        try:
            fds = (first.fileno(), reader, writer, second.fileno(), 9999)
            self.assertEqual(linux.stdio_sockets(fds), [first.fileno(), second.fileno()])
        finally:
            first.close()
            second.close()
            os.close(reader)
            os.close(writer)


class LinuxRuleset(unittest.TestCase):
    RULES = (("/w", 1), ("/dev/null", 2))

    def test_abi8_plan(self):
        plan = linux.ruleset_plan(8, 4100, self.RULES)
        self.assertEqual(plan["handled_access_fs"], linux.DIR_RIGHTS)
        self.assertEqual(plan["handled_access_net"], linux.NET_BIND_TCP | linux.NET_CONNECT_TCP)
        self.assertEqual(plan["scoped"], linux.SCOPE_ABSTRACT_UNIX_SOCKET | linux.SCOPE_SIGNAL)
        self.assertEqual(plan["paths"], self.RULES)
        # Connect only to the gate port; bind only a kernel-chosen ephemeral port (listen() autobinds one anyway).
        self.assertEqual(plan["ports"], ((linux.NET_CONNECT_TCP, 4100), (linux.NET_BIND_TCP, 0)))
        self.assertEqual(plan["restrict_flags"], linux.RESTRICT_SELF_TSYNC)

    def test_tsync_only_from_abi8(self):
        for abi in (6, 7):
            self.assertEqual(linux.ruleset_plan(abi, 4100, self.RULES)["restrict_flags"], 0)
        self.assertEqual(linux.ruleset_plan(9, 4100, ())["restrict_flags"], linux.RESTRICT_SELF_TSYNC)

    def test_below_the_floor_refuses(self):
        with self.assertRaises(linux.Refusal):
            linux.ruleset_plan(5, 4100, self.RULES)


class LinuxSeccomp(unittest.TestCase):
    def check(self, nr, args, expected, arch="x86_64"):
        table = linux.SYSCALLS[arch]
        rows = linux.seccomp_program(arch)
        self.assertEqual(decision(run_bpf(rows, nr, args, arch=table.audit_arch)), expected, (nr, args))

    def test_program_shape(self):
        for arch in linux.SYSCALLS:
            rows = linux.seccomp_program(arch)
            self.assertLess(len(rows), 80)
            for code, jt, jf, _ in rows:
                self.assertIn(code, (linux.BPF_LD_W_ABS, linux.BPF_ALU_AND_K, linux.BPF_JEQ_K, linux.BPF_JGE_K,
                                     linux.BPF_JSET_K, linux.BPF_RET_K))
                self.assertTrue(0 <= jt <= 255 and 0 <= jf <= 255)
        with self.assertRaises(ValueError):
            linux.assemble([("jeq", 1, "far", None)] + [("ret", 0)] * 300 + [("label", "far"), ("ret", 0)])

    def test_tcp_sockets_allowed_with_flags(self):
        for family in (linux.AF_INET, linux.AF_INET6):
            for kind in (1, 1 | CLOEXEC | NONBLOCK, (1 << 32) | 1):
                for proto in (0, 6):
                    self.check(X86.socket, (family, kind, proto), "allow")

    def test_udp_raw_mptcp_sctp_and_other_families_denied(self):
        cases = [((2, 2, 0), "EPROTONOSUPPORT"), ((10, 2 | NONBLOCK, 17), "EPROTONOSUPPORT"),
                 ((2, 3, 1), "EPROTONOSUPPORT"), ((10, 3, 58), "EPROTONOSUPPORT"),
                 ((2, 1, 262), "EPROTONOSUPPORT"), ((10, 1, 262), "EPROTONOSUPPORT"),
                 ((2, 1, 132), "EPROTONOSUPPORT"), ((2, 5, 132), "EPROTONOSUPPORT"),
                 ((2, 1, 6 | (1 << 32)), "allow"), ((17, 3, 0), "EAFNOSUPPORT"), ((40, 1, 0), "EAFNOSUPPORT"),
                 ((44, 3, 0), "EAFNOSUPPORT"), ((16, 3, 9), "EAFNOSUPPORT"), ((16, 2, 15), "EAFNOSUPPORT"),
                 ((1, 1, 0), "EAFNOSUPPORT"), ((1, 2, 0), "EAFNOSUPPORT"), ((1, 5, 0), "EAFNOSUPPORT"),
                 ((0, 1, 0), "EAFNOSUPPORT")]
        for args, expected in cases:
            self.check(X86.socket, args, expected)

    def test_netlink_route_allowed(self):
        for kind in (2, 3, 3 | CLOEXEC):
            self.check(X86.socket, (16, kind, 0), "allow")

    def test_socketpair_only_unix_stream_and_seqpacket(self):
        for kind in (1, 5, 1 | CLOEXEC, 5 | NONBLOCK | CLOEXEC, (1 << 32) | 1):
            self.check(X86.socketpair, (1, kind, 0), "allow")
        # Datagram and raw (a datagram alias for AF_UNIX) pairs can sendto()/connect() outside sockets.
        for kind in (2, 3, 2 | CLOEXEC, 3 | NONBLOCK, (1 << 32) | 2, 0, 4, 6, 10):
            self.check(X86.socketpair, (1, kind, 0), "EPROTONOSUPPORT")
        for family in (2, 10, 16, 40):
            self.check(X86.socketpair, (family, 1, 0), "EAFNOSUPPORT")

    def test_fastopen_sends_denied(self):
        self.check(X86.sendto, (3, 0, 5, linux.MSG_FASTOPEN, 0, 0), "EOPNOTSUPP")
        self.check(X86.sendto, (3, 0, 5, linux.MSG_FASTOPEN | 0x40, 0, 0), "EOPNOTSUPP")
        self.check(X86.sendmmsg, (3, 0, 1, linux.MSG_FASTOPEN | 0x40, 0, 0), "EOPNOTSUPP")
        self.check(X86.sendmsg, (3, 0, linux.MSG_FASTOPEN, 0, 0, 0), "EOPNOTSUPP")
        self.check(X86.sendto, (3, 0, 5, 0x40, 0, 0), "allow")
        self.check(X86.sendmsg, (3, 0, 0x4000, 0, 0, 0), "allow")
        self.check(X86.sendmmsg, (3, 0, 1, 0, 0, 0), "allow")

    def test_tiocsti_denied_by_its_low_32_bits(self):
        self.check(X86.ioctl, (0, linux.TIOCSTI, 0), "EPERM")
        self.check(X86.ioctl, (5, (1 << 32) | linux.TIOCSTI, 0), "EPERM")  # the kernel ignores the high half
        for request in (0x5401, 0x5402, 0x5413, 0x540E, 0x541C, 0x5412 << 1):
            self.check(X86.ioctl, (0, request, 0), "allow")

    def test_io_uring_denied_everything_else_allowed(self):
        for nr in X86.io_uring:
            self.check(nr, (), "ENOSYS")
        for nr in (0, 1, 2, 42, 43, 49, 50, 59, 62, 435, 436, 444, 445, 446):
            self.check(nr, (2, 2, 0), "allow")

    def test_foreign_architectures_killed_and_x32_denied(self):
        rows = linux.seccomp_program("x86_64")
        for arch in (0x40000003, ARM.audit_arch, 0):
            self.assertEqual(decision(run_bpf(rows, 41, (2, 1, 0), arch=arch)), "kill")
        self.assertEqual(decision(run_bpf(rows, 41 | linux.X32_SYSCALL_BIT, (2, 1, 0))), "ENOSYS")
        self.assertEqual(decision(run_bpf(rows, 0xFFFFFFFF, ())), "ENOSYS")

    def test_aarch64_table(self):
        self.check(ARM.socket, (2, 2, 0), "EPROTONOSUPPORT", arch="aarch64")
        self.check(ARM.socket, (2, 1, 0), "allow", arch="aarch64")
        self.check(ARM.socket, (1, 1, 0), "EAFNOSUPPORT", arch="aarch64")
        self.check(ARM.socketpair, (1, 2, 0), "EPROTONOSUPPORT", arch="aarch64")
        self.check(ARM.sendto, (3, 0, 5, linux.MSG_FASTOPEN, 0, 0), "EOPNOTSUPP", arch="aarch64")
        self.check(ARM.ioctl, (0, linux.TIOCSTI, 0), "EPERM", arch="aarch64")
        self.check(ARM.io_uring[0], (), "ENOSYS", arch="aarch64")
        self.check(X86.socket, (2, 2, 0), "allow", arch="aarch64")  # 41 is not socket() on aarch64
        rows = linux.seccomp_program("aarch64")
        self.assertEqual(decision(run_bpf(rows, ARM.socket, (2, 1, 0), arch=X86.audit_arch)), "kill")


# Run in a child: load the module by path, confine, then exec the command in argv.
RUNNER = r"""
import importlib.util, json, os, sys
spec = json.loads(sys.argv[1])
loader = importlib.util.spec_from_file_location("sandbox_under_test", spec["module"])
module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(module)
try:
    prefix = module.confine(spec["gate"], spec["writable"], spec["provider"])
except module.Refusal as refusal:
    print(json.dumps({"refused": refusal.kind, "message": str(refusal)}), flush=True)
    sys.exit(3)
argv = prefix + spec["argv"]
os.execv(argv[0], argv)
"""

# Attempts made from inside the boundary; each records "ok", a value or the errno name.
ACTORS = r"""
import ctypes, errno, fcntl, json, os, socket, subprocess, sys, termios, time
spec = json.loads(sys.argv[1])
out = {}

def attempt(name, action):
    try:
        out[name] = action() or "ok"
    except OSError as error:
        out[name] = errno.errorcode.get(error.errno, str(error.errno))

def tcp(host, port, family=socket.AF_INET, tag=b"inside"):
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect((host, port))
        sock.sendall(tag)
        return "connected"

def fastopen():
    with socket.socket() as sock:
        sock.sendto(b"tfo", 0x20000000, ("127.0.0.1", spec["other"]))

def udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b"udp", ("127.0.0.1", spec["udp"]))

def pair(kind):
    first, second = socket.socketpair(socket.AF_UNIX, kind)
    first.close()
    second.close()

def bind(port):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))
        return "bound"

def write(path):
    with open(path, "w") as handle:
        handle.write("x")

def tiocsti():
    master, slave = os.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSTI, b"x")
    finally:
        os.close(master)
        os.close(slave)

def io_uring():
    libc = ctypes.CDLL(None, use_errno=True)
    params = ctypes.create_string_buffer(120)
    if libc.syscall(ctypes.c_long(425), ctypes.c_long(1), params) < 0:
        raise OSError(ctypes.get_errno(), "io_uring_setup")
    return "created"

# First, before anything opens a descriptor that could take the inherited one's number.
attempt("inherited", lambda: os.write(spec["inherited"], b"x") and None)
attempt("gate", lambda: tcp("127.0.0.1", spec["gate"]))
attempt("other", lambda: tcp("127.0.0.1", spec["other"]))
if spec.get("other6"):
    attempt("other6", lambda: tcp("::1", spec["other6"], socket.AF_INET6))
attempt("fastopen", fastopen)
attempt("udp", udp)
attempt("mptcp", lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM, 262).close())
attempt("unix", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close())
attempt("unix_dgram", lambda: socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM).close())
attempt("dgram_pair", lambda: pair(socket.SOCK_DGRAM))
attempt("stream_pair", lambda: pair(socket.SOCK_STREAM))
attempt("bind_fixed", lambda: bind(spec["free"]))
attempt("bind_ephemeral", lambda: bind(0))
attempt("write_inside", lambda: write(os.path.join(spec["inside"], "written")))
attempt("mkdir_inside", lambda: os.mkdir(os.path.join(spec["inside"], "made")))
attempt("write_outside", lambda: write(os.path.join(spec["outside"], "written")))
attempt("mkdir_outside", lambda: os.mkdir(os.path.join(spec["outside"], "made")))
attempt("append_outside", lambda: open(spec["outside_file"], "a").close())
attempt("read_outside", lambda: open(spec["outside_file"]).read())
attempt("devnull", lambda: write("/dev/null"))
attempt("signal", lambda: os.kill(spec["outsider"], 0))
attempt("tiocsti", tiocsti)
attempt("io_uring", io_uring)
child =subprocess.run([sys.executable, "-I", "-B", "-c", spec["child"], sys.argv[1]], capture_output=True,
                       text=True, timeout=20, stdin=subprocess.DEVNULL)
out["child"] = child.stdout.strip()
# A double-forked, setsid grandchild that acts only after this root has exited.
root = os.getpid()
if os.fork() == 0:
    os.setsid()
    if os.fork():
        os._exit(0)
    null = os.open("/dev/null", os.O_RDWR)  # let go of the test's pipes, as a daemon does
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    os.close(null)
    late = {"root_gone": False}
    for _ in range(200):
        try:
            os.kill(root, 0)
        except ProcessLookupError:
            late["root_gone"] = True
            break
        time.sleep(0.05)
    try:
        tcp("127.0.0.1", spec["other"], tag=b"detached")
        late["other"] = "connected"
    except OSError as error:
        late["other"] = errno.errorcode.get(error.errno, str(error.errno))
    with open(os.path.join(spec["inside"], "detached.tmp"), "w") as handle:
        json.dump(late, handle)
    os.rename(os.path.join(spec["inside"], "detached.tmp"), os.path.join(spec["inside"], "detached.json"))
    os._exit(0)
print(json.dumps(out), flush=True)
"""

CHILD = r"""
import errno, json, socket, sys
spec = json.loads(sys.argv[1])
try:
    with socket.socket() as sock:
        sock.settimeout(3)
        sock.connect(("127.0.0.1", spec["other"]))
        sock.sendall(b"child")
    print("connected")
except OSError as error:
    print(errno.errorcode.get(error.errno, str(error.errno)))
"""


class Listener:
    """A TCP listener outside every boundary that records what each connection sent."""

    def __init__(self, family=socket.AF_INET, host="127.0.0.1"):
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.bind((host, 0))
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.received = []
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.done.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(2)
                try:
                    self.received.append(conn.recv(64))
                except OSError:
                    self.received.append(b"")

    def close(self):
        self.done.set()
        self.thread.join(5)
        self.sock.close()


def ipv6_loopback():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
        return True
    except OSError:
        return False


@unittest.skipUnless(sys.platform.startswith("linux"), "the Linux boundary needs Linux")
class LinuxEnforcement(unittest.TestCase):
    maxDiff = None
    """The real Landlock and seccomp boundary in a child, against listeners outside it."""

    @classmethod
    def setUpClass(cls):
        try:
            linux.probe()
        except linux.Refusal as refusal:
            raise unittest.SkipTest("this host cannot carry the Linux boundary: %s" % refusal)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-sandbox-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        for name in ("inside", "outside"):
            (self.base / name).mkdir()
        (self.base / "outside/kept").write_text("outside")
        self.gate = Listener()
        self.addCleanup(self.gate.close)
        self.other = Listener()
        self.addCleanup(self.other.close)
        self.other6 = Listener(socket.AF_INET6, "::1") if ipv6_loopback() else None
        if self.other6:
            self.addCleanup(self.other6.close)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.setblocking(False)
        self.addCleanup(self.udp.close)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.free = probe.getsockname()[1]
        self.outsider = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL)
        self.addCleanup(self.reap, self.outsider)

    @staticmethod
    def reap(process):
        process.kill()
        process.wait(10)

    def spec(self, argv, **changes):
        spec = {"module": str(LINUX_MODULE), "gate": self.gate.port, "writable": [str(self.base / "inside")],
                "provider": "codex", "argv": argv}
        spec.update(changes)
        return spec

    def run_child(self, spec, **options):
        return subprocess.run([sys.executable, "-I", "-B", "-c", RUNNER, json.dumps(spec)], capture_output=True,
                              text=True, timeout=60, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                              **options)

    def test_only_the_gate_port_and_the_writable_paths(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        try:
            actors = {"gate": self.gate.port, "other": self.other.port, "udp": self.udp.getsockname()[1],
                      "other6": self.other6.port if self.other6 else None, "free": self.free,
                      "inside": str(self.base / "inside"), "outside": str(self.base / "outside"),
                      "outside_file": str(self.base / "outside/kept"), "outsider": self.outsider.pid,
                      "inherited": writer, "child": CHILD}
            argv = [sys.executable, "-I", "-B", "-c", ACTORS, json.dumps(actors)]
            done = self.run_child(self.spec(argv), pass_fds=(writer,), stdin=subprocess.DEVNULL)
        finally:
            os.close(writer)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout.strip().splitlines()[-1])
        expected = {"gate": "connected", "other": "EACCES", "fastopen": errno.errorcode[errno.EOPNOTSUPP],
                    "udp": "EPROTONOSUPPORT",
                    "mptcp": "EPROTONOSUPPORT", "unix": "EAFNOSUPPORT", "unix_dgram": "EAFNOSUPPORT",
                    "dgram_pair": "EPROTONOSUPPORT", "stream_pair": "ok", "bind_fixed": "EACCES",
                    "bind_ephemeral": "bound", "write_inside": "ok", "mkdir_inside": "ok", "write_outside": "EACCES",
                    "mkdir_outside": "EACCES", "append_outside": "EACCES", "read_outside": "outside",
                    "devnull": "ok", "signal": "EPERM", "tiocsti": "EPERM", "io_uring": "ENOSYS",
                    "inherited": "EBADF", "child": "EACCES"}
        if self.other6:
            expected["other6"] = "EACCES"
        self.assertEqual(out, expected)
        detached = self.base / "inside/detached.json"
        for _ in range(200):
            if detached.exists():
                break
            time.sleep(0.05)
        self.assertEqual(json.loads(detached.read_text()), {"other": "EACCES", "root_gone": True})
        for _ in range(100):
            if self.gate.received:
                break
            time.sleep(0.05)
        self.assertEqual(self.gate.received, [b"inside"])
        self.assertEqual(self.other.received, [])
        if self.other6:
            self.assertEqual(self.other6.received, [])
        with self.assertRaises(BlockingIOError):
            self.udp.recv(16)
        self.assertEqual(os.read(reader, 16), b"")  # nothing written, and no copy of the write end is left
        self.assertEqual(os.listdir(self.base / "outside"), ["kept"])
        self.assertEqual((self.base / "outside/kept").read_text(), "outside")
        self.assertIsNone(self.outsider.poll())

    def test_socket_stdio_refuses_before_anything_changes(self):
        first, second = socket.socketpair()
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        done = self.run_child(self.spec(["/bin/true"]), stdin=first)
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(json.loads(done.stdout)["refused"], "stdio")

    def test_input_refusals_before_anything_changes(self):
        cases = [({"gate": 0}, "input"), ({"provider": "other"}, "input"),
                 ({"writable": [str(self.base / "missing")]}, "path"), ({"writable": ["/"]}, "path")]
        for changes, kind in cases:
            with self.subTest(changes=changes):
                done = self.run_child(self.spec(["/bin/true"], **changes), stdin=subprocess.DEVNULL)
                self.assertEqual(done.returncode, 3, done.stderr)
                self.assertEqual(json.loads(done.stdout)["refused"], kind)

    def test_returns_an_empty_prefix_and_the_native_runs(self):
        done = self.run_child(self.spec(["/bin/sh", "-c", "echo confined > \"$0/ran\"", str(self.base / "inside")]),
                              stdin=subprocess.DEVNULL)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.base / "inside/ran").read_text(), "confined\n")


if __name__ == "__main__":
    unittest.main()
