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
- The picker and `--last` select inside native Codex, after the launcher has
  run. So before they start, the launcher takes over every idle top-level
  session the root daemon holds and names them on stderr, including sessions
  that are not selected afterwards; this wider reach is deliberate, so that the
  selection never meets a daemon-held session. Sessions that may be
  running are left alone and named; selecting one shows the native conflict
  screen, and `codex-as NAME resume UUID` asks before ending it. Spawned agents
  follow their parent.
- A session held by any other process, such as another terminal or an IDE
  extension's own backend, is never touched; native Codex shows its conflict
  screen. Help, exec and fork never take over.

Takeover ends the selected session and its spawned descendants in the root
daemon, so every client attached to it closes that session: Desktop, or a plain
`codex` TUI that attached to the daemon. Native archive/unarchive temporarily
changes their saved visibility. The launcher restores visible descendants and
keeps already archived descendants archived before starting the account backend.
Other daemon sessions remain loaded. SessionEnd hooks and timestamp changes may
occur; runtime execution and hook effects cannot be rolled back. The session UUID
and saved history remain available. Native writer locks still prevent a second
writer if shutdown is delayed or another client resumes the thread concurrently,
for example a Desktop that reopens the session right after the unarchive; native
Codex then shows its conflict screen. The lock probe briefly holds a free writer
lock, so a native client opening it at that instant also sees the conflict screen.

Before archive, a 0600 recovery journal is written and synced, including its
directory, under `~/.codex/accounts/.locks/UUID.handoff.json`. A timeout, disconnect
or interruption with unknown archive completion retains that journal. The next
`resume UUID` of that session, or the next picker or `--last` launch, finishes the
recovery without a prompt. Root unarchive serializes behind any preceding native
archive before descendant visibility is restored. An explicit `resume UUID` does
not launch the account backend while its recovery remains incomplete; the picker
and `--last` warn and launch, and name the preserved records when the daemon is
not running. Keep the journal until the helper removes it.
Declining the confirmation leaves session state untouched.

This flow accepts root daemons at 0.161.0 or newer; it needs a controlling
terminal only to confirm a session that may be running. It verifies the private
Unix socket and returned home/version without starting or stopping a daemon. A
daemon that fails these checks is not used, and native Codex shows its conflict
screen. Without a running daemon the launcher prints nothing unless recovery
records are preserved.

## Version compatibility

There is no version allowlist. The launcher accepts `codex-cli 0.161.0` or newer,
prereleases included, and refuses version output it cannot parse. 0.161.0 is the
oldest local release that passes the native suite below: 0.160.1 and 0.158.0 fail
the daemon takeover fixture, and 0.155.1 also fails the refresh writeback test and
lacks `--no-daemon`. Runtime checks replace the former exact pin:

- Arguments are forwarded unchanged, including subcommands and options added by
  later releases; the launcher never parses `codex --help`. It refuses only
  `login` and `logout`, which revoke the account's tokens, `update`, whose
  installer writes under `CODEX_HOME`, and profile, remote, local-provider and
  auth/provider configuration overrides. `mcp login` and `mcp logout`
  authenticate MCP servers and pass through. A future subcommand that changes
  authentication would also pass through; the exit repair below detects a
  replaced link, but a server-side revocation cannot be undone.
- The exit repair below still detects an auth link replaced by a regular file and
  imports it only when the identity matches and secrets are unchanged. Either way
  the launcher warns, because the tested releases write through the link.
- Takeover accepts any backend version at or above the minimum; a protocol or
  semantic mismatch stops it with the recovery journal kept.

After `codex update`, run `tests/codex-as.sh`; it uses the `codex` on PATH, or
`CODEX_NATIVE_BINARY`. Releases 0.161.0, 0.162.1 and 0.163.0 pass it, and takeover
also passes with a 0.162.1 daemon and a 0.163.0 client and the reverse.

## Symlink writeback verification

