"""Small stdlib guard for the executable shell command; never rewrites JSON."""

import base64
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
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


# Native Codex 0.161-0.163 options that take a value (root, resume, fork, exec, review).
VALUES = {
    "-c", "--config", "-m", "--model", "-i", "--image", "-C", "--cd", "-p", "--profile",
    "-s", "--sandbox", "-a", "--ask-for-approval", "--add-dir", "--enable",
    "--disable", "--output-schema", "--color", "-o", "--output-last-message",
    "--thread-source", "--cyber-access-program", "--base", "--commit", "--title", "--listen",
}
SWITCHES = {
    "--strict-config", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-bypass-hook-trust", "--worktree", "--search", "--no-alt-screen",
    "--no-daemon", "-h", "--help", "-V", "--version", "--json", "--last",
    "--all", "--include-non-interactive", "--skip-git-repo-check", "--ephemeral",
    "--ignore-user-config", "--ignore-rules", "--uncommitted", "--yolo", "--not-so-yolo",
    "--experimental-json",
}
# Remote backends and local providers bypass the account login.
REFUSED = {"--remote", "--remote-auth-token-env", "--oss", "--local-provider"}
# Native `app` is a subcommand only where the Desktop app exists; elsewhere it is a prompt.
DESKTOP = sys.platform in {"darwin", "win32"}
EXEC = {"exec", "exec-prompt", "exec-resume"}
# Root subcommands and aliases of native Codex 0.163.0, plus clap's help.
COMMANDS = {
    "agents", "tcp-tunnel", "exec", "e", "review", "login", "logout", "mcp", "plugin",
    "app-server", "remote-control", "app", "completion", "update", "doctor", "sandbox",
    "debug", "execpolicy", "apply", "a", "resume", "queue", "archive", "delete",
    "migrate-rollouts", "unarchive", "fork", "cloud", "cloud-tasks", "responses-api-proxy",
    "stdio-to-uds", "exec-server", "features", "help",
}
# The launcher sets these; the update check is off because its installer writes under CODEX_HOME.
FIXED_KEYS = {"cli_auth_credentials_store", "sqlite_home", "check_for_update_on_startup"}
# Replaces the ChatGPT login with an agent identity.
FEATURES = {"use_agent_identity"}
PROFILE = re.compile(r"[A-Za-z0-9_-]+")  # native ProfileV2Name
# uuid::Uuid::parse_str forms; anything else is a session name to native Codex.
HYPHENATED = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
SESSION_ID = re.compile(rf"(?:urn:uuid:)?{HYPHENATED}|\{{{HYPHENATED}\}}|[0-9a-fA-F]{{32}}")


@dataclass(frozen=True)
class Plan:
    mode: str  # "interactive" or "exec"
    cwd: Path
    command: str | None  # first root subcommand, "exec-resume", or None for the TUI
    resume_id: str | None  # interactive resume UUID to take over
    recover_id: str | None  # fork/exec resume UUID: only finish an interrupted takeover
    scope: object  # picker/--last: None, "all", or the directory whose sessions are listed
    last: bool
    include_non_interactive: bool
    no_daemon: bool  # root --no-daemon given; native refuses a second one
    rootless: bool  # root help or version, update and completion need no account
    unknown: tuple  # options this scan does not know (typos fail in clap after any takeover)
    profile: str | None  # -p/--profile: layers ~/.codex/NAME.config.toml


def session_id(text):
    return str(uuid.UUID(text)) if isinstance(text, str) and SESSION_ID.fullmatch(text) else None


def toml_value(raw):
    # Native -c parses the value as TOML, else keeps the unquoted text.
    try:
        return tomllib.loads("_x_ = " + raw)["_x_"]
    except tomllib.TOMLDecodeError:
        # Native parses TOML 1.1 (trailing commas, newlines in inline tables); a table or
        # array this parser cannot read could hide a key, so it is not taken as text.
        if raw.strip()[:1] in ("{", "["):
            fail("a -c table or array uses TOML this Python cannot check; write it as TOML 1.0.")
        return raw.strip().strip("\"'")


