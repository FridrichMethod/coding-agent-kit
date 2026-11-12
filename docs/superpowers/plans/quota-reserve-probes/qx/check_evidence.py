"""Check the committed QX native evidence; exits nonzero on any missing or contrary fact."""
import json
from pathlib import Path
import re
import sys

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE / 'evidence'
A, B = 'fixture-account-a', 'fixture-account-b'


def load(name):
    return json.loads((EVIDENCE / (name + '.json')).read_text())


def timeline(data, event):
    return [entry for entry in data['timeline'] if entry['event'] == event]


def gate_events(data, index=0, kind=None):
    return [e for e in data['gates'][index]['events'] if kind is None or e['event'] == kind]


def check(condition, message, failures):
    if not condition:
        failures.append(message)


def main():
    failures = []
    text = ''.join(path.read_text() for path in sorted(EVIDENCE.glob('*.json')))
    # Fabricated tokens are base64 JSON ("eyJ..."); refresh values carry a fixed prefix.
    check(not re.search(r'eyJ[A-Za-z0-9_-]{10,}|fixture-refresh-|/home/|/Users/', text),
          'credential-like or machine-specific text in evidence', failures)
    manifest = load('manifest')
    check(manifest.get('version') == 'codex-cli 0.167.2', 'native version', failures)
    check(all(run['exit'] == 0 for run in manifest['runs']), 'a run failed', failures)
    check([run['label'] for run in manifest['runs']][2:] ==
          ['guard-self-check', 'shape', 'rotation', 'proactive', 'identity', 'concurrent'], 'stage list', failures)

    shape = load('shape')
    usage = [e for e in gate_events(shape, kind='get') if e['path'] == '/backend-api/wham/usage']
    first = usage[0]
    check((first['method'], first['query'], first['credential'], first['authorization_scheme'])
          == ('GET', False, 'T1', 'Bearer'), 'usage request identity', failures)
    check(first['shown'].get('chatgpt-account-id') == A, 'usage account header', failures)
    check(first['headers'] == ['accept', 'authorization', 'chatgpt-account-id', 'host', 'user-agent'],
          'usage header set', failures)
    reads = {entry['label']: entry['result'] for entry in timeline(shape, 'rate_limits_read')}
    base = reads['baseline']['rateLimits']
    check((base['primary']['windowDurationMins'], base['secondary']['windowDurationMins']) == (300, 10080),
          'baseline durations', failures)
    swapped = reads['swapped-slots']['rateLimits']
    check((swapped['primary']['windowDurationMins'], swapped['secondary']['windowDurationMins']) == (10080, 300),
          'native keeps slots as sent', failures)
    family = reads['other-family-row']['byLimitId']
    check(family.get('codex_bengalfox', {}).get('limitName') == 'gpt-fixture-spark'
          and reads['other-family-row']['rateLimits']['limitId'] == 'codex', 'other family row', failures)
    check(reads['percent-over-100']['rateLimits']['primary']['usedPercent'] == 150, 'native passes 150', failures)
    check(reads['zero-window-seconds']['rateLimits']['primary']['windowDurationMins'] is None, 'zero window', failures)
    check(reads['account-mismatch']['accountId'] == B and reads['account-mismatch']['ordinaryUsageAllowed'] is None,
          'native passes mismatched account through', failures)
    for name in ('fractional-percent', 'missing-plan', 'http-401', 'http-503', 'non-json'):
        check('rpc_error' in reads[name], name + ' should fail natively', failures)
    cases = {entry['name']: entry for entry in timeline(shape, 'case')}
    expected = {'swapped-slots': 'fresh', 'rate-limit-null': 'no-codex-family', 'percent-over-100': 'out-of-range',
                'reset-in-past': 'reset-anomaly', 'unknown-plan': 'unsupported-plan',
                'usage-based-plan': 'unsupported-plan', 'account-mismatch': 'account-mismatch',
                'user-mismatch': 'user-mismatch', 'identity-absent': 'account-mismatch', 'http-401': 'failure'}
    for name, outcome in expected.items():
        check(cases[name]['lease_outcome'] == outcome, 'lease outcome ' + name, failures)
    check(all(entry['refresh_connects'] == 0 for entry in cases.values()), 'usage 401 started a refresh', failures)
    check(not [u for u in shape['upstream'] if u['kind'] in ('model', 'refresh')], 'shape sent model/refresh', failures)
    check(sum(u['kind'] == 'usage' for u in shape['upstream']) == len(reads), 'one upstream GET per read', failures)
    check(any(e['path'] == '/backend-api/wham/rate-limit-reset-credits' for e in gate_events(shape, kind='get')),
          'reset-credit detail path', failures)
    check(timeline(shape, 'get_auth_status_include_token')[0]['matches'] == {'T1': True}, 'token export', failures)

    rotation = load('rotation')
    events = gate_events(rotation)
    t2_usage = next(i for i, e in enumerate(events) if e['event'] == 'get'
                    and e['path'].endswith('/usage') and e['credential'] == 'T2')
    t2_upstream = [i for i, e in enumerate(events) if e['event'] == 'model_upstream' and e['credential'] == 'T2']
    check(t2_upstream and min(t2_upstream) > t2_usage, 'T2 model bytes before T2 usage', failures)
    refused = [e for e in events if e['event'] == 'admission' and not e['admitted']]
    check([(e['credential'], e['reason']) for e in refused] == [('T2', 'rebind-required')], 'rotation refusal', failures)
    after = timeline(rotation, 'after_native_refresh')[0]
    check(after['refreshes'] == 1 and after['stored'] == {'T1': False, 'T2': True}, 'native refresh persisted', failures)
    turns = [entry['status'] for entry in timeline(rotation, 'turn')]
    check(turns == ['completed', 'failed', 'completed'], 'rotation turns', failures)

    proactive = {entry['variant']: entry for entry in timeline(load('proactive'), 'proactive')}
    hooked, host = proactive['hooked'], proactive['production-host']
    check(hooked['upstream_order'].index('refresh') < hooked['upstream_order'].index('usage')
          and hooked['usage_credentials'] == ['T2'] and not hooked['connects'], 'hooked proactive refresh', failures)
    check(host['connects'] and set(host['connects']) == {'auth.openai.com:443'}
          and host['usage_credentials'] == ['T1'] and 'refresh' not in host['upstream_order'],
          'production refresh host', failures)

    identity = load('identity')
    events = gate_events(identity)
    check(not [e for e in events if e['event'] == 'model_upstream' and e['credential'] == 'TB'], 'B reached upstream', failures)
    admissions = [(e['credential'], e['shown'].get('chatgpt-account-id'), e['admitted'], e['reason'])
                  for e in events if e['event'] == 'admission']
    check(admissions == [('TA', A, True, 'admitted'), ('TB', B, False, 'identity-changed'),
                         ('TB', B, False, 'identity-changed'), ('TA', A, False, 'paused:identity-changed'),
                         ('TA', A, True, 'admitted')], 'identity admissions', failures)
    check(timeline(identity, 'lease_after_fresh_a')[0]['paused'] == 'identity-changed'
          and timeline(identity, 'lease_after_continuation')[0]['paused'] is None, 'continuation gate', failures)

    concurrent = load('concurrent')
    forced = timeline(concurrent, 'session1_forced_refresh')[0]
    check(forced['secret'] == {'T1': False, 'T2': True} and forced['links'] == [True, True]
          and forced['secret_mode'] == '0o600' and forced['secret_links'] == 1, 'shared secret rotation', failures)
    check(timeline(concurrent, 'final')[0]['refreshes'] == 1, 'second session refreshed on its own', failures)
    second = [(e['credential'], e['admitted'], e['reason']) for e in gate_events(concurrent, 1, 'admission')]
    check(second == [('T1', True, 'admitted'), ('T1', True, 'admitted'), ('T2', False, 'rebind-required'),
                     ('T2', True, 'admitted')], 'session two admissions', failures)
    first = [(e['credential'], e['admitted'], e['reason']) for e in gate_events(concurrent, 0, 'admission')]
    check(first == [('T2', False, 'rebind-required'), ('T2', True, 'admitted')], 'session one admissions', failures)
    statuses = [model['status'] for model in gate_events(concurrent, 1, 'model_upstream')]
    check(statuses == [200, 401, 200], 'session two upstream statuses', failures)

    for message in failures:
        print('FAIL:', message)
    print('checked QX evidence:', 'FAILED' if failures else 'OK')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
