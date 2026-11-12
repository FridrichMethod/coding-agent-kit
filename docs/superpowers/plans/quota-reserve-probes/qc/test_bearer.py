"""Offline tests for the QC bearer reader. Every credential here is fabricated."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pickle
import shutil
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("qc_bearer", HERE / "bearer.py")
bearer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bearer)

ACCESS_A = "QC-FABRICATED-ACCESS-A-NOT-A-CREDENTIAL"
ACCESS_A2 = "QC-FABRICATED-ACCESS-A2-NOT-A-CREDENTIAL"
REFRESH_A = "QC-FABRICATED-REFRESH-A-NOT-A-CREDENTIAL"
NOW_MS = 1_800_000_000_000
FUTURE_MS = NOW_MS + 3_600_000


def record(access=ACCESS_A, expires=FUTURE_MS, scopes=("user:inference", "user:profile"), **extra):
    oauth = {"accessToken": access, "refreshToken": REFRESH_A, "expiresAt": expires,
             "scopes": list(scopes), "subscriptionType": "max", "rateLimitTier": "fixture"}
    oauth.update(extra)
    return {"claudeAiOauth": oauth, "mcpOAuth": {}}


class Fixture:
    """<root>/secrets/native-claude-accounts/NAME/config with private modes."""

    def __init__(self):
        os.umask(0o077)
        self.root = Path(tempfile.mkdtemp(prefix="quota-qc-test-"))
        self.config = self.root / "secrets" / "native-claude-accounts" / "alpha" / "config"
        self.config.mkdir(parents=True, mode=0o700)
        for path in (self.root / "secrets", self.config.parent.parent, self.config.parent, self.config):
            path.chmod(0o700)
        self.env = {"CLAUDE_CONFIG_DIR": str(self.config), "HOME": str(self.root / "home"),
                    "PATH": "/usr/bin:/bin"}

    def write(self, document, mode=0o600, raw=None):
        path = self.config / bearer.CREDENTIAL_FILE
        data = raw if raw is not None else json.dumps(document).encode()
        staging = self.config / ".staging-fixture"
        fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(staging, mode)
        os.rename(staging, path)  # same pattern as native: staged file, then rename
        return path

    def read(self, **kwargs):
        return bearer.read_linux_bearer(str(self.config), self.env, now_ms=NOW_MS, **kwargs)

    def close(self):
        shutil.rmtree(self.root)


class LinuxReaderTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def refused(self, reason, **kwargs):
        with self.assertRaises(bearer.BearerRefused) as caught:
            self.fx.read(**kwargs)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(caught.exception.args, (reason,))
        for secret in (ACCESS_A, REFRESH_A, str(self.fx.root)):
            self.assertNotIn(secret, repr(caught.exception))
        return caught.exception

    def test_minimal_retention(self):
        self.fx.write(record())
        result = self.fx.read()
        self.assertTrue(result.matches(ACCESS_A))
        self.assertFalse(result.matches(ACCESS_A2))
        self.assertEqual(result.expires_at_ms, FUTURE_MS)
        self.assertTrue(result.profile_scope and result.inference_scope)
        self.assertEqual(result.source, "linux-file")
        self.assertNotIn(ACCESS_A, repr(result) + str(result))
        values = [getattr(result, name) for name in type(result).__slots__]
        self.assertNotIn(REFRESH_A, values)
        self.assertEqual(result.authorization_value(), "Bearer " + ACCESS_A)

    def test_bearer_is_not_hashable_comparable_or_serializable(self):
        self.fx.write(record())
        result = self.fx.read()
        for action in (lambda: hash(result), lambda: result == result,
                       lambda: pickle.dumps(result), lambda: copy.copy(result),
                       lambda: copy.deepcopy(result)):
            with self.assertRaises(TypeError):
                action()
        with self.assertRaises(AttributeError):
            result.expires_at_ms = 0

    def test_native_rename_rotation_is_seen_on_next_read(self):
        self.fx.write(record())
        first = self.fx.read()
        before = os.stat(self.fx.config / bearer.CREDENTIAL_FILE).st_ino
        self.fx.write(record(access=ACCESS_A2))
        self.assertNotEqual(before, os.stat(self.fx.config / bearer.CREDENTIAL_FILE).st_ino)
        second = self.fx.read()
        self.assertTrue(first.matches(ACCESS_A) and second.matches(ACCESS_A2))
        self.assertFalse(first.matches(second))

    def test_rotation_during_read_retries_then_refuses(self):
        self.fx.write(record())
        real_stat = os.stat
        calls = {"n": 0}

        def rotating(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == bearer.CREDENTIAL_FILE and kwargs.get("dir_fd") is not None:
                calls["n"] += 1
                if calls["n"] == 1:
                    fake = mock.Mock(st_dev=info.st_dev, st_ino=info.st_ino + 1)
                    return fake
            return info

        with mock.patch.object(bearer.os, "stat", side_effect=rotating):
            self.assertTrue(self.fx.read().matches(ACCESS_A))
        self.assertEqual(calls["n"], 2)

        def always(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if path == bearer.CREDENTIAL_FILE and kwargs.get("dir_fd") is not None:
                return mock.Mock(st_dev=info.st_dev, st_ino=info.st_ino + 1)
            return info

        with mock.patch.object(bearer.os, "stat", side_effect=always):
            self.refused("credential_rotating")

    def test_file_anomalies_refuse(self):
        self.refused("credential_file_absent")
        path = self.fx.write(record(), mode=0o640)
        self.refused("credential_file_not_private")
        path.chmod(0o600)
        link = self.fx.root / "hardlink"
        os.link(path, link)
        self.refused("credential_file_not_private")
        link.unlink()
        path.unlink()
        target = self.fx.root / "elsewhere.json"
        target.write_text(json.dumps(record()))
        target.chmod(0o600)
        os.symlink(target, path)
        self.refused("credential_file_unusable")
        path.unlink()
        path.mkdir(mode=0o700)
        self.refused("credential_file_not_private")
        path.rmdir()
        self.fx.write(None, raw=b"")
        self.refused("document_size")
        self.fx.write(None, raw=b" " * (bearer.DOCUMENT_LIMIT + 1))
        self.refused("document_size")

    def test_owner_mismatch_refuses(self):
        self.fx.write(record())
        self.refused("ancestor_not_trusted", uid=os.getuid() + 1)

    def test_directory_anomalies_refuse(self):
        self.fx.write(record())
        self.fx.config.chmod(0o750)
        self.refused("account_directory_not_private")
        self.fx.config.chmod(0o700)
        self.fx.config.parent.chmod(0o711)
        self.refused("account_directory_not_private")
        self.fx.config.parent.chmod(0o700)
        (self.fx.root / "secrets").chmod(0o770)
        self.refused("ancestor_writable")
        (self.fx.root / "secrets").chmod(0o700)
        moved = self.fx.root / "real-config"
        self.fx.config.rename(moved)
        os.symlink(moved, self.fx.config)
        self.refused("path_component_unusable")

    def test_sticky_world_writable_ancestor_is_accepted(self):
        (self.fx.root / "secrets").chmod(0o1777)
        self.fx.write(record())
        self.assertTrue(self.fx.read().matches(ACCESS_A))

    def test_document_anomalies_refuse(self):
        cases = [
            (b'{"claudeAiOauth": {"accessToken": "x", "accessToken": "y"}}', "duplicate_key"),
            (b'{"claudeAiOauth": {"expiresAt": NaN}}', "non_finite_number"),
            (b"\xff\xfe", "document_malformed"),
            (b"[1, 2]", "oauth_record_missing"),
            (json.dumps({"other": 1}).encode(), "oauth_record_missing"),
            (json.dumps(record(access="short")).encode(), "access_token_malformed"),
            (json.dumps(record(access=ACCESS_A + "\n")).encode(), "access_token_malformed"),
            (json.dumps(record(expires=True)).encode(), "expiry_malformed"),
            (json.dumps(record(expires=float(FUTURE_MS))).encode(), "expiry_malformed"),
            (json.dumps(record(expires=NOW_MS)).encode(), "expired_native_refresh_required"),
            (json.dumps(record(scopes=("user:profile",))).encode(), "inference_scope_missing"),
            (json.dumps({"claudeAiOauth": dict(record()["claudeAiOauth"], scopes="user:inference")}).encode(),
             "scopes_malformed"),
        ]
        for raw, reason in cases:
            with self.subTest(reason=reason):
                self.fx.write(None, raw=raw)
                self.refused(reason)

    def test_refresh_token_shape_is_never_examined(self):
        for value in (None, 7, ["list"], "", {"nested": True}):
            with self.subTest(value=value):
                document = record()
                document["claudeAiOauth"]["refreshToken"] = value
                self.fx.write(document)
                self.assertTrue(self.fx.read().matches(ACCESS_A))
        document = record()
        del document["claudeAiOauth"]["refreshToken"]
        self.fx.write(document)
        self.assertTrue(self.fx.read().matches(ACCESS_A))

    def test_profile_scope_is_reported_not_required(self):
        self.fx.write(record(scopes=("user:inference",)))
        self.assertFalse(self.fx.read().profile_scope)

    def test_selection_conflicts_refuse_before_any_read(self):
        self.fx.write(record())
        for name in bearer.STORE_SELECTORS:
            with self.subTest(name=name):
                self.fx.env[name] = ""
                with mock.patch.object(bearer, "open_account_directory") as opened:
                    self.refused("conflicting_selector")
                    opened.assert_not_called()
                del self.fx.env[name]
        self.fx.env["CLAUDE_CONFIG_DIR"] = str(self.fx.config) + "/"
        self.refused("config_dir_mismatch")
        del self.fx.env["CLAUDE_CONFIG_DIR"]
        self.refused("config_dir_mismatch")

    def test_non_canonical_spellings_refuse(self):
        for spelling in ("relative/config", "/a//b", "/a/./b", "/a/../b", "/a/b/",
                         "/a/café", "/a/\x01b"):
            with self.subTest(spelling=spelling):
                with self.assertRaises(bearer.BearerRefused) as caught:
                    bearer.canonical_parts(spelling)
                self.assertEqual(caught.exception.reason, "path_not_canonical")


class KeychainDesignTests(unittest.TestCase):
    CONFIG = "/Users/fixture/.secrets/native-claude-accounts/alpha/config"

    def env(self, **extra):
        value = {"CLAUDE_CONFIG_DIR": self.CONFIG, "USER": "fixture.user"}
        value.update(extra)
        return value

    def test_service_name_matches_native_derivation(self):
        digest = hashlib.sha256(self.CONFIG.encode()).hexdigest()[:8]
        self.assertEqual(bearer.keychain_service(self.CONFIG, self.env()),
                         "Claude Code-credentials-" + digest)
        other = bearer.keychain_service(self.CONFIG + "2", self.env())
        self.assertNotEqual(other, bearer.keychain_service(self.CONFIG, self.env()))
        with self.assertRaises(bearer.BearerRefused):
            bearer.keychain_service(self.CONFIG + "/", self.env())
        with self.assertRaises(bearer.BearerRefused):
            bearer.keychain_service(self.CONFIG, self.env(CLAUDE_SECURESTORAGE_CONFIG_DIR=""))

    def test_account_name_matches_native_sanitization(self):
        self.assertEqual(bearer.keychain_account({"USER": "a.b-c_d"}, "x"), "a.b-c_d")
        self.assertEqual(bearer.keychain_account({}, "osuser"), "osuser")
        self.assertEqual(bearer.keychain_account({"USER": "bad name"}, "x"), "claude-code-user")
        self.assertEqual(bearer.keychain_account({"USER": ""}, "fallback"), "fallback")

    def test_argv_shape(self):
        argv = bearer.keychain_argv(self.CONFIG, self.env(), "unused")
        self.assertEqual(argv[:3], ["/usr/bin/security", "find-generic-password", "-a"])
        self.assertEqual(argv[3:], ["fixture.user", "-s",
                                    bearer.keychain_service(self.CONFIG, self.env()), "-w"])
        self.assertNotIn("-g", argv)
        self.assertNotIn("-i", argv)

    def test_fake_runner_paths(self):
        document = json.dumps(record()).encode()
        seen = []

        def runner(result):
            def run(argv):
                seen.append(argv)
                return result
            return run

        got = bearer.read_macos_bearer(self.CONFIG, self.env(), "x", runner((0, document + b"\n")), NOW_MS)
        self.assertTrue(got.matches(ACCESS_A))
        self.assertEqual(got.source, "macos-keychain")
        got = bearer.read_macos_bearer(self.CONFIG, self.env(), "x",
                                       runner((0, document.hex().encode() + b"\n")), NOW_MS)
        self.assertTrue(got.matches(ACCESS_A))
        for result, reason in (((44, b""), "keychain_item_absent"), ((51, b""), "keychain_read_failed"),
                               ((0, b"not json"), "document_malformed"),
                               ((0, b"abc"), "document_malformed")):
            with self.subTest(reason=reason):
                with self.assertRaises(bearer.BearerRefused) as caught:
                    bearer.read_macos_bearer(self.CONFIG, self.env(), "x", runner(result), NOW_MS)
                self.assertEqual(caught.exception.reason, reason)
        count = len(seen)
        with self.assertRaises(bearer.BearerRefused):
            bearer.read_macos_bearer(self.CONFIG, self.env(ANTHROPIC_API_KEY="x"), "x",
                                     runner((0, document)), NOW_MS)
        self.assertEqual(len(seen), count)

    def test_execution_is_never_approved_here(self):
        with self.assertRaises(bearer.BearerRefused) as caught:
            bearer.KeychainPermission()
        self.assertEqual(caught.exception.reason, "keychain_read_not_approved")
        with mock.patch.object(bearer.sys, "platform", "darwin"):
            with self.assertRaises(bearer.BearerRefused):
                bearer.execute_keychain(["/usr/bin/security"], object())
        with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            with self.assertRaises(bearer.BearerRefused):
                bearer.execute_keychain(bearer.keychain_argv(self.CONFIG, self.env(), "x"), None)
            run.assert_not_called()
            popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
