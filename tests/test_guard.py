"""The usage reserve guard: its hook events and its status line.

Most cases run in process through guard.answer() against launch state and usage
data in a temporary 0700 directory. The command cases run guard.py as the hook
commands the launchers inject, also from a copy of lib/ under a directory whose
name has a space and a quote. No native, provider, credential or network is
involved.
"""

import ast
import contextlib
from decimal import Decimal
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load("kit_guard_under_test", "lib/coding-agent-kit/guard.py")
guard_launch = load("kit_guard_launch_under_test", "lib/coding-agent-kit/guard_launch.py")
reserve = load("kit_reserve_for_guard", "lib/coding-agent-kit/reserve.py")

D = Decimal
NOW = 1_800_000_000  # 2027-01-15T08:00:00Z
STARTED = NOW - 600
OK = (0, "", "")
# The Codex hook commands, as codex-as injects them.
CODEX_COMMAND = '"$CODING_AGENT_KIT_GUARD_PYTHON" -I -B -S "$CODING_AGENT_KIT_GUARD_SCRIPT" '
WEEKLY_CLAUDE = ("claude-as: personal: weekly window 9% remaining, at or below its 10% reserve; "
                 "resets in 1h 0m (2027-01-15T09:00:00Z).\n")
WEEKLY_CODEX = ("codex-as: lab: weekly window 5% remaining, at or below its 10% reserve; "
                "resets in 1h 0m (2027-01-15T09:00:00Z).\n")
RELAUNCH_CLAUDE = "To use the reserve, exit and run:\n  claude-as --reserve-weekly off personal --continue"
RELAUNCH_CODEX = "To use the reserve, exit and run:\n  codex-as --reserve-weekly off lab resume --last"
STOPPED = {"prompt": "This prompt was not sent.\n",
           "tool": "This tool call was stopped. Stop working now and tell the user why.\n",
           "stop": "The work stops here.\n"}
TAG = "5f0c2a9e41d7b386"  # a login's owner tag, as the launcher writes it into STATE.turns
OTHER_TAG = "0123456789abcdef"  # another login's
THREAD, THREAD2, SUB = (f"019a0000-0000-7000-8000-00000000000{letter}" for letter in "abc")
# Turn ids are UUIDs that native Codex makes per turn; in the cases T0 and T2 are another client's.
T0, T1, T2, T3, S1 = (f"01a20000-0000-7000-8000-00000000000{digit}" for digit in "01239")


def rel(thread):
    """The path of THREAD's rollout below sessions/, as the guard notes it."""
    return f"2026/01/01/rollout-2026-01-01T00-00-00-{thread}.jsonl"


REL, REL2 = rel(THREAD), rel(THREAD2)


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def put(path, data, mode=0o600):
    """A file holding DATA (JSON unless it is text) with MODE."""
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    path.chmod(mode)
    return path


def turn_json(text):
    return json.dumps({"continue": False, "stopReason": text,
                       "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": text}})


def stop_json(text):
    return json.dumps({"continue": False, "stopReason": text})


# -- Codex rollouts -------------------------------------------------------------

def line(when, kind, payload):
    return {"timestamp": iso(when), "type": kind, "payload": payload}


def slot(used, minutes, resets=NOW + 3600):
    return {"used_percent": used, "window_minutes": minutes, "resets_at": resets}


def count(when, primary=None, secondary=None, limit_id="codex", rates=True):
    """A token_count event; RATES False writes it without rate_limits."""
    payload = {"type": "token_count", "info": {"total_token_usage": {}}}
    if rates:
        payload["rate_limits"] = {"limit_id": limit_id, "limit_name": None, "primary": primary,
                                  "secondary": secondary, "credits": None, "plan_type": None}
    return line(when, "event_msg", payload)


def reply(when):
    return line(when, "response_item", {"type": "message", "role": "assistant", "content": []})


def shell_call(when):
    return line(when, "response_item", {"type": "function_call", "name": "exec_command", "arguments": "{}",
                                         "call_id": "call_1"})


# The lines that name a turn, in the shapes codex-cli 0.171.0 writes.

def started(when, turn):
    """A turn's task_started line, which also names it inside turn_attribution."""
    return line(when, "event_msg", {"type": "task_started", "turn_id": turn, "turn_attribution": {"turn_id": turn}})


def context(when, turn):
    return line(when, "turn_context", {"turn_id": turn, "cwd": "/"})


def usage_record(when, turn):
    """The token_usage_record line of a model response."""
    return line(when, "token_usage_record", {"turn_id": turn, "input_tokens": 1})


def ended(when, turn, kind="task_complete"):
    return line(when, "event_msg", {"type": kind, "turn_id": turn})


def settings(when):
    """The line a client writes as it takes a thread over, before its first turn there."""
    return line(when, "event_msg", {"type": "thread_settings_applied", "thread_id": THREAD})


def completed(when, turn):
    """An item_completed event: it carries its turn's id, but can follow the turn's end, so it names no turn."""
    return line(when, "event_msg", {"type": "item_completed", "turn_id": turn})


def output(when, size):
    """A tool call's output of SIZE bytes, written as one rollout line."""
    return line(when, "response_item", {"type": "function_call_output", "call_id": "call_1", "output": "A" * size})


def turn(turn_id, when, reading=None, rates=True, end=True, limit_id="codex"):
    """One turn from its start at WHEN: with READING (a primary slot), or RATES False, a reply 10 seconds later,
    its token_usage_record and a token_count a second after that with READING as its primary window (none
    without RATES); with END its task_complete."""
    lines = [started(when, turn_id), context(when, turn_id)]
    if reading is not None or not rates:
        lines += [reply(when + 10), usage_record(when + 10, turn_id),
                  count(when + 11, reading, rates=rates, limit_id=limit_id)]
    if end:
        lines.append(ended(when + 12, turn_id))
    return lines


class Fixture(unittest.TestCase):
    """A guard directory and an accounts directory, both owned and 0700, in a temporary root. The caller's
    environment, which in-process status line runs pass on, gets a temporary HOME and loses the kit's own
    variables and the XDG directories."""

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o077))
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-guard-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        home = self.root / "home"
        home.mkdir(mode=0o700)
        patcher = patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in [name for name in os.environ if name.startswith(("CODING_AGENT_KIT_", "XDG_")) or name == "NO_COLOR"]:
            del os.environ[name]
        self.accounts = self.root / "state o'clock" / "accounts"
        self.guard_dir = self.accounts.parent / "guard"
        for folder in (self.accounts.parent, self.accounts, self.guard_dir):
            folder.mkdir(mode=0o700)
        self.launches = 0

    def launch(self, tool="claude-as", account=None, five_hour=None, weekly=None, statusline=None, started=STARTED):
        """A Launch of TOOL with the reserves given as percent strings; each call gets its own state prefix."""
        account = account or ("personal" if tool == "claude-as" else "lab")
        self.launches += 1
        enabled = {window: D(value) for window, value in (("five_hour", five_hour), ("weekly", weekly))
                   if value is not None}
        if tool == "claude-as":
            usage, marker = self.accounts / f"claude-{account}.usage.json", None
        else:
            usage, marker = self.accounts / f"codex-{account}.json", str(self.guard_dir / "codex-hooks-90aeb3ec484a2ca6")
        state = self.guard_dir / f"{tool}-{account}-{4000 + self.launches}-{started}"
        return guard.Launch(tool, account, enabled, started, str(usage), str(state), marker, statusline)

    def environ(self, launch, **changes):
        """The CODING_AGENT_KIT_GUARD value of LAUNCH, as the launcher writes it, with CHANGES."""
        data = {"v": 1, "tool": launch.tool, "account": launch.account, "five_hour": None, "weekly": None,
                "started": launch.started, "usage": launch.usage, "state": launch.state}
        for window, value in launch.enabled.items():
            data[window] = reserve.percent(value)
        if launch.marker is not None:
            data["marker"] = launch.marker
        if launch.statusline is not None:
            data["statusline"] = launch.statusline
        data.update(changes)
        return {"CODING_AGENT_KIT_GUARD": json.dumps(data, separators=(",", ":"))}

    def state(self, launch, name):
        return Path(launch.state + "." + name)

    def claude_usage(self, launch, five_hour=None, seven_day=None, mode=0o600):
        """The account's usage file as the status line writes it; a window is (used, reset) or None."""
        data = {key: None if value is None else {"used_percentage": value[0], "resets_at": value[1]}
                for key, value in (("five_hour", five_hour), ("seven_day", seven_day))}
        return put(Path(launch.usage), {**data, "seen_at": NOW - 100}, mode)

    def codex_record(self, launch, primary=None, secondary=None):
        """The account record codex-as keeps, with the usage of an earlier run."""
        usage = {"primary": primary, "secondary": secondary, "seen_at": NOW - 7200}
        return put(Path(launch.usage), {"email": "lab@example.com", "plan": "plus", "usage": usage})

    def attested(self, launch):
        """The path of the account's attested usage, beside the launches' state."""
        return self.guard_dir / f"codex-usage-{launch.account}.json"

    def attested_usage(self, launch, primary=None, secondary=None, owner=TAG, seen_at=NOW - 7200, mode=0o600):
        """The account's attested usage as a guard of an earlier launch wrote it; OWNER None leaves the tag out."""
        data = {"usage": {"primary": primary, "secondary": secondary, "seen_at": seen_at}, "rollout": REL,
                "launch": f"codex-as-{launch.account}-3999-{STARTED - 3600}"}
        if owner is not None:
            data["owner"] = owner
        return put(self.attested(launch), data, mode)

    def attested_reading(self, launch):
        """The usage in the account's attested usage file."""
        return json.loads(self.attested(launch).read_text())["usage"]

    def temporary_files(self):
        return [path.name for path in self.guard_dir.iterdir() if path.name.endswith(".tmp")]

    def rollout(self, records, name="rollout.jsonl", thread=None):
        """A session rollout: its session_meta line from before the launch, then RECORDS. With THREAD it is that
        thread's rollout below CODEX_HOME/sessions/, where the guard notes turns; else NAME in the root."""
        path = self.root / name if thread is None else self.root / "codex" / "sessions" / rel(thread)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = line(STARTED - 3600, "session_meta", {"id": thread or THREAD, "timestamp": iso(STARTED - 3600),
                                                     "cli_version": "0.169.0"})
        path.write_text("".join(json.dumps(record) + "\n" for record in [meta, *records]))
        return path

    def codex_event(self, transcript, kind="prompt", turn=None, agent=None):
        """A Codex hook event; TURN its turn_id, and in a sub-agent AGENT its agent_id: session_id stays the root's."""
        event = {"session_id": THREAD, "cwd": "/",
                 "hook_event_name": {"prompt": "UserPromptSubmit", "tool": "PreToolUse"}.get(kind, "Stop"),
                 "transcript_path": None if transcript is None else str(transcript)}
        if kind == "prompt":
            event["prompt"] = "hello"
        if turn is not None:
            event["turn_id"] = turn
        if agent is not None:
            event["agent_id"], event["agent_type"] = agent, "worker"
        return event

    def turns_file(self, launch, owner=TAG, turns=()):
        """STATE.turns as the launcher creates it (OWNER None leaves its line out) and a guard's note of each
        (turn, rollout path below sessions/) in TURNS."""
        text = (f"o {owner}\n" if owner else "") + "".join(f"t {turn} {path}\n" for turn, path in turns)
        return put(self.state(launch, "turns"), text)

    def notes(self, launch):
        return self.state(launch, "turns").read_text()

    def answer(self, kind, launch, event=None, now=NOW):
        return guard.answer(kind, {} if event is None else event, launch, now)

    @contextlib.contextmanager
    def replaced_while_read(self, path, every=False):
        """Another writer that renames a copy of the file at PATH over it right after a reader opened it: os.fstat
        of a file open as PATH first replaces PATH (once, or EVERY time). Yields the list of replacements."""
        real, replaced = os.fstat, []

        def fstat(fd):
            info = real(fd)
            try:
                there = os.lstat(path)
            except OSError:
                return info
            if (info.st_dev, info.st_ino) != (there.st_dev, there.st_ino) or replaced and not every:
                return info
            copy = Path(f"{path}.other")
            copy.write_bytes(Path(path).read_bytes())
            copy.chmod(0o600)
            os.replace(copy, path)
            replaced.append(True)
            return real(fd)

        with patch.object(os, "fstat", fstat):
            yield replaced

    @contextlib.contextmanager
    def renamed_while_opened(self, path, window):
        """Another writer renames a copy of the file at PATH over it as a reader first opens it, and for WINDOW
        seconds the name still leads to the replaced file, which has no link left: on Linux a rename drops that
        link before the name moves to the new file. Yields the list of opens of PATH that got the replaced file."""
        real, kept, stale = os.open, [], []

        def open_(file, flags, *args, **options):
            if os.fspath(file) != os.fspath(path):
                return real(file, flags, *args, **options)
            if not kept:
                kept.append(real(file, flags, *args, **options))
                copy = Path(f"{path}.other")
                copy.write_bytes(Path(path).read_bytes())
                copy.chmod(0o600)
                os.replace(copy, path)
                kept.append(time.monotonic() + window)
            elif time.monotonic() >= kept[1]:
                return real(file, flags, *args, **options)
            stale.append(True)
            return os.dup(kept[0])

        try:
            with patch.object(os, "open", open_):
                yield stale
        finally:
            if kept:
                os.close(kept[0])

    @contextlib.contextmanager
    def stale_handle(self, path, call):
        """A network file system's answer once another host has renamed a new file over PATH: the first os.open of
        PATH, or the first os.fstat or read of a file open as PATH (CALL), fails with ESTALE. Yields the failures."""
        real_open, real_fstat, real_fdopen, failed = os.open, os.fstat, os.fdopen, []

        def usage(fd):
            try:
                there, info = os.lstat(path), real_fstat(fd)
            except OSError:
                return False
            return (info.st_dev, info.st_ino) == (there.st_dev, there.st_ino)

        def fail():
            failed.append(call)
            raise OSError(errno.ESTALE, os.strerror(errno.ESTALE))

        class Reading:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def read(self, *args):
                if not failed:
                    fail()
                return self.stream.read(*args)

        def open_(file, *args, **options):
            if call == "open" and not failed and os.fspath(file) == os.fspath(path):
                fail()
            return real_open(file, *args, **options)

        def fstat(fd):
            if call == "fstat" and not failed and usage(fd):
                fail()
            return real_fstat(fd)

        def fdopen(fd, *args, **options):
            stream = real_fdopen(fd, *args, **options)
            return Reading(stream) if call == "read" and not failed and usage(fd) else stream

        with patch.object(os, "open", open_), patch.object(os, "fstat", fstat), patch.object(os, "fdopen", fdopen):
            yield failed


class LaunchOf(Fixture):
    EXAMPLE = ('{"v":1,"tool":"claude-as","account":"NAME","five_hour":"12.5","weekly":null,"started":1800000000,'
               '"usage":"/abs/path","state":"/abs/prefix","marker":"/abs/path","statusline":"command"}')

    def test_without_the_variable_there_is_no_launch(self):
        self.assertIsNone(guard.launch_of({}))
        self.assertIsNone(guard.launch_of({"CODING_AGENT_KIT_GUARD_SCRIPT": "/x", "CODING_AGENT_KIT_GUARD_PYTHON": "/y"}))

    def test_the_contract_example_round_trips(self):
        launch = guard.launch_of({"CODING_AGENT_KIT_GUARD": self.EXAMPLE})
        self.assertEqual(launch, guard.Launch("claude-as", "NAME", {"five_hour": D("12.5")}, 1800000000, "/abs/path",
                                              "/abs/prefix", "/abs/path", "command"))
        self.assertEqual(str(launch.enabled["five_hour"]), "12.5")

    def test_unknown_keys_are_ignored(self):
        later = self.EXAMPLE[:-1] + ',"later":1,"hooks":{"v":2}}'
        self.assertEqual(guard.launch_of({"CODING_AGENT_KIT_GUARD": later}),
                         guard.launch_of({"CODING_AGENT_KIT_GUARD": self.EXAMPLE}))

    def test_edge_and_fractional_reserves_arrive_exactly(self):
        for text in ("0", "100", "12.5", "0.25", "99.99"):
            with self.subTest(text=text):
                launch = guard.launch_of(self.environ(self.launch(), five_hour=text, weekly=text))
                self.assertEqual(launch.enabled, {"five_hour": D(text), "weekly": D(text)})
        launch = guard.launch_of(self.environ(self.launch(), five_hour=None, weekly=None))
        self.assertEqual(launch.enabled, {})

    def test_a_codex_launch_carries_its_marker(self):
        launch = self.launch("codex-as", weekly="10")
        self.assertEqual(guard.launch_of(self.environ(launch)), launch)

    def test_malformed_launches_raise(self):
        base = json.loads(self.environ(self.launch("codex-as", weekly="10"))["CODING_AGENT_KIT_GUARD"])
        bad = ["", "{", "[]", "null", '{"v":1,"v":1}', json.dumps(base).replace('"started": ' + str(STARTED), '"started": NaN')]
        for key in ("v", "tool", "account", "five_hour", "weekly", "started", "usage", "state", "marker"):
            bad.append(json.dumps({name: value for name, value in base.items() if name != key}))
        for key, value in (("tool", "codex"), ("tool", None), ("account", "-x"), ("account", "a b"), ("account", 7),
                           ("five_hour", "off"), ("five_hour", "101"), ("five_hour", "1e1"), ("five_hour", 10),
                           ("weekly", ""), ("started", True), ("started", "1800000000"), ("started", 0),
                           ("started", 253402300800), ("usage", "relative/path"), ("state", "state"),
                           ("state", None), ("marker", "codex-hooks"), ("marker", None), ("statusline", 7),
                           ("v", True), ("v", "1"), ("v", 1.0)):
            bad.append(json.dumps({**base, key: value}))
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                guard.launch_of({"CODING_AGENT_KIT_GUARD": raw})

    def test_another_version_is_a_launch_format_failure(self):
        launch = self.launch(weekly="10")
        environ = self.environ(launch, v=2)
        with self.assertRaises(ValueError) as caught:
            guard.launch_of(environ)
        self.assertEqual((caught.exception.tool, caught.exception.account), ("claude-as", "personal"))
        text = reserve.failure_text("claude-as", "personal", "prompt", "launch format")
        self.assertEqual(guard.handle("prompt", b"{}", environ, NOW), (2, "", text))
        self.assertEqual(guard.handle("tool", b"{}", environ, NOW),
                         (0, turn_json(reserve.failure_text("claude-as", "personal", "tool", "launch format")), ""))
        for kind in ("start", "stop", "status"):
            self.assertEqual(guard.handle(kind, b"{}", environ, NOW), OK, kind)

    def test_a_launch_format_failure_leaves_its_message_for_the_launcher(self):
        launch = self.launch("codex-as", weekly="10")
        for kind in ("prompt", "tool"):
            with self.subTest(kind=kind):
                text = reserve.failure_text("codex-as", "lab", kind, "launch format")
                self.assertEqual(guard.handle(kind, b"{}", self.environ(launch, v=2), NOW), (2, "", text))
                self.assertEqual(self.state(launch, "blocked").read_text(), text)
        self.state(launch, "blocked").unlink()
        for kind in ("start", "stop", "status"):
            self.assertEqual(guard.handle(kind, b"{}", self.environ(launch, v=2), NOW), OK)
        for environ in (self.environ(launch, v=2, state="relative"), self.environ(launch, v=2, state=7),
                        {"CODING_AGENT_KIT_GUARD": "{"}):
            self.assertEqual(guard.handle("prompt", b"{}", environ, NOW)[0], 2)
        self.assertEqual(sorted(self.guard_dir.iterdir()), [])

    def test_a_launch_without_tool_or_account_still_blocks(self):
        code, out, err = guard.handle("prompt", b"{}", {"CODING_AGENT_KIT_GUARD": "{"}, NOW)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("the usage reserve guard failed (launch format)", err)
        self.assertNotIn("\n", err)


