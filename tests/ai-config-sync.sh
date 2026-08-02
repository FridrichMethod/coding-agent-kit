#!/bin/bash

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/dotfiles-ai-sync.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT HUP INT TERM

CLAUDE_PORTABLE="$REPO_ROOT/common/claude/.claude/settings.json"
CLAUDE_SYNC="$REPO_ROOT/common/claude/.local/bin/claude-settings-sync"
CODEX_RULES_PORTABLE="$REPO_ROOT/common/codex/.codex/rules/portable.rules"
CODEX_RULES_SYNC="$REPO_ROOT/common/codex/.local/bin/codex-rules-sync"

hash_file() {
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        sha256sum "$1" | awk '{print $1}'
    fi
}

assert_json_policy() {
    python3 - "$CLAUDE_PORTABLE" "$1" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    portable = json.load(handle)
with open(sys.argv[2]) as handle:
    live = json.load(handle)

expected_allow = {
    "WebSearch",
    "mcp__plugin_everything-claude-code_github__search_code",
    "mcp__plugin_everything-claude-code_github__get_file_contents",
    "Bash(git commit *)",
    "Bash(rmdir *)",
    "Bash(mv *)",
    "Bash(git add *)",
    "Bash(git push *)",
    "Bash(git pull *)",
    "Bash(git clone *)",
    "Bash(git reset *)",
    "Bash(git restore *)",
    "Bash(git checkout *)",
    "Bash(curl *)",
    "Bash(wget *)",
    "Bash(ssh *)",
    "Bash(scp *)",
    "Bash(rsync *)",
    "Bash(tee *)",
    "Bash(source *)",
    "Bash(. *)",
    "Bash(pip install *)",
    "Bash(pip3 install *)",
    "Bash(uv pip install *)",
    "Bash(conda install *)",
    "Bash(mamba install *)",
    "Bash(npm install *)",
    "Bash(pnpm install *)",
    "Bash(yarn add *)",
    "Bash(brew install *)",
    "Bash(gh api *)",
    "Bash(gh pr create *)",
    "Bash(gh pr merge *)",
    "Bash(gh issue create *)",
    "Bash(gh release create *)",
}

assert set(portable["permissions"]["allow"]) == expected_allow
assert live["permissions"]["allow"] == portable["permissions"]["allow"]
assert live["permissions"]["ask"] == portable["permissions"]["ask"]
assert live["permissions"]["additionalDirectories"] == ["/machine-only/project"]
assert live["runtimeOnly"] == {"keep": True}
assert live["model"] == "machine-local-model"
assert live["attribution"] == {"commit": "", "pr": ""}

# permissions.ask is now the narrow list (see CLAUDE.md): only actions whose
# worst case is unbounded or irrecoverable. Everything else lives in allow.
unsafe_allow_fragments = (
    "rm ",
    "git clean",
    "sudo",
    "npx",
)
assert not any(
    fragment in rule
    for rule in live["permissions"]["allow"]
    for fragment in unsafe_allow_fragments
)
PY
}

cat >"$TEST_TMP/claude-live.json" <<'JSON'
{
  "permissions": {
    "allow": ["Bash(local-only *)"],
    "ask": ["Bash(old-policy *)"],
    "additionalDirectories": ["/machine-only/project"]
  },
  "runtimeOnly": {"keep": true},
  "model": "machine-local-model"
}
JSON

"$CLAUDE_SYNC" "$CLAUDE_PORTABLE" "$TEST_TMP/claude-live.json" >/dev/null
assert_json_policy "$TEST_TMP/claude-live.json"

claude_before="$(hash_file "$TEST_TMP/claude-live.json")"
"$CLAUDE_SYNC" "$CLAUDE_PORTABLE" "$TEST_TMP/claude-live.json" >/dev/null
claude_after="$(hash_file "$TEST_TMP/claude-live.json")"
[[ "$claude_before" == "$claude_after" ]]

printf '%s\n' '{"permissions":' >"$TEST_TMP/claude-malformed.json"
if "$CLAUDE_SYNC" \
    "$TEST_TMP/claude-malformed.json" \
    "$TEST_TMP/claude-live.json" >/dev/null 2>&1; then
    echo "ERROR: malformed Claude baseline unexpectedly succeeded" >&2
    exit 1
fi
[[ "$claude_after" == "$(hash_file "$TEST_TMP/claude-live.json")" ]]

