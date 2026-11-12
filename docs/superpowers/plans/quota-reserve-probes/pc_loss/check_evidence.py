"""Offline PC4 evidence assertions; absence or failed native stages never pass."""
import json
import hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent / 'evidence'
MODES = ('eof', 'monitor_death', 'lease', 'proxy_death')


def one(data, name):
    rows = [row for row in data['events'] if row['event'] == name]
    assert len(rows) == 1, (name, len(rows))
    return rows[0]


def main():
    assert (HERE / 'manifest.json').exists(), 'PC4 native loss matrix not captured'
    manifest = json.loads((HERE / 'manifest.json').read_text())
    assert [row['label'] for row in manifest['runs']] == ['guard', *MODES]
    assert all(row['exit'] == 0 and '"root_exit": 0' in row['stderr']
               for row in manifest['runs'])
    assert len(manifest['binary_sha256']) == 64
    for name, digest in manifest['fixture_sha256'].items():
        assert hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest() == digest, name
    for name, digest in manifest['evidence_sha256'].items():
        assert hashlib.sha256((HERE / name).read_bytes()).hexdigest() == digest, name
    for mode in MODES:
        data = json.loads((HERE / (mode + '.json')).read_text())
        assert one(data, 'preflight')['model_requests'] == 0
        assert one(data, 'gate_ready')['admission'] == 'closed'
        assert one(data, 'initial_closed_probe')['status'] == 503
        assert one(data, 'initial_closed_probe')['upstream_requests'] == 0
        baseline = one(data, 'before_loss')
        assert len(baseline['active_ids']) >= 2
        assert one(data, 'all_owned_closed')['closed_ids'] == baseline['active_ids']
        assert one(data, 'after_loss')['upstream_unchanged']
        assert one(data, 'after_loss')['history_available']
        assert one(data, 'unrelated')['alive'] and one(data, 'unrelated')['stream_active']
        assert one(data, 'unrelated')['turn_status'] == 'inProgress'
        queued = one(data, 'queue_before_loss')['result']['queuedSubmission']
        assert queued['input'][0]['text'] == 'Fixture previously queued prompt'
        trigger = one(data, 'loss_trigger')['at']
        assert one(data, 'all_owned_closed')['at'] >= trigger
        posts = [e for e in data['events'] if e['event'] == 'upstream_request']
        assert all(e['bearer_matches_fixture'] and e['account'] == 'fixture-account-a' for e in posts)
        if mode in ('eof', 'proxy_death'):
            assert {e['work_kind'] for e in posts} >= {'root', 'title'}
            assert one(data, 'after_loss')['ui_alive']
            assert one(data, 'ui_exit')['termios_restored']
            assert one(data, 'ui_exit')['exit'] == 0
        else:
            graph = one(data, 'native_delegation')
            assert len({graph['root_id'], graph['child_id'], graph['grandchild_id']}) == 3
            for parent, child, key in [('root_id', 'child_id', 'root_spawn'),
                                       ('child_id', 'grandchild_id', 'child_spawn')]:
                assert graph[key]['status'] == 'completed'
                assert graph[key]['senderThreadId'] == graph[parent]
                assert graph[key]['receiverThreadIds'] == [graph[child]]
        if mode == 'proxy_death':
            assert one(data, 'proxy_exit')['exit'] == -9
        else:
            closed = one(data, 'gate_latched')
            assert closed['reason'] == ('lease_expired' if mode == 'lease' else 'monitor_eof')
            assert sorted(closed['active_ids']) == baseline['active_ids']
            assert closed['at'] >= trigger
            rejected = [e for e in data['events'] if e['event'] == 'gate_reject' and e['at'] >= closed['at']]
            assert any(e['old_queue'] for e in rejected)
            assert any(e['programmatic'] for e in rejected)
            assert all(e['at'] <= closed['at'] for e in data['events'] if e['event'] == 'gate_forward')
            closed_ids = {e['request_id'] for e in data['events'] if e['event'] == 'gate_stream_closed'}
            assert set(baseline['active_ids']) <= closed_ids
            if mode == 'lease':
                assert one(data, 'heartbeat_after_latch')['ignored']
                assert one(data, 'after_loss')['monitor_alive']
            elif mode == 'eof':
                assert one(data, 'after_loss')['monitor_alive']
            else:
                assert one(data, 'monitor_exit')['exit'] == -9
        assert '<PROBE_ROOT>' in json.dumps(data)
        assert '/tmp/pc-loss-' not in json.dumps(data)
    print('PASS: bounded native loss matrix; all owned fake-upstream streams closed')
    print('LIMIT: Linux HTTP/SSE fixture only; no remote cancellation or full-A proof')


if __name__ == '__main__':
    main()
