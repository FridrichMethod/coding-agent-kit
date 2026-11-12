"""Small mutation checks for actual native local-tool evidence."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent


class Evidence(unittest.TestCase):
    def test_string_cancellation_result(self):
        spec = importlib.util.spec_from_file_location('probe', HERE / 'probe.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        result = [{'content': {'is_error': True}, 'toolUseResult': 'User rejected tool use'}]
        classify = getattr(probe, 'classify', lambda *args: {})
        self.assertEqual(classify(None, False, result), {
            'completed': False, 'ended_without_completion': True, 'terminated': False,
            'cleanup_before_observation': False})

    def test_captures_exist(self):
        for mode in ('gate', 'interrupt'):
            self.assertTrue((HERE / 'evidence' / (mode + '.json')).is_file(),
                            'actual native capture required: ' + mode)

    def test_acceptance_and_mutations(self):
        if not (HERE / 'evidence/gate.json').exists():
            self.skipTest('capture absence tested separately')
        spec = importlib.util.spec_from_file_location('acceptance', HERE / 'check_evidence.py')
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        for mode in ('gate', 'interrupt'):
            original = json.loads((HERE / 'evidence' / (mode + '.json')).read_text())
            checker.check(original)
            mutations = {
                'started': lambda d: d.update(events=[e for e in d['events'] if e['event'] != 'tool_started']),
                'early_pause': lambda d: next(e for e in d['events'] if e['event'] == 'pause_requested').update(at=0),
                'false_completed': lambda d: d['outcome'].update(completed=not d['outcome']['completed']),
                'false_terminated': lambda d: d['outcome'].update(terminated=not d['outcome']['terminated']),
                'false_ended': lambda d: d['outcome'].update(ended_without_completion=not d['outcome']['ended_without_completion']),
                'missing_result': lambda d: next(e for e in d['events'] if e['event'] == 'tool_history').update(results=[]),
                'later_forward': lambda d: d['events'].append({'event': 'gate_forward', 'at': 999}),
                'missing_rejection': lambda d: d.update(events=[e for e in d['events'] if e['event'] != 'gate_reject']),
                'not_native': lambda d: d.update(native_tui=False),
                'no_history': lambda d: d.update(terminal=''),
                'no_exit': lambda d: next(e for e in d['events'] if e['event'] == 'native_exit').update(code=1),
                'no_termios': lambda d: next(e for e in d['events'] if e['event'] == 'native_exit').update(termios_restored=False),
                'cleanup_interfered': lambda d: d['outcome'].update(cleanup_before_observation=True),
            }
            for name, mutate in mutations.items():
                with self.subTest(mode=mode, mutation=name):
                    data = copy.deepcopy(original)
                    mutate(data)
                    with self.assertRaises((AssertionError, StopIteration, IndexError)):
                        checker.check(data)


if __name__ == '__main__':
    unittest.main()
