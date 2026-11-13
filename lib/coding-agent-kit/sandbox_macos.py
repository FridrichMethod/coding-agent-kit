"""coding-agent-kit macOS boundary: one Seatbelt profile around the native and every descendant.

Protected mode starts the native as /usr/bin/sandbox-exec -p PROFILE NATIVE
ARGS. Every descendant inherits the profile, and a confined process cannot
apply another one, so it can neither loosen nor replace it. The profile:

- allows everything by default, then denies all networking except outbound
  TCP to localhost:GATE_PORT (`remote tcp`: a `remote ip` rule would admit
  UDP to the gate's port number), so every bind is denied too;
- denies launchd job creation, LaunchServices open and Apple Events;
- denies mach lookups outside the eleven MACH_SERVICES, which include
  com.apple.SecurityServer for the native Keychain;
- denies signals to processes outside the sandbox and the TIOCSTI ioctl,
  which would queue input for the unconfined shell that owns the terminal;
- denies every file write except beneath the writable paths, the terminal
  devices and, for Claude, its fixed per-user temporary directory
  /private/tmp/claude-UID; writes beneath WRITABLE/Library/LaunchAgents and
  /private/var/at stay denied. /dev/ttys* is writable because PTY tools
  open new terminals by path, so a process inside can write to every
  terminal of the user, not only its own. A writable path that is not
  printable ASCII, or holds a quote or a backslash, refuses protected mode.

`localhost` in a Seatbelt address also admits the wildcard and configured
local addresses (interface addresses, loopback aliases) on the gate port, so
the launcher holds the port on every address holder_addresses() lists, for
the whole invocation. The profile governs egress and delegation, not reads.

SBPL is an undocumented Apple interface and sandbox-exec is deprecated. The
profile is the one reviewed on the releases in REVIEWED_RELEASES, other
releases refuse, and the startup self-test turns a semantic change it checks
into a refusal.

Public API (load this file by path; it is never on sys.path):

    Refusal(kind, message)
        Protected mode cannot start. str() is the message for the user, kind
        one of KINDS.
    probe() -> Host
        Whether this host can carry the boundary, without changing anything:
        Host(version, arch), or Refusal with the reason.
    holder_addresses(ifconfig=IFCONFIG, timeout=10) -> Hold
        The addresses the launcher must hold the gate port on: Hold.listen
        (127.0.0.1 and ::1, listening) and Hold.reserve (0.0.0.0, :: and every
        other address from `ifconfig -a`, loopback aliases such as 127.0.0.2
        included, bound with SO_REUSEADDR and, for IPv6, IPV6_V6ONLY, without
        listening), each ("inet" | "inet6", address); IPv6 addresses keep
        their %scope. A hold that fails, EADDRINUSE included, refuses
        protected mode. Refusal("ifconfig") when the list cannot be read.
        Addresses that appear later are not covered.
    self_test(gate_port, writable_paths, provider, *, python=sys.executable,
              timeout=SELF_TEST_TIMEOUT) -> Tested
        Runs a probe under the profile for these arguments, while the gate
        port is held: it must connect to the gate port and be denied (EPERM)
        another loopback port, a bind, UDP to the gate port, a write outside
        the writable paths and TIOCSTI. Returns Tested(profile, answers) or
        raises Refusal("self-test"). The launcher runs it before any native
        process under the profile.
    confine(gate_port, writable_paths, provider, *, tested) -> list[str]
        In the child that is about to exec the native, just before exec, with
        the native's terminal on fd 0-2: renders the profile again and
        refuses ("self-test") unless it equals tested.profile, so only the
        profile the self-test checked runs; refuses socket stdio; closes
        every other descriptor (Seatbelt does not police sockets that
        already exist) and returns the argv prefix [SANDBOX_EXEC, "-p",
        PROFILE] to put before the native command. Every refusal but
        "descriptors" comes before anything changes and leaves a status
        pipe open; after "descriptors" every descriptor above 2 may be
        closed, so the supervisor learns of it only from the exit status
        and what the child writes to fd 0-2, and the child exits without
        starting the native. writable_paths come from the adapter and never
        include HOME or an ancestor of it, which would reopen HOME-wide
        delegation (shell rc files, HOME/Library/LaunchAgents for an
        ancestor); this module never reads HOME, so the caller checks.
    render_profile(gate_port, writable_paths, provider, uid) -> str
        The profile for resolved writable paths (pure).
    resolve_writable(paths) -> list[str]
        The writable paths resolved, each existing and not "/".
    assess(facts) -> Host, host_facts() -> dict, check_inputs(...),
    parse_ifconfig(text), self_test_failures(result), close_inherited(),
    stdio_sockets(fds), claude_temp(uid)
        The pure rules and helpers behind the calls above.

Supported: macOS releases in REVIEWED_RELEASES on Apple silicon, with
/usr/bin/sandbox-exec and /sbin/ifconfig.
"""

