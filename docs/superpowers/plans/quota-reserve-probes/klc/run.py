"""Bounded KLC runner: each native case in its own process group with a timeout.

Writes evidence/CASE.json (by probe.py) and evidence/manifest.json, which binds
every evidence digest to the code digests that produced it and records each
stage's exit status and duration. Fails if a stage fails, a temporary root
listed in <worktree>/.cache/klc/temp-roots.txt still exists, or a probe
process survives. A missing native is recorded as a skip, never a pass.

python3 -I -B run.py [CASE...]
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import time

HERE = Path(os.path.realpath(__file__)).parent
PROBES = HERE.parent
WT = HERE.parents[4]
CACHE = WT / '.cache' / 'klc'
ORDER = ['refusals', 'threshold', 'loss-kill', 'loss-eof', 'loss-stall', 'gate-death', 'continuation',
         'independent', 'rotation', 'norefresh', 'prelaunch', 'tools', 'sandbox', 'fsrules']
TIMEOUT = {'norefresh': 300, 'gate-death': 300}
CODE = ['klc/' + name for name in ('child.py', 'compose.py', 'fixtures.py', 'fsrules.py', 'gate.py', 'monitor.py',
                                   'probe.py', 'run.py')]
IMPORTED = ['qc/admission.py', 'qc/bearer.py', 'qc/usage.py', 'qc/fake_usage.py', 'nl/boundary.py',
            'nl/natives.py', 'nl/probe.py', 'pc_continue/control.py']


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_digests():
    return {name: sha(PROBES / name) for name in CODE + IMPORTED}


def probe_processes():
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmd = (entry / 'cmdline').read_bytes()
        except OSError:
            continue
        if b'quota-klc-' in cmd or (b'klc/' in cmd and (b'gate.py' in cmd or b'monitor.py' in cmd
                                                          or b'child.py' in cmd or b'probe.py' in cmd)):
            found.append(int(entry.name))
    return found


def main(argv):
    binary = shutil.which('claude')
    binary = os.path.realpath(binary) if binary else None
    cases = argv or ORDER
    manifest_path = HERE / 'evidence' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'stages': {}}
    roots_file = CACHE / 'temp-roots.txt'
    before = set(roots_file.read_text().split()) if roots_file.exists() else set()
    failed = []
    for case in cases:
        if binary is None and case != 'refusals':
            manifest['stages'][case] = {'skipped': 'native_absent'}
            print('skip', case, 'native absent', flush=True)
            continue
        started = time.monotonic()
        process = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'probe.py'), case, binary or ''],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, start_new_session=True, cwd=HERE)
        try:
            output, _ = process.communicate(timeout=TIMEOUT.get(case, 240))
            code = process.returncode
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
            code = 'timeout'
        log = CACHE / 'runs' / ('%s-%d.log' % (case, int(time.time())))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(output)
        evidence = HERE / 'evidence' / (case + '.json')
        manifest['stages'][case] = {'exit': code, 'duration_s': round(time.monotonic() - started, 1),
                                    'evidence_sha256': sha(evidence) if evidence.exists() else None}
        print(case, 'exit', code, round(time.monotonic() - started, 1), flush=True)
        if code != 0:
            failed.append(case)
    time.sleep(0.5)
    after = set(roots_file.read_text().split()) if roots_file.exists() else set()
    leftover = sorted(r for r in after - before if os.path.exists(r))
    alive = probe_processes()
    for pid in alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    manifest.update({
        'native_sha256': sha(binary) if binary else None,
        'native_version': subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=20,
                                         stdin=subprocess.DEVNULL,
                                         env={'PATH': '/usr/bin:/bin', 'HOME': str(CACHE / 'version-home'),
                                              'DISABLE_AUTOUPDATER': '1'}).stdout.strip() if binary else None,
        'kernel': platform.release(), 'machine': platform.machine(), 'python': platform.python_version(),
        'code_sha256': code_digests(), 'leftover_temp_roots': len(leftover), 'leftover_processes': len(alive)})
    manifest['evidence_sha256'] = {p.name: sha(p) for p in sorted((HERE / 'evidence').glob('*.json'))
                                   if p.name != 'manifest.json'}
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + '\n')
    if leftover or alive or failed:
        print('FAILED', {'stages': failed, 'leftover_roots': leftover, 'alive': alive}, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    (CACHE / 'version-home').mkdir(parents=True, exist_ok=True)
    raise SystemExit(main(sys.argv[1:]))
