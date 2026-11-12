"""Offline checks of the NL research launcher's pure policy logic.

A small classic-BPF interpreter evaluates the seccomp program on synthetic
seccomp_data, so these tests need no kernel feature and change no process.
"""
import errno
import importlib.util
import os
from pathlib import Path
import socket
import struct
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('nl_boundary', HERE / 'boundary.py')
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
X86 = boundary.ARCHES['x86_64']


def run_bpf(rows, nr, args=(0,) * 6, arch=X86['audit']):
    args = (tuple(args) + (0,) * 6)[:6]
    data = struct.pack('<IIQ6Q', nr, arch, 0, *[a & (2 ** 64 - 1) for a in args])
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = rows[pc]
        if code == boundary.LD_ABS:
            acc = struct.unpack_from('<I', data, k)[0]
        elif code == boundary.ALU_AND:
            acc &= k
        elif code == boundary.RET:
            return k
        else:
            taken = {boundary.JEQ: acc == k, boundary.JGE: acc >= k,
                     boundary.JSET: bool(acc & k)}[code]
            pc += jt if taken else jf
        pc += 1


def decision(value):
    if value == boundary.RET_ALLOW:
        return 'allow'
    if value == boundary.RET_KILL_PROCESS:
        return 'kill'
    # errno.errorcode maps 95 to ENOTSUP on Linux; EOPNOTSUPP is the same value.
    return {errno.EOPNOTSUPP: 'EOPNOTSUPP'}.get(value & 0xffff, errno.errorcode[value & 0xffff])


class SeccompProgram(unittest.TestCase):
    def check(self, allow_unix, nr, args, expected):
        rows = boundary.seccomp_program('x86_64', allow_unix)
        self.assertEqual(decision(run_bpf(rows, nr, args)), expected)

    def test_tcp_sockets_allowed_with_flags(self):
        for family in (boundary.AF_INET, boundary.AF_INET6):
            for kind in (1, 1 | 0o2000000 | 0o4000):  # SOCK_CLOEXEC | SOCK_NONBLOCK
                for proto in (0, 6):
                    self.check(False, X86['socket'], (family, kind, proto), 'allow')

    def test_udp_raw_mptcp_and_unknown_families_denied(self):
        cases = [((2, 2, 0), 'EPROTONOSUPPORT'), ((10, 2 | 0o4000, 17), 'EPROTONOSUPPORT'),
                 ((2, 3, 1), 'EPROTONOSUPPORT'), ((2, 1, 262), 'EPROTONOSUPPORT'),
                 ((2, 5, 132), 'EPROTONOSUPPORT'), ((17, 3, 0), 'EAFNOSUPPORT'),
                 ((40, 1, 0), 'EAFNOSUPPORT'), ((16, 3, 9), 'EAFNOSUPPORT')]
        for args, expected in cases:
            self.check(True, X86['socket'], args, expected)

    def test_unix_socket_mode_and_socketpair(self):
        self.check(False, X86['socket'], (1, 1, 0), 'EAFNOSUPPORT')
        self.check(True, X86['socket'], (1, 1, 0), 'allow')
        self.check(False, X86['socketpair'], (1, 1, 0), 'allow')
        self.check(False, X86['socketpair'], (2, 1, 0), 'EAFNOSUPPORT')
        self.check(False, X86['socket'], (16, 3, 0), 'allow')

    def test_socketpair_types_without_unix(self):
        cloexec, nonblock, high = 0o2000000, 0o4000, 1 << 32
        for kind in (1, 5, 1 | cloexec, 5 | nonblock | cloexec, high | 1):
            self.check(False, X86['socketpair'], (1, kind, 0), 'allow')
        # Datagram and raw (a datagram alias for AF_UNIX) pairs can sendto()/connect() outside.
        for kind in (2, 3, 2 | cloexec, 3 | nonblock, high | 2, 0, 4, 6, 10):
            self.check(False, X86['socketpair'], (1, kind, 0), 'EPROTONOSUPPORT')
        for kind in (1, 2, 3, 5):
            self.check(True, X86['socketpair'], (1, kind, 0), 'allow')
            self.check(False, X86['socketpair'], (10, kind, 0), 'EAFNOSUPPORT')

    def test_fastopen_and_io_uring_denied_others_allowed(self):
        self.check(False, X86['sendto'], (3, 0, 5, boundary.MSG_FASTOPEN, 0, 0), 'EOPNOTSUPP')
        self.check(False, X86['sendmmsg'], (3, 0, 1, boundary.MSG_FASTOPEN | 0x40, 0, 0), 'EOPNOTSUPP')
        self.check(False, X86['sendmsg'], (3, 0, boundary.MSG_FASTOPEN, 0, 0, 0), 'EOPNOTSUPP')
        self.check(False, X86['sendto'], (3, 0, 5, 0x40, 0, 0), 'allow')
        for nr in X86['io_uring']:
            self.check(False, nr, (0,) * 6, 'ENOSYS')
        for nr in (0, 1, 42, 59, 435):
            self.check(False, nr, (0,) * 6, 'allow')

    def test_foreign_arch_killed_and_x32_denied(self):
        rows = boundary.seccomp_program('x86_64', False)
        self.assertEqual(decision(run_bpf(rows, 41, (2, 2, 0), arch=0x40000003)), 'kill')
        self.assertEqual(decision(run_bpf(rows, 41 | boundary.X32_BIT, (2, 2, 0))), 'ENOSYS')

    def test_arm64_table_assembles(self):
        rows = boundary.seccomp_program('aarch64', False)
        arm = boundary.ARCHES['aarch64']
        self.assertEqual(decision(run_bpf(rows, arm['socket'], (2, 2, 0), arch=arm['audit'])),
                         'EPROTONOSUPPORT')
        self.assertEqual(decision(run_bpf(rows, arm['socket'], (2, 1, 0), arch=arm['audit'])), 'allow')


