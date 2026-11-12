# Claude PA0 authentication discovery and HAuth proposal

Current direction: setup-token-only support is preferred. The user permits
initial web login as fallback if needed, provided credentials persist per
account and routine switching requires no further login. This proposal remains
subject to concrete HAuth isolation/schema review and quota/control evidence.
Its storage findings authorize no real-account or shared device-state access.

Status: PA0 research complete; validation and independent review results are
recorded in the coordinator handoff.
This is a proposal, not implemented behavior. Candidate-schema review has been
requested; HAuth remains unresolved. PA1 (authenticated freshness and native
control) has not started. See the coordinator handoff for current decisions.
Research date: November 11, 2026. Base: `0a79911`.

## Decision to review

Retain existing setup-token accounts unchanged. Offer an explicit
`native-login` account mode for claude.ai subscriptions, with one private,
fixed `CLAUDE_CONFIG_DIR` per account and credentials entirely managed by
Claude Code. Do not copy credentials from the default account, convert a
setup-token, implement refresh, or promise quota protection on this evidence.

There are two unresolved implementation gates even if HAuth approves this
storage design: fresh, attributable quota without inference and full option A
control. Successful authentication cannot waive either gate. Linux storage and
macOS namespace behavior are partly static/documented evidence; no real login,
refresh, account migration, Keychain operation or authenticated quota request
was performed. No default account credential/configuration was read.

## Evidence and reproducible fixture

The native executable is Linux Claude Code `2.1.322`, SHA-256
`bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478`.
This is the tested version, not an established minimum version or a pin.

