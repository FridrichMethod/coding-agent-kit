"""Bounded NL runner: every stage in its own process group with a timeout.

Writes sanitized evidence/*.json plus evidence/manifest.json, archives a copy
under <worktree>/.cache/nl/runs/, records temporary roots in
<worktree>/.cache/nl/temp-roots.txt and verifies that no probe process,
temporary root or transient unit is left behind. A native that is absent is
skipped and recorded; no stage result is inferred.

python3 -I -B run.py [STAGE...]
"""
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
SCRATCH = REPO / '.cache' / 'nl'
FIXTURES = ('attempt.py', 'boundary.py', 'compat.py', 'hostfacts.py', 'natives.py', 'probe.py', 'run.py')
CLAUDE, CODEX = shutil.which('claude'), shutil.which('codex')
STAGES = [
    ('host', ['hostfacts.py'], None, 240),
    ('matrix', ['probe.py'], None, 600),
    ('claude-inet', ['natives.py', 'claude', CLAUDE, 'inet'], CLAUDE, 300),
    ('claude-inet+unix', ['natives.py', 'claude', CLAUDE, 'inet+unix'], CLAUDE, 300),
    ('codex-inet', ['natives.py', 'codex', CODEX, 'inet', 'default'], CODEX, 300),
    ('codex-inet-no-daemon', ['natives.py', 'codex', CODEX, 'inet', 'no-daemon'], CODEX, 300),
    ('codex-inet+unix', ['natives.py', 'codex', CODEX, 'inet+unix', 'default'], CODEX, 300),
    ('codex-inet+unix-outside-daemon', ['natives.py', 'codex', CODEX, 'inet+unix', 'outside-daemon'], CODEX, 300),
    ('compat', ['compat.py'], None, 900),
]


def replacements():
    pairs = [(str(REPO), '<WORKTREE>'), (sys.prefix, '<PYTHON_PREFIX>'), (sys.base_prefix, '<PYTHON_PREFIX>'),
             (os.path.realpath(sys.executable), '<PYTHON>'), (sys.executable, '<PYTHON>')]
    for tool in ('node', 'curl', 'claude', 'codex', 'git', 'ssh', 'strace'):
        path = shutil.which(tool)
        if path:
            pairs += [(os.path.realpath(path), '<%s>' % tool.upper()), (path, '<%s>' % tool.upper())]
    pairs += [(str(Path.home()), '<HOME>'), ('/run/user/%d' % os.getuid(), '<RUNTIME_DIR>'),
              (platform.node(), '<HOSTNAME>'), (getpass.getuser(), '<USER>')]
    return sorted({p for p in pairs if p[0] and len(p[0]) > 3}, key=lambda p: -len(p[0]))


def sanitize(text, roots):
    for root in sorted(roots, key=len, reverse=True):
        text = text.replace(root, '<PROBE_ROOT>')
    for old, new in replacements():
        text = text.replace(old, new)
    return text


RESIDUE = re.compile(r'/home/|/Users/|/run/user/\d|/tmp/quota-nl')


def leftovers(roots):
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / 'cmdline').read_bytes().decode(errors='replace')
        except OSError:
            continue
        if any(root in cmdline for root in roots):
            found.append(int(entry.name))
    return found


def main(selected):
    SCRATCH.mkdir(parents=True, exist_ok=True)
    out = HERE / 'evidence'
    out.mkdir(exist_ok=True)
    manifest_path = out / 'manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() and selected else {'stages': {}}
    roots, status = [], 0
    for name, args, needed, bound in STAGES:
        if selected and name not in selected:
            continue
        if needed is None and args[0] == 'natives.py':
            manifest['stages'][name] = {'exit': None, 'skipped': 'native absent'}
            print(name, 'SKIP native absent', flush=True)
            continue
        command = [sys.executable, '-I', '-B', str(HERE / args[0]), *[a for a in args[1:] if a]]
        started = time.time()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True, stdin=subprocess.DEVNULL, close_fds=True)
        try:
            stdout, _ = process.communicate(timeout=bound)
            code = process.returncode
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, _ = process.communicate()
            code = 124
            stdout += '\nouter timeout; not evidence of behavior'
        stage_roots = re.findall(r'^scratch (\S+)$', stdout, re.M)
        roots.extend(stage_roots)
        with (SCRATCH / 'temp-roots.txt').open('a') as handle:
            for root in stage_roots:
                handle.write(root + '\n')
        left = leftovers(stage_roots)
        for pid in left:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        remaining = [r for r in stage_roots if Path(r).exists()]
        for root in remaining:
            shutil.rmtree(root, ignore_errors=True)
        manifest['stages'][name] = {'exit': code, 'seconds': round(time.time() - started, 1),
                                    'leftover_processes_killed': len(left),
                                    'roots_left_by_stage': len(remaining),
                                    'stdout_tail': sanitize(stdout[-1500:], stage_roots)}
        print(name, 'exit', code, flush=True)
        if code or left or remaining:
            status = 1
    units = subprocess.run(['systemctl', '--user', 'list-units', '--all', '--no-legend', 'quota-nl-*'],
                           capture_output=True, text=True, timeout=20)
    manifest['transient_units_left'] = units.stdout.strip().splitlines()
    for path in sorted(out.glob('*.json')):
        if path.name == 'manifest.json':
            continue
        text = sanitize(path.read_text(), roots)
        path.write_text(text)
        if RESIDUE.search(text):
            print('unsanitized residue in', path.name, RESIDUE.search(text).group(0), flush=True)
            status = 1
    binaries = {}
    for name, path in (('claude', CLAUDE), ('codex', CODEX)):
        if path:
            real = Path(path).resolve()
            version = subprocess.run([str(real), '--version'], capture_output=True, text=True, timeout=20,
                                     env={'PATH': '/usr/bin:/bin', 'HOME': str(SCRATCH)}, cwd=SCRATCH)
            binaries[name] = {'version': version.stdout.strip(),
                              'sha256': hashlib.sha256(real.read_bytes()).hexdigest()}
    manifest.update({
        'host': {'machine': platform.machine(), 'kernel_release': platform.release(),
                 'python': platform.python_version()},
        'natives': binaries,
        'fixture_sha256': {n: hashlib.sha256((HERE / n).read_bytes()).hexdigest() for n in FIXTURES},
        'evidence_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted(out.glob('*.json')) if p.name != 'manifest.json'},
        'runner_exit': status,
    })
    text = sanitize(json.dumps(manifest, indent=2, sort_keys=True), roots)
    manifest_path.write_text(text + '\n')
    archive = SCRATCH / 'runs' / str(time.time_ns())
    archive.mkdir(parents=True)
    for path in out.glob('*.json'):
        shutil.copyfile(path, archive / path.name)
    if manifest['transient_units_left']:
        status = 1
    print('runner exit', status, 'archive', archive.name, flush=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
