"""NL research launcher: Landlock gate-port boundary plus optional seccomp complement.

Research code only: not product runtime, not an adopted sandbox. Before exec
it refuses socket stdio, closes inherited descriptors, sets no_new_privs,
enters a Landlock domain that allows TCP connect only to the gate port and
scopes abstract UNIX sockets and signals, and (with --seccomp) installs a
socket filter. A requested layer that is unavailable refuses with exit 2;
nothing falls back silently. The Landlock rules govern egress only: listening
and accepting inbound connections stay possible.

python3 -I -B boundary.py --gate-port P [--seccomp none|inet|inet+unix]
        [--keep-fd N]... [--extra-port N]... [--verbose] -- COMMAND ARG...
--keep-fd only models a faulty launcher in the inherited-fd test; --extra-port
only models a broader sibling domain for an outside daemon in natives.py.
"""
import ctypes
import errno
import json
import os
import platform
import stat
import sys

# Landlock uapi (include/uapi/linux/landlock.h, kernel 7.0 = ABI 8).
SYS_CREATE, SYS_ADD_RULE, SYS_RESTRICT = 444, 445, 446  # same on x86_64 and arm64
CREATE_VERSION, CREATE_ERRATA = 1, 2
RULE_NET_PORT = 2
NET_BIND_TCP, NET_CONNECT_TCP = 1, 2
SCOPE_ABSTRACT_UNIX, SCOPE_SIGNAL = 1, 2
RESTRICT_TSYNC = 8
PR_SET_NO_NEW_PRIVS = 38

# seccomp and classic BPF.
SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC = 1, 1
RET_KILL_PROCESS, RET_ERRNO, RET_ALLOW = 0x80000000, 0x00050000, 0x7fff0000
LD_ABS, ALU_AND, JEQ, JGE, JSET, RET = 0x20, 0x54, 0x15, 0x35, 0x45, 0x06
AF_UNIX, AF_INET, AF_INET6, AF_NETLINK = 1, 2, 10, 16
SOCK_STREAM, SOCK_SEQPACKET, IPPROTO_TCP, NETLINK_ROUTE = 1, 5, 6, 0
MSG_FASTOPEN = 0x20000000
X32_BIT = 0x40000000
# Only x86_64 was exercised. The arm64 row is review data for G0 cost (U).
ARCHES = {
    'x86_64': {'audit': 0xc000003e, 'seccomp': 317, 'close_range': 436, 'socket': 41,
               'socketpair': 53, 'sendto': 44, 'sendmsg': 46, 'sendmmsg': 307,
               'io_uring': (425, 426, 427)},
    'aarch64': {'audit': 0xc00000b7, 'seccomp': 277, 'close_range': 436, 'socket': 198,
                'socketpair': 199, 'sendto': 206, 'sendmsg': 211, 'sendmmsg': 269,
                'io_uring': (425, 426, 427)},
}
SECCOMP_MODES = ('none', 'inet', 'inet+unix')


class Refusal(Exception):
    pass


def parse(argv):
    options = {'gate_port': None, 'seccomp': 'none', 'keep': [], 'extra': [], 'verbose': False}
    args = list(argv)
    while args and args[0] != '--':
        flag = args.pop(0)
        if flag == '--verbose':
            options['verbose'] = True
            continue
        if not args:
            raise Refusal('missing value for ' + flag)
        value = args.pop(0)
        if flag == '--gate-port' and value.isdigit() and 0 < int(value) < 65536:
            options['gate_port'] = int(value)
        elif flag == '--seccomp' and value in SECCOMP_MODES:
            options['seccomp'] = value
        elif flag == '--keep-fd' and value.isdigit() and int(value) > 2:
            options['keep'].append(int(value))
        elif flag == '--extra-port' and value.isdigit() and 0 < int(value) < 65536:
            options['extra'].append(int(value))
        else:
            raise Refusal('invalid option ' + flag + ' ' + value)
    if not args or args.pop(0) != '--' or not args:
        raise Refusal('usage: --gate-port P [--seccomp MODE] -- COMMAND')
    if options['gate_port'] is None:
        raise Refusal('--gate-port is required')
    options['command'] = args
    return options


