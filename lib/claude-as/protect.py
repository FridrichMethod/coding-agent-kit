"""claude-as protected mode: the launcher side of one protected invocation.

Load this file by path (it is never on sys.path). launch() runs when a native-login account starts
with an enabled reserve. In order (reviewed design, section 2.1):

1. refusals: the platform boundary's host checks (Linux Landlock ABI 6+ on x86_64, or a reviewed
   macOS release with sandbox-exec), an interactive terminal, the native arguments (an allowlist of
   the interactive forms of Claude Code 2.1.322, parsed as its option parser does; every subcommand,
   print, background, remote, cloud, resume and settings form refuses), the caller environment
   (credential, provider, store, endpoint and proxy selectors), the native version (only the reviewed
   2.1.322: a disclosed upper bound, since a new subcommand typed as a bare word looks like a prompt),
   the working directory (never HOME, an ancestor of it or HOME/Library), and the policy evaluator
   (policy.py) for the account's server-managed policy and the local settings sources;
2. the port: the launcher becomes the child subreaper and holds a random loopback port (on macOS also
   every interface address), then runs the macOS boundary self-test;
3. the D1 refresh: `claude auth status` runs confined behind a refresh gate that tunnels only
   platform.claude.com:443, so native refreshes its own token before model work; it must succeed;
4. the gate (provider.py) reads the token memory-only and refuses on its local checks; the monitor
   starts; once a heartbeat is heard the gate's first quota check must pass;
5. the native runs on the launcher's PTY inside the boundary with ANTHROPIC_BASE_URL and HTTPS_PROXY
   at the gate, its own sandbox off (the outer boundary confines writes to the workspace, the account
   directory, a private temporary directory and any --add-dir) and remoteControlAtStartup off;
   CODING_AGENT_KIT_CLAUDE_PID is the native's own pid. At the end every boundary descendant is
   ended, this invocation's temporary entries are removed and the port is released.

A refusal raises ValueError with one line (launch.py prints it, exit 1); nothing ran under the
boundary then. Afterwards launch() returns the native's exit status.
"""

from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(os.path.realpath(__file__)).parent
KIT = HERE.parent / "coding-agent-kit"
REVIEWED_VERSIONS = ("2.1.322",)
VERSION_LINE = re.compile(r"([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}) \(Claude Code\)")
REFRESH_TIMEOUT = 30.0
PREFLIGHT_TIMEOUT = 45.0
REFRESH_TARGET = "platform.claude.com:443"
# Interactive option forms of Claude Code 2.1.322 (S: its --help) that keep one terminal session
# whose model requests all reach the gate. Values are checked by native; models by the gate.
VALUES = {"--model", "--effort", "--system-prompt", "--append-system-prompt", "--system-prompt-file",
          "--append-system-prompt-file", "--agent", "--agents", "--permission-mode", "--fallback-model",
          "--debug-file", "-n", "--name", "--autocompact", "--system-prompt-snapshot"}
VARIADIC = {"--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--tools", "--add-dir"}
OPTIONAL = {"-d", "--debug", "--prompt-suggestions"}
SWITCHES = {"--verbose", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
            "--disable-slash-commands", "--strict-mcp-config", "--ax-screen-reader", "--brief", "--no-chrome",
            "--restricted", "--safe-mode", "--exclude-dynamic-system-prompt-sections"}
# The first operand native dispatches as a command (2.1.322 --help, plus the hidden login words).
SUBCOMMANDS = {"agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import", "install", "logs", "mcp",
               "plugin", "plugins", "purge", "respawn", "rm", "setup-token", "stop", "kill", "ultrareview",
               "update", "upgrade", "login", "logout", "remote-control", "rc", "config", "api"}
