#!/bin/bash

set -euo pipefail

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the code under test; each case sets what it needs.
unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR XDG_CONFIG_HOME NO_COLOR

# GitHub's runners start jobs with SIGPIPE ignored, so a writer into a pipe
# whose reader stopped early prints "write error: Broken pipe" there instead
# of dying quietly. Ignore it here too, so local runs see what CI sees.
trap '' PIPE

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

prerequisites_only=0
for argument in "$@"; do
    case "$argument" in
        # CI needs no tool beyond the required set: the real Stow cases
        # stay in dotfiles. The option keeps the dotfiles command line.
        --ci) ;;
        --check-prerequisites) prerequisites_only=1 ;;
        --help)
            printf 'Usage: %s [--ci] [--check-prerequisites]\n' "$0"
            printf 'CI mode requires nothing more; local optional skips are reported.\n'
            exit 0
            ;;
        *)
            printf 'ERROR: unknown test option: %s\n' "$argument" >&2
            exit 2
            ;;
    esac
done

missing=0
for dependency in bash sh git python3 node; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        printf 'ERROR: required test dependency not found: %s\n' "$dependency" >&2
        missing=1
    fi
done
if ! command -v zsh >/dev/null 2>&1; then
    printf 'SKIP: optional zsh launcher and completion checks (install zsh to enable).\n'
fi
if ! command -v codex >/dev/null 2>&1; then
    printf 'SKIP: optional installed-Codex native sharing checks.\n'
fi
[[ "$missing" == 0 ]] || exit 1
if [[ "$prerequisites_only" == 1 ]]; then
    printf 'test-prerequisites=PASS\n'
    exit 0
fi

node --test "$TEST_DIR/claude-customizations.cjs"

tests=(
    test-entrypoints.sh
    config.sh
    codex-as.sh
    account-enrollment.sh
)

for test_name in "${tests[@]}"; do
    printf '==> %s\n' "$test_name"
    "$TEST_DIR/$test_name"
done

echo "test-suite=PASS"
