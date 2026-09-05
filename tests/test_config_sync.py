"""Native, isolated config-backend tests; run with the provisioned interpreter."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("config_sync", ROOT / "lib/config_sync.py")
sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync)
CODEX = (ROOT / "common/codex/.codex/config.toml").read_bytes()
CLAUDE = b'{"permissions":{"allow":[],"ask":[]},"flag":false,"nil":null,"hooks":{"Stop":[]}}'


class TransformTests(unittest.TestCase):
    def codex(self, live, portable=CODEX):
        result, _ = sync.merge_codex(portable, live)
        return sync.parse_toml(result).unwrap(), result

    def test_pinned_runtime(self):
        sync.require_runtime()

    def test_codex_multiline_owned_array_regression(self):
        live = b'project_doc_fallback_filenames = [\n  "MACHINE.md",\n]\n'
        result, _ = self.codex(live)
        self.assertEqual(result["project_doc_fallback_filenames"], ["CLAUDE.md"])

    def test_codex_multiline_strings_preserve_content_and_comments(self):
        live = b'# keep heading\nnotes = """\n  first\n\n[not_a_table]\n  last\n""" # keep suffix\n'
        result, raw = self.codex(live)
        self.assertEqual(result["notes"], sync.parse_toml(live)["notes"])
        self.assertIn(live.rstrip(), raw)

    def test_codex_quoted_dotted_tables_inline_comments_and_aot(self):
        live = b'''"features".network_proxy = false # comment on owned field
"features".unknown = "keep"
[permissions."workspace-net".network.domains] # comment on table
"*" = "deny"
"private.example.org" = "deny"
[[mcp_servers.demo.tools]]
name = "first"
model = "local"
[[mcp_servers.demo.tools]]
name = "second"
'''
        result, raw = self.codex(live)
        self.assertTrue(result["features"]["network_proxy"])
        self.assertEqual(result["features"]["unknown"], "keep")
        self.assertEqual(result["permissions"]["workspace-net"]["network"]["domains"]["private.example.org"], "deny")
        self.assertEqual(result["mcp_servers"]["demo"]["tools"], [{"name": "first", "model": "local"}, {"name": "second"}])
        self.assertIn(b"# comment on table", raw)

    def test_codex_inline_live_tables(self):
        result, _ = self.codex(b'features = { network_proxy = false, unknown = "keep" }\n')
        self.assertTrue(result["features"]["network_proxy"])
        self.assertEqual(result["features"]["unknown"], "keep")

    def test_literal_dotted_table_name_is_not_an_owned_path(self):
        live = b'["permissions.workspace-net"]\nextends="literal-local"\n'
        result, _ = self.codex(live)
        self.assertEqual(result["permissions.workspace-net"]["extends"], "literal-local")
        self.assertEqual(result["permissions"]["workspace-net"]["extends"], ":workspace")

    def test_codex_quoted_and_multiline_portable(self):
        import tomlkit

        portable = sync.parse_toml(CODEX)
        portable["project_doc_fallback_filenames"] = tomlkit.array().multiline(True)
        portable["project_doc_fallback_filenames"].append("文档.md")
        encoded = tomlkit.dumps(portable).replace("[features]", '["features"] # legal header').encode("utf-8")
        result, _ = self.codex(b"", encoded)
        self.assertEqual(result["project_doc_fallback_filenames"], ["文档.md"])

    def test_codex_all_required_fields(self):
        import tomlkit

        for path, _ in sync.CODEX_POLICY:
            with self.subTest(path=path):
                portable = sync.parse_toml(CODEX)
                parent = portable
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                with self.assertRaisesRegex(sync.SyncError, "missing portable key"):
                    self.codex(b"", tomlkit.dumps(portable).encode())

    def test_codex_type_validation(self):
        for old, new in ((b'network_proxy = true', b'network_proxy = "true"'),
                         (b'project_doc_fallback_filenames = ["CLAUDE.md"]', b'project_doc_fallback_filenames = [42]')):
            with self.subTest(old=old):
                self.assertIn(old, CODEX)
                with self.assertRaisesRegex(sync.SyncError, "invalid portable value type"):
                    self.codex(b"", CODEX.replace(old, new))

    def test_codex_bad_toml_rejected(self):
        for bad in (b'a = [', b'a = 1\na = 2\n', b'\xff', b'[[missing]\n'):
            for portable, live in ((bad, b""), (CODEX, bad)):
                with self.subTest(portable=portable[:20], live=live):
                    with self.assertRaises(Exception):
                        self.codex(live, portable)

    def test_codex_rejects_ancestor_type_conflict(self):
        with self.assertRaisesRegex(sync.SyncError, "live table conflicts"):
            self.codex(b'features = "not a table"\n')

    def test_codex_retired_policy_and_unknown_state(self):
        live = b'''sandbox_mode = "read-only"
[sandbox_workspace_write]
network_access = false
[features]
js_repl = true
unknown = true
[projects."/machine-only/project"]
trust_level = "trusted"
'''
        result, _ = self.codex(live)
        self.assertNotIn("sandbox_mode", result)
        self.assertNotIn("sandbox_workspace_write", result)
        self.assertNotIn("js_repl", result["features"])
        self.assertTrue(result["features"]["unknown"])
        self.assertEqual(result["projects"]["/machine-only/project"]["trust_level"], "trusted")

    def test_codex_noop_bytes(self):
        _, once = self.codex(b'# runtime comment\nnotes="  keep  "\n')
        _, twice = self.codex(once)
        self.assertEqual(once, twice)

    def test_claude_authoritative_arrays_false_null_unicode_and_live_model(self):
        live = {"permissions": {"allow": ["old"], "ask": ["old"], "additionalDirectories": ["C:/文档"]},
                "hooks": {"Stop": [{"command": "old"}], "Unknown": [1]},
                "model": "local", "flag": True, "nil": 42, "hostOnly": {"nested": "保持"}}
        merged = sync.parse_json(sync.merge_claude(CLAUDE, json.dumps(live).encode()))
        self.assertEqual(merged["permissions"], {"allow": [], "ask": [], "additionalDirectories": ["C:/文档"]})
        self.assertEqual(merged["hooks"], {"Stop": [], "Unknown": [1]})
        self.assertFalse(merged["flag"])
        self.assertIsNone(merged["nil"])
        self.assertEqual(merged["model"], "local")
        self.assertEqual(merged["hostOnly"], {"nested": "保持"})

    def test_claude_type_conflicts_use_portable_values(self):
        live = b'{"permissions":false,"hooks":[],"flag":{"old":true},"nil":[1]}'
        self.assertEqual(sync.parse_json(sync.merge_claude(CLAUDE, live)), sync.parse_json(CLAUDE))

    def test_claude_invalid_inputs(self):
        for bad in (b'{', b'[]', b'null', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'\xff'):
            for portable, live in ((bad, b"{}"), (CLAUDE, bad)):
                with self.subTest(portable=portable, live=live):
                    with self.assertRaises((ValueError, UnicodeError)):
                        sync.merge_claude(portable, live)

    def test_claude_invalid_permission_policy(self):
        for policy in ({}, {"allow": []}, {"allow": [42], "ask": []}, {"allow": [], "ask": "bad"}):
            with self.subTest(policy=policy):
                with self.assertRaisesRegex(sync.SyncError, "string arrays"):
                    sync.merge_claude(json.dumps({"permissions": policy}).encode(), b"{}")

    def test_claude_model_is_not_portable(self):
        with self.assertRaisesRegex(sync.SyncError, "machine-local"):
            sync.merge_claude(b'{"model":"oops","permissions":{"allow":[],"ask":[]}}', b"{}")

    def test_claude_noop_preserves_original_format(self):
        live = CLAUDE + b"\n\n"
        self.assertEqual(sync.merge_claude(CLAUDE, live), live)

    def test_claude_booleans_are_not_numeric_noops(self):
        portable = b'{"permissions":{"allow":[],"ask":[]},"nested":{"flag":false,"values":[true,false]}}'
        live = b'{"permissions":{"allow":[],"ask":[]},"nested":{"flag":0,"values":[1,0]}}'
        merged = sync.parse_json(sync.merge_claude(portable, live))
        self.assertIs(merged["nested"]["flag"], False)
        self.assertIs(merged["nested"]["values"][0], True)
        self.assertIs(merged["nested"]["values"][1], False)

    def test_claude_empty_live_is_fresh(self):
        self.assertEqual(sync.parse_json(sync.merge_claude(CLAUDE, b"")), sync.parse_json(CLAUDE))


class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="dotfiles sync tests ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.portable = self.directory / "portable.toml"
        self.live = self.directory / "home" / "config.toml"
        self.portable.write_bytes(CODEX)

    def run_sync(self, *, kind="codex-config-sync", **kwargs):
        sync.synchronize(kind, self.portable, self.live, **kwargs)

    def create_live(self, content):
        self.live.parent.mkdir(parents=True, exist_ok=True)
        self.live.write_bytes(content)

    def wrapper_command(self, wrapper, *arguments):
        if os.name != "nt":
            return [str(wrapper), *map(str, arguments)]
        # Never accidentally select Windows' WSL bash.exe; wrappers need the
        # same Git Bash/MSYS path conversion used by stow-all.ps1.
        git = shutil.which("git")
        candidates = []
        if git:
            candidates.append(Path(git).parent.parent / "bin/bash.exe")
        candidates.append(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe")
        bash = next((path for path in candidates if path.is_file()), None)
        self.assertIsNotNone(bash, "Git Bash is required for Windows wrapper tests")
        return [str(bash), "--noprofile", "--norc", str(wrapper), *map(str, arguments)]

    def test_fresh_parent_and_source_immutable(self):
        before = self.portable.stat()
        self.run_sync()
        self.assertEqual(self.portable.read_bytes(), CODEX)
        self.assertEqual(self.portable.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertFalse(self.live.is_symlink())
        self.assertEqual(sync.parse_toml(self.live.read_bytes()).unwrap(), sync.parse_toml(CODEX).unwrap())

    def test_check_creates_no_directories_or_files(self):
        self.run_sync(check=True)
        self.assertFalse(self.live.parent.exists())
        self.assertEqual(list(self.directory.iterdir()), [self.portable])

    def test_check_validates_live(self):
        self.create_live(b'bad = [')
        with self.assertRaises(Exception):
            self.run_sync(check=True)
        self.assertEqual(self.live.read_bytes(), b'bad = [')

    def test_noop_keeps_inode_and_mtime(self):
        self.run_sync()
        before = self.live.stat()
        self.run_sync()
        after = self.live.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_legacy_relative_symlink_is_replaced_not_followed(self):
        self.live.parent.mkdir()
        self.live.symlink_to(Path("..") / self.portable.name)
        self.run_sync(check=True)
        self.assertTrue(self.live.is_symlink())
        self.run_sync()
        self.assertFalse(self.live.is_symlink())
        self.assertEqual(self.portable.read_bytes(), CODEX)

    def test_explicit_migration_preserves_polluted_live_state(self):
        polluted = '# legacy state\nruntime="保持"\n'.encode() + CODEX + b'\n[host]\nx=42\n'
        self.portable.write_bytes(polluted)
        self.live.parent.mkdir()
        self.live.symlink_to(self.portable)
        self.run_sync()
        self.assertEqual(self.portable.read_bytes(), polluted)
        self.run_sync(migrate=True)
        self.assertNotIn("runtime", sync.parse_toml(self.portable.read_bytes()))
        self.assertNotIn("host", sync.parse_toml(self.portable.read_bytes()))
        self.assertEqual(sync.parse_toml(self.live.read_bytes())["runtime"], "保持")
        self.assertEqual(sync.parse_toml(self.live.read_bytes())["host"]["x"], 42)

    def test_migration_retains_source_only_runtime_on_materialized_live(self):
        self.portable.write_bytes(CODEX + b'\n[projects.legacy]\ntrust_level="trusted"\n[projects.shared]\nx="source"\n')
        self.create_live(b'[projects.current]\ntrust_level="untrusted"\n[projects.shared]\nx="live"\n')
        self.run_sync(migrate=True)
        projects = sync.parse_toml(self.live.read_bytes())["projects"]
        self.assertEqual(projects["legacy"]["trust_level"], "trusted")
        self.assertEqual(projects["current"]["trust_level"], "untrusted")
        self.assertEqual(projects["shared"]["x"], "live")
        self.assertNotIn("projects", sync.parse_toml(self.portable.read_bytes()))

    def test_migration_retains_source_only_runtime_on_fresh_live(self):
        self.portable.write_bytes(CODEX + b'\n[projects.legacy]\ntrust_level="trusted"\n')
        self.run_sync(migrate=True)
        self.assertEqual(sync.parse_toml(self.live.read_bytes())["projects"]["legacy"]["trust_level"], "trusted")
        self.assertNotIn("projects", sync.parse_toml(self.portable.read_bytes()))

    def test_migration_source_write_failure_keeps_saved_live_and_old_source(self):
        polluted = CODEX + b'\n[projects.legacy]\ntrust_level="trusted"\n'
        self.portable.write_bytes(polluted)
        original = sync.os.replace

        def fail_only_source(source, target):
            if target == self.portable:
                raise PermissionError("injected source replacement failure")
            original(source, target)

        with mock.patch.object(sync.os, "replace", side_effect=fail_only_source):
            with self.assertRaises(PermissionError):
                self.run_sync(migrate=True)
        self.assertEqual(self.portable.read_bytes(), polluted)
        self.assertEqual(sync.parse_toml(self.live.read_bytes())["projects"]["legacy"]["trust_level"], "trusted")
        self.assertEqual(set(self.directory.iterdir()), {self.portable, self.live.parent})

    def test_missing_source_fail_closed(self):
        self.portable.unlink()
        with self.assertRaisesRegex(sync.SyncError, "missing portable"):
            self.run_sync()
        self.assertFalse(self.live.parent.exists())

    def test_directory_inputs_rejected(self):
        self.live.mkdir(parents=True)
        with self.assertRaisesRegex(sync.SyncError, "not a regular"):
            self.run_sync()
        self.portable.unlink()
        self.portable.mkdir()
        with self.assertRaisesRegex(sync.SyncError, "not a regular"):
            self.run_sync()

    def test_dangling_symlink_not_treated_as_fresh(self):
        self.live.parent.mkdir()
        self.live.symlink_to("missing.toml")
        with self.assertRaisesRegex(sync.SyncError, "dangling symlink"):
            self.run_sync()
        self.assertTrue(self.live.is_symlink())

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_unreadable_live_fails_closed(self):
        self.create_live(b'local = "keep"\n')
        self.live.chmod(0)
        self.addCleanup(self.live.chmod, 0o600)
        with self.assertRaisesRegex(sync.SyncError, "unreadable"):
            self.run_sync()

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_unreadable_portable_fails_closed(self):
        self.portable.chmod(0)
        self.addCleanup(self.portable.chmod, 0o600)
        with self.assertRaisesRegex(sync.SyncError, "unreadable"):
            self.run_sync()
        self.assertFalse(self.live.parent.exists())

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_unix_permissions(self):
        self.run_sync()
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o600)
        self.portable.write_bytes(CLAUDE)
        self.live.unlink()
        self.run_sync(kind="claude-settings-sync")
        self.assertEqual(stat.S_IMODE(self.live.stat().st_mode), 0o644)

    @unittest.skipIf(os.name == "nt", "POSIX FIFO")
    def test_fifo_rejected_without_blocking(self):
        self.live.parent.mkdir()
        os.mkfifo(self.live)
        with self.assertRaisesRegex(sync.SyncError, "not a regular"):
            self.run_sync()

    def test_same_source_and_live_rejected(self):
        self.live = self.portable
        with self.assertRaisesRegex(sync.SyncError, "same regular file"):
            self.run_sync()
        self.assertEqual(self.portable.read_bytes(), CODEX)

    def test_hardlinked_source_and_live_rejected(self):
        self.live.parent.mkdir()
        self.live.hardlink_to(self.portable)
        with self.assertRaisesRegex(sync.SyncError, "same regular file"):
            self.run_sync()

    def test_replace_failure_preserves_legacy_link_and_cleans_temp(self):
        self.live.parent.mkdir()
        self.live.symlink_to(self.portable)
        with mock.patch.object(sync.os, "replace", side_effect=PermissionError("injected replace failure")):
            with self.assertRaises(PermissionError):
                self.run_sync()
        self.assertTrue(self.live.is_symlink())
        self.assertEqual(self.portable.read_bytes(), CODEX)
        self.assertEqual(list(self.live.parent.iterdir()), [self.live])

    def test_fsync_failure_preserves_regular_file_and_cleans_temp(self):
        self.create_live(b'local="keep"\n')
        with mock.patch.object(sync.os, "fsync", side_effect=OSError("injected write failure")):
            with self.assertRaises(OSError):
                self.run_sync()
        self.assertEqual(self.live.read_bytes(), b'local="keep"\n')
        self.assertEqual(list(self.live.parent.iterdir()), [self.live])

    def test_temp_creation_failure_keeps_legacy_symlink(self):
        self.live.parent.mkdir()
        self.live.symlink_to(self.portable)
        with mock.patch.object(sync.tempfile, "mkstemp", side_effect=PermissionError("injected temporary failure")):
            with self.assertRaises(PermissionError):
                self.run_sync()
        self.assertTrue(self.live.is_symlink())
        self.assertEqual(self.portable.read_bytes(), CODEX)

    def test_observed_concurrent_write_is_not_overwritten(self):
        self.create_live(b'local="old"\n')
        with mock.patch.object(sync, "read_regular", wraps=sync.read_regular) as reader:
            original = sync.os.fsync

            def changed_after_write(fd):
                original(fd)
                self.live.write_bytes(b'local="new"\n')

            with mock.patch.object(sync.os, "fsync", side_effect=changed_after_write):
                with self.assertRaisesRegex(sync.SyncError, "changed during synchronization"):
                    self.run_sync()
            self.assertGreaterEqual(reader.call_count, 3)
        self.assertEqual(self.live.read_bytes(), b'local="new"\n')
        self.assertEqual(list(self.live.parent.iterdir()), [self.live])

    def test_rules_copy_bytes_only_and_preserve_siblings(self):
        self.portable.write_bytes(b'# rules\nopaque_non_parser_payload()\n')
        self.live.parent.mkdir()
        sibling = self.live.parent / "default.rules"
        sibling.write_bytes(b"machine-local")
        self.run_sync(kind="codex-rules-sync", check=True)
        self.assertFalse(self.live.exists())
        self.run_sync(kind="codex-rules-sync")
        self.assertEqual(self.live.read_bytes(), self.portable.read_bytes())
        self.assertEqual(sibling.read_bytes(), b"machine-local")

    def test_empty_rules_fail_closed(self):
        self.portable.write_bytes(b"")
        with self.assertRaisesRegex(sync.SyncError, "empty portable rules"):
            self.run_sync(kind="codex-rules-sync", check=True)
        self.assertFalse(self.live.parent.exists())

    def test_claude_check_no_mutation(self):
        self.portable.write_bytes(CLAUDE)
        self.run_sync(kind="claude-settings-sync", check=True)
        self.assertFalse(self.live.parent.exists())

    def test_cli_native_python_paths_with_spaces(self):
        self.live = self.directory / "文档 home" / "配置.toml"
        result = subprocess.run([sys.executable, "-I", "-B", "-X", "utf8", str(ROOT / "lib/config_sync.py"),
                                 "codex-config-sync", "--check", str(self.portable), str(self.live)],
                                capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("配置.toml", result.stdout)
        self.assertFalse(self.live.parent.exists())

    def test_stowed_wrapper_resolves_relative_symlinks_and_spaced_paths(self):
        bin_dir = self.directory / "fake home" / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        helper = ROOT / "common/codex/.local/bin/codex-config-sync"
        intermediary = self.directory / "relative wrapper"
        intermediary.symlink_to(helper)
        wrapper = bin_dir / "codex-config-sync"
        wrapper.symlink_to(os.path.relpath(intermediary, bin_dir))
        self.live = self.directory / "文档 home" / "配置.toml"
        env = dict(os.environ, DOTFILES_SYNC_PYTHON=sys.executable)
        result = subprocess.run(self.wrapper_command(wrapper, "--check", self.portable, self.live), env=env,
                                capture_output=True, encoding="utf-8", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("配置.toml", result.stdout)
        self.assertFalse(self.live.parent.exists())

    def test_invalid_runtime_override_fails_closed_without_fallback(self):
        helper = ROOT / "common/codex/.local/bin/codex-config-sync"
        env = dict(os.environ, DOTFILES_SYNC_PYTHON=str(self.directory / "missing interpreter"))
        result = subprocess.run(self.wrapper_command(helper, self.portable, self.live), env=env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("runtime unavailable", result.stderr)
        self.assertFalse(self.live.parent.exists())

    def test_setup_rejects_linked_environment_without_touching_target(self):
        external = self.directory / "external environment"
        external.mkdir()
        marker = external / "pyvenv.cfg"
        marker.write_bytes(b"sentinel: not ours")
        linked = self.directory / ".venv-sync"
        linked.symlink_to(external, target_is_directory=True)
        if os.name == "nt":
            setup = self.directory / "setup-sync.ps1"
            shutil.copyfile(ROOT / setup.name, setup)
            command = ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(setup), "-Python", "must-not-run"]
        else:
            setup = self.directory / "setup-sync.sh"
            shutil.copyfile(ROOT / setup.name, setup)
            command = ["sh", str(setup), "--python", "must-not-run"]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing" if os.name == "nt" else "refusing", result.stderr)
        self.assertEqual(marker.read_bytes(), b"sentinel: not ours")
        self.assertEqual(list(external.iterdir()), [marker])

    def test_cli_invalid_inputs_fail_without_traceback_or_writes(self):
        self.create_live(b'bad = [')
        result = subprocess.run([sys.executable, "-I", "-B", str(ROOT / "lib/config_sync.py"),
                                 "codex-config-sync", "--check", str(self.portable), str(self.live)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.live.read_bytes(), b'bad = [')

    def test_missing_or_mismatched_tomlkit_fails_closed(self):
        for installed in ("0.0.0", None):
            effect = sync.importlib.metadata.PackageNotFoundError("tomlkit") if installed is None else None
            with self.subTest(installed=installed):
                with mock.patch.object(sync.importlib.metadata, "version", return_value=installed, side_effect=effect):
                    with self.assertRaisesRegex(sync.SyncError, "setup-sync"):
                        sync.require_runtime()


if __name__ == "__main__":
    unittest.main()
