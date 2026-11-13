"""Protected-mode boundaries: the Linux Landlock and seccomp layer, the macOS Seatbelt profile.

Pure logic runs in process on every host: host refusal classification, the
Landlock ruleset plan, the seccomp program (evaluated by a small classic-BPF
interpreter over synthetic seccomp_data), the SBPL profile, interface address
parsing and the self-test verdict. Nothing in process ever confines the test
runner: every call of confine() runs in a child process.

Enforcement cases run the real boundary in a child against 127.0.0.1
listeners that the test owns outside it, and skip cleanly where the host
cannot carry it (Linux without Landlock ABI 6, seccomp or x86_64; a host
without /usr/bin/sandbox-exec). On a macOS release outside
REVIEWED_RELEASES the macOS cases run and skip with what they observed
whenever it differs from the reviewed expectation (see MacEnforcement).
Every listener, fixture and temporary directory is removed again.
"""

import errno
import importlib.util
import json
import os
from pathlib import Path
import platform
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock


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
        with self.assertRaises(linux.Refusal) as caught:
            linux.writable_rules([str(self.base / "root")])
        self.assertIn("the whole filesystem", str(caught.exception))

    def test_a_link_inside_a_writable_path_on_the_way_to_another_refuses(self):
        """Every confined start resolves the writable paths again: a process inside that replaced a
        nested writable path (or a directory on the way to it) with a link must not widen the next
        boundary, here to the directory standing in for HOME."""
        home, outer = self.base / "home", self.base / "home/.codex"
        (outer / "sqlite").mkdir(parents=True)
        (outer / "deep/er").mkdir(parents=True)
        (self.base / "alias").symlink_to(outer)  # a link outside every writable path is fine
        paths = [str(outer), str(outer / "sqlite"), str(outer / "deep/er"), str(self.base / "alias/deep")]
        self.assertIn(str(outer / "sqlite"), dict(linux.writable_rules(paths)))
        (outer / "sqlite").rmdir()
        (outer / "sqlite").symlink_to(home)
        with self.assertRaises(linux.Refusal) as caught:
            linux.writable_rules(paths)
        self.assertEqual(caught.exception.kind, "path")
        self.assertIn(str(outer / "sqlite"), str(caught.exception))
        (outer / "sqlite").unlink()
        (outer / "sqlite").mkdir()
        (outer / "deep/er").rmdir()
        (outer / "deep").rmdir()
        (outer / "deep").symlink_to(self.base)  # deep/er now resolves outside, through deep
        (self.base / "er").mkdir()
        with self.assertRaises(linux.Refusal) as caught:
            linux.writable_rules(paths)
        self.assertIn(str(outer / "deep"), str(caught.exception))

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
options = {"tested": module.Tested(profile=spec["tested"], answers={})} if "tested" in spec else {}
try:
    prefix = module.confine(spec["gate"], spec["writable"], spec["provider"], **options)
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

def alias(kind):
    # Bring the outside file into the writable directory by a link, a rename or a symlink, then write it there.
    target = os.path.join(spec["inside"], "alias-" + kind)
    {"link": os.link, "rename": os.rename, "symlink": os.symlink}[kind](spec["outside_file"], target)
    with open(target, "a") as handle:
        handle.write("x")

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
for kind in ("link", "rename", "symlink"):
    attempt(kind + "_write", lambda: alias(kind))
# Landlock has no right for metadata changes (chmod, chown, utimes, xattrs): disclosed, not denied.
attempt("chmod_outside", lambda: os.chmod(spec["outside_file"], 0o640))
child = subprocess.run([sys.executable, "-I", "-B", "-c", spec["child"], sys.argv[1]], capture_output=True,
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

    def __init__(self, family=socket.AF_INET, address=("127.0.0.1", 0)):
        self.sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock.bind(address)
        self.sock.listen(16)
        self.sock.settimeout(0.2)
        name = self.sock.getsockname()
        self.port = name[1] if isinstance(name, tuple) else None
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
    """The real Landlock and seccomp boundary in a child, against listeners outside it."""

    maxDiff = None

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
        self.other6 = Listener(socket.AF_INET6, ("::1", 0)) if ipv6_loopback() else None
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
                    "link_write": "EXDEV", "rename_write": "EACCES", "symlink_write": "EACCES",
                    "chmod_outside": "ok", "inherited": "EBADF", "child": "EACCES"}
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


