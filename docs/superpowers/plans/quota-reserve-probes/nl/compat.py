"""NL compatibility cost of a gate-only boundary; writes evidence/compat.json.

Ordinary developer workflows run unconfined (L0), under L2a and under L2b
against loopback fixtures only: a dumb-HTTP git repository and a package file
on an alternate port, and an SSH-banner listener. Local-only workflows show
what the boundary breaks even without remote access. No real network.

python3 -I -B compat.py            (driver)
python3 -I -B compat.py --actor SPEC   (inside a boundary)
"""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import functools
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
LOCAL_TCP = ('import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(); '
             'c=socket.create_connection(s.getsockname(),2); print("local tcp ok")')
LOCAL_UNIX = ('import socket,os,tempfile; p=os.path.join(tempfile.mkdtemp(),"s"); s=socket.socket(1); '
              's.bind(p); s.listen(); c=socket.socket(1); c.connect(p); print("local unix ok")')
POOL = ('import multiprocessing as m; print(m.get_start_method(), m.Pool(2).map(abs,[-1,-2]))')
FORK_POOL = ('import multiprocessing as m; print(m.get_context("fork").Pool(2).map(abs,[-1,-2]))')
PAIR = 'import socket; a,b=socket.socketpair(socket.AF_UNIX,socket.SOCK_%s); a.send(b"x"); print("pair",b.recv(1))'
ASYNCIO = 'import asyncio; asyncio.run(asyncio.sleep(0)); print("asyncio ok")'
NODE_FETCH = ('fetch(process.argv[1]).then(r=>r.text()).then(t=>console.log("fetched",t.length))'
              '.catch(e=>{console.log(String(e.cause&&e.cause.code||e));process.exit(1)})')


def actor(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    work = Path(tempfile.mkdtemp(prefix='compat-', dir=spec['root']))
    env = {'PATH': '/usr/bin:/bin', 'HOME': spec['home'], 'GIT_CONFIG_NOSYSTEM': '1', 'LANG': 'C.UTF-8',
           'GIT_TERMINAL_PROMPT': '0', 'TMPDIR': str(work)}
    alt = 'http://127.0.0.1:%d' % spec['alt_port']
    ssh = ('ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null '
           '-o ConnectTimeout=3')
    py = spec['python']
    cases = [
        ('git_local_commit', ['sh', '-c', 'git init -q r && cd r && git -c user.name=f -c user.email=f@x '
                              'commit -q --allow-empty -m x && echo committed']),
        ('dns_localhost', [py, '-I', '-c', 'import socket; print(socket.getaddrinfo("localhost", 80)[0][4])']),
        ('git_http_clone_alt', ['git', 'clone', '-q', alt + '/repo.git', 'clone-http']),
        ('curl_download_alt', [spec['curl'], '-fsS', '--noproxy', '*', '-m', '5', '-o', 'pkg.tar.gz',
                               alt + '/pkg-1.0.tar.gz']),
        ('python_urllib_alt', [py, '-I', '-c', 'import urllib.request,sys; print(len(urllib.request.urlopen('
                               'sys.argv[1], timeout=5).read()))', alt + '/pkg-1.0.tar.gz']),
        ('node_fetch_alt', [spec['node'], '-e', NODE_FETCH, alt + '/pkg-1.0.tar.gz']),
        ('ssh_alt', ssh.split() + ['-p', str(spec['ssh_port']), 'fixture@127.0.0.1', 'true']),
        ('git_ssh_clone_alt', ['git', '-c', 'core.sshCommand=' + ssh, 'clone', '-q',
                               'ssh://fixture@127.0.0.1:%d/repo.git' % spec['ssh_port'], 'clone-ssh']),
        ('local_tcp_server', [py, '-I', '-c', LOCAL_TCP]),
        ('local_unix_server', [py, '-I', '-c', LOCAL_UNIX]),
        ('multiprocessing_default_pool', [py, '-I', '-c', POOL]),
        ('multiprocessing_fork_pool', [py, '-I', '-c', FORK_POOL]),
        ('unix_socketpair_stream', [py, '-I', '-c', PAIR % 'STREAM']),
        ('unix_socketpair_dgram', [py, '-I', '-c', PAIR % 'DGRAM']),
        ('asyncio_event_loop', [py, '-I', '-c', ASYNCIO]),
    ]
    for name, command in cases:
        try:
            result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=20,
                                    stdin=subprocess.DEVNULL)
            code, out = result.returncode, (result.stdout + result.stderr).strip()
        except subprocess.TimeoutExpired:
            code, out = 124, 'timeout'
        except FileNotFoundError:
            code, out = None, 'absent'
        print(json.dumps({'case': name, 'exit': code, 'output': out[-240:]}), flush=True)
    return 0


