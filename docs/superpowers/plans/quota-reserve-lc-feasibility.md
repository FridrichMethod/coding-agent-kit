# LC: live driver for Claude HLogin and N1 (one named alias)

Nodes LC and LF (pre-live fixes), base `87aa145`. Research only: no launcher, completion, helper,
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
  `default_workspace_parent`, `check_workspace_ancestors`, `run_n1`, and its
  directory opener for the registration directory) and, through the
  harness's own instances, [bearer.py](quota-reserve-probes/qc/bearer.py)
  (`check_store_selection`, `read_linux_bearer`) and
  [usage.py](quota-reserve-probes/qc/usage.py) (`fetch`,
  `fetch_profile_identity`, production endpoints and TLS context).

It adds no login, credential, quota, transport or policy logic. Its own code
is argument handling, the result-directory rules, the one-shot attempt
markers, a preflight that calls the harness's checks before anything is
consumed, the bearer-step gate on the n1 result, output allowlisting and
signal handling.

| Subcommand | Effect | Network or spawn |
| --- | --- | --- |
| `pending --alias NAME --out DIR` | `create_pending`; prints the two user commands | none |
| `ready --alias NAME --out DIR [--confirm]` | reads the two UUIDs; `mark_ready` only with `--confirm` | none |
| `n1 --alias NAME --out DIR --binary PATH (--tunnel or --no-tunnel)` | one `run_n1` | native child (one-shot) |
| `bearer --alias NAME --out DIR [--accept-incomplete-n1]` | one bearer read, one usage GET, one profile GET | wrapper HTTPS (one-shot) |

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
`ready`, `n1` and `bearer` without any registration refuse
(`path_component_unusable`, from the harness's directory opener).

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
`.claude` above the workspace; no earlier result or attempt marker in DIR
or beside the registration. Then it creates the attempt markers (section 2)
and calls `run_n1(resolved_binary, paths, caller_env, allow_requery=True,
tunnel_connector=..., workspace_parent=XDG_RUNTIME_DIR, monotonic=...,
sleep=...)` with the module's default deadlines. The resolved path is passed
so that the hashed file is the spawned file (an auto-update that moves the
`claude` symlink cannot swap it). The driver compares its own hash before
calling `run_n1`; `run_n1` reads the file again and spawns it, and its hash
is compared only afterwards (`native_sha256_matches`). A replacement of the
file between those two reads would run one unreviewed binary once and be
flagged after the run (I: negligible for a file only the user can write and
that nothing updates during the run). The `monotonic` and `sleep` passed
are real time until a signal arrives (section 2), so the reviewed bounds
are unchanged and can only get shorter.

The accounting tunnel (CONNECT only to `api.anthropic.com:443` and
`platform.claude.com:443`, upstream connect timeout 10 s) is not named in
the approval record: its N1-native text names the reviewed `run_n1` but not
the tunnel, and the QC draft calls the tunnel optional. The driver therefore
has no default: the coordinator must pass `--tunnel` or `--no-tunnel`
(neither, or both, is a usage error). Pass `--tunnel` only after the user
confirms it; otherwise pass `--no-tunnel`. With the tunnel the driver itself
opens the connections to the two allowed hosts on native's behalf, native
sees `HTTPS_PROXY=http://127.0.0.1:PORT` (a transport the harness refuses
from the caller) and every other host gets 403, which can change native's
behavior (I). That is also the condition under which QC observed native's
auxiliary and usage traffic (S, QC section 4). The chosen value is recorded
as `tunnel`.

`bearer` requires an n1 result in the same DIR that was produced for the
same alias (`alias`) and the same registration (`enrollment_tag`, a 16-hex
SHA-256 prefix of the local enrollment id, never the id itself), whose n1
attempt was created at most 1 h ago and at most 5 min in the future
(`n1_result_stale` otherwise), and whose run completed: `status` equal to the
harness classification, zero model events, `cost_zero` in every reply, an
empty process group, identity unchanged before and after,
`server_policy_written` false, a cache record present and the reviewed
binary hash. By default only `fresh_windows` passes (`n1_incomplete`
otherwise), which is the brief's "after a successful n1". With the explicit
`--accept-incomplete-n1` it also accepts `inconclusive` and `unavailable`
(native ran to completion on the account; `run_n1` classifies `inconclusive`
before it checks session cost, so the gate checks cost, model events and
reaping itself); the result records the gate used (`n1_gate`: `fresh_windows`
or `completed`). Under either gate a cache stamp that advanced for an account
other than the registration's refuses (`n1_cache_account_mismatch`): that is
an identity anomaly, not staleness. It also refuses before the attempt on:
registration not ready, conflicting caller environment, a
`CLAUDE_CONFIG_DIR` other than the registered one (`config_dir_mismatch`), a
server-managed policy cache, or local identity mismatch. Then it creates the
attempt markers, reads the bearer memory-only with `read_linux_bearer`, runs
exactly one `usage.fetch` and one `usage.fetch_profile_identity` against the
production endpoints with the production TLS context, and drops the bearer
before writing the result. An expired or near-expiry token refuses
(`expired_native_refresh_required`) without any request; the wrapper never
refreshes.

Both bearer requests carry the neutral `User-Agent: quota-probe/1`
(`Wiring.user_agent`, passed to `usage.py` through its `user_agent`
parameter), so no project, user or host name reaches the provider. The other
headers are fixed by `usage.py`: `Authorization`, `Accept`, `anthropic-beta`,
`Cache-Control`, `Connection` and `Host` (`api.anthropic.com`). `usage.py`
keeps its own research default (`coding-agent-kit-quota-research`) for its
offline fixture callers and is unchanged, because the KLC evidence manifest
binds its digest; any other live caller of `usage.py` must pass a neutral
value the same way (I).

`verified` (bearer exit 0) is a probe outcome: valid general windows and both
profile UUIDs equal to the registration. It is not an admission decision;
unproved rows, model-family association and A/B/A rotation are outside it.

## 2. Result directory, files, signals and exit codes

DIR must exist, be owned by the user with no group or other bits, not be a
symlink, have no `.git` in itself or any ancestor (outside every Git
worktree) and lie outside `secrets_dir`. Files are written by descriptor,
0600, created exclusively, and never replaced:

- `pending.result.json`, `ready.result.json`: written once DIR is valid,
  including for a refusal; every rerun writes the next free
  `<step>.rerun-N.json` (N from 1). Under the preview-then-confirm sequence
  of section 4, `ready.result.json` holds `confirmation_required` and
  `ready.rerun-1.json` holds `marked_ready`. The registration file
  `account.json`, not these records, is the authority on registration state.
- Attempt markers, created before any spawn, credential read or network,
  alias marker first: `lc-n1.attempt` and `lc-bearer.attempt` beside the
  registration (`<secrets_dir>/native-claude-accounts/NAME/`), then
  `n1.attempt` and `bearer.attempt` in DIR. Each holds only the schema, step
  and creation time. Any existing marker or result file refuses (exit 4).
- The two alias markers are an addition to the reviewed HLogin registration
  layout, which names only `account.json` and `config/` in the alias
  directory. They hold no identity or secret, so the approval's "kit keeps
  only accountUuid and organizationUuid" still holds. They persist until the
  registration itself is removed. The coordinator must never delete one:
  deleting it reopens that step on the registration, which is a retry and
  needs a new human decision. K and any later loader of the registration
  layout must tolerate them, or remove them only by a recorded decision.
- `n1.result.json`, `bearer.result.json`: written only when this run created
  an attempt marker, including after an interruption or internal error.
  Preflight refusals write no file.

The guard in DIR alone would be per DIR: a second, replacement or lost DIR
would let the step run again. The alias markers close that: n1 and bearer
are one-shot per registration whatever DIR is used (a second DIR refuses
`alias_attempt_exists`, exit 4). The coordinator still uses exactly one DIR
for the alias. Any other DIR, a lost DIR or a deleted marker counts as a
retry and needs a new human decision. Deleting the registration directory
also deletes the alias markers, but a new registration needs a new login,
which the approval does not cover.

stdout carries one JSON document (sorted keys, ASCII), identical to the
result file except that `pending` adds `user_commands` and `user_notes`.
No result file contains a path, token, account or organization UUID, e-mail
or raw body. stderr is empty except for argument errors.

| Exit | Meaning |
| --- | --- |
| 0 | completed with the success outcome (pending created, ready marked, n1 `fresh_windows`, bearer `verified`) |
| 1 | ran but not success; for n1 and bearer the attempt is consumed; also interruption |
| 2 | usage error |
| 3 | refused before any account effect: no attempt consumed, no registration change, nothing spawned or sent; pending and ready still record the refusal in their result or rerun file |
| 4 | an attempt marker or result file for the step already exists (in DIR or beside the registration); bearer checks this right after the registration state, before any exit-3 refusal, so a rerun after a success exits 4 even when its n1 result is now stale |
| 5 | internal error (class name only) |

Signals. During n1 and bearer the driver installs a handler for SIGINT,
SIGTERM and SIGHUP that only sets a flag and returns; it never raises, so
no cleanup is cut short wherever the signal lands. The step checks the flag
before its attempt markers (refused `interrupted`, exit 3, nothing
consumed), right after them (n1: native never spawned; bearer: no credential
read), and between the bearer read and each request. Inside `run_n1` the
injected `monotonic` jumps past every deadline once the flag is set, so the
request in progress ends at its next bound check (at most 0.2 s,
`operation_deadline`), and the injected `sleep` raises the harness's own
refusal (`interrupted`) inside `run_n1`'s try block. The clock's first
reading is `run_n1`'s `start`, the base of every deadline `run_n1`
computes, and it is always real time, even when the flag is already set. A
signal that lands after the driver's last check but before `run_n1` takes
`start` therefore still ends the run at the first bound check (tested: no
`get_usage`, harness reason `operation_deadline`); it can no longer shift
the base and leave the reviewed bounds running in full. `run_n1` then runs its
whole cleanup as written: stdin close, the 5 s wait, process-group TERM and
KILL, tunnel exit and workspace removal. The driver records `interrupted`
(exit 1), keeps the harness result and replaces `run_n1`'s `elapsed_s`,
which was measured on the jumped clock, with its own measurement. Every
live result has `signal_received`.

Residuals, all disclosed:

- SIGKILL of the driver cannot be handled. Native, in its own session, then
  sees its stdin close (I: it exits on EOF); nobody reaps its group, and the
  `quota-qc-n1-*` workspace in `XDG_RUNTIME_DIR` stays for the coordinator to
  remove after checking that no native process is left.
- A signal that lands inside `run_n1` before the spawn (its own hash of the
  binary and its file reads) cannot stop the spawn: native starts, receives
  one `initialize` request and no `get_usage`, then stdin EOF and group
  reaping (harness reason `operation_deadline`).
- A signal cannot recall a `get_usage` already written to native; the
  request ends at the next bound check whether or not the reply came.
- `run_n1`'s own cleanup raises only if a child still has not exited 5 s
  after SIGKILL (for example in uninterruptible sleep); the tunnel exit and
  workspace removal are then skipped and the driver records `internal_error`
  with the attempt consumed (S, harness code).
- The handler is installed after the DIR checks. A signal before that ends
  the driver by its default action before anything is created.

## 3. Retained fields

| Step | Retained (allowlist) |
| --- | --- |
| all | `schema`, `step`, `status`, `reason` (fixed code), `attempt_consumed`, `exit_code`, `config_source` (fixed string), `config_warnings` (count); n1 and bearer also `signal_received` |
| pending | `registration_state`, `config_directory_trailing_slash`; stdout only: `user_commands`, `user_notes` |
| ready | three presence booleans, `registration_state`, `marked_ready` |
| n1 | `alias`, `enrollment_tag`, `attempt_created_utc`, `tunnel`, `requery_allowed`, `bounds`, `native_sha256_matches`, and the `run_n1` sanitized result as reviewed (classification, identity and cache booleans, normalized windows, subscription type from a fixed list, reaping, event and byte counts, binary SHA-256) |
| bearer | `n1_classification`, `n1_gate`, `n1_age_s`, `identity_matches_local`, `user_agent` (the fixed neutral value sent), `bounds`, credential `read` and refusal code; usage `outcome`, `http_status`, `error_type` (fixed list, else `other`), `fresh`, `valid_general`, normalized general windows, window states, row names only from native's known list with counts for any other name, `unknown_rows`; profile `available` and the two UUID equality booleans |

## 4. Exact coordinator procedure

The coordinator's own shell carries `CLAUDE_CODE_ENTRYPOINT` and other
variables that `check_environment` refuses (by design it refuses, never
strips), so every call goes through `env -i` with an explicit allowlist that
keeps the kit's configuration variables and passes nothing that is unset.
DIR is durable (not tmpfs), private, outside every worktree, created once
and reused unchanged after any stop and resume; each step's files are copied
into the ledger right after that step.

```sh
D=<repo>/docs/superpowers/plans/quota-reserve-probes/lc
A=<ledger>/live-results/lc
STATE="${XDG_STATE_HOME:-$HOME/.local/state}"
OUT="$STATE/quota-reserve-lc-live-NAME"   # the only DIR for NAME
mkdir -p "$STATE" && mkdir -m 700 "$OUT" && mkdir -p -m 700 "$A"   # once; never recreate
lc() {
  env -i HOME="$HOME" PATH="$PATH" LANG="${LANG:-C.UTF-8}" \
    ${USER:+USER="$USER"} ${LOGNAME:+LOGNAME="$LOGNAME"} \
    ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"} \
    ${CODING_AGENT_KIT_CONFIG:+CODING_AGENT_KIT_CONFIG="$CODING_AGENT_KIT_CONFIG"} \
    ${XDG_CONFIG_HOME:+XDG_CONFIG_HOME="$XDG_CONFIG_HOME"} \
    ${XDG_STATE_HOME:+XDG_STATE_HOME="$XDG_STATE_HOME"} \
    python3 -I -B "$D/live_claude.py" "$@"
}
keep() { cp -p "$OUT/$1".* "$A"/; }        # run after every step
lc pending --alias NAME --out "$OUT"; keep pending   # exit 0; show user_commands to the user
# user: runs the two commands in a clean terminal, completes one sign-in,
#       confirms the account and organization shown by auth status
lc ready --alias NAME --out "$OUT"; keep ready       # optional preview; exit 3 confirmation_required
lc ready --alias NAME --out "$OUT" --confirm; keep ready   # exit 0 marked_ready (in ready.rerun-1.json after a preview)
lc n1 --alias NAME --out "$OUT" --binary "$(command -v claude)" --no-tunnel; keep n1
#   (--tunnel instead only after the user confirms the tunnel; tool timeout >= 300 s)
lc bearer --alias NAME --out "$OUT"; keep bearer     # within 1 h of n1; tool timeout >= 60 s
```

Wall-time bounds and timeouts. n1's worst case is about 140 s, not 120 s:
two full SHA-256 reads of the binary before the spawn (I: about a second or
less each), up to 120 s inside `run_n1`'s total deadline (plus at most 0.2 s
of select granularity), then cleanup of up to 18 s (5 s stdin-EOF wait, up
to 8 s of group TERM and KILL with their waits, a final wait of up to 5 s),
plus up to 2 s for each tunnel connection still open at exit. After a
signal the driver needs at most about 20 s (plus the tunnel joins) to finish
that cleanup and write the result. The coordinator therefore runs n1 in the
foreground with a tool timeout of at least 300 s, or as an owned background
process it waits for, never under a 120 s default; a tool that escalates to
SIGKILL too early leaves the SIGKILL residual of section 2. bearer's worst
case is about 20 s (two 10 s request deadlines) plus the credential read.

