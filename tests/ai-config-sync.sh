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

assert_mode() {
    python3 - "$1" "$2" <<'PY'
import os
import sys

actual = os.stat(sys.argv[1]).st_mode & 0o777
expected = int(sys.argv[2], 8)
assert actual == expected, f"{sys.argv[1]}: mode {actual:o}, expected {expected:o}"
PY
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
assert live["permissions"]["defaultMode"] == "auto"
assert live["autoMode"]["classifyAllShell"] is True
assert live["autoMode"]["environment"] == ["machine-only context"]
assert live["effortLevel"] == "xhigh"
assert live["statusLine"] == portable["statusLine"]
assert live["hooks"]["PreToolUse"] == portable["hooks"]["PreToolUse"]
assert live["hooks"]["Notification"] == portable["hooks"]["Notification"]
assert live["hooks"]["SessionStart"] == []
assert live["hooks"]["Stop"] == []
assert live["hooks"]["SessionEnd"] == []
assert live["hooks"]["PostToolUse"] == []
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
    "defaultMode": "acceptEdits",
    "allow": ["Bash(local-only *)"],
    "ask": ["Bash(old-policy *)"],
    "additionalDirectories": ["/machine-only/project"]
  },
  "runtimeOnly": {"keep": true},
  "autoMode": {"classifyAllShell": false, "environment": ["machine-only context"]},
  "effortLevel": "medium",
  "statusLine": {"type": "command", "command": "old-status-line"},
  "hooks": {
    "PreToolUse": [{"hooks": [{"type": "command", "command": "npx old-guard"}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "old-plugin-copy"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "old-plugin-copy"}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "old-plugin-copy"}]}],
    "PostToolUse": []
  },
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

printf '%s\n' '{"runtimeOnly":' >"$TEST_TMP/claude-malformed-live.json"
malformed_live_before="$(hash_file "$TEST_TMP/claude-malformed-live.json")"
if "$CLAUDE_SYNC" \
    "$CLAUDE_PORTABLE" \
    "$TEST_TMP/claude-malformed-live.json" >/dev/null 2>&1; then
    echo "ERROR: malformed Claude live settings unexpectedly succeeded" >&2
    exit 1
fi
[[ "$malformed_live_before" == "$(hash_file "$TEST_TMP/claude-malformed-live.json")" ]]

printf '%s\n' '[]' >"$TEST_TMP/claude-nonobject.json"
if "$CLAUDE_SYNC" \
    "$TEST_TMP/claude-nonobject.json" \
    "$TEST_TMP/claude-live.json" >/dev/null 2>&1; then
    echo "ERROR: non-object Claude baseline unexpectedly succeeded" >&2
    exit 1
fi
[[ "$claude_after" == "$(hash_file "$TEST_TMP/claude-live.json")" ]]

cat >"$TEST_TMP/claude-invalid-array.json" <<'JSON'
{"permissions":{"allow":[42],"ask":[]}}
JSON
if "$CLAUDE_SYNC" \
    "$TEST_TMP/claude-invalid-array.json" \
    "$TEST_TMP/claude-live.json" >/dev/null 2>&1; then
    echo "ERROR: non-string Claude permission unexpectedly succeeded" >&2
    exit 1
fi
[[ "$claude_after" == "$(hash_file "$TEST_TMP/claude-live.json")" ]]

# Missing/empty live files seed a fresh host and create their parent directory.
"$CLAUDE_SYNC" \
    "$CLAUDE_PORTABLE" \
    "$TEST_TMP/fresh/.claude/settings.json" >/dev/null
cmp -s "$CLAUDE_PORTABLE" "$TEST_TMP/fresh/.claude/settings.json"
assert_mode "$TEST_TMP/fresh/.claude/settings.json" 644

mkdir -p "$TEST_TMP/empty/.claude"
: >"$TEST_TMP/empty/.claude/settings.json"
"$CLAUDE_SYNC" \
    "$CLAUDE_PORTABLE" \
    "$TEST_TMP/empty/.claude/settings.json" >/dev/null
cmp -s "$CLAUDE_PORTABLE" "$TEST_TMP/empty/.claude/settings.json"

# Migrating a matching legacy Stow link must leave the baseline untouched and
# replace the live path with a regular file.
mkdir -p "$TEST_TMP/claude-symlink"
cp "$CLAUDE_PORTABLE" "$TEST_TMP/claude-symlink/portable.json"
ln -s portable.json "$TEST_TMP/claude-symlink/live.json"
claude_symlink_target_before="$(hash_file "$TEST_TMP/claude-symlink/portable.json")"
"$CLAUDE_SYNC" \
    "$TEST_TMP/claude-symlink/portable.json" \
    "$TEST_TMP/claude-symlink/live.json" >/dev/null
[[ ! -L "$TEST_TMP/claude-symlink/live.json" ]]
[[ "$claude_symlink_target_before" == "$(hash_file "$TEST_TMP/claude-symlink/portable.json")" ]]
cmp -s \
    "$TEST_TMP/claude-symlink/portable.json" \
    "$TEST_TMP/claude-symlink/live.json"

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
assert_mode "$TEST_TMP/codex/rules/portable.rules" 644

