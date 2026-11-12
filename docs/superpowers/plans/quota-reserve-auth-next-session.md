# PN: Claude auth and quota handoff for the implementation session

Planning checkpoint, November 12, 2026; base `f0158df`. No runtime change,
real credential read, login, device-store access or authenticated request was
performed by PN. The future Claude session may adjust internal APIs, types,
test structure and DAG partitioning from evidence. The accepted account,
security, full-A, freshness and manual-continuation invariants remain binding.

## Decision and evidence boundary

Prefer setup-token mode; preserve its existing behavior with reserves off.
The coordinator supplied this sanitized live result: the separately approved
single setup-token usage GET returned HTTP 403 `permission_error`, with both
five-hour and seven-day windows missing. Its permission is consumed. The earlier
count request returned 200 without quota windows. Native setup-token usage was
also unavailable in PA0. These tested sources cannot authorize protection;
one 403 does not establish universal impossibility for every setup token or
interface. No new endpoint search, retry, refresh or generation is proposed.

The fallback is one initial user-performed native web login per account, then
persistent reuse on ordinary switches. It remains conditional on a separately
reviewed live permission node and usable attributable quota. Login success
alone does not pass G0. Revocation or failed native refresh may need explicit
recovery; switching must not itself invoke login/logout.

Evidence anchors: [PA0](quota-reserve-claude-auth-discovery.md),
[PA2](quota-reserve-claude-persistence-research.md),
[PA1F](quota-reserve-claude-freshness-research.md),
[PR](quota-reserve-routing-audit.md), and
[PTU](quota-reserve-claude-usage-permission.md). Their native findings concern
Claude Code 2.1.322, not a supported minimum or all future versions. The new
403 summary supersedes PTU's pending-experiment status, not its limitations.

## HAuth: accepted boundary versus new interface choice

The durable HAuth decision accepts explicit `--enroll-native NAME`,
`claude_auth.accounts.NAME.mode = "native-login"`, and the fixed private root
`<secrets_dir>/native-claude-accounts/NAME/config/`. Native may access, migrate
and update its account-indexed shared `HOME/.claude/.device-keys.json`; this
accepted exposure includes the default account's shared device state. It is
not permission for the kit to inspect/copy that file, reuse default OAuth
credentials, migrate accounts automatically or run an unlisted real login.

PA0 specifically forbids serializing/copying the native OAuth record, inventing
scopes, exporting a refresh token, implementing refresh, managing native locks,
and using `security` to extract/copy/rename/delete Keychain entries. Its proposed
metadata read is limited to account/organization UUIDs in account-local
`.claude.json`. Native owns credentials, refresh and recovery. PA1F's later
"must not read/export refresh tokens" wording must not be used to silently
claim that HAuth approved an access-token extraction mechanism, or to invent
a blanket prohibition on the already-reviewed request gate seeing a bearer.
Memory-only access-bearer acquisition is a distinct G0 design/permission choice;
no existing approval supplies it. Any change to the explicit prohibitions must
be presented as a change, not hidden in implementation.

## Minimal future human nodes

`N0 -> HLogin -> N1 -> G0-auth`; the credential-interface choice below can be
reviewed alongside N0. G0-auth feeds the existing routing/ownership/full-A G0,
not a separate authorization to ship a partial protected mode.

N0 is offline preparation and independent review of one bounded harness, not
a new broad matrix. Resolve one chosen alias and its exact canonical directory
without enumerating accounts. Preserve HAuth's pending `account.json` with
`identity: null`, random enrollment ID and recorded config directory; only
after native login and human identity confirmation may registration become
ready atomically. Ready records enrollment, not quota-protection capability.
An existing/pending directory is a refusal, never an overwrite or automatic
recovery. No directory outside the workspace is created by this outline.

Validate owned real 0700 account/config directories, private non-writable
parents, and owned regular single-link 0600 registration/credential files where
native uses files. Do not follow symlinks or chmod existing state into compliance.
Keep the canonical path and normal OS identity fixed for native Keychain
namespacing; validate macOS isolation separately. Reject conflicting effective
auth/provider, secure-storage, endpoint/profile and hosted-credential selectors.
Preserve managed login/organization restrictions and policy loading; refuse if
they cannot coexist with the chosen subscription mode. Do not erase policy,
copy default settings or substitute an isolated HOME to obtain apparent success.

HLogin must separately name the alias, resolved paths, native commands, allowed
metadata and shared-state/network effects. The user performs authentication:

```text
CLAUDE_CONFIG_DIR=<secrets_dir>/native-claude-accounts/NAME/config/ claude auth login
CLAUDE_CONFIG_DIR=<same-fixed-directory> claude auth status
```

These are templates, not executable authorization. Confirm the displayed
claude.ai account/organization with the user and record only the approved UUID
association. `auth status` can report logged in for fabricated credentials;
neither it nor local UUIDs prove server identity or quota permission. Failed or
cancelled enrollment remains pending. Native login/refresh can write OAuth,
metadata and shared device state and contact auth/profile/policy services;
successful bounded inference-free validation is not a promise of one HTTP call.

N1 reuses that exact directory, without login. The reviewed harness must first
bound output/time, close inherited descriptors, supervise/reap owned children,
define its sanitized metadata/window allowlist and account for effective policy,
native refresh/retry and auxiliary network effects. No token, raw auth record,
whole metadata file or raw response may enter retained logs. A new real-account
permission must cover this concrete harness; the following outline is not ready
or authorized merely because the command sequence is known:

