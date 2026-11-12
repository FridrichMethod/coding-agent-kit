"""Bounded guarded PA1T runner with durable private archives."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]


def main():
    candidate = shutil.which('claude')
    if not candidate:
        print('SKIP: Claude absent; no capability evidence')
        return 0
    binary = Path(candidate).resolve()
    archive = REPO / '.cache/claude_tools' / str(time.time_ns())
    archive.mkdir(parents=True)
    (HERE / 'evidence').mkdir(exist_ok=True)
    guard = HERE.parent / 'pc/network_guard.py'
    # Adopt descendants if the outer guard must be stopped at its hard bound.
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0):
        raise RuntimeError('subreaper setup failed')
    stages = [('guard', [str(HERE.parent / 'pc/guard_check.py')], 10)]
    stages += [(mode, [str(HERE / 'probe.py'), str(binary), mode], 45) for mode in ('gate', 'interrupt')]
    manifest = {'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'stages': []}
    roots, code = [], 1
    try:
        for mode, args, bound in stages:
            process = subprocess.Popen([sys.executable, '-I', '-B', str(guard), sys.executable,
                '-I', '-B', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True, close_fds=True)
            try:
                stdout, stderr = process.communicate(timeout=bound)
                stage_code = process.returncode
            except subprocess.TimeoutExpired:
                # EXITKILL on unchanged PC guard terminates all traced descendants.
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=5)
                stage_code = 124
            # Also reap adopted children after an internal guard failure.
            end = time.monotonic() + 5
            while time.monotonic() < end:
                try:
                    pid, _ = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if not pid:
                    time.sleep(.02)
            else:
                raise RuntimeError('guard descendants not reaped')
            roots += re.findall(r'^scratch (.+)$', stdout, re.M)
            (archive / (mode + '.stdout')).write_text(stdout)
            (archive / (mode + '.stderr')).write_text(stderr)
            for raw in [str(REPO), str(binary), *roots]:
                stdout, stderr = stdout.replace(raw, '<LOCAL>'), stderr.replace(raw, '<LOCAL>')
            manifest['stages'].append({'mode': mode, 'exit': stage_code, 'stdout': stdout, 'stderr': stderr})
            print(mode, stage_code, flush=True)
            if stage_code:
                code = stage_code
                return code
        code = 0
        return code
    finally:
        sources = [HERE / 'run.py', HERE / 'probe.py', guard, HERE.parent / 'pc/guard_check.py',
                   HERE.parent / 'claude_control/gate.py', HERE.parent / 'pc_loss/gate.py']
        manifest['sources'] = {str(p.relative_to(HERE.parent)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        for path in sources:
            target = archive / 'sources' / path.relative_to(HERE.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        manifest['evidence_sha256'] = [{'file': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                                       for p in sorted((HERE / 'evidence').glob('*.json')) if p.name != 'manifest.json']
        (HERE / 'evidence/manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        for path in (HERE / 'evidence').glob('*.json'):
            shutil.copyfile(path, archive / path.name)
        for value in roots:
            root = Path(value)
            destination = archive / root.name
            destination.mkdir()
            inventory = []
            for path in sorted(root.rglob('*')):
                info = path.lstat()
                relative = path.relative_to(root)
                inventory.append({'path': str(relative), 'mode': oct(info.st_mode), 'size': info.st_size})
                target = destination / relative
                if stat.S_ISDIR(info.st_mode):
                    target.mkdir(exist_ok=True)
                elif stat.S_ISREG(info.st_mode):
                    shutil.copyfile(path, target)
            (destination / 'inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
        (archive / 'roots.json').write_text(json.dumps(roots, indent=2) + '\n')
        (archive / 'run.exit').write_text(str(code) + '\n')
        print('archive', archive, flush=True)


if __name__ == '__main__':
    raise SystemExit(main())