printf '%s\n' '{"permissions":{"allow":[]}}' >"$TEST_TMP/claude-missing-ask.json"
if "$CLAUDE_SYNC" \
    "$TEST_TMP/claude-missing-ask.json" \
    "$TEST_TMP/claude-live.json" >/dev/null 2>&1; then
    echo "ERROR: incomplete Claude permission baseline unexpectedly succeeded" >&2
    exit 1
fi
[[ "$claude_after" == "$(hash_file "$TEST_TMP/claude-live.json")" ]]

mkdir -p "$TEST_TMP/codex/rules"
printf '%s\n' \
    'prefix_rule(pattern=["host-only"], decision="allow")' \
    >"$TEST_TMP/codex/rules/default.rules"
default_before="$(hash_file "$TEST_TMP/codex/rules/default.rules")"

"$CODEX_RULES_SYNC" \
    "$CODEX_RULES_PORTABLE" \
    "$TEST_TMP/codex/rules/portable.rules" >/dev/null
cmp -s "$CODEX_RULES_PORTABLE" "$TEST_TMP/codex/rules/portable.rules"
[[ "$default_before" == "$(hash_file "$TEST_TMP/codex/rules/default.rules")" ]]

portable_before="$(hash_file "$TEST_TMP/codex/rules/portable.rules")"
"$CODEX_RULES_SYNC" \
    "$CODEX_RULES_PORTABLE" \
    "$TEST_TMP/codex/rules/portable.rules" >/dev/null
[[ "$portable_before" == "$(hash_file "$TEST_TMP/codex/rules/portable.rules")" ]]

: >"$TEST_TMP/empty.rules"
if "$CODEX_RULES_SYNC" \
    "$TEST_TMP/empty.rules" \
    "$TEST_TMP/codex/rules/portable.rules" >/dev/null 2>&1; then
    echo "ERROR: empty Codex rules baseline unexpectedly succeeded" >&2
    exit 1
fi
[[ "$portable_before" == "$(hash_file "$TEST_TMP/codex/rules/portable.rules")" ]]

if grep -Eq 'decision[[:space:]]*=[[:space:]]*"allow"' "$CODEX_RULES_PORTABLE"; then
    echo "ERROR: portable Codex policy must not grant cross-host allow rules" >&2
    exit 1
fi

if grep -Eq '(/Users/|/home/|/apps/)' "$CODEX_RULES_PORTABLE"; then
    echo "ERROR: portable Codex policy contains a host-specific path" >&2
    exit 1
fi

grep -Fq 'codex-rules-sync' "$REPO_ROOT/stow-all.sh"
grep -Fq 'codex-rules-sync' "$REPO_ROOT/stow-all.ps1"
grep -Fq '\.codex/rules/portable\.rules' "$REPO_ROOT/.stowrc"

if command -v stow >/dev/null 2>&1; then
    STOW_TARGET="$TEST_TMP/stow-target"
    mkdir -p \
        "$STOW_TARGET/.claude" \
        "$STOW_TARGET/.codex/rules"
    printf '%s\n' 'claude-live-sentinel' \
        >"$STOW_TARGET/.claude/settings.json"
    printf '%s\n' 'codex-live-sentinel' \
        >"$STOW_TARGET/.codex/config.toml"
    printf '%s\n' 'rules-live-sentinel' \
        >"$STOW_TARGET/.codex/rules/portable.rules"

    (
        cd "$REPO_ROOT"
        stow \
            --restow \
            --no-folding \
            --target="$STOW_TARGET" \
            -d "$REPO_ROOT/common" \
            claude codex
    )

    [[ ! -L "$STOW_TARGET/.claude/settings.json" ]]
    [[ ! -L "$STOW_TARGET/.codex/config.toml" ]]
    [[ ! -L "$STOW_TARGET/.codex/rules/portable.rules" ]]
    grep -Fxq 'claude-live-sentinel' "$STOW_TARGET/.claude/settings.json"
    grep -Fxq 'codex-live-sentinel' "$STOW_TARGET/.codex/config.toml"
    grep -Fxq 'rules-live-sentinel' "$STOW_TARGET/.codex/rules/portable.rules"
    [[ -L "$STOW_TARGET/.claude/CLAUDE.md" ]]
    [[ -L "$STOW_TARGET/.codex/AGENTS.md" ]]
fi

if command -v codex >/dev/null 2>&1; then
    codex_result="$(
        codex execpolicy check \
            --rules "$CODEX_RULES_PORTABLE" \
            -- git push origin main 2>/dev/null
    )"
    grep -Eq '"decision"[[:space:]]*:[[:space:]]*"prompt"' <<<"$codex_result"
fi

echo "ai-config-sync=PASS"
