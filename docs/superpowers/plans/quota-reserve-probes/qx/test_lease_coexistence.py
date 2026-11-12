"""The QX binding beside codex-as's real account layout, leases and post-run repair (offline).

Uses lib/codex-as/{check,layout}.py unchanged in a temporary directory with
fabricated credentials. Native file writes are imitated the way the tested
releases write through the account link (atomic replace since 0.167.0).
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]


def load(name, path):
    spec = importlib.util.spec_from_file_location('qx_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


binding = load('binding', HERE / 'binding.py')
fixtures = load('fixtures', HERE / 'fixtures.py')
guard = load('check', REPO / 'lib/codex-as/check.py')
storage = load('layout', REPO / 'lib/codex-as/layout.py')
A, B, UA, UB = 'fixture-account-a', 'fixture-account-b', 'fixture-user-a', 'fixture-user-b'


class Coexistence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='quota-qx-lease-')
        root = Path(self.temp.name)
        self.config = SimpleNamespace(codex_home=root / 'codex', secrets_dir=root / 'secrets')
        self.config.secrets_dir.mkdir(mode=0o700)
        self.t1, self.t2, self.tb = (fixtures.token(A, UA), fixtures.token(A, UA), fixtures.token(B, UB))
        self.secret = self.config.secrets_dir / 'codex-work.json'
        guard.exclusive(self.secret, fixtures.auth_json(self.t1, 'r1', A, UA).encode())
        self.layout = storage.Layout(guard, 'work', self.config)
        self.layout.root_check()
        self.assertTrue(self.layout.acquire())
        self.layout.prepare()
        fcntl.flock(self.layout.fd, fcntl.LOCK_SH)
        self.baseline = self.layout.validate()
        # The lease's expected identity comes from the claims codex-as already reads, not the bearer.
        self.user, self.account = storage.owner(self.baseline)
        self.lease = binding.Lease(expected_account=self.account, expected_user=self.user,
                                   policy=binding.Policy(20, 30, 120), clock=time.monotonic, wall=time.time)

    def tearDown(self):
        self.layout.close()
        self.temp.cleanup()

    def native_refresh(self, access, account=A, user=UA):
        """Native 0.167+: private temporary file beside the resolved target, renamed over it."""
        staged = self.secret.parent / '.auth-fixture'
        guard.exclusive(staged, fixtures.auth_json(access, 'r2', account, user).encode())
        os.replace(staged, self.secret)

    def usage(self, bearer, account=A, user=UA):
        return self.lease.observe_usage(bearer, account, 200, json.dumps(fixtures.usage_body(account, user)).encode())

    def post_run(self):
        """The decisions launch.post_run makes once the last client exits (without printing)."""
        self.layout.close()
        unlinked = not self.layout.auth.is_symlink()
        self.assertTrue(self.layout.acquire())
        self.layout.repair(self.baseline)
        current = self.layout.validate()
        return unlinked, storage.same_identity(current, self.baseline)

    def test_expected_identity_matches_what_native_sends(self):
        self.assertEqual((self.user, self.account), (UA, A))
        data = json.loads(self.baseline)
        self.assertEqual(data['tokens']['account_id'], self.account)  # native ChatGPT-Account-ID source

    def test_same_account_refresh_rebinds_and_repair_stays_quiet(self):
        self.usage(self.t1)
        self.assertEqual(self.lease.admit(self.t1, A, 'gpt-5.4'), (True, 'admitted'))
        self.native_refresh(self.t2)
        self.assertTrue(self.layout.auth.is_symlink())
        guard.credential(self.secret)  # still an owned 0600 regular file with one link
        self.assertEqual(self.lease.admit(self.t2, A, 'gpt-5.4'), (False, 'rebind-required'))
        self.usage(self.t2)
        self.assertEqual(self.lease.admit(self.t2, A, 'gpt-5.4'), (True, 'admitted'))
        self.assertEqual(self.post_run(), (False, True))  # link kept, same ChatGPT user: no warning
        self.assertEqual(json.loads(self.secret.read_bytes())['tokens']['access_token'], self.t2)

    def test_in_session_login_as_b_pauses_and_repair_preserves_both(self):
        self.usage(self.t1)
        # Native logout removes the link; a new login writes a regular file in the account home.
        self.layout.auth.unlink()
        guard.exclusive(self.layout.auth, fixtures.auth_json(self.tb, 'rb', B, UB).encode())
        self.assertEqual(self.lease.admit(self.tb, B, 'gpt-5.4'), (False, 'identity-changed'))
        self.assertEqual(self.lease.paused, 'identity-changed')
        with self.assertRaisesRegex(ValueError, 'diverged'):
            self.post_run()
        self.assertEqual(json.loads(self.secret.read_bytes())['tokens']['access_token'], self.t1)

    def test_second_launch_shares_the_account_lease_with_its_own_binding(self):
        second = storage.Layout(guard, 'work', self.config)
        try:
            self.assertFalse(second.acquire())  # shared: another client of this account runs
            other = binding.Lease(expected_account=A, expected_user=UA, policy=binding.Policy(10, None, 120),
                                  clock=time.monotonic, wall=time.time)
            self.usage(self.t1)
            other.observe_usage(self.t1, A, 200, json.dumps(fixtures.usage_body(A, UA, five=85)).encode())
            self.assertEqual(other.admit(self.t1, A, 'gpt-5.4'), (True, 'admitted'))
            self.lease.observe_usage(self.t1, A, 200, json.dumps(fixtures.usage_body(A, UA, five=85)).encode())
            self.assertEqual(self.lease.admit(self.t1, A, 'gpt-5.4'), (False, 'paused:threshold:five_hour'))
        finally:
            second.close()


if __name__ == '__main__':
    unittest.main()
