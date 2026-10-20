# Agent Guide

This repository is the Codex and Claude Code tooling behind
[FridrichMethod/dotfiles](https://github.com/FridrichMethod/dotfiles): the
`codex-as` and `claude-as` account launchers and the Claude status line,
notification and Git-hook helpers. Dotfiles consumes it as the submodule
`common/codex/coding-agent-kit`. The settings data (the portable Claude
`settings.json`, the Codex `config.toml` and `portable.rules`, and the global
instruction files) and the AI config sync engine that applies it stay in
dotfiles.

## Scope and layout

- Paths mirror the dotfiles Stow packages. The files under `common/` that
  dotfiles delivers are the contract: dotfiles links these 14 files one by one,
  with relative links (the three `common/claude/.claude/dotfiles/*.cjs`
  helpers, `claude-as`, `codex-as`, their `.local/lib` modules and their Bash
  and zsh completions). Renaming, removing or changing the mode of one needs a
  coordinated dotfiles change.
- Track no symlinks, so a Windows checkout needs no `core.symlinks`.
- Frozen output contract: the `--help` pointer
  `~/dotfiles/docs/account-launchers.md` and the `DOTFILES_COLOR` knob name
  keep their bytes.
- Develop in a separate clone, never inside a dotfiles checkout's submodule:
  any untracked file there stops that host's automatic update. `.gitignore`
  covers editor, cache and venv files at any depth for the same reason.
- `tests/fixtures/claude-settings.json` is an interface: dotfiles replaces it
  with its real data and reruns `claude-customizations.cjs`. Keep the name, and
  keep the suites reading only the fixtures.

## Required conventions

- Keep secrets and machine-specific absolute paths out of version control.
- Write this repository's documentation in English using printable ASCII, tabs and line breaks only. This includes README files, agent guides and docs. Use ASCII punctuation and diagrams; do not add Chinese text, emoji or decorative Unicode symbols. The native pre-commit pygrep hook `english-documentation` checks every documentation file in this repository.

## Weight and dependencies

These files are installed on every host, including fresh machines and clusters, so they must run on what a host already has and stay small enough to audit.

- At run time a stowed helper needs only POSIX `sh` or Bash with ordinary POSIX utilities, the Python 3.11+ standard library run as `python3 -I -B`, and the tool it wraps. It never needs a pip package, virtual environment, Node, compiler, network service or background process of its own. The one exception is Node.js 22+, which the `.cjs` Claude helpers need. Installing tools stays with `setup-host` (see Bootstrap and doctor).
- A missing optional tool narrows the feature; a missing required tool fails once with a message that names it, never with a traceback or a half-done run. For example, codex-as without `git` limits takeover to the launch directory.
- A wrapper forwards to the native tool instead of reimplementing it. It requires a minimum version, never an exact pin or an upper bound. Anything beyond the tool's documented CLI and files (internal socket paths, wire protocols, lock layouts) is best effort: a failure there warns and falls back to plain native behavior, and blocks or asks only where continuing could lose work.
- Keep helper code proportional to what it adds over the native tool. A feature that needs its own protocol client, journal or lock scheme must justify its code and test cost; prefer the native tool's own feature when one exists.
- Tests use the Python standard library (`unittest`), the shells under test and fakes for third-party CLIs, run in a temporary HOME, and reach no network beyond `127.0.0.1` fixtures. A test may use a real host tool (zsh, codex) when it is present and must skip cleanly when it is absent; anything that needs Docker, the internet or real credentials is opt-in, like `tests/e2e/run.sh`. Tests never touch the real home, its credentials or running daemons.
- CI runs every pre-commit hook on every push, so keep hooks scoped by `files:` and fast: test parsing and other pure logic in process, and spawn real shells and child processes only for behavior that needs them (exec, signals, umask, TTY, file modes).

## Bootstrap and doctor

- Installing tools (`setup-host`), the doctor and the opt-in Docker e2e suite (`tests/e2e/run.sh`) live in the consuming dotfiles repository; nothing here installs a tool.

## Working commands

- Run the tests: `./tests/run.sh` (CI: `--ci`; Windows: `./tests/run.ps1`, CI: `-CI`)
- Run checks: `pre-commit run --all-files`

## Verification

After modifying any file, run `pre-commit run --all-files` to ensure changes pass CI checks before committing.

## Shell conventions

- Use Bash/Zsh-specific syntax only in matching shell files.
- Follow pre-commit shell style:
  - Bash: `shfmt -i 4 -ci -ln bash`
  - POSIX: `shfmt -i 4 -ci -ln posix`
  - Zsh: `shfmt -i 4 -ci -ln zsh` (needs shfmt 3.13+, the first release with a zsh dialect)
- Launcher messages follow the dotfiles terminal policy: `DOTFILES_COLOR=auto|always|never`, and a nonempty `NO_COLOR` or `TERM=dumb` disables color. Native CLI output and completion lists stay untouched, and redirected logs stay plain by default.

## Testing

- Every test entry point (`tests/*.sh`, `tests/run.ps1`) unsets the dotfiles knobs first, from the one list in `tests/run.sh`; `tests/test-entrypoints.sh` checks each entry point against it and that it covers every `DOTFILES_*`/`AWESOME_SKILLS_*` name in a tracked file outside `tests/`, `docs/`, `.github/` and Markdown, so a knob a new file reads goes on that list too.
- Behavior coverage is tracked as a source-to-test matrix in `tests/COVERAGE.md`; update it with the tests.

## Windows line endings

- `.gitattributes` pins `eol=lf` because Git for Windows enables `core.autocrlf` in its system config, and Git Bash fails on a CRLF `set -eu`. Superproject attributes do not apply inside a submodule, so this repository carries its own. The Windows CI job fails when `git ls-files --eol` reports a `w/crlf` file.

## Workflows and checks

- Keep workflow definitions in `.github/workflows/*.yml`.
- Pin action versions and avoid unnecessary matrix complexity. Actions use major tags (for example `@v6`) and CI installs `pre-commit==4.6.2`, the same convention as dotfiles.
- Keep CI aligned with `.pre-commit-config.yaml` tools:
  - `shellcheck`
  - `shfmt`
  - basic hygiene hooks
  - the `english-documentation` ASCII check
  - `gitleaks`, plus the manual `gitleaks-history` stage that CI runs over the full history
- CI runs `./tests/run.sh --ci` on Linux and macOS and `./tests/run.ps1 -CI` on native Windows, and each job fails if the tests or hooks leave an untracked file.
- If CI behavior affects contributors, update `README.md` in the same change.

## Relationship to dotfiles

- Every host auto-updates from dotfiles `main` at login, so `main` here must stay releasable; a change reaches hosts only through the guarded dotfiles bump (`update-coding-agent-kit-submodule.yml` there).
- Never force-push or rewrite `main` and never move a tag: a dotfiles gitlink may point at any commit on `main`, and a host that cannot fetch it never updates again on its own.
- History before the import lives in dotfiles (https://github.com/FridrichMethod/dotfiles, up to 6fe50bc); the imported commits keep only these files.
