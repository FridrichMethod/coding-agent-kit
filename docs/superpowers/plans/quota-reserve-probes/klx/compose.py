"""KLX composed protected invocations: real codex --no-daemon TUI, L2a boundary, gate, monitor.

Research harness only (not product runtime). This process plays the trusted launcher: it owns the
native's PTY and the trusted terminal control (PC5), holds the gate's listening socket for the
invocation's lifetime, starts the gate and the monitor as separate processes outside the
boundary, runs the model-free preflight through a fresh in-boundary native App Server, and only
then starts the TUI inside the NL L2a boundary with fabricated credentials. It also plays the
user (typing into the outer terminal) and the fake world (loopback backend). Evidence goes to
evidence/<stage>.json; run only through run.py.

    python3 -I -B compose.py BINARY STAGE
"""
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
import tempfile
import termios
import threading
import time
import tty

HERE = Path(__file__).resolve().parent
PROBES = HERE.parent
REPO = HERE.parents[4]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixtures = load('klx_fixtures', HERE / 'fixtures.py')
reader = load('klx_reader', HERE / 'reader.py')
launcher = load('klx_launcher', HERE / 'launcher.py')
control = load('klx_pc_control', PROBES / 'pc_continue' / 'control.py')
natives = load('klx_nl_natives', PROBES / 'nl' / 'natives.py')
A, UA, B, UB = 'fixture-account-a', 'fixture-user-a', 'fixture-account-b', 'fixture-user-b'
HELPER = ('import os,fcntl,termios,sys; os.setsid(); '
          'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')
ANSI = re.compile(r'\x1b(\[[0-9;?<>=]*[A-Za-z~]|\][^\x07\x1b]*(\x07|\x1b\\)|[78()=>][A-Z0-9]?)')


