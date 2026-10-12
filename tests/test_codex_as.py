"""Isolated HOME, fabricated JSON and fake executable; never runs real Codex."""

import base64
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "common/codex/.local"
SHELLS = [shutil.which(name) for name in ("bash", "zsh") if shutil.which(name)]
TOKENS = ["fabricated-sensitive-access-A", "fabricated-sensitive-access-B", "fabricated-refreshed-A"]
ENV_CONFLICTS = (
    "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_API_KEY", "OPENAI_BASE_URL",
    "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_ORGANIZATION", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
    "CODEX_CHATGPT_BASE_URL", "CODEX_CHATGPT_ACCOUNT_ID", "OPENAI_FEDERATION_RULE_ID",
    "OPENAI_WORKLOAD_IDENTITY_CONTEXT", "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
    "CODEX_APP_SERVER_LOGIN_CLIENT_ID", "CODEX_AUTHAPI_BASE_URL",
    "CODEX_AGENT_IDENTITY_AUTHAPI_BASE_URL", "CODEX_AGENT_IDENTITY_JWKS_BASE_URL",
)
FAKE = r'''
import json, os, pathlib, sys
if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_VERSION", "codex-cli 0.162.1"))
    sys.exit(0)
args = sys.argv[1:]
assert args[:2] == ["-c", 'cli_auth_credentials_store="file"']
home = pathlib.Path(os.environ["HOME"])
assert os.environ["CODEX_HOME"] == str(home / ".codex")
auth = home / ".codex/auth.json"
mode = os.environ.get("FAKE_MODE", "normal")
record = {"args": args, "cwd": os.getcwd(), "stdin": sys.stdin.read(),
          "tty": [os.isatty(n) for n in (0, 1, 2)], "umask": os.umask(0o077)}
pathlib.Path(os.environ["FAKE_RECORD"]).write_text(json.dumps(record))
if mode in ("refresh", "rename"):
    data = json.loads(auth.read_text())
    data["tokens"]["access_token"] = "fabricated-refreshed-A"
    data["tokens"]["refresh_token"] = "fabricated-new-refresh-A"
    if mode == "refresh":
        auth.write_text(json.dumps(data))
    else:
        temporary = auth.with_suffix(".new")
        temporary.write_text(json.dumps(data))
        os.replace(temporary, auth)
print("stdout sentinel")
print("stderr sentinel", file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


class CodexAs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-as-test-")
        self.addCleanup(self.tmp.cleanup)
        # macOS TMPDIR uses /var -> /private/var; getcwd() reports the physical
        # path. Keep all fixture expectations in that same directory spelling.
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home with spaces"
        self.home.mkdir(mode=0o700)
        self.codex = self.home / ".codex"
        self.codex.mkdir(mode=0o700)
        self.secret = self.home / ".secrets"
        self.secret.mkdir(mode=0o700)
        self.auth = self.codex / "auth.json"
        self.install = self.home / ".local/lib/codex-as"
        shutil.copytree(SOURCE / "lib/codex-as", self.install)
        self.command_bin = self.home / ".local/bin"
        self.command_bin.mkdir()
        shutil.copy2(SOURCE / "bin/codex-as", self.command_bin / "codex-as")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = self.bin / "codex"
        fake.write_text(f"#!{sys.executable}\n" + FAKE)
        fake.chmod(0o700)
        self.cwd = self.base / "current working directory"
        self.cwd.mkdir()
        for name, content in {
            "config.toml": 'model = "unchanged-model"\n',
            "sessions/old.jsonl": "old session\n",
            "history.jsonl": "old history\n",
            "skills/example/SKILL.md": "old skill\n",
            "mcp.json": "old MCP configuration\n",
        }.items():
            path = self.codex / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for name, token in zip(("A", "B"), TOKENS):
            self.write_credential(name, {
                "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
                "tokens": {"id_token": "fake." + base64.urlsafe_b64encode(b'{}').decode().rstrip("=") + ".fake", "access_token": token,
                           "refresh_token": "fabricated-refresh-" + name, "account_id": name},
                "last_refresh": "2026-10-01T00:00:00Z",
                "unknown": {"preserve": [1, {"nested": True}]},
            })
        marker = self.codex / "codex-as.initialized"
        marker.write_text("codex-as v1\n")
        marker.chmod(0o600)
        self.shared = self.snapshot()
        # Explicitly initialized, managed link; the function never adopts a
        # regular file or repairs a missing link on its own.
        self.auth.symlink_to(self.secret / "codex-A.json")
        self.env = os.environ.copy()
        for name in (*ENV_CONFLICTS, "CODEX_HOME", "PYTHONPATH", "BASH_ENV", "ENV", "ZDOTDIR"):
            self.env.pop(name, None)
        self.record = self.base / "record.json"
        self.env.update(HOME=str(self.home), PATH=os.pathsep.join((str(self.bin), str(self.command_bin), self.env["PATH"])),
                        FAKE_RECORD=str(self.record))

    def write_credential(self, name, data):
        path = self.secret / f"codex-{name}.json"
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        return path

    def snapshot(self):
        return {str(p.relative_to(self.codex)): (hashlib.sha256(p.read_bytes()).hexdigest(),
                stat.S_IMODE(p.stat().st_mode)) for p in self.codex.rglob("*") if p.is_file()}

    def run_as(self, shell, args, env=None, input="", setup=""):
        flags = ["-f"] if Path(shell).name == "zsh" else ["--noprofile", "--norc"]
        command = [shell, *flags, "-c", setup + 'command codex-as "$@"',
                   "codex-as-test", *args]
        result = subprocess.run(command, cwd=self.cwd, env=self.env | (env or {}),
                                input=input, text=True, capture_output=True, timeout=10)
        for token in (*TOKENS, "fabricated-new-refresh-A", "fabricated-conflict-secret"):
            if token in result.stdout + result.stderr:
                self.fail("secret appeared in output (values hidden)")
        return result

    def rejected(self, shell, args, env=None):
        before = self.auth.read_bytes() if self.auth.is_file() else None
        link = os.readlink(self.auth) if self.auth.is_symlink() else None
        self.record.unlink(missing_ok=True)
        result = self.run_as(shell, args, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.record.exists(), "refusal must not launch fake Codex")
        self.assertEqual(os.readlink(self.auth) if self.auth.is_symlink() else None, link)
        self.assertTrue((self.auth.read_bytes() if self.auth.is_file() else None) == before,
                        "refusal modified credentials (values hidden)")
        return result

    def test_switch_and_refresh(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                for name in ("A", "B", "A"):
                    result = self.run_as(shell, [name])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(os.readlink(self.auth), str(self.secret / f"codex-{name}.json"))
                before_b = (self.secret / "codex-B.json").read_bytes()
                self.assertEqual(self.run_as(shell, ["A"], {"FAKE_MODE": "refresh"}).returncode, 0)
                self.assertTrue(self.auth.is_symlink())
                for name in ("B", "A"):
                    self.assertEqual(self.run_as(shell, [name]).returncode, 0)
                data = json.loads(self.auth.read_text())
                self.assertTrue(data["tokens"]["access_token"] == TOKENS[2], "refresh not retained (values hidden)")
                self.assertTrue(data["tokens"]["refresh_token"] == "fabricated-new-refresh-A", "refresh token not retained (values hidden)")
                self.assertEqual(data["unknown"], {"preserve": [1, {"nested": True}]})
                self.assertTrue((self.secret / "codex-B.json").read_bytes() == before_b,
                                "wrong account was modified (values hidden)")
                self.assertEqual({k: v for k, v in self.snapshot().items() if k != "auth.json"}, self.shared)
                self.assertEqual(list(self.codex.glob(".codex-as-*")), [])

    def test_arguments_io_cwd_exit_and_umask(self):
        cases = [[], ["resume", "--last"], ["fork", "--last"], ["--help"],
                 ["hello"], ["hello", "exec", "login"], ["--", "logout"],
                 ["resume", "--last", "logout"], ["fork", "--last", "login"],
                 ["-m", "logout", "hello"],
                 ["exec", "--json", 'spaces "double" and \'single\'', "", "--color", "never"],
                 ["exec", "login"], ["exec", "logout"], ["exec", "--", "--config", "auth=prompt"],
                 ["exec", "resume", "--last", "检查测试"],
                 ["-m", "login", "exec", "--json", "resume logout login"],
                 ["-c", 'model="harmless"', "exec", "login --config auth=sensitive text"],
                 ["exec", "-cmodel=harmless", "--json", "检查测试"]]
        for shell in SHELLS:
            for args in cases:
                with self.subTest(shell=shell, args=args):
                    result = self.run_as(shell, ["A", *args], {"FAKE_EXIT": "23"}, input="stdin text\n第二行\n")
                    self.assertEqual(result.returncode, 23, result.stderr)
                    self.assertEqual(result.stdout, "stdout sentinel\n")
                    self.assertEqual(result.stderr, "stderr sentinel\n")
                    record = json.loads(self.record.read_text())
                    mode = "exec" if "exec" in args else "interactive"
                    expected = ["-c", 'cli_auth_credentials_store="file"']
                    if mode == "interactive":
                        expected += ["--no-daemon"]
                    self.assertEqual(record["args"], expected + args)
                    self.assertEqual(record["cwd"], str(self.cwd))
                    self.assertEqual(record["stdin"], "stdin text\n第二行\n")
                    self.assertEqual(record["umask"], 0o077)

    def test_root_prompt_cannot_hide_a_later_subcommand(self):
        # Codex clap still dispatches a root subcommand after its optional
        # PROMPT. Refusal must happen before switching the shared auth link.
        for shell in SHELLS:
            for args in (["hello", "login"], ["hello", "logout"],
                         ["hello", "--no-alt-screen", "logout"],
                         ["-m", "login", "hello", "logout"],
                         ["hello", "mcp"], ["hello", "help", "logout"]):
                with self.subTest(shell=shell, args=args):
                    result = self.rejected(shell, ["B", *args])
                    self.assertIn("unsupported subcommand", result.stderr)

    def test_non_finite_credential_json_is_refused_before_switching(self):
        # Python's permissive JSON constants cannot enter Codex's strict
        # serde_json authentication reader, even in an unknown nested field.
        target = self.secret / "codex-B.json"
        for shell in SHELLS:
            for constant in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(shell=shell, constant=constant):
                    target.write_text('{"OPENAI_API_KEY":"fixture-key","unknown":{"value":'
                                      + constant + '}}')
                    self.rejected(shell, ["B"])

    def test_api_key_optional_fields_must_match_native_credential_schema(self):
        # Falsy values are not interchangeable with absent/null Option fields
        # in the native authentication reader. A refusal preserves account A.
        for shell in SHELLS:
            for field, value in (("tokens", {}), ("tokens", []), ("tokens", False),
                                 ("tokens", 0), ("tokens", ""),
                                 ("last_refresh", "bad-date"), ("last_refresh", False),
                                 ("last_refresh", "2026-13-01T00:00:00Z"),
                                 ("last_refresh", "2026-10-01T00:00:00")):
                with self.subTest(shell=shell, field=field, value=value):
                    self.write_credential("B", {"OPENAI_API_KEY": "fixture-key", field: value})
                    self.rejected(shell, ["B"])
            for fields in ({}, {"tokens": None, "last_refresh": None},
                           {"last_refresh": "2026-10-01T00:00:00Z"}):
                with self.subTest(shell=shell, fields=fields):
                    self.write_credential("B", {"OPENAI_API_KEY": "fixture-key", **fields})
                    result = self.run_as(shell, ["B", "exec", "prompt"])
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_inputs_and_conflicts(self):
        for shell in SHELLS:
            for index, args in enumerate(([], [""], ["../A"], ["A/B"], ["A B"], ["A.1"], ["中文"], ["missing"],
                         ["A", "login"], ["A", "logout"], ["A", "--remote", "server"],
                         ["A", "exec", "--oss"], ["A", "-p", "other"],
                         ["A", "-c", 'cli_auth_credentials_store="keyring"'],
                         ["A", "exec", "--config=OPENAI_API_KEY=fabricated-conflict-secret"],
                         ["A", "exec", 'prompt text', "-cmodel_provider=other"],
                         ["A", "--config", ' "cli_auth_credentials_store" = "auto"'],
                         ["A", "exec", "--ignore-rules"], ["A", "exec", "--ignore-user-config"])):
                with self.subTest(shell=shell, case=index):
                    self.rejected(shell, args)
            for variable in ENV_CONFLICTS:
                result = self.rejected(shell, ["A"], {variable: "fabricated-conflict-secret"})
                self.assertIn(variable, result.stderr)
            self.rejected(shell, ["A"], {"CODEX_HOME": str(self.base / "other-home")})
            self.rejected(shell, ["A"], {"FAKE_VERSION": "codex-cli 99.0.0"})

    def test_json_permission_and_link_refusals(self):
        for shell in SHELLS:
            a = self.secret / "codex-A.json"
            self.auth.unlink(missing_ok=True)
            self.auth.symlink_to(a)
            original = a.read_bytes()
            for bad in ("broken-json", "{}", "[]", '{"tokens":{}}', '{"auth_mode":"chatgptAuthTokens"}',
                        '{"OPENAI_API_KEY":"one","OPENAI_API_KEY":"two"}',
                        '{"tokens":{"access_token":"fabricated-sensitive-access-A"}}'):
                a.write_text(bad)
                self.rejected(shell, ["A"])
            a.write_bytes(original)
            for field, value in (("last_refresh", None), ("last_refresh", "bad-date"),
                                 ("OPENAI_API_KEY", ""), ("OPENAI_API_KEY", []),
                                 ("personal_access_token", "fabricated-conflict-secret"),
                                 ("bedrock_api_key", {}), ("agent_identity", "fabricated-conflict-secret")):
                data = json.loads(original)
                data[field] = value
                a.write_text(json.dumps(data))
                self.rejected(shell, ["A"])
            a.write_bytes(original)
            a.chmod(0o644)
            self.rejected(shell, ["A"])
            self.assertEqual(stat.S_IMODE(a.stat().st_mode), 0o644)
            a.chmod(0o600)
            hard = self.base / "hardlink"
            os.link(a, hard)
            self.rejected(shell, ["A"])
            hard.unlink()
            backup = self.base / "original-credential"
            a.rename(backup)
            a.symlink_to(backup)
            self.rejected(shell, ["A"])
            a.unlink()
            backup.rename(a)
            self.auth.unlink(missing_ok=True)
            self.auth.write_bytes(original)
            self.rejected(shell, ["B"])
            self.auth.unlink()
            self.rejected(shell, ["A"])
            for target in (str(self.base / "unmanaged.json"), "../.secrets/codex-A.json"):
                self.auth.symlink_to(target)
                self.rejected(shell, ["B"])
                self.auth.unlink()
            self.auth.symlink_to(self.secret / "codex-missing.json")
            self.rejected(shell, ["B"])
            self.auth.unlink()
            for directory, mode in ((self.secret, 0o755), (self.codex, 0o775)):
                directory.chmod(mode)
                self.rejected(shell, ["A"])
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), mode)
                directory.chmod(0o700)
            self.secret.rename(self.base / "real-secrets")
            self.secret.symlink_to(self.base / "real-secrets", target_is_directory=True)
            self.rejected(shell, ["A"])
            self.secret.unlink()
            (self.base / "real-secrets").rename(self.secret)
            # Wrong ownership is tested against root-owned /etc/passwd without reads.
            if os.getuid() != os.stat("/etc/passwd").st_uid:
                checker = str(self.install / "check.py")
                code = 'import runpy,sys,pathlib; m=runpy.run_path(sys.argv[1]); m["credential"](pathlib.Path("/etc/passwd"))'
                result = subprocess.run([sys.executable, "-I", "-B", "-c", code, checker], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("owned regular file", result.stderr)

    def test_atomic_replacement_preserves_new_credentials_and_exit(self):
        for shell in SHELLS:
            self.auth.unlink(missing_ok=True)
            self.auth.symlink_to(self.secret / "codex-A.json")
            old = (self.secret / "codex-A.json").read_bytes()
            result = self.run_as(shell, ["A", "exec"], {"FAKE_MODE": "rename", "FAKE_EXIT": "17"})
            self.assertEqual(result.returncode, 17)
            self.assertIn("preserve it and recover", result.stderr)
            self.assertFalse(self.auth.is_symlink())
            newest = self.auth.read_bytes()
            self.assertTrue(json.loads(newest)["tokens"]["access_token"] == TOKENS[2], "newest credentials lost (values hidden)")
            self.rejected(shell, ["B"])
            self.assertTrue(self.auth.read_bytes() == newest, "newest credentials overwritten (values hidden)")
            self.assertTrue((self.secret / "codex-A.json").read_bytes() == old, "old account rewritten (values hidden)")
            self.auth.unlink()

    def test_local_policy_and_provider_refusals(self):
        cfg = self.codex / "config.toml"
        for shell in SHELLS:
            for content in ('model_provider="other"\n', 'forced_login_method="api"\n',
                            'forced_chatgpt_workspace_id="restricted"\n',
                            '[model_providers.openai]\nenv_key="OTHER_KEY"\n',
                            'chatgpt_base_url="https://example.invalid"\n',
                            'openai_base_url="https://example.invalid"\n'):
                cfg.write_text(content)
                self.rejected(shell, ["A"])
                self.assertEqual(cfg.read_text(), content)
            cfg.write_text('model="unchanged-model"\n')
            project_cfg = self.cwd / ".codex/config.toml"
            project_cfg.parent.mkdir(exist_ok=True)
            project_cfg.write_text('model_provider="project-provider"\n')
            self.rejected(shell, ["A"])
            project_cfg.unlink()

    def test_executable_parent_state_and_command_bypass(self):
        (self.home / ".aliases").write_text("")
        for shell in SHELLS:
            setup = 'umask 022; codex() { return 99; }; alias codex="false"; '
            if Path(shell).name == "bash":
                setup += 'export -f codex; '
            result = self.run_as(shell, ["A", "exec", "login"], setup=setup)
            self.assertEqual(result.returncode, 0, result.stderr)
            flags = ["-f"] if Path(shell).name == "zsh" else ["--noprofile", "--norc"]
            command = [shell, *flags, "-c", 'umask 022; before=$(umask); command codex-as A --help >/dev/null 2>/dev/null; '
                       '[ "$(umask)" = "$before" ] && [ "$PWD" = "$1" ] && [ "${CODEX_HOME-unset}" = unset ]',
                       "state", str(self.cwd)]
            result = subprocess.run(command, cwd=self.cwd, env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            alias_file = ROOT / ("common/zsh/.zsh_aliases" if Path(shell).name == "zsh" else "common/bash/.bash_aliases")
            command = [shell, *flags, "-c", '. "$1"; . "$1"; codex-as --help', "alias", str(alias_file)]
            result = subprocess.run(command, cwd=self.cwd, env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("NO concurrency protection", result.stdout)

    def test_tty_fds_are_preserved(self):
        for shell in SHELLS:
            master, slave = pty.openpty()
            flags = ["-f"] if Path(shell).name == "zsh" else ["--noprofile", "--norc"]
            command = [shell, *flags, "-c", 'command codex-as A', "tty"]
            process = subprocess.Popen(command, cwd=self.cwd, env=self.env, stdin=slave, stdout=slave, stderr=slave)
            os.close(slave)
            try:
                os.write(master, b"terminal input\n\x04")
                output = bytearray()
                while process.poll() is None:
                    if select.select([master], [], [], 1)[0]:
                        try:
                            output.extend(os.read(master, 4096))
                        except OSError:
                            break
                self.assertEqual(process.wait(timeout=10), 0)
                record = json.loads(self.record.read_text())
                self.assertEqual(record["tty"], [True, True, True])
                self.assertEqual(record["stdin"], "terminal input\n")
            finally:
                os.close(master)
                if process.poll() is None:
                    process.kill()  # Only this test's fake process, never real Codex.
                    process.wait()


if __name__ == "__main__":
    for name in ("bash", "zsh"):
        print(f"shell={name}: {'installed, testing' if shutil.which(name) else 'SKIP (not installed)'}", flush=True)
    unittest.main(verbosity=2)
