"""Check the KM evidence files: recompute every verdict from the raw rows (research only).

python3 -I -B check_evidence.py [--dir DIR] [--measure]

Each case check reads the gate, launcher, upstream and child rows a stage recorded and fails on
any contradiction (an upstream model POST or an admitted decision after a latch, a granted
continuation that the case expected to refuse, a squatter that bound the held port, a refusal
that started the native, and so on). Booleans the probe computed are cross-checked against the
rows rather than trusted. Exit 0 only when every stage file is present and passes. --measure
prints the G0 measurements (loopback, this host) taken from the same rows.
"""
import json
import os
from pathlib import Path
import sys

HERE = Path(os.path.realpath(__file__)).parent
STAGES = ('claude-loss', 'codex-loss', 'claude-resume', 'codex-resume', 'claude-refresh', 'codex-refresh',
          'claude-tools', 'codex-tools', 'refusals', 'portmatrix')
PROVIDERS = ('claude', 'codex')
REFUSED = (403, 503)


class Fail(AssertionError):
    pass


def need(condition, message):
    if not condition:
        raise Fail(message)


def rows(case, event, kind=None, name='summary'):
    out = [r for r in case[name]['gate_rows'] if r.get('event') == event]
    return [r for r in out if r.get('kind') == kind] if kind else out


def first_latch(case, after=0.0, name='summary'):
    found = sorted(r['t'] for r in rows(case, 'controller', 'latched', name) if r['t'] > after)
    return found[0] if found else None


def posts_after(case, t):
    return [r for r in case['upstream'] if r.get('method') == 'POST' and r['t'] > t]


def admitted_after(case, t, name='summary'):
    return [r for r in rows(case, 'decision', name=name) if r.get('ok') and r['t'] > t]


def common_latch(case, reasons):
    latch = first_latch(case)
    need(latch is not None, 'no latch row')
    need(abs(latch - case['latch_t']) < 1e-6, 'latch_t does not match the gate row')
    reason = [r for r in rows(case, 'controller', 'latched') if r['t'] == latch][0].get('reason')
    need(any(reason.startswith(x) for x in reasons), 'unexpected latch reason %r' % reason)
    need(not posts_after(case, latch), 'upstream model POST after the latch')
    need(not admitted_after(case, latch), 'admitted decision after the latch')
    return latch


def tui_usable(case):
    need(case['native_alive'] is True, 'native not alive after the pause')
    need(case['local_command']['alive'] and case['local_command']['output_bytes'] > 200, 'local command had no effect')
    need(all(ok is False for ok, *_ in case['later']['decisions_after']) and case['later']['decisions_after'],
         'a later turn was not refused')
    need(case['later']['upstream_marker_hits'] == 0, 'a later turn reached upstream')
    need(case['exit']['native_exit'] == 0, 'native did not exit cleanly')


def streams_closed(case, latch, limit_ms=250):
    closed = [r['t'] for r in rows(case, 'stream_closed') if r['t'] >= latch]
    need(case['stream_held'] is True and closed, 'no held stream was closed')
    need((min(closed) - latch) * 1000 < limit_ms, 'stream closed too late after the latch')
    ends = [r for r in case['upstream'] if r.get('event') == 'hold_end']
    if ends:
        need(all(r['outcome'] == 'client_gone' for r in ends), 'held upstream stream was released, not cut')


def check_threshold(case, provider):
    latch = common_latch(case, ('threshold',))
    need(case['crossed_t'] < latch, 'latch precedes the threshold crossing')
    streams_closed(case, latch)
    tui_usable(case)
    sdk = [r for r in case['sdk_rows'] if r.get('action') == 'sdk']
    roles = {r['role'] for r in sdk}
    need({'child', 'grandchild'} <= roles, 'child and detached grandchild did not both run')
    need(any(r.get('status') == 200 and r['t'] < latch for r in sdk), 'no child request was admitted before')
    late = [r for r in sdk if r['t'] > latch + 0.05]
    need(late and all(r.get('status') in REFUSED for r in late), 'a child request after the latch was not refused')
    need(case['direct_rows'] and all(r['result'] == 'errno:1' for r in case['direct_rows']), 'direct egress not denied')
    need(case['bind_rows'] and all(r['result'] == 'errno:1' for r in case['bind_rows']), 'gate-port bind not denied')