n1 and bearer run in one sitting. bearer accepts an n1 result only while
its attempt is at most 1 h old (`N1_MAX_AGE_S`, no override flag), and n1
cannot run again, so a stop or account switch longer than an hour between
the two steps blocks N1-bearer for good (it refuses `n1_result_stale` and
consumes nothing); running it then needs a new human decision. The
coordinator starts n1 only when bearer can follow at once.

Coordinator-tool preconditions (U): the live steps run without a
network-filtering or filesystem-restricting tool sandbox. The `env -i` form
strips any proxy variables such a sandbox injects, so under one, native and
the wrapper requests would fail after the attempt is consumed, or the marker
and result writes would fail. This sits next to the timeout delivery above
as a condition the coordinator checks before n1.

Checks before and between steps: the config directory in `user_commands`
must match `claude-as --show-config` in the user's shell (the driver reports
`config_source`); if `ready` reports `server_policy_present` true, stop (n1
refuses before the attempt); n1 refuses any binary other than 2.1.322, so
after an auto-update pass the retained 2.1.322 file or stop; bearer refuses
an n1 result older than 1 h (nothing consumed), so run it promptly, and
pass `--accept-incomplete-n1` only as a recorded coordinator decision when
n1 ended `inconclusive` or `unavailable`. The archived files hold no path,
token or account UUID.