class LandlockPlan(unittest.TestCase):
    def test_abi8_plan_scopes_and_tsync(self):
        plan = boundary.landlock_plan(8, 4100, 'none')
        self.assertEqual(plan['handled_access_net'], boundary.NET_CONNECT_TCP)
        self.assertEqual(plan['scoped'], 3)
        self.assertEqual(plan['rules'], [{'allowed_access': 2, 'port': 4100}])
        self.assertEqual(plan['restrict_flags'], boundary.RESTRICT_TSYNC)

    def test_refusals(self):
        with self.assertRaises(boundary.Refusal):
            boundary.landlock_plan(3, 4100, 'inet')
        for mode in ('none', 'inet+unix'):
            with self.assertRaises(boundary.Refusal):
                boundary.landlock_plan(5, 4100, mode)
        plan = boundary.landlock_plan(4, 4100, 'inet')
        self.assertEqual((plan['scoped'], plan['restrict_flags']), (0, 0))

    def test_parse(self):
        options = boundary.parse(['--gate-port', '9', '--seccomp', 'inet', '--keep-fd', '7',
                                  '--extra-port', '80', '--', 'x', '-y'])
        self.assertEqual((options['gate_port'], options['seccomp'], options['keep'], options['extra'],
                          options['command']), (9, 'inet', [7], [80], ['x', '-y']))
        plan = boundary.landlock_plan(8, 9, 'inet', options['extra'])
        self.assertEqual([rule['port'] for rule in plan['rules']], [9, 80])
        for argv in ([], ['--', 'x'], ['--gate-port', '0', '--', 'x'], ['--gate-port', '9'],
                     ['--gate-port', '9', '--seccomp', 'all', '--', 'x'], ['--gate-port', '9', '--'],
                     ['--keep-fd', '2', '--gate-port', '9', '--', 'x']):
            with self.assertRaises(boundary.Refusal):
                boundary.parse(argv)

    def test_main_refuses_without_exec(self):
        self.assertEqual(boundary.main(['--gate-port', '9']), 2)

    def test_socket_stdio_detected(self):
        first, second = socket.socketpair()
        reader, writer = os.pipe()
        try:
            fds = (first.fileno(), reader, writer, second.fileno())
            self.assertEqual(boundary.stdio_sockets(fds), [first.fileno(), second.fileno()])
        finally:
            for item in (first, second):
                item.close()
            for fd in (reader, writer):
                os.close(fd)


if __name__ == '__main__':
    unittest.main()
