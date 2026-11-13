"""Offline tests for e0/read_keys.py: temporary HOME, fabricated caches, no native, no network.

Run: python3 -I -B test_read_keys.py
Every fabricated value contains "seed"; no result may contain that word in any case.
"""
from datetime import datetime, timezone
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = Path(os.path.realpath(__file__)).parent
_spec = importlib.util.spec_from_file_location('e0_read_keys_under_test', HERE / 'read_keys.py')
rk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rk)
MODULES = rk.Modules()

OPENED = []
RECORDING = [False]


def _audit(event, args):
    if RECORDING[0] and event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
        OPENED.append(os.fsdecode(args[0]))


sys.addaudithook(_audit)

KIT_VARIABLES = ('CODING_AGENT_KIT_CONFIG', 'CODING_AGENT_KIT_COLOR', 'CODING_AGENT_KIT_USAGE_FILE',
                 'CODING_AGENT_KIT_TOKEN_ID', 'CODING_AGENT_KIT_CLAUDE_PID', 'CODING_AGENT_KIT_REPO_URL',
                 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'NO_COLOR')
SEED_USER = 'user-seed-chatgpt-user-0001'
SEED_ACCOUNT = 'seed-workspace-account-0001'
SEED_SIGNATURE = 'c2VlZC1zaWduYXR1cmUtbm90LXJlYWw='
PINNING = '\n'.join((
    'chatgpt_base_url = "https://seed-pin.example.invalid/backend-api/"',
    'additional_developer_instructions = "Seed developer instructions that stay private"',
    'seed_unknown_requirement = "seed-unknown-requirement-value"',
    '[model_providers.seedprovider]',
    'base_url = "https://seed-provider.example.invalid/v1"',
    '[application.network.domains]',
    '"seed-domain.example.invalid" = "allow"',
    ''))


def stamp(when):
    return datetime.fromtimestamp(when, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f') + '123Z'


def fragment(kind, index, contents):
    return {'id': 'seed-%s-fragment-id-%04d' % (kind, index), 'name': 'Seed %s fragment name %d' % (kind, index),
            'contents': contents}


def cache_document(requirements=(), configs=(), cached_offset=-60.0, ttl=3600.0):
    now = time.time()
    return {'signed_payload': {
        'version': 1, 'cached_at': stamp(now + cached_offset), 'expires_at': stamp(now + cached_offset + ttl),
        'chatgpt_user_id': SEED_USER, 'account_id': SEED_ACCOUNT,
        'bundle': {'config_toml': {'enterprise_managed': [fragment('config', i, c) for i, c in enumerate(configs)]},
                   'requirements_toml': {'enterprise_managed': [fragment('requirements', i, c)
                                                               for i, c in enumerate(requirements)]}}},
        'signature': SEED_SIGNATURE}


class Base(unittest.TestCase):
    step = None

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='quota-e0-'))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        os.chmod(self.root, 0o700)
        self.home = self.mkdir(self.root / 'home')
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in KIT_VARIABLES:
            os.environ.pop(name, None)
        self.codex_home = self.home / '.codex'
        self.secrets = self.mkdir(self.home / '.secrets')
        self.kit_config = self.home / '.config/coding-agent-kit/config.toml'
        self.out = self.mkdir(self.root / 'out')

    def mkdir(self, path, mode=0o700):
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, mode)
        return path

    def write(self, path, data, mode=0o600):
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        os.chmod(path, mode)
        return path

    def fresh_out(self):
        return self.mkdir(Path(tempfile.mkdtemp(prefix='out-', dir=self.root)))

    def run_step(self, step=None, out=None, alias='stanford'):
        step = step or self.step
        out = out or self.out
        self.last_out = out
        stdout, stderr = io.StringIO(), io.StringIO()
        OPENED.clear()
        RECORDING[0] = True
        try:
            code = rk.main([step, '--alias', alias, '--out', str(out)], modules=MODULES, stdout=stdout, stderr=stderr)
        finally:
            RECORDING[0] = False
        path = out / ('e0-%s.result.json' % step)
        text = path.read_text() if path.exists() else ''
        if text:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertNotIn('seed', text.lower())
        return code, json.loads(text) if text else None, text, stdout.getvalue(), stderr.getvalue()

    def opened_under_root(self):
        """Paths opened during the run: relative names (opened by directory fd) and paths under the root."""
        return [path for path in OPENED if not path.startswith('/') or path.startswith(str(self.root) + '/')]

    def assert_nothing_written(self, out=None):
        self.assertEqual(sorted(os.listdir(out or self.out)), [])


