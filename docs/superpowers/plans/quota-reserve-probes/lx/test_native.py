"""LX with the real `codex app-server` 0.167.2 under the PC network guard (fixtures only).

Skips cleanly unless a native Codex binary (LX_NATIVE_CODEX, else `codex` on PATH)
reports the reviewed version, is the reviewed file (SHA-256) and the guard
self-check passes. Native gets a temporary HOME with a codex-as account made by
lib/codex-as and fabricated credentials, and a private runtime directory for its
workspace; the gate forwards to a loopback TLS server trusted through the
test-only context hook; refresh CONNECTs go to a loopback sink or are refused.
Every header native sends that reaches the server is checked for the project
name, the user and host name and home paths. Nothing reaches a real account,
credential or network.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import threading
import unittest

HERE = Path(os.path.realpath(__file__)).parent
spec = importlib.util.spec_from_file_location('lx_testkit', HERE / 'testkit.py')
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)
live, gates, qx = kit.live, kit.gates, kit.qx
A, U, B, V = kit.ACCOUNT, kit.USER, kit.OTHER_ACCOUNT, kit.OTHER_USER


def native_binary():
    candidate = os.environ.get('LX_NATIVE_CODEX') or shutil.which('codex')
    return os.path.realpath(candidate) if candidate and os.path.isfile(candidate) else None


def available():
    """(binary, reason): the binary when it is the reviewed version and the guard works here."""
    binary = native_binary()
    if binary is None:
        return None, 'native codex not found'
    if os.uname().sysname != 'Linux' or os.uname().machine != 'x86_64':
        return None, 'network guard is Linux x86_64 only'
    if not kit.openssl():
        return None, 'openssl command not found'
    if not live.guard_self_check(live.GUARD_COMMAND):
        return None, 'network guard self-check failed'
    root = kit.temp_root('native-version')
    try:
        home = root / 'home'
        home.mkdir(mode=0o700)
        version = live.native_version(live.GUARD_COMMAND, binary, {'HOME': str(home), 'PATH': '/usr/bin:/bin'})
    except live.Refused as error:
        return None, 'native version unavailable: ' + error.reason
    finally:
        shutil.rmtree(root, ignore_errors=True)
    if version != live.EXPECTED_VERSION:
        return None, 'native codex %s is not the reviewed %s' % (version, live.EXPECTED_VERSION)
    if live.sha256(binary) != live.EXPECTED_SHA256:
        return None, 'native codex %s is not the reviewed binary (SHA-256)' % version
    return binary, None


class Sink:
    """Loopback stand-in for the refresh host behind the CONNECT tunnel: reads, then closes."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen()
        self.dialed = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                connection, _ = self.sock.accept()
            except OSError:
                return
            with connection:
                connection.settimeout(2)
                try:
                    connection.recv(4096)
                except OSError:
                    pass

    def tunnel(self, host, port, timeout):
        self.dialed.append('%s:%d' % (host, port))
        return socket.create_connection(self.sock.getsockname(), timeout=timeout)

    def close(self):
        self.sock.close()


def usage_responder(body):
    data = json.dumps(body).encode()
    return lambda bearer: (200, [('Content-Type', 'application/json')], data)


