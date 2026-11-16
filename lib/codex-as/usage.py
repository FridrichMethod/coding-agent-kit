"""Codex rate limits and activity, read from the session rollouts native Codex writes.

latest(root, ids, account, started, ended) gives the account record the last
snapshot of a codex-as run; session(path, started) gives the reserve guard what
one session's rollout shows since a launch started; ran_tools(root, ids,
account, started, ended) tells codex-as whether a run made a call that a tool
hook sees.

The last snapshot of a run:

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

One session, for the guard (session): the tail of the rollout at PATH, without
a first line the tail cut and a last line still being written, and of it the
lines stamped at or after started - SLACK. Lines a sub-agent inherited from its
parent (metadata.inherited_user_message) never count, whatever their time.

- snapshot: the newest token_count line with at least one usable window, by
  slot ({"primary", "secondary", "seen_at"}); a slot window() refuses is None.
  A rate_limits object with both slots null or unusable is no snapshot.
- family: the snapshot line's own limit_id; None for "codex", for none, and
  for one that is not a short identifier (FAMILY).
- accounted: a token_count line, unless it has no usable window and comes
  right after a compaction and its turn_context (a compaction's own token_count,
  not a response's; after a cut first line the compaction is assumed).
- responded: a model response: a response_item reasoning, assistant message or
  *_call, or a token_usage_record line; never an agent_message (input from
  another agent).
- tools: a call a tool hook sees: a function_call other than the plain wait and
  write_stdin, a custom_tool_call or a local_shell_call; not web_search_call or
  tool_search_call.

A run's calls (ran_tools): the rollouts latest() would read, minus guardian
review threads, which run no user hook and never take a file's place, with a
call as in session() stamped within the run. Like latest() it reads only each
rollout's tail, so a call followed by more than TAIL bytes of its rollout is
missed.
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
FAMILY = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # a limit_id named in a message
CALLS = ("function_call", "custom_tool_call", "local_shell_call")  # payload types a tool hook sees
UNHOOKED = ("wait", "write_stdin")  # plain function_call names no tool hook sees
TURN_EVENTS = ("task_started", "task_complete", "turn_aborted", "token_count")  # events that end a compaction
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


def meta(stream):
    """The payload of the rollout's session_meta line, or None."""
    stream.seek(0)
    head = stream.read(FIRST_LINE).split(b"\n", 1)[0]
    try:
        line = json.loads(head)
    except (ValueError, RecursionError):
        return None
    payload = line.get("payload") if isinstance(line, dict) and line.get("type") == "session_meta" else None
    return payload if isinstance(payload, dict) else None


def created_by(payload):
    """(creator user, creator workspace) of a session_meta PAYLOAD, or None."""
    if payload is None:
        return None
    user, account = payload.get("creator_user_id"), payload.get("creator_account_id")
    return (user, account) if isinstance(user, str) and isinstance(account, str) else None


def creator(stream):
    """(creator user, creator workspace) from the rollout's session_meta line, or None."""
    return created_by(meta(stream))


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


def tail(stream):
    """The complete lines of the rollout's last TAIL bytes, and whether a first line was cut off."""
    start = max(0, os.fstat(stream.fileno()).st_size - TAIL)
    stream.seek(start)
    lines = stream.read(TAIL).split(b"\n")[:-1]  # the last piece is empty, or a line still being written
    return (lines[1:], True) if start else (lines, False)


def snapshots(stream, started, ended):
    """(time, snapshot) of each usable token_count event in the rollout's tail."""
    for raw in tail(stream)[0]:
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


# -- the reserve guard: one session, and a run's calls --------------------------

# Where a line leaves the search for a compaction's token_count: CLEAR, after a
# compaction (COMPACTED), after it and its turn_context (CONTEXT), or after a cut
# first line, which may have been either (UNKNOWN).
CLEAR, COMPACTED, CONTEXT, UNKNOWN = range(4)


def parsed(raw):
    """(line, type, payload, payload type) of one rollout line, or None when it is not a JSON object."""
    try:
        line = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(line, dict):
        return None
    payload = line.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    return line, line.get("type"), payload, payload.get("type")


