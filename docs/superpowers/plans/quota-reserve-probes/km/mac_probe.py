"""KM macOS composed-invocation probes; run on the Mac only via run.py (research only).

python3 -I -B mac_probe.py ROOT STAGE TOOLS_JSON [DEADLINE_SECONDS]

Each case builds fabricated accounts under ROOT, starts loopback fixtures, and runs the KM
launcher (trusted terminal owner and port holder) on an outer PTY that plays the user's
terminal. The launcher starts the monitor and the gate, renders the Seatbelt profile, runs the
self-test and execs the real native under /usr/bin/sandbox-exec. Results go to
ROOT/out/STAGE.json with the raw gate, launcher and upstream rows that check_evidence.py
recomputes verdicts from. Every child is bounded; process groups are killed; leftovers naming
ROOT are reaped at the end.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pty
import pwd
import re
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time

HERE = Path(os.path.realpath(__file__)).parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FX = _load('km_fixtures', HERE / 'fixtures.py')
LAUNCHER = _load('km_launcher', HERE / 'launcher.py')
SANDBOX_EXEC = '/usr/bin/sandbox-exec'
CLAUDE_MODELS = ['claude-sonnet-4-6', 'claude-haiku-4-5', 'claude-haiku-4-5-20251001']
IDENT_A, IDENT_B = ('km-acct-a', 'km-org-a'), ('km-acct-b', 'km-org-b')
CODEX_A, CODEX_B = ('km-codex-acct-a', 'km-codex-user-a'), ('km-codex-acct-b', 'km-codex-user-b')
KEEP = ('decision', 'forwarded', 'stream_closed', 'aborted_before_upstream', 'connect', 'tunnel_closed', 'lost',
        'notify', 'startup_refused', 'preflight', 'gate_ready', 'route_refused', 'read_refused', 'control')
CHALLENGE = re.compile(r'Type ([0-9a-f]{8}) then Enter')
F12 = b'\x1b[24~'


def now():
    return time.monotonic()


class Terminal:
    """The user's terminal: an outer PTY whose slave is the launcher's stdin and stdout."""

    def __init__(self):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        self.output = bytearray()
        self.stop = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while not self.stop.is_set():
            if not select.select([self.master], [], [], .1)[0]:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if len(self.output) < 6_000_000:
                self.output.extend(data)
            if b'\x1b[6n' in data:
                os.write(self.master, b'\x1b[1;1R')
            if b'\x1b[c' in data:
                os.write(self.master, b'\x1b[?1;2c')

    def write(self, data):
        os.write(self.master, data)

    def text(self, since=0):
        return bytes(self.output[since:]).decode(errors='replace')

    def mark(self):
        return len(self.output)

    def close(self):
        self.stop.set()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


def jsonl(path):
    rows = []
    try:
        for line in Path(path).read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return rows


class Invocation:
    def __init__(self, case, name, provider, policy, **opts):
        self.case, self.name, self.provider, self.opts = case, name, provider, opts
        self.dir = case.dir / name
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.term = Terminal()
        self.proc = None
        self.policy = policy
        self.ctx = case.ctx

    def config(self):
        ctx, case, o = self.ctx, self.case, self.opts
        provider = self.provider
        env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(case.home), 'TMPDIR': str(case.tmp) + '/',
               'USER': 'kmfixture', 'LOGNAME': 'kmfixture', 'SHELL': '/bin/bash', 'LANG': 'en_US.UTF-8',
               'TERM': 'xterm-256color', 'XDG_CONFIG_HOME': str(case.root_dir / 'xdg'),
               'XDG_CACHE_HOME': str(case.root_dir / 'cache'), 'XDG_STATE_HOME': str(case.root_dir / 'state'),
               'XDG_DATA_HOME': str(case.root_dir / 'data')}
        gate = {'provider': provider, 'upstream_port': case.upstream.port, 'policy': {
            'reserves': self.policy['reserves'], 'max_age_s': self.policy['max_age_s'],
            'allowed_models': CLAUDE_MODELS, 'revalidations_per_minute': o.get('budget', 6)},
            'heartbeat_max_age_s': o.get('beat_max', 3.0), 'connect_allow': o.get('connect_allow', {}),
            'labels': case.labels}
        if provider == 'claude':
            env.update(CLAUDE_CONFIG_DIR=str(case.cfg), DISABLE_AUTOUPDATER='1',
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY='1',
                       PATH=str(case.shim) + ':/usr/bin:/bin:/usr/sbin:/sbin',
                       HTTPS_PROXY='http://127.0.0.1:{GATE}', https_proxy='http://127.0.0.1:{GATE}',
                       NO_PROXY='127.0.0.1,localhost,::1', no_proxy='127.0.0.1,localhost,::1')
            if o.get('ca'):
                env['NODE_EXTRA_CA_CERTS'] = str(o['ca'])
            settings = {'permissions': {'allow': ['Bash']}, 'env': {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:{GATE}'}}
            if o.get('native_sandbox'):
                settings['sandbox'] = {'enabled': True, 'autoAllowBashIfSandboxed': True}
            else:
                settings['sandbox'] = {'enabled': False}
            # --tools is variadic: keep it before another option so the prompt stays positional.
            fixed = ['--tools', 'Bash', '--settings', json.dumps(settings), '--model', CLAUDE_MODELS[0],
                     '--permission-mode', 'default']
            gate.update(usage_port=case.usage.port, config_dir=str(case.cfg), registered=list(IDENT_A))
            pre = []
        else:
            env.update(CODEX_HOME=str(case.codex), CODEX_SQLITE_HOME=str(case.codex))
            if o.get('proxy', True):
                env.update(HTTPS_PROXY='http://127.0.0.1:{GATE}', https_proxy='http://127.0.0.1:{GATE}',
                           NO_PROXY='127.0.0.1,localhost,::1', no_proxy='127.0.0.1,localhost,::1')
            if o.get('ca'):
                env['CODEX_CA_CERTIFICATE'] = str(o['ca'])
            fixed = ['--no-alt-screen', '-C', str(case.workspace)]
            gate.update(expected_account=CODEX_A[0], expected_user=CODEX_A[1])
            mode = 'workspace-write' if o.get('native_sandbox') else 'danger-full-access'
            FX.codex_config(case.codex, 'http://127.0.0.1:9', str(case.workspace), mode)
            pre = ['-c', 'chatgpt_base_url="http://127.0.0.1:{GATE}/backend-api"',
                   '-c', 'model_providers.gate.base_url="http://127.0.0.1:{GATE}/backend-api/codex"']
        config = {'provider': provider, 'native': ctx.tools[provider], 'python': ctx.python, 'env': env,
                  'cwd': str(case.workspace), 'fixed_args': fixed, 'user_args': o.get('user_args', []),
                  'user_env': o.get('user_env', {}), 'policy': self.policy, 'monitor_interval_s': 0.5,
                  'gate': gate, 'holder': o.get('holder', 'all'), 'tag': ctx.tag(), 'case_dir': str(self.dir),
                  'deny_write': [str(ctx.root / 'anchors'), '/private/var/at',
                                 os.path.realpath(ctx.real_home) + '/Library/LaunchAgents'],
                  'store': o.get('store', True), 'restart_gate': o.get('restart_gate', False),
                  'restart_delay_s': o.get('restart_delay_s', 0), 'self_test': o.get('self_test', True),
                  'native_pre': pre, 'fs_rules': o.get('fs_rules'), 'profile_extra': o.get('profile_extra', ''),
                  'sandbox_exec': o.get('sandbox_exec', SANDBOX_EXEC)}
        if 'version_override' in o:
            config['version_override'] = o['version_override']
        if o.get('prompt'):
            config['user_args'] = config['user_args'] + [o['prompt']]
        return config

    def launch(self):
        path = self.dir / 'launcher-config.json'
        FX.write_private(path, json.dumps(self.config()))
        helper = ('import os,fcntl,termios,sys; os.setsid(); fcntl.ioctl(0,termios.TIOCSCTTY,0); '
                  'os.execv(sys.argv[1],sys.argv[1:])')
        self.proc = subprocess.Popen([self.ctx.python, '-I', '-B', '-c', helper, self.ctx.python, '-I', '-B',
                                      str(HERE / 'launcher.py'), str(path)], cwd=str(self.case.workspace),
                                     stdin=self.term.slave, stdout=self.term.slave, stderr=self.term.slave,
                                     close_fds=True, env={'PATH': '/usr/bin:/bin', 'LANG': 'en_US.UTF-8'})
        self.ctx.groups.append(self.proc.pid)
        self.started = now()
        return self

    # -- observations --
    def gate(self, event=None, kind=None):
        rows = jsonl(self.dir / 'gate.jsonl')
        if event:
            rows = [r for r in rows if r.get('event') == event]
        if kind:
            rows = [r for r in rows if r.get('kind') == kind]
        return rows

    def launcher(self, event=None):
        rows = jsonl(self.dir / 'launcher.jsonl')
        return [r for r in rows if r.get('event') == event] if event else rows

    def port(self):
        rows = self.launcher('holder')
        return rows[0]['port'] if rows else None

    def pid(self, event):
        rows = self.launcher(event)
        return rows[-1]['pid'] if rows else None

    def wait_launcher(self, event, seconds=60):
        return FX.wait_for(lambda: bool(self.launcher(event)) or (self.proc.poll() is not None), seconds) and \
            bool(self.launcher(event))

    def latch_t(self, after=0):
        rows = [r for r in self.gate('controller', 'latched') if r['t'] > after]
        return rows[0]['t'] if rows else None

    def wait_latch(self, seconds=20, after=0):
        FX.wait_for(lambda: self.latch_t(after) is not None, seconds)
        return self.latch_t(after)

    def decisions(self, after=0, ok=None, label=None):
        rows = [r for r in self.gate('decision') if r['t'] > after]
        if ok is not None:
            rows = [r for r in rows if r.get('ok') == ok]
        if label is not None:
            rows = [r for r in rows if r.get('label') == label]
        return rows

    def native_alive(self):
        pid = self.pid('native_started')
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def turn(self, text):
        mark = now()
        self.term.write(text.encode())
        time.sleep(1.2)
        self.term.write(b'\r')
        return mark

    def local_command(self):
        """A native-local command after a pause; returns whether the TUI output changed."""
        before = self.term.mark()
        if self.provider == 'claude':
            self.term.write(b'/help')
            time.sleep(1)
            self.term.write(b'\r')
            time.sleep(2.5)
            self.term.write(b'\x1b')
        else:
            self.term.write(b'/status')
            time.sleep(1)
            self.term.write(b'\r')
            time.sleep(2.5)
        time.sleep(1)
        grew = self.term.mark() - before
        return {'output_bytes': grew, 'alive': self.native_alive()}

    def exit(self):
        keys = [b'\x03', b'\x03'] if self.provider == 'codex' else [b'\x1b', b'/exit\r']
        for key in keys:
            if self.proc.poll() is not None:
                break
            try:
                self.term.write(key)
            except OSError:
                break
            time.sleep(0.6)
        try:
            self.proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            self.ctx.kill(self.proc.pid)
        result = {'launcher_exit': self.proc.poll()}
        exits = self.launcher('native_exit')
        result['native_exit'] = exits[-1].get('code') if exits else None
        return result

    def summary(self):
        quiet = ('quota_fetch', 'extended', 'paused_poll', 'not_extended')
        gate = [r for r in jsonl(self.dir / 'gate.jsonl') if r.get('event') in KEEP or
                (r.get('event') == 'controller' and r.get('kind') not in quiet) or
                (r.get('event') == 'fetch' and r.get('cause') not in (None, 'poll'))]
        polls = [r for r in jsonl(self.dir / 'gate.jsonl') if r.get('event') == 'poll']
        fetches = [r for r in jsonl(self.dir / 'gate.jsonl') if r.get('event') == 'fetch']
        return {'gate_rows': gate[-400:], 'poll_count': len(polls),
                'poll_ms': sorted(r['ms'] for r in polls)[len(polls) // 2] if polls else None,
                'fetch_ms': sorted(r['ms'] for r in fetches)[len(fetches) // 2] if fetches else None,
                'launcher_rows': [r for r in self.launcher() if r.get('event') != 'gate_notify'][-200:],
                'monitor_rows': jsonl(self.dir / 'monitor.jsonl')[-20:],
                'gate_stderr_tail': _tail(self.dir / 'gate.err'),
                'terminal_head': FX_strip(self.term.text())[:2500],
                'terminal_tail': FX_strip(self.term.text())[-2500:]}

    def close(self):
        if self.proc is not None and self.proc.poll() is None:
            self.ctx.kill(self.proc.pid)
        self.term.close()


def _tail(path, n=1500):
    try:
        return Path(path).read_text(errors='replace')[-n:]
    except OSError:
        return ''


_ANSI = re.compile(r'\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[78=>]')


def FX_strip(text):
    return re.sub(r'\s+', ' ', _ANSI.sub(' ', text))


class Case:
    """One case: fabricated accounts, fixtures and helpers; cleaned up by close()."""

    def __init__(self, ctx, name, provider):
        self.ctx, self.name, self.provider = ctx, name, provider
        self.dir = ctx.root / 'cases' / name
        if self.dir.exists():  # an earlier run of this case in the same root: start clean
            import shutil
            shutil.rmtree(self.dir)
        self.root_dir = self.dir
        for part in ('home', 'tmp', 'xdg', 'cache', 'state', 'data', 'workspace', 'shim', 'out',
                     'accounts', 'accounts/a', 'accounts/a/cfg', 'codex', 'outside'):
            (self.dir / part).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.home, self.tmp, self.workspace = self.dir / 'home', self.dir / 'tmp', self.dir / 'workspace'
        self.cfg, self.codex, self.shim, self.out = (self.dir / 'accounts/a/cfg', self.dir / 'codex',
                                                     self.dir / 'shim', self.dir / 'out')
        self.upstream = FX.Upstream()
        self.usage = FX.ClaudeUsage()
        self.labels, self.tokens = {}, {}
        self.servers = [self.upstream, self.usage]
        self.invocations = []
        self.write_security_shim()

    def write_security_shim(self):
        """PATH shim for /usr/bin/security (NM's device): find returns 44 (not found), anything else
        fails, so no real Keychain item is ever read or written; argv is logged with secrets redacted."""
        log = self.out / 'security-argv.jsonl'
        shim = self.shim / 'security'
        shim.write_text('#!%s -I\nimport json, sys\nargs = sys.argv[1:]\nsafe = list(args)\n'
                        'for i, a in enumerate(args[:-1]):\n    if args[:1] == ["add-generic-password"] and a in ("-w", "-X"):\n'
                        '        safe[i + 1] = "<redacted>"\n'
                        'open(%r, "a").write(json.dumps(safe) + "\\n")\n'
                        'sys.exit(44 if args[:1] == ["find-generic-password"] else 1)\n' % (self.ctx.python, str(log)))
        shim.chmod(0o700)

    # -- accounts --
    def claude_account(self, label='A', identity=IDENT_A, expires_in_ms=3600 * 1000 * 24, five=20, week=30):
        access, refresh = FX.claude_token(label), FX.claude_token(label + 'R')
        self.labels[label] = access
        self.tokens[label] = (access, refresh)
        self.upstream.labels[access] = label
        self.usage.add(label, access, identity, five, week)
        return access, refresh, int(time.time() * 1000) + expires_in_ms

    def claude_setup(self, expires_in_ms=3600 * 1000 * 24):
        access, refresh, expires = self.claude_account('A', expires_in_ms=expires_in_ms)
        FX.claude_store(self.cfg, access, refresh, expires)
        FX.claude_metadata(self.cfg, IDENT_A, [self.workspace])

    def codex_account(self, label='A', who=CODEX_A, lifetime=3600, five=20, weekly=30):
        access, refresh = self.upstream.add_codex(label, who[0], who[1], five, weekly, lifetime)
        self.labels[label] = access
        self.tokens[label] = (access, refresh)
        return access, refresh

    def codex_setup(self, lifetime=3600):
        access, refresh = self.codex_account('A', lifetime=lifetime)
        FX.codex_auth(self.codex, access, refresh, CODEX_A[0], CODEX_A[1])

    def setup(self, **kw):
        (self.claude_setup if self.provider == 'claude' else self.codex_setup)(**kw)

    def set_usage(self, five=None, week=None, identity=None):
        if self.provider == 'claude':
            self.usage.set(identity or IDENT_A, five, week)
        else:
            self.upstream.set_usage((identity or CODEX_A)[0], five, week)

    def store_label(self):
        try:
            if self.provider == 'claude':
                token = json.loads((self.cfg / '.credentials.json').read_text())['claudeAiOauth']['accessToken']
            else:
                token = json.loads((self.codex / 'auth.json').read_text())['tokens']['access_token']
        except (OSError, ValueError, KeyError):
            return None
        return next((label for label, value in self.labels.items() if value == token), 'unlabelled')

    def policy(self, five=30, weekly=None, max_age=8.0, family=None):
        return {'family': family or ('claude-subscription-general' if self.provider == 'claude' else 'codex'),
                'reserves': {'five_hour': five, 'weekly': weekly}, 'max_age_s': max_age}

    def invocation(self, name='main', policy=None, **opts):
        inv = Invocation(self, name, self.provider, policy or self.policy(), **opts)
        self.invocations.append(inv)
        return inv

    def child_spec(self, inv, name, **extra):
        spec = {'provider': self.provider, 'gate_port': inv.port(), 'config_dir': str(self.cfg),
                'codex_home': str(self.codex), 'model': CLAUDE_MODELS[0] if self.provider == 'claude' else 'gpt-5.4',
                'upstream_port': self.upstream.port, 'direct_ports': [self.upstream.port, self.usage.port],
                'port_roles': {str(self.upstream.port): 'upstream', str(self.usage.port): 'usage'},
                'tool_file': str(self.workspace / ('%s-tool.txt' % name))}
        spec.update(extra)
        path = self.dir / ('child-%s.json' % name)
        path.write_text(json.dumps(spec))
        return path

    def child_cmd(self, spec, action, out_name):
        q = lambda s: "'" + str(s) + "'"  # noqa: E731 - fixture paths without quotes
        return '%s -I -B %s %s %s %s' % (q(self.ctx.python), q(HERE / 'child.py'), q(spec), action,
                                         q(self.out / (out_name + '.jsonl')))

    def child_rows(self, out_name):
        return jsonl(self.out / (out_name + '.jsonl'))

    def run_confined(self, inv, argv, timeout=30, env=None):
        """A process under the invocation's own rendered profile (a confined descendant stand-in)."""
        profile = (inv.dir / 'profile.sb').read_text()
        return self.ctx.run([SANDBOX_EXEC, '-p', profile] + list(argv), timeout=timeout,
                            env=env or {'PATH': '/usr/bin:/bin', 'HOME': str(self.home), 'TMPDIR': str(self.tmp)})

    def script(self, marker, *actions):
        self.upstream.script.setdefault(marker, []).extend(actions)

    def model_after(self, t):
        return [r for r in self.upstream.after(t) if r.get('method') == 'POST']

    def close(self):
        for inv in self.invocations:
            inv.close()
        for server in self.servers:
            try:
                server.close()
            except Exception:
                pass


class Ctx:
    def __init__(self, root, stage, tools):
        self.root = Path(os.path.realpath(root))
        self.stage, self.tools = stage, tools
        self.python = os.path.realpath(sys.executable)
        self.groups = []
        self.real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.data = {'stage': stage, 'started': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                     'host': {'macos': subprocess.run(['/usr/bin/sw_vers', '-productVersion'], capture_output=True,
                                                      text=True).stdout.strip(), 'arch': os.uname().machine},
                     'cases': {}}
        for name in ('out', 'cases', 'anchors'):
            (self.root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.redact = {}

    def tag(self):
        return 'qkm-' + secrets.token_hex(6)

    def run(self, argv, timeout=20, env=None, cwd=None):
        began = now()
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        self.groups.append(proc.pid)
        try:
            out, err = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            self.kill(proc.pid)
            out, err = proc.communicate()
            timed_out = True
        return {'code': proc.returncode, 'timed_out': timed_out, 'seconds': round(now() - began, 2),
                'stdout': out.decode(errors='replace')[-3000:], 'stderr': err.decode(errors='replace')[-1500:]}

    def kill(self, pgid):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(.3)

    def leftovers(self):
        result = subprocess.run(['/bin/ps', '-axo', 'pid=,command='], capture_output=True, text=True)
        mine, me = [], os.getpid()
        for line in result.stdout.splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and str(self.root) in parts[1] and int(parts[0]) != me \
                    and 'mac_probe.py' not in parts[1]:
                mine.append({'pid': int(parts[0]), 'command': parts[1][:160]})
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
        time.sleep(.5)
        self.data['leftover_processes_killed'] = found
        self.data['leftover_processes_after'] = self.leftovers()

    def save(self):
        self.data['finished'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        path = self.root / 'out' / (self.stage + '.json')
        text = json.dumps(self.data, indent=1, sort_keys=True, default=str)
        for old, new in sorted(self.redact.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(old, new)
        path.write_text(text + '\n')


# -- common sequences ---------------------------------------------------------------------
def start(case, inv, first_marker, wait_reply=True):
    """Launch; wait for the native and its first admitted turn."""
    inv.launch()
    started = inv.wait_launcher('native_started', 70)
    result = {'native_started': started, 'refused': [r.get('reason') for r in inv.launcher('refused')]}
    if started and wait_reply:
        result['first_turn_forwarded'] = FX.wait_for(
            lambda: any(r.get('marker') == first_marker for r in case.upstream.snapshot()), 60)
        time.sleep(2)
    return result


def no_model_after(case, t):
    rows = case.model_after(t) if t else None
    return {'latch_t': t, 'model_posts_after_latch': None if rows is None else len(rows)}


def hold_turn(case, inv, marker, seconds=30):
    case.script(marker, ('hold',))
    typed = inv.turn('Please answer %s' % marker)
    held = FX.wait_for(lambda: marker in case.upstream.held, seconds)
    return typed, held


def closed_after(inv, latch):
    """Stream closures and their delay after the latch."""
    rows = [r for r in inv.gate('stream_closed') if latch and r['t'] >= latch]
    return [round((r['t'] - latch) * 1000, 1) for r in rows]


def later_refused(case, inv, marker):
    typed = inv.turn('Later turn %s' % marker)
    FX.wait_for(lambda: inv.decisions(typed), 25)
    time.sleep(1.5)
    rows = inv.decisions(typed)
    return {'decisions_after': [(r.get('ok'), r.get('reason')) for r in rows],
            'upstream_marker_hits': sum(1 for r in case.upstream.snapshot() if r.get('marker') == marker)}


# -- cases --------------------------------------------------------------------------------
def case_threshold(case):
    """K1 + K2: a held SSE stream, an SDK child and its detached grandchild across a threshold latch."""
    case.setup()
    inv = case.invocation(prompt='KM-thr-first start')
    case.script('KM-thr-first', ('tool', 'CHILD_PLACEHOLDER'))
    inv.launch()
    if not inv.wait_launcher('native_started', 70):
        return {'native_started': False, 'summary': inv.summary()}
    spec = case.child_spec(inv, 'thr', duration=40, interval=0.4)
    command = '; '.join([case.child_cmd(spec, 'direct', 'direct'), case.child_cmd(spec, 'bind', 'bind'),
                         case.child_cmd(spec, 'detach', 'sdk'), 'echo km-tool-done'])
    case.upstream.script['KM-thr-first'] = [('tool', command)]
    result = {'native_started': True}
    result['tool_ran'] = FX.wait_for(lambda: len(case.child_rows('sdk')) >= 4, 60)
    typed, result['stream_held'] = hold_turn(case, inv, 'KM-thr-hold')
    time.sleep(1)
    crossed = now()
    case.set_usage(five=80)
    latch = inv.wait_latch(15, after=crossed)
    result.update(no_model_after(case, latch))
    time.sleep(3)
    result['crossed_t'] = crossed
    result['latch_after_cross_ms'] = round((latch - crossed) * 1000, 1) if latch else None
    result['stream_close_ms'] = closed_after(inv, latch)
    result['hold_end'] = [r for r in case.upstream.snapshot() if r.get('event') == 'hold_end']
    result['local_command'] = inv.local_command()
    result['later'] = later_refused(case, inv, 'KM-thr-later')
    time.sleep(2)
    result['sdk_rows'] = case.child_rows('sdk')
    result['direct_rows'] = case.child_rows('direct')
    result['bind_rows'] = case.child_rows('bind')
    result['model_posts_after_latch_final'] = len(case.model_after(latch)) if latch else None
    result['native_alive'] = inv.native_alive()
    result['exit'] = inv.exit()
    result['upstream'] = case.upstream.snapshot()
    result['summary'] = inv.summary()
    return result


def loss_case(case, mode):
    case.setup()
    inv = case.invocation(prompt='KM-loss-first start')
    result = start(case, inv, 'KM-loss-first')
    if not result['native_started']:
        result['summary'] = inv.summary()
        return result
    typed, result['stream_held'] = hold_turn(case, inv, 'KM-loss-hold')
    time.sleep(1)
    pid = inv.pid('monitor_started')
    trigger = now()
    os.kill(pid, {'kill': signal.SIGKILL, 'eof': signal.SIGUSR1, 'stall': signal.SIGUSR2}[mode])
    latch = inv.wait_latch(15, after=trigger)
    result['trigger_t'] = trigger
    result['latch_after_trigger_ms'] = round((latch - trigger) * 1000, 1) if latch else None
    result['lost'] = [r.get('reason') for r in inv.gate('lost')]
    result.update(no_model_after(case, latch))
    time.sleep(2)
    result['stream_close_ms'] = closed_after(inv, latch)
    result['local_command'] = inv.local_command()
    result['later'] = later_refused(case, inv, 'KM-loss-later')
    result['model_posts_after_latch_final'] = len(case.model_after(latch)) if latch else None
    result['native_alive'] = inv.native_alive()
    result['exit'] = inv.exit()
    result['upstream'] = case.upstream.snapshot()
    result['summary'] = inv.summary()
    return result


def squat_matrix(port, host4):
    """Outside binds of the gate port while the holder is in place (same uid, no privilege)."""
    out = {}
    trials = [('lo4_plain', socket.AF_INET, '127.0.0.1', False, False),
              ('lo4_reuseaddr', socket.AF_INET, '127.0.0.1', True, False),
              ('lo4_reuseport', socket.AF_INET, '127.0.0.1', False, True),
              ('lo6_reuseaddr', socket.AF_INET6, '::1', True, False),
              ('any4_plain', socket.AF_INET, '0.0.0.0', False, False),
              ('any4_reuseaddr', socket.AF_INET, '0.0.0.0', True, False),
              ('any6_reuseaddr', socket.AF_INET6, '::', True, False)]
    if host4:
        trials += [('host4_plain', socket.AF_INET, host4, False, False),
                   ('host4_reuseaddr', socket.AF_INET, host4, True, False),
                   ('host4_reuseport', socket.AF_INET, host4, True, True)]
    for name, fam, address, reuseaddr, reuseport in trials:
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            if fam == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            if reuseaddr:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if reuseport:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            s.bind((address, port))
            s.listen(4)
            out[name] = 'bound'
        except OSError as error:
            out[name] = 'errno:%s' % error.errno
        finally:
            s.close()
    return out


def host_ipv4():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('192.0.2.1', 9))  # UDP connect sends nothing
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def case_gate_death(case):
    """K4: kill the gate; boundary still denies; holder keeps the port; restart starts paused."""
    case.setup()
    inv = case.invocation(prompt='KM-gd-first start', restart_gate=True, restart_delay_s=10)
    result = start(case, inv, 'KM-gd-first')
    if not result['native_started']:
        result['summary'] = inv.summary()
        return result
    port = inv.port()
    gate_pid = inv.pid('gate_started')
    killed = now()
    os.kill(gate_pid, signal.SIGKILL)
    result['killed_t'] = killed
    time.sleep(0.5)
    queued = inv.turn('Queued turn KM-gd-queued')
    spec = case.child_spec(inv, 'gd', duration=4, interval=0.5)
    result['confined_bind'] = case.run_confined(inv, [case.ctx.python, '-I', '-B', str(HERE / 'child.py'), str(spec),
                                                      'bind', str(case.out / 'gd-bind.jsonl')])
    result['confined_direct'] = case.run_confined(inv, [case.ctx.python, '-I', '-B', str(HERE / 'child.py'),
                                                        str(spec), 'direct', str(case.out / 'gd-direct.jsonl')])
    result['bind_rows'] = case.child_rows('gd-bind')
    result['direct_rows'] = case.child_rows('gd-direct')
    address = host_ipv4()
    if address:
        case.ctx.redact[address] = '<host-ipv4>'
    result['squat_during_death'] = squat_matrix(port, address)
    result['restart_seen'] = inv.wait_launcher('gate_restarted', 30)
    restarted = inv.launcher('gate_restarted')
    restart_t = restarted[0]['t'] if restarted else None
    result['restart_t'] = restart_t
    time.sleep(3)
    result['model_posts_between_kill_and_continuation'] = None
    result['queued_turn_decisions'] = [(r.get('ok'), r.get('reason')) for r in inv.decisions(killed)]
    sdk = case.run_confined(inv, [case.ctx.python, '-I', '-B', str(HERE / 'child.py'), str(spec), 'sdk',
                                  str(case.out / 'gd-sdk.jsonl')], timeout=20)
    result['sdk_after_restart'] = case.child_rows('gd-sdk')
    result['sdk_exit'] = sdk['code']
    result['squat_after_restart'] = squat_matrix(port, address)
    before_continue = now()
    result['model_posts_after_kill'] = len(case.model_after(killed))
    result['continuation'] = continuation(case, inv, expect=True)
    result['after_continue'] = later_turn(case, inv, 'KM-gd-resumed', expect_ok=True)
    result['model_posts_after_kill_before_continue'] = len([r for r in case.model_after(killed)
                                                            if r['t'] < before_continue])
    result['native_alive'] = inv.native_alive()
    result['exit'] = inv.exit()
    result['upstream'] = case.upstream.snapshot()
    result['summary'] = inv.summary()
    return result


def read_challenge(inv, since, seconds=10):
    found = []
    FX.wait_for(lambda: bool(CHALLENGE.findall(FX_strip(inv.term.text(since)))), seconds)
    found = CHALLENGE.findall(FX_strip(inv.term.text(since)))
    return found[-1] if found else None


def continuation(case, inv, expect, answer=None):
    """F12 -> fresh challenge on the trusted terminal -> type it -> fresh check."""
    mark = inv.term.mark()
    inv.term.write(F12)
    challenge = read_challenge(inv, mark)
    if challenge is None:
        return {'challenge': None}
    began = now()
    inv.term.write((answer or challenge).encode())
    time.sleep(0.3)
    inv.term.write(b'\r')
    FX.wait_for(lambda: 'Fresh quota check passed' in FX_strip(inv.term.text(mark)) or
                'Still paused' in FX_strip(inv.term.text(mark)), 30)
    text = FX_strip(inv.term.text(mark))
    return {'challenge_shown': True, 'granted': 'Fresh quota check passed' in text,
            'still_paused': 'Still paused' in text, 'expected': expect,
            'check_ms': round((now() - began) * 1000, 1),
            'messages': re.findall(r'\[coding-agent-kit\] ([^\[]{0,160})', text)[-4:]}


def later_turn(case, inv, marker, expect_ok):
    typed = inv.turn('Turn %s' % marker)
    FX.wait_for(lambda: inv.decisions(typed), 25)
    time.sleep(1.5)
    return {'decisions': [(r.get('ok'), r.get('reason'), r.get('label')) for r in inv.decisions(typed)],
            'forwarded': sum(1 for r in case.upstream.snapshot() if r.get('marker') == marker),
            'expect_ok': expect_ok}


def case_continuation(case):
    """K5: only a fresh typed confirmation on the trusted terminal plus a fresh check reopens."""
    case.setup()
    inv = case.invocation(prompt='KM-cont-first start')
    result = start(case, inv, 'KM-cont-first')
    if not result['native_started']:
        result['summary'] = inv.summary()
        return result
    pids = {'native': inv.pid('native_started'), 'gate': inv.pid('gate_started')}
    case.set_usage(five=80)
    latch = inv.wait_latch(15)
    result['latch_t'] = latch
    time.sleep(1)
    steps = {}
    # typed text and Enter without F12 go to the native, never to the control
    mark = inv.term.mark()
    inv.term.write(b'deadbeef')
    time.sleep(0.5)
    inv.term.write(b'\r')
    time.sleep(2)
    steps['typed_without_f12'] = {'challenge': bool(CHALLENGE.findall(FX_strip(inv.term.text(mark)))),
                                  'granted': bool(inv.launcher('granted'))}
    # a stale answer: the challenge shown first is replaced by a new F12 before it is typed
    mark = inv.term.mark()
    inv.term.write(F12)
    old = read_challenge(inv, mark)
    inv.term.write(b'\x03')  # cancels the pending challenge
    time.sleep(0.5)
    steps['stale_answer'] = continuation(case, inv, expect=False, answer=old or '00000000')
    # bracketed paste carrying the current challenge is never an answer
    mark = inv.term.mark()
    inv.term.write(F12)
    current = read_challenge(inv, mark)
    inv.term.write(b'\x1b[200~' + (current or '').encode() + b'\r\x1b[201~')
    time.sleep(2)
    steps['paste'] = {'challenge': current is not None, 'granted': bool(inv.launcher('granted')),
                      'status': status_of(inv)}
    inv.term.write(b'\x03')
    # background wakeup: the quota recovers and the monitor keeps polling; nothing reopens
    case.set_usage(five=20)
    time.sleep(4)
    steps['background_wakeup'] = {'status': status_of(inv), 'granted': bool(inv.launcher('granted')),
                                  'polls_during_pause': len([r for r in inv.gate('poll') if r['t'] > latch])}
    # native output after the pause (a local command) cannot answer either
    steps['native_output'] = inv.local_command()
    steps['native_output']['granted'] = bool(inv.launcher('granted'))
    # a failing fresh check stays paused with the deterministic message
    case.set_usage(five=85)
    steps['failing_check'] = continuation(case, inv, expect=False)
    steps['failing_status'] = status_of(inv)
    # the passing fresh check reopens the same invocation
    case.set_usage(five=20)
    steps['passing_check'] = continuation(case, inv, expect=True)
    steps['after'] = later_turn(case, inv, 'KM-cont-resumed', expect_ok=True)
    steps['same_invocation'] = {'native': inv.pid('native_started') == pids['native'] and inv.native_alive(),
                                'gate': inv.pid('gate_started') == pids['gate']}
    result['steps'] = steps
    result['model_posts_paused'] = [r for r in case.model_after(latch)
                                    if r['t'] < (inv.launcher('granted') or [{'t': 1e18}])[0]['t']] if latch else None
    result['exit'] = inv.exit()
    result['upstream'] = case.upstream.snapshot()
    result['summary'] = inv.summary()
    return result


def status_of(inv):
    rows = [r for r in inv.gate('control') if str(r.get('op')) == 'reply:status']
    paused = [r for r in inv.gate('controller', 'latched')]
    resumed = [r for r in inv.gate('notify') if r.get('op') == 'resumed']
    return {'latched': len(paused), 'resumed': len(resumed), 'last_status': rows[-1] if rows else None}


def case_independent(case):
    """K6: a second protected invocation of the same account with a smaller reserve keeps working."""
    case.setup()
    first = case.invocation('first', case.policy(five=30), prompt='KM-ind1-first start')
    second = case.invocation('second', case.policy(five=5), prompt='KM-ind2-first start')
    result = {'first': start(case, first, 'KM-ind1-first'), 'second': start(case, second, 'KM-ind2-first')}
    if not (result['first']['native_started'] and result['second']['native_started']):
        result['summary'] = {'first': first.summary(), 'second': second.summary()}
        return result
    crossed = now()
    case.set_usage(five=80)  # 20% left: below 30, above 5
    latch1 = first.wait_latch(15, after=crossed)
    time.sleep(2)
    result['first_latch_t'] = latch1
    result['second_latched_at_80'] = second.latch_t(crossed)
    result['second_turn'] = later_turn(case, second, 'KM-ind2-works', expect_ok=True)
    result['first_turn'] = later_turn(case, first, 'KM-ind1-refused', expect_ok=False)
    outside = now()
    case.set_usage(five=96)  # consumption outside both invocations
    result['second_latch_t'] = second.wait_latch(15, after=outside)
    result['outside_t'] = outside
    result['second_after'] = later_turn(case, second, 'KM-ind2-refused', expect_ok=False)
    result['exit'] = {'first': first.exit(), 'second': second.exit()}
    result['upstream'] = case.upstream.snapshot()
    result['summary'] = {'first': first.summary(), 'second': second.summary()}
    return result


def refresh_fixture(case):
    ca, leaf, key = FX.make_ca(case.ctx.root / 'tls')
    server = FX.RefreshServer(leaf, key)
    case.servers.append(server)
    return ca, server


def case_rotation(case):
    """K7 + K8a: native refresh A->A2 through the exact-host tunnel, then A->B with a queued child."""
    ca, refresh = refresh_fixture(case)
    if case.provider == 'claude':
        access, rtoken, _ = case.claude_account('A')
        FX.claude_store(case.cfg, access, rtoken, int(time.time() * 1000) + 150000)
        FX.claude_metadata(case.cfg, IDENT_A, [case.workspace])
        a2, r2, _ = case.claude_account('A2')
        b, rb, _ = case.claude_account('B', IDENT_B, five=5)
        refresh.grants[rtoken] = lambda: {'access_token': a2, 'refresh_token': r2, 'expires_in': 3600,
                                          'scope': 'user:inference user:profile user:sessions:claude_code',
                                          'account': {'uuid': IDENT_A[0], 'email_address': 'km@example.invalid'},
                                          'organization': {'uuid': IDENT_A[1]}}
        allow = {'platform.claude.com:443': refresh.port}
    else:
        access, rtoken = case.codex_account('A', lifetime=-120)
        FX.codex_auth(case.codex, access, rtoken, CODEX_A[0], CODEX_A[1])
        a2, r2 = case.codex_account('A2')
        b, rb = case.codex_account('B', CODEX_B, five=5)
        refresh.grants[rtoken] = lambda: {'access_token': a2, 'refresh_token': r2, 'expires_in': 3600,
                                          'token_type': 'Bearer',
                                          'id_token': FX.QXFIX.token(CODEX_A[0], CODEX_A[1])}
        allow = {'auth.openai.com:443': refresh.port}
    inv = case.invocation(prompt='KM-rot-first start', connect_allow=allow, ca=ca, budget=8)
    case.script('KM-rot-first', ('tool', 'echo placeholder'))
    inv.launch()
    if not inv.wait_launcher('native_started', 70):
        return {'native_started': False, 'summary': inv.summary()}
    spec = case.child_spec(inv, 'rot', duration=45, interval=1.0)
    case.upstream.script['KM-rot-first'] = [('tool', case.child_cmd(spec, 'detach', 'sdk') + '; echo km-tool-done')]
    result = {'native_started': True}
    result['refreshed'] = FX.wait_for(lambda: bool(inv.decisions(label='A2', ok=True)), 60)
    result['store_after_refresh'] = case.store_label()
    FX.wait_for(lambda: len(case.child_rows('sdk')) >= 4, 40)
    connects = case.child_spec(inv, 'rot-connect', connect_targets=list(allow) + ['api.anthropic.com:443',
                                                                                 'chatgpt.com:443'], phase='open')
    result['connect_open'] = case.run_confined(inv, [case.ctx.python, '-I', '-B', str(HERE / 'child.py'),
                                                     str(connects), 'connect', str(case.out / 'connect.jsonl')])
    swap = now()
    if case.provider == 'claude':
        FX.claude_store(case.cfg, b, rb, int(time.time() * 1000) + 3600000)
    else:
        FX.codex_auth(case.codex, b, rb, CODEX_B[0], CODEX_B[1])
    result['swap_t'] = swap
    latch = inv.wait_latch(20, after=swap)
    result['latch_t'] = latch
    time.sleep(2)
    connects = case.child_spec(inv, 'rot-connect2', connect_targets=list(allow), phase='paused')
    case.run_confined(inv, [case.ctx.python, '-I', '-B', str(HERE / 'child.py'), str(connects), 'connect',
                            str(case.out / 'connect.jsonl')])
    result['connect_rows'] = case.child_rows('connect')
    result['later'] = later_refused(case, inv, 'KM-rot-later')
    result['sdk_rows'] = case.child_rows('sdk')
    result['refresh_events'] = refresh.events
    result['usage_labels'] = case.usage.labels() if case.provider == 'claude' else None
    result['profile_labels'] = case.usage.labels('/api/oauth/profile') if case.provider == 'claude' else None
    result['security_argv'] = jsonl(case.out / 'security-argv.jsonl')
    result['model_posts_after_latch'] = len(case.model_after(latch)) if latch else None
    result['native_alive'] = inv.native_alive()
    result['exit'] = inv.exit()
    result['upstream'] = case.upstream.snapshot()
    result['summary'] = inv.summary()
    return result


def case_norefresh(case):
    """K8b: no refresh path; token expiry leads to a stale pause."""
    life = 45
    if case.provider == 'claude':
        case.claude_setup(expires_in_ms=life * 1000)
    else:
        case.codex_setup(lifetime=life)
    inv = case.invocation(case.policy(max_age=6.0), prompt='KM-nr-first start')
    result = start(case, inv, 'KM-nr-first')
    if not result['native_started']:
        result['summary'] = inv.summary()
        return result
    expiry = inv.started + life
    FX.wait_for(lambda: now() >= expiry, life + 5)
    revoked = now()
    if case.provider == 'claude':
        case.usage.revoke(case.tokens['A'][0])
    else:
        with case.upstream.lock:
            case.upstream.codex[case.tokens['A'][0]]['revoked'] = True
    result['revoked_t'] = revoked
    latch = inv.wait_latch(20, after=revoked)
    result['latch_t'] = latch
    result['latch_after_expiry_s'] = round(latch - revoked, 2) if latch else None
    result['connects'] = [(r.get('target'), r.get('ok')) for r in inv.gate('connect')]
    result['later'] = later_refused(case, inv, 'KM-nr-later')
    result['model_posts_after_latch'] = len(case.model_after(latch)) if latch else None
    result['native_alive'] = inv.native_alive()
    result['exit'] = inv.exit()
    result['upstream'] = case.upstream.snapshot()
    result['summary'] = inv.summary()
    return result


class TunnelProxy:
    """Prelaunch stand-in: a CONNECT proxy for exactly the refresh host; everything else 403."""

    def __init__(self, allow):
        self.allow, self.rows = allow, []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        try:
            conn.settimeout(10)
            head = b''
            while b'\r\n\r\n' not in head and len(head) < 8192:
                chunk = conn.recv(1)
                if not chunk:
                    return
                head += chunk
            parts = head.split(b' ', 2)
            target = parts[1].decode('latin-1') if len(parts) > 1 else ''
            port = self.allow.get(target) if parts[0] == b'CONNECT' else None
            self.rows.append({'method': parts[0].decode('latin-1'), 'target': target[:80], 'ok': port is not None})
            if port is None:
                conn.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                return
            up = socket.create_connection(('127.0.0.1', port), timeout=5)
            conn.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
            conn.settimeout(None)
            up.settimeout(None)
            done = threading.Event()

            def pump(a, b):
                try:
                    while True:
                        data = a.recv(65536)
                        if not data:
                            break
                        b.sendall(data)
                except OSError:
                    pass
                done.set()
            threading.Thread(target=pump, args=(conn, up), daemon=True).start()
            pump(up, conn)
            up.close()
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


def case_prelaunch(case):
    """Refresh option (a): can a model-free native command refresh before launch, through only the refresh host?"""
    ca, refresh = refresh_fixture(case)
    runs = []
    host = 'platform.claude.com:443' if case.provider == 'claude' else 'auth.openai.com:443'
    proxy = TunnelProxy({host: refresh.port})
    case.servers.append(proxy)
    for variant, offset in (('near_expiry', 150), ('expired', -60)):
        if case.provider == 'claude':
            access, rtoken, _ = case.claude_account('P' + variant)
            FX.claude_store(case.cfg, access, rtoken, int((time.time() + offset) * 1000))
            FX.claude_metadata(case.cfg, IDENT_A, [case.workspace])
            new, newr, _ = case.claude_account('P' + variant + '2')
            refresh.grants[rtoken] = lambda new=new, newr=newr: {
                'access_token': new, 'refresh_token': newr, 'expires_in': 3600,
                'scope': 'user:inference user:profile user:sessions:claude_code',
                'account': {'uuid': IDENT_A[0], 'email_address': 'km@example.invalid'},
                'organization': {'uuid': IDENT_A[1]}}
            argv = [case.ctx.tools['claude'], 'auth', 'status']
            env = {'PATH': str(case.shim) + ':/usr/bin:/bin', 'HOME': str(case.home), 'TMPDIR': str(case.tmp),
                   'CLAUDE_CONFIG_DIR': str(case.cfg), 'DISABLE_AUTOUPDATER': '1', 'NODE_EXTRA_CA_CERTS': str(ca)}
        else:
            access, rtoken = case.codex_account('P' + variant, lifetime=offset)
            FX.codex_auth(case.codex, access, rtoken, CODEX_A[0], CODEX_A[1])
            new, newr = case.codex_account('P' + variant + '2')
            refresh.grants[rtoken] = lambda new=new, newr=newr: {
                'access_token': new, 'refresh_token': newr, 'expires_in': 3600, 'token_type': 'Bearer',
                'id_token': FX.QXFIX.token(CODEX_A[0], CODEX_A[1])}
            FX.codex_config(case.codex, 'http://127.0.0.1:9', str(case.workspace))
            argv = [case.ctx.tools['codex'], 'login', 'status']
            env = {'PATH': '/usr/bin:/bin', 'HOME': str(case.home), 'TMPDIR': str(case.tmp),
                   'CODEX_HOME': str(case.codex), 'CODEX_CA_CERTIFICATE': str(ca)}
        env.update(HTTPS_PROXY='http://127.0.0.1:%d' % proxy.port, NO_PROXY='127.0.0.1,localhost')
        tag = case.ctx.tag()
        sbpl = _load('km_nm_sbpl', HERE.parent / 'nm' / 'sbpl.py')
        profile = sbpl.render(LAUNCHER.profile_policy(tag, proxy.port, [str(case.ctx.root / 'anchors')]))
        before = len(refresh.events)
        done = case.ctx.run([SANDBOX_EXEC, '-p', profile] + argv, timeout=40, env=env, cwd=str(case.workspace))
        runs.append({'variant': variant, 'exit': done['code'], 'timed_out': done['timed_out'],
                     'seconds': done['seconds'], 'stdout_tail': done['stdout'][-300:],
                     'refresh_requests': refresh.events[before:], 'store_label_after': case.store_label()})
    return {'runs': runs, 'tunnel_rows': proxy.rows}


def case_tools(case):
    """K9 + K10: running tools through a pause; git and pip workflows during running and after pause."""
    case.setup()
    inv = case.invocation(prompt='KM-tools-first start')
    case.script('KM-tools-first', ('tool', 'echo placeholder'))
    inv.launch()
    if not inv.wait_launcher('native_started', 70):
        return {'native_started': False, 'summary': inv.summary()}
    spec = case.child_spec(inv, 'tools', duration=60)
    loop = case.workspace / 'fg-loop.txt'
    case.upstream.script['KM-tools-first'] = [('tool', case.child_cmd(spec, 'tools', 'tools') + '; echo km-bg-started')]
    result = {'native_started': True}
    result['bg_started'] = FX.wait_for(lambda: (case.workspace / 'tools-tool.txt').exists(), 60)
    fixtures = WorkflowFixtures(case)
    result['workflow_running'] = fixtures.run(inv, 'running')
    fg = 'for i in $(seq 1 25); do echo $i >> %s; sleep 1; done; echo km-fg-done' % loop
    case.script('KM-tools-fg', ('tool', fg))
    inv.turn('Run KM-tools-fg now')
    FX.wait_for(lambda: loop.exists() and len(loop.read_text().split()) >= 3, 40)
    crossed = now()
    case.set_usage(five=80)
    latch = inv.wait_latch(15, after=crossed)
    result['latch_t'] = latch
    lines_at_latch = len(loop.read_text().split()) if loop.exists() else 0
    bg_at_latch = len((case.workspace / 'tools-tool.txt').read_text().split('\n'))
    time.sleep(3)
    interrupt = now()
    inv.term.write(b'\x1b' if case.provider == 'claude' else b'\x1b')
    time.sleep(1)
    lines_after_esc = len(loop.read_text().split()) if loop.exists() else 0
    time.sleep(5)
    result['fg_loop'] = {'at_latch': lines_at_latch, 'one_s_after_esc': lines_after_esc,
                         'five_s_later': len(loop.read_text().split()) if loop.exists() else 0,
                         'interrupt_t': interrupt}
    result['bg_file'] = {'at_latch': bg_at_latch,
                         'after': len((case.workspace / 'tools-tool.txt').read_text().split('\n'))}
    result['tool_net_rows'] = case.child_rows('tools')
    result['workflow_paused'] = fixtures.run(inv, 'paused')
    result['decisions_after_latch'] = [(r.get('ok'), r.get('reason')) for r in inv.decisions(latch or 0)]
    result['model_posts_after_latch'] = len(case.model_after(latch)) if latch else None
    result['native_alive'] = inv.native_alive()
    result['exit'] = inv.exit()
    time.sleep(1)
    result['bg_after_native_exit'] = len((case.workspace / 'tools-tool.txt').read_text().split('\n'))
    fixtures.close()
    result['upstream'] = case.upstream.snapshot()
    result['summary'] = inv.summary()
    return result


class WorkflowFixtures:
    """A dumb-HTTP git repository and a PEP 503 simple index on loopback (outside the boundary)."""

    def __init__(self, case):
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        import functools
        self.case = case
        base = case.dir / 'wf-served'
        repo = base / 'repo.git'
        work = case.dir / 'wf-src'
        work.mkdir(exist_ok=True)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(case.home), 'GIT_CONFIG_NOSYSTEM': '1'}
        (work / 'README').write_text('km fixture\n')
        for argv in (['git', 'init', '-q', '-b', 'main', str(work)],
                     ['git', '-C', str(work), '-c', 'user.name=km', '-c', 'user.email=km@example.invalid', 'add', '.'],
                     ['git', '-C', str(work), '-c', 'user.name=km', '-c', 'user.email=km@example.invalid',
                      'commit', '-qm', 'fixture'],
                     ['git', 'clone', '-q', '--bare', str(work), str(repo)],
                     ['git', '-C', str(repo), 'update-server-info']):
            subprocess.run(argv, env=env, check=True, capture_output=True, timeout=30)
        simple = base / 'simple' / 'kmfixture'
        simple.mkdir(parents=True, exist_ok=True)
        wheel = 'kmfixture-1.0-py3-none-any.whl'
        import zipfile
        with zipfile.ZipFile(simple / wheel, 'w') as z:
            z.writestr('kmfixture/__init__.py', '')
            z.writestr('kmfixture-1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: kmfixture\nVersion: 1.0\n')
            z.writestr('kmfixture-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
            z.writestr('kmfixture-1.0.dist-info/RECORD', '')
        (simple / 'index.html').write_text('<a href="%s">%s</a>\n' % (wheel, wheel))
        handler = functools.partial(_Quiet, directory=str(base))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = env

    def run(self, inv, phase):
        case, out = self.case, {}
        dest = case.dir / ('wf-clone-' + phase)
        url = 'http://127.0.0.1:%d' % self.port
        env = dict(self.env, TMPDIR=str(case.tmp))
        out['git_confined'] = _brief(case.run_confined(inv, ['/usr/bin/git', 'clone', '-q', url + '/repo.git',
                                                             str(dest)], env=env))
        pip = [case.ctx.python, '-m', 'pip', 'download', '--no-deps', '--disable-pip-version-check', '--no-cache-dir',
               '--retries', '0', '--timeout', '3', '--index-url', url + '/simple', '-d',
               str(case.dir / ('wf-pip-' + phase)), 'kmfixture']
        out['pip_confined'] = _brief(case.run_confined(inv, pip, env=env, timeout=40))
        proxy = dict(env, HTTPS_PROXY='http://127.0.0.1:%d' % inv.port(), HTTP_PROXY='http://127.0.0.1:%d' % inv.port())
        out['pip_via_gate_proxy'] = _brief(case.run_confined(inv, pip[:-3] + [
            str(case.dir / ('wf-pip-proxy-' + phase)), 'kmfixture'], env=proxy, timeout=40))
        if phase == 'running':
            control = case.ctx.run(['/usr/bin/git', 'clone', '-q', url + '/repo.git', str(case.dir / 'wf-control')],
                                   env=env, timeout=30)
            out['git_control_outside'] = _brief(control)
            out['pip_control_outside'] = _brief(case.ctx.run(pip[:-3] + [str(case.dir / 'wf-pip-control'),
                                                                        'kmfixture'], env=env, timeout=40))
        return out

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _brief(result):
    return {'code': result['code'], 'timed_out': result['timed_out'],
            'stderr_tail': (result['stderr'] or result['stdout'])[-240:]}


class _QuietBase:
    pass


def _make_quiet():
    from http.server import SimpleHTTPRequestHandler

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    return Quiet


_Quiet = _make_quiet()


def case_sandbox(case):
    """Native tool sandbox inside the outer profile (Claude sandbox.enabled, Codex workspace-write)."""
    case.setup()
    inv = case.invocation(prompt='KM-nsb-first start', native_sandbox=True)
    marker = case.workspace / 'nsb-done.txt'
    case.script('KM-nsb-first', ('tool', 'echo km-nested > %s; /usr/bin/true' % marker))
    result = start(case, inv, 'KM-nsb-first')
    if result['native_started']:
        FX.wait_for(lambda: any(r.get('kind') == 'tool_result' for r in case.upstream.snapshot()), 45)
        time.sleep(1)
    result['tool_effect_file'] = marker.exists()
    result['tool_results'] = [r for r in case.upstream.snapshot() if r.get('kind') == 'tool_result']
    result['exit'] = inv.exit() if result['native_started'] else None
    result['summary'] = inv.summary()
    return result


def case_fsrules(case):
    """Outer write rules (workspace, account, home and temp writable; the rest read-only)."""
    case.setup()
    writable = [str(case.dir / p) for p in ('home', 'tmp', 'xdg', 'cache', 'state', 'data', 'workspace', 'accounts',
                                            'codex', 'out')]
    inv = case.invocation(prompt='KM-fs-first start', fs_rules=writable)
    outside = case.dir / 'outside' / 'fs-escape.txt'
    inside = case.workspace / 'fs-inside.txt'
    case.script('KM-fs-first', ('tool', 'echo in > %s; echo out > %s; echo rc=$?' % (inside, outside)))
    result = start(case, inv, 'KM-fs-first')
    if result['native_started']:
        FX.wait_for(lambda: any(r.get('kind') == 'tool_result' for r in case.upstream.snapshot()), 45)
        time.sleep(1)
        result['second_turn'] = later_turn(case, inv, 'KM-fs-second', expect_ok=True)
    result['inside_written'] = inside.exists()
    result['outside_written'] = outside.exists()
    result['tool_results'] = [r for r in case.upstream.snapshot() if r.get('kind') == 'tool_result']
    result['exit'] = inv.exit() if result['native_started'] else None
    result['summary'] = inv.summary()
    return result


def case_refusals(case):
    """K11: each startup refusal is clear and sends zero model requests."""
    rows = {}
    claude = case.provider == 'claude'
    variants = [
        ('old_version', {'version_override': '2.1.300 (Claude Code)' if claude else 'codex-cli 0.150.0'}, None),
        ('unsupported_mode', {'user_args': ['-p'] if claude else ['resume']}, None),
        ('conflicting_env', {'user_env': {'ANTHROPIC_API_KEY' if claude else 'OPENAI_API_KEY': 'x'}}, None),
        ('unsupported_family', {}, 'family'),
        ('missing_quota', {}, 'missing'),
        ('stale_quota', {}, 'stale'),
        ('malformed_quota', {}, 'malformed'),
        ('threshold_at_start', {}, 'threshold'),
        ('missing_sandbox_exec', {'sandbox_exec': '/private/var/empty/no-sandbox-exec'}, None),
        ('self_test_fails', {'profile_extra': '(allow network-outbound (remote tcp "localhost:*"))\n'}, None),
    ]
    for name, opts, quota in variants:
        sub = Case(case.ctx, case.name + '-' + name, case.provider)
        try:
            sub.setup()
            policy = sub.policy(family='claude-api-usage' if quota == 'family' and claude else
                                ('codex-credits' if quota == 'family' else None))
            if quota == 'missing':
                if claude:
                    sub.usage.revoke(sub.tokens['A'][0])
                else:
                    with sub.upstream.lock:
                        sub.upstream.codex[sub.tokens['A'][0]]['revoked'] = True
            elif quota == 'stale':
                if claude:
                    sub.usage.server.server_time = lambda: time.time() - 3600
                else:
                    with sub.upstream.lock:
                        sub.upstream.usage[CODEX_A[0]]['reset_offset'] = -20000
            elif quota == 'malformed':
                if claude:
                    sub.usage.accounts[IDENT_A].mode = 'malformed'
                else:
                    with sub.upstream.lock:
                        sub.upstream.usage[CODEX_A[0]]['mode'] = 'malformed'
            elif quota == 'threshold':
                sub.set_usage(five=90)
            inv = sub.invocation(policy=policy, prompt='KM-ref-first start', **opts)
            inv.launch()
            FX.wait_for(lambda: inv.proc.poll() is not None, 60)
            time.sleep(0.5)
            rows[name] = {'refused': [r.get('reason') for r in inv.launcher('refused')],
                          'native_started': bool(inv.launcher('native_started')),
                          'launcher_exit': inv.proc.poll(),
                          'model_posts': len([r for r in sub.upstream.snapshot() if r.get('method') == 'POST']),
                          'upstream_gets': sorted({r['path'] for r in sub.upstream.snapshot() if r.get('method') == 'GET'}),
                          'terminal': re.findall(r'\[coding-agent-kit\] ([^\[]{0,200})', FX_strip(inv.term.text()))[-2:],
                          'self_test': [r.get('result') for r in inv.launcher('self_test')],
                          'gate_started': bool(inv.launcher('gate_started'))}
        except Exception as error:  # record; an error is never a pass
            rows[name] = {'error': '%s: %s' % (type(error).__name__, error)}
        finally:
            sub.close()
    return {'variants': rows}


def case_portmatrix(case):
    """K4 port holder shapes without a native: which outside binds still succeed, and what a
    confined client reaches on the gate port at the host's interface address."""
    out = {}
    address = host_ipv4()
    if address:
        case.ctx.redact[address] = '<host-ipv4>'
    for mode in ('loopback', 'wildcard', 'all'):
        holder = LAUNCHER.Launcher.__new__(LAUNCHER.Launcher)
        holder.config = {'holder': mode}
        holder.emit = lambda *a, **k: None
        port = holder.hold()
        squats = squat_matrix(port, address)
        reach = {}
        squatter = None
        if address:
            try:
                squatter = socket.socket()
                squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                squatter.bind((address, port))
                squatter.listen(4)
                reach['host4_reuseaddr_squatter'] = 'bound'
            except OSError as error:
                reach['host4_reuseaddr_squatter'] = 'errno:%s' % error.errno
                squatter.close()
                squatter = None
        sbpl = _load('km_nm_sbpl', HERE.parent / 'nm' / 'sbpl.py')
        profile = sbpl.render(LAUNCHER.profile_policy(case.ctx.tag(), port, [str(case.ctx.root / 'anchors')]))
        targets = [['lo4', '127.0.0.1'], ['any4', '0.0.0.0']] + ([['host4', address]] if address else [])
        client = ('import json,socket,sys\nres={}\nfor n,h in json.loads(sys.argv[1]):\n    s=socket.socket();'
                  's.settimeout(2)\n    try:\n        s.connect((h,%d)); res[n]="connected"\n    except OSError as e:\n'
                  '        res[n]="errno:%%s"%%e.errno\n    s.close()\nprint(json.dumps(res))\n' % port)
        done = case.ctx.run([SANDBOX_EXEC, '-p', profile, case.ctx.python, '-I', '-B', '-c', client,
                             json.dumps(targets)], timeout=20)
        accepted = []
        if squatter is not None:
            squatter.settimeout(0.5)
            try:
                conn, _ = squatter.accept()
                accepted.append('host4')
                conn.close()
            except OSError:
                pass
            squatter.close()
        reach['confined_connects'] = done['stdout'].strip()[-300:]
        reach['squatter_accepted'] = accepted
        out[mode] = {'squats': squats, 'reach': reach, 'reserved': len(holder.reserved)}
        for s in holder.listeners + holder.reserved:
            s.close()
    out['host_ipv4_found'] = bool(address)
    return out


CASES = {'threshold': case_threshold, 'loss-kill': lambda c: loss_case(c, 'kill'),
         'loss-eof': lambda c: loss_case(c, 'eof'), 'loss-stall': lambda c: loss_case(c, 'stall'),
         'gate-death': case_gate_death, 'continuation': case_continuation, 'independent': case_independent,
         'rotation': case_rotation, 'norefresh': case_norefresh, 'prelaunch': case_prelaunch, 'tools': case_tools,
         'sandbox': case_sandbox, 'fsrules': case_fsrules, 'refusals': case_refusals, 'portmatrix': case_portmatrix}
STAGES = {
    'claude-loss': [('claude', c) for c in ('threshold', 'loss-kill', 'loss-eof', 'loss-stall')],
    'codex-loss': [('codex', c) for c in ('threshold', 'loss-kill', 'loss-eof', 'loss-stall')],
    'claude-resume': [('claude', c) for c in ('gate-death', 'continuation', 'independent')],
    'codex-resume': [('codex', c) for c in ('gate-death', 'continuation', 'independent')],
    'claude-refresh': [('claude', c) for c in ('rotation', 'norefresh', 'prelaunch')],
    'codex-refresh': [('codex', c) for c in ('rotation', 'norefresh', 'prelaunch')],
    'claude-tools': [('claude', c) for c in ('tools', 'sandbox', 'fsrules')],
    'codex-tools': [('codex', c) for c in ('tools', 'sandbox', 'fsrules')],
    'refusals': [('claude', 'refusals'), ('codex', 'refusals')],
    'portmatrix': [('none', 'portmatrix')],
}


class StageDeadline(BaseException):
    pass


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
    only = os.environ.get('KM_ONLY', '').split(',') if os.environ.get('KM_ONLY') else None
    try:
        import hashlib
        for name in ('claude', 'codex'):
            path = os.path.realpath(tools[name])
            ctx.data.setdefault('binaries', {})[name] = {
                'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                'version': subprocess.run([path, '--version'], capture_output=True, text=True, timeout=30,
                                          env={'PATH': '/usr/bin:/bin', 'HOME': str(ctx.root / 'cases')}).stdout.strip()}
        for provider, name in STAGES[stage]:
            if only and name not in only:
                continue
            key = provider + '-' + name
            case = Case(ctx, key, provider if provider != 'none' else 'claude')
            began = now()
            try:
                ctx.data['cases'][key] = CASES[name](case)
            except Exception as error:  # record and continue; an error is never a pass
                import traceback
                ctx.data['cases'][key] = {'error': '%s: %s' % (type(error).__name__, error),
                                          'trace': traceback.format_exc()[-1500:],
                                          'summaries': {inv.name: inv.summary() for inv in case.invocations}}
            finally:
                case.close()
                ctx.data['cases'][key]['case_seconds'] = round(now() - began, 1)
            ctx.save()
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
