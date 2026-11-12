"""Offline tests for the KLX launcher policy: startup refusals, preflight verdicts, config and env.

python3 -I -B test_launcher.py
"""
import importlib.util
from pathlib import Path
import socket
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('klx_test_launcher', HERE / 'launcher.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)
HOST = {'system': 'Linux', 'machine': 'x86_64', 'landlock_abi': 8, 'legacy_tiocsti': 0, 'socket_stdio': []}


class Launcher(unittest.TestCase):
    def test_version_minimum_not_pin(self):
        self.assertEqual(launcher.check_version('codex-cli 0.167.2'), (0, 167, 2))
        self.assertEqual(launcher.check_version('codex-cli 0.170.0'), (0, 170, 0))
        for text in ('codex-cli 0.166.9', 'something else', ''):
            with self.assertRaises(launcher.Refusal):
                launcher.check_version(text)

    def test_arguments(self):
        self.assertEqual(launcher.check_args(['hello']), ['--no-daemon', 'hello'])
        self.assertEqual(launcher.check_args(['--no-daemon', 'hello']), ['--no-daemon', 'hello'])
        self.assertEqual(launcher.check_args(['-m', 'gpt-5.4', 'fix the resume parser']),
                         ['--no-daemon', '-m', 'gpt-5.4', 'fix the resume parser'])
        for argv in (['--remote', 'ws://x'], ['--remote=ws://x'], ['resume'], ['fork', 'x'], ['remote-control'],
                     ['app-server'], ['-p', 'x'], ['--profile=x'], ['-c', 'chatgpt_base_url="x"'],
                     ['--config', 'model_providers.y.base_url="x"'], ['-c', 'model_provider="other"'],
                     ['-c', 'experimental_realtime_ws_base_url="x"'], ['--oss']):
            with self.assertRaises(launcher.Refusal, msg=argv):
                launcher.check_args(argv)
        self.assertEqual(launcher.check_args(['-c', 'model_reasoning_effort="high"', 'x'])[0], '--no-daemon')

    def test_clap_spellings_that_reach_a_subcommand_or_a_routing_key(self):
        # Each of these reaches a subcommand or a protected key in clap 0.167.2 (offline --help), or is
        # a stricter fail-closed refusal (subcommand name as an option value, combined short flags).
        for argv in (['-m', 'gpt-5.4', 'resume', '--last'], ['-mgpt-5.4', 'resume'], ['--model=x', 'fork'],
                     ['-C', '/work', 'fork'], ['--cd=/work', 'fork'], ['-i', 'a.png', 'remote-control'],
                     ['--image=a.png', 'fork'], ['-i=a.png', 'fork'], ['hello', 'resume'], ['e'], ['a'],
                     ['help'], ['exec', 'x'], ['cloud-tasks'], ['-m', 'resume'],
                     ['-cchatgpt_base_url="https://x"'], ['-cmodel_providers.klxgate.base_url="http://x"'],
                     ['-c=chatgpt_base_url=x'], ['--config=model_provider=x'], ['-c', ' chatgpt_base_url =x'],
                     ['-c', 'use_legacy_landlock=true'], ['-c', 'features.apps=true'], ['-c', 'sandbox_mode=x'],
                     ['-c', '"chatgpt_base_url"=x'], ['-c', 'model'], ['-c'], ['-m', '-x'], ['--enable', 'apps'],
                     ['--enable=apps'], ['--disable', 'x'], ['-s', 'workspace-write'], ['-sread-only'],
                     ['--search'], ['--add-dir', '/x'], ['--worktree'], ['--unknown-flag'], ['-z'], ['-hV'],
                     ['--no-alt-screen=1'], ['--approve-for-me']):
            with self.assertRaises(launcher.Refusal, msg=argv):
                launcher.check_args(argv)
        for argv, expected in ((['--', 'resume'], ['--no-daemon', '--', 'resume']),
                               (['-c', 'model="o3"', '--', 'fork'], ['--no-daemon', '-c', 'model="o3"', '--', 'fork']),
                               (['-mgpt-5.4', 'x'], ['--no-daemon', '-mgpt-5.4', 'x']),
                               (['--model=gpt-5.4', 'x'], ['--no-daemon', '--model=gpt-5.4', 'x']),
                               (['-C', '/w', '--no-alt-screen', 'x'], ['--no-daemon', '-C', '/w', '--no-alt-screen', 'x']),
                               (['-i', 'a.png', 'describe it'], ['--no-daemon', '-i', 'a.png', 'describe it']),
                               (['-cmodel=x', '--no-daemon', 'y'], ['--no-daemon', '-cmodel=x', 'y']),
                               (['--', '--no-daemon'], ['--no-daemon', '--', '--no-daemon'])):
            self.assertEqual(launcher.check_args(argv), expected, argv)

    def test_prelaunch_refresh_verdict(self):
        self.assertIsNone(launcher.prelaunch_refresh_verdict('2026-11-12T00:00:00Z', '2026-11-12T00:01:00Z'))
        self.assertEqual(launcher.prelaunch_refresh_verdict('t', 't'), 'prelaunch-refresh:failed')
        self.assertEqual(launcher.prelaunch_refresh_verdict('t', None), 'prelaunch-refresh:credential-unreadable')

    def test_environment(self):
        launcher.check_env({'PATH': '/usr/bin'})
        for name in launcher.REFUSED_ENV:
            with self.assertRaises(launcher.Refusal):
                launcher.check_env({name: 'x'})

    def test_host_preconditions(self):
        launcher.check_host(**HOST)
        launcher.check_host(**{**HOST, 'landlock_abi': 4})
        for change in ({'system': 'Darwin'}, {'machine': 'aarch64'}, {'landlock_abi': None}, {'landlock_abi': 3},
                       {'legacy_tiocsti': 1}, {'legacy_tiocsti': None}, {'socket_stdio': [1]}):
            with self.assertRaises(launcher.Refusal, msg=change):
                launcher.check_host(**{**HOST, **change})

    def test_socket_stdio_detection(self):
        left, right = socket.socketpair()
        try:
            self.assertEqual(launcher.stdio_sockets((left.fileno(),)), [left.fileno()])
        finally:
            left.close()
            right.close()

    def test_preflight_verdict(self):
        self.assertIsNone(launcher.preflight_verdict({'ready': True, 'paused': None, 'lost': None, 'heartbeat': True}))
        self.assertEqual(launcher.preflight_verdict({'ready': True, 'paused': None, 'lost': None}), 'monitor-not-started')
        self.assertIsNone(launcher.preflight_verdict({'ready': True, 'paused': None, 'lost': None}, require_monitor=False))
        self.assertEqual(launcher.preflight_verdict(None), 'gate-unavailable')
        self.assertEqual(launcher.preflight_verdict({'ready': False, 'paused': 'stale', 'blocking': 'stale'}),
                         'quota:stale')
        self.assertEqual(launcher.preflight_verdict({'ready': False, 'paused': None, 'last_outcome': 'malformed'}),
                         'quota:malformed')
        self.assertEqual(launcher.preflight_verdict({'ready': False, 'paused': None, 'last_outcome': None}),
                         'quota:no-fresh-passing-observation')
        self.assertEqual(launcher.preflight_verdict({'ready': True, 'lost': 'monitor-eof'}), 'gate-monitor-eof')

    def test_config_routes_everything_to_the_gate(self):
        text = launcher.render_config('http://127.0.0.1:4321', '/work')
        for line in ('chatgpt_base_url = "http://127.0.0.1:4321/backend-api/"',
                     'base_url = "http://127.0.0.1:4321/backend-api/codex"', 'supports_websockets = false',
                     'requires_openai_auth = true', 'experimental_realtime_ws_base_url = "http://127.0.0.1:4321"',
                     'experimental_realtime_webrtc_call_base_url = "http://127.0.0.1:4321"',
                     'cli_auth_credentials_store = "file"', 'request_max_retries = 0', 'web_search = "disabled"'):
            self.assertIn(line, text)

    def test_only_the_tui_gets_proxy_variables(self):
        tui = launcher.native_env(home='/h', codex_home='/c', tmp='/t', gate_url='http://127.0.0.1:1', ca='/ca.pem')
        reader = launcher.native_env(home='/h', codex_home='/c', tmp='/t')
        self.assertEqual(tui['HTTPS_PROXY'], 'http://127.0.0.1:1')
        self.assertEqual(tui['CODEX_CA_CERTIFICATE'], '/ca.pem')
        self.assertNotIn('HTTPS_PROXY', reader)
        self.assertNotIn('CODEX_CA_CERTIFICATE', reader)
        self.assertIn('127.0.0.1', tui['NO_PROXY'])


if __name__ == '__main__':
    unittest.main()
