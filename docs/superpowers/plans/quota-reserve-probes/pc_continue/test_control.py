"""Input provenance checks using an owned terminal, not origin labels."""
import importlib.util
import os
from pathlib import Path
import pty
import termios
import tty
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('continuation', HERE / 'control.py')
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class InputBoundary(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        self.original = termios.tcgetattr(self.slave)
        tty.setraw(self.slave)
        self.checks, self.grants, self.forwarded, self.messages = [], [], [], []
        self.allowed = True
        def check():
            self.checks.append('fresh check')
            return self.allowed
        self.ctl = control.TerminalControl(self.slave, self.forwarded.append,
            self.messages.append, check, lambda: self.grants.append('lease'))

    def tearDown(self):
        termios.tcsetattr(self.slave, termios.TCSANOW, self.original)
        os.close(self.master)
        os.close(self.slave)

    def send(self, data):
        os.write(self.master, data)
        self.ctl.pump(.05)

    def confirm(self):
        self.send(control.KEY)
        self.send(self.ctl.challenge.encode() + b'\r')

    def test_prebuffered_key_and_repeated_key_do_not_check(self):
        os.write(self.master, control.KEY)
        self.ctl.pause()
        self.ctl.pump(.05)
        self.assertIsNone(self.ctl.challenge)
        self.send(control.KEY * 8)
        self.assertFalse(self.checks)
        self.assertFalse(self.grants)

    def test_split_paste_never_confirms(self):
        self.ctl.pause()
        self.send(control.KEY)
        value = self.ctl.challenge.encode()
        for part in (b'\x1b[20', b'0~', value + b'\r', b'\x1b[201~'):
            self.send(part)
        self.assertFalse(self.checks)
        self.assertFalse(self.grants)

    def test_fresh_action_checks_once_and_later_replay_does_not_grant(self):
        self.ctl.pause()
        self.send(control.KEY)
        old = self.ctl.challenge.encode() + b'\r'
        self.send(old)
        self.assertEqual(len(self.checks), 1)
        self.assertEqual(len(self.grants), 1)
        self.ctl.pause()
        self.send(old)
        self.assertEqual(len(self.grants), 1)

    def test_failed_check_requires_another_action(self):
        self.allowed = False
        self.ctl.pause()
        self.confirm()
        self.assertEqual(len(self.checks), 1)
        self.assertFalse(self.grants)
        self.allowed = True
        self.ctl.pump(.01)
        self.assertFalse(self.grants)
        self.confirm()
        self.assertEqual(len(self.checks), 2)
        self.assertEqual(len(self.grants), 1)

    def test_ordinary_keys_forward_and_native_output_has_no_input_path(self):
        self.send(b'hello\x1b[A\x03')
        self.assertEqual(b''.join(self.forwarded), b'hello\x1b[A\x03')
        self.assertFalse(self.checks)

    def test_native_output_and_terminal_reports_cannot_confirm(self):
        self.ctl.pause()
        self.send(control.KEY)
        os.write(self.slave, control.KEY + self.ctl.challenge.encode() + b'\r')
        self.ctl.pump(.01)
        self.assertFalse(self.checks)
        self.send(b'\x1b[1;1R\x1b[?1;2c')
        self.assertFalse(self.checks)
        self.assertFalse(self.grants)

    def test_single_escape_forwards_after_one_idle_pump(self):
        self.send(b'\x1b')
        self.ctl.pump(.06)
        self.assertEqual(b''.join(self.forwarded), b'\x1b')

    def test_split_key_and_held_repeat_do_not_replace_challenge(self):
        self.ctl.pause()
        self.send(control.KEY[:2])
        self.send(control.KEY[2:])
        challenge = self.ctl.challenge
        self.assertIsNotNone(challenge)
        self.send(control.KEY * 5)
        self.assertEqual(self.ctl.challenge, challenge)
        self.assertFalse(self.checks)

    def test_pause_in_middle_of_paste_cannot_reinterpret_tail_as_control(self):
        self.send(b'\x1b[200~')
        self.ctl.pause()
        self.send(control.KEY)
        self.assertIsNone(self.ctl.challenge)
        self.send(b'\x1b[201~')
        self.send(control.KEY)
        self.assertIsNotNone(self.ctl.challenge)


if __name__ == '__main__':
    unittest.main()
