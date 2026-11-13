"""A fake pre-launch refresh: python3 -I -B refresh_client.py TARGET [--confined PORT].

It opens a CONNECT tunnel through the gate on GATE_PORT to TARGET, sends one line and exits 0 when
the echo comes back, 4 when the tunnel is refused, 5 when it was not confined first.
"""

import os
import socket
import sys


def main(argv):
    if argv[1:2] != ["--confined"]:
        return 5
    sock = socket.create_connection(("127.0.0.1", int(os.environ["GATE_PORT"])), timeout=5)
    sock.sendall(b"CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (argv[0].encode(), argv[0].encode()))
    head = b""
    while b"\r\n\r\n" not in head:
        data = sock.recv(1)
        if not data:
            return 4
        head += data
    if b" 200 " not in head.split(b"\r\n")[0] + b" ":
        return 4
    sock.sendall(b"refresh\n")
    return 0 if sock.recv(64) == b"refresh\n" else 6


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
