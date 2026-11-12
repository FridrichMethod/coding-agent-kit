"""KLC composition: launcher refusals and one protected invocation (research).

Order of one invocation, as the launcher would run it:
  1. refuse on host, native version, mode or environment (launcher_refusal, called by the
     harness launch path before the gate or native starts); the token-window check below is
     a pure function only, because the launcher never reads the store: in a product it
     belongs in the gate's startup, next to QC's expired-token refusal, which does run there;
  2. bind the gate port on 127.0.0.1 (random ephemeral port) in the launcher; the
     launcher keeps this listening socket for the invocation's lifetime (port holder);
  3. start the gate with that socket; the gate reads the bearer memory-only and runs
     the model-free same-bearer preflight (usage and profile, both windows); any
     failure refuses before anything else starts;
  4. start the monitor (poll heartbeat over a socketpair);
  5. confine (NL L2a: Landlock connect to the gate port only, seccomp) and exec the
     real native on a PTY, with ANTHROPIC_BASE_URL pinned to the gate by inline
     --settings and HTTPS_PROXY pointing at the gate for the refresh tunnel.
Here the harness plays the launcher (PTY owner, port holder, trusted terminal);
the monitor is a separate process only so that the loss cases can kill it.
"""
import ctypes
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import platform
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time

HERE = Path(os.path.realpath(__file__)).parent
NL = HERE.parent / 'nl'
SUPPORTED_VERSIONS = ('2.1.322',)
NATIVE_REFRESH_WINDOW_MS = 300000  # native yP(): refresh when now + 300000 >= expiresAt (S, 2.1.322)
KIT_EXPIRY_SKEW_MS = 30000  # QC reader refuses expired_native_refresh_required inside this skew
# Native options that leave the protected shape: other auth or no TUI (print, bare),
# delegation outside the invocation (background, cloud, remote control, desktop,
# browser and IDE bridges), downloads at startup, or settings that could move the
# base URL or add MCP servers. Research list for 2.1.322 (S, --help); G0 owns it.
REFUSED_OPTIONS = ('-p', '--print', '--bare', '--bg', '--background', '--remote-control', '--cloud',
                   '--teleport', '--desktop', '--environment', '--chrome', '--ide', '--plugin-url', '--file',
                   '--mcp-config', '--settings', '--setting-sources', '--plugin-dir', '--from-pr')
CALLER_ENV_REFUSED = ('ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_CUSTOM_HEADERS',
                      'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
                      'CLAUDE_CODE_USE_GATEWAY', 'CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN',
                      'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR', 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN',
                      'CLAUDE_CODE_CUSTOM_OAUTH_URL', 'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR',
                      'CLAUDE_CODE_REMOTE_SESSION_ID', 'CLAUDE_LOCAL_OAUTH_API_BASE')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


boundary = _load('klc_nl_boundary', NL / 'boundary.py')


# -- pure refusal checks (offline tests) -----------------------------------------
def host_refusal(facts):
    if facts.get('platform') != 'linux':
        return 'not_linux'
    abi = facts.get('landlock_abi')
    if abi is None:
        return 'landlock_unavailable'
    if abi < 4:
        return 'landlock_abi_below_4'
    if facts.get('arch') != 'x86_64':
        return 'seccomp_arch_unvalidated'
    if facts.get('stdio_sockets'):
        return 'stdio_socket'
    tiocsti = facts.get('legacy_tiocsti')
    if tiocsti is None:
        return 'tiocsti_unknown'
    if tiocsti != 0:
        return 'tiocsti_enabled'
    return None


def version_refusal(text):
    match = re.fullmatch(r'\s*(\d+\.\d+\.\d+) \(Claude Code\)\s*', text or '')
    if not match:
        return 'native_version_unreadable'
    if match.group(1) not in SUPPORTED_VERSIONS:
        return 'native_version_unsupported'
    return None


def mode_refusal(argv):
    """Refused long options by exact name; any single-dash cluster containing p (-p, -pc, -cp)
    as print mode. Fails closed: a short option whose attached value contains p refuses too."""
    for item in argv:
        name = item.split('=', 1)[0]
        if name in REFUSED_OPTIONS:
            return 'native_mode_unsupported'
        if re.fullmatch(r'-[A-Za-z]+', name) and 'p' in name[1:]:
            return 'native_mode_unsupported'
    return None


def env_refusal(env, config_dir):
    for name in CALLER_ENV_REFUSED:
        if name in env:
            return 'caller_env_conflict'
    if env.get('CLAUDE_CONFIG_DIR', config_dir) != config_dir:
        return 'config_dir_mismatch'
    return None


