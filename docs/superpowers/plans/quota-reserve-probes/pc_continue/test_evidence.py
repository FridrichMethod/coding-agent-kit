"""Negative checks prevent fixture logs from being mistaken for full capability."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pc5_evidence', HERE / 'check_evidence.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class Evidence(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((HERE / 'evidence/delegation.json').read_text())

    def row(self, event):
        return next(e for e in self.data['events'] if e['event'] == event)

    def test_native_captures(self):
        for mode in checker.MODES:
            checker.validate(json.loads((HERE / ('evidence/' + mode + '.json')).read_text()))

    def test_reject_recovery_grant(self):
        self.row('quota_recovery_no_grant')['grants'] = 2
        with self.assertRaises(AssertionError):
            checker.validate(self.data)

    def test_reject_forwarding_while_paused(self):
        forged = copy.deepcopy(self.row('gate_forward'))
        forged['at'] = self.row('gate_pause')['at'] + .001
        self.data['events'].append(forged)
        with self.assertRaises(AssertionError):
            checker.validate(self.data)

    def test_reject_missing_queue_evidence(self):
        self.data['events'] = [e for e in self.data['events'] if e['event'] != 'gate_reject']
        with self.assertRaises(AssertionError):
            checker.validate(self.data)

    def test_reject_stale_quota_pass(self):
        next(e for e in self.data['events'] if e['event'] == 'fresh_check' and e['label'] == 'stale')['allowed'] = True
        with self.assertRaises(AssertionError):
            checker.validate(self.data)

    def test_reject_surviving_stream(self):
        self.data['events'].remove(self.row('upstream_disconnected'))
        with self.assertRaises(AssertionError):
            checker.validate(self.data)

    def test_reject_fake_genealogy(self):
        self.row('native_delegation')['child'] = self.row('native_delegation')['root']
        with self.assertRaises(AssertionError):
            checker.validate(self.data)

    def test_reject_tool_marker_history_disagreement(self):
        data = json.loads((HERE / 'evidence/tool_interrupt.json').read_text())
        event = next(e for e in data['events'] if e['event'] == 'local_tool_after_gate_pause')
        event['finished'] = not event['finished']
        with self.assertRaises(AssertionError):
            checker.validate(data)

    def test_reject_unusable_ui(self):
        self.row('native_status_ui')['output'] = ''
        with self.assertRaises(AssertionError):
            checker.validate(self.data)


if __name__ == '__main__':
    unittest.main()
