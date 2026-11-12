"""Offline tests for the KM probes (research only; Linux or macOS, loopback fixtures only).

python3 -I -B -m unittest discover -s docs/superpowers/plans/quota-reserve-probes/km -p test_km.py

They cover the launcher's pure refusals, profile shape and control round trip without a gate,
the gate route table and selector rules, both composed adapters against loopback fixtures (no
native, no Seatbelt), an established CONNECT tunnel through the real gate handler being cut by a
latch and by monitor loss, the driver's sanitizing and Claude temp-entry matching, and the
evidence checker: every committed stage passes, and each targeted mutation of a verdict-bearing
row makes it fail. No native code is started and nothing leaves 127.0.0.1.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest

HERE = Path(os.path.realpath(__file__)).parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LAUNCHER = load('km_t_launcher', HERE / 'launcher.py')
GATE = load('km_t_gate', HERE / 'gate.py')
FX = load('km_t_fixtures', HERE / 'fixtures.py')
CHECK = load('km_t_check', HERE / 'check_evidence.py')
RUN = load('km_t_run', HERE / 'run.py')
SBPL = load('km_t_sbpl', HERE.parent / 'nm' / 'sbpl.py')


class LauncherChecks(unittest.TestCase):
    def test_versions_are_minimums(self):
        self.assertEqual(LAUNCHER.check_version('claude', '2.1.322 (Claude Code)'), (2, 1, 322))
        self.assertEqual(LAUNCHER.check_version('claude', '2.2.0 (Claude Code)'), (2, 2, 0))
        self.assertEqual(LAUNCHER.check_version('codex', 'codex-cli 0.167.2'), (0, 167, 2))
        for provider, text in (('claude', '2.1.300 (Claude Code)'), ('codex', 'codex-cli 0.150.0'),
                               ('claude', 'garbage'), ('codex', '')):
            with self.assertRaises(LAUNCHER.Refusal):
                LAUNCHER.check_version(provider, text)

    def test_args(self):
        self.assertEqual(LAUNCHER.check_args('codex', ['hello']), ['--no-daemon', 'hello'])
        self.assertEqual(LAUNCHER.check_args('codex', ['--no-daemon', 'x']), ['--no-daemon', 'x'])
        self.assertEqual(LAUNCHER.check_args('codex', ['hello', 'resume']), ['--no-daemon', 'hello', 'resume'])
        self.assertEqual(LAUNCHER.check_args('codex', ['--config=model="m"', 'fix', 'exec']),
                         ['--no-daemon', '--config=model="m"', 'fix', 'exec'])
        for argv in (['resume'], ['--profile', 'p'], ['-c', 'chatgpt_base_url="x"'], ['--config=model_providers.a=1'],
                     ['app-server'], ['exec', 'prompt'], ['e', 'prompt'], ['review'], ['-m', 'gpt', 'resume'],
                     ['-c', 'model="m"', 'exec', 'x'], ['--no-alt-screen', 'review']):
            with self.assertRaises(LAUNCHER.Refusal, msg=argv):
                LAUNCHER.check_args('codex', argv)
        self.assertEqual(LAUNCHER.check_args('claude', ['KM prompt']), ['KM prompt'])
        for argv in (['-p'], ['--settings', '{}'], ['--print'], ['--dangerously-skip-permissions']):
            with self.assertRaises(LAUNCHER.Refusal):
                LAUNCHER.check_args('claude', argv)

    def test_env_and_policy(self):
        LAUNCHER.check_env('claude', {'HOME': '/x'})
        for provider, name in (('claude', 'ANTHROPIC_API_KEY'), ('claude', 'CLAUDE_CODE_OAUTH_TOKEN'),
                               ('claude', 'ANTHROPIC_BASE_URL'), ('codex', 'OPENAI_API_KEY'),
                               ('codex', 'CODEX_REFRESH_TOKEN_URL_OVERRIDE')):
            with self.assertRaises(LAUNCHER.Refusal):
                LAUNCHER.check_env(provider, {name: ''})
        good = {'family': 'codex', 'reserves': {'five_hour': 30, 'weekly': None}, 'max_age_s': 8}
        self.assertEqual(LAUNCHER.check_policy('codex', good), {'five_hour': 30})
        bad = [dict(good, family='claude-subscription-general'), dict(good, reserves={'five_hour': None}),
               dict(good, reserves={'five_hour': 100}), dict(good, reserves={'monthly': 5}),
               dict(good, max_age_s=0), dict(good, reserves={'five_hour': True})]
        for policy in bad:
            with self.assertRaises(LAUNCHER.Refusal):
                LAUNCHER.check_policy('codex', policy)

    def test_host(self):
        with self.assertRaises(LAUNCHER.Refusal):
            LAUNCHER.check_host('/usr/bin/sandbox-exec', platform='linux')
        with self.assertRaises(LAUNCHER.Refusal):
            LAUNCHER.check_host('/nonexistent/sandbox-exec', platform='darwin')

    def test_proxy(self):
        self.assertEqual(LAUNCHER.parse_proxy('http://127.0.0.1:3128'), ['127.0.0.1', 3128])
        self.assertEqual(LAUNCHER.parse_proxy('http://proxy.example.invalid:8080/'), ['proxy.example.invalid', 8080])
        for bad in ('https://p:1', 'socks5://p:1', 'http://p', 'http://u:pw@p:1', 'http://p:1/x', 'p:1',
                    'http://p:99999'):
            with self.assertRaises(LAUNCHER.Refusal):
                LAUNCHER.parse_proxy(bad)

    def test_ifconfig(self):
        text = ('lo0: flags\n\tinet 127.0.0.1 netmask 0xff000000\n\tinet6 ::1 prefixlen 128\n'
                '\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1\nen0: flags\n\tinet 192.0.2.7 netmask 0xffffff00\n'
                '\tinet6 2001:db8::7 prefixlen 64\n')
        self.assertEqual(LAUNCHER.parse_ifconfig(text), [('inet6', 'fe80::1%lo0'), ('inet', '192.0.2.7'),
                                                         ('inet6', '2001:db8::7')])

    def test_profile_shape(self):
        text = SBPL.render(LAUNCHER.profile_policy('qkm-abcdef', 4242, ['/private/var/at']))
        self.assertIn('(allow network-outbound (remote tcp "localhost:4242"))', text)
        self.assertNotIn('remote ip', text)
        self.assertIn('(global-name "com.apple.SecurityServer")', text)
        self.assertIn('(deny file-ioctl (ioctl-command TIOCSTI)', text)
        self.assertIn('(allow signal (target same-sandbox))', text)
        for escape in SBPL.ESCAPE_SERVICES:
            self.assertNotIn('"%s"' % escape, text)
        without = SBPL.render(LAUNCHER.profile_policy('qkm-abcdef', 4242, [], store=False))
        self.assertNotIn('SecurityServer', without)
        rules = LAUNCHER.fs_rules(['/private/tmp/w'], ['/private/var/at'])
        self.assertTrue(rules.startswith('(deny file-write* (subpath "/"))'))
        self.assertIn('(subpath "/private/tmp/w")', rules)
        self.assertIn('/dev/ptmx', rules)
        self.assertTrue(rules.rstrip().endswith('(deny file-write* (subpath "/private/var/at"))'))

    def test_continuation_without_gate_stays_paused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for provider in ('claude', 'codex'):
                launcher = LAUNCHER.Launcher({'case_dir': tmp, 'provider': provider})
                said = []
                launcher.say = said.append
                launcher.control = None  # the gate died and its channel hit EOF
                self.assertFalse(launcher.check())
                a, b = socket.socketpair()
                b.close()
                launcher.control = a  # a broken channel during a restart
                self.assertEqual(launcher.request('continue')['reason'], 'gate_unavailable')
                self.assertIsNone(launcher.control)
                a.close()
                self.assertEqual(said, ['still paused: gate_unavailable'])
                launcher.log.close()

    def test_self_test_verdict(self):
        good = {'gate': 'connected', 'other': 'errno:1', 'bind': 'errno:1', 'tiocsti': 'errno:1'}
        self.assertTrue(LAUNCHER.self_test_ok(good))
        for key, value in (('gate', 'errno:61'), ('other', 'connected'), ('other', 'errno:61'),
                           ('bind', 'bound'), ('tiocsti', 'injected'), ('tiocsti', 'errno:13')):
            self.assertFalse(LAUNCHER.self_test_ok(dict(good, **{key: value})))
        self.assertFalse(LAUNCHER.self_test_ok({'error': 'TimeoutExpired'}))


class GatePure(unittest.TestCase):
    def test_routes(self):
        self.assertEqual(GATE.route('claude', 'POST', '/v1/messages'), 'model')
        self.assertIsNone(GATE.route('claude', 'GET', '/v1/messages'))
        self.assertIsNone(GATE.route('claude', 'POST', '/v1/messages/count_tokens'))
        self.assertEqual(GATE.route('codex', 'POST', '/backend-api/codex/responses'), 'model')
        self.assertEqual(GATE.route('codex', 'GET', '/backend-api/wham/usage'), 'usage')
        self.assertEqual(GATE.route('codex', 'GET', '/backend-api/codex/models'), 'read')
        self.assertIsNone(GATE.route('codex', 'POST', '/backend-api/codex/memories'))
        self.assertEqual(GATE.route('codex', 'CONNECT', 'x:443'), 'connect')

    def test_connect_allowed(self):
        allow = {'platform.claude.com:443': ('127.0.0.1', 1)}
        self.assertTrue(GATE.connect_allowed('platform.claude.com:443', allow, True, None))
        self.assertFalse(GATE.connect_allowed('platform.claude.com:443', allow, False, None))
        self.assertFalse(GATE.connect_allowed('platform.claude.com:443', allow, True, 'monitor_eof'))
        self.assertFalse(GATE.connect_allowed('api.anthropic.com:443', allow, True, None))
        self.assertFalse(GATE.connect_allowed('platform.claude.com:8443', allow, True, None))

    def test_codex_selectors(self):
        a, acct = ('Authorization', 'Bearer tok'), ('ChatGPT-Account-ID', 'acct-a')
        self.assertEqual(GATE.codex_selectors([a, acct]), ('tok', 'acct-a'))
        self.assertEqual(GATE.codex_selectors([a]), ('tok', None))
        for headers in ([a, acct, ('chatgpt-account-id', 'acct-b')], [a, a, acct], [acct],
                        [('Authorization', 'Basic x'), acct]):
            self.assertIsNone(GATE.codex_selectors(headers))

    def test_labels(self):
        labels = GATE.Labels({'A': 'KMFAKE-A-0123456789abcdef'})
        self.assertEqual(labels('KMFAKE-A-0123456789abcdef'), 'A')
        self.assertEqual(labels('other-token-value-xx'), 'unlabelled')
        self.assertIsNone(labels(None))

    def test_read_response(self):
        a, b = socket.socketpair()
        a.sendall(b'HTTP/1.0 401 X\r\nContent-Type: application/json\r\n\r\n{"e":1}')
        a.close()
        self.assertEqual(GATE.read_response(b), (401, b'{"e":1}'))
        b.close()


class Collect:
    def __init__(self):
        self.rows = []

    def __call__(self, event, **fields):
        self.rows.append(dict(event=event, t=time.monotonic(), **fields))


class CodexAdapter(unittest.TestCase):
    def setUp(self):
        self.up = FX.Upstream()
        self.access, _ = self.up.add_codex('A', 'acct-a', 'user-a')
        self.other, _ = self.up.add_codex('B', 'acct-b', 'user-b')
        self.emit = Collect()
        config = {'provider': 'codex', 'upstream_port': self.up.port, 'expected_account': 'acct-a',
                  'expected_user': 'user-a', 'labels': {'A': self.access, 'B': self.other},
                  'policy': {'reserves': {'five_hour': 30, 'weekly': None}, 'max_age_s': 30}}
        self.gate = GATE.Gate(config, self.emit)
        self.closed = []

    def tearDown(self):
        self.up.close()

    def headers(self, token, account):
        return [('Authorization', 'Bearer ' + token), ('ChatGPT-Account-ID', account)]

    def test_rebind_threshold_and_continuation(self):
        adapter = self.gate.adapter
        body = json.dumps({'model': 'gpt-5.4'}).encode()
        ok, reason, stream = adapter.admit(self.headers(self.access, 'acct-a'), body, lambda: self.closed.append(1))
        self.assertTrue(ok, reason)
        self.assertTrue(any(r['event'] == 'controller' and r['kind'] == 'rotation_hold' for r in self.emit.rows))
        self.assertEqual(self.up.snapshot()[0]['path'], '/backend-api/wham/usage')
        self.up.set_usage('acct-a', five=80)
        self.assertEqual(adapter.poll(), 'fresh')
        self.assertEqual(self.closed, [1])
        ok, reason, _ = adapter.admit(self.headers(self.access, 'acct-a'), body, lambda: None)
        self.assertFalse(ok)
        self.assertIn('threshold', reason)
        self.up.set_usage('acct-a', five=10)
        adapter.poll()
        self.assertFalse(adapter.admit(self.headers(self.access, 'acct-a'), body, lambda: None)[0])
        self.assertTrue(adapter.continue_check()['needs_read'])
        adapter.poll()  # the next fresh passing read answers the armed continuation
        self.assertTrue(adapter.admit(self.headers(self.access, 'acct-a'), body, lambda: None)[0])

    def test_foreign_account_latches(self):
        adapter = self.gate.adapter
        body = b'{"model": "gpt-5.4"}'
        self.assertTrue(adapter.admit(self.headers(self.access, 'acct-a'), body, lambda: None)[0])
        ok, reason, _ = adapter.admit(self.headers(self.other, 'acct-b'), body, lambda: None)
        self.assertFalse(ok)
        self.assertEqual(reason, 'identity-changed')
        self.assertFalse(adapter.admit(self.headers(self.access, 'acct-a'), body, lambda: None)[0])

    def test_duplicate_selectors_refused_before_admission(self):
        adapter = self.gate.adapter
        body = b'{"model": "gpt-5.4"}'
        self.assertTrue(adapter.admit(self.headers(self.access, 'acct-a'), body, lambda: None)[0])
        posts = len(self.up.snapshot())
        ok, reason, _ = adapter.admit(self.headers(self.access, 'acct-a') + [('ChatGPT-Account-ID', 'acct-b')],
                                      body, lambda: None)
        self.assertEqual((ok, reason), (False, 'auth_selector_conflict'))
        self.assertEqual(len(self.up.snapshot()), posts)  # nothing reached upstream
        self.assertFalse(adapter.admit(self.headers(self.access, 'acct-a'), body, lambda: None)[0])

    def test_loss_is_final_and_closes_connect(self):
        self.gate.allow = {'auth.openai.com:443': ('127.0.0.1', 9)}
        adapter = self.gate.adapter
        self.assertTrue(adapter.admit(self.headers(self.access, 'acct-a'), b'{"model":"m"}', lambda: None)[0])
        self.assertTrue(GATE.connect_allowed('auth.openai.com:443', self.gate.allow, adapter.is_open(), self.gate.lost))
        self.gate.lose('monitor_eof')
        self.assertFalse(GATE.connect_allowed('auth.openai.com:443', self.gate.allow, adapter.is_open(),
                                              self.gate.lost))
        self.assertEqual(self.gate.status()['lost'], 'monitor_eof')

    def test_restart_starts_paused(self):
        adapter = self.gate.adapter
        adapter.restart_paused()
        ok, reason, _ = adapter.admit(self.headers(self.access, 'acct-a'), b'{"model":"m"}', lambda: None)
        self.assertFalse(ok)
        self.assertIn('gate_restart', reason)


class TunnelClosure(unittest.TestCase):
    """An established refresh CONNECT tunnel through the real gate handler is cut by a latch and
    by monitor loss: the allowlisted loopback sink receives no byte after either."""

    def setUp(self):
        self.up = FX.Upstream()
        self.access, _ = self.up.add_codex('A', 'acct-a', 'user-a')
        self.sink = socket.socket()
        self.sink.bind(('127.0.0.1', 0))
        self.sink.listen(4)
        self.received, self.sink_eof = [], threading.Event()
        threading.Thread(target=self._sink, daemon=True).start()
        config = {'provider': 'codex', 'upstream_port': self.up.port, 'expected_account': 'acct-a',
                  'expected_user': 'user-a', 'labels': {'A': self.access},
                  'connect_allow': {'auth.openai.com:443': self.sink.getsockname()[1]},
                  'policy': {'reserves': {'five_hour': 30, 'weekly': None}, 'max_age_s': 30}}
        self.emit = Collect()
        self.gate = GATE.Gate(config, self.emit)
        self.gate.notify = lambda op, **kw: None
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen(8)
        self.server = GATE.Server(listener, self.gate)
        threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.port = listener.getsockname()[1]
        headers = [('Authorization', 'Bearer ' + self.access), ('ChatGPT-Account-ID', 'acct-a')]
        self.assertTrue(self.gate.adapter.admit(headers, b'{"model": "m"}', lambda: None)[0])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.sink.close()
        self.up.close()

    def _sink(self):
        try:
            conn, _ = self.sink.accept()
        except OSError:
            return
        with conn:
            while True:
                try:
                    data = conn.recv(65536)
                except OSError:
                    data = b''
                if not data:
                    self.sink_eof.set()
                    return
                self.received.append(data)

    def tunnel(self):
        client = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        client.sendall(b'CONNECT auth.openai.com:443 HTTP/1.1\r\nHost: auth.openai.com:443\r\n\r\n')
        head = b''
        while b'\r\n\r\n' not in head:
            head += client.recv(1)
        self.assertIn(b' 200 ', head.split(b'\r\n')[0] + b' ')
        client.sendall(b'hello')
        FX.wait_for(lambda: sum(map(len, self.received)) == 5, 5)
        return client

    def cut(self, trigger):
        client = self.tunnel()
        trigger()
        self.assertTrue(self.sink_eof.wait(5), 'upstream side of the tunnel stayed open')
        try:
            client.sendall(b'after the latch')
        except OSError:
            pass
        time.sleep(0.3)
        self.assertEqual(b''.join(self.received), b'hello')
        client.settimeout(5)
        try:
            self.assertEqual(client.recv(10), b'')
        except OSError:
            pass  # reset is also a closed tunnel
        client.close()
        FX.wait_for(lambda: any(r['event'] == 'tunnel_closed' for r in self.emit.rows), 5)
        late = socket.create_connection(('127.0.0.1', self.port), timeout=5)
        late.sendall(b'CONNECT auth.openai.com:443 HTTP/1.1\r\nHost: auth.openai.com:443\r\n\r\n')
        self.assertTrue(late.recv(64).startswith(b'HTTP/1.1 403'))
        late.close()

    def test_latch_cuts_open_tunnel(self):
        self.cut(lambda: self.gate.adapter.latch('threshold:five_hour'))

    def test_monitor_loss_cuts_open_tunnel(self):
        self.cut(lambda: self.gate.lose('monitor_eof'))


class ClaudeAdapter(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='km-test-'))
        self.cfg = self.root / 'accounts' / 'a' / 'cfg'
        self.cfg.mkdir(parents=True, mode=0o700)
        for d in (self.root / 'accounts', self.root / 'accounts' / 'a'):
            os.chmod(d, 0o700)
        self.usage = FX.ClaudeUsage()
        self.token = FX.claude_token('A')
        self.usage.add('A', self.token, ('acct-a', 'org-a'))
        FX.claude_store(self.cfg, self.token, FX.claude_token('AR'), int(time.time() * 1000) + 86400000)
        FX.claude_metadata(self.cfg, ('acct-a', 'org-a'), [])
        self.emit = Collect()
        self.notes = []
        config = {'provider': 'claude', 'upstream_port': 9, 'usage_port': self.usage.port,
                  'config_dir': str(self.cfg), 'registered': ['acct-a', 'org-a'], 'labels': {'A': self.token},
                  'policy': {'reserves': {'five_hour': 30, 'weekly': None}, 'max_age_s': 30,
                             'allowed_models': ['claude-sonnet-4-6']}}
        self.gate = GATE.Gate(config, self.emit)
        self.gate.notify = lambda op, **kw: self.notes.append(op)

    def tearDown(self):
        self.usage.close()
        shutil.rmtree(self.root)

    def test_startup_threshold_and_continuation(self):
        adapter = self.gate.adapter
        self.assertEqual(adapter.startup(), (True, 'startup_ok'))
        body = b'{"model": "claude-sonnet-4-6"}'
        headers = [('Authorization', 'Bearer ' + self.token)]
        closed = []
        ok, reason, stream = adapter.admit(headers, body, lambda: closed.append(1))
        self.assertTrue(ok, reason)
        self.usage.set(('acct-a', 'org-a'), five=80)
        self.assertEqual(adapter.poll(), 'threshold_five_hour')
        self.assertEqual(closed, [1])
        self.assertFalse(adapter.admit(headers, body, lambda: None)[0])
        self.assertEqual(adapter.continue_check()['ok'], False)
        self.usage.set(('acct-a', 'org-a'), five=10)
        adapter.poll()
        self.assertFalse(adapter.admit(headers, body, lambda: None)[0], 'passive recovery reopened')
        self.assertEqual(adapter.continue_check(), {'ok': True, 'reason': 'resumed'})
        self.assertTrue(adapter.admit(headers, body, lambda: None)[0])

    def test_unknown_bearer_holds_then_latches(self):
        adapter = self.gate.adapter
        adapter.startup()
        other = FX.claude_token('B')
        self.usage.add('B', other, ('acct-b', 'org-b'))
        ok, reason, _ = adapter.admit([('Authorization', 'Bearer ' + other)], b'{"model": "claude-sonnet-4-6"}',
                                      lambda: None)
        self.assertFalse(ok)
        self.assertIn(reason, ('bearer_identity_mismatch', 'identity_mismatch'))

    def test_startup_refusals(self):
        self.usage.revoke(self.token)
        ok, reason = self.gate.adapter.startup()
        self.assertFalse(ok)


class Fixtures(unittest.TestCase):
    def test_latest_turn(self):
        self.assertEqual(FX.latest_turn('claude', {'messages': [{'role': 'user', 'content': 'hi KM-a'}]}),
                         ('turn', 'hi KM-a'))
        self.assertEqual(FX.latest_turn('claude', {'messages': [{'role': 'user', 'content': [
            {'type': 'tool_result', 'content': 'x'}]}]})[0], 'tool_result')
        body = {'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'run KM-b now'}]}]}
        self.assertEqual(FX.marker_of(FX.latest_turn('codex', body)[1]), 'KM-b')
        self.assertEqual(FX.latest_turn('codex', {'input': [{'type': 'function_call_output'}]})[0], 'tool_result')


class Driver(unittest.TestCase):
    def test_replacements(self):
        mapping = RUN.replacements('/tmp/quota-km-AbC123', '/Users/someone', 'someone', '501')
        self.assertEqual(mapping['/private/tmp/quota-km-AbC123'], '<root>')
        self.assertEqual(mapping['/claude-501/'], '/claude-<uid>/')
        self.assertEqual(mapping['quota-km-AbC123'], '<root-name>')
        for bad in (('/tmp/quota-nm-AbC123', '/h', 'u'), ('/tmp/quota-km-AbC123', 'rel', 'u'),
                    ('/tmp/quota-km-AbC123', '/h', '')):
            with self.assertRaises(ValueError):
                RUN.replacements(*bad)
        with self.assertRaises(SystemExit):
            RUN.parse_discovery('claude=/x\nhome=/h\n')

    def test_claude_tmp_entries(self):
        root = '/tmp/quota-km-AbC123'
        self.assertEqual(RUN.claude_tmp_slug(root), '-private-tmp-quota-km-AbC123')
        listing = '\n'.join(['/private/tmp/claude-501/-private-tmp-quota-km-AbC123-cases-x-workspace',
                              '/private/tmp/claude-501/-private-tmp-quota-km-AbC123',
                              '/private/tmp/claude-501/-private-tmp-quota-km-AbC1234-x',
                              '/private/tmp/claude-501/-private-tmp-quota-km-Other1-x',
                              '/private/tmp/claude-501/-Users-someone-project',
                              '/private/tmp/claude-501/-private-tmp-quota-km-AbC123-x/../../etc',
                              '/private/tmp/claude-501'])
        self.assertEqual(RUN.claude_tmp_entries(root, listing),
                         ['/private/tmp/claude-501/-private-tmp-quota-km-AbC123-cases-x-workspace',
                          '/private/tmp/claude-501/-private-tmp-quota-km-AbC123'])
        with self.assertRaises(ValueError):
            RUN.claude_tmp_slug('/tmp/quota-nm-AbC123')


class Evidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = {name: json.loads((HERE / 'evidence' / (name + '.json')).read_text()) for name in CHECK.STAGES}

    def test_committed_evidence_passes(self):
        for name, data in self.data.items():
            CHECK.check_stage(name, data)

    def test_no_host_strings(self):
        text = json.dumps(self.data)
        for needle in ('/Users/', os.path.expanduser('~'), '/private/tmp/quota-km-', 'quota-km-'):
            self.assertNotIn(needle, text)

    def mutate(self, stage, edit):
        data = copy.deepcopy(self.data[stage])
        edit(data['cases'])
        with self.assertRaises(CHECK.Fail):
            CHECK.check_stage(stage, data)

    def test_mutations_fail(self):
        def late_post(cases):
            c = cases['claude-threshold']
            c['upstream'].append({'t': c['latch_t'] + 0.5, 'method': 'POST', 'path': '/v1/messages'})

        def late_admit(cases):
            c = cases['codex-loss-eof']
            c['summary']['gate_rows'].append({'event': 'decision', 'ok': True, 't': c['latch_t'] + 1})

        def child_admitted(cases):
            c = cases['codex-threshold']
            c['sdk_rows'].append({'action': 'sdk', 'role': 'child', 'status': 200, 't': c['latch_t'] + 2})

        def slow_stall(cases):
            cases['claude-loss-stall']['trigger_t'] -= 10

        self.mutate('claude-loss', late_post)
        self.mutate('codex-loss', late_admit)
        self.mutate('codex-loss', child_admitted)
        self.mutate('claude-loss', slow_stall)
        self.mutate('claude-resume', lambda c: c['claude-continuation']['steps']['paste'].update(granted=True))
        self.mutate('codex-resume', lambda c: c['codex-gate-death']['squat_during_death'].update(host4_reuseaddr='bound'))
        self.mutate('codex-resume', lambda c: c['codex-independent']['second_turn']['decisions'].__setitem__(
            0, [False, 'paused', 'A']))
        self.mutate('claude-refresh', lambda c: c['claude-rotation']['connect_rows'][1].update(status=200))
        self.mutate('codex-refresh', lambda c: c['codex-norefresh']['summary']['gate_rows'].append(
            {'event': 'connect', 'ok': True, 'target': 'auth.openai.com:443', 't': 1}))
        self.mutate('claude-tools', lambda c: c['claude-tools']['bg_file'].update(after=0))
        self.mutate('codex-tools', lambda c: c['codex-sandbox'].update(tool_effect_file=True))
        self.mutate('refusals', lambda c: c['codex-refusals']['variants']['stale_quota'].update(native_started=True))
        self.mutate('refusals', lambda c: c['claude-refusals']['variants']['old_version'].update(model_posts=1))
        self.mutate('portmatrix', lambda c: c['none-portmatrix']['all']['squats'].update(any4_reuseaddr='bound'))
        self.mutate('portmatrix', lambda c: c['none-portmatrix']['sbpl_forms']['tcp4_localhost'].update(
            lo6='connected'))
        self.mutate('proxy', lambda c: c['codex-proxy'].update(native_proxy_unused=False))
        self.mutate('proxy', lambda c: c['claude-proxy'].update(launcher_proxy=None))
        self.mutate('proxy', lambda c: c['codex-proxy']['proxy_rows'].append(
            {'ok': True, 'target': 'api.anthropic.com:443', 't': 1}))
        self.mutate('claude-resume', lambda c: c['claude-continuation']['steps']['background_wakeup']['status'].update(
            resumed=1))
        self.mutate('codex-resume', lambda c: [r.update(result='connected') for r in c['codex-gate-death']['direct_rows']
                                               if r['port_role'] == 'gate_udp'])
        self.mutate('codex-resume', lambda c: [r.update(port=r['port'] + 1) for r in c['codex-gate-death']['summary'][
            'gate_rows'] if r['event'] == 'gate_ready' and r.get('mode') == 'restart'])
        self.mutate('refusals', lambda c: c['codex-refusals']['variants']['self_test_fails'].update(
            upstream_gets=['/backend-api/codex/models']))
        self.mutate('refusals', lambda c: c['codex-refusals']['variants']['self_test_fails'].update(reader=[True]))
        self.mutate('refusals', lambda c: c['codex-refusals']['variants']['stale_quota'].update(self_test=[]))
        self.mutate('claude-loss', lambda c: c['claude-threshold']['summary'].update(
            gate_rows_total=c['claude-threshold']['summary']['gate_rows_total'] + 1))

    def test_codex_rotation_order_mutations(self):
        def first_a2(case):
            return min(r['t'] for r in case['summary']['gate_rows']
                       if r['event'] == 'decision' and r.get('ok') and r.get('label') == 'A2')

        def late_upstream_read(cases):
            c = cases['codex-rotation']
            first = first_a2(c)
            for r in c['upstream']:
                if r.get('method') == 'GET' and r.get('label') == 'A2' and r['t'] < first:
                    r['t'] = first + 0.01

        def late_gate_bind(cases):
            c = cases['codex-rotation']
            first = first_a2(c)
            for r in c['summary']['gate_rows']:
                if r['event'] in ('read', 'fetch') and r.get('label') == 'A2' and r['t'] < first:
                    r['t'] = first + 0.01

        def old_epoch(cases):
            c = cases['codex-rotation']
            for r in c['summary']['gate_rows']:
                if r['event'] == 'controller' and r.get('kind') == 'admitted':
                    r['epoch'] = 1

        self.mutate('codex-refresh', late_upstream_read)
        self.mutate('codex-refresh', late_gate_bind)
        self.mutate('codex-refresh', old_epoch)

    def test_stage_level_failures(self):
        data = copy.deepcopy(self.data['codex-loss'])
        data['binaries']['claude']['version'] = '2.1.300 (Claude Code)'
        with self.assertRaises(CHECK.Fail):
            CHECK.check_stage('codex-loss', data)
        data = copy.deepcopy(self.data['claude-resume'])
        data['clock']['wall_end'] += 30  # a sleep stops the monotonic clock, not the wall clock
        with self.assertRaises(CHECK.Fail):
            CHECK.check_stage('claude-resume', data)
        data = copy.deepcopy(self.data['codex-tools'])
        data['leftover_processes_after'] = [{'pid': 1, 'command': 'x'}]
        with self.assertRaises(CHECK.Fail):
            CHECK.check_stage('codex-tools', data)
        data = copy.deepcopy(self.data['refusals'])
        data['completed'] = False
        with self.assertRaises(CHECK.Fail):
            CHECK.check_stage('refusals', data)

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in CHECK.STAGES[1:]:
                shutil.copy(HERE / 'evidence' / (name + '.json'), Path(tmp) / (name + '.json'))
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(CHECK.main(['--dir', tmp]), 1)
            self.assertIn('claude-loss: FAIL missing evidence file', out.getvalue())


if __name__ == '__main__':
    unittest.main()
