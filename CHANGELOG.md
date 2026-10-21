# Changelog

## 0.2.0

coding-agent-kit becomes a standalone install. The launchers find their own
files, an optional configuration file replaces the hard-coded paths and
executables, and `install.sh` links the commands. Without a configuration file
`codex-as` and `claude-as` pass the same arguments, environment, files and
links to Codex and Claude Code as in 0.1.0.

Added:

- `install.sh [--prefix DIR] [--uninstall]` links `bin/codex-as` and
  `bin/claude-as` into `DIR/bin` (default `~/.local`), the Bash completions into
  `${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-~/.local/share}/bash-completion}/completions`
  and the zsh completions into `DIR/share/zsh/site-functions`. It replaces or
  removes only its own links, whose text starts with the checkout's physical
  path as `pwd -P` prints it (also after their target moved within it); a
  link made through another spelling of that path is not its own. Before
  changing anything it refuses every other existing path, including relative
  links such as Stow's, any destination reached through a relative directory
  link below `$HOME` or the parent of `DIR` (a Stow fold or a hand-made
  relative alias; absolute aliases, `$HOME` itself and the links above it
  are accepted), and any destination whose nearest existing parent is not a
  writable directory. `--uninstall` changes nothing when one of its own links
  is in a directory it cannot write or a directory on the way cannot be
  searched. It prints the zsh `fpath` line and the Claude Code helper
  commands, and never writes a configuration file.
- An optional configuration file, `CODING_AGENT_KIT_CONFIG` or
  `${XDG_CONFIG_HOME:-~/.config}/coding-agent-kit/config.toml`, with the keys
  `codex`, `claude`, `codex_home` (default `~/.codex`) and `secrets_dir`
  (default `~/.secrets`). It must be owned by you or root and not writable by
  others (group-writable only for your user private group, which is your
  login group in the password database, also in a `newgrp` or `sg` shell,
  and then without an access ACL; on NFSv4 every group-writable file or
  directory counts as having one), and so must the directories on its way,
  up to `$HOME` (or its resolved path) or `/`; a symlink on its way must be
  yours or root's. Values with control characters (C0, DEL and C1) are
  refused; unknown keys only warn. See `docs/configuration.md`.
- `codex-as --show-config` and `claude-as --show-config` print the effective
  values and where they came from. `codex-as --list-accounts` prints the
  account names, like `claude-as --list-accounts`. Neither starts a native CLI.
- The launchers resolve their own symlinks (at most 40) to the kit root and run
  `lib/<tool>/launch.py` there, so linked, copied and Stow-managed installs all
  work. An incomplete install prints `incomplete install at ROOT` instead of a
  traceback.
- Tests: `tests/test_config.py` (runner `tests/config.sh`) for the loader and
  the launchers' location, `tests/standalone-e2e.sh` for an install from a
  plain clone, and `tests/contract.sh` for the public contract.

Changed:

- `--help` names the configurable `<secrets_dir>` and `<codex_home>` with their
  defaults, adds a `Configuration:` line, and ends with
  `Details and manual repair: ROOT/docs/account-launchers.md`, or with the
  GitHub URL of that file when the launcher has no kit around it.
- With a configured `codex_home`, root help, version, `update` and
  `completion` run with `CODEX_HOME=<codex_home>`, and an inherited `CODEX_HOME`
  equal to it is accepted.
- `codex-as` checks the parents of `codex_home` and of `secrets_dir`,
  `claude-as` the parent of `secrets_dir`. Both are `$HOME` by default, checked
  as before. A missing parent, `$HOME` included, or one below a regular file,
  is now named (`PARENT does not exist; ...`) instead of the generic
  `missing, unreadable or malformed ...` message.
- The configuration location is looked up even without a file. A missing file,
  or a path component that is a regular file, means the defaults; any other
  failure to open it (an unreadable file, an unsearchable `~/.config` or
  `$XDG_CONFIG_HOME`, a symlink loop) is an error, so it stops every launch,
  `--list-accounts` and completion, where 0.1.0 never looked there.
- `claude-as` catches the same errors at the top level as `codex-as`, so none
  ends in a traceback; for example `claude-as NAME --settings ~unknown/x` now
  prints the one-line `missing, unreadable or malformed ...` message.
- An unset or relative `HOME` fails with `TOOL: HOME must be set to an
  absolute path.`, for `--list-accounts` too.
