# QC: attributable model-free Claude quota and credential binding

Feasibility node QC, research date November 12, 2026, base `0c5b492`;
revised after independent review round 1 (findings I1, M1-M5; see the last
section).
Research only: no launcher, completion, helper, installer or suite change.
Code and tests live in [quota-reserve-probes/qc/](quota-reserve-probes/qc/).
G0 is not passed; nothing here is production capability or release evidence.

No real credential read, login, account selection, Keychain operation or
real-provider request occurred in this node. Every token, account, quota and
refresh response is fabricated. The native runs used private temporary
HOME/config/workspace roots under the unchanged PC loopback-only guard; the
guard's denial log contains only its own four self-check syscalls, so the
native child made no direct network attempt and reached only loopback
fixtures (section 4). The Keychain path is never executed. The approved scope
is access-bearer DESIGN research with fabricated credentials only.

Evidence labels: **N** native observation in this node (Linux x86_64, Claude
Code 2.1.322, SHA-256 `bfb3703a...c478`, the same binary as PA0/PA1F/PR);
**S** static inspection of that installed executable or documentation;
**I** inference; **U** unproved. Byte offsets identify version-specific
anchors, not stable APIs; excerpts are kept in the ignored worktree cache.

## Decision

A concrete mechanism yields fresh, model-free windows bound to the exact
credential the request gate admits, in design and in fixtures:

1. Read the access bearer memory-only from the selected account's fixed native
   store (Linux `.credentials.json`; macOS Keychain designed, not executed).
2. Send one wrapper-side `GET https://api.anthropic.com/api/oauth/usage` with
   that exact bearer; normalize the general `five_hour`/`seven_day` windows.
3. Admit Messages only under a lease whose bearer equals the outgoing bearer;
   on rotation, hold the request, repeat step 2 with the OBSERVED bearer and
   re-check identity before admitting; otherwise latch a persistent pause.

Quota is attributable to the admitted credential by construction (same
bearer) and fresh by construction (a direct 200 for this request; no native
cache). This closes PA1F's cache-ambiguity and PR's "which bearer produced the
observation" gap in fixtures. It does not by itself bind the bearer to the
REGISTERED account: local UUID reads cannot authenticate an opaque bearer.
The same-bearer profile read (option 3 below) closes that gap in fixtures at
the cost of one more endpoint. Live availability of the usage endpoint for a
full-login bearer is U: native itself calls it with the stored bearer (N, S),
but no real account was used. Cost: this expands the accepted metadata-only
read surface to a credential-bearing record and needs G0 acceptance.

## 1. Credential interface

Static facts (S, 2.1.322):

| Item | Finding | Anchor |
| --- | --- | --- |
| Store directory | `CLAUDE_SECURESTORAGE_CONFIG_DIR` if set (empty means `~/.claude`), else `(CLAUDE_CONFIG_DIR or ~/.claude).normalize("NFC")`, no realpath, no slash trimming | 284879935, 283178925 |
| Linux file | `<store>/.credentials.json`; plaintext backend only (Keychain backend absent from this build) | 284895839 |
| Write | mkdir store; stage `O_CREAT|O_EXCL` mode 0600 beside target, write, fsync, rename over target; in-place truncate/write arm only on EBUSY bind mounts; chmod 0600 | 283584706, 284896620 |
| Write lock | lockfile `<store>/.storage-write` (stale 15 s) plus PA0's `.oauth_refresh.lock` | 284894571 |
| Record | `claudeAiOauth{accessToken, refreshToken, expiresAt(ms), refreshTokenExpiresAt?, scopes[], subscriptionType, subscriptionTypeSource?, subscriptionTypeVerifiedAt?, rateLimitTier, clientId?}`; other top-level keys (`mcpOAuth`, gateway, legacy device) may coexist | 286925816 |
| Selection | env token or fd token wins over the store (`hg()`: env, fd, then store) | 286935470 |
| macOS item | service `Claude Code` + `OAUTH_FILE_SUFFIX` ("" in prod) + `-credentials` + `-` + first 8 hex of SHA-256 over the exact NFC config-dir string; account `$USER` or OS user, `[a-zA-Z0-9._-]+` else `claude-code-user`; native reads with `security find-generic-password -a ACCOUNT -w -s SERVICE` (exit 44 = absent) | 284879912, 284880106, 284895511 |

Native refresh observed (N, fixture): an expired record or a usage 401 makes
native rewrite the file by rename (new inode, mode 0600, nlink 1) with the new
access and refresh token; `invalid_grant` makes native rewrite it with empty
`accessToken`/`refreshToken` and `expiresAt: 0`, a state that needs explicit
native re-login. The wrapper never refreshes and never writes the store.

Linux reader ([bearer.py](quota-reserve-probes/qc/bearer.py), 18 tests):

