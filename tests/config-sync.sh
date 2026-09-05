#!/bin/bash

set -euo pipefail

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$TEST_DIR/test_config_sync.py" ]]; then
    echo 'ERROR: required configuration backend test suite is missing.' >&2
    exit 1
fi
sync_python=${DOTFILES_SYNC_PYTHON:-$TEST_DIR/../.venv-sync/bin/python}
if [[ ! -x "$sync_python" ]]; then
    echo 'ERROR: AI-sync runtime missing; run ./setup-sync.sh explicitly.' >&2
    exit 1
fi
export DOTFILES_SYNC_PYTHON="$sync_python"
exec "$sync_python" -I -B -m unittest discover -s "$TEST_DIR" -p 'test_config_sync.py' -v
