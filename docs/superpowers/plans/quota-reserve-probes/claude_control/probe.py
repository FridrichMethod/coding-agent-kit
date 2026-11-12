"""Fabricated Claude native TUI probe. Invoke through run.py, never directly."""
import fcntl
import hashlib
import http.client
import importlib.util
import itertools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import re
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
import tty

HERE = Path(__file__).resolve().parent
SHARED_SHA256 = {
    'control': '8b98eee39bd83b7769356db3fb49bcfefe8423da28cb4942430a9bad5e218dad',
    'gate': '045fafde6a9472e044fb747a8a81fabc40a31df79710216ee6d47c6912a6e8cb',
}


def main(binary, mode, shared=None):
    os.umask(0o077)
    for name in ('.oauth_token', '.api_key'):
        try:
            (Path('/home/claude/.claude/remote') / name).lstat()
        except FileNotFoundError:
            continue
        raise RuntimeError('hosted credential path exists; probe refused')
    root = Path(tempfile.mkdtemp(prefix='pa1c-'))
    print('scratch', root, flush=True)
    events, processes, terminals, servers = [], [], [], []
    done = threading.Event()
    emitted = set()
    active = {}
    credential_hashes = {}
    request_ids = itertools.count(1)
    continuation_gate = None
    control_pty = None
    outer_output = bytearray()
    quota = {'remaining': 80, 'sequence': 0}
    start = time.monotonic()

    def record(event, **data):
        when = data.pop('when', time.monotonic())
        events.append({'at': round(when - start, 4), 'event': event, **data})
        print(event, json.dumps(data)[:300], flush=True)

    def wait_for(predicate, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(.05)
        return bool(predicate())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/fixture-quota':
                quota['sequence'] += 1
                body = {'account': 'fixture-owned', 'remaining': [quota['remaining'], quota['remaining']],
                        'sequence': quota['sequence'], 'at': time.monotonic()}
                record('fake_quota_request', account=body['account'], remaining=body['remaining'],
                       sequence=body['sequence'], server_at=body['at'])
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            record('other_get', path=self.path)
            self.send_error(503)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            if self.path.split('?')[0] != '/v1/messages':
                record('other_post', path=self.path)
                self.send_error(503)
                return
            messages = body.get('messages', [])
            texts = [c.get('text', '') for m in messages if m.get('role') == 'user'
                     for c in m.get('content', []) if isinstance(c, dict) and c.get('type') == 'text']
            marker = next((s for s in texts if s in ('Fixture root', 'Fixture child', 'Fixture grandchild', 'Fixture unrelated')), '')
            kind = marker.removeprefix('Fixture ') or 'auxiliary'
            tools = body.get('tools', [])
            agent = next((t for t in tools if t.get('name') in ('Agent', 'Task')), None)
            ident = next(request_ids)
            tool_results = [{'tool_use_id': c.get('tool_use_id'),
                             'agent_ids': re.findall(r'agentId: ([a-zA-Z0-9_-]+)', json.dumps(c)),
                             'background': 'Async agent launched successfully' in json.dumps(c)}
                            for m in messages if m.get('role') == 'user'
                            for c in m.get('content', []) if isinstance(c, dict) and c.get('type') == 'tool_result']
            record('model_request', request_id=ident, kind=kind, path=self.path,
                   gate_id=self.headers.get('X-Fixture-Request'),
                   model=body.get('model'), agent_fields=sorted((agent or {}).get('input_schema', {}).get('properties', {})),
                   fixture_auth=self.headers.get('Authorization') == 'Bearer PA1C-FABRICATED',
                   tool_results=tool_results,
                   manually_continued='Fixture manually continued' in json.dumps(messages))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            def send(value):
                self.wfile.write(('event: ' + value['type'] + '\ndata: ' + json.dumps(value) + '\n\n').encode())
                self.wfile.flush()

            try:
                send({'type': 'message_start', 'message': {'id': 'msg_fixture_' + str(ident),
                      'type': 'message', 'role': 'assistant', 'model': body.get('model'),
                      'content': [], 'stop_reason': None, 'stop_sequence': None,
                      'usage': {'input_tokens': 10, 'output_tokens': 0}}})
                if mode not in ('bootstrap', 'oauth', 'fullauth') and kind in ('root', 'child') and kind not in emitted and agent:
                    emitted.add(kind)
                    target = 'child' if kind == 'root' else 'grandchild'
                    args = {'description': 'Fixture ' + target, 'prompt': 'Fixture ' + target,
                            'subagent_type': 'general-purpose'}
                    record('fabricated_tool', kind=kind, name=agent['name'], arguments=args)
                    send({'type': 'content_block_start', 'index': 0, 'content_block': {
                        'type': 'tool_use', 'id': 'tool_fixture_' + kind, 'name': agent['name'], 'input': {}}})
                    send({'type': 'content_block_delta', 'index': 0, 'delta': {
                        'type': 'input_json_delta', 'partial_json': json.dumps(args)}})
                    send({'type': 'content_block_stop', 'index': 0})
                    send({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None},
                          'usage': {'output_tokens': 10}})
                    send({'type': 'message_stop'})
                    return
                send({'type': 'content_block_start', 'index': 0,
                      'content_block': {'type': 'text', 'text': ''}})
                send({'type': 'content_block_delta', 'index': 0,
                      'delta': {'type': 'text_delta', 'text': 'Fixture held stream. '}})
                active[ident] = kind
                while not done.wait(.1):
                    send({'type': 'ping'})
            except OSError:
                record('stream_disconnected', request_id=ident, kind=kind)
            finally:
                active.pop(ident, None)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    servers.append(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = 'http://127.0.0.1:' + str(server.server_port)
    gate_endpoint = endpoint
    def check_fake_quota():
        previous = quota['sequence']
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
        began = time.monotonic()
        try:
            connection.request('GET', '/fixture-quota')
            response = connection.getresponse()
            data = json.loads(response.read(4096))
            passed = (response.status == 200 and data['account'] == 'fixture-owned'
                      and data['sequence'] > previous and began <= data['at'] <= time.monotonic()
                      and len(data['remaining']) == 2 and all(v > 20 for v in data['remaining']))
            record('fake_quota_check', passed=passed, sequence=data['sequence'])
            return passed
        finally:
            connection.close()

    if mode == 'continuation':
        shared = Path(shared) if shared else HERE.parent / 'pc_continue'
        modules = {}
        for name in ('control', 'gate'):
            source = shared / (name + '.py')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            assert digest == SHARED_SHA256[name], 'shared fixture changed; review before reuse'
            spec = importlib.util.spec_from_file_location('shared_' + name, source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules[name] = module
            record('shared_source', name=name, sha256=digest)
        continuation_gate = modules['gate'].FixtureGate(server.server_port, record, route='/v1/messages?beta=true')
        gate_endpoint = 'http://127.0.0.1:' + str(continuation_gate.port)
        assert check_fake_quota()
        continuation_gate.grant()
        outer_master, outer_slave = pty.openpty()
        tty.setraw(outer_slave)
        control_pty = (outer_master, outer_slave)

        def read_outer():
            while not done.is_set():
                if select.select([outer_master], [], [], .1)[0]:
                    try:
                        outer_output.extend(os.read(outer_master, 65536))
                    except OSError:
                        return
        threading.Thread(target=read_outer, daemon=True).start()
    gate_process = monitor_process = None
    if mode in ('eof', 'monitor_death', 'lease', 'gate_death'):
        left, right = socket.socketpair()
        gate_process = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'gate.py'),
            'gate', str(left.fileno()), str(server.server_port)], pass_fds=(left.fileno(),),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        processes.append(gate_process)
        left.close()
        ready = json.loads(gate_process.stdout.readline())
        record(ready.pop('event'), **ready)
        gate_endpoint = 'http://127.0.0.1:' + str(ready['port'])

        def read_gate():
            for line in gate_process.stdout:
                row = json.loads(line)
                record(row.pop('event'), **row)
        threading.Thread(target=read_gate, daemon=True).start()
        monitor_process = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'gate.py'),
            'monitor', str(right.fileno())], pass_fds=(right.fileno(),), stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
        processes.append(monitor_process)
        right.close()
        assert wait_for(lambda: any(e['event'] == 'gate_open' for e in events))

    def launch(name):
        base = root / name
        for part in ('home', 'config', 'cache', 'state', 'data', 'tmp', 'workspace', 'anthropic', 'xdg'):
            (base / part).mkdir(mode=0o700, parents=True)
        settings = {'hasCompletedOnboarding': True, 'theme': 'dark',
                    'projects': {str(base / 'workspace'): {'hasTrustDialogAccepted': True,
                                'hasCompletedProjectOnboarding': True}}}
        (base / 'config/.claude.json').write_text(json.dumps(settings))
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(base / 'home'),
               'CLAUDE_CONFIG_DIR': str(base / 'config'), 'ANTHROPIC_CONFIG_DIR': str(base / 'anthropic'),
               **{'XDG_' + part.upper() + '_HOME': str(base / part)
                  for part in ('cache', 'state', 'data')}, 'XDG_CONFIG_HOME': str(base / 'xdg'),
               'TMPDIR': str(base / 'tmp'), 'USER': 'fixture', 'LOGNAME': 'fixture',
               'TERM': 'xterm-256color', 'LANG': 'C.UTF-8',
               'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
               'ANTHROPIC_AUTH_TOKEN': 'PA1C-FABRICATED',
               'ANTHROPIC_BASE_URL': endpoint if name == 'unrelated' else gate_endpoint,
               'CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY': '1'}
        if mode == 'oauth':
            env.pop('ANTHROPIC_AUTH_TOKEN')
            env['CLAUDE_CODE_OAUTH_TOKEN'] = 'PA1C-FABRICATED'
        if mode in ('fullauth', 'eof', 'monitor_death', 'lease', 'gate_death', 'continuation'):
            env.pop('ANTHROPIC_AUTH_TOKEN')
            (base / 'config/.credentials.json').write_text(json.dumps({'claudeAiOauth': {
                'accessToken': 'PA1C-FABRICATED', 'refreshToken': 'PA1C-FABRICATED-REFRESH',
                'expiresAt': 4102444800000, 'scopes': ['user:profile', 'user:inference'],
                'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_5x'}}))
            settings['oauthAccount'] = {'accountUuid': 'fixture-' + name,
                'organizationUuid': 'fixture-org-' + name, 'emailAddress': name + '@example.invalid'}
            (base / 'config/.claude.json').write_text(json.dumps(settings))
            credential_hashes[name] = hashlib.sha256((base / 'config/.credentials.json').read_bytes()).hexdigest()
        status = subprocess.run([binary, 'auth', 'status'], env=env, cwd=base / 'workspace',
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, close_fds=True)
        record('auth_status', name=name, exit=status.returncode, data=json.loads(status.stdout))
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        helper = ('import os,fcntl,termios,sys; os.setsid(); '
                  'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', helper, binary,
            '--model', 'claude-sonnet-4-6', '--permission-mode', 'default',
            '--settings', '{"permissions":{"allow":["Agent"]}}', 'Fixture ' + ('unrelated' if name == 'unrelated' else 'root')],
            env=env, cwd=base / 'workspace', stdin=slave, stdout=slave, stderr=slave,
            close_fds=True)
        processes.append(process)
        output = bytearray()
        terminals.append((master, slave, output))

        def read():
            while not done.is_set():
                if not select.select([master], [], [], .1)[0]:
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                output.extend(data)
                if control_pty and name == 'owned':
                    os.write(control_pty[1], data)
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
                if b'\x1b[c' in data:
                    os.write(master, b'\x1b[?1;2c')
        threading.Thread(target=read, daemon=True).start()
        return process, master, slave, output, before

    try:
        native, master, slave, output, before = launch('owned')
        ready = wait_for(lambda: any(e['event'] == 'model_request' for e in events), 15)
        record('bootstrap', ready=ready, native_alive=native.poll() is None)
        assert ready, 'native TUI model request absent'
        if mode not in ('bootstrap', 'oauth', 'fullauth'):
            nested = wait_for(lambda: any(e['event'] == 'model_request' and e.get('kind') == 'grandchild' for e in events), 15)
            record('nested', observed=nested)
            assert nested, 'native grandchild absent'
            assert wait_for(lambda: len(active) >= 3), 'root/child/grandchild streams absent'
        if continuation_gate:
            outer_master, outer_slave = control_pty

            def display(message):
                os.write(outer_slave, ('\r\n' + message + '\r\n').encode())
                record('control_display', message=message)

            control = modules['control'].TerminalControl(outer_slave,
                lambda data: os.write(master, data), display, check_fake_quota, continuation_gate.grant)

            def enter(data):
                os.write(outer_master, data)
                control.pump(.1)

            enter(b'Fixture previously queued prompt\r')
            time.sleep(.2)
            os.write(outer_master, modules['control'].KEY + b'old\r')
            continuation_gate.pause()
            control.pause()
            control.pump(0)
            assert control.challenge is None
            before_count = sum(e['event'] == 'model_request' for e in events)
            quota['remaining'] = 10
            enter(modules['control'].KEY)
            challenge = control.challenge
            enter(b'\x1b[200~' + challenge.encode() + b'\r\x1b[201~')
            assert continuation_gate.grants == 1 and quota['sequence'] == 1
            enter(modules['control'].KEY)
            enter(control.challenge.encode() + b'\r')
            assert continuation_gate.grants == 1 and quota['sequence'] == 2
            quota['remaining'] = 80
            time.sleep(.3)
            enter(b'\x03')
            assert wait_for(lambda: any(e['event'] == 'gate_reject' and e.get('old_queue') for e in events))
            assert sum(e['event'] == 'model_request' for e in events) == before_count
            record('manual_negative_controls', stale_buffer_ignored=True, paste_ignored=True,
                   bad_quota_denied=True, recovery_did_not_grant=True, old_queue_rejected=True)
            enter(modules['control'].KEY)
            enter(control.challenge.encode() + b'\r')
            assert continuation_gate.grants == 2 and quota['sequence'] == 3
            enter(b'\x03')
            time.sleep(.2)
            enter(b'\x15Fixture manually continued\r')
            time.sleep(.2)
            enter(b'\x18\x13')
            assert wait_for(lambda: any(e['event'] == 'model_request' and e.get('manually_continued') for e in events))
            record('manual_continuation', native_alive=native.poll() is None, grants=continuation_gate.grants,
                   fake_quota_only=True, physical_human_verified=False)
            continuation_gate.pause('second_pause')
            control.pause()
            time.sleep(.1)
            record('outer_terminal', native_output_visible=b'Claude Code' in outer_output,
                   notice_visible=b'Paused.' in outer_output, confirmation_visible=b'Type ' in outer_output)
        if gate_process:
            unrelated, _, _, _, _ = launch('unrelated')
            assert wait_for(lambda: 'unrelated' in active.values())
            os.write(master, b'Fixture previously queued\r')
            time.sleep(.3)
            before_ids = sorted(i for i, kind in active.items() if kind != 'unrelated')
            record('loss_trigger', mode=mode, active_ids=before_ids)
            if mode == 'eof':
                monitor_process.stdin.write('eof\n')
                monitor_process.stdin.flush()
            elif mode == 'monitor_death':
                monitor_process.kill()
                monitor_process.wait(timeout=3)
            elif mode == 'lease':
                monitor_process.stdin.write('quiet\n')
                monitor_process.stdin.flush()
            else:
                gate_process.kill()
                gate_process.wait(timeout=3)
                record('gate_exit', code=gate_process.returncode)
            closed = wait_for(lambda: not any(i in active for i in before_ids))
            time.sleep(2)
            record('loss_observed', all_owned_closed=closed,
                   unrelated_alive=unrelated.poll() is None,
                   unrelated_stream_active='unrelated' in active.values(), native_alive=native.poll() is None)
            assert closed and unrelated.poll() is None and 'unrelated' in active.values()
            if mode == 'lease':
                monitor_process.stdin.write('pulse\n')
                monitor_process.stdin.flush()
                assert wait_for(lambda: any(e['event'] == 'heartbeat_after_latch' for e in events))
        os.write(master, b'\x03')
        time.sleep(2)
        record('after_interrupt', native_alive=native.poll() is None, active_kinds=sorted(active.values()))
        if gate_process:
            os.write(master, b'\x03')
            time.sleep(.2)
            os.write(master, b'\x18\x0b')
            time.sleep(.1)
            os.write(master, b'\x18\x0b')
            time.sleep(.3)
            # Ctrl-U clears the draft restored by interruption. Native send-now
            # dispatches the queue even while stop-agent notifications retry.
            os.write(master, b'\x15Fixture later prompt\r')
            time.sleep(.2)
            os.write(master, b'\x18\x13')
            if mode not in ('gate_death', 'continuation'):
                assert wait_for(lambda: any(e['event'] == 'gate_reject' and e.get('later_prompt') for e in events)), 'later native prompt did not reach closed gate'
            time.sleep(.5)
            record('later_prompt_observed', native_alive=native.poll() is None,
                   owned_upstream_count=sum(e['event'] == 'model_request' and e.get('kind') != 'unrelated' for e in events))
            os.write(master, b'\x03')
            time.sleep(.3)
        # Native transcript view is keyboard-driven; its output is retained.
        os.write(master, b'\x0f')
        time.sleep(.3)
        record('history_view', native_alive=native.poll() is None)
        os.write(master, b'\x0f')
        time.sleep(.2)
        if gate_process or continuation_gate:
            os.write(master, b'\x18\x0b')
            time.sleep(.1)
            os.write(master, b'\x18\x0b')
            time.sleep(.3)
        for _ in range(5):
            if native.poll() is not None:
                break
            os.write(master, b'\x03')
            time.sleep(.2)
        exited = wait_for(lambda: native.poll() is not None, 4)
        record('native_exit', exited=exited, code=native.poll(),
               termios_restored=termios.tcgetattr(slave) == before)
        assert exited and native.returncode == 0 and termios.tcgetattr(slave) == before
        record('credential_files', unchanged=all(
            hashlib.sha256((root / name / 'config/.credentials.json').read_bytes()).hexdigest() == value
            for name, value in credential_hashes.items()))
    finally:
        done.set()
        for process in reversed(processes):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        data = {'native_tui': True, 'mode': mode, 'events': events,
                'terminal': [bytes(item[2]).decode(errors='replace') for item in terminals]}
        for master, slave, _ in terminals:
            os.close(master)
            os.close(slave)
        for server in servers:
            server.shutdown()
            server.server_close()
        if continuation_gate:
            continuation_gate.close()
        if control_pty:
            for fd in control_pty:
                os.close(fd)
        text = json.dumps(data, indent=2, ensure_ascii=True).replace(str(root), '<PROBE_ROOT>').replace(endpoint, '<UPSTREAM>').replace(gate_endpoint, '<GATE>')
        (HERE / 'evidence' / ('case-' + mode + '.json')).write_text(text + '\n')


if __name__ == '__main__':
    main(str(Path(sys.argv[1]).resolve()), sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
