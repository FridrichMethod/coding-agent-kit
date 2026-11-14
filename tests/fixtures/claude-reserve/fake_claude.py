"""A fake claude for the claude-as reserve tests; fabricated data only, no network.

--version prints CLAUDE_FAKE_VERSION (default the reviewed 2.1.322). `auth status` prints a
fixed line and exits CLAUDE_FAKE_AUTH_STATUS (default 0); with CLAUDE_FAKE_RENEW=SECONDS it first
renews the token as native's refresh does: CLAUDE_CONFIG_DIR/.credentials.json is replaced (0600,
by rename) with CLAUDE_FAKE_RENEW_TOKEN as the access token, expiring SECONDS from now; `auth login` records the identity
CLAUDE_FAKE_LOGIN (account,organization) in CLAUDE_CONFIG_DIR/.claude.json as native does.
`auth status` records to FAKE_RECORD + ".auth". FAKE_DIRECT=PORT first tries a direct TCP connect.
Anything else records its arguments, environment and pid in FAKE_RECORD (when set), prints a
sentinel and exits FAKE_EXIT (default 0), or kills itself with signal FAKE_SIGNAL when that is set.
With FAKE_MESSAGES=N it first sends N Messages requests to ANTHROPIC_BASE_URL with its own bearer
and records each status in FAKE_RECORD. With FAKE_PAUSE_CYCLE=PATH it sends one request (printing
"fake claude admitted" after a 200), waits until PATH exists, then sends a request every 0.2 s,
printing "fake claude refused" after the first refusal, until one passes again; every status is
recorded. With FAKE_FAMILY_CYCLE=1 it holds an endless haiku and an endless sonnet stream open, sends
an opus request (printing "fake claude opus refused" after a refusal), then one every 0.2 s until one
passes ("fake claude opus admitted"), waits for the sonnet stream to end and sends one sonnet
request; the statuses and how each stream ended are recorded. FAKE_OUTSIDE_MARK=PATH, a path outside
everything a protected boundary can write, gets one line per invocation with its arguments (written
only when it runs unconfined: inside the boundary the write fails, silently).
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


def messages(count, model=None):
    import http.client
    from urllib.parse import urlsplit
    base = urlsplit(os.environ["ANTHROPIC_BASE_URL"])
    bearer = os.environ.get("FAKE_BEARER", "")
    statuses = []
    for _ in range(count):
        connection = http.client.HTTPConnection(base.hostname, base.port, timeout=20)
        body = json.dumps({"model": model or os.environ.get("FAKE_MODEL", "claude-sonnet-4-5-20250929"),
                           "max_tokens": 1, "messages": [{"role": "user", "content": "fixture"}]})
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


def pause_cycle(go):
    import time
    statuses = messages(1)
    if statuses[0][0] == 200:
        print("fake claude admitted", flush=True)
    deadline = time.monotonic() + 100
    while not os.path.exists(go) and time.monotonic() < deadline:
        time.sleep(0.05)
    refused = False
    while time.monotonic() < deadline:
        statuses += messages(1)
        if statuses[-1][0] == 200 and refused:
            break
        if statuses[-1][0] != 200 and not refused:
            refused = True
            print("fake claude refused", flush=True)
        time.sleep(0.2)
    return statuses


def hold(model, ended, sockets):
    """One endless Messages stream of MODEL ("forever" in the body): ended[model] is "open" once its
    first event arrived, then "closed" when the gate or the upstream ends it."""
    import socket
    from urllib.parse import urlsplit
    base = urlsplit(os.environ["ANTHROPIC_BASE_URL"])
    body = json.dumps({"model": model, "max_tokens": 1, "forever": True,
                       "messages": [{"role": "user", "content": "fixture"}]}).encode()
    sock = socket.create_connection((base.hostname, base.port), timeout=60)
    sockets[model] = sock
    sock.sendall(("POST /v1/messages?beta=true HTTP/1.1\r\nHost: gate\r\nAuthorization: Bearer %s\r\n"
                  "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                  % (os.environ.get("FAKE_BEARER", ""), len(body))).encode() + body)
    seen = b""
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            seen += data
            if b"event: ping" in seen:
                ended[model] = "open"
                seen = b""
    except OSError:
        pass
    ended[model] = "closed"


def family_cycle():
    import threading
    import time
    opus, sonnet, haiku = "claude-opus-4-1", "claude-sonnet-4-5", "claude-haiku-4-5"
    ended, sockets = {}, {}
    for model in (haiku, sonnet):
        threading.Thread(target=hold, args=(model, ended, sockets), daemon=True).start()
    deadline = time.monotonic() + 100
    while len(ended) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    statuses = messages(1, opus)
    if statuses[0][0] != 200:
        print("fake claude opus refused", flush=True)
    while statuses[-1][0] != 200 and time.monotonic() < deadline:
        time.sleep(0.2)
        statuses += messages(1, opus)
    if statuses[-1][0] == 200:
        print("fake claude opus admitted", flush=True)
    while ended.get(sonnet) != "closed" and time.monotonic() < deadline:
        time.sleep(0.02)
    statuses += messages(1, sonnet)
    streams = dict(ended)
    if haiku in sockets:
        sockets[haiku].close()
    return {"statuses": statuses, "streams": streams}


def renew(seconds, token):
    import time
    target = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
    document = json.loads(target.read_text())
    document["claudeAiOauth"].update(accessToken=token, expiresAt=int(time.time() * 1000) + seconds * 1000)
    fresh = target.with_name(".credentials.json.new")
    fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(document))
    os.replace(fresh, target)


def mark_outside():
    target = os.environ.get("FAKE_OUTSIDE_MARK")
    if not target:
        return
    try:
        with open(target, "a") as stream:
            stream.write(json.dumps(sys.argv[1:]) + "\n")
    except OSError:
        pass  # inside the boundary


def main():
    args = sys.argv[1:]
    mark_outside()
    if args == ["--version"]:
        print(os.environ.get("CLAUDE_FAKE_VERSION", "2.1.322 (Claude Code)"))
        return 0
    if args == ["auth", "status"]:
        if os.environ.get("CLAUDE_FAKE_RENEW"):
            renew(int(os.environ["CLAUDE_FAKE_RENEW"]), os.environ["CLAUDE_FAKE_RENEW_TOKEN"])
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
    if os.environ.get("FAKE_PAUSE_CYCLE"):
        extra["statuses"] = pause_cycle(os.environ["FAKE_PAUSE_CYCLE"])
    if os.environ.get("FAKE_FAMILY_CYCLE"):
        extra.update(family_cycle())
    record(extra)
    print("fake claude sentinel", flush=True)
    if os.environ.get("FAKE_SIGNAL"):
        import signal
        signal.signal(int(os.environ["FAKE_SIGNAL"]), signal.SIG_DFL)
        os.kill(os.getpid(), int(os.environ["FAKE_SIGNAL"]))
    return int(os.environ.get("FAKE_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
