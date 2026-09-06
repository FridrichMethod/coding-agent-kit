"""Small, offline configuration transforms behind the shell sync entrypoints."""

from __future__ import annotations

import argparse
import copy
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from collections.abc import MutableMapping

TOMLKIT_VERSION = "0.15.1"

# Every owned field is required. Keep ownership, validation and migration in
# one policy, expressed as parsed TOML paths (not source-code regexes).
CODEX_POLICY = (
    (("model",), str),
    (("model_reasoning_effort",), str),
    (("plan_mode_reasoning_effort",), str),
    (("personality",), str),
    (("default_permissions",), str),
    (("approval_policy",), str),
    (("approvals_reviewer",), str),
    (("project_doc_fallback_filenames",), list),
    (("features", "network_proxy"), bool),
    (("features", "multi_agent"), bool),
    (("features", "memories"), bool),
    (("permissions", "workspace-net", "description"), str),
    (("permissions", "workspace-net", "extends"), str),
    (("permissions", "workspace-net", "network", "enabled"), bool),
    (("permissions", "workspace-net", "network", "allow_local_binding"), bool),
    (("permissions", "workspace-net", "network", "domains", "*"), str),
    (("memories", "generate_memories"), bool),
    (("memories", "use_memories"), bool),
)
CODEX_RETIRED = (
    ("sandbox_mode",),
    ("sandbox_workspace_write",),
    ("features", "js_repl"),
)


class SyncError(ValueError):
    """An unsafe or invalid synchronization request."""


def require_runtime():
    if sys.version_info < (3, 11):
        raise SyncError("Python 3.11 or newer is required; run setup-sync explicitly")
    try:
        installed = importlib.metadata.version("tomlkit")
    except importlib.metadata.PackageNotFoundError as exc:
        raise SyncError("missing tomlkit; run setup-sync.sh or setup-sync.ps1 explicitly") from exc
    if installed != TOMLKIT_VERSION:
        raise SyncError(f"tomlkit {TOMLKIT_VERSION} required, found {installed}; rerun setup-sync explicitly")


def parse_toml(data: bytes):
    import tomlkit

    return tomlkit.parse(data.decode("utf-8"))


def plain(value):
    return value.unwrap() if hasattr(value, "unwrap") else value


def codex_values(portable):
    values = []
    for path, expected in CODEX_POLICY:
        value = portable
        for key in path:
            if not isinstance(value, MutableMapping) or key not in value:
                raise SyncError(f"missing portable key: {'.'.join(path)}")
            value = value[key]
        unwrapped = plain(value)
        if type(unwrapped) is not expected or (
            expected is list and not all(isinstance(item, str) for item in unwrapped)
        ):
            raise SyncError(f"invalid portable value type: {'.'.join(path)}")
        values.append((path, value))
    return values


def set_toml_path(document, path, value):
    import tomlkit

    table = document
    for key in path[:-1]:
        if key not in table:
            table[key] = tomlkit.table()
        table = table[key]
        if not isinstance(table, MutableMapping):
            raise SyncError(f"live table conflicts with portable key: {'.'.join(path)}")
    key = path[-1]
    if key not in table or type(plain(table[key])) is not type(plain(value)) or plain(table[key]) != plain(value):
        table[key] = copy.deepcopy(value)


def retain_missing(source, target):
    """Explicit migration keeps old source-only state; current live wins conflicts."""
    for key, value in source.items():
        if key not in target:
            target[key] = copy.deepcopy(value)
        elif isinstance(value, MutableMapping) and isinstance(target[key], MutableMapping):
            retain_missing(value, target[key])


def merge_codex(portable_bytes: bytes, live_bytes: bytes, *, migrate=False) -> tuple[bytes, bytes]:
    import tomlkit

    portable = parse_toml(portable_bytes)
    values = codex_values(portable)
    live = parse_toml(live_bytes)
    if migrate:
        retain_missing(portable, live)
    for path in CODEX_RETIRED:
        table = live
        for key in path[:-1]:
            table = table.get(key) if isinstance(table, MutableMapping) else None
        if isinstance(table, MutableMapping):
            table.pop(path[-1], None)
    clean = tomlkit.document()
    for path, value in values:
        set_toml_path(live, path, value)
        set_toml_path(clean, path, value)
    merged = tomlkit.dumps(live).encode("utf-8")
    canonical = tomlkit.dumps(clean).encode("utf-8")
    # A parser successfully reading input is not enough: validate serialized
    # candidates before any filesystem mutation (including --check).
    parse_toml(merged)
    parse_toml(canonical)
    return merged, canonical


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SyncError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise SyncError(f"invalid JSON constant: {value}")


def parse_json(data: bytes):
    value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise SyncError("both settings documents must be JSON objects")
    return value


def deep_merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        result = dict(base)
        for key, value in override.items():
            result[key] = deep_merge(base[key], value) if key in base else copy.deepcopy(value)
        return result
    return copy.deepcopy(override)


def same_json(left, right):
    # bool is a subclass of int in Python: plain == mistakes false for 0,
    # including values nested inside arrays and objects.
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right


