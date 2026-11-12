"""Summarize the G0 measurements from retained KLC evidence (loopback only).

python3 -I -B measure.py [EVIDENCE_DIR]   -> JSON on stdout
Values are milliseconds on one host against loopback fixtures; they bound the
composition's own overhead, not live provider latency.
"""
import json
import os
from pathlib import Path
import statistics
import sys

HERE = Path(os.path.realpath(__file__)).parent


def stats(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {'n': len(values), 'min': round(values[0], 3), 'median': round(statistics.median(values), 3),
            'max': round(values[-1], 3)}


def main(argv):
    directory = Path(argv[0]) if argv else HERE / 'evidence'
    docs = {p.stem: json.loads(p.read_text()) for p in directory.glob('*.json') if p.name != 'manifest.json'}
    gate = lambda d: [r for r in d.get('rows', []) if r.get('row') == 'event' and r.get('source') == 'gate']
    out = {'preflight_ms': [], 'spawn_to_ready_ms': [], 'usage_get_ms': [], 'profile_get_ms': [], 'poll_ms': [],
           'admit_ms': [], 'gate_forward_ms': [], 'rotation_hold_ms': [], 'latch_to_upstream_close_ms': [],
           'trigger_to_latch_ms': {}}
    for name, d in docs.items():
        rows = gate(d)
        out['preflight_ms'] += [r['ms'] for r in rows if r.get('event') == 'preflight']
        out['spawn_to_ready_ms'] += [r['spawn_to_ready_ms'] for r in d.get('rows', [])
                                     if r.get('event') == 'gate_started' and r.get('mode') == 'startup']
        out['usage_get_ms'] += [r['ms'] for r in rows if r.get('event') == 'fetch' and r.get('what') == 'usage']
        out['profile_get_ms'] += [r['ms'] for r in rows if r.get('event') == 'fetch' and r.get('what') == 'profile']
        out['poll_ms'] += [r['ms'] for r in rows if r.get('event') == 'poll' and r.get('ok')]
        holds = [r['t'] for r in rows if r.get('kind') == 'rotation_hold']
        decisions = [r for r in rows if r.get('event') == 'decision']
        for decision in decisions:
            if decision['ok'] and not any(0 <= decision['t'] - h <= decision['admit_ms'] / 1000 + 0.01 for h in holds):
                out['admit_ms'].append(decision['admit_ms'])
        out['gate_forward_ms'] += [r['gate_ms'] for r in rows if r.get('event') == 'forwarded']
        for hold in d.get('rotation_holds', []):
            out['rotation_hold_ms'].append(hold['admit_ms'])
        latches = [r for r in rows if r.get('kind') == 'latched' and r.get('inv') == 'main']
        if d.get('killed_t') and d.get('held_ids'):
            out.setdefault('gate_death_to_upstream_close_ms', [])
            out['gate_death_to_upstream_close_ms'] += [(r['t'] - d['killed_t']) * 1000 for r in d['rows']
                                                       if r.get('row') == 'stream_disconnected'
                                                       and r['id'] in d['held_ids']]
        elif latches and d.get('held_ids'):
            mark = latches[0]['t']
            for row in d['rows']:
                if row.get('row') == 'stream_disconnected' and row['id'] in d['held_ids']:
                    out['latch_to_upstream_close_ms'].append((row['t'] - mark) * 1000)
            if d.get('trigger_t'):
                out['trigger_to_latch_ms'][name] = round((mark - d['trigger_t']) * 1000, 1)
    result = {k: (stats(v) if isinstance(v, list) else v) for k, v in out.items()}
    print(json.dumps(result, indent=1, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
