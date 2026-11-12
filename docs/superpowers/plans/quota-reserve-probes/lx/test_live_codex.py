"""LX probe orchestration tests with a fake app-server, a temporary HOME and loopback TLS.

Covers the one-shot guard, lease-held and layout refusals (nothing written, nothing
repaired), the private out directory, the workspace under a private XDG_RUNTIME_DIR
with no .codex ancestor, the version and binary pins, the owner account claim and the
gate's account scope, the read sequence and second-read rule, model POST refusal,
unarmed extra reads, refresh detection, TLS failure without fallback, process-group
cleanup, main()'s production wiring, no project, user or host name in what native
is given or sends upstream, and a leak scan of every output.
"""
import fcntl
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import stat
import time
import unittest
from unittest import mock

HERE = Path(os.path.realpath(__file__)).parent
spec = importlib.util.spec_from_file_location('lx_testkit', HERE / 'testkit.py')
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)
live, gates, qx = kit.live, kit.gates, kit.qx


@unittest.skipUnless(kit.openssl(), 'openssl command not found; TLS fixtures need it')
class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs_root = kit.temp_root('certs')
        cls.certs = kit.make_ca(cls.certs_root / 'ca')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.certs_root, ignore_errors=True)

    def setUp(self):
        self.root = kit.temp_root('probe')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.report = self.root / 'fake-report.jsonl'
        self.server = kit.TlsUsageServer(*self.certs['leaf'])
        self.addCleanup(self.server.close)
        self.out = self.root / 'out'
        self.runtime = self.root / 'run'  # stands in for the caller's private XDG_RUNTIME_DIR
        self.runtime.mkdir(mode=0o700)

    def make_home(self, modes='', version='0.167.2', **home):
        self.home = kit.Home(self.root, self.set_fake(modes, version), **home)
        return self.home

    def set_fake(self, modes='', version='0.167.2'):
        """(Re)write the fake codex the kit configuration names; returns its path."""
        bindir = self.root / 'bin'
        bindir.mkdir(exist_ok=True)
        codex = kit.fake_codex(bindir, FAKE_CODEX_MODE=modes, FAKE_CODEX_REPORT=self.report,
                               FAKE_CODEX_VERSION=version)
        self.fake_sha = live.sha256(codex)
        return codex

    def run_probe(self, upstream=None, tunnel=None, guard_command=None, delay=0.2, second=True, environ=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        kw.setdefault('expect_sha256', self.fake_sha)  # the fake is not the reviewed binary
        environ = dict({'XDG_RUNTIME_DIR': str(self.runtime)}, **(environ or {}))
        with self.home.environment(**{key: value for key, value in environ.items() if value is not None}) as env:
            code = live.run_probe(self.home.alias, self.out, config=self.home.config,
                                  upstream=upstream or kit.test_upstream(self.server, self.certs['ca']),
                                  tunnel=tunnel, guard_command=guard_command, delay=delay, second=second,
                                  environ=env, stdout=out, stderr=err, **kw)
        self.stdout, self.stderr = out.getvalue(), err.getvalue()
        return code

    def result(self):
        return json.loads((self.out / live.RESULT).read_text())

    def events(self, name=None):
        if not self.report.exists():
            return []
        rows = [json.loads(line) for line in self.report.read_text().splitlines()]
        return [row for row in rows if name is None or row['event'] == name]

    def assert_no_leak(self, result=None):
        sha = (result or {}).get('native', {}).get('binary_sha256')
        texts = kit.outputs(self.out, self.stdout, self.stderr)
        secrets = self.home.secrets() + [os.uname().nodename, '00000000-0000-4000-8000-00000000f1c7']
        self.assertEqual(kit.scan(texts, secrets, allowed=(sha,) if sha else ()), [])

    def assert_untouched(self):
        self.assertEqual(self.home.secret.read_bytes(), self.home.raw.encode())

    def assert_refused_before_spawn(self, reason):
        self.assertIn('refused before spawn: ' + reason, self.stderr)
        self.assertEqual(self.events(), [])  # nothing spawned
        self.assertEqual(self.server.count(), 0)
        self.assertFalse(self.out.exists() and os.listdir(self.out))

    def rewrite_secret(self, raw):
        """Replace the codex-as secret the account link points at (0600, single link)."""
        staged = self.home.secret.with_name('.staged-fixture')
        staged.write_bytes(raw.encode())
        staged.chmod(0o600)
        os.replace(staged, self.home.secret)

    def test_full_run_reads_twice_and_retains_only_sanitized_fields(self):
        self.make_home()
        self.assertEqual(self.run_probe(), 0, self.stderr)
        result = self.result()
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
        self.assertEqual(sorted(os.listdir(self.out)), [live.ATTEMPT, live.RESULT])  # workspace removed
        for name in (live.ATTEMPT, live.RESULT):
            self.assertEqual(stat.S_IMODE(os.stat(self.out / name).st_mode), 0o600)
        rpc = result['rpc']
        self.assertEqual(rpc['methods_sent'], ['initialize', 'initialized', 'account/rateLimits/read',
                                               'account/rateLimits/read'])
        self.assertEqual((rpc['second_read'], [read['state'] for read in rpc['reads']]), ('done', ['ok', 'ok']))
        self.assertEqual([read['forwards_during_read'] for read in rpc['reads']], [1, 1])
        self.assertEqual(rpc['notifications'], {'remoteControl/status/changed': 1})
        native = rpc['reads'][0]['native']
        self.assertEqual(native['rate_limits']['primary'], {'usedPercent': 25, 'windowDurationMins': 300,
                                                            'resetsAt': native['rate_limits']['primary']['resetsAt']})
        self.assertEqual((native['account_id_present'], native['account_id_equals_codex_as_account'],
                          native['ordinary_usage_allowed']), (True, True, True))
        gate = result['gate']
        self.assertEqual((gate['forwarded'], [item['status'] for item in gate['exchanges']]), (2, [200, 200]))
        for exchange in gate['exchanges']:
            identity = exchange['identity']
            self.assertTrue(all(value for key, value in identity.items() if key != 'bearer_exp_within_300s'), identity)
            self.assertEqual(exchange['qx_lease_verdict'], 'observation:five_hour+weekly')
            self.assertTrue(exchange['bearer_same_as_first_forward'])
        self.assertEqual(result['freshness']['comparable'], True)
        self.assertEqual(result['credential'], {'link_intact_before': True, 'changed_during_run': False,
                                                'link_intact_after': True, 'still_private_regular_file': True})
        self.assertEqual(result['native']['user_agent_version'], '0.167.2')
        self.assertTrue(result['native']['reap']['process_group_empty'])
        self.assertEqual(self.server.count(gates.USAGE_PATH), 2)
        # Privacy: the client name native builds its User-Agent from, and every header that reached
        # the upstream (Authorization excluded), carry no project, user, host or home identifier.
        self.assertEqual([row['client_info'] for row in self.events('initialize')], [live.CLIENT_INFO])
        self.assertEqual(kit.identifier_findings(json.dumps(live.CLIENT_INFO), self.root), [])
        self.assertEqual(len(self.server.sent), 2)
        for headers in self.server.sent:
            self.assertEqual([value for name, value in headers if name.lower() == 'user-agent'],
                             ['usage-probe/0.167.2 (fake; linux)'])
            self.assertEqual(kit.identifier_findings(kit.header_text(headers), self.home.home, self.root), [])
        # The workspace (cwd and TMPDIR) lived in the private runtime directory and is gone.
        self.assertEqual(result['workspace'], {  # under the system temporary directory an ancestor is open
            'parent': 'xdg-runtime-dir', 'ancestor_codex_found': False,
            'ancestors_closed_to_other_writers': live.check_workspace_ancestors(self.runtime / 'probe')})
        start = self.events('start')[0]
        self.assertEqual(Path(start['cwd']).parent, self.runtime)
        self.assertTrue(Path(start['cwd']).name.startswith(live.WORKSPACE_PREFIX))
        self.assertEqual(start['tmpdir'], start['cwd'])
        self.assertEqual(os.listdir(self.runtime), [])
        self.assertEqual(start['codex_home'], str(self.home.config.codex_home / 'accounts' / kit.ALIAS))
        self.assertEqual(len(start['lock_fds']), 1)  # the account lease fd is inherited
        self.assertTrue(start['lock_fds'][0].endswith('/accounts/.locks/%s.lock' % kit.ALIAS))
        overrides = start['overrides']
        self.assertTrue(overrides['chatgpt_base_url'].startswith('"http://127.0.0.1:'))
        self.assertEqual(overrides['model_provider'], '"lxgate"')
        self.assertEqual(overrides['cli_auth_credentials_store'], '"file"')
        self.assertEqual(overrides['sqlite_home'], json.dumps(str(self.home.config.codex_home)))
        gate_url = overrides['chatgpt_base_url'].strip('"')[:-len('/backend-api/')]
        self.assertEqual(set(start['proxies'].values()) - {'127.0.0.1,localhost'}, {gate_url})
        self.assertNotIn('FAKE_CODEX_REPORT', [key for key in start['env'] if key not in (
            'FAKE_CODEX_MODE', 'FAKE_CODEX_REPORT', 'FAKE_CODEX_VERSION')])
        allowed_env = set(live.ENV_ALLOW) | set(live.PROXY_KEYS) | {
            'CODEX_HOME', 'CODEX_SQLITE_HOME', 'TMPDIR', 'TERM', 'NO_PROXY', 'no_proxy',
            'FAKE_CODEX_MODE', 'FAKE_CODEX_REPORT', 'FAKE_CODEX_VERSION', 'PWD', 'SHLVL', '_'}
        self.assertLessEqual(set(start['env']), allowed_env)
        self.assert_untouched()
        self.assert_no_leak(result)

    def test_one_shot_out_directory_refuses_a_second_attempt(self):
        self.make_home()
        self.assertEqual(self.run_probe(), 0)
        before = sorted(os.listdir(self.out))
        self.report.unlink()
        self.assertEqual(self.run_probe(), 2)
        self.assertIn('out-not-empty', self.stderr)
        self.assertEqual(sorted(os.listdir(self.out)), before)
        self.assertEqual(self.events(), [])  # nothing spawned

    def test_attempt_file_exists_even_when_the_app_server_dies(self):
        self.make_home(modes='crash-init')
        self.assertEqual(self.run_probe(), 3)
        self.assertTrue((self.out / live.ATTEMPT).exists())
        result = self.result()
        self.assertEqual(result['outcome'], 'initialize-closed')
        self.assertEqual(result['rpc']['methods_sent'], ['initialize'])
        self.assertEqual(self.server.count(), 0)
        self.assert_no_leak(result)

    def test_lease_held_by_a_running_codex_as_client_refuses_before_spawn(self):
        self.make_home()
        fd = os.open(self.home.lock_path(), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_SH)  # what a running codex-as client holds
        self.assertEqual(self.run_probe(), 2)
        self.assertIn('lease-held', self.stderr)
        self.assertEqual(os.listdir(self.out), [])
        self.assertEqual(self.events(), [])
        self.assert_untouched()

    def test_layout_refusals_never_repair_or_write(self):
        self.make_home()
        auth = self.home.layout.auth
        cases = {
            'replaced-link': lambda: (auth.unlink(), kit.guard.exclusive(auth, self.home.raw.encode())),
            'missing-link': lambda: auth.unlink(),
            'marker': lambda: (self.home.layout.home / 'codex-as.initialized').write_bytes(b'other\n'),
        }
        for name, damage in cases.items():
            with self.subTest(name):
                state = self.snapshot()
                damage()
                damaged = self.snapshot()
                self.assertEqual(self.run_probe(), 2)
                self.assertIn('layout-unexpected', self.stderr)
                self.assertEqual(self.snapshot(), damaged)  # no repair, no import
                self.assertEqual(os.listdir(self.out), [])
                self.assertEqual(self.events(), [])
                self.restore(state)
        self.assert_untouched()

    def snapshot(self):
        auth = self.home.layout.auth
        marker = self.home.layout.home / 'codex-as.initialized'
        return (os.readlink(auth) if auth.is_symlink() else (auth.read_bytes() if auth.exists() else None),
                self.home.secret.read_bytes(), marker.read_bytes())

    def restore(self, state):
        auth = self.home.layout.auth
        if os.path.lexists(auth):
            auth.unlink()
        os.symlink(state[0], auth)
        marker = self.home.layout.home / 'codex-as.initialized'
        marker.write_bytes(state[2])

    def test_unknown_account_and_pending_setup_refuse(self):
        self.make_home()
        self.home.alias = 'lxother'
        self.assertEqual(self.run_probe(), 2)
        self.assertIn('account-unknown', self.stderr)
        self.home.alias = kit.ALIAS
        pending = self.home.config.codex_home / 'accounts/.locks' / ('.setup-%s.json' % kit.ALIAS)
        kit.guard.exclusive(pending, json.dumps({'version': 2, 'account': kit.ALIAS}).encode())
        self.assertEqual(self.run_probe(), 2)
        self.assertIn('setup-pending', self.stderr)
        self.assertEqual(os.listdir(self.out), [])

    def test_codex_as_preflight_refusals_apply(self):
        self.make_home()
        self.assertEqual(self.run_probe(environ={'CODEX_REFRESH_TOKEN_URL_OVERRIDE': 'http://127.0.0.1:9/'}), 2)
        self.assertIn('codex-as-preflight', self.stderr)
        self.assertEqual(self.run_probe(environ={'SSL_CERT_FILE': '/nonexistent'}), 2)
        self.assertIn('tls-environment', self.stderr)
        self.assertEqual(self.events(), [])

    def test_out_directory_must_be_private_new_or_empty_and_outside_work_trees(self):
        self.make_home()
        cases = []
        relative = Path('relative-out')
        cases.append((relative, 'out-not-absolute'))
        loose = self.root / 'loose'
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        cases.append((loose, 'out-not-private'))
        tree = self.root / 'tree'
        (tree / '.git').mkdir(parents=True)
        cases.append((tree / 'out', 'out-inside-git-work-tree'))
        target = self.root / 'real'
        target.mkdir(mode=0o700)
        link = self.root / 'link'
        os.symlink(target, link)
        cases.append((link / 'out', None))  # a symlinked parent resolves; the directory itself is new
        os.symlink(target, self.root / 'link-out')
        cases.append((self.root / 'link-out', 'out-not-a-directory'))
        cases.append((self.home.config.codex_home / 'probe-out', 'out-inside-account-state-or-repository'))
        cases.append((kit.REPO / '.cache/lx/out', 'out-inside-git-work-tree'))
        for path, reason in cases:
            with self.subTest(str(path)):
                if reason is None:
                    self.assertEqual(live.prepare_out(path, self.home.config), target / 'out')
                    continue
                with self.assertRaises(live.Refused) as caught:
                    live.prepare_out(path, self.home.config)
                self.assertEqual(caught.exception.reason, reason)
        self.assertFalse((tree / 'out').exists())

    def test_failed_first_read_skips_the_second_and_keeps_no_error_text(self):
        self.make_home()
        self.server.respond = lambda bearer: (401, [('Content-Type', 'application/json')],
                                              b'{"error":{"code":"token_expired","detail":"fixture-body-secret"}}')
        self.assertEqual(self.run_probe(), 0)
        result = self.result()
        reads = result['rpc']['reads']
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]['error'], {'code': -32603, 'kind': 'fetch-failed', 'http_status': 401,
                                             'decode_error': False})
        self.assertEqual(result['rpc']['second_read'], 'skipped:first-read-did-not-succeed')
        self.assertEqual(result['gate']['forwarded'], 1)
        self.assertNotIn('fixture-body-secret', (self.out / live.RESULT).read_text())
        self.assert_no_leak(result)

    def test_single_read_option(self):
        self.make_home()
        self.assertEqual(self.run_probe(second=False), 0)
        result = self.result()
        self.assertEqual((result['rpc']['second_read'], result['gate']['forwarded']), ('skipped:single-read', 1))

    def test_model_post_unarmed_and_extra_reads_never_reach_upstream(self):
        self.make_home(modes='model-post,unsolicited,triple')
        self.assertEqual(self.run_probe(), 0)
        result = self.result()
        self.assertEqual([row['status'] for row in self.events('model_post')], [503, 503])
        self.assertEqual([row['status'] for row in self.events('unsolicited')], [503])
        self.assertEqual([row['status'] for row in self.events('usage_get_extra')], [503, 503])
        self.assertEqual(result['gate']['forwarded'], 2)
        self.assertEqual(self.server.count(), 2)
        self.assertEqual(self.server.count('/backend-api/codex/responses'), 0)
        # the extra read after the second forward meets the cap before the arm check
        self.assertEqual(result['gate']['refused_by_reason'], {'forward-cap': 1, 'model-request': 2, 'unarmed': 2})

    def test_native_refresh_between_reads_is_detected_as_a_boolean(self):
        self.make_home(modes='refresh')
        self.assertEqual(self.run_probe(), 0)
        result = self.result()
        self.assertTrue(result['credential']['changed_during_run'])
        self.assertTrue(result['credential']['link_intact_after'])
        self.assertTrue(result['credential']['still_private_regular_file'])
        self.assertEqual([item['bearer_same_as_first_forward'] for item in result['gate']['exchanges']], [True, False])
        self.assertTrue(result['gate']['exchanges'][1]['identity']['response_account_id_equals_codex_as_account'])
        self.assert_no_leak(result)

    def test_tls_verification_failure_refuses_and_never_falls_back(self):
        self.make_home()
        upstream = gates.Upstream('localhost', self.server.port, gates.production_context,
                                  resolver=kit.loopback_resolver)
        self.assertEqual(self.run_probe(upstream=upstream), 0)
        result = self.result()
        exchange = result['gate']['exchanges'][0]
        self.assertEqual((exchange['outcome'], exchange['status']), ('tls_verification_failed', None))
        self.assertEqual(result['rpc']['reads'][0]['error']['http_status'], 502)
        self.assertEqual(result['rpc']['second_read'], 'skipped:first-read-did-not-succeed')
        self.assertEqual(self.server.count(), 0)
        self.assert_no_leak(result)

    def test_connect_without_tunnel_is_refused_and_recorded(self):
        self.make_home(modes='connect')
        self.assertEqual(self.run_probe(), 0)
        self.assertEqual([row['reply'].split(' ')[1] for row in self.events('connect')], ['403', '403'])
        self.assertEqual(self.result()['gate']['connect']['refused'], {'auth.openai.com:443': 1,
                                                                       'evil.example:443': 1})

    def test_server_requests_are_refused_and_counted(self):
        self.make_home(modes='server-request')
        self.assertEqual(self.run_probe(second=False), 0)
        self.assertEqual(self.result()['rpc']['server_requests'], {'account/chatgptAuthTokens/refresh': 1})
        self.assertEqual(self.events('client_response'), [{'event': 'client_response', 'id': 'srv-1', 'error': True}])

    def test_process_group_is_reaped_with_sigkill_when_needed(self):
        self.make_home(modes='grandchild,ignore-stdin-eof,ignore-term')
        self.assertEqual(self.run_probe(second=False), 0)
        reap = self.result()['native']['reap']
        self.assertEqual(reap['signals'], ['SIGTERM', 'SIGKILL'])
        self.assertFalse(reap['graceful_exit'])
        self.assertTrue(reap['process_group_empty'])
        pid = self.events('grandchild')[0]['pid']
        time.sleep(0.2)
        self.assertFalse(Path('/proc/%d' % pid).exists() and 'Z' not in Path('/proc/%d/stat' % pid).read_text().split()[2:3])

    def test_version_other_than_the_reviewed_one_refuses(self):
        self.make_home(version='0.168.0')
        self.assertEqual(self.run_probe(), 2)
        self.assertIn('version-mismatch', self.stderr)
        self.assertEqual(os.listdir(self.out), [])

    def test_prerelease_of_the_reviewed_version_refuses(self):
        self.make_home()
        for version in ('0.167.2-alpha.1', '0.167.2-rc1'):
            with self.subTest(version):
                self.set_fake(version=version)
                self.assertEqual(self.run_probe(), 2)
                self.assert_refused_before_spawn('version-prerelease')
                self.assertFalse(self.events('start'))

    def test_binary_other_than_the_reviewed_file_refuses(self):
        self.make_home()
        self.assertEqual(self.run_probe(expect_sha256=live.EXPECTED_SHA256), 2)  # the live pin; the fake differs
        self.assert_refused_before_spawn('binary-unreviewed')

    def test_workspace_needs_a_private_runtime_directory(self):
        self.make_home()
        loose = self.root / 'loose-run'
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        link = self.root / 'link-run'
        os.symlink(self.runtime, link)
        for value in (None, '', 'relative/run', str(self.root / 'absent'), str(loose), str(link),
                      str(self.home.secret)):
            with self.subTest(value):
                self.assertEqual(self.run_probe(environ={'XDG_RUNTIME_DIR': value}), 2)
                self.assert_refused_before_spawn('xdg-runtime-dir-not-private')
                self.assertFalse(self.out.exists())  # refused before the out directory is made
        self.assert_untouched()

    def test_codex_entry_in_any_workspace_ancestor_refuses(self):
        self.make_home()
        project = self.root / 'project'
        (project / 'run').mkdir(parents=True, mode=0o700)
        link = self.root / 'via-link'
        os.symlink(project, link)
        for marker, runtime in (('dir', project / 'run'), ('file', project / 'run'), ('dir', link / 'run')):
            with self.subTest(marker=marker, runtime=str(runtime)):
                entry = project / '.codex'
                entry.mkdir() if marker == 'dir' else entry.write_text('')
                try:
                    self.assertEqual(self.run_probe(environ={'XDG_RUNTIME_DIR': str(runtime)}), 2)
                finally:
                    entry.rmdir() if marker == 'dir' else entry.unlink()
                self.assert_refused_before_spawn('workspace-ancestor-codex')
                self.assertFalse(self.out.exists())
        self.assertEqual(self.run_probe(environ={'XDG_RUNTIME_DIR': str(project / 'run')}), 0, self.stderr)

    def test_owner_auth_json_account_claim_is_checked_at_preflight(self):
        self.make_home()
        data = json.loads(self.home.raw)
        cases = (('owner-account-claim-inconsistent', qx.token(kit.OTHER_ACCOUNT, kit.USER)),
                 ('owner-account-claim-missing', qx.jwt({'sub': kit.USER, 'https://api.openai.com/auth': {
                     'chatgpt_user_id': kit.USER}})))
        for reason, id_token in cases:
            with self.subTest(reason):
                data['tokens']['id_token'] = id_token
                self.rewrite_secret(json.dumps(data))
                self.assertEqual(self.run_probe(), 2)
                self.assert_refused_before_spawn(reason)

    def test_bearer_or_header_for_another_account_is_refused_before_any_upstream_byte(self):
        self.make_home()
        for mode, reason in (('foreign-bearer', 'bearer-account-mismatch'),
                             ('foreign-header', 'account-header-mismatch')):
            with self.subTest(mode):
                self.set_fake(modes=mode)
                self.assertEqual(self.run_probe(), 0, self.stderr)
                result = self.result()
                self.assertEqual(self.server.count(), 0)
                gate = result['gate']
                self.assertEqual((gate['forwarded'], gate['refused_by_reason'], gate['exchanges']),
                                 (0, {reason: 1}, []))
                read = result['rpc']['reads'][0]
                self.assertEqual((read['state'], read['error']['http_status']), ('error', 503))
                self.assertEqual(result['rpc']['second_read'], 'skipped:first-read-did-not-succeed')
                self.assert_no_leak(result)
                self.assert_untouched()
                shutil.rmtree(self.out)
                self.report.unlink()

    @unittest.skipUnless(os.uname().sysname == 'Linux' and os.uname().machine == 'x86_64', 'guard is Linux x86_64')
    def test_full_run_under_the_network_guard(self):
        if not live.guard_self_check(live.GUARD_COMMAND):
            self.skipTest('network guard self-check failed on this host')
        self.make_home(modes='connect')
        self.assertEqual(self.run_probe(guard_command=live.GUARD_COMMAND), 0, self.stderr)
        result = self.result()
        self.assertEqual(result['gate']['forwarded'], 2)
        self.assertTrue(result['native']['guarded'])
        self.assertEqual(result['native']['guard']['root_exit'], 0)
        self.assertIsInstance(result['native']['guard']['denied_syscalls'], dict)
        self.assertTrue(result['native']['reap']['process_group_empty'])
        self.assert_no_leak(result)


