"""Small stdlib guard for the executable shell command; never rewrites JSON."""

import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import stat
import shutil
import subprocess
import sys
import tempfile


def styled(text, style, fd):
    """Use the same auto/always/never policy as lib/terminal.sh per destination."""
    mode = os.environ.get("DOTFILES_COLOR", "auto")
    enabled = (not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
               and (mode == "always" or (mode != "never" and os.isatty(fd))))
    return f"\033[{style}m{text}\033[0m" if enabled and text.strip() else text


try:
    import tomllib
except ImportError:
    print(styled("codex-as: Python 3.11+ is required (no dependencies installed).", "31", 2), file=sys.stderr)
    sys.exit(1)


def fail(message):
    raise ValueError(message)


def arguments(args):
    # A bounded guard, not a replacement for clap. Values/prompts are never
    # interpreted as login/logout. Options after a prompt still need checking.
    values = {
        "-c", "--config", "-m", "--model", "-i", "--image", "-C", "--cd",
        "-s", "--sandbox", "-a", "--ask-for-approval", "--add-dir", "--enable",
        "--disable", "--output-schema", "--color", "-o", "--output-last-message",
        "--thread-source", "--cyber-access-program",
    }
    switches = {
        "--strict-config", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust", "--worktree", "--search", "--no-alt-screen",
        "--no-daemon", "-h", "--help", "-V", "--version", "--json", "--last",
        "--all", "--include-non-interactive", "--skip-git-repo-check", "--ephemeral",
    }
    commands = {
        "agents", "login", "logout", "mcp", "plugin", "app-server", "remote-control",
        "completion", "update", "doctor", "sandbox", "debug", "apply", "a", "queue",
        "archive", "delete", "migrate-rollouts", "unarchive", "cloud", "exec-server",
        "features", "help", "review",
        "app", "cloud-tasks", "tcp-tunnel", "execpolicy", "responses-api-proxy", "stdio-to-uds",
    }
    mode, root, cwd = "interactive", True, Path.cwd()
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            break
        flag, sep, value = arg.partition("=")
        if flag in values:
            if not sep:
                if i == len(args):
                    fail("option requires a value; no account switch performed.")
                value = args[i]
                i += 1
        elif arg.startswith("-c") and not arg.startswith("--"):
            flag, value = "-c", arg[2:]
        elif arg in switches:
            continue
        elif arg.startswith("-") and arg != "-":
            fail("unsupported option; profiles, remote/provider/auth overrides are refused.")
        else:
            if root:
                if arg in {"exec", "e"}:
                    mode = "exec"
                elif arg in commands:
                    fail("unsupported subcommand; login/logout must run outside codex-as.")
                root = False
            continue
        if flag in {"-c", "--config"}:
            key = value.partition("=")[0]
            if re.search(r"auth|login|provider|base_url|workspace_id|remote|api_key|access_token", key, re.I):
                fail("authentication/provider configuration overrides are refused.")
        elif flag in {"-C", "--cd"}:
            cwd = Path(value).expanduser().absolute()
        elif flag in {"--enable", "--disable"} and re.search(r"auth|login|provider|remote|identity", value, re.I):
            fail("authentication/provider feature overrides are refused (conservative local guard).")
    return mode, cwd


def directory(path, private=False, initializing=False):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        fail("home, .codex and .secrets must be owned directories, never symlinks.")
    if info.st_mode & 0o022 and not initializing:
        fail("home/.codex directory is group/other writable; review and restrict it first.")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        fail(".secrets must have permissions 0700; review it before changing permissions.")


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("credential JSON contains duplicate fields.")
        result[key] = value
    return result


