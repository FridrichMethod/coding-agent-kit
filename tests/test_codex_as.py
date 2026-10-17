"""Isolated HOME, fabricated JSON and fake executable; never runs real Codex."""

import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "common/codex/.local"
SHELLS = [shutil.which(name) for name in ("bash", "zsh") if shutil.which(name)]
TOKENS = ["fabricated-sensitive-access-A", "fabricated-sensitive-access-B", "fabricated-refreshed-A",
          "fabricated-root-native-access"]
ENV_CONFLICTS = (
    "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY",
    "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_ORGANIZATION", "OPENAI_PROJECT", "OPENAI_FEDERATION_RULE_ID",
    "OPENAI_WORKLOAD_IDENTITY_CONTEXT", "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
    "CODEX_REVOKE_TOKEN_URL_OVERRIDE", "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
    "CODEX_APP_SERVER_LOGIN_ISSUER", "CODEX_AUTHAPI_BASE_URL",
    "CODEX_AGENT_IDENTITY_AUTHAPI_BASE_URL", "CODEX_AGENT_IDENTITY_JWKS_BASE_URL",
)
# Read by no supported native release; common in other OpenAI tools.
# OPENAI_BASE_URL only routes the user's own API key for tool subprocesses.
ENV_UNUSED = ("OPENAI_ORG_ID", "OPENAI_PROJECT_ID", "CODEX_CHATGPT_BASE_URL", "CODEX_CHATGPT_ACCOUNT_ID", "OPENAI_BASE_URL")
FAKE = r'''
import json, os, pathlib, sys, time, signal
if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_VERSION", "codex-cli 0.163.0"))
    sys.exit(0)
args = sys.argv[1:]
home = pathlib.Path(os.environ["HOME"])
account_home = pathlib.Path(os.environ["CODEX_HOME"])
# Help, update and completion run in the root home, exactly as given.
assert account_home == home / ".codex" or args[:2] == ["-c", 'cli_auth_credentials_store="file"']
auth = account_home / "auth.json"
mode = os.environ.get("FAKE_MODE", "normal")
record = {"args": args, "cwd": os.getcwd(), "stdin": sys.stdin.read(),
          "codex_home": str(account_home), "sqlite_home": os.environ.get("CODEX_SQLITE_HOME"),
          "tty": [os.isatty(n) for n in (0, 1, 2)], "umask": os.umask(0o077)}
pathlib.Path(os.environ["FAKE_RECORD"]).write_text(json.dumps(record))
if mode in ("refresh", "rename", "diverged"):
    canonical = auth.resolve()
    data = json.loads(auth.read_text())
    data["tokens"]["access_token"] = "fabricated-refreshed-A"
    data["tokens"]["refresh_token"] = "fabricated-new-refresh-A"
    if os.environ.get("FAKE_ID_TOKEN"):
        data["tokens"]["id_token"] = os.environ["FAKE_ID_TOKEN"]
    if mode == "refresh":
        auth.write_text(json.dumps(data))
    else:
        temporary = auth.with_suffix(".new")
        temporary.write_text(json.dumps(data))
        os.replace(temporary, auth)
        if mode == "diverged":
            other = dict(data)
            other["tokens"] = dict(data["tokens"], access_token="fixture-other-refresh")
            canonical.write_text(json.dumps(other))
if mode == "logout":
    auth.unlink()  # native logout removes the account link after revoking its tokens
if mode == "hold":
    if os.environ.get("FAKE_SIGINT_COUNT"):
        count = [0]
        def interrupted(signum, frame):
            count[0] += 1
            pathlib.Path(os.environ["FAKE_SIGINT_COUNT"]).write_text(str(count[0]))
        signal.signal(signal.SIGINT, interrupted)
    pathlib.Path(os.environ["FAKE_READY"]).write_text(str(os.getpid()))
    while not pathlib.Path(os.environ["FAKE_GATE"]).exists():
        time.sleep(.01)
print("stdout sentinel")
print("stderr sentinel", file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


class HandoffDaemon:
    """Small local protocol peer; writer locks and archive moves are real."""

    def __init__(self, root, thread, child, already_archived, unrelated):
        self.root, self.thread = root, thread
        self.parents = {thread: None, child: thread, already_archived: child, unrelated: None}
        self.original_archived = {already_archived}
        self.archived = set(self.original_archived)
        self.writers = {}
        self.mutations = []
        self.fail_restore = False
        self.fail_archive = False
        # Loaded sessions are idle unless set; None omits the field like an unknown backend.
        self.statuses = {}
        # Sessions start in the launcher's directory unless set.
        self.cwds = {}
        # Top-level sessions are "cli" unless set; spawned agents are always subAgent.
        self.sources = {}
        # Every session has a first message and the default provider unless set.
        self.previews, self.providers = {}, {}
        # Most recently updated first: the session, its agents, then the unrelated one.
        self.updated = {thread: 4, child: 3, already_archived: 2, unrelated: 1}
        self.version = '0.162.1'
        self.originator = 'codex_cli_rs'
        self.agent_suffix = ''
        self.claimed_home = root
        self.names = {}
        for name in self.parents:
            directory = root / ('archived_sessions' if name in self.archived else 'sessions')
            directory.mkdir(exist_ok=True)
            (directory / (name + '.jsonl')).write_text('saved fixture history\n')
            lock = root / 'thread-writer-locks' / (name + '.lock')
            lock.parent.mkdir(mode=0o700, exist_ok=True)
            stream = lock.open('wb')
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.writers[name] = stream
        logical = root / 'app-server-control/app-server-control.sock'
        logical.parent.mkdir(mode=0o700, exist_ok=True)
        directory = Path('/tmp').resolve() / f'codex-daemon-{os.geteuid()}'
        directory.mkdir(mode=0o700, exist_ok=True)
        self.path = directory / hashlib.sha256(os.fsencode(logical)).hexdigest()
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.path))
        self.server.listen()
        self.server.settimeout(.1)
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self.serve, daemon=True)
        self.worker.start()

    def close(self):
        self.stopped.set()
        self.worker.join(timeout=3)
        self.server.close()
        self.path.unlink(missing_ok=True)
        for stream in self.writers.values():
            stream.close()

    @staticmethod
    def read_exact(stream, count):
        value = stream.read(count)
        if len(value) != count:
            raise EOFError
        return value

    def serve(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.server.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(2)
                try:
                    self.serve_client(connection)
                except (EOFError, OSError):
                    pass

    def serve_client(self, connection):
        with connection.makefile('rb') as stream:
            self.read_exact(stream, len(b'GET / HTTP/1.1\r\n'))
            headers = {}
            while (line := stream.readline()) != b'\r\n':
                if not line:
                    raise EOFError
                name, value = line.decode().split(':', 1)
                headers[name.lower()] = value.strip()
            key = headers['sec-websocket-key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
            accept = base64.b64encode(hashlib.sha1(key.encode()).digest())
            connection.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                               b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
            while True:
                first, second = self.read_exact(stream, 2)
                length = second & 127
                if length == 126:
                    length = struct.unpack('!H', self.read_exact(stream, 2))[0]
                elif length == 127:
                    length = struct.unpack('!Q', self.read_exact(stream, 8))[0]
                mask = self.read_exact(stream, 4) if second & 128 else b'\0' * 4
                payload = self.read_exact(stream, length)
                payload = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
                if first & 15 == 8:
                    return
                message = json.loads(payload)
                if 'id' not in message:
                    continue
                try:
                    result = self.dispatch(message['method'], message.get('params', {}))
                    reply = {'id': message['id'], 'result': result}
                except (ValueError, KeyError, TypeError):
                    reply = {'id': message['id'], 'error': {'code': -32600, 'message': 'fixture refusal'}}
                encoded = json.dumps(reply).encode()
                header = bytes((129, len(encoded))) if len(encoded) < 126 else b'\x81\x7e' + struct.pack('!H', len(encoded))
                connection.sendall(header + encoded)

    def dispatch(self, method, params):
        if method == 'initialize':
            return {'codexHome': str(self.claimed_home), 'userAgent': self.originator + '/' + self.version + self.agent_suffix,
                    'platformFamily': 'unix', 'platformOs': 'linux'}
        if method == 'thread/loaded/list':
            return {'data': list(self.writers), 'nextCursor': None}
        if method == 'thread/list' and 'ancestorThreadId' in params:
            ids = [name for name in self.subtree(params['ancestorThreadId'])[1:]
                   if (name in self.archived) == params['archived']]
            return {'data': [{'id': name} for name in ids], 'nextCursor': None}
        if method == 'thread/list':
            # Native resume --last: newest first, archived excluded, filtered by source kind.
            assert params['sortKey'] == 'updated_at' and params['modelProviders'] == ['openai']
            cwds = params.get('cwd')
            allowed = None if cwds is None else {os.path.realpath(c) for c in ([cwds] if isinstance(cwds, str) else cwds)}
            ids = sorted((name for name in self.parents if (name in self.archived) == params['archived']
                          and self.source(name) in params['sourceKinds']
                          and self.providers.get(name, 'openai') in params['modelProviders']
                          and (allowed is None or os.path.realpath(self.cwds.get(name, os.getcwd())) in allowed)),
                         key=lambda name: -self.updated[name])
            return {'data': [self.listing(name) for name in ids][:params['limit']], 'nextCursor': None}
        name = params['threadId']
        if method == 'thread/read':
            status = self.statuses.get(name, {'type': 'idle'}) if name in self.writers else {'type': 'notLoaded'}
            return {'thread': {**self.listing(name), **({'status': status} if status is not None else {}),
                               # Native thread/read marks spawned agents in source, not parentThreadId.
                               'parentThreadId': None, 'name': self.names.get(name),
                               'preview': self.previews.get(name, 'fixture first message'),
                               'modelProvider': self.providers.get(name, 'openai'),
                               'source': {'subAgent': {'thread_spawn': {'parent_thread_id': self.parents[name]}}}
                               if self.parents[name] else self.source(name)}}
        if method == 'thread/archive':
            names = self.subtree(name)
            for item in names:
                if item in self.archived:
                    continue
                writer = self.writers.pop(item, None)  # A spawned agent need not be loaded.
                if writer is not None:
                    writer.close()
                (self.root / 'sessions' / (item + '.jsonl')).rename(self.root / 'archived_sessions' / (item + '.jsonl'))
                self.archived.add(item)
                self.mutations.append((method, item))
                if self.fail_archive:
                    raise ValueError('partial archive')
            return {}
        if method == 'thread/unarchive':
            if self.fail_restore or name not in self.archived:
                raise ValueError('restore failed')
            (self.root / 'archived_sessions' / (name + '.jsonl')).rename(self.root / 'sessions' / (name + '.jsonl'))
            self.archived.remove(name)
            # Like native touch_modified_time: an unarchived session becomes the newest.
            self.updated[name] = max(self.updated.values()) + 1
            self.mutations.append((method, name))
            return {'thread': {'id': name}}
        raise ValueError('unsupported method')

    def source(self, name):
        return 'subAgent' if self.parents[name] else self.sources.get(name, 'cli')

    def listing(self, name):
        directory = 'archived_sessions' if name in self.archived else 'sessions'
        return {'id': name, 'path': str(self.root / directory / (name + '.jsonl')),
                'cwd': self.cwds.get(name, os.getcwd()), 'updatedAt': self.updated[name]}

    def subtree(self, root):
        result = [root]
        for parent in result:
            result.extend(child for child, ancestor in self.parents.items() if ancestor == parent)
        return result


class CodexAs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-as-test-")
        self.addCleanup(self.tmp.cleanup)
        # macOS TMPDIR uses /var -> /private/var; getcwd() reports the physical
        # path. Keep all fixture expectations in that same directory spelling.
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home with spaces"
        self.home.mkdir(mode=0o700)
        self.codex = self.home / ".codex"
        self.codex.mkdir(mode=0o700)
        self.secret = self.home / ".secrets"
        self.secret.mkdir(mode=0o700)
        self.auth = self.codex / "auth.json"
        self.install = self.home / ".local/lib/codex-as"
        shutil.copytree(SOURCE / "lib/codex-as", self.install)
        self.command_bin = self.home / ".local/bin"
        self.command_bin.mkdir()
        shutil.copy2(SOURCE / "bin/codex-as", self.command_bin / "codex-as")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = self.bin / "codex"
        fake.write_text(f"#!{sys.executable}\n" + FAKE)
        fake.chmod(0o700)
        self.cwd = self.base / "current working directory"
        self.cwd.mkdir()
        for name, content in {
            "config.toml": 'model = "unchanged-model"\n',
            "sessions/old.jsonl": "old session\n",
            "history.jsonl": "old history\n",
            "skills/example/SKILL.md": "old skill\n",
            "mcp.json": "old MCP configuration\n",
        }.items():
            path = self.codex / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for name, token in zip(("A", "B"), TOKENS):
            self.write_credential(name, {
                "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                "tokens": {"id_token": "fake." + base64.urlsafe_b64encode(json.dumps({"sub": "fixture-" + name}).encode()).decode().rstrip("=") + ".fake", "access_token": token,
                           "refresh_token": "fabricated-refresh-" + name, "account_id": name},
                "last_refresh": "2026-10-01T00:00:00Z",
                "unknown": {"preserve": [1, {"nested": True}]},
            })
        # A native root login of its own; codex-as never reads or changes it.
        self.auth.write_text(json.dumps({
            "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"id_token": "fake." + base64.urlsafe_b64encode(b'{"sub": "fixture-root"}').decode().rstrip("=") + ".fake",
                       "access_token": TOKENS[3], "refresh_token": "fabricated-refresh-root", "account_id": "root"},
            "last_refresh": "2026-10-01T00:00:00Z",
        }))
        self.auth.chmod(0o600)
        self.root_auth = self.auth.read_bytes()
        self.shared = self.snapshot()
        self.env = os.environ.copy()
        for name in (*ENV_CONFLICTS, "CODEX_HOME", "CODEX_SQLITE_HOME", "PYTHONPATH", "BASH_ENV", "ENV", "ZDOTDIR"):
            self.env.pop(name, None)
        self.record = self.base / "record.json"
        self.env.update(HOME=str(self.home), PATH=os.pathsep.join((str(self.bin), str(self.command_bin), self.env["PATH"])),
                        FAKE_RECORD=str(self.record))

    def write_credential(self, name, data):
        path = self.secret / f"codex-{name}.json"
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def snapshot(self):
        return {str(p.relative_to(self.codex)): (hashlib.sha256(p.read_bytes()).hexdigest(),
                stat.S_IMODE(p.stat().st_mode)) for p in self.codex.rglob("*") if p.is_file()}

    def run_as(self, shell, args, env=None, input="", setup=""):
        flags = ["-f"] if Path(shell).name == "zsh" else ["--noprofile", "--norc"]
        command = [shell, *flags, "-c", setup + 'command codex-as "$@"',
                   "codex-as-test", *args]
        result = subprocess.run(command, cwd=self.cwd, env=self.env | (env or {}),
                                input=input, text=True, capture_output=True, timeout=10)
        for token in (*TOKENS, "fabricated-new-refresh-A", "fabricated-conflict-secret"):
            if token in result.stdout + result.stderr:
                self.fail("secret appeared in output (values hidden)")
        return result

    def rejected(self, shell, args, env=None):
        before = self.auth.read_bytes() if self.auth.is_file() else None
        link = os.readlink(self.auth) if self.auth.is_symlink() else None
        self.record.unlink(missing_ok=True)
        result = self.run_as(shell, args, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.record.exists(), "refusal must not launch fake Codex")
        self.assertEqual(os.readlink(self.auth) if self.auth.is_symlink() else None, link)
        self.assertTrue((self.auth.read_bytes() if self.auth.is_file() else None) == before,
                        "refusal modified credentials (values hidden)")
        return result

    def test_switch_and_refresh(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                for name in ("A", "B", "A"):
                    result = self.run_as(shell, [name])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(os.readlink(self.codex / "accounts" / name / "auth.json"), str(self.secret / f"codex-{name}.json"))
                before_b = (self.secret / "codex-B.json").read_bytes()
                self.assertEqual(self.run_as(shell, ["A"], {"FAKE_MODE": "refresh"}).returncode, 0)
                self.assertTrue((self.codex / "accounts/A/auth.json").is_symlink())
                for name in ("B", "A"):
                    self.assertEqual(self.run_as(shell, [name]).returncode, 0)
                data = json.loads((self.secret / "codex-A.json").read_text())
                self.assertTrue(data["tokens"]["access_token"] == TOKENS[2], "refresh not retained (values hidden)")
                self.assertTrue(data["tokens"]["refresh_token"] == "fabricated-new-refresh-A", "refresh token not retained (values hidden)")
                self.assertEqual(data["unknown"], {"preserve": [1, {"nested": True}]})
                self.assertTrue((self.secret / "codex-B.json").read_bytes() == before_b,
                                "wrong account was modified (values hidden)")
                snapshot = self.snapshot()
                self.assertEqual({k: snapshot[k] for k in self.shared}, self.shared)
                self.assertEqual(list(self.codex.glob(".codex-as-*")), [])

    def test_accounts_have_fixed_auth_and_shared_state(self):
        root_before = self.auth.read_bytes()
        for name in ("A", "B", "A"):
            result = self.run_as(SHELLS[0], [name, "exec", "prompt"])
            self.assertEqual(result.returncode, 0, result.stderr)
            account_home = self.codex / "accounts" / name
            self.assertEqual(os.readlink(account_home / "auth.json"), str(self.secret / f"codex-{name}.json"))
            for shared in ("config.toml", "sessions", "archived_sessions", "skills", "history.jsonl", "thread-writer-locks"):
                self.assertEqual((account_home / shared).resolve(), self.codex / shared)
            self.assertEqual(self.auth.read_bytes(), root_before)
            record = json.loads(self.record.read_text())
            self.assertEqual(record["codex_home"], str(account_home))
            self.assertEqual(record["sqlite_home"], str(self.codex))

    def test_named_resume_is_refused_but_ids_and_prompts_are_preserved(self):
        for args in (["resume", "friendly name"], ["resume", "--", "friendly name"],
                     ["fork", "--", "friendly name"], ["exec", "resume", "--", "friendly name"]):
            result = self.run_as(SHELLS[0], ["A", *args])
            self.assertNotEqual(result.returncode, 0, args)
            self.assertIn("UUID", result.stderr)
        for args in (["resume", "12345678-1234-4234-8234-123456789abc", "follow-up"],
                     ["resume", "--last", "follow-up"], ["exec", "friendly name"]):
            result = self.run_as(SHELLS[0], ["A", *args])
            self.assertEqual(result.returncode, 0, result.stderr)

    def handoff_fixture(self, answer='y'):
        ids = ['12345678-1234-4234-8234-123456789ab' + char for char in 'cdef']
        daemon = HandoffDaemon(self.codex, *ids)
        self.addCleanup(daemon.close)
        spec = importlib.util.spec_from_file_location('handoff_test_launch', self.install / 'launch.py')
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        prompts = []
        tty = SimpleNamespace(write=lambda *args: None, readline=lambda: prompts.append(answer) or answer,
                              close=lambda: None)
        attempts = []

        def launch_after_release(command, env, fd=None):
            attempts.append((command, env))
            with (self.codex / 'thread-writer-locks' / (ids[0] + '.lock')).open('rb') as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return 77
            return 0

        def run(native_args=None):
            with patch.dict(os.environ, self.env, clear=True), \
                 patch.object(launcher.guard, 'terminal', return_value=tty), \
                 patch.object(launcher, 'run_native', side_effect=launch_after_release):
                return launcher.main(['B', *(native_args if native_args is not None else ['resume', ids[0]])])

        run.launcher, run.prompts = launcher, prompts
        return daemon, ids, attempts, run

    def test_idle_handoff_takes_over_without_prompt(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        root_auth = self.auth.read_bytes()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(run(), 0, 'account backend must start only after the old writer releases the UUID')
        self.assertEqual(run.prompts, [], 'an idle session needs no confirmation')
        self.assertIn('took over ' + ids[0], stderr.getvalue())
        self.assertEqual(attempts[0][1]['CODEX_HOME'], str(self.codex / 'accounts/B'))
        self.assertIn('--no-daemon', attempts[0][0])
        self.assertEqual(daemon.archived, {ids[2]})
        self.assertIn(ids[3], daemon.writers, 'unrelated session must remain loaded')
        self.assertEqual(self.auth.read_bytes(), root_auth)
        self.assertFalse(list((self.codex / 'accounts/.locks').glob('*.handoff.*')), 'no journal or lease remains')

    def test_possibly_running_handoff_asks_first(self):
        # A running turn, a running spawned agent, or a backend without status.
        for target, status in ((0, {'type': 'active', 'activeFlags': []}),
                               (1, {'type': 'active', 'activeFlags': ['waitingOnApproval']}), (0, None)):
            with self.subTest(target=target, status=status):
                daemon, ids, attempts, run = self.handoff_fixture()
                daemon.statuses[ids[target]] = status
                self.assertEqual(run(), 0)
                self.assertEqual(len(run.prompts), 1)
                self.assertEqual(daemon.archived, {ids[2]})
                daemon.close()

    def test_native_group_writable_lock_is_accepted_only_in_private_directory(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        (self.codex / 'thread-writer-locks' / (ids[0] + '.lock')).chmod(0o664)
        directory = self.codex / 'thread-writer-locks'
        directory.chmod(0o775)
        with self.assertRaisesRegex(ValueError, 'directory.*0700'):
            run()
        self.assertEqual(daemon.mutations, [])
        self.assertEqual(attempts, [])
        directory.chmod(0o700)
        self.assertEqual(run(), 0)

    def test_help_fork_exec_and_prompt_operands_never_trigger_takeover(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        for args in (['resume', ids[0], '--help'], ['fork', ids[0]], ['fork', '--last'],
                     ['exec', 'resume', ids[0]], ['exec', 'resume', '--last'], ['--', 'resume', ids[0]],
                     ['--model', 'resume', 'ordinary prompt'], []):
            with self.subTest(args=args):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    run(args)
                self.assertEqual(daemon.mutations, [])
                self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(len(attempts), 8)

    def test_picker_takes_over_idle_sessions_before_selection(self):
        for args in (['resume'], ['resume', '--all'], ['resume', '--all', '--include-non-interactive']):
            with self.subTest(args=args):
                daemon, ids, attempts, run = self.handoff_fixture()
                daemon.names[ids[3]] = 'unrelated\nsession \x1b[31mname'
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(run(args), 0, 'the selection must find the session released')
                text = stderr.getvalue()
                self.assertIn(ids[0], text)
                self.assertIn(ids[3] + '  unrelated session [31mname', text)
                # Spawned agents follow their parent; only top-level sessions are named.
                self.assertNotIn(ids[1], text)
                self.assertNotIn(ids[2], text)
                self.assertEqual(run.prompts, [])
                self.assertEqual([item for method, item in daemon.mutations if method == 'thread/archive'],
                                 [ids[0], ids[1], ids[3]])
                self.assertEqual(daemon.archived, {ids[2]}, 'saved visibility is restored')
                self.assertEqual(len(attempts), 1)
                daemon.close()

    def test_last_takes_over_only_the_session_it_opens(self):
        # Native --last opens the newest listed session; taking more over would make another newest.
        for args in (['resume', '--last'], ['resume', '--all', '--last', 'follow-up']):
            with self.subTest(args=args):
                daemon, ids, attempts, run = self.handoff_fixture()
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(run(args), 0)
                self.assertEqual([item for method, item in daemon.mutations if method == 'thread/archive'],
                                 [ids[0], ids[1]])
                self.assertIn(ids[3], daemon.writers, 'an older session stays with the root daemon')
                self.assertNotIn(ids[3], stderr.getvalue())
                daemon.close()
        for label in ('newest', 'newest elsewhere', 'newest not loaded', 'newest non-interactive'):
            with self.subTest(label=label):
                daemon, ids, attempts, run = self.handoff_fixture()
                daemon.updated[ids[3]] = 10
                if label == 'newest elsewhere':
                    daemon.cwds[ids[3]] = str(self.cwd)
                elif label == 'newest not loaded':
                    daemon.writers.pop(ids[3]).close()
                elif label == 'newest non-interactive':
                    daemon.sources[ids[3]] = 'exec'
                with contextlib.redirect_stderr(io.StringIO()):
                    run(['resume', '--last'])
                archived = [item for method, item in daemon.mutations if method == 'thread/archive']
                expected = {'newest': [ids[3]], 'newest not loaded': []}.get(label, [ids[0], ids[1]])
                self.assertEqual(archived, expected)
                daemon.close()
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.updated[ids[3]] = 10
        daemon.sources[ids[3]] = 'exec'
        with contextlib.redirect_stderr(io.StringIO()):
            run(['resume', '--last', '--include-non-interactive'])
        self.assertEqual([item for method, item in daemon.mutations if method == 'thread/archive'], [ids[3]])

    def test_picker_takes_over_only_sessions_the_picker_shows(self):
        # Native lists no session without a first message, nor another provider's.
        for setting in ('preview', 'provider'):
            with self.subTest(setting=setting):
                daemon, ids, attempts, run = self.handoff_fixture()
                if setting == 'preview':
                    daemon.previews[ids[3]] = ''
                else:
                    daemon.providers[ids[3]] = 'other'
                with contextlib.redirect_stderr(io.StringIO()):
                    run(['resume', '--all'])
                self.assertIn(ids[3], daemon.writers)
                self.assertNotIn(ids[0], daemon.writers)
                daemon.close()

    def test_recovery_alone_does_not_take_a_session_over_again(self):
        # A journaled session the daemon still holds idle, outside --last's choice.
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.cwds[ids[3]] = str(self.cwd)
        locks = self.codex / 'accounts/.locks'
        locks.parent.mkdir(mode=0o700, exist_ok=True)
        locks.mkdir(mode=0o700, exist_ok=True)
        journal = locks / (ids[3] + '.handoff.json')
        journal.write_text(json.dumps({'version': 1, 'thread': ids[3], 'archived': []}) + '\n')
        journal.chmod(0o600)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run(['resume', '--last']), 0)
        self.assertFalse(journal.exists())
        self.assertIn(ids[3], daemon.writers, 'recovery restores visibility; it does not close the session')
        self.assertNotIn(ids[0], daemon.writers)

    def test_picker_lists_interactive_sources_unless_asked(self):
        for args, taken in ((['resume'], False), (['resume', '--include-non-interactive'], True)):
            with self.subTest(args=args):
                daemon, ids, attempts, run = self.handoff_fixture()
                daemon.sources[ids[3]] = 'appServer'
                with contextlib.redirect_stderr(io.StringIO()):
                    run(args)
                self.assertEqual(ids[3] not in daemon.writers, taken)
                self.assertNotIn(ids[0], daemon.writers)
                daemon.close()

    def test_unrecognized_resume_option_skips_takeover(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        for args in (['resume', '--lats'], ['resume', ids[0], '--future-option'], ['--future-root', 'resume']):
            with self.subTest(args=args):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    run(args)
                self.assertIn('unrecognized option', stderr.getvalue())
                self.assertEqual(daemon.mutations, [], 'a typo must not close sessions before clap reports it')

    def test_picker_takes_over_only_sessions_it_would_list(self):
        for args, outside_taken in ((['resume'], False), (['resume', '--all'], True)):
            with self.subTest(args=args):
                daemon, ids, attempts, run = self.handoff_fixture()
                daemon.cwds[ids[3]] = str(self.cwd)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(run(args), 0)
                self.assertIn(ids[0], stderr.getvalue())
                self.assertEqual(ids[3] in stderr.getvalue(), outside_taken)
                self.assertEqual(ids[3] not in daemon.writers, outside_taken)
                daemon.close()
        # -C moves the filter like native Codex.
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.cwds[ids[3]] = str(self.cwd)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run(['-C', str(self.cwd), 'resume']), 77, 'the other directory stays held')
        self.assertNotIn(ids[3], daemon.writers)
        self.assertIn(ids[0], daemon.writers)

    def test_picker_scope_includes_the_same_place_in_linked_worktrees(self):
        git = ['git', '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.invalid']
        # A pre-commit hook inherits GIT_INDEX_FILE and friends from the outer commit.
        env = {name: value for name, value in os.environ.items() if not name.startswith('GIT_')}
        main, linked = self.base / 'repository', self.base / 'linked checkout'
        (main / 'sub').mkdir(parents=True)
        for args in (['init', '-q', str(main)], ['-C', str(main), 'commit', '-q', '--allow-empty', '-m', 'fixture'],
                     ['-C', str(main), 'worktree', 'add', '-q', '--detach', str(linked)]):
            subprocess.run(git + args, env=env, check=True)
        (linked / 'sub').mkdir()
        (linked / 'other').mkdir()
        # resume --last asks the daemon with native's path list: here, and the same place elsewhere.
        spec = importlib.util.spec_from_file_location('places_test_handoff', self.install / 'handoff.py')
        handoff = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(handoff)
        self.assertEqual(handoff.places(main / 'sub'), [str(main / 'sub'), os.path.realpath(linked / 'sub')])
        self.assertEqual(handoff.places(self.cwd), [str(self.cwd)])
        for cwd, taken in ((linked / 'sub', True), (linked / 'other', False), (linked, False)):
            with self.subTest(cwd=cwd.name):
                daemon, ids, attempts, run = self.handoff_fixture()
                daemon.cwds[ids[3]] = str(cwd)
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(run(['--cd', str(main / 'sub'), 'resume']), 77)
                self.assertEqual(ids[3] not in daemon.writers, taken)
                self.assertIn(ids[0], daemon.writers, 'sessions of the launcher directory are outside --cd')
                daemon.close()

    def test_picker_scope_arguments(self):
        spec = importlib.util.spec_from_file_location('scope_test_check', self.install / 'check.py')
        check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(check)
        here, uuid_text = Path.cwd(), '12345678-1234-4234-8234-123456789abc'
        for args, scope in ((['resume'], here), (['resume', '--last'], here), (['resume', '--all'], 'all'),
                            (['resume', '--all', '--last'], 'all'), (['resume', '-C', str(self.cwd)], self.cwd),
                            (['--cd', str(self.cwd), 'resume', '--all'], 'all'),
                            (['resume', '-m', '--all'], here),  # --all is the model value here
                            (['--all', 'resume'], here), (['resume', uuid_text, '--all'], None),
                            (['fork', '--all'], None), (['exec', 'resume', '--all'], None),
                            (['resume', '--help'], None), (['--model', 'resume', 'prompt'], None), ([], None),
                            # A spaced -i takes every following word outside exec, as in clap.
                            (['-i', 'a.png', 'resume'], None), (['resume', '-i', 'shot.png', uuid_text], here),
                            (['exec', '-i', 'a.png', 'resume', uuid_text], None)):
            with self.subTest(args=args):
                self.assertEqual(check.arguments(args).scope, scope)
        for args in (['fork', uuid_text], ['exec', 'resume', uuid_text], ['exec', 'fork', uuid_text]):
            with self.subTest(args=args):
                self.assertEqual(check.arguments(args).recover_id, uuid_text)

    def test_picker_leaves_possibly_running_sessions_and_names_them(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.statuses[ids[3]] = {'type': 'active', 'activeFlags': []}
        daemon.statuses[ids[1]] = None
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(run(['resume']), 77, 'the running session stays with the root daemon')
        text = stderr.getvalue()
        self.assertIn('left running', text)
        self.assertIn(ids[0], text)
        self.assertIn(ids[3], text)
        self.assertIn('codex-as B resume UUID', text)
        self.assertEqual(daemon.mutations, [])
        self.assertEqual(run.prompts, [])
        daemon.close()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(run(['resume']), 0)
        self.assertEqual(stderr.getvalue(), '', 'no daemon, no output')

    def test_picker_failure_warns_and_still_launches(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.fail_restore = True
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            run(['resume'])
        self.assertIn('could not take over ' + ids[0], stderr.getvalue())
        self.assertEqual(len(attempts), 1)
        self.assertTrue((self.codex / 'accounts/.locks' / (ids[0] + '.handoff.json')).exists())
        # The next picker launch finishes the interrupted takeover first.
        daemon.fail_restore = False
        self.assertEqual(run(['resume']), 0)
        self.assertEqual(daemon.archived, {ids[2]})
        self.assertFalse(list((self.codex / 'accounts/.locks').glob('*.handoff.json')))

    def test_picker_survives_malformed_records_and_daemon_replies(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        locks = self.codex / 'accounts/.locks'
        locks.parent.mkdir(mode=0o700, exist_ok=True)
        locks.mkdir(mode=0o700, exist_ok=True)
        journal = locks / (ids[0] + '.handoff.json')
        journal.write_text('[]\n')
        journal.chmod(0o600)
        client = run.launcher.handoff.Client
        request = client.request

        def null_read(connection, method, params, **options):
            if method == 'thread/read' and params['threadId'] == ids[3]:
                return {'thread': None}
            return request(connection, method, params, **options)

        stderr = io.StringIO()
        with patch.object(client, 'request', new=null_read), contextlib.redirect_stderr(stderr):
            self.assertEqual(run(['resume']), 77)
        self.assertIn('could not take over ' + ids[0], stderr.getvalue())
        self.assertIn('could not take over ' + ids[3], stderr.getvalue())
        self.assertTrue(journal.exists(), 'an unrecognized record is preserved')
        self.assertEqual(daemon.mutations, [])
        with self.assertRaisesRegex(ValueError, 'recovery record'):
            run()
        self.assertEqual(len(attempts), 1, 'an explicit UUID never launches with unknown recovery')

    def test_picker_recovers_spawned_agent_journal_and_skips_busy_lease(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.fail_restore = True
        with self.assertRaises(ValueError):
            run(['resume', ids[1]])
        journal = self.codex / 'accounts/.locks' / (ids[1] + '.handoff.json')
        self.assertTrue(journal.exists())
        daemon.fail_restore = False
        lease = os.open(self.codex / 'accounts/.locks' / (ids[3] + '.handoff.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lease)
        fcntl.flock(lease, fcntl.LOCK_EX)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(run(['resume']), 0)
        self.assertFalse(journal.exists(), 'a spawned agent journal is recovered too')
        self.assertIn('in progress', stderr.getvalue())
        self.assertIn(ids[3], daemon.writers, 'another takeover owns that session')
        self.assertEqual(daemon.archived, {ids[2]})

    def test_torn_journal_means_archive_never_started(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        locks = self.codex / 'accounts/.locks'
        locks.parent.mkdir(mode=0o700, exist_ok=True)
        locks.mkdir(mode=0o700, exist_ok=True)
        journal = locks / (ids[0] + '.handoff.json')
        # A crash between the exclusive create and the synced write.
        journal.write_text('{"version": 1, "thr')
        journal.chmod(0o600)
        self.assertEqual(run(), 0)
        self.assertFalse(journal.exists())
        self.assertEqual(daemon.archived, {ids[2]})

    def test_last_does_not_launch_while_its_session_may_be_archived(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        client = run.launcher.handoff.Client
        request = client.request

        def timed_out(connection, method, params, **options):
            if method == 'thread/archive':
                raise TimeoutError('archive outcome unknown')
            return request(connection, method, params, **options)

        with patch.object(client, 'request', new=timed_out), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'unknown'):
                run(['resume', '--last'])
        self.assertEqual(attempts, [], 'native --last would open another session')
        daemon.dispatch('thread/archive', {'threadId': ids[0]})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run(['resume', '--last']), 0, 'recovery makes it the newest; then it is taken over')
        self.assertEqual(daemon.archived, {ids[2]})

    def test_torn_journal_needs_no_daemon(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.close()
        locks = self.codex / 'accounts/.locks'
        locks.parent.mkdir(mode=0o700, exist_ok=True)
        locks.mkdir(mode=0o700, exist_ok=True)
        for args in (None, ['resume']):
            with self.subTest(args=args):
                journal = locks / (ids[0] + '.handoff.json')
                journal.write_text('')
                journal.chmod(0o600)
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    run(args)
                self.assertFalse(journal.exists())
                self.assertNotIn('recovery record', stderr.getvalue())
        self.assertEqual(len(attempts), 2)

    def test_fork_and_exec_resume_finish_an_interrupted_takeover(self):
        for args in (['fork', '{id}'], ['exec', 'resume', '{id}', 'prompt']):
            with self.subTest(args=args):
                daemon, ids, attempts, run = self.handoff_fixture()
                daemon.fail_restore = True
                with self.assertRaises(ValueError):
                    run()
                daemon.fail_restore = False
                journal = self.codex / 'accounts/.locks' / (ids[0] + '.handoff.json')
                self.assertTrue(journal.exists())
                run([arg.format(id=ids[0]) for arg in args])
                self.assertFalse(journal.exists(), 'the saved session is visible again before it is forked')
                self.assertEqual(daemon.archived, {ids[2]})
                self.assertEqual(len(attempts), 1)
                attempts.clear()
                daemon.close()

    def test_daemon_refusal_is_reported_by_name(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        error = run.launcher.handoff.RpcError('daemon thread/archive refused (-32600).')
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            run.launcher.guard.report(error)
            run.launcher.guard.report(json.JSONDecodeError('fixture', 'quoted secret text', 0))
        self.assertIn('thread/archive refused', stderr.getvalue())
        self.assertNotIn('quoted secret text', stderr.getvalue())

    def test_picker_without_daemon_names_preserved_recovery_records(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.fail_restore = True
        with self.assertRaises(ValueError):
            run()
        daemon.close()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(run(['resume']), 0)
        self.assertIn('recovery records', stderr.getvalue())
        self.assertTrue((self.codex / 'accounts/.locks' / (ids[0] + '.handoff.json')).exists())

    def test_loaded_session_reported_not_loaded_counts_as_running(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.statuses[ids[0]] = {'type': 'notLoaded'}
        self.assertEqual(run(), 0)
        self.assertEqual(len(run.prompts), 1)

    def test_without_controlling_terminal_only_running_handoff_is_refused(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.statuses[ids[0]] = {'type': 'active', 'activeFlags': []}
        (self.codex / 'accounts/.locks').mkdir(parents=True, mode=0o700)
        with patch.object(run.launcher.guard, 'terminal', side_effect=ValueError('controlling terminal required')):
            # run() supplies its own terminal; exercise the production helper directly.
            with self.assertRaisesRegex(ValueError, 'terminal'):
                run.launcher.handoff.prepare(run.launcher.guard, self.codex, 'B', ids[0])
            self.assertEqual(daemon.mutations, [])
            del daemon.statuses[ids[0]]
            run.launcher.handoff.prepare(run.launcher.guard, self.codex, 'B', ids[0])
        self.assertEqual(daemon.archived, {ids[2]})
        self.assertEqual(attempts, [])

    def test_declined_handoff_does_not_mutate_or_launch(self):
        daemon, ids, attempts, run = self.handoff_fixture('n')
        daemon.statuses[ids[0]] = {'type': 'active', 'activeFlags': []}
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            run()
        self.assertEqual(daemon.mutations, [])
        self.assertEqual(attempts, [])
        self.assertIn(ids[0], daemon.writers)

    def test_session_held_elsewhere_launches_native_conflict_view(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.writers.pop(ids[3]).close()
        lock = (self.codex / 'thread-writer-locks' / (ids[3] + '.lock')).open('wb')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            run(['resume', ids[3]])
        self.assertIn('another client', stderr.getvalue())
        self.assertEqual(daemon.mutations, [])
        self.assertEqual(len(attempts), 1, 'native Codex shows its own conflict view')

    def test_partial_archive_restores_visibility_and_does_not_launch(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.fail_archive = True
        with self.assertRaisesRegex(ValueError, 'archive|handoff|takeover'):
            run()
        self.assertIn(('thread/archive', ids[0]), daemon.mutations)
        self.assertEqual(daemon.archived, {ids[2]})
        self.assertEqual(attempts, [])

    def test_failed_restore_keeps_recovery_record_and_retry_recovers(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.fail_restore = True
        with self.assertRaisesRegex(ValueError, 'recover|restore'):
            run()
        journals = list((self.codex / 'accounts/.locks').glob('*.handoff.json'))
        self.assertEqual(len(journals), 1)
        self.assertEqual(stat.S_IMODE(journals[0].stat().st_mode), 0o600)
        self.assertEqual(attempts, [])
        daemon.fail_restore = False
        self.assertEqual(run(), 0)
        self.assertEqual(daemon.archived, {ids[2]})
        self.assertFalse(journals[0].exists())

    def test_handoff_refuses_unknown_daemon_identity_before_mutation(self):
        for bad_home, bad_version in ((True, False), (False, True)):
            for args in (None, ['resume']):
                daemon, ids, attempts, run = self.handoff_fixture()
                daemon.claimed_home = self.cwd if bad_home else self.codex
                daemon.version = '0.160.0' if bad_version else '0.162.1'
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    # Native Codex then shows its own conflict view for the held session.
                    self.assertEqual(run(args), 77)
                self.assertRegex(stderr.getvalue(), 'identity|version')
                self.assertIn('no takeover performed', stderr.getvalue())
                self.assertEqual(daemon.mutations, [])
                self.assertEqual(len(attempts), 1)
                daemon.close()

    def test_desktop_daemon_identity_uses_backend_version(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.originator = 'Codex Desktop'
        daemon.version = '0.161.0'
        daemon.agent_suffix = ' (Ubuntu 24.4.0; x86_64) unknown (codex-tui; 0.162.1)'
        try:
            result = run()
        except ValueError as error:
            self.fail('desktop backend must be accepted: ' + str(error))
        self.assertEqual(result, 0)

    def test_newer_daemon_backend_is_accepted(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.version = '0.170.2'
        self.assertEqual(run(), 0)
        self.assertEqual(daemon.archived, {ids[2]})

    def test_client_suffix_does_not_accept_old_backend_version(self):
        daemon, ids, attempts, run = self.handoff_fixture()
        daemon.originator = 'Codex Desktop'
        daemon.version = '0.160.0'
        daemon.agent_suffix = ' (codex-tui; 0.162.1)'
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(run(), 77)
        self.assertIn('no takeover performed', stderr.getvalue())
        self.assertEqual(daemon.mutations, [])

    def test_timed_out_archive_keeps_journal_until_late_completion_is_recovered(self):
        self.check_ambiguous_archive(TimeoutError('archive outcome unknown'))

    def test_interrupted_archive_keeps_journal_until_late_completion_is_recovered(self):
        self.check_ambiguous_archive(KeyboardInterrupt())

    def check_ambiguous_archive(self, error):
        daemon, ids, attempts, run = self.handoff_fixture()
        client = run.launcher.handoff.Client
        request = client.request

        def interrupted(connection, method, params, **options):
            if method == 'thread/archive':
                raise error
            return request(connection, method, params, **options)

        with patch.object(client, 'request', new=interrupted):
            with self.assertRaises((OSError, ValueError, KeyboardInterrupt)):
                run()
        journals = list((self.codex / 'accounts/.locks').glob('*.handoff.json'))
        self.assertEqual(len(journals), 1, 'an unacknowledged archive may still finish later')
        self.assertEqual(attempts, [])
        # The native operation can finish after the original connection is gone.
        daemon.dispatch('thread/archive', {'threadId': ids[0]})
        self.assertEqual(run(), 0)
        self.assertEqual(daemon.archived, {ids[2]})
        self.assertFalse(journals[0].exists())

    def test_picker_interrupt_cancels_but_unknown_outcome_still_launches(self):
        for error in (KeyboardInterrupt(), TimeoutError('archive outcome unknown')):
            with self.subTest(error=type(error).__name__):
                daemon, ids, attempts, run = self.handoff_fixture()
                client = run.launcher.handoff.Client
                request = client.request

                def interrupted(connection, method, params, request=request, error=error, **options):
                    if method == 'thread/archive' and params['threadId'] == ids[0]:
                        raise error
                    return request(connection, method, params, **options)

                with patch.object(client, 'request', new=interrupted), contextlib.redirect_stderr(io.StringIO()):
                    if isinstance(error, KeyboardInterrupt):
                        with self.assertRaisesRegex(ValueError, 'unknown'):
                            run(['resume'])
                        self.assertEqual(attempts, [], 'Ctrl-C cancels the launch')
                    else:
                        self.assertEqual(run(['resume']), 77)
                        self.assertEqual(len(attempts), 1)
                self.assertTrue((self.codex / 'accounts/.locks' / (ids[0] + '.handoff.json')).exists())
                daemon.dispatch('thread/archive', {'threadId': ids[0]})
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(run(['resume']), 0)
                self.assertEqual(daemon.archived, {ids[2]})
                self.assertFalse(list((self.codex / 'accounts/.locks').glob('*.handoff.json')))
                daemon.close()

    def test_same_account_leases_allow_clients_but_exclude_repairs(self):
        result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
        self.assertEqual(result.returncode, 0, result.stderr)
        account_home = self.codex / "accounts/A"
        auth = account_home / "auth.json"
        lease = self.codex / "accounts/.locks/A.lock"
        with lease.open("rb") as first, lease.open("rb") as second:
            fcntl.flock(first, fcntl.LOCK_SH)
            fcntl.flock(second, fcntl.LOCK_SH | fcntl.LOCK_NB)
            result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
            self.assertEqual(result.returncode, 0, result.stderr)
            raw = auth.read_bytes()
            auth.unlink()
            auth.write_bytes(raw)
            auth.chmod(0o600)
            result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active", result.stderr)
            self.assertFalse(auth.is_symlink())
        result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(auth.is_symlink())

    def start_held(self, account, label, overrides=None):
        ready, gate, record = (self.base / (label + suffix) for suffix in ("-ready", "-gate", "-record"))
        env = self.env | {"FAKE_MODE": "hold", "FAKE_READY": str(ready), "FAKE_GATE": str(gate),
                          "FAKE_RECORD": str(record)} | (overrides or {})
        process = subprocess.Popen([str(self.command_bin / "codex-as"), account, "exec", "prompt"],
                                   cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (process.kill(), process.wait()) if process.poll() is None else None)
        deadline = time.monotonic() + 8
        while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.01)
        self.assertTrue(ready.exists(), "fake client failed to reach its barrier")
        return process, int(ready.read_text()), gate, record

    def test_concurrent_accounts_and_same_account_clients(self):
        processes = [self.start_held(account, str(index)) for index, account in enumerate(("A", "B", "A"))]
        for (process, _, gate, record), account in zip(processes, ("A", "B", "A")):
            data = json.loads(record.read_text())
            self.assertEqual(data["codex_home"], str(self.codex / "accounts" / account))
            self.assertIsNone(process.poll())
            gate.write_text("release")
        for process, _, _, _ in processes:
            self.assertEqual(process.wait(timeout=8), 0)
        self.assertEqual(self.auth.read_bytes(), self.root_auth)

    def test_child_keeps_lease_after_supervisor_is_killed(self):
        process, child_pid, gate, _ = self.start_held("A", "orphan")
        process.kill()
        process.wait(timeout=8)
        try:
            lease = self.codex / "accounts/.locks/A.lock"
            with lease.open("rb") as stream:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            auth = self.codex / "accounts/A/auth.json"
            raw = auth.read_bytes()
            auth.unlink()
            auth.write_bytes(raw)
            auth.chmod(0o600)
            result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active", result.stderr)
        finally:
            gate.write_text("release")
            # Only this test's fabricated child; never a real Codex process.
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def test_interrupt_is_not_duplicated_and_termination_is_forwarded(self):
        counter = self.base / 'interrupt-count'
        process, child_pid, gate, _ = self.start_held("A", "interrupt", {"FAKE_SIGINT_COUNT": str(counter)})
        os.kill(process.pid, signal.SIGINT)
        os.kill(child_pid, signal.SIGINT)
        deadline = time.monotonic() + 5
        while not counter.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(counter.read_text(), "1")
        self.assertIsNone(process.poll())
        gate.write_text("release")
        self.assertEqual(process.wait(timeout=8), 0)
        for signum in (signal.SIGTERM, signal.SIGHUP):
            process, _, _, _ = self.start_held("A", str(signum))
            os.kill(process.pid, signum)
            # The child dies of the forwarded signal; the launcher dies of it too, like native Codex.
            self.assertEqual(process.wait(timeout=8), -signum)
        # An out-of-memory kill of native Codex is reported the same way, not as a launcher error.
        process, child_pid, _, _ = self.start_held("A", "killed")
        os.kill(child_pid, signal.SIGKILL)  # only this test's fabricated child
        self.assertEqual(process.wait(timeout=8), -signal.SIGKILL)

    def test_suspended_client_still_prevents_repair(self):
        process, child_pid, gate, _ = self.start_held("A", "suspended")
        os.kill(child_pid, signal.SIGSTOP)
        auth = self.codex / "accounts/A/auth.json"
        try:
            raw = auth.read_bytes()
            auth.unlink()
            auth.write_bytes(raw)
            auth.chmod(0o600)
            result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("active", result.stderr)
            self.assertFalse(auth.is_symlink())
        finally:
            os.kill(child_pid, signal.SIGCONT)
            gate.write_text("release")
        self.assertEqual(process.wait(timeout=8), 0)
        # The last client to exit restores the link from identical bytes.
        self.assertTrue(auth.is_symlink())

    def test_both_changed_credentials_are_preserved_for_a_manual_choice(self):
        result = self.run_as(SHELLS[0], ["A", "exec", "prompt"], {"FAKE_MODE": "diverged", "FAKE_EXIT": "17"})
        self.assertEqual(result.returncode, 17)
        self.assertIn("diverged", result.stderr)
        self.assertIn("ln -sfn " + str(self.secret / "codex-A.json"), result.stderr)
        auth = self.codex / "accounts/A/auth.json"
        self.assertFalse(auth.is_symlink())
        local, canonical = auth.read_bytes(), (self.secret / "codex-A.json").read_bytes()
        self.assertNotEqual(local, canonical)
        self.record.unlink()
        result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(auth.read_bytes(), local)
        self.assertEqual((self.secret / "codex-A.json").read_bytes(), canonical)

    def test_shared_relative_paths_and_sqlite_overrides_are_refused(self):
        config = self.codex / "config.toml"
        for content in ('model_instructions_file="relative.md"\n', 'sqlite_home="state"\n',
                        '[agents.worker]\nconfig_file="worker.toml"\n',
                        '[[skills.config]]\npath="skills/example"\nenabled=true\n',
                        '[otel.exporter.otlp-http.tls]\nca-certificate="certs/ca.pem"\n',
                        '[otel.exporter.otlp-grpc.tls]\nclient-certificate="certs/client.pem"\n',
                        '[otel.exporter.otlp-grpc.tls]\nclient-private-key="certs/key.pem"\n'):
            config.write_text(content)
            result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("absolute", result.stderr)
            self.assertEqual(config.read_text(), content)
        config.write_text('model="unchanged-model"\n')
        self.rejected(SHELLS[0], ["A", "-c", 'sqlite_home="/tmp/other"'])
        self.rejected(SHELLS[0], ["A"], {"CODEX_SQLITE_HOME": str(self.base / "other")})

    def test_inherited_managed_account_home_can_switch_accounts(self):
        self.assertEqual(self.run_as(SHELLS[0], ["A", "exec", "prompt"]).returncode, 0)
        result = self.run_as(SHELLS[0], ["B", "exec", "prompt"],
                             {"CODEX_HOME": str(self.codex / "accounts/A")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.record.read_text())["codex_home"], str(self.codex / "accounts/B"))

    def test_unicode_sqlite_override_is_valid_native_toml(self):
        sqlite = self.base / "sqlite-📁"
        sqlite.mkdir()
        (self.codex / "config.toml").write_text('sqlite_home=' + json.dumps(str(sqlite), ensure_ascii=False) + '\n')
        result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
        self.assertEqual(result.returncode, 0, result.stderr)
        override = json.loads(self.record.read_text())["args"][3]
        self.assertEqual(tomllib.loads(override)["sqlite_home"], str(sqlite))

    def test_interrupted_marker_publication_can_retry_without_partial_markers(self):
        def load(name):
            spec = importlib.util.spec_from_file_location('fixture_' + name, self.install / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        guard, storage = load('check'), load('layout')
        tty = SimpleNamespace(write=lambda *args: None, close=lambda: None)
        with patch.dict(os.environ, self.env, clear=True), \
             patch.object(guard, 'terminal', return_value=tty), patch.object(guard, 'confirm'):
            for account in ('A', 'B'):
                layout = storage.Layout(guard, account)
                self.assertTrue(layout.acquire())
                marker = layout.home / 'codex-as.initialized'
                raw = b'codex-as v2\n'
                original = guard.exclusive
                interrupted = [False]

                def halfway(path, data):
                    if data == raw and not interrupted[0]:
                        interrupted[0] = True
                        original(path, b'')
                        raise OSError('fixture process interrupted before marker write')
                    return original(path, data)

                try:
                    with patch.object(guard, 'exclusive', side_effect=halfway):
                        with self.assertRaises(OSError):
                            layout.prepare()
                    self.assertFalse(marker.exists(), 'a partial marker was published')
                    layout.prepare()
                    self.assertTrue(marker.exists())
                    layout.validate()
                finally:
                    layout.close()

    def test_foreign_account_views_and_shared_link_drift_are_refused(self):
        self.assertEqual(self.run_as(SHELLS[0], ["A", "exec", "prompt"]).returncode, 0)
        account = self.codex / "accounts/A"
        (account / "auth.json").unlink()
        (account / "auth.json").symlink_to(self.secret / "codex-B.json")
        self.rejected(SHELLS[0], ["A"])
        (account / "auth.json").unlink()
        (account / "auth.json").symlink_to(self.secret / "codex-A.json")
        (account / "config.toml").unlink()
        (account / "config.toml").write_text('model="forked"\n')
        self.rejected(SHELLS[0], ["A"])

    def test_arguments_io_cwd_exit_and_umask(self):
        cases = [[], ["resume", "--last"], ["fork", "--last"], ["--help"],
                 ["hello"], ["hello", "exec", "login"], ["--", "logout"],
                 ["resume", "--last", "logout"], ["fork", "--last", "login"],
                 ["-m", "logout", "hello"],
                 ["exec", "--json", 'spaces "double" and \'single\'', "", "--color", "never"],
                 ["exec", "login"], ["exec", "logout"], ["exec", "--", "--config", "auth=prompt"],
                 ["exec", "resume", "--last", "检查测试"],
                 ["-m", "login", "exec", "--json", "resume logout login"],
                 ["-c", 'model="harmless"', "exec", "login --config auth=sensitive text"],
                 ["exec", "-cmodel=harmless", "--json", "检查测试"]]
        for shell in SHELLS:
            for args in cases:
                with self.subTest(shell=shell, args=args):
                    result = self.run_as(shell, ["A", *args], {"FAKE_EXIT": "23"}, input="stdin text\n第二行\n",
                                         setup="umask 027; ")
                    self.assertEqual(result.returncode, 23, result.stderr)
                    self.assertEqual(result.stdout, "stdout sentinel\n")
                    self.assertEqual(result.stderr, "stderr sentinel\n")
                    record = json.loads(self.record.read_text())
                    mode = "exec" if "exec" in args else "interactive"
                    expected = ["-c", 'cli_auth_credentials_store="file"', "-c", "sqlite_home=" + json.dumps(str(self.codex))]
                    if mode == "interactive":
                        expected += ["-c", "check_for_update_on_startup=false", "--no-daemon"]
                    if args == ["--help"]:
                        expected = []  # help is the same for every login
                    self.assertEqual(record["args"], expected + args)
                    self.assertEqual(record["cwd"], str(self.cwd))
                    self.assertEqual(record["stdin"], "stdin text\n第二行\n")
                    self.assertEqual(record["umask"], 0o027, "native Codex keeps the caller's umask")

    def test_root_prompt_cannot_hide_a_later_subcommand(self):
        # Codex clap still dispatches a root subcommand after its optional
        # PROMPT. Refusal must happen before switching the shared auth link.
        for shell in SHELLS:
            for args in (["hello", "login"], ["hello", "logout"],
                         ["hello", "--no-alt-screen", "logout"],
                         ["-m", "login", "hello", "logout"], ["hello", "login", "--device-auth"]):
                with self.subTest(shell=shell, args=args):
                    result = self.rejected(shell, ["B", *args])
                    self.assertIn("unsupported subcommand", result.stderr)

    def test_non_finite_credential_json_is_refused_before_switching(self):
        # Python's permissive JSON constants cannot enter Codex's strict
        # serde_json authentication reader, even in an unknown nested field.
        target = self.secret / "codex-B.json"
        for shell in SHELLS:
            for constant in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(shell=shell, constant=constant):
                    target.write_text('{"OPENAI_API_KEY":"fixture-key","unknown":{"value":'
                                      + constant + '}}')
                    self.rejected(shell, ["B"])

    def test_api_key_optional_fields_must_match_native_credential_schema(self):
        # Falsy values are not interchangeable with absent/null Option fields
        # in the native authentication reader. A refusal preserves account A.
        for shell in SHELLS:
            for field, value in (("tokens", {}), ("tokens", []), ("tokens", False),
                                 ("tokens", 0), ("tokens", ""),
                                 ("last_refresh", "bad-date"), ("last_refresh", False),
                                 ("last_refresh", "2026-13-01T00:00:00Z"),
                                 ("last_refresh", "2026-10-01T00:00:00")):
                with self.subTest(shell=shell, field=field, value=value):
                    self.write_credential("B", {"OPENAI_API_KEY": "fixture-key", field: value})
                    self.rejected(shell, ["B"])
            for fields in ({}, {"tokens": None, "last_refresh": None},
                           {"last_refresh": "2026-10-01T00:00:00Z"}):
                with self.subTest(shell=shell, fields=fields):
                    self.write_credential("B", {"OPENAI_API_KEY": "fixture-key", **fields})
                    result = self.run_as(shell, ["B", "exec", "prompt"])
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_inputs_and_conflicts(self):
        for shell in SHELLS:
            for index, args in enumerate(([], [""], ["../A"], ["A/B"], ["A B"], ["A.1"], ["中文"], ["missing"],
                         ["--migrate", "A"], ["--recover", "A"], ["-A"],
                         ["A", "login"], ["A", "logout"], ["A", "--remote", "server"],
                         ["A", "exec", "--oss"], ["A", "--remote-auth-token-env", "TOKEN"],
                         ["A", "--local-provider", "ollama"],
                         ["A", "-c", 'cli_auth_credentials_store="keyring"'],
                         ["A", "exec", 'prompt text', "-cmodel_provider=other"],
                         ["A", "--config", ' "cli_auth_credentials_store" = "auto"'],
                         ["A", "mcp", "add", "x", "-c", "model_provider=other"],
                         ["A", "-c", "features.use_agent_identity=true"],
                         ["A", "--enable", "use_agent_identity"], ["A", "exec", "--enable=use_agent_identity", "p"],
                         ["A", "features", "enable", "use_agent_identity"],
                         # TOML 1.1 that tomllib before 3.15 cannot read must not pass as text.
                         ["A", "-c", "features={use_agent_identity=true,}"], ["A", "queue", "x", "message"],
                         ["A", "-c", 'model_providers={openai={base_url="https://example.invalid"}}'],
                         ["A", "-c", 'chatgpt_base_url="https://example.invalid"'],
                         ["A", "agents"], *([["A", "app"]] if sys.platform in ("darwin", "win32") else []),
                         ["A", "remote-control", "start"],
                         ["A", "app-server", "daemon", "start"], ["A", "app-server", "--listen", "x", "daemon", "start"],
                         ["A", "-c", "check_for_update_on_startup=true"],
                         ["A", "resume", "uuid:12345678-1234-4234-8234-123456789abc"])):
                with self.subTest(shell=shell, case=index):
                    self.rejected(shell, args)
            for variable in ENV_CONFLICTS:
                result = self.rejected(shell, ["A"], {variable: "fabricated-conflict-secret"})
                self.assertIn(variable, result.stderr)
            self.rejected(shell, ["A"], {"CODEX_HOME": str(self.base / "other-home")})
            for version in ("codex-cli 0.160.0", "codex-cli 0.161.0-alpha.1", "codex 0.163.0",
                            "codex-cli 0.163", ""):
                self.rejected(shell, ["A"], {"FAKE_VERSION": version})

    def test_newer_versions_launch_and_native_arguments_are_forwarded(self):
        for version in ("codex-cli 0.161.0", "codex-cli 0.170.2", "codex-cli 1.0.0-alpha.3"):
            with self.subTest(version=version):
                result = self.run_as(SHELLS[0], ["A", "exec", "prompt"], {"FAKE_VERSION": version})
                self.assertEqual(result.returncode, 0, result.stderr)
        # Subcommands and options codex-as does not know reach native Codex unchanged.
        prefix = ["-c", 'cli_auth_credentials_store="file"', "-c", "sqlite_home=" + json.dumps(str(self.codex))]
        interactive = ["-c", "check_for_update_on_startup=false", "--no-daemon"]
        for args, extra in ((["auth", "status"], interactive), (["mcp", "list", "--json"], interactive),
                            (["mcp", "login", "server"], interactive), (["mcp", "logout", "server"], interactive),
                            (["features", "list"], interactive), (["apply", "--future-flag"], interactive),
                            (["--future-switch", "hello"], interactive), (["--ignore-rules", "hello"], interactive),
                            (["-c", 'permission_profile="workspace"', "hello"], interactive),
                            (["exec", "--ignore-user-config", "--future=value", "prompt"], []),
                            # Exact settings, not substrings: these change no login.
                            (["-c", "allow_login_shell=false", "hello"], interactive),
                            (["-c", 'projects."/srv/remote-sensing".trust_level="trusted"'], interactive),
                            (["-c", "mcp_oauth_callback_port=5555", "--enable", "remote_models"], interactive),
                            (["-c", 'model_provider="openai"', "exec", "prompt"], []),
                            (["exec", "--config=OPENAI_API_KEY=not-a-setting", "prompt"], []),
                            # Words after a subcommand are its own: a command line, a branch, a prompt.
                            (["sandbox", "bash", "-c", "git remote -v"], interactive),
                            (["sandbox", "brew", "update"], interactive), (["review", "--base", "update"], interactive),
                            (["login", "status"], interactive),
                            (["remote-control"], interactive), (["app-server", "proxy"], interactive),
                            # clap's attached values and a root --no-daemon of the caller's own.
                            (["-C" + str(self.base), "resume", "--last"], interactive),
                            (["--no-daemon", "hello"], interactive[:2]),
                            (["--no-daemon", "resume", "--no-daemon", "--last"], interactive[:2]),
                            # Native forms that change no login.
                            (["login", "--help"], interactive), (["login", "-c", "x=1", "status"], interactive),
                            (["logout", "--help"], interactive), (["--yolo", "resume", "--last"], interactive),
                            (["-i", "a.png", "b.png", "resume"], interactive),
                            (["-c", 'profile="legacy"'], interactive), (["-c", 'profiles.x.model_provider="o"'], interactive),
                            (["--enable", "secret_auth_storage"], interactive),
                            *([(["app", "is a prompt here"], interactive)] if sys.platform not in ("darwin", "win32") else [])):
            with self.subTest(args=args):
                result = self.run_as(SHELLS[0], ["A", *args])
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.record.read_text())
                self.assertEqual(record["args"], prefix + extra + args)
        for variable in ENV_UNUSED:
            with self.subTest(variable=variable):
                result = self.run_as(SHELLS[0], ["A", "exec", "prompt"], {variable: "fixture-value"})
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_profiles_layer_the_shared_profile_file(self):
        # Native -p NAME layers $CODEX_HOME/NAME.config.toml; the account home shows the shared one.
        profile = self.codex / "work.config.toml"
        profile.write_text('model="profile-model"\n')
        for args in (["-p", "work", "exec", "prompt"], ["--profile=work", "hello"], ["resume", "-pwork", "--last"]):
            with self.subTest(args=args):
                result = self.run_as(SHELLS[0], ["A", *args])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(self.record.read_text())["args"][-len(args):], args)
                self.assertEqual(os.readlink(self.codex / "accounts/A/work.config.toml"), str(profile))
        for content in ('model_provider="other"\n', 'model_instructions_file="relative.md"\n',
                        'sqlite_home="/elsewhere"\n', '[features]\nuse_agent_identity=true\n'):
            with self.subTest(content=content):
                profile.write_text(content)
                self.rejected(SHELLS[0], ["A", "-p", "work", "exec", "prompt"])
        profile.unlink()
        result = self.run_as(SHELLS[0], ["A", "-p", "missing", "exec", "prompt"])
        self.assertEqual(result.returncode, 0, "native Codex reports a missing profile itself")
        self.assertFalse(os.path.lexists(self.codex / "accounts/A/missing.config.toml"))

    def test_help_update_and_completion_run_as_plain_codex(self):
        # They need no account: the root home, no added flags, no environment refusal.
        for args in (["--help"], ["help", "logout"], ["update"], ["completion", "zsh"], ["-V"]):
            for account in ("A", "missing"):
                with self.subTest(args=args, account=account):
                    self.record.unlink(missing_ok=True)
                    result = self.run_as(SHELLS[0], [account, *args], {"OPENAI_API_KEY": "fixture-value"})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    record = json.loads(self.record.read_text())
                    self.assertEqual(record["args"], args)
                    self.assertEqual(record["codex_home"], str(self.codex))
                    self.assertIsNone(record["sqlite_home"])
        self.assertFalse((self.secret / "codex-missing.json").exists(), "no login for a command that needs none")
        self.assertFalse((self.codex / "accounts/missing").exists())
        # A help flag after a subcommand may belong to a command Codex runs: the account is used.
        for args in (["exec", "--help"], ["mcp", "add", "x", "npx", "-h"]):
            with self.subTest(args=args):
                result = self.run_as(SHELLS[0], ["A", *args])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(self.record.read_text())["codex_home"], str(self.codex / "accounts/A"))

    def test_logout_and_identity_change_inside_codex_are_reported(self):
        result = self.run_as(SHELLS[0], ["A", "exec", "prompt"], {"FAKE_MODE": "logout"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("logout", result.stderr)
        self.assertIn("codex-as A", result.stderr)
        self.assertTrue((self.secret / "codex-A.json").exists(), "the secret is never deleted")
        result = self.run_as(SHELLS[0], ["A", "exec", "prompt"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("logout", result.stderr)
        (self.codex / "accounts/A/auth.json").symlink_to(self.secret / "codex-A.json")
        # A write through the link that names another ChatGPT user is kept but reported.
        foreign = json.loads((self.secret / "codex-B.json").read_bytes())["tokens"]["id_token"]
        result = self.run_as(SHELLS[0], ["A", "exec", "prompt"], {"FAKE_MODE": "refresh", "FAKE_ID_TOKEN": foreign})
        self.assertEqual(result.returncode, 0)
        self.assertIn("different ChatGPT user", result.stderr)
        self.assertTrue((self.codex / "accounts/A/auth.json").is_symlink())

    def test_permission_error_identifies_the_actual_directory(self):
        temporary = self.codex / '.tmp'
        temporary.mkdir(mode=0o775)
        temporary.chmod(0o775)

        result = self.run_as(SHELLS[0], ['A', 'exec', 'prompt'])

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(temporary), result.stderr)
        self.assertIn('group/other writable', result.stderr)
        self.assertEqual(temporary.stat().st_mode & 0o777, 0o775)
        self.assertFalse(self.record.exists())
        temporary.rmdir()
        foreign = self.base / 'foreign-temporary'
        foreign.mkdir(mode=0o700)
        temporary.symlink_to(foreign, target_is_directory=True)
        result = self.run_as(SHELLS[0], ['A', 'exec', 'prompt'])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(temporary), result.stderr)
        self.assertEqual(list(foreign.iterdir()), [])
        self.assertFalse(self.record.exists())

    def test_root_auth_is_never_read_or_changed(self):
        cases = {"native file": None, "absent": "", "broken JSON": "{", "unmanaged link": self.base / "unmanaged.json",
                 "older codex-as link": self.secret / "codex-A.json", "dangling link": self.secret / "codex-missing.json"}
        for label, state in cases.items():
            with self.subTest(root=label):
                self.auth.unlink(missing_ok=True)
                if state is None:
                    self.auth.write_bytes(self.root_auth)
                    self.auth.chmod(0o600)
                elif state == "{":
                    self.auth.write_text(state)
                elif state:
                    self.auth.symlink_to(state)
                before = os.readlink(self.auth) if self.auth.is_symlink() else (
                    self.auth.read_bytes() if self.auth.exists() else None)
                for name in ("A", "B"):
                    result = self.run_as(SHELLS[0], [name, "exec", "prompt"], {"FAKE_MODE": "refresh"})
                    self.assertEqual(result.returncode, 0, result.stderr)
                after = os.readlink(self.auth) if self.auth.is_symlink() else (
                    self.auth.read_bytes() if self.auth.exists() else None)
                self.assertEqual(after, before)
        self.assertEqual(json.loads((self.secret / "codex-A.json").read_text())["tokens"]["access_token"], TOKENS[2])

    def test_repair_does_not_confuse_workspace_with_user_identity(self):
        canonical = (self.secret / "codex-A.json").read_bytes()
        # The replacement keeps workspace account_id A but names user B: never imported over A.
        foreign = json.loads((self.secret / "codex-B.json").read_bytes())["tokens"]["id_token"]
        result = self.run_as(SHELLS[0], ["A", "exec", "prompt"], {"FAKE_MODE": "rename", "FAKE_ID_TOKEN": foreign})
        self.assertEqual(result.returncode, 0)
        self.assertIn("diverged", result.stderr)
        auth = self.codex / "accounts/A/auth.json"
        self.assertFalse(auth.is_symlink())
        self.assertEqual(json.loads(auth.read_bytes())["tokens"]["id_token"], foreign)
        self.assertEqual((self.secret / "codex-A.json").read_bytes(), canonical)

    def test_json_permission_and_link_refusals(self):
        for shell in SHELLS:
            a = self.secret / "codex-A.json"
            original = a.read_bytes()
            for bad in ("broken-json", "{}", "[]", '{"tokens":{}}', '{"auth_mode":"chatgptAuthTokens"}',
                        '{"OPENAI_API_KEY":"one","OPENAI_API_KEY":"two"}',
                        '{"tokens":{"access_token":"fabricated-sensitive-access-A"}}'):
                a.write_text(bad)
                self.rejected(shell, ["A"])
            a.write_bytes(original)
            for field, value in (("last_refresh", None), ("last_refresh", "bad-date"),
                                 ("OPENAI_API_KEY", ""), ("OPENAI_API_KEY", []),
                                 ("personal_access_token", "fabricated-conflict-secret"),
                                 ("bedrock_api_key", {}), ("agent_identity", "fabricated-conflict-secret")):
                data = json.loads(original)
                data[field] = value
                a.write_text(json.dumps(data))
                self.rejected(shell, ["A"])
            a.write_bytes(original)
            a.chmod(0o644)
            self.rejected(shell, ["A"])
            self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o644)
            a.chmod(0o600)
            hard = self.base / "hardlink"
            os.link(a, hard)
            self.rejected(shell, ["A"])
            hard.unlink()
            backup = self.base / "original-credential"
            a.rename(backup)
            a.symlink_to(backup)
            self.rejected(shell, ["A"])
            a.unlink()
            backup.rename(a)
            for directory, mode in ((self.secret, 0o755), (self.codex, 0o775)):
                directory.chmod(mode)
                self.rejected(shell, ["A"])
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), mode)
                directory.chmod(0o700)
            self.secret.rename(self.base / "real-secrets")
            self.secret.symlink_to(self.base / "real-secrets", target_is_directory=True)
            self.rejected(shell, ["A"])
            self.secret.unlink()
            (self.base / "real-secrets").rename(self.secret)
            # Wrong ownership is tested against root-owned /etc/passwd without reads.
            if os.getuid() != os.stat("/etc/passwd").st_uid:
                checker = str(self.install / "check.py")
                code = 'import runpy,sys,pathlib; m=runpy.run_path(sys.argv[1]); m["credential"](pathlib.Path("/etc/passwd"))'
                result = subprocess.run([sys.executable, "-I", "-B", "-c", code, checker], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("owned regular file", result.stderr)

    def test_atomic_replacement_preserves_new_credentials_and_exit(self):
        for shell in SHELLS:
            old = (self.secret / "codex-A.json").read_bytes()
            result = self.run_as(shell, ["A", "exec"], {"FAKE_MODE": "rename", "FAKE_EXIT": "17"})
            self.assertEqual(result.returncode, 17)
            self.assertEqual(result.stderr, "stderr sentinel\ncodex-as: Codex replaced the account auth.json link "
                             "with a regular file; credentials were imported into secrets and the link restored.\n")
            auth = self.codex / "accounts/A/auth.json"
            self.assertTrue(auth.is_symlink())
            newest = auth.read_bytes()
            self.assertTrue(json.loads(newest)["tokens"]["access_token"] == TOKENS[2], "newest credentials lost (values hidden)")
            self.assertEqual(self.run_as(shell, ["B"]).returncode, 0)
            self.assertTrue(auth.read_bytes() == newest, "newest credentials overwritten (values hidden)")
            backups = list(self.secret.glob(".codex-as-backup-*"))
            self.assertTrue(any(p.read_bytes() == old for p in backups))

    def test_local_policy_and_provider_refusals(self):
        cfg = self.codex / "config.toml"
        for shell in SHELLS:
            for content in ('model_provider="other"\n', 'forced_login_method="api"\n',
                            'forced_chatgpt_workspace_id="restricted"\n',
                            '[model_providers.openai]\nenv_key="OTHER_KEY"\n',
                            'chatgpt_base_url="https://example.invalid"\n',
                            'openai_base_url="https://example.invalid"\n'):
                cfg.write_text(content)
                self.rejected(shell, ["A"])
                self.assertEqual(cfg.read_text(), content)
            cfg.write_text('model="unchanged-model"\n')
            project_cfg = self.cwd / ".codex/config.toml"
            project_cfg.parent.mkdir(exist_ok=True)
            # Native drops provider and endpoint keys from project config; other keys still apply.
            project_cfg.write_text('model_provider="project-provider"\nchatgpt_base_url="https://example.invalid"\n')
            self.assertEqual(self.run_as(shell, ["A", "exec", "prompt"]).returncode, 0)
            for content in ('forced_login_method="api"\n', '[features]\nuse_agent_identity=true\n'):
                project_cfg.write_text(content)
                self.rejected(shell, ["A"])
            project_cfg.unlink()

    def test_executable_parent_state_and_command_bypass(self):
        (self.home / ".aliases").write_text("")
        for shell in SHELLS:
            setup = 'umask 022; codex() { return 99; }; alias codex="false"; '
            if Path(shell).name == "bash":
                setup += 'export -f codex; '
            result = self.run_as(shell, ["A", "exec", "login"], setup=setup)
            self.assertEqual(result.returncode, 0, result.stderr)
            flags = ["-f"] if Path(shell).name == "zsh" else ["--noprofile", "--norc"]
            command = [shell, *flags, "-c", 'umask 022; before=$(umask); command codex-as A --help >/dev/null 2>/dev/null; '
                       '[ "$(umask)" = "$before" ] && [ "$PWD" = "$1" ] && [ "${CODEX_HOME-unset}" = unset ]',
                       "state", str(self.cwd)]
            result = subprocess.run(command, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            alias_file = ROOT / ("common/zsh/.zsh_aliases" if Path(shell).name == "zsh" else "common/bash/.bash_aliases")
            command = [shell, *flags, "-c", '. "$1"; . "$1"; codex-as --help', "alias", str(alias_file)]
            result = subprocess.run(command, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Concurrent accounts", result.stdout)

    def test_tty_fds_are_preserved(self):
        for shell in SHELLS:
            master, slave = pty.openpty()
            flags = ["-f"] if Path(shell).name == "zsh" else ["--noprofile", "--norc"]
            command = [shell, *flags, "-c", 'command codex-as A', "tty"]
            process = subprocess.Popen(command, cwd=self.cwd, env=self.env, stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            try:
                os.write(master, b"terminal input\n\x04")
                output = bytearray()
                while process.poll() is None:
                    if select.select([master], [], [], 1)[0]:
                        try:
                            output.extend(os.read(master, 4096))
                        except OSError:
                            break
                self.assertEqual(process.wait(timeout=10), 0)
                record = json.loads(self.record.read_text())
                self.assertEqual(record["tty"], [True, True, True])
                self.assertEqual(record["stdin"], "terminal input\n")
            finally:
                os.close(master)
                if process.poll() is None:
                    process.kill()  # Only this test's fake process, never real Codex.
                    process.wait()


if __name__ == "__main__":
    for name in ("bash", "zsh"):
        print(f"shell={name}: {'installed, testing' if shutil.which(name) else 'SKIP (not installed)'}", flush=True)
    unittest.main(verbosity=2)
