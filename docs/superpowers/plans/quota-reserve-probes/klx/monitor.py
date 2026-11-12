"""KLX monitor process: heartbeats to the gate and usage polls, separate from the terminal owner.

Research code only. It must be a different process from the launcher that owns the native's
PTY: K case 3 kills it, and the native's terminal must survive that. It sends a heartbeat over
its inherited socket to the gate every `hb_interval` seconds and starts a controller read
(reader.py) every `poll_interval` seconds. Commands arrive on a pipe from the launcher:
`stall` (stop heartbeats and polls, keep the socket open), `close` (close the gate socket, stay
alive) and `exit`.

    python3 -I -B monitor.py CONFIG.json
"""
import importlib.util
import json
import os
from pathlib import Path
import select
import socket
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('klx_reader', HERE / 'reader.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


def main(config_path):
    config = json.loads(Path(config_path).read_text())
    log = open(config['events'], 'a', buffering=1)
    lock = threading.Lock()

    def record(event, **data):
        with lock:
            log.write(json.dumps({'at': time.monotonic(), 'event': event, **data}, sort_keys=True) + '\n')

    gate = socket.socket(fileno=config['gate_fd'])
    commands = config['command_fd']
    state = {'stalled': False, 'closed': False, 'seq': 0}
    polling = threading.Lock()

    def poll():
        if not polling.acquire(blocking=False):
            return
        try:
            result = reader.read(config['reader'])
            record('poll', **{key: result.get(key) for key in ('result', 'total_seconds', 'exit', 'error')})
        finally:
            polling.release()

    record('monitor_started', pid=os.getpid(), hb_interval=config['hb_interval'],
           poll_interval=config.get('poll_interval'))
    next_hb, next_poll = 0.0, time.monotonic() + (config.get('poll_interval') or 1e9)
    while True:
        now = time.monotonic()
        if not state['stalled'] and not state['closed'] and now >= next_hb:
            state['seq'] += 1
            try:
                gate.sendall((json.dumps({'hb': state['seq']}) + '\n').encode())
            except OSError:
                record('heartbeat_failed')
                state['closed'] = True
            next_hb = now + config['hb_interval']
        if not state['stalled'] and config.get('poll_interval') and now >= next_poll:
            threading.Thread(target=poll, daemon=True).start()
            next_poll = now + config['poll_interval']
        if select.select([commands], [], [], 0.02)[0]:
            data = os.read(commands, 4096)
            if not data:
                record('command_pipe_eof')
                return 0
            for word in data.decode(errors='replace').split():
                record('command', command=word)
                if word == 'stall':
                    state['stalled'] = True
                elif word == 'close':
                    gate.close()
                    state['closed'] = True
                elif word == 'exit':
                    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