from dataclasses import dataclass
import ipaddress
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile


SANDBOX_EXEC = "/usr/bin/sandbox-exec"
IFCONFIG = "/sbin/ifconfig"
LISTEN = (("inet", "127.0.0.1"), ("inet6", "::1"))  # Hold.listen; holder_addresses() reserves every other address
PROVIDERS = ("codex", "claude")
ARCHES = ("arm64",)
REVIEWED_RELEASES = ((27, 2),)  # (major, minor); a kit release that reviews another one adds it
KINDS = ("platform", "architecture", "version", "sandbox-exec", "ifconfig", "input", "path", "stdio", "descriptors",
         "self-test")
TAG = "coding-agent-kit"
SELF_TEST_TIMEOUT = 20.0
# Where the self-test's write check makes its probe directory, outside every writable path.
PROBE_BASES = ("/private/tmp", "/private/var/tmp")
MACH_SERVICES = (
    "com.apple.FSEvents",
    "com.apple.PowerManagement.control",
    "com.apple.SecurityServer",
    "com.apple.SystemConfiguration.DNSConfiguration",
    "com.apple.SystemConfiguration.configd",
    "com.apple.cfprefsd.agent",
    "com.apple.cfprefsd.daemon",
    "com.apple.logd",
    "com.apple.system.notification_center",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.system.opendirectoryd.membership",
)
# Writable for every native: the terminal, its PTYs and the descriptor aliases.
DEVICE_RULES = ('(literal "/dev/null")', '(literal "/dev/tty")', '(literal "/dev/ptmx")',
                '(regex #"^/dev/ttys[0-9]+$")', '(literal "/dev/dtracehelper")', '(regex #"^/dev/fd/")')
# Links into /private on macOS: Seatbelt matches the resolved path, so a rule under them never matches.
UNRESOLVED = ("/tmp", "/var", "/etc")
CHILD_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
SELF_TEST_EXPECTED = {"gate": "connected", "other": "errno:1", "bind": "errno:1", "udp": "errno:1",
                      "write": "errno:1", "tiocsti": "errno:1"}
SELF_TEST_PROBLEMS = {"gate": "the gate port is unreachable", "other": "another loopback port is not denied",
                      "bind": "binding a port is not denied", "udp": "UDP to the gate port is not denied",
                      "write": "a write outside the writable paths is not denied",
                      "tiocsti": "terminal input injection (TIOCSTI) is not denied"}

# Runs under the profile: python3 -I -B -c SELF_TEST '{"gate": P, "other": Q, "probe": DIR}'.
SELF_TEST = r"""
import fcntl, json, os, socket, sys, termios
spec = json.loads(sys.argv[1])
result = {}

def attempt(name, action):
    try:
        result[name] = action()
    except OSError as error:
        result[name] = "errno:%d" % (error.errno or 0)

def connect(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect(("127.0.0.1", port))
        return "connected"

def bind():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return "bound"

def udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b"x", ("127.0.0.1", spec["gate"]))
        return "sent"

def write():
    os.close(os.open(os.path.join(spec["probe"], "write-test"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    return "written"

attempt("gate", lambda: connect(spec["gate"]))
attempt("other", lambda: connect(spec["other"]))
attempt("bind", bind)
attempt("udp", udp)
attempt("write", write)
master, slave = os.openpty()
pid = os.fork()
if pid == 0:
    try:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    except OSError:
        os._exit(5)
    try:
        fcntl.ioctl(slave, termios.TIOCSTI, b"x")
    except OSError as error:
        os._exit(20 + min(error.errno or 0, 200))
    os._exit(10)
_, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
result["tiocsti"] = "injected" if code == 10 else "errno:%d" % (code - 20) if code >= 20 else "exit:%d" % code
print(json.dumps(result), flush=True)
"""

