"""Offline evidence validator for PC6's deliberately narrow routing claim."""
import hashlib
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent


def validate_case(data):
    assert data['version'] == 'codex-cli 0.167.2', 'unreviewed native version'
    assert data['mode'] in ('websocket', 'webrtc')
    assert data['forced_endpoints'] == dict.fromkeys(('websocket', 'webrtc'), '<GATE_URL>')
    assert data['configured_endpoints'] == dict.fromkeys(('websocket', 'webrtc'), '<ALTERNATE_URL>')
    events = data['events']
    assert not any(e.get('service') == 'alternate' for e in events), 'alternate route reached'
    responses = [e for e in events if e['event'] == 'http_response']
    assert all(e['service'] == 'gate' for e in responses), 'unexpected non-gate request'
    requests = [e for e in responses if not (e['method'] == 'GET' and '/models?' in e['path']
                and e['status'] == 200 and e['upgrade'] is False)]
    assert requests, 'no native gate request'
    start = next(e for e in events if e['event'] == 'start_requested')
    assert start['transport'] == data['mode']
    grant = next(e for e in events if e['event'] == 'gate_grant')
    for request in requests:
        assert request['at'] >= start['at'], 'request predates native start'
        assert grant['at'] < request['at'] < grant['at'] + grant['seconds'], 'lease not open'
        assert request['status'] == 503, 'gate admitted work or upgraded connection'
        if data['mode'] == 'websocket':
            assert request['method'] == 'GET' and request['upgrade'] is True
            assert request['path'].split('?')[0] == '/v1/live'
        else:
            assert request['method'] == 'POST' and request['upgrade'] is False
            assert request['path'] == '/live'
    failures = [e for e in events if e['event'] == 'native_failure']
    assert any(e['at'] >= requests[-1]['at'] and '503' in e['message'] for e in failures), \
        'missing native refusal after gate request'
    end = next(e for e in events if e['event'] == 'observation_complete')
    assert end['backend_alive'] and end['at'] - requests[-1]['at'] >= 2
    assert next(e for e in events if e['event'] == 'cleanup')['all_reaped'] is True
    assert not any(e['event'] == 'gate_forward' for e in events), 'upstream model work'
    return len(requests)


def validate_source(expected, actual):
    assert actual == expected, 'source commit or file hashes differ'


def validate_public(data):
    assert not re.search(r'/(?:home|Users|tmp|private|var/folders)/', json.dumps(data)), \
        'machine-specific private path in public evidence'
    if isinstance(data, dict):
        for key, value in data.items():
            if key == 'instructionSources':
                assert all(isinstance(path, str) and path.startswith('<') for path in value), \
                    'instruction source is not anonymized'
            validate_public(value)
    elif isinstance(data, list):
        for value in data:
            validate_public(value)


def main():
    manifest = json.loads((HERE / 'evidence/manifest.json').read_text())
    for path in (HERE / 'evidence').glob('*.json'):
        validate_public(json.loads(path.read_text()))
    validate_source(json.loads((HERE / 'source-lock.json').read_text()), manifest['source'])
    assert manifest['binary_sha256'] == 'a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b'
    assert [r['label'] for r in manifest['runs']] == ['guard', 'websocket', 'webrtc']
    assert all(r['exit'] == 0 for r in manifest['runs'])
    assert 'blocked before network' in manifest['runs'][0]['stdout']
    for group in ('fixture_sha256', 'evidence_sha256'):
        for name, digest in manifest[group].items():
            path = HERE / name
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    if 'resanitization' in manifest:
        for name, digest in manifest['resanitization']['transform_sha256'].items():
            assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == digest, name
    for mode in ('websocket', 'webrtc'):
        count = validate_case(json.loads((HERE / 'evidence' / (mode + '.json')).read_text()))
        print(mode + ': ' + str(count) + ' denied native attempt(s), no alternate hit')
    if len(sys.argv) > 1:
        root = Path(sys.argv[1])
        actual = {'commit': manifest['source']['commit'], 'files': {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in manifest['source']['files']}}
        validate_source(manifest['source'], actual)
        print('source file hashes match pinned checkout')


if __name__ == '__main__':
    main()
