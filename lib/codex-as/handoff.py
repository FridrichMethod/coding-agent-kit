"""Native archive/unarchive takeover from the root daemon; never kills a daemon or removes locks."""

import base64
from contextlib import contextmanager
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
    own_message = True  # built here from the method name and an integer error code only


class Pending(ValueError):
    """A takeover whose archive may have happened: its journal is kept for recovery."""
    own_message = True


# Client construction and replies can fail in these ways; none is a credential problem.
FAILURES = (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError, RuntimeError)


class Client:
    """Bounded RFC 6455 JSON-RPC client for an existing owned Unix daemon."""

    def __init__(self, root, guard, timeout=10):
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

    def request(self, method, params, timeout=None):
        self.deadline = time.monotonic() + (timeout or self.timeout)
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
                code = result["error"].get("code") if isinstance(result["error"], dict) else None
                raise RpcError(f"daemon {method} refused ({code if type(code) is int else 'unknown'}).")
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
        # this verified 0700 directory. Open relative to its verified fd,
        # writable like native: NFS emulates flock with write locks.
        try:
            try:
                fd = os.open(path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except PermissionError:
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
        client.request("thread/unarchive", {"threadId": thread}, timeout=30)
    except RpcError:
        # A partial archive can leave root unarchived. Verify that state;
        # never interpret a timeout/disconnect as an already-unarchived reply.
        if root_archived(client, root, thread):
            raise
    current = descendants(client, thread, True)
    for item in sorted(current - original_archived, key=lambda item: (item != thread, item)):
        client.request("thread/unarchive", {"threadId": item}, timeout=30)
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
    except FAILURES:
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


@contextmanager
def lease(journal):
    """Serialize wrapper takeovers of one UUID; the native writer lock is never removed."""
    path = journal.with_suffix(".lock")
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError("unsafe takeover lease.")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("another codex-as takeover is in progress for this UUID.") from None
            try:
                current = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            # A finished takeover unlinks the file it locked; lock the current one instead.
            if current is not None and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                break
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)
    try:
        yield
    finally:
        try:
            os.unlink(path)  # still locked: a waiter that opened it retries on the new file
        except FileNotFoundError:
            pass
        os.close(fd)


def record(guard, journal):
    """The journal's record, or None after removing a torn one; the caller holds the lease.

    Archive starts only after the journal is complete and synced, so a torn
    one means it never started: there is nothing to restore."""
    raw = guard.private_metadata(journal)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        journal.unlink()
        return None


def discard_torn(guard, journal):
    """Remove a torn journal without a daemon; True when no journal remains."""
    try:
        with lease(journal):
            return not os.path.lexists(journal) or record(guard, journal) is None
    except ValueError:
        return False  # in progress elsewhere, or unsafe: keep it


def finish(guard, root, client, thread, journal):
    """Complete an interrupted takeover of THREAD from its journal; the caller holds the lease."""
    if not os.path.lexists(journal):
        return
    value = record(guard, journal)
    if value is None:
        return
    if (not isinstance(value, dict) or value.get("version") != 1 or value.get("thread") != thread
            or not isinstance(value.get("archived"), list)):
        raise ValueError("unrecognized takeover recovery record; preserve it.")
    try:
        restore(client, root, thread, {str(uuid.UUID(item)) for item in value["archived"]})
    except FAILURES as error:
        raise Pending(f"takeover recovery failed; preserve {journal} and retry the same command.") from error
    journal.unlink()