Run the [isolated probe](quota-reserve-probes/claude_auth_probe.py) with:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_auth_probe.py "$(command -v claude)"
```

This research command requires Linux and strace; neither becomes a product
runtime dependency. It takes no credential argument. It creates an owned
0700 temporary HOME, Claude config, Anthropic profile directory, XDG roots,
temporary directory and empty workspace. Its environment is an allowlist,
its stdin consists only of initialize/get_usage control requests, and
`--safe-mode --no-session-persistence` disables ordinary customization and
session persistence. Native auxiliary config files can still be written.
No settings, credentials, prompts or transcripts are copied from the caller.

Every native command, including version and auth status, runs under strace
fault injection denying `socket`, `connect`, `sendto`, `sendmsg`, `sendmmsg`
and `io_uring_setup`. Denying socket creation also covers ordinary writes to
new sockets; `close_fds` prevents inherited network descriptors. This is
stronger than a reject proxy. It is a bounded cooperative native fixture,
not a security sandbox for a malicious executable. Each child has a 45-second
total deadline; stdout uses selectors and bounded reads, stderr goes to a
private bounded file, and final cleanup signals only its newly created
process group, even if its leader has exited. The fixture retains its exact
printed root for inspection; cleanup is a separate canonical command.

[Observed sanitized result](quota-reserve-probes/claude_auth_observed.json):

| Observation | Result | Evidence class |
| --- | --- | --- |
| Stream initialize and get_usage | Both successful control responses | Reproduced on Linux |
| Rate limits | `rate_limits_available: false`, `rate_limits: null` | Reproduced unsupported result |
| Model work | No user/assistant/stream events, cost 0, API duration 0, model usage empty | Reproduced; network also denied |
| Native auth status with fabricated token | `loggedIn: true`, `authMethod: oauth_token` | Reproduced; not server authentication |
| Config/session root in status | Both point inside the private probe root | Reproduced on Linux |
| Denied syscall attempts | 8 for status, 36 for usage, 0 for version | Reproduced first retained run |
| Session duration at get_usage | 17.174 seconds | Observation, not a production timeout |

The unsupported result is never normalized to zero utilization or unlimited
allowance. The fake-token `loggedIn: true` result is a concrete counterexample
to treating auth status as credential validity or fresh identity proof.

## Native authentication and isolation findings

Evidence labels here distinguish documentation, static inspection of the
above binary, actual fixture behavior and unverified expectations.

- **Documented:** separate `CLAUDE_CONFIG_DIR` values isolate claude.ai login,
  settings and session history. Console sign-in without an API key uses an
  external profile store and is explicitly excluded from that isolation.
  Linux uses `.credentials.json` at mode 0600. macOS uses a directory-keyed
  Keychain entry, falling back to that file when Keychain writing fails.
  See [authentication](https://code.claude.com/docs/en/authentication).
- **Documented:** `auth login`, `auth logout` and JSON `auth status` are native
  commands. Status exposes config directory, auth method and account fields.
  `setup-token` prints a token without saving it; it is model-request-only.
  See [CLI reference](https://code.claude.com/docs/en/cli-reference) and the
  [long-lived token description](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token).
- **Static:** `SB()` selects secure storage from
  `CLAUDE_SECURESTORAGE_CONFIG_DIR` when present, otherwise the normal Claude
  config directory. Even an empty override has special semantics. Therefore
  setting `CLAUDE_CONFIG_DIR` alone is insufficient if inherited storage
  overrides are permitted. The proposed mode must reject such overrides,
  including those supplied through settings, rather than quietly accepting
  storage outside the account root.
- **Static:** Linux storage appends `.credentials.json`; its native write
  requests mode 384 decimal (0600). Native OAuth records include access and
  refresh tokens, expiry and granted scopes. The kit must not serialize that
  record, invent scopes or export a refresh token into its own environment.
- **Static, macOS runtime unverified:** `Yxe(suffix)` constructs
  `Claude Code` + native OAuth environment suffix + supplied suffix + an
  optional `-` plus the first eight hex characters of SHA-256 over the
  NFC-normalized configured path. The credential-store suffix is
  `-credentials`; the production explicit-directory candidate namespace is
  `Claude Code-credentials-<8-hex-path-hash>`. The account field is native
  `j3()` (sanitized USER or OS username). The no-explicit-directory case has
  no path suffix. This is a native implementation detail, not a kit naming
  API. The kit must use the same fixed canonical path and normal OS identity
  for every invocation and let native code select the item. Do not call
  `security` to extract, copy, rename or delete entries. A truncated hash is
  not collision-proof: validate isolation on macOS before claiming support.
- **Static:** native full-login scopes include `user:profile` and
  `user:inference`; environment/setup-token paths default to inference-only.
  The usage response gates plan availability on the profile-scope check.
  Profile-capable metadata does not establish a grant from the server.
- **Static:** `XGn`/`Tea` and refresh logic coordinate on the secure-storage
  directory, reread credentials after acquiring a native refresh lock and
  recognize tokens changed by another process. The inspected lock paths are
  `.oauth_refresh.lock` inside that directory and a legacy sibling `.lock`.
  The current lock's stale/update constants are 60/5 seconds. Those details
  explain why sharing one account directory is the candidate for concurrent
  refresh; the kit must never manage, clear or replicate these locks.
- **Static:** status reports stored email, organization ID/name and plan
  information. It does not expose an immutable account UUID in the examined
  output builder. Native global config holds `oauthAccount.accountUuid` and
  `organizationUuid`; these are local metadata, not live server proof.
- **Documented/static:** config isolation also separates user settings,
  projects/history and plugins; it does not isolate the project files or
  machine policy. The explicit-directory global config is
  `<config>/.claude.json`. Do not copy the default `.claude.json`, MCP config,
  history or plugins. Project and managed settings still apply. See
  [settings and precedence](https://code.claude.com/docs/en/settings).
- **Static shared device identity boundary:** native `bCr()` resolves
  `HOME/.claude/.device-keys.json` (normally `~/.claude/.device-keys.json`)
  independently of `CLAUDE_CONFIG_DIR`. Native authentication-file enumeration
  includes it. Device lookup uses this store; `Fct -> Ce -> loadOrCreate`
  can generate and persist a P-256 private key before server registration.
  Callers include device binding and bridge device registration. This file is
  credential-bearing device identity, distinct from the subscription OAuth
  credential in the account config directory. Native logout also has code to
  preserve legacy `coworkRemoteDevice` entries in the credential store.
  No independent device-key path override was found in these static functions
  or the [environment variable reference](https://code.claude.com/docs/en/env-vars).
  This absence is a research finding, not proof that no override exists.
- **Unverified and blocking real-HOME enrollment:** whether ordinary login,
  logout, usage or concurrent sessions read, write, migrate or remove that
  shared device identity. The temporary-HOME probe cannot establish this.
  OAuth credential separation alone is insufficient to promise complete
  account isolation. Before running enrollment with the real HOME, either
  prove the intended native operations leave shared device credentials
  untouched using static/fabricated evidence, or obtain a separately reviewed
  human decision accepting the precisely identified shared-state behavior.
  Approval of the account-local schema alone does not authorize access to
  this shared file. Do not read it, change HOME, or copy it to resolve the gate.
- **Unverified:** successful full-login storage and refresh under concurrent
  processes, macOS Keychain lock/fallback/recovery, server identity binding
  and all real-account permissions.

## Exact proposed opt-in and schema (not implemented)

Proposed first-argument enrollment command:

```sh
claude-as --enroll-native borrowed
```

It creates a pending private account directory and runs the configured native
executable as `claude auth login` with that account's `CLAUDE_CONFIG_DIR`.
The human performs browser/SSO authentication. No `--console`, refresh-token
input, custom OAuth client, scope override or token capture is involved.
Enrollment exits after native login and verification; it never starts a model
turn. Success requires first-party claude.ai subscription mode and explicit
confirmation of the displayed account/organization identity. Other login
modes remain unregistered. The wrapper must preserve organization restrictions
and refuse enrollment if it cannot preserve the managed-policy loading chain.

Exact proposed selection in the existing kit TOML:

```toml
[claude_auth.accounts.borrowed]
mode = "native-login"

