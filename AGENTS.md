# Agent Guide

This repository is coding-agent-kit, a standalone Codex and Claude Code
multi-account wrapper: the `codex-as` and `claude-as` launchers, with optional
usage reserves that a reserve guard enforces through the natives' own hooks,
and three Claude Code helpers (status line, notifications, Git hook guard). It
installs on top of an existing `codex` or `claude` and never installs either.
Users install it as a managed install that follows the newest patch release of
a channel (`install.sh` piped to `sh`), with `install.sh` from a checkout of
their own, or by linking or copying its files. It runs on Linux and macOS (WSL
counts as Linux); native Windows is not supported.

## Scope and layout

```
bin/codex-as, bin/claude-as      launchers (Bash, POSIX sh)
lib/codex-as/*.py                codex-as modules: launch, check, layout, handoff, usage
lib/claude-as/*.py               claude-as module: launch
lib/coding-agent-kit/config.py   the configuration loader both launchers share
lib/coding-agent-kit/accounts.py the account metadata cache both launchers share
lib/coding-agent-kit/reserve.py  reserve flags, per-window policy, the guard's decision and messages (pure)
lib/coding-agent-kit/guard.py    the reserve guard: hook and status line command of a guarded launch
lib/coding-agent-kit/guard_launch.py what both launchers need to start a guarded launch
completions/{bash,zsh}/          account-name and own-option completion for both launchers
helpers/*.cjs                    Claude Code helpers (Node)
install.sh                       links a checkout's bin/ and completions/ into a prefix; the managed install
docs/                            account-launchers.md, codex-as.md, configuration.md (public);
                                 README.md (index), install.md, helpers.md, development.md, assets/
tests/                           suites, fixtures, runners, COVERAGE.md
```

- `bin/..` has the shape of a `~/.local` prefix: each launcher resolves its own
  symlinks with a bounded loop (40 hops, `readlink` without `-f`, `cd -P`) to
  `bin/..` and runs `lib/<tool>/launch.py` there, so linked, copied and
  Stow-managed installs all work. Keep `lib/<tool>/` beside `bin/`, and load
  sibling modules by path from `os.path.realpath(__file__)`: `python3 -I` puts
  no script directory on `sys.path`.
- Track no symlinks, so a checkout works where links are unsupported.
- `--help` (`-h`), `--version` (`-V`, `-v`) and a run without arguments are
  pure shell and work without `python3`; `--version` also needs no `git` and
  starts no native CLI. Every other Python entry checks the Python version first, then
  loads its modules inside the error handling, so a missing file prints
  `incomplete install at ROOT`, never a traceback. The guard checks the
  Python version first too, and a module it cannot load is a guard failure.
- `lib/coding-agent-kit/config.py` is the only launcher or completion code
  that reads `HOME`, `XDG_STATE_HOME` or the configuration file (a test greps for other reads). It loads once per run into
  one frozen object that every consumer takes. Unknown keys warn and never
  fail, so a newer configuration file works with an older kit. See
  [docs/configuration.md](docs/configuration.md).
- Without a configuration file the launchers must pass exactly the
  arguments, environment, files and links that a file with the defaults
  written out gives; the golden test in `tests/test_config.py` compares the
  two.

## Public contract

Consumers build on public interfaces: the paths they link, the help text,
the output formats of `--labels` and `--info`, and the rest of this
section. Changing one needs a new minor release and a matching
CHANGELOG entry. Consumers may track patch tags automatically, so a patch is
tagged only from a green `main` and never changes an interface.

`tests/contract.sh` pins the file layout, and runs in `run.sh` and as a
pre-commit hook:

- The public paths and their modes (`git ls-files -s`): the launchers, their
  `lib/` modules, the shared `lib/coding-agent-kit/` modules (`guard.py` and `guard_launch.py` among them), the completions, the helpers, `install.sh`
  and the three docs. Consumers may link these paths one by one from a tree of
  their own, copied installs depend on the `bin/` plus `lib/` shape, and
  `--help` points at `docs/account-launchers.md`.
- Each file is a regular file in the working tree, executable where its mode
  says so, and the launchers and `install.sh` keep their `#!/bin/bash` or
  `#!/bin/sh` line.
- No tracked symlinks.

