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
import errno
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
                 upstream_proxy=None, strace=True, self_read=False):
        self.h, self.name, self.account, self.user = h, name, account, user
        self.policy = {'five_hour': five, 'weekly': weekly, 'max_age': max_age}
        self.hold, self.hb_max, self.hb_interval, self.poll = hold, hb_max, hb_interval, poll
        self.refresh, self.upstream_proxy, self.use_strace = refresh, upstream_proxy, strace
        self.self_read = self_read
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
                  'upstream_proxy': list(self.upstream_proxy) if self.upstream_proxy else None,
                  'self_read': self.self_read}
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
        # A binding read first: after an identity latch the lease holds no credential, so the
        # check's own read would start a new epoch and use the continuation up (QX rule).
        self.read('continuation-bind')
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


    def restart(self, kill=False):
        """Replace this invocation's gate (and monitor) on the same held port; the new gate starts
        latched and needs a trusted continuation plus a fresh passing read."""
        if self.gate_process is not None and self.gate_process.poll() is None:
            if kill:
                self.kill_gate()
            else:
                self.control.call('shutdown', timeout=1)
                try:
                    self.gate_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.kill_gate()
        if self.monitor_process is not None and self.monitor_process.poll() is None:
            self.monitor_command('exit')
            try:
                self.monitor_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.monitor_process.kill()
                self.monitor_process.wait(timeout=3)
        self.start_gate(restart=True)
        self.start_monitor()

    def held(self, count=1, timeout=15):
        return self.h.wait(lambda: (self.status() or {}).get('streams', 0) >= count, timeout)

    def status_card(self):
        self.press(b'\x7f' * 80, .3)
        mark = len(self.output)
        self.user_text('/status')
        shown = self.h.wait(lambda: 'Context window' in self.screen(mark), 6)
        self.press(b'\x1b', .3)
        return shown

    def turn(self, text, expect, timeout=12):
        """Type a prompt and wait until `expect` appears on the native screen."""
        mark = len(self.output)
        self.user_text(text)
        return self.h.wait(lambda: expect in self.screen(mark), timeout)

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
        refresh_failures = sum(path.read_text(errors='replace').count('Failed to refresh token')
                               for path in self.base.glob('*reader*.stderr'))
        return {'reader_refresh_failures': refresh_failures, 'name': self.name, 'port': self.port, 'policy': self.policy, 'refresh': self.refresh,
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


def stage_loss(h):
    """Case 3: monitor EOF, monitor SIGKILL and a stalled monitor each latch the gate closed."""
    inv = Invocation(h, 'a', hb_max=1.5, poll=None)
    inv.start_gate()
    assert inv.preflight() is None
    inv.start_monitor()
    inv.start_tui('KLX-START a first turn')
    assert h.wait(lambda: 'KLX fixture reply KLX-START' in inv.screen(), 30), 'first turn did not complete'
    results = []
    for case in ('eof', 'kill', 'stall'):
        before = len(inv.paused_events)
        marker = 'KLX-HOLD-' + case
        inv.user_text(marker + ' a long native stream')
        assert inv.held(1), 'no held stream for ' + case
        trigger = time.monotonic()
        if case == 'eof':
            inv.monitor_command('close')
        elif case == 'kill':
            inv.monitor_process.kill()
            inv.monitor_process.wait(timeout=5)
            h.record('monitor_killed', invocation=inv.name, pid=inv.monitor_process.pid)
        else:
            inv.monitor_command('stall')
        assert h.wait(lambda: len(inv.paused_events) > before, 6), 'loss did not latch for ' + case
        latch = inv.paused_events[-1]
        h.wait(lambda: any(b['kind'] == 'stream_lost' and b.get('marker') == marker for b in h.backend.events), 4)
        refused = inv.turn('KLX-AFTER-' + case + ' typed after the loss', 'quota reserve gate refused')
        card = inv.status_card()
        results.append({'case': case, 'trigger': trigger, 'latch_at': latch['at'], 'reason': latch['reason'],
                        'refusal_visible': refused, 'status_card': card, 'tui_alive': inv.tui_alive(),
                        'monitor_alive': inv.monitor_process.poll() is None, 'status': inv.status()})
        h.record('loss_case', **results[-1])
        inv.restart()
        refused_restart = inv.turn('KLX-RESTARTED-' + case + ' before continuation', 'quota reserve gate refused')
        resumed = inv.confirm()
        completed = inv.turn('KLX-RECOVERED-' + case + ' after continuation', 'KLX fixture reply KLX-RECOVERED-' + case)
        h.record('loss_recovery', case=case, refused_before_continuation=refused_restart, resumed=resumed,
                 completed=completed, gate_pid=inv.gate_process.pid, tui_pid=inv.tui.pid)
    h.extra['loss_cases'] = results
    inv.exit_tui()


def stage_restart(h):
    """Case 4: gate death and port reuse; a restarted gate needs a fresh verified continuation."""
    inv = Invocation(h, 'a', poll=2.0)
    inv.start_gate()
    assert inv.preflight() is None
    inv.start_monitor()
    inv.start_tui('KLX-START a first turn')
    assert h.wait(lambda: 'KLX fixture reply KLX-START' in inv.screen(), 30)

    def outside_bind(tag):
        rows = []
        for name, address in (('loopback', '127.0.0.1'), ('wildcard', '0.0.0.0'), ('other', '127.0.0.2')):
            sock = socket.socket()
            try:
                sock.bind((address, inv.port))
                rows.append((name, 'bound'))
            except OSError as error:
                rows.append((name, errno.errorcode.get(error.errno, str(error.errno))))
            finally:
                sock.close()
        h.record('outside_bind', tag=tag, results=rows)
        return rows

    h.extra['bind_while_alive'] = outside_bind('gate-alive')
    inv.user_text('KLX-HOLD-gate a long native stream')
    assert inv.held(1)
    models_before = h.backend.count('model')
    kill_at = time.monotonic()
    inv.kill_gate()
    h.extra['gate_killed_at'] = kill_at
    h.wait(lambda: any(b['kind'] == 'stream_lost' and b.get('marker') == 'KLX-HOLD-gate' for b in h.backend.events), 4)
    h.extra['bind_while_dead'] = outside_bind('gate-dead')
    code = h.run_inside(inv, 'bind', '--out', str(inv.base / 'out' / 'bind-dead.jsonl'))
    h.record('inside_bind', exit=code)
    inv.user_text('KLX-WHILE-DEAD typed while the gate is dead')
    time.sleep(2.0)
    h.extra['models_while_dead'] = h.backend.count('model') - models_before
    inv.restart()
    time.sleep(4.5)  # monitor polls pass while latched
    h.record('restarted_latched', status=inv.status(), screen_refusal='gate-restarted' in inv.screen())
    refused = inv.turn('KLX-BEFORE-CONTINUE after the restart', 'quota reserve gate refused')
    resumed = inv.confirm()
    completed = inv.turn('KLX-AFTER-CONTINUE after the restart', 'KLX fixture reply KLX-AFTER-CONTINUE')
    h.record('restart_continuation', refused_before=refused, resumed=resumed, completed=completed,
             gate_pid=inv.gate_process.pid, tui_pid=inv.tui.pid, status=inv.status())
    h.extra['bind_after_restart'] = outside_bind('gate-restarted')
    inv.exit_tui()


def stage_sessions(h):
    """Case 6: two protected invocations of one account, different reserves; outside use counts.
    The second gate reaches the backend through a loopback CONNECT proxy captured at startup."""
    secrets = h.root / 'secrets'
    secrets.mkdir(mode=0o700)
    access, refresh = h.backend.issue(A, UA, 'A1')
    secret = secrets / 'codex-fixture.json'
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, fixtures.auth_json(access, refresh, A, UA).encode())
    os.close(fd)
    proxy = fixtures.ConnectProxy({'127.0.0.1:%d' % h.backend.port})
    one = Invocation(h, 's1', five=20, poll=1.5, link=str(secret))
    two = Invocation(h, 's2', five=10, poll=1.5, link=str(secret), upstream_proxy=('127.0.0.1', proxy.port))
    for inv in (one, two):
        inv.start_gate()
        assert inv.preflight() is None
        inv.start_monitor()
    one.start_tui('KLX-S1-START first session')
    two.start_tui('KLX-S2-START second session')
    assert h.wait(lambda: 'KLX fixture reply KLX-S1-START' in one.screen()
                  and 'KLX fixture reply KLX-S2-START' in two.screen(), 40), 'sessions did not start'

    def outside(count, consume):
        h.backend.consume = consume
        for index in range(count):
            fixtures_request(h.backend.port, access, A, 'KLX-OUTSIDE-%d' % index)
        h.backend.consume = 0.0
        h.record('outside_consumption', requests=count, usage=dict(h.backend.usage[A]))

    outside(10, 6.0)  # 25 -> 85 percent used: 15 remaining
    assert h.wait(lambda: one.paused_events, 8), 'first session did not pause'
    time.sleep(2.0)
    first = one.turn('KLX-S1-AFTER outside use', 'quota reserve gate refused')
    second = two.turn('KLX-S2-AFTER outside use', 'KLX fixture reply KLX-S2-AFTER')
    h.record('independent_sessions', s1_refused=first, s2_completed=second, s1=one.status(), s2=two.status())
    outside(2, 3.5)  # 85 -> 92 percent used: 8 remaining, below the second reserve too
    assert h.wait(lambda: two.paused_events, 8), 'second session did not pause'
    late = two.turn('KLX-S2-LATE after more outside use', 'quota reserve gate refused')
    h.record('second_paused', s2_refused=late, s2=two.status())
    h.extra['proxy_tunnels'] = proxy.events
    for inv in (one, two):
        inv.exit_tui()
    proxy.close()