macos = load("kit_sandbox_macos_under_test", MACOS_MODULE)

GOLDEN_CODEX = """\
(version 1)
(allow default)
(deny network* (with message "coding-agent-kit"))
(allow network-outbound (remote tcp "localhost:4100"))
(deny job-creation (with message "coding-agent-kit"))
(deny lsopen (with message "coding-agent-kit"))
(deny appleevent-send (with message "coding-agent-kit"))
(deny mach-lookup (with message "coding-agent-kit"))
(allow mach-lookup (global-name "com.apple.FSEvents"))
(allow mach-lookup (global-name "com.apple.PowerManagement.control"))
(allow mach-lookup (global-name "com.apple.SecurityServer"))
(allow mach-lookup (global-name "com.apple.SystemConfiguration.DNSConfiguration"))
(allow mach-lookup (global-name "com.apple.SystemConfiguration.configd"))
(allow mach-lookup (global-name "com.apple.cfprefsd.agent"))
(allow mach-lookup (global-name "com.apple.cfprefsd.daemon"))
(allow mach-lookup (global-name "com.apple.logd"))
(allow mach-lookup (global-name "com.apple.system.notification_center"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.membership"))
(deny signal (with message "coding-agent-kit"))
(allow signal (target same-sandbox))
(deny file-ioctl (ioctl-command TIOCSTI) (with message "coding-agent-kit"))
(deny file-write* (subpath "/"))
(allow file-write* (subpath "/Users/u/proj") (subpath "/private/var/folders/ab/T") (literal "/dev/null") \
(literal "/dev/tty") (literal "/dev/ptmx") (regex #"^/dev/ttys[0-9]+$") (literal "/dev/dtracehelper") \
(regex #"^/dev/fd/"))
(deny file-write* (subpath "/Users/u/proj/Library/LaunchAgents") \
(subpath "/private/var/folders/ab/T/Library/LaunchAgents") (subpath "/private/var/at"))
"""
WRITABLE = ["/Users/u/proj", "/private/var/folders/ab/T"]
PASS_RESULT = {"gate": "connected", "other": "errno:1", "bind": "errno:1", "udp": "errno:1", "write": "errno:1",
               "tiocsti": "errno:1"}