class Claude(Fixture):
    def test_start_records_that_the_hooks_run(self):
        launch = self.launch(weekly="10")
        self.assertEqual(self.answer("start", launch), OK)
        self.assertTrue(self.state(launch, "ran.start").is_file())
        self.assertEqual(oct(self.state(launch, "ran.start").stat().st_mode & 0o777), "0o600")

    def test_a_first_prompt_without_usage_data_passes(self):
        launch = self.launch(five_hour="10", weekly="10")
        self.assertEqual(self.answer("prompt", launch), OK)
        self.assertTrue(self.state(launch, "ran.prompt").is_file())
        for name in ("ran.tool", "reply", "blocked", "seen"):
            self.assertFalse(self.state(launch, name).exists(), name)

    def test_a_window_at_its_reserve_blocks_each_event_as_c4_says(self):
        launch = self.launch(weekly="10")
        self.claude_usage(launch, seven_day=(91, NOW + 3600))
        prompt = WEEKLY_CLAUDE + STOPPED["prompt"] + RELAUNCH_CLAUDE
        self.assertEqual(self.answer("prompt", launch), (2, "", prompt))
        self.assertEqual(self.state(launch, "blocked").read_text(), prompt)
        tool, stop = (WEEKLY_CLAUDE + STOPPED[kind] + RELAUNCH_CLAUDE for kind in ("tool", "stop"))
        for stop_turn, end_at_stop, expected_tool, expected_stop in (
                (True, True, (0, turn_json(tool), ""), (0, stop_json(stop), "")),
                (False, False, (2, "", tool), OK),
                (True, False, (0, turn_json(tool), ""), OK),
                (False, True, (2, "", tool), (0, stop_json(stop), ""))):
            with self.subTest(stop_turn=stop_turn, end_at_stop=end_at_stop), \
                    patch.object(guard, "STOP_TURN", stop_turn), patch.object(guard, "END_AT_STOP", end_at_stop):
                self.assertEqual(self.answer("tool", launch), expected_tool)
                self.assertEqual(self.state(launch, "blocked").read_text(), tool)
                self.assertEqual(self.answer("stop", launch), expected_stop)
                self.assertEqual(self.state(launch, "blocked").read_text(), stop)
        for name in ("ran.prompt", "ran.tool", "ran.stop", "reply"):
            self.assertTrue(self.state(launch, name).is_file(), name)

    def test_the_settled_constants(self):
        self.assertEqual((guard.STOP_TURN, guard.END_AT_STOP, guard.BLIND_AFTER, guard.RECORD_AGAIN),
                         (True, True, 120, 300))

    def test_readings_that_do_not_count(self):
        launch = self.launch(five_hour="10", weekly="10")
        for five_hour, seven_day in (((95, NOW), (95, NOW - 1)),  # reset passed
                                     (None, (50, NOW + 60)),  # five-hour null, weekly below its reserve
                                     ((95, None), (95, "soon")),  # no usable reset time
                                     ((95, True), (95, 0))):
            with self.subTest(five_hour=five_hour, seven_day=seven_day):
                self.claude_usage(launch, five_hour, seven_day)
                self.assertEqual(self.answer("prompt", launch), OK)
        only_five = self.launch(five_hour="10")
        self.claude_usage(only_five, None, (100, NOW + 60))
        self.assertEqual(self.answer("prompt", only_five), OK)

    def test_an_unusable_usage_file_is_no_reading(self):
        launch = self.launch(weekly="10")
        for content, mode in (("not json", 0o600), ("[]", 0o600), ('{"seven_day": 7}', 0o600),
                              (json.dumps({"seven_day": {"used_percentage": 95, "resets_at": NOW + 60}}), 0o644)):
            with self.subTest(content=content, mode=oct(mode)):
                put(Path(launch.usage), content, mode)
                self.assertEqual(self.answer("prompt", launch), OK)
        Path(launch.usage).unlink()
        Path(launch.usage).mkdir()
        self.assertEqual(self.answer("prompt", launch), OK)

    def test_a_usage_file_replaced_while_it_is_read_is_read_again(self):
        # Another session's status line can rename its usage file over the one a hook has just opened, which then
        # has no link left: the hook reads the file that is there now, so it still blocks at the reserve.
        launch = self.launch(weekly="10")
        self.claude_usage(launch, seven_day=(91, NOW + 3600))
        with self.replaced_while_read(launch.usage) as replaced:
            self.assertEqual(self.answer("prompt", launch), (2, "", WEEKLY_CLAUDE + STOPPED["prompt"] + RELAUNCH_CLAUDE))
        self.assertEqual(replaced, [True])
        # A bounded number of times: a file replaced at every open is still refused.
        accounts = guard.module("accounts")
        with self.replaced_while_read(launch.usage, every=True) as replaced, self.assertRaises(ValueError):
            accounts.read(Path(launch.usage))
        self.assertEqual(len(replaced), accounts.TRIES)
        self.assertEqual(accounts.TRIES, 3)

    def test_a_usage_file_opened_while_a_rename_over_it_is_under_way_is_read_again_after_a_pause(self):
        # On Linux a rename drops the replaced file's link before the name moves to the new file, so opens right after
        # the hook's first one can still get the replaced file: the hook waits before it opens the file again.
        launch = self.launch(weekly="10")
        self.claude_usage(launch, seven_day=(91, NOW + 3600))
        with self.renamed_while_opened(launch.usage, 0.001) as stale:
            self.assertEqual(self.answer("prompt", launch), (2, "", WEEKLY_CLAUDE + STOPPED["prompt"] + RELAUNCH_CLAUDE))
        self.assertEqual(stale, [True])
        self.assertEqual(guard.module("accounts").PAUSE, 0.002)  # longer than the 1 ms the fixture keeps the name

    def test_a_stale_usage_file_handle_is_opened_again(self):
        # On a network home directory another host's rename over the usage file can leave the hook's handle stale
        # (ESTALE) instead of without a link: the hook opens the file again and still blocks at the reserve.
        launch = self.launch(weekly="10")
        self.claude_usage(launch, seven_day=(91, NOW + 3600))
        for call in ("open", "fstat", "read"):
            with self.subTest(call=call):
                with self.stale_handle(launch.usage, call) as failed:
                    self.assertEqual(self.answer("prompt", launch),
                                     (2, "", WEEKLY_CLAUDE + STOPPED["prompt"] + RELAUNCH_CLAUDE))
                self.assertEqual(failed, [call])

    def test_reserves_at_the_edges_and_fractions_through_the_environment(self):
        for reserve_text, used, blocks in (("12.5", 87.5, True), ("12.5", 87.4, False), ("0", 100, True),
                                           ("0", 99.99, False), ("100", 0, True), ("10", 90, True), ("10", 89.9, False)):
            with self.subTest(reserve=reserve_text, used=used):
                launch = guard.launch_of(self.environ(self.launch(), five_hour=reserve_text))
                self.claude_usage(launch, five_hour=(used, NOW + 60))
                code, _, err = self.answer("prompt", launch)
                self.assertEqual(code, 2 if blocks else 0)
                if blocks:
                    self.assertIn(f"at or below its {reserve_text}% reserve", err)

    def test_both_windows_at_their_reserves_name_both_flags(self):
        launch = self.launch(five_hour="5", weekly="10")
        self.claude_usage(launch, (99.5, NOW + 300), (95, NOW + 2 * 86400 + 3 * 3600))
        self.assertEqual(self.answer("prompt", launch), (2, "", (
            "claude-as: personal: five-hour window 0.5% remaining, at or below its 5% reserve; resets in 5m "
            "(2027-01-15T08:05:00Z). weekly window 5% remaining, at or below its 10% reserve; resets in 2d 3h "
            "(2027-01-17T11:00:00Z).\nThis prompt was not sent.\nTo use the reserve, exit and run:\n"
            "  claude-as --reserve-5h off --reserve-weekly off personal --continue")))


class ClaudeBlind(Fixture):
    def blind_text(self, kind):
        return reserve.block_text("claude-as", "personal", reserve.Decision(False, "blind"), kind, NOW)

    def test_the_first_tool_or_stop_event_records_the_first_reply(self):
        for first in ("tool", "stop"):
            with self.subTest(first=first):
                launch = self.launch(weekly="10")
                self.assertEqual(self.answer(first, launch, now=NOW - 50), OK)
                self.assertEqual(self.state(launch, "reply").read_text(), str(NOW - 50))
                for kind in ("tool", "stop", "prompt"):
                    self.assertEqual(self.answer(kind, launch, now=NOW - 10), OK)
                self.assertEqual(self.state(launch, "reply").read_text(), str(NOW - 50))
        launch = self.launch(weekly="10")
        self.answer("prompt", launch)
        self.answer("start", launch)
        self.assertFalse(self.state(launch, "reply").exists())

    def test_a_denying_tool_event_records_the_first_reply(self):
        # After a STOP_TURN deny no Stop hook runs for that turn, so the denying tool event records the reply.
        launch = self.launch(weekly="10")
        self.claude_usage(launch, seven_day=(91, NOW + 3600))
        text = WEEKLY_CLAUDE + STOPPED["tool"] + RELAUNCH_CLAUDE
        self.assertEqual(self.answer("tool", launch, now=NOW - 50), (0, turn_json(text), ""))
        self.assertEqual(self.state(launch, "reply").read_text(), str(NOW - 50))
        self.assertFalse(self.state(launch, "ran.stop").exists())
        for kind in ("tool", "stop", "prompt"):
            self.assertNotEqual(self.answer(kind, launch, now=NOW - 10), OK)
        self.assertEqual(self.state(launch, "reply").read_text(), str(NOW - 50))

    def test_two_minutes_after_the_first_reply_without_data_the_session_is_blind(self):
        launch = self.launch(weekly="10")
        put(self.state(launch, "reply"), str(NOW - 120))
        self.assertEqual(self.answer("prompt", launch), (2, "", self.blind_text("prompt")))
        self.assertEqual(self.state(launch, "blocked").read_text(), self.blind_text("prompt"))
        self.assertEqual(self.answer("tool", launch), (0, turn_json(self.blind_text("tool")), ""))
        self.assertEqual(self.answer("stop", launch), (0, stop_json(self.blind_text("stop")), ""))
        self.assertEqual(self.answer("start", launch), OK)
        put(self.state(launch, "reply"), str(NOW - 119))
        self.assertEqual(self.answer("prompt", launch), OK)

    def test_the_blindness_text(self):
        self.assertEqual(self.blind_text("prompt"), (
            "claude-as: personal: no usage data since this session started, so its usage reserve cannot be checked "
            "(the status line has recorded no rate limits: this account reports none, a server-managed or "
            "registry policy replaced the status line, or a write to coding-agent-kit's state directory failed).\n"
            "This prompt was not sent.\nTo go on without reserves, exit and run:\n"
            "  claude-as --reserve-5h off --reserve-weekly off personal --continue\n"
            'To keep them off for this account, set them to "off" under [usage_reserve.claude_accounts.personal].'))

    def test_data_seen_by_this_launch_clears_it(self):
        launch = self.launch(weekly="10")
        put(self.state(launch, "reply"), str(NOW - 3600))
        put(self.state(launch, "seen"), "")
        self.assertEqual(self.answer("prompt", launch), OK)

    def test_a_window_at_its_reserve_wins_over_blindness(self):
        launch = self.launch(weekly="10")
        put(self.state(launch, "reply"), str(NOW - 3600))
        self.claude_usage(launch, seven_day=(91, NOW + 3600))
        self.assertEqual(self.answer("prompt", launch), (2, "", WEEKLY_CLAUDE + STOPPED["prompt"] + RELAUNCH_CLAUDE))

    def test_the_content_decides_not_the_file_time(self):
        launch = self.launch(weekly="10")
        reply = put(self.state(launch, "reply"), str(NOW - 10))
        os.utime(reply, (NOW - 3600, NOW - 3600))
        self.assertEqual(self.answer("prompt", launch), OK)
        put(reply, str(NOW - 600))
        os.utime(reply, (NOW, NOW))
        self.assertEqual(self.answer("prompt", launch)[0], 2)

    def test_a_reply_file_that_is_not_a_number_counts_as_created_now(self):
        launch = self.launch(weekly="10")
        for content in ("", "soon", "-5", "1.5e9", "\x00"):
            with self.subTest(content=content):
                put(self.state(launch, "reply"), content)
                self.assertEqual(self.answer("prompt", launch), OK)
                self.assertEqual(self.answer("tool", launch), OK)
                self.assertEqual(self.state(launch, "reply").read_text(), content)

    def test_another_launch_of_the_account_never_clears_this_ones_blindness(self):
        first, second = self.launch(weekly="10"), self.launch(weekly="10")
        self.assertEqual(first.usage, second.usage)
        for launch in (first, second):
            put(self.state(launch, "reply"), str(NOW - 600))
        put(self.state(first, "seen"), "")
        self.assertEqual(self.answer("prompt", first), OK)
        self.assertEqual(self.answer("prompt", second), (2, "", self.blind_text("prompt")))


class Quiet(Fixture):
    """start and stop never hold the native up: no exit 2 and nothing on stderr, whatever fails."""

    def assert_quiet(self, launch, kinds=("start", "stop")):
        for kind in kinds:
            code, _, err = self.answer(kind, launch)
            self.assertNotEqual(code, 2, kind)
            self.assertEqual(err, "", kind)
            code, _, err = guard.handle(kind, b"not json", self.environ(launch), NOW)
            self.assertEqual((code, err), (0, ""), kind)

    def test_an_unwritable_state_directory(self):
        if os.geteuid() == 0:
            self.skipTest("root writes into a 0500 directory")
        launch = self.launch(weekly="10")
        self.guard_dir.chmod(0o500)
        self.addCleanup(self.guard_dir.chmod, 0o700)
        self.assert_quiet(launch)
        self.assert_quiet(self.launch("codex-as", weekly="10"))

    def test_a_garbage_usage_file(self):
        for tool in ("claude-as", "codex-as"):
            launch = self.launch(tool, weekly="10")
            put(Path(launch.usage), "\x00garbage")
            self.assert_quiet(launch)

    def test_an_exception_in_the_decision_path(self):
        with patch.object(guard.module("reserve"), "decide", side_effect=RuntimeError("boom")):
            self.assert_quiet(self.launch(weekly="10"))
            self.assert_quiet(self.launch("codex-as", weekly="10"))

    def test_a_missing_or_unknown_event_kind_never_passes_silently(self):
        launch = self.launch(weekly="10")
        for kind in (None, "", "status2", "Prompt"):
            with self.subTest(kind=kind):
                code, out, err = guard.handle(kind, b"{}", self.environ(launch), NOW)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("the usage reserve guard failed", err)
        self.assertEqual(guard.handle(None, b"{}", {}, NOW), OK)


