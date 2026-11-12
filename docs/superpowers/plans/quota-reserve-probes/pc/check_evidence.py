"""Validate research artifacts, not production reserve behavior or feasibility."""
import json
from pathlib import Path
import unittest

EVIDENCE = Path(__file__).resolve().parent / 'evidence'


def read(name):
    return json.loads((EVIDENCE / (name + '.json')).read_text())


class Evidence(unittest.TestCase):
    def test_original_results(self):
        for name in ('sigint', 'keyboard', 'samebackend'):
            data = read(name)
            self.assertEqual(data['supervisor_exit'], 0)
            self.assertIn('held_response True', data['stdout'])
            self.assertTrue(data['output.bin'])
        self.assertIn('exit_after -2', read('sigint')['stdout'])
        self.assertIn('interrupted_banner True', read('keyboard')['stdout'])
        self.assertIn('new_request_after_control_loss True', read('samebackend')['stdout'])
        requests = json.loads(read('samebackend')['requests.json'])
        self.assertGreaterEqual(sum(row == ['POST', '/v1/responses'] for row in requests), 2)

    def test_control_counterexamples_and_terminal(self):
        data = read('control')
        events = {event['event']: event for event in data['events']}
        before = events['controller_disconnected']['model_requests']
        self.assertEqual(before, events['idle_after_disconnect']['model_requests'])
        self.assertEqual(events['queued_start_while_goal_paused']['model_requests'], before + 1)
        self.assertTrue(events['queued_start_while_goal_paused']['dispatched'])
        self.assertEqual(events['programmatic_prompt_after_resume']['model_requests'], before + 2)
        self.assertTrue(events['programmatic_prompt_after_resume']['dispatched'])
        goals = [event['result']['goal']['status'] for event in data['events']
                 if event.get('method') == 'thread/goal/get']
        self.assertEqual(goals, ['paused', 'paused'])
        self.assertIn('Session:', events['status_ui']['output'])
        self.assertIn('Goal paused', events['status_ui']['output'])
        self.assertTrue(events['status_ui']['ui_alive'])
        self.assertEqual(events['terminal_after_exit']['ui_exit'], 0)
        self.assertTrue(events['terminal_after_exit']['termios_restored'])
        self.assertEqual(len([row for row in data['requests'] if row['method'] == 'POST']), 4)
        self.assertIn('task title', str(data['requests']))

    def test_quota_fresh_fetch_identity_and_failure(self):
        data = read('quota')
        cases = {e['label']: e for e in data['events'] if e['event'] == 'quota_case'}
        for case in cases.values():
            self.assertEqual(case['model_requests'], 0)
            self.assertEqual(case['requests_added'], 1)
        first = cases['first']['result']
        self.assertEqual(first, cases['unchanged']['result'])
        self.assertEqual(first['accountId'], 'fixture-account-a')
        self.assertEqual(first['rateLimits']['primary']['windowDurationMins'], 300)
        self.assertEqual(first['rateLimits']['secondary']['windowDurationMins'], 10080)
        self.assertEqual(first['rateLimits']['primary']['usedPercent'], 25)
        self.assertEqual(first['rateLimits']['secondary']['usedPercent'], 60)
        self.assertEqual(cases['wrong_account']['result']['accountId'], 'fixture-account-b')
        self.assertIn('probe_error', cases['fractional']['result'])
        self.assertIn('probe_error', cases['failed']['result'])
        self.assertNotIn('POST', [request['method'] for request in data['requests']])

    def test_schema_and_sanitization(self):
        schema = read('schema')
        self.assertEqual(schema['method_count'], len(schema['methods']))
        self.assertIn('turn/interrupt', schema['methods'])
        for file in EVIDENCE.glob('*.json'):
            text = file.read_text()
            json.loads(text)
            self.assertTrue(text.isascii(), file.name)
            self.assertNotIn('/home/', text, file.name)
            self.assertNotIn('/tmp/pc-native-', text, file.name)

    def test_all_supervised_runs_completed(self):
        manifest = read('manifest')
        self.assertEqual(manifest['version'], 'codex-cli 0.167.2')
        self.assertEqual(manifest['binary_sha256'], 'a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b')
        self.assertEqual({r['label'] for r in manifest['runs']},
                         {'guard-self-check', 'version', 'preserved-reproductions', 'quota', 'control'})
        self.assertTrue(all(r['exit'] == 0 for r in manifest['runs']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
