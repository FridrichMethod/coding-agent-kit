#!/bin/sh
# Sourced by the resolved sync wrapper; never installs or activates anything.
: "${sync_root:?sync wrapper must set the checkout root}"
: "${sync_kind:?sync wrapper must set its helper kind}"

if [ "${DOTFILES_SYNC_PYTHON+x}" = x ]; then
    sync_python=$DOTFILES_SYNC_PYTHON
    # PowerShell/Windows Python emits native paths; Git Bash executes POSIX paths.
    if command -v cygpath >/dev/null 2>&1 && [ -n "$sync_python" ]; then
        sync_python=$(cygpath -u "$sync_python")
    fi
elif [ -x "$sync_root/.venv-sync/bin/python" ]; then
    sync_python=$sync_root/.venv-sync/bin/python
else
    sync_python=$sync_root/.venv-sync/Scripts/python.exe
fi

if [ ! -x "$sync_python" ]; then
    echo "config-sync: Python runtime unavailable; run $sync_root/setup-sync.sh (or setup-sync.ps1) explicitly, or set DOTFILES_SYNC_PYTHON to a provisioned interpreter path" >&2
    exit 1
fi

exec "$sync_python" -I -B -X utf8 "$sync_root/lib/config_sync.py" "$sync_kind" "$@"
