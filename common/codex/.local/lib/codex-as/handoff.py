"""Confirmed native archive/unarchive takeover; never kills a daemon or removes locks."""

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


def hint(guard, root, account):
    """For the picker and --last: name the root daemon sessions a UUID takeover needs."""
    try:
        client = Client(root, guard, timeout=5)
    except (FileNotFoundError, ConnectionRefusedError):
        return  # No root daemon is running.
    except (OSError, ValueError) as error:
        guard.warn(f"could not list sessions open in the root daemon ({error}).")
        return
    sessions = []
    try:
        loaded = client.request("thread/loaded/list", {})["data"]
        for thread in loaded[:10]:
            value = client.request("thread/read", {"threadId": thread, "includeTurns": False})["thread"]
            # Spawned agents follow their parent's takeover; list only top-level sessions.
            source = value.get("source")
            if not (isinstance(source, dict) and "subAgent" in source):
                # One line per session: fold whitespace first, then drop control characters.
                label = " ".join(str(value.get("name") or value.get("preview") or "").split())
                label = "".join(char for char in label if char.isprintable())[:60]
                sessions.append(f"  {uuid.UUID(value['id'])}  {label}".rstrip())
    except (OSError, ValueError, KeyError, TypeError) as error:
        guard.warn(f"could not list sessions open in the root daemon ({type(error).__name__}).")
        return
    finally:
        client.close()
    if len(loaded) > 10:
        sessions.append(f"  ... and up to {len(loaded) - 10} more")
    if sessions:
        guard.warn("the root daemon holds these sessions; the picker and --last cannot take them over:\n"
                   + "\n".join(sessions) + f"\nTo move one to this account: codex-as {account} resume UUID")


def prepare(guard, root, account, thread):
    """Prompt only for an explicit interactive resume UUID held by the root daemon."""
    if thread is None:
        return
    journal = root / "accounts/.locks" / (thread + ".handoff.json")
    if not os.path.lexists(journal) and not locked(root, thread):
        return
    # Serialize wrapper takeovers; the native writer lock is never removed.
    lease = journal.with_suffix(".lock")
    fd = os.open(lease, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    client = None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError("unsafe takeover lease.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another codex-as takeover is in progress for this UUID.") from None
        pending = os.path.lexists(journal)
        if not pending and not locked(root, thread):
            return
        try:
            client = Client(root, guard)
        except (OSError, ValueError) as error:
            raise ValueError("session is in use; the root daemon is unavailable. Close the owning client, then retry. "
                             + (f"Preserve recovery record {journal}." if pending else str(error))) from None
        if pending:
            record = json.loads(guard.private_metadata(journal))
            if (record.get("version") != 1 or record.get("thread") != thread
                    or not isinstance(record.get("archived"), list)):
                raise ValueError("unrecognized takeover recovery record; preserve it.")
            original_archived = {str(uuid.UUID(item)) for item in record["archived"]}
        else:
            loaded = client.request("thread/loaded/list", {})["data"]
            if thread not in loaded:
                raise ValueError("session writer belongs to another client; exit that client before switching accounts.")
            if root_archived(client, root, thread):
                raise ValueError("session is already archived; unarchive it in the original client first.")
            original_archived = descendants(client, thread, True)
        tty = guard.terminal()
        try:
            if pending:
                guard.confirm(tty, f"Restore interrupted takeover of {thread} before resuming with {account}?")
            else:
                tty.write("Takeover ends this session AND its spawned agents in the old daemon, including active work.\n"
                          "Close other views of this session first. Saved history is kept; archive visibility is restored.\n", "33")
                guard.confirm(tty, f"Take over {thread} for account {account}?")
        finally:
            tty.close()
        if pending:
            try:
                restore(client, root, thread, original_archived)
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise ValueError(f"takeover recovery failed; preserve {journal} and retry the same command.") from error
            journal.unlink()
        else:
            # Refresh after confirmation: reading the tree is not a transaction.
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
        if locked(root, thread):
            raise ValueError("takeover ended the session but its writer is still shutting down; retry after it exits.")
    finally:
        if client is not None:
            client.close()
        os.close(fd)
