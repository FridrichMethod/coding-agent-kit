"""Codex rate limits and activity, read from the session rollouts native Codex writes.

latest(root, ids, account, started, ended, skew=0) gives the account record the
last snapshot of a codex-as run; session(path, started) gives the reserve guard
what one session's rollout shows since a launch started.

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

The run has two bounds, one for each clock. A rollout is opened when its file
time is at most SLACK + skew seconds before STARTED: on a network home directory
that time is the file server's, whose clock may lag this host's by skew. A
snapshot counts when its own line is stamped from STARTED to ENDED + SLACK:
Codex stamps the lines with this host's clock, in milliseconds, truncated, and
codex-as passes as STARTED the moment before it starts Codex less that one
millisecond, so no line of the run is older. A snapshot that an earlier run
left in a rollout this run opens (a resumed thread's, or one of its
sub-agents') may be another account's reading and never counts, whatever skew
is and however shortly before the run it was written.

Known gaps: a session switched inside Codex (/resume) is missed, and another
account resuming, during the run, a thread this account created can be counted
here.

One session, for the guard (session): the tail of the rollout at PATH, without
a first line the tail cut and a last line still being written, and of it the
lines stamped at or after started - SLACK. Lines a sub-agent inherited from its
parent (metadata.inherited_user_message) never count, whatever their time. A
token_count line or a model response whose time is not an ISO string (format
drift) is accounted and nothing else, so the guard is blind instead of open.

- snapshot: the newest token_count line with at least one usable window, by
  slot ({"primary", "secondary", "seen_at"}); a slot window() refuses is None,
  and a percentage above 100 counts as 100 (window()).
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
"""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePath
import re
import stat
import time


TAIL = 1 << 20
FIRST_LINE = 1 << 20
MAX_FILES = 8  # rollouts of this account read in one run
MAX_HEADS = 64  # first lines of other rollouts read to find them
LAST = 253402300800  # 10000-01-01 UTC: no calendar time from here on
# Seconds of allowance at a run's edges: for a file's time before the run, for a snapshot's line after
# it, and for the lines session() reads before a launch. Never for a snapshot's line before a run.
SLACK = 5
FAMILY = re.compile(r"[A-Za-z0-9_.-]{1,64}")  # a limit_id named in a message
CALLS = ("function_call", "custom_tool_call", "local_shell_call")  # payload types a tool hook sees
UNHOOKED = ("wait", "write_stdin")  # plain function_call names no tool hook sees
TURN_EVENTS = ("task_started", "task_complete", "turn_aborted", "token_count")  # events that end a compaction
STAMP = re.compile(rb'\{\s*"timestamp"\s*:\s*"([^"\\]{1,64})"')  # how native Codex starts each line
NAME = re.compile(r"rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
                  r"(?:_[0-9a-f-]{36})?\.jsonl")
# The notes of a launch's turns (STATE.turns) and the account's attested usage. Explicit [0-9] classes, matched
# whole: \d takes digits outside ASCII.
TURN = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")  # a turn id as native Codex makes one
OWNER = re.compile(r"[0-9a-f]{16}")  # the tag of a login (tag)
REL = re.compile(r"[0-9]{4}/[0-9]{2}/[0-9]{2}/rollout-[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}"
                 r"-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?:_[0-9a-f-]{36})?\.jsonl")  # a rollout below sessions/
ATTESTED = re.compile(r"codex-usage-[A-Za-z0-9_][A-Za-z0-9_-]*\.json")
NOTES = 1 << 20  # bytes of STATE.turns a reader takes, and the size at which a guard stops appending
WITNESSED = 64  # rollouts read after a run whose hooks named turns
TOLERANCE = 120  # seconds two reset times of one window may differ
AGAIN = 300  # seconds after which an unchanged reading is written again
NAMING = ("task_started", "task_complete", "turn_aborted")  # event_msg types that name their turn
TURN_ENDS = ("task_complete", "turn_aborted")


def days(start, end):
    """The local and UTC date directories a rollout started in [start, end] can be in."""
    result, moment = set(), start - 86400
    while moment <= end + 86400:
        for parts in (time.localtime(moment), time.gmtime(moment)):
            result.add(f"{parts.tm_year:04d}/{parts.tm_mon:02d}/{parts.tm_mday:02d}")
        moment += 3600
    return sorted(result)


