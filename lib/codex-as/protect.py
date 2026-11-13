"""codex-as protected mode: usage reserves enforced around interactive Codex (launcher side).

A launch whose resolved policy (reserve.py) enables a reserve for either window runs through this
module; with both windows off codex-as never calls it and behaves exactly as without it. Help,
version, update, completion and other help forms stay unprotected and unchanged.

Refusals come first and change nothing (Session.check): the native release must be a reviewed one
(REVIEWED, with an upper bound); the arguments an allowlisted interactive TUI form, parsed like
clap 0.167.2 (screen); the environment may name no other endpoint; the login must be a ChatGPT
login of a reviewed plan, not FedRAMP; the host must carry the OS boundary; and no managed
requirement (local requirements.toml, macOS managed preferences, the account's cached cloud
bundle) may forbid what protected mode sets (blockers).

Session.run then, with the account lease held by the launcher for the whole run:
1. holds a loopback port and refreshes the credential natively (reader.py, refresh mode) behind a
   refresh-mode gate whose only exits are auth.openai.com:443 and the config bundle, and refuses
   unless the credential was refreshed for the same ChatGPT user;
2. starts the gate (gate.py beside this file) and the monitor, and runs one fresh in-boundary App
   Server read whose usage request binds the gate's lease (the preflight);
3. starts the TUI inside the boundary on its own PTY with every model, quota and realtime URL at
   the gate (overrides) and the native tool sandbox off (danger-full-access): the boundary and its
   write rules replace it. Pauses, continuation (F12 and a one-use code, then a fresh read), gate
   restarts and the end-of-run sweep belong to the shared supervisor (supervise.py).

The writable set is the workspace, codex_home (which holds the account home and shared sessions),
the shared SQLite home and a private temporary directory; the refresh adds secrets_dir, where
native writes the refreshed credential. None may be HOME, an ancestor of HOME, HOME/Library or
lie at or inside HOME/Library/LaunchAgents.
"""

from dataclasses import dataclass
import base64
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import stat
import sys
import tempfile

try:
    import tomllib
except ImportError:  # check.py has already refused Python before 3.11
    tomllib = None


HERE = Path(os.path.realpath(__file__)).parent
KIT = HERE.parent / "coding-agent-kit"
GATE, READER = HERE / "gate.py", HERE / "reader.py"


class Refused(ValueError):
    """Protected mode cannot start; nothing ran under the boundary and no model request was sent."""

    own_message = True


def refuse(reason):
    raise Refused(f"usage reserve protection refused: {reason}. "
                  "(--reserve-5h off --reserve-weekly off launches unprotected)")


# -- reviewed native releases -----------------------------------------------------------------------


@dataclass(frozen=True)
class Native:
    """The per-release tables protected mode relies on (S: codex-rs at the release commit)."""

    version: tuple
    subcommands: frozenset   # every subcommand name and alias, hidden ones and clap's help included
    long_flags: frozenset    # accepted options without a value
    short_flags: frozenset
    long_values: frozenset   # accepted options that take one value
    short_values: frozenset
    refused: frozenset       # known options refused with their own message
    models: frozenset        # model slugs that draw from the codex quota family
    plans: frozenset         # plan_type values whose codex windows protected mode governs

    @property
    def name(self):
        return "%d.%d.%d" % self.version


# codex-cli 0.167.2 (codex-rs ce21072c): cli/src/main.rs Subcommand, tui/src/cli.rs, and the
# bundled models-manager/models.json. Plans: protocol/src/account.rs PlanType without free, go,
# the usage-based and automation SKUs and unknown values.
V0_167_2 = Native(
    version=(0, 167, 2),
    subcommands=frozenset({
        "agents", "tcp-tunnel", "exec", "e", "review", "login", "logout", "mcp", "plugin", "app-server",
        "remote-control", "app", "completion", "update", "doctor", "sandbox", "debug", "execpolicy", "apply",
        "a", "resume", "queue", "archive", "delete", "migrate-rollouts", "migrate-nfs-db", "unarchive", "fork",
        "cloud", "cloud-tasks", "responses-api-proxy", "stdio-to-uds", "exec-server", "features", "help"}),
    long_flags=frozenset({"--no-daemon", "--no-alt-screen", "--strict-config", "--help", "--version"}),
    short_flags=frozenset({"-h", "-V"}),
    long_values=frozenset({"--config", "--model", "--cd", "--image"}),
    short_values=frozenset({"-c", "-m", "-C", "-i"}),
    refused=frozenset({
        "--remote", "--remote-auth-token-env", "--oss", "--local-provider", "-p", "--profile", "--enable",
        "--disable", "-s", "--sandbox", "-a", "--ask-for-approval", "--search", "--approve-for-me",
        "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust", "--worktree",
        "--add-dir", "--yolo", "--not-so-yolo"}),
    models=frozenset({
        "gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol",
        "gpt-5.6-terra", "gpt-5.6-luna", "gpt-daybreak-blue-latest", "gpt-daybreak-red-latest", "gpt-5.5",
        "codex-auto-review"}),
    plans=frozenset({
        "plus", "pro", "prolite", "promax", "team", "self_serve_business_prolite", "business", "ent26",
        "enterprise", "edu", "edu_plus", "edu_pro", "sci", "sci_plus"}),
)
REVIEWED = {native.version: native for native in (V0_167_2,)}
BY_NAME = {native.name: native for native in REVIEWED.values()}


