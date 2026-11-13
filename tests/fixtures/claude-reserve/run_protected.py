"""Run one protected claude-as launch against loopback fakes: python3 -I -B run_protected.py SPEC.

SPEC is JSON: kit (the copied kit root), account, reserves ({"five_hour": "10", ...}), args (native
arguments), upstream_port (the fake Claude upstream on 127.0.0.1) and optionally connect_allow for
the refresh gate and security (a fake /usr/bin/security for the macOS Keychain lookup). It does
what launch.py does for a native-login account with an enabled reserve, except that it points the
gate at the fake upstream through protect.GATE_EXTRA, a module attribute no launch sets. Exit status: the native's, or 1 with "claude-as: MESSAGE" on stderr for a refusal.
"""

from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import sys


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(spec):
    kit = Path(spec["kit"])
    config_module = load("fixture_config", kit / "lib/coding-agent-kit/config.py")
    reserve = load("fixture_reserve", kit / "lib/coding-agent-kit/reserve.py")
    accounts = load("fixture_accounts", kit / "lib/coding-agent-kit/accounts.py")
    launch = load("fixture_launch", kit / "lib/claude-as/launch.py")
    native = load("fixture_native", kit / "lib/claude-as/native.py")
    protect = load("fixture_protect", kit / "lib/claude-as/protect.py")
    protect.GATE_EXTRA.update(upstream={"host": "127.0.0.1", "port": spec["upstream_port"], "tls": False})
    if spec.get("security"):
        protect.GATE_EXTRA.update(security=spec["security"])  # macOS: a fake, never the login Keychain
    if spec.get("connect_allow"):
        protect.REFRESH_EXTRA.update(connect_allow=spec["connect_allow"])
    config = config_module.load()
    values = {window: (reserve.OFF if value == "off" else Decimal(value)) for window, value in spec["reserves"].items()}
    policy = reserve.resolve("claude-as", spec["account"], reserve.Overrides(**values), config.usage_reserve)
    paths = native.Paths(config, spec["account"])
    record = native.load(paths)
    env = dict(os.environ, CLAUDE_CONFIG_DIR=paths.config)
    launch.usage_environment(env, config, accounts, spec["account"], None, token_id=native.token_id(record))
    executable = config.which("claude")
    try:
        return protect.launch(config, policy, paths, record, spec["args"], executable, env)
    except ValueError as error:
        print("claude-as: " + str(error), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(json.loads(sys.argv[1])))