class Codex(Fixture):
    def threshold(self, kind, family=""):
        sentence = WEEKLY_CODEX if not family else WEEKLY_CODEX.replace(".\n", f" (limit {family}).\n")
        return sentence + STOPPED[kind] + RELAUNCH_CODEX

    def test_a_rollout_snapshot_at_the_reserve_blocks_prompt_and_tool_on_stderr(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([reply(NOW - 30), count(NOW - 30, slot(95, 10080))])
        for kind in ("prompt", "tool"):
            with self.subTest(kind=kind):
                self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)),
                                 (2, "", self.threshold(kind)))
                self.assertEqual(self.state(launch, "blocked").read_text(), self.threshold(kind))

    def test_windows_are_named_by_their_length_not_their_slot(self):
        launch = self.launch("codex-as", five_hour="10")
        for primary, secondary, blocks in ((slot(95, 300), None, True), (None, slot(95, 300), True),
                                           (slot(95, 10080), None, False), (slot(99, 60), slot(50, 300), False),
                                           (slot(99, None), slot(50, 300), False), (slot(95, 0), slot(95, 300), True)):
            with self.subTest(primary=primary, secondary=secondary):
                transcript = self.rollout([count(NOW - 30, primary, secondary)])
                self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript))[0], 2 if blocks else 0)

    def test_the_newest_snapshot_decides_and_older_sessions_lines_do_not_count(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([count(NOW - 90, slot(95, 10080)), count(NOW - 30, slot(50, 10080))])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.attested_usage(launch, slot(95, 10080))
        # The session's snapshot, not the account's attested usage, decides while there is one, also when the
        # snapshot is of another limit family.
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        spark = self.rollout([count(NOW - 30, slot(50, 10080), limit_id="spark")])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(spark)), OK)
        self.assertEqual(self.answer("tool", launch, self.codex_event(spark, "tool")), OK)
        before = self.rollout([count(STARTED - 3000, slot(20, 10080))])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(before))[0], 2)

    def test_a_snapshot_without_a_window_of_a_known_length_counts_as_none(self):
        launch = self.launch("codex-as", weekly="10")
        for primary, secondary in ((slot(97, None), None), (slot(97, 60), None), (slot(97, 60), slot(97, 1440))):
            with self.subTest(primary=primary, secondary=secondary):
                transcript = self.rollout([reply(NOW - 30), count(NOW - 30, primary, secondary)])
                self.attested(launch).unlink(missing_ok=True)
                # Nothing the guard can judge by: the session is blind, as after a token count without rate limits.
                for kind in ("prompt", "tool"):
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)),
                                     (2, "", reserve.block_text("codex-as", "lab", reserve.Decision(False, "blind"),
                                                                kind, NOW)))
                # The account's attested usage still decides its threshold.
                self.attested_usage(launch, slot(95, 10080))
                for kind in ("prompt", "tool"):
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)),
                                     (2, "", self.threshold(kind)))

    def test_a_reading_above_100_percent_counts_as_100(self):
        launch = self.launch("codex-as", five_hour="10", weekly="10")

        def exhausted(window, kind):
            decision = reserve.decide(launch.enabled, [reserve.Reading(window, 100, NOW + 3600)], NOW)
            return 2, "", reserve.block_text("codex-as", "lab", decision, kind, NOW)

        self.assertIn("five-hour window 0% remaining", exhausted("five_hour", "prompt")[2])
        beside = self.rollout([reply(NOW - 30), count(NOW - 30, slot(100.5, 300), slot(50, 10080))], "beside.jsonl")
        lone = self.rollout([reply(NOW - 30), count(NOW - 30, slot(101, 10080))], "lone.jsonl")
        for transcript, window in ((beside, "five_hour"), (lone, "weekly")):
            for kind in ("prompt", "tool"):
                with self.subTest(transcript=transcript.name, kind=kind):
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)),
                                     exhausted(window, kind))
        # The account's attested usage, too.
        self.attested_usage(launch, slot(100.5, 10080))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(self.rollout([]))), exhausted("weekly", "prompt"))

    def test_a_window_the_snapshot_does_not_name_comes_from_the_attested_usage(self):
        launch = self.launch("codex-as", five_hour="10", weekly="10")
        self.attested_usage(launch, slot(95, 10080))
        # Codex can report window_minutes 0: the snapshot's weekly slot is unusable, or absent, and the attested
        # usage has it.
        for secondary in (slot(95, 0), None):
            transcript = self.rollout([reply(NOW - 30), count(NOW - 30, slot(20, 300), secondary)])
            for kind in ("prompt", "tool"):
                with self.subTest(secondary=secondary, kind=kind):
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)),
                                     (2, "", self.threshold(kind)))
        # Where the snapshot names the window, the snapshot decides.
        both = self.rollout([reply(NOW - 30), count(NOW - 30, slot(20, 300), slot(50, 10080))])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(both)), OK)
        # Another limit family's snapshot is the limit the session's requests count against: the attested general
        # windows stay out, also those the snapshot does not name.
        spark = self.rollout([reply(NOW - 30), count(NOW - 30, slot(20, 300), None, limit_id="spark")])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(spark)), OK)
        # An attested window without a reset time is no reading here either.
        self.attested_usage(launch, slot(95, 10080, None))
        partial = self.rollout([reply(NOW - 30), count(NOW - 30, slot(20, 300), slot(95, 0))])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(partial)), OK)

    def test_without_a_snapshot_the_attested_usage_decides_minus_readings_without_a_reset(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.attested_usage(launch, slot(95, 10080, None))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.attested_usage(launch, slot(95, 10080, NOW - 1))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.attested_usage(launch, slot(20, 300), slot(95, 10080))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), (2, "", self.threshold("prompt")))
        self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool")),
                         (2, "", self.threshold("tool")))

    def test_attested_usage_of_no_use_is_no_reading(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([])
        for content, mode in (("garbage", 0o600), ('{"usage": 7}', 0o600), ('{"usage": {"primary": 7}}', 0o600),
                              (json.dumps({"owner": TAG, "usage": {"primary": slot(95, 10080)}}), 0o640),
                              (json.dumps({"owner": TAG, "usage": {"primary": slot(95, 10080, None)}}), 0o600),
                              (json.dumps({"owner": TAG, "usage": {"primary": slot(95, 10080, NOW)}}), 0o600),
                              (json.dumps({"owner": TAG, "usage": {"primary": slot(95, 10080, NOW - 1)}}), 0o600)):
            with self.subTest(content=content, mode=oct(mode)):
                put(self.attested(launch), content, mode)
                for kind in ("prompt", "tool"):
                    for path in (None, transcript):
                        self.assertEqual(self.answer(kind, launch, self.codex_event(path, kind)), OK)
        # A directory, or a link to another account's file, in its place.
        self.attested(launch).unlink()
        self.attested(launch).mkdir()
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.attested(launch).rmdir()
        self.attested(launch).symlink_to(self.attested_usage(self.launch("codex-as", "lab-2"), slot(95, 10080)))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)

    def test_the_stored_reading_is_the_attested_usage_never_the_account_record(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([])
        # The record that --info shows, which launches without hooks write too, never decides.
        self.codex_record(launch, slot(95, 10080))
        for kind in ("prompt", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)), OK)
        self.attested_usage(launch, slot(95, 10080))
        Path(launch.usage).unlink()
        for kind in ("prompt", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)), (2, "", self.threshold(kind)))
        # The attested usage is the launch's account's, beside the launches' state: another account's never decides.
        self.attested(launch).rename(self.attested(self.launch("codex-as", "lab-2")))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.assertEqual(guard.attested_path(launch), str(self.guard_dir / "codex-usage-lab.json"))

    def test_token_counts_without_rate_limits_are_blind_on_both_events(self):
        launch = self.launch("codex-as", weekly="10")
        self.attested_usage(launch, slot(20, 10080))
        blind = {kind: reserve.block_text("codex-as", "lab", reserve.Decision(False, "blind"), kind, NOW)
                 for kind in ("prompt", "tool")}
        for records in ([reply(NOW - 30), count(NOW - 30, rates=False)], [count(NOW - 30)],
                        [count(NOW - 30, slot(95, 10080, "x"), None)]):
            transcript = self.rollout(records)
            for kind in ("prompt", "tool"):
                with self.subTest(records=records, kind=kind):
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)), (2, "", blind[kind]))

    def test_usage_lines_whose_time_cannot_be_read_are_blind(self):
        # Format drift in the line times: the guard cannot tell the session's usage, so it is blind, never open.
        launch = self.launch("codex-as", weekly="10")
        blind = {kind: reserve.block_text("codex-as", "lab", reserve.Decision(False, "blind"), kind, NOW)
                 for kind in ("prompt", "tool")}
        for stamp in ((NOW - 20), (NOW - 20) * 1000, "absent"):
            # The second rollout is a new session at its first tool call: a response and its token_usage_record,
            # and no token_count yet.
            for name, records in (("counted", [reply(NOW - 20), count(NOW - 20, rates=False),
                                               count(NOW - 20, slot(97, 10080))]),
                                  ("responses only", [reply(NOW - 20), shell_call(NOW - 20),
                                                      line(NOW - 20, "token_usage_record", {"input_tokens": 1})])):
                for record in records:
                    if stamp == "absent":
                        del record["timestamp"]
                    else:
                        record["timestamp"] = stamp
                transcript = self.rollout(records)
                for kind in ("tool", "prompt"):
                    with self.subTest(stamp=stamp, rollout=name, kind=kind):
                        self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)),
                                         (2, "", blind[kind]))

    def test_a_response_without_a_token_count_is_blind_on_prompt_only(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([reply(NOW - 30), shell_call(NOW - 30)])
        self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool")), OK)
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)),
                         (2, "", reserve.block_text("codex-as", "lab", reserve.Decision(False, "blind"), "prompt", NOW)))

    def test_no_readable_transcript_is_never_blind(self):
        launch = self.launch("codex-as", weekly="10")
        (self.root / "folder").mkdir()
        (self.root / "link.jsonl").symlink_to(self.rollout([count(NOW - 30, rates=False)]))
        for transcript in (None, self.root / "missing.jsonl", self.root / "folder", self.root / "link.jsonl"):
            with self.subTest(transcript=transcript):
                for kind in ("prompt", "tool"):
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)), OK)
        self.assertEqual(self.answer("prompt", launch, {"session_id": "s"}), OK)

    def test_a_transcript_path_of_another_shape_is_a_failure(self):
        launch = self.launch("codex-as", weekly="10")
        for event in ({"transcript_path": 7}, {"transcript_path": ["/x"]}, [], "event", None):
            with self.subTest(event=event):
                code, _, err = guard.answer("prompt", event, launch, NOW)
                self.assertEqual(code, 2)
                self.assertIn("the usage reserve guard failed", err)

    def test_a_family_other_than_codex_is_named(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([count(NOW - 30, slot(95, 10080), limit_id="spark")])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)),
                         (2, "", self.threshold("prompt", "spark")))

    def test_what_each_event_leaves(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.assertEqual(sorted(path.name for path in self.guard_dir.iterdir()), [Path(launch.state).name + ".ran.prompt"])
        self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool")), OK)
        self.assertTrue(self.state(launch, "ran.tool").is_file())
        self.assertTrue(Path(launch.marker).is_file())
        self.assertEqual(oct(Path(launch.marker).stat().st_mode & 0o777), "0o600")
        for name in ("reply", "seen", "blocked"):
            self.assertFalse(self.state(launch, name).exists(), name)
        # With the launcher's notes: one line for the turn and, once the turn holds a snapshot, the attested usage.
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        before = set(os.listdir(self.guard_dir))
        replied = self.rollout(turn(T1, NOW - 60, slot(50, 10080), end=False), thread=THREAD)
        for kind in ("prompt", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(replied, kind, T1)), OK)
        name = Path(launch.state).name
        self.assertEqual(sorted(set(os.listdir(self.guard_dir)) - before),
                         sorted([f"{name}.ran.prompt", f"{name}.ran.tool", "codex-usage-lab.json"]))
        self.assertEqual(self.notes(launch), f"o {TAG}\nt {T1} {REL}\n")
        self.assertEqual(self.temporary_files(), [])

    def test_a_stop_event_only_records_itself(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([count(NOW - 30, slot(95, 10080))])
        for end_at_stop in (True, False):
            with patch.object(guard, "END_AT_STOP", end_at_stop):
                self.assertEqual(self.answer("stop", launch, self.codex_event(transcript, "stop")), OK)
        self.assertTrue(self.state(launch, "ran.stop").is_file())
        self.assertFalse(Path(launch.marker).exists())
        self.assertFalse(self.state(launch, "blocked").exists())

    # -- a launch's turns: STATE.turns, the event's turn_id and the rollout below sessions/

    def test_an_event_notes_its_turn_once(self):
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        transcript = self.rollout(turn(T1, NOW - 60, end=False), thread=THREAD)
        for kind in ("prompt", "tool", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind, T1)), OK)
        self.assertEqual(self.notes(launch), f"o {TAG}\nt {T1} {REL}\n")
        # A turn the tail does not name yet is noted all the same, and a turn is noted once per rollout.
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T2)), OK)
        other = self.rollout(turn(T1, NOW - 60, end=False), thread=THREAD2)
        self.assertEqual(self.answer("tool", launch, self.codex_event(other, "tool", T1)), OK)
        self.assertEqual(self.notes(launch), f"o {TAG}\nt {T1} {REL}\nt {T2} {REL}\nt {T1} {REL2}\n")
        self.assertTrue(self.notes(launch).isascii())

    def test_without_the_launcher_s_notes_nothing_is_noted_and_the_lines_since_the_start_decide(self):
        launch = self.launch("codex-as", weekly="10")
        # Another client's turn since the launch started, then this launch's: a launcher without notes (0.9.1).
        transcript = self.rollout(turn(T0, NOW - 60, slot(95, 10080)) + turn(T1, NOW - 20, end=False), thread=THREAD)
        for kind in ("prompt", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind, T1)),
                             (2, "", self.threshold(kind)))
        self.assertFalse(self.state(launch, "turns").exists(), "the guard created the launcher's notes")
        self.assertFalse(self.attested(launch).exists(), "a reading by time was attested")

    def test_an_event_without_a_usable_turn_id_reads_the_lines_since_the_start(self):
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        transcript = self.rollout(turn(T0, NOW - 60, slot(95, 10080)) + turn(T1, NOW - 20, end=False), thread=THREAD)
        absent = object()
        for value in (absent, None, "1", 1, 1.5, [T1], {"id": T1}, T1.upper(), "auto-compact-0", T1 + "\n", ""):
            for kind in ("prompt", "tool"):
                with self.subTest(turn_id=value, kind=kind):
                    event = self.codex_event(transcript, kind)
                    if value is not absent:
                        event["turn_id"] = value
                    self.assertEqual(self.answer(kind, launch, event), (2, "", self.threshold(kind)))
        self.assertEqual(self.notes(launch), f"o {TAG}\n")
        self.assertFalse(self.attested(launch).exists())

    def test_a_snapshot_of_another_client_s_turn_never_decides(self):
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        # Another client's turn, stamped since the launch started, before the launch's own turn ...
        records = turn(T0, NOW - 90, slot(95, 10080)) + turn(T1, NOW - 60, slot(50, 10080))
        transcript = self.rollout(records, thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool", T1)), OK)
        # ... and one after it: the launch's next prompt is judged by its own earlier turn.
        transcript = self.rollout(records + turn(T2, NOW - 40, slot(95, 10080)) + turn(T3, NOW - 20, end=False),
                                  thread=THREAD)
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T3)), OK)
        self.assertEqual(self.attested_reading(launch)["primary"]["used_percent"], 50)
        # Read by time, the other client's newest snapshot would have refused it.
        self.assertEqual(self.answer("prompt", self.launch("codex-as", weekly="10"),
                                     self.codex_event(transcript, "prompt", T3)), (2, "", self.threshold("prompt")))

    def test_the_first_event_in_a_resumed_session_is_decided_by_the_attested_usage(self):
        # The previous client's last snapshot is 2 seconds old, within the 5 seconds the lines by time allow; it was
        # killed in mid turn (no end line), and its last token count may carry no rate limits.
        launch = self.launch("codex-as", weekly="10", started=NOW - 1)
        self.turns_file(launch)
        for reading, rates in ((slot(95, 10080), True), (None, False)):
            records = turn(T0, NOW - 13, reading, rates=rates, end=False)
            transcript = self.rollout(records + [settings(NOW - 1), started(NOW - 1, T1), context(NOW - 1, T1)],
                                      thread=THREAD)
            with self.subTest(rates=rates):
                self.attested(launch).unlink(missing_ok=True)
                # Nothing attested: allowed, and not blind.
                self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T1)), OK)
                self.attested_usage(launch, slot(50, 10080))
                self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T1)), OK)
                self.attested_usage(launch, slot(95, 10080))
                self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T1)),
                                 (2, "", self.threshold("prompt")))

    def test_a_prompt_whose_turn_the_tail_does_not_name_reads_only_the_noted_turns(self):
        launch = self.launch("codex-as", weekly="10")
        records = turn(T1, NOW - 90, slot(50, 10080)) + turn(T0, NOW - 60, slot(95, 10080))
        transcript = self.rollout(records, thread=THREAD)
        self.turns_file(launch, turns=[(T1, REL)])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T2)), OK)
        self.assertEqual(self.notes(launch), f"o {TAG}\nt {T1} {REL}\nt {T2} {REL}\n")
        # Without an earlier turn of its own there, the attested usage decides.
        self.turns_file(launch)
        self.attested_usage(launch, slot(95, 10080))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T3)),
                         (2, "", self.threshold("prompt")))
        self.attested(launch).unlink()
        self.turns_file(launch)
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T3)), OK)

    def test_a_tool_call_whose_turn_the_tail_does_not_name_reads_the_lines_since_the_start(self):
        launch = self.launch("codex-as", weekly="10")
        records = turn(T1, NOW - 90, slot(50, 10080)) + turn(T0, NOW - 60, slot(95, 10080))
        transcript = self.rollout(records, thread=THREAD)
        self.turns_file(launch, turns=[(T1, REL)])
        self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool", T2)),
                         (2, "", self.threshold("tool")))
        self.assertEqual(self.notes(launch), f"o {TAG}\nt {T1} {REL}\nt {T2} {REL}\n")
        self.assertFalse(self.attested(launch).exists(), "a reading by time was attested")

    def test_a_tail_without_turn_ids_reads_the_lines_since_the_start(self):
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        transcript = self.rollout([reply(NOW - 30), count(NOW - 30, slot(95, 10080))], thread=THREAD)
        for kind in ("prompt", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind, T1)),
                             (2, "", self.threshold(kind)))
        self.assertEqual(self.notes(launch), f"o {TAG}\nt {T1} {REL}\n")
        # A transcript that is no rollout below sessions/ is read by time too, and noted nowhere.
        plain = self.rollout(turn(T0, NOW - 60, slot(95, 10080)) + turn(T2, NOW - 20, end=False))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(plain, "prompt", T2)),
                         (2, "", self.threshold("prompt")))
        self.assertEqual(self.notes(launch), f"o {TAG}\nt {T1} {REL}\n")
        self.assertFalse(self.attested(launch).exists())

    def long_output(self, used, size=None, limit_id="codex"):
        """The launch's turn T1, ended at 80 percent of the weekly window used, and its turn T2 up to its second
        tool call: the first call's output of SIZE bytes (by default longer than the tail, as a screenshot's base64
        can be), the token_count of the response that read it, with USED percent of the weekly window, then that
        response's call and token_usage_record."""
        size = int(1.2 * guard.module("usage").TAIL) if size is None else size
        return turn(T1, NOW - 300, slot(80, 10080)) + [
            started(NOW - 200, T2), context(NOW - 200, T2), shell_call(NOW - 190), usage_record(NOW - 190, T2),
            completed(NOW - 180, T2), output(NOW - 180, size), count(NOW - 179, None, slot(used, 10080), limit_id),
            shell_call(NOW - 178), usage_record(NOW - 178, T2)]

    def refused(self, launch, kind, *readings):
        """The block of KIND that READINGS, (window, used, family) each, give LAUNCH."""
        decision = reserve.decide(launch.enabled, [reserve.Reading(window, used, NOW + 3600, family)
                                                   for window, used, family in readings], NOW)
        return 2, "", reserve.block_text("codex-as", launch.account, decision, kind, NOW)

    def test_after_an_output_longer_than_the_tail_the_reading_before_its_first_naming_line_decides(self):
        # The tail starts inside the output line, so the response's token_count stands before the tail's first line
        # that names a turn. No turn began or ended before that line, and it names the launch's turn T2: one client
        # writes a thread, so the reading is T2's. It decides at every such event, on both kinds, but is never
        # attested: such a reading may stand before the turn's first response and repeat an earlier turn's.
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch, turns=[(T1, REL), (T2, REL)])
        self.attested_usage(launch, slot(80, 10080), seen_at=NOW - 289)
        stored = self.attested(launch).read_text()
        for used in (91, 95, 98, 99.5, 85):
            # The next tool call of T2, and the prompt of T3 after T2 was interrupted there: without the reading,
            # T2's response would leave the prompt blind and the tool call to the attested 80 percent.
            interrupted = [ended(NOW - 170, T2, "turn_aborted"), started(NOW - 160, T3), context(NOW - 160, T3)]
            for kind, after, turn_id in (("tool", [], T2), ("prompt", interrupted, T3)):
                with self.subTest(used=used, kind=kind):
                    transcript = self.rollout(self.long_output(used) + after, thread=THREAD)
                    expected = OK if used == 85 else self.refused(launch, kind, ("weekly", used, None))
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind, turn_id)), expected)
                    self.assertEqual(self.attested(launch).read_text(), stored)
        # A tool call whose turn the tail does not name reads the lines since the start, as it does without one.
        theirs = self.rollout(self.long_output(50) + [ended(NOW - 170, T2)] + turn(T0, NOW - 160, slot(95, 10080)),
                              thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(theirs, "tool", T3)),
                         self.refused(launch, "tool", ("weekly", 95, None)))
        self.assertEqual(self.attested(launch).read_text(), stored)
        # An output that the tail holds whole: the reading follows T2's first naming line and is T2's snapshot.
        whole = self.rollout(self.long_output(95, 300 << 10), thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(whole, "tool", T2)),
                         self.refused(launch, "tool", ("weekly", 95, None)))
        self.assertEqual(self.attested_reading(launch)["secondary"]["used_percent"], 95)

    def test_the_reading_before_the_first_naming_line_counts_as_a_snapshot_would(self):
        launch = self.launch("codex-as", five_hour="10", weekly="10")
        self.turns_file(launch, turns=[(T1, REL), (T2, REL)])
        # A newer reading of T2, after the tail's first naming line, is T2's snapshot: it decides, and is attested.
        newer = self.rollout(self.long_output(95) + [completed(NOW - 170, T2), output(NOW - 170, 100),
                                                     count(NOW - 169, None, slot(50, 10080))], thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(newer, "tool", T2)), OK)
        self.assertEqual(self.attested_reading(launch)["secondary"]["used_percent"], 50)
        # A window the reading does not name comes from the attested usage ...
        self.attested_usage(launch, slot(95, 300), seen_at=NOW - 289)
        low = self.rollout(self.long_output(20), thread=THREAD)
        for kind, turn_id in (("tool", T2), ("prompt", T2)):
            with self.subTest(kind=kind):
                self.assertEqual(self.answer(kind, launch, self.codex_event(low, kind, turn_id)),
                                 self.refused(launch, kind, ("five_hour", 95, None)))
        # ... and another limit family's reading stands alone, named in the message.
        spark = self.rollout(self.long_output(20, limit_id="spark"), thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(spark, "tool", T2)), OK)
        spark = self.rollout(self.long_output(95, limit_id="spark"), thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(spark, "tool", T2)),
                         self.refused(launch, "tool", ("weekly", 95, "spark")))
        self.assertEqual(self.attested_reading(launch)["primary"]["used_percent"], 95)

    def test_turns_count_by_rollout(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout(turn(T1, NOW - 90, slot(95, 10080)) + turn(T2, NOW - 30, end=False), thread=THREAD)
        # T1 is noted for another rollout, which repeats its id (as a sub-agent's copied head does).
        self.turns_file(launch, turns=[(T1, REL2)])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T2)), OK)
        self.turns_file(launch, turns=[(T1, REL)])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T2)),
                         (2, "", self.threshold("prompt")))

    def test_a_snapshot_of_the_launch_s_turn_decides_and_is_recorded(self):
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        # The reply's token_usage_record is at NOW - 50, its token_count a second later.
        transcript = self.rollout(turn(T1, NOW - 60, slot(95, 10080), end=False), thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool", T1)),
                         (2, "", self.threshold("tool")))
        self.assertEqual(stat.S_IMODE(self.attested(launch).lstat().st_mode), 0o600)
        self.assertEqual(json.loads(self.attested(launch).read_text()),
                         {"owner": TAG, "usage": {"primary": slot(95, 10080), "secondary": None, "seen_at": NOW - 50},
                          "rollout": REL, "launch": Path(launch.state).name})
        self.assertEqual(self.temporary_files(), [])
        # Another launch's first prompt, in a session of its own, is judged by it.
        later = self.launch("codex-as", weekly="10")
        self.turns_file(later)
        fresh = self.rollout([started(NOW - 5, T2), context(NOW - 5, T2)], thread=THREAD2)
        self.assertEqual(self.answer("prompt", later, self.codex_event(fresh, "prompt", T2)),
                         (2, "", self.threshold("prompt")))

    def test_another_family_s_snapshot_is_never_recorded(self):
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        transcript = self.rollout(turn(T1, NOW - 60, slot(95, 10080), limit_id="spark", end=False), thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool", T1)),
                         (2, "", self.threshold("tool", "spark")))
        self.assertFalse(self.attested(launch).exists())

    def test_a_sub_agent_s_first_call_is_decided_by_what_another_session_recorded(self):
        # Another launch of the account reads a reply in its root session at a tool call, and records it.
        root = self.launch("codex-as", weekly="10")
        self.turns_file(root)
        parent = self.rollout(turn(T1, NOW - 60, slot(95, 10080), end=False), thread=THREAD)
        self.assertEqual(self.answer("tool", root, self.codex_event(parent, "tool", T1)), (2, "", self.threshold("tool")))
        # A sub-agent's rollout starts with a copy of its parent's history (the parent's turn lines, unmarked, an
        # end event without an id, inherited response items, no token line), then its own turn at its first call,
        # whose token_count follows the call's output.
        inherited = {**reply(NOW - 50), "metadata": {"inherited_user_message": True}}
        head = [started(NOW - 60, T1), context(NOW - 60, T1), inherited, ended(NOW - 55, T1),
                ended(NOW - 55, None, "turn_aborted")]
        own = [started(NOW - 10, S1), context(NOW - 10, S1), shell_call(NOW - 5), usage_record(NOW - 5, S1)]
        sub = self.rollout(head + own, thread=SUB)
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch, turns=[(T1, REL)])
        event = self.codex_event(sub, "tool", S1, agent=SUB)
        self.assertEqual(event["session_id"], THREAD, "the event of a sub-agent names the root session")
        self.assertEqual(self.answer("tool", launch, event), (2, "", self.threshold("tool")))
        self.assertEqual(self.notes(launch), f"o {TAG}\nt {T1} {REL}\nt {S1} {rel(SUB)}\n")
        # Without what the root recorded nothing is checked yet: the sub-agent's own first reply decides.
        self.attested(launch).unlink()
        self.assertEqual(self.answer("tool", launch, event), OK)

    def test_two_launches_of_one_account_share_the_newest_reading(self):
        one, two, three = (self.launch("codex-as", weekly="10") for _ in range(3))
        for launch in (one, two, three):
            self.turns_file(launch)
        first = self.rollout(turn(T1, NOW - 200, slot(50, 10080), end=False), thread=THREAD)
        second = self.rollout(turn(T2, NOW - 100, slot(95, 10080), end=False), thread=THREAD2)
        self.assertEqual(self.answer("tool", one, self.codex_event(first, "tool", T1)), OK)
        self.assertEqual(self.attested_reading(one)["seen_at"], NOW - 190)
        self.assertEqual(self.answer("tool", two, self.codex_event(second, "tool", T2)), (2, "", self.threshold("tool")))
        # The first launch's older reading never replaces the newer one.
        self.assertEqual(self.answer("tool", one, self.codex_event(first, "tool", T1)), OK)
        usage = self.attested_reading(one)
        self.assertEqual((usage["primary"]["used_percent"], usage["seen_at"]), (95, NOW - 90))
        fresh = self.rollout([started(NOW - 5, T3), context(NOW - 5, T3)], thread=SUB)
        self.assertEqual(self.answer("prompt", three, self.codex_event(fresh, "prompt", T3)),
                         (2, "", self.threshold("prompt")))

    def test_another_login_s_attested_usage_is_no_reading(self):
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        transcript = self.rollout([started(NOW - 5, T1), context(NOW - 5, T1)], thread=THREAD)
        self.attested_usage(launch, slot(95, 10080), owner=OTHER_TAG)
        for kind in ("prompt", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind, T1)), OK)
        # A launch without notes knows no tag, and reads it whatever its owner.
        older = self.launch("codex-as", weekly="10")
        for owner in (OTHER_TAG, None):
            with self.subTest(owner=owner):
                self.attested_usage(launch, slot(95, 10080), owner=owner)
                self.assertEqual(self.answer("prompt", older, self.codex_event(transcript, "prompt", T1)),
                                 (2, "", self.threshold("prompt")))
        # A reading of the launch's own turn replaces another login's.
        self.attested_usage(launch, slot(95, 10080), owner=OTHER_TAG, seen_at=NOW - 10)
        replied = self.rollout(turn(T1, NOW - 60, slot(50, 10080), end=False), thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(replied, "tool", T1)), OK)
        data = json.loads(self.attested(launch).read_text())
        self.assertEqual((data["owner"], data["usage"]["primary"]["used_percent"], data["usage"]["seen_at"]),
                         (TAG, 50, NOW - 50))

    def test_notes_that_cannot_be_read_or_written_never_block(self):
        # Another client's turn at the reserve since the launch started, then the launch's new turn: by the launch's
        # turns nothing is known yet, by the lines since the start the other client's snapshot refuses. The
        # launch's own turn at the reserve refuses either way.
        theirs = self.rollout(turn(T0, NOW - 90, slot(95, 10080)) + [started(NOW - 20, T1), context(NOW - 20, T1)],
                              thread=THREAD)
        mine = self.rollout(turn(T2, NOW - 60, slot(95, 10080), end=False), thread=THREAD2)
        events = [(transcript, kind, turn_id) for transcript, turn_id in ((theirs, T1), (mine, T2))
                  for kind in ("prompt", "tool")]
        bare = self.launch("codex-as", weekly="10")  # a launch without notes, as a 0.9.1 launcher starts it
        by_time = {event: self.answer(event[1], bare, self.codex_event(*event)) for event in events}
        self.assertEqual(by_time, {event: (2, "", self.threshold(event[1])) for event in events})
        usage = guard.module("usage")
        # Notes that cannot be read: the event is judged as without notes, and nothing is noted or attested.
        target = put(self.root / "notes elsewhere", f"o {TAG}\n")
        for case in ("a directory", "a link", "a FIFO", "notes whose reading fails"):
            launch = self.launch("codex-as", weekly="10")
            notes = self.state(launch, "turns")
            with contextlib.ExitStack() as stack:
                if case == "a directory":
                    notes.mkdir()
                elif case == "a link":
                    notes.symlink_to(target)
                elif case == "a FIFO":
                    os.mkfifo(notes, 0o600)
                else:
                    self.turns_file(launch)
                    stack.enter_context(patch.object(usage, "turn_notes", side_effect=RuntimeError("boom")))
                for event in events:
                    with self.subTest(case=case, kind=event[1], turn=event[2]):
                        self.assertEqual(self.answer(event[1], launch, self.codex_event(*event)), by_time[event])
            self.assertFalse(self.attested(launch).exists(), "notes that could not be read were taken for a tag")
        self.assertEqual(target.read_text(), f"o {TAG}\n")
        self.assertEqual(self.notes(launch), f"o {TAG}\n")
        # Notes that cannot be written: the event's turn counts all the same, the decision is the one writable
        # notes give, and the launch's own snapshot is attested.
        cases = ["a full disk", "an error of another kind"] + ([] if os.geteuid() == 0 else ["a read-only file"])
        for case in cases:  # root writes into a 0400 file
            launch = self.launch("codex-as", weekly="10")
            self.turns_file(launch)
            self.attested(launch).unlink(missing_ok=True)
            with contextlib.ExitStack() as stack:
                if case == "a read-only file":
                    self.state(launch, "turns").chmod(0o400)
                else:
                    error = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)) if case == "a full disk" else TypeError("boom")
                    stack.enter_context(patch.object(guard, "add_turn", side_effect=error))
                for event in events:
                    with self.subTest(case=case, kind=event[1], turn=event[2]):
                        expected = OK if event[0] == theirs else (2, "", self.threshold(event[1]))
                        self.assertEqual(self.answer(event[1], launch, self.codex_event(*event)), expected)
            self.assertEqual(self.notes(launch), f"o {TAG}\n")
            self.assertEqual(self.attested_reading(launch)["primary"]["used_percent"], 95)

    def test_a_torn_note_does_not_take_the_next_one_with_it(self):
        launch = self.launch("codex-as", weekly="10")
        torn = f"o {TAG}\nt {T1} {REL}\nt {T2[:12]}"  # a write that a full disk cut short
        put(self.state(launch, "turns"), torn)
        transcript = self.rollout(turn(T3, NOW - 60, slot(50, 10080), end=False), thread=THREAD)
        for _ in range(2):
            self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool", T3)), OK)
            self.assertEqual(self.notes(launch), torn + f"\nt {T3} {REL}\n")

    def test_full_notes_take_no_more_lines_and_a_turn_they_do_not_hold_is_read_by_time(self):
        launch = self.launch("codex-as", weekly="10")
        limit, head, mine = guard.module("usage").NOTES, f"o {TAG}\n", f"t {T1} {REL}\n"
        full = head + mine + "x" * (limit - len(head) - len(mine) - 1) + "\n"  # a line of a kind a later release may add
        put(self.state(launch, "turns"), full)
        # The launch's turn T1 below the reserve, then another client's turn at it, both since the launch started.
        records = turn(T1, NOW - 90, slot(50, 10080)) + turn(T0, NOW - 60, slot(95, 10080))
        transcript = self.rollout(records, thread=THREAD)
        # A turn that the full notes hold keeps the launch's turns: T1's reading decides and is attested.
        self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool", T1)), OK)
        self.assertEqual(self.attested_reading(launch)["primary"]["used_percent"], 50)
        # A turn they cannot take any more is read by time, as a launch without notes reads: no later event could
        # count it as the launch's. The other client's reading refuses it, and nothing is noted or attested.
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T2)),
                         (2, "", self.threshold("prompt")))
        self.assertEqual(self.notes(launch), full)
        self.assertEqual(self.attested_reading(launch)["primary"]["used_percent"], 50)
        # The notes' tag still counts: another login's attested usage is no reading for that event.
        fresh = self.rollout([started(NOW - 5, T2), context(NOW - 5, T2)], thread=THREAD)
        self.attested_usage(launch, slot(95, 10080), owner=OTHER_TAG)
        self.assertEqual(self.answer("prompt", launch, self.codex_event(fresh, "prompt", T2)), OK)
        self.attested_usage(launch, slot(95, 10080))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(fresh, "prompt", T2)),
                         (2, "", self.threshold("prompt")))
        # A byte short of the bound, the note is taken and the launch's turns decide.
        put(self.state(launch, "turns"), full[:-2] + "\n")
        transcript = self.rollout(records, thread=THREAD)
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript, "prompt", T2)), OK)
        self.assertEqual(self.notes(launch), full[:-2] + f"\nt {T2} {REL}\n")

    def test_a_failed_record_write_never_blocks(self):
        # The launch's own turns: a reply below the reserve, and a later one at it. Whatever the write of the
        # attested usage does, the first is allowed and the second refused by its threshold.
        low = self.rollout(turn(T1, NOW - 60, slot(50, 10080), end=False), thread=THREAD)
        high = self.rollout(turn(T2, NOW - 40, slot(95, 10080), end=False), thread=THREAD2)
        usage = guard.module("usage")
        for case in ("a guard directory with mode 0755", "a directory in the file's place", "keep fails to write",
                     "keep raises an error of another kind", "the sweep after the write fails"):
            launch = self.launch("codex-as", weekly="10")
            self.turns_file(launch)
            with contextlib.ExitStack() as stack:
                if case == "a directory in the file's place":
                    self.attested(launch).mkdir()
                    stack.callback(self.attested(launch).rmdir)
                elif case == "a guard directory with mode 0755":
                    self.guard_dir.chmod(0o755)
                    self.addCleanup(self.guard_dir.chmod, 0o700)
                    stack.callback(self.guard_dir.chmod, 0o700)
                elif case == "the sweep after the write fails":
                    stack.enter_context(patch.object(guard, "sweep", side_effect=RuntimeError("boom")))
                else:
                    error = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)) if case == "keep fails to write" \
                        else RuntimeError("boom")
                    stack.enter_context(patch.object(usage, "keep", side_effect=error))
                for kind in ("prompt", "tool"):
                    with self.subTest(case=case, kind=kind):
                        self.assertEqual(self.answer(kind, launch, self.codex_event(low, kind, T1)), OK)
                        self.assertEqual(self.answer(kind, launch, self.codex_event(high, kind, T2)),
                                         (2, "", self.threshold(kind)))
                self.assertEqual(self.temporary_files(), [])
                if case == "the sweep after the write fails":  # the write itself took place
                    self.assertEqual(self.attested_reading(launch)["primary"]["used_percent"], 95)
                    self.attested(launch).unlink()
                self.assertFalse(self.attested(launch).is_file())

    def test_blindness_is_told_from_the_launch_s_turns(self):
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        blind = {kind: reserve.block_text("codex-as", "lab", reserve.Decision(False, "blind"), kind, NOW)
                 for kind in ("prompt", "tool")}
        # Another client's token counts without rate limits never make the launch blind ...
        theirs = turn(T0, NOW - 90, rates=False)
        transcript = self.rollout(theirs + [started(NOW - 30, T1), context(NOW - 30, T1)], thread=THREAD)
        for kind in ("prompt", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind, T1)), OK)
        # ... its own do, on both events, ...
        mine = self.rollout(theirs + turn(T1, NOW - 30, rates=False, end=False), thread=THREAD)
        for kind in ("prompt", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(mine, kind, T1)), (2, "", blind[kind]))
        # ... and a model response of its own without one on a prompt only.
        replied = self.rollout(theirs + [started(NOW - 30, T1), context(NOW - 30, T1), shell_call(NOW - 20),
                                         usage_record(NOW - 20, T1)], thread=THREAD)
        self.assertEqual(self.answer("tool", launch, self.codex_event(replied, "tool", T1)), OK)
        self.assertEqual(self.answer("prompt", launch, self.codex_event(replied, "prompt", T2)), (2, "", blind["prompt"]))

    def test_no_readable_transcript_is_never_blind_with_notes_either(self):
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        sessions = self.root / "codex" / "sessions"
        linked = self.rollout(turn(T1, NOW - 60, rates=False, end=False), thread=THREAD)
        folder, link, missing = (sessions / rel(thread) for thread in (THREAD2, SUB, T0))
        folder.mkdir(parents=True)
        link.symlink_to(linked)
        for transcript in (None, missing, folder, link):
            for kind in ("prompt", "tool"):
                with self.subTest(transcript=transcript, kind=kind):
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind, T1)), OK)
        self.assertFalse(self.attested(launch).exists())


