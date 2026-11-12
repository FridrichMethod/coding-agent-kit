"""NM macOS Seatbelt outer-boundary probes; run on the Mac only via run.py.

python3 -I -B mac_probe.py ROOT STAGE TOOLS_JSON [DEADLINE_SECONDS]

ROOT is a fresh private temp root (created by run.py with mktemp). Every
native and client gets HOME, CLAUDE_CONFIG_DIR, CODEX_HOME, XDG_* and TMPDIR
inside ROOT, fabricated credentials, and loopback fixtures only. Results go to
ROOT/out/STAGE.json; run.py copies and sanitizes them. Every child is bounded
and its process group is killed; detached fixtures are reaped by ROOT path.
DEADLINE_SECONDS, when given, bounds the whole stage: SIGALRM then aborts it,
records it as incomplete and still runs the cleanup.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pty
import pwd
import secrets
import select
import shlex
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time

HERE = Path(os.path.realpath(__file__)).parent


def _load(name):
    spec = importlib.util.spec_from_file_location('nm_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SBPL = _load('sbpl')
GATE = _load('gate')
SANDBOX_EXEC = '/usr/bin/sandbox-exec'

# Mach services the enforced profile admits. Chosen from the census stage
# (allow-with-report) and kept only when needed for the probed flows; see
# the report. Escape services in sbpl.ESCAPE_SERVICES are always refused.
ENFORCED_MACH = (
    'com.apple.system.opendirectoryd.libinfo',
    'com.apple.system.opendirectoryd.membership',
    'com.apple.system.notification_center',
    'com.apple.system.logger',
    'com.apple.logd',
    'com.apple.diagnosticd',
    'com.apple.bsd.dirhelper',
    'com.apple.cfprefsd.daemon',
    'com.apple.cfprefsd.agent',
    'com.apple.trustd',
    'com.apple.trustd.agent',
    'com.apple.SystemConfiguration.configd',
    'com.apple.SystemConfiguration.DNSConfiguration',
    'com.apple.FSEvents',
    'com.apple.PowerManagement.control',
)
STORE = 'com.apple.SecurityServer'
# Recommended candidate: only the services that census runs actually looked up
# (trustd can fetch OCSP/CRL/AIA URLs for a client, so it is left out).
MINIMAL_MACH = tuple(s for s in ENFORCED_MACH if s not in (
    'com.apple.trustd', 'com.apple.trustd.agent', 'com.apple.diagnosticd', 'com.apple.system.logger',
    'com.apple.bsd.dirhelper'))
# Fabricated TLS fixture name; reserved TLD, reached only through the gate's CONNECT tunnel.
TLS_HOST = 'nm-tls.test'
TLS_CONFIG = """[req]
distinguished_name = dn
prompt = no
[dn]
CN = NM Fabricated Test CA
[ca]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
[leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:%s
authorityKeyIdentifier = keyid
""" % TLS_HOST


class Ctx:
    def __init__(self, root, stage, tools):
        self.root = Path(os.path.realpath(root))
        self.stage = stage
        self.tools = tools
        self.python = os.path.realpath(sys.executable)
        self.out = self.root / 'out'
        self.groups = []
        self.data = {'stage': stage, 'started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                     'host': {'macos': subprocess.run(['/usr/bin/sw_vers', '-productVersion'],
                              capture_output=True, text=True).stdout.strip(),
                              'arch': os.uname().machine}, 'cases': {}, 'profiles': {}}
        self.real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.redact = {}  # host-specific strings (the host address) replaced before saving
        for name in ('out', 'home', 'cfg', 'codex', 'workspace', 'tmp', 'xdg', 'cache', 'state',
                     'data', 'outside', 'private', 'anchors/LaunchAgents', 'shim'):
            (self.root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.nm = self.root / 'nm'

    def tag(self):
        return 'qnm-' + secrets.token_hex(6)

    def policy(self, kind, gate_port, tag, **extra):
        base = {'tag': tag, 'gate_ports': [gate_port] if gate_port else [],
                'private_socket_dirs': [str(self.root / 'private')],
                'deny_write': [str(self.root / 'anchors'), '/private/var/at',
                               os.path.realpath(self.real_home) + '/Library/LaunchAgents']}
        if kind == 'census':
            base.update(deny_mach=False, report_mach=True,
                        mach_deny=sorted(set(SBPL.ESCAPE_SERVICES) | {STORE}))
        elif kind == 'enforced':
            base.update(deny_mach=True, mach_allow=list(ENFORCED_MACH))
        elif kind == 'enforced+store':
            base.update(deny_mach=True, mach_allow=list(ENFORCED_MACH) + [STORE])
        elif kind == 'hardened':
            base.update(deny_mach=True, mach_allow=list(ENFORCED_MACH), deny_signal_others=True,
                        deny_tiocsti=True)
        elif kind == 'recommended':
            base.update(deny_mach=True, mach_allow=list(MINIMAL_MACH), deny_signal_others=True,
                        deny_tiocsti=True, gate_tcp_only=True)
        elif kind == 'm1':  # recommended without the unused private Unix-socket allowance
            base.update(deny_mach=True, mach_allow=list(MINIMAL_MACH), deny_signal_others=True,
                        deny_tiocsti=True, gate_tcp_only=True, private_socket_dirs=[])
        elif kind == 'network-only':
            base.update(deny_mach=False, deny_launch=False, deny_write=[])
        else:
            raise ValueError(kind)
        base.update(extra)
        text = SBPL.render(base)
        self.data['profiles'][kind + ':' + tag] = text
        return text

    def env(self, **extra):
        r = self.root
        env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(r / 'home'), 'TMPDIR': str(r / 'tmp') + '/',
               'USER': 'nmfixture', 'LOGNAME': 'nmfixture', 'SHELL': '/bin/bash', 'LANG': 'en_US.UTF-8',
               'TERM': 'xterm-256color', 'XDG_CONFIG_HOME': str(r / 'xdg'), 'XDG_CACHE_HOME': str(r / 'cache'),
               'XDG_STATE_HOME': str(r / 'state'), 'XDG_DATA_HOME': str(r / 'data'),
               'CLAUDE_CONFIG_DIR': str(r / 'cfg'), 'CODEX_HOME': str(r / 'codex')}
        env.update(extra)
        return env

    def run(self, argv, profile=None, timeout=20, env=None, pass_fds=(), cwd=None, stdin=subprocess.DEVNULL):
        if profile is not None:
            argv = [SANDBOX_EXEC, '-p', profile] + list(argv)
        began = time.monotonic()
        proc = subprocess.Popen(argv, env=env or self.env(), cwd=cwd or str(self.root / 'workspace'),
                                stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                pass_fds=pass_fds, close_fds=True, start_new_session=True)
        self.groups.append(proc.pid)
        try:
            out, err = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            self.kill(proc.pid)
            out, err = proc.communicate()
            timed_out = True
        return {'code': proc.returncode, 'timed_out': timed_out, 'seconds': round(time.monotonic() - began, 2),
                'stdout': out.decode(errors='replace')[-4000:], 'stderr': err.decode(errors='replace')[-2000:]}

    def kill(self, pgid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(.3)

    def logs(self, since, tag):
        time.sleep(2.5)
        result = subprocess.run(['/usr/bin/log', 'show', '--start', since, '--style', 'ndjson',
                                 '--predicate', 'eventMessage CONTAINS "%s"' % tag],
                                capture_output=True, text=True, timeout=90)
        rows = SBPL.parse_log_ndjson(result.stdout, tag)
        return {'log_exit': result.returncode, 'summary': SBPL.summarize_reports(rows),
                'processes': sorted({r['process'] for r in rows})}

    def leftovers(self):
        result = subprocess.run(['/bin/ps', '-axo', 'pid=,pgid=,command='], capture_output=True, text=True)
        mine, me = [], os.getpid()
        for line in result.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and str(self.root) in parts[2] and int(parts[0]) != me \
                    and 'mac_probe.py' not in parts[2]:
                mine.append({'pid': int(parts[0]), 'command': parts[2][:200]})
        return mine

    def cleanup(self):
        for pgid in self.groups:
            self.kill(pgid)
        found = self.leftovers()
        for item in found:
            try:
                os.kill(item['pid'], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(.3)
        self.data['leftover_processes_killed'] = found
        self.data['leftover_processes_after'] = self.leftovers()

    def save(self):
        self.data['finished'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        path = self.out / (self.stage + '.json')
        data = SBPL.sanitize(self.data, self.redact) if self.redact else self.data
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def now_for_log():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - 1))


class Listener:
    """Outside-sandbox fixture: TCP v4+v6 on one port, optional UDP, or Unix."""

    def __init__(self, kind, path=None, port=0):
        self.kind, self.hits, self.lock = kind, [], threading.Lock()
        self.socks = []
        if kind in ('tcp', 'udp'):
            stype = socket.SOCK_STREAM if kind == 'tcp' else socket.SOCK_DGRAM
            for _ in range(50):
                first = socket.socket(socket.AF_INET, stype)
                first.bind(('127.0.0.1', port))
                self.port = first.getsockname()[1]
                second = socket.socket(socket.AF_INET6, stype)
                second.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    second.bind(('::1', self.port))
                    break
                except OSError:
                    first.close()
                    second.close()
                if port:
                    raise OSError('requested loopback port %d is busy' % port)
            else:
                raise OSError('no common IPv4/IPv6 loopback port')
            for s in (first, second):
                self.socks.append(s)
                if kind == 'tcp':
                    s.listen(64)
                    threading.Thread(target=self._accept, args=(s,), daemon=True).start()
                else:
                    threading.Thread(target=self._recv, args=(s,), daemon=True).start()
        else:
            if os.path.lexists(path):
                os.unlink(path)  # a stale fixture socket from an earlier run in this root
            dgram = kind == 'unix_dgram'
            s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM if dgram else socket.SOCK_STREAM)
            s.bind(path)
            if not dgram:
                s.listen(16)
            self.path = path
            self.socks.append(s)
            threading.Thread(target=self._recv if dgram else self._accept, args=(s,), daemon=True).start()

    def _note(self, data):
        with self.lock:
            self.hits.append(data[:64].decode(errors='replace'))

    def _accept(self, sock):
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            conn.settimeout(2)
            try:
                data = conn.recv(256)
            except OSError:
                data = b''
            self._note(data or b'<connect>')
            if data.startswith(b'GET ') or data.startswith(b'HEAD '):
                try:
                    conn.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok')
                except OSError:
                    pass
            conn.close()

    def _recv(self, sock):
        while True:
            try:
                data, _ = sock.recvfrom(256)
            except OSError:
                return
            self._note(data)

    def count(self):
        with self.lock:
            return len(self.hits)

    def close(self):
        for s in self.socks:
            s.close()


def client_cmd(ctx, spec):
    return [ctx.python, '-I', '-B', str(ctx.nm / 'client.py'), json.dumps(spec)]


def standard_attempts(gate, alt, udp, unix_path, private):
    return [
        {'name': 'gate_tcp4', 'kind': 'tcp4', 'host': '127.0.0.1', 'port': gate},
        {'name': 'gate_tcp6', 'kind': 'tcp6', 'host': '::1', 'port': gate},
        {'name': 'gate_mapped6', 'kind': 'tcp6', 'host': '::ffff:127.0.0.1', 'port': gate},
        {'name': 'alt_tcp4', 'kind': 'tcp4', 'host': '127.0.0.1', 'port': alt},
        {'name': 'alt_tcp6', 'kind': 'tcp6', 'host': '::1', 'port': alt},
        {'name': 'alt_mapped6', 'kind': 'tcp6', 'host': '::ffff:127.0.0.1', 'port': alt},
        {'name': 'alt_udp4', 'kind': 'udp4', 'host': '127.0.0.1', 'port': udp},
        {'name': 'alt_udp6', 'kind': 'udp6', 'host': '::1', 'port': udp},
        {'name': 'outside_unix', 'kind': 'unix', 'path': unix_path},
        {'name': 'private_unix_self', 'kind': 'unix_self', 'path': private},
    ]


def parse_json(text):
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def stage_boundary(ctx, kinds=('census', 'enforced')):
    gate, alt, udp = Listener('tcp'), Listener('tcp'), Listener('udp')
    unix = Listener('unix', str(ctx.root / 'outside' / 'daemon.sock'))
    attempts = standard_attempts(gate.port, alt.port, udp.port, unix.path, str(ctx.root / 'private'))
    spec = {'attempts': attempts}
    shell_cmd = ' '.join(shlex.quote(x) for x in client_cmd(ctx, spec))
    node_spec = {'attempts': [
        {'name': 'gate_tcp4', 'kind': 'tcp4', 'host': '127.0.0.1', 'port': gate.port},
        {'name': 'gate_tcp6', 'kind': 'tcp6', 'host': '::1', 'port': gate.port},
        {'name': 'alt_tcp4', 'kind': 'tcp4', 'host': '127.0.0.1', 'port': alt.port},
        {'name': 'alt_tcp6', 'kind': 'tcp6', 'host': '::1', 'port': alt.port},
        {'name': 'alt_udp4', 'kind': 'udp4', 'host': '127.0.0.1', 'port': udp.port},
        {'name': 'alt_fetch', 'kind': 'fetch', 'url': 'http://127.0.0.1:%d/' % alt.port},
        {'name': 'gate_fetch', 'kind': 'fetch', 'url': 'http://127.0.0.1:%d/' % gate.port}]}
    try:
        for kind in kinds:
            tag = ctx.tag()
            since = now_for_log()
            profile = ctx.policy(kind, gate.port, tag)
            cases = {}
            before = {'alt': alt.count(), 'udp': udp.count(), 'unix': unix.count()}
            cases['root_python'] = ctx.run(client_cmd(ctx, spec), profile)
            cases['shell_child_python'] = ctx.run(['/bin/sh', '-c', shell_cmd], profile)
            cases['env_i_absolute_python'] = ctx.run(['/usr/bin/env', '-i'] + client_cmd(ctx, spec), profile)
            cases['env_i_absolute_node'] = ctx.run(['/usr/bin/env', '-i', ctx.tools['node'],
                                                    str(ctx.nm / 'client.mjs'), json.dumps(node_spec)], profile)
            detach = str(ctx.root / 'out' / ('detached-' + kind + '.json'))
            cases['detached_setsid_python'] = ctx.run(client_cmd(ctx, dict(spec, detach=detach)), profile)
            deadline = time.monotonic() + 15
            while not os.path.exists(detach) and time.monotonic() < deadline:
                time.sleep(.1)
            cases['detached_setsid_python']['detached_result'] = (
                json.loads(Path(detach).read_text()) if os.path.exists(detach) else None)
            cases['curl_alt4'] = ctx.run(['/usr/bin/curl', '-sS', '-m', '3', '-o', '/dev/null', '-w', '%{http_code}',
                                          'http://127.0.0.1:%d/' % alt.port], profile)
            cases['curl_alt6'] = ctx.run(['/usr/bin/curl', '-sS', '-m', '3', '-o', '/dev/null', '-w', '%{http_code}',
                                          'http://[::1]:%d/' % alt.port], profile)
            cases['nc_alt4'] = ctx.run(['/usr/bin/nc', '-z', '-G', '2', '127.0.0.1', str(alt.port)], profile)
            cases['nc_gate4'] = ctx.run(['/usr/bin/nc', '-z', '-G', '2', '127.0.0.1', str(gate.port)], profile)
            during = {'alt': alt.count() - before['alt'], 'udp': udp.count() - before['udp'],
                      'unix': unix.count() - before['unix']}
            control = ctx.run(client_cmd(ctx, spec))
            after = {'alt': alt.count() - before['alt'] - during['alt'],
                     'udp': udp.count() - before['udp'] - during['udp'],
                     'unix': unix.count() - before['unix'] - during['unix']}
            for case in cases.values():
                case['parsed'] = parse_json(case['stdout'])
            ctx.data['cases'][kind] = {'sandboxed': cases, 'listener_hits_from_sandboxed': during,
                                       'outside_control': dict(control, parsed=parse_json(control['stdout'])),
                                       'listener_hits_from_control': after, 'log': ctx.logs(since, tag)}
    finally:
        for listener in (gate, alt, udp, unix):
            listener.close()


def stage_ipc(ctx):
    unix = Listener('unix', str(ctx.root / 'outside' / 'ipc-daemon.sock'))
    tag = ctx.tag()
    since = now_for_log()
    profile = ctx.policy('enforced', None, tag)
    cases = {}
    services = list(SBPL.ESCAPE_SERVICES) + [STORE, 'com.apple.system.opendirectoryd.libinfo']
    lookups = {'attempts': [{'name': s, 'kind': 'mach', 'service': s} for s in services]}
    cases['mach_lookup_sandboxed'] = ctx.run(client_cmd(ctx, lookups), profile)
    cases['mach_lookup_control'] = ctx.run(client_cmd(ctx, lookups))
    for name in ('mach_lookup_sandboxed', 'mach_lookup_control'):
        cases[name]['parsed'] = parse_json(cases[name]['stdout'])
    scheme = 'qnm-nonexistent-%s://probe' % secrets.token_hex(4)
    cases['open_url_sandboxed'] = ctx.run(['/usr/bin/open', '-g', '-j', scheme], profile, timeout=15)
    cases['open_url_control'] = ctx.run(['/usr/bin/open', '-g', '-j', scheme], timeout=15)
    label = 'quota-nm-' + secrets.token_hex(6)
    marker = ctx.root / 'outside' / 'launchd-marker'
    job = '/bin/echo ran > %s; exec /bin/sleep 300' % shlex.quote(str(marker))
    cases['launchctl_submit_sandboxed'] = ctx.run(['/bin/launchctl', 'submit', '-l', label, '--',
                                                   '/bin/sh', '-c', job], profile, timeout=15)
    time.sleep(2)
    listed = subprocess.run(['/bin/launchctl', 'list', label], capture_output=True, text=True, timeout=15)
    removed = subprocess.run(['/bin/launchctl', 'remove', label], capture_output=True, text=True, timeout=15)
    cases['launchctl_cleanup'] = {'label_listed_code': listed.returncode, 'remove_code': removed.returncode,
                                  'marker_written': marker.exists(), 'label': label}
    cases['launchctl_list_sandboxed'] = ctx.run(['/bin/launchctl', 'list'], profile, timeout=15)
    cases['launchctl_list_sandboxed']['stdout'] = '<omitted: %d lines>' % len(
        cases['launchctl_list_sandboxed']['stdout'].splitlines())
    cases['outside_daemon_unix'] = ctx.run(client_cmd(ctx, {'attempts': [
        {'name': 'outside_unix', 'kind': 'unix', 'path': unix.path},
        {'name': 'private_unix_self', 'kind': 'unix_self', 'path': str(ctx.root / 'private')},
        {'name': 'outside_unix_self', 'kind': 'unix_self', 'path': str(ctx.root / 'outside')}]}), profile)
    cases['outside_daemon_unix']['parsed'] = parse_json(cases['outside_daemon_unix']['stdout'])
    cases['outside_daemon_hits'] = unix.count()
    anchor = ctx.root / 'anchors' / 'LaunchAgents' / 'quota-nm.plist'
    cases['anchor_write_sandboxed'] = ctx.run(['/usr/bin/touch', str(anchor)], profile)
    cases['anchor_write_sandboxed']['file_exists'] = anchor.exists()
    cases['workspace_write_sandboxed'] = ctx.run(['/usr/bin/touch', str(ctx.root / 'workspace' / 'ok')], profile)
    cases['workspace_write_sandboxed']['file_exists'] = (ctx.root / 'workspace' / 'ok').exists()
    cases['dns_sandboxed'] = ctx.run(client_cmd(ctx, {'attempts': [
        {'name': 'localhost', 'kind': 'dns', 'host': 'localhost'},
        {'name': 'numeric', 'kind': 'dns', 'host': '127.0.0.1'},
        {'name': 'invalid_name', 'kind': 'dns', 'host': 'quota-nm.invalid'}]}), profile)
    cases['dns_sandboxed']['parsed'] = parse_json(cases['dns_sandboxed']['stdout'])
    cases['setuid_ps_sandboxed'] = ctx.run(['/bin/ps', '-p', '1', '-o', 'pid='], profile)
    cases['setuid_ps_shell_child'] = ctx.run(['/bin/sh', '-c', '/bin/ps -p 1 -o pid=; echo "rc=$?"'], profile)
    cases['setuid_ps_control'] = ctx.run(['/bin/ps', '-p', '1', '-o', 'pid='])
    cases['setuid_modes'] = {p: oct(os.stat(p).st_mode & 0o7777) for p in (
        '/bin/ps', '/usr/bin/crontab', '/usr/bin/at', '/usr/bin/batch', '/usr/bin/sudo')}
    sleeper = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
    ctx.groups.append(sleeper.pid)
    cases['signal0_outside_sandboxed'] = ctx.run(['/bin/kill', '-0', str(sleeper.pid)], profile)
    tag2 = ctx.tag()
    hardened = ctx.policy('enforced', None, tag2, deny_signal_others=True)
    cases['signal0_outside_hardened'] = ctx.run(['/bin/kill', '-0', str(sleeper.pid)], hardened)
    cases['signal_same_sandbox_hardened'] = ctx.run(['/bin/sh', '-c', '/bin/sleep 30 & p=$!; kill $p; wait $p; echo "rc=$?"'],
                                                    hardened)
    sleeper.kill()
    sleeper.wait()
    ctx.data['log_hardened'] = None
    cases['nested_sandbox_exec'] = ctx.run([SANDBOX_EXEC, '-p', '(version 1)(allow default)'] + client_cmd(
        ctx, {'attempts': [{'name': 'outside_unix', 'kind': 'unix', 'path': unix.path}]}), profile)
    cases['nested_sandbox_exec']['parsed'] = parse_json(cases['nested_sandbox_exec']['stdout'])
    unix.close()
    ctx.data['cases'] = cases
    ctx.data['log'] = ctx.logs(since, tag)
    ctx.data['log_hardened'] = ctx.logs(since, tag2)


def stage_fds(ctx):
    alt, udp = Listener('tcp'), Listener('udp')
    unix = Listener('unix', str(ctx.root / 'outside' / 'fd-daemon.sock'))
    tcp = socket.create_connection(('127.0.0.1', alt.port))
    dgram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dgram.connect(('127.0.0.1', udp.port))
    stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stream.connect(unix.path)
    fds = {'tcp': tcp.fileno(), 'udp': dgram.fileno(), 'unix': stream.fileno()}
    time.sleep(.5)
    base = {'alt': alt.count(), 'udp': udp.count(), 'unix': unix.count()}
    tag = ctx.tag()
    since = now_for_log()
    profile = ctx.policy('enforced', None, tag)
    spec = {'attempts': [{'name': 'inherited_' + k, 'kind': 'fdwrite', 'fd': v} for k, v in fds.items()]}
    cases = {'inherited': ctx.run(client_cmd(ctx, spec), profile, pass_fds=tuple(fds.values())),
             'closed_before_confinement': ctx.run(client_cmd(ctx, spec), profile)}
    time.sleep(1)
    for case in cases.values():
        case['parsed'] = parse_json(case['stdout'])
    for s in (tcp, dgram, stream):
        s.close()
    time.sleep(.5)
    cases['listener_hits'] = {'alt_connections': alt.hits, 'udp_datagrams': udp.hits[base['udp']:],
                              'unix_connections': unix.hits}
    for listener in (alt, udp, unix):
        listener.close()
    ctx.data['cases'] = cases
    ctx.data['log'] = ctx.logs(since, tag)


def stage_keychain(ctx):
    service = 'quota-nm-nonexistent-' + secrets.token_hex(8)
    argv = ['/usr/bin/security', 'find-generic-password', '-s', service]
    tag1, tag2 = ctx.tag(), ctx.tag()
    since = now_for_log()
    cases = {'control_outside': ctx.run(argv),
             'store_allowed': ctx.run(argv, ctx.policy('enforced+store', None, tag1)),
             'store_denied': ctx.run(argv, ctx.policy('enforced', None, tag2))}
    for case in cases.values():
        case['stdout'] = '<omitted>'
    ctx.data['cases'] = cases
    ctx.data['service_name_shape'] = 'quota-nm-nonexistent-<16 hex>'
    ctx.data['log_store_allowed'] = ctx.logs(since, tag1)
    ctx.data['log_store_denied'] = ctx.logs(since, tag2)
    config = str(ctx.root / 'cfg')
    ctx.data['claude_names_for_probe_config'] = {
        'credentials': SBPL.claude_keychain_service(config),
        'legacy_api_key': SBPL.claude_keychain_service(config, kind=''),
        'device_keys_fixed': 'Claude Code-device-keys'}


# ---- native TUI cases ----------------------------------------------------

def write_security_shim(ctx):
    log = ctx.out / 'security-argv.jsonl'
    shim = ctx.root / 'shim' / 'security'
    shim.write_text('#!%s -I\n'
                    'import json, sys\n'
                    'args = sys.argv[1:]\n'
                    'safe = list(args)\n'
                    'if args[:1] == ["add-generic-password"]:\n'
                    '    for i, a in enumerate(args[:-1]):\n'
                    '        if a in ("-w", "-X"):\n'
                    '            safe[i + 1] = "<redacted>"\n'
                    'open(%r, "a").write(json.dumps(safe) + "\\n")\n'
                    'sys.exit(44 if args[:1] == ["find-generic-password"] else 1)\n' % (ctx.python, str(log)))
    shim.chmod(0o700)
    return log


# Tool-side terminal and signal probe. The marker is split in the source, so
# only a successful injection puts the joined word into a model request.
TOOL_TTY = '''import fcntl, json, sys, termios
res = {}
try:
    fd = open('/dev/tty', 'rb+', buffering=0)
    for ch in ('NMTOOL' + 'INJECT' + chr(13)).encode():
        fcntl.ioctl(fd, termios.TIOCSTI, bytes([ch]))
    res['tiocsti'] = 'injected'
except OSError as error:
    res['tiocsti'] = 'error:%s:%s' % (type(error).__name__, error.errno)
open(sys.argv[1], 'w').write(json.dumps(res))
'''


def tool_command(ctx, case, gate_port, alt_port, udp_port, unix_path, sleeper_pid=None):
    outdir = ctx.root / 'workspace' / 'nm-out'
    attempts = [
        {'name': 'gate_tcp4', 'kind': 'tcp4', 'host': '127.0.0.1', 'port': gate_port},
        {'name': 'alt_tcp4', 'kind': 'tcp4', 'host': '127.0.0.1', 'port': alt_port},
        {'name': 'alt_tcp6', 'kind': 'tcp6', 'host': '::1', 'port': alt_port},
        {'name': 'alt_udp4', 'kind': 'udp4', 'host': '127.0.0.1', 'port': udp_port},
        {'name': 'outside_unix', 'kind': 'unix', 'path': unix_path},
        {'name': 'launchservices', 'kind': 'mach', 'service': 'com.apple.coreservices.launchservicesd'}]
    py = [ctx.python, '-I', '-B', str(ctx.nm / 'client.py')]
    q = shlex.quote
    parts = [
        'mkdir -p %s' % q(str(outdir)),
        '%s %s > %s 2>&1' % (' '.join(map(q, py)), q(json.dumps({'attempts': attempts})),
                              q(str(outdir / (case + '-tool.json')))),
        '/usr/bin/env -i %s %s > %s 2>&1' % (' '.join(map(q, py)), q(json.dumps({'attempts': attempts})),
                                              q(str(outdir / (case + '-envi.json')))),
        '%s %s' % (' '.join(map(q, py)), q(json.dumps({'attempts': attempts,
                                                        'detach': str(outdir / (case + '-detached.json'))}))),
        '/usr/bin/curl -sS -m 3 -o /dev/null -w %%{http_code} http://127.0.0.1:%d/ > %s 2>&1' % (
            alt_port, q(str(outdir / (case + '-curl.txt'))))]
    if sleeper_pid is not None:
        parts += ['%s -I -B -c %s %s' % (q(ctx.python), q(TOOL_TTY), q(str(outdir / (case + '-tty.json')))),
                  '/bin/kill -0 %d 2>/dev/null; echo "rc=$?" > %s' % (sleeper_pid, q(str(outdir / (case + '-signal.txt'))))]
    parts.append('echo done > %s' % q(str(outdir / (case + '-done'))))
    return '; '.join(parts), outdir


class Pty:
    def __init__(self):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        self.before = self.attrs()
        self.output = bytearray()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while not self.stop.is_set():
            if not select.select([self.master], [], [], .1)[0]:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if len(self.output) < 4_000_000:
                self.output.extend(data)
            if b'\x1b[6n' in data:
                os.write(self.master, b'\x1b[1;1R')
            if b'\x1b[c' in data:
                os.write(self.master, b'\x1b[?1;2c')

    def write(self, data):
        os.write(self.master, data)

    def text(self):
        return bytes(self.output).decode(errors='replace')

    def attrs(self):
        # The slave descriptor is revoked when the native session ends on
        # macOS, so read the line discipline through the master side.
        for fd in (self.master, self.slave):
            try:
                return termios.tcgetattr(fd)
            except termios.error:
                continue
        return None

    def restored(self):
        now = self.attrs()
        return None if now is None or self.before is None else now == self.before

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)
        os.close(self.master)
        os.close(self.slave)


def wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.1)
    return bool(predicate())


def find_marker(directory, marker):
    hits = []
    for path in Path(directory).rglob('*.jsonl'):
        try:
            if marker in path.read_text(errors='replace'):
                hits.append(str(path.relative_to(directory)))
        except OSError:
            pass
    return sorted(hits)


def claude_setup(ctx, gate_url, auth, expired=False):
    cfg = ctx.root / 'cfg'
    account = {'hasCompletedOnboarding': True, 'theme': 'dark',
               'projects': {str(ctx.root / 'workspace'): {'hasTrustDialogAccepted': True,
                            'hasCompletedProjectOnboarding': True}},
               'oauthAccount': {'accountUuid': 'nm-fixture', 'organizationUuid': 'nm-org',
                                'emailAddress': 'nm@example.invalid'}}
    (cfg / '.claude.json').write_text(json.dumps(account))
    extra = {'ANTHROPIC_BASE_URL': gate_url, 'DISABLE_AUTOUPDATER': '1',
             'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY': '1'}
    if auth == 'oauth-file':
        (cfg / '.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'NM-FABRICATED-ACCESS', 'refreshToken': 'NM-FABRICATED-REFRESH',
            'expiresAt': int(time.time() * 1000) - 3600000 if expired else 4102444800000,
            'scopes': ['user:profile', 'user:inference'],
            'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_5x'}}))
        bearer = 'NM-FABRICATED-ACCESS'
    else:
        extra['CLAUDE_CODE_OAUTH_TOKEN'] = 'NM-FABRICATED-SETUP'
        bearer = 'NM-FABRICATED-SETUP'
    return extra, bearer


def codex_setup(ctx, gate_url, sandbox_mode, expired=False):
    import base64

    def enc(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    now = int(time.time())
    token = enc({'alg': 'none'}) + '.' + enc({'email': 'nm@example.invalid', 'sub': 'nm-user', 'iat': now - 7200,
        'exp': now - 60 if expired else now + 3600, 'https://api.openai.com/auth': {'chatgpt_account_id': 'nm-account',
        'chatgpt_plan_type': 'pro', 'chatgpt_user_id': 'nm-user'}}) + '.nm-fixture-signature'
    (ctx.root / 'codex' / 'auth.json').write_text(json.dumps({'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
        'tokens': {'id_token': token, 'access_token': token, 'refresh_token': 'nm-fixture-refresh',
                   'account_id': 'nm-account'}, 'last_refresh': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}))
    (ctx.root / 'codex' / 'config.toml').write_text(f'''model = "gpt-5.4"
model_provider = "gate"
chatgpt_base_url = "{gate_url}"
cli_auth_credentials_store = "file"
approval_policy = "never"
sandbox_mode = "{sandbox_mode}"
web_search = "disabled"
check_for_update_on_startup = false
[model_providers.gate]
name = "gate"
base_url = "{gate_url}/backend-api/codex"
requires_openai_auth = true
supports_websockets = false
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
[features]
plugins = false
recommended_plugins = false
apps = false
enable_mcp_apps = false
memories = false
shell_snapshot = false
shell_snapshot_v2 = false
network_proxy = false
code_mode_prewarm = false
[memories]
generate_memories = false
use_memories = false
[analytics]
enabled = false
[projects."{ctx.root / 'workspace'}"]
trust_level = "trusted"
''')
    return token


def make_tls(ctx):
    """Fabricated CA and leaf for TLS_HOST in ROOT/tls (never added to any keychain)."""
    d = ctx.root / 'tls'
    paths = {'ca': str(d / 'ca.pem'), 'leaf': str(d / 'leaf.pem'), 'key': str(d / 'leaf.key')}
    openssl = '/usr/bin/openssl'
    if 'tls_fixture' in ctx.data:
        return paths
    if d.exists():  # a fixture from an earlier stage in this root: make a fresh one
        for child in d.iterdir():
            child.unlink()
    d.mkdir(mode=0o700, exist_ok=True)
    (d / 'ext.cnf').write_text(TLS_CONFIG)
    for argv in ([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'ca.key', '-out', 'ca.pem',
                  '-days', '2', '-config', 'ext.cnf', '-extensions', 'ca'],
                 [openssl, 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-keyout', 'leaf.key', '-out', 'leaf.csr',
                  '-subj', '/CN=' + TLS_HOST],
                 [openssl, 'x509', '-req', '-in', 'leaf.csr', '-CA', 'ca.pem', '-CAkey', 'ca.key', '-set_serial', '2',
                  '-out', 'leaf.pem', '-days', '2', '-extfile', 'ext.cnf', '-extensions', 'leaf']):
        subprocess.run(argv, cwd=str(d), check=True, capture_output=True, timeout=30)
    ctx.data['tls_fixture'] = {
        'host': TLS_HOST, 'openssl': subprocess.run([openssl, 'version'], capture_output=True, text=True,
                                                    timeout=10).stdout.strip(),
        'leaf': subprocess.run([openssl, 'x509', '-in', 'leaf.pem', '-noout', '-subject', '-issuer'], cwd=str(d),
                               capture_output=True, text=True, timeout=10).stdout.strip().splitlines()}
    return paths


def native_case(ctx, native, kind, variant):
    """One bounded TUI run of a real native under the given profile kind.

    `gate` receives the model requests; `exit_gate` holds the only admitted
    port. They are one fixture, except for the tls-tunnel variant, where the
    native reaches a TLS fixture for TLS_HOST through the exit gate's CONNECT.
    """
    case = native + '-' + kind + '-' + variant
    alt, udp = Listener('tcp'), Listener('udp')
    unix = Listener('unix', str(ctx.root / 'outside' / (case + '.sock')))
    exit_gate = gate = GATE.FakeGate('true', 'unset')
    tls = None
    if variant == 'tls-tunnel':
        tls = make_tls(ctx)
        gate = GATE.FakeGate('true', 'unset', tls=(tls['leaf'], tls['key']))
        exit_gate.tunnels[TLS_HOST + ':443'] = gate.port
    sleeper = None
    if variant == 'tty-probe':  # an unrelated same-UID process outside the profile
        sleeper = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
        ctx.groups.append(sleeper.pid)
    command, outdir = tool_command(ctx, case, exit_gate.port, alt.port, udp.port, unix.path,
                                   sleeper.pid if sleeper else None)
    gate.command = command
    proxy_url = 'http://127.0.0.1:%d' % exit_gate.port
    gate_url = 'https://' + TLS_HOST if tls else proxy_url
    proxy_env = {k: proxy_url for k in ('HTTPS_PROXY', 'https_proxy')}
    proxy_env.update({k: '127.0.0.1,localhost,::1' for k in ('NO_PROXY', 'no_proxy')})
    tag = ctx.tag()
    since = now_for_log()
    profile = ctx.policy(kind, exit_gate.port, tag)
    result = {'case': case, 'gate_port_is_only_exit': True}
    term = Pty()
    proc = None
    shim_log = None
    try:
        if native == 'claude':
            auth = 'setup-token' if variant == 'setup-token' else 'oauth-file'
            extra, gate.bearer = claude_setup(ctx, gate_url, auth, expired=variant.startswith('refresh'))
            if variant in ('refresh-proxy', 'tls-tunnel'):
                extra.update(proxy_env)
            if tls:
                extra['NODE_EXTRA_CA_CERTS'] = tls['ca']
            path = '/usr/bin:/bin:/usr/sbin:/sbin'
            if variant != 'real-security':
                shim_log = write_security_shim(ctx)
                path = str(ctx.root / 'shim') + ':' + path
            env = ctx.env(PATH=path, **extra)
            settings = {'permissions': {'allow': ['Bash']}, 'env': {'ANTHROPIC_BASE_URL': gate_url}}
            if variant == 'native-sandbox':
                settings['sandbox'] = {'enabled': True, 'autoAllowBashIfSandboxed': True}
            argv = [ctx.tools['claude'], '--model', 'claude-sonnet-4-6', '--permission-mode', 'default',
                    '--tools', 'Bash', '--settings', json.dumps(settings), 'NM_TOOL_TURN claude ' + case]
            config_dir = ctx.root / 'cfg'
        else:
            mode = 'workspace-write' if variant == 'native-sandbox' else 'danger-full-access'
            gate.bearer = codex_setup(ctx, gate_url, mode, expired=variant.startswith('refresh'))
            env = ctx.env(CODEX_SQLITE_HOME=str(ctx.root / 'codex'))
            if variant == 'refresh-override':
                env['CODEX_REFRESH_TOKEN_URL_OVERRIDE'] = gate_url + '/oauth/token'
            if tls:
                env.update(proxy_env, CODEX_CA_CERTIFICATE=tls['ca'])
            daemon = [] if variant == 'daemon' else ['--no-daemon']
            argv = [ctx.tools['codex'], '--no-alt-screen', *daemon, '-C', str(ctx.root / 'workspace'),
                    'NM_TOOL_TURN codex ' + case]
            config_dir = ctx.root / 'codex'
        helper = ('import os,fcntl,termios,sys; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); '
                  'os.execv(sys.argv[1],sys.argv[1:])')
        proc = subprocess.Popen([ctx.python, '-I', '-B', '-c', helper, SANDBOX_EXEC, '-p', profile] + argv,
                                env=env, cwd=str(ctx.root / 'workspace'), stdin=term.slave, stdout=term.slave,
                                stderr=term.slave, close_fds=True)
        ctx.groups.append(proc.pid)
        model_key = native + '_model'
        if variant.startswith('refresh'):
            wait_for(lambda: False, 25)  # observe refresh routing only; no tool turn expected
        else:
            result['tool_call_sent'] = wait_for(
                lambda: any(e['event'] == model_key and e.get('tool_call') for e in gate.events), 40)
            # Without a tool call nothing can run, so do not spend the tool waits.
            sent = result['tool_call_sent']
            result['tool_done'] = sent and wait_for(lambda: (outdir / (case + '-done')).exists(), 45)
            result['tool_result_returned'] = sent and wait_for(
                lambda: any(e['event'] == model_key and e.get('tool_result') for e in gate.events), 30)
            if sent:
                wait_for(lambda: (outdir / (case + '-detached.json')).exists(), 15)
            time.sleep(1)
            # Codex treats a fast character burst as a paste, so Enter is sent separately.
            term.write(b'NM_SECOND_TURN ' + case.encode())
            time.sleep(1.5)
            term.write(b'\r')
            result['second_turn_reached_gate'] = wait_for(
                lambda: any(e['event'] == model_key and e.get('second_turn') for e in gate.events), 20)
        result['termios_changed_while_running'] = term.attrs() != term.before
        result['native_alive_before_exit'] = proc.poll() is None
        keys = [b'/exit\r'] if native == 'claude' else [b'\x03', b'\x03']
        for key in keys:
            if proc.poll() is not None:
                break
            try:
                term.write(key)
            except OSError:  # EIO: the native already closed its terminal and exited
                break
            time.sleep(.3)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        result['exit_code'] = proc.poll()
        result['termios_restored'] = term.restored()
        result['transcript_files_with_marker'] = find_marker(config_dir, case)
    except Exception as error:  # keep partial observations; an error is never a pass
        result['error'] = '%s: %s' % (type(error).__name__, error)
    finally:
        if proc is not None and proc.poll() is None:
            ctx.kill(proc.pid)
        term.close()
        gate.close()
        if exit_gate is not gate:
            exit_gate.close()
        for listener in (alt, udp, unix):
            listener.close()
        if sleeper is not None:
            result['outside_sleeper_alive_after'] = sleeper.poll() is None
            sleeper.kill()
            sleeper.wait()
    outputs = {}
    for name in ('tool.json', 'envi.json', 'detached.json', 'curl.txt', 'tty.json', 'signal.txt'):
        path = outdir / (case + '-' + name)
        text = path.read_text(errors='replace') if path.exists() else None
        outputs[name] = parse_json(text) if text and name.endswith('.json') else text
    result['tool_outputs'] = outputs
    result['outside_listener_hits'] = {'alt_tcp': alt.count(), 'alt_udp': udp.count(), 'unix': unix.count()}
    result['gate_events'] = gate.events
    if tls:
        result['exit_gate_events'] = exit_gate.events
        result['tls_handshake_errors'] = gate.tls_errors
    result['terminal_tail'] = term.text()[-3000:]
    result['terminal_has_reply'] = 'NM fixture reply' in SBPL.strip_ansi(term.text())
    if shim_log is not None and shim_log.exists():
        result['security_shim_argv'] = [json.loads(x) for x in shim_log.read_text().splitlines()]
        shim_log.unlink()
    result['log'] = ctx.logs(since, tag)
    return result


def stage_natives(ctx, which):
    for native, kind, variant in which:
        key = native + '-' + kind + '-' + variant
        try:
            ctx.data['cases'][key] = native_case(ctx, native, kind, variant)
        except Exception as error:  # record and continue; a failure is not a pass
            ctx.data['cases'][key] = {'error': '%s: %s' % (type(error).__name__, error)}
        ctx.save()


def stage_compat(ctx):
    alt = Listener('tcp')
    tag = ctx.tag()
    since = now_for_log()
    profile = ctx.policy('enforced', None, tag)
    git = ctx.tools['git']
    repo = ctx.root / 'outside' / 'repo'
    env = ctx.env(GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    for argv in ([git, 'init', '-q', '-b', 'main', str(repo)],
                 [git, '-C', str(repo), '-c', 'user.name=nm', '-c', 'user.email=nm@example.invalid',
                  'commit', '-q', '--allow-empty', '-m', 'nm fixture']):
        subprocess.run(argv, env=env, check=True, capture_output=True, timeout=20)
    cases = {
        'git_clone_local_path': ctx.run([git, 'clone', '-q', str(repo), str(ctx.root / 'workspace' / 'c1')],
                                        profile, env=env, timeout=30),
        'git_clone_http_fixture': ctx.run([git, '-c', 'http.lowSpeedLimit=1', '-c', 'http.lowSpeedTime=5', 'clone',
                                           '-q', 'http://127.0.0.1:%d/repo.git' % alt.port,
                                           str(ctx.root / 'workspace' / 'c2')], profile, env=env, timeout=30),
        'ssh_fixture_port': ctx.run(['/usr/bin/ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o',
                                     'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no', '-o',
                                     'UserKnownHostsFile=/dev/null', '-p', str(alt.port), '127.0.0.1', 'true'],
                                    profile, timeout=20),
        'curl_fixture': ctx.run(['/usr/bin/curl', '-sS', '-m', '3', 'http://127.0.0.1:%d/simple/' % alt.port],
                                profile, timeout=15),
        'pip_download_fixture': ctx.run([ctx.python, '-m', 'pip', 'download', '--no-deps', '--no-cache-dir',
                                         '--timeout', '3', '--retries', '0', '--index-url',
                                         'http://127.0.0.1:%d/simple/' % alt.port, '-d', str(ctx.root / 'tmp'),
                                         'nmfixture'], profile, timeout=40),
        'npm_view_fixture': ctx.run([ctx.tools['node'], ctx.tools['npm_cli'], 'view', 'nmfixture',
                                     '--registry', 'http://127.0.0.1:%d/' % alt.port, '--fetch-retries=0',
                                     '--fetch-timeout=3000'], profile,
                                    env=ctx.env(npm_config_cache=str(ctx.root / 'cache' / 'npm')), timeout=40)
        if ctx.tools.get('npm_cli') else {'skipped': 'npm cli not found'},
        'localhost_name_curl': ctx.run(['/usr/bin/curl', '-sS', '-m', '3', 'http://localhost:%d/' % alt.port],
                                       profile, timeout=15),
    }
    cases['alt_hits_from_sandboxed'] = alt.count()
    cases['control_curl_outside'] = ctx.run(['/usr/bin/curl', '-sS', '-m', '3',
                                             'http://127.0.0.1:%d/' % alt.port], timeout=15)
    cases['alt_hits_total'] = alt.count()
    alt.close()
    ctx.data['cases'] = cases
    ctx.data['log'] = ctx.logs(since, tag)


# ---- terminal injection through the inherited TTY ------------------------

# Unconfined "shell" stand-in: owns the session and controlling terminal of a
# private PTY, runs the child in its own foreground group (as a login shell
# runs a native), then reads what is left in the terminal input queue.
TTY_SHELL = '''import json, os, select, subprocess, sys, fcntl, termios
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
try:
    rc = subprocess.run(sys.argv[2:], timeout=20).returncode
except subprocess.TimeoutExpired:
    rc = 'timeout'
ready = select.select([0], [], [], 1.5)[0]
data = os.read(0, 4096) if ready else b''
open(sys.argv[1], 'w').write(json.dumps({'child_rc': rc, 'read_after_child': data.decode(errors='replace')}))
'''

# Child: keep using the terminal (tcsetattr), then try TIOCSTI injection.
TTY_CHILD = '''import fcntl, json, sys, termios
res = {}
try:
    termios.tcsetattr(0, termios.TCSANOW, termios.tcgetattr(0))
    res['tcsetattr'] = 'ok'
except Exception as error:
    res['tcsetattr'] = 'error:%s:%s' % (type(error).__name__, getattr(error, 'errno', error))
sent, failure = 0, None
for ch in b'NMINJECT\\n':
    try:
        fcntl.ioctl(0, termios.TIOCSTI, bytes([ch]))
        sent += 1
    except OSError as error:
        failure = 'error:%s:%s' % (type(error).__name__, error.errno)
        break
res.update(tiocsti_sent=sent, tiocsti_error=failure, tiocsti=termios.TIOCSTI)
open(sys.argv[1], 'w').write(json.dumps(res))
'''


def stage_tty(ctx):
    import termios as _termios
    tag = ctx.tag()
    since = now_for_log()
    base = ctx.policy('enforced', None, tag)
    deny = '(deny file-ioctl (ioctl-command %s) (with message "' + tag + '"))\n'
    variants = {'control': None, 'enforced': base,
                'enforced_deny_tiocsti_symbolic': base + deny % 'TIOCSTI',
                'enforced_deny_tiocsti_numeric': base + deny % _termios.TIOCSTI}
    for name, profile in variants.items():
        if profile is not None:
            ctx.data['profiles']['tty:' + name] = profile
        shell_out = ctx.out / ('tty-%s-shell.json' % name)
        child_out = ctx.out / ('tty-%s-child.json' % name)
        child = [ctx.python, '-I', '-B', '-c', TTY_CHILD, str(child_out)]
        if profile is not None:
            child = [SANDBOX_EXEC, '-p', profile] + child
        term = Pty()
        proc = subprocess.Popen([ctx.python, '-I', '-B', '-c', TTY_SHELL, str(shell_out)] + child,
                                env=ctx.env(), cwd=str(ctx.root / 'workspace'), stdin=term.slave,
                                stdout=term.slave, stderr=term.slave, close_fds=True)
        ctx.groups.append(proc.pid)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            ctx.kill(proc.pid)
        time.sleep(.3)
        term.close()
        case = {'exit_code': proc.poll(), 'terminal_tail': SBPL.strip_ansi(term.text())[-600:]}
        for key, path in (('shell', shell_out), ('child', child_out)):
            case[key] = json.loads(path.read_text()) if path.exists() else None
        ctx.data['cases'][name] = case
    ctx.data['log'] = ctx.logs(since, tag)


def stage_dgram(ctx):
    """Datagram gaps: AF_UNIX datagrams outside, and UDP to the gate port number."""
    gate = Listener('tcp')
    gate_udp = Listener('udp', port=gate.port)  # an outside process owning UDP on the gate port
    unix = Listener('unix_dgram', str(ctx.root / 'outside' / 'dgram-daemon.sock'))
    attempts = [
        {'name': 'gate_tcp4', 'kind': 'tcp4', 'host': '127.0.0.1', 'port': gate.port},
        {'name': 'gate_tcp6', 'kind': 'tcp6', 'host': '::1', 'port': gate.port},
        {'name': 'gate_port_udp4', 'kind': 'udp4', 'host': '127.0.0.1', 'port': gate.port},
        {'name': 'gate_port_udp6', 'kind': 'udp6', 'host': '::1', 'port': gate.port},
        {'name': 'outside_unix_dgram', 'kind': 'unix_dgram', 'path': unix.path},
        {'name': 'private_unix_dgram_self', 'kind': 'unix_dgram_self', 'path': str(ctx.root / 'private')}]
    tags = []
    try:
        for name, extra in (('enforced_remote_ip', {}), ('enforced_remote_tcp', {'gate_tcp_only': True})):
            tag = ctx.tag()
            tags.append(tag)
            since = now_for_log()
            before = {'gate': gate.count(), 'udp': gate_udp.count(), 'unix': unix.count()}
            case = ctx.run(client_cmd(ctx, {'attempts': attempts}), ctx.policy('enforced', gate.port, tag, **extra))
            time.sleep(.5)
            case['parsed'] = parse_json(case['stdout'])
            case['hits'] = {'gate_tcp': gate.count() - before['gate'], 'gate_port_udp': gate_udp.count() - before['udp'],
                            'outside_unix_dgram': unix.count() - before['unix']}
            case['log'] = ctx.logs(since, tag)
            ctx.data['cases'][name] = case
        before = {'udp': gate_udp.count(), 'unix': unix.count()}
        control = ctx.run(client_cmd(ctx, {'attempts': attempts}))
        time.sleep(.5)
        control['parsed'] = parse_json(control['stdout'])
        control['hits'] = {'gate_port_udp': gate_udp.count() - before['udp'],
                           'outside_unix_dgram': unix.count() - before['unix']}
        ctx.data['cases']['outside_control'] = control
    finally:
        for listener in (gate, gate_udp, unix):
            listener.close()


def host_ipv4():
    """The host's own interface address (read locally; redacted before saving)."""
    for iface in ('en0', 'en1'):
        result = subprocess.run(['/usr/sbin/ipconfig', 'getifaddr', iface], capture_output=True, text=True,
                                timeout=10)
        address = result.stdout.strip()
        if result.returncode == 0 and address and not address.startswith('127.'):
            return address
    return None


# Unconfined job-control "shell": session leader of a private PTY that runs
# the child as its foreground job, reports the Ctrl-Z stop, then resumes it
# from outside the profile the way `fg` does.
JOB_SHELL = '''import fcntl, json, os, signal, sys, termios, time
os.setsid()
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGTTOU, signal.SIG_IGN)
res = {}
pid = os.fork()
if pid == 0:
    os.setpgid(0, 0)
    os.tcsetpgrp(0, os.getpid())
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    os.execv(sys.argv[2], sys.argv[2:])
try:
    os.setpgid(pid, pid)
except OSError:
    pass
os.tcsetpgrp(0, pid)
signal.alarm(25)
_, status = os.waitpid(pid, os.WUNTRACED)
res['stopped_by_tty'] = os.WIFSTOPPED(status)
res['stop_signal'] = os.WSTOPSIG(status) if os.WIFSTOPPED(status) else None
if os.WIFSTOPPED(status):
    os.tcsetpgrp(0, os.getpgrp())
    time.sleep(.3)
    os.tcsetpgrp(0, pid)
    os.killpg(pid, signal.SIGCONT)
    _, status = os.waitpid(pid, 0)
res['exited'] = os.WIFEXITED(status)
res['exit_code'] = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
os.tcsetpgrp(0, os.getpgrp())
open(sys.argv[1], 'w').write(json.dumps(res))
'''

# Confined job: waits for the Ctrl-Z stop and resume, then does in-profile job
# control on its own child (stop, continue, terminate) and signals its group.
JOB_CHILD = '''import json, os, signal, subprocess, sys, time
res = {}
open(sys.argv[2], 'w').write('ready')
began = time.monotonic()
time.sleep(4)
res['slept_seconds'] = round(time.monotonic() - began, 2)
child = subprocess.Popen(['/bin/sleep', '30'])
inner = {}
for name, sig in (('stop', signal.SIGSTOP), ('cont', signal.SIGCONT), ('term', signal.SIGTERM)):
    try:
        os.kill(child.pid, sig)
        inner[name] = 'ok'
    except OSError as error:
        inner[name] = 'error:%s' % error.errno
    if name == 'stop' and inner[name] == 'ok':
        inner['stopped'] = os.WIFSTOPPED(os.waitpid(child.pid, os.WUNTRACED)[1])
res['inner'] = inner
res['inner_returncode'] = child.wait(timeout=10)
try:
    os.kill(0, signal.SIGCONT)
    res['own_group_cont'] = 'ok'
except OSError as error:
    res['own_group_cont'] = 'error:%s' % error.errno
open(sys.argv[1], 'w').write(json.dumps(res))
'''


def job_control_case(ctx, name, profile):
    shell_out, child_out = ctx.out / ('job-%s-shell.json' % name), ctx.out / ('job-%s-child.json' % name)
    ready = ctx.out / ('job-%s-ready' % name)
    child = [ctx.python, '-I', '-B', '-c', JOB_CHILD, str(child_out), str(ready)]
    if profile is not None:
        child = [SANDBOX_EXEC, '-p', profile] + child
    term = Pty()
    proc = subprocess.Popen([ctx.python, '-I', '-B', '-c', JOB_SHELL, str(shell_out)] + child, env=ctx.env(),
                            cwd=str(ctx.root / 'workspace'), stdin=term.slave, stdout=term.slave,
                            stderr=term.slave, close_fds=True)
    ctx.groups.append(proc.pid)
    case = {'child_ready': wait_for(ready.exists, 15)}
    time.sleep(.5)
    term.write(b'\x1a')  # Ctrl-Z through the line discipline: the kernel sends SIGTSTP
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        ctx.kill(proc.pid)
    time.sleep(.3)
    term.close()
    case['exit_code'] = proc.poll()
    for key, path in (('shell', shell_out), ('child', child_out)):
        case[key] = json.loads(path.read_text()) if path.exists() else None
    return case


def launchd_cases(ctx, profile):
    """Other launchd entry points from inside; every created label is removed."""
    uid, cases = os.getuid(), {}
    for verb in ('bootstrap', 'load'):
        label = 'quota-nm-' + secrets.token_hex(6)
        plist, marker = ctx.root / 'outside' / (label + '.plist'), ctx.root / 'outside' / (label + '.marker')
        with plist.open('wb') as handle:
            plistlib.dump({'Label': label, 'RunAtLoad': True, 'ProgramArguments': [
                '/bin/sh', '-c', '/bin/echo ran > ' + shlex.quote(str(marker))]}, handle)
        argv = (['/bin/launchctl', 'bootstrap', 'gui/%d' % uid, str(plist)] if verb == 'bootstrap'
                else ['/bin/launchctl', 'load', str(plist)])
        case = ctx.run(argv, profile, timeout=15)
        time.sleep(2)
        target = 'gui/%d/%s' % (uid, label)
        printed = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True, text=True, timeout=15)
        bootout = subprocess.run(['/bin/launchctl', 'bootout', target], capture_output=True, text=True, timeout=15)
        removed = subprocess.run(['/bin/launchctl', 'remove', label], capture_output=True, text=True, timeout=15)
        after = subprocess.run(['/bin/launchctl', 'print', target], capture_output=True, text=True, timeout=15)
        case.update(label_printed_code=printed.returncode, bootout_code=bootout.returncode,
                    remove_code=removed.returncode, label_printed_after_cleanup_code=after.returncode,
                    marker_written=marker.exists())
        cases[verb] = case
    missing = 'gui/%d/qnm-absent-%s' % (uid, secrets.token_hex(6))
    cases['kickstart_absent_sandboxed'] = ctx.run(['/bin/launchctl', 'kickstart', missing], profile, timeout=15)
    cases['kickstart_absent_control'] = ctx.run(['/bin/launchctl', 'kickstart', missing], timeout=15)
    probe = client_cmd(ctx, {'attempts': [
        {'name': 'launchservices', 'kind': 'mach', 'service': 'com.apple.coreservices.launchservicesd'},
        {'name': 'nsurlsessiond', 'kind': 'mach', 'service': 'com.apple.nsurlsessiond'}]})
    cases['asuser_sandboxed'] = ctx.run(['/bin/launchctl', 'asuser', str(uid)] + probe, profile, timeout=20)
    cases['asuser_sandboxed']['parsed'] = parse_json(cases['asuser_sandboxed']['stdout'])
    return cases


