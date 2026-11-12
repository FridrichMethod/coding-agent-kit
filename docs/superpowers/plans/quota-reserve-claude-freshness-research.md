# PA1F: model-free full-login quota freshness

Research date: November 12, 2026. Base: `bb195717`.
Evidence only; no launcher, enrollment, credential schema or runtime change.
The coordinator records the accepted native-login/shared-device boundary and
the consumed single count-request authorization. This node uses no real
credentials, login or real endpoint requests. G0 remains closed.

## Result

Native full-login-shaped credentials can fetch subscription usage without a
model request in the local HTTPS fixture. The public `get_usage` reply alone
cannot establish freshness: a new successful GET, a cache hit, and a failed
GET falling back to old data can return identical quota objects.

The account-local native `cachedUsageUtilization.fetchedAtMs` is a measurable
additional signal in this build. It advances on the tested successful fetches,
including unchanged percentages, and does not advance on cache hits or tested
failures. Disk persistence is asynchronous: some replies arrive before the
new timestamp reaches disk, but the fixture observed it while native remained
alive two seconds later. Two seconds is an observation, not a latency promise.

This supports a conservative freshness candidate, conditional on a stable,
separately established account and quota-family binding. It does not yet prove
that binding: neither the usage control reply nor its native cache identifies
the credential's live server account or organization. The cache checks a local
account UUID but does not store an organization UUID. Successful native token
rotation, live account permissions, concurrent account changes, provider-side
reporting age and macOS remain unverified.

## Reproduction and containment

