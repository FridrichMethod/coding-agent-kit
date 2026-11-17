"""Small adapter for Claude Code setup-token logins; no OAuth endpoints, refresh, logs or credential output."""

import codecs
from dataclasses import dataclass, field
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import termios
import time
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
# The 0.7 names and short forms of the launcher's own options, which count only as
# the first argument and by exact match; the older spellings stay accepted.
QUERIES = {"--list": "--list-accounts", "-l": "--list-accounts", "--labels": "--account-labels",
           "--info": "--account-info", "-i": "--account-info", "--label": "--set-label"}
OPTIONS = {*QUERIES, *QUERIES.values(), "--show-config", "--help", "-h", "--version", "-V", "-v"}
# A dash word that is safe to echo back in the hint.
PLAIN = re.compile(r"-[A-Za-z0-9_=.,:/@+-]*")
# The machine's managed settings files, which outrank every other source, and the drop-in directories
# beside them (Linux, macOS). A launch without a usage reserve reads the first directory only.
MANAGED_FILES = (Path("/etc/claude-code/managed-settings.json"),
                 Path("/Library/Application Support/ClaudeCode/managed-settings.json"))
MANAGED_DIRECTORIES = (Path("/etc/claude-code/managed-settings.d"),
                       Path("/Library/Application Support/ClaudeCode/managed-settings.d"))
# The machine's other policy sources that a launch with a usage reserve checks with them (machine_policies): on
# macOS the managed preferences an MDM profile installs for the com.anthropic.claudecode domain, the machine's and
# this user's; on WSL the Windows managed settings, which Claude Code takes in only when one of them sets
# wslInheritsWindowsSettings. Server-managed settings and the HKLM policy key (which Claude Code on WSL reads
# through reg.exe) are not checked: hooks they stop show as the footer line and the warning at the account's
# next guarded launch.
MACOS = sys.platform == "darwin"
MDM_PREFERENCES = Path("/Library/Managed Preferences")
MDM_NAME = "com.anthropic.claudecode.plist"
WINDOWS_POLICY = Path("/mnt/c/Program Files/ClaudeCode")
PROC_VERSION = "/proc/version"

# A launch with a usage reserve on (the guarded launch): Claude Code 2.1.296 has every hook contract the
# guard relies on (onFailure "block", a prompt hook that Esc cannot skip, continue false ending a turn).
GUARD_MINIMUM = (2, 1, 296)
HOOK_TIMEOUT = 20  # seconds Claude Code gives each guard hook
VERSION_TIMEOUT = 30  # seconds claude --version may take
VERSION = re.compile(r"\s*(\d+)\.(\d+)\.(\d+)(?![\d.])")
# The reasons claude-as gives when it refuses a form of REFUSED, given its words.
OUTSIDE = "{} runs Claude Code outside its interactive session, where the usage reserve guard cannot stop it"
HANDED = ("{} hands the session to Claude Code's background process, which runs it with the environment and credential "
          "of whichever launch started that process")
JOINED = "claude {} runs or joins sessions in Claude Code's background process, outside this launch's usage reserve guard"
DETACHED = "claude {} runs the model outside Claude Code's interactive session, where the usage reserve guard cannot stop it"
SERVED = ("claude {} serves other clients' model requests outside Claude Code's interactive session, where the usage "
          "reserve guard cannot stop them")
# Every form of Claude Code 2.1.326 (the reviewed release) that runs model work outside the session hooks a guarded
# launch injects, or away from this terminal, with the reason claude-as refuses it with when a reserve is on: an
# option (a word), wherever the command line parser takes it as one (also spelled with =), or a command (a tuple), by
# its first and second operands, also after options and after --. Claude Code's modes that run none of the hooks are
# refused through HOOKLESS and HOOKLESS_FLAGS, beside their environment switches; every other option or command either
# runs the interactive session, where the hooks run, starts none (SESSIONLESS, VERBS), is refused as the first
# argument (LEADING), or is refused for every launch by the authentication audit (scan). A form a later release adds
# is not here.
REFUSED = {
    # Print mode: one turn with no terminal session and no status line, so the guard never records this account's
    # usage; an option every launch accepts.
    "-p": OUTSIDE,
    "--print": OUTSIDE,  # -p's long name
    # Non-interactive like -p: runs the Setup and SessionStart hooks, then exits. Hidden, and an unsupported option
    # without a reserve, so that a launch with one names this reason.
    "--init-only": OUTSIDE,
    # Starts the session in Claude Code's background process and returns at once; unsupported without a reserve.
    "--bg": HANDED,
    "--background": HANDED,  # --bg's long name
    # The agent view: lists, dispatches and opens the background process's sessions.
    ("agents",): JOINED,
    # Opens one of those sessions in this terminal; its turns still run in the background process.
    ("attach",): JOINED,
    # Restarts one of them, or all of them with --all, in the background process.
    ("respawn",): JOINED,
    # A cloud-hosted multi-agent review of the branch, on this account, whose findings it waits for and prints.
    ("ultrareview",): DETACHED,
    # One model request that critiques the user's auto mode rules, outside any session.
    ("auto-mode", "critique"): DETACHED,
    # Plugin evals: each run is a claude child of its own, and a model grader runs outside any hook.
    ("plugin", "eval"): DETACHED,
    ("plugins", "eval"): DETACHED,  # plugins is plugin's alias
    # The MCP server, whose Agent tool runs sub-agents on this launch's credential with none of its hooks.
    ("mcp", "serve"): DETACHED,
    # The enterprise gateway: a server that forwards other clients' Messages API requests upstream until stopped.
    # Its required --config is not an audited option, so in practice the audit refuses it first.
    ("gateway",): SERVED,
}
FIRST = ("claude {} is Claude Code's Remote Control only as its first argument, where a launch with usage reserves puts "
         "the guard's --settings, so Claude Code would send it to the model as a prompt")
