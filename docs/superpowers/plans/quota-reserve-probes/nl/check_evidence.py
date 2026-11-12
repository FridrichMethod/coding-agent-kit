"""Semantic checks of the committed NL evidence; raises AssertionError on any gap.

Ground truth is the fixture listeners' record (markers received outside every
boundary), cross-checked with the actors' own rows. Controls must succeed, so a
broken fixture cannot pass as a denial. Run: python3 -I -B check_evidence.py
"""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent / 'evidence'
PYTHON_ACTORS = ('root', 'shell', 'envi', 'detached')
DIRECT_TCP = ('tcp4_alt', 'tcp6_alt', 'mapped_alt', 'tfo_alt')
NATIVE_NAMES = ('claude-inet', 'claude-inet-unix', 'codex-inet', 'codex-inet-no-daemon', 'codex-inet-unix',
                'codex-inet-unix-outside-daemon')


def load(name):
    return json.loads((HERE / (name + '.json')).read_text())


def rows(run):
    return run['rows'] + run.get('detached_rows', [])


def result(run, actor, attempt):
    found = [r['result'] for r in rows(run) if r.get('actor') == actor and r.get('attempt') == attempt]
    assert len(found) == 1, (actor, attempt, found)
    return found[0]


def markers(data, event):
    return {e.get('marker') for e in data['events'] if e['event'] == event}


def check_manifest():
    manifest = load('manifest')
    assert manifest['runner_exit'] == 0, 'runner failed'
    assert not manifest['transient_units_left'], manifest['transient_units_left']
    for name, stage in manifest['stages'].items():
        assert stage['exit'] == 0, (name, stage['exit'])
        assert stage['leftover_processes_killed'] == 0 and stage['roots_left_by_stage'] == 0, name
    for name, digest in manifest['evidence_sha256'].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == digest, name
    assert set(manifest['evidence_sha256']) == {p.name for p in HERE.glob('*.json')} - {'manifest.json'}
    for name, digest in manifest['fixture_sha256'].items():
        # Evidence is bound to the probe code that produced it; edit code, rerun run.py.
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == digest, name
    return manifest


def check_host():
    host = load('host')
    landlock = host['landlock']
    assert landlock['abi'] == 8 and landlock['net_bits_accepted'] == [0, 1], landlock
    assert landlock['scope_bits_accepted'] == [0, 1] and max(landlock['fs_bits_accepted']) == 15
    assert landlock['uapi_header']['mentions_tsync'] and not landlock['uapi_header']['mentions_udp']
    assert not landlock['uapi_header']['mentions_resolve_unix']
    refusals = host['refusals']
    assert refusals['bwrap_unshare_net']['exit'] != 0 and 'RTM_NEWADDR' in refusals['bwrap_unshare_net']['stderr']
    assert refusals['bwrap_unshare_user']['exit'] != 0 and refusals['unshare_map_root_net']['exit'] != 0
    for name in ('default', 'legacy_landlock', 'network_enabled'):
        assert host['codex_sandbox'][name]['exit'] != 0, name
        assert host['codex_sandbox'][name]['non_loopback_inet_attempts'] == [], name
    checks = host['systemd_user']['checks']
    assert checks['service_IPAddressDeny_any']['listener_reached'], 'IPAddressDeny unexpectedly enforced'
    assert checks['service_PrivateNetwork_yes']['listener_reached']
    assert checks['scope_IPAddressDeny_any']['listener_reached']
    assert not checks['service_RestrictAddressFamilies_AF_UNIX']['listener_reached']
    assert checks['scope_RestrictAddressFamilies_AF_UNIX']['exit'] != 0
    assert host['systemd_user']['units_after'] == ['LoadState=not-found']