def take_over(guard, root, client, account, thread, ask):
    """Release THREAD from the root daemon: "free", "taken", "running" (left alone) or "elsewhere"."""
    journal = root / "accounts/.locks" / (thread + ".handoff.json")
    with lease(journal):
        # Finish an interrupted takeover; restoring saved visibility needs no confirmation.
        finish(guard, root, client, thread, journal)
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
            client.request("thread/archive", {"threadId": thread}, timeout=30)
            completed = True
        except RpcError:
            completed = True
            raise
        except BaseException as error:
            raise Pending(f"takeover outcome is unknown; preserve {journal} and retry the same command to recover.") from error
        finally:
            if completed:
                try:
                    restore(client, root, thread, original_archived)
                except FAILURES as error:
                    raise Pending(f"takeover restore failed; preserve {journal} and retry the same command.") from error
                journal.unlink()
        deadline = time.monotonic() + 5
        while locked(root, thread):
            if time.monotonic() > deadline:
                raise ValueError("takeover ended the session but its writer is still shutting down; retry after it exits.")
            time.sleep(.05)
        return "taken"


def prepare(guard, root, account, thread, take=True):
    """Explicit resume UUID: take it over from the root daemon, asking only if it may be running.

    With take=False (fork, exec resume) only an interrupted takeover is finished."""
    if thread is None:
        return
    journal = root / "accounts/.locks" / (thread + ".handoff.json")
    held = take and locked(root, thread)
    # Look for the journal after the probe: a concurrent takeover writes it before archiving.
    pending = os.path.lexists(journal) and not discard_torn(guard, journal)
    if not held and not pending:
        return
    try:
        client = Client(root, guard)
    except FAILURES as error:
        if pending:
            raise ValueError(f"the root daemon is unavailable; preserve recovery record {journal} and retry.") from None
        # Native Codex shows its own conflict screen, as it would without codex-as.
        guard.warn(f"session is in use and the root daemon is unavailable ({error}); no takeover performed.")
        return
    try:
        if not take:
            with lease(journal):
                finish(guard, root, client, thread, journal)
            return
        try:
            outcome = take_over(guard, root, client, account, thread, ask=True)
        except OSError as error:
            # Archive and restore failures arrive as ValueError; this one changed nothing,
            # or came while waiting for an ended session's writer to exit.
            guard.warn(f"root daemon request failed ({error}); native Codex reports the session if it is still held.")
            return
    finally:
        client.close()
    if outcome == "elsewhere":
        guard.warn("session is in use by another client, not the root daemon; no takeover performed.")
    elif outcome == "taken":
        guard.warn(f"took over {thread} from the root daemon; it is closed there.")


def git(path, *args):
    env = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "-C", path, *args], env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None if result.returncode else result.stdout


CHECKOUTS = {}


def checkout(path):
    """(Git common directory, path within its checkout) for PATH, or None outside Git."""
    if path not in CHECKOUTS:
        lines = (git(path, "rev-parse", "--path-format=absolute", "--git-common-dir", "--show-prefix") or "").split("\n")
        CHECKOUTS[path] = (os.path.realpath(lines[0]), lines[1]) if len(lines) >= 2 and os.path.isabs(lines[0]) else None
    return CHECKOUTS[path]


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


def places(cwd):
    """Native repository_cwd_filter paths: CWD, its resolved path, and the same place in
    every checkout of its repository."""
    result = list(dict.fromkeys([str(cwd), os.path.realpath(cwd)]))
    here = checkout(str(cwd))
    for line in (git(str(cwd), "worktree", "list", "--porcelain") or "").splitlines() if here else ():
        if line.startswith("worktree "):
            root = os.path.realpath(line.removeprefix("worktree "))
            candidate = os.path.realpath(os.path.join(root, here[1]))
            if (os.path.isdir(candidate) and candidate.startswith(root) and candidate not in result
                    and checkout(candidate) == here):
                result.append(candidate)
    return result


def latest(client, cwds, kinds, provider="openai"):
    """The session native resume --last opens: the most recently updated one listed for
    CWDS (any directory when None); nothing when its rollout file is gone.

    Native lists the history of its configured provider; codex-as refuses any but openai."""
    params = {"limit": 1, "sortKey": "updated_at", "sourceKinds": kinds, "archived": False,
              "modelProviders": [provider]}
    if cwds is not None:
        params["cwd"] = cwds[0] if len(cwds) == 1 else cwds
    # Like native: the state database first, then a scan when it fails or finds no rollout.
    for state_only in (True, False):
        try:
            data = client.request("thread/list", {**params, "useStateDbOnly": state_only})["data"]
        except RpcError:
            if state_only:
                continue
            raise
        path = data[0].get("path") if data else None
        if isinstance(path, str) and os.path.exists(path):
            return data[0]["id"]
    return None


