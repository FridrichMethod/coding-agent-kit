"""KM launcher: the trusted controller of one protected invocation on macOS (research only).

It owns the user's terminal and the gate port. Order of one invocation:
  1. refuse on host, native version, arguments, environment or quota policy (pure checks);
  2. bind the gate port in the launcher (port holder) and keep the listening sockets for the
     whole invocation, plus non-listening reservations of the same port on the wildcard and
     interface addresses that Seatbelt's `localhost` filter admits;
  3. render the Seatbelt profile (NM M1 plus com.apple.SecurityServer); start the monitor, then
     the gate with the holder's listening sockets; the Claude gate runs its preflight outside the
     boundary (memory-only bearer, same-bearer usage plus profile); any failure refuses;
  4. run the startup self-test under the profile (gate reachable, other port EPERM, bind EPERM,
     TIOCSTI EPERM) and refuse on mismatch, before any native code runs under the profile;
     only then, for Codex, a fresh in-boundary `codex app-server` read binds the first epoch;
  5. exec the native under /usr/bin/sandbox-exec on its own PTY with every other descriptor
     closed, and relay the terminal through PC5's TerminalControl: only the user's terminal can
     request a continuation (F12, a fresh typed challenge, then a fresh check).
A dead gate is restarted once (when configured) on the same held port; it starts paused.

python3 -I -B launcher.py CONFIG.json     (stdin/stdout are the user's terminal)
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty

HERE = Path(os.path.realpath(__file__)).parent
SANDBOX_EXEC = '/usr/bin/sandbox-exec'
MIN_VERSION = {'claude': (2, 1, 322), 'codex': (0, 166, 0)}  # minimums, never exact pins
VERSION_RE = {'claude': re.compile(r'^(\d+)\.(\d+)\.(\d+) \(Claude Code\)'),
              'codex': re.compile(r'^codex-cli (\d+)\.(\d+)\.(\d+)')}
STORE = 'com.apple.SecurityServer'
CLAUDE_REFUSED_ENV = ('CLAUDE_SECURESTORAGE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN',
                      'CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR', 'CLAUDE_CODE_OAUTH_REFRESH_TOKEN',
                      'CLAUDE_CODE_CUSTOM_OAUTH_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY',
                      'CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX',
                      'CLAUDE_CODE_USE_FOUNDRY', 'CLAUDE_CODE_USE_GATEWAY', 'CLAUDE_CODE_REMOTE_SESSION_ID',
                      'CLAUDE_LOCAL_OAUTH_API_BASE', 'ANTHROPIC_BASE_URL')
CODEX_REFUSED_ENV = ('CODEX_REFRESH_TOKEN_URL_OVERRIDE', 'CODEX_REVOKE_TOKEN_URL_OVERRIDE', 'OPENAI_BASE_URL',
                     'OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN')
CODEX_REFUSED_WORDS = {'resume', 'fork', 'remote-control', 'app-server', 'exec-server', 'cloud', 'login',
                       'logout', 'mcp-server', 'queue', 'sandbox',
                       'exec', 'e', 'review'}  # headless model modes: no trusted terminal to continue from
CODEX_REFUSED_OPTIONS = {'--remote', '--remote-auth-token-env', '--oss', '--local-provider', '-p', '--profile'}
CODEX_PROTECTED_KEYS = ('model_provider', 'model_providers', 'chatgpt_base_url', 'openai_base_url',
                        'cli_auth_credentials_store', 'sandbox_mode', 'profile', 'profiles')
CLAUDE_REFUSED_OPTIONS = {'--settings', '--setting-sources', '--mcp-config', '--dangerously-skip-permissions',
                          '-p', '--print', '--remote', '--teleport'}
FAMILIES = {'claude': 'claude-subscription-general', 'codex': 'codex'}


class Refusal(Exception):
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- pure checks --------------------------------------------------------------------------
def check_version(provider, text):
    match = VERSION_RE[provider].match((text or '').strip())
    if not match:
        raise Refusal('unrecognized native version output')
    version = tuple(int(x) for x in match.groups())
    if version < MIN_VERSION[provider]:
        raise Refusal('native %s is older than the protected-mode minimum %s'
                      % ('.'.join(map(str, version)), '.'.join(map(str, MIN_VERSION[provider]))))
    return version


def check_args(provider, argv):
    """User arguments -> native arguments the kit adds to, or Refusal."""
    for index, word in enumerate(argv):
        name = word.split('=', 1)[0]
        if provider == 'claude':
            if name in CLAUDE_REFUSED_OPTIONS:
                raise Refusal('protected mode refuses ' + name)
            continue
        if name in CODEX_REFUSED_OPTIONS:
            raise Refusal('protected mode refuses ' + name)
        if name in ('-c', '--config'):
            value = word.split('=', 1)[1] if '=' in word else (argv[index + 1] if index + 1 < len(argv) else '')
            key = value.split('=', 1)[0].strip()
            if any(key == k or key.startswith(k + '.') for k in CODEX_PROTECTED_KEYS):
                raise Refusal('protected mode refuses a user override of ' + key)
        if not word.startswith('-') and word in CODEX_REFUSED_WORDS and could_be_subcommand(argv, index):
            raise Refusal('protected mode refuses the %s subcommand' % word)
    if provider == 'codex':
        return ['--no-daemon'] + [w for w in argv if w != '--no-daemon']
    return list(argv)


def could_be_subcommand(argv, index):
    """Whether argv[index] can be the native's subcommand: every earlier word is an option or
    may be an option's value (the word right after an option). Conservative: the kit does not
    track which native options take a value, so `-m gpt resume` counts as a subcommand."""
    for position, previous in enumerate(argv[:index]):
        if not previous.startswith('-') and not (position and argv[position - 1].startswith('-')
                                                  and '=' not in argv[position - 1]):
            return False
    return True


def check_env(provider, env):
    for name in CLAUDE_REFUSED_ENV if provider == 'claude' else CODEX_REFUSED_ENV:
        if name in env:
            raise Refusal('protected mode refuses the environment variable ' + name)


def check_policy(provider, policy):
    if not isinstance(policy, dict) or policy.get('family') != FAMILIES[provider]:
        raise Refusal('unsupported quota family')
    reserves = policy.get('reserves')
    if not isinstance(reserves, dict) or set(reserves) - {'five_hour', 'weekly'}:
        raise Refusal('malformed quota policy')
    enabled = {k: v for k, v in reserves.items() if v is not None}
    if not enabled:
        raise Refusal('no reserve enabled')
    for value in enabled.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 100:
            raise Refusal('malformed quota policy')
    age = policy.get('max_age_s')
    if isinstance(age, bool) or not isinstance(age, (int, float)) or not 0 < age <= 3600:
        raise Refusal('malformed quota policy')
    return enabled


def check_host(sandbox_exec, platform=sys.platform):
    if platform != 'darwin':
        raise Refusal('protected mode on this host needs macOS')
    if not os.path.isfile(sandbox_exec) or not os.access(sandbox_exec, os.X_OK):
        raise Refusal('boundary precondition missing: %s is not an executable file' % sandbox_exec)


def parse_proxy(url):
    """The launcher's HTTPS_PROXY, inherited once at startup for wrapper-originated upstream
    connections (provisional decision): plain http://HOST:PORT only, else Refusal."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url or '')
        port = parts.port
    except ValueError:
        raise Refusal('unsupported HTTPS_PROXY for protected mode') from None
    if parts.scheme != 'http' or not parts.hostname or not port or parts.username or parts.password \
            or parts.path not in ('', '/'):
        raise Refusal('unsupported HTTPS_PROXY for protected mode')
    return [parts.hostname, port]