class Failure(Fixture):
    def test_an_exception_in_the_decision_path_blocks_prompt_and_tool(self):
        for tool in ("claude-as", "codex-as"):
            launch = self.launch(tool, weekly="10")
            event = self.codex_event(None) if tool == "codex-as" else {}
            with patch.object(guard.module("reserve"), "decide", side_effect=RuntimeError("boom")):
                for kind in ("prompt", "tool"):
                    with self.subTest(tool=tool, kind=kind):
                        text = reserve.failure_text(tool, launch.account, kind, "RuntimeError")
                        expected = (0, turn_json(text), "") if (tool, kind) == ("claude-as", "tool") else (2, "", text)
                        self.assertEqual(guard.answer(kind, event, launch, NOW), expected)
                        self.assertEqual(self.state(launch, "blocked").read_text(), text)
                with patch.object(guard, "STOP_TURN", False):
                    self.assertEqual(guard.answer("tool", event, launch, NOW),
                                     (2, "", reserve.failure_text(tool, launch.account, "tool", "RuntimeError")))

    def test_the_failure_text(self):
        self.assertEqual(reserve.failure_text("codex-as", "lab", "prompt", "JSONDecodeError"), (
            "codex-as: lab: the usage reserve guard failed (JSONDecodeError).\nThis prompt was not sent.\n"
            "To go on without reserves, exit and run:\n  codex-as --reserve-5h off --reserve-weekly off lab resume --last"))

    def test_an_unwritable_state_directory_blocks(self):
        if os.geteuid() == 0:
            self.skipTest("root writes into a 0500 directory")
        launch = self.launch("codex-as", weekly="10")
        self.guard_dir.chmod(0o500)
        self.addCleanup(self.guard_dir.chmod, 0o700)
        for kind in ("prompt", "tool"):
            self.assertEqual(self.answer(kind, launch, self.codex_event(None, kind)),
                             (2, "", reserve.failure_text("codex-as", "lab", kind, "PermissionError")))

    def test_an_event_that_is_not_a_json_object_blocks(self):
        launch = self.launch(weekly="10")
        environ = self.environ(launch)
        for raw, name in ((b"not json", "JSONDecodeError"), (b"[1]", "ValueError"), (b"\xff", "UnicodeDecodeError"),
                          (b"", "JSONDecodeError")):
            with self.subTest(raw=raw):
                self.assertEqual(guard.handle("prompt", raw, environ, NOW),
                                 (2, "", reserve.failure_text("claude-as", "personal", "prompt", name)))

    def test_a_missing_sibling_module_blocks(self):
        launch = self.launch("codex-as", weekly="10")
        with patch.dict(guard.PATHS, {"usage": "codex-as/missing.py"}), patch.dict(guard.MODULES, clear=True):
            self.assertEqual(self.answer("prompt", launch, self.codex_event(None)),
                             (2, "", reserve.failure_text("codex-as", "lab", "prompt", "FileNotFoundError")))
        with patch.dict(guard.PATHS, {"reserve": "coding-agent-kit/missing.py"}), patch.dict(guard.MODULES, clear=True):
            self.assertEqual(self.answer("prompt", launch, self.codex_event(None)),
                             (2, "", reserve.failure_text("codex-as", "lab", "prompt", "FileNotFoundError")))
            self.assertEqual(self.state(launch, "blocked").read_text(),
                             reserve.failure_text("codex-as", "lab", "prompt", "FileNotFoundError"))

    def test_without_reserve_py_the_failure_text_is_still_the_whole_message(self):
        with patch.dict(guard.PATHS, {"reserve": "coding-agent-kit/missing.py"}), patch.dict(guard.MODULES, clear=True):
            for tool in guard.TOOLS:
                for kind in ("prompt", "tool"):
                    with self.subTest(tool=tool, kind=kind):
                        self.assertEqual(guard.failure_text(tool, "lab-2", kind, "OSError"),
                                         reserve.failure_text(tool, "lab-2", kind, "OSError"))
            # The launch itself needs reserve.py: the guard still names the launcher and account, and the way out.
            launch = self.launch(weekly="10")
            text = reserve.failure_text("claude-as", "personal", "tool", "FileNotFoundError")
            self.assertEqual(guard.handle("tool", b"{}", self.environ(launch), NOW), (0, turn_json(text), ""))
            self.assertEqual(self.state(launch, "blocked").read_text(), text)
            for kind in ("start", "stop", "status"):
                self.assertEqual(guard.handle(kind, b"{}", self.environ(launch), NOW), OK, kind)
            self.assertEqual(guard.failure_text("claude-as", None, "prompt", "OSError"),
                             "coding-agent-kit: the usage reserve guard failed (OSError).")
            self.assertEqual(guard.failure_text("claude-as", "personal", "Prompt", "OSError"),
                             "claude-as: personal: the usage reserve guard failed (OSError).")