printf '%s\n' 'host-local-sentinel' >"$TEST_TMP/codex/rules/host-local.rules"
"$CODEX_RULES_SYNC" \
    "$CODEX_RULES_PORTABLE" \
    "$TEST_TMP/codex/rules/portable.rules" >/dev/null
grep -Fxq 'host-local-sentinel' "$TEST_TMP/codex/rules/host-local.rules"

# A content-identical legacy link is not a valid materialized live rules file.
mkdir -p "$TEST_TMP/rules-symlink"
cp "$CODEX_RULES_PORTABLE" "$TEST_TMP/rules-symlink/source.rules"
ln -s source.rules "$TEST_TMP/rules-symlink/live.rules"
rules_symlink_target_before="$(hash_file "$TEST_TMP/rules-symlink/source.rules")"
"$CODEX_RULES_SYNC" \
    "$TEST_TMP/rules-symlink/source.rules" \
    "$TEST_TMP/rules-symlink/live.rules" >/dev/null
[[ ! -L "$TEST_TMP/rules-symlink/live.rules" ]]
[[ "$rules_symlink_target_before" == "$(hash_file "$TEST_TMP/rules-symlink/source.rules")" ]]
cmp -s \
    "$TEST_TMP/rules-symlink/source.rules" \
    "$TEST_TMP/rules-symlink/live.rules"

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

if grep -Eq 'decision[[:space:]]*=[[:space:]]*"forbidden"' "$CODEX_RULES_PORTABLE"; then
    echo "ERROR: portable Codex policy must keep destructive commands reviewable" >&2
    exit 1
fi

if grep -Eq '(/Users/|/home/|/apps/)' "$CODEX_RULES_PORTABLE"; then
    echo "ERROR: portable Codex policy contains a host-specific path" >&2
    exit 1
fi

# Routine workspace-local execution must not be forced through the portable
# policy layer. These commands remain bounded by the active permission profile
# and any host-local rules.
for low_friction_example in \
    'git add README.md' \
    'git commit -m update' \
    'source .venv/bin/activate' \
    "bash -lc 'echo \$HOME'" \
    "python -c 'print(1)'" \
    'uv run pytest' \
    'conda run -n research python analysis.py' \
    'git push origin main' \
    'curl -LsS https://example.com' \
    'ssh lab-ubuntu hostname' \
    'npx prettier --check .' \
    'pip install package' \
    'gh pr create --fill'; do
    if grep -Fq "$low_friction_example" "$CODEX_RULES_PORTABLE"; then
        echo "ERROR: routine command remains in portable prompt policy: $low_friction_example" >&2
        exit 1
    fi
done

for auto_reviewed_example in \
    'git reset --hard HEAD~1' \
    'git clean -fd' \
    'rm -rf build' \
    'rm -f -r build' \
    'Remove-Item -Recurse build' \
    'rd /s build' \
    'shred secret.txt' \
    'dd if=image.raw of=device.img' \
    'mkfs /dev/example' \
    'sudo apt-get update'; do
    if ! grep -Fq "$auto_reviewed_example" "$CODEX_RULES_PORTABLE"; then
        echo "ERROR: required auto-review guardrail is missing: $auto_reviewed_example" >&2
        exit 1
    fi
done

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
    [[ -L "$STOW_TARGET/.claude/dotfiles/statusline.cjs" ]]
    [[ -L "$STOW_TARGET/.claude/dotfiles/notify.cjs" ]]
    [[ -L "$STOW_TARGET/.claude/dotfiles/check-git-hooks.cjs" ]]
    [[ -L "$STOW_TARGET/.codex/AGENTS.md" ]]
fi

if command -v codex >/dev/null 2>&1; then
    assert_codex_decision() {
        expected=$1
        shift
        codex_result="$(
            codex execpolicy check \
                --rules "$CODEX_RULES_PORTABLE" \
                -- "$@" 2>/dev/null
        )"
        grep -Eq \
            '"decision"[[:space:]]*:[[:space:]]*"'"$expected"'"' \
            <<<"$codex_result"
    }

    assert_codex_no_decision() {
        codex_result="$(
            codex execpolicy check \
                --rules "$CODEX_RULES_PORTABLE" \
                -- "$@" 2>/dev/null
        )"
        grep -Eq '"matchedRules"[[:space:]]*:[[:space:]]*\[\]' <<<"$codex_result"
        ! grep -Eq '"decision"[[:space:]]*:' <<<"$codex_result"
    }

    assert_codex_no_decision rm artifact.txt
    assert_codex_no_decision rm -f artifact.txt
    assert_codex_no_decision rmdir empty-directory
    assert_codex_no_decision mv source.txt destination.txt
    assert_codex_no_decision git reset --soft HEAD~1
    assert_codex_decision prompt git reset --hard HEAD~1
    assert_codex_decision prompt git clean -fd
    assert_codex_decision prompt rm -rf build
    assert_codex_decision prompt rm -f -r build
    assert_codex_decision prompt Remove-Item -Recurse build
    assert_codex_decision prompt rd /s build
    assert_codex_decision prompt shred secret.txt
    assert_codex_decision prompt dd if=image.raw of=device.img
    assert_codex_decision prompt mkfs /dev/example
    assert_codex_decision prompt sudo apt-get update
fi

echo "ai-config-sync=PASS"