The command-line surface is public too, guarded by the suites rather than
`contract.sh`: the help text; the own options, only as the first argument
and by exact match, in every spelling, aliases included, which no release drops:
`-h`/`--help`, `-V`/`-v`/`--version`, `-l`/`--list`/`--list-accounts`, `--labels`/`--account-labels`,
`-i`/`--info`/`--account-info NAME`, `claude-as --label`/`--set-label`
and `--show-config`, with their outputs and exit codes (`--version` prints
`coding-agent-kit X.Y.Z (FORM)`, FORM being `managed vX.Y.Z, channel X.Y`,
`checkout ROOT`, `files ROOT` or `unknown`); the hint and exit code for any
other dash word there; the launch flags `--reserve-5h V` and
`--reserve-weekly V` before NAME, with their errors and their meaning (the
reserve guard, and the launch forms it refuses); the `main` each helper exports, the
configuration file location, keys and defaults, the account metadata files in
`${XDG_STATE_HOME:-~/.local/state}/coding-agent-kit/accounts/` with their
fields, `install.sh` with its options and exit codes,
`CODING_AGENT_KIT_COLOR`, and the `CODING_AGENT_KIT_USAGE_FILE`,
`CODING_AGENT_KIT_TOKEN_ID` and `CODING_AGENT_KIT_CLAUDE_PID` that
`claude-as` passes its child. A launch with a reserve that the kit refuses
exits 1; the refusal reasons, the guard's block messages, the line a guarded
launch prints and the footer line `reserve guard not running` are documented
behavior, not a machine interface. `CODING_AGENT_KIT_GUARD`,
`CODING_AGENT_KIT_GUARD_SCRIPT` and `CODING_AGENT_KIT_GUARD_PYTHON` are
launcher-internal: the launcher's channel to the guard it injects, removed
from every launch's environment and set only for a guarded one, never an
interface for other programs; so are the guard's state files under
`${XDG_STATE_HOME:-~/.local/state}/coding-agent-kit/guard/`. A guarded
session outlives kit updates, because a managed install's hooks reach
`guard.py` through `current`, so every later release of a minor line accepts
the `CODING_AGENT_KIT_GUARD` format (`"v": 1`), the hook kinds (`start`,
`prompt`, `tool`, `stop`, `status`) and the state files of that line's
earlier releases.

