"""KLC evidence checker: per-case verdict predicates plus manifest binding.

python3 -I -B check_evidence.py [EVIDENCE_DIR]
Exit 0 only if every case file exists, matches its manifest digest, was
produced by the current code digests (the manifest must bind exactly
run.CODE + run.IMPORTED), and satisfies its case predicates.
check(name, document) is importable for the regression tests.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

HERE = Path(os.path.realpath(__file__)).parent
CASES = ['refusals', 'threshold', 'loss-kill', 'loss-eof', 'loss-stall', 'gate-death', 'continuation',
         'independent', 'rotation', 'norefresh', 'prelaunch', 'tools', 'sandbox', 'fsrules']
NATIVE = [c for c in CASES if c not in ('refusals', 'prelaunch')]
MAX_AGE = 4.0
QUOTA_REFUSALS = {'threshold': 'threshold_five_hour', 'window_missing': 'window_unavailable', 'malformed': 'quota_200',
                  'unavailable': 'quota_503', 'rate_limited': 'quota_429', 'unproved_row': 'quota_row_unproved',
                  'no_usage_account': 'server_identity_unavailable', 'stale_server_clock': 'clock_anomaly',
                  'expired_token': 'expired_native_refresh_required'}
LAUNCH_REFUSALS = {'older_native': 'native_version_unsupported', 'print': 'native_mode_unsupported',
                   'combined_print': 'native_mode_unsupported', 'bare': 'native_mode_unsupported',
                   'caller_base_url': 'caller_env_conflict', 'caller_oauth_token': 'caller_env_conflict'}


class Failure(Exception):
    pass


def need(condition, message):
    if not condition:
        raise Failure(message)


def rows(d, row, **match):
    return [r for r in d['rows'] if r.get('row') == row and all(r.get(k) == v for k, v in match.items())]


def gate(d, event, inv='main', **match):
    return [r for r in rows(d, 'event', source='gate', inv=inv, event=event)
            if all(r.get(k) == v for k, v in match.items())]


def latch(d, inv='main'):
    found = gate(d, 'controller', inv=inv, kind='latched')
    return found[0] if found else None


def native_common(d, invs=('main',)):
    launches = rows(d, 'launch')
    need(launches and all(r.get('launcher_refusal', 'absent') is None and r['gate_event'] == 'gate_ready'
                          for r in launches), 'launch did not pass the launcher checks and gate preflight')
    forwards = sum(len(gate(d, 'forwarded', inv=inv)) for inv in invs)
    arrivals = len(rows(d, 'upstream_request'))
    need(arrivals == forwards, 'upstream arrivals %d != gate forwards %d' % (arrivals, forwards))
    for inv in invs:
        clean = d['cleanup'][inv]
        need(clean.get('native_main_exit') == 0, '%s native exit %r' % (inv, clean.get('native_main_exit')))
        for item in clean.get('syscalls', {}).get('inet_connect', []):
            need(item['target'] != 'non_loopback', 'non-loopback connect %r' % item)
            if item['target'] != 'gate':
                need(item['result'] in ('EACCES', 'EPERM'), 'non-gate connect not denied %r' % item)
    exits = d['exits'] if 'exits' in d else [d['exit']]
    for item in exits:
        need(item['exited'] and item['termios_restored'], 'native exit/termios %r' % item)
    need(d['lingering_after_cleanup'] == 0, 'lingering processes')


def no_bytes_after(d, inv='main'):
    """Every upstream arrival is a gate forward, and no forward follows the latch."""
    mark = latch(d, inv)
    need(mark is not None, 'no latch')
    forwarded = gate(d, 'forwarded', inv=inv)
    need(all(f['t'] < mark['t'] for f in forwarded), 'forward after latch')
    if inv == 'main':
        need(all(u['t'] <= mark['t'] + 0.25 for u in rows(d, 'upstream_request')), 'upstream arrival after latch')
    return mark


def held_closed(d, mark, ids, within=1.0):
    closed = {r['id']: r['t'] for r in rows(d, 'stream_disconnected')}
    for ident in ids:
        need(ident in closed and 0 <= closed[ident] - mark['t'] <= within, 'held stream %s not closed' % ident)


def sdk_split(d, actor, mark):
    calls = [r for r in d['child_rows'] if r['actor'] == actor and r['attempt'] == 'model_request']
    need(any(r['status'] == 200 and r['t'] < mark['t'] for r in calls), '%s never admitted before latch' % actor)
    need(any(r['status'] == 403 and r['sent'] > mark['t'] for r in calls), '%s never refused after latch' % actor)
    need(not any(r['status'] == 200 and r['sent'] > mark['t'] for r in calls), '%s admitted after latch' % actor)
    direct = [r['result'] for r in d['child_rows'] if r['actor'] == actor and r['attempt'] == 'direct_upstream']
    need(direct and set(direct) == {'EACCES'}, '%s direct attempt not denied %r' % (actor, direct))


def c_threshold(d):
    native_common(d)
    mark = no_bytes_after(d)
    need(mark['reason'] == 'threshold_five_hour', 'reason %s' % mark['reason'])
    kinds = {r['kind'] for r in rows(d, 'stream_held') if r['id'] in d['held_ids']}
    need(kinds == {'root', 'child', 'grandchild'}, 'held kinds %r' % kinds)
    held_closed(d, mark, d['held_ids'])
    sdk_split(d, 'sdk', mark)
    sdk_split(d, 'sdk-grandchild', mark)
    need(d['native_alive_after_pause'] and d['later_refused'] and not d['later_admitted'], 'TTY/later prompt')
    history_view(d)


def history_view(d):
    view = rows(d, 'later_prompt')[0]
    need(view['history_view_alive'] and view['history_banner'], 'history view not shown')


def loss(d, reason, bound, continuation):
    native_common(d)
    mark = no_bytes_after(d)
    need(mark['reason'] == reason, 'reason %s' % mark['reason'])
    need(0 <= mark['t'] - d['trigger_t'] <= bound, 'latch delay %.3f' % (mark['t'] - d['trigger_t']))
    held_closed(d, mark, d['held_ids'])
    sdk_split(d, 'sdk', mark)
    need(d['native_alive_after_pause'] and d['later_refused'] and not d['later_admitted'], 'TTY/later prompt')
    history_view(d)
    # Monitor loss is sticky for continuation: refused before any store read or quota fetch.
    reply = d['continue_after_loss']
    need(reply.get('ok') is False and reply.get('reason') == continuation, 'continuation after loss %r' % reply)
    need(d['status_after_refused_continue']['state'] == 'paused', 'reopened after loss')
    need(d['usage_fetches_during_refused_continue'] == 0, 'quota fetched for a refused continuation')
    need(gate(d, 'controller', kind='continuation_refused', reason=continuation), 'no continuation_refused event')
    need(not gate(d, 'controller', kind='lease', cause='manual_continuation'), 'continuation lease after loss')


def c_loss_kill(d):
    loss(d, 'monitor_eof', 0.5, 'monitor_lost')


def c_loss_eof(d):
    loss(d, 'monitor_eof', 0.5, 'monitor_lost')
    need(d['status_after_refused_continue']['monitor_lost'] is True, 'monitor_lost not reported')
    need(d['upstream_proxy_tunnels'] == len(gate(d, 'forwarded')) + len(gate(d, 'aborted_before_upstream')),
         'upstream not via launcher proxy')


def c_loss_stall(d):
    loss(d, 'freshness_expired', MAX_AGE + 0.5, 'monitor_stale')


def c_gate_death(d):
    native_common(d)
    need(d['held_closed_on_gate_death'], 'held stream survived gate death')
    need(d['in_boundary_bind_with_holder'] == ['EADDRINUSE'], 'in-boundary bind with holder')
    need(d['outside_bind_with_holder'] == 'EADDRINUSE', 'outside bind with holder')
    need(d['upstream_count_while_dead'] == d['upstream_count_at_kill'] == d['upstream_count_before_continue'],
         'upstream moved while dead or restarted')
    need(d['gate_pids_differ'] and d['restart_mode'] == 'restart', 'restart')
    need(d['refused_after_restart'] >= 1, 'restart did not refuse')
    first = latch(d)
    need(first and first['reason'] == 'gate_restart', 'restart latch')
    need(d['continue_reply'].get('ok') is True, 'continuation failed')
    need(d['upstream_count_after_continue'] > d['upstream_count_before_continue'], 'no admission after continue')
    lease = gate(d, 'controller', kind='lease', cause='manual_continuation')
    need(lease and all(f['t'] > lease[0]['t'] for f in gate(d, 'forwarded') if f['t'] > first['t']),
         'forward after restart without continuation')
    # The gap without the holder: each impostor binds and receives the confined client's request.
    need(d['in_boundary_bind_without_holder'] == ['bound'], 'in-boundary impostor did not bind')
    need(any(str(line).startswith('POST /v1/messages') for line in d['in_boundary_accepted_without_holder']),
         'in-boundary impostor not reached')
    need(d['outside_bind_without_holder'] == 'bound', 'outside impostor did not bind')
    need(str(d['outside_impostor_reached_by']).startswith('POST /v1/messages'), 'outside impostor not reached')


def c_continuation(d):
    native_common(d)
    mark = latch(d)
    need(mark and mark['reason'] == 'threshold_five_hour', 'pause')
    need(d['pc5_control_sha256'] == '8b98eee39bd83b7769356db3fb49bcfefe8423da28cb4942430a9bad5e218dad', 'pc5 digest')
    for key in ('stale_typed_ignored', 'paste_ignored', 'native_output_ignored', 'native_output_on_display',
                'same_invocation', 'native_alive'):
        need(d[key] is True, key)
    need(d['background_requests_while_paused'] == [403], 'background wakeup admitted')
    for key in ('passive_status', 'status_after_failing_check', 'passive_status_after_failing_check'):
        need(d[key]['state'] == 'paused', key)
    need(d['failing_checks'] and d['failing_checks'][0]['ok'] is False
         and d['failing_checks'][0]['reason'].startswith('threshold'), 'failing check')
    need(len(d['checks']) == 2 and d['checks'][1]['ok'] is True, 'exactly one passing typed check')
    need(d['status_after_continue']['state'] == 'open', 'not reopened')
    need(d['outer_display']['still_paused'] and d['outer_display']['passed'], 'deterministic messages')
    forwarded = gate(d, 'forwarded')
    need(not any(mark['t'] < f['t'] < d['checks'][1]['t'] for f in forwarded), 'forward while paused')
    need(d['continued_upstream'] >= 1, 'continued prompt not forwarded')


def c_independent(d):
    native_common(d, ('inv1', 'inv2'))
    one, two = latch(d, 'inv1'), latch(d, 'inv2')
    need(one and one['reason'] == 'threshold_five_hour' and d['first_change_t'] < one['t'] < d['second_change_t'],
         'inv1 latch')
    need(two and two['reason'] == 'threshold_five_hour' and two['t'] > d['second_change_t'], 'inv2 latch')
    need(not d['inv2_latched_after_first_change'] and d['inv2_forwarded_after_first_change'] >= 1, 'inv2 kept working')
    need(d['inv1_forwarded_after_first_change'] == 0, 'inv1 forwarded after pause')
    for inv in ('inv1', 'inv2'):
        mark = latch(d, inv)
        need(all(f['t'] < mark['t'] for f in gate(d, 'forwarded', inv=inv)), '%s forward after latch' % inv)


def c_rotation(d):
    native_common(d)
    mark = no_bytes_after(d)
    need(mark['reason'] == 'bearer_identity_mismatch' and mark['t'] > d['swap_t'], 'switch latch')
    refresh = rows(d, 'refresh_request')
    need(refresh and refresh[0]['expected_refresh'], 'native refresh through tunnel')
    need(d['store_refreshed_to_A2'] and d['store_inode_replaced'], 'store rotation')
    need(d['rotation_holds'] and d['rotation_holds'][0]['ok'], 'same-account rotation not admitted')
    upstream = rows(d, 'upstream_request')
    need('B' not in {u['label'] for u in upstream}, 'B bytes upstream')
    usage = d['usage_requests']
    for item in upstream:
        need(any(u['account'] == item['label'] and u['t'] <= item['t'] for u in usage),
             'bearer %s forwarded before its own quota fetch' % item['label'])
    need(any(u['account'] == 'B' and u['t'] > d['swap_t'] for u in usage), 'B not checked with B')
    need(['platform.claude.com:443', 200] in d['connects_while_open'], 'refresh CONNECT while open')
    need(all(status == 403 for target, status in d['connects_while_open'] if target != 'platform.claude.com:443'),
         'non-exact CONNECT allowed')
    need(all(status == 403 for _, status in d['connects_while_paused']), 'CONNECT while paused')
    need(d['later_refused'], 'later prompt')


def c_norefresh(d):
    native_common(d)
    mark = no_bytes_after(d)
    need(0 <= mark['t'] - d['revoked_t'] <= MAX_AGE + 1.5, 'stale pause delay')
    need(mark['reason'] in ('server_identity_unavailable', 'freshness_expired', 'quota_401'), 'reason %s' % mark['reason'])
    need(not d['summary']['connects'], 'proxy path present')
    need(d['native_requests_after_typed'] and all(not r['ok'] and r['bearer'] == 'A'
                                                  for r in d['native_requests_after_typed']),
         'native admitted after expiry, or not with the expired bearer')
    need(d['native_bearers_after_expiry'] == ['A'] and d['store_label_end'] == 'A', 'bearer after expiry')
    denied = d['cleanup']['main']['syscalls']['socket_denied']
    need(any(k.endswith('AF_INET/SOCK_DGRAM/IPPROTO_IP') and k.startswith('2.1.322') for k in denied),
         'no denied native resolution attempt recorded')


def c_prelaunch(d):
    need(len(d['runs']) == 2, 'runs')
    for run in d['runs']:
        need(run['exit'] == 0 and run['refresh_requests'] == 1 and run['store_label_after'] == 'A2', run['variant'])
    need(all(ok is False for target, ok in d['tunnel_targets'] if target != 'platform.claude.com:443'), 'tunnel')
    need(d['lingering_after_cleanup'] == 0, 'lingering')


def c_tools(d):
    native_common(d)
    mark = no_bytes_after(d)
    need(d['fg_ticks_2s_after'] > d['fg_ticks_at_latch'] and d['bg_ticks_2s_after'] > d['bg_ticks_at_latch'],
         'local work stopped at pause')
    need(d['fg_shell_alive_before_interrupt'] and not d['fg_shell_alive_after_interrupt']
         and not d['fg_finished_marker'] and d['native_alive_after_interrupt'], 'Ctrl-C did not end only the shell')
    need(d['bg_ticks_growing_after_interrupt'], 'background stopped by interrupt')
    ticks = [r for r in d['child_rows'] if r['actor'] == 'ticks' and r['attempt'] == 'tick_network']
    need(any(r['gate_status'] == 200 and r['t'] < mark['t'] for r in ticks), 'test-like gate use before pause')
    need(all(r['gate_status'] != 200 for r in ticks if r['t'] > mark['t']), 'test-like admitted after pause')
    need(all(r['direct'] == 'EACCES' for r in ticks), 'test-like direct egress')
    control = {r['attempt']: r for r in d['workflows_control']}
    need(control['git_clone']['exit'] == 0 and control['index_download']['result'] == 'ok', 'fixture control')
    need(control['https_via_proxy'].get('skipped'), 'outside control made an HTTPS (resolver) call')
    for phase in ('workflows-run', 'workflows-pause'):
        got = {r['attempt']: r for r in d['child_rows'] if r['actor'] == phase}
        need(got['git_clone']['exit'] != 0 and got['index_download']['result'] == 'EACCES', phase)
        need(got['https_via_proxy']['exit'] == 56 and '403' in got['https_via_proxy']['stderr'], phase + ' proxy')


def c_refusals(d):
    need(set(d['quota_startup']) == set(QUOTA_REFUSALS), 'quota variants %r' % sorted(d['quota_startup']))
    for name, item in d['quota_startup'].items():
        need(item['event'] == 'startup_refused' and item['gate_exit'] == 3 and not item['native_started'], name)
        need(item['reason'] == QUOTA_REFUSALS[name], '%s reason %r' % (name, item['reason']))
    need(set(d['launch_path']) == set(LAUNCH_REFUSALS), 'launch variants %r' % sorted(d['launch_path']))
    for name, item in d['launch_path'].items():
        need(item['launched'] is False and item['reason'] == LAUNCH_REFUSALS[name] and not item['gate_started']
             and not item['native_started'], 'launch path %s %r' % (name, item))
    need(d['upstream_requests'] == 0, 'model request during refusals')
    need(d['host_refusal'] is None, 'host refused: %r' % d['host_refusal'])
    need(d['version']['real'] is None and d['version']['fake_older'] == 'native_version_unsupported', 'version')
    need(d['mode'] == {'print': 'native_mode_unsupported', 'bare': 'native_mode_unsupported', 'interactive': None},
         'mode %r' % d['mode'])
    need(d['env'] == {'oauth_token_env': 'caller_env_conflict', 'base_url_env': 'caller_env_conflict'},
         'env %r' % d['env'])
    need(d['token_window'] == {'near_no_tunnel': 'native_refresh_window', 'near_with_tunnel': None}, 'token window')
    need(d['boundary_socket_stdin']['exit'] == 2 and d['boundary_socket_stdin']['launcher_refusal'] == 'stdio_socket',
         'socket stdio')


def c_sandbox(d):
    native_common(d)
    text = ' '.join(d['tool_result'])
    need('failed to initialize' in text and 'EAFNOSUPPORT' in text, 'native sandbox outcome %r' % text[:80])
    need(d['file_written'] is False, 'sandboxed command ran')
    need('bwrap' in d['cleanup']['main']['traced_executables'], 'bwrap not attempted')


def c_fsrules(d):
    native_common(d)
    need(d['fsrules_applied'] and d['inside_written'] and not d['outside_written'], 'fs rule')
    need('permission denied' in ' '.join(d['tool_result']), 'outside write error not shown')


CHECKS = {'threshold': c_threshold, 'loss-kill': c_loss_kill, 'loss-eof': c_loss_eof, 'loss-stall': c_loss_stall,
          'gate-death': c_gate_death, 'continuation': c_continuation, 'independent': c_independent,
          'rotation': c_rotation, 'norefresh': c_norefresh, 'prelaunch': c_prelaunch, 'tools': c_tools,
          'refusals': c_refusals, 'sandbox': c_sandbox, 'fsrules': c_fsrules}


def check(name, document):
    need(document.get('case') == name and document.get('launched') is True, 'case not launched')
    need(document.get('sanitized') is True, 'not sanitized')
    if name in NATIVE:
        need(document.get('native_sha256'), 'native digest')
    CHECKS[name](document)


def bound_code():
    """The code list run.py binds; the manifest must name exactly these files."""
    spec = importlib.util.spec_from_file_location('klc_run_code', HERE / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.CODE + module.IMPORTED)


def main(argv):
    directory = Path(argv[0]) if argv else HERE / 'evidence'
    manifest = json.loads((directory / 'manifest.json').read_text())
    probes = HERE.parent
    failures = []
    if set(manifest['code_sha256']) != bound_code():
        failures.append('manifest code list differs from run.CODE + run.IMPORTED')
    for name, digest in manifest['code_sha256'].items():
        if hashlib.sha256((probes / name).read_bytes()).hexdigest() != digest:
            failures.append('code changed since capture: ' + name)
    for name in CASES:
        path = directory / (name + '.json')
        try:
            raw = path.read_bytes()
            need(hashlib.sha256(raw).hexdigest() == manifest['evidence_sha256'].get(path.name), 'digest mismatch')
            need(manifest['stages'][name]['exit'] == 0, 'stage exit')
            check(name, json.loads(raw))
        except (Failure, OSError, KeyError, ValueError, TypeError, IndexError) as error:
            failures.append('%s: %s' % (name, error))
    need_clean = manifest.get('leftover_temp_roots') == 0 and manifest.get('leftover_processes') == 0
    if not need_clean:
        failures.append('runner left temporary roots or processes')
    for line in failures:
        print('FAIL', line)
    if not failures:
        print('OK %d cases' % len(CASES))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
