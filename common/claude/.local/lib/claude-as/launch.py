"""Small native-login adapter; no OAuth endpoints, refresh, logs or credential output."""

import codecs
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import termios
from urllib.parse import urlsplit


CONFLICTS = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN", "CLAUDE_CODE_USE_HOST_MANAGED_AUTH",
    "CLAUDE_CODE_HOSTED", "CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID",
    "ANTHROPIC_UNIX_SOCKET", "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_API_BASE_URL",
    "CLAUDE_CODE_CUSTOM_OAUTH_URL", "CLAUDE_CODE_OAUTH_CLIENT_ID",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS", "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_MANTLE", "CLAUDE_CODE_USE_GATEWAY", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
    "CLAUDE_CODE_HOST_AUTH_ENV_VAR", "CLAUDE_CODE_HOST_CREDS_FILE",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR", "CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR",
    "CLAUDE_SESSION_INGRESS_TOKEN_FILE", "CLAUDE_BG_AUTH_SNAPSHOT_PATH",
}
PINS = {"apiKeyHelper", "forceLoginMethod", "forceLoginOrgUUID", "allowedProviders", "forceLoginGatewayUrl"}
NAME = re.compile(r"[A-Za-z0-9_-]+")
TOKEN = re.compile(r"sk-ant-oat[A-Za-z0-9_.-]{1,8192}")
CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC = re.compile(r"\x1b\]([^\x07]*?)(?:\x07|\x1b\\)", re.S)
SECTION = re.compile(r"Your OAuth token \(valid for [^\r\n)]{1,80}\):\s*(.*?)\s*Store this token securely\.", re.S)


def styled(text, style, fd):
    """Use the same auto/always/never policy as lib/terminal.sh per destination."""
    mode = os.environ.get("DOTFILES_COLOR", "auto")
    enabled = (not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
               and (mode == "always" or (mode != "never" and os.isatty(fd))))
    return f"\033[{style}m{text}\033[0m" if enabled and text.strip() else text


def fail(message):
    raise ValueError(message)


def directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        fail(".secrets must be an owned, real directory with permissions 0700.")


def read_secret(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            fail("token must be an owned regular 0600 file with exactly one hard link.")
        raw = stream.read(16385)
    if len(raw) > 16384:
        fail("oversized token file; no login or overwrite performed.")
    token = raw.decode("ascii").strip()
    validate(token)
    return token


def validate(token):
    if not TOKEN.fullmatch(token) or token.count("sk-ant-oat") != 1:
        fail("unrecognized/empty OAuth token structure; this is not server authentication.")


def accounts(root):
    if not os.path.lexists(root):
        return []
    directory(root)
    result = []
    for path in root.iterdir():
        name = path.name.removeprefix("claude-")
        info = path.lstat()
        if (path.name.startswith("claude-") and NAME.fullmatch(name)
                and stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600):
            result.append(name)
    return sorted(result)


class Terminal:
    def __init__(self):
        try:
            self.fd = os.open("/dev/tty", os.O_RDWR)
        except OSError:
            fail("an interactive controlling terminal is required to confirm a new login.")

    def fileno(self):
        return self.fd

    def write(self, text, style="36"):
        os.write(self.fd, styled(text, style, self.fd).encode("utf-8"))

    def read(self):
        line = bytearray()
        while True:
            byte = os.read(self.fd, 1)
            if not byte or byte == b"\n":
                return line.decode("utf-8", errors="replace")
            line.extend(byte)
            if len(line) > 16384:
                fail("input too long; no credentials saved.")

    def confirm(self, text):
        self.write(text + " [y/N] ", "33")
        if self.read().strip().lower() not in ("y", "yes"):
            fail("cancelled; no credentials saved or client launched.")

    def hidden(self):
        previous = termios.tcgetattr(self.fd)
        quiet = list(previous)
        quiet[3] &= ~termios.ECHO
        try:
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, quiet)
            self.write("Paste OAuth token (input hidden), then Enter: ")
            return self.read().strip()
        finally:
            termios.tcsetattr(self.fd, termios.TCSANOW, previous)
            self.write("\n")

    def close(self):
        os.close(self.fd)


def config_check(path, enrollment=False):
    if not path.exists():
        return
    with path.open("rb") as stream:
        cfg = json.load(stream)
    inspect_config(cfg)
    if enrollment and cfg:
        fail("automatic enrollment cannot preserve managed policy from " + str(path) + "; use your organization's native login procedure.")


def inspect_config(cfg):
    if not isinstance(cfg, dict):
        fail("unrecognized settings structure; no auth source changes performed.")
    if PINS.intersection(cfg):
        fail("settings contain authentication helper/login/provider restrictions; no policy bypass performed.")
    env = cfg.get("env", {})
    if not isinstance(env, dict) or any(key in env for key in CONFLICTS):
        fail("settings.env conflicts with account authentication (values hidden).")


