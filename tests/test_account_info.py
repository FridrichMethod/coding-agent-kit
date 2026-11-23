"""Account metadata: the cache module, Codex identity and usage, and the queries.

Module cases run in process against a temporary state directory. Launcher
cases run the real scripts from kit copies in a temporary HOME with fake codex
and claude that only record, and fabricated rollouts; nothing reaches a real
login or the network.
"""

import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


accounts = load("kit_accounts_under_test", "lib/coding-agent-kit/accounts.py")
# 2026-01-01 12:00:00 UTC, a Thursday.
NOW = 1767268800


class Store(unittest.TestCase):
    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o077))
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-accounts-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.config = SimpleNamespace(state_dir=self.base / "state/coding-agent-kit", secrets_dir=self.base / "secrets")
        self.dir = self.config.state_dir / "accounts"
        patcher = patch.dict(os.environ, {"TZ": "UTC"})
        patcher.start()
        self.addCleanup(patcher.stop)
        time.tzset()
        self.addCleanup(time.tzset)

    def put(self, name, data, mode=0o600):
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.dir / name
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        path.chmod(mode)
        return path

    def test_directory_is_created_private_and_only_on_request(self):
        os.umask(0o022)  # the mode is the module's, whatever the caller's umask
        self.assertEqual(accounts.directory(self.config), self.dir)
        self.assertFalse(self.config.state_dir.exists(), "a plain lookup created the directory")
        self.assertEqual(accounts.directory(self.config, create=True), self.dir)
        self.assertEqual(stat.S_IMODE(self.dir.lstat().st_mode), 0o700)
        self.dir.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "0700"):
            accounts.directory(self.config, create=True)
        self.dir.chmod(0o700)
        self.config.state_dir.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "other users cannot change"):
            accounts.directory(self.config, create=True)
        self.config.state_dir.chmod(0o700)
        os.rename(self.dir, self.base / "real")
        os.symlink(self.base / "real", self.dir)
        with self.assertRaisesRegex(ValueError, "0700"):
            accounts.directory(self.config, create=True)

    def test_a_named_directory_beside_it_has_the_same_rules(self):
        os.umask(0o022)
        guard = self.config.state_dir / "guard"
        self.assertEqual(accounts.directory(self.config, name="guard"), guard)
        self.assertFalse(self.config.state_dir.exists(), "a plain lookup created the directory")
        self.assertEqual(accounts.directory(self.config, create=True, name="guard"), guard)
        self.assertEqual(stat.S_IMODE(guard.lstat().st_mode), 0o700)
        self.assertFalse(self.dir.exists(), "the guard directory made the accounts directory")
        self.assertEqual(accounts.directory(self.config, create=True), self.dir)
        guard.chmod(0o750)
        with self.assertRaisesRegex(ValueError, "0700"):
            accounts.directory(self.config, create=True, name="guard")
        guard.chmod(0o700)
        self.config.state_dir.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "other users cannot change"):
            accounts.directory(self.config, create=True, name="guard")
        self.config.state_dir.chmod(0o700)
        os.rename(guard, self.base / "real-guard")
        os.symlink(self.base / "real-guard", guard)
        with self.assertRaisesRegex(ValueError, "0700"):
            accounts.directory(self.config, create=True, name="guard")
        config = SimpleNamespace(state_dir=self.config.state_dir, secrets_dir=self.base / "real-guard")
        for create in (False, True):
            with self.subTest(create=create), self.assertRaisesRegex(ValueError, "secrets directory"):
                accounts.directory(config, create=create, name="guard")
        for name in ("", ".", "..", "a/b", "/abs", "guard/", "a\0b", None, b"guard"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "directory name"):
                accounts.directory(self.config, create=True, name=name)
        self.assertEqual(sorted(p.name for p in self.config.state_dir.iterdir()), ["accounts", "guard"])

    def test_the_directory_is_never_the_secrets_directory(self):
        # Metadata files carry the credentials' names (codex-NAME.json): reading or writing one
        # there would open or replace a login.
        secrets = self.base / "real/coding-agent-kit/accounts"
        secrets.mkdir(parents=True, mode=0o700)
        credential = secrets / "codex-A.json"
        credential.write_text('{"tokens": {"access_token": "fabricated-sensitive-access"}}')
        credential.chmod(0o600)
        (self.base / "link").symlink_to(self.base / "real")
        for state in (self.base / "real/coding-agent-kit",  # the same path
                      self.base / "link/coding-agent-kit"):  # through a linked ancestor
            config = SimpleNamespace(state_dir=state, secrets_dir=secrets)
            for create in (False, True):
                with self.subTest(state=state, create=create), self.assertRaisesRegex(ValueError, "secrets directory"):
                    accounts.directory(config, create=create)
            with self.assertRaisesRegex(ValueError, "secrets directory"):
                accounts.info("codex-as", "A", config, NOW)
            self.assertEqual(accounts.labels("codex-as", ["A"], config), ["A"])
            self.assertEqual(credential.read_text(), '{"tokens": {"access_token": "fabricated-sensitive-access"}}')

    def test_reads_are_strict(self):
        self.assertIsNone(accounts.read(self.dir / "codex-A.json"))
        good = self.put("codex-A.json", {"email": "a@example.com"})
        self.assertEqual(accounts.read(good), {"email": "a@example.com"})
        for name, data, mode in (("loose.json", "{}", 0o644), ("dup.json", '{"a": 1, "a": 2}', 0o600),
                                 ("nan.json", '{"a": NaN}', 0o600), ("list.json", "[]", 0o600),
                                 ("big.json", '{"a": "' + "x" * accounts.LIMIT + '"}', 0o600),
                                 ("bad.json", "{", 0o600)):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    accounts.read(self.put(name, data, mode))
                self.assertNotIn("x" * 20, str(caught.exception), "an error quoted the file")
        os.symlink(good, self.dir / "link.json")
        with self.assertRaises(ValueError):
            accounts.read(self.dir / "link.json")
        os.link(good, self.dir / "hard.json")
        with self.assertRaises(ValueError):
            accounts.read(good)

    def test_writes_are_atomic_private_and_only_on_change(self):
        path = accounts.directory(self.config, create=True) / "codex-A.json"
        self.assertTrue(accounts.write(path, {"email": "a@example.com"}))
        self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600)
        inode = path.lstat().st_ino
        self.assertFalse(accounts.write(path, {"email": "a@example.com"}), "rewrote identical content")
        self.assertEqual(path.lstat().st_ino, inode)
        self.assertTrue(accounts.write(path, {"email": "b@example.com"}))
        self.assertEqual(accounts.read(path), {"email": "b@example.com"})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["codex-A.json"], "a temporary file was left")
        # A link in its place is replaced, never written through.
        victim = self.base / "victim"
        victim.write_text("kept")
        path.unlink()
        os.symlink(victim, path)
        self.assertTrue(accounts.write(path, {"email": "c@example.com"}))
        self.assertEqual(victim.read_text(), "kept")
        self.assertFalse(path.is_symlink())

    def test_writes_are_0600_under_any_umask(self):
        # A umask that takes the owner's own read or write bit must not leave a file that read() refuses.
        path = accounts.directory(self.config, create=True) / "claude-A.usage.json"
        for mask in (0o277, 0o477, 0o000):
            with self.subTest(umask=oct(mask)):
                path.unlink(missing_ok=True)
                previous = os.umask(mask)
                try:
                    self.assertTrue(accounts.write(path, {"seen_at": mask + 1}))
                finally:
                    os.umask(previous)
                self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600)
                self.assertEqual(accounts.read(path), {"seen_at": mask + 1})

    def test_labels_refuse_control_characters_length_and_tokens(self):
        self.assertIsNone(accounts.label_problem("alice@example.com (work)"))
        self.assertIsNone(accounts.label_problem("x" * 100))
        self.assertIn("longer", accounts.label_problem("x" * 101))
        self.assertIn("control", accounts.label_problem("a\x1b[31mb"))
        self.assertIn("control", accounts.label_problem("a\x85b"))
        self.assertIn("token", accounts.label_problem("pasted sk-ant-oat01-abc"))

    def test_codex_info_shows_identity_usage_and_age(self):
        names = {"primary": {"used_percent": 23.4, "window_minutes": 300, "resets_at": NOW + 2 * 3600 + 600},
                 "secondary": {"used_percent": 41.0, "window_minutes": 10080, "resets_at": NOW + 3 * 86400 + 4 * 3600 + 120},
                 "seen_at": NOW - 12 * 60}
        self.put("codex-work.json", {"email": "alice@example.com", "plan": "plus", "usage": names})
        self.assertEqual(accounts.info("codex-as", "work", self.config, NOW).splitlines(), [
            "work (Codex)",
            "email   alice@example.com",
            "plan    plus",
            "5h      23% used, resets 14:10 (in 2h 10m)",
            "weekly  41% used, resets Sun 16:02 (in 3d 4h)",
            "seen    12 min ago",
        ])

    def test_info_marks_reset_windows_and_missing_data(self):
        self.assertEqual(accounts.info("codex-as", "A", self.config, NOW).splitlines(), [
            "A (Codex)", "email   not recorded yet", "plan    not recorded yet", "usage   not recorded yet"])
        self.assertEqual(accounts.info("claude-as", "B", self.config, NOW).splitlines(), [
            "B (Claude)", "label   not set (claude-as --label B LABEL)", "usage   not recorded yet"])
        self.put("claude-B.json", {"label": "bob@example.com"})
        self.put("claude-B.usage.json", {"five_hour": {"used_percentage": 80, "resets_at": NOW - 60},
                                         "seven_day": {"used_percentage": 12.6, "resets_at": NOW + 1800},
                                         "seen_at": NOW - 3 * 86400})
        self.assertEqual(accounts.info("claude-as", "B", self.config, NOW).splitlines(), [
            "B (Claude)", "label   bob@example.com", "5h      reset since 11:59",
            "weekly  13% used, resets 12:30 (in 30m)", "seen    3 d ago"])

    def test_info_prints_only_allow_listed_and_clean_fields(self):
        self.put("codex-A.json", {"email": "a@ex\x1b]0;owned\x07ample.com", "plan": "\x9bplus",
                                  "tokens": {"access_token": "fabricated-sensitive-access"},
                                  "usage": {"primary": {"used_percent": 150, "window_minutes": 300},
                                            "secondary": {"used_percent": "50", "window_minutes": 10080},
                                            "seen_at": NOW}})
        text = accounts.info("codex-as", "A", self.config, NOW)
        self.assertNotIn("fabricated", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x9b", text)
        self.assertEqual(text.splitlines(), ["A (Codex)", "email   a@ex]0;ownedample.com", "plan    plus",
                                             "usage   not recorded yet"])

    def test_info_paints_only_its_own_words(self):
        # The launchers pass their colour policy as PAINT: the heading, missing
        # data and each window's use are styled, a stored value never is (and
        # holds no escape of its own), and without the styles the text is the
        # plain one byte for byte.
        def paint(text, style):
            return f"\x1b[{style}m{text}\x1b[0m"

        def sgr(style, text):
            return f"\x1b[{style}m{text}\x1b[0m"

        self.put("codex-A.json", {"email": "a@ex\x1b[31mample.com", "plan": "plus", "usage": {
            "primary": {"used_percent": 69.6, "window_minutes": 300, "resets_at": NOW + 600},
            "secondary": {"used_percent": 90, "window_minutes": 10080}, "seen_at": NOW - 60}})
        self.put("claude-B.json", {"label": "bob@example.com"})
        self.put("claude-B.usage.json", {"five_hour": {"used_percentage": 70, "resets_at": NOW - 60},
                                         "seven_day": {"used_percentage": 89.9, "resets_at": NOW + 1800}})
        cases = {
            ("codex-as", "A"): [sgr(36, "A (Codex)"), "email   a@ex[31mample.com", "plan    plus",
                                f"5h      {sgr(32, '70% used')}, resets 12:10 (in 10m)", f"weekly  {sgr(31, '90% used')}",
                                "seen    1 min ago"],
            ("claude-as", "B"): [sgr(36, "B (Claude)"), "label   bob@example.com", f"5h      {sgr(32, 'reset since 11:59')}",
                                 f"weekly  {sgr(33, '90% used')}, resets 12:30 (in 30m)"],
            ("codex-as", "C"): [sgr(36, "C (Codex)"), f"email   {sgr(33, 'not recorded yet')}",
                                f"plan    {sgr(33, 'not recorded yet')}", f"usage   {sgr(33, 'not recorded yet')}"],
            ("claude-as", "D"): [sgr(36, "D (Claude)"), f"label   {sgr(33, 'not set (claude-as --label D LABEL)')}",
                                 f"usage   {sgr(33, 'not recorded yet')}"],
        }
        for (tool, name), expected in cases.items():
            with self.subTest(tool=tool, name=name):
                self.assertEqual(accounts.info(tool, name, self.config, NOW, paint).splitlines(), expected)
                plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in expected]
                self.assertEqual(accounts.info(tool, name, self.config, NOW).splitlines(), plain)

    def test_a_windows_colour_follows_the_status_line(self):
        # helpers/statusline.cjs usageColor: red from 90, yellow from 70, else green.
        for used, style in ((0, "32"), (69.9, "32"), (70, "33"), (89.9, "33"), (90, "31"), (100, "31")):
            with self.subTest(used=used):
                self.assertEqual(accounts.used_style(used), style)
        source = (ROOT / "helpers/statusline.cjs").read_text()
        self.assertIn("return percentage >= 90 ? 31 : percentage >= 70 ? 33 : 32;", source)

    def test_times_beyond_the_calendar_are_ignored(self):
        for resets in (10**17, 10**19, 253402300800):
            with self.subTest(resets=resets):
                self.put("codex-A.json", {"usage": {"primary": {"used_percent": 5, "window_minutes": 300,
                                                                "resets_at": resets}, "seen_at": 10**19}})
                self.assertEqual(accounts.info("codex-as", "A", self.config, NOW).splitlines()[3:], ["5h      5% used"])

    def test_unsafe_metadata_fails_the_query_without_quoting_it(self):
        self.put("codex-A.json", {"email": "a@example.com"}, mode=0o644)
        with self.assertRaises(ValueError) as caught:
            accounts.info("codex-as", "A", self.config, NOW)
        self.assertNotIn("a@example.com", str(caught.exception))

    def test_labels_are_emails_or_labels_or_bare_names(self):
        self.put("codex-A.json", {"email": "a@example.com\tx"})
        self.put("codex-C.json", {"email": "c@example.com"}, mode=0o644)
        self.put("claude-B.json", {"label": "bob"})
        self.assertEqual(accounts.labels("codex-as", ["A", "B", "C"], self.config),
                         ["A\ta@example.comx", "B", "C"])
        self.assertEqual(accounts.labels("claude-as", ["A", "B"], self.config), ["A", "B\tbob"])



