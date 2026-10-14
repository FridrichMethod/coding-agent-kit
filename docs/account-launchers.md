# Codex/Claude account launchers

Each tool's Stow package installs its executable command in `~/.local/bin`; no sourcing is required. Small Python standard-library helpers live in each tool's `~/.local/lib`. They require Python 3.11+, install no dependencies, implement no OAuth flow, and run no background service.

## Daily use and adding accounts

```sh
codex-as ayting
codex-as work resume --last
codex-as ayting exec --json "Check the tests"
claude-as work
claude-as work -p "Check the tests"
```

Valid local credentials launch immediately. A missing account lists existing accounts and asks `[y/N]`, defaulting to no, without correcting similar names. After confirmation, successful native login, and saving, the original command continues. Corrupt files, broken links, and permission errors are not treated as missing accounts and are never overwritten for a fresh login.

- **Codex**: runs `codex -c 'cli_auth_credentials_store="file"' login --device-auth` in a private, empty temporary `CODEX_HOME`. Device login must be enabled in the account's Security settings or by the workspace admin. Failure, cancellation, or timeout does not save credentials or automatically switch to a browser flow. Use the official URL/code to complete authorization initiated on this machine. [Official authentication documentation](https://learn.chatgpt.com/docs/auth)
- **Claude**: runs `claude setup-token` in a temporary `CLAUDE_CONFIG_DIR`; there is no `generate` subcommand or equivalent device-code flow. Automatic browser opening is temporarily disabled, and the native manual authorization link is displayed. If the page returns a code, paste the complete `code#state` and press Enter. Browser cookies are not isolated by the CLI configuration directory; choose the intended account in the browser.
- Claude has no machine-readable token export interface. The local `2.1.296` adapter captures the complete success section, deduplicates it, and saves only after a successful exit, without replaying raw output. It streams ANSI/OSC sequences, chunks, and line breaks with bounded buffering. An unknown version, capture error, or failure prompts separately to run the official command and paste without echo; the default is no. **In the manual fallback, the official command displays the token; the wrapper's paste input does not echo it.**

