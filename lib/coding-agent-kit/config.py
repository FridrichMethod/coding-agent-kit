"""coding-agent-kit configuration: one optional TOML file, read once per run.

Public API (load this file by path; it is never on sys.path):

    load() -> Config
        Find, read and validate the configuration file. The launchers call it
        once per run, after their Python version check. Raises ConfigError.
    Config, a frozen dataclass:
        file         Path of the configuration file that was looked for
        source       "found", "not found; defaults" or "from CODING_AGENT_KIT_CONFIG"
        home         Path of $HOME, absolute; no other kit code reads HOME
        codex        str: the codex executable, a bare name looked up on PATH or
                     an absolute path (~/ already expanded)
        claude       str: the claude executable, the same rule
        codex_home   Path, absolute; the account root is codex_home / "accounts"
        secrets_dir  Path, absolute; neither equal to codex_home nor nested with it
        warnings     tuple of "<file>: unknown key <key> ignored", one per unknown key
        expand(path) -> Path: Path(path).expanduser() with this run's HOME
        which(key) -> str | None: shutil.which() of the "codex" or "claude" value
        show(tool) -> str: the --show-config lines for "codex-as" or "claude-as"
    ConfigError(ValueError): one line, "<file>: <key>: <reason>" or
        "<file>: <reason>", without the "<tool>: " prefix and never quoting file
        contents; own_message is True, so the launchers print it as written.
    DEFAULTS: the four keys and their default values, as written in a file.

Location: CODING_AGENT_KIT_CONFIG when set and non-empty (absolute, and the
file must exist); else $XDG_CONFIG_HOME/coding-agent-kit/config.toml when
XDG_CONFIG_HOME is absolute; else $HOME/.config/coding-agent-kit/config.toml.
A missing file, or a path component that is a file, means the defaults; a
dangling symlink on the way is an error. Other users must not be able to change
the file, nor the directories and symlinks on its way (unsafe, exposed).
Values are normalised by pathlib only (plus a leading run of slashes collapsed
to one), never resolved, so they stay byte-stable in the absolute links the
launchers create.
"""

from dataclasses import dataclass
import errno
import functools
import grp
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shutil
import stat
import tomllib


DEFAULTS = {"codex": "codex", "claude": "claude", "codex_home": "~/.codex", "secrets_dir": "~/.secrets"}
VARIABLE = "CODING_AGENT_KIT_CONFIG"
LIMIT = 64 * 1024
COMMAND = re.compile(r"[A-Za-z0-9._+-]+")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0, DEL and C1: values are printed and passed on as is


class ConfigError(ValueError):
    own_message = True


@dataclass(frozen=True)
class Config:
    file: Path
    source: str
    home: Path
    codex: str
    claude: str
    codex_home: Path
    secrets_dir: Path
    warnings: tuple = ()

    def expand(self, path):
        """Path(path).expanduser(), with this run's HOME for ~ and ~/."""
        path = Path(path)
        if path.parts[:1] == ("~",):
            return self.home.joinpath(*path.parts[1:])
        return path.expanduser()  # ~user: the password database, never HOME

    def which(self, key):
        return shutil.which(getattr(self, key))

    def show(self, tool):
        key = "codex" if tool == "codex-as" else "claude"
        value = getattr(self, key)
        found = self.which(key) or ("not found on PATH" if COMMAND.fullmatch(value) else "not an executable file")
        lines = [f"config = {self.file} ({self.source})", f"{key} = {value} ({found})"]
        if key == "codex":
            lines.append(f"codex_home = {self.codex_home}")
        return "\n".join([*lines, f"secrets_dir = {self.secrets_dir}"])


@functools.lru_cache(maxsize=None)
def private_group(gid):
    """Debian's user private group: the user's login group in the password database (newgrp
    and sg change the real gid, not this), named after the user, with no other member, neither
    listed nor by primary group (as OpenSSH's secure_permissions checks). Cached per gid for
    one load(), which clears it: listing every account can be slow on a directory service."""
    try:
        me = pwd.getpwuid(os.getuid())
        group = grp.getgrgid(gid)
    except KeyError:
        return False
    if gid != me.pw_gid or group.gr_name != me.pw_name or not set(group.gr_mem) <= {me.pw_name}:
        return False
    return not any(entry.pw_gid == gid and entry.pw_uid != os.getuid() for entry in pwd.getpwall())


def access_acl(target):
    """Whether the fd or directory TARGET has a POSIX or NFSv4 access ACL; its group bits
    are then the ACL mask. No os.listxattr (macOS) or no xattr support means none."""
    try:
        names = os.listxattr(target)
    except AttributeError:
        return False
    except OSError as error:
        if error.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return False
        raise
    return "system.posix_acl_access" in names or "system.nfs4_acl" in names


def unsafe(info, target):
    """Why other users could change this file or directory (TARGET, its fd or path), or None."""
    if info.st_uid not in (os.getuid(), 0):
        return "owned by another user."
    if stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX:
        return None  # sticky, like /tmp: others cannot rename or remove what they do not own
    if info.st_mode & stat.S_IWOTH:
        return "writable by other users."
    if info.st_mode & stat.S_IWGRP:
        if not private_group(info.st_gid):
            return "group-writable, and the group is not your private group."
        if access_acl(target):
            return "group-writable with an access ACL; remove the ACL (setfacl -b) or the group write (chmod g-w)."
    return None