usage = load("kit_codex_usage_under_test", "lib/codex-as/usage.py")
USER, WORKSPACE = "user-fixture-A", "workspace-fixture-A"
THREAD, OTHER, RESUMED = ("019a0000-0000-7000-8000-00000000000a", "019a0000-0000-7000-8000-00000000000b",
                          "019a0000-0000-7000-8000-00000000000c")


def iso(epoch, milliseconds=0):
    """A line's time as native Codex stamps it: the whole second EPOCH and MILLISECONDS past it."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".%03dZ" % milliseconds


def limits(primary, secondary=None, limit_id=None):
    windows = [None if used is None else {"used_percent": used, "window_minutes": minutes, "resets_at": NOW + 3600}
               for used, minutes in ((primary, 300), (secondary, 10080))]
    return {"limit_id": limit_id, "limit_name": None, "primary": windows[0], "secondary": windows[1], "credits": None,
            "plan_type": None}


def rollout(root, thread, lines, day="2026/01/01", creator=(USER, WORKSPACE), stamp=NOW, suffix=""):
    """A rollout as native Codex writes it: session_meta, then event lines (WHEN, RATE_LIMITS)."""
    path = Path(root) / "sessions" / day / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', time.gmtime(stamp))}-{thread}.jsonl{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"id": thread, "timestamp": iso(stamp), "cwd": "/", "originator": "codex_cli_rs", "cli_version": "0.163.0"}
    if creator:
        meta.update(creator_user_id=creator[0], creator_account_id=creator[1])
    records = [{"timestamp": iso(stamp), "type": "session_meta", "payload": meta}]
    for when, rate in lines:
        records.append({"timestamp": iso(when), "type": "event_msg",
                        "payload": {"type": "token_count", "info": None, "rate_limits": rate}})
    with open(path, "a") as stream:
        stream.write("".join(json.dumps(record) + "\n" for record in records))
    return path


def append(path, stamp, rate):
    """One more token_count line at the end of the rollout at PATH, stamped STAMP (see iso)."""
    with open(path, "a") as stream:
        stream.write(json.dumps({"timestamp": stamp, "type": "event_msg",
                                 "payload": {"type": "token_count", "info": None, "rate_limits": rate}}) + "\n")


class CodexUsage(unittest.TestCase):
    """The rollout scan in process, on fabricated rollouts of a shared sessions directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-usage-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        patcher = patch.dict(os.environ, {"TZ": "UTC"})
        patcher.start()
        self.addCleanup(patcher.stop)
        time.tzset()
        self.addCleanup(time.tzset)
        self.start, self.end = NOW - 600, NOW + 600

    def latest(self, ids=(), account=(USER, WORKSPACE), skew=None):
        if skew is None:
            return usage.latest(self.root, ids, account, self.start, self.end)
        return usage.latest(self.root, ids, account, self.start, self.end, skew=skew)

    def touch_all(self):
        for path in (self.root / "sessions").rglob("*"):
            if path.is_file():
                os.utime(path, (NOW, NOW))

    def test_a_new_session_of_this_account_gives_its_last_snapshot(self):
        rollout(self.root, THREAD, [(NOW - 60, limits(10.0, 30.0)), (NOW, limits(23.5, 41.0)), (NOW + 1, None)])
        self.touch_all()
        self.assertEqual(self.latest(), {
            "primary": {"used_percent": 23.5, "window_minutes": 300, "resets_at": NOW + 3600},
            "secondary": {"used_percent": 41.0, "window_minutes": 10080, "resets_at": NOW + 3600},
            "seen_at": NOW})

    def test_another_accounts_concurrent_session_is_ignored(self):
        rollout(self.root, OTHER, [(NOW, limits(90.0))], creator=("user-fixture-B", "workspace-fixture-B"))
        rollout(self.root, THREAD, [(NOW - 30, limits(5.0))])
        self.touch_all()
        self.assertEqual(self.latest()["primary"]["used_percent"], 5.0)
        # The same user in another workspace is another quota.
        self.assertIsNone(self.latest(account=(USER, "workspace-other")))
        # An API-key login has no creator to match.
        self.assertIsNone(self.latest(account=None))

    def test_a_resumed_thread_counts_by_its_id_whoever_created_it(self):
        old = rollout(self.root, RESUMED, [(NOW - 5 * 86400, limits(70.0))], day="2025/12/27",
                      creator=("user-fixture-B", "workspace-fixture-B"), stamp=NOW - 5 * 86400)
        self.touch_all()
        self.assertIsNone(self.latest(ids=(RESUMED,)), "a snapshot from before the run counted")
        with open(old, "a") as stream:
            stream.write(json.dumps({"timestamp": iso(NOW), "type": "event_msg",
                                     "payload": {"type": "token_count", "info": None, "rate_limits": limits(12.0)}}) + "\n")
        self.touch_all()
        self.assertIsNone(self.latest(), "a resumed thread of another creator counted without its id")
        self.assertEqual(self.latest(ids=(RESUMED,))["primary"]["used_percent"], 12.0)

    def test_compressed_null_foreign_and_stale_snapshots_are_skipped(self):
        rollout(self.root, OTHER, [(NOW, limits(99.0))], suffix=".zst")
        rollout(self.root, THREAD, [(NOW - 3600, limits(50.0)),  # before the run
                                    (NOW - 20, limits(8.0)),
                                    (NOW - 10, None),
                                    (NOW - 5, limits(77.0, limit_id="codex_bengalfox")),
                                    (NOW - 2, {"limit_id": "codex", "primary": {"used_percent": "high"}})])
        self.touch_all()
        self.assertEqual(self.latest(), {"primary": {"used_percent": 8.0, "window_minutes": 300, "resets_at": NOW + 3600},
                                         "secondary": None, "seen_at": NOW - 20})

    def test_files_untouched_during_the_run_are_not_read(self):
        path = rollout(self.root, THREAD, [(NOW, limits(30.0))])
        os.utime(path, (NOW - 3600, NOW - 3600))
        self.assertIsNone(self.latest())

    def test_skew_opens_a_rollout_whose_file_time_lags_the_run(self):
        """A file server's clock may lag this host's: SKEW seconds of it open a rollout whose file time
        lies that long before the run, by its id or by its creator. Without SKEW it stays closed."""
        path = rollout(self.root, THREAD, [(NOW, limits(21.0))])
        for ids in ((), (THREAD,)):
            with self.subTest(ids=ids):
                os.utime(path, (self.start - 30, self.start - 30))
                self.assertIsNone(self.latest(ids=ids))
                self.assertIsNone(self.latest(ids=ids, skew=0))
                self.assertIsNone(self.latest(ids=ids, skew=20))
                self.assertEqual(self.latest(ids=ids, skew=300)["primary"]["used_percent"], 21.0)
                # SKEW, and the SLACK every file time gets, bound it.
                os.utime(path, (self.start - 300 - usage.SLACK, self.start - 300 - usage.SLACK))
                self.assertEqual(self.latest(ids=ids, skew=300)["primary"]["used_percent"], 21.0)
                os.utime(path, (self.start - 301 - usage.SLACK, self.start - 301 - usage.SLACK))
                self.assertIsNone(self.latest(ids=ids, skew=300))

    def test_skew_never_widens_the_lines_that_count(self):
        """A snapshot counts by its own line's time, which Codex stamps with this host's clock: one from
        before the run is another run's reading, of this account or of another, whatever SKEW opens."""
        before = self.start - 60
        resumed = rollout(self.root, RESUMED, [(before, limits(90.0))], creator=("user-fixture-B", "workspace-fixture-B"),
                          stamp=before - 3600)
        own = rollout(self.root, THREAD, [(before, limits(80.0)), (self.start - 1, limits(81.0))], stamp=before)
        # Left as they were before the run, or written during it without a snapshot.
        for mtime in (before, NOW):
            for path in (resumed, own):
                os.utime(path, (mtime, mtime))
            for skew in (None, 300):
                with self.subTest(mtime=mtime - self.start, skew=skew):
                    self.assertIsNone(self.latest(ids=(RESUMED,), skew=skew))
        # The run's own lines in the same rollouts count, from its first second on.
        for path, when, used in ((resumed, NOW, 12.0), (own, self.start, 13.0)):
            with open(path, "a") as stream:
                stream.write(json.dumps({"timestamp": iso(when), "type": "event_msg",
                                         "payload": {"type": "token_count", "info": None, "rate_limits": limits(used)}}) + "\n")
            os.utime(path, (before, before))
        self.assertEqual(self.latest(skew=300), {"primary": {"used_percent": 13.0, "window_minutes": 300, "resets_at": NOW + 3600},
                                                 "secondary": None, "seen_at": self.start})
        self.assertEqual(self.latest(ids=(RESUMED,), skew=300)["primary"]["used_percent"], 12.0)

    def test_the_lines_bound_is_exact_below_a_second(self):
        """Native Codex stamps its lines in milliseconds: a snapshot stamped one millisecond before the
        start never counts, in a thread resumed by id or in one of this account's, and one stamped at the
        start does."""
        for ids in ((), (THREAD,)):
            with self.subTest(ids=ids):
                shutil.rmtree(self.root / "sessions", ignore_errors=True)
                path = rollout(self.root, THREAD, [], stamp=self.start - 3600)
                append(path, iso(self.start - 1, 999), limits(80.0))
                os.utime(path, (NOW, NOW))
                for skew in (None, 300):
                    self.assertIsNone(self.latest(ids=ids, skew=skew))
                append(path, iso(self.start), limits(13.0))
                os.utime(path, (NOW, NOW))
                self.assertEqual(self.latest(ids=ids, skew=300), {
                    "primary": {"used_percent": 13.0, "window_minutes": 300, "resets_at": NOW + 3600},
                    "secondary": None, "seen_at": self.start})

    def test_only_regular_rollouts_are_opened(self):
        own = rollout(self.root, THREAD, [(NOW - 10, limits(20.0))])
        # A FIFO would block the read after Codex exits; a link could point anywhere.
        fifo = own.with_name(f"rollout-2026-01-01T12-00-00-{OTHER}.jsonl")
        os.mkfifo(fifo)
        self.addCleanup(self.release, fifo)
        elsewhere = rollout(self.base_elsewhere(), RESUMED, [(NOW, limits(90.0))])
        own.with_name(elsewhere.name).symlink_to(elsewhere)
        self.touch_all()
        found = []
        scan = threading.Thread(target=lambda: found.append(self.latest(ids=(OTHER, RESUMED))), daemon=True)
        scan.start()
        scan.join(10)
        self.assertFalse(scan.is_alive(), "the scan blocked on a FIFO")
        self.assertEqual(found[0]["primary"]["used_percent"], 20.0)

    def base_elsewhere(self):
        path = self.root / "elsewhere"
        path.mkdir(exist_ok=True)
        return path

    @staticmethod
    def release(fifo):
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass

    def test_other_accounts_rollouts_never_crowd_this_ones_out(self):
        rollout(self.root, THREAD, [(NOW - 30, limits(15.0))])
        rollout(self.root, RESUMED, [(NOW - 20, limits(25.0))], creator=("user-fixture-B", "workspace-fixture-B"))
        self.touch_all()
        def others(count):
            for number in range(count):
                path = rollout(self.root, f"019a0000-0000-7000-8000-0000000010{number:02x}", [(NOW, limits(99.0))],
                               creator=("user-fixture-B", "workspace-fixture-B"))
                os.utime(path, (NOW + 60, NOW + 60))

        # The file limit counts only this account's rollouts.
        others(usage.MAX_FILES + 2)
        self.assertEqual(self.latest()["primary"]["used_percent"], 15.0)
        self.assertEqual(self.latest(ids=(RESUMED,))["primary"]["used_percent"], 25.0)
        # Past MAX_HEADS newer foreign rollouts the search for its own stops; a continued thread comes first.
        others(usage.MAX_HEADS)
        self.assertIsNone(self.latest())
        self.assertEqual(self.latest(ids=(RESUMED,))["primary"]["used_percent"], 25.0)

    def test_reset_times_beyond_the_calendar_are_skipped(self):
        rate = limits(30.0)
        rate["primary"]["resets_at"] = 10**19
        rollout(self.root, THREAD, [(NOW - 20, limits(10.0)), (NOW - 10, rate)])
        self.touch_all()
        self.assertEqual(self.latest()["primary"]["used_percent"], 10.0)

    def test_a_reading_above_100_percent_is_kept_as_100(self):
        # An exhausted window is still a window: the newest snapshot keeps it, never an older lower one.
        rollout(self.root, THREAD, [(NOW - 20, limits(80.0, 60.0)), (NOW - 10, limits(100.5, 101))])
        self.touch_all()
        self.assertEqual(self.latest(), {
            "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": NOW + 3600},
            "secondary": {"used_percent": 100.0, "window_minutes": 10080, "resets_at": NOW + 3600},
            "seen_at": NOW - 10})

    def test_only_the_tail_is_read(self):
        path = rollout(self.root, THREAD, [(NOW - 100, limits(40.0))])
        with open(path, "a") as stream:
            filler = json.dumps({"timestamp": iso(NOW - 50), "type": "response_item", "payload": {"text": "x" * 1000}}) + "\n"
            stream.write(filler * (usage.TAIL // len(filler) + 10))
        self.touch_all()
        # The snapshot lies before the last MiB; the session_meta line is still read from the head.
        self.assertIsNone(self.latest())
        with open(path, "a") as stream:
            stream.write(json.dumps({"timestamp": iso(NOW), "type": "event_msg",
                                     "payload": {"type": "token_count", "rate_limits": limits(41.0)}}) + "\n")
        self.touch_all()
        self.assertEqual(self.latest()["primary"]["used_percent"], 41.0)


def line(when, kind, payload=None, inherited=False):
    """One rollout line as native Codex writes it; INHERITED marks a sub-agent's copy of its parent's history."""
    record = {"timestamp": iso(when), "type": kind, "payload": {} if payload is None else payload}
    if inherited:
        record["metadata"] = {"inherited_user_message": True}
    return record


def count(when, rate="absent"):
    """A token_count event; RATE is its rate_limits object, None, or "absent" for no such key."""
    payload = {"type": "token_count", "info": {"total_token_usage": {}}}
    if rate != "absent":
        payload["rate_limits"] = rate
    return line(when, "event_msg", payload)


def item(when, kind, inherited=False, **fields):
    return line(when, "response_item", {"type": kind, **fields}, inherited)


def call(when, name="exec_command", namespace=None, inherited=False):
    fields = {"name": name, "arguments": "{}", "call_id": "call_1"}
    if namespace is not None:
        fields["namespace"] = namespace
    return item(when, "function_call", inherited, **fields)


def slot(used, minutes, resets=NOW + 3600):
    return {"used_percent": used, "window_minutes": minutes, "resets_at": resets}


def compaction(when):
    """What Codex writes for a compaction before a turn: compacted, turn_context, then a token_count without limits."""
    return [line(when, "compacted", {"message": "", "replacement_history": []}),
            line(when, "turn_context", {"cwd": "/", "model": "m"}), count(when)]


EMPTY = {"snapshot": None, "family": None, "accounted": False, "responded": False, "tools": False, "own": None}


class SessionRollout(unittest.TestCase):
    """usage.session: what one session's rollout shows since a launch started, for the reserve guard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-session-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.files = 0

    def write(self, records, last="\n", meta=None):
        """A rollout of RECORDS after its session_meta line; LAST ends the final line."""
        self.files += 1
        path = self.root / f"rollout-{self.files}.jsonl"
        payload = {"id": THREAD, "timestamp": iso(NOW - 3600), "cwd": "/", "cli_version": "0.169.0", **(meta or {})}
        lines = [json.dumps(record) for record in [line(NOW - 3600, "session_meta", payload), *records]]
        path.write_text("\n".join(lines) + last)
        return path

    def session(self, records, last="\n"):
        return usage.session(self.write(records, last), NOW)

    def test_nothing_since_the_start_shows_nothing(self):
        before = NOW - usage.SLACK - 1
        self.assertEqual(self.session([]), EMPTY)
        self.assertEqual(self.session([count(before, limits(99.0)), count(before), item(before, "reasoning"),
                                       call(before), line(before, "token_usage_record", {})]), EMPTY)
        # The slack before the start counts, for clocks that tick between the launcher and Codex.
        self.assertTrue(self.session([count(NOW - usage.SLACK)])["accounted"])

    def test_a_token_count_without_a_usable_window_is_accounted_but_no_snapshot(self):
        both_null = {"limit_id": "codex", "limit_name": None, "primary": None, "secondary": None, "credits": None}
        zero = dict(both_null, primary=slot(5.0, 0))
        for rate in ("absent", None, both_null, zero, {"limit_id": "codex", "primary": {"used_percent": "high"}}, []):
            with self.subTest(rate=rate):
                self.assertEqual(self.session([count(NOW + 1, rate)]), dict(EMPTY, accounted=True))

    def test_the_newest_snapshot_wins_with_its_time(self):
        found = self.session([count(NOW + 1, limits(10.0, 30.0)), count(NOW + 2, limits(20.0)), count(NOW + 3)])
        self.assertEqual(found, dict(EMPTY, accounted=True, snapshot={
            "primary": {"used_percent": 20.0, "window_minutes": 300, "resets_at": NOW + 3600},
            "secondary": None, "seen_at": NOW + 2}))

    def test_each_slot_is_read_on_its_own(self):
        # window_minutes 0 occurs: the line still counts by its other slot, and slots are kept as found.
        rate = dict(limits(None), primary=slot(40.0, 0), secondary=slot(60.0, 10080))
        snapshot = self.session([count(NOW + 1, rate)])["snapshot"]
        self.assertEqual(snapshot, {"primary": None, "secondary": slot(60.0, 10080), "seen_at": NOW + 1})
        weekly_first = dict(limits(None), primary=slot(70.0, 10080))
        self.assertEqual(self.session([count(NOW + 1, weekly_first)])["snapshot"]["primary"], slot(70.0, 10080))
        # A later line whose only window is unusable leaves the earlier snapshot in place.
        later = dict(limits(None), primary=slot(99.0, 0))
        found = self.session([count(NOW + 1, limits(15.0)), count(NOW + 2, later)])
        self.assertEqual((found["snapshot"]["primary"]["used_percent"], found["snapshot"]["seen_at"]), (15.0, NOW + 1))

    def test_a_reading_above_100_percent_counts_as_100(self):
        # The design's rule: a reading above 100 percent counts as 100, so an exhausted window is never dropped.
        rate = dict(limits(None), primary=slot(100.5, 300), secondary=slot(50.0, 10080))
        self.assertEqual(self.session([count(NOW + 1, rate)])["snapshot"],
                         {"primary": slot(100.0, 300), "secondary": slot(50.0, 10080), "seen_at": NOW + 1})
        lone = dict(limits(None), primary=slot(101, 10080))
        self.assertEqual(self.session([count(NOW + 1, lone)])["snapshot"]["primary"], slot(100.0, 10080))
        # What is no percentage at all is still no window.
        for used in (-0.5, float("nan"), float("inf"), "101", True):
            with self.subTest(used=used):
                rate = dict(limits(None), primary=slot(used, 300))
                self.assertEqual(self.session([count(NOW + 1, rate)]), dict(EMPTY, accounted=True))

    def test_the_family_comes_from_the_snapshot_line(self):
        for limit_id, family in (("spark", "spark"), ("codex_bengalfox", "codex_bengalfox"), ("codex", None),
                                 (None, None), (7, None), ("bad family\n", None)):
            with self.subTest(limit_id=limit_id):
                rate = limits(30.0, limit_id=limit_id)
                if limit_id is None:
                    del rate["limit_id"]
                self.assertEqual(self.session([count(NOW + 1, rate)])["family"], family)
        # A premium line has no window: it is not the snapshot and never names the family.
        premium = {"limit_id": "premium", "primary": None, "secondary": None,
                   "rate_limit_reached_type": "workspace_member_credits_depleted"}
        found = self.session([count(NOW + 1, limits(25.0)), count(NOW + 2, premium)])
        self.assertEqual((found["snapshot"]["seen_at"], found["family"], found["accounted"]), (NOW + 1, None, True))
        found = self.session([count(NOW + 1, limits(25.0, limit_id="spark")), count(NOW + 2, limits(26.0))])
        self.assertEqual((found["snapshot"]["primary"]["used_percent"], found["family"]), (26.0, None))

    def test_model_responses(self):
        responses = (item(NOW, "reasoning", summary=[]), item(NOW, "message", role="assistant", content=[]),
                     call(NOW), item(NOW, "custom_tool_call", name="exec", input=""),
                     item(NOW, "web_search_call", status="completed"), item(NOW, "tool_search_call"),
                     line(NOW, "token_usage_record", {"input_tokens": 1}))
        for record in responses:
            with self.subTest(record=record):
                self.assertTrue(self.session([record])["responded"])
        others = (item(NOW, "agent_message", author="/root", recipient="/root/a", content=[]),
                  item(NOW, "message", role="user", content=[]), item(NOW, "message", role="developer", content=[]),
                  item(NOW, "function_call_output", call_id="call_1", output=""),
                  item(NOW, "reasoning", inherited=True), item(NOW, "message", inherited=True, role="assistant"),
                  call(NOW, inherited=True), line(NOW, "event_msg", {"type": "agent_message", "message": ""}),
                  line(NOW, "turn_context", {}), line(NOW, "event_msg", {"type": "task_started"}))
        for record in others:
            with self.subTest(record=record):
                self.assertEqual(self.session([record]), EMPTY)

    def test_tool_calls_that_a_tool_hook_sees(self):
        tools = (call(NOW), call(NOW, "apply_patch"), call(NOW, "spawn_agent", "collaboration"),
                 call(NOW, "wait", "collaboration"), item(NOW, "custom_tool_call", name="exec", input=""),
                 item(NOW, "local_shell_call", action={}))
        for record in tools:
            with self.subTest(record=record):
                self.assertEqual(self.session([record]), dict(EMPTY, responded=True, tools=True))
        unseen = (call(NOW, "wait"), call(NOW, "write_stdin"), call(NOW, "wait", ""),
                  item(NOW, "web_search_call", status="completed"), item(NOW, "tool_search_call"))
        for record in unseen:
            with self.subTest(record=record):
                self.assertEqual(self.session([record]), dict(EMPTY, responded=True))
        for record in (call(NOW, inherited=True), item(NOW, "custom_tool_call", inherited=True, name="exec"),
                       item(NOW, "function_call_output", call_id="call_1", output="")):
            with self.subTest(record=record):
                self.assertEqual(self.session([record]), EMPTY)

    def test_a_compactions_token_count_is_not_accounted(self):
        self.assertEqual(self.session(compaction(NOW)), EMPTY)
        # As in a sub-agent whose inherited history is compacted at its start, then its first call.
        self.assertEqual(self.session([*compaction(NOW), item(NOW, "agent_message", author="/root"), call(NOW + 1)]),
                         dict(EMPTY, responded=True, tools=True))
        # Event lines between them do not matter; a response_item compaction counts like compacted.
        quiet = line(NOW, "event_msg", {"type": "item_completed", "item": {}})
        self.assertFalse(self.session([compaction(NOW)[0], quiet, compaction(NOW)[1], quiet, count(NOW)])["accounted"])
        self.assertFalse(self.session([item(NOW, "compaction", encrypted_content=""), *compaction(NOW)[1:]])["accounted"])
        # Only the token_count right after them is the compaction's.
        self.assertTrue(self.session([*compaction(NOW), item(NOW, "reasoning"), count(NOW + 1)])["accounted"])
        self.assertTrue(self.session([line(NOW, "turn_context", {}), count(NOW)])["accounted"])
        self.assertTrue(self.session([compaction(NOW)[0], count(NOW)])["accounted"])
        self.assertTrue(self.session([compaction(NOW)[0], item(NOW, "message", role="user"),
                                      *compaction(NOW)[1:]])["accounted"])
        # A compaction's token_count with rate limits is a snapshot all the same.
        found = self.session([*compaction(NOW)[:2], count(NOW, limits(12.0))])
        self.assertEqual((found["accounted"], found["snapshot"]["primary"]["used_percent"]), (True, 12.0))
        # A compaction before the start is still a compaction.
        self.assertFalse(self.session([*compaction(NOW - 60)[:2], count(NOW)])["accounted"])

    def test_inherited_lines_are_skipped_whatever_their_time(self):
        history = [item(NOW + 1, kind, inherited=True) for kind in ("reasoning", "function_call", "custom_tool_call")]
        self.assertEqual(self.session([*history, item(NOW + 1, "message", inherited=True, role="assistant")]), EMPTY)

    def tail_test(self, before, cut_record):
        """A rollout whose last TAIL bytes start exactly at CUT_RECORD, the end of a line cut by the tail."""
        path = self.write(before)
        cut = json.dumps(cut_record)
        filler = item(NOW + 10, "function_call_output", call_id="call_1", output="")
        rest = usage.TAIL - len(cut) - 1  # the tail is CUT, its newline and REST bytes
        filler["payload"]["output"] = "y" * (rest - len(json.dumps(filler)) - 1)
        with open(path, "a") as stream:
            stream.write("x" * 100 + cut + "\n" + json.dumps(filler) + "\n")
        with open(path, "rb") as stream:
            stream.seek(-usage.TAIL, os.SEEK_END)
            self.assertTrue(stream.read().startswith(cut.encode() + b"\n"))
        return usage.session(path, NOW)

    def test_a_cut_first_line_and_an_unfinished_last_line_are_skipped(self):
        # Read whole, the cut line would be a snapshot at 99 percent and a model response.
        self.assertEqual(self.tail_test([], count(NOW + 5, limits(99.0))), EMPTY)
        self.assertEqual(self.tail_test([], item(NOW + 5, "reasoning")), EMPTY)
        found = self.session([count(NOW + 1, limits(10.0))], last="\n" + json.dumps(count(NOW + 2, limits(99.0))))
        self.assertEqual(found["snapshot"]["primary"]["used_percent"], 10.0)
        self.assertEqual(self.session([], last="\n" + json.dumps(call(NOW + 2))[:-1]), EMPTY)

    def cut_then(self, records):
        """usage.session of a rollout whose tail starts inside a long line, then RECORDS."""
        path = self.write([])
        with open(path, "a") as stream:
            stream.write('{"type": "compacted", "payload": {"message": "' + "x" * usage.TAIL + '"}}\n')
            stream.write("".join(json.dumps(record) + "\n" for record in records))
        with open(path, "rb") as stream:
            stream.seek(-usage.TAIL, os.SEEK_END)
            self.assertNotIn(b'"compacted"', stream.read())
        return usage.session(path, NOW)

    def test_after_a_cut_line_a_compactions_token_count_is_not_accounted(self):
        # A compacted line can be megabytes long: the tail cuts it, and what follows it is still its own.
        self.assertEqual(self.cut_then([*compaction(NOW + 1)[1:], call(NOW + 2)]), dict(EMPTY, responded=True, tools=True))
        self.assertEqual(self.cut_then([count(NOW + 1)]), EMPTY)
        self.assertTrue(self.cut_then([item(NOW + 1, "message", role="user"), count(NOW + 1)])["accounted"])
        self.assertTrue(self.cut_then([line(NOW + 1, "turn_context", {}), count(NOW + 1), count(NOW + 2)])["accounted"])

    def test_other_lines_and_shapes_are_skipped(self):
        records = [count(NOW + 1, limits(30.0)), "not json", [1, 2],
                   {"timestamp": "yesterday", "type": "response_item", "payload": {"type": "reasoning"}},
                   {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}},
                   line(NOW + 2, "response_item", "text"), line(NOW + 2, "response_item", {"type": ["function_call"]}),
                   line(NOW + 2, "event_msg", {"type": "token_count", "rate_limits": 5})]
        path = self.write([])
        with open(path, "a") as stream:
            for record in records:
                stream.write((record if isinstance(record, str) else json.dumps(record)) + "\n")
            stream.write("[" * 100000 + "\n")  # deeper than the parser's recursion limit
        found = usage.session(path, NOW)
        self.assertEqual((found["snapshot"]["seen_at"], found["accounted"], found["responded"], found["tools"]),
                         (NOW + 1, True, False, False))

    def test_a_usage_line_without_a_readable_time_is_accounted(self):
        # Format drift: a token_count line or a model response whose time cannot be read is usage at a time
        # unknown. It is no snapshot and no response since the start, but it is accounted, so the guard is blind
        # instead of allowing what it cannot read.
        for stamp in (NOW + 1, (NOW + 1) * 1000, None, "soon", "absent"):
            with self.subTest(stamp=stamp):
                records = [item(NOW + 1, "message", role="assistant"), count(NOW + 1), count(NOW + 1, limits(97.0))]
                for record in records:
                    if stamp == "absent":
                        del record["timestamp"]
                    else:
                        record["timestamp"] = stamp
                self.assertEqual(self.session(records), dict(EMPTY, accounted=True))
                # A model response alone counts too: a new session's first tool call has a response and its
                # token_usage_record, and no token_count yet.
                for response in (item(NOW + 1, "message", role="assistant"), call(NOW + 1),
                                 line(NOW + 1, "token_usage_record", {"input_tokens": 1})):
                    if stamp == "absent":
                        del response["timestamp"]
                    else:
                        response["timestamp"] = stamp
                    self.assertEqual(self.session([response]), dict(EMPTY, accounted=True), response["type"])
        # Other lines without a readable time stay out, and so does an inherited one.
        others = [{"type": "turn_context", "payload": {}}, {"type": "event_msg", "payload": {"type": "task_started"}},
                  {"type": "response_item", "payload": {"type": "message", "role": "user"}},
                  dict(item(NOW + 1, "reasoning", inherited=True), timestamp=None)]
        self.assertEqual(self.session(others), EMPTY)

    def test_only_a_readable_regular_file_is_read(self):
        good = self.write([count(NOW + 1, limits(30.0))])
        link = self.root / "link.jsonl"
        link.symlink_to(good)
        fifo = self.root / "fifo.jsonl"
        os.mkfifo(fifo)
        self.addCleanup(CodexUsage.release, fifo)
        for path in (link, self.root, self.root / "missing.jsonl", fifo, str(self.root / "missing.jsonl")):
            with self.subTest(path=path):
                self.assertIsNone(usage.session(path, NOW))
        for path in (None, "", "bad\0path"):
            with self.subTest(path=path):
                self.assertIsNone(usage.session(path, NOW))
        self.assertEqual(usage.session(str(good), NOW)["snapshot"]["seen_at"], NOW + 1)
        unreadable = self.write([])
        unreadable.chmod(0)
        if os.geteuid() != 0:
            self.assertIsNone(usage.session(unreadable, NOW))


# Turn ids as native Codex makes them, UUIDs in lower case; the reset times of two accounts' weekly windows.
TURN_A, TURN_B, TURN_C, TURN_P = ("019a0000-0000-7000-8000-0000000000" + end for end in ("a1", "b2", "c3", "d4"))
OURS, THEIRS = NOW + 3600, NOW + 86400


def weekly(used, resets=OURS):
    """A rate_limits object of the general family whose only window is a weekly one."""
    return dict(limits(None), limit_id="codex", primary=slot(used, 10080, resets))


def event(when, kind, **fields):
    """An event_msg line of payload type KIND."""
    return line(when, "event_msg", {"type": kind, **fields})


def started(when, turn):
    """A turn's task_started line, its id in turn_id and in turn_attribution as codex-cli 0.171.0 writes it."""
    return event(when, "task_started", turn_id=turn, turn_attribution={"turn_id": turn}, model_context_window=None)


def context(when, turn):
    return line(when, "turn_context", {"turn_id": turn, "cwd": "/", "model": "m"})


def record(when, turn):
    """The token_usage_record line of a model response."""
    return line(when, "token_usage_record", {"turn_id": turn, "input_tokens": 1})


def complete(when, turn):
    return event(when, "task_complete", turn_id=turn, last_agent_message=None)


def aborted(when, turn):
    return event(when, "turn_aborted", turn_id=turn, reason="interrupted")


def completed(when, turn):
    """An item_completed line: it names the turn its item began in, which may have ended."""
    return event(when, "item_completed", turn_id=turn, item={})


def applied(when):
    """The thread_settings_applied line a client writes before its first turn; it names no turn."""
    return event(when, "thread_settings_applied", thread_id=THREAD)


def reply(when, turn, rate):
    """One model response of TURN with a tool call: the call, its token_usage_record, the call's
    output, then the token_count with RATE."""
    return [call(when), record(when, turn), item(when, "function_call_output", call_id="call_1", output=""),
            count(when, rate)]


def whole(when, turn, rate):
    """A whole turn of one client, stamped from WHEN on: one reply with RATE, then its end."""
    return [started(when, turn), context(when, turn), applied(when), item(when, "message", role="user", content=[]),
            *reply(when + 1, turn, rate), complete(when + 2, turn)]


def used(own):
    """The weekly percentage of an own view's snapshot, or what stands in for it."""
    return own and own["snapshot"] and own["snapshot"]["primary"]["used_percent"]


# An own view that holds nothing: the five keys over no line, and no reading before a cut tail's first naming line.
NOTHING = {"snapshot": None, "family": None, "accounted": False, "responded": False, "tools": False, "before": None}


class Turns(unittest.TestCase):
    """usage.session with a launch's turns: the own view, the lines in the turns its hooks named."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-turns-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.files = 0

    def write(self, records):
        self.files += 1
        path = self.root / f"rollout-{self.files}.jsonl"
        meta = {"id": THREAD, "timestamp": iso(NOW - 3600), "cwd": "/", "cli_version": "0.171.0"}
        path.write_text("".join(json.dumps(record) + "\n" for record in [line(NOW - 3600, "session_meta", meta), *records]))
        return path

    def own(self, records, turns):
        return usage.session(self.write(records), NOW, set(turns))["own"]

    def cut(self, records, turns):
        """The own view of a rollout whose tail starts inside a long line, then RECORDS."""
        path = self.write([])
        with open(path, "a") as stream:
            stream.write(json.dumps(item(NOW, "function_call_output", call_id="call_0", output="x" * usage.TAIL)) + "\n")
            stream.write("".join(json.dumps(record) + "\n" for record in records))
        return usage.session(path, NOW, set(turns))["own"]

    def test_a_line_counts_in_a_turn_the_launch_names(self):
        self.assertEqual(self.own(whole(NOW + 10, TURN_A, weekly(40.0)), {TURN_A}), {
            "snapshot": {"primary": slot(40.0, 10080), "secondary": None, "seen_at": NOW + 11},
            "family": None, "accounted": True, "responded": True, "tools": True, "turns": {TURN_A}, "before": None})
        # No time bound: a turn's lines are the launch's whenever they are stamped.
        self.assertEqual(used(self.own(whole(NOW - 7200, TURN_A, weekly(41.0)), {TURN_A})), 41.0)

    def test_another_client_s_turn_in_the_same_rollout_never_counts(self):
        theirs = whole(NOW + 10, TURN_B, weekly(83.0, THEIRS))
        mine = whole(NOW + 20, TURN_A, weekly(40.0))
        later = whole(NOW + 30, TURN_C, weekly(85.0, THEIRS))
        found = usage.session(self.write([*theirs, *mine, *later]), NOW, {TURN_A})
        self.assertEqual(found["snapshot"]["primary"]["used_percent"], 85.0)  # by time: the other client's
        self.assertEqual(used(found["own"]), 40.0)
        self.assertEqual(used(self.own([*theirs, *mine, *later], {TURN_B})), 83.0)
        # The launch's turn before its first reply holds nothing of the other client's.
        first = self.own([*theirs, applied(NOW + 20), started(NOW + 20, TURN_A), context(NOW + 20, TURN_A)], {TURN_A})
        self.assertEqual(first, {**NOTHING, "turns": {TURN_A}})

    def test_the_turn_id_is_read_from_task_started_also_inside_turn_attribution(self):
        nested = event(NOW + 1, "task_started", turn_attribution={"turn_id": TURN_A})
        self.assertEqual(used(self.own([nested, call(NOW + 2), count(NOW + 3, weekly(41.0))], {TURN_A})), 41.0)
        for direct in (None, 7, [TURN_B], {"turn_id": TURN_B}):
            with self.subTest(direct=direct):
                payload = {"type": "task_started", "turn_id": direct, "turn_attribution": {"turn_id": TURN_A}}
                self.assertEqual(usage.turn_of("event_msg", "task_started", payload), TURN_A)
        # A turn_id that is a string decides, also one that names no turn.
        for direct, turn in ((TURN_B, TURN_B), ("1", None)):
            payload = {"type": "task_started", "turn_id": direct, "turn_attribution": {"turn_id": TURN_A}}
            self.assertEqual(usage.turn_of("event_msg", "task_started", payload), turn)
        # Only task_started carries the attribution.
        for kind, ptype in (("turn_context", None), ("token_usage_record", None), ("event_msg", "task_complete"),
                            ("event_msg", "turn_aborted")):
            with self.subTest(kind=kind, ptype=ptype):
                self.assertIsNone(usage.turn_of(kind, ptype, {"type": ptype, "turn_attribution": {"turn_id": TURN_A}}))

    def test_only_a_uuid_names_a_turn(self):
        naming = (("event_msg", "task_started"), ("turn_context", None), ("token_usage_record", None),
                  ("event_msg", "task_complete"), ("event_msg", "turn_aborted"))
        for kind, ptype in naming:
            self.assertEqual(usage.turn_of(kind, ptype, {"type": ptype, "turn_id": TURN_A}), TURN_A)
            for value in ("1", "auto-compact-0", TURN_A.upper(), TURN_A + "\n", " " + TURN_A, TURN_A[:-1] + "\u0661",
                          7, 1.5, True, None, [TURN_A], {"turn_id": TURN_A}):
                with self.subTest(kind=kind, ptype=ptype, value=value):
                    self.assertIsNone(usage.turn_of(kind, ptype, {"type": ptype, "turn_id": value}))
        for attribution in (TURN_A, [TURN_A], {"turn_id": "auto-compact-1"}, {"turn_id": TURN_A.upper()}):
            with self.subTest(attribution=attribution):
                self.assertIsNone(usage.turn_of("event_msg", "task_started", {"turn_attribution": attribution}))
        for payload in (None, [], "x", 7):
            self.assertIsNone(usage.turn_of("turn_context", None, payload))
        self.assertIsNone(usage.turn_of(["turn_context"], None, {"turn_id": TURN_A}))
        # A rollout whose ids are none of them a UUID has no own view.
        odd = [event(NOW + 1, "task_started", turn_id="1"), call(NOW + 2), line(NOW + 2, "token_usage_record", {"turn_id": "1"}),
               count(NOW + 3, weekly(83.0))]
        self.assertIsNone(self.own(odd, {"1"}))
        # auto-compact-N, which repeats in every session, on a turn_context between turns changes nothing.
        compact = line(NOW + 5, "turn_context", {"turn_id": "auto-compact-0"})
        mine = whole(NOW + 1, TURN_A, weekly(40.0))
        self.assertEqual(used(self.own([*mine, compact, count(NOW + 5, weekly(83.0, THEIRS))], {TURN_A, "auto-compact-0"})), 40.0)

    def test_an_item_completed_line_never_names_a_turn(self):
        mine = whole(NOW + 1, TURN_A, weekly(40.0))
        later = [started(NOW + 10, TURN_B), completed(NOW + 11, TURN_A), call(NOW + 12), count(NOW + 13, weekly(83.0, THEIRS))]
        self.assertEqual(used(self.own(mine + later, {TURN_A})), 40.0)
        after_end = [completed(NOW + 10, TURN_A), call(NOW + 11), count(NOW + 12, weekly(83.0, THEIRS))]
        self.assertEqual(used(self.own(mine + after_end, {TURN_A})), 40.0)
        for kind, ptype in (("event_msg", "item_completed"), ("event_msg", "token_count"), ("event_msg", "turn_done"),
                            ("event_msg", "thread_settings_applied"), ("response_item", "function_call"),
                            ("session_meta", None), ("compacted", None), ("world_state", None)):
            with self.subTest(kind=kind, ptype=ptype):
                self.assertIsNone(usage.turn_of(kind, ptype, {"type": ptype, "turn_id": TURN_A}))

    def test_a_token_count_counts_only_after_a_response_of_its_turn(self):
        # Before the turn's first response a token_count can repeat what the process received in an earlier turn.
        self.assertEqual(self.own([started(NOW + 1, TURN_A), count(NOW + 2, weekly(83.0, THEIRS))], {TURN_A}),
                         {**NOTHING, "turns": {TURN_A}})
        # A response of an earlier turn does not make it a reply's reading.
        mine = whole(NOW + 1, TURN_A, weekly(40.0))
        self.assertEqual(used(self.own([*mine, started(NOW + 10, TURN_C), count(NOW + 11, weekly(83.0, THEIRS))],
                                       {TURN_A, TURN_C})), 40.0)
        # A turn interrupted after a response: the interrupt stamps what the client itself received.
        interrupted = [started(NOW + 1, TURN_A), *reply(NOW + 2, TURN_A, weekly(40.0)), count(NOW + 3, weekly(41.0)),
                       aborted(NOW + 4, TURN_A)]
        self.assertEqual(used(self.own(interrupted, {TURN_A})), 41.0)

    def test_a_token_count_between_turns_belongs_to_no_turn(self):
        mine = whole(NOW + 1, TURN_A, weekly(40.0))
        self.assertEqual(used(self.own([*mine, count(NOW + 10, weekly(83.0, THEIRS))], {TURN_A})), 40.0)
        quiet = [started(NOW + 1, TURN_A), call(NOW + 2), record(NOW + 2, TURN_A), complete(NOW + 3, TURN_A), count(NOW + 4)]
        found = self.own(quiet, {TURN_A})
        self.assertEqual((found["accounted"], found["responded"]), (False, True))

    def test_a_turn_without_an_end_line_ends_at_the_next_turn(self):
        # A client ended by a signal writes no end line; the next client writes its settings, then its turn.
        killed = [started(NOW + 1, TURN_A), *reply(NOW + 2, TURN_A, weekly(40.0)), applied(NOW + 5), started(NOW + 6, TURN_B),
                  *reply(NOW + 7, TURN_B, weekly(83.0, THEIRS))]
        self.assertEqual(used(self.own(killed, {TURN_A})), 40.0)
        self.assertEqual(used(self.own(killed, {TURN_B})), 83.0)
        # A task_started of the same id starts the turn again: a token_count before its first response is no reply's.
        again = [started(NOW + 1, TURN_A), *reply(NOW + 2, TURN_A, weekly(40.0)), started(NOW + 5, TURN_A),
                 count(NOW + 6, weekly(83.0, THEIRS))]
        self.assertEqual(used(self.own(again, {TURN_A})), 40.0)

    def test_an_end_event_ends_the_open_turn_whatever_id_it_carries(self):
        opened = [started(NOW + 1, TURN_A), *reply(NOW + 2, TURN_A, weekly(40.0))]
        for end in (aborted(NOW + 3, None), event(NOW + 3, "turn_aborted"), complete(NOW + 3, TURN_B),
                    event(NOW + 3, "task_complete", turn_id="auto-compact-0")):
            with self.subTest(end=end):
                self.assertEqual(used(self.own([*opened, end, count(NOW + 4, weekly(83.0, THEIRS))], {TURN_A})), 40.0)
        # An end event of another name ends nothing: it loses only the lines between turns, up to the next task_started.
        renamed = [*opened, event(NOW + 3, "turn_done", turn_id=TURN_A), count(NOW + 4, weekly(41.0)), started(NOW + 5, TURN_B),
                   *reply(NOW + 6, TURN_B, weekly(83.0, THEIRS))]
        self.assertEqual(used(self.own(renamed, {TURN_A})), 41.0)

    def test_a_copied_head_names_the_parent_s_turns_and_holds_nothing(self):
        # A sub-agent's rollout starts with a copy of its parent's history: turn lines unmarked, response items
        # marked inherited, a turn_aborted with a null id, and no token line.
        head = [started(NOW - 600, TURN_P), context(NOW - 600, TURN_P), item(NOW - 599, "message", inherited=True, role="user"),
                call(NOW - 599, inherited=True), item(NOW - 599, "message", inherited=True, role="assistant"),
                complete(NOW - 598, TURN_P), aborted(NOW - 597, None)]
        own = [started(NOW + 1, TURN_C), context(NOW + 1, TURN_C), *reply(NOW + 2, TURN_C, weekly(11.0))]
        self.assertEqual(self.own(head + own, {TURN_P}), {**NOTHING, "turns": {TURN_P}})
        self.assertEqual(used(self.own(head + own, {TURN_C})), 11.0)

    def test_a_tail_cut_inside_a_turn_counts_from_the_first_line_that_names_it(self):
        self.assertEqual(used(self.cut([record(NOW + 1, TURN_A), count(NOW + 2, weekly(42.0))], {TURN_A})), 42.0)
        # What comes before the first line that names a turn belongs to none: no snapshot, nothing accounted, no
        # call of the turn. The newest reading there is kept apart as "before".
        before = [call(NOW + 1), count(NOW + 2, weekly(83.0))]
        self.assertIsNone(self.cut(before, {TURN_A}))
        self.assertEqual(self.cut([*before, record(NOW + 3, TURN_A)], {TURN_A}),
                         {**NOTHING, "responded": True, "turns": {TURN_A},
                          "before": {"snapshot": reading(NOW + 2, 83.0), "family": None}})
        found = self.cut([*before, record(NOW + 3, TURN_A), count(NOW + 4, weekly(42.0))], {TURN_A})
        self.assertEqual((used(found), found["before"]), (42.0, {"snapshot": reading(NOW + 2, 83.0), "family": None}))
        self.assertEqual(used(self.cut([record(NOW + 1, TURN_A), count(NOW + 2, weekly(42.0)), *reply(NOW + 3, TURN_A, weekly(43.0))],
                                       {TURN_A})), 43.0)

    def test_a_cut_tail_has_a_reading_before_its_first_naming_line_only_inside_a_noted_turn(self):
        head, named = [call(NOW + 1), count(NOW + 2, weekly(83.0))], record(NOW + 3, TURN_A)
        kept = {"snapshot": reading(NOW + 2, 83.0), "family": None}
        self.assertEqual(self.cut([*head, named], {TURN_A})["before"], kept)
        # The newest such reading, with its own line's time and its own family; a line that names no turn in UUID
        # form (an automatic compaction's turn_context) leaves it.
        spark = dict(weekly(30.0), limit_id="codex_bengalfox")
        compact = line(NOW + 2, "turn_context", {"turn_id": "auto-compact-0"})
        self.assertEqual(self.cut([count(NOW, weekly(82.0)), *head, compact, named], {TURN_A})["before"], kept)
        self.assertEqual(self.cut([*head, count(NOW + 2, spark), named], {TURN_A})["before"],
                         {"snapshot": reading(NOW + 2, 30.0), "family": "codex_bengalfox"})
        # A token_count without a usable window, an inherited one and one whose time cannot be read are passed over.
        copied = dict(count(NOW + 2, weekly(84.0)), metadata={"inherited_user_message": True})
        for odd in (count(NOW + 2), count(NOW + 2, None), count(NOW + 2, limits(None)), copied,
                    dict(count(0, weekly(84.0)), timestamp=None), dict(count(0, weekly(84.0)), timestamp="soon")):
            with self.subTest(odd=odd):
                self.assertEqual(self.cut([*head, odd, named], {TURN_A})["before"], kept)
                self.assertIsNone(self.cut([call(NOW + 1), odd, named], {TURN_A})["before"])
        # Not where the tail is not cut: there the lines before the first naming line are another turn's.
        self.assertIsNone(self.own([*head, named], {TURN_A})["before"])
        # Not where a turn ended or began before the first naming line, whatever id the line carries, before or after
        # the reading.
        for edge in (complete(NOW + 2, None), aborted(NOW + 2, None), event(NOW + 2, "turn_aborted"),
                     complete(NOW + 2, "auto-compact-0"), event(NOW + 2, "task_started", turn_id="1"),
                     event(NOW + 2, "task_started")):
            with self.subTest(edge=edge):
                self.assertIsNone(self.cut([*head, edge, named], {TURN_A})["before"])
                self.assertIsNone(self.cut([edge, *head, named], {TURN_A})["before"])
        # Not where the first naming line is a task_started, even of a noted turn, or names a turn outside TURNS; an end
        # event of a noted turn is that turn's end, and what precedes it is the turn's.
        self.assertIsNone(self.cut([*head, started(NOW + 3, TURN_A), named], {TURN_A})["before"])
        for first in (record(NOW + 3, TURN_B), context(NOW + 3, TURN_B), complete(NOW + 3, TURN_B)):
            with self.subTest(first=first):
                self.assertIsNone(self.cut([*head, first, named], {TURN_A})["before"])
        self.assertEqual(self.cut([*head, complete(NOW + 3, TURN_A), started(NOW + 4, TURN_C), context(NOW + 4, TURN_C)],
                                  {TURN_A, TURN_C})["before"], kept)
        # A sub-agent's copied head names its parent's turn, which is not noted for its rollout.
        self.assertIsNone(self.cut([*head, context(NOW + 3, TURN_P), complete(NOW + 3, TURN_P), aborted(NOW + 3, None),
                                    started(NOW + 4, TURN_C), context(NOW + 4, TURN_C)], {TURN_C})["before"])
        # It never changes the rest of the own view, nor the lines since the start.
        path = self.write([])
        with open(path, "a") as stream:
            stream.write(json.dumps(item(NOW, "function_call_output", call_id="call_0", output="x" * usage.TAIL)) + "\n")
            stream.write("".join(json.dumps(record) + "\n" for record in [*head, named]))
        found = usage.session(path, NOW, {TURN_A})
        self.assertEqual(found["own"], {**NOTHING, "responded": True, "turns": {TURN_A}, "before": kept})
        self.assertEqual(dict(found, own=None), usage.session(path, NOW))

    def test_a_tail_that_names_no_turn_has_no_own_view(self):
        for records in ([], [call(NOW + 1), count(NOW + 2, weekly(40.0))],
                        [completed(NOW + 1, TURN_A), call(NOW + 2), count(NOW + 3, weekly(40.0))],
                        [line(NOW + 1, "turn_context", {"turn_id": TURN_A}, inherited=True), call(NOW + 2), count(NOW + 3, weekly(40.0))]):
            with self.subTest(records=records):
                self.assertIsNone(self.own(records, {TURN_A}))
        # Without turns there is no own view at all.
        path = self.write(whole(NOW + 1, TURN_A, weekly(40.0)))
        self.assertIsNone(usage.session(path, NOW)["own"])
        self.assertIsNone(usage.session(path, NOW, None)["own"])

    def test_a_tail_that_names_only_other_turns_has_an_empty_own_view(self):
        theirs = whole(NOW + 1, TURN_B, weekly(83.0, THEIRS))
        self.assertEqual(self.own(theirs, {TURN_A}), {**NOTHING, "turns": set()})
        self.assertEqual(self.own(theirs, set()), {**NOTHING, "turns": set()})

    def test_a_snapshot_carries_the_time_of_its_reply(self):
        # The token_count line is stamped when the response's call has ended: an hour later here.
        late = [started(NOW + 1, TURN_A), call(NOW + 2), record(NOW + 3, TURN_A), count(NOW + 3603, weekly(90.0))]
        self.assertEqual(self.own(late, {TURN_A})["snapshot"]["seen_at"], NOW + 3)
        # Each reply takes its own record's time, as a whole second.
        second = [call(NOW + 3604), dict(record(0, TURN_A), timestamp=iso(NOW + 3700, 900)), count(NOW + 3800, weekly(91.0))]
        self.assertEqual(self.own(late + second, {TURN_A})["snapshot"], {"primary": slot(91.0, 10080), "secondary": None,
                                                                         "seen_at": NOW + 3700})
        # Without a record in the turn, the line's own time; a record of an earlier turn is not this one's.
        bare = [started(NOW + 1, TURN_A), item(NOW + 2, "reasoning", summary=[]), count(NOW + 5, weekly(40.0))]
        self.assertEqual(self.own(bare, {TURN_A})["snapshot"]["seen_at"], NOW + 5)
        after = [complete(NOW + 3604, TURN_A), started(NOW + 3610, TURN_C), item(NOW + 3611, "reasoning", summary=[]),
                 count(NOW + 3620, weekly(92.0))]
        self.assertEqual(self.own(late + after, {TURN_A, TURN_C})["snapshot"]["seen_at"], NOW + 3620)

    def test_blindness_is_told_from_the_launch_s_turns_alone(self):
        # Another client's reply without rate limits makes the lines by time blind, never the own view.
        theirs = [started(NOW + 1, TURN_B), call(NOW + 2), record(NOW + 2, TURN_B), count(NOW + 3), complete(NOW + 4, TURN_B)]
        found = usage.session(self.write([*theirs, applied(NOW + 5), started(NOW + 5, TURN_A)]), NOW, {TURN_A})
        self.assertEqual((found["snapshot"], found["accounted"], found["responded"]), (None, True, True))
        self.assertEqual(found["own"], {**NOTHING, "turns": {TURN_A}})
        # The launch's own reply without rate limits is.
        mine = self.own([started(NOW + 1, TURN_A), call(NOW + 2), record(NOW + 2, TURN_A), count(NOW + 3)], {TURN_A})
        self.assertEqual(mine, {**NOTHING, "accounted": True, "responded": True, "tools": True, "turns": {TURN_A}})
        # A compaction's token_count in the launch's turn is not.
        compacted = [started(NOW + 1, TURN_A), line(NOW + 2, "compacted", {"message": "", "replacement_history": []}),
                     context(NOW + 2, TURN_A), count(NOW + 2)]
        self.assertEqual(self.own(compacted, {TURN_A}), {**NOTHING, "turns": {TURN_A}})
        # A token_count with a usable window whose time cannot be read is accounted and no snapshot.
        drift = [started(NOW + 1, TURN_A), call(NOW + 2), dict(count(0, weekly(40.0)), timestamp=None)]
        self.assertEqual(self.own(drift, {TURN_A}), {**NOTHING, "accounted": True, "responded": True, "tools": True,
                                                     "turns": {TURN_A}})

    def test_the_own_view_names_the_turns_the_tail_carries(self):
        lines = whole(NOW + 1, TURN_A, weekly(40.0)) + whole(NOW + 10, TURN_B, weekly(83.0, THEIRS))
        for turns, named in (({TURN_A, TURN_C}, {TURN_A}), ({TURN_A, TURN_B}, {TURN_A, TURN_B}), ({TURN_C}, set())):
            with self.subTest(turns=turns):
                self.assertEqual(self.own(lines, turns)["turns"], named)
        # Back in a thread that another client continued: the launch's newest turn decides.
        self.assertEqual(used(self.own(lines + whole(NOW + 20, TURN_C, weekly(44.0)), {TURN_A, TURN_C})), 44.0)

    def test_the_lines_since_the_start_are_as_before_beside_the_own_view(self):
        records = [*whole(NOW - 60, TURN_B, weekly(83.0, THEIRS)), *whole(NOW + 1, TURN_A, weekly(40.0)), count(NOW + 5),
                   *compaction(NOW + 6), item(NOW + 7, "reasoning", inherited=True), started(NOW + 8, TURN_C)]
        path = self.write(records)
        plain = usage.session(path, NOW)
        self.assertEqual(plain, dict(EMPTY, accounted=True, responded=True, tools=True,
                                     snapshot={"primary": slot(40.0, 10080), "secondary": None, "seen_at": NOW + 2}))
        for turns in ({TURN_A}, {TURN_B}, {TURN_C}, set()):
            with self.subTest(turns=turns):
                found = usage.session(path, NOW, turns)
                self.assertIsNotNone(found["own"])
                self.assertEqual(dict(found, own=None), plain)


REL_A = f"2026/01/01/rollout-2026-01-01T12-00-00-{THREAD}.jsonl"
TAG = "5f0c2a9e41d7b386"


class Notes(unittest.TestCase):
    """The notes of a launch's turns (STATE.turns), the tag of a login, and REL of a transcript."""

    def test_relative_is_a_day_path_below_sessions(self):
        for path in (f"/home/u/.codex/sessions/{REL_A}", Path(f"/home/u/.codex/sessions/{REL_A}"), f"/sessions/{REL_A}"):
            with self.subTest(path=path):
                self.assertEqual(usage.relative(path), REL_A)
        fork = REL_A.replace(".jsonl", f"_{OTHER}.jsonl")
        self.assertEqual(usage.relative(f"/c/sessions/{fork}"), fork)
        digit = REL_A.replace("T12-", "T1\u0661-")  # a digit outside ASCII
        for path in (f"/home/u/.codex/archived_sessions/{REL_A}", f"/c/sessions/x/{REL_A}", f"sessions/{REL_A}", REL_A,
                     f"/c/sessions/{digit}", f"/c/sessions/{REL_A}.zst", f"/c/sessions/{REL_A.upper()}",
                     f"/c/sessions/{REL_A}\n", f"/c/sessions/{REL_A}/", "", None, 7, f"/c/sessions/{REL_A}".encode()):
            with self.subTest(path=path):
                self.assertIsNone(usage.relative(path))

    def test_turn_notes_reads_the_owner_and_the_turns_by_rollout(self):
        other = REL_A.replace(THREAD, OTHER)
        raw = (f"o {TAG}\nt {TURN_A} {REL_A}\nt {TURN_B} {REL_A}\nt {TURN_A} {other}\nt {TURN_A} {REL_A}\n").encode()
        self.assertEqual(usage.turn_notes(raw), (TAG, {REL_A: {TURN_A, TURN_B}, other: {TURN_A}}))
        self.assertEqual(usage.turn_notes(f"o {TAG}\n".encode()), (TAG, {}))
        self.assertEqual(usage.turn_notes(bytearray(raw)), usage.turn_notes(raw))

    def test_turn_notes_skips_what_it_cannot_read(self):
        owner, good = f"o {TAG}\n".encode(), f"t {TURN_A} {REL_A}\n".encode()
        found = (TAG, {REL_A: {TURN_A}})
        cases = (
            (b"", (None, {})),
            (owner + b"x a later kind\n" + good + b"t\n\n" + f"t {TURN_B}\n".encode(), found),  # other kinds and shapes
            (owner + good + f"t {TURN_B} {REL_A}".encode(), found),  # a torn last line
            (owner[:-1], (None, {})),
            (owner + f"t {TURN_B} {REL_A}\xff\n".encode("latin-1") + good, found),  # bytes outside ASCII
            (owner + f"t {TURN_B} {REL_A.replace('T12-', 'T1' + chr(0x661) + '-')}\n".encode() + good, found),
            (good + owner, (None, {REL_A: {TURN_A}})),  # an owner line that is not the first
            (owner + b"o 0000000000000000\n" + good, found),
            (owner.upper() + good, (None, {REL_A: {TURN_A}})),
            (f"o {TAG} \n".encode() + good, (None, {REL_A: {TURN_A}})),
            (owner + f"t {TURN_B} {REL_A} more\n".encode() + f"t  {TURN_B} {REL_A}\n".encode() + f"t {TURN_B}\t{REL_A}\n".encode()
             + f"t {TURN_B} {REL_A}\r\n".encode() + f"t {TURN_B.upper()} {REL_A}\n".encode() + f"t {TURN_B} ../{REL_A}\n".encode()
             + good, found),
        )
        for raw, want in cases:
            with self.subTest(raw=raw):
                self.assertEqual(usage.turn_notes(raw), want)
        for raw in (None, f"o {TAG}\n", 7):
            with self.subTest(raw=raw):
                self.assertEqual(usage.turn_notes(raw), (None, {}))

    def test_a_turn_note_is_one_ascii_line(self):
        self.assertEqual(usage.turn_note(TURN_A, REL_A), f"t {TURN_A} {REL_A}\n".encode("ascii"))
        longest = REL_A.replace(".jsonl", f"_{OTHER}.jsonl")
        note = usage.turn_note(TURN_A, longest)
        self.assertLessEqual(len(note), 160)
        self.assertEqual((note.count(b"\n"), note.endswith(b"\n")), (1, True))
        self.assertEqual(usage.turn_notes(f"o {TAG}\n".encode() + note), (TAG, {longest: {TURN_A}}))
        for turn, rel in ((TURN_A.upper(), REL_A), ("auto-compact-0", REL_A), (None, REL_A), (7, REL_A), (TURN_A, None),
                          (TURN_A, "../x"), (TURN_A, f"sessions/{REL_A}"), (TURN_A, REL_A + f"\nt {TURN_B} {REL_A}"),
                          (TURN_A, Path(REL_A)), (TURN_A + " ", REL_A)):
            with self.subTest(turn=turn, rel=rel):
                with self.assertRaises(ValueError):
                    usage.turn_note(turn, rel)

    def test_tag_is_sixteen_hex_digits_of_user_and_workspace(self):
        import hashlib

        tag = usage.tag((USER, WORKSPACE))
        self.assertEqual(tag, hashlib.sha256(f"{USER}\n{WORKSPACE}".encode("utf-8")).hexdigest()[:16])
        self.assertTrue(usage.OWNER.fullmatch(tag))
        self.assertNotEqual(usage.tag((USER, "workspace-other")), tag)
        self.assertEqual(usage.tag(("\u00e9", "w")), hashlib.sha256("\u00e9\nw".encode("utf-8")).hexdigest()[:16])
        self.assertIsNone(usage.tag(None))
        # The guard loads usage.py at every event: hashlib only when a tag is made.
        self.assertNotIn("hashlib", vars(usage))


def reading(seen_at, used, minutes=10080, resets=OURS, secondary=None):
    """A snapshot as usage.session builds one: one window in the first slot."""
    return {"primary": slot(used, minutes, resets), "secondary": secondary, "seen_at": seen_at}


class Merged(unittest.TestCase):
    """usage.merged and usage.keep: the account's attested usage, guard/codex-usage-NAME.json."""

    LAUNCH = "codex-as-A-4242-1767268000"

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o077))
        tmp = tempfile.TemporaryDirectory(prefix="kit-attested-test-")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name).resolve() / "guard"
        self.dir.mkdir()
        self.dir.chmod(0o700)
        self.path = self.dir / "codex-usage-A.json"

    def keep(self, snapshot, now=NOW, owner=TAG, path=None):
        return usage.keep(accounts, self.path if path is None else path, owner, snapshot, now, REL_A, self.LAUNCH)

    def stored(self):
        return json.loads(self.path.read_text())

    def test_the_newest_reading_wins(self):
        self.assertEqual(usage.merged(None, reading(NOW, 10.0), NOW), reading(NOW, 10.0))
        self.assertEqual(usage.merged(reading(NOW - 900, 95.0), reading(NOW, 90.0), NOW), reading(NOW, 90.0))
        for seen_at in (NOW, NOW - 1):
            with self.subTest(seen_at=seen_at - NOW):
                self.assertIsNone(usage.merged(reading(NOW, 95.0), reading(seen_at, 90.0), NOW))
        # A stored usage without a whole seen_at is none, windows and all.
        five = slot(60.0, 300, NOW + 600)
        for stored in ({}, [], "x", {"primary": five, "secondary": None}, {"primary": five, "secondary": None, "seen_at": float(NOW - 10)},
                       {"primary": five, "secondary": None, "seen_at": True}, {"primary": five, "secondary": None, "seen_at": str(NOW)}):
            with self.subTest(stored=stored):
                self.assertEqual(usage.merged(stored, reading(NOW - 3600, 20.0), NOW), reading(NOW - 3600, 20.0))

    def test_a_window_the_snapshot_does_not_name_is_kept_by_its_length_until_it_resets(self):
        five = slot(60.0, 300, NOW + 600)
        stored = {"primary": five, "secondary": slot(30.0, 10080), "seen_at": NOW - 10}
        self.assertEqual(usage.merged(stored, reading(NOW, 31.0), NOW), reading(NOW, 31.0, secondary=five))
        # By its length, not by its slot.
        stored = {"primary": slot(30.0, 10080), "secondary": None, "seen_at": NOW - 10}
        incoming = {"primary": None, "secondary": slot(61.0, 300, NOW + 600), "seen_at": NOW}
        self.assertEqual(usage.merged(stored, incoming, NOW), dict(incoming, primary=slot(30.0, 10080)))
        # Until it resets; a window without a reset time or a length, or one that is no window, is not kept.
        for gone in (dict(five, resets_at=NOW), dict(five, resets_at=NOW - 1), dict(five, resets_at=None),
                     dict(five, window_minutes=None), dict(five, used_percent="x")):
            with self.subTest(gone=gone):
                self.assertEqual(usage.merged({"primary": gone, "secondary": None, "seen_at": NOW - 10}, reading(NOW, 31.0), NOW),
                                 reading(NOW, 31.0))
        # A length the snapshot names is the snapshot's; with both slots taken a third length has no room.
        both = {"primary": slot(62.0, 300, NOW + 600), "secondary": slot(32.0, 10080), "seen_at": NOW}
        self.assertEqual(usage.merged({"primary": slot(1.0, 1440, NOW + 600), "secondary": five, "seen_at": NOW - 10}, both, NOW),
                         both)

    def test_reset_time_jitter_is_no_change(self):
        stored = reading(NOW - 10, 50.0)
        for resets in (OURS + 17, OURS - usage.TOLERANCE, OURS + usage.TOLERANCE):
            with self.subTest(resets=resets - OURS):
                self.assertIsNone(usage.merged(stored, reading(NOW, 50.0, resets=resets), NOW))
        for incoming in (reading(NOW, 50.0, resets=OURS + usage.TOLERANCE + 1), reading(NOW, 51.0), reading(NOW, 50.0, resets=None)):
            with self.subTest(incoming=incoming):
                self.assertEqual(usage.merged(stored, incoming, NOW), incoming)
        # Another length is another window: the stored one stays beside it, and the reading is new.
        self.assertEqual(usage.merged(stored, reading(NOW, 50.0, minutes=300), NOW),
                         reading(NOW, 50.0, minutes=300, secondary=slot(50.0, 10080)))
        # Both without a reset time, and null slots on both sides, are alike.
        self.assertIsNone(usage.merged(reading(NOW - 10, 50.0, resets=None), reading(NOW, 50.0, resets=None), NOW))
        null_first = {"primary": None, "secondary": slot(50.0, 10080), "seen_at": NOW - 10}
        self.assertIsNone(usage.merged(null_first, dict(null_first, seen_at=NOW), NOW))

    def test_unchanged_windows_are_written_again_after_300_seconds(self):
        self.assertEqual(usage.AGAIN, 300)
        self.assertEqual(usage.merged(reading(NOW - 300, 50.0), reading(NOW, 50.0), NOW), reading(NOW, 50.0))
        self.assertIsNone(usage.merged(reading(NOW - 299, 50.0), reading(NOW, 50.0), NOW))

    def test_a_stored_time_ahead_of_the_clock_is_no_stored_reading(self):
        # One line stamped by a clock that runs ahead would otherwise hold every later reading off.
        ahead = {"primary": slot(95.0, 10080), "secondary": slot(60.0, 300, NOW + 600), "seen_at": NOW + 3600}
        self.assertEqual(usage.merged(ahead, reading(NOW, 20.0), NOW), reading(NOW, 20.0))
        self.assertEqual(usage.merged(dict(ahead, seen_at=NOW + usage.SLACK + 1), reading(NOW, 20.0), NOW), reading(NOW, 20.0))
        self.assertIsNone(usage.merged(dict(ahead, seen_at=NOW + usage.SLACK), reading(NOW, 20.0), NOW))

    def test_keep_writes_owner_usage_rollout_and_launch(self):
        self.assertTrue(usage.keep(accounts, str(self.path), TAG, reading(NOW - 60, 40.0), NOW, REL_A, self.LAUNCH))
        self.assertEqual(self.stored(), {"owner": TAG, "usage": reading(NOW - 60, 40.0), "rollout": REL_A, "launch": self.LAUNCH})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        # A newer reading, through a Path, from another rollout of another launch; a stored window it does not
        # name stays.
        five = slot(60.0, 300, NOW + 600)
        accounts.write(self.path, {"owner": TAG, "usage": reading(NOW - 60, 40.0, secondary=five), "rollout": REL_A, "launch": "x"})
        other, launch = REL_A.replace(THREAD, OTHER), "codex-as-A-4343-1767268100"
        self.assertTrue(usage.keep(accounts, self.path, TAG, reading(NOW, 41.0), NOW, other, launch))
        self.assertEqual(self.stored(), {"owner": TAG, "usage": reading(NOW, 41.0, secondary=five), "rollout": other, "launch": launch})
        # Nothing new: nothing written, and no temporary file left.
        self.assertFalse(self.keep(reading(NOW, 41.0)))
        self.assertFalse(self.keep(reading(NOW + 10, 41.0), now=NOW + 10))
        self.assertEqual((self.stored()["usage"]["seen_at"], self.stored()["launch"]), (NOW, launch))
        self.assertEqual(os.listdir(self.dir), ["codex-usage-A.json"])

    def test_keep_replaces_another_owner_s_reading_and_an_unusable_file(self):
        five = slot(60.0, 300, NOW + 600)
        accounts.write(self.path, {"owner": "0" * 16, "usage": reading(NOW, 99.0, secondary=five), "rollout": REL_A, "launch": "x"})
        self.assertTrue(self.keep(reading(NOW - 600, 20.0)))
        self.assertEqual(self.stored(), {"owner": TAG, "usage": reading(NOW - 600, 20.0), "rollout": REL_A, "launch": self.LAUNCH})
        for content in ("not json", "[]", "", json.dumps({"owner": TAG, "usage": "x"}), json.dumps({"owner": TAG}),
                        json.dumps({"usage": reading(NOW, 99.0)})):
            with self.subTest(content=content):
                self.path.unlink()
                self.path.write_text(content)
                self.path.chmod(0o600)
                self.assertTrue(self.keep(reading(NOW - 600, 20.0)))
                self.assertEqual(self.stored()["usage"], reading(NOW - 600, 20.0))
        # An unsafe file is replaced by a 0600 one, and a link in its place is replaced, never written through.
        self.path.write_text(json.dumps({"owner": TAG, "usage": reading(NOW, 99.0)}))
        self.path.chmod(0o644)
        self.assertTrue(self.keep(reading(NOW - 600, 20.0)))
        self.assertEqual((stat.S_IMODE(self.path.stat().st_mode), self.stored()["usage"]["primary"]["used_percent"]), (0o600, 20.0))
        target = self.dir.parent / "elsewhere.json"
        target.write_text("{}")
        self.path.unlink()
        self.path.symlink_to(target)
        self.assertTrue(self.keep(reading(NOW - 600, 20.0)))
        self.assertEqual((self.path.is_symlink(), target.read_text()), (False, "{}"))

    def test_keep_refuses_another_file_name_an_unsafe_directory_and_another_tag(self):
        snapshot = reading(NOW, 20.0)
        for path in ("codex-usage-A.json", Path("guard/codex-usage-A.json"), self.dir / "codex-A.json",
                     self.dir / "codex-usage-.json", self.dir / "codex-usage--A.json", self.dir / "claude-A.usage.json",
                     self.dir / ".codex-usage-A.json.0123456789abcdef.tmp", self.dir / "codex-usage-A.json.tmp",
                     self.dir / "codex-usage-A/B.json"):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    self.keep(snapshot, path=path)
        for owner in (None, "", TAG.upper(), TAG[:-1], TAG + "0", TAG + "\n", 7, (USER, WORKSPACE)):
            with self.subTest(owner=owner):
                with self.assertRaises(ValueError):
                    self.keep(snapshot, owner=owner)
        self.dir.chmod(0o755)
        with self.assertRaises(ValueError):
            self.keep(snapshot)
        self.dir.chmod(0o700)
        link = self.dir.parent / "link"
        link.symlink_to(self.dir)
        with self.assertRaises(ValueError):
            self.keep(snapshot, path=link / self.path.name)
        with self.assertRaises(OSError):
            self.keep(snapshot, path=self.dir.parent / "missing" / self.path.name)
        self.assertEqual(os.listdir(self.dir), [])
        # A failed write is an OSError; so is a directory in the file's place, which nothing replaces.
        def full(path, data):
            raise OSError(28, "No space left on device")

        with self.assertRaises(OSError):
            usage.keep(SimpleNamespace(read=accounts.read, write=full), self.path, TAG, snapshot, NOW, REL_A, self.LAUNCH)
        self.path.mkdir()
        with self.assertRaises(OSError):
            self.keep(snapshot)
        self.assertEqual((os.listdir(self.dir), os.listdir(self.path)), ([self.path.name], []))

    def test_keep_never_stores_a_time_ahead_of_the_clock(self):
        for seen_at in (NOW + usage.SLACK + 1, NOW + 3600, float(NOW), True, None, str(NOW)):
            with self.subTest(seen_at=seen_at):
                self.assertFalse(self.keep(dict(reading(0, 20.0), seen_at=seen_at)))
        for snapshot in (None, {"primary": slot(20.0, 10080), "secondary": None}):
            self.assertFalse(self.keep(snapshot))
        self.assertFalse(self.path.exists())
        self.assertTrue(self.keep(reading(NOW + usage.SLACK, 20.0)))
        # A stored reading ahead of the clock gives way.
        accounts.write(self.path, {"owner": TAG, "usage": reading(NOW + 3600, 99.0), "rollout": REL_A, "launch": "x"})
        self.assertTrue(self.keep(reading(NOW, 21.0)))
        self.assertEqual(self.stored()["usage"], reading(NOW, 21.0))


