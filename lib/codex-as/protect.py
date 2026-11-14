"""codex-as protected mode: usage reserves enforced around interactive Codex (launcher side).

A launch whose resolved policy (reserve.py) enables a reserve for either window runs through this
module; with both windows off codex-as never calls it and behaves exactly as without it. The query
forms (-h, --help, -V, --version and help, check.Plan.query) stay unprotected and unchanged, and so
do update and completion (check.Plan.rootless), which launch.py runs as plain codex in codex_home
before any protection: they are the same for every login and send no model request. Every other
subcommand refuses.

Refusals come first and change nothing (Session.check): the arguments an allowlisted interactive
TUI form, parsed like clap 0.167.2 (screen); the environment may name no other endpoint; the host
must carry the OS boundary; no name on the way to the native executable (or to the interpreter of a
#! script native) may lie in the boundary's reach, since a session could then replace the native
under the user's next plain codex run, and no PATH entry in that reach may come before the one that
supplies python3, codex or that interpreter, where a session could plant one (supervise.path_order);
the shared entries the writable set names beneath codex_home must be plain directories and files of
this user, not links (shared_entries); the native release, which `codex --version` reports from
inside a boundary of its own (native_version: the kit never runs the native outside a boundary),
must be a reviewed one (REVIEWED, with an upper bound); the account must already hold a saved
login (the native device login cannot run inside the boundary); the login must be a ChatGPT login
of a reviewed plan, not FedRAMP; the release's databases must exist in the shared SQLite home;
and no managed requirement (local requirements.toml, macOS managed preferences, the account's
cached cloud bundle) may forbid what protected mode sets (blockers).

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
The TUI and every confined reader keep the caller's umask, as unprotected Codex does. The gate
reaches the provider through the proxy Codex would use for HTTPS (gate_proxy), which neither the TUI
nor a reader sees.

The writable set is what the reviewed TUI, its tools, its App Server readers and the refresh write
(N: codex 0.167.2 under strace against loopback fixtures): the workspace, the account home
(codex_home/accounts/NAME, native's CODEX_HOME), the shared directories and files native writes
through the account home's links (SHARED_WRITABLE_DIRS, SHARED_WRITABLE_FILES), the release's
databases in the shared SQLite home as files (each NAME.sqlite with its -wal and -shm, which the
launcher creates when missing; the SQLite home is codex_home itself by default, and a directory
rule there would reach everything beneath it) and a private temporary directory; the refresh
adds secrets_dir, where native writes the refreshed credential. Everything else under codex_home
stays read-only: packages/ (Codex's standalone install), config.toml, hooks.json, mcp.json,
skills, rules, agents, themes, worktrees, memories, the rest of .tmp (plain codex's plugin staging
area), the other accounts and the root auth.json. None may be
HOME, an ancestor of HOME, HOME/Library or lie at or inside HOME/Library/LaunchAgents. The
boundary also leaves its devices writable (fixed_writable: /dev/shm and /dev/pts on Linux). The
readers' specifications live in another private directory, which must lie outside all of it (a
TMPDIR inside the workspace refuses), and so does the run gate's requirement record (one word, "none"
until a config bundle forbids protected mode), which a restarted gate starts from (gate.py); the
monitor's reader takes its boundary from the argv the launcher builds, never from a file.

The gate, the monitor and the monitor's reader run outside the boundary (the reader until it
confines itself), so nothing they load code from may be in its reach: they run the base
interpreter of the Python running codex-as (interpreter: a virtual environment's site-packages and
.pth files stay out), and the run refuses unless that interpreter, its prefixes, its module path
and the kit's module directories lie outside every writable path, through every link on the way
(unreachable). The launcher itself runs before any check, as the python3 that bin/codex-as finds
on PATH, started with -S so that no .pth file runs in it; the run refuses when that python3, its
prefixes or its module path lie in the boundary's reach (launcher_sources), since a session could
then replace them under the next launch. While the run lasts, commands the launcher itself runs by
name resolve only in the system directories (launcher_path).
"""

