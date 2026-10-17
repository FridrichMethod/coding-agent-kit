# codex-as

See [account launchers](account-launchers.md) for daily use, first-time initialization, new-account login, and completion.

```sh
codex-as ayting
codex-as work resume --last
codex-as ayting exec --json "Check the tests"
```

Existing accounts launch directly. Missing accounts require confirmation, followed by native device login in an empty temporary `CODEX_HOME`; after saving complete JSON credentials, the original command continues. No sourcing or old /tmp initialization examples are needed.

## Daemon takeover

```sh
codex-as work resume SESSION_UUID
codex-as work resume
```

Ordinary `codex resume` can reuse a root daemon's loaded session. An account
launch uses an independent backend to keep authentication fixed, so the root
daemon must release a session before the account can write it; otherwise native
Codex shows "This conversation is open in another app". Before an interactive
resume starts, the launcher releases such sessions through native
archive/unarchive:

- An explicit `resume UUID` held by the root daemon is taken over without a
  prompt when the session and its loaded spawned agents report `idle` (or
  `systemError`). When any of them may be running (an active turn, any other
  status, or a backend that reports none), the launcher asks first. The status
  is a snapshot: a turn that starts just before the archive is still ended.
- `resume --last` opens one session: the most recently updated one native
  Codex lists (the state database first, then a scan), a top-level `cli` or
  `vscode` session of the launch directory
  (`-C`/`--cd` included) or, in a Git repository with linked worktrees, of the
  same place in another checkout (same common directory and prefix); any
  directory with `--all`, and `exec` and `appServer` sessions too with
  `--include-non-interactive`. Native Codex opens nothing when that session's
  rollout file is gone. The launcher asks the root daemon's `thread/list` the
  same question, with native's list of directories, and takes over only that
  session, when the daemon holds it and
  it is idle; if it may be running it stays and is named, and native Codex opens
  it into its conflict screen.
- The picker selects inside native Codex, which reports the choice nowhere
  outside its process, so the launcher cannot wait for it. Before it starts, it
  takes over the idle top-level sessions the root daemon holds that the picker
  lists by default: the same directory and source rules, the `openai` provider,
  a first message (native lists no empty session) and not ephemeral. It names
  them on stderr, including ones not selected afterwards. Native unarchive refreshes their modification time, so
  they move to the top of the picker. Sessions that may be running are left
  alone and named; selecting one shows the native conflict screen, and
  `codex-as NAME resume UUID` asks before ending it. Spawned agents follow their
  parent. A daemon-held session from another directory, reached by switching
  the picker to all sessions, also shows the conflict screen; R cannot help
  there, so quit and rerun with `--all` or `codex-as NAME resume UUID`.
- The directory match uses resolved paths and Git's common directory, so it can
  be slightly wider than native in rare layouts (a symlinked alias of the
  directory, bare-repository worktrees, or the Worktrees feature disabled).
  Linked worktrees need git 2.31 or newer on PATH; without it only the directory
  itself matches.
- A resume command with an option the launcher does not know takes nothing over
  and says so: clap rejects a typo such as `--lats` only after the launch. Its
  operand may then be that option's value, so a session name is not refused.
- A session held by any other process, such as another terminal or an IDE
  extension's own backend, is never touched; native Codex shows its conflict
  screen. Help, exec and fork never take over. A daemon-held session that is
  already archived is refused; unarchive it in the original client first.

Takeover ends the selected session and its spawned descendants in the root
daemon, so every client attached to it closes that session: Desktop, or a plain
`codex` TUI that attached to the daemon. Native archive/unarchive temporarily
changes their saved visibility. The launcher restores visible descendants and
keeps already archived descendants archived before starting the account backend.
Other daemon sessions remain loaded. SessionEnd hooks run, and the session's
modification time becomes the takeover time; runtime execution and hook effects
cannot be rolled back. The session UUID
and saved history remain available. Native writer locks still prevent a second
writer if shutdown is delayed or another client resumes the thread concurrently,
for example a Desktop that reopens the session right after the unarchive; native
Codex then shows its conflict screen. The lock probe briefly holds a free writer
lock, so a native client opening it at that instant also sees the conflict screen.

