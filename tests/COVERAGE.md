# Behavioral test coverage

`./tests/run.sh` is the Unix entry point; `./tests/run.ps1` is the PowerShell
entry point. CI requires `--ci` on Unix and `-CI` on native Windows. Provision
the pinned parser with `setup-sync.sh` / `setup-sync.ps1` first. The pre-commit
hooks run the same focused Unix tests when their production sources change.

The suites read only the frozen fixtures in `tests/fixtures/`, byte copies of
the dotfiles settings data. Dotfiles replaces `codex-config.toml`,
`claude-settings.json` and `portable.rules` there with its real files and
reruns `config-sync.sh` and `claude-customizations.cjs`; the checks of what
that data contains, Stow wiring and the installers stay in dotfiles.

Shell line-coverage is not used as a hard gate: these programs mix sourced
POSIX shell, Bash, Python, Node, child processes, and PowerShell, for
which one percentage would omit meaningful branches. Coverage is tracked as a
source-to-test behavior matrix instead.

| Production target | Test | Covered behavior |
| --- | --- | --- |
| `lib/config_sync.py` and sync runtime/wrappers | `test_config_sync.py`, `config-sync.sh` | parsed TOML multiline/quoted/dotted/inline/AoT forms, required/retired ownership, JSON authoritative arrays and type-sensitive no-op, source immutability, explicit migration, read-only preflight, invalid/unreadable inputs, adjacent replacement and failure cleanup, stricter permissions retained on no-op/write, quiet success but visible errors, JSON/rules with stdlib-only runtime |
| `setup-sync.sh` / `setup-sync.ps1` | `test_config_sync.py`, CI setup steps | explicit pinned runtime, dependency validation, no runtime auto-install, unsafe environment reuse rejection |
| `common/claude/.local/bin/claude-settings-sync` | `ai-config-sync.sh` | portable-wins deep merge, project-instructions plugin config, live-only state, malformed/mistyped input, fresh and empty live files, permissions, idempotence, legacy symlink |
| `common/claude/.claude/dotfiles/*.cjs` | `claude-customizations.cjs` | status-line fallbacks and Git branches, terminal notification routing and escaping, Git bypass blocks and allowed commands, portable launchers with spaced paths; desktop delivery needs an interactive terminal |
| `common/codex/.local/bin/codex-config-sync` | `codex-config-sync.sh` | portable public-network profile ownership, runtime top-level/table/array-table preservation, legacy sandbox removal, removed compatibility flags, fresh live file, permissions, idempotence, missing-key failure, legacy symlink |
| `common/codex/.local/bin/codex-rules-sync` | `ai-config-sync.sh` | create/no-op, empty source failure, sibling preservation, permissions, matching legacy symlink |
| `common/codex/.local/bin/codex-instructions-sync` (the `codex-instructions-sync` kind in `lib/config_sync.py`) | `test_config_sync.py` (InstructionsTests) | MARKER contract, portable plus host rules written 0644 and never widened, newline normalization, byte-identical no-op, stale regeneration, foreign or symlinked LIVE refused with `--append` and kept with a warning without it, removal of a generated LIVE only, `--check` writing nothing and reporting current/stale/foreign, dangling rules with the submodule hint, empty or non-UTF-8 inputs, option rejection, no tomlkit |
| `tests/run.sh`, `tests/run.ps1` and CI | `test-entrypoints.sh`, three native CI jobs | required dependencies fail visibly, local optional skips reported, native Windows job cannot run on Unix, shared backend suite required, every entry point unsets the same dotfiles knobs (covering each `DOTFILES_*`/`AWESOME_SKILLS_*` name in any tracked file outside tests, docs, `.github` and Markdown, in shell, PowerShell or Python spelling) and a suite passes under the provisioning exports |

Desktop notification delivery still requires an interactive terminal.

Interactive presentation helpers are outside this core automation matrix. Add
a row and a focused test when promoting one of them to a supported automation
contract.
