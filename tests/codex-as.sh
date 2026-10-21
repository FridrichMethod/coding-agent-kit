#!/bin/bash

set -euo pipefail

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the code under test; each case sets what it needs.
unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR CODING_AGENT_KIT_USAGE_FILE CODING_AGENT_KIT_TOKEN_ID XDG_CONFIG_HOME XDG_STATE_HOME NO_COLOR

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 -I -B "$TEST_DIR/test_codex_as.py"
python3 -I -B "$TEST_DIR/test_codex_sharing_native.py"