def token_window_refusal(expires_at_ms, now_ms, refresh_tunnel):
    """Without a refresh tunnel, a token inside native's refresh window cannot be refreshed
    in the boundary: refuse and ask for a native refresh outside before launch (option a)."""
    left = expires_at_ms - now_ms
    if left <= KIT_EXPIRY_SKEW_MS:
        return 'expired_native_refresh_required'
    if left <= NATIVE_REFRESH_WINDOW_MS and not refresh_tunnel:
        return 'native_refresh_window'
    return None


def launcher_refusal(facts, version_text, argv, env, config_dir):
    """The checks a launcher can make without reading the credential store."""
    for reason in (host_refusal(facts), version_refusal(version_text), mode_refusal(argv),
                   env_refusal(env, config_dir)):
        if reason:
            return reason
    return None


def first_refusal(facts, version_text, argv, env, config_dir, expires_at_ms, now_ms, refresh_tunnel):
    return (launcher_refusal(facts, version_text, argv, env, config_dir)
            or token_window_refusal(expires_at_ms, now_ms, refresh_tunnel))


def host_facts():
    facts = {'platform': sys.platform, 'arch': platform.machine(), 'stdio_sockets': boundary.stdio_sockets()}
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        abi = libc.syscall(boundary.SYS_CREATE, None, 0, boundary.CREATE_VERSION)
        facts['landlock_abi'] = abi if abi >= 0 else None
    except OSError:
        facts['landlock_abi'] = None
    try:
        facts['legacy_tiocsti'] = int(Path('/proc/sys/dev/tty/legacy_tiocsti').read_text())
    except (OSError, ValueError):
        facts['legacy_tiocsti'] = None
    return facts


# -- one invocation (harness as launcher) ------------------------------------------
ANSI = re.compile(r'\x1b(\[[0-9;?<>=]*[A-Za-z~]|\][^\x07\x1b]*(\x07|\x1b\\)|[78()][A-Z0-9]?)')
HELPER = ('import os,fcntl,termios,sys; os.setsid(); '
          'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')