def check_matrix():
    data = load('matrix')
    runs = data['runs']
    alt, udp = markers(data, 'alt_tcp'), markers(data, 'alt_udp')
    gate, other = markers(data, 'gate_tcp'), markers(data, 'other_host_tcp')
    relay = markers(data, 'daemon_request')
    abstract = markers(data, 'abstract_request')
    for run_id in ('L0', 'L1', 'L2a', 'L2b'):
        run = runs[run_id]
        assert run['exit'] == 0 and run['root_ended_before_detached'], run_id
        identity = [r for r in run['detached_rows'] if r['attempt'] == 'detached_identity'][0]
        root_pid = [r['pid'] for r in run['rows'] if r.get('actor') == 'root'][0]
        # Double-forked into a new session (led by the exited middle child) and reparented.
        assert identity['root_done_seen'] and root_pid not in (identity['ppid'], identity['sid']), identity
        assert identity['sid'] != identity['pid'], identity
        for actor in PYTHON_ACTORS + ('node',):
            for attempt in ('tcp4_gate', 'tcp6_gate'):
                assert '%s:%s:%s' % (run_id, actor, attempt) in gate, (run_id, actor, attempt)
        assert '%s:root:curl_tcp4_gate' % run_id in gate
        if run_id != 'L0':
            assert run['boundary'] and run['boundary'][0]['seccomp'] == {'L1': 'none', 'L2a': 'inet',
                                                                         'L2b': 'inet+unix'}[run_id]
            assert run['escape_unit_after'] == ['LoadState=not-found', 'ActiveState=inactive'], run_id
    victims = {e['sender_pid'] for e in data['events'] if e['event'] == 'victim_signal'}
    for actor in PYTHON_ACTORS:
        # Controls: the unconfined concurrent process reaches every fixture.
        for attempt in ('tcp4_alt', 'tcp6_alt', 'mapped_alt', 'tfo_alt', 'mptcp_alt', 'gate_port_other_host'):
            assert 'L0:%s:%s' % (actor, attempt) in alt | other, (actor, attempt)
        for attempt in ('udp4_alt', 'udp6_alt', 'udp4_connected_alt'):
            assert 'L0:%s:%s' % (actor, attempt) in udp, (actor, attempt)
        assert 'relay:L0:%s:unix_daemon_relay' % actor in alt and 'L0:%s:unix_abstract' % actor in abstract
        assert result(runs['L0'], actor, 'dbus_session_auth') == 'ok'
        pids = {r['pid'] for r in rows(runs['L0']) if r.get('actor') == actor}
        assert pids & victims, ('L0 signal not delivered', actor)
        for run_id in ('L1', 'L2a', 'L2b'):
            run = runs[run_id]
            for attempt in DIRECT_TCP:
                assert '%s:%s:%s' % (run_id, actor, attempt) not in alt, (run_id, actor, attempt)
            assert result(run, actor, 'tcp4_alt') == 'EACCES'
            assert '%s:%s:gate_port_other_host' % (run_id, actor) in other, 'port-only rule not observed'
            assert result(run, actor, 'signal_outside') == 'EPERM'
            assert not {r['pid'] for r in rows(run) if r.get('actor') == actor} & victims
            assert '%s:%s:unix_abstract' % (run_id, actor) not in abstract
        l1 = runs['L1']
        assert 'L1:%s:mptcp_alt' % actor in alt, 'MPTCP gap not observed'
        assert {'L1:%s:udp4_alt' % actor, 'L1:%s:udp6_alt' % actor} <= udp, 'UDP gap not observed'
        assert 'relay:L1:%s:unix_daemon_relay' % actor in alt and result(l1, actor, 'dbus_session_auth') == 'ok'
        for run_id in ('L2a', 'L2b'):
            run = runs[run_id]
            assert '%s:%s:mptcp_alt' % (run_id, actor) not in alt
            assert not {m for m in udp if m.startswith(run_id + ':')}
            assert result(run, actor, 'udp4_alt') == 'EPROTONOSUPPORT'
        assert result(runs['L2a'], actor, 'dbus_session_auth') == 'EAFNOSUPPORT'
        assert result(runs['L2a'], actor, 'unix_daemon_relay') == 'EAFNOSUPPORT'
        assert 'relay:L2b:%s:unix_daemon_relay' % actor in alt
        assert result(runs['L2b'], actor, 'dbus_session_auth') == 'ok'
    for run_id in ('L1', 'L2a', 'L2b'):
        for attempt in ('tcp4_alt', 'tcp6_alt'):
            assert '%s:node:%s' % (run_id, attempt) not in alt
        assert not {m for m in alt if m and m.startswith(run_id + ':root:curl')}
    assert {'L0:node:tcp4_alt', 'L0:node:udp4_alt'} <= alt | udp and 'L0:root:curl_tcp4_alt' in alt
    assert 'L1:node:udp4_alt' in udp and 'L2a:node:udp4_alt' not in udp
    assert not {m for m in alt | udp | relay | abstract if m and 'L2a:' in m}, 'L2a reached a fixture'
    escapes = {m for m in alt if m and ':escaped:' in m}
    assert escapes == {'L1:escaped:tcp4_alt', 'L2b:escaped:tcp4_alt'}, escapes
    spawn = [r for r in runs['L2a']['rows'] if r['attempt'] == 'systemd_run_spawn'][0]
    assert spawn['code'] != 0 and 'Address family not supported' in spawn['stderr']
    stages = data['stages']
    leak, closed = stages['INH-leak'], stages['INH-closed']
    assert {r['attempt']: r['result'] for r in leak['rows']} == {
        'inherited_tcp': 'ok', 'inherited_udp': 'ok', 'inherited_listener': 'ok'}
    assert 'INH-leak:inh:inherited_tcp' in alt and 'INH-leak:inh:inherited_udp' in udp
    assert leak['outside_peer_saw'] == 'INH-leak:inh:inherited_listener'
    assert {r['result'] for r in closed['rows']} == {'EBADF'} and len(closed['rows']) == 3
    assert closed['outside_peer_saw'] == 'error ConnectionRefusedError'
    assert not {m for m in alt | udp if m and m.startswith('INH-closed')}
    latch = {r['attempt']: r['result'] for r in stages['LATCH']['rows']}
    assert latch['stream_before_latch'] == 'ok'
    assert latch['stream_after_latch'] in ('eof', 'EPIPE', 'ECONNRESET'), latch
    assert latch['gate_reconnect_after_latch'] == 'ECONNREFUSED'
    assert latch['alt_after_latch'] == 'EACCES' and latch['udp_after_latch'] == 'EPROTONOSUPPORT'
    assert not [e for e in stages['LATCH']['gate_received'] if e['after_latch']]
    reuse = stages['REUSE']
    assert reuse['impostor_bind_while_held'] == 'EADDRINUSE'
    assert [r['result'] for r in reuse['held']['rows']] == ['timeout']
    assert [(r['result'], r.get('reply')) for r in reuse['free']['rows']] == [('ok', 'IMPOSTOR')]


