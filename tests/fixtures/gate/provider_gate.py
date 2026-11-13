"""A gate process with the fixture provider and evaluator: python3 -I -B provider_gate.py GATE-OPTIONS.

stdin carries the core configuration plus upstream_port, name, holds_bearer, store_bearer,
connect_allow ({"host:port": [host, port]}), reserves, next_poll and notice_every (the
provider's tunable).
"""

import importlib.util
import os
from pathlib import Path
import sys

HERE = Path(os.path.realpath(__file__)).parent
spec = importlib.util.spec_from_file_location("gate_fakes", HERE / "fakes.py")
fakes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fakes)


def make(config):
    allow = {target: tuple(address) for target, address in (config.get("connect_allow") or {}).items()}
    provider = fakes.FakeProvider(int(config["upstream_port"]), name=config.get("name", "codex"),
                                  holds_bearer=bool(config.get("holds_bearer")),
                                  store_bearer=config.get("store_bearer", fakes.BEARER_A), connect_allow=allow)
    if config.get("notice_every"):
        provider.notice_every = config["notice_every"]
    return provider, fakes.FakeEvaluator(config.get("reserves"), config.get("next_poll"))


if __name__ == "__main__":
    raise SystemExit(fakes.gate.main(sys.argv[1:], make))