def reviewed(text):
    """The Native tables for `codex --version` output, or Refused (D7: a disclosed upper bound)."""
    match = re.fullmatch(r"codex-cli (\d+)\.(\d+)\.(\d+)(\S*)", text.strip())
    names = ", ".join(native.name for native in REVIEWED.values())
    if not match:
        refuse("the Codex version output is not recognized")
    version = tuple(int(part) for part in match.groups()[:3])
    shown = "%d.%d.%d" % version + ("" if not match[4] else " (with a suffix)")
    if match[4] or version not in REVIEWED:
        if version > max(REVIEWED):
            refuse(f"Codex {shown} is newer than the newest reviewed release ({names}); "
                   "a kit update has to review it first")
        refuse(f"Codex {shown} is not a reviewed release (reviewed: {names})")
    return REVIEWED[version]


# -- the invocation form ----------------------------------------------------------------------------

# Configuration roots protected mode sets itself, or that would reroute traffic, change the
# credential store, the sandbox or a feature it disables, or start tools outside its review.
PROTECTED_KEYS = frozenset({
    "model_provider", "model_providers", "chatgpt_base_url", "openai_base_url",
    "experimental_realtime_ws_base_url", "experimental_realtime_webrtc_call_base_url",
    "cli_auth_credentials_store", "forced_chatgpt_workspace_id", "forced_login_method", "profile", "profiles",
    "features", "use_legacy_landlock", "sandbox_mode", "sandbox_workspace_write", "default_permissions",
    "permissions", "web_search", "tools", "mcp_servers", "oss_provider", "analytics", "otel", "memories",
    "sqlite_home", "check_for_update_on_startup", "experimental_network", "network_proxy"})
CONFIG_KEY = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*")


def screen(args, native):
    """The user's native arguments as protected mode forwards them (without --no-daemon, which the
    launcher adds once), or Refused.

    An allowlist parsed like clap: `--name value`, `--name=value`, `-Xvalue` and `-X=value` for
    options with a value; a value from the next word never starts with '-'; before `--` every bare
    word that names a subcommand refuses, also after an option value or a prompt word (clap would
    run it), and so does a subcommand name given as an option value (stricter than clap)."""
    index, dashes, kept = 0, False, []

    def value_of(name, attached):
        nonlocal index
        if attached is not None:
            return attached
        if index >= len(args) or (args[index].startswith("-") and args[index] != "-"):
            refuse(f"{name} needs a value")
        index += 1
        return args[index - 1]

    def check(name, value):
        if value in native.subcommands:
            refuse(f"{name} {value}: a subcommand name is not accepted as an option value")
        if name in ("-c", "--config"):
            key = value.split("=", 1)[0].strip()
            if "=" not in value or not CONFIG_KEY.fullmatch(key):
                refuse("a -c override needs a plain dotted KEY=VALUE")
            if key.split(".", 1)[0] in PROTECTED_KEYS:
                refuse(f"protected mode sets {key.split('.', 1)[0]} itself; -c {key} is not accepted")
            if key == "model":
                model(value.split("=", 1)[1].strip().strip("\"'"), native)
        elif name in ("-m", "--model"):
            model(value, native)

    while index < len(args):
        word = args[index]
        index += 1
        if dashes:
            kept.append(word)
            continue
        if word == "--":
            dashes = True
            kept.append(word)
            continue
        if word.startswith("--"):
            name, equals, attached = word.partition("=")
            if name in native.refused:
                refuse(f"protected mode does not accept {name}")
            if name in native.long_flags:
                if equals:
                    refuse(f"{name} takes no value")
                if name != "--no-daemon":
                    kept.append(word)
                continue
            if name not in native.long_values:
                refuse(f"protected mode does not accept the option {shown(name)}")
            start = index
            check(name, value_of(name, attached if equals else None))
            kept.extend(args[start - 1:index])
            continue
        if word.startswith("-") and word != "-":
            name, rest = word[:2], word[2:]
            if name in native.refused:
                refuse(f"protected mode does not accept {name}")
            if name in native.short_flags:
                if rest:
                    refuse(f"protected mode does not accept combined short options ({shown(word)})")
                kept.append(word)
                continue
            if name not in native.short_values:
                refuse(f"protected mode does not accept the option {shown(name)}")
            start = index
            check(name, value_of(name, (rest[1:] if rest.startswith("=") else rest) if rest else None))
            kept.extend(args[start - 1:index])
            continue
        if word in native.subcommands:
            refuse(f"protected mode runs only the interactive TUI, not codex {word}")
        kept.append(word)
    return kept