- Refuses any store selector in the environment native will receive
  (`CLAUDE_SECURESTORAGE_CONFIG_DIR`, env/fd OAuth or API keys, alternate
  providers, custom OAuth endpoints) and any `CLAUDE_CONFIG_DIR` other than
  the one recorded spelling.
- Walks from `/` with `O_NOFOLLOW|O_DIRECTORY`; the account root, NAME and
  config directories must be uid-owned with no group/other bits; ancestors
  must be root/uid owned and not group/other writable unless sticky.
- Opens `.credentials.json` `O_NOFOLLOW|O_NONBLOCK`; requires an owned regular
  0600 single-link file of at most 256 KiB; reads one snapshot by descriptor
  and requires identical fstat before/after. A native rename during the read
  (directory entry inode changed) retries once, then refuses.
- Strict JSON (duplicate keys, NaN refused). Retains only `accessToken`,
  `expiresAt` and two scope booleans in an unhashable, unpicklable,
  uncopyable object with a redacted repr and constant-time `matches()`. An
  expired record refuses with `expired_native_refresh_required`.
- `refreshToken` is never examined: any type, empty or absent passes; the
  parsed document is dropped immediately. Limit: the JSON parser necessarily
  holds refresh material transiently in process memory, and Python strings
  cannot be wiped. No token is logged, persisted or hashed by this code.

macOS design (not executed; NM owns Seatbelt reachability):

```text
/usr/bin/security find-generic-password -a ACCOUNT -s "Claude Code-credentials-XXXXXXXX" -w
```

argv only, no shell, no `-i` script, no `-g`; 2 to 5 second bound; stdout
bounded and decoded as JSON text or strict hex; exit 44 means no item, after
which native may use its plaintext fallback, validated by the Linux checks.
The item is created by native through `/usr/bin/security`, so invoking that
same tool is the shape most likely to avoid an ACL prompt (U); a
Security-framework call from python3 would present a different client (I).
`execute_keychain` refuses unless a reviewed permission object exists; none
can be constructed in this node. Tests cover service/account derivation,
argv, decoding and refusal paths with an injected fake runner.

Hazard found: the PN/PA1F login template spells `.../NAME/config/` with a
trailing slash. Native hashes the exact string, so on macOS `.../config/` and
`.../config` select different Keychain items (S). The kit must pass exactly
the recorded `config_directory` (no trailing slash) every time; the reader
and harness refuse any other spelling.

## 2. Same-bearer quota

[usage.py](quota-reserve-probes/qc/usage.py) and the loopback
[fake](quota-reserve-probes/qc/fake_usage.py), 21 tests (two loopback TLS
tests need `/usr/bin/openssl` and skip without it):

- Production target `https://api.anthropic.com/api/oauth/usage`, verifying
  default TLS context, `Authorization: Bearer`, `anthropic-beta:
  oauth-2025-04-20`, `Cache-Control: no-cache`, `Connection: close`, neutral
  User-Agent (whether the server accepts a non-native agent is U).
- Time: one 10 s watchdog is armed before name resolution. Resolution runs
  in a daemon thread joined with the time left (getaddrinfo ignores socket
  timeouts); at most two addresses are tried, each connect and the TLS
  handshake take min(5 s, time left); the deadline is checked again right
  before the request, so no header byte (and no `Authorization`) is sent once
  it is spent; the watchdog shuts tracked sockets down at 10 s, so slow-drip
  peers cannot extend it. Bound: 10 s plus thread scheduling. A hung resolver
  thread may outlive the call; it holds no token. Tests: hung resolver,
  deadline spent during resolution (no connection), deadline spent after
  connect (zero bytes reach the peer), slow drip.
- 64 KiB headers and body, no redirects (3xx refused), no retries, no
  refresh, no proxy discovery.
- Only a fresh direct 200 with `application/json` and every enabled window
  valid is usable. 401/403/429/5xx, transport errors, deadline, malformed or
  cache-sourced data are never fresh.
- Windows by name and duration: utilization finite in [0, 100]; RFC 3339
  reset after the receive time (120 s skew) and no later than one window
  duration ahead; `null` reset is `reset_unknown` (live semantics U).
  Native's other rows (`seven_day_oauth_apps`, `seven_day_opus`,
  `seven_day_sonnet`, `cinder_cove`, `wattle_ember`, `extra_usage`, `limits`)
  and any `five_hour_*`/`seven_day_*` row are counted, never substituted.
- Present (non-null, non-empty) rows whose admission semantics are unproved
  are reported as `unproved_rows`: `seven_day_oauth_apps`, the credit pools
  `cinder_cove` and `wattle_ember`, any `limits[]` entry, unknown
  `five_hour_*`/`seven_day_*` rows and unknown rows. The two model rows and
  the `extra_usage` overage pool are not in that set (I). Native (S, offset
  292579091) parses each `limits[]` entry as `{kind, group, percent,
  resets_at, severity, is_active, scope.model.display_name,
  scope.surface.display_name}` and shows them as per-model weekly buckets
  labelled by display name (for example "Fable"), filtered by a server-flag
  allowlist; nothing in the response maps a display name to an API model id.