Before archive, a 0600 recovery journal is written and synced, including its
directory, under `~/.codex/accounts/.locks/UUID.handoff.json`; a per-UUID lease
next to it serializes takeovers and is removed when one ends. A timeout,
disconnect or interruption with unknown archive completion retains the journal.
The next `resume`, `fork` or `exec resume` of that UUID, or the next picker or
`--last` launch whatever its directory, finishes the recovery without a prompt.
Recovery only restores saved visibility: `resume UUID` then takes that session
over as usual, and the picker and `--last` take it over only by their own
rules; fork and exec resume never take over. A journal that does not parse was torn
before its archive started, so it is removed, even without a running daemon.
Root unarchive serializes behind any preceding native archive before descendant
visibility is restored. An explicit UUID, and `--last` for the session it
opens, do not launch the account backend while that session's recovery remains
incomplete, because native Codex would open it archived or open another one;
the picker warns and launches. Both point to the preserved records in
`~/.codex/accounts/.locks` when the daemon is not running. `--last` chooses its session after recovering the others, since
a restored session becomes the newest. Keep the journal until the helper
removes it. Declining the confirmation leaves session state untouched.

This flow accepts root daemons at 0.161.0 or newer; it needs a controlling
terminal only to confirm a session that may be running. It verifies the private
Unix socket and returned home/version without starting or stopping a daemon. A
daemon that fails these checks is not used, and native Codex shows its conflict
screen. Without a running daemon the picker and `--last` print nothing unless
recovery records are preserved; an explicit UUID held by another process warns
and launches.

## Version compatibility

There is no version allowlist. The launcher accepts `codex-cli 0.161.0` or newer,
including prereleases of later versions (a prerelease precedes its release, so
`0.161.0-alpha.1` is refused), and refuses version output it cannot parse.
0.161.0 is the oldest local release that passes the native suite below: 0.160.1
and 0.158.0 fail the daemon takeover fixture, and 0.155.1 also fails the refresh
writeback test and lacks `--no-daemon`. Runtime checks replace the former exact
pin:

- Arguments are forwarded unchanged after the launcher's own
  `-c cli_auth_credentials_store="file"` and `-c sqlite_home=...`; every command
  but `exec` also gets `-c check_for_update_on_startup=false` and `--no-daemon`
  (not repeated when the caller gives a root `--no-daemon`, which clap allows
  once). Subcommands and options added by later releases pass through; the
  launcher never parses `codex --help`.
- A help or version flag before any subcommand or prompt (`NAME --help`,
  `NAME -V`), `help`, `update` and `completion` are the same for every login,
  so they run as plain `codex` with the root `CODEX_HOME`, no added flags and no
  environment or configuration checks; the argument refusals below still apply
  first, so `codex-as NAME -c chatgpt_base_url=URL --help` is refused. A help flag later in the
  command line may belong to a program Codex runs, so it uses the account as
  usual. An account's own
  `update` would install under its `CODEX_HOME`, and the update prompt is
  turned off in account launches for the same reason.