class Witnessed(unittest.TestCase):
    """usage.witnessed: after a run whose hooks named turns, its snapshot from those turns only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-witnessed-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def write(self, thread, records, mtime=NOW, **more):
        """A rollout below ROOT/sessions: session_meta, then RECORDS; (its path, its REL)."""
        path = rollout(self.root, thread, [], **more)
        with open(path, "a") as stream:
            stream.write("".join(json.dumps(record) + "\n" for record in records))
        os.utime(path, (mtime, mtime))
        return path, path.relative_to(self.root / "sessions").as_posix()

    def test_only_the_noted_turns_of_the_noted_rollouts_count(self):
        _, rel = self.write(THREAD, [*whole(NOW - 100, TURN_B, weekly(83.0, THEIRS)), *whole(NOW - 50, TURN_A, weekly(40.0)),
                                     *whole(NOW - 10, TURN_C, weekly(85.0, THEIRS))])
        self.assertEqual(rel, REL_A)
        self.assertEqual(usage.witnessed(self.root, {rel: {TURN_A}}), (reading(NOW - 49, 40.0), rel, True))
        # A rollout that is not noted is not read, however new its reading.
        _, other = self.write(OTHER, whole(NOW - 5, TURN_A, weekly(90.0)))
        self.assertEqual(usage.witnessed(self.root, {rel: {TURN_A}}), (reading(NOW - 49, 40.0), rel, True))
        # Of the noted ones the newest reading wins.
        self.assertEqual(usage.witnessed(self.root, {rel: {TURN_A}, other: {TURN_A}}), (reading(NOW - 4, 90.0), other, True))
        self.assertEqual(usage.witnessed(self.root, {rel: {TURN_A, TURN_C}}), (reading(NOW - 9, 85.0, resets=THEIRS), rel, True))
        self.assertEqual(usage.witnessed(self.root, {}), (None, None, False))

    def test_a_thread_of_any_creator_counts(self):
        for creator in (("user-fixture-B", "workspace-fixture-B"), (USER, "workspace-other"), None):
            with self.subTest(creator=creator):
                shutil.rmtree(self.root / "sessions", ignore_errors=True)
                # No time bound either: a turn has one client, whenever its lines are stamped.
                _, rel = self.write(RESUMED, [*whole(NOW - 86400, TURN_B, weekly(83.0, THEIRS)),
                                              *whole(NOW - 86300, TURN_A, weekly(40.0))], creator=creator)
                self.assertEqual(usage.witnessed(self.root, {rel: {TURN_A}}), (reading(NOW - 86299, 40.0), rel, True))

    def test_a_copy_of_a_noted_turn_in_another_rollout_never_counts(self):
        _, parent = self.write(THREAD, whole(NOW - 100, TURN_P, weekly(40.0)))
        # A sub-agent's rollout repeats its parent's turn lines; this one even a reading in that turn, newer than
        # the parent's, then the sub-agent's own turn without a reply yet.
        copy = [started(NOW - 10, TURN_P), call(NOW - 10), record(NOW - 10, TURN_P), count(NOW - 10, weekly(83.0, THEIRS)),
                complete(NOW - 10, TURN_P), started(NOW - 5, TURN_C), context(NOW - 5, TURN_C)]
        _, child = self.write(OTHER, copy)
        self.assertEqual(usage.witnessed(self.root, {parent: {TURN_P}, child: {TURN_C}}), (reading(NOW - 99, 40.0), parent, True))
        self.assertEqual(usage.witnessed(self.root, {child: {TURN_C}}), (None, None, True))

    def test_another_family_s_snapshot_is_never_recorded(self):
        spark = dict(weekly(30.0), limit_id="codex_bengalfox")
        _, rel = self.write(THREAD, [started(NOW - 60, TURN_A), *reply(NOW - 59, TURN_A, weekly(40.0)),
                                     *reply(NOW - 50, TURN_A, spark), complete(NOW - 49, TURN_A)])
        self.assertEqual(usage.witnessed(self.root, {rel: {TURN_A}}), (None, None, True))
        _, other = self.write(OTHER, whole(NOW - 100, TURN_C, weekly(41.0)))
        self.assertEqual(usage.witnessed(self.root, {rel: {TURN_A}, other: {TURN_C}}), (reading(NOW - 99, 41.0), other, True))

    def test_the_newest_files_are_read_up_to_the_bound(self):
        notes = {}
        for number in range(usage.WITNESSED + 1):
            # The newest file holds the oldest reading; the one past the bound, the newest reading of all.
            past = number == usage.WITNESSED
            seen = NOW - 100 if past else NOW - 3600 - number
            _, rel = self.write(f"019a0000-0000-7000-8000-0000000020{number:02x}",
                                whole(seen - 1, TURN_A, weekly(90.0 if past else float(number))), mtime=NOW - number)
            notes[rel] = {TURN_A}
        with patch.object(usage, "session", wraps=usage.session) as read:
            found = usage.witnessed(self.root, notes)
        self.assertEqual((found[0], found[2]), (reading(NOW - 3600, 0.0), True))
        self.assertEqual((usage.WITNESSED, read.call_count), (64, 64))

    def test_an_older_file_is_not_opened_once_a_newer_reading_is_found(self):
        newer, new = self.write(THREAD, whole(NOW - 11, TURN_A, weekly(40.0)), mtime=NOW)
        # A file whose time lies more than SLACK before the best reading cannot hold a newer one; this one does.
        older, old = self.write(OTHER, whole(NOW - 6, TURN_A, weekly(90.0)), mtime=NOW - 10 - usage.SLACK - 1)
        notes = {new: {TURN_A}, old: {TURN_A}}
        with patch.object(usage, "session", wraps=usage.session) as read:
            self.assertEqual(usage.witnessed(self.root, notes), (reading(NOW - 10, 40.0), new, True))
        self.assertEqual([Path(call.args[0]) for call in read.call_args_list], [newer])
        # Within SLACK it is read, and further back within SKEW, the file server's clock lag.
        os.utime(older, (NOW - 10 - usage.SLACK, NOW - 10 - usage.SLACK))
        self.assertEqual(usage.witnessed(self.root, notes), (reading(NOW - 5, 90.0), old, True))
        os.utime(older, (NOW - 10 - usage.SLACK - 60, NOW - 10 - usage.SLACK - 60))
        self.assertEqual(usage.witnessed(self.root, notes, skew=60), (reading(NOW - 5, 90.0), old, True))
        self.assertEqual(usage.witnessed(self.root, notes, skew=59), (reading(NOW - 10, 40.0), new, True))

    def test_a_link_or_a_missing_rollout_is_skipped(self):
        _, rel = self.write(THREAD, whole(NOW - 11, TURN_A, weekly(40.0)))
        sessions = self.root / "sessions"
        elsewhere = self.root / "elsewhere"
        target = rollout(elsewhere, RESUMED, [])
        with open(target, "a") as stream:
            stream.write("".join(json.dumps(record) + "\n" for record in whole(NOW - 6, TURN_A, weekly(90.0))))
        linked = rel.replace(THREAD, RESUMED)
        (sessions / linked).symlink_to(target)
        fifo = rel.replace(THREAD, OTHER)
        os.mkfifo(sessions / fifo)
        self.addCleanup(CodexUsage.release, sessions / fifo)
        folder = rel.replace(THREAD, "019a0000-0000-7000-8000-0000000000ee")
        (sessions / folder).mkdir()
        missing = rel.replace(THREAD, "019a0000-0000-7000-8000-0000000000ff")
        outside = "../elsewhere/" + target.relative_to(elsewhere).as_posix()
        others = {linked: {TURN_A}, fifo: {TURN_A}, folder: {TURN_A}, missing: {TURN_A}, outside: {TURN_A}}
        found = []
        scan = threading.Thread(target=lambda: found.append((usage.witnessed(self.root, {rel: {TURN_A}, **others}),
                                                             usage.witnessed(self.root, others))), daemon=True)
        scan.start()
        scan.join(10)
        self.assertFalse(scan.is_alive(), "the read blocked on a FIFO")
        self.assertEqual(found[0], ((reading(NOW - 10, 40.0), rel, True), (None, None, False)))

    def test_a_rollout_is_viewed_when_its_tail_names_a_turn(self):
        _, plain = self.write(THREAD, [call(NOW - 10), count(NOW - 10, weekly(40.0))])  # a rollout without turn ids
        self.assertEqual(usage.witnessed(self.root, {plain: {TURN_A}}), (None, None, False))
        # A tail that names turns, none of them noted, is viewed and gives nothing: what stands there is another's.
        _, theirs = self.write(OTHER, whole(NOW - 10, TURN_B, weekly(83.0, THEIRS)))
        self.assertEqual(usage.witnessed(self.root, {plain: {TURN_A}, theirs: {TURN_A}}), (None, None, True))
        # A noted turn without a reply, as after a refused prompt, is viewed and holds no snapshot.
        _, refused = self.write(RESUMED, [started(NOW - 10, TURN_A), context(NOW - 10, TURN_A), complete(NOW - 9, TURN_A)])
        self.assertEqual(usage.witnessed(self.root, {plain: {TURN_A}, refused: {TURN_A}}), (None, None, True))

    def test_the_reading_before_a_cut_tail_s_first_naming_line_is_never_recorded(self):
        # A tool output longer than the tail, then the noted turn's reading and its next reply's record.
        path, rel = self.write(THREAD, [item(NOW - 20, "function_call_output", call_id="call_0", output="x" * usage.TAIL),
                                        call(NOW - 19), count(NOW - 18, weekly(83.0)), record(NOW - 17, TURN_A)])
        self.assertEqual(usage.session(path, usage.LAST, {TURN_A})["own"]["before"],
                         {"snapshot": reading(NOW - 18, 83.0), "family": None})
        self.assertEqual(usage.witnessed(self.root, {rel: {TURN_A}}), (None, None, True))

    def test_a_noted_turn_before_the_tail_lets_no_other_client_s_turn_count(self):
        # The launch's noted turn, then another client's turn in the same thread with more than the tail of lines.
        filler = [item(NOW - 49, "message", role="assistant", content="p" * 32768) for _ in range(usage.TAIL // 32768 + 1)]
        theirs = [started(NOW - 50, TURN_B), context(NOW - 50, TURN_B), *filler, *reply(NOW - 48, TURN_B, weekly(90.0, THEIRS)),
                  complete(NOW - 47, TURN_B)]
        path, rel = self.write(THREAD, [*whole(NOW - 100, TURN_A, weekly(31.0)), *theirs])
        self.assertGreater(path.stat().st_size, usage.TAIL)
        self.assertEqual(usage.witnessed(self.root, {rel: {TURN_A}}), (None, None, True))
        # Within the tail the noted turn decides.
        _, near = self.write(OTHER, [*whole(NOW - 100, TURN_A, weekly(31.0)), *whole(NOW - 50, TURN_B, weekly(90.0, THEIRS))])
        self.assertEqual(usage.witnessed(self.root, {near: {TURN_A}}), (reading(NOW - 99, 31.0), near, True))
        # A tail that names no turn at all, here one line longer than the tail and another client's reading, is not
        # viewed.
        _, unnamed = self.write(RESUMED, [*whole(NOW - 100, TURN_A, weekly(31.0)),
                                          item(NOW - 50, "function_call_output", call_id="call_0", output="x" * usage.TAIL),
                                          call(NOW - 49), count(NOW - 48, weekly(90.0, THEIRS))])
        self.assertEqual(usage.witnessed(self.root, {unnamed: {TURN_A}}), (None, None, False))


CODEX_FAKE = r"""
import json, os, pathlib, sys, time
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.163.0")
    sys.exit(0)
