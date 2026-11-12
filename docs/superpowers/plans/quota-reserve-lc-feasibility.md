# LC: live driver for Claude HLogin and N1 (one named alias)

Node LC, base `afffc23`. Research only: no launcher, completion, helper,
installer or suite change. Code and tests live in
[quota-reserve-probes/lc/](quota-reserve-probes/lc/). G0 is not passed;
nothing here is production capability or release evidence.

This node never ran the driver against a real account. No real login,
credential read, account selection or real-provider request occurred. The
coordinator alone runs it, within the named-alias permission recorded in the
ledger (HLogin, N1-native and N1-bearer, one attempt each, no retry). Every
account, token, quota and native reply in this node is fabricated, and the
tests reach only loopback fixtures.

Evidence labels: **N** observation in this node (Linux x86_64, Python 3.15
for the tests, fake native and loopback fixtures only); **S** inspected
source or documentation; **I** inference; **U** unproved.

## 1. Design

[live_claude.py](quota-reserve-probes/lc/live_claude.py) (`python3 -I -B`,
stdlib only, Linux only) composes, by path and read-only:

- the kit loader `lib/coding-agent-kit/config.py`: `load()` resolves
  `secrets_dir` exactly as both launchers do (`CODING_AGENT_KIT_CONFIG`,
  else `$XDG_CONFIG_HOME/coding-agent-kit/config.toml`, else
  `$HOME/.config/coding-agent-kit/config.toml`, else the default
  `~/.secrets`; values normalized by pathlib, so never a trailing slash);
- the reviewed QC harness [n1_harness.py](quota-reserve-probes/qc/n1_harness.py)
  (`AccountPaths`, `create_pending`, `load_registration`, `read_metadata`,
  `mark_ready`, `check_environment`, `check_effective_policy`,
  `default_workspace_parent`, `check_workspace_ancestors`, `run_n1`) and,
  through the harness's own instances, [bearer.py](quota-reserve-probes/qc/bearer.py)
  (`check_store_selection`, `read_linux_bearer`) and
  [usage.py](quota-reserve-probes/qc/usage.py) (`fetch`,
  `fetch_profile_identity`, production endpoints and TLS context).

It adds no login, credential, quota, transport or policy logic. Its own code
is argument handling, the result-directory rules, the one-shot attempt
files, a preflight that calls the harness's checks before anything is
consumed, the bearer-step gate on the n1 result, output allowlisting and
signal handling.

| Subcommand | Effect | Network or spawn |
| --- | --- | --- |
| `pending --alias NAME --out DIR` | `create_pending`; prints the two user commands | none |
| `ready --alias NAME --out DIR [--confirm]` | reads the two UUIDs; `mark_ready` only with `--confirm` | none |
| `n1 --alias NAME --out DIR --binary PATH [--no-tunnel]` | one `run_n1` | native child (one-shot) |
| `bearer --alias NAME --out DIR` | one bearer read, one usage GET, one profile GET | wrapper HTTPS (one-shot) |

`pending` prints, with the configured claude command (`claude` by default)
and the config directory quoted by `shlex.quote` and spelled exactly:

```text
CLAUDE_CONFIG_DIR=<secrets_dir>/native-claude-accounts/NAME/config claude auth login
CLAUDE_CONFIG_DIR=<secrets_dir>/native-claude-accounts/NAME/config claude auth status
```

A second `pending` refuses (`account_directory_exists`), reports the
registration state and prints no login command, so no second login is ever
implied.

`ready` reports only `account_uuid_present`, `organization_uuid_present`
and `server_policy_present` (a server-managed policy cache after login
means n1 will refuse). Without `--confirm` it refuses
(`confirmation_required`) and changes nothing. With `--confirm` (the user
confirmed to the coordinator the account and organization that
`claude auth status` displayed) and both UUIDs present, it calls
`mark_ready` with the values it read; `mark_ready` re-reads them and writes
the registration atomically. A missing UUID refuses (`identity_missing`)
before `mark_ready`, which would otherwise compare `None` with `None`.

