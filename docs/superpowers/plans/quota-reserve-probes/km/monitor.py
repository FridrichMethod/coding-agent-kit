"""KM monitor: the trusted controller's heartbeat to the gate (research only).

It holds no bearer. Every interval it writes one `poll` line on its inherited socketpair end;
the gate runs one bounded quota poll per heartbeat and latches when the channel reaches EOF or
no heartbeat arrives within its maximum age. The three loss modes are driven from outside:
SIGKILL ends the process, SIGUSR1 closes the channel while the process stays alive (EOF), and
SIGUSR2 stops heartbeats with the channel still open (stall).

python3 -I -B monitor.py --fd N --interval SECONDS
"""
import json
import signal
import socket
import sys
import time


def main(argv):
    options = dict(zip(argv[::2], argv[1::2]))
    state = {'sock': socket.socket(fileno=int(options['--fd'])), 'polling': True}
    interval = float(options['--interval'])

    def note(event):
        print(json.dumps({'event': event, 't': time.monotonic()}), flush=True)

    def eof(*_):
        if state['sock'] is not None:
            state['sock'].close()
            state['sock'] = None
        note('monitor_eof_sent')

    def stall(*_):
        state['polling'] = False
        note('monitor_stalled')

    signal.signal(signal.SIGUSR1, eof)
    signal.signal(signal.SIGUSR2, stall)
    note('monitor_started')
    while True:
        if state['polling'] and state['sock'] is not None:
            try:
                state['sock'].sendall(b'poll\n')
            except OSError:
                state['sock'] = None
        time.sleep(interval)


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