_SAFE = re.compile(r"[\x20-\x7e]*")
_VERSION = re.compile(r"(\d+)(?:\.(\d+))?(?:\.\d+)?")
_SCOPE = re.compile(r"[A-Za-z0-9]+")
_LISTENED = frozenset(ipaddress.ip_address(address) for _, address in LISTEN)


class Refusal(Exception):
    """Protected mode cannot start; str() is the message for the user."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class Host:
    version: str
    arch: str


@dataclass(frozen=True)
class Hold:
    listen: tuple
    reserve: tuple


@dataclass(frozen=True)
class Tested:
    """A profile that passed the self-test, and the probe's answers under it."""

    profile: str
    answers: dict


def _executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def host_facts():
    """What assess() decides on. Reads the system version and two file modes; changes nothing."""
    darwin = sys.platform == "darwin"
    return {"system": platform.system(), "machine": platform.machine(),
            "version": platform.mac_ver()[0] if darwin else "", "sandbox_exec": darwin and _executable(SANDBOX_EXEC),
            "ifconfig": darwin and _executable(IFCONFIG)}


def _release(version):
    match = _VERSION.fullmatch(version) if isinstance(version, str) else None
    return (int(match[1]), int(match[2] or 0)) if match else None


def assess(facts):
    """Host or Refusal for facts as host_facts() returns them."""
    if facts.get("system") != "Darwin":
        raise Refusal("platform", "the macOS boundary needs macOS")
    machine = facts.get("machine")
    if machine not in ARCHES:
        raise Refusal("architecture", "macOS protected mode is reviewed on Apple silicon (arm64) only, not on %s"
                      % _clean(machine))
    version = facts.get("version")
    release = _release(version)
    reviewed = ", ".join("%d.%d" % item for item in REVIEWED_RELEASES)
    if release is None:
        raise Refusal("version", "the macOS version could not be read")
    if release not in REVIEWED_RELEASES:
        if release > max(REVIEWED_RELEASES):
            raise Refusal("version", "macOS %s is newer than the newest reviewed release (%s); a kit update has to"
                          " review its sandbox first" % (version, reviewed))
        raise Refusal("version", "macOS %s is not a reviewed release (reviewed: %s)" % (version, reviewed))
    if not facts.get("sandbox_exec"):
        raise Refusal("sandbox-exec", "%s is missing or not executable" % SANDBOX_EXEC)
    if not facts.get("ifconfig"):
        raise Refusal("ifconfig", "%s is missing or not executable; the launcher needs it to hold the gate port on"
                      " every interface address" % IFCONFIG)
    return Host(version=version, arch=machine)


def probe():
    """Host, or Refusal when this host cannot carry the boundary. Changes nothing."""
    return assess(host_facts())


def _clean(value, limit=80):
    """Printable ASCII for a message, every other character as ?, cut to limit."""
    text = "".join(char if " " <= char <= "~" else "?" for char in str(value))
    return text if len(text) <= limit else text[:limit] + "..."


def check_inputs(gate_port, writable_paths, provider):
    if isinstance(gate_port, bool) or not isinstance(gate_port, int) or not 1 <= gate_port <= 65535:
        raise Refusal("input", "invalid gate port: %s" % ascii(gate_port))
    if provider not in PROVIDERS:
        raise Refusal("input", "unknown provider: %s" % ascii(provider))
    if isinstance(writable_paths, (str, bytes)) or not all(isinstance(path, str) for path in writable_paths):
        raise Refusal("input", "the writable paths must be a list of strings")


def claude_temp(uid):
    """Claude's fixed per-user temporary directory, which it uses whatever TMPDIR says."""
    return "/private/tmp/claude-%d" % uid


def resolve_writable(paths):
    """The writable paths resolved through links, in order, without duplicates."""
    if isinstance(paths, (str, bytes)) or not all(isinstance(path, str) for path in paths):
        raise Refusal("input", "the writable paths must be a list of strings")
    resolved = []
    for path in paths:
        if not path.startswith("/") or "\0" in path:
            raise Refusal("path", "a writable path must be absolute: %s" % ascii(path))
        real = os.path.realpath(path)
        try:
            os.stat(real)
        except OSError as error:
            raise Refusal("path", "the writable path %s cannot be used: %s" % (ascii(path), error.strerror)) from None
        if real == "/":
            raise Refusal("path", "the whole filesystem cannot be writable inside the boundary")
        if real not in resolved:
            resolved.append(real)
    return resolved


