"""Bounded optional native experiment; independent inherited network denial."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--shared-control-dir', type=Path, default=HERE.parent / 'pc_continue')
    parser.add_argument('modes', nargs='*')
    args = parser.parse_args()
    candidate = shutil.which('claude')
    if not candidate:
        print('SKIP: Claude absent; no capability evidence')
        return 0
    binary = Path(candidate).resolve()
    cache = REPO / '.cache/claude_control'
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / str(time.time_ns())
    archive.mkdir()
    guard = HERE.parent / 'pc/network_guard.py'
    modes = args.modes or ['bootstrap', 'oauth', 'fullauth', 'delegation', 'eof', 'monitor_death', 'lease', 'gate_death', 'continuation']
    stages = [('guard', [str(HERE.parent / 'pc/guard_check.py')], 10)]
    stages += [(mode, [str(HERE / 'probe.py'), str(binary), mode, str(args.shared_control_dir)], 55) for mode in modes]
    result = {'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'stages': []}
    code = 1
    roots = []
    try:
        for mode, args, bound in stages:
            try:
                child = subprocess.run([sys.executable, '-I', '-B', str(guard), sys.executable,
                    '-I', '-B', *args], capture_output=True, text=True, timeout=bound,
                    start_new_session=True, close_fds=True)
                stage_code, stdout, stderr = child.returncode, child.stdout, child.stderr
            except subprocess.TimeoutExpired as error:
                stage_code = 124
                stdout = (error.stdout or b'').decode(errors='replace')
                stderr = (error.stderr or b'').decode(errors='replace')
            roots += re.findall(r'^scratch (.+)$', stdout, re.M)
            for raw in [str(REPO), str(binary), *roots]:
                stdout, stderr = stdout.replace(raw, '<LOCAL>'), stderr.replace(raw, '<LOCAL>')
            result['stages'].append({'mode': mode, 'exit': stage_code, 'stdout': stdout, 'stderr': stderr})
            print(mode, stage_code, flush=True)
            if stage_code:
                code = stage_code
                return stage_code
        code = 0
        return code
    finally:
        result['sources'] = {str(p.relative_to(HERE.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in [HERE / 'probe.py', HERE / 'run.py', HERE / 'gate.py', guard,
                                       HERE.parent / 'pc/guard_check.py', HERE.parent / 'pc_loss/gate.py']}
        result['evidence_sha256'] = [{'file': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                                    for p in sorted((HERE / 'evidence').glob('case-*.json'))]
        (HERE / 'evidence/manifest.json').write_text(json.dumps(result, indent=2) + '\n')
        for p in (HERE / 'evidence').glob('*.json'):
            shutil.copyfile(p, archive / p.name)
        for root in roots:
            root = Path(root)
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


if __name__ == '__main__':
    raise SystemExit(main())
