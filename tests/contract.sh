#!/bin/bash
# The public contract that consumers build on: the paths they link or copy,
# with their modes, and the two test files a consumer may rerun with its own
# Claude settings. Changing any of it is a breaking change.

set -euo pipefail

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the code under test; each case sets what it needs.
unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR CODING_AGENT_KIT_USAGE_FILE CODING_AGENT_KIT_TOKEN_ID CODING_AGENT_KIT_CLAUDE_PID XDG_CONFIG_HOME XDG_STATE_HOME NO_COLOR

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -P -- "$TEST_DIR/.." && pwd)"

die() {
    printf 'ERROR: contract: %s\n' "$1" >&2
    exit 1
}

# Tracked mode and path, as git ls-files -s records them.
LAYOUT='100755 bin/codex-as
100755 bin/claude-as
100644 lib/codex-as/launch.py
100644 lib/codex-as/check.py
100644 lib/codex-as/layout.py
100644 lib/codex-as/handoff.py
100644 lib/codex-as/usage.py
100644 lib/claude-as/launch.py
100644 lib/coding-agent-kit/config.py
100644 lib/coding-agent-kit/accounts.py
100644 completions/bash/codex-as
100644 completions/bash/claude-as
100644 completions/zsh/_codex-as
100644 completions/zsh/_claude-as
100644 helpers/statusline.cjs
100644 helpers/notify.cjs
100644 helpers/check-git-hooks.cjs
100755 install.sh
100644 docs/account-launchers.md
100644 docs/codex-as.md
100644 docs/configuration.md
100644 tests/claude-customizations.cjs
100644 tests/fixtures/claude-settings.json'

git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$ROOT is not a Git checkout."
paths=()
while read -r _ path; do
    paths+=("$path")
done <<<"$LAYOUT"
tracked="$(git -C "$ROOT" ls-files -s -- "${paths[@]}" | awk -F '\t' '{ split($1, a, " "); print a[1], $2 }' | LC_ALL=C sort)"
if [[ "$tracked" != "$(LC_ALL=C sort <<<"$LAYOUT")" ]]; then
    diff -u <(LC_ALL=C sort <<<"$LAYOUT") <(printf '%s\n' "$tracked") >&2 || true
    die 'the tracked public paths or their modes differ from the contract (- expected, + tracked).'
fi
while read -r mode path; do
    [[ -f "$ROOT/$path" && ! -L "$ROOT/$path" ]] || die "$path is not a regular file in the working tree."
    [[ "$mode" == 100644 || -x "$ROOT/$path" ]] || die "$path is not executable in the working tree."
done <<<"$LAYOUT"
for entry in 'bin/codex-as #!/bin/bash' 'bin/claude-as #!/bin/sh' 'install.sh #!/bin/sh'; do
    [[ "$(head -n 1 "$ROOT/${entry%% *}")" == "${entry#* }" ]] || die "${entry%% *} must start with ${entry#* }."
done

# The kit tracks no symlinks: a checkout must work where links are unsupported.
links="$(git -C "$ROOT" ls-files -s | awk -F '\t' '$1 ~ /^120000 / { print $2 }')"
[[ -z "$links" ]] || die "tracked symlinks: $(tr '\n' ' ' <<<"$links")"

printf 'contract=PASS\n'
