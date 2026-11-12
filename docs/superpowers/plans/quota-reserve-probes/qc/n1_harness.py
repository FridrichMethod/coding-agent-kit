"""QC research: bounded HLogin/N1 live-harness design, tested only with fakes.

NOT authorized for any real account. Running it against a real account needs
the separately reviewed, named-account HLogin and N1 permissions drafted in
quota-reserve-qc-feasibility.md. Nothing here performs a login.

Design points (PN N0/HLogin/N1):
- One alias resolves to one fixed canonical directory; nothing is enumerated.
- Registration starts pending with identity null and a random enrollment id;
  it becomes ready atomically only after native login and human confirmation
  of the displayed identity. Existing or pending directories refuse.
- Caller selectors that would change auth/provider/storage/endpoint refuse;
  managed policy is preserved (no settings-source override, real HOME kept).
- Every file policy source native 2.1.322 reads on this platform is inspected
  per document: <config>/settings.json, managed-settings.json and the
  managed-settings.d/*.json drop-ins (WSL adds the Windows-side managed
  directory). Sources the harness cannot evaluate refuse: cached server-managed
  policy (remote-settings.json, policy-limits.json), policyHelper and
  policyHelpers (executables that compute policy), managed-mcp.json, macOS
  managed preferences and the WSL Windows registry. Keys that run a command,
  change auth or transport, or start a connection, upload, service or
  background work refuse in any document. Server-managed policy fetched during
  the run is detected afterwards by its cache file and voids the result; it
  cannot be prevented before it applies.
- Native runs in a fresh 0700 workspace, by default under a private
  XDG_RUNTIME_DIR; any workspace ancestor holding .git or .claude refuses,
  because native could take it as a project root with its own settings.
- The native child gets an allowlisted environment, closed inherited fds, its
  own session/process group, bounded output/time, and is reaped with its group.
  Limit: a descendant that calls setsid() leaves the group and is not reaped.
- Output is a sanitized allowlist: no token, raw record, whole metadata file
  or raw response is retained; stderr is counted and discarded.
- Auxiliary traffic is accounted by an optional loopback CONNECT tunnel that
  records host:port and byte counts only and refuses unlisted hosts. Limit: it
  sees only traffic that honors HTTPS_PROXY; direct egress is neither counted
  nor blocked.
"""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid

_HERE = Path(os.path.realpath(__file__)).parent


def _load(name):
    spec = importlib.util.spec_from_file_location("qc_" + name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bearer_mod = _load("bearer")
usage_mod = _load("usage")

NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
ROOT_NAME = "native-claude-accounts"
REGISTRATION = "account.json"
NATIVE_ARGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--safe-mode", "--no-session-persistence")
TOTAL_DEADLINE = 120.0
OPERATION_DEADLINE = 45.0
PERSISTENCE_WAIT = 5.0
CACHE_SHORTCUT_WAIT = 65.0
OUTPUT_LIMIT = 4 * 1024 * 1024
STDERR_LIMIT = 4 * 1024 * 1024
ENV_ALLOW = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")
TUNNEL_HOSTS = ("api.anthropic.com:443", "platform.claude.com:443")
SUBSCRIPTION_TYPES = ("free", "pro", "max", "team", "enterprise")
# Caller variables that change auth, provider, storage, endpoint, transport or
# native usage behavior. Present (even empty) means refuse, never strip.
REFUSED_ENV = tuple(bearer_mod.STORE_SELECTORS) + (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_CONFIG_DIR", "ANTHROPIC_PROFILE", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_ENTRYPOINT", "HTTPS_PROXY", "https_proxy",
    "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "NODE_EXTRA_CA_CERTS",
    "NODE_TLS_REJECT_UNAUTHORIZED", "SSL_CERT_FILE", "SSL_CERT_DIR", "CLAUDE_CODE_PROCESS_WRAPPER",
    "CLAUDE_CODE_HTTP_PROXY", "CLAUDE_CODE_HTTPS_PROXY", "ANTHROPIC_UNIX_SOCKET")
# Settings keys of native 2.1.322 (S, schema near offset 283926780). Present in
# any inspected document means refuse, whatever the value:
# - the credential helpers and auth pins that native's managedSourcesBehavior
#   description names, plus processWrapper and the XAA IdP login config;
SETTINGS_REFUSED = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh",
                    "otelHeadersHelper", "proxyAuthHelper", "processWrapper", "forceLoginGatewayUrl",
                    "gatewayInternalNetworks", "xaaIdp")
