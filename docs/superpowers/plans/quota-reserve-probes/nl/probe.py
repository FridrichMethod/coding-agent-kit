"""NL decisive boundary matrix with loopback fixtures; writes evidence/matrix.json.

All listeners run in this process, outside every boundary, and record the
markers they receive: that record, not the actor's report, decides whether a
direct attempt reached the alternate listener. Runs (layers):
  L0   no boundary (unrelated outside process; runs concurrently with L2a)
  L1   Landlock only (TCP connect to the gate port, abstract UNIX + signal scopes)
  L2a  L1 plus seccomp complement denying AF_UNIX socket() and datagram socketpair(),
       UDP, MPTCP, io_uring
  L2b  L1 plus seccomp complement that still allows AF_UNIX socket()
L1, L2a and L2b each try one `systemd-run --user` escape as a transient,
collected unit named quota-nl-*. No real network, credentials or native client.
"""
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
LAYERS = {'L1': 'none', 'L2a': 'inet', 'L2b': 'inet+unix'}


class Fixtures:
    def __init__(self, root):
        self.root, self.events, self.lock = root, [], threading.Lock()
        self.start = time.monotonic()
        self.sockets, self.threads = [], []
        self.nonce = '%d-%d' % (os.getpid(), time.time_ns() % 10 ** 6)

    def record(self, event, **data):
        with self.lock:
            self.events.append({'at': round(time.monotonic() - self.start, 4), 'event': event, **data})

    def thread(self, target, *args):
        worker = threading.Thread(target=target, args=args, daemon=True)
        worker.start()
        self.threads.append(worker)

    def tcp_pair(self, reply, event, port=0, hosts=('127.0.0.1', '::1')):
        for attempt in range(20):
            first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            first.bind((hosts[0], port))
            chosen = first.getsockname()[1]
            pair = [first]
            try:
                for host in hosts[1:]:
                    other = socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET)
                    if ':' in host:
                        other.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    other.bind((host, chosen))
                    pair.append(other)
            except OSError:
                for item in pair:
                    item.close()
                continue
            for item in pair:
                item.listen(64)
                self.sockets.append(item)
                self.thread(self.serve_tcp, item, reply, event)
            return chosen
        raise RuntimeError('no common port')

    def serve_tcp(self, listener, reply, event):
        while True:
            try:
                peer, address = listener.accept()
            except OSError:
                return
            self.thread(self.handle_tcp, peer, address, reply, event)

    def handle_tcp(self, peer, address, reply, event):
        with peer:
            peer.settimeout(3)
            try:
                line = (peer.recv(256).decode(errors='replace').splitlines() or [''])[0]
            except OSError:
                line = ''
            marker = line.split(' ')[1].lstrip('/') if line.startswith('GET ') else line
            self.record(event, marker=marker, local=list(peer.getsockname()[:2]), peer_host=address[0])
            try:
                peer.sendall(reply)
            except OSError:
                pass

    def udp(self, port):
        for host, family in (('127.0.0.1', socket.AF_INET), ('::1', socket.AF_INET6)):
            s = socket.socket(family, socket.SOCK_DGRAM)
            s.bind((host, port))
            self.sockets.append(s)
            self.thread(self.serve_udp, s)
        return port

    def serve_udp(self, s):
        while True:
            try:
                data, address = s.recvfrom(256)
            except OSError:
                return
            self.record('alt_udp', marker=data.decode(errors='replace'), peer_host=address[0])
            s.sendto(b'UDP-ALT', address)

    def unix(self, address, kind):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(address)
        s.listen(16)
        self.sockets.append(s)
        self.thread(self.serve_unix, s, kind)

    def serve_unix(self, s, kind):
        while True:
            try:
                peer, _ = s.accept()
            except OSError:
                return
            with peer:
                peer.settimeout(3)
                try:
                    line = peer.recv(256).decode(errors='replace').strip()
                except OSError:
                    continue
                if kind == 'daemon' and line.startswith('RELAY '):
                    marker = line[6:]
                    self.record('daemon_request', marker=marker)
                    # The modeled host daemon acts outside the boundary for its client.
                    try:
                        with socket.create_connection(('127.0.0.1', self.alt_port), timeout=2) as out:
                            out.sendall(('relay:' + marker + '\n').encode())
                            out.recv(16)
                        peer.sendall(b'RELAYED\n')
                    except OSError:
                        peer.sendall(b'RELAY-FAILED\n')
                elif kind == 'bus':
                    # Stand-in for the session bus in native runs: record and refuse.
                    self.record('fake_bus_contact', first_bytes=len(line))
                else:
                    self.record('abstract_request', marker=line)
                    peer.sendall(b'ABSTRACT\n')

    def unix_dgram(self, address, event):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.bind(address)
        self.sockets.append(s)
        self.thread(self.serve_dgram, s, event)

    def serve_dgram(self, s, event):
        while True:
            try:
                data = s.recv(256)
            except OSError:
                return
            self.record(event, marker=data.decode(errors='replace'))

    def signals(self):
        while True:
            info = signal.sigwaitinfo({signal.SIGUSR1})
            self.record('victim_signal', sender_pid=info.si_pid)

    def close(self):
        for s in self.sockets:
            try:
                s.close()
            except OSError:
                pass