def _quote(text):
    if not _SAFE.fullmatch(text) or '"' in text or "\\" in text:
        raise Refusal("path", "the macOS boundary cannot name %s: only printable ASCII without quotes or"
                      " backslashes" % ascii(text))
    return '"' + text + '"'


def _profile_path(path):
    if not path.startswith("/") or path.startswith("//") or os.path.normpath(path) != path:
        raise Refusal("path", "a writable path must be absolute and normalized: %s" % ascii(path))
    if path == "/":
        raise Refusal("path", "the whole filesystem cannot be writable inside the boundary")
    if any(path == top or path.startswith(top + "/") for top in UNRESOLVED):
        raise Refusal("path", "%s must be resolved: macOS links /tmp, /var and /etc into /private" % ascii(path))
    _quote(path)
    return path


def render_profile(gate_port, writable_paths, provider, uid):
    """The SBPL profile (see the module documentation) for resolved writable paths."""
    check_inputs(gate_port, writable_paths, provider)
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise Refusal("input", "invalid uid: %s" % ascii(uid))
    writable = sorted({_profile_path(path) for path in writable_paths})
    allowed = sorted(set(writable) | ({claude_temp(uid)} if provider == "claude" else set()))
    message = "(with message %s)" % _quote(TAG)
    lines = ["(version 1)", "(allow default)", "(deny network* %s)" % message,
             '(allow network-outbound (remote tcp "localhost:%d"))' % gate_port]
    lines += ["(deny %s %s)" % (operation, message) for operation in ("job-creation", "lsopen", "appleevent-send")]
    lines.append("(deny mach-lookup %s)" % message)
    lines += ["(allow mach-lookup (global-name %s))" % _quote(name) for name in MACH_SERVICES]
    lines += ["(deny signal %s)" % message, "(allow signal (target same-sandbox))",
              "(deny file-ioctl (ioctl-command TIOCSTI) %s)" % message, '(deny file-write* (subpath "/"))',
              "(allow file-write* %s)" % " ".join(["(subpath %s)" % _quote(path) for path in allowed]
                                                  + list(DEVICE_RULES))]
    denied = [path + "/Library/LaunchAgents" for path in writable] + ["/private/var/at"]
    lines.append("(deny file-write* %s)" % " ".join("(subpath %s)" % _quote(path) for path in denied))
    return "\n".join(lines) + "\n"


def parse_ifconfig(text):
    """(("inet" | "inet6", address), ...) of `ifconfig -a`, without 127.0.0.1, ::1 and malformed lines.

    127.0.0.1 and ::1 are left out because the launcher listens there
    (Hold.listen). Every other address stays, loopback aliases such as
    127.0.0.2 included: Seatbelt's `localhost` admits configured local
    addresses, and an address left to the wildcard hold alone can be bound by
    an outside process that sets SO_REUSEADDR.
    """
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("inet", "inet6"):
            continue
        literal, sep, scope = parts[1].partition("%")
        if sep and not _SCOPE.fullmatch(scope):
            continue
        try:
            address = ipaddress.ip_address(literal)
        except ValueError:
            continue
        if address.version != (4 if parts[0] == "inet" else 6) or address in _LISTENED:
            continue
        entry = (parts[0], parts[1])
        if entry not in found:
            found.append(entry)
    return tuple(found)


def holder_addresses(ifconfig=IFCONFIG, timeout=10.0):
    """Hold(listen, reserve): every address the launcher holds the gate port on."""
    try:
        done = subprocess.run([ifconfig, "-a"], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                              env=dict(CHILD_ENV))
    except subprocess.TimeoutExpired:
        raise Refusal("ifconfig", "%s -a did not finish within %g s" % (ifconfig, timeout)) from None
    except OSError as error:
        raise Refusal("ifconfig", "%s could not run: %s" % (ifconfig, error.strerror)) from None
    if done.returncode != 0:
        raise Refusal("ifconfig", "%s -a failed with exit status %d" % (ifconfig, done.returncode))
    return Hold(listen=LISTEN,
                reserve=(("inet", "0.0.0.0"), ("inet6", "::")) + parse_ifconfig(done.stdout.decode("utf-8", "replace")))


