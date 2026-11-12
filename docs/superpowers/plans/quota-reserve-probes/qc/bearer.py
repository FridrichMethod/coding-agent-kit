"""QC research: memory-only access-bearer acquisition from a native Claude store.

Research code only, not launcher runtime. It is exercised exclusively against
fabricated fixtures. Actual credential reads are NOT approved; G0 must accept
this expanded read surface before any product use.

Linux: native Claude 2.1.322 stores full-login OAuth in
<config>/.credentials.json (mode 0600, staged O_EXCL temp file, fsync, rename;
an in-place truncate/write arm exists only for EBUSY bind mounts). The reader
walks the fixed canonical path without following symlinks, requires private
owned directories and an owned regular single-link 0600 file, reads one bounded
snapshot by descriptor and keeps only accessToken plus expiry (and two scope
booleans) in memory. It never logs, persists or hashes the token, and never
validates, copies or retains refreshToken: the JSON parser passes over it and
the parsed document is dropped immediately.

macOS: only the command shape is designed here (see keychain_argv). It is never
executed by this node; execute_keychain refuses unless a caller passes an
explicit permission object, which no test or harness constructs.
"""

import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
import unicodedata

CREDENTIAL_FILE = ".credentials.json"
DOCUMENT_LIMIT = 256 * 1024
TOKEN_RE = re.compile(r"[A-Za-z0-9._~+/=-]{16,8192}")
MAX_EXPIRES_MS = 7258118400000  # 2200-01-01T00:00:00Z
EXPIRY_SKEW_MS = 30000
KEYCHAIN_TOOL = "/usr/bin/security"
KEYCHAIN_NOT_FOUND = 44
KEYCHAIN_CREDENTIAL_SUFFIX = "-credentials"
_ACCOUNT_RE = re.compile(r"[a-zA-Z0-9._-]+")

# Environment selectors that make native ignore or relocate the account store.
# Any of them present (even empty) refuses: native gives an empty
# CLAUDE_SECURESTORAGE_CONFIG_DIR special meaning, and env/fd tokens win over
# the stored login (native hg(): "env"/"fd" before "store").
STORE_SELECTORS = (
    "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_CUSTOM_OAUTH_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_REMOTE_SESSION_ID",
    "CLAUDE_LOCAL_OAUTH_API_BASE",
)