# - executables or payloads that compute managed settings nobody inspected
#   (native reads policyHelper from admin sources, offset about 284577561).
POLICY_HELPERS = ("policyHelper", "policyHelpers")
# Keys that run a command or start a connection, upload, service or background
# work. Refused unless null, false or empty. Native keeps managed hooks in safe
# mode (S, offset about 287982373); the rest are refused rather than traced.
SETTINGS_ACTIVE = ("hooks", "statusLine", "subagentStatusLine", "fileSuggestion", "sshConfigs", "sshHostCommands",
                   "managedMcpServers", "extraKnownMarketplaces", "additionalMarketplaces", "remoteControlAtStartup",
                   "autoUploadSessions", "daemonColdStart", "autoDreamEnabled", "forceRemoteSettingsRefresh")
# MCP server file the managed directory may hold (S); it can name programs.
MANAGED_MCP = "managed-mcp.json"
# Workspace ancestors with these entries could act as a project root.
PROJECT_MARKERS = (".git", ".claude")
# A settings `env` may set anything; refuse whole families, not only known names.
SETTINGS_ENV_REFUSED = re.compile(r"(?:ANTHROPIC_|CLAUDE_|AWS_|GOOGLE_|VERTEX_|CLOUD_ML_|NODE_|SSL_|BUN_|"
                                  r"NPM_CONFIG_).*|.*(?:PROXY|CERT|_CA_).*", re.IGNORECASE)
# File policy sources of native 2.1.322 (S): managed-settings.json plus the
# managed-settings.d drop-ins in each managed directory; WSL reads the
# Windows-side directory first.
MANAGED_DIRS = {"linux": ("/etc/claude-code",), "darwin": ("/Library/Application Support/ClaudeCode",),
                "wsl": ("/mnt/c/Program Files/ClaudeCode", "/etc/claude-code")}
# Signed server-managed policy caches in the config directory (S). Present
# means the account has server-managed policy the harness cannot evaluate.
SERVER_MANAGED = ("remote-settings.json", "policy-limits.json")
# OS policy sources the harness cannot read: refuse on those platforms.
UNINSPECTABLE_OS_SOURCES = {"darwin": "managed preferences", "wsl": "Windows policy registry"}


class HarnessRefused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class AccountPaths:
    __slots__ = ("secrets_dir", "name", "root", "account", "config", "registration")

    def __init__(self, secrets_dir, name):
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise HarnessRefused("invalid_account_name")
        try:
            bearer_mod.canonical_parts(secrets_dir)
        except bearer_mod.BearerRefused:
            raise HarnessRefused("secrets_dir_not_canonical") from None
        self.secrets_dir, self.name = secrets_dir, name
        self.root = secrets_dir + "/" + ROOT_NAME
        self.account = self.root + "/" + name
        self.config = self.account + "/config"  # exact spelling: no trailing slash
        self.registration = self.account + "/" + REGISTRATION


def _open_dir(path, uid, private_depth):
    try:
        return bearer_mod.open_account_directory(path, uid, private_depth)
    except bearer_mod.BearerRefused as error:
        raise HarnessRefused(error.reason) from None


