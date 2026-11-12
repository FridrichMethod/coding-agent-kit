"""NL host facts and candidate refusal probes; writes evidence/host.json.

Reads kernel and policy facts, asks the kernel which Landlock access bits it
accepts (rulesets are created and closed, never enforced here), and runs the
smallest refusal checks for bubblewrap, unshare, the Codex sandbox helper and
systemd user-manager properties. systemd checks create transient collected
units named quota-nl-hf-* that only connect to this script's loopback listener.
No installation, privilege, credential or real network use.
"""
import ctypes
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError as error:
        return 'unreadable: ' + type(error).__name__


def command(args, timeout=20, **kwargs):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL, start_new_session=True, **kwargs)
        return {'exit': result.returncode, 'stdout': result.stdout.strip()[-400:],
                'stderr': result.stderr.strip()[-400:]}
    except FileNotFoundError:
        return {'exit': None, 'absent': True}
    except subprocess.TimeoutExpired:
        return {'exit': 124, 'timeout': True}


def landlock_bits():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    class Attr(ctypes.Structure):
        _fields_ = [('fs', ctypes.c_uint64), ('net', ctypes.c_uint64), ('scoped', ctypes.c_uint64)]

    def accepted(fs=0, net=0, scoped=0):
        attr = Attr(fs, net, scoped)
        fd = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(Attr), 0)
        if fd >= 0:
            os.close(fd)
            return True
        return False

    abi = libc.syscall(444, None, 0, 1)
    errata = libc.syscall(444, None, 0, 2)
    return {'abi': abi, 'errata_bitmask': errata,
            'fs_bits_accepted': [b for b in range(24) if accepted(fs=1 << b)],
            'net_bits_accepted': [b for b in range(8) if accepted(net=1 << b)],
            'scope_bits_accepted': [b for b in range(8) if accepted(scoped=1 << b)]}


def systemd_checks(root):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    seen = []

    def serve():
        while True:
            try:
                peer, _ = listener.accept()
            except OSError:
                return
            with peer:
                peer.settimeout(2)
                try:
                    seen.append(peer.recv(64).decode().strip())
                except OSError:
                    pass
    threading.Thread(target=serve, daemon=True).start()
    nonce = '%d' % (time.time_ns() % 10 ** 8)
    checks, units = {}, []
    variants = [('service_IPAddressDeny_any', ['--wait', '-p', 'IPAddressDeny=any']),
                ('service_PrivateNetwork_yes', ['--wait', '-p', 'PrivateNetwork=yes']),
                ('service_RestrictAddressFamilies_AF_UNIX', ['--wait', '-p', 'RestrictAddressFamilies=AF_UNIX']),
                ('scope_IPAddressDeny_any', ['--scope', '-p', 'IPAddressDeny=any']),
                ('scope_RestrictAddressFamilies_AF_UNIX', ['--scope', '-p', 'RestrictAddressFamilies=AF_UNIX'])]
    for name, extra in variants:
        unit = 'quota-nl-hf-%s-%d' % (nonce, len(units))
        units.append(unit)
        client = ('import socket; s=socket.create_connection(("127.0.0.1",%d),2); s.sendall(b"%s")'
                  % (port, name))
        result = command(['systemd-run', '--user', '--collect', '--quiet', '--unit', unit, *extra, '--',
                          sys.executable, '-I', '-B', '-c', client], timeout=30)
        time.sleep(.3)
        result['listener_reached'] = name in seen
        checks[name] = result
    listener.close()
    after = command(['systemctl', '--user', 'show', *units, '--property=LoadState'])
    return {'checks': checks, 'units_after': sorted(set(after.get('stdout', '').split())),
            'unit_count': len(units)}