`n1` runs these local refusals first, through the harness's functions, so a
refusal consumes nothing: registration ready; caller environment
(`conflicting_environment`, with the offending variable names listed);
every policy source (`check_effective_policy`, platform from
`detect_platform`); binary absolute and executable; SHA-256 of the resolved
binary equal to native 2.1.322
(`bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478`, else
`native_version_unreviewed`); local identity equal to the registration; a
private `XDG_RUNTIME_DIR` (the N1 draft's precondition; the driver refuses
instead of falling back to the shared temporary directory); no `.git` or
`.claude` above the workspace. Then it creates `DIR/n1.attempt` and calls
`run_n1(resolved_binary, paths, caller_env, allow_requery=True,
tunnel_connector=..., workspace_parent=XDG_RUNTIME_DIR)` with the module's
default deadlines. The resolved path is passed so that the hashed file is the
spawned file (an auto-update that moves the `claude` symlink cannot swap it).
The accounting tunnel is on by default (CONNECT only to
`api.anthropic.com:443` and `platform.claude.com:443`, upstream connect
timeout 10 s); `--no-tunnel` turns it off. Both are within the reviewed
draft, which names the tunnel as optional. With the tunnel, native sees
`HTTPS_PROXY`, which is also the condition under which QC observed native's
auxiliary and usage traffic (S, QC section 4).

`bearer` requires an n1 result in the same DIR whose native run completed
(`fresh_windows`, `inconclusive` or `unavailable`) with zero model events,
`cost_zero` in every reply, an empty process group, identity unchanged
before and after, `server_policy_written` false and the reviewed binary
hash. `run_n1` classifies `inconclusive` before it checks session cost, so
the gate checks cost, model events and reaping itself (I: a broader gate
than `fresh_windows` alone, chosen because the bearer evidence does not
depend on native's cache stamp; the coordinator can require
`fresh_windows` by not running bearer otherwise). It also refuses before
the attempt on: registration not ready, conflicting caller environment, a
`CLAUDE_CONFIG_DIR` other than the registered one (`config_dir_mismatch`), a
server-managed policy cache, or local identity mismatch. Then it creates
`DIR/bearer.attempt`, reads the bearer memory-only with
`read_linux_bearer`, runs exactly one `usage.fetch` and one
`usage.fetch_profile_identity` against the production endpoints with the
production TLS context, and drops the bearer before writing the result. An
expired or near-expiry token refuses (`expired_native_refresh_required`)
without any request; the wrapper never refreshes.

## 2. Result directory, files and exit codes

DIR must exist, be owned by the user with no group or other bits, not be a
symlink, have no `.git` in itself or any ancestor (outside every Git
worktree) and lie outside `secrets_dir`. Results are written by descriptor,
0600, created exclusively:

- `pending.result.json`, `ready.result.json`: always written once DIR is
  valid; a rerun writes `<step>.rerun.json` and never replaces the first
  record.
- `n1.attempt`, `bearer.attempt`: created with `O_EXCL` before any spawn,
  credential read or network; an existing attempt or result file refuses
  (exit 4).
- `n1.result.json`, `bearer.result.json`: written only when this run
  created the attempt, including after an interruption or internal error.
  Preflight refusals write no file.

stdout carries one JSON document (sorted keys, ASCII), identical to the
result file except that `pending` adds `user_commands` and `user_notes`.
No result file contains a path, token, UUID, e-mail or raw body. stderr is
empty except for argument errors.

| Exit | Meaning |
| --- | --- |
| 0 | completed with the success outcome (pending created, ready marked, n1 `fresh_windows`, bearer verified) |
| 1 | ran but not success; for n1 and bearer the attempt is consumed; also interruption |
| 2 | usage error |
| 3 | refused before any effect; nothing created or consumed |
| 4 | the step's attempt or result file already exists |
| 5 | internal error (class name only) |

SIGINT, SIGTERM and SIGHUP during n1 or bearer raise an interruption that
lets `run_n1` reap native's process group and the bearer be dropped; further
signals are ignored until the result is written. SIGKILL cannot be handled:
native, in its own session, then sees its stdin close (I: it exits on EOF).

## 3. Retained fields

