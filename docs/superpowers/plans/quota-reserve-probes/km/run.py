"""Linux-side driver for the KM macOS composed probes (research only).

python3 -I -B run.py [--dev] [--only CASE,CASE] init|STAGE...|cleanup

`init` creates one remote temp root with mktemp (or reuses the recorded one) and records it in
the ignored worktree cache (.cache/km/remote.json and temp-roots.txt). Each STAGE copies the KM
probes and the read-only F1 modules they import (nm, qc, qx, pc_continue) to ROOT on the Mac,
runs mac_probe.py there under a remote stage deadline and a local ssh timeout 90 s longer,
copies ROOT/out/STAGE.json back to .cache/km/raw/, and writes sanitized evidence to
evidence/STAGE.json. --dev (and --only) write to .cache/km/dev/ instead, so development runs
never replace committed evidence. `cleanup` checks for leftover processes naming ROOT, then
removes ROOT. The Mac host alias, remote username and home never enter tracked files.
"""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

HERE = Path(os.path.realpath(__file__)).parent
PROBES = HERE.parent
WORKTREE = HERE.parents[4]
CACHE = WORKTREE / '.cache' / 'km'
HOST = os.environ.get('KM_MAC_HOST', 'mac')
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15']
COPY = {'km': ('fixtures.py', 'gate.py', 'monitor.py', 'launcher.py', 'child.py', 'mac_probe.py'),
        'nm': ('gate.py', 'sbpl.py'), 'qc': ('admission.py', 'bearer.py', 'usage.py', 'fake_usage.py'),
        'qx': ('binding.py', 'fixtures.py', 'gate.py'), 'pc_continue': ('control.py',)}
STAGES = {'claude-loss': 600, 'codex-loss': 600, 'claude-resume': 700, 'codex-resume': 700,
          'claude-refresh': 600, 'codex-refresh': 600, 'claude-tools': 600, 'codex-tools': 600,
          'refusals': 900, 'portmatrix': 180, 'proxy': 300}
ROOT_PATTERN = re.compile(r'^/tmp/quota-km-[A-Za-z0-9]{6}$')


def root_ok(path):
    return bool(ROOT_PATTERN.match(path or ''))


def replacements(root, home, user, uid=None):
    """Ordered sanitization map; the realpath form of /tmp is /private/tmp."""
    if not root_ok(root) or not home.startswith('/') or not user:
        raise ValueError('refusing incomplete sanitization inputs')
    local_home = os.path.expanduser('~')
    slug = ('/private' + root).replace('/', '-')  # Claude names project dirs after the cwd
    mapping = {'/private' + root: '<root>', root: '<root>', slug: '-<root-slug>', home: '<home>', user: '<user>',
               local_home: '<local-home>'}
    if uid and uid.isdigit():
        mapping['/claude-%s/' % uid] = '/claude-<uid>/'  # Claude's fixed per-uid temp directory
        mapping['/claude-%s"' % uid] = '/claude-<uid>"'
    return mapping


def load(name, path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sanitize_file(raw, dest, mapping):
    sbpl = load('km_run_sbpl', PROBES / 'nm' / 'sbpl.py')
    data = json.loads(Path(raw).read_text())
    clean = sbpl.sanitize(data, mapping)
    leaked = sbpl.leaks(clean, list(mapping))
    if leaked:
        raise RuntimeError('sanitization left %d sensitive needles' % len(leaked))
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_text(json.dumps(clean, indent=1, sort_keys=True) + '\n')
    return clean


def ssh(command, timeout):
    return subprocess.run(SSH + [HOST, command], capture_output=True, text=True, timeout=timeout)


def state():
    path = CACHE / 'remote.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(data):
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / 'remote.json').write_text(json.dumps(data, indent=2) + '\n')


def parse_discovery(out):
    found = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
    missing = [k for k in ('claude', 'codex', 'home', 'user') if not found.get(k)]
    if missing:
        raise SystemExit('remote discovery incomplete: missing %s' % ', '.join(missing))
    return found


def discover():
    script = ("for t in claude codex; do print -r -- \"$t=$(whence -p $t)\"; done; "
              "print -r -- \"home=$HOME\"; print -r -- \"user=$(id -un)\"; print -r -- \"uid=$(id -u)\"")
    found = parse_discovery(ssh("zsh -lic '%s' 2>/dev/null" % script, 60).stdout)
    return {k: found[k] for k in ('claude', 'codex')}, found['home'], found['user'], found.get('uid')


