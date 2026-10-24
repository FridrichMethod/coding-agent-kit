#!/bin/bash
# Managed installs from a local repository with release tags and no network:
# install.sh piped to /bin/sh, bash and dash as the bootstrap, channels,
# --update, the lock, a running launcher across a switch, refusals,
# --uninstall and --purge, in temporary homes with recording fakes.

set -euo pipefail

# Hermetic: a caller's kit configuration, colour choice and XDG directories
# never reach the code under test; each case sets what it needs.
unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR CODING_AGENT_KIT_USAGE_FILE CODING_AGENT_KIT_TOKEN_ID CODING_AGENT_KIT_CLAUDE_PID CODING_AGENT_KIT_REPO_URL XDG_CONFIG_HOME XDG_STATE_HOME NO_COLOR

TEST_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -P -- "$TEST_DIR/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/kit-managed-install.XXXXXX")"
STALL=''
trap '[ -z "$STALL" ] || kill "$STALL" 2>/dev/null; chmod -R u+rwx "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT
TMP="$(cd -P -- "$TMP" && pwd)"

SOURCE="$TMP/source"
REMOTE="$TMP/remote.git"
URL="file://$REMOTE"
FAKES="$TMP/fake bin"
TOOLS="$TMP/tools"
OUT="$TMP/out"
WORK="$TMP/work"
LOG="$TMP/release log"
RECORD="$TMP/record"
GATE="$TMP/gate"
mkdir "$SOURCE" "$FAKES" "$TOOLS" "$OUT" "$WORK"

die() {
    printf 'ERROR: managed-install: %s\n' "$1" >&2
    exit 1
}

# The commands under test see only these tools, the fakes and the install's bin.
SHELLS=(/bin/sh bash)
for tool in sh bash dash python3 git tar sort sed cat dirname readlink mkdir ln rm mv rmdir cksum env sleep node; do
    if command -v "$tool" >/dev/null 2>&1; then
        ln -s "$(command -v "$tool")" "$TOOLS/$tool"
    elif [[ "$tool" != dash ]]; then
        die "the suite needs $tool."
    fi
done
if [[ -e "$TOOLS/dash" ]]; then
    SHELLS+=(dash)
else
    printf 'managed-install: dash not installed; the bootstrap runs under /bin/sh and bash only.\n'
fi

# git for the fixtures, without the caller's configuration.
fixture_git() {
    env -i HOME="$TMP" PATH="$PATH" GIT_CONFIG_NOSYSTEM=1 git -c init.defaultBranch=main \
        -c user.name=fixture -c user.email=fixture@example.invalid "$@"
}