# The words Claude Code 2.1.326 runs as a command only as its first argument, with the reason claude-as refuses them
# with there when a reserve is on: the names of Remote Control's fast path that its command line parser does not know.
# Behind the guard's --settings Claude Code would take the word as the first prompt of a session, as it does behind
# any option or --. remote-control and rc, the names the parser knows, are refused for every launch by the
# authentication audit (scan). A cc:// or cc+unix:// connect URL is a prompt wherever it stands in this release.
LEADING = {"remote": FIRST, "sync": FIRST, "bridge": FIRST}
# The commands of Claude Code 2.1.326 that start no session, where no hook ever runs, by first operand, with the
# second operands that keep them so (empty for a command that takes no operand, which counts alone; None for one whose
# operand is a path, a version or a file, where any second operand counts): with a reserve on they launch as without
# one, so that no launch state expects the guard's hooks. A subcommand or an operand of a later release, an operand
# after --, and an option of REFUSED keep the launch guarded. Claude Code takes the background verbs as verbs wherever they are the
# first argument; after options only before a session id, and as a prompt otherwise. The hidden commands that need
# an option the audit refuses (--json) are not here. plugin test runs a mod's tests, each file in a child of Claude
# Code that runs no model, and only with plugin as the first argument, which a guard's --settings would take.
PLUGIN = {"init", "new", "validate", "tag", "list", "details", "marketplace", "install", "i", "uninstall", "remove",
          "rm", "prune", "autoremove", "restore-set-aside", "enable", "disable", "update", "configure", "test", "help"}
SESSIONLESS = {"doctor": set(), "update": set(), "upgrade": set(), "install": None, "purge": None,
               "project": {"purge", "help"}, "auto-mode": {"defaults", "config", "reset", "help"},
               "plugin": PLUGIN, "plugins": PLUGIN,
               "mcp": {"add", "add-json", "add-from-claude-desktop", "get", "list", "login", "logout", "remove",
                       "reset-project-choices", "xaa", "help"},
               # Hidden: imports exported conversations; reviews and accepts the settings cloud sessions use; reports,
               # installs or removes the Windows sandbox.
               "import-conversations": None, "apply-project-settings": set(),
               "sandbox": {"install", "status", "uninstall", "help"}}
VERBS = ("logs", "stop", "kill", "rm")
# A guarded launch keeps its session in this process: no move to the background, no agent view, and no
# re-run of a turn that the guard stopped. Claude Code also reads these from a settings file's env.
CLOSED = {"CLAUDE_DISABLE_ADOPT": "1", "CLAUDE_CODE_DISABLE_AGENT_VIEW": "1"}
REOPENING = ("CLAUDE_CODE_RESUME_INTERRUPTED_TURN",)
# The switches of Claude Code's modes that run none of the hooks of a --settings, from the environment or
# a settings file's env (CLAUDE_CODE_SIMPLE is what --bare sets).
HOOKLESS = {"CLAUDE_CODE_SAFE_MODE": "safe mode", "CLAUDE_CODE_SIMPLE": "bare mode"}
# Their switches on the command line, which Claude Code finds by a plain scan of its arguments up to the first
# --, so also as the value of an option (a word with = in it is not one).
HOOKLESS_FLAGS = {"--safe-mode": "safe mode", "--bare": "bare mode"}
# The command that Claude Code runs every command hook and the status line through when it is not empty, from the
# environment or a settings file's env: a wrapper may run the guard without this launch's variables, so that it
# allows and records nothing, or lose its exit code, so that a block becomes an allow.
SHELL_PREFIX = "CLAUDE_CODE_SHELL_PREFIX"
WRAPS = ("the command Claude Code runs the usage reserve guard's hooks and status line through, which can hide this "
         "launch from the guard or turn its blocks into allows")
# What Claude Code's isEnvTruthy reads as on, and the values it documents as off.
ON_WORDS = ("1", "true", "yes", "on")
OFF_WORDS = ("", "0", "false", "no", "off")


def shown(word):
    """What the hint echoes of a plain dash word: the part before the first =, since a value or a
    secret may follow it, or None when that part holds sk- or names an own option."""
    word = word.partition("=")[0]
    return None if "sk-" in word.lower() or word in OPTIONS else word