def run_case(case, binary, certs, root):
    """One probe run with the real native. Returns (exit, result, stdout, stderr, home, server)."""
    server = kit.TlsUsageServer(*certs['leaf'])
    sink = Sink()
    try:
        lifetime = 120 if case == 'expiring' else 3600
        home = kit.Home(root, binary, lifetime=lifetime)
        upstream = kit.test_upstream(server, certs['ca'])
        tunnel, second, delay = None, False, 1.0
        if case == 'baseline':
            second = True
        elif case == 'identity-absent':
            server.respond = usage_responder({k: v for k, v in qx.usage_body(A, U).items()
                                              if k not in ('account_id', 'user_id')})
        elif case == 'identity-mismatch':
            server.respond = usage_responder(qx.usage_body(B, V))
        elif case == 'tls-failure':
            upstream = gates.Upstream('localhost', server.port, gates.production_context,
                                      resolver=kit.loopback_resolver)
        elif case == 'expiring':
            tunnel = sink.tunnel
        out, err = io.StringIO(), io.StringIO()
        runtime = root / 'run'
        runtime.mkdir(mode=0o700)
        with home.environment(XDG_RUNTIME_DIR=str(runtime)) as env:
            code = live.run_probe(home.alias, root / 'out', config=home.config, upstream=upstream, tunnel=tunnel,
                                  guard_command=live.GUARD_COMMAND, delay=delay, second=second, environ=env,
                                  stdout=out, stderr=err)
        path = root / 'out' / live.RESULT
        result = json.loads(path.read_text()) if path.exists() else None
        if result is not None:
            result.setdefault('fixture', {})['sink_dialed'] = list(sink.dialed)
            result['fixture']['upstream_requests'] = server.count()
            # What native sent upstream, reduced to the User-Agent product token and identifier labels.
            agents = [value for headers in server.sent for name, value in headers if name.lower() == 'user-agent']
            result['fixture']['upstream_user_agent_products'] = sorted({value.split('/', 1)[0] for value in agents})
            result['fixture']['upstream_identifier_findings'] = sorted({
                label for headers in server.sent
                for label in kit.identifier_findings(kit.header_text(headers), home.home, root)})
            result['fixture']['runtime_dir_left'] = sorted(os.listdir(runtime))
        return code, result, out.getvalue(), err.getvalue(), home
    finally:
        sink.close()
        server.close()


BINARY, REASON = available()
CASES = ('baseline', 'identity-absent', 'identity-mismatch', 'tls-failure', 'expiring')