def candidates(root, ids, started, ended, skew=0):
    """Regular rollouts modified during the run, or at most SLACK + SKEW seconds before it: the
    threads of IDS first, then the newest."""
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
        if match and stat.S_ISREG(info.st_mode) and info.st_mtime >= started - SLACK - skew:
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
    """One rate-limit slot as {"used_percent", "window_minutes", "resets_at"}, or None for null.

    ValueError for anything else. A finite percentage above 100 counts as 100: an exhausted
    window is a reading, never an unusable slot."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError
    used, minutes, resets = value.get("used_percent"), value.get("window_minutes"), value.get("resets_at")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or isinstance(used, float) and not math.isfinite(used):
        raise ValueError
    if used < 0:
        raise ValueError
    for item in (minutes, resets):
        if item is not None and (isinstance(item, bool) or not isinstance(item, int) or item <= 0):
            raise ValueError
    if resets is not None and resets >= LAST:
        raise ValueError
    return {"used_percent": float(min(used, 100)), "window_minutes": minutes, "resets_at": resets}


def tail(stream):
    """The complete lines of the rollout's last TAIL bytes, and whether a first line was cut off."""
    start = max(0, os.fstat(stream.fileno()).st_size - TAIL)
    stream.seek(start)
    lines = stream.read(TAIL).split(b"\n")[:-1]  # the last piece is empty, or a line still being written
    return (lines[1:], True) if start else (lines, False)


def backwards(stream):
    """The rollout's complete lines, newest first, one list per TAIL bytes read back from the end;
    a last line still being written is skipped, and a line longer than TAIL comes in a later list."""
    position, carry, done = os.fstat(stream.fileno()).st_size, b"", False
    while position > 0:
        start = max(0, position - TAIL)
        stream.seek(start)
        data = stream.read(position - start) + carry
        position = start
        if not done:
            cut = data.rfind(b"\n")
            if cut < 0:
                carry = b""  # all of it is the line still being written
                continue
            data, done = data[:cut], True
        pieces = data.split(b"\n")
        carry, lines = pieces[0], pieces[:0:-1]
        if start == 0:
            lines.append(carry)
        yield lines


def snapshots(stream, started, ended):
    """(time, snapshot) of each usable token_count event in the rollout's tail whose line is stamped
    from STARTED to ENDED + SLACK."""
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
            if when is None or not started <= when <= ended + SLACK:
                continue
            windows = {key: window(limits.get(key)) for key in ("primary", "secondary")}
        except (ValueError, KeyError, TypeError, RecursionError):
            continue
        if any(windows.values()):
            yield when, windows


def latest(root, ids, account, started, ended, skew=0):
    """The newest snapshot of this run: {"primary", "secondary", "seen_at"}, or None.

    IDS are the thread UUIDs this run resumed or recovered; ACCOUNT is the
    (ChatGPT user, workspace) pair of this login, or None for an API key.
    SKEW is how many seconds a file server's clock may lag this host's: it
    widens which rollouts are opened (candidates), never which lines count
    (snapshots)."""
    ids = {thread for thread in ids if thread}
    best, owned, heads = None, 0, 0
    for path in candidates(root, ids, started, ended, skew):
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


def tag(owner):
    """The tag of a login: the first 16 hex digits of the SHA-256 of its (ChatGPT user, workspace)
    pair joined by a line feed, as UTF-8; None for None (an API key)."""
    if owner is None:
        return None
    import hashlib  # here, not at the top: the guard loads this module at every event

    return hashlib.sha256("\n".join(owner).encode("utf-8")).hexdigest()[:16]


def relative(path):
    """REL of a transcript path (a str or a Path): its last four components, when the path has at
    least six and the fifth from the end is sessions; None for anything else."""
    if isinstance(path, PurePath):
        path = str(path)
    if not isinstance(path, str):
        return None
    parts = path.split("/")
    rel = "/".join(parts[-4:])
    return rel if len(parts) >= 6 and parts[-5] == "sessions" and REL.fullmatch(rel) else None


def turn_note(turn, rel):
    """The line of STATE.turns that notes TURN for the rollout REL; ValueError unless both match."""
    if not (isinstance(turn, str) and TURN.fullmatch(turn) and isinstance(rel, str) and REL.fullmatch(rel)):
        raise ValueError("not a turn id and a rollout below sessions/")
    return f"t {turn} {rel}\n".encode("ascii")