def native_common(data, run):
    assert data['tool_started'] and data['tool_finished'] and data['tool_result_returned'], run
    assert data['detached_finished'] and data['termios_restored'], run
    assert data['boundary_applied'] and data['terminal_markers']['tool_output_visible'], run
    assert data['terminal_markers']['fixture_reply_visible'], run
    requests = [e for e in data['events'] if e['event'] == 'model_request']
    assert any(e.get('tool_call') and e['fabricated_auth'] for e in requests), run
    assert any(e.get('tool_result') for e in requests), run
    assert data['lingering_after_kill'] == 0, run
    for item in data['native_only_syscalls']['inet_connect']:
        assert item['target'] in ('gate', 'other_loopback') or item['result'] != 'ok', (run, item)
        assert item['target'] != 'alt' and not (item['target'] == 'non_loopback' and item['result'] == 'ok')


def check_natives():
    seen = {}
    for name in NATIVE_NAMES:
        data = load(name)
        seen[name] = data
        events = data['fixture_events']
        reached = {e.get('marker') for e in events if e['event'] in ('alt_tcp', 'alt_udp')}
        if name == 'codex-inet':
            # Default daemon mode cannot reach its control socket: fail closed before any model request.
            assert data['native_exit'] not in (0, None) and not data['tool_started'], name
            assert not [e for e in data['events'] if e['event'] == 'model_request'], name
            assert 'Address family not supported' in data['terminal_tail'], name
            continue
        native_common(data, name)
        tool = {r['attempt']: r['result'] for r in data['tool_rows']}
        detached = {r['attempt']: r['result'] for r in data['detached_rows']}
        for table in (tool, detached):
            assert table['tcp4_gate'] == 'ok' and table['udp4_alt'] == 'EPROTONOSUPPORT', name
            assert table['mptcp_alt'] == 'EPROTONOSUPPORT' and table['signal_outside'] == 'EPERM', name
        if name.endswith('outside-daemon'):
            # The tool ran in the outside daemon's domain: the invocation boundary was bypassed.
            assert tool['tcp4_alt'] == 'ok' and detached['tcp4_alt'] == 'ok', name
            assert any(m and m.endswith('codex_tool:tcp4_alt') for m in reached), name
            continue
        for table in (tool, detached):
            assert table['tcp4_alt'] == 'EACCES' and table['tcp6_alt'] == 'EACCES', name
        assert not {m for m in reached if m and not m.startswith('relay:')}, (name, reached)
        if name.endswith('-unix'):
            assert tool['unix_daemon_relay'] == 'ok', name
        else:
            assert tool['unix_daemon_relay'] == 'EAFNOSUPPORT' and not reached, name
            assert data['native_exit'] == 0, name
    assert seen['codex-inet-unix']['lingering_killed'] >= 1, 'self-started Codex daemon did not linger'
    assert seen['codex-inet-unix-outside-daemon']['outside_daemon_start']['exit'] == 0


def check_compat():
    runs = load('compat')['runs']
    table = {layer: {c['case']: c for c in run['cases']} for layer, run in runs.items()}
    network = ('git_http_clone_alt', 'curl_download_alt', 'python_urllib_alt', 'node_fetch_alt')
    for case in network + ('local_tcp_server', 'local_unix_server', 'multiprocessing_default_pool',
                           'multiprocessing_fork_pool', 'git_local_commit', 'dns_localhost'):
        assert table['L0'][case]['exit'] == 0, case
    assert 'Connection reset' in table['L0']['ssh_alt']['output'] or 'kex_exchange' in table['L0']['ssh_alt']['output']
    assert any(hit['event'] == 'alt_ssh' for hit in runs['L0']['fixture_hits'])
    for layer in ('L2a', 'L2b'):
        assert not runs[layer]['fixture_hits'], layer
        for case in network + ('local_tcp_server',):
            assert table[layer][case]['exit'] != 0, (layer, case)
        assert 'Permission denied' in table[layer]['ssh_alt']['output'], layer
        for case in ('git_local_commit', 'dns_localhost', 'multiprocessing_fork_pool'):
            assert table[layer][case]['exit'] == 0, (layer, case)
    assert table['L2a']['local_unix_server']['exit'] != 0 and table['L2a']['multiprocessing_default_pool']['exit'] != 0
    assert table['L2b']['local_unix_server']['exit'] == 0 and table['L2b']['multiprocessing_default_pool']['exit'] == 0


def main():
    check_manifest()
    check_host()
    check_matrix()
    check_natives()
    check_compat()
    print('NL evidence checks passed')


if __name__ == '__main__':
    main()