def settings(cfg, project=False):
    """Fail on settings that select another login, provider, endpoint or credential store.

    Native drops the provider and endpoint keys from project config, which comes from
    repository contents, so a project layer is checked only for the others."""
    if not isinstance(cfg, dict):
        return
    if not project:
        if cfg.get("model_provider", "openai") != "openai":
            fail("a configuration selects another model provider; review its auth/billing source first.")
        providers = cfg.get("model_providers")
        provider = providers.get("openai") if isinstance(providers, dict) else None
        if isinstance(provider, dict) and any(k in provider for k in (
                "env_key", "experimental_bearer_token", "base_url", "http_headers", "env_http_headers",
                "requires_openai_auth", "auth", "gateway_oauth", "aws")):
            fail("configuration overrides OpenAI provider authentication/endpoint; review it first.")
        if cfg.get("chatgpt_base_url") or cfg.get("openai_base_url"):
            fail("configuration overrides ChatGPT authentication endpoint; review it first.")
    if cfg.get("forced_login_method") or cfg.get("forced_chatgpt_workspace_id"):
        fail("local login/workspace restrictions require credential review; no switch performed and no policy removed.")
    features = cfg.get("features")
    if isinstance(features, dict) and any(features.get(name) not in (None, False) for name in FEATURES):
        fail("an authentication feature (" + ", ".join(sorted(FEATURES)) + ") is enabled; it replaces the account login.")


def override(text):
    """Check one -c KEY=VALUE the way native Codex applies it: dotted key, TOML value."""
    key, _, raw = text.partition("=")
    parts = [part.strip().strip("\"'") for part in key.strip().split(".")]
    if parts == ["use_legacy_landlock"]:
        parts = ["features", "use_legacy_landlock"]
    if parts[0] in FIXED_KEYS:
        fail("authentication/provider configuration overrides are refused.")
    nested = cfg = {}
    for part in parts[:-1]:
        nested = nested.setdefault(part, {})
    nested[parts[-1]] = toml_value(raw)
    settings(cfg)


def arguments(args):
    # A bounded scan, not a replacement for clap: arguments are forwarded
    # unchanged. Values and prompts are never read as subcommands once a
    # subcommand is known. A root PROMPT does not end clap's subcommand
    # recognition, so words are checked until a subcommand or --. Unknown
    # options are forwarded as switches, so their values are scanned as words.
    mode, root, cwd = "interactive", True, Path.cwd()
    command, target, last, query, show_all = None, None, False, False, False
    no_daemon, others, words, unknown, bare, profile = False, False, [], [], False, None
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            if command in {"resume", "fork", "exec-resume"} and target is None and i < len(args):
                target = args[i]
            break
        if arg.startswith("--"):
            flag, sep, value = arg.partition("=")
        elif len(arg) > 2 and arg[0] == "-" and (arg[:2] in VALUES or arg[:2] == "-p"):
            # clap's attached short values: -C/dir, -C=/dir, -cmodel=x.
            flag, sep, value = arg[:2], "=", arg[2:].removeprefix("=")
        else:
            flag, sep, value = arg, "", ""
        if flag in REFUSED:
            fail("unsupported option; profiles, remote/provider/auth overrides are refused.")
        if flag in VALUES:
            if not sep:
                if i == len(args):
                    fail("option requires a value; no account switch performed.")
                value = args[i]
                i += 1
                # Outside exec, a spaced -i/--image takes every following non-option word.
                while flag in {"-i", "--image"} and command not in EXEC and i < len(args) and not args[i].startswith("-"):
                    i += 1
        elif flag in SWITCHES:
            last |= flag == "--last"
            show_all |= flag == "--all" and command == "resume"
            others |= flag == "--include-non-interactive"
            no_daemon |= flag == "--no-daemon" and root
            query |= flag in {"-h", "--help", "-V", "--version"}
            # Only root help or version is the same for every login; elsewhere -h may be a command's.
            bare |= flag in {"-h", "--help", "-V", "--version"} and root and not words
            continue
        elif arg.startswith("-") and arg != "-":
            unknown.append(flag)
            continue
        else:
            if root and arg not in COMMANDS:
                words.append(arg)  # a root prompt
            if root:
                if arg in {"login", "logout"} and not harmless(arg, args[i:]):
                    fail("unsupported subcommand; login and logout revoke the account's tokens "
                         "(a missing account logs in on its first codex-as NAME).")
                if arg in {"agents", "queue"} or (arg == "app" and DESKTOP):
                    # Each needs or starts a daemon in the account home, which would hold its lease.
                    fail(f"codex {arg} needs the shared Codex daemon; codex-as accounts run without one.")
                if arg == "help":
                    query = bare = True  # help never runs the subcommand it describes
                    break
                if arg in COMMANDS and (arg != "app" or DESKTOP):
                    root, words = False, []
                    command = {"e": "exec", "a": "apply", "cloud-tasks": "cloud"}.get(arg, arg)
                    if command == "exec":
                        mode = "exec"
                    elif command == "sandbox":
                        break  # the rest is the sandboxed command line
            elif command == "exec":
                command = "exec-resume" if arg in {"resume", "fork"} else "exec-prompt"
            elif command in {"resume", "fork", "exec-resume"} and target is None:
                target = arg
            else:
                words.append(arg)
                # Foreground remote-control and stdio app-server need no daemon; these manage one.
                if command == "remote-control" or (command == "app-server" and "daemon" in words):
                    fail("this command manages a Codex daemon; codex-as accounts run without one.")
                if command == "features" and words[:1] == ["enable"] and arg in FEATURES:
                    fail("an authentication feature replaces the account login; it is refused.")
            continue
        if flag in {"-c", "--config"}:
            override(value)
        elif flag in {"-C", "--cd"}:
            cwd = Path(value).expanduser().absolute()
        elif flag in {"-p", "--profile"}:
            profile = value
        elif flag == "--enable" and value.strip() in FEATURES:
            fail("an authentication feature replaces the account login; it is refused.")
    exact = target is not None and not last and not query
    if exact and not unknown and session_id(target) is None:
        fail("resume/fork requires a session UUID; use --last or the native picker instead of a session name.")
    target_id = session_id(target) if exact else None
    resume_id = target_id if command == "resume" else None
    picking = command == "resume" and resume_id is None and not query
    # The picker and --last list this directory's sessions unless --all.
    scope = None if not picking else "all" if show_all else cwd
    return Plan(mode, cwd, command, resume_id, target_id if command in {"fork", "exec-resume"} else None,
                scope, last and picking, others, no_daemon,
                bare or command in {"update", "completion"}, tuple(unknown),
                profile if profile is not None and PROFILE.fullmatch(profile) else None)