def preflight(args, home, enrollment):
    conflicts = sorted(CONFLICTS.intersection(os.environ))
    if conflicts:
        fail("authentication/provider environment conflict: " + ", ".join(conflicts) + " (values hidden).")
    # Native paths use env || default and Node path joining, not shell '~'
    # expansion. Inspect exactly the configuration the real CLI will load.
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(home / ".claude")).absolute()
    sources = [config / "settings.json", config / "settings.local.json",
               Path("/etc/claude-code/managed-settings.json"),
               Path("/Library/Application Support/ClaudeCode/managed-settings.json")]
    for parent in (Path.cwd(), *Path.cwd().parents):
        sources += [parent / ".claude/settings.json", parent / ".claude/settings.local.json"]
    managed = Path("/etc/claude-code/managed-settings.d")
    if managed.exists():
        sources += sorted(managed.glob("*.json"))
    for path in set(sources):
        config_check(path, enrollment=enrollment and path in {
            Path("/etc/claude-code/managed-settings.json"),
            Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
        } or enrollment and path.parent == managed)
    config_check(config / "remote-settings.json", enrollment=enrollment)
    if enrollment and (config / "remote-settings-helper-consent").exists():
        fail("managed policy helper present; automatic enrollment cannot preserve its loading chain.")
    if enrollment:
        profiles = Path(os.environ.get("ANTHROPIC_CONFIG_DIR") or str(Path(os.environ.get("XDG_CONFIG_HOME") or str(home / ".config")) / "anthropic"))
        if (profiles / "active_config").exists() or (profiles / "configs/default.json").exists():
            fail("Anthropic profile selection present; review its authentication before automatic enrollment.")
    values = {"--model", "--effort", "--output-format", "--input-format", "--system-prompt",
              "--append-system-prompt", "--system-prompt-file", "--append-system-prompt-file",
              "--agent", "--agents", "--permission-mode", "--max-budget-usd", "--json-schema",
              "--session-id", "--settings", "--debug-file", "--fallback-model"}
    variadic = {"--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools",
                "--tools", "--mcp-config", "--betas", "--add-dir"}
    optional = {"-r", "--resume", "-d", "--debug"}
    switches = {"-p", "--print", "-c", "--continue", "--verbose", "-h", "--help", "-v", "--version",
                "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                "--strict-mcp-config", "--include-partial-messages", "--replay-user-messages",
                "--no-session-persistence", "--fork-session", "--disable-slash-commands"}
    root, options, i = True, True, 0
    while i < len(args):
        arg = args[i]
        i += 1
        if options and arg == "--":
            options = False
            continue
        flag, sep, value = arg.partition("=")
        if options and flag in {"--bare", "--setting-sources", "--env", "--provider", "--api-key",
                                "--managed-settings", "--project-config-root", "--client-data-url", "--sdk-url",
                                "--remote", "--cloud", "--teleport", "--remote-control", "--rc"}:
            fail("CLI option changes authentication/settings/provider source; refused.")
        if options and flag in values | variadic:
            if not sep:
                if i == len(args):
                    fail("option requires a value; no login performed.")
                value = args[i]
                i += 1
            if flag == "--settings":
                if value.lstrip().startswith("{"):
                    inspect_config(json.loads(value))
                else:
                    config_check(Path(value).expanduser())
            # Native Commander does not activate the variadic collector for
            # --flag=value syntax. A lone '-' is a value, not an option.
            if flag in variadic and not sep:
                while i < len(args) and not (len(args[i]) > 1 and args[i].startswith("-")):
                    i += 1
        elif options and flag in optional:
            if not sep and i < len(args) and not (len(args[i]) > 1 and args[i].startswith("-")):
                i += 1
        elif options and arg in switches:
            continue
        elif options and arg.startswith("-") and arg != "-":
            fail("unsupported/combined option; authentication safety is only audited for the supported local CLI options.")
        elif root:
            # Native Commander dispatches the first operand after options,
            # even with --print/--continue or after --. Values remain values.
            if arg in {"login", "logout", "setup-token", "auth", "remote-control", "rc"}:
                fail("login/logout/setup-token must not be passed through claude-as.")
            root = False


class Capture:
    """Versioned UI adapter; keeps <=64 KiB, never replays raw child output."""
    def __init__(self, tty):
        self.tty, self.raw, self.token = tty, "", None
        self.urls = set()
        self.retry_shown = False
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def feed(self, chunk):
        self.raw = (self.raw + self.decoder.decode(chunk))[-65536:]
        links = [m.group(1).split(";", 2)[-1] for m in OSC.finditer(self.raw) if m.group(1).startswith("8;")]
        clean = CSI.sub("", OSC.sub("", self.raw))
        # Plain UI URLs can wrap; wait for the following blank line rather
        # than displaying a partial URL at a pipe/chunk boundary.
        visible = re.findall(r"(https://[^\s\x1b<>]+(?:\r?\n[ \t]*[A-Za-z0-9_%?&=+./:~-]+)*)(?=\r?\n[ \t]*\r?\n)", clean)
        links += [re.sub(r"\s", "", url) for url in visible]
        for url in links:
            parsed = urlsplit(url)
            if (len(url) <= 16384 and parsed.scheme == "https" and parsed.netloc == "claude.com"
                    and parsed.path == "/cai/oauth/authorize" and url not in self.urls):
                if len(self.urls) == 8:
                    fail("too many different authorization URLs; use manual setup-token.")
                self.tty.write("Authorize using this native Claude URL:\n" + url + "\n")
                self.urls.add(url)
        if "Press Enter to retry" in clean and not self.retry_shown:
            self.tty.write("Claude authorization needs retry. Press Enter to retry or Ctrl-C to cancel.\n")
            self.retry_shown = True
        for match in SECTION.finditer(clean):
            body = match.group(1)
            if len(body) > 16384:
                fail("oversized native token section; automatic capture stopped.")
            candidate = re.sub(r"\s", "", body)
            validate(candidate)
            if self.token is not None and self.token != candidate:
                fail("ambiguous native token output; automatic capture stopped.")
            self.token = candidate