def exposed(path, home):
    """Why other users could replace the file through a directory or symlink on its way, or None.

    Checked: PATH itself when it is a symlink, and every directory from its parent up to
    HOME when it lies below HOME, else up to /, both as written and after realpath."""
    homes = {home, Path(os.path.realpath(home))}
    places = [path]
    for start in (path, Path(os.path.realpath(path))):
        for parent in start.parents:
            places.append(parent)
            if parent in homes:
                break
    for place in dict.fromkeys(places):
        info = os.lstat(place)
        if stat.S_ISLNK(info.st_mode):
            if info.st_uid not in (os.getuid(), 0):
                return f"symlink {place}: owned by another user."
        elif place != path:
            problem = unsafe(info, place)
            if problem:
                return f"directory {place}: {problem}"
    return None


def read(path, explicit, home):
    """The file's bytes, checked on the opened inode, or None when it is absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        # A component that is a file means, like a missing one, that no file can be there.
        for place in (path, *path.parents):
            if os.path.islink(place) and not os.path.exists(place):
                raise ConfigError(f"{path}: dangling symlink" + ("." if place == path else f" {place}.")) from None
        if explicit:
            raise ConfigError(f"{path}: not found ({VARIABLE}).") from None
        return None
    except OSError as error:
        raise ConfigError(f"{path}: cannot be opened ({error.strerror}).") from None
    try:
        info = os.fstat(fd)
        try:
            if not stat.S_ISREG(info.st_mode):
                problem = "not a regular file."
            else:
                problem = unsafe(info, fd) or exposed(path, home)
        except OSError as error:
            problem = f"cannot be checked ({error.strerror})."
        if problem:
            raise ConfigError(f"{path}: {problem}")
        chunks, size = [], 0
        while size <= LIMIT:
            chunk = os.read(fd, LIMIT + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > LIMIT:
        raise ConfigError(f"{path}: larger than 64 KiB.")
    return b"".join(chunks)


def parse(path, raw):
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not UTF-8 text.") from None
    except tomllib.TOMLDecodeError as error:
        # The parser's message can quote the file; keep only the position.
        where = re.search(r"\(at (line \d+, column \d+|end of document)\)$", str(error))
        raise ConfigError(f"{path}: invalid TOML" + (f" at {where[1]}." if where else ".")) from None
    except (ValueError, RecursionError):
        # Nesting deeper than the parser's recursion, or an integer past Python's digit limit.
        raise ConfigError(f"{path}: invalid TOML.") from None


def place(home, value):
    """An absolute or ~/ value as a Path, normalised by pathlib only; None otherwise.

    pathlib keeps exactly two leading slashes as a root of their own, which Linux and
    macOS read as /, so a leading run of slashes becomes one."""
    if value.startswith("~/"):
        return home.joinpath(value[2:].lstrip("/"))
    return Path("/" + value.lstrip("/")) if value.startswith("/") else None


def single(path):
    """PATH with a leading run of slashes as one, for comparisons; HOME may start with //."""
    return Path("/" + str(path).lstrip("/"))


def load():
    private_group.cache_clear()  # one listing of the accounts per run, never one from an earlier load
    value = os.environ.get("HOME", "")
    if not Path(value).is_absolute():
        raise ConfigError("HOME must be set to an absolute path.")
    home = Path(value)
    explicit = os.environ.get(VARIABLE, "")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute():
            raise ConfigError(f"{explicit}: {VARIABLE} must be an absolute path.")
    else:
        xdg = Path(os.environ.get("XDG_CONFIG_HOME", ""))
        path = (xdg if xdg.is_absolute() else home / ".config") / "coding-agent-kit/config.toml"
    raw = read(path, bool(explicit), home)
    data = {} if raw is None else parse(path, raw)
    values, warnings = dict(DEFAULTS), []
    for key, item in data.items():
        if key not in DEFAULTS:
            shown = key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)
            warnings.append(f"{path}: unknown key {shown} ignored")
        elif not isinstance(item, str):
            raise ConfigError(f"{path}: {key}: must be a string.")
        elif not item:
            raise ConfigError(f"{path}: {key}: must not be empty.")
        elif CONTROL.search(item):
            raise ConfigError(f"{path}: {key}: must not contain control characters.")
        else:
            values[key] = item
    for key in ("codex", "claude"):
        if not COMMAND.fullmatch(values[key]):
            where = place(home, values[key])
            if where is None:
                raise ConfigError(f"{path}: {key}: must be a command name ({COMMAND.pattern}) or an absolute or ~/ path.")
            values[key] = str(where)
    for key in ("codex_home", "secrets_dir"):
        where = place(home, values[key])
        if where is None:
            raise ConfigError(f"{path}: {key}: must be an absolute or ~/ path.")
        if ".." in PurePosixPath(values[key]).parts:
            raise ConfigError(f"{path}: {key}: must not contain a .. component.")
        values[key] = where
    root, secrets = single(values["codex_home"]), single(values["secrets_dir"])
    if root == secrets:
        raise ConfigError(f"{path}: secrets_dir: must differ from codex_home.")
    if root in secrets.parents:
        raise ConfigError(f"{path}: secrets_dir: must not be inside codex_home.")
    if secrets in root.parents:
        raise ConfigError(f"{path}: codex_home: must not be inside secrets_dir.")
    source = f"from {VARIABLE}" if explicit else "found" if raw is not None else "not found; defaults"
    return Config(path, source, home, warnings=tuple(warnings), **values)