# Caller variables that select another credential store, endpoint, transport or process wrapper
# (launch.py's preflight refuses the credential and provider ones first).
ENV_REFUSED = ("CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_LOCAL_OAUTH_API_BASE", "CLAUDE_CODE_PROCESS_WRAPPER",
               "CLAUDE_CODE_HTTP_PROXY", "CLAUDE_CODE_HTTPS_PROXY", "ANTHROPIC_CONFIG_DIR", "CLAUDE_CODE_TMPDIR")
PLAIN = re.compile(r"-[A-Za-z0-9_.,:/@+-]*")
# Empty in every launch: only the in-process test driver (tests/fixtures/claude-reserve) sets these, to
# point the gate at loopback fakes. No environment variable or file reaches them.
GATE_EXTRA, REFRESH_EXTRA = {}, {}
REASONS = {
    "threshold": "a usage window is at or below its reserve",
    "native_refresh_window": "the account's access token expires within five minutes and was not refreshed",
    "unproved_quota_row": "the usage report has a limit row protected mode has not reviewed",
    "identity_unattributed": "the usage reads did not prove the enrolled account",
    "identity_mismatch": "the signed-in account is not the enrolled one",
    "identity_changed": "the signed-in account is not the enrolled one",
    "upstream_unavailable": "the quota reads could not reach the provider",
    "monitor_lost": "the quota monitor stopped",
    "gate did not start": "the request gate did not start",
}


class Refused(ValueError):
    own_message = True


def _load(name, path):
    spec = importlib.util.spec_from_file_location("claude_as_protect_" + name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ImportError, SyntaxError):
        raise Refused(f"incomplete install at {HERE.parent.parent}") from None
    return module


def refuse(text):
    raise Refused("protected mode refused: " + text)


def explain(reason):
    reason = str(reason)
    if reason.startswith("credential_"):
        return "the account's stored login could not be read (%s); sign in again with the enrollment commands" % reason
    return REASONS.get(reason, reason.replace("_", " "))


@dataclass
class Arguments:
    add_dirs: list


def screen_arguments(args):
    """Arguments(add_dirs) for an allowlisted interactive invocation; Refused otherwise."""
    add_dirs, index, options, first = [], 0, True, True
    while index < len(args):
        arg = args[index]
        index += 1
        if options and arg == "--":
            options = False
            continue
        if options and arg.startswith("-") and arg != "-":
            flag, sep, value = arg.partition("=")
            if flag in VALUES | VARIADIC:
                if not sep:
                    if index == len(args):
                        refuse(f"{flag} needs a value.")
                    value = args[index]
                    index += 1
                values = [value]
                if flag in VARIADIC and not sep:
                    while index < len(args) and not (len(args[index]) > 1 and args[index].startswith("-")):
                        values.append(args[index])
                        index += 1
                if flag == "--add-dir":
                    add_dirs += values
            elif flag in OPTIONAL:
                if not sep and index < len(args) and not (len(args[index]) > 1 and args[index].startswith("-")) \
                        and flag != "--prompt-suggestions":
                    index += 1
            elif arg in SWITCHES:
                pass
            else:
                shown = flag if PLAIN.fullmatch(flag) and "sk-" not in flag.lower() else "this option"
                refuse(f"{shown} is not an interactive form protected mode supports; launch without reserves "
                       "for it.")
            continue
        if first:
            # Native dispatches the first operand as a command, even after options or --.
            if arg in SUBCOMMANDS:
                refuse(f"the claude {arg} command does not run in protected mode; launch without reserves for it.")
            first = False
    return Arguments(add_dirs)


def screen_environment(env):
    found = sorted(name for name in ENV_REFUSED if name in env)
    if found:
        refuse("environment selector " + ", ".join(found) + " (values hidden); unset it.")


