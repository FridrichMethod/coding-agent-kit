"""Bounded KLX runner: offline tests, then every native composition stage, then the checker.

Linux x86_64 with `codex` (0.167.2 was exercised) on PATH or given as the first argument. Each
stage runs compose.py in its own session with an outer timeout; a timeout or failure is recorded
and is never behavioral evidence. After every stage the runner kills any process that still
names that stage's scratch root (a timed-out harness leaves its own sessions behind), archives
the root under .cache/klx/roots/, removes it and appends it to .cache/klx/temp-roots.txt.
Without a native binary it prints SKIP after the offline tests, which proves nothing.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import os

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
CACHE = REPO / '.cache' / 'klx'
STAGES = (('startup', 120), ('pause', 170), ('loss', 210), ('restart', 150), ('sessions', 150),
          ('rotation', 180), ('rotation_gate', 180), ('refresh', 180))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def execute(command, bound):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=bound, start_new_session=True,
                                stdin=subprocess.DEVNULL)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        out = error.stdout.decode(errors='replace') if isinstance(error.stdout, bytes) else (error.stdout or '')
        err = error.stderr.decode(errors='replace') if isinstance(error.stderr, bytes) else (error.stderr or '')
        return 124, out, err + '\nouter timeout; not behavioral evidence'


def sweep(roots, natives):
    """Kill every process that still names one of these scratch roots; returns the count."""
    killed = 0
    for root in roots:
        for item in natives.lingering(root):
            try:
                os.kill(item['pid'], signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                pass
    if killed:
        time.sleep(.5)
    return killed


def archive(roots):
    kept = []
    for root in roots:
        path = Path(root)
        if not path.name.startswith('quota-klx-') or not path.is_absolute() or path.is_symlink() or not path.is_dir():
            continue
        target = CACHE / 'roots' / (path.name + '.tar.gz')
        target.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(target, 'w:gz') as bundle:
            bundle.add(path, arcname=path.name)
        shutil.rmtree(path)
        kept.append(str(target.relative_to(REPO)))
    return kept


def sanitize(text, binary, roots):
    for old, new in [(str(binary), '<NATIVE_BINARY>'), (str(REPO), '<WORKTREE>'), (sys.executable, '<PYTHON>'),
                     (str(Path.home()), '<HOME>'), *[(root, '<PROBE_ROOT>') for root in roots]]:
        text = text.replace(old, new)
    return re.sub(r'eyJ[A-Za-z0-9_-]{8,}\S*', '<JWT>', text)


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    python = [sys.executable, '-I', '-B']
    natives = load('klx_run_natives', HERE.parent / 'nl' / 'natives.py')
    checker = load('klx_run_checker', HERE / 'check_evidence.py')
    summary, status = {'runs': [], 'exit': 1}, 1
    all_roots = []
    try:
        for name in ('test_launcher', 'test_gate'):
            code, out, err = execute([*python, str(HERE / (name + '.py'))], 180)
            summary['runs'].append({'label': name, 'exit': code, 'tail': (out + err)[-300:]})
            print(name, 'exit', code, flush=True)
            if code:
                raise RuntimeError(name + ' failed')
        candidate = sys.argv[1] if len(sys.argv) > 1 else shutil.which('codex')
        if not candidate or not Path(candidate).is_file():
            print('SKIP: native Codex binary not found; offline tests only, no native capability result')
            return 0
        binary = Path(candidate).resolve()
        summary['binary_sha256'] = hashlib.sha256(binary.read_bytes()).hexdigest()
        scratch = CACHE / 'version-home'
        scratch.mkdir(exist_ok=True)
        code, out, _ = execute(['/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'HOME=' + str(scratch),
                                'CODEX_HOME=' + str(scratch), str(binary), '--version'], 15)
        summary['version'] = out.strip()
        for stage, bound in STAGES:
            started = time.monotonic()
            code, out, err = execute([*python, str(HERE / 'compose.py'), str(binary), stage], bound)
            roots = re.findall(r'^scratch (.+)$', out, re.M)
            all_roots += roots
            with (CACHE / 'temp-roots.txt').open('a') as inventory:
                inventory.writelines(root + '\n' for root in roots)
            killed = sweep(roots, natives)
            summary['runs'].append({'label': stage, 'exit': code, 'seconds': round(time.monotonic() - started, 1),
                                    'swept_processes': killed,
                                    'stdout_tail': sanitize(out, binary, roots)[-1200:],
                                    'stderr_tail': sanitize(err, binary, roots)[-1200:]})
            (CACHE / ('stage-' + stage + '.log')).write_text(out + '\n--- stderr ---\n' + err)
            print(stage, 'exit', code, 'seconds', summary['runs'][-1]['seconds'], 'swept', killed, flush=True)
            summary['archived_roots'] = summary.get('archived_roots', []) + archive(roots)
            if code:
                raise RuntimeError(stage + ' failed; see manifest and .cache/klx/stage-%s.log' % stage)
        evidence = {stage + '.json': checker.digest(HERE / 'evidence' / (stage + '.json')) for stage, _ in STAGES}
        summary['evidence'] = evidence
        summary['sources'] = {source: checker.digest(HERE.parent / source) for source in checker.SOURCES}
        summary['metrics'] = checker.metrics({stage: json.loads((HERE / 'evidence' / (stage + '.json')).read_text())
                                              for stage, _ in STAGES})
        summary['exit'] = 0
        write(summary, all_roots)
        status = checker.main([str(HERE / 'evidence')])
        return status
    finally:
        if summary['exit'] != 0:
            write(summary, all_roots)
        (CACHE / 'run.exit').write_text(str(status) + '\n')


def write(summary, roots):
    summary['leftover_roots'] = [root for root in roots if Path(root).exists()]
    (HERE / 'evidence').mkdir(exist_ok=True)
    (HERE / 'evidence' / 'manifest.json').write_text(json.dumps(summary, indent=1, sort_keys=True,
                                                                ensure_ascii=True) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
