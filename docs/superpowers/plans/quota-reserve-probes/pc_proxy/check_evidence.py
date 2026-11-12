"""Check recorded native observations; these do not certify full option A."""
import json
from pathlib import Path
import unittest

OUT = Path(__file__).resolve().parent / 'evidence'


def events(mode, kind):
    return [e for e in json.loads((OUT / (mode + '.json')).read_text())['events']
            if e['event'] == kind]


class Evidence(unittest.TestCase):
    def test_runs_are_bounded_and_successful(self):
        runs = json.loads((OUT / 'manifest.json').read_text())['runs']
        self.assertEqual([r['label'] for r in runs], ['guard', 'routing', 'websocket', 'refresh', 'ui', 'loss'])
        self.assertTrue(all(r['exit'] == 0 for r in runs))

    def test_subscription_and_model_free_quota(self):
        for mode in ('routing', 'websocket', 'refresh', 'ui', 'loss'):
            self.assertEqual(events(mode, 'preflight')[0]['model_requests'], 0)
        routed = events('routing', 'admit')
        self.assertEqual(len(routed), 1)
        self.assertTrue(routed[0]['bearer_matches_fixture'])
        self.assertEqual(routed[0]['account'], 'fixture-account-a')

    def test_websocket_fallback_and_native_refresh(self):
        self.assertGreaterEqual(len([e for e in events('websocket', 'get') if e['websocket']]), 1)
        self.assertTrue(events('websocket', 'admit'))
        self.assertIn('auth.openai.com:443', [e['destination'] for e in events('refresh', 'connect_rejected')])

    def test_closed_queue_and_provider_check(self):
        self.assertEqual(events('routing', 'queue_blocked')[0]['admitted'], 1)
        self.assertFalse(events('routing', 'new_thread_provider_bypass')[0]['observed'])
        errors = events('routing', 'rpc_error_wire')
        self.assertTrue(any(e['message']['error'].get('data', {}).get('type') == 'modelProviderMismatch'
                            for e in errors))

    def test_listener_loss_and_ui_limitation(self):
        self.assertIn(events('loss', 'queue_after_listener_loss')[0]['status'], ('failed', 'inProgress'))
        self.assertEqual(events('loss', 'quota_after_listener_loss')[0]['admitted'], 1)
        ui = events('ui', 'subscription_ui_bootstrap')[0]
        self.assertTrue(ui['exited'] and ui['discovery_error'])
        self.assertEqual(ui['model_requests'], 0)
        self.assertNotEqual(ui['exit'], 0)


if __name__ == '__main__':
    unittest.main()