class Main(Fixture):
    """guard.py as the hook commands run it, with the event on stdin and the launch in the environment."""

    def run_guard(self, command, environ, stdin=b"{}"):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **environ}
        result = subprocess.run(command, input=stdin, env=env, capture_output=True, timeout=60)
        return result.returncode, result.stdout.decode(), result.stderr.decode()

    def kit_copy(self):
        """A copy of lib/ under a directory whose name has a space and a quote."""
        kit = self.root / "kit o'clock dir"
        shutil.copytree(ROOT / "lib", kit / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        return kit / "lib/coding-agent-kit/guard.py"

    def commands(self, script, kind):
        """The guard started directly, through Claude Code's hook command and through Codex's."""
        claude = shlex.join([sys.executable, "-I", "-B", "-S", str(script)]) + " " + kind
        return {"direct": ([sys.executable, "-I", "-B", "-S", str(script), kind], {}),
                "claude": (["/bin/sh", "-c", claude], {}),
                "codex": (["/bin/sh", "-c", CODEX_COMMAND + kind],
                          {"CODING_AGENT_KIT_GUARD_PYTHON": sys.executable, "CODING_AGENT_KIT_GUARD_SCRIPT": str(script)})}

    @staticmethod
    def thresholds(launch, reading, before, after):
        """Each threshold block, with its newline, that a guard reading the clock between BEFORE and AFTER
        can print: the span to the reset is whole minutes of whole seconds, so these clock seconds cover it."""
        return {reserve.block_text(launch.tool, launch.account, reserve.decide(launch.enabled, [reading], now),
                                   "prompt", now) + "\n"
                for now in range(int(before), int(after) + 2)}

    def check_script(self, script):
        launch = self.launch(weekly="10")
        # guard.py judges the reading by the real clock, so the reset is an hour from the real clock.
        reset = int(time.time()) + 3600
        self.claude_usage(launch, seven_day=(91, reset))
        for form, (command, extra) in self.commands(script, "prompt").items():
            with self.subTest(form=form):
                self.assertEqual(self.run_guard(command, extra), OK)
                self.assertEqual(self.run_guard(command, extra, b"not json"), OK)
                before = time.time()
                code, out, err = self.run_guard(command, {**extra, **self.environ(launch)})
                after = time.time()
                self.assertEqual((code, out), (2, ""))
                self.assertIn(err, self.thresholds(launch, reserve.Reading("weekly", 91, reset), before, after))
                self.assertTrue(err.startswith("claude-as: personal: weekly window 9% remaining, at or below its 10% "
                                               "reserve; resets in "), err)
                self.assertTrue(err.endswith(STOPPED["prompt"] + RELAUNCH_CLAUDE + "\n"), err)
                self.assertEqual(self.run_guard(command, {**extra, **self.environ(launch)}, b"not json"),
                                 (2, "", reserve.failure_text("claude-as", "personal", "prompt", "JSONDecodeError") + "\n"))

    def test_the_hook_commands(self):
        self.check_script(ROOT / "lib/coding-agent-kit/guard.py")

    def test_the_hook_commands_from_a_kit_path_with_a_space_and_a_quote(self):
        self.check_script(self.kit_copy())

    def test_a_codex_tool_event_from_a_kit_path_with_a_space_and_a_quote(self):
        script = self.kit_copy()
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([count(int(time.time()) - 5, slot(95, 10080, int(time.time()) + 3600))])
        event = json.dumps(self.codex_event(transcript, "tool")).encode()
        command, extra = self.commands(script, "tool")["codex"]
        environ = {**extra, **self.environ(launch, started=int(time.time()) - 60)}
        code, out, err = self.run_guard(command, environ, event)
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("codex-as: lab: weekly window 5% remaining"), err)
        self.assertTrue(Path(launch.marker).is_file())

    def test_a_codex_hook_run_as_codex_runs_it_notes_the_turn_and_records(self):
        now = int(time.time())
        launch = self.launch("codex-as", weekly="10")
        self.turns_file(launch)
        transcript = self.rollout(turn(T1, now - 60, slot(95, 10080, now + 3600), end=False), thread=THREAD)
        command, extra = self.commands(self.kit_copy(), "tool")["codex"]
        environ = {**extra, **self.environ(launch, started=now - 120)}
        code, out, err = self.run_guard(command, environ, json.dumps(self.codex_event(transcript, "tool", T1)).encode())
        self.assertEqual((code, out), (2, ""))
        self.assertTrue(err.startswith("codex-as: lab: weekly window 5% remaining"), err)
        self.assertEqual(self.notes(launch), f"o {TAG}\nt {T1} {REL}\n")
        data = json.loads(self.attested(launch).read_text())
        self.assertEqual((data["owner"], data["usage"]["seen_at"], data["rollout"], data["launch"]),
                         (TAG, now - 50, REL, Path(launch.state).name))
        self.assertEqual(stat.S_IMODE(self.attested(launch).lstat().st_mode), 0o600)
        self.assertEqual(self.temporary_files(), [])

    def test_start_and_stop_print_nothing_and_exit_0(self):
        launch = self.launch(weekly="10")
        self.claude_usage(launch, seven_day=(91, int(time.time()) + 3600))
        script = ROOT / "lib/coding-agent-kit/guard.py"
        for kind in ("start", "stop"):
            with self.subTest(kind=kind):
                command, extra = self.commands(script, kind)["claude"]
                for stdin in (b"{}", b"garbage"):
                    code, out, err = self.run_guard(command, {**extra, **self.environ(launch)}, stdin)
                    self.assertEqual((code, err), (0, ""))
        self.assertTrue(self.state(launch, "ran.start").is_file())

    def test_a_turn_stop_that_cannot_be_written_is_a_deny_with_exit_2(self):
        launch = self.launch(weekly="10")
        self.claude_usage(launch, seven_day=(91, int(time.time()) + 3600))

        class Broken(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(32, "Broken pipe")

        with patch.dict(os.environ, self.environ(launch)), patch.object(guard.sys, "stdout", Broken()), \
                patch.object(guard.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}"))), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(guard.main(["guard.py", "tool"]), 2)
        self.assertTrue(err.getvalue().startswith("claude-as: personal: weekly window 9% remaining"), err.getvalue())
        self.assertTrue(err.getvalue().endswith(STOPPED["tool"] + RELAUNCH_CLAUDE + "\n"), err.getvalue())
        script = ROOT / "lib/coding-agent-kit/guard.py"
        targets = [">&-"] + ([">/dev/full"] if os.path.exists("/dev/full") else [])
        for target in targets:
            for kind, code in (("tool", 2), ("stop", 0), ("start", 0)):
                with self.subTest(target=target, kind=kind):
                    command = ["/bin/sh", "-c", f'exec "$0" -I -B -S "$1" {kind} {target}', sys.executable, str(script)]
                    result = self.run_guard(command, self.environ(launch))
                    self.assertEqual(result[0], code)
                    if code:
                        self.assertTrue(result[2].endswith(STOPPED["tool"] + RELAUNCH_CLAUDE + "\n"), result[2])
                    else:
                        self.assertEqual(result[2], "")

    def test_an_interrupted_guard_blocks_prompt_and_tool_and_stays_quiet_elsewhere(self):
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))

        def interrupt(*_):
            raise KeyboardInterrupt

        stdin = types.SimpleNamespace(buffer=types.SimpleNamespace(read=interrupt))
        for launch in (self.launch(weekly="10"), self.launch("codex-as", weekly="10"), None):
            for kind in ("prompt", "tool", "start", "stop", "status"):
                for where in ("reading the event", "answering it"):
                    if launch is not None:
                        self.state(launch, "blocked").unlink(missing_ok=True)
                    with self.subTest(tool=launch and launch.tool, kind=kind, where=where), \
                            patch.dict(os.environ, {} if launch is None else self.environ(launch)), \
                            patch.object(guard.sys, "stdin", stdin if where == "reading the event" else
                                         io.TextIOWrapper(io.BytesIO(b"{}"))), \
                            patch.object(guard, "handle", interrupt if where == "answering it" else guard.handle), \
                            contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
                        code = guard.main(["guard.py", kind])
                    answer = (code, out.getvalue(), err.getvalue())
                    if launch is None or kind in guard.QUIET:
                        self.assertEqual(answer, OK)
                        continue
                    text = reserve.failure_text(launch.tool, launch.account, kind, "KeyboardInterrupt")
                    if launch.tool == "claude-as" and kind == "tool":
                        self.assertEqual(answer, (0, turn_json(text), ""))
                    else:
                        self.assertEqual(answer, (2, "", text + "\n"))
                    self.assertEqual(self.state(launch, "blocked").read_text(), text)

    def sigint_cases(self):
        """A Codex launch whose account record is at the weekly reserve by the real clock, the environment of its
        hook commands, and a check of a guard's answer to KIND: the threshold block on prompt and tool, silence
        elsewhere. SIGINT reaches the guard as a terminal's Ctrl-C would, also from a suite started with it ignored
        (a shell's background job)."""
        launch = self.launch("codex-as", weekly="10")
        self.attested_usage(launch, slot(95, 10080, int(time.time()) + 3600))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **self.environ(launch)}
        self.addCleanup(signal.signal, signal.SIGINT, signal.getsignal(signal.SIGINT))
        signal.signal(signal.SIGINT, signal.default_int_handler)

        def check(kind, code, out, err):
            if kind in guard.QUIET:
                self.assertEqual((code, out, err), (0, b"", b""))
            else:
                self.assertEqual((code, out), (2, b""), err)
                self.assertTrue(err.decode().startswith("codex-as: lab: weekly window 5% remaining"), err)
                self.assertTrue(err.decode().endswith(STOPPED[kind] + RELAUNCH_CODEX + "\n"), err)

        return env, check

    def test_a_sigint_while_the_guard_waits_for_its_event_is_ignored(self):
        env, check = self.sigint_cases()
        script = ROOT / "lib/coding-agent-kit/guard.py"
        for kind in ("prompt", "tool", "start", "stop", "status"):
            with self.subTest(kind=kind):
                run = subprocess.Popen([sys.executable, "-I", "-B", "-S", str(script), kind], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                self.addCleanup(run.kill)
                self.assertTrue(asleep(run.pid), "the guard never waited for its event")
                run.send_signal(signal.SIGINT)
                time.sleep(0.2)
                self.assertIsNone(run.poll(), "a SIGINT ended the guard")
                out, err = run.communicate(b"{}", timeout=30)
                check(kind, run.returncode, out, err)

    # guard.py run as a program, with a SIGINT sent to itself at the first import of enum (which signal.py would
    # import) or dataclasses (its first import after the version check): the imports are the window a Ctrl-C most
    # often hits in a hook's short life.
    INTERRUPTED_IMPORT = ("import os, sys, _signal\n"
                          "fired = sys.argv[1]\n"
                          "class Interrupt:\n"
                          "    def find_spec(self, name, path=None, target=None):\n"
                          "        if name in ('enum', 'dataclasses'):\n"
                          "            sys.meta_path.remove(self)\n"
                          "            open(fired, 'w').close()\n"
                          "            os.kill(os.getpid(), _signal.SIGINT)\n"
                          "        return None\n"
                          "sys.meta_path.insert(0, Interrupt())\n"
                          "del sys.argv[:2]\n"
                          "exec(compile(open(sys.argv[0]).read(), sys.argv[0], 'exec'), "
                          "{'__name__': '__main__', '__file__': sys.argv[0]})\n")

    def test_a_sigint_while_the_guard_imports_is_ignored(self):
        env, check = self.sigint_cases()
        script, fired = ROOT / "lib/coding-agent-kit/guard.py", self.root / "interrupted"
        for kind in ("prompt", "tool", "start", "stop", "status"):
            with self.subTest(kind=kind):
                fired.unlink(missing_ok=True)
                result = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", self.INTERRUPTED_IMPORT, str(fired),
                                         str(script), kind], input=b"{}", env=env, capture_output=True, timeout=60)
                self.assertTrue(fired.exists(), "the guard imported neither enum nor dataclasses")
                check(kind, result.returncode, result.stdout, result.stderr)

    def test_an_old_python_blocks_prompt_and_tool_and_stays_quiet_elsewhere(self):
        for kind, code in (("prompt", 2), ("tool", 2), ("start", 0), ("stop", 0), ("status", 0)):
            with self.subTest(kind=kind), patch.object(guard.sys, "version_info", (3, 10, 14)), \
                    patch.object(guard.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}"))), \
                    contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(guard.main(["guard.py", kind]), code)
            self.assertEqual(out.getvalue(), "")
            if code:
                self.assertEqual(err.getvalue(), "coding-agent-kit: the usage reserve guard needs Python 3.11 or newer.\n")
            else:
                self.assertEqual(err.getvalue(), "")

    # guard.py run as a program by an older Python, without any module the guard imports besides sys
    # (dataclasses is new in 3.7): the version check comes before every other import.
    OLD_PYTHON = ("import sys\n"
                  "sys.version_info = (3, 6, 15)\n"
                  "for name in ('dataclasses', 'importlib', 'importlib.util', 'json', 'math', 'os', 'pathlib', 're',\n"
                  "             'signal', 'stat', 'subprocess', 'time'):\n"
                  "    sys.modules[name] = None\n"
                  "del sys.argv[0]\n"
                  "exec(compile(open(sys.argv[0]).read(), sys.argv[0], 'exec'), {'__name__': '__main__', '__file__': sys.argv[0]})\n")

    def test_an_old_python_reaches_the_version_check_before_any_other_import(self):
        script = ROOT / "lib/coding-agent-kit/guard.py"
        launch = self.launch(weekly="10")
        for kind, code in (("prompt", 2), ("tool", 2), ("start", 0), ("stop", 0), ("status", 0)):
            with self.subTest(kind=kind):
                result = self.run_guard([sys.executable, "-I", "-B", "-S", "-c", self.OLD_PYTHON, str(script), kind],
                                        self.environ(launch))
                line = "coding-agent-kit: the usage reserve guard needs Python 3.11 or newer.\n"
                self.assertEqual(result, (code, "", line if code else ""))
        # The whole file is in a grammar such a Python reads, so it gets as far as the check.
        ast.parse(script.read_text(), feature_version=(3, 7))


def gone(pid, wait=5):
    """Whether process PID ends within WAIT seconds; a zombie that waits for its reaper has ended."""
    deadline = time.monotonic() + wait
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
        if state.strip().startswith("Z"):
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def asleep(pid, wait=30):
    """Whether process PID is seen asleep (waiting, as on a read of an open pipe) in two checks in a row
    within WAIT seconds; a starting interpreter is running, not asleep."""
    deadline, seen = time.monotonic() + wait, 0
    while time.monotonic() < deadline:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        seen = seen + 1 if state.startswith(("S", "I")) else 0
        if seen == 2:
            return True
        time.sleep(0.1)
    return False


def kill_group(pid):
    """Best effort: end what is left of the process group of PID when a case fails."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass


def another_users(pid):
    """os.kill as it behaves when process PID belongs to another user: EPERM for it, the real call otherwise."""
    real = os.kill

    def kill(target, signum):
        if target == pid:
            raise PermissionError(1, "Operation not permitted")
        return real(target, signum)

    return kill


def limits(five_hour=(42, NOW + 3600), seven_day=(17, NOW + 86400)):
    """The rate_limits of Claude Code's status line input; a window is (used, reset) or None for none."""
    return {key: {"used_percentage": value[0], "resets_at": value[1]}
            for key, value in (("five_hour", five_hour), ("seven_day", seven_day)) if value is not None}


def status_input(rate_limits="default", extra=None):
    data = {"session_id": "s", "model": {"id": "m", "display_name": "Model é"}, "workspace": {"current_dir": "/"},
            **(extra or {})}
    if rate_limits is not None:
        data["rate_limits"] = limits() if rate_limits == "default" else rate_limits
    return json.dumps(data, ensure_ascii=False).encode()


def window(used, resets):
    return {"used_percentage": used, "resets_at": resets}