def styled(text, style, fd):
    """CODING_AGENT_KIT_COLOR=auto|always|never per destination; NO_COLOR or TERM=dumb turn it off."""
    mode = os.environ.get("CODING_AGENT_KIT_COLOR", "auto")
    enabled = (not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
               and (mode == "always" or (mode != "never" and os.isatty(fd))))
    return f"\033[{style}m{text}\033[0m" if enabled and text.strip() else text


def fail(message):
    raise ValueError(message)


def directory(path, config):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        name = ".secrets" if path == config.home / ".secrets" else str(path)
        fail(name + " must be an owned, real directory with permissions 0700.")


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


def accounts(root, config):
    if not os.path.lexists(root):
        return []
    directory(root, config)
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
    """The settings at PATH, checked; None when there is no file."""
    if not path.exists():
        return None
    with path.open("rb") as stream:
        cfg = json.load(stream)
    inspect_config(cfg)
    if enrollment and cfg:
        fail("automatic enrollment cannot preserve managed policy from " + str(path) + "; use your organization's native login procedure.")
    return cfg


def inspect_config(cfg):
    if not isinstance(cfg, dict):
        fail("unrecognized settings structure; no auth source changes performed.")
    if PINS.intersection(cfg):
        fail("settings contain authentication helper/login/provider restrictions; no policy bypass performed.")
    env = cfg.get("env", {})
    if not isinstance(env, dict) or any(key in env for key in CONFLICTS):
        fail("settings.env conflicts with account authentication (values hidden).")


def managed(path):
    """Whether PATH is one of the machine's managed settings sources."""
    return (path in MANAGED_FILES or path.parent in MANAGED_DIRECTORIES
            or (path.name == MDM_NAME and MDM_PREFERENCES in path.parents)
            or path == WINDOWS_POLICY / "managed-settings.json" or path.parent == WINDOWS_POLICY / "managed-settings.d")


