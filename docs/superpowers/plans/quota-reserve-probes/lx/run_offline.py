"""Offline LX runner: the three suites, then the real-native fixture cases as evidence.

Run from anywhere: python3 -I -B docs/superpowers/plans/quota-reserve-probes/lx/run_offline.py
Each suite runs in its own bounded process; a timeout or failure is recorded and is
never evidence. The native cases use only fixtures (see test_native.py). Each case's
result is the probe's own sanitized output, scanned again for every fabricated secret,
the host name and token- or id-shaped text before it is written to evidence/.
Exit 0 only when every suite passed, the native cases ran and no temporary root is left.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(os.path.realpath(__file__)).parent
REPO = HERE.parents[4]
EVIDENCE = HERE / 'evidence'
SUITES = (('test_gate', 300), ('test_live_codex', 600), ('test_native', 600))
SUMMARY = re.compile(r'(Ran [0-9]+ tests? in [0-9.]+s|OK( \(skipped=[0-9]+\))?|FAILED \([a-z=0-9, ]+\))')


def load(name):
    spec = importlib.util.spec_from_file_location('lxrun_' + name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def suite(name, bound):
    try:
        result = subprocess.run([sys.executable, '-I', '-B', str(HERE / (name + '.py')), '-v'], capture_output=True,
                                text=True, timeout=bound, stdin=subprocess.DEVNULL, cwd=REPO)
        code, text = result.returncode, result.stderr
    except subprocess.TimeoutExpired:
        code, text = 124, 'outer timeout; not evidence'
    lines = [line.strip() for line in text.splitlines() if SUMMARY.fullmatch(line.strip())]
    skipped = sorted({line.split(' ... skipped ', 1)[1].strip("'") for line in text.splitlines()
                      if ' ... skipped ' in line})
    return {'name': name, 'exit': code, 'summary': lines, 'skip_reasons': skipped}


def main():
    manifest = {'suites': [], 'native': None, 'cases': []}
    status = 0
    for name, bound in SUITES:
        entry = suite(name, bound)
        manifest['suites'].append(entry)
        print(name, 'exit', entry['exit'], ' '.join(entry['summary']), flush=True)
        status |= entry['exit'] != 0
    native = load('test_native')
    kit = native.kit
    if native.BINARY is None:
        manifest['native'] = {'skipped': native.REASON}
        print('SKIP native cases:', native.REASON)
        status = 1
    else:
        with open(native.BINARY, 'rb') as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
        manifest['native'] = {'version': kit.live.EXPECTED_VERSION, 'binary_sha256': digest,
                              'guard': 'pc/network_guard.py'}
        EVIDENCE.mkdir(exist_ok=True)
        certs_root = kit.temp_root('evidence-certs')
        try:
            certs = kit.make_ca(certs_root / 'ca')
            for case in native.CASES:
                root = kit.temp_root('evidence-' + case)
                try:
                    code, result, stdout, stderr, home = native.run_case(case, native.BINARY, certs, root)
                    text = json.dumps(result, indent=1, sort_keys=True, ensure_ascii=True) + '\n'
                    findings = kit.scan({'result': text, 'stdout': stdout, 'stderr': stderr},
                                        home.secrets() + [os.uname().nodename], allowed=(digest,))
                    manifest['cases'].append({'case': case, 'exit': code, 'outcome': (result or {}).get('outcome'),
                                              'leak_findings': findings})
                    print('case', case, 'exit', code, 'findings', findings, flush=True)
                    if findings or result is None:
                        status = 1
                        continue
                    (EVIDENCE / ('native-%s.json' % case)).write_text(text)
                finally:
                    shutil.rmtree(root, ignore_errors=True)
        finally:
            shutil.rmtree(certs_root, ignore_errors=True)
    inventory = kit.CACHE / 'temp-roots.txt'
    left = [line for line in inventory.read_text().splitlines() if line and os.path.lexists(line)] \
        if inventory.exists() else []
    manifest['temp_roots_left'] = len(left)
    status |= bool(left)
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / 'manifest.json').write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n')
    (kit.CACHE / 'run.exit').write_text('%d\n' % status)
    print('exit', status)
    return status


if __name__ == '__main__':
    sys.exit(main())
