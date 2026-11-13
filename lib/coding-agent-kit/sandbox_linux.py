"""coding-agent-kit Linux boundary: the native and every descendant reach only the gate port.

Protected mode starts the native inside a boundary that every descendant
inherits and none can leave. confine() builds it in the child that is about
to exec the native, in this order:

- refuse when fd 0, 1 or 2 is a socket, then close every other descriptor:
  Landlock and seccomp police new sockets, not ones that already exist;
- set no_new_privs;
- enter one Landlock domain that
  - handles TCP connect and allows it only to the gate port,
  - handles TCP bind and allows only port 0, a kernel-chosen ephemeral port
    (listen() on an unbound socket autobinds one anyway), so no process
    inside can bind the gate port or any other chosen port,
  - scopes abstract UNIX sockets and signals to the domain, so nothing inside
    reaches an outside abstract socket or signals the gate, monitor or holder,
  - handles every write-class filesystem right and allows it only beneath
    the writable paths and the terminal devices in DEVICES; reads and
    execution stay open;
- install a seccomp filter that allows socket() only for IPv4 and IPv6 TCP
  (protocol 0 or IPPROTO_TCP) and netlink route, and socketpair() only for
  AF_UNIX stream and seqpacket, whose connected ends cannot be re-addressed.
  UDP, raw, MPTCP and SCTP sockets fail with EPROTONOSUPPORT, every other
  family and every AF_UNIX socket() with EAFNOSUPPORT, TCP Fast Open sends
  (they connect without the connect hook) with EOPNOTSUPP, io_uring with
  ENOSYS and ioctl(TIOCSTI) with EPERM. A syscall of another architecture
  kills the process.

Landlock names a port, not an address: an outside listener on the gate port
at another loopback address stays reachable, so the launcher holds the port
for the whole invocation. The boundary governs egress; a process inside can
still accept connections that outside parties start.

Public API (load this file by path; it is never on sys.path):

    Refusal(kind, message)
        Protected mode cannot start. str() is the message for the user, kind
        one of KINDS.
    probe() -> Host
        Whether this host can carry the boundary, without changing anything:
        Host(abi, arch), or Refusal with the reason. Call it in the launcher
        before the gate starts; it also loads libc there, before any fork.
    confine(gate_port, writable_paths, provider) -> list[str]
        Applies the boundary to the calling process: the child that is about
        to exec the native, after fork and just before exec, with the
        native's terminal already on fd 0-2. Returns the argv prefix to put
        before the native command, which is empty here (the macOS module
        returns its sandbox-exec prefix). Every host and input refusal comes
        before anything changes; a Refusal of kind "descriptors" or
        "enforcement" may follow a partial confinement, so after any Refusal
        the caller exits without starting the native.
    assess(facts) -> Host, host_facts() -> dict
        The pure refusal rules of probe() and the host facts they read.
    check_inputs(gate_port, writable_paths, provider)
        The input refusals of confine().
    writable_rules(paths) -> tuple
        (resolved path, Landlock rights) for each existing writable path and
        each device in DEVICES that exists.
    ruleset_plan(abi, gate_port, rules) -> dict
        The Landlock ruleset confine() creates.
    seccomp_program(arch) -> list
        The seccomp filter as classic-BPF (code, jt, jf, k) rows.
    stdio_sockets(fds=(0, 1, 2)) -> list[int]
        The descriptors among fds that are sockets.

Supported: Landlock ABI 6 or newer (Linux 6.12), seccomp filters, 64-bit
x86_64, not WSL. The aarch64 syscall table is written and unit-tested but
not validated on a host, so aarch64 refuses until it is.
"""

import ctypes
from dataclasses import dataclass
import errno
import os
import platform
import stat
import struct


PROVIDERS = ("codex", "claude")
MIN_ABI = 6
VALIDATED_ARCHES = ("x86_64",)
KINDS = ("platform", "architecture", "wsl", "landlock", "seccomp", "input", "path", "stdio", "descriptors",
         "enforcement")
# Writable for every native: the terminal, its PTYs and POSIX shared memory.
DEVICES = ("/dev/null", "/dev/tty", "/dev/ptmx", "/dev/pts", "/dev/shm")

