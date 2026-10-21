"""The last Codex rate-limit snapshot one codex-as run saw, read from its own rollouts.

Best effort and offline: after native Codex exits, codex-as reads the tail of
the shared session rollouts written during the run and keeps the newest
token_count event with rate limits that belongs to this account. A rollout
belongs to it when its thread is the one this run resumed or recovered, or
when the thread's creator (as native Codex records it in session_meta) is this
account's ChatGPT user and workspace. The rollout format is Codex's own and
internal; anything unexpected yields nothing. Only regular files are opened,
never a FIFO, device or link, so nothing blocks after Codex exits. The threads
this run continues come first, then the newest rollouts; at most MAX_FILES of
this account's are read, after at most MAX_HEADS session_meta lines of others.
Known gaps: a session switched inside Codex (/resume) is missed, and another
account resuming, during the run, a thread this account created can be counted
here.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import time


TAIL = 1 << 20
FIRST_LINE = 1 << 20
MAX_FILES = 8  # rollouts of this account read in one run
MAX_HEADS = 64  # first lines of other rollouts read to find them
LAST = 253402300800  # 10000-01-01 UTC: no calendar time from here on
SLACK = 5  # seconds around the run, for clocks that tick between the launcher and Codex
NAME = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
                  r"(?:_[0-9a-f-]{36})?\.jsonl")


def days(start, end):
    """The local and UTC date directories a rollout started in [start, end] can be in."""
    result, moment = set(), start - 86400
    while moment <= end + 86400:
        for parts in (time.localtime(moment), time.gmtime(moment)):
            result.add(f"{parts.tm_year:04d}/{parts.tm_mon:02d}/{parts.tm_mday:02d}")
        moment += 3600
    return sorted(result)


def candidates(root, ids, started, ended):
    """Regular rollouts modified during the run: the threads of IDS first, then the newest."""
    sessions = Path(root) / "sessions"
    found = set()
    for thread in ids:
        found.update(sessions.glob(f"*/*/*/rollout-*-{thread}*.jsonl"))
    for day in days(started, ended):
        found.update((sessions / day).glob("rollout-*.jsonl"))
    recent = []
    for path in found:
        match = NAME.fullmatch(path.name)
        try:
            info = path.lstat()
        except OSError:
            continue
        if match and stat.S_ISREG(info.st_mode) and info.st_mtime >= started - SLACK:
            recent.append((match[1] not in ids, -info.st_mtime, str(path), path))
    return [path for *_, path in sorted(recent)]


def opened(path):
    """PATH as a binary stream while it is a regular file; never waits on a FIFO or device."""
    stream = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC), "rb")
    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
        stream.close()
        raise OSError(f"{path.name}: not a regular file")
    return stream


def creator(stream):
    """(creator user, creator workspace) from the rollout's session_meta line, or None."""
    stream.seek(0)
    head = stream.read(FIRST_LINE).split(b"\n", 1)[0]
    try:
        line = json.loads(head)
    except ValueError:
        return None
    payload = line.get("payload") if isinstance(line, dict) and line.get("type") == "session_meta" else None
    if not isinstance(payload, dict):
        return None
    user, account = payload.get("creator_user_id"), payload.get("creator_account_id")
    return (user, account) if isinstance(user, str) and isinstance(account, str) else None


def moment(text):
    try:
        stamp = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).timestamp()


def window(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError
    used, minutes, resets = value.get("used_percent"), value.get("window_minutes"), value.get("resets_at")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
        raise ValueError
    for item in (minutes, resets):
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item <= 0):
            raise ValueError
    if resets is not None and resets >= LAST:
        raise ValueError
    return {"used_percent": float(used), "window_minutes": minutes, "resets_at": resets}


def snapshots(stream, started, ended):
    """(time, snapshot) of each usable token_count event in the rollout's tail."""
    start = max(0, os.fstat(stream.fileno()).st_size - TAIL)
    stream.seek(start)
    lines = stream.read(TAIL).split(b"\n")
    if start:
        lines = lines[1:]  # the first line was cut
    for raw in lines:
        if b"token_count" not in raw:
            continue
        try:
            line = json.loads(raw)
            payload = line["payload"]
            limits = payload["rate_limits"] if line["type"] == "event_msg" and payload["type"] == "token_count" else None
            if not isinstance(limits, dict) or limits.get("limit_id") not in (None, "codex"):
                continue
            when = moment(line.get("timestamp"))
            if when is None or not started - SLACK <= when <= ended + SLACK:
                continue
            windows = {key: window(limits.get(key)) for key in ("primary", "secondary")}
        except (ValueError, KeyError, TypeError, RecursionError):
            continue
        if any(windows.values()):
            yield when, windows


def latest(root, ids, account, started, ended):
    """The newest snapshot of this run: {"primary", "secondary", "seen_at"}, or None.

    IDS are the thread UUIDs this run resumed or recovered; ACCOUNT is the
    (ChatGPT user, workspace) pair of this login, or None for an API key."""
    ids = {thread for thread in ids if thread}
    best, owned, heads = None, 0, 0
    for path in candidates(root, ids, started, ended):
        continued = NAME.fullmatch(path.name)[1] in ids
        # The threads of IDS come first: past them, only a creator match counts.
        if owned == MAX_FILES or not continued and (account is None or heads == MAX_HEADS):
            break
        try:
            with opened(path) as stream:
                if not continued:
                    heads += 1
                    if creator(stream) != account:
                        continue
                owned += 1
                for when, windows in snapshots(stream, started, ended):
                    if best is None or when > best[0]:
                        best = (when, windows)
        except OSError:
            continue
    if best is None:
        return None
    return {**best[1], "seen_at": int(best[0])}
