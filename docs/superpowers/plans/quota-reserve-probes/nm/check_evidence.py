"""Check the sanitized NM evidence files; raises AssertionError on a mismatch.

python3 -I -B check_evidence.py [STAGE...]   (default: every present stage)
A missing stage file is reported as missing, never as a pass.
"""
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
DENIED = 'error:PermissionError:1'
PY_CASES = ('root_python', 'shell_child_python', 'env_i_absolute_python', 'detached_setsid_python')
SENSITIVE = ('/Users/', '/home/')


def _common(data):
    assert data.get('completed') is True, 'stage did not complete: %s' % data.get('error')
    assert data.get('leftover_processes_after') == [], 'leftover processes after cleanup'
    text = json.dumps(data)
    for needle in SENSITIVE:
        assert needle not in text, 'unsanitized path prefix %s' % needle


def _denies(log, operation, target=None, process=None):
    return any(r['action'] == 'deny' and r['operation'] == operation
               and (target is None or r['target'] == target)
               and (process is None or r['process'] == process) for r in log['summary'])


def check_boundary(data):
    _common(data)
    assert set(data['cases']) == {'census', 'enforced'}
    for kind, block in data['cases'].items():
        cases = block['sandboxed']
        for name in PY_CASES:
            result = cases[name]['detached_result'] if name.startswith('detached') else cases[name]['parsed']
            assert result, '%s/%s produced no result' % (kind, name)
            got = result['results']
            for key in ('gate_tcp4', 'gate_tcp6', 'gate_mapped6', 'private_unix_self'):
                assert got[key] == 'connected', (kind, name, key, got[key])
            for key in ('alt_tcp4', 'alt_tcp6', 'alt_mapped6', 'alt_udp4', 'alt_udp6', 'outside_unix'):
                assert got[key] == DENIED, (kind, name, key, got[key])
        detached = cases['detached_setsid_python']['detached_result']
        assert detached['reparented_to_launchd'] and detached['sid'] != cases['root_python']['parsed']['sid']
        node = cases['env_i_absolute_node']['parsed']['results']
        assert node['gate_tcp4'] == node['gate_tcp6'] == 'connected' and node['gate_fetch'] == 'status:200'
        assert all(node[k] == 'error:EPERM' for k in ('alt_tcp4', 'alt_tcp6', 'alt_udp4', 'alt_fetch')), node
        assert cases['curl_alt4']['code'] == cases['curl_alt6']['code'] == 7
        assert cases['nc_alt4']['code'] != 0 and cases['nc_gate4']['code'] == 0
        assert block['listener_hits_from_sandboxed'] == {'alt': 0, 'udp': 0, 'unix': 0}, kind
        control = block['outside_control']['parsed']['results']
        assert all(v in ('connected', 'sent') for v in control.values()), control
        assert all(n > 0 for n in block['listener_hits_from_control'].values())
        assert _denies(block['log'], 'network-outbound'), 'no tagged network denial logged'


def check_ipc(data):
    _common(data)
    cases, log = data['cases'], data['log']
    inside = cases['mach_lookup_sandboxed']['parsed']['results']
    outside = cases['mach_lookup_control']['parsed']['results']
    assert all(v == 'found' for v in outside.values()), outside
    for name, value in inside.items():
        expect = 'found' if name == 'com.apple.system.opendirectoryd.libinfo' else 'kern_return:1100'
        assert value == expect, (name, value)
    assert _denies(log, 'job-creation', process='launchctl')
    assert cases['launchctl_submit_sandboxed']['code'] != 0
    cleanup = cases['launchctl_cleanup']
    assert cleanup['label_listed_code'] != 0 and not cleanup['marker_written']
    assert cases['open_url_sandboxed']['code'] != 0
    assert '-10814' in cases['open_url_control']['stderr'], 'control did not reach LaunchServices'
    assert _denies(log, 'mach-lookup', 'com.apple.lsd.mapdb', 'open') or \
        _denies(log, 'mach-lookup', 'com.apple.coreservices.launchservicesd', 'open')
    assert cases['anchor_write_sandboxed']['code'] != 0 and not cases['anchor_write_sandboxed']['file_exists']
    assert cases['workspace_write_sandboxed']['file_exists']
    dns = cases['dns_sandboxed']['parsed']['results']
    assert dns['localhost'].startswith('resolved') and dns['invalid_name'].startswith('error')
    assert _denies(log, 'network-outbound', '/private/var/run/mDNSResponder')
    nested = cases['nested_sandbox_exec']
    assert nested['code'] == 71 and 'sandbox_apply: Operation not permitted' in nested['stderr']
    unix = cases['outside_daemon_unix']['parsed']['results']
    assert unix['outside_unix'] == DENIED and unix['outside_unix_self'] == DENIED
    assert unix['private_unix_self'] == 'connected' and cases['outside_daemon_hits'] == 0


def check_fds(data):
    _common(data)
    cases = data['cases']
    assert set(cases['inherited']['parsed']['results'].values()) == {'written'}
    assert set(cases['closed_before_confinement']['parsed']['results'].values()) == {'error:OSError:9'}
    hits = cases['listener_hits']
    for key in ('alt_connections', 'udp_datagrams', 'unix_connections'):
        assert 'NM-INHERITED-WRITE\n' in hits[key], key


def check_keychain(data):
    _common(data)
    cases = data['cases']
    found = 'The specified item could not be found in the keychain.'
    invalid = 'One or more parameters passed to a function were not valid.'
    for name in ('control_outside', 'store_allowed', 'store_denied'):
        assert cases[name]['code'] == 44, name
    assert found in cases['store_allowed']['stderr'] and invalid not in cases['store_allowed']['stderr']
    assert invalid in cases['store_denied']['stderr']
    assert not _denies(data['log_store_allowed'], 'mach-lookup', 'com.apple.SecurityServer')
    assert _denies(data['log_store_denied'], 'mach-lookup', 'com.apple.SecurityServer', 'security')
    names = data['claude_names_for_probe_config']
    assert names['device_keys_fixed'] == 'Claude Code-device-keys'
    assert names['credentials'].startswith('Claude Code-credentials-') and len(names['credentials']) == 32


CHECKS = {'boundary': check_boundary, 'ipc': check_ipc, 'fds': check_fds, 'keychain': check_keychain}


def check_stage(stage, data):
    CHECKS[stage](data)


def main(stages):
    missing = []
    for stage in stages or sorted(CHECKS):
        path = HERE / 'evidence' / (stage + '.json')
        if not path.exists():
            missing.append(stage)
            continue
        check_stage(stage, json.loads(path.read_text()))
        print('ok', stage)
    if missing:
        print('missing (not a pass):', ' '.join(missing))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