@unittest.skipIf(BINARY is None, REASON or '')
class NativeProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs_root = kit.temp_root('native-certs')
        cls.certs = kit.make_ca(cls.certs_root / 'ca')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.certs_root, ignore_errors=True)

    def run_case(self, case):
        root = kit.temp_root('native-' + case)
        self.addCleanup(shutil.rmtree, root, True)
        code, result, stdout, stderr, home = run_case(case, BINARY, self.certs, root)
        self.assertIsNotNone(result, stderr)
        sha = result['native']['binary_sha256']
        texts = kit.outputs(root / 'out', stdout, stderr)
        secrets = home.secrets() + [os.uname().nodename]
        self.assertEqual(kit.scan(texts, secrets, allowed=(sha,)), [])
        self.assertEqual(result['native']['version'], '0.167.2')
        self.assertTrue(result['native']['reap']['process_group_empty'])
        self.assertEqual(result['native']['guard']['root_exit'] in (0, None), True, result['native'])
        self.assertTrue(result['credential']['link_intact_after'])
        self.assertTrue(result['credential']['still_private_regular_file'])
        self.assertEqual(result['rpc']['initialize'], 'ok')
        self.assertEqual(result['native']['user_agent_version'], '0.167.2')
        self.assertEqual(result['native']['binary_sha256'], live.EXPECTED_SHA256)
        self.assertEqual(result['fixture']['upstream_identifier_findings'], [])
        self.assertEqual(result['fixture']['runtime_dir_left'], [])  # the workspace is removed
        if result['fixture']['upstream_requests']:
            self.assertEqual(result['fixture']['upstream_user_agent_products'], ['usage-probe'])
        gate = result['gate']
        self.assertNotIn('POST model-request', gate['refused_by_category'])
        self.assertEqual(gate['refused_by_reason'].get('unarmed', 0), 0)  # no unsolicited usage read
        self.assertEqual(gate['refused_by_reason'].get('forward-cap', 0), 0)
        for method in result['rpc']['methods_sent']:
            self.assertIn(method, ('initialize', 'initialized', 'account/rateLimits/read'))
        return code, result, home

    def test_baseline_two_reads(self):
        code, result, home = self.run_case('baseline')
        self.assertEqual((code, result['outcome']), (0, 'completed'))
        self.assertEqual(result['rpc']['methods_sent'], ['initialize', 'initialized', 'account/rateLimits/read',
                                                         'account/rateLimits/read'])
        self.assertEqual([item['status'] for item in result['gate']['exchanges']], [200, 200])
        self.assertEqual(result['fixture']['upstream_requests'], 2)
        first = result['gate']['exchanges'][0]
        self.assertEqual(first['forwarded_header_names'], ['accept', 'authorization', 'chatgpt-account-id',
                                                           'user-agent'])
        self.assertEqual(first['request_header_names'], ['accept', 'authorization', 'chatgpt-account-id', 'host',
                                                         'user-agent'])
        identity = first['identity']
        self.assertTrue(all(value for key, value in identity.items() if key != 'bearer_exp_within_300s'), identity)
        native = result['rpc']['reads'][0]['native']
        self.assertEqual((native['rate_limits']['primary']['usedPercent'],
                          native['rate_limits']['primary']['windowDurationMins'],
                          native['rate_limits']['secondary']['windowDurationMins']), (25, 300, 10080))
        self.assertEqual((native['ordinary_usage_allowed'], native['account_id_equals_codex_as_account']), (True, True))
        self.assertTrue(result['freshness']['comparable'])
        self.assertFalse(result['credential']['changed_during_run'])
        self.assertEqual(home.secret.read_bytes(), home.raw.encode())

    def test_identity_absent(self):
        code, result, _ = self.run_case('identity-absent')
        self.assertEqual(code, 0)
        identity = result['gate']['exchanges'][0]['identity']
        self.assertEqual((identity['response_account_id_present'], identity['response_user_id_present']), (False, False))
        native = result['rpc']['reads'][0]['native']
        self.assertEqual((native['account_id_present'], native['ordinary_usage_allowed']), (False, None))
        self.assertEqual(result['gate']['exchanges'][0]['qx_lease_verdict'], 'unavailable:account-mismatch')

    def test_identity_mismatch(self):
        code, result, _ = self.run_case('identity-mismatch')
        self.assertEqual(code, 0)
        identity = result['gate']['exchanges'][0]['identity']
        self.assertTrue(identity['response_account_id_present'])
        self.assertFalse(identity['response_account_id_equals_bearer_account'])
        self.assertFalse(identity['response_user_id_equals_codex_as_user'])
        native = result['rpc']['reads'][0]['native']
        self.assertEqual((native['account_id_present'], native['account_id_equals_codex_as_account'],
                          native['ordinary_usage_allowed']), (True, False, None))

    def test_tls_failure_gives_native_an_error_and_no_fallback(self):
        code, result, _ = self.run_case('tls-failure')
        self.assertEqual(code, 0)
        self.assertEqual(result['gate']['exchanges'][0]['outcome'], 'tls_verification_failed')
        self.assertEqual(result['fixture']['upstream_requests'], 0)
        self.assertEqual(result['rpc']['reads'][0]['state'], 'error')
        self.assertEqual(result['rpc']['reads'][0]['error']['kind'], 'fetch-failed')
        self.assertEqual(result['rpc']['reads'][0]['error']['http_status'], 502)  # the gate's refusal

    def test_expiring_token_refresh_goes_only_through_the_allowlisted_tunnel(self):
        code, result, home = self.run_case('expiring')
        self.assertEqual(code, 0)
        connect = result['gate']['connect']
        # The sink fails every refresh, so native retries; the tunnel cap bounds it and the rest are refused.
        self.assertEqual(connect['tunnelled'] + connect['failed'], gates.MAX_TUNNELS, connect)
        self.assertEqual(set(connect['refused']), {'auth.openai.com:443'})
        self.assertEqual(set(result['fixture']['sink_dialed']), {'auth.openai.com:443'})
        self.assertGreater(connect['bytes_up'], 0)  # native's TLS ClientHello, never inspected
        self.assertTrue(result['gate']['exchanges'][0]['identity']['bearer_exp_within_300s'])
        self.assertFalse(result['credential']['changed_during_run'])  # the sink cannot refresh
        self.assertEqual(home.secret.read_bytes(), home.raw.encode())


if __name__ == '__main__':
    unittest.main()