Tested Linux Claude Code `2.1.322`, executable SHA-256
`bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478`.
This identifies the observation, not a supported minimum or version pin.

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_freshness/probe.py "$(command -v claude)"
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc/network_guard.py python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_freshness/tls_probe.py "$(command -v claude)"
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_freshness/check_evidence.py
```

The first probe reuses the unchanged PA0 runner and denies every socket,
connect, message/datagram send and io_uring setup. The TLS probe runs entirely
under the unchanged PC Linux x86_64 guard, which permits only loopback TCP and
Unix sockets. Its unchanged self-check verifies external IPv4, IPv6, UDP and
exec descendants are denied before native starts. Both use fabricated full
OAuth records, private temporary HOME/config/profile/XDG/workspace directories,
an allowlisted environment, closed inherited descriptors, no user messages,
and initialize plus two `get_usage(skip_behaviors=true)` requests per case.
The fixed hosted credential paths are checked for absence before execution;
their contents are never read. System files and machine policy may be read.
These are cooperative research guards, not hostile-code sandboxes.

Each native child has a 45-second total bound, bounded stdout/stderr and
owned-process-group cleanup using PA0's unchanged stop helper. Native commands
use `--safe-mode --no-session-persistence`; no settings-source override removes
machine policy. No settings, credentials or history come from the real HOME.
The probes retain exact temporary-root inventories, raw fake-only outputs,
exit records and sanitized evidence beneath the worktree's ignored
`.cache/pa1f/`. Static excerpts are retained there too; no native source dump or
temporary path is committed. Earlier TLS iterations remain in the same archive.

The TLS fixture uses documented `HTTPS_PROXY` and child-only
`NODE_EXTRA_CA_CERTS`. An ephemeral OpenSSL certificate/key stays in its private
root; nothing changes system trust or hosts files. The proxy accepts only
`CONNECT api.anthropic.com:443`, terminates TLS locally, and returns fixture JSON
only for `GET /api/oauth/usage`. Other hosts/paths receive 403; nothing is
forwarded. It records no header values except equality with a literal fake
bearer. No Messages or Complete POST attempts occurred. All native model costs,
API durations and usage were zero, with no user/assistant/stream events.
OpenSSL, strace and ptrace are research dependencies, not product dependencies.

Official [network configuration](https://code.claude.com/docs/en/network-config)
documents both transport settings. The documented
[API endpoint override](https://code.claude.com/docs/en/env-vars) does not route
this usage fetch in the inspected implementation: `KH -> jt.get -> x_(api)`
selects `sn().BASE_API_URL`. The internal custom-OAuth override is allowlisted
and is not used here. The local TLS fixture supplies evidence without proposing
production TLS interception or changing native OAuth endpoints.

## Measured matrix

[Offline observations](quota-reserve-probes/claude_freshness/observed-offline.json)
contain 11 cases with two usage replies each. Every child exited 0, all network
attempts were denied, and fabricated credential bytes stayed unchanged.

| Fixture input | Native result |
| --- | --- |
| Recent cache, matching account UUID | Returns stored 5h/week data; no timestamp advance |
| Two-minute or 59-minute cache, network unavailable | Returns the same stored data; no timestamp advance |
| More than one-hour-old cache, future timestamp, or no cache | `rate_limits_available: true`, but `rate_limits: null` |
| Cache UUID differs from local account UUID | Cache removed; rate limits null |
| Same account UUID, changed local organization UUID | Cached quota still accepted |
| Cache has only five-hour data | Weekly remains absent; never filled with zero |
| Two-minute cache with reset dates in year 2000 | Returns the old windows with their past reset dates |
| Inference scope only in fabricated full-login record | `rate_limits_available: false`; rate limits null |

The organization case is a local-metadata counterexample: the cache has no
organization field, so there is no recorded origin organization to compare.
It does not simulate a server-authenticated organization switch.

[TLS observations](quota-reserve-probes/claude_freshness/observed-tls.json)
contain eight cases. Each asks twice, waiting two seconds between replies to
observe asynchronous persistence without changing the native cache.

| Server fixture / initial state | HTTP usage GETs | Timestamp / returned data |
| --- | --- | --- |
| 200 full windows, no initial cache | 1 | New timestamp; two identical complete replies |
| 200 unchanged values, two-minute initial cache | 1 | Timestamp advances despite identical values |
| 200 five-hour only, old complete cache | 1 | Timestamp advances; old weekly window is removed |
| 403, old complete cache | 1 | Timestamp unchanged; both replies contain old complete data |
| 401, old complete cache | See recorded count | Timestamp unchanged; both replies contain old complete data |
| 429, old complete cache | 1 | Timestamp unchanged; both replies contain old complete data |
| 200 empty object, old complete cache | 2 | Timestamp unchanged; both replies contain old complete data |
| 200 full windows, expired fake access credential | 1 | Native attempts refresh host, then uses existing bearer; timestamp advances |

The two successful no-change cases and all seeded failure cases have the
same public quota fields. All replies expose only `session`,
`subscription_type`, `rate_limits_available`, `rate_limits` and `behaviors`.
Neither origin, fetched time, HTTP status, failure/seed state nor account
identity appears. `rate_limits_available` reflects a local capability gate,
not successful retrieval. Failure caching can also suppress the second HTTP
request, so polling more often does not establish freshness.

The expired-credential server deliberately accepts the old fake bearer. Native
attempted three connections to the refresh host, all rejected by the fixture.
This proves fallback after failed refresh in this case, not credential validity
or successful renewal. The 401 case likewise does not prove token rotation.

## Native source interpretation and bounds

These are version-specific implementation findings from the identified binary.
Symbols are scoped to their embedded modules, not stable interfaces.

| Byte location / symbols | Finding |
| --- | --- |
| 292578853 vicinity, `Gt`, `Be`, `Jco`, `dDi` | Persisted seeds accepted up to one hour; immediate snapshot shortcut below 60 seconds; rejects future timestamps and mismatched local account UUID |
| 292579906, `lDi` | Writes utilization, local account UUID and `Date.now()` after recognized usage response; suppresses writes when existing matching cache is younger than 60 seconds |
| 308960028 vicinity, `U` | Checks cache before endpoint; falls back to persisted/header seeds on failures and fieldless bodies |
| 308961005 | Only found caller of `lDi`; it receives the account UUID captured before fetch and checks it again before writing |
| 308962905 vicinity, `Gor` | Projects success and seeded results into the same public reply; strips internal origin/fetch-time/failure metadata |
| 292559975 vicinity, `KH`/`ae` | Usage GET has 5-second HTTP timeout, but native auth recovery/retry and initialization add work |
| 287207000-287217300, `uo`/`O_` | Native refresh is requested before transport; refresh errors can be logged while proceeding with cached bearer |
| 286945000-286953000, `Tea`/`XGn`/`ml` | Native owns refresh locking, rereads, persistence and coordination; wrapper must not duplicate this |

The snapshot timestamp is local fetch-completion metadata, not server
computation time. Its accepted one-hour seed bound is far too weak to authorize
startup on a cache read. The 60-second shortcut also means a new process is not
a force-refresh command. No documented force-fresh flag was found in the
inspected usage request schema, whose only option is `skip_behaviors`.

## Conservative freshness candidate, not implemented

The following can fail closed and is compatible with native-owned persistent
credentials. Its correctness remains conditional on the binding gates below.

1. Before protected work, capture the registered account/organization identity
   and the native cache timestamp. A historical cache may explain refusal but
   cannot authorize startup. Keep the model gate closed.
2. Request `get_usage(skip_behaviors=true)` and observe only the allowed local
   metadata/cache fields. Require a strictly newer native `fetchedAtMs` produced
   during this checking episode, with stable registered account/organization
   before and after, matching cache account UUID, and valid enabled windows.
   A successful control reply without that new record is insufficient.
3. If native returns its under-60-second cache, wait conservatively until that
   shortcut expires, then issue one bounded query. Do not delete/edit the cache,
   spoof time/scopes, or make a model call. Proposed research bounds are a
   120-second total startup check, at most two control queries, and at most
   45 seconds for one native operation. Wait no more than five seconds for
   asynchronous persistence after a reply; failure to observe it refuses.
   These are proposed bounds, not validated production latency/rate budgets.
4. During monitoring, only a newly observed timestamp resets the successful
   observation age. Unchanged utilization with a new timestamp is new data;
   unchanged timestamp with any number of replies is old data. Convert a
   validated wall-clock age into a monotonic deadline, detect clock anomalies,
   and never extend an earlier deadline from a cache reread, reset time,
   retry, or recovery failure. Missing windows remain unavailable.
5. A candidate query cadence must accommodate the 60-second native shortcut
   and actual service limits; 65 seconds is an offline candidate, not a
   measured safe live rate. Choose a maximum observation age separately,
   stop at its deadline, and bound transient recovery from the last new record.
   If strict freshness needs more frequent reliable snapshots, this path is
   insufficient. A paused session never resumes automatically.

Disk writes can lag and unrelated native processes can write the same account
cache. A newer same-account record may be useful evidence but is not proof that
this invocation's active request succeeded. Requiring strict request-specific
attribution needs a native origin/timestamp/correlation API or another reviewed
observation mechanism. The fixture sees TLS traffic; the product does not gain
that visibility merely by using an inference request proxy.

The strongest safe current outcome remains unavailable/refuse when a new
attributable observation cannot be established. No production timeout, startup
success guarantee or maximum overshoot follows from these local timings.

## Attribution, family and refresh gates

Preserve PA0's accepted fixed
`<secrets_dir>/native-claude-accounts/NAME/config/` location and native storage.
Ordinary selection must never invoke login. Native handles expiry and refresh;
the kit must not read/export refresh tokens, clone stores, clear locks, patch
scope metadata or implement a second refresh client. Only fixtures here create
fake OAuth records. The
[authentication documentation](https://code.claude.com/docs/en/authentication)
documents persistence through separate claude.ai config directories and native
reauthentication when recovery is required. This is not live refresh evidence.

The local UUID check is necessary but insufficient. `auth status` and
`oauthAccount` are local metadata; PA0/PA2 already demonstrated fabricated
credentials reporting logged in. Cache records omit organization identity;
the 401/403 fallback can keep old quota visible. Changes in native credentials,
organization, provider, account metadata or current family must invalidate the
monitor's binding and pause until revalidated. This research does not show a
race-free way to detect all changes without examining credential material.

Only explicit `five_hour` and `seven_day` windows cover the requested general
windows. `seven_day_opus`, `seven_day_sonnet`, `seven_day_oauth_apps`,
`model_scoped`, `limits` and overage rows must not silently replace them.
The native response schema permits nullable/missing fields and additional rows;
validate finite percentages in [0,100], reset semantics and enabled-window
presence independently. A model/family change requires its own applicability
proof. This matrix does not establish the production account's quota families.

Successful refresh/revocation recovery, concurrent same-account refresh,
in-flight organization changes, real server identity binding and macOS
Keychain behavior remain separate tests. Preserve the user's accepted shared
device-state boundary, but do not treat it as authorization to access that
state or run enrollment here.

## Smallest next permission question

Before requiring a web login, a separately authorized direct setup-token
`GET https://api.anthropic.com/api/oauth/usage` is a narrower remaining
permission hypothesis. Native's profile-scope rejection is local; the consumed
count experiment established no usable headers, not server rejection of this
distinct endpoint. This is an internal endpoint, not a documented setup-token
capability, and it has not been called with real credentials by this node.

