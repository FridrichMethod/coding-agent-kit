"""Focused regressions for research supervisor failure and timeout ownership."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load_runner():
    spec = importlib.util.spec_from_file_location('pc_runner_test', HERE / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Supervision(unittest.TestCase):
    def test_preserved_child_failure_is_nonzero_after_artifact_and_inventory(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory(prefix='pc-supervision-test-') as temp:
            failed = subprocess.CompletedProcess([], 7, ('scratch ' + temp + '\n').encode(), b'fixture failure')
            with patch.object(sys, 'argv', ['reproduce.py', sys.executable]), \
                    patch('subprocess.run', return_value=failed), \
                    patch.object(Path, 'write_text') as writes, redirect_stdout(output):
                with self.assertRaises(SystemExit) as exit_status:
                    runpy.run_path(str(HERE / 'reproduce.py'), run_name='__main__')
            self.assertNotEqual(exit_status.exception.code, 0)
            artifacts = [json.loads(call.args[0]) for call in writes.call_args_list]
            self.assertTrue(any(row.get('supervisor_exit') == 7 for row in artifacts))
            self.assertIn('TEMP_ROOTS=', output.getvalue())
            self.assertIn(temp, output.getvalue())

    def test_every_bounded_native_stage_directly_owns_a_tracer(self):
        runner = load_runner()
        commands = []
        def fake(command, **kwargs):
            commands.append(command)
            self.assertEqual(Path(command[3]).name, 'network_guard.py', command)
            if kwargs.get('text'):
                out = 'codex-cli 0.167.2\n' if '--version' in command else ''
                return subprocess.CompletedProcess(command, 0, out, '')
            return subprocess.CompletedProcess(command, 0, b'', b'')
        with tempfile.TemporaryDirectory(prefix='pc-supervision-test-') as temp:
            with patch.object(sys, 'argv', ['run.py', sys.executable]), \
                    patch('subprocess.run', side_effect=fake), \
                    patch('tempfile.mkdtemp', return_value=temp), \
                    patch.object(Path, 'write_text'), redirect_stdout(io.StringIO()):
                self.assertEqual(runner.main(), 0)
        self.assertGreaterEqual(len(commands), 7)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux ptrace guard')
    def test_killed_tracer_cancels_owned_new_session_descendants(self):
        child_code = ('import os,subprocess,sys,time; '
                      'p=subprocess.Popen([sys.executable,"-I","-B","-c",'
                      '"import time; time.sleep(60)"],start_new_session=True); '
                      'print(os.getpid(),p.pid,flush=True); time.sleep(60)')
        process = subprocess.Popen([sys.executable, '-I', '-B', str(HERE / 'network_guard.py'),
                                    sys.executable, '-I', '-B', '-c', child_code],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        owned = []
        try:
            with self.assertRaises(subprocess.TimeoutExpired) as expired:
                process.communicate(timeout=1)
            owned = [int(word) for word in (expired.exception.output or b'').split()]
            self.assertEqual(len(owned), 2, 'tracees must be running before timeout')
            process.kill()
            process.communicate(timeout=3)
            def active(pid):
                try:
                    return Path('/proc', str(pid), 'stat').read_text().split(') ', 1)[1][0] != 'Z'
                except (FileNotFoundError, ProcessLookupError):
                    return False
            end = time.monotonic() + 2
            while any(active(pid) for pid in owned) and time.monotonic() < end:
                time.sleep(.02)
            self.assertFalse([pid for pid in owned if active(pid)])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
            for pid in owned:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
