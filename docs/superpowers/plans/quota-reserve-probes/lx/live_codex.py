"""LX live probe: one model-free Codex usage read for one codex-as account (research only).

Run as `python3 -I -B live_codex.py --account NAME --out DIR` (standard library only).
The live run against the approved account is executed by the coordinator under
live-permissions-approval.json, never by a test or by the node that wrote it.
Tests drive run_probe() with a temporary HOME, fabricated credentials and
loopback fixtures; only main() builds the production upstream, the system TLS
context and the refresh tunnel.

Sequence:
1. Refusals before anything is spawned or written (exit 2): the approved account
   name, platform, TLS-relevant environment, the private out directory (absolute,
   0700, empty, outside every Git work tree and the account state), the codex-as
   preflight and layout through lib/codex-as (validated read-only, never repaired),
   the account lease (exclusive and non-blocking, so a running codex-as client of
   the account refuses), the PC network guard self-check and the Codex version.
2. One-shot attempt file (0600, O_EXCL) in the out directory, written before spawn.
3. One owned `codex app-server` (stdio) under the network guard, with the account's
   CODEX_HOME and shared SQLite home exactly as codex-as passes them, the account
   lease fd inherited (shared, as codex-as's run_native does), and configuration
   overrides on the command line only: chatgpt_base_url and a forced custom model
   provider both point at the LX gate, which refuses every model request.
4. JSON-RPC: initialize, then account/rateLimits/read once; a second read only
   after a fixed delay and only if the first succeeded. No thread, turn, login or
   model request; no retry.
5. Reap the process group, check (read-only) whether native refreshed the credential
   and whether the account link is intact, scan the sanitized result for anything
   token- or id-shaped and write it (0600, O_EXCL). codex-as's post-run repair is
   never run here: the next codex-as launch of the account performs it if needed.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

HERE = Path(os.path.realpath(__file__)).parent
REPO = HERE.parents[4]


def load(name, path):
    spec = importlib.util.spec_from_file_location('lx_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load('check', REPO / 'lib/codex-as/check.py')
storage = load('layout', REPO / 'lib/codex-as/layout.py')
settings = load('config', REPO / 'lib/coding-agent-kit/config.py')
binding = load('binding', HERE.parent / 'qx/binding.py')
gates = load('gate', HERE / 'gate.py')

APPROVED_ACCOUNT = 'stanford'  # live-permissions-approval.json, codex section
EXPECTED_VERSION = '0.167.2'
DEFAULT_DELAY = 30.0
MAX_DELAY = 120.0
INIT_TIMEOUT = 20.0
READ_TIMEOUT = 40.0
TOTAL_DEADLINE = 200.0
CHECK_TIMEOUT = 20.0
LINE_LIMIT = 1 << 20
OUTPUT_LIMIT = 16 << 20
NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_-]*')
VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+')
METHOD = re.compile(r'[A-Za-z0-9/_.-]{1,80}')
ENUM = re.compile(r'[A-Za-z0-9_]{1,48}')
ENV_ALLOW = ('HOME', 'USER', 'LOGNAME', 'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ')
PROXY_KEYS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')
# They change the trust store or TLS behavior of this process's OpenSSL, whatever python3 -I does.
REFUSED_ENV = ('SSL_CERT_FILE', 'SSL_CERT_DIR', 'SSLKEYLOGFILE', 'OPENSSL_CONF', 'OPENSSL_MODULES',
               'OPENSSL_ENGINES')
GUARD_COMMAND = (sys.executable, '-I', '-B', str(HERE.parent / 'pc/network_guard.py'))
GUARD_CHECK = HERE.parent / 'pc/guard_check.py'
ATTEMPT, RESULT, WORKSPACE = 'lx-attempt.json', 'lx-result.json', 'workspace'
CLIENT_INFO = {'name': 'coding-agent-kit-lx-probe', 'version': '1'}
READ_PARAMS = {'excludeResetCreditDetails': True}


class Refused(Exception):
    """A fixed reason code (and, for stderr only, codex-as's own message)."""

    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason, self.detail = reason, detail


def utc():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def overrides(gate_url, sqlite):
    """codex-as's fixed keys plus the probe's routing and quiet-startup keys, as -c pairs."""
    return [
        'cli_auth_credentials_store="file"',
        'sqlite_home=' + json.dumps(str(sqlite), ensure_ascii=False),
        'check_for_update_on_startup=false',
        'chatgpt_base_url="%s/backend-api/"' % gate_url,
        'model_provider="lxgate"',
        'model_providers.lxgate.name="lxgate"',
        'model_providers.lxgate.base_url="%s/backend-api/codex"' % gate_url,
        'model_providers.lxgate.requires_openai_auth=true',
        'model_providers.lxgate.supports_websockets=false',
        'model_providers.lxgate.wire_api="responses"',
        'model_providers.lxgate.request_max_retries=0',
        'model_providers.lxgate.stream_max_retries=0',
        'analytics.enabled=false',
        'otel.exporter="none"',
        'otel.trace_exporter="none"',
        'otel.metrics_exporter="none"',
        'web_search="disabled"',
        'features.plugins=false',
        'features.recommended_plugins=false',
        'features.apps=false',
        'features.enable_mcp_apps=false',
        'features.memories=false',
        'features.network_proxy=false',
        'features.code_mode_prewarm=false',
        'memories.generate_memories=false',
        'memories.use_memories=false',
    ]


def trust_store_loaded():
    """The production context's system store holds CA certificates (else every forward fails)."""
    try:
        return gates.production_context().cert_store_stats().get('x509_ca', 0) > 0
    except (ssl.SSLError, OSError, RuntimeError):
        return False


def check_environment(environ):
    present = [name for name in REFUSED_ENV if name in environ]
    if present:
        raise Refused('tls-environment', ', '.join(present) + ' set (values hidden)')


def _git_ancestor(path):
    return any(os.path.lexists(parent / '.git') for parent in (path, *path.parents))


def _inside(path, roots):
    for root in roots:
        real = Path(os.path.realpath(root))
        if path == real or real in path.parents or path in real.parents:
            return True
    return False


def prepare_out(out, config, uid=None):
    """A private, empty out directory outside every Git work tree and the account state."""
    uid = os.getuid() if uid is None else uid
    if not out.is_absolute():
        raise Refused('out-not-absolute')
    parent = Path(os.path.realpath(out.parent))
    target = parent / out.name
    if _git_ancestor(target):
        raise Refused('out-inside-git-work-tree')
    if _inside(target, (config.codex_home, config.secrets_dir, REPO)):
        raise Refused('out-inside-account-state-or-repository')
    if os.path.lexists(target):
        info = os.lstat(target)
        if not stat.S_ISDIR(info.st_mode):
            raise Refused('out-not-a-directory')
        if info.st_uid != uid:
            raise Refused('out-not-owned')
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise Refused('out-not-private')
        if os.listdir(target):
            raise Refused('out-not-empty')  # one attempt per out directory
    else:
        info = os.lstat(parent)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (uid, 0) or (
                info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise Refused('out-parent-unsafe')
        os.mkdir(target, 0o700)
        if stat.S_IMODE(os.lstat(target).st_mode) != 0o700:
            raise Refused('out-not-private')
    return target


class Account:
    """The codex-as account, validated read-only, with its lease fd held."""

    def __init__(self, layout, fd, owner, sqlite):
        self.layout, self.fd, self.owner, self.sqlite = layout, fd, owner, sqlite

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def link_intact(self):
        auth = self.layout.auth
        return auth.is_symlink() and Path(os.readlink(auth)) == self.layout.target

    def credential_stat(self):
        info = os.stat(self.layout.target, follow_symlinks=False)
        return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)

    def credential_private(self):
        info = os.stat(self.layout.target, follow_symlinks=False)
        return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600)


