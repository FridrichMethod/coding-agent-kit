"""Evidence-check regressions: incomplete stream/queue/native proof must fail."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pc4_check', HERE / 'check_evidence.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class EvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pc4-check-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(HERE / 'evidence', self.root / 'evidence')
        for name in ('gate.py', 'probe.py', 'run.py'):
            shutil.copyfile(HERE / name, self.root / name)
        original = checker.HERE
        checker.HERE = self.root / 'evidence'
        self.addCleanup(setattr, checker, 'HERE', original)

    def verify(self):
        with contextlib.redirect_stdout(io.StringIO()):
            checker.main()

    def corrupt(self, mode, change):
        path = checker.HERE / (mode + '.json')
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))
        # Rebind the digest so these cases test semantic evidence, not hashing.
        manifest = checker.HERE / 'manifest.json'
        value = json.loads(manifest.read_text())
        value['evidence_sha256'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(value))
        with self.assertRaises(AssertionError):
            self.verify()

    def test_complete_capture_passes(self):
        self.verify()

    def test_missing_stream_close_fails(self):
        self.corrupt('lease', lambda data: checker.one(data, 'all_owned_closed')['closed_ids'].pop())

    def test_wrong_native_genealogy_fails(self):
        def change(data):
            checker.one(data, 'native_delegation')['child_spawn']['receiverThreadIds'] = ['unrelated']
        self.corrupt('monitor_death', change)

    def test_heartbeat_reopen_fails(self):
        self.corrupt('lease', lambda data: checker.one(data, 'heartbeat_after_latch').update(ignored=False))

    def test_missing_queue_rejection_fails(self):
        def change(data):
            for row in data['events']:
                if row['event'] == 'gate_reject':
                    row['old_queue'] = False
        self.corrupt('eof', change)

    def test_late_forward_fails(self):
        def change(data):
            data['events'].append({'event': 'gate_forward', 'request_id': 99,
                                  'at': checker.one(data, 'gate_latched')['at'] + 1})
        self.corrupt('lease', change)

    def test_failed_native_stage_fails(self):
        path = checker.HERE / 'manifest.json'
        data = json.loads(path.read_text())
        data['runs'][-1]['exit'] = 124
        path.write_text(json.dumps(data))
        with self.assertRaises(AssertionError):
            self.verify()


if __name__ == '__main__':
    unittest.main()