def boundary(port, mode, command, keep=()):
    if mode is None:
        return command
    extra = [x for fd in keep for x in ('--keep-fd', str(fd))]
    return [sys.executable, '-I', '-B', str(HERE / 'boundary.py'), '--gate-port', str(port),
            '--seccomp', mode, '--verbose', *extra, '--', *command]


def actor(spec_path, name, mode):
    return [sys.executable, '-I', '-B', str(HERE / 'attempt.py'), '--spec', str(spec_path),
            '--actor', name, '--mode', mode]


def run(command, timeout=60, **kwargs):
    started = time.time()
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                start_new_session=True, stdin=subprocess.DEVNULL, **kwargs)
        code, out, err = result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as error:
        code = 124
        out = (error.stdout or b'').decode(errors='replace') if isinstance(error.stdout, bytes) else (error.stdout or '')
        err = 'outer timeout'
    rows = []
    for line in out.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    applied = [json.loads(line.split(' ', 1)[1]) for line in err.splitlines()
               if line.startswith('boundary-applied ')]
    return {'exit': code, 'rows': rows, 'boundary': applied, 'started': started,
            'ended': time.time(), 'stderr_tail': err[-600:]}


def main():
    root = Path(tempfile.mkdtemp(prefix='quota-nl-'))
    print('scratch', root, flush=True)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    fx = Fixtures(root)
    fx.thread(fx.signals)
    result = {'nonce_units': [], 'runs': {}, 'stages': {}}
    base = {}
    try:
        fx.gate_port = fx.tcp_pair(b'GATE\n', 'gate_tcp')
        fx.alt_port = fx.tcp_pair(b'ALT\n', 'alt_tcp')
        fx.tcp_pair(b'OTHER-HOST\n', 'other_host_tcp', port=fx.gate_port, hosts=('127.0.0.2',))
        probe_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe_udp.bind(('127.0.0.1', 0))
        udp_port = probe_udp.getsockname()[1]
        probe_udp.close()
        fx.udp(udp_port)
        fx.unix(str(root / 'daemon.sock'), 'daemon')
        abstract = 'quota-nl-' + fx.nonce
        fx.unix('\0' + abstract, 'abstract')
        fx.unix_dgram(str(root / 'dgram.sock'), 'unix_dgram')
        fx.unix_dgram('\0' + abstract + '-dgram', 'abstract_dgram')
        runtime = os.environ.get('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid())
        bus = os.environ.get('DBUS_SESSION_BUS_ADDRESS', '')
        dbus_path = bus.split('unix:path=', 1)[1].split(',')[0] if 'unix:path=' in bus else runtime + '/bus'
        base = {'alt_port': fx.alt_port, 'gate_port': fx.gate_port, 'udp_port': udp_port,
                'remote_host': '127.0.0.2', 'daemon_path': str(root / 'daemon.sock'),
                'abstract_name': abstract, 'dbus_path': dbus_path, 'runtime_dir': runtime,
                'dgram_path': str(root / 'dgram.sock'), 'abstract_dgram_name': abstract + '-dgram',
                'victim_pid': os.getpid(), 'python': sys.executable, 'node': shutil.which('node'),
                'curl': shutil.which('curl'), 'outdir': str(root)}
        result['fixture'] = {'dbus_present': Path(dbus_path).exists(),
                             'node': bool(base['node']), 'curl': bool(base['curl'])}

        def spec_for(run_id, **extra):
            spec = {**base, 'run': run_id, 'root_done': str(root / (run_id + '.done')), **extra}
            path = root / (run_id + '.json')
            path.write_text(json.dumps(spec))
            return path, spec

        def layer(run_id, mode):
            unit = 'quota-nl-%s-%s' % (fx.nonce, run_id.lower())
            escape = mode is not None
            if escape:
                result['nonce_units'].append(unit)
            path, spec = spec_for(run_id, escape=escape, escape_unit=unit)
            outcome = run(boundary(fx.gate_port, mode, actor(path, 'root', 'root')), timeout=120)
            Path(spec['root_done']).write_text('done')
            detached = root / ('detached-%s.jsonl' % run_id)
            end = time.monotonic() + 15
            while time.monotonic() < end and 'detached_done' not in (detached.read_text() if detached.exists() else ''):
                time.sleep(.1)
            outcome['detached_rows'] = [json.loads(x) for x in detached.read_text().splitlines()] if detached.exists() else []
            attempts = [r['at'] for r in outcome['detached_rows'] if r['attempt'] != 'detached_identity']
            outcome['root_ended_before_detached'] = bool(attempts) and outcome['ended'] <= min(attempts)
            if escape:
                state = subprocess.run(['systemctl', '--user', 'show', unit, '--property=LoadState,ActiveState'],
                                       capture_output=True, text=True, timeout=10)
                outcome['escape_unit_after'] = state.stdout.split()
            result['runs'][run_id] = outcome

        layer('L1', 'none')
        concurrent = threading.Thread(target=layer, args=('L0', None))
        concurrent.start()
        layer('L2a', 'inet')
        concurrent.join(timeout=150)
        layer('L2b', 'inet+unix')

        # Inherited descriptors: a faulty launcher that keeps them, then the default close.
        for run_id, keep in (('INH-leak', True), ('INH-closed', False)):
            tcp = socket.create_connection(('127.0.0.1', fx.alt_port))
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.connect(('127.0.0.1', udp_port))
            listener = socket.socket()
            listener.bind(('127.0.0.1', 0))
            listener.listen(4)
            listen_port = listener.getsockname()[1]
            fds = {'tcp': tcp.fileno(), 'udp': udp.fileno(), 'listener': listener.fileno()}
            path, _ = spec_for(run_id, inherited=fds)
            holder = {}

            def outside_connect():
                time.sleep(.6)
                try:
                    with socket.create_connection(('127.0.0.1', listen_port), timeout=2) as s:
                        holder['outside'] = s.recv(64).decode().strip()
                        s.sendall(b'OUTSIDE\n')
                except OSError as error:
                    holder['outside'] = 'error ' + type(error).__name__
            helper = threading.Thread(target=outside_connect)
            command = boundary(fx.gate_port, 'inet', actor(path, 'inh', 'inherited'),
                               keep=fds.values() if keep else ())
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       pass_fds=tuple(fds.values()), start_new_session=True,
                                       stdin=subprocess.DEVNULL)
            for item in (tcp, udp, listener):
                item.close()
            helper.start()
            out, err = process.communicate(timeout=20)
            helper.join(timeout=5)
            result['stages'][run_id] = {'exit': process.returncode, 'rows': [json.loads(x) for x in out.splitlines()],
                                        'outside_peer_saw': holder.get('outside'),
                                        'boundary': [json.loads(x.split(' ', 1)[1]) for x in err.splitlines()
                                                     if x.startswith('boundary-applied ')]}

        # Latch: an admitted gate stream is shut by the gate; the boundary offers no bypass.
        latch_events, active = [], []
        latch_listener = socket.socket()
        latch_listener.bind(('127.0.0.1', 0))
        latch_listener.listen(4)
        latch_port = latch_listener.getsockname()[1]
        latched = threading.Event()

        def latch_serve():
            while True:
                try:
                    peer, _ = latch_listener.accept()
                except OSError:
                    return
                active.append(peer)

                def handle(peer=peer):
                    while True:
                        try:
                            data = peer.recv(256)
                        except OSError:
                            return
                        if not data:
                            return
                        latch_events.append({'after_latch': latched.is_set(), 'data': data.decode().strip()})
                        if not latched.is_set():
                            peer.sendall(b'GATE\n')
                threading.Thread(target=handle, daemon=True).start()
        threading.Thread(target=latch_serve, daemon=True).start()
        path, spec = spec_for('LATCH', gate_port=latch_port, latched=str(root / 'latched'))
        process = subprocess.Popen(boundary(latch_port, 'inet', actor(path, 'latch', 'latch')),
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                   start_new_session=True, stdin=subprocess.DEVNULL)
        end = time.monotonic() + 10
        while not latch_events and time.monotonic() < end:
            time.sleep(.02)
        latched.set()
        for peer in active:
            try:
                peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            peer.close()
        # shutdown() stops listening and wakes the blocked accept(); close() alone does not.
        latch_listener.shutdown(socket.SHUT_RDWR)
        latch_listener.close()
        Path(spec['latched']).write_text('1')
        out, _ = process.communicate(timeout=20)
        result['stages']['LATCH'] = {'exit': process.returncode, 'rows': [json.loads(x) for x in out.splitlines()],
                                     'gate_received': latch_events}

        # Port reuse: the Landlock rule names a port, not the gate's identity.
        reuse = {}
        hold = socket.socket()
        hold.bind(('127.0.0.1', 0))
        hold.listen(1)
        reuse_port = hold.getsockname()[1]
        path, _ = spec_for('REUSE-held', gate_port=reuse_port)
        try:
            impostor = socket.socket()
            impostor.bind(('127.0.0.1', reuse_port))
            reuse['impostor_bind_while_held'] = 'bound'
            impostor.close()
        except OSError as error:
            reuse['impostor_bind_while_held'] = errno.errorcode.get(error.errno, type(error).__name__)
        reuse['held'] = run(boundary(reuse_port, 'inet', actor(path, 'reuse', 'reuse')), timeout=20)
        hold.close()
        impostor = socket.socket()
        impostor.bind(('127.0.0.1', reuse_port))
        impostor.listen(4)
        fx.sockets.append(impostor)
        fx.thread(fx.serve_tcp, impostor, b'IMPOSTOR\n', 'impostor_tcp')
        path, _ = spec_for('REUSE-free', gate_port=reuse_port)
        reuse['free'] = run(boundary(reuse_port, 'inet', actor(path, 'reuse', 'reuse')), timeout=20)
        result['stages']['REUSE'] = reuse
        time.sleep(.5)
    finally:
        fx.close()
        with fx.lock:
            result['events'] = list(fx.events)
        result['ports'] = {'gate': 'GATE_PORT', 'alt': 'ALT_PORT'}
        text = json.dumps(result, indent=2, sort_keys=True)
        replacements = [(str(root), '<PROBE_ROOT>'), (base.get('runtime_dir', ''), '<RUNTIME_DIR>'),
                        (str(Path.home()), '<HOME>')]
        for old, new in replacements:
            if old:
                text = text.replace(old, new)
        (HERE / 'evidence').mkdir(exist_ok=True)
        (HERE / 'evidence' / 'matrix.json').write_text(text + '\n')
        shutil.rmtree(root, ignore_errors=True)
        print('removed', root, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