def stage_m1(ctx):
    """The boundary matrix under the exact M1 shape, plus the review-1 gaps."""
    stage_boundary(ctx, kinds=('m1',))
    gate, alt = Listener('tcp'), Listener('tcp')
    unix = Listener('unix', str(ctx.root / 'outside' / 'alias-daemon.sock'))
    private = ctx.root / 'private'
    address = host_ipv4()
    ctx.data['host_ipv4_found'] = bool(address)
    if address:
        ctx.redact[address] = '<host-ipv4>'
    extent = [
        {'name': 'gate_loopback4', 'kind': 'tcp4', 'host': '127.0.0.1', 'port': gate.port},
        {'name': 'gate_loopback6', 'kind': 'tcp6', 'host': '::1', 'port': gate.port},
        {'name': 'gate_any4', 'kind': 'tcp4', 'host': '0.0.0.0', 'port': gate.port},
        {'name': 'gate_any6', 'kind': 'tcp6', 'host': '::', 'port': gate.port},
        {'name': 'gate_loopback4_other', 'kind': 'tcp4', 'host': '127.0.0.2', 'port': gate.port},
        {'name': 'gate_linklocal_lo0', 'kind': 'tcp6', 'host': 'fe80::1%lo0', 'port': gate.port},
        {'name': 'alt_linklocal_lo0', 'kind': 'tcp6', 'host': 'fe80::1%lo0', 'port': alt.port},
        {'name': 'gate_udp4', 'kind': 'udp4', 'host': '127.0.0.1', 'port': gate.port}]
    if address:
        extent += [{'name': 'gate_host_ipv4', 'kind': 'tcp4', 'host': address, 'port': gate.port},
                   {'name': 'alt_host_ipv4', 'kind': 'tcp4', 'host': address, 'port': alt.port}]
    alias = [
        {'name': 'symlink_in_private', 'kind': 'unix_alias', 'link': 'sym', 'path': str(private / 'alias-sym.sock'),
         'target': unix.path},
        {'name': 'hardlink_in_private', 'kind': 'unix_alias', 'link': 'hard',
         'path': str(private / 'alias-hard.sock'), 'target': unix.path},
        {'name': 'direct_outside', 'kind': 'unix', 'path': unix.path}]
    icmp = [{'name': 'icmp4_loopback', 'kind': 'icmp4', 'host': '127.0.0.1'},
            {'name': 'icmp6_loopback', 'kind': 'icmp6', 'host': '::1'}]
    trust = [{'name': 'trust_' + d, 'kind': 'trust_settings', 'domain': d} for d in ('system', 'admin')]
    try:
        tag = ctx.tag()
        since = now_for_log()
        m1 = ctx.policy('m1', gate.port, tag)
        tag_rec = ctx.tag()
        recommended = ctx.policy('recommended', gate.port, tag_rec)
        cases = {}
        for name, spec, profile in (('extent_m1', extent, m1), ('extent_control', extent, None),
                                    ('alias_recommended', alias, recommended), ('alias_m1', alias, m1),
                                    ('alias_control', alias, None),
                                    ('icmp_m1', icmp, m1), ('icmp_control', icmp, None),
                                    ('trust_m1', trust, m1), ('trust_control', trust, None)):
            before = {'gate': gate.count(), 'alt': alt.count(), 'unix': unix.count()}
            case = ctx.run(client_cmd(ctx, {'attempts': spec}), profile, timeout=40)
            time.sleep(.3)
            case['parsed'] = parse_json(case['stdout'])
            case['hits'] = {k: (gate.count(), alt.count(), unix.count())[i] - before[k]
                            for i, k in enumerate(('gate', 'alt', 'unix'))}
            cases[name] = case
        cases['launchd_m1'] = launchd_cases(ctx, m1)
        cases['job_control_m1'] = job_control_case(ctx, 'm1', m1)
        cases['job_control_control'] = job_control_case(ctx, 'control', None)
        ctx.data['cases']['review'] = cases
        ctx.data['log_m1_review'] = ctx.logs(since, tag)
        ctx.data['log_recommended_alias'] = ctx.logs(since, tag_rec)
    finally:
        for listener in (gate, alt, unix):
            listener.close()