def on_wsl():
    """Whether Claude Code takes this host for WSL: WSL_DISTRO_NAME is set, or /proc/version names microsoft or wsl."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open(PROC_VERSION, "rb") as stream:
            text = stream.read(4096).decode("utf-8", "replace").lower()
    except OSError:
        return False
    return "microsoft" in text or "wsl" in text


def machine_policies():
    """{path: parsed settings} of the policy sources beside the managed settings files (MDM_PREFERENCES, and on WSL
    WINDOWS_POLICY once inherited); one that is absent, unreadable or not a dictionary is left out, as Claude Code
    leaves it out. Claude Code reads a wslInheritsWindowsSettings other than true, false or null as withheld
    policy; it counts as inherited here."""
    found = {}
    if MACOS:
        try:
            import plistlib  # needs pyexpat, which nothing else of a launch needs
            from xml.parsers.expat import ExpatError
        except ImportError:
            fail("this python3 cannot read the macOS managed preferences (plistlib), which a launch with usage "
                 "reserves checks.")
        paths = [MDM_PREFERENCES / MDM_NAME]
        try:
            paths.append(MDM_PREFERENCES / pwd.getpwuid(os.getuid()).pw_name / MDM_NAME)
        except KeyError:
            pass
        for path in paths:
            try:
                with path.open("rb") as stream:
                    data = plistlib.load(stream)
            except (OSError, ValueError, ExpatError):
                continue
            if isinstance(data, dict):
                found[path] = data
    if on_wsl():
        documents = {}
        try:
            drop_ins = sorted((WINDOWS_POLICY / "managed-settings.d").glob("*.json"))
        except OSError:
            drop_ins = []
        for path in (WINDOWS_POLICY / "managed-settings.json", *drop_ins):
            try:
                with path.open("rb") as stream:
                    data = json.load(stream)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                documents[path] = data
        flags = [data.get("wslInheritsWindowsSettings") for data in documents.values()]
        if any(not (flag is None or flag is False or flag == "false") for flag in flags):
            found.update(documents)
    return found


def canonical_root(root):
    """The git root ROOT as Claude Code canonicalizes it: for a linked worktree (its .git a file that names an
    administrative directory under the main repository's worktrees, which names the worktree back) the main
    repository's root; ROOT otherwise."""
    try:
        if not stat.S_ISREG((root / ".git").lstat().st_mode):
            return root
        text = (root / ".git").read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return root
        admin = Path(os.path.normpath(root / text[7:].strip()))
        common = Path(os.path.normpath(admin / (admin / "commondir").read_text(encoding="utf-8").strip()))
        back = (admin / "gitdir").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return root
    if admin.parent != common / "worktrees" or os.path.realpath(admin / back) != os.path.realpath(root / ".git"):
        return root
    return common.parent if common.name == ".git" else common


def local_root(cwd, home):
    """The directory whose .claude/settings.local.json Claude Code reads as the local settings of a session started
    in CWD: the canonical root of the git repository CWD lies in (the nearest directory with a .git directory or
    file), unless that root is CWD or HOME, or it, its .git or its .claude is not this user's; CWD otherwise.
    Where that root is not CWD, Claude Code also reads CWD's own .claude/settings.local.json, ranked below the
    root's and above the project settings."""
    for folder in (cwd, *cwd.parents):
        try:
            mode = (folder / ".git").lstat().st_mode
        except OSError:
            continue
        if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
            break
    else:
        return cwd
    root = canonical_root(folder)
    if root == cwd or root == Path(os.path.realpath(home)):
        return cwd
    try:
        owners = [root.stat().st_uid, (root / ".git").lstat().st_uid]
        if os.path.lexists(root / ".claude"):
            owners.append((root / ".claude").lstat().st_uid)
    except OSError:
        return cwd
    return root if set(owners) == {os.geteuid()} else cwd


@dataclass
class Form:
    """What preflight saw that a launch with a usage reserve checks."""
    refused: list = field(default_factory=list)  # the options of REFUSED given, in order
    modes: list = field(default_factory=list)  # HOOKLESS_FLAGS words before the first --, wherever they stand
    settings: bool = False  # a --settings of the caller
    first: object = None  # the first argument, which Claude Code's fast paths dispatch (LEADING)
    operand: object = None  # the first operand, which Claude Code dispatches as a command
    subcommand: object = None  # the second operand, which a command with subcommands dispatches
    dashed: bool = False  # the first or the second operand comes after --
    sources: dict = field(default_factory=dict)  # Path: parsed settings, for every settings file read
    statusline: object = None  # own_status_line() of the user, project and local settings (with a reserve)


def preflight(args, kit, enrollment, guarded=False):
    """Refuses what would change the account's authentication, and returns the Form a launch with a
    usage reserve (GUARDED) checks; only such a launch accepts --init-only, --bg and --background
    here, which it then refuses with its own message."""
    form = Form()
    conflicts = sorted(CONFLICTS.intersection(os.environ))
    if conflicts:
        fail("authentication/provider environment conflict: " + ", ".join(conflicts) + " (values hidden).")
    # Native paths use env || default and Node path joining, not shell '~'
    # expansion. Inspect exactly the configuration the real CLI will load.
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(kit.home / ".claude")).absolute()
    sources = [config / "settings.json", config / "settings.local.json", *MANAGED_FILES]
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        sources += [parent / ".claude/settings.json", parent / ".claude/settings.local.json"]
    # A linked worktree's local settings may lie outside the launch directory's parents.
    local = local_root(cwd, kit.home) / ".claude/settings.local.json" if guarded else None
    if local:
        sources.append(local)
    for folder in MANAGED_DIRECTORIES if guarded else MANAGED_DIRECTORIES[:1]:
        if folder.exists():
            sources += sorted(folder.glob("*.json"))
    for path in set(sources):
        cfg = config_check(path, enrollment=enrollment and managed(path))
        if cfg is not None:
            form.sources[path] = cfg
    if guarded:
        form.sources.update(machine_policies())
        # Claude Code's precedence below the command line: the local settings at local_root, the launch directory's
        # own local settings (the same file when that root is the launch directory), the project settings at the
        # launch directory, the user's.
        form.statusline = own_status_line([form.sources.get(path) for path in (
            config / "settings.json", cwd / ".claude/settings.json", cwd / ".claude/settings.local.json", local)])
    config_check(config / "remote-settings.json", enrollment=enrollment)
    if enrollment and (config / "remote-settings-helper-consent").exists():
        fail("managed policy helper present; automatic enrollment cannot preserve its loading chain.")
    if enrollment:
        profiles = Path(os.environ.get("ANTHROPIC_CONFIG_DIR") or str(Path(os.environ.get("XDG_CONFIG_HOME") or str(kit.home / ".config")) / "anthropic"))
        if (profiles / "active_config").exists() or (profiles / "configs/default.json").exists():
            fail("Anthropic profile selection present; review its authentication before automatic enrollment.")
    scan(args, form, kit, guarded)
    form.first = args[0] if args else None
    scanned = args[:args.index("--")] if "--" in args else args
    form.modes = [word for word in HOOKLESS_FLAGS if word in scanned]
    return form