def credential(path):
    # O_NOFOLLOW plus fstat avoids following a swapped symlink and checks the
    # actual opened inode. No chmod, writes, partial copies or token output.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            fail("credential must be an owned regular file with exactly one hard link.")
        if stat.S_IMODE(info.st_mode) != 0o600:
            fail("credential permissions must be 0600; no chmod was performed.")
        raw = stream.read()
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys)
    if not isinstance(data, dict):
        fail("credential JSON must be a complete file login object.")
    tokens = data.get("tokens")
    key = data.get("OPENAI_API_KEY")
    if any(data.get(k) is not None for k in ("personal_access_token", "bedrock_api_key", "bedrock_access_keys", "agent_identity")):
        fail("credential contains an alternative authentication source; review it first.")
    nonempty = lambda item: isinstance(item, str) and bool(item.strip())
    chatgpt = (
        isinstance(tokens, dict)
        and all(nonempty(tokens.get(k)) for k in ("id_token", "access_token", "refresh_token", "account_id"))
        and data.get("auth_mode") in (None, "chatgpt")
        and key is None
    )
    apikey = nonempty(key) and not tokens and data.get("auth_mode") in (None, "apikey")
    if not (chatgpt or apikey):
        fail("credential has no complete ChatGPT tokens or file API-key login; not server validation.")
    if chatgpt:
        refreshed = data.get("last_refresh")
        if not isinstance(refreshed, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", refreshed):
            fail("ChatGPT credential requires a valid last_refresh timestamp.")
        try:
            datetime.fromisoformat(refreshed)
            parts = tokens["id_token"].split(".")
            if len(parts) != 3 or not all(parts) or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]):
                fail("invalid ID token envelope.")
            payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
            if not isinstance(json.loads(payload), dict):
                fail("invalid ID token payload.")
        except (ValueError, UnicodeError):
            fail("ChatGPT timestamp/ID-token structure is invalid; no signature or server validation performed.")
    return raw


def accounts():
    """Metadata only: completion must never open credentials or start a CLI."""
    root = Path(os.environ["HOME"]) / ".secrets"
    if not os.path.lexists(root):
        return []
    directory(root, private=True)
    names = []
    for path in root.iterdir():
        match = re.fullmatch(r"codex-([A-Za-z0-9_-]+)\.json", path.name)
        info = path.lstat()
        if (match and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600):
            names.append(match[1])
    return sorted(names)


class Terminal:
    def __init__(self):
        self.fd = os.open("/dev/tty", os.O_RDWR)

    def fileno(self):
        return self.fd

    def write(self, text, style="36"):
        os.write(self.fd, styled(text, style, self.fd).encode("utf-8"))

    def readline(self):
        line = bytearray()
        while True:
            byte = os.read(self.fd, 1)
            if not byte or byte == b"\n":
                return line.decode("utf-8", errors="replace")
            line.extend(byte)

    def close(self):
        os.close(self.fd)


def terminal():
    try:
        return Terminal()
    except OSError:
        fail("an interactive controlling terminal is required to confirm initialization/new login.")


def confirm(tty, text):
    tty.write(text + " [y/N] ", "33")
    answer = tty.readline()
    if answer.strip().lower() not in ("y", "yes"):
        fail("cancelled; no account switch performed.")


def exclusive(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    inode = os.fstat(fd).st_ino
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if path.lstat().st_ino == inode:
            path.unlink()
        raise


def marker_valid(marker):
    if not os.path.lexists(marker):
        return False
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or stream.read(64) != b"codex-as v1\n"):
            fail("unsafe/unrecognized initialization marker; preserve auth.json and recover manually.")
    return True


