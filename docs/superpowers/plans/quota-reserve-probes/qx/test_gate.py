"""Offline HTTP tests: the QX gate between a scripted client and the fake upstream.

Loopback only; no native binary. Exercises forwarding, same-bearer binding,
refusal before upstream bytes, stream closure on pause and CONNECT refusal.
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


def load(name):
    spec = importlib.util.spec_from_file_location('qx_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


binding, fixtures, gate_module = load('binding'), load('fixtures'), load('gate')
A, B, UA, UB = 'acct-a', 'acct-b', 'user-a', 'user-b'


class GateTests(unittest.TestCase):
    def setUp(self):
        self.upstream = fixtures.Upstream()
        self.t1, self.r1 = self.upstream.issue(A, UA)
        self.lease = binding.Lease(expected_account=A, expected_user=UA,
                                   policy=binding.Policy(20, 30, 120), clock=time.monotonic, wall=time.time)
        self.gate = gate_module.Gate(self.lease, self.upstream.url,
                                     labels={self.lease.digest(self.t1): 'T1'})
        self.port = int(self.gate.url.rsplit(':', 1)[1])

    def tearDown(self):
        self.upstream.hold.set()
        self.gate.close()
        self.upstream.close()

    def call(self, method, path, bearer, account=A, body=None, timeout=5):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
        headers = {'Authorization': 'Bearer ' + bearer, 'ChatGPT-Account-ID': account}
        data = None if body is None else json.dumps(body).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())

    def usage(self, bearer, account=A):
        return self.call('GET', '/backend-api/wham/usage', bearer, account)

    def model(self, bearer, account=A, model='gpt-5.4'):
        return self.call('POST', '/backend-api/codex/responses', bearer, account,
                         {'model': model, 'input': [], 'stream': True})

    def rotate(self):
        connection = http.client.HTTPConnection('127.0.0.1', int(self.upstream.url.rsplit(':', 1)[1]), timeout=5)
        connection.request('POST', '/oauth/token', body=json.dumps({'grant_type': 'refresh_token',
                                                                    'refresh_token': self.r1}).encode())
        tokens = json.loads(connection.getresponse().read())
        self.gate.labels[self.lease.digest(tokens['access_token'])] = 'T2'
        return tokens['access_token']

    def test_usage_is_relayed_unchanged_and_binds_the_same_bearer(self):
        status, body, _ = self.usage(self.t1)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['account_id'], A)
        self.assertTrue(self.lease.ready())
        event = [e for e in self.gate.events if e['event'] == 'get'][-1]
        self.assertEqual((event['credential'], event['lease'], event['shown']['chatgpt-account-id']),
                         ('T1', 'fresh', A))
        status, body, _ = self.model(self.t1)
        self.assertEqual(status, 200)
        self.assertIn(b'response.completed', body)
        self.assertEqual(self.upstream.count('model'), 1)

    def test_model_request_before_preflight_never_reaches_upstream(self):
        status, body, _ = self.model(self.t1)
        self.assertEqual(status, 503)
        self.assertIn(b'rebind-required', body)
        self.assertEqual(self.upstream.count('model'), 0)

    def test_rotated_bearer_needs_its_own_usage_read(self):
        self.usage(self.t1)
        t2 = self.rotate()
        self.assertEqual(self.model(t2)[0], 503)
        self.assertEqual(self.upstream.count('model'), 0)
        self.assertEqual(self.usage(t2)[0], 200)
        self.assertEqual(self.model(t2)[0], 200)
        self.assertEqual(self.upstream.count('model'), 1)
        labels = [e['credential'] for e in self.gate.events if e['event'] == 'admission']
        self.assertEqual(labels, ['T2', 'T2'])

    def test_hold_and_rebind_admits_only_after_a_same_bearer_read(self):
        self.usage(self.t1)
        t2 = self.rotate()
        self.gate.rebind = lambda: threading.Thread(target=self.usage, args=(t2,)).start()
        self.gate.hold = 3
        status, body, _ = self.model(t2)
        self.assertEqual(status, 200)
        order = [(e['event'], e.get('credential'), e.get('admitted')) for e in self.gate.events
                 if e['event'] in ('admission', 'get', 'model_upstream')]
        self.assertEqual(order, [('get', 'T1', None), ('admission', 'T2', False), ('get', 'T2', None),
                                 ('admission', 'T2', True), ('model_upstream', 'T2', None)])

    def test_hold_expires_without_a_read(self):
        self.usage(self.t1)
        t2 = self.rotate()
        self.gate.rebind, self.gate.hold = (lambda: None), .3
        status, body, _ = self.model(t2)
        self.assertEqual((status, b'rebind-required' in body), (503, True))
        self.assertEqual(self.upstream.count('model'), 0)

    def test_identity_change_is_refused_before_upstream(self):
        self.usage(self.t1)
        tb, _ = self.upstream.issue(B, UB)
        self.assertEqual(self.model(tb, account=B)[0], 503)
        self.assertEqual(self.lease.paused, 'identity-changed')
        self.assertEqual(self.model(self.t1)[0], 503)
        self.assertEqual(self.upstream.count('model'), 0)

    def test_pause_closes_admitted_streams(self):
        self.usage(self.t1)
        self.upstream.held = True
        result = {}

        def stream():
            connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
            connection.request('POST', '/backend-api/codex/responses',
                               body=json.dumps({'model': 'gpt-5.4'}).encode(),
                               headers={'Authorization': 'Bearer ' + self.t1, 'ChatGPT-Account-ID': A})
            response = connection.getresponse()
            result['status'] = response.status
            try:
                result['body'] = response.read()
            except (OSError, http.client.HTTPException) as error:
                result['error'] = type(error).__name__
        worker = threading.Thread(target=stream)
        worker.start()
        deadline = time.monotonic() + 5
        while not self.gate.streams and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(len(self.gate.streams), 1)
        self.upstream.usage_override = lambda identity: (200, fixtures.usage_body(A, UA, five=85))
        self.usage(self.t1)  # a fresh read crosses the five-hour reserve
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.lease.paused, 'threshold:five_hour')
        self.assertNotIn(b'response.completed', result.get('body', b''))
        closed = [e for e in self.gate.events if e['event'] == 'streams_closed']
        self.assertEqual(closed[0]['count'], 1)
        self.assertEqual(self.model(self.t1)[0], 503)

    def test_failed_usage_is_relayed_and_does_not_bind(self):
        self.upstream.usage_override = lambda identity: (503, {'error': 'fixture outage'})
        self.assertEqual(self.usage(self.t1)[0], 503)
        self.assertFalse(self.lease.ready())
        self.assertEqual(self.model(self.t1)[0], 503)

    def test_unlisted_paths_are_refused_before_upstream(self):
        self.usage(self.t1)
        for path in ('/backend-api/codex/memories/trace_summarize', '/backend-api/codex/alpha/search',
                     '/backend-api/codex/images/generations', '/backend-api/codex/images/edits',
                     '/backend-api/codex/responses/compact', '/backend-api/wham/unknown'):
            status, body, _ = self.call('POST', path, self.t1, body={'model': 'gpt-5.4'})
            self.assertEqual((status, b'unlisted-path' in body), (503, True), path)
        status, body, _ = self.call('GET', '/backend-api/wham/unknown', self.t1)
        self.assertEqual((status, b'unlisted-path' in body), (503, True))
        kinds = {event['kind'] for event in self.upstream.events}
        self.assertEqual(kinds, {'usage'})  # nothing else reached the upstream
        self.assertEqual(len([e for e in self.gate.events if e['event'] == 'refused_unlisted']), 7)

    def test_failed_continuation_check_through_the_gate_stays_paused(self):
        self.upstream.usage_override = lambda identity: (200, fixtures.usage_body(A, UA, five=85))
        self.usage(self.t1)
        self.assertEqual(self.lease.paused, 'threshold:five_hour')
        self.upstream.usage_override = lambda identity: (503, {'error': 'fixture outage'})
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.assertEqual(self.usage(self.t1)[0], 503)  # the check it asked for fails: used up
        self.upstream.usage_override = None
        self.assertEqual(self.usage(self.t1)[0], 200)
        status, body, _ = self.model(self.t1)
        self.assertEqual((status, b'paused:awaiting-continuation' in body), (503, True))
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.assertEqual(self.usage(self.t1)[0], 200)
        self.assertEqual(self.model(self.t1)[0], 200)
        self.assertEqual(self.upstream.count('model'), 1)

    def test_connect_is_refused(self):
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as stream:
            stream.sendall(b'CONNECT auth.example.invalid:443 HTTP/1.1\r\nHost: auth.example.invalid:443\r\n\r\n')
            self.assertIn(b' 403 ', stream.recv(200))
        self.assertEqual([e['destination'] for e in self.gate.events if e['event'] == 'connect_refused'],
                         ['auth.example.invalid:443'])

    def test_records_hold_no_credential_values(self):
        self.usage(self.t1)
        self.model(self.t1)
        text = json.dumps(self.gate.events)
        self.assertNotIn(self.t1, text)
        self.assertNotIn(self.t1.split('.')[1], text)


if __name__ == '__main__':
    unittest.main()
