"""Bounded QX runner: offline tests, guard self-check, then the native stages.

Linux x86_64 only for native stages (it reuses PC's research network guard).
Each stage runs under the guard with an outer timeout; a timeout or failure is
recorded and is never behavioral evidence. Temporary roots (quota-qx-*) are
archived under .cache/qx/ and then removed; only roots this run printed are
touched. Without a native binary the runner says SKIP and proves nothing.
"""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
CACHE = REPO / '.cache/qx'
STAGES = (('shape', 60), ('rotation', 60), ('proactive', 60), ('identity', 75), ('concurrent', 75))


def sanitize(text, binary, roots):
    for old, new in [(str(binary), '<NATIVE_BINARY>'), (str(REPO), '<WORKTREE>'), *[(r, '<PROBE_ROOT>') for r in roots]]:
        text = text.replace(old, new)
    return re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:<PORT>', text)


def execute(command, bound):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=bound, start_new_session=True,
                                stdin=subprocess.DEVNULL)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        # subprocess.run kills the tracer; the guard's EXITKILL ends its tracees.
        out = error.stdout.decode(errors='replace') if isinstance(error.stdout, bytes) else (error.stdout or '')
        err = error.stderr.decode(errors='replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
        return 124, out, err + '\nouter timeout; not behavioral evidence'


def archive(roots):
    """Keep a durable copy of each fixture-only temporary root, then remove exactly those roots."""
    kept = []
    for root in roots:
        path = Path(root)
        if not path.name.startswith('quota-qx-') or path.parent != Path('/tmp') or not path.is_dir():
            continue
        target = CACHE / 'roots' / (path.name + '.tar.gz')
        target.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(target, 'w:gz') as bundle:
            bundle.add(path, arcname=path.name)  # tarfile skips sockets itself
        shutil.rmtree(path)
        kept.append(str(target.relative_to(REPO)))
    return kept


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    status = 1
    roots, summary = [], {'runs': []}
    python = [sys.executable, '-I', '-B']
    try:
        for name in ('test_binding', 'test_gate', 'test_lease_coexistence'):
            code, out, err = execute([*python, str(HERE / (name + '.py'))], 120)
            summary['runs'].append({'label': name, 'exit': code, 'tail': (out + err)[-400:]})
            print(name, 'exit', code, flush=True)
            if code:
                raise RuntimeError(name + ' failed')
        candidate = sys.argv[1] if len(sys.argv) > 1 else shutil.which('codex')
        if not candidate or not Path(candidate).is_file():
            print('SKIP: native Codex binary not found; offline tests only, no native capability result')
            status = 0
            return status
        binary = Path(candidate).resolve()
        summary['binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
        guard = [*python, str(HERE.parent / 'pc/network_guard.py')]
        code, out, err = execute([*guard, *python, str(HERE.parent / 'pc/guard_check.py')], 15)
        summary['runs'].append({'label': 'guard-self-check', 'exit': code, 'stdout': out, 'stderr': err})
        print('guard-self-check exit', code, flush=True)
        if code:
            raise RuntimeError('guard self-check failed')
        scratch = CACHE / 'version-home'
        scratch.mkdir(exist_ok=True)
        env = ['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'HOME=' + str(scratch), 'CODEX_HOME=' + str(scratch)]
        code, out, err = execute([*guard, *env, str(binary), '--version'], 15)
        summary['version'] = out.strip()
        for stage, bound in STAGES:
            started = time.monotonic()
            code, out, err = execute([*guard, *python, str(HERE / 'probe.py'), str(binary), stage], bound)
            roots.extend(re.findall(r'^scratch (.+)$', out, re.M))
            denied = re.findall(r'^\{"guard_denied_syscalls".*$', err, re.M)
            summary['runs'].append({'label': stage, 'exit': code, 'seconds': round(time.monotonic() - started, 1),
                                    'guard': json.loads(denied[-1]) if denied else None,
                                    'stdout_tail': sanitize(out, binary, roots)[-1500:],
                                    'stderr_tail': sanitize(err, binary, roots)[-1500:]})
            print(stage, 'exit', code, flush=True)
            if code:
                raise RuntimeError(stage + ' failed; see manifest')
        status = 0
        return status
    finally:
        with (CACHE / 'temp-roots.txt').open('a') as inventory:
            for root in roots:
                inventory.write(root + '\n')
        summary['archived_roots'] = archive(roots)
        (HERE / 'evidence').mkdir(exist_ok=True)
        (HERE / 'evidence/manifest.json').write_text(json.dumps(summary, indent=1, ensure_ascii=True) + '\n')
        (CACHE / 'run.exit').write_text(str(status) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