def fixtures_request(port, access, account, marker):
    """An unprotected client using the same account directly (outside every invocation)."""
    import http.client
    body = json.dumps({'model': 'gpt-5.4', 'stream': True,
                       'input': [{'type': 'message', 'role': 'user',
                                  'content': [{'type': 'input_text', 'text': marker}]}]})
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    connection.request('POST', '/backend-api/codex/responses', body=body,
                       headers={'Authorization': 'Bearer ' + access, 'ChatGPT-Account-ID': account})
    connection.getresponse().read()
    connection.close()


def stage_rotation(h, self_read=False):
    """Case 7: native refresh A -> A' and a disk switch A -> B while child requests are queued.
    With self_read the gate binds a held bearer by its own usage GET with that bearer (variant)."""
    h.extra['rebind_mode'] = 'gate-read' if self_read else 'controller-reader'
    inv = Invocation(h, 'a', refresh='connect', hold=5.0, poll=None, self_read=self_read)
    inv.start_gate()
    assert inv.preflight() is None
    inv.start_monitor()
    long_lived = reader.Session(inv.reader_config('long-lived'))
    assert long_lived.initialize()
    first = long_lived.read()
    h.record('long_lived_read', phase='before', result=first['result'], disk_label=inv.stored_label())
    signals = {name: inv.workspace / ('SIGNAL-' + name) for name in ('old', 'new', 'b')}
    h.backend.tools['KLX-TOOL-children'] = tool_command(h, inv, [
        'setsid -f ACT sdk --marker KLX-C-OLD --wait %s --seconds 90 </dev/null >/dev/null 2>&1' % signals['old'],
        'setsid -f ACT sdk --marker KLX-C-NEW --wait %s --seconds 90 --reread --out %s </dev/null >/dev/null 2>&1'
        % (signals['new'], inv.base / 'out' / 'sdk-new.jsonl'),
        'setsid -f ACT sdk --marker KLX-C-B --wait %s --seconds 90 --reread --out %s </dev/null >/dev/null 2>&1'
        % (signals['b'], inv.base / 'out' / 'sdk-b.jsonl')])
    inv.start_tui('KLX-TOOL-children start the SDK-like children')
    assert h.wait(lambda: any(b['kind'] == 'model' and b.get('request') == 'tool_result' for b in h.backend.events), 40), \
        'children turn did not finish'
    assert h.wait(lambda: sum(r['attempt'] == 'model_before_latch' for r in inv.actor_rows()) >= 3, 10)
    old_label = inv.stored_label()
    queued = {'armed': True}
    original = inv.read

    def read_with_queue(why):
        if why == 'rebind' and queued['armed']:
            queued['armed'] = False
            signals['old'].write_text('go')  # the old-bearer child request arrives during the hold
            h.record('child_released_during_hold')
        return original(why)
    inv.read = read_with_queue
    if self_read:  # no controller round trip: release the old-bearer child right at the refresh
        h.backend.on_issue.append(lambda access, label: label.startswith('A1r') and queued.pop('armed', None)
                                  and signals['old'].write_text('go'))
    h.backend.revoke(next(a for a, label in h.issued if label == old_label))
    rotated = inv.turn('KLX-ROT-1 after the server revoked the bearer', 'KLX fixture reply KLX-ROT-1', 20)
    h.record('native_refresh', completed=rotated, disk_label=inv.stored_label(), status=inv.status())
    h.wait(lambda: any(r['attempt'] == 'model_after_signal' and r['marker'].startswith('KLX-C-OLD')
                       for r in inv.actor_rows()), 10)
    signals['new'].write_text('go')
    h.wait(lambda: any(r['marker'].startswith('KLX-C-NEW') and r['attempt'] == 'model_after_signal_reread'
                       for r in inv.actor_rows() if 'marker' in r), 15)
    stale = long_lived.read()
    h.record('long_lived_read', phase='after-refresh', result=stale['result'], disk_label=inv.stored_label())
    h.extra['long_lived_exit'] = long_lived.close()
    recovered = inv.turn('KLX-ROT-2 after the long-lived read', 'KLX fixture reply KLX-ROT-2', 15)
    h.record('after_long_lived', completed=recovered, status=inv.status())
    # A -> B on disk (another terminal logs in as B); the TUI keeps A' in memory.
    a_text = (inv.codex_home / 'auth.json').read_text()
    access_b, refresh_b = h.backend.issue(B, UB, 'B1')
    inv.write_auth(fixtures.auth_json(access_b, refresh_b, B, UB))
    signals['b'].write_text('go')
    h.wait(lambda: any(r.get('marker', '').startswith('KLX-C-B') and r['attempt'] == 'model_after_signal_reread'
                       for r in inv.actor_rows()), 10)
    refused = inv.turn('KLX-ROT-B after the disk switch', 'quota reserve gate refused')
    resumed_b = inv.confirm()
    h.record('switch_to_b', refused=refused, resumed_with_b_on_disk=resumed_b, status=inv.status())
    inv.write_auth(a_text)
    resumed_a = inv.confirm()
    completed = inv.turn('KLX-ROT-A after switching back', 'KLX fixture reply KLX-ROT-A')
    h.record('switch_back', resumed=resumed_a, completed=completed, status=inv.status())
    inv.exit_tui()