| Step | Retained (allowlist) |
| --- | --- |
| all | `schema`, `step`, `status`, `reason` (fixed code), `attempt_consumed`, `exit_code`, `config_source` (fixed string), `config_warnings` (count) |
| pending | `registration_state`, `config_directory_trailing_slash`; stdout only: `user_commands`, `user_notes` |
| ready | three presence booleans, `registration_state`, `marked_ready` |
| n1 | `tunnel`, `requery_allowed`, `bounds`, `native_sha256_matches`, and the `run_n1` sanitized result as reviewed (classification, identity and cache booleans, normalized windows, subscription type from a fixed list, reaping, event and byte counts, binary SHA-256) |
| bearer | `n1_classification`, `identity_matches_local`, `bounds`, credential `read` and refusal code; usage `outcome`, `http_status`, `error_type` (fixed list, else `other`), `fresh`, `valid_general`, normalized general windows, window states, row names only from native's known list with counts for any other name, `unknown_rows`; profile `available` and the two UUID equality booleans |

## 4. Exact coordinator procedure

The coordinator's own shell carries `CLAUDE_CODE_ENTRYPOINT` and other
variables that `check_environment` refuses (by design it refuses, never
strips), so every call goes through `env -i` with an explicit allowlist that
keeps the kit's configuration variables:

```sh
D=<repo>/docs/superpowers/plans/quota-reserve-probes/lc
OUT="$XDG_RUNTIME_DIR/quota-lc-live"   # new, 0700, outside any worktree; tmpfs: archive afterwards
mkdir -m 700 "$OUT"
lc() {
  env -i HOME="$HOME" USER="$USER" LOGNAME="$LOGNAME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" \
    XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    ${CODING_AGENT_KIT_CONFIG:+CODING_AGENT_KIT_CONFIG="$CODING_AGENT_KIT_CONFIG"} \
    ${XDG_CONFIG_HOME:+XDG_CONFIG_HOME="$XDG_CONFIG_HOME"} \
    ${XDG_STATE_HOME:+XDG_STATE_HOME="$XDG_STATE_HOME"} \
    python3 -I -B "$D/live_claude.py" "$@"
}
lc pending --alias NAME --out "$OUT"       # exit 0; show user_commands to the user
# user: runs the two commands in a clean terminal, completes one sign-in,
#       confirms the account and organization shown by auth status
lc ready --alias NAME --out "$OUT"         # optional preview; exit 3 confirmation_required
lc ready --alias NAME --out "$OUT" --confirm                      # exit 0 marked_ready
lc n1 --alias NAME --out "$OUT" --binary "$(command -v claude)"   # up to 120 s
lc bearer --alias NAME --out "$OUT"        # run promptly after n1
```

Checks before and between steps: the config directory in `user_commands`
must match `claude-as --show-config` in the user's shell (the driver reports
`config_source`); if `ready` reports `server_policy_present` true, stop (n1
refuses before the attempt); n1 refuses any binary other than 2.1.322, so
after an auto-update pass the retained 2.1.322 file or stop. Archive
`$OUT/*.json` and the attempt files into the ledger; they hold no path,
token or UUID.

Bounds (from the reviewed modules; the driver passes them unchanged and
records them in `bounds`):

| Step | Bound |
| --- | --- |
| n1 | 120 s total, 45 s per operation, 5 s persistence wait, at most two `get_usage` with one 65 s cache-shortcut wait, 4 MiB stdout and stderr, process-group reaping; tunnel idle 30 s |
| bearer | one credential snapshot (256 KiB; one re-open only if native renamed the file during the read); usage and profile GET each 5 s socket, 10 s total including resolution and TLS, 64 KiB headers and body, at most two addresses, no retry, redirect, refresh or proxy |

## 5. Offline evidence

[test_live_claude.py](quota-reserve-probes/lc/test_live_claude.py): 49
tests, all OK, none skipped (N). Each runs in a temporary HOME with
temporary secrets, config, result and runtime directories, the reviewed QC
fake native ([fake_claude.py](quota-reserve-probes/qc/fake_claude.py)) behind
a wrapper that records whether `n1.attempt` existed at spawn, a loopback echo
upstream for the tunnel, and the QC loopback usage/profile fake
([fake_usage.py](quota-reserve-probes/qc/fake_usage.py)), over plain HTTP and
over TLS with a disposable certificate.

