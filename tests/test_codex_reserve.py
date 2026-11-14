"""codex-as protected mode (usage reserves): screens, managed requirements, the Codex gate provider,
the decision adapter and whole protected launches.

Fabricated credentials only, a temporary HOME and 127.0.0.1 fixtures. Whole launches need the OS
boundary (Linux Landlock ABI 6+ on x86_64, or a reviewed macOS release) and openssl for the fixture
CA, and skip cleanly without them; with a real codex 0.167.2 on PATH one case runs it too.
"""

import base64
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pty
import pwd
import re
import select
import shutil
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/codex-reserve"
LAUNCHER = ROOT / "bin/codex-as"
KIT_KNOBS = ("CODING_AGENT_KIT_CONFIG", "CODING_AGENT_KIT_COLOR", "CODING_AGENT_KIT_USAGE_FILE",
             "CODING_AGENT_KIT_TOKEN_ID", "CODING_AGENT_KIT_CLAUDE_PID", "CODING_AGENT_KIT_REPO_URL",
             "XDG_CONFIG_HOME", "XDG_STATE_HOME", "NO_COLOR")
F12 = b"\x1b[24~"


def load(name, path):
    spec = importlib.util.spec_from_file_location("test_codex_reserve_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


protect = load("protect", ROOT / "lib/codex-as/protect.py")
reader = load("reader", ROOT / "lib/codex-as/reader.py")
provider_gate = load("provider_gate", ROOT / "lib/codex-as/gate.py")
kit = provider_gate.kit
reserve = provider_gate.reserve
backend = load("backend", FIXTURES / "backend.py")
layout = load("layout", ROOT / "lib/codex-as/layout.py")
supervise = load("supervise", ROOT / "lib/coding-agent-kit/supervise.py")
NATIVE = protect.V0_167_2


def boundary_missing():
    name = "sandbox_macos" if sys.platform == "darwin" else "sandbox_linux"
    sandbox = load(name, ROOT / "lib/coding-agent-kit" / (name + ".py"))
    try:
        sandbox.probe()
    except sandbox.Refusal as error:
        return str(error)
    return None


def pwd_name():
    return pwd.getpwuid(os.getuid()).pw_name


def refused(call, *args):
    try:
        call(*args)
    except protect.Refused as error:
        return str(error)
    raise AssertionError("not refused: %r" % (args,))


class Screen(unittest.TestCase):
    """The invocation form, version and write rules: pure, no process."""

    def test_interactive_forms_pass_and_lose_their_no_daemon(self):
        self.assertEqual(protect.screen(["-m", "gpt-5.5", "fix it", "--no-daemon", "-cmodel_reasoning_effort=high",
                                         "-C", "/w", "--image=a.png", "--no-alt-screen", "--", "resume"], NATIVE),
                         ["-m", "gpt-5.5", "fix it", "-cmodel_reasoning_effort=high", "-C", "/w", "--image=a.png",
                          "--no-alt-screen", "--", "resume"])
        self.assertEqual(protect.screen([], NATIVE), [])
        self.assertEqual(protect.screen(["--model=gpt-6-sol", "-c=model=gpt-5.5"], NATIVE),
                         ["--model=gpt-6-sol", "-c=model=gpt-5.5"])

    def test_scan_names_the_model_the_arguments_select(self):
        self.assertEqual(protect.scan(["-m", "gpt-5.5", "fix it"], NATIVE)[1], "gpt-5.5")
        self.assertEqual(protect.scan(["-cmodel=gpt-6-sol"], NATIVE)[1], "gpt-6-sol")
        self.assertEqual(protect.scan(["--config", 'model="gpt-5.5"'], NATIVE)[1], "gpt-5.5")
        self.assertIsNone(protect.scan(["fix it", "-c", "model_reasoning_effort=high"], NATIVE)[1])

    def test_a_selected_profile_model_wins_over_the_files_own(self):
        profiles = {"p": {"model": "o3"}}
        self.assertEqual(protect.configured_model({"model": "gpt-5.5", "profile": "p", "profiles": profiles}), "o3")
        self.assertEqual(protect.configured_model({"model": "gpt-5.5", "profile": "q", "profiles": profiles}), "gpt-5.5")
        self.assertIsNone(protect.configured_model({}))

    def test_reader_specifications_must_lie_outside_every_writable_path(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            work, other = base / "work", base / "tmp"
            (work / "tmp/spec").mkdir(parents=True)
            (other / "spec").mkdir(parents=True)
            protect.outside(other / "spec", [str(work)])
            for path in (work, work / "tmp/spec"):
                with self.subTest(path=path):
                    self.assertIn("set TMPDIR to a directory outside", refused(protect.outside, path, [str(work)]))
            link = base / "link"
            link.symlink_to(work / "tmp")
            refused(protect.outside, link / "spec", [str(work)])

    def test_the_boundary_devices_count_as_writable(self):
        name = "sandbox_macos" if sys.platform == "darwin" else "sandbox_linux"
        fixed = protect.fixed_writable(load(name, ROOT / "lib/coding-agent-kit" / (name + ".py")))
        for device in ("/dev/shm", "/dev/pts") if sys.platform != "darwin" else ("/dev/fd", "/dev/ttys000"):
            with self.subTest(device=device):
                self.assertTrue(any(protect.within(os.path.realpath(device), root) for root in fixed), fixed)
        if sys.platform != "darwin" and os.path.isdir("/dev/shm"):
            self.assertIn("set TMPDIR", refused(protect.outside, "/dev/shm/codex-as-protect-x", fixed))

    def test_code_loaded_outside_the_boundary_must_lie_out_of_its_reach_through_every_link(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            work, out = base / "work", base / "out"
            for path in (work / "venv/lib", out / "lib"):
                path.mkdir(parents=True)
            roots = [str(work)]
            protect.unreachable([("the Python", str(out / "python3"), False), ("the kit directory", str(out / "lib"), True),
                                 ("the Python prefix", str(base), False)], roots)
            python = work / "venv/bin/python3"
            self.assertIn("the Python %s lies inside %s, which the boundary can write; run codex-as with a Python and "
                          "a kit installed outside" % (python, work),
                          refused(protect.unreachable, [("the Python", str(python), False)], roots))
            self.assertIn("%s, which the boundary can write, lies inside the kit directory %s" % (work, base),
                          refused(protect.unreachable, [("the kit directory", str(base), True)], roots))
            (out / "link").symlink_to(work / "venv")  # the real path is inside
            (work / "hop").symlink_to(out / "lib")    # the name is inside, the real path outside
            (work / "b").symlink_to(out / "lib")
            (out / "a").symlink_to(work / "b")        # only a link on the way is inside
            self.assertEqual(os.path.realpath(out / "a"), str(out / "lib"))
            for path in (out / "link/lib", work / "hop", out / "a"):
                with self.subTest(path=path):
                    refused(protect.unreachable, [("the Python module path entry", str(path), True)], roots)
            names = [name for name in protect.resolution(out / "a/x/../y") if name.startswith(str(base) + "/")]
            self.assertEqual(names, [str(out), str(out / "a"), str(work), str(work / "b"), str(out), str(out / "lib"),
                                     str(out / "lib/x"), str(out / "lib/y")])

    def test_kit_processes_run_the_base_interpreter_and_report_its_code_paths(self):
        python = protect.interpreter()
        self.assertEqual(python, os.path.realpath(python))
        with mock.patch.object(sys, "_base_executable", sys.executable, create=True):
            self.assertEqual(protect.interpreter(), os.path.realpath(sys.executable))
        sources = protect.code_sources(python, {})
        kinds = {}
        for what, path, holds in sources:
            kinds.setdefault(what, []).append((path, holds))
        self.assertEqual(kinds["the Python"], [(python, False)])
        self.assertEqual(len(kinds["the Python prefix"]), 4)
        self.assertEqual(kinds["the kit directory"], [(str(protect.HERE), True), (str(protect.KIT), True)])
        self.assertTrue(any(os.path.isfile(os.path.join(path, "json/__init__.py"))
                            for path, _ in kinds["the Python module path entry"]))
        self.assertIn("did not report where it loads code from",
                      refused(protect.code_sources, str(ROOT / "missing/python3"), {}))

    def test_the_launchers_own_python_must_lie_out_of_reach_by_the_name_it_ran(self):
        sources = protect.launcher_sources()
        kinds = {}
        for what, path, holds in sources:
            kinds.setdefault(what, []).append((path, holds))
        self.assertEqual(kinds["the launcher's Python"], [(sys.executable, False)])
        self.assertEqual(len(kinds["the launcher's Python prefix"]), 4)
        self.assertEqual(kinds["the launcher's module path entry"], [(path, True) for path in sys.path])
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            work = base / "work"
            (work / ".venv/bin").mkdir(parents=True)
            (base / "bin").mkdir()
            protect.unreachable(protect.launcher_sources(), [str(work)], protect.LAUNCHER_HINT)
            python = work / ".venv/bin/python3"
            linked = base / "bin/python3"
            linked.symlink_to(python)  # a link outside to an interpreter inside
            for patch in ({"executable": str(python)}, {"executable": str(linked)},
                          {"prefix": str(work / ".venv")}, {"path": [*sys.path, str(work / "lib")]},
                          {"path": [*sys.path, str(base)]}):
                with self.subTest(patch=patch), mock.patch.multiple(sys, **patch):
                    message = refused(protect.unreachable, protect.launcher_sources(), [str(work)], protect.LAUNCHER_HINT)
                    self.assertIn("; start codex-as with a python3 outside the workspace and the Codex homes first on "
                                  "PATH", message)
            with mock.patch.object(sys, "executable", str(python)):
                self.assertIn("the launcher's Python %s lies inside %s" % (python, work),
                              refused(protect.unreachable, protect.launcher_sources(), [str(work)], protect.LAUNCHER_HINT))
            for patch in ({"executable": ""}, {"executable": None}, {"path": ["relative"]}):
                with self.subTest(patch=patch), mock.patch.multiple(sys, **patch):
                    self.assertIn("does not report where it loads code from", refused(protect.launcher_sources))

    def test_the_confine_hook_refuses_a_writable_path_that_became_a_link(self):
        class Refusal(Exception):
            def __init__(self, kind, message):
                super().__init__(message)
                self.kind = kind

        calls = []
        sandbox = types.SimpleNamespace(Refusal=Refusal, confine=lambda port, paths, name, **_: calls.append(paths) or [])
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            (base / "home").mkdir()
            inner = base / "codex/sqlite"
            inner.mkdir(parents=True)
            paths = [str(base / "codex"), str(inner)]
            hook = protect.Session.hook(types.SimpleNamespace(sandbox=sandbox), paths, None)
            self.assertEqual(hook(50123, ["codex"]), ["codex"])
            inner.rmdir()
            inner.symlink_to(base / "home")
            with self.assertRaises(Refusal) as caught:
                hook(50123, ["codex"])
            self.assertIn("%s no longer resolves to itself" % inner, str(caught.exception))
            self.assertEqual(calls, [paths])

    def test_the_gate_takes_the_proxy_codex_would_use_for_https(self):
        """Codex reads HTTPS_PROXY, then ALL_PROXY (either spelling) for its HTTPS connections, and
        HTTP_PROXY only for http:// URLs; the gate's upstream connections follow it."""
        for env, expected in (({}, None),
                              ({"HTTPS_PROXY": "http://a.test:1", "ALL_PROXY": "http://b.test:2"}, ("a.test", 1, ())),
                              ({"https_proxy": "http://a.test:1", "HTTP_PROXY": "socks5h://c.test:3"}, ("a.test", 1, ())),
                              ({"ALL_PROXY": "http://b.test:2", "HTTP_PROXY": "http://c.test:3"}, ("b.test", 2, ())),
                              ({"all_proxy": "http://b.test:2", "NO_PROXY": ".chatgpt.com"},
                               ("b.test", 2, ((".chatgpt.com",),))),
                              ({"HTTP_PROXY": "socks5h://c.test:3", "http_proxy": "http://c.test:3"}, None)):
            with self.subTest(env=env):
                self.assertEqual(protect.gate_proxy(supervise, kit.parse_proxy, env, darwin=False), expected)
        for env, reason in (({"ALL_PROXY": "socks5h://b.test:2"}, "unsupported ALL_PROXY"),
                            ({"HTTPS_PROXY": "http://a.test:1", "https_proxy": "http://b.test:2"},
                             "HTTPS_PROXY and https_proxy differ")):
            with self.subTest(env=env), self.assertRaises(supervise.Refused) as caught:
                protect.gate_proxy(supervise, kit.parse_proxy, env, darwin=False)
            self.assertIn(reason, caught.exception.reason)
            self.assertNotIn("b.test", caught.exception.reason)

    def test_on_macos_the_gate_takes_the_system_https_proxy_codex_would_use(self):
        """Codex 0.167.2 on macOS uses the system HTTPS proxy when no HTTPS_PROXY spelling is set, ahead
        of ALL_PROXY (hyper-util 0.1.20); the launcher reads it with scutil, outside the boundary."""
        settings = {"HTTPSEnable": 1, "HTTPSProxy": "system.test", "HTTPSPort": 8443, "ExceptionsList": None}
        with mock.patch.object(supervise, "macos_proxies", lambda: settings):
            for env, expected in (({"ALL_PROXY": "http://b.test:2"}, ("system.test", 8443, ())),
                                  ({"HTTPS_PROXY": "http://a.test:1"}, ("a.test", 1, ()))):
                with self.subTest(env=env):
                    self.assertEqual(protect.gate_proxy(supervise, kit.parse_proxy, env, darwin=True), expected)
            self.assertEqual(protect.gate_proxy(supervise, kit.parse_proxy, {}, darwin=False), None)
        with mock.patch.object(supervise, "macos_proxies", lambda: {"ProxyAutoConfigEnable": 1}), \
                self.assertRaises(supervise.Refused) as caught:
            protect.gate_proxy(supervise, kit.parse_proxy, {"ALL_PROXY": "http://b.test:2"}, darwin=True)
        self.assertIn("PAC or WPAD", caught.exception.reason)

    def session(self, base, databases=True):
        """A Session over a laid-out account home in BASE (codex_home BASE/home/.codex), unchecked."""
        home = base / "home"
        root = home / ".codex"
        account = root / "accounts/A"
        account.mkdir(parents=True)
        for name in (*layout.SHARED_DIRS, ".tmp"):
            (root / name).mkdir()
        for name in (*layout.SHARED_FILES, *protect.SHARED_WRITABLE_FILES):
            (root / name).touch()
        (root / "packages/standalone").mkdir(parents=True)
        (base / "work").mkdir()
        if databases:
            for name in (*NATIVE.databases, *NATIVE.later_databases):
                (root / (name + ".sqlite")).write_bytes(b"")
        config = types.SimpleNamespace(codex_home=root, home=home, secrets_dir=home / ".secrets")
        session = protect.Session(None, None, None, config, types.SimpleNamespace(home=account, name="A"), None, [],
                                  "codex", {}, root, base / "work")
        session.native = NATIVE
        return session

    def test_the_writable_set_is_what_native_writes_and_the_rest_of_codex_home_stays_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            session = self.session(base)
            root = base / "home/.codex"
            paths = session.paths()
            self.assertEqual(paths[:2], [str(base / "work"), str(root / "accounts/A")])
            for name in ("sessions", "archived_sessions", "rollout-migrations", "thread-writer-locks",
                         ".tmp/rollout-maintenance.lock", "history.jsonl", "session_index.jsonl", "state_5.sqlite",
                         "state_5.sqlite-wal", "state_5.sqlite-shm", "queue_1.sqlite-shm"):
                self.assertIn(str(root / name), paths)
            self.assertNotIn(str(root), paths)
            # .tmp is plain codex's plugin staging area (plugins, marketplaces), memories a prompt
            # context that protected mode turns off: native writes neither through the account home.
            for name in ("packages/standalone/x", "config.toml", "hooks.json", "mcp.json", "skills/x", "rules/x",
                         "agents/x", "themes/x", "worktrees/x", "accounts/B/x", "accounts/.locks/x", "auth.json",
                         "new.sqlite", ".tmp/plugins/x", ".tmp/plugins.sha", ".tmp/x", "memories/x", "memories_v2/x"):
                with self.subTest(name=name):
                    self.assertFalse(any(protect.within(str(root / name), path) for path in paths))
            self.assertIn(str(root / "thread_history_1.sqlite-wal"), paths)
            self.assertEqual(session.paths(refresh=True), session.paths() + [str(base / "home/.secrets")])

    def test_the_databases_wal_and_shm_files_are_made_beside_them_with_their_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            session = self.session(base)
            root = base / "home/.codex"
            (root / "state_5.sqlite").chmod(0o640)
            (root / "logs_2.sqlite-wal").write_bytes(b"kept")
            session.prepare_databases()
            self.assertEqual(stat.S_IMODE((root / "state_5.sqlite-wal").stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE((root / "state_5.sqlite-shm").stat().st_mode), 0o640)
            self.assertEqual((root / "state_5.sqlite-wal").read_bytes(), b"")
            self.assertEqual((root / "logs_2.sqlite-wal").read_bytes(), b"kept")
            for name in ("goals_1.sqlite", "memories_1.sqlite-wal"):
                with self.subTest(name=name):
                    (root / name).unlink()
                    (root / name).symlink_to(base / "home/.bashrc")
                    self.assertIn("%s is not a regular file of this user" % name, refused(session.prepare_databases))
                    self.assertFalse((base / "home/.bashrc").exists())
                    (root / name).unlink()
                    (root / name).write_bytes(b"")

    def test_the_shared_entries_the_boundary_names_must_be_plain_entries_of_this_user(self):
        """Each launch resolves the shared entries beneath read-only codex_home again; on macOS a
        session can replace one with a link (its rule covers the name itself), which would make the
        link's target writable in the next launch. A link, another type or a missing entry refuses;
        before the layout exists a missing one passes (the launcher creates it)."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            session = self.session(base)
            root = base / "home/.codex"
            session.shared_entries()
            ssh, rc = base / "home/.ssh", base / "home/.zshrc"
            ssh.mkdir()
            rc.write_text("# rc\n")
            for name, target, kind in (("sessions", ssh, "a directory"), ("history.jsonl", rc, "a regular file"),
                                       (".tmp", ssh, "a directory"),
                                       (".tmp/rollout-maintenance.lock", rc, "a regular file")):
                with self.subTest(name=name):
                    held = base / "held"
                    (root / name).rename(held)
                    (root / name).symlink_to(target)
                    self.assertIn("%s is a link, not %s of this user: protected mode makes it writable" % (root / name,
                                                                                                    kind),
                                  refused(session.shared_entries))
                    self.assertIn("is a link", refused(session.shared_entries, False))
                    (root / name).unlink()
                    held.rename(root / name)
            (root / "history.jsonl").unlink()
            (root / "history.jsonl").mkdir()
            self.assertIn("%s is not a regular file of this user" % (root / "history.jsonl"),
                          refused(session.shared_entries))
            (root / "history.jsonl").rmdir()
            self.assertIn("%s is missing" % (root / "history.jsonl"), refused(session.shared_entries))
            session.shared_entries(False)
            self.assertEqual(rc.read_text(), "# rc\n")

    def test_a_protected_launch_never_logs_in_also_when_the_login_disappears_after_the_check(self):
        """check() refuses an account without a saved login; one removed after it reaches enroll(),
        which refuses too instead of running the native device login outside the boundary."""
        launch = load("launch", ROOT / "lib/codex-as/launch.py")
        with tempfile.TemporaryDirectory() as temp:
            account = types.SimpleNamespace(target=Path(temp) / "codex-A.json", name="A")
            with mock.patch.object(launch.guard, "terminal", side_effect=AssertionError("the login started")), \
                    self.assertRaises(launch.protect.Refused) as caught:
                launch.enroll(account, "codex", protected=True)
            self.assertIn("the account A has no saved login yet, and protected mode never runs the native login",
                          str(caught.exception))

    def test_the_launcher_runs_commands_by_name_from_system_directories_only_while_protected(self):
        with mock.patch.dict(os.environ, {"PATH": "/w/.venv/bin:/usr/bin"}):
            with protect.launcher_path():
                self.assertEqual(os.environ["PATH"], protect.SYSTEM_PATH)
            self.assertEqual(os.environ["PATH"], "/w/.venv/bin:/usr/bin")

    def test_a_selected_profile_is_found_with_its_protected_keys(self):
        profiles = {"p": {"model": "gpt-5.5", "sandbox_mode": "workspace-write"}, "q": {"model_provider": "x"}}
        self.assertEqual(protect.selected_profile({"profile": "p", "profiles": profiles}), ("p", profiles["p"]))
        self.assertEqual(protect.selected_profile({"profiles": profiles}), (None, {}))
        self.assertEqual(protect.selected_profile({"profile": "r", "profiles": profiles}), (None, {}))
        self.assertEqual(sorted(protect.PROTECTED_KEYS & set(profiles["p"])), ["sandbox_mode"])

    def test_subcommands_and_headless_modes_refuse_wherever_clap_would_run_them(self):
        for args in (["resume"], ["fork", "x"], ["exec", "x"], ["e", "x"], ["review"], ["hello", "resume"],
                     ["-m", "gpt-5.5", "resume"], ["-mgpt-5.5", "fork"], ["-c=model=gpt-5.5", "resume"],
                     ["--model=gpt-5.5", "fork"], ["-C", "/d", "app-server"], ["--image=a.png", "remote-control"],
                     ["help"], ["login"], ["mcp"], ["cloud-tasks"], ["migrate-nfs-db"], ["-m", "resume"],
                     ["-iresume"]):
            with self.subTest(args=args):
                refused(protect.screen, args, NATIVE)

    def test_options_outside_the_allowlist_refuse(self):
        for args in (["--remote", "ws://x"], ["--profile", "p"], ["-pp"], ["--enable", "x"], ["-s", "read-only"],
                     ["-a", "never"], ["--search"], ["--yolo"], ["--add-dir", "/x"], ["--unknown"], ["-x"],
                     ["-hV"], ["--no-alt-screen=1"], ["-m"], ["-c"], ["-c", "--model"],
                     ["--dangerously-bypass-approvals-and-sandbox"], ["--oss"]):
            with self.subTest(args=args):
                refused(protect.screen, args, NATIVE)

    def test_overrides_of_what_protected_mode_sets_refuse_in_every_spelling(self):
        for value in ("chatgpt_base_url=x", "model_provider=x", "model_providers.openai.base_url=x",
                      "features.memories=true", "sandbox_mode=read-only", "web_search=live", "mcp_servers.x.url=y",
                      " profile =x", "experimental_realtime_ws_base_url=x", "otel.exporter=x", "permissions.x=1",
                      '"model_provider"=x', "a b=1", "noequals", "model=gpt-4o"):
            for args in (["-c", value], ["-c" + value], ["-c=" + value], ["--config", value], ["--config=" + value]):
                with self.subTest(args=args):
                    refused(protect.screen, args, NATIVE)

    def test_models_outside_the_reviewed_table_refuse(self):
        for args in (["-m", "gpt-4o"], ["--model=o3"], ["-mgpt-5.4"]):
            with self.subTest(args=args):
                self.assertIn("is not one whose quota", refused(protect.screen, args, NATIVE))

    def test_only_reviewed_releases_start_and_newer_ones_name_the_upper_bound(self):
        self.assertIs(protect.reviewed("codex-cli 0.167.2\n"), NATIVE)
        self.assertIn("newer than the newest reviewed release (0.167.2)", refused(protect.reviewed, "codex-cli 0.168.0"))
        self.assertIn("not a reviewed release", refused(protect.reviewed, "codex-cli 0.166.0"))
        self.assertIn("with a suffix", refused(protect.reviewed, "codex-cli 0.167.2-alpha.1"))
        self.assertIn("not recognized", refused(protect.reviewed, "codex 0.167.2"))
        message = refused(protect.reviewed, "codex-cli 0.168.0")
        self.assertIn("--reserve-5h off --reserve-weekly off launches unprotected", message)

    def test_overrides_route_everything_to_the_gate_with_the_native_sandbox_off(self):
        words = protect.overrides(51234)
        pairs = dict(words[index + 1].split("=", 1) for index in range(0, len(words), 2))
        self.assertEqual(set(words[::2]), {"-c"})
        self.assertEqual(pairs["chatgpt_base_url"], '"http://127.0.0.1:51234/backend-api/"')
        self.assertEqual(pairs["model_provider"], '"quota_gate"')
        self.assertEqual(pairs["model_providers.quota_gate.base_url"], '"http://127.0.0.1:51234/backend-api/codex"')
        self.assertEqual(pairs["model_providers.quota_gate.supports_websockets"], "false")
        self.assertEqual(pairs["model_providers.quota_gate.request_max_retries"], "0")
        self.assertEqual(pairs["sandbox_mode"], '"danger-full-access"')
        self.assertEqual(pairs["experimental_realtime_ws_base_url"], '"http://127.0.0.1:51234"')
        for name in protect.FORCED_OFF:
            self.assertEqual(pairs["features." + name], "false")
        self.assertNotIn("localhost", " ".join(words))

    def test_home_its_ancestors_and_launch_agents_are_never_writable(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            home = base / "home"
            (home / "Library/LaunchAgents/x").mkdir(parents=True)
            work = base / "work"
            work.mkdir()
            self.assertEqual(protect.writable(home, [work, home / ".codex"]), [str(work), str(home / ".codex")])
            for path in (home, base, Path("/"), home / "Library", home / "Library/LaunchAgents",
                         home / "Library/LaunchAgents/x"):
                with self.subTest(path=path):
                    self.assertIn("start from a project directory", refused(protect.writable, home, [path]))
            link = base / "link"
            link.symlink_to(home)
            refused(protect.writable, home, [link])

    def test_logins_need_a_reviewed_plan_and_no_fedramp(self):
        raw = json.dumps(backend.credential()).encode()
        self.assertEqual(protect.login(raw, NATIVE, layout), protect.Login("acct-fixture-A", "user-fixture-A", "pro"))
        for data, text in ((backend.credential(plan="free"), "plan (free)"),
                           (backend.credential(plan="self_serve_business_usage_based"), "is not one whose windows"),
                           (backend.credential(fedramp=True), "FedRAMP"),
                           ({"OPENAI_API_KEY": "sk-fixture", "tokens": None}, "API-key login")):
            with self.subTest(text=text):
                self.assertIn(text, refused(protect.login, json.dumps(data).encode(), NATIVE, layout))
        self.assertIn("edu_pro", NATIVE.plans)


class Reader(unittest.TestCase):
    """The monitor's reader: its boundary comes from the argv the launcher builds, and it reads its
    specification file only inside that boundary."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.spec = Path(temp.name) / "poll.json"
        self.spec.write_text(json.dumps({"command": ["codex"], "env": {}, "cwd": "/", "mode": "read", "timeout": 1,
                                         # what a boundary process could write there: ignored
                                         "port": 443, "writable": ["/"], "profile": "(allow default)"}))
        self.calls = []

    def main(self, argv, refuse=False):
        def confine(port, writable, provider):
            self.calls.append(("confine", port, writable, provider))
            if refuse:
                raise RuntimeError("refused")

        def exchange(spec):
            self.calls.append(("exchange", spec["mode"]))
            return 0

        with mock.patch.object(reader, "sandbox_module", return_value=types.SimpleNamespace(confine=confine)), \
                mock.patch.object(reader, "exchange", side_effect=exchange), \
                mock.patch.object(reader.sys, "platform", "linux"):
            return reader.main(argv)

    def test_the_boundary_comes_from_the_argv_and_applies_before_the_spec_is_read(self):
        limits = json.dumps({"port": 50123, "writable": ["/w"], "profile": None})
        self.assertEqual(self.main(["--confine", limits, str(self.spec)]), 0)
        self.assertEqual(self.calls, [("confine", 50123, ["/w"], "codex"), ("exchange", "read")])
        self.calls = []
        self.spec.write_text("not json")
        self.assertEqual(self.main(["--confine", limits, str(self.spec)]), 6)
        self.assertEqual(self.calls, [("confine", 50123, ["/w"], "codex")])

    def test_a_writable_path_that_became_a_link_refuses_before_the_boundary_applies(self):
        """The launcher resolved the writable paths once; a process inside the boundary may later replace
        a nested one (a SQLite home inside codex_home, say) with a link to HOME, so a path that no longer
        resolves to itself refuses instead of widening the next reader's boundary."""
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            (base / "home").mkdir()
            inner = base / "codex/sqlite"
            inner.mkdir(parents=True)
            limits = json.dumps({"port": 50123, "writable": [str(base / "codex"), str(inner)], "profile": None})
            self.assertEqual(self.main(["--confine", limits, str(self.spec)]), 0)
            self.calls = []
            inner.rmdir()
            inner.symlink_to(base / "home")
            self.assertEqual(self.main(["--confine", limits, str(self.spec)]), 5)
            self.assertEqual(self.calls, [])

    def test_an_unusable_or_refused_boundary_runs_nothing(self):
        for limits in ("[]", "not json", '{"port": 0, "writable": [], "profile": null}',
                       '{"port": true, "writable": [], "profile": null}', '{"port": 1, "writable": "/", "profile": null}',
                       '{"port": 1, "writable": [], "profile": null, "command": ["sh"]}', '{"port": 1, "writable": []}'):
            with self.subTest(limits=limits):
                self.assertEqual(self.main(["--confine", limits, str(self.spec)]), 6)
        self.assertEqual(self.main(["--confine", str(self.spec)]), 6)
        self.assertEqual(self.calls, [])
        limits = json.dumps({"port": 50123, "writable": [], "profile": None})
        self.assertEqual(self.main(["--confine", limits, str(self.spec)], refuse=True), 5)
        self.assertEqual([call[0] for call in self.calls], ["confine"])


def fragment(text):
    return {"id": "fixture", "name": "fixture", "contents": text}


def bundle(*requirements, config=()):
    return {"config_toml": {"enterprise_managed": [fragment(text) for text in config]},
            "requirements_toml": {"enterprise_managed": [fragment(text) for text in requirements]}}


class Requirements(unittest.TestCase):
    """D14 and the E0 note: managed requirements that forbid what protected mode sets."""

    sqlite = "/fixture/.codex"

    def blockers(self, *requirements, config=()):
        return protect.bundle_blockers(bundle(*requirements, config=config), self.sqlite)

    def test_a_bundle_without_blockers_passes(self):
        self.assertEqual(self.blockers(), set())
        self.assertEqual(protect.bundle_blockers({"config_toml": None, "requirements_toml": None}, self.sqlite), set())
        self.assertEqual(self.blockers('allowed_sandbox_modes = ["read-only", "danger-full-access"]\n'
                                       'allowed_web_search_modes = ["cached"]\n'
                                       'allowed_approval_policies = ["on-request"]\n[features]\nfast_mode = false\n',
                                       "[application.network]\nenabled = false\n",
                                       "[experimental_network]\nenabled = false\n",
                                       'chatgpt_base_url = "https://x"\ncli_auth_credentials_store = "keyring"\n',
                                       'sqlite_home = "/fixture/.codex/"\ncheck_for_update_on_startup = false\n'), set())

    def test_application_network_restrictions_refuse_as_native_composes_them(self):
        self.assertEqual(self.blockers("[application.network]\n"), {"application_network"})
        self.assertEqual(self.blockers("[application.network]\nenabled = true\n"), {"application_network"})
        # Highest priority first: an unset enabled above an explicit false stays false.
        self.assertEqual(self.blockers('[application.network.domains]\n"x.example" = "allow"\n',
                                       "[application.network]\nenabled = false\n"), set())
        self.assertEqual(self.blockers("[application.network]\nenabled = true\n",
                                       "[application.network]\nenabled = false\n"), {"application_network"})
        self.assertEqual(self.blockers("[application]\nother = 1\n"), {"requirements_unreadable"})

    def test_network_controls_refuse_unless_explicitly_disabled(self):
        for text in ("[experimental_network]\nenabled = true\n", "[experimental_network]\nhttp_port = 1\n",
                     "[network_proxy]\nenabled = true\n", "[permissions.network]\nenabled = true\n"):
            with self.subTest(text=text):
                self.assertEqual(self.blockers(text), {"network_requirements"})
        self.assertEqual(self.blockers("[experimental_network]\nenabled = true\n[network_proxy]\nenabled = false\n"),
                         {"requirements_unreadable"})

    def test_pins_of_forced_keys_refuse(self):
        cases = {'model_provider = "x"\n': "pins_model_provider", "[model_providers.x]\nname = 1\n": "pins_model_provider",
                 "enforce_residency = true\n": "pins_residency", 'config_server = "https://x"\n': "config_server",
                 'sqlite_home = "/elsewhere"\n': "pins_sqlite_home", "check_for_update_on_startup = true\n":
                 "pins_update_check", 'allowed_sandbox_modes = ["read-only", "workspace-write"]\n':
                 "sandbox_mode_not_allowed", "[features]\nmemories = true\n": "pins_feature",
                 "[feature_requirements]\nplugins = true\n": "pins_feature",
                 "[features]\nuse_agent_identity = true\n": "agent_identity"}
        for text, reason in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.blockers(text), {reason})
        self.assertEqual(self.blockers(config=("[features]\nuse_agent_identity = true\n",)), {"agent_identity"})

    def test_unreadable_bundles_refuse(self):
        for document in (None, [], {"requirements_toml": []}, {"requirements_toml": {"enterprise_managed": {}}},
                         {"requirements_toml": {"enterprise_managed": [{"contents": 1}]}},
                         bundle("not = [toml"), bundle("[features]\nx = 1\n[feature_requirements]\ny = 1\n")):
            with self.subTest(document=document):
                self.assertEqual(protect.bundle_blockers(document, self.sqlite), {"requirements_unreadable"})

    def test_local_requirements_keep_the_auth_pins_native_drops_from_cloud_layers(self):
        with tempfile.TemporaryDirectory() as temp:
            system, home = Path(temp) / "requirements.toml", Path(temp) / "account"
            home.mkdir()
            system.write_text('chatgpt_base_url = "https://x"\n')
            with mock.patch.object(protect, "SYSTEM_REQUIREMENTS", system):
                self.assertIn("pin chatgpt_base_url", refused(protect.requirement_check, home, self.sqlite))
                system.write_text('cli_auth_credentials_store = "file"\n')
                protect.requirement_check(home, self.sqlite)
                system.write_text("not toml [")
                self.assertIn("cannot be read", refused(protect.requirement_check, home, self.sqlite))
                system.unlink()
                cache = {"signed_payload": dict(self.payload(), bundle=bundle("[application.network]\n"))}
                (home / protect.BUNDLE_CACHE).write_text(json.dumps(cache))
                message = refused(protect.requirement_check, home, self.sqlite)
                self.assertIn("application network", message)
                self.assertIn("from the cached cloud requirements in %s" % (home / protect.BUNDLE_CACHE), message)
                cache["signed_payload"]["version"] = 2  # native ignores another cache version
                (home / protect.BUNDLE_CACHE).write_text(json.dumps(cache))
                protect.requirement_check(home, self.sqlite)
                cache["signed_payload"].update(version=1, expires_at="2000-01-01T00:00:00Z")  # native refetches
                (home / protect.BUNDLE_CACHE).write_text(json.dumps(cache))
                protect.requirement_check(home, self.sqlite)

    @staticmethod
    def payload(**changes):
        return dict({"version": 1, "chatgpt_user_id": USER, "account_id": ACCOUNT,
                     "cached_at": "2026-11-12T00:00:00Z", "expires_at": "2999-01-01T00:00:00.123456789Z"}, **changes)

    def test_only_a_bundle_cache_native_would_load_is_checked(self):
        identity = protect.Login(ACCOUNT, USER, "edu_pro")
        self.assertTrue(protect.cache_current(self.payload(), identity))
        self.assertTrue(protect.cache_current(self.payload(), None))
        self.assertTrue(protect.cache_current(self.payload(expires_at="2026-11-12T10:00:00+00:00"), identity,
                                              now=time.mktime((2026, 11, 12, 0, 0, 0, 0, 0, 0))))
        self.assertTrue(protect.cache_current(self.payload(expires_at="garbled"), identity))  # unreadable: checked
        for changes in ({"expires_at": "2000-01-01T00:00:00Z"}, {"account_id": "acct-B"}, {"chatgpt_user_id": None},
                        {"version": 2}):
            with self.subTest(changes=changes):
                self.assertFalse(protect.cache_current(self.payload(**changes), identity))
        self.assertFalse(protect.cache_current(None, identity))

    def test_linked_documents_are_followed_as_native_follows_them(self):
        with tempfile.TemporaryDirectory() as temp:
            real, link, home = Path(temp) / "real.toml", Path(temp) / "requirements.toml", Path(temp) / "account"
            home.mkdir()
            real.write_text('chatgpt_base_url = "https://x"\n')
            link.symlink_to(real)
            with mock.patch.object(protect, "SYSTEM_REQUIREMENTS", link), \
                    mock.patch.object(protect, "SYSTEM_MANAGED_CONFIG", Path(temp) / "absent.toml"):
                self.assertIn("pin chatgpt_base_url", refused(protect.requirement_check, home, self.sqlite))
            (Path(temp) / "device.toml").symlink_to("/dev/null")
            self.assertIn("cannot be read", refused(protect.read_document, Path(temp) / "device.toml"))

    def test_managed_configuration_that_would_override_protected_settings_refuses(self):
        with tempfile.TemporaryDirectory() as temp:
            managed, home = Path(temp) / "managed_config.toml", Path(temp) / "account"
            home.mkdir()
            with mock.patch.object(protect, "SYSTEM_MANAGED_CONFIG", managed), \
                    mock.patch.object(protect, "SYSTEM_REQUIREMENTS", Path(temp) / "absent.toml"):
                for text, key in (('sandbox_mode = "workspace-write"\n', "sandbox_mode"),
                                  ('chatgpt_base_url = "https://x"\n', "chatgpt_base_url"),
                                  ('[model_providers.quota_gate]\nenv_key = "K"\n', "model_providers.quota_gate"),
                                  ("[features]\nmemories = true\n", "features.memories"),
                                  ('web_search = "live"\n', "web_search"), ("analytics = false\n", "analytics.enabled"),
                                  ('sqlite_home = "/elsewhere"\n', "sqlite_home")):
                    with self.subTest(text=text):
                        managed.write_text(text)
                        message = refused(protect.requirement_check, home, self.sqlite)
                        self.assertIn("managed configuration", message)
                        self.assertIn("%s sets %s" % (managed, key), message)
                for text in ('model = "gpt-5.5"\n[analytics]\nenabled = false\n',
                             'web_search = "disabled"\nsqlite_home = "/fixture/.codex"\n[otel]\nenvironment = "x"\n'):
                    with self.subTest(text=text):
                        managed.write_text(text)
                        protect.requirement_check(home, self.sqlite)

    def test_macos_forced_managed_preferences_are_read_where_present(self):
        with tempfile.TemporaryDirectory() as temp:
            managed, home = Path(temp) / "Managed Preferences", Path(temp) / "account"
            (managed / pwd_name()).mkdir(parents=True)
            home.mkdir()
            encoded = base64.b64encode(b"[application.network]\nenabled = true\n").decode()
            (managed / pwd_name() / protect.MANAGED_DOMAIN).write_bytes(
                plistlib.dumps({"requirements_toml_base64": encoded}))
            with mock.patch.object(protect, "MANAGED_PREFERENCES", managed), \
                    mock.patch.object(protect, "SYSTEM_REQUIREMENTS", Path(temp) / "absent.toml"), \
                    mock.patch.object(protect, "SYSTEM_MANAGED_CONFIG", Path(temp) / "absent.toml"), \
                    mock.patch.object(protect.sys, "platform", "darwin"):
                self.assertIn("application network", refused(protect.requirement_check, home, self.sqlite))
                config = base64.b64encode(b'sandbox_mode = "read-only"\n').decode()
                (managed / pwd_name() / protect.MANAGED_DOMAIN).write_bytes(plistlib.dumps({"config_toml_base64": config}))
                self.assertIn("sets sandbox_mode", refused(protect.requirement_check, home, self.sqlite))
                (managed / protect.MANAGED_DOMAIN).write_bytes(b"not a plist")
                self.assertIn("cannot be read", refused(protect.requirement_check, home, self.sqlite))


class Upstream:
    """A plain HTTP upstream on 127.0.0.1 standing in for chatgpt.com in process."""

    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(32)
        self.port = self.listener.getsockname()[1]
        self.requests, self.document = [], None
        self.bundle = None
        self.close_header = True  # chatgpt.com answers a Connection: close request in kind
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.listener.close()

    def _accept(self):
        while True:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock):
        with sock:
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = sock.recv(65536)
                if not chunk:
                    return
                raw += chunk
            head, _, body = raw.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = dict(line.split(": ", 1) for line in lines[1:])
            length = int(headers.get("Content-Length", "0"))
            while len(body) < length:
                body += sock.recv(65536)
            self.requests.append((method, target, headers, body))
            if target == "/backend-api/wham/usage":
                payload = json.dumps(self.document).encode()
            elif target == "/backend-api/wham/config/bundle":
                payload = json.dumps(self.bundle).encode()
            else:
                payload = b'{"ok":true}'
            close = b"Connection: close\r\n" if self.close_header else b""
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n%s\r\n" % (len(payload), close) + payload)


ACCOUNT, USER = "acct-fixture-A", "user-fixture-A"
TOKEN = backend.credential()["tokens"]["access_token"]


def policy(five_hour="10", weekly="10"):
    setting = lambda window, value: reserve.Setting(window, None if value is None else reserve.Decimal(value), "invocation")
    return reserve.Policy("codex-as", "A", setting("five_hour", five_hour), setting("weekly", weekly))


def usage_document(primary=10, secondary=20, plan="pro", account=ACCOUNT, user=USER, swap=False, **extra):
    windows = [backend.window(primary, 18000), backend.window(secondary, 604800)]
    if swap:
        windows.reverse()
    return dict({"plan_type": plan, "account_id": account, "user_id": user,
                 "rate_limit": {"allowed": True, "limit_reached": False, "primary_window": windows[0],
                                "secondary_window": windows[1]}}, **extra)


def limited(primary, secondary, limit_reached=True, allowed=False):
    """A usage reply whose rate limit carries Codex's limit flags."""
    document = usage_document(primary=primary, secondary=secondary)
    document["rate_limit"].update(limit_reached=limit_reached, allowed=allowed)
    return document


class Provider(unittest.TestCase):
    """The Codex provider and decision adapter on the shared gate, in process, over HTTP."""

    def setUp(self, reserves=None):
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)
        self.upstream.document = usage_document()
        self.provider = provider_gate.CodexProvider(ACCOUNT, USER, NATIVE, "/fixture/.codex",
                                                    upstream=kit.Upstream("127.0.0.1", self.upstream.port, tls=False))
        self.decider = provider_gate.Decider(reserves or policy())
        self.gate = provider_gate.CodexGate(self.provider, self.decider, max_age=120)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        self.server = kit.Server(listener, self.gate)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = listener.getsockname()[1]
        self.gate.attach()
        self.gate.heartbeat()

    def request(self, method, path, body=None, headers=None):
        if headers is None:
            headers = {"Authorization": "Bearer " + TOKEN, "ChatGPT-Account-ID": ACCOUNT}
        lines = ["%s %s HTTP/1.1" % (method, path), "Host: gate"]
        if isinstance(headers, dict):
            headers = list(headers.items())
        lines += ["%s: %s" % pair for pair in headers]
        data = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        if body is not None:
            lines.append("Content-Length: %d" % len(data))
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
            raw = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                raw += chunk
        return int(raw.split(b" ", 2)[1]), raw.partition(b"\r\n\r\n")[2]

    def model_requests(self):
        return [r for r in self.upstream.requests if r[1] == provider_gate.MODEL_PATH]

    def test_a_forwarded_usage_read_binds_and_admits_reviewed_models_only(self):
        self.assertEqual(self.request("GET", "/backend-api/wham/usage")[0], 200)
        self.assertEqual(self.gate.status()["state"], "running")
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 200)
        status, body = self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-4o"})
        self.assertEqual(status, 503)
        self.assertIn(b"quota_reserve_paused", body)
        self.assertEqual(len(self.model_requests()), 1)
        usage = [r for r in self.upstream.requests if r[1] == "/backend-api/wham/usage"]
        self.assertNotIn("Accept-Encoding", usage[0][2])

    def test_a_body_with_a_duplicate_key_is_refused(self):
        self.request("GET", "/backend-api/wham/usage")
        for body in (b'{"model":"gpt-4o","model":"gpt-5.5"}', b'{"model":"gpt-5.5","model":"gpt-4o"}',
                     b'{"model":"gpt-5.5","input":{"a":1,"a":2}}'):
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, body)[0], 503)
        self.assertEqual(self.model_requests(), [])
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, b'{"model":"gpt-5.5","input":"x"}')[0], 200)

    def test_unlisted_routes_and_upgrades_never_reach_upstream(self):
        self.request("GET", "/backend-api/wham/usage")
        before = len(self.upstream.requests)
        for method, path in (("POST", "/backend-api/codex/memories/trace_summarize"), ("POST", "/backend-api/codex/alpha/search"),
                             ("GET", "/backend-api/wham/tasks"), ("GET", "/backend-api/wham/usage?x=1"),
                             ("POST", "/backend-api/wham/usage"), ("GET", "/api/codex/usage")):
            with self.subTest(path=path):
                self.assertIn(self.request(method, path, {"model": "gpt-5.5"} if method == "POST" else None)[0], (404, 503))
        headers = {"Authorization": "Bearer " + TOKEN, "ChatGPT-Account-ID": ACCOUNT, "Upgrade": "websocket"}
        self.assertEqual(self.request("GET", provider_gate.MODEL_PATH, headers=headers)[0], 503)
        self.assertEqual(len(self.upstream.requests), before)

    def test_account_scope_and_duplicate_headers_refuse_before_any_upstream_byte(self):
        foreign = backend.credential(account="acct-B")["tokens"]["access_token"]
        cases = [
            [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN), ("ChatGPT-Account-ID", ACCOUNT)],
            [("Authorization", "Bearer " + TOKEN), ("ChatGPT-Account-ID", ACCOUNT), ("chatgpt-account-id", ACCOUNT)],
            [("Authorization", "Bearer " + TOKEN)],
            [("Authorization", "Bearer " + TOKEN), ("ChatGPT-Account-ID", "acct-B")],
            [("Authorization", "Bearer " + foreign), ("ChatGPT-Account-ID", ACCOUNT)],
            [("Authorization", "Bearer " + TOKEN + " second"), ("ChatGPT-Account-ID", ACCOUNT)],
            [("Authorization", "Bearer " + TOKEN), ("ChatGPT-Account-ID", ACCOUNT), ("X-Api-Key", "k")],
            [("Authorization", "Bearer " + TOKEN), ("ChatGPT-Account-ID", ACCOUNT), ("X-OpenAI-Fedramp", "true")],
        ]
        for headers in cases:
            with self.subTest(headers=[name for name, _ in headers]):
                self.assertEqual(self.request("GET", "/backend-api/wham/usage", headers=headers)[0], 503)
        self.assertEqual(self.upstream.requests, [])

    def test_a_foreign_account_on_a_model_request_latches(self):
        self.request("GET", "/backend-api/wham/usage")
        headers = {"Authorization": "Bearer " + TOKEN, "ChatGPT-Account-ID": "acct-B"}
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"}, headers=headers)[0], 503)
        status = self.gate.status()
        self.assertEqual((status["state"], status["reason"]), ("paused", "foreign_account"))
        self.assertIn("account changed", status["fields"]["detail"])
        self.assertEqual(self.model_requests(), [])

    def test_windows_map_by_duration_never_by_slot(self):
        self.upstream.document = usage_document(primary=20, secondary=95, swap=True)  # weekly in the first slot
        self.request("GET", "/backend-api/wham/usage")
        status = self.gate.status()
        self.assertEqual((status["state"], status["fields"]["window"]), ("paused", "weekly"))
        observation = self.provider.usage(200, json.dumps(usage_document(primary=95, swap=True)).encode(), 1.0)
        names = {reading.duration: reading.used for reading in observation.data["readings"]}
        self.assertEqual(names, {18000: 95, 604800: 20})

    def test_a_threshold_pauses_with_the_message_fields_and_closes_admission(self):
        self.request("GET", "/backend-api/wham/usage")
        self.upstream.document = usage_document(primary=91)
        self.request("GET", "/backend-api/wham/usage")
        status = self.gate.status()
        self.assertEqual((status["state"], status["reason"]), ("paused", "threshold"))
        fields = status["fields"]
        self.assertEqual((fields["account"], fields["window"], fields["remaining"], fields["reserve"]),
                         ("A", "five-hour", "9", "10"))
        self.assertIn("reset", fields)
        self.assertIn("at or below its 10% reserve", fields["detail"])
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 503)
        self.assertEqual(self.model_requests(), [])

    def test_only_a_continuation_and_a_fresh_passing_read_resume(self):
        self.request("GET", "/backend-api/wham/usage")
        self.upstream.document = usage_document(primary=91)
        self.request("GET", "/backend-api/wham/usage")
        self.upstream.document = usage_document(primary=10)
        self.request("GET", "/backend-api/wham/usage")  # a passing poll never resumes
        self.assertEqual(self.gate.status()["reason"], "awaiting_continuation")
        self.gate.heartbeat()
        self.assertEqual(self.gate.arm_continuation(), {"ok": False, "needs_read": True, "reason": "armed"})
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.gate.status()["state"], "running")
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 200)

    def test_a_failed_continuation_read_is_used_up(self):
        self.request("GET", "/backend-api/wham/usage")
        self.upstream.document = usage_document(primary=91)
        self.request("GET", "/backend-api/wham/usage")
        self.gate.heartbeat()
        self.gate.arm_continuation()
        self.request("GET", "/backend-api/wham/usage")  # still at the reserve: uses the continuation up
        self.upstream.document = usage_document(primary=10)
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual((self.gate.status()["state"], self.gate.status()["reason"]), ("paused", "awaiting_continuation"))

    def test_unsupported_plans_foreign_identities_and_missing_windows_refuse_the_start(self):
        cases = ((usage_document(plan="free"), "unsupported_plan"), (usage_document(user="user-B"), "identity_mismatch"),
                 (usage_document(account=None), "identity_unattributed"),
                 (dict(usage_document(), rate_limit=None), "window_unavailable"),
                 (usage_document(primary=95), "threshold"), (["not", "an", "object"], "no_reading"))
        for document, reason in cases:
            with self.subTest(reason=reason):
                self.setUp()
                self.upstream.document = document
                self.request("GET", "/backend-api/wham/usage")
                status = self.gate.status()
                self.assertEqual((status["state"], status["reason"]), ("paused", reason))

    def test_limit_reached_pauses_although_the_windows_pass(self):
        document = usage_document()
        document["rate_limit"]["limit_reached"] = True
        self.upstream.document = document
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.gate.status()["reason"], "limit_reached")

    def test_a_limit_flag_that_a_window_turned_off_accounts_for_neither_refuses_nor_pauses(self):
        """Codex sets limit_reached and allowed for its rate limit as a whole: a window at its limit that
        the user turned off accounts for them, so the enabled window alone decides, at the start and
        during the run."""
        for reserves, exhausted, flags in ((policy(five_hour=None), (100, 20), {"limit_reached": True, "allowed": False}),
                                           (policy(weekly=None), (20, 100), {"limit_reached": True, "allowed": False}),
                                           (policy(five_hour=None), (100, 20), {"limit_reached": False, "allowed": False}),
                                           (policy(weekly=None), (20, 100), {"limit_reached": True, "allowed": True})):
            with self.subTest(reserves=reserves, exhausted=exhausted, flags=flags):
                self.setUp(reserves)
                self.upstream.document = limited(*exhausted, **flags)
                self.request("GET", "/backend-api/wham/usage")
                self.assertEqual(self.gate.status()["state"], "running", self.gate.status())
                self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 200)
                self.request("GET", "/backend-api/wham/usage")
                self.assertEqual(self.gate.status()["state"], "running", self.gate.status())
                self.setUp(reserves)  # mid-run: the window turned off runs out
                self.request("GET", "/backend-api/wham/usage")
                self.upstream.document = limited(*exhausted, **flags)
                self.request("GET", "/backend-api/wham/usage")
                self.assertEqual(self.gate.status()["state"], "running", self.gate.status())
                self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 200)

    def test_an_enabled_window_at_its_limit_refuses_as_its_own_threshold(self):
        self.setUp(policy(weekly=None))
        self.upstream.document = limited(100, 20)
        self.request("GET", "/backend-api/wham/usage")
        status = self.gate.status()
        self.assertEqual((status["state"], status["reason"], status["fields"]["window"]), ("paused", "threshold", "five-hour"))
        self.assertTrue(status["fields"]["detail"].startswith("A: refused: five-hour window "), status["fields"])
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 503)
        self.assertEqual(self.model_requests(), [])

    def test_a_limit_flag_no_window_accounts_for_refuses_the_start_and_pauses_a_run(self):
        """Fail closed when the flags cannot be attributed: no window at its limit, a turned-off window
        just short of it, a window of another length, or a window without a usage number."""
        other = dict(limited(10, 20), rate_limit=dict(limited(10, 20)["rate_limit"], primary_window=backend.window(100, 3600)))
        broken = dict(limited(10, 20), rate_limit=dict(limited(10, 20)["rate_limit"], primary_window="exhausted"))
        for reserves, document in ((policy(), limited(10, 20)), (policy(five_hour=None), limited(99, 20)),
                                   (policy(five_hour=None), other), (policy(five_hour=None), broken)):
            with self.subTest(reserves=reserves, document=document["rate_limit"]):
                self.setUp(reserves)
                self.upstream.document = document
                self.request("GET", "/backend-api/wham/usage")
                status = self.gate.status()
                self.assertEqual((status["state"], status["reason"]), ("paused", "limit_reached"))
                # A refusal of the start, not a run that paused at once.
                self.assertTrue(status["fields"]["detail"].startswith("A: refused: "), status["fields"])
                self.assertIn("usage limit reached", protect.explain(status["reason"], status["fields"]))
                self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 503)
                self.setUp(reserves)
                self.request("GET", "/backend-api/wham/usage")
                self.assertEqual(self.gate.status()["state"], "running")
                self.upstream.document = document
                self.request("GET", "/backend-api/wham/usage")
                status = self.gate.status()
                self.assertEqual((status["state"], status["reason"]), ("paused", "limit_reached"))
                self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 503)
                self.assertEqual(self.model_requests(), [])

    def test_models_with_their_own_quota_row_are_refused(self):
        self.upstream.document = usage_document(additional_rate_limits=[
            {"limit_name": "gpt-6-luna", "metered_feature": "codex_other", "rate_limit": None}])
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-6-luna"})[0], 503)
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 200)

    def test_the_config_bundle_is_relayed_unchanged_and_blocking_requirements_refuse(self):
        self.upstream.bundle = bundle('allowed_sandbox_modes = ["read-only", "danger-full-access"]\n')
        status, body = self.request("GET", provider_gate.BUNDLE_PATH)
        self.assertEqual((status, json.loads(body)), (200, self.upstream.bundle))
        self.assertEqual(self.gate.status()["state"], "checking")
        self.upstream.bundle = bundle("[application.network]\nenabled = true\n")
        status, body = self.request("GET", provider_gate.BUNDLE_PATH)
        self.assertEqual((status, json.loads(body)), (200, self.upstream.bundle))  # never fabricated
        self.assertEqual(self.gate.status()["reason"], "requirements_application_network")
        self.assertIn("application network", protect.explain("requirements_application_network", {}))

    def test_a_blocking_bundle_applies_although_a_later_quota_read_finished_first(self):
        self.request("GET", "/backend-api/wham/usage")
        presented = kit.Presented(kit.Secret(TOKEN), (ACCOUNT,))
        started = self.gate.last_fresh - 1.0  # both requests started before the usage read that finished first
        stale = self.provider.usage(200, json.dumps(usage_document(primary=95)).encode(), started)
        self.assertEqual(self.gate.observe(presented, stale, "forwarded"), "superseded")  # quota data: newer stands
        verdict = self.provider.bundle(200, json.dumps(bundle("[application.network]\n")).encode(), started)
        self.assertEqual(self.gate.observe(presented, verdict, "forwarded"), "requirements_application_network")
        status = self.gate.status()
        self.assertEqual((status["state"], status["reason"]), ("paused", "requirements_application_network"))
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 503)
        self.assertEqual(self.model_requests(), [])

    def test_a_blocking_bundle_keeps_the_run_paused_until_a_later_bundle_lifts_it(self):
        """Passing usage reads and a continuation never undo a requirement refusal that still applies;
        a later bundle without blockers lifts it, and a continuation then resumes as after any pause."""
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.gate.status()["state"], "running")
        self.upstream.bundle = bundle("[application.network]\nenabled = true\n")
        self.assertEqual(self.request("GET", provider_gate.BUNDLE_PATH)[0], 200)
        self.assertEqual(self.gate.status()["reason"], "requirements_application_network")
        self.request("GET", "/backend-api/wham/usage")  # passes both reserves
        self.assertEqual(self.gate.status()["reason"], "requirements_application_network")
        self.gate.heartbeat()
        self.gate.arm_continuation()
        self.request("GET", "/backend-api/wham/usage")
        status = self.gate.status()
        self.assertEqual((status["state"], status["reason"]), ("paused", "requirements_application_network"))
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 503)
        self.assertEqual(self.model_requests(), [])
        self.upstream.bundle = bundle('allowed_sandbox_modes = ["read-only", "danger-full-access"]\n')
        self.assertEqual(self.request("GET", provider_gate.BUNDLE_PATH)[0], 200)
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.gate.status()["reason"], "awaiting_continuation")
        self.gate.heartbeat()
        self.gate.arm_continuation()
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.gate.status()["state"], "running")
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 200)

    def serve(self, record, mode):
        """Replace the gate as the launcher's restart does: a new process's provider and Decider from the
        run configuration (gate.main's make), on the same requirement record, in MODE."""
        config = {"native": NATIVE.name, "mode": "run", "expected": [ACCOUNT, USER], "sqlite_home": "/fixture/.codex",
                  "account": "A", "reserves": {"five_hour": ["10", "invocation"], "weekly": ["10", "invocation"]},
                  "requirement_file": str(record)}
        self.provider, self.decider = provider_gate.make(config)
        if self.provider.record_fd is not None:
            self.addCleanup(os.close, self.provider.record_fd)
        self.provider.upstream = kit.Upstream("127.0.0.1", self.upstream.port, tls=False)
        self.gate = provider_gate.CodexGate(self.provider, self.decider, max_age=120, mode=mode)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        self.server = kit.Server(listener, self.gate)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = listener.getsockname()[1]
        self.gate.attach()
        self.gate.heartbeat()

    def record(self):
        directory = tempfile.TemporaryDirectory(prefix="codex-reserve-record-")
        self.addCleanup(directory.cleanup)
        record = Path(directory.name) / "requirement"
        record.write_text("none\n")  # as protect.py creates it for the run
        record.chmod(0o600)
        return record

    def continued(self):
        self.gate.heartbeat()
        self.gate.arm_continuation()
        self.request("GET", "/backend-api/wham/usage")
        return self.gate.status()

    def test_a_requirement_pause_carries_over_a_gate_restart_until_a_later_bundle_lifts_it(self):
        """U2 (a), the reviewer's N3 repro: a restarted gate (a new provider and Decider) keeps the run
        paused under the latest bundle's requirement; neither a passing read nor a continuation lifts
        it, only a later bundle without blockers does."""
        record = self.record()
        self.serve(record, "startup")
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.gate.status()["state"], "running")
        self.upstream.bundle = bundle('allowed_sandbox_modes = ["read-only"]\n')
        self.assertEqual(self.request("GET", provider_gate.BUNDLE_PATH)[0], 200)
        self.assertEqual(self.gate.status()["reason"], "requirements_sandbox_mode_not_allowed")
        self.assertEqual(record.read_text(), "requirements_sandbox_mode_not_allowed\n")
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.serve(record, "restart")
        self.request("GET", "/backend-api/wham/usage")  # passes both reserves
        self.assertEqual(self.gate.status()["reason"], "requirements_sandbox_mode_not_allowed")
        status = self.continued()
        self.assertEqual((status["state"], status["reason"]), ("paused", "requirements_sandbox_mode_not_allowed"))
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 503)
        self.assertEqual(self.model_requests(), [])
        self.serve(record, "restart")  # and again: still carried
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.continued()["reason"], "requirements_sandbox_mode_not_allowed")
        self.upstream.bundle = bundle('allowed_sandbox_modes = ["read-only", "danger-full-access"]\n')
        self.assertEqual(self.request("GET", provider_gate.BUNDLE_PATH)[0], 200)
        self.assertEqual(record.read_text(), "none\n")
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.gate.status()["reason"], "awaiting_continuation")
        self.assertEqual(self.continued()["state"], "running")
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 200)
        # A restart after the lift carries nothing over: a continuation resumes as after any restart.
        self.serve(record, "restart")
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.gate.status()["reason"], "awaiting_continuation")
        self.assertEqual(self.continued()["state"], "running")

    def test_a_missing_or_garbled_requirement_record_keeps_a_restarted_gate_paused(self):
        """A record a restarted gate cannot read fails closed, as an unreadable requirement does, until a
        later bundle without blockers lifts it; a refresh gate keeps no record."""
        for name, arrange in (("missing", lambda record: record.unlink()),
                              ("garbled", lambda record: record.write_text("requirements_not_a_blocker\n")),
                              ("other", lambda record: record.write_text("threshold\n"))):
            with self.subTest(name=name):
                record = self.record()
                arrange(record)
                self.serve(record, "restart")
                self.request("GET", "/backend-api/wham/usage")
                self.assertEqual(self.continued()["reason"], "requirements_requirements_unreadable")
                self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 503)
                self.upstream.bundle = bundle('allowed_sandbox_modes = ["read-only", "danger-full-access"]\n')
                self.assertEqual(self.request("GET", provider_gate.BUNDLE_PATH)[0], 200)
                self.assertEqual(self.continued()["state"], "running")
                self.upstream.bundle = None
        provider, _ = provider_gate.make({"native": NATIVE.name, "mode": "refresh", "expected": [ACCOUNT, USER],
                                          "sqlite_home": "/fixture/.codex", "account": "A",
                                          "reserves": {"five_hour": ["10", "invocation"], "weekly": [None, "default"]},
                                          "requirement_file": "/nonexistent/requirement", "connect_allow": {}})
        self.assertIsNone(provider.requirement)
        self.assertIsNone(provider.carry)

    def test_a_record_that_cannot_be_written_fails_a_restart_closed(self):
        """The gate writes the record in place through the descriptor it opened it with, so a directory it
        can no longer write changes nothing; a blocking verdict it cannot write at all removes the record,
        which a restarted gate reads as unreadable (fail closed)."""
        record = self.record()
        self.serve(record, "startup")
        self.request("GET", "/backend-api/wham/usage")
        record.parent.chmod(0o500)  # no new entry, rename or removal in the private directory
        self.addCleanup(record.parent.chmod, 0o700)
        self.upstream.bundle = bundle('allowed_sandbox_modes = ["read-only"]\n')
        self.request("GET", provider_gate.BUNDLE_PATH)
        self.assertEqual(self.gate.status()["reason"], "requirements_sandbox_mode_not_allowed")
        self.assertEqual(record.read_text(), "requirements_sandbox_mode_not_allowed\n")
        self.assertEqual(sorted(path.name for path in record.parent.iterdir()), ["requirement"])
        record.parent.chmod(0o700)
        self.serve(record, "restart")
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.continued()["reason"], "requirements_sandbox_mode_not_allowed")
        # Writes through the descriptor fail now (it is read-only): the blocking verdict removes the record.
        self.upstream.bundle = bundle('allowed_sandbox_modes = ["read-only", "danger-full-access"]\n')
        self.request("GET", provider_gate.BUNDLE_PATH)
        self.assertEqual(record.read_text(), "none\n")
        readonly = os.open(record, os.O_RDONLY)
        os.dup2(readonly, self.provider.record_fd)
        os.close(readonly)
        self.upstream.bundle = bundle('allowed_sandbox_modes = ["read-only"]\n')
        self.request("GET", provider_gate.BUNDLE_PATH)
        self.assertEqual(self.gate.status()["reason"], "requirements_sandbox_mode_not_allowed")
        self.assertFalse(record.exists())
        self.serve(record, "restart")
        self.request("GET", "/backend-api/wham/usage")
        self.assertEqual(self.continued()["reason"], "requirements_requirements_unreadable")

    def test_the_record_is_opened_safely_and_a_run_gate_needs_one(self):
        """The record must be a private regular file of this user, opened without following a link or
        blocking on a FIFO and writable by the gate; anything else is unreadable (fail closed). A run gate
        without a record is refused, so the carry-over never silently stops."""
        run = {"native": NATIVE.name, "mode": "run", "expected": [ACCOUNT, USER], "sqlite_home": "/fixture/.codex",
               "account": "A", "reserves": {"five_hour": ["10", "invocation"], "weekly": ["10", "invocation"]}}
        for value in (None, "relative/requirement", 7):
            with self.subTest(requirement_file=value), self.assertRaises(kit.Refusal) as caught:
                provider_gate.make(dict(run, **({} if value is None else {"requirement_file": value})))
            self.assertEqual(caught.exception.reason, "requirement_record_invalid")
        directory = Path(tempfile.mkdtemp(prefix="codex-reserve-record-"))
        self.addCleanup(shutil.rmtree, directory)
        target = directory / "target"
        target.write_text("none\n")
        target.chmod(0o600)
        cases = {"symlink": lambda path: path.symlink_to(target), "fifo": lambda path: os.mkfifo(path, 0o600),
                 "shared": lambda path: (path.write_text("none\n"), path.chmod(0o640)),
                 "read-only": lambda path: (path.write_text("none\n"), path.chmod(0o400)),
                 "directory": lambda path: path.mkdir(mode=0o700)}
        for name, arrange in cases.items():
            with self.subTest(name=name):
                path = directory / name
                arrange(path)
                provider, decider = provider_gate.make(dict(run, requirement_file=str(path)))
                self.assertEqual(provider.requirement, "requirements_requirements_unreadable")
                self.assertIsNone(provider.record_fd)
        provider, _ = provider_gate.make(dict(run, requirement_file=str(target)))
        self.assertIsNone(provider.requirement)
        self.assertIsInstance(provider.record_fd, int)
        self.assertTrue(os.get_inheritable(provider.record_fd) is False)
        os.close(provider.record_fd)

    def test_the_bundle_route_stays_open_while_paused(self):
        self.request("GET", "/backend-api/wham/usage")
        self.upstream.document = usage_document(primary=91)
        self.request("GET", "/backend-api/wham/usage")
        self.upstream.bundle = bundle()
        self.assertEqual(self.request("GET", provider_gate.BUNDLE_PATH)[0], 200)
        self.assertEqual(self.request("GET", "/backend-api/codex/models?client_version=0.167.2")[0], 503)

    def test_a_rotated_bearer_is_bound_by_a_gate_read_with_its_own_token(self):
        self.upstream.close_header = False
        self.request("GET", "/backend-api/wham/usage")
        rotated = backend.credential(access="A1r1")["tokens"]["access_token"]
        headers = {"Authorization": "Bearer " + rotated, "ChatGPT-Account-ID": ACCOUNT}
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"}, headers=headers)[0], 200)
        reads = [r for r in self.upstream.requests if r[1] == "/backend-api/wham/usage"]
        self.assertEqual(reads[-1][2]["Authorization"], "Bearer " + rotated)
        self.assertEqual(reads[-1][2]["ChatGPT-Account-ID"], ACCOUNT)
        self.assertEqual(reads[-1][2]["User-Agent"], kit.USER_AGENT)
        self.assertEqual(self.gate.status()["epoch"], 2)

    def test_a_gate_read_survives_an_upstream_that_closes_the_connection(self):
        """A gate read of a reply that says Connection: close, as chatgpt.com sends, binds the bearer."""
        self.request("GET", "/backend-api/wham/usage")
        rotated = backend.credential(access="A1r1")["tokens"]["access_token"]
        headers = {"Authorization": "Bearer " + rotated, "ChatGPT-Account-ID": ACCOUNT}
        self.assertEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"}, headers=headers)[0], 200)

    def test_refusals_are_503_never_401(self):
        self.assertEqual(self.provider.refusal("paused")[0], 503)
        self.assertNotEqual(self.request("POST", provider_gate.MODEL_PATH, {"model": "gpt-5.5"})[0], 401)


