"""Check recorded PC3 native observations without a native CLI or network."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent / 'evidence'


def read(name):
    return json.loads((HERE / (name + '.json')).read_text())


def event(data, kind):
    matches = [row for row in data['events'] if row['event'] == kind]
    assert len(matches) == 1, kind
    return matches[0]


def main():
    manifest = read('manifest')
    assert {row['label'] for row in manifest['runs']} == {'guard', 'ui', 'delegation'}
    assert all(row['exit'] == 0 for row in manifest['runs'])
    assert all('"root_exit": 0' in row['stderr'] for row in manifest['runs'])
    assert len(manifest['binary_sha256']) == 64
    sources = read('sources')
    assert sources['tag'] == 'rust-v0.167.2'
    assert len(sources['resolved_commit']) == 40
    assert len(sources['files']) == 6
    assert all(len(row['sha256']) == 64 for row in sources['files'])

    ui = read('ui')
    boot = event(ui, 'subscription_ui_bootstrap')
    assert boot['ready'] and boot['exit'] is None and not boot['discovery_error']
    paused = event(ui, 'paused_ui')
    assert paused['ui_alive'] and paused['history_available'] and paused['stream_disconnected']
    assert paused['turn_status'] == 'interrupted'
    admitted = [row for row in ui['events'] if row['event'] == 'admit']
    assert {row['work_kind'] for row in admitted} == {'root', 'title'}
    survivors = [row for row in admitted if row['request_id'] in paused['active_request_ids']]
    assert [row['work_kind'] for row in survivors] == ['title']
    exited = event(ui, 'ui_exit')
    assert exited['exited'] and exited['exit'] == 0 and exited['termios_restored']

    delegation = read('delegation')
    graph = event(delegation, 'native_delegation')
    assert graph['grandchild_request_observed']
    assert len({graph['root_id'], graph['child_id'], graph['grandchild_id']}) == 3
    for parent, child, key in [('root_id', 'child_id', 'root_spawn'),
                               ('child_id', 'grandchild_id', 'child_spawn')]:
        spawn = graph[key]
        assert spawn['status'] == 'completed' and spawn['tool'] == 'spawnAgent'
        assert spawn['senderThreadId'] == graph[parent]
        assert spawn['receiverThreadIds'] == [graph[child]]
    requests = [row for row in delegation['events'] if row['event'] == 'admit']
    assert len(requests) == 5
    assert {row['session_id'] for row in requests} == {graph['root_id']}
    assert {row['marker'] for row in requests} == {
        'Fixture delegation root', 'Fixture delegation child', 'Fixture delegation grandchild'}
    remaining = event(delegation, 'delegation_after_root_interrupt')['active_request_ids']
    assert {row['marker'] for row in requests if row['request_id'] in remaining} == {
        'Fixture delegation child', 'Fixture delegation grandchild'}
    assert event(delegation, 'delegation_after_individual_interrupts')['all_disconnected']
    for data in (ui, delegation):
        assert event(data, 'preflight')['model_requests'] == 0
        assert any(row['event'] == 'get' and row['path'] == '/api/codex/accounts/check'
                   for row in data['events'])
        for row in data['events']:
            if row['event'] == 'admit':
                assert row['service'] == 'gate' and row['bearer_matches_fixture']
                assert row['account'] == 'fixture-account-a'
        assert '<PROBE_ROOT>' in json.dumps(data)
        assert '/tmp/pc-ui-' not in json.dumps(data)
    print('PASS: sourced discovery, native TUI, title, two-level delegation and scoped interruption')
    print('LIMIT: fabricated backend only; no exhaustive boundary or full-A proof')


if __name__ == '__main__':
    main()
