# QX: Codex attributable model-free quota and credential binding

Status: **feasibility result, offline only**. With the real Codex binary,
fabricated credentials and loopback fakes, a per-invocation gate that serves
both `chatgpt_base_url` and the model provider can bind the exact bearer and
`ChatGPT-Account-ID` it admits to fresh five-hour and weekly windows. That
binding survives native refresh, an account switch, A/B/A and two sessions
sharing one account. It does not need a wrapper credential read or a wrapper
refresh. What a real server returns for a real bearer is **not proved**. No
product runtime changed. This is not a G0 pass and not production capability.

Base `0c5b492`, research date November 12, 2026. Linux x86_64, `codex-cli
0.167.2`, binary SHA-256
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b` (the same
binary PC, PC2 and PR used). macOS (0.166.0 there) was not tested. Source
facts refer to `codex-rs/` at the 0.167.2 release commit
`ce21072c90d32c40af7a55b60b2fb35c84a43a6a` that PR archived. QX inspected that
source and did not rebuild it.

Labels: **N** native observation in this node, **S** inspected source or
documentation, **I** inference, **U** unproved.

## Evidence boundary: no real credential, network or account

- Every native process got a new temporary `CODEX_HOME`/`HOME` and a
  fabricated `auth.json` (unsigned JWTs naming `fixture-account-*`). Nothing
  was read from the real home, Keychain or secrets directory. The two consumed
  setup-token experiments were not touched.
- Native stages ran under PC's reviewed Linux seccomp/ptrace guard. That guard
  allows only Unix sockets and IPv4 TCP to 127.0.0.1, and its self-check passed
  first. Each app-server made exactly one `socket()` call with another family
  or type, and the guard denied it (`guard_denied_syscalls {"41": n}` in the
  manifest, with n equal to the number of app-servers in the stage). The reused guard does not record the family (U). No such call
  reached a network.
- All proxy variables pointed at the gate, which refuses `CONNECT` and records
  the destination. Production-host refresh attempts appear there as refused
  `auth.openai.com:443` tunnels (N). Nothing else tried to leave.
- `check_evidence.py` fails if the evidence contains JWT-shaped text, refresh
  values or home paths. Bearers are compared through a per-process HMAC and
  recorded only as fixture labels (`T1`, `T2`, `TA`, `TB`).
- A test-only guard is research containment, not the product boundary (PO).

## 1. What `account/rateLimits/read` sends and requires

The table below shows the request at the gate, with `chatgpt_base_url =
http://127.0.0.1:<PORT>/backend-api/` (N, `evidence/shape.json`):

| Property | Observed |
| --- | --- |
| Method, path | `GET /backend-api/wham/usage`, no query. A base URL without `/backend-api` uses `/api/codex/usage` instead (S: `PathStyle::from_base_url` is a substring test; PC observed the other form) |
| Headers | Exactly `accept`, `authorization`, `chatgpt-account-id`, `host`, `user-agent` |
| Bearer | `Bearer` plus the access token native holds in memory (equals the file token `T1`; after refresh, `T2`) |
| Account | `ChatGPT-Account-ID` = `tokens.account_id` of the login record |
| User-Agent | Product is the connected client's `clientInfo.name/version` (`qx-native-research/0.167.2`), so the UA is not an identity signal |
| Other identity headers | None on usage. Model POSTs add `originator`, `session-id`, `thread-id`, `x-client-request-id`, `x-codex-*` |
| Extra request | Without `excludeResetCreditDetails`, also `GET /backend-api/wham/rate-limit-reset-credits` |
| Model requests | Zero in the whole stage. No thread or turn is needed |
| Caching | Each RPC produced exactly one upstream GET. 22 reads, 22 GETs (S: the handler has no cache) |

The response native requires (N, plus S `codex-backend-openapi-models`) is a
JSON object with `plan_type` (required string; unknown values become
`unknown`) and an optional `rate_limit` object `{allowed, limit_reached,
primary_window, secondary_window}`. Each window is `{used_percent: i32,
limit_window_seconds: i32, reset_after_seconds, reset_at: i32}`. The optional
fields are `account_id`, `user_id`, `additional_rate_limits[]` (`limit_name`,
`metered_feature`, `rate_limit`, `normal_model_slug`), credits and spend
control. The RPC returns `rateLimits` (the `codex` family), `rateLimitsByLimitId`
keyed by `metered_feature`, `accountId`, and `ordinaryUsageAllowed`. The last is
null unless the response `account_id` and `user_id` equal the active login's (S).

