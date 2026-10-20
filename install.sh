#!/bin/sh
# install.sh [--prefix DIR] [--uninstall]: link this checkout's launchers and
# completions. It replaces or removes only its own links: those whose text
# starts with this checkout's physical path (as pwd -P prints it) and has no
# . or .. components, the form it writes, even when that target has since
# moved or gone. A file, a directory, another checkout's link, a link made
# through another spelling of this checkout's path and every relative link
# (Stow's or one made by hand) are never touched. It places no link through a
# relative directory link below HOME or the prefix's parent, the form Stow
# folding writes; HOME itself and links above it never count. It checks every
# destination before it changes anything.
set -eu
fail() {
    printf 'install.sh: %s\n' "$2" >&2
    exit "$1"
}
usage() {
    printf '%s\n' 'Usage: install.sh [--prefix DIR] [--uninstall]' \
        'Links bin/{codex-as,claude-as} into DIR/bin (default DIR: ~/.local), their bash completions into' \
        '${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-~/.local/share}/bash-completion}/completions and their' \
        'zsh completions into DIR/share/zsh/site-functions. Refuses any other existing path there.' >&"$1"
    exit "$2"
}
unset prefix
uninstall=0
while [ "$#" -gt 0 ]; do
    case $1 in
        --prefix) case ${2-} in /*) prefix=${2%/} && shift ;; *) fail 2 '--prefix needs an absolute directory.' ;; esac ;;
        --uninstall) uninstall=1 ;;
        --help) usage 1 0 ;;
        *) usage 2 2 ;;
    esac
    shift
done
case ${HOME-} in /*) ;; *) fail 1 'HOME must be set to an absolute path.' ;; esac
prefix=${prefix-$HOME/.local}
bash_dir=${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion}/completions
zsh_dir=$prefix/share/zsh/site-functions
case $bash_dir in /*) ;; *) fail 1 "the bash completion directory $bash_dir is not absolute." ;; esac
# check looks for relative directory links strictly below these two, so links
# above them (macOS /var -> private/var) never count; the prefix stands in for /.
home=${HOME%/} base=$(dirname -- "$prefix")
[ -n "$home" ] || home=$prefix
[ "$base" != / ] || base=$prefix
# The root is the physical directory of this file after its links (no readlink -f).
self=$0 hops=0
case $self in */*) ;; *) [ -e "$self" ] || self=$(command -v "$self") || fail 1 "cannot find $0." ;; esac
while [ -L "$self" ]; do
    [ "$((hops += 1))" -le 40 ] || fail 1 'symlink loop (more than 40 links).'
    link=$(readlink "$self") || fail 1 "cannot resolve the link $self."
    case $link in /*) self=$link ;; *) self=$(dirname -- "$self")/$link ;; esac
done
root=$(CDPATH='' cd -P -- "$(dirname -- "$self")" && pwd) || fail 1 "cannot resolve the directory of $self."
# ours PATH: PATH is a link whose text is an absolute path in root, the form place writes.
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
check() {
    [ -f "$root/$2" ] || fail 1 "$root is not a complete coding-agent-kit checkout (missing $2)."
    if { [ -e "$1" ] || [ -L "$1" ]; } && ! ours "$1"; then
        printf 'install.sh: refusing %s: %s, not an absolute link into %s\n' "$1" "$(what "$1")" "$root" >&2
        refused=1
        return 0
    fi
    # A relative directory link on the way is what Stow folding writes (a
    # missing ~/.local/bin becomes a link into another repository's package):
    # a link placed through it would land in that repository. Absolute
    # aliases are fine.
    dir=$(dirname -- "$1")
    while :; do
        case $dir in "$home"/?* | "$base"/?*) ;; *) break ;; esac
        case ${HOME%/}/ in "$dir"/*) break ;; esac # HOME and its ancestors never count
        if [ -L "$dir" ]; then
            case $(readlink "$dir") in
                /*) ;;
                *)
                    printf 'install.sh: refusing %s: %s is a relative link to %s, the form Stow folding writes\n' \
                        "$1" "$dir" "$(readlink "$dir")" >&2
                    refused=1
                    return 0
                    ;;
            esac
        fi
        dir=$(dirname -- "$dir")
    done
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
    elif [ -e "$1" ] || [ -L "$1" ]; then
        printf 'kept %s: %s, not an absolute link into %s\n' "$1" "$(what "$1")" "$root"
    fi
}
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