class Decisions(unittest.TestCase):
    """The decision adapter alone, on an injected clock."""

    def setUp(self):
        self.now = 1000.0
        self.decider = provider_gate.Decider(policy(), clock=lambda: self.now)

    def observation(self, primary=10, at=None):
        data = {"readings": provider_gate.readings(usage_document(primary=primary)["rate_limit"]), "wall": time.time()}
        return kit.Event("observation", self.now if at is None else at, observation=kit.Observation(0, True, data=data))

    def test_decisions_are_well_formed_for_the_gate(self):
        for event in (self.observation(), kit.Event("health", self.now, name="stale"),
                      kit.Event("health", self.now, name="monitor_eof")):
            decision = self.decider(event)
            self.assertTrue(kit._well_formed(decision), decision)

    def test_a_restarted_gate_starts_paused_and_needs_a_continuation(self):
        decider = provider_gate.Decider(policy(), clock=lambda: self.now)
        decision = decider(kit.Event("health", self.now, name="gate_restart"))
        self.assertEqual((decision.running, decision.reason), (False, "gate_restarted"))
        self.now += 1
        self.assertFalse(decider(self.observation()).running)
        self.now += 1
        armed = self.now
        decider(kit.Event("continuation", armed))
        self.now += 1
        self.assertTrue(decider(self.observation(at=armed + 0.5)).running)

    def test_monitor_loss_is_final_for_the_evaluator(self):
        self.assertTrue(self.decider(self.observation()).running)
        self.assertEqual(self.decider(kit.Event("health", self.now, name="monitor_stale")).reason, "monitor_stale")
        self.now += 1
        self.decider(kit.Event("continuation", self.now))
        self.now += 1
        self.assertFalse(self.decider(self.observation()).running)

    def test_failures_keep_running_within_max_age_and_stale_pauses(self):
        self.assertTrue(self.decider(self.observation()).running)
        self.now += 60
        failed = kit.Observation(self.now, False, reason="http_503")
        self.assertTrue(self.decider(kit.Event("health", self.now, name="quota_unavailable", observation=failed)).running)
        self.now += 61
        self.assertEqual(self.decider(kit.Event("health", self.now, name="stale")).reason, "stale")

    def test_reason_words_never_collide_with_the_evaluators_own(self):
        for name in ("stale", "threshold", "monitor_lost", "Bad Reason!", "9x", ""):
            word = provider_gate.word(name)
            self.assertRegex(word, r"^[a-z][a-z0-9_]{0,63}$")
            self.assertNotIn(word, provider_gate.RESERVED)


