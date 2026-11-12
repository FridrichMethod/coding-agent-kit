"""Native PC5 research. Invoke only via run.py's independent syscall guard."""
from datetime import datetime, timezone
import fcntl
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prior = load('pc_rpc', HERE.parent / 'pc/targeted.py')
control = load('pc_continue_control', HERE / 'control.py')
gate_module = load('pc_continue_gate', HERE / 'gate.py')


def main(binary, mode):
    root = Path(tempfile.mkdtemp(prefix='pc-continue-'))
    print('scratch', root, flush=True)
    for name in ('home', 'codex', 'workspace'):
        (root / name).mkdir(mode=0o700)
    events, processes, servers, clients, threads, fds = [], [], [], [], [], []
    active, spawned = set(), set()
    lock = threading.Lock()
    done = threading.Event()
    output = bytearray()
    visible = bytearray()
    start = time.monotonic()
    quota = {'label': 'passing', 'reads': 0}
    gate = None
    terminal = None
    outer_before = None
    endpoints = {}

    def record(event, **data):
        with lock:
            events.append({'at': round(time.monotonic() - start, 4), 'event': event, **data})
        print(event, flush=True)

    def rows(event):
        with lock:
            return [e.copy() for e in events if e['event'] == event]

    def wait(predicate, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if terminal is not None:
                terminal.pump(0)
            if predicate():
                return True
            time.sleep(.03)
        return bool(predicate())

    token = prior.jwt({'email': 'fixture@example.invalid', 'sub': 'fixture-user',
        'iat': int(time.time()), 'exp': int(time.time()) + 3600,
        'https://api.openai.com/auth': {'chatgpt_account_id': 'fixture-account-a',
            'chatgpt_plan_type': 'pro', 'chatgpt_user_id': 'fixture-user'}})
    auth = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
        'tokens': {'id_token': token, 'access_token': token,
                  'refresh_token': 'fixture-refresh', 'account_id': 'fixture-account-a'},
        'last_refresh': datetime.now(timezone.utc).isoformat()}
    (root / 'codex/auth.json').write_text(json.dumps(auth))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_CONNECT(self):
            record('external_connect_denied', destination=self.path)
            self.send_error(503)

        def do_GET(self):
            if '/usage' in self.path:
                quota['reads'] += 1
                record('quota_http', label=quota['label'])
                if quota['label'] == 'failed':
                    self.send_error(503)
                    return
                body = {'account_id': 'fixture-account-a', 'plan_type': 'pro',
                    'rate_limit': {'allowed': True, 'limit_reached': False,
                        'primary_window': {'used_percent': 80 if quota['label'] == 'five_hour' else 25,
                            'limit_window_seconds': 18000, 'reset_at': 2000000000,
                            'reset_after_seconds': 18000},
                        'secondary_window': {'used_percent': 70 if quota['label'] == 'weekly' else 60,
                            'limit_window_seconds': 604800, 'reset_at': 2000400000,
                            'reset_after_seconds': 604800}}}
            elif self.path == '/api/codex/accounts/check':
                body = {'accounts': [{'id': 'fixture-account-a',
                    'workspace_backend_origin': 'https://fixture.invalid',
                    'account_routing_override': 'NO_CONSTRAINT', 'name': 'Fixture'}]}
            elif '/models' in self.path:
                body = {'models': []}
            else:
                self.send_error(503)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            texts = [' '.join(c.get('text', '') for c in item.get('content', [])
                if isinstance(c, dict)) for item in body.get('input', []) if item.get('role') == 'user']
            marker = next((s for s in reversed(texts) if s.startswith('Fixture delegation ')), None)
            kind = 'title' if any(s.startswith('Generate a concise, single-line task title')
                for s in texts) else marker or 'root'
            ident = int(self.headers.get('X-Fixture-Request', 0))
            record('upstream_request', request_id=ident, kind=kind,
                resumed=any('Fixture manually continued' in s for s in texts))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            call = None
            if mode == 'delegation' and marker in ('Fixture delegation root', 'Fixture delegation child') and marker not in spawned:
                spawned.add(marker)
                namespace = next(t for t in body['tools'] if t.get('name') == 'multi_agent_v1')
                assert any(t.get('name') == 'spawn_agent' for t in namespace['tools'])
                target = 'Fixture delegation child' if marker.endswith('root') else 'Fixture delegation grandchild'
                call = {'namespace': 'multi_agent_v1', 'name': 'spawn_agent',
                    'arguments': json.dumps({'message': target, 'fork_context': False})}
            elif mode.startswith('tool') and kind == 'root' and 'tool' not in spawned:
                spawned.add('tool')
                record('offered_tools', names=[t.get('name') for t in body['tools']])
                command = "python3 -I -B -c 'import pathlib,time; pathlib.Path(\"started\").write_text(\"started\"); time.sleep(4); pathlib.Path(\"finished\").write_text(\"finished\")'"
                call = {'name': 'exec_command', 'arguments': json.dumps({'cmd': command, 'yield_time_ms': 1000})}
            if call:
                for event in [
                    {'type': 'response.created', 'response': {'id': 'fixture-' + str(ident)}},
                    {'type': 'response.output_item.done', 'item': {'type': 'function_call',
                        'call_id': 'call-' + str(ident), **call}},
                    {'type': 'response.completed', 'response': {'id': 'fixture-' + str(ident),
                        'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]:
                    self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                self.wfile.flush()
                return
            with lock:
                active.add(ident)
            try:
                self.wfile.write(b'data: {"type":"response.created","response":{"id":"fixture","status":"in_progress","output":[]}}\n\n')
                self.wfile.flush()
                while not done.wait(.05):
                    self.wfile.write(b': fixture heartbeat\n\n')
                    self.wfile.flush()
            except OSError:
                record('upstream_disconnected', request_id=ident)
            finally:
                with lock:
                    active.discard(ident)

    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
        endpoints['quota'] = 'http://127.0.0.1:' + str(server.server_port)
        gate = gate_module.FixtureGate(server.server_port, record)
        endpoints['gate'] = 'http://127.0.0.1:' + str(gate.port)
        config = f'''model = "gpt-5.4"
model_provider = "gate"
chatgpt_base_url = "{endpoints['quota']}"
cli_auth_credentials_store = "file"
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"
check_for_update_on_startup = false
[model_providers.gate]
name = "gate"
base_url = "{endpoints['gate']}/backend-api/codex"
requires_openai_auth = true
supports_websockets = false
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
[agents]
max_depth = 2
max_threads = 4
[features]
multi_agent = true
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
[projects."{root / 'workspace'}"]
trust_level = "trusted"
'''
        (root / 'codex/config.toml').write_text(config)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'),
            'CODEX_HOME': str(root / 'codex'), 'CODEX_SQLITE_HOME': str(root / 'codex'),
            'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_CACHE_HOME': str(root / 'cache'),
            'TMPDIR': str(root), 'TERM': 'xterm-256color',
            'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
            **{key: endpoints['quota'] for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                'http_proxy', 'https_proxy', 'all_proxy')}}
        sock = root / 'backend.sock'
        with (root / 'backend.stderr').open('wb') as errors:
            backend = subprocess.Popen([binary, 'app-server', '--listen', 'unix://' + str(sock)],
                env=env, cwd=root / 'workspace', stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=errors, start_new_session=True, close_fds=True)
        processes.append(backend)
        assert wait(sock.exists)
        ctl = prior.Client(sock, record)
        clients.append(ctl)

        def quota_check():
            before = quota['reads']
            result = ctl.request('account/rateLimits/read', {'excludeResetCreditDetails': True})
            # Stale adapter-sample injection: the new native response is
            # deliberately assigned an old receipt time. This tests the
            # freshness gate, not the native server's cache behavior.
            fetched = time.monotonic() - (10 if quota['label'] == 'stale' else 0)
            try:
                windows = {w['windowDurationMins']: w['usedPercent']
                    for w in (result['rateLimits']['primary'], result['rateLimits']['secondary'])}
                allowed = (quota['reads'] > before and time.monotonic() - fetched < 2
                    and 100 - windows[300] > 20 and 100 - windows[10080] > 30)
            except (KeyError, TypeError):
                allowed = False
            record('fresh_check', label=quota['label'], allowed=allowed,
                http_reads=quota['reads'] - before, native=result)
            return allowed

        assert quota_check() and not rows('upstream_request')
        gate.grant()
        master, slave = pty.openpty()
        outer_master, outer_slave = pty.openpty()
        fds.extend((master, slave, outer_master, outer_slave))
        before = termios.tcgetattr(slave)
        outer_before = termios.tcgetattr(outer_slave)
        tty.setraw(outer_slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        helper = ('import os,fcntl,termios,sys; os.setsid(); '
                  'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')
        prompt = 'Fixture delegation root' if mode == 'delegation' else 'Fixture local tool root'
        ui = subprocess.Popen([sys.executable, '-I', '-B', '-c', helper, binary,
            '--remote', 'unix://' + str(sock), '--no-alt-screen', '-C', str(root / 'workspace'), prompt],
            env=env, cwd=root / 'workspace', stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        processes.append(ui)
        def display(message):
            record('control_display', message=message)
            os.write(outer_slave, ('\r\n[quota-research] ' + message + '\r\n').encode())
        terminal = control.TerminalControl(outer_slave, lambda data: os.write(master, data),
            display, quota_check, gate.grant)

        def read_ui():
            while not done.is_set():
                if not select.select([master], [], [], .1)[0]:
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                output.extend(data)
                os.write(outer_slave, data)

        def emulate_terminal():
            while not done.is_set():
                if not select.select([outer_master], [], [], .1)[0]:
                    continue
                try:
                    data = os.read(outer_master, 65536)
                except OSError:
                    return
                visible.extend(data)
                # Emulator replies enter through the same trusted input fd,
                # while native output itself never enters the control parser.
                if b'\x1b[6n' in data:
                    os.write(outer_master, b'\x1b[1;1R')
                if b'\x1b[c' in data:
                    os.write(outer_master, b'\x1b[?1;2c')
        emulator = threading.Thread(target=emulate_terminal, daemon=True)
        emulator.start()
        threads.append(emulator)
        thread = threading.Thread(target=read_ui, daemon=True)
        thread.start()
        threads.append(thread)
        if mode == 'delegation':
            assert wait(lambda: len(active) >= 3, 12), 'default descendant streams absent'
        else:
            assert wait(lambda: (root / 'workspace/started').exists(), 10), 'bounded tool did not start'
        tid = ctl.request('thread/list', {'limit': 10})['data'][0]['id']
        if mode == 'delegation':
            # thread/list order may put a descendant first; root is in UI-loaded list.
            for entry in ctl.request('thread/list', {'limit': 10})['data']:
                history = ctl.request('thread/read', {'threadId': entry['id'], 'includeTurns': True})
                if history['thread'].get('source') == 'cli':
                    tid = entry['id']
                if history['thread']['turns'] and prompt in json.dumps(history['thread']['turns'][0]['items']):
                    tid = entry['id']
                    break
            history = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})
            root_spawn = next(i for t in history['thread']['turns'] for i in t['items']
                              if i['type'] == 'collabAgentToolCall')
            child_id = root_spawn['receiverThreadIds'][0]
            child = ctl.request('thread/read', {'threadId': child_id, 'includeTurns': True})
            child_spawn = next(i for t in child['thread']['turns'] for i in t['items']
                               if i['type'] == 'collabAgentToolCall')
            record('native_delegation', root=tid, child=child_id,
                grandchild=child_spawn['receiverThreadIds'][0], root_spawn=root_spawn, child_spawn=child_spawn)
        queued = ctl.request('thread/queue/add', {'threadId': tid, 'clientUserMessageId': 'pc5-old',
            'input': [{'type': 'text', 'text': 'Fixture previously queued prompt'}]})
        assert 'probe_error' not in queued
        count = len(rows('upstream_request'))
        os.write(outer_master, control.KEY)  # Buffered before the pause epoch.
        gate.pause()
        terminal.pause()
        terminal.pump(.05)
        assert terminal.challenge is None
        record('prebuffer_discarded', challenge_absent=True, grants=gate.grants)
        assert wait(lambda: not active), 'owned streams survived pause'
        if mode == 'tool_interrupt':
            current = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})['thread']['turns'][-1]
            result = ctl.request('turn/interrupt', {'threadId': tid, 'turnId': current['id']})
            record('local_tool_interrupt', result=result)
        if mode.startswith('tool'):
            tool_finished = wait(lambda: (root / 'workspace/finished').exists(), 6)
            record('local_tool_after_gate_pause', finished=tool_finished,
                native_interrupt=mode == 'tool_interrupt', observation_seconds=6)
        if mode == 'tool_interrupt':
            tool_history = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})
            record('interrupted_tool_history', history=tool_history)
            queued_start = ctl.request('thread/queue/start', {'threadId': tid})
            record('explicit_old_queue_start', result=queued_start)
        assert wait(lambda: any(e['old_queue'] for e in rows('gate_reject')), 8), 'old native queue did not hit closed gate'
        recovered = ctl.request('account/rateLimits/read', {'excludeResetCreditDetails': True})
        assert 'rateLimits' in recovered
        assert gate.grants == 1 and len(rows('upstream_request')) == count
        record('quota_recovery_no_grant', grants=gate.grants, upstream_requests=count)

        def send(data):
            os.write(outer_master, data)
            terminal.pump(.1)

        send(control.KEY * 6)
        challenge = terminal.challenge
        assert challenge
        send(b'\x1b[200~' + challenge.encode() + b'\r\x1b[201~')
        assert gate.grants == 1
        record('held_and_paste_no_grant', grants=gate.grants)
        for label in ('failed', 'stale', 'five_hour', 'weekly', 'passing'):
            quota['label'] = label
            send(control.KEY)
            assert terminal.challenge
            send(terminal.challenge.encode() + b'\r')
            record('continuation_case', label=label, grants=gate.grants, paused=terminal.paused)
            assert gate.grants == (2 if label == 'passing' else 1)
        # A successful action grants a new healthy lease. A new native turn is
        # explicit fixture input; it is not represented as physical user input.
        resumed = ctl.request('turn/start', {'threadId': tid,
            'input': [{'type': 'text', 'text': 'Fixture manually continued after confirmation'}]})
        assert 'probe_error' not in resumed
        assert wait(lambda: any(e['resumed'] for e in rows('upstream_request')))
        assert gate.grants == 2
        gate.pause('second_pause')
        terminal.pause()
        send(challenge.encode() + b'\r')
        assert gate.grants == 2
        record('old_confirmation_no_replay', grants=gate.grants)
        # Exercise a documented native read-only command through ordinary keys.
        begin = len(output)
        send(b'\x7f' * 200)
        time.sleep(.2)
        send(b'/status')
        time.sleep(.5)
        send(b'\r')
        time.sleep(.5)
        assert b'Context window:' in output[begin:] and b'Model provider:' in output[begin:]
        record('native_status_ui', output=bytes(output[begin:]).decode(errors='replace'),
            ui_alive=ui.poll() is None, grants=gate.grants)
        # Ordinary Ctrl-C passes through the owner input path. Resize is applied
        # to the inner PTY and delivered only to the owned native TUI group.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 32, 100, 0, 0))
        os.killpg(ui.pid, signal.SIGWINCH)
        record('resize', size=[32, 100], ui_alive=ui.poll() is None)
        history = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})
        record('history_after_pause', available='probe_error' not in history, ui_alive=ui.poll() is None)
        send(b'\x03')
        time.sleep(.2)
        send(b'\x03')
        assert wait(lambda: ui.poll() is not None, 4)
        termios.tcsetattr(outer_slave, termios.TCSANOW, outer_before)
        record('terminal_exit', exit=ui.returncode, native_restored=termios.tcgetattr(slave) == before,
            outer_restored=termios.tcgetattr(outer_slave) == outer_before)
        assert ui.returncode == 0 and termios.tcgetattr(slave) == before
        # Explicit endpoint policy rejects alternate protocols/routes; this is
        # synthetic endpoint coverage, not native voice route discovery.
        for method, route, headers in [('GET', '/backend-api/codex/responses', {'Upgrade': 'websocket'}),
                ('POST', '/voice', {}), ('CONNECT', 'fixture.invalid:443', {})]:
            connection = http.client.HTTPConnection('127.0.0.1', gate.port, timeout=2)
            connection.request(method, route, body=b'', headers=headers)
            response = connection.getresponse()
            response.read()
            record('alternate_route_probe', synthetic=True, method=method, status=response.status)
            assert response.status == 503
            connection.close()
    finally:
        done.set()
        for client in clients:
            client.close()
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            record('owned_process_exit', kind='ui' if process is processes[-1] else 'backend',
                exit=process.returncode)
        if gate:
            gate.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=1)
        if outer_before is not None:
            termios.tcsetattr(outer_slave, termios.TCSANOW, outer_before)
        for fd in fds:
            os.close(fd)
        data = {'mode': mode, 'events': events, 'terminal': output.decode(errors='replace'),
            'visible_terminal': visible.decode(errors='replace'),
            'stderr': {p.name: p.read_text(errors='replace') for p in root.glob('*.stderr')}}
        serialized = json.dumps(data, indent=2, ensure_ascii=True).replace(str(root), '<PROBE_ROOT>')
        for name, endpoint in endpoints.items():
            serialized = serialized.replace(endpoint, '<' + name.upper() + '_URL>')
        (HERE / 'evidence' / (mode + '.json')).write_text(serialized + '\n')


if __name__ == '__main__':
    main(str(Path(sys.argv[1]).resolve()), sys.argv[2])
