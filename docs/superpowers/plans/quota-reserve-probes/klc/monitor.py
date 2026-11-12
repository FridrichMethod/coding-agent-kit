"""KLC research monitor: the trusted controller's poll heartbeat to the gate.

It holds no bearer. Every interval it asks the gate (over an inherited
socketpair end) for one bounded same-bearer revalidation; the gate's lease
deadline, not this process, decides freshness. Commands on stdin model the
three loss modes: `eof` closes the channel while the process stays alive,
`stall` stops polling with the channel open, `exit` ends the process. A
SIGKILL from outside is the third mode.

python3 -I -B monitor.py --fd N --interval SECONDS
"""
import json
import os
import select
import socket
import sys
import time


def main(argv):
    options = dict(zip(argv[::2], argv[1::2]))
    sock = socket.socket(fileno=int(options['--fd']))
    interval = float(options['--interval'])
    polling, last = True, 0.0
    stdin = sys.stdin.fileno()
    while True:
        now = time.monotonic()
        if polling and sock is not None and now - last >= interval:
            try:
                sock.sendall(b'poll\n')
            except OSError:
                sock = None
            last = now
        ready = select.select([stdin], [], [], 0.05)[0]
        if not ready:
            continue
        line = os.read(stdin, 256).decode(errors='replace').strip()
        if not line or line == 'exit':
            return 0
        for command in line.split():
            if command == 'eof' and sock is not None:
                sock.close()
                sock = None
            elif command == 'stall':
                polling = False
            print(json.dumps({'event': 'monitor_command', 'command': command, 't': time.monotonic()}), flush=True)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
