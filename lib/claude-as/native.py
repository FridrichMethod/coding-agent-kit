"""claude-as native-login accounts: the fixed account directory and its registration.

Load this file by path (it is never on sys.path). A native-login account NAME lives in

    <secrets_dir>/native-claude-accounts/NAME/            0700, owned
    <secrets_dir>/native-claude-accounts/NAME/config      0700: CLAUDE_CONFIG_DIR, spelled exactly
    <secrets_dir>/native-claude-accounts/NAME/account.json 0600: the registration

Native Claude Code owns everything inside config/ (its login, credential storage and refresh);
this module never reads the credential. It reads only the two identity UUIDs native records in
config/.claude.json (oauthAccount.accountUuid and organizationUuid), when enrollment confirms
them. The registration is

    {"schema_version": 1, "mode": "native-login", "state": "pending" | "ready",
     "enrollment_id": UUID, "config_directory": "<exact path>",
     "identity": null | {"account_uuid": ..., "organization_uuid": ...}}

created pending (identity null) and replaced atomically by a ready one once the user confirmed
the account native shows. Every path is opened from / without following a symlink; the account
directories must be owned by this user with no group or other permission bits, and every other
ancestor owned by this user or root and not writable by others (unless sticky).

Public API:
    Paths(config, name)                 the fixed paths; ValueError for an invalid name
    load(paths) -> dict | None          the registration, None when the account directory is absent
    create_pending(paths) -> str        a new pending registration; its enrollment id
    read_identity(config_dir) -> (account_uuid, organization_uuid) | None
    mark_ready(paths, identity)         pending -> ready when native still reports identity
    names(config) -> list[str]          the enrolled names (an account directory with a registration)
    token_id(record) -> str             the status line's identifier for this enrollment (not a token hash)
    NativeError(ValueError)             one line for the user; own_message is True
"""

import hashlib
import json
import os
import re
import stat
import uuid


ROOT_NAME = "native-claude-accounts"
REGISTRATION = "account.json"
METADATA = ".claude.json"
METADATA_LIMIT = 16 * 1024 * 1024
REGISTRATION_LIMIT = 4096
NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
UUID_TEXT = re.compile(r"[A-Za-z0-9-]{1,64}")
KEYS = {"schema_version", "mode", "state", "enrollment_id", "config_directory", "identity"}
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class NativeError(ValueError):
    own_message = True


class Paths:
    """The fixed spelling of one account: strings, never resolved, without a trailing slash."""

    __slots__ = ("name", "secrets", "root", "account", "config", "registration")

    def __init__(self, config, name):
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise NativeError("invalid account name (ASCII letters, digits, _ or -; no leading -).")
        self.name = name
        self.secrets = str(config.secrets_dir)
        self.config = str(config.native_claude_config(name))
        self.account = os.path.dirname(self.config)
        self.root = os.path.dirname(self.account)
        self.registration = self.account + "/" + REGISTRATION
        if not self.config.startswith("/") or self.config.endswith("/") or "//" in self.config:
            raise NativeError("the native account directory must be an absolute path without empty components.")


def _components(path):
    parts = path.split("/")[1:]
    if not path.startswith("/") or not parts or any(part in ("", ".", "..") for part in parts):
        raise NativeError(f"{path} must be an absolute, normalized path.")
    return parts


def _trusted(info, uid, where):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid):
        raise NativeError(f"{where} must be a directory owned by you or root.")
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        raise NativeError(f"{where} must not be writable by other users.")


def _private(info, uid, where):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise NativeError(f"{where} must be a real directory owned by you with permissions 0700.")