home = pathlib.Path(os.environ["CODEX_HOME"])
if os.environ.get("FAKE_PROBE"):
    # What --account-info could show while this session runs.
    state = pathlib.Path(os.environ["HOME"]) / ".local/state/coding-agent-kit/accounts/codex-A.json"
    pathlib.Path(os.environ["FAKE_PROBE"]).write_text(state.read_text() if state.exists() else "absent")
for item in json.loads(os.environ.get("FAKE_ROLLOUTS", "[]")):
    now = time.time() + item.get("offset", 0)
    # Milliseconds, truncated, as native Codex stamps its lines.
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + ".%03dZ" % (int(now * 1000) % 1000)
    day = home / "sessions" / time.strftime("%Y/%m/%d", time.localtime(now))
    day.mkdir(parents=True, exist_ok=True)
    path = day / ("rollout-" + time.strftime("%Y-%m-%dT%H-%M-%S", time.localtime(now)) + "-" + item["thread"] + ".jsonl")
    meta = {"id": item["thread"], "timestamp": stamp}
    if item.get("creator"):
        meta.update(creator_user_id=item["creator"][0], creator_account_id=item["creator"][1])
    with open(path, "a") as stream:
        stream.write(json.dumps({"timestamp": stamp, "type": "session_meta", "payload": meta}) + "\n")
        for rate in item["limits"]:
            stream.write(json.dumps({"timestamp": stamp, "type": "event_msg",
                                     "payload": {"type": "token_count", "info": None, "rate_limits": rate}}) + "\n")
