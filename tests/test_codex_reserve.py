"""codex-as protected mode (usage reserves): screens, managed requirements, the Codex gate provider,
the decision adapter and whole protected launches.

Fabricated credentials only, a temporary HOME and 127.0.0.1 fixtures. Whole launches need the OS
boundary (Linux Landlock ABI 6+ on x86_64, or a reviewed macOS release) and openssl for the fixture
CA, and skip cleanly without them; with a real codex 0.167.2 on PATH one case runs it too.
"""

import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
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
provider_gate = load("provider_gate", ROOT / "lib/codex-as/gate.py")
kit = provider_gate.kit
reserve = provider_gate.reserve
backend = load("backend", FIXTURES / "backend.py")
layout = load("layout", ROOT / "lib/codex-as/layout.py")
NATIVE = protect.V0_167_2


def boundary_missing():
    name = "sandbox_macos" if sys.platform == "darwin" else "sandbox_linux"
    sandbox = load(name, ROOT / "lib/coding-agent-kit" / (name + ".py"))
    try:
        sandbox.probe()
    except sandbox.Refusal as error:
        return str(error)
    return None


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
                cache = {"signed_payload": {"version": 1, "bundle": bundle("[application.network]\n")}}
                (home / protect.BUNDLE_CACHE).write_text(json.dumps(cache))
                self.assertIn("application network", refused(protect.requirement_check, home, self.sqlite))
                cache["signed_payload"]["version"] = 2  # native ignores another cache version
                (home / protect.BUNDLE_CACHE).write_text(json.dumps(cache))
                protect.requirement_check(home, self.sqlite)


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


class Provider(unittest.TestCase):
    """The Codex provider and decision adapter on the shared gate, in process, over HTTP."""

    def setUp(self):
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)
        self.upstream.document = usage_document()
        self.provider = provider_gate.CodexProvider(ACCOUNT, USER, NATIVE, "/fixture/.codex",
                                                    upstream=kit.Upstream("127.0.0.1", self.upstream.port, tls=False))
        self.decider = provider_gate.Decider(policy())
        self.gate = kit.Gate(self.provider, self.decider, max_age=120)
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
        data = b"" if body is None else json.dumps(body).encode()
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

    @unittest.expectedFailure
    def test_a_gate_read_survives_an_upstream_that_closes_the_connection(self):
        """Kit gate.fetch sets a timeout on the socket after http.client closed it (EBADF) when the
        reply says Connection: close; L/nodes/X/gate-fetch-close.patch fixes it in gate.py (S2)."""
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
        self.log = self.codex_home / "fake-codex.log"
        if real_codex is None:
            fake = self.bin / "codex"
            fake.write_text("#!%s\n" % sys.executable + (FIXTURES / "fake_codex.py").read_text())
            fake.chmod(0o700)
        else:
            (self.bin / "codex").symlink_to(real_codex)
        self.write_credential(backend.credential())
        self.env = {name: value for name, value in os.environ.items() if name not in KIT_KNOBS
                    and not name.lower().endswith("_proxy") and not name.startswith(("CODEX_", "OPENAI_", "SSL_"))}
        self.env.update(HOME=str(self.home), PATH=os.pathsep.join((str(self.bin), "/usr/bin", "/bin")),
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
        path = self.home / ".config/coding-agent-kit"
        path.mkdir(parents=True, exist_ok=True)
        (path / "config.toml").write_text(text)

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
        self.home.config("[usage_reserve.codex_accounts.A]\nweekly = 10\n")
        result = self.home.run(["A"], env={"FAKE_VERSION": "codex-cli 0.168.0"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("newer than the newest reviewed release", result.stderr)
        self.assertEqual(self.home.events(), [])

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

    def test_startup_refusals_send_no_model_request_and_start_no_tui(self):
        cases = (
            ("threshold", lambda: setattr(self.backend, "used", [95, 20]), "five-hour window 5% remaining"),
            ("identity", lambda: setattr(self.backend, "user", "user-other"), "account changed"),
            ("refresh", lambda: setattr(self.backend, "refresh_status", 400), "pre-launch credential refresh failed"),
            ("usage", lambda: setattr(self.backend, "usage_status", 503), "no quota reading yet"),
        )
        for name, arrange, text in cases:
            with self.subTest(name=name):
                self.backend.used, self.backend.user = [10, 20], "user-fixture-A"
                self.backend.refresh_status = self.backend.usage_status = 200
                self.home.log.unlink(missing_ok=True)
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


REAL_CODEX = shutil.which("codex")


def real_version():
    if not REAL_CODEX:
        return None
    try:
        return subprocess.run([REAL_CODEX, "--version"], capture_output=True, text=True, timeout=20,
                              env={"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir()}).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


@unittest.skipIf(boundary_missing(), "protected mode needs the OS boundary")
@unittest.skipIf(shutil.which("openssl") is None, "the fixture CA needs openssl")
@unittest.skipUnless(real_version() == "codex-cli 0.167.2", "needs the reviewed codex 0.167.2 on PATH")
class RealCodex(Launch):
    """The reviewed native against loopback fixtures: a model-free protected start."""

    real_codex = REAL_CODEX

    def setUp(self):
        super().setUp()
        self.home.env["CODEX_CA_CERTIFICATE"] = self.ca
        (self.home.codex_home / "config.toml").write_text(
            'model = "gpt-5.5"\n[projects."%s"]\ntrust_level = "trusted"\n' % self.home.work)

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

if __name__ == "__main__":
    unittest.main()
