"""Fixed account views over shared Codex state; private credentials stay in secrets."""

import base64
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat


# Share only audited user/state paths. Remote plugin and cloud caches are private.
SHARED_DIRS = (
    "sessions", "archived_sessions", "rollout-migrations", "thread-writer-locks",
    "skills", "rules", "agents", "memories", "memories_v2", "themes", "worktrees",
)
SHARED_FILES = ("config.toml", "history.jsonl", "session_index.jsonl")
OPTIONAL_FILES = ("AGENTS.md", "AGENTS.override.md", "hooks.json", "environments.toml", "mcp.json")
ROOT_PATH_KEYS = {
    "model_instructions_file", "model_catalog_json", "experimental_compact_prompt_file",
    "js_repl_node_path", "js_repl_node_module_dirs", "sqlite_home", "log_dir",
}
TLS_PATH_KEYS = {"ca-certificate", "client-certificate", "client-private-key"}
NETWORK_PATH_KEYS = {"certificate", "private_key", "tls_certificate", "tls_private_key", "tls_ca_certificate"}


def validate_paths(guard, value, prefix=()):
    """Fail closed for the audited schema's path fields in shared user config."""
    if isinstance(value, dict):
        for key, item in value.items():
            field = (*prefix, key)
            scope = prefix[2:] if prefix[:1] == ("profiles",) and len(prefix) >= 2 else prefix
            path_field = (
                (not scope and key in ROOT_PATH_KEYS)
                or (scope[:1] == ("agents",) and key == "config_file")
                or (scope[:2] == ("skills", "config") and key == "path")
                or (scope == ("sandbox_workspace_write",) and key == "writable_roots")
                or (scope[:1] == ("otel",) and "tls" in scope and key in TLS_PATH_KEYS)
                or ("network" in scope and key in NETWORK_PATH_KEYS)
            )
            if path_field:
                paths = item if isinstance(item, list) else [item]
                for path in paths:
                    if isinstance(path, str) and not Path(path).is_absolute():
                        guard.fail(f"shared configuration path {'.'.join(field)} must be absolute; configuration was not rewritten.")
            validate_paths(guard, item, field)
    elif isinstance(value, list):
        for item in value:
            validate_paths(guard, item, (*prefix, "[]"))


def shared_sqlite(guard, root, cwd):
    result = root
    config = root / "config.toml"
    if config.exists():
        with config.open("rb") as stream:
            data = guard.tomllib.load(stream)
        validate_paths(guard, data)
        result = Path(data.get("sqlite_home", str(root)))
    inherited = os.environ.get("CODEX_SQLITE_HOME")
    if inherited and Path(os.path.abspath(inherited)) != result:
        guard.fail("CODEX_SQLITE_HOME conflicts with shared SQLite storage (value hidden).")
    for parent in (cwd, *cwd.parents):
        path = parent / ".codex/config.toml"
        if path.exists() and path != config:
            with path.open("rb") as stream:
                data = guard.tomllib.load(stream)
            if "sqlite_home" in data and Path(data["sqlite_home"]) != result:
                guard.fail("project configuration conflicts with shared SQLite storage; no launch performed.")
    return result


def identity(raw):
    data = json.loads(raw)
    if data.get("tokens") is None:
        return ("apikey", data.get("OPENAI_API_KEY"))
    token = data["tokens"]["id_token"].split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    auth = payload.get("https://api.openai.com/auth", {})
    return ("chatgpt", payload.get("sub"), auth.get("chatgpt_user_id"))


def same_identity(left, right):
    a, b = identity(left), identity(right)
    if a[0] != b[0]:
        return False
    pairs = [(x, y) for x, y in zip(a[1:], b[1:]) if x and y]
    return bool(pairs) and all(x == y for x, y in pairs)


