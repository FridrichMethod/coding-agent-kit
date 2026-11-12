"""Run only two fake-account reload cases under the unchanged PC network guard."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]


def main():
    candidate = shutil.which('claude')
    if not candidate:
        print('SKIP: native Claude absent; no capability evidence')
        return 0
    binary = Path(candidate).resolve()
    archive = REPO / '.cache/pr' / ('reload-' + str(time.time_ns()))
    archive.mkdir(parents=True)
    guard = HERE.parent / 'pc/network_guard.py'
    check = HERE.parent / 'pc/guard_check.py'
    manifest = {'binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(), 'stages': [],
                'sources': [{'file': str(p.relative_to(HERE.parent)),
                             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                            for p in (Path(__file__), HERE / 'reload_probe.py', guard, check)]}
    code = 1
    try:
        stages = [('guard', [str(check)], 10)]
        for mode in ('environment', 'pinned'):
            root = archive / mode
            root.mkdir(mode=0o700)
            stages.append((mode, [str(HERE / 'reload_probe.py'), str(binary), str(root), mode], 40))
        for mode, args, bound in stages:
            try:
                result = subprocess.run([sys.executable, '-I', '-B', str(guard),
                    sys.executable, '-I', '-B', *args], capture_output=True, text=True,
                    timeout=bound, start_new_session=True, close_fds=True)
                status, out, err = result.returncode, result.stdout, result.stderr
            except subprocess.TimeoutExpired as failure:
                status = 124
                out = (failure.stdout or b'').decode(errors='replace')
                err = (failure.stderr or b'').decode(errors='replace')
            for raw in (str(REPO), str(binary), str(archive)):
                out, err = out.replace(raw, '<LOCAL>'), err.replace(raw, '<LOCAL>')
            manifest['stages'].append({'mode': mode, 'exit': status, 'stdout': out, 'stderr': err})
            print(mode, status, flush=True)
            if status:
                code = status
                return status
        code = 0
        return code
    finally:
        manifest['exit'] = code
        (archive / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        if code == 0:
            capture = dict(manifest)
            capture['cases'] = {mode: json.loads((archive / mode / 'events.json').read_text())
                                for mode in ('environment', 'pinned')}
            (HERE / 'reload-evidence.json').write_text(json.dumps(capture, indent=2) + '\n')
        print('archive', archive)


if __name__ == '__main__':
    raise SystemExit(main())
