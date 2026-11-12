"""Offline tests for the KLX composed gate: routing, lock to first byte, loss latch, restart,
CONNECT allowlist, identity, gate-read rebind and the upstream proxy. Loopback fixtures only.

python3 -I -B test_gate.py
"""
import http.client
import importlib.util
import json
from pathlib import Path
import socket
import threading
import time
import unittest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate_module = load('klx_test_gate', HERE / 'gate.py')
fixtures = load('klx_test_fixtures', HERE / 'fixtures.py')
binding = gate_module.binding
A, UA, B, UB = 'fixture-account-a', 'fixture-user-a', 'fixture-account-b', 'fixture-user-b'


class Pure(unittest.TestCase):
    def test_route_table(self):
        route = gate_module.route
        self.assertEqual(route('GET', '/backend-api/wham/usage', {}, paused='x', lost=None), ('usage', None))
        self.assertEqual(route('GET', '/backend-api/codex/models', {}, paused=None, lost=None), ('forward', None))
        self.assertEqual(route('GET', '/backend-api/codex/models', {}, paused='x', lost=None)[0], 'refuse')
        self.assertEqual(route('POST', '/backend-api/codex/responses', {}, paused='x', lost=None), ('admit', None))
        self.assertEqual(route('POST', '/backend-api/codex/memories/trace_summarize', {}, paused=None, lost=None),
                         ('refuse', 'unlisted-path'))
        self.assertEqual(route('GET', '/backend-api/codex/responses', {'Upgrade': 'websocket'}, paused=None,
                               lost=None), ('refuse', 'websocket-refused'))
        self.assertEqual(route('GET', '/backend-api/wham/usage', {}, paused=None, lost='monitor-eof'),
                         ('refuse', 'paused:monitor-eof'))
        self.assertEqual(route('DELETE', '/backend-api/wham/usage', {}, paused=None, lost=None)[1], 'unlisted-path')

    def test_connect_allowlist(self):
        allow = {'auth.openai.com:443': ('127.0.0.1', 1)}
        self.assertTrue(gate_module.connect_allowed('auth.openai.com:443', allow, paused=None, lost=None))
        self.assertFalse(gate_module.connect_allowed('auth.openai.com:443', allow, paused='threshold', lost=None))
        self.assertFalse(gate_module.connect_allowed('auth.openai.com:443', allow, paused=None, lost='monitor-eof'))
        self.assertFalse(gate_module.connect_allowed('chatgpt.com:443', allow, paused=None, lost=None))
        self.assertFalse(gate_module.connect_allowed('auth.openai.com:8443', allow, paused=None, lost=None))

    def test_describe_uses_latest_user_turn(self):
        body = json.dumps({'input': [
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'KLX-FIRST one'}]},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': 'KLX-SECOND two'}]},
            {'type': 'function_call_output', 'output': 'x'}]}).encode()
        self.assertEqual(gate_module.describe(body), ('KLX-SECOND', 'tool_result'))
        title = json.dumps({'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'Generate a concise title for KLX-X'}]}]}).encode()
        self.assertEqual(gate_module.describe(title), ('KLX-X', 'title'))
        self.assertEqual(gate_module.describe(b'not json'), (None, None))


class Http(unittest.TestCase):
    def setUp(self):
        self.backend = fixtures.Backend()
        self.events = []
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(64)

    def tearDown(self):
        self.gate.shutdown()
        self.listener.close()
        self.backend.close()

    def make(self, five=20, weekly=30, max_age=60, **options):
        lease = gate_module.KLease(expected_account=A, expected_user=UA, policy=binding.Policy(five, weekly, max_age),
                                   clock=time.monotonic, wall=time.time)
        self.gate = gate_module.Gate(lease, ('127.0.0.1', self.backend.port), self.listener,
                                     record=self.events.append, **options)
        threading.Thread(target=self.gate.serve, daemon=True).start()
        self.port = self.listener.getsockname()[1]
        return lease

    def request(self, method, path, bearer, account=A, body=None, read=True):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Authorization': 'Bearer ' + bearer, 'ChatGPT-Account-ID': account}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read() if read else None
        return response.status, data, connection, response

    def model(self, bearer, marker, account=A, read=True):
        body = json.dumps({'model': 'gpt-5.4', 'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': marker}]}]})
        return self.request('POST', '/backend-api/codex/responses', bearer, account, body, read)

    def usage(self, bearer, account=A):
        return self.request('GET', '/backend-api/wham/usage', bearer, account)

    def test_bound_bearer_is_admitted_and_threshold_closes_streams(self):
        self.make()
        access, _ = self.backend.issue(A, UA, 'A1')
        self.assertEqual(self.usage(access)[0], 200)
        status, _, connection, response = self.model(access, 'KLX-HOLD-a', read=False)
        self.assertEqual(status, 200)
        self.assertTrue(self.wait(lambda: self.backend.count('stream_open') == 1))
        self.backend.set_usage(A, 85, 40)
        self.usage(access)
        self.assertTrue(self.wait(lambda: self.backend.count('stream_lost') == 1))
        models = self.backend.count('model')
        self.assertEqual(self.model(access, 'KLX-AFTER')[0], 503)
        self.assertEqual(self.backend.count('model'), models)  # refused before any upstream byte
        connection.close()

    def test_lock_to_first_byte_rechecks_after_connect(self):
        lease = self.make()
        access, _ = self.backend.issue(A, UA, 'A1')
        self.usage(access)
        original = self.gate.open_upstream

        def open_then_pause():
            connection = original()
            with self.gate.lock:
                lease.latch('threshold:five_hour')  # a pause lands between decision and first byte
            return connection
        self.gate.open_upstream = open_then_pause
        status, data, _, _ = self.model(access, 'KLX-RACE')
        self.assertEqual(status, 503)
        self.assertIn(b'paused:threshold', data)
        self.assertEqual(self.backend.count('model'), 0)

    def test_monitor_stale_and_eof_latch_for_good(self):
        self.make(hb_max=0.2)
        access, _ = self.backend.issue(A, UA, 'A1')
        self.usage(access)
        self.assertTrue(self.wait(lambda: self.gate.lost == 'monitor-stale', 3))
        self.assertEqual(self.usage(access)[0], 503)  # nothing is forwarded once lost
        self.assertFalse(self.gate.request_continuation())
        self.assertEqual(self.model(access, 'KLX-X')[0], 503)
        self.assertEqual(self.backend.count('model'), 0)

    def test_heartbeats_keep_the_monitor_lease(self):
        self.make(hb_max=0.3)
        for _ in range(8):
            self.gate.heartbeat()
            time.sleep(.1)
        self.assertIsNone(self.gate.lost)
        self.gate.lose('monitor-eof')
        self.assertEqual(self.gate.status()['lost'], 'monitor-eof')

    def test_restart_needs_continuation_and_a_later_passing_read(self):
        lease = self.make()
        with self.gate.lock:
            lease.latch('gate-restarted')
        access, _ = self.backend.issue(A, UA, 'A1')
        self.usage(access)
        self.assertEqual(self.gate.status()['paused'], 'gate-restarted')  # a passing read never reopens
        self.assertEqual(self.model(access, 'KLX-X')[0], 503)
        self.assertTrue(self.gate.request_continuation())
        time.sleep(.01)
        self.usage(access)
        self.assertIsNone(self.gate.status()['paused'])
        self.assertEqual(self.model(access, 'KLX-Y')[0], 200)

    def test_foreign_account_latches_identity_before_upstream(self):
        self.make()
        access, _ = self.backend.issue(A, UA, 'A1')
        other, _ = self.backend.issue(B, UB, 'B1')
        self.usage(access)
        before = self.backend.count('usage')
        self.assertEqual(self.usage(other, B)[0], 503)
        self.assertEqual(self.backend.count('usage'), before)
        self.assertEqual(self.gate.status()['paused'], 'identity-changed')
        self.assertEqual(self.model(access, 'KLX-X')[0], 503)
        self.assertEqual(self.backend.count('model'), 0)

    def test_new_bearer_needs_its_own_read_and_gate_read_binds_it(self):
        self.make()
        first, _ = self.backend.issue(A, UA, 'A1')
        second, _ = self.backend.issue(A, UA, 'A2')
        self.usage(first)
        self.assertEqual(self.model(second, 'KLX-NEW')[0], 503)  # old quota never authorizes a new bearer
        self.gate.shutdown()
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(64)
        self.make(self_read=True)
        self.usage(first)
        self.assertEqual(self.model(second, 'KLX-NEW')[0], 200)
        self.backend.revoke(first)
        self.assertEqual(self.model(first, 'KLX-OLD')[0], 503)  # its own read fails: never bound
        self.assertTrue(any(e['event'] == 'self_read' and e['status'] == 401 for e in self.events))

    def test_connect_tunnel_only_while_running(self):
        target = socket.socket()
        target.bind(('127.0.0.1', 0))
        target.listen(4)
        lease = self.make(connect_allow={'auth.openai.com:443': target.getsockname()})

        def tunnel(host):
            sock = socket.create_connection(('127.0.0.1', self.port), timeout=5)
            sock.sendall(('CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' % (host, host)).encode())
            head = sock.recv(64).split(b'\r\n')[0]
            sock.close()
            return head
        self.assertIn(b' 200 ', tunnel('auth.openai.com:443'))
        self.assertIn(b' 403 ', tunnel('chatgpt.com:443'))
        with self.gate.lock:
            lease.latch('threshold:weekly')
        self.assertIn(b' 403 ', tunnel('auth.openai.com:443'))
        target.close()

    def test_upstream_through_a_launcher_proxy(self):
        proxy = fixtures.ConnectProxy({'127.0.0.1:%d' % self.backend.port})
        self.make(upstream_proxy=('127.0.0.1', proxy.port))
        access, _ = self.backend.issue(A, UA, 'A1')
        self.assertEqual(self.usage(access)[0], 200)
        self.assertEqual(self.model(access, 'KLX-P')[0], 200)
        self.assertGreaterEqual(len(proxy.events), 2)
        self.assertTrue(all(event['allowed'] for event in proxy.events))
        proxy.close()

    def test_paused_non_usage_reads_are_refused(self):
        lease = self.make()
        access, _ = self.backend.issue(A, UA, 'A1')
        self.assertEqual(self.request('GET', '/backend-api/codex/models', access)[0], 200)
        with self.gate.lock:
            lease.latch('threshold:five_hour')
        self.assertEqual(self.request('GET', '/backend-api/codex/models', access)[0], 503)
        self.assertEqual(self.usage(access)[0], 200)  # recovery observation still flows

    def wait(self, predicate, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(.02)
        return bool(predicate())


if __name__ == '__main__':
    unittest.main()
