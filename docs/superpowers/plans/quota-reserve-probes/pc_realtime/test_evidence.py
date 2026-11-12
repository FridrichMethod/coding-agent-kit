"""Mutations that would otherwise turn absent/escaped native work into a pass."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('pc6_check', HERE / 'check_evidence.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
spec = importlib.util.spec_from_file_location('pc6_probe', HERE / 'probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def fixture():
    return {'mode': 'websocket', 'version': 'codex-cli 0.167.2',
        'events': [
            {'event': 'start_requested', 'transport': 'websocket', 'at': 1},
            {'event': 'http_response', 'service': 'gate', 'method': 'GET',
             'path': '/v1/live?model=fixture', 'upgrade': True, 'status': 503, 'at': 2},
            {'event': 'native_failure', 'message': 'HTTP error: 503 Service Unavailable', 'at': 3},
            {'event': 'observation_complete', 'at': 5, 'backend_alive': True},
            {'event': 'gate_grant', 'at': 0, 'seconds': 20},
            {'event': 'cleanup', 'all_reaped': True}],
        'forced_endpoints': {'websocket': '<GATE_URL>', 'webrtc': '<GATE_URL>'},
        'configured_endpoints': {'websocket': '<ALTERNATE_URL>', 'webrtc': '<ALTERNATE_URL>'}}


class EvidenceMutations(unittest.TestCase):
    def test_worktree_and_ancestor_instruction_sources_are_redacted(self):
        raw = {'instructionSources': ['/srv/private/tree/AGENTS.md', '/srv/private/AGENTS.md'],
            'nested': {'instructionSources': ['/home/private/AGENTS.md']},
            'cwd': '/srv/private/tree/.cache/probe/workspace',
            'message': 'read /srv/private/tree/AGENTS.md', 'path': '/live',
            'binary': '/opt/private/codex', 'url': 'http://127.0.0.1:123/live'}
        actual = json.loads(probe.sanitize_evidence(json.dumps(raw),
            '/srv/private/tree/.cache/probe', '/opt/private/codex',
            {'gate': 'http://127.0.0.1:123'}, '/srv/private/tree'))
        self.assertEqual(actual['instructionSources'],
            ['<WORKTREE>/AGENTS.md', '<ANCESTOR_INSTRUCTION_SOURCE>'])
        self.assertEqual(actual['nested']['instructionSources'], ['<ANCESTOR_INSTRUCTION_SOURCE>'])
        self.assertEqual(actual['cwd'], '<PROBE_ROOT>/workspace')
        self.assertEqual(actual['message'], 'read <WORKTREE>/AGENTS.md')
        self.assertEqual(actual['binary'], '<NATIVE_BINARY>')
        self.assertEqual(actual['url'], '<GATE_URL>/live')
        self.assertEqual(actual['path'], '/live')
        check.validate_public(actual)

    def test_public_checker_rejects_private_paths_and_absolute_instruction_sources(self):
        for value in [{'instructionSources': ['/srv/private/AGENTS.md']},
                      {'nested': {'message': 'source /home/private/tree/AGENTS.md'}}]:
            with self.subTest(value=value), self.assertRaises(AssertionError):
                check.validate_public(value)

    def test_saved_artifacts_have_no_machine_paths(self):
        for path in (HERE / 'evidence').glob('*.json'):
            with self.subTest(path=path.name):
                check.validate_public(json.loads(path.read_text()))

    def test_saved_native_cases_and_mutations(self):
        for mode in ('websocket', 'webrtc'):
            evidence = json.loads((HERE / 'evidence' / (mode + '.json')).read_text())
            with self.subTest(mode=mode):
                self.assertEqual(check.validate_case(evidence), 1)
            for mutation in ('missing_request', 'alternate', 'successful_upgrade'):
                changed = copy.deepcopy(evidence)
                requests = [e for e in changed['events'] if e['event'] == 'http_response'
                            and e['status'] == 503]
                if mutation == 'missing_request':
                    changed['events'] = [e for e in changed['events'] if e not in requests]
                elif mutation == 'alternate':
                    requests[0]['service'] = 'alternate'
                else:
                    requests[0]['status'] = 101
                with self.subTest(mode=mode, mutation=mutation), self.assertRaises(AssertionError):
                    check.validate_case(changed)

    def test_denied_native_attempt_is_required(self):
        evidence = fixture()
        self.assertEqual(check.validate_case(evidence), 1)
        evidence['events'] = [e for e in evidence['events'] if e['event'] != 'http_response']
        with self.assertRaises(AssertionError):
            check.validate_case(evidence)

    def test_alternate_attempt_cannot_be_hidden_by_rejected_gate(self):
        evidence = fixture()
        extra = copy.deepcopy(evidence['events'][1])
        extra['service'] = 'alternate'
        evidence['events'].append(extra)
        with self.assertRaises(AssertionError):
            check.validate_case(evidence)

    def test_successful_upgrade_or_call_is_not_refusal(self):
        for status in (101, 200):
            with self.subTest(status=status):
                evidence = fixture()
                evidence['events'][1]['status'] = status
                with self.assertRaises(AssertionError):
                    check.validate_case(evidence)

    def test_wrong_path_or_transport_does_not_prove_route(self):
        for key, value in [('path', '/models'), ('method', 'POST'), ('upgrade', False)]:
            with self.subTest(key=key):
                evidence = fixture()
                evidence['events'][1][key] = value
                with self.assertRaises(AssertionError):
                    check.validate_case(evidence)

    def test_failure_before_request_cannot_pass(self):
        evidence = fixture()
        evidence['events'][2]['at'] = .5
        with self.assertRaises(AssertionError):
            check.validate_case(evidence)

    def test_missing_failure_or_unreaped_backend_cannot_pass(self):
        evidence = fixture()
        evidence['events'] = [e for e in evidence['events'] if e['event'] != 'native_failure']
        with self.assertRaises(AssertionError):
            check.validate_case(evidence)
        evidence = fixture()
        evidence['events'][-1]['all_reaped'] = False
        with self.assertRaises(AssertionError):
            check.validate_case(evidence)

    def test_independent_both_endpoint_overrides_required(self):
        evidence = fixture()
        evidence['forced_endpoints']['webrtc'] = '<ALTERNATE_URL>'
        with self.assertRaises(AssertionError):
            check.validate_case(evidence)

    def test_source_hash_binding(self):
        source = {'commit': 'fixed', 'files': {'core.rs': 'abc'}}
        check.validate_source(source, copy.deepcopy(source))
        for changed in [{'commit': 'other', 'files': source['files']},
                        {'commit': 'fixed', 'files': {'core.rs': 'def'}}]:
            with self.assertRaises(AssertionError):
                check.validate_source(source, changed)

    def test_live_response_grant_and_observation_window_required(self):
        evidence = fixture()
        evidence['events'][-2]['seconds'] = .5
        with self.assertRaises(AssertionError):
            check.validate_case(evidence)
        evidence = fixture()
        evidence['events'][3]['at'] = 2.1
        with self.assertRaises(AssertionError):
            check.validate_case(evidence)


if __name__ == '__main__':
    unittest.main()
