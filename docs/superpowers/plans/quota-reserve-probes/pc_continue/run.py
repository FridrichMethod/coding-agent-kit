"""Bounded optional PC5 native research runner; logs survive failed runs."""
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


def main():
    candidate = sys.argv[1] if len(sys.argv) > 1 else shutil.which('codex')
    if not candidate or not Path(candidate).is_file():
        print('SKIP: Codex absent; no capability result')
        return 0
    binary = Path(candidate).resolve()
    out = HERE / 'evidence'
    out.mkdir(exist_ok=True)
    scratch = REPO / '.cache/pc_continue'
    scratch.mkdir(parents=True, exist_ok=True)
    roots = []
    summary = {'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'runs': []}
    guard = [sys.executable, '-I', '-B', str(HERE.parent / 'pc/network_guard.py')]
    status = 1
    try:
        stages = [('guard', [sys.executable, '-I', '-B', str(HERE.parent / 'pc/guard_check.py')], 10)]
        stages.extend((mode, [sys.executable, '-I', '-B', str(HERE / 'probe.py'), str(binary), mode], 50)
                      for mode in ('delegation', 'tool', 'tool_interrupt'))
        for label, command, bound in stages:
            try:
                result = subprocess.run([*guard, *command], capture_output=True, text=True, timeout=bound,
                                        start_new_session=True, close_fds=True)
                code, stdout, stderr = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired as error:
                code = 124
                stdout = (error.stdout or b'').decode(errors='replace')
                stderr = (error.stderr or b'').decode(errors='replace') + '\nouter timeout; invalid behavior evidence'
            roots.extend(re.findall(r'^scratch (.+)$', stdout, re.M))
            for old, new in [(str(binary), '<NATIVE_BINARY>'), (str(REPO), '<WORKTREE>')]:
                stdout, stderr = stdout.replace(old, new), stderr.replace(old, new)
            for old in roots:
                stdout, stderr = stdout.replace(old, '<PROBE_ROOT>'), stderr.replace(old, '<PROBE_ROOT>')
            summary['runs'].append({'label': label, 'exit': code, 'stdout': stdout, 'stderr': stderr})
            print(label, 'exit', code, flush=True)
            if code:
                raise RuntimeError(label + ' failed; see manifest')
        status = 0
        return status
    finally:
        summary['fixture_sha256'] = {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest()
                                     for name in ('control.py', 'gate.py', 'probe.py', 'run.py')}
        summary['evidence_sha256'] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in sorted(out.glob('*.json')) if path.name != 'manifest.json'}
        (out / 'manifest.json').write_text(json.dumps(summary, indent=2) + '\n')
        archive = scratch / str(time.time_ns())
        archive.mkdir()
        for path in out.glob('*.json'):
            shutil.copyfile(path, archive / path.name)
        previous = json.loads((scratch / 'roots.json').read_text()) if (scratch / 'roots.json').exists() else []
        (scratch / 'roots.json').write_text(json.dumps(list(dict.fromkeys(previous + roots)), indent=2) + '\n')
        (scratch / 'run.exit').write_text(str(status) + '\n')
        print('TEMP_ROOTS=' + json.dumps(roots), flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