Normalization: native keeps each window **in the slot the server used**. When
the fixture swapped them, `primary` became `windowDurationMins: 10080` (N).
Duration is `ceil(seconds / 60)`, and zero gives `null` (N/S). A consumer must
map windows by duration (300 = five-hour, 10080 = weekly), never by slot. A
model-specific row (`metered_feature codex_bengalfox`, `limit_name` = a model
slug) appears as another family. The top-level view stays `codex` (N). The
App Server rounds the core `f64` to `i32` (S); the usage wire is already an
integer. Response headers `x-codex-primary-used-percent` etc. carry fractional
values on model responses (S, `codex-api/src/rate_limits.rs`).

Missing, malformed and stale cases (N; native result, then the lease's verdict):

| Fixture response | Native 0.167.2 | Lease (`binding.py`) |
| --- | --- | --- |
| `secondary_window: null` | success, `secondary: null` | five-hour only; weekly missing -> pause if weekly is enabled |
| `rate_limit: null` | success, both windows null | `no-codex-family` (unavailable) |
| `used_percent 25.5` | **RPC error** (expects `i32`) | parses 25.5 unrounded |
| `used_percent 150` or `-5` | success, passes the value through | `out-of-range` |
| `limit_window_seconds 0` | success, duration null | `malformed` |
| `reset_at` two hours in the past | success, passes it through | `reset-anomaly` |
| `allowed false, limit_reached true` | success | pause `limit-reached` |
| `plan_type` missing | RPC error | `malformed` |
| unknown or usage-based plan | success (`unknown`, `self_serve_business_usage_based`) | `unsupported-plan` (latched) |
| `account_id` B, `user_id` B, or both absent | success, returns B or null | `identity-changed` pause |
| HTTP 401 | RPC error, **no refresh attempt** (0 CONNECTs; S: the backend client has no 401 recovery) | failure; freshness not renewed |
| HTTP 503, non-JSON | RPC error | failure, `malformed` |

So native success is not validity. Native passes impossible percentages, past
resets, foreign accounts and unknown plans through as successful reads. The
response has no observation timestamp. Freshness can come only from the
consumer seeing a new server response.

## 2. Gate-observed correlation

Mechanism: the invocation's gate is the HTTP endpoint for both
`chatgpt_base_url` and the forced custom provider (`requires_openai_auth =
true`, `supports_websockets = false`). Native talks plain HTTP to loopback, and
the gate originates the upstream TLS itself, so there is **no TLS
interception**. For every forwarded usage exchange the gate records (bearer
digest, request account, upstream response). Every model request is admitted
under the admission lock only if its (bearer digest, account) equals the
binding of a fresh, passing observation. This is done before any upstream byte
(`gate.py`, `binding.py`).

Native results (all N):

- **Same process, same credential.** The usage read and the model client use
  the same app-server `AuthManager`. `getAuthStatus {includeToken: true}`
  returned the bearer the gate saw (`T1`, later `T2`). Model requests read the
  current snapshot on every request (S: `AuthManagerAuthProvider`).
- **Refresh after 401** (`rotation`): `T1` was revoked at the fake server.
  Native sent two `T1` POSTs (both 401), refreshed at the token endpoint, wrote
  `T2` into `auth.json`, then sent a `T2` POST. The gate refused it with 503
  `rebind-required` before upstream, and the turn failed (provider retries 0).
  The gate answers 503, not 401, so it never starts native refresh. A usage read
  then carried `T2`, the binding moved to epoch 2, and the next turn completed.
  No `T2` model byte left before the `T2` usage read.
- **Hold-and-rebind** (`hold`): with the optional hold (5 s bound), the gate
  held that first `T2` request without forwarding it. It asked the controller,
  over a second App Server connection, for a native usage read, which carried
  `T2`. It then admitted the request, and the turn **completed across the
  refresh**. Order: `T2` refused/held -> `T2` usage -> `T2` admitted -> `T2`
  upstream.
- **Proactive refresh** (`proactive`): when an access token expires within five
  minutes (S: `CHATGPT_ACCESS_TOKEN_REFRESH_WINDOW_MINUTES = 5`), native
  refreshes at startup, before the first usage read. The usage GET then already
  carries `T2`. Without the internal test hook, native made 7 refused
  `CONNECT auth.openai.com:443` attempts through `HTTPS_PROXY`. It then sent the
  usage read with the old `T1`. A failed refresh is not fatal (S: `auth()` logs
  the failure and keeps the old token).
- **Refresh does rotate the bearer the gate sees**, in all three paths above.
  The gate never sees refresh traffic.

Which paths reach the gate, and which do not (N for the observed rows):

| Native traffic | Configurable to the gate | Evidence |
| --- | --- | --- |
| `/backend-api/codex/responses`, `/backend-api/codex/models?client_version=...` | yes, provider `base_url` | N |
| `/backend-api/wham/usage`, `/wham/rate-limit-reset-credits` | yes, `chatgpt_base_url` | N |
| `/backend-api/wham/accounts/check` (startup, new thread), `/wham/settings/user` (each turn) | yes, `chatgpt_base_url` | N. The fixture answered 503 and the app-server tolerated it; PC2 showed the TUI needs a real `accounts/check` |
| config bundle, workspace messages, plugins, connectors, compaction, memories, realtime | provider or `chatgpt_base_url` for some; realtime has its own URLs | S/PR. Not exercised here; a census is still needed (U) |
| Token refresh `https://auth.openai.com/oauth/token` | **no** (documented config). Only the internal env `CODEX_REFRESH_TOKEN_URL_OVERRIDE` (codex-as refuses it today) or the login record's `oauth_provenance.issuer` (written by native login; the wrapper must not rewrite it) | S, N (CONNECT via proxy) |
| Revoke on logout `https://auth.openai.com/oauth/revoke` | no (derived from the refresh endpoint) | S |

**Alternatives considered:**

- *Native in-memory export* (`getAuthStatus includeToken`, N): this returns
  exactly the bearer native uses. It is a protocol v1 method, so internal and
  best effort. It moves the token into wrapper memory and adds nothing to the
  gate comparison. Useful at most as a diagnostic cross-check.
- *Wrapper read of `auth.json`*: codex-as **already reads the whole file**
  (`guard.credential` returns raw bytes, tokens included) to validate it and to
  take identity from the `id_token` claims. It does not use `access_token`. The
  change would be to start using that field and to originate a usage GET from
  the wrapper. The `concurrent` stage shows why that is weaker. After session
  one refreshed, the shared secret held `T2` while session two kept sending its
  cached `T1` (admitted by its own gate, upstream 200). A file read would have
  bound the wrong bearer for that session. Before 0.167.0, native saves by
  truncate-and-write (codex-as docs), so a concurrent read can be torn. A
  wrapper usage client is a second client on the inference TLS host. The file
  route still needs the gate's per-request bearer comparison. Not recommended.
- *Gate-initiated usage fetch with the in-transit bearer* (I): the gate holds
  the request and GETs usage upstream itself with that request's bearer. This
  removes the controller round trip. The cost is a usage client and protocol
  knowledge in the gate, plus extra usage calls. Hold-and-rebind gets the same
  UX through native's own read, so this is not needed.

## 3. Rotation sequences (deterministic offline tests)

`test_binding.py` (22 tests; 5 code mutations each fail at least one test),
`test_gate.py` (10 HTTP tests through the real gate and fake upstream) and the
native stages:

| Required sequence | Offline test | Native stage |
| --- | --- | --- |
| Native refresh to a new bearer, same account | `test_native_refresh_same_account_rebinds_without_pause`, `test_rotated_bearer_needs_its_own_usage_read`, `test_hold_and_rebind_*` | `rotation`, `hold`, `proactive` |
| A -> B with stale A data | `test_account_change_with_stale_a_data_pauses_and_needs_continuation`, `test_server_reported_other_account_or_user_is_an_identity_change` | `identity`: after `account/login/start chatgptAuthTokens` B, both the existing and a new thread sent `TB` with account B; refused before upstream |
| A/B/A | same test plus `test_a_b_a_continuation_while_native_still_on_b_stays_paused` | `identity`: back on the same `TA`, still refused; a fresh A read alone stays paused; a deliberate continuation plus a newer read resumes; the turn completes |
| Two codex-as sessions sharing account and lease | `test_two_sessions_share_an_account_with_their_own_leases`, `test_second_launch_shares_the_account_lease_with_its_own_binding` | `concurrent`: one forced refresh (count 1); session two adopted `T2` from disk without its own refresh, then each gate refused `T2` until its own read |
| Unchanged-but-fresh extends; repeated failures do not | `test_unchanged_but_fresh_data_extends_age`, `test_repeated_failures_do_not_extend_age`, `test_rotation_does_not_restart_the_recovery_deadline` | each native read is one upstream GET |
| Unsupported family or plan refused | `test_unsupported_family_and_plan`, normalization tests | `shape` |
| Expired freshness pauses; no auto-resume | `test_expired_freshness_pauses_and_never_auto_resumes`, `test_independent_reset_while_paused_only_allows_continuation` | `identity` continuation gate |

Lease rules (I, implemented in `binding.py`):

- A different credential digest starts a new epoch, and no older observation
  carries over.
- The freshness deadline runs from the last fresh observation of any epoch, so
  rotation or failures never restart it.
- Same-account rotation refuses (or holds) without pausing and without closing
  already admitted streams.
- An account or user change, an unsupported plan or family, staleness, a
  missing enabled window, `limit_reached` or a threshold latches a pause, closes
  registered streams and requires `request_continuation(trusted=True)` followed
  by a strictly newer passing read.
- Equality pauses. Zero is enabled, and 100 always pauses.
- Model-response headers can renew freshness only for the bound credential, and
  never at startup.

## 4. Coexistence with codex-as leases and post-run repair

`test_lease_coexistence.py` drives the unchanged `lib/codex-as/{check,layout}.py`
in a temporary directory:

- A native-style atomic refresh through the account link keeps the link and a
  0600 single-link secret. Post-run repair stays quiet (same ChatGPT user). The
  lease needs one rebind and no file action. The native `concurrent` stage shows
  the same on 0.167.2 through real links.
- An in-session login as B (link removed, regular file written) pauses the gate
  as `identity-changed`. The existing repair refuses with "diverged" and
  preserves both files. Both mechanisms independently refuse to continue
  silently under B.
- The expected identity is `storage.owner(baseline)`, i.e. the claims and
  `tokens.account_id` codex-as already extracts. Native's account header equals
  `tokens.account_id` (N). The wrapper never needs the access token.

What X must preserve, and the minimal changes (I):

- Keep the file store, shared `sqlite_home`, the update-check override, account
  flock leases, `enroll`, `post_run`, `remember` and the `--info`/`--labels`
  formats unchanged. The protected app-server and the `--remote` TUI are both
  native clients of the account. Each must inherit the account lease fd, as
  `run_native` passes it today, so "the last client repairs" still holds.
  `post_run` runs after both exit.
- X injects the forced provider, `chatgpt_base_url` and the realtime sink as
  `-c` overrides on **both** native processes (PR). It must keep refusing the
  same keys, `--remote`, `CODEX_REFRESH_TOKEN_URL_OVERRIDE` and the other
  endpoint variables when users supply them. Project config already cannot set
  `chatgpt_base_url` (S, config reference).
- The wrapper never refreshes, rewrites or copies credentials. The gate holds
  only an HMAC of the bearer for comparison and the in-flight header while
  forwarding.
- Protected mode refuses on any failure of these best-effort interfaces,
  whereas AGENTS.md says internal interfaces fall back to plain native
  behavior. Adopting this candidate means G0 accepts that conflict (the spec
  requires refusal).

## 5. Boundary destinations for K, and config keys

| Destination | Purpose | Route | TLS host shared with inference? |
| --- | --- | --- | --- |
| `chatgpt.com` `/backend-api/codex/*` | inference, models, compaction, memories | gate (provider `base_url`) | is the inference host |
| `chatgpt.com` `/backend-api/wham/*` | usage, reset credits, accounts/check, settings, config | gate (`chatgpt_base_url`) | **yes**; cannot be split by domain or CONNECT |
| `auth.openai.com:443` `/oauth/token` | native refresh (N: CONNECT through `HTTPS_PROXY`) | allowed by name outside the gate, or refresh fails | no |
| `auth.openai.com` `/oauth/revoke`, login, device auth, `/api/accounts` | logout revoke, enrollment (enrollment runs unprotected) | same host as refresh | no; allowing refresh also permits logout revocation (compatibility, not inference) |
| `gov.chatgpt.com`, FedRAMP header | FedRAMP default | not supported in protected mode (refuse) | n/a |

K must therefore deny direct `chatgpt.com` from the whole invocation. The gate
alone reaches it, with a reviewed path allowlist (accounts/check is needed for
TUI bootstrap; PC2). K must allow `CONNECT auth.openai.com:443` or accept that
refresh fails and the session pauses at expiry (I). Without that permission the
startup proactive refresh fails and native keeps the old token (N). A later
401 cannot recover, so the result is a stale pause, never a bypass.

| Key or interface | Status |
| --- | --- |
| `model_provider`, `model_providers.<id>.{base_url, requires_openai_auth, supports_websockets, request_max_retries, stream_max_retries, wire_api}` | documented config reference |
| `chatgpt_base_url` | documented key ("base URL used during the ChatGPT login flow"); its use for usage/account/settings requests and the `/backend-api` path switch is source behavior: **best effort** |
| `cli_auth_credentials_store = "file"` | documented; codex-as already forces it |
| `account/rateLimits/read` with `excludeResetCreditDetails` | documented App Server method; the parameter is in the schema (S) |
| `/wham/usage` JSON fields, `x-codex-*` response headers | undocumented backend contract: best effort, fail closed |
| `getAuthStatus includeToken` | protocol v1, internal |
| `CODEX_REFRESH_TOKEN_URL_OVERRIDE`, `oauth_provenance` | internal; test hook only, not for product |

## 6. Result, permissions, G0 options and the contract K and X need

**Proved (N, Linux, 0.167.2, fakes only):** model-free usage reads and their
exact request identity; native's lack of validation; per-request bearer
rotation visible at the gate on 401, proactive and adopted-from-disk refresh;
refusal before upstream of every unbound, foreign or paused credential;
hold-and-rebind continuity; independent per-session bindings over one shared
secret; refresh host and its proxy CONNECT.

**Inferred (I):** the lease rules and API; K's destination split; that real
servers answer usage for the bearer's own account/user and include both
identity fields; the plan allowlist (`plus, pro, prolite, promax, team,
business` is a research default only).

**Unproved (U):** real-server semantics for any of the above (permission,
whether `account_id`/`user_id` are present and match the claims, integer
percentages, durations, additional rows, rate limits on usage, caching in
front of the server). Also unproved: real refresh cadence and access-token
lifetime; macOS 0.166.0; WebSocket and realtime routes; a complete census of
`chatgpt_base_url` paths; the TUI composition with a forwarding gate; the
denied socket family.

**Live check, only after separate human approval:** one named account
(`<NAME>`, its existing codex-as home, no login). One owned app-server with this
gate forwarding over TLS to the production backend. One or two
`account/rateLimits/read` calls; no thread, no model request, no retry beyond
the approved count. Retain only: status codes, header names, window durations
and percentages, reset times, plan, and booleans saying `account_id`/`user_id`
equal the credential claims. Never retain tokens, ids or raw bodies.
Disclose that native may refresh at startup (contacting `auth.openai.com` and
rewriting the secret through the link) if the token is within five minutes
of expiry.

**G0 options:**

1. *Gate-observed correlation with hold-and-rebind* (recommended). Costs: an
   HTTP/SSE forwarding relay for chatgpt.com (WebSockets off) with a maintained
   path allowlist; dependence on best-effort `chatgpt_base_url` routing and the
   usage JSON; controller polls within max-age (the native TUI also polls every
   60 s, or 5-30 s near exhaustion (S), and those reads renew too); a bounded
   hold on every token rotation; X must inject and keep refusing routing keys.
   No credential read, export or refresh by the kit.
2. Option 1 without the hold. Simpler, but every native rotation fails the
   in-flight turn once (N), and the user must resubmit.
3. Wrapper file read of the access token plus a wrapper usage client. Costs:
   disk/memory skew (N), torn reads before 0.167.0, a second client on the
   inference host, and a larger credential surface. It still needs the gate
   comparison.
4. Wait for a native correlated quota/credential-epoch interface. None exists
   in 0.167.2 (S).

**Contract for K and X** (I; names may change):

```text
Lease(expected_account, expected_user, policy{five_hour, weekly, max_age}, plans, clock, wall)
  observe_usage(bearer, account_header, status, body) -> outcome   # forwarded native read only
  observe_headers(bearer, account_header, headers)    -> outcome   # optional, running state only
  admit(bearer, account_header, model) -> (admitted, reason)       # under the admission lock, before bytes
  tick() -> paused reason or None                                  # monitor
  ready() -> bool                                                  # startup preflight verdict
  request_continuation(trusted) -> bool                            # trusted control (PC5) only
  on_close(reason)                                                 # gate closes registered streams
Gate(lease, upstream, rebind=controller_read_trigger, hold=seconds)
Controller: app-server -> account/rateLimits/read before the TUI starts; poll within max_age;
            on rebind trigger, read on a separate connection; continuation -> read.
```

A gate restart starts a new `Lease`, and nothing persists. The expected
identity comes from the credential claims captured at launch.

## Reproduction

From the worktree root, on Linux x86_64 with `codex` on PATH:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/qx/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/qx/check_evidence.py
pre-commit run --all-files
```

`run.py` runs the three offline suites, the guard self-check, then six native
stages (`shape`, `rotation`, `hold`, `proactive`, `identity`, `concurrent`),
each under the guard with a 60-75 s outer timeout. A timeout or failure is
recorded and is never evidence. It writes sanitized `evidence/*.json` and the
manifest, appends each temporary `quota-qx-*` root to `.cache/qx/temp-roots.txt`,
archives it under `.cache/qx/roots/` and removes exactly those roots. Without a
native binary it prints SKIP after the offline suites, and that proves nothing.
The final run passed all ten steps, and the checker passed. A tampered copy of
the evidence fails the checker.
