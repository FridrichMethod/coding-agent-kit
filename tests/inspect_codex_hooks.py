"""Explicit offline hooks/list probe with an isolated Codex home; no model call."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


def inspect(home, cwd, executable="codex"):
    home, cwd = Path(home).resolve(), Path(cwd).resolve()
    if home == Path.home() / ".codex" or not (home / "config.toml").is_file():
        raise ValueError("explicit disposable Codex home with config.toml required")
    process = subprocess.Popen([executable, "app-server", "--strict-config"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               env=dict(os.environ, CODEX_HOME=str(home)))
    messages = queue.Queue()

    def read():
        for line in process.stdout:
            messages.put(json.loads(line))

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    requests = [{"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "sherlock-hook-inspection", "version": "1"},
        "capabilities": {"experimentalApi": True}}}, {"method": "initialized"},
        {"id": 2, "method": "hooks/list", "params": {"cwds": [str(cwd)]}}]
    try:
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                response = messages.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if response.get("id") == 2:
                if "error" in response:
                    raise ValueError(f"hooks/list rejected the configuration: {response['error']}")
                return response["result"]
        raise ValueError("hooks/list response was unavailable before the deadline")
    finally:
        process.terminate()
        process.wait(timeout=5)
        reader.join(timeout=1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--codex", default="codex")
    args = parser.parse_args()
    result = inspect(args.home, args.cwd, args.codex)
    summary = []
    for entry in result["data"]:
        summary.append({"errors": entry["errors"], "warnings": entry["warnings"], "hooks": [
            {key: hook.get(key) for key in ("command", "commandWindows", "matcher", "timeoutSec", "enabled", "trustStatus", "currentHash")}
            for hook in entry["hooks"] if hook.get("command") == "shk guard --client codex"]})
    print(json.dumps(summary, indent=2))
