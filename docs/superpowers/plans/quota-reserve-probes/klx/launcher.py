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
# Subcommands and options that leave the invocation's single gate-bound native process.
REFUSED_WORDS = {'resume', 'fork', 'remote-control', 'app-server', 'exec-server', 'cloud', 'login',
                 'logout', 'mcp-server', 'queue', 'sandbox'}
REFUSED_OPTIONS = {'--remote', '--remote-auth-token-env', '--oss', '--local-provider', '-p', '--profile'}
# Configuration keys protected mode injects itself; a user override would reroute traffic.
PROTECTED_KEYS = ('model_provider', 'model_providers', 'chatgpt_base_url', 'openai_base_url',
                  'experimental_realtime_ws_base_url', 'experimental_realtime_webrtc_call_base_url',
                  'cli_auth_credentials_store', 'forced_chatgpt_workspace_id', 'profile', 'profiles')
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


def check_args(argv):
    """User arguments -> native argv with --no-daemon first, or Refusal."""
    for index, word in enumerate(argv):
        name = word.split('=', 1)[0]
        if name in REFUSED_OPTIONS:
            raise Refusal('protected mode refuses ' + name)
        if name in ('-c', '--config'):
            value = word.split('=', 1)[1] if '=' in word else (argv[index + 1] if index + 1 < len(argv) else '')
            key = value.split('=', 1)[0].strip()
            if any(key == protected or key.startswith(protected + '.') for protected in PROTECTED_KEYS):
                raise Refusal('protected mode refuses a user override of ' + key)
        if name == '--no-daemon':
            continue
        if not word.startswith('-') and word in REFUSED_WORDS and all(
                previous.startswith('-') for previous in argv[:index]):
            raise Refusal('protected mode refuses the %s subcommand' % word)
    return ['--no-daemon', *[word for word in argv if word != '--no-daemon']]


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


def preflight_verdict(status):
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
    return None


def render_config(gate_url, workspace, model='gpt-5.4'):
    """Native config.toml for a protected invocation: every model, quota and realtime URL at the gate."""
    return f'''model = "{model}"
model_provider = "klxgate"
chatgpt_base_url = "{gate_url}/backend-api/"
experimental_realtime_ws_base_url = "{gate_url}"
experimental_realtime_webrtc_call_base_url = "{gate_url}"
cli_auth_credentials_store = "file"
approval_policy = "never"
sandbox_mode = "danger-full-access"
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
