"""QX native stages: real codex app-server, fabricated credentials, loopback gate and backend.

Run only through run.py, which applies the PC Linux network guard and an outer
timeout. Every native process gets a temporary CODEX_HOME and HOME, proxy
variables pointing at the gate (which refuses CONNECT and records the host) and
a fake backend reachable only through the gate. CODEX_REFRESH_TOKEN_URL_OVERRIDE
is an internal test hook used here only to make native refresh observable; the
gate never sees refresh traffic either way. No token value enters the evidence.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import base64
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]


def load(name, path):
    spec = importlib.util.spec_from_file_location('qx_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


binding = load('binding', HERE / 'binding.py')
fixtures = load('fixtures', HERE / 'fixtures.py')
gate_module = load('gate', HERE / 'gate.py')
handoff = load('handoff', REPO / 'lib/codex-as/handoff.py')
A, B, UA, UB = 'fixture-account-a', 'fixture-account-b', 'fixture-user-a', 'fixture-user-b'


class Rpc(handoff.Client):
    """App Server JSON-RPC over the private Unix socket; keeps notifications, answers server requests."""

    def __init__(self, path, record):
        self.timeout, self.deadline, self.buffer, self.next_id = 10, time.monotonic() + 10, bytearray(), 0
        self.record, self.notes = record, []
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(10)
        self.sock.connect(str(path))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(('GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ' + key + '\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buffer:
            self.buffer.extend(self._recv())
        head, _, rest = self.buffer.partition(b'\r\n\r\n')
        self.buffer = bytearray(rest)
        assert b'101 Switching Protocols' in head
        self.request('initialize', {'clientInfo': {'name': 'qx-native-research', 'version': '1'},
                                   'capabilities': {'experimentalApi': True}})
        self.send({'method': 'initialized', 'params': {}})

    def receive(self):
        message = super().receive()
        if isinstance(message, dict) and 'method' in message:
            if 'id' in message:  # a server request, e.g. external-token refresh: refuse it, visibly
                self.record('server_request', method=message['method'])
                self.send({'id': message['id'], 'error': {'code': -32000, 'message': 'qx fixture refuses'}})
            else:
                self.notes.append(message)
        return message

    def request(self, method, params, timeout=None):
        self.deadline = time.monotonic() + (timeout or self.timeout)
        self.next_id += 1
        ident = self.next_id
        self.send({'id': ident, 'method': method, 'params': params})
        try:
            while True:
                message = self.receive()
                if isinstance(message, dict) and message.get('id') == ident and 'method' not in message:
                    if 'error' in message:
                        return {'rpc_error': str(message['error'].get('message'))[:600]}
                    return message['result']
        except (OSError, ValueError) as error:
            return {'rpc_error': type(error).__name__ + ': ' + str(error)[:200]}

    def wait(self, predicate, timeout):
        self.deadline = time.monotonic() + timeout
        seen = 0
        try:
            while True:
                for note in self.notes[seen:]:
                    if predicate(note):
                        return note
                seen = len(self.notes)
                self.receive()
        except (OSError, ValueError):
            return None


def windows(result):
    """A sanitized, complete view of what native returned for a rate-limit read."""
    if 'rpc_error' in result:
        return {'rpc_error': result['rpc_error']}
    def view(snapshot):
        return None if snapshot is None else {key: snapshot.get(key) for key in (
            'limitId', 'limitName', 'primary', 'secondary', 'planType', 'rateLimitReachedType')}
    return {'rateLimits': view(result.get('rateLimits')), 'accountId': result.get('accountId'),
            'ordinaryUsageAllowed': result.get('ordinaryUsageAllowed'),
            'byLimitId': {key: view(value) for key, value in sorted((result.get('rateLimitsByLimitId') or {}).items())}}


class Harness:
    def __init__(self, binary, stage):
        self.binary, self.stage = binary, stage
        self.root = Path(tempfile.mkdtemp(prefix='quota-qx-' + stage + '-'))
        print('scratch', self.root, flush=True)
        self.upstream = fixtures.Upstream()
        self.timeline, self.processes, self.clients, self.gates = [], [], [], []
        self.start_time = time.monotonic()
        self.issued = None  # the access token the fake token endpoint issued last

    def record(self, event, **data):
        self.timeline.append({'at': round(time.monotonic() - self.start_time, 3), 'event': event, **data})
        print(event, json.dumps(data, default=str)[:300], flush=True)

    def gate(self, five=20, weekly=30, max_age=120):
        lease = binding.Lease(expected_account=A, expected_user=UA, policy=binding.Policy(five, weekly, max_age),
                              clock=time.monotonic, wall=time.time)
        gate = gate_module.Gate(lease, self.upstream.url)
        self.gates.append(gate)
        return gate

    def home(self, name, gate, auth=None, link=None):
        home = self.root / name
        home.mkdir(mode=0o700)
        (home / 'workspace').mkdir(mode=0o700)
        if link is not None:
            os.symlink(link, home / 'auth.json')
        elif auth is not None:
            fd = os.open(home / 'auth.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.write(fd, auth.encode())
            os.close(fd)
        (home / 'config.toml').write_text(f'''model = "gpt-5.4"
model_provider = "qxgate"
chatgpt_base_url = "{gate.url}/backend-api/"
cli_auth_credentials_store = "file"
approval_policy = "never"
web_search = "disabled"
check_for_update_on_startup = false
[model_providers.qxgate]
name = "qxgate"
base_url = "{gate.url}/backend-api/codex"
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
[projects."{home / 'workspace'}"]
trust_level = "trusted"
''')
        return home

    def start(self, home, gate, refresh_hook):
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'CODEX_HOME': str(home),
               'CODEX_SQLITE_HOME': str(home), 'XDG_CONFIG_HOME': str(home / 'xdg-config'),
               'XDG_CACHE_HOME': str(home / 'xdg-cache'), 'TMPDIR': str(self.root), 'TERM': 'dumb',
               'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost',
               **{key: gate.url for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                            'http_proxy', 'https_proxy', 'all_proxy')}}
        if refresh_hook:
            env['CODEX_REFRESH_TOKEN_URL_OVERRIDE'] = self.upstream.url + '/oauth/token'
        sock = home / 'backend.sock'
        with (home / 'backend.stderr').open('wb') as errors:
            process = subprocess.Popen([self.binary, 'app-server', '--listen', 'unix://' + str(sock)],
                                       env=env, cwd=home / 'workspace', stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True)
        self.processes.append(process)
        deadline = time.monotonic() + 10
        while not sock.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert sock.exists(), 'app-server socket missing'
        rpc = Rpc(sock, self.record)
        self.clients.append(rpc)
        return rpc

    def read(self, rpc, label, exclude=True):
        params = {'excludeResetCreditDetails': True} if exclude else {}
        result = rpc.request('account/rateLimits/read', params)
        self.record('rate_limits_read', label=label, result=windows(result))
        return result

    def turn(self, rpc, thread, text, timeout=15):
        before = self.upstream.count('model')
        started = rpc.request('turn/start', {'threadId': thread, 'input': [{'type': 'text', 'text': text}]})
        if 'rpc_error' in started:
            self.record('turn', text=text, start_error=started['rpc_error'])
            return 'start-error'
        ident = started['turn']['id']
        note = rpc.wait(lambda n: n.get('method') == 'turn/completed'
                        and n['params']['turn']['id'] == ident, timeout)
        turn = note['params']['turn'] if note else {}
        errors = [n['params']['error'].get('message', '')[:300] for n in rpc.notes
                  if n.get('method') == 'error' and n['params'].get('turnId') == ident]
        status = turn.get('status', 'timeout')
        self.record('turn', text=text, status=status,
                    error=((turn.get('error') or {}).get('message') or '')[:300], error_notifications=errors,
                    upstream_model_requests=self.upstream.count('model') - before)
        return status

    def thread(self, rpc, home):
        return rpc.request('thread/start', {'cwd': str(home / 'workspace')})['thread']['id']

    def token_matches(self, rpc, **tokens):
        result = rpc.request('getAuthStatus', {'includeToken': True})
        exported = result.get('authToken')
        matches = {name: exported == value for name, value in tokens.items()}
        self.record('get_auth_status_include_token', exported=exported is not None, matches=matches,
                    auth_method=result.get('authMethod'), rpc_error=result.get('rpc_error'))
        return matches

    def stored(self, path, **tokens):
        data = json.loads(Path(path).read_text())
        return {name: data['tokens']['access_token'] == value for name, value in tokens.items()}

    def finish(self, ok):
        for client in self.clients:
            client.close()
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        stderr = {}
        for path in sorted(self.root.glob('*/backend.stderr')):
            stderr[path.parent.name] = path.read_text(errors='replace')[-4000:]
        for gate in self.gates:
            gate.close()
        self.upstream.close()
        data = {'stage': self.stage, 'ok': ok, 'timeline': self.timeline,
                'gates': [{'events': gate.events, 'lease_log': gate.lease.log} for gate in self.gates],
                'upstream': self.upstream.events, 'backend_stderr_tail': stderr}
        text = json.dumps(data, indent=1, ensure_ascii=True, default=str)
        text = text.replace(str(self.root), '<PROBE_ROOT>')
        text = re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:<PORT>', text)
        for gate in self.gates:
            for digest in gate.labels:
                assert digest.hex() not in text
        (HERE / 'evidence').mkdir(exist_ok=True)
        (HERE / 'evidence' / (self.stage + '.json')).write_text(text + '\n')


def label(gate, **tokens):
    for name, value in tokens.items():
        gate.labels[gate.lease.digest(value)] = name


def stage_shape(h):
    gate = h.gate()
    t1, r1 = h.upstream.issue(A, UA)
    label(gate, T1=t1)
    home = h.home('codex', gate, fixtures.auth_json(t1, r1, A, UA))
    rpc = h.start(home, gate, refresh_hook=False)
    h.record('startup_requests', gate=[(e['event'], e.get('path')) for e in gate.events])
    h.read(rpc, 'baseline')
    h.read(rpc, 'baseline-repeat')
    h.read(rpc, 'with-reset-credit-details', exclude=False)
    h.token_matches(rpc, T1=t1)
    wall = int(time.time())
    def body(**changes):
        value = fixtures.usage_body(A, UA, wall=wall)
        for key, change in changes.items():
            if key == 'rate':
                value['rate_limit'].update(change)
            else:
                value[key] = change
        return value
    win = fixtures.usage_body(A, UA, wall=wall)['rate_limit']
    five, week = win['primary_window'], win['secondary_window']
    spark = {'limit_name': 'gpt-fixture-spark', 'metered_feature': 'codex_bengalfox',
             'rate_limit': {'allowed': True, 'limit_reached': False, 'primary_window': dict(five, used_percent=90),
                            'secondary_window': None}}
    cases = [
        ('swapped-slots', 200, body(rate={'primary_window': week, 'secondary_window': five})),
        ('other-family-row', 200, body(additional_rate_limits=[spark])),
        ('missing-secondary', 200, body(rate={'secondary_window': None})),
        ('rate-limit-null', 200, body(rate_limit=None)),
        ('fractional-percent', 200, body(rate={'primary_window': dict(five, used_percent=25.5)})),
        ('percent-over-100', 200, body(rate={'primary_window': dict(five, used_percent=150)})),
        ('percent-negative', 200, body(rate={'primary_window': dict(five, used_percent=-5)})),
        ('zero-window-seconds', 200, body(rate={'primary_window': dict(five, limit_window_seconds=0)})),
        ('reset-in-past', 200, body(rate={'primary_window': dict(five, reset_at=wall - 7200)})),
        ('limit-reached', 200, body(rate={'allowed': False, 'limit_reached': True})),
        ('unknown-plan', 200, body(plan_type='fixture_unknown_plan')),
        ('usage-based-plan', 200, body(plan_type='self_serve_business_usage_based')),
        ('missing-plan', 200, {k: v for k, v in body().items() if k != 'plan_type'}),
        ('account-mismatch', 200, body(account_id=B)),
        ('user-mismatch', 200, body(user_id=UB)),
        ('identity-absent', 200, {k: v for k, v in body().items() if k not in ('account_id', 'user_id')}),
        ('http-401', 401, {'error': {'code': 'token_expired'}}),
        ('http-503', 503, {'error': 'fixture outage'}),
        ('non-json', 200, b'fixture is not json'),
    ]
    for name, status, payload in cases:
        before_connect = sum(e['event'] == 'connect_refused' for e in gate.events)
        h.upstream.usage_override = lambda identity, s=status, p=payload: (s, p)
        result = h.read(rpc, name)
        event = [e for e in gate.events if e['event'] == 'get' and e['path'].endswith('/usage')][-1]
        h.record('case', name=name, native_ok='rpc_error' not in result, lease_outcome=event['lease'],
                 refresh_connects=sum(e['event'] == 'connect_refused' for e in gate.events) - before_connect)
    h.upstream.usage_override = None
    assert h.upstream.count('model') == 0 and h.upstream.count('refresh') == 0


def stage_rotation(h):
    gate = h.gate()
    t1, r1 = h.upstream.issue(A, UA)
    label(gate, T1=t1)
    home = h.home('codex', gate, fixtures.auth_json(t1, r1, A, UA))
    rpc = h.start(home, gate, refresh_hook=True)
    h.read(rpc, 'preflight')
    h.record('preflight_ready', ready=gate.lease.ready(), epoch=gate.lease.epoch)
    thread = h.thread(rpc, home)
    assert h.turn(rpc, thread, 'Fixture turn one') == 'completed'
    h.upstream.revoke(t1)  # the server now answers 401 for T1, as after expiry
    original_issue = h.upstream.issue

    def issue(*args, **kwargs):
        access, refresh = original_issue(*args, **kwargs)
        label(gate, T2=access)
        h.issued = access
        return access, refresh
    h.upstream.issue = issue
    h.turn(rpc, thread, 'Fixture turn after server revocation')
    t2 = h.issued
    h.record('after_native_refresh', refreshes=h.upstream.count('refresh'),
             stored=h.stored(home / 'auth.json', T1=t1, T2=t2),
             model_upstream_by_credential=[e['credential'] for e in gate.events if e['event'] == 'model_upstream'])
    h.token_matches(rpc, T1=t1, T2=t2)
    h.read(rpc, 'rebind')
    h.record('rebound', ready=gate.lease.ready(), epoch=gate.lease.epoch)
    assert h.turn(rpc, thread, 'Fixture turn after rebind') == 'completed'
    h.read(rpc, 'stale-old-bearer-check')


def stage_proactive(h):
    for hooked in (True, False):
        gate = h.gate()
        t1, r1 = h.upstream.issue(A, UA, lifetime=120)  # inside native's five-minute refresh window
        label(gate, T1=t1)
        original_issue = h.upstream.issue

        def issue(*args, _gate=gate, _orig=original_issue, **kwargs):
            access, refresh = _orig(*args, **kwargs)
            label(_gate, T2=access)
            return access, refresh
        h.upstream.issue = issue
        name = 'hooked' if hooked else 'production-host'
        home = h.home(name, gate, fixtures.auth_json(t1, r1, A, UA))
        before = len(h.upstream.events)
        rpc = h.start(home, gate, refresh_hook=hooked)
        h.read(rpc, name)
        h.record('proactive', variant=name,
                 upstream_order=[e['kind'] for e in h.upstream.events[before:]],
                 connects=[e['destination'] for e in gate.events if e['event'] == 'connect_refused'],
                 usage_credentials=[e['credential'] for e in gate.events if e['event'] == 'get'
                                    and e['path'].endswith('/usage')])
        h.upstream.issue = original_issue
        rpc.close()
        h.clients.remove(rpc)


def stage_identity(h):
    gate = h.gate()
    ta, ra = h.upstream.issue(A, UA)
    tb, _ = h.upstream.issue(B, UB)
    label(gate, TA=ta, TB=tb)
    home = h.home('codex', gate, fixtures.auth_json(ta, ra, A, UA))
    rpc = h.start(home, gate, refresh_hook=True)
    h.read(rpc, 'preflight-a')
    thread = h.thread(rpc, home)
    assert h.turn(rpc, thread, 'Fixture turn as A') == 'completed'
    h.record('login_b', result=rpc.request('account/login/start', {
        'type': 'chatgptAuthTokens', 'accessToken': tb, 'chatgptAccountId': B, 'chatgptPlanType': 'pro'}))
    h.turn(rpc, thread, 'Fixture existing thread after switch to B')
    other = h.thread(rpc, home)
    h.turn(rpc, other, 'Fixture new thread after switch to B')
    h.read(rpc, 'read-as-b')
    h.record('login_a_again', result=rpc.request('account/login/start', {
        'type': 'chatgptAuthTokens', 'accessToken': ta, 'chatgptAccountId': A, 'chatgptPlanType': 'pro'}))
    third = h.thread(rpc, home)
    h.turn(rpc, third, 'Fixture new thread back on A before continuation')
    h.read(rpc, 'fresh-a-without-continuation')
    h.record('lease_after_fresh_a', paused=gate.lease.paused, epoch=gate.lease.epoch)
    h.record('continuation', accepted=gate.lease.request_continuation(trusted=True))
    time.sleep(.05)
    h.read(rpc, 'fresh-a-after-continuation')
    h.record('lease_after_continuation', paused=gate.lease.paused, epoch=gate.lease.epoch)
    h.turn(rpc, third, 'Fixture turn after deliberate continuation')


def stage_concurrent(h):
    secrets = h.root / 'secrets'
    secrets.mkdir(mode=0o700)
    t1, r1 = h.upstream.issue(A, UA)
    secret = secrets / 'codex-fixture.json'
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, fixtures.auth_json(t1, r1, A, UA).encode())
    os.close(fd)
    gates, rpcs, homes = [], [], []
    for index in (1, 2):
        gate = h.gate()
        label(gate, T1=t1)
        home = h.home('session%d' % index, gate, link=secret)
        gates.append(gate)
        homes.append(home)
        rpcs.append(h.start(home, gate, refresh_hook=True))
    original_issue = h.upstream.issue

    def issue(*args, **kwargs):
        access, refresh = original_issue(*args, **kwargs)
        for gate in gates:
            label(gate, T2=access)
        h.issued = access
        return access, refresh
    h.upstream.issue = issue
    for index, rpc in enumerate(rpcs, 1):
        h.read(rpc, 'preflight-session%d' % index)
    forced = rpcs[0].request('getAuthStatus', {'refreshToken': True})
    t2 = h.issued
    h.record('session1_forced_refresh', rpc_error=forced.get('rpc_error'), refreshes=h.upstream.count('refresh'),
             secret=h.stored(secret, T1=t1, T2=t2),
             links=[os.path.islink(home / 'auth.json') and os.readlink(home / 'auth.json') == str(secret)
                    for home in homes],
             secret_mode=oct(os.stat(secret).st_mode & 0o777), secret_links=os.stat(secret).st_nlink)
    thread2 = h.thread(rpcs[1], homes[1])
    assert h.turn(rpcs[1], thread2, 'Fixture session two with its cached bearer') == 'completed'
    h.upstream.revoke(t1)
    h.turn(rpcs[1], thread2, 'Fixture session two after T1 revocation')
    h.record('session2_after_adoption', refreshes=h.upstream.count('refresh'),
             lease2=gates[1].lease.paused, epoch2=gates[1].lease.epoch)
    h.token_matches(rpcs[1], T1=t1, T2=t2)
    h.read(rpcs[1], 'session2-rebind')
    assert h.turn(rpcs[1], thread2, 'Fixture session two after rebind') == 'completed'
    thread1 = h.thread(rpcs[0], homes[0])
    h.turn(rpcs[0], thread1, 'Fixture session one before its own rebind')
    h.read(rpcs[0], 'session1-rebind')
    assert h.turn(rpcs[0], thread1, 'Fixture session one after rebind') == 'completed'
    h.record('final', refreshes=h.upstream.count('refresh'), secret=h.stored(secret, T1=t1, T2=t2))


STAGES = {'shape': stage_shape, 'rotation': stage_rotation, 'proactive': stage_proactive,
          'identity': stage_identity, 'concurrent': stage_concurrent}


def main(binary, stage):
    harness = Harness(binary, stage)
    ok = False
    try:
        STAGES[stage](harness)
        ok = True
    finally:
        harness.finish(ok)
    return 0


if __name__ == '__main__':
    sys.exit(main(str(Path(sys.argv[1]).resolve()), sys.argv[2]))
