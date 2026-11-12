"""Check the sanitized NM evidence files; raises AssertionError on a mismatch.

python3 -I -B check_evidence.py [STAGE...]   (default: every present stage)
A missing stage file is reported as missing, never as a pass.
"""
import json
from pathlib import Path
import re
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
    assert not re.search(r'quota-nm-[A-Za-z0-9]{6}(?![A-Za-z0-9])', text), 'unsanitized remote root name'


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
    assert cases['setuid_ps_sandboxed']['code'] == 71 and 'Operation not permitted' in cases['setuid_ps_sandboxed']['stderr']
    assert 'rc=126' in cases['setuid_ps_shell_child']['stdout'] and cases['setuid_ps_control']['code'] == 0
    assert all(int(mode, 8) & 0o4000 for mode in cases['setuid_modes'].values())
    assert cases['signal0_outside_sandboxed']['code'] == 0, 'default profile no longer allows outside signals'
    assert cases['signal0_outside_hardened']['code'] != 0
    assert 'rc=143' in cases['signal_same_sandbox_hardened']['stdout']
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


def _tool_confined(case):
    outputs = case['tool_outputs']
    for name in ('tool.json', 'envi.json', 'detached.json'):
        got = outputs[name]['results']
        assert got['gate_tcp4'] == 'connected', (name, got)
        for key in ('alt_tcp4', 'alt_tcp6', 'alt_udp4', 'outside_unix'):
            assert got[key] == DENIED, (name, key, got[key])
        assert got['launchservices'] == 'kern_return:1100', (name, got)
    assert outputs['detached.json']['reparented_to_launchd'] is True
    assert outputs['curl.txt'].strip().endswith('000')
    assert case['outside_listener_hits'] == {'alt_tcp': 0, 'alt_udp': 0, 'unix': 0}


def _native_ok(case, native):
    assert not case.get('error'), case.get('error')
    for key in ('tool_call_sent', 'tool_done', 'tool_result_returned', 'second_turn_reached_gate',
                'termios_changed_while_running', 'native_alive_before_exit', 'termios_restored',
                'terminal_has_reply'):
        assert case[key] is True, (native, key, case[key])
    assert case['exit_code'] == 0 and case['transcript_files_with_marker']
    model = [e for e in case['gate_events'] if e['event'] == native + '_model']
    assert model and all(e['bearer_matches'] for e in model), 'model request without the fixture bearer'
    _tool_confined(case)


def check_natives(data):
    _common(data)
    cases = data['cases']
    for name in ('claude', 'codex'):
        assert len(data['binaries'][name]['sha256']) == 64
    for key in ('claude-census-oauth-file', 'claude-enforced-oauth-file'):
        _native_ok(cases[key], 'claude')
        names = {a[-1] for a in cases[key]['security_shim_argv']
                 if a[:5] == ['find-generic-password', '-a', 'nmfixture', '-w', '-s']}
        assert any(re.match(r'^Claude Code-credentials-[0-9a-f]{8}$', n) for n in names), names
    for key in ('codex-census-plain', 'codex-enforced-plain'):
        _native_ok(cases[key], 'codex')
    daemon = cases['codex-enforced-daemon']
    assert 'failed to invoke ps for pid-managed app server: Operation not permitted' in daemon['terminal_tail']
    assert not any(e['event'] == 'codex_model' for e in daemon['gate_events'])
    census = {r['target'] for k in ('claude-census-oauth-file', 'codex-census-plain')
              for r in cases[k]['log']['summary'] if r['action'] == 'allow'}
    assert census and not census & {'com.apple.SecurityServer', 'com.apple.coreservices.launchservicesd'}


def check_natives_extra(data):
    _common(data)
    cases = data['cases']
    _native_ok(cases['claude-enforced-setup-token'], 'claude')
    real = cases['claude-enforced-real-security']
    _native_ok(real, 'claude')
    assert 'security_shim_argv' not in real
    assert _denies(real['log'], 'mach-lookup', 'com.apple.SecurityServer', 'security')


def check_compat(data):
    _common(data)
    cases = data['cases']
    assert cases['git_clone_local_path']['code'] == 0
    assert cases['git_clone_http_fixture']['code'] == 128 and 'Failed to connect' in cases['git_clone_http_fixture']['stderr']
    assert cases['ssh_fixture_port']['code'] == 255 and 'Operation not permitted' in cases['ssh_fixture_port']['stderr']
    for key in ('curl_fixture', 'localhost_name_curl'):
        assert cases[key]['code'] == 7, key
    assert cases['pip_download_fixture']['code'] != 0
    assert cases['npm_view_fixture'].get('skipped') or cases['npm_view_fixture']['code'] != 0
    assert cases['alt_hits_from_sandboxed'] == 0
    assert cases['control_curl_outside']['code'] == 0 and cases['alt_hits_total'] == 1


def _outputs(case):
    return [e.get('tool_output') or '' for e in case['gate_events']]


def check_nested(data):
    _common(data)
    claude, codex = data['cases']['claude-enforced-native-sandbox'], data['cases']['codex-enforced-native-sandbox']
    for case, native in ((claude, 'claude'), (codex, 'codex')):
        assert not case.get('error') and case['exit_code'] == 0 and case['termios_restored'] is True
        assert case['tool_call_sent'] and case['tool_result_returned'] and not case['tool_done'], native
        assert case['second_turn_reached_gate'] and case['outside_listener_hits'] == {'alt_tcp': 0, 'alt_udp': 0, 'unix': 0}
    assert any('Sandbox is enabled but failed to initialize: EPERM' in o and 'Sandboxing is disabled for the rest'
               in o for o in _outputs(claude)), 'Claude nested sandbox outcome changed'
    assert _denies(claude['log'], 'network-bind', process='2.1.322')
    assert any('sandbox-exec: sandbox_apply: Operation not permitted' in o and 'exited with code 71' in o
               for o in _outputs(codex)), 'Codex nested seatbelt outcome changed'


def check_refresh(data):
    _common(data)
    cases = data['cases']
    for case in cases.values():
        assert not case.get('error') and case['exit_code'] == 0 and case['termios_restored'] is True
        assert case['outside_listener_hits'] == {'alt_tcp': 0, 'alt_udp': 0, 'unix': 0}
    direct = cases['claude-enforced-refresh-direct']
    assert not any(e['event'] == 'connect_refused' for e in direct['gate_events'])
    assert _denies(direct['log'], 'network-outbound', '/private/var/run/mDNSResponder', '2.1.322')
    proxy = {e['path'] for e in cases['claude-enforced-refresh-proxy']['gate_events'] if e['event'] == 'connect_refused'}
    assert {'platform.claude.com:443', 'api.anthropic.com:443'} <= proxy, proxy
    codex = cases['codex-enforced-refresh-override']['gate_events']
    assert sum(e['event'] == 'post_refused' and e['path'] == '/oauth/token' for e in codex) >= 1
    assert any(e['event'] == 'codex_model' and e['bearer_matches'] for e in codex), 'expired bearer still used'


CHECKS = {'boundary': check_boundary, 'ipc': check_ipc, 'fds': check_fds, 'keychain': check_keychain,
          'natives': check_natives, 'natives-extra': check_natives_extra, 'compat': check_compat,
          'nested': check_nested, 'refresh': check_refresh}


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
