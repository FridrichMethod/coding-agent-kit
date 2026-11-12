"""KLX in-boundary actors, started by the native's own tool call (or by the harness inside the
same boundary). Each prints or appends JSON rows stamped with the host monotonic clock, which
the gate and the fake backend share. Fabricated credentials only; loopback targets only.

python3 -I -B actors.py --spec SPEC.json ACTOR [options]
Actors: sdk, grandchild, testlike, workflow, loop, bind, tunnel, direct, fswrite.
"""
import argparse
import errno
import http.client
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import urllib.request


def code(error):
    if isinstance(error, socket.timeout):
        return 'timeout'
    number = getattr(error, 'errno', None)
    if number is None and isinstance(getattr(error, 'reason', None), OSError):
        number = error.reason.errno
    return errno.errorcode.get(number or 0, type(error).__name__)


class Out:
    def __init__(self, path, actor):
        self.path, self.actor = path, actor

    def __call__(self, attempt, result, **data):
        row = {'at': time.monotonic(), 'actor': self.actor, 'attempt': attempt, 'result': result,
               'pid': os.getpid(), **data}
        with open(self.path, 'a') as handle:
            handle.write(json.dumps(row) + '\n')
        return row


def credential(codex_home):
    data = json.loads((Path(codex_home) / 'auth.json').read_text())
    return data['tokens']['access_token'], data['tokens']['account_id']


def model_request(spec, marker, out, attempt, bearer=None, account=None, port=None):
    """An SDK-like Responses POST to the gate; reports how the response ended."""
    if bearer is None:
        bearer, account = credential(spec['codex_home'])
    body = json.dumps({'model': spec.get('model', 'gpt-5.4'), 'stream': True, 'instructions': 'klx',
                       'input': [{'type': 'message', 'role': 'user',
                                  'content': [{'type': 'input_text', 'text': marker + ' sdk child'}]}]})
    started = time.monotonic()
    try:
        connection = http.client.HTTPConnection('127.0.0.1', port or spec['gate_port'], timeout=30)
        connection.request('POST', '/backend-api/codex/responses', body=body,
                           headers={'Authorization': 'Bearer ' + bearer, 'ChatGPT-Account-ID': account,
                                    'Content-Type': 'application/json', 'Accept': 'text/event-stream'})
        response = connection.getresponse()
        received = 0
        try:
            while True:
                chunk = response.read1(65536)
                if not chunk:
                    break
                received += len(chunk)
            ended = 'eof'
        except (OSError, http.client.HTTPException) as error:
            ended = code(error)
        return out(attempt, response.status, marker=marker, bytes=received, ended=ended,
                   seconds=round(time.monotonic() - started, 3))
    except (OSError, http.client.HTTPException) as error:
        return out(attempt, code(error), marker=marker, seconds=round(time.monotonic() - started, 3))


def tcp(address, out, attempt, payload=b'klx\n'):
    try:
        with socket.create_connection(address, timeout=2) as sock:
            sock.sendall(payload)
            return out(attempt, 'connected')
    except OSError as error:
        return out(attempt, code(error))


def udp(address, out, attempt):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b'klx', address)
            return out(attempt, 'sent')
    except OSError as error:
        return out(attempt, code(error))