IFCONFIG_SAMPLE = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\toptions=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
\tinet 127.0.0.2 netmask 0xff000000
\tinet 127.1.2.3 netmask 0xff000000
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1%lo0 prefixlen 128
\tnd6 options=201<PERFORMNUD,DAD>
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 00:00:5e:00:53:01
\tinet6 fe80::1c2b:3d4e:5f60:7182%en0 prefixlen 64 secured scopeid 0xb
\tinet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255
\tinet6 2001:db8::10 prefixlen 64 autoconf secured
\tinet6 2001:db8::10 prefixlen 64 autoconf temporary
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet6 fe80::abcd%utun0 prefixlen 64 scopeid 0xf
\tinet 198.51.100.7 --> 198.51.100.1 netmask 0xffffffff
bad0: flags=0<> mtu 0
\tinet notanaddress netmask 0x0
\tinet 999.1.1.1 netmask 0x0
\tinet6 192.0.2.99 prefixlen 64
\tinet 2001:db8::99 netmask 0x0
\tinet6 fe80::2%bad/scope prefixlen 64
\tinet6
"""
# 127.0.0.1 and ::1 are held listening (Hold.listen); every other address, loopback aliases included, is reserved.
IFCONFIG_PARSED = (("inet6", "fe80::1%lo0"), ("inet", "127.0.0.2"), ("inet", "127.1.2.3"),
                   ("inet6", "fe80::1c2b:3d4e:5f60:7182%en0"), ("inet", "192.0.2.10"), ("inet6", "2001:db8::10"),
                   ("inet6", "fe80::abcd%utun0"), ("inet", "198.51.100.7"))


def mac_facts(**changes):
    facts = {"system": "Darwin", "machine": "arm64", "version": "27.2", "sandbox_exec": True, "ifconfig": True}
    facts.update(changes)
    return facts


class MacHost(unittest.TestCase):
    """macOS refuses every host the reviewed matrix did not cover (G0 D7), before anything runs."""

    def refused(self, kind, **changes):
        with self.assertRaises(macos.Refusal) as caught:
            macos.assess(mac_facts(**changes))
        self.assertEqual(caught.exception.kind, kind)
        message = str(caught.exception)
        self.assertTrue(message and message.isascii() and message.isprintable(), message)
        return message

    def test_reviewed_release(self):
        self.assertEqual(macos.assess(mac_facts()), macos.Host(version="27.2", arch="arm64"))
        self.assertEqual(macos.assess(mac_facts(version="27.2.1")), macos.Host(version="27.2.1", arch="arm64"))

    def test_other_systems_architectures_and_releases(self):
        self.assertIn("macOS", self.refused("platform", system="Linux"))
        self.assertIn("x86_64", self.refused("architecture", machine="x86_64"))
        for version in ("27.3", "28", "28.0.1", "30.1"):
            with self.subTest(version=version):
                message = self.refused("version", version=version)
                self.assertIn("newer than the newest reviewed release (27.2)", message)
        for version in ("27.1", "27.0", "27", "26.4", "15.5", "10.16"):
            with self.subTest(version=version):
                self.assertIn("not a reviewed release", self.refused("version", version=version))
        for version in ("", None, "27.2-beta", "27.2.1.4", "x", "27.2\n"):
            with self.subTest(version=version):
                self.assertIn("could not be read", self.refused("version", version=version))

    def test_missing_tools(self):
        self.assertIn(macos.SANDBOX_EXEC, self.refused("sandbox-exec", sandbox_exec=False))
        self.assertIn(macos.IFCONFIG, self.refused("ifconfig", ifconfig=False))

    def test_order(self):
        self.refused("platform", system="Linux", machine="x86_64", version="")
        self.refused("architecture", machine="x86_64", version="28.0", sandbox_exec=False)
        self.refused("version", version="28.0", sandbox_exec=False, ifconfig=False)
        self.refused("sandbox-exec", sandbox_exec=False, ifconfig=False)

    def test_probe_changes_nothing(self):
        try:
            host = macos.probe()
        except macos.Refusal as refusal:
            self.assertIn(refusal.kind, macos.KINDS)
            if sys.platform != "darwin":
                self.assertEqual(refusal.kind, "platform")
        else:
            self.assertIn(host.arch, macos.ARCHES)


class MacProfile(unittest.TestCase):
    def test_codex_profile(self):
        self.assertEqual(macos.render_profile(4100, WRITABLE, "codex", 501), GOLDEN_CODEX)

    def test_claude_adds_its_fixed_temp_directory(self):
        claude = macos.render_profile(4100, WRITABLE, "claude", 501)
        self.assertEqual(claude, GOLDEN_CODEX.replace('(subpath "/private/var/folders/ab/T") ',
                                                      '(subpath "/private/tmp/claude-501") '
                                                      '(subpath "/private/var/folders/ab/T") '))
        self.assertNotIn("claude-501/Library", claude)
        self.assertIn('(subpath "/private/tmp/claude-0")', macos.render_profile(4100, WRITABLE, "claude", 0))
        self.assertEqual(macos.claude_temp(501), "/private/tmp/claude-501")

    def test_deterministic_for_the_same_set(self):
        shuffled = [WRITABLE[1], WRITABLE[0], WRITABLE[1]]
        self.assertEqual(macos.render_profile(4100, shuffled, "codex", 501), GOLDEN_CODEX)
        self.assertEqual(macos.render_profile(4100, tuple(WRITABLE), "codex", 501), GOLDEN_CODEX)

    def test_network_rules(self):
        profile = macos.render_profile(65535, ["/Users/u"], "codex", 501)
        self.assertIn('(allow network-outbound (remote tcp "localhost:65535"))', profile)
        self.assertNotIn("remote ip", profile)  # a remote ip rule would admit UDP to the gate's port number
        self.assertEqual(profile.count("network-outbound"), 1)
        self.assertNotIn("network-bind", profile)

    def test_services(self):
        self.assertEqual(len(macos.MACH_SERVICES), 11)
        self.assertIn("com.apple.SecurityServer", macos.MACH_SERVICES)
        for escape in ("com.apple.coreservices.launchservicesd", "com.apple.coreservices.appleevents",
                       "com.apple.nsurlsessiond", "com.apple.dnssd.service", "com.apple.lsd.mapdb",
                       "com.apple.lsd.modifydb", "com.apple.trustd.agent"):
            self.assertNotIn(escape, macos.MACH_SERVICES)

    def test_refusals(self):
        cases = [((0, WRITABLE, "codex", 501), "input"), ((True, WRITABLE, "codex", 501), "input"),
                 ((65536, WRITABLE, "codex", 501), "input"), (("4100", WRITABLE, "codex", 501), "input"),
                 ((4100, WRITABLE, "gemini", 501), "input"), ((4100, "/Users/u", "codex", 501), "input"),
                 ((4100, WRITABLE, "codex", -1), "input"), ((4100, WRITABLE, "codex", True), "input"),
                 ((4100, WRITABLE, "codex", "501"), "input")]
        for path in ("relative", "", "/", "/tmp", "/tmp/x", "/var/folders/x", "/etc/hosts", '/Users/u/a"b',
                     "/Users/u/a\\b", "/Users/u/café", "/Users/u/proj/", "/Users/u/../x", "//Users/u",
                     "/Users/u/./x", "/Users/u\nx", "/Users/u/x\n", "/Users/u/x)y\x7f"):
            cases.append(((4100, [path], "codex", 501), "path"))
        for args, kind in cases:
            with self.subTest(args=args), self.assertRaises(macos.Refusal) as caught:
                macos.render_profile(*args)
            self.assertEqual(caught.exception.kind, kind)
            self.assertTrue(str(caught.exception).isascii() and str(caught.exception).isprintable())

    def test_parentheses_and_spaces_are_fine_inside_quotes(self):
        profile = macos.render_profile(4100, ["/Users/u/My Projects (old)"], "codex", 501)
        self.assertIn('(subpath "/Users/u/My Projects (old)")', profile)


class MacWritable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-sandbox-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_resolves_links_and_drops_duplicates(self):
        (self.base / "work").mkdir()
        (self.base / "link").symlink_to(self.base / "work")
        (self.base / "file").write_text("")
        paths = [str(self.base / "link"), str(self.base / "work"), str(self.base / "file")]
        self.assertEqual(macos.resolve_writable(paths), [str(self.base / "work"), str(self.base / "file")])

    def test_refusals(self):
        (self.base / "root").symlink_to("/")
        for paths, kind in ((["relative"], "path"), ([str(self.base / "missing")], "path"), (["/"], "path"),
                            ([str(self.base / "root")], "path"), ([str(self.base / "x\0")], "path"),
                            ("/x", "input"), ([b"/x"], "input")):
            with self.subTest(paths=paths), self.assertRaises(macos.Refusal) as caught:
                macos.resolve_writable(paths)
            self.assertEqual(caught.exception.kind, kind)


def write_script(path, text):
    path.write_text(text)
    path.chmod(0o755)
    return str(path)


class MacHolder(unittest.TestCase):
    """The addresses the launcher holds the gate port on (G0 D9, section 2.4)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-sandbox-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_parse(self):
        self.assertEqual(macos.parse_ifconfig(IFCONFIG_SAMPLE), IFCONFIG_PARSED)
        self.assertEqual(macos.parse_ifconfig(""), ())

    def test_holder_addresses(self):
        (self.base / "out.txt").write_text(IFCONFIG_SAMPLE)
        fake = write_script(self.base / "ifconfig", '#!/bin/sh\n[ "$1" = -a ] || exit 64\ncat "%s"\n'
                            % (self.base / "out.txt"))
        hold = macos.holder_addresses(ifconfig=fake)
        self.assertEqual(hold.listen, (("inet", "127.0.0.1"), ("inet6", "::1")))
        self.assertEqual(hold.reserve, (("inet", "0.0.0.0"), ("inet6", "::")) + IFCONFIG_PARSED)

    def test_holder_refusals(self):
        failing = write_script(self.base / "failing", "#!/bin/sh\necho broken >&2\nexit 1\n")
        hanging = write_script(self.base / "hanging", "#!/bin/sh\nexec sleep 30\n")
        for fake, timeout in ((failing, 10), (str(self.base / "missing"), 10), (hanging, 0.5)):
            with self.subTest(fake=fake), self.assertRaises(macos.Refusal) as caught:
                macos.holder_addresses(ifconfig=fake, timeout=timeout)
            self.assertEqual(caught.exception.kind, "ifconfig")