[usage_reserve.claude_accounts.borrowed]
five_hour = 20
weekly = 30
```

`mode` accepts only `"setup-token"` or `"native-login"`; omission means
`"setup-token"`. There is no global implicit conversion and no mode fallback.
Existing setup-token enrollment remains `claude-as NAME`. Enrollment does not
rewrite the user's TOML: it prints the reviewed stanza for the user to add.
An enabled reserve on setup-token refuses before model work unless a future
separately proved native mechanism supports it. Reserves off preserve current
setup-token behavior exactly. Native enrollment and selection do not by
themselves enable reserves. Unknown table fields follow the kit's warning
contract; an invalid recognized `mode` is an error. Older kits ignore this
new table and cannot provide these guarantees; a new account alias with no
legacy token is recommended during rollout. Public interfaces require a minor
release (expected 0.8.0, to be recomputed at integration).

All paths derive centrally from existing `secrets_dir` through config.py.
The new root `native-claude-accounts` is outside both legacy `claude-NAME`
and `codex-NAME` namespaces. Thus valid setup-token names such as `native`
remain available and existing files never become account-directory blockers:

```text
<secrets_dir>/claude-NAME                    existing setup-token, unchanged
<secrets_dir>/native-claude-accounts/                 0700, owned real directory
<secrets_dir>/native-claude-accounts/NAME/            0700, owned real directory
<secrets_dir>/native-claude-accounts/NAME/account.json 0600, non-secret registration
<secrets_dir>/native-claude-accounts/NAME/config/     0700, fixed native config directory
  .credentials.json                        native-owned 0600 on Linux/fallback
  .claude.json                              native-owned account/config metadata
  settings.json, projects/, plugins/, ...   independent native configuration
