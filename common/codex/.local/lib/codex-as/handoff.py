"""Native archive/unarchive takeover from the root daemon; never kills a daemon or removes locks."""

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import subprocess
import time
import uuid


class RpcError(ValueError):
    pass


class Client:
    """Bounded RFC 6455 JSON-RPC client for an existing owned Unix daemon."""

    def __init__(self, root, guard, timeout=30):
        self.timeout = timeout
        logical = root / "app-server-control/app-server-control.sock"
        guard.directory(logical.parent, private=True)
        directory = Path("/tmp").resolve() / f"codex-daemon-{os.geteuid()}"
        guard.directory(directory, private=True)
        path = directory / hashlib.sha256(os.fsencode(logical.parent.resolve() / logical.name)).hexdigest()
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unsafe daemon socket; no takeover performed.")
        self.sock = socket.socket(socket.AF_UNIX)
        self.buffer = bytearray()
        self.next_id = 0
        self.deadline = time.monotonic() + 5
        try:
            self.sock.settimeout(5)
            self.sock.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                               "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                               f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
            while b"\r\n\r\n" not in self.buffer:
                if len(self.buffer) > 8192:
                    raise ValueError("invalid daemon WebSocket upgrade.")
                self.buffer.extend(self._recv())
            head, _, rest = self.buffer.partition(b"\r\n\r\n")
            self.buffer = bytearray(rest)
            lines = head.decode("ascii").split("\r\n")
            headers = {name.lower(): value.strip() for name, value in (line.split(":", 1) for line in lines[1:])}
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if (lines[0] != "HTTP/1.1 101 Switching Protocols"
                    or headers.get("sec-websocket-accept") != expected
                    or headers.get("upgrade", "").lower() != "websocket"):
                raise ValueError("invalid daemon WebSocket identity.")
            result = self.request("initialize", {
                # Native management identity avoids changing the daemon's global originator.
                "clientInfo": {"name": "codex_app_server_daemon", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            # Native originators include "Codex Desktop"; the running backend
            # version is after the FIRST slash, before platform/client suffixes.
            version = re.match(r"[^/\r\n]+/(\S+)", result.get("userAgent", ""))
            if not version or Path(result.get("codexHome", "")).resolve() != root.resolve():
                raise ValueError("daemon home/version identity is not recognized; no takeover performed.")
            guard.codex_version("codex-cli " + version[1])
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise

    def _recv(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("daemon request timed out")
        self.sock.settimeout(remaining)
        data = self.sock.recv(65536)
        if not data:
            raise OSError("daemon disconnected")
        return data

    def _take(self, count):
        while len(self.buffer) < count:
            self.buffer.extend(self._recv())
        value = bytes(self.buffer[:count])
        del self.buffer[:count]
        return value

    def _frame(self, opcode, payload):
        length = len(payload)
        if length < 126:
            header = bytes((128 | opcode, 128 | length))
        elif length < 65536:
            header = bytes((128 | opcode, 254)) + struct.pack("!H", length)
        else:
            header = bytes((128 | opcode, 255)) + struct.pack("!Q", length)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))

    def send(self, message):
        self._frame(1, json.dumps(message, separators=(",", ":")).encode())

    def receive(self):
        message = bytearray()
        started = False
        while True:
            first, second = self._take(2)
            opcode, length = first & 15, second & 127
            if first & 112 or second & 128:
                raise ValueError("invalid daemon WebSocket frame.")
            if length == 126:
                length = struct.unpack("!H", self._take(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._take(8))[0]
            if length + len(message) > 8 * 1024 * 1024:
                raise ValueError("daemon response exceeded the takeover limit.")
            payload = self._take(length)
            if opcode == 8:
                raise OSError("daemon closed the connection")
            if opcode in (9, 10):
                if not first & 128 or length > 125:
                    raise ValueError("invalid daemon control frame.")
                if opcode == 9:
                    self._frame(10, payload)
                continue
            if (opcode == 1 and started) or (opcode == 0 and not started) or opcode not in (0, 1):
                raise ValueError("invalid daemon message framing.")
            started = True
            message.extend(payload)
            if first & 128:
                return json.loads(message)

    def request(self, method, params):
        self.deadline = time.monotonic() + self.timeout
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            result = self.receive()
            if not isinstance(result, dict):
                raise ValueError("invalid daemon RPC response.")
            if result.get("id") != request_id or "method" in result:
                continue
            if "error" in result:
                raise RpcError(f"daemon {method} refused ({result['error'].get('code', 'unknown')}).")
            return result["result"]

    def close(self):
        self.sock.close()


def locked(root, thread):
    path = root / "thread-writer-locks" / (thread + ".lock")
    try:
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
            raise ValueError("session writer lock directory must be owned and private (0700).")
        # Native OpenOptions uses the caller's umask: 0664 is valid INSIDE
        # this verified 0700 directory. Open relative to its verified fd.
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return False
    finally:
        os.close(directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            raise ValueError("unsafe session writer lock; no takeover performed.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


def descendants(client, thread, archived):
    result, seen, cursor = set(), set(), None
    for _ in range(100):
        response = client.request("thread/list", {"ancestorThreadId": thread, "archived": archived,
                                                  "limit": 100, "cursor": cursor})
        result.update(str(uuid.UUID(item["id"])) for item in response["data"])
        cursor = response.get("nextCursor")
        if not cursor:
            return result
        if cursor in seen:
            break
        seen.add(cursor)
    raise ValueError("daemon descendant listing is incomplete; takeover stopped.")


def root_archived(client, root, thread):
    value = client.request("thread/read", {"threadId": thread, "includeTurns": False})["thread"]
    if value["id"] != thread or not value.get("path"):
        raise ValueError("cannot verify saved session visibility; recovery is required.")
    path = Path(value["path"]).resolve()
    if path.is_relative_to((root / "archived_sessions").resolve()):
        return True
    if path.is_relative_to((root / "sessions").resolve()):
        return False
    raise ValueError("session archive path is outside shared storage; recovery is required.")


def restore(client, root, thread, original_archived):
    # Unlike thread/list/read, native unarchive takes the lifecycle permit
    # shared with archive. Restore root first, waiting behind an older archive.
    try:
        client.request("thread/unarchive", {"threadId": thread})
    except RpcError:
        # A partial archive can leave root unarchived. Verify that state;
        # never interpret a timeout/disconnect as an already-unarchived reply.
        if root_archived(client, root, thread):
            raise
    current = descendants(client, thread, True)
    for item in sorted(current - original_archived, key=lambda item: (item != thread, item)):
        client.request("thread/unarchive", {"threadId": item})
    if root_archived(client, root, thread) or descendants(client, thread, True) != original_archived:
        raise ValueError("saved archive visibility was not fully restored.")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def label(value):
    # One line per session: fold whitespace first, then drop control characters.
    text = " ".join(str(value.get("name") or value.get("preview") or "").split())
    return f"  {uuid.UUID(value['id'])}  {''.join(char for char in text if char.isprintable())[:60]}".rstrip()


def describe(client, thread):
    try:
        return label(client.request("thread/read", {"threadId": thread, "includeTurns": False})["thread"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return f"  {thread}"


def busy(client, thread, loaded):
    """Whether the session or a loaded spawned agent may be running; an unknown status counts.

    Only a snapshot: a turn that starts before thread/archive is still ended."""
    for item in [thread, *sorted(descendants(client, thread, False) & set(loaded))]:
        value = client.request("thread/read", {"threadId": item, "includeTurns": False})["thread"]
        status = value.get("status") if isinstance(value, dict) else None
        if not isinstance(status, dict) or status.get("type") not in ("idle", "systemError"):
            return True
    return False


def take_over(guard, root, client, account, thread, ask):
    """Release THREAD from the root daemon: "free", "taken", "running" (left alone) or "elsewhere"."""
    journal = root / "accounts/.locks" / (thread + ".handoff.json")
    # Serialize wrapper takeovers; the native writer lock is never removed.
    lease = journal.with_suffix(".lock")
    fd = os.open(lease, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("unsafe takeover lease.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another codex-as takeover is in progress for this UUID.") from None
        if os.path.lexists(journal):
            # Finish an interrupted takeover; restoring saved visibility needs no confirmation.
            record = json.loads(guard.private_metadata(journal))
            if (not isinstance(record, dict) or record.get("version") != 1 or record.get("thread") != thread
                    or not isinstance(record.get("archived"), list)):
                raise ValueError("unrecognized takeover recovery record; preserve it.")
            try:
                restore(client, root, thread, {str(uuid.UUID(item)) for item in record["archived"]})
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ValueError(f"takeover recovery failed; preserve {journal} and retry the same command.") from error
            journal.unlink()
        if not locked(root, thread):
            return "free"
        loaded = client.request("thread/loaded/list", {})["data"]
        if thread not in loaded:
            return "elsewhere"
        if root_archived(client, root, thread):
            raise ValueError("session is already archived; unarchive it in the original client first.")
        if busy(client, thread, loaded):
            if not ask:
                return "running"
            tty = guard.terminal()
            try:
                tty.write("This session may be running in the root daemon. Takeover ends it AND its spawned agents,\n"
                          "including active work. Saved history is kept; archive visibility is restored.\n", "33")
                guard.confirm(tty, f"Take over {thread} for account {account}?")
            finally:
                tty.close()
        # Read the tree after any confirmation: reading it is not a transaction.
        original_archived = descendants(client, thread, True)
        guard.exclusive(journal, (json.dumps({"version": 1, "thread": thread,
                                            "archived": sorted(original_archived)}) + "\n").encode())
        sync_directory(journal.parent)
        completed = False
        try:
            client.request("thread/archive", {"threadId": thread})
            completed = True
        except RpcError:
            completed = True
            raise
        except BaseException as error:
            raise ValueError(f"takeover outcome is unknown; preserve {journal} and retry the same command to recover.") from error
        finally:
            if completed:
                try:
                    restore(client, root, thread, original_archived)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    raise ValueError(f"takeover restore failed; preserve {journal} and retry the same command.") from error
                journal.unlink()
        deadline = time.monotonic() + 5
        while locked(root, thread):
            if time.monotonic() > deadline:
                raise ValueError("takeover ended the session but its writer is still shutting down; retry after it exits.")
            time.sleep(.05)
        return "taken"
    finally:
        os.close(fd)


def prepare(guard, root, account, thread):
    """Explicit interactive resume UUID: take it over from the root daemon, asking only if it may be running."""
    if thread is None:
        return
    journal = root / "accounts/.locks" / (thread + ".handoff.json")
    pending = os.path.lexists(journal)
    if not pending and not locked(root, thread):
        return
    try:
        client = Client(root, guard)
    except (OSError, ValueError) as error:
        if pending:
            raise ValueError(f"the root daemon is unavailable; preserve recovery record {journal} and retry.") from None
        # Native Codex shows its own conflict screen, as it would without codex-as.
        guard.warn(f"session is in use and the root daemon is unavailable ({error}); no takeover performed.")
        return
    try:
        outcome = take_over(guard, root, client, account, thread, ask=True)
    finally:
        client.close()
    if outcome == "elsewhere":
        guard.warn("session is in use by another client, not the root daemon; no takeover performed.")
    elif outcome == "taken":
        guard.warn(f"took over {thread} from the root daemon; it is closed there.")


def checkout(path):
    """(Git common directory, path within its checkout) for PATH, or None outside Git."""
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "-C", path, "rev-parse", "--path-format=absolute",
                                 "--git-common-dir", "--show-prefix"], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    lines = result.stdout.split("\n")
    if result.returncode or len(lines) < 2 or not os.path.isabs(lines[0]):
        return None
    return os.path.realpath(lines[0]), lines[1]


def scope(cwd):
    """The native default picker/--last filter: CWD itself or, in a repository with
    linked worktrees, the same location in any of its checkouts."""
    here, repository = os.path.realpath(cwd), None

    def matches(path):
        nonlocal repository
        if not isinstance(path, str) or not os.path.isabs(path) or not os.path.isdir(path):
            return False
        if os.path.realpath(path) == here:
            return True
        if repository is None:
            repository = checkout(str(cwd)) or False
        return bool(repository) and checkout(path) == repository

    return matches


def release(guard, root, account, cwd):
    """Picker and --last: before the native selection, take over the root daemon's idle
    top-level sessions that it would list (those of CWD, or all when CWD is None)."""
    locks = root / "accounts/.locks"
    # Interrupted takeovers first: their sessions may be archived and no longer loaded.
    journals = [path.name.removesuffix(".handoff.json") for path in sorted(locks.glob("*.handoff.json"))]
    try:
        client = Client(root, guard)
    except (OSError, ValueError) as error:
        if journals:
            guard.warn(f"the root daemon is unavailable ({error}); interrupted takeovers keep their "
                       f"recovery records in {locks} and those sessions may stay archived until it runs.")
        elif not isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
            guard.warn(f"could not reach the root daemon ({error}); no takeover performed.")
        return
    taken, running = [], []
    listed = scope(cwd) if cwd is not None else lambda path: True
    try:
        for thread in dict.fromkeys(journals + client.request("thread/loaded/list", {})["data"]):
            try:
                if not isinstance(thread, str) or thread != str(uuid.UUID(thread)):
                    continue
                if thread not in journals:
                    value = client.request("thread/read", {"threadId": thread, "includeTurns": False})["thread"]
                    # Spawned agents follow their parent's takeover.
                    source = value.get("source")
                    if (isinstance(source, dict) and "subAgent" in source) or not listed(value.get("cwd")):
                        continue
                outcome = take_over(guard, root, client, account, thread, ask=False)
                if outcome in ("running", "taken"):
                    (running if outcome == "running" else taken).append(describe(client, thread))
            except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError) as error:
                if isinstance(error.__cause__, KeyboardInterrupt):
                    raise
                # The selection may not need this session; native Codex guards it.
                guard.warn(f"could not take over {thread} ({error}).")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError) as error:
        if isinstance(error.__cause__, KeyboardInterrupt):
            raise
        guard.warn(f"stopped taking over root daemon sessions ({error}).")
    finally:
        client.close()
    if taken:
        guard.warn("took over from the root daemon; these sessions are closed there:\n" + "\n".join(taken))
    if running:
        guard.warn("left running in the root daemon; selecting one shows Codex's conflict screen:\n"
                   + "\n".join(running) + f"\nTo end it there and continue here: codex-as {account} resume UUID")
