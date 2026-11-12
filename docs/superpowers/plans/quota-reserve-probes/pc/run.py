"""Bounded entry point for the optional, offline Linux native research suite."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
OUT = HERE / 'evidence'


def main():
    candidate = sys.argv[1] if len(sys.argv) > 1 else shutil.which('codex')
    if not candidate or not Path(candidate).is_file():
        print('SKIP: native Codex binary not found; no capability result')
        return 0
    binary = Path(candidate).resolve()
    OUT.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix='pc-inspection-'))
    roots = [str(scratch)]
    summary = {'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'runs': []}
    guard = [sys.executable, '-I', '-B', str(HERE / 'network_guard.py')]
    def sanitized(text):
        for old in [str(binary), str(REPO), *roots]:
            text = text.replace(old, '<NATIVE_BINARY>' if old == str(binary) else
                                '<WORKTREE>' if old == str(REPO) else '<PROBE_ROOT>')
        return re.sub(r'127\.0\.0\.1:\d+', '127.0.0.1:<PORT>', text)
    def execute(label, command, limit, env=None):
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=limit, env=env, start_new_session=True)
        except subprocess.TimeoutExpired:
            # subprocess.run kills its child tracer; EXITKILL kills its tracees.
            raise RuntimeError(label + ': outer timeout; not behavioral evidence') from None
        roots.extend(re.findall(r'^scratch (.+)$', result.stdout, re.M))
        found = re.search(r'^TEMP_ROOTS=(.+)$', result.stdout, re.M)
        if found:
            roots.extend(json.loads(found[1]))
        entry = {'label': label, 'exit': result.returncode,
                 'stdout': sanitized(result.stdout), 'stderr': sanitized(result.stderr)}
        summary['runs'].append(entry)
        print(label, 'exit', result.returncode, flush=True)
        if result.returncode:
            print(entry['stdout'], entry['stderr'])
            raise RuntimeError(label + ': failed; inspect evidence')
        return result
    try:
        execute('guard-self-check', [*guard, sys.executable, '-I', '-B', str(HERE / 'guard_check.py')], 10)
        inspect_env = {'PATH': '/usr/bin:/bin', 'HOME': str(scratch), 'CODEX_HOME': str(scratch)}
        summary['version'] = execute('version', [*guard, str(binary), '--version'], 10, inspect_env).stdout.strip()
        spec = importlib.util.spec_from_file_location('pc_reproduce', HERE / 'reproduce.py')
        reproducer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reproducer)
        status, stages, probe_roots = reproducer.run(binary)
        roots.extend(probe_roots)
        summary['runs'].append({'label': 'preserved-reproductions', 'exit': status, 'stages': stages})
        if status:
            raise RuntimeError('preserved-reproductions failed; artifacts and roots retained')
        for mode in ('quota', 'control'):
            execute(mode, [*guard, sys.executable, '-I', '-B', str(HERE / 'targeted.py'), str(binary), mode], 45)
    finally:
        (OUT / 'manifest.json').write_text(json.dumps(summary, indent=2, ensure_ascii=True) + '\n')
        inventory = REPO / '.cache/pc/roots.json'
        inventory.parent.mkdir(parents=True, exist_ok=True)
        previous = json.loads(inventory.read_text()) if inventory.exists() else []
        inventory.write_text(json.dumps(list(dict.fromkeys(previous + roots)), indent=2) + '\n')
        print('TEMP_ROOTS=' + json.dumps(roots), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