# Landlock (include/uapi/linux/landlock.h). The syscall numbers are the same on x86_64 and aarch64.
SYS_LANDLOCK_CREATE_RULESET, SYS_LANDLOCK_ADD_RULE, SYS_LANDLOCK_RESTRICT_SELF = 444, 445, 446
SYS_CLOSE_RANGE = 436
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2
FS_WRITE_FILE = 1 << 1
FS_REMOVE_DIR, FS_REMOVE_FILE, FS_MAKE_CHAR, FS_MAKE_DIR, FS_MAKE_REG = (1 << bit for bit in range(4, 9))
FS_MAKE_SOCK, FS_MAKE_FIFO, FS_MAKE_BLOCK, FS_MAKE_SYM, FS_REFER, FS_TRUNCATE = (1 << bit for bit in range(9, 15))
# Every write-class right; EXECUTE, READ_FILE, READ_DIR and IOCTL_DEV stay unhandled.
DIR_RIGHTS = (FS_WRITE_FILE | FS_REMOVE_DIR | FS_REMOVE_FILE | FS_MAKE_CHAR | FS_MAKE_DIR | FS_MAKE_REG
              | FS_MAKE_SOCK | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM | FS_REFER | FS_TRUNCATE)
FILE_RIGHTS = FS_WRITE_FILE | FS_TRUNCATE  # the write rights a rule on a non-directory may carry
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1
RESTRICT_SELF_TSYNC, TSYNC_ABI = 1 << 3, 8
PR_SET_NO_NEW_PRIVS = 38

# seccomp and classic BPF (include/uapi/linux/seccomp.h, filter.h).
SECCOMP_SET_MODE_FILTER, SECCOMP_GET_ACTION_AVAIL = 1, 2
SECCOMP_FILTER_FLAG_TSYNC = 1
RET_KILL_PROCESS, RET_ERRNO, RET_ALLOW = 0x80000000, 0x00050000, 0x7FFF0000
BPF_LD_W_ABS, BPF_ALU_AND_K, BPF_JEQ_K, BPF_JGE_K, BPF_JSET_K, BPF_RET_K = 0x20, 0x54, 0x15, 0x35, 0x45, 0x06
# struct seccomp_data offsets; an argument's low 32 bits on little-endian. The kernel reads the
# family, type, protocol, flags and ioctl request below as 32-bit values, so their high half is ignored.
DATA_NR, DATA_ARCH = 0, 4


def data_arg(index):
    return 16 + 8 * index


AF_UNIX, AF_INET, AF_INET6, AF_NETLINK = 1, 2, 10, 16
SOCK_STREAM, SOCK_SEQPACKET, SOCK_TYPE_MASK = 1, 5, 0xF
IPPROTO_TCP, NETLINK_ROUTE = 6, 0
MSG_FASTOPEN = 0x20000000
TIOCSTI = 0x5412  # asm-generic; the same on x86_64 and aarch64
X32_SYSCALL_BIT = 0x40000000


@dataclass(frozen=True)
class Syscalls:
    audit_arch: int
    seccomp: int
    ioctl: int
    socket: int
    socketpair: int
    sendto: int
    sendmsg: int
    sendmmsg: int
    io_uring: tuple
    x32: bool


SYSCALLS = {
    "x86_64": Syscalls(audit_arch=0xC000003E, seccomp=317, ioctl=16, socket=41, socketpair=53, sendto=44,
                       sendmsg=46, sendmmsg=307, io_uring=(425, 426, 427), x32=True),
    "aarch64": Syscalls(audit_arch=0xC00000B7, seccomp=277, ioctl=29, socket=198, socketpair=199, sendto=206,
                        sendmsg=211, sendmmsg=269, io_uring=(425, 426, 427), x32=False),
}