def release(guard, root, account, cwd, last=False, others=False, provider="openai"):
    """Picker and --last: before the native selection, take over what it can open from the
    root daemon. --last opens one session, so only that one; the picker lists the idle
    top-level sessions of CWD (all when CWD is None) of its source kinds and provider
    that have a first message. Interrupted takeovers are only recovered."""
    locks = root / "accounts/.locks"
    # Interrupted takeovers first: their sessions may be archived and no longer loaded.
    journals = [path.name.removesuffix(".handoff.json") for path in sorted(locks.glob("*.handoff.json"))]
    journals = [thread for thread in journals if not discard_torn(guard, locks / (thread + ".handoff.json"))]
    try:
        client = Client(root, guard)
    except FAILURES as error:
        if journals:
            guard.warn(f"the root daemon is unavailable ({error}); interrupted takeovers keep their "
                       f"recovery records in {locks} and those sessions may stay archived until it runs.")
        elif not isinstance(error, (FileNotFoundError, ConnectionRefusedError)):
            guard.warn(f"could not reach the root daemon ({error}); no takeover performed.")
        return
    taken, running = [], []
    listed = scope(cwd) if cwd is not None else lambda path: True
    # The native picker and --last list interactive sources unless --include-non-interactive.
    kinds = ["cli", "vscode", *(["exec", "appServer"] if others else [])]
    target = None

    def attempt(thread, check):
        try:
            if not isinstance(thread, str) or thread != str(uuid.UUID(thread)):
                return
            if check is None:
                journal = locks / (thread + ".handoff.json")
                with lease(journal):
                    finish(guard, root, client, thread, journal)
                return
            if check:
                value = client.request("thread/read", {"threadId": thread, "includeTurns": False})["thread"]
                # Spawned agents follow their parent's takeover; native lists no
                # session without a first message, nor an ephemeral one.
                if (value.get("source") not in kinds or not listed(value.get("cwd"))
                        or not value.get("preview") or value.get("ephemeral")
                        or value.get("modelProvider", provider) != provider):
                    return
            outcome = take_over(guard, root, client, account, thread, ask=False)
            if outcome in ("running", "taken"):
                (running if outcome == "running" else taken).append(describe(client, thread))
        except FAILURES as error:
            # --last would silently open another session while its own may be archived.
            if (isinstance(error, OSError) or isinstance(error.__cause__, KeyboardInterrupt)
                    or (isinstance(error, Pending) and thread == target)):
                raise
            # The selection may not need this session; native Codex guards it.
            guard.warn(f"could not take over {thread} ({error}).")

    try:
        for thread in journals:
            attempt(thread, check=None)
        loaded = client.request("thread/loaded/list", {})["data"]
        if last and loaded:
            # After recovery, which makes a restored session the newest one.
            target = latest(client, None if cwd is None else places(cwd), kinds, provider)
            if target in loaded:
                attempt(target, check=False)
        else:
            for thread in loaded:
                attempt(thread, check=True)
    except FAILURES as error:
        if isinstance(error, Pending) or isinstance(error.__cause__, KeyboardInterrupt):
            raise
        guard.warn(f"stopped taking over root daemon sessions ({error}).")
    finally:
        client.close()
    if taken:
        guard.warn("took over from the root daemon; these sessions are closed there:\n" + "\n".join(taken))
    if running:
        guard.warn("left running in the root daemon; selecting one shows Codex's conflict screen:\n"
                   + "\n".join(running) + f"\nTo end it there and continue here: codex-as {account} resume UUID")
