"""A fake claude for the claude-as reserve tests; fabricated data only, no network.

--version prints CLAUDE_FAKE_VERSION (default the reviewed 2.1.322). `auth status` prints a
fixed line and exits CLAUDE_FAKE_AUTH_STATUS (default 0); `auth login` records the identity
CLAUDE_FAKE_LOGIN (account,organization) in CLAUDE_CONFIG_DIR/.claude.json as native does.
`auth status` records to FAKE_RECORD + ".auth". FAKE_DIRECT=PORT first tries a direct TCP connect.
Anything else records its arguments, environment and pid in FAKE_RECORD (when set), prints a
sentinel and exits FAKE_EXIT (default 0), or kills itself with signal FAKE_SIGNAL when that is set.
With FAKE_MESSAGES=N it first sends N Messages requests to ANTHROPIC_BASE_URL with its own bearer
and records each status in FAKE_RECORD.
"""

import json
import os
from pathlib import Path
import sys


def record(extra=None, suffix=""):
    target = os.environ.get("FAKE_RECORD")
    if not target:
        return
    target += suffix
    data = {"args": sys.argv[1:], "env": dict(os.environ), "pid": os.getpid(), "cwd": os.getcwd()}
    data.update(extra or {})
    Path(target).write_text(json.dumps(data))


def messages(count):
    import http.client
    from urllib.parse import urlsplit
    base = urlsplit(os.environ["ANTHROPIC_BASE_URL"])
    bearer = os.environ.get("FAKE_BEARER", "")
    statuses = []
    for _ in range(count):
        connection = http.client.HTTPConnection(base.hostname, base.port, timeout=20)
        body = json.dumps({"model": os.environ.get("FAKE_MODEL", "claude-sonnet-4-5-20250929"), "max_tokens": 1,
                           "messages": [{"role": "user", "content": "fixture"}]})
        try:
            connection.request("POST", "/v1/messages?beta=true", body,
                               {"Authorization": "Bearer " + bearer, "Content-Type": "application/json"})
            response = connection.getresponse()
            statuses.append([response.status, response.read().decode("utf-8", "replace")[:300]])
        except OSError as error:
            statuses.append([0, str(error)])
        finally:
            connection.close()
    return statuses


def main():
    args = sys.argv[1:]
    if args == ["--version"]:
        print(os.environ.get("CLAUDE_FAKE_VERSION", "2.1.322 (Claude Code)"))
        return 0
    if args == ["auth", "status"]:
        print("Logged in to the fixture account")
        record({"auth_status": True}, ".auth")
        return int(os.environ.get("CLAUDE_FAKE_AUTH_STATUS", "0"))
    if args == ["auth", "login"]:
        account, organization = os.environ["CLAUDE_FAKE_LOGIN"].split(",")
        target = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
        target.write_text(json.dumps({"oauthAccount": {"accountUuid": account, "organizationUuid": organization,
                                                       "emailAddress": "fixture@example.invalid"}}))
        target.chmod(0o600)
        return 0
    extra = {}
    if os.environ.get("FAKE_DIRECT"):
        import socket
        try:
            socket.create_connection(("127.0.0.1", int(os.environ["FAKE_DIRECT"])), timeout=5).close()
            extra["direct"] = "connected"
        except OSError as error:
            extra["direct"] = "errno:%s" % error.errno
    if os.environ.get("FAKE_MESSAGES"):
        extra["statuses"] = messages(int(os.environ["FAKE_MESSAGES"]))
    record(extra)
    print("fake claude sentinel", flush=True)
    if os.environ.get("FAKE_SIGNAL"):
        import signal
        signal.signal(int(os.environ["FAKE_SIGNAL"]), signal.SIG_DFL)
        os.kill(os.getpid(), int(os.environ["FAKE_SIGNAL"]))
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
