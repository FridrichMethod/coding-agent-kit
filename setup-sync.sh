#!/bin/sh
# Explicit, checkout-local AI-sync dependency setup. Never called by a profile.
set -eu

setup_python=python3
if [ "$#" -eq 2 ] && [ "$1" = --python ]; then
    setup_python=$2
elif [ "$#" -ne 0 ]; then
    echo "usage: setup-sync.sh [--python PYTHON_EXECUTABLE]" >&2
    exit 2
fi

setup_root=$(CDPATH='' cd -P -- "$(dirname -- "$0")" && pwd)
setup_venv=$setup_root/.venv-sync
if [ -L "$setup_venv" ]; then
    echo "setup-sync: refusing a symlinked virtual environment: $setup_venv" >&2
    exit 1
fi
"$setup_python" -I -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "Python 3.11 or newer is required")'
if [ -e "$setup_venv" ] && [ ! -f "$setup_venv/pyvenv.cfg" ]; then
    echo "setup-sync: refusing to reuse a non-venv directory: $setup_venv" >&2
    exit 1
fi
"$setup_python" -I -m venv "$setup_venv"
if [ -x "$setup_venv/bin/python" ]; then
    setup_runtime=$setup_venv/bin/python
else
    setup_runtime=$setup_venv/Scripts/python.exe
fi
"$setup_runtime" -I -m pip install --disable-pip-version-check --require-hashes --only-binary=:all: --no-deps -r "$setup_root/requirements-sync.txt"
"$setup_runtime" -I -B "$setup_root/lib/config_sync.py" --runtime-check
echo "AI-sync runtime ready in $setup_venv; no shell activation is needed."
