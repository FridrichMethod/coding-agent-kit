# PT: setup-token-only startup quota feasibility

Research date: November 11, 2026. Base: `2df1ce5d4f95a42dacae3e2675897c26c258a4a5`.
Scope: evidence only; no runtime change or provider credential request.

## Result and binding requirement

No verified model-free startup source of fresh five-hour and weekly subscription
quota is available for setup-token-only accounts on the examined Claude Code.
The native `get_usage` path is locally blocked for inference-only tokens. This
does not prove that the provider rejects every possible non-inference request.
The smallest remaining endpoint hypothesis is whether a direct token-count
response carries subscription-window headers; neither its setup-token
authorization nor those headers is documented or demonstrated here.

Setup-token-only support is the preferred path. The latest user clarification
permits an initial web login as fallback if needed, with persistent credentials
and no repeated login during routine switching. It supersedes the hard
token-only constraint in force when this research was performed. Detailed
credential design and live experiments still need their gates; existing
setup-token behavior remains unchanged.
Full A, fresh startup observations before any model generation, per-invocation
control and manual continuation remain hard gates. Proxy research is now
authorized at a bounded architectural level; proxy implementation is not.

| Candidate | Finding | Consequence |
| --- | --- | --- |
| Native `get_usage`, inference-only env token | Unavailable, reproduced and explained by source | Cannot authorize protected startup |
| Native `/usage` | Uses the gated utilization machinery; command existence grants no scope | No independent token-only quota path established |
| Status-line subscription windows | Documented only after first API response | Cannot bootstrap startup; saved values are historical |
| Token count response body | Input size, not subscription utilization | Never a quota substitute |
| Direct count response subscription headers | Unknown authorization, presence, freshness and family | One bounded permission probe could test existence |
| API models/org/rate-limit metadata | Different quantities and/or credentials | No evidence of subscription-window coverage |
| Per-invocation request proxy | Potential dispatch boundary, incomplete native semantics | Cannot create a missing startup observation |

## Reproducible local evidence

Tested Linux Claude Code `2.1.322`, executable SHA-256
`bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478`.
This identifies the experiment; it is not a supported minimum or version pin.

