"""KLX launcher policy: pure startup checks and native configuration for one protected invocation.

Research code only. Every check runs before the gate, the monitor or the native starts, and a
refusal names its cause; nothing falls back to an unprotected run. The order is: resolve policy,
check the native version and arguments, check the host's boundary preconditions, then (outside
this module) start the gate, run the model-free preflight and only then the native.
"""
import os
import re
import stat

MIN_VERSION = (0, 167, 2)  # the only version K exercised; a minimum, never an exact pin
# Every subcommand name and alias of codex-cli 0.167.2 (S cli/src/main.rs `Subcommand`, hidden ones
# included, plus clap's `help`). Protected mode runs only the interactive TUI, so any word clap could
# take as a subcommand refuses. The table is per native version: a newer native's new subcommand
# typed as a one-word prompt would pass this screen (I).
SUBCOMMANDS = frozenset({
    'agents', 'tcp-tunnel', 'exec', 'e', 'review', 'login', 'logout', 'mcp', 'plugin', 'app-server',
    'remote-control', 'app', 'completion', 'update', 'doctor', 'sandbox', 'debug', 'execpolicy', 'apply',
    'a', 'resume', 'queue', 'archive', 'delete', 'migrate-rollouts', 'migrate-nfs-db', 'unarchive', 'fork',
    'cloud', 'cloud-tasks', 'responses-api-proxy', 'stdio-to-uds', 'exec-server', 'features', 'help'})
# Top-level options protected mode accepts, with their arity (S cli/src/main.rs, tui/src/cli.rs; N
# offline `--help` parses). Anything else that starts with '-' refuses: an allowlist, not a denylist.
LONG_FLAGS = frozenset({'--no-daemon', '--no-alt-screen', '--strict-config', '--help', '--version'})
SHORT_FLAGS = frozenset({'-h', '-V'})
LONG_VALUES = frozenset({'--config', '--model', '--cd', '--image'})
SHORT_VALUES = frozenset({'-c', '-m', '-C', '-i'})
# Known options with a named refusal: they leave the single gate-bound process, change routing,
# features or the sandbox, or were not exercised in protected mode.
REFUSED_OPTIONS = frozenset({
    '--remote', '--remote-auth-token-env', '--oss', '--local-provider', '-p', '--profile', '--enable',
    '--disable', '-s', '--sandbox', '-a', '--ask-for-approval', '--search', '--approve-for-me',
    '--dangerously-bypass-approvals-and-sandbox', '--dangerously-bypass-hook-trust', '--worktree',
    '--add-dir'})
# Configuration roots protected mode injects or fixes itself; a user override would reroute traffic,
# re-enable a disabled feature or change the sandbox (`--enable X` is `-c features.X=true`).
PROTECTED_KEYS = ('model_provider', 'model_providers', 'chatgpt_base_url', 'openai_base_url',
                  'experimental_realtime_ws_base_url', 'experimental_realtime_webrtc_call_base_url',
                  'cli_auth_credentials_store', 'forced_chatgpt_workspace_id', 'forced_login_method', 'profile',
                  'profiles', 'features', 'use_legacy_landlock', 'sandbox_mode', 'sandbox_workspace_write',
                  'web_search', 'tools', 'mcp_servers', 'oss_provider')
CONFIG_KEY = re.compile(r'[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*')
REFUSED_ENV = ('CODEX_REFRESH_TOKEN_URL_OVERRIDE', 'CODEX_REVOKE_TOKEN_URL_OVERRIDE', 'OPENAI_BASE_URL',
               'OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN')
SUPPORTED_ARCH = ('x86_64',)


class Refusal(Exception):
    pass


def parse_version(text):
    match = re.search(r'codex-cli (\d+)\.(\d+)\.(\d+)', text or '')
    if not match:
        raise Refusal('unrecognized native version output')
    return tuple(int(part) for part in match.groups())


def check_version(text):
    version = parse_version(text)
    if version < MIN_VERSION:
        raise Refusal('native %s is older than the protected-mode minimum %s'
                      % ('.'.join(map(str, version)), '.'.join(map(str, MIN_VERSION))))
    return version


