"""Offline tests of census.py: a temporary HOME, a fabricated native-login account and credential, and
the loopback Claude fixture upstream (tests/fixtures/claude-reserve/upstream.py). Nothing reaches a
real account, credential or provider.

    python3 -I -B -m unittest discover -s docs/superpowers/plans/quota-reserve-probes/lr -p test_census.py
"""

import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

HERE = Path(os.path.realpath(__file__)).parent
ROOT = HERE.parents[4]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


census = load("lr_test_census", HERE / "census.py")
upstream_fixture = load("lr_test_upstream", ROOT / "tests/fixtures/claude-reserve/upstream.py")
kit_config = load("lr_test_config", ROOT / "lib/coding-agent-kit/config.py")
native = load("lr_test_native", ROOT / "lib/claude-as/native.py")

TOKEN = "fixture-census-bearer-0001-SECRETVALUE"
REFRESH = "fixture-census-refresh-0001-SECRETVALUE"
ACCOUNT, ORGANIZATION = str(uuid.uuid4()), str(uuid.uuid4())
EMAIL = "fixture.person@example.invalid"
DOLLARS = ("98765.43", "87654.32", "76543.21", "4321987")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
TYPES = {"null", "object", "empty_object", "array", "empty_array", "string", "number", "boolean", "other"}
WORDS = TYPES | {"absent", "malformed", "out_of_range", "none", "model", "surface", "both", "five_hour", "seven_day",
                 "unknown"}
DROPPED = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "SystemRoot")


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class Wiring(census.Wiring):
    upstream = None
    security = None