def workflow(spec, out, tag):
    """A git clone over dumb HTTP and a pip-style simple-index download, both from loopback fixtures."""
    target = Path(spec['workspace']) / ('clone-' + tag)
    env = {'PATH': '/usr/bin:/bin', 'HOME': spec['home'], 'GIT_TERMINAL_PROMPT': '0',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_HTTP_LOW_SPEED_LIMIT': '1', 'GIT_HTTP_LOW_SPEED_TIME': '5'}
    try:
        result = subprocess.run(['git', 'clone', '--quiet', 'http://127.0.0.1:%d/repo.git' % spec['static_port'],
                                 str(target)], env=env, capture_output=True, text=True, timeout=20,
                                stdin=subprocess.DEVNULL)
        out('git_clone_' + tag, result.returncode, stderr=result.stderr.strip()[-200:],
            cloned=(target / 'README').exists())
    except subprocess.TimeoutExpired:
        out('git_clone_' + tag, 'timeout')
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/simple/klx-fixture/' % spec['static_port'],
                                    timeout=5) as response:
            out('pip_index_' + tag, response.status, bytes=len(response.read()))
    except OSError as error:
        out('pip_index_' + tag, code(error))
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/backend-api/codex/models' % spec['gate_port'],
                                    timeout=5) as response:
            out('gate_port_http_' + tag, response.status)
    except urllib.error.HTTPError as error:
        out('gate_port_http_' + tag, error.code)
    except OSError as error:
        out('gate_port_http_' + tag, code(error))


