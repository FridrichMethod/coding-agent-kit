"""A fake codex for protected-mode tests: version, App Server reads and refresh, and a tiny TUI.

Installed by the tests as an executable named codex (with a #! line for this interpreter). It
reads the -c overrides codex-as and protected mode pass, the account credential through CODEX_HOME,
and talks to the gate the way native 0.167.2 does: plain HTTP to chatgpt_base_url and the provider
base_url, CONNECT through HTTPS_PROXY for auth.openai.com:443 (verified with SSL_CERT_FILE).

    codex --version                      FAKE_VERSION, default codex-cli 0.167.2 (with FAKE_VERSION_MARK
                                         set, it first tries to append to that file and prints
                                         VERSION-MARK and the errno on stderr; with FAKE_VERSION_LINGER
                                         set, it leaves a sleeping child, whose argv carries that value,
                                         holding its output open)
    codex [-c K=V]... app-server ...     JSON-RPC on stdio: initialize, account/rateLimits/read
                                         (one usage GET), account/read with refreshToken (one
                                         refresh through the tunnel, written like native: a
                                         temporary file beside the resolved auth.json, renamed)
    codex [-c K=V]... --no-daemon ...    TUI: prints READY, then per line on its terminal:
                                         usage | bundle | turn [MODEL] | net PORT |
                                         write PATH [BASE64] | spawn |
                                         replace (auth.json becomes a regular file) | quit
Protected mode gives App Server readers an allowlisted environment, so the knobs come from the
environment or from <codex_home>/fake-codex.json (codex_home is the parent of accounts/NAME), and
every action is appended to <codex_home>/sessions/fake-codex.log (JSON lines; protected mode leaves
codex_home itself read-only, its sessions directory writable). FAKE_BUNDLE=1 fetches the
config bundle at App Server and TUI start, as a workspace plan does; FAKE_REPLACE_LINK=1 makes the
refresh replace the auth.json link with a regular file instead of writing through it.
"""

import base64
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
from datetime import datetime, timezone


def root():
    home = Path(os.environ.get("CODEX_HOME", "."))
    return home.parent.parent if home.parent.name == "accounts" else home


def knob(name, default=None):
    if name in os.environ:
        return os.environ[name]
    try:
        return json.loads((root() / "fake-codex.json").read_text()).get(name, default)
    except (OSError, ValueError):
        return default


def log(event, **fields):
    (root() / "sessions").mkdir(exist_ok=True)
    with open(root() / "sessions/fake-codex.log", "a") as stream:
        stream.write(json.dumps(dict(fields, event=event, pid=os.getpid())) + "\n")


def parse(argv):
    config, rest, index = {}, [], 0
    while index < len(argv):
        word = argv[index]
        if word == "-c" and index + 1 < len(argv):
            key, _, value = argv[index + 1].partition("=")
            try:
                config[key] = json.loads(value)
            except ValueError:
                config[key] = value
            index += 2
            continue
        rest.append(word)
        index += 1
    return config, rest


def credential():
    path = Path(os.environ["CODEX_HOME"]) / "auth.json"
    return path, json.loads(path.read_text())


