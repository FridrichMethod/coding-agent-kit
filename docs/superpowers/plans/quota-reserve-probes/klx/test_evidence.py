"""Regression tests for check_evidence.py: the committed capture passes; tampered copies fail.

python3 -I -B test_evidence.py
"""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('klx_test_checker', HERE / 'check_evidence.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class Evidence(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='klx-evidence-'))
        for path in (HERE / 'evidence').glob('*.json'):
            shutil.copy(path, self.directory / path.name)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def load(self, stage):
        return json.loads((self.directory / (stage + '.json')).read_text())

    def save(self, stage, data, rebind=True):
        path = self.directory / (stage + '.json')
        path.write_text(json.dumps(data, indent=1, sort_keys=True) + '\n')
        if rebind:  # keep the digest valid so the property check itself must fail
            manifest = json.loads((self.directory / 'manifest.json').read_text())
            manifest['evidence'][stage + '.json'] = checker.digest(path)
            stages = {name: json.loads((self.directory / (name + '.json')).read_text()) for name in checker.STAGES}
            manifest['metrics'] = checker.metrics(stages)
            (self.directory / 'manifest.json').write_text(json.dumps(manifest))

    def fails(self, message=None):
        with self.assertRaises((checker.Failure, KeyError, IndexError, StopIteration)) as raised:
            checker.check(self.directory)
        if message:
            self.assertIn(message, str(raised.exception))

    def test_capture_passes(self):
        checker.check(self.directory)

    def test_digest_binding(self):
        data = self.load('pause')
        data['note'] = 'edited'
        self.save('pause', data, rebind=False)
        self.fails('digest mismatch')

    def test_missing_stage(self):
        (self.directory / 'loss.json').unlink()
        self.fails('missing evidence')

    def test_leaked_token(self):
        data = self.load('rotation')
        data['extra']['leak'] = 'eyJhbGciOiJub25lIn0.payload.sig'
        self.save('rotation', data)
        self.fails('leaks')

    def test_child_admitted_after_latch(self):
        data = self.load('pause')
        for row in data['invocations'][0]['actors']:
            if row['attempt'] == 'model_after_signal':
                row['result'] = 200
        self.save('pause', data)
        self.fails('child request')

    def test_model_reached_upstream_while_paused(self):
        data = self.load('pause')
        latch = data['extra']['latch_at']
        data['backend'].append({'at': latch + 0.5, 'kind': 'model', 'marker': 'KLX-INJECTED', 'label': 'A1'})
        self.save('pause', data)
        self.fails('reached upstream')

    def test_unbound_admission(self):
        data = self.load('rotation')
        events = data['invocations'][0]['gates'][0]['events']
        events.append({'at': events[-1]['at'] + 1, 'event': 'admission', 'admitted': True, 'credential': 'T-NEW',
                       'path': '/backend-api/codex/responses'})
        self.save('rotation', data)
        self.fails('without its own fresh observation')

    def test_restarted_gate_open(self):
        data = self.load('restart')
        for row in data['timeline']:
            if row['event'] == 'restarted_latched':
                row['status']['paused'] = None
        self.save('restart', data)
        self.fails('restarted open')

    def test_monitor_loss_without_tty(self):
        data = self.load('loss')
        data['extra']['loss_cases'][1]['tui_alive'] = False
        self.save('loss', data)
        self.fails('TTY')

    def test_sessions_not_independent(self):
        data = self.load('sessions')
        for row in data['timeline']:
            if row['event'] == 'independent_sessions':
                row['s2_completed'] = False
        self.save('sessions', data)
        self.fails('not independent')

    def test_refresh_tunnel_while_paused(self):
        data = self.load('refresh')
        rows = [r for r in data['invocations'][0]['actors'] if r['actor'] == 'tunnel'
                and r['attempt'].startswith('connect_')]
        rows[-1]['result'] = 'tunnel'
        self.save('refresh', data)
        self.fails('tunnel while paused')

    def test_startup_model_request(self):
        data = self.load('startup')
        data['extra']['preflight_cases'][0]['model_requests'] = 1
        self.save('startup', data)
        self.fails('native started')

    def test_lingering_process(self):
        data = self.load('sessions')
        data['lingering_after_kill'] = 1
        self.save('sessions', data)
        self.fails('processes left')

    def test_metrics_binding(self):
        manifest = json.loads((self.directory / 'manifest.json').read_text())
        manifest['metrics']['admission_overhead'] = {'n': 1, 'median_ms': 0.0, 'max_ms': 0.0}
        (self.directory / 'manifest.json').write_text(json.dumps(manifest))
        self.fails('metrics differ')

    def test_non_gate_connect(self):
        data = self.load('pause')
        data['invocations'][0]['trace']['syscalls']['inet_connect'].append(
            {'call': 'connect', 'exe': 'codex', 'target': 'non_loopback', 'result': 'EINPROGRESS'})
        self.save('pause', data)
        self.fails('non-loopback')


if __name__ == '__main__':
    unittest.main()
