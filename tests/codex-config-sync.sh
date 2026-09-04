#!/bin/bash

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
SYNC="$REPO_ROOT/common/codex/.local/bin/codex-config-sync"
TEST_TMP="$(mktemp -d "${TMPDIR:-/tmp}/dotfiles-codex-sync.XXXXXX")"
trap 'rm -rf "$TEST_TMP"' EXIT HUP INT TERM

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

assert_status() {
    expected=$1
    shift
    set +e
    "$@" >"$TEST_TMP/status.stdout" 2>"$TEST_TMP/status.stderr"
    actual=$?
    set -e
    if [[ "$actual" -ne "$expected" ]]; then
        echo "ERROR: expected exit $expected, got $actual: $*" >&2
        sed 's/^/stdout: /' "$TEST_TMP/status.stdout" >&2
        sed 's/^/stderr: /' "$TEST_TMP/status.stderr" >&2
        exit 1
    fi
}

cat >"$TEST_TMP/portable.toml" <<'TOML'
# Runtime state left by an older symlink layout must not stay in the baseline.
model = "source-only-runtime"
model_reasoning_effort = "high"
personality = "pragmatic"
default_permissions = "workspace-net"
sandbox_mode = "workspace-write"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
project_doc_fallback_filenames = ["CLAUDE.md"]

[sandbox_workspace_write]
network_access = true
source_only = "drop-me"

[features]
network_proxy = true
memories = true
multi_agent = true
js_repl = true
source_only = "drop-me"

[permissions.workspace-net]
description = "Workspace editing with unrestricted public network access."
extends = ":workspace"
source_only = "drop-me"

[permissions.workspace-net.network]
enabled = true
allow_local_binding = false
source_only = "drop-me"

[permissions.workspace-net.network.domains]
"*" = "allow"
"source-only.example.com" = "deny"

[memories]
use_memories = true
generate_memories = true
source_only = "drop-me"

[source_only]
drop = true
TOML

cat >"$TEST_TMP/live.toml" <<'TOML'
model_reasoning_effort = "low"
personality = "friendly"
default_permissions = ":read-only"
sandbox_mode = "read-only"
approval_policy = "never"
approvals_reviewer = "manual"
project_doc_fallback_filenames = ["AGENTS.md"]
model = "machine-local-model"
runtime_flag = true

[sandbox_workspace_write]
network_access = false
writable_roots = ["/machine-only/project"]

[permissions.machine_local]
extends = ":workspace"

[permissions.workspace-net]
description = "live description"
extends = ":read-only"
runtime_profile = "keep-me"

[permissions.workspace-net.network]
enabled = false
allow_local_binding = true
runtime_network = "keep-me"

[permissions.workspace-net.network.domains]
"*" = "deny"
"private.example.com" = "deny"

[features]
network_proxy = false
multi_agent = false
memories = false
js_repl = true
runtime_feature = "keep-me"

[memories]
generate_memories = false
use_memories = false
retention_days = 30

[projects."/machine-only/project"]
trust_level = "trusted"

[[mcp_servers.demo.tools]]
name = "runtime-tool"
TOML

chmod 600 "$TEST_TMP/portable.toml"
"$SYNC" "$TEST_TMP/portable.toml" "$TEST_TMP/live.toml" >/dev/null

cat >"$TEST_TMP/expected-portable.toml" <<'TOML'
model_reasoning_effort = "high"
personality = "pragmatic"
default_permissions = "workspace-net"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
project_doc_fallback_filenames = ["CLAUDE.md"]

[features]
network_proxy = true
multi_agent = true
memories = true

[permissions.workspace-net]
description = "Workspace editing with unrestricted public network access."
extends = ":workspace"

[permissions.workspace-net.network]
enabled = true
allow_local_binding = false

[permissions.workspace-net.network.domains]
"*" = "allow"

[memories]
generate_memories = true
use_memories = true
TOML

cat >"$TEST_TMP/expected-live.toml" <<'TOML'
model_reasoning_effort = "high"
personality = "pragmatic"
default_permissions = "workspace-net"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
project_doc_fallback_filenames = ["CLAUDE.md"]
model = "machine-local-model"
runtime_flag = true

[features]
network_proxy = true
multi_agent = true
memories = true
runtime_feature = "keep-me"

[permissions.workspace-net]
description = "Workspace editing with unrestricted public network access."
extends = ":workspace"
runtime_profile = "keep-me"

[permissions.workspace-net.network]
enabled = true
allow_local_binding = false
runtime_network = "keep-me"

[permissions.workspace-net.network.domains]
"*" = "allow"
"private.example.com" = "deny"

[memories]
generate_memories = true
use_memories = true
retention_days = 30

[permissions.machine_local]
extends = ":workspace"

[projects."/machine-only/project"]
trust_level = "trusted"

[[mcp_servers.demo.tools]]
name = "runtime-tool"
TOML

cmp -s "$TEST_TMP/expected-portable.toml" "$TEST_TMP/portable.toml"
cmp -s "$TEST_TMP/expected-live.toml" "$TEST_TMP/live.toml"

