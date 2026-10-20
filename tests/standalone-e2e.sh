#!/bin/bash
# A standalone install without dotfiles: install.sh under /bin/sh, bash and
# dash in temporary homes, then codex-as, claude-as and their completions
# through the installed links, with codex and claude fakes that only record.

set -euo pipefail

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the code under test; each case sets what it needs.
unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR XDG_CONFIG_HOME NO_COLOR

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -P -- "$TEST_DIR/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/kit-standalone-e2e.XXXXXX")"
# A failed case can leave a directory of a home read-only or unsearchable.
trap 'chmod -R u+rwx "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT
TMP="$(cd -P -- "$TMP" && pwd)"

KIT="$TMP/kit [with] spaces*"
OTHER="$TMP/other checkout"
FAKES="$TMP/fake bin"
ALT="$TMP/alternative tools"
TOOLS="$TMP/tools"
OUT="$TMP/out"
WORK="$TMP/work"
RECORD="$TMP/record"
mkdir "$KIT" "$OTHER" "$FAKES" "$ALT" "$TOOLS" "$OUT" "$WORK"
# The checkout under test, copied: what a clone provides, at a path with spaces
# and pattern characters, so an unquoted path in a pattern shows.
cp -R "$ROOT/bin" "$ROOT/lib" "$ROOT/completions" "$ROOT/helpers" "$ROOT/docs" "$ROOT/install.sh" "$KIT/"
cp -R "$ROOT/bin" "$ROOT/completions" "$OTHER/"
KIT_TREE="$(cd -- "$KIT" && find . -print | LC_ALL=C sort)"

# The commands under test see only these tools, the fakes and the install's bin.
SHELLS=(/bin/sh bash)
for tool in sh bash python3 dirname readlink mkdir ln rm cksum env dash zsh; do
    if command -v "$tool" >/dev/null 2>&1; then
        ln -s "$(command -v "$tool")" "$TOOLS/$tool"
    elif [[ "$tool" != dash && "$tool" != zsh ]]; then
        printf 'ERROR: standalone-e2e needs %s.\n' "$tool" >&2
        exit 1
    fi
done
if [[ -e "$TOOLS/dash" ]]; then
    SHELLS+=(dash)
else
    printf 'standalone-e2e: dash not installed; install.sh runs under /bin/sh and bash only.\n'
fi
[[ -e "$TOOLS/zsh" ]] || printf 'standalone-e2e: zsh not installed; zsh completion not checked.\n'

