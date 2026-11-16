"""The usage reserve guard: its hook events and its status line.

Most cases run in process through guard.answer() against launch state and usage
data in a temporary 0700 directory. The command cases run guard.py as the hook
commands the launchers inject, also from a copy of lib/ under a directory whose
name has a space and a quote. No native, provider, credential or network is
involved.
"""

import contextlib
from decimal import Decimal
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load("kit_guard_under_test", "lib/coding-agent-kit/guard.py")
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
    """A guard directory and an accounts directory, both owned and 0700, in a temporary root."""

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o077))
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-guard-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
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
                                           (slot(95, 10080), None, False), (slot(99, 60), slot(99, 1440), False),
                                           (slot(95, 0), slot(95, 300), True)):
            with self.subTest(primary=primary, secondary=secondary):
                transcript = self.rollout([count(NOW - 30, primary, secondary)])
                self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript))[0], 2 if blocks else 0)

    def test_the_newest_snapshot_decides_and_older_sessions_lines_do_not_count(self):
        launch = self.launch("codex-as", weekly="10")
        transcript = self.rollout([count(NOW - 90, slot(95, 10080)), count(NOW - 30, slot(50, 10080))])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(transcript)), OK)
        self.codex_record(launch, slot(95, 10080))
        before = self.rollout([count(STARTED - 3000, slot(20, 10080))])
        self.assertEqual(self.answer("prompt", launch, self.codex_event(before))[0], 2)

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
            code, out, err = self.answer("prompt", launch, self.codex_event(None))
            self.assertEqual((code, out), (2, ""))
            self.assertEqual(err, "codex-as: lab: the usage reserve guard failed (FileNotFoundError).")


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

    def check_script(self, script):
        launch = self.launch(weekly="10")
        self.claude_usage(launch, seven_day=(91, NOW + 3600))
        threshold = reserve.block_text("claude-as", "personal",
                                       reserve.decide(launch.enabled, [reserve.Reading("weekly", 91, NOW + 3600)], NOW),
                                       "prompt", time.time())
        for form, (command, extra) in self.commands(script, "prompt").items():
            with self.subTest(form=form):
                self.assertEqual(self.run_guard(command, extra), OK)
                self.assertEqual(self.run_guard(command, extra, b"not json"), OK)
                code, out, err = self.run_guard(command, {**extra, **self.environ(launch)})
                # The reset is an hour from NOW, which is in the future; only the span depends on the clock.
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err.split("; resets in")[0], threshold.split("; resets in")[0])
                self.assertTrue(err.endswith(STOPPED["prompt"] + RELAUNCH_CLAUDE + "\n"), err)
                self.assertEqual(err.count("\n"), threshold.count("\n") + 1)
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


class Isolation(unittest.TestCase):
    def test_the_guard_reads_only_its_launch_variable(self):
        source = (ROOT / "lib/coding-agent-kit/guard.py").read_text()
        for word in ("XDG_", '"HOME"', "config.py", "CODING_AGENT_KIT_CONFIG", "expanduser", "Path.home"):
            self.assertNotIn(word, source)
        self.assertEqual(guard.VARIABLE, "CODING_AGENT_KIT_GUARD")


if __name__ == "__main__":
    unittest.main()