class CodexTest(Base):
    step = 'codex'

    def setUp(self):
        super().setUp()
        self.mkdir(self.codex_home)
        self.mkdir(self.codex_home / 'accounts/.locks')
        self.account = self.mkdir(self.codex_home / 'accounts/stanford')
        self.credential = self.write(self.secrets / 'codex-stanford.json', '{"tokens": {"id_token": "seed.decoy.x"}}')
        os.symlink(self.credential, self.account / 'auth.json')
        self.write(self.codex_home / 'config.toml', 'model = "seed-config-model-name"\n')
        self.write(self.codex_home / 'accounts/.locks/stanford.lock', '')
        self.cache = self.account / rk.CODEX_CACHE

    def write_cache(self, document=None, raw=None, mode=0o600):
        return self.write(self.cache, raw if raw is not None else json.dumps(document, indent=2), mode)

    def assert_only_cache_opened(self, cache_opened=True):
        allowed = {str(self.last_out), str(self.account), str(self.kit_config), 'e0-codex.attempt',
                   'e0-codex.result.json'}
        if cache_opened:
            allowed.add(rk.CODEX_CACHE)
        opened = self.opened_under_root()
        self.assertTrue(set(opened) <= allowed, opened)
        self.assertEqual(rk.CODEX_CACHE in opened, cache_opened)

    def test_reports_key_paths_types_and_d14_without_values(self):
        self.write_cache(cache_document(requirements=[PINNING, 'allowed_sandbox_modes = ["read-only"]\n'],
                                        configs=['model = "seed-config-layer-model"\n[features]\nseed_flag = true\n']))
        code, doc, text, stdout, stderr = self.run_step()
        self.assertEqual((code, stderr), (0, ''))
        self.assertEqual((doc['outcome'], doc['shape']), ('read', 'cache-v1'))
        paths = {(item['path'], item['type']) for item in doc['key_paths']}
        self.assertIn(('signed_payload.bundle.requirements_toml.enterprise_managed[].contents', 'string'), paths)
        self.assertIn(('signed_payload.chatgpt_user_id', 'string'), paths)
        self.assertIn(('signature', 'string'), paths)
        self.assertEqual(doc['cache'], {'version_is_1': True, 'chatgpt_user_id_present': True, 'account_id_present': True,
                                        'signature_present': True, 'timestamps_parse': True, 'age_minutes': 1,
                                        'ttl_minutes': 60, 'expired': False})
        first, second = doc['bundle']['requirements']
        self.assertEqual(first['requirement_names'],
                         ['additional_developer_instructions', 'application', 'chatgpt_base_url', 'model_providers'])
        self.assertEqual((first['other_names'], first['application_network'], first['pins'],
                          first['cloud_ignored_names'], first['agent_identity'], first['aliases']),
                         (1, 'network-enabled-unset', ['chatgpt_base_url', 'model_providers'], ['chatgpt_base_url'],
                          'absent', 'ok'))
        self.assertEqual((second['requirement_names'], second['application_network']),
                         (['allowed_sandbox_modes'], 'absent'))
        self.assertEqual(doc['bundle']['config'], [{'toml': 'ok', 'names': ['features'], 'other_names': 1,
                                                    'agent_identity': 'absent'}])
        d14 = doc['d14']
        # Native drops chatgpt_base_url from cloud layers (layer.rs LOCAL_ONLY_AUTH_REQUIREMENTS).
        self.assertEqual((d14['verdict'], d14['reasons']),
                         ('refuse', ['application-network-enabled', 'pins-model-provider']))
        self.assertEqual((d14['application_network_effective'], d14['application_network_enabled'],
                          d14['pinned_forced_keys'], d14['cloud_ignored_names'], d14['config_layer_names']),
                         ('network-enabled-unset', True, ['model_providers'], ['chatgpt_base_url'], ['features']))
        self.assertEqual(json.loads(stdout[len('e0: '):])['verdict'], 'refuse')
        self.assert_only_cache_opened()
        self.assertTrue((self.out / 'e0-codex.attempt').exists())

    def test_disabled_network_and_matching_pins_find_no_blocker(self):
        self.write_cache(cache_document(requirements=[
            '[application.network]\nenabled = false\n',
            'cli_auth_credentials_store = "file"\ncheck_for_update_on_startup = false\nsqlite_home = %s\n'
            % json.dumps(str(self.codex_home))]))
        code, doc, *_ = self.run_step()
        self.assertEqual(code, 0)
        d14 = doc['d14']
        self.assertEqual((d14['verdict'], d14['reasons'], d14['application_network_effective'],
                          d14['application_network_enabled']),
                         ('no-blocker-in-cache', [], 'network-enabled-false', False))
        self.assertEqual(d14['pinned_forced_keys'], ['sqlite_home', 'check_for_update_on_startup'])
        self.assertEqual(d14['pin_matches_codex_as'], {'sqlite_home': True, 'check_for_update_on_startup': True})
        self.assertEqual(d14['cloud_ignored_names'], ['cli_auth_credentials_store'])

    def test_conflicting_pins_and_config_server_refuse(self):
        self.write_cache(cache_document(requirements=[
            'cli_auth_credentials_store = "keyring"\nsqlite_home = "/seed/elsewhere"\nenforce_residency = "us"\n'
            'model_provider = "seed-provider-name"\n[config_server]\nurl = "https://seed-config.example.invalid"\n']))
        code, doc, *_ = self.run_step()
        self.assertEqual(code, 0)
        self.assertEqual(doc['d14']['reasons'], ['config-server-in-cloud-layer', 'pins-enforce-residency',
                                                 'pins-model-provider', 'pins-sqlite-home'])
        self.assertEqual(doc['d14']['pin_matches_codex_as'], {'sqlite_home': False})
        self.assertEqual(doc['bundle']['requirements'][0]['pin_matches_codex_as'], {'sqlite_home': False})

    def test_fragments_compose_as_native_merges_them(self):
        # Bundle order is highest priority first; native merges lowest first, extending tables
        # recursively and replacing scalars (requirements_layers/stack.rs, merge.rs).
        home = json.dumps(str(self.codex_home))
        cases = (
            ('table-then-network', ['[application]\n', '[application.network]\n'],
             'network-enabled-unset', 'refuse', ['application-network-enabled']),
            ('higher-domains-lower-disabled',
             ['[application.network.domains]\n"a.example.com" = "allow"\n', '[application.network]\nenabled = false\n'],
             'network-enabled-false', 'no-blocker-in-cache', []),
            ('higher-enables-lower-disabled',
             ['[application.network]\nenabled = true\n', '[application.network]\nenabled = false\n'],
             'network-enabled-true', 'refuse', ['application-network-enabled']),
            ('higher-matching-pin-wins', ['sqlite_home = %s\n' % home, 'sqlite_home = "/seed/lower"\n'],
             'absent', 'no-blocker-in-cache', []),
            ('higher-conflicting-pin-wins', ['sqlite_home = "/seed/higher"\n', 'sqlite_home = %s\n' % home],
             'absent', 'refuse', ['pins-sqlite-home']))
        for name, fragments, network, verdict, reasons in cases:
            with self.subTest(name):
                self.write_cache(cache_document(requirements=fragments))
                code, doc, *_ = self.run_step(out=self.fresh_out())
                d14 = doc['d14']
                self.assertEqual((code, d14['application_network_effective'], d14['verdict'], d14['reasons']),
                                 (0, network, verdict, reasons))
        states = [view['application_network'] for view in doc['bundle']['requirements']]
        self.assertEqual(states, ['absent', 'absent'])
        self.write_cache(cache_document(requirements=['[application.network]\nenabled = true\n',
                                                      '[application.network]\n', '[application.network]\nenabled = false\n']))
        doc = self.run_step(out=self.fresh_out())[1]
        self.assertEqual([view['application_network'] for view in doc['bundle']['requirements']],
                         ['network-enabled-true', 'network-enabled-unset', 'network-enabled-false'])

    def test_invalid_network_flag_is_unevaluable(self):
        for name, fragment in (('flag', '[application.network]\nenabled = "yes"\n'),
                               ('network-field', '[application.network]\nallowed_domains = ["a.example.com"]\n'),
                               ('application-field', '[application]\nseed_field = 1\n')):
            with self.subTest(name):
                self.write_cache(cache_document(requirements=[fragment, '[application.network]\nenabled = false\n']))
                doc = self.run_step(out=self.fresh_out())[1]
                self.assertEqual((doc['d14']['verdict'], doc['d14']['reasons']),
                                 ('unevaluable', ['application-network-invalid']))

    def test_agent_identity_feature(self):
        cases = (('requirement', ['[features]\nuse_agent_identity = true\n'], [], 'refuse', ['forces-agent-identity']),
                 ('requirement-alias', ['[feature_requirements]\nuse_agent_identity = true\n'], [], 'refuse',
                  ['forces-agent-identity']),
                 ('requirement-overridden', ['[features]\nuse_agent_identity = false\n',
                                             '[features]\nuse_agent_identity = true\n'], [], 'no-blocker-in-cache', []),
                 ('config', [], ['[features]\nuse_agent_identity = true\n'], 'refuse', ['enables-agent-identity']),
                 ('config-off', [], ['[features]\nuse_agent_identity = false\n'], 'no-blocker-in-cache', []),
                 ('invalid', ['[features]\nuse_agent_identity = "yes"\n'], [], 'unevaluable', ['agent-identity-invalid']),
                 ('alias-conflict', ['[features]\nx_flag = true\n[feature_requirements]\ny_flag = true\n'], [],
                  'unevaluable', ['requirements-alias-conflict']))
        for name, requirements, configs, verdict, reasons in cases:
            with self.subTest(name):
                self.write_cache(cache_document(requirements=requirements, configs=configs))
                doc = self.run_step(out=self.fresh_out())[1]
                self.assertEqual((doc['d14']['verdict'], doc['d14']['reasons']), (verdict, reasons))
        self.assertEqual(doc['bundle']['requirements'][0]['aliases'], 'conflict')

    def test_known_names_in_the_bundle_are_not_leaks(self):
        # Nested TOML names that equal or sit inside retained requirement names (codex feature keys).
        for name, requirements, configs, names in (
                ('remote-control', ['allow_remote_control = false\n[features]\nremote_control = false\n'], [],
                 ['allow_remote_control', 'features']),
                ('computer-use', ['allow_browser_and_computer_use = false\n[computer_use]\nseed_x = 1\n'],
                 ['[features]\ncomputer_use = false\nin_app_browser = false\n'],
                 ['allow_browser_and_computer_use', 'computer_use'])):
            with self.subTest(name):
                self.write_cache(cache_document(requirements=requirements, configs=configs))
                code, doc, _text, stdout, _err = self.run_step(out=self.fresh_out())
                self.assertEqual((code, doc.get('sanitizer_findings')), (0, None))
                self.assertEqual(doc['bundle']['requirements'][0]['requirement_names'], names)
                self.assertEqual(json.loads(stdout[len('e0: '):])['sanitizer_findings'], [])

    def test_a_value_in_the_result_is_redacted_and_exits_3(self):
        document = cache_document(requirements=['allow_login_shell = false\n'])
        document['signed_payload']['bundle']['requirements_toml']['enterprise_managed'][0]['name'] = 'leaked_seed_name'
        document['signed_payload']['leaked_seed_name'] = 1  # a key path segment equal to a document value
        self.write_cache(document)
        code, doc, *_ = self.run_step()
        self.assertEqual((code, doc['sanitizer_findings']), (3, ['known-value']))
        self.assertIn(('signed_payload.<redacted>', 'integer'), {(item['path'], item['type'])
                                                                 for item in doc['key_paths']})

    def test_marker_write_failure(self):
        self.write_cache(cache_document())

        def full(*_args):
            raise OSError(errno.ENOSPC, 'no space')
        with mock.patch.object(rk, 'write_exclusive', full):
            code, _doc, _text, _out, stderr = self.run_step()
        self.assertEqual((code, stderr), (2, 'e0: refused: attempt-marker-not-written\n'))
        self.assert_nothing_written()
        with mock.patch.object(rk.os, 'fsync', full):
            code, _doc, _text, _out, stderr = self.run_step()
        self.assertEqual((code, stderr), (3, 'e0: refused: attempt-marker-incomplete\n'))
        self.assertEqual(os.listdir(self.out), ['e0-codex.attempt'])
        self.assertEqual(self.run_step()[0], 4)

    def test_result_write_failure_after_the_marker(self):
        self.write_cache(cache_document())
        original = rk.write_exclusive

        def marker_only(directory, name, text):
            if name.endswith('.result.json'):
                raise OSError(errno.EDQUOT, 'quota')
            return original(directory, name, text)
        with mock.patch.object(rk, 'write_exclusive', marker_only):
            code, _doc, _text, stdout, stderr = self.run_step()
        self.assertEqual((code, stderr), (3, ''))
        self.assertEqual(json.loads(stdout[len('e0: '):])['outcome'], 'result-not-written')
        self.assertEqual(os.listdir(self.out), ['e0-codex.attempt'])

    def test_stale_cache_age_and_expiry(self):
        self.write_cache(cache_document(cached_offset=-23.3 * 3600))
        doc = self.run_step()[1]
        self.assertEqual((doc['cache']['age_minutes'], doc['cache']['ttl_minutes'], doc['cache']['expired']),
                         (1398, 60, True))
        self.assertEqual((doc['d14']['verdict'], doc['bundle']['requirements_fragments']), ('no-blocker-in-cache', 0))

    def test_unknown_and_id_like_keys(self):
        document = cache_document(requirements=['allow_remote_control = false\n'])
        document['signed_payload']['bundle']['seed-workspace-0f1e2d3c4b5a6978'] = {'x': 1}
        document['0a1b2c3d-1111-2222-3333-444455556666'] = 'seed-uuid-keyed-value'
        document['extraField'] = [1, 2.5, None, True]
        self.write_cache(document)
        doc = self.run_step()[1]
        paths = {(item['path'], item['type']) for item in doc['key_paths']}
        self.assertIn(('{id}', 'string'), paths)
        self.assertIn(('signed_payload.bundle.{id}.x', 'integer'), paths)
        self.assertIn(('extraField[]', 'number'), paths)
        self.assertIn(('extraField[]', 'null'), paths)
        self.assertEqual((doc['shape'], doc['d14']['verdict']), ('cache-v1-extra-keys', 'no-blocker-in-cache'))

    def test_unrecognized_shape_and_bad_toml_are_unevaluable(self):
        for name, document in (('shape', {'signed_payload': {'version': '1'}, 'signature': 7}),
                               ('toml', cache_document(requirements=['chatgpt_base_url = "seed-unterminated\n']))):
            with self.subTest(name):
                self.write_cache(document)
                code, doc, *_ = self.run_step(out=self.fresh_out())
                self.assertEqual((code, doc['d14']['verdict']), (0, 'unevaluable'))
                if name == 'toml':
                    self.assertEqual(doc['bundle']['requirements'], [{'toml': 'malformed'}])

    def test_unsupported_cache_version_is_unevaluable(self):
        document = cache_document(requirements=['allow_login_shell = false\n'])
        document['signed_payload']['version'] = 2  # native: CacheVersionUnsupported, the cache is ignored
        self.write_cache(document)
        doc = self.run_step()[1]
        self.assertEqual((doc['shape'], doc['cache']['version_is_1'], doc['d14']['verdict'], doc['d14']['reasons']),
                         ('cache-version-unsupported', False, 'unevaluable', ['cache-version-unsupported']))

    def test_malformed_json_is_never_echoed(self):
        for raw, outcome in ((b'{"seed-key": "seed-value", ', 'parse-malformed'),
                             (b'{"a": "seed-one", "a": "seed-two"}', 'parse-duplicate-key'),
                             (b'{"seed": NaN}', 'parse-non-finite-number'),
                             (b'{"seed": "\xff\xfe"}', 'parse-not-utf8')):
            with self.subTest(outcome):
                self.write_cache(raw=raw)
                code, doc, *_ = self.run_step(out=self.fresh_out())
                self.assertEqual((code, doc['outcome'], doc['d14']['verdict']), (0, outcome, 'unevaluable'))
                self.assertNotIn('key_paths', doc)

    def test_unsafe_files_are_never_opened(self):
        target = self.write(self.root / 'seed-target.json', json.dumps(cache_document(requirements=[PINNING])))
        cases = (('symlink', lambda: os.symlink(target, self.cache)),
                 ('not-regular', lambda: os.mkfifo(self.cache, 0o600)),
                 ('mode-not-private', lambda: self.write_cache(cache_document(), mode=0o640)),
                 ('hard-linked', lambda: (self.write_cache(cache_document()), os.link(self.cache, self.root / 'link'))),
                 ('oversize', lambda: self.write_cache(raw=b'{' + b' ' * rk.FILE_LIMIT + b'}')))
        for outcome, make in cases:
            with self.subTest(outcome):
                for path in (self.cache, self.root / 'link'):
                    if os.path.lexists(path):
                        os.unlink(path)
                make()
                code, doc, *_ = self.run_step(out=self.fresh_out())
                self.assertEqual((code, doc['outcome'], doc['d14']['reasons']), (0, outcome, ['cache-' + outcome]))
                self.assertTrue(doc['file']['present'])
                self.assert_only_cache_opened(cache_opened=False)
                self.assertNotIn(str(target), OPENED)

    def test_absent_cache(self):
        code, doc, *_ = self.run_step()
        self.assertEqual((code, doc['outcome'], doc['file'], doc['d14']['verdict']),
                         (0, 'absent', {'present': False}, 'unevaluable'))

    def test_other_managed_config_caches_are_counted_not_opened(self):
        self.write(self.account / 'managed-config-seedspace.json', '{"seed": "x"}')
        self.write_cache(cache_document())
        doc = self.run_step()[1]
        self.assertEqual(doc['other_managed_config_caches'], 1)
        self.assert_only_cache_opened()

    def test_access_time_inode_and_mtime_unchanged(self):
        self.write_cache(cache_document())
        old = time.time() - 86400
        os.utime(self.cache, (old, os.stat(self.cache).st_mtime))
        before = os.stat(self.cache)
        doc = self.run_step()[1]
        after = os.stat(self.cache)
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_size), (after.st_ino, after.st_mtime_ns,
                                                                              after.st_size))
        if rk.NOATIME:
            self.assertTrue(doc['file']['access_time_kept'])
            self.assertEqual(before.st_atime_ns, after.st_atime_ns)

    def test_change_or_replacement_during_read(self):
        def once(name, action):
            original = getattr(os, name)
            state = {'done': False}

            def call(*args):
                if not state['done'] and RECORDING[0]:
                    state['done'] = True
                    action()
                return original(*args)
            return mock.patch.object(rk.os, name, call)

        def append():
            with open(self.cache, 'ab') as handle:
                handle.write(b'  ')

        def replace():
            os.replace(self.write(self.root / 'replacement', json.dumps(cache_document())), self.cache)

        # Replacing during the read drops the open inode's last link (fstat sees it); replacing after the
        # last fstat, before the close, is caught by the second lstat.
        for name, (hook, action), outcome in (('appended', ('read', append), 'changed-during-read'),
                                              ('replaced', ('read', replace), 'changed-during-read'),
                                              ('replaced-late', ('close', replace), 'replaced-during-read')):
            with self.subTest(name):
                self.write_cache(cache_document())
                with once(hook, action):
                    code, doc, *_ = self.run_step(out=self.fresh_out())
                self.assertEqual((code, doc['outcome']), (0, outcome))

    def test_no_socket_is_created(self):
        self.write_cache(cache_document(requirements=[PINNING]))
        with mock.patch.object(socket, 'socket', side_effect=AssertionError('socket')):
            code, doc, *_ = self.run_step()
        self.assertEqual((code, doc['outcome']), (0, 'read'))

    def test_one_shot_and_refusals_before_the_marker(self):
        self.write_cache(cache_document())
        self.assertEqual(self.run_step()[0], 0)
        result = (self.out / 'e0-codex.result.json').read_bytes()
        code, _doc, _text, _out, stderr = self.run_step()
        self.assertEqual((code, stderr), (4, 'e0: refused: result-exists\n'))
        self.assertEqual((self.out / 'e0-codex.result.json').read_bytes(), result)
        os.unlink(self.out / 'e0-codex.result.json')
        self.assertEqual(self.run_step()[0], 4)  # the marker alone refuses
        missing = self.fresh_out()
        shutil.rmtree(self.account)
        code, _doc, _text, _out, stderr = self.run_step(out=missing)
        self.assertEqual((code, stderr), (2, 'e0: refused: account-home-missing\n'))
        self.assert_nothing_written(missing)

    def test_unsafe_account_home_refuses_before_the_marker(self):
        os.chmod(self.account, 0o750)
        code, _doc, _text, _out, stderr = self.run_step()
        self.assertEqual((code, stderr), (2, 'e0: refused: account-home-unsafe\n'))
        self.assert_nothing_written()

    def test_command_line_entry(self):
        self.write_cache(cache_document(requirements=[PINNING]))
        env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin')}
        result = subprocess.run([sys.executable, '-I', '-B', str(HERE / 'read_keys.py'), 'codex', '--alias', 'stanford',
                                 '--out', str(self.out)], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertNotIn(str(self.root), result.stdout)
        text = (self.out / 'e0-codex.result.json').read_text()
        self.assertNotIn('seed', text.lower())
        self.assertEqual(json.loads(text)['d14']['verdict'], 'refuse')


class ClaudeTest(Base):
    step = 'claude'

    def setUp(self):
        super().setUp()
        self.account = self.mkdir(self.secrets / 'native-claude-accounts/stanford')
        self.config = self.mkdir(self.account / 'config')
        self.write(self.account / 'account.json', '{"identity": "seed-registration"}')
        self.write(self.config / '.credentials.json', '{"claudeAiOauth": {"accessToken": "seed-access"}}')
        self.write(self.config / '.claude.json', '{"oauthAccount": "seed-metadata"}')
        self.settings = self.config / 'remote-settings.json'
        self.limits = self.config / 'policy-limits.json'

    def write_json(self, path, document, mode=0o600):
        return self.write(path, json.dumps(document), mode)

    def assert_only_policy_opened(self, *names):
        allowed = set(Path(str(self.config)).parts[1:]) | {str(self.last_out), str(self.kit_config), 'e0-claude.attempt',
                                                           'e0-claude.result.json'} | set(names)
        opened = self.opened_under_root()
        self.assertTrue(set(opened) <= allowed, opened)
        for forbidden in ('.credentials.json', 'account.json', '.claude.json'):
            self.assertNotIn(forbidden, [os.path.basename(path) for path in opened])

    def test_empty_settings_and_unreviewed_policy_limits(self):
        self.write(self.settings, '{}')
        self.write_json(self.limits, {
            'restrictions': {'allow_remote_sessions': {'allowed': False}, 'allow_web_fetch': {'allowed': True}},
            'compliance_taints': ['seed-regime'], 'defaults': {'someSettingName': 'seed-default-value'},
            'monitoring_notice': {'text': 'Seed monitoring notice text', 'url': 'https://seed.example.invalid/x'},
            'futureKey': 'seed-future-value'})
        code, doc, text, stdout, stderr = self.run_step()
        self.assertEqual((code, stderr, doc['outcome']), (0, '', 'completed'))
        settings = doc['files']['remote-settings.json']
        self.assertEqual((settings['keys'], settings['scopes']['native']['verdict'],
                          settings['scopes']['bearer']['verdict']), ([], 'accepted', 'accepted'))
        limits = doc['files']['policy-limits.json']
        self.assertEqual([(key['name'], key['in_native_2_1_322_schema'], key['native'], key['bearer'])
                          for key in limits['keys']],
                         [('compliance_taints', True, 'unknown', 'unknown'), ('defaults', True, 'unknown', 'unknown'),
                          ('futureKey', False, 'unknown', 'unknown'), ('monitoring_notice', True, 'unknown', 'unknown'),
                          ('restrictions', True, 'unknown', 'unknown')])
        self.assertEqual(limits['restrictions'], {'names': ['allow_remote_sessions', 'allow_web_fetch'],
                                                  'names_truncated': 0})
        self.assertEqual(limits['defaults'], {'names': ['someSettingName'], 'names_truncated': 0})
        # Key names and verdicts only (g0-approval.json "e0"): no value type, count or length.
        self.assertNotIn('compliance_taints', limits)
        self.assertTrue(all('type' not in key for key in limits['keys']))
        self.assertNotIn('allowed', text)
        self.assertNotIn('"type"', text)
        self.assertNotIn('"count"', text)
        for scope in ('native', 'bearer'):
            self.assertEqual(doc['scopes'][scope], {'verdict': 'refused', 'reasons': ['policy_limits_unreviewed_keys']})
        self.assert_only_policy_opened('remote-settings.json', 'policy-limits.json')

    def test_every_reviewed_rule_and_scope(self):
        self.write_json(self.settings, {
            'env': {'ANTHROPIC_BASE_URL': 'https://seed.example.invalid', 'SEED_HARMLESS_NAME': 'seed-value'},
            'apiKeyHelper': 'seed-helper-command', 'policyHelper': None, 'hooks': {'Stop': [{'seed': 'command'}]},
            'statusLine': None, 'forceLoginMethod': 'console', 'permissions': {'deny': ['seed-rule']},
            '$schema': 'https://seed.example.invalid/schema', '$comment': 'seed-comment-value'})
        doc = self.run_step()[1]
        settings = doc['files']['remote-settings.json']
        keys = {key['name']: (key['native'], key['bearer'], key['reason']) for key in settings['keys']}
        self.assertEqual(keys, {
            'env': ('refused', 'refused', 'policy_conflicting_env'),
            'apiKeyHelper': ('refused', 'refused', 'policy_conflicting_auth'),
            'policyHelper': ('refused', 'refused', 'policy_helper_unevaluable'),
            'hooks': ('active', 'accepted', 'policy_command_unevaluable'),
            'statusLine': ('accepted', 'accepted', None),
            'forceLoginMethod': ('refused', 'refused', 'policy_login_method_incompatible'),
            'permissions': ('unknown', 'unknown', None),
            '$schema': ('unknown', 'unknown', None),
            '$comment': ('unknown', 'unknown', None)})
        self.assertEqual(settings['env'], {'names': 2, 'refused_names': 1})
        self.assertEqual(settings['scopes']['native']['reasons'],
                         ['policy_command_unevaluable', 'policy_conflicting_auth', 'policy_conflicting_env',
                          'policy_helper_unevaluable', 'policy_login_method_incompatible'])
        self.assertEqual(settings['scopes']['bearer']['reasons'],
                         ['policy_conflicting_auth', 'policy_conflicting_env', 'policy_helper_unevaluable',
                          'policy_login_method_incompatible'])
        self.assertEqual([key['name'] for key in settings['keys'] if key['ignored_by_native']], ['$comment'])
        self.assertEqual(settings['unknown_keys'], 3)
        self.assertTrue(all(set(key) == {'name', 'native', 'bearer', 'rule', 'reason', 'ignored_by_native'}
                            for key in settings['keys']))

    def test_a_wrapper_around_reviewed_keys_is_not_accepted(self):
        # The traced loader applies the bare object; an envelope would hide reviewed keys one level down.
        self.write_json(self.settings, {'settings': {'apiKeyHelper': 'seed-helper-command'},
                                        'checksum': 'seed-checksum-value', 'permissions': {'deny': ['seed-rule']}})
        doc = self.run_step()[1]
        settings = doc['files']['remote-settings.json']
        rules = {key['name']: key['rule'] for key in settings['keys']}
        self.assertEqual(rules, {'settings': 'wraps-reviewed-keys', 'checksum': 'not-in-qc-review',
                                 'permissions': 'not-in-qc-review'})
        for scope in ('native', 'bearer'):
            self.assertEqual(doc['scopes'][scope], {'verdict': 'unevaluated',
                                                    'reasons': ['server_policy_possible_envelope']})

    def test_native_only_active_keys_pass_the_bearer_scope(self):
        self.write_json(self.settings, {'hooks': {'Stop': []}, 'forceRemoteSettingsRefresh': True,
                                        'forceLoginMethod': 'claudeai', 'env': {'SEED_NAME': 'x'}})
        doc = self.run_step()[1]
        self.assertEqual(doc['files']['remote-settings.json']['scopes'],
                         {'native': {'verdict': 'refused', 'reasons': ['policy_command_unevaluable']},
                          'bearer': {'verdict': 'accepted', 'reasons': []}})
        self.assertEqual(doc['scopes']['bearer'], {'verdict': 'accepted', 'reasons': []})

    def test_organization_pin_is_reported_not_compared(self):
        self.write_json(self.settings, {'forceLoginOrgUUID': ['seed-organization-0001']})
        doc = self.run_step()[1]
        for scope in ('native', 'bearer'):
            self.assertEqual(doc['scopes'][scope], {'verdict': 'unevaluated',
                                                    'reasons': ['policy_organization_not_compared']})

    def test_unsafe_files_are_never_opened(self):
        target = self.write(self.root / 'seed-target.json', '{"apiKeyHelper": "seed"}')
        cases = (('symlink', lambda: os.symlink(target, self.settings)),
                 ('not-regular', lambda: os.mkfifo(self.settings, 0o600)),
                 ('mode-not-0600', lambda: self.write(self.settings, '{}', 0o400)),
                 ('hard-linked', lambda: (self.write(self.settings, '{}'), os.link(self.settings, self.root / 'link'))),
                 ('oversize', lambda: self.write(self.settings, b'{' + b' ' * rk.FILE_LIMIT + b'}')))
        for outcome, make in cases:
            with self.subTest(outcome):
                for path in (self.settings, self.root / 'link'):
                    if os.path.lexists(path):
                        os.unlink(path)
                make()
                code, doc, *_ = self.run_step(out=self.fresh_out())
                report = doc['files']['remote-settings.json']
                reason = 'server_policy_' + outcome.replace('-', '_')
                self.assertEqual((code, doc['outcome'], report['outcome']), (0, 'incomplete', outcome))
                self.assertEqual(doc['scopes']['bearer'], {'verdict': 'refused', 'reasons': [reason]})
                self.assert_only_policy_opened()
                self.assertNotIn(str(target), OPENED)

    def test_shapes_native_would_not_apply_refuse(self):
        for raw, outcome, reason in ((b'[1, 2]', 'read', 'server_policy_shape_unrecognized'),
                                     (b'{"seed": ', 'parse-malformed', 'server_policy_unparsed')):
            with self.subTest(outcome):
                self.write(self.limits, raw)
                doc = self.run_step(out=self.fresh_out())[1]
                self.assertEqual(doc['files']['policy-limits.json']['outcome'], outcome)
                self.assertEqual(doc['scopes']['native'], {'verdict': 'refused', 'reasons': [reason]})

    def test_absent_files_are_accepted(self):
        code, doc, *_ = self.run_step()
        self.assertEqual((code, doc['outcome']), (0, 'completed'))
        self.assertEqual(doc['files']['policy-limits.json']['file'], {'present': False})
        self.assertEqual(doc['scopes']['native'], {'verdict': 'accepted', 'reasons': []})
        self.assert_only_policy_opened()

    def test_policy_files_unchanged(self):
        self.write(self.settings, '{}')
        old = time.time() - 86400
        os.utime(self.settings, (old, os.stat(self.settings).st_mtime))
        before = os.stat(self.settings)
        self.run_step()
        after = os.stat(self.settings)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        if rk.NOATIME:
            self.assertEqual(before.st_atime_ns, after.st_atime_ns)

    def test_unsafe_directories_refuse_before_the_marker(self):
        os.chmod(self.config, 0o750)
        code, _doc, _text, _out, stderr = self.run_step()
        self.assertEqual((code, stderr), (2, 'e0: refused: account_directory_not_private\n'))
        self.assert_nothing_written()
        os.chmod(self.config, 0o700)
        moved = self.account.with_name('elsewhere')
        os.rename(self.account, moved)
        os.symlink(moved, self.account)
        code, _doc, _text, _out, stderr = self.run_step()
        self.assertEqual((code, stderr), (2, 'e0: refused: path_component_unusable\n'))
        self.assert_nothing_written()

    def test_both_steps_share_one_out_directory(self):
        self.mkdir(self.codex_home / 'accounts/stanford')
        self.write(self.settings, '{}')
        self.assertEqual(self.run_step()[0], 0)
        self.assertEqual(self.run_step('codex')[0], 0)
        self.assertEqual(sorted(os.listdir(self.out)), ['e0-claude.attempt', 'e0-claude.result.json',
                                                         'e0-codex.attempt', 'e0-codex.result.json'])
        self.assertEqual(self.run_step()[0], 4)


class OutDirectoryTest(Base):
    step = 'claude'

    def setUp(self):
        super().setUp()
        self.mkdir(self.secrets / 'native-claude-accounts/stanford/config')

    def test_out_directory_refusals(self):
        repository = self.mkdir(self.root / 'repository')
        self.mkdir(repository / '.git')
        cases = (('out-missing', self.root / 'missing'),
                 ('out-not-private', self.mkdir(self.root / 'open', 0o750)),
                 ('out-in-git-work-tree', self.mkdir(repository / 'out')),
                 ('out-overlaps-account-state-or-repository', self.mkdir(self.secrets / 'out')),
                 ('out-overlaps-account-state-or-repository', self.mkdir(self.codex_home / 'out')),
                 ('out-overlaps-account-state-or-repository', self.home))
        for reason, out in cases:
            with self.subTest(reason=reason, out=out.name):
                code, _doc, _text, _out, stderr = self.run_step(out=out)
                self.assertEqual((code, stderr), (2, 'e0: refused: %s\n' % reason))
        os.symlink(self.out, self.root / 'link')
        self.assertEqual(self.run_step(out=self.root / 'link')[4], 'e0: refused: out-not-directory\n')
        self.assertEqual(self.run_step(out=Path('relative'))[4], 'e0: refused: out-not-absolute\n')
        self.assert_nothing_written()

    def test_alias_other_than_the_approved_one_refuses(self):
        code, _doc, _text, _out, stderr = self.run_step(alias='other')
        self.assertEqual((code, stderr), (2, 'e0: refused: alias-not-approved\n'))
        self.assert_nothing_written()


class PureTest(unittest.TestCase):
    def test_safe_names(self):
        for name in ('restrictions', 'allow_web_fetch', '$schema', 'x-header', 'enterprise_managed'):
            self.assertEqual(rk.safe_name(name), name)
        for name in ('0a1b2c3d-1111-2222-3333-444455556666', 'acct_ABCdef1234567890XYZ', 'org-deadbeef00112233',
                     'host.example', 'a/b', 'user@x', 'n' * 65, '123456', ''):
            self.assertEqual(rk.safe_name(name), '{id}')

    def test_stamps(self):
        self.assertEqual(rk.parse_stamp('1970-01-01T00:00:01.500000000Z'), 1.5)
        self.assertEqual(rk.parse_stamp('1970-01-01T01:00:00+01:00'), 0.0)
        self.assertIsNone(rk.parse_stamp('yesterday'))

    def test_sanitizer_redacts_or_blocks(self):
        text, findings = rk.sanitize({'probe': 'e0-x', 'name': 'seed-known-value-123'}, {'seed-known-value-123'})
        self.assertEqual(findings, ['known-value'])
        self.assertNotIn('seed-known-value-123', text)
        self.assertEqual(json.loads(text)['sanitizer_findings'], ['known-value'])
        text, findings = rk.sanitize({'probe': 'e0-x', 'paths': [{'path': 'a.seed_known_name_12[].b'}]},
                                     {'seed_known_name_12'})
        self.assertEqual((findings, json.loads(text)['paths']), (['known-value'], [{'path': 'a.<redacted>[].b'}]))
        text, findings = rk.sanitize({'probe': 'e0-x', 'seed_known_key_name': 1}, {'seed_known_key_name'})
        self.assertEqual((findings, '<redacted>' in json.loads(text)), (['known-value'], True))
        text, findings = rk.sanitize({'probe': 'e0-x', 'name': 'seed-café-value-12'}, {'seed-café-value-12'})
        self.assertEqual((findings, json.loads(text)['name']), (['known-value'], '<redacted>'))
        text, findings = rk.sanitize({'probe': 'e0-x', 'name': 'in /seed/home/path/x too'}, {'/seed/home/path'})
        self.assertEqual((findings, json.loads(text)['name']), (['known-value'], 'in <redacted>/x too'))
        text, findings = rk.sanitize({'probe': 'e0-x', 'name': 'eyJseedseed.eyJseedseed.sig0'}, set())
        self.assertEqual((findings, json.loads(text)['name']), (['jwt-shaped'], '<redacted>'))

    def test_sanitizer_ignores_vocabulary_and_partial_names(self):
        result = {'probe': 'e0-x', 'requirement_names': ['allow_remote_control', 'computer_use'],
                  'key_paths': [{'path': 'signed_payload.bundle.requirements_toml'}]}
        for secrets in ({'remote_control'}, {'computer_use'}, {'requirements_toml'}, {'token-shaped'},
                        {'allow_remote'}):
            with self.subTest(sorted(secrets)[0]):
                self.assertEqual(rk.sanitize(result, secrets), (rk.dump(result), []))


if __name__ == '__main__':
    unittest.main()