def self_test_failures(result):
    """Why a self-test result fails, one line each; empty when it passes."""
    if not isinstance(result, dict):
        return ["the self-test gave no result"]
    if "error" in result:
        return [_clean(result["error"], 200)]
    return ["%s (%s)" % (SELF_TEST_PROBLEMS[key], _clean(result.get(key, "no answer")))
            for key, expected in SELF_TEST_EXPECTED.items() if result.get(key) != expected]


def _beneath(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _probe_dir(bases, allowed):
    for base in bases:
        if not os.path.isdir(base) or any(_beneath(os.path.realpath(base), path) for path in allowed):
            continue
        try:
            return tempfile.mkdtemp(prefix="coding-agent-kit-self-test-", dir=base)
        except OSError:
            continue
    raise Refusal("self-test", "the self-test found no directory outside the writable paths for its write check")


def _kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _run_probe(argv, timeout):
    try:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=dict(CHILD_ENV), close_fds=True, start_new_session=True)
    except OSError as error:
        return {"error": "%s could not run: %s" % (argv[0], error.strerror)}
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        return {"error": "the self-test did not finish within %g s" % timeout}
    lines = stdout.decode("utf-8", "replace").strip().splitlines()
    if process.returncode == 0 and lines:
        try:
            result = json.loads(lines[-1])
        except ValueError:
            result = None
        if isinstance(result, dict):
            return result
    detail = stderr.decode("utf-8", "replace").strip().splitlines()
    return {"error": "the self-test exited with status %d%s" % (process.returncode,
                                                                ": " + detail[-1] if detail else "")}


def self_test(gate_port, writable_paths, provider, *, python=sys.executable, timeout=SELF_TEST_TIMEOUT,
              sandbox_exec=SANDBOX_EXEC, probe_bases=PROBE_BASES):
    """Tested(profile, answers) for the profile confine() renders, or Refusal("self-test")."""
    check_inputs(gate_port, writable_paths, provider)
    uid = os.getuid()
    writable = resolve_writable(writable_paths)
    profile = render_profile(gate_port, writable, provider, uid)
    allowed = writable + ([claude_temp(uid)] if provider == "claude" else [])
    probe_dir = _probe_dir(probe_bases, allowed)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
            other.bind(("127.0.0.1", 0))
            other.listen(8)
            spec = json.dumps({"gate": gate_port, "other": other.getsockname()[1], "probe": probe_dir})
            result = _run_probe([sandbox_exec, "-p", profile, python, "-I", "-B", "-c", SELF_TEST, spec], timeout)
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    failures = self_test_failures(result)
    if failures:
        raise Refusal("self-test", "the boundary self-test failed: " + "; ".join(failures))
    return Tested(profile=profile, answers=result)


def stdio_sockets(fds=(0, 1, 2)):
    found = []
    for fd in fds:
        try:
            if stat.S_ISSOCK(os.fstat(fd).st_mode):
                found.append(fd)
        except OSError:
            pass
    return found


def _open_descriptors():
    try:
        names = os.listdir("/dev/fd")
    except OSError as error:
        raise Refusal("descriptors", "the open descriptors could not be listed: %s" % error.strerror) from None
    found = []
    for name in names:
        if name.isdigit() and int(name) > 2:
            try:
                os.fstat(int(name))  # the descriptor listdir used is closed again by now
                found.append(int(name))
            except OSError:
                pass
    return found


def close_inherited():
    """Close every descriptor above 2; Refusal("descriptors") when one stays open."""
    for fd in _open_descriptors():
        try:
            os.close(fd)
        except OSError:
            pass
    left = _open_descriptors()
    if left:
        raise Refusal("descriptors", "descriptors %s are still open" % ", ".join(map(str, left)))


def confine(gate_port, writable_paths, provider, *, tested=None):
    """Prepare this process for the tested profile (see the module documentation); returns the argv prefix."""
    check_inputs(gate_port, writable_paths, provider)
    if not isinstance(tested, Tested):
        raise Refusal("input", "confine() needs the Tested result of a passing self_test()")
    probe()
    profile = render_profile(gate_port, resolve_writable(writable_paths), provider, os.getuid())
    if profile != tested.profile:
        raise Refusal("self-test", "the profile for this native differs from the one the self-test checked")
    sockets = stdio_sockets()
    if sockets:
        raise Refusal("stdio", "descriptor %s of the native is a socket, which the boundary cannot police"
                      % ", ".join(map(str, sockets)))
    close_inherited()
    return [SANDBOX_EXEC, "-p", profile]
