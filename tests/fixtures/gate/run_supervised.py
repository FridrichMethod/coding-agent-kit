"""Run one supervised invocation on the caller's terminal: python3 -I -B run_supervised.py SPEC.

SPEC is JSON: upstream_port, store_bearer, next_poll, notice_every, native (argv), env (extra native
environment; "{port}" in a value becomes the gate port), confine (null; "wrap" or "refuse";
"close", "close-refuse" or "close-missing" first close every descriptor above 2 as the boundary
modules do), pids (a file that SIGUSR1 fills with the gate and monitor pids and the port), extra
(holder addresses), restart_delay, refresh (an argv run first under a refresh-mode gate),
connect_allow (the refresh gate's allowlist; the run gate's provider lists the same targets unless
run_connect_allow says otherwise) and bystander (a file that gets the pid of a child started before
the supervisor). Exit status: the native's, or 3 on a refusal.
"""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

HERE = Path(os.path.realpath(__file__)).parent
ROOT = HERE.parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


supervise = load("kit_supervise", ROOT / "lib/coding-agent-kit/supervise.py")


class BoundaryRefusal(Exception):
    """The boundary modules' Refusal(kind, message); str() is the message."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def main(spec):
    def confine(port, argv):
        mode = spec.get("confine")
        if mode.startswith("close"):
            os.closerange(3, 65536)
        if mode == "refuse":
            raise supervise.Refused("boundary_refused_fixture")
        if mode == "close-refuse":
            raise BoundaryRefusal("enforcement", "boundary enforcement failed in the fixture")
        if mode == "close-missing":
            return [str(HERE / "no-such-native")]
        if mode in ("wrap", "close"):
            return argv + ["--confined", str(port)]
        return argv

    if spec.get("bystander"):
        bystander = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL, start_new_session=True)
        Path(spec["bystander"]).write_text(str(bystander.pid))

    allow = spec.get("connect_allow", {})
    config = {"max_age_s": 120, "heartbeat_max_s": 1.5, "upstream_port": spec["upstream_port"],
              "holds_bearer": True, "store_bearer": spec.get("store_bearer", "fixture-bearer-A-0001"),
              "next_poll": spec.get("next_poll"), "notice_every": spec.get("notice_every"),
              "name": spec.get("name", "claude"),
              "connect_allow": spec.get("run_connect_allow", allow)}
    sup = supervise.Supervisor([sys.executable, "-I", "-B", str(HERE / "provider_gate.py")], config,
                               refresh_config=dict(config, connect_allow=allow),
                               confine=confine if spec.get("confine") else None,
                               pid_env="CODING_AGENT_KIT_CLAUDE_PID", holder_extra=spec.get("extra", ()),
                               restart_delay=spec.get("restart_delay", 0.5))
    def report(*args):
        target = Path(spec["pids"])
        target.with_suffix(".new").write_text(
            json.dumps([sup.gate_pid, sup.monitor_pid, sup.holder.port if sup.holder else None]))
        target.with_suffix(".new").replace(target)

    if spec.get("pids"):
        signal.signal(signal.SIGUSR1, report)
    try:
        port = sup.hold()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GATE_PORT": str(port)}
        env.update({name: value.replace("{port}", str(port)) for name, value in spec.get("env", {}).items()})
        if spec.get("refresh"):
            code = sup.refresh(spec["refresh"], env, timeout=10)
            sys.stdout.write("REFRESH %s\n" % code)
            sys.stdout.flush()
            if not spec.get("native"):
                return 0 if code == 0 else 1
        status = sup.run(spec["native"], env)
    except supervise.Refused as refused:
        sys.stderr.write("protected mode refused: %s\n" % refused.reason)
        return 3
    return supervise.finish(status)


if __name__ == "__main__":
    raise SystemExit(main(json.loads(sys.argv[1])))
