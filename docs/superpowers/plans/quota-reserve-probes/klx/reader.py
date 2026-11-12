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


class Session:
    """One in-boundary native App Server over stdio pipes. A Session kept open across reads is the
    long-lived variant whose cached bearer goes stale after a native refresh elsewhere."""

    def __init__(self, config, timeout=10.0):
        self.started, self.timeout, self.buffer, self.next_id = time.monotonic(), timeout, b'', 0
        argv = boundary_argv(config['python'], config['gate_port'], [config['binary'], 'app-server'])
        self.stderr = open(config['stderr'], 'ab') if config.get('stderr') else subprocess.DEVNULL
        self.process = subprocess.Popen(argv, env=config['env'], cwd=config['cwd'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.stderr, start_new_session=True)
        self.pid = self.process.pid

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        self.process.stdin.flush()

    def receive(self, ident):
        end = time.monotonic() + self.timeout
        while time.monotonic() < end:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get('id') == ident and 'method' not in message:
                    return message
            if select.select([self.process.stdout], [], [], .1)[0]:
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    return None
                self.buffer += data
        return None

    def request(self, method, params):
        self.next_id += 1
        self.send({'id': self.next_id, 'method': method, 'params': params})
        return self.receive(self.next_id)

    def initialize(self):
        answer = self.request('initialize', {'clientInfo': {'name': 'klx-controller', 'version': '1'},
                                             'capabilities': {'experimentalApi': True}})
        if answer is None or 'result' not in answer:
            return False
        self.send({'method': 'initialized', 'params': {}})
        return True

    def read(self):
        before = time.monotonic()
        answer = self.request('account/rateLimits/read', {'excludeResetCreditDetails': True})
        return {'read_seconds': round(time.monotonic() - before, 4), 'started': before,
                'result': summarize((answer or {}).get('result') or (answer or {}))}

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=3)
        if self.stderr is not subprocess.DEVNULL:
            self.stderr.close()
        return self.process.returncode


def refresh(config, timeout=15.0):
    """Refresh option (a): one fresh native App Server asks native for a forced refresh
    (`account/read` with `refreshToken`, S v2/account.rs GetAccountParams). Returns timings and the
    account summary; native writes the refreshed credential itself."""
    session = Session(config, timeout)
    out = {'pid': session.pid, 'started': session.started}
    try:
        ok = session.initialize()
        out['init_seconds'] = round(time.monotonic() - session.started, 4)
        if not ok:
            out['error'] = 'initialize-failed'
        else:
            before = time.monotonic()
            answer = session.request('account/read', {'refreshToken': True})
            out['refresh_seconds'] = round(time.monotonic() - before, 4)
            result = (answer or {}).get('result')
            account = (result or {}).get('account') or {}
            out['result'] = ({'type': account.get('type'), 'planType': account.get('planType')} if result
                             else {'rpc_error': str((answer or {}).get('error', answer))[:300]})
    except (OSError, ValueError) as error:
        out['error'] = type(error).__name__
    finally:
        out['exit'] = session.close()
        out['total_seconds'] = round(time.monotonic() - session.started, 4)
    return out


def read(config, timeout=10.0):
    """One bounded read by a fresh process; returns a summary with timings. Never raises for
    native failures."""
    session = Session(config, timeout)
    out = {'pid': session.pid, 'started': session.started}
    try:
        ok = session.initialize()
        out['init_seconds'] = round(time.monotonic() - session.started, 4)
        if not ok:
            out['error'] = 'initialize-failed'
        else:
            row = session.read()
            out.update(read_seconds=row['read_seconds'], result=row['result'])
    except (OSError, ValueError) as error:
        out['error'] = type(error).__name__
    finally:
        out['exit'] = session.close()
        out['total_seconds'] = round(time.monotonic() - session.started, 4)
    return out


if __name__ == '__main__':
    print(json.dumps(read(json.loads(Path(sys.argv[1]).read_text()))), flush=True)
