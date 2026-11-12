"""One-shot count-header research probe. Real execution requires separate approval."""
import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime, format_datetime
import http.client
import json
import math
import os
from pathlib import Path
import re
import resource
import ssl
import stat
import subprocess
import sys
import time

TOTAL_SECONDS = 10
LIMIT = 65536
TOKEN_LIMIT = 8204
BODY = b'{"model":"claude-sonnet-5-5","messages":[{"role":"user","content":"quota metadata probe"}]}'
DESCRIPTION = (
    "Offline by default. No account is authorized. After independent review and "
    "specific user approval only: --count-once --account NAME --token-file EXACT_PATH. "
    "One POST to https://api.anthropic.com/v1/messages/count_tokens; no retries, "
    "redirects or generation. Exact configured directory and direct-network policy "
    "must be verified by the coordinator before approval."
)
ERROR_TYPES = frozenset(("invalid_request_error", "authentication_error", "permission_error", "not_found_error", "request_too_large", "rate_limit_error", "api_error", "overloaded_error"))
BLOCKED_ENV = frozenset((
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "ssl_cert_file",
    "ssl_cert_dir", "sslkeylogfile", "requests_ca_bundle", "curl_ca_bundle",
    "node_extra_ca_certs", "node_tls_reject_unauthorized",
))


class ProbeError(Exception):
    """Never carry untrusted or credential-bearing error details."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProbeError()


def _read_token(account, path):
    """Open each path component without following symlinks; read once by fd."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", account):
        raise ProbeError()
    parts = path.split("/")
    if not path.startswith("/") or any(part in ("", ".", "..") for part in parts[1:]) or parts[-1] != "claude-" + account:
        raise ProbeError()
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    fd = None
    try:
        for part in parts[1:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o600 or before.st_uid != os.getuid() or before.st_nlink != 1 or not 0 < before.st_size <= TOKEN_LIMIT:
            raise ProbeError()
        raw = os.read(fd, TOKEN_LIMIT + 1)
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_mode, before.st_nlink, before.st_uid) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_mode, after.st_nlink, after.st_uid) or len(raw) != before.st_size:
            raise ProbeError()
        token = raw.removesuffix(b"\n").decode("ascii")
        if not re.fullmatch(r"sk-ant-oat[A-Za-z0-9_.-]{1,8192}", token) or token.count("sk-ant-oat") != 1:
            raise ProbeError()
        return token
    except (OSError, UnicodeError):
        raise ProbeError() from None
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


class _HeaderBudget:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = LIMIT

    def readline(self, limit=-1):
        line = self.stream.readline(min(self.remaining + 1, limit) if limit >= 0 else self.remaining + 1)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise ProbeError()
        return line

    def __getattr__(self, name):
        return getattr(self.stream, name)


class _BoundedResponse(http.client.HTTPResponse):
    def begin(self):
        # Keep the readline budget through chunk framing/trailers as well.
        # Body reads delegate to the stream and have their separate LIMIT.
        if not isinstance(self.fp, _HeaderBudget):
            self.fp = _HeaderBudget(self.fp)
        super().begin()


def _date(value):
    if not re.fullmatch(r"[A-Za-z]{3}, [0-9]{2} [A-Za-z]{3} [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2} GMT", value):
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return format_datetime(parsed, usegmt=True)
    except (TypeError, ValueError, OverflowError):
        return None


def _value(name, value, token):
    if len(value) > 256 or token in value or "sk-ant-" in value.lower():
        return None
    if name == "date":
        return _date(value)
    if name in ("age", "retry-after"):
        return int(value) if re.fullmatch(r"[0-9]{1,10}", value) else (_date(value) if name == "retry-after" else None)
    if name == "request-id":
        return value if re.fullmatch(r"req_[A-Za-z0-9]{1,100}", value) else None
    if name == "cache-control":
        return value if re.fullmatch(r"(?:no-cache|no-store|private|public|must-revalidate|max-age=[0-9]{1,10}|s-maxage=[0-9]{1,10})(?:, *(?:no-cache|no-store|private|public|must-revalidate|max-age=[0-9]{1,10}|s-maxage=[0-9]{1,10}))*", value) else None
    if name.endswith("-utilization"):
        if not re.fullmatch(r"[01](?:\.[0-9]{1,12})?", value):
            return None
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 1 else None
    if name.endswith("-reset"):
        return int(value) if re.fullmatch(r"[0-9]{1,10}", value) and 0 < int(value) <= 4102444800 else None
    if name.endswith("-status"):
        return value if value in ("allowed", "allowed_warning", "rejected") else None
    return None


