"""Two bounded native settings-reload cases; run only through run_reload.py."""
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time


def main(binary, root, pinned):
    os.umask(0o077)
    for name in ('.oauth_token', '.api_key'):
        try:
            (Path('/home/claude/.claude/remote') / name).lstat()
        except FileNotFoundError:
            continue
        raise RuntimeError('hosted credential path exists; refused')
    for name in ('home', 'config', 'workspace', 'tmp', 'anthropic', 'cache', 'state', 'data', 'xdg'):
        (root / name).mkdir(mode=0o700)
    events, servers = [], []
    stopped = threading.Event()
    output = bytearray()
    native = None
    reader = None
    master = slave = None
    began = time.monotonic()

    def record(kind, **data):
        events.append({'at': round(time.monotonic() - began, 4), 'event': kind, **data})

    def wait(predicate, seconds=12):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if predicate():
                return True
            time.sleep(.05)
        return bool(predicate())

    def handler(route):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.send_error(503)

            def do_POST(self):
                size = int(self.headers.get('Content-Length', 0))
                if size > 2_000_000:
                    self.send_error(413)
                    return
                body = json.loads(self.rfile.read(size) or b'{}')
                if self.path.split('?')[0] != '/v1/messages':
                    self.send_error(503)
                    return
                content = json.dumps(body.get('messages', []))
                marker = 'second' if 'ROUTING_SECOND' in content else 'first' if 'ROUTING_FIRST' in content else 'auxiliary'
                record('model', route=route, marker=marker,
                       fixture_bearer=self.headers.get('Authorization') == 'Bearer ROUTING-FABRICATED')
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                rows = [
                    {'type': 'message_start', 'message': {'id': 'msg_routing', 'type': 'message',
                     'role': 'assistant', 'model': body.get('model'), 'content': [],
                     'stop_reason': None, 'stop_sequence': None,
                     'usage': {'input_tokens': 10, 'output_tokens': 0}}},
                    {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                    {'type': 'content_block_delta', 'index': 0,
                     'delta': {'type': 'text_delta', 'text': 'Fixture completed.'}},
                    {'type': 'content_block_stop', 'index': 0},
                    {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                     'usage': {'output_tokens': 3}},
                    {'type': 'message_stop'},
                ]
                try:
                    for row in rows:
                        self.wfile.write(('event: ' + row['type'] + '\ndata: ' + json.dumps(row) + '\n\n').encode())
                    self.wfile.flush()
                except OSError:
                    pass
        return Handler

    try:
        endpoints = {}
        for route in ('gate', 'alternate'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(route))
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            endpoints[route] = 'http://127.0.0.1:' + str(server.server_port)
        account = {'hasCompletedOnboarding': True, 'theme': 'dark',
                   'projects': {str(root / 'workspace'): {'hasTrustDialogAccepted': True,
                                'hasCompletedProjectOnboarding': True}},
                   'oauthAccount': {'accountUuid': 'fixture-routing', 'organizationUuid': 'fixture-org',
                                    'emailAddress': 'routing@example.invalid'}}
        (root / 'config/.claude.json').write_text(json.dumps(account))
        credential = root / 'config/.credentials.json'
        credential.write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'ROUTING-FABRICATED', 'refreshToken': 'ROUTING-FABRICATED-REFRESH',
            'expiresAt': 4102444800000, 'scopes': ['user:profile', 'user:inference'],
            'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_5x'}}))
        credential_digest = hashlib.sha256(credential.read_bytes()).hexdigest()
        settings_file = root / 'config/settings.json'
        settings_file.write_text('{}')
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'),
               'CLAUDE_CONFIG_DIR': str(root / 'config'), 'ANTHROPIC_CONFIG_DIR': str(root / 'anthropic'),
               **{'XDG_' + part.upper() + '_HOME': str(root / part)
                  for part in ('cache', 'state', 'data')}, 'XDG_CONFIG_HOME': str(root / 'xdg'),
               'TMPDIR': str(root / 'tmp'), 'USER': 'fixture', 'LOGNAME': 'fixture',
               'TERM': 'xterm-256color', 'LANG': 'C.UTF-8', 'DISABLE_AUTOUPDATER': '1',
               'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
               'CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY': '1', 'ANTHROPIC_BASE_URL': endpoints['gate']}
        flags = {'permissions': {'deny': ['*']}}
        if pinned:
            flags['env'] = {'ANTHROPIC_BASE_URL': endpoints['gate']}
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        helper = ('import os,fcntl,termios,sys; os.setsid(); '
                  'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')
        native = subprocess.Popen([sys.executable, '-I', '-B', '-c', helper, binary,
            '--model', 'claude-sonnet-4-6', '--settings', json.dumps(flags), 'ROUTING_FIRST'],
            env=env, cwd=root / 'workspace', stdin=slave, stdout=slave, stderr=slave, close_fds=True)

        def read():
            while not stopped.is_set():
                if not select.select([master], [], [], .1)[0]:
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if len(output) + len(data) > 2_000_000:
                    return
                output.extend(data)
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
                if b'\x1b[c' in data:
                    os.write(master, b'\x1b[?1;2c')
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        assert wait(lambda: any(e.get('marker') == 'first' for e in events)), 'first turn missing'
        assert all(e['route'] == 'gate' for e in events if e['event'] == 'model')
        time.sleep(1)
        settings_file.write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': endpoints['alternate']}}))
        record('user_settings_changed')
        time.sleep(4)
        os.write(master, b'ROUTING_SECOND\r')
        assert wait(lambda: any(e.get('marker') == 'second' for e in events)), 'second turn missing'
        actual = {e['route'] for e in events if e.get('marker') == 'second'}
        assert actual == ({'gate'} if pinned else {'alternate'}), actual
        assert all(e['fixture_bearer'] for e in events if e['event'] == 'model')
        assert credential_digest == hashlib.sha256(credential.read_bytes()).hexdigest()
        record('passed', pinned=pinned, native_alive=native.poll() is None)
        time.sleep(.5)
        os.write(master, b'/exit\r')
        native.wait(timeout=5)
        record('native_exit', code=native.returncode, termios_restored=termios.tcgetattr(slave) == before)
        assert native.returncode == 0
    finally:
        if native and native.poll() is None:
            os.killpg(native.pid, signal.SIGTERM)
            try:
                native.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(native.pid, signal.SIGKILL)
                native.wait(timeout=2)
        stopped.set()
        if master is not None:
            if reader is not None:
                reader.join(timeout=1)
            os.close(master)
            os.close(slave)
        for server in servers:
            server.shutdown()
            server.server_close()
        (root / 'events.json').write_text(json.dumps(events, indent=2) + '\n')
        (root / 'terminal.bin').write_bytes(output)


if __name__ == '__main__':
    main(str(Path(sys.argv[1]).resolve()), Path(sys.argv[2]), sys.argv[3] == 'pinned')
