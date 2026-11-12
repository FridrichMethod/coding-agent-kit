"""One-shot usage permission research. Real execution needs separate approval."""
from datetime import datetime
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

_spec = importlib.util.spec_from_file_location("ptp", Path(__file__).resolve().parent.parent / "claude_token" / "permission_probe.py")
ptp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ptp)
ProbeError = ptp.ProbeError
_read_token = ptp._read_token
_BoundedResponse = ptp._BoundedResponse
http, ssl, subprocess = ptp.http, ptp.ssl, ptp.subprocess
TOTAL_SECONDS = 10
DESCRIPTION = (
    "Offline by default. No account is authorized. After independent review and "
    "specific user approval only: --usage-once --account NAME --token-file EXACT_PATH. "
    "One GET to https://api.anthropic.com/api/oauth/usage, no body, retries, "
    "redirects, refresh or generation. Coordinator must verify the configured "
    "directory and direct-network policy before approval."
)
HEADER_NAMES = ("date", "age", "cache-control", "request-id", "retry-after")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProbeError()
        result[key] = value
    return result


def _window(value):
    if not isinstance(value, dict):
        return None
    utilization, reset = value.get("utilization"), value.get("resets_at")
    if type(utilization) not in (int, Decimal) or not 0 <= utilization <= 100:
        return None
    if isinstance(utilization, Decimal):
        number = float(utilization)
        if Decimal(str(number)) != utilization:
            return None
        utilization = number
    if not isinstance(reset, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", reset):
        return None
    if reset[-1] != "Z" and (int(reset[-5:-3]) > 23 or int(reset[-2:]) > 59):
        return None
    try:
        datetime.fromisoformat(reset)
    except ValueError:
        return None
    return {"utilization": utilization, "resets_at": reset}


def _summarize(status, headers, body, token):
    data = json.loads(body, object_pairs_hook=_unique, parse_float=Decimal)
    if not isinstance(data, dict):
        raise ProbeError()
    grouped = {name: [] for name in HEADER_NAMES}
    for name, value in headers:
        if name.lower() in grouped:
            grouped[name.lower()].append(value)
    retained, header_states = {}, {}
    for name, values in grouped.items():
        value = ptp._value(name, values[0], token) if len(values) == 1 else None
        header_states[name] = "missing" if not values else ("valid" if value is not None else "malformed")
        if value is not None:
            retained[name] = value
    windows, window_states = {}, {}
    for name in ("five_hour", "seven_day"):
        value = _window(data.get(name))
        window_states[name] = "missing" if name not in data else ("valid" if value is not None else "malformed")
        if value is not None:
            windows[name] = value
    error = data.get("error")
    error_type = error.get("type") if isinstance(error, dict) else None
    if not isinstance(error_type, str) or error_type not in ptp.ERROR_TYPES:
        error_type = None
    return {"outcome": "response", "http_status": status, "headers": retained,
            "header_states": header_states, "windows": windows,
            "window_states": window_states,
            "windows_complete": status == 200 and error is None and len(windows) == 2,
            "error_type": error_type,
            "other_model_windows_present": any(name.startswith(("five_hour_", "seven_day_")) for name in data)}


def _request_once(token):
    connection, status = None, None
    started, start = ptp._utc(), time.monotonic()
    try:
        connection = http.client.HTTPSConnection("api.anthropic.com", timeout=5, context=ssl.create_default_context())
        connection.response_class = _BoundedResponse
        connection.request("GET", "/api/oauth/usage", None, {
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "Content-Type": "application/json", "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-cli/2.1.322 (external, cli)",
            "Cache-Control": "no-cache", "Connection": "close",
        })
        response = connection.getresponse()
        status = response.status
        body = response.read(ptp.LIMIT + 1)
        if len(body) > ptp.LIMIT:
            raise ProbeError()
        result = _summarize(status, response.getheaders(), body, token)
    except (ValueError, UnicodeError, ProbeError):
        result = {"outcome": "response_invalid", "http_status": status, "windows_complete": False}
    except Exception:
        result = {"outcome": "transport_error", "http_status": status, "windows_complete": False}
    finally:
        if connection is not None:
            connection.close()
    result.update(request_start_utc=started, receive_utc=ptp._utc() if status is not None else None, duration_seconds=round(time.monotonic() - start, 6))
    return result


def _worker(account, path):
    try:
        ptp.resource.setrlimit(ptp.resource.RLIMIT_CORE, (0, 0))
        return _request_once(_read_token(account, path))
    except BaseException:
        return {"outcome": "probe_failed", "windows_complete": False}


def _run_owned(account, path):
    # Keep this entry local: PTP's worker would reenter the POST count probe.
    entry = 'import runpy,sys; m=runpy.run_path(sys.argv[1]); sys.exit(m["main"](sys.argv[2:], _child=True))'
    command = [sys.executable, "-I", "-B", "-c", entry, str(Path(__file__).resolve()), "--usage-once", "--account", account, "--token-file", path]
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
    parser = ptp._Parser(description=DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--usage-once", action="store_true")
    parser.add_argument("--account")
    parser.add_argument("--token-file")
    try:
        args = parser.parse_args(argv)
        if not args.usage_once:
            if args.account or args.token_file:
                raise ProbeError()
            print(DESCRIPTION)
            return 0
        if args.describe or not args.account or not args.token_file:
            raise ProbeError()
        if any(value and (key.lower() in ptp.BLOCKED_ENV or key.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_", "AWS_", "GOOGLE_", "AZURE_"))) for key, value in os.environ.items()):
            raise ProbeError()
        result = _worker(args.account, args.token_file) if _child else _run_owned(args.account, args.token_file)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except Exception:
        print('{"outcome":"preflight_refused","windows_complete":false}')
        return 2


if __name__ == "__main__":
    sys.exit(main())