The managed install is the interface other repositories install the kit
through, so all of it is public, guarded by `tests/managed-install.sh`: the
bootstrap command (`install.sh` from `main` piped to
`sh -s -- --channel MAJOR.MINOR`), the update and removal commands
(`${XDG_DATA_HOME:-~/.local/share}/coding-agent-kit/current/install.sh --update`,
`--uninstall [--purge]`), the repair `--reinstall`, the managed root's layout (`repo.git`, `releases/`,
`current`, `channel`, `prefix`, `.lock`, and `.lock.d` where there is no `flock`), the linked paths,
including `~/.claude/coding-agent-kit/{statusline,notify,check-git-hooks}.cjs`,
the channel and update rules, the exit codes 0, 1, 2 and 75, and
`CODING_AGENT_KIT_REPO_URL`. The hand-over is a contract between releases:
a release's installer runs `install.sh --activate --channel MAJOR.MINOR
--prefix DIR/` (with the lock on fd 9) in the release it switches to, so
every later release accepts that call. A channel switch
runs the old release's installer, so a new minor keeps accepting it too.

## Required conventions

- Keep secrets and machine-specific absolute paths out of version control.
- Write this repository's documentation in English using printable ASCII, tabs and line breaks only. This includes README files, agent guides and docs. Use ASCII punctuation and diagrams; do not add Chinese text, emoji or decorative Unicode symbols. The native pre-commit pygrep hook `english-documentation` checks every documentation file in this repository.
- Develop in your own clone. A consumer's submodule checkout must stay clean,
  because an untracked file there can stop its updates; for the same reason
  tests and hooks leave nothing behind, and `.gitignore` covers editor, cache
  and venv files at any depth.

## Weight and dependencies

The kit runs on what a host already has and stays small enough to audit.

- At run time the launchers and `install.sh` need only POSIX `sh` or Bash with ordinary POSIX utilities, the Python 3.11+ standard library run as `python3 -I -B` (the launchers add `-S`), and the tool they wrap; the managed install also needs Git and `tar`, and takes its lock and renames over links with `python3`. Every network git call is bounded (HTTP low-speed limit, ssh timeouts, `timeout` where it exists), because a run holds the lock while it waits. The helpers need Node.js 22+ and nothing from npm. Nothing needs a pip package, virtual environment, compiler, network service or background process of its own. The kit never installs a tool.
- A missing optional tool narrows the feature; a missing required tool fails once with a message that names it, never with a traceback or a half-done run. For example, codex-as without `git` limits takeover to the launch directory.
- A wrapper forwards to the native tool instead of reimplementing it. It requires a minimum version, never an exact pin or an upper bound. Anything beyond the tool's documented CLI and files (internal socket paths, wire protocols, lock layouts) is best effort: a failure there warns and falls back to plain native behavior, and blocks or asks only where continuing could lose work.
- Keep helper code proportional to what it adds over the native tool. A feature that needs its own protocol client, journal or lock scheme must justify its code and test cost; prefer the native tool's own feature when one exists.
- Usage reserves are enforced by the reserve guard (`guard.py`), which a guarded launch (a reserve on and a launch form that starts model work) injects as the natives' own hooks (`claude-as` with `--settings`, also as Claude Code's status line; `codex-as` with `-c hooks.*`): one short process per hook event under the launcher's own `python3 -I -B -S`, never a service, daemon or background process. It reads, best effort, the usage data the native already has: the rate limits in Claude Code's status line input, which it records in the account's usage file, and the Codex session rollout, of which the turns that its hook events name are the launch's own, and the account's attested usage, which it records from those turns in the guard's directory; what it cannot read or does not know counts as no data. Unlike the best-effort rule above, the guard does not fall back to plain native behavior: with a reserve on, a session that the guard sees has had a reply but no usage data, a guard failure on a prompt or a tool call, and a launch form the hooks cannot cover block or are refused (a Codex guard that ends without an answer, as one that cannot start, lets the event through, which the check after the run reports where it can tell), and hooks that the native does not run at all are reported where the kit can tell (a footer line, a warning after the run or at the next launch). A launch with both reserves off injects nothing and follows every rule above unchanged. The guard requires a minimum native release (`GUARD_MINIMUM` in `lib/claude-as/launch.py`; for Codex the `MINIMUM_VERSION` in `lib/codex-as/check.py` that every launch needs), never an exact pin or an upper bound, and its Codex hook definitions stay the same across releases, because Codex trusts a hook by a hash of its definition.
- Tests use the Python standard library (`unittest`), Node's built-in test runner, the shells under test and fakes for third-party CLIs, run in a temporary HOME, and reach no network beyond `127.0.0.1` fixtures. A test may use a real host tool (zsh, dash, GNU Stow, codex, Claude Code) when it is present and must skip cleanly, with the reason, when it is absent. Nothing in the suites needs Docker, the internet or real credentials. Tests never touch the real home, its credentials or running daemons.
- CI runs every pre-commit hook on every push, so keep hooks scoped by `files:` and fast: test parsing and other pure logic in process, and spawn real shells and child processes only for behavior that needs them (exec, signals, umask, TTY, file modes).

## Working commands

- Install into a prefix: `./install.sh [--prefix DIR]`; remove: `./install.sh --uninstall`
- Managed install: `install.sh --managed --channel MAJOR.MINOR` (or piped to `sh -s --`); update: `"${XDG_DATA_HOME:-$HOME/.local/share}/coding-agent-kit/current/install.sh" --update`; remove: `--uninstall [--purge]`
- Run the tests: `./tests/run.sh` (CI: `--ci`)
- Run one suite: `tests/config.sh`, `tests/codex-as.sh`, `tests/account-enrollment.sh`, `tests/account-info.sh`, `tests/standalone-e2e.sh`, `tests/managed-install.sh`, `tests/contract.sh`; usage reserves: `tests/reserves.sh`, `tests/guard.sh`, `tests/claude-guard.sh`, `tests/codex-guard.sh`, `tests/reserve-completion.sh`
- Run checks: `pre-commit run --all-files`

## Verification

After modifying any file, run `pre-commit run --all-files` to ensure changes pass CI checks before committing.

## Shell conventions

- Use Bash/Zsh-specific syntax only in matching shell files. `bin/claude-as` and `install.sh` are POSIX `sh` and run under dash.
- `install.sh` defines functions only and starts its work on its last line, `main "$@"`, so a truncated download runs nothing; in managed mode every child gets `/dev/null` as stdin, because a piped `install.sh` is the shell's stdin, and git runs with the lock's fd 9 closed.
- Follow pre-commit shell style:
  - Bash: `shfmt -i 4 -ci -ln bash`
  - POSIX: `shfmt -i 4 -ci -ln posix`
  - Zsh: `shfmt -i 4 -ci -ln zsh` (needs shfmt 3.13+, the first release with a zsh dialect)
- Launcher messages follow one colour policy in shell and Python: `CODING_AGENT_KIT_COLOR=auto|always|never` (unset or invalid means `auto`, styled only when the destination is a terminal), and a nonempty `NO_COLOR` or `TERM=dumb` disables color. Native CLI output, completion lists and the output of `--list`, `--labels` and `--show-config` stay untouched; `--info` styles only its own words (the heading, missing data and each window's use, in the status line's colours) under the same policy for stdout, and redirected logs stay plain by default.

## Testing

- Every test entry point (`tests/*.sh`) starts with the one statement `unset CODING_AGENT_KIT_CONFIG CODING_AGENT_KIT_COLOR CODING_AGENT_KIT_USAGE_FILE CODING_AGENT_KIT_TOKEN_ID CODING_AGENT_KIT_CLAUDE_PID CODING_AGENT_KIT_REPO_URL CODING_AGENT_KIT_GUARD CODING_AGENT_KIT_GUARD_SCRIPT CODING_AGENT_KIT_GUARD_PYTHON XDG_CONFIG_HOME XDG_STATE_HOME NO_COLOR`. `tests/test-entrypoints.sh` checks each entry point against `tests/run.sh` (the statement, and that the entry point is in its suite list; every `tests/test_*.py` must be run by an entry point), and that the statement names every `CODING_AGENT_KIT_*` variable (pattern `_?CODING_AGENT_KIT_[A-Z_]+`) in a tracked file outside `tests/`, `docs/`, `.github/` and Markdown, so a knob a new file reads goes on that list too. Python tests that pass the caller's environment on drop the same names in `setUp`.
- Behavior coverage is tracked as a source-to-test matrix in `tests/COVERAGE.md`; update it with the tests.

## Line endings

- `.gitattributes` pins `eol=lf`, so a clone made with Git for Windows, which enables `core.autocrlf` in its system config, keeps LF files and still runs under WSL, where a CRLF `set -eu` fails. A superproject's attributes do not apply inside a submodule, so this repository carries its own.

## Workflows and checks

- Keep workflow definitions in `.github/workflows/*.yml`.
- Pin action versions and avoid unnecessary matrix complexity. Actions use major tags (for example `@v6`) and CI installs `pre-commit==4.6.2`.
- Keep CI aligned with `.pre-commit-config.yaml` tools:
  - `shellcheck`
  - `shfmt`
  - basic hygiene hooks
  - the `english-documentation` ASCII check
  - `gitleaks`, plus the manual `gitleaks-history` stage that CI runs over the full history
- CI runs `./tests/run.sh --ci` on Linux (zsh and GNU Stow installed; the test step fails if the folded Stow cases of `standalone-e2e.sh` did not run) and on macOS (system Bash 3.2, `/bin/sh`); each job fails if the tests or hooks leave an untracked file.
- `ci.yml` runs on pull requests, on pushes to `main` and on pushes to `ci/**` branches: check a change on a `ci/**` branch, then fast-forward `main` to that exact green commit and delete the branch. It also runs on pushed `v*` tags; only there (`GITHUB_REF_TYPE=tag`) the `ReleaseVersion` test checks the tags already on the checked-out commit, and fails unless each `vX.Y.Z` one names the launchers' release.
- Agents land their own changes this way without a pull request and without asking before each push, merge or release tag, once the local suite, `pre-commit` and every job of that `ci.yml` run are green. They still ask before anything this file forbids or that cannot be undone: moving or deleting a tag, rewriting `main`, or deleting someone else's work.
- If CI behavior affects contributors, update `docs/development.md` in the same change.

## Releases and tags

- Versions are `vMAJOR.MINOR.PATCH` tags on `main`, with semantic versioning in the 0.x range: a minor release may break the public contract, a patch release must not. Every release gets a CHANGELOG section, and a breaking one lists its breaking changes and moved paths.
- `_codex_as_version` in `bin/codex-as` and `_claude_as_version` in `bin/claude-as` name the release that `--version` prints. Both must equal the first `## X.Y.Z` heading of `CHANGELOG.md`, which is the next or current release (there is no `Unreleased` heading); the `release-version` hook checks it, so bump all three together before tagging. Nothing else ties a tag to them, and a managed install prints the literal beside its tag, so commit the bump, then, on that commit, run `KIT_RELEASE_TAG=vX.Y.Z tests/config.sh -k ReleaseVersion` and tag it with `git tag vX.Y.Z` and no commit argument, so the tag goes on the HEAD the check read. The check fails unless the tag is `v` plus the release that HEAD's committed `CHANGELOG.md` and both committed launchers name; the work tree does not count, so a bump written or staged but not committed fails it. Tags already on HEAD are checked only in CI's run on a pushed tag, against the `CHANGELOG.md` committed there, when only a new patch can fix a wrong one; local runs and branch CI check `KIT_RELEASE_TAG` alone, so a wrong tag on HEAD never refuses the commit of that patch. Set `KIT_RELEASE_TAG` for that one command only: exported, it also fails the hook on the bump's own commit, whose HEAD still names the previous release.
- Tags are immutable: never move or delete one. A bad release is fixed by a new patch tag.
- `main` is never force-pushed or rewritten, because a consumer's gitlink may point at any commit on it, and a host that cannot fetch that commit stops updating.
- Consumers may track patch tags automatically, through the managed install's `--update` or, for example, with a scheduled job that moves a submodule to the newest tag of a pinned minor line, so a patch reaches them without review. Tag a patch only from a green `main`, run `tests/contract.sh` and the full suite first, and never let a patch change a public interface; that takes a minor release.