def codex_checks(root):
    binary = shutil.which('codex')
    if not binary:
        return {'absent': True}
    binary = str(Path(binary).resolve())
    for part in ('home', 'codex', 'ws'):
        (root / part).mkdir(exist_ok=True)
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'), 'CODEX_HOME': str(root / 'codex'),
           'TMPDIR': '/tmp'}
    probe = 'import socket; print("inner command ran")'
    out = {'version': command([binary, '--version'], env=env)['stdout']}
    variants = {'default': [],
                'legacy_landlock': ['--enable', 'use_legacy_landlock'],
                'network_enabled': ['-c', 'sandbox_mode="workspace-write"',
                                    '-c', 'sandbox_workspace_write.network_access=true']}
    strace = shutil.which('strace')
    for name, extra in variants.items():
        trace = root / ('strace-%s.log' % name)
        wrapped = [sys.executable, '-I', '-B', str(HERE / 'boundary.py'), '--gate-port', '9',
                   '--seccomp', 'none', '--', binary, 'sandbox', *extra, '--',
                   sys.executable, '-I', '-c', probe]
        if strace:
            wrapped = [strace, '-f', '-qq', '-e', 'trace=connect,sendto,sendmsg', '-e', 'signal=none',
                       '-o', str(trace), *wrapped]
        result = command(wrapped, timeout=40, env=env, cwd=root / 'ws')
        lines = trace.read_text().splitlines() if trace.exists() else []
        result['non_loopback_inet_attempts'] = [x for x in lines if ('AF_INET' in x and '127.0.0.1' not in x
                                                                      and '::1' not in x)][:5]
        result['observer'] = 'strace' if strace else 'none'
        result['stderr'] = '\n'.join(x for x in result.get('stderr', '').splitlines()
                                     if 'PATH aliases' not in x)
        out[name] = result
    return out


def main():
    root = Path(tempfile.mkdtemp(prefix='quota-nl-hf-'))
    print('scratch', root, flush=True)
    facts = {}
    try:
        release = dict(line.split('=', 1) for line in read('/etc/os-release').splitlines() if '=' in line)
        facts['host'] = {'machine': platform.machine(), 'kernel_release': platform.release(),
                         'os': release.get('PRETTY_NAME', '').strip('"'),
                         'wsl': 'microsoft' in read('/proc/version').lower(),
                         'lsm': read('/sys/kernel/security/lsm')}
        facts['sysctl'] = {name: read('/proc/sys/' + name) for name in (
            'kernel/apparmor_restrict_unprivileged_userns', 'kernel/unprivileged_userns_clone',
            'user/max_user_namespaces', 'kernel/yama/ptrace_scope', 'net/mptcp/enabled',
            'net/ipv4/tcp_fastopen', 'net/ipv4/ping_group_range', 'net/ipv6/conf/lo/disable_ipv6')}
        facts['landlock'] = landlock_bits()
        header = Path('/usr/src/linux-hwe-7.0-headers-%s' % platform.release().rsplit('-', 1)[0]) / \
            'include/uapi/linux/landlock.h'
        text = header.read_text() if header.exists() else ''
        facts['landlock']['uapi_header'] = {
            'present': bool(text), 'mentions_tsync': 'LANDLOCK_RESTRICT_SELF_TSYNC' in text,
            'mentions_udp': 'UDP' in text, 'mentions_resolve_unix': 'RESOLVE_UNIX' in text}
        facts['tools'] = {name: bool(shutil.which(name)) for name in (
            'bwrap', 'unshare', 'slirp4netns', 'pasta', 'socat', 'systemd-run', 'strace', 'firejail',
            'nsjail', 'srt', 'setpriv', 'curl', 'git', 'ssh', 'node', 'codex', 'claude')}
        facts['tools']['setpriv_landlock'] = 'landlock' in command(['setpriv', '--help']).get('stdout', '').lower()
        facts['refusals'] = {
            'bwrap_version': command(['bwrap', '--version']).get('stdout'),
            'bwrap_unshare_net': command(['bwrap', '--ro-bind', '/', '/', '--unshare-net', '--', '/bin/true']),
            'bwrap_unshare_user': command(['bwrap', '--ro-bind', '/', '/', '--unshare-user', '--', '/bin/true']),
            'unshare_map_root_net': command(['unshare', '-rn', '/bin/true']),
            'unshare_user_only': command(['unshare', '-U', '/bin/true']),
        }
        facts['codex_sandbox'] = codex_checks(root)
        facts['systemd_user'] = systemd_checks(root)
        facts['systemd_user']['version'] = command(['systemd-run', '--version']).get('stdout', '').splitlines()[:1]
    finally:
        text = json.dumps(facts, indent=2, sort_keys=True)
        for old, new in ((str(root), '<PROBE_ROOT>'), (str(Path.home()), '<HOME>'),
                         (sys.executable, '<PYTHON>'), (platform.node(), '<HOSTNAME>')):
            text = text.replace(old, new)
        (HERE / 'evidence').mkdir(exist_ok=True)
        (HERE / 'evidence' / 'host.json').write_text(text + '\n')
        subprocess.run(['rm', '-rf', '--', str(root)], check=False)
        print('removed', root, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
