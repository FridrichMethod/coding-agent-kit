# coding-agent-kit

Multi-account wrappers and helpers for Codex and Claude Code:

- `codex-as NAME [codex arguments...]` and
  `claude-as NAME [claude arguments...]` run the native CLI as a named
  account, with private stored credentials and the shared everyday
  configuration and history;
- three Claude Code helpers: a status line, terminal notifications and a Git
  hook guard.

It installs on top of an existing `codex` or `claude` and never installs,
updates or replaces either. Install it with `install.sh`, or link or copy its
files, for example from a Git submodule in a repository of your own.

## Requirements

- Linux or macOS. On native Windows only the Node helpers are tested.
- Python 3.11 or newer, standard library only (run as `python3 -I -B`).
- Bash for `codex-as` (3.2 is enough), POSIX `sh` for `claude-as` and
  `install.sh`, and ordinary POSIX utilities.
- Codex CLI 0.161.0 or newer for `codex-as`, with no upper limit. Claude Code
  for `claude-as`; its token capture is audited for 2.1.296, and other
  versions fall back to an explicit manual paste.
- Node.js 22 or newer for the helpers; Git is optional for the status line.
- Optional: bash-completion or zsh for account-name completion.

## Install

```sh
git clone https://github.com/FridrichMethod/coding-agent-kit.git
cd coding-agent-kit
./install.sh                   # or: sh install.sh, ./install.sh --prefix DIR
```

`install.sh [--prefix DIR] [--uninstall]` creates absolute links into the
checkout; `DIR` must be absolute and defaults to `~/.local`:

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

It then prints what it cannot do for you:

```
zsh: unless fpath has it, add this before compinit (with oh-my-zsh, before sourcing it):
  fpath=("/home/u/.local/share/zsh/site-functions" $fpath)
Claude Code helpers, as commands in settings.json:
  statusLine:             node "/home/u/src/coding-agent-kit/helpers/statusline.cjs"
  Notification hook:      node "/home/u/src/coding-agent-kit/helpers/notify.cjs"
  PreToolUse hook (Bash): node "/home/u/src/coding-agent-kit/helpers/check-git-hooks.cjs"
PATH: add /home/u/.local/bin
```

The `PATH` line appears only when `DIR/bin` is not on `PATH`. In Claude
Code's `settings.json`, the helpers go in as:

```json
{
  "statusLine": {
    "type": "command",
    "command": "node \"/home/u/src/coding-agent-kit/helpers/statusline.cjs\""
  },
  "hooks": {
    "Notification": [
      {
        "matcher": "permission_prompt|idle_prompt",
        "hooks": [{ "type": "command", "command": "node \"/home/u/src/coding-agent-kit/helpers/notify.cjs\"" }]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "node \"/home/u/src/coding-agent-kit/helpers/check-git-hooks.cjs\"" }]
      }
    ]
  }
}
```

A `settings.json` shared by machines whose home directories differ can name
the helpers relative to the home directory instead: copy or link
`helpers/*.cjs` into `~/.claude/coding-agent-kit/` and give each command in
this form, with `notify.cjs` and `check-git-hooks.cjs` for the two hooks:

```
node -e "require(require('os').homedir() + '/.claude/coding-agent-kit/statusline.cjs').main()"
```

Each helper exports `main`, which does what running the file does.

Instead of `install.sh` you can link or copy the files yourself. A launcher
follows its own symlinks (up to 40) to `bin/..` and runs `lib/<tool>/launch.py`
there, and `bin/..` has the shape of a `~/.local` prefix: link `bin/codex-as`
and `bin/claude-as` from any directory on `PATH`, or copy them and
`lib/{codex-as,claude-as,coding-agent-kit}/` into `PREFIX/bin` and
`PREFIX/lib`. Put the completions wherever your shell loads them; they call the
launchers by name. A launcher without its `lib/` reports
`incomplete install at ROOT (missing lib/<tool>/launch.py)` and exits 1, and
one without `docs/` beside its `bin/` points `--help` at the GitHub copy of
[docs/account-launchers.md](docs/account-launchers.md).

