"""Offline rotation and freshness sequences for the QX binding model (no native binary)."""
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('qx_binding', HERE / 'binding.py')
binding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(binding)

WALL = 2_000_000_000
A, B, UA, UB = 'acct-a', 'acct-b', 'user-a', 'user-b'
# Fabricated opaque bearer values; never real credentials.
T1, T2, TB = 'fixture-bearer-a1', 'fixture-bearer-a2', 'fixture-bearer-b1'


def window(used, seconds):
    return {'used_percent': used, 'limit_window_seconds': seconds,
            'reset_after_seconds': seconds // 2, 'reset_at': WALL + seconds // 2}


def usage(account=A, user=UA, plan='pro', five=25, weekly=60, swap=False, extra=None, **rate):
    slots = [window(five, 18000) if five is not None else None,
             window(weekly, 604800) if weekly is not None else None]
    if swap:
        slots.reverse()
    body = {'account_id': account, 'user_id': user, 'plan_type': plan,
            'rate_limit': {'allowed': True, 'limit_reached': False,
                           'primary_window': slots[0], 'secondary_window': slots[1], **rate}}
    if extra is not None:
        body['additional_rate_limits'] = extra
    return json.dumps(body).encode()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def lease(clock, five=20, weekly=30, max_age=120, user=UA, closes=None):
    return binding.Lease(expected_account=A, expected_user=user,
                         policy=binding.Policy(five, weekly, max_age), clock=clock,
                         wall=lambda: WALL, on_close=(closes.append if closes is not None else None))


class Normalization(unittest.TestCase):
    def normal(self, body, request=A):
        return binding.normalize_usage(body, request_account=request, expected_account=A,
                                       expected_user=UA, wall_now=WALL)

    def reason(self, body, request=A):
        with self.assertRaises(binding.Unavailable) as caught:
            self.normal(body, request)
        return caught.exception.reason

    def test_windows_follow_duration_not_slot(self):
        for swap in (False, True):
            observed = self.normal(usage(five=25, weekly=60, swap=swap))
            self.assertEqual(observed.windows['five_hour'].used_percent, 25.0)
            self.assertEqual(observed.windows['weekly'].used_percent, 60.0)
            self.assertEqual(observed.windows['five_hour'].minutes, 300)

    def test_missing_and_unrecognized_windows_are_not_invented(self):
        self.assertEqual(set(self.normal(usage(weekly=None)).windows), {'five_hour'})
        body = json.loads(usage())
        body['rate_limit']['secondary_window'] = window(10, 3600)  # an hourly window maps to nothing
        self.assertEqual(set(self.normal(json.dumps(body).encode()).windows), {'five_hour'})
        body['rate_limit']['secondary_window'] = window(10, 18000)
        self.assertEqual(self.reason(json.dumps(body).encode()), 'ambiguous-window')

    def test_invalid_values_are_unavailable(self):
        cases = {'out-of-range': [usage(five=150), usage(five=-1)],
                 'malformed': [b'not json', b'{"plan_type": "pro", "x": NaN}', b'[]',
                               usage(five=True), json.dumps({'account_id': A}).encode()],
                 'no-codex-family': [json.dumps({'account_id': A, 'user_id': UA, 'plan_type': 'pro',
                                                 'rate_limit': None}).encode()]}
        for reason, bodies in cases.items():
            for body in bodies:
                self.assertEqual(self.reason(body), reason, body)
        # The gate sees raw JSON; it keeps a fractional value unrounded (native 0.167.2 rejects it).
        self.assertEqual(self.normal(usage(five=25.5)).windows['five_hour'].used_percent, 25.5)

    def test_reset_times_must_describe_a_current_window(self):
        body = json.loads(usage())
        body['rate_limit']['primary_window']['reset_at'] = WALL - 3600
        self.assertEqual(self.reason(json.dumps(body).encode()), 'reset-anomaly')
        body['rate_limit']['primary_window']['reset_at'] = WALL + 18000 * 3
        self.assertEqual(self.reason(json.dumps(body).encode()), 'reset-anomaly')

    def test_identity_and_plan(self):
        self.assertEqual(self.reason(usage(account=B)), 'account-mismatch')
        self.assertEqual(self.reason(usage(), request=B), 'account-mismatch')
        self.assertEqual(self.reason(usage(user=UB)), 'user-mismatch')
        for plan in ('free', 'unknown', 'self_serve_business_usage_based', 'guest'):
            self.assertEqual(self.reason(usage(plan=plan)), 'unsupported-plan')
        rows = [{'limit_name': 'gpt-fixture-spark', 'metered_feature': 'codex_bengalfox',
                 'normal_model_slug': 'gpt-fixture-spark-normal',
                 'rate_limit': {'allowed': True, 'limit_reached': False}}]
        # Every name a non-codex row carries counts, so an unproved mapping fails closed.
        self.assertEqual(self.normal(usage(extra=rows)).other_families,
                         ('codex_bengalfox', 'gpt-fixture-spark', 'gpt-fixture-spark-normal'))

    def test_response_headers(self):
        headers = {'X-Codex-Primary-Used-Percent': '12.5', 'x-codex-primary-window-minutes': '300',
                   'x-codex-primary-reset-at': str(WALL + 100),
                   'x-codex-bengalfox-primary-used-percent': '99', 'x-codex-bengalfox-primary-window-minutes': '300'}
        windows = binding.normalize_headers(headers, WALL)
        self.assertEqual(list(windows), ['five_hour'])
        self.assertEqual(windows['five_hour'].used_percent, 12.5)
        with self.assertRaises(binding.Unavailable):
            binding.normalize_headers({'x-codex-primary-used-percent': 'x'}, WALL)


class Sequences(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.closed = []
        self.lease = lease(self.clock, closes=self.closed)

    def step(self, seconds):
        self.clock.now += seconds

    def bind(self, bearer=T1, **body):
        return self.lease.observe_usage(bearer, A, 200, usage(**body))

    def test_startup_needs_fresh_same_bearer_read(self):
        self.assertFalse(self.lease.ready())
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'rebind-required'))
        self.assertEqual(self.bind(), 'fresh')
        self.assertTrue(self.lease.ready())
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))

    def test_native_refresh_same_account_rebinds_without_pause(self):
        self.bind()
        epoch = self.lease.epoch
        self.step(10)
        self.assertEqual(self.lease.admit(T2, A, 'gpt-5.4'), (False, 'rebind-required'))
        self.assertGreater(self.lease.epoch, epoch)
        self.assertIsNone(self.lease.paused)
        self.assertEqual(self.closed, [])  # same-account rotation does not close admitted streams
        self.step(1)
        self.assertEqual(self.bind(T2), 'fresh')
        self.assertEqual(self.lease.admit(T2, A, 'gpt-5.4'), (True, 'admitted'))
        # A late request with the superseded bearer cannot ride on the new observation.
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'rebind-required'))

    def test_account_change_with_stale_a_data_pauses_and_needs_continuation(self):
        self.bind()
        self.step(5)
        self.assertEqual(self.lease.admit(TB, B, 'gpt-5.4'), (False, 'identity-changed'))
        self.assertEqual(self.lease.paused, 'identity-changed')
        self.assertEqual(self.closed, ['identity-changed'])
        self.assertEqual(self.lease.observe_usage(TB, B, 200, usage(account=B, user=UB)), 'identity-changed')
        # Back to A with the very same bearer: the stale A observation is gone with its epoch.
        self.step(5)
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'paused:identity-changed'))
        self.assertIsNone(self.lease.observation)
        self.assertEqual(self.bind(), 'fresh')  # fresh but no deliberate continuation yet
        self.assertEqual(self.lease.paused, 'identity-changed')
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.step(1)
        self.assertEqual(self.bind(), 'fresh')
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))

    def test_server_reported_other_account_or_user_is_an_identity_change(self):
        self.bind()
        self.step(1)
        self.assertEqual(self.lease.observe_usage(T1, A, 200, usage(account=B)), 'account-mismatch')
        self.assertEqual(self.lease.paused, 'identity-changed')
        other = lease(Clock())
        other.observe_usage(T1, A, 200, usage())
        self.assertEqual(other.observe_usage(T1, A, 200, usage(user=UB)), 'user-mismatch')
        self.assertEqual(other.paused, 'identity-changed')

    def test_a_b_a_continuation_while_native_still_on_b_stays_paused(self):
        self.bind()
        self.lease.admit(TB, B, 'gpt-5.4')
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.step(1)
        self.assertEqual(self.lease.observe_usage(TB, B, 200, usage(account=B, user=UB)), 'identity-changed')
        self.assertIsNone(self.lease.continuation)  # the identity change used it up
        self.assertEqual(self.lease.admit(TB, B, 'gpt-5.4'), (False, 'identity-changed'))
        self.assertEqual(self.lease.paused, 'identity-changed')
        # Native returns to A an hour later; its passing poll must not resume on the old request.
        self.step(3600)
        self.assertEqual(self.bind(), 'fresh')
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'paused:awaiting-continuation'))

    def test_failed_continuation_check_is_used_up(self):
        self.bind(five=85)
        self.assertEqual(self.lease.paused, 'threshold:five_hour')
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.step(1)
        self.assertEqual(self.lease.observe_usage(T1, A, 503, b''), 'failure')
        self.assertIsNone(self.lease.continuation)
        self.step(1)
        self.assertEqual(self.lease.observe_usage(T1, A, 200, b'not json'), 'malformed')
        self.step(600)  # a background poll ten minutes later passes
        self.assertEqual(self.bind(), 'fresh')
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'paused:awaiting-continuation'))
        self.assertEqual(self.lease.paused, 'threshold:five_hour')

    def test_stale_then_failed_checks_then_success_stays_paused(self):
        self.bind()
        self.step(121)
        self.assertEqual(self.lease.tick(), 'stale')
        self.assertTrue(self.lease.request_continuation(trusted=True))
        for _ in range(5):
            self.step(1)
            self.assertEqual(self.lease.observe_usage(T1, A, 503, b''), 'failure')
        self.step(1)
        self.assertEqual(self.bind(), 'fresh')
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'paused:awaiting-continuation'))
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.step(1)
        self.assertEqual(self.bind(), 'fresh')
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))

    def test_continuation_is_answered_only_by_a_read_received_after_it(self):
        self.bind(five=85)
        self.step(10)
        sent_before = self.clock.now
        self.step(1)
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.step(1)
        # A poll the gate received before the request but answered after it.
        self.assertEqual(self.lease.observe_usage(T1, A, 200, usage(), started=sent_before), 'fresh')
        self.assertEqual(self.lease.paused, 'threshold:five_hour')
        self.assertIsNotNone(self.lease.continuation)  # not the check it asked for, not used up
        self.step(1)
        self.assertEqual(self.bind(), 'fresh')
        self.assertIsNone(self.lease.paused)

    def test_continuation_expires_without_a_prompt_check(self):
        self.bind(five=85)
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.step(31)  # beyond the 30 s window
        self.assertEqual(self.bind(), 'fresh')
        self.assertEqual(self.lease.paused, 'threshold:five_hour')
        self.assertIsNone(self.lease.continuation)

    def test_same_account_rotation_clears_the_continuation(self):
        self.bind(five=85)
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.step(1)
        self.assertEqual(self.bind(T2), 'fresh')
        self.assertEqual(self.lease.paused, 'threshold:five_hour')

    def test_blocking_reason_follows_the_current_condition(self):
        self.bind(five=85)
        self.assertEqual((self.lease.paused, self.lease.blocking), ('threshold:five_hour', 'threshold:five_hour'))
        self.step(1)
        self.assertEqual(self.lease.admit(TB, B, 'gpt-5.4'), (False, 'identity-changed'))
        self.assertEqual((self.lease.paused, self.lease.blocking), ('threshold:five_hour', 'identity-changed'))
        self.step(1)
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'paused:identity-changed'))
        self.step(1)
        self.bind()  # back on A, data passes: only a continuation is missing
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'paused:awaiting-continuation'))
        self.step(200)
        self.assertEqual(self.lease.tick(), 'threshold:five_hour')
        self.assertEqual(self.lease.blocking, 'stale')

    def test_late_answer_to_an_older_read_is_superseded(self):
        self.step(10)
        older = self.clock.now
        self.step(5)
        self.assertEqual(self.bind(five=85), 'fresh')
        self.assertEqual(self.lease.observe_usage(T1, A, 200, usage(), started=older), 'superseded')
        self.assertEqual(self.lease.observation.windows['five_hour'].used_percent, 85.0)
        self.assertEqual(self.lease.blocking, 'threshold:five_hour')

    def test_two_sessions_share_an_account_with_their_own_leases(self):
        first = lease(self.clock, five=20)
        second = lease(self.clock, five=10)
        body = usage(five=85)  # 15% remaining
        first.observe_usage(T1, A, 200, body)
        second.observe_usage(T1, A, 200, body)
        self.assertEqual(first.admit(T1, A, 'gpt-5.4'), (False, 'paused:threshold:five_hour'))
        self.assertEqual(second.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))
        # The first session's native refresh rewrites the shared secret; the second keeps its
        # cached bearer until native adopts the new one, then must rebind on its own lease.
        self.step(5)
        self.assertEqual(second.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))
        self.assertEqual(second.admit(T2, A, 'gpt-5.4'), (False, 'rebind-required'))
        second.observe_usage(T2, A, 200, body)
        self.assertEqual(second.admit(T2, A, 'gpt-5.4'), (True, 'admitted'))
        self.assertEqual(first.paused, 'threshold:five_hour')

    def test_unchanged_but_fresh_data_extends_age(self):
        self.bind()
        self.step(100)
        self.bind()
        self.step(100)  # 200 s after the first read, 100 s after the identical second one
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))

    def test_repeated_failures_do_not_extend_age(self):
        self.bind()
        for status, body in ((503, b''), (200, b'not json'), (401, b''), (200, usage(five=150))):
            self.step(25)
            self.assertNotEqual(self.lease.observe_usage(T1, A, status, body), 'fresh')
        self.step(25)  # 125 s since the last fresh read
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'paused:stale'))
        self.assertEqual(self.closed, ['stale'])

    def test_rotation_does_not_restart_the_recovery_deadline(self):
        self.bind()
        self.step(100)
        self.lease.admit(T2, A, 'gpt-5.4')
        self.lease.observe_usage(T2, A, 503, b'')
        self.step(21)
        self.assertEqual(self.lease.tick(), 'stale')

    def test_unsupported_family_and_plan(self):
        rows = [{'limit_name': 'gpt-fixture-spark', 'metered_feature': 'codex_bengalfox',
                 'rate_limit': {'allowed': True, 'limit_reached': False}}]
        self.bind(extra=rows)
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))
        self.assertEqual(self.lease.admit(T1, A, 'gpt-fixture-spark'), (False, 'paused:unsupported-family'))
        slug = lease(Clock())
        slug.observe_usage(T1, A, 200, usage(extra=[{'limit_name': 'fixture-family', 'normal_model_slug': 'gpt-x',
                                                     'metered_feature': 'codex_fixture', 'rate_limit': None}]))
        self.assertEqual(slug.admit(T1, A, 'gpt-x'), (False, 'paused:unsupported-family'))
        reviewed = binding.Lease(expected_account=A, expected_user=UA, policy=binding.Policy(20, 30, 120),
                                 clock=Clock(), wall=lambda: WALL, models={'gpt-5.4'})
        reviewed.observe_usage(T1, A, 200, usage())
        self.assertEqual(reviewed.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))
        self.assertEqual(reviewed.admit(T1, A, 'gpt-unreviewed'), (False, 'paused:unsupported-family'))
        other = lease(Clock())
        self.assertEqual(other.observe_usage(T1, A, 200, usage(plan='free')), 'unsupported-plan')
        self.assertEqual(other.paused, 'unsupported-plan')
        self.assertFalse(other.ready())

    def test_expired_freshness_pauses_and_never_auto_resumes(self):
        self.bind()
        self.step(121)
        self.assertEqual(self.lease.tick(), 'stale')
        self.assertEqual(self.bind(), 'fresh')
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (False, 'paused:awaiting-continuation'))
        self.assertFalse(self.lease.request_continuation(trusted=False))
        self.assertTrue(self.lease.request_continuation(trusted=True))
        self.assertEqual(self.bind(), 'fresh')  # same instant as the request: not newer
        self.assertEqual(self.lease.paused, 'stale')
        self.step(1)
        self.bind()
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))

    def test_threshold_boundaries(self):
        cases = [((20, None), {'five': 80}, False), ((20, None), {'five': 79.9}, True),
                 ((0, None), {'five': 100}, False), ((0, None), {'five': 99}, True),
                 ((100, None), {'five': 0}, False), ((None, None), {'five': 100, 'weekly': 100}, True),
                 ((None, 30), {'five': 100, 'weekly': 70}, False), ((None, 30), {'five': None}, True),
                 ((20, 30), {'weekly': None}, False)]
        for (five, weekly), body, admitted in cases:
            subject = lease(Clock(), five=five, weekly=weekly)
            subject.observe_usage(T1, A, 200, usage(**body))
            self.assertEqual(subject.admit(T1, A, 'gpt-5.4')[0], admitted, (five, weekly, body))

    def test_limit_reached_flag_pauses(self):
        self.bind(limit_reached=True)
        self.assertEqual(self.lease.paused, 'limit-reached')

    def test_independent_reset_while_paused_only_allows_continuation(self):
        self.bind(five=85, weekly=75)  # five-hour at reserve breach, weekly also below 30
        self.assertEqual(self.lease.paused, 'threshold:five_hour')
        self.step(1)
        self.bind(five=0, weekly=75)  # five-hour reset; weekly still violated
        self.assertEqual((self.lease.paused, self.lease.blocking), ('threshold:five_hour', 'threshold:weekly'))
        self.lease.request_continuation(trusted=True)
        self.step(1)
        self.bind(five=0, weekly=75)
        self.assertEqual(self.lease.blocking, 'threshold:weekly')
        self.assertIsNone(self.lease.continuation)  # consumed; ask again
        self.step(1)
        self.bind(five=0, weekly=10)
        self.assertEqual((self.lease.paused, self.lease.blocking), ('threshold:five_hour', None))  # reset alone never resumes
        self.lease.request_continuation(trusted=True)
        self.step(1)
        self.bind(five=0, weekly=10)
        self.assertIsNone(self.lease.paused)

    def test_response_headers_renew_only_the_bound_credential(self):
        self.bind()
        headers = {'x-codex-primary-used-percent': '30.5', 'x-codex-primary-window-minutes': '300',
                   'x-codex-primary-reset-at': str(WALL + 100), 'x-codex-secondary-used-percent': '61',
                   'x-codex-secondary-window-minutes': '10080', 'x-codex-secondary-reset-at': str(WALL + 1000)}
        self.step(100)
        self.assertEqual(self.lease.observe_headers(T2, A, headers), 'ignored')
        self.assertEqual(self.lease.observe_headers(T1, A, headers), 'fresh')
        self.step(100)
        self.assertEqual(self.lease.admit(T1, A, 'gpt-5.4'), (True, 'admitted'))
        headers['x-codex-primary-used-percent'] = '80.5'
        self.lease.observe_headers(T1, A, headers)
        self.assertEqual(self.lease.paused, 'threshold:five_hour')

    def test_no_bearer_value_is_retained(self):
        self.bind()
        self.lease.admit(T2, A, 'gpt-5.4')
        retained = repr(vars(self.lease)) + json.dumps(self.lease.log, default=str)
        for secret in (T1, T2):
            self.assertNotIn(secret, retained)


if __name__ == '__main__':
    unittest.main()
