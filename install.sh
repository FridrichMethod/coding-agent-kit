#!/bin/sh
# install.sh: install coding-agent-kit from a checkout, or as a managed install.
#
# Checkout mode, install.sh [--prefix DIR] [--uninstall] run from a checkout:
# link this checkout's launchers and completions. It replaces or removes only
# its own links: those whose text starts with this checkout's physical path
# (as pwd -P prints it) and has no . or .. components, the form it writes,
# even when that target has since moved or gone. A file, a directory, another
# checkout's link, a link made through another spelling of this checkout's
# path and every relative link (Stow's or one made by hand) are never touched.
# It places no link through a relative directory link below HOME or the
# prefix's parent, the form Stow folding writes; HOME itself and links above
# it never count. It checks every destination before it changes anything.
#
# Managed mode, with --managed, --channel, --update or --purge, without a
# checkout next to this file (piped to sh), and always for a release of the
# managed root ${XDG_DATA_HOME:-~/.local/share}/coding-agent-kit. The root
# holds a bare clone (repo.git), exported release trees (releases/vX.Y.Z),
# the link current to one of them, the remembered channel and prefix, and the
# lock file .lock (exit 75 while another run holds it). A run lists the tags
# of the repository, exports the highest vMAJOR.MINOR.PATCH of its channel and
# hands over to that release's own install.sh --activate, which checks every
# destination, renames current over to itself, keeps the previous release,
# prunes older ones and links the launchers, completions and
# ~/.claude/coding-agent-kit/*.cjs to current/, with the ownership rules above
# for links into the managed root. A launcher resolves to its physical
# release, so a switch never moves a running session. All work starts on the
# last line, so a truncated download runs nothing.
set -eu
fail() {
    printf 'install.sh: %s\n' "$2" >&2
    exit "$1"
}
usage() {
    printf '%s\n' 'Usage: install.sh [--prefix DIR] [--uninstall]' \
        '       install.sh --managed [--channel MAJOR.MINOR] [--update] [--prefix DIR]' \
        '       install.sh --managed --uninstall [--purge] [--prefix DIR]' \
        'From a checkout: links bin/{codex-as,claude-as} into DIR/bin (default DIR: ~/.local), their bash completions into' \
        '${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-~/.local/share}/bash-completion}/completions and their' \
        'zsh completions into DIR/share/zsh/site-functions. Refuses any other existing path there.' \
        'Managed (piped to sh, or --managed): installs the newest release of the channel (default: that of the' \
        'newest release) in ${XDG_DATA_HOME:-~/.local/share}/coding-agent-kit and links the same paths and' \
        'the helpers ~/.claude/coding-agent-kit/{statusline,notify,check-git-hooks}.cjs to it; --update takes only' \
        'a newer patch of the remembered channel; --purge also removes the managed root. Exit 75: another run holds' \
        'the lock.' >&"$1"
    exit "$2"
}
# number TEXT: a decimal number of at most nine digits, without a leading zero.
number() {
    case $1 in '' | *[!0-9]* | 0?* | ??????????*) return 1 ;; esac
}
channel_ok() {
    case $1 in *.*.*) return 1 ;; *.*) number "${1%.*}" && number "${1#*.}" ;; *) return 1 ;; esac
}
version_ok() {
    case $1 in *.*.*.*) return 1 ;; *.*.*) channel_ok "${1%.*}" && number "${1##*.}" ;; *) return 1 ;; esac
}
# managed_ok CHANNEL: the channel has managed installs, which 0.6 introduced.
managed_ok() {
    [ "${1%.*}" -gt 0 ] || [ "${1#*.}" -ge 6 ]
}