print("fake codex ran")
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def id_token(claims):
    return "fake." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".fake"


class CodexLaunch(unittest.TestCase):
    """codex-as from a kit copy, with a fake codex that writes rollouts like native Codex."""

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-account-launch-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.kit = self.base / "kit"
        for part in ("bin", "lib", "docs"):
            shutil.copytree(ROOT / part, self.kit / part)
        self.home = self.base / "home with spaces"
        self.home.mkdir(mode=0o700)
        (self.home / ".codex").mkdir(mode=0o700)
        (self.home / ".codex/config.toml").write_text('model = "fixture-model"\n')
        (self.home / ".secrets").mkdir(mode=0o700)
        self.credential("A", {"sub": "fixture-A", "email": "alice@example.com",
                              "https://api.openai.com/auth": {"chatgpt_plan_type": "plus", "chatgpt_user_id": USER,
                                                              "chatgpt_account_id": WORKSPACE}})
        fakes = self.base / "fake bin"
        fakes.mkdir()
        (fakes / "codex").write_text(f"#!{sys.executable}\n" + CODEX_FAKE)
        (fakes / "codex").chmod(0o700)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("ANTHROPIC_", "CLAUDE_", "CODEX_", "OPENAI_", "CODING_AGENT_KIT_", "XDG_"))}
        self.env.update(HOME=str(self.home), TZ="UTC", PATH=os.pathsep.join((str(fakes), self.env.get("PATH", "/usr/bin:/bin"))))
        self.state = self.home / ".local/state/coding-agent-kit/accounts/codex-A.json"

    def credential(self, name, claims, api_key=False):
        path = self.home / f".secrets/codex-{name}.json"
        if api_key:
            data = {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-fabricated-sensitive-key", "tokens": None}
        else:
            data = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-10-01T00:00:00Z",
                    "tokens": {"id_token": id_token(claims), "access_token": "fabricated-sensitive-access-" + name,
                               "refresh_token": "fabricated-sensitive-refresh-" + name,
                               "account_id": claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", name)}}
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def run_codex(self, *args, env=None):
        result = subprocess.run([str(self.kit / "bin/codex-as"), *args], cwd=self.base, env=self.env | (env or {}),
                                stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=30, start_new_session=True)
        self.assertNotIn("fabricated-sensitive", result.stdout + result.stderr)
        return result

    def stored(self):
        raw = self.state.read_text()
        self.assertNotIn("fabricated-sensitive", raw, "a token reached the metadata file")
        self.assertEqual(stat.S_IMODE(self.state.lstat().st_mode), 0o600)
        return json.loads(raw)

    def test_a_launch_records_the_email_and_plan(self):
        probe = self.base / "probe"
        result = self.run_codex("A", "exec", "hello", env={"FAKE_PROBE": str(probe)})
        self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(self.stored(), {"email": "alice@example.com", "plan": "plus"})
        # Recorded before native Codex starts, so a long session shows it too.
        self.assertEqual(json.loads(probe.read_text()), {"email": "alice@example.com", "plan": "plus"})
        self.assertEqual(stat.S_IMODE(self.state.parent.lstat().st_mode), 0o700)

    def test_the_profile_claim_and_an_api_key_login(self):
        self.credential("A", {"https://api.openai.com/profile": {"email": "fallback@example.com"}})
        self.run_codex("A", "exec", "hello")
        self.assertEqual(self.stored(), {"email": "fallback@example.com"})
        self.credential("A", {}, api_key=True)
        self.run_codex("A", "exec", "hello")
        self.assertEqual(self.stored(), {"plan": "API key"})

    def test_the_run_records_its_last_rate_limits_and_keeps_the_newest(self):
        rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "limits": [limits(23.0, 41.0)]},
                    {"thread": OTHER, "creator": ["user-fixture-B", WORKSPACE], "limits": [limits(99.0)]}]
        result = self.run_codex("A", "exec", "hello", env={"FAKE_ROLLOUTS": json.dumps(rollouts), "FAKE_EXIT": "3"})
        self.assertEqual(result.returncode, 3, "the exit code of native Codex changed")
        record = self.stored()
        self.assertEqual(record["usage"]["primary"]["used_percent"], 23.0)
        self.assertEqual(record["usage"]["secondary"]["window_minutes"], 10080)
        seen = record["usage"]["seen_at"]
        self.assertLessEqual(abs(seen - time.time()), 60)
        # A run that wrote no snapshot keeps the last one.
        self.run_codex("A", "exec", "hello")
        self.assertEqual(self.stored()["usage"]["seen_at"], seen)
        # An older snapshot never replaces a newer one.
        future = dict(record["usage"], seen_at=seen + 86400)
        self.state.write_text(json.dumps(dict(record, usage=future)))
        self.run_codex("A", "exec", "hello", env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        self.assertEqual(self.stored()["usage"], future)

    def test_a_fork_never_counts_its_source_thread(self):
        # Another account's session goes on in the source thread while this one forks it.
        rollouts = [{"thread": THREAD, "creator": [USER, WORKSPACE], "limits": [limits(10.0)]},
                    {"thread": RESUMED, "creator": ["user-fixture-B", "workspace-fixture-B"], "limits": [limits(95.0)],
                     "offset": 2}]
        for args, used in ((("exec", "fork", RESUMED, "go"), 10.0), (("exec", "resume", RESUMED, "go"), 95.0)):
            with self.subTest(args=args):
                shutil.rmtree(self.home / ".codex/sessions", ignore_errors=True)
                self.state.unlink(missing_ok=True)
                result = self.run_codex("A", *args, env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.stored()["usage"]["primary"]["used_percent"], used)

    def test_a_snapshot_from_before_the_run_is_never_recorded(self):
        """A thread resumed by id is read whoever created it. The snapshot another account left in it a
        minute before this run started is that account's reading: after a run that got no snapshot of its
        own, the record's usage stays absent, and an older reading of this account's stays as it is."""
        then = int(time.time()) - 60
        path = rollout(self.home / ".codex", RESUMED, [(then, limits(90.0, 95.0))], stamp=then,
                       day=time.strftime("%Y/%m/%d", time.gmtime(then)), creator=("user-fixture-B", WORKSPACE))
        os.utime(path, (then, then))  # as the other account's Codex left it
        result = self.run_codex("A", "exec", "resume", RESUMED, "go")
        self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(self.stored(), {"email": "alice@example.com", "plan": "plus"})
        kept = {"primary": {"used_percent": 20.0, "window_minutes": 300, "resets_at": then + 7200}, "secondary": None,
                "seen_at": then - 3600}
        self.state.write_text(json.dumps({"email": "alice@example.com", "plan": "plus", "usage": kept}))
        result = self.run_codex("A", "exec", "resume", RESUMED, "go")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stored()["usage"], kept)
        # A snapshot of the run itself in that thread is recorded.
        rollouts = [{"thread": RESUMED, "creator": ["user-fixture-B", WORKSPACE], "limits": [limits(31.0)]}]
        self.run_codex("A", "exec", "resume", RESUMED, "go", env={"FAKE_ROLLOUTS": json.dumps(rollouts)})
        self.assertEqual(self.stored()["usage"]["primary"]["used_percent"], 31.0)

    def test_a_snapshot_from_the_moment_before_the_launch_is_never_recorded(self):
        """The run's own lines begin when codex-as starts Codex, not with the wall-clock second of the
        launch: the snapshot another account's client wrote in the resumed thread the moment before
        codex-as was started, early in a second so that the launch normally begins within the same one,
        is that account's reading as much as one from a minute before."""
        while time.time() % 1 > 0.1:
            time.sleep(0.005)
        then = time.time()
        path = rollout(self.home / ".codex", RESUMED, [], stamp=int(then) - 3600,
                       day=time.strftime("%Y/%m/%d", time.gmtime(then)), creator=("user-fixture-B", WORKSPACE))
        append(path, iso(then, int(then * 1000) % 1000), limits(90.0, 95.0))
        result = self.run_codex("A", "exec", "resume", RESUMED, "go")
        self.assertEqual((result.returncode, result.stdout), (0, "fake codex ran\n"), result.stderr)
        self.assertEqual(self.stored(), {"email": "alice@example.com", "plan": "plus"})

    def test_a_secrets_dir_in_the_state_directory_is_refused_before_any_write(self):
        secrets = self.home / "x/coding-agent-kit/accounts"
        secrets.mkdir(parents=True, mode=0o700)
        os.rename(self.home / ".secrets/codex-A.json", secrets / "codex-A.json")
        before = (secrets / "codex-A.json").read_bytes()
        (self.home / ".config/coding-agent-kit").mkdir(parents=True, mode=0o700)
        (self.home / ".config/coding-agent-kit/config.toml").write_text('secrets_dir = "~/x/coding-agent-kit/accounts"\n')
        result = self.run_codex("A", "exec", "hello", env={"XDG_STATE_HOME": str(self.home / "x")})
        self.assertEqual(result.returncode, 1)
        self.assertIn("must differ from the account metadata directory", result.stderr)
        self.assertEqual((secrets / "codex-A.json").read_bytes(), before)
        # The same directory through a linked ancestor: the launch goes on and writes nothing there.
        (self.home / "st").symlink_to("x")
        for args, code in ((("A", "exec", "hello"), 0), (("--account-info", "A"), 1)):
            with self.subTest(args=args):
                result = self.run_codex(*args, env={"XDG_STATE_HOME": str(self.home / "st")})
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual(sorted(os.listdir(secrets)), ["codex-A.json"])
                self.assertEqual((secrets / "codex-A.json").read_bytes(), before)
        self.assertIn("must not be the secrets directory", result.stderr)

    def test_an_unusable_state_directory_never_fails_the_launch(self):
        state = self.home / ".local/state"
        state.mkdir(parents=True)
        (state / "coding-agent-kit").write_text("in the way")
        result = self.run_codex("A", "exec", "hello")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "fake codex ran\n", ""))
        self.assertEqual(result.returncode, 0)
        (state / "coding-agent-kit").unlink()
        (state / "coding-agent-kit/accounts").mkdir(parents=True, mode=0o755)
        (state / "coding-agent-kit/accounts").chmod(0o755)
        result = self.run_codex("A", "exec", "hello")
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertFalse(self.state.exists(), "metadata was written into a directory others can read")


