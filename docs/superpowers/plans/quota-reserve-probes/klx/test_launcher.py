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
        self.assertIsNone(launcher.preflight_verdict({'ready': True, 'paused': None, 'lost': None}))
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
