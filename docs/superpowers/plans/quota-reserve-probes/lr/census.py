"""LR: the one-shot census read of an enrolled Claude account's usage rows.

Research code, not launcher runtime. It composes the product's modules (loaded by path, read-only)
and adds no credential, quota or network logic of its own:
- lib/coding-agent-kit/config.py: the kit configuration (CODING_AGENT_KIT_CONFIG, else
  XDG_CONFIG_HOME, else HOME), its secrets_dir and the account's claude_auth mode;
- lib/claude-as/native.py: the ready native-login registration and its two identity UUIDs;
- lib/claude-as/provider.py: the gate's memory-only bearer read (bearer.py: .credentials.json on
  Linux, the Keychain item through /usr/bin/security on macOS), its request paths and headers, its
  strict JSON parse, its profile identity and its time parse;
- lib/coding-agent-kit/gate.py: Gate.fetch (verified TLS, the neutral User-Agent quota-probe/1, the
  10 s read bound, no retry or redirect), through the proxy supervise.capture_proxy captures by the
  launcher's rule for Claude Code (HTTPS_PROXY, then HTTP_PROXY, then ALL_PROXY, each spelling; a
  plain http:// one only; NO_PROXY as the gate reads it).

Only the coordinator runs it, once, under the user's explicit one-shot permission for the account
(gate-decisions "Claude usage rows": the follow-up census read waits for that approval). The offline
tests (test_census.py) use a temporary HOME, a fabricated credential and the loopback fixture upstream.

Usage (python3 -I -B, stdlib only, Linux or macOS):
    census.py --alias NAME --out DIR --kit-commit SHA

Local refusals come first and consume nothing: the configuration must select native-login for NAME,
its registration must be ready, DIR must be an existing directory of this user without group or other
permission bits, outside any Git worktree and the secrets directory, the proxy must be usable, and DIR
must hold no earlier attempt. Then DIR/census.attempt is created (0600, exclusively): the attempt is
consumed. The bearer is read memory-only; an account that is not pro or max is refused there, as the
gate's startup refuses it, before any request. Then exactly one GET /api/oauth/profile and, only when
it proves the registered identity (compared in memory; nothing of the profile is kept), exactly one
GET /api/oauth/usage. The bearer and both bodies are dropped before DIR/census.result.json (0600,
exclusively) is written; the same document goes to stdout.

Retained, and nothing else (research RQ section 4; the result's keys in brackets):
1. the read: [time_utc] UTC to the minute, [kit_commit], per GET [http_status], [json] (a JSON
   content type), the profile's [identity_match] and the usage body's [object]; for a read that got
   no reply, the gate's fixed reason word [error]; the [outcome] and a fixed refusal [reason].
2. per top-level key, in server order: [name] when it matches [a-z0-9_]{1,64}, else "unknown"; [type]:
   null, object, empty_object, array, empty_array, string, number, boolean.
3. object values: the sorted field names [fields] (the same pattern, else "unknown"); [utilization]
   rounded to 0.1 (or its type when not a number, "absent" when missing); [resets_at]: absent, null,
   malformed or the hours from the read rounded to 0.1; for extra_usage also [is_enabled] (a boolean,
   else its type). No dollar amount, currency, label, notice, period or disabled_reason.
4. array values: their [length]; for limits, the first 16 [entries]: each one's [type] and, for an
   object, [kind], [group] and [severity] when each matches [a-z0-9_]{1,32} (else "other", "absent"
   when missing), [is_active], [percent] and [resets_at] as above, [same_reset_as] five_hour, seven_day
   or none (exact comparison of the resets_at text), [scope] none, model, surface, both or other, the
   scope's [model_display_name] and [surface_display_name] only when they match [A-Za-z0-9 .]{1,32}
   (else "other"), and the names of its other fields [other_fields].
5. a top-level string, number or boolean: its type only.
Never kept: a body, header, token, account or organization UUID, e-mail, dollar amount, currency,
label or any other value.

Exit codes:
    0  both GETs were answered and the usage body summarized (outcome "read")
    1  the attempt was consumed and the outcome is another one
    2  usage error (arguments)
    3  refused before any account effect: no marker, nothing read or sent
    4  refused: DIR already holds a marker or a result
    5  internal error
"""

import sys

if sys.version_info < (3, 11):
    print("census: Python 3.11 or later is needed.", file=sys.stderr)
    raise SystemExit(5)