Run [probe.py](quota-reserve-probes/claude_token/probe.py):

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_token/probe.py "$(command -v claude)"
```

The probe imports the unchanged PA0 runner, whose SHA-256 is recorded in
[observed.json](quota-reserve-probes/claude_token/observed.json). It creates and
prints an owned private temporary root with isolated HOME, config, profile,
cache, state, data, temp and empty working directories. The environment is an
allowlist containing one literal fabricated token. No credentials, user config,
history, plugins or scopes are copied. No login command runs.

All native children, including `--version`, run under strace fault injection
denying `socket`, `connect`, `sendto`, `sendmsg`, `sendmmsg` and `io_uring_setup`.
`close_fds` prevents inherited network sockets. The reused runner bounds each
child to 45 seconds, bounds output, and terminates only its new process group.
This is a cooperative Linux research fixture, not a hostile-code sandbox or a
new product dependency. There is no network-enabled fallback.

After initialize, the fixture asks `get_usage` three times: twice with
`skip_behaviors: true`, once with `false`. All three succeed as control requests
but report `rate_limits_available: false` and `rate_limits: null`. Each reports
zero API duration/cost and empty model usage. There are no user, assistant or
stream events. All 39 observed usage-process network syscall attempts are
injected failures; no inference is possible. This rules out repeat-query and
behavior-collection flags as fixes on this path. It does not exercise valid
provider authorization or populated-cache freshness.

Raw native files stay in the printed private root. Sanitized observations are
durable here; ignored `native.log` and `native-exit.log` record the local run.
Exact temporary paths belong in the coordinator's private handoff, not Git.

## Native source findings

These are static observations of the identified installed executable, not a
public ABI. Byte offsets locate embedded JavaScript without reading account
files. Symbols must be interpreted in their own embedded module: minified names
can repeat across modules.

| Location | Observed behavior |
| --- | --- |
| `NC`, `_g`, `tj`, near byte 286933130 | Environment token becomes an access-token record with null refresh token/expiry and default `user:inference` scope |
| `Ku`, byte 286953592; scope constant near 283183839 | Checks locally recorded scopes for `user:profile` |
| `Gor`, byte 308961839 vicinity | Combines subscriber predicate `At()` and `Ku()` before requesting plan utilization; otherwise null |
| `KH`/`ae`, near 292559975-292561500 | Selects `/api/oauth/usage`; returns without fetching unless `At()` and `Ku()` pass; actual request has a 5-second timeout |
| `u5t`/`U`, near 308960028 | May answer from a cached snapshot without asking the endpoint; may return seeded data on failure |
| `Jco`/`dDi`, near 292580000; constants near 292578853 | Persisted snapshot eligibility includes account UUID; fresh-cache shortcut uses 60 seconds, persisted seed eligibility up to one hour |
| `Wor`, byte 308959066 | Seed source is persisted data or response headers; internal source/fetch-time metadata exists |
| `Gor` return, near 308962905 | Public usage reply omits that origin/fetch-time metadata |
| Production `It`/`De`, near 292573676 | Maps `5h`/`7d` unified response headers to `five_hour`/`seven_day`; requires utilization and reset for complete windows |
| `qXe`/`_7o`, near 294309893-294311700 | Calls count endpoint and extracts input count; a gateway 501 path can fall back to Messages creation |
| `sVe`/`yXi`, near 287865113; `MU`, near 288833655 | Inference scope participates in OAuth selection; client receives selected auth token and optional API base URL |

The local scope gate is not a server grant check. Editing credential metadata
or setting scope overrides could change that branch without granting anything;
it is forbidden and was not attempted. The older fabricated `auth status`
success also proves only credential selection, not live token validity.

The count fallback is significant: `_7o` invokes `beta.messages.create` with
`max_tokens: 1` for the ordinary non-thinking case. Therefore a native command
that happens to count tokens is not a proved zero-generation permission probe.
No native count/context command was invoked here. Gateway fallback behavior
does not prove that direct first-party counting uses generation.

The header parser uses explicit subscription names:
`anthropic-ratelimit-unified-5h-utilization`, `-5h-reset`,
`-7d-utilization`, and `-7d-reset`. It also recognizes distinct overage and
model-scoped variants. Native display multiplies utilization fractions by 100.
These are source-observed headers, not a documented promise that count requests
return them. Ordinary API RPM/TPM headers and input/context counts have different
denominators and cannot be normalized into subscription quota.

The current launcher exports the selected setup token and records token-hash/
native-PID attribution for the status-line cache. The helper's `seen_at` means
local recording time. Unchanged input can be recorded again after five minutes;
that is not a new server observation. The token hash is not a provider account
UUID, and after changing the exec/supervisor arrangement the existing PID test
needs independent validation. None of these cache fields authorizes startup.

## Current authoritative documentation

Checked November 11, 2026:

- [Authentication](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)
  describes setup-token as subscription authentication limited to model
  requests. It documents no setup-token subscription-usage endpoint.
- [Commands](https://code.claude.com/docs/en/commands) documents `/usage` as an
  in-session command, including while a response is running. It does not
  establish different token permissions or cache freshness.
- [Status line](https://code.claude.com/docs/en/statusline) documents subscription
  rate-limit data after the first API response; windows can be independently
  absent and are dropped after reset. A missing window is not an empty window.
- [Count API](https://platform.claude.com/docs/en/api/messages/count_tokens)
  documents `POST /v1/messages/count_tokens` as counting without creating a
  message. [Token counting](https://platform.claude.com/docs/en/build-with-claude/token-counting)
  describes the response as input-token count and counting as free with its own
  request rate limits. Neither page promises subscription headers or
  setup-token authorization.
- [API rate limits](https://platform.claude.com/docs/en/api/rate-limits)
  documents organization/workspace request and token limits, including
  restrictive-limit selection. These are not the requested subscription
  five-hour/week windows. [Models](https://platform.claude.com/docs/en/models/overview)
  metadata describes model capabilities/context, not remaining subscription.
- [Environment variables](https://code.claude.com/docs/en/env-vars) documents
  API base URL routing and live application of some settings environment
  changes. A changed base host can disable features such as Remote Control.
  [Network configuration](https://code.claude.com/docs/en/network-config)
  documents HTTP(S) proxies, bypass variables and CA trust.

No documented non-inference operation was found that promises both required
subscription windows under setup-token authentication. This is a scoped search
result, not proof that an undocumented provider capability cannot exist.

## Minimal separate permission probe proposal, not authorized or run

The useful next question is whether the documented zero-generation count
endpoint returns independently sourced subscription headers for a setup token.
A successful count alone is a negative result for this task. The proposal needs
independent review, a finished dry-run harness and explicit user authorization
for one named account before any credential read or request. There is no such
credential-bearing harness in this change.

Proposed operation: exactly one HTTPS request to
`https://api.anthropic.com/v1/messages/count_tokens`, method `POST`, with this
fixed harmless body:

```json
{"model":"claude-sonnet-5-5","messages":[{"role":"user","content":"quota metadata probe"}]}
```

The model identifier is in the current official model list. A rejection does
not authorize selecting another model or retrying. Proposed headers are
`Authorization: Bearer <selected setup token>`, `Content-Type: application/json`,
`anthropic-version: 2023-06-01`, `anthropic-beta: oauth-2025-04-20`,
`Cache-Control: no-cache`, and `Connection: close`. Bearer/OAuth-beta use is
based on native authentication code; provider acceptance on this endpoint is
the unknown being tested. No fake client identity or fabricated scopes.