Claude setup-token tokens typically last one year and do not renew automatically. They support model requests, with no guarantee for Remote Control or claude.ai connectors; local MCP remains usable. [Official documentation](https://code.claude.com/docs/en/authentication)

Confirmation and login I/O use a separate `/dev/tty`, preserving the final stdin/stdout/stderr, cwd, arguments, and exit code. A missing account is refused without a controlling terminal; there is no `--yes`. The wrapper's `--help`, `NAME --help`, and root-level version queries do not trigger initialization or login.

## First-time Codex initialization

**If the root home already has credentials, exit the current Codex and close all terminals, suspended processes, desktop clients, and IDE extensions using that root home before running:**

```sh
codex-as --migrate stanford
```

`stanford` is an example local default account; replace it with the alias belonging to the current root credentials. Migration confirms, backs up the current root file and any existing account snapshot, imports the original bytes, and fixes the root link to the default account. Different bytes must match a stable user identity: a shared workspace `account_id` does not prove the same user, and unknown identities are never guessed. Migration retries support both regular root files and safe existing default links.

Private backups live at `~/.secrets/.codex-as-backup-*` and are excluded from completion. User-owned `.codex` and `.codex/.tmp` directories are tightened to 0700 only during explicit migration, preserving their contents. Migration rejects temporary directories owned by someone else or represented by symlinks, and permission errors name the exact path. Incorrect permissions on an existing `.secrets` directory cause refusal rather than an automatic chmod. The old `codex-as.initialized` marker remains; the new `codex-as.layout.json` records the layout version and default alias, not credentials. Markers are fully written to temporary files before atomic publication; private pending setup is also recorded before account creation. Interrupted setup can be retried; unknown existing directories are preserved and initialization is refused. Migration neither deletes old markers nor overwrites files with mismatched identities.

Each account has a fixed entry point: `~/.codex/accounts/NAME/auth.json -> ~/.secrets/codex-NAME.json`. Account directories are 0700; complete credentials are 0600. Normal startup no longer switches the global link. Native `codex` keeps using the root default account; `codex-as NAME` sets an account-specific `CODEX_HOME` and enforces file authentication. A new home without root credentials can create the account layout directly; existing credentials or initialization markers require explicit migration.

Config, both AGENTS files, rules, skills, agents, memories, session directories, history, the session index, and the complete SQLite home are shared. Database files and WAL/SHM use the original directory together, rather than individual file links. Configuration path fields must be absolute because native relative paths resolve against the account directory; the launcher reports the field name and never rewrites configuration. An explicit `sqlite_home` retains its absolute directory; otherwise, the root directory is used. Conflicting environment, project, or CLI overrides are refused.

Native thread writer locks and the rollout maintenance lock are shared. An active writer prevents another account from writing the same session. An explicit `codex-as OTHER resume UUID` can offer confirmed takeover from the root daemon: it ends the old session and spawned agents, temporarily archives them, restores saved archive visibility, then launches the selected account on the same UUID. Confirmation can interrupt active work. Close other views of that session before confirming. The remaining daemon sessions stay loaded. Standalone clients must exit before their session can be resumed elsewhere. `resume --last` and the picker share discovery results but do not perform takeover. Explicit session names have account-path filtering problems, so the launcher rejects them and requests a UUID. Names can still be displayed and edited in the native UI.

Takeover requires a controlling terminal and an audited root daemon (0.161.0 or 0.162.1). Unknown identities, unsafe sockets and ambiguous ownership cause refusal. Recovery metadata stays private under `~/.codex/accounts/.locks/UUID.handoff.json`; retain it and retry the same explicit resume command after an interrupted or failed takeover. Saved history and original descendant archive visibility are preserved; unfinished runtime progress, SessionEnd hook effects and timestamp changes cannot be rolled back. Takeover through exec, fork or the picker is outside this flow.

Cloud policy, the model catalog/cache, remote plugin installations/cache, MCP/Connector login, daemons, IPC, temporary files, and logs remain separate for each account. Local plugin paths in shared configuration can be shared. Remote plugin cache directories cannot be linked wholesale: one account's sync could delete another account's bundles. These existing root account caches are not imported into new accounts.

If account credentials are atomically replaced with a regular file, the link is restored when there are no other wrapper clients and the file matches secrets. If secrets have not changed during this run and the user identity matches, both copies are backed up, the new original bytes are imported, and the link is restored. If both copies changed, no startup snapshot exists, or identities differ, repair stops without guessing from timestamps. The last exiting client or the next idle startup can perform safe repair; repair errors do not replace the native exit code.

For manual source selection, close all clients for that account and run `codex-as --recover NAME`: choose 1 to keep secrets, 2 to import the account directory's regular file, or empty input to cancel. Confirmation is followed by backup and link restoration. A missing link requires explicitly choosing saved secrets. **Do not delete markers or overwrite regular files yourself with old snapshots.** Damaged root default credentials require another explicit migration; account recovery cannot guess their source. See the [Codex verification record](codex-as.md) for source and test limitations.

## Claude configuration and security boundaries

Tokens are saved at `~/.secrets/claude-NAME`: 0600, a regular file owned by the current user, with a single hard link. Daily startup sets only `CLAUDE_CODE_OAUTH_TOKEN` for the child and inherits the existing `CLAUDE_CONFIG_DIR`. It does not force `~/.claude`, preserving existing sources of `.claude.json`, MCP, configuration, and trust state.

Explicit API key/OAuth token/authentication FD/alternative provider/endpoint/profile overrides, authentication helpers in settings, and organization restrictions are refused. Only variable names or sources are reported, never values. Passthrough of login/logout/setup-token, `--bare`, authentication configuration source overrides such as remote/SDK/managed-settings/project-config-root, and unaudited options is prohibited. Only the first command operand is identified using native argument boundaries; words inside prompts are not scanned. Native Claude dispatches an exact first `auth` operand to authentication subcommands even with `-p` or `--`, so it is refused. Required/optional/variadic argument values retain their native roles. This is a bounded local CLI option check, without a promise of compatibility with every command.

Managed policy is respected: automatic enrollment stops for nonempty managed caches, system policy, policy helpers, authentication bindings, or profile loading chains that cannot be preserved. Empty or missing caches do not cause this refusal. The local cache inspected was an empty object; it is checked again at runtime.

New account files are created exclusively; incorrect permissions, ownership, symlinks, or hard links are refused. Account names must be nonempty and contain only ASCII letters, digits, underscores, and hyphens. Local format validation is not server authentication; expired credentials are not automatically deleted or replaced through login.

## Completion, installation, and concurrency

zsh uses OMZ custom completions from each tool's package and the existing compinit; Bash autoloads from the standard bash-completion user directory. The first argument enumerates only safe filenames and metadata, without reading credentials, running the CLI, or using the network. Corrupt contents are detected at startup. Later arguments first complete the supported scope, then delegate to existing native completion when available.

fzf-tab uses the same candidates; previews for both account commands only print text and do not read files. Normal Tab works without fzf. New terminals load the new completions; no source or PATH lines are appended to rc files. Installation uses Stow and triggers no real initialization, login, or model requests.

The launcher's own step/help headings are cyan, confirmations/warnings yellow, successful saves/initialization green, and errors red. By default, color is enabled only for the terminal receiving the message; redirected logs stay plain. Confirmations on a separate `/dev/tty` can still use color without affecting redirected Codex/Claude output. `DOTFILES_COLOR=auto|always|never` is supported; nonempty `NO_COLOR` or `TERM=dumb` takes priority and disables color. Native command output, completion account lists, and internal machine output are not colorized.

**Codex supports concurrent sessions across accounts and different sessions within one account.** Each wrapper and native child holds a shared account lease; credential repair requires an exclusive lease. Suspended processes or a child surviving an abnormal supervisor exit still block repair. Lease files persist and are not deleted based on PIDs. A shell inheriting `CODEX_HOME` from account A can launch B directly, but the inherited source must be a verified managed account directory. Native same-account OAuth refresh has only an in-process lock, so multiple processes can still refresh simultaneously or briefly read partial JSON; the wrapper implements no additional OAuth. Direct CLI, desktop, and IDE clients do not participate in wrapper leases, and automatic exit repair cannot exclude them. Migration/manual recovery requires closing related clients; this boundary also applies to unmanaged clients using the root default account. Processes are not automatically killed, quotas are not switched automatically, no background keepalive runs, and Claude tokens are not renewed.

## Validation

```sh
tests/codex-as.sh
tests/account-enrollment.sh
pre-commit run --all-files
```

Routine tests use temporary HOME directories, fictitious credentials, and fake CLIs. They cover Bash/zsh, TTY, stdin/JSON stdout, migration/recovery/interruption, concurrent leases, signals, refresh writeback, incorrect permissions, and completion/preview secrecy. When the audited version is installed, an additional real Codex offline app-server fixture checks legacy/paginated history, shared SQLite discovery, and native writer locks, without login or model calls and with zero requests. Missing or mismatched versions explicitly skip this check. Native protocol tests do not verify real-account server authorization, quotas, or OAuth refresh, and do not directly operate the terminal picker/`--last`.
