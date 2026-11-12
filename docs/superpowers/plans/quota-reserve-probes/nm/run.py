"""Linux-side driver for the NM macOS probes (research only).

python3 -I -B run.py init|STAGE...|cleanup

`init` creates one remote temp root with mktemp and records it in the ignored
worktree cache (.cache/nm/remote.json and temp-roots.txt), so a stopped run
resumes with the same root. Each STAGE copies this directory to ROOT/nm on
the Mac, runs mac_probe.py there under a hard timeout, copies ROOT/out back
to .cache/nm/raw/, and writes sanitized evidence to evidence/STAGE.json.
`cleanup` checks for leftover processes naming ROOT, then removes ROOT.
The Mac host alias, remote username and home never enter tracked files.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

HERE = Path(os.path.realpath(__file__)).parent
WORKTREE = HERE.parents[4]
CACHE = WORKTREE / '.cache' / 'nm'
HOST = os.environ.get('NM_MAC_HOST', 'mac')
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15']
FILES = ('sbpl.py', 'client.py', 'client.mjs', 'gate.py', 'mac_probe.py')
STAGES = {'boundary': 240, 'ipc': 180, 'fds': 120, 'keychain': 150, 'natives': 600,
          'natives-extra': 400, 'nested': 400, 'compat': 240}
ROOT_PATTERN = re.compile(r'^/tmp/quota-nm-[A-Za-z0-9]{6}$')


def root_ok(path):
    return bool(ROOT_PATTERN.match(path or ''))


def replacements(root, home, user):
    """Ordered sanitization map; the realpath form of /tmp is /private/tmp."""
    if not root_ok(root) or not home.startswith('/') or not user:
        raise ValueError('refusing incomplete sanitization inputs')
    local_home = os.path.expanduser('~')
    return {'/private' + root: '<root>', root: '<root>', home: '<home>', user: '<user>',
            local_home: '<local-home>'}


def sanitize_file(raw, dest, mapping, sbpl):
    data = json.loads(Path(raw).read_text())
    clean = sbpl.sanitize(data, mapping)
    leaked = sbpl.leaks(clean, list(mapping))
    if leaked:
        raise RuntimeError('sanitization left %d sensitive needles' % len(leaked))
    Path(dest).write_text(json.dumps(clean, indent=2, sort_keys=True) + '\n')
    return clean


def ssh(command, timeout):
    return subprocess.run(SSH + [HOST, command], capture_output=True, text=True, timeout=timeout)


def load_sbpl():
    import importlib.util
    spec = importlib.util.spec_from_file_location('nm_sbpl', HERE / 'sbpl.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def state():
    path = CACHE / 'remote.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(data):
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / 'remote.json').write_text(json.dumps(data, indent=2) + '\n')


def discover():
    script = ("for t in claude codex node npm; do print -r -- \"$t=$(whence -p $t)\"; done; "
              "print -r -- \"home=$HOME\"; print -r -- \"user=$(id -un)\"")
    out = ssh("zsh -lic '%s' 2>/dev/null" % script, 60).stdout
    found = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
    tools = {k: found[k] for k in ('claude', 'codex', 'node') if found.get(k)}
    npm = found.get('npm')
    if npm:
        real = ssh('/opt/homebrew/bin/python3 -I -c "import os,sys;print(os.path.realpath(sys.argv[1]))" %s'
                   % npm, 30).stdout.strip()
        tools['npm_cli'] = real
    tools['git'] = '/usr/bin/git'
    return tools, found['home'], found['user']


def init():
    current = state()
    if current.get('root') and not current.get('removed'):
        print('reusing', current['root'])
        return current
    root = ssh('mktemp -d /tmp/quota-nm-XXXXXX', 30).stdout.strip()
    if not root_ok(root):
        raise SystemExit('unexpected remote root')
    current = {'root': root, 'created': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    save_state(current)
    with (CACHE / 'temp-roots.txt').open('a') as handle:
        handle.write('mac:%s created %s (run.py)\n' % (root, current['created']))
    return current


def run_stage(stage):
    current = init()
    root = current['root']
    tools, home, user = discover()
    subprocess.run(['scp', '-q', '-o', 'BatchMode=yes'] + [str(HERE / f) for f in FILES]
                   + ['%s:%s/' % (HOST, root)], check=True, timeout=60)
    ssh('mkdir -p %s/nm && cd %s && mv %s nm/' % (root, root, ' '.join(FILES)), 30)
    command = "/opt/homebrew/bin/python3 -I -B %s/nm/mac_probe.py %s %s '%s'" % (
        root, root, stage, json.dumps(tools))
    began = time.monotonic()
    try:
        result = ssh(command, STAGES[stage] + 60)
        code, tail = result.returncode, (result.stdout + result.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        code, tail = 'timeout', ''
    raw = CACHE / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    fetched = subprocess.run(['scp', '-q', '-o', 'BatchMode=yes', '%s:%s/out/%s.json' % (HOST, root, stage),
                              str(raw / (stage + '.json'))], timeout=60)
    log = {'stage': stage, 'exit': code, 'seconds': round(time.monotonic() - began, 1),
           'fetched': fetched.returncode == 0, 'tail': tail}
    with (CACHE / 'stage-runs.jsonl').open('a') as handle:
        handle.write(json.dumps(log) + '\n')
    if fetched.returncode != 0:
        raise SystemExit('stage %s produced no evidence (exit %s)' % (stage, code))
    (HERE / 'evidence').mkdir(exist_ok=True)
    clean = sanitize_file(raw / (stage + '.json'), HERE / 'evidence' / (stage + '.json'),
                          replacements(root, home, user), load_sbpl())
    print(json.dumps({'stage': stage, 'exit': code, 'completed': clean.get('completed')}))


def cleanup():
    current = state()
    root = current.get('root')
    if not root_ok(root) or current.get('removed'):
        print('nothing to clean')
        return
    ps = ssh('/bin/ps -axo pid=,command= | /usr/bin/grep -F %s | /usr/bin/grep -v grep || true' % root, 30)
    if ps.stdout.strip():
        raise SystemExit('leftover processes name the remote root; not removing:\n' + ps.stdout)
    subprocess.run(['scp', '-q', '-r', '-o', 'BatchMode=yes', '%s:%s/out' % (HOST, root),
                    str(CACHE / 'final-out')], timeout=120)
    removed = ssh('rm -rf %s && test ! -e %s && echo removed' % (root, root), 60).stdout.strip()
    if removed != 'removed':
        raise SystemExit('remote root not removed')
    current['removed'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    save_state(current)
    with (CACHE / 'temp-roots.txt').open('a') as handle:
        handle.write('mac:%s removed %s (no leftover processes)\n' % (root, current['removed']))
    print('removed')


def main():
    for arg in sys.argv[1:]:
        if arg == 'init':
            print(init()['root'])
        elif arg == 'cleanup':
            cleanup()
        elif arg in STAGES:
            run_stage(arg)
        else:
            raise SystemExit('unknown stage: %s' % arg)


if __name__ == '__main__':
    main()
