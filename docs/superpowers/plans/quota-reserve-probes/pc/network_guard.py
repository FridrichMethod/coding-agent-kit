"""Linux x86_64 research-only guard: loopback TCP and Unix sockets only.

Not a security boundary for adversarial programs (sockaddr has a ptrace TOCTOU).
Used with trusted native Codex and fabricated, fixed provider configuration.
No runtime dependency. Run: python3 -I -B network_guard.py COMMAND ARG...
"""
import ctypes as c
import errno
import json
import os
import platform
import signal
import sys

LIBC = c.CDLL(None, use_errno=True)
LIBC.ptrace.restype = c.c_long
TRACEME, PEEKDATA, CONT, GETREGS, SETREGS = 0, 2, 7, 12, 13
SETOPTIONS, GETEVENTMSG = 0x4200, 0x4201
OPTIONS = 2 | 4 | 8 | 16 | 128 | (1 << 20)  # fork/vfork/clone/exec/seccomp/EXITKILL


class Regs(c.Structure):
    _fields_ = [(name, c.c_ulonglong) for name in (
        'r15 r14 r13 r12 rbp rbx r11 r10 r9 r8 rax rcx rdx rsi rdi '
        'orig_rax rip cs eflags rsp ss fs_base gs_base ds es fs gs').split()]


class Filter(c.Structure):
    _fields_ = [('code', c.c_ushort), ('jt', c.c_ubyte), ('jf', c.c_ubyte), ('k', c.c_uint)]


class Program(c.Structure):
    _fields_ = [('len', c.c_ushort), ('filter', c.POINTER(Filter))]


def ptrace(request, pid, addr=0, data=0):
    c.set_errno(0)
    result = LIBC.ptrace(c.c_uint(request), c.c_uint(pid), c.c_void_p(addr),
                         c.c_void_p(data) if isinstance(data, int) else data)
    if result == -1 and c.get_errno():
        raise OSError(c.get_errno(), os.strerror(c.get_errno()))
    return result


def install_filter():
    # Reject other ABIs. Trace socket creation/connect/destination send; block
    # io_uring (which can submit network work without these ordinary syscalls).
    rows = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xc000003e), (6, 0, 0, 0x80000000),
            (0x20, 0, 0, 0)]
    for number in (41, 42, 44, 46, 307, 425, 426, 427):
        rows += [(0x15, 0, 1, number), (6, 0, 0, 0x7ff00000)]
    rows += [(0x35, 0, 1, 0x40000000), (6, 0, 0, 0x80000000),
             (6, 0, 0, 0x7fff0000)]
    filters = (Filter * len(rows))(*(Filter(*row) for row in rows))
    program = Program(len(rows), filters)
    if LIBC.prctl(38, 1, 0, 0, 0) or LIBC.prctl(22, 2, c.byref(program), 0, 0):
        raise OSError(c.get_errno(), 'seccomp setup failed')


def allowed(pid, regs):
    nr = regs.orig_rax
    if nr == 41:
        return regs.rdi == 1 or (regs.rdi == 2 and regs.rsi & 15 == 1)
    if nr in (42, 44):
        address = regs.rsi if nr == 42 else regs.r8
        length = regs.rdx if nr == 42 else regs.r9
        if not address:
            return nr == 44  # connected send; prior connect checked
        if length < 2:
            return False
        value = ptrace(PEEKDATA, pid, address) & ((1 << 64) - 1)
        raw = value.to_bytes(8, 'little')
        family = int.from_bytes(raw[:2], 'little')
        return family == 1 or (family == 2 and length >= 16 and raw[4:8] == b'\x7f\0\0\1')
    return False


def main(command):
    if sys.platform != 'linux' or platform.machine() != 'x86_64' or not command:
        raise SystemExit('requires Linux x86_64 and a command')
    root = os.fork()
    if root == 0:
        try:
            ptrace(TRACEME, 0)
            os.kill(os.getpid(), signal.SIGSTOP)
            install_filter()
            os.execvp(command[0], command)
        except BaseException as error:
            os.write(2, ('guard child: ' + str(error) + '\n').encode())
            os._exit(125)
    owned = {root}
    root_status = 125
    denied = {}
    try:
        os.waitpid(root, 0)
        ptrace(SETOPTIONS, root, 0, OPTIONS)
        ptrace(CONT, root)
        while owned:
            pid, status = os.waitpid(-1, 0x40000000)
            if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                owned.discard(pid)
                if pid == root:
                    root_status = os.waitstatus_to_exitcode(status)
                continue
            event = status >> 16
            deliver = os.WSTOPSIG(status)
            if event in (1, 2, 3):
                child = c.c_ulong()
                ptrace(GETEVENTMSG, pid, 0, c.byref(child))
                owned.add(child.value)
            if event == 7:
                regs = Regs()
                ptrace(GETREGS, pid, 0, c.byref(regs))
                try:
                    permit = allowed(pid, regs)
                except OSError:
                    permit = False
                if not permit:
                    number = str(regs.orig_rax)
                    denied[number] = denied.get(number, 0) + 1
                    regs.orig_rax = (1 << 64) - 1
                    regs.rax = (1 << 64) - errno.ENETUNREACH
                    ptrace(SETREGS, pid, 0, c.byref(regs))
            if event or deliver in (signal.SIGSTOP, signal.SIGTRAP):
                deliver = 0
            try:
                ptrace(CONT, pid, 0, deliver)
            except ProcessLookupError:
                pass
    finally:
        for pid in owned:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        print(json.dumps({'guard_denied_syscalls': denied, 'root_exit': root_status}), file=sys.stderr)
    return root_status if root_status >= 0 else 128 - root_status


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