def parse_ifconfig(text):
    """Local IPv4/IPv6 addresses (scope kept) from `ifconfig -a`; loopback excluded."""
    found = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in ('inet', 'inet6'):
            address = parts[1]
            if address in ('127.0.0.1', '::1') or address.startswith('127.'):
                continue
            found.append((parts[0], address))
    return found


def profile_policy(tag, gate_port, deny_write, store=True):
    """NM M1 (TCP gate port only, mach allowlist, launch, signal and TIOCSTI denials) plus,
    under provisional decision 6(a), com.apple.SecurityServer for the native Keychain."""
    mach = ['com.apple.FSEvents', 'com.apple.PowerManagement.control',
            'com.apple.SystemConfiguration.DNSConfiguration', 'com.apple.SystemConfiguration.configd',
            'com.apple.cfprefsd.agent', 'com.apple.cfprefsd.daemon', 'com.apple.logd',
            'com.apple.system.notification_center', 'com.apple.system.opendirectoryd.libinfo',
            'com.apple.system.opendirectoryd.membership']
    if store:
        mach.append(STORE)
    return {'tag': tag, 'gate_ports': [gate_port], 'deny_mach': True, 'mach_allow': mach,
            'deny_signal_others': True, 'deny_tiocsti': True, 'gate_tcp_only': True,
            'deny_write': list(deny_write)}