class Remember(unittest.TestCase):
    """codex-as's metadata write in process: racing another run of the same account, and the bound of
    the run's own lines."""

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o077))
        self.launch = load("kit_codex_launch_under_test", "lib/codex-as/launch.py")
        tmp = tempfile.TemporaryDirectory(prefix="kit-remember-test-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.config = SimpleNamespace(state_dir=self.base / "state", secrets_dir=self.base / "secrets")
        self.layout = SimpleNamespace(name="A", root=self.base, target=self.base / "unused")
        claims = {"email": "a@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus",
                                                                           "chatgpt_user_id": USER}}
        self.raw = json.dumps({"tokens": {"id_token": id_token(claims), "account_id": WORKSPACE}}).encode()

    def stored(self):
        return json.loads((self.config.state_dir / "accounts/codex-A.json").read_text())

    def test_a_newer_record_written_during_the_scan_is_kept(self):
        launch, config = self.launch, self.config
        plan = SimpleNamespace(continued=(), resume_id=None, recover_id=None)

        def snapshot(seen):
            return {"primary": {"used_percent": 1.0, "window_minutes": 300, "resets_at": None}, "secondary": None,
                    "seen_at": seen}

        def scan(root, ids, account, started, ended, skew=0):
            # The other run finishes while this one still reads its rollouts.
            path = launch.metadata.directory(config, create=True) / "codex-A.json"
            launch.metadata.write(path, {"email": "a@example.com", "plan": "plus", "usage": snapshot(NOW + 100)})
            return snapshot(NOW)

        with patch.object(launch.usage, "latest", scan):
            launch.remember(config, self.layout, self.raw, plan, NOW - 60)
        self.assertEqual(self.stored()["usage"]["seen_at"], NOW + 100)

    def test_the_run_s_lines_begin_when_codex_started_to_the_millisecond(self):
        """The record takes the lines stamped from the moment before Codex started (BEGUN), less the
        millisecond a truncated stamp can lose: never a line that another account's client wrote into the
        resumed thread earlier in that same second, or one millisecond too early; the line written at
        BEGUN, stamped just before it, counts."""
        launch = self.launch
        second = int(time.time()) - 60
        begun = second + 0.7 + 0.4 * launch.STAMPED
        path = rollout(self.base, RESUMED, [], stamp=second - 3600, creator=("user-fixture-B", WORKSPACE),
                       day=time.strftime("%Y/%m/%d", time.gmtime(second)))
        plan = SimpleNamespace(continued=(RESUMED,), resume_id=RESUMED, recover_id=None)
        for milliseconds, used, recorded in ((300, 90.0, None), (699, 91.0, None), (700, 12.0, 12.0)):
            with self.subTest(milliseconds=milliseconds):
                append(path, iso(second, milliseconds), limits(used))
                launch.remember(self.config, self.layout, self.raw, plan, begun)
                usage = self.stored().get("usage")
                self.assertEqual(usage and (usage["primary"]["used_percent"], usage["seen_at"]),
                                 recorded and (recorded, second))


if __name__ == "__main__":
    unittest.main()