def init():
    current = state()
    if current.get('root') and not current.get('removed'):
        print('reusing', current['root'])
        return current
    root = ssh('mktemp -d /tmp/quota-km-XXXXXX', 30).stdout.strip()
    if not root_ok(root):
        raise SystemExit('unexpected remote root')
    current = {'root': root, 'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    save_state(current)
    with (CACHE / 'temp-roots.txt').open('a') as handle:
        handle.write('mac:%s created %s (run.py)\n' % (root, current['created']))
    return current


def copy(root):
    ssh('mkdir -p %s' % ' '.join('%s/%s' % (root, d) for d in COPY), 30)
    for directory, names in COPY.items():
        subprocess.run(['scp', '-q', '-o', 'BatchMode=yes'] + [str(PROBES / directory / n) for n in names]
                       + ['%s:%s/%s/' % (HOST, root, directory)], check=True, timeout=90)


def run_stage(stage, dev=False, only=None):
    root = init()['root']
    tools, home, user, uid = discover()
    copy(root)
    ssh('rm -f %s/out/%s.json' % (root, stage), 30)
    prefix = 'KM_ONLY=%s ' % shlex.quote(only) if only else ''
    command = "%s/opt/homebrew/bin/python3 -I -B %s/km/mac_probe.py %s %s %s %d" % (
        prefix, root, root, stage, shlex.quote(json.dumps(tools)), STAGES[stage])
    began = time.monotonic()
    try:
        result = ssh(command, STAGES[stage] + 90)
        code, tail = result.returncode, (result.stdout + result.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        code, tail = 'timeout', ''
    base = CACHE / 'dev' if dev else CACHE
    raw = base / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    fetched = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '%s:%s/out/%s.json' % (HOST, root, stage),
                              str(raw / (stage + '.json'))], timeout=90)
    log = {'stage': stage, 'exit': code, 'seconds': round(time.monotonic() - began, 1), 'only': only,
           'fetched': fetched.returncode == 0, 'tail': tail, 'dev': dev,
           'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    with (CACHE / 'stage-runs.jsonl').open('a') as handle:
        handle.write(json.dumps(log) + '\n')
    if fetched.returncode != 0:
        raise SystemExit('stage %s produced no evidence (exit %s)' % (stage, code))
    evidence = base / 'evidence' if dev else HERE / 'evidence'
    clean = sanitize_file(raw / (stage + '.json'), evidence / (stage + '.json'), replacements(root, home, user, uid))
    print(json.dumps({'stage': stage, 'exit': code, 'completed': clean.get('completed'),
                      'cases': {k: ('error' if 'error' in v else 'ok') for k, v in clean.get('cases', {}).items()}}))


def cleanup():
    current = state()
    root = current.get('root')
    if not root_ok(root) or current.get('removed'):
        print('nothing to clean')
        return
    ps = ssh('/bin/ps -axo pid=,command= | /usr/bin/grep -F %s | /usr/bin/grep -v grep || true' % root, 30)
    if ps.stdout.strip():
        raise SystemExit('leftover processes name the remote root; not removing:\n' + ps.stdout)
    removed = ssh('rm -rf %s && test ! -e %s && echo removed' % (root, root), 60).stdout.strip()
    if removed != 'removed':
        raise SystemExit('remote root not removed')
    current['removed'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    save_state(current)
    with (CACHE / 'temp-roots.txt').open('a') as handle:
        handle.write('mac:%s removed %s (no leftover processes)\n' % (root, current['removed']))
    print('removed')


def main():
    args = sys.argv[1:]
    dev = '--dev' in args
    only = None
    if '--only' in args:
        only = args[args.index('--only') + 1]
        args.remove('--only')
        args.remove(only)
        dev = True
    for arg in args:
        if arg == '--dev':
            continue
        if arg == 'init':
            print(init()['root'])
        elif arg == 'cleanup':
            cleanup()
        elif arg in STAGES:
            run_stage(arg, dev, only)
        else:
            raise SystemExit('unknown stage: %s' % arg)


if __name__ == '__main__':
    main()