- Refused: `login` and `logout`, which revoke the account's tokens (`login
  status` and their `--help` pass); `agents`, `queue`, `app` (a subcommand
  only on macOS and Windows; elsewhere a prompt word), any `remote-control`
  subcommand and `app-server daemon`, which need or start a daemon in the
  account home (foreground `remote-control` and other `app-server` modes
  pass); `--remote`, `--remote-auth-token-env`, `--oss` and
  `--local-provider`. Settings are checked by exact key: in `-c`, user and
  system configuration and the `-p` profile file, `model_provider` other than
  `openai`, endpoint or credential keys of `model_providers.openai`,
  `chatgpt_base_url` and `openai_base_url`; everywhere, project configuration
  included, `forced_login_method`, `forced_chatgpt_workspace_id` and the
  feature `use_agent_identity` (also through `--enable` and `features
  enable`). Native drops the provider and endpoint keys from project
  configuration, so they pass there. `-c` cannot set
  `cli_auth_credentials_store`, `sqlite_home` or
  `check_for_update_on_startup`, which the launcher sets itself, and a `-c`
  table or array is refused when it uses TOML 1.1 syntax (such as a trailing
  comma in an inline table) that this Python's `tomllib` cannot read.
- `-p NAME` and `--profile NAME` select profile v2, which layers
  `~/.codex/NAME.config.toml`: the launcher links that file into the account
  home, where native Codex reads it, and checks it like the user
  configuration (absolute paths, the shared `sqlite_home`). A legacy
  `profile` key is a native error, and `[profiles.*]` tables are never
  applied, so neither is checked. `mcp login` and `mcp logout` authenticate MCP
  servers and pass through. Words after a known subcommand are its own (a
  sandboxed command line, a review branch, a prompt), so they are never read
  as subcommands; before one, a root prompt does not end clap's subcommand
  recognition, so `codex-as NAME hello logout` is refused.
- Also refused, by name with values hidden: environment variables that native
  Codex reads as a login, token endpoint or billing scope (`CODEX_API_KEY`,
  `CODEX_ACCESS_TOKEN`, `OPENAI_API_KEY`, which realtime voice uses as its
  login, `OPENAI_ORGANIZATION`, `OPENAI_PROJECT`, the token and login URL
  overrides and the workload identity variables); a `CODEX_HOME` other than `~/.codex` or
  a managed account home; a `CODEX_SQLITE_HOME` or project `sqlite_home` other
  than the shared one; relative path fields in shared configuration; and an
  administrator `cli_auth_credentials_store` other than `file`. Variables no
  supported release reads as Codex's own login, such as `OPENAI_ORG_ID`, pass,
  and so does `OPENAI_BASE_URL`, which only routes the user's own API key for
  programs Codex runs.
- A future subcommand that changes authentication would also pass through; the
  exit repair described in [account launchers](account-launchers.md) detects a
  removed or replaced link, but a server-side revocation cannot be undone.
- That exit repair still detects an auth link replaced by a regular file and
  imports it only when the identity matches and secrets are unchanged. Either way
  the launcher warns, because the tested releases write through the link.
- Takeover accepts any backend version at or above the minimum; a protocol or
  semantic mismatch stops it, and once its journal is written that journal is
  kept until a later run recovers.

After `codex update`, run `tests/codex-as.sh`; it uses the `codex` on PATH, or
`CODEX_NATIVE_BINARY`. Checked 2026-10-17: releases 0.161.0, 0.162.1 and 0.163.0
pass it, and takeover also passes with a 0.161.0 or 0.162.1 daemon and a 0.163.0
client, and with a 0.163.0 daemon and a 0.161.0 client.

## Remaining differences from native Codex

`codex-as NAME ARGS...` aims to behave like logging in natively as NAME and
running `codex ARGS...`. What still differs:

- The refusals above, and the update prompt turned off in account launches.
- Account launches never use a daemon, so `queue` and `agents`, which need
  one, are refused. Commands that act on a session the root daemon holds, such
  as `archive`, `delete` or `unarchive`, act through a separate backend, so for
  a held session they meet its writer lock instead of reaching the running
  session. Only interactive resume takes sessions over.
- Taking a session over closes it in the root daemon and in Desktop, and moves
  it to the top of session lists.
- Session names are refused for resume, fork and exec resume; use a UUID,
  `--last` or the picker. Native name lookup filters by the account's path.
- Tools that Codex runs see `CODEX_HOME` and `CODEX_SQLITE_HOME` of the account
  view, which codex-as must set, so a nested plain `codex` uses that account.
- While native Codex runs, a SIGINT sent to the launcher alone is ignored: a
  terminal's Ctrl-C reaches native Codex directly, and forwarding would deliver
  it twice. Send it to the process group instead. Before native Codex starts,
  Ctrl-C cancels the launch. SIGTERM and SIGHUP are forwarded; one that arrives
  after native Codex exited takes effect once the exit repair is done. When
  native Codex dies of SIGINT, SIGTERM, SIGHUP, SIGKILL or SIGPIPE, the launcher
  dies of the same signal; other signals exit with 128 plus the signal number.
- A logout inside Codex (`/logout`) is native and revokes the account's tokens
  in `~/.secrets/codex-NAME.json`; the launcher cannot stop it, and warns at
  exit with the steps to log in again. A write through the link that names
  another ChatGPT user is kept and reported.

## Symlink writeback verification

The source audit below used `codex-cli 0.162.1`, official tag `rust-v0.162.1` and commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`. In `rust-v0.163.0`, `login/src/auth/manager.rs` is unchanged and `login/src/auth/storage.rs` is untouched; the native refresh test checks the behavior for every release it runs against.

