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
from unittest.mock import patch


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
# The Codex hook commands of C3, as codex-as injects them.
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

    def launch(self, tool="claude-as", account=None, five_hour=None, weekly=None, statusline=None):
        """A Launch of TOOL with the reserves given as percent strings; each call gets its own state prefix."""
        account = account or ("personal" if tool == "claude-as" else "lab")
        self.launches += 1
        enabled = {window: D(value) for window, value in (("five_hour", five_hour), ("weekly", weekly))
                   if value is not None}
        if tool == "claude-as":
            usage, marker = self.accounts / f"claude-{account}.usage.json", None
        else:
            usage, marker = self.accounts / f"codex-{account}.json", str(self.guard_dir / "codex-hooks-90aeb3ec484a2ca6")
        state = self.guard_dir / f"{tool}-{account}-{4000 + self.launches}-{STARTED}"
        return guard.Launch(tool, account, enabled, STARTED, str(usage), str(state), marker, statusline)

    def environ(self, launch, **changes):
        """The CODING_AGENT_KIT_GUARD value of LAUNCH, as C1 writes it, with CHANGES."""
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

    def rollout(self, records, name="rollout.jsonl"):
        """A session rollout: its session_meta line from before the launch, then RECORDS."""
        path = self.root / name
        meta = line(STARTED - 3600, "session_meta", {"id": "019a0000-0000-7000-8000-00000000000a",
                                                     "timestamp": iso(STARTED - 3600), "cli_version": "0.169.0"})
        path.write_text("".join(json.dumps(record) + "\n" for record in [meta, *records]))
        return path

    def codex_event(self, transcript, kind="prompt"):
        event = {"session_id": "019a0000-0000-7000-8000-00000000000a", "cwd": "/",
                 "hook_event_name": {"prompt": "UserPromptSubmit", "tool": "PreToolUse"}.get(kind, "Stop"),
                 "transcript_path": None if transcript is None else str(transcript)}
        if kind == "prompt":
            event["prompt"] = "hello"
        return event

    def answer(self, kind, launch, event=None, now=NOW):
        return guard.answer(kind, {} if event is None else event, launch, now)


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
        # After a STOP_TURN deny no Stop hook runs for that turn (R3), so the denying tool event records the reply.
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
            "(the status line has reported no rate limits: the workspace is not trusted yet, or this account reports "
            "none).\nThis prompt was not sent.\nTo go on without reserves, exit and run:\n"
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
        self.codex_record(launch, slot(95, 10080))
        # The session's snapshot, not the account record from earlier runs, decides while there is one,
        # also when the snapshot is of another limit family.
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
                Path(launch.usage).unlink(missing_ok=True)
                # Nothing the guard can judge by: the session is blind, as after a token count without rate limits.
                for kind in ("prompt", "tool"):
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)),
                                     (2, "", reserve.block_text("codex-as", "lab", reserve.Decision(False, "blind"),
                                                                kind, NOW)))
                # The account record from earlier runs still decides its threshold.
                self.codex_record(launch, slot(95, 10080))
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
        # The account record from earlier runs, too.
        self.codex_record(launch, slot(100.5, 10080))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(self.rollout([]))), exhausted("weekly", "prompt"))

    def test_a_window_the_snapshot_does_not_name_comes_from_the_account_record(self):
        launch = self.launch("codex-as", five_hour="10", weekly="10")
        self.codex_record(launch, slot(95, 10080))
        # window_minutes 0 occurs (R16): the snapshot's weekly slot is unusable, or absent, and the record has it.
        for secondary in (slot(95, 0), None):
            transcript = self.rollout([reply(NOW - 30), count(NOW - 30, slot(20, 300), secondary)])
            for kind in ("prompt", "tool"):
                with self.subTest(secondary=secondary, kind=kind):
                    self.assertEqual(self.answer(kind, launch, self.codex_event(transcript, kind)),
                                     (2, "", self.threshold(kind)))
        # Where the snapshot names the window, the snapshot decides.
        both = self.rollout([reply(NOW - 30), count(NOW - 30, slot(20, 300), slot(50, 10080))])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(both)), OK)
        # Another limit family's snapshot is the limit the session's requests count against: the general record's
        # windows stay out, also those the snapshot does not name.
        spark = self.rollout([reply(NOW - 30), count(NOW - 30, slot(20, 300), None, limit_id="spark")])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(spark)), OK)
        # A record window without a reset time is no reading here either.
        self.codex_record(launch, slot(95, 10080, None))
        partial = self.rollout([reply(NOW - 30), count(NOW - 30, slot(20, 300), slot(95, 0))])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(partial)), OK)

    def test_without_a_snapshot_the_account_record_decides_minus_readings_without_a_reset(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.codex_record(launch, slot(95, 10080, None))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.codex_record(launch, slot(95, 10080, NOW - 1))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.codex_record(launch, slot(20, 300), slot(95, 10080))
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), (2, "", self.threshold("prompt")))
        self.assertEqual(self.answer("tool", launch, self.codex_event(transcript, "tool")),
                         (2, "", self.threshold("tool")))

    def test_an_unusable_record_is_no_reading(self):
        launch = self.launch("codex-as", weekly="10")
        for content, mode in (("garbage", 0o600), ('{"usage": 7}', 0o600), ('{"usage": {"primary": 7}}', 0o600),
                              (json.dumps({"usage": {"primary": slot(95, 10080)}}), 0o640)):
            with self.subTest(content=content):
                put(Path(launch.usage), content, mode)
                self.assertEqual(self.answer("prompt", launch, self.codex_event(None)), OK)

    def test_token_counts_without_rate_limits_are_blind_on_both_events(self):
        launch = self.launch("codex-as", weekly="10")
        self.codex_record(launch, slot(20, 10080))
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

    def test_a_stop_event_only_records_itself(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([count(NOW - 30, slot(95, 10080))])
        for end_at_stop in (True, False):
            with patch.object(guard, "END_AT_STOP", end_at_stop):
                self.assertEqual(self.answer("stop", launch, self.codex_event(transcript, "stop")), OK)
        self.assertTrue(self.state(launch, "ran.stop").is_file())
        self.assertFalse(Path(launch.marker).exists())
        self.assertFalse(self.state(launch, "blocked").exists())


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
        self.codex_record(launch, slot(95, 10080, int(time.time()) + 3600))
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

    def test_unchanged_readings_are_rewritten_only_after_record_again(self):
        launch = self.launch(weekly="10")
        self.assertTrue(guard.record(launch.usage, limits(), NOW))
        self.assertFalse(guard.record(launch.usage, limits(), NOW + 299))
        self.assertEqual(self.usage_file(launch)["seen_at"], NOW)
        self.assertTrue(guard.record(launch.usage, limits(), NOW + 300))
        self.assertEqual(self.usage_file(launch)["seen_at"], NOW + 300)
        self.assertTrue(guard.record(launch.usage, limits((43, NOW + 3600)), NOW + 301))
        self.assertEqual(self.usage_file(launch)["five_hour"], window(43, NOW + 3600))

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

    def test_two_status_lines_at_once_keep_the_newer_reading(self):
        newer, older = self.launch(weekly="10"), self.launch(weekly="10")
        lock = self.accounts / f".{Path(newer.usage).name}.lock"
        accounts = guard.module("accounts")
        real_write = accounts.write
        # On a network home directory the lock file's time is the server's clock, however far from this host's.
        for skew in (0, 20, -20):
            with self.subTest(skew=skew):
                put(Path(newer.usage), {"five_hour": window(50, NOW + 3600), "seven_day": None, "seen_at": NOW - 1000})
                writing = threading.Event()

                def slow_write(path, data):
                    # The older session's run writes slowly, so the newer one's run comes while it holds its reading.
                    if threading.current_thread().name == "older":
                        os.utime(lock, (time.time() + skew,) * 2)
                        writing.set()
                        time.sleep(0.3)
                    return real_write(path, data)

                with patch.object(accounts, "write", slow_write):
                    first = threading.Thread(target=guard.record, args=(older.usage, limits((60, NOW + 3600), None), NOW),
                                             name="older")
                    first.start()
                    self.assertTrue(writing.wait(10))
                    self.assertTrue(guard.record(newer.usage, limits((91, NOW + 3600), None), NOW))
                    first.join(10)
                self.assertEqual(self.usage_file(newer)["five_hour"], window(91, NOW + 3600))
                self.assertEqual([path.name for path in self.accounts.iterdir()], [Path(newer.usage).name],
                                 "a lock was left")

    def test_a_run_that_read_before_another_wrote_reads_again_under_the_lock(self):
        newer, older = self.launch(weekly="10"), self.launch(weekly="10")
        put(Path(newer.usage), {"five_hour": window(50, NOW + 3600), "seven_day": None, "seen_at": NOW - 1000})
        real, read, written, results = guard.merged_record, threading.Event(), threading.Event(), []

        def merged_record(path, incoming, now):
            result = real(path, incoming, now)
            if threading.current_thread().name == "older" and not read.is_set():
                # The older session's run has read the stored 50% and not taken the lock yet; the newer run writes now.
                read.set()
                written.wait(10)
            return result

        with patch.object(guard, "merged_record", merged_record):
            first = threading.Thread(target=lambda: results.append(guard.record(older.usage, limits((60, NOW + 3600), None),
                                                                                NOW)), name="older")
            first.start()
            self.assertTrue(read.wait(10))
            self.assertTrue(guard.record(newer.usage, limits((91, NOW + 3600), None), NOW))
            written.set()
            first.join(10)
        self.assertEqual(results, [False])
        self.assertEqual(self.usage_file(newer)["five_hour"], window(91, NOW + 3600))
        self.assertEqual([path.name for path in self.accounts.iterdir()], [Path(newer.usage).name], "a lock was left")

    def test_a_run_never_removes_a_lock_that_is_no_longer_its_own(self):
        launch = self.launch(weekly="10")
        name, held = guard.lock(Path(launch.usage))
        self.assertEqual(name, self.accounts / f".{Path(launch.usage).name}.lock")
        # Another run took this lock for one a cancelled run left, removed it and holds its own in its place.
        name.unlink()
        put(name, "another run's lock")
        guard.unlock(name, held)
        self.assertEqual(name.read_text(), "another run's lock")
        name.unlink()
        name, held = guard.lock(Path(launch.usage))
        guard.unlock(name, held)
        self.assertEqual(list(self.accounts.iterdir()), [])

    def lock_file(self, launch, content=None, host=None, pid=None, at=None):
        """The usage file's lock as a run that holds it writes it: its host, process id and clock; or CONTENT."""
        if content is None:
            content = json.dumps({"host": os.uname().nodename if host is None else host,
                                  "pid": os.getpid() if pid is None else pid, "at": int(time.time()) if at is None else at})
        return put(self.accounts / f".{Path(launch.usage).name}.lock", content)

    def test_a_lock_names_its_holder(self):
        launch = self.launch(weekly="10")
        before = int(time.time())
        name, held = guard.lock(Path(launch.usage))
        after = time.time()
        try:
            info = name.lstat()
            self.assertEqual((stat.S_ISREG(info.st_mode), stat.S_IMODE(info.st_mode)), (True, 0o600))
            content = json.loads(name.read_text())
            self.assertEqual(sorted(content), ["at", "host", "pid"])
            self.assertEqual((content["host"], content["pid"]), (os.uname().nodename, os.getpid()))
            self.assertTrue(before <= content["at"] <= after, content)
        finally:
            guard.unlock(name, held)
        self.assertFalse(name.exists())

    def test_the_usage_file_lock(self):
        launch = self.launch(weekly="10")
        put(self.state(launch, "ran.start"), "")
        lock = self.lock_file(launch)
        with patch.object(guard, "LOCK_WAIT", 0.2):
            with self.assertRaises(TimeoutError):
                guard.record(launch.usage, limits(), NOW)
            self.assertEqual(guard.status(status_input(), launch, NOW), "")
        for name in (launch.usage, launch.state + ".seen"):
            self.assertFalse(Path(name).exists(), name)
        self.assertTrue(lock.exists(), "another writer's lock was taken")
        # What the holder wrote decides, never the file's time, which on a network home is the server's clock.
        now = int(time.time())
        held = {"a run of this host, the file's time 20 seconds ahead": {"mtime": 20},
                "a run of this host, the file's time 20 seconds behind": {"mtime": -20},
                "a run of this host, an hour old by the file's time": {"mtime": -3600},
                "a run of another host whose process id is none here": {"host": "another host", "pid": GONE},
                "a run of another host, within LOCK_STALE of this clock": {"host": "another host",
                                                                           "at": now - guard.LOCK_STALE + 3},
                "a run that has not written its lock yet": {"content": ""}}
        for case, holder in held.items():
            with self.subTest(case), patch.object(guard, "LOCK_WAIT", 0.2):
                mtime = holder.pop("mtime", 0)
                lock = self.lock_file(launch, **holder)
                os.utime(lock, (time.time() + mtime,) * 2)
                with self.assertRaises(TimeoutError):
                    guard.record(launch.usage, limits(), NOW)
                self.assertTrue(lock.exists())
                self.assertFalse(Path(launch.usage).exists())
        # A lock that a run left when it ended is removed: its clock further than LOCK_STALE from this one's, a
        # process of this host that has ended, or no holder named for LOCK_EMPTY seconds.
        left = {"a clock LOCK_STALE behind": {"host": "another host", "at": now - guard.LOCK_STALE - 5},
                "a clock LOCK_STALE ahead": {"host": "another host", "at": now + guard.LOCK_STALE + 5},
                "a process of this host that has ended": {"pid": GONE},
                "nothing written": {"content": ""}, "not JSON": {"content": "held"},
                "bytes that are not UTF-8": {"content": "\udcff"}, "a list": {"content": "[1]"},
                "no host": {"content": json.dumps({"pid": os.getpid(), "at": now})},
                "a process id of 0": {"pid": 0}, "a negative process id": {"pid": -1},
                "a clock that is no whole number": {"at": now + 0.5}, "a clock as text": {"at": str(now)}}
        for case, holder in left.items():
            with self.subTest(case), patch.object(guard, "LOCK_WAIT", 0.2), patch.object(guard, "LOCK_EMPTY", 0.05):
                Path(launch.usage).unlink(missing_ok=True)
                if holder.get("content") == "\udcff":
                    lock.write_bytes(b"\xff")
                else:
                    lock = self.lock_file(launch, **holder)
                self.assertTrue(guard.record(launch.usage, limits(), NOW))
                self.assertEqual(sorted(self.accounts.iterdir()), [Path(launch.usage)], "a lock or its remains were left")
        # Unchanged readings need no lock.
        self.lock_file(launch)
        with patch.object(guard, "LOCK_WAIT", 0.2):
            self.assertFalse(guard.record(launch.usage, limits(), NOW + 10))
        self.assertEqual((guard.LOCK_WAIT, guard.LOCK_STALE, guard.LOCK_EMPTY), (2, 10, 1))

    def test_a_lock_of_a_process_of_another_user_is_held(self):
        # kill(pid, 0) refuses a process of another user with EPERM: such a process runs, and its lock holds.
        launch = self.launch(weekly="10")
        lock = self.lock_file(launch, pid=GONE + 7)
        with patch.object(guard.os, "kill", another_users(GONE + 7)), patch.object(guard, "LOCK_WAIT", 0.2), \
                self.assertRaises(TimeoutError):
            guard.lock(Path(launch.usage))
        self.assertTrue(lock.exists())
        name, held = guard.lock(Path(launch.usage))  # unpatched, that process has ended
        guard.unlock(name, held)

    def test_a_lock_that_took_the_place_of_a_stale_one_meanwhile_is_never_moved(self):
        launch = self.launch(weekly="10")
        lock = self.lock_file(launch, host="another host", at=int(time.time()) - 60)
        fresh = json.dumps({"host": "another host", "pid": 7, "at": int(time.time())})
        real, taken = guard.discard, []

        def discard(name, identity, seen):
            # Another run judged the same lock stale, removed it and took the lock before this run breaks it.
            name.unlink()
            put(name, fresh)
            taken.append(name.lstat().st_ino)
            # A third run comes now, and finds the lock held.
            with patch.object(guard, "LOCK_WAIT", 0.1), self.assertRaises(TimeoutError):
                guard.lock(Path(launch.usage))
            return real(name, identity, seen)

        with patch.object(guard, "discard", discard), patch.object(guard, "LOCK_WAIT", 0.2), \
                patch.object(guard.os, "rename", side_effect=AssertionError("a lock was moved")):
            with self.assertRaises(TimeoutError):
                guard.record(launch.usage, limits(), NOW)
        self.assertEqual((lock.read_text(), lock.lstat().st_ino), (fresh, taken[0]))
        self.assertEqual(list(self.accounts.iterdir()), [lock])

    def test_another_hosts_clock_counts_in_whole_seconds(self):
        # A lock holds its taker's clock in whole seconds, up to a second behind: a lock of another host is held
        # for LOCK_STALE seconds of this clock whatever fraction of a second it was taken at.
        launch = self.launch(weekly="10")
        at = int(time.time()) - 3600
        lags = {guard.LOCK_STALE + 0.999: False, -guard.LOCK_STALE - 0.999: False,
                guard.LOCK_STALE + 1.001: True, -guard.LOCK_STALE - 1.001: True}
        for lag, stale in lags.items():
            with self.subTest(lag=lag):
                lock = self.lock_file(launch, host="another host", pid=7, at=at)
                with patch.object(guard.time, "time", return_value=at + lag), patch.object(guard, "LOCK_WAIT", 0.1):
                    if stale:
                        name, fd = guard.lock(Path(launch.usage))
                        guard.unlock(name, fd)
                    else:
                        with self.assertRaises(TimeoutError):
                            guard.lock(Path(launch.usage))
                self.assertEqual(lock.exists(), not stale)

    def test_a_stale_lock_is_broken_by_one_run_at_a_time(self):
        launch = self.launch(weekly="10")
        lock = self.lock_file(launch, pid=GONE)
        breaker = self.accounts / (lock.name + ".break")
        # Another run breaks it now: this run waits for that run instead of breaking it too.
        put(breaker, "")
        with patch.object(guard, "LOCK_WAIT", 0.2), self.assertRaises(TimeoutError):
            guard.lock(Path(launch.usage))
        self.assertEqual(sorted(self.accounts.iterdir()), [lock, breaker])
        # A breaker that stays LOCK_EMPTY seconds was left by a run that ended while it held it: it is removed, and
        # then the stale lock.
        with patch.object(guard, "LOCK_WAIT", 1), patch.object(guard, "LOCK_EMPTY", 0.05):
            name, held = guard.lock(Path(launch.usage))
        self.assertEqual(json.loads(name.read_text())["pid"], os.getpid())
        guard.unlock(name, held)
        self.assertEqual(list(self.accounts.iterdir()), [])
        # A breaker is held only to check the lock and remove it: none is left behind.
        lock = self.lock_file(launch, pid=GONE)
        name, held = guard.lock(Path(launch.usage))
        self.assertEqual(sorted(self.accounts.iterdir()), [name])
        guard.unlock(name, held)

    def test_a_run_cancelled_while_it_holds_the_lock_lets_it_go_first(self):
        launch = self.launch(weekly="10")
        put(self.state(launch, "ran.start"), "")
        lock, writing = self.accounts / f".{Path(launch.usage).name}.lock", self.scripts / "writing"
        # The run writes the usage file slowly (a slow fsync), so the cancel comes while it holds the lock.
        runner = self.runner("accounts = guard.module('accounts')\n"
                             "real = accounts.write\n"
                             "def write(path, data):\n"
                             f"    open({str(writing)!r}, 'w').close()\n"
                             "    time.sleep(1)\n"
                             "    return real(path, data)\n"
                             "accounts.write = write\n")
        for signum, code in ((signal.SIGTERM, -signal.SIGTERM), (signal.SIGHUP, -signal.SIGHUP), (signal.SIGINT, 0)):
            with self.subTest(signal=signum.name):
                Path(launch.usage).unlink(missing_ok=True)
                writing.unlink(missing_ok=True)
                run = self.run_runner(runner, launch)
                self.addCleanup(run.kill)
                run.stdin.write(status_input())
                run.stdin.close()
                run.stdin = None  # communicate() of Python 3.11 and 3.12 would flush the closed stream
                deadline = time.monotonic() + 30
                while not writing.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(lock.exists())
                run.send_signal(signum)
                out, err = run.communicate(timeout=30)
                self.assertEqual((run.returncode, out, err), (code, b"", b""))
                self.assertFalse(lock.exists(), "a cancelled run left its lock")
                self.assertEqual(self.usage_file(launch)["five_hour"], window(42, NOW + 3600))
                self.assertEqual(sorted(self.accounts.iterdir()), [Path(launch.usage)])

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
                    name, held = guard.lock(Path(launch.usage))
                    lock_mode = stat.S_IMODE(name.lstat().st_mode)
                    guard.unlock(name, held)
                    decision = reserve.decide(launch.enabled, [reserve.Reading("weekly", 95, NOW + 86400)], NOW)
                    self.assertEqual(self.answer("prompt", launch),
                                     (2, "", reserve.block_text("claude-as", "personal", decision, "prompt", NOW)))
                finally:
                    os.umask(previous)
                self.assertEqual(lock_mode, 0o600)
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