import contextlib
from dataclasses import dataclass
import base64
import datetime
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time

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
    databases: tuple         # the SQLite databases native keeps in sqlite_home (NAME.sqlite) from its start on
    later_databases: tuple   # ... and those it creates only with its first thread

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
    # N: what 0.167.2 opens in sqlite_home: five databases from every start on (its App Server does
    # not initialize without them) and thread_history_1 from the first thread on (resuming a thread
    # fails without it). Protected mode needs them all.
    databases=("state_5", "logs_2", "goals_1", "memories_1", "queue_1"),
    later_databases=("thread_history_1",),
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
    launcher adds once), or Refused (see scan)."""
    return scan(args, native)[0]


def scan(args, native):
    """(the arguments screen() forwards, the model they select or None), or Refused.

    An allowlist parsed like clap: `--name value`, `--name=value`, `-Xvalue` and `-X=value` for
    options with a value; a value from the next word never starts with '-'; before `--` every bare
    word that names a subcommand refuses, also after an option value or a prompt word (clap would
    run it), and so does a subcommand name given as an option value (stricter than clap)."""
    index, dashes, kept, chosen = 0, False, [], None

    def value_of(name, attached):
        nonlocal index
        if attached is not None:
            return attached
        if index >= len(args) or (args[index].startswith("-") and args[index] != "-"):
            refuse(f"{name} needs a value")
        index += 1
        return args[index - 1]

    def check(name, value):
        nonlocal chosen
        if value in native.subcommands:
            refuse(f"{name} {value}: a subcommand name is not accepted as an option value")
        if name in ("-c", "--config"):
            key = value.split("=", 1)[0].strip()
            if "=" not in value or not CONFIG_KEY.fullmatch(key):
                refuse("a -c override needs a plain dotted KEY=VALUE")
            if key.split(".", 1)[0] in PROTECTED_KEYS:
                refuse(f"protected mode sets {key.split('.', 1)[0]} itself; -c {key} is not accepted")
            if key == "model":
                chosen = value.split("=", 1)[1].strip().strip("\"'")
                model(chosen, native)
        elif name in ("-m", "--model"):
            chosen = value
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
    return kept, chosen


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
# Features overrides() turns off; a requirement that forces one on conflicts. respect_system_proxy
# would have native resolve system, PAC and WPAD proxy settings, which only the gate may use.
FORCED_OFF = ("plugins", "recommended_plugins", "apps", "enable_mcp_apps", "memories", "network_proxy",
              "code_mode_prewarm", "respect_system_proxy")
# The user configuration's keys for that feature (system_proxy_fallback is its older name).
SYSTEM_PROXY_FEATURES = ("respect_system_proxy", "system_proxy_fallback")
SANDBOX = "danger-full-access"
SYSTEM_REQUIREMENTS = Path("/etc/codex/requirements.toml")
SYSTEM_MANAGED_CONFIG = Path("/etc/codex/managed_config.toml")
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
    "managed_config": "the managed configuration (managed_config.toml or MDM config_toml_base64), which "
                      "overrides command-line settings, sets what protected mode must set itself",
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


def read_document(path, limit=DOCUMENT_LIMIT, reason=BLOCKERS["requirements_unreadable"]):
    """Bytes of a regular file of at most LIMIT bytes, None when absent; Refused (REASON and the
    path) otherwise. Links are followed, as native follows them (dotfile managers link config.toml)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        refuse(reason + f" ({path})")
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            refuse(reason + f" ({path})")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        refuse(reason + f" ({path})")
    return raw


def managed_preferences():
    """The forced com.openai.codex managed preferences of macOS as (path, plist dict), else []."""
    if sys.platform != "darwin":
        return []
    found = []
    user = pwd.getpwuid(os.getuid()).pw_name
    for path in (MANAGED_PREFERENCES / user / MANAGED_DOMAIN, MANAGED_PREFERENCES / MANAGED_DOMAIN):
        raw = read_document(path)
        if raw is None:
            continue
        try:
            document = plistlib.loads(raw)
        except (ValueError, plistlib.InvalidFileException):
            document = None
        if not isinstance(document, dict):
            refuse(BLOCKERS["requirements_unreadable"] + f" ({path})")
        found.append((path, document))
    return found


def preference_toml(path, document, key):
    """The TOML table a managed preference holds base64-encoded under KEY ({} when unset)."""
    encoded = document.get(key)
    try:
        table = {} if encoded is None else toml_document(base64.b64decode(encoded.strip()).decode("utf-8"))
    except (ValueError, AttributeError, TypeError, UnicodeError):
        table = None
    if table is None:
        refuse(BLOCKERS["requirements_unreadable"] + f" ({path})")
    return table


def toml_file(path):
    raw = read_document(path)
    if raw is None:
        return None
    table = toml_document(raw.decode("utf-8", "replace"))
    if table is None:
        refuse(BLOCKERS["requirements_unreadable"] + f" ({path})")
    return table


def local_requirements():
    """The requirements documents native composes around the cloud layers, as (where, table):
    /etc/codex/requirements.toml and, on macOS, the forced com.openai.codex managed preference."""
    found = []
    table = toml_file(SYSTEM_REQUIREMENTS)
    if table is not None:
        found.append((str(SYSTEM_REQUIREMENTS), table))
    for path, document in managed_preferences():
        found.append((str(path), preference_toml(path, document, "requirements_toml_base64")))
    return found


def managed_configs():
    """The legacy managed configuration layers, which take precedence over -c session flags
    (codex-rs config_layer_source.rs: SessionFlags 30, managed_config.toml 40, MDM 50), as
    (where, table): /etc/codex/managed_config.toml and, on macOS, the forced config_toml_base64.
    The cloud bundle's config layer (EnterpriseManaged, 15) ranks below -c and cannot override it."""
    found = []
    table = toml_file(SYSTEM_MANAGED_CONFIG)
    if table is not None:
        found.append((str(SYSTEM_MANAGED_CONFIG), table))
    for path, document in managed_preferences():
        found.append((str(path), preference_toml(path, document, "config_toml_base64")))
    return found


def flatten(table, prefix=""):
    """(dotted key, value) for every leaf of a TOML table; an empty table is a leaf."""
    for name, value in table.items():
        key = prefix + str(name)
        if isinstance(value, dict) and value:
            yield from flatten(value, key + ".")
        else:
            yield key, value


def forced_settings(sqlite_home):
    """What protected mode sets with -c, as {dotted key: value} (the gate URLs name port 0 here, so
    a managed URL never equals one)."""
    forced = {}
    for pair in fixed(sqlite_home) + settings(0):
        forced.update(flatten(tomllib.loads(pair)))
    return forced


def managed_conflicts(table, forced):
    """The keys of one managed configuration table that would override what protected mode sets:
    a different value for a key it sets, a table or value above or below one, or anything for its
    own provider entry."""
    found = set()
    own = "model_providers.%s" % PROVIDER
    for key, value in flatten(table):
        if key == own or key.startswith(own + "."):
            found.add(own)
            continue
        for name, setting in forced.items():
            if (key == name and value != setting) or key.startswith(name + ".") or name.startswith(key + "."):
                found.add(name)
    return found