def fs_rules(writable, denied_after):
    """Outer write rules replacing native filesystem confinement (evaluated, not adopted):
    every write denied except the listed subpaths and the character devices natives need."""
    quote = lambda p: '"' + p + '"'  # noqa: E731 - paths are already resolved and checked by the renderer
    lines = ['(deny file-write* (subpath "/"))',
             '(allow file-write* %s (literal "/dev/null") (literal "/dev/tty") (literal "/dev/ptmx")'
             ' (regex #"^/dev/ttys[0-9]+$") (literal "/dev/dtracehelper") (regex #"^/dev/fd/"))' % ' '.join(
                 '(subpath %s)' % quote(p) for p in writable)]
    if denied_after:
        lines.append('(deny file-write* %s)' % ' '.join('(subpath %s)' % quote(p) for p in denied_after))
    return '\n'.join(lines) + '\n'


SELF_TEST = r'''
import fcntl, json, os, socket, sys, termios
spec = json.loads(sys.argv[1]); res = {}
for name, port in (('gate', spec['gate']), ('other', spec['other'])):
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect(('127.0.0.1', port)); res[name] = 'connected'
    except OSError as e:
        res[name] = 'errno:%s' % e.errno
    s.close()
s = socket.socket()
try:
    s.bind(('127.0.0.1', 0)); s.listen(1); res['bind'] = 'bound'
except OSError as e:
    res['bind'] = 'errno:%s' % e.errno
s.close()
master, slave = os.openpty()
pid = os.fork()
if pid == 0:
    os.setsid(); fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
    try:
        fcntl.ioctl(slave, termios.TIOCSTI, b'x'); os._exit(10)
    except OSError as e:
        os._exit(20 + (e.errno or 0))
_, status = os.waitpid(pid, 0)
code = os.waitstatus_to_exitcode(status)
res['tiocsti'] = 'injected' if code == 10 else 'errno:%d' % (code - 20)
print(json.dumps(res))
'''


def self_test_ok(result):
    return (isinstance(result, dict) and result.get('gate') == 'connected' and result.get('other') == 'errno:1'
            and result.get('bind') == 'errno:1' and result.get('tiocsti') == 'errno:1')