def native_version(executable, env):
    try:
        result = subprocess.run([executable, "--version"], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        refuse("claude --version did not answer.")
    match = VERSION_LINE.fullmatch(result.stdout.decode("utf-8", "replace").strip())
    if result.returncode or not match:
        refuse("claude --version did not print a Claude Code version.")
    if match.group(1) not in REVIEWED_VERSIONS:
        refuse(f"Claude Code {match.group(1)} is not a version this kit release reviewed for protected mode "
               f"({', '.join(REVIEWED_VERSIONS)}); point the claude key at a reviewed version or launch without "
               "reserves.")
    return match.group(1)


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def check_writable(paths, home):
    """Refuse a writable path that is HOME, an ancestor of HOME, HOME/Library or in its LaunchAgents."""
    homes = {str(home), os.path.realpath(home)}
    for path in paths:
        for top in homes:
            if _inside(top, path) or path == top + "/Library" or _inside(path, top + "/Library/LaunchAgents"):
                refuse(f"{path} cannot be writable in protected mode (it is your home directory, contains it or "
                       "holds login items); start Claude Code in a project directory.")


def private_directory(path):
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not os.path.isdir(path) or os.path.islink(path) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        refuse(f"{path} must be a private directory of yours.")
    return path


def macos_temp_entries(uid):
    root = "/private/tmp/claude-%d" % uid
    try:
        return root, set(os.listdir(root))
    except OSError:
        return root, set()


def clean_macos_temp(root, before, cwd):
    """Remove this invocation's entry in Claude's fixed /private/tmp/claude-UID: the one named after the
    working directory, and only when it did not exist before the native started."""
    name = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    path = os.path.join(root, name)
    if name in before:
        return
    try:
        info = os.lstat(path)
    except OSError:
        return
    if os.path.isdir(path) and not os.path.islink(path) and info.st_uid == os.getuid():
        shutil.rmtree(path, ignore_errors=True)


def launch(config, policy, paths, record, args, executable, env, style=lambda text: text):
    supervise = _load("supervise", KIT / "supervise.py")
    gate = _load("gate", KIT / "gate.py")
    reserve = _load("reserve", KIT / "reserve.py")
    policies = _load("policy", HERE / "policy.py")
    darwin = sys.platform == "darwin"
    if not (darwin or sys.platform.startswith("linux")):
        refuse("this platform has no protected-mode boundary (Linux or macOS only).")
    sandbox = _load("sandbox", KIT / ("sandbox_macos.py" if darwin else "sandbox_linux.py"))
    try:
        sandbox.probe()
    except sandbox.Refusal as error:
        refuse(str(error))
    if not os.isatty(0) or not os.isatty(1):
        refuse("protected mode needs an interactive terminal for continuation.")
    screened = screen_arguments(args)
    screen_environment(env)
    native_version(executable, env)
    identity = (record["identity"]["account_uuid"], record["identity"]["organization_uuid"])
    cwd = os.path.realpath(os.getcwd())
    verdict = policies.evaluate(paths.config, identity[1], cwd)
    if verdict.refused():
        refuse("Claude Code policy for this account: " + verdict.describe())
    try:
        proxy = supervise.capture_proxy(os.environ, gate.parse_proxy)
    except supervise.Refused as error:
        refuse(error.reason)
    uid = os.getuid()
    tmp_root = os.path.realpath(env.get("TMPDIR") or "/tmp") if not darwin else os.path.realpath(
        env.get("TMPDIR") or tempfile.gettempdir())
    add_dirs = []
    for path in screened.add_dirs:
        real = os.path.realpath(path)
        if not os.path.isdir(real):
            refuse(f"--add-dir {path} is not a directory.")
        add_dirs.append(real)
    private_tmp = tempfile.mkdtemp(prefix="claude-as-", dir=tmp_root)
    try:
        writable = [cwd, paths.config, private_tmp, *add_dirs]
        check_writable(writable, config.home)
        settings = {"sandbox": {"enabled": False}, "remoteControlAtStartup": False}
        reserves = {setting.window: None if setting.reserve is None else str(setting.reserve)
                    for setting in policy.settings()}
        sources = {setting.window: setting.source for setting in policy.settings()}
        user = env.get("USER") or pwd.getpwuid(uid).pw_name
        gate_config = {"max_age_s": reserve.MAX_AGE, "heartbeat_max_s": reserve.HEARTBEAT_LOSS,
                       "proxy": list(proxy) if proxy else None, "account": paths.name, "reserves": reserves,
                       "sources": sources, "identity": list(identity), "config_dir": paths.config,
                       "platform": "darwin" if darwin else "linux", "keychain_user": user, "home": str(config.home)}
        gate_config.update(GATE_EXTRA)
        refresh_config = dict(gate_config, connect_allow={REFRESH_TARGET: ["platform.claude.com", 443]})
        refresh_config.update(REFRESH_EXTRA)
        tested = {}

        def confine(port, argv):
            if darwin:
                return sandbox.confine(port, writable, "claude", tested=tested["result"]) + argv
            return sandbox.confine(port, writable, "claude") + argv

        def preflight(sup):
            import time
            deadline = time.monotonic() + PREFLIGHT_TIMEOUT
            while time.monotonic() < deadline:
                status = sup.request("status", 10)
                if status.get("state") != "checking" and status.get("heard"):
                    if status.get("state") == "running":
                        return
                    fields = status.get("fields") or {}
                    raise supervise.Refused("%s (%s)" % (fields.get("message") or explain(status.get("reason")),
                                                         status.get("reason")))
                time.sleep(0.05)
            raise supervise.Refused("no fresh quota check passed in time")

        extra = sandbox.holder_addresses().reserve if darwin else ()
        gate_env = {name: env[name] for name in ("SSL_CERT_FILE", "SSL_CERT_DIR") if env.get(name)}
        sup = supervise.Supervisor([sys.executable, "-I", "-B", str(HERE / "provider.py")], gate_config,
                                   refresh_config=refresh_config, preflight=preflight, confine=confine,
                                   pid_env="CODING_AGENT_KIT_CLAUDE_PID", holder_extra=extra, gate_env=gate_env,
                                   heartbeat=reserve.HEARTBEAT, style=style)
        print(style("claude-as: " + policy.describe() + "; protected mode: only the request gate is reachable, "
                    "and after a pause F12 then the code shown asks for a fresh check."), file=sys.stderr)
        before = None
        try:
            port = sup.prepare()
            if darwin:
                tested["result"] = sandbox.self_test(port, writable, "claude")
            gate_url = "http://127.0.0.1:%d" % port
            native_env = dict(env, HTTPS_PROXY=gate_url, https_proxy=gate_url, TMPDIR=private_tmp)
            native_env.pop("ANTHROPIC_BASE_URL", None)
            code = sup.refresh([executable, "auth", "status"], native_env, timeout=REFRESH_TIMEOUT, cwd=cwd)
            if code != 0:
                refuse(f"the pre-launch native refresh (claude auth status) failed ({'timeout' if code is None else code});"
                       f" run CLAUDE_CONFIG_DIR={paths.config} claude auth status yourself to see why.")
            verdict = policies.evaluate(paths.config, identity[1], cwd)
            if verdict.refused():
                refuse("Claude Code policy for this account: " + verdict.describe())
            settings["env"] = {"ANTHROPIC_BASE_URL": gate_url}
            native_env["ANTHROPIC_BASE_URL"] = gate_url
            if darwin:
                temp_root, before = macos_temp_entries(uid)
            status = sup.run([executable, "--settings", json.dumps(settings, separators=(",", ":")), *args],
                             native_env, cwd=cwd)
        except supervise.Refused as error:
            refuse(explain(error.reason) if error.reason in REASONS else error.reason)
        except sandbox.Refusal as error:
            refuse(str(error))
        finally:
            sup.close()
            if before is not None:
                clean_macos_temp(temp_root, before, cwd)
        return supervise.finish(status)
    finally:
        shutil.rmtree(private_tmp, ignore_errors=True)
