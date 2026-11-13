"""claude-as protected mode: the policy evaluator for Claude Code's settings and server-managed policy.

Load this file by path (it is never on sys.path). Pure apart from the bounded file reads; it keeps
key names and verdicts only, never a value.

Server-managed policy (reviewed design D15): native Claude Code caches the organization's policy in
the account's configuration directory as remote-settings.json (a settings document) and
policy-limits.json (feature limits). Protected mode starts native, so both are judged in two scopes:

    native   the whole settings review: credential helpers, auth, transport and login pins, policy
             helpers, refused environment names, and keys that run a command or start a connection,
             upload, service or background work (they would run outside what the reserves cover or
             fail inside the boundary in ways nobody reviewed);
    bearer   what the gate's own credential, usage and profile reads depend on: credential helpers,
             auth, transport and login pins, policy helpers, refused environment names and every
             unreviewed policy-limits key.

policy-limits.json keys are those of the Claude Code 2.1.322 schema (S): restrictions (feature
limits, each an object or boolean), compliance_taints (regime names), monitoring_notice and
artifact_create_notice (notices native shows), core_skills (skill names), upcoming_minimum_version
(a notice) and defaults. They only narrow what native does or tell the user something, so both
scopes accept them in their reviewed shapes. Of defaults only remote_control_at_startup is reviewed:
protected mode passes remoteControlAtStartup=false in its own settings, which native ranks above an
organization default, and the gate routes no remote-control traffic. Any other key, or another
shape, refuses both scopes until a kit release reviews it.

Native's in-run policy refresh (GET https://api.anthropic.com/api/claude_code/settings and
/policy_limits) goes through its HTTPS proxy, the gate, which refuses every CONNECT while model work
runs; native then keeps these cached copies, the ones judged at launch. forceRemoteSettingsRefresh
would make native exit at startup, so it refuses (it is one of the active keys).

Local settings sources (user, project and managed files) are judged by the bearer rules plus
forceRemoteSettingsRefresh: the user's own hooks, MCP servers and plugins stay enabled and run inside
the boundary. macOS managed preferences cannot be read safely here, so their presence refuses.

Files: lstat first; a symlink, a non-regular file, another owner, a second hard link, permissions
other than 0600 (server-managed caches) or group/other write (local sources), or more than 1 MiB
refuses; one bounded read of the opened inode, which must be the one checked and unchanged afterwards.

Public API:
    evaluate(config_dir, organization, cwd, platform=sys.platform, managed=None) -> Verdict
    judge_remote_settings(document, organization) / judge_policy_limits(document)
        / judge_local(document, organization) -> list[Finding]   (pure)
    Verdict(native, bearer, findings): native and bearer are tuples of reason words, empty when
        accepted; refused() is True when either scope refuses; describe() names reasons and keys.
"""

from dataclasses import dataclass
import json
import os
import pwd
import re
import stat
import sys


FILE_LIMIT = 1 << 20
SERVER_MANAGED = ("remote-settings.json", "policy-limits.json")
HELPER_CONSENT = "remote-settings-helper-consent"
NOATIME = getattr(os, "O_NOATIME", 0)
# Settings keys (Claude Code 2.1.322, S) that name a credential helper, an auth or transport pin.
SETTINGS_REFUSED = ("apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh", "otelHeadersHelper",
                    "proxyAuthHelper", "processWrapper", "forceLoginGatewayUrl", "gatewayInternalNetworks", "xaaIdp",
                    "allowedProviders")
POLICY_HELPERS = ("policyHelper", "policyHelpers")
# Keys that run a command or start a connection, upload, service or background work.
SETTINGS_ACTIVE = ("hooks", "statusLine", "subagentStatusLine", "fileSuggestion", "sshConfigs", "sshHostCommands",
                   "managedMcpServers", "extraKnownMarketplaces", "additionalMarketplaces", "remoteControlAtStartup",
                   "autoUploadSessions", "daemonColdStart", "autoDreamEnabled", "forceRemoteSettingsRefresh")
