"""KLX outer filesystem write rules: one Landlock layer that allows writes only beneath named paths.

Research code only (not product runtime, not an adopted sandbox). It models the K brief's
replacement for the native tool sandbox in protected mode: the workspace and temporary
directories stay writable and the rest of the filesystem becomes read-only. Reads and execution
are not restricted. It sets no_new_privs, enters the Landlock domain and execs the rest of the
command line, normally NL's boundary.py, whose network layer stacks on top of this one. An
unavailable layer or a missing path refuses with exit 2; nothing falls back silently. Descriptors
opened before the exec (the PTY) keep working: Landlock checks access when a file is opened.

python3 -I -B fsrules.py [--write PATH]... [--verbose] -- COMMAND ARG...
"""
import ctypes
import json
import os
import stat
import struct
import sys

SYS_CREATE, SYS_ADD_RULE, SYS_RESTRICT = 444, 445, 446  # same on x86_64 and arm64
CREATE_VERSION, RULE_PATH_BENEATH, PR_SET_NO_NEW_PRIVS = 1, 1, 38
# include/uapi/linux/landlock.h filesystem access rights.
WRITE_FILE, REMOVE_DIR, REMOVE_FILE = 1 << 1, 1 << 4, 1 << 5
MAKE_CHAR, MAKE_DIR, MAKE_REG, MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = (1 << bit for bit in range(6, 13))
REFER, TRUNCATE = 1 << 13, 1 << 14  # ABI 2 and ABI 3
FILE_RIGHTS = WRITE_FILE | TRUNCATE  # the write rights a rule on a non-directory may carry


class Refusal(Exception):
    pass


def write_mask(abi):
    """Every write-type right the running ABI knows; read, execute and device ioctl stay unhandled."""
    if abi < 1:
        raise Refusal('Landlock unavailable')
    mask = WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR | MAKE_REG | MAKE_SOCK | MAKE_FIFO \
        | MAKE_BLOCK | MAKE_SYM
    if abi >= 2:
        mask |= REFER
    if abi >= 3:
        mask |= TRUNCATE
    return mask


def plan(abi, paths, kinds):
    """(handled mask, [(path, allowed)]): directories get every write right, other files only the
    file rights. `kinds` maps each path to 'dir' or 'file'."""
    mask = write_mask(abi)
    rules = []
    for path in paths:
        if path not in kinds:
            raise Refusal('writable path missing: ' + path)
        rules.append((path, mask if kinds[path] == 'dir' else mask & FILE_RIGHTS))
    return mask, rules


def parse(argv):
    paths, verbose, args = [], False, list(argv)
    while args and args[0] != '--':
        flag = args.pop(0)
        if flag == '--verbose':
            verbose = True
        elif flag == '--write' and args:
            paths.append(args.pop(0))
        else:
            raise Refusal('invalid option ' + flag)
    if not args or args.pop(0) != '--' or not args:
        raise Refusal('usage: [--write PATH]... -- COMMAND')
    return paths, verbose, args


class Attr(ctypes.Structure):
    _fields_ = [('fs', ctypes.c_uint64), ('net', ctypes.c_uint64), ('scoped', ctypes.c_uint64)]


def path_attr(allowed, fd):
    """struct landlock_path_beneath_attr: u64 allowed_access, s32 parent_fd, packed (12 bytes)."""
    return ctypes.create_string_buffer(struct.pack('=Qi', allowed, fd), 12)


def apply(paths):
    if sys.platform != 'linux':
        raise Refusal('Linux required')
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
    kinds = {}
    for path in paths:
        try:
            kinds[path] = 'dir' if stat.S_ISDIR(os.stat(path).st_mode) else 'file'
        except OSError:
            pass
    mask, rules = plan(abi, paths, kinds)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
        raise Refusal('no_new_privs failed')
    attr = Attr(mask, 0, 0)
    size = ctypes.sizeof(Attr) if abi >= 6 else 16 if abi >= 4 else 8
    try:
        ruleset = call(SYS_CREATE, ctypes.byref(attr), size, 0)
        for path, allowed in rules:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                call(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, path_attr(allowed, fd), 0)
            finally:
                os.close(fd)
        call(SYS_RESTRICT, ruleset, 0)
        os.close(ruleset)
    except OSError as error:
        raise Refusal('Landlock filesystem enforcement failed: ' + error.strerror)
    return {'abi': abi, 'handled_access_fs': mask, 'writable': [path for path, _ in rules]}


def main(argv):
    try:
        paths, verbose, command = parse(argv)
        applied = apply(paths)
    except Refusal as error:
        print('fsrules: refused: ' + str(error), file=sys.stderr, flush=True)
        return 2
    if verbose:
        print('fsrules-applied ' + json.dumps(applied, sort_keys=True), file=sys.stderr, flush=True)
    try:
        os.execvp(command[0], command)
    except OSError as error:
        print('fsrules: exec failed: ' + error.strerror, file=sys.stderr, flush=True)
        return 127


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