class Sanitizer(unittest.TestCase):
    def test_clean_result_is_unchanged(self):
        result = {'outcome': 'completed', 'gate': {'refused_by_category': {'GET accounts-check': 1}},
                  'native': {'binary_sha256': 'a' * 64}}
        text, findings = live.sanitize(result, {'fixture-account-x'}, allowed=('a' * 64,))
        self.assertEqual((json.loads(text), findings), (result, []))

    def test_tokens_ids_and_known_values_are_redacted(self):
        bearer = qx.token('fixture-account-x', 'fixture-user-x')
        result = {'outcome': 'completed', 'a': bearer, 'b': '0f1e2d3c-4b5a-4968-8776-655443322110',
                  'c': 'fixture-account-x', 'd': 'AbCdEf0123456789AbCdEf0123456789xyz', 'e': 'b' * 64}
        text, findings = live.sanitize(result, {'fixture-account-x'}, allowed=('b' * 64,))
        self.assertEqual(findings, ['jwt-shaped', 'known-secret', 'token-shaped', 'uuid-shaped'])
        data = json.loads(text)
        self.assertEqual((data['a'], data['b'], data['c'], data['d'], data['e']),
                         ('<redacted>', '<redacted>', '<redacted>', '<redacted>', 'b' * 64))
        self.assertEqual(data['sanitizer_findings'], findings)

    def test_rpc_error_classification_keeps_no_text(self):
        error = live.rpc_error({'code': -32603, 'message': 'failed to fetch codex rate limits: Decode error for '
                                'http://127.0.0.1:1/x: unexpected status 403; body=secret'})
        self.assertEqual(error, {'code': -32603, 'kind': 'fetch-failed', 'http_status': 403, 'decode_error': True})
        native = live.rpc_error({'code': -32603, 'message': 'failed to fetch codex rate limits: GET http://127.0.0.1:1/'
                                 'backend-api/wham/usage failed: 502 Bad Gateway; content-type=application/json; body={}'})
        self.assertEqual(native['http_status'], 502)


