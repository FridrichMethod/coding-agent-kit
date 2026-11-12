"""Directly supervise tracers for verbatim historical probes; retain failures."""
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
OUT = HERE / 'evidence'


def run(binary):
    binary = Path(binary).resolve()
    OUT.mkdir(exist_ok=True)
    roots, stages = [], []
    status = 0
    print('binary_sha256', hashlib.sha256(binary.read_bytes()).hexdigest(), flush=True)
    for mode, script in (('sigint', 'codex_signal.py'), ('keyboard', 'codex_signal.py'),
                         ('samebackend', 'codex_same_backend.py')):
        argv = [sys.executable, '-I', '-B', str(HERE / 'network_guard.py'),
                sys.executable, '-I', '-B', str(HERE.parent / script), mode, str(binary)]
        if mode == 'samebackend':
            argv.append(str(REPO))
        timed_out = False
        try:
            # The directly supervised child is always the tracer. Its EXITKILL
            # option covers native tracees even when they start new sessions.
            result = subprocess.run(argv, capture_output=True, timeout=40, start_new_session=True)
        except subprocess.TimeoutExpired as error:
            timed_out = True
            result = subprocess.CompletedProcess(argv, 124, error.stdout or b'', error.stderr or b'')
        text = result.stdout.decode(errors='replace')
        match = re.search(r'^scratch (.+)$', text, re.M)
        root = Path(match[1]) if match else None
        if root:
            roots.append(str(root))
        replacements = [(str(binary), '<NATIVE_BINARY>'), (str(REPO), '<WORKTREE>')]
        if root:
            replacements.append((str(root), '<PROBE_ROOT>'))
        def clean(value):
            for old, new in replacements:
                value = value.replace(old, new)
            return re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:<PORT>', value)
        artifact = {'probe': mode, 'supervisor_exit': result.returncode, 'timed_out': timed_out,
                    'original_script_sha256': hashlib.sha256((HERE.parent / script).read_bytes()).hexdigest(),
                    'stdout': clean(text), 'stderr': clean(result.stderr.decode(errors='replace'))}
        if root:
            native = root / mode
            for name in ('output.bin', 'requests.json', 'backend.stderr'):
                if (native / name).exists():
                    artifact[name] = clean((native / name).read_bytes().decode(errors='replace'))
        (OUT / (mode + '.json')).write_text(json.dumps(artifact, indent=2, ensure_ascii=True) + '\n')
        stages.append({'probe': mode, 'exit': result.returncode, 'timed_out': timed_out})
        print(mode, result.returncode, clean(text), flush=True)
        if result.returncode:
            status = result.returncode if result.returncode > 0 else 128 - result.returncode
            break
    print('TEMP_ROOTS=' + json.dumps(roots), flush=True)
    return status, stages, roots


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1])[0])
