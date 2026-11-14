"""Usage reserves: launch flags, per-window resolution and the quota evaluator.

Everything runs in process: reserve.py is pure (no I/O, an injected clock), and
config.py runs in a temporary HOME. No provider, credential or network is involved.
"""

import ast
import dataclasses
from decimal import Decimal
from fractions import Fraction
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
WALL = 1_800_000_000.0  # wall-clock epoch seconds when the fake clock reads START
START = 1000.0
FIVE_HOURS, WEEK = 5 * 3600, 7 * 86400
SPEC_RESERVES = ('[usage_reserve]\nfive_hour = 20\nweekly = 30\n'
                 '[usage_reserve.codex_accounts.borrowed]\nweekly = 40\n'
                 '[usage_reserve.claude_accounts.borrowed]\nweekly = 40\n')


class Clock:
    """The evaluator's injected monotonic clock."""

    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def wall(self):
        return WALL + (self.now - START)


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


def observation(clock, five=10, weekly=10, *, five_reset=3600, weekly_reset=86400, attributed=True, extra=(),
                fresh_at=None, wall=None):
    """A new response: the five-hour window named by duration, the weekly one by name."""
    wall = clock.wall() if wall is None else wall
    readings = []
    if five is not None:
        readings.append(reserve.Reading(used=five, resets_at=wall + five_reset, duration=FIVE_HOURS))
    if weekly is not None:
        readings.append(reserve.Reading(used=weekly, resets_at=wall + weekly_reset, name="weekly"))
    return reserve.Observation(tuple(readings) + tuple(extra), clock.now if fresh_at is None else fresh_at, wall,
                               attributed)


def running(clock, five=D(10), weekly=D(40), **reading):
    evaluator = reserve.Evaluator(policy(five, weekly), clock)
    evaluator.control_up()
    verdict = evaluator.observe(observation(clock, **reading))
    assert verdict.decision == "run", verdict
    return evaluator


def paused_by_threshold(clock):
    evaluator = running(clock)
    clock.advance(1)
    verdict = evaluator.observe(observation(clock, weekly=60))  # 40% left: at the weekly reserve
    assert (verdict.state, verdict.reason) == ("paused", "threshold"), verdict
    return evaluator


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
                             (reserve.Setting("weekly", None, "default"), "reserve")):
            with self.subTest(value=value), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(value, field, None)