REVIEWED_SETTINGS = frozenset(SETTINGS_REFUSED + POLICY_HELPERS + SETTINGS_ACTIVE
                              + ("env", "forceLoginMethod", "forceLoginOrgUUID"))
# Environment names a settings env may not set: auth, provider, endpoint, transport and runtime families.
ENV_REFUSED = re.compile(r"(?:ANTHROPIC_|CLAUDE_|AWS_|GOOGLE_|VERTEX_|CLOUD_ML_|NODE_|SSL_|BUN_|NPM_CONFIG_).*"
                         r"|.*(?:PROXY|CERT|_CA_).*", re.IGNORECASE)
LIMITS_KEYS = frozenset(("restrictions", "compliance_taints", "monitoring_notice", "defaults", "core_skills",
                         "artifact_create_notice", "upcoming_minimum_version"))
REVIEWED_DEFAULTS = frozenset(("remote_control_at_startup",))
WORD = re.compile(r"[A-Za-z0-9_]{1,64}")
MANAGED_DIRS = {"linux": ("/etc/claude-code",), "darwin": ("/Library/Application Support/ClaudeCode",)}
MANAGED_PREFERENCES = "/Library/Managed Preferences"  # MDM profiles; the Claude Code domain name is U
REASON_TEXT = {
    "policy_conflicting_auth": "a credential helper or an auth, provider or transport pin",
    "policy_helper_unevaluable": "a policy helper that computes settings",
    "policy_conflicting_env": "an environment setting that changes auth, provider, endpoint or transport",
    "policy_command_unevaluable": "a setting that runs a command or starts a connection or background work",
    "policy_login_method_incompatible": "a login method other than the Claude account login",
    "policy_organization_mismatch": "a login organization other than the enrolled one",
    "policy_shape_unrecognized": "a document or value of an unreviewed shape",
    "policy_limits_unreviewed_keys": "an organization limit this kit release has not reviewed",
    "policy_refresh_forced": "a forced policy refresh, which native cannot make in protected mode",
    "policy_file_unsafe": "a policy file that is not a private regular file of yours",
    "policy_file_unreadable": "a policy file that cannot be read once and unchanged",
    "policy_unparsed": "a policy file that is not JSON",
    "policy_os_managed": "macOS managed preferences, which protected mode cannot inspect",
}


@dataclass(frozen=True)
class Finding:
    """One refusing key: the file it is in, its name (or a fixed label), the scopes and the reason."""
    source: str
    name: str
    native: bool
    bearer: bool
    reason: str


@dataclass(frozen=True)
class Verdict:
    native: tuple
    bearer: tuple
    findings: tuple

    def refused(self):
        return bool(self.native or self.bearer)

    def describe(self):
        reasons = sorted(set(self.native) | set(self.bearer))
        parts = []
        for reason in reasons:
            keys = sorted({f"{finding.source}: {finding.name}" for finding in self.findings if finding.reason == reason})
            parts.append(REASON_TEXT.get(reason, reason) + (" (" + ", ".join(keys) + ")" if keys else ""))
        return "; ".join(parts)


def _name(name):
    """A key as shown: printable ASCII up to 64 characters, else a fixed label (never a value)."""
    return name if isinstance(name, str) and re.fullmatch(r"[\x21-\x7e]{1,64}", name) else "<unprintable key>"


def _active(value):
    """False only for an absent, null, false or empty setting."""
    return not (value is None or value is False or (isinstance(value, (dict, list, str)) and len(value) == 0))


def _organization_matches(value, organization):
    if value is None:
        return True
    if isinstance(value, str):
        return value == organization
    return isinstance(value, list) and bool(value) and all(isinstance(item, str) for item in value) \
        and organization in value


