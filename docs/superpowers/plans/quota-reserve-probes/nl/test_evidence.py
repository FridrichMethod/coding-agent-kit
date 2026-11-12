"""Evidence-check regressions: a weakened capture or a missing control must fail.

python3 -I -B test_evidence.py
"""
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
spec = importlib.util.spec_from_file_location('nl_check', HERE / 'check_evidence.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class EvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nl-check-')
        self.addCleanup(self.temp.cleanup)
        shutil.copytree(HERE / 'evidence', Path(self.temp.name) / 'evidence')
        for path in HERE.glob('*.py'):
            shutil.copyfile(path, Path(self.temp.name) / path.name)
        original = checker.HERE
        checker.HERE = Path(self.temp.name) / 'evidence'
        self.addCleanup(setattr, checker, 'HERE', original)

    def verify(self):
        with contextlib.redirect_stdout(io.StringIO()):
            checker.main()

    def corrupt(self, name, change, rebind=True):
        path = checker.HERE / (name + '.json')
        data = json.loads(path.read_text())
        change(data)
        path.write_text(json.dumps(data))
        if rebind:
            # Rebind the digest so these cases test semantics, not hashing.
            manifest = checker.HERE / 'manifest.json'
            value = json.loads(manifest.read_text())
            value['evidence_sha256'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(value))
        with self.assertRaises(AssertionError):
            self.verify()

    def event(self, data, event, marker):
        data['events'].append({'at': 0, 'event': event, 'marker': marker})

    def test_complete_capture_passes(self):
        self.verify()

    def test_unsigned_change_fails(self):
        self.corrupt('host', lambda data: data['landlock'].update(abi=9), rebind=False)

    def test_changed_probe_code_fails(self):
        target = checker.HERE.parent / 'boundary.py'
        target.write_text(target.read_text() + '# changed\n')
        with self.assertRaises(AssertionError):
            self.verify()

    def test_failed_stage_fails(self):
        self.corrupt('manifest', lambda data: data['stages']['matrix'].update(exit=124), rebind=False)

    def test_l2a_reaching_alternate_listener_fails(self):
        self.corrupt('matrix', lambda data: self.event(data, 'alt_tcp', 'L2a:detached:tcp6_alt'))

    def test_l2a_udp_reaching_listener_fails(self):
        self.corrupt('matrix', lambda data: self.event(data, 'alt_udp', 'L2a:node:udp4_alt'))

    def test_missing_control_fails(self):
        def change(data):
            data['events'] = [e for e in data['events'] if e.get('marker') != 'L0:shell:tcp6_alt']
        self.corrupt('matrix', change)

    def test_unobserved_mptcp_gap_fails(self):
        def change(data):
            data['events'] = [e for e in data['events'] if e.get('marker') != 'L1:root:mptcp_alt']
        self.corrupt('matrix', change)

    def test_l2a_escape_fails(self):
        self.corrupt('matrix', lambda data: self.event(data, 'alt_tcp', 'L2a:escaped:tcp4_alt'))

    def test_usable_closed_inherited_descriptor_fails(self):
        def change(data):
            data['stages']['INH-closed']['rows'][0]['result'] = 'ok'
        self.corrupt('matrix', change)

    def test_send_after_latch_fails(self):
        def change(data):
            data['stages']['LATCH']['gate_received'].append({'after_latch': True, 'data': 'late'})
        self.corrupt('matrix', change)

    def test_native_direct_egress_fails(self):
        self.corrupt('claude-inet', lambda data: data['fixture_events'].append(
            {'event': 'alt_tcp', 'marker': 'CLAUDE-inet:claude_tool:tcp4_alt'}))

    def test_native_without_model_tool_call_fails(self):
        def change(data):
            for row in data['events']:
                row.pop('tool_call', None)
        self.corrupt('codex-inet-no-daemon', change)

    def test_unsupported_outside_daemon_claim_fails(self):
        def change(data):
            for row in data['tool_rows']:
                if row['attempt'] == 'tcp4_alt':
                    row['result'] = 'EACCES'
        self.corrupt('codex-inet-unix-outside-daemon', change)

    def test_native_lingering_process_fails(self):
        self.corrupt('claude-inet', lambda data: data.update(lingering_after_kill=1))

    def test_compat_forkserver_success_under_l2a_fails(self):
        def change(data):
            for case in data['runs']['L2a']['cases']:
                if case['case'] == 'multiprocessing_default_pool':
                    case['exit'] = 0
        self.corrupt('compat', change)

    def test_enforced_ip_address_deny_fails(self):
        def change(data):
            data['systemd_user']['checks']['service_IPAddressDeny_any']['listener_reached'] = False
        self.corrupt('host', change)


if __name__ == '__main__':
    unittest.main()