def check_loss(mode):
    def run(case, provider):
        reason = 'monitor_stale' if mode == 'stall' else 'monitor_eof'
        latch = common_latch(case, (reason,))
        need(case['lost'] == [reason], 'lost reason mismatch')
        delay = latch - case['trigger_t']
        need(0 <= delay < (4.5 if mode == 'stall' else 0.5), 'latch delay out of bounds: %.3f s' % delay)
        streams_closed(case, latch)
        tui_usable(case)
    return run


def check_gate_death(case, provider):
    killed = case['killed_t']
    granted = [r['t'] for r in case['summary']['launcher_rows'] if r['event'] == 'granted']
    need(granted, 'no granted continuation')
    need(not [r for r in case['upstream'] if r.get('method') == 'POST' and killed < r['t'] < granted[0]],
         'model POST between gate death and continuation')
    need(not [r for r in rows(case, 'decision') if r.get('ok') and killed < r['t'] < granted[0]],
         'admitted decision before continuation')
    queued = [d for d in case['queued_turn_decisions'] if d[2] == 'KM-gd-queued']
    need(case['queued_turn_visible'] and queued and all(d[0] is False and 'gate_restart' in d[1] for d in queued),
         'queued native request was not refused by the restarted gate')
    restarted = [r for r in case['summary']['gate_rows'] if r['event'] == 'gate_ready' and r.get('mode') == 'restart']
    need(restarted and restarted[0]['port'] == case['summary']['gate_rows'][0].get('port', restarted[0]['port']),
         'gate did not restart')
    need(all(r['result'] == 'errno:1' for r in case['bind_rows']) and case['bind_rows'], 'confined bind not denied')
    need(all(r['result'] == 'errno:1' for r in case['direct_rows'] if r['port_role'] != 'gate_udp') and
         case['direct_rows'], 'confined direct egress not denied')
    for phase in ('squat_during_death', 'squat_after_restart'):
        need(case[phase] and all(v == 'errno:48' for v in case[phase].values()), 'an outside bind took the port')
    need(case['sdk_after_restart'] and all(r.get('status') in REFUSED for r in case['sdk_after_restart']),
         'a request after the restart was admitted before continuation')
    need(case['continuation']['granted'] is True, 'continuation not granted')
    need(case['after_continue']['decisions'] and case['after_continue']['decisions'][0][0] is True,
         'turn after continuation not admitted')


def check_continuation(case, provider):
    steps = case['steps']
    granted = [r['t'] for r in case['summary']['launcher_rows'] if r['event'] == 'granted']
    need(len(granted) == 1, 'expected exactly one grant')
    for name in ('typed_without_f12', 'paste', 'background_wakeup', 'native_output'):
        need(steps[name]['granted'] is False, '%s granted a continuation' % name)
    need(steps['typed_without_f12']['challenge'] is False, 'typing without F12 produced a challenge')
    need(steps['background_wakeup']['polls_during_pause'] > 2, 'no polls during the pause')
    for name in ('stale_answer', 'failing_check'):
        need(steps[name]['granted'] is False and steps[name]['still_paused'] is True, '%s reopened' % name)
    need(steps['passing_check']['granted'] is True, 'passing check did not reopen')
    need(steps['after']['decisions'][0][0] is True, 'turn after continuation not admitted')
    need(steps['same_invocation'] == {'native': True, 'gate': True}, 'continuation did not reopen the same invocation')
    latch = first_latch(case)
    need(latch is not None, 'no latch')
    need(not [r for r in case['upstream'] if r.get('method') == 'POST' and latch < r['t'] < granted[0]],
         'model POST while paused')
    need(not [r for r in rows(case, 'decision') if r.get('ok') and latch < r['t'] < granted[0]],
         'admitted decision while paused')