def _settings_key(source, name, value, organization, active_scope):
    """The Finding for one settings key, or None when it is accepted."""
    shown = _name(name)
    if name == "env":
        env = value or {}
        if not isinstance(env, dict):
            return Finding(source, shown, True, True, "policy_conflicting_env")
        if any(not isinstance(key, str) or ENV_REFUSED.fullmatch(key) for key in env):
            return Finding(source, shown, True, True, "policy_conflicting_env")
        return None
    if name in POLICY_HELPERS:
        return Finding(source, shown, True, True, "policy_helper_unevaluable")
    if name in SETTINGS_REFUSED:
        return Finding(source, shown, True, True, "policy_conflicting_auth")
    if name == "forceRemoteSettingsRefresh" and _active(value):
        return Finding(source, shown, True, False, "policy_refresh_forced")
    if name in SETTINGS_ACTIVE:
        if active_scope and _active(value):
            return Finding(source, shown, True, False, "policy_command_unevaluable")
        return None
    if name == "forceLoginMethod":
        return None if value in (None, "claudeai") else Finding(source, shown, True, True,
                                                                "policy_login_method_incompatible")
    if name == "forceLoginOrgUUID":
        return None if _organization_matches(value, organization) else Finding(source, shown, True, True,
                                                                               "policy_organization_mismatch")
    if not isinstance(name, str):
        return Finding(source, "<unprintable key>", True, True, "policy_shape_unrecognized")
    if not name.startswith("$") and isinstance(value, dict) and any(inner in REVIEWED_SETTINGS for inner in value):
        # An envelope around reviewed keys would mean an on-disk shape nobody traced.
        return Finding(source, shown, True, True, "policy_shape_unrecognized")
    return None


def judge_settings(source, document, organization, active_scope):
    if not isinstance(document, dict):
        return [Finding(source, "<document>", True, True, "policy_shape_unrecognized")]
    findings = []
    for name, value in document.items():
        finding = _settings_key(source, name, value, organization, active_scope)
        if finding is not None:
            findings.append(finding)
    return findings


def judge_remote_settings(document, organization):
    return judge_settings("remote-settings.json", document, organization, active_scope=True)


def judge_local(source, document, organization):
    return judge_settings(source, document, organization, active_scope=False)


def _strings(value, limit=None):
    return isinstance(value, list) and all(isinstance(item, str) and (limit is None or len(item) <= limit)
                                           for item in value)


def _notice(value):
    return value is None or isinstance(value, dict)


def judge_policy_limits(document):
    source = "policy-limits.json"
    if not isinstance(document, dict):
        return [Finding(source, "<document>", True, True, "policy_shape_unrecognized")]
    findings = []
    for name, value in document.items():
        shown = _name(name)
        if name not in LIMITS_KEYS:
            findings.append(Finding(source, shown, True, True, "policy_limits_unreviewed_keys"))
        elif name == "restrictions":
            if not isinstance(value, dict) or not all(isinstance(key, str) and WORD.fullmatch(key)
                                                      and isinstance(entry, (dict, bool)) for key, entry in value.items()):
                findings.append(Finding(source, shown, True, True, "policy_shape_unrecognized"))
        elif name == "compliance_taints":
            if not _strings(value, 128):
                findings.append(Finding(source, shown, True, True, "policy_shape_unrecognized"))
        elif name == "core_skills":
            if value is not None and not _strings(value, 128):
                findings.append(Finding(source, shown, True, True, "policy_shape_unrecognized"))
        elif name in ("monitoring_notice", "artifact_create_notice", "upcoming_minimum_version"):
            if not _notice(value):
                findings.append(Finding(source, shown, True, True, "policy_shape_unrecognized"))
        elif name == "defaults":
            if not isinstance(value, dict):
                findings.append(Finding(source, shown, True, True, "policy_shape_unrecognized"))
                continue
            for key, entry in value.items():
                if key not in REVIEWED_DEFAULTS:
                    findings.append(Finding(source, "defaults." + _name(key), True, True,
                                            "policy_limits_unreviewed_keys"))
                elif not isinstance(entry, bool):
                    findings.append(Finding(source, "defaults." + key, True, True, "policy_shape_unrecognized"))
    return findings


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(name):
    raise ValueError("non-finite number")