assert_mode "$TEST_TMP/portable.toml" 644
assert_mode "$TEST_TMP/live.toml" 600

# A no-op sync must not replace either file.
portable_hash="$(hash_file "$TEST_TMP/portable.toml")"
live_hash="$(hash_file "$TEST_TMP/live.toml")"
portable_inode="$(python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_ino)' "$TEST_TMP/portable.toml")"
live_inode="$(python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_ino)' "$TEST_TMP/live.toml")"
"$SYNC" "$TEST_TMP/portable.toml" "$TEST_TMP/live.toml" >/dev/null
[[ "$portable_hash" == "$(hash_file "$TEST_TMP/portable.toml")" ]]
[[ "$live_hash" == "$(hash_file "$TEST_TMP/live.toml")" ]]
[[ "$portable_inode" == "$(python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_ino)' "$TEST_TMP/portable.toml")" ]]
[[ "$live_inode" == "$(python3 -c 'import os, sys; print(os.stat(sys.argv[1]).st_ino)' "$TEST_TMP/live.toml")" ]]

# A fresh host gets a parent directory and a complete live config.
mkdir -p "$TEST_TMP/fresh"
cp "$TEST_TMP/portable.toml" "$TEST_TMP/fresh/portable.toml"
"$SYNC" \
    "$TEST_TMP/fresh/portable.toml" \
    "$TEST_TMP/fresh/home/.codex/config.toml" >/dev/null
cmp -s \
    "$TEST_TMP/fresh/portable.toml" \
    "$TEST_TMP/fresh/home/.codex/config.toml"
assert_mode "$TEST_TMP/fresh/home/.codex/config.toml" 600

# Migrating an old Stow layout must replace the live symlink, not its target.
mkdir -p "$TEST_TMP/symlink"
cp "$TEST_TMP/portable.toml" "$TEST_TMP/symlink/portable.toml"
ln -s portable.toml "$TEST_TMP/symlink/live.toml"
symlink_target_hash="$(hash_file "$TEST_TMP/symlink/portable.toml")"
"$SYNC" \
    "$TEST_TMP/symlink/portable.toml" \
    "$TEST_TMP/symlink/live.toml" >/dev/null
[[ ! -L "$TEST_TMP/symlink/live.toml" ]]
[[ "$symlink_target_hash" == "$(hash_file "$TEST_TMP/symlink/portable.toml")" ]]
cmp -s "$TEST_TMP/symlink/portable.toml" "$TEST_TMP/symlink/live.toml"

# Validation failures are fail-closed for both the portable source and live file.
cat >"$TEST_TMP/incomplete.toml" <<'TOML'
model_reasoning_effort = "high"
personality = "pragmatic"
default_permissions = "workspace-net"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
project_doc_fallback_filenames = ["CLAUDE.md"]

[features]
network_proxy = true
multi_agent = true
memories = true

[permissions.workspace-net]
description = "Workspace editing with unrestricted public network access."
extends = ":workspace"

[permissions.workspace-net.network]
enabled = true
allow_local_binding = false

[permissions.workspace-net.network.domains]
"*" = "allow"

[memories]
generate_memories = true
TOML
printf '%s\n' 'live-sentinel = true' >"$TEST_TMP/failure-live.toml"
incomplete_hash="$(hash_file "$TEST_TMP/incomplete.toml")"
failure_live_hash="$(hash_file "$TEST_TMP/failure-live.toml")"
assert_status 1 "$SYNC" "$TEST_TMP/incomplete.toml" "$TEST_TMP/failure-live.toml"
[[ "$incomplete_hash" == "$(hash_file "$TEST_TMP/incomplete.toml")" ]]
[[ "$failure_live_hash" == "$(hash_file "$TEST_TMP/failure-live.toml")" ]]

# A legacy-only baseline must fail closed instead of silently retaining an old
# sandbox_mode that would bypass the selected permission profile.
cat >"$TEST_TMP/legacy-only.toml" <<'TOML'
model_reasoning_effort = "high"
personality = "pragmatic"
sandbox_mode = "workspace-write"
approval_policy = "on-request"
approvals_reviewer = "auto_review"
project_doc_fallback_filenames = ["CLAUDE.md"]

[sandbox_workspace_write]
network_access = true

[features]
multi_agent = true
memories = true

[memories]
generate_memories = true
use_memories = true
TOML
legacy_hash="$(hash_file "$TEST_TMP/legacy-only.toml")"
assert_status 1 "$SYNC" "$TEST_TMP/legacy-only.toml" "$TEST_TMP/failure-live.toml"
[[ "$legacy_hash" == "$(hash_file "$TEST_TMP/legacy-only.toml")" ]]
[[ "$failure_live_hash" == "$(hash_file "$TEST_TMP/failure-live.toml")" ]]

assert_status 1 "$SYNC" "$TEST_TMP/missing.toml" "$TEST_TMP/failure-live.toml"
assert_status 2 "$SYNC"
assert_status 2 "$SYNC" one two three

echo "codex-config-sync=PASS"