class StatusLine(Fixture):
    """The guard as Claude Code's status line: it records the rate limits and chains the user's own command."""

    def setUp(self):
        super().setUp()
        self.scripts = self.root / "status o'clock dir"
        self.scripts.mkdir()

    def script(self, name, body):
        """An executable Python script at a path with a space and a quote, isolated as the guard runs."""
        path = self.scripts / name
        path.write_text(f"#!{sys.executable} -I\nimport os, sys, time\n{body}\n")
        path.chmod(0o700)
        return path

    def usage_file(self, launch):
        return json.loads(Path(launch.usage).read_text())

    def runner(self, body):
        """A script that loads guard.py as `guard`, runs BODY to change it, and then runs guard.main() for the
        kind in its first argument, as the hook and status line commands do."""
        path = self.scripts / f"runner {len(list(self.scripts.glob('runner *')))}.py"
        path.write_text("import importlib.util, os, signal, subprocess, sys, threading, time\n"
                        "spec = importlib.util.spec_from_file_location("
                        f"'guard', {str(ROOT / 'lib/coding-agent-kit/guard.py')!r})\n"
                        "guard = importlib.util.module_from_spec(spec)\n"
                        "spec.loader.exec_module(guard)\n"
                        f"{body}\n"
                        "sys.exit(guard.main([spec.origin, sys.argv[1]]))\n")
        return path

    def run_runner(self, runner, launch, kind="status"):
        """RUNNER started like the status line command of LAUNCH, with Claude Code's input on stdin."""
        return subprocess.Popen([sys.executable, "-I", "-B", "-S", str(runner), kind], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **self.environ(launch)})

    def test_record_validates_like_the_helper(self):
        launch = self.launch(weekly="10")
        for value in (window(-1, NOW + 60), window(100.5, NOW + 60), window(float("nan"), NOW + 60),
                      window(float("inf"), NOW + 60), window(True, NOW + 60), window("42", NOW + 60),
                      window(None, NOW + 60), window(42, 0), window(42, -5), window(42, True), window(42, 1.5),
                      window(42, 253402300800), window(42, "soon"), window(42, None), {"used_percentage": 42}, 7, None):
            with self.subTest(value=value):
                self.assertFalse(guard.record(launch.usage, {"five_hour": value, "seven_day": value}, NOW))
                self.assertFalse(Path(launch.usage).exists())
        for bad in (None, [], "limits", {}):
            self.assertFalse(guard.record(launch.usage, bad, NOW))
        for used, resets in ((0, 1), (100, 253402300799), (42.5, NOW + 60), (7, float(NOW + 60))):
            with self.subTest(used=used, resets=resets):
                self.assertTrue(guard.record(launch.usage, {"five_hour": window(used, resets), "seven_day": 7}, NOW))
                self.assertEqual(self.usage_file(launch),
                                 {"five_hour": window(used, int(resets)), "seven_day": None, "seen_at": NOW})
                Path(launch.usage).unlink()
        for five_hour in ({}, {"five_hour": None}, {"five_hour": window(150, NOW + 60)}):
            with self.subTest(weekly_only=five_hour):
                self.assertTrue(guard.record(launch.usage, {**five_hour, "seven_day": window(95, NOW + 60)}, NOW))
                self.assertEqual(self.usage_file(launch),
                                 {"five_hour": None, "seven_day": window(95, NOW + 60), "seen_at": NOW})
                Path(launch.usage).unlink()

    def test_record_writes_a_private_file_by_rename_into_an_owned_private_directory(self):
        launch = self.launch(weekly="10")
        stale = put(self.accounts / f".{Path(launch.usage).name}.0123abcd.tmp", "half")
        fresh = put(self.accounts / f".{Path(launch.usage).name}.4567cdef.tmp", "half")
        os.utime(stale, (time.time() - 120, time.time() - 120))
        renames = []
        real_replace = os.replace

        def replace(source, target):
            renames.append((Path(source).parent, Path(source).name.startswith("."), oct(os.stat(source).st_mode & 0o777),
                            Path(target)))
            return real_replace(source, target)

        with patch.object(os, "replace", replace):
            self.assertTrue(guard.record(launch.usage, limits(), time.time()))
        self.assertEqual(renames, [(self.accounts, True, "0o600", Path(launch.usage))])
        self.assertEqual(oct(Path(launch.usage).stat().st_mode & 0o777), "0o600")
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        fresh.unlink()
        self.assertEqual(sorted(path.name for path in self.accounts.iterdir()), [Path(launch.usage).name])
        Path(launch.usage).unlink()
        for refused in (0o755, 0o750, 0o500):
            with self.subTest(mode=oct(refused)):
                self.accounts.chmod(refused)
                try:
                    with self.assertRaises((ValueError, OSError)):
                        guard.record(launch.usage, limits(), NOW)
                finally:
                    self.accounts.chmod(0o700)
                self.assertFalse(Path(launch.usage).exists())
        linked = self.root / "linked"
        linked.symlink_to(self.accounts, target_is_directory=True)
        for path in (linked / Path(launch.usage).name, self.accounts / "claude-personal.json",
                     self.accounts / "usage.json", "claude-personal.usage.json"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                guard.record(str(path), limits(), NOW)
        self.assertEqual(list(self.accounts.iterdir()), [])

    def test_temporary_files_are_judged_by_their_file_times_alone(self):
        # On a network home directory file times come from the server's clock, which this host's may run ahead of or
        # behind. A run removes a temporary file that is STALE seconds older than the file it has just written,
        # whatever this host's clock and the run's time say, and keeps another writer's fresh one.
        launch = self.launch(weekly="10")
        name = Path(launch.usage).name
        real = time.time
        for offset in (-3600, 0, 3600, NOW - real()):
            with self.subTest(clock_ahead_by=round(offset)):
                Path(launch.usage).unlink(missing_ok=True)
                stale = put(self.accounts / f".{name}.0123abcd.tmp", "half")
                fresh = put(self.accounts / f".{name}.4567cdef.tmp", "half")
                old = real() - guard.STALE - 60
                os.utime(stale, (old, old))
                now = real() + offset
                with patch.object(guard.time, "time", lambda: now):
                    self.assertTrue(guard.record(launch.usage, limits(), now))
                self.assertFalse(stale.exists())
                self.assertTrue(fresh.exists())
                fresh.unlink()
        self.assertEqual(guard.STALE, 60)

    def test_unchanged_readings_are_rewritten_only_after_record_again(self):
        launch = self.launch(weekly="10")
        self.assertTrue(guard.record(launch.usage, limits(), NOW))
        self.assertFalse(guard.record(launch.usage, limits(), NOW + 299))
        self.assertEqual(self.usage_file(launch)["seen_at"], NOW)
        self.assertTrue(guard.record(launch.usage, limits(), NOW + 300))
        self.assertEqual(self.usage_file(launch)["seen_at"], NOW + 300)
        self.assertTrue(guard.record(launch.usage, limits((43, NOW + 3600)), NOW + 301))
        self.assertEqual(self.usage_file(launch)["five_hour"], window(43, NOW + 3600))
        # A whole seen_at written as a float is that second, as the status line helper reads it.
        put(Path(launch.usage), {**limits((43, NOW + 3600)), "seen_at": float(NOW + 301)})
        self.assertFalse(guard.record(launch.usage, limits((43, NOW + 3600)), NOW + 600))
        self.assertTrue(guard.record(launch.usage, limits((43, NOW + 3600)), NOW + 601))
        self.assertEqual(self.usage_file(launch)["seen_at"], NOW + 601)

    def merged(self, stored, incoming, now=NOW):
        """The windows stored after recording INCOMING over STORED (both rate_limits shapes)."""
        launch = self.launch(weekly="10")
        Path(launch.usage).unlink(missing_ok=True)
        if stored is not None:
            put(Path(launch.usage), {**{"five_hour": None, "seven_day": None}, **stored, "seen_at": NOW - 1000})
        guard.record(launch.usage, incoming, now)
        data = self.usage_file(launch)
        return data["five_hour"], data["seven_day"]

    def test_readings_never_go_backwards(self):
        five, week = NOW + 3600, NOW + 86400
        later = window(60, five + 5 * 3600)
        cases = (
            ("a lower reading of the same window", {"five_hour": window(91, five)}, {"five_hour": window(60, five)},
             (window(91, five), None)),
            ("a higher reading of the same window", {"five_hour": window(60, five)}, {"five_hour": window(91, five)},
             (window(91, five), None)),
            ("a later window", {"five_hour": window(91, five)}, {"five_hour": later}, (later, None)),
            ("an earlier window", {"five_hour": later}, {"five_hour": window(91, five)}, (later, None)),
            ("an absent window that has not reset", {"seven_day": window(80, week)}, {"five_hour": window(10, five)},
             (window(10, five), window(80, week))),
            ("an absent window that has reset", {"seven_day": window(80, NOW)}, {"five_hour": window(10, five)},
             (window(10, five), None)),
            ("an earlier reading over one that has reset", {"five_hour": window(91, NOW - 1)},
             {"five_hour": window(5, NOW - 100)}, (window(5, NOW - 100), None)),
            ("nothing stored", None, {"five_hour": window(10, five)}, (window(10, five), None)),
            ("an unusable stored window", {"five_hour": window(150, five)}, {"five_hour": window(10, five)},
             (window(10, five), None)))
        for name, stored, incoming, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.merged(stored, incoming), expected)
        launch = self.launch(weekly="10")
        put(Path(launch.usage), "garbage")
        self.assertTrue(guard.record(launch.usage, limits(), NOW))
        self.assertEqual(self.usage_file(launch)["five_hour"], window(42, NOW + 3600))

    def test_an_older_sessions_reading_never_replaces_a_newer_one(self):
        newer, older = self.launch(weekly="10"), self.launch(weekly="10")
        self.assertEqual(newer.usage, older.usage)
        guard.status(status_input(limits((91, NOW + 3600), (40, NOW + 86400))), newer, NOW)
        guard.status(status_input(limits((60, NOW + 3600), (30, NOW + 86400))), older, NOW + 400)
        data = self.usage_file(newer)
        self.assertEqual((data["five_hour"], data["seven_day"]), (window(91, NOW + 3600), window(40, NOW + 86400)))

    def test_two_status_lines_at_once_can_lag_until_the_next_run(self):
        # The usage file has no lock: a run that merged before another wrote writes its older merge over the newer
        # reading, and the newer session's next status line run puts its reading back by the merge rule.
        newer, older = self.launch(weekly="10"), self.launch(weekly="10")
        put(Path(newer.usage), {"five_hour": window(50, NOW + 3600), "seven_day": None, "seen_at": NOW - 1000})
        accounts = guard.module("accounts")
        real, merged, written, results = accounts.write, threading.Event(), threading.Event(), []

        def write(path, data):
            if threading.current_thread().name == "older":
                merged.set()  # the older session's run has merged the stored 50%; the newer run writes now
                written.wait(10)
            return real(path, data)

        with patch.object(accounts, "write", write):
            first = threading.Thread(target=lambda: results.append(guard.record(older.usage, limits((60, NOW + 3600), None),
                                                                                NOW)), name="older")
            first.start()
            self.assertTrue(merged.wait(10))
            self.assertTrue(guard.record(newer.usage, limits((91, NOW + 3600), None), NOW))
            written.set()
            first.join(10)
        self.assertEqual(results, [True])
        self.assertEqual(self.usage_file(newer)["five_hour"], window(60, NOW + 3600))
        self.assertTrue(guard.record(newer.usage, limits((91, NOW + 3600), None), NOW + 1))
        self.assertEqual(self.usage_file(newer)["five_hour"], window(91, NOW + 3600))
        self.assertEqual([path.name for path in self.accounts.iterdir()], [Path(newer.usage).name])

    def test_a_usage_file_replaced_while_a_run_merges_keeps_its_windows(self):
        # The merge reads the file that another writer renamed over the one it opened, so a stored window the
        # incoming reading lacks stays.
        launch = self.launch(weekly="10")
        put(Path(launch.usage), {**limits((50, NOW + 3600), (95, NOW + 86400)), "seen_at": NOW - 1000})
        with self.replaced_while_read(launch.usage) as replaced:
            self.assertTrue(guard.record(launch.usage, limits((60, NOW + 3600), None), NOW))
        self.assertEqual(replaced, [True])
        self.assertEqual(self.usage_file(launch), {**limits((60, NOW + 3600), (95, NOW + 86400)), "seen_at": NOW})
        self.assertEqual([path.name for path in self.accounts.iterdir()], [Path(launch.usage).name])

    def test_a_usage_file_a_rename_or_another_host_replaces_while_a_run_merges_keeps_its_windows(self):
        # The same for a merge that opens the file while a rename over it is under way, or whose handle another
        # host's rename made stale (ESTALE): the run reads the new file and keeps the window the reading lacks.
        launch = self.launch(weekly="10")
        stored = {**limits((50, NOW + 3600), (95, NOW + 86400)), "seen_at": NOW - 1000}
        cases = [("a rename under way", lambda: self.renamed_while_opened(launch.usage, 0.001))]
        cases += [(f"ESTALE from {call}", lambda call=call: self.stale_handle(launch.usage, call))
                  for call in ("open", "fstat", "read")]
        for name, replaced in cases:
            with self.subTest(name):
                put(Path(launch.usage), stored)
                with replaced() as events:
                    self.assertTrue(guard.record(launch.usage, limits((60, NOW + 3600), None), NOW))
                self.assertEqual(len(events), 1)
                self.assertEqual(self.usage_file(launch),
                                 {**limits((60, NOW + 3600), (95, NOW + 86400)), "seen_at": NOW})
                self.assertEqual([path.name for path in self.accounts.iterdir()], [Path(launch.usage).name])

    def test_the_usage_file_has_no_lock(self):
        # A run reads, merges and writes the file with no lock of any kind, so it needs no hard link either.
        launch = self.launch(weekly="10")
        put(self.state(launch, "ran.start"), "")
        real = {name: getattr(os, name) for name in ("open", "rename", "replace", "mkdir", "symlink")}
        touched = []

        def watched(name):
            def call(*args, **options):
                touched.extend(os.fsdecode(arg) for arg in args[:2] if isinstance(arg, (str, bytes, os.PathLike)))
                return real[name](*args, **options)
            return call

        # Changed, unchanged, changed and partial readings: three writes and one unchanged read.
        readings = (limits(), limits(), limits((43, NOW + 3600)), limits(None, (95, NOW + 86400)))
        for mask in (0o077, 0o277, 0o477):
            with self.subTest(umask=oct(mask)):
                Path(launch.usage).unlink(missing_ok=True)
                self.state(launch, "seen").unlink(missing_ok=True)
                touched.clear()
                link = Mock(side_effect=OSError(errno.EPERM, os.strerror(errno.EPERM)))  # no hard links here
                previous = os.umask(mask)
                try:
                    with patch.multiple(os, link=link, **{name: watched(name) for name in real}):
                        for offset, reading in enumerate(readings):
                            self.assertEqual(guard.status(status_input(reading), launch, NOW + offset), "")
                finally:
                    os.umask(previous)
                link.assert_not_called()
                self.assertTrue(touched)
                self.assertEqual([name for name in touched if ".lock" in Path(name).name or name.endswith(".break")], [])
                self.assertEqual(self.usage_file(launch),
                                 {**limits((43, NOW + 3600), (95, NOW + 86400)), "seen_at": NOW + 3})
                self.assertEqual(stat.S_IMODE(Path(launch.usage).lstat().st_mode), 0o600)
                self.assertTrue(self.state(launch, "seen").is_file())
                self.assertEqual([path.name for path in self.accounts.iterdir()], [Path(launch.usage).name])

    def test_the_status_line_helper_merges_alike(self):
        # helpers/statusline.cjs, the usage file's other writer, writes what record() writes from the same stored
        # file and reading, and reports the same: both merge by one rule.
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not on PATH")
        major = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        major = major[1:].split(".")[0] if major.startswith("v") else ""
        if not major.isdigit() or int(major) < 22:
            self.skipTest("the node on PATH is not Node.js 22 or newer")
        five, week, later = NOW + 3600, NOW + 86400, NOW + 6 * 3600

        def stored(five_hour=None, seven_day=None, seen=NOW - 1000):
            return json.dumps({"five_hour": five_hour, "seven_day": seven_day, "seen_at": seen})

        cases = (
            ("nothing stored", None, limits((10, five), None)),
            ("a lower reading with the same reset is kept", stored(window(91, five), window(40, week)),
             limits((60, five), (40, week))),
            ("a higher reading with the same reset replaces it", stored(window(60, five)), limits((91, five), None)),
            ("a later reset replaces it", stored(window(91, five)), limits((30, later), None)),
            ("an earlier reset is ignored", stored(window(30, later)), limits((99, five), None)),
            ("an absent window that has not reset is kept", stored(window(30, five), window(41, week)),
             limits(None, (42, week))),
            ("an absent window that has reset is dropped", stored(window(30, NOW), window(41, week)),
             limits(None, (42, week))),
            ("a reset window gives way to an earlier reading", stored(window(91, NOW - 1)), limits((5, NOW - 100), None)),
            ("unchanged within RECORD_AGAIN", stored(window(42, five), window(17, week), NOW - 299), limits()),
            ("unchanged after RECORD_AGAIN", stored(window(42, five), window(17, week), NOW - 300), limits()),
            ("a stored seen_at written as a float", stored(window(42, five), window(17, week), float(NOW - 10)), limits()),
            ("a stored seen_at with a fraction", stored(window(42, five), window(17, week), NOW - 10.5), limits()),
            ("a weekly window alone", None, limits(None, (95, week))),
            ("a whole reset written as a float", None, {"five_hour": window(7, float(five))}),
            ("an unusable stored window", stored(window(150, five)), limits((10, five), None)),
            ("a file that is not JSON", "garbage", limits()),
            ("no valid window", stored(window(91, five)), {"five_hour": window(101, five), "seven_day": window(5, 1.5)}))
        path = Path(self.launch(weekly="10").usage)

        def outcome(wrote):
            return wrote, json.loads(path.read_text()) if path.exists() else None

        ours = []
        for _, data, reading in cases:
            path.unlink(missing_ok=True)
            if data is not None:
                put(path, data)
            ours.append(outcome(guard.record(str(path), reading, NOW)))
        program = ("const fs = require('node:fs');\n"
                   "const helper = require(process.argv[1]);\n"
                   "const file = process.env.CODING_AGENT_KIT_USAGE_FILE;\n"
                   "process.stdout.write(JSON.stringify(JSON.parse(fs.readFileSync(0, 'utf8')).map(([data, limits]) => {\n"
                   "  fs.rmSync(file, { force: true });\n"
                   "  if (data !== null) { fs.writeFileSync(file, data); fs.chmodSync(file, 0o600); }\n"
                   "  const wrote = helper.recordUsage({ rate_limits: limits }, process.env, Number(process.argv[2]) * 1000);\n"
                   "  return [wrote, fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null];\n"
                   "})));\n")
        # As the helper runs under the claude process claude-as became: this process, its parent.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ["HOME"],
               "CODING_AGENT_KIT_USAGE_FILE": str(path), "CODING_AGENT_KIT_TOKEN_ID": "0" * 16,
               "CODING_AGENT_KIT_CLAUDE_PID": str(os.getpid())}
        result = subprocess.run([node, "-e", program, str(ROOT / "helpers/statusline.cjs"), str(NOW)],
                                input=json.dumps([[data, reading] for _, data, reading in cases]), capture_output=True,
                                text=True, env=env, timeout=60)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        theirs = [tuple(pair) for pair in json.loads(result.stdout)]
        self.assertEqual(len(theirs), len(cases))
        for (name, _, _), mine, helper in zip(cases, ours, theirs):
            with self.subTest(name):
                self.assertEqual(helper, mine)
        self.assertEqual([path.name for path in self.accounts.iterdir()], [path.name])

    def test_rate_limits_are_recorded_for_the_account_and_seen_by_this_launch_only(self):
        first, second = self.launch(weekly="10"), self.launch(weekly="10")
        for launch in (first, second):
            put(self.state(launch, "ran.start"), "")
        self.assertEqual(guard.status(status_input(), first, NOW), "")
        self.assertEqual(self.usage_file(first), {**limits(), "seen_at": NOW})
        self.assertTrue(self.state(first, "seen").is_file())
        self.assertFalse(self.state(second, "seen").exists())
        put(self.state(second, "reply"), str(NOW - 600))
        self.assertEqual(self.answer("prompt", second)[0], 2)
        self.assertEqual(guard.status(status_input(), second, NOW), "")
        self.assertEqual(self.answer("prompt", second), OK)

    def test_an_account_that_reports_only_the_weekly_window(self):
        launch = self.launch(weekly="10")
        put(self.state(launch, "ran.start"), "")
        put(self.state(launch, "reply"), str(NOW - 600))
        self.assertEqual(guard.status(status_input(limits(None, (95, NOW + 86400))), launch, NOW), "")
        data = self.usage_file(launch)
        self.assertEqual((data["five_hour"], data["seven_day"]), (None, window(95, NOW + 86400)))
        self.assertTrue(self.state(launch, "seen").is_file())
        decision = reserve.decide(launch.enabled, [reserve.Reading("weekly", 95, NOW + 86400)], NOW)
        self.assertEqual(self.answer("prompt", launch),
                         (2, "", reserve.block_text("claude-as", "personal", decision, "prompt", NOW)))

    def test_under_any_umask_the_guard_reads_what_it_wrote(self):
        # A umask that takes the owner's own read or write bit must not leave a usage file the guard refuses,
        # which would let prompts through at the reserve while .seen keeps the launch from being blind.
        for mask in (0o277, 0o477, 0o000):
            with self.subTest(umask=oct(mask)):
                launch = self.launch(weekly="10")
                Path(launch.usage).unlink(missing_ok=True)
                put(self.state(launch, "ran.start"), "")
                put(self.state(launch, "reply"), str(NOW - 600))
                previous = os.umask(mask)
                try:
                    self.assertEqual(guard.status(status_input(limits(None, (95, NOW + 86400))), launch, NOW), "")
                    decision = reserve.decide(launch.enabled, [reserve.Reading("weekly", 95, NOW + 86400)], NOW)
                    self.assertEqual(self.answer("prompt", launch),
                                     (2, "", reserve.block_text("claude-as", "personal", decision, "prompt", NOW)))
                finally:
                    os.umask(previous)
                for path in (Path(launch.usage), self.state(launch, "seen"), self.state(launch, "ran.prompt"),
                             self.state(launch, "blocked")):
                    self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600, path.name)

    def test_without_rate_limits_nothing_is_recorded(self):
        launch = self.launch(weekly="10")
        for raw in (status_input(None), status_input({}), status_input(limits(None, None)),
                    status_input({"five_hour": window(150, NOW + 60)}), status_input([1]), b"not json", b"\xff",
                    b"[]", b"", json.dumps({"rate_limits": {"five_hour": window(42, NOW + 60)}})[:-3].encode()):
            with self.subTest(raw=raw):
                self.assertEqual(guard.status(raw, launch, NOW), "")
                self.assertEqual(sorted(self.guard_dir.iterdir()), [])
                self.assertEqual(sorted(self.accounts.iterdir()), [])

    def test_a_refused_record_leaves_no_seen(self):
        launch = self.launch(weekly="10", statusline="printf 'mine'")
        put(self.state(launch, "ran.start"), "")
        self.accounts.chmod(0o750)
        self.addCleanup(self.accounts.chmod, 0o700)
        self.assertEqual(guard.status(status_input(), launch, NOW), "mine")
        self.assertFalse(self.state(launch, "seen").exists())
        self.assertFalse(Path(launch.usage).exists())

    def test_a_failed_write_leaves_no_seen_and_no_temporary_file(self):
        # A home directory at its quota: the write fails, so the launch has no data and turns blind, never seen,
        # and the blindness message names the failed write among its causes.
        blind = reserve.block_text("claude-as", "personal", reserve.Decision(False, "blind"), "prompt", NOW)
        self.assertIn("or a write to coding-agent-kit's state directory failed).", blind)
        for call, code in (("replace", errno.ENOSPC), ("fsync", errno.EDQUOT)):
            with self.subTest(call=call):
                launch = self.launch(weekly="10", statusline="printf 'mine'")
                put(self.state(launch, "ran.start"), "")
                put(self.state(launch, "reply"), str(NOW - guard.BLIND_AFTER))
                with patch.object(os, call, Mock(side_effect=OSError(code, os.strerror(code)))):
                    self.assertEqual(guard.status(status_input(), launch, NOW), "mine")
                self.assertFalse(self.state(launch, "seen").exists())
                self.assertEqual(list(self.accounts.iterdir()), [])
                self.assertEqual(self.answer("prompt", launch), (2, "", blind))

    def test_the_footer_is_the_users_own_status_line(self):
        counter = self.script("count it.py", "print(len(sys.stdin.buffer.read()))")
        raw = status_input()
        for command in (shlex.join([sys.executable, str(counter)]), shlex.quote(str(counter)),
                        f'"{counter}" --flag', f"exec {shlex.quote(str(counter))} 2>/dev/null"):
            with self.subTest(command=command):
                launch = self.launch(weekly="10", statusline=command)
                put(self.state(launch, "ran.start"), "")
                self.assertEqual(guard.status(raw, launch, NOW), f"{len(raw)}\n")
        self.assertGreater(len(raw), len(raw.decode()))  # the count is of the exact bytes, not of characters
        launch = self.launch(weekly="10", statusline="printf 'not json still reaches me: %s' \"$(cat)\"")
        self.assertEqual(guard.status(b"plain", launch, NOW), "not json still reaches me: plain")

    def test_the_footer_passes_through_unchanged_with_the_callers_environment(self):
        launch = self.launch(weekly="10", statusline="printf '\\033[36mModel\\033[0m | %s\\n\\377 second\\n' \"$KIT_FOOTER_MARK\"")
        put(self.state(launch, "ran.start"), "")
        with patch.dict(os.environ, {"KIT_FOOTER_MARK": "from the session"}):
            footer = guard.status(status_input(), launch, NOW)
        self.assertEqual(footer.encode("utf-8", "surrogateescape"),
                         b"\x1b[36mModel\x1b[0m | from the session\n\xff second\n")
        home = self.launch(weekly="10", statusline='printf %s "$HOME"')
        put(self.state(home, "ran.start"), "")
        self.assertEqual(guard.status(status_input(), home, NOW), str(self.root / "home"), "the suite's HOME is not temporary")

    def test_no_footer_without_a_command_or_from_one_that_fails_prints_nothing_or_hangs(self):
        sleeper = self.script("sleep long.py", "print('late', flush=True); time.sleep(30)")
        pidfile = self.scripts / "background pid"
        background = f"sleep 30 & echo $! > {shlex.quote(str(pidfile))}; sleep 30; echo late"
        for statusline in (None, "", "printf 'partial'; exit 3", "true", "kill -9 $$", "/nonexistent/status-line",
                           shlex.quote(str(sleeper)), background):
            with self.subTest(statusline=statusline), patch.object(guard, "STATUS_TIMEOUT", 0.5):
                launch = self.launch(weekly="10", statusline=statusline)
                put(self.state(launch, "ran.start"), "")
                started = time.monotonic()
                self.assertEqual(guard.status(status_input(), launch, NOW), "")
                self.assertLess(time.monotonic() - started, 5)
        # The whole process group of a command that runs too long is killed, its background children too.
        self.assertTrue(gone(int(pidfile.read_text())), "a background child of the status line command survived")
        self.assertEqual(guard.STATUS_TIMEOUT, 5)

    def test_a_cancelled_status_line_run_ends_the_users_command_too(self):
        pidfile = self.scripts / "chained pid"
        launch = self.launch(weekly="10", statusline=f"sleep 47.5 & echo $! > {shlex.quote(str(pidfile))}; wait")
        put(self.state(launch, "ran.start"), "")
        script = ROOT / "lib/coding-agent-kit/guard.py"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **self.environ(launch)}
        for name, send in (("SIGTERM to the run's process group", lambda pid: os.killpg(pid, signal.SIGTERM)),
                           ("SIGTERM", lambda pid: os.kill(pid, signal.SIGTERM)),
                           ("SIGHUP", lambda pid: os.kill(pid, signal.SIGHUP)),
                           ("SIGINT", lambda pid: os.kill(pid, signal.SIGINT))):
            with self.subTest(name):
                pidfile.unlink(missing_ok=True)
                run = subprocess.Popen([sys.executable, "-I", "-B", "-S", str(script), "status"], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True)
                run.stdin.write(status_input())
                run.stdin.close()
                run.stdin = None  # communicate() of Python 3.11 and 3.12 would flush the closed stream
                deadline = time.monotonic() + 30
                while not (pidfile.exists() and pidfile.read_text().endswith("\n")) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.addCleanup(kill_group, int(pidfile.read_text()))
                send(run.pid)
                out, err = run.communicate(timeout=30)
                self.assertEqual((run.returncode, out, err), (0, b"", b""))
                self.assertTrue(gone(int(pidfile.read_text())), "the user's status line command outlived the run")

    def test_a_run_cancelled_while_it_starts_the_users_command_ends_it_too(self):
        pidfile = self.scripts / "starting pid"
        launch = self.launch(weekly="10", statusline=f"sleep 47.5 & echo $! > {shlex.quote(str(pidfile))}; wait")
        put(self.state(launch, "ran.start"), "")
        for name in ("SIGTERM", "SIGHUP", "SIGINT"):
            with self.subTest(name):
                pidfile.unlink(missing_ok=True)
                # The cancel comes before Popen() has returned, once the command has started its child.
                runner = self.runner(
                    "class Popen(subprocess.Popen):\n"
                    "    def __init__(self, *args, **kwargs):\n"
                    "        super().__init__(*args, **kwargs)\n"
                    f"        pidfile, deadline = {str(pidfile)!r}, time.monotonic() + 30\n"
                    "        while not (os.path.exists(pidfile) and open(pidfile).read().endswith('\\n')):\n"
                    "            if time.monotonic() > deadline:\n"
                    "                raise SystemExit('the command never started its child')\n"
                    "            time.sleep(0.02)\n"
                    f"        os.kill(os.getpid(), signal.{name})\n"
                    "subprocess.Popen = Popen\n")
                run = self.run_runner(runner, launch)
                out, err = run.communicate(status_input(), timeout=60)
                self.addCleanup(kill_group, int(pidfile.read_text()))
                self.assertEqual((run.returncode, out, err), (0, b"", b""))
                self.assertTrue(gone(int(pidfile.read_text())), "the user's status line command outlived the run")

    def test_a_reply_without_any_hook_run_is_reported_first(self):
        launch = self.launch(weekly="10", statusline="printf 'user footer\\n'")
        self.assertEqual(guard.status(status_input(), launch, NOW), "reserve guard not running\nuser footer\n")
        self.assertEqual(guard.status(status_input(None), launch, NOW), "user footer\n")
        quiet = self.launch(weekly="10")
        self.assertEqual(guard.status(status_input(), quiet, NOW), "reserve guard not running")
        for kind in guard.KINDS:
            with self.subTest(kind=kind):
                ran = self.launch(weekly="10", statusline="printf 'user footer\\n'")
                put(self.state(ran, "ran." + kind), "")
                self.assertEqual(guard.status(status_input(), ran, NOW), "user footer\n")

    def test_a_codex_launch_records_nothing(self):
        launch = self.launch("codex-as", weekly="10")
        self.assertEqual(guard.status(status_input(), launch, NOW), "")
        self.assertEqual(sorted(self.guard_dir.iterdir()), [])
        self.assertEqual(sorted(self.accounts.iterdir()), [])

    def test_through_main_status_always_exits_0_without_stderr(self):
        counter = self.script("count it.py", "print(len(sys.stdin.buffer.read()))")
        script = ROOT / "lib/coding-agent-kit/guard.py"
        good = self.launch(weekly="10", statusline=shlex.join([sys.executable, str(counter)]))
        failing = self.launch(weekly="10", statusline="echo no; exit 1")
        for launch in (good, failing):
            put(self.state(launch, "ran.start"), "")
        cases = [(self.environ(good), status_input(), None), (self.environ(good), b"not json", "8\n"),
                 (self.environ(good), status_input(None), None), (self.environ(failing), status_input(), ""),
                 ({}, status_input(), ""), (self.environ(good, v=2), status_input(), ""),
                 ({"CODING_AGENT_KIT_GUARD": "{"}, status_input(), "")]
        for environ, raw, expected in cases:
            with self.subTest(environ=environ, raw=raw):
                result = subprocess.run([sys.executable, "-I", "-B", "-S", str(script), "status"], input=raw,
                                        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **environ},
                                        capture_output=True, timeout=60)
                self.assertEqual((result.returncode, result.stderr), (0, b""))
                self.assertEqual(result.stdout.decode(), f"{len(raw)}\n" if expected is None else expected)
        self.assertTrue(self.state(good, "seen").is_file())
        self.assertEqual(self.usage_file(good)["five_hour"], window(42, NOW + 3600))
        unguarded = self.launch(weekly="10", statusline="echo no; exit 1")
        self.accounts.chmod(0o755)
        self.addCleanup(self.accounts.chmod, 0o700)
        result = subprocess.run([sys.executable, "-I", "-B", "-S", str(script), "status"], input=status_input(),
                                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **self.environ(unguarded)},
                                capture_output=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"reserve guard not running", b""))
        self.assertFalse(self.state(unguarded, "seen").exists())


