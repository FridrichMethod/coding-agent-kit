"""Sourced subscription discovery and native UI research, never a production proxy.

The entry point run.py supplies the inherited Linux network guard and timeout.
Only generated credentials and temporary homes are used. The admission fixture
holds or rejects requests; no service beyond loopback is contacted.
"""
import fcntl
import importlib.util
import json
import itertools
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('prior_pc', HERE.parent / 'pc/targeted.py')
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)


def main(binary, mode):
    root = Path(tempfile.mkdtemp(prefix='pc-ui-'))
    print('scratch', root, flush=True)
    for name in ('home', 'codex', 'workspace'):
        (root / name).mkdir(mode=0o700)
    events, processes, clients, servers = [], [], [], []
    state = {'open': True}
    emitted = set()
    request_ids = itertools.count(1)
    done = threading.Event()
    active = {}
    active_lock = threading.Lock()
    start = time.monotonic()
    master = slave = None
    output = bytearray()

    def record(event, **data):
        events.append({'at': round(time.monotonic() - start, 3), 'event': event, **data})
        print(event, json.dumps(data)[:500], flush=True)

    def wait_for(predicate, timeout=6):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(.05)
        return bool(predicate())

    def count(kind='admit', service='gate'):
        return sum(e['event'] == kind and e.get('service') == service for e in events)

    def handler(service):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                record('connect_rejected', destination=self.path)
                self.send_error(503)

            def do_GET(self):
                record('get', service=service, path=self.path,
                       account=self.headers.get('ChatGPT-Account-ID'),
                       websocket=self.headers.get('Upgrade') == 'websocket')
                if self.headers.get('Upgrade') == 'websocket':
                    self.send_error(426)
                    return
                if '/usage' in self.path:
                    body = {'account_id': 'fixture-account-a', 'plan_type': 'pro',
                            'rate_limit': {'allowed': True, 'limit_reached': False,
                                'primary_window': {'used_percent': 25, 'limit_window_seconds': 18000,
                                                   'reset_at': 2000000000, 'reset_after_seconds': 18000},
                                'secondary_window': {'used_percent': 60, 'limit_window_seconds': 604800,
                                                     'reset_at': 2000400000, 'reset_after_seconds': 604800}}}
                elif self.path == '/api/codex/accounts/check':
                    # Codex rust-v0.167.2 backend-client/types.rs and workspace_routing.rs.
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
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or b'{}')
                is_model = self.path.endswith('/responses')
                if not is_model:
                    record('post_other', service=service, path=self.path)
                    self.send_error(503)
                    return
                user_texts = [' '.join(c.get('text', '') for c in item.get('content', [])
                              if isinstance(c, dict)) for item in body.get('input', [])
                              if item.get('role') == 'user']
                marker = next((text for text in reversed(user_texts)
                               if text.startswith('Fixture delegation ')), None)
                request_id = next(request_ids)
                kind = 'title' if any(text.startswith('Generate a concise, single-line task title') for text in user_texts) else marker or 'root'
                admitted = service != 'gate' or state['open']
                record('admit' if admitted else 'reject', service=service, path=self.path,
                       request_id=request_id, work_kind=kind, session_id=self.headers.get('session-id'),
                       account=self.headers.get('ChatGPT-Account-ID'),
                       bearer_matches_fixture=self.headers.get('Authorization') == 'Bearer ' + token,
                       model=body.get('model'), marker=marker, input_summary=[
                           {'role': item.get('role'), 'type': item.get('type'),
                            'text': ' '.join(c.get('text', '') for c in item.get('content', [])
                                             if isinstance(c, dict))[:400]}
                           for item in (body.get('input') or [])],
                       header_names=sorted(self.headers.keys()),
                       tools=[{'name': t.get('name'), 'type': t.get('type'),
                               'spawn_fields': sorted(next((child.get('parameters', {}).get('properties', {})
                                 for child in t.get('tools', []) if child.get('name') == 'spawn_agent'), {}))}
                              for t in body.get('tools', [])])
                if not admitted:
                    self.send_error(503)
                    return
                if mode == 'delegation' and marker in ('Fixture delegation root', 'Fixture delegation child') and marker not in emitted:
                    emitted.add(marker)
                    target = 'Fixture delegation child' if marker.endswith('root') else 'Fixture delegation grandchild'
                    namespace = next(t for t in body['tools'] if t.get('name') == 'multi_agent_v1')
                    spawn = next(t for t in namespace['tools'] if t.get('name') == 'spawn_agent')
                    args = {'message': target}
                    if 'fork_context' in spawn['parameters']['properties']:
                        args['fork_context'] = False
                    record('fabricated_spawn_response', marker=marker, namespace='multi_agent_v1', arguments=args)
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    for event in [
                        {'type': 'response.created', 'response': {'id': 'fixture-' + marker.split()[-1]}},
                        {'type': 'response.output_item.done', 'item': {'type': 'function_call',
                         'call_id': 'spawn-' + marker.split()[-1], 'namespace': 'multi_agent_v1',
                         'name': 'spawn_agent', 'arguments': json.dumps(args)}},
                        {'type': 'response.completed', 'response': {'id': 'fixture-' + marker.split()[-1],
                         'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]:
                        self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
                    self.wfile.flush()
                    return
                with active_lock:
                    active[(service, request_id)] = self.connection
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                try:
                    self.wfile.write(b'data: {"type":"response.created","response":{"id":"fixture","status":"in_progress","output":[]}}\n\n')
                    self.wfile.flush()
                    while not done.wait(.1):
                        self.wfile.write(b': fixture heartbeat\n\n')
                        self.wfile.flush()
                except OSError:
                    record('stream_disconnected', service=service, request_id=request_id)
                finally:
                    with active_lock:
                        active.pop((service, request_id), None)
        return Handler

    now = int(time.time())
    token = prior.jwt({'email': 'fixture@example.invalid', 'sub': 'fixture-user',
                       'iat': now, 'exp': now + 3600,
                       'https://api.openai.com/auth': {'chatgpt_account_id': 'fixture-account-a',
                           'chatgpt_plan_type': 'pro', 'chatgpt_user_id': 'fixture-user'}})
    auth = {'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
            'tokens': {'id_token': token, 'access_token': token,
                       'refresh_token': 'fixture-refresh', 'account_id': 'fixture-account-a'},
            'last_refresh': datetime.now(timezone.utc).isoformat()}
    (root / 'codex/auth.json').write_text(json.dumps(auth))
    try:
        endpoints = {}
        for service in ('gate', 'bypass', 'quota'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(service))
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            endpoints[service] = 'http://127.0.0.1:' + str(server.server_port)
        config = f'''model = "gpt-5.4"
model_provider = "gate"
chatgpt_base_url = "{endpoints['quota']}"
cli_auth_credentials_store = "file"
approval_policy = "never"
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
[model_providers.bypass]
name = "bypass"
base_url = "{endpoints['bypass']}/backend-api/codex"
requires_openai_auth = true
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
        endpoint = endpoints['quota']
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'),
               'CODEX_HOME': str(root / 'codex'), 'CODEX_SQLITE_HOME': str(root / 'codex'),
               'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_CACHE_HOME': str(root / 'cache'),
               'TMPDIR': str(root), 'TERM': 'xterm-256color',
               'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
               **{key: endpoint for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                            'http_proxy', 'https_proxy', 'all_proxy')}}
        sock = root / 'backend.sock'
        with (root / 'backend.stderr').open('wb') as errors:
            backend = subprocess.Popen([binary, 'app-server', '--listen', 'unix://' + str(sock)],
                env=env, cwd=root / 'workspace', stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=errors, start_new_session=True)
        processes.append(backend)
        assert wait_for(sock.exists), 'backend socket missing'
        ctl = prior.Client(sock, record)
        clients.append(ctl)
        result = ctl.request('account/rateLimits/read', {'excludeResetCreditDetails': True})
        record('preflight', result=result, model_requests=count())
        assert 'probe_error' not in result and count() == 0
        if mode == 'ui':
            master, slave = pty.openpty()
            before_termios = termios.tcgetattr(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
            helper = ('import os,fcntl,termios,sys; os.setsid(); '
                      'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')
            ui = subprocess.Popen([sys.executable, '-I', '-B', '-c', helper, binary,
                '--remote', 'unix://' + str(sock), '--no-alt-screen', '-C', str(root / 'workspace'),
                'Fixture native UI prompt'], env=env, cwd=root / 'workspace',
                stdin=slave, stdout=slave, stderr=slave)
            processes.append(ui)
            def read():
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
            threading.Thread(target=read, daemon=True).start()
            ready = wait_for(lambda: count() >= 1 or ui.poll() is not None, 12)
            record('subscription_ui_bootstrap', ready=ready, exit=ui.poll(),
                   model_requests=count(), discovery_error='workspace routing discovery failed' in bytes(output).decode(errors='replace'))
            assert ready and ui.poll() is None and count() >= 1, 'subscription TUI did not dispatch'
            assert wait_for(lambda: any(e.get('work_kind') == 'title' for e in events)), 'native title request absent'
            listing = ctl.request('thread/list', {'limit': 10})
            tid = listing['data'][0]['id']
            history = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})
            turns = history['thread']['turns']
            assert turns, 'UI turn missing'
            state['open'] = False
            ctl.request('turn/interrupt', {'threadId': tid, 'turnId': turns[-1]['id']})
            disconnected = wait_for(lambda: any(e['event'] == 'stream_disconnected' for e in events))
            history = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})
            time.sleep(1)
            record('paused_ui', ui_alive=ui.poll() is None, stream_disconnected=disconnected,
                   turn_status=history['thread']['turns'][-1]['status'],
                   history_available='probe_error' not in history, admitted=count(),
                   active_request_ids=[key[1] for key in active])
            assert ui.poll() is None and disconnected
            assert history['thread']['turns'][-1]['status'] == 'interrupted'
            # The observer sends only the native exit shortcut, not a continuation key.
            os.write(master, b'\x03')
            time.sleep(.2)
            os.write(master, b'\x03')
            exited = wait_for(lambda: ui.poll() is not None, 4)
            record('ui_exit', exited=exited, exit=ui.poll(),
                   termios_restored=termios.tcgetattr(slave) == before_termios)
            assert exited and termios.tcgetattr(slave) == before_termios
            return
        if mode == 'delegation':
            thread = ctl.request('thread/start', {'cwd': str(root / 'workspace')})
            tid = thread['thread']['id']
            turn = ctl.request('turn/start', {'threadId': tid,
                'input': [{'type': 'text', 'text': 'Fixture delegation root'}]})
            reached = wait_for(lambda: any(e['event'] == 'admit' and e.get('marker') == 'Fixture delegation grandchild' for e in events), 12)
            history = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})
            root_items = history['thread']['turns'][0]['items']
            spawn = next(item for item in root_items if item['type'] == 'collabAgentToolCall')
            child_id = spawn['receiverThreadIds'][0]
            child = ctl.request('thread/read', {'threadId': child_id, 'includeTurns': True})
            child_spawn = next(item for item in child['thread']['turns'][0]['items'] if item['type'] == 'collabAgentToolCall')
            grandchild_id = child_spawn['receiverThreadIds'][0]
            grandchild = ctl.request('thread/read', {'threadId': grandchild_id, 'includeTurns': True})
            record('native_delegation', grandchild_request_observed=reached,
                   root_id=tid, child_id=child_id, grandchild_id=grandchild_id,
                   root_spawn=spawn, child_spawn=child_spawn)
            assert reached, 'actual native grandchild request absent'
            # Native children retain the root session-id header; graph identity comes from history.
            assert len({tid, child_id, grandchild_id}) == 3
            for marker in ('Fixture delegation root', 'Fixture delegation child', 'Fixture delegation grandchild'):
                assert any(e['event'] == 'admit' and e.get('session_id') == tid and e.get('marker') == marker for e in events)
            state['open'] = False
            ctl.request('turn/interrupt', {'threadId': tid, 'turnId': turn['turn']['id']})
            time.sleep(2)
            record('delegation_after_root_interrupt', active_request_ids=[key[1] for key in active])
            for thread_data in (child, grandchild):
                child_thread = thread_data['thread']
                ctl.request('turn/interrupt', {'threadId': child_thread['id'],
                    'turnId': child_thread['turns'][-1]['id']})
            all_disconnected = wait_for(lambda: not active)
            record('delegation_after_individual_interrupts', all_disconnected=all_disconnected)
            assert all_disconnected
            return
        raise ValueError('unknown mode')
    finally:
        done.set()
        for client in clients:
            client.close()
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        if master is not None:
            os.close(master)
        if slave is not None:
            os.close(slave)
        for server in servers:
            server.shutdown()
            server.server_close()
        data = {'mode': mode, 'events': events, 'terminal': bytes(output).decode(errors='replace'),
                'backend_stderr': (root / 'backend.stderr').read_text(errors='replace')
                if (root / 'backend.stderr').exists() else ''}
        serialized = json.dumps(data, indent=2, ensure_ascii=True).replace(str(root), '<PROBE_ROOT>')
        for service, endpoint in locals().get('endpoints', {}).items():
            serialized = serialized.replace(endpoint, '<' + service.upper() + '_URL>')
        (HERE / 'evidence' / (mode + '.json')).write_text(serialized + '\n')


if __name__ == '__main__':
    main(str(Path(sys.argv[1]).resolve()), sys.argv[2])