def step(state, kind, ptype):
    """The compaction state after a line of KIND and payload type PTYPE."""
    if kind == "compacted" or kind == "response_item" and ptype == "compaction":
        return COMPACTED
    if kind == "turn_context":
        return CONTEXT if state in (COMPACTED, UNKNOWN) else CLEAR
    if kind in ("session_meta", "response_item", "token_usage_record") or kind == "event_msg" and ptype in TURN_EVENTS:
        return CLEAR
    return state  # other events (item_completed, ...) and line types leave it


def inherited(line):
    """A sub-agent's copy of a line of its parent's history."""
    metadata = line.get("metadata")
    return isinstance(metadata, dict) and metadata.get("inherited_user_message") is True


def usable(limits):
    """The windows of a rate_limits object by slot, an unusable slot as None; None when none is usable."""
    if not isinstance(limits, dict):
        return None
    windows = {}
    for key in ("primary", "secondary"):
        try:
            windows[key] = window(limits.get(key))
        except ValueError:
            windows[key] = None  # window_minutes 0 occurs; the other slot may still count
    return windows if any(windows.values()) else None


def family_of(limits):
    family = limits.get("limit_id")
    return family if isinstance(family, str) and family != "codex" and FAMILY.fullmatch(family) else None


def responded(kind, ptype, payload):
    """A line of a model response: never an agent_message, which is another agent's input."""
    if kind == "token_usage_record":
        return True
    return kind == "response_item" and isinstance(ptype, str) and (
        ptype == "reasoning" or ptype == "message" and payload.get("role") == "assistant" or ptype.endswith("_call"))


def hooked(kind, ptype, payload):
    """A call that a tool hook sees."""
    if kind != "response_item" or ptype not in CALLS:
        return False
    return not (ptype == "function_call" and not payload.get("namespace") and payload.get("name") in UNHOOKED)


def session(path, started):
    """What the rollout at PATH shows since STARTED, as the module docstring says; None when PATH
    is not a readable regular file.

    {"snapshot": {"primary": window|None, "secondary": window|None, "seen_at": int} | None,
     "family": str | None, "accounted": bool, "responded": bool, "tools": bool}"""
    try:
        with opened(Path(path)) as stream:
            lines, cut = tail(stream)
    except (OSError, TypeError, ValueError):
        return None
    found = {"snapshot": None, "family": None, "accounted": False, "responded": False, "tools": False}
    state = UNKNOWN if cut else CLEAR
    for raw in lines:
        line = parsed(raw)
        if line is None:
            continue
        line, kind, payload, ptype = line
        before, state = state, step(state, kind, ptype)
        when = moment(line.get("timestamp"))
        if when is None or when < started - SLACK or inherited(line):
            continue
        if kind == "event_msg" and ptype == "token_count":
            limits = payload.get("rate_limits")
            windows = usable(limits)
            if windows:
                found.update(snapshot={**windows, "seen_at": int(when)}, family=family_of(limits), accounted=True)
            elif before not in (CONTEXT, UNKNOWN):
                found["accounted"] = True
        elif responded(kind, ptype, payload):
            found["responded"] = True
            found["tools"] = found["tools"] or hooked(kind, ptype, payload)
    return found


def ran_tools(root, ids, account, started, ended):
    """Whether this run's rollouts of this account show a call that a tool hook sees.

    IDS and ACCOUNT as for latest(); guardian review threads are skipped."""
    ids = {thread for thread in ids if thread}
    owned, heads = 0, 0
    for path in candidates(root, ids, started, ended):
        continued = NAME.fullmatch(path.name)[1] in ids
        if owned == MAX_FILES or not continued and (account is None or heads == MAX_HEADS):
            break
        try:
            with opened(path) as stream:
                payload = meta(stream)
                if not continued:
                    heads += 1
                    if created_by(payload) != account:
                        continue
                if payload is not None and payload.get("thread_source") == "guardian_review":
                    continue  # no user hook runs there, and reviews must not crowd the run's own threads out
                owned += 1
                for raw in tail(stream)[0]:
                    found = parsed(raw) if b"_call" in raw else None
                    if found is None:
                        continue
                    line, kind, payload, ptype = found
                    when = moment(line.get("timestamp"))
                    if (hooked(kind, ptype, payload) and not inherited(line) and when is not None
                            and started - SLACK <= when <= ended + SLACK):
                        return True
        except OSError:
            continue
    return False
