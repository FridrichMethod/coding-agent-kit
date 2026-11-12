"""Pure helpers for the NM macOS Seatbelt feasibility probes (research only).

Nothing here touches the host. mac_probe.py renders profiles with these
functions on the Mac; test_nm.py checks them offline on Linux. The profile
language (SBPL) is an undocumented Apple interface: these helpers encode the
forms observed to parse on the probed macOS release, not a stable contract.
"""
import hashlib
import json
import re
import unicodedata

# Mach services that the probes treat as launch or delegation facilities:
# a sandboxed caller that reaches them can make work run outside the sandbox
# (LaunchServices open, Apple Events, background URL sessions) or leave the
# host through a daemon (mDNSResponder). They must never be allowlisted.
ESCAPE_SERVICES = (
    'com.apple.coreservices.launchservicesd',
    'com.apple.coreservices.appleevents',
    'com.apple.nsurlsessiond',
    'com.apple.dnssd.service',
    'com.apple.lsd.mapdb',
    'com.apple.lsd.modifydb',
)

# Daemon-checked sandbox operations that start work outside the caller.
LAUNCH_OPERATIONS = ('job-creation', 'lsopen', 'appleevent-send')

_SAFE_TEXT = re.compile(r'^[\x20-\x7e]*$')
_SERVICE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')
_TAG = re.compile(r'^[A-Za-z0-9-]{4,64}$')


class ProfileError(ValueError):
    """Refuse to render a profile from unsafe or ambiguous input."""


def quote(text):
    """Return an SBPL string literal, refusing anything that needs escaping."""
    if not isinstance(text, str) or not _SAFE_TEXT.match(text) or '"' in text or '\\' in text:
        raise ProfileError('unsafe SBPL string: %r' % (text,))
    return '"' + text + '"'


def _path(path):
    if not isinstance(path, str) or not path.startswith('/'):
        raise ProfileError('path must be absolute: %r' % (path,))
    parts = path.split('/')
    if any(part in ('.', '..') for part in parts) or '//' in path or (len(path) > 1 and path.endswith('/')):
        raise ProfileError('path must be normalized: %r' % (path,))
    if path == '/tmp' or path.startswith('/tmp/') or path == '/var' or path.startswith('/var/'):
        # Seatbelt matches the resolved vnode path; /tmp and /var are symlinks
        # to /private/... on macOS, so an unresolved rule would never match.
        raise ProfileError('path must be resolved (use /private/...): %r' % (path,))
    return quote(path)


def _port(port):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ProfileError('invalid port: %r' % (port,))
    return port


def render(policy):
    """Render one profile from a dict; unknown keys are refused.

    Keys: tag (required), gate_ports, gate_sockets, private_socket_dirs,
    deny_network, deny_launch, deny_mach, mach_allow, mach_deny,
    report_mach, deny_write, deny_signal_others. Later SBPL rules override earlier ones, so the
    explicit mach_deny list is emitted after any allow.
    """
    known = {'tag', 'gate_ports', 'gate_sockets', 'private_socket_dirs', 'deny_network',
             'deny_launch', 'deny_mach', 'mach_allow', 'mach_deny', 'report_mach', 'deny_write',
             'deny_signal_others'}
    unknown = set(policy) - known
    if unknown:
        raise ProfileError('unknown policy keys: %s' % sorted(unknown))
    tag = policy.get('tag')
    if not isinstance(tag, str) or not _TAG.match(tag):
        raise ProfileError('tag must be 4-64 alphanumeric/dash characters')
    msg = '(with message %s)' % quote(tag)
    lines = ['(version 1)', '(allow default)']
    if policy.get('deny_network', True):
        lines.append('(deny network* %s)' % msg)
        for port in sorted(set(_port(p) for p in policy.get('gate_ports', ()))):
            lines.append('(allow network-outbound (remote ip "localhost:%d"))' % port)
        for sock in sorted(set(policy.get('gate_sockets', ()))):
            lines.append('(allow network-outbound (remote unix-socket (path-literal %s)))' % _path(sock))
        for directory in sorted(set(policy.get('private_socket_dirs', ()))):
            quoted = _path(directory)
            lines.append('(allow network-bind (local unix-socket (subpath %s)))' % quoted)
            lines.append('(allow network-outbound (remote unix-socket (subpath %s)))' % quoted)
    elif policy.get('gate_ports') or policy.get('gate_sockets') or policy.get('private_socket_dirs'):
        raise ProfileError('gate exits are meaningless without deny_network')
    if policy.get('deny_launch', True):
        for operation in LAUNCH_OPERATIONS:
            lines.append('(deny %s %s)' % (operation, msg))
    allow = list(policy.get('mach_allow', ()))
    explicit = list(policy.get('mach_deny', ()))
    for name in allow + explicit:
        if not isinstance(name, str) or not _SERVICE.match(name):
            raise ProfileError('invalid mach service: %r' % (name,))
    if set(allow) & set(explicit):
        raise ProfileError('service both allowed and denied: %s' % sorted(set(allow) & set(explicit)))
    if policy.get('deny_mach', True):
        bad = sorted(set(allow) & set(ESCAPE_SERVICES))
        if bad:
            raise ProfileError('escape services cannot be allowlisted: %s' % bad)
        lines.append('(deny mach-lookup %s)' % msg)
        report = ' (with report) %s' % msg if policy.get('report_mach') else ''
        for name in sorted(set(allow)):
            lines.append('(allow mach-lookup (global-name %s)%s)' % (quote(name), report))
    else:
        if allow:
            raise ProfileError('mach_allow requires deny_mach')
        if policy.get('report_mach'):
            lines.append('(allow mach-lookup (with report) %s)' % msg)
    if explicit:
        names = ' '.join('(global-name %s)' % quote(n) for n in sorted(set(explicit)))
        lines.append('(deny mach-lookup %s %s)' % (names, msg))
    if policy.get('deny_signal_others'):
        lines.append('(deny signal %s)' % msg)
        lines.append('(allow signal (target same-sandbox))')
    denied = sorted(set(policy.get('deny_write', ())))
    if denied:
        lines.append('(deny file-write* %s %s)' % (' '.join('(subpath %s)' % _path(p) for p in denied), msg))
    return '\n'.join(lines) + '\n'