def http(base, method, path, body=None, headers=()):
    """One plain HTTP/1.1 request to the gate: (status, body)."""
    host, _, port = base.split("//", 1)[1].split("/", 1)[0].partition(":")
    prefix = "/" + base.split("//", 1)[1].split("/", 1)[1] if "/" in base.split("//", 1)[1] else "/"
    target = prefix.rstrip("/") + "/" + path.lstrip("/")
    data = b"" if body is None else json.dumps(body).encode()
    lines = ["%s %s HTTP/1.1" % (method, target), "Host: %s:%s" % (host, port), "User-Agent: fake-codex/0.167.2",
             "Accept: application/json", "Connection: close"]
    lines += ["%s: %s" % pair for pair in headers]
    if body is not None:
        lines += ["Content-Type: application/json", "Content-Length: %d" % len(data)]
    try:
        sock = socket.create_connection((host, int(port)), timeout=15)
    except OSError as error:
        return -error.errno if error.errno else -1, b""
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
        raw = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
    except OSError:
        return -2, b""
    finally:
        sock.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    try:
        status = int(head.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return 0, b""
    return status, payload


def auth_headers(data):
    tokens = data["tokens"]
    return (("Authorization", "Bearer " + tokens["access_token"]), ("ChatGPT-Account-ID", tokens["account_id"]))


def bundle(config):
    if knob("FAKE_BUNDLE"):
        _, data = credential()
        status, _ = http(config["chatgpt_base_url"], "GET", "wham/config/bundle", headers=auth_headers(data))
        log("bundle", status=status)


def usage(config):
    _, data = credential()
    status, body = http(config["chatgpt_base_url"], "GET", "wham/usage", headers=auth_headers(data))
    log("usage", status=status)
    return status, body


def length(raw):
    for line in raw.partition(b"\r\n\r\n")[0].split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            return int(value)
    return 0


def refresh():
    """POST /oauth/token to auth.openai.com through HTTPS_PROXY; native writes the new tokens."""
    proxy = os.environ.get("HTTPS_PROXY", "")
    if not proxy.startswith("http://"):
        log("refresh", ok=False, why="no proxy")
        return False
    host, _, port = proxy[7:].rstrip("/").partition(":")
    path, data = credential()
    try:
        sock = socket.create_connection((host, int(port)), timeout=10)
        sock.sendall(b"CONNECT auth.openai.com:443 HTTP/1.1\r\nHost: auth.openai.com:443\r\n\r\n")
        head = b""
        while not head.endswith(b"\r\n\r\n"):
            byte = sock.recv(1)
            if not byte:
                raise OSError("tunnel closed")
            head += byte
        if head.split(b" ")[1] != b"200":
            raise OSError("tunnel refused")
        context = ssl.create_default_context()
        tls = context.wrap_socket(sock, server_hostname="auth.openai.com")
        body = json.dumps({"grant_type": "refresh_token", "refresh_token": data["tokens"]["refresh_token"]}).encode()
        tls.sendall(b"POST /oauth/token HTTP/1.1\r\nHost: auth.openai.com\r\nContent-Type: application/json\r\n"
                    b"Connection: close\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
        raw = b""  # like native's client: the reply ends at its Content-Length, not at EOF
        while b"\r\n\r\n" not in raw or len(raw.partition(b"\r\n\r\n")[2]) < length(raw):
            chunk = tls.recv(65536)
            if not chunk:
                break
            raw += chunk
        tls.close()
    except (OSError, ssl.SSLError, IndexError) as error:
        log("refresh", ok=False, why=type(error).__name__)
        return False
    head, _, payload = raw.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n", 1)[0] + b" ":
        log("refresh", ok=False, why="status")
        return False
    tokens = json.loads(payload)
    data["tokens"].update(tokens)
    data["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    save(path if knob("FAKE_REPLACE_LINK") else path.resolve(), data)
    log("refresh", ok=True)
    return True


def save(target, data):
    """Write like native: a temporary file beside TARGET, renamed over it (a link at TARGET is replaced)."""
    temporary = target.with_name(".auth.json.tmp-%d" % os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(data))
    os.replace(temporary, target)


def app_server(config):
    bundle(config)
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method, ident = message.get("method"), message.get("id")
        if ident is None:
            continue
        if method == "initialize":
            log("initialize", client=message.get("params", {}).get("clientInfo"))
            reply = {"result": {"userAgent": "fake"}}
        elif method == "account/rateLimits/read" and knob("FAKE_POLICY_ERROR"):
            reply = {"error": {"code": -32603, "message": "failed to fetch codex rate limits: "
                                                           "application network policy is unavailable"}}
        elif method == "account/rateLimits/read":
            status, body = usage(config)
            reply = {"result": {"rateLimits": json.loads(body)}} if status == 200 else \
                {"error": {"code": -32603, "message": "failed to fetch codex rate limits"}}
        elif method == "account/read":
            ok = message.get("params", {}).get("refreshToken") and refresh()
            reply = {"result": {"account": {"type": "chatgpt"}}} if ok else {"result": {"account": None}}
        else:
            reply = {"error": {"code": -32601, "message": "unknown"}}
        sys.stdout.write(json.dumps(dict(reply, id=ident)) + "\n")
        sys.stdout.flush()


def tui(config, rest):
    log("tui", argv=rest, raw=sys.argv[1:], env=dict(os.environ), cwd=os.getcwd(), env_proxy=sorted(name for name in os.environ if name.lower().endswith("_proxy")),
        tmpdir=os.environ.get("TMPDIR"), pid_env=os.environ.get("CODING_AGENT_KIT_CLAUDE_PID"))
    bundle(config)
    base = config.get("model_providers.quota_gate.base_url", "https://chatgpt.com/backend-api/codex")
    print("READY", flush=True)
    for line in sys.stdin:
        words = line.split()
        if not words:
            continue
        if words[0] == "usage":
            status, _ = usage(config)
            print("USAGE %d" % status, flush=True)
        elif words[0] == "bundle":  # a config bundle fetch mid-run, as native makes once its cache is stale
            _, data = credential()
            status, _ = http(config["chatgpt_base_url"], "GET", "wham/config/bundle", headers=auth_headers(data))
            log("bundle", status=status)
            print("BUNDLE %d" % status, flush=True)
        elif words[0] == "turn":
            _, data = credential()
            model = words[1] if len(words) > 1 else "gpt-5.5"
            status, body = http(base, "POST", "responses", {"model": model, "input": "fixture"},
                                headers=auth_headers(data))
            log("turn", status=status, model=model)
            print("TURN %d" % status, flush=True)
        elif words[0] == "net":
            try:
                socket.create_connection(("127.0.0.1", int(words[1])), timeout=3).close()
                print("NET open", flush=True)
            except OSError as error:
                print("NET %s" % error.errno, flush=True)
        elif words[0] == "write":
            try:
                Path(words[1]).write_bytes(base64.b64decode(words[2]) if len(words) > 2 else b"x")
                print("WRITE ok", flush=True)
            except OSError as error:
                print("WRITE %s" % error.errno, flush=True)
        elif words[0] == "spawn":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            print("SPAWN %d" % child.pid, flush=True)
        elif words[0] == "replace":
            path, data = credential()
            data["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            data["tokens"]["refresh_token"] = "fixture-refresh-replaced"
            save(path, data)
            print("REPLACED", flush=True)
        elif words[0] == "quit":
            break
    log("exit")
    sys.exit(int(knob("FAKE_EXIT", "0")))


def main(argv):
    if argv == ["--version"]:
        if knob("FAKE_VERSION_LINGER"):
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", knob("FAKE_VERSION_LINGER")])
        if knob("FAKE_VERSION_MARK"):
            try:
                with open(knob("FAKE_VERSION_MARK"), "a") as stream:
                    stream.write("version ran unconfined\n")
                print("VERSION-MARK ok", file=sys.stderr)
            except OSError as error:
                print("VERSION-MARK %s" % error.errno, file=sys.stderr)
        print(knob("FAKE_VERSION", "codex-cli 0.167.2"))
        return 0
    config, rest = parse(argv)
    if rest[:1] == ["app-server"]:
        app_server(config)
        return 0
    if "--no-daemon" in rest:
        tui(config, rest)
        return 0
    log("other", argv=rest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
