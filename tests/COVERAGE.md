# Behavioral test coverage

`./tests/run.sh` is the Unix entry point; `./tests/run.ps1` is the PowerShell
entry point. CI requires `--ci` on Unix and `-CI` on native Windows. The
pre-commit hooks run the same focused Unix tests when their production sources
change.

The suites read only the frozen fixtures in `tests/fixtures/`.
`claude-settings.json` there is a byte copy of the dotfiles Claude settings;
dotfiles replaces it with its real file and reruns `claude-customizations.cjs`.
The checks of what that data contains, Stow wiring and the installers stay in
dotfiles.

Shell line-coverage is not used as a hard gate: these programs mix sourced
POSIX shell, Bash, Python, Node, child processes, and PowerShell, for
which one percentage would omit meaningful branches. Coverage is tracked as a
source-to-test behavior matrix instead.

| Production target | Test | Covered behavior |
| --- | --- | --- |
| `common/claude/.claude/dotfiles/*.cjs` | `claude-customizations.cjs` | status-line fallbacks and Git branches, terminal notification routing and escaping, Git bypass blocks and allowed commands, portable launchers with spaced paths; desktop delivery needs an interactive terminal |
| `tests/run.sh`, `tests/run.ps1` and CI | `test-entrypoints.sh`, three native CI jobs | required dependencies fail visibly, local optional skips reported, native Windows job cannot run on Unix, every entry point unsets the same dotfiles knobs (covering each `DOTFILES_*`/`AWESOME_SKILLS_*` name in any tracked file outside tests, docs, `.github` and Markdown, in shell, PowerShell or Python spelling) and a suite passes under the provisioning exports |

Desktop notification delivery still requires an interactive terminal.

Interactive presentation helpers are outside this core automation matrix. Add
a row and a focused test when promoting one of them to a supported automation
contract.
