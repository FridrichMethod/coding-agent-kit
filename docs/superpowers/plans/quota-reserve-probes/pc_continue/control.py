"""Shared RESEARCH terminal control; not a launcher or physical-human attestor.

The owner alone reads a trusted terminal fd. Native output is never fed here.
F12 is an experimental key, not a public shortcut. Its fresh one-use challenge
prevents old buffered input, held F12 and bracketed paste from granting a lease.
The embedding loop must serialize pause/pump, bound check(), and own grant().
"""
import os
import secrets
import select
import termios
import time

KEY = b'\x1b[24~'
PASTE_START = b'\x1b[200~'
PASTE_END = b'\x1b[201~'


class TerminalControl:
    def __init__(self, input_fd, forward, display, check, grant):
        self.input_fd = input_fd
        self.forward = forward
        self.display = display
        self.check = check
        self.grant = grant
        self.paused = False
        self.challenge = None
        self.answer = bytearray()
        self.pending = bytearray()
        self.pasting = False
        self.epoch = 0
        self.pending_since = 0.0

    def pause(self):
        self.paused = True
        self.epoch += 1
        self.challenge = None
        self.answer.clear()
        self.pending.clear()
        termios.tcflush(self.input_fd, termios.TCIFLUSH)
        self.display('Paused. Research F12 control requires a new typed confirmation.')

    def pump(self, timeout=.05):
        if not select.select([self.input_fd], [], [], timeout)[0]:
            if self.pending and time.monotonic() - self.pending_since >= .05:
                while self.pending:
                    self._ordinary(self.pending.pop(0))
            return
        data = os.read(self.input_fd, 4096)
        if not data:
            raise EOFError('terminal input closed')
        for byte in data:
            if not self.pending:
                self.pending_since = time.monotonic()
            self.pending.append(byte)
            while self.pending:
                value = bytes(self.pending)
                if value in (KEY, PASTE_START, PASTE_END):
                    self.pending.clear()
                    self._sequence(value)
                    break
                if any(seq.startswith(value) for seq in (KEY, PASTE_START, PASTE_END)):
                    break
                char = self.pending.pop(0)
                self._ordinary(char)

    def _sequence(self, value):
        if value == PASTE_START:
            self.pasting = True
            self.challenge = None
            self.answer.clear()
            self.forward(value)
        elif value == PASTE_END:
            self.pasting = False
            self.forward(value)
        elif self.pasting:
            self.forward(value)
        elif self.paused:
            if self.challenge is None:
                self.challenge = secrets.token_hex(4)
                self.answer.clear()
                self.display('Type ' + self.challenge + ' then Enter to request a fresh quota check.')
        else:
            self.forward(value)

    def _ordinary(self, char):
        if self.pasting or self.challenge is None:
            self.forward(bytes([char]))
            return
        if char in (10, 13):
            accepted = bytes(self.answer) == self.challenge.encode('ascii')
            self.challenge = None
            self.answer.clear()
            if accepted and self.check():
                self.grant()
                self.paused = False
                self.display('Fresh quota check passed; one healthy lease granted.')
            else:
                self.display('Still paused; request a new confirmation to retry.')
        elif char in (8, 127):
            self.answer = self.answer[:-1]
        elif char == 3:
            self.challenge = None
            self.answer.clear()
            self.forward(bytes([char]))
        elif 32 <= char < 127 and len(self.answer) < 64:
            self.answer.append(char)
        else:
            self.challenge = None
            self.answer.clear()
