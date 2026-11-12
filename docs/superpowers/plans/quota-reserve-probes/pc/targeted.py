"""Native research only. Invoke under network_guard.py with an outer timeout.

Uses an empty temporary HOME, a fabricated auth file, and a loopback fake service.
No credentials are copied from the caller. No native model service is contacted.
"""
import base64
from datetime import datetime, timezone
import fcntl
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
REPO = HERE.parents[4]
spec = importlib.util.spec_from_file_location('pc_handoff', REPO / 'lib/codex-as/handoff.py')
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


class Client(handoff.Client):
    def __init__(self, path, record):
        self.timeout = 5
        self.deadline = time.monotonic() + 5
        self.buffer = bytearray()
        self.next_id = 0
        self.record = record
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n'
                           'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                           'Sec-WebSocket-Key: ' + key + '\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buffer:
            self.buffer.extend(self._recv())
        head, _, rest = self.buffer.partition(b'\r\n\r\n')
        self.buffer = bytearray(rest)
        assert b'101 Switching Protocols' in head
        self.request('initialize', {'clientInfo': {'name': 'pc-native-research', 'version': '1'},
                                   'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized', 'params': {}})

    def receive(self):
        message = super().receive()
        if "error" in message:
            self.record("rpc_error_wire", message=message)
        return message

    def request(self, method, params, timeout=None):
        try:
            result = super().request(method, params, timeout)
        except Exception as error:
            self.record('rpc', method=method, params=params, error=str(error))
            return {'probe_error': str(error)}
        self.record('rpc', method=method, params=params, result=result)
        return result


def jwt(payload):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    return encode({'alg': 'none'}) + '.' + encode(payload) + '.fixture-signature'


