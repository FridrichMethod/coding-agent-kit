# coding-agent-kit

The Codex and Claude Code tooling of
[FridrichMethod/dotfiles](https://github.com/FridrichMethod/dotfiles), kept as
its own repository:

- `codex-as` and `claude-as`, account launchers that run the native CLI as a
  named account;
- the AI config sync engine `lib/config_sync.py` behind `codex-config-sync`,
  `claude-settings-sync`, `codex-rules-sync` and `codex-instructions-sync`;
- the Claude Code status line, notification and Git-hook helpers.

Dotfiles consumes it as the submodule `common/codex/coding-agent-kit`. The
settings data the helpers apply (the portable Claude `settings.json`, the Codex
`config.toml` and `portable.rules`, and the global instruction files) stays in
dotfiles. Version 0.1.0 is the dotfiles code at 6fe50bc, moved with no behavior
change; see [CHANGELOG.md](CHANGELOG.md).

## Requirements

- POSIX `sh` or Bash with ordinary POSIX utilities.
- Python 3.11+ standard library, run as `python3 -I -B`.
- The pinned `tomlkit` in [`requirements-sync.txt`](requirements-sync.txt),
  only for TOML sync and the full runtime check, provisioned by
  `./setup-sync.sh` (Windows: `./setup-sync.ps1`).
- Node.js 22+ for the `.cjs` Claude helpers.
- The tool a launcher wraps: Codex CLI 0.161.0 or newer for `codex-as`, Claude
  Code for `claude-as`.

## Contract

### E1: delivered files

Dotfiles links each of these files from the same path in its own tree, one
relative link per file. Their paths and modes are the interface:

| Path | Mode |
| --- | --- |
| `common/claude/.claude/dotfiles/check-git-hooks.cjs` | 100644 |
| `common/claude/.claude/dotfiles/notify.cjs` | 100644 |
| `common/claude/.claude/dotfiles/statusline.cjs` | 100644 |
| `common/claude/.local/bin/claude-as` | 100755 |
| `common/claude/.local/lib/claude-as/launch.py` | 100644 |
| `common/claude/.local/share/bash-completion/completions/claude-as` | 100644 |
| `common/claude/.oh-my-zsh/custom/completions/_claude-as` | 100644 |
| `common/codex/.local/bin/codex-as` | 100755 |
| `common/codex/.local/lib/codex-as/check.py` | 100644 |
| `common/codex/.local/lib/codex-as/handoff.py` | 100644 |
| `common/codex/.local/lib/codex-as/launch.py` | 100644 |
| `common/codex/.local/lib/codex-as/layout.py` | 100644 |
| `common/codex/.local/share/bash-completion/completions/codex-as` | 100644 |
| `common/codex/.oh-my-zsh/custom/completions/_codex-as` | 100644 |

The engine `lib/config_sync.py` is canonical here; dotfiles calls it at
`common/codex/coding-agent-kit/lib/config_sync.py`. The binding (the four
`*-sync` launchers, `lib/sync-runtime.sh`, `setup-sync.sh`, `setup-sync.ps1`
and `requirements-sync.txt`) is a reference twin: dotfiles keeps its own
byte-identical copy, apart from the engine path, and its delivery test refuses
drift, including in the `tomlkit` pin.

### E2: helper CLI

```text
codex-config-sync [--quiet] [--check | --migrate-portable] [--hooks EVENTS.json] PORTABLE LIVE
claude-settings-sync [--quiet] [--check] [--hooks EVENTS.json] PORTABLE LIVE
codex-rules-sync [--quiet] [--check] PORTABLE LIVE
codex-instructions-sync [--quiet] [--check] [--append RULES] PORTABLE LIVE
```

Every path is an argument; no helper infers a home or a dotfiles checkout.
`--check` is a read-only preflight, `--quiet` hides only successful chatter,
and every helper fails closed. See [docs/config-sync.md](docs/config-sync.md)
for merge ownership, the instructions overlay, migration and file safety.

### E3: runtime lookup

A launcher resolves its own symlink chain and takes the checkout root four
levels above the directory of the real launcher file. It runs the engine with
`DOTFILES_SYNC_PYTHON` when that is set (an invalid value fails, with no
fallback), and otherwise with `<root>/.venv-sync/bin/python`, or
`<root>/.venv-sync/Scripts/python.exe` under Git Bash. Python runs as
`-I -B -X utf8`. It never searches for a system Python.

### E4: provisioning

`./setup-sync.sh [--python PYTHON]` (Windows:
`./setup-sync.ps1 [-Python PYTHON]`) creates the ignored, checkout-local
`.venv-sync/`, installs only the hash-pinned `tomlkit` wheel and runs
`lib/config_sync.py --runtime-check`. Profiles, installers and automatic
updates never install anything; a missing or outdated runtime fails with a
request to rerun setup.

### E5: frozen strings

These strings keep their bytes, because dotfiles or existing hosts depend on
them:

- `INSTRUCTIONS_MARKER`, the first line and ownership key of every generated
  `~/.codex/AGENTS.override.md`;
- `SUBMODULE_HINT` (`git submodule update --init --recursive`), the
  foreign-instructions refusal texts and the `(current)`/`(stale)` suffixes of
  `codex-instructions-sync --check`, which the dotfiles doctor parses;
- the setup-sync hints (`run setup-sync.sh or setup-sync.ps1`,
  `rerun setup-sync explicitly`);
- the `--help` pointer `~/dotfiles/docs/account-launchers.md` of both
  launchers;
- the knob names `DOTFILES_COLOR` and `DOTFILES_SYNC_PYTHON`.

## Why the paths mirror Stow packages

Each file sits at the path it had in dotfiles. The `*-sync` launchers find the
checkout root at a fixed depth, every test's repository-root arithmetic and
every pre-commit file pattern keep working, and the imported history needs no
renames. Dotfiles can then link each delivered file from the same package path,
so every stowed `$HOME` link keeps its exact text.

## Account launchers

`codex-as NAME [ARGS...]` runs Codex with a fixed `~/.codex/accounts/NAME`
view, private credentials in untracked `~/.secrets/codex-NAME.json`, and shared
config, session and SQLite state; its Python standard-library helpers live in
`~/.local/lib/codex-as/`. `claude-as ACCOUNT [ARGS...]` runs Claude Code with
the `claude setup-token` OAuth token kept untracked in
`~/.secrets/claude-ACCOUNT`.

Existing accounts launch immediately. Missing accounts ask `[y/N]`, run native login in an isolated temporary configuration, save private credentials, then continue the original command. zsh/fzf-tab and Bash complete account names without reading tokens.
Launcher messages use terminal-aware colors; redirected logs stay plain by default. `DOTFILES_COLOR=auto|always|never`, `NO_COLOR` and `TERM=dumb` follow the existing dotfiles convention; native CLI output and completion lists remain untouched.
Both require Python 3.11+. Codex requires CLI 0.161.0 or newer and forces file authentication.
Apart from `--help`, `codex-as` has no options of its own: `codex-as NAME ARGS...` runs `codex ARGS...` as NAME,
refusing what would change, revoke or bypass that login, daemon-only commands, session names,
and settings that would split the shared state.
The root `~/.codex/auth.json` stays native; `codex-as` never reads or changes it, so root
login/logout and desktop clients keep their own token chain.
Accounts and different sessions can run concurrently and share history. Resume by UUID,
`--last` or picker; explicit session names are refused. Account leases exclude credential
maintenance while wrapper clients are active, and native thread locks exclude two writers
to the same session. Same-account OAuth refresh retains native cross-process limitations.
Interactive resume takes sessions over from the root daemon (Desktop's backend): an
idle `codex-as NAME resume UUID` without a prompt, one that may be running after a
confirmation, for `--last` only the session it opens when idle, and before the picker the
idle sessions it lists by default (this directory or the same place in a linked worktree;
all with `--all`).
Takeover ends the old runtime and spawned agents there, restores saved archive
visibility, then resumes with the selected account. Interrupted takeovers keep a private
recovery record that the next `resume`, `fork` or `exec resume` of that UUID, picker or
`resume --last` finishes. Exec, fork and sessions held by other
processes are never taken over.
Ambiguous credential divergence stops with both copies kept and a manual relink command; shared config paths
must be absolute, while cloud policy, remote plugin caches and auth-bound integrations remain isolated.
See [account layout, new accounts, completion and safety boundaries](docs/account-launchers.md), and [Codex source verification](docs/codex-as.md). Installing does not start login or switch real accounts.

## Claude helpers

The portable dotfiles `settings.json` wires `~/.claude/dotfiles/*.cjs` into
Claude Code.

Claude's local helpers require Node.js 22 or newer on `PATH` (18 and 20 are end of life; Git is optional for the status line):

- **Status line:** shows the active model, reported effort, project and Git branch (or detached commit), context usage, and estimated session cost. For example: `Opus | xhigh | dotfiles (main) | ctx 38% | ~$1.23`. Missing usage/cost data shows `?`; cost is Claude's estimate, not an invoice. It performs only bounded Git ref reads, without scanning the worktree or contacting the network. See the [status-line reference](https://code.claude.com/docs/en/statusline).
- **Notifications:** `permission_prompt` and `idle_prompt` events emit [Claude's `terminalSequence`](https://code.claude.com/docs/en/hooks#emit-terminal-notifications), using OSC 99 for Kitty, OSC 777 for Ghostty/Warp/rxvt, or OSC 9 for iTerm2/WezTerm/Windows Terminal. The terminal handles desktop delivery, including remote sessions when terminal identification is available. Enable notifications for the terminal in OS settings; unsupported terminals and non-interactive sessions may ignore them. The notice contains only the project name and a fixed status, never prompt or command contents. The plugin's existing Stop notification remains separate.
- **Git hook checks:** a local `PreToolUse` helper replaces per-Bash `npx block-no-verify@1.1.2` execution. It blocks common `--no-verify`, commit `-n`, and `-c core.hooksPath=...` forms without npm startup or downloads. This is a conservative text check, not a shell parser; quoted text can trigger it, and indirect bypasses are outside its scope. Successful checks do not grant permission or skip the classifier.

## Consuming it from dotfiles

Dotfiles carries this repository as the https submodule
`common/codex/coding-agent-kit`, following `main`. The checkout sits inside
the `codex` Stow package and is kept out of `$HOME` by one `.stowrc` line,
`--ignore=^coding-agent-kit(/|$)`. A package-local `.stow-local-ignore` would
replace Stow's built-in ignore list for the whole package.

`common/claude` and `common/codex` in dotfiles hold one relative symlink per
delivered file, never a directory link, for example
`common/codex/.local/bin/codex-as ->
../../../codex/coding-agent-kit/common/codex/.local/bin/codex-as`. The package
paths are unchanged, so Stow links `$HOME` to them exactly as before and
resolution takes one more hop.

A change here reaches hosts only through the dotfiles bump workflow
`update-coding-agent-kit-submodule.yml`, which runs on manual dispatch, checks
the new commit and pushes a review branch; the dotfiles pull request then runs
the full dotfiles CI. Develop in a separate clone: an untracked file inside a
host's submodule checkout stops that host's automatic update.

## Tests

```sh
./setup-sync.sh
./tests/run.sh             # CI: ./tests/run.sh --ci
pre-commit run --all-files
```

```powershell
./setup-sync.ps1
./tests/run.ps1            # CI: ./tests/run.ps1 -CI
```

The suites read frozen copies of the dotfiles settings in `tests/fixtures/`.
Dotfiles replaces `codex-config.toml`, `claude-settings.json` and
`portable.rules` there with its real files and reruns the engine suites, so
those names are an interface. CI runs the Unix suite on Linux and macOS, the
native suite on Windows, and every pre-commit hook on Linux.
[tests/COVERAGE.md](tests/COVERAGE.md) maps each source file to the tests that
cover it.

## Versioning

Releases follow semantic versioning in the 0.x range. `v0.1.0` is the first
commit that dotfiles pins; a tag is never moved, so a later fix becomes
v0.1.1. `main` is never force-pushed or rewritten, because a dotfiles gitlink
may point at any commit on it. Changes are listed in
[CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) - see the file for the full text.
