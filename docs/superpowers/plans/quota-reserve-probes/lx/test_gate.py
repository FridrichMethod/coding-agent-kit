"""LX gate tests: allowlist, arming and the 2-forward cap, model POST refusal, identity booleans,
TLS verification (no fallback), redirects, limits, CONNECT allowlist, and no secret in records.

Loopback only: a TLS usage server with a temporary CA (the test-only context hook) and an
in-process client standing in for native.
"""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import threading
import time
import unittest

HERE = Path(os.path.realpath(__file__)).parent
spec = importlib.util.spec_from_file_location('lx_testkit', HERE / 'testkit.py')
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)
gates, qx = kit.gates, kit.qx
A, U, B, V = kit.ACCOUNT, kit.USER, kit.OTHER_ACCOUNT, kit.OTHER_USER


@unittest.skipUnless(kit.openssl(), 'openssl command not found; TLS fixtures need it')
class GateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = kit.temp_root('gate')
        cls.certs = kit.make_ca(cls.root / 'ca')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.server = kit.TlsUsageServer(*self.certs['leaf'])
        self.bearer = qx.token(A, U)
        self.gate = self.make_gate(kit.test_upstream(self.server, self.certs['ca']))

    def tearDown(self):
        self.gate.close()
        self.server.close()

    def make_gate(self, upstream, **kwargs):
        gate = gates.Gate(upstream, expected=(U, A), **kwargs)
        self.addCleanup(gate.close)
        return gate

    def call(self, method, path, bearer=None, account=A, body=None, gate=None, headers=None):
        gate = gate or self.gate
        connection = http.client.HTTPConnection('127.0.0.1', gate.server.server_port, timeout=20)
        sent = {'Accept': 'application/json', 'User-Agent': 'lx-test/0.167.2'}
        if bearer is not None:
            sent['Authorization'] = 'Bearer ' + bearer
        if account is not None:
            sent['ChatGPT-Account-ID'] = account
        sent.update(headers or {})
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data, dict(response.getheaders())

    def usage(self, bearer=None, account=A, gate=None):
        return self.call('GET', gates.USAGE_PATH, self.bearer if bearer is None else bearer, account, gate=gate)

    def assert_clean(self, gate=None):
        text = json.dumps((gate or self.gate).summary())
        for value in (self.bearer, A, U, B, V, self.bearer.split('.')[1]):
            self.assertNotIn(value, text)
        self.assertEqual(kit.live.leaks(text), [])

    def test_armed_usage_is_forwarded_over_verified_tls_with_allowlisted_headers(self):
        self.assertTrue(self.gate.arm())
        status, data, _ = self.call('GET', gates.USAGE_PATH, self.bearer, A, headers={'X-Extra': 'not forwarded'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)['plan_type'], 'pro')
        method, path, names, bearer = self.server.requests[0]
        self.assertEqual((method, path, bearer == self.bearer), ('GET', gates.USAGE_PATH, True))
        self.assertEqual(names, ['accept', 'accept-encoding', 'authorization', 'chatgpt-account-id', 'connection',
                                 'host', 'user-agent'])
        record = self.gate.summary()['exchanges'][0]
        self.assertEqual((record['outcome'], record['status'], record['index']), ('relayed', 200, 1))
        self.assertIn('x-extra', record['request_header_names'])
        self.assertNotIn('x-extra', record['forwarded_header_names'])
        self.assertTrue(record['tls_version'].startswith('TLSv1.'))
        window = record['usage']['rate_limit']['primary_window']
        self.assertEqual((window['used_percent'], window['minutes']), (25, 300))
        self.assertEqual(record['qx_lease_verdict'] if 'qx_lease_verdict' in record else None, None)
        self.assert_clean()

    def test_unarmed_reads_and_the_cap_are_refused_before_upstream(self):
        status, data, _ = self.usage()
        self.assertEqual((status, json.loads(data)['error']['message'][-7:]), (503, 'unarmed'))
        self.assertTrue(self.gate.arm())
        self.assertEqual(self.usage()[0], 200)
        self.assertEqual(self.usage()[0], 503)  # one forward per arm
        self.assertTrue(self.gate.arm())
        self.assertEqual(self.usage()[0], 200)
        self.assertFalse(self.gate.arm())  # MAX_FORWARDS used
        self.assertEqual(self.usage()[0], 503)
        self.assertEqual(self.server.count(), 2)
        summary = self.gate.summary()
        self.assertEqual(summary['forwarded'], 2)
        self.assertEqual(summary['refused_by_reason'], {'forward-cap': 1, 'unarmed': 2})

    def test_model_requests_and_unlisted_paths_are_refused_with_503_never_401(self):
        self.gate.arm()
        cases = [('POST', '/backend-api/codex/responses', b'{"model":"gpt-fixture","input":[]}'),
                 ('POST', '/backend-api/codex/memories/trace_summarize', b'{}'),
                 ('GET', '/backend-api/codex/models?client_version=0.167.2', None),
                 ('GET', '/backend-api/wham/accounts/check', None),
                 ('GET', '/backend-api/wham/rate-limit-reset-credits', None),
                 ('GET', '/backend-api/wham/tasks/some-id', None),
                 ('PUT', gates.USAGE_PATH, b'{}'), ('DELETE', gates.USAGE_PATH, None)]
        for method, path, body in cases:
            status, data, _ = self.call(method, path, self.bearer, body=body)
            self.assertEqual(status, 503, (method, path))
            self.assertEqual(json.loads(data)['error']['code'], 'quota_reserve_lx_gate')
        self.assertEqual(self.server.count(), 0)
        self.assertEqual(self.gate.armed, 1)  # refusals do not use the arm
        summary = self.gate.summary()
        self.assertEqual(summary['refused_by_category'], {
            'DELETE usage': 1, 'GET accounts-check': 1, 'GET models': 1, 'GET reset-credits': 1, 'GET unlisted': 1,
            'POST model-adjacent': 1, 'POST model-request': 1, 'PUT usage': 1})
        self.assertEqual(summary['refused_by_reason']['model-request'], 1)
        self.assertNotIn('some-id', json.dumps(summary))

    def test_usage_with_query_body_or_without_bearer_is_refused(self):
        self.gate.arm()
        self.assertEqual(self.call('GET', gates.USAGE_PATH + '?x=1', self.bearer)[0], 503)
        self.assertEqual(self.call('GET', gates.USAGE_PATH, self.bearer, body=b'{}')[0], 503)
        self.assertEqual(self.call('GET', gates.USAGE_PATH, None)[0], 503)
        self.assertEqual(self.call('GET', gates.USAGE_PATH, None, headers={'Authorization': 'Basic abc'})[0], 503)
        self.assertEqual(self.server.count(), 0)
        self.assertEqual(self.gate.summary()['refused_by_reason'],
                         {'missing-bearer': 2, 'unexpected-request-shape': 2})

    def identity_for(self, body, bearer=None, account=A):
        self.server.respond = lambda _bearer: (200, [('Content-Type', 'application/json')], json.dumps(body).encode())
        self.gate.arm()
        self.assertEqual(self.usage(bearer, account)[0], 200)
        return self.gate.summary()['exchanges'][-1]['identity']

    def test_identity_booleans_present_equal(self):
        identity = self.identity_for(qx.usage_body(A, U))
        self.assertTrue(all(identity.values()) or identity['bearer_exp_within_300s'] is False)
        self.assertFalse(identity['bearer_exp_within_300s'])
        self.assertTrue(identity['response_account_id_equals_bearer_account'])
        self.assertTrue(identity['response_user_id_equals_codex_as_user'])

    def test_identity_booleans_absent(self):
        body = {k: v for k, v in qx.usage_body(A, U).items() if k not in ('account_id', 'user_id')}
        identity = self.identity_for(body)
        self.assertEqual((identity['response_account_id_present'], identity['response_user_id_present']), (False, False))
        self.assertFalse(identity['response_account_id_equals_bearer_account'])
        self.assertFalse(identity['response_user_id_equals_codex_as_user'])
        self.assertTrue(identity['bearer_account_equals_codex_as_account'])

    def test_identity_booleans_mismatch(self):
        identity = self.identity_for(qx.usage_body(B, V))
        self.assertTrue(identity['response_account_id_present'] and identity['response_user_id_present'])
        for key in ('response_account_id_equals_bearer_account', 'response_user_id_equals_bearer_user',
                    'response_account_id_equals_codex_as_account', 'response_user_id_equals_codex_as_user',
                    'response_account_id_equals_request_header'):
            self.assertFalse(identity[key], key)
        identity = self.identity_for(qx.usage_body(A, V))
        self.assertTrue(identity['response_account_id_equals_bearer_account'])
        self.assertFalse(identity['response_user_id_equals_bearer_user'])

    def test_bearer_of_another_login_and_header_mismatch(self):
        other = qx.token(B, V)
        identity = self.identity_for(qx.usage_body(B, V), bearer=other, account=B)
        self.assertTrue(identity['response_account_id_equals_bearer_account'])
        self.assertFalse(identity['bearer_account_equals_codex_as_account'])
        self.assertFalse(identity['response_account_id_equals_codex_as_account'])
        identity = self.identity_for(qx.usage_body(A, U), account=B)
        self.assertFalse(identity['request_account_header_equals_bearer_account'])
        self.assertFalse(identity['response_account_id_equals_request_header'])
        record = self.gate.summary()['exchanges'][-1]
        self.assertFalse(record['bearer_same_as_first_forward'])
        for value in (other, B, V):
            self.assertNotIn(value, json.dumps(self.gate.summary()))

    def test_expiring_bearer_is_flagged(self):
        self.bearer = qx.token(A, U, lifetime=120)
        identity = self.identity_for(qx.usage_body(A, U))
        self.assertTrue(identity['bearer_exp_in_future'])
        self.assertTrue(identity['bearer_exp_within_300s'])

    def test_untrusted_certificate_refuses_without_fallback(self):
        # Production context (system store) against the temporary CA: verification must fail.
        gate = self.make_gate(gates.Upstream('localhost', self.server.port, gates.production_context,
                                             resolver=kit.loopback_resolver))
        gate.arm()
        status, data, headers = self.usage(gate=gate)
        self.assertEqual(status, 502)
        self.assertIn(b'upstream-tls-verification-failed', data)
        record = gate.summary()['exchanges'][0]
        self.assertEqual((record['outcome'], record['phase'], record['status']), ('tls_verification_failed', 'tls', None))
        self.assertEqual(self.server.count(), 0)  # no HTTP request reached the server, TLS or plain
        deadline = time.monotonic() + 3  # the server thread may record the failed handshake later
        while self.server.handshake_failures < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(self.server.handshake_failures, 1)
        self.assertEqual(gate.forwards, 1)  # the failed attempt counts toward the cap
        gate.arm()
        self.assertEqual(self.usage(gate=gate)[0], 502)
        self.assertFalse(gate.arm())

    def test_hostname_mismatch_refuses(self):
        wrong = kit.TlsUsageServer(*self.certs['wrong'])
        self.addCleanup(wrong.close)
        gate = self.make_gate(kit.test_upstream(wrong, self.certs['ca']))
        gate.arm()
        self.assertEqual(self.usage(gate=gate)[0], 502)
        self.assertEqual(gate.summary()['exchanges'][0]['outcome'], 'tls_verification_failed')
        self.assertEqual(wrong.count(), 0)

    def test_unverified_context_is_refused_before_connecting(self):
        def insecure():
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            return context
        gate = self.make_gate(gates.Upstream('localhost', self.server.port, insecure, resolver=kit.loopback_resolver))
        gate.arm()
        self.assertEqual(self.usage(gate=gate)[0], 502)
        self.assertEqual(gate.summary()['exchanges'][0]['outcome'], 'unverified_context_refused')
        self.assertEqual((self.server.count(), self.server.handshake_failures), (0, 0))

    def test_redirect_is_neither_followed_nor_relayed(self):
        self.server.respond = lambda _b: (302, [('Location', 'https://chatgpt.com/elsewhere')], b'')
        self.gate.arm()
        status, _, headers = self.usage()
        self.assertEqual(status, 502)
        self.assertNotIn('Location', headers)
        self.assertEqual(self.gate.summary()['exchanges'][0]['outcome'], 'redirect_refused')
        self.assertEqual(self.server.count(), 1)

    def test_oversized_body_is_refused(self):
        big = json.dumps({'plan_type': 'pro', 'pad': 'x' * (gates.BODY_LIMIT + 10)}).encode()
        self.server.respond = lambda _b: (200, [('Content-Type', 'application/json')], big)
        self.gate.arm()
        self.assertEqual(self.usage()[0], 502)
        self.assertEqual(self.gate.summary()['exchanges'][0]['outcome'], 'response_too_large')

    def test_slow_upstream_hits_the_total_deadline(self):
        gate = self.make_gate(kit.test_upstream(self.server, self.certs['ca']), total=1.0)
        self.server.delay = 3
        gate.arm()
        started = time.monotonic()
        self.assertEqual(self.usage(gate=gate)[0], 502)
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(gate.summary()['exchanges'][0]['outcome'], 'deadline_exceeded')

    def test_non_json_and_error_statuses_are_relayed_but_recorded_without_body(self):
        self.server.respond = lambda _b: (401, [('Content-Type', 'text/html'), ('Set-Cookie', 'cf=secret-cookie')],
                                          b'<html>token_expired fixture-secret-text</html>')
        self.gate.arm()
        status, data, _ = self.usage()
        self.assertEqual(status, 401)
        record = self.gate.summary()['exchanges'][0]
        self.assertEqual((record['body_json'], record['body_problem'], record['content_type_is_json']),
                         (False, 'not-json', False))
        self.assertIn('set-cookie', record['response_header_names'])
        text = json.dumps(self.gate.summary())
        self.assertNotIn('secret-cookie', text)
        self.assertNotIn('fixture-secret-text', text)

    def test_connect_is_tunnelled_only_to_the_allowlisted_refresh_host(self):
        echo = socket.socket()
        echo.bind(('127.0.0.1', 0))
        echo.listen()
        self.addCleanup(echo.close)

        def serve():
            connection, _ = echo.accept()
            with connection:
                connection.sendall(connection.recv(64))
        threading.Thread(target=serve, daemon=True).start()
        dialed = []

        def tunnel(host, port, timeout):
            dialed.append((host, port))
            return socket.create_connection(echo.getsockname(), timeout=timeout)
        gate = self.make_gate(kit.test_upstream(self.server, self.certs['ca']), tunnel=tunnel)
        for destination, expected in (('auth.openai.com:443', '200'), ('evil.example:443', '403'),
                                      ('chatgpt.com:443', '403'), ('bad_host:443', '403')):
            with socket.create_connection(('127.0.0.1', gate.server.server_port), timeout=5) as sock:
                sock.sendall(('CONNECT %s HTTP/1.1\r\nHost: x\r\n\r\n' % destination).encode())
                reply = sock.recv(256)
                self.assertIn((' %s ' % expected).encode(), reply.split(b'\r\n', 1)[0] + b' ', destination)
                if expected == '200':
                    if b'\r\n\r\n' not in reply:
                        sock.recv(256)
                    sock.sendall(b'tunnel-bytes')
                    self.assertEqual(sock.recv(64), b'tunnel-bytes')
        deadline = time.monotonic() + 3
        while gate.summary()['connect']['bytes_up'] == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        connect = gate.summary()['connect']
        self.assertEqual(dialed, [('auth.openai.com', 443)])
        self.assertEqual((connect['tunnelled'], connect['bytes_up'], connect['bytes_down']), (1, 12, 12))
        self.assertEqual(connect['refused'], {'<malformed>': 1, 'chatgpt.com:443': 1, 'evil.example:443': 1})
        self.assertEqual(connect['allowlist'], ['auth.openai.com:443'])

    def test_without_a_tunnel_every_connect_is_refused(self):
        with socket.create_connection(('127.0.0.1', self.gate.server.server_port), timeout=5) as sock:
            sock.sendall(b'CONNECT auth.openai.com:443 HTTP/1.1\r\nHost: x\r\n\r\n')
            self.assertIn(b' 403 ', sock.recv(256) + b' ')
        self.assertEqual(self.gate.summary()['connect']['refused'], {'auth.openai.com:443': 1})

    def test_qx_lease_verdict_is_a_fixed_code(self):
        gate = self.make_gate(kit.test_upstream(self.server, self.certs['ca']),
                              verdict=kit.live.qx_verdict((U, A)))
        gate.arm()
        self.assertEqual(self.usage(gate=gate)[0], 200)
        self.assertEqual(gate.summary()['exchanges'][0]['qx_lease_verdict'], 'observation:five_hour+weekly')
        self.server.respond = lambda _b: (200, [('Content-Type', 'application/json')],
                                          json.dumps(qx.usage_body(B, V)).encode())
        gate.arm()
        self.usage(gate=gate)
        self.assertEqual(gate.summary()['exchanges'][1]['qx_lease_verdict'], 'unavailable:account-mismatch')


