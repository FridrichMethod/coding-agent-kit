# Codex/Claude account launchers

`install.sh` links both commands into `~/.local/bin` (or `PREFIX/bin`); no sourcing is required. Each launcher follows its own symlinks to the kit and runs its Python standard-library modules from the kit's `lib/`. They require Python 3.11+, install no dependencies, implement no OAuth flow, and run no background service.

Paths below use the configured values: `<codex_home>` (default `~/.codex`) and `<secrets_dir>` (default `~/.secrets`). `codex-as --show-config` and `claude-as --show-config` print the values in effect; [configuration](configuration.md) describes the file that changes them.

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

Confirmation and login I/O use a separate `/dev/tty`, preserving the final stdin/stdout/stderr, cwd, arguments, and exit code. A missing account is refused without a controlling terminal; there is no `--yes`. The wrapper's [own options](#own-options), and `codex-as NAME` with a root help or version flag, `help`, `update` or `completion`, run without the account and never start a login.

## Own options

Each launcher has a few options of its own. They count only as the first argument, by exact match and with exactly the arguments shown; everything after `NAME` goes to the native CLI. An account name never starts with `-`, so an own option never meets a native one.

| Option | Alias | Output |
| --- | --- | --- |
| `-h`, `--help` | | the help, also for a run without arguments (which exits 2); later arguments are ignored |
| `-V`, `-v`, `--version` | | the kit's release and install form; `NAME --version` is the native CLI's |
| `-l`, `--list` | `--list-accounts` | the account names, one per line |
| `--labels` | `--account-labels` | `NAME<TAB>LABEL` lines, the descriptions of the zsh completions |
| `-i NAME`, `--info NAME` | `--account-info NAME` | the last-seen details of one account ([account metadata](#account-metadata)) |
| `--show-config` | | the configuration in effect ([configuration](configuration.md)) |
| `claude-as --label NAME [LABEL]` | `claude-as --set-label NAME [LABEL]` | sets a Claude label, asks for it, or clears it with an empty one |

The aliases are accepted too; the help lists only the primary spellings. `-h` and `--help` ignore any later arguments; any other own option with arguments beyond its own is an invalid account name. Any other dash word in the first argument is refused with a hint and the exit status of an invalid account name (2 for `codex-as`, 1 for `claude-as`), and nothing starts or is written. The hint echoes only the part before the first `=`, since a value or a secret may follow it; a word with characters other than ASCII letters, digits and `_=.,:/@+-`, or whose part before `=` holds `sk-` in any case or names an own option, gets the plain invalid-name message instead, so it is never echoed:

```text
$ codex-as -c model=o3
codex-as: -c is not a codex-as option; native options go after NAME (codex-as NAME -c ...)
```

`--version` is plain shell, like the help: it needs no `python3` or `git`, starts no native CLI, writes nothing and is never styled. It prints `coding-agent-kit X.Y.Z (FORM)` on stdout, where `X.Y.Z` is the release written into the launcher and `FORM` comes from the directory the launcher resolves to after its links:

| FORM | When |
| --- | --- |
| `managed vX.Y.Z, channel X.Y` | the directory is `vX.Y.Z` in a `releases` directory, as in a managed install; the channel comes from the managed root's `channel` file and is left out when that is missing or malformed |
| `checkout ROOT` | `ROOT/.git` exists: a clone, a worktree or a submodule |
| `files ROOT` | anything else, such as copied files |
| `unknown` | the launcher cannot resolve its own location |

A session started before a managed update keeps its release, and that release's launchers keep printing it. A checkout between tags prints the release its launchers name, the first heading of its CHANGELOG, and no commit.

```text
$ codex-as --version
coding-agent-kit 0.7.0 (managed v0.7.0, channel 0.7)
```

## Codex account layout

Each account has a fixed entry point: `<codex_home>/accounts/NAME/auth.json -> <secrets_dir>/codex-NAME.json`, an absolute link. Account directories are 0700; complete credentials are 0600. `codex-as NAME` sets that account's `CODEX_HOME` and enforces file authentication; the first launch creates the account view itself, with no separate initialization command. Other arguments go to native `codex` unchanged after its own file-auth and SQLite flags (every command but `exec` also gets the update-check and `--no-daemon` flags), and the launched Codex keeps the caller's umask. `login` (except `login status` and help), `logout`, the daemon-only `agents`, `queue`, `app` (macOS and Windows), `remote-control` subcommands and `app-server daemon`, and remote or provider/auth settings are refused; `-p NAME` profiles work from `<codex_home>/NAME.config.toml`; root help or version, `help`, `update` and `completion` run as plain `codex`. See the [Codex verification record](codex-as.md) for the exact list and the remaining differences from native Codex. The root `<codex_home>/auth.json` belongs to native `codex`, desktop and IDE clients. codex-as never reads, checks or changes it, so the root login and each account keep independent token chains, even for the same ChatGPT user.

Private backups live at `<secrets_dir>/.codex-as-backup-*` and are excluded from completion. Permission errors name the exact path and are never fixed by an automatic chmod: the parents of `<codex_home>` and `<secrets_dir>` (both `$HOME` by default), `<codex_home>` and `<codex_home>/.tmp` must be owned real directories that are not group/other writable, and `<secrets_dir>` must be 0700. The account marker `<codex_home>/accounts/NAME/codex-as.initialized` and a private pending-setup record are fully written to temporary files before atomic publication, so an interrupted first launch can be retried; unknown existing directories are preserved and initialization is refused.

A root `<codex_home>/auth.json` that links into `<secrets_dir>`, and the root-level files `<codex_home>/codex-as.layout.json` and `<codex_home>/codex-as.initialized`, are not part of the codex-as layout. codex-as ignores those two files, so they can be deleted; the account marker in each account directory stays. Such a root link works, but it shares that account's token chain with the root clients, so a root login or logout revokes the account. To separate them, quit every root, desktop and IDE client completely, including the root app-server daemon and IDE extension app-servers, until `pgrep -af 'codex.*app-server'` lists none. Then remove only the link with `rm <codex_home>/auth.json` (the secrets file stays) and log in natively in the root home. A client that is still running keeps the old tokens in memory, and its logout (`account/logout`) revokes those cached tokens even after the link is gone, so quit it rather than logging out.

Credentials renew without a new login. The access token carries its own expiry; native Codex refreshes it when less than five minutes remain, and the server rotates the refresh token, which Codex writes through the account link into `<secrets_dir>/codex-NAME.json`. Only use triggers this refresh; no background keepalive runs. A revoked refresh token cannot be renewed and needs a new device login. **Native `codex login` and `codex logout` revoke the tokens of the current home's `auth.json` before removing it.** In the root home, a desktop or IDE logout included, that affects only the root's own login, unless that file is still a link into `<secrets_dir>` or a still-running client loaded its tokens through such a link; codex-as refuses both inside account views. A logout from inside an account's Codex (`/logout`) is native and still revokes that account's tokens; the launcher warns at exit with the steps below. Add accounts with `codex-as NEWNAME`. To replace a revoked account, move its secrets file aside under a name that does not match `codex-NAME.json`, then run `codex-as NAME`.

Config, both AGENTS files, rules, skills, agents, memories, session directories, history, the session index, and the complete SQLite home are shared. Database files and WAL/SHM use the original directory together, rather than individual file links. Configuration path fields must be absolute because native relative paths resolve against the account directory; the launcher reports the field name and never rewrites configuration. An explicit `sqlite_home` retains its absolute directory; otherwise, the root directory is used. Conflicting environment, project, or CLI overrides are refused.

Native thread writer locks and the rollout maintenance lock are shared. An active writer prevents another account from writing the same session. Interactive resume takes sessions over from the root daemon so another account can write them: it ends the old session and spawned agents there, temporarily archives them, restores saved archive visibility, then launches the selected account. An explicit `codex-as OTHER resume UUID` does this without a prompt for an idle session and asks first when it may be running, since takeover interrupts active work. `resume --last` takes over only the session it opens, the newest one native Codex lists, when that one is idle in the root daemon. Before the picker starts, the idle top-level sessions it lists by default (this directory, or the same place in a linked worktree; all of them with `--all`) are taken over and named, because the selection happens inside native Codex; they move to the top of the list. Sessions that may be running stay and are named too. Clients attached to the root daemon, such as Desktop, close a session taken over. The remaining daemon sessions stay loaded. Sessions held by standalone clients are never touched; native Codex shows its conflict screen until they exit. Explicit session names have account-path filtering problems, so the launcher rejects them and requests a UUID. Names can still be displayed and edited in the native UI.

Takeover requires a root daemon at 0.161.0 or newer, and a controlling terminal only to confirm a session that may be running. Unknown identities, unsafe sockets and ambiguous ownership cause refusal, and an unrecognized option in the resume command skips takeover. Recovery metadata stays private under `<codex_home>/accounts/.locks/UUID.handoff.json`; retain it and retry the same command after an interrupted or failed takeover. `fork UUID`, `exec resume UUID` and `exec fork UUID` never take over, but first finish an interrupted takeover of that UUID; `resume UUID` does too, and the picker and `resume --last` finish every interrupted takeover. Recovery only restores visibility. Saved history and original descendant archive visibility are preserved; unfinished runtime progress, SessionEnd hook effects and timestamp changes cannot be rolled back.

Cloud policy, the model catalog/cache, remote plugin installations/cache, MCP/Connector login, daemons, IPC, temporary files, and logs remain separate for each account. Local plugin paths in shared configuration can be shared. Remote plugin cache directories cannot be linked wholesale: one account's sync could delete another account's bundles. These existing root account caches are not imported into new accounts.

If account credentials are atomically replaced with a regular file, the link is restored when there are no other wrapper clients and the file matches secrets. If secrets have not changed during this run and the user identity matches, both copies are backed up, the new original bytes are imported, and the link is restored. If both copies changed, no startup snapshot exists, or identities differ, repair stops without guessing from timestamps. The last exiting client or the next idle startup can perform safe repair; repair errors do not replace the native exit code.

Divergence needs a manual choice while every client of that account is closed; the error prints both paths and the command. Back up `<codex_home>/accounts/NAME/auth.json` and `<secrets_dir>/codex-NAME.json`, keep the wanted copy at `<secrets_dir>/codex-NAME.json`, then restore the link with `ln -sfn <secrets_dir>/codex-NAME.json <codex_home>/accounts/NAME/auth.json`. Use the absolute paths the error or `codex-as --show-config` prints: the launcher compares the link text with them, so a link through a symlinked alias of the same directory is refused. A missing link is restored the same way, unless a logout inside Codex removed it: those tokens are revoked, so move the secrets file aside and run `codex-as NAME` to log in again. **Do not delete the account marker or overwrite either file with an older snapshot.** See the [Codex verification record](codex-as.md) for source and test limitations.

## Claude configuration and security boundaries

Tokens are saved at `<secrets_dir>/claude-NAME`: 0600, a regular file owned by the current user, with a single hard link. Daily startup sets only `CLAUDE_CODE_OAUTH_TOKEN` for the child and inherits the existing `CLAUDE_CONFIG_DIR`. It does not force `~/.claude`, preserving existing sources of `.claude.json`, MCP, configuration, and trust state.

Explicit API key/OAuth token/authentication FD/alternative provider/endpoint/profile overrides, authentication helpers in settings, and organization restrictions are refused. Only variable names or sources are reported, never values. Passthrough of login/logout/setup-token, `--bare`, authentication configuration source overrides such as remote/SDK/managed-settings/project-config-root, and unaudited options is prohibited. Only the first command operand is identified using native argument boundaries; words inside prompts are not scanned. Native Claude dispatches an exact first `auth` operand to authentication subcommands even with `-p` or `--`, so it is refused. Required/optional/variadic argument values retain their native roles. This is a bounded local CLI option check, without a promise of compatibility with every command.

Managed policy is respected: automatic enrollment stops for nonempty managed caches, system policy, policy helpers, authentication bindings, or profile loading chains that cannot be preserved. Empty or missing caches do not cause this refusal. The managed cache is checked at every enrollment.

New account files are created exclusively; incorrect permissions, ownership, symlinks, or hard links are refused. Account names must be nonempty, contain only ASCII letters, digits, underscores, and hyphens, and not start with a hyphen, as in codex-as. Local format validation is not server authentication; expired credentials are not automatically deleted or replaced through login.

## Account metadata

Both launchers keep a small cache of what they last saw about each account, so a completion preview or a person can look it up offline: `codex-as --info NAME` and `claude-as --info NAME` (`-i NAME`). It lives in `${XDG_STATE_HOME:-~/.local/state}/coding-agent-kit/accounts/` (an absolute `XDG_STATE_HOME` only; the directory is created 0700 on first use and must stay an owned, real 0700 directory), never in `secrets_dir`: a configuration whose `secrets_dir` is that directory is refused, and one that reaches it through a link is never read or written. Each file is a 0600 JSON object, replaced atomically and only when its content changes, with one writer:

| File | Content | Written by |
| --- | --- | --- |
| `codex-NAME.json` | email, plan, and the last usage snapshot | `codex-as`, before and after each run |
| `claude-NAME.json` | the label | `claude-as` enrolment or `claude-as --label` |
| `claude-NAME.usage.json` | the last 5-hour and weekly windows | `helpers/statusline.cjs` |

No file holds a token, and no time in them comes from the launcher's clock: a Codex snapshot carries the time of the rollout line it came from, a Claude one the time the status line saw it.

```text
$ codex-as --info work
work (Codex)
email   alice@example.com
plan    plus
5h      23% used, resets 14:30 (in 2h 10m)
weekly  41% used, resets Mon 09:00 (in 3d 4h)
seen    12 min ago
```

Times are local. A window whose reset time has passed reads `reset since HH:MM`; missing data reads `not recorded yet`, and an account without a label reads `not set (claude-as --label NAME LABEL)`. `--labels` prints one line per account, `NAME<TAB>LABEL` (the email for Codex, the label for Claude) or the bare name when there is none; the zsh completions use it as descriptions. Both queries, like `--list`, create no directory, run no preflight or native CLI and never open a credential: they read only the state files, accept only an owned regular 0600 file with one link, at most 64 KiB, with unique keys, and print only the fields above with control characters removed. An unsafe file fails `--info` with one line that never quotes it; `--labels` shows that account's bare name.

- **Codex identity**: the email (the `email` claim, else the profile claim's `email`) and the plan (`chatgpt_plan_type`) from the account's `id_token`, decoded locally at each launch and again after the run. An API-key login records the plan `API key` and no email. The plan changes only when Codex refreshes the token.
- **Codex usage**: after native Codex exits, codex-as reads the session rollouts modified during the run, only regular files (never a FIFO, device or link), and keeps the newest `token_count` event with rate limits of the default `codex` family timestamped within the run. A rollout counts when its thread is the one the run resumes (`resume UUID`, `exec resume UUID`; a fork writes a new thread, so its source never counts), or when its `session_meta` names this account's ChatGPT user and workspace as the creator. The resumed thread comes first, then the newest rollouts: codex-as reads the tail (at most 1 MiB) of up to eight of this account's, after the first lines of at most 64 others. Of concurrent runs of one account the newest snapshot normally wins: each run reads the stored record only just before it replaces it. Times from the year 10000 on are ignored. The windows are named by their length (300 minutes is `5h`, 10080 is `weekly`). This is best effort, silent on failure and never changes the exit code. Known gaps: the rollout format is Codex's internal one, so a change leaves usage blank; a session switched inside Codex (`/resume`) is missed; compressed rollouts (`.jsonl.zst`) are skipped; past 64 other accounts' rollouts modified during the run, this account's own new session can be missed; and another account that resumes, during the run, a thread this account created can be counted here.
- **Claude label**: a new account asks `Label for this account (optional, e.g. your email):` after the confirmation and before any token exists, and saves the label only after the token. Control characters, more than 100 characters and anything containing `sk-ant-` are refused, so a pasted token is never stored. `claude-as --label NAME LABEL` sets the label of an existing account, `claude-as --label NAME` asks for it on `/dev/tty`, and an empty label clears it.
- **Claude usage**: Claude Code gives the status line its rate limits only for Pro and Max subscribers and only after the first API response; headless `claude-as NAME -p` runs render no status line and record nothing. claude-as passes its child `CODING_AGENT_KIT_USAGE_FILE` (the path of `claude-NAME.usage.json`), `CODING_AGENT_KIT_TOKEN_ID` (the first 16 hex digits of the token's SHA-256) and `CODING_AGENT_KIT_CLAUDE_PID` (its own pid, which stays the session's because it execs claude), and drops inherited values first. Claude Code passes `CLAUDE_CODE_OAUTH_TOKEN` to none of the commands it starts, the status line included, so `helpers/statusline.cjs` recognises the session by that pid: it records only when all three are set and the claude with that pid runs it, as its parent or behind one shell (`sh`, `bash`, `zsh`, `dash`, `ksh` or `fish`; read from `/proc` on Linux and with `ps` elsewhere). A claude started inside the session has a pid of its own and records nothing, whatever it inherited. A token that is visible all the same must hash to the identifier. The path must be absolute with a `claude-NAME.usage.json` name in an owned 0700 directory, and the numbers are valid; it writes when the windows change or the record is older than five minutes, never on Windows, and its printed status line is unchanged.

Privacy: emails and labels appear in completion previews, screen shares and recordings, and the output of `--info` lands in any agent transcript that runs it. No token is ever printed or stored there.

## Completion, installation, and concurrency

`install.sh` links the zsh completions into `PREFIX/share/zsh/site-functions`, which must be on `fpath` before `compinit` runs (with oh-my-zsh, before it is sourced; the installer prints the line), and the Bash completions into the bash-completion user directory, `${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-~/.local/share}/bash-completion}/completions`, where bash-completion autoloads them. For the first argument Bash runs `codex-as --list-accounts` or `claude-as --list-accounts` and zsh `--account-labels`, which adds each account's email or label as its description, so the launchers must be on `PATH`; the listing enumerates only safe filenames and metadata, without reading credentials, running the native CLI, or using the network, and an invalid configuration offers nothing. Corrupt contents are detected at startup. A first argument that starts with `-` completes the [own options](#own-options) instead. After `-i`, `--info` or `--account-info`, and `claude-as --label` or `--set-label`, the next argument completes account names the same way, in zsh in the group `Codex accounts` or `Claude accounts`; nothing else follows an own option. The completions call `--list-accounts` and `--account-labels`, the spellings every release from 0.3.0 on accepts, so a shell opened before an update or after a rollback keeps completing. After `NAME`, later arguments first complete the supported scope, then delegate to existing native completion when available.

fzf-tab, when present, uses the same candidates; normal Tab works without fzf. New terminals load the new completions; nothing appends source or PATH lines to rc files. Installation only creates links and triggers no real initialization, login, or model requests.

The launcher's own step/help headings are cyan, confirmations/warnings yellow, successful saves/initialization green, and errors red. By default, color is enabled only for the terminal receiving the message; redirected logs stay plain. Confirmations on a separate `/dev/tty` can still use color without affecting redirected Codex/Claude output. `CODING_AGENT_KIT_COLOR=auto|always|never` is supported (unset or any other value means `auto`); nonempty `NO_COLOR` or `TERM=dumb` takes priority and disables color. Native command output, completion account lists, and internal machine output are not colorized.

**Codex supports concurrent sessions across accounts and different sessions within one account.** Each wrapper and native child holds a shared account lease; credential repair requires an exclusive lease. Suspended processes or a child surviving an abnormal supervisor exit still block repair. Account lease files persist and are not deleted based on PIDs; per-session takeover leases are removed when the takeover ends. A shell inheriting `CODEX_HOME` from account A can launch B directly, but the inherited source must be a verified managed account directory. Before Codex 0.167.0, the native same-account OAuth refresh semaphore is in-process only and saves truncate and rewrite the secret, so several processes can refresh at once or briefly read partial JSON; 0.167.0 and newer serialize their own commits through native lock files and replace the secret atomically (see [symlink writeback verification](codex-as.md#symlink-writeback-verification)). The wrapper implements no additional OAuth. Direct CLI, desktop, and IDE clients do not participate in wrapper leases, and automatic exit repair cannot exclude them. Manual repair requires closing that account's clients. Processes are not automatically killed, quotas are not switched automatically, no background keepalive runs, and Claude tokens are not renewed.

## Validation

```sh
tests/codex-as.sh
tests/account-enrollment.sh
tests/config.sh
tests/account-info.sh
tests/standalone-e2e.sh
pre-commit run --all-files
```

Routine tests use temporary HOME directories, fictitious credentials, and fake CLIs. They cover Bash/zsh, TTY, stdin/JSON stdout, link repair and interrupted setup, root auth isolation, concurrent leases, signals, refresh writeback, incorrect permissions, completion secrecy, the configuration file, launcher location and `install.sh`. When Codex 0.161.0 or newer is installed, an additional real Codex offline app-server fixture checks legacy/paginated history, shared SQLite discovery, native writer locks and token refresh writeback through the account link against a local token endpoint, without login or model calls and with zero other requests. A missing or older Codex explicitly skips this check. Native protocol tests do not verify real-account server authorization, quotas or token lifetimes, and do not directly operate the terminal picker/`--last`.