Proposed future request: exactly one GET, no body, bearer from one explicitly
named setup-token file, `Accept: application/json`,
`anthropic-beta: oauth-2025-04-20`, `Cache-Control: no-cache` and
`Connection: close`. No scope/client impersonation, redirects, retries, refresh,
fallback or inference. Standard TLS verification; a 5-second socket and
10-second total child bound; response/header limits 64 KiB. Inherit no arbitrary
proxy/auth override; do not bypass required organizational routing. Use the
reviewed permission harness's credential-file checks and parent supervision.

Retain only status, UTC start/receive and monotonic duration, Date/Age/cache
headers, request ID, Retry-After, allowlisted error type, and validated
five-hour/seven-day utilization/reset values or explicit missing/malformed
states. Record only presence of other quota-family fields; no raw body, free
text, cookies, bearer, credential hash or account/profile payload. A 200 can
establish existence for that token and moment only, not general permission,
refresh cadence, server data age, account UUID binding or G0. Build/test against
loopback, review independently, then request new named-account authorization.
The earlier count permission cannot be reused. No extra harness is added here.

If that fails, the later native-login check should specify one new account
alias and its exact fixed PA0 config path for separate approval. The user
performs `CLAUDE_CONFIG_DIR=<approved-path> claude auth login` once; the kit
does not authenticate for them. Native credential/device/policy effects are
part of that approval. A later check uses the same directory and no login:

