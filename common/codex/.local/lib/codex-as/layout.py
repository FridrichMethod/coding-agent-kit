"""Fixed account views over shared Codex state; private credentials stay in secrets.

The root ~/.codex/auth.json belongs to native clients and is never read or changed.
"""

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


def shared_sqlite(guard, root, cwd, profile=None):
    result = root
    config = root / "config.toml"
    if config.exists():
        with config.open("rb") as stream:
            data = guard.tomllib.load(stream)
        validate_paths(guard, data)
        result = Path(data.get("sqlite_home", str(root)))
    # A -p profile layers on the user config from the same directory: same path rules.
    layered = root / (profile + ".config.toml") if profile is not None else None
    if layered is not None and layered.exists():
        with layered.open("rb") as stream:
            data = guard.tomllib.load(stream)
        validate_paths(guard, data)
        if "sqlite_home" in data and Path(data["sqlite_home"]) != result:
            guard.fail("the selected profile conflicts with shared SQLite storage; no launch performed.")
    inherited = os.environ.get("CODEX_SQLITE_HOME")
    if inherited and Path(os.path.abspath(inherited)) != result:
        guard.fail("CODEX_SQLITE_HOME conflicts with shared SQLite storage (value hidden).")
    for parent in {*(cwd, *cwd.parents), *(cwd.resolve(), *cwd.resolve().parents)}:
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

    def root_check(self):
        self.g.directory(self.root.parent)
        if os.path.lexists(self.root):
            self.g.directory(self.root)
        if os.path.lexists(self.secrets):
            self.g.directory(self.secrets, private=True)

    def acquire(self):
        """Exclusive when no other client of this account runs, otherwise shared."""
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
            self.g.fail(f"managed link {path} is missing/replaced; preserve it and restore the link before launch.")

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

    def link_profile(self, profile):
        """Show ~/.codex/PROFILE.config.toml in the account home, where native -p reads it."""
        target = self.root / (profile + ".config.toml")
        if os.path.exists(target):
            try:
                self.add_link(self.home / target.name, target)
            except FileExistsError:
                self.check_link(self.home / target.name, target)  # another launch just made it

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
            self.g.fail(f"account auth.json is absent. A logout inside Codex removes it and revokes the tokens in "
                        f"{self.target}: move that file aside under a name that does not match codex-NAME.json and "
                        f"run codex-as {self.name} to log in again. Otherwise, while the account is idle, run: "
                        f"ln -s {self.target} {self.auth}")
        latest = self.g.credential(self.auth)
        canonical = self.g.credential(self.target)
        if latest != canonical:
            if baseline is None or canonical != baseline or not same_identity(latest, baseline):
                self.g.fail(f"account auth.json and secrets diverged; both kept. While the account is idle, back up "
                            f"both, keep the wanted copy at {self.target}, then run: ln -sfn {self.target} {self.auth}")
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
            self.init_home()
            if not os.path.lexists(self.auth) and self.setup_pending():
                self.add_link(self.auth, self.target)
            else:
                self.repair()
            self.validate()
            self.finish_setup()