# The fakes print their version, or record argv, CODEX_HOME and a checksum of
# the Claude token; they never touch the network or print a token.
cat >"$FAKES/codex" <<'EOF'
#!/bin/sh
if [ "$*" = --version ]; then
    case ${0##*/} in codex*) echo 'codex-cli 0.163.0' ;; *) echo '2.1.296 (Claude Code)' ;; esac
    exit 0
fi
{
    printf 'run=%s\n' "${0##*/}"
    for arg in "$@"; do printf 'arg=%s\n' "$arg"; done
    printf 'CODEX_HOME=%s\n' "${CODEX_HOME-}"
    printf 'token=%s\n' "$(printf %s "${CLAUDE_CODE_OAUTH_TOKEN-}" | cksum)"
} >>"$FAKE_RECORD"
EOF
chmod 755 "$FAKES/codex"
cp "$FAKES/codex" "$FAKES/claude"
cp "$FAKES/codex" "$ALT/codex-alt"
cp "$FAKES/codex" "$ALT/claude-alt"

# A fresh session without a controlling terminal: a prompt fails instead of
# waiting, and a hang ends after 60 s.
DETACH='import os, signal, sys
pid = os.fork()
if pid == 0:
    os.setsid()
    os.execvp(sys.argv[1], sys.argv[1:])
signal.signal(signal.SIGALRM, lambda *_: (os.killpg(pid, signal.SIGKILL), os._exit(124)))
signal.alarm(60)
status = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
sys.exit(status if status >= 0 else 128 - status)'

die() {
    printf 'ERROR: standalone-e2e: %s\n' "$1" >&2
    exit 1
}

# run LABEL [NAME=VALUE...] COMMAND [ARG...]: COMMAND in an empty directory with
# only HOME ($H), PATH, TMPDIR, the record file and the given variables; sets
# rc, and leaves the output in $OUT/LABEL.out and $OUT/LABEL.err.
run() {
    local label=$1
    shift
    set +e
    (cd -- "$WORK" && env -i HOME="$H" PATH="$FAKES:$H/.local/bin:$TOOLS" TMPDIR="$TMP" FAKE_RECORD="$RECORD" \
        "$TOOLS/python3" -I -c "$DETACH" "$TOOLS/env" "$@" >"$OUT/$label.out" 2>"$OUT/$label.err" </dev/null)
    rc=$?
    set -e
}

# same FILE TEXT: FILE holds exactly TEXT plus a newline, or nothing for an empty TEXT.
same() {
    if [[ -z "$2" ]]; then
        [[ ! -s "$1" ]] && return
        cat "$1" >&2
    else
        printf '%s\n' "$2" | diff -u - "$1" >&2 && return
    fi
    die "unexpected content in ${1#"$OUT"/}"
}

# expect LABEL RC STDOUT [STDERR]: the status and both outputs of run LABEL.
expect() {
    if [[ "$rc" != "$2" ]]; then
        cat "$OUT/$1.out" "$OUT/$1.err" >&2
        die "$1: exit $rc, expected $2"
    fi
    same "$OUT/$1.out" "$3"
    same "$OUT/$1.err" "${4-}"
}

# snapshot DIR: every path below DIR, with link texts.
snapshot() {
    (cd -- "$1" && find . -print | LC_ALL=C sort | while IFS= read -r path; do
        if [[ -L "$path" ]]; then
            printf '%s -> %s\n' "$path" "$(readlink "$path")"
        else
            printf '%s\n' "$path"
        fi
    done)
}

# home NAME: a new HOME ($H) with a private .codex and .secrets and one
# fabricated account for each tool.
home() {
    H="$TMP/$1 home"
    mkdir -m 700 "$H" "$H/.codex" "$H/.secrets"
    seed "$H/.secrets" A work
}

# seed DIR CODEX-NAME CLAUDE-NAME: fabricated 0600 credentials.
seed() {
    (
        umask 077
        printf '{"auth_mode": "chatgpt", "last_refresh": "2026-10-01T00:00:00Z", "tokens": {%s, %s, %s, %s}}\n' \
            '"id_token": "fake.eyJzdWIiOiJlMmUifQ.fake"' '"access_token": "fabricated-access"' \
            '"refresh_token": "fabricated-refresh"' "\"account_id\": \"$2\"" >"$1/codex-$2.json"
        printf '%s\n' "sk-ant-oat01-fabricated-e2e-$3" >"$1/claude-$3"
    )
}

checksum() {
    printf %s "sk-ant-oat01-fabricated-e2e-$1" | cksum
}

# installed PREFIX BASH-DIR [PATH]: what install.sh prints; PATH when PREFIX/bin is not on it.
installed() {
    local tool zsh_dir="$1/share/zsh/site-functions"
    for tool in codex-as claude-as; do
        printf '%s -> %s\n' "$1/bin/$tool" "$KIT/bin/$tool" "$2/$tool" "$KIT/completions/bash/$tool" \
            "$zsh_dir/_$tool" "$KIT/completions/zsh/_$tool"
    done
    printf '%s\n' 'zsh: unless fpath has it, add this before compinit (with oh-my-zsh, before sourcing it):' \
        "  fpath=(\"$zsh_dir\" \$fpath)" 'Claude Code helpers, as commands in settings.json:' \
        "  statusLine:             node \"$KIT/helpers/statusline.cjs\"" \
        "  Notification hook:      node \"$KIT/helpers/notify.cjs\"" \
        "  PreToolUse hook (Bash): node \"$KIT/helpers/check-git-hooks.cjs\""
    if [[ -n "${3-}" ]]; then
        printf 'PATH: add %s\n' "$1/bin"
    fi
}

# refuse LABEL REFUSED KEPT: under every shell, install.sh refuses this home
# ($H) with the REFUSED lines and --uninstall keeps it with KEPT; neither
# changes a path below $H.
refuse() {
    local shell before
    before="$(snapshot "$H")"
    for shell in "${SHELLS[@]}"; do
        run "$1-${shell//\//}" "$shell" "$KIT/install.sh"
        expect "$1-${shell//\//}" 1 '' "$2
install.sh: nothing changed; move those paths aside or choose another --prefix."
        run "$1-uninstall-${shell//\//}" "$shell" "$KIT/install.sh" --uninstall
        expect "$1-uninstall-${shell//\//}" 0 "$3"
        [[ "$(snapshot "$H")" == "$before" ]] || die "$1: install.sh under $shell changed a refused home"
    done
}

# offer SHELL TOOL: the account names that SHELL's installed completion offers.
offer() {
    if [[ "$1" == bash ]]; then
        run "complete-$1-$2" bash --noprofile --norc -c 'source "$1"; COMP_WORDS=("$2" ""); COMP_CWORD=1; "$3"
            [ "${#COMPREPLY[@]}" -eq 0 ] || printf "%s\n" "${COMPREPLY[@]}"' \
            bash "$H/.local/share/bash-completion/completions/$2" "$2" "_${2//-/_}"
    else
        # The fpath line install.sh printed, then autoload as compinit would.
        run "complete-$1-$2" zsh -f -c "$(sed -n 's/^  \(fpath=.*\)$/\1/p' "$OUT/install-sh.out")"'
            autoload -Uz "_$1"
            _describe() { [[ $1 == -t ]] || return 0; shift 3; print -rl -- "${(@P)1}"; }
            CURRENT=2; words=("$1" ""); "_$1"' zsh "$2"
    fi
}

# completions CODEX-NAME CLAUDE-NAME: what the installed bash and zsh completions offer.
completions() {
    local shell
    for shell in bash zsh; do
        [[ -e "$TOOLS/$shell" ]] || continue
        offer "$shell" codex-as
        expect "complete-$shell-codex-as" 0 "$1"
        offer "$shell" claude-as
        expect "complete-$shell-claude-as" 0 "$2"
    done
}

printf '==> install.sh under %s, run directly, by a relative name and through a link\n' "${SHELLS[*]}"
home default
DEFAULT_LINKS="$(installed "$H/.local" "$H/.local/share/bash-completion/completions")"
run install-sh /bin/sh "$KIT/install.sh"
expect install-sh 0 "$DEFAULT_LINKS"
LINKS="$(snapshot "$H")"
[[ ! -e "$H/.config" ]] || die 'install.sh created a configuration directory'
ln -s "$KIT/install.sh" "$TMP/linked install.sh"
for shell in "${SHELLS[@]}"; do
    for label in "$shell" "$shell-linked"; do
        script="$KIT/install.sh"
        [[ "$label" == "$shell" ]] || script="$TMP/linked install.sh"
        run "reinstall-${label//\//}" "$shell" "$script"
        expect "reinstall-${label//\//}" 0 "$DEFAULT_LINKS"
        [[ "$(snapshot "$H")" == "$LINKS" ]] || die "reinstall under $label changed the links"
    done
done
run reinstall-direct "$KIT/install.sh"
expect reinstall-direct 0 "$DEFAULT_LINKS"
run reinstall-relative sh -c 'cd "$1" && exec sh install.sh' sh "$KIT"
expect reinstall-relative 0 "$DEFAULT_LINKS"
# A relative link two levels down, so its text does not resolve from the cwd ($WORK).
mkdir -p "$TMP/rel/link"
ln -s "../../kit [with] spaces*/install.sh" "$TMP/rel/link/install.sh"
for shell in "${SHELLS[@]}"; do
    run "reinstall-rel-link-${shell//\//}" "$shell" "$TMP/rel/link/install.sh"
    expect "reinstall-rel-link-${shell//\//}" 0 "$DEFAULT_LINKS"
done
# Through a directory alias of the checkout, as /var is /private/var on macOS:
# the links name the physical path, so every spelling of the checkout owns them.
ln -s "$KIT" "$TMP/kit alias"
for shell in "${SHELLS[@]}"; do
    run "reinstall-alias-${shell//\//}" "$shell" "$TMP/kit alias/install.sh"
    expect "reinstall-alias-${shell//\//}" 0 "$DEFAULT_LINKS"
done
# A chain of links: 40 are followed, 41 are refused. The kernel opens at most
# 40 (32 on macOS), so sh runs the script text with $0 set to the chain.
mkdir "$TMP/chain"
ln -s "$KIT/install.sh" "$TMP/chain/0"
for ((hop = 1; hop <= 40; hop++)); do
    ln -s "$((hop - 1))" "$TMP/chain/$hop"
done
run reinstall-40-links /bin/sh -c "$(cat "$KIT/install.sh")" "$TMP/chain/39"
expect reinstall-40-links 0 "$DEFAULT_LINKS"
run reinstall-41-links /bin/sh -c "$(cat "$KIT/install.sh")" "$TMP/chain/40"
expect reinstall-41-links 1 '' 'install.sh: symlink loop (more than 40 links).'
[[ "$(snapshot "$H")" == "$LINKS" ]] || die 'a reinstall changed the links'

printf '==> codex-as and claude-as through the installed links, default configuration\n'
for tool in codex-as claude-as; do
    run "help-$tool" "$tool" --help
    [[ "$rc" == 0 && ! -s "$OUT/help-$tool.err" ]] || die "$tool --help failed"
    [[ "$(head -n 1 "$OUT/help-$tool.out")" == "Usage: $tool NAME ["* ]] || die "$tool --help usage line"
    [[ "$(tail -n 1 "$OUT/help-$tool.out")" == "Details and manual repair: $KIT/docs/account-launchers.md" ]] ||
        die "$tool --help does not name the kit's documentation"
done
run show-codex codex-as --show-config
expect show-codex 0 "config = $H/.config/coding-agent-kit/config.toml (not found; defaults)
codex = codex ($FAKES/codex)
codex_home = $H/.codex
secrets_dir = $H/.secrets"
run show-claude claude-as --show-config
expect show-claude 0 "config = $H/.config/coding-agent-kit/config.toml (not found; defaults)
claude = claude ($FAKES/claude)
secrets_dir = $H/.secrets"
run list-codex codex-as --list-accounts
expect list-codex 0 A
run list-claude claude-as --list-accounts
expect list-claude 0 work
completions A work
: >"$RECORD"
run launch-codex codex-as A exec hello
expect launch-codex 0 ''
SQLITE="$(sed -n 's/^arg=sqlite_home=//p' "$RECORD")"
same "$RECORD" "run=codex
arg=-c
arg=cli_auth_credentials_store=\"file\"
arg=-c
arg=sqlite_home=$SQLITE
arg=exec
arg=hello
CODEX_HOME=$H/.codex/accounts/A
token=$(printf '' | cksum)"
[[ "$(readlink "$H/.codex/accounts/A/auth.json")" == "$H/.secrets/codex-A.json" ]] || die 'codex-as account link'
: >"$RECORD"
run launch-claude claude-as work -p hello
expect launch-claude 0 ''
same "$RECORD" "run=claude
arg=-p
arg=hello
CODEX_HOME=
token=$(checksum work)"

printf '==> configuration file overrides: codex, claude, codex_home and secrets_dir\n'
mkdir -m 700 "$H/vault" "$H/alt codex" "$H/.config" "$H/.config/coding-agent-kit" "$TMP/xdg" "$TMP/xdg/coding-agent-kit"
seed "$H/vault" Z vault
(
    umask 077
    printf '%s\n' "codex = \"$ALT/codex-alt\"" "claude = \"$ALT/claude-alt\"" 'codex_home = "~/alt codex"' \
        'secrets_dir = "~/vault"' >"$H/.config/coding-agent-kit/config.toml"
    printf '%s\n' 'secrets_dir = "~/vault"' >"$TMP/xdg/coding-agent-kit/config.toml"
    printf '%s\n' 'codex_home = "~/alt codex"' >"$TMP/explicit config.toml"
)
run show-codex-file codex-as --show-config
expect show-codex-file 0 "config = $H/.config/coding-agent-kit/config.toml (found)
codex = $ALT/codex-alt ($ALT/codex-alt)
codex_home = $H/alt codex
secrets_dir = $H/vault"
run show-claude-file claude-as --show-config
expect show-claude-file 0 "config = $H/.config/coding-agent-kit/config.toml (found)
claude = $ALT/claude-alt ($ALT/claude-alt)
secrets_dir = $H/vault"
run list-codex-file codex-as --list-accounts
expect list-codex-file 0 Z
run list-claude-file claude-as --list-accounts
expect list-claude-file 0 vault
completions Z vault
: >"$RECORD"
run launch-codex-file codex-as Z exec hello
expect launch-codex-file 0 ''
grep -Fxq 'run=codex-alt' "$RECORD" || die 'codex-as did not run the configured codex'
grep -Fxq "CODEX_HOME=$H/alt codex/accounts/Z" "$RECORD" || die 'codex-as did not use the configured codex_home'
[[ "$(readlink "$H/alt codex/accounts/Z/auth.json")" == "$H/vault/codex-Z.json" ]] || die 'configured account link'
: >"$RECORD"
run launch-claude-file claude-as vault -p hello
expect launch-claude-file 0 ''
same "$RECORD" "run=claude-alt
arg=-p
arg=hello
CODEX_HOME=
token=$(checksum vault)"
# XDG_CONFIG_HOME replaces ~/.config, and CODING_AGENT_KIT_CONFIG both.
run show-codex-xdg XDG_CONFIG_HOME="$TMP/xdg" codex-as --show-config
expect show-codex-xdg 0 "config = $TMP/xdg/coding-agent-kit/config.toml (found)
codex = codex ($FAKES/codex)
codex_home = $H/.codex
secrets_dir = $H/vault"
run list-claude-xdg XDG_CONFIG_HOME="$TMP/xdg" claude-as --list-accounts
expect list-claude-xdg 0 vault
run show-codex-explicit XDG_CONFIG_HOME="$TMP/xdg" CODING_AGENT_KIT_CONFIG="$TMP/explicit config.toml" \
    codex-as --show-config
expect show-codex-explicit 0 "config = $TMP/explicit config.toml (from CODING_AGENT_KIT_CONFIG)
codex = codex ($FAKES/codex)
codex_home = $H/alt codex
secrets_dir = $H/.secrets"
: >"$RECORD"
run launch-codex-explicit CODING_AGENT_KIT_CONFIG="$TMP/explicit config.toml" codex-as A exec hello
expect launch-codex-explicit 0 ''
grep -Fxq 'run=codex' "$RECORD" || die 'codex-as did not run codex from PATH'
grep -Fxq "CODEX_HOME=$H/alt codex/accounts/A" "$RECORD" || die 'CODING_AGENT_KIT_CONFIG was not used'
[[ "$(readlink "$H/alt codex/accounts/A/auth.json")" == "$H/.secrets/codex-A.json" ]] || die 'explicit account link'

printf '==> --uninstall removes only links into this checkout\n'
rm "$H/.local/share/bash-completion/completions/claude-as"
ln -s "$OTHER/completions/bash/claude-as" "$H/.local/share/bash-completion/completions/claude-as"
FOREIGN="kept $H/.local/share/bash-completion/completions/claude-as: a symlink to $OTHER/completions/bash/claude-as, not an absolute link into $KIT"
for shell in "${SHELLS[@]}"; do
    run "install-foreign-${shell//\//}" "$shell" "$KIT/install.sh"
    [[ "$rc" == 1 ]] || die "install.sh under $shell replaced a foreign link"
    run "uninstall-${shell//\//}" "$shell" "$KIT/install.sh" --uninstall
    if [[ "$shell" == "${SHELLS[0]}" ]]; then
        expect "uninstall-${shell//\//}" 0 "removed $H/.local/bin/codex-as
removed $H/.local/share/bash-completion/completions/codex-as
removed $H/.local/share/zsh/site-functions/_codex-as
removed $H/.local/bin/claude-as
$FOREIGN
removed $H/.local/share/zsh/site-functions/_claude-as"
    else
        expect "uninstall-${shell//\//}" 0 "$FOREIGN"
    fi
done
[[ "$(find "$H/.local" -type l | wc -l | tr -d ' ')" == 1 ]] || die 'uninstall left a kit link or removed the foreign one'
[[ "$(readlink "$H/.local/share/bash-completion/completions/claude-as")" == "$OTHER/completions/bash/claude-as" ]] ||
    die 'uninstall changed the foreign link'

printf '==> refusals: a file, a directory, a link into another checkout, a Stow link into this one\n'
home refusal
mkdir -p "$H/.local/bin" "$H/.local/share/bash-completion/completions/codex-as" \
    "$H/.local/share/zsh/site-functions" "$H/dotfiles/common/codex/.local/bin"
printf 'not a link\n' >"$H/.local/bin/claude-as"
ln -s "$OTHER/completions/zsh/_claude-as" "$H/.local/share/zsh/site-functions/_claude-as"
# A dotfiles host: HOME link (Stow, relative) -> package link -> this checkout.
ln -s "$KIT/bin/codex-as" "$H/dotfiles/common/codex/.local/bin/codex-as"
ln -s ../../dotfiles/common/codex/.local/bin/codex-as "$H/.local/bin/codex-as"
refuse refusal "install.sh: refusing $H/.local/bin/codex-as: a symlink to ../../dotfiles/common/codex/.local/bin/codex-as, not an absolute link into $KIT
install.sh: refusing $H/.local/share/bash-completion/completions/codex-as: a directory, not an absolute link into $KIT
install.sh: refusing $H/.local/bin/claude-as: a file, not an absolute link into $KIT
install.sh: refusing $H/.local/share/zsh/site-functions/_claude-as: a symlink to $OTHER/completions/zsh/_claude-as, not an absolute link into $KIT" \
    "kept $H/.local/bin/codex-as: a symlink to ../../dotfiles/common/codex/.local/bin/codex-as, not an absolute link into $KIT
kept $H/.local/share/bash-completion/completions/codex-as: a directory, not an absolute link into $KIT
kept $H/.local/bin/claude-as: a file, not an absolute link into $KIT
kept $H/.local/share/zsh/site-functions/_claude-as: a symlink to $OTHER/completions/zsh/_claude-as, not an absolute link into $KIT"

printf '==> refusals: dangling, prefix-sibling, escaping, relative and checkout-alias links\n'
home dangling
mkdir -p "$H/.local/bin" "$H/.local/share/bash-completion/completions" "$H/.local/share/zsh/site-functions" "$KIT-old"
cp -R "$KIT/bin" "$KIT-old/"
# A deleted checkout, a sibling whose path extends this one's, an absolute text
# that leaves this checkout through .., a hand-made relative link into it, and
# a hand-made link through another spelling of its path (the alias above).
ln -s "$TMP/gone/bin/codex-as" "$H/.local/bin/codex-as"
ln -s "$KIT-old/bin/claude-as" "$H/.local/bin/claude-as"
ln -s "$KIT/../other checkout/completions/bash/codex-as" "$H/.local/share/bash-completion/completions/codex-as"
ln -s "../../../../../kit [with] spaces*/completions/zsh/_codex-as" "$H/.local/share/zsh/site-functions/_codex-as"
ln -s "$TMP/kit alias/completions/bash/claude-as" "$H/.local/share/bash-completion/completions/claude-as"
[[ ! -e "$H/.local/bin/codex-as" && -e "$H/.local/bin/claude-as" && -e "$H/.local/share/zsh/site-functions/_codex-as" &&
    -e "$H/.local/share/bash-completion/completions/codex-as" && -e "$H/.local/share/bash-completion/completions/claude-as" ]] ||
    die 'the dangling-link fixture'
refuse dangling "install.sh: refusing $H/.local/bin/codex-as: a symlink to $TMP/gone/bin/codex-as, not an absolute link into $KIT
install.sh: refusing $H/.local/share/bash-completion/completions/codex-as: a symlink to $KIT/../other checkout/completions/bash/codex-as, not an absolute link into $KIT
install.sh: refusing $H/.local/share/zsh/site-functions/_codex-as: a symlink to ../../../../../kit [with] spaces*/completions/zsh/_codex-as, not an absolute link into $KIT
install.sh: refusing $H/.local/bin/claude-as: a symlink to $KIT-old/bin/claude-as, not an absolute link into $KIT
install.sh: refusing $H/.local/share/bash-completion/completions/claude-as: a symlink to $TMP/kit alias/completions/bash/claude-as, not an absolute link into $KIT" \
    "kept $H/.local/bin/codex-as: a symlink to $TMP/gone/bin/codex-as, not an absolute link into $KIT
kept $H/.local/share/bash-completion/completions/codex-as: a symlink to $KIT/../other checkout/completions/bash/codex-as, not an absolute link into $KIT
kept $H/.local/share/zsh/site-functions/_codex-as: a symlink to ../../../../../kit [with] spaces*/completions/zsh/_codex-as, not an absolute link into $KIT
kept $H/.local/bin/claude-as: a symlink to $KIT-old/bin/claude-as, not an absolute link into $KIT
kept $H/.local/share/bash-completion/completions/claude-as: a symlink to $TMP/kit alias/completions/bash/claude-as, not an absolute link into $KIT"

printf '==> refusals: relative directory links on the way, below HOME and below a --prefix outside it\n'
# A home name with pattern characters, so an unquoted HOME in a pattern shows.
home 'relative [x]*'
RELATIVE="$TMP/relative prefix"
# Made by hand, in the form Stow folding writes: ~/.local/share and the
# prefix's bin are relative links to directories beside them. A link of this
# checkout already placed through one is refused too.
mkdir -p "$H/.local" "$H/shared" "$RELATIVE/kit" "$RELATIVE/store/bin"
ln -s ../shared "$H/.local/share"
ln -s ../store/bin "$RELATIVE/kit/bin"
ln -s "$KIT/bin/codex-as" "$RELATIVE/store/bin/codex-as"
FORM='the form Stow folding writes'
BEFORE="$(snapshot "$H")$(snapshot "$RELATIVE")"
for shell in "${SHELLS[@]}"; do
    run "relative-${shell//\//}" "$shell" "$KIT/install.sh" --prefix "$RELATIVE/kit"
    expect "relative-${shell//\//}" 1 '' "install.sh: refusing $RELATIVE/kit/bin/codex-as: $RELATIVE/kit/bin is a relative link to ../store/bin, $FORM
install.sh: refusing $H/.local/share/bash-completion/completions/codex-as: $H/.local/share is a relative link to ../shared, $FORM
install.sh: refusing $RELATIVE/kit/bin/claude-as: $RELATIVE/kit/bin is a relative link to ../store/bin, $FORM
install.sh: refusing $H/.local/share/bash-completion/completions/claude-as: $H/.local/share is a relative link to ../shared, $FORM
install.sh: nothing changed; move those paths aside or choose another --prefix."
    [[ "$(snapshot "$H")$(snapshot "$RELATIVE")" == "$BEFORE" ]] || die "relative: install.sh under $shell placed a link"
    # HOME spelled with a trailing slash bounds the walk as without one; the
    # prefix's parent lies outside HOME.
    run "relative-slash-${shell//\//}" HOME="$H/" XDG_DATA_HOME="$H/.local/share" "$shell" "$KIT/install.sh" --prefix "$TMP/deep/p"
    expect "relative-slash-${shell//\//}" 1 '' "install.sh: refusing $H/.local/share/bash-completion/completions/codex-as: $H/.local/share is a relative link to ../shared, $FORM
install.sh: refusing $H/.local/share/bash-completion/completions/claude-as: $H/.local/share is a relative link to ../shared, $FORM
install.sh: nothing changed; move those paths aside or choose another --prefix."
    [[ "$(snapshot "$H")$(snapshot "$RELATIVE")" == "$BEFORE" && ! -e "$TMP/deep" ]] ||
        die "relative-slash: install.sh under $shell placed a link"
done

printf '==> absolute directory links, HOME a relative link, and relative ones above HOME, are accepted\n'
# HOME itself a relative link, as /home/u -> ../export/u: Stow never folds HOME
# or a directory above it, so neither counts, also with HOME as the prefix.
# Its name has pattern characters, so an unquoted directory in a pattern shows.
home real
ln -s 'real home' "$TMP/rel-link [x]* home"
H="$TMP/rel-link [x]* home"
for shell in "${SHELLS[@]}"; do
    for rel_prefix in '' "$H"; do
        label="rel-link-${shell//\//}${rel_prefix:+-prefix}"
        links="$(installed "${rel_prefix:-$H/.local}" "$H/.local/share/bash-completion/completions" ${rel_prefix:+path})"
        run "$label" "$shell" "$KIT/install.sh" ${rel_prefix:+--prefix "$rel_prefix"}
        expect "$label" 0 "$links"
        run "$label-uninstall" "$shell" "$KIT/install.sh" --uninstall ${rel_prefix:+--prefix "$rel_prefix"}
        expect "$label-uninstall" 0 "$(sed -n 's/^\(.*\) -> .*$/removed \1/p' <<<"$links")"
        [[ -z "$(find "$TMP/real home" -type l)" ]] || die "$label: --uninstall left a link"
    done
done
# A home below a relative system link, as /var is private/var on macOS and
# /home is var/home on Fedora Atomic, whose ~/.local is an absolute link.
mkdir -p "$TMP/system/private/var" "$TMP/local store"
ln -s private/var "$TMP/system/var"
home system/var/alias
ln -s "$TMP/local store" "$H/.local"
ALIAS_LINKS="$(installed "$H/.local" "$H/.local/share/bash-completion/completions")"
for shell in "${SHELLS[@]}"; do
    run "alias-${shell//\//}" "$shell" "$KIT/install.sh"
    expect "alias-${shell//\//}" 0 "$ALIAS_LINKS"
    [[ "$(readlink "$TMP/local store/bin/codex-as")" == "$KIT/bin/codex-as" ]] || die "alias: under $shell no link through ~/.local"
    run "alias-uninstall-${shell//\//}" "$shell" "$KIT/install.sh" --uninstall
    expect "alias-uninstall-${shell//\//}" 0 "$(sed -n 's/^\(.*\) -> .*$/removed \1/p' <<<"$ALIAS_LINKS")"
    # A shared --prefix whose parent lies above that link, and the link itself
    # as the prefix: an ancestor of HOME never counts.
    for tools in "$TMP/system/tools" "$TMP/system/var"; do
        label="alias-${tools##*/}-${shell//\//}"
        links="$(installed "$tools" "$H/.local/share/bash-completion/completions" path)"
        run "$label" "$shell" "$KIT/install.sh" --prefix "$tools"
        expect "$label" 0 "$links"
        run "$label-uninstall" "$shell" "$KIT/install.sh" --uninstall --prefix "$tools"
        expect "$label-uninstall" 0 "$(sed -n 's/^\(.*\) -> .*$/removed \1/p' <<<"$links")"
    done
done
# HOME=/, as a service account has it: the prefix stands in for HOME, so the
# relative link above the prefix's parent is accepted, and a prefix that is
# itself a relative link is refused as for any other HOME.
ROOT_HOME_LINKS="$(installed "$TMP/system/var/p" "$TMP/root completions/completions" path)"
ROOT_HOME_VAR="$TMP/system/var is a relative link to private/var, $FORM"
for shell in "${SHELLS[@]}"; do
    label="root-home-${shell//\//}"
    run "$label" HOME=/ BASH_COMPLETION_USER_DIR="$TMP/root completions" "$shell" "$KIT/install.sh" --prefix "$TMP/system/var/p"
    expect "$label" 0 "$ROOT_HOME_LINKS"
    run "$label-uninstall" HOME=/ BASH_COMPLETION_USER_DIR="$TMP/root completions" \
        "$shell" "$KIT/install.sh" --uninstall --prefix "$TMP/system/var/p"
    expect "$label-uninstall" 0 "$(sed -n 's/^\(.*\) -> .*$/removed \1/p' <<<"$ROOT_HOME_LINKS")"
    BEFORE="$(snapshot "$TMP/system/private")$(snapshot "$TMP/root completions")"
    run "$label-relative" HOME=/ BASH_COMPLETION_USER_DIR="$TMP/root completions" "$shell" "$KIT/install.sh" --prefix "$TMP/system/var"
    expect "$label-relative" 1 '' "install.sh: refusing $TMP/system/var/bin/codex-as: $ROOT_HOME_VAR
install.sh: refusing $TMP/system/var/share/zsh/site-functions/_codex-as: $ROOT_HOME_VAR
install.sh: refusing $TMP/system/var/bin/claude-as: $ROOT_HOME_VAR
install.sh: refusing $TMP/system/var/share/zsh/site-functions/_claude-as: $ROOT_HOME_VAR
install.sh: nothing changed; move those paths aside or choose another --prefix."
    [[ "$(snapshot "$TMP/system/private")$(snapshot "$TMP/root completions")" == "$BEFORE" ]] ||
        die "$label-relative: install.sh under $shell placed a link"
done

if command -v stow >/dev/null 2>&1; then
    # CI's Linux job fails unless this heading is in the output.
    printf '==> refusals: destinations in directories GNU Stow folded into another repository\n'
    home folded
    # GNU Stow folds the missing ~/.local/bin into the package directory of
    # another repository, whose tracked relative link points into this checkout.
    mkdir -p "$H/.local" "$H/dots/kitlinks/.local/bin"
    ln -s "../../../../../kit [with] spaces*/bin/codex-as" "$H/dots/kitlinks/.local/bin/codex-as"
    (cd -- "$WORK" && env -i HOME="$H" PATH="$PATH" stow -d "$H/dots" -t "$H" kitlinks) || die 'stow failed'
    [[ "$(readlink "$H/.local/bin")" == ../dots/kitlinks/.local/bin && -e "$H/.local/bin/codex-as" ]] ||
        die 'stow did not fold ~/.local/bin into the package'
    refuse folded "install.sh: refusing $H/.local/bin/codex-as: a symlink to ../../../../../kit [with] spaces*/bin/codex-as, not an absolute link into $KIT
install.sh: refusing $H/.local/bin/claude-as: $H/.local/bin is a relative link to ../dots/kitlinks/.local/bin, $FORM" \
        "kept $H/.local/bin/codex-as: a symlink to ../../../../../kit [with] spaces*/bin/codex-as, not an absolute link into $KIT"
    home folded-clean
    # A package with no kit link: Stow folds ~/.local/bin and ~/.local/share
    # into a Git repository, where a new link would be an untracked file.
    mkdir -p "$H/.local" "$H/dots/tools/.local/bin" "$H/dots/tools/.local/share/zsh/site-functions"
    printf '#!/bin/sh\n' >"$H/dots/tools/.local/bin/mytool"
    printf '#compdef mytool\n' >"$H/dots/tools/.local/share/zsh/site-functions/_mytool"
    dots() { (cd -- "$WORK" && env -i HOME="$H" PATH="$PATH" GIT_CONFIG_NOSYSTEM=1 "$@"); }
    dots git -c init.defaultBranch=main init -q "$H/dots" || die 'git init failed'
    dots git -C "$H/dots" add -A || die 'git add failed'
    dots git -C "$H/dots" -c user.name=e2e -c user.email=e2e@example.invalid commit -qm tools || die 'git commit failed'
    dots stow -d "$H/dots" -t "$H" tools || die 'stow failed'
    [[ "$(readlink "$H/.local/bin")" == ../dots/tools/.local/bin && "$(readlink "$H/.local/share")" == ../dots/tools/.local/share ]] ||
        die 'stow did not fold ~/.local/bin and ~/.local/share into the package'
    FOLD_BIN="$H/.local/bin is a relative link to ../dots/tools/.local/bin, $FORM"
    FOLD_SHARE="$H/.local/share is a relative link to ../dots/tools/.local/share, $FORM"
    refuse folded-clean "install.sh: refusing $H/.local/bin/codex-as: $FOLD_BIN
install.sh: refusing $H/.local/share/bash-completion/completions/codex-as: $FOLD_SHARE
install.sh: refusing $H/.local/share/zsh/site-functions/_codex-as: $FOLD_SHARE
install.sh: refusing $H/.local/bin/claude-as: $FOLD_BIN
install.sh: refusing $H/.local/share/bash-completion/completions/claude-as: $FOLD_SHARE
install.sh: refusing $H/.local/share/zsh/site-functions/_claude-as: $FOLD_SHARE" ''
    [[ -z "$(dots git -C "$H/dots" status --porcelain)" ]] || die 'folded-clean: install.sh wrote into the other repository'
else
    printf 'standalone-e2e: GNU Stow not installed; the folded Stow cases are skipped.\n'
fi

printf '==> links of an older layout of this checkout are replaced and removed\n'
home stale
mkdir -p "$H/.local/bin" "$H/.local/share/zsh/site-functions"
STALE_LINKS="$(installed "$H/.local" "$H/.local/share/bash-completion/completions")"
for shell in "${SHELLS[@]}"; do
    # A directory and a file that this checkout no longer has.
    ln -s "$KIT/share/zsh/_codex-as" "$H/.local/share/zsh/site-functions/_codex-as"
    ln -s "$KIT/bin/old-claude-as" "$H/.local/bin/claude-as"
    run "stale-${shell//\//}" "$shell" "$KIT/install.sh"
    expect "stale-${shell//\//}" 0 "$STALE_LINKS"
    [[ "$(readlink "$H/.local/share/zsh/site-functions/_codex-as")" == "$KIT/completions/zsh/_codex-as" ]] ||
        die "stale: under $shell the zsh completion link was not replaced"
    [[ "$(readlink "$H/.local/bin/claude-as")" == "$KIT/bin/claude-as" ]] || die "stale: under $shell the claude-as link was not replaced"
    run "stale-uninstall-${shell//\//}" "$shell" "$KIT/install.sh" --uninstall
    expect "stale-uninstall-${shell//\//}" 0 "$(sed -n 's/^\(.*\) -> .*$/removed \1/p' <<<"$STALE_LINKS")"
    ln -s "$KIT/share/zsh/_codex-as" "$H/.local/share/zsh/site-functions/_codex-as"
    ln -s "$KIT/bin/old-claude-as" "$H/.local/bin/claude-as"
    run "stale-uninstall-old-${shell//\//}" "$shell" "$KIT/install.sh" --uninstall
    expect "stale-uninstall-old-${shell//\//}" 0 "removed $H/.local/share/zsh/site-functions/_codex-as
removed $H/.local/bin/claude-as"
    [[ -z "$(find "$H" -type l)" ]] || die "stale: --uninstall under $shell left a link"
done

printf '==> refusals: a destination directory that cannot be created or written\n'
home blocked
# A file where the zsh completion directory belongs, writable and executable
# like a directory.
mkdir -p "$H/.local/share/zsh"
printf 'not a directory\n' >"$H/.local/share/zsh/site-functions"
chmod 755 "$H/.local/share/zsh/site-functions"
refuse blocked "install.sh: refusing $H/.local/share/zsh/site-functions/_codex-as: its nearest existing parent $H/.local/share/zsh/site-functions is not a writable directory
install.sh: refusing $H/.local/share/zsh/site-functions/_claude-as: its nearest existing parent $H/.local/share/zsh/site-functions is not a writable directory" ''
home blocked-link
# A dangling link where the zsh completion directory belongs, as a removed
# Stow package can leave one.
mkdir -p "$H/.local/share/zsh"
ln -s "$TMP/gone" "$H/.local/share/zsh/site-functions"
refuse blocked-link "install.sh: refusing $H/.local/share/zsh/site-functions/_codex-as: its nearest existing parent $H/.local/share/zsh/site-functions is not a writable directory
install.sh: refusing $H/.local/share/zsh/site-functions/_claude-as: its nearest existing parent $H/.local/share/zsh/site-functions is not a writable directory" ''
if [[ "$EUID" == 0 ]]; then
    printf 'standalone-e2e: running as root; the read-only directory cases are skipped.\n'
else
    home readonly-own
    # This checkout's links, of an older layout, in a read-only directory: a
    # reinstall and --uninstall refuse them before they replace or remove any
    # link.
    mkdir -p "$H/.local/bin" "$H/.local/share/zsh/site-functions"
    for tool in codex-as claude-as; do
        ln -s "$KIT/bin/old-$tool" "$H/.local/bin/$tool"
        ln -s "$KIT/share/zsh/_$tool" "$H/.local/share/zsh/site-functions/_$tool"
    done
    chmod 555 "$H/.local/share/zsh/site-functions"
    BEFORE="$(snapshot "$H")"
    for shell in "${SHELLS[@]}"; do
        run "readonly-own-${shell//\//}" "$shell" "$KIT/install.sh"
        expect "readonly-own-${shell//\//}" 1 '' "install.sh: refusing $H/.local/share/zsh/site-functions/_codex-as: its nearest existing parent $H/.local/share/zsh/site-functions is not a writable directory
install.sh: refusing $H/.local/share/zsh/site-functions/_claude-as: its nearest existing parent $H/.local/share/zsh/site-functions is not a writable directory
install.sh: nothing changed; move those paths aside or choose another --prefix."
        run "readonly-own-uninstall-${shell//\//}" "$shell" "$KIT/install.sh" --uninstall
        expect "readonly-own-uninstall-${shell//\//}" 1 '' "install.sh: refusing to remove $H/.local/share/zsh/site-functions/_codex-as: $H/.local/share/zsh/site-functions is not a writable directory
install.sh: refusing to remove $H/.local/share/zsh/site-functions/_claude-as: $H/.local/share/zsh/site-functions is not a writable directory
install.sh: nothing changed; make those directories writable and searchable first."
        [[ "$(snapshot "$H")" == "$BEFORE" ]] || die "readonly-own: install.sh under $shell changed a link"
    done
    chmod 755 "$H/.local/share/zsh/site-functions"
    home readonly
    # A read-only ~/.local/share, as a stray sudo install can leave it.
    mkdir -p "$H/.local/share"
    chmod 555 "$H/.local/share"
    READONLY="install.sh: refusing $H/.local/share/bash-completion/completions/codex-as: its nearest existing parent $H/.local/share is not a writable directory
install.sh: refusing $H/.local/share/zsh/site-functions/_codex-as: its nearest existing parent $H/.local/share is not a writable directory
install.sh: refusing $H/.local/share/bash-completion/completions/claude-as: its nearest existing parent $H/.local/share is not a writable directory
install.sh: refusing $H/.local/share/zsh/site-functions/_claude-as: its nearest existing parent $H/.local/share is not a writable directory"
    refuse readonly "$READONLY" ''
    # Writable but not searchable: nothing can be created below it either, and
    # --uninstall cannot tell what is there. The snapshot cannot read it, so
    # only the absence of ~/.local/bin is checked.
    chmod 666 "$H/.local/share"
    for shell in "${SHELLS[@]}"; do
        run "unsearchable-${shell//\//}" "$shell" "$KIT/install.sh"
        expect "unsearchable-${shell//\//}" 1 '' "$READONLY
install.sh: nothing changed; move those paths aside or choose another --prefix."
        [[ ! -e "$H/.local/bin" ]] || die "unsearchable: install.sh under $shell placed a link"
        run "unsearchable-uninstall-${shell//\//}" "$shell" "$KIT/install.sh" --uninstall
        expect "unsearchable-uninstall-${shell//\//}" 1 '' "install.sh: refusing to look for $H/.local/share/bash-completion/completions/codex-as: $H/.local/share is not a searchable directory
install.sh: refusing to look for $H/.local/share/zsh/site-functions/_codex-as: $H/.local/share is not a searchable directory
install.sh: refusing to look for $H/.local/share/bash-completion/completions/claude-as: $H/.local/share is not a searchable directory
install.sh: refusing to look for $H/.local/share/zsh/site-functions/_claude-as: $H/.local/share is not a searchable directory
install.sh: nothing changed; make those directories writable and searchable first."
    done
    chmod 755 "$H/.local/share"
fi

printf '==> refusals: an incomplete checkout and a piped install.sh, before any link\n'
home incomplete
INCOMPLETE="$TMP/incomplete kit"
mkdir "$INCOMPLETE"
cp -R "$KIT/completions" "$KIT/helpers" "$KIT/install.sh" "$INCOMPLETE/"
BEFORE="$(snapshot "$H")"
run incomplete /bin/sh "$INCOMPLETE/install.sh"
expect incomplete 1 '' "install.sh: $INCOMPLETE is not a complete coding-agent-kit checkout (missing bin/codex-as)."
# sh <install.sh has no path to the script, so the root is where sh lives.
run piped sh -c 'sh <"$1"' sh "$KIT/install.sh"
[[ "$rc" == 1 && ! -s "$OUT/piped.out" && "$(wc -l <"$OUT/piped.err")" -eq 1 ]] || die "piped: exit $rc, expected 1"
[[ "$(cat "$OUT/piped.err")" == 'install.sh: /'*' is not a complete coding-agent-kit checkout (missing bin/codex-as).' ]] ||
    die "piped: $(cat "$OUT/piped.err")"
[[ "$(snapshot "$H")" == "$BEFORE" ]] || die 'an incomplete checkout wrote to HOME'

printf '==> --prefix with a space, XDG_DATA_HOME and BASH_COMPLETION_USER_DIR\n'
home prefix
PREFIX="$TMP/pre fix"
for shell in "${SHELLS[@]}"; do
    # BASH_COMPLETION_USER_DIR wins over XDG_DATA_HOME.
    for user_dir in '' "$TMP/user completions"; do
        label="prefix-${shell//\//}${user_dir:+-user}"
        bash_dir="${user_dir:-$TMP/data home/bash-completion}/completions"
        run "$label" XDG_DATA_HOME="$TMP/data home" ${user_dir:+BASH_COMPLETION_USER_DIR="$user_dir"} \
            "$shell" "$KIT/install.sh" --prefix "$PREFIX"
        expect "$label" 0 "$(installed "$PREFIX" "$bash_dir" path)"
        run "$label-help" "$PREFIX/bin/codex-as" --help
        [[ "$(tail -n 1 "$OUT/$label-help.out")" == "Details and manual repair: $KIT/docs/account-launchers.md" ]] ||
            die "$label: codex-as --help through the prefix"
        run "$label-uninstall" XDG_DATA_HOME="$TMP/data home" ${user_dir:+BASH_COMPLETION_USER_DIR="$user_dir"} \
            "$shell" "$KIT/install.sh" --uninstall --prefix "$PREFIX/"
        expect "$label-uninstall" 0 "$(installed "$PREFIX" "$bash_dir" | sed -n 's/^\(.*\) -> .*$/removed \1/p')"
        [[ -z "$(find "$PREFIX" "$TMP/data home" "$TMP/user completions" -type l 2>/dev/null)" ]] ||
            die "$label: --uninstall left a link"
    done
done
[[ ! -e "$H/.local" && ! -e "$H/.config" ]] || die '--prefix wrote under HOME'

printf '==> usage errors\n'
home usage
run usage-help /bin/sh "$KIT/install.sh" --help
[[ "$rc" == 0 && ! -s "$OUT/usage-help.err" ]] || die 'install.sh --help'
[[ "$(head -n 1 "$OUT/usage-help.out")" == 'Usage: install.sh [--prefix DIR] [--uninstall]' ]] || die 'install.sh usage line'
for args in --bogus extra '--uninstall --bogus' --prefix '--prefix relative/dir'; do
    # shellcheck disable=SC2086 # each case is a word list
    run usage /bin/sh "$KIT/install.sh" $args
    [[ "$rc" == 2 && ! -s "$OUT/usage.out" && -s "$OUT/usage.err" ]] || die "install.sh $args: exit $rc, expected 2"
done
run usage-prefix /bin/sh "$KIT/install.sh" --prefix ''
expect usage-prefix 2 '' 'install.sh: --prefix needs an absolute directory.'
run usage-prefix-relative /bin/sh "$KIT/install.sh" --prefix relative/dir
expect usage-prefix-relative 2 '' 'install.sh: --prefix needs an absolute directory.'
run relative-xdg XDG_DATA_HOME=rel /bin/sh "$KIT/install.sh"
expect relative-xdg 1 '' 'install.sh: the bash completion directory rel/bash-completion/completions is not absolute.'
run relative-user-dir BASH_COMPLETION_USER_DIR=rel /bin/sh "$KIT/install.sh"
expect relative-user-dir 1 '' 'install.sh: the bash completion directory rel/completions is not absolute.'
run usage-no-home env -u HOME /bin/sh "$KIT/install.sh" --help
[[ "$rc" == 0 ]] || die 'install.sh --help needs HOME'
run no-home env -u HOME /bin/sh "$KIT/install.sh" --uninstall
expect no-home 1 '' 'install.sh: HOME must be set to an absolute path.'
[[ "$(snapshot "$H")" == "$(printf '.\n./.codex\n./.secrets\n./.secrets/claude-work\n./.secrets/codex-A.json')" ]] ||
    die 'a usage error wrote to HOME'

[[ "$(cd -- "$KIT" && find . -print | LC_ALL=C sort)" == "$KIT_TREE" ]] || die 'a run wrote into the checkout'
[[ -z "$(ls -A "$WORK")" ]] || die 'a run wrote into its working directory'
if grep -rl -e sk-ant-oat01 -e fabricated-access "$OUT"; then
    die 'an output above contains a fabricated token'
fi
printf 'standalone-e2e=PASS\n'