```text
claude -p --input-format stream-json --output-format stream-json --verbose --safe-mode --no-session-persistence
{"type":"control_request","request_id":"init","request":{"subtype":"initialize","hooks":{}}}
{"type":"control_request","request_id":"quota","request":{"subtype":"get_usage","skip_behaviors":true}}
```

Set the fixed `CLAUDE_CONFIG_DIR`; send quota only after successful initialize.
Send no user message/model request; close and reap after the bounded observation.
Do not clear caches or force generation. Review safe-mode's effective behavior
on the actual version; it does not excuse removing mandatory policy. If a cache
shortcut prevents new data, an explicit bounded wait/requery must be part of
the approved procedure, not an implicit retry. PA1F's 120-second total, two
queries, 45-second operation and five-second persistence wait are candidates,
not frozen production timings or approved live limits.

N1 acceptance is finite: fresh enabled-window data or explicit refusal; repeat
selection of the saved directory without login; and reuse after selecting a
second separately authorized account if cross-account live switching is needed.
The fake PA2 A/B/A test already supplies local persistence evidence; no second
real account is implicitly authorized. Linux results do not validate macOS,
successful live refresh or concurrent login/logout.

## Freshness and actual-bearer gap

Confirmed PA1F findings: native can return identical `get_usage` objects after
a successful GET, a cache hit or a failed GET. The under-60-second shortcut can
avoid a network fetch even in a new process. Matching-cache `fetchedAtMs` can
advance after success, but is local completion metadata, not server data age;
the cache omits organization and credential identity. Another process can write
it. A newer timestamp with stable before/after UUIDs is useful freshness evidence
conditional on binding, not proof of which bearer produced the observation.

Reject startup on a cache-only answer, missing/malformed enabled windows,
unsupported family, identity ambiguity or clock anomaly. During monitoring,
only a newly attributable observation can extend a monotonic deadline; cache
rereads, reset times, retries and 401/403 fallback cannot renew it. Separate
general `five_hour`/`seven_day` windows from model-specific or overage rows.
Validate finite percentages, reset semantics and the admitted model/family set.
Expired freshness pauses; recovery never resumes automatically.

PR's concrete gate remedy is necessary but partial: under the admission lock,
compare the actual outgoing bearer and routing/account/family selectors against
the accepted lease before upstream bytes. Rotation or mismatch closes admission
and registered streams until revalidation. Never substitute an old bearer for
a new one. This prevents a B request using A's lease, but cannot identify an
opaque bearer or establish that native quota was fetched with it. Polling local
metadata faster cannot exclude A -> B -> A or refresh races. Native refresh
ownership remains unchanged; same-account concurrent sessions remain allowed.

## G0 credential-interface choices and refusal

The current [authentication docs](https://code.claude.com/docs/en/authentication)
and [CLI reference](https://code.claude.com/docs/en/cli-reference), checked on
November 12, document persistent config directories, status and login commands.
They do not document a native access-token export or atomic quota-to-bearer
correlation interface. PA1F's inspected usage reply has neither. This bounded
finding is not a claim that no future native interface could exist.

1. Prefer a supported native correlated observation/credential-handle interface
   if one becomes available. It must associate a fresh model-free observation
   with the credential epoch/account/organization/family and bind dispatch to
   that epoch atomically. Cost: native capability dependency; currently missing.
   Merely wrapping `auth status` and `get_usage` does not implement it.
2. Review a concrete memory-only access-bearer bootstrap plus a model-free usage
   fetch with that exact bearer, then exact-bearer admission. No such bootstrap
   is established. A Linux account-local credential-file reader would expand
   the accepted metadata-only surface and encounter a record containing refresh
   material; it requires an explicit revised boundary, minimization and review,
   even if only accessToken is retained. macOS Keychain extraction is prohibited
   under current HAuth; the coordinator's explicit file/Keychain access-bearer
   research-boundary question is pending. Forcing file fallback is not approved. Without
   a compatible supported acquisition route, this candidate cannot establish
   cross-platform support. A platform scope change needs its own decision.
   Initial model-free acquisition cannot be replaced by sending a model request
   to discover its bearer. A later native rotation must obtain fresh same-bearer
   quota before admission; the wrapper never refreshes or rewrites native auth.

For either choice, provider semantics must connect the observed bearer to the
selected subscription identity/family; bearer equality alone is insufficient.
The existing usage endpoint and local metadata do not yet establish that fact.
Do not add speculative identity endpoints or assume a UUID authenticates a
token. A successful full-login quota experiment proves permission/availability
only to its measured extent, not this complete interface.

Finite acceptance: attributable initial quota before inference; then one fake
pending-admission sequence with A quota, native refresh/account change to B,
stale A cache and fresh B quota. No B bytes may use A's lease; uncorrelated data
must stay refused/paused. Exercise concurrent same-account rotation without
disabling native refresh, plus explicit recovery with fresh manual continuation.
No universal hostile-process proof is required. Unproved correlation means
protected Claude remains blocked at G0; setup-token mode with reserves off stays
usable. Do not silently weaken full A or declare a partial-provider release.

Remaining human decisions are the concrete credential interface and its costs,
the separately named-account HLogin/N1 permissions, and any explicit scope change
if no acceptable interface exists. Optional platform containment dependencies
are accepted in principle; their concrete ownership design is separate, with
no implied installation or system-firewall authorization in this auth plan.
Implementation may refine tests/types/node boundaries; it cannot turn cache
success, mocked credentials or the consumed 403 experiment into a G0 pass.