def stage_refresh(h):
    """Case 8: native refresh through the gate's CONNECT allowlist (a), and no refresh path (b)."""
    inv = Invocation(h, 'a', refresh='connect', hold=5.0, poll=2.0, lifetime=120)
    inv.start_gate()
    assert inv.preflight() is None
    h.record('reader_refresh_attempts', refreshes=h.backend.count('refresh'))
    inv.start_monitor()
    inv.start_tui('KLX-REFRESH-A first turn')
    completed = h.wait(lambda: 'KLX fixture reply KLX-REFRESH-A' in inv.screen(), 30)
    h.record('connect_refresh', completed=completed, refreshes=h.backend.count('refresh'),
             disk_label=inv.stored_label(), status=inv.status())
    inv.spec['ca'] = inv.ca['ca']
    inv.spec_path.write_text(json.dumps(inv.spec))
    h.record('tunnel_running', exit=h.run_inside(inv, 'tunnel', '--out', str(inv.base / 'out' / 'tunnel-running.jsonl')))
    h.backend.set_usage(A, 85, 40)
    assert h.wait(lambda: inv.paused_events, 8)
    h.record('tunnel_paused', exit=h.run_inside(inv, 'tunnel', '--out', str(inv.base / 'out' / 'tunnel-paused.jsonl')))
    inv.exit_tui()
    h.backend.set_usage(A, 25, 40)
    nr = Invocation(h, 'b', refresh='none', hold=5.0, poll=2.0, lifetime=120, max_age=8, label='A5')
    nr.start_gate()
    assert nr.preflight() is None
    nr.start_monitor()
    nr.start_tui('KLX-NOREFRESH-1 first turn')
    first = h.wait(lambda: 'KLX fixture reply KLX-NOREFRESH-1' in nr.screen(), 30)
    h.record('no_refresh_first', completed=first, disk_label=nr.stored_label(), status=nr.status())
    expired_at = time.monotonic()
    h.backend.revoke(next(a for a, label in h.issued if label == 'A5'))
    h.extra['expired_at'] = expired_at
    stale = h.wait(lambda: nr.paused_events, 20)
    h.record('no_refresh_stale', paused=stale, status=nr.status(),
             seconds=round(time.monotonic() - expired_at, 2))
    refused = nr.turn('KLX-NOREFRESH-2 after expiry', 'quota reserve gate refused')
    h.record('no_refresh_refused', refused=refused, status=nr.status())
    nr.exit_tui()