def shown(word):
    """An option name for a message: printable ASCII, cut short, never a value."""
    text = "".join(char if "!" <= char <= "~" else "?" for char in word.partition("=")[0])
    return text if len(text) <= 40 else text[:40] + "..."


def model(slug, native):
    if slug not in native.models:
        refuse(f"the model {shown(slug)} is not one whose quota protected mode governs")


# Variables that route native traffic elsewhere; check.preflight refuses the login and endpoint
# ones for every launch.
REFUSED_ENV = ("OPENAI_BASE_URL",)


def environment(env):
    found = sorted(name for name in REFUSED_ENV if env.get(name))
    if found:
        refuse("the environment sets " + ", ".join(found))


# -- the login --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Login:
    account: str   # tokens.account_id, the ChatGPT-Account-ID native sends
    user: str      # chatgpt_user_id (else user_id) of the id_token
    plan: object   # chatgpt_plan_type of the id_token, or None


def login(raw, native, storage):
    """The expected identity of a validated codex-as credential, or Refused."""
    data = json.loads(raw)
    if data.get("tokens") is None:
        refuse("an API-key login has no subscription windows to protect")
    owner = storage.owner(raw)
    if owner is None:
        refuse("the login names no ChatGPT user and workspace")
    auth = storage.claims(data).get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        auth = {}
    if auth.get("chatgpt_account_is_fedramp") is True:
        refuse("FedRAMP accounts use another backend, which protected mode does not cover")
    plan = auth.get("chatgpt_plan_type")
    if not isinstance(plan, str) or plan not in native.plans:
        refuse("the account's plan%s is not one whose windows protected mode governs"
               % (f" ({plan})" if isinstance(plan, str) and re.fullmatch(r"[a-z0-9_]{1,40}", plan) else ""))
    return Login(account=owner[1], user=owner[0], plan=plan)


def last_refresh(raw):
    value = json.loads(raw).get("last_refresh")
    return value if isinstance(value, str) else None


# -- managed requirements (D14 and the E0 note) -----------------------------------------------------

# Native drops these from every cloud requirements layer (config/src/requirements_layers/layer.rs).
CLOUD_IGNORED = ("allowed_login_methods", "allowed_chatgpt_workspaces", "cli_auth_credentials_store",
                 "chatgpt_base_url")
ALIASES = (("feature_requirements", "features"), ("experimental_network", "network_proxy"))
APPLICATION_KEYS = frozenset(("network", "storage"))
NETWORK_KEYS = frozenset(("enabled", "domains"))
# Features overrides() turns off; a requirement that forces one on conflicts.
FORCED_OFF = ("plugins", "recommended_plugins", "apps", "enable_mcp_apps", "memories", "network_proxy",
              "code_mode_prewarm")