def prepare(account):
    home = Path(os.environ["HOME"])
    directory(home)
    codex, secret = home / ".codex", home / ".secrets"
    auth, target = codex / "auth.json", secret / f"codex-{account}.json"
    marker = codex / "codex-as.initialized"
    if os.path.lexists(codex):
        directory(codex, initializing=True)
    initialized = marker_valid(marker) if codex.exists() else False
    if initialized:
        directory(codex)
    if os.path.lexists(secret):
        directory(secret, private=True)
    if initialized:
        managed_link(auth, target)
    elif auth.is_symlink():
        managed_link(auth, target)
    ordinary = os.path.lexists(auth) and not auth.is_symlink()
    current = credential(auth) if ordinary else None
    missing = not os.path.lexists(target)
    if not missing:
        credential(target)
    tty = None
    current_alias, replace_snapshot = None, False
    try:
        if not initialized:
            tty = terminal()
            matches = [name for name in accounts() if current is not None
                       and credential(secret / f"codex-{name}.json") == current]
            if current is not None:
                if len(matches) == 1:
                    current_alias = matches[0]
                else:
                    tty.write("Current file auth cannot identify Keychain/IDE login. Enter its local account name (empty cancels): ")
                    current_alias = tty.readline().strip()
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", current_alias):
                        fail("cancelled/invalid current account name; auth.json preserved.")
                existing = secret / f"codex-{current_alias}.json"
                if os.path.lexists(existing) and credential(existing) != current:
                    confirm(tty, f"Replace the different {current_alias} snapshot with current file auth after backing up both?")
                    replace_snapshot = True
            detail = f"; preserve current file as {current_alias}" if current_alias else ""
            confirm(tty, "Initialize shared auth link" + detail + "; close ALL terminals/desktop/IDE clients first. No lock or concurrency protection")
        if missing and current_alias != account:
            tty = tty or terminal()
            tty.write("Existing accounts: " + (", ".join(accounts()) or "(none)") + "\n")
            confirm(tty, f"Account {account} does not exist. Login and save a new account?")
            with tempfile.TemporaryDirectory(prefix="codex-as-login-") as temp:
                env = os.environ.copy()
                env["CODEX_HOME"] = temp
                result = subprocess.run([shutil.which("codex"), "-c", 'cli_auth_credentials_store="file"',
                                         "login", "--device-auth"], env=env, stdin=tty, stdout=tty, stderr=tty)
                if result.returncode:
                    fail("native device login failed/cancelled; no credentials imported. Device login must be enabled by the account/workspace.")
                raw = credential(Path(temp) / "auth.json")
                secret.mkdir(mode=0o700, exist_ok=True)
                directory(secret, private=True)
                exclusive(target, raw)
                tty.write(f"Credentials saved for account {account}.\n", "32")
        if not initialized:
            codex.mkdir(mode=0o700, exist_ok=True)
            directory(codex, initializing=True)
            secret.mkdir(mode=0o700, exist_ok=True)
            directory(secret, private=True)
            if ordinary:
                if credential(auth) != current:
                    fail("auth.json changed during initialization; preserve it and stop all clients.")
                exclusive(secret / (".codex-as-backup-" + secrets.token_hex(12)), current)
                existing = secret / f"codex-{current_alias}.json"
                if not os.path.lexists(existing):
                    exclusive(existing, current)
                elif replace_snapshot:
                    old = credential(existing)
                    exclusive(secret / (".codex-as-backup-" + secrets.token_hex(12)), old)
                    staged = secret / (".codex-as-import-" + secrets.token_hex(12))
                    exclusive(staged, current)
                    os.replace(staged, existing)
                elif credential(existing) != current:
                    fail("account snapshot changed; no overwrite performed.")
            credential(target)
            # Mark BEFORE replacement: interrupted initialization is fail-closed,
            # never confused with a later CLI's atomic replacement of the link.
            os.chmod(codex, 0o700)
            exclusive(marker, b"codex-as v1\n")
            link = codex / (".codex-as-" + secrets.token_hex(12))
            os.symlink(str(target), link)
            try:
                if ordinary and credential(auth) != current:
                    fail("auth.json changed; initialized marker retained for manual recovery.")
                if not ordinary and os.path.lexists(auth):
                    managed_link(auth, target)
                os.replace(link, auth)
            finally:
                link.unlink(missing_ok=True)
            tty.write(f"Shared auth link initialized for account {account}.\n", "32")
        else:
            credential(target)
            managed_link(auth, target)
            if Path(os.readlink(auth)) != target:
                staged = codex / (".codex-as-" + secrets.token_hex(12))
                os.symlink(str(target), staged)
                try:
                    managed_link(auth, target)
                    os.replace(staged, auth)
                finally:
                    staged.unlink(missing_ok=True)
    finally:
        if tty:
            tty.close()


def paths(account):
    home = Path(os.environ["HOME"])
    if not home.is_absolute() or home == Path("/"):
        fail("HOME must be an absolute user home directory.")
    codex, secret = home / ".codex", home / ".secrets"
    for path in (home, codex):
        directory(path)
    directory(secret, private=True)
    return codex / "auth.json", secret / f"codex-{account}.json"


