# codex-as

See [account launchers](account-launchers.md) for daily use, first-time initialization, new-account login, and completion. Paths below use the configured `<codex_home>` (default `~/.codex`) and `<secrets_dir>` (default `~/.secrets`); see [configuration](configuration.md).

```sh
codex-as ayting
codex-as work resume --last
codex-as ayting exec --json "Check the tests"
```

Existing accounts launch directly. Missing accounts require confirmation, followed by native device login in an empty temporary `CODEX_HOME`; after saving complete JSON credentials, the original command continues. No sourcing or separate initialization step is needed.

Apart from `-h` (`--help`), the launcher has its own queries, none starting Codex or opening a credential: `codex-as -l` (`--list`) prints the account names (what Bash completion offers), `codex-as --labels` prints them with their last-seen emails (the zsh descriptions), `codex-as -i NAME` (`--info NAME`) prints the last-seen email, plan and usage of one account, and `codex-as --show-config` prints the configuration file in use, the `codex` executable, `<codex_home>` and `<secrets_dir>`. Each but `-i NAME` is accepted only as the single argument; the aliases `--list-accounts`, `--account-labels` and `--account-info NAME` are accepted too, and any other dash word before `NAME` is refused with a hint that Codex options go after it (see [own options](account-launchers.md#own-options)).

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
directory, under `<codex_home>/accounts/.locks/UUID.handoff.json`; a per-UUID lease
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
`<codex_home>/accounts/.locks` when the daemon is not running. `--last` chooses its session after recovering the others, since
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

There is no version allowlist for unprotected launches (a launch with a usage reserve accepts only the reviewed codex-cli 0.167.2; see [protected mode](#protected-mode)). The launcher accepts `codex-cli 0.161.0` or newer,
including prereleases of later versions (a prerelease precedes its release, so
`0.161.0-alpha.1` is refused), and refuses version output it cannot parse.
0.161.0 is the oldest release with a recorded pass of the native suite below. In
the recorded runs of older releases, 0.160.1 and 0.158.0 fail its daemon
takeover fixture, and 0.155.1 also fails the refresh writeback test and lacks
`--no-daemon`. The launcher relies on these runtime rules instead:

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
  `<codex_home>/NAME.config.toml`: the launcher links that file into the account
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
  overrides and the workload identity variables); a `CODEX_HOME` other than `<codex_home>` or
  an initialized account home in `<codex_home>/accounts`; a `CODEX_SQLITE_HOME` or project `sqlite_home` other
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
`CODEX_NATIVE_BINARY`. Recorded passing runs, by the kit release whose code and
suite ran:

| Kit | Codex releases | Takeover daemon / client |
| --- | --- | --- |
| 0.7.7 | 0.167.2 | not run |
| 0.7.5 | 0.163.0, 0.166.0, 0.167.0 | 0.163.0 / 0.167.0, 0.167.0 / 0.163.0 |
| 0.1.0 | 0.161.0, 0.162.1, 0.163.0 | 0.161.0 / 0.163.0, 0.162.1 / 0.163.0, 0.163.0 / 0.161.0 |

A root daemon at 0.166.0 or newer needs kit 0.7.5 or newer. A takeover that an
older kit left pending completes when the same command runs again with the
current kit.

## Protected mode

A launch with a [usage reserve](account-launchers.md#usage-reserves) on runs interactive Codex in protected mode; the account launchers documentation describes what that means, what it requires and what it costs for both launchers. This section adds what is specific to Codex. A refusal reads:

```text
codex-as: usage reserve protection refused: REASON. (--reserve-5h off --reserve-weekly off launches unprotected)
```

It exits 1 before the Codex session starts and before any model request; the version check, the pre-launch refresh and the start-up quota read may have run, inside the boundary. `NAME --help`, `-h`, `--version`, `-V`, `help`, `update` and `completion` stay unprotected and unchanged: they run as plain `codex` in `<codex_home>` and send no model request. A subcommand's own help, such as `NAME resume --help`, stays unprotected too.

**Release.** Only codex-cli 0.167.2, the reviewed release, as `codex --version` reports it inside the boundary (see install and state below). Any other version is refused as unreviewed, an older one as well as a newer one, a suffix included; a newer one is named as beyond the newest reviewed release, so a Codex update stops protected launches until a kit patch reviews it.

**Arguments.** Only the interactive TUI, parsed as codex-cli 0.167.2 parses them (attached and `=` forms, subcommands after option values): `-c`/`--config`, `-m`/`--model`, `-C`/`--cd`, `-i`/`--image`, `--no-alt-screen`, `--no-daemon`, `--strict-config` and a prompt. A one-word prompt that names a subcommand needs `--` before it. Every other subcommand is refused, `resume` and `fork` included: inside the TUI `/new`, `/resume` and `/fork` work and stay behind the same boundary, gate, account and pause. Refused too: `-p`/`--profile`, `--remote`, `--oss`, feature, sandbox, approval, search and worktree options, `--add-dir`, and `-c` overrides of the keys protected mode sets itself (model providers and base URLs, the credential store, forced login and workspace, profiles, features, sandbox and permission settings, web search, tools, MCP servers, analytics, telemetry, memories, `sqlite_home`, the update check and network settings). `-C DIR` works as without reserves: Codex starts in the launch directory, and `DIR` is the workspace. The environment may not set `OPENAI_BASE_URL`.

**Account.** A ChatGPT login of a plan whose Codex windows protected mode governs (Plus, Pro, Team, Business, Enterprise, Edu and the related plans of the reviewed release; not Free, Go, usage-based or automation plans). API-key and FedRAMP logins are refused.

**Install and state.** codex-as never runs the native outside the boundary in a protected launch: it reads the release with `codex --version` inside a boundary of its own (only the boundary's devices writable, and only a held port that nothing serves as its exit), ends whatever that run left behind and bounds the read. A launch is refused when the `codex` it would run, a link on the way to it, or the interpreter of a `#!` script `codex` (for `#!/usr/bin/env NAME`, NAME as `PATH` finds it) lies inside a path the boundary can write (`the Codex executable PATH lies inside DIR, which the boundary can write; protected mode refuses a Codex that a protected session could replace, ...`), since the next plain `codex` would run a replacement unconfined; the standalone install under `<codex_home>/packages` is outside the writable set. A `#!/usr/bin/env` line is followed only as `#!/usr/bin/env NAME`, with `-S`, `VAR=VALUE` assignments other than `PATH` and `--`; any other form refuses. A launch is also refused when a `PATH` entry that lies inside a path the boundary can write (through every link), or a relative or empty one, comes before the entry that supplies `python3` (which `bin/codex-as` runs), `codex` (when the configuration names it by name) or that `#!/usr/bin/env` interpreter (`the PATH entry DIR comes before DIR2, which supplies NAME, and lies inside ROOT, which the boundary can write: ...`), because a session could put a command of that name there for the next launch or a plain `codex` to run unconfined; when no entry supplies the command, every entry counts. An account without a saved login is refused (`the account NAME has no saved login yet, ...; log in with one launch without reserves first`): the native login needs more network than the boundary allows, so log in with one launch without reserves first. Codex must already have created its databases in the shared SQLite home, which it can write but not create inside the boundary: the five it opens at every start (`start Codex once without reserves first`) and `thread_history_1.sqlite`, which it creates with its first thread (`send one prompt in a Codex session without reserves first`). A fresh `<codex_home>` therefore needs one ordinary prompt without reserves before its first protected launch.

**Model.** The reviewed models of the Codex quota family. The model of the user `config.toml`, or of the profile it selects, is checked at launch unless `-m` or `-c model=` replaces it; a model chosen in another way is refused by the gate per request. A profile that the user `config.toml` selects and that sets a key protected mode sets is refused (`the profile NAME that PATH selects sets KEYS, which protected mode sets itself`).

**Proxy.** The gate's proxy follows Codex's order: `HTTPS_PROXY`, then `ALL_PROXY` (see [protected mode](account-launchers.md#protected-mode)). A user `config.toml` that enables `features.respect_system_proxy` (or its older name `system_proxy_fallback`) is refused, and protected mode turns the feature off inside the session. On macOS, when neither `HTTPS_PROXY` spelling is set, the gate uses the system HTTPS proxy that Codex itself uses, read with the root-owned `/usr/sbin/scutil --proxy` outside the boundary (`HTTPSEnable`, `HTTPSProxy` and `HTTPSPort`, ahead of `ALL_PROXY`, with `NO_PROXY` but without the system exceptions list, as Codex applies it); automatic proxy configuration (a PAC file or WPAD) and a system HTTPS proxy without a plain host and port are refused (`set HTTPS_PROXY to the proxy to use`).

**Managed requirements.** A launch is refused when the account's managed requirements forbid what protected mode sets: application network restrictions (they allow only HTTPS and WebSocket to listed hosts, which denies the plain-HTTP loopback gate), network controls such as `experimental_network`, `network_proxy` or `permissions.network` unless disabled, pins of the model provider, data residency, `chatgpt_base_url`, the credential store, `sqlite_home` or the update check, a config server elsewhere, `allowed_sandbox_modes` without `danger-full-access`, a feature forced on that protected mode turns off, agent identity, a managed configuration that sets what protected mode sets itself, or requirements that cannot be read. Such an account is refused before any model request: by the launch checks, or when the configuration bundle that the pre-launch refresh or the start-up quota read fetches forbids protected mode. The refusal names the requirement, and its last words say that `--reserve-5h off --reserve-weekly off` launches the account unprotected, the only way to run it. The sources are the local `requirements.toml`, `/etc/codex/managed_config.toml`, macOS managed preferences (including MDM `config_toml_base64`) and the account's cached cloud configuration bundle, which counts only while Codex would use it: the same login, not expired. A refusal names the source; for the bundle cache, one unprotected launch refreshes it. Links to `config.toml` and requirement files are followed. A configuration bundle that forbids protected mode and arrives while a session runs pauses it until Codex fetches one without that requirement; a passing quota read, F12 and a restart of the request gate do not lift it (each run gate keeps its latest verdict in a private record outside the boundary, which it opens once and writes in place; a restarted gate that cannot open or read that record keeps the session paused until such a bundle arrives).

**Start.** Before every protected launch Codex refreshes the credential natively, behind a refresh gate that passes only `auth.openai.com:443` and the configuration bundle; this rotates the refresh token on every protected launch, and Codex may refresh twice. Concurrent protected launches of one account take turns at it under `<codex_home>/accounts/.locks/.refresh-NAME.lock`. A fresh App Server read inside the boundary then binds the gate to the account's credential and must pass both reserves.

**Inside the session.** Codex runs with `--no-daemon`, every model, usage and realtime URL at the gate, and its own sandbox off (`danger-full-access`). Processes inside write only beneath the workspace, the account home `<codex_home>/accounts/NAME`, the shared state Codex writes through it (the directories `sessions`, `archived_sessions`, `rollout-migrations` and `thread-writer-locks` and the files `history.jsonl`, `session_index.jsonl` and `.tmp/rollout-maintenance.lock` in `<codex_home>`), the release's six databases in the shared SQLite home as files (each `NAME.sqlite` with its `-wal` and `-shm`, which the launcher creates with the database's mode when they are missing) and a private `TMPDIR` removed at exit; the refresh can also write `<secrets_dir>`. Everything else in `<codex_home>` stays read-only inside the session: `packages/` (where the Codex standalone installer puts the native), `config.toml`, `hooks.json`, `mcp.json`, `skills`, `rules`, `agents`, `themes`, `worktrees`, `memories` and `memories_v2` (protected mode turns memories off), the rest of `.tmp` (where plain Codex keeps its plugin checkout and marketplaces), the other accounts' homes and the root `auth.json`. Codex therefore cannot save settings to `config.toml` or install its system skills during a protected session; it logs an error and goes on. Each of those shared directories and files in `<codex_home>` must be a plain directory or regular file of the user, never a link (`PATH is a link, not a directory of this user: protected mode makes it writable, ...`): every launch resolves them again, and on macOS a session could otherwise replace one with a link whose target the next launch would make writable. A workspace that is HOME or an ancestor of it is refused (`start from a project directory`). `TMPDIR` must lie outside every path the boundary can write and outside the boundary's devices (`/dev/shm` and `/dev/pts` on Linux, `/dev` on macOS), so `TMPDIR=/dev/shm` is refused. The TUI loses the proxy variables. The gate forwards the configuration bundle (`GET /backend-api/wham/config/bundle`) while running and paused, and refuses compaction, realtime, WebSocket, memories, search and image routes. A refusal is HTTP 503, which Codex shows as an error. A quota reader is confined anew for every read; it refuses, and the session pauses, when a writable path nested in another, such as a custom SQLite home inside the workspace, has been replaced by a link, also while the reader is being confined. Codex, its tools and the quota readers keep the caller's umask, as in an unprotected launch.

**Kit processes.** The gate, the monitor and its quota readers run outside the boundary, so nothing they load code from may be writable from inside: they run the base interpreter of the Python that runs codex-as, and a launch is refused when that interpreter, its prefix, a module path entry or the kit's directory lies inside a writable path (`run codex-as with a Python and a kit installed outside the workspace and the Codex homes`). The launcher itself runs as the `python3` found on `PATH`, started as `python3 -I -B -S`, so no site-packages or `.pth` file of that Python runs in it, and a launch is refused when that `python3` (by the name found on `PATH`, through every link), its prefix or a module path entry lies inside a writable path (`the launcher's Python PATH lies inside DIR, which the boundary can write; start codex-as with a python3 outside the workspace and the Codex homes first on PATH (deactivate a workspace virtual environment)`), since a protected session could replace it under the next launch, and, as install and state says, when a `PATH` entry the boundary can write, or a relative or empty one, comes before the one that supplies it. A virtual environment outside the workspace and the Codex homes first on `PATH` works. The readers get an allowlisted environment (`CODEX_CA_CERTIFICATE` included), and `SSL_CERT_FILE` and `SSL_CERT_DIR` reach the gate. While the run lasts, commands the launcher runs by name resolve only in the system directories.

**After the run.** The credential link repair runs after every protected run, also after a refusal that follows the refresh. Protected mode confines the invocation, not later use of what it wrote: a file a boundary process wrote runs outside any boundary when another tool acts on it later, so everything the session could write stays untrusted after it. In the workspace that includes Git hooks and `.git/config` entries such as `core.hooksPath`, direnv's `.envrc` and editor or workspace settings, and a workspace in a sync root carries such files to other machines. The account home is included: its `.env`, plugin cache, profile files and, on Linux, `tmp/arg0-linux` affect later unprotected launches of the account; see [what protected mode costs](account-launchers.md#what-protected-mode-costs).

Limits:

- Only the profile that the user `config.toml` selects is checked for protected keys. A profile that a project `.codex/config.toml` or `/etc/codex/config.toml` selects is not, and then fails inside the boundary with connection or sandbox errors instead of a refusal.
- `SSL_CERT_FILE` and `SSL_CERT_DIR` are passed to the gate even when they lie inside the writable set.
- The code-reach check does not follow links inside a checked module directory that point elsewhere, editable-install finders that map outside the module path, or the base installation's OpenSSL configuration and provider modules. It, the native check and the `PATH` order check compare paths as they are spelled, so spellings that differ only in case on a case-insensitive APFS volume pass them.
- The native check follows one `#!` line: an interpreter that is itself a script is not followed further, and a `#!` line whose interpreter is not an absolute path is not followed at all (claude-as follows up to four nested interpreters and refuses a relative one).
- `bin/codex-as` runs `dirname`, `readlink` and `python3` by name from the caller's `PATH` before any check, so whatever a `PATH` lookup of those names finds runs unconfined before the launch can refuse. The checks above cover `python3`: the entries searched before the one that supplies it, and the program by the name it ran, its prefixes and its module path. They do not cover `dirname` and `readlink`, nor a `python3` wrapper script in the supplying entry, after which the launcher's Python names what the script runs. Keep the writable set's directories off `PATH`, or after the system directories, while you use protected mode (see [what protected mode costs](account-launchers.md#what-protected-mode-costs)).

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
  in `<secrets_dir>/codex-NAME.json`; the launcher cannot stop it, and warns at
  exit with the steps to log in again. A write through the link that names
  another ChatGPT user is kept and reported.

## Symlink writeback verification

The source audit below starts from `codex-cli 0.162.1`, official tag `rust-v0.162.1` and commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`; the bullets name the later releases that change it. In `rust-v0.163.0`, `login/src/auth/manager.rs` and `login/src/auth/storage.rs` are unchanged; the native refresh test checks the behavior for every release it runs against.

- [storage.rs](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/login/src/auth/storage.rs#L214) opens auth.json with OpenOptions write/truncate/create, then writes and flushes. This path uses neither rename/atomic replacement nor O_NOFOLLOW, so it follows symlinks to update the target.
- AuthManager saves refreshes through this storage.save. Imports and backups preserve all original bytes; Codex manages the actual refresh format. The wrapper does not implement OAuth refresh.
- From 0.167.0 ([#58251](https://github.com/openai/codex/commit/582a8812e0)) the save is atomic instead. It resolves the link chain to the target, writes a private (0600) `.auth-*` temporary file in the target's directory and renames it over the target. The link is kept and the secret's inode changes. It stays an owned 0600 regular file with one link, which is all the launcher checks. A file that is read-only or hard-linked is refused rather than split. Since 0.166.0 a commit holds `auth.lock` in the account home. Since 0.167.0 it also holds `codex-NAME.json.lock`, a lock file that stays beside the secret, and it saves a refresh only while the stored credentials are unchanged. Neither lock name matches `codex-NAME.json`, so neither is listed as an account.
- Each account's link stays fixed in its own home, and the root `auth.json` is never read or changed. Account repair leases do not serialize native cross-process OAuth refresh. Before 0.167.0 the native refresh semaphore is in-process only and file saves use truncate/write; newer releases serialize their own commits through the locks above. Native multiple-client behavior and its limitations remain.
- Native `thread-writer-locks` take cross-process exclusive locks by UUID. This directory and `.tmp/rollout-maintenance.lock` are shared. Compaction scheduling markers using create-new/unlink remain separate for each account; the shared lock still serializes maintenance.
- New threads are paginated by default, with actual history in `thread_history_1.sqlite`; the complete SQLite home must therefore be shared. Sharing only sessions JSONL is insufficient. The wrapper fixes the explicit database directory override while preserving an existing absolute `sqlite_home`.
- Native configuration editing resolves the symlink writeback path, so config/value/write preserves the shared config link. Relative paths read from configuration instead resolve against the account home, so the wrapper rejects relative path fields in shared user configuration.
- Session-name lookup uses lexical prefix filtering by account path; cross-account resume is limited to UUIDs, `--last`, and the picker. Native remote plugin sync removes bundles not installed by the current account, so remote caches are isolated.
- The CLI supports a one-time `-c 'cli_auth_credentials_store="file"'` override. Every command but exec adds `--no-daemon` to avoid reusing stale authentication in a persistent app-server; exec remains a separate invocation.
- Native login clears existing authentication and may revoke it. Login therefore runs only in a temporary home without old credentials, never through logout/login in the shared home.
- Native subcommands can still appear after the root command's prompt; the wrapper also rejects login and logout in those positions. Prompts, option values and words after a known subcommand are preserved.
- Complete credentials are checked against the native JSON format before switching: NaN/Infinity, invalid non-null tokens, and any supplied but invalid last_refresh are rejected.

Static verification is combined with real offline app-server tests of each tested release. Fictitious fixtures verify legacy/paginated history resume across homes, SQLite list order, concurrent different UUIDs within/across accounts, rejection/release of writes to the same UUID, and shared config editing. There are no login or turn/start requests, and no HTTP requests besides the local token endpoint of the refresh test. `thread/list` checks discovery and ordering without directly operating the terminal picker/`--last`. A native refresh test sends `getAuthStatus` with `refreshToken` to a local token endpoint (`CODEX_REFRESH_TOKEN_URL_OVERRIDE`) and checks four things: the rotated tokens land in the linked secret (in place before 0.167.0, by atomic replacement since), the account link is intact, the secret is still an owned 0600 file with one link, and no credential copy is left beside either one, apart from the native lock files. Fake Codex atomic replacement tests prove only wrapper logic; real-account authorization, quotas and server-side token lifetimes remain unverified.

Takeover also relies on these native protocol semantics: `thread/unsubscribe` only removes
the requesting client's subscription; `thread/archive` ends the selected runtime
and spawned subtree; `thread/unarchive` restores one saved thread before 0.166.0.
Since 0.166.0 ([#57635](https://github.com/openai/codex/commit/6ba3a84680)) it also
restores the thread's archived spawned descendants, even one archived on its own.
Restore therefore works the same under either behavior. After unarchiving the
session, it re-reads the subtree before each request and archives or unarchives each
spawned agent whose state differs from the journal. It repeats until the subtree
matches; the spawn depth bounds the passes. It never archives an agent that the
root daemon has loaded, together with any of its agents. A stale record must not
end running work, so that agent stays visible and the launcher warns. Native delete
removes a thread together with its spawned subtree. A saved agent that has left the
session's subtree and passes the deletion test below has nothing to restore, so
recovery skips it. One merely missing from the listing keeps the record. A rollout
removed by hand, or by a native delete interrupted after removing it, leaves the
thread's metadata behind: the subtree listing still names the thread, and archive and
unarchive, which move the rollout, refuse it. When one of them refuses an agent that
passes the deletion test, recovery leaves that agent as it is and warns. A record
whose session passes the test is removed with a warning, after the spawned agents
still listed under it (a rollout removed by hand leaves them) get their saved
visibility back. The test needs no rollout of the thread left under `sessions/` or
`archived_sessions/`, linked folders below them included, and a daemon that
either refuses to read it (native delete removes the rollouts before it succeeds) or
reads it from metadata whose rollout path no longer exists. Native refusals all
share one error code, so a refusal alone keeps the record, and so does a path or
folder that cannot be checked, which the recovery message then names. The
search covers `sessions/` both before and after `archived_sessions/`, so a rollout
that a concurrent unarchive moves is still found.
A fresh helper
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
