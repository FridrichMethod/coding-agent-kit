"""Reject route/capture mutations without running native code."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('check_reload', HERE / 'check_reload.py')
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class ReloadEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((HERE / 'reload-evidence.json').read_text())

    def test_capture(self):
        CHECKER.check(self.data)

    def test_reject_incomplete_guard(self):
        data = copy.deepcopy(self.data)
        data['stages'][0]['exit'] = 1
        with self.assertRaises(AssertionError):
            CHECKER.check(data)

    def test_reject_wrong_route_and_missing_events(self):
        for mode in ('environment', 'pinned'):
            for mutation in ('route', 'remove', 'time', 'auth'):
                with self.subTest(mode=mode, mutation=mutation):
                    data = copy.deepcopy(self.data)
                    rows = data['cases'][mode]
                    second = next(row for row in rows if row.get('marker') == 'second')
                    if mutation == 'route':
                        second['route'] = 'alternate' if mode == 'pinned' else 'gate'
                    elif mutation == 'remove':
                        data['cases'][mode] = [row for row in rows if row.get('marker') != 'second']
                    elif mutation == 'time':
                        second['at'] = 0
                    else:
                        second['fixture_bearer'] = False
                    with self.assertRaises(AssertionError):
                        CHECKER.check(data)


if __name__ == '__main__':
    unittest.main()