def cache_current(payload, identity, now=None):
    """Whether native would load a cloud bundle cache payload: version 1, the login's identity
    (when known) and an expiry in the future (codex-rs cloud-config/src/cache.rs; an expiry this
    check cannot read counts as current, so the cache is checked). The signature is not checked:
    a cache native rejects is refetched through the gate, which checks the bundle again."""
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return False
    user, account = payload.get("chatgpt_user_id"), payload.get("account_id")
    if not (isinstance(user, str) and isinstance(account, str)):
        return False
    if identity is not None and (account, user) != (identity.account, identity.user):
        return False
    expires = payload.get("expires_at")
    try:
        text = re.sub(r"(\.\d{6})\d+", r"\1", expires).replace("Z", "+00:00")
        moment = datetime.datetime.fromisoformat(text)
        if moment.tzinfo is None:
            return True
    except (TypeError, ValueError):
        return True
    return moment.timestamp() > (time.time() if now is None else now)


def requirement_check(account_home, sqlite_home, identity=None):
    """Refused when a local requirements document, a managed configuration layer or the account's
    current cloud bundle cache forbids protected mode. An expired cache, or one of another login,
    is ignored as native ignores it (native then fetches the bundle through the gate, which checks
    it again); a current one is what native will run under, without a fetch."""
    found, notes = set(), []
    for _, table in local_requirements():
        layer = requirement_layer(table, cloud=False)
        found |= {"requirements_unreadable"} if layer is None else blockers(layer, sqlite_home, cloud=False)
    forced = forced_settings(sqlite_home)
    for where, table in managed_configs():
        keys = managed_conflicts(table, forced)
        if keys:
            found.add("managed_config")
            notes.append("%s sets %s" % (where, ", ".join(sorted(keys))))
    cache = Path(account_home) / BUNDLE_CACHE
    raw = read_document(cache)
    if raw is not None:
        try:
            document = json.loads(raw)
            payload = document.get("signed_payload") if isinstance(document, dict) else None
        except ValueError:
            payload = None
        if cache_current(payload, identity):
            cached = bundle_blockers(payload.get("bundle"), sqlite_home)
            if cached - found:
                notes.append("from the cached cloud requirements in %s, which native uses until they expire; "
                             "an unprotected launch refreshes them" % cache)
            found |= cached
    if found:
        refuse("; ".join(BLOCKERS[reason] for reason in sorted(found)) + "".join(" (%s)" % note for note in notes))


# -- native configuration and write rules -----------------------------------------------------------

PROVIDER = "quota_gate"


def fixed(sqlite_home):
    """codex-as's own -c pairs for every launch of an account, protected or not."""
    return ['cli_auth_credentials_store="file"', "sqlite_home=" + json.dumps(str(sqlite_home), ensure_ascii=False),
            "check_for_update_on_startup=false"]