class Layout:
    def __init__(self, guard, account):
        self.g = guard
        self.name = account
        self.root = Path(os.environ["HOME"]) / ".codex"
        self.secrets = self.root.parent / ".secrets"
        self.home = self.root / "accounts" / account
        self.auth = self.home / "auth.json"
        self.target = self.secrets / f"codex-{account}.json"
        self.marker = self.root / "codex-as.layout.json"
        self.fd = None

    def private_file(self, path):
        return self.g.private_metadata(path)

    def setup_pending(self):
        path = self.root / "accounts/.locks" / f".setup-{self.name}.json"
        if not os.path.lexists(path):
            return False
        data = json.loads(self.private_file(path), object_pairs_hook=self.g.unique_keys)
        if data != {"version": 2, "account": self.name}:
            self.g.fail("unrecognized pending account setup; preserve account files.")
        return True

    def finish_setup(self):
        if self.setup_pending():
            (self.root / "accounts/.locks" / f".setup-{self.name}.json").unlink()

    def root_check(self, initializing=False):
        self.g.directory(self.root.parent)
        if os.path.lexists(self.root):
            self.g.directory(self.root, initializing=initializing)
        if os.path.lexists(self.secrets):
            self.g.directory(self.secrets, private=True)

    def layout_check(self):
        self.root_check()
        if not os.path.lexists(self.marker):
            if os.path.lexists(self.root / "auth.json") or os.path.lexists(self.root / "codex-as.initialized"):
                self.g.fail("legacy root authentication needs explicit migration: codex-as --migrate CURRENT_ACCOUNT; close existing clients first.")
            return False
        data = json.loads(self.private_file(self.marker), object_pairs_hook=self.g.unique_keys)
        if (data.get("version") != 2 or not isinstance(data.get("default_account"), str)
                or not self.g.re.fullmatch(r"[A-Za-z0-9_-]+", data["default_account"])):
            self.g.fail("unrecognized account layout; preserve authentication and recover manually.")
        self.check_link(self.root / "auth.json", self.secrets / f"codex-{data['default_account']}.json")
        return True

    def acquire(self, exclusive=False):
        self.root.mkdir(mode=0o700, exist_ok=True)
        for path in (self.root / "accounts", self.root / "accounts/.locks"):
            path.mkdir(mode=0o700, exist_ok=True)
            self.g.directory(path, private=True)
        path = self.root / "accounts/.locks" / f"{self.name}.lock"
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        info = os.fstat(self.fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            self.g.fail("unsafe account lease; no launch performed.")
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if exclusive:
                self.g.fail("account has active clients; close them before migration/recovery.")
            fcntl.flock(self.fd, fcntl.LOCK_SH)
            return False

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    @contextmanager
    def setup_lock(self):
        path = self.root / "accounts/.locks/.layout.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                self.g.fail("unsafe shared layout lease; no initialization performed.")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def replace_link(self, path, target):
        staged = path.parent / (".codex-as-link-" + secrets.token_hex(12))
        os.symlink(str(target), staged)
        try:
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)

    def replace_bytes(self, path, raw):
        staged = path.parent / (".codex-as-import-" + secrets.token_hex(12))
        self.g.exclusive(staged, raw)
        try:
            os.replace(staged, path)
        finally:
            staged.unlink(missing_ok=True)

    def backup(self, raw):
        self.g.exclusive(self.secrets / (".codex-as-backup-" + secrets.token_hex(12)), raw)

    def check_link(self, path, target):
        if (not path.is_symlink() or path.lstat().st_uid != os.getuid()
                or Path(os.readlink(path)) != target):
            self.g.fail("managed account link is missing/replaced; preserve it and recover before launch.")

    def init_home(self):
        marker = self.home / "codex-as.initialized"
        pending = self.setup_pending()
        marked = os.path.lexists(marker)
        if os.path.lexists(self.home):
            self.g.directory(self.home, private=True)
            if not marked and not pending:
                self.g.fail("unmarked account directory already exists; review it before initialization.")
        if marked and self.private_file(marker) != b"codex-as v2\n":
            self.g.fail("unrecognized account initialization marker; preserve account state.")
        if not marked and not pending:
            path = self.root / "accounts/.locks" / f".setup-{self.name}.json"
            self.replace_bytes(path, json.dumps({"version": 2, "account": self.name}).encode() + b"\n")
        self.home.mkdir(mode=0o700, exist_ok=True)
        self.g.directory(self.home, private=True)
        for name in SHARED_DIRS:
            target = self.root / name
            if not os.path.lexists(target):
                target.mkdir(mode=0o700, exist_ok=True)
            if not target.is_dir():
                self.g.fail("shared state directory has an unexpected file type; preserve it.")
            self.add_link(self.home / name, target)
        for name in SHARED_FILES:
            target = self.root / name
            if not os.path.lexists(target):
                try:
                    self.g.exclusive(target, b"")
                except FileExistsError:
                    pass  # A native root client may have just created it.
            if not target.is_file():
                self.g.fail("shared configuration/history has an unexpected file type; preserve it.")
            self.add_link(self.home / name, target)
        for name in OPTIONAL_FILES:
            self.add_link(self.home / name, self.root / name)
        for path in (self.root / ".tmp", self.home / ".tmp"):
            path.mkdir(mode=0o700, exist_ok=True)
            self.g.directory(path)
        target = self.root / ".tmp/rollout-maintenance.lock"
        if not os.path.lexists(target):
            try:
                self.g.exclusive(target, b"")
            except FileExistsError:
                pass
        self.add_link(self.home / ".tmp/rollout-maintenance.lock", target)
        if not os.path.lexists(marker):
            self.replace_bytes(marker, b"codex-as v2\n")

    def add_link(self, path, target):
        if not os.path.lexists(path):
            os.symlink(str(target), path)
        else:
            self.check_link(path, target)

    def validate(self):
        self.g.directory(self.home, private=True)
        if self.private_file(self.home / "codex-as.initialized") != b"codex-as v2\n":
            self.g.fail("unrecognized account initialization marker.")
        self.check_link(self.auth, self.target)
        for name in (*SHARED_DIRS, *SHARED_FILES, *OPTIONAL_FILES):
            self.check_link(self.home / name, self.root / name)
        self.check_link(self.home / ".tmp/rollout-maintenance.lock", self.root / ".tmp/rollout-maintenance.lock")
        return self.g.credential(self.target)

    def repair(self, baseline=None):
        if self.auth.is_symlink():
            self.check_link(self.auth, self.target)
            return
        if not os.path.lexists(self.auth):
            self.g.fail("account auth.json is absent; use codex-as --recover NAME while the account is idle.")
        latest = self.g.credential(self.auth)
        canonical = self.g.credential(self.target)
        if latest != canonical:
            if baseline is None or canonical != baseline or not same_identity(latest, baseline):
                self.g.fail("account auth.json and secrets diverged; preserve both and use codex-as --recover NAME while idle.")
            self.backup(latest)
            self.backup(canonical)
            if self.g.credential(self.auth) != latest or self.g.credential(self.target) != canonical:
                self.g.fail("credentials changed during repair; backups preserved.")
            self.replace_bytes(self.target, latest)
        if self.g.credential(self.auth) != latest:
            self.g.fail("account authentication changed during repair; preserve it.")
        self.replace_link(self.auth, self.target)

    def prepare(self):
        with self.setup_lock():
            # Another account may have initialized the default while we waited.
            self.prepare_locked(self.layout_check())

    def prepare_locked(self, initialized):
        self.init_home()
        if not os.path.lexists(self.auth) and self.setup_pending():
            self.add_link(self.auth, self.target)
        else:
            self.repair()
        if not initialized:
            self.replace_bytes(self.marker, json.dumps({"version": 2, "default_account": self.name}).encode() + b"\n")
            self.replace_link(self.root / "auth.json", self.target)
        self.validate()
        self.finish_setup()

    def migrate(self):
        with self.setup_lock():
            self.migrate_locked()

    def migrate_locked(self):
        self.root_check(initializing=True)
        if os.path.lexists(self.marker):
            marker = json.loads(self.private_file(self.marker), object_pairs_hook=self.g.unique_keys)
            if (marker.get("version") != 2 or not isinstance(marker.get("default_account"), str)
                    or not self.g.re.fullmatch(r"[A-Za-z0-9_-]+", marker["default_account"])):
                self.g.fail("unrecognized existing layout marker; preserve it before migration.")
        auth = self.root / "auth.json"
        if not os.path.lexists(auth) and os.path.lexists(self.marker):
            marker = json.loads(self.private_file(self.marker), object_pairs_hook=self.g.unique_keys)
            if marker != {"version": 2, "default_account": self.name}:
                self.g.fail("missing root authentication has an unrecognized layout; preserve it.")
            current = self.g.credential(self.target)
        elif auth.is_symlink():
            self.check_link(auth, self.target)
            current = self.g.credential(self.target)
        else:
            current = self.g.credential(auth)
        old = self.g.credential(self.target) if os.path.lexists(self.target) else None
        if old is not None and current != old and not same_identity(current, old):
            self.g.fail("root authentication does not match the selected user's identity; preserve both files.")
        legacy = self.root / "codex-as.initialized"
        if os.path.lexists(legacy) and self.private_file(legacy) != b"codex-as v1\n":
            self.g.fail("unrecognized legacy initialization marker; preserve current authentication.")
        temporary = self.root / ".tmp"
        if os.path.lexists(temporary):
            self.g.directory(temporary, initializing=True)
        tty = self.g.terminal()
        try:
            self.g.confirm(tty, f"Migrate shared state; root codex stays on {self.name}. Close ALL root/desktop/IDE clients first; backup both credential files")
            if os.path.lexists(temporary):
                self.g.directory(temporary, initializing=True)
                os.chmod(temporary, 0o700)
            self.secrets.mkdir(mode=0o700, exist_ok=True)
            self.g.directory(self.secrets, private=True)
            self.backup(current)
            if old is not None:
                self.backup(old)
            if (os.path.lexists(auth) and not auth.is_symlink() and self.g.credential(auth) != current) or (
                    old is not None and self.g.credential(self.target) != old):
                self.g.fail("credentials changed during migration; backups preserved.")
            if old is None:
                self.g.exclusive(self.target, current)
            elif old != current:
                self.replace_bytes(self.target, current)
            os.chmod(self.root, 0o700)
            # Root marker is written first; interrupted migration remains explicit.
            data = json.dumps({"version": 2, "default_account": self.name}).encode() + b"\n"
            self.replace_bytes(self.marker, data)
            self.replace_link(auth, self.target)
            self.init_home()
            if not os.path.lexists(self.auth):
                self.add_link(self.auth, self.target)
            self.validate()
            self.finish_setup()
            tty.write(f"Shared account layout initialized; default account {self.name}.\n", "32")
        finally:
            tty.close()

    def recover(self):
        self.g.directory(self.home, private=True)
        canonical = self.g.credential(self.target)
        local = self.g.credential(self.auth) if os.path.lexists(self.auth) and not self.auth.is_symlink() else None
        if self.auth.is_symlink():
            self.check_link(self.auth, self.target)
            return
        if local is not None and local != canonical and not same_identity(local, canonical):
            self.g.fail("account authentication identity mismatch; no recovery performed.")
        tty = self.g.terminal()
        try:
            tty.write("Recovery source: 1 keep secrets; 2 import account auth.json; empty cancels: ", "33")
            choice = tty.readline().strip()
            if choice not in {"1", "2"} or (choice == "2" and local is None):
                self.g.fail("cancelled; credentials preserved.")
            self.g.confirm(tty, "Close ALL clients for this account; back up both files and restore its fixed link")
            self.backup(canonical)
            if local is not None:
                self.backup(local)
            if self.g.credential(self.target) != canonical or (
                    local is not None and self.g.credential(self.auth) != local):
                self.g.fail("credentials changed during recovery; backups preserved.")
            if choice == "2" and local != canonical:
                self.replace_bytes(self.target, local)
            self.replace_link(self.auth, self.target)
            self.validate()
            tty.write(f"Account {self.name} recovered.\n", "32")
        finally:
            tty.close()
