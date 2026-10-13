# codex-as

See [account launchers](account-launchers.md) for daily use, first-time initialization, new-account login, and completion.

```sh
codex-as ayting
codex-as work resume --last
codex-as ayting exec --json "Check the tests"
```

Existing accounts launch directly. Missing accounts require confirmation, followed by native device login in an empty temporary `CODEX_HOME`; after saving complete JSON credentials, the original command continues. No sourcing or old /tmp initialization examples are needed.

## Symlink writeback verification

The locally audited version, `codex-cli 0.162.1`, corresponds to official tag `rust-v0.162.1` and commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`.

- [storage.rs](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/login/src/auth/storage.rs#L214) opens auth.json with OpenOptions write/truncate/create, then writes and flushes. This path uses neither rename/atomic replacement nor O_NOFOLLOW, so it follows symlinks to update the target.
- AuthManager saves refreshes through this storage.save. Imports and backups preserve all original bytes; Codex manages the actual refresh format. The wrapper does not implement OAuth refresh.
- Each account's link stays fixed in its own home; the global authentication link is no longer switched while processes run. Account repair leases cannot serialize native cross-process OAuth refresh: the native refresh semaphore is in-process, and file saves use truncate/write. Native multiple-client behavior and its limitations remain.
- Native `thread-writer-locks` take cross-process exclusive locks by UUID. This directory and `.tmp/rollout-maintenance.lock` are shared. Compaction scheduling markers using create-new/unlink remain separate for each account; the shared lock still serializes maintenance.
- New threads are paginated by default, with actual history in `thread_history_1.sqlite`; the complete SQLite home must therefore be shared. Sharing only sessions JSONL is insufficient. The wrapper fixes the explicit database directory override while preserving an existing absolute `sqlite_home`.
- Native configuration editing resolves the symlink writeback path, so config/value/write preserves the shared config link. Relative paths read from configuration instead resolve against the account home, so the wrapper rejects relative path fields in shared user configuration.
- Session-name lookup uses lexical prefix filtering by account path; cross-account resume is limited to UUIDs, `--last`, and the picker. Native remote plugin sync removes bundles not installed by the current account, so remote caches are isolated.
- The CLI supports a one-time `-c 'cli_auth_credentials_store="file"'` override. Interactive, resume, and fork commands add `--no-daemon` to avoid reusing stale authentication in a persistent app-server; exec remains a separate invocation.
- Native login clears existing authentication and may revoke it. Login therefore runs only in a temporary home without old credentials, never through logout/login in the shared home.
- Native subcommands can still appear after the root command's prompt; the wrapper also rejects login/logout in those positions. Prompts and option values in exec, resume, and fork are preserved.
- Complete credentials are checked against the native JSON format before switching: NaN/Infinity, invalid non-null tokens, and any supplied but invalid last_refresh are rejected.

Static verification is combined with real `0.162.1` offline app-server tests. Fictitious fixtures verify legacy/paginated history resume across homes, SQLite list order, concurrent different UUIDs within/across accounts, rejection/release of writes to the same UUID, and shared config editing. There are no login, turn/start, or HTTP requests. `thread/list` checks discovery and ordering without directly operating the terminal picker/`--last`. Fake Codex refresh/atomic replacement tests prove only wrapper logic; real-account authorization, quotas, and OAuth refresh remain unverified. A version change causes startup refusal until it is audited again.

Migration and manual recovery require closing related clients; native CLI/desktop/IDE clients do not participate in wrapper leases. Daily account startup does not change the root default authentication entry point.
