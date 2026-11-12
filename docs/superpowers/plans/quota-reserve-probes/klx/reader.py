"""KLX controller read: a fresh native `codex app-server` (stdio) inside the boundary, one usage read.

Research code only. The --no-daemon TUI runs its App Server in process, so a controller cannot
ask it for a read. A long-lived controller App Server would keep the bearer it loaded at start:
AuthManager observes stored credentials only on an explicit reload (login/logout), so after the
TUI refreshes, its reads would carry the old bearer and move the gate's epoch away from the TUI.
A fresh process per read loads the current auth.json instead. It runs under the same boundary
(gate port only) with no proxy variables, so its own refresh attempt, if any, is denied by the
boundary instead of reaching the refresh host. The gate observes the forwarded usage GET; the
reader's own view of the result is diagnostic only.

    python3 -I -B reader.py CONFIG.json   (prints one JSON summary line)
"""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
BOUNDARY = HERE.parent / 'nl' / 'boundary.py'


def boundary_argv(python, gate_port, command):
    return [python, '-I', '-B', str(BOUNDARY), '--gate-port', str(gate_port), '--seccomp', 'inet', '--',
            *command]


def summarize(result):
    if not isinstance(result, dict) or 'rateLimits' not in result:
        return {'rpc_error': str((result or {}).get('error', result))[:300] if isinstance(result, dict) else 'none'}
    limits = result['rateLimits'] or {}
    windows = {}
    for slot in ('primary', 'secondary'):
        window = limits.get(slot)
        if isinstance(window, dict):
            windows[str(window.get('windowDurationMins'))] = window.get('usedPercent')
    return {'windows': windows, 'plan': limits.get('planType'), 'accountId': result.get('accountId'),
            'ordinaryUsageAllowed': result.get('ordinaryUsageAllowed')}


def read(config, timeout=10.0):
    """One bounded read; returns a summary with timings. Never raises for native failures."""
    started = time.monotonic()
    argv = boundary_argv(config['python'], config['gate_port'], [config['binary'], 'app-server'])
    stderr = open(config['stderr'], 'ab') if config.get('stderr') else subprocess.DEVNULL
    process = subprocess.Popen(argv, env=config['env'], cwd=config['cwd'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
    out = {'pid': process.pid, 'started': started}
    buffer = b''

    def send(message):
        process.stdin.write((json.dumps(message) + '\n').encode())
        process.stdin.flush()

    def receive(ident):
        nonlocal buffer
        end = started + timeout
        while time.monotonic() < end:
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get('id') == ident and 'method' not in message:
                    return message
            if select.select([process.stdout], [], [], .1)[0]:
                data = os.read(process.stdout.fileno(), 65536)
                if not data:
                    return None
                buffer += data
        return None

    try:
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'klx-controller', 'version': '1'},
                                                           'capabilities': {'experimentalApi': True}}})
        initialized = receive(1)
        out['init_seconds'] = round(time.monotonic() - started, 4)
        if initialized is None or 'result' not in initialized:
            out['error'] = 'initialize-failed'
            return out
        send({'method': 'initialized', 'params': {}})
        before = time.monotonic()
        send({'id': 2, 'method': 'account/rateLimits/read', 'params': {'excludeResetCreditDetails': True}})
        answer = receive(2)
        out['read_seconds'] = round(time.monotonic() - before, 4)
        out['result'] = summarize((answer or {}).get('result') or (answer or {}))
    except (OSError, ValueError) as error:
        out['error'] = type(error).__name__
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
        out['exit'] = process.returncode
        out['total_seconds'] = round(time.monotonic() - started, 4)
        if stderr is not subprocess.DEVNULL:
            stderr.close()
    return out


if __name__ == '__main__':
    print(json.dumps(read(json.loads(Path(sys.argv[1]).read_text()))), flush=True)