def landlock_plan(abi, gate_port, seccomp, extra=()):
    """Ruleset for one gate port. Scopes need ABI 6; inet seccomp covers abstract UNIX."""
    if abi < 4:
        raise Refusal('Landlock ABI %d lacks TCP port rules (needs 4)' % abi)
    if abi < 6 and seccomp in ('none', 'inet+unix'):
        raise Refusal('Landlock ABI %d lacks abstract UNIX scope (needs 6) for this mode' % abi)
    return {'abi': abi, 'handled_access_net': NET_CONNECT_TCP,
            'scoped': SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL if abi >= 6 else 0,
            'rules': [{'allowed_access': NET_CONNECT_TCP, 'port': port} for port in (gate_port, *extra)],
            'restrict_flags': RESTRICT_TSYNC if abi >= 8 else 0}


def assemble(program):
    """Resolve labelled classic-BPF jumps into (code, jt, jf, k) rows."""
    labels, rows = {}, []
    for item in program:
        if item[0] == 'label':
            labels[item[1]] = len(rows)
        else:
            rows.append(item)
    out = []
    for index, item in enumerate(rows):
        op = item[0]
        if op == 'ld':
            out.append((LD_ABS, 0, 0, item[1]))
        elif op == 'and':
            out.append((ALU_AND, 0, 0, item[1]))
        elif op == 'ret':
            out.append((RET, 0, 0, item[1]))
        else:
            offsets = []
            for target in item[2:4]:
                offset = 0 if target is None else labels[target] - index - 1
                if not 0 <= offset <= 255:
                    raise Refusal('BPF jump out of range')
                offsets.append(offset)
            out.append(({'jeq': JEQ, 'jge': JGE, 'jset': JSET}[op], offsets[0], offsets[1], item[1]))
    return out


def seccomp_program(arch, allow_unix):
    """Allow TCP-only inet sockets and netlink route; deny UDP/raw/MPTCP/other families,
    TCP Fast Open sends and io_uring. AF_UNIX socket() only when allow_unix; without it,
    socketpair(AF_UNIX) only for stream and seqpacket, whose connected ends cannot be
    re-addressed (a datagram or raw pair can sendto()/connect() any outside socket)."""
    table = ARCHES[arch]
    deny = lambda name: RET_ERRNO | getattr(errno, name)
    program = [('ld', 4), ('jeq', table['audit'], None, 'kill'), ('ld', 0),
               ('jge', X32_BIT, 'enosys', None),
               ('jeq', table['socket'], 'socket', None),
               ('jeq', table['socketpair'], 'socketpair', None),
               ('jeq', table['sendto'], 'flags3', None),
               ('jeq', table['sendmmsg'], 'flags3', None),
               ('jeq', table['sendmsg'], 'flags2', None)]
    program += [('jeq', number, 'enosys', None) for number in table['io_uring']]
    program += [('ret', RET_ALLOW),
                ('label', 'socket'), ('ld', 16),
                ('jeq', AF_INET, 'stream', None), ('jeq', AF_INET6, 'stream', None),
                ('jeq', AF_NETLINK, 'netlink', None),
                ('jeq', AF_UNIX, 'allow' if allow_unix else 'eafnosupport', 'eafnosupport'),
                ('label', 'stream'), ('ld', 24), ('and', 0xf),
                ('jeq', SOCK_STREAM, None, 'eprotonosupport'), ('ld', 32),
                ('jeq', 0, 'allow', None), ('jeq', IPPROTO_TCP, 'allow', 'eprotonosupport'),
                ('label', 'netlink'), ('ld', 32), ('jeq', NETLINK_ROUTE, 'allow', 'eafnosupport'),
                ('label', 'socketpair'), ('ld', 16),
                ('jeq', AF_UNIX, 'allow' if allow_unix else 'pairtype', 'eafnosupport'),
                ('label', 'pairtype'), ('ld', 24), ('and', 0xf), ('jeq', SOCK_STREAM, 'allow', None),
                ('jeq', SOCK_SEQPACKET, 'allow', 'eprotonosupport'),
                ('label', 'flags3'), ('ld', 40), ('jset', MSG_FASTOPEN, 'eopnotsupp', 'allow'),
                ('label', 'flags2'), ('ld', 32), ('jset', MSG_FASTOPEN, 'eopnotsupp', 'allow'),
                ('label', 'allow'), ('ret', RET_ALLOW),
                ('label', 'kill'), ('ret', RET_KILL_PROCESS),
                ('label', 'enosys'), ('ret', deny('ENOSYS')),
                ('label', 'eafnosupport'), ('ret', deny('EAFNOSUPPORT')),
                ('label', 'eprotonosupport'), ('ret', deny('EPROTONOSUPPORT')),
                ('label', 'eopnotsupp'), ('ret', deny('EOPNOTSUPP'))]
    return assemble(program)