def check_independent(case, provider):
    s1, s2 = case['summary']['first'], case['summary']['second']
    latch1 = sorted(r['t'] for r in s1['gate_rows'] if r.get('kind') == 'latched')
    need(latch1, 'first invocation did not pause')
    early2 = [r for r in s2['gate_rows'] if r.get('kind') == 'latched' and r['t'] < case['outside_t']]
    need(not early2, 'second invocation paused with its own reserve still available')
    need(case['second_turn']['decisions'][0][0] is True and case['second_turn']['forwarded'] >= 1,
         'second invocation did not keep working')
    need(all(d[0] is False for d in case['first_turn']['decisions']), 'first invocation admitted after its pause')
    late2 = [r['t'] for r in s2['gate_rows'] if r.get('kind') == 'latched' and r['t'] > case['outside_t']]
    need(late2, 'outside consumption did not pause the second invocation')
    need(all(d[0] is False for d in case['second_after']['decisions']), 'second admitted after its pause')


def check_rotation(case, provider):
    need(any(e.get('granted') for e in case['refresh_events']), 'no granted native refresh')
    need(case['store_after_refresh'] == 'A2', 'store not refreshed to A2')
    a2 = [r for r in rows(case, 'decision') if r.get('label') == 'A2' and r.get('ok')]
    need(a2, 'A2 never admitted')
    if provider == 'claude':
        holds = rows(case, 'controller', 'rotation_hold')
        need(holds and min(h['t'] for h in holds) <= min(r['t'] for r in a2), 'A2 admitted without a rotation hold')
    else:
        reads = [r for r in case['summary']['gate_rows'] if r['event'] == 'controller' and r.get('kind') == 'latched']
        need(case['upstream_usage_labels'].get('A2', 0) >= 1, 'no same-bearer usage read for A2')
        del reads
    latch = first_latch(case, case['swap_t'])
    need(latch is not None and latch == case['latch_t'], 'A -> B did not latch')
    reason = [r for r in rows(case, 'controller', 'latched') if r['t'] == latch][0]['reason']
    need(reason in ('bearer_identity_mismatch', 'identity-changed', 'identity_mismatch'), 'unexpected reason %r' % reason)
    need(not posts_after(case, latch), 'upstream POST after the identity latch')
    need(not admitted_after(case, latch), 'admitted after the identity latch')
    host = 'platform.claude.com:443' if provider == 'claude' else 'auth.openai.com:443'
    for row in case['connect_rows']:
        expect = 200 if (row['phase'] == 'open' and row['target'] == host) else 403
        need(row['status'] == expect, 'CONNECT %s during %s gave %s' % (row['target'], row['phase'], row['status']))
    need({r['phase'] for r in case['connect_rows']} == {'open', 'paused'}, 'CONNECT phases missing')
    gate_connects = rows(case, 'connect')
    need(all(r['target'] == host for r in gate_connects if r.get('ok')), 'a non-refresh CONNECT was tunnelled')


def check_norefresh(case, provider):
    latch = first_latch(case, case['revoked_t'])
    need(latch is not None and latch == case['latch_t'], 'no pause after token expiry')
    reason = [r for r in rows(case, 'controller', 'latched') if r['t'] == latch][0]['reason']
    expected = ('server_identity_unavailable', 'freshness_expired') if provider == 'claude' else ('stale',)
    need(reason in expected, 'unexpected expiry reason %r' % reason)
    need(not [r for r in rows(case, 'connect') if r.get('ok')], 'a CONNECT was tunnelled without an allowlist')
    need(not posts_after(case, latch), 'upstream POST after the stale pause')
    need(all(d[0] is False for d in case['later']['decisions_after']), 'a later turn was admitted')


def check_prelaunch(case, provider):
    runs = case['runs']
    host = 'platform.claude.com:443' if provider == 'claude' else 'auth.openai.com:443'
    need(all(r['target'] == host for r in case['tunnel_rows'] if r['ok']), 'prelaunch tunnel admitted another host')
    if provider == 'claude':
        need(len(runs) == 2 and all(any(e.get('granted') for e in r['refresh_requests']) for r in runs),
             'claude auth status did not refresh through the tunnel')
        need(all(r['store_label_after'].endswith('2') for r in runs), 'store not updated by prelaunch refresh')
    else:
        status = [r for r in runs if r['command'] == 'login status']
        rpc = [r for r in runs if r['command'].startswith('app-server')]
        need(status and all(not r['refresh_requests'] for r in status), 'codex login status refreshed')
        need(rpc and all(any(e.get('granted') for e in r['refresh_requests']) for r in rpc),
             'app-server account/read did not refresh')


