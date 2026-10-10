#!/bin/bash

set -euo pipefail

# Hermetic: the provisioning exports (DOTFILES_AUTO_UPDATE=0 and the like,
# docs/bootstrap.md) and other dotfiles knobs never reach the code under
# test from the caller; each case sets what it needs.
unset DOTFILES_AUTO_UPDATE DOTFILES_AUTO_STOW DOTFILES_HOST DOTFILES_DIR _DOTFILES_CHECKED \
    DOTFILES_STOW_WITHOUT_OH_MY_ZSH DOTFILES_COLOR AWESOME_SKILLS_AUTO_UPDATE AWESOME_SKILLS_FORCE \
    AWESOME_SKILLS_BG AWESOME_SKILLS_INSTALLER_URL AWESOME_SKILLS_REFRESH_DAYS _AWESOME_SKILLS_CHECKED

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