DAY = 86400
GONE = 4_194_400  # a process id above every host's limit (Linux 2**22, macOS 99999): a launch that has ended
VARIABLES = ("CODING_AGENT_KIT_GUARD", "CODING_AGENT_KIT_GUARD_SCRIPT", "CODING_AGENT_KIT_GUARD_PYTHON")
MARKER = "codex-hooks-90aeb3ec484a2ca6"


def policy(tool, account, five_hour=None, weekly=None):
    """The resolved policy of a launch with these reserves (Decimal values, None for off)."""
    return reserve.Policy(tool, account,
                          reserve.Setting("five_hour", five_hour, "default" if five_hour is None else "invocation"),
                          reserve.Setting("weekly", weekly, "default" if weekly is None else "global"))


class GuardLaunch(Fixture):
    """What a launcher needs for a guarded launch: the guard's path, the environment and the state's upkeep."""

    def setUp(self):
        super().setUp()
        self.started = int(time.time()) - 5

    def release(self, managed, name):
        """A release tree with guard.py under MANAGED/releases/NAME; returns its lib directory."""
        lib = managed / "releases" / name / "lib"
        (lib / "coding-agent-kit").mkdir(parents=True)
        (lib / "coding-agent-kit/guard.py").write_text("")
        return lib

    def test_the_script_of_a_managed_install_goes_through_current(self):
        managed = self.root / "share o'clock" / "coding-agent-kit"
        lib, other = self.release(managed, "v0.9.0"), self.release(managed, "v0.9.1")
        (managed / "current").symlink_to("releases/v0.9.0")
        through = str(managed / "current/lib/coding-agent-kit/guard.py")
        self.assertEqual(guard_launch.script(lib), through)
        self.assertEqual(guard_launch.script(str(lib)), through)
        self.assertEqual(guard_launch.script(managed / "current/lib"), through)
        self.assertEqual(guard_launch.script(other), str(other / "coding-agent-kit/guard.py"))
        (managed / "current").unlink()
        (managed / "current").symlink_to("releases/v0.9.1")
        self.assertEqual(guard_launch.script(lib), str(lib / "coding-agent-kit/guard.py"))
        self.assertEqual(guard_launch.script(other), str(managed / "current/lib/coding-agent-kit/guard.py"))
        (managed / "current").unlink()
        self.assertEqual(guard_launch.script(lib), str(lib / "coding-agent-kit/guard.py"))

    def test_the_script_of_a_checkout_or_copy_is_under_its_resolved_root(self):
        self.assertEqual(guard_launch.script(ROOT / "lib"), str(ROOT / "lib/coding-agent-kit/guard.py"))
        linked = self.root / "linked kit"
        linked.symlink_to(ROOT)
        self.assertEqual(guard_launch.script(linked / "lib"), str(ROOT / "lib/coding-agent-kit/guard.py"))
        # A directory named releases without a current link is a plain copy.
        copy = self.release(self.root / "copy", "kit")
        self.assertEqual(guard_launch.script(copy), str(copy / "coding-agent-kit/guard.py"))
        (copy / "coding-agent-kit/guard.py").unlink()
        with self.assertRaises(FileNotFoundError):
            guard_launch.script(copy)

    def test_clear_drops_the_three_variables_only(self):
        env = {name: "inherited" for name in VARIABLES} | {"CODING_AGENT_KIT_USAGE_FILE": "/kept", "PATH": "/bin"}
        guard_launch.clear(env)
        self.assertEqual(env, {"CODING_AGENT_KIT_USAGE_FILE": "/kept", "PATH": "/bin"})
        guard_launch.clear(env)
        self.assertEqual(guard_launch.VARIABLES, VARIABLES)

    def test_prepare_exports_a_claude_launch(self):
        env = {name: "inherited" for name in VARIABLES} | {"PATH": "/bin"}
        usage = str(self.accounts / "claude-personal.usage.json")
        command = "bash '/home/a b/it'\"'\"'s status.sh' --flag é"
        state = guard_launch.prepare(env, self.guard_dir, "claude-as", "personal",
                                     policy("claude-as", "personal", five_hour=D("12.5")), usage, self.started,
                                     statusline=command)
        self.assertEqual(state, str(self.guard_dir / f"claude-as-personal-{os.getpid()}-{self.started}"))
        launched = Path(state + ".launched").lstat()
        self.assertTrue(stat.S_ISREG(launched.st_mode))
        self.assertEqual(stat.S_IMODE(launched.st_mode), 0o600)
        self.assertEqual(Path(state + ".launched").read_text(), os.uname().nodename, "the launch's host is not named")
        expected = {"v": 1, "tool": "claude-as", "account": "personal", "five_hour": "12.5", "weekly": None,
                    "started": self.started, "usage": usage, "state": state, "statusline": command}
        self.assertEqual(env["CODING_AGENT_KIT_GUARD"], json.dumps(expected, separators=(",", ":")))
        self.assertTrue(env["CODING_AGENT_KIT_GUARD"].isascii())
        self.assertEqual(env["CODING_AGENT_KIT_GUARD_SCRIPT"], str(ROOT / "lib/coding-agent-kit/guard.py"))
        self.assertEqual(env["CODING_AGENT_KIT_GUARD_PYTHON"], sys.executable)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(guard.launch_of(env), guard.Launch("claude-as", "personal", {"five_hour": D("12.5")},
                                                            self.started, usage, state, None, command))

    def test_prepare_exports_a_codex_launch(self):
        env = {}
        record, marker = str(self.accounts / "codex-lab.json"), str(self.guard_dir / MARKER)
        state = guard_launch.prepare(env, self.guard_dir, "codex-as", "lab", policy("codex-as", "lab", weekly=D(10)),
                                     record, self.started, marker=marker)
        data = json.loads(env["CODING_AGENT_KIT_GUARD"])
        self.assertEqual(data, {"v": 1, "tool": "codex-as", "account": "lab", "five_hour": None, "weekly": "10",
                                "started": self.started, "usage": record, "state": state, "marker": marker})
        self.assertEqual(guard.launch_of(env), guard.Launch("codex-as", "lab", {"weekly": D(10)}, self.started,
                                                            record, state, marker, None))
        self.assertTrue(Path(state + ".launched").is_file())
        self.assertFalse(Path(marker).exists(), "the marker is the guard's to create")

    def test_reserves_reach_the_guard_exactly(self):
        for index, (five_hour, weekly) in enumerate(((D(0), D(100)), (D("0.25"), D("99.99")), (D("12.50"), None),
                                                     (None, D("1E+1")), (D("0.00"), D("7")))):
            with self.subTest(five_hour=five_hour, weekly=weekly):
                env = {}
                guard_launch.prepare(env, self.guard_dir, "claude-as", "personal",
                                     policy("claude-as", "personal", five_hour, weekly),
                                     str(self.accounts / "claude-personal.usage.json"), self.started + index)
                launch = guard.launch_of(env)
                self.assertEqual(launch.enabled, policy("claude-as", "personal", five_hour, weekly).enabled())
                for window, value in launch.enabled.items():
                    self.assertEqual(json.loads(env["CODING_AGENT_KIT_GUARD"])[window], reserve.percent(value))

    def test_prepare_refuses_what_the_guard_could_not_read(self):
        usage, record = str(self.accounts / "claude-personal.usage.json"), str(self.accounts / "codex-lab.json")
        marker = str(self.guard_dir / MARKER)
        on = policy("claude-as", "personal", weekly=D(10))
        cases = {
            "no reserve on": ("claude-as", "personal", policy("claude-as", "personal"), usage, self.started, {}),
            "another launcher": ("gemini-as", "personal", on, usage, self.started, {}),
            "another tool's policy": ("codex-as", "personal", on, record, self.started, {"marker": marker}),
            "another account's policy": ("claude-as", "work", on, usage, self.started, {}),
            "an account name": ("claude-as", "-p", policy("claude-as", "-p", weekly=D(10)), usage, self.started, {}),
            "a reserve above 100": ("claude-as", "personal", policy("claude-as", "personal", weekly=D(101)), usage,
                                    self.started, {}),
            "a reserve that is no Decimal": ("claude-as", "personal", policy("claude-as", "personal", weekly=10),
                                             usage, self.started, {}),
            "a relative usage path": ("claude-as", "personal", on, "claude-personal.usage.json", self.started, {}),
            "a start time": ("claude-as", "personal", on, usage, float(self.started), {}),
            "a start of zero": ("claude-as", "personal", on, usage, 0, {}),
            "no Codex marker": ("codex-as", "lab", policy("codex-as", "lab", weekly=D(10)), record, self.started, {}),
            "a relative marker": ("codex-as", "lab", policy("codex-as", "lab", weekly=D(10)), record, self.started,
                                  {"marker": MARKER}),
            "a Claude marker": ("claude-as", "personal", on, usage, self.started, {"marker": marker}),
            "a Codex status line": ("codex-as", "lab", policy("codex-as", "lab", weekly=D(10)), record, self.started,
                                    {"marker": marker, "statusline": "echo"}),
            "a status line that is no string": ("claude-as", "personal", on, usage, self.started, {"statusline": 1}),
        }
        for reason, (tool, account, resolved, path, started, extra) in cases.items():
            with self.subTest(reason=reason):
                env = {name: "inherited" for name in VARIABLES}
                with self.assertRaises(ValueError):
                    guard_launch.prepare(env, self.guard_dir, tool, account, resolved, path, started, **extra)
                self.assertEqual(env, {}, "an inherited guard variable survived a refusal")
        with self.assertRaises(ValueError):
            guard_launch.prepare({}, "guard", "claude-as", "personal", on, usage, self.started)
        with self.assertRaises(TypeError):
            guard_launch.prepare({}, self.guard_dir, "claude-as", "personal", on, usage, self.started, other="x")
        self.assertEqual(os.listdir(self.guard_dir), [], "a refused launch left state")

    def test_prepare_leaves_a_0600_launched_under_any_umask(self):
        for mask in (0o277, 0o477, 0o000):
            with self.subTest(umask=oct(mask)):
                env, previous = {}, os.umask(mask)
                try:
                    state = guard_launch.prepare(env, self.guard_dir, "claude-as", "personal",
                                                 policy("claude-as", "personal", weekly=D(10)),
                                                 str(self.accounts / "claude-personal.usage.json"), self.started + mask)
                finally:
                    os.umask(previous)
                launched = Path(state + ".launched")
                self.assertEqual(stat.S_IMODE(launched.lstat().st_mode), 0o600)
                self.assertEqual(launched.read_text(), os.uname().nodename)

    def test_prepare_never_writes_through_a_link(self):
        target = self.root / "elsewhere"
        prefix = self.guard_dir / f"claude-as-personal-{os.getpid()}-{self.started}"
        Path(str(prefix) + ".launched").symlink_to(target)
        env = {name: "inherited" for name in VARIABLES}
        with self.assertRaises(OSError):
            guard_launch.prepare(env, self.guard_dir, "claude-as", "personal",
                                 policy("claude-as", "personal", weekly=D(10)),
                                 str(self.accounts / "claude-personal.usage.json"), self.started)
        self.assertFalse(target.exists())
        self.assertEqual(env, {})

    def test_prepare_creates_the_turns_file_for_codex_only(self):
        record, marker = str(self.accounts / "codex-lab.json"), str(self.guard_dir / MARKER)
        on = policy("codex-as", "lab", weekly=D(10))
        for mask in (0o077, 0o277, 0o000):
            with self.subTest(umask=oct(mask)):
                env, previous = {}, os.umask(mask)
                try:
                    state = guard_launch.prepare(env, self.guard_dir, "codex-as", "lab", on, record,
                                                 self.started + mask, marker=marker, turns=TAG)
                finally:
                    os.umask(previous)
                notes = Path(state + ".turns").lstat()
                self.assertTrue(stat.S_ISREG(notes.st_mode))
                self.assertEqual(stat.S_IMODE(notes.st_mode), 0o600)
                self.assertEqual(Path(state + ".turns").read_bytes(), f"o {TAG}\n".encode("ascii"))
                self.assertTrue(Path(state + ".launched").is_file())
                # The launch JSON does not name the notes: the guard finds them beside the state.
                self.assertEqual(json.loads(env["CODING_AGENT_KIT_GUARD"]),
                                 {"v": 1, "tool": "codex-as", "account": "lab", "five_hour": None, "weekly": "10",
                                  "started": self.started + mask, "usage": record, "state": state, "marker": marker})
        # Without a tag no notes file is made: the launch runs as under a launcher that has none.
        state = guard_launch.prepare({}, self.guard_dir, "codex-as", "lab", on, record, self.started + 1000,
                                     marker=marker)
        self.assertTrue(Path(state + ".launched").is_file())
        self.assertFalse(os.path.lexists(state + ".turns"))
        usage = str(self.accounts / "claude-personal.usage.json")
        cases = {
            "a tag in upper case": ("codex-as", "lab", on, record, {"marker": marker, "turns": TAG.upper()}),
            "a short tag": ("codex-as", "lab", on, record, {"marker": marker, "turns": TAG[:15]}),
            "a long tag": ("codex-as", "lab", on, record, {"marker": marker, "turns": TAG + "0"}),
            "a tag with a line feed": ("codex-as", "lab", on, record, {"marker": marker, "turns": TAG + "\n"}),
            "a tag as bytes": ("codex-as", "lab", on, record, {"marker": marker, "turns": TAG.encode()}),
            "an empty tag": ("codex-as", "lab", on, record, {"marker": marker, "turns": ""}),
            "a Claude tag": ("claude-as", "personal", policy("claude-as", "personal", weekly=D(10)), usage,
                             {"turns": TAG}),
        }
        for reason, (tool, account, resolved, path, extra) in cases.items():
            with self.subTest(reason=reason):
                env = {name: "inherited" for name in VARIABLES}
                with self.assertRaises(ValueError):
                    guard_launch.prepare(env, self.guard_dir, tool, account, resolved, path, self.started + 2000,
                                         **extra)
                self.assertEqual(env, {}, "an inherited guard variable survived a refusal")
        self.assertFalse([name for name in self.names() if f"-{self.started + 2000}." in name], "a refusal left state")

    def test_prepare_removes_launched_when_the_turns_file_cannot_be_created(self):
        record, marker = str(self.accounts / "codex-lab.json"), str(self.guard_dir / MARKER)
        on = policy("codex-as", "lab", weekly=D(10))
        prefix = self.guard_dir / f"codex-as-lab-{os.getpid()}-{self.started}"
        target = self.root / "elsewhere"
        Path(str(prefix) + ".turns").symlink_to(target)
        env = {name: "inherited" for name in VARIABLES}
        with self.assertRaises(OSError):
            guard_launch.prepare(env, self.guard_dir, "codex-as", "lab", on, record, self.started, marker=marker,
                                 turns=TAG)
        self.assertFalse(target.exists())
        self.assertEqual(env, {})
        self.assertEqual(self.names(), [prefix.name + ".turns"], "the launch's .launched was left behind")
        Path(str(prefix) + ".turns").unlink()
        # A write that a full disk refuses: neither file stays.
        real, writes = os.write, []

        def write(fd, data):
            writes.append(data)
            if data.startswith(b"o "):
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            return real(fd, data)

        with patch.object(guard_launch.os, "write", write), self.assertRaises(OSError):
            guard_launch.prepare(env, self.guard_dir, "codex-as", "lab", on, record, self.started, marker=marker,
                                 turns=TAG)
        self.assertEqual(writes[-1], f"o {TAG}\n".encode("ascii"))
        self.assertEqual(env, {})
        self.assertEqual(self.names(), [])

    def test_forget_and_prune_remove_the_turns_file(self):
        env, record, marker = {}, str(self.accounts / "codex-lab.json"), str(self.guard_dir / MARKER)
        state = guard_launch.prepare(env, self.guard_dir, "codex-as", "lab", policy("codex-as", "lab", weekly=D(10)),
                                     record, self.started, marker=marker, turns=TAG)
        self.assertEqual(self.names(), sorted(os.path.basename(state) + suffix for suffix in (".launched", ".turns")))
        guard_launch.forget(state)
        self.assertEqual(self.names(), [])
        # The account's attested usage and its temporary files are no launch's state.
        attested = ["codex-usage-lab.json", ".codex-usage-lab.json.0123456789abcdef.tmp"]
        for name in attested:
            (self.guard_dir / name).write_text("")
        one = self.state_files("codex-as", "lab", 4001, self.started, "launched", "turns", "ran.tool")
        # An older prune() leaves the notes of a launch whose other state it removed.
        old = self.state_files("codex-as", "lab", 4002, self.started - 30 * DAY - 1, "turns")
        kept = self.state_files("codex-as", "lab", 4003, self.started - DAY, "launched", "turns")
        guard_launch.forget(one)
        guard_launch.prune(self.guard_dir, self.started)
        self.assertFalse(os.path.lexists(old + ".turns"))
        self.assertEqual(self.names(), sorted(attested + [os.path.basename(kept) + suffix
                                                          for suffix in (".launched", ".turns")]))

    def test_launch_state_names_take_the_turns_file_and_never_the_attested_usage(self):
        for name, prefix in (("codex-as-lab-4001-1800000000.turns", "codex-as-lab-4001-1800000000"),
                             ("claude-as-personal-7-1800000000.turns", "claude-as-personal-7-1800000000"),
                             ("codex-usage-lab.json", None), ("codex-usage-lab-4001-1800000000.turns", None),
                             (".codex-usage-lab.json.0123456789abcdef.tmp", None),
                             ("codex-as-lab-4001-1800000000.turns.tmp", None),
                             ("codex-as-lab-4001-1800000000.turn", None)):
            with self.subTest(name=name):
                parsed = guard_launch.STATE.fullmatch(name)
                self.assertEqual(parsed and parsed["prefix"], prefix)
        self.assertTrue(guard_launch.OWNER.fullmatch(TAG))
        for tag in (TAG.upper(), TAG[:15], TAG + "0", "g" * 16):
            self.assertFalse(guard_launch.OWNER.fullmatch(tag), tag)

    def state_files(self, tool, account, pid, started, *suffixes, host=None):
        """Launch state as the launcher and the guard leave it, with HOST (by default this one) named in
        .launched; returns the prefix."""
        prefix = self.guard_dir / f"{tool}-{account}-{pid}-{started}"
        for suffix in suffixes:
            Path(f"{prefix}.{suffix}").write_text(
                (os.uname().nodename if host is None else host) if suffix == "launched" else "")
        return str(prefix)

    def names(self):
        return sorted(os.listdir(self.guard_dir))

    def test_prune_removes_launch_state_older_than_30_days(self):
        now = self.started
        old = self.state_files("claude-as", "personal", 4001, now - 30 * DAY - 1, "launched", "ran.start", "ran.prompt",
                               "reply", "seen", "blocked", "blocked.0a1b2c3d4e5f.tmp")
        old_codex = self.state_files("codex-as", "lab-2", 4002, now - 90 * DAY, "launched", "ran.tool")
        kept = self.state_files("claude-as", "personal", 4003, now - 30 * DAY, "launched", "ran.start")
        new = self.state_files("codex-as", "lab", 4004, now - DAY, "launched", "ran.prompt", "blocked")
        marker = self.guard_dir / MARKER
        marker.write_text("")
        os.utime(marker, (now - 400 * DAY, now - 400 * DAY))
        other = self.guard_dir / "notes.txt"
        other.write_text("")
        folder = self.guard_dir / f"claude-as-personal-4005-{now - 90 * DAY}.launched"
        folder.mkdir()
        guard_launch.prune(self.guard_dir, now)
        names = self.names()
        self.assertFalse([name for name in names if name.startswith(os.path.basename(old) + ".")])
        self.assertFalse([name for name in names if name.startswith(os.path.basename(old_codex) + ".")])
        for prefix, suffixes in ((kept, ("launched", "ran.start")), (new, ("blocked", "launched", "ran.prompt"))):
            self.assertEqual(sorted(name for name in names if name.startswith(os.path.basename(prefix) + ".")),
                             [f"{os.path.basename(prefix)}.{suffix}" for suffix in suffixes])
        for path in (marker, other, folder):
            self.assertTrue(path.exists(), path)
        guard_launch.prune(self.root / "missing", now)

    def test_an_earlier_launch_whose_hooks_never_ran(self):
        earlier = self.state_files("claude-as", "personal", GONE + 1, self.started - 3600, "launched", "seen")
        self.assertTrue(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", None))
        self.assertEqual(self.names(), [], "the earlier launch's state was kept")
        self.assertFalse(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", None))
        # A usage file recorded at or after that launch's start is the other sign of a reply.
        for recorded, expected in ((self.started - 3601, False), (None, False), (True, False), ("x", False),
                                   (self.started - 3600, True), (self.started + 0.5, True)):
            with self.subTest(recorded=recorded):
                earlier = self.state_files("claude-as", "personal", GONE + 1, self.started - 3600, "launched")
                self.assertEqual(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", recorded), expected)
                self.assertEqual(Path(earlier + ".launched").exists(), not expected)
        for path in self.guard_dir.iterdir():
            path.unlink()
        # Every such launch is counted and removed at once.
        self.state_files("codex-as", "lab", GONE + 1, self.started - 600, "launched", "blocked")
        self.state_files("codex-as", "lab", GONE + 2, self.started - 60, "launched", "seen")
        self.assertTrue(guard_launch.unguarded(self.guard_dir, "codex-as", "lab", self.started))
        self.assertEqual(self.names(), [])

    def test_a_launch_that_still_runs_counts_only_by_its_own_seen(self):
        native = subprocess.Popen(["sleep", "30"])
        self.addCleanup(native.wait)
        self.addCleanup(native.kill)
        # Waiting at the workspace trust dialog, say, while another launch of the account records usage.
        running = self.state_files("claude-as", "personal", native.pid, self.started - 20, "launched")
        self.assertFalse(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", self.started))
        self.assertTrue(Path(running + ".launched").exists())
        # Its own status line saw a reply and no hook has run: it is unguarded while it runs.
        Path(running + ".seen").write_text("")
        self.assertTrue(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", None))
        self.assertEqual(self.names(), [])

    def test_a_launch_of_another_host_counts_only_by_its_own_seen(self):
        # Cluster login nodes share the home: a launch that runs on another node, waiting at the workspace trust
        # dialog, say, has a process id that is none here while another launch of the account records usage.
        other = self.state_files("claude-as", "personal", GONE + 1, self.started - 20, "launched", host="login-2")
        unnamed = self.state_files("claude-as", "personal", GONE + 2, self.started - 20, "launched", host="")
        cut = self.state_files("claude-as", "personal", GONE + 3, self.started - 20, "launched",
                               host=os.uname().nodename + "-2")
        self.assertFalse(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", self.started))
        for prefix in (other, unnamed, cut):
            self.assertTrue(Path(prefix + ".launched").exists(), prefix)
        # Its own status line saw a reply and no hook has run.
        Path(other + ".seen").write_text("")
        self.assertTrue(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", None))
        self.assertFalse(Path(other + ".launched").exists())
        self.assertEqual(self.names(), sorted(os.path.basename(prefix) + ".launched" for prefix in (unnamed, cut)))

    def test_a_launch_whose_process_belongs_to_another_user_still_runs(self):
        # kill(pid, 0) refuses a process of another user with EPERM: such a process runs.
        earlier = self.state_files("claude-as", "personal", GONE + 7, self.started - 3600, "launched")
        with patch.object(guard_launch.os, "kill", another_users(GONE + 7)):
            self.assertFalse(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", self.started))
        self.assertTrue(Path(earlier + ".launched").exists())
        self.assertTrue(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", self.started))

    def test_launches_whose_hooks_ran_or_that_had_no_reply_do_not_count(self):
        ran = [self.state_files("claude-as", "personal", 4000 + index, self.started - 600, "launched", "seen", kind)
               for index, kind in enumerate(("ran.start", "ran.prompt", "ran.tool", "ran.stop"))]
        quiet = self.state_files("claude-as", "personal", 4010, self.started - 600, "launched", "blocked")
        unlaunched = self.state_files("claude-as", "personal", 4011, self.started - 600, "seen")
        others = [self.state_files(tool, account, 4012, self.started - 600, "launched", "seen")
                  for tool, account in (("claude-as", "personal-2"), ("claude-as", "Personal"), ("codex-as", "personal"),
                                        ("claude-as", "work"))]
        before = self.names()
        self.assertFalse(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", None))
        self.assertEqual(self.names(), before)
        self.assertTrue(guard_launch.unguarded(self.guard_dir, "claude-as", "personal-2", None))
        self.assertTrue(all(Path(prefix + ".launched").exists() for prefix in [*ran, quiet, *others[1:]]))
        self.assertTrue(Path(unlaunched + ".seen").exists())
        self.assertFalse(guard_launch.unguarded(self.root / "missing", "claude-as", "personal", None))

    def test_the_current_launch_is_never_counted(self):
        env = {}
        state = guard_launch.prepare(env, self.guard_dir, "claude-as", "personal",
                                     policy("claude-as", "personal", weekly=D(10)),
                                     str(self.accounts / "claude-personal.usage.json"), self.started)
        Path(state + ".seen").write_text("")
        self.assertFalse(guard_launch.unguarded(self.guard_dir, "claude-as", "personal", self.started + 1))
        self.assertTrue(Path(state + ".launched").exists())

    def test_forget_removes_one_launchs_state(self):
        one = self.state_files("codex-as", "lab", 4001, self.started, "launched", "ran.tool", "blocked",
                               "blocked.00ff.tmp")
        two = self.state_files("codex-as", "lab", 4001, self.started + 1, "launched")
        three = self.state_files("codex-as", "lab-4001", 7, self.started, "launched")
        (self.guard_dir / MARKER).write_text("")
        guard_launch.forget(one)
        self.assertEqual(self.names(), sorted([MARKER, os.path.basename(two) + ".launched",
                                               os.path.basename(three) + ".launched"]))
        guard_launch.forget(str(self.root / "missing/codex-as-lab-1-2"))


class Isolation(unittest.TestCase):
    def test_the_guard_reads_only_its_launch_variable(self):
        source = (ROOT / "lib/coding-agent-kit/guard.py").read_text()
        for word in ("XDG_", '"HOME"', "config.py", "CODING_AGENT_KIT_CONFIG", "expanduser", "Path.home"):
            self.assertNotIn(word, source)
        self.assertEqual(guard.VARIABLE, "CODING_AGENT_KIT_GUARD")

    def test_the_launch_support_reads_no_configuration_and_no_environment(self):
        source = (ROOT / "lib/coding-agent-kit/guard_launch.py").read_text()
        for word in ("XDG_", '"HOME"', "config.py", "CODING_AGENT_KIT_CONFIG", "expanduser", "Path.home",
                     "os.environ", "getenv"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
