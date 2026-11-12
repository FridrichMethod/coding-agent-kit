"""Offline tests for the NM probe logic; run on Linux, start no native code.

python3 -I -B -m unittest discover -s docs/superpowers/plans/quota-reserve-probes/nm -p 'test_nm.py'
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location('nm_test_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SBPL = load('sbpl')
RUN = load('run')
CHECK = load('check_evidence')


class Render(unittest.TestCase):
    def base(self, **extra):
        policy = {'tag': 'qnm-abc123', 'gate_ports': [4242], 'private_socket_dirs': ['/private/tmp/r/private'],
                  'mach_allow': ['com.apple.system.opendirectoryd.libinfo'],
                  'deny_write': ['/private/var/at']}
        policy.update(extra)
        return policy

    def test_enforced_profile_shape(self):
        text = SBPL.render(self.base())
        lines = text.splitlines()
        self.assertEqual(lines[:3], ['(version 1)', '(allow default)', '(deny network* (with message "qnm-abc123"))'])
        self.assertIn('(allow network-outbound (remote ip "localhost:4242"))', lines)
        for op in SBPL.LAUNCH_OPERATIONS:
            self.assertIn('(deny %s (with message "qnm-abc123"))' % op, lines)
        deny = lines.index('(deny mach-lookup (with message "qnm-abc123"))')
        allow = lines.index('(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))')
        self.assertLess(deny, allow, 'the allowlist must follow the default mach deny')
        self.assertTrue(text.endswith('(deny file-write* (subpath "/private/var/at") (with message "qnm-abc123"))\n'))

    def test_every_deny_carries_the_tag(self):
        for line in SBPL.render(self.base()).splitlines():
            if line.startswith('(deny'):
                self.assertIn('(with message "qnm-abc123")', line)

    def test_census_reports_and_still_denies_escapes(self):
        text = SBPL.render(self.base(deny_mach=False, mach_allow=[], report_mach=True,
                                     mach_deny=list(SBPL.ESCAPE_SERVICES)))
        lines = text.splitlines()
        report = lines.index('(allow mach-lookup (with report) (with message "qnm-abc123"))')
        explicit = [i for i, line in enumerate(lines) if line.startswith('(deny mach-lookup (global-name')]
        self.assertEqual(len(explicit), 1)
        self.assertGreater(explicit[0], report, 'explicit denies must override the report allow')
        for name in SBPL.ESCAPE_SERVICES:
            self.assertIn('"%s"' % name, lines[explicit[0]])

    def test_signal_hardening_rule(self):
        lines = SBPL.render(self.base(deny_signal_others=True)).splitlines()
        deny = lines.index('(deny signal (with message "qnm-abc123"))')
        self.assertEqual(lines[deny + 1], '(allow signal (target same-sandbox))')
        self.assertNotIn('(deny signal (with message "qnm-abc123"))', SBPL.render(self.base()))

    def test_refusals(self):
        bad = [
            {'tag': 'x'},
            {'tag': 'qnm-ok', 'gate_ports': [0]},
            {'tag': 'qnm-ok', 'gate_ports': [70000]},
            {'tag': 'qnm-ok', 'gate_ports': [True]},
            {'tag': 'qnm-ok', 'gate_sockets': ['relative.sock']},
            {'tag': 'qnm-ok', 'gate_sockets': ['/tmp/x.sock']},
            {'tag': 'qnm-ok', 'deny_write': ['/var/at']},
            {'tag': 'qnm-ok', 'deny_write': ['/private/a/../b']},
            {'tag': 'qnm-ok', 'deny_write': ['/private/a/']},
            {'tag': 'qnm-ok', 'deny_write': ['/private/a"b']},
            {'tag': 'qnm-ok', 'deny_write': ['/private/a\\b']},
            {'tag': 'qnm-ok', 'deny_write': ['/private/café']},
            {'tag': 'qnm-ok', 'mach_allow': ['com.apple.coreservices.launchservicesd']},
            {'tag': 'qnm-ok', 'mach_allow': ['bad name']},
            {'tag': 'qnm-ok', 'mach_allow': ['a.b'], 'mach_deny': ['a.b']},
            {'tag': 'qnm-ok', 'deny_mach': False, 'mach_allow': ['a.b']},
            {'tag': 'qnm-ok', 'deny_network': False, 'gate_ports': [1]},
            {'tag': 'qnm-ok', 'unknown_key': 1},
            {'tag': 'bad"tag'},
        ]
        for policy in bad:
            with self.subTest(policy=policy), self.assertRaises(SBPL.ProfileError):
                SBPL.render(policy)


class KeychainNames(unittest.TestCase):
    def test_default_and_custom_dirs(self):
        self.assertEqual(SBPL.claude_keychain_service(), 'Claude Code-credentials')
        digest = hashlib.sha256('/private/tmp/r/cfg'.encode()).hexdigest()[:8]
        self.assertEqual(SBPL.claude_keychain_service('/private/tmp/r/cfg'), 'Claude Code-credentials-' + digest)
        self.assertEqual(SBPL.claude_keychain_service('/private/tmp/r/cfg', kind=''), 'Claude Code-' + digest)
        self.assertEqual(SBPL.claude_keychain_service(secure_dir=''), 'Claude Code-credentials')

    def test_nfc_normalization(self):
        decomposed, composed = '/x/café', '/x/café'
        self.assertEqual(SBPL.claude_keychain_service(decomposed), SBPL.claude_keychain_service(composed))


class Reports(unittest.TestCase):
    def test_parse_and_summarize(self):
        tag = 'qnm-abc123'
        lines = [json.dumps({'eventMessage': 'Sandbox: nc(77596) deny(1) network-outbound remote:*:9\n' + tag}),
                 json.dumps({'eventMessage': '2 duplicate reports for Sandbox: nc(77596) allow mach-lookup '
                                             'com.apple.logd\n' + tag}),
                 json.dumps({'eventMessage': 'Sandbox: other(1) deny(1) mach-lookup x\nqnm-other'}),
                 json.dumps({'eventMessage': 'Sandbox: untagged(1) deny(1) mach-lookup x'}),
                 'not json', '']
        rows = SBPL.parse_log_ndjson('\n'.join(lines), tag)
        self.assertEqual([r['process'] for r in rows], ['nc', 'nc'])
        self.assertEqual(SBPL.summarize_reports(rows), [
            {'action': 'allow', 'operation': 'mach-lookup', 'target': 'com.apple.logd', 'process': 'nc', 'count': 2},
            {'action': 'deny', 'operation': 'network-outbound', 'target': 'remote:*:9', 'process': 'nc', 'count': 1}])

    def test_process_names_with_spaces(self):
        row = SBPL.parse_report('Sandbox: Google Chrome He(12) deny(1) file-read-data /x y\nqnm-t1t1', 'qnm-t1t1')
        self.assertEqual((row['process'], row['target']), ('Google Chrome He', '/x y'))


class Terminal(unittest.TestCase):
    def test_strip_ansi(self):
        raw = '\x1b[2mNM\x1b[0m fixture\x1b[1C reply\x1b]0;title\x07 x \x1b(B\x1b7done\x1b]10;?\x1b\\ end'
        self.assertEqual(SBPL.strip_ansi(raw), ' NM fixture reply x done end')
        self.assertIn('NM fixture reply', SBPL.strip_ansi('NM\x1b[38;5;2m fixture\r\n reply'))


class Sanitize(unittest.TestCase):
    def test_longest_first_and_leak_check(self):
        mapping = RUN.replacements('/tmp/quota-nm-AbC123', '/Users/someone', 'someone')
        data = {'a': '/private/tmp/quota-nm-AbC123/x and /tmp/quota-nm-AbC123/y',
                'b': ['/Users/someone/Library', 'user someone'], '/Users/someone': 1}
        clean = SBPL.sanitize(data, mapping)
        self.assertEqual(clean['a'], '<root>/x and <root>/y')
        self.assertEqual(clean['b'], ['<home>/Library', 'user <user>'])
        self.assertIn('<home>', clean)
        self.assertEqual(SBPL.leaks(clean, list(mapping)), [])
        self.assertEqual(SBPL.leaks(data, ['someone']), ['someone'])

    def test_root_pattern_and_refusals(self):
        self.assertTrue(RUN.root_ok('/tmp/quota-nm-AbC123'))
        for bad in ('/tmp/quota-nm-', '/tmp/quota-nm-AbC123/x', '/tmp/other-AbC123', '', None,
                    '/tmp/quota-nm-AbC12;rm'):
            self.assertFalse(RUN.root_ok(bad), bad)
        with self.assertRaises(ValueError):
            RUN.replacements('/tmp/x', '/Users/a', 'a')
        with self.assertRaises(ValueError):
            RUN.replacements('/tmp/quota-nm-AbC123', 'relative', 'a')


class Evidence(unittest.TestCase):
    """The committed evidence passes; targeted mutations are rejected."""

    def load(self, stage):
        path = HERE / 'evidence' / (stage + '.json')
        if not path.exists():
            self.skipTest('evidence %s not captured' % stage)
        return json.loads(path.read_text())

    def reject(self, stage, mutate):
        data = copy.deepcopy(self.load(stage))
        mutate(data)
        with self.assertRaises((AssertionError, KeyError, TypeError)):
            CHECK.check_stage(stage, data)

    def test_committed_evidence(self):
        for stage in CHECK.CHECKS:
            with self.subTest(stage=stage):
                CHECK.check_stage(stage, self.load(stage))

    def test_observed_keychain_name_matches_source_rule(self):
        names = self.load('keychain')['claude_names_for_probe_config']
        argv = self.load('natives')['cases']['claude-enforced-oauth-file']['security_shim_argv']
        services = {a[-1] for a in argv}
        self.assertIn(names['credentials'], services)
        self.assertIn(names['legacy_api_key'], services)
        self.assertNotIn(names['device_keys_fixed'], services)

    def test_boundary_mutations(self):
        def escape(d):
            d['cases']['enforced']['sandboxed']['env_i_absolute_python']['parsed']['results']['alt_tcp6'] = 'connected'
        def hit(d):
            d['cases']['census']['listener_hits_from_sandboxed']['udp'] = 1
        def detached(d):
            d['cases']['enforced']['sandboxed']['detached_setsid_python']['detached_result']['results'][
                'outside_unix'] = 'connected'
        def control(d):
            d['cases']['enforced']['listener_hits_from_control']['alt'] = 0
        def incomplete(d):
            d['completed'] = False
        def leftover(d):
            d['leftover_processes_after'] = [{'pid': 1}]
        def leak(d):
            d['profiles']['x'] = '/Users/someone'
        for mutate in (escape, hit, detached, control, incomplete, leftover, leak):
            with self.subTest(mutate=mutate.__name__):
                self.reject('boundary', mutate)

    def test_ipc_mutations(self):
        def lookup(d):
            d['cases']['mach_lookup_sandboxed']['parsed']['results']['com.apple.nsurlsessiond'] = 'found'
        def launchd(d):
            d['cases']['launchctl_cleanup']['marker_written'] = True
        def nested(d):
            d['cases']['nested_sandbox_exec']['code'] = 0
        def anchor(d):
            d['cases']['anchor_write_sandboxed']['file_exists'] = True
        for mutate in (lookup, launchd, nested, anchor):
            with self.subTest(mutate=mutate.__name__):
                self.reject('ipc', mutate)

    def test_native_mutations(self):
        def egress(d):
            d['cases']['codex-enforced-plain']['tool_outputs']['detached.json']['results']['alt_tcp4'] = 'connected'
        def bearer(d):
            for e in d['cases']['claude-enforced-oauth-file']['gate_events']:
                e['bearer_matches'] = False
        def tty(d):
            d['cases']['claude-census-oauth-file']['termios_restored'] = False
        def turn(d):
            d['cases']['codex-census-plain']['second_turn_reached_gate'] = False
        def daemon(d):
            d['cases']['codex-enforced-daemon']['gate_events'].append({'event': 'codex_model'})
        def store(d):
            d['cases']['claude-enforced-oauth-file']['security_shim_argv'] = [['find-generic-password', '-s', 'x']]
        for mutate in (egress, bearer, tty, turn, daemon, store):
            with self.subTest(mutate=mutate.__name__):
                self.reject('natives', mutate)

    def test_compat_and_extra_mutations(self):
        def git(d):
            d['cases']['git_clone_http_fixture']['code'] = 0
        def hits(d):
            d['cases']['alt_hits_from_sandboxed'] = 2
        def control(d):
            d['cases']['alt_hits_total'] = 0
        for mutate in (git, hits, control):
            with self.subTest(mutate=mutate.__name__):
                self.reject('compat', mutate)

        def store(d):
            d['cases']['claude-enforced-real-security']['log']['summary'] = []
        self.reject('natives-extra', store)

        def signal(d):
            d['cases']['signal0_outside_hardened']['code'] = 0
        self.reject('ipc', signal)

    def test_fds_and_keychain_mutations(self):
        def closed(d):
            d['cases']['closed_before_confinement']['parsed']['results']['inherited_tcp'] = 'written'
        self.reject('fds', closed)

        def reachable(d):
            d['cases']['store_denied']['stderr'] = 'security: could not be found'
        self.reject('keychain', reachable)


if __name__ == '__main__':
    unittest.main()