def check_config(value):
    """One -c/--config value (`key=value`): Codex splits at the first '=' and trims the key (S
    utils/cli/src/config_override.rs). Refuse protected roots and any key that is not a plain
    dotted name, so quoting cannot hide a protected root."""
    key = value.split('=', 1)[0].strip()
    if '=' not in value or not CONFIG_KEY.fullmatch(key):
        raise Refusal('protected mode refuses the configuration override %r' % key)
    if key.split('.', 1)[0] in PROTECTED_KEYS:
        raise Refusal('protected mode refuses a user override of ' + key)


def check_args(argv):
    """User arguments -> native argv with --no-daemon first, or Refusal.

    Follows clap's grammar for codex 0.167.2 (N: offline `--help` parses with a scratch home):
    `--name value`, `--name=value`, `-Xvalue` and `-X=value` for options that take a value; a value
    taken from the next word never starts with '-'; every other bare word before `--` is a
    subcommand candidate, also after an option value or a prompt (`-m gpt-5.4 resume` and
    `hello resume` both run `resume`); words after `--` are prompt text only. The screen is stricter
    than clap in two places, both fail-closed: a subcommand name refuses even where clap would take
    it as an option value (`-m resume`, `-i a.png fork`), and combined short flags (`-hV`) refuse."""
    index, dashes, keep = 0, False, []

    def value_of(name, attached):
        nonlocal index
        if attached is not None:
            return attached
        if index >= len(argv) or (argv[index].startswith('-') and argv[index] != '-'):
            raise Refusal('protected mode needs a value for ' + name)
        index += 1
        return argv[index - 1]

    def screen(name, value):
        if value in SUBCOMMANDS:
            raise Refusal('protected mode refuses %r as a value of %s (a subcommand name)' % (value, name))
        if name in ('-c', '--config'):
            check_config(value)

    while index < len(argv):
        word = argv[index]
        index += 1
        if dashes:
            keep.append(word)  # prompt text: clap never takes it as an option or a subcommand
            continue
        if word == '--':
            dashes = True
            keep.append(word)
            continue
        if word.startswith('--'):
            name, equals, attached = word.partition('=')
            if name in REFUSED_OPTIONS:
                raise Refusal('protected mode refuses ' + name)
            if name in LONG_FLAGS:
                if equals:
                    raise Refusal('protected mode refuses a value for the flag ' + name)
                if name != '--no-daemon':
                    keep.append(word)
                continue
            if name not in LONG_VALUES:
                raise Refusal('protected mode does not accept the option ' + name)
            start = index
            screen(name, value_of(name, attached if equals else None))
            keep.extend(argv[start - 1:index])
            continue
        if word.startswith('-') and word != '-':
            name, rest = word[:2], word[2:]
            if name in REFUSED_OPTIONS:
                raise Refusal('protected mode refuses ' + name)
            if name in SHORT_FLAGS:
                if rest:
                    raise Refusal('protected mode refuses combined short options ' + word)
                keep.append(word)
                continue
            if name not in SHORT_VALUES:
                raise Refusal('protected mode does not accept the option ' + name)
            start = index
            attached = (rest[1:] if rest.startswith('=') else rest) if rest else None
            screen(name, value_of(name, attached))
            keep.extend(argv[start - 1:index])
            continue
        if word in SUBCOMMANDS:
            raise Refusal('protected mode runs only the interactive TUI; it refuses the %s subcommand' % word)
        keep.append(word)
    return ['--no-daemon', *keep]


def check_env(env):
    found = sorted(name for name in REFUSED_ENV if env.get(name))
    if found:
        raise Refusal('protected mode refuses ' + ', '.join(found))


def stdio_sockets(fds=(0, 1, 2)):
    found = []
    for fd in fds:
        try:
            if stat.S_ISSOCK(os.fstat(fd).st_mode):
                found.append(fd)
        except OSError:
            pass
    return found


