"""E0: reviewed local key-name readers for the stanford policy caches (research only).

Run as `python3 -I -B read_keys.py {codex|claude} --alias stanford --out DIR` (standard
library, Python 3.11+). Only the coordinator runs it against the real files, once per
subcommand, under the ledger's g0-approval.json "e0" permission. The offline tests drive it
with a temporary HOME and fabricated caches. Nothing here starts a native, reads a
credential, opens a socket or writes outside DIR.

codex   Resolves the codex-as account home as codex-as does (the kit's configuration
        loader, then lib/codex-as/layout.py: codex_home/accounts/NAME; root_check and owned,
        private directories, validated read-only and never repaired; no preflight, lease or
        credential) and reads one file there: cloud-config-bundle-cache.json, the signed
        bundle cache native codex 0.167.2 writes after a successful
        GET /backend-api/wham/config/bundle (codex-rs ce21072c cloud-config/src/cache.rs).
        Retained: the JSON key paths (array items as [], id-like segments as {id}) with value
        types and counts; the cache shape, identity-field presence and age; per fragment,
        whether its TOML parses, the ConfigRequirementsToml field names a requirements
        fragment sets (other names counted), its [application.network] state and the forced
        keys it pins; the D14 summary and verdict. Never a fragment id, name or contents,
        a user or account id, the signature, a timestamp or any other value.
claude  Resolves <secrets_dir>/native-claude-accounts/NAME/config through the kit loader and
        the QC harness's fixed alias paths (qc/n1_harness.py AccountPaths; walked from / with
        O_NOFOLLOW, the last three components private) and reads remote-settings.json and
        policy-limits.json there (n1_harness.SERVER_MANAGED). Every top-level key is
        classified with the reviewed QC key review (SETTINGS_REFUSED, POLICY_HELPERS,
        SETTINGS_ACTIVE, REFUSED_ENV and SETTINGS_ENV_REFUSED for env names,
        forceLoginMethod, forceLoginOrgUUID) as refused, active, accepted or unknown, for
        the native and the bearer scope (G0 D15). QC reviewed no policy-limits key, so each
        one is unknown and refuses until reviewed. Retained: key names, value types,
        classifications, verdicts, env names as two counts, restriction and defaults names
        of policy-limits.json. Never a value; never .credentials.json, account.json,
        .claude.json, the policy stamp or helper-consent sidecars or any other file. The
        organization pin is reported, not compared: that needs the registration file.

File checks (both): lstat first. A symlink, non-regular file, file of another owner, file
with a second hard link, file with group or other permission bits (claude: anything but
0600, as D15 says) or a file over 1 MiB is never opened. Open with O_NOFOLLOW and O_NONBLOCK
(plus O_NOATIME where the platform has it, so the access time stays), require the opened
inode to be the checked one, read once with a bound, and require the file unchanged
afterwards (fstat and a second lstat).

One-shot: DIR must be an existing directory owned by this user with no group or other bits,
not a symlink, outside every Git work tree, the repository, codex_home and secrets_dir.
e0-STEP.attempt (0600, O_EXCL) is created before the first target file is looked at, then
e0-STEP.result.json (0600, O_EXCL). Either present refuses the step (exit 4). A refusal
before the marker writes nothing (exit 2). Exit 0 when a clean result was written (its
outcome says what was read), 3 when the marker exists but no clean result could be written.
"""
import sys

if sys.version_info < (3, 11):
    sys.exit('e0: Python 3.11 or newer is required (tomllib)')

import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import stat
import time
import tomllib

HERE = Path(os.path.realpath(__file__)).parent
REPO = HERE.parents[4]

APPROVED_ALIAS = 'stanford'  # g0-approval.json "e0"
SCHEMA = 1
EXIT_OK, EXIT_REFUSED, EXIT_INCOMPLETE, EXIT_ATTEMPTED = 0, 2, 3, 4
FILE_LIMIT = 1 << 20
PATH_LIMIT = 256
DEPTH_LIMIT = 16
NAME_LIMIT = 128
SECRET_MIN = 12  # values at least this long must never appear in the output
NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,63}')
NOATIME = getattr(os, 'O_NOATIME', 0)

