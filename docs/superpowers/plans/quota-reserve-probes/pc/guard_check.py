"""Containment self-check; run only under network_guard.py."""
import os
import socket
import subprocess
import sys

listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
local = socket.create_connection(listener.getsockname(), timeout=1)
accepted, _ = listener.accept()
local.sendall(b'loopback')
assert accepted.recv(8) == b'loopback'
local.close()
accepted.close()
listener.close()
print('loopback TCP: allowed', flush=True)
for family, kind, address in (
    (socket.AF_INET, socket.SOCK_STREAM, ('192.0.2.1', 9)),
    (socket.AF_INET6, socket.SOCK_STREAM, ('::1', 9)),
    (socket.AF_INET, socket.SOCK_DGRAM, ('192.0.2.1', 9)),
):
    try:
        with socket.socket(family, kind) as stream:
            stream.settimeout(.2)
            stream.connect(address)
    except OSError as error:
        assert error.errno in (38, 101), error
        print('non-loopback or unsupported socket: blocked before network', family, kind, error.errno)
    else:
        raise AssertionError('network guard failed')
left, right = socket.socketpair()
left.sendall(b'unix')
assert right.recv(4) == b'unix'
left.close()
right.close()
print('Unix socket pair: allowed')
# A new session and an exec must inherit the guard.
child = subprocess.run([sys.executable, '-I', '-B', '-c',
    "import socket; s=socket.socket(); s.connect(('192.0.2.1',9))"],
    start_new_session=True, capture_output=True, text=True, timeout=2)
assert child.returncode != 0 and ('101' in child.stderr or '38' in child.stderr), child
print('exec descendant in new session: blocked')