# -- process side --------------------------------------------------------------------------
class Launcher:
    def __init__(self, config):
        self.config = config
        self.case = Path(config['case_dir'])
        self.log = open(self.case / 'launcher.jsonl', 'a', buffering=1)
        self.children = []
        self.gate = self.monitor = None
        self.control = None
        self.buffer = b''
        self.replies = []
        self.events = []
        self.restarts = 0
        self.term = None
        self.control_ui = None

    def emit(self, event, **fields):
        self.log.write(json.dumps({'event': event, 't': time.monotonic(), **fields}, sort_keys=True,
                                  default=str) + '\n')

    def say(self, text):
        os.write(1, ('\r\n[coding-agent-kit] ' + text + '\r\n').encode())
        self.emit('said', text=text)

    # -- startup --
    def checks(self):
        c = self.config
        check_host(c.get('sandbox_exec', SANDBOX_EXEC))
        plain = {k: v for k, v in c['env'].items() if '{GATE}' not in v}
        version = subprocess.run([c['native'], '--version'], capture_output=True, text=True, timeout=30,
                                 env=plain).stdout
        self.emit('native_version', text=version.strip()[:80])
        check_version(c['provider'], c.get('version_override', version))
        check_env(c['provider'], c.get('user_env', {}))
        self.native_args = check_args(c['provider'], c.get('user_args', []))
        check_policy(c['provider'], c['policy'])
        inherited = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        if inherited and 'upstream_proxy' not in c['gate']:
            c['gate']['upstream_proxy'] = parse_proxy(inherited)
            self.emit('proxy_inherited', proxy=c['gate']['upstream_proxy'])

    def hold(self):
        mode = self.config.get('holder', 'all')
        l4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        l4.bind(('127.0.0.1', 0))
        port = l4.getsockname()[1]
        l6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        l6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        l6.bind(('::1', port))
        for s in (l4, l6):
            s.listen(64)
            s.set_inheritable(True)
        reserved, failed = [], []
        if mode in ('wildcard', 'all'):
            extra = [('inet', '0.0.0.0'), ('inet6', '::')]
            if mode == 'all':
                text = subprocess.run(['/sbin/ifconfig', '-a'], capture_output=True, text=True, timeout=10).stdout
                extra += parse_ifconfig(text)
            for family, address in extra:
                fam = socket.AF_INET if family == 'inet' else socket.AF_INET6
                s = socket.socket(fam, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if fam == socket.AF_INET6:
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                try:
                    if fam == socket.AF_INET6:
                        info = socket.getaddrinfo(address, port, fam, socket.SOCK_STREAM)[0][4]
                        s.bind(info)
                    else:
                        s.bind((address, port))
                    reserved.append(s)
                except OSError as error:
                    failed.append(error.errno)
                    s.close()
        self.listeners, self.reserved, self.port = [l4, l6], reserved, port
        self.emit('holder', port=port, mode=mode, reserved=len(reserved), failed=failed)
        return port

    def substitute(self):
        """Pin the native's model and proxy URLs to the held port ({GATE} in the fixture config)."""
        c, port = self.config, str(self.port)
        c['env'] = {k: v.replace('{GATE}', port) for k, v in c['env'].items()}
        c['fixed_args'] = [a.replace('{GATE}', port) for a in c.get('fixed_args', [])]
        c['native_pre'] = [a.replace('{GATE}', port) for a in c.get('native_pre', [])]

    def spawn_monitor(self, sock):
        log = open(self.case / 'monitor.jsonl', 'ab')
        proc = subprocess.Popen([self.config['python'], '-I', '-B', str(HERE / 'monitor.py'), '--fd',
                                 str(sock.fileno()), '--interval', str(self.config['monitor_interval_s'])],
                                pass_fds=(sock.fileno(),), stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
        log.close()
        self.children.append(proc)
        self.emit('monitor_started', pid=proc.pid)
        return proc

    def spawn_gate(self, mode):
        mon_gate, mon_side = socket.socketpair()
        ctl_gate, ctl_side = socket.socketpair()
        self.monitor = self.spawn_monitor(mon_side)
        mon_side.close()  # the monitor's death must reach the gate as EOF
        gconf = dict(self.config['gate'], mode=mode)
        path = self.case / ('gate-config-%d.json' % self.restarts)
        path.write_text(json.dumps(gconf))
        os.chmod(str(path), 0o600)
        fds = [s.fileno() for s in self.listeners] + [mon_gate.fileno(), ctl_gate.fileno()]
        out = open(self.case / 'gate.jsonl', 'ab')
        err = open(self.case / 'gate.err', 'ab')
        proc = subprocess.Popen([self.config['python'], '-I', '-B', str(HERE / 'gate.py'), str(path),
                                 '--listen-fds', ','.join(str(s.fileno()) for s in self.listeners),
                                 '--monitor-fd', str(mon_gate.fileno()), '--control-fd', str(ctl_gate.fileno())],
                                pass_fds=fds, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                start_new_session=True)
        out.close()
        err.close()
        mon_gate.close()
        ctl_gate.close()
        self.children.append(proc)
        self.gate, self.control, self.buffer = proc, ctl_side, b''
        self.emit('gate_started', pid=proc.pid, mode=mode)
        return self.wait_gate(proc)

    def wait_gate(self, proc, seconds=40):
        path = self.case / 'gate.jsonl'
        deadline = time.monotonic() + seconds
        seen = 0
        while time.monotonic() < deadline:
            lines = path.read_text().splitlines()
            for line in lines[seen:]:
                row = json.loads(line)
                if row.get('event') == 'gate_ready' and row.get('pid') == proc.pid:
                    return True, None
                if row.get('event') == 'startup_refused':
                    return False, row.get('reason')
            seen = len(lines)
            if proc.poll() is not None:
                return False, 'gate_exited'
            time.sleep(0.05)
        return False, 'gate_timeout'

    def request(self, op, seconds=15):
        """One control round trip; a missing or broken control channel (dead or restarting gate)
        answers 'still paused' instead of raising into the terminal relay."""
        if self.control is None:
            return {'ok': False, 'reason': 'gate_unavailable'}
        try:
            self.control.sendall((json.dumps({'op': op}) + '\n').encode())
        except OSError:
            self.control = None
            return {'ok': False, 'reason': 'gate_unavailable'}
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.read_control(0.1)
            for reply in self.replies:
                if reply.get('op') == 'reply:' + op:
                    self.replies.remove(reply)
                    return reply
        return {'ok': False, 'reason': 'control_timeout'}

    def read_control(self, timeout):
        if self.control is None or not select.select([self.control], [], [], timeout)[0]:
            return
        try:
            data = self.control.recv(65536)
        except OSError:
            data = b''
        if not data:
            self.control = None
            return
        self.buffer += data
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if str(message.get('op', '')).startswith('reply:'):
                self.replies.append(message)
            else:
                self.events.append(message)

    def profile(self):
        c = self.config
        sbpl = _load('km_nm_sbpl', HERE.parent / 'nm' / 'sbpl.py')
        tag = c['tag']
        text = sbpl.render(profile_policy(tag, self.port, c['deny_write'], store=c.get('store', True)))
        if c.get('fs_rules'):
            text += fs_rules(c['fs_rules'], c['deny_write'])
        text += c.get('profile_extra', '')
        (self.case / 'profile.sb').write_text(text)
        self.emit('profile', lines=len(text.splitlines()), fs_rules=bool(c.get('fs_rules')))
        return text

    def self_test(self, profile):
        other = socket.socket()
        other.bind(('127.0.0.1', 0))
        other.listen(4)
        spec = json.dumps({'gate': self.port, 'other': other.getsockname()[1]})
        began = time.monotonic()
        try:
            done = subprocess.run([self.config.get('sandbox_exec', SANDBOX_EXEC), '-p', profile,
                                   self.config['python'], '-I', '-B', '-c', SELF_TEST, spec],
                                  capture_output=True, text=True, timeout=20, env=self.config['env'],
                                  start_new_session=True)
            result = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else \
                {'exit': done.returncode, 'stderr': done.stderr[-300:]}
        except (subprocess.TimeoutExpired, ValueError, IndexError) as error:
            result = {'error': type(error).__name__}
        finally:
            other.close()
        self.emit('self_test', result=result, ok=self_test_ok(result), ms=round((time.monotonic() - began) * 1000, 1))
        return self_test_ok(result)

    def reader(self, profile):
        """Codex: one fresh in-boundary `codex app-server` usage read; the gate observes it."""
        c = self.config
        began = time.monotonic()
        argv = [c.get('sandbox_exec', SANDBOX_EXEC), '-p', profile, c['native']] + c.get('native_pre', []) + \
            ['app-server']
        err = open(self.case / 'reader.err', 'ab')
        proc = subprocess.Popen(argv, env=c['env'], cwd=c['cwd'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=err, start_new_session=True)
        err.close()
        buffer, result = b'', None

        def send(message):
            proc.stdin.write((json.dumps(message) + '\n').encode())
            proc.stdin.flush()

        def receive(ident, seconds=10):
            nonlocal buffer
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if message.get('id') == ident and 'method' not in message:
                        return message
                if select.select([proc.stdout], [], [], 0.1)[0]:
                    data = os.read(proc.stdout.fileno(), 65536)
                    if not data:
                        return None
                    buffer += data
            return None
        try:
            send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'km-controller', 'version': '1'},
                                                              'capabilities': {'experimentalApi': True}}})
            if receive(1):
                send({'method': 'initialized', 'params': {}})
                send({'id': 2, 'method': 'account/rateLimits/read', 'params': {'excludeResetCreditDetails': True}})
                answer = receive(2) or {}
                result = 'result' in answer
        except OSError:
            result = None
        finally:
            try:
                proc.stdin.close()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=3)
        self.emit('reader', ok=result, seconds=round(time.monotonic() - began, 3))
        return result

    # -- run --
    def start_native(self, profile):
        c = self.config
        master, slave = os.openpty()
        size = fcntl.ioctl(0, termios.TIOCGWINSZ, b'\0' * 8) if os.isatty(0) else struct.pack('HHHH', 40, 120, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
        helper = ('import os,fcntl,termios,sys; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); '
                  'os.execv(sys.argv[1],sys.argv[1:])')
        argv = [c['python'], '-I', '-B', '-c', helper, c.get('sandbox_exec', SANDBOX_EXEC), '-p', profile,
                c['native']] + c.get('native_pre', []) + c.get('fixed_args', []) + self.native_args
        proc = subprocess.Popen(argv, env=c['env'], cwd=c['cwd'], stdin=slave, stdout=slave, stderr=slave,
                                close_fds=True)
        os.close(slave)
        self.emit('native_started', pid=proc.pid)
        return proc, master

    def check(self):
        """TerminalControl's check(): one fresh verified continuation through the gate."""
        began = time.monotonic()
        reply = self.request('continue', 20)
        self.emit('continue_reply', ms=round((time.monotonic() - began) * 1000, 1),
                  **{k: v for k, v in reply.items() if k != 'op'})
        if self.config['provider'] == 'codex' and reply.get('needs_read'):
            self.reader(self.profile_text)
            status = self.request('status')
            ok = status.get('paused') is None and status.get('ready')
            self.emit('continue_status', ok=bool(ok), reason=status.get('reason'),
                      ms=round((time.monotonic() - began) * 1000, 1))
            if not ok:
                self.say('still paused: %s' % status.get('reason'))
            return bool(ok)
        if not reply.get('ok'):
            self.say('still paused: %s' % reply.get('reason'))
        return bool(reply.get('ok'))

    def refuse(self, reason):
        self.emit('refused', reason=reason)
        self.say('protected mode refused: %s (no model request was sent)' % reason)
        return 3

    def run(self):
        c = self.config
        try:
            self.checks()
        except Refusal as error:
            return self.refuse(str(error))
        self.hold()
        self.substitute()
        self.profile_text = profile = self.profile()
        ok, reason = self.spawn_gate('startup')
        if not ok:
            return self.refuse(reason)
        # The self-test precedes every native process under the profile (the Codex reader too).
        if c.get('self_test', True) and not self.self_test(profile):
            return self.refuse('boundary self-test failed')
        if c['provider'] == 'codex':
            self.reader(profile)
            status = self.request('status')
            if not status.get('ready'):
                return self.refuse('preflight: %s' % (status.get('reason') or 'no fresh passing usage read'))
        control = _load('km_pc_control', HERE.parent / 'pc_continue' / 'control.py')
        native, master = self.start_native(profile)
        outer = termios.tcgetattr(0) if os.isatty(0) else None
        if outer is not None:
            tty.setraw(0)
        ui = control.TerminalControl(0, lambda data: os.write(master, data), self.say, self.check,
                                     lambda: self.emit('granted'))
        self.control_ui = ui
        code = None
        dead_since = None
        try:
            while True:
                if native.poll() is not None:
                    code = native.returncode
                    break
                ready = select.select([0, master] + ([self.control] if self.control else []), [], [], 0.05)[0]
                if master in ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b''
                    if data:
                        os.write(1, data)
                if 0 in ready:
                    try:
                        ui.pump(0)
                    except EOFError:
                        break
                elif ui.pending:
                    ui.pump(0)
                if self.control in ready:
                    self.read_control(0)
                for message in self.events:
                    self.emit('gate_notify', **message)
                    if message.get('op') == 'latched' and not ui.paused:
                        ui.pause()
                        self.say('quota reserve paused (%s); press F12 to request a fresh check'
                                 % message.get('reason'))
                    elif message.get('op') == 'resumed':
                        self.say('quota reserve resumed after a fresh check')
                self.events = []
                if self.gate.poll() is not None and dead_since is None:
                    # No gate: nothing can be admitted. Keep relaying the terminal meanwhile.
                    dead_since = time.monotonic()
                    self.emit('gate_exited', code=self.gate.returncode)
                    if not ui.paused:
                        ui.pause()
                    self.say('gate stopped; requests are refused until a restarted gate gets a fresh check')
                if dead_since is not None and c.get('restart_gate') and self.restarts < 1 and \
                        time.monotonic() - dead_since >= float(c.get('restart_delay_s', 0)):
                    self.restarts += 1
                    if self.monitor and self.monitor.poll() is None:
                        os.killpg(self.monitor.pid, signal.SIGKILL)
                    ok, reason = self.spawn_gate('restart')
                    self.emit('gate_restarted', ok=ok, reason=reason)
                    dead_since = None if ok else dead_since
                    self.say('gate restarted paused; press F12 to request a fresh check')
        finally:
            if outer is not None:
                termios.tcsetattr(0, termios.TCSADRAIN, outer)
            if native.poll() is None:
                try:
                    os.killpg(native.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.emit('native_exit', code=code if code is not None else native.poll())
        return code if code is not None else 0

    def cleanup(self):
        for proc in self.children:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        for s in getattr(self, 'listeners', []) + getattr(self, 'reserved', []):
            s.close()
        self.emit('launcher_exit')


def main(argv):
    config = json.loads(Path(argv[0]).read_text())
    os.umask(0o077)
    launcher = Launcher(config)
    try:
        return launcher.run()
    finally:
        launcher.cleanup()


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