CODEX_CACHE = 'cloud-config-bundle-cache.json'
CODEX_SOURCE_CACHES = re.compile(r'managed-config-.*\.json')  # cache.rs for_source (config_server)
# ConfigRequirementsToml at codex-rs ce21072c (config/src/config_requirements.rs), with the
# serde aliases feature_requirements and experimental_network. Native ignores other names.
REQUIREMENT_FIELDS = frozenset((
    'config_server', 'allowed_login_methods', 'allowed_chatgpt_workspaces', 'cli_auth_credentials_store',
    'chatgpt_base_url', 'sqlite_home', 'unsafe_enforce_sqlite_journal_mode', 'log_dir', 'model_catalog_json',
    'model_provider', 'model_providers', 'check_for_update_on_startup', 'allow_login_shell', 'feedback',
    'allowed_approval_policies', 'allowed_approvals_reviewers', 'allowed_sandbox_modes',
    'allowed_permission_profiles', 'default_permissions', 'remote_sandbox_config', 'allowed_web_search_modes',
    'allow_managed_hooks_only', 'allow_browser_and_computer_use', 'allow_appshots', 'allow_remote_control',
    'computer_use', 'browser_use', 'in_app_browser', 'windows', 'features', 'feature_requirements', 'hooks',
    'mcp_servers', 'plugins', 'marketplaces', 'otel', 'apps', 'rules', 'enforce_residency', 'network_proxy',
    'experimental_network', 'application', 'permissions', 'auto_review', 'models',
    'additional_developer_instructions', 'guardian_policy_config', 'guardian_extra_policy'))
# Keys codex-as (lib/codex-as/launch.py) or the gate (G0 2.2, D14) force, plus config_server,
# which moves the bundle to another host.
FORCED_KEYS = ('chatgpt_base_url', 'model_provider', 'model_providers', 'cli_auth_credentials_store', 'sqlite_home',
               'enforce_residency', 'check_for_update_on_startup', 'config_server')
# Config-layer names worth naming: the forced keys and the roots the LX probe overrides with -c.
CONFIG_NAMES = frozenset(FORCED_KEYS + ('features', 'otel', 'analytics', 'web_search', 'memories'))
D14_REASONS = {'chatgpt_base_url': 'pins-chatgpt-base-url', 'model_provider': 'pins-model-provider',
               'model_providers': 'pins-model-provider', 'enforce_residency': 'pins-enforce-residency',
               'config_server': 'requires-config-server'}
# Pins whose codex-as value is known: a pin to that value does not conflict.
D14_COMPATIBLE = {'cli_auth_credentials_store': 'pins-credential-store',
                  'check_for_update_on_startup': 'pins-update-check', 'sqlite_home': 'pins-sqlite-home'}

# policy-limits.json top-level names in the 2.1.322 zod schema (S, binary offset near 287881255).
LIMITS_SCHEMA_2_1_322 = frozenset(('restrictions', 'compliance_taints', 'monitoring_notice', 'defaults', 'core_skills',
                                   'artifact_create_notice', 'upcoming_minimum_version'))

JWT = re.compile(r'[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}')
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
RUN = re.compile(r'[A-Za-z0-9_+=-]{32,}')
SEGMENT = re.compile(r'[A-Za-z_$][A-Za-z0-9_$-]{0,63}')
STAMP = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})')


class Refused(Exception):
    """A fixed reason code; never a path or a value."""

    def __init__(self, reason, code=EXIT_REFUSED):
        super().__init__(reason)
        self.reason, self.code = reason, code