def stage_startup(h):
    """Case 11: every startup refusal happens before any model request; the TUI never starts."""
    rows = []

    def refusal(name, check):
        try:
            check()
            rows.append({'case': name, 'refused': False})
        except launcher.Refusal as error:
            rows.append({'case': name, 'refused': True, 'message': str(error)})
        h.record('startup_check', **rows[-1])

    fake = h.root / 'old-codex'
    fake.write_text('#!/bin/sh\necho "codex-cli 0.150.0"\n')
    fake.chmod(0o700)
    scratch = {'PATH': '/usr/bin:/bin', 'HOME': str(h.root), 'CODEX_HOME': str(h.root / 'version-home')}
    (h.root / 'version-home').mkdir()

    def version_of(binary):
        return subprocess.run([binary, '--version'], env=scratch, capture_output=True, text=True, timeout=15,
                              stdin=subprocess.DEVNULL).stdout
    refusal('version-old', lambda: launcher.check_version(version_of(str(fake))))
    refusal('version-real', lambda: launcher.check_version(version_of(h.binary)))
    for argv in (['--remote', 'ws://127.0.0.1:9'], ['resume', '--last'], ['fork'], ['remote-control'],
                 ['-c', 'chatgpt_base_url="https://example.invalid"'], ['--profile', 'other'],
                 ['-c', 'model_providers.x.base_url="http://127.0.0.1:9"']):
        refusal('args ' + ' '.join(argv), lambda argv=argv: launcher.check_args(argv))
    refusal('args plain prompt', lambda: launcher.check_args(['KLX prompt']))
    refusal('env refresh override', lambda: launcher.check_env({'CODEX_REFRESH_TOKEN_URL_OVERRIDE': 'x'}))
    abi = None
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        abi = libc.syscall(444, None, 0, 1)
        abi = abi if abi > 0 else None
    except OSError:
        abi = None
    host = {'system': platform.system(), 'machine': platform.machine(), 'landlock_abi': abi,
            'legacy_tiocsti': launcher.read_legacy_tiocsti(), 'socket_stdio': []}
    h.extra['host_probe'] = host
    refusal('host actual', lambda: launcher.check_host(**host))
    for name, change in (('host abi3', {'landlock_abi': 3}), ('host no landlock', {'landlock_abi': None}),
                         ('host tiocsti', {'legacy_tiocsti': 1}), ('host arm64', {'machine': 'aarch64'}),
                         ('host socket stdio', {'socket_stdio': [0]})):
        refusal(name, lambda change=change: launcher.check_host(**{**host, **change}))
    # Native boundary refusal with a socket on stdin: no native starts.
    left, right = socket.socketpair()
    result = subprocess.run(h.boundary(9, ['/bin/sh', '-c', 'echo native-started']), stdin=left,
                            capture_output=True, text=True, timeout=15)
    left.close()
    right.close()
    h.extra['socket_stdio_boundary'] = {'exit': result.returncode, 'stderr': result.stderr.strip()[-160:],
                                        'native_started': 'native-started' in result.stdout}
    h.record('socket_stdio_boundary', **h.extra['socket_stdio_boundary'])
    wall = int(time.time())
    win = fixtures.usage_body(A, UA, wall=wall)['rate_limit']
    five, week = win['primary_window'], win['secondary_window']

    def body(**changes):
        value = fixtures.usage_body(A, UA, wall=wall)
        for key, change in changes.items():
            if key == 'rate':
                value['rate_limit'].update(change)
            else:
                value[key] = change
        return value

    def slow(identity):
        time.sleep(1.0)
        return 200, body()
    cases = [('missing-503', lambda identity: (503, {'error': 'fixture outage'}), {}),
             ('malformed-non-json', lambda identity: (200, b'not json'), {}),
             ('unsupported-family', lambda identity: (200, body(rate_limit=None)), {}),
             ('unsupported-plan', lambda identity: (200, body(plan_type='fixture_unknown_plan')), {}),
             ('weekly-missing', lambda identity: (200, body(rate={'secondary_window': None})), {}),
             ('reset-in-past', lambda identity: (200, body(rate={'primary_window': dict(five, reset_at=wall - 7200)})), {}),
             ('identity-mismatch', lambda identity: (200, body(account_id=B)), {}),
             ('below-reserve', lambda identity: (200, body(rate={'primary_window': dict(five, used_percent=85)})), {}),
             ('stale', slow, {'max_age': 0.5}),
             ('passing-control', None, {})]
    verdicts = []
    for name, override, options in cases:
        before = h.backend.count('model')
        inv = Invocation(h, 'q-' + name, **options)
        inv.start_gate()
        h.backend.usage_override = override
        verdict = inv.preflight()
        h.backend.usage_override = None
        verdicts.append({'case': name, 'verdict': verdict, 'model_requests': h.backend.count('model') - before,
                         'tui_started': inv.tui is not None})
        h.record('preflight_case', **verdicts[-1])
        inv.stop()
    h.extra['startup_checks'] = rows
    h.extra['preflight_cases'] = verdicts
    _ = week


STAGES = {'pause': stage_pause, 'loss': stage_loss, 'restart': stage_restart, 'sessions': stage_sessions,
          'rotation': stage_rotation, 'rotation_gate': lambda h: stage_rotation(h, self_read=True),
          'refresh': stage_refresh, 'startup': stage_startup}


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