# release TAG [-a]: commit the kit under test as release TAG of the fixture
# repository and push it, with one line added to its install.sh that logs
# which release's code ran, with its arguments.
release() {
    local line
    rm -rf "${SOURCE:?}"/*
    cp -R "$ROOT/bin" "$ROOT/lib" "$ROOT/completions" "$ROOT/helpers" "$ROOT/docs" "$SOURCE/"
    while IFS= read -r line; do
        printf '%s\n' "$line"
        if [[ "$line" == 'set -eu' ]]; then
            # shellcheck disable=SC2016 # the release's code, not this suite's
            printf '[ -z "${KIT_FIXTURE_LOG-}" ] || printf '\''%%s\\n'\'' "%s${1+ $*}" >>"$KIT_FIXTURE_LOG"\n' "$1"
        fi
    done <"$ROOT/install.sh" >"$SOURCE/install.sh"
    chmod 755 "$SOURCE/install.sh"
    grep -Fq "KIT_FIXTURE_LOG" "$SOURCE/install.sh" || die 'the release fixture has no log line'
    fixture_git -C "$SOURCE" add -A
    fixture_git -C "$SOURCE" commit -q --allow-empty -m "release $1"
    if [[ "${2-}" == -a ]]; then
        fixture_git -C "$SOURCE" tag -a -m "$1" "$1"
    else
        fixture_git -C "$SOURCE" tag "$1"
    fi
    fixture_git -C "$SOURCE" push -q "$REMOTE" main "refs/tags/$1"
}
fixture_git init -q "$SOURCE"
fixture_git init -q --bare "$REMOTE"
release v0.5.0
release v0.6.0
release v0.6.1 -a
release v0.7.0 -a
fixture_git -C "$SOURCE" commit -q --allow-empty -m 'unreleased work on main'
fixture_git -C "$SOURCE" push -q "$REMOTE" main

# The codex fake prints its version, or records argv and waits for $FAKE_GATE.
cat >"$FAKES/codex" <<'EOF'
#!/bin/sh
if [ "$*" = --version ]; then
    echo 'codex-cli 0.163.0'
    exit 0
fi
for arg in "$@"; do printf 'arg=%s\n' "$arg"; done >>"$FAKE_RECORD"
if [ -n "${FAKE_GATE-}" ]; then
    : >"$FAKE_GATE.started"
    while [ ! -e "$FAKE_GATE" ]; do sleep 0.1; done
fi
EOF
chmod 755 "$FAKES/codex"

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

# Every path below a directory with its type, link text, size and modification
# time, directories included: an unchanged listing means nothing was written.
SNAPSHOT='import os, sys
top = sys.argv[1]
if os.path.lexists(top):
    for here, dirs, files in os.walk(top):
        for name in sorted([*dirs, *files]):
            path = os.path.join(here, name)
            info = os.lstat(path)
            if os.path.islink(path):
                print(os.path.relpath(path, top), "->", os.readlink(path))
            else:
                print(os.path.relpath(path, top), oct(info.st_mode), info.st_size, info.st_mtime_ns)
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(here, name))]
    print(".", os.lstat(top).st_mtime_ns)'
snapshot() {
    python3 -I -c "$SNAPSHOT" "$1"
}

# run LABEL [NAME=VALUE...] COMMAND [ARG...]: COMMAND in an empty directory with
# only HOME ($H), PATH, TMPDIR, the repository URL, the logs and the given
# variables; sets rc, and leaves the output in $OUT/LABEL.out and $OUT/LABEL.err.
run() {
    local label=$1
    shift
    set +e
    (cd -- "$WORK" && env -i HOME="$H" PATH="$FAKES:$H/.local/bin:$TOOLS" TMPDIR="$TMP" GIT_CONFIG_NOSYSTEM=1 \
        CODING_AGENT_KIT_REPO_URL="$URL" KIT_FIXTURE_LOG="$LOG" FAKE_RECORD="$RECORD" \
        "$TOOLS/python3" -I -c "$DETACH" "$TOOLS/env" "$@" >"$OUT/$label.out" 2>"$OUT/$label.err" </dev/null)
    rc=$?
    set -e
}
# boot LABEL SHELL [NAME=VALUE...] [-- ARG...]: install.sh of this checkout
# piped to SHELL -s, as the documented one-liner pipes it.
boot() {
    local label=$1 shell=$2
    shift 2
    local vars=()
    while [[ "$#" -gt 0 && "$1" != -- ]]; do
        vars+=("$1")
        shift
    done
    [[ "$#" == 0 ]] || shift
    run "$label" ${vars[@]+"${vars[@]}"} "$TOOLS/sh" -c 'boot=$1 shell=$2; shift 2; cat "$boot" | "$shell" -s -- "$@"' \
        sh "$ROOT/install.sh" "$shell" "$@"
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

# logged TEXT: the release log holds exactly TEXT since the last call.
logged() {
    if [[ -z "$1" ]]; then
        [[ ! -s "$LOG" ]] || { cat "$LOG" >&2 && die 'a release install.sh ran'; }
    else
        printf '%s\n' "$1" | diff -u - "$LOG" >&2 || die 'unexpected release log'
    fi
    : >"$LOG"
}

# home NAME: a new HOME ($H) with a fabricated Codex account; M is its
# managed root and MP that root's physical path.
home() {
    H="$TMP/$1 [h]* home"
    mkdir -m 700 "$H" "$H/.codex" "$H/.secrets"
    (
        umask 077
        printf '{"auth_mode": "chatgpt", "last_refresh": "2026-10-01T00:00:00Z", "tokens": {%s, %s, %s, %s}}\n' '"id_token": "fake.eyJzdWIiOiJlMmUifQ.fake"' \
            '"access_token": "fabricated-access"' '"refresh_token": "fabricated-refresh"' '"account_id": "A"' \
            >"$H/.secrets/codex-A.json"
    )
    M="$H/.local/share/coding-agent-kit"
    MP="$M"
    : >"$LOG"
}

# installed RELEASE [PREFIX]: what the first activation of RELEASE prints for $H.
installed() {
    local prefix=${2:-$H/.local} tool helper
    printf 'coding-agent-kit: %s (channel %s) in %s\n' "$1" "$(sed 's/^v//; s/\.[0-9]*$//' <<<"$1")" "$MP"
    for tool in codex-as claude-as; do
        printf '%s -> %s\n' "$prefix/bin/$tool" "$MP/current/bin/$tool" \
            "$H/.local/share/bash-completion/completions/$tool" "$MP/current/completions/bash/$tool" \
            "$prefix/share/zsh/site-functions/_$tool" "$MP/current/completions/zsh/_$tool"
    done
    for helper in statusline notify check-git-hooks; do
        printf '%s -> %s\n' "$H/.claude/coding-agent-kit/$helper.cjs" "$MP/current/helpers/$helper.cjs"
    done
    printf '%s\n' 'zsh: unless fpath has it, add this before compinit (with oh-my-zsh, before sourcing it):' \
        "  fpath=(\"$prefix/share/zsh/site-functions\" \$fpath)" 'Claude Code helpers, as commands in settings.json:'
    # shellcheck disable=SC2016 # literal JavaScript
    printf '  %s node -e "require(require('\''os'\'').homedir() + '\''/.claude/coding-agent-kit/%s.cjs'\'').main()"\n' \
        'statusLine:            ' statusline 'Notification hook:     ' notify 'PreToolUse hook (Bash):' check-git-hooks
    [[ -z "${2-}" ]] || printf 'PATH: add %s\n' "$prefix/bin"
}

# releases: the release trees in the managed root and the release current names.
releases() {
    (cd -- "$M/releases" && printf '%s ' v*)
    printf -- '-> %s\n' "$(readlink "$M/current")"
}

# links PREFIX: every link where the kit installs for PREFIX, with its text.
links() {
    find "$1/bin" "$1/share/zsh" "$H/.local/share/bash-completion" "$H/.claude" \
        -type l -print 2>/dev/null | LC_ALL=C sort | while IFS= read -r path; do
        printf '%s -> %s\n' "$path" "$(readlink "$path")"
    done
}

already() {
    printf 'coding-agent-kit: %s is already current (channel %s).' "$1" "$2"
}

# helpers_work: the helpers answer through the home-relative command form.
helpers_work() {
    local helper
    for helper in statusline notify check-git-hooks; do
        run "$1-$helper" node -e "console.log(typeof require(require('os').homedir() + '/.claude/coding-agent-kit/$helper.cjs').main)"
        expect "$1-$helper" 0 function
    done
    run "$1-hook" sh -c 'printf "%s\n" "{\"tool_input\": {\"command\": \"git commit --no-verify\"}}" |
        node -e "require(require('\''os'\'').homedir() + '\''/.claude/coding-agent-kit/check-git-hooks.cjs'\'').main()"'
    expect "$1-hook" 2 '' 'Git hooks must run: remove --no-verify and fix the failing check.'
}

printf '==> the piped bootstrap under %s installs the newest patch of the channel\n' "${SHELLS[*]}"
for shell in "${SHELLS[@]}"; do
    label="boot-${shell//\//}"
    home "$label"
    boot "$label" "$shell" -- --channel 0.6
    expect "$label" 0 "$(installed v0.6.1)"
    logged "v0.6.1 --activate --channel 0.6 --prefix $H/.local/"
    [[ "$(releases)" == 'v0.6.1 -> releases/v0.6.1' ]] || die "$label: $(releases)"
    [[ "$(cat "$M/channel")" == 0.6 && "$(cat "$M/prefix")" == "$H/.local" ]] || die "$label: the remembered state"
    [[ -d "$M/repo.git" && "$(fixture_git -C "$M/repo.git" config --get remote.origin.url)" == "$URL" ]] ||
        die "$label: the managed clone"
    [[ ! -e "$H/.config" && ! -e "$H/.local/state" ]] || die "$label: wrote outside the install paths"
done
LINKS="$(links "$H/.local")"

printf '==> the launchers resolve to their physical release; the helpers answer from ~/.claude\n'
for tool in codex-as claude-as; do
    run "help-$tool" "$tool" --help
    [[ "$rc" == 0 && "$(tail -n 1 "$OUT/help-$tool.out")" == "Details and manual repair: $MP/releases/v0.6.1/docs/account-launchers.md" ]] ||
        die "$tool --help through the managed link: $(tail -n 1 "$OUT/help-$tool.out")"
done
run list-codex codex-as --list-accounts
expect list-codex 0 A
helpers_work helpers

printf '==> a second run, the bootstrap again and --update write nothing and say so in one line\n'
BEFORE="$(snapshot "$H")"
boot again /bin/sh -- --channel 0.6
expect again 0 "$(already v0.6.1 0.6)"
logged 'v0.6.1 --managed --channel 0.6'
run current-again sh "$M/current/install.sh"
expect current-again 0 "$(already v0.6.1 0.6)"
logged v0.6.1
run update-current "$M/current/install.sh" --update
expect update-current 0 "$(already v0.6.1 0.6)"
logged 'v0.6.1 --update'
# The URL the managed root was cloned from serves without the variable.
run update-stored env -u CODING_AGENT_KIT_REPO_URL sh "$M/current/install.sh" --update
expect update-stored 0 "$(already v0.6.1 0.6)"
logged 'v0.6.1 --update'
[[ "$(snapshot "$H")" == "$BEFORE" ]] || die 'a run with nothing to change wrote below HOME'

printf '==> --update fails closed when the repository cannot be reached\n'
run update-offline CODING_AGENT_KIT_REPO_URL="file://$TMP/missing.git" sh "$M/current/install.sh" --update
[[ "$rc" == 1 && ! -s "$OUT/update-offline.out" && "$(wc -l <"$OUT/update-offline.err")" -eq 1 ]] ||
    die "update-offline: exit $rc"
grep -Fq "install.sh: cannot list the releases at file://$TMP/missing.git: " "$OUT/update-offline.err" ||
    die "update-offline: $(cat "$OUT/update-offline.err")"
logged 'v0.6.1 --update'
[[ "$(snapshot "$H")" == "$BEFORE" ]] || die 'a failed update wrote below HOME'

printf '==> the lock: a held lock exits 75 and changes nothing\n'
exec 8<>"$M/.lock"
python3 -I -c 'import fcntl; fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)'
for args in --update '' --uninstall; do
    # shellcheck disable=SC2086 # each case is a word list
    run "locked${args:+-}${args#--}" sh "$M/current/install.sh" $args
    expect "locked${args:+-}${args#--}" 75 '' "install.sh: another install.sh holds $MP/.lock; nothing changed."
done
boot locked-boot /bin/sh -- --channel 0.7
expect locked-boot 75 '' "install.sh: another install.sh holds $MP/.lock; nothing changed."
exec 8<&-
logged "v0.6.1 --update
v0.6.1
v0.6.1 --uninstall
v0.6.1 --managed --channel 0.7"
[[ "$(snapshot "$H")" == "$BEFORE" ]] || die 'a locked run wrote below HOME'

printf '==> --update takes a newer patch only; a running launcher keeps its release\n'
release v0.6.2
: >"$RECORD"
(cd -- "$WORK" && env -i HOME="$H" PATH="$FAKES:$H/.local/bin:$TOOLS" TMPDIR="$TMP" FAKE_RECORD="$RECORD" \
    FAKE_GATE="$GATE" "$TOOLS/python3" -I -c "$DETACH" "$TOOLS/env" codex-as A exec hello \
    >"$OUT/session.out" 2>"$OUT/session.err" </dev/null) &
SESSION=$!
for _ in $(seq 100); do
    [[ ! -e "$GATE.started" ]] || break
    sleep 0.1
done
if [[ ! -e "$GATE.started" ]]; then
    wait "$SESSION" || :
    cat "$OUT/session.out" "$OUT/session.err" >&2
    die 'the running session did not start'
fi
run update-patch "$M/current/install.sh" --update
expect update-patch 0 'coding-agent-kit: switched from v0.6.1 to v0.6.2 (channel 0.6).'
logged "v0.6.1 --update
v0.6.2 --activate --channel 0.6 --prefix $H/.local/"
[[ "$(releases)" == 'v0.6.1 v0.6.2 -> releases/v0.6.2' ]] || die "update-patch: $(releases)"
[[ "$(links "$H/.local")" == "$LINKS" ]] || die 'update-patch changed a link'
: >"$GATE"
SESSION_RC=0
wait "$SESSION" || SESSION_RC=$?
[[ "$SESSION_RC" == 0 ]] || {
    cat "$OUT/session.out" "$OUT/session.err" >&2
    die "the session that started from v0.6.1 failed after the switch: exit $SESSION_RC"
}
grep -Fxq 'arg=exec' "$RECORD" && grep -Fxq 'arg=hello' "$RECORD" || die 'the running session lost its arguments'
rm -f "$GATE" "$GATE.started"
run help-patch codex-as --help
[[ "$(tail -n 1 "$OUT/help-patch.out")" == "Details and manual repair: $MP/releases/v0.6.2/docs/account-launchers.md" ]] ||
    die 'a new launch does not run the new release'
run update-patch-again "$M/current/install.sh" --update
expect update-patch-again 0 "$(already v0.6.2 0.6)"
logged 'v0.6.2 --update'
# A third patch prunes the release before the previous one.
release v0.6.3
run update-prune "$M/current/install.sh" --update
expect update-prune 0 'coding-agent-kit: switched from v0.6.2 to v0.6.3 (channel 0.6).'
logged "v0.6.2 --update
v0.6.3 --activate --channel 0.6 --prefix $H/.local/"
[[ "$(releases)" == 'v0.6.2 v0.6.3 -> releases/v0.6.3' ]] || die "update-prune: $(releases)"

printf '==> a different --channel switches, also back; --update never leaves the channel\n'
run channel-up sh "$M/current/install.sh" --channel 0.7
expect channel-up 0 'coding-agent-kit: switched from v0.6.3 to v0.7.0 (channel 0.7).'
logged "v0.6.3 --channel 0.7
v0.7.0 --activate --channel 0.7 --prefix $H/.local/"
[[ "$(cat "$M/channel")" == 0.7 && "$(releases)" == 'v0.6.3 v0.7.0 -> releases/v0.7.0' ]] || die "channel-up: $(releases)"
release v0.6.4
run update-channel "$M/current/install.sh" --update
expect update-channel 0 "$(already v0.7.0 0.7)"
logged 'v0.7.0 --update'
run channel-down "$M/current/install.sh" --update --channel 0.6
expect channel-down 0 'coding-agent-kit: switched from v0.7.0 to v0.6.4 (channel 0.6).'
logged "v0.7.0 --update --channel 0.6
v0.6.4 --activate --channel 0.6 --prefix $H/.local/"
[[ "$(cat "$M/channel")" == 0.6 && "$(releases)" == 'v0.6.4 v0.7.0 -> releases/v0.6.4' ]] || die "channel-down: $(releases)"
[[ "$(links "$H/.local")" == "$LINKS" ]] || die 'a channel switch changed a link'

printf '==> a foreign path refuses an update, which keeps the current release\n'
release v0.6.5
mv "$H/.claude/coding-agent-kit/notify.cjs" "$TMP/notify.cjs"
printf 'my own notify\n' >"$H/.claude/coding-agent-kit/notify.cjs"
BEFORE="$(snapshot "$H/.local/bin")$(snapshot "$H/.claude")"
run update-foreign "$M/current/install.sh" --update
expect update-foreign 1 '' "install.sh: refusing $H/.claude/coding-agent-kit/notify.cjs: a file, not an absolute link into $MP
install.sh: nothing changed; move those paths aside or choose another --prefix."
logged "v0.6.4 --update
v0.6.5 --activate --channel 0.6 --prefix $H/.local/"
[[ "$(readlink "$M/current")" == releases/v0.6.4 ]] || die 'a refused update switched the release'
[[ "$(snapshot "$H/.local/bin")$(snapshot "$H/.claude")" == "$BEFORE" ]] || die 'a refused update changed a link'
mv "$TMP/notify.cjs" "$H/.claude/coding-agent-kit/notify.cjs"
run update-unblocked "$M/current/install.sh" --update
expect update-unblocked 0 'coding-agent-kit: switched from v0.6.4 to v0.6.5 (channel 0.6).'
logged "v0.6.4 --update
v0.6.5 --activate --channel 0.6 --prefix $H/.local/"

printf '==> a missing link is put back; --uninstall removes only the managed links, --purge the root\n'
rm "$H/.local/bin/claude-as"
run repair "$M/current/install.sh"
expect repair 0 "coding-agent-kit: v0.6.5 (channel 0.6) in $MP
$H/.local/bin/claude-as -> $MP/current/bin/claude-as
$(installed v0.6.5 | sed -n '/^zsh: /,$p')"
logged v0.6.5
[[ "$(links "$H/.local")" == "$LINKS" ]] || die 'the repair changed another link'
ln -sf "$ROOT/completions/zsh/_codex-as" "$H/.local/share/zsh/site-functions/_codex-as"
FOREIGN="kept $H/.local/share/zsh/site-functions/_codex-as: a symlink to $ROOT/completions/zsh/_codex-as, not an absolute link into $MP"
run uninstall "$M/current/install.sh" --uninstall
expect uninstall 0 "removed $H/.local/bin/codex-as
removed $H/.local/share/bash-completion/completions/codex-as
$FOREIGN
removed $H/.local/bin/claude-as
removed $H/.local/share/bash-completion/completions/claude-as
removed $H/.local/share/zsh/site-functions/_claude-as
removed $H/.claude/coding-agent-kit/statusline.cjs
removed $H/.claude/coding-agent-kit/notify.cjs
removed $H/.claude/coding-agent-kit/check-git-hooks.cjs
removed $H/.claude/coding-agent-kit"
logged 'v0.6.5 --uninstall'
[[ -L "$M/current" && -d "$M/repo.git" ]] || die '--uninstall without --purge removed the managed root'
run uninstall-again "$M/current/install.sh" --uninstall
expect uninstall-again 0 "$FOREIGN"
logged 'v0.6.5 --uninstall'
rm "$H/.local/share/zsh/site-functions/_codex-as"
run uninstall-none "$M/current/install.sh" --uninstall
expect uninstall-none 0 'coding-agent-kit: no managed install links to remove.'
logged 'v0.6.5 --uninstall'
boot purge /bin/sh -- --uninstall --purge
expect purge 0 "removed $M"
logged 'v0.6.5 --managed --uninstall --purge'
[[ ! -e "$M" && -z "$(links "$H/.local")" ]] || die '--purge left the managed root or a link'

printf '==> the default channel is the newest release'"'"'s; a remembered one stays\n'
home default
boot default /bin/sh
expect default 0 "$(installed v0.7.0)"
logged "v0.7.0 --activate --channel 0.7 --prefix $H/.local/"
release v0.8.0
run default-update "$M/current/install.sh" --update
expect default-update 0 "$(already v0.7.0 0.7)"
logged 'v0.7.0 --update'
# A checkout's install.sh with --managed is the bootstrap too, and delegates.
run default-checkout "$ROOT/install.sh" --managed
expect default-checkout 0 "$(already v0.7.0 0.7)"
logged 'v0.7.0 --managed'
boot default-purge /bin/sh -- --uninstall --purge
[[ "$rc" == 0 && ! -e "$M" ]] || die 'default-purge'
: >"$LOG"

printf '==> --prefix and XDG_DATA_HOME are honored and the prefix is remembered\n'
home prefix
PREFIX="$TMP/pre fix"
DATA="$TMP/data home"
M="$DATA/coding-agent-kit" MP="$DATA/coding-agent-kit"
run prefix-checkout XDG_DATA_HOME="$DATA" BASH_COMPLETION_USER_DIR="$H/.local/share/bash-completion" \
    "$ROOT/install.sh" --managed --channel 0.6 --prefix "$PREFIX"
expect prefix-checkout 0 "$(installed v0.6.5 "$PREFIX")"
logged "v0.6.5 --activate --channel 0.6 --prefix $PREFIX/"
[[ "$(cat "$M/prefix")" == "$PREFIX" && ! -e "$H/.local/bin" && ! -e "$H/.local/share/coding-agent-kit" ]] ||
    die 'prefix: the prefix or the managed root'
release v0.6.6
run prefix-update XDG_DATA_HOME="$DATA" BASH_COMPLETION_USER_DIR="$H/.local/share/bash-completion" \
    "$M/current/install.sh" --update
expect prefix-update 0 'coding-agent-kit: switched from v0.6.5 to v0.6.6 (channel 0.6).'
logged "v0.6.5 --update
v0.6.6 --activate --channel 0.6 --prefix $PREFIX/"
[[ ! -e "$H/.local/bin" ]] || die 'prefix-update linked into the default prefix'
run prefix-uninstall XDG_DATA_HOME="$DATA" BASH_COMPLETION_USER_DIR="$H/.local/share/bash-completion" \
    "$M/current/install.sh" --uninstall --purge
[[ "$rc" == 0 && ! -e "$M" && -z "$(find "$PREFIX" "$H" -type l)" ]] || die 'prefix-uninstall left a link'
: >"$LOG"

printf '==> refusals: foreign paths before the first install, channels, usage and a missing install\n'
home refusal
mkdir -p "$H/.local/bin" "$H/.claude/coding-agent-kit"
printf 'not a link\n' >"$H/.local/bin/claude-as"
ln -s "$ROOT/bin/codex-as" "$H/.local/bin/codex-as"
ln -s ../elsewhere/notify.cjs "$H/.claude/coding-agent-kit/notify.cjs"
BEFORE="$(snapshot "$H/.local/bin")$(snapshot "$H/.claude")"
boot refusal /bin/sh -- --channel 0.6
expect refusal 1 '' "install.sh: refusing $H/.local/bin/codex-as: a symlink to $ROOT/bin/codex-as, not an absolute link into $MP
install.sh: refusing $H/.local/bin/claude-as: a file, not an absolute link into $MP
install.sh: refusing $H/.claude/coding-agent-kit/notify.cjs: a symlink to ../elsewhere/notify.cjs, not an absolute link into $MP
install.sh: nothing changed; move those paths aside or choose another --prefix."
logged "v0.6.6 --activate --channel 0.6 --prefix $H/.local/"
[[ ! -e "$M/current" && ! -L "$M/current" ]] || die 'a refused first install made current'
[[ "$(snapshot "$H/.local/bin")$(snapshot "$H/.claude")" == "$BEFORE" ]] || die 'a refused install changed a path'
[[ -z "$(find "$H/.local/share" -type l)" ]] || die 'a refused install placed a link'
run refusal-update sh "$ROOT/install.sh" --update
expect refusal-update 1 '' "install.sh: no managed install in $M to update; install one first: install.sh --managed --channel MAJOR.MINOR"
home channels
for args in '--channel 0.5' '--channel 1' '--channel 0.6.1' '--channel 0.06' '--channel' '--purge' \
    '--update --activate' '--update --reinstall' '--uninstall --channel 0.6' '--uninstall --update' \
    '--uninstall --reinstall' '--bogus'; do
    # shellcheck disable=SC2086 # each case is a word list
    boot channel /bin/sh -- $args
    [[ "$rc" == 2 && ! -s "$OUT/channel.out" && -s "$OUT/channel.err" ]] || die "install.sh $args: exit $rc, expected 2"
done
boot channel-old /bin/sh -- --channel 0.5
expect channel-old 2 '' 'install.sh: channel 0.5 predates managed installs; the first channel with them is 0.6.'
boot channel-none /bin/sh -- --channel 0.9
expect channel-none 1 '' "install.sh: $URL has no release in channel 0.9."
[[ ! -e "$M/current" && -z "$(find "$H" -type l)" ]] || die 'channel-none installed something'
run activate-checkout "$ROOT/install.sh" --activate
expect activate-checkout 1 '' 'install.sh: --activate works only in a release of the managed root.'
logged ''

printf '==> a stalled repository fails a run within seconds, which leaves the lock free\n'
home stall
boot stall-install /bin/sh -- --channel 0.6
[[ "$rc" == 0 ]] || die "stall-install: exit $rc"
# A 127.0.0.1 server that accepts and never answers. Git's own
# GIT_HTTP_LOW_SPEED_TIME shortens the 60 s that the installer allows a
# transfer below its 1000 bytes/s; without that limit git would wait forever.
python3 -I -c 'import os, socket, sys
server = socket.create_server(("127.0.0.1", 0))
with open(sys.argv[1] + ".tmp", "w") as file:
    file.write(str(server.getsockname()[1]))
os.rename(sys.argv[1] + ".tmp", sys.argv[1])
held = []
while True:
    held.append(server.accept())' "$TMP/stall.port" &
STALL=$!
for _ in $(seq 100); do
    [[ ! -e "$TMP/stall.port" ]] || break
    sleep 0.1
done
STALLED="http://127.0.0.1:$(cat "$TMP/stall.port")/kit.git"
BEFORE="$(snapshot "$H")"
started=$SECONDS
run stall CODING_AGENT_KIT_REPO_URL="$STALLED" GIT_HTTP_LOW_SPEED_TIME=2 sh "$M/current/install.sh" --update
[[ "$rc" == 1 && "$((SECONDS - started))" -lt 30 && "$(wc -l <"$OUT/stall.err")" -eq 1 ]] ||
    die "stall: exit $rc after $((SECONDS - started)) s: $(cat "$OUT/stall.err")"
grep -Fq "install.sh: cannot list the releases at $STALLED: " "$OUT/stall.err" || die "stall: $(cat "$OUT/stall.err")"
kill "$STALL"
STALL=''
[[ "$(snapshot "$H")" == "$BEFORE" ]] || die 'a stalled update wrote below HOME'
run stall-after sh "$M/current/install.sh" --update
expect stall-after 0 "$(already v0.6.6 0.6)"

printf '==> --reinstall repairs a broken release that every other run is handed to\n'
: >"$LOG"
printf '#!/bin/sh\nexit 3\n' >"$M/current/install.sh"
boot reinstall-broken /bin/sh -- --channel 0.6
[[ "$rc" == 3 ]] || die "reinstall-broken: exit $rc"
boot reinstall /bin/sh -- --reinstall
expect reinstall 0 "coding-agent-kit: exported v0.6.6 again from $URL.
$(already v0.6.6 0.6)"
logged "v0.6.6 --activate --channel 0.6 --prefix $H/.local/"
fixture_git -C "$REMOTE" show v0.6.6:install.sh >"$TMP/released install.sh"
cmp -s "$TMP/released install.sh" "$M/current/install.sh" || die '--reinstall did not restore the release'
[[ "$(releases)" == 'v0.6.6 -> releases/v0.6.6' && -d "$M/repo.git" ]] || die "reinstall: $(releases)"

printf '==> the remembered prefix must be absolute; stray temporary links are removed\n'
printf 'relative/dir\n' >"$M/prefix"
run prefix-bad sh "$M/current/install.sh" --update
expect prefix-bad 1 '' "install.sh: $M/prefix holds no absolute directory; pass --prefix DIR."
printf '%s\n' "$H/.local" >"$M/prefix"
ln -s nowhere "$H/.local/bin/.tmp.coding-agent-kit.1"
ln -s nowhere "$H/.claude/coding-agent-kit/.tmp.coding-agent-kit.1"
run stray sh "$M/current/install.sh" --update
expect stray 0 "$(already v0.6.6 0.6)"
[[ ! -L "$H/.local/bin/.tmp.coding-agent-kit.1" && ! -L "$H/.claude/coding-agent-kit/.tmp.coding-agent-kit.1" ]] ||
    die 'a stray temporary link stayed'

printf '==> without flock, as on some NFS and Lustre mounts, the lock is a directory with its owner\n'
# python3 whose flock fails with ENOLCK, as there.
NOLOCK="$TMP/nolock tools"
mkdir "$NOLOCK"
for tool in "$TOOLS"/*; do
    [[ "${tool##*/}" == python3 ]] || ln -s "$tool" "$NOLOCK/${tool##*/}"
done
cat >"$NOLOCK/python3" <<EOF
#!/bin/sh
[ "\$1 \$2 \$3" = '-I -B -c' ] || exec "$TOOLS/python3" "\$@"
code=\$4
shift 4
exec "$TOOLS/python3" -I -B -c "import errno, fcntl
def flock(fd, operation):
    raise OSError(errno.ENOLCK, 'No locks available')
fcntl.flock = flock
\$code" "\$@"
EOF
chmod 755 "$NOLOCK/python3"
HOST="$(python3 -I -c 'import socket; print(socket.gethostname())')"
home nolock
boot nolock /bin/sh PATH="$H/.local/bin:$NOLOCK" -- --channel 0.6
expect nolock 0 "$(installed v0.6.6)"
[[ ! -e "$M/.lock.d" ]] || die 'the lock directory stayed after the run'
BUSY="install.sh: another install.sh holds $MP/.lock; nothing changed."
sh -c 'exit 0' &
GONE=$!
wait "$GONE"
# A lock of another host or of a live pid here holds; a dead pid here, or an
# owner older than an hour, is a lock left behind.
NOW="$(date +%s)"
for entry in "75 elsewhere 1 $NOW" "75 $HOST $$ $NOW" "0 $HOST $GONE $NOW" "0 elsewhere 1 $((NOW - 7200))"; do
    mkdir "$M/.lock.d"
    printf '%s\n' "${entry#* }" >"$M/.lock.d/owner"
    run nolock-held PATH="$H/.local/bin:$NOLOCK" sh "$M/current/install.sh" --update
    if [[ "${entry%% *}" == 75 ]]; then
        expect nolock-held 75 '' "$BUSY"
    else
        expect nolock-held 0 "$(already v0.6.6 0.6)"
        [[ ! -e "$M/.lock.d" ]] || die "nolock: the lock of ${entry#* } stayed"
    fi
    rm -rf "$M/.lock.d"
done
printf 'a file\n' >"$M/.lock.d"
run nolock-file PATH="$H/.local/bin:$NOLOCK" sh "$M/current/install.sh" --update
expect nolock-file 1 '' "install.sh: cannot lock $MP/.lock: this file system has no flock, and the lock directory $MP/.lock.d cannot be made (Not a directory); set XDG_DATA_HOME to a local directory."
rm "$M/.lock.d"

[[ -z "$(ls -A "$WORK")" ]] || die 'a run wrote into its working directory'
if grep -rl -e fabricated-access "$OUT"; then
    die 'an output above contains a fabricated token'
fi
printf 'managed-install=PASS\n'
