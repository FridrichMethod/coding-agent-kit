"""Two fixed Bash lifecycle observations in the actual Claude TUI.

Invoke only through run.py, which installs the unchanged PC network guard.
The PA1C Messages gate is imported unchanged; no production control code.
"""
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

HERE = Path(__file__).resolve().parent
COMMAND = ('printf "%s\\n" "$$" > tool.pid; printf "started\\n" > started; '
           'sleep 4; printf "finished\\n" > finished; printf "PA1T command finished\\n"')


def identity(pid):
    try:
        fields = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'state': fields[0], 'parent': int(fields[1]),
                'group': int(fields[2]), 'session': int(fields[3]), 'start_ticks': int(fields[19])}
    except FileNotFoundError:
        return None


def wait_for(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.025)
    return bool(predicate())


def classify(finished, same_alive, history):
    return {'completed': finished == 'finished\n',
            'ended_without_completion': finished is None and not same_alive
            and any(r['content'].get('is_error') is True for r in history),
            # No wait status or termination signal is available for Bash here.
            'terminated': False, 'cleanup_before_observation': False}


def gate_child(fd, port):
    source = HERE.parent / 'claude_control/gate.py'
    spec = importlib.util.spec_from_file_location('pa1c_gate', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.gate(fd, port)


def main(binary, mode):
    assert mode in ('gate', 'interrupt')
    os.umask(0o077)
    for name in ('.oauth_token', '.api_key'):
        try:
            (Path('/home/claude/.claude/remote') / name).lstat()
        except FileNotFoundError:
            continue
        raise RuntimeError('hosted credential path exists; probe refused')
    root = Path(tempfile.mkdtemp(prefix='pa1t-'))
    print('scratch', root, flush=True)
    for part in ('home', 'config', 'cache', 'state', 'data', 'tmp', 'workspace', 'anthropic', 'xdg'):
        (root / part).mkdir(mode=0o700)
    events, output, processes = [], bytearray(), []
    start = time.monotonic()
    done, pulses = threading.Event(), threading.Event()
    master = slave = None
    tool_identity = None
    outcome = {}

    def record(event, **data):
        events.append({'at': round(time.monotonic() - start, 5), 'event': event, **data})
        print(event, json.dumps(data)[:500], flush=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            record('unexpected_get', path=self.path)
            self.send_error(503)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            if self.path != '/v1/messages?beta=true':
                record('unexpected_post', path=self.path)
                self.send_error(503)
                return
            record('model_request', path=self.path,
                   fixture_auth=self.headers.get('Authorization') == 'Bearer PA1T-FABRICATED',
                   bash_available=any(t.get('name') == 'Bash' for t in body.get('tools', [])))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            frames = [
                {'type': 'message_start', 'message': {'id': 'msg_pa1t', 'type': 'message',
                 'role': 'assistant', 'model': body.get('model'), 'content': [],
                 'stop_reason': None, 'stop_sequence': None,
                 'usage': {'input_tokens': 10, 'output_tokens': 0}}},
                {'type': 'content_block_start', 'index': 0, 'content_block': {
                 'type': 'tool_use', 'id': 'tool_pa1t', 'name': 'Bash', 'input': {}}},
                {'type': 'content_block_delta', 'index': 0, 'delta': {
                 'type': 'input_json_delta', 'partial_json': json.dumps({
                     'command': COMMAND, 'description': 'Fixed private fixture markers and four-second sleep',
                     'timeout': 10000})}},
                {'type': 'content_block_stop', 'index': 0},
                {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None},
                 'usage': {'output_tokens': 10}},
                {'type': 'message_stop'},
            ]
            for frame in frames:
                self.wfile.write(('event: ' + frame['type'] + '\ndata: ' + json.dumps(frame) + '\n\n').encode())
            self.wfile.flush()
            record('fixed_tool_sent', command=COMMAND)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    left, right = socket.socketpair()
    gate = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'probe.py'),
        '--gate', str(left.fileno()), str(server.server_port)], pass_fds=(left.fileno(),),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    processes.append(gate)
    left.close()
    try:
        ready = json.loads(gate.stdout.readline())
        gate_port = ready.pop('port')
        record(ready.pop('event'), **ready)

        def read_gate():
            for line in gate.stdout:
                row = json.loads(line)
                row.pop('at', None)
                record(row.pop('event'), **row)
        threading.Thread(target=read_gate, daemon=True).start()

        def pulse():
            while not pulses.is_set():
                right.sendall(b'p')
                pulses.wait(.1)
        heartbeat = threading.Thread(target=pulse, daemon=True)
        heartbeat.start()
        assert wait_for(lambda: any(e['event'] == 'gate_open' for e in events))
        settings = {'hasCompletedOnboarding': True, 'theme': 'dark',
                    'projects': {str(root / 'workspace'): {'hasTrustDialogAccepted': True,
                                 'hasCompletedProjectOnboarding': True}},
                    'oauthAccount': {'accountUuid': 'fixture-tools', 'organizationUuid': 'fixture-org',
                                     'emailAddress': 'tools@example.invalid'}}
        (root / 'config/.claude.json').write_text(json.dumps(settings))
        credential = root / 'config/.credentials.json'
        credential.write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'PA1T-FABRICATED', 'refreshToken': 'PA1T-FABRICATED-REFRESH',
            'expiresAt': 4102444800000, 'scopes': ['user:profile', 'user:inference'],
            'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_5x'}}))
        credential_digest = hashlib.sha256(credential.read_bytes()).hexdigest()
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'),
               'CLAUDE_CONFIG_DIR': str(root / 'config'), 'ANTHROPIC_CONFIG_DIR': str(root / 'anthropic'),
               **{'XDG_' + part.upper() + '_HOME': str(root / part) for part in ('cache', 'state', 'data')},
               'XDG_CONFIG_HOME': str(root / 'xdg'), 'TMPDIR': str(root / 'tmp'),
               'USER': 'fixture', 'LOGNAME': 'fixture', 'TERM': 'xterm-256color', 'LANG': 'C.UTF-8',
               'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
               'ANTHROPIC_BASE_URL': 'http://127.0.0.1:' + str(gate_port),
               'CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY': '1'}
        for args, event in ((['--version'], 'version'), (['auth', 'status'], 'auth_status')):
            result = subprocess.run([binary, *args], env=env, cwd=root / 'workspace',
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, close_fds=True)
            record(event, code=result.returncode, output=result.stdout.strip())
            assert result.returncode == 0
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        helper = ('import os,fcntl,termios,sys; os.setsid(); '
                  'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')
        native = subprocess.Popen([sys.executable, '-I', '-B', '-c', helper, binary,
            '--model', 'claude-sonnet-4-6', '--permission-mode', 'default', '--tools', 'Bash',
            '--settings', '{"permissions":{"allow":["Bash"]}}', 'Fixture local tool'],
            env=env, cwd=root / 'workspace', stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        processes.append(native)

        def read_terminal():
            while not done.is_set():
                if not select.select([master], [], [], .1)[0]:
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                output.extend(data)
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
                if b'\x1b[c' in data:
                    os.write(master, b'\x1b[?1;2c')
        threading.Thread(target=read_terminal, daemon=True).start()
        workspace = root / 'workspace'
        assert wait_for(lambda: (workspace / 'started').exists(), 15), 'fixed native Bash never started'
        tool_pid = int((workspace / 'tool.pid').read_text())
        tool_identity = identity(tool_pid)
        assert tool_identity and tool_identity['state'] != 'Z'
        record('tool_started', marker=(workspace / 'started').read_text(), identity=tool_identity,
               native_alive=native.poll() is None, finished_present=(workspace / 'finished').exists())
        children_path = Path('/proc/' + str(tool_pid) + '/task/' + str(tool_pid) + '/children')
        children = [identity(int(pid)) for pid in children_path.read_text().split()]
        record('tool_children', identities=[child for child in children if child])
        record('pause_requested', mode=mode)
        pulses.set()
        heartbeat.join(timeout=1)
        right.shutdown(socket.SHUT_WR)
        assert wait_for(lambda: any(e['event'] == 'gate_latched' for e in events), 2)
        if mode == 'interrupt':
            os.write(master, b'\x03')
            record('native_interrupt', key='Ctrl-C', identity=identity(tool_pid))
        # Observation ends before any UI cleanup or native exit shortcuts.
        # Missing finished alone never means termination.
        end = time.monotonic() + 5
        seen_finished = seen_gone = False
        while time.monotonic() < end:
            current = identity(tool_pid)
            if not seen_gone and (not current or current['start_ticks'] != tool_identity['start_ticks']
                                  or current['state'] == 'Z'):
                record('tool_shell_gone', identity=current)
                seen_gone = True
            if not seen_finished and (workspace / 'finished').exists():
                record('tool_finished', marker=(workspace / 'finished').read_text(),
                       marker_elapsed_seconds=((workspace / 'finished').stat().st_mtime_ns
                                               - (workspace / 'started').stat().st_mtime_ns) / 1e9)
                seen_finished = True
            time.sleep(.025)
        end_identity = identity(tool_pid)
        finished = (workspace / 'finished').read_text() if (workspace / 'finished').exists() else None
        record('tool_observed', finished=finished, identity=end_identity,
               same_identity_alive=bool(end_identity and end_identity['start_ticks'] == tool_identity['start_ticks']
                                        and end_identity['state'] != 'Z'), native_alive=native.poll() is None,
               children=[identity(child['pid']) for child in children if child])
        history = []
        for path in sorted((root / 'config/projects').rglob('*.jsonl')):
            for line in path.read_text().splitlines():
                row = json.loads(line)
                for content in row.get('message', {}).get('content', []) if isinstance(row.get('message', {}).get('content'), list) else []:
                    if isinstance(content, dict) and content.get('tool_use_id') == 'tool_pa1t':
                        history.append({'content': content, 'toolUseResult': row.get('toolUseResult')})
        record('tool_history', results=history)
        # A new typed prompt is a separate closed-gate admission observation.
        # Cancel native error retries only after the complete local observation.
        os.write(master, b'\x03')
        time.sleep(.3)
        os.write(master, b'\x15Fixture later prompt\r')
        time.sleep(.2)
        os.write(master, b'\x18\x13')
        assert wait_for(lambda: any(e['event'] == 'gate_reject' and e.get('later_prompt') for e in events), 5)
        record('later_prompt', native_alive=native.poll() is None)
        os.write(master, b'\x03')
        time.sleep(.3)
        os.write(master, b'\x0f')
        time.sleep(.3)
        record('history_view', native_alive=native.poll() is None)
        os.write(master, b'\x0f')
        time.sleep(.2)
        for _ in range(5):
            if native.poll() is not None:
                break
            os.write(master, b'\x03')
            time.sleep(.2)
        exited = wait_for(lambda: native.poll() is not None, 4)
        record('native_exit', exited=exited, code=native.poll(),
               termios_restored=termios.tcgetattr(slave) == before)
        assert exited and native.returncode == 0 and termios.tcgetattr(slave) == before
        record('credential_files', unchanged=hashlib.sha256(credential.read_bytes()).hexdigest() == credential_digest)
        same_alive = bool(end_identity and end_identity['start_ticks'] == tool_identity['start_ticks']
                          and end_identity['state'] != 'Z')
        outcome = classify(finished, same_alive, history)
    finally:
        done.set()
        pulses.set()
        right.close()
        for process in reversed(processes):
            if process.poll() is None:
                record('owned_cleanup', pid=process.pid)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=3)
        if tool_identity:
            current = identity(tool_identity['pid'])
            record('final_tool_identity', identity=current)
        server.shutdown()
        server.server_close()
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        data = {'native_tui': True, 'mode': mode, 'command': COMMAND, 'events': events,
                'outcome': outcome, 'terminal': output.decode(errors='replace')}
        text = json.dumps(data, indent=2, ensure_ascii=True).replace(str(root), '<PROBE_ROOT>')
        (HERE / 'evidence' / (mode + '.json')).write_text(text + '\n')


if __name__ == '__main__':
    if sys.argv[1] == '--gate':
        gate_child(int(sys.argv[2]), int(sys.argv[3]))
    else:
        main(str(Path(sys.argv[1]).resolve()), sys.argv[2])