def harmless(command, rest):
    """login status, or help for login or logout, revokes nothing."""
    j = 0
    while j < len(rest):
        arg = rest[j]
        j += 1
        if arg in {"-h", "--help"}:
            return True
        if arg == "--":
            return False
        if arg in {"-c", "--config", "--enable", "--disable"}:
            j += 1
        elif not arg.startswith("-"):
            return command == "login" and arg == "status"
    return False


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
        fail("an interactive controlling terminal is required to confirm a new login or a takeover.")


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


def config_sources(cwd, profile=None):
    home = Path(os.environ["HOME"])
    user = {home / ".codex/config.toml", Path("/etc/codex/config.toml"), Path("/etc/codex/managed_config.toml"),
            Path("/etc/codex/requirements.toml")}
    if profile is not None:
        user.add(home / ".codex" / (profile + ".config.toml"))
    project = set()
    # Lexical and resolved parents: a symlinked -C has both.
    for place in (cwd, cwd.resolve()):
        project.update(parent / ".codex/config.toml" for parent in (place, *place.parents))
    for path in sorted(user | project):
        if not path.exists():
            continue
        with path.open("rb") as stream:
            try:
                cfg = tomllib.load(stream)
            except tomllib.TOMLDecodeError as error:
                fail(f"{path}: TOML this Python cannot check ({error}); no launch performed.")
        settings(cfg, project=path not in user)
        if path.name in {"requirements.toml", "managed_config.toml"} and cfg.get("cli_auth_credentials_store", "file") != "file":
            fail("administrator credential-store policy conflicts with file authentication; no switch performed.")
    # Codex still loads/enforces its own system, workspace and cloud policies.


def preflight(account, plan):
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
    # Variables that native Codex 0.161-0.163 reads as a login, token endpoint or billing scope.
    names = (
        "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY",
        "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_ORGANIZATION", "OPENAI_PROJECT",
        "OPENAI_FEDERATION_RULE_ID", "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE", "CODEX_REVOKE_TOKEN_URL_OVERRIDE",
        "CODEX_APP_SERVER_LOGIN_CLIENT_ID", "CODEX_APP_SERVER_LOGIN_ISSUER",
        "CODEX_AUTHAPI_BASE_URL", "CODEX_AGENT_IDENTITY_AUTHAPI_BASE_URL",
        "CODEX_AGENT_IDENTITY_JWKS_BASE_URL",
    )
    conflicts = [name for name in names if name in os.environ]
    if conflicts:
        fail("authentication/endpoint environment conflict: " + ", ".join(conflicts) + " (values hidden).")
    config_sources(plan.cwd, plan.profile)


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
    # Only messages written by codex-as are shown; other errors may quote file contents.
    own = type(error) is ValueError or getattr(error, "own_message", False)
    message = str(error) if own else "missing, unreadable or malformed credential/configuration; no unsafe repair performed."
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