def wait_for(path, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if Path(path).exists():
            return True
        time.sleep(.05)
    return False


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument('--spec', required=True)
    parser.add_argument('actor')
    parser.add_argument('--marker', default='KLX-HOLD-child')
    parser.add_argument('--tag', default='run')
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--wait', default=None)
    parser.add_argument('--out', default=None)
    parser.add_argument('--reread', action='store_true')
    options = parser.parse_args(argv)
    spec = json.loads(Path(options.spec).read_text())
    out = Out(options.out or str(Path(spec['outdir']) / (options.actor + '.jsonl')), options.actor)
    out('started', 'ok', ppid=os.getppid(), sid=os.getsid(0))
    if options.actor == 'sdk':
        # Keep the bearer read at start: a later request reuses it, like an SDK client would.
        bearer, account = credential(spec['codex_home'])
        model_request(spec, options.marker, out, 'model_before_latch', bearer, account)
        if options.wait and wait_for(options.wait, options.seconds):
            if not options.reread:
                model_request(spec, options.marker + '-again', out, 'model_after_signal', bearer, account)
            # A client that re-reads the credential file adopts whatever the file holds now.
            if options.reread or spec.get('sdk_reread'):
                model_request(spec, options.marker + '-reread', out, 'model_after_signal_reread')
    elif options.actor == 'grandchild':
        if not wait_for(options.wait or spec['paused_file'], options.seconds):
            out('signal', 'timeout')
            return 0
        out('signal', 'seen', ppid=os.getppid())
        model_request(spec, 'KLX-GRANDCHILD', out, 'model_after_latch')
        tcp(('127.0.0.1', spec['backend_port']), out, 'direct_backend_tcp')
        tcp(('127.0.0.1', spec['static_port']), out, 'direct_static_tcp')
        udp(('127.0.0.1', spec['backend_port']), out, 'direct_udp')
        workflow(spec, out, 'after')
    elif options.actor == 'testlike':
        end = time.monotonic() + options.seconds
        while time.monotonic() < end:
            try:
                connection = http.client.HTTPConnection('127.0.0.1', spec['gate_port'], timeout=3)
                connection.request('GET', '/backend-api/codex/models?client_version=klx')
                response = connection.getresponse()
                response.read()
                out('gate_models', response.status)
            except (OSError, http.client.HTTPException) as error:
                out('gate_models', code(error))
            tcp(('127.0.0.1', spec['static_port']), out, 'static_tcp')
            time.sleep(.5)
        out('finished', 'ok')
    elif options.actor == 'workflow':
        workflow(spec, out, options.tag)
    elif options.actor == 'loop':
        path = Path(spec['workspace']) / 'loop.txt'
        end, count = time.monotonic() + options.seconds, 0
        while time.monotonic() < end:
            count += 1
            with open(path, 'a') as handle:
                handle.write('%d %.3f\n' % (count, time.monotonic()))
            time.sleep(.3)
        (Path(spec['workspace']) / 'loop.finished').write_text(str(count))
        out('finished', count)
    elif options.actor == 'bind':
        for name, address in (('loopback', '127.0.0.1'), ('wildcard', '0.0.0.0'), ('other', '127.0.0.2')):
            sock = socket.socket()
            try:
                sock.bind((address, spec['gate_port']))
                sock.listen(1)
                out('bind_gate_port_' + name, 'bound')
            except OSError as error:
                out('bind_gate_port_' + name, code(error))
            finally:
                sock.close()
        direct = tcp(('127.0.0.1', spec['backend_port']), out, 'direct_backend_tcp')
        del direct
        sock = socket.socket()
        sock.settimeout(1.5)
        try:
            sock.connect(('127.0.0.1', spec['gate_port']))
            sock.sendall(b'GET /backend-api/codex/models HTTP/1.0\r\n\r\n')
            data = sock.recv(64)
            out('gate_port_request', 'reply' if data else 'eof', head=data[:12].decode(errors='replace'))
        except OSError as error:
            out('gate_port_request', code(error))
        finally:
            sock.close()
    elif options.actor == 'tunnel':
        context = ssl.create_default_context(cafile=spec['ca'])
        context.verify_flags &= ~getattr(ssl, 'VERIFY_X509_STRICT', 0)  # fixture CA; hostname still checked
        for path in ('/oauth/revoke', '/backend-api/codex/responses'):
            try:
                sock = socket.create_connection(('127.0.0.1', spec['gate_port']), timeout=5)
                sock.sendall(b'CONNECT auth.openai.com:443 HTTP/1.1\r\nHost: auth.openai.com:443\r\n\r\n')
                raw = b''
                while b'\r\n\r\n' not in raw:
                    data = sock.recv(1)
                    if not data:
                        break
                    raw += data
                head = raw.decode(errors='replace').split('\r\n')[0]
                if ' 200 ' not in head + ' ':
                    out('connect_' + path, head[:40])
                    sock.close()
                    continue
                tls = context.wrap_socket(sock, server_hostname='auth.openai.com')
                tls.sendall(('POST %s HTTP/1.1\r\nHost: auth.openai.com\r\nContent-Length: 2\r\n'
                             'Connection: close\r\n\r\n{}' % path).encode())
                reply = tls.recv(256).decode(errors='replace').split('\r\n')[0]
                out('connect_' + path, 'tunnel', reply=reply[:40])
                tls.close()
            except ssl.SSLError as error:
                out('connect_' + path, 'ssl-error', reason=str(error.reason))
            except OSError as error:
                out('connect_' + path, code(error))
    elif options.actor == 'direct':
        tcp(('127.0.0.1', spec['backend_port']), out, 'direct_backend_tcp')
        udp(('127.0.0.1', spec['backend_port']), out, 'direct_udp')
    elif options.actor == 'fswrite':
        # Outer filesystem write rules: create a file in each named place; report ok or the errno.
        for name, directory in sorted(spec['fs_targets'].items()):
            target = Path(directory) / ('klx-fs-' + name)
            try:
                with open(target, 'w') as handle:
                    handle.write('klx\n')
                os.unlink(target)
                out('write_' + name, 'ok')
            except OSError as error:
                out('write_' + name, code(error))
        env = {'PATH': '/usr/bin:/bin', 'HOME': spec['home'], 'GIT_CONFIG_NOSYSTEM': '1',
               'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
        repo = Path(spec['workspace']) / 'fs-repo'
        steps = [['git', 'init', '--quiet', str(repo)], ['git', '-C', str(repo), 'commit', '--quiet',
                                                         '--allow-empty', '-m', 'klx']]
        results = [subprocess.run(step, env=env, capture_output=True, text=True, timeout=20,
                                  stdin=subprocess.DEVNULL) for step in steps]
        out('git_commit_workspace', max(result.returncode for result in results),
            stderr=' '.join(result.stderr.strip() for result in results)[-200:])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
