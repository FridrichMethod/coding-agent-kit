"""PC4 native loss matrix; run only through run.py's inherited network guard.

Uses fabricated auth, private homes, one owned HTTP relay process and a distinct
fake upstream. The unchanged PC RPC helper and PC3 discovery/SSE shapes are reused.
"""
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
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pc_rpc', HERE.parent / 'pc/targeted.py')
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)


def main(binary, mode):
    root = Path(tempfile.mkdtemp(prefix='pc-loss-'))
    print('scratch', root, flush=True)
    events, processes, clients, servers, threads = [], [], [], [], []
    active, emitted = {}, set()
    lock = threading.Lock()
    done = threading.Event()
    start = time.monotonic()
    output = bytearray()
    master = slave = None
    endpoints = {}

    def record(event, **data):
        at = data.pop('when', time.monotonic()) - start
        with lock:
            events.append({'at': round(at, 4), 'event': event, **data})
        print(event, flush=True)

    def wait_for(predicate, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(.03)
        return bool(predicate())

    def rows(event):
        with lock:
            return [e.copy() for e in events if e['event'] == event]

    def active_ids(service='upstream'):
        with lock:
            return sorted(ident for (name, ident) in active if name == service)

    token = prior.jwt({'email': 'fixture@example.invalid', 'sub': 'fixture-user',
        'iat': int(time.time()), 'exp': int(time.time()) + 3600,
        'https://api.openai.com/auth': {'chatgpt_account_id': 'fixture-account-a',
            'chatgpt_plan_type': 'pro', 'chatgpt_user_id': 'fixture-user'}})
    auth = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
            'tokens': {'id_token': token, 'access_token': token,
                'refresh_token': 'fixture-refresh', 'account_id': 'fixture-account-a'},
            'last_refresh': datetime.now(timezone.utc).isoformat()}

    def handler(service):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                record('connect_rejected', destination=self.path)
                self.send_error(503)

            def do_GET(self):
                if '/usage' in self.path:
                    body = {'account_id': 'fixture-account-a', 'plan_type': 'pro',
                        'rate_limit': {'allowed': True, 'limit_reached': False,
                            'primary_window': {'used_percent': 25, 'limit_window_seconds': 18000,
                                'reset_at': 2000000000, 'reset_after_seconds': 18000},
                            'secondary_window': {'used_percent': 60, 'limit_window_seconds': 604800,
                                'reset_at': 2000400000, 'reset_after_seconds': 604800}}}
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
                if self.path != '/backend-api/codex/responses':
                    self.send_error(503)
                    return
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                texts = [' '.join(c.get('text', '') for c in item.get('content', [])
                                 if isinstance(c, dict)) for item in body.get('input', [])
                         if item.get('role') == 'user']
                marker = next((s for s in reversed(texts) if s.startswith('Fixture delegation ')), None)
                kind = 'title' if any(s.startswith('Generate a concise, single-line task title')
                                     for s in texts) else marker or 'root'
                ident = int(self.headers.get('X-Fixture-Request', '0'))
                record('upstream_request' if service == 'upstream' else 'unrelated_request',
                       request_id=ident, work_kind=kind, session_id=self.headers.get('session-id'),
                       account=self.headers.get('ChatGPT-Account-ID'),
                       bearer_matches_fixture=self.headers.get('Authorization') == 'Bearer ' + token)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                if service == 'upstream' and mode in ('lease', 'monitor_death') and marker in (
                        'Fixture delegation root', 'Fixture delegation child') and marker not in emitted:
                    emitted.add(marker)
                    target = 'Fixture delegation child' if marker.endswith('root') else 'Fixture delegation grandchild'
                    namespace = next(t for t in body['tools'] if t.get('name') == 'multi_agent_v1')
                    spawn = next(t for t in namespace['tools'] if t.get('name') == 'spawn_agent')
                    assert 'fork_context' in spawn['parameters']['properties']
                    for event in [
                        {'type': 'response.created', 'response': {'id': 'fixture-' + marker.split()[-1]}},
                        {'type': 'response.output_item.done', 'item': {'type': 'function_call',
                            'call_id': 'spawn-' + marker.split()[-1], 'namespace': 'multi_agent_v1',
                            'name': 'spawn_agent', 'arguments': json.dumps({'message': target, 'fork_context': False})}},
                        {'type': 'response.completed', 'response': {'id': 'fixture-' + marker.split()[-1],
                            'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]:
                        self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                    self.wfile.flush()
                    return
                with lock:
                    active[(service, ident)] = self.connection
                try:
                    self.wfile.write(b'data: {"type":"response.created","response":{"id":"fixture","status":"in_progress","output":[]}}\n\n')
                    self.wfile.flush()
                    while not done.wait(.05):
                        self.wfile.write(b': fixture heartbeat\n\n')
                        self.wfile.flush()
                except OSError:
                    record('upstream_disconnected' if service == 'upstream' else 'unrelated_disconnected',
                           request_id=ident)
                finally:
                    with lock:
                        active.pop((service, ident), None)
        return Handler

    def launch_native(name, provider):
        for part in ('home', 'codex', 'workspace'):
            (root / name / part).mkdir(mode=0o700, parents=True)
        (root / name / 'codex/auth.json').write_text(json.dumps(auth))
        config = f'''model = "gpt-5.4"
model_provider = "gate"
chatgpt_base_url = "{endpoints['quota']}"
cli_auth_credentials_store = "file"
approval_policy = "never"
web_search = "disabled"
check_for_update_on_startup = false
[model_providers.gate]
name = "gate"
base_url = "{provider}/backend-api/codex"
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
[projects."{root / name / 'workspace'}"]
trust_level = "trusted"
'''
        (root / name / 'codex/config.toml').write_text(config)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / name / 'home'),
            'CODEX_HOME': str(root / name / 'codex'), 'CODEX_SQLITE_HOME': str(root / name / 'codex'),
            'XDG_CONFIG_HOME': str(root / name / 'config'), 'XDG_CACHE_HOME': str(root / name / 'cache'),
            'TMPDIR': str(root), 'TERM': 'xterm-256color',
            'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
            **{key: endpoints['quota'] for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                'http_proxy', 'https_proxy', 'all_proxy')}}
        sock = root / (name + '.sock')
        with (root / (name + '.stderr')).open('wb') as errors:
            process = subprocess.Popen([binary, 'app-server', '--listen', 'unix://' + str(sock)],
                env=env, cwd=root / name / 'workspace', stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True, close_fds=True)
        processes.append(process)
        assert wait_for(sock.exists), 'backend socket absent'
        client = prior.Client(sock, record)
        clients.append(client)
        return process, client, sock, env

    def command(text):
        monitor.stdin.write(text + '\n')
        monitor.stdin.flush()

    def start_turn(client, tid, text):
        result = client.request('turn/start', {'threadId': tid,
            'input': [{'type': 'text', 'text': text}]})
        assert 'probe_error' not in result
        return result['turn']['id']

    try:
        for service in ('upstream', 'unrelated', 'quota'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(service))
            servers.append(server)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
            endpoints[service] = 'http://127.0.0.1:' + str(server.server_port)
        gate_sock, monitor_sock = socket.socketpair()
        with (root / 'gate.stderr').open('wb') as errors:
            gate = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'gate.py'), 'gate',
                str(gate_sock.fileno()), str(servers[0].server_port)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
                pass_fds=(gate_sock.fileno(),), close_fds=True, text=True, start_new_session=True)
        processes.append(gate)
        gate_sock.close()

        def read_gate():
            for line in gate.stdout:
                data = json.loads(line)
                record(data.pop('event'), **data)
        reader = threading.Thread(target=read_gate, daemon=True)
        reader.start()
        threads.append(reader)
        assert wait_for(lambda: rows('gate_ready'))
        endpoints['gate'] = 'http://127.0.0.1:' + str(rows('gate_ready')[0]['port'])
        startup = http.client.HTTPConnection('127.0.0.1', rows('gate_ready')[0]['port'], timeout=2)
        try:
            startup.request('POST', '/backend-api/codex/responses', body=b'{"input": []}')
            response = startup.getresponse()
            response.read()
            record('initial_closed_probe', synthetic=True, status=response.status,
                   upstream_requests=len(rows('upstream_request')))
            assert response.status == 503 and not rows('upstream_request')
        finally:
            startup.close()
        backend, ctl, sock, env = launch_native('owned', endpoints['gate'])
        preflight = ctl.request('account/rateLimits/read', {'excludeResetCreditDetails': True})
        assert preflight['rateLimits']['primary']['usedPercent'] == 25
        assert preflight['rateLimits']['secondary']['usedPercent'] == 60
        record('preflight', result=preflight, model_requests=len(rows('upstream_request')))
        assert not rows('upstream_request')
        # No monitor heartbeat exists until the fabricated native quota read passes.
        with (root / 'monitor.stderr').open('wb') as errors:
            monitor = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'gate.py'), 'monitor',
                str(monitor_sock.fileno())], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=errors, pass_fds=(monitor_sock.fileno(),), close_fds=True,
                text=True, start_new_session=True)
        processes.append(monitor)
        monitor_sock.close()
        assert wait_for(lambda: rows('gate_open'))
        unrelated, other, _, _ = launch_native('unrelated', endpoints['unrelated'])
        other_tid = other.request('thread/start', {'cwd': str(root / 'unrelated/workspace')})['thread']['id']
        start_turn(other, other_tid, 'Fixture unrelated session')
        assert wait_for(lambda: active_ids('unrelated'))

        ui = None
        if mode in ('eof', 'proxy_death'):
            master, slave = pty.openpty()
            before_termios = termios.tcgetattr(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
            helper = ('import os,fcntl,termios,sys; os.setsid(); '
                      'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')
            ui = subprocess.Popen([sys.executable, '-I', '-B', '-c', helper, binary,
                '--remote', 'unix://' + str(sock), '--no-alt-screen', '-C', str(root / 'owned/workspace'),
                'Fixture native loss UI prompt'], env=env, cwd=root / 'owned/workspace',
                stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            processes.append(ui)

            def read_ui():
                while not done.is_set():
                    if not select.select([master], [], [], .1)[0]:
                        continue
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    output.extend(data)
                    if b'\x1b[6n' in data:
                        os.write(master, b'\x1b[1;1R')
                    if b'\x1b[c' in data:
                        os.write(master, b'\x1b[?1;2c')
            thread = threading.Thread(target=read_ui, daemon=True)
            thread.start()
            threads.append(thread)
            assert wait_for(lambda: len(active_ids()) == 2, 12), 'root/title absent'
            assert ui.poll() is None
            tid = ctl.request('thread/list', {'limit': 10})['data'][0]['id']
        else:
            tid = ctl.request('thread/start', {'cwd': str(root / 'owned/workspace')})['thread']['id']
            start_turn(ctl, tid, 'Fixture delegation root')
            assert wait_for(lambda: len(active_ids()) == 3, 12), 'root/child/grandchild absent'
            history = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})
            spawn = next(i for i in history['thread']['turns'][0]['items'] if i['type'] == 'collabAgentToolCall')
            child_id = spawn['receiverThreadIds'][0]
            child = ctl.request('thread/read', {'threadId': child_id, 'includeTurns': True})
            child_spawn = next(i for i in child['thread']['turns'][0]['items'] if i['type'] == 'collabAgentToolCall')
            record('native_delegation', root_id=tid, child_id=child_id,
                grandchild_id=child_spawn['receiverThreadIds'][0], root_spawn=spawn, child_spawn=child_spawn)

        baseline = active_ids()
        count_before = len(rows('upstream_request'))
        record('before_loss', active_ids=baseline, upstream_requests=count_before)
        # Submit old queue before loss; backend owns its prompt, no fresh human action.
        queued = ctl.request('thread/queue/add', {'threadId': tid, 'clientUserMessageId': 'pc4-old',
            'input': [{'type': 'text', 'text': 'Fixture previously queued prompt'}]})
        assert 'probe_error' not in queued
        record('queue_before_loss', result=queued)
        record('loss_trigger', mode=mode)
        if mode == 'eof':
            command('eof')
        elif mode == 'monitor_death':
            monitor.kill()
            monitor.wait(timeout=3)
            record('monitor_exit', exit=monitor.returncode)
        elif mode == 'lease':
            command('quiet')
        else:
            gate.kill()
            gate.wait(timeout=3)
            record('proxy_exit', exit=gate.returncode)
        if mode != 'proxy_death':
            assert wait_for(lambda: rows('gate_latched'))
        assert wait_for(lambda: not active_ids()), 'owned upstream streams survive loss'
        disconnected = sorted(e['request_id'] for e in rows('upstream_disconnected'))
        record('all_owned_closed', closed_ids=disconnected)
        assert disconnected == baseline
        if mode == 'lease':
            command('pulse')
            assert wait_for(lambda: rows('heartbeat_after_latch'))
        recovered = ctl.request('account/rateLimits/read', {'excludeResetCreditDetails': True})
        record('quota_recovery', result=recovered)
        assert 'probe_error' not in recovered
        # Start queued and programmatic work only after loss has been observed.
        queued_start = ctl.request('thread/queue/start', {'threadId': tid})
        record('queued_after_loss', result=queued_start)
        fresh = ctl.request('thread/start', {'cwd': str(root / 'owned/workspace')})['thread']['id']
        fresh_turn = start_turn(ctl, fresh, 'Fixture programmatic continuation after loss')
        time.sleep(3)
        history = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})
        fresh_history = ctl.request('thread/read', {'threadId': fresh, 'includeTurns': True})
        assert 'Fixture previously queued prompt' in json.dumps(history), 'native old queue not consumed'
        record('after_loss', upstream_unchanged=len(rows('upstream_request')) == count_before,
            history_available='probe_error' not in history, ui_alive=ui.poll() is None if ui else None,
            root_status=history.get('thread', {}).get('turns', [{}])[-1].get('status'),
            programmatic_turn=fresh_turn, programmatic_history=fresh_history,
            observation_seconds=3, monitor_alive=monitor.poll() is None)
        assert len(rows('upstream_request')) == count_before
        if mode != 'proxy_death':
            assert rows('gate_reject')
        other_history = other.request('thread/read', {'threadId': other_tid, 'includeTurns': True})
        record('unrelated', alive=unrelated.poll() is None, stream_active=bool(active_ids('unrelated')),
               turn_status=other_history['thread']['turns'][-1]['status'])
        assert unrelated.poll() is None and active_ids('unrelated')
        if ui:
            assert ui.poll() is None
            os.write(master, b'\x03')
            time.sleep(.2)
            os.write(master, b'\x03')
            assert wait_for(lambda: ui.poll() is not None, 4)
            record('ui_exit', exit=ui.returncode, termios_restored=termios.tcgetattr(slave) == before_termios)
            assert ui.returncode == 0 and termios.tcgetattr(slave) == before_termios
    finally:
        done.set()
        for client in clients:
            client.close()
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=1)
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        data = {'mode': mode, 'events': events, 'terminal': bytes(output).decode(errors='replace'),
            'stderr': {path.name: path.read_text(errors='replace') for path in root.glob('*.stderr')}}
        serialized = json.dumps(data, indent=2, ensure_ascii=True).replace(str(root), '<PROBE_ROOT>')
        for service, endpoint in endpoints.items():
            serialized = serialized.replace(endpoint, '<' + service.upper() + '_URL>')
        (HERE / 'evidence' / (mode + '.json')).write_text(serialized + '\n')


if __name__ == '__main__':
    main(str(Path(sys.argv[1]).resolve()), sys.argv[2])
