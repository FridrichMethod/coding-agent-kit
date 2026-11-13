"""A hook command or stdio MCP server for the real-claude protected-mode tests (no network beyond 127.0.0.1).

    python3 -I inside.py KIND OUT PORT HOME

Records to OUT/inside-KIND.json what a process Claude Code starts can reach from inside the boundary:
a direct TCP connect to 127.0.0.1:PORT (the fixture upstream, outside the gate) and a file write in
HOME (outside the writable set), each "ok" or the errno, plus its TMPDIR. As KIND "mcp" it then
answers the MCP initialize and tools/list requests on stdin (one JSON-RPC message per line) until EOF.
"""

import json
import os
import socket
import sys


def main(kind, out, port, home):
    result = {"kind": kind, "tmpdir": os.environ.get("TMPDIR")}
    try:
        socket.create_connection(("127.0.0.1", int(port)), timeout=3).close()
        result["direct"] = "ok"
    except OSError as error:
        result["direct"] = error.errno
    try:
        with open(os.path.join(home, "inside-%s.txt" % kind), "w"):
            pass
        result["home_write"] = "ok"
    except OSError as error:
        result["home_write"] = error.errno
    with open(os.path.join(out, "inside-%s.json" % kind), "w") as handle:
        json.dump(result, handle)
    if kind != "mcp":
        return
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue
        if message.get("method") == "initialize":
            answer = {"protocolVersion": (message.get("params") or {}).get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif message.get("method") == "tools/list":
            answer = {"tools": [{"name": "fixture_echo", "description": "Echo nothing.",
                                 "inputSchema": {"type": "object", "properties": {}}}]}
        else:
            answer = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": answer}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main(*sys.argv[1:5])