def scan(args, form, kit, guarded):
    """Scans the caller's native ARGS as Claude Code's command line parser takes them, into FORM: refuses an
    option that would change the account's authentication or that is not audited, a --settings whose
    content does, and the login commands; only a launch with a usage reserve (GUARDED) accepts --init-only,
    --bg and --background."""
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
                form.settings = True
                if value.lstrip().startswith("{"):
                    inspect_config(json.loads(value))
                else:
                    config_check(kit.expand(value))
            # Native Commander does not activate the variadic collector for
            # --flag=value syntax. A lone '-' is a value, not an option.
            if flag in variadic and not sep:
                while i < len(args) and not (len(args[i]) > 1 and args[i].startswith("-")):
                    i += 1
        elif options and flag in optional:
            if not sep and i < len(args) and not (len(args[i]) > 1 and args[i].startswith("-")):
                i += 1
        elif options and arg in switches:
            if arg in REFUSED:
                form.refused.append(arg)
        elif options and guarded and flag in REFUSED:  # -p=..., and the options unsupported without a reserve
            form.refused.append(flag)
        elif options and arg.startswith("-") and arg != "-":
            fail("unsupported/combined option; authentication safety is only audited for the supported local CLI options.")
        elif root:
            # Native Commander dispatches the first operand after options,
            # even with --print/--continue or after --. Values remain values.
            if arg in {"login", "logout", "setup-token", "auth", "remote-control", "rc"}:
                fail("login/logout/setup-token must not be passed through claude-as.")
            form.operand, form.dashed = arg, not options
            root = False
        elif form.subcommand is None:
            form.subcommand = arg
            form.dashed |= not options


def sessionless(args, kit):
    """Whether the native ARGS run a command that starts no session (SESSIONLESS, or one of VERBS as the first
    argument) with no option of REFUSED and no -- before the operands that tell; False when the scan of ARGS
    refuses them, as the launch then does."""
    form = Form()
    try:
        scan(args, form, kit, guarded=True)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError):
        return False
    if form.refused or form.dashed:
        return False
    if form.operand in VERBS:
        return args[0] == form.operand
    if form.operand not in SESSIONLESS:
        return False
    subcommands = SESSIONLESS[form.operand]
    return subcommands is None or form.subcommand is None or form.subcommand in subcommands


def hook_keys(settings, managed=False):
    """The keys of one parsed settings source that keep Claude Code from running the guard's hooks:
    disableAllHooks in any source; in a MANAGED one (the only place they count) allowManagedHooksOnly, and
    strictPluginOnlyCustomization true or a list that names hooks."""
    if not isinstance(settings, dict):
        return []
    names = ("disableAllHooks", "allowManagedHooksOnly") if managed else ("disableAllHooks",)
    keys = [key for key in names if settings.get(key) is True]
    strict = settings.get("strictPluginOnlyCustomization")
    if managed and (strict is True or (isinstance(strict, list) and "hooks" in strict)):
        keys.append("strictPluginOnlyCustomization")
    return keys


def env_text(value):
    """String(VALUE) in JavaScript for a VALUE parsed from JSON, as far as reading a flag goes."""
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return "1" if value == 1 else repr(value)
    if isinstance(value, list):
        return ",".join("" if item is None else env_text(item) for item in value)
    return "[object Object]"


def env_on(value):
    """Whether Claude Code reads VALUE, of a variable or of a settings env, as on (its isEnvTruthy): a falsy
    value is off, a boolean is itself, and anything else is on when String(value), trimmed and lower-cased,
    is 1, true, yes or on."""
    if not value or isinstance(value, bool):
        return bool(value)
    return env_text(value).strip().lower() in ON_WORDS


def env_off(value):
    """Whether VALUE certainly turns a switch off: falsy, or one of the spellings Claude Code reads as off.
    Any other value counts as on, whatever this release makes of it."""
    return not value or (not env_on(value) and env_text(value).strip().lower() in OFF_WORDS)


def accepted_status_line(line):
    """Whether Claude Code's settings schema accepts LINE as a statusLine: an object whose type is "command" and
    whose command is a string, with a number as padding and a boolean as hideVimModeIndicator where given (other
    keys are let through). Claude Code skips a settings file with any other statusLine entirely."""
    return (isinstance(line, dict) and line.get("type") == "command" and isinstance(line.get("command"), str)
            and ("padding" not in line or type(line["padding"]) in (int, float))
            and ("hideVimModeIndicator" not in line or isinstance(line["hideVimModeIndicator"], bool)))


def own_status_line(sources):
    """The status line the user's settings give Claude Code, as {"command", "padding"}: the statusLine objects of
    SOURCES (parsed settings or None, lowest precedence first) that Claude Code accepts, merged as it merges them
    (a later key wins), when the command is not blank; None otherwise. A padding that is not a whole number of
    columns from 0 up is 0. The guard runs that command after it has recorded the launch's usage; best effort,
    since Claude Code also skips a file that other keys make invalid."""
    line = {}
    for settings in sources:
        if isinstance(settings, dict) and accepted_status_line(settings.get("statusLine")):
            line.update(settings["statusLine"])
    if not line or not line["command"].strip():
        return None
    padding = line.get("padding", 0)
    return {"command": line["command"], "padding": padding if type(padding) is int and padding >= 0 else 0}