```

Proposed `account.json` schema, illustrative identifiers only:

```json
{
  "schema_version": 1,
  "mode": "native-login",
  "state": "ready",
  "enrollment_id": "11111111-1111-4111-8111-111111111111",
  "config_directory": "<absolute-canonical-account-config-directory>",
  "identity": {
    "account_uuid": "22222222-2222-4222-8222-222222222222",
    "organization_uuid": "33333333-3333-4333-8333-333333333333"
  }
}
```

The initial file has state `"pending"` and `identity: null`; ready is written
atomically only after native login and confirmed metadata association.
`enrollment_id` is a random UUID, never a hash of a token. No tokens, expiry,
scopes, email or duplicated OAuth objects enter this file. A canonical path
mismatch refuses selection; moving a registered directory requires recovery.
Reading only the two native identity fields from account-local `.claude.json`
is the proposed metadata access; never log the whole file. PA1 must establish
how to verify that binding against the credential and native session over time.

Create directories under umask 077 without following account-root symlinks;
validate ownership, private modes and non-writable parents before native
execution. Registration and Linux credential files must be regular, owned,
0600 and single-linked. Do not chmod or overwrite preexisting files to make
checks pass. Native may create additional private state; inspect containment
and mode behavior in later platform tests. A platform ACL that grants access
beyond the POSIX bits is an additional portability check, not a proven result.

`CLAUDE_CONFIG_DIR` is set to the recorded fixed directory. The candidate
preserves the user's real HOME for ordinary native tool behavior, conditional
on resolving the explicit shared-device-identity gate above. Real-HOME
enrollment is blocked until that gate closes; silently substituting an
isolated HOME would change tool behavior and is not part of this proposal.
Reject inherited mismatched
config, secure-storage and project/storage-root overrides; reject auth/provider,
profile/federation, custom endpoint/client, hosted-auth and credential-FD
conflicts from environment and effective settings. Existing preflight provides
a starting point but is not a complete audit of this newer native surface.
In particular `CLAUDE_SECURESTORAGE_CONFIG_DIR` is absent from its current
conflict set. Do not strip managed restrictions or replace policy with an empty
settings source in a production enrollment. Native Keychain uses normal USER;
the fixture's invented USER is only for the credential-free Linux test.

## Concurrency, compatibility and recovery

Multiple normal sessions for the same registered account use the same config
directory. Native owns credential storage, rotation and refresh coordination.
The wrapper takes no lifetime account lock and copies no credentials per run.
Quota reserves remain invocation-specific; no session may stop unrelated
sessions sharing the account. Any failure to verify credential/identity
continuity pauses a protected invocation; a recovered login never auto-resumes.
Concurrent refresh reliability remains unverified despite native lock code.

Enrollment atomically creates a new NAME directory and refuses one that
already exists, including pending state. Thus two enrollments cannot replace
one another. Reauthentication/logout/removal require the human to close all
sessions for that native account first, including direct native invocations;
the kit cannot reliably establish that merely by scanning process names.
This is an operational precondition, not a demonstrated race-free exclusion
protocol. An identity switch from a concurrently running native session remains
a PA1 gate: local registration alone cannot prevent it or detect it promptly.

For compatibility, legacy files remain the only source for setup-token mode.
A native-login selection never silently falls back to one. An alias may retain
both formats for rollback, with the TOML mode choosing explicitly. Account
listing must merge registered native names and legacy names without duplicates;
existing --info/--labels formats remain unchanged, and metadata-only queries
read registration rather than tokens or launching Claude. Native account
metadata and historical usage must be invalidated on binding changes.

The present status line binds its cache to a setup-token hash and an exec-kept
native PID. Full login has neither a stable access token nor necessarily that
process shape. Do not synthesize a token hash from account UUID or reuse a stale
legacy usage file to claim attribution. Native quota monitoring and any new
status-line binding require a separately reviewed adapter; disable unsupported
cache recording rather than manufacture identity. Settings are separate, so a
new native account needs explicit native configuration of its status line and
other preferences; enrollment must not import arbitrary default hooks/plugins.

Migration is additive: enroll a fresh alias, verify it, then explicitly select
native-login and only later opt into reserves if G0 passes. Keep the old token
and its sessions untouched. Same-name conversion is allowed only as an explicit
later choice after the user closes relevant sessions; it is never automatic.
Rollback selects setup-token and disables any enabled reserves for that launch;
it does not delete the native login or revoke the old token.

A failed login leaves pending state for inspection, never a partially ready
account. Recovery uses native auth login/logout only in the printed exact
account directory, after closing its sessions. The user explicitly authorizes
native credential mutation. The kit neither removes native refresh locks nor
manually edits `.credentials.json`. On macOS, recover Keychain availability
using native login after unlocking it; never move the directory to repair a
path-keyed credential. If relocation is necessary, enroll a new directory and
sign in again; preserve the old directory until the user authorizes retirement.
Do not promise that directory deletion removes a Keychain item.

## Remaining gates and HAuth review payload

Static `Gor` accepts both `ok` and `seeded` utilization. `Wor` can seed from
persisted data or headers and tracks origin/age internally; the public usage
reply omits those fields. A successful RPC with unchanged cached data cannot
refresh the observation deadline. The fixture deliberately exercises only an
unsupported path, so no fresh quota or refresh timing guarantee follows.

HAuth reviews this exact native-login opt-in, private paths, metadata schema,
normal same-account concurrency, native refresh ownership, independent settings
and additive migration/recovery. It must also review the distinction between
account-local OAuth credentials and the shared real-HOME device-key store.
Proposed choice: approve the schema as a candidate while keeping real-HOME
enrollment blocked pending device-isolation evidence or a separately reviewed
shared-state decision, or request changes. This is not permission to access
`~/.claude/.device-keys.json`. PA1/G0 gates remain unresolved. Independent review
must precede that human question. No approval is inferred from research scope.

A subsequent real-account check requires a separate request naming the selected
account, native command/control sequence and data accessed: that account's
native credentials/refresh, local identity metadata, profile and subscription
quota. It must separately inventory any potential access to the real-HOME
`~/.claude/.device-keys.json`, legacy device-key migration and server device
registration, including possible effects on concurrent/default sessions.
Without resolved evidence or explicit shared-state authorization, real-HOME
enrollment and that experiment remain blocked. Login is performed by the user.
Only initialize/get_usage with
`skip_behaviors: true` would be sent; no user prompt, inference or transcript
scan. Native network behavior may include refresh and policy/feature requests,
so authorization must not imply that only one usage endpoint is contacted.
Exact command, version and containment are to be reviewed before execution.

No full option A session attachment, descendant stop, running-tool behavior,
manual-origin proof or monitor-loss gate has been demonstrated by PA0. Stop
rather than substitute repeated interrupt, process exit or a different UI.
macOS execution and real-account permission checks remain outstanding.

## Validation checkpoint

- Native fixture passed twice with the same unsupported/zero-inference result;
  the second run also verified every completed traced syscall was denied.
- Python AST parsing and retained JSON evidence assertions passed.
- Staged `git diff --check` passed. Only the report, isolated fixture and its
  sanitized observed JSON are in scope; there are no runtime/shared-file edits.
- `pre-commit run --all-files` passed all hooks on the first review snapshot,
  including native-wrapper, enrollment, configuration, metadata, contract and
  both installation suites. The exact revised snapshot also passed all hooks
  and independent spec-compliance and quality/security re-review, and was
  committed as `2df1ce5`. Final logs and decisions are in the coordinator
  handoff. HAuth and the shared-state authorization remain unresolved.
- Independent review round 1 identified a legacy-name collision and an
  insufficiently explicit shared-device-identity gate. This revision uses the
  collision-free root and names the exact shared credential path, its role,
  unresolved effects and blocking authorization boundary. No runtime or real
  account action was added.
