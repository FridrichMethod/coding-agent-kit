#!/bin/bash

set -euo pipefail

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the code under test; each case sets what it needs.
unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR XDG_CONFIG_HOME NO_COLOR

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/kit-test-entrypoints.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT
mkdir "$TEST_TMP/bin"

# Deliberately omit optional tools without relying on the host's installed set.
for dependency in bash sh git python3 node dirname; do
    ln -s "$(command -v "$dependency")" "$TEST_TMP/bin/$dependency"
done
PATH="$TEST_TMP/bin" /bin/bash "$TEST_DIR/run.sh" --check-prerequisites \
    >"$TEST_TMP/local.stdout" 2>"$TEST_TMP/local.stderr"
grep -Fq 'SKIP: optional zsh launcher and completion checks' "$TEST_TMP/local.stdout"
grep -Fq 'SKIP: optional installed-Codex native sharing checks' "$TEST_TMP/local.stdout"
grep -Fxq 'test-prerequisites=PASS' "$TEST_TMP/local.stdout"

# CI installs zsh, so there its launcher and completion cases cannot skip.
if PATH="$TEST_TMP/bin" /bin/bash "$TEST_DIR/run.sh" --ci --check-prerequisites \
    >"$TEST_TMP/ci.stdout" 2>"$TEST_TMP/ci.stderr"; then
    printf 'ERROR: CI tests accepted a missing zsh.\n' >&2
    exit 1
else
    [[ "$?" == 1 ]]
fi
grep -Fq 'required test dependency not found: zsh' "$TEST_TMP/ci.stderr"
if command -v zsh >/dev/null 2>&1; then
    ln -s "$(command -v zsh)" "$TEST_TMP/bin/zsh"
    PATH="$TEST_TMP/bin" /bin/bash "$TEST_DIR/run.sh" --ci --check-prerequisites \
        >"$TEST_TMP/ci.stdout" 2>"$TEST_TMP/ci.stderr"
    if grep -Fq zsh "$TEST_TMP/ci.stdout" "$TEST_TMP/ci.stderr"; then
        printf 'ERROR: CI tests reported zsh although it is installed.\n' >&2
        exit 1
    fi
    grep -Fq 'SKIP: optional installed-Codex native sharing checks' "$TEST_TMP/ci.stdout"
    grep -Fxq 'test-prerequisites=PASS' "$TEST_TMP/ci.stdout"
    rm "$TEST_TMP/bin/zsh"
fi

rm "$TEST_TMP/bin/node"
if PATH="$TEST_TMP/bin" /bin/bash "$TEST_DIR/run.sh" --check-prerequisites \
    >"$TEST_TMP/missing.stdout" 2>"$TEST_TMP/missing.stderr"; then
    printf 'ERROR: local tests accepted a missing required Node dependency.\n' >&2
    exit 1
fi
grep -Fq 'required test dependency not found: node' "$TEST_TMP/missing.stderr"

if /bin/bash "$TEST_DIR/run.sh" --unknown-option \
    >"$TEST_TMP/option.stdout" 2>"$TEST_TMP/option.stderr"; then
    printf 'ERROR: test entrypoint accepted an unknown option.\n' >&2
    exit 1
else
    [[ "$?" == 2 ]]
fi

# Every entry point drops the kit's knobs with the one statement of
# tests/run.sh, tests/run.ps1 the same names in the same order, and the
# statement names every CODING_AGENT_KIT_* variable the code can read, so a
# caller's configuration file or colour choice cannot fail a suite.
REPO_ROOT="$(cd -- "$TEST_DIR/.." && pwd)"
# knob_statement FILE: FILE's "unset CODING_AGENT_KIT_..." lines.
knob_statement() {
    grep -E '^unset CODING_AGENT_KIT_' "$1" || true
}
STATEMENT=$(knob_statement "$TEST_DIR/run.sh")
KNOBS=${STATEMENT#unset }
case $KNOBS in
    *$'\n'* | '' | *[!A-Z_\ ]*)
        printf 'ERROR: tests/run.sh needs one unset statement for the kit knobs.\n' >&2
        exit 1
        ;;
esac
for knob in XDG_CONFIG_HOME NO_COLOR; do
    if [[ " $KNOBS " != *" $knob "* ]]; then
        printf 'ERROR: tests/run.sh does not unset %s, which the launchers read.\n' "$knob" >&2
        exit 1
    fi
done
for test_file in "$TEST_DIR"/*.sh; do
    if [[ "$(knob_statement "$test_file")" != "$STATEMENT" ]]; then
        printf 'ERROR: %s does not carry the kit knob statement of tests/run.sh.\n' "${test_file#"$REPO_ROOT"/}" >&2
        exit 1
    fi
done
PS_KNOBS=$(awk '/foreach \(\$knob in @\(/ { on = 1 } on { print } on && /\)\) \{/ { exit }' "$TEST_DIR/run.ps1" |
    grep -oE "'[A-Z_]+'" | tr -d "'" | tr '\n' ' ')
if [[ "$PS_KNOBS" != "$KNOBS " ]]; then
    printf 'ERROR: tests/run.ps1 does not remove the kit knobs that tests/run.sh unsets.\n' >&2
    exit 1
fi
# The names come from every tracked file outside tests/, docs/, .github/ and
# Markdown, whatever reads them: $NAME and ${NAME in shell, $env:NAME,
# Env:NAME and GetEnvironmentVariable('NAME') in PowerShell, os.environ in
# Python. A name embedded in a longer identifier does not count.
if ! KNOB_NAMES=$(git -C "$REPO_ROOT" -c grep.lineNumber=false -c grep.column=false \
    grep -I -h -o --no-color -E '(^|[^A-Za-z0-9_])_?CODING_AGENT_KIT_[A-Z_]+' \
    -- ':!tests' ':!docs' ':!.github' ':!*.md'); then
    printf 'ERROR: git grep found no CODING_AGENT_KIT_* name in %s.\n' "$REPO_ROOT" >&2
    exit 1
fi
READ_KNOBS=$(printf '%s\n' "$KNOB_NAMES" | sed -E 's/^[^A-Z_]//' | LC_ALL=C sort -u)
for knob in $READ_KNOBS; do
    if [[ " $KNOBS " != *" $knob "* ]]; then
        printf 'ERROR: tracked code reads %s, which the test entry points do not unset.\n' "$knob" >&2
        exit 1
    fi
done

# The knobs in the caller's shell change nothing. The fast configuration and
# metadata cases stand in for every suite; they must run a test and pass.
if ! env CODING_AGENT_KIT_CONFIG="$TEST_TMP/missing/config.toml" CODING_AGENT_KIT_COLOR=always \
    XDG_CONFIG_HOME="$TEST_TMP/missing-xdg" NO_COLOR=1 \
    /bin/bash "$TEST_DIR/config.sh" -k Loader -k metadata_queries -k unknown_keys \
    >"$TEST_TMP/hermetic.stdout" 2>"$TEST_TMP/hermetic.stderr"; then
    cat "$TEST_TMP/hermetic.stdout" "$TEST_TMP/hermetic.stderr" >&2
    printf 'ERROR: tests/config.sh fails when the caller exports the kit knobs.\n' >&2
    exit 1
fi
grep -Eq '^Ran [1-9][0-9]* tests? in ' "$TEST_TMP/hermetic.stderr"
grep -Eq '^OK( \(.*\))?$' "$TEST_TMP/hermetic.stderr"

printf 'test-entrypoints=PASS\n'
