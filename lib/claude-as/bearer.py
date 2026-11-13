"""claude-as protected mode: the memory-only read of a native-login account's access token.

Load this file by path (it is never on sys.path). Only the gate process calls it: the launcher never
reads the credential store. Native Claude Code owns the store and its refresh; this reader never
writes, refreshes, logs, persists or hashes anything, and keeps only the access token (wrapped at
once by the caller's secret type), its expiry and whether it carries the inference scope. The
refresh token and every other field are parsed past and dropped.

Stores (Claude Code 2.1.322, S; reviewed design QC option 3):
    Linux   CONFIG/.credentials.json, an owned regular 0600 file with one link, read once by
            descriptor from a directory walked from / without following links (the last three
            components, native-claude-accounts/NAME/config, private). A native rename between the
            read and a re-check of the directory entry means the snapshot was superseded: one retry.
    macOS   the login Keychain item native writes for this exact CLAUDE_CONFIG_DIR spelling:
            /usr/bin/security find-generic-password -a ACCOUNT -s "Claude Code-credentials-HASH" -w,
            HASH the first 8 hex digits of SHA-256 of the NFC config path (a trailing slash would name
            another item), ACCOUNT native's own choice (USER, else claude-code-user for other names).
            No item (exit 44) falls back to the Linux file, which native writes when the Keychain
            write fails. The command runs with a fixed PATH and HOME, no stdin, a 10 s bound and a
            bounded output.

Public API:
    read(config_dir, *, platform, wrap, now_ms=None, keychain_user=None, home=None,
         security=SECURITY) -> Credential(secret, expires_at_ms)
    Credential: secret is wrap(token); expires_at_ms an int.
    keychain_service(config_dir), keychain_account(user), parse_document(raw, now_ms, wrap)
    Refused(reason): a fixed reason word only, never a path, value or token.
"""

from dataclasses import dataclass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import unicodedata


CREDENTIAL_FILE = ".credentials.json"
DOCUMENT_LIMIT = 256 * 1024
TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{16,8192}")
MAX_EXPIRES_MS = 7258118400000  # 2200-01-01T00:00:00Z
SECURITY = "/usr/bin/security"
KEYCHAIN_NOT_FOUND = 44
KEYCHAIN_TIMEOUT = 10.0
ACCOUNT_NAME = re.compile(r"[a-zA-Z0-9._-]+")
FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Credential:
    secret: object
    expires_at_ms: int

    def __repr__(self):
        return "Credential(<redacted>, expires_at_ms=%d)" % self.expires_at_ms


def _native():
    spec = importlib.util.spec_from_file_location("claude_as_bearer_native",
                                                  Path(os.path.realpath(__file__)).parent / "native.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused("document_malformed")
        result[key] = value
    return result


def _reject_constant(name):
    raise Refused("document_malformed")


def parse_document(raw, now_ms, wrap):
    """The Credential of one native credential document; refuses an expired or scope-less token."""
    if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= DOCUMENT_LIMIT:
        raise Refused("document_size")
    document = oauth = token = None
    try:
        try:
            document = json.loads(bytes(raw).decode("utf-8"), object_pairs_hook=_unique,
                                  parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise Refused("document_malformed") from None
        oauth = document.get("claudeAiOauth") if isinstance(document, dict) else None
        if not isinstance(oauth, dict):
            raise Refused("oauth_record_missing")
        token, expires, scopes = oauth.get("accessToken"), oauth.get("expiresAt"), oauth.get("scopes")
        if not isinstance(token, str) or not TOKEN.fullmatch(token):
            raise Refused("access_token_malformed")
        if type(expires) is not int or not 0 < expires < MAX_EXPIRES_MS:
            raise Refused("expiry_malformed")
        if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
            raise Refused("scopes_malformed")
        if "user:inference" not in scopes:
            raise Refused("inference_scope_missing")
        if expires <= now_ms:
            raise Refused("token_expired")
        try:
            return Credential(wrap(token), expires)
        except ValueError:
            raise Refused("access_token_malformed") from None
    finally:
        document = oauth = token = None  # drop the only references to the refresh material


def _snapshot(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode,
            info.st_nlink, info.st_uid)


def _read_once(directory):
    try:
        fd = os.open(CREDENTIAL_FILE, FILE, dir_fd=directory)
    except FileNotFoundError:
        raise Refused("credential_absent") from None
    except OSError:
        raise Refused("credential_unusable") from None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
            raise Refused("credential_not_private")
        if not 0 < before.st_size <= DOCUMENT_LIMIT:
            raise Refused("document_size")
        chunks, total = [], 0
        while total <= before.st_size:
            chunk = os.read(fd, before.st_size + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        chunks = None
        if _snapshot(before) != _snapshot(os.fstat(fd)) or len(raw) != before.st_size:
            raise Refused("credential_changed_during_read")
        try:
            entry = os.stat(CREDENTIAL_FILE, dir_fd=directory, follow_symlinks=False)
        except OSError:
            entry = None
        return raw, entry is None or (entry.st_dev, entry.st_ino) != (before.st_dev, before.st_ino)
    finally:
        os.close(fd)


def read_file(config_dir, now_ms, wrap, attempts=2):
    native = _native()
    try:
        directory = native.open_directory(config_dir, private=3)
    except (FileNotFoundError, native.NativeError):
        raise Refused("account_directory_unusable") from None
    try:
        for _ in range(attempts):
            raw, rotated = _read_once(directory)
            try:
                if not rotated:
                    return parse_document(raw, now_ms, wrap)
            finally:
                raw = None
        raise Refused("credential_rotating")
    finally:
        os.close(directory)


def keychain_service(config_dir):
    digest = hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode("utf-8")).hexdigest()
    return "Claude Code-credentials-" + digest[:8]


def keychain_account(user):
    return user if isinstance(user, str) and ACCOUNT_NAME.fullmatch(user) else "claude-code-user"


def read_keychain(config_dir, now_ms, wrap, user, home, security=SECURITY):
    """The Keychain item's Credential, or None when there is no item."""
    argv = [security, "find-generic-password", "-a", keychain_account(user), "-s", keychain_service(config_dir), "-w"]
    env = {"PATH": "/usr/bin:/bin"}
    if home:
        env["HOME"] = home
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, timeout=KEYCHAIN_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise Refused("keychain_timeout") from None
    except OSError:
        raise Refused("keychain_unavailable") from None
    output = result.stdout
    try:
        if result.returncode == KEYCHAIN_NOT_FOUND:
            return None
        if result.returncode != 0:
            raise Refused("keychain_read_failed")
        if len(output) > 2 * DOCUMENT_LIMIT + 1:
            raise Refused("document_size")
        data = output.rstrip(b"\n")
        if data[:1] != b"{":
            if not data or len(data) % 2 or not re.fullmatch(rb"[0-9a-fA-F]+", data):
                raise Refused("document_malformed")
            data = bytes.fromhex(data.decode("ascii"))
        return parse_document(data, now_ms, wrap)
    finally:
        output = data = result = None


def read(config_dir, *, platform, wrap, now_ms=None, keychain_user=None, home=None, security=SECURITY):
    """The account's current Credential from the store native uses on PLATFORM ("linux" or "darwin")."""
    if not isinstance(config_dir, str) or config_dir.endswith("/") \
            or unicodedata.normalize("NFC", config_dir) != config_dir:
        raise Refused("path_not_canonical")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    if platform == "darwin":
        found = read_keychain(config_dir, now_ms, wrap, keychain_user, home, security)
        if found is not None:
            return found
    elif platform != "linux":
        raise Refused("platform_unsupported")
    return read_file(config_dir, now_ms, wrap)