- Option-3 helper `fetch_profile_identity` (same bounds) keeps only
  `account.uuid` and `organization.uuid` from `/api/oauth/profile`, the
  endpoint native uses at login and refresh to bind a token to an account.

## 3. Admission and rotation contract

[admission.py](quota-reserve-probes/qc/admission.py) with
[gate_server.py](quota-reserve-probes/qc/gate_server.py), 23 tests on a
deterministic clock. Lease = (bearer, registered account/organization,
family, observation time, monotonic deadline, clock offset). Under one
admission lock, before any upstream byte: refuse unless open; exactly one
`Authorization: Bearer` and no `x-api-key` (else pause); model in the admitted
family set; clock offset stable; deadline not reached; bearer equal to the
lease. On mismatch the request is held, the observed bearer is wrapped
memory-only, identity `begin`, fresh usage GET with that bearer, identity
`finish`, policy evaluation; then a new lease or a persistent pause. The
forwarded request always carries its own unchanged bearer.

Choices for G0 (I): same-identity rotation keeps already admitted streams
(they were admitted under valid quota); any failure closes all registered
streams. Revalidations are budgeted (default 4 per minute, then pause).
A model with a model-specific row present (`seven_day_opus` for an opus
model, by a substring heuristic) is refused as `model_family_unproved`.
While the lease holds any `unproved_rows` entry, such as a model-scoped
`limits[]` entry, every request is refused as `quota_row_unproved` (a
refusal, not a latch). Remaining G0 gates: the production `allowed_models`
list and its model-to-family mapping (the research heuristic is not one),
and the semantics of each unproved row from a live sample; until then the
gate refuses rather than admitting under the general windows.

Fail-closed costs that are G0/K inputs (I, from the contract as written):

- Idle bearer expiry: `monitor()` polls with the lease bearer. If native
  refreshes during an idle period, polls fail until `freshness_expired`
  pauses the session, and the next prompt (carrying the refreshed bearer) is
  refused and needs manual continuation; paused polls have the same problem.
  Possible change, not implemented: on a 401 for the lease bearer, re-read
  the store bearer and run the same rotation contract (hold, fresh
  same-bearer GET, identity check) before pausing.
- Old-bearer stragglers after a same-identity rotation: a revoked old bearer
  pauses the session at once; one that is not revoked flips the lease back
  and forth, each flip a revalidation, until the 4-per-minute budget pauses
  the session.
- Monitor loss: the controller has no timer of its own. If `monitor()` stops
  being called, registered streams stay open until the next `admit()`; only
  new requests are refused. K must close streams at the lease deadline or on
  monitor loss without waiting for `admit()`.
- The admission lock is held across the rotation fetch (section 2 bound:
  about 10 s), so every concurrent admission waits that long during a
  rotation check.

| Required sequence | Test |
| --- | --- |
| A quota, native refresh A2 same account; revoked A straggler never forwarded | `test_A_then_native_refresh_A2_same_account` |
| A to B with stale A cache: B fetched with B, identity mismatch, zero B bytes | `test_A_to_B_switch_with_stale_A_cache`, `test_switch_where_B_quota_fetch_fails` |
| A/B/A: latch; A again needs manual continuation and a fresh A fetch | `test_A_B_A`, `test_metadata_flip_during_check_refuses` |
| Two concurrent same-account sessions rotating (10 threads) | `test_two_concurrent_same_account_sessions_rotate` |
| Unchanged fresh data extends; 429/401/403/malformed/redirect/transport/cache never do | `test_unchanged_fresh_data_extends_but_failures_do_not` |
| Missing, malformed, stale, unsupported-family, threshold/equality, server and local clock anomalies | `test_startup_refusals_send_no_model_request`, `test_threshold_and_equality_refuse_startup`, `test_server_clock_anomaly_refuses`, `test_clock_anomaly_pauses` |
| Expired freshness pauses and closes streams | `test_expired_freshness_pauses_at_admission` |
| Recovery never auto-resumes; stale/forged/reused continuation rejected | `test_recovery_never_auto_resumes`, `test_continuation_with_failing_check_stays_paused` |
| Local-UUID verifier gap and its closure by the same-bearer profile verifier | `test_bearer_B_with_unchanged_local_metadata_is_the_local_verifier_gap` |
| Real loopback gate: refused request opens no upstream connection | `test_rotation_and_switch_through_a_real_gate` |
| Unproved rows (model-scoped `limits[]`, OAuth-apps, credit, unknown) refuse every model | `test_unproved_rows_refuse_every_model_without_latching` |