import argparse
from decimal import Decimal
import importlib.util
import json
import math
import os
from pathlib import Path
import pwd
import re
import stat
import time

HERE = Path(os.path.realpath(__file__)).parent
ROOT = HERE.parents[4]  # lr -> probes -> plans -> superpowers -> docs -> the checkout

SCHEMA = "lr-census-1"
MARKER, RESULT = "census.attempt", "census.result.json"
EXIT_OK, EXIT_NOT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_ATTEMPTED, EXIT_INTERNAL = 0, 1, 2, 3, 4, 5
NAME = re.compile(r"[a-z0-9_]{1,64}")
WORD = re.compile(r"[a-z0-9_]{1,32}")
LABEL = re.compile(r"[A-Za-z0-9 .]{1,32}")
COMMIT = re.compile(r"[0-9a-f]{7,40}")
ALIAS = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
ENTRIES_SHOWN = 16
ENTRY_FIELDS = ("kind", "group", "percent", "resets_at", "severity", "is_active", "scope")
BOUND = 100000  # a rounded number or hour count beyond this is kept as "out_of_range"


class Refused(Exception):
    """A fixed reason word, and the exit code."""

    def __init__(self, reason, code=EXIT_REFUSED):
        super().__init__(reason)
        self.reason, self.code = reason, code


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Modules:
    """The product modules, from this checkout's files (never from sys.path)."""

    def __init__(self):
        try:
            self.config = _load("lr_census_config", ROOT / "lib/coding-agent-kit/config.py")
            self.native = _load("lr_census_native", ROOT / "lib/claude-as/native.py")
            self.provider = _load("lr_census_provider", ROOT / "lib/claude-as/provider.py")
            self.supervise = _load("lr_census_supervise", ROOT / "lib/coding-agent-kit/supervise.py")
        except (OSError, ImportError, SyntaxError, SystemExit):
            raise Refused("incomplete_checkout") from None
        self.gate = self.provider.gate  # the provider's own instance, so its classes agree


class Wiring:
    """What the live read touches; the offline tests substitute the loopback fixture."""

    upstream = None  # None: the provider's api.anthropic.com:443 over verified TLS
    security = None  # None: /usr/bin/security (macOS)
    wall = staticmethod(time.time)


# -- local checks -----------------------------------------------------------------------------------

def _inside(path, ancestor):
    try:
        return os.path.commonpath([path, ancestor]) == ancestor
    except ValueError:
        return False