def open_account(alias, config, cwd):
    """codex-as's preflight, layout validation and lease, without any repair or write."""
    if not NAME.fullmatch(alias):
        raise Refused('invalid-account-name')
    try:
        guard.preflight(alias, SimpleNamespace(cwd=cwd, profile=None), config)
    except ValueError as error:
        raise Refused('codex-as-preflight', str(error)) from None
    try:
        if alias not in guard.accounts(config.secrets_dir):
            raise Refused('account-unknown')
        layout = storage.Layout(guard, alias, config)
        layout.root_check()
    except ValueError as error:
        raise Refused('layout-unexpected', str(error)) from None
    path = config.codex_home / 'accounts/.locks' / (alias + '.lock')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        raise Refused('lease-file-missing') from None
    except OSError:
        raise Refused('lease-file-unsafe') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise Refused('lease-file-unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused('lease-held') from None
        try:
            if layout.setup_pending():
                raise Refused('setup-pending')
            owner = storage.owner(layout.validate())
            if owner is None:
                raise Refused('not-chatgpt-login')
            # As codex-as does before native starts: shared, then revalidate (EX->SH is not atomic).
            fcntl.flock(fd, fcntl.LOCK_SH)
            if storage.owner(layout.validate()) != owner:
                raise Refused('identity-changed-during-preflight')
            sqlite = storage.shared_sqlite(guard, layout.root, cwd, None)
        except ValueError as error:
            raise Refused('layout-unexpected', str(error)) from None
        except (KeyError, TypeError, UnicodeError, AttributeError):
            raise Refused('layout-unexpected') from None
        return Account(layout, fd, owner, sqlite)
    except BaseException:
        os.close(fd)
        raise


def guard_self_check(command):
    try:
        result = subprocess.run([*command, sys.executable, '-I', '-B', str(GUARD_CHECK)], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=CHECK_TIMEOUT, start_new_session=True)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def native_version(command, binary, environ):
    """`codex --version` as codex-as runs it (caller HOME, root CODEX_HOME), under the guard."""
    env = {key: environ[key] for key in ENV_ALLOW if key in environ}
    try:
        result = subprocess.run([*command, binary, '--version'], env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=CHECK_TIMEOUT, start_new_session=True)
    except (OSError, subprocess.TimeoutExpired):
        raise Refused('version-unavailable') from None
    try:
        version = guard.codex_version(result.stdout)
    except ValueError as error:
        raise Refused('version-unrecognized', str(error)) from None
    return '%d.%d.%d' % version


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_private(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def label(method):
    return method if isinstance(method, str) and METHOD.fullmatch(method) else '<unlisted>'


class Rpc:
    """Newline-delimited JSON-RPC over the app-server's stdio. Server requests are refused;
    notifications are counted by method name only (their contents may name the host)."""

    def __init__(self, process):
        self.process = process
        self.messages = queue.Queue()
        self.write_lock = threading.Lock()
        self.sent = []
        self.notifications = Counter()
        self.server_requests = Counter()
        self.unparsed = 0
        self.overflow = False
        self.next_id = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        total = 0
        try:
            while True:
                line = self.process.stdout.readline(LINE_LIMIT + 1)
                if not line:
                    break
                total += len(line)
                if len(line) > LINE_LIMIT or total > OUTPUT_LIMIT:
                    self.overflow = True
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    self.unparsed += 1
                    continue
                if not isinstance(message, dict):
                    self.unparsed += 1
                elif isinstance(message.get('method'), str) and 'id' in message:
                    self.server_requests[label(message['method'])] += 1
                    self._send({'id': message['id'], 'error': {'code': -32000,
                                                               'message': 'lx probe refuses server requests'}})
                elif isinstance(message.get('method'), str):
                    self.notifications[label(message['method'])] += 1
                elif 'id' in message:
                    self.messages.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self.messages.put(None)

    def _send(self, message):
        data = (json.dumps(message) + '\n').encode()
        with self.write_lock:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def notify(self, method, params):
        self.sent.append(method)
        self._send({'method': method, 'params': params})

    def request(self, method, params, timeout):
        self.next_id += 1
        ident = self.next_id
        self.sent.append(method)
        try:
            self._send({'id': ident, 'method': method, 'params': params})
        except (OSError, ValueError):
            return 'closed', None
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return 'timeout', None
            try:
                message = self.messages.get(timeout=left)
            except queue.Empty:
                return 'timeout', None
            if message is None:
                self.messages.put(None)
                return 'closed', None
            if message.get('id') == ident:
                if 'error' in message:
                    error = message['error']
                    return 'error', error if isinstance(error, dict) else {}
                return 'ok', message.get('result')


def _num(value):
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    return {'type': type(value).__name__}


def _enum(value):
    if value is None:
        return None
    return value if isinstance(value, str) and ENUM.fullmatch(value) else '<unrecognized>'


def native_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        return None if snapshot is None else {'type': type(snapshot).__name__}

    def window(raw):
        if not isinstance(raw, dict):
            return None if raw is None else {'type': type(raw).__name__}
        return {key: _num(raw.get(key)) for key in ('usedPercent', 'windowDurationMins', 'resetsAt')}
    return {'limit_id_is_codex': snapshot.get('limitId') == 'codex',
            'limit_name_present': isinstance(snapshot.get('limitName'), str),
            'normal_model_slug_present': isinstance(snapshot.get('normalModelSlug'), str),
            'plan_type': gates.plan(snapshot.get('planType')),
            'primary': window(snapshot.get('primary')), 'secondary': window(snapshot.get('secondary')),
            'rate_limit_reached_type': _enum(snapshot.get('rateLimitReachedType')),
            'credits_present': snapshot.get('credits') is not None,
            'individual_limit_present': snapshot.get('individualLimit') is not None,
            'spend_control_reached': _num(snapshot.get('spendControlReached'))}


def native_view(result, owner):
    """What native returned, reduced to approved fields and booleans (no ids, no names)."""
    if not isinstance(result, dict):
        return {'type': type(result).__name__}
    by = result.get('rateLimitsByLimitId')
    by = by if isinstance(by, dict) else {}
    account = result.get('accountId')
    allowed = result.get('ordinaryUsageAllowed')
    return {'rate_limits': native_snapshot(result.get('rateLimits')),
            'by_limit_id_count': len(by), 'by_limit_id_has_codex': 'codex' in by,
            'other_families': [native_snapshot(value) for key, value in sorted(by.items(), key=lambda item: str(item[0]))
                               if key != 'codex'][:8],
            'account_id_present': isinstance(account, str) and bool(account),
            'account_id_equals_codex_as_account': gates._same(account, owner[1]),
            'ordinary_usage_allowed': allowed if isinstance(allowed, bool) or allowed is None else '<unrecognized>',
            'reset_credits_present': result.get('rateLimitResetCredits') is not None,
            'upsell_present': result.get('rateLimitUpsell') is not None}


def rpc_error(error):
    """A fixed classification; native's messages can quote the response body."""
    message = error.get('message') if isinstance(error.get('message'), str) else ''
    code = error.get('code')
    # native 0.167.2: 'GET <url> failed: 502 Bad Gateway; content-type=...; body=...' (backend-client)
    status = re.search(r'(?:failed: |status )([1-5][0-9][0-9])\b', message)
    kind = ('fetch-failed' if message.startswith('failed to fetch codex rate limits')
            else 'auth-required' if 'authentication required' in message else 'other')
    return {'code': code if isinstance(code, int) and not isinstance(code, bool) else None, 'kind': kind,
            'http_status': int(status[1]) if status else None, 'decode_error': 'Decode error' in message}


def qx_verdict(owner):
    """The QX lease's verdict on the forwarded body (qx/binding.py), as a fixed code."""
    user, account = owner

    def verdict(status, body, header_account):
        if status != 200:
            return 'http-%d' % status
        try:
            observation = binding.normalize_usage(body, request_account=header_account, expected_account=account,
                                                  expected_user=user, wall_now=int(time.time()))
        except binding.Unavailable as error:
            return 'unavailable:' + error.reason
        except Exception:  # research view only; never fails the probe
            return 'error'
        code = 'observation:' + ('+'.join(sorted(name for name in observation.windows if name)) or 'none')
        return code + (':limit-reached' if observation.limit_reached else '') + (
            ':other-families' if observation.other_families else '')
    return verdict


def freshness(exchanges):
    """Two relayed 200 reads compared by approved fields only (deltas, never values of ids)."""
    good = [item for item in exchanges if item.get('outcome') == 'relayed' and item.get('status') == 200
            and isinstance(item.get('usage'), dict) and item['usage'].get('object')]
    if len(good) < 2:
        return {'comparable': False}
    first, second = good[0], good[1]
    view = {'comparable': True, 'elapsed_ms': second['received_ms'] - first['received_ms'],
            'usage_equal': first['usage'] == second['usage'], 'windows': {}}
    for slot in ('primary_window', 'secondary_window'):
        left = (first['usage'].get('rate_limit') or {}).get(slot)
        right = (second['usage'].get('rate_limit') or {}).get(slot)
        if isinstance(left, dict) and isinstance(right, dict):
            view['windows'][slot] = {
                key + '_delta': (right[key] - left[key]) if all(
                    isinstance(side.get(key), (int, float)) and not isinstance(side.get(key), bool)
                    for side in (left, right)) else None
                for key in ('used_percent', 'reset_at', 'reset_after_seconds', 'limit_window_seconds')}
    return view


JWT = re.compile(r'[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}')
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
RUN = re.compile(r'[A-Za-z0-9_+=-]{32,}')  # no '/': method names are slash-separated


def leaks(text, secrets=(), allowed=()):
    """Reasons the sanitized output must not be written: token-, id- or path-shaped content."""
    for value in allowed:
        text = text.replace(value, '')
    found = set()
    if JWT.search(text):
        found.add('jwt-shaped')
    if UUID.search(text):
        found.add('uuid-shaped')
    for run in RUN.findall(text):
        if (re.search(r'[0-9]', run) and re.search(r'[A-Z]', run)) or re.fullmatch(r'[0-9a-fA-F]{32,}', run):
            found.add('token-shaped')
    if any(isinstance(value, str) and len(value) >= 4 and value in text for value in secrets):
        found.add('known-secret')
    return sorted(found)


def guard_summary(stderr_text):
    matches = re.findall(r'^\{"guard_denied_syscalls".*\}$', stderr_text, re.M)
    if not matches:
        return None
    try:
        data = json.loads(matches[-1])
    except ValueError:
        return {'parse_error': True}
    denied = data.get('guard_denied_syscalls')
    denied = {key: value for key, value in denied.items() if re.fullmatch(r'[0-9]{1,4}', str(key))
              and isinstance(value, int)} if isinstance(denied, dict) else {}
    root = data.get('root_exit')
    return {'denied_syscalls': denied, 'root_exit': root if isinstance(root, int) else None}


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reap(process, grace=5.0):
    """Close stdin, wait, then SIGTERM and SIGKILL the whole process group; verify it is empty."""
    exited_early = process.poll() is not None
    try:
        process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=grace)
        graceful = True
    except subprocess.TimeoutExpired:
        graceful = False
    signals = []
    for signum in (signal.SIGTERM, signal.SIGKILL):
        if not group_alive(process.pid):
            break
        try:
            os.killpg(process.pid, signum)
            signals.append(signum.name)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and group_alive(process.pid):
            if process.poll() is None:
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            else:
                time.sleep(0.05)
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and group_alive(process.pid):
        time.sleep(0.05)
    return {'exited_before_stdin_close': exited_early, 'graceful_exit': graceful, 'exit_code': process.returncode,
            'signals': signals, 'process_group_empty': not group_alive(process.pid)}


def child_env(environ, account, workspace, gate_url):
    env = {key: environ[key] for key in ENV_ALLOW if key in environ}
    env.update({'CODEX_HOME': str(account.layout.home), 'CODEX_SQLITE_HOME': str(account.sqlite),
                'TMPDIR': str(workspace), 'TERM': 'dumb', 'NO_PROXY': '127.0.0.1,localhost',
                'no_proxy': '127.0.0.1,localhost'})
    env.update({key: gate_url for key in PROXY_KEYS})
    return env


def say(stream, text):
    print('lx: ' + text, file=stream, flush=True)


def run_probe(alias, out, *, config, upstream, tunnel, guard_command, delay=DEFAULT_DELAY, second=True,
              expect_version=EXPECTED_VERSION, environ=None, stdout=None, stderr=None, read_timeout=READ_TIMEOUT):
    """Returns 0 when the planned reads ran and the result was written, 2 when refused before
    spawn (nothing written), 3 when the attempt was used but did not complete."""
    environ = os.environ if environ is None else environ
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    account = None
    try:
        check_environment(environ)
        out = prepare_out(Path(out), config)
        binary = config.which('codex')
        if binary is None:
            raise Refused('codex-not-found')
        workspace = out / WORKSPACE
        account = open_account(alias, config, workspace)
        if guard_command and not guard_self_check(guard_command):
            raise Refused('guard-self-check-failed')
        version = native_version(guard_command or (), binary, environ)
        if version != expect_version:
            raise Refused('version-mismatch', 'found %s, reviewed %s' % (version, expect_version))
        binary_sha = sha256(binary)
    except Refused as error:
        if account is not None:
            account.close()
        say(stderr, 'refused before spawn: ' + error.reason + (' (' + error.detail + ')' if error.detail else ''))
        return 2
    except BaseException:
        if account is not None:
            account.close()
        raise
    return attempt(alias, out, account, binary, binary_sha, version, config=config, upstream=upstream, tunnel=tunnel,
                   guard_command=guard_command, delay=delay, second=second, environ=environ, stdout=stdout,
                   stderr=stderr, read_timeout=read_timeout)


class Attempt:
    """The one-shot part: everything after the attempt file exists. finish() always runs."""

    def __init__(self, alias, out, account, binary, binary_sha, version, *, config, upstream, tunnel,
                 guard_command, delay, second, environ, read_timeout):
        self.alias, self.out, self.account, self.binary = alias, out, account, binary
        self.binary_sha, self.config, self.upstream, self.tunnel = binary_sha, config, upstream, tunnel
        self.guard_command, self.delay, self.second, self.environ = guard_command, delay, second, environ
        self.read_timeout = read_timeout
        self.workspace = out / WORKSPACE
        self.gate = self.process = self.errors = self.rpc = self.before = None
        self.started = time.monotonic()
        self.deadline = self.started + TOTAL_DEADLINE
        self.result = {'schema': 1, 'probe': 'lx-live-codex', 'account': alias, 'started_utc': utc(),
                       'platform': {'system': platform.system().lower(), 'machine': platform.machine(),
                                    'python': platform.python_version(), 'openssl': ssl.OPENSSL_VERSION},
                       'native': {'version': version, 'binary_sha256': binary_sha, 'guarded': bool(guard_command)},
                       'lease': {'exclusive_at_start': True, 'shared_during_run': True, 'inherited_by_native': True},
                       'credential': {}, 'rpc': {'reads': [], 'delay_seconds': delay}, 'outcome': 'started'}

    def left(self, cap):
        return min(cap, self.deadline - time.monotonic())

    def execute(self):
        result, account = self.result, self.account
        write_private(self.out / ATTEMPT, json.dumps({'schema': 1, 'probe': 'lx-live-codex', 'account': self.alias,
                                                      'state': 'spawning', 'started_utc': result['started_utc']},
                                                     indent=1) + '\n')
        os.mkdir(self.workspace, 0o700)
        result['credential']['link_intact_before'] = account.link_intact()
        self.before = account.credential_stat()
        self.gate = gate = gates.Gate(self.upstream, tunnel=self.tunnel, expected=account.owner,
                                      verdict=qx_verdict(account.owner))
        command = [*(self.guard_command or ()), self.binary]
        for pair in overrides(gate.url, account.sqlite):
            command += ['-c', pair]
        command += ['app-server', '--listen', 'stdio://']
        self.errors = open(self.workspace / 'native.stderr', 'w+b')
        self.process = process = subprocess.Popen(
            command, env=child_env(self.environ, account, self.workspace, gate.url), cwd=self.workspace,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors, pass_fds=(account.fd,),
            start_new_session=True)
        self.rpc = rpc = Rpc(process)
        state, reply = rpc.request('initialize', {'clientInfo': CLIENT_INFO}, self.left(INIT_TIMEOUT))
        result['rpc']['initialize'] = state
        if state != 'ok':
            result['outcome'] = 'initialize-' + state
            return 3
        agent = reply.get('userAgent') if isinstance(reply, dict) else None
        found = re.match(r'[^/\s]+/([0-9]+\.[0-9]+\.[0-9]+)', agent) if isinstance(agent, str) else None
        result['native']['user_agent_version'] = found[1] if found else None
        rpc.notify('initialized', {})
        reads = result['rpc']['reads']
        for index in (1, 2):
            if index == 2:
                skip = self.second_read_skip()
                if skip:
                    result['rpc']['second_read'] = 'skipped:' + skip
                    break
            budget = self.left(self.read_timeout)
            if budget <= 0:
                result['rpc']['second_read' if index == 2 else 'first_read'] = 'skipped:deadline'
                break
            forwards = gate.forwards
            gate.arm()
            begun = time.monotonic()
            state, reply = rpc.request('account/rateLimits/read', READ_PARAMS, budget)
            gate.disarm()
            record = {'index': index, 'state': state, 'elapsed_ms': int((time.monotonic() - begun) * 1000),
                      'forwards_during_read': gate.forwards - forwards}
            if state == 'ok':
                record['native'] = native_view(reply, account.owner)
            elif state == 'error':
                record['error'] = rpc_error(reply)
            reads.append(record)
            if index == 2:
                result['rpc']['second_read'] = 'done'
        result['outcome'] = 'completed'
        return 0

    def second_read_skip(self):
        if not self.second:
            return 'single-read'
        first = self.gate.exchanges[0] if len(self.gate.exchanges) == 1 else None
        if not (self.result['rpc']['reads'][0]['state'] == 'ok' and first and first['outcome'] == 'relayed'
                and first['status'] == 200):
            return 'first-read-did-not-succeed'
        wake = time.monotonic() + self.delay
        while time.monotonic() < wake and self.process.poll() is None:
            time.sleep(min(0.2, max(0.0, wake - time.monotonic())))
        return 'native-exited' if self.process.poll() is not None else None

    def finish(self):
        """Reap, post-check read-only, sanitize and write the result. Returns sanitizer findings."""
        result, account = self.result, self.account
        if self.process is not None:
            result['native']['reap'] = reap(self.process)
            for stream in (self.process.stdout,):
                try:
                    stream.close()
                except OSError:
                    pass
        if self.rpc is not None:
            rpc = self.rpc
            result['rpc'].update({'methods_sent': list(rpc.sent),
                                  'notifications': dict(sorted(rpc.notifications.items())),
                                  'server_requests': dict(sorted(rpc.server_requests.items())),
                                  'unparsed_lines': rpc.unparsed, 'output_overflow': rpc.overflow})
        if self.errors is not None:
            self.errors.seek(0)
            text = self.errors.read(8 << 20).decode('utf-8', 'replace')
            self.errors.close()
            result['native']['stderr_lines'] = text.count('\n')  # the text itself is discarded
            result['native']['guard'] = guard_summary(text) if self.guard_command else None
        secrets = {value for value in account.owner if value}
        if self.gate is not None:
            self.gate.close()
            result['gate'] = self.gate.summary()
            result['freshness'] = freshness(result['gate']['exchanges'])
            secrets |= self.gate.secrets
        account.close()  # the lease ends only after the native process group is gone
        try:
            result['credential'].update({
                'changed_during_run': None if self.before is None else account.credential_stat() != self.before,
                'link_intact_after': account.link_intact(),
                'still_private_regular_file': account.credential_private()})
        except OSError:
            result['credential']['post_check_failed'] = True
        shutil.rmtree(self.workspace, ignore_errors=True)
        result['finished_utc'] = utc()
        result['elapsed_s'] = round(time.monotonic() - self.started, 1)
        secrets |= {value for value in (str(self.config.home), str(self.out)) if len(value) >= 8}
        text, findings = sanitize(result, secrets, allowed=(self.binary_sha,))
        secrets.clear()
        write_private(self.out / RESULT, text)
        return findings


def sanitize(result, secrets, allowed=()):
    """Serialize, redact anything token-, id- or path-shaped, and verify. Returns (text, findings)."""
    text = json.dumps(result, indent=1, sort_keys=True, ensure_ascii=True) + '\n'
    findings = leaks(text, secrets, allowed)
    if not findings:
        return text, []
    for value in sorted((value for value in secrets if isinstance(value, str) and len(value) >= 4),
                        key=len, reverse=True):
        text = text.replace(value, '<redacted>')
    kept = {value: '<kept-%d>' % index for index, value in enumerate(allowed)}
    for value, mark in kept.items():
        text = text.replace(value, mark)
    for pattern in (JWT, UUID):
        text = pattern.sub('<redacted>', text)
    text = RUN.sub(lambda match: '<redacted>' if leaks(match[0]) else match[0], text)
    for value, mark in kept.items():
        text = text.replace(mark, value)
    try:
        data = json.loads(text)
        data['sanitizer_findings'] = findings
        text = json.dumps(data, indent=1, sort_keys=True, ensure_ascii=True) + '\n'
    except ValueError:
        text = None
    if text is None or leaks(text, secrets, allowed):
        text = json.dumps({'schema': 1, 'probe': 'lx-live-codex', 'outcome': 'sanitizer-blocked',
                           'sanitizer_findings': findings}, indent=1) + '\n'
    return text, findings


def attempt(alias, out, account, binary, binary_sha, version, *, config, upstream, tunnel, guard_command, delay,
            second, environ, stdout, stderr, read_timeout):
    run = Attempt(alias, out, account, binary, binary_sha, version, config=config, upstream=upstream, tunnel=tunnel,
                  guard_command=guard_command, delay=delay, second=second, environ=environ,
                  read_timeout=read_timeout)
    findings = []
    status = 3
    try:
        status = run.execute()
    except KeyboardInterrupt:
        run.result['outcome'] = 'interrupted'
        status = 130
    except Exception as error:  # recorded by class only; messages may hold paths
        run.result['outcome'] = 'error:' + type(error).__name__
        status = 3
    finally:
        findings = run.finish()
    exchanges = (run.result.get('gate') or {}).get('exchanges', [])
    say(stdout, json.dumps({'outcome': run.result['outcome'], 'sanitizer_findings': findings,
                            'forwarded': (run.result.get('gate') or {}).get('forwarded'),
                            'statuses': [item.get('status') for item in exchanges],
                            'result_file': RESULT}))  # inside --out; no path is printed
    return 3 if findings and status == 0 else status


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='LX: one reviewed live Codex usage read (research probe).')
    parser.add_argument('--account', required=True)
    parser.add_argument('--out', required=True, help='new or empty private directory outside any Git work tree')
    parser.add_argument('--delay', type=float, default=DEFAULT_DELAY, help='seconds before the second read')
    parser.add_argument('--single-read', action='store_true')
    parser.add_argument('--expect-version', default=EXPECTED_VERSION)
    args = parser.parse_args(argv)
    try:
        if args.account != APPROVED_ACCOUNT:
            raise Refused('account-not-approved')
        if not 0 < args.delay <= MAX_DELAY:
            raise Refused('delay-out-of-range')
        if not VERSION.fullmatch(args.expect_version):
            raise Refused('expect-version-malformed')
        if sys.platform != 'linux' or platform.machine() != 'x86_64':
            raise Refused('platform-unsupported')  # the network guard is Linux x86_64 only
        if not trust_store_loaded():
            raise Refused('system-trust-store-empty')
        config = settings.load()
    except Refused as error:
        say(sys.stderr, 'refused before spawn: ' + error.reason)
        return 2
    except ValueError as error:
        say(sys.stderr, 'refused before spawn: configuration (' + str(error) + ')')
        return 2
    return run_probe(args.account, Path(args.out), config=config, upstream=gates.production_upstream(),
                     tunnel=gates.production_tunnel, guard_command=GUARD_COMMAND, delay=args.delay,
                     second=not args.single_read, expect_version=args.expect_version)


if __name__ == '__main__':
    sys.exit(main())