- Configuration (6): the driver's config directory equals
  `bin/claude-as --show-config` for the default, `CODING_AGENT_KIT_CONFIG`
  (with `//` and a trailing slash in the value) and `XDG_CONFIG_HOME`
  cases, with the configured claude command; a trailing-slash HOME gives the
  same spelling; a `//` HOME refuses `secrets_dir_not_canonical` and creates
  nothing; configuration errors refuse; `claude-as --list-accounts` ignores
  the new registration root.
- pending (4) and ready (5): registration modes and schema; rerun refuses
  without a login command and keeps the first record; every result-directory
  rule (relative, missing, group-readable, file, symlink, inside a Git
  worktree, inside secrets); invalid aliases; ready refuses before login,
  without `--confirm` and with a missing UUID, marks ready only with
  `--confirm`, refuses a second time, and reports a server-managed cache.
- n1 (9): happy path through the tunnel (allowed host counted, other host
  refused); attempt file present at spawn; one-shot by result and by attempt
  marker, native not spawned; nine preflight refusals consume nothing
  (unreviewed hash, two conflicting variables, missing and non-private
  `XDG_RUNTIME_DIR`, identity mismatch, managed `apiKeyHelper`, relative
  binary, pending registration); the CLI refuses the fake binary
  (`native_version_unreviewed`), showing the pinned hash is not bypassable
  from the command line; a 1 s operation bound ends a hung native and its
  grandchild; a server-managed cache written during the run voids the result;
  `--no-tunnel`; SIGTERM to the driver reaps native and its grandchild and
  records `interrupted` with the attempt consumed.
- Live wiring (2): the CLI path uses 5 s, 10 s, 64 KiB, the system resolver,
  production endpoints and TLS context, the 2.1.322 hash and the module
  deadlines; the live tunnel connector refuses unlisted hosts before any
  connect.
- bearer (11, plus the same 11 and one more over TLS): verified happy path
  with exactly one usage and one profile request; attempt file present at
  both resolutions; one-shot; refusal without a completed n1 (missing, failed,
  refused, policy unknown, identity changed, hash mismatch, wrong schema,
  model event, nonzero cost in any reply, no replies, non-empty process
  group, non-private result file), while `inconclusive` is accepted;
  environment, `CLAUDE_CONFIG_DIR`, server-policy and identity refusals
  before the attempt; absent and near-expiry credentials consume the
  attempt with zero requests; a rejected bearer gives 401 with
  `authentication_error` and still exactly one request each; profile
  mismatch booleans; unknown row names counted, never retained; a slow peer
  ends at the injected 0.6 s bound; system trust refuses the disposable
  certificate with zero requests.
- Every test scans stdout, stderr and every file in DIR for the fabricated
  access, refresh and echoed tokens and both fabricated UUIDs: none appears.
  No temporary root or child process outlived the suite.

The installed native file on this host hashes to the pinned 2.1.322 value
(N, hash only; the binary was not run).

Reproduction:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/lc/test_live_claude.py
pre-commit run --all-files
```

## 6. Proved, inferred, unproved

- **N (fixtures):** config resolution identical to the launcher in three
  cases; every refusal above happens before any attempt file, spawn,
  credential read or request; attempt files precede spawn and network; the
  one-shot guard holds; bounds propagate; outputs are allowlisted and free of
  the fabricated secrets; signals reap native.
- **S:** the composed modules and their bounds as reviewed in QC.
- **I:** `claude auth login` with the printed `CLAUDE_CONFIG_DIR` stores the
  login in that directory as QC section 1 describes; native exits on stdin
  EOF after a SIGKILL of the driver; the bearer gate's acceptance of
  `inconclusive` and `unavailable` is safe given the extra checks.
- **U:** everything live: login behavior, server-managed policy for the
  account, native traffic and refresh on a real account, live usage and
  profile responses for a full-login bearer, real timing. The QC section 5
  limits stay as stated (Linux only, setsid escape, proxy-only tunnel
  accounting, 2.1.322 key review, unobserved server-side feature defaults).

Remaining for the coordinator: run the procedure above under the recorded
permission, archive the results, and feed them to K and G0. A failed or
interrupted step is not retried; deleting an attempt file to rerun a step
needs a new human decision.
