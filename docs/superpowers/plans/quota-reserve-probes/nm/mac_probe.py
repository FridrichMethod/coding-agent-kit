"""NM macOS Seatbelt outer-boundary probes; run on the Mac only via run.py.

python3 -I -B mac_probe.py ROOT STAGE TOOLS_JSON

ROOT is a fresh private temp root (created by run.py with mktemp). Every
native and client gets HOME, CLAUDE_CONFIG_DIR, CODEX_HOME, XDG_* and TMPDIR
inside ROOT, fabricated credentials, and loopback fixtures only. Results go to
ROOT/out/STAGE.json; run.py copies and sanitizes them. Every child is bounded
and its process group is killed; detached fixtures are reaped by ROOT path.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
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
        path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + '\n')


def now_for_log():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - 1))


class Listener:
    """Outside-sandbox fixture: TCP v4+v6 on one port, optional UDP, or Unix."""

    def __init__(self, kind, path=None):
        self.kind, self.hits, self.lock = kind, [], threading.Lock()
        self.socks = []
        if kind in ('tcp', 'udp'):
            stype = socket.SOCK_STREAM if kind == 'tcp' else socket.SOCK_DGRAM
            for _ in range(50):
                first = socket.socket(socket.AF_INET, stype)
                first.bind(('127.0.0.1', 0))
                self.port = first.getsockname()[1]
                second = socket.socket(socket.AF_INET6, stype)
                second.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    second.bind(('::1', self.port))
                    break
                except OSError:
                    first.close()
                    second.close()
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
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.bind(path)
            s.listen(16)
            self.path = path
            self.socks.append(s)
            threading.Thread(target=self._accept, args=(s,), daemon=True).start()

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


def stage_boundary(ctx):
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
        for kind in ('census', 'enforced'):
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


def native_case(ctx, native, kind, variant):
    """One bounded TUI run of a real native under the given profile kind."""
    case = native + '-' + kind + '-' + variant
    alt, udp = Listener('tcp'), Listener('udp')
    unix = Listener('unix', str(ctx.root / 'outside' / (case + '.sock')))
    gate = GATE.FakeGate('true', 'unset')
    sleeper = None
    if variant == 'tty-probe':  # an unrelated same-UID process outside the profile
        sleeper = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
        ctx.groups.append(sleeper.pid)
    command, outdir = tool_command(ctx, case, gate.port, alt.port, udp.port, unix.path,
                                   sleeper.pid if sleeper else None)
    gate.command = command
    gate_url = 'http://127.0.0.1:%d' % gate.port
    tag = ctx.tag()
    since = now_for_log()
    profile = ctx.policy(kind, gate.port, tag)
    result = {'case': case, 'gate_port_is_only_exit': True}
    term = Pty()
    proc = None
    shim_log = None
    try:
        if native == 'claude':
            auth = 'setup-token' if variant == 'setup-token' else 'oauth-file'
            extra, gate.bearer = claude_setup(ctx, gate_url, auth, expired=variant.startswith('refresh'))
            if variant == 'refresh-proxy':
                extra.update({k: gate_url for k in ('HTTPS_PROXY', 'https_proxy')})
                extra.update({k: '127.0.0.1,localhost,::1' for k in ('NO_PROXY', 'no_proxy')})
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
            result['tool_done'] = wait_for(lambda: (outdir / (case + '-done')).exists(), 45)
            result['tool_result_returned'] = wait_for(
                lambda: any(e['event'] == model_key and e.get('tool_result') for e in gate.events), 30)
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


NATIVE_SETS = {
    'natives': [('claude', 'census', 'oauth-file'), ('claude', 'enforced', 'oauth-file'),
                ('codex', 'census', 'plain'), ('codex', 'enforced', 'plain'), ('codex', 'enforced', 'daemon')],
    'natives-extra': [('claude', 'enforced', 'setup-token'), ('claude', 'enforced', 'real-security')],
    'nested': [('claude', 'enforced', 'native-sandbox'), ('codex', 'enforced', 'native-sandbox')],
    'refresh': [('claude', 'enforced', 'refresh-direct'), ('claude', 'enforced', 'refresh-proxy'),
                ('codex', 'enforced', 'refresh-override')],
    'hardened': [('claude', 'enforced', 'tty-probe'), ('codex', 'enforced', 'tty-probe'),
                 ('claude', 'hardened', 'tty-probe'), ('codex', 'hardened', 'tty-probe')],
}


def main():
    root, stage, tools = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
    os.umask(0o077)
    ctx = Ctx(root, stage, tools)
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
             'compat': stage_compat, 'tty': stage_tty}[stage](ctx)
        ctx.data['completed'] = True
    except Exception as error:
        ctx.data['completed'] = False
        ctx.data['error'] = '%s: %s' % (type(error).__name__, error)
    finally:
        ctx.cleanup()
        ctx.save()
    print(json.dumps({'stage': stage, 'completed': ctx.data.get('completed')}))


if __name__ == '__main__':
    main()