class Evaluation(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def evaluator(self, five=D(10), weekly=D(40), **options):
        return reserve.Evaluator(policy(five, weekly), self.clock, **options)

    def started(self, *args, **reading):
        evaluator = self.evaluator()
        evaluator.control_up()
        return evaluator, evaluator.observe(observation(self.clock, *args, **reading))

    def test_tunables_are_the_g0_values(self):
        self.assertEqual((reserve.POLL_RUNNING, reserve.POLL_NEAR, reserve.NEAR_POINTS), (30, 15, 10))
        self.assertEqual(reserve.MAX_AGE, 120)
        self.assertIs(reserve.RECOVERY_BUDGET, reserve.MAX_AGE)  # one deadline, not a second setting
        self.assertEqual((reserve.BACKOFF_MIN, reserve.BACKOFF_MAX), (5, 60))
        self.assertEqual((reserve.POLL_PAUSED, reserve.POLL_PAUSED_MAX), (60, 300))
        self.assertEqual((reserve.QUERY_TIMEOUT, reserve.CONNECT_TIMEOUT, reserve.CONTINUATION_WINDOW), (10, 5, 30))
        self.assertEqual((reserve.HEARTBEAT, reserve.HEARTBEAT_LOSS, reserve.REBIND_HOLD), (0.25, 1.5, 3))
        self.assertEqual((reserve.REVALIDATIONS_PER_MINUTE, reserve.USAGE_READS_PER_MINUTE), (4, 10))
        self.assertEqual((reserve.SELF_TEST_BOUND, reserve.RESET_SKEW), (20, 120))

    def test_the_runtime_constants_are_these_tunables(self):
        """The gate, the monitor and the macOS boundary never import this module; their own copies of
        the timing values must stay these."""
        gate = load_module("kit_gate_for_reserves", "lib/coding-agent-kit/gate.py")
        monitor = load_module("kit_monitor_for_reserves", "lib/coding-agent-kit/monitor.py")
        macos = load_module("kit_sandbox_macos_for_reserves", "lib/coding-agent-kit/sandbox_macos.py")
        self.assertEqual((gate.READ_TIMEOUT, gate.CONNECT_TIMEOUT, gate.CONTINUATION_WINDOW, gate.HEARTBEAT_MAX),
                         (reserve.QUERY_TIMEOUT, reserve.CONNECT_TIMEOUT, reserve.CONTINUATION_WINDOW,
                          reserve.HEARTBEAT_LOSS))
        self.assertEqual((gate.USAGE_READS, gate.REVALIDATIONS),
                         ((reserve.USAGE_READS_PER_MINUTE, 60), (reserve.REVALIDATIONS_PER_MINUTE, 60)))
        self.assertEqual((gate.POLL_RUNNING, gate.POLL_PAUSED), (reserve.POLL_RUNNING, reserve.POLL_PAUSED))
        self.assertEqual((monitor.HEARTBEAT, monitor.POLL), (reserve.HEARTBEAT, reserve.POLL_RUNNING))
        self.assertEqual(macos.SELF_TEST_TIMEOUT, reserve.SELF_TEST_BOUND)

    def test_an_evaluator_needs_an_enabled_window(self):
        with self.assertRaises(ValueError):
            reserve.Evaluator(policy(), self.clock)
        with self.assertRaises(ValueError):
            reserve.Evaluator(policy("off", "off"), self.clock)

    def test_startup_runs_only_with_a_fresh_passing_reading_and_control(self):
        evaluator = self.evaluator()
        verdict = evaluator.verdict()
        self.assertEqual((verdict.state, verdict.decision, verdict.reason), ("checking", "check", "control_pending"))
        evaluator.control_up()
        self.assertEqual(evaluator.verdict().reason, "no_reading")
        self.assertFalse(evaluator.admits())
        verdict = evaluator.observe(observation(self.clock))
        self.assertEqual((verdict.state, verdict.decision, verdict.reason), ("running", "run", None))
        self.assertTrue(evaluator.admits())
        # The order does not matter: data first, then control.
        evaluator = self.evaluator()
        self.assertEqual(evaluator.observe(observation(self.clock)).reason, "control_pending")
        self.assertEqual(evaluator.control_up().decision, "run")

    def test_startup_refuses_on_definitive_conditions_in_either_order(self):
        for name, act, reason in (
                ("threshold", lambda e: e.observe(observation(self.clock, weekly=60)), "threshold"),
                ("missing", lambda e: e.observe(observation(self.clock, weekly=None)), "window_unavailable"),
                ("unattributed", lambda e: e.observe(observation(self.clock, attributed=False)), "unattributed"),
                ("fault", lambda e: e.fault("unsupported_plan"), "unsupported_plan"),
                ("control", lambda e: e.control_down("monitor_lost"), "monitor_lost")):
            for control_first in (True, False):
                with self.subTest(name=name, control_first=control_first):
                    evaluator = self.evaluator()
                    if control_first:
                        evaluator.control_up()
                    verdict = act(evaluator)  # before the first heartbeat too: the same refusal
                    self.assertEqual((verdict.state, verdict.decision, verdict.reason, verdict.cause),
                                     ("checking", "refuse", reason, reason))
                    # A refusal is final: a newer passing reading and healthy control never start
                    # model work, and a startup fault is never cleared.
                    self.clock.advance(1)
                    evaluator.observe(observation(self.clock))
                    verdict = evaluator.control_up()
                    self.assertEqual((verdict.decision, verdict.reason), ("refuse", reason))
                    self.assertFalse(evaluator.admits())

    def test_transient_startup_failures_keep_checking_until_the_launcher_gives_up(self):
        evaluator = self.evaluator()
        evaluator.control_up()
        verdict = evaluator.failure("timeout")
        self.assertEqual((verdict.decision, verdict.reason, verdict.failure), ("check", "no_reading", "timeout"))
        verdict = evaluator.give_up()
        self.assertEqual((verdict.decision, verdict.reason, verdict.cause), ("refuse", "no_reading", "no_reading"))
        self.assertEqual(verdict.message(), "borrowed: refused: no quota reading yet (last error: timeout).")
        # A passing reading after the startup bound does not start model work.
        self.assertEqual(evaluator.observe(observation(self.clock)).decision, "refuse")
        with self.assertRaises(ValueError):
            running(self.clock).give_up()  # only a start can be given up

    def test_pause_when_remaining_is_at_or_below_the_reserve(self):
        # remaining = 100 - used, exact for the numbers as the provider wrote them.
        for used, reserve_value, decision in ((90, D(10), "refuse"), (89.99, D(10), "run"), (70.1, D("29.9"), "refuse"),
                                              (D("70.09"), D("29.9"), "run"), (100, D(0), "refuse"),
                                              (99.999, D(0), "run"), (0, D(100), "refuse"), (0.0, D(99.99), "run")):
            with self.subTest(used=used, reserve=reserve_value):
                evaluator = self.evaluator(five=reserve_value, weekly=None)
                evaluator.control_up()
                self.assertEqual(evaluator.observe(observation(self.clock, five=used, weekly=None)).decision, decision)

    def test_either_window_crossing_pauses_and_names_each_crossing_window(self):
        for five, weekly, crossing in ((95, 10, ("five_hour",)), (10, 70, ("weekly",)), (90, 60, ("five_hour", "weekly"))):
            with self.subTest(five=five, weekly=weekly):
                evaluator = running(self.clock)
                self.clock.advance(1)
                verdict = evaluator.observe(observation(self.clock, five, weekly))
                self.assertEqual((verdict.state, verdict.decision, verdict.reason, verdict.cause),
                                 ("paused", "pause", "threshold", "threshold"))
                self.assertEqual(tuple(w.window for w in verdict.windows if w.status == "at_reserve"), crossing)
                self.assertFalse(evaluator.admits())

    def test_only_enabled_windows_matter(self):
        for weekly_reading in (None, "x", -5, D("NaN")):
            with self.subTest(weekly=weekly_reading):
                evaluator = self.evaluator(five=D(10), weekly="off")
                evaluator.control_up()
                verdict = evaluator.observe(observation(self.clock, weekly=weekly_reading))
                self.assertEqual(verdict.decision, "run")
                self.assertEqual([w.window for w in verdict.windows], ["five_hour"])

    def test_missing_and_invalid_windows_are_unavailable_never_zero(self):
        wall = self.clock.wall()
        cases = {
            "missing": [],
            "malformed": [reserve.Reading(used=True, resets_at=wall + 60, name="weekly")],
            "malformed-string": [reserve.Reading(used="10", resets_at=wall + 60, name="weekly")],
            "malformed-nan": [reserve.Reading(used=float("nan"), resets_at=wall + 60, name="weekly")],
            "malformed-inf": [reserve.Reading(used=float("inf"), resets_at=wall + 60, name="weekly")],
            "malformed-reset": [reserve.Reading(used=1, resets_at="soon", name="weekly")],
            "out_of_range": [reserve.Reading(used=-1, resets_at=wall + 60, name="weekly")],
            "out_of_range-high": [reserve.Reading(used=100.5, resets_at=wall + 60, name="weekly")],
            "reset_unknown": [reserve.Reading(used=1, resets_at=None, name="weekly")],
            "reset_anomaly-past": [reserve.Reading(used=1, resets_at=wall - 121, name="weekly")],
            "reset_anomaly-far": [reserve.Reading(used=1, resets_at=wall + WEEK + 121, name="weekly")],
            "ambiguous": [reserve.Reading(used=1, resets_at=wall + 60, name="weekly"),
                          reserve.Reading(used=1, resets_at=wall + 60, duration=WEEK)],
            "mismatch": [reserve.Reading(used=1, resets_at=wall + 60, name="weekly", duration=FIVE_HOURS)],
            "mismatch-other-length": [reserve.Reading(used=1, resets_at=wall + 60, name="weekly", duration=3600)],
            # Another window length or another name is not mapped onto an enabled window.
            "missing-other": [reserve.Reading(used=1, resets_at=wall + 60, duration=24 * 3600),
                              reserve.Reading(used=1, resets_at=wall + 60, name="seven_day_opus"),
                              reserve.Reading(used=1, resets_at=wall + 60, name="seven_day_opus", duration=WEEK),
                              reserve.Reading(used=1, resets_at=wall + 60)],
        }
        for case, readings in cases.items():
            with self.subTest(case=case):
                evaluator = self.evaluator()
                evaluator.control_up()
                verdict = evaluator.observe(observation(self.clock, weekly=None, extra=readings))
                self.assertEqual((verdict.decision, verdict.reason), ("refuse", "window_unavailable"))
                weekly = verdict.windows[1]
                self.assertEqual((weekly.window, weekly.status, weekly.remaining, weekly.resets_at),
                                 ("weekly", case.split("-")[0], None, None))
        # Within the skew, a reset is accepted.
        evaluator = self.evaluator()
        evaluator.control_up()
        edge = [reserve.Reading(used=1, resets_at=wall - 119, name="weekly")]
        self.assertEqual(evaluator.observe(observation(self.clock, weekly=None, extra=edge)).decision, "run")

    def test_a_window_not_started_is_valid_without_a_reset_only_at_exactly_zero(self):
        """A provider that reports a window as not started (Claude: utilization exactly 0 and resets_at
        null) gets a valid reading with no reset; any other reading without a reset stays unavailable."""
        idle = [reserve.Reading(used=0, resets_at=None, name="weekly", started=False)]
        for used in (0, 0.0, D("0.000"), D("-0")):
            with self.subTest(used=used):
                evaluator = self.evaluator()
                evaluator.control_up()
                readings = [reserve.Reading(used=used, resets_at=None, name="weekly", started=False)]
                verdict = evaluator.observe(observation(self.clock, weekly=None, extra=readings))
                self.assertEqual(verdict.decision, "run", verdict)
                self.assertEqual((verdict.windows[1].status, verdict.windows[1].remaining, verdict.windows[1].resets_at),
                                 ("ok", 100, None))
                self.assertEqual(verdict.message(), "borrowed: running: five-hour 90% remaining (reserve 10%), "
                                                    "weekly 100% remaining (reserve 40%).")
        for case, readings in (("used", [reserve.Reading(used=0.5, resets_at=None, name="weekly", started=False)]),
                               ("started", [reserve.Reading(used=0, resets_at=None, name="weekly")]),
                               ("bool", [reserve.Reading(used=False, resets_at=None, name="weekly", started=False)])):
            with self.subTest(case=case):
                evaluator = self.evaluator()
                evaluator.control_up()
                verdict = evaluator.observe(observation(self.clock, weekly=None, extra=readings))
                self.assertEqual((verdict.decision, verdict.reason), ("refuse", "window_unavailable"))
                self.assertEqual(verdict.windows[1].status, "malformed" if case == "bool" else "reset_unknown")
        # A reset given for a window not started is checked as any other.
        late = [reserve.Reading(used=0, resets_at=self.clock.wall() + WEEK + 121, name="weekly", started=False)]
        evaluator = self.evaluator()
        evaluator.control_up()
        self.assertEqual(evaluator.observe(observation(self.clock, weekly=None, extra=late)).windows[1].status,
                         "reset_anomaly")
        # At its reserve (100% keeps nothing for use), the message names no reset.
        evaluator = self.evaluator(five=None, weekly=D(100))
        evaluator.control_up()
        verdict = evaluator.observe(observation(self.clock, five=None, weekly=None, extra=idle))
        self.assertEqual((verdict.decision, verdict.reason), ("refuse", "threshold"))
        self.assertEqual(verdict.message(), "borrowed: refused: weekly window 100% remaining, at or below its 100% "
                                            "reserve.")

    def test_a_reading_with_another_name_is_never_ours_whatever_its_length(self):
        wall = self.clock.wall()
        for name in ("seven_day_opus", "seven_day_sonnet", "Weekly", "", 7):
            others = (reserve.Reading(used=99, resets_at=wall + 60, name=name, duration=WEEK),
                      reserve.Reading(used=99, resets_at=wall + 60, name=name, duration=FIVE_HOURS))
            with self.subTest(name=name):
                # Beside the real windows it changes nothing (no "ambiguous").
                evaluator = self.evaluator()
                evaluator.control_up()
                verdict = evaluator.observe(observation(self.clock, extra=others))
                self.assertEqual(verdict.decision, "run")
                self.assertEqual([(w.window, w.status, w.remaining) for w in verdict.windows],
                                 [("five_hour", "ok", D(90)), ("weekly", "ok", D(90))])
                # Without them it never stands in for either.
                evaluator = self.evaluator()
                evaluator.control_up()
                verdict = evaluator.observe(observation(self.clock, five=None, weekly=None, extra=others))
                self.assertEqual((verdict.decision, verdict.reason), ("refuse", "window_unavailable"))
                self.assertEqual([w.status for w in verdict.windows], ["missing", "missing"])

    def test_a_running_reading_without_an_enabled_window_pauses_at_once(self):
        evaluator = running(self.clock)
        self.clock.advance(1)
        verdict = evaluator.observe(observation(self.clock, weekly=None))
        self.assertEqual((verdict.state, verdict.reason), ("paused", "window_unavailable"))
        self.assertEqual(verdict.message(), "borrowed: paused: weekly window unavailable (missing); "
                                            "last valid reading at 2027-01-15T08:00:00Z.")

    def test_unattributed_readings_pause_at_once(self):
        evaluator = running(self.clock)
        self.clock.advance(1)
        verdict = evaluator.observe(observation(self.clock, attributed=False))
        self.assertEqual((verdict.state, verdict.reason, verdict.windows), ("paused", "unattributed", ()))
        self.assertEqual(verdict.message(), "borrowed: paused: the quota reading is not attributed to this account; "
                                            "last valid reading at 2027-01-15T08:00:00Z.")

    def test_freshness_expires_after_the_maximum_age(self):
        evaluator = running(self.clock)
        self.clock.advance(reserve.MAX_AGE)
        self.assertEqual(evaluator.tick().decision, "run")
        self.assertTrue(evaluator.admits())
        self.clock.advance(0.001)
        self.assertFalse(evaluator.admits())  # admission itself sees the deadline, without a tick
        verdict = evaluator.verdict()
        self.assertEqual((verdict.state, verdict.reason, verdict.cause), ("paused", "stale", "stale"))
        self.assertEqual(verdict.last_fresh, WALL)
        self.assertEqual(verdict.message(), "borrowed: paused: no fresh quota reading for 120 s, "
                                            "last at 2027-01-15T08:00:00Z.")

    def test_transient_failures_are_bounded_by_the_last_fresh_reading(self):
        evaluator = running(self.clock)
        delays = []
        while evaluator.verdict().state == "running":
            delay = evaluator.next_poll()
            delays.append(delay)
            self.clock.advance(delay)
            evaluator.failure("http_503")
        # Finite retries: backoff from 5 s up to 60 s; the budget is never restarted by failures.
        self.assertEqual(delays, [30, 5, 10, 20, 40, 60])
        self.assertLessEqual(self.clock.now - START, reserve.MAX_AGE + reserve.BACKOFF_MAX)
        verdict = evaluator.verdict()
        self.assertEqual((verdict.reason, verdict.failure), ("stale", "http_503"))
        self.assertEqual(verdict.message(), "borrowed: paused: no fresh quota reading for 165 s, "
                                            "last at 2027-01-15T08:00:00Z (last error: http_503).")
        # Jitter stays within the bounds.
        evaluator = running(self.clock)
        for count in range(1, 9):
            evaluator.failure("timeout")
            low, high = evaluator.next_poll(jitter=lambda a, b: a), evaluator.next_poll(jitter=lambda a, b: b)
            self.assertEqual(low, reserve.BACKOFF_MIN)
            self.assertEqual(high, min(reserve.BACKOFF_MAX, reserve.BACKOFF_MIN * 2 ** (count - 1)))
            self.assertEqual(evaluator.next_poll(jitter=lambda a, b: b * 10), high)

    def test_unchanged_but_fresh_readings_refresh_age_and_repeats_do_not(self):
        evaluator = running(self.clock)
        first = observation(self.clock)
        evaluator.failure("timeout")
        verdict = evaluator.observe(first)  # the same response again: no new information
        self.assertEqual((verdict.failure, evaluator.next_poll()), ("timeout", reserve.BACKOFF_MIN))
        self.clock.advance(100)
        verdict = evaluator.observe(observation(self.clock))  # a new response, same numbers
        self.assertEqual((verdict.decision, verdict.age), ("run", 0))
        self.clock.advance(100)
        self.assertEqual(evaluator.verdict().age, 100)
        # A cache re-read hands back the old receipt time: it never refreshes the age.
        self.clock.advance(19)
        self.assertEqual(evaluator.observe(observation(self.clock, fresh_at=START + 100)).age, 119)
        self.assertEqual(evaluator.observe(first).age, 119)
        self.clock.advance(2)
        self.assertEqual(evaluator.observe(observation(self.clock, fresh_at=START + 100)).reason, "stale")

    def test_late_future_and_malformed_observations_are_failures(self):
        evaluator = self.evaluator()
        evaluator.control_up()
        late = observation(self.clock)
        self.clock.advance(reserve.MAX_AGE + 1)
        self.assertEqual(evaluator.observe(late).failure, "stale_response")
        self.assertEqual(evaluator.observe(observation(self.clock, fresh_at=self.clock.now + 1)).failure, "invalid_time")
        far = 253_400_000_001  # beyond it, an accepted reset could not be shown as a date
        for bad in (reserve.Observation((), float("nan"), WALL, True),
                    reserve.Observation((), self.clock.now, far, True),
                    reserve.Observation((), self.clock.now, -1, True),
                    reserve.Observation((), self.clock.now, float("inf"), True),
                    reserve.Observation((), self.clock.now, 1e300, True),
                    reserve.Observation((), self.clock.now, self.clock.wall(), 1),
                    reserve.Observation(("reading",), self.clock.now, self.clock.wall(), True),
                    reserve.Observation(None, self.clock.now, self.clock.wall(), True)):
            with self.subTest(bad=bad):
                verdict = evaluator.observe(bad)
                self.assertEqual((verdict.decision, verdict.failure), ("check", "invalid_observation"))
        with self.assertRaises(TypeError):
            evaluator.observe("not an observation")

    def test_reset_or_recovery_never_resumes(self):
        evaluator = paused_by_threshold(self.clock)
        self.clock.advance(1)
        verdict = evaluator.observe(observation(self.clock, five=0, weekly=0))  # both windows reset
        self.assertEqual((verdict.state, verdict.reason, verdict.cause, verdict.eligible),
                         ("paused", None, "threshold", True))
        self.assertFalse(evaluator.admits())
        self.assertEqual(verdict.message("Press F12 to continue."),
                         "borrowed: paused: a usage window reached its reserve; the latest quota check passes; "
                         "a continuation resumes. Press F12 to continue.")
        for _ in range(5):
            self.clock.advance(30)
            evaluator.observe(observation(self.clock, five=0, weekly=0))
            self.assertEqual(evaluator.verdict().state, "paused")

    def test_independent_resets(self):
        evaluator = paused_by_threshold(self.clock)  # weekly at its reserve
        self.clock.advance(1)
        verdict = evaluator.observe(observation(self.clock, five=0, weekly=60))  # only the five-hour window reset
        self.assertEqual((verdict.reason, verdict.eligible), ("threshold", False))
        self.assertEqual(evaluator.continuation().continuation, "armed")
        self.clock.advance(1)
        verdict = evaluator.observe(observation(self.clock, five=0, weekly=60))
        self.assertEqual((verdict.state, verdict.reason, verdict.continuation), ("paused", "threshold", None))
        # A passed reset time proves nothing by itself: without new data the window stays at its
        # reserve until the reading is too old, and the evaluator never reads the wall clock.
        self.clock.advance(reserve.MAX_AGE)
        self.assertEqual(evaluator.verdict().reason, "threshold")
        self.clock.advance(1)
        self.assertEqual((evaluator.verdict().state, evaluator.verdict().reason), ("paused", "stale"))

    def test_manual_continuation_needs_a_new_passing_check(self):
        evaluator = paused_by_threshold(self.clock)
        self.clock.advance(1)
        before = observation(self.clock, five=0, weekly=0)  # received before the request
        self.clock.advance(1)
        self.assertEqual(evaluator.continuation().continuation, "armed")
        self.assertEqual(evaluator.next_poll(), 0)  # check now
        verdict = evaluator.observe(before)
        self.assertEqual((verdict.state, verdict.continuation), ("paused", "armed"))
        self.clock.advance(1)
        verdict = evaluator.observe(observation(self.clock, five=0, weekly=0))
        self.assertEqual((verdict.state, verdict.decision, verdict.cause, verdict.continuation),
                         ("running", "run", None, None))
        self.assertTrue(evaluator.admits())

    def test_a_failed_continuation_check_is_used_up(self):
        for name, check in (("threshold", lambda e: e.observe(observation(self.clock, weekly=70))),
                            ("failure", lambda e: e.failure("timeout")),
                            ("unattributed", lambda e: e.observe(observation(self.clock, weekly=0, attributed=False)))):
            with self.subTest(name=name):
                evaluator = paused_by_threshold(self.clock)
                evaluator.continuation()
                self.clock.advance(1)
                verdict = check(evaluator)
                self.assertEqual((verdict.state, verdict.continuation), ("paused", None))
                self.clock.advance(1)
                verdict = evaluator.observe(observation(self.clock, five=0, weekly=0))
                self.assertEqual((verdict.state, verdict.eligible), ("paused", True))
        # A failed check never resumes on older passing data.
        evaluator = paused_by_threshold(self.clock)
        self.clock.advance(1)
        evaluator.observe(observation(self.clock, five=0, weekly=0))
        evaluator.continuation()
        self.clock.advance(1)
        self.assertEqual(evaluator.failure("timeout").state, "paused")
        # A failure of a request sent before the continuation does not use it up; a time
        # after now counts as now.
        evaluator.continuation()
        self.clock.advance(1)
        self.assertEqual(evaluator.failure("timeout", at=self.clock.now - 2).continuation, "armed")
        self.assertEqual(evaluator.failure("timeout", at=float("nan")).continuation, None)
        evaluator.continuation()
        self.assertEqual(evaluator.failure("timeout", at=self.clock.now + 5).continuation, "armed")
        for bad in ("Timeout", "http 503", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                evaluator.failure(bad)

    def test_a_continuation_expires_without_a_check(self):
        evaluator = paused_by_threshold(self.clock)
        evaluator.continuation()
        self.clock.advance(reserve.CONTINUATION_WINDOW)
        self.assertEqual(evaluator.tick().continuation, "armed")
        self.clock.advance(0.001)
        self.assertEqual(evaluator.tick().continuation, None)
        self.clock.advance(1)
        self.assertEqual(evaluator.observe(observation(self.clock, five=0, weekly=0)).state, "paused")

    def test_continuations_only_while_paused_with_control(self):
        evaluator = self.evaluator()
        self.assertEqual(evaluator.continuation().continuation, None)  # checking
        evaluator = running(self.clock)
        self.assertEqual(evaluator.continuation().continuation, None)
        for _ in range(3):  # a gate may report every heard heartbeat: nothing changes
            self.assertEqual(evaluator.control_up().decision, "run")
        self.clock.advance(1)
        verdict = evaluator.control_down("monitor_lost")
        self.assertEqual((verdict.state, verdict.reason, verdict.cause), ("paused", "monitor_lost", "monitor_lost"))
        verdict = evaluator.continuation()
        self.assertEqual((verdict.continuation, verdict.reason), (None, "monitor_lost"))
        self.assertEqual(verdict.message(), "borrowed: paused: the quota monitor stopped; "
                                            "last valid reading at 2027-01-15T08:00:00Z.")

    def test_control_loss_is_final_for_the_evaluator(self):
        evaluator = running(self.clock)
        self.clock.advance(1)
        verdict = evaluator.control_down("monitor_stale")
        self.assertEqual((verdict.state, verdict.reason, verdict.cause), ("paused", "monitor_stale", "monitor_stale"))
        self.clock.advance(1)
        self.assertFalse(evaluator.observe(observation(self.clock)).eligible)
        # The monitor is heard again: the loss stays, so no continuation is armed and a
        # passing check never resumes this evaluator.
        for _ in range(3):
            verdict = evaluator.control_up()
            self.assertEqual((verdict.state, verdict.reason, verdict.cause, verdict.eligible),
                             ("paused", "monitor_stale", "monitor_stale", False))
        self.assertEqual(verdict.message(), "borrowed: paused: the quota monitor stopped answering; "
                                            "last valid reading at 2027-01-15T08:00:02Z.")
        self.assertEqual(evaluator.continuation().continuation, None)
        self.clock.advance(1)
        self.assertEqual(evaluator.observe(observation(self.clock)).state, "paused")
        self.assertEqual(evaluator.next_poll(), reserve.POLL_PAUSED * 2 ** 2)  # two paused reads; nothing armed
        # The first loss stays when another one follows.
        self.assertEqual(evaluator.control_down("launcher_lost").reason, "monitor_stale")
        self.assertFalse(evaluator.admits())
        # A loss before the first heartbeat of a restarted gate is final as well.
        restarted = self.evaluator(paused="gate_restarted")
        restarted.control_down("monitor_lost")
        verdict = restarted.control_up()
        self.assertEqual((verdict.state, verdict.reason, verdict.cause, verdict.eligible),
                         ("paused", "monitor_lost", "gate_restarted", False))
        self.assertEqual(restarted.continuation().continuation, None)
        # Recovery is a restarted gate: a new evaluator that starts paused and gets a new monitor.
        restarted = self.evaluator(paused="gate_restarted")
        restarted.control_up()
        self.clock.advance(1)
        self.assertTrue(restarted.observe(observation(self.clock)).eligible)
        self.assertEqual(restarted.continuation().continuation, "armed")
        self.clock.advance(1)
        self.assertEqual(restarted.observe(observation(self.clock)).decision, "run")

    def test_loss_and_fault_reasons_are_not_the_evaluators_own_conditions(self):
        own = set(reserve.WAITING) | set(reserve.CAUSE_TEXT)
        for method, words in (("control_down", sorted(own | set(reserve.FAULT_TEXT))),
                              ("fault", sorted(own | set(reserve.CONTROL_TEXT)))):
            for word in words:
                with self.subTest(method=method, word=word):
                    evaluator = running(self.clock)
                    with self.assertRaises(ValueError):
                        getattr(evaluator, method)(word)
                    self.assertEqual(evaluator.verdict().decision, "run")
                    checking = self.evaluator()
                    with self.assertRaises(ValueError):
                        getattr(checking, method)(word)
                    self.assertEqual(checking.verdict().reason, "control_pending")
        # Their own words, and new ones, are accepted.
        for method, word in (("control_down", "gate_unavailable"), ("control_down", "monitor_gone"),
                             ("fault", "identity_changed"), ("fault", "token_expiring")):
            with self.subTest(method=method, word=word):
                self.assertEqual(getattr(running(self.clock), method)(word).reason, word)

    def test_the_deadlines_are_finite_and_positive(self):
        for name in ("max_age", "continuation_window"):
            for bad in (float("nan"), float("inf"), -float("inf"), 0, -1, True, "120", None, D(120)):
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    self.evaluator(**{name: bad})
        evaluator = self.evaluator(max_age=2, continuation_window=0.5)
        evaluator.control_up()
        evaluator.observe(observation(self.clock))
        self.clock.advance(2.001)
        self.assertEqual(evaluator.verdict().reason, "stale")

    def test_a_restarted_gate_starts_paused(self):
        evaluator = self.evaluator(paused="gate_restarted")
        verdict = evaluator.verdict()
        self.assertEqual((verdict.state, verdict.decision, verdict.reason, verdict.cause, verdict.eligible),
                         ("paused", "pause", "control_pending", "gate_restarted", False))
        self.assertEqual(verdict.message(), "borrowed: paused: the quota monitor is not running yet.")
        self.assertFalse(evaluator.admits())
        self.assertEqual(evaluator.next_poll(), reserve.POLL_PAUSED)
        self.assertEqual(evaluator.continuation().continuation, None)  # refused before a heard heartbeat
        self.clock.advance(1)
        verdict = evaluator.observe(observation(self.clock))  # passing, but no continuation yet
        self.assertEqual((verdict.state, verdict.reason, verdict.eligible), ("paused", "control_pending", False))
        verdict = evaluator.control_up()
        self.assertEqual((verdict.state, verdict.reason, verdict.cause, verdict.eligible),
                         ("paused", None, "gate_restarted", True))
        self.assertFalse(evaluator.admits())
        self.assertEqual(verdict.message(), "borrowed: paused: the request gate restarted; the latest quota check "
                                            "passes; a continuation resumes.")
        self.clock.advance(30)
        self.assertEqual(evaluator.observe(observation(self.clock)).state, "paused")  # a passing poll never resumes
        self.assertEqual(evaluator.continuation().continuation, "armed")
        self.clock.advance(1)
        self.assertEqual(evaluator.observe(observation(self.clock)).decision, "run")
        self.assertTrue(evaluator.admits())

    def test_a_threshold_while_restarted_is_a_pause_not_a_refusal(self):
        for control_first in (True, False):
            with self.subTest(control_first=control_first):
                evaluator = self.evaluator(paused="gate_restarted")
                if control_first:
                    evaluator.control_up()
                verdict = evaluator.observe(observation(self.clock, weekly=60))
                self.assertEqual((verdict.state, verdict.decision, verdict.reason, verdict.cause),
                                 ("paused", "pause", "threshold", "gate_restarted"))
                evaluator.control_up()
                self.clock.advance(1)
                self.assertTrue(evaluator.observe(observation(self.clock, weekly=0)).eligible)  # the window reset
                evaluator.continuation()
                self.clock.advance(1)
                self.assertEqual(evaluator.observe(observation(self.clock, weekly=0)).decision, "run")
        for bad in ("Gate restarted", "", 1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.evaluator(paused=bad)

    def test_control_loss_while_armed_disarms(self):
        evaluator = paused_by_threshold(self.clock)
        evaluator.continuation()
        self.clock.advance(1)
        self.assertEqual(evaluator.control_down("launcher_lost").continuation, None)
        evaluator.control_up()
        self.clock.advance(1)
        verdict = evaluator.observe(observation(self.clock, five=0, weekly=0))
        self.assertEqual((verdict.state, verdict.reason, verdict.continuation, verdict.eligible),
                         ("paused", "launcher_lost", None, False))

    def test_faults_pause_and_clear_only_with_newer_attributed_data(self):
        evaluator = running(self.clock)
        older = observation(self.clock)
        self.clock.advance(1)
        in_flight = observation(self.clock)  # received before the fault, delivered after it
        self.clock.advance(1)
        verdict = evaluator.fault("identity_changed")
        self.assertEqual((verdict.state, verdict.reason), ("paused", "identity_changed"))
        self.assertEqual(verdict.message(), "borrowed: paused: the signed-in account changed; "
                                            "last valid reading at 2027-01-15T08:00:00Z.")
        verdict = evaluator.observe(in_flight)
        self.assertEqual((verdict.state, verdict.reason, verdict.eligible), ("paused", "identity_changed", False))
        evaluator.continuation()
        self.assertEqual(evaluator.observe(older).state, "paused")  # not newer than the latest reading
        self.clock.advance(1)
        self.assertEqual(evaluator.observe(observation(self.clock, attributed=False)).state, "paused")
        evaluator.continuation()
        self.clock.advance(1)
        self.assertEqual(evaluator.observe(observation(self.clock)).state, "running")
        for bad in ("", "Identity", "a b", "x" * 65, "é", 3):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                evaluator.fault(bad)

    def test_a_fault_names_what_it_concerns_and_never_a_value(self):
        """A fault's names (for example the unreviewed quota rows of a usage report) are part of the
        deterministic line for a refused start and a pause alike, and only while that fault blocks."""
        checking = self.evaluator()
        checking.control_up()
        verdict = checking.fault("unproved_quota_row", names=("seven_day_oauth_apps", "cinder_cove", "cinder_cove"))
        self.assertEqual((verdict.decision, verdict.reason, verdict.names),
                         ("refuse", "unproved_quota_row", ("cinder_cove", "seven_day_oauth_apps")))
        self.assertEqual(verdict.message(), "borrowed: refused: the usage report has a limit row protected mode has "
                                            "not reviewed (cinder_cove, seven_day_oauth_apps).")
        evaluator = running(self.clock)
        self.clock.advance(1)
        verdict = evaluator.fault("unproved_quota_row", names=["wattle_ember"])
        self.assertEqual(verdict.message(), "borrowed: paused: the usage report has a limit row protected mode has "
                                            "not reviewed (wattle_ember); last valid reading at 2027-01-15T08:00:00Z.")
        # Names belong to the fault that brought them: another fault and a control loss show none.
        self.assertEqual(evaluator.fault("identity_changed").names, ())
        verdict = evaluator.fault("unproved_quota_row", names=("wattle_ember",))
        verdict = evaluator.control_down("monitor_lost")
        self.assertEqual((verdict.reason, verdict.names), ("monitor_lost", ()))
        self.assertNotIn("wattle_ember", verdict.message())
        # A long list stays one bounded line.
        many = tuple("row_%02d" % index for index in range(20))
        message = self.evaluator().fault("unproved_quota_row", names=many).message()
        self.assertIn("(row_00, row_01, row_02, row_03, row_04, row_05, row_06, row_07, and 12 more)", message)
        # Only sanitized names are accepted, never a value or free text.
        for bad in (("Seven_Day",), ("a b",), ("x" * 65,), ("",), (3,), "cinder_cove", ("cinder_cove", None)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.evaluator().fault("unproved_quota_row", names=bad)

    def test_poll_intervals(self):
        evaluator = running(self.clock, five_reset=3600)
        self.assertEqual(evaluator.next_poll(), reserve.POLL_RUNNING)  # 90% and 90% left
        self.clock.advance(1)
        evaluator.observe(observation(self.clock, five=80))  # 20% left: 10 points above the 10% reserve
        self.assertEqual(evaluator.next_poll(), reserve.POLL_NEAR)
        self.clock.advance(1)
        evaluator.observe(observation(self.clock, weekly=49.9))  # 50.1% left, 10.1 points above 40%
        self.assertEqual(evaluator.next_poll(), reserve.POLL_RUNNING)
        evaluator = paused_by_threshold(self.clock)
        delays = []
        for _ in range(5):
            delays.append(evaluator.next_poll())
            self.clock.advance(delays[-1])
            evaluator.observe(observation(self.clock, weekly=60))
        self.assertEqual(delays, [60, 120, 240, 300, 300])
        evaluator.continuation()
        self.assertEqual(evaluator.next_poll(), 0)

    def test_checking_polls_at_once_only_without_a_usable_reading(self):
        evaluator = self.evaluator()
        self.assertEqual(evaluator.next_poll(), 0)
        evaluator.observe(observation(self.clock))  # fresh and passing; control is still pending
        self.assertEqual((evaluator.verdict().reason, evaluator.next_poll()), ("control_pending", reserve.POLL_RUNNING))
        self.clock.advance(1)
        evaluator.observe(observation(self.clock, weekly=50.5))  # 49.5% left: near the 40% reserve
        self.assertEqual(evaluator.next_poll(), reserve.POLL_NEAR)
        self.clock.advance(reserve.MAX_AGE + 1)
        self.assertEqual((evaluator.verdict().reason, evaluator.next_poll()), ("stale", 0))
        evaluator.failure("timeout")
        self.assertEqual(evaluator.next_poll(), reserve.BACKOFF_MIN)

    def test_the_reserve_comparison_is_exact_at_any_precision(self):
        near_all = D("99.9999999999999999999999999995")  # 30 significant digits: 100 - 5E-28
        for used, decision in ((1e-27, "refuse"), (D("5E-28"), "refuse"), (D("4.9E-28"), "run"),
                               (D("1E-1000"), "run"), (D("0.0000000000000000000000000005000000000001"), "refuse")):
            with self.subTest(used=used):
                evaluator = self.evaluator(five=near_all, weekly=None)
                evaluator.control_up()
                verdict = evaluator.observe(observation(self.clock, five=used, weekly=None))
                self.assertEqual(verdict.decision, decision)
                # The remaining value shown is never above the one observed.
                exact = 100 - Fraction(D(repr(used)) if isinstance(used, float) else used)
                self.assertLessEqual(Fraction(verdict.windows[0].remaining), exact)

    def test_threshold_message_fields(self):
        evaluator = running(self.clock)
        self.clock.advance(1)
        verdict = evaluator.observe(observation(self.clock, five=90.04, weekly=60.5, five_reset=1800))
        self.assertEqual(verdict.account, "borrowed")
        self.assertEqual(verdict.windows, (
            reserve.WindowStatus("five_hour", D(10), "at_reserve", D("9.96"), self.clock.wall() + 1800),
            reserve.WindowStatus("weekly", D(40), "at_reserve", D("39.5"), self.clock.wall() + 86400)))
        self.assertEqual((verdict.last_fresh, verdict.age), (self.clock.wall(), 0))
        # Remaining is shown rounded down, never above what was observed.
        self.assertEqual(verdict.message("Press F12 to continue."),
                         "borrowed: paused: five-hour window 9.9% remaining, at or below its 10% reserve, "
                         "resets 2027-01-15T08:30:01Z; weekly window 39.5% remaining, at or below its 40% reserve, "
                         "resets 2027-01-16T08:00:01Z. Press F12 to continue.")
        self.assertEqual(running(self.clock).verdict().message(),
                         "borrowed: running: five-hour 90% remaining (reserve 10%), weekly 90% remaining (reserve 40%).")

    def test_an_exhausted_window_shows_zero_remaining_never_minus_zero(self):
        for used in (100, 100.0, D("100.000")):
            with self.subTest(used=used):
                evaluator = running(self.clock)
                self.clock.advance(1)
                verdict = evaluator.observe(observation(self.clock, five=used, weekly=10))
                self.assertEqual(verdict.windows[0].remaining, 0)
                self.assertFalse(verdict.windows[0].remaining.is_signed())
                self.assertEqual(reserve.percent(verdict.windows[0].remaining, floor=True), "0")
                self.assertIn("five-hour window 0% remaining, at or below its 10% reserve", verdict.message())
                self.assertNotIn(" -0", verdict.message())

    def test_a_running_window_is_shown_above_its_reserve(self):
        # Rounded down to one decimal, with more decimals only where one would reach the
        # reserve; never above the value observed.
        for used, reserve_value, shown in ((89.96, D(10), "10.04"), (89.9, D(10), "10.1"), (10.04, D(10), "89.9"),
                                           (D("89.85"), D("10.1"), "10.15"), (D("89.81"), D("10.1"), "10.19"),
                                           (D("89.75"), D("10.1"), "10.2"), (0, D(10), "100"),
                                           (D("99.99"), D(0), "0.01"),
                                           (D("89." + "9" * 26), D(10), "10." + "0" * 25 + "1"),
                                           # Beyond 28 digits only "over" stays true.
                                           (D("89." + "9" * 30), D(10), "over 10"),
                                           (D("99." + "9" * 40), D(0), "over 0")):
            with self.subTest(used=used, reserve=reserve_value):
                evaluator = self.evaluator(five=reserve_value, weekly=None)
                evaluator.control_up()
                verdict = evaluator.observe(observation(self.clock, five=used, weekly=None))
                self.assertEqual(verdict.message(), f"borrowed: running: five-hour {shown}% remaining "
                                                    f"(reserve {reserve.percent(reserve_value)}%).")

    def test_checking_messages(self):
        evaluator = self.evaluator()
        self.assertEqual(evaluator.verdict().message(), "borrowed: checking: the quota monitor is not running yet.")
        evaluator.control_up()
        self.assertEqual(evaluator.verdict().message(), "borrowed: checking: no quota reading yet.")


if __name__ == "__main__":
    unittest.main()