class Harness:
    def __init__(self, binary, stage):
        self.binary, self.stage = binary, stage
        self.since = natives.start_time('self')
        self.root = Path(tempfile.mkdtemp(prefix='quota-klx-' + stage + '-', dir='/tmp'))
        print('scratch', self.root, flush=True)
        self.start = time.monotonic()
        self.timeline, self.invocations, self.extra = [], [], {}
        self.lock = threading.Lock()
        self.backend = fixtures.Backend()
        self.backend.on_issue.append(self._issued)
        self.issued = []  # (access, label) pairs, re-sent to every (re)started gate
        self.static = self._static()
        self.processes = []

    def _issued(self, access, label):
        self.issued.append((access, label))
        for invocation in self.invocations:
            invocation.label(access, label)

    def _static(self):
        """A dumb-HTTP git remote and a pip-style simple index, both outside every boundary."""
        site = self.root / 'site'
        (site / 'simple' / 'klx-fixture').mkdir(parents=True)
        (site / 'simple' / 'klx-fixture' / 'index.html').write_text(
            '<a href="klx_fixture-1.0-py3-none-any.whl">klx_fixture-1.0</a>\n')
        work = self.root / 'repo-work'
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(self.root), 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
        work.mkdir()
        (work / 'README').write_text('klx fixture repository\n')
        for command in (['git', 'init', '--quiet', '-b', 'main'], ['git', 'add', 'README'],
                        ['git', 'commit', '--quiet', '-m', 'fixture']):
            subprocess.run(command, cwd=work, env=env, check=True, capture_output=True, stdin=subprocess.DEVNULL)
        subprocess.run(['git', 'clone', '--quiet', '--bare', str(work), str(site / 'repo.git')], env=env,
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
        subprocess.run(['git', 'update-server-info'], cwd=site / 'repo.git', env=env, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)
        return fixtures.StaticServer(site)

    def record(self, event, **data):
        row = {'at': round(time.monotonic() - self.start, 4), 'mono': time.monotonic(), 'event': event, **data}
        with self.lock:
            self.timeline.append(row)
        print(event, json.dumps(data, default=str)[:240], flush=True)

    def wait(self, predicate, timeout, step=.05):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(step)
        return bool(predicate())

    def boundary(self, gate_port, command, verbose=False):
        return [sys.executable, '-I', '-B', str(PROBES / 'nl' / 'boundary.py'), '--gate-port', str(gate_port),
                '--seccomp', 'inet', *(['--verbose'] if verbose else []), '--', *command]

    def run_inside(self, invocation, actor, *args, timeout=40):
        """An in-boundary actor started by the launcher (same L2a rules as a native descendant)."""
        command = self.boundary(invocation.port, [sys.executable, '-I', '-B', str(HERE / 'actors.py'), '--spec',
                                                  str(invocation.spec_path), actor, *args])
        result = subprocess.run(command, env=invocation.env(proxy=False), cwd=invocation.workspace,
                                capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                start_new_session=True)
        return result.returncode

    def finish(self, ok, error=None):
        for invocation in self.invocations:
            invocation.stop()
        self.backend.release.set()
        time.sleep(.3)
        traced = set()
        for invocation in self.invocations:
            traced |= invocation.traced()
        left = natives.lingering(self.root, traced, self.since)
        for item in left:
            try:
                os.kill(item['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        if left:
            time.sleep(.5)
        after = natives.lingering(self.root, traced, self.since)
        data = {'stage': self.stage, 'ok': ok, 'error': error, 'timeline': self.timeline,
                'invocations': [invocation.evidence() for invocation in self.invocations],
                'backend': self.backend.events, 'static': self.static.events, 'extra': self.extra,
                'lingering_killed': len(left), 'lingering_after_kill': len(after),
                'host': {'machine': platform.machine(), 'kernel': platform.release()}}
        self.backend.close()
        self.static.close()
        text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=True, default=str)
        for old, new in ((str(self.root), '<PROBE_ROOT>'), (self.binary, '<NATIVE_BINARY>'),
                         (str(REPO), '<WORKTREE>'), (sys.executable, '<PYTHON>'), (str(Path.home()), '<HOME>')):
            text = text.replace(old, new)
        text = re.sub(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_.-]+', '<JWT>', text)
        text = re.sub(r'fixture-refresh-[0-9a-f]+', '<REFRESH>', text)
        (HERE / 'evidence').mkdir(exist_ok=True)
        (HERE / 'evidence' / (self.stage + '.json')).write_text(text + '\n')


class Control:
    """Launcher end of the private gate channel: replies by id, events to a callback."""

    def __init__(self, sock, on_event):
        self.sock, self.on_event = sock, on_event
        self.replies, self.cond, self.next_id = {}, threading.Condition(), 0
        self.open = True
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        buffer = b''
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError:
                data = b''
            if not data:
                with self.cond:
                    self.open = False
                    self.cond.notify_all()
                return
            buffer += data
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                message = json.loads(line)
                if 'reply' in message:
                    with self.cond:
                        self.replies[message.get('id')] = message
                        self.cond.notify_all()
                else:
                    self.on_event(message)

    def call(self, command, timeout=5, **data):
        with self.cond:
            self.next_id += 1
            ident = self.next_id
        try:
            self.sock.sendall((json.dumps({'cmd': command, 'id': ident, **data}) + '\n').encode())
        except OSError:
            return None
        end = time.monotonic() + timeout
        with self.cond:
            while ident not in self.replies and self.open and time.monotonic() < end:
                self.cond.wait(.05)
            return self.replies.pop(ident, None)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Invocation:
    """One protected invocation: held port, gate process, monitor process, preflight, boundary + TUI."""

    def __init__(self, h, name, *, account=A, user=UA, label='A1', five=20, weekly=30, max_age=60, hold=5.0,
                 hb_max=2.0, hb_interval=0.25, poll=None, refresh='none', link=None, lifetime=3600,
                 upstream_proxy=None, strace=True):
        self.h, self.name, self.account, self.user = h, name, account, user
        self.policy = {'five_hour': five, 'weekly': weekly, 'max_age': max_age}
        self.hold, self.hb_max, self.hb_interval, self.poll = hold, hb_max, hb_interval, poll
        self.refresh, self.upstream_proxy, self.use_strace = refresh, upstream_proxy, strace
        self.base = h.root / name
        for part in ('codex', 'home', 'workspace', 'tmp', 'out'):
            (self.base / part).mkdir(parents=True, mode=0o700)
        self.codex_home, self.workspace = self.base / 'codex', self.base / 'workspace'
        self.gates, self.monitors, self.readers, self.events = [], [], [], []
        self.gate_process = self.monitor_process = self.control = self.monitor_pipe = None
        self.tui = None
        self.terminal = None
        self.paused_events = []
        self.output, self.visible = bytearray(), bytearray()
        self.done = threading.Event()
        self.owner_hold = threading.Event()
        self.pending_pause = []
        self.checks = []
        self.fds = []
        self.ca = None
        self.tls = None
        # Port holder: bound and listening for the invocation's lifetime, inherited by each gate.
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(128)
        self.port = self.listener.getsockname()[1]
        self.url = 'http://127.0.0.1:%d' % self.port
        if link is None:
            access, refresh_token = h.backend.issue(account, user, label, lifetime=lifetime)
            self.write_auth(fixtures.auth_json(access, refresh_token, account, user))
        else:
            os.symlink(link, self.codex_home / 'auth.json')
        (self.codex_home / 'config.toml').write_text(launcher.render_config(self.url, str(self.workspace)))
        self.spec_path = self.base / 'spec.json'
        self.spec = {'gate_port': self.port, 'backend_port': h.backend.port, 'static_port': h.static.port,
                     'codex_home': str(self.codex_home), 'workspace': str(self.workspace),
                     'home': str(self.base / 'home'), 'outdir': str(self.base / 'out'),
                     'paused_file': str(self.workspace / 'PAUSED'), 'model': 'gpt-5.4'}
        self.spec_path.write_text(json.dumps(self.spec))
        h.invocations.append(self)

    # -- files and environment -----------------------------------------------------------------
    def write_auth(self, text):
        path = self.codex_home / 'auth.json'
        temporary = self.codex_home / 'auth.json.klx'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, text.encode())
        os.close(fd)
        os.replace(temporary, path)

    def stored_label(self):
        try:
            access = json.loads((self.codex_home / 'auth.json').read_text())['tokens']['access_token']
        except (OSError, ValueError, KeyError):
            return None
        identity = self.h.backend.identities.get(access)
        return identity and identity['label']

    def env(self, proxy=True):
        return launcher.native_env(home=str(self.base / 'home'), codex_home=str(self.codex_home),
                                   tmp=str(self.base / 'tmp'), gate_url=self.url if proxy else None,
                                   ca=self.ca['ca'] if self.ca and proxy else None)

    def reader_config(self, name='reader'):
        return {'python': sys.executable, 'binary': self.h.binary, 'gate_port': self.port,
                'env': self.env(proxy=False), 'cwd': str(self.workspace),
                'stderr': str(self.base / (name + '.stderr'))}

    # -- gate ------------------------------------------------------------------------------------
    def label(self, access, label):
        if self.control is not None and self.gate_process is not None and self.gate_process.poll() is None:
            self.control.call('label', bearer=access, label=label)

    def start_gate(self, restart=False):
        if self.refresh == 'connect' and self.ca is None:
            self.ca = fixtures.make_ca(self.base / 'ca')
            if self.ca is None:
                raise RuntimeError('openssl unavailable for the CONNECT refresh fixture')
            self.tls = fixtures.TlsRefreshHost(self.h.backend, self.ca)
        index = len(self.gates)
        ours, theirs = socket.socketpair()
        monitor_ours, monitor_theirs = socket.socketpair()
        events = self.base / ('gate-%d.jsonl' % index)
        config = {'events': str(events), 'listen_fd': self.listener.fileno(), 'launcher_fd': theirs.fileno(),
                  'monitor_fd': monitor_theirs.fileno(), 'account': self.account, 'user': self.user,
                  **self.policy, 'upstream': ['127.0.0.1', self.h.backend.port], 'hold': self.hold,
                  'hb_max': self.hb_max, 'restart': restart,
                  'connect_allow': ({'auth.openai.com:443': ['127.0.0.1', self.tls.port]}
                                    if self.refresh == 'connect' else {}),
                  'upstream_proxy': list(self.upstream_proxy) if self.upstream_proxy else None}
        path = self.base / ('gate-%d.json' % index)
        path.write_text(json.dumps(config))
        ready = threading.Event()

        def on_event(message):
            if message.get('event') == 'ready':
                ready.set()
            elif message.get('event') == 'paused':
                self.paused_events.append(message)
                self.pending_pause.append(message)
                self.h.record('gate_paused', invocation=self.name, reason=message['reason'], gate_at=message['at'])
            elif message.get('event') == 'rebind':
                threading.Thread(target=self.read, args=('rebind',), daemon=True).start()

        process = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'gate.py'), str(path)],
                                   pass_fds=(self.listener.fileno(), theirs.fileno(), monitor_theirs.fileno()),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=open(self.base / ('gate-%d.stderr' % index), 'wb'),
                                   start_new_session=True)
        theirs.close()
        monitor_theirs.close()
        if self.control is not None:
            self.control.close()
        self.control = Control(ours, on_event)
        self.gate_process = process
        self.gates.append({'pid': process.pid, 'events': events, 'restart': restart, 'monitor_sock': monitor_ours})
        assert ready.wait(10), 'gate did not start'
        for access, name in self.h.issued:
            self.control.call('label', bearer=access, label=name)
        self.h.record('gate_started', invocation=self.name, index=index, restart=restart, pid=process.pid)
        return process

    def status(self):
        reply = self.control.call('status') if self.control else None
        return reply

    def kill_gate(self):
        self.gate_process.kill()
        self.gate_process.wait(timeout=5)
        self.h.record('gate_killed', invocation=self.name, pid=self.gate_process.pid,
                      exit=self.gate_process.returncode)

    # -- monitor ---------------------------------------------------------------------------------
    def start_monitor(self):
        gate = self.gates[-1]
        read_end, write_end = os.pipe()
        config = {'events': str(self.base / ('monitor-%d.jsonl' % len(self.monitors))),
                  'gate_fd': gate['monitor_sock'].fileno(), 'command_fd': read_end,
                  'hb_interval': self.hb_interval, 'poll_interval': self.poll,
                  'reader': self.reader_config('monitor-reader')}
        path = self.base / ('monitor-%d.json' % len(self.monitors))
        path.write_text(json.dumps(config))
        process = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'monitor.py'), str(path)],
                                   pass_fds=(gate['monitor_sock'].fileno(), read_end), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=open(str(path) + '.stderr', 'wb'),
                                   start_new_session=True)
        os.close(read_end)
        gate['monitor_sock'].close()  # only the monitor holds the gate's heartbeat channel now
        self.monitor_process, self.monitor_pipe = process, write_end
        self.monitors.append({'pid': process.pid, 'events': Path(config['events'])})
        self.h.record('monitor_started', invocation=self.name, pid=process.pid)

    def monitor_command(self, word):
        os.write(self.monitor_pipe, (word + '\n').encode())
        self.h.record('monitor_command', invocation=self.name, command=word)

    # -- controller reads ------------------------------------------------------------------------
    def read(self, why):
        result = reader.read(self.reader_config('reader-' + why))
        row = {'why': why, **{key: result.get(key) for key in ('result', 'init_seconds', 'read_seconds',
                                                               'total_seconds', 'exit', 'error')},
               'started': result.get('started'), 'disk_label': self.stored_label()}
        self.readers.append(row)
        self.h.record('controller_read', invocation=self.name, **row)
        return row

    def preflight(self):
        before = self.h.backend.count('model')
        row = self.read('preflight')
        status = self.status()
        verdict = launcher.preflight_verdict(status)
        self.h.record('preflight', invocation=self.name, verdict=verdict, status=status,
                      model_requests=self.h.backend.count('model') - before)
        return verdict

    # -- native TUI ------------------------------------------------------------------------------
    def start_tui(self, prompt):
        master, slave = pty.openpty()
        outer_master, outer_slave = pty.openpty()
        self.fds += [master, slave, outer_master, outer_slave]
        self.master, self.slave, self.outer_master, self.outer_slave = master, slave, outer_master, outer_slave
        self.termios_before = termios.tcgetattr(slave)
        tty.setraw(outer_slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        argv = launcher.check_args(['--no-alt-screen', '-C', str(self.workspace), prompt])
        inner = self.h.boundary(self.port, [sys.executable, '-I', '-B', '-c', HELPER, self.h.binary, *argv],
                                verbose=True)
        observer = []
        strace = shutil.which('strace') if self.use_strace else None
        if strace:
            observer = [strace, '-ff', '-q', '-s', '96', '-e',
                        'trace=execve,clone,clone3,fork,vfork,socket,socketpair,connect,sendto,sendmsg,sendmmsg,'
                        'bind,listen,getsockname', '-e', 'signal=none', '-o', str(self.base / 'strace.log')]
        self.tui = subprocess.Popen([*observer, *inner], env=self.env(proxy=True), cwd=self.workspace, stdin=slave,
                                    stdout=slave, stderr=slave, close_fds=True, start_new_session=True)
        self.h.record('tui_started', invocation=self.name, pid=self.tui.pid, argv_head=argv[:1],
                      observer=bool(strace))

        def display(message):
            self.h.record('control_display', invocation=self.name, message=message)
            os.write(outer_slave, ('\r\n[quota-research] ' + message + '\r\n').encode())

        def check():
            return self.continuation_check()

        def grant():
            self.h.record('continuation_granted', invocation=self.name)

        self.terminal = control.TerminalControl(outer_slave, lambda data: os.write(master, data), display,
                                                check, grant)
        threading.Thread(target=self._native_output, daemon=True).start()
        threading.Thread(target=self._emulator, daemon=True).start()
        threading.Thread(target=self._owner, daemon=True).start()

    def _native_output(self):
        while not self.done.is_set():
            if not select.select([self.master], [], [], .1)[0]:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            self.output.extend(data)
            try:
                os.write(self.outer_slave, data)
            except OSError:
                return

    def _emulator(self):
        while not self.done.is_set():
            if not select.select([self.outer_master], [], [], .1)[0]:
                continue
            try:
                data = os.read(self.outer_master, 65536)
            except OSError:
                return
            self.visible.extend(data)
            if b'\x1b[6n' in data:
                os.write(self.outer_master, b'\x1b[1;1R')
            if b'\x1b[c' in data:
                os.write(self.outer_master, b'\x1b[?1;2c')

    def _owner(self):
        """The single owner loop: pause notices first (input drained), then pump trusted input."""
        while not self.done.is_set():
            while self.pending_pause:
                message = self.pending_pause.pop(0)
                self.terminal.pause()
                self.h.record('terminal_paused', invocation=self.name, reason=message['reason'])
            if self.owner_hold.is_set():
                time.sleep(.02)
                continue
            try:
                self.terminal.pump(.03)
            except (OSError, EOFError):
                return

    def continuation_check(self):
        reply = self.control.call('continue')
        accepted = bool(reply and reply.get('accepted'))
        row = self.read('continuation') if accepted else None
        status = self.status()
        resumed = bool(accepted and status and status.get('paused') is None and not status.get('lost'))
        self.checks.append({'accepted': accepted, 'read': row and row.get('result'), 'status': status,
                            'resumed': resumed})
        self.h.record('continuation_check', invocation=self.name, accepted=accepted, resumed=resumed,
                      status=status)
        return resumed

    def press(self, data, pause=.15):
        os.write(self.outer_master, data)
        time.sleep(pause)

    def user_text(self, text):
        for char in text:
            os.write(self.outer_master, char.encode())
            time.sleep(.012)
        time.sleep(.35)
        self.press(b'\r', .3)
        self.h.record('user_typed', invocation=self.name, text=text)

    def confirm(self):
        """The user presses the research F12 key and types the displayed challenge."""
        self.press(control.KEY, .3)
        assert self.h.wait(lambda: self.terminal.challenge is not None, 3), 'no challenge displayed'
        before = len(self.checks)
        for char in self.terminal.challenge:
            self.press(char.encode(), .02)
        self.press(b'\r', .1)
        self.h.wait(lambda: len(self.checks) > before, 15)
        return self.checks[-1]['resumed'] if len(self.checks) > before else None

    def screen(self, since=0):
        return ANSI.sub('', bytes(self.output[since:]).decode(errors='replace'))

    def tui_alive(self):
        return self.tui is not None and self.tui.poll() is None

    def exit_tui(self):
        for _ in range(3):
            if not self.tui_alive():
                break
            self.press(b'\x03', .6)
        exited = self.h.wait(lambda: not self.tui_alive(), 6)
        try:
            restored = termios.tcgetattr(self.slave) == self.termios_before
        except termios.error:
            restored = None
        self.h.record('tui_exit', invocation=self.name, exited=exited, exit=self.tui.poll(), termios_restored=restored)
        return exited

    # -- teardown and evidence -------------------------------------------------------------------
    def traced(self):
        return {path.name.rsplit('.', 1)[1] for path in self.base.glob('strace.log.*')}

    def stop(self):
        self.done.set()
        if self.monitor_process is not None and self.monitor_process.poll() is None:
            try:
                self.monitor_command('exit')
            except OSError:
                pass
            try:
                self.monitor_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.monitor_process.kill()
        if self.tui is not None and self.tui.poll() is None:
            for pid in [int(p) for p in self.traced()] + [self.tui.pid]:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self.tui is not None:
            try:
                self.tui.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if self.gate_process is not None and self.gate_process.poll() is None:
            self.control.call('shutdown', timeout=1)
            try:
                self.gate_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.gate_process.kill()
                self.gate_process.wait(timeout=3)
        if self.control is not None:
            self.control.close()
        self.listener.close()
        if self.tls is not None:
            self.tls.close()
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def actor_rows(self):
        rows = []
        for path in sorted((self.base / 'out').glob('*.jsonl')):
            for line in path.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
        return sorted(rows, key=lambda row: row['at'])

    def evidence(self):
        def lines(path):
            try:
                return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
            except OSError:
                return []
        files = sorted(self.base.glob('strace.log.*'))
        trace = None
        if files:
            parsed = natives.parse_traces(files)
            roots = parsed['roots']
            trace = {'processes': len(files), 'executables': sorted(set(parsed['exe'].values())),
                     'syscalls': natives.summarize(parsed['rows'], self.port, self.h.static.port,
                                                   self.h.backend.port),
                     'native_main_exit': parsed['exits'].get(roots[0]) if len(roots) == 1 else None}
        loop = self.workspace / 'loop.txt'
        return {'name': self.name, 'port': self.port, 'policy': self.policy, 'refresh': self.refresh,
                'gates': [{'pid': gate['pid'], 'restart': gate['restart'], 'events': lines(gate['events'])}
                          for gate in self.gates],
                'monitors': [{'pid': monitor['pid'], 'events': lines(monitor['events'])} for monitor in self.monitors],
                'readers': self.readers, 'checks': self.checks, 'actors': self.actor_rows(),
                'loop': [line.split() for line in loop.read_text().splitlines()] if loop.exists() else [],
                'loop_finished': (self.workspace / 'loop.finished').exists(),
                'tui_exit': self.tui.poll() if self.tui else None, 'trace': trace,
                'screen_tail': re.sub(r'\S*/\S*', '<PATH>', self.screen())[-3000:],
                'control_visible_tail': ANSI.sub('', bytes(self.visible).decode(errors='replace'))[-1500:]}


def tool_command(h, invocation, steps):
    actor = '%s -I -B %s --spec %s' % (sys.executable, HERE / 'actors.py', invocation.spec_path)
    return '; '.join(step.replace('ACT', actor) for step in steps) + '; echo KLX tool finished'


# -- stages ----------------------------------------------------------------------------------------

def stage_pause(h):
    """Cases 1, 2, 9, 10 and 5 on one invocation: threshold latch, children, tools, workflow,
    then fresh manual continuation of the same bound invocation."""
    inv = Invocation(h, 'a', poll=2.0, max_age=60)
    inv.start_gate()
    verdict = inv.preflight()
    assert verdict is None, verdict
    inv.start_monitor()
    h.backend.hold_titles = True
    h.backend.tools['KLX-TOOL-work'] = tool_command(h, inv, [
        'ACT sdk --marker KLX-HOLD-child --wait PAUSED_FILE --seconds 60 >/dev/null 2>&1 &',
        'ACT testlike --seconds 40 >/dev/null 2>&1 &',
        'setsid -f ACT grandchild --seconds 90 </dev/null >/dev/null 2>&1',
        'ACT workflow --tag before >/dev/null 2>&1',
        'ACT loop --seconds 20']).replace('PAUSED_FILE', inv.spec['paused_file'])
    h.backend.tool_yield['KLX-TOOL-work'] = 30000  # the turn stays inside the tool call through the pause
    inv.start_tui('KLX-TOOL-work run the fixture tool')
    assert h.wait(lambda: (inv.workspace / 'loop.txt').exists(), 45), 'tool loop did not start'
    assert h.wait(lambda: (inv.status() or {}).get('streams', 0) >= 2, 15), 'held title and child streams absent'
    h.record('streams_before_latch', status=inv.status())
    inv.user_text('KLX-QUEUED-1 follow up typed while the tool runs')
    # Stale typed input: keys the user typed before the pause stay buffered and must be drained.
    inv.owner_hold.set()
    time.sleep(.15)  # let a pump already inside select() return before the user types
    inv.press(control.KEY + b'deadbeef\r', .1)
    latch_mark = time.monotonic()
    h.backend.set_usage(A, 85, 40)
    assert h.wait(lambda: inv.paused_events, 10), 'threshold did not latch'
    latch = inv.paused_events[0]['at']
    h.extra['latch_at'] = latch
    h.extra['usage_crossed_at'] = latch_mark
    time.sleep(.2)
    inv.owner_hold.clear()
    time.sleep(.3)
    h.record('stale_input_after_pause', challenge=inv.terminal.challenge is not None, checks=len(inv.checks))
    assert h.wait(lambda: h.backend.count('stream_lost') >= 2, 5), 'held streams were not closed'
    (inv.workspace / 'PAUSED').write_text('paused')
    h.wait(lambda: any(r['attempt'] == 'git_clone_after' for r in inv.actor_rows()), 30)
    h.wait(lambda: any(r['attempt'] == 'model_after_signal' for r in inv.actor_rows()), 10)
    lines_at_esc = len((inv.workspace / 'loop.txt').read_text().splitlines())
    inv.press(b'\x1b', .2)
    h.record('native_interrupt_key', loop_lines=lines_at_esc, loop_finished=(inv.workspace / 'loop.finished').exists())
    time.sleep(2.5)
    h.record('after_interrupt', loop_lines=len((inv.workspace / 'loop.txt').read_text().splitlines()),
             loop_finished=(inv.workspace / 'loop.finished').exists(), tui_alive=inv.tui_alive())
    inv.press(b'\x7f' * 80, .3)
    mark = len(inv.output)
    inv.user_text('/status')
    h.wait(lambda: 'Context window' in inv.screen(mark), 6)
    inv.press(b'\x1b[5~', .4)
    inv.press(b'\x1b[6~', .4)
    h.record('tui_usable_while_paused', status_card='Context window' in inv.screen(mark),
             tui_alive=inv.tui_alive(), screen_bytes=len(inv.output) - mark)
    # Background wakeup: quota recovers and the monitor's fresh reads pass, but nothing reopens.
    h.backend.set_usage(A, 30, 40)
    polls = h.backend.count('usage')
    h.wait(lambda: h.backend.count('usage') >= polls + 2, 8)
    h.record('recovered_quota_no_resume', status=inv.status())
    # Paste of a displayed challenge, and native-output keys, never confirm.
    inv.press(control.KEY, .3)
    challenge = inv.terminal.challenge
    inv.press(control.PASTE_START + (challenge or '').encode() + b'\r' + control.PASTE_END, .4)
    os.write(inv.outer_slave, control.KEY + b'\r\n' + (challenge or 'x').encode() + b'\r\n')
    time.sleep(.4)
    h.record('paste_and_output_no_check', challenge_shown=challenge is not None, checks=len(inv.checks),
             status=inv.status())
    # A failing fresh check stays paused with the deterministic message.
    h.backend.usage_override = lambda identity: (503, {'error': 'fixture outage'})
    failed = inv.confirm()
    h.backend.usage_override = None
    mark = len(inv.output)
    inv.user_text('KLX-AFTER-FAIL typed after a failed check')
    h.wait(lambda: 'quota reserve gate refused' in inv.screen(mark), 8)
    h.record('failed_check', resumed=failed, message_visible='quota reserve gate refused' in inv.screen(mark),
             status=inv.status())
    passed = inv.confirm()
    mark = len(inv.output)
    inv.user_text('KLX-RESUMED after a fresh passing check')
    h.wait(lambda: 'KLX fixture reply KLX-RESUMED' in inv.screen(mark), 10)
    h.record('resumed_same_invocation', resumed=passed, reply_visible='KLX fixture reply KLX-RESUMED' in inv.screen(mark),
             gate_pid=inv.gate_process.pid, tui_pid=inv.tui.pid, tui_alive=inv.tui_alive(), status=inv.status())
    inv.exit_tui()


STAGES = {'pause': stage_pause}


def main(binary, stage):
    h = Harness(binary, stage)
    ok, error = False, None
    try:
        STAGES[stage](h)
        ok = True
    except Exception as failure:  # recorded, never evidence of capability
        error = '%s: %s' % (type(failure).__name__, failure)
        h.record('stage_error', error=error)
        raise
    finally:
        h.finish(ok, error)  # run.py archives the scratch root under .cache/klx/ and removes it
    return 0


if __name__ == '__main__':
    sys.exit(main(str(Path(sys.argv[1]).resolve()), sys.argv[2]))
