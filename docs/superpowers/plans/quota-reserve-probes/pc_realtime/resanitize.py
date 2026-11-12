"""Offline R1 repair from preserved raw capture; never executes native Codex."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tomllib

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
spec = importlib.util.spec_from_file_location('pc6_transform', HERE / 'probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: resanitize.py ORIGINAL_CAPTURE_ARCHIVE CAPTURE_SOURCE_TREE')
    archive = Path(sys.argv[1]).resolve()
    original = archive / 'manifest.json'
    manifest = json.loads(original.read_text())
    assert 'resanitization' not in manifest, 'supply the original native capture'
    for name, expected in manifest['fixture_sha256'].items():
        relative = (HERE / name).resolve().relative_to(REPO)
        source = subprocess.check_output(['git', 'show', sys.argv[2] + ':' + str(relative)],
            cwd=REPO, timeout=5)
        assert hashlib.sha256(source).hexdigest() == expected, 'incorrect capture source tree'
    manifest['capture_fixture_sha256'] = manifest['fixture_sha256'].copy()
    outputs, raw_hashes = {}, {}
    for mode in ('websocket', 'webrtc'):
        raw, = archive.glob(mode + '-*/raw.json')
        data = json.loads(raw.read_text())
        command = next(e['command'] for e in data['events'] if e['event'] == 'backend_command')
        config = tomllib.loads((raw.parent / 'codex/config.toml').read_text())
        endpoints = {'gate': tomllib.loads(command[2])['experimental_realtime_ws_base_url'],
            'alternate': config['experimental_realtime_ws_base_url'],
            'quota': config['chatgpt_base_url']}
        text = probe.sanitize_evidence(raw.read_text(), raw.parent, command[0], endpoints, REPO)
        probe.check.validate_case(json.loads(text))
        probe.check.validate_public(json.loads(text))
        outputs[mode] = text
        raw_hashes[mode + '/raw.json'] = digest(raw)
    for mode, text in outputs.items():
        (HERE / 'evidence' / (mode + '.json')).write_text(text)
    manifest['fixture_sha256'] = {name: digest(HERE / name)
        for name in [*manifest['capture_fixture_sha256'], 'resanitize.py']}
    manifest['evidence_sha256'] = {name: digest(HERE / name)
        for name in manifest['evidence_sha256']}
    manifest['resanitization'] = {'kind': 'offline-only; no native rerun',
        'capture_source_tree': sys.argv[2], 'original_manifest_sha256': digest(original),
        'raw_sha256': raw_hashes,
        'transform_sha256': {name: digest(HERE / name) for name in ('resanitize.py', 'probe.py')}}
    probe.check.validate_public(manifest)
    (HERE / 'evidence/manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print('Regenerated both public cases from original raw capture; preserved capture hashes.')


if __name__ == '__main__':
    main()
