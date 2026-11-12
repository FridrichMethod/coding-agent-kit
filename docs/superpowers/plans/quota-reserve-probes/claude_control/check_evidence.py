"""Offline bounded-evidence acceptance. No native process or network here."""
import hashlib
import json
from pathlib import Path

MODES = ('bootstrap', 'oauth', 'fullauth', 'delegation', 'eof', 'monitor_death',
         'lease', 'gate_death', 'continuation')
LOSSES = ('eof', 'monitor_death', 'lease', 'gate_death')
HERE = Path(__file__).resolve().parent


def check(data):
    assert data.get('native_tui') is True, 'actual native TUI required'
    events = data['events']
    mode = data['mode']

    def rows(name):
        return [e for e in events if e['event'] == name]

    requests = rows('model_request')
    assert requests and all(e['fixture_auth'] for e in requests)
    assert all(e['path'] == '/v1/messages?beta=true' for e in requests)
    assert len({e['request_id'] for e in requests}) == len(requests)
    assert rows('bootstrap')[0]['ready'] and rows('bootstrap')[0]['native_alive']
    assert rows('history_view')[0]['native_alive']
    assert 'Showing detailed' in ''.join(data['terminal'])
    ending = rows('native_exit')[0]
    assert ending['exited'] and ending['code'] == 0 and ending['termios_restored']
    assert rows('credential_files')[0]['unchanged']
    assert all(e['exit'] == 0 and e['data']['loggedIn'] for e in rows('auth_status'))
    if mode in (*LOSSES, 'fullauth', 'continuation'):
        assert 'Claude Max' in data['terminal'][0]
        assert all(e['data']['authMethod'] == 'claude.ai' and e['data']['storedClaudeAiLogin']
                   and e['data']['apiProvider'] == 'firstParty' for e in rows('auth_status'))
    if mode not in ('bootstrap', 'oauth', 'fullauth'):
        assert {'root', 'child', 'grandchild'} <= {e['kind'] for e in requests}
        assert rows('nested')[0]['observed']
        children = []
        for kind, tool in (('root', 'tool_fixture_root'), ('child', 'tool_fixture_child')):
            result = next(t for e in requests if e['kind'] == kind
                          for t in e['tool_results'] if t['tool_use_id'] == tool)
            assert result['background'] and len(result['agent_ids']) == 1
            children += result['agent_ids']
        assert len(set(children)) == 2
    if mode == 'delegation':
        assert rows('after_interrupt')[0]['active_kinds'] == ['child', 'grandchild']
    if mode in LOSSES:
        trigger = rows('loss_trigger')[0]
        owned = set(trigger['active_ids'])
        assert len(owned) == 3
        closed = {e['request_id'] for e in rows('stream_disconnected') if e['at'] >= trigger['at']}
        assert owned <= closed
        observed = rows('loss_observed')[0]
        assert all(observed[k] for k in ('all_owned_closed', 'unrelated_alive',
                                        'unrelated_stream_active', 'native_alive'))
        assert any(e['kind'] == 'unrelated' and e['gate_id'] is None for e in requests)
        assert not any(e['at'] > trigger['at'] and e['kind'] != 'unrelated' for e in requests)
        if mode == 'gate_death':
            assert rows('gate_exit')[0]['code'] == -9
        else:
            latch = rows('gate_latched')[0]
            expected = 'lease_expired' if mode == 'lease' else 'monitor_eof'
            assert latch['reason'] == expected
            assert not any(e['at'] >= latch['at'] for e in rows('gate_forward'))
            assert any(e['old_queue'] for e in rows('gate_reject'))
            assert any(e['later_prompt'] for e in rows('gate_reject'))
        if mode == 'lease':
            assert rows('heartbeat_after_latch')[0]['ignored']
    if mode == 'continuation':
        assert len(rows('shared_source')) == 2
        negative = rows('manual_negative_controls')[0]
        assert all(value for key, value in negative.items() if key not in ('event', 'at'))
        assert [e['passed'] for e in rows('fake_quota_check')] == [True, False, True]
        assert [e['sequence'] for e in rows('fake_quota_request')] == [1, 2, 3]
        grants = rows('gate_grant')
        assert len(grants) == 2 and grants[1]['at'] >= rows('fake_quota_check')[2]['at']
        pause = rows('gate_pause')[0]
        assert not any(pause['at'] < e['at'] < grants[1]['at'] for e in rows('gate_forward'))
        assert any(e['manually_continued'] and e['at'] >= grants[1]['at'] for e in requests)
        result = rows('manual_continuation')[0]
        assert result['native_alive'] and result['fake_quota_only'] and not result['physical_human_verified']
        assert all(value for key, value in rows('outer_terminal')[0].items() if key not in ('event', 'at'))


def main():
    root = HERE / 'evidence'
    manifest = json.loads((root / 'manifest.json').read_text())
    assert [row['mode'] for row in manifest['stages']] == ['guard', *MODES]
    assert all(row['exit'] == 0 for row in manifest['stages'])
    for name, digest in manifest['sources'].items():
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == digest, name
    evidence_hashes = {row['file']: row['sha256'] for row in manifest['evidence_sha256']}
    for mode in MODES:
        path = root / ('case-' + mode + '.json')
        assert hashlib.sha256(path.read_bytes()).hexdigest() == evidence_hashes[path.name]
        check(json.loads(path.read_text()))
    print('Nine Claude native scenario checks passed; full A remains unproved')


if __name__ == '__main__':
    main()