def check_host(*, system, machine, landlock_abi, legacy_tiocsti, socket_stdio):
    """Boundary preconditions (NL refusal conditions); inputs are probed by the caller."""
    if system != 'Linux':
        raise Refusal('Linux protected mode needs Linux')
    if machine not in SUPPORTED_ARCH:
        raise Refusal('seccomp table not validated on ' + machine)
    if landlock_abi is None:
        raise Refusal('Landlock unavailable')
    if landlock_abi < 4:
        raise Refusal('Landlock ABI %d lacks TCP port rules (needs 4)' % landlock_abi)
    if legacy_tiocsti is None or legacy_tiocsti != 0:
        raise Refusal('terminal injection (legacy TIOCSTI) is not disabled on this host')
    if socket_stdio:
        raise Refusal('stdio descriptor %s is a socket' % socket_stdio)


def read_legacy_tiocsti(path='/proc/sys/dev/tty/legacy_tiocsti'):
    try:
        with open(path) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def preflight_verdict(status, require_monitor=True):
    """The gate's status after the model-free preflight read -> None (start) or a refusal cause."""
    if status is None:
        return 'gate-unavailable'
    if status.get('lost'):
        return 'gate-' + status['lost']
    if status.get('paused'):
        return 'quota:' + str(status.get('blocking') or status['paused'])
    if not status.get('ready'):
        outcome = status.get('last_outcome')
        return 'quota:' + (outcome if outcome and outcome != 'fresh' else 'no-fresh-passing-observation')
    if require_monitor and not status.get('heartbeat'):
        return 'monitor-not-started'  # the heartbeat lease starts at the first heartbeat
    return None


def prelaunch_refresh_verdict(before, after):
    """Refresh option (a): a native outside the gate refreshed before launch. `before` and `after`
    are the credential file's `last_refresh` values (the launcher reads no token) -> None or cause."""
    if after is None:
        return 'prelaunch-refresh:credential-unreadable'
    if after == before:
        return 'prelaunch-refresh:failed'
    return None


def render_config(gate_url, workspace, model='gpt-5.4', sandbox='danger-full-access'):
    """Native config.toml for a protected invocation: every model, quota and realtime URL at the gate.
    `sandbox` stays danger-full-access in protected mode; other values only record the native
    sandbox's behaviour inside the outer boundary (K brief, added items)."""
    return f'''model = "{model}"
model_provider = "klxgate"
chatgpt_base_url = "{gate_url}/backend-api/"
experimental_realtime_ws_base_url = "{gate_url}"
experimental_realtime_webrtc_call_base_url = "{gate_url}"
cli_auth_credentials_store = "file"
approval_policy = "never"
sandbox_mode = "{sandbox}"
web_search = "disabled"
check_for_update_on_startup = false
[model_providers.klxgate]
name = "klxgate"
base_url = "{gate_url}/backend-api/codex"
requires_openai_auth = true
supports_websockets = false
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
[features]
plugins = false
recommended_plugins = false
apps = false
enable_mcp_apps = false
memories = false
shell_snapshot = false
shell_snapshot_v2 = false
network_proxy = false
code_mode_prewarm = false
[memories]
generate_memories = false
use_memories = false
[analytics]
enabled = false
[projects."{workspace}"]
trust_level = "trusted"
'''


def native_env(*, home, codex_home, tmp, gate_url=None, ca=None, term='xterm-256color'):
    """Environment for natives inside the boundary. Only the TUI gets proxy variables (gate_url), so
    its refresh can use the gate's CONNECT allowlist; controller readers get none."""
    env = {'PATH': '/usr/bin:/bin', 'HOME': home, 'CODEX_HOME': codex_home, 'CODEX_SQLITE_HOME': codex_home,
           'TMPDIR': tmp, 'TERM': term, 'LANG': 'C.UTF-8', 'XDG_CONFIG_HOME': home + '/.config',
           'XDG_CACHE_HOME': home + '/.cache', 'XDG_STATE_HOME': home + '/.state', 'XDG_DATA_HOME': home + '/.data',
           'XDG_RUNTIME_DIR': home + '/.runtime', 'NO_PROXY': '127.0.0.1,localhost', 'no_proxy': '127.0.0.1,localhost'}
    if gate_url:
        env.update({key: gate_url for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy')})
    if ca:
        env['CODEX_CA_CERTIFICATE'] = ca
    return env
