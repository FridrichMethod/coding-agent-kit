# Installation

coding-agent-kit installs on top of an existing `codex` or `claude` and never
installs, updates or replaces either. Install it as a managed install that
follows the newest patch release of a minor line and updates itself, link a
checkout of your own with `install.sh`, or link or copy its files.

## Requirements

- Linux or macOS; WSL counts as Linux. Native Windows is not supported.
  The 0.6 line is the only one with `install.ps1`, the managed install of
  the helpers there: its installs keep working, frozen at their release, and
  `pwsh -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\coding-agent-kit\install.ps1" -Uninstall -Purge`
  removes one. `main` has no `install.ps1`, so a new or repaired 0.6
  install downloads it from the `v0.6.0` tag,
  `https://raw.githubusercontent.com/FridrichMethod/coding-agent-kit/v0.6.0/install.ps1`,
  and runs it with `-Channel 0.6` as the
  [v0.6.0 README](https://github.com/FridrichMethod/coding-agent-kit/blob/v0.6.0/README.md)
  describes.
- Python 3.11 or newer, standard library only (run as `python3 -I -B`, the
  launchers with `-S`).
- Bash for `codex-as` (3.2 is enough), POSIX `sh` for `claude-as` and
  `install.sh`, and ordinary POSIX utilities. The managed install also needs
  Git and `tar`.
- Codex CLI 0.161.0 or newer for `codex-as`, with no upper limit. Claude Code
  for `claude-as`; its token capture is audited for 2.1.296, and other
  versions fall back to an explicit manual paste.
- Node.js 22 or newer for the helpers; Git is optional for the status line.
  Usage reserves need no Node.
- Optional: bash-completion or zsh for account-name completion.
- Usage reserves need Claude Code 2.1.296 or newer for `claude-as`, and for
  `codex-as` codex-cli 0.161.0 or newer, the minimum of every launch, with no
  upper limit.

## Managed install

A managed install follows one channel, a `MAJOR.MINOR` line such as `0.9`:
it installs the highest `vMAJOR.MINOR.PATCH` release of it and later moves
to newer patches only. Install, update and remove it with:

```sh
curl -fsSL https://raw.githubusercontent.com/FridrichMethod/coding-agent-kit/main/install.sh | sh -s -- --channel 0.9
"${XDG_DATA_HOME:-$HOME/.local/share}/coding-agent-kit/current/install.sh" --update
"${XDG_DATA_HOME:-$HOME/.local/share}/coding-agent-kit/current/install.sh" --uninstall   # --purge: also the managed root
```

These commands, the paths below and the exit codes are the stable interface
for scripts and other repositories that install the kit; they change only
in a minor release. The same bootstrap command installs, re-installs and,
with another `--channel`, switches the channel.

`install.sh` runs as a managed install when no checkout is next to it (piped
to `sh`), with `--managed`, `--channel`, `--update` or `--purge`, and always
in a release of its managed root:

```
install.sh --managed [--channel MAJOR.MINOR] [--update] [--prefix DIR]
install.sh --managed --reinstall [--channel MAJOR.MINOR] [--prefix DIR]
install.sh --managed --uninstall [--purge] [--prefix DIR]
```

The managed root is `${XDG_DATA_HOME:-~/.local/share}/coding-agent-kit/`:

| Path | Content |
| --- | --- |
| `repo.git` | a bare clone of the repository |
| `releases/vX.Y.Z/` | exported release trees: the current one and the one before it |
| `current` | a link to `releases/vX.Y.Z`, replaced by a rename |
| `channel`, `prefix` | the remembered channel and `--prefix` |
| `.lock` | the lock file |

and the install links these paths to `current/`, with `DIR` as for a
[checkout](#from-a-checkout) below:

| Link | Target |
| --- | --- |
| `DIR/bin/codex-as`, `DIR/bin/claude-as` | `current/bin/` |
| `${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-~/.local/share}/bash-completion}/completions/{codex-as,claude-as}` | `current/completions/bash/` |
| `DIR/share/zsh/site-functions/{_codex-as,_claude-as}` | `current/completions/zsh/` |
| `~/.claude/coding-agent-kit/{statusline,notify,check-git-hooks}.cjs` | `current/helpers/` |

- `--channel MAJOR.MINOR` selects the channel; the managed root remembers it.
  Without it, a first install takes the channel of the newest release. A
  different `--channel` switches to that channel's newest release, also to
  an older line. `0.6` is the first channel with managed installs.
- `--update` needs an existing managed install. It lists the repository's
  tags (`git ls-remote`, which writes nothing) and moves only to a newer
  patch of the remembered channel, or of the one `--channel` names. With
  nothing newer it writes nothing and prints one line,
  `coding-agent-kit: vX.Y.Z is already current (channel X.Y).` It never
  leaves the channel. A run without `--update` does the same, installs when
  nothing is installed yet and puts back a missing link.
- A run exports the release it chose into `releases/` and hands over to that
  release's own `install.sh --activate`. It checks every destination,
  renames `current` over to itself, keeps the release before it, removes
  older ones and places the links that are not yet right; a switch that
  changes no link prints only
  `coding-agent-kit: switched from vX.Y.Z to vX.Y.W (channel X.Y).` Once a
  release is installed, every bootstrap or checkout `install.sh` hands its
  run to `current/install.sh`, so only released code changes an install.
- `--reinstall` repairs an install whose release is broken, which every
  other run would be handed to: the bootstrap (or any `install.sh`) runs on
  its own, exports the newest patch of the channel again from a fresh clone,
  replaces the old clone and tree only once that worked, and hands that
  release's own `install.sh --activate` the switch:
  `curl -fsSL https://raw.githubusercontent.com/FridrichMethod/coding-agent-kit/main/install.sh | sh -s -- --reinstall`.
- A launcher resolves its links to its physical release directory, so a
  switch leaves a running session on the release it started with, which
  stays until the next switch.
- A destination is the managed install's own only when it is a symlink whose
  text starts with the managed root's physical path and has no `.` or `..`
  components, with the same refusals as a [checkout install](#from-a-checkout)
  below. Links of a checkout install, Stow's links and every other path are
  refused before anything changes; remove them, or run `--uninstall` from
  their checkout, first.
- The repository is `CODING_AGENT_KIT_REPO_URL` when set, otherwise the one
  the managed root was cloned from, otherwise this repository on GitHub.
- Only one run at a time holds the lock (`flock` on `.lock` through
  `python3`); another run exits 75 and changes nothing. Where the file
  system has no `flock`, as some NFS and Lustre mounts, the lock is the
  directory `.lock.d`, renamed into place with its owner (host, pid, time)
  and removed at exit; one left behind counts as gone when its pid is gone on
  that host or it is older than an hour. A managed root such a mount shares
  between hosts is safe from concurrent runs either way.
- Every git call that can reach the network gives up when a transfer stays
  below 1000 bytes/s for 60 s, ssh after 30 s without a connection or a
  minute without an answer (unless `GIT_SSH_COMMAND`, `GIT_SSH` or
  `core.sshCommand` says how to run ssh), and, where `timeout` exists, after
  300 s in all, so a stalled repository fails the run instead of keeping the
  lock.
- The remembered prefix must be an absolute directory; a run refuses
  another one. After taking the lock, a run removes the temporary links a
  killed run left beside its destinations, and it writes the remembered
  channel and prefix before it renames `current`.
- `--uninstall` removes the managed links and reports any other path it
  keeps, then `~/.claude/coding-agent-kit/` if it is empty; `--purge` also
  removes the managed root.
- Every child gets `/dev/null` as stdin and all work starts on the last line
  of `install.sh`, so a truncated download runs nothing.
- Exit 0 on success, `already current` included, 1 on a refusal or error, 2
  on a usage error, and 75 when another run holds the lock. Nothing is
  changed by a run that fails; a failed first install may leave the managed
  root with its clone and exported release, which the next run reuses.

When a run places a link, a managed install prints the zsh `fpath` line, the
helper commands in the home-relative form that
[Claude Code helpers](helpers.md#wiring-the-helpers) shows, and the `PATH`
line, as a checkout install does below.

## From a checkout

```sh
git clone https://github.com/FridrichMethod/coding-agent-kit.git
cd coding-agent-kit
./install.sh                   # or: sh install.sh, ./install.sh --prefix DIR
```

`install.sh [--prefix DIR] [--uninstall]` run from a checkout creates
absolute links into the checkout; `DIR` must be absolute and defaults to
`~/.local`:

| Link | Target |
| --- | --- |
| `DIR/bin/codex-as`, `DIR/bin/claude-as` | `bin/` |
| `${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-~/.local/share}/bash-completion}/completions/{codex-as,claude-as}` | `completions/bash/` |
| `DIR/share/zsh/site-functions/{_codex-as,_claude-as}` | `completions/zsh/` |

- It checks every destination first. A path is this checkout's own only when
  it is a symlink whose text starts with the checkout's physical path (as
  `pwd -P` prints it) and has no `.` or `..` components. That is the form
  `install.sh` writes, also when you run it through another spelling of the
  path. Any other existing path is refused with its name and what it is: a
  file, a directory, a link into another checkout, a dangling link, every
  relative link, whether Stow made it or you did, and a link you made
  through another spelling of the checkout's path, such as a symlinked
  directory above it or macOS `/var` for `/private/var`. Such a link reaches
  this checkout but is not its own; remove it by hand.
- A destination is refused too when a directory on its way, below `$HOME` or
  below the parent of `DIR`, is a relative symlink. That is the form Stow
  folding writes: a missing `~/.local/bin` becomes a link into another
  repository's package, and a new link there would land in that repository.
  Absolute directory links are accepted, and so is any link above both,
  such as macOS `/var`; `$HOME` itself and the links above it are accepted
  for every `DIR`. Stow with `--no-folding`, or create such a directory (for
  example `~/.local/bin`) before you stow.
- A destination whose nearest existing parent is not a writable directory is
  refused too, naming that parent. A refusal exits 1, and nothing changes.
- Otherwise it creates or replaces the links, so a second run is a no-op.
  `git pull` needs no reinstall.
- `--uninstall` removes only this checkout's own links and reports any other
  path it keeps; directories stay. It first refuses, exit 1 with nothing
  changed, when an own link is in a directory it cannot write, or when a
  directory on the way cannot be searched, so it cannot tell what is there.
  Give it the same `--prefix` and environment as the install.
- A link of this checkout whose target has moved or gone within it, as after
  a release that moved a file, is still its own: a reinstall replaces it and
  `--uninstall` removes it. A moved or deleted checkout leaves dangling links
  that count as foreign: remove them by hand, or run `--uninstall` from the
  old place first.
- Exit 0 on success, 1 on a refusal or error, 2 on a usage error. It never
  writes a configuration file and starts no login.

A checkout install then prints what it cannot do for you:

```
zsh: unless fpath has it, add this before compinit (with oh-my-zsh, before sourcing it):
  fpath=("/home/u/.local/share/zsh/site-functions" $fpath)
Claude Code helpers, as commands in settings.json:
  statusLine:             node "/home/u/src/coding-agent-kit/helpers/statusline.cjs"
  Notification hook:      node "/home/u/src/coding-agent-kit/helpers/notify.cjs"
  PreToolUse hook (Bash): node "/home/u/src/coding-agent-kit/helpers/check-git-hooks.cjs"
PATH: add /home/u/.local/bin
```

The `PATH` line appears only when `DIR/bin` is not on `PATH`.
[Claude Code helpers](helpers.md#wiring-the-helpers) shows how these
commands go into Claude Code's `settings.json`.

## Linking or copying the files

Instead of `install.sh` you can link or copy the files yourself. A launcher
follows its own symlinks (up to 40) to `bin/..` and runs `lib/<tool>/launch.py`
there, and `bin/..` has the shape of a `~/.local` prefix: link `bin/codex-as`
and `bin/claude-as` from any directory on `PATH`, or copy them and
`lib/{codex-as,claude-as,coding-agent-kit}/` into `PREFIX/bin` and
`PREFIX/lib`. Put the completions wherever your shell loads them; they call the
launchers by name. A launcher without its `lib/` reports
`incomplete install at ROOT (missing lib/<tool>/launch.py)` and exits 1, and
one without `docs/` beside its `bin/` points `--help` at the GitHub copy of
[docs/account-launchers.md](account-launchers.md).

To use the helpers from copied files, copy or link `helpers/*.cjs` where your
Claude Code settings name them; [Claude Code helpers](helpers.md#wiring-the-helpers)
shows the commands.
