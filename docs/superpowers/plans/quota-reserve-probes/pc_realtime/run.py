"""One bounded PC6 candidate; fake auth, no audio, no external requests."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
BINARY_SHA256 = 'a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if len(sys.argv) != 3:
        raise SystemExit('usage: run.py NATIVE_BINARY PINNED_CODEX_SOURCE_CHECKOUT')
    binary, source = (Path(arg).resolve() for arg in sys.argv[1:])
    if not binary.is_file():
        print('SKIP: Codex absent; no capability result')
        return 0
    assert digest(binary) == BINARY_SHA256, 'different binary: review source/version before native execution'
    expected = json.loads((HERE / 'source-lock.json').read_text())
    actual = {'commit': expected['commit'], 'files': {
        name: digest(source / name) for name in expected['files']}}
    assert actual == expected, 'source hash mismatch'
    scratch = REPO / '.cache/pc_realtime'
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = scratch / str(time.time_ns())
    archive.mkdir(mode=0o700)
    out = HERE / 'evidence'
    out.mkdir(exist_ok=True)
    roots = []
    summary = {'binary_sha256': digest(binary), 'source': actual, 'runs': []}
    guard = [sys.executable, '-I', '-B', str(HERE.parent / 'pc/network_guard.py')]
    status = 1
    try:
        stages = [('guard', [sys.executable, '-I', '-B', str(HERE.parent / 'pc/guard_check.py')], 10)]
        stages.extend((mode, [sys.executable, '-I', '-B', str(HERE / 'probe.py'), str(binary),
            mode, str(archive)], 40) for mode in ('websocket', 'webrtc'))
        for label, command, bound in stages:
            try:
                result = subprocess.run([*guard, *command], capture_output=True, text=True,
                    timeout=bound, start_new_session=True, close_fds=True)
                code, stdout, stderr = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired as error:
                # subprocess.run kills/reaps the guard; its EXITKILL covers tracees.
                code = 124
                stdout = (error.stdout or b'').decode(errors='replace')
                stderr = (error.stderr or b'').decode(errors='replace') + '\nouter timeout; invalid evidence'
            (archive / (label + '.stdout')).write_text(stdout)
            (archive / (label + '.stderr')).write_text(stderr)
            roots.extend(re.findall(r'^scratch (.+)$', stdout, re.M))
            for old, new in [(str(binary), '<NATIVE_BINARY>'), *[(r, '<PROBE_ROOT>') for r in roots],
                             (str(REPO), '<WORKTREE>')]:
                stdout, stderr = stdout.replace(old, new), stderr.replace(old, new)
            summary['runs'].append({'label': label, 'exit': code, 'stdout': stdout, 'stderr': stderr})
            print(label, 'exit', code, flush=True)
            if code:
                raise RuntimeError(label + ' failed; private logs retained')
        status = 0
    finally:
        summary['fixture_sha256'] = {name: digest(HERE / name) for name in (
            'run.py', 'probe.py', 'source-lock.json', '../pc/network_guard.py',
            '../pc/guard_check.py', '../pc/targeted.py', '../pc_continue/gate.py')}
        summary['evidence_sha256'] = {str(path.relative_to(HERE)): digest(path)
            for path in out.glob('*.json') if path.name != 'manifest.json'}
        (out / 'manifest.json').write_text(json.dumps(summary, indent=2) + '\n')
        for path in out.glob('*.json'):
            shutil.copyfile(path, archive / path.name)
        previous = json.loads((scratch / 'roots.json').read_text()) if (scratch / 'roots.json').exists() else []
        (scratch / 'roots.json').write_text(json.dumps(list(dict.fromkeys(previous + [str(archive)] + roots)), indent=2) + '\n')
        (archive / 'run.exit').write_text(str(status) + '\n')
        print('TEMP_ROOTS=' + json.dumps(roots), flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
