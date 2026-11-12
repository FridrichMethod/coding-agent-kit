"""Offline PC5 evidence checks. A missing native run is not capability success."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODES = ('delegation', 'tool', 'tool_interrupt')


def validate(data):
    events = data['events']
    def rows(name):
        return [e for e in events if e['event'] == name]
    def one(name):
        result = rows(name)
        assert len(result) == 1, name
        return result[0]

    grants = rows('gate_grant')
    assert [e['grant'] for e in grants] == [1, 2]
    pause = rows('gate_pause')[0]
    assert pause['reason'] == 'quota' and pause['active_ids']
    initial = rows('fresh_check')[0]
    assert initial['allowed'] and initial['http_reads'] == 1
    assert initial['at'] <= grants[0]['at']
    assert min(e['at'] for e in rows('upstream_request')) >= grants[0]['at']
    assert all(not (pause['at'] < e['at'] < grants[1]['at']) for e in rows('gate_forward'))
    closed = {e['request_id'] for e in rows('upstream_disconnected') if e['at'] < grants[1]['at']}
    assert closed == set(pause['active_ids'])
    assert any(e['old_queue'] and pause['at'] < e['at'] < grants[1]['at'] for e in rows('gate_reject'))
    assert one('prebuffer_discarded')['challenge_absent']
    assert one('prebuffer_discarded')['grants'] == 1
    assert one('held_and_paste_no_grant')['grants'] == 1
    assert one('quota_recovery_no_grant')['grants'] == 1
    assert one('old_confirmation_no_replay')['grants'] == 2
    cases = rows('continuation_case')
    assert [e['label'] for e in cases] == ['failed', 'stale', 'five_hour', 'weekly', 'passing']
    assert [(e['grants'], e['paused']) for e in cases] == [(1, True)] * 4 + [(2, False)]
    checks = rows('fresh_check')[1:]
    assert [e['label'] for e in checks] == [e['label'] for e in cases]
    assert [e['allowed'] for e in checks] == [False] * 4 + [True]
    assert all(e['http_reads'] == 1 for e in checks)
    assert checks[-1]['at'] <= grants[1]['at']
    resumed = [e for e in rows('upstream_request') if e['resumed']]
    assert len(resumed) == 1 and resumed[0]['at'] >= grants[1]['at']
    history = one('history_after_pause')
    assert history['available'] and history['ui_alive']
    ui = one('native_status_ui')
    assert ui['ui_alive'] and ui['grants'] == 2
    assert 'Context window:' in ui['output'] and 'Model provider:' in ui['output']
    terminal = one('terminal_exit')
    assert terminal['exit'] == 0 and terminal['native_restored'] and terminal['outer_restored']
    assert one('resize')['ui_alive']
    assert '[quota-research] Type ' in data['visible_terminal']
    assert {e['method'] for e in rows('alternate_route_probe')} == {'GET', 'POST', 'CONNECT'}
    assert all(e['status'] == 503 and e['synthetic'] for e in rows('alternate_route_probe'))
    if data['mode'] == 'delegation':
        native = one('native_delegation')
        assert len({native['root'], native['child'], native['grandchild']}) == 3
        assert native['root_spawn']['receiverThreadIds'] == [native['child']]
        assert native['child_spawn']['receiverThreadIds'] == [native['grandchild']]
        assert native['root_spawn']['status'] == native['child_spawn']['status'] == 'completed'
        assert {'Fixture delegation root', 'Fixture delegation child', 'Fixture delegation grandchild', 'title'} <= {
            e['kind'] for e in rows('upstream_request')}
    else:
        tool = one('local_tool_after_gate_pause')
        assert isinstance(tool['finished'], bool)
        if data['mode'] == 'tool':
            assert tool['finished']
        assert tool['native_interrupt'] == (data['mode'] == 'tool_interrupt')
        if data['mode'] == 'tool_interrupt':
            assert one('local_tool_interrupt')['result'] == {}
            turn = one('interrupted_tool_history')['history']['thread']['turns'][-1]
            assert turn['status'] == 'interrupted'
            completed = any(i['type'] == 'commandExecution' and i['status'] == 'completed'
                and i.get('exitCode') == 0 for i in turn['items'])
            assert completed == tool['finished']
            assert 'probe_error' not in one('explicit_old_queue_start')['result']


def main():
    out = HERE / 'evidence'
    manifest = json.loads((out / 'manifest.json').read_text())
    assert [(r['label'], r['exit']) for r in manifest['runs']] == [(n, 0) for n in ('guard', *MODES)]
    for name, digest in manifest['fixture_sha256'].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == digest, name
    for name, digest in manifest['evidence_sha256'].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest, name
    for mode in MODES:
        validate(json.loads((out / (mode + '.json')).read_text()))
    print('PC5 native evidence, input separation, quotas and tool boundaries: PASS')


if __name__ == '__main__':
    main()