Read only the exact `<configured secrets_dir>/claude-NAME` path named in the
approval, once, with owned regular 0600/single-link/no-symlink checks. Resolve
that concrete path before asking approval; do not enumerate accounts, inspect
the default account or import a full OAuth store. Keep the token only in memory
and the TLS Authorization header; never argv, logs, output, traceback, hashes
or copied credential files. Use the fixed provider host with normal hostname/
certificate verification and no redirects or inherited proxy/custom CA/auth
environment. Do not silently bypass a required organizational network policy;
if direct access is disallowed, stop for a separately reviewed transport.

The proposed command shape is a future standalone standard-library probe with
an explicit `--account NAME --token-file EXACT_PATH --count-once` invocation.
It must be implemented and independently validated before execution, so this
paragraph is not a runnable authorization request. The request primitive must
hard-code the single host/path/method, reject redirects, disable retries and
fallback, bound headers/body to 64 KiB, use a 5-second socket timeout and a
10-second total process deadline including DNS. A parent deadline must terminate
the child on timeout. Tests against loopback must reject any `/v1/messages`
request and demonstrate that 401, 403, 429, 501, redirect, malformed response
and timeout produce no second request. Never call the native count helper.

Retain only HTTP status, request start/receive times (UTC and monotonic duration),
`Date`, `Age`, `Cache-Control`, `request-id`, `retry-after`, and the explicit
unified `5h`/`7d` utilization/reset/status headers. Record presence, not values,
of unrelated scope/model-family headers. From the body retain only whether
`input_tokens` is present and an allowlisted `error.type`; discard its count,
free-text errors and all other fields. Do not retain cookies, echoed auth or
account/profile data. Any workspace/account identifier needed later requires
a separately approved data list.

One response can establish existence only for that token, endpoint and moment.
Missing either window, authorization failure or malformed headers means no
usable observation. Even both windows with an HTTP Date do not alone establish
provider-side computation age, account UUID, intended quota family or safe
refresh cadence. Cache-Control is a request preference, not proof of freshness.
Success would justify a second reviewed freshness/attribution experiment, not
implementation or automatic protected launches. A direct `/api/oauth/usage`
permission test is another possible question, but is an internal endpoint and
is not included in this one-request proposal.

## Bounded per-invocation proxy assessment

A proxy can forward a selected setup token without a new login or refresh-token
store. Setup tokens have no native refresh credential in the inspected path;
the proxy must not invent refresh or replace expiration handling. Static client
construction makes base URL plus selected token a candidate, not an observed
end-to-end forwarding guarantee. The current launcher rejects inherited custom
base URLs, so any protected routing exception would require explicit design
and tests rather than weakening ordinary conflict checks.

An HTTPS CONNECT tunnel preserves TLS and native auth but cannot see individual
requests or subscription headers inside a reused encrypted connection. Closing
it interrupts transport, not necessarily provider computation, and opening it
can admit further requests. It is not a per-request gate. Terminating TLS adds
CA/key handling; a local base-URL reverse proxy can inspect HTTP but changes the
native endpoint classification and handles credentials/prompts. Neither design
has been adopted or implemented here.

For either inspectable proxy, all main, subagent, retry, compaction, suggestion,
background and resumed-session inference must route through its invocation's
gate. Proxy/bypass variables, mutable settings, provider/model changes,
WebSocket/Unix-socket transports and independently spawned clients need explicit
coverage. Inherited environment alone does not prove descendant coverage.
Native network errors/retries are not demonstrated persistent pause semantics.

The gate would have to deny model dispatch from startup until quota is fresh,
close before forwarding queued requests on a violation or watchdog expiry,
keep a persistent paused latch through resets, cancel its own active streams
where possible, and fail closed if its monitor dies. Closing a stream cannot
guarantee immediate cancellation upstream or undo consumption. A response
header arrives after a request has already been admitted; concurrent requests
can already be in flight. Header-only monitoring also goes stale while idle or
when other sessions spend quota. None of that solves startup freshness.

Holding requests indefinitely can hang the native UI or time out; rejecting
them can trigger retries. Reopening a gate can release queued/background work,
which violates manual continuation. HTTP request headers do not prove a
deliberate human resume action. Keeping the native UI usable, separating that
action from queued work, and defining ongoing tool-command behavior therefore
remain native-control gates even if routing succeeds. A proxy does not itself
provide full A.

## Completion and remaining decision

This research adds only the report, fabricated native probe and sanitized
observations. The focused probe passed; all-files hook results are recorded in
the coordinator handoff and ignored validation logs beside the fixture. No real
credentials, default-account files, login, model request, proxy runtime or
central specification were changed or accessed.

Do not begin implementation on this evidence. The smallest capability decision
is whether to prepare and separately authorize the exact one-account count
header probe above, or seek provider confirmation of a setup-token-compatible
fresh usage interface. If no such source exists, token-only protection with
the accepted startup guarantee is blocked. The user now permits a separately
reviewed persistent-login fallback, which still needs its own quota/control
proof; a first generation request cannot satisfy either path's startup check.