def _summarize(status, headers, body, token):
    names = {"date", "age", "cache-control", "request-id", "retry-after"}
    names.update("anthropic-ratelimit-unified-" + window + "-" + field for window in ("5h", "7d") for field in ("utilization", "reset", "status"))
    grouped = {name: [] for name in names}
    other_scope = other_family = False
    for name, value in headers:
        name = name.lower()
        if name in grouped:
            grouped[name].append(value)
        else:
            other_scope |= "scope" in name
            other_family |= name.startswith("anthropic-ratelimit-unified-")
    retained = {}
    states = {}
    for name, values in grouped.items():
        value = _value(name, values[0], token) if len(values) == 1 else None
        states[name] = "missing" if not values else ("valid" if value is not None else "malformed")
        if value is not None:
            retained[name] = value
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ProbeError()
    error = data.get("error")
    error_type = error.get("type") if isinstance(error, dict) else None
    if not isinstance(error_type, str) or error_type not in ERROR_TYPES:
        error_type = None
    windows = {}
    for window in ("5h", "7d"):
        fields = [states["anthropic-ratelimit-unified-" + window + "-" + field] for field in ("utilization", "reset")]
        status_state = states["anthropic-ratelimit-unified-" + window + "-status"]
        windows[window] = "complete" if fields == ["valid", "valid"] and status_state != "malformed" else "unusable"
    return {"outcome": "response", "http_status": status, "headers": retained,
            "header_states": states, "windows": windows,
            "windows_complete": status == 200 and error is None and all(value == "complete" for value in windows.values()),
            "input_tokens_present": "input_tokens" in data, "error_type": error_type,
            "other_scope_headers_present": other_scope,
            "other_model_family_headers_present": other_family}


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _request_once(token):
    connection = None
    started, start = _utc(), time.monotonic()
    status = None
    try:
        context = ssl.create_default_context()
        connection = http.client.HTTPSConnection("api.anthropic.com", timeout=5, context=context)
        connection.response_class = _BoundedResponse
        connection.request("POST", "/v1/messages/count_tokens", BODY, {
            "Authorization": "Bearer " + token, "Content-Type": "application/json",
            "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20",
            "Cache-Control": "no-cache", "Connection": "close",
        })
        response = connection.getresponse()
        status = response.status
        body = response.read(LIMIT + 1)
        if len(body) > LIMIT:
            raise ProbeError()
        result = _summarize(status, response.getheaders(), body, token)
    except (ValueError, UnicodeError, ProbeError):
        result = {"outcome": "response_invalid", "http_status": status, "windows_complete": False}
    except Exception:
        result = {"outcome": "transport_error", "http_status": status, "windows_complete": False}
    finally:
        if connection is not None:
            connection.close()
    result.update(request_start_utc=started, receive_utc=_utc() if status is not None else None, duration_seconds=round(time.monotonic() - start, 6))
    return result


def _worker(account, path):
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return _request_once(_read_token(account, path))
    except BaseException:
        return {"outcome": "probe_failed", "windows_complete": False}


def _run_owned(account, path):
    # A fresh interpreter closes inherited descriptors and avoids threaded fork
    # callbacks/preexec functions. No credential ever crosses this pipe or argv.
    entry = 'import runpy,sys; m=runpy.run_path(sys.argv[1]); sys.exit(m["main"](sys.argv[2:], _child=True))'
    command = [sys.executable, "-I", "-B", "-c", entry, str(Path(__file__).resolve()), "--count-once", "--account", account, "--token-file", path]
    start = time.monotonic()
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True, env={"LANG": "C", "LC_ALL": "C"})
    try:
        output, _ = child.communicate(timeout=max(0.001, TOTAL_SECONDS - (time.monotonic() - start)))
        if child.returncode or len(output) > 16384:
            raise ProbeError()
        result = json.loads(output)
        if not isinstance(result, dict):
            raise ProbeError()
        return result
    except subprocess.TimeoutExpired:
        return {"outcome": "deadline_exceeded", "windows_complete": False}
    except (ValueError, ProbeError):
        return {"outcome": "probe_failed", "windows_complete": False}
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()


def main(argv=None, *, _child=False):
    parser = _Parser(description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--count-once", action="store_true")
    parser.add_argument("--account")
    parser.add_argument("--token-file")
    try:
        args = parser.parse_args(argv)
        if not args.count_once:
            if args.account or args.token_file:
                raise ProbeError()
            print(DESCRIPTION)
            return 0
        if args.describe or not args.account or not args.token_file:
            raise ProbeError()
        if any(value and (key.lower() in BLOCKED_ENV or key.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_", "AWS_", "GOOGLE_", "AZURE_"))) for key, value in os.environ.items()):
            raise ProbeError()
        result = _worker(args.account, args.token_file) if _child else _run_owned(args.account, args.token_file)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except Exception:
        print('{"outcome":"preflight_refused","windows_complete":false}')
        return 2


if __name__ == "__main__":
    sys.exit(main())
