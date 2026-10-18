#!/bin/bash

set -euo pipefail

# Hermetic: the provisioning exports (DOTFILES_AUTO_UPDATE=0 and the like,
# docs/bootstrap.md) and other dotfiles knobs never reach the code under
# test from the caller; each case sets what it needs.
unset DOTFILES_AUTO_UPDATE DOTFILES_AUTO_STOW DOTFILES_HOST DOTFILES_DIR _DOTFILES_CHECKED \
    DOTFILES_STOW_WITHOUT_OH_MY_ZSH DOTFILES_COLOR AWESOME_SKILLS_AUTO_UPDATE AWESOME_SKILLS_FORCE \
    AWESOME_SKILLS_BG AWESOME_SKILLS_INSTALLER_URL AWESOME_SKILLS_REFRESH_DAYS _AWESOME_SKILLS_CHECKED

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/dotfiles-ai-sync.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT HUP INT TERM

CLAUDE_PORTABLE="$REPO_ROOT/tests/fixtures/claude-settings.json"
CLAUDE_SYNC="$REPO_ROOT/common/claude/.local/bin/claude-settings-sync"
CODEX_RULES_PORTABLE="$REPO_ROOT/tests/fixtures/portable.rules"
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

# The stub import is external when launched from a subdirectory; older clients
# read only the legacy plugin ID, so native AGENTS.md loading needs both IDs.
for plugin_id in ("agents-md@builtin", "cc-plugin-agents-md@builtin"):
    assert portable["pluginConfigs"][plugin_id] == {
        "options": {"instructionFiles": "claude-md-and-agents-md"}
    }
    assert live["pluginConfigs"][plugin_id] == portable["pluginConfigs"][plugin_id]
PY
}

# Preflight validates a complete candidate without creating an absent target
# parent, materializing a file, or touching either portable source.
claude_source_before="$(hash_file "$CLAUDE_PORTABLE")"
rules_source_before="$(hash_file "$CODEX_RULES_PORTABLE")"
"$CLAUDE_SYNC" --check "$CLAUDE_PORTABLE" "$TEST_TMP/check-only/claude/settings.json" >/dev/null
"$CODEX_RULES_SYNC" --check "$CODEX_RULES_PORTABLE" "$TEST_TMP/check-only/codex/portable.rules" >/dev/null
[[ ! -e "$TEST_TMP/check-only" ]]
[[ "$claude_source_before" == "$(hash_file "$CLAUDE_PORTABLE")" ]]
[[ "$rules_source_before" == "$(hash_file "$CODEX_RULES_PORTABLE")" ]]

# Explicit source migration belongs only to Codex TOML, not JSON or rules.
if "$CLAUDE_SYNC" --migrate-portable "$CLAUDE_PORTABLE" "$TEST_TMP/check-only/settings.json" >/dev/null 2>&1; then
    echo "ERROR: Claude unexpectedly accepted Codex-only migration" >&2
    exit 1
fi
[[ ! -e "$TEST_TMP/check-only" ]]

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

echo "ai-config-sync=PASS"