class Invocation:
    def __init__(self, root, name, record, binary, *, config_dir, registered, usage_port, upstream_port, policy,
                 prompt, connect_allow=None, upstream_proxy=None, interval=1.0, gate_proxy_env=True,
                 extra_env=None, settings_extra=None, fs_rules=None, strace=True, on_output=None):
        self.root, self.name, self.record, self.binary = Path(root), name, record, binary
        self.config_dir, self.prompt, self.interval = config_dir, prompt, interval
        self.config = {'config_dir': config_dir, 'registered': list(registered), 'usage_port': usage_port,
                       'upstream_port': upstream_port, 'policy': policy, 'connect_allow': connect_allow or {},
                       'upstream_proxy': upstream_proxy, 'mode': 'startup'}
        self.gate_proxy_env, self.extra_env, self.settings_extra = gate_proxy_env, extra_env or {}, settings_extra or {}
        self.fs_rules, self.use_strace, self.on_output = fs_rules, strace and shutil.which('strace'), on_output
        self.events, self.output = [], bytearray()
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.gate = self.monitor = self.native = None
        self.master = self.slave = self.termios_before = None
        self.control_sock = None
        self.replies = []
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.gate_url = 'http://127.0.0.1:%d' % self.port
        self.started_monotonic = None

    # events -----------------------------------------------------------------
    def _note(self, source, row):
        row = dict(row)
        row['source'], row['inv'] = source, self.name
        with self.lock:
            self.events.append(row)
        self.record('event', **row)

    def gate_events(self, name=None):
        with self.lock:
            return [e for e in self.events if e['source'] == 'gate' and (name is None or e.get('event') == name)]

    def _reader(self, stream, source, process):
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                row = {'event': 'unparsed', 'text': line[:120]}
            if row.get('event') == 'control':
                with self.lock:
                    self.replies.append(row)
            self._note(source, row)
        self._note(source, {'event': 'process_exit', 'code': process.wait(), 't': time.monotonic()})

    # gate and monitor -------------------------------------------------------------
    def start_gate(self, mode='startup', timeout=20):
        self.config['mode'] = mode
        path = self.root / ('gate-%s-%s.json' % (self.name, mode))
        path.write_text(json.dumps(self.config))
        gate_mon, self.monitor_end = socket.socketpair()
        gate_ctl, self.control_sock = socket.socketpair()
        fds = (self.listener.fileno(), gate_mon.fileno(), gate_ctl.fileno())
        began = time.monotonic()
        self.gate = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'gate.py'), str(path),
                                      '--listen-fd', str(fds[0]), '--monitor-fd', str(fds[1]),
                                      '--control-fd', str(fds[2])], pass_fds=fds, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     start_new_session=True, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        gate_mon.close()
        gate_ctl.close()
        threading.Thread(target=self._reader, args=(self.gate.stdout, 'gate', self.gate), daemon=True).start()
        threading.Thread(target=self._drain, args=(self.gate.stderr, 'gate_stderr'), daemon=True).start()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            ready = self.gate_events('gate_ready')
            refused = self.gate_events('startup_refused')
            if ready and ready[-1]['t'] >= began:
                self._note('launcher', {'event': 'gate_started', 'mode': mode,
                                        'spawn_to_ready_ms': round((ready[-1]['t'] - began) * 1000, 3),
                                        't': time.monotonic()})
                return ready[-1]
            if refused and refused[-1]['t'] >= began or self.gate.poll() is not None:
                time.sleep(0.1)
                return {'event': 'startup_refused', 'reason': refused[-1]['reason'] if refused else 'gate_exit'}
            time.sleep(0.02)
        return {'event': 'startup_refused', 'reason': 'gate_timeout'}

    def _drain(self, stream, source):
        for line in stream:
            self._note(source, {'event': 'stderr', 'text': line.strip()[-200:], 't': time.monotonic()})

    def start_monitor(self):
        if self.monitor is not None and self.monitor.poll() is None:
            self.monitor.kill()  # a restarted gate gets a fresh monitor channel
            self.monitor.wait(5)
        end = self.monitor_end
        self.monitor = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'monitor.py'), '--fd', str(end.fileno()),
                                         '--interval', str(self.interval)], pass_fds=(end.fileno(),),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, start_new_session=True, env={'PATH': '/usr/bin:/bin'})
        end.close()
        threading.Thread(target=self._reader, args=(self.monitor.stdout, 'monitor', self.monitor), daemon=True).start()

    def monitor_command(self, command):
        self._note('launcher', {'event': 'monitor_trigger', 'command': command, 't': time.monotonic()})
        self.monitor.stdin.write(command + '\n')
        self.monitor.stdin.flush()

    def kill_monitor(self):
        self._note('launcher', {'event': 'monitor_trigger', 'command': 'sigkill', 't': time.monotonic()})
        self.monitor.kill()
        self.monitor.wait(5)

    def kill_gate(self):
        self._note('launcher', {'event': 'gate_trigger', 'command': 'sigkill', 't': time.monotonic()})
        self.gate.kill()
        self.gate.wait(5)

    def control(self, op, timeout=15):
        count = len(self.replies)
        self._note('launcher', {'event': 'control_request', 'op': op, 't': time.monotonic()})
        self.control_sock.sendall((json.dumps({'op': op}) + '\n').encode())
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if len(self.replies) > count:
                    return self.replies[-1]
            time.sleep(0.02)
        return {'op': op, 'ok': False, 'reason': 'control_timeout'}

    # native -----------------------------------------------------------------------------
    def native_env(self):
        base = self.root / ('native-' + self.name)
        for part in ('home', 'cache', 'state', 'data', 'tmp', 'xdg', 'runtime', 'workspace'):
            (base / part).mkdir(mode=0o700, parents=True, exist_ok=True)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(base / 'home'), 'TMPDIR': str(base / 'tmp'),
               'TERM': 'xterm-256color', 'LANG': 'C.UTF-8', 'USER': 'fixture', 'LOGNAME': 'fixture',
               'XDG_CONFIG_HOME': str(base / 'xdg'), 'XDG_CACHE_HOME': str(base / 'cache'),
               'XDG_STATE_HOME': str(base / 'state'), 'XDG_DATA_HOME': str(base / 'data'),
               'XDG_RUNTIME_DIR': str(base / 'runtime'), 'CLAUDE_CONFIG_DIR': self.config_dir,
               'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY': '1'}
        if self.gate_proxy_env:
            env.update({'HTTPS_PROXY': self.gate_url, 'NO_PROXY': '127.0.0.1,localhost'})
        env.update(self.extra_env)
        return env, base

    def start_native(self, extra_args=()):
        env, base = self.native_env()
        self.workspace = base / 'workspace'
        settings = {'env': {'ANTHROPIC_BASE_URL': self.gate_url}, 'permissions': {'allow': ['Bash', 'Agent']}}
        settings.update(self.settings_extra)
        argv = [self.binary, '--safe-mode', '--model', 'claude-sonnet-4-6', '--permission-mode', 'dontAsk',
                '--settings', json.dumps(settings), *extra_args, self.prompt]
        inner = [sys.executable, '-I', '-B', str(NL / 'boundary.py'), '--gate-port', str(self.port),
                 '--seccomp', 'inet', '--verbose', '--', sys.executable, '-I', '-B', '-c', HELPER, *argv]
        if self.fs_rules:
            inner = [sys.executable, '-I', '-B', str(HERE / 'fsrules.py'), *self.fs_rules, '--', *inner]
        self.trace = self.root / ('strace-' + self.name)
        observer = ([self.use_strace, '-ff', '-q', '-s', '96', '-e',
                     'trace=execve,clone,clone3,fork,vfork,socket,socketpair,connect,sendto,sendmsg,sendmmsg,'
                     'bind,listen,getsockname', '-e', 'signal=none', '-o', str(self.trace)]
                    if self.use_strace else [])
        self.master, self.slave = pty.openpty()
        self.termios_before = termios.tcgetattr(self.slave)
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        self.started_monotonic = time.monotonic()
        self.native = subprocess.Popen([*observer, *inner], env=env, cwd=self.workspace, stdin=self.slave,
                                       stdout=self.slave, stderr=self.slave, close_fds=True,
                                       start_new_session=bool(observer))
        self._note('launcher', {'event': 'native_started', 't': self.started_monotonic})
        threading.Thread(target=self._terminal, daemon=True).start()

    def _terminal(self):
        while not self.done.is_set():
            if not select.select([self.master], [], [], 0.1)[0]:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            with self.lock:
                self.output.extend(data)
            if self.on_output:
                self.on_output(data)
            if b'\x1b[6n' in data:
                os.write(self.master, b'\x1b[1;1R')
            if b'\x1b[c' in data:
                os.write(self.master, b'\x1b[?1;2c')

    def write(self, data):
        os.write(self.master, data)

    def visible(self):
        with self.lock:
            return ANSI.sub('', self.output.decode(errors='replace'))

    def native_alive(self):
        return self.native is not None and self.native.poll() is None

    def exit_native(self, keys=(b'\x18\x0b', b'\x18\x0b', b'\x03', b'\x03', b'\x04')):
        """Stop native agents, then exit; confirm native's 'Exit and stop tasks' dialog if shown."""
        dialog, began = False, time.monotonic()
        for step in range(16):
            if self.native.poll() is not None:
                break
            if 'Exitandstoptasks' in re.sub(r'\s', '', self.visible()[-900:]) and not dialog:
                dialog = True
                self.write(b'\r')
                time.sleep(1.5)
                continue
            self.write(keys[step % len(keys)])
            time.sleep(0.6)
        end = time.monotonic() + 45
        while time.monotonic() < end and self.native.poll() is None:
            time.sleep(0.05)
        exited = self.native.poll() is not None
        exited_t = time.monotonic() if exited else None  # observed within one 50 ms poll
        try:
            restored = termios.tcgetattr(self.slave) == self.termios_before
        except termios.error:
            restored = None
        return {'exited': exited, 'termios_restored': restored, 'stop_tasks_dialog': dialog,
                'seconds': round(time.monotonic() - began, 1), 'began_t': began, 'exited_t': exited_t}

    # cleanup ------------------------------------------------------------------------------------
    def close(self, natives_module, since):
        self.done.set()
        traced = lambda: {p.name.rsplit('.', 1)[1] for p in self.root.glob('strace-%s.*' % self.name)}
        own = {p.pid for p in (self.native, self.gate, self.monitor) if p is not None}
        alive = [i for i in natives_module.lingering(self.root, traced(), since) if i['pid'] not in own]
        for item in alive:
            try:
                os.kill(item['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in (self.native, self.gate, self.monitor):  # gate before monitor: no cleanup latch
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process is not None:
                try:
                    process.wait(5)
                except subprocess.TimeoutExpired:
                    pass
        for fd in (self.master, self.slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.listener.close()
        if self.control_sock is not None:
            self.control_sock.close()
        result = {'lingering_killed': len(alive)}
        files = sorted(self.root.glob('strace-%s.*' % self.name))
        if files:
            parsed = natives_module.parse_traces(files)
            roots = parsed['roots']
            result['native_main_exit'] = parsed['exits'].get(roots[0]) if len(roots) == 1 else None
            result['syscalls'] = natives_module.summarize(parsed['rows'], self.port, -1, -1)
            result['traced_processes'] = len(files)
            result['traced_executables'] = sorted(set(parsed['exe'].values()))
        return result