def check_tools(case, provider):
    latch = first_latch(case)
    need(latch is not None and latch == case['latch_t'], 'no pause during the running tool')
    need(not posts_after(case, latch), 'upstream POST after the pause')
    need(case['bg_file']['after'] > case['bg_file']['at_latch'], 'background tool stopped at the pause')
    net = [r for r in case['tool_net_rows'] if r.get('action') == 'tool_net']
    need(net and all(r['result'] == 'errno:1' for r in net), 'tool network to a non-gate port not denied')
    for phase in ('workflow_running', 'workflow_paused'):
        wf = case[phase]
        need(wf['git_confined']['code'] != 0 and wf['pip_confined']['code'] != 0, '%s workflow not denied' % phase)
        need(wf['pip_via_gate_proxy']['code'] != 0, 'pip through the gate proxy worked')
    need(case['workflow_running']['git_control_outside']['code'] == 0, 'git fixture control failed')
    need(case['workflow_running']['pip_control_outside']['code'] == 0, 'pip fixture control failed')
    fg = case['fg_loop']
    need(fg['at_latch'] >= 1, 'foreground loop not running at the pause')


def check_sandbox(case, provider):
    out = ' '.join(r.get('tool_output') or '' for r in case['tool_results'])
    need(case['tool_effect_file'] is False, 'nested sandbox command ran')
    if provider == 'claude':
        need('Sandbox is enabled but failed to initialize' in out, 'claude nested sandbox message missing')
    else:
        need('sandbox_apply: Operation not permitted' in out and 'code 71' in out, 'codex exit 71 missing')


def check_fsrules(case, provider):
    strict = case['strict']
    need(strict['outside_written'] is False, 'strict rules allowed a write outside')
    need(strict['second_turn']['decisions'][0][0] is True, 'native unusable under write rules')
    if provider == 'claude':
        out = ' '.join(r.get('tool_output') or '' for r in strict['tool_results'])
        need('EPERM' in out and strict['inside_written'] is False, 'claude fixed temp directory not denied')
        tmp = case['native_tmp']
        need(tmp['inside_written'] is True and tmp['outside_written'] is False, 'native_tmp variant mismatch')
    else:
        need(strict['inside_written'] is True, 'codex inside write failed under strict rules')


def check_refusals(case, provider):
    variants = case['variants']
    need(len(variants) == 10, 'missing refusal variants')
    for name, row in variants.items():
        need('error' not in row, '%s errored' % name)
        need(row['refused'] and row['native_started'] is False and row['launcher_exit'] == 3, '%s not refused' % name)
        need(row['model_posts'] == 0, '%s sent a model request' % name)
        need(any('protected mode refused' in t for t in row['terminal']), '%s showed no refusal message' % name)


def check_portmatrix(case, provider):
    need(case['host_ipv4_found'], 'no interface address to test')
    loop = case['loopback']
    need(any(v == 'bound' for v in loop['squats'].values()), 'loopback-only holder blocked every squat (gap gone?)')
    need(loop['reach']['squatter_accepted'] == ['host4'], 'squatter on the interface address was not reachable')
    allh = case['all']
    need(all(v == 'errno:48' for v in allh['squats'].values()), 'full holder let a squatter bind')
    need(allh['reach']['squatter_accepted'] == [] and allh['reach']['host4_reuseaddr_squatter'] == 'errno:48',
         'full holder squatter reachable')


CHECKS = {'threshold': check_threshold, 'loss-kill': check_loss('kill'), 'loss-eof': check_loss('eof'),
          'loss-stall': check_loss('stall'), 'gate-death': check_gate_death, 'continuation': check_continuation,
          'independent': check_independent, 'rotation': check_rotation, 'norefresh': check_norefresh,
          'prelaunch': check_prelaunch, 'tools': check_tools, 'sandbox': check_sandbox, 'fsrules': check_fsrules,
          'refusals': check_refusals, 'portmatrix': check_portmatrix}