class ParseError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _load(name, path):
    spec = importlib.util.spec_from_file_location('e0_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Modules:
    """The kit's loader, codex-as's layout and check modules and the QC harness, loaded by path."""

    def __init__(self):
        try:
            self.config = _load('config', REPO / 'lib/coding-agent-kit/config.py')
            self.guard = _load('check', REPO / 'lib/codex-as/check.py')
            self.layout = _load('layout', REPO / 'lib/codex-as/layout.py')
            self.n1 = _load('n1_harness', HERE.parent / 'qc/n1_harness.py')
        except (OSError, ImportError, SyntaxError):
            raise Refused('incomplete-checkout') from None


def utc(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# --- names, types and the output scan ---------------------------------------

def id_like(name):
    lowered = name.lower()
    if UUID.search(name) or re.search(r'[0-9]{6,}', name):
        return True
    if any(re.search(r'[0-9]', run) for run in re.findall(r'[0-9a-f]{12,}', lowered)):
        return True
    return any(re.search(r'[0-9]', run) and re.search(r'[A-Z]', run) for run in re.findall(r'[A-Za-z0-9]{20,}', name))


def safe_name(name):
    """A key name as retained: schema-like names as they are, anything else as {id}."""
    return name if isinstance(name, str) and SEGMENT.fullmatch(name) and not id_like(name) else '{id}'


def json_type(value):
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return 'null' if value is None else 'other'


def key_paths(document):
    """Every node's path and type, counted; array items as [], unsafe names as {id}."""
    counts, truncated = Counter(), 0

    def walk(value, path, level):
        nonlocal truncated
        key = (path or '<root>', json_type(value))
        if key not in counts and len(counts) >= PATH_LIMIT:
            truncated += 1
            return
        counts[key] += 1
        if level >= DEPTH_LIMIT:
            return
        if isinstance(value, dict):
            for name, item in value.items():
                walk(item, (path + '.' if path else '') + safe_name(name), level + 1)
        elif isinstance(value, list):
            for item in value:
                walk(item, path + '[]', level + 1)

    walk(document, '', 0)
    return [{'path': path, 'type': kind, 'count': count} for (path, kind), count in sorted(counts.items())], truncated


def names_view(mapping):
    if not isinstance(mapping, dict):
        return {'type': json_type(mapping)}
    names = sorted({safe_name(name) for name in mapping})
    return {'type': 'object', 'count': len(mapping), 'names': names[:NAME_LIMIT],
            'names_truncated': max(0, len(names) - NAME_LIMIT)}


def strings(value, found, keys_too=False, depth=0):
    """String values (and, with keys_too, key names) at least SECRET_MIN long, for the scan."""
    if depth > 64:
        return found
    if isinstance(value, str):
        if len(value) >= SECRET_MIN:
            found.add(value)
    elif isinstance(value, dict):
        for name, item in value.items():
            if keys_too and isinstance(name, str) and len(name) >= SECRET_MIN:
                found.add(name)
            strings(item, found, keys_too, depth + 1)
    elif isinstance(value, list):
        for item in value:
            strings(item, found, keys_too, depth + 1)
    return found


def leaks(text, secrets):
    found = set()
    if JWT.search(text):
        found.add('jwt-shaped')
    if UUID.search(text):
        found.add('uuid-shaped')
    for run in RUN.findall(text):
        if (re.search(r'[0-9]', run) and re.search(r'[A-Z]', run)) or re.fullmatch(r'[0-9a-fA-F]{32,}', run):
            found.add('token-shaped')
    if any(secret in text or json.dumps(secret, ensure_ascii=True)[1:-1] in text for secret in secrets):
        found.add('known-value')
    return sorted(found)


def dump(document):
    return json.dumps(document, indent=1, sort_keys=True, ensure_ascii=True) + '\n'


def sanitize(result, secrets):
    """Serialize and scan. On a finding, redact and rescan; if anything remains, keep only the findings."""
    text = dump(result)
    findings = leaks(text, secrets)
    if not findings:
        return text, []
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(json.dumps(secret, ensure_ascii=True)[1:-1], '<redacted>').replace(secret, '<redacted>')
    for pattern in (JWT, UUID):
        text = pattern.sub('<redacted>', text)
    text = RUN.sub(lambda match: '<redacted>' if leaks(match[0], ()) else match[0], text)
    try:
        data = json.loads(text)
        data['sanitizer_findings'] = findings
        text = dump(data)
    except ValueError:
        text = None
    if text is None or leaks(text, secrets):
        text = dump({'schema': SCHEMA, 'probe': result.get('probe'), 'outcome': 'sanitizer-blocked',
                     'sanitizer_findings': findings})
    return text, findings


# --- the out directory and the one-shot marker ------------------------------

def _git_work_tree(real):
    current = real
    while True:
        try:
            os.lstat(os.path.join(current, '.git'))
            return True
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError:
            raise Refused('out-unusable') from None
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _overlaps(path, root):
    try:
        common = os.path.commonpath([path, root])
    except ValueError:
        return False
    return common in (path, root)


def open_out(path, roots, uid):
    """A directory fd for DIR: existing, owned, private, no symlink, outside work trees and state."""
    if not isinstance(path, str) or not path.startswith('/'):
        raise Refused('out-not-absolute')
    try:
        info = os.lstat(path)
    except OSError:
        raise Refused('out-missing') from None
    if not stat.S_ISDIR(info.st_mode):
        raise Refused('out-not-directory')
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o077:
        raise Refused('out-not-private')
    real = os.path.realpath(path)
    if _git_work_tree(real):
        raise Refused('out-in-git-work-tree')
    if any(_overlaps(real, os.path.realpath(root)) for root in roots):
        raise Refused('out-overlaps-account-state-or-repository')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise Refused('out-unusable') from None
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        raise Refused('out-changed')
    return fd


def _exists(directory, name):
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise Refused('out-unusable') from None
    return True


def write_exclusive(directory, name, text):
    """0600 by descriptor, created exclusively (FileExistsError if present)."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    with os.fdopen(fd, 'w', encoding='ascii') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.fsync(directory)


def marker_names(step):
    return 'e0-%s.attempt' % step, 'e0-%s.result.json' % step


def check_unused(out, step):
    attempt, result = marker_names(step)
    if _exists(out, result):
        raise Refused('result-exists', EXIT_ATTEMPTED)
    if _exists(out, attempt):
        raise Refused('attempt-exists', EXIT_ATTEMPTED)


# --- one target file ---------------------------------------------------------

def _kind(mode):
    if stat.S_ISREG(mode):
        return 'regular'
    if stat.S_ISLNK(mode):
        return 'symlink'
    if stat.S_ISDIR(mode):
        return 'directory'
    if stat.S_ISFIFO(mode):
        return 'fifo'
    return 'other'


def size_bucket(size):
    if size == 0:
        return '0'
    for limit, label in ((1 << 10, '<=1KiB'), (4 << 10, '<=4KiB'), (16 << 10, '<=16KiB'), (64 << 10, '<=64KiB'),
                         (256 << 10, '<=256KiB'), (1 << 20, '<=1MiB')):
        if size <= limit:
            return label
    return '>1MiB'


def _problem(info, uid, exact_mode):
    if stat.S_ISLNK(info.st_mode):
        return 'symlink'
    if not stat.S_ISREG(info.st_mode):
        return 'not-regular'
    if info.st_uid != uid:
        return 'not-owned'
    if exact_mode is not None and stat.S_IMODE(info.st_mode) != exact_mode:
        return 'mode-not-0600'
    if stat.S_IMODE(info.st_mode) & 0o077:
        return 'mode-not-private'
    if info.st_nlink != 1:
        return 'hard-linked'
    if info.st_size > FILE_LIMIT:
        return 'oversize'
    return None


def _snapshot(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns)


def read_target(directory, name, uid, now, exact_mode=None):
    """(status, raw bytes or None, metadata). status is 'read', 'absent' or a refusal code."""
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return 'absent', None, {'present': False}
    except OSError:
        return 'stat-failed', None, {'present': None}
    meta = {'present': True, 'kind': _kind(info.st_mode), 'mode': '%04o' % stat.S_IMODE(info.st_mode),
            'owned_by_caller': info.st_uid == uid, 'links': info.st_nlink, 'size_bucket': size_bucket(info.st_size),
            'mtime_age_minutes': int((now - info.st_mtime) // 60)}
    problem = _problem(info, uid, exact_mode)
    if problem:
        return problem, None, meta
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        try:
            fd = os.open(name, flags | NOATIME, dir_fd=directory)
            meta['access_time_kept'] = bool(NOATIME)
        except PermissionError:
            if not NOATIME:
                raise
            fd = os.open(name, flags, dir_fd=directory)
            meta['access_time_kept'] = False
    except OSError:
        return 'open-failed', None, meta
    chunks, total = [], 0
    try:
        opened = os.fstat(fd)
        if _snapshot(opened) != _snapshot(info):
            return 'changed-before-open', None, meta
        while total <= FILE_LIMIT:
            chunk = os.read(fd, FILE_LIMIT + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    raw = b''.join(chunks)
    chunks.clear()
    if total > FILE_LIMIT:
        return 'oversize', None, meta
    if _snapshot(after) != _snapshot(opened) or len(raw) != opened.st_size:
        return 'changed-during-read', None, meta
    try:
        again = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except OSError:
        return 'replaced-during-read', None, meta
    if _snapshot(again) != _snapshot(opened):
        return 'replaced-during-read', None, meta
    return 'read', raw, meta


def parse_json(raw):
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise ParseError('not-utf8') from None

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ParseError('duplicate-key')
            result[key] = value
        return result

    def constant(_name):
        raise ParseError('non-finite-number')

    try:
        return json.loads(text, object_pairs_hook=unique, parse_constant=constant)
    except ParseError:
        raise
    except RecursionError:
        raise ParseError('too-deep') from None
    except ValueError:
        raise ParseError('malformed') from None


# --- codex -------------------------------------------------------------------

def parse_stamp(text):
    match = STAMP.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    try:
        base = datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    offset = 0
    if match[3] != 'Z':
        sign = 1 if match[3][0] == '+' else -1
        offset = sign * (int(match[3][1:3]) * 3600 + int(match[3][4:6]) * 60)
    return base + (int(match[2].ljust(9, '0')) / 1e9 if match[2] else 0.0) - offset


def _fragments(bundle, bucket):
    holder = bundle.get(bucket) if isinstance(bundle, dict) else None
    items = holder.get('enterprise_managed') if isinstance(holder, dict) else None
    return items if isinstance(items, list) else None


def cache_shape(document):
    """cache-v1 (exactly cache.rs's fields and types), cache-v1-extra-keys (more fields, which serde
    ignores) or unrecognized."""
    extra = []

    def holds(value, required):
        if not isinstance(value, dict) or not required <= set(value):
            return False
        extra.append(set(value) != required)
        return True

    if not holds(document, {'signed_payload', 'signature'}) or not isinstance(document['signature'], str):
        return 'unrecognized'
    payload = document['signed_payload']
    if not holds(payload, {'version', 'cached_at', 'expires_at', 'chatgpt_user_id', 'account_id', 'bundle'}):
        return 'unrecognized'
    if (type(payload['version']) is not int or not all(isinstance(payload[key], str) for key in ('cached_at', 'expires_at'))
            or not all(payload[key] is None or isinstance(payload[key], str) for key in ('chatgpt_user_id', 'account_id'))):
        return 'unrecognized'
    bundle = payload['bundle']
    if not holds(bundle, {'config_toml', 'requirements_toml'}):
        return 'unrecognized'
    for bucket in ('config_toml', 'requirements_toml'):
        if not holds(bundle[bucket], {'enterprise_managed'}) or not isinstance(bundle[bucket]['enterprise_managed'], list):
            return 'unrecognized'
        for fragment in bundle[bucket]['enterprise_managed']:
            if not holds(fragment, {'id', 'name', 'contents'}) or not all(
                    isinstance(fragment[key], str) for key in ('id', 'name', 'contents')):
                return 'unrecognized'
    return 'cache-v1-extra-keys' if any(extra) else 'cache-v1'


def network_state(table):
    """The fragment's [application.network] as native 0.167.2 reads it: enabled defaults to true."""
    if 'application' not in table:
        return 'absent'
    application = table['application']
    if not isinstance(application, dict):
        return 'invalid'
    if 'network' not in application:
        return 'application-without-network'
    network = application['network']
    if not isinstance(network, dict):
        return 'invalid'
    enabled = network.get('enabled', True)
    if not isinstance(enabled, bool):
        return 'invalid'
    return 'enabled' if enabled else 'disabled'


def _compatible(key, value, codex_home):
    if key == 'cli_auth_credentials_store':
        return value == 'file'
    if key == 'check_for_update_on_startup':
        return value is False
    if key == 'sqlite_home':
        return isinstance(value, str) and value.startswith('/') and os.path.normpath(value) == os.path.normpath(
            str(codex_home))
    return None


def _toml(fragment, secrets):
    contents = fragment.get('contents') if isinstance(fragment, dict) else None
    if not isinstance(contents, str):
        return None, 'not-a-string'
    try:
        table = tomllib.loads(contents)
    except (tomllib.TOMLDecodeError, RecursionError, ValueError):
        return None, 'malformed'
    for value in table.values():  # top-level names may be retained; values and nested names never
        strings(value, secrets, keys_too=True)
    return table, 'ok'


def requirements_view(fragment, codex_home, secrets):
    table, state = _toml(fragment, secrets)
    if table is None:
        return {'toml': state}, None
    known = sorted(name for name in table if name in REQUIREMENT_FIELDS)
    pins = [key for key in FORCED_KEYS if key in table]
    view = {'toml': 'ok', 'requirement_names': known, 'other_names': len(table) - len(known),
            'application_network': network_state(table), 'pins': pins}
    compatible = {key: _compatible(key, table[key], codex_home) for key in pins if key in D14_COMPATIBLE}
    if compatible:
        view['pin_matches_codex_as'] = compatible
    return view, table


def config_view(fragment, secrets):
    table, state = _toml(fragment, secrets)
    if table is None:
        return {'toml': state}
    named = sorted(name for name in table if name in CONFIG_NAMES)
    return {'toml': 'ok', 'names': named, 'other_names': len(table) - len(named)}


def d14_summary(shape, requirements, configs):
    """The D14 answers for this cached bundle. Bundle order is highest priority first and
    [application] merges as a whole table (S); local requirements are not read here."""
    summary = {'application_network_any_enabled': any(view.get('application_network') == 'enabled'
                                                      for view in requirements),
               'application_network_any_disabled': any(view.get('application_network') == 'disabled'
                                                       for view in requirements)}
    effective = 'absent'
    for view in requirements:
        if view.get('application_network', 'absent') != 'absent':
            effective = view['application_network']
            break
    summary['application_network_effective'] = effective
    pinned = sorted({key for view in requirements for key in view.get('pins', ())}, key=FORCED_KEYS.index)
    summary['pinned_forced_keys'] = pinned
    matches = {}
    for view in requirements:
        for key, value in view.get('pin_matches_codex_as', {}).items():
            matches[key] = matches.get(key, True) and bool(value)
    summary['pin_matches_codex_as'] = matches
    summary['config_layer_names'] = sorted({name for view in configs for name in view.get('names', ())})
    unparsed = sum(view.get('toml') != 'ok' for view in requirements + configs)
    summary['fragments_unparsed'] = unparsed
    reasons = []
    if shape == 'unrecognized':
        reasons.append('cache-shape-unrecognized')
    if unparsed:
        reasons.append('fragment-toml-unparsed')
    if effective == 'invalid':
        reasons.append('application-network-invalid')
    if reasons:
        summary.update(verdict='unevaluable', reasons=reasons)
        return summary
    if effective == 'enabled':
        reasons.append('application-network-enabled')
    for key in pinned:
        if key in D14_REASONS:
            reasons.append(D14_REASONS[key])
        elif not matches.get(key, False):
            reasons.append(D14_COMPATIBLE[key])
    summary.update(verdict='refuse' if reasons else 'no-blocker-in-cache', reasons=sorted(set(reasons)))
    return summary


def codex_account_home(modules, config, alias, uid):
    """The account home as codex-as lays it out, validated read-only; a directory fd."""
    layout = modules.layout.Layout(modules.guard, alias, config)
    try:
        layout.root_check()
        for path, private in ((config.codex_home, False), (config.codex_home / 'accounts', True), (layout.home, True)):
            modules.guard.directory(path, private=private)
    except (FileNotFoundError, NotADirectoryError):
        raise Refused('account-home-missing') from None
    except ValueError:
        raise Refused('account-home-unsafe') from None
    except OSError:
        raise Refused('account-home-unusable') from None
    try:
        before = os.lstat(layout.home)
        fd = os.open(layout.home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise Refused('account-home-unusable') from None
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise Refused('account-home-changed')
    return fd, layout.home


def codex_report(home, codex_home, uid, now, secrets):
    report = {}
    try:
        report['other_managed_config_caches'] = sum(bool(CODEX_SOURCE_CACHES.fullmatch(name))
                                                    for name in os.listdir(home))
    except OSError:
        report['other_managed_config_caches'] = None
    status, raw, meta = read_target(home, CODEX_CACHE, uid, now)
    report['file'] = meta
    if status != 'read':
        report['outcome'] = status
        report['d14'] = {'verdict': 'unevaluable', 'reasons': ['cache-' + status]}
        return report
    try:
        document = parse_json(raw)
    except ParseError as error:
        report.update(outcome='parse-' + error.reason, d14={'verdict': 'unevaluable', 'reasons': ['cache-unparsed']})
        return report
    finally:
        raw = None
    strings(document, secrets)  # key names are retained (as {id} when id-like); values never
    paths, truncated = key_paths(document)
    shape = cache_shape(document)
    report.update(outcome='read', shape=shape, key_paths=paths, key_paths_truncated=truncated)
    payload = document.get('signed_payload') if isinstance(document, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    cached, expires = parse_stamp(payload.get('cached_at')), parse_stamp(payload.get('expires_at'))
    report['cache'] = {
        'version_is_1': payload.get('version') == 1 and type(payload.get('version')) is int,
        'chatgpt_user_id_present': isinstance(payload.get('chatgpt_user_id'), str) and bool(payload['chatgpt_user_id']),
        'account_id_present': isinstance(payload.get('account_id'), str) and bool(payload['account_id']),
        'signature_present': isinstance(document, dict) and isinstance(document.get('signature'), str)
        and bool(document['signature']),
        'timestamps_parse': cached is not None and expires is not None,
        'age_minutes': int((now - cached) // 60) if cached is not None else None,
        'ttl_minutes': round((expires - cached) / 60) if cached is not None and expires is not None else None,
        'expired': expires <= now if expires is not None else None}
    bundle = payload.get('bundle')
    requirement_items = _fragments(bundle, 'requirements_toml')
    config_items = _fragments(bundle, 'config_toml')
    requirements = [requirements_view(item, codex_home, secrets)[0] for item in requirement_items or ()]
    configs = [config_view(item, secrets) for item in config_items or ()]
    report['bundle'] = {
        'requirements_fragments': len(requirement_items) if requirement_items is not None else None,
        'config_fragments': len(config_items) if config_items is not None else None,
        'requirements': requirements, 'config': configs}
    report['d14'] = d14_summary(shape, requirements, configs)
    document = None
    return report


# --- claude ------------------------------------------------------------------

def classify_setting(n1, name, value):
    """(native, bearer, rule, QC reason or None) for one top-level key, by the QC review."""
    if name == 'env':
        env = value or {}  # check_policy: settings.get("env") or {}
        if not isinstance(env, dict):
            return 'refused', 'refused', 'env-not-object', 'policy_conflicting_env'
        if any(key in n1.REFUSED_ENV or n1.SETTINGS_ENV_REFUSED.fullmatch(key) for key in env):
            return 'refused', 'refused', 'env-refused-name', 'policy_conflicting_env'
        return 'accepted', 'accepted', 'env-names-allowed', None
    if name in n1.POLICY_HELPERS:
        return 'refused', 'refused', 'policy-helper', 'policy_helper_unevaluable'
    if name in n1.SETTINGS_REFUSED:
        return 'refused', 'refused', 'credential-helper-or-auth-pin', 'policy_conflicting_auth'
    if name in n1.SETTINGS_ACTIVE:
        if n1._active(value):
            return 'active', 'accepted', 'native-only-active', 'policy_command_unevaluable'
        return 'accepted', 'accepted', 'active-key-inactive-value', None
    if name == 'forceLoginMethod':
        if value is None or value == 'claudeai':
            return 'accepted', 'accepted', 'login-method-compatible', None
        return 'refused', 'refused', 'login-method-incompatible', 'policy_login_method_incompatible'
    if name == 'forceLoginOrgUUID':
        if value is None:
            return 'accepted', 'accepted', 'organization-pin-null', None
        return 'unknown', 'unknown', 'organization-pin-not-compared', 'policy_organization_not_compared'
    return 'unknown', 'unknown', 'not-in-qc-review', None


def verdicts(keys):
    result = {}
    for scope in ('native', 'bearer'):
        refused = sorted({key['reason'] for key in keys if key[scope] in ('refused', 'active') and key['reason']})
        if refused:
            result[scope] = {'verdict': 'refused', 'reasons': refused}
        elif any(key[scope] == 'unknown' and key['reason'] for key in keys):
            result[scope] = {'verdict': 'unevaluated',
                             'reasons': sorted({key['reason'] for key in keys if key[scope] == 'unknown' and key['reason']})}
        else:
            result[scope] = {'verdict': 'accepted', 'reasons': []}
    return result


def settings_view(n1, document, secrets):
    keys = []
    for name, value in document.items():
        native, bearer, rule, reason = classify_setting(n1, name, value)
        keys.append({'name': safe_name(name), 'type': json_type(value), 'native': native, 'bearer': bearer,
                     'rule': rule, 'reason': reason, 'ignored_by_native': name.startswith('$') and name != '$schema'})
    env = document.get('env')
    view = {'keys': sorted(keys, key=lambda key: key['name'])}
    if isinstance(env, dict):
        secrets.update(name for name in env if len(name) >= 8)
        view['env'] = {'names': len(env), 'refused_names': sum(
            bool(name in n1.REFUSED_ENV or n1.SETTINGS_ENV_REFUSED.fullmatch(name)) for name in env)}
    view['unknown_keys'] = sum(key['native'] == 'unknown' for key in keys)
    view['scopes'] = verdicts(keys)
    return view


def limits_view(document):
    keys = [{'name': safe_name(name), 'type': json_type(value), 'native': 'unknown', 'bearer': 'unknown',
             'rule': 'policy-limits-not-in-qc-review', 'reason': 'policy_limits_unreviewed_keys',
             'in_native_2_1_322_schema': name in LIMITS_SCHEMA_2_1_322}
            for name, value in document.items()]
    view = {'keys': sorted(keys, key=lambda key: key['name'])}
    for name in ('restrictions', 'defaults'):
        if name in document:
            view[name] = names_view(document[name])
    for name in ('compliance_taints', 'core_skills'):
        if isinstance(document.get(name), list):
            view[name] = {'type': 'array', 'count': len(document[name])}
    scopes = {}
    for scope in ('native', 'bearer'):
        scopes[scope] = ({'verdict': 'refused', 'reasons': ['policy_limits_unreviewed_keys']} if keys
                         else {'verdict': 'accepted', 'reasons': []})
    view['scopes'] = scopes
    return view


def claude_file(n1, directory, name, uid, now, secrets):
    status, raw, meta = read_target(directory, name, uid, now, exact_mode=0o600)
    report = {'file': meta}
    if status == 'absent':
        report.update(outcome='absent', scopes={scope: {'verdict': 'accepted', 'reasons': []}
                                                for scope in ('native', 'bearer')})
        return report
    refusal = None
    if status != 'read':
        report['outcome'] = status
        refusal = 'server_policy_' + status.replace('-', '_')
    else:
        try:
            document = parse_json(raw)
        except ParseError as error:
            report['outcome'] = 'parse-' + error.reason
            refusal = 'server_policy_unparsed'
        else:
            strings(document, secrets, keys_too=False)
            if not isinstance(document, dict):
                report.update(outcome='read', shape='not-an-object', type=json_type(document))
                refusal = 'server_policy_shape_unrecognized'
            else:
                report.update(outcome='read', shape='object')
                report.update(settings_view(n1, document, secrets) if name == n1.SERVER_MANAGED[0]
                              else limits_view(document))
            document = None
        finally:
            raw = None
    if refusal:
        report['scopes'] = {scope: {'verdict': 'refused', 'reasons': [refusal]} for scope in ('native', 'bearer')}
    return report


def combine(files):
    result = {}
    for scope in ('native', 'bearer'):
        states = [report['scopes'][scope] for report in files.values()]
        verdict = ('refused' if any(state['verdict'] == 'refused' for state in states)
                   else 'unevaluated' if any(state['verdict'] == 'unevaluated' for state in states) else 'accepted')
        result[scope] = {'verdict': verdict,
                         'reasons': sorted({reason for state in states if state['verdict'] == verdict
                                            for reason in state['reasons']})}
    return result


def claude_config_dir(modules, config, alias, uid):
    n1 = modules.n1
    secrets = str(config.secrets_dir)
    if not secrets.isascii():
        raise Refused('secrets-dir-not-ascii')
    try:
        paths = n1.AccountPaths(secrets, alias)
        return n1._open_dir(paths.config, uid, 3), paths
    except n1.HarnessRefused as error:
        raise Refused(error.reason) from None


# --- the steps ---------------------------------------------------------------

def say(stream, text):
    print('e0: ' + text, file=stream, flush=True)


def _base(step, alias, now):
    return {'schema': SCHEMA, 'probe': 'e0-' + step, 'alias': alias, 'started_utc': utc(now),
            'platform': {'system': platform.system().lower(), 'python': platform.python_version()}}


def attempt(step, alias, out, now, body, path_secrets, stdout):
    """Marker, body, sanitized result. Returns the exit code."""
    attempt_name, result_name = marker_names(step)
    started = now()
    try:
        write_exclusive(out, attempt_name, dump({'schema': SCHEMA, 'probe': 'e0-' + step, 'alias': alias,
                                                 'created_utc': utc(started)}))
    except FileExistsError:
        raise Refused('attempt-exists', EXIT_ATTEMPTED) from None
    result, secrets = _base(step, alias, started), set()
    try:
        result.update(body(secrets))
    except KeyboardInterrupt:
        result['outcome'] = 'interrupted'
    except Exception as error:  # recorded by class only: messages may hold paths or values
        result['outcome'] = 'error:' + type(error).__name__
    result['finished_utc'] = utc(now())
    secrets |= {value for value in path_secrets if len(value) >= 8}
    text, findings = sanitize(result, secrets)
    secrets.clear()
    try:
        write_exclusive(out, result_name, text)
    except OSError:
        say(stdout, json.dumps({'step': step, 'outcome': 'result-not-written'}))
        return EXIT_INCOMPLETE
    summary = json.loads(text)
    say(stdout, json.dumps({'step': step, 'outcome': summary.get('outcome'), 'result_file': result_name,
                            'sanitizer_findings': findings,
                            'verdict': (summary.get('d14') or {}).get('verdict') if step == 'codex'
                            else summary.get('scopes')}, sort_keys=True))
    return EXIT_INCOMPLETE if findings or summary.get('outcome', '').startswith(('error:', 'interrupted')) else EXIT_OK


def run_codex(alias, out_path, *, modules, config, uid=None, now=time.time, stdout=None):
    uid = os.getuid() if uid is None else uid
    stdout = stdout or sys.stdout
    if not NAME.fullmatch(alias):
        raise Refused('invalid-alias')
    out = open_out(out_path, (config.codex_home, config.secrets_dir, REPO), uid)
    try:
        check_unused(out, 'codex')
        home, home_path = codex_account_home(modules, config, alias, uid)
        try:
            def body(secrets):
                return codex_report(home, config.codex_home, uid, now(), secrets)
            return attempt('codex', alias, out, now, body,
                           (str(config.home), str(config.codex_home), str(config.secrets_dir), str(home_path),
                            out_path, os.path.realpath(out_path)), stdout)
        finally:
            os.close(home)
    finally:
        os.close(out)


def run_claude(alias, out_path, *, modules, config, uid=None, now=time.time, stdout=None):
    uid = os.getuid() if uid is None else uid
    stdout = stdout or sys.stdout
    if not NAME.fullmatch(alias):
        raise Refused('invalid-alias')
    out = open_out(out_path, (config.codex_home, config.secrets_dir, REPO), uid)
    try:
        check_unused(out, 'claude')
        directory, paths = claude_config_dir(modules, config, alias, uid)
        try:
            def body(secrets):
                clock = now()
                files = {name: claude_file(modules.n1, directory, name, uid, clock, secrets)
                         for name in modules.n1.SERVER_MANAGED}
                return {'outcome': 'completed' if all(report['outcome'] in ('read', 'absent')
                                                      for report in files.values()) else 'incomplete',
                        'files': files, 'scopes': combine(files),
                        'rules': 'qc/n1_harness.py SETTINGS_REFUSED POLICY_HELPERS SETTINGS_ACTIVE REFUSED_ENV '
                                 'SETTINGS_ENV_REFUSED forceLoginMethod forceLoginOrgUUID'}
            return attempt('claude', alias, out, now, body,
                           (str(config.home), str(config.codex_home), str(config.secrets_dir), paths.config,
                            out_path, os.path.realpath(out_path)), stdout)
        finally:
            os.close(directory)
    finally:
        os.close(out)


def main(argv=None, *, modules=None, stdout=None, stderr=None, now=time.time):
    os.umask(0o077)
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    parser = argparse.ArgumentParser(description='E0: key names and verdicts of one cached policy (research).')
    parser.add_argument('step', choices=('codex', 'claude'))
    parser.add_argument('--alias', required=True)
    parser.add_argument('--out', required=True, help='existing private directory outside any Git work tree')
    args = parser.parse_args(argv)
    try:
        if args.alias != APPROVED_ALIAS:
            raise Refused('alias-not-approved')
        modules = modules or Modules()
        try:
            config = modules.config.load()
        except modules.config.ConfigError:
            raise Refused('config-error') from None
        runner = run_codex if args.step == 'codex' else run_claude
        return runner(args.alias, args.out, modules=modules, config=config, now=now, stdout=stdout)
    except Refused as error:
        say(stderr, 'refused: ' + error.reason)
        return error.code


if __name__ == '__main__':
    sys.exit(main())