The gap test is deliberate: with only before/after local UUID reads, a B
bearer while local metadata still says A is admitted with B's own quota.
Quota attribution to the admitted credential holds; attribution to the
selected account does not. The profile verifier refuses it.

## 4. Native-path observation

[native_observe.py](quota-reserve-probes/qc/native_observe.py) reuses the
PA1F TLS proxy technique as a research aid only. Sanitized evidence:
[observed-native.json](quota-reserve-probes/qc/observed-native.json),
checked by [check_native.py](quota-reserve-probes/qc/check_native.py).
Each case: fabricated full-login store, initialize plus two
`get_usage(skip_behaviors=true)`, no user message, 45 s bound. Retained run
(run-5): all six cases exited 0 (measured from the child's `exit.json`) with
zero model attempts at the fixture, zero model events counted in native's
stdout, and no CONNECT outside the two fixture hosts.

Completeness: the request lists below are the complete requests. Five cases
(all but nonessential-off) also had exactly one incomplete request, recorded
under `unrecorded_connections`: a second `POST /api/event_logging/v2/batch`
to api.anthropic.com after `get_usage`. The fixture recorded only
`stage=request_body` and `error=ValueError`, so its response and bearer
class were not recorded. That the body exceeded the fixture's 64 KiB body
budget is an inference (I): the fixture's body reader raises ValueError both
for that budget and for an unparsable Content-Length or chunk-size line, and
it did not keep the message. It is telemetry, not a preconnect.

| Case | Requests observed (host api unless noted) |
| --- | --- |
| startup-valid | profile, `POST /api/eval/sdk-*`, bootstrap, penguin_mode, grove, referral eligibility, usage (bearer A), mcp-registry (no auth), event_logging batch |
| nonessential traffic off | profile only; native usage NOT fetched (`rate_limits` null) |
| expired, refresh success | first `POST platform.claude.com/v1/oauth/token` (no Authorization; JSON keys `client_id, grant_type, refresh_token, scope`), then everything with A2; store renamed to A2 |
| expired, `invalid_grant` | refresh attempt; store cleared (empty tokens, expiry 0); usage not fetched |
| usage 401 | usage A gets 401, native refreshes, usage retried with A2; store rotated mid-session |
| `ANTHROPIC_BASE_URL` = loopback gate | identical auxiliary set still to api.anthropic.com; zero requests reached the gate |

Inputs for K (N unless marked): native refresh needs
`platform.claude.com:443` `POST /v1/oauth/token`, a TLS host distinct from
inference. Profile, usage, bootstrap, feature-evaluation, telemetry and other
bearer-carrying calls use `api.anthropic.com:443`, the same TLS host as
first-party inference, and ignore `ANTHROPIC_BASE_URL`. A domain or CONNECT
rule therefore cannot separate them from direct inference; the Messages pin
plus the gate is the separation, and any native path that ignores the pin
remains a K routing question (I). Setting
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` suppresses native usage, so a
native-usage design must not set it (N). The wrapper's own usage GET also
needs `api.anthropic.com:443`. Gate lease API for K: `startup(bearer)`,
`admit(headers, model, close) -> Decision` before any upstream byte,
`monitor()`, `issue_continuation()` from the trusted terminal only,
`continue_manually(token, bearer)`, `release(id)`; a pause closes every
registered stream and nothing passive reopens it. K must also own a timer
that closes registered streams at the lease deadline or when monitoring
stops, independent of `admit()` (section 3), and must budget for an admission
lock held up to the usage-GET bound during a rotation check.

## 5. N1 harness and human permissions

[n1_harness.py](quota-reserve-probes/qc/n1_harness.py) with a fake native
([fake_claude.py](quota-reserve-probes/qc/fake_claude.py)), 25 tests:
alias-to-path resolution without enumeration; `create_pending` (exclusive
mkdir, 0700, `account.json` 0600, state pending, identity null, random
enrollment id, exact `config_directory`; existing or pending refuses);
`mark_ready` only when local metadata equals the human-confirmed identity,
written atomically; strict registration schema; refusal of conflicting
caller variables (store/provider/endpoint/proxy/CA, nonessential-off); policy
documents are read, never rewritten, and real HOME is kept. The child gets
an allowlisted environment, closed inherited descriptors (verified), its own
session, 4 MiB output and stderr bounds, 45 s per operation and 120 s total,
and is reaped with its process group (grandchild verified). Output is a
sanitized allowlist; a fabricated token echoed by the fake never appears;
non-object control replies refuse as `protocol_malformed`. A cache shortcut
is inconclusive unless the approved procedure names one bounded 65 s wait (at
most two queries). An optional loopback CONNECT tunnel counts host:port and
bytes and refuses unlisted hosts. It was never run live.

Policy sources (S, 2.1.322; review findings I1 and I2). Native composes
several managed sources, so the harness inspects every file policy source it
reads, refuses every policy source it cannot evaluate, and refuses the
reviewed settings keys listed after the table wherever they appear:

| Source (native anchor) | Harness |
| --- | --- |
| `<config>/settings.json` (user) | inspected |
| `<managed>/managed-settings.json`; managed dir `/etc/claude-code`, macOS `/Library/Application Support/ClaudeCode` (284122138) | inspected |
| `<managed>/managed-settings.d/*.json`: regular files or symlinks, no leading dot, sorted by UTF-16 code units (284156811, 284158452) | inspected, native order |
| WSL: `/mnt/c/Program Files/ClaudeCode` first, plus HKLM/HKCU `SOFTWARE\Policies\ClaudeCode` through `reg.exe` (283678656, 284508534) | refused (registry not inspectable) |
| macOS managed preferences plist (reader absent from the Linux build: `plistStdouts` is always null there) | refused (not inspectable) |
| Signed server-managed `<config>/remote-settings.json` and `policy-limits.json` caches (284089179, 283866174) | present refuses |
| Server-managed fetch during the run: `api.anthropic.com/api/claude_code/settings` and `/policy_limits` (301264218) | detected afterwards by the cache file; voids the result |
| `policyHelper` in any document, whatever its value: the documented admin key, "Executable that computes managed settings at startup" (283927935); native reads it from admin, non-remote sources (`P=E?void 0:e?.policyHelper`, 284577548) | refused (`policy_helper_unevaluable`) |
| `policyHelpers` in any document: its `@internal` per-OS variant (283928159) | refused (`policy_helper_unevaluable`) |
| `<managed>/managed-mcp.json` (297393297); it can name programs, and its handling in safe mode was not traced | present refuses |

Each document is refused on: an `env` key in the caller refusal list or in
the families `ANTHROPIC_*`, `CLAUDE_*`, `AWS_*`, `GOOGLE_*`, `VERTEX_*`,
`CLOUD_ML_*`, `NODE_*`, `SSL_*`, `BUN_*`, `NPM_CONFIG_*` or any name
containing `PROXY`, `CERT` or `_CA_`; a non-claude.ai `forceLoginMethod`;
a `forceLoginOrgUUID` without the registered organization; and the keys
below. The checks are per document and a refused key is refused wherever it
appears, so whatever native's merge order and composition mode (first-wins
or merge), the effective policy holds only accepted values (I).

Settings keys reviewed (S: every top-level key of the 2.1.322 settings
schema, from offset 283926804, classified by its own description;
the N1 consequence is I unless marked):

- Refused whatever the value (`policy_conflicting_auth`): the credential
  helpers that native's `managedSourcesBehavior` description names
  (`apiKeyHelper`, `awsAuthRefresh`, `awsCredentialExport`, `gcpAuthRefresh`,
  `otelHeadersHelper`, `proxyAuthHelper`; safe mode still honors a managed
  `otelHeadersHelper`, 286957995), `processWrapper`, the gateway auth pins
  `forceLoginGatewayUrl` and `gatewayInternalNetworks`, and the XAA login
  config `xaaIdp`.
- Refused unless null, false or empty (`policy_command_unevaluable`): keys
  that run a command or start a connection, upload, service or background
  work. `hooks` is traced: safe mode keeps the managed hooks
  (287982339). `forceRemoteSettingsRefresh` blocks startup until a
  server-managed fetch completes and exits if it fails (283975225). The
  others are refused rather than traced under print and safe mode:
  `statusLine`, `subagentStatusLine`, `fileSuggestion`, `sshConfigs`,
  `sshHostCommands`, `managedMcpServers`, `extraKnownMarketplaces`,
  `additionalMarketplaces`, `remoteControlAtStartup`, `autoUploadSessions`,
  `daemonColdStart` and `autoDreamEnabled`.
- Accepted, because they cannot change N1's auth, transport or traffic:
  - `allowedProviders` only narrows: native says it "cannot be widened" (S).
    Under a list, a first-party destination is honored only when the same
    managed source pins it in `env`, which N1 refuses. A list without
    `anthropic` makes native refuse at startup, and N1 then fails.
  - `requiredMinimumVersion` and `requiredMaximumVersion` make native exit,
    and N1 then fails.
  - `enabledPlugins`, `prependPlugins`, `appendPlugins` and user MCP
    servers: safe mode disables installed plugins and MCP servers (S,
    287985081 and the `--safe-mode` help at 304556349).
  - `syncClaudeAiSkills`, `syncClaudeAiPlugins` and the `disable*` and
    `allowManaged*` keys only turn features off or restrict them (S).
  - Model-selection, permission, sandbox, attribution, memory, teammate and
    other tool-scope keys apply only to model requests or tool use, and N1
    makes neither.
  - `autoUpdatesChannel` and `minimumVersion` only select the update
    channel; the fixture runs made no update request (N, section 4).
  - Notification keys (`inputNeededNotifEnabled`, `agentPushNotifEnabled`)
    fire only on prompts, questions or agent messages, which N1 never
    produces. Local-retention keys (`cleanupPeriodDays`) touch only local
    transcripts.
  - `parentSettingsBehavior` and `managedSourcesBehavior` only change
    composition, which per-document refusal makes irrelevant.
    `wslInheritsWindowsSettings` applies only on WSL, which is refused.
  - Display, terminal-UI and Desktop-only keys.

The key review is a denylist for 2.1.322. A key that a later native version
adds is not refused until it is reviewed, so the N1 permission names that
version, and the result records the binary's SHA-256.

Project and local settings: the child runs in a new empty 0700 workspace.
By default the workspace is under `XDG_RUNTIME_DIR` when that is a private
directory of the user, otherwise under the system temporary directory.
run_n1 refuses before spawning native (`workspace_ancestor_project`) if
any ancestor of the workspace holds `.git` or `.claude`. Native's upward
project-root lookup was not traced (I), and the check covers it either way.
Residual: under the system-temporary fallback, another local user could
create such an entry in the shared ancestor after the check (a race, U).
The N1 draft therefore names a private `XDG_RUNTIME_DIR`.

Limits, stated in the N1 draft below:

- Server-managed policy fetched during the run applies before the harness
  can see it (Team/Enterprise organizations; not fetched in the fixture runs
  with a fabricated `max` subscription; eligibility rules not traced, U).
  After such a run every later N1 refuses on the cache; evaluating signed
  server-managed policy is a remaining gate.
- Reaping covers the child's process group only. A descendant that calls
  `setsid()` or daemonizes leaves the group and is neither signalled nor
  reaped.
- The tunnel sees, counts and refuses only traffic that honors
  `HTTPS_PROXY`. Direct egress is neither counted nor blocked live; the
  fixture guard showed none for 2.1.322 (section 4), which is not a live
  guarantee.
- Server-side feature defaults for a real account, for example whether
  background memory consolidation (auto-dream) runs, were not observed. The
  fixture runs saw zero model attempts under the fixture's
  feature-evaluation replies (N), which does not prove the same for a real
  account (U). A model event on stdout fails the run, but background
  traffic is visible only to the tunnel, as above.
- The settings-key review applies to 2.1.322 only (see above).

Draft for the coordinator to present (placeholders, not authorization):

```text
HLogin permission (one named alias)
  Alias NAME; paths, exact spelling without trailing slash:
    <secrets_dir>/native-claude-accounts/NAME/              0700, created new
    <secrets_dir>/native-claude-accounts/NAME/account.json  0600 pending, identity null
    <secrets_dir>/native-claude-accounts/NAME/config        0700, fixed CLAUDE_CONFIG_DIR
  Steps: (1) harness create_pending, no network; (2) the user runs
    CLAUDE_CONFIG_DIR=<config> claude auth login   and completes browser sign-in;
    (3) CLAUDE_CONFIG_DIR=<config> claude auth status; (4) the user confirms the
    displayed claude.ai account and organization; (5) mark_ready reads only
    oauthAccount.accountUuid and organizationUuid from <config>/.claude.json.
  Native effects: writes .credentials.json (OAuth access and refresh), .claude.json,
    backups/, sessions/, telemetry/; may create or migrate HOME/.claude/.device-keys.json
    (accepted shared state); contacts claude.com and platform.claude.com OAuth endpoints
    and api.anthropic.com profile, policy, bootstrap and telemetry endpoints.
  Retained by the kit: account.json with the two confirmed UUIDs; nothing else.
  Bounds: one login attempt; failure or cancel stays pending; no retry, no other
    account, no default-account access, no quota query, no model request, no bearer read.
  Policy: for a Team/Enterprise organization native may fetch and cache
    server-managed policy (remote-settings.json, policy-limits.json) during login;
    if it does, N1 refuses for this alias until that policy can be evaluated.

N1 permission (same alias, no login)
  Native version: 2.1.322 only (the policy key review is version-specific); the
    result records the binary's SHA-256.
  Pre-run refusals: run_n1 runs on Linux only and refuses before spawning native:
    on macOS and WSL (OS policy sources not inspectable); when a server-managed
    policy cache or a managed-mcp.json exists; when any inspected policy document
    (user settings, managed-settings.json, managed-settings.d/*.json) carries a
    refused env family, a credential helper (apiKeyHelper, awsAuthRefresh,
    awsCredentialExport, gcpAuthRefresh, otelHeadersHelper, proxyAuthHelper),
    processWrapper, a gateway or XAA login pin, policyHelper or policyHelpers (any
    value), a non-empty key that runs a command or starts a connection, upload,
    service or background work (hooks, statusLine, managedMcpServers,
    forceRemoteSettingsRefresh and the others listed in section 5), or an
    incompatible forceLogin selector; and when an ancestor of the native workspace
    holds .git or .claude.
  Workspace: a fresh 0700 directory under the user's private XDG_RUNTIME_DIR,
    removed afterwards. Precondition: XDG_RUNTIME_DIR is set and private; otherwise
    run_n1 falls back to the shared system temporary directory (section 5).
  N1-native: run_n1 on the registered directory; claude -p --input-format stream-json
    --output-format stream-json --verbose --safe-mode --no-session-persistence;
    initialize, then get_usage(skip_behaviors=true); no user message. 120 s total,
    45 s per operation, 5 s persistence wait, at most two get_usage with one named
    65 s wait for the native cache shortcut. Optional accounting tunnel limited to
    api.anthropic.com:443 and platform.claude.com:443; other hosts are refused only
    for traffic that honors HTTPS_PROXY; direct egress is neither counted nor blocked.
    Native and its process group are reaped; a descendant that calls setsid() escapes.
    Expected native traffic (fixture-observed; real counts may differ): profile,
    feature evaluation, bootstrap, penguin_mode, grove, referral eligibility, usage,
    mcp-registry, event_logging; token refresh to platform.claude.com if expired or 401.
    Possible auth/network effect: for a Team/Enterprise organization native may fetch
    server-managed policy (api.anthropic.com /api/claude_code/settings, /policy_limits)
    and apply it during the run; the harness detects its cache only afterwards and then
    reports server_policy_fetched and keeps no replies, windows or cache fields. A
    managed forceRemoteSettingsRefresh, which would force that fetch and block
    startup on it (native exits if the fetch fails), is refused before the run.
    Unobserved: server-side feature defaults for a real account (for example
    background memory consolidation) may start traffic the fixture runs did not show;
    a model event on stdout fails the run, other traffic is seen only by the tunnel.
    Retained: sanitized result JSON (classification, booleans, normalized windows,
    cache advance, byte and connection counts). No token, raw record or raw body.
  N1-bearer (only if G0 selects option 2 or 3; credential read NOT yet approved):
    read the access bearer memory-only from <config>/.credentials.json; exactly one
    GET https://api.anthropic.com/api/oauth/usage (option 3: plus one GET
    /api/oauth/profile) with that bearer; 5 s socket, 10 s total, 64 KiB, no retry,
    redirect or refresh; run after N1-native so native, not the kit, refreshes.
    Retained: HTTP status, allowlisted error type, normalized windows, the two
    profile UUID equality booleans.
```

## 6. Proved, inferred, unproved

- **N:** native store rotation by atomic rename, 0600, single link; 401- and
  expiry-triggered native refresh to platform.claude.com; `invalid_grant`
  clears the store; startup auxiliary set and its independence from
  `ANTHROPIC_BASE_URL`; nonessential-off suppresses native usage; zero model
  traffic in all cases.
- **S:** store path/selector rules, record schema, write pattern and locks,
  Keychain service/account naming and read call, usage row names, profile
  endpoint use at login/refresh; the 2.1.322 settings-key classification and
  the `policyHelper`, managed-hooks and `otelHeadersHelper` read sites.
- **Fixture-proved:** reader refusal matrix and minimization; exact-bearer
  usage client bounds (including resolution and pre-send deadline) and
  normalization; every required admission sequence and the unproved-row
  refusal; harness registration, selector refusal, policy-source coverage
  (drop-in order, server-managed caches, managed-mcp.json, uninspectable OS
  sources), refusal of every reviewed 2.1.322 helper, auth, transport and
  active settings key in any document (`policyHelper` and
  `otelHeadersHelper` included), the workspace ancestor check, no quota data
  in a voided result, bounds, fd closing, group reaping, protocol shape
  refusal, sanitization and traffic accounting.
- **I:** same-bearer usage is fresh and credential-attributable; `/usr/bin/
  security` avoids an ACL prompt; retaining same-identity streams is safe.
- **U:** live usage/profile availability for a full-login bearer; server
  acceptance of a non-native User-Agent; `null` reset semantics; `limits`
  model scoping and the semantics of every other unproved row; macOS
  Keychain behavior, ACL and fallback; macOS managed preferences and the WSL
  registry as policy sources; server-managed policy eligibility and content;
  settings keys of other native versions; native's project-root lookup;
  server-side feature defaults for background work on a real account;
  real refresh cadence and concurrent native refresh; proxy-required
  networks; measured poll/age/budget values.

Remaining gates (not closed by this node):

- G0: credential interface (options below); production `allowed_models` and
  model-to-family mapping; admission semantics of unproved rows; stream
  retention on same-identity rotation; idle-expiry and straggler behavior
  (section 3).
- K: independent stream-closing timer at lease deadline and on monitor
  loss; refresh egress to platform.claude.com; lock-held fetch bound.
- N1/HLogin: Linux only; accounts with server-managed policy refused until
  signed server-managed policy can be evaluated; the settings-key denylist
  re-reviewed for any native version other than 2.1.322; a private
  `XDG_RUNTIME_DIR` for the workspace; setsid escape and
  proxy-only tunnel accounting accepted as stated limits or closed by an
  outer boundary.

## G0 credential-interface options

| Option | Binding | Cost |
| --- | --- | --- |
| 1. Native correlated observation/handle | Best | Does not exist in 2.1.322 (S); external dependency |
| 2. Memory-only bearer + same-bearer usage + exact-bearer gate, local UUID identity | Quota bound to admitted credential; account binding weak (gap test) | Credential-bearing read surface; Linux only until macOS proved |
| 3. Option 2 plus same-bearer `/api/oauth/profile` identity | Quota and account bound to the bearer | One more endpoint and request per check; profile carries personal fields (only two UUIDs kept) |
| 4. macOS Keychain read for options 2/3 | Same as chosen option | Unexecuted design; ACL/prompt and fallback unproved; NM evidence needed |
| 5. Native `get_usage` plus cache timestamp (PA1F) without bearer read | Not bound to a bearer | Cannot close refresh or A/B/A races; insufficient alone |
| 6. Keep protected Claude refused | n/a | No reserves for Claude; setup-token mode with reserves off unchanged |

Recommendation (I): option 3 on Linux for the live proof, option 4 gated on
NM, with option 6 as the outcome if live permission fails or G0 rejects the
read surface. Do not weaken full A or ship a partial provider.

## Reproduction

```sh
Q=docs/superpowers/plans/quota-reserve-probes/qc
for t in test_bearer test_usage test_admission test_n1_harness test_native_evidence; do
  python3 -I -B "$Q/$t.py"
done
python3 -I -B "$Q/check_native.py"
cd "$Q" && python3 -I -B ../pc/network_guard.py python3 -I -B native_observe.py "$(command -v claude)"
pre-commit run --all-files
```

Recorded results: 18, 21, 23, 25 and 2 tests OK; the checker passes; the
retained native run (run-5) exited 0 under the guard, which again denied
only its four self-check syscalls. Earlier runs also exited 0: run-1 lacked
the base-URL case, run-2 the key redaction, run-3 measured exit and model
fields only by assertion, run-4 lacked method and path for incomplete
requests. Raw fake-only outputs, the temporary-root inventory and static
excerpts are archived in the worktree's ignored `.cache/qc/`; the temporary
roots were removed after archival. The coordinator should preserve that
archive before removing the worktree.

## Review round 1 changes

| Finding | Resolution |
| --- | --- |
| I1 policy sources | Code and tests: drop-ins in native order, server-managed caches refused, policy helpers refused, macOS/WSL refused, post-run server-policy detection; scope and limits in section 5 and the N1 draft; remaining gate listed |
| M1 deadline | Code and tests: watchdog before resolution, bounded resolution, remaining time per connect, check before first header byte; bound and lock cost in sections 2, 3 and 4 |
| M2 unproved rows | Code and test: `quota_row_unproved` while any unproved row is present; `allowed_models` mapping and row semantics listed as G0 gates |
| M3 monitor costs | Documented as G0/K inputs (section 3, K paragraph, remaining gates) |
| M4 evidence fields | Code and rerun: measured `exit_status`, counted `model_events`, `unrecorded_connections` disclosed (section 4) |
| M5 harness limits | `protocol_malformed` for non-object replies (tests); setsid and tunnel limits stated in section 5 and the N1 draft |

## Review round 2 changes

| Finding | Resolution |
| --- | --- |
| I2 `policyHelper`, `otelHeadersHelper` | Code and tests: `policyHelper` (any value, `{"path": null}` included) refuses with `policyHelpers` as `policy_helper_unevaluable`; `otelHeadersHelper`, the gateway auth pins and `xaaIdp` refuse as `policy_conflicting_auth`; the whole 2.1.322 settings schema was reviewed, keys that run a command or start a connection, upload, service or background work refuse (`forceRemoteSettingsRefresh` included), and a managed-mcp.json refuses. The reviewer's reproduction is a test. Section 5 table and key review, N1 draft and section 6 corrected |
| M6 voided result | Code and test: a `server_policy_fetched` result keeps no `replies` or `cache` field and no window data |
| M7 inference label | Section 4 labels the body-size cause I; section 2 says two TLS tests |
| M8 workspace | Code and tests: default parent is a private `XDG_RUNTIME_DIR`; any workspace ancestor with `.git` or `.claude` refuses before spawn; fallback race and `forceRemoteSettingsRefresh` (now refused) in section 5 and the N1 draft |
| M9 carried limits | Kept in sections 3 to 6 and in the K and N1 remaining gates |