class MainWiring(unittest.TestCase):
    def test_other_accounts_are_refused_before_any_configuration_is_read(self):
        with mock.patch.object(live.settings, 'load', side_effect=AssertionError('read')) as load, \
                mock.patch.object(live, 'run_probe', side_effect=AssertionError('ran')):
            for argv in (['--account', 'other', '--out', '/x'], ['--account', 'stanford-2', '--out', '/x'],
                         ['--account', 'stanford', '--out', '/x', '--delay', '0']):
                with self.subTest(argv), mock.patch('sys.stderr', io.StringIO()):
                    self.assertEqual(live.main(argv), 2)
            load.assert_not_called()

    def test_no_version_or_binary_override_reaches_the_live_entry_point(self):
        with mock.patch.object(live.settings, 'load', side_effect=AssertionError('read')), \
                mock.patch.object(live, 'run_probe', side_effect=AssertionError('ran')):
            for extra in (['--expect-version', '0.168.0'], ['--expect-sha256', '0' * 64]):
                with self.subTest(extra), mock.patch('sys.stderr', io.StringIO()) as err, \
                        self.assertRaises(SystemExit) as caught:
                    live.main(['--account', 'stanford', '--out', '/x', *extra])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn('unrecognized arguments', err.getvalue())
        self.assertEqual(inspect.signature(live.run_probe).parameters['expect_sha256'].default,
                         'a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b')
        self.assertEqual(live.EXPECTED_VERSION, '0.167.2')

    def test_client_name_names_no_project_user_or_host(self):
        self.assertEqual(live.CLIENT_INFO, {'name': 'usage-probe', 'version': '1'})
        self.assertEqual(kit.identifier_findings(json.dumps(live.CLIENT_INFO)), [])

    def test_main_uses_the_production_upstream_tunnel_and_guard(self):
        captured = {}

        def fake_run(alias, out, **kwargs):
            captured.update(kwargs, alias=alias, out=out)
            return 0
        config = object()
        self.assertTrue(live.trust_store_loaded())
        with mock.patch.object(live.settings, 'load', return_value=config), \
                mock.patch.object(live, 'run_probe', side_effect=fake_run), \
                mock.patch.object(live.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(live.sys, 'platform', 'linux'):
            self.assertEqual(live.main(['--account', 'stanford', '--out', '/tmp/quota-lx-live']), 0)
        upstream = captured['upstream']
        self.assertEqual((captured['alias'], upstream.host, upstream.port), ('stanford', 'chatgpt.com', 443))
        self.assertIs(upstream.context_factory, gates.production_context)
        self.assertIs(captured['tunnel'], gates.production_tunnel)
        self.assertEqual(captured['guard_command'], live.GUARD_COMMAND)
        self.assertEqual((captured['delay'], captured['second']), (30.0, True))
        self.assertNotIn('expect_sha256', captured)  # the live path keeps the reviewed pin
        self.assertIs(captured['config'], config)
        context = upstream.context_factory()
        self.assertEqual((context.verify_mode, context.check_hostname), (ssl.CERT_REQUIRED, True))

    def test_an_empty_system_trust_store_refuses_before_configuration(self):
        with mock.patch.object(live, 'trust_store_loaded', return_value=False), \
                mock.patch.object(live.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(live.sys, 'platform', 'linux'), \
                mock.patch.object(live.settings, 'load', side_effect=AssertionError('read')), \
                mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(live.main(['--account', 'stanford', '--out', '/tmp/quota-lx-live']), 2)
        self.assertIn('system-trust-store-empty', err.getvalue())

    def test_probe_overrides_route_both_endpoints_to_the_gate(self):
        pairs = live.overrides('http://127.0.0.1:1234', Path('/shared'))
        self.assertIn('chatgpt_base_url="http://127.0.0.1:1234/backend-api/"', pairs)
        self.assertIn('model_providers.lxgate.base_url="http://127.0.0.1:1234/backend-api/codex"', pairs)
        self.assertIn('model_providers.lxgate.supports_websockets=false', pairs)
        self.assertIn('model_providers.lxgate.request_max_retries=0', pairs)
        self.assertEqual([pair.split('=')[0] for pair in pairs[:3]],
                         ['cli_auth_credentials_store', 'sqlite_home', 'check_for_update_on_startup'])
        self.assertEqual(live.READ_PARAMS, {'excludeResetCreditDetails': True})


if __name__ == '__main__':
    unittest.main()
