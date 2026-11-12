"""NL actor: egress and IPC attempts made from inside (or, as control, outside) a boundary.

Each attempt sends a marker RUN:ACTOR:ATTEMPT so the fixture listeners, which
run outside every boundary, record ground truth independently of this report.
Prints one JSON object per attempt. Fabricated fixtures only; loopback targets.

python3 -I -B attempt.py --spec SPEC.json --actor NAME [--mode root|matrix|detach|escape|inherited|latch]
"""
import errno
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
IPPROTO_MPTCP = 262
NODE_CLIENT = r'''
const net = require('net'), dgram = require('dgram');
const [prefix, a4, a6, port, gate, uport] = process.argv.slice(1);
const out = (row) => process.stdout.write(JSON.stringify(row) + '\n');
function tcp(name, host, p) { return new Promise((done) => {
  let over = false; const s = net.connect({host, port: +p, timeout: 2000});
  const fin = (r) => { if (!over) { over = true; s.destroy(); out({attempt: name, ...r}); done(); } };
  s.on('connect', () => s.write(prefix + ':' + name + '\n'));
  s.on('data', (d) => fin({result: 'ok', reply: d.toString().trim()}));
  s.on('error', (e) => fin({result: e.code || String(e)}));
  s.on('timeout', () => fin({result: 'timeout'})); }); }
function udp(name, type, host, p) { return new Promise((done) => {
  let over = false; const s = dgram.createSocket(type);
  const fin = (r) => { if (!over) { over = true; try { s.close(); } catch (e) {} out({attempt: name, ...r}); done(); } };
  s.on('error', (e) => fin({result: e.code || String(e)}));
  s.on('message', (m) => fin({result: 'ok', reply: m.toString().trim()}));
  try { s.send(prefix + ':' + name, +p, host, (e) => { if (e) fin({result: e.code || String(e)}); }); }
  catch (e) { fin({result: e.code || String(e)}); }
  setTimeout(() => fin({result: 'timeout'}), 1500); }); }
(async () => {
  await tcp('tcp4_alt', a4, port); await tcp('tcp6_alt', a6, port);
  await tcp('tcp4_gate', a4, gate); await tcp('tcp6_gate', a6, gate);
  await udp('udp4_alt', 'udp4', a4, uport); await udp('udp6_alt', 'udp6', a6, uport);
})();
'''


def code(error):
    if isinstance(error, socket.timeout):
        return 'timeout'
    return errno.errorcode.get(getattr(error, 'errno', None) or 0, type(error).__name__)