SANDBOX = "danger-full-access"
SYSTEM_REQUIREMENTS = Path("/etc/codex/requirements.toml")
MANAGED_PREFERENCES = Path("/Library/Managed Preferences")
MANAGED_DOMAIN = "com.openai.codex.plist"
BUNDLE_CACHE = "cloud-config-bundle-cache.json"
DOCUMENT_LIMIT = 1024 * 1024
BLOCKERS = {
    "application_network": "the account's managed requirements enable application network restrictions, "
                           "which deny the local gate",
    "network_requirements": "the account's managed requirements set network controls (network_proxy or "
                            "permissions.network) that protected mode cannot honor inside its boundary",
    "pins_model_provider": "the account's managed requirements pin the model provider",
    "pins_residency": "the account's managed requirements pin data residency",
    "config_server": "the account's managed requirements move the config bundle to another server",
    "pins_chatgpt_base_url": "the account's managed requirements pin chatgpt_base_url",
    "pins_credential_store": "the account's managed requirements pin another credential store",
    "pins_sqlite_home": "the account's managed requirements pin another sqlite_home",
    "pins_update_check": "the account's managed requirements pin the startup update check",
    "sandbox_mode_not_allowed": "the account's managed requirements do not allow the danger-full-access "
                                "sandbox mode that protected mode sets (its own boundary replaces it)",
    "pins_feature": "the account's managed requirements force on a feature protected mode turns off",
    "agent_identity": "the account's managed configuration enables agent identity, which replaces the login",
    "requirements_unreadable": "the account's managed requirements cannot be read or parsed",
}


def requirement_layer(table, cloud):
    """One requirements document as native prepares a layer: cloud layers lose the local-only auth
    fields; aliases fold into one key. None when both spellings are set (native rejects it)."""
    layer = {name: value for name, value in table.items() if not (cloud and name in CLOUD_IGNORED)}
    for alias, name in ALIASES:
        if alias in layer:
            if name in layer:
                return None
            layer[name] = layer.pop(alias)
    return layer


def merge(base, overlay):
    """merge_toml_values: tables extend recursively, anything else replaces."""
    for name, value in overlay.items():
        if isinstance(base.get(name), dict) and isinstance(value, dict):
            merge(base[name], value)
        else:
            base[name] = merge({}, value) if isinstance(value, dict) else value
    return base


def compose(layers):
    """Layers arrive highest priority first; native merges them lowest priority first."""
    merged = {}
    for layer in reversed(layers):
        merge(merged, layer)
    return merged


def network_state(table):
    """[application.network] as native 0.167.2 reads it: an unset enabled means enabled."""
    if "application" not in table:
        return "absent"
    application = table["application"]
    if not isinstance(application, dict) or set(application) - APPLICATION_KEYS:
        return "invalid"
    network = application.get("network")
    if network is None:
        return "absent"
    if not isinstance(network, dict) or set(network) - NETWORK_KEYS:
        return "invalid"
    enabled = network.get("enabled", True)
    return "invalid" if not isinstance(enabled, bool) else "enabled" if enabled else "disabled"


def blockers(table, sqlite_home, cloud):
    """Reasons (BLOCKERS keys) why one composed requirements table forbids protected mode."""
    found = set()
    state = network_state(table)
    if state == "invalid":
        found.add("requirements_unreadable")
    elif state == "enabled":
        found.add("application_network")
    proxy = table.get("network_proxy")
    permissions = table.get("permissions")
    networks = [proxy] if proxy is not None else []
    if isinstance(permissions, dict) and permissions.get("network") is not None:
        networks.append(permissions["network"])
    if any(not (isinstance(item, dict) and item.get("enabled") is False) for item in networks):
        found.add("network_requirements")
    for key, reason in (("model_provider", "pins_model_provider"), ("model_providers", "pins_model_provider"),
                        ("enforce_residency", "pins_residency"), ("config_server", "config_server")):
        if key in table:
            found.add(reason)
    if not cloud:
        if "chatgpt_base_url" in table:
            found.add("pins_chatgpt_base_url")
        if "cli_auth_credentials_store" in table and table["cli_auth_credentials_store"] != "file":
            found.add("pins_credential_store")
    if "sqlite_home" in table and not (isinstance(table["sqlite_home"], str)
                                       and os.path.normpath(table["sqlite_home"]) == os.path.normpath(sqlite_home)):
        found.add("pins_sqlite_home")
    if "check_for_update_on_startup" in table and table["check_for_update_on_startup"] is not False:
        found.add("pins_update_check")
    modes = table.get("allowed_sandbox_modes")
    if modes is not None and (not isinstance(modes, list) or SANDBOX not in modes):
        found.add("sandbox_mode_not_allowed")
    features = table.get("features")
    if features is not None:
        if not isinstance(features, dict):
            found.add("requirements_unreadable")
        else:
            if any(features.get(name) is True for name in FORCED_OFF):
                found.add("pins_feature")
            if features.get("use_agent_identity") is True:
                found.add("agent_identity")
    return found


