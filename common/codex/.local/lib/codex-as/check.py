"""Small stdlib guard for the executable shell command; never rewrites JSON."""

import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid


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


# Oldest release with the shared thread writer locks the account homes rely on.
MINIMUM_VERSION = (0, 161, 0)


def codex_version(text):
    """Accept any release from the minimum on; behavior is checked at runtime."""
    match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?", text.strip())
    if not match:
        fail("unrecognized Codex version output; no launch performed.")
    version = tuple(int(part) for part in match.groups()[:3])
    # A prerelease precedes its release, so 0.161.0-alpha is below the minimum.
    if (*version, match[4] is None) < (*MINIMUM_VERSION, True):
        fail("Codex version %d.%d.%d or newer is required; found %s." % (*MINIMUM_VERSION, text.strip()))
    return version


def native_commands(codex):
    """Root subcommands and aliases this binary lists; new ones are refused too."""
    result = subprocess.run([codex, "--help"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, check=False)
    names, section = set(), False
    for line in result.stdout.splitlines() if result.returncode == 0 else ():
        if line == "Commands:":
            section = True
        elif section and not line.strip():
            break
        elif section:
            # Wrapped descriptions are indented further and never match a name.
            match = re.match(r"  ([a-z][a-z0-9-]*)(?:\s|$)", line)
            if match:
                names.add(match[1])
            for aliases in re.findall(r"\[aliases?: ([^\]]+)\]", line):
                names.update(alias.strip() for alias in aliases.split(","))
    if not {"exec", "resume", "fork", "login", "logout"} <= names:
        fail("cannot identify native Codex subcommands from codex --help; no launch performed.")
    return frozenset(names)


def arguments(args, native=frozenset()):
    # A bounded guard, not a replacement for clap. Values/prompts are never
    # interpreted as login/logout inside an allowed subcommand. A root
    # PROMPT does not end clap's subcommand recognition; keep checking until
    # exec/resume/fork or --. Options after a prompt still need checking.
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
    command, target, last, query = None, None, False, False
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            if command in {"resume", "fork", "exec-resume"} and target is None and i < len(args):
                target = args[i]
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
            if arg == "--last":
                last = True
            if arg in {"-h", "--help", "-V", "--version"}:
                query = True
            continue
        elif arg.startswith("-") and arg != "-":
            fail("unsupported option; profiles, remote/provider/auth overrides are refused.")
        else:
            if root:
                if arg in {"exec", "e"}:
                    mode = "exec"
                    command = "exec"
                    root = False
                elif arg in {"resume", "fork"}:
                    command = arg
                    root = False
                elif arg in commands or arg in native:
                    fail("unsupported subcommand; login/logout must run outside codex-as.")
            elif command == "exec":
                command = "exec-resume" if arg == "resume" else "exec-prompt"
            elif command in {"resume", "fork", "exec-resume"} and target is None:
                target = arg
            continue
        if flag in {"-c", "--config"}:
            key = value.partition("=")[0]
            if re.search(r"auth|login|provider|base_url|workspace_id|remote|api_key|access_token|sqlite_home", key, re.I):
                fail("authentication/provider configuration overrides are refused.")
        elif flag in {"-C", "--cd"}:
            cwd = Path(value).expanduser().absolute()
        elif flag in {"--enable", "--disable"} and re.search(r"auth|login|provider|remote|identity", value, re.I):
            fail("authentication/provider feature overrides are refused (conservative local guard).")
    if target is not None and not last:
        try:
            uuid.UUID(target)
        except ValueError:
            fail("resume/fork requires a session UUID; use --last or the native picker instead of a session name.")
    resume_id = str(uuid.UUID(target)) if command == "resume" and target is not None and not last and not query else None
    picking = command == "resume" and resume_id is None and not query
    return mode, cwd, resume_id, picking


def directory(path, private=False):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        fail(f"{path}: must be an owned directory, never a symlink.")
    if info.st_mode & 0o022:
        fail(f"{path}: directory is group/other writable; review and restrict it first.")
    if private and stat.S_IMODE(info.st_mode) != 0o700:
        fail(f"{path}: private credential/account directory must have permissions 0700; review it before changing permissions.")


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
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_keys,
                          parse_constant=lambda _: fail("credential JSON contains a non-finite number."))
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
    apikey = nonempty(key) and tokens is None and data.get("auth_mode") in (None, "apikey")
    if not (chatgpt or apikey):
        fail("credential has no complete ChatGPT tokens or file API-key login; not server validation.")
    refreshed = data.get("last_refresh")
    if chatgpt or refreshed is not None:
        if not isinstance(refreshed, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", refreshed):
            fail("credential requires a valid last_refresh timestamp.")
        try:
            datetime.fromisoformat(refreshed)
        except ValueError:
            fail("credential requires a valid last_refresh timestamp.")
    if chatgpt:
        try:
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


def preflight(account, args):
    expected = Path(os.environ["HOME"]) / ".codex"
    allowed = (expected, expected / "accounts" / account)
    if "CODEX_HOME" in os.environ:
        inherited = Path(os.path.abspath(os.environ["CODEX_HOME"]))
        if inherited not in allowed:
            if inherited.parent != expected / "accounts" or not re.fullmatch(r"[A-Za-z0-9_-]+", inherited.name):
                fail("CODEX_HOME conflicts with the shared root/managed accounts; no launch performed.")
            directory(inherited.parent, private=True)
            directory(inherited, private=True)
            if private_metadata(inherited / "codex-as.initialized") != b"codex-as v2\n":
                fail("inherited account home is not initialized by codex-as.")
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
    mode, cwd, resume_id, picking = arguments(args)
    config_sources(cwd)
    return mode, cwd, resume_id, picking


def private_metadata(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            fail("unsafe layout/lease metadata; no repair performed.")
        return stream.read()


def warn(message):
    print(styled("codex-as: " + message, "33", 2), file=sys.stderr)


def report(error):
    message = str(error) if type(error) is ValueError else "missing, unreadable or malformed credential/configuration; no unsafe repair performed."
    print(styled("codex-as: " + message, "31", 2), file=sys.stderr)


if __name__ == "__main__":
    try:
        action, = sys.argv[1:]
        if action == "accounts":
            names = accounts()
            if names:
                print("\n".join(names))
        else:
            fail("unsupported internal action.")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError) as error:
        report(error)
        sys.exit(1)
