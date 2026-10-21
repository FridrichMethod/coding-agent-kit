"""Kit configuration, launcher location and metadata queries.

Loader cases run in process. Launcher cases run the real scripts from kit
copies in a temporary HOME, with fake codex and claude that only record.
"""

import base64
from contextlib import contextmanager
import errno
import grp
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_codex_as import ENV_CONFLICTS, KIT_KNOBS


ROOT = Path(__file__).resolve().parents[1]
URL = "https://github.com/FridrichMethod/coding-agent-kit/blob/main/docs/account-launchers.md"
CONFIGURATION = ("Configuration: ${XDG_CONFIG_HOME:-~/.config}/coding-agent-kit/config.toml; "
                 "--show-config prints the effective values.")
TOOLS = ("codex-as", "claude-as")
CLAUDE_TOKEN = "sk-ant-oat01-" + "fabricated-config-claude-" * 3
DEFAULTS_FILE = 'codex = "codex"\nclaude = "claude"\ncodex_home = "~/.codex"\nsecrets_dir = "~/.secrets"\n'
# Records argv and environment, never a token; one JSON line per run.
FAKE = r'''
import hashlib, json, os, sys
if sys.argv[1:] == ["--version"]:
    print(VERSION)
    sys.exit(0)
env = dict(os.environ)
token = env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
record = {"argv": sys.argv, "env": env, "cwd": os.getcwd(), "umask": os.umask(0o077),
          "token": token and hashlib.sha256(token.encode()).hexdigest()}
with open(os.environ["FAKE_RECORD"], "a") as stream:
    stream.write(json.dumps(record) + "\n")
print("stdout sentinel")
'''
PACKAGES = (  # dotfiles' coding-agent-kit package path (from common/) and kit path, as dotfiles links them
    ("coding-agent-kit/.local/bin/codex-as", "bin/codex-as"),
    *((f"coding-agent-kit/.local/lib/codex-as/{name}.py", f"lib/codex-as/{name}.py")
      for name in ("launch", "check", "layout", "handoff")),
    ("coding-agent-kit/.local/share/bash-completion/completions/codex-as", "completions/bash/codex-as"),
    ("coding-agent-kit/.oh-my-zsh/custom/completions/_codex-as", "completions/zsh/_codex-as"),
    ("coding-agent-kit/.local/bin/claude-as", "bin/claude-as"),
    ("coding-agent-kit/.local/lib/claude-as/launch.py", "lib/claude-as/launch.py"),
    ("coding-agent-kit/.local/share/bash-completion/completions/claude-as", "completions/bash/claude-as"),
    ("coding-agent-kit/.oh-my-zsh/custom/completions/_claude-as", "completions/zsh/_claude-as"),
)

spec = importlib.util.spec_from_file_location("kit_config_under_test", ROOT / "lib/coding-agent-kit/config.py")
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


def private_group(gid):
    """The documented rule: the user's login group in the password database, named after the user,
    with no other members, neither listed nor accounts whose primary group it is."""
    me = pwd.getpwuid(os.getuid())
    try:
        group = grp.getgrgid(gid)
    except KeyError:
        return False
    return (gid == me.pw_gid and group.gr_name == me.pw_name and set(group.gr_mem) <= {me.pw_name}
            and not any(entry.pw_gid == gid and entry.pw_uid != os.getuid() for entry in pwd.getpwall()))


def owned_by(uid, real, only=None):
    """A stand-in for os.fstat or os.lstat that reports UID as the owner (of ONLY, when given)."""
    def fake(target, *args, **kwargs):
        info = real(target, *args, **kwargs)
        if only is not None and Path(target) != only:
            return info
        values = list(info[:10])
        values[4] = uid
        return os.stat_result(values)
    return fake


@contextmanager
def upg(gid, members=(), primary=(), login=None):
    """GID as the fixture user's own group, with these listed members and these other uids
    whose primary group it is; the user's login group is LOGIN, by default GID."""
    user = pwd.struct_passwd(("fixture-user", "x", os.getuid(), gid if login is None else login, "", "/", "/bin/sh"))
    others = [pwd.struct_passwd((f"other-{uid}", "x", uid, gid, "", "/", "/bin/sh")) for uid in primary]
    group = grp.struct_group(("fixture-user", "x", gid, list(members)))
    with patch.object(config.pwd, "getpwuid", return_value=user), \
         patch.object(config.pwd, "getpwall", return_value=[user, *others]), \
         patch.object(config.grp, "getgrgid", return_value=group):
        yield


def shared_group(gid):
    """A patch that makes GID a shared group with another member."""
    return patch.object(config.grp, "getgrgid", return_value=grp.struct_group(("staff", "x", gid, ["colleague"])))


def install(target, parts=("bin", "lib", "docs")):
    """A kit copy: what a checkout provides."""
    for part in parts:
        shutil.copytree(ROOT / part, target / part)
    return target


def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text) if isinstance(text, bytes) else path.write_text(text)
    path.chmod(mode)
    return path