class Refusal(Exception):
    """Protected mode cannot start; str() is the message for the user."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Host:
    abi: int
    arch: str


_LIBC = None


def _libc():
    global _LIBC
    if _LIBC is None:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        libc.prctl.restype = ctypes.c_int
        _LIBC = libc
    return _LIBC


def _syscall(number, *args):
    values = [ctypes.c_long(arg) if isinstance(arg, int) else ctypes.c_void_p() if arg is None else arg
              for arg in args]
    result = _libc().syscall(ctypes.c_long(number), *values)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def _wsl():
    try:
        with open("/proc/sys/kernel/osrelease") as handle:
            release = handle.read().lower()
    except OSError:
        release = ""
    return ("microsoft" in release or "wsl" in release or os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")
            or "WSL_DISTRO_NAME" in os.environ)


def host_facts():
    """What assess() decides on. Reads /proc and asks the kernel; changes nothing."""
    facts = {"system": platform.system(), "machine": platform.machine(), "bits": struct.calcsize("P") * 8,
             "wsl": False, "abi": None, "abi_errno": None, "seccomp_errno": None}
    if facts["system"] != "Linux" or facts["bits"] != 64 or facts["machine"] not in SYSCALLS:
        return facts
    facts["wsl"] = _wsl()
    try:
        facts["abi"] = _syscall(SYS_LANDLOCK_CREATE_RULESET, None, 0, LANDLOCK_CREATE_RULESET_VERSION)
    except OSError as error:
        facts["abi_errno"] = error.errno
    for action in (RET_KILL_PROCESS, RET_ERRNO):
        value = ctypes.c_uint32(action)
        try:
            _syscall(SYSCALLS[facts["machine"]].seccomp, SECCOMP_GET_ACTION_AVAIL, 0, ctypes.byref(value))
        except OSError as error:
            facts["seccomp_errno"] = error.errno
            break
    return facts


def assess(facts):
    """Host or Refusal for facts as host_facts() returns them."""
    if facts.get("system") != "Linux":
        raise Refusal("platform", "the Linux boundary needs Linux")
    machine = facts.get("machine")
    if facts.get("bits") != 64 or machine not in SYSCALLS:
        raise Refusal("architecture", "protected mode supports 64-bit x86_64 Linux, not this %d-bit %s process"
                      % (facts.get("bits") or 0, _printable(machine)))
    if machine not in VALIDATED_ARCHES:
        raise Refusal("architecture", "the boundary's %s syscall table is not validated yet; Linux protected mode"
                      " supports x86_64" % machine)
    if facts.get("wsl"):
        raise Refusal("wsl", "protected mode does not support WSL: its Landlock support and Windows interop are"
                      " unproven")
    abi = facts.get("abi")
    if abi is None:
        code = facts.get("abi_errno")
        if code == errno.EOPNOTSUPP:
            reason = "Landlock is disabled in this kernel (it is not in the boot lsm= list)"
        elif code == errno.ENOSYS:
            reason = "this kernel has no Landlock"
        else:
            reason = "Landlock is unavailable (%s)" % (os.strerror(code) if code else "no answer")
        raise Refusal("landlock", reason + "; protected mode needs Landlock ABI %d (Linux 6.12 or newer)" % MIN_ABI)
    if abi < MIN_ABI:
        raise Refusal("landlock", "Landlock ABI %d is too old: protected mode needs ABI %d (Linux 6.12 or newer),"
                      " which keeps signals and abstract UNIX sockets inside the boundary" % (abi, MIN_ABI))
    code = facts.get("seccomp_errno")
    if code is not None:
        raise Refusal("seccomp", "seccomp filters are unavailable (%s)" % os.strerror(code))
    return Host(abi=abi, arch=machine)


def probe():
    """Host, or Refusal when this host cannot carry the boundary. Changes nothing."""
    return assess(host_facts())


def _printable(value):
    text = str(value)
    return text if text.isascii() and text.isprintable() else ascii(text)


def check_inputs(gate_port, writable_paths, provider):
    if isinstance(gate_port, bool) or not isinstance(gate_port, int) or not 1 <= gate_port <= 65535:
        raise Refusal("input", "invalid gate port: %s" % ascii(gate_port))
    if provider not in PROVIDERS:
        raise Refusal("input", "unknown provider: %s" % ascii(provider))
    if isinstance(writable_paths, (str, bytes)) or not all(isinstance(path, str) for path in writable_paths):
        raise Refusal("input", "the writable paths must be a list of strings")


def writable_rules(paths):
    """((resolved path, rights), ...) for the writable paths, then the devices that exist."""
    rules, seen = [], set()

    def add(real, mode):
        if real not in seen:
            seen.add(real)
            rules.append((real, DIR_RIGHTS if stat.S_ISDIR(mode) else FILE_RIGHTS))

    for path in paths:
        if not path.startswith("/") or "\0" in path:
            raise Refusal("path", "a writable path must be absolute: %s" % ascii(path))
        real = os.path.realpath(path)
        try:
            mode = os.stat(real).st_mode
        except OSError as error:
            raise Refusal("path", "the writable path %s cannot be used: %s" % (ascii(path), error.strerror)) from None
        if real == "/":
            raise Refusal("path", "the whole filesystem cannot be writable inside the boundary")
        add(real, mode)
    for device in DEVICES:
        try:
            add(os.path.realpath(device), os.stat(device).st_mode)
        except OSError:
            pass
    return tuple(rules)


def ruleset_plan(abi, gate_port, rules):
    """The single Landlock domain: write layer, gate-port connect, ephemeral bind, both scopes."""
    if abi < MIN_ABI:
        raise Refusal("landlock", "Landlock ABI %d is below the floor of %d" % (abi, MIN_ABI))
    return {"handled_access_fs": DIR_RIGHTS, "handled_access_net": NET_BIND_TCP | NET_CONNECT_TCP,
            "scoped": SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL, "paths": tuple(rules),
            "ports": ((NET_CONNECT_TCP, gate_port), (NET_BIND_TCP, 0)),
            "restrict_flags": RESTRICT_SELF_TSYNC if abi >= TSYNC_ABI else 0}


def assemble(program):
    """Resolve labelled jumps into classic-BPF (code, jt, jf, k) rows."""
    labels, rows = {}, []
    for item in program:
        if item[0] == "label":
            labels[item[1]] = len(rows)
        else:
            rows.append(item)
    out = []
    for index, item in enumerate(rows):
        op = item[0]
        if op == "ld":
            out.append((BPF_LD_W_ABS, 0, 0, item[1]))
        elif op == "and":
            out.append((BPF_ALU_AND_K, 0, 0, item[1]))
        elif op == "ret":
            out.append((BPF_RET_K, 0, 0, item[1]))
        else:
            offsets = []
            for target in item[2:4]:
                offset = 0 if target is None else labels[target] - index - 1
                if not 0 <= offset <= 255:
                    raise ValueError("BPF jump to %s out of range" % target)
                offsets.append(offset)
            code = {"jeq": BPF_JEQ_K, "jge": BPF_JGE_K, "jset": BPF_JSET_K}[op]
            out.append((code, offsets[0], offsets[1], item[1]))
    return out


def seccomp_program(arch):
    """The socket, Fast Open, io_uring and TIOCSTI filter for one architecture's syscall table."""
    table = SYSCALLS[arch]
    program = [("ld", DATA_ARCH), ("jeq", table.audit_arch, None, "kill"), ("ld", DATA_NR)]
    if table.x32:
        program.append(("jge", X32_SYSCALL_BIT, "enosys", None))
    program += [("jeq", table.socket, "socket", None), ("jeq", table.socketpair, "socketpair", None),
                ("jeq", table.sendto, "flags3", None), ("jeq", table.sendmmsg, "flags3", None),
                ("jeq", table.sendmsg, "flags2", None), ("jeq", table.ioctl, "ioctl", None)]
    program += [("jeq", number, "enosys", None) for number in table.io_uring]
    program += [("ret", RET_ALLOW),
                ("label", "socket"), ("ld", data_arg(0)),
                ("jeq", AF_INET, "stream", None), ("jeq", AF_INET6, "stream", None),
                ("jeq", AF_NETLINK, "netlink", "eafnosupport"),
                ("label", "stream"), ("ld", data_arg(1)), ("and", SOCK_TYPE_MASK),
                ("jeq", SOCK_STREAM, None, "eprotonosupport"), ("ld", data_arg(2)),
                ("jeq", 0, "allow", None), ("jeq", IPPROTO_TCP, "allow", "eprotonosupport"),
                ("label", "netlink"), ("ld", data_arg(2)), ("jeq", NETLINK_ROUTE, "allow", "eafnosupport"),
                ("label", "socketpair"), ("ld", data_arg(0)), ("jeq", AF_UNIX, None, "eafnosupport"),
                ("ld", data_arg(1)), ("and", SOCK_TYPE_MASK), ("jeq", SOCK_STREAM, "allow", None),
                ("jeq", SOCK_SEQPACKET, "allow", "eprotonosupport"),
                ("label", "flags3"), ("ld", data_arg(3)), ("jset", MSG_FASTOPEN, "eopnotsupp", "allow"),
                ("label", "flags2"), ("ld", data_arg(2)), ("jset", MSG_FASTOPEN, "eopnotsupp", "allow"),
                ("label", "ioctl"), ("ld", data_arg(1)), ("jeq", TIOCSTI, "eperm", "allow"),
                ("label", "allow"), ("ret", RET_ALLOW),
                ("label", "kill"), ("ret", RET_KILL_PROCESS)]
    for name in ("ENOSYS", "EAFNOSUPPORT", "EPROTONOSUPPORT", "EOPNOTSUPP", "EPERM"):
        program += [("label", name.lower()), ("ret", RET_ERRNO | getattr(errno, name))]
    return assemble(program)