- `claude-as` with a missing `lib/claude-as/launch.py` prints the
  incomplete-install message and exits 1 (it exited 2 with Python's error).
- `tests/run.sh --ci` requires zsh, and the Linux CI job installs it and GNU
  Stow and fails if the folded Stow cases of `tests/standalone-e2e.sh` did
  not run. The Windows job no longer sets up Python.
- `tests/claude-customizations.cjs` takes the helper directory from each
  fixture command instead of assuming `~/.claude/dotfiles`.

Breaking changes:

- Every launcher, library, completion and helper file moved; see the table
  below. A consumer that links files must use the new paths.
- `DOTFILES_COLOR` is no longer read. Use
  `CODING_AGENT_KIT_COLOR=auto|always|never`; `NO_COLOR` and `TERM=dumb` work
  as before.
- The launchers no longer run `~/.local/lib/<tool>/`. They run `lib/<tool>/`
  beside their own `bin/` after resolving symlinks, so a copied launcher needs
  `PREFIX/lib/{codex-as,claude-as,coding-agent-kit}/` beside `PREFIX/bin/`.
- The `--help` pointer `~/dotfiles/docs/account-launchers.md` is gone (see
  Changed).
- The completions get account names from `codex-as --list-accounts` and
  `claude-as --list-accounts`, so the launchers must be on `PATH`. The old
  listing `check.py accounts` still works for shells opened before an update.
- `codex-as --list-accounts` and `codex-as --show-config` are no longer
  refused as invalid account names, and `claude-as --show-config` is no longer
  taken as an account name.
- `claude-as` refuses account names that start with `-`, as `codex-as` does:
  `claude-as -x`, `claude-as -h` or `claude-as --version` prints `claude-as:
  invalid account name (ASCII letters, digits, _ or -; no leading -).` and
  exits 1 instead of offering to enrol an account of that name. Every invalid
  name now gets this message, which `codex-as` already printed.
- The AI config sync engine moved back to dotfiles: `lib/config_sync.py`, the
  four `*-sync` launchers, `lib/sync-runtime.sh`, `setup-sync.*`,
  `requirements-sync.txt`, their suites and fixtures and `docs/config-sync.md`
  are removed. The kit needs no `tomlkit`, only the Python 3.11+ standard
  library and Node.js 22+.
- The test entry points unset `CODING_AGENT_KIT_CONFIG`,
  `CODING_AGENT_KIT_COLOR`, `XDG_CONFIG_HOME` and `NO_COLOR` instead of the
  dotfiles knobs, and the knob-hygiene check covers `CODING_AGENT_KIT_*` names.

Paths, 0.1.0 to 0.2.0:

| 0.1.0 | 0.2.0 |
| --- | --- |
| `common/codex/.local/bin/codex-as` | `bin/codex-as` |
| `common/claude/.local/bin/claude-as` | `bin/claude-as` |
| `common/codex/.local/lib/codex-as/launch.py` | `lib/codex-as/launch.py` |
| `common/codex/.local/lib/codex-as/check.py` | `lib/codex-as/check.py` |
| `common/codex/.local/lib/codex-as/layout.py` | `lib/codex-as/layout.py` |
| `common/codex/.local/lib/codex-as/handoff.py` | `lib/codex-as/handoff.py` |
| `common/claude/.local/lib/claude-as/launch.py` | `lib/claude-as/launch.py` |
| `common/codex/.local/share/bash-completion/completions/codex-as` | `completions/bash/codex-as` |
| `common/claude/.local/share/bash-completion/completions/claude-as` | `completions/bash/claude-as` |
| `common/codex/.oh-my-zsh/custom/completions/_codex-as` | `completions/zsh/_codex-as` |
| `common/claude/.oh-my-zsh/custom/completions/_claude-as` | `completions/zsh/_claude-as` |
| `common/claude/.claude/dotfiles/statusline.cjs` | `helpers/statusline.cjs` |
| `common/claude/.claude/dotfiles/notify.cjs` | `helpers/notify.cjs` |
| `common/claude/.claude/dotfiles/check-git-hooks.cjs` | `helpers/check-git-hooks.cjs` |
| `common/claude/.local/bin/claude-settings-sync` | removed |
| `common/codex/.local/bin/codex-config-sync` | removed |
| `common/codex/.local/bin/codex-instructions-sync` | removed |
| `common/codex/.local/bin/codex-rules-sync` | removed |
| `lib/config_sync.py` | removed |
| `lib/sync-runtime.sh` | removed |
| `setup-sync.sh`, `setup-sync.ps1` | removed |
| `requirements-sync.txt` | removed |
| `docs/config-sync.md` | removed |
| `tests/test_config_sync.py`, `tests/config-sync.sh`, `tests/codex-config-sync.sh`, `tests/ai-config-sync.sh`, `tests/inspect_codex_hooks.py` | removed |
| `tests/fixtures/codex-config.toml`, `tests/fixtures/portable.rules` | removed |
| (new) | `lib/coding-agent-kit/config.py`, `install.sh`, `docs/configuration.md`, `tests/config.sh`, `tests/test_config.py`, `tests/contract.sh`, `tests/standalone-e2e.sh` |

Every other 0.1.0 path is unchanged: `.gitattributes`,
`.github/workflows/ci.yml`, `.gitignore`, `.gitleaks.toml`,
`.pre-commit-config.yaml`, `AGENTS.md`, `CHANGELOG.md`, `CLAUDE.md`,
`LICENSE`, `README.md`, `docs/account-launchers.md`, `docs/codex-as.md`,
`tests/COVERAGE.md`, `tests/account-enrollment.sh`,
`tests/claude-customizations.cjs`, `tests/codex-as.sh`,
`tests/fixtures/bash_aliases`, `tests/fixtures/claude-settings.json`,
`tests/fixtures/zsh_aliases`, `tests/run.ps1`, `tests/run.sh`,
`tests/test-entrypoints.sh`, `tests/test_account_enrollment.py`,
`tests/test_codex_as.py` and `tests/test_codex_sharing_native.py`.

## 0.1.0

- Extracted from dotfiles 6fe50bc, no behavior change. The `codex-as` and
  `claude-as` launchers, the AI config sync engine `lib/config_sync.py` and
  the Claude status line, notification and Git-hook helpers keep their
  dotfiles paths and bytes. The `*-sync` launchers, `lib/sync-runtime.sh`,
  `setup-sync.*` and `requirements-sync.txt` are reference twins of the
  dotfiles binding: byte-identical, except that the dotfiles
  `lib/sync-runtime.sh` and `setup-sync.*` name the engine inside the
  submodule. The imported history keeps the commits that touched these files.
- The moved suites read frozen copies of the dotfiles settings in
  `tests/fixtures/`; the tests of dotfiles content stay in dotfiles.
  `docs/config-sync.md` keeps the helper half of the dotfiles guide.