def _in_git_worktree(real):
    current = real
    while True:
        if os.path.lexists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def open_out_dir(path, secrets):
    """A descriptor of the private result directory PATH, or Refused."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise Refused("out_dir_not_absolute")
    try:
        info = os.lstat(path)
    except OSError:
        raise Refused("out_dir_missing") from None
    if not stat.S_ISDIR(info.st_mode):
        raise Refused("out_dir_not_directory")  # a symlink included
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused("out_dir_not_private")
    real = os.path.realpath(path)
    if _in_git_worktree(real):
        raise Refused("out_dir_in_git_worktree")
    if _inside(real, os.path.realpath(secrets)):
        raise Refused("out_dir_in_secrets")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise Refused("out_dir_unusable") from None
    opened = os.fstat(fd)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        raise Refused("out_dir_changed")
    return fd


def _dump(document):
    return json.dumps(document, sort_keys=True, ensure_ascii=True, indent=2) + "\n"


def _exclusive(directory, name, document):
    """Create DIRECTORY/NAME (0600, exclusively) holding DOCUMENT."""
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                     dir_fd=directory)
    except FileExistsError:
        raise Refused("already_attempted", EXIT_ATTEMPTED) from None
    with os.fdopen(fd, "w", encoding="ascii") as stream:
        stream.write(_dump(document))
        stream.flush()
        os.fsync(stream.fileno())


# -- the summary --------------------------------------------------------------------------------------

def kind(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object" if value else "empty_object"
    if isinstance(value, list):
        return "array" if value else "empty_array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    return "other"


def _name(text):
    return text if isinstance(text, str) and NAME.fullmatch(text) else "unknown"


def _rounded(value):
    number = float(value)
    return round(number, 1) if math.isfinite(number) and abs(number) <= BOUND else "out_of_range"


def _number(item, key):
    if key not in item:
        return "absent"
    value = item[key]
    return _rounded(value) if kind(value) == "number" else kind(value)


def _flag(item, key):
    if key not in item:
        return "absent"
    return item[key] if isinstance(item[key], bool) else kind(item[key])


def _word(item, key):
    if key not in item:
        return "absent"
    value = item[key]
    return value if isinstance(value, str) and WORD.fullmatch(value) else "other"


def _reset(provider, item, wall):
    if "resets_at" not in item:
        return "absent"
    value = item["resets_at"]
    if value is None:
        return "null"
    moment = provider.epoch(value)
    if not isinstance(moment, Decimal):
        return "malformed"
    return _rounded((moment - Decimal(repr(float(wall)))) / 3600)


def _scope(value):
    if value is None:
        return {"scope": "none"}
    if not isinstance(value, dict):
        return {"scope": "other"}
    parts = {key: value.get(key) for key in ("model", "surface")}
    if any(item is not None for key, item in value.items() if key not in parts) \
            or any(part is not None and not isinstance(part, dict) for part in parts.values()):
        where = "other"
    else:
        where = {(False, False): "none", (True, False): "model", (False, True): "surface",
                 (True, True): "both"}[(parts["model"] is not None, parts["surface"] is not None)]
    summary = {"scope": where}
    for key, part in parts.items():
        if isinstance(part, dict) and "display_name" in part:
            label = part["display_name"]
            summary[key + "_display_name"] = label if isinstance(label, str) and LABEL.fullmatch(label) else "other"
    return summary


def _entry(provider, item, wall, general):
    summary = {"type": kind(item)}
    if summary["type"] != "object":
        return summary
    for key in ("kind", "group", "severity"):
        summary[key] = _word(item, key)
    summary.update(is_active=_flag(item, "is_active"), percent=_number(item, "percent"),
                   resets_at=_reset(provider, item, wall))
    reset = item.get("resets_at")
    summary["same_reset_as"] = next((window for window in ("five_hour", "seven_day")
                                     if isinstance(reset, str) and general.get(window) == reset), "none")
    summary.update(_scope(item.get("scope")))
    summary["other_fields"] = sorted({_name(field) for field in item if field not in ENTRY_FIELDS})
    return summary


def summarize(provider, document, wall):
    """The retained summary of one usage document's top-level keys, in server order."""
    general = {window: document[window].get("resets_at") for window in ("five_hour", "seven_day")
               if isinstance(document.get(window), dict)}
    keys = []
    for name, value in document.items():
        entry = {"name": _name(name), "type": kind(value)}
        if entry["type"] == "object":
            entry.update(fields=sorted({_name(field) for field in value}), utilization=_number(value, "utilization"),
                         resets_at=_reset(provider, value, wall))
            if name == "extra_usage":
                entry["is_enabled"] = _flag(value, "is_enabled")
        elif entry["type"] == "array":
            entry["length"] = len(value)
            if name == "limits":
                entry["entries"] = [_entry(provider, item, wall, general) for item in value[:ENTRIES_SHOWN]]
        keys.append(entry)
    return keys


# -- the read -----------------------------------------------------------------------------------------

def _json_type(headers):
    kind_ = next((value for name, value in headers if name.lower() == "content-type"), "")
    return kind_.split(";")[0].strip().lower() == "application/json"


def _fetch(modules, g, path, presented):
    """One GET as the provider sends it: (status, headers, body), or raise Unavailable."""
    return g.fetch("GET", path, secret=presented.secret,
                   headers=(("Accept", "application/json"), ("anthropic-beta", modules.provider.OAUTH_BETA)))