# -- whole launches -----------------------------------------------------------------------------------


class Home:
    """A temporary HOME with a codex-as account A, a fake codex and a workspace outside HOME."""

    def __init__(self, test, real_codex=None):
        self.temp = tempfile.TemporaryDirectory(prefix="quota-x-test-")
        test.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.codex_home = self.home / ".codex"
        self.secrets = self.home / ".secrets"
        for path in (self.home, self.codex_home, self.secrets):
            path.mkdir(mode=0o700)
        (self.codex_home / "config.toml").write_text('model = "gpt-5.5"\n')
        self.work = self.base / "work"
        self.work.mkdir()
        self.tmp = self.base / "tmp"
        self.tmp.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.codex_home / "sessions/fake-codex.log"
        if real_codex is None:
            fake = self.bin / "codex"
            fake.write_text("#!%s\n" % sys.executable + (FIXTURES / "fake_codex.py").read_text())
            fake.chmod(0o700)
            # The databases native 0.167.2 keeps in the shared SQLite home (codex_home by default),
            # which protected mode writes as files only; the fake codex never opens them.
            for name in (*NATIVE.databases, *NATIVE.later_databases):
                (self.codex_home / (name + ".sqlite")).write_bytes(b"")
        else:
            (self.bin / "codex").symlink_to(real_codex)
        self.write_credential(backend.credential())
        self.env = {name: value for name, value in os.environ.items() if name not in KIT_KNOBS
                    and not name.lower().endswith("_proxy") and not name.startswith(("CODEX_", "OPENAI_", "SSL_"))}
        # The launcher runs python3 from PATH: this interpreter first (macOS /usr/bin/python3 is older than 3.11).
        python = os.path.dirname(os.path.realpath(sys.executable))
        self.env.update(HOME=str(self.home), PATH=os.pathsep.join((str(self.bin), python, "/usr/bin", "/bin")),
                        TMPDIR=str(self.tmp), PYTHONDONTWRITEBYTECODE="1",
                        TERM="xterm-256color")

    def write_credential(self, data):
        path = self.secrets / "codex-A.json"
        path.write_text(json.dumps(data))
        path.chmod(0o600)

    def secret(self):
        return json.loads((self.secrets / "codex-A.json").read_text())

    def events(self, name=None):
        if not self.log.exists():
            return []
        events = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [event for event in events if name is None or event["event"] == name]

    def knobs(self, **values):
        """Fake codex settings that App Server readers see too (their environment is allowlisted)."""
        (self.codex_home / "fake-codex.json").write_text(json.dumps(values))

    def config(self, text):
        """The kit configuration file, private whatever the caller's umask: the loader refuses a
        group-writable one unless the group is the user's private group, which a macOS group is not."""
        path = self.home / ".config/coding-agent-kit"
        path.mkdir(parents=True, exist_ok=True)
        for directory in (path.parent, path):
            directory.chmod(0o700)
        (path / "config.toml").write_text(text)
        (path / "config.toml").chmod(0o600)

    def run(self, args, timeout=30, env=None):
        """A launch without a terminal (stdin is /dev/null)."""
        return subprocess.run([str(LAUNCHER), *args], cwd=self.work, env=dict(self.env, **(env or {})),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)


