"""coding-agent-kit account metadata: last-seen identity, labels and usage, never a token.

Public API (load this file by path; it is never on sys.path):

    directory(config, create=False) -> Path
        config.state_dir / "accounts". With create, makes it 0700 (and its
        parents under the launcher's umask) and checks that it is an owned,
        real 0700 directory whose parent other users cannot change. Either
        way it raises ValueError when it is config.secrets_dir, also through
        a link: its files carry the Codex credentials' names.
    read(path) -> dict | None
        One metadata file, or None when it is absent. Never follows a link;
        only an owned regular 0600 file with one link, at most 64 KiB, holding
        a JSON object with unique keys. Raises ValueError otherwise.
    write(path, data) -> bool
        Replaces the file atomically (an O_EXCL|O_NOFOLLOW 0600 temporary file
        in the same directory, then rename) unless it already holds exactly
        this content; returns whether it wrote.
    clean(value, limit) -> str | None
        A string with C0, DEL and C1 characters removed and cut to limit, or
        None for anything else or an empty result.
    label_problem(text) -> str | None
        Why TEXT cannot be a label: control characters, more than 100
        characters, or a Claude token prefix (a pasted token is never stored).
    info(tool, name, config, now) -> str
        The --account-info text of NAME for "codex-as" or "claude-as".
    labels(tool, names, config) -> list[str]
        "NAME<TAB>LABEL" (the email for Codex, the label for Claude), or a bare
        NAME, for each name: the --account-labels lines. Unsafe metadata, or a
        directory that is the secrets directory, gives bare names.

Files, each with one writer, under directory(config):
    codex-NAME.json         codex-as: email, plan and the last usage snapshot
    claude-NAME.json        claude-as: the label
    claude-NAME.usage.json  helpers/statusline.cjs: the last rate-limit windows
Times in them come from a token, a Codex rollout or the status line, never
from the launcher's clock; one from year 10000 on is ignored.
"""

import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time


LIMIT = 64 * 1024
LABEL_LIMIT = 100
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
WINDOWS = {"codex-as": ("primary", "secondary"), "claude-as": ("five_hour", "seven_day")}
NAMES = {"five_hour": "5h", "seven_day": "weekly"}
LAST = 253402300800  # 10000-01-01 UTC: no calendar time from here on


def directory(config, create=False):
    path = config.state_dir / "accounts"
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        aliased = os.path.samefile(path, config.secrets_dir)
    except OSError:
        aliased = False
    if aliased:
        raise ValueError(f"{path}: must not be the secrets directory.")
    if not create:
        return path
    uid = os.getuid()
    parent, info = path.parent.lstat(), path.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid not in (uid, 0) or parent.st_mode & 0o022:
        raise ValueError(f"{path.parent}: must be a real directory that other users cannot change.")
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(f"{path}: must be an owned, real directory with permissions 0700.")
    return path


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def finite(_):
    raise ValueError("non-finite number")


def read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(f"{path.name}: unreadable metadata ({error.strerror}).") from None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > LIMIT):
            raise ValueError(f"{path.name}: unsafe metadata (not an owned 0600 file up to 64 KiB).")
        raw = stream.read(LIMIT + 1)
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique, parse_constant=finite)
    except (ValueError, RecursionError):
        raise ValueError(f"{path.name}: malformed metadata.") from None
    if len(raw) > LIMIT or not isinstance(data, dict):
        raise ValueError(f"{path.name}: malformed metadata.")
    return data


def write(path, data):
    raw = (json.dumps(data, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC), "rb") as stream:
            if stream.read(LIMIT + 1) == raw:
                return False
    except OSError:
        pass
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return True


def clean(value, limit):
    if not isinstance(value, str):
        return None
    return CONTROL.sub("", value).strip()[:limit] or None


def label_problem(text):
    if CONTROL.search(text):
        return "it contains control characters"
    if "sk-ant-" in text:
        return "it looks like a token"
    if len(text) > LABEL_LIMIT:
        return f"it is longer than {LABEL_LIMIT} characters"
    return None


def window(value):
    """A usage window as (used percent, window minutes or None, reset epoch or None), or None."""
    if not isinstance(value, dict):
        return None
    used = value.get("used_percent", value.get("used_percentage"))
    minutes, resets = value.get("window_minutes"), value.get("resets_at")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
        return None
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0):
        minutes = None
    if resets is not None and (isinstance(resets, bool) or not isinstance(resets, int) or not 0 < resets < LAST):
        resets = None
    return used, minutes, resets


def window_name(key, minutes):
    if key in NAMES:
        return NAMES[key]
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return "weekly"
    if minutes:
        return f"{minutes // 1440}d" if minutes % 1440 == 0 else f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"
    return key


def span(seconds):
    minutes = max(0, int(seconds) // 60)
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{minutes // 1440}d {minutes % 1440 // 60}h"


def clock(epoch, now):
    """EPOCH as local HH:MM, with the weekday unless it is today; None where the platform has no such time."""
    try:
        local = time.localtime(epoch)
        same_day = time.localtime(now)[:3] == local[:3]
        return time.strftime("%H:%M" if same_day else "%a %H:%M", local)
    except (OverflowError, OSError, ValueError):
        return None


def age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def usage_lines(tool, usage, now):
    """The window lines and the seen line of a usage record, or None without any window."""
    if not isinstance(usage, dict):
        return None
    lines = []
    for key in WINDOWS[tool]:
        parsed = window(usage.get(key))
        if parsed is None:
            continue
        used, minutes, resets = parsed
        name = window_name(key, minutes)
        at = None if resets is None else clock(resets, now)
        if at is None:
            lines.append(f"{name:<8}{round(used)}% used")
        elif resets <= now:
            lines.append(f"{name:<8}reset since {at}")
        else:
            lines.append(f"{name:<8}{round(used)}% used, resets {at} (in {span(resets - now)})")
    seen = usage.get("seen_at")
    if not lines:
        return None
    if isinstance(seen, int) and not isinstance(seen, bool) and 0 < seen < LAST:
        lines.append(f"{'seen':<8}{age(now - seen)}")
    return lines


def info(tool, name, config, now):
    folder = directory(config)
    if tool == "codex-as":
        data = read(folder / f"codex-{name}.json") or {}
        lines = [f"{name} (Codex)"]
        for key in ("email", "plan"):
            lines.append(f"{key:<8}{clean(data.get(key), 254) or 'not recorded yet'}")
        usage = data.get("usage")
    else:
        data = read(folder / f"claude-{name}.json") or {}
        label = clean(data.get("label"), LABEL_LIMIT)
        lines = [f"{name} (Claude)", f"{'label':<8}{label or f'not set (claude-as --set-label {name} LABEL)'}"]
        usage = read(folder / f"claude-{name}.usage.json")
    lines.extend(usage_lines(tool, usage, now) or [f"{'usage':<8}not recorded yet"])
    return "\n".join(lines)


def labels(tool, names, config):
    try:
        folder = directory(config)
    except ValueError:
        return list(names)
    result = []
    for name in names:
        try:
            if tool == "codex-as":
                label = clean((read(folder / f"codex-{name}.json") or {}).get("email"), 254)
            else:
                label = clean((read(folder / f"claude-{name}.json") or {}).get("label"), LABEL_LIMIT)
        except ValueError:
            label = None
        result.append(f"{name}\t{label}" if label else name)
    return result
