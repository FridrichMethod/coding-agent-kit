"""Offline tests for the KLX outer filesystem write rules (fsrules.py).

python3 -I -B test_fsrules.py
The process test needs Landlock and skips cleanly without it.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('klx_test_fsrules', HERE / 'fsrules.py')
fsrules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fsrules)


class Pure(unittest.TestCase):
    def test_write_mask_follows_the_abi(self):
        self.assertFalse(fsrules.write_mask(1) & (fsrules.REFER | fsrules.TRUNCATE))
        self.assertTrue(fsrules.write_mask(2) & fsrules.REFER)
        self.assertTrue(fsrules.write_mask(3) & fsrules.TRUNCATE)
        for abi in (1, 3, 8):  # reads, execution and device ioctl are never handled
            self.assertFalse(fsrules.write_mask(abi) & ((1 << 0) | (1 << 2) | (1 << 3) | (1 << 15)))
        with self.assertRaises(fsrules.Refusal):
            fsrules.write_mask(0)

    def test_plan_rights_by_kind_and_missing_paths(self):
        mask, rules = fsrules.plan(8, ['/w', '/dev/null'], {'/w': 'dir', '/dev/null': 'file'})
        self.assertEqual(dict(rules), {'/w': mask, '/dev/null': fsrules.WRITE_FILE | fsrules.TRUNCATE})
        with self.assertRaises(fsrules.Refusal):
            fsrules.plan(8, ['/gone'], {})

    def test_usage(self):
        for argv in ([], ['--write'], ['--write', '/w'], ['--bogus', '--', 'true'], ['--']):
            with self.assertRaises(fsrules.Refusal, msg=argv):
                fsrules.parse(argv)
        self.assertEqual(fsrules.parse(['--write', '/w', '--', 'true']), (['/w'], False, ['true']))


class Process(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='quota-klx-test-fs-'))
        (self.root / 'ok').mkdir()
        (self.root / 'no').mkdir()

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_inside(self, script, *writable):
        argv = [sys.executable, '-I', '-B', str(HERE / 'fsrules.py')]
        for path in writable:
            argv += ['--write', str(path)]
        return subprocess.run([*argv, '--', '/bin/sh', '-c', script], capture_output=True, text=True, timeout=20,
                              stdin=subprocess.DEVNULL)

    def test_writes_only_beneath_named_paths(self):
        probe = subprocess.run([sys.executable, '-I', '-B', str(HERE / 'fsrules.py'), '--', 'true'],
                               capture_output=True, text=True, timeout=20)
        if probe.returncode == 2 and 'Landlock unavailable' in probe.stderr:
            self.skipTest('Landlock unavailable on this host')
        ok, no = self.root / 'ok', self.root / 'no'
        result = self.run_inside(
            'echo a > %s/f && mkdir %s/d && mv %s/f %s/d/f && echo inside-ok; '
            'echo b > %s/f 2>/dev/null || echo outside-denied; '
            'ln -s x %s/l 2>/dev/null || echo symlink-denied; '
            'cat %s/d/f; echo c > /dev/null && echo devnull-ok' % (ok, ok, ok, ok, no, no, ok), ok, '/dev/null')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ['inside-ok', 'outside-denied', 'symlink-denied', 'a', 'devnull-ok'])
        self.assertFalse(os.path.exists(no / 'f'))
        self.assertEqual(self.run_inside('true', self.root / 'missing').returncode, 2)


if __name__ == '__main__':
    unittest.main()