def claude_keychain_service(config_dir=None, kind='-credentials', oauth_suffix='', secure_dir=None):
    """Mirror the inspected Claude Code 2.1.322 service-name rule (S, not N).

    Name = 'Claude Code' + OAUTH_FILE_SUFFIX + kind + ('-' + sha256(dir)[:8]
    when CLAUDE_CONFIG_DIR is set, or CLAUDE_SECURESTORAGE_CONFIG_DIR is set
    and nonempty). The device-key store uses the fixed name
    'Claude Code-device-keys' with no directory suffix.
    """
    if secure_dir is not None:
        plain, directory = not secure_dir, secure_dir
    else:
        plain, directory = config_dir is None, config_dir
    suffix = ''
    if not plain:
        normalized = unicodedata.normalize('NFC', directory)
        suffix = '-' + hashlib.sha256(normalized.encode()).hexdigest()[:8]
    return 'Claude Code' + oauth_suffix + kind + suffix


_REPORT = re.compile(r'^(?:(?P<dups>\d+) duplicate reports for )?Sandbox: (?P<proc>.+?)\((?P<pid>\d+)\) '
                     r'(?P<action>allow|deny)(?:\((?P<count>\d+)\))? (?P<op>[a-z][a-z0-9*-]*)(?: (?P<target>.*))?$')


def parse_report(message, tag):
    """Parse one kernel Sandbox report carrying our tag; None if not ours."""
    if not isinstance(message, str):
        return None
    head, sep, rest = message.partition('\n')
    if not sep or rest.strip() != tag:
        return None
    match = _REPORT.match(head.strip())
    if not match:
        return None
    return {'process': match['proc'], 'pid': int(match['pid']), 'action': match['action'],
            'operation': match['op'], 'target': (match['target'] or '').strip(),
            'repeat': int(match['dups']) if match['dups'] else 1}


def parse_log_ndjson(text, tag):
    """Return our tagged reports from `log show --style ndjson` output."""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('{'):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        row = parse_report(entry.get('eventMessage'), tag)
        if row:
            rows.append(row)
    return rows


def summarize_reports(rows):
    """Collapse reports to sorted (action, operation, target, process) counts."""
    totals = {}
    for row in rows:
        key = (row['action'], row['operation'], row['target'], row['process'])
        totals[key] = totals.get(key, 0) + row['repeat']
    return [{'action': a, 'operation': o, 'target': t, 'process': p, 'count': n}
            for (a, o, t, p), n in sorted(totals.items())]


_ANSI = re.compile(r'\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[78=>]')


def strip_ansi(text):
    """Drop terminal control sequences and collapse whitespace for matching."""
    return re.sub(r'\s+', ' ', _ANSI.sub(' ', text))


def sanitize(value, replacements):
    """Recursively replace sensitive substrings (longest first) in strings."""
    pairs = sorted(((k, v) for k, v in replacements.items() if k), key=lambda kv: -len(kv[0]))

    def walk(item):
        if isinstance(item, str):
            for old, new in pairs:
                item = item.replace(old, new)
            return item
        if isinstance(item, list):
            return [walk(x) for x in item]
        if isinstance(item, dict):
            return {walk(k): walk(v) for k, v in item.items()}
        return item
    return walk(value)


def leaks(value, needles):
    """Return the needles still present anywhere in a JSON-serializable value."""
    text = json.dumps(value)
    return sorted(n for n in needles if n and n in text)
