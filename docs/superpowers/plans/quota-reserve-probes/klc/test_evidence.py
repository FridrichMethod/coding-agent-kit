"""Regression tests: the KLC checker accepts the retained evidence and rejects
tampered copies (a forward after the latch, a wrong latch reason, a non-loopback
connect, an admitted later prompt, a reopened pause, upstream bytes of the
switched bearer, a changed digest, and more). Offline; no native run.
"""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('klc_check', HERE / 'check_evidence.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)
EVIDENCE = HERE / 'evidence'


def load(name):
    return json.loads((EVIDENCE / (name + '.json')).read_text())


def first(rows, predicate):
    return next(r for r in rows if predicate(r))


class Accepts(unittest.TestCase):
    def test_retained_evidence_passes(self):
        self.assertEqual(checker.main([str(EVIDENCE)]), 0)


class Rejects(unittest.TestCase):
    def rejects(self, name, mutate):
        document = copy.deepcopy(load(name))
        checker.check(name, load(name))  # the original passes
        mutate(document)
        with self.assertRaises((checker.Failure, KeyError, IndexError, TypeError)):
            checker.check(name, document)

    def test_forward_after_latch(self):
        def mutate(d):
            mark = checker.latch(d)
            row = copy.deepcopy(checker.gate(d, 'forwarded')[0])
            row['t'] = mark['t'] + 0.01
            d['rows'].append(row)
        for name in ('threshold', 'loss-kill', 'loss-eof', 'loss-stall', 'rotation', 'norefresh', 'tools'):
            self.rejects(name, mutate)

    def test_upstream_arrival_without_gate_forward(self):
        def mutate(d):
            row = copy.deepcopy(first(d['rows'], lambda r: r['row'] == 'upstream_request'))
            d['rows'].append(row)
        self.rejects('threshold', mutate)

    def test_wrong_latch_reason(self):
        def mutate(d):
            checker.latch(d)['reason'] = 'monitor_eof'
        self.rejects('threshold', mutate)
        self.rejects('loss-stall', mutate)

    def test_slow_monitor_loss(self):
        self.rejects('loss-kill', lambda d: d.__setitem__('trigger_t', checker.latch(d)['t'] - 2))

    def test_non_loopback_connect(self):
        def mutate(d):
            d['cleanup']['main']['syscalls']['inet_connect'].append(
                {'call': 'connect', 'exe': 'x', 'result': 'EINPROGRESS', 'target': 'non_loopback'})
        self.rejects('threshold', mutate)

    def test_undenied_other_port(self):
        def mutate(d):
            d['cleanup']['main']['syscalls']['inet_connect'].append(
                {'call': 'connect', 'exe': 'x', 'result': 'EINPROGRESS', 'target': 'other_loopback'})
        self.rejects('tools', mutate)

    def test_later_prompt_admitted_or_native_dead(self):
        self.rejects('threshold', lambda d: d.__setitem__('later_admitted', True))
        self.rejects('loss-eof', lambda d: d.__setitem__('native_alive_after_pause', False))

    def test_held_stream_not_closed(self):
        def mutate(d):
            d['rows'] = [r for r in d['rows'] if r['row'] != 'stream_disconnected']
        self.rejects('threshold', mutate)

    def test_sdk_child_admitted_after_latch(self):
        def mutate(d):
            mark = checker.latch(d)
            row = copy.deepcopy(first(d['child_rows'], lambda r: r['attempt'] == 'model_request'))
            row.update(status=200, sent=mark['t'] + 1, t=mark['t'] + 1)
            d['child_rows'].append(row)
        self.rejects('threshold', mutate)

    def test_direct_egress_reached(self):
        def mutate(d):
            first(d['child_rows'], lambda r: r['attempt'] == 'direct_upstream')['result'] = 'reached'
        self.rejects('threshold', mutate)

    def test_gate_death_variants(self):
        self.rejects('gate-death', lambda d: d.__setitem__('outside_bind_with_holder', 'bound'))
        self.rejects('gate-death', lambda d: d.__setitem__('upstream_count_while_dead',
                                                           d['upstream_count_at_kill'] + 1))
        self.rejects('gate-death', lambda d: d.__setitem__('refused_after_restart', 0))

    def test_continuation_variants(self):
        for key in ('paste_ignored', 'stale_typed_ignored', 'native_output_ignored', 'same_invocation'):
            self.rejects('continuation', lambda d, key=key: d.__setitem__(key, False))
        self.rejects('continuation', lambda d: d['passive_status'].__setitem__('state', 'open'))
        self.rejects('continuation', lambda d: d.__setitem__('background_requests_while_paused', [200, 403]))

    def test_independent_variants(self):
        self.rejects('independent', lambda d: d.__setitem__('inv2_latched_after_first_change', True))
        self.rejects('independent', lambda d: d.__setitem__('inv1_forwarded_after_first_change', 1))

    def test_rotation_variants(self):
        def b_upstream(d):
            row = copy.deepcopy(first(d['rows'], lambda r: r['row'] == 'upstream_request'))
            row['label'] = 'B'
            d['rows'].append(row)
        self.rejects('rotation', b_upstream)
        self.rejects('rotation', lambda d: d['connects_while_paused'].append(['platform.claude.com:443', 200]))
        self.rejects('rotation', lambda d: d.__setitem__('connects_while_open',
                                                         d['connects_while_open'] + [['api.anthropic.com:443', 200]]))

        def early_forward(d):
            usage = [u for u in d['usage_requests'] if u['account'] != 'A2']
            d['usage_requests'] = usage
        self.rejects('rotation', early_forward)

    def test_tools_and_refusals_variants(self):
        self.rejects('tools', lambda d: d.__setitem__('fg_ticks_2s_after', d['fg_ticks_at_latch']))
        self.rejects('refusals', lambda d: d.__setitem__('upstream_requests', 1))
        self.rejects('refusals', lambda d: d['quota_startup']['threshold'].__setitem__('native_started', True))
        self.rejects('prelaunch', lambda d: d['runs'][0].__setitem__('refresh_requests', 0))
        self.rejects('fsrules', lambda d: d.__setitem__('outside_written', True))
        self.rejects('sandbox', lambda d: d.__setitem__('file_written', True))

    def test_manifest_binding(self):
        directory = Path(tempfile.mkdtemp(prefix='quota-klc-evidence-'))
        self.addCleanup(shutil.rmtree, directory)
        for path in EVIDENCE.glob('*.json'):
            shutil.copy(path, directory / path.name)
        target = directory / 'threshold.json'
        target.write_text(target.read_text().replace('"held_closed": true', '"held_closed": false'))
        self.assertEqual(checker.main([str(directory)]), 1)
        manifest = json.loads((EVIDENCE / 'manifest.json').read_text())
        manifest['code_sha256']['klc/gate.py'] = '0' * 64
        (directory / 'manifest.json').write_text(json.dumps(manifest))
        shutil.copy(EVIDENCE / 'threshold.json', target)
        self.assertEqual(checker.main([str(directory)]), 1)


if __name__ == '__main__':
    unittest.main()
