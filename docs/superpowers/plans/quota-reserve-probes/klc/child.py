"""KLC in-boundary actor, started by the native's own tool or by the boundary launcher.

Every row is one JSON line with a host-monotonic time, so the harness can
order it against the gate latch. Fixture ground truth (gate decisions and the
fake upstream record) decides results, never this report.

Modes (python3 -I -B child.py MODE --spec SPEC.json [--name NAME] [--out FILE]):
  sdk        SDK-like client: reads the store bearer the way an SDK child of the
             native would, POSTs Messages to the gate every interval; first a
             direct attempt to the upstream port. --detach forks a setsid
             grandchild that does the same.
  ticks      local side effect: append a tick line every interval (test-like process).
  connects   CONNECT through the gate to the refresh host and to api.anthropic.com.
  bind       try to bind and listen on the gate port.
  workflows  git clone over loopback HTTP and a pip-style index download, direct,
             plus one https fetch through HTTPS_PROXY.
  ttywrite   write a fake confirmation key and text to /dev/tty (native output side).
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.request


def code(error):
    return errno.errorcode.get(getattr(error, 'errno', None) or 0, type(error).__name__)


class Out:
    def __init__(self, path, name):
        self.path, self.name = path, name

    def __call__(self, attempt, **fields):
        row = {'actor': self.name, 'attempt': attempt, 'pid': os.getpid(), 't': time.monotonic(), **fields}
        with open(self.path, 'a') as handle:
            handle.write(json.dumps(row) + '\n')


def bearer(config_dir):
    with open(os.path.join(config_dir, '.credentials.json')) as handle:
        return json.load(handle)['claudeAiOauth']['accessToken']


def post(port, token, text, timeout=3):
    body = json.dumps({'model': 'claude-sonnet-4-6', 'max_tokens': 16, 'stream': True,
                       'messages': [{'role': 'user', 'content': text}]}).encode()
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as sock:
        sock.sendall(('POST /v1/messages?beta=true HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n'
                      'anthropic-version: 2023-06-01\r\nanthropic-beta: oauth-2025-04-20\r\n'
                      'Authorization: Bearer %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                      % (token, len(body))).encode() + body)
        data = b''
        end = time.monotonic() + timeout
        while b'\r\n' not in data and time.monotonic() < end:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    status = data.split(b' ', 2)[1:2]
    return int(status[0]) if status and status[0].isdigit() else None


def direct(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2) as sock:
            sock.sendall(b'GET / HTTP/1.0\r\n\r\n')
            return 'reached'
    except OSError as error:
        return code(error)


def sdk(spec, out, name, detach):
    if detach:
        argv = [sys.executable, '-I', '-B', __file__, 'sdk', '--spec', spec['path'], '--name', name + '-grandchild',
                '--out', out.path]
        subprocess.Popen(['setsid', '-f', *argv], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
        out('spawned_grandchild')
    out('direct_upstream', result=direct(spec['upstream_port']))
    deadline = time.monotonic() + spec.get('sdk_duration', 20)
    seq = 0
    while time.monotonic() < deadline:
        seq += 1
        try:
            token = bearer(spec['config_dir'])
            label = spec['labels'].get(token, 'other')
        except (OSError, ValueError, KeyError):
            token, label = None, 'unreadable'
        sent = time.monotonic()
        try:
            status = post(spec['gate_port'], token, 'KLC sdk %s %d' % (name, seq)) if token else None
            out('model_request', seq=seq, sent=sent, status=status, bearer=label)
        except OSError as error:
            out('model_request', seq=seq, sent=sent, status=None, error=code(error), bearer=label)
        if seq % 5 == 0:
            out('direct_upstream', result=direct(spec['upstream_port']))
        time.sleep(spec.get('sdk_interval', 0.4))
    out('done')


def ticks(spec, out):
    deadline = time.monotonic() + spec.get('tick_duration', 10)
    path = Path(spec['tick_file'])
    n = 0
    while time.monotonic() < deadline:
        n += 1
        with path.open('a') as handle:
            handle.write('%d %.4f\n' % (n, time.monotonic()))
        if n % 4 == 0:
            try:
                status = post(spec['gate_port'], bearer(spec['config_dir']), 'KLC sdk ticks %d' % n, timeout=2)
            except OSError as error:
                status = code(error)
            out('tick_network', n=n, gate_status=status, direct=direct(spec['upstream_port']))
        time.sleep(spec.get('tick_interval', 0.5))
    out('done', ticks=n)


def connect_status(port, target):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=3) as sock:
            sock.sendall(('CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' % (target, target)).encode())
            data = sock.recv(256)
        part = data.split(b' ', 2)[1:2]
        return int(part[0]) if part and part[0].isdigit() else 'no_status'
    except OSError as error:
        return code(error)


def connects(spec, out):
    for target in ('platform.claude.com:443', 'api.anthropic.com:443', 'platform.claude.com:80'):
        out('connect', target=target, status=connect_status(spec['gate_port'], target))


def bind(spec, out):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', spec['gate_port']))
            sock.listen(4)
            out('bind_gate_port', result='bound')
            sock.settimeout(spec.get('bind_hold', 3))
            try:
                peer, _ = sock.accept()
                data = peer.recv(256)
                out('bind_accepted', request_line=data.split(b'\r\n', 1)[0].decode('latin-1')[:40])
                peer.close()
            except OSError as error:
                out('bind_accepted', result=code(error))
    except OSError as error:
        out('bind_gate_port', result=code(error))


def workflows(spec, out, phase):
    work = Path(spec['work_dir']) / phase
    work.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(['git', 'clone', '-q', 'http://127.0.0.1:%d/repo.git' % spec['git_port'], str(work / 'repo')],
                           capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    tail = (clone.stderr.strip().splitlines() or [''])[-1]
    out('git_clone', phase=phase, exit=clone.returncode, stderr=re.sub(r'\d{4,5}', 'PORT', tail)[:160])
    try:
        page = urllib.request.urlopen('http://127.0.0.1:%d/simple/klc-fixture/' % spec['index_port'], timeout=5).read()
        link = re.search(rb'href="([^"#]+)#sha256=([0-9a-f]{64})"', page)
        data = urllib.request.urlopen('http://127.0.0.1:%d%s' % (spec['index_port'], link.group(1).decode()),
                                      timeout=5).read()
        out('index_download', phase=phase, result='ok', sha256_ok=hashlib.sha256(data).hexdigest() == link.group(2).decode())
    except OSError as error:
        reason = getattr(error, 'reason', error)
        out('index_download', phase=phase, result=code(reason) if isinstance(reason, OSError) else type(error).__name__)
    proxied = subprocess.run(['curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '5',
                              'https://pypi.fixture.invalid/simple/'], capture_output=True, text=True, timeout=15,
                             stdin=subprocess.DEVNULL)
    out('https_via_proxy', phase=phase, exit=proxied.returncode, stderr=proxied.stderr.strip()[-120:])


def ttywrite(spec, out):
    try:
        fd = os.open('/dev/tty', os.O_WRONLY | os.O_NOCTTY)
    except OSError as error:
        out('ttywrite', result=code(error))
        return
    try:
        for guess in spec.get('tty_guesses', ['00000000']):
            os.write(fd, b'\x1b[24~' + guess.encode() + b'\r')
            out('ttywrite', result='written', length=len(guess) + 6)
            time.sleep(spec.get('tty_interval', 0.5))
    finally:
        os.close(fd)


def main(argv):
    mode = argv[0]
    options = dict(zip(argv[1::2], argv[2::2]))
    spec = json.loads(Path(options['--spec']).read_text())
    spec['path'] = options['--spec']
    name = options.get('--name', mode)
    out = Out(options.get('--out', spec['out_dir'] + '/' + name + '.jsonl'), name)
    out('start', mode=mode)
    if mode == 'sdk':
        sdk(spec, out, name, '--detach' in options)
    elif mode == 'ticks':
        ticks(spec, out)
    elif mode == 'connects':
        connects(spec, out)
    elif mode == 'bind':
        bind(spec, out)
    elif mode == 'workflows':
        workflows(spec, out, options.get('--phase', 'run'))
    elif mode == 'ttywrite':
        ttywrite(spec, out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
