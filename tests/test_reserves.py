"""Usage reserves: launch flags, per-window resolution, the guard's decision and its messages.

Everything runs in process: reserve.py is pure (no I/O, no environment), and
config.py runs in a temporary HOME. No provider, credential or network is involved.
"""

import ast
import dataclasses
from decimal import Decimal
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reserve = load_module("kit_reserve_under_test", "lib/coding-agent-kit/reserve.py")
config = load_module("kit_config_for_reserves", "lib/coding-agent-kit/config.py")

D = Decimal
SPEC_RESERVES = ('[usage_reserve]\nfive_hour = 20\nweekly = 30\n'
                 '[usage_reserve.codex_accounts.borrowed]\nweekly = 40\n'
                 '[usage_reserve.claude_accounts.borrowed]\nweekly = 40\n')


def layers(text):
    """config.UsageReserve from a configuration file in a temporary HOME."""
    with tempfile.TemporaryDirectory(prefix="kit-reserve-test-") as top:
        home = Path(top).resolve()
        file = home / ".config/coding-agent-kit/config.toml"
        file.parent.mkdir(parents=True)
        for directory in (file.parent, file.parent.parent):
            directory.chmod(0o700)  # whatever the umask: a shared group (macOS staff) may not write them
        file.write_text(text)
        file.chmod(0o600)
        with patch.dict(os.environ, {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}, clear=True):
            loaded = config.load()
    return loaded.usage_reserve


def policy(five=None, weekly=None, tool="codex-as", account="borrowed"):
    """A policy whose enabled windows come from the invocation layer."""
    return reserve.resolve(tool, account, reserve.Overrides(five, weekly), config.UsageReserve())


class Flags(unittest.TestCase):
    def test_without_reserve_flags_the_arguments_are_untouched(self):
        for args in ([], ["A"], ["A", "--reserve-5h", "10"], ["--list"], ["-c"], ["--reserve", "10", "A"],
                     ["--reserve-5H", "10", "A"], ["--reserve-5h-x", "A"], ["--reserve-weekly5", "A"], ["", "A"]):
            with self.subTest(args=args):
                original = list(args)
                overrides, rest = reserve.split_flags(args)
                self.assertEqual((overrides, rest, args), (reserve.Overrides(), original, original))

    def test_both_flags_in_either_order_before_name(self):
        for args in (["--reserve-5h", "10", "--reserve-weekly", "40", "A", "--reserve-5h", "1"],
                     ["--reserve-weekly", "40", "--reserve-5h", "10", "A", "--reserve-5h", "1"]):
            with self.subTest(args=args):
                original = list(args)
                self.assertEqual(reserve.split_flags(args),
                                 (reserve.Overrides(D(10), D(40)), ["A", "--reserve-5h", "1"]))
                self.assertEqual(args, original)  # the caller's list is not changed
        self.assertEqual(reserve.split_flags(["--reserve-weekly", "off", "A"]), (reserve.Overrides(None, "off"), ["A"]))

    def test_values_are_ascii_decimals_from_0_through_100_or_off(self):
        for text, value in (("0", D(0)), ("100", D(100)), ("12.5", D("12.5")), ("0.0", D("0.0")), ("007", D(7)),
                            ("100.000", D("100.000")), ("off", "off")):
            with self.subTest(text=text):
                self.assertEqual(reserve.split_flags(["--reserve-5h", text, "A"])[0], reserve.Overrides(value, None))
        message = "--reserve-weekly takes a number from 0 through 100, or off, as the next argument."
        for text in ("101", "100.01", "-1", "+1", "1e2", "nan", "inf", "1_0", " 1", "1 ", "1\n", "", ".5", "5.",
                     "OFF", "Off", "0x10", "١٢", "１", "A", "--reserve-5h"):
            with self.subTest(text=text), self.assertRaises(reserve.ReserveError) as caught:
                reserve.split_flags(["--reserve-weekly", text, "A"])
            self.assertEqual(str(caught.exception), message)  # never echoes the value
            self.assertTrue(caught.exception.own_message)
        with self.assertRaises(reserve.ReserveError) as caught:
            reserve.split_flags(["--reserve-weekly"])
        self.assertEqual(str(caught.exception), message)

    def test_duplicates_equals_forms_and_a_missing_name_are_errors(self):
        cases = {
            ("--reserve-5h", "1", "--reserve-5h", "2", "A"): "--reserve-5h is given twice.",
            ("--reserve-weekly", "off", "--reserve-5h", "1", "--reserve-weekly", "off", "A"):
                "--reserve-weekly is given twice.",
            ("--reserve-5h=10", "A"): "--reserve-5h=VALUE is not accepted; write --reserve-5h VALUE.",
            ("--reserve-5h", "1", "--reserve-weekly=", "A"):
                "--reserve-weekly=VALUE is not accepted; write --reserve-weekly VALUE.",
            ("--reserve-5h", "10"): "an account name must follow --reserve-5h and --reserve-weekly.",
            ("--reserve-weekly", "10", "--list"): "an account name must follow --reserve-5h and --reserve-weekly.",
            ("--reserve-weekly", "10", "-c"): "an account name must follow --reserve-5h and --reserve-weekly.",
            ("--reserve-weekly", "10", "--reserve", "1", "A"):
                "an account name must follow --reserve-5h and --reserve-weekly.",
        }
        for args, message in cases.items():
            with self.subTest(args=args), self.assertRaises(reserve.ReserveError) as caught:
                reserve.split_flags(list(args))
            self.assertEqual(str(caught.exception), message)