class MacSelfTestVerdict(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(macos.self_test_failures(dict(PASS_RESULT)), [])

    def test_each_failure_is_named(self):
        problems = {"gate": ("errno:61", "the gate port is unreachable"),
                    "other": ("connected", "another loopback port is not denied"),
                    "bind": ("bound", "binding a port is not denied"),
                    "udp": ("sent", "UDP to the gate port is not denied"),
                    "write": ("written", "a write outside the writable paths is not denied"),
                    "tiocsti": ("injected", "terminal input injection (TIOCSTI) is not denied")}
        for key, (value, text) in problems.items():
            with self.subTest(key=key):
                result = dict(PASS_RESULT, **{key: value})
                self.assertEqual(macos.self_test_failures(result), ["%s (%s)" % (text, value)])
                missing = dict(PASS_RESULT)
                del missing[key]
                self.assertEqual(macos.self_test_failures(missing), ["%s (no answer)" % text])

    def test_no_result(self):
        self.assertEqual(len(macos.self_test_failures(None)), 1)
        self.assertEqual(len(macos.self_test_failures([PASS_RESULT])), 1)
        self.assertEqual(macos.self_test_failures({"error": "timed out\x1b[2J"}), ["timed out?[2J"])
        self.assertEqual(macos.self_test_failures(dict(PASS_RESULT, other="\x1b]0;x\x07" * 50))[0][-4:], "...)")


class MacSelfTestRunner(unittest.TestCase):
    """The self-test plumbing with fake sandbox-exec scripts; nothing is confined here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-sandbox-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / "probe").mkdir()
        self.gate = Listener()
        self.addCleanup(self.gate.close)

    def run_self_test(self, fake, **options):
        options.setdefault("probe_bases", [str(self.base / "probe")])
        return macos.self_test(self.gate.port, ["/usr"], "codex", sandbox_exec=fake, **options)

    def test_passing_profile(self):
        fake = write_script(self.base / "sandbox-exec", "#!/bin/sh\n[ \"$1\" = -p ] || exit 64\n"
                            "printf '%%s' \"$2\" > '%s'\nprintf '%%s\\n' '%s'\n"
                            % (self.base / "profile.sb", json.dumps(PASS_RESULT)))
        profile = macos.render_profile(self.gate.port, ["/usr"], "codex", os.getuid())
        self.assertEqual(self.run_self_test(fake), macos.Tested(profile=profile, answers=PASS_RESULT))
        self.assertEqual((self.base / "profile.sb").read_text(), profile)
        self.assertEqual(os.listdir(self.base / "probe"), [])

    def test_an_unconfined_run_fails_on_every_denial_it_checks(self):
        fake = write_script(self.base / "sandbox-exec", '#!/bin/sh\n[ "$1" = -p ] || exit 64\nshift 2\nexec "$@"\n')
        with self.assertRaises(macos.Refusal) as caught:
            self.run_self_test(fake)
        self.assertEqual(caught.exception.kind, "self-test")
        message = str(caught.exception)
        texts = ["another loopback port is not denied (connected)", "binding a port is not denied (bound)",
                 "UDP to the gate port is not denied (sent)",
                 "a write outside the writable paths is not denied (written)"]
        if sys.platform == "darwin":  # Linux may refuse TIOCSTI itself (dev.tty.legacy_tiocsti)
            texts.append("terminal input injection (TIOCSTI) is not denied (injected)")
        for text in texts:
            self.assertIn(text, message)
        self.assertNotIn("the gate port is unreachable", message)
        self.assertEqual(os.listdir(self.base / "probe"), [])

    def test_a_rejected_profile(self):
        fake = write_script(self.base / "sandbox-exec",
                            "#!/bin/sh\necho 'sandbox-exec: unbound variable: tcp' >&2\nexit 65\n")
        with self.assertRaises(macos.Refusal) as caught:
            self.run_self_test(fake)
        self.assertIn("exited with status 65: sandbox-exec: unbound variable: tcp", str(caught.exception))

    def test_a_missing_sandbox_exec(self):
        with self.assertRaises(macos.Refusal) as caught:
            self.run_self_test(str(self.base / "missing"))
        self.assertEqual(caught.exception.kind, "self-test")

    def test_bounded_in_time(self):
        fake = write_script(self.base / "sandbox-exec", "#!/bin/sh\nexec sleep 30\n")
        began = time.monotonic()
        with self.assertRaises(macos.Refusal) as caught:
            self.run_self_test(fake, timeout=0.5)
        self.assertLess(time.monotonic() - began, 10)
        self.assertIn("did not finish within 0.5 s", str(caught.exception))
        self.assertEqual(os.listdir(self.base / "probe"), [])

    def test_needs_a_directory_outside_the_writable_paths(self):
        fake = write_script(self.base / "sandbox-exec", "#!/bin/sh\nexit 0\n")
        for bases in (["/usr"], ["/usr/share"], [str(self.base / "missing")], []):
            with self.subTest(bases=bases), self.assertRaises(macos.Refusal) as caught:
                self.run_self_test(fake, probe_bases=bases)
            self.assertIn("outside the writable paths", str(caught.exception))

    def test_input_refusals_come_first(self):
        with self.assertRaises(macos.Refusal) as caught:
            macos.self_test(0, ["/usr"], "codex", sandbox_exec="/nonexistent")
        self.assertEqual(caught.exception.kind, "input")


DESCRIPTORS = r"""
import errno, importlib.util, json, os, sys
loader = importlib.util.spec_from_file_location("sandbox_under_test", sys.argv[1])
module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(module)
reader, writer = os.pipe()
module.close_inherited()
results = {}
for name, fd in (("reader", reader), ("writer", writer), ("inherited", int(sys.argv[2]))):
    try:
        os.fstat(fd)
        results[name] = "open"
    except OSError as error:
        results[name] = errno.errorcode[error.errno]
print(json.dumps(results))
"""


class MacDescriptors(unittest.TestCase):
    """confine() closes every descriptor but the terminal: Seatbelt does not police existing sockets."""

    def test_close_inherited(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        try:
            done = subprocess.run([sys.executable, "-I", "-B", "-c", DESCRIPTORS, str(MACOS_MODULE), str(writer)],
                                  capture_output=True, text=True, timeout=30, pass_fds=(writer,),
                                  stdin=subprocess.DEVNULL)
        finally:
            os.close(writer)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {"reader": "EBADF", "writer": "EBADF", "inherited": "EBADF"})
        self.assertEqual(os.read(reader, 16), b"")


class MacConfine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-sandbox-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def run_child(self, argv, omit=(), **changes):
        spec = {"module": str(MACOS_MODULE), "gate": 4100, "writable": [str(self.base)], "provider": "codex",
                "argv": argv, "tested": ""}
        spec.update(changes)
        for key in omit:
            del spec[key]
        return subprocess.run([sys.executable, "-I", "-B", "-c", RUNNER, json.dumps(spec)], capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})

    def test_confine_on_this_host(self):
        argv = [sys.executable, "-I", "-B", "-c", "print('ran')"]
        try:
            macos.probe()
        except macos.Refusal as refusal:
            done = self.run_child(argv)
            self.assertEqual(done.returncode, 3, done.stderr)
            self.assertEqual(json.loads(done.stdout)["refused"], refusal.kind)
        else:
            tested = macos.render_profile(4100, macos.resolve_writable([str(self.base)]), "codex", os.getuid())
            done = self.run_child(argv, tested=tested)
            self.assertEqual((done.returncode, done.stdout), (0, "ran\n"), done.stderr)

    def test_input_refusals_come_first(self):
        # The last case has no result of a passing self_test(), so there is nothing to exec.
        for changes, kind in (({"gate": 0}, "input"), ({"provider": "x"}, "input"), ({"omit": ["tested"]}, "input")):
            with self.subTest(changes=changes):
                done = self.run_child(["/usr/bin/true"], **changes)
                self.assertEqual(done.returncode, 3, done.stderr)
                self.assertEqual(json.loads(done.stdout)["refused"], kind)

    def test_execs_only_the_tested_profile(self):
        # probe() and the descriptor steps are stubbed, so this runs on any host and changes nothing here.
        writable = [str(self.base)]
        stubs = {"probe": lambda: macos.Host(version="27.2", arch="arm64"), "stdio_sockets": lambda: [],
                 "close_inherited": lambda: None, "resolve_writable": lambda paths: ["/Users/u/proj"]}
        with unittest.mock.patch.multiple(macos, **stubs):
            profile = macos.render_profile(4100, ["/Users/u/proj"], "codex", os.getuid())
            tested = macos.Tested(profile=profile, answers=dict(PASS_RESULT))
            self.assertEqual(macos.confine(4100, writable, "codex", tested=tested),
                             [macos.SANDBOX_EXEC, "-p", profile])
            for port, provider in ((4101, "codex"), (4100, "claude")):
                with self.subTest(port=port, provider=provider), self.assertRaises(macos.Refusal) as caught:
                    macos.confine(port, writable, provider, tested=tested)
                self.assertEqual(caught.exception.kind, "self-test")
                self.assertIn("differs from the one the self-test checked", str(caught.exception))
            for tested in (None, profile, {"profile": profile}):
                with self.subTest(tested=tested), self.assertRaises(macos.Refusal) as caught:
                    macos.confine(4100, writable, "codex", tested=tested)
                self.assertEqual(caught.exception.kind, "input")


MAC_ACTORS = r"""
import errno, json, os, socket, subprocess, sys, time
spec = json.loads(sys.argv[1])
out = {}

def attempt(name, action):
    try:
        out[name] = action() or "ok"
    except OSError as error:
        out[name] = errno.errorcode.get(error.errno, str(error.errno))

def tcp(port, tag=b"inside"):
    with socket.socket() as sock:
        sock.settimeout(3)
        sock.connect(("127.0.0.1", port))
        sock.sendall(tag)
        return "connected"

def udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b"udp", ("127.0.0.1", spec["udp"]))
        return "sent"

