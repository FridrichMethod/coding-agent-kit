"""Offline native Codex 0.162.1 account-home sharing integration tests.

Run with CODEX_NATIVE_BINARY=/absolute/path/to/codex python3 this_file.py.
Only temporary fixture homes are modified. No login or turn/start is issued.
The local provider and HTTP proxy reject and count all startup HTTP requests.
Protocol thread/list tests cover discovery, not literal terminal picker/--last.
"""

from __future__ import annotations

import json
import importlib.util
import hashlib
import os
from pathlib import Path
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace


class RpcError(RuntimeError):
    pass


class AppServer:
    def __init__(self, binary: Path, env: dict[str, str], cwd: Path, lease_fd=None):
        self.stderr = tempfile.TemporaryFile(mode="w+b")
        self.process = subprocess.Popen(
            [str(binary), "app-server"], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, bufsize=1, pass_fds=() if lease_fd is None else (lease_fd,),
        )
        self.messages: queue.Queue[dict | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.next_id = 0
        self.notifications = []
        try:
            self.request("initialize", {
                "clientInfo": {"name": "codex-sharing-native-test", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            self.notify("initialized", {})
        except BaseException:
            self.close()
            raise

    def _read(self):
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except json.JSONDecodeError:
                self.messages.put({"invalid_line": line})
        self.messages.put(None)

    def notify(self, method: str, params: dict):
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"method": method, "params": params}) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 20):
        self.next_id += 1
        rpc_id = self.next_id
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps({"id": rpc_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"RPC {method} timed out: {self.diagnostics()}")
            try:
                response = self.messages.get(timeout=remaining)
            except queue.Empty as error:
                raise RuntimeError(f"RPC {method} timed out: {self.diagnostics()}") from error
            if response is None:
                raise RuntimeError(f"app-server exited during {method}: {self.diagnostics()}")
            if response.get("id") == rpc_id:
                if "error" in response:
                    raise RpcError(f"{method}: {json.dumps(response['error'])}")
                return response["result"]
            if "id" in response and "method" in response:
                # No protocol requests that need authorization should occur in this fixture.
                raise RuntimeError(f"unexpected server request: {response}")
            self.notifications.append(response)

    def diagnostics(self):
        self.stderr.flush()
        self.stderr.seek(0)
        return self.stderr.read().decode(errors="replace")[-5000:]

    def close(self):
        if self.process.poll() is None:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()
        self.reader.join(timeout=1)
        self.stderr.close()


class NativeSharingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        candidate = os.environ.get("CODEX_NATIVE_BINARY") or shutil.which("codex")
        if not candidate:
            raise unittest.SkipTest("Codex binary not found")
        cls.binary = Path(candidate).resolve()
        with tempfile.TemporaryDirectory(prefix="codex-native-version-") as version_home:
            version = subprocess.check_output(
                [str(cls.binary), "--version"], text=True, stderr=subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "HOME": version_home,
                     "CODEX_HOME": version_home},
            ).strip()
        if version != "codex-cli 0.162.1":
            raise unittest.SkipTest(f"requires codex-cli 0.162.1, found {version}")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-native-sharing-")
        self.fixture = Path(self.temp.name)
        self.home = self.fixture / "home"
        self.home.mkdir(mode=0o700)
        self.root = self.home / ".codex"
        self.root.mkdir(mode=0o700)
        self.workspace = self.fixture / "workspace"
        self.workspace.mkdir()
        self.http_requests = []
        requests = self.http_requests

        class RejectHTTP(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.command, self.path))
                self.send_error(503)

            do_POST = do_GET
            do_CONNECT = do_GET

            def log_message(self, *args):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), RejectHTTP)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        endpoint = f"http://127.0.0.1:{self.http.server_port}"
        self.config = self.root / "config.toml"
        self.config.write_text(
            'model = "gpt-5.4"\nmodel_provider = "offline_fixture"\n'
            'cli_auth_credentials_store = "file"\n'
            'approval_policy = "never"\nweb_search = "disabled"\n'
            '[model_providers.offline_fixture]\nname = "offline_fixture"\n'
            f'base_url = "{endpoint}/v1"\n'
            'env_key = "CODEX_NATIVE_TEST_KEY"\nwire_api = "responses"\n'
            'request_max_retries = 0\nstream_max_retries = 0\n'
            '[features]\nplugins = false\nrecommended_plugins = false\n'
            'apps = false\nenable_mcp_apps = false\nmemories = false\n'
            'shell_snapshot = false\nshell_snapshot_v2 = false\n'
            'network_proxy = false\ncode_mode_prewarm = false\n'
            '[memories]\ngenerate_memories = false\nuse_memories = false\n'
            '[analytics]\nenabled = false\n',
            encoding="utf-8",
        )
        for directory in (
            "sessions", "archived_sessions", "thread-writer-locks", "rollout-migrations",
        ):
            (self.root / directory).mkdir(mode=0o700)
        self.accounts = {}
        for name in ("a", "b"):
            account = self.root / "accounts" / name
            account.mkdir(parents=True, mode=0o700)
            (account / "config.toml").symlink_to("../../config.toml")
            for directory in (
                "sessions", "archived_sessions", "thread-writer-locks", "rollout-migrations",
            ):
                (account / directory).symlink_to(f"../../{directory}", target_is_directory=True)
            self.accounts[name] = account
        self.base_env = {
            "PATH": "/usr/bin:/bin", "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "TMPDIR": str(self.fixture),
            "CODEX_SQLITE_HOME": str(self.root),
            "CODEX_NATIVE_TEST_KEY": "fictional-offline-native-test-key",
            "HTTP_PROXY": endpoint, "HTTPS_PROXY": endpoint, "ALL_PROXY": endpoint,
            "http_proxy": endpoint, "https_proxy": endpoint, "all_proxy": endpoint,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            "DOTFILES_AUTO_UPDATE": "0", "AWESOME_SKILLS_AUTO_UPDATE": "0",
        }
        self.servers = []

    def tearDown(self):
        for server in reversed(self.servers):
            server.close()
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(timeout=1)
        try:
            self.assertEqual(self.http_requests, [], "fixture attempted startup HTTP")
        finally:
            self.temp.cleanup()

    def server(self, account):
        env = {**self.base_env, "CODEX_HOME": str(self.accounts[account])}
        result = AppServer(self.binary, env, self.workspace)
        self.servers.append(result)
        return result

    def start_thread(self, server, history_mode=None):
        params = {"cwd": str(self.workspace), "ephemeral": False,
                  "approvalPolicy": "never", "sandbox": "read-only"}
        if history_mode:
            params["historyMode"] = history_mode
        return server.request("thread/start", params)["thread"]

    def test_confirmed_daemon_takeover_preserves_uuid_history_and_other_threads(self):
        source = Path(__file__).resolve().parents[1] / 'common/codex/.local/lib/codex-as'

        def load(name):
            spec = importlib.util.spec_from_file_location('native_handoff_' + name, source / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module

        handoff, guard = load('handoff'), load('check')
        original = self.server('a')
        saved = []
        for mode in ('legacy', 'paginated'):
            thread = self.start_thread(original, mode)
            marker = 'fictional takeover history ' + mode
            original.request('thread/inject_items', {'threadId': thread['id'], 'items': [{
                'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': marker}],
            }]})
            self.wait_for_persisted_rollout(thread)
            saved.append((mode, thread, marker))
        children = []
        parent = saved[0][1]['id']
        for marker in ('fictional visible child', 'fictional archived grandchild'):
            child = self.start_thread(original, 'legacy')
            original.request('thread/inject_items', {'threadId': child['id'], 'items': [{
                'type': 'message', 'role': 'user',
                'content': [{'type': 'input_text', 'text': marker}],
            }]})
            self.wait_for_persisted_rollout(child)
            children.append((child, marker, parent))
            parent = child['id']
        original.close()
        for _, thread, marker in saved:
            self.seed_completed_turn(thread, marker)
        # Saved spawn fixtures require no model calls. Populate both the real
        # persisted source and the real native relation index while stopped.
        for depth, (child, marker, parent) in enumerate(children, 1):
            self.seed_completed_turn(child, marker)
            path = Path(child['path'])
            records = [json.loads(line) for line in path.read_text().splitlines()]
            child_source = {'subagent': {'thread_spawn': {'parent_thread_id': parent, 'depth': depth}}}
            records[0]['payload']['source'] = child_source
            path.write_text(''.join(json.dumps(record) + '\n' for record in records))
            with closing(sqlite3.connect(self.root / 'state_5.sqlite')) as connection:
                with connection:
                    connection.execute('UPDATE threads SET source = ? WHERE id = ?',
                                       (json.dumps(child_source), child['id']))
                    connection.execute('INSERT INTO thread_spawn_edges VALUES (?, ?, ?)',
                                       (parent, child['id'], 'closed'))
        setup = self.server('a')
        setup.request('thread/archive', {'threadId': children[1][0]['id']})
        setup.close()
        env = {**self.base_env, 'CODEX_HOME': str(self.root)}
        binary = os.environ.get('CODEX_NATIVE_DAEMON_BINARY', str(self.binary))
        errors = tempfile.TemporaryFile()
        self.addCleanup(errors.close)
        process = subprocess.Popen([binary, 'app-server', '--listen', 'unix://'],
                                   env=env, cwd=self.workspace, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=errors)

        def stop_fixture():
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            logical = (self.root / 'app-server-control').resolve() / 'app-server-control.sock'
            protected = (Path('/tmp').resolve() / f'codex-daemon-{os.geteuid()}' /
                         hashlib.sha256(os.fsencode(logical)).hexdigest())
            protected.unlink(missing_ok=True)

        self.servers.append(SimpleNamespace(close=stop_fixture))
        owner = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and process.poll() is None:
            try:
                owner = handoff.Client(self.root, guard)
                break
            except OSError:
                time.sleep(.02)
        self.assertIsNotNone(owner, 'fixture daemon failed to start')
        self.addCleanup(owner.close)
        self.assertEqual(handoff.descendants(owner, saved[0][1]['id'], False), {children[0][0]['id']})
        self.assertEqual(handoff.descendants(owner, saved[0][1]['id'], True), {children[1][0]['id']})
        owner.request('thread/resume', {'threadId': children[0][0]['id']})
        # The owner connection stays attached: takeover must stop only the selected subtree.
        other = self.start_thread(owner)
        second = self.server('b')
        locks = self.root / 'accounts/.locks'
        locks.mkdir(parents=True, mode=0o700)
        tty = SimpleNamespace(write=lambda *args: None, close=lambda: None)
        confirmed = []
        management = SimpleNamespace(directory=guard.directory, private_metadata=guard.private_metadata,
                                     exclusive=guard.exclusive, terminal=lambda: tty,
                                     confirm=lambda *args: confirmed.append(True))
        for mode, thread, marker in saved:
            with self.subTest(history=mode):
                before = owner.request('thread/resume', {'threadId': thread['id']})
                self.assertIn(marker, json.dumps(before['thread']['turns']))
                self.assertTrue(handoff.locked(self.root, thread['id']))
                handoff.prepare(management, self.root, 'b', thread['id'])
                self.assertFalse(handoff.locked(self.root, thread['id']))
                resumed = second.request('thread/resume', {'threadId': thread['id']})
                self.assertEqual(resumed['thread']['id'], thread['id'])
                read = second.request('thread/read', {'threadId': thread['id'], 'includeTurns': True})
                self.assertIn(marker, json.dumps(read['thread']['turns']))
                self.assertTrue(handoff.locked(self.root, other['id']))
                self.assertIn(other['id'], owner.request('thread/loaded/list', {})['data'])
                if mode == 'legacy':
                    self.assertFalse(handoff.locked(self.root, children[0][0]['id']))
                    self.assertEqual(handoff.descendants(owner, thread['id'], False), {children[0][0]['id']})
                    self.assertEqual(handoff.descendants(owner, thread['id'], True), {children[1][0]['id']})
                    child_read = owner.request('thread/read', {'threadId': children[0][0]['id'], 'includeTurns': True})
                    self.assertIn(children[0][1], json.dumps(child_read['thread']['turns']))
        self.assertEqual(len(confirmed), 2)

    def seed_completed_turn(self, thread, marker):
        """Append a synthetic completed turn only after the fixture server stops.

        This supplies realistic saved UI history without issuing turn/start or
        a model request. The native server created SessionMeta and will project
        these records into its own paginated history database when reading.
        """
        turn_id = str(uuid.uuid4())
        records = [
            ("event_msg", {"type": "task_started", "turn_id": turn_id,
                           "model_context_window": 128000}),
            ("event_msg", {"type": "user_message", "message": marker,
                           "local_images": [], "text_elements": []}),
            ("response_item", {"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": marker}]}),
            ("response_item", {"type": "message", "role": "assistant",
                               "content": [{"type": "output_text", "text": "fictional answer"}]}),
            ("event_msg", {"type": "agent_message", "message": "fictional answer"}),
            ("event_msg", {"type": "item_completed", "thread_id": thread["id"],
                           "turn_id": turn_id, "item": {
                               "type": "UserMessage", "id": f"{turn_id}-user",
                               "content": [{"type": "text", "text": marker}],
                           }}),
            ("event_msg", {"type": "item_completed", "thread_id": thread["id"],
                           "turn_id": turn_id, "item": {
                               "type": "AgentMessage", "id": f"{turn_id}-assistant",
                               "content": [{"type": "Text", "text": "fictional answer"}],
                           }}),
            ("event_msg", {"type": "task_complete", "turn_id": turn_id,
                           "last_agent_message": "fictional answer"}),
        ]
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        path = Path(thread["path"])
        saved = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        next_ordinal = 1 + max((record.get("ordinal", -1) for record in saved), default=-1)
        with path.open("a", encoding="utf-8") as stream:
            for kind, payload in records:
                record = {"timestamp": timestamp, "type": kind, "payload": payload}
                if thread["historyMode"] == "paginated":
                    record["ordinal"] = next_ordinal
                    next_ordinal += 1
                stream.write(json.dumps(record) + "\n")
        # Native discovery intentionally omits threads without a preview.
        # Populate fixture metadata while no server has this fixture open.
        with closing(sqlite3.connect(self.root / "state_5.sqlite")) as connection:
            with connection:
                connection.execute("UPDATE threads SET preview = ?, has_user_event = 1 WHERE id = ?",
                                   (marker, thread["id"]))

    def wait_for_persisted_rollout(self, thread):
        deadline = time.monotonic() + 5
        path = Path(thread["path"])
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size > 0:
                return
            time.sleep(0.01)
        self.fail("native thread history did not persist within five seconds")

    def test_paginated_uuid_resume_and_shared_discovery(self):
        first = self.server("a")
        thread = self.start_thread(first)
        self.assertEqual(thread["historyMode"], "paginated")
        thread_id = thread["id"]
        first.request("thread/name/set", {"threadId": thread_id, "name": "shared-native-thread"})
        first.request("thread/inject_items", {"threadId": thread_id, "items": [{
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "fictional shared context marker"}],
        }]})
        first.close()
        marker = "fictional completed paginated context"
        self.seed_completed_turn(thread, marker)
        second = self.server("b")
        result = second.request("thread/resume", {"threadId": thread_id})
        self.assertEqual(result["thread"]["id"], thread_id)
        self.assertEqual(result["thread"]["name"], "shared-native-thread")
        # The actual default history is SQLite-backed and must survive the account change.
        self.assertTrue((self.root / "thread_history_1.sqlite").exists())
        read = second.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        self.assertEqual(read["thread"]["id"], thread_id)
        self.assertIn(marker, json.dumps(read["thread"]["turns"]))
        self.assertIn("fictional answer", json.dumps(read["thread"]["turns"]))
        listing = second.request("thread/list", {
            "sortKey": "updated_at", "limit": 100, "useStateDbOnly": True,
            "cwd": str(self.workspace), "modelProviders": ["offline_fixture"],
        })
        self.assertIn(thread_id, [item["id"] for item in listing["data"]])

    def test_legacy_uuid_resume(self):
        first = self.server("a")
        thread = self.start_thread(first, "legacy")
        self.assertEqual(thread["historyMode"], "legacy")
        thread_id = thread["id"]
        first.request("thread/inject_items", {"threadId": thread_id, "items": [{
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "fictional legacy context marker"}],
        }]})
        first.close()
        marker = "fictional completed legacy context"
        self.seed_completed_turn(thread, marker)
        second = self.server("b")
        resumed = second.request("thread/resume", {"threadId": thread_id})
        self.assertEqual(resumed["thread"]["id"], thread_id)
        read = second.request("thread/read", {"threadId": thread_id, "includeTurns": True})
        self.assertIn(marker, json.dumps(read["thread"]["turns"]))
        self.assertIn("fictional answer", json.dumps(read["thread"]["turns"]))

    def test_same_thread_writer_lock_and_different_threads(self):
        first = self.server("a")
        thread = self.start_thread(first)
        first.request("thread/inject_items", {"threadId": thread["id"], "items": [{
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "fictional lock fixture"}],
        }]})
        self.wait_for_persisted_rollout(thread)
        second = self.server("b")
        other = self.start_thread(second)
        self.assertNotEqual(thread["id"], other["id"])
        with self.assertRaisesRegex(RpcError, "active writer|already.*writer|already.*use"):
            second.request("thread/resume", {"threadId": thread["id"]})
        first.close()
        resumed = second.request("thread/resume", {"threadId": thread["id"]})
        self.assertEqual(resumed["thread"]["id"], thread["id"])

    def test_config_write_follows_symlink(self):
        server = self.server("a")
        server.request("config/value/write", {
            "keyPath": "model_reasoning_effort", "value": "low", "mergeStrategy": "replace",
        })
        self.assertTrue((self.accounts["a"] / "config.toml").is_symlink())
        self.assertTrue((self.accounts["b"] / "config.toml").is_symlink())
        self.assertIn('model_reasoning_effort = "low"', self.config.read_text(encoding="utf-8"))

    def test_same_account_concurrent_different_threads(self):
        first = self.server("a")
        second = self.server("a")
        one = self.start_thread(first)
        two = self.start_thread(second)
        self.assertNotEqual(one["id"], two["id"])
        for server, thread in ((first, one), (second, two)):
            server.request("thread/inject_items", {"threadId": thread["id"], "items": [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "fictional concurrent context"}],
            }]})
            self.wait_for_persisted_rollout(thread)
        self.assertTrue((self.root / "thread-writer-locks" / f"{one['id']}.lock").exists())
        self.assertTrue((self.root / "thread-writer-locks" / f"{two['id']}.lock").exists())

    def test_shared_discovery_order_and_current_directory(self):
        threads = []
        for account in ("a", "b"):
            server = self.server(account)
            thread = self.start_thread(server)
            server.request("thread/inject_items", {"threadId": thread["id"], "items": [{
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "fictional discovery context"}],
            }]})
            server.close()
            self.seed_completed_turn(thread, f"fictional completed context {account}")
            threads.append(thread)
        first = self.server("a")
        second = self.server("b")
        params = {"sortKey": "updated_at", "limit": 100, "useStateDbOnly": True,
                  "cwd": str(self.workspace), "modelProviders": ["offline_fixture"]}
        first_listing = first.request("thread/list", params)["data"]
        second_listing = second.request("thread/list", params)["data"]
        first_ids = [thread["id"] for thread in first_listing]
        self.assertEqual(first_ids, [thread["id"] for thread in second_listing])
        self.assertEqual(set(first_ids), {thread["id"] for thread in threads})
        self.assertEqual(first_ids[0], threads[-1]["id"])
        other_cwd = self.fixture / "other-workspace"
        other_cwd.mkdir()
        empty = second.request("thread/list", {**params, "cwd": str(other_cwd)})
        self.assertEqual(empty["data"], [])

    @unittest.skipUnless(os.name == "posix", "account leases require POSIX flock")
    def test_native_process_keeps_inherited_account_lease(self):
        import fcntl

        path = self.fixture / "account.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            env = {**self.base_env, "CODEX_HOME": str(self.accounts["a"])}
            server = AppServer(self.binary, env, self.workspace, fd)
            self.servers.append(server)
        finally:
            os.close(fd)
        with path.open("rb") as stream:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            server.close()
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


if __name__ == "__main__":
    unittest.main(verbosity=2)
