# Development

Develop in your own clone. [AGENTS.md](../AGENTS.md) holds the repository's
rules for every contributor, human or agent: the public contract, the weight
and dependency rules, the shell and test conventions, and how releases are
tagged.

## Tests

```sh
./tests/run.sh             # CI: ./tests/run.sh --ci
pre-commit run --all-files
```

- `run.sh` needs bash, sh, git, python3 and node. Without zsh it skips the zsh
  cases and says so; `--ci` requires zsh. Without `codex` on `PATH` it skips
  the native Codex sharing and hook checks, without `claude` the real Claude
  Code hook checks, and without GNU Stow the folded Stow cases of
  `standalone-e2e.sh`, and says so; as root it skips the read-only
  directory cases.
- Every suite runs in a temporary HOME with fake CLIs and fictitious
  credentials, reaches no network beyond local fixtures, and first unsets
  `CODING_AGENT_KIT_CONFIG`, `CODING_AGENT_KIT_COLOR`,
  `CODING_AGENT_KIT_USAGE_FILE`, `CODING_AGENT_KIT_TOKEN_ID`,
  `CODING_AGENT_KIT_CLAUDE_PID`, `CODING_AGENT_KIT_REPO_URL`,
  `CODING_AGENT_KIT_GUARD`, `CODING_AGENT_KIT_GUARD_SCRIPT`,
  `CODING_AGENT_KIT_GUARD_PYTHON`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and
  `NO_COLOR`.
- `tests/standalone-e2e.sh` installs with `install.sh` under `/bin/sh`, bash and
  (when present) dash, checks its refusals and drives both launchers and their
  completions through the links;
  `tests/managed-install.sh` builds a local repository with release tags,
  passes it as `CODING_AGENT_KIT_REPO_URL` (`file://`) and drives the
  managed install through bootstrap, update, channel switch, lock, refusals,
  uninstall and purge;
  `tests/contract.sh` checks the public contract (see
  [AGENTS.md](../AGENTS.md#public-contract)).
- [tests/COVERAGE.md](../tests/COVERAGE.md) maps each source file to its tests.

[Account launchers](account-launchers.md#validation) lists the suites of the
launchers and of usage reserves, and what they cover.

## CI

CI runs the suite on Linux and on macOS with the system Bash 3.2, and every
pre-commit hook on Linux. The Linux job installs GNU Stow and fails if the
folded Stow cases did not run; macOS skips them. It runs on pull requests, on
pushes to `main` and on pushes to `ci/**` branches, so a change can be
checked on both before `main` is fast-forwarded to it, and on pushed `v*`
tags, where it fails unless each `vX.Y.Z` tag on the commit names the
launchers' release.

## Versioning

Releases follow semantic versioning within 0.x. A minor release (0.2.0 to
0.3.0) may break the contract, the command-line surface or the defaults; a
patch release (0.2.0 to 0.2.1) must not. Tags are never moved and `main` is
never rewritten, so a fix ships as a new patch tag.

Consumers may take patch tags automatically: a managed install moves to the
newest patch of its channel on `--update`, and a scheduled job may move a
submodule to the newest tag of a pinned minor line. A patch is therefore
tagged only from a green `main` and never changes a public interface: the
linked paths, the help text, the own options and the `--labels` and
`--info` output, the managed install's commands and paths, or anything else in the
contract in [AGENTS.md](../AGENTS.md#public-contract). Take a minor release,
a new channel, through a reviewed change. [CHANGELOG.md](../CHANGELOG.md) lists every release.

[AGENTS.md](../AGENTS.md#releases-and-tags) describes how a release is
bumped, checked and tagged.