def bind():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return "bound"

def unix():
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(spec["unix"])
        sock.sendall(b"inside")
        return "connected"

def write(path):
    with open(path, "w") as handle:
        handle.write("x")

def alias(kind):
    # Bring the outside file into the writable directory by a link, a rename or a symlink, then write it there.
    target = os.path.join(spec["inside"], "alias-" + kind)
    {"link": os.link, "rename": os.rename, "symlink": os.symlink}[kind](spec["outside_file"], target)
    with open(target, "a") as handle:
        handle.write("x")

attempt("gate", lambda: tcp(spec["gate"]))
attempt("other", lambda: tcp(spec["other"]))
attempt("udp", udp)
attempt("bind", bind)
if spec.get("unix"):
    attempt("unix", unix)
attempt("write_inside", lambda: write(os.path.join(spec["inside"], "written")))
attempt("write_outside", lambda: write(os.path.join(spec["outside"], "written")))
attempt("read_outside", lambda: open(spec["outside_file"]).read())
for kind in ("link", "rename", "symlink"):
    attempt(kind + "_write", lambda: alias(kind))
attempt("signal", lambda: os.kill(spec["outsider"], 0))
child = subprocess.run([sys.executable, "-I", "-B", "-c", spec["child"], sys.argv[1]], capture_output=True,
                       text=True, timeout=20, stdin=subprocess.DEVNULL)