def stdio_sockets(fds=(0, 1, 2)):
    """Descriptors kept across exec that are sockets: the boundary does not police them."""
    found = []
    for fd in fds:
        try:
            if stat.S_ISSOCK(os.fstat(fd).st_mode):
                found.append(fd)
        except OSError:
            pass
    return found


class Attr(ctypes.Structure):
    _fields_ = [('fs', ctypes.c_uint64), ('net', ctypes.c_uint64), ('scoped', ctypes.c_uint64)]


class PortAttr(ctypes.Structure):
    _fields_ = [('allowed_access', ctypes.c_uint64), ('port', ctypes.c_uint64)]


class Filter(ctypes.Structure):
    _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte), ('jf', ctypes.c_ubyte),
                ('k', ctypes.c_uint)]


class Program(ctypes.Structure):
    _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.POINTER(Filter))]


def apply(options):
    if sys.platform != 'linux':
        raise Refusal('Linux required')
    arch = platform.machine()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def call(*args):
        result = libc.syscall(*args)
        if result < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return result

    try:
        abi = call(SYS_CREATE, None, 0, CREATE_VERSION)
    except OSError as error:
        raise Refusal('Landlock unavailable: ' + error.strerror)
    plan = landlock_plan(abi, options['gate_port'], options['seccomp'], options['extra'])
    if options['seccomp'] != 'none' and arch not in ('x86_64',):
        raise Refusal('seccomp complement not validated on ' + arch)
    sockets = stdio_sockets()
    if sockets:
        raise Refusal('stdio descriptor %s is a socket' % sockets)
    keep = set(options['keep'])
    for name in os.listdir('/proc/self/fd'):
        fd = int(name)
        if fd > 2 and fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
        raise Refusal('no_new_privs failed')
    attr = Attr(0, plan['handled_access_net'], plan['scoped'])
    size = ctypes.sizeof(Attr) if abi >= 6 else 16
    try:
        ruleset = call(SYS_CREATE, ctypes.byref(attr), size, 0)
        for rule in plan['rules']:
            port = PortAttr(rule['allowed_access'], rule['port'])
            call(SYS_ADD_RULE, ruleset, RULE_NET_PORT, ctypes.byref(port), 0)
        call(SYS_RESTRICT, ruleset, plan['restrict_flags'])
        os.close(ruleset)
    except OSError as error:
        raise Refusal('Landlock enforcement failed: ' + error.strerror)
    plan['errata'] = call(SYS_CREATE, None, 0, CREATE_ERRATA)
    plan['seccomp'] = options['seccomp']
    if options['seccomp'] != 'none':
        rows = seccomp_program(arch, options['seccomp'] == 'inet+unix')
        filters = (Filter * len(rows))(*(Filter(*row) for row in rows))
        program = Program(len(rows), filters)
        try:
            call(ARCHES[arch]['seccomp'], SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC,
                 ctypes.byref(program))
        except OSError as error:
            raise Refusal('seccomp enforcement failed: ' + error.strerror)
        plan['seccomp_instructions'] = len(rows)
    plan['kept_fds'] = sorted(keep)
    return plan


def main(argv):
    try:
        options = parse(argv)
        plan = apply(options)
    except Refusal as error:
        print('boundary: refused: ' + str(error), file=sys.stderr, flush=True)
        return 2
    if options['verbose']:
        print('boundary-applied ' + json.dumps(plan, sort_keys=True), file=sys.stderr, flush=True)
    try:
        os.execvp(options['command'][0], options['command'])
    except OSError as error:
        print('boundary: exec failed: ' + error.strerror, file=sys.stderr, flush=True)
        return 127


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
