"""Accept observed native behavior without assuming Ctrl-C stops local tools."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def check(data):
    assert data['native_tui'] is True
    assert data['mode'] in ('gate', 'interrupt')
    events = data['events']

    def rows(name):
        return [e for e in events if e['event'] == name]

    started = rows('tool_started')[0]
    pause = rows('pause_requested')[0]
    latch = rows('gate_latched')[0]
    observed = rows('tool_observed')[0]
    history = rows('tool_history')[0]['results']
    assert started['marker'] == 'started\n' and not started['finished_present']
    assert started['identity']['start_ticks'] > 0 and started['identity']['state'] != 'Z'
    assert started['native_alive'] and started['at'] < pause['at'] <= latch['at'] < observed['at']
    assert observed['at'] - pause['at'] >= 5
    assert latch['reason'] == 'monitor_eof'
    assert len(rows('gate_forward')) == len(rows('model_request')) == 1
    assert not any(e['at'] >= pause['at'] for e in rows('gate_forward') + rows('model_request'))
    assert any(e['later_prompt'] and e['at'] > pause['at'] for e in rows('gate_reject'))
    assert all(e['fixture_auth'] and e['bash_available'] for e in rows('model_request'))
    assert rows('fixed_tool_sent')[0]['command'] == data['command']
    assert rows('version')[0]['code'] == 0
    auth = json.loads(rows('auth_status')[0]['output'])
    assert auth['loggedIn'] and auth['storedClaudeAiLogin']
    assert auth['authMethod'] == 'claude.ai' and auth['apiProvider'] == 'firstParty'
    assert history and all(r['content']['tool_use_id'] == 'tool_pa1t' for r in history)
    observed_identity = observed['identity']
    same_alive = bool(observed_identity and observed_identity['start_ticks'] == started['identity']['start_ticks']
                      and observed_identity['state'] != 'Z')
    assert observed['same_identity_alive'] is same_alive
    completed = observed['finished'] == 'finished\n'
    ended_without_completion = observed['finished'] is None and not observed['same_identity_alive']
    ended_without_completion = ended_without_completion and any(r['content'].get('is_error') is True for r in history)
    assert data['outcome'] == {'completed': completed, 'ended_without_completion': ended_without_completion,
                               'terminated': False, 'cleanup_before_observation': False}
    if completed:
        assert rows('tool_finished')[0]['at'] > pause['at']
        assert rows('tool_finished')[0]['marker'] == 'finished\n'
        assert rows('tool_finished')[0]['marker_elapsed_seconds'] >= 3.9
        assert any('PA1T command finished' in str(r['content'].get('content', '')) for r in history)
    else:
        assert not rows('tool_finished')
    if not observed['same_identity_alive']:
        assert rows('tool_shell_gone')
    assert all(e['at'] > observed['at'] for e in rows('owned_cleanup'))
    if data['mode'] == 'interrupt':
        interrupt = rows('native_interrupt')[0]
        assert len(rows('native_interrupt')) == 1
        assert latch['at'] <= interrupt['at'] < observed['at']
        assert interrupt['identity']['start_ticks'] == started['identity']['start_ticks']
    else:
        assert not rows('native_interrupt')
    for name in ('tool_observed', 'later_prompt', 'history_view'):
        assert rows(name)[0]['native_alive']
    assert 'Claude Code' in data['terminal'] and 'Claude Max' in data['terminal']
    assert 'Showing detailed' in data['terminal']
    ending = rows('native_exit')[0]
    assert ending['exited'] and ending['code'] == 0 and ending['termios_restored']
    assert rows('credential_files')[0]['unchanged']


def main():
    root = HERE / 'evidence'
    manifest = json.loads((root / 'manifest.json').read_text())
    assert [e['mode'] for e in manifest['stages']] == ['guard', 'gate', 'interrupt']
    assert all(e['exit'] == 0 for e in manifest['stages'])
    assert len(manifest['binary_sha256']) == 64
    for source, digest in manifest['sources'].items():
        assert hashlib.sha256((HERE.parent / source).read_bytes()).hexdigest() == digest, source
    hashes = {row['file']: row['sha256'] for row in manifest['evidence_sha256']}
    for mode in ('gate', 'interrupt'):
        path = root / (mode + '.json')
        assert hashlib.sha256(path.read_bytes()).hexdigest() == hashes[path.name]
        check(json.loads(path.read_text()))
    print('Two actual native local-tool cases accepted; no general termination guarantee')


if __name__ == '__main__':
    main()