out["child"] = child.stdout.strip()
root = os.getpid()
if os.fork() == 0:
    os.setsid()
    if os.fork():
        os._exit(0)
    null = os.open("/dev/null", os.O_RDWR)
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
        except OSError:
            break
        time.sleep(0.05)
    try:
        late["other"] = tcp(spec["other"], tag=b"detached")
    except OSError as error:
        late["other"] = errno.errorcode.get(error.errno, str(error.errno))
    with open(os.path.join(spec["inside"], "detached.tmp"), "w") as handle:
        json.dump(late, handle)
    os.rename(os.path.join(spec["inside"], "detached.tmp"), os.path.join(spec["inside"], "detached.json"))
    os._exit(0)
print(json.dumps(out), flush=True)
"""


def wait_for(condition, tries):
    for _ in range(tries):
        if condition():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(sys.platform == "darwin", "the macOS boundary needs macOS")
class MacEnforcement(unittest.TestCase):
    """The real profile under /usr/bin/sandbox-exec, against listeners outside it.

    On a release in REVIEWED_RELEASES every difference from the reviewed
    expectation fails. Protected mode refuses every other host (an unreviewed
    release, an Intel Mac), so there a failing self-test or a difference
    skips the case and names what it observed, which a -v run prints;
    test_self_test_passes also writes the host and the self-test answers to
    stderr. Hosted macOS CI thus records evidence without turning red, and
    the reviewed host is where these cases decide.
    """

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        if not os.access(macos.SANDBOX_EXEC, os.X_OK):
            raise unittest.SkipTest("%s is not available" % macos.SANDBOX_EXEC)
        try:
            macos.probe()
        except macos.Refusal as refusal:
            cls.unreviewed = str(refusal)
        else:
            cls.unreviewed = None

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
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.setblocking(False)
        self.addCleanup(self.udp.close)
        self.unix = None
        try:
            self.unix = Listener(socket.AF_UNIX, str(self.base / "outside/s"))
            self.addCleanup(self.unix.close)
        except OSError:
            pass  # a temporary directory too deep for a socket path; the TCP cases still run
        self.outsider = subprocess.Popen(["sleep", "60"], stdin=subprocess.DEVNULL)
        self.addCleanup(LinuxEnforcement.reap, self.outsider)

    def settle(self, observed, expected, detail=""):
        """Fail on a reviewed release; on any other, skip and name every difference."""
        if observed == expected or self.unreviewed is None:
            self.assertEqual(observed, expected, detail)
            return
        differences = {key: observed.get(key, "missing") for key in sorted(set(observed) | set(expected))
                       if observed.get(key, "missing") != expected.get(key, "missing")}
        self.skipTest("unreviewed host (%s); differs from the reviewed expectation: %s %s"
                      % (self.unreviewed, json.dumps(differences, sort_keys=True), detail))

    def require_profile(self):
        try:
            tested = macos.self_test(self.gate.port, [str(self.base / "inside")], "codex")
        except macos.Refusal as refusal:
            if self.unreviewed is None:
                raise
            self.skipTest("unreviewed host (%s); its self-test failed: %s" % (self.unreviewed, refusal))
        # The self-test connected to the gate once and sent nothing; let the listener record that, then start afresh.
        wait_for(lambda: self.gate.received, 100)
        self.assertEqual(self.gate.received, [b""])
        self.gate.received.clear()
        return tested

    def test_self_test_passes(self):
        tested = self.require_profile()
        sys.stderr.write("\nMacEnforcement: macOS %s %s, %s; self-test answers %s\n"
                         % (platform.mac_ver()[0], platform.machine(),
                            "reviewed" if self.unreviewed is None else "unreviewed (%s)" % self.unreviewed,
                            json.dumps(tested.answers, sort_keys=True)))
        self.assertEqual(tested.answers, PASS_RESULT)
        self.assertEqual(sorted(os.listdir(self.base / "inside")), [])

    def test_only_the_gate_port_for_every_descendant(self):
        tested = self.require_profile()
        actors = {"gate": self.gate.port, "other": self.other.port, "udp": self.udp.getsockname()[1],
                  "unix": str(self.base / "outside/s") if self.unix else None, "inside": str(self.base / "inside"),
                  "outside": str(self.base / "outside"), "outside_file": str(self.base / "outside/kept"),
                  "outsider": self.outsider.pid, "child": CHILD}
        done = subprocess.run([macos.SANDBOX_EXEC, "-p", tested.profile, sys.executable, "-I", "-B", "-c",
                               MAC_ACTORS, json.dumps(actors)], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)
        lines = done.stdout.strip().splitlines()
        if done.returncode != 0 or not lines:
            self.settle({"exit": done.returncode}, {"exit": 0}, done.stderr.strip()[-400:])
        observed = json.loads(lines[-1])
        detached = self.base / "inside/detached.json"
        wait_for(detached.exists, 200)
        observed["detached"] = json.loads(detached.read_text()) if detached.exists() else "missing"
        wait_for(lambda: self.gate.received, 100)
        observed["gate_received"] = [data.decode("ascii", "replace") for data in self.gate.received]
        observed["other_received"] = [data.decode("ascii", "replace") for data in self.other.received]
        if self.unix:
            observed["unix_received"] = [data.decode("ascii", "replace") for data in self.unix.received]
        try:
            observed["udp_received"] = self.udp.recv(16).decode("ascii", "replace")
        except BlockingIOError:
            observed["udp_received"] = None
        observed["outside"] = sorted(os.listdir(self.base / "outside"))
        kept = self.base / "outside/kept"
        observed["outside_kept"] = kept.read_text() if kept.exists() else "missing"
        observed["outsider_alive"] = self.outsider.poll() is None
        expected = {"gate": "connected", "other": "EPERM", "udp": "EPERM", "bind": "EPERM", "write_inside": "ok",
                    "write_outside": "EPERM", "read_outside": "outside", "link_write": "EPERM",
                    "rename_write": "EPERM", "symlink_write": "EPERM", "signal": "EPERM", "child": "EPERM",
                    "detached": {"other": "EPERM", "root_gone": True}, "gate_received": ["inside"],
                    "other_received": [], "udp_received": None, "outside": ["kept"], "outside_kept": "outside",
                    "outsider_alive": True}
        if self.unix:
            expected.update(unix="EPERM", unix_received=[], outside=["kept", "s"])
        self.settle(observed, expected)


if __name__ == "__main__":
    unittest.main()