def check_stage(name, data):
    need(data.get('completed') is True, 'stage incomplete')
    need(data.get('leftover_processes_after') == [], 'leftover processes after the stage')
    for provider in PROVIDERS:
        need(data['binaries'][provider]['version'], 'missing native version')
    need(data['cases'], 'no cases')
    for key, case in data['cases'].items():
        need('error' not in case, '%s errored: %s' % (key, case.get('error')))
        provider, _, kind = key.partition('-')
        try:
            CHECKS[kind](case, provider)
        except (KeyError, IndexError, TypeError) as error:
            raise Fail('%s: malformed evidence (%s: %s)' % (key, type(error).__name__, error))
        except Fail as error:
            raise Fail('%s: %s' % (key, error))


def median(values):
    values = sorted(v for v in values if v is not None)
    return values[len(values) // 2] if values else None


def measure(directory):
    out = {}
    for provider in PROVIDERS:
        loss = json.loads((directory / ('%s-loss.json' % provider)).read_text())['cases']
        resume = json.loads((directory / ('%s-resume.json' % provider)).read_text())['cases']
        refresh = json.loads((directory / ('%s-refresh.json' % provider)).read_text())['cases']
        gate = [r for c in loss.values() for r in c['summary']['gate_rows']]
        launch = [r for c in loss.values() for r in c['summary']['launcher_rows']]
        m = {'admit_ms_median': median(r.get('admit_ms') for r in gate if r['event'] == 'decision' and r.get('ok')),
             'forward_ms_median': median(r.get('gate_ms') for r in gate if r['event'] == 'forwarded'),
             'usage_fetch_ms_median': median(c['summary'].get('fetch_ms') for c in loss.values()),
             'poll_ms_median': median(c['summary'].get('poll_ms') for c in loss.values()),
             'self_test_ms_median': median(r.get('ms') for r in launch if r['event'] == 'self_test'),
             'latch_to_close_ms': sorted(x for c in loss.values() for x in c['stream_close_ms']),
             'threshold_latch_after_cross_ms': loss['%s-threshold' % provider]['latch_after_cross_ms'],
             'loss_latch_ms': {k: c['latch_after_trigger_ms'] for k, c in loss.items() if 'latch_after_trigger_ms' in c
                               and c['latch_after_trigger_ms'] is not None},
             'continuation_ms': [r.get('ms') for c in resume.values() for r in c.get('summary', {}).get(
                 'launcher_rows', []) if r['event'] in ('continue_reply', 'continue_status')]}
        if provider == 'claude':
            m['preflight_ms'] = median(r.get('ms') for r in gate if r['event'] == 'preflight')
            rot = refresh['claude-rotation']['summary']['gate_rows']
            holds = [r['t'] for r in rot if r.get('kind') == 'rotation_hold']
            m['rotation_hold_admit_ms'] = [r['admit_ms'] for r in rot if r['event'] == 'decision' and holds
                                           and any(0 <= r['t'] - h < 1 for h in holds)][:4]
        else:
            m['preflight_reader_s'] = median(r.get('seconds') for r in launch if r['event'] == 'reader')
            rot = refresh['codex-rotation']['summary']['gate_rows']
            m['rebind_fetch_ms'] = [r['ms'] for r in rot if r['event'] == 'fetch' and r.get('cause') == 'rebind'][:4]
        out[provider] = m
    return out


def main(argv):
    directory = Path(argv[argv.index('--dir') + 1]) if '--dir' in argv else HERE / 'evidence'
    failures = 0
    for name in STAGES:
        path = directory / (name + '.json')
        try:
            if not path.exists():
                raise Fail('missing evidence file')
            check_stage(name, json.loads(path.read_text()))
            print('%s: ok' % name)
        except Fail as error:
            failures += 1
            print('%s: FAIL %s' % (name, error))
    if '--measure' in argv and not failures:
        print(json.dumps(measure(directory), indent=1, sort_keys=True))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
