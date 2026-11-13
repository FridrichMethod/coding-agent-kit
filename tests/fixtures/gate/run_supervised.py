"""Run one supervised invocation on the caller's terminal: python3 -I -B run_supervised.py SPEC.

SPEC is JSON: upstream_port, store_bearer, next_poll, native (argv), env (extra native
environment), confine (null, "refuse" or "wrap"), pids (a file that SIGUSR1 fills with the gate
and monitor pids), extra (holder addresses), restart_delay, refresh (an argv run first under a
refresh-mode gate) and connect_allow. Exit status: the native's, or 3 on a refusal.
"""

import importlib.util
import json
import os
from pathlib import Path
import signal
import sys

HERE = Path(os.path.realpath(__file__)).parent
ROOT = HERE.parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


supervise = load("kit_supervise", ROOT / "lib/coding-agent-kit/supervise.py")


def main(spec):
    def confine(port, argv):
        if spec.get("confine") == "refuse":
            raise supervise.Refused("boundary_refused_fixture")
        if spec.get("confine") == "wrap":
            return argv + ["--confined", str(port)]
        return argv

    config = {"max_age_s": 120, "heartbeat_max_s": 1.5, "upstream_port": spec["upstream_port"],
              "holds_bearer": True, "store_bearer": spec.get("store_bearer", "fixture-bearer-A-0001"),
              "next_poll": spec.get("next_poll"), "name": spec.get("name", "claude"),
              "connect_allow": spec.get("connect_allow", {})}
    sup = supervise.Supervisor([sys.executable, "-I", "-B", str(HERE / "provider_gate.py")], config,
                               confine=confine if spec.get("confine") else None,
                               pid_env="CODING_AGENT_KIT_CLAUDE_PID", holder_extra=spec.get("extra", ()),
                               restart_delay=spec.get("restart_delay", 0.5))
    if spec.get("pids"):
        signal.signal(signal.SIGUSR1, lambda *args: Path(spec["pids"]).write_text(
            json.dumps([sup.gate_pid, sup.monitor_pid, sup.holder.port if sup.holder else None])))
    try:
        port = sup.hold()
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GATE_PORT": str(port)}
        env.update(spec.get("env", {}))
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
