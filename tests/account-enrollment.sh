#!/bin/bash

set -euo pipefail

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the code under test; each case sets what it needs.
unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR XDG_CONFIG_HOME NO_COLOR

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 -I -B -m unittest discover -s "$TEST_DIR" -p test_account_enrollment.py -v
