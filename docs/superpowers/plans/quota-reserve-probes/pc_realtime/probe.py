"""PC6 core-routing research; run.py installs the unchanged PC network guard."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prior = load('pc6_prior', HERE.parent / 'pc/targeted.py')
gate_module = load('pc6_gate', HERE.parent / 'pc_continue/gate.py')
check = load('pc6_check', HERE / 'check_evidence.py')


def sanitize_evidence(raw, root, binary, endpoints, worktree):
    replacements = [(str(root), '<PROBE_ROOT>'), (str(binary), '<NATIVE_BINARY>'),
                    (str(worktree), '<WORKTREE>')]
    for name, endpoint in endpoints.items():
        replacements.extend([(endpoint, '<' + name.upper() + '_URL>'),
            (endpoint.replace('http:', 'ws:'), '<' + name.upper() + '_WS_URL>')])

    def clean(value, field=None):
        if isinstance(value, dict):
            return {key: clean(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item, field) for item in value]
        if isinstance(value, str):
            for old, new in replacements:
                value = value.replace(old, new)
            if field == 'instructionSources' and value.startswith('/'):
                return '<ANCESTOR_INSTRUCTION_SOURCE>'
        return value

    return json.dumps(clean(json.loads(raw)), indent=2, ensure_ascii=True) + '\n'


def main(binary, mode, scratch):
    os.umask(0o077)
    root = Path(tempfile.mkdtemp(prefix=mode + '-', dir=scratch))
    print('scratch', root, flush=True)
    for name in ('home', 'codex', 'workspace'):
        (root / name).mkdir(mode=0o700)
    events, servers, processes = [], [], []
    start = time.monotonic()
    lock = threading.Lock()
    client = gate = None
    endpoints = {}
    version = ''

    def record(event, **data):
        with lock:
            events.append({'at': round(time.monotonic() - start, 4), 'event': event, **data})
        print(event, flush=True)

    def wait(predicate, bound=6):
        end = time.monotonic() + bound
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(.03)
        return bool(predicate())

    def handler(service):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_CONNECT(self):
                record('http_response', service=service, method='CONNECT', path=self.path,
                       status=503, upgrade=False)
                self.send_error(503)

            def do_GET(self):
                # Unchanged PC3 discovery payload; this is not a model endpoint.
                if service == 'quota' and self.path == '/api/codex/accounts/check':
                    data = json.dumps({'accounts': [{'id': 'fixture-account-a',
                        'workspace_backend_origin': 'https://fixture.invalid',
                        'account_routing_override': 'NO_CONSTRAINT', 'name': 'Fixture'}]}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    record('discovery', service=service)
                    return
                self.do_CONNECT()

            def do_POST(self):
                record('http_response', service=service, method='POST', path=self.path,
                       status=503, upgrade=bool(self.headers.get('Upgrade')))
                self.send_error(503)
        return Handler

    class ObservedClient(prior.Client):
        def receive(self):
            message = super().receive()
            if message.get('method') == 'thread/realtime/error':
                record('native_failure', **message['params'])
            elif message.get('method', '').startswith('thread/realtime/'):
                record('native_notification', message=message)
            return message

    try:
        for service in ('quota', 'alternate'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(service))
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            endpoints[service] = 'http://127.0.0.1:' + str(server.server_port)
        gate = gate_module.FixtureGate(servers[0].server_port, record)
        endpoints['gate'] = 'http://127.0.0.1:' + str(gate.port)
        original_handler = gate.server.RequestHandlerClass

        class ObservedHandler(original_handler):
            # HTTP/1.0 makes native WS reject the protocol before reading 503.
            protocol_version = 'HTTP/1.1'

            def send_response(self, code, message=None):
                record('http_response', service='gate', method=self.command, path=self.path,
                       upgrade=bool(self.headers.get('Upgrade')), status=code)
                super().send_response(code, message)

        gate.server.RequestHandlerClass = ObservedHandler
        gate.grant(seconds=20)
        token = prior.jwt({'email': 'fixture@example.invalid', 'sub': 'fixture-user',
            'iat': int(time.time()), 'exp': int(time.time()) + 3600,
            'https://api.openai.com/auth': {'chatgpt_account_id': 'fixture-account-a',
                'chatgpt_plan_type': 'pro', 'chatgpt_user_id': 'fixture-user'}})
        (root / 'codex/auth.json').write_text(json.dumps({'auth_mode': 'chatgpt',
            'OPENAI_API_KEY': None, 'tokens': {'id_token': token, 'access_token': token,
                'refresh_token': 'fixture-refresh', 'account_id': 'fixture-account-a'},
            'last_refresh': datetime.now(timezone.utc).isoformat()}))
        config = f'''model = "gpt-5.4"
model_provider = "gate"
chatgpt_base_url = "{endpoints['quota']}"
experimental_realtime_ws_base_url = "{endpoints['alternate']}"
experimental_realtime_webrtc_call_base_url = "{endpoints['alternate']}"
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
        if mode == 'websocket':
            # Source realtime_api_key requires this for direct WS; WebRTC keeps
            # the fake subscription auth alone. This is not a real API key.
            config = config.replace('name = "gate"', 'name = "gate"\nenv_key = "PC6_FAKE_KEY"')
        (root / 'codex/config.toml').write_text(config)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'),
            'CODEX_HOME': str(root / 'codex'), 'CODEX_SQLITE_HOME': str(root / 'codex'),
            'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_CACHE_HOME': str(root / 'cache'),
            'TMPDIR': str(root), 'TERM': 'xterm-256color',
            'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
            **{key: endpoints['quota'] for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                'http_proxy', 'https_proxy', 'all_proxy')}}
        if mode == 'websocket':
            env['PC6_FAKE_KEY'] = 'fictional-pc6-websocket-key'
        version = subprocess.run([binary, '--version'], env=env, cwd=root / 'workspace',
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        assert version == 'codex-cli 0.167.2', 'native version requires a new source review'
        # Short Unix path: deep worktree + durable scratch exceeds sockaddr_un.
        sock = root / 'backend.sock'
        relative_sock = 'unix://../backend.sock'
        command = [binary, '-c', 'experimental_realtime_ws_base_url="' + endpoints['gate'] + '"',
            '-c', 'experimental_realtime_webrtc_call_base_url="' + endpoints['gate'] + '"',
            'app-server', '--listen', relative_sock]
        record('backend_command', command=command)
        with (root / 'backend.stderr').open('wb') as errors:
            backend = subprocess.Popen(command, env=env, cwd=root / 'workspace',
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                start_new_session=True, close_fds=True)
        processes.append(backend)
        assert wait(sock.exists), 'backend socket missing'
        # AF_UNIX supports relative paths too; only this single-threaded caller changes cwd.
        old_cwd = Path.cwd()
        os.chdir(root)
        try:
            client = ObservedClient(Path('backend.sock'), record)
        finally:
            os.chdir(old_cwd)
        thread = client.request('thread/start', {'cwd': str(root / 'workspace')})
        tid = thread['thread']['id']
        params = {'threadId': tid, 'clientManagedHandoffs': True,
            'outputModality': 'audio', 'includeStartupContext': False,
            'version': 'v3', 'transport': {'type': mode}}
        if mode == 'webrtc':
            # No browser, audio capture, media negotiation, or provider response.
            params['transport']['sdp'] = 'v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=PC6-fixture\r\nt=0 0\r\n'
        record('start_requested', transport=mode, params=params)
        result = client.request('thread/realtime/start', params)
        assert 'probe_error' not in result, 'native start RPC rejected before routing'
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(e['event'] == 'native_failure' for e in events):
                break
            if client.buffer or select.select([client.sock], [], [], .1)[0]:
                client.deadline = time.monotonic() + 2
                client.receive()
        assert any(e['event'] == 'native_failure' for e in events), 'no native refusal notification'
        # Keep native backend and all listeners alive to observe retries/fallback.
        time.sleep(3)
        record('observation_complete', backend_alive=backend.poll() is None)
    finally:
        if client:
            client.close()
        for process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        if gate:
            gate.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        record('cleanup', all_reaped=bool(processes) and all(p.poll() is not None for p in processes))
        data = {'mode': mode, 'version': version, 'events': events,
            'auth_fixture': 'chatgpt-with-fake-provider-key' if mode == 'websocket' else 'chatgpt',
            'configured_endpoints': dict.fromkeys(('websocket', 'webrtc'), '<ALTERNATE_URL>'),
            'forced_endpoints': dict.fromkeys(('websocket', 'webrtc'), '<GATE_URL>'),
            'backend_stderr': (root / 'backend.stderr').read_text(errors='replace')
                if (root / 'backend.stderr').exists() else '',
            'scope': 'core RPC routing only; TUI and audio devices not exercised'}
        raw = json.dumps(data, indent=2, ensure_ascii=True) + '\n'
        (root / 'raw.json').write_text(raw)
        text = sanitize_evidence(raw, root, binary, endpoints, HERE.parents[4])
        (HERE / 'evidence' / (mode + '.json')).write_text(text)
    check.validate_case(json.loads(text))


if __name__ == '__main__':
    main(str(Path(sys.argv[1]).resolve()), sys.argv[2], Path(sys.argv[3]).resolve())