NATIVE_SETS = {
    'natives': [('claude', 'census', 'oauth-file'), ('claude', 'enforced', 'oauth-file'),
                ('codex', 'census', 'plain'), ('codex', 'enforced', 'plain'), ('codex', 'enforced', 'daemon')],
    'natives-extra': [('claude', 'enforced', 'setup-token'), ('claude', 'enforced', 'real-security')],
    'nested': [('claude', 'enforced', 'native-sandbox'), ('codex', 'enforced', 'native-sandbox')],
    'refresh': [('claude', 'enforced', 'refresh-direct'), ('claude', 'enforced', 'refresh-proxy'),
                ('codex', 'enforced', 'refresh-override')],
    'hardened': [('claude', 'enforced', 'tty-probe'), ('codex', 'enforced', 'tty-probe'),
                 ('claude', 'hardened', 'tty-probe'), ('codex', 'hardened', 'tty-probe')],
    'recommended': [('claude', 'recommended', 'tty-probe'), ('codex', 'recommended', 'tty-probe')],
    'm1-natives': [('claude', 'm1', 'tty-probe'), ('codex', 'm1', 'tty-probe')],
    'tls': [('claude', 'census', 'tls-tunnel'), ('claude', 'm1', 'tls-tunnel'),
            ('codex', 'census', 'tls-tunnel'), ('codex', 'm1', 'tls-tunnel')],
}


