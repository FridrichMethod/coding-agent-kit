"""Offline tests for the KM probes (research only; Linux or macOS, loopback fixtures only).

python3 -I -B -m unittest discover -s docs/superpowers/plans/quota-reserve-probes/km -p test_km.py

They cover the launcher's pure refusals and profile shape, the gate route table and both
composed adapters against loopback fixtures (no native, no Seatbelt), the driver's sanitizing,
and the evidence checker: every committed stage passes, and each targeted mutation of a
verdict-bearing row makes it fail. No native code is started and nothing leaves 127.0.0.1.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
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
        self.assertEqual(LAUNCHER.check_args('codex', ['-m', 'gpt', 'resume']), ['--no-daemon', '-m', 'gpt', 'resume'])
        for argv in (['resume'], ['--profile', 'p'], ['-c', 'chatgpt_base_url="x"'], ['--config=model_providers.a=1'],
                     ['app-server']):
            with self.assertRaises(LAUNCHER.Refusal):
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
        for bad in (('/tmp/quota-nm-AbC123', '/h', 'u'), ('/tmp/quota-km-AbC123', 'rel', 'u'),
                    ('/tmp/quota-km-AbC123', '/h', '')):
            with self.assertRaises(ValueError):
                RUN.replacements(*bad)
        with self.assertRaises(SystemExit):
            RUN.parse_discovery('claude=/x\nhome=/h\n')


class Evidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = {name: json.loads((HERE / 'evidence' / (name + '.json')).read_text()) for name in CHECK.STAGES}

    def test_committed_evidence_passes(self):
        for name, data in self.data.items():
            CHECK.check_stage(name, data)

    def test_no_host_strings(self):
        text = json.dumps(self.data)
        for needle in ('/Users/', os.path.expanduser('~'), '/private/tmp/quota-km-'):
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

    def test_stage_level_failures(self):
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
