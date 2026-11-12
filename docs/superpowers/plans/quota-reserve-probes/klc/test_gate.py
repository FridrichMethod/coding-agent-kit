"""Offline tests of the KLC composed gate: QC admission plus the K-owned timer,
monitor latch, restart pause, CONNECT allowlist and port holder.

Loopback fixtures only, fabricated bearers and accounts, temporary private
account directories. No native, no boundary.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load('klc_gate', HERE / 'gate.py')
fixtures = load('klc_fixtures', HERE / 'fixtures.py')
fake = load('klc_qc_fake_usage', HERE.parent / 'qc' / 'fake_usage.py')
admission = gate.admission

A = 'KLC-TEST-FABRICATED-ACCESS-A-NOT-A-CREDENTIAL'
A2 = 'KLC-TEST-FABRICATED-ACCESS-A2-NOT-A-CREDENTIAL'
B = 'KLC-TEST-FABRICATED-ACCESS-B-NOT-A-CREDENTIAL'
IDENT_A, IDENT_B = ('fixture-account-a', 'fixture-org-a'), ('fixture-account-b', 'fixture-org-b')
MODEL = 'claude-sonnet-4-6'
OPEN = []


def close_open():
    while OPEN:
        OPEN.pop().close()


def write_private(path, document):
    staging = path.with_name('.staging-' + path.name)
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(document, handle)
    os.rename(staging, path)


def make_account(root, token=A, expires=4102444800000, ident=IDENT_A):
    config = root / 'accounts' / 'alpha' / 'config'
    config.mkdir(parents=True, mode=0o700)
    for path in (root, root / 'accounts', config.parent, config):
        path.chmod(0o700)
    write_private(config / '.credentials.json', {'claudeAiOauth': {
        'accessToken': token, 'refreshToken': 'KLC-TEST-FABRICATED-REFRESH-NOT-A-CREDENTIAL', 'expiresAt': expires,
        'scopes': ['user:inference', 'user:profile']}})
    write_private(config / '.claude.json', {'oauthAccount': {'accountUuid': ident[0], 'organizationUuid': ident[1]}})
    return config


class Recorder:
    def __init__(self):
        self.rows, self.lock = [], threading.Lock()

    def __call__(self, event, **fields):
        with self.lock:
            self.rows.append(dict(event=event, t=time.monotonic(), **fields))

    def find(self, event, **match):
        with self.lock:
            return [r for r in self.rows if r['event'] == event and all(r.get(k) == v for k, v in match.items())]


def post(port, token, timeout=3):
    body = json.dumps({'model': MODEL, 'max_tokens': 8, 'stream': True,
                       'messages': [{'role': 'user', 'content': 'KLC sdk test'}]}).encode()
    sock = socket.create_connection(('127.0.0.1', port), timeout=timeout)
    OPEN.append(sock)
    head = 'POST /v1/messages?beta=true HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n' % len(body)
    if token:
        head += 'Authorization: Bearer %s\r\n' % token
    sock.sendall((head + '\r\n').encode() + body)
    return sock


def status_of(sock, timeout=3):
    sock.settimeout(timeout)
    data = b''
    try:
        while b'\r\n' not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    part = data.split(b' ', 2)[1:2]
    return int(part[0]) if part and part[0].isdigit() else None


class InProcess(unittest.TestCase):
    """Gate Server and controller in this process on a real loopback listener."""

    max_age = 2.0

    def setUp(self):
        os.umask(0o077)
        self.addCleanup(close_open)
        self.root = Path(tempfile.mkdtemp(prefix='quota-klc-test-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config_dir = make_account(self.root)
        self.usage = fake.FakeUsageServer()
        self.usage.__enter__()
        self.addCleanup(self.usage.__exit__, None, None, None)
        self.acct = self.usage.register(A, fake.FakeAccount('A', *IDENT_A, fake.windows(40, 30)))
        self.usage.register(A2, fake.FakeAccount('A2', *IDENT_A, fake.windows(40, 30)))
        self.usage.register(B, fake.FakeAccount('B', *IDENT_B, fake.windows(5, 5)))
        self.events = Recorder()
        self.held = []
        self.upstream = fixtures.Upstream({A: 'A', A2: 'A2', B: 'B'}, lambda info: ('hold', 'held'), self.events)
        self.addCleanup(self.upstream.close)
        self.tunnel_target = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(self.tunnel_target.close)
        self.config = {'config_dir': str(self.config_dir), 'registered': list(IDENT_A), 'usage_port': self.usage.port,
                       'upstream_port': self.upstream.port, 'mode': 'startup',
                       'connect_allow': {'platform.claude.com:443': self.tunnel_target.getsockname()[1]},
                       'policy': {'reserves': {'five_hour': 20, 'weekly': 10}, 'max_age_s': self.max_age,
                                  'allowed_models': [MODEL]}}

    def start(self, mode='startup'):
        self.controller = gate.build_controller(self.config, self.events)
        if mode == 'restart':
            self.controller.restart_paused()
        else:
            decision = self.controller.startup(gate.store_bearer(self.config))
            if not decision.ok:
                return decision
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.server = gate.Server(self.listener, gate.Gate(self.config, self.controller, self.events))
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.mon_gate, self.mon = socket.socketpair()
        OPEN.extend((self.mon, self.mon_gate))
        threading.Thread(target=gate.run_monitor, args=(self.mon_gate, self.controller, self.events),
                         daemon=True).start()
        self.stop_timer = threading.Event()
        self.addCleanup(self.stop_timer.set)

        def timer():
            while not self.stop_timer.wait(gate.TICK):
                self.controller.tick()

        threading.Thread(target=timer, daemon=True).start()
        return admission.Decision(True, 'started')

    def poll(self):
        count = len(self.events.find('poll'))
        self.mon.sendall(b'poll\n')
        self.assertTrue(fixtures.wait_for(lambda: len(self.events.find('poll')) > count, 5))
        return self.events.find('poll')[-1]

    def admitted_stream(self, token=A):
        sock = post(self.port, token)
        self.assertEqual(status_of(sock), 200)
        self.assertTrue(fixtures.wait_for(lambda: self.upstream.held(), 3))
        return sock

    def test_admitted_request_is_forwarded_with_its_own_bearer(self):
        self.assertTrue(self.start().ok)
        sock = self.admitted_stream()
        self.assertEqual([r['label'] for r in self.events.find('upstream_request')], ['A'])
        self.assertTrue(self.events.find('forwarded'))
        sock.close()

    def test_threshold_poll_latches_closes_streams_and_refuses_with_zero_upstream_bytes(self):
        self.start()
        sock = self.admitted_stream()
        self.acct.body = fake.windows(85, 30)
        result = self.poll()
        self.assertEqual(result['reason'], 'threshold_five_hour')
        self.assertTrue(fixtures.wait_for(lambda: not self.upstream.held(), 3))
        self.assertTrue(fixtures.wait_for(lambda: self.events.find('stream_disconnected'), 3))
        before = self.upstream.count
        self.assertEqual(status_of(post(self.port, A)), 403)
        self.acct.body = fake.windows(10, 30)
        self.poll()  # passive recovery never reopens
        self.assertEqual(status_of(post(self.port, A)), 403)
        self.assertEqual(self.upstream.count, before)
        sock.close()

    def test_monitor_eof_latches_at_once(self):
        self.start()
        sock = self.admitted_stream()
        self.mon.close()
        self.assertTrue(fixtures.wait_for(lambda: self.events.find('controller', kind='latched',
                                                                    reason='monitor_eof'), 2))
        self.assertTrue(fixtures.wait_for(lambda: not self.upstream.held(), 3))
        self.assertEqual(status_of(post(self.port, A)), 403)
        sock.close()

    def test_stalled_monitor_closes_streams_at_the_lease_deadline_without_admit(self):
        self.start()
        began = time.monotonic()
        sock = self.admitted_stream()
        self.assertTrue(fixtures.wait_for(lambda: self.events.find('controller', kind='latched',
                                                                    reason='freshness_expired'), self.max_age + 2))
        latch = self.events.find('controller', kind='latched')[0]['t']
        self.assertLess(latch - began, self.max_age + 0.5)
        self.assertTrue(fixtures.wait_for(lambda: not self.upstream.held(), 3))
        sock.close()

    def test_polls_extend_the_lease(self):
        self.start()
        for _ in range(3):
            time.sleep(self.max_age / 2)
            self.assertTrue(self.poll()['ok'])
        self.assertFalse(self.events.find('controller', kind='latched'))

    def test_connect_exact_target_only_while_open(self):
        self.start()
        tunnel = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        tunnel.sendall(b'CONNECT platform.claude.com:443 HTTP/1.1\r\nHost: platform.claude.com:443\r\n\r\n')
        self.assertEqual(status_of(tunnel), 200)
        accepted, _ = self.tunnel_target.accept()
        tunnel.sendall(b'payload')
        accepted.settimeout(3)
        self.assertEqual(accepted.recv(16), b'payload')
        for target in ('api.anthropic.com:443', 'platform.claude.com:80', 'platform.claude.com.evil:443'):
            other = socket.create_connection(('127.0.0.1', self.port), timeout=3)
            OPEN.append(other)
            other.sendall(('CONNECT %s HTTP/1.1\r\n\r\n' % target).encode())
            self.assertEqual(status_of(other), 403, target)
            other.close()
        self.acct.body = fake.windows(85, 30)
        self.poll()
        accepted.settimeout(3)
        self.assertEqual(accepted.recv(16), b'')  # the open tunnel was closed by the pause
        late = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        OPEN.append(late)
        late.sendall(b'CONNECT platform.claude.com:443 HTTP/1.1\r\n\r\n')
        self.assertEqual(status_of(late), 403)
        for item in (tunnel, accepted, late):
            item.close()

    def test_rotation_to_same_account_bearer_holds_then_admits(self):
        self.start()
        sock = self.admitted_stream(A2)
        kinds = [r['kind'] for r in self.events.find('controller')]
        self.assertIn('rotation_hold', kinds)
        self.assertEqual(self.usage.labels()[-1], 'A2')
        self.assertEqual([r['label'] for r in self.events.find('upstream_request')], ['A2'])
        sock.close()

    def test_switch_to_other_account_pauses_with_zero_bytes_of_the_new_bearer(self):
        self.start()
        sock = self.admitted_stream(A)
        self.assertEqual(status_of(post(self.port, B)), 403)
        self.assertIn('B', self.usage.labels())
        self.assertNotIn('B', [r['label'] for r in self.events.find('upstream_request')])
        self.assertTrue(self.events.find('controller', kind='latched', reason='bearer_identity_mismatch'))
        self.assertTrue(fixtures.wait_for(lambda: not self.upstream.held(), 3))
        sock.close()

    def test_missing_authorization_latches(self):
        self.start()
        self.assertEqual(status_of(post(self.port, None)), 403)
        self.assertTrue(self.events.find('controller', kind='latched', reason='auth_selector_conflict'))

    def test_restart_starts_paused_and_needs_a_fresh_continuation(self):
        self.start('restart')
        self.assertEqual(status_of(post(self.port, A)), 403)
        self.assertEqual(self.upstream.count, 0)
        self.acct.body = fake.windows(85, 30)
        token = self.controller.issue_continuation()
        self.assertFalse(self.controller.continue_manually(token, gate.store_bearer(self.config)).ok)
        self.assertFalse(self.controller.continue_manually(token, gate.store_bearer(self.config)).ok)  # one use
        self.acct.body = fake.windows(40, 30)
        token = self.controller.issue_continuation()
        self.assertTrue(self.controller.continue_manually(token, gate.store_bearer(self.config)).ok)
        sock = self.admitted_stream()
        sock.close()

    def test_unproved_row_refuses_at_startup(self):
        self.acct.body = fake.windows(40, 30, extra={'limits': [{'kind': 'weekly', 'percent': 5}]})
        decision = self.start()
        self.assertFalse(decision.ok)
        self.assertEqual(decision.reason, 'quota_row_unproved')

    def test_unknown_route_is_refused_without_latch(self):
        self.start()
        sock = socket.create_connection(('127.0.0.1', self.port), timeout=3)
        OPEN.append(sock)
        sock.sendall(b'GET /api/oauth/usage HTTP/1.1\r\nHost: x\r\n\r\n')
        self.assertEqual(status_of(sock), 404)
        self.assertFalse(self.events.find('controller', kind='latched'))


class Process(unittest.TestCase):
    """The real gate process on a holder-owned listener: death, holder and restart."""

    def setUp(self):
        os.umask(0o077)
        self.addCleanup(close_open)
        self.root = Path(tempfile.mkdtemp(prefix='quota-klc-test-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config_dir = make_account(self.root)
        self.usage = fake.FakeUsageServer()
        self.usage.__enter__()
        self.addCleanup(self.usage.__exit__, None, None, None)
        self.acct = self.usage.register(A, fake.FakeAccount('A', *IDENT_A, fake.windows(40, 30)))
        self.events = Recorder()
        self.upstream = fixtures.Upstream({A: 'A'}, lambda info: ('text', 'ok'), self.events)
        self.addCleanup(self.upstream.close)
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.addCleanup(self.listener.close)
        self.port = self.listener.getsockname()[1]
        self.processes = []
        self.addCleanup(self.reap)

    def reap(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(5)
            process.stdout.close()

    def spawn(self, mode):
        config = {'config_dir': str(self.config_dir), 'registered': list(IDENT_A), 'usage_port': self.usage.port,
                  'upstream_port': self.upstream.port, 'mode': mode,
                  'policy': {'reserves': {'five_hour': 20}, 'max_age_s': 30, 'allowed_models': [MODEL]}}
        path = self.root / ('gate-%s.json' % mode)
        path.write_text(json.dumps(config))
        mon_gate, self.mon = socket.socketpair()
        ctl_gate, self.ctl = socket.socketpair()
        fds = (self.listener.fileno(), mon_gate.fileno(), ctl_gate.fileno())
        process = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'gate.py'), str(path), '--listen-fd',
                                    str(fds[0]), '--monitor-fd', str(fds[1]), '--control-fd', str(fds[2])],
                                   pass_fds=fds, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        mon_gate.close()
        ctl_gate.close()
        OPEN.extend((self.mon, self.ctl))
        self.processes.append(process)
        first = json.loads(process.stdout.readline())
        while first['event'] not in ('gate_ready', 'startup_refused'):
            first = json.loads(process.stdout.readline())
        return process, first

    def test_startup_refusal_exits_before_serving(self):
        self.acct.body = fake.windows(90, 30)
        process, first = self.spawn('startup')
        self.assertEqual(first, dict(first, event='startup_refused', reason='threshold_five_hour'))
        self.assertEqual(process.wait(5), 3)
        self.assertEqual(self.upstream.count, 0)

    def test_gate_death_holder_keeps_port_and_restart_refuses_backlog(self):
        process, first = self.spawn('startup')
        self.assertEqual(first['event'], 'gate_ready')
        self.assertEqual(status_of(post(self.port, A)), 200)
        process.kill()
        process.wait(5)
        impostor = socket.socket()
        with self.assertRaises(OSError) as raised:
            impostor.bind(('127.0.0.1', self.port))
        self.assertEqual(raised.exception.errno, 98)  # EADDRINUSE while the holder lives
        impostor.close()
        queued = post(self.port, A)  # lands in the holder's backlog
        self.assertIsNone(status_of(queued, timeout=1))
        restarted, first = self.spawn('restart')
        self.assertEqual(first['mode'], 'restart')
        self.assertEqual(status_of(queued), 403)
        self.assertEqual(status_of(post(self.port, A)), 403)
        self.assertEqual(self.upstream.count, 1)
        self.ctl.sendall(b'{"op": "continue"}\n')
        self.ctl.settimeout(10)
        reply = json.loads(self.ctl.recv(4096))
        self.assertTrue(reply['ok'], reply)
        self.assertEqual(status_of(post(self.port, A)), 200)
        self.assertEqual(self.upstream.count, 2)


if __name__ == '__main__':
    unittest.main()