class Terminal:
    """A launch on a pseudo-terminal of its own, driven like a user would."""

    def __init__(self, argv, env, cwd):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.chdir(cwd)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        self.output, self.status, self.cursor = b"", None, 0

    # What a terminal answers to the queries a real TUI sends at startup.
    ANSWERS = ((b"\x1b]10;?", b"\x1b]10;rgb:cccc/cccc/cccc\x1b\\"), (b"\x1b]11;?", b"\x1b]11;rgb:0000/0000/0000\x1b\\"),
               (b"\x1b[6n", b"\x1b[1;1R"), (b"\x1b[c", b"\x1b[?62;22c"), (b"\x1b[?u", b"\x1b[?0u"))
    answer = False

    def read(self, timeout):
        if select.select([self.fd], [], [], timeout)[0]:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            self.output += data
            if self.answer:
                for query, reply in self.ANSWERS:
                    for _ in range(data.count(query)):
                        self.send(reply)
            return bool(data)
        return True

    def expect(self, pattern, timeout=60):
        """The first match after the previous one."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = re.compile(pattern.encode()).search(self.output, self.cursor)
            if match:
                self.cursor = match.end()
                return match
            if not self.read(0.1):
                break
        raise AssertionError("expected %r; terminal output:\n%s" % (pattern, self.output.decode("utf-8", "replace")))

    def send(self, data):
        os.write(self.fd, data.encode() if isinstance(data, str) else data)

    def wait(self, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = status
                while self.read(0.05) and select.select([self.fd], [], [], 0)[0]:
                    pass
                return status
            self.read(0.05)
        os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)
        raise AssertionError("the launcher did not exit; output:\n%s" % self.output.decode("utf-8", "replace"))

    def close(self):
        if self.status is None:
            with contextlib.suppress(ProcessLookupError, ChildProcessError):
                os.kill(self.pid, signal.SIGKILL)
                os.waitpid(self.pid, 0)
        with contextlib.suppress(OSError):
            os.close(self.fd)


def leftovers(base):
    """Processes whose command line names the test root (gate, monitor, readers, natives)."""
    found = []
    for name in os.listdir("/proc") if os.path.isdir("/proc") else ():
        if name.isdigit():
            try:
                cmdline = Path("/proc", name, "cmdline").read_bytes()
            except OSError:
                continue
            if str(base).encode() in cmdline and int(name) != os.getpid():
                found.append(int(name))
    if not os.path.isdir("/proc"):
        out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "command="], capture_output=True, text=True).stdout
        found = [int(line.split(None, 1)[0]) for line in out.splitlines()
                 if str(base) in line and int(line.split(None, 1)[0]) != os.getpid()]
    return found


def descendants(pid, needle):
    """Descendants of PID (from the system ps) whose command line contains NEEDLE."""
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=", "-o", "ppid=", "-o", "command="], capture_output=True,
                         text=True).stdout
    rows = [line.split(None, 2) for line in out.splitlines() if len(line.split(None, 2)) == 3]
    children = {}
    for child, parent, command in rows:
        children.setdefault(int(parent), []).append((int(child), command))
    found, stack = [], [pid]
    while stack:
        for child, command in children.get(stack.pop(), ()):
            stack.append(child)
            if needle in command:
                found.append(child)
    return found


def command_lines(base):
    """{pid: command line} of the processes whose command line names the test root."""
    lines = {}
    for pid in leftovers(base):
        try:
            if os.path.isdir("/proc"):
                lines[pid] = Path("/proc", str(pid), "cmdline").read_bytes().replace(b"\0", b" ").decode()
            else:
                lines[pid] = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True,
                                            text=True).stdout.strip()
        except OSError:
            continue
    return lines


class Unprotected(unittest.TestCase):
    """Launches with every window off are byte-identical to today's; refusals need no boundary."""

    def setUp(self):
        self.home = Home(self)

    def launch_record(self, args):
        self.home.log.unlink(missing_ok=True)
        result = self.home.run(args)
        self.assertEqual(result.returncode, 0, result.stderr)
        (record,) = self.home.events("tui")
        del record["pid"]
        return record, result.stderr

    def test_flags_off_and_reserves_off_launch_exactly_as_without_them(self):
        plain = self.launch_record(["A", "fix it"])
        for args in (["--reserve-5h", "off", "--reserve-weekly", "off", "A", "fix it"],
                     ["--reserve-weekly", "off", "--reserve-5h", "off", "A", "fix it"]):
            with self.subTest(args=args):
                self.assertEqual(self.launch_record(args), plain)
        self.home.config("[usage_reserve]\nfive_hour = 20\n[usage_reserve.codex_accounts.A]\nweekly = 30\n")
        self.assertEqual(self.launch_record(["--reserve-5h", "off", "--reserve-weekly", "off", "A", "fix it"]), plain)
        self.home.config("[usage_reserve]\nfive_hour = \"off\"\n[usage_reserve.codex_accounts.B]\nweekly = 30\n")
        self.assertEqual(self.launch_record(["A", "fix it"]), plain)

    def test_help_and_version_forms_stay_unprotected(self):
        self.home.config("[usage_reserve]\nfive_hour = 20\n")
        for args in (["A", "--version"], ["A", "--help"], ["A", "resume", "--help"], ["--reserve-5h", "5", "A", "-V"]):
            with self.subTest(args=args):
                self.home.log.unlink(missing_ok=True)
                result = self.home.run(args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("protection", result.stderr)

    def test_update_and_completion_run_as_plain_codex_with_a_reserve_on(self):
        """They are the same for every login and send no model request, so they stay unprotected."""
        self.home.config("[usage_reserve]\nfive_hour = 20\n")
        for args in (["--reserve-5h", "10", "A", "update"], ["A", "completion", "bash"]):
            with self.subTest(args=args):
                self.home.log.unlink(missing_ok=True)
                result = self.home.run(args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("protection", result.stderr)
                self.assertEqual([event["argv"] for event in self.home.events("other")], [args[-2:] if
                                 args[-1] == "bash" else ["update"]])

    def test_a_protected_launch_without_the_monitor_refuses_before_anything_starts(self):
        kit = self.home.base / "kit"
        shutil.copytree(ROOT / "bin", kit / "bin")
        shutil.copytree(ROOT / "lib", kit / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        (kit / "lib/coding-agent-kit/monitor.py").unlink()
        result = subprocess.run([str(kit / "bin/codex-as"), "--reserve-5h", "10", "A"], cwd=self.home.work,
                                env=self.home.env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("usage reserve protection refused: incomplete install at %s" % kit, result.stderr)
        self.assertEqual(self.home.events(), [])

    def test_enabled_reserves_refuse_every_other_mode_before_anything_starts(self):
        for args, text in ((["--reserve-5h", "10", "A", "exec", "x"], "not codex exec"),
                           (["--reserve-weekly", "10", "A", "resume", "--last"], "not codex resume"),
                           (["--reserve-5h", "10", "A", "--profile", "p"], "does not accept --profile"),
                           (["--reserve-5h", "10", "A", "-c", "sandbox_mode=read-only"], "sets sandbox_mode itself"),
                           (["--reserve-5h", "10", "A", "review"], "not codex review"),
                           (["--reserve-5h", "10", "A", "-m", "gpt-4o"], "gpt-4o")):
            with self.subTest(args=args):
                self.home.log.unlink(missing_ok=True)
                result = self.home.run(args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("usage reserve protection refused: ", result.stderr)
                self.assertIn(text, result.stderr)
                self.assertEqual(self.home.events(), [])

    def test_inherited_reserves_refuse_unreviewed_releases(self):
        """The release comes from `codex --version` run inside the boundary, so a host without one
        refuses for that first."""
        self.home.config("[usage_reserve.codex_accounts.A]\nweekly = 10\n")
        result = self.home.run(["A"], env={"FAKE_VERSION": "codex-cli 0.168.0"})
        self.assertEqual(result.returncode, 1)
        self.assertIn(boundary_missing() or "newer than the newest reviewed release", result.stderr)
        self.assertEqual(self.home.events(), [])

    def test_a_native_or_its_interpreter_in_the_boundarys_reach_refuses_before_anything_runs(self):
        """A session that could replace the native (or a link on the way to it, or the interpreter of a
        #! script native) would have the next plain codex run its code unconfined. The check runs before
        the native's version is read, so nothing of it runs (the mark would show an unconfined run)."""
        missing = boundary_missing()
        mark = self.home.base / "version-mark"
        fake = self.home.bin / "codex"
        tools = self.home.work / "tools"
        tools.mkdir()
        shutil.move(str(fake), tools / "codex")
        path = self.home.env["PATH"]
        python = tools / "fakepython"
        python.symlink_to(sys.executable)
        script = self.home.base / "script-codex"
        script.write_text("#!/usr/bin/env fakepython\n" + (FIXTURES / "fake_codex.py").read_text())
        script.chmod(0o700)
        cases = (("in the workspace", os.pathsep.join((str(tools), path)), "the Codex executable %s" % (tools / "codex")),
                 ("linked from outside", path, "the Codex executable %s" % fake),
                 ("interpreter", os.pathsep.join((str(self.home.bin), str(tools), path)),
                  "the interpreter of the Codex executable %s" % python))
        for name, search, text in cases:
            with self.subTest(name=name):
                for link in (fake,):
                    if os.path.lexists(link):
                        link.unlink()
                if name == "linked from outside":
                    fake.symlink_to(tools / "codex")
                elif name == "interpreter":
                    fake.symlink_to(script)
                result = self.home.run(["--reserve-5h", "10", "A"], env={"PATH": search, "FAKE_VERSION_MARK": str(mark)})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("usage reserve protection refused: " + (missing or "%s lies inside %s, which the boundary "
                              "can write; protected mode refuses a Codex that a protected session could replace"
                              % (text, self.home.work)), result.stderr)
                self.assertFalse(mark.exists())
                self.assertEqual(self.home.events(), [])

    def test_a_path_entry_the_boundary_can_write_before_python3_codex_or_its_interpreter_refuses(self):
        """The reviewer's PATH-shadow shape: a workspace directory searched before the one that supplies
        the launcher's python3, codex or the #!/usr/bin/env interpreter of a script codex would let a
        session plant that command there for the next launch or plain codex to run unconfined; so does
        a relative entry, which a later run from the workspace resolves there. Nothing runs."""
        missing = boundary_missing()
        mark = self.home.base / "version-mark"
        shadow = self.home.work / "bin"
        shadow.mkdir()
        python = os.path.dirname(os.path.realpath(sys.executable))
        tools = self.home.base / "tools"
        tools.mkdir()
        (tools / "fakepython").symlink_to(sys.executable)
        script = self.home.base / "script-codex"
        script.write_text("#!/usr/bin/env fakepython\n" + (FIXTURES / "fake_codex.py").read_text())
        script.chmod(0o700)
        fake, held = self.home.bin / "codex", self.home.base / "fake-codex"
        system = ("/usr/bin", "/bin")
        # dirname and readlink, which bin/codex-as runs too, from an entry before the shadow, so that
        # each case reaches the command it names.
        utilities = self.home.base / "utilities"
        utilities.mkdir()
        for utility in ("dirname", "readlink"):
            found = shutil.which(utility, path=os.pathsep.join(system))
            if found is None:
                self.skipTest("%s is not in /usr/bin or /bin" % utility)
            (utilities / utility).symlink_to(found)
        cases = (("python3", (shadow, self.home.bin, python, *system), "the PATH entry %s comes before %s, which "
                  "supplies python3, and lies inside %s" % (shadow, python, self.home.work)),
                 ("codex", (python, utilities, shadow, self.home.bin, *system), "the PATH entry %s comes before %s, "
                  "which supplies codex, and lies inside %s" % (shadow, self.home.bin, self.home.work)),
                 ("interpreter", (python, utilities, self.home.bin, shadow, tools, *system), "the PATH entry %s comes "
                  "before %s, "
                  "which supplies fakepython (the interpreter of the Codex executable), and lies inside %s"
                  % (shadow, tools, self.home.work)),
                 ("relative", ("", self.home.bin, python, *system), "the empty PATH entry (the current directory) "
                  "comes before %s, which supplies python3; a later run from the workspace would search the "
                  "workspace there" % python))
        for name, entries, text in cases:
            with self.subTest(name=name):
                if name == "interpreter":
                    fake.rename(held)
                    fake.symlink_to(script)
                search = os.pathsep.join(str(entry) for entry in entries)
                result = self.home.run(["--reserve-5h", "10", "A"], env={"PATH": search, "FAKE_VERSION_MARK": str(mark)})
                if name == "interpreter":
                    fake.unlink()
                    held.rename(fake)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("usage reserve protection refused: " + (missing or text), result.stderr)
                self.assertFalse(mark.exists())
                self.assertEqual(self.home.events(), [])
                self.assertEqual(list(shadow.iterdir()), [])

    def test_a_path_entry_the_boundary_can_write_before_dirname_or_readlink_or_supplying_python3_refuses(self):
        """bin/codex-as runs dirname, readlink and python3 by name before any check, as bin/claude-as
        does, so an entry the boundary can write that is searched before the one supplying dirname or
        readlink refuses too, and so does such an entry that supplies python3: a python3 wrapper script
        there leaves the launcher's Python naming what the script execs, which lies outside. Nothing
        runs."""
        missing = boundary_missing()
        mark = self.home.base / "version-mark"
        shadow = self.home.work / "bin"
        shadow.mkdir()
        python = os.path.realpath(sys.executable)
        tools = self.home.base / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(python)
        system = ("/usr/bin", "/bin")
        found = {name: shutil.which(name, path=os.pathsep.join(system)) for name in ("dirname", "readlink")}
        if None in found.values():
            self.skipTest("dirname and readlink are not in /usr/bin or /bin")
        cases = (("dirname", (self.home.bin, tools, shadow, *system), "the PATH entry %s comes before %s, which "
                  "supplies dirname, and lies inside %s" % (shadow, os.path.dirname(found["dirname"]), self.home.work)),
                 ("readlink", (self.home.bin, tools, shadow, *system), "the PATH entry %s comes before %s, which "
                  "supplies readlink, and lies inside %s" % (shadow, os.path.dirname(found["readlink"]),
                                                             self.home.work)),
                 ("wrapper", (self.home.bin, shadow, *system), "the PATH entry %s, which supplies python3, lies "
                  "inside %s, which the boundary can write: a protected session could replace that python3, which "
                  "the next run outside the boundary would execute unconfined; supply python3 from a PATH entry "
                  "outside every path the boundary can write" % (shadow, self.home.work)))
        for name, entries, text in cases:
            with self.subTest(name=name):
                if name == "readlink":
                    (tools / "dirname").symlink_to(found["dirname"])
                elif name == "wrapper":
                    wrapper = shadow / "python3"
                    wrapper.write_text('#!/bin/sh\nexec %s "$@"\n' % shlex.quote(python))
                    wrapper.chmod(0o755)
                search = os.pathsep.join(str(entry) for entry in entries)
                result = self.home.run(["--reserve-5h", "10", "A"], env={"PATH": search, "FAKE_VERSION_MARK": str(mark)})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("usage reserve protection refused: " + (missing or text), result.stderr)
                self.assertFalse(mark.exists())
                self.assertEqual(self.home.events(), [])

    def test_a_nested_or_relative_interpreter_of_the_native_refuses_before_anything_runs(self):
        """An interpreter that is a #! script itself runs its own interpreter (Linux nests four levels),
        so one in the boundary's reach refuses at any level, as claude-as follows them; a relative #!
        interpreter, which the kernel looks up from the working directory, refuses too. Nothing runs."""
        missing = boundary_missing()
        mark = self.home.base / "version-mark"
        fake = self.home.bin / "codex"
        tools = self.home.work / "tools"
        tools.mkdir()
        (tools / "fakepython").symlink_to(sys.executable)
        outer = self.home.base / "outer"
        outer.write_text("#!%s\n" % (tools / "fakepython"))
        outer.chmod(0o700)
        source = (FIXTURES / "fake_codex.py").read_text()
        cases = (("nested", "#!%s\n" % outer, "the interpreter of the Codex executable %s lies inside %s, which the "
                  "boundary can write; protected mode refuses a Codex that a protected session could replace"
                  % (tools / "fakepython", self.home.work)),
                 ("relative", "#!fakepython\n", "the #! line of the Codex executable names an interpreter that is not "
                  "an absolute path, which the kernel looks up from the working directory; protected mode does not "
                  "follow it"))
        for name, line, text in cases:
            with self.subTest(name=name):
                fake.write_text(line + source)
                result = self.home.run(["--reserve-5h", "10", "A"], env={"FAKE_VERSION_MARK": str(mark)})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("usage reserve protection refused: " + (missing or text), result.stderr)
                self.assertFalse(mark.exists())
                self.assertEqual(self.home.events(), [])

    def test_shared_state_replaced_by_a_link_refuses_before_anything_runs(self):
        """A shared entry beneath codex_home that became a link (a protected session on macOS can do
        that; planted from outside here, so it runs on every platform) would put the link's target in
        the next launch's writable set."""
        missing = boundary_missing()
        root = self.home.codex_home
        ssh, rc = self.home.home / ".ssh", self.home.home / ".zshrc"
        ssh.mkdir()
        rc.write_text("# rc\n")
        for name, target in (("sessions", ssh), ("history.jsonl", rc), (".tmp", ssh)):
            with self.subTest(name=name):
                (root / name).symlink_to(target)
                result = self.home.run(["--reserve-5h", "10", "A"])
                (root / name).unlink()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("usage reserve protection refused: " + (missing or "%s is a link" % (root / name)),
                              result.stderr)
                self.assertEqual(rc.read_text(), "# rc\n")
                self.assertEqual(list(ssh.iterdir()), [])

    def test_a_version_check_whose_descendant_keeps_its_output_open_still_ends(self):
        """`codex --version` runs confined; a descendant that keeps its output open is ended with the
        boundary's other processes before the output is read, so the read never waits for it."""
        missing = boundary_missing()
        started = time.monotonic()
        result = self.home.run(["--reserve-5h", "10", "A"], timeout=45,
                               env={"FAKE_VERSION_LINGER": str(self.home.base)})
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(missing or "needs an interactive terminal", result.stderr)  # the version passed
        self.assertLess(time.monotonic() - started, protect.VERSION_TIMEOUT)
        self.assertEqual(leftovers(self.home.base), [])

    def test_protected_mode_never_runs_the_native_login(self):
        """codex login needs more network than the boundary allows, so an account without a saved
        login refuses instead of logging in outside the boundary."""
        missing = boundary_missing()
        (self.home.secrets / "codex-A.json").unlink()
        terminal = Terminal([str(LAUNCHER), "--reserve-5h", "10", "A"], self.home.env, self.home.work)
        self.addCleanup(terminal.close)
        terminal.expect("usage reserve protection refused: " + re.escape(
            missing or "the account A has no saved login yet, and protected mode never runs the native login"))
        self.assertEqual(os.waitstatus_to_exitcode(terminal.wait()), 1)
        self.assertEqual(self.home.events(), [])
        self.assertFalse((self.home.secrets / "codex-A.json").exists())

    def test_databases_native_has_not_created_yet_refuse(self):
        """The boundary names native's databases as files, which native can write but not create."""
        missing = boundary_missing()
        for name, advice in (("thread_history_1", "send one prompt in a Codex session without reserves first"),
                             ("state_5", "start Codex once without reserves first")):
            with self.subTest(name=name):
                for database in (*NATIVE.databases, *NATIVE.later_databases):
                    (self.home.codex_home / (database + ".sqlite")).touch()
                (self.home.codex_home / (name + ".sqlite")).unlink()
                terminal = Terminal([str(LAUNCHER), "--reserve-5h", "10", "A"], self.home.env, self.home.work)
                self.addCleanup(terminal.close)
                terminal.expect("usage reserve protection refused: " + re.escape(
                    missing or "native Codex has not created %s.sqlite in %s yet" % (name, self.home.codex_home)))
                if not missing:  # without a boundary the refusal above is the only line
                    terminal.expect(re.escape(advice))
                self.assertEqual(os.waitstatus_to_exitcode(terminal.wait()), 1)
                self.assertEqual(self.home.events(), [])

    def test_a_configuration_that_follows_system_proxy_settings_refuses(self):
        """With respect_system_proxy native would take system, PAC and WPAD settings first, which the
        gate cannot follow; inside the boundary protected mode turns the feature off."""
        missing = boundary_missing()
        config = self.home.codex_home / "config.toml"
        for key in protect.SYSTEM_PROXY_FEATURES:
            with self.subTest(key=key):
                config.write_text('model = "gpt-5.5"\n[features]\n%s = true\n' % key)
                terminal = Terminal([str(LAUNCHER), "--reserve-5h", "10", "A"], self.home.env, self.home.work)
                self.addCleanup(terminal.close)
                terminal.expect("usage reserve protection refused: " + re.escape(
                    missing or "%s enables features.respect_system_proxy" % config))
                self.assertEqual(os.waitstatus_to_exitcode(terminal.wait()), 1)
                self.assertEqual(self.home.events(), [])
        self.assertIn("features.respect_system_proxy=false", protect.overrides(50000))

    def test_flag_errors_name_the_problem(self):
        for args, text in ((["--reserve-5h", "A"], "takes a number from 0 through 100"),
                           (["--reserve-5h", "10"], "takes a number" if False else "an account name must follow"),
                           (["--reserve-5h=10", "A"], "is not accepted"),
                           (["--reserve-5h", "10", "--reserve-5h", "5", "A"], "is given twice"),
                           (["--reserve-weekly", "101", "A"], "takes a number"),
                           (["--reserve-5h", "10", "--list"], "an account name must follow")):
            with self.subTest(args=args):
                result = self.home.run(args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(text, result.stderr)
                self.assertEqual(self.home.events(), [])

    def test_a_configured_model_outside_the_reviewed_table_refuses_also_through_a_link(self):
        missing = boundary_missing()
        dotfile, config = self.home.base / "dotfiles-config.toml", self.home.codex_home / "config.toml"
        dotfile.write_text('model = "gpt-4o"\n')
        config.unlink()
        config.symlink_to(dotfile)  # a dotfile manager's link is followed, as native follows it
        terminal = Terminal([str(LAUNCHER), "--reserve-5h", "10", "A"], self.home.env, self.home.work)
        self.addCleanup(terminal.close)
        terminal.expect("usage reserve protection refused: " + re.escape(
            missing or "the model gpt-4o that %s selects is not one" % config))
        self.assertEqual(os.waitstatus_to_exitcode(terminal.wait()), 1)
        self.assertEqual(self.home.events(), [])

    def test_a_selected_profile_that_sets_what_protected_mode_sets_refuses(self):
        missing = boundary_missing()
        config = self.home.codex_home / "config.toml"
        config.write_text('model = "gpt-5.5"\nprofile = "p"\n[profiles.p]\nmodel = "gpt-5.5"\n'
                          'sandbox_mode = "workspace-write"\nmodel_provider = "openai"\n')
        terminal = Terminal([str(LAUNCHER), "--reserve-5h", "10", "A"], self.home.env, self.home.work)
        self.addCleanup(terminal.close)
        terminal.expect("usage reserve protection refused: " + re.escape(
            missing or "the profile p that %s selects sets model_provider, sandbox_mode, which protected mode sets "
            "itself" % config))
        self.assertEqual(os.waitstatus_to_exitcode(terminal.wait()), 1)
        self.assertEqual(self.home.events(), [])

    def test_without_a_terminal_protected_mode_refuses(self):
        missing = boundary_missing()
        result = self.home.run(["--reserve-5h", "10", "A"])
        self.assertEqual(result.returncode, 1)
        self.assertIn(missing or "needs an interactive terminal", result.stderr)
        self.assertEqual(self.home.events(), [])


class Launch(unittest.TestCase):
    """Fixtures of a whole protected launch on a terminal, against the loopback backend."""

    real_codex = None

    def setUp(self):
        self.home = Home(self, self.real_codex)
        (self.home.base / "ca").mkdir()
        made = backend.make_ca(self.home.base / "ca")
        if made is None:
            self.skipTest("openssl could not make the fixture CA")
        self.ca, context = made
        self.backend = backend.Backend(context)
        self.addCleanup(self.backend.close)
        self.proxy = backend.Proxy(self.backend.port)
        self.addCleanup(self.proxy.close)
        self.home.env.update(HTTPS_PROXY="http://127.0.0.1:%d" % self.proxy.port, SSL_CERT_FILE=self.ca)
        self.terminal = None
        self.addCleanup(self.check_leftovers)

    def check_leftovers(self):
        if self.terminal is not None:
            self.terminal.close()
        deadline = time.monotonic() + 5
        while leftovers(self.home.base) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(leftovers(self.home.base), [])

    def start(self, args=("--reserve-5h", "10", "--reserve-weekly", "10", "A"), env=None):
        self.terminal = Terminal([str(LAUNCHER), *args], dict(self.home.env, **(env or {})), self.home.work)
        return self.terminal

    def exit_code(self):
        status = self.terminal.wait()
        return os.waitstatus_to_exitcode(status)

    def models(self):
        return self.backend.seen("/backend-api/codex/responses")


@unittest.skipIf(boundary_missing(), "protected mode needs the OS boundary: %s" % boundary_missing())
@unittest.skipIf(shutil.which("openssl") is None, "the fixture CA needs openssl")
class Protected(Launch):
    """Whole protected launches with the fake codex."""

    def test_a_model_free_start_then_turns_and_the_native_exit_status(self):
        terminal = self.start(env={"FAKE_EXIT": "7"})
        terminal.expect("usage reserves for A: five-hour 10% \\(--reserve-5h\\), weekly 10% \\(--reserve-weekly\\)")
        terminal.expect("READY")
        self.assertEqual(self.models(), [])
        refresh = self.backend.seen("/oauth/token")
        self.assertEqual(len(refresh), 1)
        reads = self.backend.seen("/backend-api/wham/usage")
        self.assertTrue(reads and reads[0]["at"] > refresh[0]["at"])
        self.assertTrue(all(dict((k.lower(), v) for k, v in r["headers"])["user-agent"] in ("fake-codex/0.167.2", kit.USER_AGENT)
                            for r in reads))
        terminal.send("turn\r")
        terminal.expect("TURN 200")
        self.assertEqual(len(self.models()), 1)
        terminal.send("net %d\r" % self.backend.port)
        terminal.expect("NET (13|1)\\b")  # EACCES (Linux) or EPERM (macOS): only the gate is reachable
        terminal.send("write %s\r" % (self.home.home / "outside"))
        terminal.expect("WRITE (13|1)\\b")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 7)
        (tui,) = self.home.events("tui")
        self.assertEqual(tui["argv"].count("--no-daemon"), 1)
        self.assertEqual(tui["env_proxy"], [])
        self.assertNotEqual(tui["tmpdir"], str(self.home.tmp))
        self.assertFalse(Path(tui["tmpdir"]).exists())
        initialize = self.home.events("initialize")
        self.assertTrue(initialize and all(event["client"] == {"name": "quota-probe", "version": "1"}
                                           for event in initialize))
        # Native refreshed through the account link; the link and the secret stay as codex-as keeps them.
        link = self.home.codex_home / "accounts/A/auth.json"
        self.assertEqual(os.readlink(link), str(self.home.secrets / "codex-A.json"))
        info = (self.home.secrets / "codex-A.json").stat()
        self.assertEqual((info.st_mode & 0o777, info.st_nlink), (0o600, 1))
        self.assertNotEqual(self.home.secret()["last_refresh"], "2026-10-01T00:00:00Z")
        self.assertNotIn(b"Codex replaced", terminal.output)

    def test_a_threshold_pauses_the_tui_and_a_fresh_check_continues_it(self):
        terminal = self.start()
        terminal.expect("READY")
        self.backend.used = [95, 20]
        terminal.send("usage\r")  # native's own read is an observation too
        terminal.expect("quota reserve paused \\(threshold\\); account A; window five-hour; remaining 5% \\(reserve 10%\\)")
        terminal.expect("press F12")
        terminal.send("turn\r")
        terminal.expect("TURN 503")
        self.assertEqual(self.models(), [])
        terminal.send(F12)
        code = terminal.expect("type ([0-9a-f]{8}) then Enter").group(1).decode()
        terminal.send(code + "\r")
        terminal.expect("still paused")
        self.backend.used = [10, 20]
        terminal.send(F12)
        code = terminal.expect("type ([0-9a-f]{8}) then Enter").group(1).decode()
        terminal.send(code + "\r")
        terminal.expect("fresh quota check passed")
        terminal.send("turn\r")
        terminal.expect("TURN 200")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)

    def test_monitor_loss_latches_until_a_restarted_gate_continues(self):
        terminal = self.start()
        terminal.expect("READY")
        monitors = [pid for pid in leftovers(self.home.base) if b"monitor.py" in Path("/proc", str(pid), "cmdline").read_bytes()] \
            if os.path.isdir("/proc") else []
        if not monitors:
            out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "command="], capture_output=True, text=True).stdout
            monitors = [int(line.split(None, 1)[0]) for line in out.splitlines() if "monitor.py" in line
                        and "lib/coding-agent-kit" in line and str(ROOT) in line]
        self.assertTrue(monitors)
        for pid in monitors:
            os.kill(pid, signal.SIGKILL)
        terminal.expect("quota reserve paused \\(monitor_")
        terminal.send("turn\r")
        terminal.expect("TURN 503")
        self.assertEqual(self.models(), [])
        terminal.expect("gate restarted paused", timeout=30)
        terminal.send(F12)
        code = terminal.expect("type ([0-9a-f]{8}) then Enter").group(1).decode()
        terminal.send(code + "\r")
        terminal.expect("fresh quota check passed", timeout=40)
        terminal.send("turn\r")
        terminal.expect("TURN 200")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)

    def test_a_requirement_pause_survives_a_gate_restart_until_a_later_bundle_lifts_it(self):
        """U2 (a) end to end: the gate dies while a mid-run bundle's requirement pauses the run; the
        restarted gate keeps that pause through F12 and a passing read, and a later bundle lifts it."""
        self.home.write_credential(backend.credential(plan="edu_pro"))
        self.backend.plan = "edu_pro"
        allowed = bundle('allowed_sandbox_modes = ["read-only", "danger-full-access"]\n')
        self.backend.bundle = allowed
        self.home.knobs(FAKE_BUNDLE="1")
        terminal = self.start()
        terminal.expect("READY")
        self.backend.bundle = bundle('allowed_sandbox_modes = ["read-only"]\n')
        terminal.send("bundle\r")
        terminal.expect("quota reserve paused \\(requirements_sandbox_mode_not_allowed\\)")
        # Native now holds the bundle in its one-hour cache: its readers fetch no other one meanwhile.
        self.home.knobs()
        gates = descendants(terminal.pid, "lib/codex-as/gate.py")
        self.assertEqual(len(gates), 1, gates)
        os.kill(gates[0], signal.SIGKILL)
        terminal.expect("gate restarted paused", timeout=30)
        terminal.send(F12)
        code = terminal.expect("type ([0-9a-f]{8}) then Enter").group(1).decode()
        terminal.send(code + "\r")
        terminal.expect("still paused: requirements_sandbox_mode_not_allowed", timeout=40)
        terminal.send("turn\r")
        terminal.expect("TURN 503")
        self.assertEqual(self.models(), [])
        self.backend.bundle = allowed
        terminal.send("bundle\r")
        terminal.expect("BUNDLE 200")
        terminal.send(F12)
        code = terminal.expect("type ([0-9a-f]{8}) then Enter").group(1).decode()
        terminal.send(code + "\r")
        terminal.expect("fresh quota check passed", timeout=40)
        terminal.send("turn\r")
        terminal.expect("TURN 200")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)

    def test_startup_refusals_send_no_model_request_and_start_no_tui(self):
        cases = (
            ("threshold", lambda: setattr(self.backend, "used", [95, 20]), "five-hour window 5% remaining"),
            ("identity", lambda: setattr(self.backend, "user", "user-other"), "account changed"),
            ("refresh", lambda: setattr(self.backend, "refresh_status", 400), "pre-launch credential refresh failed"),
            ("usage", lambda: setattr(self.backend, "usage_status", 503), "no quota reading yet"),
            ("policy", lambda: self.home.knobs(FAKE_POLICY_ERROR="1"), "(policy-unavailable)"),
        )
        for name, arrange, text in cases:
            with self.subTest(name=name):
                self.backend.used, self.backend.user = [10, 20], "user-fixture-A"
                self.backend.refresh_status = self.backend.usage_status = 200
                self.home.log.unlink(missing_ok=True)
                self.home.knobs()
                arrange()
                terminal = self.start()
                terminal.expect("usage reserve protection refused: .*" + re.escape(text), timeout=90)
                self.assertEqual(self.exit_code(), 1)
                self.assertEqual(self.home.events("tui"), [])
                self.assertEqual(self.models(), [])
                terminal.close()

    def test_workspace_plans_forward_the_config_bundle_and_refuse_blocking_requirements(self):
        self.home.write_credential(backend.credential(plan="edu_pro"))
        self.backend.plan = "edu_pro"
        self.backend.bundle = bundle('allowed_sandbox_modes = ["read-only", "danger-full-access"]\n')
        self.home.knobs(FAKE_BUNDLE="1")
        terminal = self.start()
        terminal.expect("READY")
        self.assertTrue(self.backend.seen("/backend-api/wham/config/bundle"))
        self.assertTrue(all(event["status"] == 200 for event in self.home.events("bundle")))
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        self.terminal.close()
        self.home.log.unlink()
        self.backend.bundle = bundle("[application.network]\n")
        terminal = self.start()
        terminal.expect("usage reserve protection refused: .*application network restrictions")
        self.assertEqual(self.exit_code(), 1)
        self.assertEqual(self.home.events("tui"), [])

    def test_a_standalone_codex_under_codex_home_starts_and_no_session_can_replace_it(self):
        """The reviewer's layout: Codex's standalone installer puts the native under
        <codex_home>/packages/standalone with a current link, and PATH links to it. The writable set
        leaves packages/ and the rest of codex_home read-only, and the native's version runs inside a
        boundary (an unconfined run could write the mark in HOME)."""
        release = self.home.codex_home / "packages/standalone/releases/0.167.2-x86_64-unknown-linux-musl"
        (release / "bin").mkdir(parents=True)
        native = release / "bin/codex"
        shutil.move(str(self.home.bin / "codex"), native)
        (release.parent.parent / "current").symlink_to(release)
        (self.home.bin / "codex").symlink_to(release.parent.parent / "current/bin/codex")
        before = native.read_bytes()
        mark = self.home.home / "version-mark"
        root = self.home.codex_home
        (root / ".tmp").mkdir(mode=0o700)
        (root / ".tmp/plugins").mkdir()  # plain codex's plugin checkout, which its later starts sync from
        terminal = self.start(env={"FAKE_VERSION_MARK": str(mark)})
        terminal.expect("READY")
        denied = (native, release.parent.parent / "current-new", root / "config.toml", root / "hooks.json",
                  root / "mcp.json", root / "skills/planted", root / "rules/planted", root / "agents/planted",
                  root / "accounts/planted", root / "new.sqlite", root / "auth.json", root / ".tmp/plugins/x",
                  root / ".tmp/plugins.sha", root / "memories/planted", root / "memories_v2/planted")
        for path in denied:
            terminal.send("write %s\r" % path)
            terminal.expect("WRITE (13|1)\\b")
        allowed = (root / "sessions/written", root / "accounts/A/written", root / "history.jsonl",
                   root / "session_index.jsonl", root / ".tmp/rollout-maintenance.lock", root / "accounts/A/.tmp/x",
                   root / "state_5.sqlite-wal", root / "archived_sessions/written", root / "thread-writer-locks/written")
        for path in allowed:
            terminal.send("write %s\r" % path)
            terminal.expect("WRITE ok")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        self.assertEqual(native.read_bytes(), before)
        self.assertFalse(mark.exists(), mark.read_text() if mark.exists() else "")
        self.assertFalse(os.path.lexists(release.parent.parent / "current-new"))
        self.assertEqual(list((root / ".tmp/plugins").iterdir()), [])
        for name in NATIVE.databases:
            database = root / (name + ".sqlite")
            for suffix in ("-wal", "-shm"):  # made by the launcher, as SQLite makes them
                self.assertEqual(stat.S_IMODE(Path(str(database) + suffix).stat().st_mode),
                                 stat.S_IMODE(database.stat().st_mode))

    def test_a_signal_to_the_launcher_reaches_the_native_and_its_status_returns(self):
        terminal = self.start()
        terminal.expect("READY")
        os.kill(terminal.pid, signal.SIGTERM)
        status = terminal.wait()
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGTERM, status)

    def test_detached_descendants_are_swept_at_exit(self):
        terminal = self.start()
        terminal.expect("READY")
        terminal.send("spawn\r")
        child = int(terminal.expect("SPAWN ([0-9]+)").group(1))
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("a detached descendant outlived the invocation")

    def test_the_gate_reaches_upstream_through_all_proxy_as_codex_would(self):
        """With no HTTPS_PROXY, Codex uses ALL_PROXY for HTTPS and ignores HTTP_PROXY, so the gate
        does too (an unusable HTTP_PROXY would refuse under any other order); the native and its
        readers never see the caller's proxy."""
        del self.home.env["HTTPS_PROXY"]
        terminal = self.start(env={"ALL_PROXY": "http://127.0.0.1:%d" % self.proxy.port,
                                   "HTTP_PROXY": "socks5h://127.0.0.1:1"})
        terminal.expect("READY")
        terminal.send("turn\r")
        terminal.expect("TURN 200")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        self.assertIn("auth.openai.com:443", self.proxy.targets)  # the pre-launch refresh
        self.assertIn("chatgpt.com:443", self.proxy.targets)  # quota reads and the model request
        (tui,) = self.home.events("tui")
        self.assertEqual(tui["env_proxy"], [])

    def test_the_native_and_its_readers_keep_the_callers_umask(self):
        """The launcher runs under 077, but what the native and every confined command (the quota
        readers the monitor starts included) create follows the caller's umask, as unprotected."""
        old = os.umask(0o027)
        try:
            terminal = self.start()
        finally:
            os.umask(old)
        terminal.expect("READY")
        monitors = [line for line in command_lines(self.home.base).values() if "monitor.py" in line]
        self.assertEqual(len(monitors), 1, monitors)
        self.assertIn(" --reader-umask 027", monitors[0])
        target = self.home.work / "made-in-session.txt"
        terminal.send("write %s\r" % target)
        terminal.expect("WRITE ok")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        info = (self.home.secrets / "codex-A.json").stat()  # the refreshed credential stays private
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)

    def test_a_tmpdir_inside_the_workspace_refuses_before_anything_starts(self):
        inside = self.home.work / "tmp"
        inside.mkdir()
        terminal = self.start(env={"TMPDIR": str(inside)})
        terminal.expect("usage reserve protection refused: the temporary directory %s lies inside %s, which the "
                        "boundary can write; set TMPDIR" % (re.escape(str(inside)), re.escape(str(self.home.work))))
        self.assertEqual(self.exit_code(), 1)
        self.assertEqual(self.home.events(), [])
        self.assertEqual(self.backend.seen(), [])
        self.assertEqual(list(inside.iterdir()), [])

    def test_reader_specifications_are_out_of_reach_of_the_boundary(self):
        terminal = self.start()
        terminal.expect("READY")
        (private,) = self.home.tmp.glob("codex-as-protect-*")
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
        spec = private / "poll.json"
        self.assertEqual(set(json.loads(spec.read_text())), {"command", "env", "cwd", "mode", "timeout"})
        for path in (spec, private / "new.json"):
            terminal.send("write %s\r" % path)
            terminal.expect("WRITE (13|1)\\b")
        if os.path.isdir("/proc"):  # the monitor's reader takes its boundary from its argv
            monitors = [Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0") for pid in leftovers(self.home.base)]
            monitors = [argv for argv in monitors if any(word.endswith(b"monitor.py") for word in argv)]
            self.assertTrue(monitors)
            argv = json.loads(monitors[0][monitors[0].index(b"--reader-argv") + 1])
            limits = json.loads(argv[argv.index("--confine") + 1])
            self.assertEqual(set(limits), {"port", "writable", "profile"})
            self.assertIn(str(self.home.work), limits["writable"])
            self.assertEqual(argv[-1], str(spec))
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        self.assertFalse(private.exists())

    def venv(self, path, mark):
        """A virtual environment at PATH whose site-packages hold a .pth file that appends to MARK."""
        made = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(path)], capture_output=True,
                              timeout=120)
        if made.returncode:
            self.skipTest("python3 -m venv is unavailable")
        (site,) = path.glob("lib/python*/site-packages")
        (site / "zz-planted.pth").write_text("import os; open(%r, 'a').write('pth %%d\\n' %% os.getpid())\n" % str(mark))
        subprocess.run([str(path / "bin/python3"), "-I", "-B", "-c", "pass"], check=True, timeout=30)
        self.assertTrue(mark.exists())  # the environment's own python3 -I -B runs it
        mark.unlink()
        return os.pathsep.join((str(path / "bin"), self.home.env["PATH"]))

    def test_a_workspace_venv_first_on_path_refuses_before_anything_starts(self):
        """A python3 the boundary can write could take over the next launch outside every boundary, before
        any check, so a launch through one refuses; meanwhile -S keeps its .pth files out of the launcher."""
        venv = self.home.work / ".venv"
        mark = self.home.base / "planted.log"  # outside every writable path: only an unconfined process can write it
        terminal = self.start(env={"PATH": self.venv(venv, mark)})
        terminal.expect("usage reserve protection refused: the launcher's Python %s lies inside %s, which the boundary "
                        "can write; start codex-as with a python3 outside the workspace and the Codex homes first on "
                        "PATH" % (re.escape(str(venv / "bin/python3")), re.escape(str(self.home.work))))
        self.assertEqual(self.exit_code(), 1)
        self.assertEqual(self.home.events(), [])
        self.assertEqual(self.backend.seen(), [])
        self.assertFalse(mark.exists(), mark.read_text() if mark.exists() else "")

    def test_a_venv_first_on_path_never_runs_planted_code_outside_the_boundary(self):
        """With a virtual environment outside the boundary's reach first on PATH, neither the launcher
        (python3 -S) nor the gate, monitor and readers (the base interpreter) process its .pth files."""
        venv = self.home.base / "venv"
        mark = self.home.base / "planted.log"
        path = self.venv(venv, mark)
        ps = venv / "bin/ps"  # what the launcher would run by name on macOS
        ps.write_text("#!/bin/sh\necho ps >> '%s'\nexec /bin/ps \"$@\"\n" % mark)
        ps.chmod(0o755)
        terminal = self.start(env={"PATH": path})
        terminal.expect("READY")
        self.assertFalse(mark.exists(), mark.read_text() if mark.exists() else "")
        # A new gate and monitor, then a continuation's reader (and on macOS the supervisor's ps).
        for pid in self.kit_processes("monitor.py"):
            os.kill(pid, signal.SIGKILL)
        terminal.expect("gate restarted paused", timeout=30)
        terminal.send(F12)
        code = terminal.expect("type ([0-9a-f]{8}) then Enter").group(1).decode()
        terminal.send(code + "\r")
        terminal.expect("fresh quota check passed", timeout=40)
        if os.path.isdir("/proc"):
            for pid in self.kit_processes("gate.py") + self.kit_processes("monitor.py"):
                argv = Path("/proc", str(pid), "cmdline").read_bytes().decode().split("\0")
                self.assertEqual(argv[0], protect.interpreter())
                if "--reader-argv" in argv:
                    self.assertEqual(json.loads(argv[argv.index("--reader-argv") + 1])[0], protect.interpreter())
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        self.assertFalse(mark.exists(), mark.read_text() if mark.exists() else "")

    def kit_processes(self, script):
        """Pids of this launch's kit processes that run SCRIPT."""
        if os.path.isdir("/proc"):
            return [pid for pid in leftovers(self.home.base) if any(
                word.endswith(script.encode()) for word in Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0"))]
        out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "command="], capture_output=True, text=True).stdout
        return [int(line.split(None, 1)[0]) for line in out.splitlines() if script in line and str(ROOT) in line]

    def test_a_kit_inside_the_workspace_refuses_before_anything_starts(self):
        kit = self.home.work / "kit"
        shutil.copytree(ROOT / "bin", kit / "bin")
        shutil.copytree(ROOT / "lib", kit / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        self.terminal = Terminal([str(kit / "bin/codex-as"), "--reserve-5h", "10", "A"], self.home.env, self.home.work)
        self.terminal.expect("usage reserve protection refused: the kit directory %s lies inside %s, which the boundary "
                             "can write; run codex-as with a Python and a kit installed outside the workspace"
                             % (re.escape(str(kit / "lib/codex-as")), re.escape(str(self.home.work))))
        self.assertEqual(self.exit_code(), 1)
        self.assertEqual(self.home.events(), [])
        self.assertEqual(self.backend.seen(), [])

    def test_a_relative_cd_resolves_once_against_the_launch_directory(self):
        (self.home.work / "sub").mkdir()
        result = self.home.run(["A", "-C", "sub"])
        self.assertEqual(result.returncode, 0, result.stderr)
        (plain,) = self.home.events("tui")
        self.home.log.unlink()
        terminal = self.start(args=("--reserve-5h", "10", "A", "-C", "sub"))
        terminal.expect("READY")
        terminal.send("write %s\r" % (self.home.work / "sub/file"))  # the -C directory is the workspace
        terminal.expect("WRITE ok")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        (record,) = self.home.events("tui")
        self.assertEqual((record["cwd"], record["argv"][-2:]), (plain["cwd"], plain["argv"][-2:]))
        self.assertEqual((record["cwd"], record["argv"][-2:]), (str(self.home.work), ["-C", "sub"]))

    def test_a_model_override_replaces_the_configured_model(self):
        (self.home.codex_home / "config.toml").write_text('model = "gpt-4o"\n')
        terminal = self.start(args=("--reserve-5h", "10", "A", "-c", 'model="gpt-5.5"'))
        terminal.expect("READY")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)

    def test_the_repair_restores_a_link_the_native_replaced(self):
        terminal = self.start()
        terminal.expect("READY")
        terminal.send("replace\r")
        terminal.expect("REPLACED")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        terminal.expect("Codex replaced the account auth.json link with a regular file; credentials were imported")
        link = self.home.codex_home / "accounts/A/auth.json"
        self.assertEqual(os.readlink(link), str(self.home.secrets / "codex-A.json"))
        self.assertEqual(self.home.secret()["tokens"]["refresh_token"], "fixture-refresh-replaced")

    def test_a_refusal_after_the_refresh_still_repairs_the_link(self):
        self.home.knobs(FAKE_REPLACE_LINK="1")
        terminal = self.start()
        self.assertEqual(self.exit_code(), 1)
        output = terminal.output.decode("utf-8", "replace")
        self.assertIn("credentials were imported into secrets and the link restored", output)
        self.assertIn("usage reserve protection refused: the pre-launch credential refresh failed", output)
        self.assertEqual(self.home.events("tui"), [])
        link = self.home.codex_home / "accounts/A/auth.json"
        self.assertEqual(os.readlink(link), str(self.home.secrets / "codex-A.json"))
        self.assertNotEqual(self.home.secret()["last_refresh"], "2026-10-01T00:00:00Z")

    def test_an_unprotected_client_of_the_account_runs_beside_a_protected_one(self):
        terminal = self.start()
        terminal.expect("READY")
        result = self.home.run(["A", "hello"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.home.events("tui")), 2)
        terminal.send("turn\r")
        terminal.expect("TURN 200")
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)
        self.assertNotIn(b"Codex replaced", terminal.output)
        link = self.home.codex_home / "accounts/A/auth.json"
        self.assertEqual(os.readlink(link), str(self.home.secrets / "codex-A.json"))
        info = (self.home.secrets / "codex-A.json").stat()
        self.assertEqual((info.st_mode & 0o777, info.st_nlink), (0o600, 1))

    def test_the_refresh_waits_for_another_launch_of_the_account(self):
        locks = self.home.codex_home / "accounts/.locks"
        locks.mkdir(parents=True, mode=0o700)
        (self.home.codex_home / "accounts").chmod(0o700)
        fd = os.open(locks / ".refresh-A.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        terminal = self.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            terminal.read(0.1)
        self.assertEqual(self.backend.seen("/oauth/token"), [])
        self.assertEqual(self.home.events("refresh"), [])
        released = time.monotonic()
        fcntl.flock(fd, fcntl.LOCK_UN)
        terminal.expect("READY")
        (refresh,) = self.backend.seen("/oauth/token")
        self.assertGreater(refresh["at"], released)
        terminal.send("quit\r")
        self.assertEqual(self.exit_code(), 0)


REAL_CODEX = shutil.which("codex")


def real_version():
    """What the host's codex reports, run in a HOME of its own that is removed afterwards: codex
    writes CODEX_HOME/tmp (arg0-macos on macOS) even for --version, so the temporary directory itself
    would keep a .codex."""
    if not REAL_CODEX:
        return None
    with tempfile.TemporaryDirectory(prefix="quota-x-version-", ignore_cleanup_errors=True) as home:
        try:
            return subprocess.run([REAL_CODEX, "--version"], capture_output=True, text=True, timeout=20,
                                  env={"PATH": "/usr/bin:/bin", "HOME": home}).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return None


@unittest.skipIf(boundary_missing(), "protected mode needs the OS boundary")
@unittest.skipIf(shutil.which("openssl") is None, "the fixture CA needs openssl")
@unittest.skipUnless(real_version() == "codex-cli 0.167.2", "needs the reviewed codex 0.167.2 on PATH")
class RealCodex(Launch):
    """The reviewed native against loopback fixtures: a model-free protected start, and the in-TUI
    /new, /resume and /fork commands (G0 D11) staying behind the gate, the account and the pause."""

    real_codex = REAL_CODEX

    def setUp(self):
        super().setUp()
        self.home.env["CODEX_CA_CERTIFICATE"] = self.ca
        (self.home.codex_home / "config.toml").write_text(
            'model = "gpt-5.5"\n[projects."%s"]\ntrust_level = "trusted"\n' % self.home.work)
        self.prime()

    def prime(self):
        """Native's databases, which protected mode lets native write but not create: one direct
        native session in codex_home with one prompt (native creates thread_history_1 with its first
        thread), through a fixture provider over HTTP (on chatgpt.com native would use a WebSocket,
        which the backend does not serve). It runs outside any boundary, so it gets protected mode's
        quiet set (no plugins, apps, memories, analytics, telemetry or web search; no update check),
        and the fixture proxy, the only exit it is given, must see nothing but the fixture names. Its
        root auth.json goes again, and so does what it sent upstream."""
        root = self.home.codex_home
        auth = root / "auth.json"
        shutil.copy(self.home.secrets / "codex-A.json", auth)
        auth.chmod(0o600)
        quiet = [pair for pair in protect.settings(0) if pair.startswith(("features.", "analytics.", "otel.",
                                                                          "memories.", "web_search="))]
        provider = ['model_provider="fixture"', 'model_providers.fixture.name="fixture"',
                    'model_providers.fixture.base_url="https://chatgpt.com/backend-api/codex"',
                    "model_providers.fixture.requires_openai_auth=true",
                    "model_providers.fixture.supports_websockets=false", 'model_providers.fixture.wire_api="responses"',
                    'cli_auth_credentials_store="file"', 'model="gpt-6-sol"',  # a current model: no migration prompt
                    "check_for_update_on_startup=false", *quiet]
        argv = [REAL_CODEX, *(word for pair in provider for word in ("-c", pair)), "--no-daemon"]
        self.terminal = Terminal(argv, dict(self.home.env, CODEX_HOME=str(root)), self.home.work)
        self.terminal.answer = True
        self.shows("Ask Codex", timeout=90)
        self.settle(2)
        self.type("priming prompt")
        self.shows("fixture reply", timeout=60)
        self.settle(2)
        self.terminal.send(b"\x03")
        time.sleep(0.5)
        self.terminal.send(b"\x03")
        self.assertEqual(os.waitstatus_to_exitcode(self.terminal.wait(timeout=30)), 0)
        self.terminal.close()
        self.terminal = None
        auth.unlink()
        for name in (*NATIVE.databases, *NATIVE.later_databases):
            self.assertTrue((root / (name + ".sqlite")).is_file(), name)
        # Besides the fixture names, only the TUI's start-up announcement tip, which 0.167.2 fetches
        # whatever the configuration (S: raw.githubusercontent.com/openai/codex/main/announcement_tip.toml)
        # and the fixture proxy refuses with 403. Plugin sync, GitHub releases or anything else fails here.
        self.assertTrue(self.proxy.targets)
        self.assertLessEqual(set(self.proxy.targets),
                             {"chatgpt.com:443", "auth.openai.com:443", "raw.githubusercontent.com:443"})
        with self.backend.lock:
            del self.backend.requests[:]

    def test_the_real_tui_starts_without_a_model_request(self):
        self.terminal = Terminal([str(LAUNCHER), "--reserve-5h", "10", "A"], self.home.env, self.home.work)
        self.terminal.answer = True
        self.terminal.expect("usage reserves for A", timeout=60)
        self.terminal.expect("OpenAI Codex", timeout=60)
        self.terminal.expect("v0\\.167\\.2", timeout=10)
        self.terminal.expect("Ask Codex", timeout=30)
        self.assertTrue(self.backend.seen("/oauth/token"))  # D1 (a): native's own refresh, before the TUI
        self.assertTrue(self.backend.seen("/backend-api/wham/usage"))
        for _ in range(25):  # let it settle: nothing reaches the model route unprompted
            self.terminal.read(0.2)
        self.assertEqual(self.backend.seen("/backend-api/codex/responses"), [])
        for request in self.backend.seen():
            agent = dict((k.lower(), v) for k, v in request["headers"]).get("user-agent", "")
            for private in ("coding-agent-kit", "codex-as", str(self.home.home), socket.gethostname()):
                self.assertNotIn(private, agent)
        self.terminal.send(b"\x03")
        time.sleep(0.5)
        self.terminal.send(b"\x03")
        self.assertEqual(os.waitstatus_to_exitcode(self.terminal.wait(timeout=30)), 0)

    def test_a_real_tool_writes_the_workspace_and_the_shared_sessions_but_not_the_rest_of_codex_home(self):
        """The reviewed TUI's exec tool runs a shell command inside the boundary: the workspace and the
        writable shared state take its writes; config.toml and a new file in codex_home do not."""
        (self.home.codex_home / "config.toml").write_text(
            'model = "gpt-6-sol"\n[projects."%s"]\ntrust_level = "trusted"\n' % self.home.work)
        root, report = self.home.codex_home, self.home.work / "tool-report.txt"
        config = (root / "config.toml").read_bytes()
        command = ("echo ran > %s; for f in %s %s %s %s; do (echo x >> $f) 2>/dev/null && echo ok $f >> %s "
                   "|| echo denied $f >> %s; done" % (report, root / "config.toml", root / "planted",
                                                      root / "skills/planted", root / "sessions/tool-written",
                                                      report, report))
        self.backend.tool = ("run the tool", "const r = await tools.exec_command({cmd: %s}); text(JSON.stringify(r));"
                             % json.dumps(command))
        self.terminal = self.start(args=("--reserve-5h", "10", "A"))
        self.terminal.answer = True
        self.shows("Ask Codex", timeout=90)
        self.settle(2)
        self.type("please run the tool")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (report.exists() and "sessions/tool-written" in report.read_text()):
            self.terminal.read(0.1)
        self.quiet()
        self.assertEqual(report.read_text().splitlines(),
                         ["ran", "denied %s" % (root / "config.toml"), "denied %s" % (root / "planted"),
                          "denied %s" % (root / "skills/planted"), "ok %s" % (root / "sessions/tool-written")])
        self.assertEqual((root / "config.toml").read_bytes(), config)
        outputs = [item for request in self.models() for item in json.loads(request["body"]).get("input", [])
                   if isinstance(item, dict) and item.get("type") == "custom_tool_call_output"]
        self.assertTrue(outputs)  # the tool's result went back to the model through the gate
        self.terminal.send(b"\x03")
        time.sleep(0.5)
        self.terminal.send(b"\x03")
        self.assertEqual(os.waitstatus_to_exitcode(self.terminal.wait(timeout=30)), 0)

    # The TUI redraws only the cells that change: words of one line may be separated by cursor moves.
    GAP = r"(?:\s|\x1b\[[0-9;?]*[ -/]*[@-~])+"

    def shows(self, phrase, timeout=60):
        return self.terminal.expect(self.GAP.join(re.escape(word) for word in phrase.split()), timeout)

    def type(self, text):
        for char in text:
            self.terminal.send(char)
            time.sleep(0.03)
        time.sleep(0.3)
        self.terminal.send("\r")

    def settle(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.terminal.read(0.1)

    def turn(self, text, timeout=30):
        """Type a prompt and return the body of the model request of its turn (not its title request)."""
        self.type(text)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for request in self.models():
                body = json.loads(request["body"])
                users = [item for item in body.get("input", []) if isinstance(item, dict)
                         and item.get("type") == "message" and item.get("role") == "user"]
                if users and users[-1].get("content") == [{"type": "input_text", "text": text}]:
                    self.quiet()
                    return json.dumps(body["input"])
            self.terminal.read(0.1)
        self.fail("no model request for %r; terminal output:\n%s" % (text, self.terminal.output.decode("utf-8", "replace")))

    def quiet(self, seconds=2.0):
        """Wait until no model request arrived for SECONDS (a turn is followed by its title request)."""
        count, since = len(self.models()), time.monotonic()
        while time.monotonic() - since < seconds:
            self.terminal.read(0.1)
            if len(self.models()) != count:
                count, since = len(self.models()), time.monotonic()

    def pause_and_refuse(self, text):
        """Lose the monitor (a pause), check that a prompt gets the gate's 503 and reaches no upstream,
        then continue from the launcher terminal."""
        before = len(self.models())
        commands = {pid: Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0") for pid in leftovers(self.home.base)}
        monitors = [pid for pid, argv in commands.items() if any(word.endswith(b"monitor.py") for word in argv)]
        self.assertTrue(monitors)
        for pid in monitors:
            os.kill(pid, signal.SIGKILL)
        self.terminal.expect("quota reserve paused \\(monitor_")
        self.terminal.expect("gate restarted paused", timeout=30)
        self.type(text)
        self.shows("503 Service Unavailable: quota reserve: model requests are paused")
        self.settle(2)
        self.assertEqual(len(self.models()), before)
        self.terminal.send(F12)
        code = self.terminal.expect("type ([0-9a-f]{8}) then Enter").group(1).decode()
        self.terminal.send(code + "\r")
        self.terminal.expect("fresh quota check passed", timeout=40)

    @unittest.skipUnless(os.path.isdir("/proc"), "finds the monitor through /proc")
    def test_in_tui_new_resume_and_fork_stay_behind_the_gate_and_the_pause(self):
        (self.home.codex_home / "config.toml").write_text(  # a current model: no migration prompt at start
            'model = "gpt-6-sol"\n[projects."%s"]\ntrust_level = "trusted"\n' % self.home.work)
        self.terminal = self.start(args=("--reserve-5h", "10", "A"))
        self.terminal.answer = True
        self.shows("Ask Codex", timeout=90)
        self.settle(2)
        self.assertIn("first prompt", self.turn("first prompt"))
        self.shows("fixture reply")
        self.type("/new")
        self.shows("Ask Codex")
        self.settle(1)
        self.assertNotIn("first prompt", self.turn("second prompt"))  # a new chat: no earlier turn
        self.pause_and_refuse("third prompt")
        self.type("/resume")
        self.shows("Resume a previous session")
        self.shows("first prompt")
        self.settle(1)
        self.terminal.send(b"\x1b[B")  # by update time: the second chat, then the first one
        time.sleep(0.5)
        self.terminal.send("\r")
        self.shows("Ask Codex")
        self.settle(2)
        resumed = self.turn("fourth prompt")
        self.assertIn("first prompt", resumed)
        self.assertNotIn("second prompt", resumed)
        self.pause_and_refuse("fifth prompt")
        self.type("/fork")
        self.shows("Fork created")
        self.settle(1)
        forked = self.turn("sixth prompt")
        self.assertIn("first prompt", forked)
        self.assertIn("fourth prompt", forked)
        # The refused fifth prompt stays in native's history and goes upstream only now, inside a
        # turn admitted after the continuation; while paused nothing reached upstream (counted above).
        self.assertIn("fifth prompt", forked)
        for request in self.models():  # every model request: account A's bearer and account id, once each
            headers = [(name.lower(), value) for name, value in request["headers"]]
            (bearer,) = [value for name, value in headers if name == "authorization"]
            self.assertEqual([value for name, value in headers if name == "chatgpt-account-id"], ["acct-fixture-A"])
            claims = json.loads(base64.urlsafe_b64decode(bearer.split(".")[1] + "=="))
            self.assertEqual(claims["https://api.openai.com/auth"]["chatgpt_account_id"], "acct-fixture-A")
            self.assertTrue(claims["fixture"].startswith("A1r"))  # a token the fixture's refresh issued
        self.terminal.send(b"\x03")
        time.sleep(0.5)
        self.terminal.send(b"\x03")
        self.assertEqual(os.waitstatus_to_exitcode(self.terminal.wait(timeout=30)), 0)


if __name__ == "__main__":
    unittest.main()
