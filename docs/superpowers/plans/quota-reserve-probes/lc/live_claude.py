"""LC: coordinator-run live driver for Claude HLogin + N1 on one named alias.

Research code, not launcher runtime. It composes the reviewed QC modules
(n1_harness.py, bearer.py, usage.py; imported by path, read-only) and the
kit's own configuration loader (lib/coding-agent-kit/config.py). It adds no
login, credential, quota or network logic of its own.

Only the coordinator runs it against a real account, and only within the
named-alias permission recorded in the ledger (HLogin, N1-native, N1-bearer;
one attempt each). Its offline tests use temporary homes, a fake native and
loopback fixtures.

Usage (python3 -I -B, stdlib only, Linux):
    live_claude.py pending --alias NAME --out DIR
    live_claude.py ready   --alias NAME --out DIR [--confirm]
    live_claude.py n1      --alias NAME --out DIR --binary PATH [--no-tunnel]
    live_claude.py bearer  --alias NAME --out DIR

pending  Resolves secrets_dir exactly as the launchers do (load() of the kit
         config: CODING_AGENT_KIT_CONFIG, else XDG_CONFIG_HOME, else HOME), runs
         create_pending and prints the two user commands with the config
         directory spelled exactly (no trailing slash). A second run refuses
         and prints no login command.
ready    Reads the two UUIDs from the account-local metadata and reports only
         whether each is present. With --confirm (the user confirmed the
         account and organization that `claude auth status` displayed) it
         calls mark_ready with them; without it, it refuses and changes
         nothing.
n1       One-shot. Local refusals first (registration ready, caller
         environment, every policy source, binary SHA-256 equal to the
         reviewed native 2.1.322, local identity, private XDG_RUNTIME_DIR);
         then DIR/n1.attempt is created exclusively and run_n1 runs with the
         reviewed bounds (120 s total, 45 s per operation, at most two
         get_usage with one 65 s cache-shortcut wait) on the resolved binary.
         The accounting tunnel (api.anthropic.com:443 and
         platform.claude.com:443 only) is on unless --no-tunnel.
bearer   One-shot, only after an n1 result whose native run completed with
         the identity unchanged and no server-managed policy. DIR/bearer.attempt
         is created before the credential read; then one memory-only access
         bearer read, exactly one usage GET and one profile GET (5 s socket,
         10 s total, 64 KiB, no retry, redirect or refresh). The bearer is
         dropped before the result is written.

DIR must be an existing directory owned by this user with no group or other
permission bits, not a symlink, outside any Git worktree and outside the
secrets directory. Results are DIR/<step>.result.json (0600, created
exclusively). n1 and bearer write theirs only after their attempt file
exists, and their preflight refusals write no file (they consume nothing). A
pending or ready rerun never replaces the first record; it writes
DIR/<step>.rerun.json instead.

Output: one JSON document on stdout (sorted keys, ASCII), the same document
as the result file except that pending adds user_commands and user_notes.
Retained fields are allowlisted: reason codes, booleans, counts, normalized
windows, allowlisted row names and error types. Never a token, UUID, e-mail,
raw record or raw body. stderr stays empty except for argument errors.

Exit codes:
    0  the step completed and its outcome is the success outcome
    1  the step ran (attempt consumed for n1/bearer) but the outcome is not
       success, or it was interrupted
    2  usage error (argument parsing)
    3  refused before any effect; nothing created or consumed
    4  refused: the step's attempt or result file already exists
    5  internal error
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import stat
import sys
import time
import uuid

_HERE = Path(os.path.realpath(__file__)).parent
_QC = _HERE.parent / "qc"
_KIT_ROOT = _HERE.parents[4]  # lc -> probes -> plans -> superpowers -> docs -> root

SCHEMA = "lc-live-claude-1"
# Native Claude Code 2.1.322, the version the QC policy-key review covers.
EXPECTED_NATIVE_SHA256 = "bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478"
EXIT_OK, EXIT_NOT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_ATTEMPTED, EXIT_INTERNAL = 0, 1, 2, 3, 4, 5
# n1 outcomes after which native has run to completion on the account.
N1_COMPLETED = ("fresh_windows", "inconclusive", "unavailable")
ERROR_TYPES = ("authentication_error", "permission_error", "rate_limit_error", "not_found_error",
               "invalid_request_error", "api_error", "overloaded_error")
UUID_RE = re.compile(r"[A-Za-z0-9-]{1,64}")
RESULT_LIMIT = 256 * 1024
USER_NOTES = (
    "Run both commands in your own terminal, in a shell that sets none of CLAUDE_CODE_OAUTH_TOKEN, "
    "ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL or another auth, provider or proxy variable.",
    "Complete one browser sign-in; on failure or cancel the registration stays pending and no retry is approved.",
    "Confirm to the coordinator the account and organization that auth status displays; nothing else is needed.",
)


class Refused(Exception):
    """Fixed reason code only; extra holds allowlisted fields for the output."""

    def __init__(self, reason, code=EXIT_REFUSED, **extra):
        super().__init__(reason)
        self.reason, self.code, self.extra = reason, code, extra


class _Interrupted(BaseException):
    pass


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Modules:
    """The reviewed modules, loaded once from their files (never from sys.path)."""

    def __init__(self):
        try:
            self.n1 = _load(_QC / "n1_harness.py", "lc_qc_n1_harness")
            self.config = _load(_KIT_ROOT / "lib/coding-agent-kit/config.py", "lc_kit_config")
        except (OSError, ImportError, SyntaxError):
            raise Refused("incomplete_checkout") from None
        # The harness's own instances, so refusal classes and constants agree.
        self.bearer = self.n1.bearer_mod
        self.usage = self.n1.usage_mod


def _live_connector(host, port):
    """Tunnel upstream for live use: only the reviewed hosts, bounded connect."""
    if "%s:%d" % (host, port) not in _LIVE_TUNNEL_HOSTS:
        raise OSError("host not allowed")
    return socket.create_connection((host, port), timeout=10)


_LIVE_TUNNEL_HOSTS = ("api.anthropic.com:443", "platform.claude.com:443")


class Wiring:
    """Everything the live path touches. The CLI uses this class unchanged;
    offline tests substitute loopback fixtures through a subclass."""

    expected_native_sha256 = EXPECTED_NATIVE_SHA256
    n1_options = {}  # extra run_n1 keyword arguments (tests: managed_dirs, platform, deadlines)
    workspace_parent = None  # None: the caller's private XDG_RUNTIME_DIR, required
    tunnel_connector = staticmethod(_live_connector)
    usage_endpoint = None  # None: usage.PRODUCTION
    profile_endpoint = None  # None: usage.PRODUCTION_PROFILE
    tls_context = None  # None: usage.production_context() (system trust, host name check)
    socket_timeout = 5.0
    total_deadline = 10.0
    resolver = staticmethod(socket.getaddrinfo)


def _utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _dump(document):
    return json.dumps(document, sort_keys=True, ensure_ascii=True, indent=2) + "\n"


# --- configuration and paths -------------------------------------------------

def resolve_paths(modules, alias):
    """secrets_dir through the kit loader, then the harness's fixed alias paths."""
    try:
        config = modules.config.load()
    except modules.config.ConfigError:
        raise Refused("config_error") from None
    secrets = str(config.secrets_dir)
    if not secrets.isascii():
        raise Refused("secrets_dir_not_ascii")
    try:
        paths = modules.n1.AccountPaths(secrets, alias)
    except modules.n1.HarnessRefused as error:
        raise Refused(error.reason) from None
    return config, paths


