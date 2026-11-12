# PR: native routing and ownership audit

Status: **finite audit complete; a narrower routing candidate is supported,
but complete protected-mode feasibility and G0 remain unproved**. This is not
a proof that an invocation-local gate is impossible. It identifies concrete
configuration and identity checks still needed, without reopening accepted
PC3/PC4/PC5 and PA1C default-route, loss or trusted-terminal evidence.

Research date: November 12, 2026. Base: `098ad2d`. Only this report and the
new `quota-reserve-probes/routing_audit/` subtree are owned by PR. No launcher,
credential storage, refresh implementation, public UX or existing fixture was
changed. The two new native cases use fake persistent-login records and local
servers, never real accounts. No hidden usage/login/auth-status command runs.

## Evidence and reproducibility

The installed Linux executables still match the earlier research:

| Native | SHA-256 |
| --- | --- |
| Codex `0.167.2` | `a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b` |
| Claude Code `2.1.322` | `bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478` |

[sources.json](quota-reserve-probes/routing_audit/sources.json) records the
20 inspected public Codex file hashes and seven bounded Claude executable
byte ranges. Codex source is the already resolved official release commit
`ce21072c90d32c40af7a55b60b2fb35c84a43a6a`; its downloaded archive hashes to
`11e3321cd1a158dcfd920f8d24b07a08e896b9eaa7f4800f3042227b56342170`.
The source was inspected, not rebuilt, and is not a binary reproducibility
attestation. Claude findings are static inspection of the installed executable;
no proprietary source dump is committed. Hashes identify evidence, not a
proposed exact-version or upper-version product pin.

Labels below distinguish **N** (native observation), **S** (source fact),
**I** (inference conditional on stated configuration), and **U** (unproved).
An I route is not silently promoted to an N full-A guarantee. In particular,
the independent syscall guard is test containment, not the proposed product
route enforcement. No native default/delegation/loss matrix was repeated.

## New Claude reload observation

[reload-evidence.json](quota-reserve-probes/routing_audit/reload-evidence.json)
contains one controlled contrast with two native TUI processes. Each starts
with its own private HOME, config, workspace and fabricated full-login record.
Both receive `ANTHROPIC_BASE_URL` pointing at a loopback gate-shaped endpoint.
After the first native turn completes, the harness edits that account's user
`settings.json` to point at a distinct alternate loopback listener. Four seconds
later it types a second prompt into the same TUI. This simulates the supported
user-settings edit/reload mechanism; it is not arbitrary process injection.

| Launch | First turn | Second turn after user-settings edit |
| --- | --- | --- |
| Environment URL only | gate | **alternate** |
| Environment URL plus inline `--settings` containing the same `env` pin | gate | **gate** |

Both model requests carry the fake stored OAuth bearer. The probe asserts
credential bytes remain unchanged. Each native UI stays alive through reload,
then `/exit` returns zero and restores termios. The servers return finite fake
Messages/SSE replies; no tools execute. This is evidence for endpoint selection,
not a quota check, production forwarding relay or proof of server entitlement.