def read_checked(directory, name, exact_mode):
    """("absent" | "unsafe" | "unreadable" | "read", bytes or None) for NAME in the directory fd."""
    try:
        before = os.lstat(name, dir_fd=directory)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "unreadable", None
    mode = stat.S_IMODE(before.st_mode)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
            or (mode != exact_mode if exact_mode is not None else mode & 0o022)
            or before.st_size > FILE_LIMIT):
        return "unsafe", None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC | NOATIME, dir_fd=directory)
    except OSError:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        except OSError:
            return "unreadable", None
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return "unreadable", None
        raw = os.read(fd, FILE_LIMIT + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    try:
        again = os.lstat(name, dir_fd=directory)
    except OSError:
        return "unreadable", None
    stamp = (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns, opened.st_nlink)
    if (len(raw) != opened.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink) != stamp
            or (again.st_dev, again.st_ino) != (before.st_dev, before.st_ino)):
        return "unreadable", None
    return "read", raw


def _document(source, directory, name, exact_mode):
    """(document or None, findings) of one file; an absent file is (None, [])."""
    status, raw = read_checked(directory, name, exact_mode)
    if status == "absent":
        return None, []
    if status != "read":
        return None, [Finding(source, "<file>", True, True, "policy_file_" + status)]
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant), []
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, [Finding(source, "<file>", True, True, "policy_unparsed")]
    finally:
        raw = None


def _open_dir(path):
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None


def _local(path, organization):
    directory = _open_dir(os.path.dirname(path))
    if directory is None:
        return []
    try:
        document, findings = _document(path, directory, os.path.basename(path), None)
    finally:
        os.close(directory)
    return findings if document is None else judge_local(path, document, organization)


def local_sources(config_dir, cwd, platform):
    """The settings files native 2.1.322 reads for this account and working directory (S)."""
    sources = [os.path.join(config_dir, "settings.json")]
    place = cwd
    while True:
        sources += [os.path.join(place, ".claude/settings.json"), os.path.join(place, ".claude/settings.local.json")]
        parent = os.path.dirname(place)
        if parent == place:
            break
        place = parent
    for managed in MANAGED_DIRS.get(platform, ()):
        sources.append(os.path.join(managed, "managed-settings.json"))
        try:
            names = sorted(name for name in os.listdir(os.path.join(managed, "managed-settings.d"))
                           if name.endswith(".json"))
        except OSError:
            names = []
        sources += [os.path.join(managed, "managed-settings.d", name) for name in names]
    return sources


def os_managed(root):
    """Managed-preference files that may configure Claude Code (any name with anthropic or claude),
    machine-wide or for this user; they cannot be inspected safely, so their presence refuses."""
    found = []
    places = [root]
    try:
        places.append(os.path.join(root, pwd.getpwuid(os.getuid()).pw_name))
    except KeyError:
        pass
    for place in places:
        try:
            names = os.listdir(place)
        except OSError:
            continue
        found += [os.path.join(place, name) for name in sorted(names)
                  if "anthropic" in name.lower() or "claude" in name.lower()]
    return found


def evaluate(config_dir, organization, cwd, platform=None, managed_preferences=MANAGED_PREFERENCES):
    """The Verdict for this account's cached server-managed policy and the local settings sources."""
    platform = platform or ("darwin" if sys.platform == "darwin" else "linux")
    findings = []
    directory = _open_dir(config_dir)
    if directory is None:
        findings.append(Finding(config_dir, "<directory>", True, True, "policy_file_unreadable"))
    else:
        try:
            for name in SERVER_MANAGED:
                document, problems = _document(name, directory, name, 0o600)
                findings += problems
                if document is not None:
                    findings += (judge_remote_settings(document, organization) if name == SERVER_MANAGED[0]
                                 else judge_policy_limits(document))
                document = None
            if read_checked(directory, HELPER_CONSENT, None)[0] != "absent":
                findings.append(Finding(HELPER_CONSENT, "<file>", True, True, "policy_helper_unevaluable"))
        finally:
            os.close(directory)
    for path in local_sources(config_dir, cwd, platform):
        findings += _local(path, organization)
    if platform == "darwin":
        findings += [Finding(path, "<file>", True, True, "policy_os_managed") for path in os_managed(managed_preferences)]
    native = tuple(sorted({finding.reason for finding in findings if finding.native}))
    bearer = tuple(sorted({finding.reason for finding in findings if finding.bearer}))
    return Verdict(native, bearer, tuple(findings))