def _write_exclusive(directory, name, document):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=directory)
    with os.fdopen(fd, "w") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def create_pending(paths, uid=None):
    """HLogin preparation: new pending registration, never overwrite or recover."""
    uid = os.getuid() if uid is None else uid
    old = os.umask(0o077)
    try:
        secrets = _open_dir(paths.secrets_dir, uid, 1)
        try:
            try:
                os.mkdir(ROOT_NAME, 0o700, dir_fd=secrets)
            except FileExistsError:
                pass
        finally:
            os.close(secrets)
        root = _open_dir(paths.root, uid, 1)
        try:
            try:
                os.mkdir(paths.name, 0o700, dir_fd=root)
            except FileExistsError:
                raise HarnessRefused("account_directory_exists") from None
        finally:
            os.close(root)
        account = _open_dir(paths.account, uid, 2)
        try:
            os.mkdir("config", 0o700, dir_fd=account)
            enrollment = str(uuid.uuid4())
            _write_exclusive(account, REGISTRATION, {
                "schema_version": 1, "mode": "native-login", "state": "pending",
                "enrollment_id": enrollment, "config_directory": paths.config, "identity": None})
            os.fsync(account)
        finally:
            os.close(account)
        return enrollment
    finally:
        os.umask(old)


def load_registration(paths, uid=None):
    uid = os.getuid() if uid is None else uid
    account = _open_dir(paths.account, uid, 2)
    try:
        try:
            fd = os.open(REGISTRATION, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=account)
        except OSError:
            raise HarnessRefused("registration_unusable") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1 or not 0 < info.st_size <= 4096):
                raise HarnessRefused("registration_not_private")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
    finally:
        os.close(account)
    try:
        record = json.loads(raw, object_pairs_hook=bearer_mod._unique_object)
    except (bearer_mod.BearerRefused, ValueError):
        raise HarnessRefused("registration_malformed") from None
    keys = {"schema_version", "mode", "state", "enrollment_id", "config_directory", "identity"}
    if not isinstance(record, dict) or set(record) != keys or record["schema_version"] != 1 \
            or record["mode"] != "native-login" or record["state"] not in ("pending", "ready"):
        raise HarnessRefused("registration_malformed")
    try:
        if str(uuid.UUID(record["enrollment_id"])) != record["enrollment_id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise HarnessRefused("registration_malformed") from None
    if record["config_directory"] != paths.config:
        raise HarnessRefused("registration_path_mismatch")
    identity = record["identity"]
    if record["state"] == "pending" and identity is not None:
        raise HarnessRefused("registration_malformed")
    if record["state"] == "ready":
        if (not isinstance(identity, dict) or set(identity) != {"account_uuid", "organization_uuid"}
                or not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", v) for v in identity.values())):
            raise HarnessRefused("registration_malformed")
    return record


def read_metadata(config_dir, uid=None):
    """Account-local .claude.json: only the two UUIDs and the usage cache stamp."""
    uid = os.getuid() if uid is None else uid
    directory = _open_dir(config_dir, uid, 3)
    try:
        try:
            fd = os.open(".claude.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=directory)
        except OSError:
            raise HarnessRefused("metadata_unusable") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022 \
                    or not 0 < info.st_size <= 16 * 1024 * 1024:
                raise HarnessRefused("metadata_not_private")
            chunks, total = [], 0
            while total <= info.st_size:
                chunk = os.read(fd, info.st_size + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            os.close(fd)
    finally:
        os.close(directory)
    try:
        document = json.loads(b"".join(chunks), object_pairs_hook=bearer_mod._unique_object)
        account = document.get("oauthAccount") or {}
        cache = document.get("cachedUsageUtilization") or {}
        result = {"account_uuid": account.get("accountUuid"), "organization_uuid": account.get("organizationUuid"),
                  "cache_fetched_at_ms": cache.get("fetchedAtMs"), "cache_account_uuid": cache.get("accountUuid")}
    except (bearer_mod.BearerRefused, ValueError, AttributeError, TypeError):
        raise HarnessRefused("metadata_malformed") from None
    finally:
        document = chunks = None
    if result["cache_fetched_at_ms"] is not None and type(result["cache_fetched_at_ms"]) is not int:
        result["cache_fetched_at_ms"] = None
    return result


def mark_ready(paths, confirmed_identity, uid=None):
    """After user-performed native login and human confirmation of the identity."""
    uid = os.getuid() if uid is None else uid
    record = load_registration(paths, uid)
    if record["state"] != "pending":
        raise HarnessRefused("registration_not_pending")
    local = read_metadata(paths.config, uid)
    observed = {"account_uuid": local["account_uuid"], "organization_uuid": local["organization_uuid"]}
    if observed != confirmed_identity:
        raise HarnessRefused("identity_not_confirmed")
    record.update(state="ready", identity=dict(confirmed_identity))
    account = _open_dir(paths.account, uid, 2)
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


def check_environment(caller_env):
    for name in REFUSED_ENV:
        if name in caller_env:
            raise HarnessRefused("conflicting_environment")
    if not caller_env.get("HOME", "").startswith("/"):
        raise HarnessRefused("home_missing")


def _active(value):
    """False only for an absent, null, false or empty setting."""
    return not (value is None or value is False or (isinstance(value, (dict, list, str)) and len(value) == 0))


def _present(path):
    """lstat-based existence (a dangling symlink counts); refuses if it cannot tell."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        raise HarnessRefused("policy_unreadable") from None
    return True


def check_policy(settings_paths, registration):
    """Inspect (never rewrite) each policy document for incompatible selectors.

    The checks are per document and refuse on any incompatible value, so the
    native merge order and composition mode cannot turn refused documents into
    an accepted effective policy.
    """
    for path in settings_paths:
        try:
            with open(path, "rb") as handle:
                raw = handle.read(1024 * 1024 + 1)
        except FileNotFoundError:
            continue
        except OSError:
            raise HarnessRefused("policy_unreadable") from None
        try:
            settings = json.loads(raw)
        except ValueError:
            raise HarnessRefused("policy_malformed") from None
        if not isinstance(settings, dict):
            raise HarnessRefused("policy_malformed")
        env = settings.get("env") or {}
        if not isinstance(env, dict) or any(name in REFUSED_ENV or SETTINGS_ENV_REFUSED.fullmatch(name)
                                            for name in env):
            raise HarnessRefused("policy_conflicting_env")
        if any(key in settings for key in POLICY_HELPERS):
            raise HarnessRefused("policy_helper_unevaluable")
        if any(key in settings for key in SETTINGS_REFUSED):
            raise HarnessRefused("policy_conflicting_auth")
        if any(_active(settings.get(key)) for key in SETTINGS_ACTIVE):
            raise HarnessRefused("policy_command_unevaluable")
        method = settings.get("forceLoginMethod")
        if method is not None and method != "claudeai":
            raise HarnessRefused("policy_login_method_incompatible")
        org = settings.get("forceLoginOrgUUID")
        if org is not None:
            allowed = org if isinstance(org, list) else [org]
            if registration["identity"]["organization_uuid"] not in allowed:
                raise HarnessRefused("policy_organization_mismatch")


def detect_platform(environ=None, proc_version="/proc/version"):
    """darwin, wsl or linux, with native's WSL test (WSL_DISTRO_NAME or /proc/version)."""
    environ = os.environ if environ is None else environ
    if sys.platform == "darwin":
        return "darwin"
    if environ.get("WSL_DISTRO_NAME"):
        return "wsl"
    try:
        with open(proc_version, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read(4096).lower()
    except OSError:
        return "linux"
    return "wsl" if "microsoft" in text or "wsl" in text else "linux"


def managed_documents(directory):
    """managed-settings.json, then managed-settings.d/*.json in native order.

    Native 2.1.322 (S) lists the drop-in directory, keeps regular files and
    symlinks named *.json that do not start with a dot, and sorts the names by
    UTF-16 code units. A missing directory is no source; any other listing
    error refuses.
    """
    documents = [directory + "/managed-settings.json"]
    dropin = directory + "/managed-settings.d"
    try:
        with os.scandir(dropin) as listing:
            names = [entry.name for entry in listing
                     if entry.name.endswith(".json") and not entry.name.startswith(".")
                     and (entry.is_symlink() or entry.is_file(follow_symlinks=False))]
    except (FileNotFoundError, NotADirectoryError):
        return documents
    except OSError:
        raise HarnessRefused("policy_unreadable") from None
    names.sort(key=lambda name: name.encode("utf-16-be", "surrogatepass"))
    return documents + [dropin + "/" + name for name in names]


def server_managed_present(config_dir):
    """True if any signed server-managed policy cache exists; refuses if it cannot tell."""
    return any(_present(config_dir + "/" + name) for name in SERVER_MANAGED)


def check_effective_policy(config_dir, registration, platform, managed_dirs=None):
    """Refuse unless every policy source native would apply can be inspected and is compatible.

    Returns the inspected document paths. Server-managed policy fetched during
    a run is outside this pre-spawn check (see run_n1).
    """
    if platform in UNINSPECTABLE_OS_SOURCES:
        raise HarnessRefused("policy_os_source_uninspectable")
    if platform not in MANAGED_DIRS:
        raise HarnessRefused("platform_unsupported")
    if server_managed_present(config_dir):
        raise HarnessRefused("policy_server_managed_present")
    documents = [config_dir + "/settings.json"]
    for directory in MANAGED_DIRS[platform] if managed_dirs is None else managed_dirs:
        if _present(directory + "/" + MANAGED_MCP):
            raise HarnessRefused("policy_managed_mcp_present")
        documents += managed_documents(directory)
    check_policy(documents, registration)
    return documents


def default_workspace_parent(caller_env, uid=None):
    """XDG_RUNTIME_DIR when it is this user's private directory, else None (system temp)."""
    uid = os.getuid() if uid is None else uid
    path = caller_env.get("XDG_RUNTIME_DIR", "")
    if not path.startswith("/"):
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISDIR(info.st_mode) and info.st_uid == uid and not stat.S_IMODE(info.st_mode) & 0o077:
        return path
    return None


def check_workspace_ancestors(workspace):
    """Refuse if any ancestor of the workspace holds .git or .claude (or cannot be checked)."""
    current = os.path.realpath(workspace)
    while True:
        parent = os.path.dirname(current)
        for marker in PROJECT_MARKERS:
            try:
                os.lstat(os.path.join(parent, marker))
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                raise HarnessRefused("workspace_unusable") from None
            raise HarnessRefused("workspace_ancestor_project")
        if parent == current:
            return
        current = parent


def build_child_env(caller_env, paths, proxy_port=None):
    env = {k: caller_env[k] for k in ENV_ALLOW if k in caller_env}
    env["CLAUDE_CONFIG_DIR"] = paths.config
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    if proxy_port is not None:
        env["HTTPS_PROXY"] = "http://127.0.0.1:%d" % proxy_port
    return env


class Tunnel:
    """Loopback CONNECT accounting: host:port and byte counts only; unlisted hosts refused.

    connector(host, port) opens the upstream socket; live use would connect to
    the real host (a network effect that the N1 permission must name).
    """

    def __init__(self, connector, allowed=TUNNEL_HOSTS, idle_timeout=30.0):
        self.connector, self.allowed, self.idle_timeout = connector, allowed, idle_timeout
        self.counts, self.refused, self.bytes_up, self.bytes_down = {}, 0, 0, 0
        self.lock = threading.Lock()
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.threads = []
        self.closing = False
        self.acceptor = threading.Thread(target=self._accept, daemon=True)

    def __enter__(self):
        self.acceptor.start()
        return self

    def __exit__(self, *exc):
        self.closing = True
        self.sock.close()
        for thread in list(self.threads):
            thread.join(timeout=2)

    def _accept(self):
        while not self.closing:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            thread = threading.Thread(target=self._one, args=(client,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def _one(self, client):
        upstream = None
        try:
            client.settimeout(5)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk or len(head) > 8192:
                    return
                head += chunk
            line = head.split(b"\r\n", 1)[0].decode("ascii", "replace").split()
            target = line[1] if len(line) == 3 and line[0] == "CONNECT" else None
            if target not in self.allowed:
                with self.lock:
                    self.refused += 1
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            host, port = target.rsplit(":", 1)
            upstream = self.connector(host, int(port))
            with self.lock:
                self.counts[target] = self.counts.get(target, 0) + 1
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self._pump(client, upstream)
        except OSError:
            return
        finally:
            client.close()
            if upstream is not None:
                upstream.close()

    def _pump(self, client, upstream):
        with selectors.DefaultSelector() as selector:
            selector.register(client, selectors.EVENT_READ, (client, upstream, "up"))
            selector.register(upstream, selectors.EVENT_READ, (upstream, client, "down"))
            while not self.closing:
                events = selector.select(self.idle_timeout)
                if not events:
                    return
                for key, _ in events:
                    source, sink, direction = key.data
                    data = source.recv(65536)
                    if not data:
                        return
                    sink.sendall(data)
                    with self.lock:
                        if direction == "up":
                            self.bytes_up += len(data)
                        else:
                            self.bytes_down += len(data)

    def summary(self):
        with self.lock:
            return {"connect": dict(sorted(self.counts.items())), "refused": self.refused,
                    "bytes_up": self.bytes_up, "bytes_down": self.bytes_down}


def _kill_group(child):
    killed = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
            killed = True
        except ProcessLookupError:
            break
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
    if child.poll() is None:
        child.kill()
    child.wait(timeout=5)
    try:
        os.killpg(child.pid, 0)
        group_empty = False
    except ProcessLookupError:
        group_empty = True
    return killed, group_empty


class _Native:
    """One owned native child speaking stream-json control requests."""

    def __init__(self, binary, env, cwd, stderr_path, monotonic):
        self.monotonic = monotonic
        self.stderr = open(stderr_path, "wb")
        self.child = subprocess.Popen([binary, *NATIVE_ARGS], env=env, cwd=cwd, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=self.stderr, close_fds=True,
                                      start_new_session=True)
        os.set_blocking(self.child.stdout.fileno(), False)
        self.buffer, self.read_total = bytearray(), 0
        self.events = {"control_ok": 0, "control_error": 0, "model": 0, "other": 0}

    def request(self, request_id, body, deadline):
        line = {"type": "control_request", "request_id": request_id, "request": body}
        self.child.stdin.write((json.dumps(line) + "\n").encode())
        self.child.stdin.flush()
        with selectors.DefaultSelector() as selector:
            selector.register(self.child.stdout, selectors.EVENT_READ)
            while True:
                left = deadline - self.monotonic()
                if left <= 0:
                    raise HarnessRefused("operation_deadline")
                for _key, _ in selector.select(min(left, 0.2)):
                    chunk = os.read(self.child.stdout.fileno(), 65536)
                    if not chunk:
                        raise HarnessRefused("native_exited_early")
                    self.read_total += len(chunk)
                    if self.read_total > OUTPUT_LIMIT:
                        raise HarnessRefused("output_limit")
                    self.buffer.extend(chunk)
                if os.fstat(self.stderr.fileno()).st_size > STDERR_LIMIT:
                    raise HarnessRefused("stderr_limit")
                while b"\n" in self.buffer:
                    raw, _, rest = self.buffer.partition(b"\n")
                    self.buffer = bytearray(rest)
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        raise HarnessRefused("protocol_malformed") from None
                    kind = event.get("type") if isinstance(event, dict) else None
                    if kind in ("assistant", "user", "stream_event"):
                        self.events["model"] += 1
                        raise HarnessRefused("model_event")
                    if kind == "control_response":
                        response = event.get("response")
                        if not isinstance(response, dict):
                            raise HarnessRefused("protocol_malformed")
                        if response.get("request_id") != request_id:
                            self.events["other"] += 1
                            continue
                        if response.get("subtype") != "success":
                            self.events["control_error"] += 1
                            raise HarnessRefused("control_error")
                        reply = response.get("response")
                        reply = {} if reply is None else reply
                        if not isinstance(reply, dict):
                            raise HarnessRefused("protocol_malformed")
                        self.events["control_ok"] += 1
                        return reply
                    self.events["other"] += 1

    def close(self):
        try:
            if not self.child.stdin.closed:
                self.child.stdin.close()
        except OSError:
            pass
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        killed, empty = _kill_group(self.child)
        self.child.stdout.close()
        size = os.fstat(self.stderr.fileno()).st_size
        self.stderr.close()
        return {"exit": self.child.returncode, "group_signalled": killed, "group_empty": empty,
                "stderr_bytes": size}


def _sanitize_reply(reply, received_wall_ms):
    rate_limits = reply.get("rate_limits")
    session = reply.get("session")
    session = {} if session is None else session
    if not isinstance(session, dict):
        raise HarnessRefused("protocol_malformed")
    out = {"rate_limits_available": reply.get("rate_limits_available") is True,
           "subscription_type": reply.get("subscription_type") if reply.get("subscription_type")
           in SUBSCRIPTION_TYPES else None,
           "cost_zero": session.get("total_cost_usd") == 0 and session.get("total_api_duration_ms") == 0
           and session.get("model_usage") == {}}
    if isinstance(rate_limits, dict):
        observation = usage_mod.normalize(200, "application/json", json.dumps(rate_limits).encode(),
                                          started_wall_ms=received_wall_ms, received_wall_ms=received_wall_ms,
                                          received_monotonic=0.0, source="native-control")
        out.update(windows={k: v.as_dict() for k, v in sorted(observation.windows.items())},
                   window_states=dict(sorted(observation.window_states.items())),
                   other_families=list(observation.other_families))
    else:
        out.update(windows={}, window_states={"five_hour": "missing", "seven_day": "missing"}, other_families=[])
    return out


def run_n1(binary, paths, caller_env, *, managed_dirs=None, platform=None, allow_requery=False,
           tunnel_connector=None, workspace_parent=None, monotonic=time.monotonic,
           wall_ms=lambda: int(time.time() * 1000), sleep=time.sleep, deadlines=None):
    """One bounded inference-free quota observation in the registered directory.

    Returns a sanitized dict; never raises for expected refusals. A
    server-managed policy cache that appears during the run voids the result
    (server_policy_fetched) and drops its replies and cache fields: native
    already applied that policy.
    """
    limits = {"total": TOTAL_DEADLINE, "operation": OPERATION_DEADLINE, "persistence": PERSISTENCE_WAIT,
              "shortcut": CACHE_SHORTCUT_WAIT}
    limits.update(deadlines or {})
    start = monotonic()
    result = {"schema": "qc-n1-result-1", "classification": "refused", "reason": None,
              "queries": 0, "model_events": 0}
    workspace = native = tunnel = None
    try:
        registration = load_registration(paths)
        if registration["state"] != "ready":
            raise HarnessRefused("registration_pending")
        check_environment(caller_env)
        result["policy_documents"] = len(check_effective_policy(
            paths.config, registration, detect_platform() if platform is None else platform, managed_dirs))
        if not os.path.isabs(binary) or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
            raise HarnessRefused("native_binary_unusable")
        with open(os.path.realpath(binary), "rb") as handle:
            result["native_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
        expected = registration["identity"]
        before = read_metadata(paths.config)
        result["identity_matches_before"] = (before["account_uuid"], before["organization_uuid"]) == (
            expected["account_uuid"], expected["organization_uuid"])
        if not result["identity_matches_before"]:
            raise HarnessRefused("identity_mismatch_before")
        if tunnel_connector is not None:
            tunnel = Tunnel(tunnel_connector).__enter__()
        if workspace_parent is None:
            workspace_parent = default_workspace_parent(caller_env)
        workspace = tempfile.mkdtemp(prefix="quota-qc-n1-", dir=workspace_parent)
        check_workspace_ancestors(workspace)
        native = _Native(binary, build_child_env(caller_env, paths, tunnel.port if tunnel else None),
                         workspace, os.path.join(workspace, ".stderr"), monotonic)

        def deadline(seconds):
            return min(start + limits["total"], monotonic() + seconds)

        native.request("init", {"subtype": "initialize", "hooks": {}}, deadline(limits["operation"]))
        replies, stamps = [], [before["cache_fetched_at_ms"]]
        while True:
            reply = native.request("quota-%d" % result["queries"], {"subtype": "get_usage", "skip_behaviors": True},
                                   deadline(limits["operation"]))
            result["queries"] += 1
            replies.append(_sanitize_reply(reply, wall_ms()))
            waited_until = deadline(limits["persistence"])
            stamp = read_metadata(paths.config)["cache_fetched_at_ms"]
            while (stamp is None or stamp == stamps[-1]) and monotonic() < waited_until:
                sleep(0.1)
                stamp = read_metadata(paths.config)["cache_fetched_at_ms"]
            advanced = stamp is not None and (stamps[0] is None or stamp > stamps[0])
            stamps.append(stamp)
            if advanced or not allow_requery or result["queries"] >= 2:
                break
            # Explicitly approved bounded wait for native's under-60-second cache shortcut.
            remaining = start + limits["total"] - monotonic() - limits["operation"] - limits["persistence"]
            if remaining < limits["shortcut"]:
                break
            sleep(limits["shortcut"])
        after = read_metadata(paths.config)
        result["identity_matches_after"] = (after["account_uuid"], after["organization_uuid"]) == (
            expected["account_uuid"], expected["organization_uuid"])
        result["cache"] = {"advanced": advanced,
                           "advance_ms": (stamp - stamps[0]) if advanced and stamps[0] is not None else None,
                           "account_matches": after["cache_account_uuid"] == expected["account_uuid"]}
        result["replies"] = replies
        last = replies[-1]
        complete = all(last["window_states"].get(name) == "valid" for name in ("five_hour", "seven_day"))
        if not result["identity_matches_after"]:
            result.update(classification="refused", reason="identity_changed")
        elif not last["rate_limits_available"] or not last["windows"]:
            result.update(classification="unavailable", reason="no_windows")
        elif not advanced or not result["cache"]["account_matches"]:
            result.update(classification="inconclusive", reason="cache_not_advanced")
        elif not complete or not all(r["cost_zero"] for r in replies):
            result.update(classification="unavailable", reason="windows_incomplete")
        else:
            result.update(classification="fresh_windows", reason=None)
    except HarnessRefused as error:
        result["reason"] = error.reason
        result["classification"] = "failed" if native is not None else "refused"
    finally:
        if native is not None:
            result["native"] = native.close()
            result["model_events"] = native.events["model"]
            result["events"] = dict(native.events)
            try:
                written = server_managed_present(paths.config)
            except HarnessRefused:
                written = None  # cannot tell: treat as written
            result["server_policy_written"] = written
            if written is not False and result["classification"] != "failed":
                result.update(classification="refused", reason="server_policy_fetched")
            if written is not False:
                # Observed under policy nobody evaluated: retain no quota data.
                result.pop("replies", None)
                result.pop("cache", None)
        if tunnel is not None:
            tunnel.__exit__(None, None, None)
            result["traffic"] = tunnel.summary()
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
        result["elapsed_s"] = round(monotonic() - start, 3)
    return result