# Shared by both modes. ours PATH: PATH is a link whose text is an absolute
# path in root (the checkout, or the managed root), the form place writes.
ours() {
    [ -L "$1" ] && target=$(readlink "$1") || return 1
    case /$target/ in */../* | */./*) return 1 ;; esac
    case $target in "$root"/?*) return 0 ;; *) return 1 ;; esac
}
what() {
    if [ -L "$1" ]; then
        printf 'a symlink to %s' "$(readlink "$1")"
    elif [ -d "$1" ]; then
        printf 'a directory'
    else
        printf 'a file'
    fi
}
each() {
    for tool in codex-as claude-as; do
        "$1" "$prefix/bin/$tool" "bin/$tool"
        "$1" "$bash_dir/$tool" "completions/bash/$tool"
        "$1" "$zsh_dir/_$tool" "completions/zsh/_$tool"
    done
}
# bounds: check looks for relative directory links strictly below these two,
# so links above them (macOS /var -> private/var) never count; the prefix
# stands in for /.
bounds() {
    zsh_dir=$prefix/share/zsh/site-functions
    home=${HOME%/} base=$(dirname -- "$prefix")
    [ -n "$home" ] || home=$prefix
    [ "$base" != / ] || base=$prefix
}
# folded PATH: a directory on the way to PATH is a relative link (left in dir).
# A relative directory link on the way is what Stow folding writes (a missing
# ~/.local/bin becomes a link into another repository's package): a link
# placed through it would land in that repository. Absolute aliases are fine.
folded() {
    dir=$(dirname -- "$1")
    while :; do
        case $dir in "$home"/?* | "$base"/?*) ;; *) return 1 ;; esac
        case ${HOME%/}/ in "$dir"/*) return 1 ;; esac # HOME and its ancestors never count
        if [ -L "$dir" ]; then
            case $(readlink "$dir") in /*) ;; *) return 0 ;; esac
        fi
        dir=$(dirname -- "$dir")
    done
}
check() {
    [ -f "$src/$2" ] || fail 1 "$src is not a complete coding-agent-kit checkout (missing $2)."
    if { [ -e "$1" ] || [ -L "$1" ]; } && ! ours "$1"; then
        printf 'install.sh: refusing %s: %s, not an absolute link into %s\n' "$1" "$(what "$1")" "$root" >&2
        refused=1
        return 0
    fi
    if folded "$1"; then
        printf 'install.sh: refusing %s: %s is a relative link to %s, the form Stow folding writes\n' \
            "$1" "$dir" "$(readlink "$dir")" >&2
        refused=1
        return 0
    fi
    # place must be able to create the missing directories and the link.
    dir=$(dirname -- "$1")
    while [ ! -e "$dir" ] && [ ! -L "$dir" ]; do dir=$(dirname -- "$dir"); done
    if [ ! -d "$dir" ] || [ ! -w "$dir" ] || [ ! -x "$dir" ]; then
        printf 'install.sh: refusing %s: its nearest existing parent %s is not a writable directory\n' "$1" "$dir" >&2
        refused=1
    fi
}
place() {
    mkdir -p -- "$(dirname -- "$1")"
    rm -f -- "$1"
    ln -s -- "$root/$2" "$1"
    printf '%s -> %s\n' "$1" "$root/$2"
}
# uncheck PATH: --uninstall can tell whether PATH is its own link, and remove it.
uncheck() {
    dir=$(dirname -- "$1")
    while [ ! -e "$dir" ]; do dir=$(dirname -- "$dir"); done
    if [ -d "$dir" ] && [ ! -x "$dir" ]; then
        printf 'install.sh: refusing to look for %s: %s is not a searchable directory\n' "$1" "$dir" >&2
        refused=1
    elif ours "$1" && [ ! -w "$dir" ]; then
        printf 'install.sh: refusing to remove %s: %s is not a writable directory\n' "$1" "$dir" >&2
        refused=1
    fi
}
remove() {
    if ours "$1"; then
        rm -f -- "$1"
        printf 'removed %s\n' "$1"
        found=1
    elif [ -e "$1" ] || [ -L "$1" ]; then
        printf 'kept %s: %s, not an absolute link into %s\n' "$1" "$(what "$1")" "$root"
        found=1
    fi
}

checkout_main() {
    src=$root
    refused=0
    if [ "$uninstall" = 1 ]; then
        each uncheck
        [ "$refused" = 0 ] || fail 1 'nothing changed; make those directories writable and searchable first.'
        each remove
        exit 0
    fi
    each check
    [ "$refused" = 0 ] || fail 1 'nothing changed; move those paths aside or choose another --prefix.'
    each place
    printf '%s\n' 'zsh: unless fpath has it, add this before compinit (with oh-my-zsh, before sourcing it):' \
        "  fpath=(\"$zsh_dir\" \$fpath)" 'Claude Code helpers, as commands in settings.json:' \
        "  statusLine:             node \"$root/helpers/statusline.cjs\"" \
        "  Notification hook:      node \"$root/helpers/notify.cjs\"" \
        "  PreToolUse hook (Bash): node \"$root/helpers/check-git-hooks.cjs\""
    case :${PATH-}: in *":$prefix/bin:"*) ;; *) printf 'PATH: add %s\n' "$prefix/bin" ;; esac
}

# Managed mode. Every child gets /dev/null as stdin, since a piped install.sh
# is the shell's stdin, and only this shell and its children hold fd 9, the
# lock: git, which can start a detached gc, gets it closed.
helpers() {
    for helper in statusline notify check-git-hooks; do
        "$1" "$claude_dir/$helper.cjs" "helpers/$helper.cjs"
    done
}
need() {
    command -v "$1" >/dev/null 2>&1 || fail 1 "managed mode needs $1, which is not on PATH."
}
kit_git() {
    GIT_TERMINAL_PROMPT=0 git -c gc.auto=0 -c maintenance.auto=false "$@" </dev/null 9>&-
}
REPLACE_PY='import os, sys
try:
    os.replace(sys.argv[1], sys.argv[2])
except OSError as error:
    sys.exit(f"install.sh: cannot rename {sys.argv[1]} to {sys.argv[2]}: {error.strerror}")'
# replace TEMP PATH: rename TEMP over PATH, a link included (never through it).
replace() {
    python3 -I -B -c "$REPLACE_PY" "$1" "$2" </dev/null || exit 1
}
LOCK_PY='import fcntl, os, sys
try:
    held = os.path.samestat(os.fstat(9), os.stat(sys.argv[1]))
except OSError:
    held = False
if sys.argv[2] == "held":
    sys.exit(0 if held else 1)
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(75)
except OSError as error:
    sys.exit(f"install.sh: cannot lock {sys.argv[1]}: {error.strerror}")'
# lock: take the managed root's lock on fd 9, which the run that started this
# one may already hold for it, and drop what a killed run left behind.
lock() {
    if ! python3 -I -B -c "$LOCK_PY" "$M/.lock" held </dev/null; then
        { [ -w "$M" ] && { [ ! -e "$M/.lock" ] || { [ -r "$M/.lock" ] && [ -w "$M/.lock" ]; }; }; } ||
            fail 1 "cannot open the lock file $M/.lock."
        exec 9<>"$M/.lock"
    fi
    python3 -I -B -c "$LOCK_PY" "$M/.lock" lock </dev/null || {
        [ "$?" != 75 ] || fail 75 "another install.sh holds $M/.lock; nothing changed."
        exit 1
    }
    rm -rf -- "$M"/.tmp.* "$M"/releases/.tmp.*
}
# state NAME: the value remembered in the managed root, or nothing.
state() {
    value=
    [ ! -f "$M/$1" ] || IFS= read -r value <"$M/$1" || :
    printf '%s\n' "$value"
}
remember() {
    [ ! -f "$M/$1" ] || [ "$(state "$1")" != "$2" ] || return 0
    printf '%s\n' "$2" >"$M/.tmp.$1.$$"
    replace "$M/.tmp.$1.$$" "$M/$1"
}
# current: the tag that current names, when that release is complete.
current() {
    link=$(readlink "$M/current" 2>/dev/null) || return 0
    case $link in releases/v*) ;; *) return 0 ;; esac
    tag=${link#releases/}
    case $tag in */*) return 0 ;; esac
    if version_ok "${tag#v}" && [ -f "$M/$link/install.sh" ]; then printf '%s\n' "$tag"; fi
}
# releases_at URL: the X.Y.Z of every vX.Y.Z tag there, ascending.
releases_at() {
    if ! out=$(kit_git ls-remote --tags "$1" 2>&1); then
        fail 1 "cannot list the releases at $1: $(printf '%s\n' "$out" | sed -n '1p')"
    fi
    printf '%s\n' "$out" | while read -r _ ref; do
        version=${ref#refs/tags/v}
        if [ "$version" != "$ref" ] && version_ok "$version"; then printf '%s\n' "$version"; fi
    done | sort -t. -k1,1n -k2,2n -k3,3n
}
newest() {
    printf '%s\n' "$@" | sort -t. -k1,1n -k2,2n -k3,3n | sed -n '$p'
}
# missing PATH FILE: PATH is not yet the link to current/FILE.
missing() {
    if [ ! -L "$1" ] || [ "$(readlink "$1")" != "$mroot/current/$2" ]; then pending=$((pending + 1)); fi
}
# mplace PATH FILE: make PATH the link to current/FILE, by a rename in its directory.
mplace() {
    [ ! -L "$1" ] || [ "$(readlink "$1")" != "$mroot/current/$2" ] || return 0
    mkdir -p -- "$(dirname -- "$1")"
    temp=$(dirname -- "$1")/.tmp.coding-agent-kit.$$
    rm -f -- "$temp"
    ln -s -- "$mroot/current/$2" "$temp"
    replace "$temp" "$1"
    printf '%s -> %s\n' "$1" "$mroot/current/$2"
}
prefix_state() {
    if [ -z "$prefix_given" ] && [ -f "$M/prefix" ]; then prefix=$(state prefix); fi
    bounds
}
# delegate: run the current release's install.sh with these options instead,
# so only released code changes an existing managed install.
delegate() {
    [ -z "$release" ] && [ -f "$M/current/install.sh" ] || return 0
    set -- --managed
    [ -z "$prefix_given" ] || set -- "$@" --prefix "$prefix/"
    [ -z "$channel" ] || set -- "$@" --channel "$channel"
    [ "$update" = 0 ] || set -- "$@" --update
    [ "$uninstall" = 0 ] || set -- "$@" --uninstall
    [ "$purge" = 0 ] || set -- "$@" --purge
    exec sh "$M/current/install.sh" "$@"
}
# activate: this release becomes current; the run that exported it holds the lock.
activate() {
    version=${release#v}
    [ -n "$release" ] && version_ok "$version" && [ "$release" = "v$version" ] ||
        fail 1 '--activate works only in a release of the managed root.'
    ch=${channel:-$(state channel)}
    ch=${ch:-${version%.*}}
    [ "$ch" = "${version%.*}" ] || fail 1 "$release is not a release of channel $ch."
    mroot=$(CDPATH='' cd -P -- "$M" && pwd)
    lock
    src=$root root=$mroot refused=0
    each check
    helpers check
    [ "$refused" = 0 ] || fail 1 'nothing changed; move those paths aside or choose another --prefix.'
    old=$(current) pending=0
    each missing
    helpers missing
    if [ "$old" = "$release" ] && [ "$pending" = 0 ] && [ "$(state channel)" = "$ch" ] &&
        [ -f "$M/prefix" ] && [ "$(state prefix)" = "$prefix" ]; then
        printf 'coding-agent-kit: %s is already current (channel %s).\n' "$release" "$ch"
        return 0
    fi
    if [ "$old" != "$release" ]; then
        rm -f -- "$M/.tmp.current.$$"
        ln -s -- "releases/$release" "$M/.tmp.current.$$"
        replace "$M/.tmp.current.$$" "$M/current"
    fi
    remember channel "$ch"
    remember prefix "$prefix"
    if [ "$old" != "$release" ]; then
        for dir in "$M"/releases/v*; do
            case ${dir##*/} in "$release" | "$old") ;; *) [ ! -d "$dir" ] || rm -rf -- "$dir" ;; esac
        done
    fi
    if [ -z "$old" ] || [ "$old" = "$release" ]; then
        printf 'coding-agent-kit: %s (channel %s) in %s\n' "$release" "$ch" "$mroot"
    else
        printf 'coding-agent-kit: switched from %s to %s (channel %s).\n' "$old" "$release" "$ch"
    fi
    each mplace
    helpers mplace
    [ "$pending" != 0 ] || return 0
    printf '%s\n' 'zsh: unless fpath has it, add this before compinit (with oh-my-zsh, before sourcing it):' \
        "  fpath=(\"$zsh_dir\" \$fpath)" 'Claude Code helpers, as commands in settings.json:'
    for entry in 'statusLine:             statusline' 'Notification hook:      notify' \
        'PreToolUse hook (Bash): check-git-hooks'; do
        printf '  %s node -e "require(require('\''os'\'').homedir() + '\''/.claude/coding-agent-kit/%s.cjs'\'').main()"\n' \
            "${entry% *}" "${entry##* }"
    done
    case :${PATH-}: in *":$prefix/bin:"*) ;; *) printf 'PATH: add %s\n' "$prefix/bin" ;; esac
}
managed_uninstall() {
    mroot=$M
    if [ -d "$M" ]; then
        mroot=$(CDPATH='' cd -P -- "$M" && pwd)
        lock
    fi
    prefix_state
    src=$root root=$mroot refused=0 found=0
    each uncheck
    helpers uncheck
    [ "$refused" = 0 ] || fail 1 'nothing changed; make those directories writable and searchable first.'
    each remove
    helpers remove
    if [ "$found" = 1 ] && rmdir -- "$claude_dir" 2>/dev/null; then printf 'removed %s\n' "$claude_dir"; fi
    if [ "$purge" = 1 ] && { [ -e "$M" ] || [ -L "$M" ]; }; then
        rm -rf -- "$M/repo.git" "$M/releases" "$M/current" "$M/channel" "$M/prefix" "$M"/.tmp.* "$M/.lock"
        if rmdir -- "$M" 2>/dev/null; then
            printf 'removed %s\n' "$M"
        else
            printf 'kept %s: %s that holds files install.sh did not make\n' "$M" "$(what "$M")"
        fi
    elif [ "$found" = 0 ]; then
        printf 'coding-agent-kit: no managed install links to remove.\n'
    fi
}
managed_main() {
    case $M in /*) ;; *) fail 1 "the managed root $M is not absolute." ;; esac
    claude_dir=${HOME%/}/.claude/coding-agent-kit
    need python3
    if [ "$uninstall" = 1 ]; then
        delegate
        managed_uninstall
        exit 0
    fi
    need git
    need tar
    if [ "$activate" = 1 ]; then
        prefix_state
        activate
        exit 0
    fi
    delegate
    [ "$update" = 0 ] || [ -n "$(current)" ] ||
        fail 1 "no managed install in $M to update; install one first: install.sh --managed --channel MAJOR.MINOR"
    prefix_state
    ! folded "$M/current" ||
        fail 1 "refusing the managed root $M: $dir is a relative link to $(readlink "$dir"), the form Stow folding writes"
    mkdir -p -- "$M" 2>/dev/null || fail 1 "cannot create the managed root $M."
    mroot=$(CDPATH='' cd -P -- "$M" && pwd)
    lock
    cur=$(current)
    [ "$update" = 0 ] || [ -n "$cur" ] || fail 1 "no managed install in $M to update."
    url=${CODING_AGENT_KIT_REPO_URL:-}
    if [ -z "$url" ] && [ -d "$M/repo.git" ]; then url=$(kit_git -C "$M/repo.git" config --get remote.origin.url) || url=; fi
    url=${url:-https://github.com/FridrichMethod/coding-agent-kit.git}
    case $url in -*) fail 1 "the repository URL $url starts with -." ;; esac
    list=$(releases_at "$url")
    [ -n "$list" ] || fail 1 "$url has no vMAJOR.MINOR.PATCH release."
    ch=${channel:-$(state channel)}
    last=$(printf '%s\n' "$list" | sed -n '$p')
    ch=${ch:-${last%.*}}
    channel_ok "$ch" || fail 1 "the channel $ch in $M/channel is not MAJOR.MINOR."
    managed_ok "$ch" || fail 1 "channel $ch predates managed installs; the first channel with them is 0.6."
    best=
    for version in $list; do
        case $version in "$ch".*) best=$version ;; esac
    done
    [ -n "$best" ] || fail 1 "$url has no release in channel $ch."
    target=v$best
    # Within the channel only a newer patch moves current.
    if [ -n "$cur" ] && [ "${cur#v}" != "$best" ] && [ "${cur%.*}" = "v$ch" ] &&
        [ "$(newest "${cur#v}" "$best")" = "${cur#v}" ]; then
        target=$cur
    fi
    if [ "$target" = "$release" ]; then
        channel=$ch
        activate
        exit 0
    fi
    if [ ! -d "$M/repo.git" ]; then
        kit_git clone --bare --quiet -- "$url" "$M/.tmp.repo.$$" || fail 1 "cannot clone $url."
        mv -- "$M/.tmp.repo.$$" "$M/repo.git"
    fi
    kit_git -C "$M/repo.git" rev-parse --quiet --verify "refs/tags/$target^{commit}" >/dev/null ||
        kit_git -C "$M/repo.git" fetch --quiet --no-tags -- "$url" "refs/tags/$target:refs/tags/$target" ||
        fail 1 "cannot fetch $target from $url."
    if [ ! -f "$M/releases/$target/install.sh" ]; then
        temp=$M/releases/.tmp.$target.$$
        mkdir -p -- "$temp"
        kit_git -C "$M/repo.git" archive --format=tar -o "$temp.tar" "refs/tags/$target" ||
            fail 1 "cannot export $target."
        (cd -- "$temp" && tar -xf "$temp.tar" </dev/null 9>&-) || fail 1 "cannot unpack $target."
        rm -f -- "$temp.tar"
        [ -f "$temp/install.sh" ] && [ -f "$temp/bin/codex-as" ] || fail 1 "$target is not a complete release."
        rm -rf -- "$M/releases/$target"
        mv -- "$temp" "$M/releases/$target"
    fi
    exec sh "$M/releases/$target/install.sh" --activate --channel "$ch" --prefix "$prefix/"
}

main() {
    unset prefix
    uninstall=0 managed=0 update=0 purge=0 activate=0 channel=
    while [ "$#" -gt 0 ]; do
        case $1 in
            --prefix) case ${2-} in /*) prefix=${2%/} && shift ;; *) fail 2 '--prefix needs an absolute directory.' ;; esac ;;
            --uninstall) uninstall=1 ;;
            --managed) managed=1 ;;
            --channel)
                channel_ok "${2-}" || fail 2 '--channel needs MAJOR.MINOR, for example 0.6.'
                managed_ok "$2" || fail 2 "channel $2 predates managed installs; the first channel with them is 0.6."
                channel=$2 managed=1 && shift
                ;;
            --update) update=1 managed=1 ;;
            --purge) purge=1 managed=1 ;;
            --activate) activate=1 managed=1 ;;
            --help) usage 1 0 ;;
            *) usage 2 2 ;;
        esac
        shift
    done
    if [ "$uninstall" = 1 ]; then
        [ -z "$channel" ] && [ "$update$activate" = 00 ] || fail 2 '--uninstall takes only --prefix, --managed and --purge.'
    else
        [ "$purge" = 0 ] || fail 2 '--purge needs --uninstall.'
        [ "$update$activate" != 11 ] || fail 2 '--update and --activate exclude each other.'
    fi
    case ${HOME-} in /*) ;; *) fail 1 'HOME must be set to an absolute path.' ;; esac
    prefix_given=${prefix+1}
    prefix=${prefix-$HOME/.local}
    bash_dir=${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion}/completions
    case $bash_dir in /*) ;; *) fail 1 "the bash completion directory $bash_dir is not absolute." ;; esac
    bounds
    # The root is the physical directory of this file after its links (no
    # readlink -f); none when sh reads this file from a pipe.
    self=$0 hops=0 root=
    case $self in */*) ;; *) [ -e "$self" ] || self=$(command -v "$self") || self= ;; esac
    if [ -n "$self" ]; then
        while [ -L "$self" ]; do
            [ "$((hops += 1))" -le 40 ] || fail 1 'symlink loop (more than 40 links).'
            link=$(readlink "$self") || fail 1 "cannot resolve the link $self."
            case $link in /*) self=$link ;; *) self=$(dirname -- "$self")/$link ;; esac
        done
        root=$(CDPATH='' cd -P -- "$(dirname -- "$self")" && pwd) || fail 1 "cannot resolve the directory of $self."
    fi
    M=${XDG_DATA_HOME:-$HOME/.local/share}/coding-agent-kit release=
    case $M in
        /*)
            if [ -n "$root" ] && releases=$(CDPATH='' cd -P -- "$M/releases" 2>/dev/null && pwd) &&
                [ "$(dirname -- "$root")" = "$releases" ]; then
                release=${root##*/}
            fi
            ;;
    esac
    if [ "$managed" = 1 ] || [ -n "$release" ] || [ -z "$root" ] ||
        { [ ! -f "$root/install.sh" ] && [ ! -f "$root/bin/codex-as" ]; }; then
        managed_main
    else
        checkout_main
    fi
}

main "$@"