def settings(port):
    """The -c pairs protected mode adds after codex-as's fixed ones: every model, quota and realtime
    URL at the gate, no native sandbox, and the reviewed quiet set (no analytics, telemetry, web
    search, plugins, apps, memories or network proxy) for the TUI and every App Server reader
    alike. web_search="disabled" is always allowed: native adds Disabled to every
    allowed_web_search_modes requirement (codex-rs config/src/config_requirements.rs)."""
    gate = "http://127.0.0.1:%d" % port
    return [
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


def overrides(port):
    """settings(port) as -c words."""
    return [word for pair in settings(port) for word in ("-c", pair)]


def selected_profile(table):
    """(name, table) of the profile a user config.toml selects with profile = NAME, else (None, {})."""
    profiles, chosen = table.get("profiles"), table.get("profile")
    selected = profiles.get(chosen) if isinstance(chosen, str) and isinstance(profiles, dict) else None
    return (chosen, selected) if isinstance(selected, dict) else (None, {})


def configured_model(table):
    """The model a user config.toml selects: its selected profile's model, else its own (native
    ranks the profile layer above the file)."""
    profile = selected_profile(table)[1]
    return profile["model"] if "model" in profile else table.get("model")


def within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def resolution(path, hops=40):
    """Every absolute name that resolving PATH looks up, in order: each leading directory and,
    through each link, the names of its target, ending with the real path. A link anywhere on the
    way can be replaced when its directory is writable, so every name counts, not only the real
    path."""
    path = str(path)
    names, current = [], "/"
    parts = list(Path(path if os.path.isabs(path) else os.path.join(os.getcwd(), path)).parts[1:])
    while parts:
        part = parts.pop(0)
        if part == "..":
            current = os.path.dirname(current)
            continue
        name = os.path.join(current, part)
        names.append(name)
        try:
            target = Path(os.readlink(name)) if hops and os.path.islink(name) else None
        except OSError:
            target = None
        if target is None:
            current = name
            continue
        hops -= 1
        parts[:0] = target.parts[1:] if target.is_absolute() else target.parts
        current = "/" if target.is_absolute() else current
    return names


def outside(path, writable_paths):
    """Refused unless PATH lies outside every (resolved) writable path, through every link."""
    for root in writable_paths:
        if any(within(name, root) for name in resolution(path)):
            refuse(f"the temporary directory {os.path.dirname(os.path.realpath(str(path)))} lies inside {root}, "
                   "which the boundary can write; set TMPDIR to a directory outside the workspace and the Codex "
                   "homes")


def fixed_writable(sandbox):
    """What the boundary leaves writable besides the writable set: the Linux boundary's DEVICES
    (/dev/pts and /dev/shm included) or the macOS device rules, which all lie beneath /dev."""
    if sys.platform == "darwin":
        return ["/dev"]
    return [os.path.realpath(device) for device in sandbox.DEVICES]


def interpreter():
    """The Python that kit processes outside the boundary run, as a real path: the base installation
    of the running one, so a virtual environment's site-packages and .pth files, which a venv in
    the workspace puts in reach of the boundary, are never processed outside it."""
    return os.path.realpath(getattr(sys, "_base_executable", None) or sys.executable)


CODE_PROBE = ("import json, sys; print(json.dumps([sys.prefix, sys.exec_prefix, sys.base_prefix, "
              "sys.base_exec_prefix, sys.path]))")
CODE_PROBE_TIMEOUT = 10.0
CODE_HINT = "; run codex-as with a Python and a kit installed outside the workspace and the Codex homes"


def code_sources(python, env):
    """(what, path, holds code) for everything a kit process outside the boundary loads code from:
    PYTHON, its prefixes, the module path it starts with as `PYTHON -I -B` (site-packages and what
    their .pth files add included, as the monitor runs it) and the kit's two module directories.
    Refused when PYTHON cannot report them."""
    try:
        done = subprocess.run([python, "-I", "-B", "-c", CODE_PROBE], env=env, cwd="/", stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=CODE_PROBE_TIMEOUT)
        *prefixes, entries = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        prefixes = entries = None
    if not (isinstance(entries, list) and len(prefixes) == 4
            and all(isinstance(path, str) and os.path.isabs(path) for path in prefixes + entries)):
        refuse(f"the Python at {python} did not report where it loads code from")
    return ([("the Python", python, False)] + [("the Python prefix", path, False) for path in prefixes]
            + [("the Python module path entry", path, True) for path in entries]
            + [("the kit directory", str(path), True) for path in (HERE, KIT)])


LAUNCHER_HINT = ("; start codex-as with a python3 outside the workspace and the Codex homes first on PATH "
                 "(deactivate a workspace virtual environment)")


def launcher_sources():
    """(what, path, holds code) for the launcher process itself, which runs outside every boundary
    before any check: the python3 that bin/codex-as found on PATH, by the name it ran (so every link
    on the way counts), its prefixes and its module path, which holds no site-packages: bin/codex-as
    starts it as `python3 -I -B -S`. A protected session that could replace one of them would run
    the next launch's code unconfined, so protected mode refuses such a launch. A python3 replaced
    before this launch has run instead of it; the check cannot see that. Refused when the
    interpreter does not report them."""
    prefixes = [sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix]
    entries = list(sys.path)
    if not all(isinstance(path, str) and os.path.isabs(path) for path in [sys.executable, *prefixes, *entries]):
        refuse("the Python running codex-as does not report where it loads code from")
    return ([("the launcher's Python", sys.executable, False)]
            + [("the launcher's Python prefix", path, False) for path in prefixes]
            + [("the launcher's module path entry", path, True) for path in entries])


def unreachable(sources, writable_paths, hint=CODE_HINT):
    """Refused (reason ending in HINT) when a code source (code_sources, launcher_sources), or a link
    on the way to it, lies inside a writable path, or a writable path lies inside a directory of
    code."""
    for what, path, holds in sources:
        names, real = resolution(path), os.path.realpath(path)
        for root in writable_paths:
            if any(within(name, root) for name in names):
                refuse(f"{what} {path} lies inside {root}, which the boundary can write" + hint)
            if holds and within(root, real):
                refuse(f"{root}, which the boundary can write, lies inside {what} {path}" + hint)


SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@contextlib.contextmanager
def launcher_path():
    """The launcher's own PATH while boundary processes may live: a command it runs by name outside
    the boundary (the supervisor's ps on macOS) never resolves in a directory of the caller's PATH,
    which the boundary may be able to write. The native and the readers keep the caller's PATH
    (Session.env)."""
    saved = os.environ.get("PATH")
    os.environ["PATH"] = SYSTEM_PATH
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = saved


def gate_proxy(supervise, parse, env, darwin=None):
    """The caller's proxy for the gate's upstream connections, as Codex itself would use it for
    HTTPS: HTTPS_PROXY, otherwise on macOS the system HTTPS proxy (scutil --proxy, read outside the
    boundary), otherwise ALL_PROXY, each variable in either spelling (HTTP_PROXY serves only http://
    URLs); None to connect directly. supervise.capture_proxy raises its Refused for a value the gate
    cannot use (parse is gate.parse_proxy)."""
    darwin = sys.platform == "darwin" if darwin is None else darwin
    return supervise.capture_proxy(env, parse, provider="codex", system=supervise.macos_proxies if darwin else None)


# What native writes under codex_home besides the account home, through the account home's links
# (layout.py), as the reviewed release does (N: 0.167.2 under strace). Everything else there stays
# read-only: memories and memories_v2, which protected mode turns off and native never wrote, and
# the rest of .tmp, plain codex's plugin staging area (plugins, plugins.sha, marketplaces), which
# its later starts copy plugin code from. The databases come from Native.databases, as files
# (DATABASE_FILES suffixes).
SHARED_WRITABLE_DIRS = ("sessions", "archived_sessions", "rollout-migrations", "thread-writer-locks")
SHARED_WRITABLE_FILES = ("history.jsonl", "session_index.jsonl", ".tmp/rollout-maintenance.lock")
DATABASE_FILES = (".sqlite", ".sqlite-wal", ".sqlite-shm")
NATIVE_HINT = ("; protected mode refuses a Codex that a protected session could replace, because the next "
               "plain codex would run it unconfined: install Codex outside the workspace and the directories "
               "the boundary writes")
PATH_HINT = "; move that entry after the one that supplies the command on PATH, or drop it"


def no_login(name):
    """Refused: protected mode never runs the native device login, which needs more network than the
    boundary allows, and never runs it outside the boundary either."""
    refuse(f"the account {name} has no saved login yet, and protected mode never runs the native login, which "
           "needs more network than the boundary allows; log in with one launch without reserves first")


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


def moved(paths, refusal):
    """Raise REFUSAL("path", ...) when a writable path, which the launcher resolved once, no longer
    resolves to itself: a process inside the boundary can replace a writable path nested in another
    one with a link (to HOME, say), which would widen the boundary of every later confined start (the
    monitor's readers, a continuation's read). Every confined start checks it first."""
    for path in paths:
        if os.path.realpath(path) != path:
            raise refusal("path", f"the writable path {path} no longer resolves to itself; a process inside the "
                                  "boundary may have replaced it with a link")


# Caller variables an App Server reader keeps; everything else stays with the TUI alone.
READER_ENV = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SSL_CERT_FILE",
              "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE", "CODEX_HOME", "CODEX_SQLITE_HOME", "TMPDIR")