def read(modules, claude, g, identity, result, wiring):
    """The census record: the bearer read, one profile GET and, for the registered identity, one usage GET."""
    gate, provider = modules.gate, modules.provider
    try:
        credential = claude.credential()  # memory-only, as the gate reads it
    except gate.Refusal as error:
        return dict(result, outcome="refused", reason=error.reason if NAME.fullmatch(error.reason) else "credential")
    personal = credential.subscription in provider.PERSONAL_PLANS
    presented = gate.Presented(credential.secret)
    credential = None
    try:
        if not personal:
            return dict(result, outcome="refused", reason="organization_policy")
        try:
            status, headers, body = _fetch(modules, g, provider.PROFILE_PATH, presented)
        except gate.Unavailable as error:
            return dict(result, outcome="profile_unavailable", profile={"error": _name(error.reason)})
        profile = {"http_status": status, "json": _json_type(headers), "identity_match": False}
        if status == 200 and profile["json"]:
            try:
                profile["identity_match"] = provider.normalize_profile(provider._json(headers, body)) == identity
            except (ValueError, UnicodeDecodeError, RecursionError):
                pass
        body = None
        result = dict(result, profile=profile)
        if not profile["identity_match"]:
            return dict(result, outcome="identity_unproved")
        try:
            status, headers, body = _fetch(modules, g, provider.USAGE_PATH, presented)
        except gate.Unavailable as error:
            return dict(result, outcome="usage_unavailable", usage={"error": _name(error.reason)})
        wall = wiring.wall()
        usage = {"http_status": status, "json": _json_type(headers)}
        result = dict(result, usage=usage)
        if status != 200 or not usage["json"]:
            return dict(result, outcome="usage_unavailable")
        try:
            document = provider._json(headers, body)
        except (ValueError, UnicodeDecodeError, RecursionError):
            return dict(result, outcome="usage_malformed")
        finally:
            body = None
        usage["object"] = isinstance(document, dict)
        if not usage["object"]:
            return dict(result, outcome="usage_malformed")
        usage["keys"] = summarize(provider, document, wall)
        document = None
        return dict(result, outcome="read")
    finally:
        presented = None


def run(args, wiring, out):
    if not ALIAS.fullmatch(args.alias):
        raise Refused("alias_invalid")
    if not COMMIT.fullmatch(args.kit_commit):
        raise Refused("kit_commit_invalid")
    modules = Modules()
    try:
        config = modules.config.load()
    except modules.config.ConfigError:
        raise Refused("config_error") from None
    if config.claude_mode(args.alias) != "native-login":
        raise Refused("not_native_login")
    native = modules.native
    try:
        paths = native.Paths(config, args.alias)
        record = native.load(paths)
    except native.NativeError:
        raise Refused("registration_unusable") from None
    if record is None or record.get("state") != "ready":
        raise Refused("registration_not_ready")
    identity = (record["identity"]["account_uuid"], record["identity"]["organization_uuid"])
    record = None
    directory = open_out_dir(args.out, paths.secrets)
    try:
        try:
            captured = modules.supervise.capture_proxy(os.environ, modules.gate.parse_proxy, provider="claude")
            proxy, no_proxy = modules.gate.proxy_setting(list(captured) if captured else None)
        except (modules.supervise.Refused, ValueError):
            raise Refused("proxy_unsupported") from None
        for name in (MARKER, RESULT):
            if os.path.lexists(os.path.join(args.out, name)):
                raise Refused("already_attempted", EXIT_ATTEMPTED)
        settings = {"identity": list(identity), "config_dir": paths.config,
                    "platform": "darwin" if sys.platform == "darwin" else "linux",
                    "keychain_user": os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name,
                    "home": str(config.home)}
        if wiring.upstream is not None:
            settings["upstream"] = dict(wiring.upstream)
        if wiring.security is not None:
            settings["security"] = wiring.security
        claude = modules.provider.ClaudeProvider(settings)
        g = modules.gate.Gate(claude, lambda event: modules.gate.Decision(False, "census"),
                              max_age=modules.provider.reserve.MAX_AGE, proxy=proxy, no_proxy=no_proxy)
        when = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(wiring.wall()))
        _exclusive(directory, MARKER, {"schema": SCHEMA, "step": "census", "alias": args.alias, "time_utc": when})
        result = read(modules, claude, g, identity, {"schema": SCHEMA, "kit_commit": args.kit_commit,
                                                    "time_utc": when}, wiring)
        _exclusive(directory, RESULT, result)
    finally:
        os.close(directory)
    out.write(_dump(result))
    return EXIT_OK if result["outcome"] == "read" else EXIT_NOT_OK


def main(argv=None, wiring=Wiring, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="census.py", description="One-shot Claude usage-row census read.")
    parser.add_argument("--alias", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--kit-commit", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return EXIT_USAGE
    try:
        return run(args, wiring, out)
    except Refused as error:
        out.write(_dump({"schema": SCHEMA, "outcome": "refused", "reason": error.reason}))
        return error.code
    except Exception:
        out.write(_dump({"schema": SCHEMA, "outcome": "internal_error"}))
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
