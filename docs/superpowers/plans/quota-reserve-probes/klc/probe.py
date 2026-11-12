"""KLC native cases: real Claude 2.1.322 in a PTY inside L2a behind the composed gate.

python3 -I -B probe.py CASE BINARY      (invoke through run.py)

Each case builds one private temporary root under /tmp (quota-klc-CASE-*),
fabricated credentials, the QC fake usage/profile server, a fake Messages
upstream and, where needed, a disposable-CA refresh fixture; it then runs the
launcher order of compose.py and drives the native TUI. All fixtures are
loopback; the native and its descendants can reach only the gate port.
Writes sanitized evidence/CASE.json and archives raw rows under
<worktree>/.cache/klc/runs/. Header values and tokens are never recorded,
only fixture labels.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tty

HERE = Path(os.path.realpath(__file__)).parent
PROBES = HERE.parent
WT = HERE.parents[4]
CACHE = WT / '.cache' / 'klc'
PC5_CONTROL_SHA256 = '8b98eee39bd83b7769356db3fb49bcfefe8423da28cb4942430a9bad5e218dad'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compose = load('klc_compose', HERE / 'compose.py')
fixtures = load('klc_fixtures', HERE / 'fixtures.py')
natives = load('klc_nl_natives', PROBES / 'nl' / 'natives.py')
fake = load('klc_qc_fake_usage', PROBES / 'qc' / 'fake_usage.py')

A = 'KLC-FABRICATED-ACCESS-A-NOT-A-CREDENTIAL'
A2 = 'KLC-FABRICATED-ACCESS-A2-NOT-A-CREDENTIAL'
B = 'KLC-FABRICATED-ACCESS-B-NOT-A-CREDENTIAL'
R_A = 'KLC-FABRICATED-REFRESH-A-NOT-A-CREDENTIAL'
R_A2 = 'KLC-FABRICATED-REFRESH-A2-NOT-A-CREDENTIAL'
R_B = 'KLC-FABRICATED-REFRESH-B-NOT-A-CREDENTIAL'
LABELS = {A: 'A', A2: 'A2', B: 'B'}
IDENT_A, IDENT_B = ('fixture-account-a', 'fixture-org-a'), ('fixture-account-b', 'fixture-org-b')
FAR = 4102444800000
MODEL = 'claude-sonnet-4-6'
POLICY = {'reserves': {'five_hour': 20, 'weekly': 10}, 'max_age_s': 4.0, 'revalidations_per_minute': 4,
          'allowed_models': [MODEL]}
PY = sys.executable
CHILD = HERE / 'child.py'
HOLD = ('hold', 'KLC held stream. ')
AGENT_CHILD = ('tool', 'Agent', {'description': 'KLC child', 'prompt': 'KLC child', 'subagent_type': 'general-purpose'})
AGENT_GRANDCHILD = ('tool', 'Agent', {'description': 'KLC grandchild', 'prompt': 'KLC grandchild',
                                      'subagent_type': 'general-purpose'})


def bash(command, background=False, description='KLC fixture command'):
    args = {'command': command, 'description': description, 'timeout': 120000}
    if background:
        args['run_in_background'] = True
    return ('tool', 'Bash', args)


def script(plan):
    def choose(info):
        if info['kind'] == 'sdk':
            return ('text', 'KLC sdk ok')
        if info['later'] or info['continued']:
            return ('text', 'KLC later reply')
        actions = plan.get(info['kind'])
        if not actions:
            return ('text', 'KLC fixture reply')
        return actions[min(info['tool_results'], len(actions) - 1)]
    return choose


class TimedUsage(fake.FakeUsageServer):
    def record(self, method, path, label, beta=None):
        with self._lock:
            self.requests.append({'method': method, 'path': path, 'account': label, 't': time.monotonic()})


def write_private(path, document):
    staging = path.with_name('.klc-staging-' + path.name)
    fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(document, handle)
    os.rename(staging, path)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Case:
    def __init__(self, name, binary):
        os.umask(0o077)
        self.name, self.binary = name, binary
        CACHE.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='quota-klc-%s-' % name, dir='/tmp'))
        with (CACHE / 'temp-roots.txt').open('a') as handle:
            handle.write(str(self.root) + '\n')
        self.lock = threading.Lock()
        self.rows = []
        self.since = natives.start_time('self')
        self.usage = TimedUsage()
        self.usage.__enter__()
        self.servers, self.invocations, self.upstreams = [self.usage], [], []
        self.out_dir = self.root / 'out'
        self.out_dir.mkdir(mode=0o700)
        self.usage_accounts = {}
        self.result = {'case': self.name, 'native_sha256': sha256(binary) if binary else None,
                       'policy': POLICY, 'sanitized': True}

    def record(self, row_type, /, **fields):
        fields.setdefault('t', time.monotonic())
        with self.lock:
            self.rows.append({'row': row_type, **fields})

    def account(self, token=A, refresh=R_A, expires=FAR, ident=IDENT_A, name='alpha'):
        config = self.root / 'accounts' / name / 'config'
        config.mkdir(parents=True, mode=0o700)
        for path in (self.root / 'accounts', config.parent, config):
            path.chmod(0o700)
        self.store(config, token, refresh, expires)
        self.metadata(config, ident, [])
        return str(config)

    def store(self, config, token, refresh, expires):
        write_private(Path(config) / '.credentials.json', {'claudeAiOauth': {
            'accessToken': token, 'refreshToken': refresh, 'expiresAt': expires,
            'scopes': ['user:inference', 'user:profile', 'user:sessions:claude_code'],
            'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_5x'}})

    def metadata(self, config, ident, workspaces):
        write_private(Path(config) / '.claude.json', {
            'hasCompletedOnboarding': True, 'theme': 'dark',
            'projects': {str(w): {'hasTrustDialogAccepted': True, 'hasCompletedProjectOnboarding': True}
                         for w in workspaces},
            'oauthAccount': {'accountUuid': ident[0], 'organizationUuid': ident[1],
                             'emailAddress': 'fixture@example.invalid'}})

    def store_label(self, config):
        try:
            token = json.loads((Path(config) / '.credentials.json').read_text())['claudeAiOauth']['accessToken']
        except (OSError, ValueError, KeyError):
            return 'unreadable'
        return LABELS.get(token, 'empty' if token == '' else 'other')

    def usage_account(self, token, ident, five=40, week=30, label=None):
        account = fake.FakeAccount(label or LABELS[token], ident[0], ident[1], fake.windows(five, week))
        self.usage.register(token, account)
        self.usage_accounts[token] = account
        return account

    def set_usage(self, five, week=30, tokens=None):
        for token, account in self.usage_accounts.items():
            if tokens is None or token in tokens:
                account.body = fake.windows(five, week)
        self.record('usage_set', five_hour_utilization=five, seven_day_utilization=week)

    def upstream(self, chooser):
        upstream = fixtures.Upstream(LABELS, chooser, self.record)
        self.upstreams.append(upstream)
        return upstream

    def invocation(self, name, config, prompt, policy=None, **kwargs):
        upstream = self.upstreams[0]
        inv = compose.Invocation(self.root, name, self.record, self.binary, config_dir=config, registered=IDENT_A,
                                 usage_port=self.usage.port, upstream_port=upstream.port, policy=policy or POLICY,
                                 prompt=prompt, **kwargs)
        self.invocations.append(inv)
        return inv

    def trust(self, config, invocations, ident=IDENT_A):
        workspaces = [inv.native_env()[1] / 'workspace' for inv in invocations]
        self.metadata(config, ident, workspaces)

    def spec(self, inv, config, **extra):
        path = self.root / ('spec-%s.json' % inv.name)
        document = {'gate_port': inv.port, 'upstream_port': self.upstreams[0].port, 'config_dir': config,
                    'labels': LABELS, 'out_dir': str(self.out_dir), 'work_dir': str(self.root / 'work')}
        document.update(extra)
        path.write_text(json.dumps(document))
        return path

    def child_cmd(self, spec, mode, name, *extra):
        return '%s -I -B %s %s --spec %s --name %s %s >/dev/null 2>&1' % (PY, CHILD, mode, spec, name, ' '.join(extra))

    def boundary_actor(self, inv, spec, mode, name, *extra, timeout=30):
        """The same L2a domain shape as the native (gate port only), outside the native tree."""
        argv = [PY, '-I', '-B', str(PROBES / 'nl' / 'boundary.py'), '--gate-port', str(inv.port), '--seccomp',
                'inet', '--', PY, '-I', '-B', str(CHILD), mode, '--spec', str(spec), '--name', name, *extra]
        env, _ = inv.native_env()
        started = time.monotonic()
        done = subprocess.run(argv, env=env, cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, start_new_session=True)
        self.record('boundary_actor', name=name, mode=mode, exit=done.returncode, stderr=done.stderr[-200:],
                    t=started)
        return self.child_rows(name)

    def child_rows(self, name=None):
        rows = []
        for path in sorted(self.out_dir.glob('*.jsonl')):
            for line in path.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if name is None or row.get('actor') == name:
                    rows.append(row)
        return sorted(rows, key=lambda r: r['t'])

    def events(self, kind=None, **match):
        with self.lock:
            return [r for r in self.rows if (kind is None or r['row'] == kind)
                    and all(r.get(k) == v for k, v in match.items())]

    def gate(self, inv, event, **match):
        return [e for e in inv.gate_events(event) if all(e.get(k) == v for k, v in match.items())]

    def latch(self, inv):
        rows = self.gate(inv, 'controller', kind='latched')
        return rows[0] if rows else None

    def finish(self, outcome):
        for upstream in self.upstreams:
            upstream.done.set()
        cleanup = {}
        for inv in self.invocations:
            cleanup[inv.name] = inv.close(natives, self.since)
        for upstream in self.upstreams:
            upstream.close()
        for server in self.servers:
            if hasattr(server, '__exit__'):
                server.__exit__(None, None, None)
            else:
                server.close()
        time.sleep(0.3)
        left = natives.lingering(self.root)
        for item in left:
            try:
                os.kill(item['pid'], signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.result.update(outcome)
        self.result['cleanup'] = cleanup
        self.result['lingering_after_cleanup'] = len(left)
        self.result['usage_requests'] = [{'path': r['path'], 'account': r['account'], 't': r['t']}
                                         for r in self.usage.requests]
        self.result['child_rows'] = self.child_rows()
        self.result['rows'] = self.rows
        self.result['terminal'] = {inv.name: sanitize_terminal(inv.visible()) for inv in self.invocations
                                   if inv.native is not None}
        archive = CACHE / 'runs' / ('%s-%d' % (self.name, int(time.time())))
        archive.mkdir(parents=True, exist_ok=True)
        (archive / 'raw.json').write_text(json.dumps(self.result, indent=1, default=str))
        for inv in self.invocations:
            if inv.native is not None:
                (archive / ('terminal-%s.txt' % inv.name)).write_text(inv.visible())
        text = sanitize(json.dumps(self.result, indent=1, sort_keys=True, default=str), self.root, self.binary)
        (HERE / 'evidence').mkdir(exist_ok=True)
        (HERE / 'evidence' / (self.name + '.json')).write_text(text + '\n')
        shutil.rmtree(self.root, ignore_errors=True)
        print('removed', self.root, flush=True)


def sanitize(text, root, binary):
    pairs = [(str(root), '<PROBE_ROOT>'), (str(WT), '<WORKTREE>'), (os.path.realpath(PY), '<PYTHON>'), (PY, '<PYTHON>')]
    if binary:
        pairs.append((binary, '<NATIVE_BINARY>'))
    pairs += [(str(Path.home()), '<HOME>'), ('/run/user/%d' % os.getuid(), '<RUNTIME_DIR>')]
    for old, new in sorted(pairs, key=lambda p: -len(p[0])):
        text = text.replace(old, new)
    return text


def sanitize_terminal(text):
    text = re.sub(r'\S*/\S*', '<PATH>', text)
    return re.sub(r'\s+', ' ', text)[-2500:]


def launch(case, inv, extra_args=()):
    ready = inv.start_gate()
    case.record('launch', inv=inv.name, gate_event=ready.get('event'), reason=ready.get('reason'))
    if ready.get('event') != 'gate_ready':
        return False
    inv.start_monitor()
    inv.start_native(extra_args)
    return True


def sdk_status(case, name, status=200):
    return [r for r in case.child_rows(name) if r.get('attempt') == 'model_request' and r.get('status') == status]


def later_prompt(case, inv, text=b'KLC later prompt'):
    """History view, then a new prompt through native send-now; returns typed time."""
    inv.write(b'\x0f')
    time.sleep(0.6)
    history_alive = inv.native_alive()
    inv.write(b'\x0f')
    time.sleep(0.3)
    typed = time.monotonic()
    inv.write(b'\x15' + text + b'\r')
    time.sleep(0.3)
    inv.write(b'\x18\x13')
    case.record('later_prompt', inv=inv.name, history_view_alive=history_alive, t=typed)
    return typed


def native_requests_after(inv, since, ok=None):
    return [d for d in inv.gate_events('decision') if d['t'] > since and (d.get('tools') or 0) > 0
            and (ok is None or d['ok'] == ok)]


def summary(case, inv, upstream):
    latch = case.latch(inv)
    forwarded = inv.gate_events('forwarded')
    connects = {}
    for row in inv.gate_events('connect'):
        key = '%s %s' % (row['target'], 'ok' if row['ok'] else 'refused')
        connects[key] = connects.get(key, 0) + 1
    return {'latch': {k: latch[k] for k in ('reason', 't')} if latch else None,
            'forwarded': len(forwarded), 'forwarded_after_latch': sum(1 for f in forwarded if latch and f['t'] > latch['t']),
            'upstream_requests': upstream.count, 'connects': connects,
            'decisions_refused': sum(1 for d in inv.gate_events('decision') if not d['ok'])}


# -- cases ---------------------------------------------------------------------------------
def case_threshold(case):
    """Cases 1 and 2 plus a native subagent chain: threshold latch mid-run."""
    config = case.account()
    account = case.usage_account(A, IDENT_A)
    plan = {'root': [None, AGENT_CHILD, HOLD], 'child': [AGENT_GRANDCHILD, HOLD], 'grandchild': [HOLD]}
    upstream = case.upstream(script(plan))
    inv = case.invocation('main', config, 'KLC root threshold')
    spec = case.spec(inv, config, sdk_duration=45)
    plan['root'][0] = bash(case.child_cmd(spec, 'sdk', 'sdk', '--detach', '1'), background=True,
                           description='KLC sdk child')
    case.trust(config, [inv])
    if not launch(case, inv):
        return {'launched': False}
    held = fixtures.wait_for(lambda: len(upstream.held(['root', 'child', 'grandchild'])) >= 3, 60)
    sdk_ready = fixtures.wait_for(lambda: len(sdk_status(case, 'sdk')) >= 2 and sdk_status(case, 'sdk-grandchild'), 30)
    held_ids = upstream.held()
    case.record('ready', held=held, held_ids=held_ids, sdk_ready=bool(sdk_ready))
    trigger = time.monotonic()
    account.body = fake.windows(85, 30)
    case.record('trigger', what='usage_crosses_reserve', t=trigger)
    fixtures.wait_for(lambda: case.latch(inv), 6)
    closed = fixtures.wait_for(lambda: not set(held_ids) & set(upstream.held()), 6)
    time.sleep(3)
    typed = later_prompt(case, inv)
    fixtures.wait_for(lambda: native_requests_after(inv, typed), 10)
    time.sleep(1)
    alive = inv.native_alive()
    exit_info = inv.exit_native()
    return {'launched': True, 'held_before_trigger': held, 'held_ids': held_ids, 'trigger_t': trigger,
            'held_closed': closed, 'native_alive_after_pause': alive, 'later_typed_t': typed,
            'later_refused': bool(native_requests_after(inv, typed, ok=False)),
            'later_admitted': bool(native_requests_after(inv, typed, ok=True)), 'exit': exit_info,
            'summary': summary(case, inv, upstream)}


def loss_case(case, mode):
    config = case.account()
    case.usage_account(A, IDENT_A)
    plan = {'root': [None, HOLD]}
    upstream = case.upstream(script(plan))
    extra = {}
    proxy = None
    if mode == 'eof':
        proxy = fixtures.ConnectProxy(case.record)
        case.servers.append(proxy)
        extra['upstream_proxy'] = ['127.0.0.1', proxy.port]
    inv = case.invocation('main', config, 'KLC root loss', **extra)
    spec = case.spec(inv, config, sdk_duration=40)
    plan['root'][0] = bash(case.child_cmd(spec, 'sdk', 'sdk'), background=True, description='KLC sdk child')
    case.trust(config, [inv])
    if not launch(case, inv):
        return {'launched': False}
    held = fixtures.wait_for(lambda: upstream.held(['root']), 45)
    fixtures.wait_for(lambda: len(sdk_status(case, 'sdk')) >= 2, 20)
    held_ids = upstream.held()
    trigger = time.monotonic()
    if mode == 'kill':
        inv.kill_monitor()
    else:
        inv.monitor_command(mode)
    fixtures.wait_for(lambda: case.latch(inv), POLICY['max_age_s'] + 4)
    closed = fixtures.wait_for(lambda: not set(held_ids) & set(upstream.held()), 6)
    time.sleep(2.5)
    typed = later_prompt(case, inv)
    fixtures.wait_for(lambda: native_requests_after(inv, typed), 10)
    time.sleep(1)
    alive = inv.native_alive()
    exit_info = inv.exit_native()
    return {'launched': True, 'mode': mode, 'held_before_trigger': bool(held), 'held_ids': held_ids,
            'trigger_t': trigger, 'held_closed': closed, 'native_alive_after_pause': alive,
            'later_typed_t': typed, 'later_refused': bool(native_requests_after(inv, typed, ok=False)),
            'later_admitted': bool(native_requests_after(inv, typed, ok=True)), 'exit': exit_info,
            'upstream_proxy_tunnels': proxy.tunnels if proxy else None, 'summary': summary(case, inv, upstream)}


def case_loss_kill(case):
    return loss_case(case, 'kill')


def case_loss_eof(case):
    return loss_case(case, 'eof')


def case_loss_stall(case):
    return loss_case(case, 'stall')


def try_bind(port):
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    try:
        sock.bind(('127.0.0.1', port))
        sock.listen(4)
        return sock, 'bound'
    except OSError as error:
        sock.close()
        return None, natives_errno(error)


def natives_errno(error):
    import errno
    return errno.errorcode.get(error.errno, str(error.errno))


def case_gate_death(case):
    """Case 4: gate death, port holder, restart without reopening, holder-loss gap."""
    config = case.account()
    case.usage_account(A, IDENT_A)
    plan = {'root': [None, HOLD]}
    upstream = case.upstream(script(plan))
    inv = case.invocation('main', config, 'KLC root gate death')
    spec = case.spec(inv, config, sdk_duration=75, bind_hold=4)
    plan['root'][0] = bash(case.child_cmd(spec, 'sdk', 'sdk'), background=True, description='KLC sdk child')
    case.trust(config, [inv])
    if not launch(case, inv):
        return {'launched': False}
    fixtures.wait_for(lambda: upstream.held(['root']), 45)
    fixtures.wait_for(lambda: len(sdk_status(case, 'sdk')) >= 2, 20)
    held_ids = upstream.held()
    first_pid = inv.gate.pid
    inv.kill_gate()
    killed = time.monotonic()
    closed = fixtures.wait_for(lambda: not set(held_ids) & set(upstream.held()), 6)
    count_at_kill = upstream.count
    in_rows = case.boundary_actor(inv, spec, 'bind', 'bind-holder')
    impostor, outside = try_bind(inv.port)
    if impostor:
        impostor.close()
    time.sleep(2.5)
    count_dead = upstream.count
    ready = inv.start_gate('restart')
    inv.start_monitor()
    restarted = time.monotonic()
    time.sleep(2.5)
    refused_after_restart = [d for d in inv.gate_events('decision') if d['t'] > restarted and not d['ok']]
    count_before_continue = upstream.count
    reply = inv.control('continue')
    continued = time.monotonic()
    fixtures.wait_for(lambda: [d for d in inv.gate_events('decision') if d['t'] > continued and d['ok']], 6)
    time.sleep(1)
    count_after_continue = upstream.count
    second_pid = inv.gate.pid
    # Holder loss (the gap the holder closes): gate dead and the listening socket released.
    inv.kill_gate()
    inv.listener.close()
    released = time.monotonic()
    case.record('holder_released', t=released)
    gap_rows = case.boundary_actor(inv, spec, 'bind', 'bind-gap')
    impostor, outside_gap = try_bind(inv.port)
    reached = None
    if impostor:
        impostor.settimeout(4)
        try:
            peer, _ = impostor.accept()
            peer.settimeout(2)
            reached = peer.recv(256).split(b'\r\n', 1)[0].decode('latin-1')[:40]
            peer.close()
        except OSError as error:
            reached = 'none:' + natives_errno(error) if getattr(error, 'errno', None) else 'none:timeout'
        impostor.close()
    case.record('impostor', bind=outside_gap, reached=reached)
    alive = inv.native_alive()
    exit_info = inv.exit_native()
    return {'launched': True, 'held_ids': held_ids, 'held_closed_on_gate_death': closed, 'killed_t': killed,
            'gate_pids_differ': first_pid != second_pid, 'restart_mode': ready.get('mode'),
            'in_boundary_bind_with_holder': [r.get('result') for r in in_rows if r['attempt'] == 'bind_gate_port'],
            'outside_bind_with_holder': outside, 'upstream_count_at_kill': count_at_kill,
            'upstream_count_while_dead': count_dead, 'refused_after_restart': len(refused_after_restart),
            'upstream_count_before_continue': count_before_continue, 'continue_reply': reply,
            'upstream_count_after_continue': count_after_continue,
            'in_boundary_bind_without_holder': [r.get('result') for r in gap_rows if r['attempt'] == 'bind_gate_port'],
            'in_boundary_accepted_without_holder': [r.get('request_line') or r.get('result') for r in gap_rows
                                                    if r['attempt'] == 'bind_accepted'],
            'outside_bind_without_holder': outside_gap, 'outside_impostor_reached_by': reached,
            'native_alive': alive, 'exit': exit_info, 'summary': summary(case, inv, upstream)}


def case_continuation(case):
    """Case 5: trusted-terminal continuation (PC5 parser, pinned) on the same invocation."""
    source = PROBES / 'pc_continue' / 'control.py'
    digest = sha256(source)
    assert digest == PC5_CONTROL_SHA256, 'PC5 control changed; review before reuse'
    control_mod = load('klc_pc5_control', source)
    config = case.account()
    account = case.usage_account(A, IDENT_A)
    plan = {'root': [None, HOLD]}
    upstream = case.upstream(script(plan))
    outer_master, outer_slave = pty.openpty()
    tty.setraw(outer_slave)
    outer = bytearray()
    done = threading.Event()

    def relay(data):
        try:
            os.write(outer_slave, data)
        except OSError:
            pass

    def read_outer():
        while not done.is_set():
            if select.select([outer_master], [], [], 0.1)[0]:
                try:
                    outer.extend(os.read(outer_master, 65536))
                except OSError:
                    return

    threading.Thread(target=read_outer, daemon=True).start()
    inv = case.invocation('main', config, 'KLC root continuation', on_output=relay)
    guesses = ['%08x' % n for n in range(40)]
    spec = case.spec(inv, config, sdk_duration=70, tty_delay=4, tty_interval=0.7, tty_guesses=guesses)
    plan['root'][0] = bash('%s & %s & wait' % (case.child_cmd(spec, 'sdk', 'sdk'),
                                               case.child_cmd(spec, 'ttywrite', 'ttywrite')),
                           background=True, description='KLC background wakeups')
    case.trust(config, [inv])
    checks = []

    def check():
        reply = inv.control('continue')
        checks.append({'t': time.monotonic(), 'ok': reply.get('ok'), 'reason': reply.get('reason')})
        return bool(reply.get('ok'))

    def display(message):
        os.write(outer_slave, ('\r\n' + message + '\r\n').encode())
        case.record('control_display', message=message)

    control = control_mod.TerminalControl(outer_slave, inv.write, display, check,
                                          lambda: case.record('grant'))

    def enter(data):
        os.write(outer_master, data)
        for _ in range(4):
            control.pump(0.05)

    if not launch(case, inv):
        done.set()
        return {'launched': False}
    fixtures.wait_for(lambda: upstream.held(['root']), 45)
    fixtures.wait_for(lambda: len(sdk_status(case, 'sdk')) >= 2, 20)
    gate_pid, native_pid = inv.gate.pid, inv.native.pid
    os.write(outer_master, control_mod.KEY + b'old')  # typed before the pause, never pumped
    account.body = fake.windows(85, 30)
    case.record('trigger', what='usage_crosses_reserve')
    fixtures.wait_for(lambda: case.latch(inv), 6)
    control.pause()
    control.pump(0)
    stale_ignored = control.challenge is None and not checks
    enter(control_mod.KEY)
    challenge = control.challenge
    enter(control_mod.PASTE_START + (challenge or '').encode() + b'\r' + control_mod.PASTE_END)
    paste_ignored = not checks
    # Native output side: bytes on the native terminal's output (as if the native printed the
    # key and the current challenge) reach the outer display only, never the parser input.
    enter(control_mod.KEY)
    pending = control.challenge
    for _ in range(3):
        os.write(inv.slave, control_mod.KEY + (pending or '').encode() + b'\r\n')
        time.sleep(0.2)
        control.pump(0.05)
    output_ignored = not checks and control.challenge == pending
    output_on_display = (control_mod.KEY + (pending or '').encode()) in outer
    account.body = fake.windows(40, 30)  # passive recovery
    case.record('usage_set', five_hour_utilization=40, passive=True)
    time.sleep(3.5)
    passive = inv.control('status')
    tty_rows = [r for r in case.child_rows('ttywrite') if r['attempt'] == 'ttywrite']
    sdk_paused = [r for r in case.child_rows('sdk') if r['attempt'] == 'model_request'
                  and r['t'] > case.latch(inv)['t']]
    account.body = fake.windows(85, 30)
    enter(control_mod.KEY)
    enter((control.challenge or '').encode() + b'\r')
    failing = list(checks)
    still = inv.control('status')
    account.body = fake.windows(40, 30)
    time.sleep(2.5)
    passive2 = inv.control('status')
    enter(control_mod.KEY)
    enter((control.challenge or '').encode() + b'\r')
    resumed = inv.control('status')
    reopened = time.monotonic()
    time.sleep(0.5)
    enter(b'\x15KLC continued prompt\r')
    time.sleep(0.4)
    enter(b'\x18\x13')
    fixtures.wait_for(lambda: [r for r in case.events('upstream_request') if r.get('continued')], 15)
    time.sleep(1)
    alive = inv.native_alive()
    same = inv.gate.pid == gate_pid and inv.native.pid == native_pid and inv.gate.poll() is None
    exit_info = inv.exit_native()
    done.set()
    for fd in (outer_master, outer_slave):
        os.close(fd)
    return {'launched': True, 'pc5_control_sha256': digest, 'stale_typed_ignored': stale_ignored,
            'paste_ignored': paste_ignored, 'native_output_ignored': output_ignored,
            'native_output_on_display': output_on_display, 'passive_status': passive, 'background_tty_write': [r.get('result') for r in tty_rows][:3],
            'background_requests_while_paused': sorted({r.get('status') for r in sdk_paused if r['t'] < reopened}),
            'checks': checks, 'failing_checks': failing, 'status_after_failing_check': still,
            'passive_status_after_failing_check': passive2, 'status_after_continue': resumed,
            'reopened_t': reopened, 'same_invocation': same, 'native_alive': alive,
            'continued_upstream': len([r for r in case.events('upstream_request') if r.get('continued')]),
            'outer_display': {'paused_notice': b'Paused.' in outer, 'still_paused': b'Still paused' in outer,
                              'passed': b'Fresh quota check passed' in outer,
                              'native_output_relayed': b'Claude Code' in outer,
                              'tty_key_bytes_on_display': control_mod.KEY in outer},
            'exit': exit_info, 'summary': summary(case, inv, upstream)}


def case_independent(case):
    """Case 6: two protected invocations of one account with different reserves."""
    config = case.account()
    case.usage_account(A, IDENT_A, five=40)
    plans = {}
    upstream = case.upstream(lambda info: script(plans.get(info.get('tag'), {}))(info))
    invs = []
    for name, reserve in (('inv1', 30), ('inv2', 10)):
        policy = dict(POLICY, reserves={'five_hour': reserve, 'weekly': 10})
        inv = case.invocation(name, config, 'KLC root ' + name, policy=policy)
        spec = case.spec(inv, config, sdk_duration=50)
        plans[name] = {'root': [bash(case.child_cmd(spec, 'sdk', 'sdk-' + name), background=True), HOLD]}
        invs.append(inv)
    case.trust(config, invs)
    for inv in invs:
        if not launch(case, inv):
            return {'launched': False}
    fixtures.wait_for(lambda: len(upstream.held(['root'])) >= 2, 60)
    fixtures.wait_for(lambda: len(sdk_status(case, 'sdk-inv1')) >= 2 and len(sdk_status(case, 'sdk-inv2')) >= 2, 25)
    case.set_usage(75)  # consumption elsewhere: remaining 25 (inv1 reserve 30, inv2 reserve 10)
    first = time.monotonic()
    fixtures.wait_for(lambda: case.latch(invs[0]), 6)
    time.sleep(4)
    inv2_ok_after = [d for d in invs[1].gate_events('forwarded') if d['t'] > first]
    inv1_after = [d for d in invs[0].gate_events('forwarded') if d['t'] > first + 1.5]
    inv2_latched_early = case.latch(invs[1])
    case.set_usage(93)  # remaining 7
    second = time.monotonic()
    fixtures.wait_for(lambda: case.latch(invs[1]), 6)
    time.sleep(1)
    alive = [inv.native_alive() for inv in invs]
    exits = [inv.exit_native() for inv in invs]
    return {'launched': True, 'first_change_t': first, 'second_change_t': second,
            'inv1_latch': case.latch(invs[0]), 'inv2_latch': case.latch(invs[1]),
            'inv2_latched_after_first_change': bool(inv2_latched_early),
            'inv2_forwarded_after_first_change': len(inv2_ok_after),
            'inv1_forwarded_after_first_change': len(inv1_after), 'natives_alive': alive, 'exits': exits,
            'summary': {inv.name: summary(case, inv, upstream) for inv in invs}}


def case_rotation(case):
    """Cases 7 and 8a: native refresh A->A2 through the exact-host tunnel, then A2->B queued."""
    cert, context = fixtures.make_ca(case.root)
    refresh = fixtures.RefreshServer(context, R_A, {
        'access_token': A2, 'refresh_token': R_A2, 'expires_in': 3600,
        'scope': 'user:inference user:profile user:sessions:claude_code',
        'account': {'uuid': IDENT_A[0], 'email_address': 'a@example.invalid'}, 'organization': {'uuid': IDENT_A[1]}},
        case.record)
    case.servers.append(refresh)
    now = int(time.time() * 1000)
    config = case.account(expires=now + 150000)
    for token in (A, A2):
        case.usage_account(token, IDENT_A)
    case.usage_account(B, IDENT_B, five=5)
    plan = {'root': [None, HOLD]}
    upstream = case.upstream(script(plan))
    inv = case.invocation('main', config, 'KLC root rotation',
                          connect_allow={'platform.claude.com:443': refresh.port},
                          extra_env={'NODE_EXTRA_CA_CERTS': str(cert)})
    spec = case.spec(inv, config, sdk_duration=60)
    plan['root'][0] = bash(case.child_cmd(spec, 'sdk', 'sdk'), background=True, description='KLC sdk child')
    case.trust(config, [inv])
    inode = (Path(config) / '.credentials.json').stat().st_ino
    if not launch(case, inv):
        return {'launched': False}
    refreshed = fixtures.wait_for(lambda: case.store_label(config) == 'A2', 30)
    fixtures.wait_for(lambda: upstream.held(['root']), 45)
    fixtures.wait_for(lambda: [r for r in sdk_status(case, 'sdk') if r.get('bearer') == 'A2'], 25)
    connects_open = case.boundary_actor(inv, spec, 'connects', 'connects-open')
    swap = time.monotonic()
    case.store(config, B, R_B, FAR)
    case.record('store_swapped', to='B', t=swap)
    fixtures.wait_for(lambda: case.latch(inv), 10)
    time.sleep(1.5)
    connects_paused = case.boundary_actor(inv, spec, 'connects', 'connects-paused')
    typed = later_prompt(case, inv)
    fixtures.wait_for(lambda: native_requests_after(inv, typed), 10)
    alive = inv.native_alive()
    exit_info = inv.exit_native()
    holds = []
    decisions = inv.gate_events('decision')
    for hold in case.gate(inv, 'controller', kind='rotation_hold'):
        after = [d for d in decisions if d['t'] >= hold['t']]
        if after:
            holds.append({'t': hold['t'], 'ok': after[0]['ok'], 'reason': after[0]['reason'],
                          'admit_ms': after[0]['admit_ms']})
    return {'launched': True, 'store_refreshed_to_A2': refreshed,
            'store_inode_replaced': (Path(config) / '.credentials.json').stat().st_ino != inode,
            'swap_t': swap, 'rotation_holds': holds,
            'connects_while_open': [(r['target'], r['status']) for r in connects_open if r['attempt'] == 'connect'],
            'connects_while_paused': [(r['target'], r['status']) for r in connects_paused if r['attempt'] == 'connect'],
            'later_refused': bool(native_requests_after(inv, typed, ok=False)), 'native_alive': alive,
            'exit': exit_info, 'refresh_requests': refresh.requests, 'summary': summary(case, inv, upstream)}


def case_norefresh(case):
    """Case 8b: no refresh path; the token expires during the run."""
    now = int(time.time() * 1000)
    expires = now + 45000
    config = case.account(expires=expires)
    case.usage_account(A, IDENT_A)
    plan = {'root': [None, HOLD]}
    upstream = case.upstream(script(plan))
    inv = case.invocation('main', config, 'KLC root norefresh', gate_proxy_env=False)
    spec = case.spec(inv, config, sdk_duration=80)
    plan['root'][0] = bash(case.child_cmd(spec, 'sdk', 'sdk'), background=True, description='KLC sdk child')
    case.trust(config, [inv])
    if not launch(case, inv):
        return {'launched': False}
    held = fixtures.wait_for(lambda: upstream.held(['root']), 45)
    wait = expires / 1000 - time.time()
    if wait > 0:
        time.sleep(wait)
    case.usage.revoke(A)
    revoked = time.monotonic()
    case.record('server_expiry', t=revoked)
    fixtures.wait_for(lambda: case.latch(inv), POLICY['max_age_s'] + 4)
    time.sleep(2)
    typed = later_prompt(case, inv)
    time.sleep(6)
    alive = inv.native_alive()
    exit_info = inv.exit_native()
    return {'launched': True, 'held_before_expiry': bool(held), 'expires_in_ms_at_start': 45000,
            'revoked_t': revoked, 'later_typed_t': typed,
            'native_requests_after_typed': [{'ok': d['ok'], 'reason': d['reason']} for d in
                                            native_requests_after(inv, typed)],
            'store_label_end': case.store_label(config), 'native_alive': alive, 'exit': exit_info,
            'summary': summary(case, inv, upstream)}


def tunnel_server(case, allow):
    """Pre-launch exact-host CONNECT tunnel (no gate, no lease): option (a)."""
    listener = socket.create_server(('127.0.0.1', 0))
    stop = threading.Event()

    def pump(a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def handle(client):
        head = b''
        while b'\r\n\r\n' not in head and len(head) < 4096:
            chunk = client.recv(1)
            if not chunk:
                return
            head += chunk
        parts = head.split(b'\r\n', 1)[0].decode('latin-1').split()
        target = parts[1] if len(parts) == 3 and parts[0] == 'CONNECT' else None
        case.record('prelaunch_connect', target=target, ok=target in allow)
        if target not in allow:
            client.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
            client.close()
            return
        upstream = socket.create_connection(('127.0.0.1', allow[target]), timeout=5)
        client.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
        threading.Thread(target=pump, args=(upstream, client), daemon=True).start()
        pump(client, upstream)

    def serve():
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                client, _ = listener.accept()
            except OSError:
                continue
            threading.Thread(target=handle, args=(client,), daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return listener, stop


def case_prelaunch(case):
    """Option (a): can a native refresh run before launch, model-free, through an exact-host tunnel?"""
    cert, context = fixtures.make_ca(case.root)
    refresh = fixtures.RefreshServer(context, R_A, {
        'access_token': A2, 'refresh_token': R_A2, 'expires_in': 3600,
        'scope': 'user:inference user:profile user:sessions:claude_code',
        'account': {'uuid': IDENT_A[0], 'email_address': 'a@example.invalid'}, 'organization': {'uuid': IDENT_A[1]}},
        case.record)
    case.servers.append(refresh)
    case.upstream(script({}))
    listener, stop = tunnel_server(case, {'platform.claude.com:443': refresh.port})
    port = listener.getsockname()[1]
    runs = []
    for variant, offset in (('near_expiry', 150000), ('expired', -60000)):
        config = case.account(expires=int(time.time() * 1000) + offset, name='prelaunch-' + variant)
        base = case.root / ('prelaunch-' + variant)
        for part in ('home', 'tmp', 'xdg', 'cache', 'state', 'data'):
            (base / part).mkdir(parents=True, mode=0o700)
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(base / 'home'), 'TMPDIR': str(base / 'tmp'),
               'XDG_CONFIG_HOME': str(base / 'xdg'), 'XDG_CACHE_HOME': str(base / 'cache'),
               'XDG_STATE_HOME': str(base / 'state'), 'XDG_DATA_HOME': str(base / 'data'), 'LANG': 'C.UTF-8',
               'CLAUDE_CONFIG_DIR': config, 'DISABLE_AUTOUPDATER': '1', 'HTTPS_PROXY': 'http://127.0.0.1:%d' % port,
               'NO_PROXY': '127.0.0.1,localhost', 'NODE_EXTRA_CA_CERTS': str(cert)}
        before = refresh.requests
        argv = [PY, '-I', '-B', str(PROBES / 'nl' / 'boundary.py'), '--gate-port', str(port), '--seccomp', 'inet',
                '--', case.binary, 'auth', 'status']
        started = time.monotonic()
        try:
            done = subprocess.run(argv, env=env, cwd=base, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=30, start_new_session=True)
            code, stdout = done.returncode, done.stdout
        except subprocess.TimeoutExpired:
            code, stdout = 'timeout', ''
        try:
            status = json.loads(stdout)
            keys = {k: status.get(k) for k in ('loggedIn', 'authMethod', 'apiProvider') if k in status}
        except ValueError:
            keys = {'unparsed': True}
        runs.append({'variant': variant, 'exit': code, 'elapsed_s': round(time.monotonic() - started, 2),
                     'status_fields': keys, 'refresh_requests': refresh.requests - before,
                     'store_label_after': case.store_label(config)})
    stop.set()
    listener.close()
    return {'launched': True, 'runs': runs,
            'tunnel_targets': sorted({(r['target'], r['ok']) for r in case.events('prelaunch_connect')})}


def case_tools(case):
    """Cases 9 and 10: local work through a pause; git and package workflows."""
    config = case.account()
    account = case.usage_account(A, IDENT_A)
    plan = {'root': [None, None, HOLD]}
    upstream = case.upstream(script(plan))
    git_env = {'PATH': '/usr/bin:/bin', 'HOME': str(case.root), 'GIT_CONFIG_NOSYSTEM': '1'}
    fixtures.make_git_repo(case.root / 'git', git_env)
    fixtures.make_index(case.root / 'index')
    git_server = fixtures.FileServer(case.root / 'git', case.record, 'git')
    index_server = fixtures.FileServer(case.root / 'index', case.record, 'index')
    case.servers += [git_server, index_server]
    inv = case.invocation('main', config, 'KLC root tools')
    fg_ticks, fg_pid = case.root / 'fg.ticks', case.root / 'fg.pid'
    spec = case.spec(inv, config, tick_file=str(case.root / 'bg.ticks'), tick_duration=40, tick_interval=0.5,
                     git_port=git_server.port, index_port=index_server.port)
    plan['root'][0] = bash('%s & %s & wait' % (case.child_cmd(spec, 'ticks', 'ticks'),
                                               case.child_cmd(spec, 'workflows', 'workflows-run', '--phase', 'run')),
                           background=True, description='KLC test-like process')
    plan['root'][1] = bash('echo $$ > %s; for i in $(seq 1 24); do echo $i >> %s; sleep 0.5; done; echo KLC fg done'
                           % (fg_pid, fg_ticks), description='KLC long local command')
    case.trust(config, [inv])
    control_rows = None
    env = dict(git_env, HOME=str(case.root / 'control-home'))
    (case.root / 'control-home').mkdir(mode=0o700)
    subprocess.run([PY, '-I', '-B', str(CHILD), 'workflows', '--spec', str(spec), '--name', 'workflows-control',
                    '--phase', 'control'], env=env, cwd=case.root, stdin=subprocess.DEVNULL, capture_output=True,
                   timeout=60)
    control_rows = case.child_rows('workflows-control')
    if not launch(case, inv):
        return {'launched': False}
    count = lambda path: len(path.read_text().splitlines()) if path.exists() else 0
    fixtures.wait_for(lambda: count(fg_ticks) >= 5, 60)
    account.body = fake.windows(85, 30)
    case.record('trigger', what='usage_crosses_reserve')
    fixtures.wait_for(lambda: case.latch(inv), 6)
    latch_t = time.monotonic()
    fg_at_latch, bg_at_latch = count(fg_ticks), count(case.root / 'bg.ticks')
    time.sleep(2)
    fg_after, bg_after = count(fg_ticks), count(case.root / 'bg.ticks')
    pause_rows = case.boundary_actor(inv, spec, 'workflows', 'workflows-pause', '--phase', 'pause', timeout=60)
    pid = int(fg_pid.read_text().strip()) if fg_pid.exists() else None
    shell_alive_before = pid is not None and Path('/proc/%d' % pid).exists()
    fg_before_int = count(fg_ticks)
    interrupt = time.monotonic()
    inv.write(b'\x03')
    case.record('interrupt', key='ctrl-c', t=interrupt)
    time.sleep(3)
    fg_after_int = count(fg_ticks)
    shell_alive_after = pid is not None and Path('/proc/%d' % pid).exists()
    bg_pids = sorted({r['pid'] for r in case.child_rows('ticks')})
    bg_alive_after_int = [Path('/proc/%d' % p).exists() for p in bg_pids]
    bg_after_int = count(case.root / 'bg.ticks')
    time.sleep(1.5)
    bg_growing = count(case.root / 'bg.ticks') > bg_after_int
    native_alive = inv.native_alive()
    exit_info = inv.exit_native()
    time.sleep(1)
    bg_alive_after_exit = [Path('/proc/%d' % p).exists() for p in bg_pids]
    return {'launched': True, 'latch_t': latch_t, 'fg_ticks_at_latch': fg_at_latch, 'fg_ticks_2s_after': fg_after,
            'bg_ticks_at_latch': bg_at_latch, 'bg_ticks_2s_after': bg_after,
            'fg_shell_alive_before_interrupt': shell_alive_before, 'fg_ticks_before_interrupt': fg_before_int,
            'fg_ticks_3s_after_interrupt': fg_after_int, 'fg_shell_alive_after_interrupt': shell_alive_after,
            'fg_finished_marker': count(fg_ticks) >= 24, 'bg_alive_after_interrupt': bg_alive_after_int,
            'bg_ticks_growing_after_interrupt': bg_growing, 'bg_alive_after_native_exit': bg_alive_after_exit,
            'workflows_control': control_rows, 'workflows_pause': pause_rows,
            'native_alive_after_interrupt': native_alive, 'exit': exit_info, 'summary': summary(case, inv, upstream)}


def case_refusals(case):
    """Case 11: startup refusals with zero model requests and no native start."""
    upstream = case.upstream(script({}))
    out = {'launched': True, 'host_facts': compose.host_facts()}
    out['host_refusal'] = compose.host_refusal(out['host_facts'])
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(case.root / 'version-home'), 'LANG': 'C.UTF-8',
           'DISABLE_AUTOUPDATER': '1', 'CLAUDE_CONFIG_DIR': str(case.root / 'version-config')}
    (case.root / 'version-home').mkdir(mode=0o700)
    real = subprocess.run([case.binary, '--version'], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=20)
    fake_native = case.root / 'fake-claude'
    fake_native.write_text('#!/bin/sh\necho "2.0.0 (Claude Code)"\n')
    fake_native.chmod(0o700)
    other = subprocess.run([str(fake_native), '--version'], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=5)
    out['version'] = {'real': compose.version_refusal(real.stdout), 'real_text': real.stdout.strip(),
                      'fake_older': compose.version_refusal(other.stdout)}
    out['mode'] = {'print': compose.mode_refusal(['-p', 'KLC']), 'bare': compose.mode_refusal(['--bare']),
                   'interactive': compose.mode_refusal(['KLC root'])}
    out['env'] = {'oauth_token_env': compose.env_refusal({'CLAUDE_CODE_OAUTH_TOKEN': ''}, 'x'),
                  'base_url_env': compose.env_refusal({'ANTHROPIC_BASE_URL': ''}, 'x')}
    now = int(time.time() * 1000)
    out['token_window'] = {'near_no_tunnel': compose.token_window_refusal(now + 100000, now, False),
                           'near_with_tunnel': compose.token_window_refusal(now + 100000, now, True)}
    variants = {}
    specs = [('threshold', {'five': 85}), ('window_missing', {'five': None}), ('malformed', {'mode': 'malformed'}),
             ('unavailable', {'mode': 'status:503'}), ('rate_limited', {'mode': 'status:429'}),
             ('unproved_row', {'extra': {'limits': [{'kind': 'weekly', 'percent': 5}]}}),
             ('no_usage_account', {'revoke': True}), ('stale_server_clock', {'skew': -3600}),
             ('expired_token', {'expires': now - 60000})]
    for name, change in specs:
        usage = case.usage
        if 'skew' in change:
            usage = TimedUsage(server_time=lambda s=change['skew']: time.time() + s)
            usage.__enter__()
            case.servers.append(usage)
        config = case.account(expires=change.get('expires', FAR), name='refuse-' + name)
        account = fake.FakeAccount('A', *IDENT_A, fake.windows(change.get('five', 40), 30, extra=change.get('extra')))
        account.mode = change.get('mode', 'normal')
        if not change.get('revoke'):
            usage.register(A, account)
        inv = compose.Invocation(case.root, 'refuse-' + name, case.record, case.binary, config_dir=config,
                                 registered=IDENT_A, usage_port=usage.port, upstream_port=upstream.port,
                                 policy=POLICY, prompt='KLC root refused')
        case.invocations.append(inv)
        result = inv.start_gate()
        code = inv.gate.wait(5) if inv.gate else None
        variants[name] = {'event': result.get('event'), 'reason': result.get('reason'), 'gate_exit': code,
                          'native_started': inv.native is not None}
        if not change.get('revoke'):
            usage.revoke(A)
    out['quota_startup'] = variants
    left, right = socket.socketpair()
    probe = subprocess.run([PY, '-I', '-B', str(PROBES / 'nl' / 'boundary.py'), '--gate-port', '9', '--seccomp',
                            'inet', '--', '/bin/true'], stdin=left, capture_output=True, text=True, timeout=10)
    left.close()
    right.close()
    out['boundary_socket_stdin'] = {'exit': probe.returncode, 'stderr': probe.stderr.strip()[-120:],
                                    'launcher_refusal': compose.host_refusal(dict(out['host_facts'], stdio_sockets=[0]))}
    out['upstream_requests'] = upstream.count
    return out


def case_sandbox(case):
    """Native sandbox inside the outer boundary (sandbox.enabled)."""
    config = case.account()
    case.usage_account(A, IDENT_A)
    plan = {'root': [bash('echo KLC sandboxed > sb.txt; echo rc=$?; cat sb.txt', description='KLC sandbox probe'),
                     ('text', 'KLC sandbox done')]}
    upstream = case.upstream(script(plan))
    inv = case.invocation('main', config, 'KLC root sandbox',
                          settings_extra={'sandbox': {'enabled': True, 'autoAllowBashIfSandboxed': True}})
    case.trust(config, [inv])
    if not launch(case, inv):
        return {'launched': False}
    fixtures.wait_for(lambda: [r for r in case.events('upstream_request') if r['kind'] == 'root'
                               and r['tool_results'] >= 1], 45)
    time.sleep(2)
    results = [r.get('result_text') for r in case.events('upstream_request') if r['kind'] == 'root'
               and r['tool_results'] >= 1]
    exists = (inv.workspace / 'sb.txt').exists()
    exit_info = inv.exit_native()
    return {'launched': True, 'tool_result': results[:1], 'file_written': exists, 'exit': exit_info,
            'summary': summary(case, inv, upstream)}


def case_fsrules(case):
    """Outer filesystem write rule (workspace, temp, config writable) with native sandbox off."""
    config = case.account()
    case.usage_account(A, IDENT_A)
    outside = case.root / 'outside'
    outside.mkdir(mode=0o700)
    plan = {'root': [None, ('text', 'KLC fs done')]}
    upstream = case.upstream(script(plan))
    inv = case.invocation('main', config, 'KLC root fsrules')
    _, base = inv.native_env()
    rules = []
    for part in ('home', 'cache', 'state', 'data', 'tmp', 'xdg', 'runtime', 'workspace'):
        rules += ['--write', str(base / part)]
    rules += ['--write', config, '--write', '/dev/pts', '--write-file', '/dev/null', '--write-file', '/dev/tty']
    inv.fs_rules = rules
    plan['root'][0] = bash('echo ok > inside.txt; echo inside=$?; echo bad > %s/outside.txt; echo outside=$?; '
                           'echo t > "$TMPDIR/t.txt"; echo tmp=$?' % outside, description='KLC fs probe')
    case.trust(config, [inv])
    if not launch(case, inv):
        return {'launched': False}
    fixtures.wait_for(lambda: [r for r in case.events('upstream_request') if r['kind'] == 'root'
                               and r['tool_results'] >= 1], 45)
    time.sleep(2)
    results = [r.get('result_text') for r in case.events('upstream_request') if r['kind'] == 'root'
               and r['tool_results'] >= 1]
    exit_info = inv.exit_native()
    return {'launched': True, 'tool_result': results[:1], 'inside_written': (inv.workspace / 'inside.txt').exists(),
            'outside_written': (outside / 'outside.txt').exists(), 'exit': exit_info,
            'fsrules_applied': 'fsrules-applied' in inv.visible(), 'summary': summary(case, inv, upstream)}


CASES = {'threshold': case_threshold, 'loss-kill': case_loss_kill, 'loss-eof': case_loss_eof,
         'loss-stall': case_loss_stall, 'gate-death': case_gate_death, 'continuation': case_continuation,
         'independent': case_independent, 'rotation': case_rotation, 'norefresh': case_norefresh,
         'prelaunch': case_prelaunch, 'tools': case_tools, 'refusals': case_refusals, 'sandbox': case_sandbox,
         'fsrules': case_fsrules}


def main(argv):
    name, binary = argv[0], str(Path(argv[1]).resolve()) if len(argv) > 1 else None
    case = Case(name, binary)
    outcome = {}
    try:
        outcome = CASES[name](case)
    except Exception as error:  # recorded, never reported as a pass
        outcome = {'launched': False, 'error': '%s: %s' % (type(error).__name__, str(error)[:300])}
        raise
    finally:
        case.finish(outcome)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