def _inside(path, ancestor):
    try:
        return os.path.commonpath([path, ancestor]) == ancestor
    except ValueError:
        return False


def _in_git_worktree(real):
    current = real
    while True:
        try:
            os.lstat(os.path.join(current, ".git"))
            return True
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError:
            raise Refused("out_dir_unusable") from None
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def open_out_dir(path, secrets_dir, uid):
    """Return a directory fd for the private result directory, or refuse."""
    if not isinstance(path, str) or not path.startswith("/") or not path.isascii():
        raise Refused("out_dir_not_absolute")
    try:
        info = os.lstat(path)
    except OSError:
        raise Refused("out_dir_missing") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise Refused("out_dir_not_directory")
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) & 0o077:
        raise Refused("out_dir_not_private")
    real = os.path.realpath(path)
    if _in_git_worktree(real):
        raise Refused("out_dir_in_git_worktree")
    if _inside(real, os.path.realpath(secrets_dir)):
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


# --- result files ------------------------------------------------------------

def _write(directory, name, document, exclusive=True):
    """0600 JSON by descriptor; exclusive create, or staged atomic replace."""
    target = name if exclusive else ".%s.%s" % (name, uuid.uuid4().hex)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=directory)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(_dump(document))
            handle.flush()
            os.fsync(handle.fileno())
        if not exclusive:
            os.rename(target, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except BaseException:
        if not exclusive:
            try:
                os.unlink(target, dir_fd=directory)
            except OSError:
                pass
        raise


def _exists(directory, name):
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise Refused("out_dir_unusable") from None
    return True


def _read_result(directory, name, uid):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    except OSError:
        raise Refused("n1_result_missing") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1 or not 0 < info.st_size <= RESULT_LIMIT):
            raise Refused("n1_result_not_private")
        raw = os.read(fd, RESULT_LIMIT + 1)
    finally:
        os.close(fd)
    try:
        return json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise Refused("n1_result_malformed") from None