- [storage.rs](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/login/src/auth/storage.rs#L214) opens auth.json with OpenOptions write/truncate/create, then writes and flushes. This path uses neither rename/atomic replacement nor O_NOFOLLOW, so it follows symlinks to update the target.
- AuthManager saves refreshes through this storage.save. Imports and backups preserve all original bytes; Codex manages the actual refresh format. The wrapper does not implement OAuth refresh.
- Each account's link stays fixed in its own home, and the root `auth.json` is never read or changed. Account repair leases cannot serialize native cross-process OAuth refresh: the native refresh semaphore is in-process, and file saves use truncate/write. Native multiple-client behavior and its limitations remain.
- Native `thread-writer-locks` take cross-process exclusive locks by UUID. This directory and `.tmp/rollout-maintenance.lock` are shared. Compaction scheduling markers using create-new/unlink remain separate for each account; the shared lock still serializes maintenance.
- New threads are paginated by default, with actual history in `thread_history_1.sqlite`; the complete SQLite home must therefore be shared. Sharing only sessions JSONL is insufficient. The wrapper fixes the explicit database directory override while preserving an existing absolute `sqlite_home`.
- Native configuration editing resolves the symlink writeback path, so config/value/write preserves the shared config link. Relative paths read from configuration instead resolve against the account home, so the wrapper rejects relative path fields in shared user configuration.
- Session-name lookup uses lexical prefix filtering by account path; cross-account resume is limited to UUIDs, `--last`, and the picker. Native remote plugin sync removes bundles not installed by the current account, so remote caches are isolated.
- The CLI supports a one-time `-c 'cli_auth_credentials_store="file"'` override. Every command but exec adds `--no-daemon` to avoid reusing stale authentication in a persistent app-server; exec remains a separate invocation.
- Native login clears existing authentication and may revoke it. Login therefore runs only in a temporary home without old credentials, never through logout/login in the shared home.
- Native subcommands can still appear after the root command's prompt; the wrapper also rejects login and logout in those positions. Prompts, option values and words after a known subcommand are preserved.
- Complete credentials are checked against the native JSON format before switching: NaN/Infinity, invalid non-null tokens, and any supplied but invalid last_refresh are rejected.

Static verification is combined with real offline app-server tests of each tested release. Fictitious fixtures verify legacy/paginated history resume across homes, SQLite list order, concurrent different UUIDs within/across accounts, rejection/release of writes to the same UUID, and shared config editing. There are no login or turn/start requests, and no HTTP requests besides the local token endpoint of the refresh test. `thread/list` checks discovery and ordering without directly operating the terminal picker/`--last`. A native refresh test sends `getAuthStatus` with `refreshToken` to a local token endpoint (`CODEX_REFRESH_TOKEN_URL_OVERRIDE`) and checks that the rotated tokens land in the same secrets inode with the account link intact. Fake Codex atomic replacement tests prove only wrapper logic; real-account authorization, quotas and server-side token lifetimes remain unverified.

Takeover also relies on these native protocol semantics: `thread/unsubscribe` only removes
the requesting client's subscription; `thread/archive` ends the selected runtime
and spawned subtree; `thread/unarchive` restores one saved thread. A fresh helper
cannot force unsubscribe another client. The takeover's Unix WebSocket management
client uses the native management identity and does not modify daemon login.
`thread/read` reports each loaded thread's `status` (`idle`, `active`, `notLoaded`,
`systemError`); anything but idle or systemError counts as possibly running.
Offline native fixtures exercise idle handoff without a prompt, the scoped picker
release, the root daemon naming the session an account backend's `--last` opens,
both history formats, visible children, archived grandchildren, a loaded spawned
agent and an unrelated loaded writer. Wrapper tests cover running and
status-less sessions, refusal, consent, partial errors, unknown completion, private
recovery metadata and retries. To check a root daemon binary that differs from the client, for
example one still running from before an update:

```sh
CODEX_NATIVE_DAEMON_BINARY=/path/to/codex-0.162.1 \
    python3 -I -B tests/test_codex_sharing_native.py \
    NativeSharingTests.test_daemon_takeover_preserves_uuid_history_and_other_threads
```

Manual repair requires closing that account's clients; native CLI/desktop/IDE clients do not participate in wrapper leases. codex-as never reads or changes the root `auth.json`.