REFRESH_HOSTS = {"auth.openai.com:443": ["auth.openai.com", 443]}
REFRESH_TIMEOUT = 30.0
REFRESH_LOCK_WAIT = REFRESH_TIMEOUT + 10.0  # another launch's refresh ends within its own bound
READ_TIMEOUT = 45.0       # a reader allows native's bundle retries (D14: up to 40 s if it stalls)
VERSION_TIMEOUT = 20.0
VERSION_LIMIT = 4096
CONTINUE_READ_TIMEOUT = 25.0  # a continuation read must answer within the 30 s continuation window
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
    the supervised TUI. guard is check.py, storage layout.py, reserve reserve.py. umask is the
    caller's file mode creation mask (launch.py's CALLER_UMASK; the launcher itself runs under
    077), which the TUI and every confined reader get, as unprotected Codex does; None keeps 077."""

    def __init__(self, guard, storage, reserve, config, layout, policy, args, codex, env, sqlite, cwd, umask=None):
        self.guard, self.storage, self.reserve, self.config = guard, storage, reserve, config
        self.layout, self.policy, self.given = layout, policy, list(args)
        self.codex, self.env, self.sqlite, self.cwd = codex, dict(env), str(sqlite), Path(cwd)
        self.umask = umask
        self.native = self.args = None  # check() reads the release and screens the arguments with its tables
        self.sandbox = self.supervise = self.kit_gate = None
        self.private = self.work = None
        self.login = self.baseline = None

    # -- refusals before anything changes ------------------------------------------------------------
    def check(self):
        # The arguments fail first when no reviewed release accepts them; the release's own tables
        # screen them again once native_version() has read it.
        failures = []
        for native in sorted(REVIEWED.values(), key=lambda item: item.version, reverse=True):
            try:
                screen(self.given, native)
                break
            except Refused as error:
                failures.append(error)
        else:
            raise failures[0]
        environment(self.env)
        try:
            self.supervise = load("supervise", KIT / "supervise.py")
            self.kit_gate = load("gate", KIT / "gate.py")
            module = "sandbox_macos" if sys.platform == "darwin" else "sandbox_linux"
            self.sandbox = load(module, KIT / (module + ".py"))
        except (OSError, ImportError, SyntaxError):
            refuse(f"incomplete install at {HERE.parent.parent}")
        if not (GATE.is_file() and READER.is_file() and (KIT / "monitor.py").is_file()):
            refuse(f"incomplete install at {HERE.parent.parent}")
        try:
            self.sandbox.probe()
        except self.sandbox.Refusal as error:
            refuse(str(error))
        reach = self.reach()
        try:
            unreachable(self.supervise.native_sources(self.codex, self.env, "the Codex executable"), reach,
                        NATIVE_HINT)
            unreachable(launcher_sources(), reach, LAUNCHER_HINT)
            self.supervise.path_order(self.lookups(), self.env, reach, PATH_HINT)
        except self.supervise.Refused as error:
            refuse(error.reason)
        self.shared_entries(created=False)
        self.native = reviewed(self.native_version())
        self.args = screen(self.given, self.native)
        if not os.isatty(0):
            refuse("protected mode needs an interactive terminal for continuation")
        if not os.path.lexists(self.layout.target):
            no_login(self.layout.name)
        identity = login(self.guard.credential(self.layout.target), self.native, self.storage)
        missing = [path for path in self.database_files() if path.suffix == ".sqlite" and not path.is_file()]
        if missing:
            later = all(path.stem in self.native.later_databases for path in missing)
            refuse(f"native Codex has not created {', '.join(path.name for path in missing)} in {self.sqlite} yet, "
                   "and the boundary lets it write its databases but not create them; "
                   + ("send one prompt in a Codex session without reserves first (native creates it with its first "
                      "thread)" if later else "start Codex once without reserves first"))
        # The model of the user config and its selected profile, unless -m or -c model= replaces it;
        # project and managed layers can select another one, which the gate refuses per request.
        path = self.config.codex_home / "config.toml"
        raw = read_document(path, reason="the Codex configuration cannot be read")
        table = (toml_document(raw.decode("utf-8", "replace")) or {}) if raw else {}
        # Native may rank a selected profile above the -c overrides (U for 0.167.2; older codex-rs did),
        # so a profile that sets what protected mode sets refuses instead of failing inside the boundary.
        name, profile = selected_profile(table)
        keys = sorted(PROTECTED_KEYS & set(profile))
        if keys:
            refuse(f"the profile {shown(name)} that {path} selects sets {', '.join(keys)}, which protected mode "
                   "sets itself")
        features = table.get("features")
        if isinstance(features, dict) and any(features.get(name) is True for name in SYSTEM_PROXY_FEATURES):
            refuse(f"{path} enables features.respect_system_proxy (system, PAC and WPAD proxy settings), which the "
                   "gate cannot follow; set HTTPS_PROXY to the proxy to use instead")
        configured = configured_model(table)
        overridden = scan(self.args, self.native)[1] is not None
        if not overridden and isinstance(configured, str) and configured not in self.native.models:
            refuse(f"the model {shown(configured)} that {path} selects is not one whose quota protected mode "
                   "governs (choose a reviewed one with -m)")
        requirement_check(self.layout.home, self.sqlite, identity)

    def native_version(self):
        """What `codex --version` prints, run inside a boundary of its own: nothing writable but the
        boundary's devices, and a held port that nothing serves as its only exit, in /, with stdin
        and stderr on /dev/null. Refused unless it exits 0 and its output ends within VERSION_TIMEOUT:
        its remaining descendants end before the output is read, and the read is bounded too."""
        supervise, python = self.supervise, interpreter()
        darwin = sys.platform == "darwin"
        read_end, write_end = os.pipe()
        sup = None
        try:
            extra = self.sandbox.holder_addresses().reserve if darwin else ()
            sup = supervise.Supervisor([python, "-I", "-B", str(GATE)], {}, python=python, holder_extra=extra)
            port = sup.prepare()
            sup.confine = self.hook([], self.sandbox.self_test(port, [], "codex", python=python) if darwin else None)
            deadline = time.monotonic() + VERSION_TIMEOUT
            with launcher_path():
                code = sup.confined([self.codex, "--version"], self.env, timeout=VERSION_TIMEOUT, cwd="/",
                                    stdout=write_end)
                sup.sweep()  # a descendant that kept the output open ends with the rest of the boundary
            os.close(write_end)
            write_end = None
            output = b""
            while len(output) <= VERSION_LIMIT:
                if not select.select([read_end], [], [], max(0.0, deadline - time.monotonic()))[0]:
                    code = None
                    break
                chunk = os.read(read_end, 4096)
                if not chunk:
                    break
                output += chunk
        except supervise.Refused as error:
            refuse("the Codex version check could not run inside the boundary: %s" % error.reason)
        except self.sandbox.Refusal as error:
            refuse(str(error))
        finally:
            if sup is not None:
                sup.close()
            for fd in (read_end, write_end):
                if fd is not None:
                    os.close(fd)
        if code != 0 or len(output) > VERSION_LIMIT:
            refuse("the Codex version check inside the boundary %s"
                   % ("timed out" if code is None else "failed" if code else "printed too much"))
        return output.decode("utf-8", "replace")

    # -- the run --------------------------------------------------------------------------------------
    def database_files(self):
        """The release's databases in the shared SQLite home, NAME.sqlite, -wal and -shm each (before
        check() has read the release, the newest reviewed one's)."""
        native = self.native or REVIEWED[max(REVIEWED)]
        return [Path(self.sqlite) / (name + suffix) for name in (*native.databases, *native.later_databases)
                for suffix in DATABASE_FILES]

    def paths(self, refresh=False):
        """The writable set (refresh adds secrets_dir, where native writes the credential)."""
        root = self.config.codex_home
        paths = [self.cwd, self.layout.home, *(root / name for name in SHARED_WRITABLE_DIRS),
                 *(root / name for name in SHARED_WRITABLE_FILES), *self.database_files()]
        if self.work is not None:
            paths.append(self.work)
        if refresh:
            paths.append(self.config.secrets_dir)
        return writable(self.config.home, paths)

    def reach(self):
        """Everything a boundary process of this run can write: both writable sets and the devices."""
        return self.paths() + self.paths(refresh=True) + fixed_writable(self.sandbox)

    def lookups(self):
        """(name, label) of the commands run by name through the caller's PATH outside the boundary,
        which no PATH entry the boundary can write may shadow (supervise.path_order): the launcher's
        python3 (bin/codex-as runs it by name), codex when the configuration names it by name (the
        next launch and a plain codex alike) and the command a `#!/usr/bin/env NAME` codex runs."""
        found = [("python3", "python3")]
        name = getattr(self.config, "codex", "codex")
        if "/" not in name:
            found.append((name, name))
        try:
            command = self.supervise.script_interpreter(self.codex)[1]
        except self.supervise.Unfollowed:  # native_sources accepted it a moment ago
            refuse("the #! line of the Codex executable changed during the launch")
        if command is not None:
            found.append((command, "%s (the interpreter of the Codex executable)" % command))
        return found

    def shared_entries(self, created=True):
        """Refused unless each shared entry the writable set names beneath codex_home
        (SHARED_WRITABLE_DIRS, SHARED_WRITABLE_FILES and their parents below codex_home) is a
        directory or regular file of this user, not a link: every launch resolves them again, and on
        macOS, whose rule for a path also covers the name itself, a session can replace one with a
        link, which would make the link's target writable in the next launch. With created false
        (before the launcher lays out the account) a missing entry passes: the launcher creates it."""
        root = self.config.codex_home
        entries = [(name, "a directory") for name in SHARED_WRITABLE_DIRS]
        entries += [(name, "a regular file") for name in SHARED_WRITABLE_FILES]
        for name, kind in entries:
            path, parts = Path(root), Path(name).parts
            for index, part in enumerate(parts):
                path = path / part
                want = kind if index == len(parts) - 1 else "a directory"
                try:
                    info = os.lstat(path)
                except FileNotFoundError:
                    if created:
                        refuse(f"{path} is missing; start Codex once without reserves to create it")
                    break
                except OSError:
                    info = None
                test = stat.S_ISDIR if want == "a directory" else stat.S_ISREG
                if info is None or not test(info.st_mode) or info.st_uid != os.getuid():
                    found = "a link, not " if info is not None and stat.S_ISLNK(info.st_mode) else "not "
                    refuse(f"{path} is {found}{want} of this user: protected mode makes it writable, and through a "
                           "link the boundary would make the link's target writable instead (a protected session can "
                           f"put one there on macOS); make it {want} again")

    def prepare_databases(self):
        """Create the -wal and -shm files native would create beside each database, as SQLite does
        (empty, with the database's mode), so that the boundary can name them as files. Each of the
        three must be a regular file of this user, not a link (SQLite opens them O_NOFOLLOW, and the
        boundary would make a link's target writable)."""
        for path in self.database_files():
            if path.suffix != ".sqlite":
                try:
                    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                except FileExistsError:
                    fd = None
                if fd is not None:
                    try:
                        os.fchmod(fd, mode)
                    finally:
                        os.close(fd)
            try:
                info = os.lstat(path)
            except OSError:
                info = None
            if info is None or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                refuse(f"the database file {path} is not a regular file of this user")
            if path.suffix == ".sqlite":
                mode = stat.S_IMODE(info.st_mode)

    def hook(self, paths, tested):
        sandbox = self.sandbox

        def confine(port, argv):
            moved(paths, sandbox.Refusal)
            if sys.platform == "darwin":
                return sandbox.confine(port, paths, "codex", tested=tested) + list(argv)
            return sandbox.confine(port, paths, "codex") + list(argv)

        return confine

    def codex_argv(self, port, *words):
        """The codex argv with codex-as's fixed -c pairs and protected mode's, then WORDS."""
        return [self.codex, *(word for pair in fixed(self.sqlite) for word in ("-c", pair)), *overrides(port), *words]

    def app_server(self, port):
        return self.codex_argv(port, "app-server", "--listen", "stdio://")

    def reader_env(self, port, refresh=False):
        env = {name: self.env[name] for name in READER_ENV if name in self.env}
        env.update(CODEX_HOME=str(self.layout.home), CODEX_SQLITE_HOME=self.sqlite, TMPDIR=self.work)
        if refresh:
            env.update(HTTPS_PROXY="http://127.0.0.1:%d" % port, https_proxy="http://127.0.0.1:%d" % port)
        return env

    def requirement_record(self):
        """The run gate's requirement record in the private directory, created holding "none"."""
        path = Path(self.private) / "requirement"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write("none\n")
        return str(path)

    def spec(self, name, port, mode, timeout):
        """A reader's specification, in the private directory outside every writable path."""
        spec = {"command": self.app_server(port), "env": self.reader_env(port, mode == "refresh"),
                "cwd": str(self.cwd), "mode": mode, "timeout": timeout}
        path = Path(self.private) / (name + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(spec, stream)
        return str(path)

    def message(self, text):
        print(self.guard.styled("codex-as: " + text, "36", 2), file=sys.stderr)

    @contextlib.contextmanager
    def refresh_lock(self):
        """The account's refresh lock, held across the pre-launch refresh, so that two protected
        launches of one account never present the same refresh token (unprotected clients do not
        take it)."""
        path = self.config.codex_home / "accounts/.locks" / (".refresh-%s.lock" % self.layout.name)
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except OSError:
            refuse(f"the account's refresh lock {path} cannot be opened")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                refuse(f"the account's refresh lock {path} is not a regular file of this user")
            deadline = time.monotonic() + REFRESH_LOCK_WAIT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        refuse("another launch of this account is still refreshing its credential")
                    time.sleep(0.1)
            yield
        finally:
            os.close(fd)

    def run(self, baseline):
        """The whole protected invocation; returns the native's wait status. self.baseline is then
        the credential post-run repair compares with: the refreshed one once the refresh passed."""
        with launcher_path():
            return self.protected(baseline)

    def protected(self, baseline):
        supervise, python = self.supervise, interpreter()
        self.baseline = baseline
        self.login = login(baseline, self.native, self.storage)
        before = last_refresh(baseline)
        sup = None
        try:
            self.shared_entries()  # laid out now; the boundary resolves them below
            self.prepare_databases()
            self.work = tempfile.mkdtemp(prefix="codex-as-tmp-")
            self.private = tempfile.mkdtemp(prefix="codex-as-protect-")
            paths, refresh_paths = self.paths(), self.paths(refresh=True)
            reach = paths + refresh_paths + fixed_writable(self.sandbox)
            unreachable(supervise.native_sources(self.codex, self.env, "the Codex executable"), reach, NATIVE_HINT)
            supervise.path_order(self.lookups(), self.env, reach, PATH_HINT)
            # Reader specifications, and the code of every kit process outside the boundary, must
            # stay out of reach of every boundary process.
            outside(self.private, reach)
            gate_env = {name: self.env[name] for name in ("SSL_CERT_FILE", "SSL_CERT_DIR", "LANG") if name in self.env}
            unreachable(launcher_sources(), reach, LAUNCHER_HINT)
            unreachable(code_sources(python, gate_env), reach)
            proxy = gate_proxy(supervise, self.kit_gate.parse_proxy, self.env)
            reserves = {setting.window: [None if setting.reserve is None else str(setting.reserve), setting.source]
                        for setting in self.policy.settings()}
            config = {"max_age_s": MAX_AGE, "heartbeat_max_s": HEARTBEAT_MAX,
                      "proxy": list(proxy) if proxy else None, "account": self.layout.name,
                      "expected": [self.login.account, self.login.user], "reserves": reserves,
                      "native": self.native.name, "sqlite_home": self.sqlite, "mode": "run"}
            refresh_config = dict(config, mode="refresh", connect_allow=REFRESH_HOSTS)
            # The run gate's requirement record, out of every boundary process's reach with the reader
            # specifications: a restarted gate keeps a requirement pause until a later bundle lifts it.
            config["requirement_file"] = self.requirement_record()
            extra = self.sandbox.holder_addresses().reserve if sys.platform == "darwin" else ()
            sup = supervise.Supervisor([python, "-I", "-B", str(GATE)], config, refresh_config=refresh_config,
                                       python=python, monitor_reader_timeout=READ_TIMEOUT, holder_extra=extra,
                                       gate_env=gate_env, style=lambda text: self.guard.styled(text, "33", 1),
                                       umask=self.umask)
            port = sup.prepare()
            tested = refresh_tested = None
            if sys.platform == "darwin":
                tested = self.sandbox.self_test(port, paths, "codex", python=python)
                refresh_tested = self.sandbox.self_test(port, refresh_paths, "codex", python=python)
            read_spec = self.spec("read", port, "read", READ_TIMEOUT)
            continue_spec = self.spec("continue", port, "read", CONTINUE_READ_TIMEOUT)
            poll_spec = self.spec("poll", port, "read", READ_TIMEOUT)
            refresh_spec = self.spec("refresh", port, "refresh", REFRESH_TIMEOUT)
            reader = [python, "-I", "-B", str(READER)]
            # The monitor runs outside the boundary: its reader takes the boundary from this argv.
            limits = {"port": port, "writable": paths, "profile": tested.profile if tested is not None else None}
            sup.monitor_reader = reader + ["--confine", json.dumps(limits), poll_spec]

            # D1 (a): the native refresh, confined, with the refresh gate as its only exit.
            sup.confine = self.hook(refresh_paths, refresh_tested)
            with self.refresh_lock():
                code = sup.refresh(reader + [refresh_spec], self.reader_env(port, True),
                                   timeout=REFRESH_TIMEOUT + 5, cwd=str(self.cwd))
            sup.confine = self.hook(paths, tested)
            # The refresh gate pauses on a config bundle that forbids protected mode; its report
            # is the clearer reason, and nothing of it may reach the run's relay.
            events, sup.events = sup.events, []
            for event in events:
                reason = str(event.get("reason")) if event.get("op") == "paused" else ""
                if reason.startswith("requirements_"):
                    refuse(explain(reason, {}))
            requirement_check(self.layout.home, self.sqlite, self.login)  # native may have cached a new bundle
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
                       % (reader_status(code) if code else "the credential is unchanged"))
            if login(current, self.native, self.storage) != self.login:
                refuse("the refreshed login names another workspace, user or plan")
            self.baseline = current  # native wrote it through the link; a later replacement is newer

            def preflight(supervisor):
                code = supervisor.confined(reader + [read_spec], self.reader_env(port), timeout=READ_TIMEOUT + 5,
                                           cwd=str(self.cwd))
                status = supervisor.request("status", 10)
                if not status.get("running"):
                    reason = status.get("reason") or "no fresh passing quota check"
                    detail = "" if code == 0 else " (quota read: %s)" % reader_status(code)
                    raise supervise.Refused(explain(reason, status.get("fields")) + detail)

            def continue_read():
                return sup.confined(reader + [continue_spec], self.reader_env(port), timeout=CONTINUE_READ_TIMEOUT + 5,
                                    cwd=str(self.cwd)) == 0

            sup.preflight, sup.reader = preflight, continue_read
            env = dict(self.env, CODEX_HOME=str(self.layout.home), CODEX_SQLITE_HOME=self.sqlite, TMPDIR=self.work)
            command = self.codex_argv(port, "--no-daemon", *self.args)
            self.message(self.policy.describe())
            self.message("protected mode: model requests pass through a local gate; after a pause press F12, "
                         "then type the code shown, to request a fresh quota check")
            # The native starts in the launcher's directory, as an unprotected launch does, so it
            # resolves a relative -C once; readers, which get no -C, start in the workspace.
            return sup.run(command, env)
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


