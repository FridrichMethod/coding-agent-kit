"""Offline tests of the KLC launcher refusal checks (pure functions, no processes)."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('klc_compose', HERE / 'compose.py')
compose = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compose)

GOOD = {'platform': 'linux', 'arch': 'x86_64', 'landlock_abi': 8, 'stdio_sockets': [], 'legacy_tiocsti': 0}
NOW = 1_800_000_000_000
CONFIG = '/private/accounts/alpha/config'


class HostTests(unittest.TestCase):
    def test_good_host(self):
        self.assertIsNone(compose.host_refusal(GOOD))

    def test_each_precondition_refuses(self):
        cases = {'not_linux': {'platform': 'darwin'}, 'landlock_unavailable': {'landlock_abi': None},
                 'landlock_abi_below_4': {'landlock_abi': 3}, 'seccomp_arch_unvalidated': {'arch': 'aarch64'},
                 'stdio_socket': {'stdio_sockets': [1]}, 'tiocsti_enabled': {'legacy_tiocsti': 1},
                 'tiocsti_unknown': {'legacy_tiocsti': None}}
        for reason, change in cases.items():
            self.assertEqual(compose.host_refusal(dict(GOOD, **change)), reason)

    def test_this_host_facts_are_readable(self):
        facts = compose.host_facts()
        self.assertIn('landlock_abi', facts)
        self.assertIn('legacy_tiocsti', facts)


class NativeTests(unittest.TestCase):
    def test_version(self):
        self.assertIsNone(compose.version_refusal('2.1.322 (Claude Code)\n'))
        self.assertEqual(compose.version_refusal('2.1.323 (Claude Code)'), 'native_version_unsupported')
        self.assertEqual(compose.version_refusal('Claude Code'), 'native_version_unreadable')
        self.assertEqual(compose.version_refusal(''), 'native_version_unreadable')

    def test_mode(self):
        self.assertIsNone(compose.mode_refusal(['--model', 'claude-sonnet-4-6', 'hello']))
        for argv in (['-p', 'x'], ['--print'], ['--bare'], ['--bg'], ['--remote-control'], ['--settings={}'],
                     ['--mcp-config', 'x.json'], ['-pc']):
            self.assertEqual(compose.mode_refusal(argv), 'native_mode_unsupported', argv)

    def test_env(self):
        self.assertIsNone(compose.env_refusal({'PATH': '/usr/bin'}, CONFIG))
        self.assertIsNone(compose.env_refusal({'CLAUDE_CONFIG_DIR': CONFIG}, CONFIG))
        self.assertEqual(compose.env_refusal({'CLAUDE_CONFIG_DIR': CONFIG + '/'}, CONFIG), 'config_dir_mismatch')
        for name in ('ANTHROPIC_BASE_URL', 'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'CLAUDE_CODE_USE_BEDROCK'):
            self.assertEqual(compose.env_refusal({name: ''}, CONFIG), 'caller_env_conflict', name)


class TokenWindowTests(unittest.TestCase):
    def test_window(self):
        refuse = compose.token_window_refusal
        self.assertIsNone(refuse(NOW + 3600_000, NOW, False))
        self.assertEqual(refuse(NOW + 120_000, NOW, False), 'native_refresh_window')
        self.assertIsNone(refuse(NOW + 120_000, NOW, True))
        self.assertEqual(refuse(NOW + 20_000, NOW, True), 'expired_native_refresh_required')
        self.assertEqual(refuse(NOW + 300_000, NOW, False), 'native_refresh_window')
        self.assertIsNone(refuse(NOW + 300_001, NOW, False))

    def test_first_refusal_order(self):
        args = (GOOD, '2.1.322 (Claude Code)', [], {}, CONFIG, NOW + 3600_000, NOW, False)
        self.assertIsNone(compose.first_refusal(*args))
        self.assertEqual(compose.first_refusal(dict(GOOD, legacy_tiocsti=1), *args[1:]), 'tiocsti_enabled')
        self.assertEqual(compose.first_refusal(GOOD, '9.9.9 (Claude Code)', ['-p'], *args[3:]),
                         'native_version_unsupported')


if __name__ == '__main__':
    unittest.main()