def turn_notes(raw):
    """(owner tag or None, {rel: set of turn ids}) of the bytes of STATE.turns.

    The piece after the last line feed is dropped (empty, or a line a full disk cut short); the
    owner comes from the first line only; a line of any other form is skipped."""
    owner, turns = None, {}
    if not isinstance(raw, (bytes, bytearray)):
        return owner, turns
    for number, piece in enumerate(bytes(raw).split(b"\n")[:-1]):
        try:
            words = piece.decode("ascii").split(" ")
        except UnicodeDecodeError:
            continue
        if number == 0 and len(words) == 2 and words[0] == "o" and OWNER.fullmatch(words[1]):
            owner = words[1]
        elif len(words) == 3 and words[0] == "t" and TURN.fullmatch(words[1]) and REL.fullmatch(words[2]):
            turns.setdefault(words[2], set()).add(words[1])
    return owner, turns


def turn_of(kind, ptype, payload):
    """The turn id a rollout line names, or None.

    A turn_context or token_usage_record line, or an event_msg of NAMING, names the turn in its
    payload's turn_id; a task_started also in turn_attribution.turn_id when turn_id is no string.
    Only an id in UUID form names a turn; an item_completed line, which can follow its turn's end,
    names none."""
    if not isinstance(payload, dict) or not (kind in ("turn_context", "token_usage_record")
                                             or kind == "event_msg" and ptype in NAMING):
        return None
    turn = payload.get("turn_id")
    if not isinstance(turn, str) and kind == "event_msg" and ptype == "task_started":
        attribution = payload.get("turn_attribution")
        turn = attribution.get("turn_id") if isinstance(attribution, dict) else None
    return turn if isinstance(turn, str) and TURN.fullmatch(turn) else None


def session(path, started, turns=None):
    """What the rollout at PATH shows since STARTED, as the module docstring says; None when PATH
    is not a readable regular file.

    {"snapshot": {"primary": window|None, "secondary": window|None, "seen_at": int} | None,
     "family": str | None, "accounted": bool, "responded": bool, "tools": bool, "own": dict | None}

    "own" is what the lines of the turns in TURNS (a set of turn ids, or None) show, whenever they
    are stamped: the first five keys counted over those lines only, and "turns", the turns of TURNS
    that the tail names. None when TURNS is None or no line of the tail names a turn."""
    try:
        with opened(Path(path)) as stream:
            lines, cut = tail(stream)
    except (OSError, TypeError, ValueError):
        return None
    found = {"snapshot": None, "family": None, "accounted": False, "responded": False, "tools": False}
    own = dict(found)
    state = UNKNOWN if cut else CLEAR
    # The open turn, whether it has had a model response, and the time of its last token_usage_record.
    current, replied, asked, named, naming = None, False, None, set(), False
    for raw in lines:
        line = parsed(raw)
        if line is None:
            continue
        line, kind, payload, ptype = line
        before, state = state, step(state, kind, ptype)
        if inherited(line):
            continue
        when = moment(line.get("timestamp"))
        if turns is not None:
            turn = turn_of(kind, ptype, payload)
            if turn is not None:
                naming = True
                if turn in turns:
                    named.add(turn)
                if turn != current or kind == "event_msg" and ptype == "task_started":
                    replied, asked = False, None
                current = turn
            mine = current in turns
            if kind == "event_msg" and ptype in TURN_ENDS:
                current, replied, asked = None, False, None  # what follows belongs to no turn
            if mine and kind == "event_msg" and ptype == "token_count":
                limits = payload.get("rate_limits")
                windows = usable(limits)
                if windows and replied and when is not None:
                    seen = when if asked is None else asked  # the reply's time, not the line's
                    own.update(snapshot={**windows, "seen_at": int(seen)}, family=family_of(limits), accounted=True)
                elif windows and when is None or not windows and before not in (CONTEXT, UNKNOWN):
                    own["accounted"] = True
                # A window before the turn's first response is no reply's reading: it is ignored.
            elif mine and responded(kind, ptype, payload):
                replied = True
                if kind == "token_usage_record" and when is not None:
                    asked = when
                own["responded"] = True
                own["tools"] = own["tools"] or hooked(kind, ptype, payload)
        if when is None:
            if kind == "event_msg" and ptype == "token_count" or responded(kind, ptype, payload):
                found["accounted"] = True  # format drift: usage at a time this reader cannot tell
            continue
        if when < started - SLACK:
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
    return {**found, "own": {**own, "turns": named} if naming else None}


