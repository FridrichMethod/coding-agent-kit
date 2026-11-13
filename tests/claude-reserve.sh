#!/bin/bash

set -euo pipefail

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the code under test; each case sets what it needs.
unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR CODING_AGENT_KIT_USAGE_FILE CODING_AGENT_KIT_TOKEN_ID CODING_AGENT_KIT_CLAUDE_PID CODING_AGENT_KIT_REPO_URL XDG_CONFIG_HOME XDG_STATE_HOME NO_COLOR

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Arguments go to unittest. Protected launches need a PTY and the OS boundary, and
# RealClaude needs a reviewed Claude Code as KIT_REAL_CLAUDE=/path/to/claude or as claude on PATH;
# each skips cleanly without them.
python3 -I -B -m unittest discover -s "$TEST_DIR" -p test_claude_reserve.py -v "$@"