```text
claude -p --input-format stream-json --output-format stream-json --verbose --safe-mode --no-session-persistence
{"type":"control_request","request_id":"init","request":{"subtype":"initialize","hooks":{}}}
{"type":"control_request","request_id":"quota","request":{"subtype":"get_usage","skip_behaviors":true}}
```

This is a procedure outline, not a ready-to-run real-account harness. Sequence
the second request after successful initialization; send no user message and
close/reap after the quota reply. Before approval, implement bounded supervision
and the explicit metadata-only output allowlist, inspect effective policy/auth
conflicts, and identify all native network/refresh/shared-state side effects.
Unlike the single direct GET, native may perform metadata requests and refresh
retries. Do not promise a one-request native experiment. A cache-only answer is
inconclusive; bounded wait/requery needs explicit inclusion in its authorization.
Do not delete native state or perform a generation to force quota availability.

## Validation and remaining limits

The focused native matrix and retained-evidence checker pass. Source hashes
bind evidence to both probes and unchanged guards. Required all-files hook
results and frozen-tree identity are recorded in the coordinator handoff and
ignored validation logs. No production behavior changed, so tests/COVERAGE.md
is untouched under this node's exclusive write scope. The 19 native cases are
the research coverage matrix above. No Linux result establishes macOS, live
provider permission, safe service cadence or full option A control.