class Run:
    """Per-invocation state: whether this run created its step's attempt file."""

    def __init__(self, out):
        self.out, self.attempt_created = out, False

    def create_attempt(self, step):
        """Exclusive 0600 marker created before any spawn, credential read or network."""
        try:
            _write(self.out, step + ".attempt", {"schema": SCHEMA, "step": step, "created_utc": _utc()})
        except FileExistsError:
            raise Refused("attempt_exists", EXIT_ATTEMPTED) from None
        self.attempt_created = True


# --- steps -------------------------------------------------------------------

def step_pending(modules, config, paths, run, args, wiring):
    n1 = modules.n1
    document = {"registration_state": None}
    try:
        n1.create_pending(paths)
    except n1.HarnessRefused as error:
        try:
            state = n1.load_registration(paths)["state"]
        except n1.HarnessRefused:
            state = None
        raise Refused(error.reason, registration_state=state) from None
    document["registration_state"] = n1.load_registration(paths)["state"]
    document["config_directory_trailing_slash"] = paths.config.endswith("/")
    claude = shlex.quote(config.claude)
    variable = "CLAUDE_CONFIG_DIR=" + shlex.quote(paths.config)
    printed = {"user_commands": [variable + " " + claude + " auth login",
                                 variable + " " + claude + " auth status"],
               "user_notes": list(USER_NOTES)}
    return EXIT_OK, "pending_created", document, printed


def step_ready(modules, config, paths, run, args, wiring):
    n1 = modules.n1
    record = n1.load_registration(paths)
    if record["state"] != "pending":
        raise Refused("registration_not_pending", registration_state=record["state"])
    local = n1.read_metadata(paths.config)
    identity = {"account_uuid": local["account_uuid"], "organization_uuid": local["organization_uuid"]}
    local = None
    present = {key: isinstance(value, str) and bool(UUID_RE.fullmatch(value)) for key, value in identity.items()}
    try:
        policy = n1.server_managed_present(paths.config)
    except n1.HarnessRefused:
        policy = None
    document = {"account_uuid_present": present["account_uuid"],
                "organization_uuid_present": present["organization_uuid"],
                "server_policy_present": policy, "registration_state": "pending", "marked_ready": False}
    try:
        if not args.confirm:
            raise Refused("confirmation_required", **document)
        if not all(present.values()):
            raise Refused("identity_missing", **document)
        n1.mark_ready(paths, identity)
    finally:
        identity = None
    document.update(registration_state=n1.load_registration(paths)["state"], marked_ready=True)
    return EXIT_OK, "marked_ready", document, {}