## Usage

```sh
codex-as work                         # Codex as account "work"
codex-as work resume --last
codex-as work exec --json "Check the tests"
claude-as personal -p "Check the tests"
codex-as --list-accounts              # account names, what completion offers
codex-as --account-info work          # last-seen email, plan and usage (offline)
claude-as --set-label personal "me@example.com"
claude-as --show-config               # configuration in effect
codex-as --help
```

`<codex_home>` and `<secrets_dir>` below are configurable and default to
`~/.codex` and `~/.secrets` (see [Configuration](#configuration)).

- Existing accounts launch immediately. A missing one lists the existing
  accounts and asks `[y/N]`; after native login in an isolated temporary
  configuration (Codex device login, Claude `setup-token`) it saves the
  credentials and continues the original command.
- `codex-as` keeps a fixed account view in `<codex_home>/accounts/NAME`, with
  credentials in `<secrets_dir>/codex-NAME.json` and the configuration,
  sessions, history and SQLite state shared from `<codex_home>`. Accounts and
  different sessions run concurrently, and interactive resume takes sessions
  over from the root daemon (the desktop app's backend). It forwards
  arguments unchanged and refuses only what would change, revoke or bypass the
  account's login, daemon-only commands, session names and settings that would
  split the shared state. The root `<codex_home>/auth.json` stays native;
  `codex-as` never reads or changes it.
- `claude-as` starts Claude Code with the `claude setup-token` OAuth token from
  `<secrets_dir>/claude-NAME` and otherwise inherits the existing
  configuration.
- Apart from `--help`, `--list-accounts`, `--account-labels` and
  `--show-config`, each accepted only alone, `--account-info NAME` and
  `claude-as --set-label NAME [LABEL]`, the launchers have no options of their
  own.
- Each launcher keeps a small metadata cache per account in
  `${XDG_STATE_HOME:-~/.local/state}/coding-agent-kit/accounts/`: the Codex
  email and plan from the account's token and the last rate limits a run saw
  in its own rollouts, and the Claude label asked at enrolment and the last
  rate limits the status line helper saw. `--account-info NAME` prints it
  offline, without opening a credential, and the zsh completions show the
  email or label as each account's description. Emails show in previews and
  transcripts; no token is ever stored there.

[docs/account-launchers.md](docs/account-launchers.md) covers the account
layout, adding accounts, completion, safety boundaries and manual repair;
[docs/codex-as.md](docs/codex-as.md) the Codex argument rules, daemon
takeover, version compatibility and the remaining differences from native
Codex.

## Configuration

The launchers read one optional TOML file:
`CODING_AGENT_KIT_CONFIG` if set, otherwise
`${XDG_CONFIG_HOME:-~/.config}/coding-agent-kit/config.toml`. Without it they
use the defaults:

```toml
codex = "codex"            # executable: a name looked up on PATH, or an absolute or ~/ path
claude = "claude"
codex_home = "~/.codex"    # accounts live in codex_home/accounts
secrets_dir = "~/.secrets"
```

The file, and the directories and symlinks on its way, must be owned by you
(or root) and not writable by others; unknown keys only warn. Choose `codex_home` and `secrets_dir` before the first
enrolment, and after moving `codex_home` export `CODEX_HOME` for plain
`codex`. `--show-config` prints the values in effect.
[docs/configuration.md](docs/configuration.md) has every rule and message.

## Claude Code helpers

- **Status line:** shows the active model, reported effort, project and Git branch (or detached commit), context usage, and estimated session cost. For example: `Opus | xhigh | coding-agent-kit (main) | ctx 38% | ~$1.23`. Missing usage/cost data shows `?`; cost is Claude's estimate, not an invoice. It performs only bounded Git ref reads, without scanning the worktree or contacting the network. See the [status-line reference](https://code.claude.com/docs/en/statusline).
- **Notifications:** `permission_prompt` and `idle_prompt` events emit [Claude's `terminalSequence`](https://code.claude.com/docs/en/hooks#emit-terminal-notifications), using OSC 99 for Kitty, OSC 777 for Ghostty/Warp/rxvt, or OSC 9 for iTerm2/WezTerm/Windows Terminal. The terminal handles desktop delivery, including remote sessions when terminal identification is available. Enable notifications for the terminal in OS settings; unsupported terminals and non-interactive sessions may ignore them. The notice contains only the project name and a fixed status, never prompt or command contents. Other events and notification types produce nothing.
- **Git hook checks:** a local `PreToolUse` helper replaces per-Bash `npx block-no-verify@1.1.2` execution. It blocks common `--no-verify`, commit `-n`, and `-c core.hooksPath=...` forms without npm startup or downloads. This is a conservative text check, not a shell parser; quoted text can trigger it, and indirect bypasses are outside its scope. Successful checks do not grant permission or skip the classifier.

## Colour

The launchers style only their own messages: headings cyan, questions and
warnings yellow, saves green, errors red. Native Codex and Claude output,
completion lists and the output of `--list-accounts`, `--account-labels`,
`--account-info` and `--show-config` are never styled.

- `CODING_AGENT_KIT_COLOR=auto` (also when unset or invalid) styles a message
  only when its destination is a terminal, so redirected logs stay plain;
  `always` and `never` force it.
- A non-empty `NO_COLOR` or `TERM=dumb` turns colour off, even with `always`.

## Tests

```sh
./tests/run.sh             # CI: ./tests/run.sh --ci
pre-commit run --all-files
```

```powershell
./tests/run.ps1            # CI: ./tests/run.ps1 -CI
```

- `run.sh` needs bash, sh, git, python3 and node. Without zsh it skips the zsh
  cases and says so; `--ci` requires zsh. Without `codex` on `PATH` it skips
  the native Codex sharing checks, and without GNU Stow the folded Stow cases
  of `standalone-e2e.sh`, and says so; as root it skips the read-only
  directory cases.
- `run.ps1` runs the Node helper suite; `-CI` requires Windows.
- Every suite runs in a temporary HOME with fake CLIs and fictitious
  credentials, reaches no network beyond local fixtures, and first unsets
  `CODING_AGENT_KIT_CONFIG`, `CODING_AGENT_KIT_COLOR`,
  `CODING_AGENT_KIT_USAGE_FILE`, `CODING_AGENT_KIT_TOKEN_ID`,
  `CODING_AGENT_KIT_CLAUDE_PID`,
  `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `NO_COLOR`.
- `tests/standalone-e2e.sh` installs with `install.sh` under `/bin/sh`, bash and
  (when present) dash, checks its refusals and drives both launchers and their
  completions through the links;
  `tests/contract.sh` checks the public contract (see
  [AGENTS.md](AGENTS.md)).
- CI runs the Unix suite on Linux and on macOS with the system Bash 3.2, the
  Node suite on Windows, and every pre-commit hook on Linux. The Linux job
  installs GNU Stow and fails if the folded Stow cases did not run; macOS
  skips them. It runs on pull requests, on pushes to `main` and on pushes to
  `ci/**` branches, so a change can be checked on all three before `main` is
  fast-forwarded to it.
  [tests/COVERAGE.md](tests/COVERAGE.md) maps each source file to its tests.

## Versioning

Releases follow semantic versioning within 0.x. A minor release (0.2.0 to
0.3.0) may break the contract, the command-line surface or the defaults; a
patch release (0.2.0 to 0.2.1) must not. Tags are never moved and `main` is
never rewritten, so a fix ships as a new patch tag.

Consumers may take patch tags automatically, for example with a scheduled job
that moves a submodule to the newest tag of a pinned minor line. A patch is
therefore tagged only from a green `main` and never changes a public
interface: the linked paths, the help text, the `--account-labels` and
`--account-info` output, or anything else in the contract in
[AGENTS.md](AGENTS.md#public-contract). Take a minor release through a
reviewed change. [CHANGELOG.md](CHANGELOG.md) lists every release.

## License

[MIT](LICENSE) - see the file for the full text.
