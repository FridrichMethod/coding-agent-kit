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


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


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

    def latest(self, ids=(), account=(USER, WORKSPACE)):
        return usage.latest(self.root, ids, account, self.start, self.end)

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


EMPTY = {"snapshot": None, "family": None, "accounted": False, "responded": False, "tools": False}


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


class RanTools(unittest.TestCase):
    """usage.ran_tools: whether a run's rollouts of this account show a call that a tool hook sees."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-ran-tools-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        patcher = patch.dict(os.environ, {"TZ": "UTC"})
        patcher.start()
        self.addCleanup(patcher.stop)
        time.tzset()
        self.addCleanup(time.tzset)
        self.start, self.end = NOW - 600, NOW + 600

    def rollout(self, thread, records, creator=(USER, WORKSPACE), meta=None, touched=NOW):
        path = self.root / "sessions/2026/01/01" / f"rollout-2026-01-01T11-50-00-{thread}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"id": thread, "timestamp": iso(self.start), "cwd": "/", "cli_version": "0.169.0", **(meta or {})}
        if creator:
            payload.update(creator_user_id=creator[0], creator_account_id=creator[1])
        path.write_text("".join(json.dumps(record) + "\n" for record in [line(self.start, "session_meta", payload),
                                                                          *records]))
        os.utime(path, (touched, touched))
        return path

    def ran(self, ids=(), account=(USER, WORKSPACE)):
        return usage.ran_tools(self.root, ids, account, self.start, self.end)

    def test_a_call_in_this_accounts_rollout_during_the_run(self):
        self.assertFalse(self.ran())
        self.rollout(THREAD, [item(NOW, "reasoning"), item(NOW, "message", role="assistant")])
        self.assertFalse(self.ran(), "a run without calls")
        self.rollout(OTHER, [call(NOW)], creator=("user-fixture-B", "workspace-fixture-B"))
        self.assertFalse(self.ran(), "another account's call counted")
        self.assertFalse(self.ran(account=None), "an API-key login has no creator to match")
        self.rollout(THREAD, [item(NOW, "custom_tool_call", name="exec", input="")])
        self.assertTrue(self.ran())

    def test_calls_outside_the_run_and_calls_no_tool_hook_sees_do_not_count(self):
        slack = usage.SLACK
        for records in ([call(self.start - slack - 1)], [call(self.end + slack + 1)],
                        [call(NOW, "wait"), call(NOW, "write_stdin"), item(NOW, "web_search_call"),
                         item(NOW, "tool_search_call")],
                        [call(NOW, inherited=True), item(NOW, "local_shell_call", inherited=True)]):
            with self.subTest(records=records):
                self.rollout(THREAD, records)
                self.assertFalse(self.ran())
        for records in ([call(self.start - slack)], [call(self.end + slack)], [item(NOW, "local_shell_call")],
                        [call(NOW, "wait", "collaboration")]):
            with self.subTest(records=records):
                self.rollout(THREAD, records)
                self.assertTrue(self.ran())

    def test_a_rollout_untouched_during_the_run_is_not_read(self):
        self.rollout(THREAD, [call(NOW)], touched=self.start - 3600)
        self.assertFalse(self.ran())

    def test_guardian_review_threads_are_skipped(self):
        guardian = {"thread_source": "guardian_review", "source": {"subagent": {"other": "guardian"}}}
        self.rollout(THREAD, [call(NOW)], meta=guardian)
        self.assertFalse(self.ran())
        self.assertFalse(self.ran(ids=(THREAD,)))
        # Reviews never crowd this account's own rollout out of the files read.
        for number in range(usage.MAX_FILES + 2):
            self.rollout(f"019a0000-0000-7000-8000-0000000020{number:02x}", [call(NOW)], meta=guardian,
                         touched=NOW + 60)
        self.rollout(OTHER, [call(NOW)])
        self.assertTrue(self.ran())

    def test_a_call_followed_by_more_than_the_tail_counts(self):
        output = item(NOW + 1, "function_call_output", call_id="call_1", output="x" * (2 * usage.TAIL))
        self.rollout(THREAD, [call(NOW), output, item(NOW + 2, "message", role="assistant"), count(NOW + 2, None)])
        self.assertTrue(self.ran())
        self.assertTrue(self.ran(ids=(THREAD,)))
        reasoning = [item(NOW + 1, "reasoning", summary=[], encrypted_content="y" * 4096) for _ in range(600)]
        self.rollout(THREAD, [call(NOW), *reasoning])
        self.assertTrue(self.ran())
        self.rollout(THREAD, [call(self.start - 3600), *[{**record, "timestamp": iso(self.start - 3600)}
                                                         for record in reasoning], item(NOW, "reasoning")])
        self.assertFalse(self.ran(), "a call from before the run counted")

    def test_the_rollout_is_read_back_only_to_the_lines_before_the_run(self):
        before = [item(self.start - 3600, "reasoning", encrypted_content="y" * 4096) for _ in range(1200)]
        during = [item(NOW, "reasoning", encrypted_content="z" * 4096) for _ in range(600)]
        path = self.rollout(THREAD, [*before, *during])
        self.assertGreater(path.stat().st_size, 7 * usage.TAIL)
        read, real = [], usage.opened

        class Counted:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.stream.close()

            def read(self, size=-1):
                data = self.stream.read(size)
                read.append(len(data))
                return data

            def __getattr__(self, name):
                return getattr(self.stream, name)

        with patch.object(usage, "opened", lambda path: Counted(real(path))):
            self.assertFalse(self.ran())
        # The first line, the lines of the run, and the one chunk that reaches back before it.
        self.assertLessEqual(sum(read), usage.FIRST_LINE + 5 * usage.TAIL)

    def test_a_resumed_thread_counts_by_its_id_whoever_created_it(self):
        self.rollout(RESUMED, [call(NOW)], creator=("user-fixture-B", "workspace-fixture-B"))
        self.assertFalse(self.ran())
        self.assertTrue(self.ran(ids=(RESUMED,)))
        self.assertTrue(self.ran(ids=(RESUMED,), account=None))


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
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now))
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
    """codex-as's metadata write in process, racing another run of the same account."""

    def test_a_newer_record_written_during_the_scan_is_kept(self):
        self.addCleanup(os.umask, os.umask(0o077))
        launch = load("kit_codex_launch_under_test", "lib/codex-as/launch.py")
        tmp = tempfile.TemporaryDirectory(prefix="kit-remember-test-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        config = SimpleNamespace(state_dir=base / "state", secrets_dir=base / "secrets")
        layout = SimpleNamespace(name="A", root=base, target=base / "unused")
        plan = SimpleNamespace(continued=(), resume_id=None, recover_id=None)
        claims = {"email": "a@example.com", "https://api.openai.com/auth": {"chatgpt_plan_type": "plus",
                                                                           "chatgpt_user_id": USER}}
        raw = json.dumps({"tokens": {"id_token": id_token(claims), "account_id": WORKSPACE}}).encode()

        def snapshot(seen):
            return {"primary": {"used_percent": 1.0, "window_minutes": 300, "resets_at": None}, "secondary": None,
                    "seen_at": seen}

        def scan(root, ids, account, started, ended):
            # The other run finishes while this one still reads its rollouts.
            path = launch.metadata.directory(config, create=True) / "codex-A.json"
            launch.metadata.write(path, {"email": "a@example.com", "plan": "plus", "usage": snapshot(NOW + 100)})
            return snapshot(NOW)

        with patch.object(launch.usage, "latest", scan):
            launch.remember(config, layout, raw, plan, NOW - 60)
        stored = json.loads((config.state_dir / "accounts/codex-A.json").read_text())
        self.assertEqual(stored["usage"]["seen_at"], NOW + 100)


if __name__ == "__main__":
    unittest.main()