The installed environment code explains the contrast: at byte 288474619,
`applySafeConfigEnvironmentVariables` applies global, user, flag and policy
environment layers with assignment to `process.env`; the flag layer follows
user settings. The ordinary local CLI does not have the desktop/self-hosted
launch-environment exemption. At 288833630 the first-party `MU` client builder
reads the resulting `ANTHROPIC_BASE_URL`. These are bounded implementation
anchors, not stable symbol APIs. The documented
[settings environment precedence](https://code.claude.com/docs/en/settings-reference#env)
and [live settings reload](https://code.claude.com/docs/en/settings#when-edits-take-effect)
agree with the observation.

Thus a product candidate must pin the URL in the invocation's inline settings,
not only in its initial environment. This test does not show that an arbitrary
administrator policy can be overridden, nor require protection from an
administrator deliberately replacing trusted machine policy. A conflicting
policy found before launch must refuse. Normal native model/account/session
operations and alternative-provider selectors still require the checks below.

## Codex route inventory

All source paths in this section are relative to `codex-rs/` at the recorded
commit. Links identify representative anchors; the manifest binds the files.

| Owned work or transition | Evidence | Candidate disposition and remaining boundary |
| --- | --- | --- |
| Ordinary turns and HTTP retries | N: PC2/PC3/PC5. S: `core/src/client.rs` constructs one provider-backed ModelClient and checks `supports_websockets` before Responses WebSockets. | Force a uniquely named custom provider with `requires_openai_auth=true`, explicit loopback URL and `supports_websockets=false`. Gate every model admission and registered stream. |
| Native title | N: PC3/PC5. S: `tui/src/app/thread_title.rs:64` supplies the current provider ID to its temporary structured thread; custom providers use the current model. | Include hidden title work in the invocation gate, even while another thread is visible. |
| Manual/local, foreground remote and speculative/background compaction | S: `core/src/compact.rs:297`, `compact_remote_v2.rs:285` and `compact_remote_v2_attempt.rs:144` use the session ModelClient; background compaction uses its Responses HTTP executor. | I: same fixed provider route; model fallback does not by itself select another provider. One focused compaction admission/closure capture remains, not an entirely new routing family. |
| Child/grandchild, custom roles and model overrides, v1/v2 | N for default descendants only. S: `agent/child_config.rs` copies the turn provider into shared spawn/resume config. `agent/role.rs` applies an explicit override struct without provider/profile keys; v1/v2 share this preparation. | I: role model changes retain the parent route. Keep a gate-side model/family allowlist; provider inheritance does not prove quota applicability for every model. One role/model override case can exercise this source extension. |
| Goals, wakeups and background continuation | S: `ext/goal/src/runtime.rs:436` uses the owned thread's `start_turn_if_idle`; ordinary wakeups reenter native turn dispatch. | I: same provider for that thread. They cannot grant the external lease. Existing old-queue rejection remains accepted. Resuming pre-existing background work must still preserve invocation ownership. |
| Memory summarization and automatic reviewers | S: `client.rs:867` sends memory summaries through the configured provider to a separate `/memories/trace_summarize` path. Reviewer configuration changes model/reasoning; a complete reviewer/Decisions route census was not proved. | Disable memory generation and unsupported auto-review modes before launch, or include their exact endpoints/models in admission. Rejecting an unknown path at the gate is safe only after its route is proved. Do not turn off required machine approval policy. |
| Image input and image generation/edit | S: ordinary image input is carried in model requests. `ext/image-generation/src/extension.rs:39` captures the thread provider; `backend.rs:80` creates its Images client from that provider and auth. | I: standalone image work reaches that provider's secondary endpoint. For text-only protection, force the image feature off and reject Images paths at the gate. Multipart image forwarding is unnecessary for that restriction; feature/config transition coverage remains. |
| Realtime/voice | S: `features/src/lib.rs:2233` marks `realtime_conversation` Removed. `tui/src/chatwidget/session_flow.rs:109` exposes voice by runtime-package availability. `core/src/realtime_conversation.rs:1369` separately resolves WebSocket and WebRTC call URLs. | `features.realtime_conversation=false` is not a proven disable mechanism. A concrete candidate forces both experimental realtime URL fields to the local denying gate. Its actual native call routing remains U; lack of audio hardware must not count as acceptance. |
| New/resumed or switched local threads | N: PC2 loaded resume and different-provider mismatch. S: `app-server/src/config_manager.rs:434,546` checks selected/saved provider IDs. Native TUI resume has override, restore and preserve-existing modes. | I: launcher-forced provider plus native ID checks helps local owned threads. Gate registration must include adopted threads and their still-running descendants; reject remote/cloud/daemon attachment before protected startup until ownership is proved. |
| `/model`, profiles, provider definitions | S: TUI start/resume projects a bounded list of SessionFlags, plus model/provider selection. It does not project provider URL definitions. | A model change need not change route; enforce model/family at the gate. Force the provider definition in the owned backend and frontend launch. A hostile extra RPC client's capabilities are not an ordinary-TUI bypass. See distinction below. |
| Account/login/organization change and refresh | S: auth is selected during request setup and includes generation/owner checks; native handles recovery. N: earlier fake 401 triggers native refresh attempts only. | Old quota must not authorize a different outgoing credential/account tuple. Native generation handling is not the wrapper's quota binding. Successful native refresh and same-token quota association remain U. |
| Hooks, plugins, MCP, shell-spawned native clients, external jobs | Ordinary shell commands can start another client with independently resolved config; arbitrary plugins/MCP can send model traffic outside the provider. | A candidate must reject unsupported automatic integrations before startup, and either establish delegated-client inheritance or explicitly restrict those tool capabilities. An unrelated independently launched user job remains outside ownership. No inference from a process name, no account-wide killing. |

The role conclusion follows the actual
[bounded override implementation](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/core/src/agent/role.rs)
and [shared child configuration](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/core/src/agent/child_config.rs),
not an assumption that descendants always inherit all parent settings.
The [gateway guide](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway)
also explicitly separates model-provider traffic from voice and other services.

### The RPC override distinction

`ConfigManager::load_with_validation` appends request overrides after its CLI
overrides. Provider-ID equality alone therefore does not establish that every
possible RPC caller preserves the provider definition. But the actual TUI's
`config_request_overrides_from_config`, at
[app_server_session.rs:2125](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/tui/src/app_server_session.rs#L2125),
does not forward `model_providers` or `base_url`. Its separate provider-ID
field is covered by the native selection checks. This audit found no ordinary
TUI operation that supplies an arbitrary replacement provider definition.
The RPC source finding is a conditional API boundary, **not a demonstrated
protected-TUI bypass** and not a reason to invent a hostile same-UID requirement.

Force route flags on both native processes. Then a focused `/model`, local
resume/fork and settings-reload case should check whether a legitimate TUI
transition actually changes the effective destination. Only a failure of that
candidate, or accepting additional native clients as owned work, would justify
an RPC admission filter as a new design decision. Do not build one preemptively.
Native system requirements can enforce provider definitions more strongly, but
the inspected public launch path did not expose per-invocation requirements
injection; the `system_requirements_path` overrides found in CLI source were
test setup. Editing system policy is outside this design.

## Claude route inventory

The source facts below refer to the pinned executable ranges, with public
documentation supplying supported-configuration context rather than a claim
that its latest text proves this installed build.

| Owned work or transition | Evidence | Candidate disposition and remaining boundary |
| --- | --- | --- |
| Ordinary Messages/SSE, retries, default nested agents | N: PA1C, plus the two reload turns here. S: the `MU` builder constructs the selected provider client for calls. | Pin `ANTHROPIC_BASE_URL` in inline settings and sanitize alternative provider/auth selectors. All admitted Messages paths share the lease. |
| Side queries, titles, compaction and fallback | S: the installed build has streaming and non-streaming Messages calls, side-query dispatch, and client construction through `MU`; `source=quota_check` and some count helpers also make Messages inference. | Gate both streaming and non-streaming Messages. Never use a helper just because its name says quota/count: first inference is forbidden at startup. Full caller reachability remains U; one native compaction/side-query case is the finite extension. |
| Custom subagent models/profiles | N: default PA1C nested graph only. S: provider selection `ve/Fd` is separate from a model name and supports multiple cloud backends. | A fixed first-party selector gives a route candidate for different Claude models, not proof for arbitrary cloud/profile combinations. Reject explicit alternate-provider profiles before launch; test one custom child model plus a forbidden alternate selector. |
| `/model`, `/resume`, `/cd`, user config reload | N here: live user `env` reload overrides the initial environment, but not the inline URL pin. Public settings describe model/session changes and reloading. | The tested pin closes this precise user-file route change. Include provider switches, fallback models, moved project settings and resume in the finite acceptance case; gate a model/family change before forwarding. |
| Native background agents, cron/goals, agent-view/background service | N: in-process default descendants in PA1C. S: background-task and cron switches exist, but they are not a proof of all detached jobs being disabled. | Reject detached service/agent-view/remote-session launch modes before startup. For owned in-process wakeups, ordinary Messages remains the candidate. Do not let a daemon adopt protected work or survive its gate lifetime without a separate ownership proof. |
| Native process relaunch or spawned native client | Public `CLAUDE_CODE_PROCESS_WRAPPER` covers native self-launches, agent-view sessions and a background service; normal PATH wrapping does not. | This is a real possible integration boundary, not coverage supplied by the existing shell launcher. Using that hook needs a concrete per-invocation ownership design; it does not automatically cover arbitrary shell/model clients. |
| Voice and image/secondary services | S: effective `voice.enabled` is checked; `/voice` writes user settings. Public docs call it dictation requiring claude.ai login. No complete secondary-service route census was proved. | Inline `voice.enabled=false` is a source-supported exclusion candidate under the tested precedence, pending a native `/voice` case. Do not count lack of microphone/account eligibility as a forced disable. Plugins/MCP image work is not made safe by the Messages URL. |
| Login/logout/account/organization switch | S: `MU` rereads auth and can rebuild after token changes; native has session-owner holds. PA1F shows the usage cache lacks organization identity. | Bind admission to the actual forwarding credential and quota identity, invalidate on change, and require revalidation. Local `auth status`, cache UUID and a passing before/after file check alone do not close the race. |
| Auto-review/classifier/agent advisor and other native endpoints | S: auxiliary inference paths and alternative provider clients exist; the finite audit is not an exhaustive closed-source call graph. | Disable unsupported optional modes through effective native settings or refuse before launch. Mandatory policy stays in force. A whole-client routing guarantee cannot be inferred from two default Messages captures. |

The [corporate-launcher documentation](https://code.claude.com/docs/en/corporate-launcher)
is useful for native self-spawns but does not itself supply lifetime ownership
or quota binding. It is not permission to start a hidden background command.
[Claude's settings reference](https://code.claude.com/docs/en/settings-reference#voice)
supports the voice setting candidate. Managed settings keep precedence over
ordinary inline flags; the product must inspect applicability without replacing
machine policy or impersonating a desktop/self-hosted environment.

## Credential, identity and quota binding

A before/after metadata comparison detects many changes, but not a change
between the last comparison and transport admission. A changed bearer can
also coexist briefly with old local account metadata or old cached quota.
PA1F's account-local cache timestamp can establish a newly written observation
under its assumptions; it does not identify the exact bearer used for that GET.
An A -> B -> A metadata sequence can evade two snapshots. These are ordinary
concurrency races, not a requirement to resist a malicious same-UID attacker.

A forwarding binding gives a precise partial remedy. Define each lease over
the selected account/organization, quota family, accepted model set, and the
credential used for its attributable quota observation. At the gate's existing
admission lock, compare the request's bearer and account/routing selectors to
that binding **before writing any upstream bytes**. Reject duplicates and
conflicting auth selectors; do not follow an upstream redirect into another
credential scope. A changed bearer or selector latches admission closed and
closes registered old streams. Do not silently replace a new account's bearer
with the old one: that would spend the wrong account's allowance.

Exact bearer equality can prevent requests with a different bearer from being
admitted under the old lease, without parsing or refreshing OAuth credentials.
It cannot establish which account owns an opaque bearer, authenticate a local
metadata file, or prove that native quota was fetched with that bearer. Codex
account headers and token claims help classification but are not, by themselves,
a new server-verified quota observation. Claude's local UUID and native usage
cache are particularly insufficient for organization binding.

The bootstrap must therefore obtain a model-free quota observation tied to the
same credential before its first model request. A native correlated identity/
quota API is one option. A reviewed model-free quota fetch using the exact
forwarded access bearer is another candidate only if provider semantics and
permission are proved; it is not implemented or authorized for live use by
this audit. The pending separately scoped setup-token usage request must not
be treated as completed or as proof for persistent-login refresh.

On native refresh, equality deliberately rejects the new bearer until a new
binding is established; native remains responsible for refresh and storage.
Closing the metadata race must not be achieved by exporting refresh material,
cloning stores or introducing wrapper OAuth recovery. Both providers need a
finite check covering concurrent refresh/account switch with a request already
waiting at admission. Already forwarded computation/billing cannot be rolled
back; the agreed bounded-overshoot limitation remains.

## Smallest candidate and finite remaining acceptance

The smallest useful candidate retains the actual native TUI, one owned local
backend/process tree, the selected persistent account, HTTP/SSE model traffic,
root/title/compaction and in-process descendants behind one invocation gate.
It forces one provider route, pins model/family admission, and rejects remote
attachment, detached execution, unsupported integrations and conflicting native
flags/configuration **before protected model work**. This candidate has not yet
been demonstrated as a complete supported configuration. Restrictions must be
visible and capability-checked; silently leaving owned work unprotected is not
an implementation choice.

For Codex, use the tested custom provider with a non-OpenAI display name so the
`supports_codex_backend_routes` workspace-routing branch does not replace its
URL. Pin its entire definition in both owned launches and sink both realtime
URLs to the gate's deny path. For Claude, pin the URL and necessary selector
values in inline settings, not just process environment; preserve real native
policy and reject unsupported configuration. No exact/upper version pin is a
substitute for checking these capabilities on a future minimum-compatible build.

The remaining work is the following bounded acceptance matrix. It does not
authorize another unrestricted research round or any live request.

| Check | Minimal experiment / pass condition | Decision if it fails |
| --- | --- | --- |
| C-route | One fake Codex native sequence: forced route, `/model`, local resume/fork, custom child model/role, and compaction. Gate closed during each relevant attempt; no alternate listener hit. Existing default descendants need not be repeated. | Restrict the specific reachable mode before startup, or review a narrowly defined RPC/config admission layer only if the TUI can actually alter the route. |
| C-voice | One fake native realtime start matching the TUI's WebRTC request with both realtime URL overrides pinned; it must reach only the denying local gate before a session is created. Test the actual `/voice` UI where the audio package is usable. | Native immutable disable/route capability or an explicitly reviewed control boundary is needed. Audio initialization failure is inconclusive. |
| A-route | Extend the successful inline-pin case with a native model/custom-child change, resume/project switch and an alternate provider selector. Add one native `/voice` attempt with inline false. | Reject a reproducibly unsupported mode before launch; if ordinary supported UI can evade the pin, request a native immutable policy hook or review an explicit narrower mode. |
| Already-running local tools | For Claude, start a private-workspace Bash command with `started`, bounded sleep and `finished` markers. Close the gate once with no native interrupt and once with native Ctrl-C; observe completion, UI usability and zero later model admission. PA1C explicitly excludes local-tool cancellation and supplies no such capture. | Record each operation's effect on running local tools, disclose it and review the pause choice before adoption; neither completion nor termination is assumed. PC5's Codex observation does not establish Claude behavior. Do not substitute broad process-tree killing for full invocation admission control. |
| Owned subprocesses | One ordinary tool starts an otherwise identical native child; one supported background/relaunch operation is attempted. Every owned model call must inherit the invocation gate, or the operation must be unavailable before initial model work. | Decide whether to add the documented native process-wrapper integration or restrict the tool/mode. Do not equate a surviving local training command with a model-route escape. |
| Identity race | Fake account A quota, pending admission, native switch/rotation to B, stale A cache, then fresh B data. No B request can use A's lease; failed correlation stays paused. | Choose the concrete same-credential quota-binding interface. Polling metadata more frequently does not fix an atomicity gap. |
| Full composition | One integrated fake run combining the accepted continuation parser, independent loss latch, binding invalidation and owned secondary stream. Linux and macOS conformance before a supported-platform claim. | Keep G0 closed if components cannot be composed without weakening the agreed behavior; do not repeat all accepted isolated matrices. |

The first four checks can reuse the existing fake native TUI/guard machinery
without a new production protocol. The ownership and identity rows contain
real design choices if configuration alone fails. A bearer-binding source
needs explicit G0 review; adding an RPC filter or subprocess propagation layer
also requires a concrete scope/cost decision. No physical-human attestation,
hostile same-UID defense, arbitrary administrator-policy-change guarantee,
runtime OS firewall, TLS interception, native patch, persistent daemon or
replacement UI is introduced as an unstated prerequisite.

Maintenance is material: stable gateway settings are small, but native request
projection, hidden secondary clients, environment layering and auth/cache
identity are version-sensitive. Native source/binary digests can audit a tested
release; runtime support needs model-free capability checks and a maintained
finite conformance suite. Unknown or inconsistent capabilities must refuse
protection before first model work. Implementing a general RPC router, arbitrary
subprocess monitor or OAuth client would exceed the demonstrated small wrapper
and needs a separate design justification.

## Validation and retained artifacts

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/routing_audit/run_reload.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/routing_audit/check_reload.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/routing_audit/test_reload.py
pre-commit run --all-files
```

The acceptance checker first failed with absent evidence. The new native run
then passed its unchanged PC guard self-check and both 40-second-bounded cases.
The checker passes; three offline tests include eight route/missing-event/time/
auth mutations plus a failed-guard mutation. Static assertions verify the
identified inheritance, compaction, image, goal, TUI-projection, removed-voice
flag and Claude environment/client anchors. They are source checks, not native
capability tests. All-files hooks and final diff validation are required before
the atomic research commit; the handoff records their final results.

The guard retains its documented cooperative-binary and sockaddr TOCTOU limits,
EXITKILL behavior and loopback/Unix-only policy. The runner bounds and reaps its
tracer; native children have explicit private environments and closed inherited
descriptors. Cleanup signals only the newly owned native process group. The
two normal exits were observed before cleanup. Raw fake-only native files,
terminal output, source download, static hashes and validation logs remain in
ignored `.cache/pr/`, durably within this worktree until coordinator archival.
No whole-suite rerun occurred outside the required all-files hooks. This is
research coverage only; no runtime source-to-test row or public contract changes.