The source audit below used `codex-cli 0.162.1`, official tag `rust-v0.162.1` and commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`. In `rust-v0.163.0`, `login/src/auth/manager.rs` is unchanged and `login/src/auth/storage.rs` is untouched; the native refresh test checks the behavior for every release it runs against.

- [storage.rs](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/login/src/auth/storage.rs#L214) opens auth.json with OpenOptions write/truncate/create, then writes and flushes. This path uses neither rename/atomic replacement nor O_NOFOLLOW, so it follows symlinks to update the target.
- AuthManager saves refreshes through this storage.save. Imports and backups preserve all original bytes; Codex manages the actual refresh format. The wrapper does not implement OAuth refresh.
- Each account's link stays fixed in its own home, and the root `auth.json` is never read or changed. Account repair leases cannot serialize native cross-process OAuth refresh: the native refresh semaphore is in-process, and file saves use truncate/write. Native multiple-client behavior and its limitations remain.
- Native `thread-writer-locks` take cross-process exclusive locks by UUID. This directory and `.tmp/rollout-maintenance.lock` are shared. Compaction scheduling markers using create-new/unlink remain separate for each account; the shared lock still serializes maintenance.
- New threads are paginated by default, with actual history in `thread_history_1.sqlite`; the complete SQLite home must therefore be shared. Sharing only sessions JSONL is insufficient. The wrapper fixes the explicit database directory override while preserving an existing absolute `sqlite_home`.
- Native configuration editing resolves the symlink writeback path, so config/value/write preserves the shared config link. Relative paths read from configuration instead resolve against the account home, so the wrapper rejects relative path fields in shared user configuration.
- Session-name lookup uses lexical prefix filtering by account path; cross-account resume is limited to UUIDs, `--last`, and the picker. Native remote plugin sync removes bundles not installed by the current account, so remote caches are isolated.
- The CLI supports a one-time `-c 'cli_auth_credentials_store="file"'` override. Interactive, resume, and fork commands add `--no-daemon` to avoid reusing stale authentication in a persistent app-server; exec remains a separate invocation.
- Native login clears existing authentication and may revoke it. Login therefore runs only in a temporary home without old credentials, never through logout/login in the shared home.
- Native subcommands can still appear after the root command's prompt; the wrapper also rejects login, logout and update in those positions. Prompts and option values in exec, resume, fork and mcp are preserved.
- Complete credentials are checked against the native JSON format before switching: NaN/Infinity, invalid non-null tokens, and any supplied but invalid last_refresh are rejected.

Static verification is combined with real offline app-server tests of each tested release. Fictitious fixtures verify legacy/paginated history resume across homes, SQLite list order, concurrent different UUIDs within/across accounts, rejection/release of writes to the same UUID, and shared config editing. There are no login or turn/start requests, and no HTTP requests besides the local token endpoint of the refresh test. `thread/list` checks discovery and ordering without directly operating the terminal picker/`--last`. A native refresh test sends `getAuthStatus` with `refreshToken` to a local token endpoint (`CODEX_REFRESH_TOKEN_URL_OVERRIDE`) and checks that the rotated tokens land in the same secrets inode with the account link intact. Fake Codex atomic replacement tests prove only wrapper logic; real-account authorization, quotas and server-side token lifetimes remain unverified.

Takeover also relies on these native protocol semantics: `thread/unsubscribe` only removes
the requesting client's subscription; `thread/archive` ends the selected runtime
and spawned subtree; `thread/unarchive` restores one saved thread. A fresh helper
cannot force unsubscribe another client. The takeover's Unix WebSocket management
client uses the native management identity and does not modify daemon login.
`thread/read` reports each loaded thread's `status` (`idle`, `active`, `notLoaded`,
`systemError`); anything but idle or systemError counts as possibly running.
Offline native fixtures exercise idle handoff without a prompt, the picker/`--last`
release, both history formats, visible children, archived grandchildren, a loaded
spawned agent and an unrelated loaded writer. Wrapper tests cover running and
status-less sessions, refusal, consent, partial errors, unknown completion, private
recovery metadata and retries. To check a root daemon binary that differs from the client, for
example one still running from before an update:

```sh
CODEX_NATIVE_DAEMON_BINARY=/path/to/codex-0.162.1 \
    python3 -I -B tests/test_codex_sharing_native.py \
    NativeSharingTests.test_daemon_takeover_preserves_uuid_history_and_other_threads
```

Manual repair requires closing that account's clients; native CLI/desktop/IDE clients do not participate in wrapper leases. codex-as never reads or changes the root `auth.json`.