def stdio_sockets(fds=(0, 1, 2)):
    found = []
    for fd in fds:
        try:
            if stat.S_ISSOCK(os.fstat(fd).st_mode):
                found.append(fd)
        except OSError:
            pass
    return found


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class _NetPortAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(_SockFilter))]


def _leftover_descriptors():
    """Descriptors above 2 still open; empty when /proc cannot tell (close_range already ran)."""
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        return []
    left = []
    for name in names:
        fd = int(name)
        if fd > 2:
            try:
                os.fstat(fd)  # the descriptor listdir used is closed again by now
                left.append(fd)
            except OSError:
                pass
    return left


def confine(gate_port, writable_paths, provider):
    """Confine this process (see the module documentation); returns the argv prefix, empty here."""
    check_inputs(gate_port, writable_paths, provider)
    host = probe()
    plan = ruleset_plan(host.abi, gate_port, writable_rules(writable_paths))
    rows = seccomp_program(host.arch)
    sockets = stdio_sockets()
    if sockets:
        raise Refusal("stdio", "descriptor %s of the native is a socket, which the boundary cannot police"
                      % ", ".join(map(str, sockets)))
    # From here on this process changes.
    try:
        _syscall(SYS_CLOSE_RANGE, 3, 0xFFFFFFFF, 0)
    except OSError as error:
        raise Refusal("descriptors", "inherited descriptors could not be closed: %s" % error.strerror) from None
    attr = _RulesetAttr(plan["handled_access_fs"], plan["handled_access_net"], plan["scoped"])
    try:
        ruleset = _syscall(SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    except OSError as error:
        raise Refusal("enforcement", "the Landlock ruleset could not be created: %s" % error.strerror) from None
    try:
        for path, rights in plan["paths"]:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                beneath = ctypes.create_string_buffer(struct.pack("=Qi", rights, fd), 12)
                _syscall(SYS_LANDLOCK_ADD_RULE, ruleset, RULE_PATH_BENEATH, beneath, 0)
            finally:
                os.close(fd)
        for access, port in plan["ports"]:
            _syscall(SYS_LANDLOCK_ADD_RULE, ruleset, RULE_NET_PORT, ctypes.byref(_NetPortAttr(access, port)), 0)
        if _libc().prctl(ctypes.c_int(PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), ctypes.c_ulong(0),
                         ctypes.c_ulong(0), ctypes.c_ulong(0)) != 0:
            code = ctypes.get_errno()
            raise OSError(code, "no_new_privs: " + os.strerror(code))
        _syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset, plan["restrict_flags"])
    except OSError as error:
        raise Refusal("enforcement", "the Landlock domain could not be entered: %s" % error.strerror) from None
    finally:
        os.close(ruleset)
    filters = (_SockFilter * len(rows))(*(_SockFilter(*row) for row in rows))
    program = _SockFprog(len(rows), filters)
    try:
        synced = _syscall(SYSCALLS[host.arch].seccomp, SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC,
                          ctypes.byref(program))
    except OSError as error:
        raise Refusal("enforcement", "the seccomp filter could not be installed: %s" % error.strerror) from None
    if synced != 0:
        raise Refusal("enforcement", "the seccomp filter could not reach thread %d" % synced)
    left = _leftover_descriptors()
    if left:
        raise Refusal("descriptors", "descriptors %s are still open" % ", ".join(map(str, left)))
    return []
