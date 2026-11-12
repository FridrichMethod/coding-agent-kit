"""Contained native routing research, not a production forwarding proxy.

The entry point run.py supplies the inherited Linux network guard and timeout.
Only generated credentials and temporary homes are used. The admission fixture
holds or rejects requests; no service beyond loopback is contacted.
"""
import fcntl
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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('prior_pc', HERE.parent / 'pc/targeted.py')
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)


def main(binary, mode):
    root = Path(tempfile.mkdtemp(prefix='pc-proxy-'))
    print('scratch', root, flush=True)
    for name in ('home', 'codex', 'workspace'):
        (root / name).mkdir(mode=0o700)
    events, processes, clients, servers = [], [], [], []
    state = {'open': True, 'unauthorized': False}
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
                admitted = service != 'gate' or state['open']
                record('admit' if admitted else 'reject', service=service, path=self.path,
                       account=self.headers.get('ChatGPT-Account-ID'),
                       bearer_matches_fixture=self.headers.get('Authorization') == 'Bearer ' + token,
                       model=body.get('model'), input_summary=[
                           {'role': item.get('role'), 'type': item.get('type'),
                            'text': ' '.join(c.get('text', '') for c in item.get('content', [])
                                             if isinstance(c, dict))[:400]}
                           for item in (body.get('input') or [])],
                       header_names=sorted(self.headers.keys()))
                if not admitted or state['unauthorized']:
                    self.send_error(401 if state['unauthorized'] else 503)
                    return
                request_id = count(service=service)
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
supports_websockets = {str(mode == 'websocket').lower()}
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
[features]
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
            exited = wait_for(lambda: ui.poll() is not None, 12)
            terminal = bytes(output).decode(errors='replace')
            record('subscription_ui_bootstrap', exited=exited, exit=ui.poll(),
                   model_requests=count(), discovery_error='workspace routing discovery failed' in terminal,
                   termios_restored=termios.tcgetattr(slave) == before_termios)
            assert exited and 'workspace routing discovery failed' in terminal and count() == 0
            return
        thread = ctl.request('thread/start', {'cwd': str(root / 'workspace')})
        tid = thread['thread']['id']
        if mode == 'refresh':
            state['unauthorized'] = True
        result = ctl.request('turn/start', {'threadId': tid,
            'input': [{'type': 'text', 'text': 'Fixture initial prompt'}]})
        assert wait_for(lambda: count() >= 1, 12), 'subscription model route not observed'
        record('subscription_route', admitted=count())
        if mode in ('refresh', 'websocket'):
            time.sleep(3)
            return
        turn = result['turn']['id']
        ctl.request('thread/goal/set', {'threadId': tid, 'objective': 'Fixture goal',
                                      'status': 'active', 'origin': 'user'})
        ctl.request('thread/queue/add', {'threadId': tid, 'clientUserMessageId': 'pc2-old',
                                        'input': [{'type': 'text', 'text': 'Fixture old queued prompt'}]})
        state['open'] = False
        record('gate_closed', admitted=count())
        if mode == 'loss':
            servers[0].shutdown()
            servers[0].server_close()
            with active_lock:
                remaining = list(active.items())
                for (service, request_id), connection in remaining:
                    connection.shutdown(socket.SHUT_RDWR)
                    connection.close()
                    record('fixture_closed_transport', service=service, request_id=request_id)
            record('gate_listener_lost', closed_streams=len(remaining))
        ctl.request('turn/interrupt', {'threadId': tid, 'turnId': turn})
        ctl.close()
        clients.remove(ctl)
        record('controller_lost')
        driver = prior.Client(sock, record)
        clients.append(driver)
        queued = driver.request('thread/queue/start', {'threadId': tid})
        if mode == 'loss':
            # Native transport retries can outlive provider request_max_retries.
            deadline = time.monotonic() + 15
            while True:
                current = driver.request('thread/read', {'threadId': tid, 'includeTurns': True})
                matching = [t for t in current['thread']['turns'] if t['id'] == queued['turn']['id']]
                latest = matching[0] if matching else {'status': 'notYetVisible'}
                if latest['status'] not in ('notYetVisible', 'inProgress') or time.monotonic() >= deadline:
                    break
                time.sleep(.5)
            record('queue_after_listener_loss', admitted=count(), status=latest['status'],
                   error=latest.get('error'))
            assert latest['status'] in ('failed', 'inProgress') and count() == 1
            result = driver.request('account/rateLimits/read', {'excludeResetCreditDetails': True})
            assert 'probe_error' not in result
            record('quota_after_listener_loss', admitted=count(), quota_available=True)
            return
        assert wait_for(lambda: count('reject') >= 1), 'queue did not reach closed gate'
        record('queue_blocked', admitted=count(), rejected=count('reject'))
        time.sleep(.5)
        driver.request('thread/resume', {'threadId': tid, 'modelProvider': 'bypass'})
        driver.request('turn/start', {'threadId': tid,
            'input': [{'type': 'text', 'text': 'Fixture provider change while gate remains closed'}]})
        record('provider_change', bypass_observed=wait_for(lambda: count(service='bypass') >= 1),
               gate_admitted=count(), bypass_admitted=count(service='bypass'))
        other = driver.request('thread/start', {'cwd': str(root / 'workspace'), 'modelProvider': 'bypass'})
        driver.request('turn/start', {'threadId': other['thread']['id'],
            'input': [{'type': 'text', 'text': 'Fixture new thread alternate provider'}]})
        observed = wait_for(lambda: count(service='bypass') >= 1)
        record('new_thread_provider_bypass', observed=observed, gate_admitted=count(),
               bypass_admitted=count(service='bypass'))
        # A provider mismatch refusal is evidence, not a failed harness.
        assert not observed, 'alternate-provider dispatch requires re-evaluating the recorded boundary'
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