def open_directory(path, private=0, uid=None):
    """A directory fd for PATH walked from / without following links; its last PRIVATE components
    must be owned 0700-style directories. FileNotFoundError when a component is missing."""
    uid = os.getuid() if uid is None else uid
    parts = _components(path)
    fd = os.open("/", DIRECTORY)
    try:
        _trusted(os.fstat(fd), uid, "/")
        walked = ""
        for index, part in enumerate(parts):
            walked += "/" + part
            try:
                child = os.open(part, DIRECTORY, dir_fd=fd)
            except FileNotFoundError:
                raise
            except OSError:
                raise NativeError(f"{walked} must be a real directory (not a symlink).") from None
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if index >= len(parts) - private:
                _private(info, uid, walked)
            else:
                _trusted(info, uid, walked)
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read(directory, name, limit, exact_mode=None):
    """One bounded read of an owned regular file in DIRECTORY; None when it is absent."""
    try:
        fd = os.open(name, FILE, dir_fd=directory)
    except FileNotFoundError:
        return None
    except OSError:
        raise NativeError(f"{name} must be a regular file (not a symlink).") from None
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or (mode != exact_mode if exact_mode is not None else mode & 0o022)):
            raise NativeError(f"{name} must be an owned regular file with one link"
                              + (f" and permissions {exact_mode:04o}." if exact_mode is not None else "."))
        if not 0 < info.st_size <= limit:
            raise NativeError(f"{name} is empty or too large.")
        chunks, size = [], 0
        while size <= info.st_size:
            chunk = os.read(fd, info.st_size + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _check_record(record, paths):
    if not isinstance(record, dict) or set(record) != KEYS or record["schema_version"] != 1 \
            or record["mode"] != "native-login" or record["state"] not in ("pending", "ready"):
        raise ValueError("shape")
    if not isinstance(record["enrollment_id"], str) or str(uuid.UUID(record["enrollment_id"])) != record["enrollment_id"]:
        raise ValueError("enrollment id")
    if record["config_directory"] != paths.config:
        raise NativeError(f"the registration of {paths.name} names another configuration directory; "
                          "re-enroll it.")
    identity = record["identity"]
    if record["state"] == "pending":
        if identity is not None:
            raise ValueError("pending identity")
    elif (not isinstance(identity, dict) or set(identity) != {"account_uuid", "organization_uuid"}
          or not all(isinstance(value, str) and UUID_TEXT.fullmatch(value) for value in identity.values())):
        raise ValueError("ready identity")
    return record


def load(paths):
    """The registration of PATHS, or None when its account directory does not exist."""
    try:
        account = open_directory(paths.account, private=2)
    except FileNotFoundError:
        return None
    try:
        raw = _read(account, REGISTRATION, REGISTRATION_LIMIT, exact_mode=0o600)
    finally:
        os.close(account)
    if raw is None:
        raise NativeError(f"{paths.registration} is missing; remove {paths.account} and enroll {paths.name} again.")
    try:
        return _check_record(json.loads(raw, object_pairs_hook=_unique), paths)
    except NativeError:
        raise
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise NativeError(f"{paths.registration} is malformed; remove {paths.account} and enroll "
                          f"{paths.name} again.") from None


def _write_exclusive(directory, name, record):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            fd = None
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if fd is not None:
            os.close(fd)


def create_pending(paths):
    """The account directories (0700) and a new pending registration; refuses an existing account."""
    old = os.umask(0o077)
    try:
        secrets = open_directory(paths.secrets, private=1)
        try:
            try:
                os.mkdir(ROOT_NAME, 0o700, dir_fd=secrets)
            except FileExistsError:
                pass
        finally:
            os.close(secrets)
        root = open_directory(paths.root, private=1)
        try:
            try:
                os.mkdir(paths.name, 0o700, dir_fd=root)
            except FileExistsError:
                raise NativeError(f"{paths.account} already exists.") from None
        finally:
            os.close(root)
        account = open_directory(paths.account, private=2)
        try:
            os.mkdir("config", 0o700, dir_fd=account)
            enrollment = str(uuid.uuid4())
            _write_exclusive(account, REGISTRATION, {
                "schema_version": 1, "mode": "native-login", "state": "pending", "enrollment_id": enrollment,
                "config_directory": paths.config, "identity": None})
            os.fsync(account)
            return enrollment
        finally:
            os.close(account)
    finally:
        os.umask(old)


def read_identity(config_dir):
    """(account_uuid, organization_uuid) from native's config/.claude.json, or None while native
    has recorded no login there. Nothing else of the file is kept."""
    try:
        directory = open_directory(config_dir, private=3)
    except FileNotFoundError:
        return None
    try:
        raw = _read(directory, METADATA, METADATA_LIMIT)
    finally:
        os.close(directory)
    if raw is None:
        return None
    try:
        document = json.loads(raw, object_pairs_hook=_unique)
        account = document.get("oauthAccount") if isinstance(document, dict) else None
        pair = (account.get("accountUuid"), account.get("organizationUuid")) if isinstance(account, dict) else (None, None)
    except (ValueError, RecursionError):
        raise NativeError(f"{config_dir}/{METADATA} is not valid JSON.") from None
    finally:
        raw = document = account = None
    if not all(isinstance(value, str) and UUID_TEXT.fullmatch(value) for value in pair):
        return None
    return pair


def mark_ready(paths, identity):
    """Replace the pending registration by a ready one for IDENTITY, which native must still report."""
    record = load(paths)
    if record is None or record["state"] != "pending":
        raise NativeError(f"the enrollment of {paths.name} is not pending.")
    if read_identity(paths.config) != tuple(identity):
        raise NativeError(f"the account signed in for {paths.name} changed while you confirmed it; run "
                          f"claude-as --enroll-native {paths.name} again.")
    record.update(state="ready", identity={"account_uuid": identity[0], "organization_uuid": identity[1]})
    account = open_directory(paths.account, private=2)
    staging = ".account.json." + uuid.uuid4().hex
    try:
        _write_exclusive(account, staging, record)
        os.rename(staging, REGISTRATION, src_dir_fd=account, dst_dir_fd=account)
        os.fsync(account)
    except BaseException:
        try:
            os.unlink(staging, dir_fd=account)
        except OSError:
            pass
        raise
    finally:
        os.close(account)
    return record


def names(config):
    """Enrolled native-login names: account directories with a registration file (not opened)."""
    root = os.path.join(str(config.secrets_dir), ROOT_NAME)
    try:
        entries = os.listdir(root)
    except (FileNotFoundError, NotADirectoryError):
        return []
    found = []
    for name in entries:
        path = os.path.join(root, name)
        try:
            info = os.lstat(path)
            registration = os.lstat(os.path.join(path, REGISTRATION))
        except OSError:
            continue
        if (NAME.fullmatch(name) and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_ISREG(registration.st_mode)):
            found.append(name)
    return sorted(found)


def token_id(record):
    """CODING_AGENT_KIT_TOKEN_ID for a native-login account: derived from the enrollment id, never
    from a credential, so a session that shows a token of its own never matches it."""
    return hashlib.sha256(("native-login:" + record["enrollment_id"]).encode("ascii")).hexdigest()[:16]
