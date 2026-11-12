# QC: attributable model-free Claude quota and credential binding

Feasibility node QC, research date November 12, 2026, base `0c5b492`.
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
[fake](quota-reserve-probes/qc/fake_usage.py), 15 tests:

- Production target `https://api.anthropic.com/api/oauth/usage`, verifying
  default TLS context, `Authorization: Bearer`, `anthropic-beta:
  oauth-2025-04-20`, `Cache-Control: no-cache`, `Connection: close`, neutral
  User-Agent (whether the server accepts a non-native agent is U).
- 5 s per socket operation, 10 s total enforced by a watchdog that shuts the
  socket down (slow-drip peers cannot extend it), 64 KiB headers and body,
  no redirects (3xx refused), no retries, no refresh, no proxy discovery.
- Only a fresh direct 200 with `application/json` and every enabled window
  valid is usable. 401/403/429/5xx, transport errors, deadline, malformed or
  cache-sourced data are never fresh.
- Windows by name and duration: utilization finite in [0, 100]; RFC 3339
  reset after the receive time (120 s skew) and no later than one window
  duration ahead; `null` reset is `reset_unknown` (live semantics U).
  Native's other rows (`seven_day_oauth_apps`, `seven_day_opus`,
  `seven_day_sonnet`, `cinder_cove`, `wattle_ember`, `extra_usage`, `limits`)
  and any `five_hour_*`/`seven_day_*` row are counted, never substituted.
- Option-3 helper `fetch_profile_identity` (same bounds) keeps only
  `account.uuid` and `organization.uuid` from `/api/oauth/profile`, the
  endpoint native uses at login and refresh to bind a token to an account.

## 3. Admission and rotation contract

[admission.py](quota-reserve-probes/qc/admission.py) with
[gate_server.py](quota-reserve-probes/qc/gate_server.py), 22 tests on a
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
model) is refused as `model_family_unproved`.

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
`get_usage(skip_behaviors=true)`, no user message, 45 s bound. All six cases
exited 0 with zero model attempts and no CONNECT outside the two fixture hosts.

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
registered stream and nothing passive reopens it.

## 5. N1 harness and human permissions

[n1_harness.py](quota-reserve-probes/qc/n1_harness.py) with a fake native
([fake_claude.py](quota-reserve-probes/qc/fake_claude.py)), 17 tests:
alias-to-path resolution without enumeration; `create_pending` (exclusive
mkdir, 0700, `account.json` 0600, state pending, identity null, random
enrollment id, exact `config_directory`; existing or pending refuses);
`mark_ready` only when local metadata equals the human-confirmed identity,
written atomically; strict registration schema; refusal of conflicting
caller variables (store/provider/endpoint/proxy/CA, nonessential-off) and of
settings with conflicting `env`, auth helpers, a non-claude.ai
`forceLoginMethod` or a mismatching `forceLoginOrgUUID`; policy files are
read, never rewritten, and real HOME is kept. The child gets an allowlisted
environment, closed inherited descriptors (verified), its own session, 4 MiB
output and stderr bounds, 45 s per operation and 120 s total, and is reaped
with its process group (grandchild verified). Output is a sanitized
allowlist; a fabricated token echoed by the fake never appears. A cache
shortcut is inconclusive unless the approved procedure names one bounded
65 s wait (at most two queries). An optional loopback CONNECT tunnel counts
host:port and bytes and refuses unlisted hosts. It was never run live.

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

N1 permission (same alias, no login)
  N1-native: run_n1 on the registered directory; claude -p --input-format stream-json
    --output-format stream-json --verbose --safe-mode --no-session-persistence;
    initialize, then get_usage(skip_behaviors=true); no user message. 120 s total,
    45 s per operation, 5 s persistence wait, at most two get_usage with one named
    65 s wait for the native cache shortcut. Optional accounting tunnel limited to
    api.anthropic.com:443 and platform.claude.com:443 (other hosts refused).
    Expected native traffic (fixture-observed; real counts may differ): profile,
    feature evaluation, bootstrap, penguin_mode, grove, referral eligibility, usage,
    mcp-registry, event_logging; token refresh to platform.claude.com if expired or 401.
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
  endpoint use at login/refresh.
- **Fixture-proved:** reader refusal matrix and minimization; exact-bearer
  usage client bounds and normalization; every required admission sequence;
  harness registration, selector/policy refusal, bounds, fd closing, reaping,
  sanitization and traffic accounting.
- **I:** same-bearer usage is fresh and credential-attributable; `/usr/bin/
  security` avoids an ACL prompt; retaining same-identity streams is safe.
- **U:** live usage/profile availability for a full-login bearer; server
  acceptance of a non-native User-Agent; `null` reset semantics; `limits`
  model scoping; macOS Keychain behavior, ACL and fallback; real refresh
  cadence and concurrent native refresh; proxy-required networks; measured
  poll/age/budget values.

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

Recorded results: 18, 15, 22, 17 and 2 tests OK; the checker passes; the
retained native run exited 0 under the guard (two earlier exploratory runs
also exited 0; the first lacked the base-URL case, the second the key
redaction). Raw fake-only outputs, the temporary-root inventory and static
excerpts are archived in the worktree's ignored `.cache/qc/`; the temporary
roots were removed after archival. The coordinator should preserve that
archive before removing the worktree.