class ProductionConstants(unittest.TestCase):
    def test_production_upstream_is_the_backend_with_the_system_store(self):
        upstream = gates.production_upstream()
        self.assertEqual((upstream.host, upstream.port), ('chatgpt.com', 443))
        self.assertIs(upstream.context_factory, gates.production_context)
        self.assertIs(upstream.resolver, socket.getaddrinfo)
        context = gates.production_context()
        self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))
        self.assertIs(gates.production_tunnel.__module__, gates.__name__)
        self.assertEqual(gates.TUNNEL_HOSTS, ('auth.openai.com:443',))
        self.assertEqual((gates.MAX_FORWARDS, gates.USAGE_PATH), (2, '/backend-api/wham/usage'))

    def test_normalized_usage_keeps_no_strings_but_the_plan(self):
        body = qx.usage_body(A, U, additional_rate_limits=[
            {'limit_name': 'gpt-secret-slug', 'metered_feature': 'codex_secret', 'rate_limit': {
                'allowed': True, 'limit_reached': False, 'primary_window': {
                    'used_percent': 'x', 'limit_window_seconds': 18000, 'reset_after_seconds': 1, 'reset_at': 2}}}],
            credits={'balance': 'secret-balance'})
        body['plan_type'] = 'user-ABC123'
        text = json.dumps(gates.normalize_usage(body))
        for value in (A, U, 'gpt-secret-slug', 'codex_secret', 'secret-balance', 'user-ABC123'):
            self.assertNotIn(value, text)
        view = json.loads(text)
        self.assertEqual(view['plan_type'], '<unrecognized>')
        self.assertEqual(view['additional_rate_limits'][0]['rate_limit']['primary_window']['used_percent'],
                         {'type': 'str'})
        self.assertTrue(view['present']['credits'])


if __name__ == '__main__':
    unittest.main()