# -- the account's attested usage -----------------------------------------------


def alike(first, second):
    """Whether two windows (as window() reads them, or None) are the same reading: both null, or
    the same length and percentage with reset times at most TOLERANCE apart, or both without one."""
    if first is None or second is None:
        return first is second
    if (first["window_minutes"], first["used_percent"]) != (second["window_minutes"], second["used_percent"]):
        return False
    if first["resets_at"] is None or second["resets_at"] is None:
        return first["resets_at"] is second["resets_at"]
    return abs(first["resets_at"] - second["resets_at"]) <= TOLERANCE


def merged(stored, incoming, now):
    """The usage to store when the snapshot INCOMING (as session() builds one) meets the STORED
    usage (or None), or None to keep the stored one.

    A stored seen_at that is no int, or lies more than SLACK seconds after NOW, is no stored usage.
    The newest reading wins. A stored window whose length the result does not name stays, in a slot
    the result leaves null, while its reset lies ahead. Unchanged windows are written again only
    AGAIN seconds apart."""
    last = stored.get("seen_at") if isinstance(stored, dict) else None
    if type(last) is not int or last > now + SLACK:
        stored, last = None, None  # none, or stamped by a clock that runs ahead
    if last is not None and incoming["seen_at"] <= last:
        return None
    result = {"primary": incoming["primary"], "secondary": incoming["secondary"], "seen_at": incoming["seen_at"]}
    old = {}
    for key in ("primary", "secondary"):
        try:
            old[key] = window(stored.get(key)) if stored else None
        except ValueError:
            old[key] = None
    named = {value["window_minutes"] for value in (result["primary"], result["secondary"]) if value}
    for key in ("primary", "secondary"):  # by length, as the guard reads windows, not by slot
        value = old[key]
        if value and value["window_minutes"] and value["window_minutes"] not in named and (value["resets_at"] or 0) > now:
            free = next((slot for slot in ("primary", "secondary") if result[slot] is None), None)
            if free:
                result[free] = value
                named.add(value["window_minutes"])
    if last is not None and incoming["seen_at"] - last < AGAIN and all(
            alike(result[key], old[key]) for key in ("primary", "secondary")):
        return None
    return result


def keep(accounts, path, owner, snapshot, now, rollout, launch):
    """Merge SNAPSHOT into the account's attested usage at PATH (merged()); True when it wrote.

    ACCOUNTS is the account metadata module, which reads and writes the file. PATH is a str or a
    Path; OWNER the tag of the launch's login; ROLLOUT and LAUNCH say where the reading came from
    (REL, the base name of the launch's state). ValueError when PATH is not an absolute
    codex-usage-NAME.json in an owned, real 0700 directory, or OWNER is no tag. Nothing is written
    when SNAPSHOT's seen_at is no int or lies more than SLACK seconds after NOW. A file that
    ACCOUNTS cannot read counts as absent, and another owner's usage as none: both are replaced.
    OSError when the write fails. There is no lock: two writers at one instant can let the older
    reading stand until the next write."""
    path = Path(path)
    if not path.is_absolute() or not ATTESTED.fullmatch(path.name):
        raise ValueError(f"{path}: not an attested usage file")
    if not isinstance(owner, str) or not OWNER.fullmatch(owner):
        raise ValueError("not the tag of a login")
    folder = os.lstat(path.parent)
    if not stat.S_ISDIR(folder.st_mode) or folder.st_uid != os.getuid() or stat.S_IMODE(folder.st_mode) != 0o700:
        raise ValueError(f"{path.parent}: not an owned, real directory with permissions 0700")
    seen = snapshot.get("seen_at") if isinstance(snapshot, dict) else None
    if type(seen) is not int or seen > now + SLACK:
        return False
    try:
        data = accounts.read(path)
    except ValueError:
        data = None  # unusable: replaced
    stored = data.get("usage") if isinstance(data, dict) and data.get("owner") == owner else None
    fresh = merged(stored, snapshot, now)
    if fresh is None:
        return False
    return accounts.write(path, {"owner": owner, "usage": fresh, "rollout": rollout, "launch": launch})