def main():
    root = Path(tempfile.mkdtemp(prefix='quota-nl-compat-'))
    print('scratch', root, flush=True)
    result, seen = {'runs': {}}, []
    servers = []
    try:
        site = root / 'site'
        site.mkdir()
        (root / 'home').mkdir()
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'), 'GIT_CONFIG_NOSYSTEM': '1'}
        source = root / 'source'
        subprocess.run(['git', 'init', '-q', str(source)], check=True, env=env)
        (source / 'README').write_text('fixture\n')
        subprocess.run(['git', '-C', str(source), 'add', 'README'], check=True, env=env)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=f', '-c', 'user.email=f@x', 'commit', '-q',
                        '-m', 'fixture'], check=True, env=env)
        subprocess.run(['git', 'clone', '-q', '--bare', str(source), str(site / 'repo.git')], check=True, env=env)
        subprocess.run(['git', '-C', str(site / 'repo.git'), 'update-server-info'], check=True, env=env)
        (site / 'pkg-1.0.tar.gz').write_bytes(os.urandom(4096))

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args):
                seen.append({'event': 'alt_http', 'path': self.path.split('?')[0]})
        http = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(site)))
        servers.append(http)
        threading.Thread(target=http.serve_forever, daemon=True).start()
        banner = socket.socket()
        banner.bind(('127.0.0.1', 0))
        banner.listen(8)
        servers.append(banner)

        def serve_banner():
            while True:
                try:
                    peer, _ = banner.accept()
                except OSError:
                    return
                seen.append({'event': 'alt_ssh'})
                with peer:
                    peer.sendall(b'SSH-2.0-NLfixture\r\n')
                    time.sleep(.2)
        threading.Thread(target=serve_banner, daemon=True).start()
        gate = socket.socket()
        gate.bind(('127.0.0.1', 0))
        gate.listen(1)
        servers.append(gate)
        spec = {'root': str(root), 'home': str(root / 'home'), 'alt_port': http.server_port,
                'ssh_port': banner.getsockname()[1], 'python': sys.executable,
                'curl': shutil.which('curl'), 'node': shutil.which('node')}
        (root / 'spec.json').write_text(json.dumps(spec))
        me = [sys.executable, '-I', '-B', str(HERE / 'compat.py'), '--actor', str(root / 'spec.json')]
        for layer, mode in (('L0', None), ('L2a', 'inet'), ('L2b', 'inet+unix')):
            before = len(seen)
            command = me if mode is None else [sys.executable, '-I', '-B', str(HERE / 'boundary.py'),
                                               '--gate-port', str(gate.getsockname()[1]), '--seccomp', mode,
                                               '--', *me]
            try:
                done = subprocess.run(command, capture_output=True, text=True, timeout=300,
                                      start_new_session=True, stdin=subprocess.DEVNULL)
                code, out = done.returncode, done.stdout
            except subprocess.TimeoutExpired as error:
                code, out = 124, (error.stdout or b'').decode(errors='replace')
            result['runs'][layer] = {'exit': code, 'cases': [json.loads(x) for x in out.splitlines()
                                                             if x.startswith('{')],
                                     'fixture_hits': seen[before:]}
    finally:
        for server in servers:
            try:
                server.shutdown() if hasattr(server, 'shutdown') and not isinstance(server, socket.socket) \
                    else server.close()
            except OSError:
                pass
        text = json.dumps(result, indent=2, sort_keys=True)
        for old, new in ((str(root), '<PROBE_ROOT>'), (str(Path.home()), '<HOME>'),
                         (sys.executable, '<PYTHON>')):
            text = text.replace(old, new)
        (HERE / 'evidence').mkdir(exist_ok=True)
        (HERE / 'evidence' / 'compat.json').write_text(text + '\n')
        shutil.rmtree(root, ignore_errors=True)
        print('removed', root, flush=True)
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--actor':
        raise SystemExit(actor(sys.argv[2]))
    raise SystemExit(main())