Bounds (from the reviewed modules; the driver passes them unchanged and
records them in `bounds`):

| Step | Bound |
| --- | --- |
| n1 | 120 s total, 45 s per operation, 5 s persistence wait, at most two `get_usage` with one 65 s cache-shortcut wait, 4 MiB stdout and stderr, process-group reaping; tunnel idle 30 s |
| bearer | one credential snapshot (256 KiB; one re-open only if native renamed the file during the read); usage and profile GET each 5 s socket, 10 s total including resolution and TLS, 64 KiB headers and body, at most two addresses, no retry, redirect, refresh or proxy |

## 5. Offline evidence

[test_live_claude.py](quota-reserve-probes/lc/test_live_claude.py): 72
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
- pending (4), ready (6) and missing registration (1): registration modes
  and schema; reruns refuse without a login command and keep every record
  (`rerun-1`, `rerun-2`); every result-directory rule (relative, missing,
  group-readable, file, symlink, inside a Git worktree, inside secrets);
  invalid aliases; ready refuses before login, without `--confirm` and with
  a missing UUID, marks ready only with `--confirm`, refuses a second time,
  keeps the preview, success and later refusal in three records, and reports
  a server-managed cache; with no registration, ready, n1 and bearer refuse
  `path_component_unusable`, spawn nothing and create no registration or
  marker (only ready's refusal record).
- n1 (16): happy path through the tunnel (allowed host counted, other host
  refused); attempt file present at spawn; one-shot by result, by DIR marker
  and, after both are deleted, by the alias marker, native not spawned; a
  second DIR refuses `alias_attempt_exists`; `--tunnel` or `--no-tunnel` is
  required; nine preflight refusals consume nothing (unreviewed hash, two
  conflicting variables, missing and non-private `XDG_RUNTIME_DIR`, identity
  mismatch, managed `apiKeyHelper`, relative binary, pending registration);
  the CLI refuses the fake binary (`native_version_unreviewed`), showing the
  pinned hash is not bypassable from the command line; a 1 s operation bound
  ends a hung native and its grandchild; a server-managed cache written during
  the run voids the result; `--no-tunnel`. Signals: SIGTERM to the driver
  process during a hung request ends it at once (not at the 25 s bound),
  reaps native and its grandchild, removes the workspace and records
  `interrupted` with the attempt consumed; SIGTERM inside `native.close()`'s
  5 s wait (after a 1 s operation bound, native ignoring stdin EOF) lets the
  cleanup finish (`elapsed_s` at least 5 s, group signalled and empty, native
  and grandchild gone, workspace removed), the case the previous handler cut
  short; a signal during the cache-shortcut wait ends the run with harness
  reason `interrupted` and one query (no second `get_usage`); a signal set
  after the driver's last check but before `run_n1` takes its start ends the
  run at the first bound check with zero queries (against the clock without
  the real-time base this run ends `fresh_windows` with one query); a signal
  before the markers refuses and consumes nothing; a signal right after them
  records `interrupted` and never spawns native. The pin test skips, instead
  of failing with another reason, on a host whose own policy sources refuse
  before the hash check.
- Live wiring (2): the CLI path uses 5 s, 10 s, 64 KiB, the system resolver,
  production endpoints and TLS context, the 2.1.322 hash, the module
  deadlines and the `quota-probe/1` User-Agent; the live tunnel connector
  refuses unlisted hosts before any connect.
- bearer (18, plus the same 18 and one more over TLS): verified happy path
  with exactly one usage and one profile request; the headers the fixture
  received on both requests (Authorization excluded) carry `User-Agent:
  quota-probe/1` and contain no `coding-agent-kit`, user name, host name or
  home path; a pending registration refuses `registration_pending` with zero
  requests; a rerun after a success whose n1 result is now stale exits 4
  `result_exists`; attempt file present at
  both resolutions; one-shot by result, DIR marker and alias marker; a
  second DIR with a copied n1 result refuses `alias_attempt_exists` with zero
  requests; refusal before the attempt for a missing, failed, refused,
  interrupted or inconsistent n1, policy unknown, identity changed, hash
  mismatch, old schema, a model event, nonzero cost in any reply, no
  replies, a non-empty process group, no cache record, another alias,
  another enrollment, an n1 attempt older than 1 h, in the future or
  undated, `inconclusive` or `unavailable` without the flag, a foreign cache
  stamp (also with the flag), and a non-private result file, while
  `inconclusive` passes with `--accept-incomplete-n1` (`n1_gate`
  `completed`); environment, `CLAUDE_CONFIG_DIR`, server-policy and identity
  refusals before the attempt; absent and near-expiry credentials consume
  the attempt with zero requests; a signal right after the markers reads no
  credential and sends nothing, and one during the usage request lets it
  finish and skips the profile request; a rejected bearer gives 401 with
  `authentication_error` and still exactly one request each; profile
  mismatch booleans; unknown row names counted, never retained; a slow peer
  ends at the injected 0.6 s bound; system trust refuses the disposable
  certificate with zero requests.
- Every test scans stdout, stderr, every file in DIR and the alias markers
  for the fabricated access, refresh and echoed tokens and both fabricated
  UUIDs: none appears. No temporary root or child process outlived the suite.

The installed native file on this host hashes to the pinned 2.1.322 value
(N, hash only; the binary was not run).

Reproduction:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/lc/test_live_claude.py
pre-commit run --all-files
```

## 6. Proved, inferred, unproved

- **N (fixtures):** config resolution identical to the launcher in three
  cases; every refusal above happens before any attempt marker, spawn,
  credential read or request; the bearer requests carry only the neutral
  User-Agent and no project, user, host or home identifier; attempt markers precede spawn and network; the
  one-shot guard holds per DIR and, through the alias markers, across DIRs;
  bounds propagate; outputs are allowlisted and free of the fabricated
  secrets; a signal before the markers consumes nothing, and a signal after
  them, including one inside `run_n1`'s cleanup or before its start reading,
  still ends with native and its grandchild reaped and the workspace removed
  (for the request, wait, cleanup and pre-start windows tested above).
- **S:** the composed modules and their bounds as reviewed in QC; the
  harness code paths behind the signal residuals of section 2.
- **I:** `claude auth login` with the printed `CLAUDE_CONFIG_DIR` stores the
  login in that directory as QC section 1 describes; native exits on stdin
  EOF after a SIGKILL of the driver; the tunnel can change native's behavior;
  the hash-to-spawn gap is negligible; the broader bearer gate is safe given
  its extra checks.
- **U:** everything live: login behavior, server-managed policy for the
  account, native traffic and refresh on a real account, live usage and
  profile responses for a full-login bearer, real timing, and how the
  coordinator's tool delivers a timeout (signal and grace period) and whether
  it runs the steps outside any network or filesystem sandbox. The QC
  section 5 limits stay as stated (Linux only, setsid escape, proxy-only
  tunnel accounting, 2.1.322 key review, unobserved server-side feature
  defaults).

Remaining for the coordinator: confirm the tunnel choice with the user, run
the procedure above under the recorded permission with the stated timeouts,
archive each step's results right after it, and feed them to K and G0. A
failed or interrupted step is not retried; deleting an attempt marker, or
using another DIR, to rerun a step needs a new human decision.
