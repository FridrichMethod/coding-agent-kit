"""KLX evidence checker: decisive properties of every native stage, bound to the manifest.

python3 -I -B check_evidence.py [EVIDENCE_DIR]

It fails (exit 1) when a stage is missing, failed, leaked a credential or a private path, left a
process behind, admitted a bearer without its own fresh observation, forwarded model work
between a latch and its continuation, or when any evidence or source digest differs from the
manifest. It also recomputes the G0 measurements and requires them to equal the manifest's.
"""
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

HERE = Path(__file__).resolve().parent
STAGES = ('pause', 'loss', 'restart', 'sessions', 'rotation', 'rotation_gate', 'refresh', 'startup')
SOURCES = ('klx/gate.py', 'klx/fixtures.py', 'klx/reader.py', 'klx/monitor.py', 'klx/actors.py', 'klx/launcher.py',
           'klx/compose.py', 'klx/run.py', 'qx/binding.py', 'qx/gate.py', 'qx/fixtures.py', 'nl/boundary.py',
           'nl/natives.py', 'nl/probe.py', 'pc_continue/control.py')
LEAKS = (re.compile(r'eyJ[A-Za-z0-9_-]{8,}'), re.compile(r'fixture-refresh-[0-9a-f]'), re.compile(r'/home/'),
         re.compile(r'/tmp/quota-klx'), re.compile(r'"Bearer '))


class Failure(Exception):
    pass