def snapshot(top, skip=()):
    """Every path below TOP: type, mode, link text and content hash, without following links."""
    result = {}
    for directory, names, files in os.walk(top):
        names[:] = [name for name in names if os.path.relpath(os.path.join(directory, name), top) not in skip]
        for name in (*names, *files):
            path = Path(directory, name)
            info = path.lstat()
            entry = [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)]
            if path.is_symlink():
                entry.append(os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                entry.append(hashlib.sha256(path.read_bytes()).hexdigest())
            result[str(path.relative_to(top))] = entry
    return result


class Loader(unittest.TestCase):
    def setUp(self):
        # The directories the cases create are checked too; keep them independent of the caller's umask.
        self.addCleanup(os.umask, os.umask(0o022))
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-config-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home with spaces"
        self.home.mkdir(mode=0o700)
        self.file = self.home / ".config/coding-agent-kit/config.toml"
        self.environ = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    def load(self, **env):
        with patch.dict(os.environ, self.environ | env, clear=True):
            return config.load()

    def fails(self, **env):
        with self.assertRaises(config.ConfigError) as caught:
            self.load(**env)
        message = str(caught.exception)
        self.assertNotIn("\n", message)
        return message

    def test_missing_file_gives_the_defaults(self):
        loaded = self.load()
        self.assertEqual((loaded.file, loaded.source, loaded.warnings), (self.file, "not found; defaults", ()))
        self.assertEqual((loaded.codex, loaded.claude), ("codex", "claude"))
        # The same Path spellings as the launchers before configuration: HOME / ".codex".
        self.assertEqual(str(loaded.codex_home), str(Path(str(self.home)) / ".codex"))
        self.assertEqual(str(loaded.secrets_dir), str(Path(str(self.home)) / ".secrets"))
        self.assertEqual(loaded.home, self.home)

    def test_explicit_defaults_equal_no_file(self):
        missing = self.load()
        for text in (DEFAULTS_FILE, DEFAULTS_FILE.replace('"~/', '"~//').replace('" ', '/" '),
                     f'codex_home = "{self.home}//.codex/"\nsecrets_dir = "{self.home}/./.secrets"\n'):
            with self.subTest(text=text):
                write(self.file, text)
                found = self.load()
                self.assertEqual(found.source, "found")
                for key in ("home", "codex", "claude", "codex_home", "secrets_dir", "warnings"):
                    self.assertEqual(str(getattr(found, key)), str(getattr(missing, key)), key)

    def test_location_precedence(self):
        xdg = self.base / "xdg config"
        explicit = write(self.base / "explicit.toml", 'codex = "from-explicit"\n')
        write(xdg / "coding-agent-kit/config.toml", 'codex = "from-xdg"\n')
        write(self.file, 'codex = "from-home"\n')
        self.assertEqual(self.load(XDG_CONFIG_HOME=str(xdg)).codex, "from-xdg")
        # A relative XDG_CONFIG_HOME is ignored, as the XDG specification says.
        for value in ("relative/xdg", ""):
            self.assertEqual(self.load(XDG_CONFIG_HOME=value).codex, "from-home")
        loaded = self.load(XDG_CONFIG_HOME=str(xdg), CODING_AGENT_KIT_CONFIG=str(explicit))
        self.assertEqual((loaded.codex, loaded.file, loaded.source),
                         ("from-explicit", explicit, "from CODING_AGENT_KIT_CONFIG"))
        self.assertEqual(self.load(CODING_AGENT_KIT_CONFIG="").codex, "from-home")

    def test_explicit_file_must_be_absolute_and_exist(self):
        self.assertEqual(self.fails(CODING_AGENT_KIT_CONFIG="relative.toml"),
                         "relative.toml: CODING_AGENT_KIT_CONFIG must be an absolute path.")
        missing = self.base / "missing.toml"
        self.assertEqual(self.fails(CODING_AGENT_KIT_CONFIG=str(missing)),
                         f"{missing}: not found (CODING_AGENT_KIT_CONFIG).")
        self.assertEqual(self.fails(CODING_AGENT_KIT_CONFIG=str(self.base)), f"{self.base}: not a regular file.")

    def test_symlinks_resolving_or_dangling(self):
        target = write(self.base / "linked target.toml", 'claude = "claude-limited"\n')
        self.file.parent.mkdir(parents=True)
        self.file.symlink_to(target)
        self.assertEqual((self.load().claude, self.load().source), ("claude-limited", "found"))
        target.unlink()
        self.assertEqual(self.fails(), f"{self.file}: dangling symlink.")
        explicit = self.base / "explicit link.toml"
        explicit.symlink_to(self.base / "nowhere.toml")
        self.assertEqual(self.fails(CODING_AGENT_KIT_CONFIG=str(explicit)), f"{explicit}: dangling symlink.")
        # A dangling directory on the way, like a stowed ~/.config whose target is gone.
        shutil.rmtree(self.home / ".config")
        (self.home / ".config").symlink_to(self.base / "gone", target_is_directory=True)
        self.assertEqual(self.fails(), f"{self.file}: dangling symlink {self.home / '.config'}.")

    def test_umask_matrix(self):
        for umask in (0o002, 0o022, 0o077):
            with self.subTest(umask=oct(umask)):
                self.file.unlink(missing_ok=True)
                self.file.parent.mkdir(parents=True, exist_ok=True)
                previous = os.umask(umask)
                try:
                    self.file.write_text('codex_home = "~/elsewhere"\n')
                finally:
                    os.umask(previous)
                info = self.file.stat()
                self.assertEqual(stat.S_IMODE(info.st_mode), 0o666 & ~umask)
                if info.st_mode & stat.S_IWGRP and not private_group(info.st_gid):
                    self.assertEqual(self.fails(), f"{self.file}: group-writable, and the group is not your private group.")
                else:
                    self.assertEqual(self.load().codex_home, self.home / "elsewhere")

    def test_group_write_only_for_the_private_group(self):
        write(self.file, 'codex = "codex"\n', 0o660)
        gid = self.file.stat().st_gid
        for name, members, login_gid, accepted in (
                ("fixture-user", [], gid, True), ("fixture-user", ["fixture-user"], gid, True),
                ("fixture-user", ["fixture-user", "colleague"], gid, False), ("staff", [], gid, False),
                ("fixture-user", [], gid + 1, False), (None, [], gid, False)):
            with self.subTest(name=name, members=members, login=login_gid == gid):
                group = grp.struct_group((name, "x", gid, members)) if name else None
                user = SimpleNamespace(pw_name="fixture-user", pw_gid=login_gid)
                with patch.object(config.pwd, "getpwuid", return_value=user), \
                     patch.object(config.pwd, "getpwall", return_value=[]), \
                     patch.object(config.grp, "getgrgid", return_value=group, side_effect=None if name else KeyError(gid)):
                    if accepted:
                        self.assertEqual(self.load().source, "found")
                    else:
                        self.assertIn("group-writable", self.fails())
        # newgrp and sg change the real gid, not the login group in the password database, nor who
        # is in the group: the login group decides, whatever id -g says in that shell.
        for real_gid, login_gid, accepted in ((gid + 1, gid, True), (gid, gid + 1, False)):
            with self.subTest(real=real_gid == gid, login=login_gid == gid), upg(gid, login=login_gid), \
                 patch.object(config.os, "getgid", return_value=real_gid):
                if accepted:
                    self.assertEqual(self.load().source, "found")
                else:
                    self.assertEqual(self.fails(), f"{self.file}: group-writable, and the group is not your private group.")
        # An account whose primary group it is never shows in the member list (OpenSSH checks
        # the password database the same way); another entry with your own uid is you.
        for primary, accepted in (((os.getuid(),), True), ((os.getuid() + 1,), False)):
            with self.subTest(primary=primary), upg(gid, primary=primary):
                if accepted:
                    self.assertEqual(self.load().source, "found")
                else:
                    self.assertEqual(self.fails(), f"{self.file}: group-writable, and the group is not your private group.")
        self.file.chmod(0o640)  # Group-readable is fine whatever the group.
        with shared_group(gid):
            self.assertEqual(self.load().source, "found")

    def test_group_write_with_an_access_acl_is_refused(self):
        acl = f"{self.file}: group-writable with an access ACL; remove the ACL (setfacl -b) or the group write (chmod g-w)."
        write(self.file, 'codex = "codex"\n', 0o664)
        gid = self.file.stat().st_gid
        # Whatever the host: os.listxattr names the ACL, is missing (macOS) or the file system has no xattrs.
        for listing, expected in ((["user.other"], None), (["system.posix_acl_access"], acl), (["system.nfs4_acl"], acl),
                                  (AttributeError("listxattr"), None), (OSError(errno.ENOTSUP, "Not supported"), None),
                                  (OSError(errno.EIO, "Input/output error"), f"{self.file}: cannot be checked (Input/output error).")):
            raises = isinstance(listing, Exception)
            with self.subTest(listing=listing), upg(gid), \
                 patch.object(config.os, "listxattr", create=True, side_effect=listing if raises else None,
                              return_value=None if raises else listing):
                if expected is None:
                    self.assertEqual(self.load().source, "found")
                else:
                    self.assertEqual(self.fails(), expected)
        self.file.chmod(0o644)  # Without group write the ACL mask grants no one write access.
        with upg(gid), patch.object(config.os, "listxattr", create=True, return_value=["system.posix_acl_access"]):
            self.assertEqual(self.load().source, "found")

    @unittest.skipUnless(shutil.which("setfacl") and hasattr(os, "listxattr"), "needs setfacl and os.listxattr")
    def test_real_access_acls_are_refused(self):
        acl = "group-writable with an access ACL; remove the ACL (setfacl -b) or the group write (chmod g-w)."
        write(self.file, 'codex = "codex"\n', 0o644)
        gid = self.file.stat().st_gid

        def setfacl(*args):
            result = subprocess.run(["setfacl", *map(str, args)], capture_output=True, text=True)
            if result.returncode:
                self.skipTest("no ACL support here: " + result.stderr.strip())

        # A named group entry on a 0644 file: the group bits now show the mask, rw.
        setfacl("-m", "g:0:rw", self.file)
        self.assertTrue(self.file.stat().st_mode & stat.S_IWGRP)
        with upg(gid):
            self.assertEqual(self.fails(), f"{self.file}: {acl}")
            setfacl("-b", self.file)
            self.file.chmod(0o664)  # the plain user private group file still loads
            self.assertEqual(self.load().source, "found")
            # A default ACL on the directory gives every new file such an entry, even under umask 022.
            self.file.unlink()
            setfacl("-d", "-m", "g:0:rwX", self.file.parent)
            self.file.write_text('codex = "codex"\n')
            self.assertEqual(self.fails(), f"{self.file}: {acl}")
            # An access ACL on a directory on the way lets that group replace the file.
            setfacl("-k", self.file.parent)
            setfacl("-b", self.file)
            self.file.chmod(0o644)
            setfacl("-m", "g:0:rwx", self.file.parent)
            self.assertEqual(self.fails(), f"{self.file}: directory {self.file.parent}: {acl}")

    def test_other_writable_and_foreign_owner_are_refused(self):
        write(self.file, 'codex = "codex"\n', 0o606)
        self.assertEqual(self.fails(), f"{self.file}: writable by other users.")
        self.file.chmod(0o644)
        with patch.object(config.os, "fstat", side_effect=owned_by(os.getuid() + 1, os.fstat)):
            self.assertEqual(self.fails(), f"{self.file}: owned by another user.")
        with patch.object(config.os, "fstat", side_effect=owned_by(0, os.fstat)):
            self.assertEqual(self.load().source, "found")

    def test_a_file_in_the_way_means_no_configuration(self):
        # No configuration file can exist below a regular file (ENOTDIR): the defaults, as without one.
        write(self.home / ".config", "not a directory\n")
        self.assertEqual((self.load().source, self.load().codex_home), ("not found; defaults", self.home / ".codex"))
        xdg = write(self.base / "xdg file", "not a directory\n")
        self.assertEqual(self.load(XDG_CONFIG_HOME=str(xdg)).source, "not found; defaults")
        explicit = xdg / "config.toml"
        self.assertEqual(self.fails(CODING_AGENT_KIT_CONFIG=str(explicit)), f"{explicit}: not found (CODING_AGENT_KIT_CONFIG).")

    def test_other_open_errors_fail_closed(self):
        # A file may be there: never the defaults, which would ignore a configured location.
        write(self.file, 'codex_home = "~/elsewhere"\n')
        if os.geteuid() != 0:
            for locked in (self.file, self.home / ".config"):
                with self.subTest(locked=locked.name):
                    mode = stat.S_IMODE(locked.stat().st_mode)
                    locked.chmod(0)
                    try:
                        self.assertEqual(self.fails(), f"{self.file}: cannot be opened (Permission denied).")
                    finally:
                        locked.chmod(mode)
        self.file.unlink()
        self.file.symlink_to(self.file.name)
        self.assertEqual(self.fails(), f"{self.file}: cannot be opened (Too many levels of symbolic links).")

    def test_a_fifo_is_refused_without_blocking(self):
        self.file.parent.mkdir(parents=True)
        os.mkfifo(self.file)
        # A blocking open waits for a writer forever: load in a child with a deadline.
        code = ("import importlib.util, sys\n"
                "spec = importlib.util.spec_from_file_location('config', sys.argv[1])\n"
                "config = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(config)\n"
                "try:\n    config.load()\nexcept config.ConfigError as error:\n    print(error)\n")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", code, ROOT / "lib/coding-agent-kit/config.py"],
                                env=self.environ, capture_output=True, text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout), (0, f"{self.file}: not a regular file.\n"), result.stderr)

    def test_directories_on_the_way_must_not_let_others_replace_the_file(self):
        write(self.file, 'codex = "codex"\n')
        directory, gid = self.file.parent, self.file.stat().st_gid
        shared = f"{self.file}: directory {directory}: group-writable, and the group is not your private group."
        for mode, expected in ((0o770, shared), (0o707, f"{self.file}: directory {directory}: writable by other users."),
                               (0o1777, None), (0o750, None)):
            with self.subTest(mode=oct(mode)), shared_group(gid):
                directory.chmod(mode)
                if expected is None:  # sticky like /tmp, or not writable by others
                    self.assertEqual(self.load().source, "found")
                else:
                    self.assertEqual(self.fails(), expected)
        directory.chmod(0o755)
        # Up to and including HOME; its parent is not checked for a file below HOME.
        for place, checked in ((self.home, True), (self.base, False)):
            with self.subTest(place=place.name), shared_group(gid):
                mode = stat.S_IMODE(place.stat().st_mode)
                place.chmod(0o770)
                try:
                    if checked:
                        self.assertEqual(self.fails(), shared.replace(f"directory {directory}:", f"directory {place}:"))
                    else:
                        self.assertEqual(self.load().source, "found")
                finally:
                    place.chmod(mode)
        # Ubuntu: umask 002 and the user private group make 0775 directories and a 0664 file.
        for place in (self.home / ".config", directory):
            place.chmod(0o775)
        self.file.chmod(0o664)
        with upg(gid):
            self.assertEqual(self.load().source, "found")
        with upg(gid, primary=(os.getuid() + 1,)):
            self.assertEqual(self.fails(), f"{self.file}: group-writable, and the group is not your private group.")
        self.file.chmod(0o644)
        with upg(gid, primary=(os.getuid() + 1,)):
            self.assertEqual(self.fails(), shared)
        # A newgrp or sg shell has another real gid; the login group in the password database still
        # decides, for the file and the directories alike.
        for mode, refused in ((0o664, f"{self.file}: group-writable, and the group is not your private group."),
                              (0o644, shared)):
            self.file.chmod(mode)
            with self.subTest(file=oct(mode)):
                with upg(gid), patch.object(config.os, "getgid", return_value=gid + 1):
                    self.assertEqual(self.load().source, "found")
                with upg(gid, login=gid + 1), patch.object(config.os, "getgid", return_value=gid):
                    self.assertEqual(self.fails(), refused)
        # Ownership alone from here: without group write, the host's own groups (macOS users share
        # staff) cannot decide the outcome.
        for place in (self.home / ".config", directory):
            place.chmod(0o755)
        with patch.object(config.os, "lstat", side_effect=owned_by(os.getuid() + 1, os.lstat, directory)):
            self.assertEqual(self.fails(), f"{self.file}: directory {directory}: owned by another user.")
        with patch.object(config.os, "lstat", side_effect=owned_by(0, os.lstat, directory)):
            self.assertEqual(self.load().source, "found")

    def test_the_accounts_are_listed_once_per_load(self):
        # Listing every account can be slow on a directory service: once per run, not per place.
        write(self.file, 'codex = "codex"\n', 0o664)
        for place in (self.home / ".config", self.file.parent):
            place.chmod(0o775)  # Ubuntu: three group-writable places in the user private group
        with upg(self.file.stat().st_gid):
            for run in (1, 2):
                self.assertEqual(self.load().source, "found")
                self.assertEqual(config.pwd.getpwall.call_count, run)  # and never kept across loads

    def test_a_symlinked_home_stops_the_walk_at_its_resolved_path(self):
        # HOME -> groups/lab/u, as with NFS or automount links: the shared directories above the
        # real home are not checked, whichever spelling the location uses.
        real = self.base / "groups/lab/u"
        write(real / ".config/coding-agent-kit/config.toml", 'codex = "from-real-home"\n', 0o644)
        real.chmod(0o700)
        (self.base / "groups/lab").chmod(0o770)
        home = self.base / "home link"
        home.symlink_to(real, target_is_directory=True)
        with shared_group(real.stat().st_gid):
            for env in ({}, {"XDG_CONFIG_HOME": str(real / ".config")}):
                with self.subTest(xdg=bool(env)):
                    self.assertEqual(self.load(HOME=str(home), **env).codex, "from-real-home")
        # The HOME link itself is on the way as written, so it must be yours or root's.
        with patch.object(config.os, "lstat", side_effect=owned_by(os.getuid() + 1, os.lstat, home)):
            self.assertEqual(self.fails(HOME=str(home)),
                             f"{home}/.config/coding-agent-kit/config.toml: symlink {home}: owned by another user.")

    def test_symlinks_and_resolved_directories_on_the_way(self):
        # A stowed file: config.toml -> a checkout you own; the checkout's directories count too.
        checkout = self.home / "dotfiles/kit"
        target = write(checkout / "config.toml", 'codex = "from-checkout"\n', 0o644)
        self.file.parent.mkdir(parents=True)
        self.file.symlink_to(os.path.relpath(target, self.file.parent))
        gid = target.stat().st_gid
        self.assertEqual(self.load().codex, "from-checkout")
        checkout.chmod(0o775)
        with upg(gid):
            self.assertEqual(self.load().codex, "from-checkout")
        with shared_group(gid):
            self.assertEqual(self.fails(), f"{self.file}: directory {checkout}: group-writable, and the group is not your private group.")
        checkout.chmod(0o755)
        # A symlink on the way must be yours or root's: whoever owns it chooses the file.
        xdg = self.home / "xdg"
        xdg.symlink_to(".config", target_is_directory=True)
        for link, env in ((self.file, {}), (xdg, {"XDG_CONFIG_HOME": str(xdg)})):
            path = xdg / "coding-agent-kit/config.toml" if env else self.file
            with self.subTest(link=link.name):
                with patch.object(config.os, "lstat", side_effect=owned_by(os.getuid() + 1, os.lstat, link)):
                    self.assertEqual(self.fails(**env), f"{path}: symlink {link}: owned by another user.")
                with patch.object(config.os, "lstat", side_effect=owned_by(0, os.lstat, link)):
                    self.assertEqual(self.load(**env).codex, "from-checkout")
        # Outside HOME every directory up to / counts, as written and as resolved.
        explicit = write(self.base / "shared/sub/explicit.toml", 'codex = "from-explicit"\n')
        alias = self.home / "alias"
        alias.symlink_to(explicit.parent, target_is_directory=True)
        for given in (explicit, alias / explicit.name):
            with self.subTest(given=str(given)):
                self.assertEqual(self.load(CODING_AGENT_KIT_CONFIG=str(given)).codex, "from-explicit")
                (self.base / "shared").chmod(0o770)
                try:
                    with shared_group(gid):
                        self.assertEqual(self.fails(CODING_AGENT_KIT_CONFIG=str(given)), f"{given}: directory "
                                         f"{self.base / 'shared'}: group-writable, and the group is not your private group.")
                finally:
                    (self.base / "shared").chmod(0o755)

    def test_size_limit_and_encoding(self):
        write(self.file, "#" * (64 * 1024 - 1) + "\n")
        self.assertEqual(self.load().source, "found")
        write(self.file, "#" * (64 * 1024) + "\n")
        self.assertEqual(self.fails(), f"{self.file}: larger than 64 KiB.")
        write(self.file, b'codex = "\xff"\n')
        self.assertEqual(self.fails(), f"{self.file}: not UTF-8 text.")

    def test_syntax_errors_name_the_position_never_the_contents(self):
        for text in ('codex = "fabricated-secret-value\n', 'fabricated-secret-value\n',
                     '[fabricated-secret-value]\n[fabricated-secret-value]\n', 'codex = "\x01fabricated-secret-value"\n'):
            with self.subTest(text=text):
                write(self.file, text)
                message = self.fails()
                self.assertRegex(message, r": invalid TOML at (line \d+, column \d+|end of document)\.$")
                self.assertNotIn("fabricated", message)

    def test_nesting_and_integers_past_the_parser_limits_are_invalid_toml(self):
        # RecursionError and the int digit-limit ValueError, both well under 64 KiB.
        for label, text in (("arrays", "x = " + "[" * 5000 + "]" * 5000), ("tables", "x = " + "{a=" * 3000 + "1" + "}" * 3000),
                            ("integer", "x = " + "7" * 5000)):
            with self.subTest(label):
                write(self.file, text + "\n")
                self.assertRegex(self.fails(), "^" + re.escape(f"{self.file}: invalid TOML") + r"( at line \d+, column \d+)?\.$")

    def test_unknown_keys_warn_and_continue(self):
        write(self.file, 'future_key = "x"\n"odd\\u001bkey" = 1\n[future_table]\nsecrets_dir = "~/elsewhere"\n'
                         'claude = "claude-limited"\n')
        loaded = self.load()
        self.assertEqual(loaded.warnings, (f"{self.file}: unknown key future_key ignored",
                                           f'{self.file}: unknown key "odd\\u001bkey" ignored',
                                           f"{self.file}: unknown key future_table ignored"))
        # The table swallows the following keys, which TOML assigns to it.
        self.assertEqual((loaded.claude, loaded.secrets_dir), ("claude", self.home / ".secrets"))

    def test_value_errors(self):
        cases = {
            'codex = 1': "codex: must be a string.",
            'codex_home = ["~/a"]': "codex_home: must be a string.",
            '[secrets_dir]': "secrets_dir: must be a string.",
            'claude = ""': "claude: must not be empty.",
            'secrets_dir = ""': "secrets_dir: must not be empty.",
            'codex_home = ".codex"': "codex_home: must be an absolute or ~/ path.",
            'codex_home = "~"': "codex_home: must be an absolute or ~/ path.",
            'secrets_dir = "~other/secrets"': "secrets_dir: must be an absolute or ~/ path.",
            'codex_home = "~/../codex"': "codex_home: must not contain a .. component.",
            'secrets_dir = "/srv/../secrets"': "secrets_dir: must not contain a .. component.",
        }
        command = "codex: must be a command name ([A-Za-z0-9._+-]+) or an absolute or ~/ path."
        for value in ("bin/codex", "./codex", "co dex", "codex;true", "~", "~other/codex", "$HOME/codex"):
            cases[f'codex = "{value}"'] = command
        cases['claude = "tools/claude"'] = command.replace("codex:", "claude:", 1)
        for text, reason in cases.items():
            with self.subTest(text=text):
                write(self.file, text + "\n")
                self.assertEqual(self.fails(), f"{self.file}: {reason}")

    def test_control_characters_are_refused(self):
        # They would reach the terminal raw in --show-config and in launch errors; NUL also breaks paths.
        # C1 too: a terminal may act on CSI (U+009B) or NEL (U+0085) as on ESC [ or a line break.
        # TOML takes them escaped and raw (the last case).
        for key in config.DEFAULTS:
            for escape in ("\\u001b]0;x\\u0007", "\\n", "\\t", "\\u0000", "\\u007f", "\\u0080", "\\u0085",
                           "\\u009b2J", "\\u009f", "\u009b31m"):
                with self.subTest(key=key, escape=escape):
                    write(self.file, f'{key} = "/opt/x{escape}y"\n')
                    self.assertEqual(self.fails(), f"{self.file}: {key}: must not contain control characters.")
        write(self.file, 'claude = "/opt/caf\\u00e9 \\u00a0/claude"\n')  # other characters stay allowed
        self.assertEqual(self.load().claude, "/opt/caf\u00e9 \u00a0/claude")

    def test_executables_are_names_or_absolute_paths(self):
        for value, expected in (("codex-beta.1+x_y", "codex-beta.1+x_y"), ("/opt/codex/bin/codex/", "/opt/codex/bin/codex"),
                                ("~/bin/codex", str(self.home / "bin/codex")), ("/opt/../codex", "/opt/../codex"),
                                ("//opt/codex", "/opt/codex"), ("///opt//codex", "/opt/codex")):
            with self.subTest(value=value):
                write(self.file, f'codex = "{value}"\n')
                self.assertEqual(self.load().codex, expected)

    def test_codex_home_and_secrets_dir_never_nest(self):
        for codex_home, secrets_dir, reason in (
                ("~/a", "~/a/", "secrets_dir: must differ from codex_home."),
                ("~/a", "~/a/secrets", "secrets_dir: must not be inside codex_home."),
                ("~/", "~/.secrets", "secrets_dir: must not be inside codex_home."),
                ("~/s/codex", "~/s", "codex_home: must not be inside secrets_dir."),
                ("/", "~/.secrets", "secrets_dir: must not be inside codex_home.")):
            with self.subTest(codex_home=codex_home, secrets_dir=secrets_dir):
                write(self.file, f'codex_home = "{codex_home}"\nsecrets_dir = "{secrets_dir}"\n')
                self.assertEqual(self.fails(), f"{self.file}: {reason}")
        write(self.file, 'codex_home = "~/a"\nsecrets_dir = "~/ab"\n')  # a prefix, not a parent
        self.assertEqual(self.load().secrets_dir, self.home / "ab")

    def test_a_leading_double_slash_is_the_root(self):
        # pathlib keeps exactly two leading slashes as their own root; Linux and macOS read them as /.
        for codex_home, secrets_dir, reason in (
                (f"/{self.home}/.codex", "~/.codex/secrets", "secrets_dir: must not be inside codex_home."),
                ("~/.codex", f"/{self.home}/.codex", "secrets_dir: must differ from codex_home."),
                ("~/.codex", f"//{self.home}/.codex/accounts", "secrets_dir: must not be inside codex_home."),
                (f"/{self.home}/s/codex", "~/s", "codex_home: must not be inside secrets_dir.")):
            with self.subTest(codex_home=codex_home, secrets_dir=secrets_dir):
                write(self.file, f'codex_home = "{codex_home}"\nsecrets_dir = "{secrets_dir}"\n')
                self.assertEqual(self.fails(), f"{self.file}: {reason}")
        write(self.file, 'codex_home = "//srv/x/"\nsecrets_dir = "///srv//y"\n')
        loaded = self.load()
        self.assertEqual((str(loaded.codex_home), str(loaded.secrets_dir)), ("/srv/x", "/srv/y"))
        # A HOME spelled with // keeps its spelling, as before configuration, and is still compared as /.
        write(self.file, f'secrets_dir = "{self.home}/.codex"\n')
        self.assertEqual(self.fails(HOME="/" + str(self.home)), f"/{self.file}: secrets_dir: must differ from codex_home.")
        self.file.unlink()
        self.assertEqual(str(self.load(HOME="/" + str(self.home)).codex_home), f"/{self.home}/.codex")

    def test_home_must_be_absolute(self):
        for value in (None, "", "relative/home"):
            with self.subTest(home=value):
                environ = {key: item for key, item in self.environ.items() if key != "HOME"}
                with patch.dict(os.environ, environ | ({} if value is None else {"HOME": value}), clear=True), \
                     self.assertRaises(config.ConfigError) as caught:
                    config.load()
                self.assertEqual(str(caught.exception), "HOME must be set to an absolute path.")

    def test_expand_uses_the_loaded_home(self):
        loaded = self.load()
        self.assertEqual(loaded.expand("~"), self.home)
        self.assertEqual(loaded.expand("~/project//x"), self.home / "project/x")
        self.assertEqual(loaded.expand("/abs"), Path("/abs"))
        self.assertEqual(loaded.expand("relative"), Path("relative"))

    def test_show_lists_each_tools_values(self):
        fakes = self.base / "fake bin"
        write(fakes / "codex", "#!/bin/sh\n", 0o700)
        with patch.dict(os.environ, self.environ | {"PATH": str(fakes)}, clear=True):
            loaded = config.load()
            self.assertEqual(loaded.show("codex-as").splitlines(), [
                f"config = {self.file} (not found; defaults)", f"codex = codex ({fakes / 'codex'})",
                f"codex_home = {self.home / '.codex'}", f"secrets_dir = {self.home / '.secrets'}"])
            self.assertEqual(loaded.show("claude-as").splitlines(), [
                f"config = {self.file} (not found; defaults)", "claude = claude (not found on PATH)",
                f"secrets_dir = {self.home / '.secrets'}"])
            write(self.file, f'claude = "{self.base}/missing claude"\n')
            self.assertIn(f"claude = {self.base}/missing claude (not an executable file)", config.load().show("claude-as"))


