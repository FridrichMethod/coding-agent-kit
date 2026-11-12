"""KM in-boundary actors: SDK-like model clients, bind and egress attempts, local tools.

Research fixture only. Run inside the protected invocation (as a native tool command or under
the same Seatbelt profile). Each action appends JSON lines to OUT; bearers are read from the
account store on every request (as an SDK child that follows the native store would) and are
never written out. Fabricated stores only.

python3 -I -B child.py SPEC.json ACTION OUT
ACTION: sdk | detach | direct | bind | connect | tools
"""
import json
import os
from pathlib import Path
import socket
import sys
import time


def note(out, **row):
    with open(out, 'a') as handle:
        handle.write(json.dumps({'t': time.monotonic(), 'pid': os.getpid(), **row}) + '\n')


def current(spec):
    """(bearer, account) from the native store, read now."""
    if spec['provider'] == 'claude':
        data = json.loads((Path(spec['config_dir']) / '.credentials.json').read_text())
        return data['claudeAiOauth']['accessToken'], None
    data = json.loads((Path(spec['codex_home']) / 'auth.json').read_text())
    return data['tokens']['access_token'], data['tokens'].get('account_id')


def model_request(spec, tag):
    bearer, account = current(spec)
    if spec['provider'] == 'claude':
        path = '/v1/messages'
        body = {'model': spec['model'], 'max_tokens': 16, 'stream': True,
                'messages': [{'role': 'user', 'content': 'KM sdk child ' + tag}]}
        extra = 'anthropic-version: 2023-06-01\r\n'
    else:
        path = '/backend-api/codex/responses'
        body = {'model': spec['model'], 'stream': True, 'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'KM sdk child ' + tag}]}]}
        extra = 'ChatGPT-Account-ID: %s\r\n' % account
    data = json.dumps(body).encode()
    head = ('POST %s HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer %s\r\nContent-Type: application/json\r\n'
            '%sContent-Length: %d\r\nConnection: close\r\n\r\n' % (path, bearer, extra, len(data))).encode()
    sock = socket.create_connection(('127.0.0.1', spec['gate_port']), timeout=10)
    try:
        sock.sendall(head + data)
        reply = b''
        while len(reply) < 65536:
            chunk = sock.recv(65536)
            if not chunk:
                break
            reply += chunk
    finally:
        sock.close()
    try:
        return int(reply.split(b' ', 2)[1])
    except (IndexError, ValueError):
        return 0


def sdk(spec, out, role):
    deadline = time.monotonic() + float(spec.get('duration', 30))
    n = 0
    while time.monotonic() < deadline:
        n += 1
        began = time.monotonic()
        try:
            status = model_request(spec, '%s-%d' % (role, n))
            note(out, action='sdk', role=role, n=n, status=status, ms=round((time.monotonic() - began) * 1000, 1))
        except OSError as error:
            note(out, action='sdk', role=role, n=n, error=error.errno)
        time.sleep(float(spec.get('interval', 0.5)))


def detach(spec, out):
    """Parent returns at once; a setsid child and its double-forked grandchild keep sending."""
    if os.fork():
        note(out, action='detach_parent_exit')
        return
    os.setsid()
    if os.fork():
        sdk(spec, out, 'child')
        os._exit(0)
    sdk(spec, out, 'grandchild')
    os._exit(0)


def attempt(kind, host, port):
    fam = socket.AF_INET6 if ':' in host else socket.AF_INET
    sock = socket.socket(fam, socket.SOCK_STREAM if kind == 'tcp' else socket.SOCK_DGRAM)
    sock.settimeout(3)
    try:
        if kind == 'tcp':
            sock.connect((host, port))
            sock.sendall(b'GET / HTTP/1.0\r\n\r\n')
            return 'connected'
        sock.sendto(b'km', (host, port))
        return 'sent'
    except OSError as error:
        return 'errno:%s' % error.errno
    finally:
        sock.close()


def direct(spec, out):
    for host in ('127.0.0.1', '::1'):
        for port in spec.get('direct_ports', []):
            note(out, action='direct', host=host, port_role=spec['port_roles'].get(str(port), 'other'),
                 result=attempt('tcp', host, port))
    note(out, action='direct', host='127.0.0.1', port_role='gate_udp',
         result=attempt('udp', '127.0.0.1', spec['gate_port']))


def bind(spec, out):
    for host in ('127.0.0.1', '::1', '0.0.0.0', '::'):
        fam = socket.AF_INET6 if ':' in host else socket.AF_INET
        sock = socket.socket(fam, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, spec['gate_port']))
            sock.listen(1)
            result = 'bound'
        except OSError as error:
            result = 'errno:%s' % error.errno
        finally:
            sock.close()
        note(out, action='bind_gate_port', host=host, result=result)


def connect(spec, out):
    for target in spec.get('connect_targets', []):
        sock = socket.create_connection(('127.0.0.1', spec['gate_port']), timeout=5)
        try:
            sock.sendall(('CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n' % (target, target)).encode())
            head = sock.recv(256)
            status = int(head.split(b' ', 2)[1]) if head else 0
        except (OSError, ValueError, IndexError):
            status = 0
        finally:
            sock.close()
        note(out, action='connect', target=target, status=status, phase=spec.get('phase'))


def tools(spec, out):
    """A long local command (file heartbeat) and a test-like process with a network probe."""
    if os.fork():
        note(out, action='tools_parent_exit')
        return
    os.setsid()
    path = Path(spec['tool_file'])
    deadline = time.monotonic() + float(spec.get('duration', 60))
    n = 0
    while time.monotonic() < deadline:
        n += 1
        with open(path, 'a') as handle:
            handle.write('%d %.3f\n' % (n, time.monotonic()))
        if n % 4 == 0:
            note(out, action='tool_net', n=n, result=attempt('tcp', '127.0.0.1', spec['upstream_port']),
                 gate=attempt('tcp', '127.0.0.1', spec['gate_port']))
        time.sleep(0.5)
    os._exit(0)


def main(argv):
    spec = json.loads(Path(argv[0]).read_text())
    action, out = argv[1], argv[2]
    {'sdk': lambda: sdk(spec, out, 'direct'), 'detach': lambda: detach(spec, out), 'direct': lambda: direct(spec, out),
     'bind': lambda: bind(spec, out), 'connect': lambda: connect(spec, out),
     'tools': lambda: tools(spec, out)}[action]()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