def need(condition, message):
    if not condition:
        raise Failure(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def timeline(data, event):
    return [row for row in data['timeline'] if row['event'] == event]


def one(data, event):
    rows = timeline(data, event)
    need(rows, '%s: no %s event' % (data['stage'], event))
    return rows[-1]


def gate_events(inv):
    return [event for gate in inv['gates'] for event in gate['events']]


def bound_admissions(stage, gate):
    """Every admitted request's bearer had a fresh usage observation in its own epoch. Only records
    written under the admission lock (usage observations, admission decisions) move the epoch."""
    current = fresh = None
    for event in gate['events']:
        kind = event['event']
        credential = event.get('credential')
        if kind in ('get', 'self_read') and (kind == 'self_read' or event['path'].endswith('/usage')):
            if event.get('account') not in (None, 'fixture-account-a') and kind == 'get':
                current = fresh = None
                continue
            if credential != current:
                current, fresh = credential, None
            if event.get('lease') == 'fresh':
                fresh = credential
        elif kind == 'refused' and event.get('reason') == 'identity-changed':
            current = fresh = None
        elif kind == 'admission' and credential is not None:
            if credential != current:
                current, fresh = credential, None
            if kind == 'admission' and event.get('admitted'):
                need(credential == fresh, '%s: %s admitted without its own fresh observation' % (stage, credential))


def no_model_between(data, start, end, label):
    late = [row for row in data['backend'] if row['kind'] == 'model' and start < row['at'] < end]
    need(not late, '%s: %d model requests reached upstream during %s' % (data['stage'], len(late), label))


def common(data):
    stage = data['stage']
    need(data['ok'] is True and data['error'] is None, '%s: stage failed: %s' % (stage, data['error']))
    need(data['lingering_after_kill'] == 0, '%s: processes left after cleanup' % stage)
    for inv in data['invocations']:
        for gate in inv['gates']:
            bound_admissions(stage, gate)
        if inv['tui_exit'] is not None:
            need(inv['tui_exit'] == 0, '%s/%s: native exit %s' % (stage, inv['name'], inv['tui_exit']))
        trace = inv.get('trace')
        if trace:
            need(trace['native_main_exit'] == 0, '%s/%s: traced native did not exit 0' % (stage, inv['name']))
            for row in trace['syscalls']['inet_connect']:
                need(row['target'] != 'non_loopback', '%s/%s: non-loopback connect' % (stage, inv['name']))
                if row['target'] != 'gate':
                    need(row['result'] in ('EACCES', 'ECONNREFUSED'), '%s/%s: non-gate connect %s' %
                         (stage, inv['name'], row['result']))
    for row in timeline(data, 'preflight'):
        need(row['model_requests'] == 0, '%s: model request before the preflight verdict' % stage)


def check_pause(data):
    inv = data['invocations'][0]
    latch = data['extra']['latch_at']
    first = inv['gates'][0]['events']
    closed = [e for e in first if e['event'] == 'streams_closed' and e['reason'] == 'threshold:five_hour']
    need(closed and closed[0]['count'] >= 2, 'pause: held streams not closed at the latch')
    lost = {row['marker'] for row in data['backend'] if row['kind'] == 'stream_lost' and latch < row['at'] < latch + 1}
    need({'KLX-HOLD-title', 'KLX-HOLD-child'} <= lost, 'pause: upstream streams survived the latch')
    resume = [e['at'] for e in first if e['event'] == 'continuation_requested']
    need(len(resume) >= 2, 'pause: continuation attempts missing')
    no_model_between(data, latch, resume[-1], 'the pause')
    rows = {(r['actor'], r['attempt']): r for r in inv['actors']}
    need(rows[('sdk', 'model_after_signal')]['result'] == 503, 'pause: child request after the latch not refused')
    need(rows[('grandchild', 'model_after_latch')]['result'] == 503, 'pause: grandchild request not refused')
    need(rows[('grandchild', 'started')]['ppid'] == 1, 'pause: grandchild was not detached')
    need(rows[('grandchild', 'direct_backend_tcp')]['result'] == 'EACCES', 'pause: direct egress not denied')
    need(rows[('grandchild', 'direct_udp')]['result'] == 'EPROTONOSUPPORT', 'pause: UDP not denied')
    for tag in ('before', 'after'):
        need(rows[('workflow' if tag == 'before' else 'grandchild', 'git_clone_' + tag)]['result'] != 0,
             'pause: git over a non-gate port worked')
        need(rows[('workflow' if tag == 'before' else 'grandchild', 'pip_index_' + tag)]['result'] == 'EACCES',
             'pause: pip-style download over a non-gate port worked')
    models = [r for r in inv['actors'] if r['actor'] == 'testlike' and r['attempt'] == 'gate_models']
    need(any(r['result'] == 200 and r['at'] < latch for r in models), 'pause: test-like read before latch missing')
    need(models and all(r['result'] == 503 for r in models if latch + .05 < r['at'] < resume[-1]),
         'pause: test-like read admitted while paused')
    queued = [e for e in first if e['event'] == 'admission' and e.get('marker') == 'KLX-QUEUED-1']
    need(queued and all(not e['admitted'] for e in queued if e['at'] > latch), 'pause: queued input admitted after latch')
    need(any(float(at) > latch + 1 for _, at in inv['loop']), 'pause: local loop stopped at the pause')
    need(one(data, 'stale_input_after_pause')['checks'] == 0, 'pause: stale typed input reached a check')
    paste = one(data, 'paste_and_output_no_check')
    need(paste['checks'] == 0 and paste['status']['paused'], 'pause: paste or native output confirmed')
    need(one(data, 'recovered_quota_no_resume')['status']['paused'], 'pause: recovered quota resumed by itself')
    need(one(data, 'tui_usable_while_paused')['status_card'], 'pause: native /status not usable while paused')
    failed = one(data, 'failed_check')
    need(failed['resumed'] is False and failed['message_visible'], 'pause: failed check resumed or no message')
    resumed = one(data, 'resumed_same_invocation')
    need(resumed['resumed'] and resumed['reply_visible'], 'pause: fresh passing check did not reopen')
    need(resumed['gate_pid'] == inv['gates'][0]['pid'] and len(inv['gates']) == 1, 'pause: not the same gate')
    need(resumed['tui_pid'] == one(data, 'tui_started')['pid'], 'pause: not the same native')


def check_loss(data):
    cases = data['extra']['loss_cases']
    need([case['reason'] for case in cases] == ['monitor-eof', 'monitor-eof', 'monitor-stale'], 'loss: latch reasons')
    recoveries = timeline(data, 'loss_recovery')
    need(len(recoveries) == 3, 'loss: recoveries missing')
    pids = {row['tui_pid'] for row in recoveries}
    need(len(pids) == 1, 'loss: native restarted')
    for case, recovery in zip(cases, recoveries):
        need(case['refusal_visible'] and case['status_card'] and case['tui_alive'], 'loss: TTY unusable')
        need(case['monitor_alive'] == (case['case'] != 'kill'), 'loss: monitor liveness')
        need(recovery['refused_before_continuation'] and recovery['resumed'] and recovery['completed'],
             'loss: recovery needs and honours continuation')
        later = [e['at'] for e in data['invocations'][0]['gates'][cases.index(case) + 1]['events']
                 if e['event'] == 'continuation_requested']
        need(later, 'loss: no continuation after restart')
        no_model_between(data, case['latch_at'], later[-1], 'monitor loss ' + case['case'])


def check_restart(data):
    inv = data['invocations'][0]
    for key in ('bind_while_alive', 'bind_while_dead', 'bind_after_restart'):
        results = dict(map(tuple, data['extra'][key]))
        need(results['loopback'] == 'EADDRINUSE' and results['wildcard'] == 'EADDRINUSE', 'restart: port taken: ' + key)
    rows = {r['attempt']: r['result'] for r in inv['actors'] if r['actor'] == 'bind'}
    need(rows['bind_gate_port_loopback'] == 'EADDRINUSE' and rows['bind_gate_port_wildcard'] == 'EADDRINUSE',
         'restart: in-boundary process took the gate port')
    need(rows['direct_backend_tcp'] == 'EACCES', 'restart: direct egress while the gate was dead')
    need(rows['gate_port_request'] in ('timeout', 'eof'), 'restart: something answered on the dead gate port')
    need(data['extra']['models_while_dead'] == 0, 'restart: model request while the gate was dead')
    need(one(data, 'restarted_latched')['status']['paused'] == 'gate-restarted', 'restart: gate restarted open')
    result = one(data, 'restart_continuation')
    need(result['refused_before'] and result['resumed'] and result['completed'], 'restart: continuation')
    again = [e['at'] for e in inv['gates'][1]['events'] if e['event'] == 'continuation_requested']
    no_model_between(data, data['extra']['gate_killed_at'], again[-1], 'gate death and restart')


def check_sessions(data):
    result = one(data, 'independent_sessions')
    need(result['s1_refused'] and result['s2_completed'], 'sessions: not independent')
    need(result['s1']['paused'] and not result['s2']['paused'], 'sessions: wrong lease states')
    need(one(data, 'second_paused')['s2_refused'], 'sessions: outside consumption did not count')
    tunnels = data['extra']['proxy_tunnels']
    need(tunnels and all(row['allowed'] for row in tunnels), 'sessions: second gate bypassed the launcher proxy')


def check_rotation(data):
    inv = data['invocations'][0]
    events = gate_events(inv)
    need(any(row['kind'] == 'refresh' and row['host'] == 'auth.openai.com' for row in data['backend']),
         'rotation: native refresh did not use the CONNECT allowlist')
    need(one(data, 'native_refresh')['completed'] or data['extra']['rebind_mode'] == 'controller-reader',
         'rotation: gate-read rebind did not carry the turn across the refresh')
    after = [r for r in timeline(data, 'long_lived_read') if r['phase'] == 'after-refresh']
    need(after and 'rpc_error' in after[0]['result'], 'rotation: long-lived reader did not show its stale bearer')
    rows = {r['marker']: r for r in inv['actors'] if r['attempt'].startswith('model_after')}
    need(rows['KLX-C-B-reread']['result'] == 503, 'rotation: B bearer was not refused')
    need(rows['KLX-C-NEW-reread']['result'] == 200, 'rotation: child did not adopt the new bearer')
    need(rows['KLX-C-OLD-again']['result'] in (503, 401), 'rotation: old bearer result')
    switch = one(data, 'switch_to_b')
    need(switch['refused'] and switch['resumed_with_b_on_disk'] is False, 'rotation: A -> B continued')
    need(switch['status']['paused'] == 'identity-changed', 'rotation: identity not latched')
    back = one(data, 'switch_back')
    need(back['resumed'] and back['completed'], 'rotation: back on A did not resume with continuation')
    need(any(e['event'] == ('self_read' if data['extra']['rebind_mode'] == 'gate-read' else 'rebind_hold_ended')
             for e in events), 'rotation: rebind mechanism not exercised')


def check_refresh(data):
    first, second = data['invocations']
    events, others = gate_events(first), gate_events(second)
    need(one(data, 'connect_refresh')['completed'], 'refresh: turn after CONNECT refresh did not complete')
    need([r['host'] for r in data['backend'] if r['kind'] == 'refresh'] == ['auth.openai.com'],
         'refresh: refreshes other than the single native CONNECT refresh')
    need(first['reader_refresh_failures'] > 0, 'refresh: reader refresh attempt not observed')
    running = [r for r in first['actors'] if r['actor'] == 'tunnel' and r['attempt'].startswith('connect_')]
    need([r['result'] for r in running[:2]] == ['tunnel', 'tunnel'], 'refresh: tunnel while running')
    need(all(' 403 ' in str(r['result']) + ' ' for r in running[2:4]) and len(running) == 4,
         'refresh: tunnel while paused')
    need(any(e['event'] == 'connect_refused' and e['destination'] == 'auth.openai.com:443' and e['paused']
             for e in events), 'refresh: paused CONNECT not refused')
    need(not any(e['event'] == 'connect_tunnel' for e in others), 'refresh: tunnel without an allowlist')
    need(any(e['event'] == 'connect_refused' and e['destination'] == 'auth.openai.com:443' for e in others),
         'refresh: no refused native refresh in the no-refresh case')
    stale = one(data, 'no_refresh_stale')
    need(stale['paused'] and stale['status']['paused'] == 'stale', 'refresh: expiry did not pause stale')
    need(one(data, 'no_refresh_refused')['refused'], 'refresh: stale pause admitted work')
    latch = [e['at'] for e in others if e['event'] == 'streams_closed' and e['reason'] == 'stale'][0]
    no_model_between(data, latch, latch + 60, 'the stale pause')


def check_startup(data):
    checks = {row['case']: row for row in data['extra']['startup_checks']}
    for case, row in checks.items():
        expected = case not in ('version-real', 'args plain prompt', 'host actual')
        need(row['refused'] == expected, 'startup: %s refused=%s' % (case, row['refused']))
    boundary = data['extra']['socket_stdio_boundary']
    need(boundary['exit'] == 2 and not boundary['native_started'], 'startup: socket stdio not refused')
    cases = data['extra']['preflight_cases']
    need(len(cases) == 10, 'startup: preflight cases missing')
    for row in cases:
        need(row['model_requests'] == 0 and not row['tui_started'], 'startup: native started for ' + row['case'])
        need((row['verdict'] is None) == (row['case'] == 'passing-control'), 'startup: verdict for ' + row['case'])
    need(not [r for r in data['backend'] if r['kind'] == 'model'], 'startup: model request reached upstream')


CHECKS = {'pause': check_pause, 'loss': check_loss, 'restart': check_restart, 'sessions': check_sessions,
          'rotation': check_rotation, 'rotation_gate': check_rotation, 'refresh': check_refresh,
          'startup': check_startup}


def metrics(stages):
    """G0 measurements from the evidence (loopback, fixtures; not live numbers)."""
    preflight, usage, overhead, holds, gate_holds, close = [], [], [], [], [], []
    for stage, data in stages.items():
        if stage == 'startup':  # refusal cases with deliberately slow or failing fixtures
            continue
        models = sorted((row['at'], row) for row in data['backend'] if row['kind'] == 'model')
        for inv in data['invocations']:
            preflight += [r['total_seconds'] for r in inv['readers'] if r['why'] == 'preflight' and r['total_seconds']]
            for event in gate_events(inv):
                if event['event'] == 'get' and event['path'].endswith('/usage') and event.get('seconds') is not None:
                    usage.append(event['seconds'])
                elif event['event'] == 'admission' and event.get('admitted') and event.get('sent') \
                        and event['sent'] - event['received'] < .05:  # no rebind hold in this request
                    arrival = next((at for at, _ in models if at >= event['sent'] - 1e-6), None)
                    if arrival is not None and arrival - event['received'] < 1:
                        overhead.append(arrival - event['received'])
                elif event['event'] == 'rebind_hold_ended' and event.get('seconds') is not None:
                    (gate_holds if event.get('mode') == 'gate-read' else holds).append(event['seconds'])
                elif event['event'] == 'streams_closed' and event['count'] and event['reason'].startswith(
                        ('threshold', 'monitor')):
                    lost = [row['at'] for row in data['backend'] if row['kind'] == 'stream_lost'
                            and event['at'] <= row['at'] < event['at'] + 2]
                    if lost:
                        close.append(max(lost) - event['at'])

    def summary(values):
        if not values:
            return None
        values = sorted(values)
        return {'n': len(values), 'median_ms': round(statistics.median(values) * 1000, 1),
                'max_ms': round(values[-1] * 1000, 1)}
    return {'preflight_reader': summary(preflight), 'usage_get_at_gate': summary(usage),
            'admission_overhead': summary(overhead), 'controller_rebind_hold': summary(holds),
            'gate_read_rebind_hold': summary(gate_holds), 'latch_to_upstream_close': summary(close)}


def check(directory=HERE / 'evidence'):
    directory = Path(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    need(manifest.get('exit') == 0, 'manifest: run did not finish with exit 0')
    stages = {}
    for stage in STAGES:
        path = directory / (stage + '.json')
        need(path.exists(), 'missing evidence for ' + stage)
        text = path.read_text()
        need(manifest['evidence'].get(stage + '.json') == digest(path), 'digest mismatch for ' + stage)
        for leak in LEAKS:
            need(not leak.search(text), '%s: evidence leaks %s' % (stage, leak.pattern))
        data = json.loads(text)
        need(data['stage'] == stage, 'stage name mismatch for ' + stage)
        common(data)
        CHECKS[stage](data)
        stages[stage] = data
    for source in SOURCES:
        need(manifest['sources'].get(source) == digest(HERE.parent / source), 'source changed since capture: ' + source)
    need(manifest.get('metrics') == metrics(stages), 'metrics differ from the manifest')
    return manifest


def main(argv):
    try:
        manifest = check(argv[0] if argv else HERE / 'evidence')
    except (Failure, KeyError, IndexError, ValueError, OSError) as error:
        print('KLX evidence check FAILED:', type(error).__name__, error)
        return 1
    print('KLX evidence check passed:', len(STAGES), 'stages;', json.dumps(manifest['metrics']))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