class Launchers(unittest.TestCase):
    """The real launchers from kit copies, in a temporary HOME with recording fakes."""

    def setUp(self):
        # The loader checks the directories on the way to a configuration file.
        self.addCleanup(os.umask, os.umask(0o022))
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-launcher-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.kit = install(self.base / "kit with spaces")
        self.home = self.base / "home with spaces"
        self.make_home(self.home)
        self.fakes = self.base / "fake bin"
        for name, version in (("codex", "codex-cli 0.163.0"), ("claude", "2.1.296 (Claude Code)")):
            write(self.fakes / name, f"#!{sys.executable}\nVERSION = {version!r}\n" + FAKE, 0o700)
        self.record = self.base / "record.jsonl"
        self.env = {key: value for key, value in os.environ.items() if not key.startswith(("ANTHROPIC_", "CLAUDE_"))}
        for name in (*KIT_KNOBS, *ENV_CONFLICTS, "CODEX_HOME", "CODEX_SQLITE_HOME", "PYTHONPATH", "BASH_ENV", "ENV", "ZDOTDIR"):
            self.env.pop(name, None)
        self.env.update(HOME=str(self.home), FAKE_RECORD=str(self.record),
                        PATH=os.pathsep.join((str(self.fakes), self.env.get("PATH", "/usr/bin:/bin"))))

    def make_home(self, home):
        home.mkdir(mode=0o700)
        (home / ".codex").mkdir(mode=0o700)
        write(home / ".codex/config.toml", 'model = "fixture-model"\n')
        (home / ".secrets").mkdir(mode=0o700)
        self.credentials(home / ".secrets", "A")
        write(home / ".secrets/claude-work", CLAUDE_TOKEN + "\n")

    @staticmethod
    def credentials(secrets, name):
        payload = base64.urlsafe_b64encode(json.dumps({"sub": "fixture-" + name}).encode()).decode().rstrip("=")
        write(secrets / f"codex-{name}.json", json.dumps({
            "auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-10-01T00:00:00Z",
            "tokens": {"id_token": f"fake.{payload}.fake", "access_token": "fabricated-access-" + name,
                       "refresh_token": "fabricated-refresh-" + name, "account_id": name}}))

    def linked_home(self, home):
        """A dotfiles host: HOME/X -> (Stow, relative) package X -> (relative) kit file; two hops.
        The kit is the checkout at the root of dotfiles' own coding-agent-kit package."""
        self.make_home(home)
        common = home / "dotfiles/common"
        kit = install(common / "coding-agent-kit/coding-agent-kit", ("bin", "lib", "docs", "completions"))
        for package, target in PACKAGES:
            link = common / package
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(os.path.relpath(kit / target, link.parent))
            stowed = home / package.split("/", 1)[1]
            stowed.parent.mkdir(parents=True, exist_ok=True)
            stowed.symlink_to(os.path.relpath(link, stowed.parent))
        return kit

    def run_tool(self, command, env=None, cwd=None):
        result = subprocess.run([str(part) for part in command], cwd=cwd or self.base, env=self.env | (env or {}),
                                stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=20,
                                start_new_session=True)
        for secret in (CLAUDE_TOKEN, "fabricated-access-"):
            self.assertNotIn(secret, result.stdout + result.stderr)
        return result

    def records(self):
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]

    def configure(self, text, home=None):
        return write((home or self.home) / ".config/coding-agent-kit/config.toml", text)

    def assert_resolves(self, command, root, cwd=None, env=None):
        tool = next(name for name in TOOLS if name in " ".join(map(str, command)))
        result = self.run_tool([*command, "--help"], env, cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-2:], [CONFIGURATION, f"Details and manual repair: {root}/docs/account-launchers.md"])
        result = self.run_tool([*command, "--show-config"], env, cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[0],
                         f"config = {self.home}/.config/coding-agent-kit/config.toml (not found; defaults)")
        self.assertIn(f"{'codex' if tool == 'codex-as' else 'claude'} = ", result.stdout)

    def test_help_names_the_kit_documentation_or_the_url(self):
        lone = self.base / "lone"
        for tool in TOOLS:
            with self.subTest(tool=tool):
                result = self.run_tool([self.kit / "bin" / tool, "--help"])
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                self.assertTrue(result.stdout.startswith(f"Usage: {tool} NAME ["))
                self.assertEqual(result.stdout.splitlines()[-1],
                                 f"Details and manual repair: {self.kit}/docs/account-launchers.md")
                self.assertIn(CONFIGURATION, result.stdout)
                self.assertIn("<secrets_dir>/", result.stdout)
                self.assertIn("(default ~/.secrets", result.stdout)
                self.assertEqual(self.run_tool([self.kit / "bin" / tool]).stdout, result.stdout)
                self.assertEqual(self.run_tool([self.kit / "bin" / tool]).returncode, 2)
                # A lone copy, a broken kit or a kit without docs answers with the URL.
                write(lone / "bin" / tool, (ROOT / "bin" / tool).read_text(), 0o700)
                result = self.run_tool([lone / "bin" / tool, "--help"])
                self.assertEqual((result.returncode, result.stdout.splitlines()[-1]), (0, f"Details and manual repair: {URL}"))
                loop = self.base / f"{tool} loop"
                loop.symlink_to(loop)
                shell = "bash" if tool == "codex-as" else "sh"
                result = self.run_tool([shell, "-c", (ROOT / "bin" / tool).read_text(), loop, "--help"])
                self.assertEqual((result.returncode, result.stdout.splitlines()[-1], result.stderr),
                                 (0, f"Details and manual repair: {URL}", ""))
        self.assertIn("Concurrent accounts", self.run_tool([self.kit / "bin/codex-as", "--help"]).stdout)

    def test_without_python3_help_works_and_launches_fail_once(self):
        tools = self.base / "no python"
        tools.mkdir()
        for name in ("dirname", "readlink"):
            (tools / name).symlink_to(shutil.which(name))
        for tool in TOOLS:
            with self.subTest(tool=tool):
                result = self.run_tool([self.kit / "bin" / tool, "--help"], {"PATH": str(tools)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines()[-1],
                                 f"Details and manual repair: {self.kit}/docs/account-launchers.md")
        # The launchers resolve their root with dirname and readlink first, then need python3.
        before = snapshot(self.home)
        required = "codex-as: Python 3.11+ is required for local JSON and file checks.\n"
        for tool, args, code in (("codex-as", ["A"], 1), ("codex-as", ["--list-accounts"], 1), ("claude-as", ["work"], 127)):
            with self.subTest(tool=tool, args=args):
                result = self.run_tool([self.kit / "bin" / tool, *args], {"PATH": str(tools)})
                self.assertEqual((result.returncode, result.stdout), (code, ""), result.stderr)
                if tool == "codex-as":
                    self.assertEqual(result.stderr, required)
                else:  # as in 0.1.0: the shell's own exec error, one line
                    self.assertRegex(result.stderr, r"\A[^\n]*python3: not found\n\Z")
        self.assertEqual(snapshot(self.home), before, "a launch without python3 wrote to HOME")
        self.assertEqual(self.records(), [])

    def test_resolution_cases(self):
        linked = self.base / "linked home"
        dotfiles_kit = self.linked_home(linked)
        alias = self.base / "alias dir"
        alias.symlink_to(self.base, target_is_directory=True)  # like macOS /var -> /private/var
        # A second hop through a symlinked ~/.local: its ../.. must be resolved physically (cd -P);
        # a logical cd would take them lexically, into the decoy directory below the link.
        hop = self.base / "hop"
        hop_kit = install(hop / "store/kit")
        (hop / "home/dot/bin").mkdir(parents=True)  # the decoy
        (hop / "home/.local").symlink_to("../store/local", target_is_directory=True)
        for top, target in (("store/local/bin", "../../dot/bin"), ("store/dot/bin", "../../kit/bin")):
            (hop / top).mkdir(parents=True)
            for tool in TOOLS:
                (hop / top / tool).symlink_to(f"{target}/{tool}")
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        interpreters = {"codex-as": [["bash"]], "claude-as": [["sh"], ["/bin/sh"]]}
        if shutil.which("dash"):
            interpreters["claude-as"].append(["dash"])
        for tool in TOOLS:
            bin_dir = self.kit / "bin"
            cases = [
                ("direct", [bin_dir / tool], None, None, self.kit),
                ("PATH lookup", ["bash", "-c", f'{tool} "$@"', "path"], None,
                 {"PATH": f"{bin_dir}{os.pathsep}{self.env['PATH']}"}, self.kit),
                ("relative", [f"./bin/{tool}"], self.kit, None, self.kit),
                ("relative upwards", [f"../kit with spaces/bin/{tool}"], elsewhere, None, self.kit),
                ("two-hop links", [linked / ".local/bin" / tool], None, None, dotfiles_kit),
                ("symlinked directory", [alias / "kit with spaces/bin" / tool], None, None, self.kit),
                ("second hop through a symlinked directory", [hop / "home/.local/bin" / tool], None, None, hop_kit),
            ]
            for interpreter in interpreters[tool]:
                cases.append((f"{interpreter[0]} path", [*interpreter, bin_dir / tool], None, None, self.kit))
                cases.append((f"{interpreter[0]} name in cwd", [*interpreter, tool], bin_dir, None, self.kit))
                cases.append((f"{interpreter[0]} two-hop links", [*interpreter, linked / ".local/bin" / tool],
                              None, None, dotfiles_kit))
                cases.append((f"{interpreter[0]} second hop through a symlinked directory",
                              [*interpreter, hop / "home/.local/bin" / tool], None, None, hop_kit))
            for label, command, cwd, env, root in cases:
                with self.subTest(tool=tool, case=label):
                    self.assert_resolves(command, root, cwd, env)
        self.assertEqual(self.records(), [], "resolution ran a native CLI")

    def chain(self, name, length, target):
        """LENGTH links, each to the next, the last to TARGET; returns the first."""
        links = [self.base / "chain" / f"{name}-{index}" for index in range(length)]
        links[0].parent.mkdir(exist_ok=True)
        for link, following in zip(links, [*links[1:], target]):
            link.symlink_to(following.name if following.parent == link.parent else following)
        return links[0]

    def test_symlink_loops_and_unresolvable_paths_fail_with_one_message(self):
        # The kernel refuses to open more than 40 links (32 on macOS), so the shell
        # runs the script text with $0 set to the chain, as it does for a link
        # that changes while the launcher resolves it.
        for tool, shell in (("codex-as", "bash"), ("claude-as", "sh")):
            text = (self.kit / "bin" / tool).read_text()
            loop = f"{tool}: launcher symlink loop (more than 40 links); reinstall the helper.\n"
            with self.subTest(tool=tool, case="40 links"):
                result = self.run_tool([shell, "-c", text, self.chain(tool + "-40", 40, self.kit / "bin" / tool),
                                        "--show-config"])
                self.assertEqual(result.returncode, 0, result.stderr)
            for label, start in (("41 links", self.chain(tool + "-41", 41, self.kit / "bin" / tool)),
                                 ("cycle", self.chain(tool + "-cycle", 2, self.base / "chain" / f"{tool}-cycle-0"))):
                with self.subTest(tool=tool, case=label):
                    result = self.run_tool([shell, "-c", text, start, "--show-config"])
                    self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", loop))
            with self.subTest(tool=tool, case="not on PATH"):
                result = self.run_tool([shell, "-c", text, tool + "-missing", "A"])
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"{tool}: cannot find this launcher ({tool}-missing) on PATH.\n"))
            if os.geteuid() != 0:
                with self.subTest(tool=tool, case="unsearchable directory"):
                    locked = self.base / f"locked {tool}"
                    locked.mkdir()
                    (locked / tool).symlink_to(self.kit / "bin" / tool)
                    locked.chmod(0)
                    self.addCleanup(locked.chmod, 0o700)
                    result = self.run_tool([shell, "-c", text, locked / tool, "A"])
                    self.assertEqual((result.returncode, result.stdout), (1, ""))
                    self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                    self.assertRegex(result.stderr, rf"^{tool}: cannot resolve the launcher (link|directory) ")
        self.assertEqual(self.records(), [])

    def test_incomplete_installs_name_the_root_without_a_traceback(self):
        lone = self.base / "lone copy"
        for tool in TOOLS:
            write(lone / "bin" / tool, (ROOT / "bin" / tool).read_text(), 0o700)
            for args in (["--show-config"], ["A"]):
                with self.subTest(tool=tool, args=args):
                    result = self.run_tool([lone / "bin" / tool, *args])
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (1, "", f"{tool}: incomplete install at {lone} (missing lib/{tool}/launch.py)\n"))
        self.assertEqual(self.run_tool([lone / "bin/codex-as", "-bad"]).returncode, 2, "the name check comes first")
        partial = install(self.base / "partial kit")
        shutil.rmtree(partial / "lib/coding-agent-kit")
        (partial / "lib/codex-as/layout.py").unlink()
        old_listing = ["python3", "-I", "-B", partial / "lib/codex-as/check.py", "accounts"]
        for command in ([partial / "bin/codex-as", "--show-config"], [partial / "bin/codex-as", "A", "exec", "x"],
                        [partial / "bin/claude-as", "--list-accounts"], [partial / "bin/claude-as", "work"], old_listing):
            with self.subTest(command=command[-2:]):
                result = self.run_tool(command)
                tool = "claude-as" if "claude-as" in str(command[0]) else "codex-as"
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (1, "", f"{tool}: incomplete install at {partial}\n"))
        self.assertEqual(self.records(), [])

    def test_metadata_queries_start_nothing_and_ignore_launch_policy(self):
        shutil.rmtree(self.home / ".codex")
        before = snapshot(self.home)
        # Conflicting variables stop a launch in preflight; the queries never run it.
        env = {"OPENAI_API_KEY": "fabricated-access-conflict", "CLAUDE_CODE_OAUTH_TOKEN": "fabricated-access-conflict",
               "CODEX_HOME": str(self.base / "elsewhere")}
        for tool, names in (("codex-as", "A\n"), ("claude-as", "work\n")):
            with self.subTest(tool=tool):
                result = self.run_tool([self.kit / "bin" / tool, "--list-accounts"], env)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, names, ""))
                result = self.run_tool([self.kit / "bin" / tool, "--show-config"], env)
                self.assertEqual(result.returncode, 0, result.stderr)
                key = "codex" if tool == "codex-as" else "claude"
                expected = [f"config = {self.home}/.config/coding-agent-kit/config.toml (not found; defaults)",
                            f"{key} = {key} ({self.fakes / key})"]
                if tool == "codex-as":
                    expected.append(f"codex_home = {self.home}/.codex")
                self.assertEqual(result.stdout.splitlines(), [*expected, f"secrets_dir = {self.home}/.secrets"])
        self.assertEqual(snapshot(self.home), before, "a metadata query wrote to HOME")
        self.assertEqual(self.records(), [], "a metadata query started a native CLI")
        # Only alone: with more arguments the option is an invalid codex-as account name.
        for args in (["--list-accounts", "x"], ["--show-config", "--show-config"]):
            result = self.run_tool([self.kit / "bin/codex-as", *args])
            self.assertEqual(result.returncode, 2)
            self.assertIn("invalid account name", result.stderr)
        self.assertEqual(self.run_tool([self.kit / "bin/codex-as", "--list-account"]).returncode, 2)

    def completion(self, shell, tool, home, env=None):
        env = (env or {}) | {"HOME": str(home), "PATH": os.pathsep.join((str(home / ".local/bin"), self.env["PATH"]))}
        if shell == "bash":
            function = "_" + tool.replace("-", "_")
            script = (f'source "$HOME/.local/share/bash-completion/completions/{tool}"; COMP_WORDS=({tool} ""); '
                      f'COMP_CWORD=1; {function}; [ "${{#COMPREPLY[@]}}" -eq 0 ] || printf "%s\\n" "${{COMPREPLY[@]}}"')
            return self.run_tool(["bash", "--noprofile", "--norc", "-c", script], env)
        script = ('_describe() { [[ $1 == -t ]] || return 0; shift 2; shift; print -rl -- "${(@P)1}"; }; '
                  f'CURRENT=2; words=({tool} ""); source "$HOME/.oh-my-zsh/custom/completions/_{tool}"')
        return self.run_tool(["zsh", "-f", "-c", script], env)

    def test_bad_configuration_gives_empty_listings_and_completions(self):
        linked = self.base / "linked home"
        self.linked_home(linked)
        missing = self.base / "missing.toml"
        old = {"codex-as": ["python3", "-I", "-B", linked / ".local/lib/codex-as/check.py", "accounts"],
               "claude-as": ["python3", "-I", "-B", linked / ".local/lib/claude-as/launch.py", "--list-accounts"]}
        shells = [shell for shell in ("bash", "zsh") if shutil.which(shell)]
        # Each case names what its one error line must say, so none passes on another's error.
        for label, env, prepare, named in (
                ("missing explicit file", {"CODING_AGENT_KIT_CONFIG": str(missing)}, None, str(missing)),
                ("invalid value", {}, lambda: self.configure('secrets_dir = "relative"\n', linked), "secrets_dir"),
                ("insecure secrets", {}, lambda: ((linked / ".config/coding-agent-kit/config.toml").unlink(),
                                                  (linked / ".secrets").chmod(0o755)), "0700")):
            if prepare:
                prepare()
            for tool in TOOLS:
                with self.subTest(case=label, tool=tool):
                    for command in ([linked / ".local/bin" / tool, "--list-accounts"], old[tool]):
                        result = self.run_tool(command, env | {"HOME": str(linked)})
                        self.assertEqual((result.returncode, result.stdout), (1, ""))
                        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                        self.assertTrue(result.stderr.startswith(tool + ": "), result.stderr)
                        self.assertIn(named, result.stderr)
                    if label != "insecure secrets":
                        result = self.run_tool([linked / ".local/bin" / tool, "--show-config"], env | {"HOME": str(linked)})
                        self.assertEqual((result.returncode, result.stdout), (1, ""))
                    for shell in shells:
                        result = self.completion(shell, tool, linked, env)
                        # zsh completion functions return 1 for no matches.
                        self.assertEqual((result.returncode, result.stdout, result.stderr),
                                         (0 if shell == "bash" else 1, "", ""), shell)
        self.assertEqual(self.records(), [])

    def test_linked_install_completions_and_old_helpers_follow_the_configuration(self):
        linked = self.base / "linked home"
        self.linked_home(linked)
        shells = [shell for shell in ("bash", "zsh") if shutil.which(shell)]
        # Shells opened before an update keep the 0.1 completion functions, which
        # call the library files directly through the retained links.
        old = {"codex-as": ["python3", "-I", "-B", linked / ".local/lib/codex-as/check.py", "accounts"],
               "claude-as": ["python3", "-I", "-B", linked / ".local/lib/claude-as/launch.py", "--list-accounts"]}
        self.assertTrue(os.readlink(linked / ".local/lib/codex-as/check.py").startswith("../../../dotfiles/"))
        for names, prepare in ((("A", "work"), None), (("Z", "vault"), self.alternate_secrets)):
            if prepare:
                prepare(linked)
            for tool, name in zip(TOOLS, names):
                with self.subTest(tool=tool, names=names):
                    env = {"HOME": str(linked)}
                    for command in ([linked / ".local/bin" / tool, "--list-accounts"], old[tool]):
                        result = self.run_tool(command, env)
                        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, name + "\n", ""))
                    for shell in shells:
                        result = self.completion(shell, tool, linked)
                        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, name + "\n", ""), shell)
        self.assertEqual(self.records(), [])

    def alternate_secrets(self, home):
        secrets = home / "vault"
        secrets.mkdir(mode=0o700)
        self.credentials(secrets, "Z")
        write(secrets / "claude-vault", CLAUDE_TOKEN + "\n")
        self.configure('secrets_dir = "~/vault"\n', home)

    def test_codex_as_uses_the_configured_paths_and_executable(self):
        state, vault, tools = self.base / "state", self.base / "vault", self.base / "tools dir"
        root, secrets = state / "codex root", vault / "secrets"
        for directory in (state, root, vault, secrets):
            directory.mkdir(mode=0o700)
        self.credentials(secrets, "A")
        write(root / "config.toml", 'model = "configured-model"\n')
        tools.mkdir()
        shutil.copy2(self.fakes / "codex", tools / "my codex")
        self.configure(f'codex = "{tools}/my codex"\ncodex_home = "{root}/"\nsecrets_dir = "{secrets}"\n')
        native = snapshot(self.home / ".codex")
        result = self.run_tool([self.kit / "bin/codex-as", "A", "exec", "prompt"])
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual(record["argv"][0], f"{tools}/my codex")
        self.assertEqual((record["env"]["CODEX_HOME"], record["env"]["CODEX_SQLITE_HOME"]),
                         (f"{root}/accounts/A", str(root)))
        self.assertEqual(os.readlink(root / "accounts/A/auth.json"), f"{secrets}/codex-A.json")
        self.assertEqual(os.readlink(root / "accounts/A/config.toml"), f"{root}/config.toml")
        self.assertEqual(snapshot(self.home / ".codex"), native, "the default root was used")
        # Rootless commands, an inherited CODEX_HOME and -p follow codex_home.
        self.assertEqual(self.run_tool([self.kit / "bin/codex-as", "A", "--help"]).returncode, 0)
        self.assertEqual(self.records()[-1]["env"]["CODEX_HOME"], str(root))
        self.assertEqual(self.run_tool([self.kit / "bin/codex-as", "A", "exec", "x"], {"CODEX_HOME": str(root)}).returncode, 0)
        count = len(self.records())
        result = self.run_tool([self.kit / "bin/codex-as", "A", "exec", "x"], {"CODEX_HOME": str(self.home / ".codex")})
        self.assertIn("CODEX_HOME conflicts", result.stderr)
        write(root / "work.config.toml", 'model = "work"\n')
        self.assertEqual(self.run_tool([self.kit / "bin/codex-as", "A", "-p", "work", "exec", "x"]).returncode, 0)
        self.assertEqual(os.readlink(root / "accounts/A/work.config.toml"), f"{root}/work.config.toml")
        # Preflight reads the configured root's config, not ~/.codex/config.toml.
        write(self.home / ".codex/config.toml", 'model_provider = "elsewhere"\n')
        self.assertEqual(self.run_tool([self.kit / "bin/codex-as", "A", "exec", "x"]).returncode, 0)
        write(root / "config.toml", 'model_provider = "elsewhere"\n')
        self.assertIn("another model provider", self.run_tool([self.kit / "bin/codex-as", "A", "exec", "x"]).stderr)
        write(root / "config.toml", 'model = "configured-model"\n')
        # Both parents must be private to the user.
        for parent in (state, vault):
            with self.subTest(parent=parent.name):
                parent.chmod(0o770)
                result = self.run_tool([self.kit / "bin/codex-as", "A", "exec", "x"])
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, f"codex-as: {parent}: directory is group/other writable; "
                                 "review and restrict it first.\n")
                parent.chmod(0o700)
        self.assertEqual(len(self.records()), count + 2)
        self.configure(f'codex = "{tools}/missing codex"\n')
        result = self.run_tool([self.kit / "bin/codex-as", "A", "exec", "x"])
        self.assertEqual(result.stderr, f"codex-as: the configured codex executable {tools}/missing codex was not found.\n")

    def test_claude_as_uses_the_configured_secrets_and_executable(self):
        vault, tools = self.base / "vault", self.base / "tools dir"
        secrets = vault / "secrets"
        for directory in (vault, secrets, tools):
            directory.mkdir(mode=0o700)
        write(secrets / "claude-work", CLAUDE_TOKEN + "\n")
        shutil.copy2(self.fakes / "claude", tools / "claude-limited")
        self.configure(f'claude = "{tools}/claude-limited"\nsecrets_dir = "{secrets}"\n')
        (self.home / ".secrets/claude-work").unlink()
        result = self.run_tool([self.kit / "bin/claude-as", "work", "-p", "hello"])
        self.assertEqual(result.returncode, 0, result.stderr)
        record = self.records()[-1]
        self.assertEqual(record["argv"], [f"{tools}/claude-limited", "-p", "hello"])
        self.assertEqual(record["token"], hashlib.sha256(CLAUDE_TOKEN.encode()).hexdigest())
        self.assertEqual(self.run_tool([self.kit / "bin/claude-as", "--list-accounts"]).stdout, "work\n")
        vault.chmod(0o750)
        self.assertEqual(self.run_tool([self.kit / "bin/claude-as", "work"]).stderr, "")
        vault.chmod(0o770)
        result = self.run_tool([self.kit / "bin/claude-as", "work"])
        self.assertEqual(result.stderr, f"claude-as: {vault} must be an owned, real directory without group/other write access.\n")
        vault.chmod(0o700)
        secrets.chmod(0o750)
        result = self.run_tool([self.kit / "bin/claude-as", "work"])
        self.assertEqual(result.stderr, f"claude-as: {secrets} must be an owned, real directory with permissions 0700.\n")
        secrets.chmod(0o700)
        self.configure(f'claude = "{tools}/missing claude"\nsecrets_dir = "{secrets}"\n')
        result = self.run_tool([self.kit / "bin/claude-as", "work"])
        self.assertEqual(result.stderr, f"claude-as: the configured claude executable {tools}/missing claude was not found.\n")
        self.assertEqual(len(self.records()), 2)

    def test_unknown_keys_warn_once_and_the_launch_continues(self):
        self.configure('future_key = "x"\n')
        warning = f"{{tool}}: {self.home}/.config/coding-agent-kit/config.toml: unknown key future_key ignored\n"
        for tool, args in (("codex-as", ["A", "exec", "x"]), ("claude-as", ["work"])):
            with self.subTest(tool=tool):
                result = self.run_tool([self.kit / "bin" / tool, *args])
                self.assertEqual((result.returncode, result.stdout, result.stderr),
                                 (0, "stdout sentinel\n", warning.format(tool=tool)))

    def test_configuration_problems_print_one_line_without_a_traceback(self):
        file = self.configure("")
        old = ["python3", "-I", "-B", self.kit / "lib/codex-as/check.py", "accounts"]
        commands = (("codex-as", [self.kit / "bin/codex-as", "--show-config"]),
                    ("codex-as", [self.kit / "bin/codex-as", "--list-accounts"]),
                    ("codex-as", [self.kit / "bin/codex-as", "A", "exec", "x"]), ("codex-as", old),
                    ("claude-as", [self.kit / "bin/claude-as", "--show-config"]),
                    ("claude-as", [self.kit / "bin/claude-as", "work", "-p", "x"]))
        for label, text in (("nesting", "x = " + "[" * 5000 + "]" * 5000), ("integer", "x = " + "7" * 5000),
                            ("control character", 'claude = "/opt/claude\\u001b[2J"')):
            self.configure(text + "\n")
            reason = "claude: must not contain control characters" if label == "control character" else "invalid TOML"
            for tool, command in commands:
                with self.subTest(label, command=command[-2:]):
                    result = self.run_tool(command)
                    self.assertEqual((result.returncode, result.stdout), (1, ""), result.stderr)
                    self.assertRegex(result.stderr, "^" + re.escape(f"{tool}: {file}: {reason}")
                                     + r"( at line \d+, column \d+)?\.\n$")
        # Errors from elsewhere, here a --settings path of an unknown user, get the generic line too.
        file.unlink()
        result = self.run_tool([self.kit / "bin/claude-as", "work", "--settings", "~no-such-kit-test-user/x.json"])
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, "", "claude-as: missing, unreadable or malformed token/settings; no unsafe repair performed.\n"))
        self.assertEqual(self.records(), [])

    def test_no_configuration_below_a_file_and_a_missing_parent_is_named(self):
        (self.home / ".config").write_text("not a directory\n")  # no configuration file can be there
        for tool, args in (("codex-as", ["A", "exec", "x"]), ("claude-as", ["work"])):
            with self.subTest(tool=tool):
                result = self.run_tool([self.kit / "bin" / tool, *args])
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "stdout sentinel\n", ""))
        (self.home / ".config").unlink()
        count = len(self.records())
        message = "{parent} does not exist; create it, owned by you and not group or other writable.\n"
        afile = write(self.home / "afile", "a regular file\n")  # below it (ENOTDIR) nothing exists either
        for text, cases in (('codex_home = "~/missing root/codex"\n', (("codex-as", ["A", "exec", "x"], "missing root"),)),
                            ('secrets_dir = "~/missing vault/secrets"\n', (("codex-as", ["A", "exec", "x"], "missing vault"),
                                                                           ("claude-as", ["work"], "missing vault"))),
                            ('codex_home = "~/afile/x/.codex"\n', (("codex-as", ["A", "exec", "x"], "afile/x"),)),
                            ('secrets_dir = "~/afile/x/sec"\n', (("codex-as", ["A", "exec", "x"], "afile/x"),
                                                                 ("claude-as", ["work"], "afile/x")))):
            self.configure(text)
            for tool, args, parent in cases:
                with self.subTest(tool=tool, parent=parent):
                    result = self.run_tool([self.kit / "bin" / tool, *args])
                    self.assertEqual((result.returncode, result.stdout, result.stderr),
                                     (1, "", f"{tool}: " + message.format(parent=self.home / parent)))
                    self.assertFalse((self.home / parent).exists())
                    self.assertEqual(afile.read_text(), "a regular file\n")
        self.assertEqual(len(self.records()), count)

    def golden(self, variant):
        """Every launch scenario from a fresh HOME at one fixed path: outputs, records and HOME."""
        top = self.base / "golden"
        shutil.rmtree(top, ignore_errors=True)
        top.mkdir()
        home = top / "home with spaces"
        self.make_home(home)
        write(home / ".codex/work.config.toml", 'model = "work"\n')
        if variant is not None:
            self.configure(variant, home)
        record = top / "record.jsonl"
        env = {"HOME": str(home), "FAKE_RECORD": str(record)}
        results = []
        for tool, args in (("codex-as", ["A", "exec", "prompt"]), ("codex-as", ["A", "-p", "work", "resume", "--last"]),
                           ("codex-as", ["missing", "--help"]), ("codex-as", ["--list-accounts"]),
                           ("claude-as", ["work", "-p", "hello"]), ("claude-as", ["work", "--version"]),
                           ("claude-as", ["--list-accounts"])):
            result = self.run_tool([self.kit / "bin" / tool, *args], env)
            results.append((tool, args, result.returncode, result.stdout, result.stderr))
        records = [json.loads(line) for line in record.read_text().splitlines()]
        return results, records, snapshot(home, skip={".config"})

    def test_explicit_defaults_reproduce_the_defaults_exactly(self):
        expected = self.golden(None)
        results, records, files = expected
        self.assertEqual([result[2] for result in results], [0] * len(results), results)
        self.assertEqual(len(records), 4)  # claude --version prints; the fakes record no query
        home = self.base / "golden/home with spaces"
        self.assertEqual(files[".codex/accounts/A/auth.json"][2], f"{home}/.secrets/codex-A.json")
        self.assertEqual(records[2]["env"]["CODEX_HOME"], f"{home}/.codex")
        for variant in (DEFAULTS_FILE, f'codex_home = "{home}//.codex/"\nsecrets_dir = "{home}/./.secrets/"\n'):
            with self.subTest(variant=variant):
                self.assertEqual(self.golden(variant), expected)

    def test_kit_code_reads_home_only_through_the_loader(self):
        reads = re.compile(r'os\.environ\[["\']HOME["\']\]|environ\.get\(["\']HOME["\']|getenv\(["\']HOME["\']'
                           r'|Path\.home\(\)|expanduser')
        python = [path for path in ROOT.glob("lib/**/*.py") if path != ROOT / "lib/coding-agent-kit/config.py"]
        self.assertGreaterEqual(len(python), 5)
        for path in python:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(reads.findall(path.read_text()), [])
        for path in (*ROOT.glob("bin/*"), *ROOT.glob("completions/*/*")):
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertEqual(re.findall(r"\$\{?HOME\b", path.read_text()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