def _bounds_n1(modules, wiring):
    n1 = modules.n1
    limits = {"total_s": n1.TOTAL_DEADLINE, "operation_s": n1.OPERATION_DEADLINE,
              "persistence_s": n1.PERSISTENCE_WAIT, "shortcut_wait_s": n1.CACHE_SHORTCUT_WAIT}
    names = {"total": "total_s", "operation": "operation_s", "persistence": "persistence_s",
             "shortcut": "shortcut_wait_s"}
    for key, value in (wiring.n1_options.get("deadlines") or {}).items():
        limits[names[key]] = value
    limits["max_get_usage"] = 2
    return limits


def _conflicting(modules):
    return sorted(name for name in modules.n1.REFUSED_ENV if name in os.environ)


def step_n1(modules, config, paths, run, args, wiring):
    n1 = modules.n1
    options = dict(wiring.n1_options)
    platform = options.pop("platform", None)
    managed_dirs = options.pop("managed_dirs", None)
    # Local refusals through the harness's own checks; nothing spawned or consumed yet.
    registration = n1.load_registration(paths)
    if registration["state"] != "ready":
        raise Refused("registration_pending")
    caller_env = dict(os.environ)
    try:
        n1.check_environment(caller_env)
    except n1.HarnessRefused as error:
        raise Refused(error.reason, conflicting_names=_conflicting(modules)) from None
    n1.check_effective_policy(paths.config, registration, n1.detect_platform() if platform is None else platform,
                              managed_dirs)
    binary = args.binary
    if not binary.startswith("/") or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        raise Refused("native_binary_unusable")
    resolved = os.path.realpath(binary)  # hash and spawn the same file
    with open(resolved, "rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != wiring.expected_native_sha256:
        raise Refused("native_version_unreviewed")
    local = n1.read_metadata(paths.config)
    matches = (local["account_uuid"], local["organization_uuid"]) == (
        registration["identity"]["account_uuid"], registration["identity"]["organization_uuid"])
    local = None
    if not matches:
        raise Refused("identity_mismatch_before")
    parent = wiring.workspace_parent or n1.default_workspace_parent(caller_env)
    if parent is None:
        raise Refused("xdg_runtime_dir_not_private")
    n1.check_workspace_ancestors(os.path.join(parent, "quota-qc-n1-preflight"))
    if _exists(run.out, "n1.result.json"):
        raise Refused("result_exists", EXIT_ATTEMPTED)
    run.create_attempt("n1")
    document = {"attempt_consumed": True, "tunnel": not args.no_tunnel, "requery_allowed": True,
                "bounds": _bounds_n1(modules, wiring)}
    try:
        result = n1.run_n1(resolved, paths, caller_env, managed_dirs=managed_dirs, platform=platform,
                           allow_requery=True,
                           tunnel_connector=None if args.no_tunnel else wiring.tunnel_connector,
                           workspace_parent=parent, **options)
    except _Interrupted:
        document.update(native_sha256_matches=None, harness=None)
        return EXIT_NOT_OK, "interrupted", document, {}
    document["native_sha256_matches"] = result.get("native_sha256") == wiring.expected_native_sha256
    document["harness"] = result
    document["reason"] = result.get("reason")
    ok = result.get("classification") == "fresh_windows" and document["native_sha256_matches"]
    return (EXIT_OK if ok else EXIT_NOT_OK), result.get("classification"), document, {}


def _n1_completed(document):
    harness = document.get("harness") if isinstance(document, dict) else None
    return (isinstance(harness, dict) and document.get("schema") == SCHEMA and document.get("step") == "n1"
            and document.get("status") in N1_COMPLETED and document.get("native_sha256_matches") is True
            and harness.get("classification") in N1_COMPLETED and harness.get("identity_matches_before") is True
            and harness.get("identity_matches_after") is True and harness.get("server_policy_written") is False)


def _names(names, allowed):
    kept = sorted(name for name in names if name in allowed)
    return kept, len(names) - len(kept)


def _usage_summary(modules, observation):
    usage = modules.usage
    error = observation.error_type
    families, other_families = _names(list(observation.other_families), usage.OTHER_FAMILY)
    unproved, other_unproved = _names(list(observation.unproved_rows), usage.OTHER_FAMILY)
    return {"outcome": observation.outcome, "http_status": observation.http_status,
            "error_type": error if error is None or error in ERROR_TYPES else "other",
            "fresh": observation.fresh, "valid_general": observation.valid_for({"five_hour", "weekly"}),
            "windows": {k: v.as_dict() for k, v in sorted(observation.windows.items())},
            "window_states": {k: v for k, v in sorted(observation.window_states.items()) if k in usage.GENERAL},
            "other_families": families, "other_family_rows_unlisted": other_families,
            "unknown_rows": observation.unknown_rows,
            "unproved_rows": unproved, "unproved_rows_unlisted": other_unproved}


def step_bearer(modules, config, paths, run, args, wiring):
    n1, bearer_mod, usage = modules.n1, modules.bearer, modules.usage
    uid = os.getuid()
    registration = n1.load_registration(paths)
    if registration["state"] != "ready":
        raise Refused("registration_pending")
    expected = registration["identity"]
    try:
        n1.check_environment(os.environ)
    except n1.HarnessRefused as error:
        raise Refused(error.reason, conflicting_names=_conflicting(modules)) from None
    read_env = dict(os.environ)
    read_env.setdefault("CLAUDE_CONFIG_DIR", paths.config)
    try:
        bearer_mod.check_store_selection(read_env, paths.config)
    except bearer_mod.BearerRefused as error:
        raise Refused(error.reason) from None
    previous = _read_result(run.out, "n1.result.json", uid)
    if not _n1_completed(previous):
        raise Refused("n1_not_completed")
    n1_classification = previous["harness"]["classification"]
    previous = None
    if n1.server_managed_present(paths.config):
        raise Refused("policy_server_managed_present")
    local = n1.read_metadata(paths.config)
    matches = (local["account_uuid"], local["organization_uuid"]) == (
        expected["account_uuid"], expected["organization_uuid"])
    local = None
    if not matches:
        raise Refused("identity_mismatch_before")
    if _exists(run.out, "bearer.result.json"):
        raise Refused("result_exists", EXIT_ATTEMPTED)
    run.create_attempt("bearer")
    document = {"attempt_consumed": True, "n1_classification": n1_classification, "identity_matches_local": True,
                "bounds": {"socket_timeout_s": wiring.socket_timeout, "total_deadline_s": wiring.total_deadline,
                           "body_limit_bytes": usage.BODY_LIMIT, "usage_requests_max": 1,
                           "profile_requests_max": 1, "retries": 0, "redirects": 0, "refresh": False},
                "credential": {"read": False, "reason": None}, "usage": None, "profile": None}
    held = observation = identity = None
    usage_endpoint = wiring.usage_endpoint or usage.PRODUCTION
    profile_endpoint = wiring.profile_endpoint or usage.PRODUCTION_PROFILE
    try:
        try:
            held = bearer_mod.read_linux_bearer(paths.config, read_env, uid=uid)
        except bearer_mod.BearerRefused as error:
            document["credential"]["reason"] = error.reason
            return EXIT_NOT_OK, "credential_refused", document, {}
        document["credential"]["read"] = True
        common = dict(context=wiring.tls_context, socket_timeout=wiring.socket_timeout,
                      total_deadline=wiring.total_deadline, resolver=wiring.resolver)
        observation = usage.fetch(held, usage_endpoint, **common)
        identity = usage.fetch_profile_identity(held, profile_endpoint, **common)
        held = None
        document["usage"] = _usage_summary(modules, observation)
        document["profile"] = {
            "available": identity is not None,
            "account_uuid_matches": None if identity is None else identity[0] == expected["account_uuid"],
            "organization_uuid_matches": None if identity is None else identity[1] == expected["organization_uuid"]}
    except _Interrupted:
        return EXIT_NOT_OK, "interrupted", document, {}
    finally:
        held = observation = identity = None
    profile = document["profile"]
    ok = (document["usage"]["valid_general"] and profile["account_uuid_matches"] is True
          and profile["organization_uuid_matches"] is True)
    return (EXIT_OK if ok else EXIT_NOT_OK), ("verified" if ok else "not_verified"), document, {}


STEPS = {"pending": step_pending, "ready": step_ready, "n1": step_n1, "bearer": step_bearer}
LIVE_STEPS = ("n1", "bearer")


def _parser():
    parser = argparse.ArgumentParser(prog="live_claude.py", description="LC live driver (coordinator only).")
    sub = parser.add_subparsers(dest="step", required=True)
    for name in STEPS:
        item = sub.add_parser(name)
        item.add_argument("--alias", required=True)
        item.add_argument("--out", required=True)
        if name == "ready":
            item.add_argument("--confirm", action="store_true")
        if name == "n1":
            item.add_argument("--binary", required=True)
            item.add_argument("--no-tunnel", action="store_true")
    return parser


def _on_signal(signum, frame):
    for name in SIGNALS:
        signal.signal(getattr(signal, name), signal.SIG_IGN)  # cleanup runs uninterrupted
    raise _Interrupted()


SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")


def main(argv=None, wiring=None):
    args = _parser().parse_args(argv)
    wiring = Wiring if wiring is None else wiring
    live = args.step in LIVE_STEPS
    document = {"schema": SCHEMA, "step": args.step, "status": "refused", "reason": None,
                "attempt_consumed": False}
    printed, run, modules, code = {}, None, None, EXIT_REFUSED
    saved = {name: signal.getsignal(getattr(signal, name)) for name in SIGNALS} if live else {}
    try:
        try:
            if sys.version_info < (3, 11):
                raise Refused("python_too_old")
            if os.getuid() == 0:
                raise Refused("root_refused")
            if sys.platform != "linux":
                raise Refused("platform_unsupported")
            modules = _Modules()
            config, paths = resolve_paths(modules, args.alias)
            document["config_source"] = config.source if config.source in (
                "found", "not found; defaults", "from CODING_AGENT_KIT_CONFIG") else "other"
            document["config_warnings"] = len(config.warnings)
            run = Run(open_out_dir(args.out, paths.secrets_dir, os.getuid()))
            for name in saved:
                signal.signal(getattr(signal, name), _on_signal)
            code, status, extra, printed = STEPS[args.step](modules, config, paths, run, args, wiring)
            document.update(extra, status=status)
        except Refused as refusal:
            code = refusal.code
            document.update(refusal.extra, reason=refusal.reason)
        except _Interrupted:
            code = EXIT_NOT_OK
            document.update(status="interrupted")
        except Exception as error:  # noqa: BLE001 - reason code or class name only, never a value
            harness = (modules.n1.HarnessRefused, modules.bearer.BearerRefused) if modules else ()
            if isinstance(error, harness):
                code = EXIT_REFUSED
                document.update(reason=error.reason)
            else:
                code = EXIT_INTERNAL
                document.update(status="internal_error", reason="internal:" + type(error).__name__)
        for name in saved:
            signal.signal(getattr(signal, name), signal.SIG_IGN)  # the result write is not interrupted
        consumed = run is not None and run.attempt_created
        if consumed and code == EXIT_REFUSED:
            code = EXIT_NOT_OK  # refusals after the attempt still consume it
        document["attempt_consumed"] = consumed
        document["exit_code"] = code
        if run is not None and (consumed or not live):
            name = args.step + ".result.json"
            try:
                if live or not _exists(run.out, name):
                    _write(run.out, name, document)
                else:  # a pending/ready rerun never replaces the first record
                    _write(run.out, args.step + ".rerun.json", document, exclusive=False)
            except (OSError, Refused):
                document["result_file_error"] = True
    finally:
        if run is not None:
            os.close(run.out)
        for name, handler in saved.items():
            signal.signal(getattr(signal, name), handler)
    document.update(printed)
    sys.stdout.write(_dump(document))
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
