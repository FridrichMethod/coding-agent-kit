"""KLC research: outer filesystem write rule as a replacement for native sandboxes.

Enters a Landlock domain that handles every write-class filesystem right and
allows them only beneath the given directories, then execs COMMAND (normally
NL's boundary.py, which stacks the network domain and seccomp). Reads and
execution stay unrestricted. Refuses (exit 2) if Landlock or a path is
unavailable; never falls back.

python3 -I -B fsrules.py --write DIR [--write DIR]... [--write-file PATH]... -- COMMAND ARG...
"""
import ctypes
import os
import sys

SYS_CREATE, SYS_ADD_RULE, SYS_RESTRICT = 444, 445, 446
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38
# Write-class rights (ABI 1-3); IOCTL_DEV (ABI 5) is not handled so terminal ioctls stay usable.
WRITE_FILE, REMOVE_DIR, REMOVE_FILE, MAKE_CHAR, MAKE_DIR, MAKE_REG = 1 << 1, 1 << 4, 1 << 5, 1 << 6, 1 << 7, 1 << 8
MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM, REFER, TRUNCATE = 1 << 9, 1 << 10, 1 << 11, 1 << 12, 1 << 13, 1 << 14
DIR_RIGHTS = (WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO
              | MAKE_BLOCK | MAKE_SYM | REFER | TRUNCATE)
FILE_RIGHTS = WRITE_FILE | TRUNCATE


class Attr(ctypes.Structure):
    _fields_ = [('fs', ctypes.c_uint64), ('net', ctypes.c_uint64), ('scoped', ctypes.c_uint64)]


class PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def parse(argv):
    dirs, files, args = [], [], list(argv)
    while args and args[0] != '--':
        flag, value = args.pop(0), args.pop(0)
        (dirs if flag == '--write' else files if flag == '--write-file' else None).append(value)
    if not args or args.pop(0) != '--' or not args:
        raise SystemExit('usage: fsrules.py --write DIR ... -- COMMAND')
    return dirs, files, args


def main(argv):
    dirs, files, command = parse(argv)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def call(*args):
        result = libc.syscall(*args)
        if result < 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        return result

    try:
        abi = call(SYS_CREATE, None, 0, 1)
        handled = DIR_RIGHTS if abi >= 3 else DIR_RIGHTS & ~TRUNCATE
        if abi < 2:
            handled &= ~REFER
        attr = Attr(handled, 0, 0)
        ruleset = call(SYS_CREATE, ctypes.byref(attr), 8, 0)  # fs field only
        for path, rights in [(d, handled) for d in dirs] + [(f, FILE_RIGHTS & handled) for f in files]:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                call(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, ctypes.byref(PathBeneath(rights, fd)), 0)
            finally:
                os.close(fd)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), 'no_new_privs')
        call(SYS_RESTRICT, ruleset, 0)
        os.close(ruleset)
    except OSError as error:
        print('fsrules: refused: %s' % error.strerror, file=sys.stderr, flush=True)
        return 2
    print('fsrules-applied abi=%d handled=%#x dirs=%d files=%d' % (abi, handled, len(dirs), len(files)),
          file=sys.stderr, flush=True)
    os.execvp(command[0], command)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