def main(binary, mode):
    root = Path(tempfile.mkdtemp(prefix='pc-native-'))
    print('scratch', root, flush=True)
    for name in ('home', 'codex', 'workspace'):
        (root / name).mkdir(mode=0o700)
    events, requests, processes, clients = [], [], [], []
    start = time.monotonic()
    done = threading.Event()
    output = bytearray()
    master = slave = None
    server = None
    backend_error = None
    def record(event, **data):
        events.append({'at': round(time.monotonic() - start, 3), 'event': event, **data})
        print(event, json.dumps(data)[:500], flush=True)
    quota = {'fractional': False, 'fail': False, 'account': 'fixture-account-a'}
    class Handler(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            requests.append({'method': 'CONNECT', 'path': self.path})
            self.send_error(503)
        def do_GET(self):
            requests.append({'method': 'GET', 'path': self.path,
                             'account': self.headers.get('ChatGPT-Account-ID')})
            if '/usage' not in self.path or quota['fail']:
                self.send_error(503)
                return
            body = {'account_id': quota['account'], 'plan_type': 'pro',
                    'rate_limit': {'allowed': True, 'limit_reached': False,
                       'primary_window': {'used_percent': 25.5 if quota['fractional'] else 25,
                                          'limit_window_seconds': 18000, 'reset_at': 2000000000, 'reset_after_seconds': 18000},
                       'secondary_window': {'used_percent': 60, 'limit_window_seconds': 604800,
                                            'reset_at': 2000400000, 'reset_after_seconds': 604800}}}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if not self.path.endswith('/responses'):
                self.send_error(503)
                return
            body = json.loads(data)
            requests.append({'method': 'POST', 'path': self.path,
                             'input': body.get('input'), 'model': body.get('model')})
            record('model_request', count=count())
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            try:
                self.wfile.write(b'event: response.created\ndata: {"type":"response.created",'
                                 b'"response":{"id":"resp_fixture","object":"response",'
                                 b'"status":"in_progress","output":[]}}\n\n')
                self.wfile.flush()
                while not done.wait(.1):
                    self.wfile.write(b': fixture heartbeat\n\n')
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                record('stream_disconnected')
        def log_message(self, *args):
            pass
    def count():
        return sum(r['method'] == 'POST' and r['path'].endswith('/responses') for r in requests)
    def wait_for(predicate, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(.05)
        return bool(predicate())
    try:
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        endpoint = 'http://127.0.0.1:' + str(server.server_port)
        config = f'''model = "gpt-5.4"
model_provider = "offline_fixture"
chatgpt_base_url = "{endpoint}"
cli_auth_credentials_store = "file"
approval_policy = "never"
web_search = "disabled"
check_for_update_on_startup = false
[model_providers.offline_fixture]
name = "offline_fixture"
base_url = "{endpoint}/v1"
env_key = "CODEX_NATIVE_TEST_KEY"
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
        if mode == 'quota':
            config = config.replace('model_provider = "offline_fixture"', 'model_provider = "openai"')
        (root / 'codex/config.toml').write_text(config)
        now = int(time.time())
        claims = {'chatgpt_account_id': 'fixture-account-a', 'chatgpt_plan_type': 'pro',
                  'chatgpt_user_id': 'fixture-user'}
        token = jwt({'email': 'fixture@example.invalid', 'sub': 'fixture-user',
                     'iat': now, 'exp': now + 3600, 'https://api.openai.com/auth': claims})
        (root / 'codex/auth.json').write_text(json.dumps({'auth_mode': 'chatgpt',
            'OPENAI_API_KEY': None, 'tokens': {'id_token': token, 'access_token': token,
            'refresh_token': 'fixture-refresh', 'account_id': 'fixture-account-a'},
            'last_refresh': datetime.now(timezone.utc).isoformat()}))
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'),
               'CODEX_HOME': str(root / 'codex'), 'CODEX_SQLITE_HOME': str(root / 'codex'),
               'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_CACHE_HOME': str(root / 'cache'),
               'TMPDIR': str(root), 'TERM': 'xterm-256color',
               'CODEX_NATIVE_TEST_KEY': 'fictional-offline-test-key',
               'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
               **{key: endpoint for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                            'http_proxy', 'https_proxy', 'all_proxy')}}
        backend_error = (root / 'backend.stderr').open('wb')
        sock = root / 'backend.sock'
        backend = subprocess.Popen([binary, 'app-server', '--listen', 'unix://' + str(sock)],
            env=env, cwd=root / 'workspace', stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=backend_error, start_new_session=True)
        processes.append(backend)
        assert wait_for(sock.exists), 'backend socket missing'
        ctl = Client(sock, record)
        clients.append(ctl)
        if mode == 'quota':
            record('startup', model_requests=count())
            ctl.request('account/read', {'refreshToken': False})
            for label in ('first', 'unchanged', 'fractional', 'wrong_account', 'failed'):
                quota['fractional'] = label == 'fractional'
                quota['account'] = 'fixture-account-b' if label == 'wrong_account' else 'fixture-account-a'
                quota['fail'] = label == 'failed'
                before = len(requests)
                result = ctl.request('account/rateLimits/read', {'excludeResetCreditDetails': True})
                record('quota_case', label=label, requests_added=len(requests) - before,
                       model_requests=count(), result=result)
            return
        master, slave = pty.openpty()
        before_termios = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        # Setup in a single-threaded helper after exec, never preexec_fn.
        helper = ('import os,fcntl,termios,sys; os.setsid(); '
                  'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')
        ui = subprocess.Popen([sys.executable, '-I', '-B', '-c', helper, binary,
            '--remote', 'unix://' + str(sock), '--no-alt-screen', '-C', str(root / 'workspace'),
            'Hold this fabricated request.'], env=env, cwd=root / 'workspace',
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
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        assert wait_for(lambda: count() == 1, 10), 'initial model request absent'
        tid = ctl.request('thread/loaded/list', {})['data'][0]
        active = ctl.request('thread/read', {'threadId': tid, 'includeTurns': True})['thread']['turns'][-1]['id']
        ctl.request('thread/goal/set', {'threadId': tid, 'objective': 'Fabricated held goal',
                                      'status': 'active', 'origin': 'user'})
        ctl.request('thread/goal/set', {'threadId': tid, 'status': 'paused', 'origin': 'user'})
        ctl.request('thread/queue/add', {'threadId': tid, 'clientUserMessageId': 'fixture-queue',
                                        'input': [{'type': 'text', 'text': 'Old queued fixture prompt'}]})
        ctl.request('turn/interrupt', {'threadId': tid, 'turnId': active})
        time.sleep(.5)
        record('paused_goal_after_interrupt', model_requests=count(), ui_alive=ui.poll() is None,
               interruption_banner=b'Conversation interrupted' in output)
        ctl.request('thread/goal/get', {'threadId': tid})
        ctl.close()
        clients.remove(ctl)
        before = count()
        record('controller_disconnected', model_requests=before)
        time.sleep(.8)
        record('idle_after_disconnect', model_requests=count())
        # Queue dispatch is an RPC test, not evidence of a human continuation.
        driver = Client(sock, record)
        clients.append(driver)
        driver.request('thread/queue/start', {'threadId': tid})
        dispatched = wait_for(lambda: count() > before)
        record('queued_start_while_goal_paused', dispatched=dispatched, model_requests=count())
        current = driver.request('thread/read', {'threadId': tid, 'includeTurns': True})['thread']['turns'][-1]
        if current['status'] == 'inProgress':
            driver.request('turn/interrupt', {'threadId': tid, 'turnId': current['id']})
        time.sleep(.4)
        # Separate characters and Enter so the native paste detector does not swallow Enter.
        begin = len(output)
        os.write(master, b'/status')
        time.sleep(.5)
        os.write(master, b'\r')
        time.sleep(.5)
        record('status_ui', ui_alive=ui.poll() is None,
               output=bytes(output[begin:]).decode(errors='replace'), model_requests=count())
        driver.request('thread/resume', {'threadId': tid})
        driver.request('thread/goal/get', {'threadId': tid})
        before = count()
        driver.request('turn/start', {'threadId': tid,
            'input': [{'type': 'text', 'text': 'Programmatic prompt, not a verified human resume'}]})
        record('programmatic_prompt_after_resume', dispatched=wait_for(lambda: count() > before),
               model_requests=count())
        backend.kill()
        backend.wait(timeout=5)
        time.sleep(.8)
        record('backend_failure', ui_exit=ui.poll(), model_requests=count())
        if ui.poll() is None:
            os.write(master, b'\x03')
            time.sleep(.3)
            os.write(master, b'\x03')
            wait_for(lambda: ui.poll() is not None, 3)
        record('terminal_after_exit', ui_exit=ui.poll(),
               termios_restored=termios.tcgetattr(slave) == before_termios)
    finally:
        done.set()
        for client in clients:
            client.close()
        for process in reversed(processes):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=2)
        if master is not None:
            os.close(master)
        if slave is not None:
            os.close(slave)
        if backend_error:
            backend_error.close()
        if server:
            server.shutdown()
            server.server_close()
        def clean(value):
            return value.replace(str(root), '<PROBE_ROOT>').replace(str(REPO), '<WORKTREE>')
        data = {'mode': mode, 'events': events, 'requests': requests,
                'terminal': bytes(output).decode(errors='replace'),
                'backend_stderr': (root / 'backend.stderr').read_text(errors='replace')
                if (root / 'backend.stderr').exists() else ''}
        (HERE / 'evidence' / (mode + '.json')).write_text(clean(json.dumps(data, indent=2, ensure_ascii=True)) + '\n')


if __name__ == '__main__':
    main(str(Path(sys.argv[1]).resolve()), sys.argv[2])