class BearerRefused(Exception):
    """Carries only a fixed reason code; never a path, value or token."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class AccessBearer:
    """Opaque in-memory bearer. Unhashable, unpicklable, redacted repr.

    Python strings cannot be wiped; the object minimizes copies but does not
    claim memory scrubbing. Compare with matches(); == is refused.
    """

    __slots__ = ("_token", "expires_at_ms", "profile_scope", "inference_scope", "source")
    __hash__ = None

    def __init__(self, token, expires_at_ms, profile_scope, inference_scope, source):
        object.__setattr__(self, "_token", token)
        object.__setattr__(self, "expires_at_ms", expires_at_ms)
        object.__setattr__(self, "profile_scope", profile_scope)
        object.__setattr__(self, "inference_scope", inference_scope)
        object.__setattr__(self, "source", source)

    def __setattr__(self, name, value):
        raise AttributeError("AccessBearer is immutable")

    def __repr__(self):
        return "AccessBearer(<redacted>, source=%s)" % self.source

    __str__ = __repr__

    def __eq__(self, other):
        raise TypeError("compare bearers with matches()")

    def __reduce_ex__(self, protocol):
        raise TypeError("AccessBearer cannot be serialized")

    def __copy__(self):
        raise TypeError("AccessBearer cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("AccessBearer cannot be copied")

    def matches(self, presented):
        """Constant-time equality with another bearer or a presented header token."""
        other = presented._token if isinstance(presented, AccessBearer) else presented
        if not isinstance(other, str):
            return False
        return hmac.compare_digest(self._token.encode("ascii"), other.encode("ascii", "replace"))

    def authorization_value(self):
        return "Bearer " + self._token

    def expired(self, now_ms, skew_ms=EXPIRY_SKEW_MS):
        return self.expires_at_ms <= now_ms + skew_ms


def check_store_selection(env, config_dir):
    """Refuse environments where native would not use exactly this account store."""
    for name in STORE_SELECTORS:
        if name in env:
            raise BearerRefused("conflicting_selector")
    if env.get("CLAUDE_CONFIG_DIR") != config_dir:
        raise BearerRefused("config_dir_mismatch")


def canonical_parts(config_dir):
    """Validate the one recorded spelling; return its components.

    Native hashes the exact NFC CLAUDE_CONFIG_DIR string for the macOS Keychain
    service name (no realpath, no trailing-slash removal), so a trailing slash
    or alternate spelling would select a different Keychain item.
    """
    if not isinstance(config_dir, str) or not config_dir.startswith("/") or len(config_dir) > 4096:
        raise BearerRefused("path_not_canonical")
    if unicodedata.normalize("NFC", config_dir) != config_dir or any(
            ord(ch) < 0x20 or ord(ch) == 0x7F for ch in config_dir):
        raise BearerRefused("path_not_canonical")
    parts = config_dir.split("/")[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise BearerRefused("path_not_canonical")
    return parts


def _check_ancestor(info, uid):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, uid):
        raise BearerRefused("ancestor_not_trusted")
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        raise BearerRefused("ancestor_writable")


def _check_private(info, uid):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o077:
        raise BearerRefused("account_directory_not_private")


def open_account_directory(config_dir, uid, private_depth=3):
    """Open config_dir by walking from / with O_NOFOLLOW; return a directory fd.

    The last private_depth components (native-claude-accounts, NAME, config)
    must be uid-owned 0700-style directories. Earlier ancestors must be owned by
    root or uid and not group/other writable unless sticky.
    """
    parts = canonical_parts(config_dir)
    if private_depth < 1 or private_depth > len(parts):
        raise BearerRefused("path_not_canonical")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open("/", flags)
    try:
        _check_ancestor(os.fstat(directory), uid)
        for index, part in enumerate(parts):
            try:
                next_fd = os.open(part, flags, dir_fd=directory)
            except OSError:
                raise BearerRefused("path_component_unusable") from None
            os.close(directory)
            directory = next_fd
            info = os.fstat(directory)
            if index >= len(parts) - private_depth:
                _check_private(info, uid)
            else:
                _check_ancestor(info, uid)
        result, directory = directory, None
        return result
    finally:
        if directory is not None:
            os.close(directory)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BearerRefused("duplicate_key")
        result[key] = value
    return result


def _reject_constant(name):
    raise BearerRefused("non_finite_number")


def parse_credential_document(raw, now_ms, source):
    """Minimize one native credential document to an AccessBearer.

    Only claudeAiOauth.accessToken, expiresAt and two scope booleans survive.
    refreshToken and every other field are parsed past and dropped unexamined.
    """
    if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= DOCUMENT_LIMIT:
        raise BearerRefused("document_size")
    try:
        text = bytes(raw).decode("utf-8")
        document = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except BearerRefused:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise BearerRefused("document_malformed") from None
    finally:
        text = None
    try:
        oauth = document.get("claudeAiOauth") if isinstance(document, dict) else None
        if not isinstance(oauth, dict):
            raise BearerRefused("oauth_record_missing")
        token = oauth.get("accessToken")
        expires = oauth.get("expiresAt")
        scopes = oauth.get("scopes")
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise BearerRefused("access_token_malformed")
        if type(expires) is not int or not 0 < expires < MAX_EXPIRES_MS:
            raise BearerRefused("expiry_malformed")
        if not isinstance(scopes, list) or not all(isinstance(item, str) for item in scopes):
            raise BearerRefused("scopes_malformed")
        profile, inference = "user:profile" in scopes, "user:inference" in scopes
        if not inference:
            raise BearerRefused("inference_scope_missing")
        if expires <= now_ms + EXPIRY_SKEW_MS:
            raise BearerRefused("expired_native_refresh_required")
        return AccessBearer(token, expires, profile, inference, source)
    finally:
        # Drop the only references to refresh material and other fields.
        document = oauth = scopes = token = None


def _snapshot(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_nlink, info.st_uid)


def _read_once(directory, uid):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(CREDENTIAL_FILE, flags, dir_fd=directory)
    except FileNotFoundError:
        raise BearerRefused("credential_file_absent") from None
    except OSError:
        raise BearerRefused("credential_file_unusable") from None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != uid
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
            raise BearerRefused("credential_file_not_private")
        if not 0 < before.st_size <= DOCUMENT_LIMIT:
            raise BearerRefused("document_size")
        chunks, total = [], 0
        while total <= before.st_size:
            chunk = os.read(fd, before.st_size + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        chunks = None
        after = os.fstat(fd)
        if _snapshot(before) != _snapshot(after) or len(raw) != before.st_size:
            raise BearerRefused("credential_changed_during_read")
        try:
            entry = os.stat(CREDENTIAL_FILE, dir_fd=directory, follow_symlinks=False)
        except OSError:
            entry = None
        rotated = entry is None or (entry.st_dev, entry.st_ino) != (before.st_dev, before.st_ino)
        return raw, rotated
    finally:
        os.close(fd)


def read_linux_bearer(config_dir, env, now_ms=None, uid=None, private_depth=3, attempts=2):
    """Read one memory-only bearer from the fixed account directory.

    A native atomic rename between open and the directory re-check means the
    snapshot was superseded; retry at most attempts times, then refuse.
    """
    check_store_selection(env, config_dir)
    uid = os.getuid() if uid is None else uid
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    directory = open_account_directory(config_dir, uid, private_depth)
    try:
        for _ in range(max(1, attempts)):
            raw, rotated = _read_once(directory, uid)
            try:
                if not rotated:
                    return parse_credential_document(raw, now_ms, "linux-file")
            finally:
                raw = None
        raise BearerRefused("credential_rotating")
    finally:
        os.close(directory)


def keychain_service(config_dir, env, oauth_suffix=""):
    """Native Yxe("-credentials") for an explicit CLAUDE_CONFIG_DIR (static, 2.1.322)."""
    canonical_parts(config_dir)
    if "CLAUDE_SECURESTORAGE_CONFIG_DIR" in env:
        raise BearerRefused("conflicting_selector")
    digest = hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode("utf-8")).hexdigest()
    return "Claude Code" + oauth_suffix + KEYCHAIN_CREDENTIAL_SUFFIX + "-" + digest[:8]


def keychain_account(env, fallback_user):
    """Native j3(): USER, else OS user name; invalid names map to claude-code-user."""
    name = env.get("USER") or fallback_user
    return name if isinstance(name, str) and _ACCOUNT_RE.fullmatch(name) else "claude-code-user"


def keychain_argv(config_dir, env, fallback_user):
    """Exact designed call; argv only, no shell, no -i stdin script, no -g."""
    check_store_selection(env, config_dir)
    return [KEYCHAIN_TOOL, "find-generic-password", "-a", keychain_account(env, fallback_user),
            "-s", keychain_service(config_dir, env), "-w"]


def decode_keychain_output(stdout):
    """`security -w` prints the item; accept JSON text or strict hex of JSON."""
    if not isinstance(stdout, (bytes, bytearray)) or len(stdout) > 2 * DOCUMENT_LIMIT + 1:
        raise BearerRefused("document_size")
    data = bytes(stdout).rstrip(b"\n")
    if data[:1] == b"{":
        return data
    if data and len(data) % 2 == 0 and re.fullmatch(rb"[0-9a-fA-F]+", data):
        return bytes.fromhex(data.decode("ascii"))
    raise BearerRefused("document_malformed")


class KeychainPermission:
    """Placeholder for a future reviewed, human-approved permission. Never built here."""

    def __init__(self):
        raise BearerRefused("keychain_read_not_approved")


def execute_keychain(argv, permission):
    if not isinstance(permission, KeychainPermission) or sys.platform != "darwin":
        raise BearerRefused("keychain_read_not_approved")
    raise BearerRefused("keychain_read_not_implemented")


def read_macos_bearer(config_dir, env, fallback_user, runner, now_ms):
    """Design path for the Keychain store; runner is injected (fake in tests).

    runner(argv) -> (exit_code, stdout_bytes). Exit 44 means no item; native may
    then use its plaintext fallback file, which read_linux_bearer validates.
    """
    argv = keychain_argv(config_dir, env, fallback_user)
    code, stdout = runner(argv)
    try:
        if code == KEYCHAIN_NOT_FOUND:
            raise BearerRefused("keychain_item_absent")
        if code != 0:
            raise BearerRefused("keychain_read_failed")
        return parse_credential_document(decode_keychain_output(stdout), now_ms, "macos-keychain")
    finally:
        stdout = None