def enroll(executable, tty):
    with tempfile.TemporaryDirectory(prefix="claude-as-login-") as temp:
        env = os.environ.copy()
        env.update(CLAUDE_CONFIG_DIR=temp)
        noop = shutil.which("true")
        if not noop:
            fail("cannot disable automatic browser opening safely.")
        env["BROWSER"] = noop
        version = subprocess.run([executable, "--version"], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        automatic = version.returncode == 0 and version.stdout.strip() == b"2.1.296 (Claude Code)"
        if automatic:
            tty.write("Complete webpage authorization; paste full code#state here if prompted, then Enter. Ctrl-C cancels.\n")
            capture = Capture(tty)
            state, child = termios.tcgetattr(tty.fileno()), None
            try:
                child = subprocess.Popen([executable, "setup-token"], env=env, stdin=tty,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                while chunk := os.read(child.stdout.fileno(), 4096):
                    capture.feed(chunk)
                if child.wait() == 0 and capture.token:
                    return capture.token
            except ValueError:
                pass  # Never print parser errors/raw output, which may contain tokens.
            finally:
                try:
                    if child:
                        child.stdout.close()
                        if child.poll() is None:
                            child.terminate()  # Only this explicitly requested enrollment child.
                            try:
                                child.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait()
                finally:
                    termios.tcsetattr(tty.fileno(), termios.TCSANOW, state)
            tty.write("Native setup-token failed or its output could not be captured safely.\n", "33")
        else:
            tty.write("Claude version has no audited automatic capture adapter.\n", "33")
        tty.confirm("Use official setup-token interactively, then paste its token without echo? (The official command displays its token)")
        state = termios.tcgetattr(tty.fileno())
        try:
            result = subprocess.run([executable, "setup-token"], env=env, stdin=tty, stdout=tty, stderr=tty)
        finally:
            termios.tcsetattr(tty.fileno(), termios.TCSANOW, state)
        if result.returncode:
            fail("native setup-token failed/cancelled; no token saved.")
        token = tty.hidden()
        validate(token)
        return token


def save(path, token):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    inode = os.fstat(fd).st_ino
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((token + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if path.lstat().st_ino == inode:
            path.unlink()
        raise


def main():
    home = Path(os.environ["HOME"])
    secret = home / ".secrets"
    if sys.argv[1:] == ["--list-accounts"]:
        names = accounts(secret)
        if names:
            print("\n".join(names))
        return
    account, *args = sys.argv[1:]
    if not NAME.fullmatch(account):
        fail("invalid account name (ASCII letters, digits, _ or - only).")
    info = home.lstat()
    if not home.is_absolute() or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        fail("HOME must be an owned, real user directory without group/other write access.")
    query = args in (["--help"], ["-h"], ["--version"], ["-v"])
    target = secret / ("claude-" + account)
    missing = not os.path.lexists(target)
    preflight(args, home, enrollment=missing and not query)
    executable = shutil.which("claude")
    if not executable:
        fail("real Claude executable not found on PATH.")
    if query:
        os.execv(executable, [executable, *args])
    if os.path.lexists(secret):
        directory(secret)
    if missing:
        tty = Terminal()
        try:
            tty.write("Existing accounts: " + (", ".join(accounts(secret)) or "(none)") + "\n")
            tty.confirm(f"Account {account} does not exist. Login and save a new account?")
            token = enroll(executable, tty)
            secret.mkdir(mode=0o700, exist_ok=True)
            directory(secret)
            save(target, token)
            tty.write(f"Credentials saved for account {account}.\n", "32")
        finally:
            tty.close()
    else:
        token = read_secret(target)
    env = os.environ.copy()
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    os.execve(executable, [executable, *args], env)


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        print(styled("claude-as: Python 3.11+ is required; no dependencies installed.", "31", 2), file=sys.stderr)
        sys.exit(1)
    os.umask(0o077)
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        error = sys.exc_info()[1]
        message = str(error) if type(error) is ValueError else "missing, unreadable or malformed token/settings; no unsafe repair performed."
        print(styled("claude-as: " + message, "31", 2), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(styled("claude-as: cancelled; no token saved or client launched.", "33", 2), file=sys.stderr)
        sys.exit(130)