# reader.py exit statuses, for refusal messages.
READER_STATUS = {
    2: "the App Server did not start", 3: "the App Server did not initialize", 4: "the request failed",
    5: "the boundary refused the reader", 6: "the reader's specification was unusable",
    7: "native's managed network policy is unavailable (policy-unavailable)",
    8: "native's managed network policy denies the gate (policy-destination)",
    9: "native's managed network permission was revoked (policy-revoked)",
    10: "native's managed network restrictions disable this transport (policy-unsupported-transport)",
}


def reader_status(code):
    return "timed out" if code is None else READER_STATUS.get(code, "ended with status %d" % code)


EXPLAINED = {
    "identity_mismatch": "the quota reply names another ChatGPT account or user",
    "bearer_identity_mismatch": "the quota reply names another ChatGPT account or user",
    "identity_unattributed": "the quota reply names no ChatGPT account and user",
    "unsupported_plan": "the account's plan is not one whose windows protected mode governs",
    "monitor_unheard": "the quota monitor did not start",
}
# Codex's own conditions, which the evaluator's line names only by their reason word.
OWN_CONDITIONS = {
    "limit_reached": "the quota reply reports a usage limit reached (limit_reached or allowed false) that no "
                     "five-hour or weekly window at 100% accounts for",
}


def explain(reason, fields):
    """A preflight refusal for the terminal: the evaluator's own line when it gave one."""
    reason = str(reason)
    if reason.startswith("requirements_"):
        return BLOCKERS.get(reason[len("requirements_"):], BLOCKERS["requirements_unreadable"])
    if reason in OWN_CONDITIONS:
        return OWN_CONDITIONS[reason]
    detail = (fields or {}).get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return EXPLAINED.get(reason, "the quota check did not pass (%s)" % reason.replace("_", " "))
