"""Real Claude Code and Codex TUIs inside the NL research boundary; writes evidence/<native>-<layer>.json.

The native runs on its own PTY inside boundary.py (gate port only). A loopback
fake gate answers model requests with one shell tool call that runs attempt.py
(network and IPC attempts plus a setsid-detached child) from the native's own
tool process, then ends the turn. strace, outside the boundary, only observes
socket/connect/send/bind/listen syscalls of the whole invocation; it denies
nothing. Its per-process files also give the native's own exit status and the
PIDs that cleanup must reach, because the native runs in its own session.
Fabricated credentials only, in private temporary homes. No real provider.

python3 -I -B natives.py claude|codex BINARY inet|inet+unix [default|no-daemon|outside-daemon]
outside-daemon (Codex) first starts the native app-server daemon in a sibling
research domain that allows the gate port and the alternate port, to show what
happens when an invocation attaches to a daemon that it does not own.
"""
import base64
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('nl_probe', HERE / 'probe.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
PROMPT = 'NL boundary fixture prompt'
TRACE = re.compile(r'^(\w+)\((.*)\)\s+=\s+(-?\d+|\?)(.*)$')
EXIT = re.compile(r'^\+\+\+ (?:exited with (\d+)|killed by (\w+))')
NETWORK = ('socket', 'socketpair', 'connect', 'sendto', 'sendmsg', 'sendmmsg')
LISTEN = ('bind', 'listen', 'getsockname')
SPAWN = ('clone', 'clone3', 'fork', 'vfork')
DENIED = ('EACCES', 'EOPNOTSUPP', 'EPROTONOSUPPORT', 'EAFNOSUPPORT')

def jwt(payload):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()
    return encode({'alg': 'none'}) + '.' + encode(payload) + '.fixture-signature'


def parse_traces(files):
    """strace -ff files -> executables, network rows, exits, root PIDs and inet listeners."""
    exe, spawned, rows, exits, listeners = {}, [], [], {}, []
    for path in files:
        pid = path.name.rsplit('.', 1)[1]
        listening, named = set(), []
        for line in path.read_text(errors='replace').splitlines():
            ended = EXIT.match(line)
            if ended:
                exits[pid] = int(ended.group(1)) if ended.group(1) else ended.group(2)
                continue
            match = TRACE.match(line)
            if not match:
                continue
            call, args, value, rest = match.groups()
            ok = value not in ('-1', '?')
            if call == 'execve' and ok:
                name = re.match(r'"([^"]*)"', args)
                exe[pid] = os.path.basename(name.group(1)) if name else '?'
            elif call in SPAWN and ok and value.isdigit():
                spawned.append((pid, value))
            elif call in NETWORK:
                rows.append(classify(pid, call, args, 'ok' if ok else rest.split()[0]))
            elif call in LISTEN and ok:
                # Same thread, same descriptor: a bound or queried address that also listened.
                fd = args.split(',', 1)[0]
                if call == 'listen':
                    listening.add(fd)
                else:
                    row = classify(pid, call, args, 'ok')
                    if row['port']:
                        named.append((fd, row))
        listeners += [row for fd, row in named if fd in listening]
    for _ in range(len(spawned) + 1):
        changed = False
        for parent, child in spawned:
            if child not in exe and parent in exe:
                exe[child] = exe[parent]
                changed = True
        if not changed:
            break
    for row in rows + listeners:
        row['exe'] = exe.get(row['pid'], '?')
    roots = sorted({path.name.rsplit('.', 1)[1] for path in files} - {child for _, child in spawned})
    return {'exe': exe, 'rows': rows, 'exits': exits, 'roots': roots, 'listeners': listeners}


def classify(pid, call, args, result):
    row = {'pid': pid, 'call': call, 'result': result}
    family = re.search(r'(AF_[A-Z0-9]+)', args)
    row['family'] = family.group(1) if family else None
    if call in ('socket', 'socketpair'):
        kind = re.search(r'(SOCK_[A-Z]+)', args)
        row['type'] = kind.group(1) if kind else None
        parts = args.split(',')
        row['protocol'] = parts[2].strip() if len(parts) > 2 else None
    port = re.search(r'htons\((\d+)\)', args)
    address = re.search(r'inet_addr\("([^"]+)"\)|inet_pton\(AF_INET6, "([^"]+)"', args)
    row['port'] = int(port.group(1)) if port else None
    row['address'] = (address.group(1) or address.group(2)) if address else None
    path = re.search(r'sun_path=(@?)"([^"]*)"', args)
    if path:
        row['unix'] = 'abstract' if path.group(1) else 'path'
        row['unix_name'] = os.path.basename(path.group(2))[:40]
    return row


def start_time(pid):
    try:
        return int(Path('/proc/%s/stat' % pid).read_text().rsplit(')', 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def lingering(root, traced=(), since=None):
    """Live processes of this probe: the command line, executable, environment or cwd names
    the private root, or strace named the PID and it started after this probe under
    no_new_privs (a native with a cleared environment elsewhere, or its own session)."""
    marker, found = str(root).encode(), []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = (entry / 'cmdline').read_bytes()
            mine = marker in cmdline or marker in os.readlink(entry / 'exe').encode()
        except OSError:
            continue
        for read in (lambda: (entry / 'environ').read_bytes(), lambda: os.readlink(entry / 'cwd').encode()):
            try:
                mine = mine or marker in read()
            except OSError:
                pass
        if not mine and entry.name in traced and since is not None:
            started = start_time(entry.name)
            try:
                confined = 'NoNewPrivs:\t1' in (entry / 'status').read_text()
            except OSError:
                confined = False
            mine = confined and started is not None and started >= since
        if mine:
            found.append({'pid': int(entry.name), 'cmd': cmdline.replace(b'\0', b' ').decode(errors='replace')[:160]})
    return found


def summarize(rows, gate_port, alt_port, udp_port, exclude=(), listen_ports=()):
    out = {'socket_denied': {}, 'socket_allowed': {}, 'inet_connect': [], 'unix_connect': {}}
    for row in rows:
        if row['pid'] in exclude:
            continue
        who = row['exe']
        if row['call'] in ('socket', 'socketpair'):
            pair = 'pair ' if row['call'] == 'socketpair' else ''
            key = '%s %s%s/%s/%s' % (who, pair, row['family'], row['type'], row['protocol'])
            bucket = out['socket_allowed'] if row['result'] == 'ok' else out['socket_denied']
            bucket[key] = bucket.get(key, 0) + 1
        elif row['family'] in ('AF_INET', 'AF_INET6') and row['port'] is not None:
            loop = row['address'] in ('127.0.0.1', '::1', '::ffff:127.0.0.1', '127.0.0.2')
            target = ('gate' if row['port'] == gate_port and row['address'] in ('127.0.0.1', '::1')
                      else 'alt' if loop and row['port'] in (alt_port, udp_port)
                      else 'self_listener' if loop and row['port'] in listen_ports
                      else 'other_loopback' if loop else 'non_loopback')
            item = {'exe': who, 'call': row['call'], 'target': target, 'result': row['result']}
            if target not in ('gate', 'alt'):
                item.update(address=row['address'], port=row['port'])
            if item not in out['inet_connect']:
                out['inet_connect'].append(item)
        elif row['family'] == 'AF_UNIX' and row.get('unix'):
            key = '%s %s %s %s %s' % (who, row['call'], row['unix'], row['unix_name'], row['result'])
            out['unix_connect'][key] = out['unix_connect'].get(key, 0) + 1
    return out


def sse_claude(frames):
    return b''.join(('event: ' + f['type'] + '\ndata: ' + json.dumps(f) + '\n\n').encode() for f in frames)


def claude_frames(model, tool=None, text='NL fixture done'):
    start = {'type': 'message_start', 'message': {'id': 'msg_nl', 'type': 'message', 'role': 'assistant',
             'model': model, 'content': [], 'stop_reason': None, 'stop_sequence': None,
             'usage': {'input_tokens': 10, 'output_tokens': 0}}}
    if tool:
        block = [{'type': 'content_block_start', 'index': 0, 'content_block': {
                  'type': 'tool_use', 'id': 'tool_nl', 'name': 'Bash', 'input': {}}},
                 {'type': 'content_block_delta', 'index': 0, 'delta': {
                  'type': 'input_json_delta', 'partial_json': json.dumps(tool)}}]
        stop = 'tool_use'
    else:
        block = [{'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                 {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}}]
        stop = 'end_turn'
    return [start, *block, {'type': 'content_block_stop', 'index': 0},
            {'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None},
             'usage': {'output_tokens': 5}}, {'type': 'message_stop'}]


def codex_events(ident, call=None, text='NL fixture done'):
    item = ({'type': 'function_call', 'call_id': 'call-nl', 'name': 'exec_command',
             'arguments': json.dumps({'cmd': call, 'yield_time_ms': 15000})} if call else
            {'type': 'message', 'role': 'assistant', 'id': 'msg-%d' % ident,
             'content': [{'type': 'output_text', 'text': text}]})
    events = [{'type': 'response.created', 'response': {'id': 'resp-%d' % ident}},
              {'type': 'response.output_item.done', 'item': item},
              {'type': 'response.completed', 'response': {'id': 'resp-%d' % ident,
               'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}}]
    return b''.join(('data: ' + json.dumps(e) + '\n\n').encode() for e in events)


def main(native, binary, layer, daemon='default'):
    assert native in ('claude', 'codex') and layer in ('inet', 'inet+unix')
    assert daemon in ('default', 'no-daemon', 'outside-daemon')
    os.umask(0o077)
    since = start_time('self')
    root = Path(tempfile.mkdtemp(prefix='quota-nl-native-'))
    print('scratch', root, flush=True)
    for part in ('home', 'config', 'codex', 'cache', 'state', 'data', 'tmp', 'workspace', 'xdg'):
        (root / part).mkdir(mode=0o700)
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})
    fx = fixtures.Fixtures(root)
    fx.thread(fx.signals)
    events, output, processes = [], bytearray(), []
    lock = threading.Lock()
    start = time.monotonic()
    done = threading.Event()
    state = {'tool_sent': False, 'tool_result': None, 'requests': 0}
    master = slave = None
    result = {'native': native, 'layer': layer, 'codex_daemon': daemon if native == 'codex' else None}

    def record(event, **data):
        with lock:
            events.append({'at': round(time.monotonic() - start, 4), 'event': event, **data})
        print(event, json.dumps(data)[:300], flush=True)

    def wait_for(predicate, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(.05)
        return bool(predicate())

    fx.alt_port = fx.tcp_pair(b'ALT\n', 'alt_tcp')
    probe_udp = fixtures.socket.socket(fixtures.socket.AF_INET, fixtures.socket.SOCK_DGRAM)
    probe_udp.bind(('127.0.0.1', 0))
    udp_port = probe_udp.getsockname()[1]
    probe_udp.close()
    fx.udp(udp_port)
    fx.unix(str(root / 'daemon.sock'), 'daemon')
    abstract = 'quota-nl-' + fx.nonce
    fx.unix('\0' + abstract, 'abstract')
    fx.unix_dgram(str(root / 'dgram.sock'), 'unix_dgram')
    fx.unix_dgram('\0' + abstract + '-dgram', 'abstract_dgram')
    # Natives and their helpers get a recording stand-in bus, never the real session bus.
    (root / 'runtime').mkdir(mode=0o700)
    fake_bus = root / 'runtime' / 'bus'
    fx.unix(str(fake_bus), 'bus')
    runtime = os.environ.get('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid())

    class Gate(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def reply(self, code, body, kind='application/json'):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            record('gate_get', path=self.path.split('?')[0])
            if '/models' in self.path:
                self.reply(200, {'models': [], 'data': []})
            elif '/usage' in self.path:
                self.reply(200, {'plan_type': 'pro', 'rate_limit': {'allowed': True, 'limit_reached': False}})
            elif self.path.startswith('/api/codex/accounts/check'):
                self.reply(200, {'accounts': [{'id': 'fixture-account-nl', 'name': 'Fixture',
                                               'workspace_backend_origin': 'https://fixture.invalid',
                                               'account_routing_override': 'NO_CONSTRAINT'}]})
            else:
                self.reply(404, {'error': 'nl fixture'})

        def do_HEAD(self):
            record('gate_head', path=self.path.split('?')[0])
            self.reply(200, b'')

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', 0) or 0))
            try:
                body = json.loads(raw or b'{}')
            except ValueError:
                body = {}
            state['requests'] += 1
            ident = state['requests']
            auth = self.headers.get('Authorization', '')
            text = json.dumps(body)
            info = {'path': self.path.split('?')[0], 'fabricated_auth': 'NL-FABRICATED' in auth
                    or auth.startswith('Bearer ' + token[:12]), 'prompt': PROMPT in text}
            if native == 'claude' and self.path.startswith('/v1/messages'):
                tools = [t.get('name') for t in body.get('tools', [])]
                results = [c for m in body.get('messages', []) if isinstance(m.get('content'), list)
                           for c in m['content'] if isinstance(c, dict) and c.get('type') == 'tool_result']
                if results:
                    state['tool_result'] = json.dumps(results[-1].get('content'))[:6000]
                if 'Bash' in tools and info['prompt'] and not state['tool_sent'] and not results:
                    state['tool_sent'] = True
                    record('model_request', tool_call=True, **info)
                    payload = sse_claude(claude_frames(body.get('model'), {
                        'command': command, 'description': 'NL boundary fixture attempts', 'timeout': 60000}))
                else:
                    record('model_request', tool_result=bool(results), **info)
                    payload = sse_claude(claude_frames(body.get('model')))
                self.reply(200, payload, 'text/event-stream')
            elif native == 'codex' and self.path.endswith('/responses'):
                outputs = [i for i in body.get('input', []) if i.get('type') == 'function_call_output']
                if outputs:
                    state['tool_result'] = json.dumps(outputs[-1].get('output'))[:6000]
                if info['prompt'] and not state['tool_sent'] and not outputs \
                        and 'Generate a concise' not in text:
                    state['tool_sent'] = True
                    record('model_request', tool_call=True, **info)
                    payload = codex_events(ident, call=command)
                else:
                    record('model_request', tool_result=bool(outputs), **info)
                    payload = codex_events(ident)
                self.reply(200, payload, 'text/event-stream')
            else:
                record('gate_post_other', path=info['path'])
                self.reply(404, {'error': 'nl fixture'})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Gate)
    gate_port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = {'alt_port': fx.alt_port, 'gate_port': gate_port, 'udp_port': udp_port, 'remote_host': '127.0.0.2',
            'daemon_path': str(root / 'daemon.sock'), 'abstract_name': abstract,
            'dgram_path': str(root / 'dgram.sock'), 'abstract_dgram_name': abstract + '-dgram',
            'dbus_path': str(fake_bus), 'runtime_dir': str(root / 'runtime'), 'victim_pid': os.getpid(),
            'python': sys.executable, 'outdir': str(root), 'run': native.upper() + '-' + layer,
            'root_done': str(root / 'tool.done')}
    spec_path = root / 'spec.json'
    spec_path.write_text(json.dumps(base))
    me = '%s -I -B %s --spec %s' % (sys.executable, HERE / 'attempt.py', spec_path)
    command = ('%s --actor %s_tool --mode matrix > %s 2>&1; setsid -f %s --actor %s_detached --mode detach '
               '</dev/null >/dev/null 2>&1; echo NL tool finished'
               % (me, native, root / 'tool.jsonl', me, native))
    token = jwt({'email': 'fixture@example.invalid', 'sub': 'fixture-user', 'iat': int(time.time()),
                 'exp': int(time.time()) + 3600, 'https://api.openai.com/auth': {
                     'chatgpt_account_id': 'fixture-account-nl', 'chatgpt_plan_type': 'pro',
                     'chatgpt_user_id': 'fixture-user'}})
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(root / 'home'), 'TMPDIR': str(root / 'tmp'),
           'TERM': 'xterm-256color', 'LANG': 'C.UTF-8', 'USER': 'fixture', 'LOGNAME': 'fixture',
           'XDG_CONFIG_HOME': str(root / 'xdg'), 'XDG_CACHE_HOME': str(root / 'cache'),
           'XDG_STATE_HOME': str(root / 'state'), 'XDG_DATA_HOME': str(root / 'data'),
           'XDG_RUNTIME_DIR': str(root / 'runtime'), 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=' + str(fake_bus)}
    if native == 'claude':
        settings = {'hasCompletedOnboarding': True, 'theme': 'dark',
                    'projects': {str(root / 'workspace'): {'hasTrustDialogAccepted': True,
                                                           'hasCompletedProjectOnboarding': True}},
                    'oauthAccount': {'accountUuid': 'fixture-nl', 'organizationUuid': 'fixture-org',
                                     'emailAddress': 'nl@example.invalid'}}
        (root / 'config/.claude.json').write_text(json.dumps(settings))
        (root / 'config/.credentials.json').write_text(json.dumps({'claudeAiOauth': {
            'accessToken': 'NL-FABRICATED', 'refreshToken': 'NL-FABRICATED-REFRESH',
            'expiresAt': 4102444800000, 'scopes': ['user:profile', 'user:inference'],
            'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_5x'}}))
        env.update({'CLAUDE_CONFIG_DIR': str(root / 'config'), 'DISABLE_AUTOUPDATER': '1',
                    'ANTHROPIC_BASE_URL': 'http://127.0.0.1:%d' % gate_port,
                    'CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY': '1'})
        argv = [binary, '--model', 'claude-sonnet-4-6', '--permission-mode', 'default', '--tools', 'Bash',
                '--settings', '{"permissions":{"allow":["Bash"]}}', PROMPT]
    else:
        (root / 'codex/auth.json').write_text(json.dumps({
            'auth_mode': 'chatgpt', 'OPENAI_API_KEY': None,
            'tokens': {'id_token': token, 'access_token': token, 'refresh_token': 'fixture-refresh',
                       'account_id': 'fixture-account-nl'}, 'last_refresh': '2026-11-12T00:00:00Z'}))
        gate = 'http://127.0.0.1:%d' % gate_port
        (root / 'codex/config.toml').write_text(f'''model = "gpt-5.4"
model_provider = "gate"
chatgpt_base_url = "{gate}"
cli_auth_credentials_store = "file"
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"
check_for_update_on_startup = false
[model_providers.gate]
name = "gate"
base_url = "{gate}/backend-api/codex"
requires_openai_auth = true
supports_websockets = false
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
[features]
plugins = false
apps = false
memories = false
shell_snapshot = false
network_proxy = false
[analytics]
enabled = false
[projects."{root / 'workspace'}"]
trust_level = "trusted"
''')
        env.update({'CODEX_HOME': str(root / 'codex'), 'CODEX_SQLITE_HOME': str(root / 'codex')})
        argv = [binary, *(['--no-daemon'] if daemon == 'no-daemon' else []), '--no-alt-screen',
                '-C', str(root / 'workspace'), PROMPT]
    strace = shutil.which('strace')
    trace = root / 'strace.log'
    helper = ('import os,fcntl,termios,sys; os.setsid(); '
              'fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])')
    inner = [sys.executable, '-I', '-B', str(HERE / 'boundary.py'), '--gate-port', str(gate_port),
             '--seccomp', layer, '--verbose', '--', sys.executable, '-I', '-B', '-c', helper, *argv]
    # -q, not -qq: keep the per-process exit lines that give the native's own status.
    observer = [strace, '-ff', '-q', '-s', '96', '-e',
                'trace=execve,clone,clone3,fork,vfork,socket,socketpair,connect,sendto,sendmsg,sendmmsg,'
                'bind,listen,getsockname',
                '-e', 'signal=none', '-o', str(trace)] if strace else []
    sibling = [sys.executable, '-I', '-B', str(HERE / 'boundary.py'), '--gate-port', str(gate_port),
               '--extra-port', str(fx.alt_port), '--seccomp', 'inet+unix', '--']
    try:
        if native == 'codex' and daemon == 'outside-daemon':
            started = subprocess.run([*sibling, binary, 'app-server', 'daemon', 'start'], env=env,
                                     cwd=root / 'workspace', capture_output=True, text=True, timeout=40,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            result['outside_daemon_start'] = {'exit': started.returncode, 'stdout': started.stdout[-300:],
                                              'stderr': started.stderr[-300:]}
            result['outside_daemon_processes'] = len(lingering(root))
        master, slave = pty.openpty()
        before = termios.tcgetattr(slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
        process = subprocess.Popen([*observer, *inner], env=env, cwd=root / 'workspace', stdin=slave,
                                   stdout=slave, stderr=slave, close_fds=True, start_new_session=True)
        processes.append(process)

        def terminal():
            while not done.is_set():
                if not select.select([master], [], [], .1)[0]:
                    continue
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                output.extend(data)
                if b'\x1b[6n' in data:
                    os.write(master, b'\x1b[1;1R')
                if b'\x1b[c' in data:
                    os.write(master, b'\x1b[?1;2c')
        threading.Thread(target=terminal, daemon=True).start()
        tool = root / 'tool.jsonl'
        gone = lambda: process.poll() is not None  # a refusing native ends the waits early
        result['tool_started'] = wait_for(lambda: state['tool_sent'] or gone(), 45) and state['tool_sent']
        result['tool_finished'] = wait_for(lambda: (tool.exists() and 'signal_outside' in tool.read_text())
                                           or gone(), 60) and tool.exists() and 'signal_outside' in tool.read_text()
        result['tool_result_returned'] = wait_for(lambda: state['tool_result'] is not None or gone(), 30) \
            and state['tool_result'] is not None
        (root / 'tool.done').write_text('done')
        detached = root / ('detached-%s.jsonl' % base['run'])
        result['detached_finished'] = result['tool_finished'] and wait_for(
            lambda: detached.exists() and 'detached_done' in detached.read_text(), 20)
        result['native_alive_after_turn'] = process.poll() is None
        time.sleep(1)
        result['tool_rows'] = [json.loads(x) for x in tool.read_text().splitlines()
                               if x.startswith('{')] if tool.exists() else []
        result['detached_rows'] = [json.loads(x) for x in detached.read_text().splitlines()] \
            if detached.exists() else []
        result['tool_result_excerpt'] = (state['tool_result'] or '')[:1200]
        for key in (b'\x03', b'\x03', b'\x04'):
            if process.poll() is not None:
                break
            os.write(master, key)
            time.sleep(.6)
        result['native_exited_by_keys'] = wait_for(lambda: process.poll() is not None, 5)
        result['native_exit'] = process.poll()
        try:
            result['termios_restored'] = termios.tcgetattr(slave) == before
        except termios.error:
            result['termios_restored'] = None
    finally:
        done.set()
        if native == 'codex' and daemon != 'no-daemon':
            stopped = subprocess.run([*sibling, binary, 'app-server', 'daemon', 'stop'], env=env,
                                     cwd=root / 'workspace', capture_output=True, text=True, timeout=40,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            result['daemon_stop_exit'] = stopped.returncode
        traced = lambda: {path.name.rsplit('.', 1)[1] for path in root.glob('strace.log.*')}
        owned = {process.pid for process in processes}
        alive = [item for item in lingering(root, traced(), since) if item['pid'] not in owned]
        result['lingering_at_cleanup'] = len(alive)
        # Tracees first: the native has its own session, and a SIGKILLed strace detaches them.
        for item in alive:
            record('owned_cleanup', pid=item['pid'])
            try:
                os.kill(item['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in processes:
            if process.poll() is None:
                record('owned_cleanup', pid=process.pid)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        fx.close()
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        left = lingering(root, traced(), since)
        for item in left:
            try:
                os.kill(item['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        result['lingering_killed'] = len(left)
        if left:
            time.sleep(.5)
        result['lingering_after_kill'] = len(lingering(root, traced(), since))
        files = sorted(root.glob('strace.log.*'))
        parsed = parse_traces(files)
        exe, rows = parsed['exe'], parsed['rows']
        # The single PID no traced process spawned is the boundary process that became the native.
        roots = parsed['roots']
        result['native_main_exit'] = parsed['exits'].get(roots[0]) if len(roots) == 1 else None
        actors = {str(r['pid']) for r in result.get('tool_rows', []) + result.get('detached_rows', []) if 'pid' in r}
        listen_ports = {row['port'] for row in parsed['listeners'] if row['pid'] not in actors}
        listeners = []
        for row in parsed['listeners']:
            item = {'exe': row['exe'], 'address': row['address'], 'port': row['port']}
            if row['pid'] not in actors and item not in listeners:
                listeners.append(item)
        boundary_lines = [x for x in output.decode(errors='replace').splitlines() if 'boundary-applied' in x]
        visible = re.sub(r'\x1b(\[[0-9;?<>=]*[A-Za-z~]|\][^\x07\x1b]*(\x07|\x1b\\)|[78()][A-Z0-9]?)', '',
                         output.decode(errors='replace'))
        result.update({'gate_port_label': 'GATE_PORT', 'observer': 'strace' if strace else 'none',
                       'traced_processes': len(files), 'traced_executables': sorted(set(exe.values())),
                       'syscalls': summarize(rows, gate_port, fx.alt_port, udp_port, (), listen_ports),
                       'native_only_syscalls': summarize(rows, gate_port, fx.alt_port, udp_port, actors,
                                                         listen_ports),
                       'native_inet_listeners': listeners,
                       'events': events, 'fixture_events': fx.events, 'boundary_applied': boundary_lines[:1],
                       'terminal_markers': {'fixture_reply_visible': 'NL fixture done' in visible,
                                            'tool_output_visible': 'NL tool finished' in visible,
                                            'boundary_refusal_visible': 'boundary: refused' in visible},
                       'terminal_tail': re.sub(r'\S*/\S*', '<PATH>', visible)[-1200:]})
        text = json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True)
        for old, new in ((str(root), '<PROBE_ROOT>'), (runtime, '<RUNTIME_DIR>'), (str(HERE.parents[4]), '<WORKTREE>'),
                         (str(Path.home()), '<HOME>'),
                         (sys.executable, '<PYTHON>'), (binary, '<NATIVE_BINARY>')):
            text = text.replace(old, new)
        (HERE / 'evidence').mkdir(exist_ok=True)
        name = '%s-%s%s.json' % (native, layer.replace('+', '-'), '' if daemon == 'default' else '-' + daemon)
        (HERE / 'evidence' / name).write_text(text + '\n')
        shutil.rmtree(root, ignore_errors=True)
        print('removed', root, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1], str(Path(sys.argv[2]).resolve()), *sys.argv[3:5]))