def guard_settings(script, padding=0):
    """The --settings JSON of a guarded launch: SCRIPT (guard.py) under this interpreter as the command of
    the SessionStart, UserPromptSubmit, PreToolUse (every tool) and Stop hooks and of the status line.
    Claude Code runs each command through /bin/sh; the two hooks that block also block when they fail."""
    command = shlex.join([sys.executable, "-I", "-B", "-S", script])

    def hooks(kind, block=False, **group):
        handler = {"type": "command", "command": f"{command} {kind}", "timeout": HOOK_TIMEOUT}
        if block:
            handler["onFailure"] = "block"
        return [{**group, "hooks": [handler]}]

    settings = {"statusLine": {"type": "command", "command": f"{command} status", "padding": padding},
                "hooks": {"SessionStart": hooks("start"), "UserPromptSubmit": hooks("prompt", True),
                          "PreToolUse": hooks("tool", True, matcher="*"), "Stop": hooks("stop")}}
    return json.dumps(settings, separators=(",", ":"))


def refuse_guarded(account, reason):
    fail(f"{account}: {reason}; to launch without usage reserves, run claude-as --reserve-5h off --reserve-weekly "
         f"off {account} with the same arguments.")


def check_form(form, account):
    """Refuses, with a reserve on, every form the guard's hooks cannot cover."""
    for word in form.refused[:1]:
        refuse_guarded(account, REFUSED[word].format(word))
    for words, reason in REFUSED.items():
        if isinstance(words, tuple) and (form.operand, form.subcommand)[:len(words)] == words:
            refuse_guarded(account, reason.format(" ".join(words)))
    if form.first in LEADING:
        refuse_guarded(account, LEADING[form.first].format(form.first))
    if form.settings:
        refuse_guarded(account, "a --settings of your own cannot be combined with usage reserves, whose hooks "
                       "claude-as passes in its own --settings")
    if not os.isatty(1):
        refuse_guarded(account, "standard output is not a terminal, so Claude Code would run outside its "
                       "interactive session, where the usage reserve guard cannot stop it")
    for name, mode in HOOKLESS.items():
        if not env_off(os.environ.get(name)):
            refuse_guarded(account, f"the environment sets {name}, the switch of Claude Code's {mode}, which runs "
                           "none of the usage reserve guard's hooks")
    if os.environ.get(SHELL_PREFIX):
        refuse_guarded(account, f"the environment sets {SHELL_PREFIX}, {WRAPS}")
    for word in form.modes:
        refuse_guarded(account, f"{word} turns on Claude Code's {HOOKLESS_FLAGS[word]} wherever it stands before a --, "
                       "also as the value of an option, and that mode runs none of the usage reserve guard's hooks")
    for path, settings in sorted(form.sources.items()):
        for key in hook_keys(settings, managed(path)):
            refuse_guarded(account, f"{path} sets {key}, so Claude Code would not run the usage reserve guard's hooks")
        if managed(path) and "statusLine" in settings:
            refuse_guarded(account, f"{path} sets statusLine, which Claude Code runs in place of the usage reserve "
                           "guard's status line, so the guard would never see this account's usage")
        env = settings.get("env") or {}
        for name in sorted(env):
            if name in HOOKLESS and not env_off(env[name]):
                refuse_guarded(account, f"{path} sets {name} in its env, the switch of Claude Code's {HOOKLESS[name]}, "
                               "which runs none of the usage reserve guard's hooks")
            if name in REOPENING or (name in CLOSED and not env_on(env[name])):
                refuse_guarded(account, f"{path} sets {name} in its env, which would reopen what a launch with usage "
                               "reserves closes")
            # Any value but an empty string counts, whatever this release makes of it.
            if name == SHELL_PREFIX and env_text(env[name]) != "":
                refuse_guarded(account, f"{path} sets {name} in its env, {WRAPS}")


def check_version(executable, account):
    """Refuses, with a reserve on, a Claude Code older than GUARD_MINIMUM or one whose version cannot be read."""
    try:
        result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=VERSION_TIMEOUT, check=False)
        match = None if result.returncode else VERSION.match(result.stdout.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError):
        match = None
    if not match:
        refuse_guarded(account, "claude --version printed no Claude Code version")
    found = tuple(int(part) for part in match.groups())
    if found < GUARD_MINIMUM:
        minimum = ".".join(map(str, GUARD_MINIMUM))
        refuse_guarded(account, f"Claude Code {'.'.join(map(str, found))} is older than {minimum}, the first release "
                       "whose hooks can enforce usage reserves")


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


def kit_module(name):
    """lib/coding-agent-kit/NAME.py by path, from the real file, loaded after the version check."""
    lib = Path(os.path.realpath(__file__)).parent.parent
    spec = importlib.util.spec_from_file_location("claude_as_" + name, lib / f"coding-agent-kit/{name}.py")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError, SyntaxError):
        fail(f"incomplete install at {lib.parent}")
    return module


def others_can_write(info, path):
    """Whether a user other than this one can write the directory INFO describes (PATH): other write,
    or group write unless the group is the user's private group without an access ACL (config.py's
    private_group and access_acl, the rule of the configuration file); an unreadable ACL counts."""
    if info.st_mode & stat.S_IWOTH:
        return True
    if not info.st_mode & stat.S_IWGRP:
        return False
    settings = kit_module("config")
    try:
        return not settings.private_group(info.st_gid) or settings.access_acl(path)
    except OSError:
        return True