def toml_document(text):
    if not isinstance(text, str) or tomllib is None:
        return None
    try:
        document = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def bundle_blockers(bundle, sqlite_home):
    """Reasons for a cloud config bundle ({"config_toml": {"enterprise_managed": [FRAGMENT]},
    "requirements_toml": {...}}, the backend reply and the cache's bundle alike)."""
    if not isinstance(bundle, dict):
        return {"requirements_unreadable"}
    buckets = []
    for name in ("requirements_toml", "config_toml"):
        bucket = bundle.get(name)
        fragments = bucket.get("enterprise_managed") if isinstance(bucket, dict) else None
        if bucket is not None and not isinstance(bucket, dict):
            return {"requirements_unreadable"}
        if fragments is None:
            fragments = []
        if not isinstance(fragments, list):
            return {"requirements_unreadable"}
        tables = []
        for fragment in fragments:
            table = toml_document(fragment.get("contents")) if isinstance(fragment, dict) else None
            if table is None:
                return {"requirements_unreadable"}
            tables.append(table)
        buckets.append(tables)
    requirements, configs = buckets
    layers = [requirement_layer(table, cloud=True) for table in requirements]
    if any(layer is None for layer in layers):
        return {"requirements_unreadable"}
    found = blockers(compose(layers), sqlite_home, cloud=True)
    features = compose(configs).get("features")
    if isinstance(features, dict) and features.get("use_agent_identity") is True:
        found.add("agent_identity")
    return found