class Census(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lr-census-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.secrets = self.home / ".secrets"
        self.out = self.base / "out"
        for path in (self.home, self.secrets, self.out):
            path.mkdir(mode=0o700)
            path.chmod(0o700)
        self.config_file = self.base / "config.toml"
        self.write_config(f'secrets_dir = "{self.secrets}"\n[claude_auth.accounts.N]\nmode = "native-login"\n')
        env = {key: value for key, value in os.environ.items() if key in DROPPED}
        env.update(HOME=str(self.home), CODING_AGENT_KIT_CONFIG=str(self.config_file))
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.paths = self.enroll("N")
        self.store()
        self.upstream = upstream_fixture.ClaudeUpstream()
        self.addCleanup(self.upstream.close)
        self.upstream.identities = {TOKEN: (ACCOUNT, ORGANIZATION)}
        now = time.time()
        self.five, self.week = upstream_fixture.stamp(now + 3600), upstream_fixture.stamp(now + 3 * 86400)
        self.upstream.extra = self.sensitive()
        security = self.base / "security"  # macOS: no Keychain item, so the store file is read
        security.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(44)\n")
        security.chmod(0o700)
        self.wiring = type("Loopback", (Wiring,), {
            "upstream": {"host": "127.0.0.1", "port": self.upstream.port, "tls": False},
            "security": str(security)})

    def write_config(self, text):
        self.config_file.write_text(text)
        self.config_file.chmod(0o600)

    def enroll(self, name, ready=True):
        paths = native.Paths(kit_config.load(), name)
        native.create_pending(paths)
        metadata = Path(paths.config) / ".claude.json"
        metadata.write_text(json.dumps({"oauthAccount": {"accountUuid": ACCOUNT, "organizationUuid": ORGANIZATION,
                                                         "emailAddress": EMAIL}}))
        metadata.chmod(0o600)
        if ready:
            native.mark_ready(paths, (ACCOUNT, ORGANIZATION))
        return paths

    def store(self, subscription="max"):
        path = Path(self.paths.config) / ".credentials.json"
        path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": TOKEN, "refreshToken": REFRESH, "expiresAt": int(time.time() * 1000) + 3600 * 1000,
            "scopes": ["user:inference", "user:profile"], "subscriptionType": subscription}}))
        path.chmod(0o600)

    def sensitive(self):
        """A usage document full of values the census must not keep: dollar amounts, a currency, labels,
        notices, an e-mail address, both UUIDs and the token, in values, labels and key names."""
        session = {"kind": "session", "group": "session", "percent": 12.25, "resets_at": self.five,
                   "severity": "normal", "is_active": False}
        entries = [session,
                   {"kind": "weekly_all", "group": "weekly", "percent": 31, "resets_at": self.week, "is_active": True},
                   {"kind": "weekly_scoped", "group": "weekly", "percent": 7.04, "resets_at": self.week,
                    "scope": {"model": {"display_name": "Sonnet 4.5"}}},
                   {"kind": "weekly_scoped", "group": "weekly", "percent": 1, "resets_at": None,
                    "scope": {"surface": {"display_name": "$%s credit" % DOLLARS[0]}}},
                   {"kind": ACCOUNT, "group": EMAIL, "severity": TOKEN, "percent": "high", "resets_at": "soon",
                    "owner_email": EMAIL, ORGANIZATION: 1, "scope": {"model": {"display_name": EMAIL},
                                                                     "surface": {"display_name": "Claude Code"}}},
                   "not an entry", None]
        entries += [dict(session, percent=index) for index in range(13)]  # 20 entries: only 16 are kept
        return {
            "five_hour": {"utilization": 12.25, "resets_at": self.five},
            "seven_day": {"utilization": 31, "resets_at": self.week},
            "seven_day_sonnet": {"utilization": 7.04, "resets_at": self.week},
            "seven_day_oauth_apps": {"utilization": 0, "resets_at": None},
            "wattle_ember": {"utilization": 61.5, "resets_at": self.week, "label": "Promo for " + EMAIL,
                             "limit_dollars": float(DOLLARS[0]), "used_dollars": float(DOLLARS[1]),
                             "remaining_dollars": float(DOLLARS[2]), "periods": [{"starts_at": self.five}],
                             "used_up_notice": "fixture notice $%s" % DOLLARS[3]},
            "extra_usage": {"is_enabled": True, "monthly_limit": int(DOLLARS[3]), "used_credits": 1, "utilization": 2.5,
                            "currency": "USD", "disabled_reason": "fixture reason"},
            "cinder_cove": None,
            "seven_day_cowork": {"utilization": 3, "resets_at": "next week", "owner": ACCOUNT},
            "contact": EMAIL, "account_uuid": ACCOUNT, "token_echo": TOKEN, ORGANIZATION: {"x": 1},
            "Odd Key": [1, 2], "limits": entries,
        }

    def run_census(self, *extra):
        out = io.StringIO()
        code = census.main(["--alias", "N", "--out", str(self.out), "--kit-commit", "0123abc", *extra],
                           wiring=self.wiring, out=out)
        return code, out.getvalue()

    def reads(self, path=None):
        return [r for r in self.upstream.requests if r["method"] == "GET" and (path is None or r["path"] == path)]

    def assertKeepsNoValue(self, text):
        for secret in (TOKEN, REFRESH, ACCOUNT, ORGANIZATION, EMAIL, "@", "$", "USD", "Promo", "fixture notice",
                       "fixture reason", "next week", "soon", "high", *DOLLARS):
            self.assertNotIn(secret, text)
        self.assertIsNone(UUID.search(text))

    def assertOnlyListedFields(self, result):
        """Every key of the result is one of research RQ section 4's fields, and every string a fixed word,
        a sanitized name or an allowed display name; numbers stand only where the list keeps one."""
        self.assertLessEqual(set(result), {"schema", "kit_commit", "time_utc", "outcome", "reason", "profile", "usage"})
        self.assertEqual(result["schema"], "lr-census-1")
        self.assertRegex(result.get("time_utc", "2026-01-01T00:00Z"), r"^\d{4}-\d\d-\d\dT\d\d:\d\dZ$")
        self.assertRegex(result.get("kit_commit", "0123abc"), r"^[0-9a-f]{7,40}$")
        self.assertRegex(result["outcome"], r"^[a-z_]+$")
        self.assertRegex(result.get("reason", "x"), r"^[a-z0-9_]{1,64}$")
        profile = result.get("profile", {})
        self.assertLessEqual(set(profile), {"http_status", "json", "identity_match", "error"})
        usage = result.get("usage", {})
        self.assertLessEqual(set(usage), {"http_status", "json", "object", "keys", "error"})
        for part in (profile, usage):
            self.assertTrue(all(isinstance(part[key], bool) for key in ("json", "identity_match", "object")
                                if key in part))
            self.assertTrue(number(part.get("http_status", 200)))
        labels = []
        for key in usage.get("keys", ()):
            self.assertLessEqual(set(key), {"name", "type", "fields", "utilization", "resets_at", "is_enabled",
                                            "length", "entries"})
            self.assertRegex(key["name"], r"^[a-z0-9_]{1,64}$")
            self.assertIn(key["type"], TYPES)
            self.assertTrue(all(re.fullmatch(r"[a-z0-9_]{1,64}", field) for field in key.get("fields", ())))
            for field in ("utilization", "resets_at"):
                if field in key:
                    self.assertTrue(number(key[field]) or key[field] in WORDS, key)
            if "is_enabled" in key:
                self.assertEqual(key["name"], "extra_usage")
                self.assertTrue(isinstance(key["is_enabled"], bool) or key["is_enabled"] in WORDS)
            self.assertTrue(number(key.get("length", 0)))
            self.assertTrue("entries" not in key or key["name"] == "limits")
            for entry in key.get("entries", ()):
                self.assertLessEqual(set(entry), {"type", "kind", "group", "severity", "is_active", "percent",
                                                  "resets_at", "same_reset_as", "scope", "model_display_name",
                                                  "surface_display_name", "other_fields"})
                self.assertIn(entry["type"], TYPES)
                for field in ("kind", "group", "severity"):
                    if field in entry:
                        self.assertTrue(re.fullmatch(r"[a-z0-9_]{1,32}", entry[field]), entry)
                for field in ("percent", "resets_at", "is_active"):
                    if field in entry:
                        self.assertTrue(number(entry[field]) or isinstance(entry[field], bool)
                                        or entry[field] in WORDS, entry)
                self.assertIn(entry.get("same_reset_as", "none"), ("five_hour", "seven_day", "none"))
                self.assertIn(entry.get("scope", "none"), ("none", "model", "surface", "both", "other"))
                self.assertTrue(all(re.fullmatch(r"[a-z0-9_]{1,64}", field) for field in entry.get("other_fields", ())))
                labels += [entry[field] for field in ("model_display_name", "surface_display_name") if field in entry]
        self.assertTrue(all(label == "other" or re.fullmatch(r"[A-Za-z0-9 .]{1,32}", label) for label in labels))
        return labels

    def test_one_read_keeps_only_the_listed_fields(self):
        code, output = self.run_census()
        self.assertEqual(code, census.EXIT_OK, output)
        result_file, marker = self.out / census.RESULT, self.out / census.MARKER
        for path in (result_file, marker):
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600)
        text = result_file.read_text()
        self.assertEqual(output, text)
        self.assertKeepsNoValue(text)
        self.assertKeepsNoValue(marker.read_text())
        result = json.loads(text)
        labels = self.assertOnlyListedFields(result)
        self.assertEqual(sorted(labels), ["Claude Code", "Sonnet 4.5", "other", "other"])
        self.assertEqual((result["outcome"], result["kit_commit"]), ("read", "0123abc"))
        self.assertEqual(result["profile"], {"http_status": 200, "json": True, "identity_match": True})
        # Exactly one profile GET and one usage GET, with the bearer and the neutral User-Agent; nothing else.
        self.assertEqual([r["path"] for r in self.upstream.requests], ["/api/oauth/profile", "/api/oauth/usage"])
        for request in self.upstream.requests:
            headers = {k.lower(): v for k, v in request["headers"]}
            self.assertEqual(headers["authorization"], "Bearer " + TOKEN)
            self.assertEqual(headers["user-agent"], "quota-probe/1")
        keys = {key["name"]: key for key in result["usage"]["keys"]}
        names = [key["name"] for key in result["usage"]["keys"]]
        self.assertEqual(names[:4], ["five_hour", "seven_day", "seven_day_opus", "seven_day_oauth_apps"])
        self.assertIn("unknown", names)  # the UUID key and "Odd Key"
        self.assertEqual(keys["five_hour"]["utilization"], 12.2)
        self.assertLessEqual(abs(keys["five_hour"]["resets_at"] - 1.0), 0.1)
        self.assertEqual((keys["seven_day_opus"]["type"], keys["cinder_cove"]["type"]), ("null", "null"))
        self.assertEqual(keys["seven_day_oauth_apps"]["resets_at"], "null")
        self.assertEqual(keys["wattle_ember"]["fields"], ["label", "limit_dollars", "periods", "remaining_dollars",
                                                          "resets_at", "used_dollars", "used_up_notice",
                                                          "utilization"])
        self.assertEqual(keys["wattle_ember"]["utilization"], 61.5)
        self.assertEqual((keys["extra_usage"]["is_enabled"], keys["extra_usage"]["utilization"]), (True, 2.5))
        self.assertEqual(keys["seven_day_cowork"]["resets_at"], "malformed")
        self.assertEqual([keys[name]["type"] for name in ("contact", "account_uuid", "token_echo")], ["string"] * 3)
        limits = keys["limits"]
        self.assertEqual((limits["length"], len(limits["entries"])), (20, 16))
        session, weekly, sonnet, surface, odd, text_entry, null_entry = limits["entries"][:7]
        self.assertEqual((session["kind"], session["percent"], session["same_reset_as"], session["scope"]),
                         ("session", 12.2, "five_hour", "none"))
        self.assertEqual((weekly["same_reset_as"], weekly["is_active"], weekly["severity"]), ("seven_day", True, "absent"))
        self.assertEqual((sonnet["scope"], sonnet["model_display_name"], sonnet["percent"]), ("model", "Sonnet 4.5", 7.0))
        self.assertEqual((surface["scope"], surface["surface_display_name"], surface["resets_at"]),
                         ("surface", "other", "null"))
        self.assertEqual((odd["kind"], odd["group"], odd["severity"], odd["percent"], odd["resets_at"], odd["scope"]),
                         ("other", "other", "other", "string", "malformed", "both"))
        self.assertEqual((odd["model_display_name"], odd["surface_display_name"]), ("other", "Claude Code"))
        self.assertEqual(odd["other_fields"], ["owner_email", "unknown"])
        self.assertEqual((text_entry, null_entry), ({"type": "string"}, {"type": "null"}))

    def test_a_second_attempt_is_refused_before_any_read(self):
        self.assertEqual(self.run_census()[0], census.EXIT_OK)
        before = (self.out / census.RESULT).read_bytes()
        count = len(self.upstream.requests)
        code, output = self.run_census()
        self.assertEqual(code, census.EXIT_ATTEMPTED)
        self.assertEqual(json.loads(output)["reason"], "already_attempted")
        self.assertEqual(len(self.upstream.requests), count)
        self.assertEqual((self.out / census.RESULT).read_bytes(), before)
        (self.out / census.RESULT).unlink()  # the marker alone still refuses
        self.assertEqual(self.run_census()[0], census.EXIT_ATTEMPTED)
        self.assertEqual(len(self.upstream.requests), count)

    def test_a_profile_of_another_identity_sends_no_usage_read(self):
        self.upstream.identities[TOKEN] = (str(uuid.uuid4()), ORGANIZATION)
        code, output = self.run_census()
        self.assertEqual(code, census.EXIT_NOT_OK)
        result = json.loads(output)
        self.assertEqual((result["outcome"], result["profile"]["identity_match"]), ("identity_unproved", False))
        self.assertEqual(self.reads("/api/oauth/usage"), [])
        self.assertKeepsNoValue(output)
        self.assertIsNone(UUID.search(output))
        self.assertOnlyListedFields(result)

    def test_a_failed_read_is_never_retried(self):
        self.upstream.status["/api/oauth/usage"] = 500
        code, output = self.run_census()
        self.assertEqual(code, census.EXIT_NOT_OK)
        result = json.loads(output)
        self.assertEqual((result["outcome"], result["usage"]), ("usage_unavailable", {"http_status": 500,
                                                                                       "json": True}))
        self.assertEqual(len(self.reads("/api/oauth/usage")), 1)
        closed = socket.socket()  # no reply at all: the gate's fixed reason word
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        self.wiring.upstream = dict(self.wiring.upstream, port=port)
        (self.out / census.MARKER).unlink()
        (self.out / census.RESULT).unlink()
        code, output = self.run_census()
        result = json.loads(output)
        self.assertEqual(result["outcome"], "profile_unavailable")
        self.assertRegex(result["profile"]["error"], r"^[a-z_]+$")

    def test_an_organization_account_is_refused_after_the_marker_and_before_any_request(self):
        self.store(subscription="team")
        code, output = self.run_census()
        self.assertEqual(code, census.EXIT_NOT_OK)
        self.assertEqual((json.loads(output)["outcome"], json.loads(output)["reason"]), ("refused", "organization_policy"))
        self.assertTrue((self.out / census.MARKER).exists())
        self.assertEqual(self.upstream.requests, [])

    def test_local_refusals_consume_nothing(self):
        def refused(reason, arrange, undo):
            arrange()
            try:
                code, output = self.run_census()
            finally:
                undo()
            self.assertEqual((code, json.loads(output)["reason"]), (census.EXIT_REFUSED, reason), output)
            self.assertEqual(list(self.out.iterdir()), [])
            self.assertEqual(self.upstream.requests, [])

        refused("out_dir_not_private", lambda: self.out.chmod(0o750), lambda: self.out.chmod(0o700))
        refused("out_dir_in_git_worktree", lambda: (self.base / ".git").mkdir(), lambda: (self.base / ".git").rmdir())
        refused("proxy_unsupported", lambda: os.environ.update(HTTPS_PROXY="socks5://127.0.0.1:9"),
                lambda: os.environ.pop("HTTPS_PROXY"))
        refused("not_native_login", lambda: self.write_config(f'secrets_dir = "{self.secrets}"\n'),
                lambda: self.write_config(f'secrets_dir = "{self.secrets}"\n[claude_auth.accounts.N]\n'
                                          'mode = "native-login"\n'))
        self.write_config(f'secrets_dir = "{self.secrets}"\n[claude_auth.accounts.P]\nmode = "native-login"\n')
        self.enroll("P", ready=False)
        out = io.StringIO()
        code = census.main(["--alias", "P", "--out", str(self.out), "--kit-commit", "0123abc"], wiring=self.wiring,
                           out=out)
        self.assertEqual((code, json.loads(out.getvalue())["reason"]), (census.EXIT_REFUSED, "registration_not_ready"))
        self.write_config(f'secrets_dir = "{self.secrets}"\n[claude_auth.accounts.N]\nmode = "native-login"\n')
        for argv, reason in ((["--alias", "a b", "--out", str(self.out), "--kit-commit", "0123abc"], "alias_invalid"),
                             (["--alias", "N", "--out", str(self.out), "--kit-commit", "HEAD"], "kit_commit_invalid"),
                             (["--alias", "N", "--out", "relative", "--kit-commit", "0123abc"], "out_dir_not_absolute"),
                             (["--alias", "N", "--out", str(self.secrets), "--kit-commit", "0123abc"],
                              "out_dir_in_secrets")):
            out = io.StringIO()
            self.assertEqual(census.main(argv, wiring=self.wiring, out=out), census.EXIT_REFUSED)
            self.assertEqual(json.loads(out.getvalue())["reason"], reason)
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(census.main(["--alias", "N"], wiring=self.wiring, out=io.StringIO()), census.EXIT_USAGE)
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertEqual(self.upstream.requests, [])

    def test_the_proxy_is_the_one_claude_code_would_use(self):
        """The launcher's rule: HTTPS_PROXY before HTTP_PROXY; a host NO_PROXY exempts goes direct."""
        os.environ.update(HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:1")
        code, output = self.run_census()
        self.assertEqual((code, json.loads(output)["outcome"]), (census.EXIT_NOT_OK, "profile_unavailable"))
        self.assertEqual(self.upstream.requests, [])
        for name in (census.MARKER, census.RESULT):
            (self.out / name).unlink()
        os.environ.update(NO_PROXY="127.0.0.1")
        self.assertEqual(self.run_census()[0], census.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