class Actor:
    def __init__(self, spec, actor):
        self.spec, self.actor, self.rows = spec, actor, []

    def marker(self, name):
        return '%s:%s:%s' % (self.spec['run'], self.actor, name)

    def emit(self, name, result, **extra):
        row = {'run': self.spec['run'], 'actor': self.actor, 'attempt': name, 'result': result,
               'pid': os.getpid(), 'at': time.time(), **extra}
        self.rows.append(row)
        print(json.dumps(row), flush=True)

    def tcp(self, name, family, address, proto=0, fastopen=False):
        try:
            with socket.socket(family, socket.SOCK_STREAM, proto) as s:
                data = (self.marker(name) + '\n').encode()
                if fastopen:
                    # Blocking with kernel timeouts, so the cookie-less TFO send completes.
                    limit = struct.pack('ll', 2, 0)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, limit)
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, limit)
                    s.sendto(data, socket.MSG_FASTOPEN, address)
                else:
                    s.settimeout(2)
                    s.connect(address)
                    s.sendall(data)
                reply = s.recv(64).decode(errors='replace').strip()
            self.emit(name, 'ok', reply=reply)
        except OSError as error:
            self.emit(name, code(error))

    def udp(self, name, family, address, connected=False):
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as s:
                s.settimeout(1.5)
                data = self.marker(name).encode()
                if connected:
                    s.connect(address)
                    s.send(data)
                else:
                    s.sendto(data, address)
                reply = s.recv(64).decode(errors='replace').strip()
            self.emit(name, 'ok', reply=reply)
        except OSError as error:
            self.emit(name, code(error))

    def unix(self, name, address, payload, expect=None):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2)
                s.connect(address)
                s.sendall(payload)
                reply = s.recv(128).decode(errors='replace').strip()
            self.emit(name, 'ok' if expect is None or reply.startswith(expect) else 'unexpected',
                      reply=reply[:16])
        except OSError as error:
            self.emit(name, code(error))

    def unix_send(self, name, address, kind=socket.SOCK_DGRAM, pair=True, connect=False):
        """Address an outside UNIX socket from a socket()- or socketpair()-made AF_UNIX socket."""
        made = []
        try:
            made = list(socket.socketpair(socket.AF_UNIX, kind)) if pair else [socket.socket(socket.AF_UNIX, kind)]
            data = self.marker(name).encode()
            if connect:
                made[0].connect(address)
                made[0].send(data)
            else:
                made[0].sendto(data, address)
            self.emit(name, 'sent')
        except OSError as error:
            self.emit(name, code(error))
        finally:
            for s in made:
                s.close()

    def network(self):
        spec = self.spec
        alt, gate, remote, udp = spec['alt_port'], spec['gate_port'], spec['remote_host'], spec['udp_port']
        self.tcp('tcp4_alt', socket.AF_INET, ('127.0.0.1', alt))
        self.tcp('tcp6_alt', socket.AF_INET6, ('::1', alt))
        self.tcp('mapped_alt', socket.AF_INET6, ('::ffff:127.0.0.1', alt))
        self.tcp('tcp4_gate', socket.AF_INET, ('127.0.0.1', gate))
        self.tcp('tcp6_gate', socket.AF_INET6, ('::1', gate))
        self.tcp('gate_port_other_host', socket.AF_INET, (remote, gate))
        self.tcp('mptcp_alt', socket.AF_INET, ('127.0.0.1', alt), proto=IPPROTO_MPTCP)
        self.tcp('tfo_alt', socket.AF_INET, ('127.0.0.1', alt), fastopen=True)
        self.udp('udp4_alt', socket.AF_INET, ('127.0.0.1', udp))
        self.udp('udp6_alt', socket.AF_INET6, ('::1', udp))
        self.udp('udp4_connected_alt', socket.AF_INET, ('127.0.0.1', udp), connected=True)

    def ipc(self):
        spec = self.spec
        self.unix('unix_daemon_relay', spec['daemon_path'],
                  ('RELAY ' + self.marker('unix_daemon_relay') + '\n').encode(), expect='RELAYED')
        self.unix('unix_abstract', '\0' + spec['abstract_name'],
                  (self.marker('unix_abstract') + '\n').encode(), expect='ABSTRACT')
        uid = str(os.getuid()).encode().hex().encode()
        self.unix('dbus_session_auth', spec['dbus_path'], b'\0AUTH EXTERNAL ' + uid + b'\r\n', expect='OK')
        # Pathname and abstract datagram fixtures outside: only the fixture record proves delivery.
        self.unix_send('unix_dgram_socket', spec['dgram_path'], pair=False)
        self.unix_send('unix_dgram_pair_sendto', spec['dgram_path'])
        self.unix_send('unix_dgram_pair_connect', spec['dgram_path'], connect=True)
        self.unix_send('unix_abstract_dgram_pair', '\0' + spec['abstract_dgram_name'])
        # Connected stream/seqpacket pairs: sendto() gives EISCONN or goes to the peer.
        self.unix_send('unix_stream_pair_sendto', spec['dgram_path'], kind=socket.SOCK_STREAM)
        self.unix_send('unix_seqpacket_pair_sendto', spec['dgram_path'], kind=socket.SOCK_SEQPACKET)
        try:
            os.kill(spec['victim_pid'], signal.SIGUSR1)
            self.emit('signal_outside', 'ok')
        except OSError as error:
            self.emit('signal_outside', code(error))

    def child(self, name, command, env=None):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=25, env=env,
                                    stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            self.emit(name + '_spawn', 'timeout')
            return
        for line in result.stdout.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if 'run' not in row:
                row.update(run=self.spec['run'], actor=name)
            self.rows.append(row)
            print(json.dumps(row), flush=True)
        self.emit(name + '_spawn', 'exit', code=result.returncode, stderr=result.stderr[-300:])

    def root(self):
        spec, py = self.spec, self.spec['python']
        me = [py, '-I', '-B', str(HERE / 'attempt.py'), '--spec', spec['path']]
        self.network()
        self.ipc()
        self.child('shell', ['/bin/sh', '-c', 'exec "$@"', 'sh', *me, '--actor', 'shell', '--mode', 'matrix'])
        self.child('envi', ['/usr/bin/env', '-i', *me, '--actor', 'envi', '--mode', 'matrix'])
        self.child('node', [spec['node'], '-e', NODE_CLIENT, spec['run'] + ':node', '127.0.0.1', '::1',
                            str(spec['alt_port']), str(spec['gate_port']), str(spec['udp_port'])])
        for name, url in (('curl_tcp4_alt', 'http://127.0.0.1:%d/' % spec['alt_port']),
                          ('curl_tcp6_alt', 'http://[::1]:%d/' % spec['alt_port']),
                          ('curl_tcp4_gate', 'http://127.0.0.1:%d/' % spec['gate_port'])):
            result = subprocess.run(['/usr/bin/env', '-i', spec['curl'], '-sS', '--http0.9', '--noproxy', '*',
                                     '-m', '3', url + self.marker(name)], capture_output=True, text=True,
                                    timeout=10, stdin=subprocess.DEVNULL)
            self.emit(name, 'ok' if result.returncode == 0 else 'curl_exit_%d' % result.returncode,
                      reply=result.stdout.strip()[:16], stderr=result.stderr.strip()[-160:])
        if spec.get('escape'):
            self.child('systemd_run', ['systemd-run', '--user', '--collect', '--wait', '--quiet',
                                       '--unit', spec['escape_unit'], '-p', 'RuntimeMaxSec=20', '--',
                                       *me, '--actor', 'escaped', '--mode', 'escape'],
                       env={'PATH': '/usr/bin:/bin', 'XDG_RUNTIME_DIR': spec['runtime_dir'],
                            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + spec['dbus_path']})
        subprocess.Popen([*me, '--actor', 'detached', '--mode', 'detach'], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait(timeout=5)
        self.emit('root_exit', 'exit')

    def detach(self):
        if os.fork():
            return
        os.setsid()
        if os.fork():
            os._exit(0)
        out = Path(self.spec['outdir']) / ('detached-%s.jsonl' % self.spec['run'])
        root = os.getppid()
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.dup2(fd, 1)
        os.close(fd)
        end = time.monotonic() + 8
        while time.monotonic() < end and Path(self.spec['root_done']).exists() is False:
            time.sleep(.05)
        self.emit('detached_identity', 'ok', sid=os.getsid(0), ppid=os.getppid(), first_ppid=root,
                  root_done_seen=Path(self.spec['root_done']).exists())
        self.network()
        self.ipc()
        self.emit('detached_done', 'ok')
        os._exit(0)

    def inherited(self):
        fds = self.spec['inherited']
        for name, fd in sorted(fds.items()):
            try:
                s = socket.socket(fileno=fd)
            except OSError as error:
                self.emit('inherited_' + name, code(error))
                continue
            try:
                s.settimeout(3)
                if name == 'listener':
                    peer, _ = s.accept()
                    with peer:
                        peer.sendall((self.marker('inherited_listener') + '\n').encode())
                        self.emit('inherited_listener', 'ok', reply=peer.recv(32).decode().strip())
                elif name == 'udp':
                    s.send(self.marker('inherited_udp').encode())
                    self.emit('inherited_udp', 'ok', reply=s.recv(32).decode().strip())
                else:
                    s.sendall((self.marker('inherited_tcp') + '\n').encode())
                    self.emit('inherited_tcp', 'ok', reply=s.recv(32).decode().strip())
            except OSError as error:
                self.emit('inherited_' + name, code(error))
            finally:
                s.detach()

    def latch(self):
        spec = self.spec
        s = socket.create_connection(('127.0.0.1', spec['gate_port']), timeout=3)
        s.sendall((self.marker('stream_before_latch') + '\n').encode())
        self.emit('stream_before_latch', 'ok', reply=s.recv(32).decode().strip())
        end = time.monotonic() + 8
        while not Path(spec['latched']).exists() and time.monotonic() < end:
            time.sleep(.02)
        try:
            s.sendall((self.marker('stream_after_latch') + '\n').encode())
            data = s.recv(32)
            self.emit('stream_after_latch', 'ok' if data else 'eof', reply=data.decode().strip())
        except OSError as error:
            self.emit('stream_after_latch', code(error))
        s.close()
        self.tcp('gate_reconnect_after_latch', socket.AF_INET, ('127.0.0.1', spec['gate_port']))
        self.tcp('alt_after_latch', socket.AF_INET, ('127.0.0.1', spec['alt_port']))
        self.udp('udp_after_latch', socket.AF_INET, ('127.0.0.1', spec['udp_port']))


def main(argv):
    args = dict(zip(argv[::2], argv[1::2]))
    spec = json.loads(Path(args['--spec']).read_text())
    spec['path'] = args['--spec']
    actor = Actor(spec, args['--actor'])
    mode = args.get('--mode', 'root')
    if mode == 'root':
        actor.root()
    elif mode == 'matrix':
        actor.network()
        actor.ipc()
    elif mode == 'detach':
        actor.detach()
    elif mode == 'escape':
        actor.tcp('tcp4_alt', socket.AF_INET, ('127.0.0.1', spec['alt_port']))
    elif mode == 'reuse':
        actor.tcp('gate_port_reuse', socket.AF_INET, ('127.0.0.1', spec['gate_port']))
    elif mode == 'inherited':
        actor.inherited()
    elif mode == 'latch':
        actor.latch()
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