def kit_config():
    config = kit_module("config").load()
    for warning in config.warnings:
        print(styled("claude-as: " + warning, "33", 2), file=sys.stderr)
    return config


def ask_label(tty, metadata):
    """An optional label, asked after the confirmation and before any token exists."""
    for _ in range(3):
        tty.write("Label for this account (optional, e.g. your email): ")
        label = tty.read().strip()
        problem = metadata.label_problem(label)
        if not problem:
            return label or None
        tty.write(f"Label refused: {problem}.\n", "33")
    tty.write("No label saved; set one later with claude-as --label.\n", "33")
    return None


def save_label(config, metadata, account, label):
    """Best effort after the token is saved: a label problem never loses the token."""
    try:
        metadata.write(metadata.directory(config, create=True) / f"claude-{account}.json", {"label": label})
    except (OSError, ValueError):
        print(styled("claude-as: the label was not saved; set it with claude-as --label.", "33", 2), file=sys.stderr)


def set_label(config, metadata, args):
    """claude-as --label (--set-label) NAME [LABEL]: metadata only; an empty label clears it."""
    name = args[0]
    if not NAME.fullmatch(name) or name.startswith("-"):
        fail("invalid account name (ASCII letters, digits, _ or -; no leading -).")
    if name not in accounts(config.secrets_dir, config):
        fail(f"no account {name}; run claude-as {name} to create it.")
    if len(args) == 2:
        label = args[1].strip()
    else:
        tty = Terminal()
        try:
            tty.write(f"Label for account {name} (empty clears it): ")
            label = tty.read().strip()
        finally:
            tty.close()
    problem = metadata.label_problem(label)
    if problem:
        fail(f"label refused: {problem}.")
    path = metadata.directory(config, create=True) / f"claude-{name}.json"
    if label:
        metadata.write(path, {"label": label})
    elif os.path.lexists(path):
        path.unlink()
    print(styled(f"claude-as: label of {name} {'set' if label else 'cleared'}.", "32", 2), file=sys.stderr)


def say(text, style):
    print(styled(text, style, 2), file=sys.stderr)


def guard(env, metadata, guard_launch, policy, account, form, usage, directory):
    """Prepares the guarded launch in ENV (the guard's variables, the state of this launch, the closed
    background) and returns the arguments that go before the caller's: the guard's --settings."""
    now = int(time.time())
    guard_launch.prune(directory, now)
    try:
        recorded = (metadata.read(usage) or {}).get("seen_at")
    except (OSError, ValueError):  # a usage file the guard cannot use either: no recorded reply
        recorded = None
    if guard_launch.unguarded(directory, "claude-as", account, recorded):
        say(f"claude-as: {account}: an earlier launch ran without its usage reserve guard: Claude Code did not run "
            "the injected hooks.", "33")
    own = form.statusline or {"command": None, "padding": 0}
    guard_launch.prepare(env, directory, "claude-as", account, policy, str(usage), now, statusline=own["command"])
    for name in REOPENING:
        env.pop(name, None)
    env.update(CLOSED)
    say("claude-as: " + policy.describe() + "; hooks refuse new prompts and tool calls at a reserve.", "36")
    return ["--settings", guard_settings(env["CODING_AGENT_KIT_GUARD_SCRIPT"], own["padding"])]


def state_directory(metadata, config, name):
    """The state directory NAME of a guarded launch, made when missing; a path that cannot be made one
    is named with the reason."""
    try:
        return metadata.directory(config, create=True, name=name)
    except OSError as error:
        fail(f"{error.filename or config.state_dir / name}: must be an owned, real directory with permissions 0700 "
             f"({error.strerror or error}).")


def usage_environment(env, config, metadata, account, token):
    """The status line helper records this account's last rate limits through these, and
    only under the claude this process becomes (exec keeps its pid) and a token, where it
    sees one, that hashes to the same identifier."""
    for name in ("CODING_AGENT_KIT_USAGE_FILE", "CODING_AGENT_KIT_TOKEN_ID", "CODING_AGENT_KIT_CLAUDE_PID"):
        env.pop(name, None)
    try:
        folder = metadata.directory(config, create=True)
    except (OSError, ValueError):
        return
    env["CODING_AGENT_KIT_USAGE_FILE"] = str(folder / f"claude-{account}.usage.json")
    env["CODING_AGENT_KIT_TOKEN_ID"] = hashlib.sha256(token.encode("ascii")).hexdigest()[:16]
    env["CODING_AGENT_KIT_CLAUDE_PID"] = str(os.getpid())


