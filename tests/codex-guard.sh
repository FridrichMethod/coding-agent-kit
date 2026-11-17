#!/bin/bash

set -euo pipefail

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the code under test; each case sets what it needs.
unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR CODING_AGENT_KIT_USAGE_FILE CODING_AGENT_KIT_TOKEN_ID CODING_AGENT_KIT_CLAUDE_PID CODING_AGENT_KIT_REPO_URL CODING_AGENT_KIT_GUARD CODING_AGENT_KIT_GUARD_SCRIPT CODING_AGENT_KIT_GUARD_PYTHON XDG_CONFIG_HOME XDG_STATE_HOME NO_COLOR

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# The pre-commit hook passes --hook first: every in-process class and one launch
# case per launch form. ./tests/run.sh runs every case, the full launch matrix
# and the real Codex among them. HookSelection checks this list.
if [[ "${1:-}" == --hook ]]; then
    shift
    set -- -k .LaunchForms. -k .Version. -k .HooksOff. -k .ManagedGoals. -k .HookState. \
        -k .RunAttribution. -k .Hooks. -k .SandboxPath. -k .HookSelection. \
        -k .GuardedLaunch.test_an_interactive_launch_gets_the_hooks_the_guard_and_goals_off \
        -k .GuardedLaunch.test_a_headless_run_prints_the_guard_s_block_after_codex_exits \
        -k .GuardedLaunch.test_forms_the_hooks_cannot_see_are_refused \
        -k .GuardedLaunch.test_a_plain_form_launches_as_without_reserves "$@"
fi
# Other arguments go to unittest, for example -k LaunchForms.
python3 -I -B -m unittest discover -s "$TEST_DIR" -p test_codex_guard.py -v "$@"