def read_document(path, limit=DOCUMENT_LIMIT):
    """Bytes of a regular file of at most LIMIT bytes, None when absent; Refused otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        refuse(BLOCKERS["requirements_unreadable"] + f" ({path})")
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            refuse(BLOCKERS["requirements_unreadable"] + f" ({path})")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        refuse(BLOCKERS["requirements_unreadable"] + f" ({path})")
    return raw


def local_requirements():
    """The requirements documents native composes around the cloud layers, as (where, table):
    /etc/codex/requirements.toml and, on macOS, the forced com.openai.codex managed preference."""
    found = []
    raw = read_document(SYSTEM_REQUIREMENTS)
    if raw is not None:
        table = toml_document(raw.decode("utf-8", "replace"))
        if table is None:
            refuse(BLOCKERS["requirements_unreadable"] + f" ({SYSTEM_REQUIREMENTS})")
        found.append((str(SYSTEM_REQUIREMENTS), table))
    if sys.platform == "darwin":
        user = pwd.getpwuid(os.getuid()).pw_name
        for path in (MANAGED_PREFERENCES / user / MANAGED_DOMAIN, MANAGED_PREFERENCES / MANAGED_DOMAIN):
            raw = read_document(path)
            if raw is None:
                continue
            try:
                encoded = plistlib.loads(raw).get("requirements_toml_base64")
                table = {} if encoded is None else toml_document(base64.b64decode(encoded.strip()).decode("utf-8"))
            except (ValueError, AttributeError, TypeError, UnicodeError, plistlib.InvalidFileException):
                table = None
            if table is None:
                refuse(BLOCKERS["requirements_unreadable"] + f" ({path})")
            found.append((str(path), table))
    return found


def requirement_check(account_home, sqlite_home):
    """Refused when a local requirements document or the account's cached cloud bundle forbids
    protected mode. The cache may be stale; the gate checks the bundle native fetches again."""
    found = set()
    for _, table in local_requirements():
        layer = requirement_layer(table, cloud=False)
        found |= {"requirements_unreadable"} if layer is None else blockers(layer, sqlite_home, cloud=False)
    raw = read_document(Path(account_home) / BUNDLE_CACHE)
    if raw is not None:
        try:
            document = json.loads(raw)
            payload = document.get("signed_payload") if isinstance(document, dict) else None
            bundle = payload.get("bundle") if isinstance(payload, dict) and payload.get("version") == 1 else None
        except ValueError:
            bundle = None
        if bundle is not None:  # another cache version: native ignores it, and so does this check
            found |= bundle_blockers(bundle, sqlite_home)
    if found:
        refuse("; ".join(BLOCKERS[reason] for reason in sorted(found)))


# -- native configuration and write rules -----------------------------------------------------------

PROVIDER = "quota_gate"


def overrides(port):
    """-c pairs after codex-as's own fixed keys: every model, quota and realtime URL at the gate,
    no native sandbox, and the reviewed quiet set (no analytics, telemetry, web search, plugins,
    apps, memories or network proxy) for the TUI and every App Server reader alike."""
    gate = "http://127.0.0.1:%d" % port
    pairs = [
        'chatgpt_base_url="%s/backend-api/"' % gate,
        'model_provider="%s"' % PROVIDER,
        'model_providers.%s.name="quota gate"' % PROVIDER,
        'model_providers.%s.base_url="%s/backend-api/codex"' % (PROVIDER, gate),
        "model_providers.%s.requires_openai_auth=true" % PROVIDER,
        "model_providers.%s.supports_websockets=false" % PROVIDER,
        'model_providers.%s.wire_api="responses"' % PROVIDER,
        "model_providers.%s.request_max_retries=0" % PROVIDER,
        "model_providers.%s.stream_max_retries=0" % PROVIDER,
        'experimental_realtime_ws_base_url="%s"' % gate,
        'experimental_realtime_webrtc_call_base_url="%s"' % gate,
        'sandbox_mode="%s"' % SANDBOX,
        'web_search="disabled"',
        "analytics.enabled=false",
        'otel.exporter="none"',
        'otel.trace_exporter="none"',
        'otel.metrics_exporter="none"',
        *("features.%s=false" % name for name in FORCED_OFF),
        "memories.generate_memories=false",
        "memories.use_memories=false",
    ]
    return [word for pair in pairs for word in ("-c", pair)]


def writable(home, paths):
    """The resolved writable paths, or Refused when one would reopen HOME-wide delegation."""
    homes = {os.path.normpath(str(home)), os.path.realpath(str(home))}
    result = []
    for path in paths:
        real = os.path.realpath(str(path))
        for root in homes:
            library = os.path.join(root, "Library")
            if (real == root or root.startswith(real.rstrip("/") + "/") or real == library
                    or real == os.path.join(library, "LaunchAgents")
                    or real.startswith(os.path.join(library, "LaunchAgents") + "/")):
                refuse(f"{path} would become writable inside the boundary, which would expose HOME; "
                       "start from a project directory")
        if real not in result:
            result.append(real)
    return result


# Caller variables an App Server reader keeps; everything else stays with the TUI alone.
READER_ENV = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SSL_CERT_FILE",
              "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE", "CODEX_HOME", "CODEX_SQLITE_HOME", "TMPDIR")
REFRESH_HOSTS = {"auth.openai.com:443": ["auth.openai.com", 443]}
REFRESH_TIMEOUT = 30.0
READ_TIMEOUT = 45.0       # a reader allows native's bundle retries (D14: up to 40 s if it stalls)
CONTINUE_TIMEOUT = 25.0   # a continuation read must answer within the 30 s continuation window
MAX_AGE = 120.0
HEARTBEAT_MAX = 1.5


def load(name, path):
    spec = importlib.util.spec_from_file_location("codex_as_protect_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Session:
    """One protected invocation of interactive Codex.

    check() runs every refusal that needs no state change; run() the refresh, gate, preflight and
    the supervised TUI. guard is check.py, storage layout.py, reserve reserve.py."""

    def __init__(self, guard, storage, reserve, config, layout, policy, native, args, codex, env, sqlite, cwd):
        self.guard, self.storage, self.reserve, self.config = guard, storage, reserve, config
        self.layout, self.policy, self.native, self.args = layout, policy, native, args
        self.codex, self.env, self.sqlite, self.cwd = codex, dict(env), str(sqlite), Path(cwd)
        self.sandbox = self.supervise = self.kit_gate = None
        self.private = self.work = None
        self.login = None

    # -- refusals before anything changes ------------------------------------------------------------
    def check(self):
        environment(self.env)
        try:
            self.supervise = load("supervise", KIT / "supervise.py")
            self.kit_gate = load("gate", KIT / "gate.py")
            module = "sandbox_macos" if sys.platform == "darwin" else "sandbox_linux"
            self.sandbox = load(module, KIT / (module + ".py"))
        except (OSError, ImportError, SyntaxError):
            refuse(f"incomplete install at {HERE.parent.parent}")
        if not (GATE.is_file() and READER.is_file()):
            refuse(f"incomplete install at {HERE.parent.parent}")
        try:
            self.sandbox.probe()
        except self.sandbox.Refusal as error:
            refuse(str(error))
        if not os.isatty(0):
            refuse("protected mode needs an interactive terminal for continuation")
        requirement_check(self.layout.home, self.sqlite)

    # -- the run --------------------------------------------------------------------------------------
    def paths(self, refresh=False):
        """The writable set (refresh adds secrets_dir, where native writes the credential)."""
        paths = [self.cwd, self.config.codex_home, self.sqlite, self.work]
        if refresh:
            paths.append(self.config.secrets_dir)
        return writable(self.config.home, paths)

    def hook(self, paths, tested):
        sandbox = self.sandbox

        def confine(port, argv):
            if sys.platform == "darwin":
                return sandbox.confine(port, paths, "codex", tested=tested) + list(argv)
            return sandbox.confine(port, paths, "codex") + list(argv)

        return confine

    def app_server(self, port):
        return [self.codex, "-c", 'cli_auth_credentials_store="file"',
                "-c", "sqlite_home=" + json.dumps(self.sqlite, ensure_ascii=False),
                "-c", "check_for_update_on_startup=false", *overrides(port), "app-server", "--listen", "stdio://"]

    def reader_env(self, port, refresh=False):
        env = {name: self.env[name] for name in READER_ENV if name in self.env}
        env.update(CODEX_HOME=str(self.layout.home), CODEX_SQLITE_HOME=self.sqlite, TMPDIR=self.work)
        if refresh:
            env.update(HTTPS_PROXY="http://127.0.0.1:%d" % port, https_proxy="http://127.0.0.1:%d" % port)
        return env

    def spec(self, name, port, mode, timeout, paths=None, profile=None):
        """A reader's specification, in the private directory the boundary cannot write."""
        spec = {"command": self.app_server(port), "env": self.reader_env(port, mode == "refresh"),
                "cwd": str(self.cwd), "mode": mode, "timeout": timeout, "port": port,
                "writable": paths or [], "profile": profile}
        path = Path(self.private) / (name + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(spec, stream)
        return str(path)

    def message(self, text):
        print(self.guard.styled("codex-as: " + text, "36", 2), file=sys.stderr)

    def run(self, baseline):
        """The whole protected invocation; returns the native's wait status."""
        supervise, python = self.supervise, sys.executable
        self.login = login(baseline, self.native, self.storage)
        before = last_refresh(baseline)
        self.private = tempfile.mkdtemp(prefix="codex-as-protect-")
        self.work = tempfile.mkdtemp(prefix="codex-as-tmp-")
        sup = None
        try:
            paths, refresh_paths = self.paths(), self.paths(refresh=True)
            proxy = supervise.capture_proxy(self.env, self.kit_gate.parse_proxy)
            gate_env = {name: self.env[name] for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "LANG") if name in self.env}
            reserves = {setting.window: [None if setting.reserve is None else str(setting.reserve), setting.source]
                        for setting in self.policy.settings()}
            config = {"max_age_s": MAX_AGE, "heartbeat_max_s": HEARTBEAT_MAX,
                      "proxy": list(proxy) if proxy else None, "account": self.layout.name,
                      "expected": [self.login.account, self.login.user], "reserves": reserves,
                      "native": self.native.name, "sqlite_home": self.sqlite, "mode": "run"}
            refresh_config = dict(config, mode="refresh", connect_allow=REFRESH_HOSTS)
            extra = self.sandbox.holder_addresses().reserve if sys.platform == "darwin" else ()
            sup = supervise.Supervisor([python, "-I", "-B", str(GATE)], config, refresh_config=refresh_config,
                                       python=python, monitor_reader_timeout=READ_TIMEOUT, holder_extra=extra,
                                       gate_env=gate_env, style=lambda text: self.guard.styled(text, "33", 1))
            port = sup.prepare()
            tested = refresh_tested = None
            if sys.platform == "darwin":
                tested = self.sandbox.self_test(port, paths, "codex", python=python)
                refresh_tested = self.sandbox.self_test(port, refresh_paths, "codex", python=python)
            read_spec = self.spec("read", port, "read", READ_TIMEOUT)
            continue_spec = self.spec("continue", port, "read", CONTINUE_TIMEOUT)
            poll_spec = self.spec("poll", port, "read", READ_TIMEOUT, paths,
                                  tested.profile if tested is not None else None)
            refresh_spec = self.spec("refresh", port, "refresh", REFRESH_TIMEOUT)
            reader = [python, "-I", "-B", str(READER)]
            sup.monitor_reader = reader + ["--confine", poll_spec]

            # D1 (a): the native refresh, confined, with the refresh gate as its only exit.
            sup.confine = self.hook(refresh_paths, refresh_tested)
            code = sup.refresh(reader + [refresh_spec], self.reader_env(port, True), timeout=REFRESH_TIMEOUT + 5,
                               cwd=str(self.cwd))
            sup.confine = self.hook(paths, tested)
            # The refresh gate pauses on a config bundle that forbids protected mode; its report
            # is the clearer reason, and nothing of it may reach the run's relay.
            events, sup.events = sup.events, []
            for event in events:
                reason = str(event.get("reason")) if event.get("op") == "paused" else ""
                if reason.startswith("requirements_"):
                    refuse(explain(reason, {}))
            requirement_check(self.layout.home, self.sqlite)  # native may have cached a new bundle
            try:
                current = self.guard.credential(self.layout.target)
            except (OSError, ValueError):
                current = None
            if current is None or not self.storage.same_identity(current, baseline):
                refuse("the credential changed to another user or became unreadable during the pre-launch refresh")
            # Native's own account read after the refresh can fail behind the refresh gate (it
            # answers only the bundle and the tunnel); the refreshed credential is the verdict.
            if last_refresh(current) == before:
                refuse("the pre-launch credential refresh failed (%s)"
                       % ("timed out" if code is None else "reader status %d" % code if code else "unchanged"))
            if login(current, self.native, self.storage) != self.login:
                refuse("the refreshed login names another workspace, user or plan")

            def preflight(supervisor):
                code = supervisor.confined(reader + [read_spec], self.reader_env(port), timeout=READ_TIMEOUT + 5,
                                           cwd=str(self.cwd))
                status = supervisor.request("status", 10)
                if not status.get("running"):
                    reason = status.get("reason") or "no fresh passing quota check"
                    detail = "" if code == 0 else " (the quota read %s)" % (
                        "timed out" if code is None else "ended with status %d" % code)
                    raise supervise.Refused(explain(reason, status.get("fields")) + detail)

            def continue_read():
                return sup.confined(reader + [continue_spec], self.reader_env(port), timeout=CONTINUE_TIMEOUT + 5,
                                    cwd=str(self.cwd)) == 0

            sup.preflight, sup.reader = preflight, continue_read
            env = dict(self.env, CODEX_HOME=str(self.layout.home), CODEX_SQLITE_HOME=self.sqlite, TMPDIR=self.work)
            command = [self.codex, "-c", 'cli_auth_credentials_store="file"',
                       "-c", "sqlite_home=" + json.dumps(self.sqlite, ensure_ascii=False),
                       "-c", "check_for_update_on_startup=false", *overrides(port), "--no-daemon", *self.args]
            self.message(self.policy.describe())
            self.message("protected mode: model requests pass through a local gate; after a pause press F12, "
                         "then type the code shown, to request a fresh quota check")
            return sup.run(command, env, cwd=str(self.cwd))
        except supervise.Refused as error:
            refuse(str(error.reason))
        except self.sandbox.Refusal as error:
            refuse(str(error))
        finally:
            if sup is not None:
                sup.close()
            for path in (self.private, self.work):
                if path:
                    shutil.rmtree(path, ignore_errors=True)


EXPLAINED = {
    "identity_mismatch": "the quota reply names another ChatGPT account or user",
    "bearer_identity_mismatch": "the quota reply names another ChatGPT account or user",
    "identity_unattributed": "the quota reply names no ChatGPT account and user",
    "unsupported_plan": "the account's plan is not one whose windows protected mode governs",
    "monitor_unheard": "the quota monitor did not start",
}


def explain(reason, fields):
    """A preflight refusal for the terminal: the evaluator's own line when it gave one."""
    reason = str(reason)
    if reason.startswith("requirements_"):
        return BLOCKERS.get(reason[len("requirements_"):], BLOCKERS["requirements_unreadable"])
    detail = (fields or {}).get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return EXPLAINED.get(reason, "the quota check did not pass (%s)" % reason.replace("_", " "))
