"""Mutation tests for research acceptance, using sanitized captures only."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('acceptance', HERE / 'check_evidence.py')
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class Evidence(unittest.TestCase):
    def case(self, mode):
        return json.loads((HERE / 'evidence' / ('case-' + mode + '.json')).read_text())

    def reject(self, mode, mutate):
        data = copy.deepcopy(self.case(mode))
        mutate(data)
        with self.assertRaises((AssertionError, StopIteration, IndexError)):
            acceptance.check(data)

    def test_valid(self):
        for mode in acceptance.MODES:
            acceptance.check(self.case(mode))

    def test_no_tui(self):
        self.reject('eof', lambda d: d.update(native_tui=False))

    def test_missing_closure(self):
        self.reject('eof', lambda d: d.update(events=[e for e in d['events'] if e['event'] != 'stream_disconnected']))

    def test_missing_genealogy(self):
        self.reject('eof', lambda d: [e.update(tool_results=[]) for e in d['events'] if e['event'] == 'model_request'])

    def test_unrelated_closed(self):
        self.reject('eof', lambda d: [e.update(unrelated_stream_active=False) for e in d['events'] if e['event'] == 'loss_observed'])

    def test_old_queue_not_checked(self):
        self.reject('eof', lambda d: [e.update(old_queue=False) for e in d['events'] if e['event'] == 'gate_reject'])

    def test_reopened_after_loss(self):
        self.reject('eof', lambda d: d['events'].append({'event': 'gate_forward', 'at': 999, 'request_id': 99}))

    def test_false_freshness(self):
        self.reject('continuation', lambda d: [e.update(passed=True) for e in d['events'] if e['event'] == 'fake_quota_check'])

    def test_output_hidden(self):
        self.reject('continuation', lambda d: [e.update(notice_visible=False) for e in d['events'] if e['event'] == 'outer_terminal'])

    def test_false_human_claim(self):
        self.reject('continuation', lambda d: [e.update(physical_human_verified=True) for e in d['events'] if e['event'] == 'manual_continuation'])


if __name__ == '__main__':
    unittest.main()