def main():
    config = kit_config()
    secret = config.secrets_dir
    args = sys.argv[1:]
    args = [QUERIES.get(args[0], args[0]), *args[1:]] if args else args
    if args in (["--list-accounts"], ["--show-config"], ["--account-labels"]) or (
            len(args) == 2 and args[0] == "--account-info") or (args[:1] == ["--set-label"] and len(args) in (2, 3)):
        # Metadata only: no preflight, no native CLI, and never a token read.
        metadata = kit_module("accounts")
        if args[0] == "--set-label":
            set_label(config, metadata, args[1:])
            return
        if args[0] == "--account-info":
            name = args[1]
            if not NAME.fullmatch(name) or name.startswith("-"):
                fail("invalid account name (ASCII letters, digits, _ or -; no leading -).")
            if name not in accounts(secret, config):
                fail(f"no account {name}.")
            text = metadata.info("claude-as", name, config, time.time())
        elif args[0] == "--account-labels":
            text = "\n".join(metadata.labels("claude-as", accounts(secret, config), config))
        else:
            text = "\n".join(accounts(secret, config)) if args[0] == "--list-accounts" else config.show("claude-as")
        if text:
            print(text)
        return
    reserve = kit_module("reserve")
    # The reserve flags come first, before NAME; without them the arguments are unchanged.
    overrides, rest = reserve.split_flags(sys.argv[1:])
    if rest and rest[0] not in OPTIONS and PLAIN.fullmatch(rest[0]) and (word := shown(rest[0])):
        fail(f"{word} is not a claude-as option; native options go after NAME (claude-as NAME {word} ...)")
    account, *args = rest
    # As in codex-as, no name starts with -, so a mistyped option is never enrolled.
    if not NAME.fullmatch(account) or account.startswith("-"):
        fail("invalid account name (ASCII letters, digits, _ or -; no leading -).")
    # The secrets parent (HOME by default) must not let others swap the secrets. Below a regular
    # file (ENOTDIR) it does not exist either.
    parent = secret.parent
    try:
        info = parent.lstat()
    except (FileNotFoundError, NotADirectoryError):
        fail(f"{parent} does not exist; create it, owned by you and not group or other writable.")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        fail("HOME must be an owned, real user directory." if parent == config.home
             else f"{parent} must be an owned, real directory.")
    if others_can_write(info, parent):
        fail(f"{parent} must not be writable by other users (other write, or group write by a group that is not "
             f"your private group); run chmod go-w {shlex.quote(str(parent))}.")
    query = args in (["--help"], ["-h"], ["--version"], ["-v"])
    policy = reserve.resolve("claude-as", account, overrides, config.usage_reserve)
    # Only a launch with a reserve passes a guard on: inherited guard variables never reach Claude Code.
    guard_launch = kit_module("guard_launch")
    guard_launch.clear(os.environ)
    # The help and version forms after NAME and the commands that start no session run no model and stay plain.
    guarded = not policy.off and not query and not sessionless(args, config)
    target = secret / ("claude-" + account)
    missing = not os.path.lexists(target)
    form = preflight(args, config, enrollment=missing and not query, guarded=guarded)
    if guarded:
        check_form(form, account)
        lib = Path(os.path.realpath(__file__)).parent.parent
        try:
            guard_launch.script(lib)
        except OSError:
            fail(f"incomplete install at {lib.parent}")
    executable = config.which("claude")
    if not executable:
        fail("real Claude executable not found on PATH." if config.claude == "claude"
             else f"the configured claude executable {config.claude} was not found.")
    if query:
        os.execv(executable, [executable, *args])
    metadata = kit_module("accounts")
    if guarded:
        # All before a missing account is offered for enrolment.
        check_version(executable, account)
        usage = state_directory(metadata, config, "accounts") / f"claude-{account}.usage.json"
        guard_dir = state_directory(metadata, config, "guard")
    if os.path.lexists(secret):
        directory(secret, config)
    if missing:
        tty = Terminal()
        try:
            tty.write("Existing accounts: " + (", ".join(accounts(secret, config)) or "(none)") + "\n")
            tty.confirm(f"Account {account} does not exist. Login and save a new account?")
            label = ask_label(tty, metadata)
            token = enroll(executable, tty)
            secret.mkdir(mode=0o700, exist_ok=True)
            directory(secret, config)
            save(target, token)
            tty.write(f"Credentials saved for account {account}.\n", "32")
            if label:
                save_label(config, metadata, account, label)
        finally:
            tty.close()
    else:
        token = read_secret(target)
    env = os.environ.copy()
    usage_environment(env, config, metadata, account, token)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    injected = guard(env, metadata, guard_launch, policy, account, form, usage, guard_dir) if guarded else []
    os.execve(executable, [executable, *injected, *args], env)


if __name__ == "__main__":
    if sys.version_info < (3, 11):
        print(styled("claude-as: Python 3.11+ is required; no dependencies installed.", "31", 2), file=sys.stderr)
        sys.exit(1)
    os.umask(0o077)
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError, RecursionError, RuntimeError):
        error = sys.exc_info()[1]
        own = type(error) is ValueError or getattr(error, "own_message", False)
        message = str(error) if own else "missing, unreadable or malformed token/settings; no unsafe repair performed."
        print(styled("claude-as: " + message, "31", 2), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print(styled("claude-as: cancelled; no token saved or client launched.", "33", 2), file=sys.stderr)
        sys.exit(130)
