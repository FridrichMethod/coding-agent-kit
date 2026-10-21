# Agent Guide

This repository is coding-agent-kit, a standalone Codex and Claude Code
multi-account wrapper: the `codex-as` and `claude-as` launchers and three
Claude Code helpers (status line, notifications, Git hook guard). It installs
on top of an existing `codex` or `claude` and never installs either. Users
install it with `install.sh` or by linking or copying its files;
[FridrichMethod/dotfiles](https://github.com/FridrichMethod/dotfiles) is one
consumer, a submodule whose files it links.

## Scope and layout

```
bin/codex-as, bin/claude-as      launchers (Bash, POSIX sh)
lib/codex-as/*.py                codex-as modules: launch, check, layout, handoff, usage
lib/claude-as/launch.py          claude-as module
lib/coding-agent-kit/config.py   the configuration loader both launchers share
lib/coding-agent-kit/accounts.py the account metadata cache both launchers share
completions/{bash,zsh}/          account-name completion for both launchers
helpers/*.cjs                    Claude Code helpers (Node)
install.sh                       links bin/ and completions/ into a prefix
docs/                            account-launchers.md, codex-as.md, configuration.md
tests/                           suites, fixtures, runners, COVERAGE.md
```

- `bin/..` has the shape of a `~/.local` prefix: each launcher resolves its own
  symlinks with a bounded loop (40 hops, `readlink` without `-f`, `cd -P`) to
  `bin/..` and runs `lib/<tool>/launch.py` there, so linked, copied and
  Stow-managed installs all work. Keep `lib/<tool>/` beside `bin/`, and load
  sibling modules by path from `os.path.realpath(__file__)`: `python3 -I` puts
  no script directory on `sys.path`.
- Track no symlinks, so a checkout works where links are unsupported.
- `--help` and a run without arguments are pure shell and work without
  `python3`. Every other Python entry checks the Python version first, then
  loads its modules inside the error handling, so a missing file prints
  `incomplete install at ROOT`, never a traceback.
- `lib/coding-agent-kit/config.py` is the only launcher or completion code
  that reads `HOME`, `XDG_STATE_HOME` or the configuration file (a test greps for other reads). It loads once per run into
  one frozen object that every consumer takes. Unknown keys warn and never
  fail, so a newer configuration file works with an older kit. See
  [docs/configuration.md](docs/configuration.md).
- Without a configuration file the launchers must keep passing the same
  arguments, environment, files and links as before; the golden test in
  `tests/test_config.py` compares no file with an explicit-defaults file.

## Public contract

`tests/contract.sh` pins what consumers build on, and runs in `run.sh` and as
a pre-commit hook:

- The public paths and their modes (`git ls-files -s`): the launchers, their
  `lib/` modules, `config.py` and `accounts.py`, the completions, the helpers, `install.sh` and
  the three docs. Consumers link these paths one by one (dotfiles links each
  file from its own tree), copied installs depend on the `bin/` plus `lib/`
  shape, and `--help` points at `docs/account-launchers.md`.
- Each file is a regular file in the working tree, executable where its mode
  says so, and the launchers and `install.sh` keep their `#!/bin/bash` or
  `#!/bin/sh` line.
- No tracked symlinks.
- The CI job names `Linux behavior and lint`, `macOS system Bash behavior` and
  `Windows native PowerShell behavior`, and no other job: dotfiles' bump gate
  requires those three check runs to be green on a release tag.
- `tests/claude-customizations.cjs` and `tests/fixtures/claude-settings.json`:
  dotfiles overlays its real `settings.json` on the fixture and reruns the test,
  which derives the helper directory from each fixture command.

The command-line surface is public too, guarded by the suites rather than
`contract.sh`: the help text (dotfiles tests grep its `Concurrent accounts`
line and its `Details and manual repair:` path), `--list-accounts`,
`--account-labels`, `--account-info NAME` (dotfiles' completion preview shows
it), `claude-as --set-label` and `--show-config` with their output, the
configuration file location, keys and defaults, the account metadata files in
`${XDG_STATE_HOME:-~/.local/state}/coding-agent-kit/accounts/` with their
fields, `install.sh` and its options, `CODING_AGENT_KIT_COLOR`, and the
`CODING_AGENT_KIT_USAGE_FILE` and `CODING_AGENT_KIT_TOKEN_ID` that `claude-as`
passes its child. Changing any of it, or anything `contract.sh` pins,
is a breaking change: it needs a minor release and a matching CHANGELOG entry.

## Required conventions

- Keep secrets and machine-specific absolute paths out of version control.
- Write this repository's documentation in English using printable ASCII, tabs and line breaks only. This includes README files, agent guides and docs. Use ASCII punctuation and diagrams; do not add Chinese text, emoji or decorative Unicode symbols. The native pre-commit pygrep hook `english-documentation` checks every documentation file in this repository.
- Develop in your own clone. A consumer's submodule checkout must stay clean,
  because an untracked file there can stop its updates; for the same reason
  tests and hooks leave nothing behind, and `.gitignore` covers editor, cache
  and venv files at any depth.

## Weight and dependencies

The kit runs on what a host already has and stays small enough to audit.

- At run time the launchers and `install.sh` need only POSIX `sh` or Bash with ordinary POSIX utilities, the Python 3.11+ standard library run as `python3 -I -B`, and the tool they wrap. The helpers need Node.js 22+ and nothing from npm. Nothing needs a pip package, virtual environment, compiler, network service or background process of its own. The kit never installs a tool.
- A missing optional tool narrows the feature; a missing required tool fails once with a message that names it, never with a traceback or a half-done run. For example, codex-as without `git` limits takeover to the launch directory.
- A wrapper forwards to the native tool instead of reimplementing it. It requires a minimum version, never an exact pin or an upper bound. Anything beyond the tool's documented CLI and files (internal socket paths, wire protocols, lock layouts) is best effort: a failure there warns and falls back to plain native behavior, and blocks or asks only where continuing could lose work.
- Keep helper code proportional to what it adds over the native tool. A feature that needs its own protocol client, journal or lock scheme must justify its code and test cost; prefer the native tool's own feature when one exists.
- Tests use the Python standard library (`unittest`), Node's built-in test runner, the shells under test and fakes for third-party CLIs, run in a temporary HOME, and reach no network beyond `127.0.0.1` fixtures. A test may use a real host tool (zsh, dash, GNU Stow, codex) when it is present and must skip cleanly when it is absent. Nothing in the suites needs Docker, the internet or real credentials. Tests never touch the real home, its credentials or running daemons.
- CI runs every pre-commit hook on every push, so keep hooks scoped by `files:` and fast: test parsing and other pure logic in process, and spawn real shells and child processes only for behavior that needs them (exec, signals, umask, TTY, file modes).

## Working commands

- Install into a prefix: `./install.sh [--prefix DIR]`; remove: `./install.sh --uninstall`
- Run the tests: `./tests/run.sh` (CI: `--ci`; Windows: `./tests/run.ps1`, CI: `-CI`)
- Run one suite: `tests/config.sh`, `tests/codex-as.sh`, `tests/account-enrollment.sh`, `tests/standalone-e2e.sh`, `tests/contract.sh`
- Run checks: `pre-commit run --all-files`

## Verification

After modifying any file, run `pre-commit run --all-files` to ensure changes pass CI checks before committing.

## Shell conventions

- Use Bash/Zsh-specific syntax only in matching shell files. `bin/claude-as` and `install.sh` are POSIX `sh` and run under dash.
- Follow pre-commit shell style:
  - Bash: `shfmt -i 4 -ci -ln bash`
  - POSIX: `shfmt -i 4 -ci -ln posix`
  - Zsh: `shfmt -i 4 -ci -ln zsh` (needs shfmt 3.13+, the first release with a zsh dialect)
- Launcher messages follow one colour policy in shell and Python: `CODING_AGENT_KIT_COLOR=auto|always|never` (unset or invalid means `auto`, styled only when the destination is a terminal), and a nonempty `NO_COLOR` or `TERM=dumb` disables color. Native CLI output, completion lists and the output of `--list-accounts`, `--account-labels`, `--account-info` and `--show-config` stay untouched, and redirected logs stay plain by default.

## Testing

- Every test entry point (`tests/*.sh`) starts with the one statement `unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR CODING_AGENT_KIT_USAGE_FILE CODING_AGENT_KIT_TOKEN_ID XDG_CONFIG_HOME XDG_STATE_HOME NO_COLOR`, and `tests/run.ps1` removes the same names in the same order. `tests/test-entrypoints.sh` checks each entry point against `tests/run.sh`, and that the statement names every `CODING_AGENT_KIT_*` variable (pattern `_?CODING_AGENT_KIT_[A-Z_]+`) in a tracked file outside `tests/`, `docs/`, `.github/` and Markdown, so a knob a new file reads goes on that list too. Python tests that pass the caller's environment on drop the same names in `setUp`.
- Behavior coverage is tracked as a source-to-test matrix in `tests/COVERAGE.md`; update it with the tests.

## Windows line endings

- `.gitattributes` pins `eol=lf` because Git for Windows enables `core.autocrlf` in its system config, and Git Bash fails on a CRLF `set -eu`. A superproject's attributes do not apply inside a submodule, so this repository carries its own. The Windows CI job fails when `git ls-files --eol` reports a `w/crlf` file.

## Workflows and checks

- Keep workflow definitions in `.github/workflows/*.yml`.
- Pin action versions and avoid unnecessary matrix complexity. Actions use major tags (for example `@v6`) and CI installs `pre-commit==4.6.2`.
- Keep CI aligned with `.pre-commit-config.yaml` tools:
  - `shellcheck`
  - `shfmt`
  - basic hygiene hooks
  - the `english-documentation` ASCII check
  - `gitleaks`, plus the manual `gitleaks-history` stage that CI runs over the full history
- CI runs `./tests/run.sh --ci` on Linux (zsh and GNU Stow installed; the test step fails if the folded Stow cases of `standalone-e2e.sh` did not run) and on macOS (system Bash 3.2, `/bin/sh`), and `./tests/run.ps1 -CI` on native Windows; each job fails if the tests or hooks leave an untracked file. The job names are part of the contract.
- If CI behavior affects contributors, update `README.md` in the same change.

## Releases and tags

- Versions are `vMAJOR.MINOR.PATCH` tags on `main`, with semantic versioning in the 0.x range: a minor release may break the public contract, a patch release must not. Every release gets a CHANGELOG section, and a breaking one lists its breaking changes and moved paths.
- Tags are immutable: never move or delete one. A bad release is fixed by a new patch tag.
- `main` is never force-pushed or rewritten, because a consumer's gitlink may point at any commit on it, and a host that cannot fetch that commit stops updating.
- dotfiles bumps its submodule daily to the newest tag of its pinned minor line once the three CI jobs are green on it, so a patch tag ships to every dotfiles host within a day. Tag a patch only from a green `main`, and run `tests/contract.sh` and the full suite first. A minor release reaches dotfiles only through a reviewed change there.
- History before 0.1.0 lives in dotfiles (up to 6fe50bc); the imported commits keep only this repository's files.
