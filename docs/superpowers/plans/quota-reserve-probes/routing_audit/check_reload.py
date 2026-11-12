"""Offline acceptance of the two reload captures; never launches native code."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def check(data):
    assert data['exit'] == 0
    assert [(row['mode'], row['exit']) for row in data['stages']] == [
        ('guard', 0), ('environment', 0), ('pinned', 0)]
    for row in data['sources']:
        assert hashlib.sha256((HERE.parent / row['file']).read_bytes()).hexdigest() == row['sha256']
    for mode, expected in [('environment', 'alternate'), ('pinned', 'gate')]:
        rows = data['cases'][mode]
        first = [row for row in rows if row.get('marker') == 'first']
        second = [row for row in rows if row.get('marker') == 'second']
        change, = [row for row in rows if row['event'] == 'user_settings_changed']
        assert first and second
        assert all(row['route'] == 'gate' and row['at'] < change['at'] for row in first)
        assert all(row['route'] == expected and row['at'] > change['at'] for row in second)
        assert all(row['fixture_bearer'] for row in first + second)
        passed, = [row for row in rows if row['event'] == 'passed']
        assert passed['native_alive'] and passed['pinned'] == (mode == 'pinned')
        ended, = [row for row in rows if row['event'] == 'native_exit']
        assert ended['code'] == 0 and ended['termios_restored']


if __name__ == '__main__':
    check(json.loads((HERE / 'reload-evidence.json').read_text()))
    print('PASS: two native reload captures and source hashes')