class Resolution(unittest.TestCase):
    def test_precedence_per_window_invocation_account_global_default(self):
        usage = layers(SPEC_RESERVES)
        resolved = reserve.resolve("codex-as", "borrowed", reserve.Overrides(D(10), None), usage)
        self.assertEqual(resolved.settings(), (reserve.Setting("five_hour", D(10), "invocation"),
                                               reserve.Setting("weekly", D(40), "account")))
        # The next ordinary launch: the override is gone, nothing was saved.
        resolved = reserve.resolve("codex-as", "borrowed", reserve.Overrides(), usage)
        self.assertEqual(resolved.settings(), (reserve.Setting("five_hour", D(20), "global"),
                                               reserve.Setting("weekly", D(40), "account")))
        self.assertEqual(usage, layers(SPEC_RESERVES))
        resolved = reserve.resolve("claude-as", "other", reserve.Overrides(None, D(5)), usage)
        self.assertEqual(resolved.settings(), (reserve.Setting("five_hour", D(20), "global"),
                                               reserve.Setting("weekly", D(5), "invocation")))
        resolved = reserve.resolve("claude-as", "other", reserve.Overrides(), config.UsageReserve())
        self.assertEqual(resolved.settings(), (reserve.Setting("five_hour", None, "default"),
                                               reserve.Setting("weekly", None, "default")))
        self.assertTrue(resolved.off)
        self.assertEqual(resolved.enabled(), {})

    def test_accounts_are_keyed_by_launcher(self):
        usage = layers('[usage_reserve.codex_accounts.shared]\nfive_hour = 7\n')
        codex = reserve.resolve("codex-as", "shared", reserve.Overrides(), usage)
        claude = reserve.resolve("claude-as", "shared", reserve.Overrides(), usage)
        self.assertEqual((codex.five_hour, claude.five_hour),
                         (reserve.Setting("five_hour", D(7), "account"), reserve.Setting("five_hour", None, "default")))
        with self.assertRaises(ValueError):
            reserve.resolve("codex", "shared", reserve.Overrides(), usage)

    def test_explicit_off_overrides_and_omission_inherits(self):
        usage = layers('[usage_reserve]\nfive_hour = 20\nweekly = 30\n'
                       '[usage_reserve.codex_accounts.a]\nfive_hour = "off"\n'
                       '[usage_reserve.codex_accounts.b]\n')
        a = reserve.resolve("codex-as", "a", reserve.Overrides(), usage)
        self.assertEqual(a.settings(), (reserve.Setting("five_hour", None, "account"),
                                        reserve.Setting("weekly", D(30), "global")))
        self.assertEqual(a.enabled(), {"weekly": D(30)})
        b = reserve.resolve("codex-as", "b", reserve.Overrides(None, "off"), usage)
        self.assertEqual(b.settings(), (reserve.Setting("five_hour", D(20), "global"),
                                        reserve.Setting("weekly", None, "invocation")))
        both = reserve.resolve("codex-as", "b", reserve.Overrides("off", "off"), usage)
        self.assertTrue(both.off)
        # A configured "off" everywhere is the same as no configuration: protection stays off.
        off = layers('[usage_reserve]\nfive_hour = "off"\nweekly = "off"\n')
        self.assertTrue(reserve.resolve("claude-as", "x", reserve.Overrides(), off).off)

    def test_zero_is_an_enabled_reserve(self):
        resolved = reserve.resolve("codex-as", "a", reserve.Overrides(D(0), None), config.UsageReserve())
        self.assertFalse(resolved.off)
        self.assertEqual(resolved.enabled(), {"five_hour": D(0)})

    def test_describe_names_each_window_and_its_source(self):
        usage = layers(SPEC_RESERVES)
        resolved = reserve.resolve("codex-as", "borrowed", reserve.Overrides(D("12.50"), None), usage)
        self.assertEqual(resolved.describe(), "usage reserves for borrowed: five-hour 12.5% (--reserve-5h), "
                                              "weekly 40% (usage_reserve.codex_accounts.borrowed)")
        resolved = reserve.resolve("claude-as", "x", reserve.Overrides(None, "off"), usage)
        self.assertEqual(resolved.describe(), "usage reserves for x: five-hour 20% (usage_reserve), "
                                              "weekly off (--reserve-weekly)")
        resolved = reserve.resolve("claude-as", "x", reserve.Overrides(), config.UsageReserve())
        self.assertEqual(resolved.describe(), "usage reserves for x: five-hour off (default), weekly off (default)")

    def test_values_are_checked_again_when_resolved(self):
        for bad in (D(101), D(-1), D("NaN"), 50, 50.0, True, "on", "OFF"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                reserve.resolve("codex-as", "a", reserve.Overrides(bad, None), config.UsageReserve())


NOW = 1_800_000_000  # 2027-01-15T08:00:00Z


class Decide(unittest.TestCase):
    def reading(self, window, used, resets_in=3600, family=None):
        return reserve.Reading(window, used, None if resets_in is None else NOW + resets_in, family)

    def test_blocks_at_and_below_the_reserve_exactly(self):
        for used, held, allow in ((89.9, "10", True), (90, "10", False), (99.99, "0", True), (100, "0", False),
                                  (0, "100", False), (87.5, "12.5", False), (87.4, "12.5", True)):
            decision = reserve.decide({"weekly": D(held)}, (self.reading("weekly", used),), NOW)
            self.assertEqual((decision.allow, decision.reason), (allow, None if allow else "threshold"), (used, held))

    def test_a_reading_expires_at_its_reset(self):
        for resets_in, allow in ((1, False), (0, True), (-1, True)):
            self.assertEqual(reserve.decide({"weekly": D(10)}, (self.reading("weekly", 95, resets_in),), NOW).allow, allow)

    def test_a_reading_without_a_reset_is_current(self):
        self.assertFalse(reserve.decide({"weekly": D(10)}, (self.reading("weekly", 95, None),), NOW).allow)

    def test_only_enabled_windows_with_a_current_reading_count(self):
        readings = (self.reading("weekly", 100), self.reading("five_hour", 10))
        self.assertTrue(reserve.decide({"five_hour": D(20)}, readings, NOW).allow)
        self.assertTrue(reserve.decide({"five_hour": D(20), "weekly": D(20)}, readings[1:], NOW).allow)

    def test_over_100_counts_as_100_and_bad_numbers_are_ignored(self):
        decision = reserve.decide({"weekly": D(0)}, (self.reading("weekly", 105),), NOW)
        self.assertEqual((decision.allow, decision.windows[0].remaining), (False, D(0)))
        for bad in (-1, "90", True, None, float("nan"), float("inf")):
            self.assertTrue(reserve.decide({"weekly": D(10)}, (self.reading("weekly", bad),), NOW).allow, bad)

    def test_the_most_used_reading_of_a_window_decides(self):
        readings = (self.reading("weekly", 50), self.reading("weekly", 95, family="other"))
        decision = reserve.decide({"weekly": D(10)}, readings, NOW)
        self.assertEqual((decision.allow, decision.windows[0].family), (False, "other"))

    def test_blind_blocks_alone_and_threshold_wins(self):
        self.assertEqual(reserve.decide({"weekly": D(10)}, (), NOW, blind=True).reason, "blind")
        self.assertEqual(reserve.decide({"weekly": D(10)}, (self.reading("weekly", 95),), NOW, blind=True).reason, "threshold")
        self.assertTrue(reserve.decide({"weekly": D(10)}, (), NOW).allow)

    def test_windows_at_their_reserve_come_in_window_order_with_their_values(self):
        readings = (self.reading("weekly", D("91.75"), 7200, "spark"), self.reading("five_hour", 96, None))
        decision = reserve.decide({"weekly": D("8.5"), "five_hour": D(5)}, readings, NOW)
        self.assertEqual(decision, reserve.Decision(False, "threshold", (
            reserve.WindowStatus("five_hour", D(5), reserve.AT_RESERVE, D(4), None),
            reserve.WindowStatus("weekly", D("8.5"), reserve.AT_RESERVE, D("8.25"), NOW + 7200, "spark"))))
        self.assertEqual(reserve.decide({"weekly": D(10)}, (self.reading("weekly", 10),), NOW),
                         reserve.Decision(True, None))

    def test_readings_may_be_any_iterable(self):
        readings = (self.reading("five_hour", 10), self.reading("weekly", 95))
        enabled = {"five_hour": D(10), "weekly": D(10)}
        expected = reserve.decide(enabled, readings, NOW)
        self.assertEqual(expected.reason, "threshold")
        self.assertEqual(reserve.decide(enabled, (reading for reading in readings), NOW), expected)
        self.assertEqual(reserve.decide(enabled, iter(list(readings)), NOW), expected)

    def test_unusable_reset_times_and_windows_are_ignored(self):
        for resets_at in ("soon", True, float("nan")):
            reading = reserve.Reading("weekly", 95, resets_at)
            self.assertTrue(reserve.decide({"weekly": D(10)}, (reading,), NOW).allow, resets_at)
        self.assertTrue(reserve.decide({"weekly": D(10)}, (reserve.Reading("daily", 100, None),), NOW).allow)


class Messages(unittest.TestCase):
    def test_threshold(self):
        status = reserve.WindowStatus("weekly", D(10), reserve.AT_RESERVE, D(9), NOW + 183600)
        self.assertEqual(reserve.block_text("claude-as", "personal", reserve.Decision(False, "threshold", (status,)), "prompt", NOW),
            "claude-as: personal: weekly window 9% remaining, at or below its 10% reserve; resets in 2d 3h (2027-01-17T11:00:00Z).\n"
            "This prompt was not sent.\nTo use the reserve, exit and run:\n  claude-as --reserve-weekly off personal --continue")

    def test_two_windows_a_family_and_no_reset(self):
        statuses = (reserve.WindowStatus("five_hour", D(5), reserve.AT_RESERVE, D(0), None),
                    reserve.WindowStatus("weekly", D("12.5"), reserve.AT_RESERVE, D("3.25"), NOW + 3900, "spark"))
        self.assertEqual(reserve.block_text("codex-as", "lab", reserve.Decision(False, "threshold", statuses), "tool", NOW),
            "codex-as: lab: five-hour window 0% remaining, at or below its 5% reserve. weekly window 3.2% remaining, "
            "at or below its 12.5% reserve; resets in 1h 5m (2027-01-15T09:05:00Z) (limit spark).\n"
            "This tool call was stopped. Stop working now and tell the user why.\nTo use the reserve, exit and run:\n"
            "  codex-as --reserve-5h off --reserve-weekly off lab resume --last")

    def test_blind_and_failure(self):
        self.assertEqual(reserve.block_text("codex-as", "lab", reserve.Decision(False, "blind"), "prompt", NOW),
            "codex-as: lab: no usage data since this session started, so its usage reserve cannot be checked "
            "(the session's rollout shows no rate limits).\nThis prompt was not sent.\nTo go on without reserves, exit and run:\n"
            "  codex-as --reserve-5h off --reserve-weekly off lab resume --last\n"
            'To keep them off for this account, set them to "off" under [usage_reserve.codex_accounts.lab].')
        self.assertEqual(reserve.failure_text("claude-as", "personal", "tool", "KeyError"),
            "claude-as: personal: the usage reserve guard failed (KeyError).\n"
            "This tool call was stopped. Stop working now and tell the user why.\nTo go on without reserves, exit and run:\n"
            "  claude-as --reserve-5h off --reserve-weekly off personal --continue")

    def test_span(self):
        self.assertEqual([reserve.span(s) for s in (0, 59, 2700, 11100, 183600)], ["0m", "0m", "45m", "3h 5m", "2d 3h"])

    def test_claude_blindness_and_the_stop_event(self):
        self.assertEqual(reserve.block_text("claude-as", "lab", reserve.Decision(False, "blind"), "stop", NOW),
            "claude-as: lab: no usage data since this session started, so its usage reserve cannot be checked "
            "(the status line has reported no rate limits: this account reports none, or a server-managed or "
            "registry policy replaced the status line).\n"
            "The work stops here.\nTo go on without reserves, exit and run:\n"
            "  claude-as --reserve-5h off --reserve-weekly off lab --continue\n"
            'To keep them off for this account, set them to "off" under [usage_reserve.claude_accounts.lab].')
        self.assertEqual(reserve.failure_text("codex-as", "lab", "prompt", "launch format"),
            "codex-as: lab: the usage reserve guard failed (launch format).\nThis prompt was not sent.\n"
            "To go on without reserves, exit and run:\n  codex-as --reserve-5h off --reserve-weekly off lab resume --last")

    def test_relaunch_names_the_windows_in_flag_order(self):
        self.assertEqual(reserve.relaunch("codex-as", "lab", ("weekly", "five_hour")),
                         "codex-as --reserve-5h off --reserve-weekly off lab resume --last")
        self.assertEqual(reserve.relaunch("claude-as", "personal", ("weekly",)),
                         "claude-as --reserve-weekly off personal --continue")

    def test_a_past_reset_shows_no_negative_span(self):
        status = reserve.WindowStatus("five_hour", D(10), reserve.AT_RESERVE, D(10), NOW - 30)
        self.assertIn("; resets in 0m (2027-01-15T07:59:30Z).", reserve.block_text(
            "claude-as", "a", reserve.Decision(False, "threshold", (status,)), "prompt", NOW))

    def test_unknown_launchers_events_and_allowing_decisions_are_errors(self):
        for call in (lambda: reserve.block_text("codex", "a", reserve.Decision(False, "blind"), "prompt", NOW),
                     lambda: reserve.block_text("codex-as", "a", reserve.Decision(False, "blind"), "start", NOW),
                     lambda: reserve.block_text("codex-as", "a", reserve.Decision(True, None), "prompt", NOW),
                     lambda: reserve.failure_text("claude", "a", "tool", "KeyError"),
                     lambda: reserve.relaunch("codex-as", "a", ())):
            with self.assertRaises(ValueError):
                call()


class Purity(unittest.TestCase):
    def test_reserve_module_does_no_io_and_reads_no_environment(self):
        source = (ROOT / "lib/coding-agent-kit/reserve.py").read_text()
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, ("open", "print", "input", "exec", "eval", "__import__"))
        self.assertLessEqual(imported, {"dataclasses", "datetime", "decimal", "math", "re"})
        self.assertNotIn("CODING_AGENT_KIT_", source)

    def test_overrides_and_policies_are_frozen(self):
        for value, field in ((reserve.Overrides(), "weekly"), (policy(D(1)), "weekly"),
                             (reserve.Setting("weekly", None, "default"), "reserve"),
                             (reserve.Reading("weekly", 1, None), "used"),
                             (reserve.WindowStatus("weekly", D(1), reserve.OK, D(99), None), "status"),
                             (reserve.Decision(True, None), "allow")):
            with self.subTest(value=value), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, field, None)

if __name__ == "__main__":
    unittest.main()