def merge_claude(portable_bytes: bytes, live_bytes: bytes) -> bytes:
    portable = parse_json(portable_bytes)
    live = parse_json(live_bytes or b"{}")
    permissions = portable.get("permissions")
    if not isinstance(permissions, dict) or any(
        not isinstance(permissions.get(key), list)
        or not all(isinstance(item, str) for item in permissions[key])
        for key in ("allow", "ask")
    ):
        raise SyncError("portable permissions.allow and permissions.ask must be string arrays")
    if "model" in portable:
        raise SyncError("portable Claude model is not allowed; keep model selection machine-local")
    merged = deep_merge(live, portable)
    # Preserve the original bytes on a semantic no-op, including whitespace.
    if same_json(merged, live) and live_bytes:
        return live_bytes
    result = (json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    parse_json(result)
    return result


def read_regular(path: Path, *, missing_ok: bool = False) -> bytes | None:
    try:
        path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise SyncError(f"missing portable file: {path}") from None
    try:
        info = path.stat()
    except FileNotFoundError:
        raise SyncError(f"dangling symlink is not a missing live file: {path}") from None
    if not stat.S_ISREG(info.st_mode):
        raise SyncError(f"not a regular file: {path}")
    if os.name != "nt" and not info.st_mode & 0o444:
        raise SyncError(f"unreadable file: {path}")
    return path.read_bytes()


def atomic_write(path: Path, data: bytes, mode: int, previous: bytes | None):
    """Replace the directory entry, never unlink or write through a live link."""
    # Defaults are an upper bound, not permission grants. Preserve a regular
    # file's tighter mode on both no-ops and replacements; symlinks use the
    # defaults without inheriting or changing their portable target's mode.
    if os.name != "nt" and previous is not None:
        existing = path.lstat()
        if stat.S_ISREG(existing.st_mode):
            mode &= stat.S_IMODE(existing.st_mode)
    if not path.is_symlink() and previous == data:
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != mode:
            path.chmod(mode)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(mode)
        if read_regular(path, missing_ok=True) != previous:
            raise SyncError(f"live file changed during synchronization; retry: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def synchronize(kind: str, portable: Path, live: Path, *, check=False, migrate=False):
    portable_bytes = read_regular(portable)
    live_bytes = read_regular(live, missing_ok=True)
    if portable.absolute() == live.absolute() or (
        live_bytes is not None and not live.is_symlink() and os.path.samefile(portable, live)
    ):
        raise SyncError("portable and live must not be the same regular file")
    if migrate and portable.is_symlink():
        raise SyncError("explicit migration requires a regular portable source, not a symlink")
    cleaned = None
    if kind == "codex-config-sync":
        merged, cleaned = merge_codex(portable_bytes, live_bytes or b"", migrate=migrate)
        mode = 0o600
    elif kind == "claude-settings-sync":
        merged = merge_claude(portable_bytes, live_bytes or b"")
        mode = 0o644
    else:
        if not portable_bytes:
            raise SyncError("missing or empty portable rules")
        merged = portable_bytes
        mode = 0o644
    if not check:
        atomic_write(live, merged, mode, live_bytes)
        # The live snapshot is materialized first so a polluted legacy source
        # can be cleaned without losing the runtime-only state it contained.
        if migrate:
            atomic_write(portable, cleaned, 0o644, portable_bytes)


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        require_runtime()
        if arguments == ["--runtime-check"]:
            print(f"config-sync runtime: Python {sys.version.split()[0]}, tomlkit {TOMLKIT_VERSION}")
            return 0
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("kind", choices=("codex-config-sync", "claude-settings-sync", "codex-rules-sync"))
        options = parser.add_mutually_exclusive_group()
        options.add_argument("--check", action="store_true", help="validate the complete merge without writing")
        options.add_argument("--migrate-portable", action="store_true", help="explicitly clean an old polluted Codex baseline after preserving its live state")
        parser.add_argument("--quiet", action="store_true", help="suppress success messages, but always report errors")
        parser.add_argument("portable", type=Path)
        parser.add_argument("live", type=Path)
        args = parser.parse_args(arguments)
        if args.migrate_portable and args.kind != "codex-config-sync":
            parser.error("--migrate-portable is only supported by codex-config-sync")
        synchronize(args.kind, args.portable, args.live, check=args.check, migrate=args.migrate_portable)
        if not args.quiet:
            label = {
                "codex-config-sync": "portable Codex settings",
                "claude-settings-sync": "portable Claude settings",
                "codex-rules-sync": "portable Codex rules",
            }[args.kind]
            print(f"{'Validated' if args.check else 'Synchronized'} {label} into {args.live}")
        return 0
    except (OSError, ValueError, ImportError) as exc:
        print(f"config-sync: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # tomlkit's parser errors do not all inherit ValueError. Keep malformed
        # input fail-closed and report its diagnostic without a traceback.
        from tomlkit.exceptions import TOMLKitError

        if isinstance(exc, TOMLKitError):
            print(f"config-sync: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