def managed_link(auth, target, after=False):
    if not auth.is_symlink():
        fail("auth.json is absent/replaced or a regular file: preserve it and recover current credentials; initialize only after closing clients; see docs/codex-as.md.")
    if auth.lstat().st_uid != os.getuid():
        fail("auth.json symlink is not owned by the current user.")
    old = Path(os.readlink(auth))
    if old.parent != target.parent or not re.fullmatch(r"codex-[A-Za-z0-9_-]+\.json", old.name):
        fail("auth.json points outside the managed absolute ~/.secrets/codex-NAME.json namespace.")
    if after and old != target:
        fail("auth.json changed during Codex; close every client and recover before switching.")
    credential(old)
    return True


def config_sources(cwd):
    home = Path(os.environ["HOME"])
    paths = {home / ".codex/config.toml", Path("/etc/codex/config.toml"), Path("/etc/codex/managed_config.toml")}
    paths.update(parent / ".codex/config.toml" for parent in (cwd, *cwd.parents))
    paths.add(Path("/etc/codex/requirements.toml"))
    for path in paths:
        if not path.exists():
            continue
        with path.open("rb") as stream:
            cfg = tomllib.load(stream)
        if cfg.get("model_provider", "openai") != "openai":
            fail("a configuration selects another model provider; review its auth/billing source first.")
        provider = cfg.get("model_providers", {}).get("openai", {})
        if any(k in provider for k in ("env_key", "experimental_bearer_token", "base_url", "http_headers", "env_http_headers", "requires_openai_auth", "auth", "gateway_oauth", "aws")):
            fail("configuration overrides OpenAI provider authentication/endpoint; review it first.")
        if cfg.get("chatgpt_base_url") or cfg.get("openai_base_url"):
            fail("configuration overrides ChatGPT authentication endpoint; review it first.")
        if cfg.get("forced_login_method") or cfg.get("forced_chatgpt_workspace_id"):
            fail("local login/workspace restrictions require credential review; no switch performed and no policy removed.")
        if path.name in {"requirements.toml", "managed_config.toml"} and cfg.get("cli_auth_credentials_store", "file") != "file":
            fail("administrator credential-store policy conflicts with file authentication; no switch performed.")
    # Codex still loads/enforces its own system, workspace and cloud policies.


def main():
    action, *rest = sys.argv[1:]
    if action == "accounts":
        names = accounts()
        if names:
            print("\n".join(names))
        return
    account, *args = rest
    if not re.fullmatch(r"[A-Za-z0-9_-]+", account):
        fail("invalid account name.")
    if action == "preflight":
        expected = os.path.join(os.environ["HOME"], ".codex")
        if "CODEX_HOME" in os.environ and os.path.abspath(os.environ["CODEX_HOME"]) != expected:
            fail("CODEX_HOME conflicts with ~/.codex; no switch performed.")
        names = (
            "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL",
            "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_ORGANIZATION", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
            "CODEX_CHATGPT_BASE_URL", "CODEX_CHATGPT_ACCOUNT_ID",
            "OPENAI_FEDERATION_RULE_ID", "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
            "CODEX_REFRESH_TOKEN_URL_OVERRIDE", "CODEX_APP_SERVER_LOGIN_CLIENT_ID",
            "CODEX_AUTHAPI_BASE_URL", "CODEX_AGENT_IDENTITY_AUTHAPI_BASE_URL",
            "CODEX_AGENT_IDENTITY_JWKS_BASE_URL",
        )
        conflicts = [name for name in names if name in os.environ]
        if conflicts:
            fail("authentication/endpoint environment conflict: " + ", ".join(conflicts) + " (values hidden).")
        mode, cwd = arguments(args)
        config_sources(cwd)
        print(mode)
        return
    if action == "prepare":
        prepare(account)
        return
    auth, target = paths(account)
    credential(target)
    exists = managed_link(auth, target, after=action == "after")
    if action == "switch":
        if exists and Path(os.readlink(auth)) == target:
            return
        temporary = auth.parent / (".codex-as-" + secrets.token_hex(12))
        os.symlink(str(target), temporary)
        try:
            managed_link(auth, target)
            os.replace(temporary, auth)
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        # Never render exception payloads: JSON/TOML errors can include secrets.
        error = sys.exc_info()[1]
        message = str(error) if type(error) is ValueError else "missing, unreadable or malformed credential/configuration; no unsafe repair performed."
        print(styled("codex-as: " + message, "31", 2), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(styled("codex-as: cancelled; credentials/shared auth preserved unless initialization already completed.", "33", 2), file=sys.stderr)
        sys.exit(130)