class StageDeadline(BaseException):
    """Not an Exception, so per-case handlers cannot swallow the stage deadline."""


def _deadline(signum, frame):
    raise StageDeadline('remote stage deadline reached')


def main():
    root, stage, tools = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    os.umask(0o077)
    ctx = Ctx(root, stage, tools)
    if len(sys.argv) > 4:
        signal.signal(signal.SIGALRM, _deadline)
        signal.alarm(int(sys.argv[4]))
        ctx.data['deadline_seconds'] = int(sys.argv[4])
    try:
        if stage in NATIVE_SETS:
            for name in ('claude', 'codex'):
                path = os.path.realpath(tools[name])
                ctx.data.setdefault('binaries', {})[name] = {
                    'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                    'version': subprocess.run([path, '--version'], capture_output=True, text=True,
                                              timeout=20, env=ctx.env()).stdout.strip()}
            stage_natives(ctx, NATIVE_SETS[stage])
        else:
            {'boundary': stage_boundary, 'ipc': stage_ipc, 'fds': stage_fds, 'keychain': stage_keychain,
             'compat': stage_compat, 'tty': stage_tty, 'dgram': stage_dgram, 'm1': stage_m1}[stage](ctx)
        ctx.data['completed'] = True
    except (Exception, StageDeadline) as error:
        ctx.data['completed'] = False
        ctx.data['error'] = '%s: %s' % (type(error).__name__, error)
    finally:
        signal.alarm(0)
        ctx.cleanup()
        ctx.save()
    print(json.dumps({'stage': stage, 'completed': ctx.data.get('completed')}))


if __name__ == '__main__':
    main()
