# Account Quota Reserve Implementation Plan and Handoff

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Follow the user's explicit parallel DAG and separate-worktree workflow instead of the skill's default prohibition on parallel implementers. Keep its independent spec-compliance and code-quality reviews. Steps use checkboxes for tracking.

**Goal:** Implement separate account-level five-hour and weekly reserves in both launchers, then integrate, validate, push, and clean up to a release-ready main.

**Architecture:** A shared, deterministic policy engine consumes provider-specific quota observations. Provider adapters must prove model-free startup queries and persistent, UI-preserving control of all model work owned by the invocation. No product runtime implementation starts until the native-control and Claude-authentication gates below pass.

**Tech Stack:** Python 3.11+ standard library, Bash/POSIX sh, existing Node.js 22+ helpers, native Codex/Claude interfaces, unittest and local native fixtures. No new runtime package, persistent service, or replacement terminal UI.

**Spec:** [Account quota reserve design](../specs/2026-11-11-account-quota-reserve-design.md).

**Research:** [Evidence and reproduction instructions](quota-reserve-research.md), including two preserved disposable Codex probes.

## Authorization and current state

The user requested this document for a NEW session. That session should use
multiple agents and independent worktrees, commit incremental progress, stop
or ask at manual decision gates, merge into main only after review and CI,
push, and remove this task's implementation-only documents. Execution method
is already chosen; do not ask the user to choose it again. Starting from the
launch prompt means the user has accepted this handoff for execution.

The user approved exploring native full OAuth login for protected Claude
accounts while retaining setup-token mode. This permits investigation, not
silent credential migration or using the user's default login for another
named account. The detailed credential design still requires human review.

Planning baseline: `ed7e401be12d0972762915f17d86acc0f3ff2629`, main, release
`0.7.7`. Reinspect before starting; another agent advanced main during this
conversation. No runtime feature, worktree, implementation commit, or remote
mutation was created by this planning session. The planning artifacts may be
untracked in the original checkout and need B0's preservation step.

The latest investigation has NOT proved full option A on either provider.
Codex current-turn interruption is demonstrated, but controller loss allowed
another model request. Claude's existing token mode fails the native profile
scope gate, and native usage may be cached without freshness provenance.
G0 is a real blocker, not a checkbox that a mock test can satisfy. The goal is
release readiness only if the evidence supports the agreed product.

### Current execution steering

The reviewed first research wave is integrated at `03f6b99`. Its native
counterexamples and PA0 proposal are evidence, not implemented functionality.
The user then approved additional **per-invocation model request gate proxy
research**, while retaining full A, native UI, owned-descendant coverage,
monitor-loss protection and verified manual continuation. This supersedes the
earlier prohibition on exploring a proxy; it does not approve a production
proxy, new public UX, replacement UI, persistent daemon or weaker behavior.

The user clarified the earlier token-only constraint: prefer setup-token-only
support; if it cannot satisfy protection, initial web login is allowed with
persistent credentials per account. Routine switching must not require another
login. This permits a persistent-login fallback in principle, not automatic
migration, default-account reuse, the detailed storage schema, shared device
state access or a live credential experiment. Native refresh remains native
owned; revocation or unrefreshable credentials require explicit recovery.

The new research nodes are PC2 (request-gate feasibility) and PT (token-only
startup quota). They extend the research before G0; they do not restart B0 or
authorize C/X/A/U. PTP prepares the offline-reviewed permission probe. PA2
extends persistent-login isolation evidence; PC3 extends subscription TUI and
routing evidence independently of the pending continuation-control decision.
PC4 adds bounded monitor/proxy-loss evidence for owned native streams within
the already-authorized gate research; it adds no continuation UX or runtime.
HAuth remains required for the concrete credential design.
Read `gate-decisions.md` in the durable ledger for decisions received after
any frozen checkpoint. Original task text below is historical where this
explicit steering supersedes it; all protection and publication gates remain.

## Global constraints

- Five-hour and weekly reserves resolve independently: invocation > account > global.
- Numbers are remaining percentages from 0 through 100; `off` disables a window; omission inherits. Factory defaults are off.
- Pause at exact equality as well as below the reserve. Never average windows, round before comparing, or substitute context/token/cost percentages.
- Query before model work; stale or missing enabled windows are unavailable. Persistent failures pause; finite retries never extend freshness indefinitely.
- Keep native UI, stop invocation-owned current/future delegated model work, and require verified manual continuation after a fresh quota check. Never silently substitute process exit.
- Preserve setup-token behavior when protection is off. No unapproved credential format, account migration, or OAuth refresh implementation.
- Prefer setup-token-only protection. A reviewed persistent-login fallback is permitted if needed; routine switching must not require login. Neither authentication nor a first inference substitutes for a model-free startup quota source.
- Python is `python3 -I -B`; modules load by physical path. Only config.py reads launcher HOME, XDG_STATE_HOME, or the kit config file.
- Linux/macOS, system Bash 3.2 compatibility, POSIX sh for claude-as/install.sh, no native Windows support. Native version floors must follow demonstrated capabilities, not an exact pin or upper bound.
- Tests use temporary homes, fictitious credentials, and loopback fixtures. A real-account probe requires explicit authorization and user-performed authentication.
- Documentation is printable ASCII English. Never commit secrets, machine-specific absolute paths, or AI attribution.
- New public flags/config/runtime paths require the next minor release. Expected baseline target is 0.8.0; recompute if main advances.
- No force push, tag creation, release publication, destructive reset, or broad cleanup. Release-ready does not mean a release has been tagged.

## Review focus

1. A successful RPC returns seeded/cached quota: it must not refresh a watchdog. Test in PA and both adapter tasks.
2. A monitor disconnects while children or queued work remain: no further owned model dispatch. Prove natively in PC/PA; regress in X/A and R.
3. A native session changes identity, quota family, or resumed thread: never apply a different account's reserve observation. Test in C/X/A.
4. One window resets while the other still violates policy, or an old prompt is queued: remain paused until a verified manual action and both checks pass. Test in C/X/A.
5. A copied/linked install, inherited policy, or changed shell argument position bypasses enforcement: test in U/I, including native arguments after NAME and offline metadata commands.

## DAG, ownership, and progress

```text
B0 inventory / isolated integration / durable progress
 |----------------------------|
PC Codex control + quota      PA0 Claude auth/control discovery
 |                            |
 |                            HAuth human credential-design review
 |                            |
 |                            PA1 approved-auth quota + control proof
 |----------------------------|
G0 human architecture review + native feasibility + interface freeze
 |
C shared configuration / prefix parser / policy and state
 |---------------------|----------------------|
X Codex integration    A Claude integration    U shell completions
 |---------------------|----------------------|
I packaging / cross-provider tests / permanent docs / minor version
 |
R independent whole-branch review and fixes
 |
V full local validation and frozen candidate
 |
CI1 ci/** push -> exact-SHA Linux + macOS success
 |
M1 ff-only main -> push -> main CI success
 |
D implementation-document cleanup commit
 |
CI2 fresh checks -> ci/** push -> exact-SHA success
 |
M2 ff-only main -> push -> main CI success
 |
Z worktree/branch cleanup and release-ready report
```

Four slots permit one coordinator plus three workers. PC and PA0 run together;
PA1 follows HAuth in the Claude probe worktree. PA below means this ordered
Claude research sequence, not a cyclic dependency on its own approval.
X, A, and U run together only after C is integrated. Reviews may require
waiting for a slot. Each write-capable worker has one checkout; reviewers are
read-only. The coordinator alone writes this handoff and integrates branches.

All worktree paths are relative to the ORIGINAL project root, not a worker's
checkout: `.worktrees/quota-integration`, `quota-probe-codex`,
`quota-probe-claude`, `quota-policy`, `quota-codex`, `quota-claude`, and
`quota-completion`, each under `.worktrees/`. Use `ci/quota-reserve` for the
integration branch and `task/quota-*` for task branches. If these names exist,
inspect and resume the matching task or choose a fresh suffix; never replace
someone else's worktree or branch.
Record the actual original root, integration branch/worktree, per-node paths,
and ledger directory in B0. Every later command uses these recorded values;
the default names above are not permission to operate an existing unrelated
branch. Run integration commands in the recorded integration worktree and
main commands only in the original main checkout.

| Node | Dependencies | Exclusive write ownership | Initial status |
| --- | --- | --- | --- |
| B0 | none | integration metadata, .gitignore, handoff | done; preservation commit and independent review passed |
| PC | B0 | Codex probe fixtures and own evidence report | blocked-user; reviewed checkpoint 9c660f8 and hooks passed |
| PA0 | B0 | Claude discovery fixtures and own auth/control proposal | done at 2df1ce5; independent review and hooks passed |
| PC2 | PC, explicit proxy-research authorization | new pc_proxy fixtures and Codex proxy report | reviewed checkpoint 9538f748; full A blocked-user |
| PT | PA0, setup-token preference | new claude_token fixtures and token-only report | reviewed checkpoint ad8c079; startup source unproved |
| PTP | PT | offline one-shot permission-probe harness and tests | done at d6ce140; 21 offline tests, hooks and independent review passed; no live authorization |
| PA2 | PA0, persistent-login fallback direction | new claude_persistence fixtures and report | done at 950948d; isolated matrix, hooks and independent review passed |
| PC3 | PC2 | new pc_ui fixtures and report | done at ab41738; native matrix, hooks and independent review passed |
| PC4 | reviewed PC3, existing gate-research authorization | new pc_loss fixtures and report | done at fe8c4a2; native matrix, hooks and independent review passed; no continuation UX |
| HAuth | any proposed credential-design change | coordinator human credential-design decision record | persistent fallback allowed in principle; concrete isolation/schema unresolved |
| PA1 | PT or reviewed fallback feasibility, applicable human gates | Claude approved quota/control fixtures and evidence | blocked on startup source, control proof and any real-account authorization |
| G0 | PC2/PC3/PC4, PA1 | coordinator architecture record and frozen interfaces | blocked; proxy routing alone does not prove full A |
| C | G0 | config.py, shared reserve.py, test_config.py, pure reserve tests | blocked |
| X | C | bin/codex-as, lib/codex-as/*, Codex-specific tests | blocked |
| A | C | bin/claude-as, lib/claude-as/*, statusline/helper changes, Claude-specific tests | blocked |
| U | C | four completion files, new completion-only tests | blocked |
| I | X, A, U | installer, shared test registration/fixtures, contract, coverage, product docs, coordinated version edits | blocked |
| R/V | I | coordinator fixes; reviewers read-only | blocked |
| CI1/M1 | V | coordinator Git/CI operations | blocked |
| D/CI2/M2/Z | M1 | exact task-doc cleanup, Git/CI operations | blocked |

For each node record: status (`pending`, `running`, `awaiting-review`,
`blocked-user`, `done`), owner, branch/worktree, base and head SHA, completed
checkboxes, test command/results, review verdicts, open questions, and next
action. A worker reports these fields to the coordinator instead of editing
central progress. The coordinator updates and commits this handoff after each
integrated node/wave and before yielding for user input.

Use an additional durable local ledger beneath the resolved Git COMMON dir,
`coding-agent-kit-handoffs/quota-reserve/`. Store sanitized logs, decisions,
review packages, candidate SHAs and CI run IDs there. This survives worktree
removal and does not make CI's source checkout dirty. Only the coordinator
writes it. Never use /tmp as the only copy of unfinished work or evidence.
After a candidate is frozen, record status here rather than committing more
handoff updates and invalidating the candidate SHA.
Inspect any existing ledger's task/run identity before writing. Resume only
the matching run; otherwise choose and record a separate suffixed directory.
Never overwrite another run's ledger when selecting new branch names.

### B0: preserve planning and create isolated ownership

- [x] Read AGENTS.md, this handoff, spec and research; inspect status, diffs, worktrees, branches, current local/remote refs, and active agent ownership. Record the actual base SHA.
- [x] Ensure `/.worktrees/` is ignored BEFORE creating it. Because main cannot receive an untested bootstrap commit, first add that exact entry to the shared `.git/info/exclude` if needed; record whether it was already present. Verify with `git check-ignore`.
- [x] Create `.worktrees/quota-integration` on a new `ci/quota-reserve` branch from current main. Add `/.worktrees/` to tracked .gitignore in that worktree. Leave the original main checkout on main.
- [x] Copy only the known spec, handoff, research and two probe files into integration. Compare bytes, stage explicit paths, run required hooks, and commit `docs: preserve quota reserve implementation handoff` with the ignore change.
- [x] Only after those copies are committed, remove identical untracked originals from the main checkout so a later fast-forward is not blocked. Preserve anything different or unexplained. Record the integration document path for subsequent resumes.
- [x] Initialize the identity-checked durable ledger and node records, including all actual branch/path selections. Create probe worktrees from the committed integration SHA. Each worker receives the original root, its exact worktree, dependency SHAs, allowed files, and its node's acceptance checks.

### Execution checkpoint: B0 preservation and probe dispatch

- Actual base remains `ed7e401be12d0972762915f17d86acc0f3ff2629`;
  local main and freshly fetched origin/main agree. Preservation commit:
  `0a79911` on `ci/quota-reserve` in `.worktrees/quota-integration`.
- The Git common directory contains the identity-checked durable ledger at
  `coding-agent-kit-handoffs/quota-reserve/`. Its `identity.json` records
  resolved local paths; `progress.md`, original byte-for-byte artifacts,
  hook logs, node briefs and independent review are preserved there.
- A pre-existing `.worktrees/` entry in `.git/info/exclude` covered bootstrap;
  this task did not add it. The committed ignore uses `/.worktrees/`.
- `pre-commit run --all-files` and staged `git diff --check` passed.
  Independent B0 spec-compliance and quality/security review both passed.
  No runtime implementation or native-account experiment was performed.
- PC owns `.worktrees/quota-probe-codex`, branch
  `task/quota-probe-codex`; PA0 owns `.worktrees/quota-probe-claude`, branch
  `task/quota-probe-claude`. Both start at `0a79911`. Workers only write
  provider-specific evidence and fixtures; coordinator owns this handoff.
- Automatic approval review rejected removal of the five untracked original
  duplicates twice, interpreting it as deletion of the retained spec.
  Separate checks proved all originals equal committed and archived copies.
  The user then explicitly approved removing exactly these five duplicates
  while retaining the committed spec. Removal succeeded, empty task-only
  directories were removed, and original main is now clean.
- B0 review identified proxy-only network containment and early-startup
  process cleanup hazards in the supplied disposable scripts. PC must use
  bounded supervision and establish containment before reproducing them.
- HAuth and G0 remain unapproved. No runtime node may start. The next task
  checkpoints are reviewed PC evidence and the reviewed PA0 auth proposal.

### Execution checkpoint: reviewed research and unresolved human gates

- PA0 is committed at `2df1ce5d4f95a42dacae3e2675897c26c258a4a5`.
  Its [auth discovery and proposal](quota-reserve-claude-auth-discovery.md)
  passed independent spec-compliance and quality/security review after one
  fix round. Two isolated native runs showed unavailable quota and zero
  inference; final `pre-commit run --all-files` passed on the reviewed tree.
- The proposal preserves setup-token and adds explicit native-login selection
  with native-owned credentials/refresh. Its root is
  `<secrets_dir>/native-claude-accounts/NAME/config`, outside every legacy
  `claude-NAME` or `codex-NAME` token pathname. The initial colliding proposal
  was rejected by review and corrected before requesting HAuth.
- The native shared `~/.claude/.device-keys.json` contains credential-bearing
  device identity outside `CLAUDE_CONFIG_DIR`. Static inspection found no
  independent supported path override; ordinary login/logout/usage and
  concurrency effects remain unverified. Candidate schema approval alone
  does not authorize access to it. Real-HOME enrollment stays blocked pending
  evidence or a separately reviewed human shared-state decision. There is
  no implicit switch to an isolated HOME and no real-account authorization.
- HAuth was asked to approve only this concrete schema candidate or request
  changes. Consult the durable `gate-decisions.md` for the latest response;
  approval cannot waive shared-state review, authenticated freshness or G0.
- PC's [native evidence report](quota-reserve-codex-evidence.md) has an
  independently accepted blocker conclusion, committed at
  `9c660f8af4ad98c79d3c01d4f74b14bb1d40b29e`. After goal pause and root
  interruption, controller loss leaves the backend available; starting old
  queued input increases model request count from 2 to 3, and programmatic
  continuation increases it from 3 to 4. Those are new dispatches. Native UI
  current-turn interruption is demonstrated, not a persistent reserve gate.
- PC also demonstrates a local model-free quota query, visible account
  mismatch, failure on HTTP 503, and native integer-only percentage parsing.
  These fixtures do not prove real account permissions, universal freshness,
  all quota families, active descendant control or manual-resume provenance.
- Independent PC review found and resolved failed-probe status propagation,
  timeout ownership, unqualified stream attribution, and local-path hygiene.
  Three supervision regressions and five evidence checks passed; the updated
  native suite reproduced the dispatch counterexample. Final hooks passed on
  the exact reviewed tree; logs, exit codes and cleanup inventory are archived
  in the durable ledger. No capability pass is inferred from those checks.
- PC's scope question is pending: retain current architecture restrictions
  and block dependent nodes, or explicitly permit additional research on a
  per-invocation request gate without weakening full A. That question grants
  no implementation permission. The two disjoint research branches are
  integrated as one ordinary merge wave preserving both commit ancestries.
- No runtime source, public interface, release version or permanent product
  behavior has changed. C/X/A/U/I/R/V and both publication/cleanup cycles are
  unstarted. Do not delete implementation evidence or claim release readiness
  while the human and native-capability gates remain open.

### Current ownership and research amendment

- First-wave PC/PA0 worktrees and their merged task branches were removed
  after clean-tree, ancestry, process and archival checks. Their commits and
  reports remain in integration and Git history. Twenty exact PC scratch
  roots and both PA0 roots were removed after evidence retention.
- PC2 owns `.worktrees/quota-proxy-codex`, branch `task/quota-proxy-codex`,
  based on `9c660f8`. Its scope is a new Codex proxy research report and
  `quota-reserve-probes/pc_proxy/`; prior evidence and runtime code are read-only.
- PT owns `.worktrees/quota-token-claude`, branch `task/quota-token-claude`,
  based on `2df1ce5`. Its scope is a token-only research report and
  `quota-reserve-probes/claude_token/`; prior evidence and runtime code are
  read-only. Check the ledger for its review/commit status after `ad8c079`.
- PC2 must distinguish subscription auth routing from fake API-key routing,
  test HTTP/WebSocket fallback and native refresh ownership, and establish
  the limits of queued work, alternate providers, listener loss and native
  UI. An admission latch alone cannot prove manual provenance, exhaustive
  descendant coverage or fail-closed monitor loss. Any new continuation
  control must be a concrete reviewed proposal before product implementation.
- PT must distinguish the local profile-scope gate from actual provider
  permission. It may inspect a zero-generation count endpoint as a hypothesis
  for subscription response headers, but token counts are never quota and
  native counting fallbacks must not trigger inference. No real token may be
  read or sent without a separately reviewed, bounded, named-account probe.
- Both nodes require focused checks, independent review, incremental commits
  and all-files hooks. Their outcomes may remain blocked-user; no partial
  evidence may open G0. The original C-through-Z sequence is unchanged.

### Research wave 2 checkpoint and next permission boundary

- PC2 is committed at `9538f7487bc6b9f82a846786312b9916da31b18f`.
  [Its report](quota-reserve-codex-proxy-research.md) and bounded fixtures
  passed independent spec/quality review, six contained native stages, five
  evidence checks and all-files hooks. Native subscription bearer/account
  routing, WebSocket-to-HTTP fallback and native refresh attempts are observed
  with fabricated credentials. A closed admission fixture rejects queued
  requests; it is not an upstream relay or a monitor-death lease.
- Subscription TUI bootstrap hits unsupported fixture account discovery;
  listener loss leaves a queued turn in progress during the observation
  bound. Trusted new manual continuation, exhaustive descendants/voice/role
  routing and monitor-loss safety remain unproved. PC2 therefore stays
  blocked-user. Any continuation-control/HTTP-only restriction proposal needs
  concrete review; no production proxy or new public UX is approved.
- PT is committed at `ad8c07924fa90b9a91931e560dfa6f5a9c31b46b`.
  [Its report](quota-reserve-claude-token-research.md) and fabricated native
  fixture passed independent spec/quality review, focused checks and all-files
  hooks. Repeated get_usage calls with both behavior-collection settings give
  unavailable quota and zero inference. This proves the local gate, not a
  universal provider permission denial.
- PT identifies one unverified hypothesis: a direct, documented zero-generation
  count response might carry subscription-window headers. Input token counts
  themselves cannot supply quota. The native count helper has a generation
  fallback and must not be used for this experiment. A successful HTTP Date
  and two headers would still not prove quota computation age, account UUID,
  family or safe poll cadence.
- PTP prepares the small, offline-tested one-request harness needed to make
  any later permission request concrete and independently reviewable. It owns
  `.worktrees/quota-token-permission`, branch `task/quota-token-permission`,
  based on `ad8c079`. No real token may be read or sent while preparing it.
  Execution requires separate named-account/exact-path/endpoint authorization
  after review; no permission is inferred from a CLI flag or research scope.
- The subsequent user clarification permits initial web login as fallback,
  with credentials retained per account and no repeated login during routine
  switching. This supersedes the former hard token-only constraint. The exact
  HAuth design, shared device-state effects and live probes remain gated.

### Execution checkpoint: permission-probe preparation and auth clarification

- PTP is committed at `d6ce140b6526e479cbb707e4a2400049f85dd29a`, following
  the initial `36c608a` checkpoint. Its [reviewable command and limits](quota-reserve-claude-permission-probe.md)
  describe one fixed count request, no generation or retry, explicit account
  and token path, strict credential handling and sanitized output. Twenty-one
  fabricated offline tests and all-files hooks passed. Independent review
  accepted the three bounded fixes: independent test network denial, accurate
  metadata-line limits and disclosed abrupt-parent-death limitations.
- This completes preparation only. No real token was read, no provider was
  contacted and no live request is authorized. A named account, exact configured
  token file and direct first-party HTTPS permission are still needed before
  the reviewed experiment. Syntactically complete headers would not themselves
  prove freshness, quota family, account identity or a polling policy.
- PC2/PT evidence was integrated at `796847a`. Their logs and temporary-root
  inventories were archived before verified cleanup of their worktrees and
  merged branches. The integration and PTP worktrees preserve ongoing work.
- PA2 and PC3 are independent bounded research extensions in separate new
  worktrees. PA2 checks persistent native-login and device-state isolation
  using fabricated records and denied network. PC3 investigates a sourced
  subscription TUI account-discovery fixture and native routing. Neither may
  read real credentials or change runtime behavior.
- A concrete G0 scope question is pending: permit research of HTTP/SSE-only
  protected routing plus a dedicated continuation key outside the native UI,
  or retain current interaction and wait for a native primitive. No new key,
  production proxy or weaker stop semantics is approved by asking. Continue
  independent research while awaiting that decision.

### Execution checkpoint: persistent selection and native delegated routing

- PA2 is committed at `950948d73574f4b2f9228f9e3906c0c7b614c5d5`.
  Its [persistence and device-state report](quota-reserve-claude-persistence-research.md)
  passed independent spec/security review and final all-files hooks. Fifteen
  native operations across three fabricated device-state cases preserve A/B
  credential separation; A logout clears A OAuth but preserves its legacy
  device record and B credentials. No shared device-key path was accessed in
  those status/logout cases. Fabricated logged-in status is not authentication
  proof, and these operations do not test real login or refresh.
- Static inspection finds account-indexed shared device keys, legacy migration,
  corrupt-file quarantine and native registration/reenrollment updates. Even
  a denied network call need not prevent a local device-key write. HAuth must
  review this specific shared-state exposure before real-HOME enrollment;
  ordinary per-account config separation cannot prove universal isolation.
  macOS, real account binding, refresh concurrency and quota freshness remain
  unproved. No actual credential or default-account file was read.
- PC3 is committed at `ab417382602dd09142b7048ba4743c49b4ed2472`.
  Its [subscription TUI and delegation report](quota-reserve-codex-ui-research.md)
  passed independent spec/security review, native fixture/evidence checks and
  all-files hooks. The account-discovery fixture comes from the matching
  official source. The native subscription TUI starts, root/title requests
  reach the configured gate, and root interruption leaves UI/history available.
  This resolves PC2's local discovery-503 fixture blocker, not real subscription
  permission, every UI interaction or workspace-policy compatibility.
- Native spawn history proves actual default root/child/grandchild delegation
  under fabricated SSE responses. All requests use the root session header;
  separate thread IDs and the native spawn graph establish ancestry. Root
  interruption leaves the title stream open at one second and descendant
  streams at two seconds. Individually interrupting descendants closes their
  local streams; no remote computation-cancellation guarantee follows.
- PC4 uses a separate worktree and branch `task/quota-loss-codex` from reviewed
  PC3 to test monitor/proxy-loss admission and owned-stream closure. It may not
  add a continuation key, product transport restrictions or production proxy.
  The two pending human questions concern that continuation research scope and
  one named-account setup-token count experiment; no answer is assumed.
- Neither research result passes G0. Real quota permissions/freshness,
  exhaustive routing, trusted new manual continuation, monitor/proxy loss,
  running tools, macOS and concrete timings/interfaces remain open. No runtime
  node, remote push or functional/document-cleanup publication cycle has begun.

### Execution checkpoint: loss gate evidence and current manual blockers

- PC4 is committed at `fe8c4a20d45bb4162c143a4d7ecc5127ae2e8bc1`.
  Its [monitor and gate loss report](quota-reserve-codex-loss-research.md)
  passed independent spec/security review, four native loopback cases, seven
  evidence regressions, final all-files hooks and an additional already-started
  full local suite. The suite's unset release-tag check skipped as expected;
  this is not hosted CI or validation of a new release version.
- The research relay starts closed and opens only after fabricated native quota
  and a monitor pulse. Monitor transport EOF, monitor process death and lease
  expiry latch it closed and shut all already owned root/title or default
  descendant streams. Old queued input is automatically consumed by native
  code but rejected at the closed gate. Passing quota and a later pulse do not
  unlock it. A separate native fixture session remains active and untouched.
- Killing the gate process closes its local transports. The attached TUI and
  history RPC remain available, but queued/new turns are still in progress
  after three seconds. This is no new upstream admission during the measured
  interval, not a completed permanent pause. The experiment's socket closure
  does not prove real provider computation or billing cancellation.
- Recorded timings describe this run only. A shared lock orders admission and
  stream closure relative to the loss latch; connect/send under that lock can
  delay detection, and already admitted bytes may cross the loss boundary.
  The 0.8-second lease and 20 ms poll are experimental values, not G0's frozen
  product timings. Restart, backpressure, all alternate routes, real quota,
  native tools, macOS and verified manual continuation remain unproved.
- Three concrete human questions are pending in the durable gate record:
  (1) whether to accept the reviewed persistent-login schema's native shared
  device-state boundary; (2) a named setup-token account and explicit exact-file,
  one-request direct HTTPS permission for PTP; and (3) whether to research
  HTTP/SSE protected routing with a dedicated continuation key outside the
  native UI. No reply, live operation or approval is inferred from elapsed time.
  The HAuth question links PA2 and covers design only, not actual enrollment.
- Resume according to those decisions. PTP may run only after account/path and
  transport authorization are recorded; its result still needs interpretation
  for freshness/family. Any full-login experiment needs a separately named
  account and user-performed authentication. A continuation prototype requires
  its scope decision, followed by native evidence and G0 review. Do not rerun
  completed research or start C/X/A/U while these gates remain unresolved.
- PA2/PC3 were integrated at `d27b266`; their verified temporary outputs,
  worktrees and merged branches were removed after durable archival. PC4
  cleanup follows this integration checkpoint. The integration worktree and
  common-directory ledger remain the recovery entry points. No remote branch
  push, CI1/M1, implementation-document deletion, CI2/M2 or release tag has
  occurred. The requested two-cycle release-ready outcome is not achieved.

### PC: prove Codex control rather than rediscover interruption

**Files:** read `lib/codex-as/{launch,check,handoff,usage}.py` and
`tests/test_codex_sharing_native.py`; create isolated conformance experiments
in the probe worktree and a task-owned research report. Do not edit runtime code.

- [x] Reproduce the three recorded signal/shared-backend outcomes with the supplied scripts against the currently installed binary. Preserve version, binary hash, requests, exit statuses, and terminal evidence; skip clearly if native binary is absent.
- [ ] Test a private per-launch foreground backend attached to the native TUI. Identify a documented/native mechanism that actually inhibits subsequent model dispatch, including internal subagent and goal continuation. A monitor RPC client or a TUI-only proxy is insufficient.
- [ ] Exercise root/child/grandchild activity, queued prompts, goals, controller death, backend failure, in-session resume, and session identity changes. A fake SSE request counter must stay unchanged after pause/monitor loss until a verified manual resume; record any in-flight requests separately.
- [ ] Establish the exact manual-resume signal and prove it differs from queued prompts and background reports. Verify TUI remains usable, and document behavior of already running local tools and terminal restoration.
- [ ] Prove model-free startup quota parsing and account/family association with native/local fixtures. Do not claim real server permissions from a mocked provider.
- [x] Report PASS only for the complete capability set in G0. If no dispatch gate can be established after targeted exploration, mark blocked-user, explain the missing mechanism, and ask whether scope should change. Do not implement a speculative proxy or weaken A.
- [x] Commit repeatable useful fixtures/evidence, with focused checks and required pre-commit; obtain an independent evidence review. Coordinator integrates the report and records its SHA.

### PA0, HAuth, and PA1: Claude authentication, freshness, and control

**Files:** read `lib/claude-as/launch.py`, `helpers/statusline.cjs`, native
authentication/control interfaces and Claude fixtures. Research only until
HAuth and G0. Keep the native default login and existing token files untouched.

- [x] **PA0:** reproduce model-free stream-json initialize/get_usage with a fully isolated, network-denied, fabricated-token harness. Assert zero model events/cost; pin `rate_limits_available:false` as an unsupported result rather than treating it as zero usage.
- [x] **PA0:** investigate native full login with `CLAUDE_CONFIG_DIR`, Linux credential storage, macOS Keychain namespaces, refresh, identity reporting, and session/settings isolation. Prefer native credential management. Do not simply copy the user's default credential or fake OAuth scopes.
- [x] **PA0:** produce a concrete proposed auth schema, enrollment command, private account paths/modes, same-account concurrency behavior, refresh ownership, compatibility behavior, and migration/recovery procedure that preserves existing setup-token mode. Commit the reviewed discovery report and record PA0 complete before requesting HAuth.
- [ ] **HAuth: stop for human review of that proposal.** Record the exact approval or requested changes. Before a real-account check, separately name the account, command, data accessed, and absence of model inference; the user performs any login. Authorization to research full login is not authorization to migrate credentials.
- [ ] **PA1:** prove native quota freshness on the approved path. A cached/seeded get_usage response with no proven age cannot reset the observation deadline. Test unchanged-but-new observations, stale cache, scope failure, missing windows, and endpoint retry/rate limits.
- [ ] **PA1:** investigate native session attachment/control with the full G0 matrix. Hooks may assist; UserPromptSubmit is not proof of human origin, SubagentStart cannot block creation, and headless control does not control another TUI. Record running-tool effects and monitor death.
- [ ] **PA1:** if full A or safe credential isolation is unavailable, stop dependent work and raise a specific design question. Otherwise commit evidence/fixtures, run checks, request independent review, and report exact capabilities and supported native versions.

### G0: manual architecture gate and interface freeze

- [ ] Present both probe reports together. Require evidence for startup quota, trustworthy account/family/freshness, interrupting owned active agents, persistent prevention of future dispatch, monitor-loss protection, usable native UI, and verified manual continuation. A mock implementation alone cannot pass this gate.
- [ ] Obtain human approval for the concrete native-control architecture and the already-reviewed Claude auth design. Any replacement UI, persistent daemon, substantial protocol/request proxy, process-exit fallback, partial-provider release, or weaker enforcement is a scope change requiring a decision, not an implementation shortcut.
- [ ] Freeze concrete adapter method implementations, capability/version checks, running-tool behavior, authentication paths/schema, and query/poll/retry/max-age timings from measured evidence. Record all values and tests in this handoff before dispatching C/X/A. Do not invent a minimum supported version from the planning binary alone.
- [ ] Freeze the shared interface contract below. If probe results require a change, revise consumers and spec together before parallel implementation; never let workers guess incompatible interfaces.

The proposed shared contract is deliberately provider-neutral:

```text
config.py:
  Config.reserves(tool: str, account: str) -> Mapping[str, float | None]
    normalized global/account defaults; keys five_hour, weekly; None is off

reserve.py:
  parse_prefix(argv: Sequence[str]) -> (remaining_argv: tuple[str, ...], overrides: Mapping[str, float | None])
    only separated --reserve-5h VALUE / --reserve-weekly VALUE before NAME
    omitted keys inherit; duplicate/invalid values fail; native tail untouched
  resolve(base: Mapping[str, float | None], overrides: Mapping[str, float | None]) -> Policy
  Policy(five_hour: float | None, weekly: float | None)
  WindowObservation(used_percent: float, resets_at: int, fresh_at: float | None)
    fresh_at is a proven observation time on the injected monotonic clock
  Snapshot(account_key: str, family: str, windows: Mapping[str, WindowObservation])
  Timing(query_timeout_s: float, poll_s: float, max_age_s: float, retry_backoff_s: tuple[float, ...])
  Decision(kind: str, windows: tuple[str, ...], reason: str)
    kind: allow, pause_threshold, pause_unavailable, paused, reject_capability
  Guard(policy: Policy, account_key: str, family: str, timing: Timing)
  Guard.observe(snapshot: Snapshot, now: float, wall_time: float) -> Decision
  Guard.unavailable(reason: str, now: float) -> Decision
  Guard.resume(snapshot: Snapshot, now: float, wall_time: float) -> Decision
    called only for an adapter-verified NEW manual continuation action
  LaunchContext(tool: str, account: str, executable: str,
    native_env: Mapping[str, str], cwd: Path, native_args: tuple[str, ...],
    account_home: Path, expected_account_key: str, inherited_fds: tuple[int, ...])
    caller owns configuration loading, enrollment, leases and post-run repair
  CapabilityReport(native_version: str, persistent_gate: bool,
    descendants: bool, manual_resume: bool, monitor_loss_safe: bool,
    fresh_quota: bool, running_tool_behavior: str)

each provider's quota.py / control.py:
  read_quota(deadline: float) -> Snapshot
    a provider adapter instance is bound to LaunchContext
  capabilities() -> CapabilityReport
  run_protected(context: LaunchContext, policy: Policy, timing: Timing) -> int
    preserve caller's native exit semantics and return for post-run repair
```

G0 must confirm that this context supports the proved ownership model or
revise the contract before those tasks become executable. C defines these
shared types; X/A instantiate provider adapters bound to LaunchContext and
implement the three methods above. This research dependency does not permit
an implementer to silently choose a different architecture.

### C: shared policy, config, and wrapper-prefix parsing

**Files:** modify `lib/coding-agent-kit/config.py` and `tests/test_config.py`;
create `lib/coding-agent-kit/reserve.py` and `tests/test_reserve_policy.py`.
Consume G0's frozen contract; produce it for X/A/U. Keep config loading in one
place and avoid importing provider adapters into config.

- [ ] Write failing tests for global 5h=20/weekly=30, account weekly=40, invocation 5h=10 -> 10/40; no invocation override on next launch -> 20/40; cross-provider same-name isolation; explicit off versus omission; 0, 100, fractions, NaN, infinity, booleans and duplicate flags.
- [ ] Add tests for exact equality, either-window crossing, no rounding, independent resets, stale/missing windows, wrong identity/family, unchanged fresh observation versus cached repeat, bounded repeated failures, pause latch and manual resume. Inject clocks; do not sleep for pure logic tests.
- [ ] Run `python3 -I -B -m unittest discover -s tests -p test_reserve_policy.py -v` and `tests/config.sh -k Reserve`; verify meaningful failures, then implement the frozen functions and immutable normalized config fields.
- [ ] Test first-position shell help/version and offline queries unchanged, malformed guard-prefix/query combinations rejected without native calls, and every argument after NAME forwarded without wrapper reinterpretation.
- [ ] Make focused suites pass, deliberately stage new runtime paths, run `pre-commit run --all-files`, commit `feat: add per-window quota reserve policy`, and obtain independent spec/quality review. Coordinator merges C before creating X/A/U worktrees.

### X: Codex adapter and protected launch

**Files:** `bin/codex-as`, `lib/codex-as/{launch,check}.py`, new
`lib/codex-as/{quota,control}.py`, `tests/test_codex_reserve.py`, and Codex-only
native conformance fixtures. Preserve after-exit `usage.py` display behavior.

- [ ] Write failing fake/native tests for model-free preflight, quota family and window duration mapping, startup refusal, native control capability refusal, and disabled-policy equivalence. Use `tests/test_codex_sharing_native.py` patterns; do not treat its stdio client as proof of TUI ownership.
- [ ] Add the full PC control matrix as regressions, including active descendants, controller death, queued prompts, manual continuation, account/session switching, and running-tool semantics agreed in G0.
- [ ] Implement only G0's proved quota/control mechanisms. Pass account-scoped CODEX_HOME/file auth, preserve shared leases, signals, credentials refresh/repair, native exit semantics, stdin/stdout and terminal restoration. Keep the existing disabled-policy path intact.
- [ ] Integrate prefix parsing and update this launcher's help. Retain all legacy options. The internal protected backend must not enable arbitrary user-supplied remote/provider overrides.
- [ ] Run `python3 -I -B -m unittest discover -s tests -p test_codex_reserve.py -v` and `tests/codex-as.sh`; run required hooks, commit the bounded adapter/integration changes, and obtain independent review before coordinator integration.

### A: Claude full-login path, adapter, and protected launch

**Files:** `bin/claude-as`, `lib/claude-as/launch.py`, new
`lib/claude-as/{quota,control}.py`, the HAuth-approved credential module if
needed, `helpers/statusline.cjs` only as required, `tests/test_claude_reserve.py`
and Claude-only native fixtures. Do not edit shared enrollment tests here.

- [ ] Write failing tests for the approved full-login design and preservation of setup-token mode; identity mismatch, secure paths, refresh, enrollment failure/recovery, and no default-account credential reuse. Use the exact HAuth schema and paths, not newly invented ones.
- [ ] Test unsupported inference-only quota queries, cached/seeded responses with unknown freshness, missing windows, startup refusal, mode/version capability checks, and all PA pause/manual-resume/monitor-loss cases.
- [ ] Implement native-managed authentication and the frozen quota/control adapter. Do not implement OAuth refresh yourself. If supervising rather than execing, keep the status line's validated actual-Claude-PID lineage and inherited environment sanitization correct.
- [ ] Integrate prefix parsing and this launcher's help; preserve unprotected exec behavior and existing public usage metadata. Any new helper needs I's install/contract registration, not a hidden user-global hook mutation.
- [ ] Run `python3 -I -B -m unittest discover -s tests -p test_claude_reserve.py -v` and `node --test tests/claude-customizations.cjs`; run required hooks, make atomic commits and obtain independent review before integration.

### U: complete wrapper flags without consuming native arguments

**Files:** four files under `completions/{bash,zsh}/` and
`tests/test_reserve_completion.py`. X/A own launcher help; I owns shared
`tests/test_account_enrollment.py` edits to avoid cross-branch write conflicts.

- [ ] Write failing Bash/zsh cases for each flag/value position, both flag orders, account completion after flags, off values, missing values, and native delegation after NAME. Assert account candidates still use offline labels/listing and never start a native client or read a credential.
- [ ] Implement scanning of the wrapper prefix; reconstruct native completion argv/cursor position after removing every wrapper argument, not only one fixed account position. Keep legacy own-option behavior.
- [ ] Run `python3 -I -B -m unittest discover -s tests -p test_reserve_completion.py -v` with installed shells and explicit optional skips; format each shell dialect, run hooks, commit and obtain independent review.

### I: integrate contracts, installation, docs, and release version

- [ ] Coordinator serially merges reviewed X/A/U heads into integration using ordinary merge commits when branches diverged. Never squash/cherry-pick away ancestry merely for cleanup; normal ancestry allows safe `git branch -d`. Run affected focused tests after each merge.
- [ ] Register an executable `tests/reserves.sh` entrypoint that discovers the new `test_*reserve*.py` suites, add it to `tests/run.sh`, and scope a pre-commit hook to all affected modules, helpers and tests. Copy the exact unset statement; synchronize every existing entrypoint and Python setUp if any new kit environment names were introduced.
- [ ] Add all new public runtime paths/modes to `tests/contract.sh` and deliberately stage them before its index-based check. Update source-to-test rows in `tests/COVERAGE.md`, shared enrollment/config link-farm fixtures, incomplete-install cases, and guarded linked/copied/managed install tests. Audit `install.sh` only where new helper links require it; recursive lib copies alone do not prove runtime loading.
- [ ] Update permanent README/account-launchers/configuration/codex-as docs with usage, effective policy, auth-mode limitations, supported modes, freshness/failure behavior, manual resume, tool interruption effects, and minimum native/kit versions. Keep machine output contracts stable and document older-kit config-ignore behavior.
- [ ] Recompute the next minor, expected 0.8.0. Update both launcher version literals and first CHANGELOG heading together, listing added interfaces. Preserve intervening main fixes and release notes.
- [ ] Run `tests/reserves.sh`, `tests/config.sh`, `tests/account-enrollment.sh`, `tests/account-info.sh`, `tests/contract.sh`, `tests/standalone-e2e.sh` and `tests/managed-install.sh` as applicable; run required hooks, commit integration, and update handoff progress.

### R/V: independent review and the exact local candidate

- [ ] A fresh read-only reviewer checks the entire feature base..head diff for spec compliance and another independent pass checks correctness, security, concurrency and regressions. Supply the native proof reports, auth design approval, test results and Review Focus list. Fix substantive findings in atomic commits and re-review affected code.
- [ ] Complete any human acceptance check required by HAuth/G0, especially real credential capability, native UI pause/resume, running tools, and macOS auth isolation. If proof is missing, report blocked rather than treating skipped tests as success.
- [ ] Integrate current local main and fetched origin/main into the integration branch without rewriting history. Preserve unrelated work; new conflicts or changes require fresh relevant tests and review.
- [ ] Write and deliberately stage the last progress update BEFORE final validation. Run `./tests/run.sh --ci` when prerequisites are present, `pre-commit run --all-files`, and `git diff --check`; verify all remaining source changes are the intended staged candidate. If --ci prerequisites are absent, run the local suite and record the skip; hosted Linux/macOS CI remains mandatory. Contract and Stow coverage cannot be silently waived.
- [ ] Commit the validated candidate, then run `pre-commit run --hook-stage manual gitleaks-history`, the clean-tree check, and the release check against committed HEAD with command-scoped `KIT_RELEASE_TAG=vNEXT_MINOR tests/config.sh -k ReleaseVersion`, substituting the actual version. Do not create a tag. Record the candidate SHA and outcomes in the durable ledger; a failing check requires a new fixed candidate, not publication.

## Commit, review, and worktree protocol for every node

Tests must fail for the intended behavior before implementation, then pass.
Run repository-required `pre-commit run --all-files` before each commit;
stage explicit paths, never `git add .`. Each checkpoint is a coherent,
reviewable commit with no AI attribution. If work is blocked, commit only a
coherent partial change or its evidence report and mark the node incomplete;
never label a placeholder implementation done.

Reviewers receive task requirements, frozen interfaces, base/head SHAs, the
whole task diff, exact tests and unresolved limits. They must not be the
implementer. Fixes use additional commits, with focused re-review. Only the
coordinator merges completed node branches and updates central progress.

After integration and validation, confirm worker/fixture processes are gone,
inspect status/diff/untracked files, and verify the worker head is an ancestor
of integration. Then promptly `git worktree remove EXACT_PATH` and run
`git branch -d EXACT_BRANCH` from a checkout that contains it. Do not force
removal or deletion. Preserve and report unexplained files or unmerged work.

## CI, main integration, and final cleanup

### CI1 and M1: publish the functional candidate

- [ ] Load the actual integration branch and frozen SHA from the ledger as `QUOTA_CI_BRANCH` and `QUOTA_CANDIDATE`. Push only that branch with `git push -u origin "$QUOTA_CI_BRANCH"`. A ci/** push before main is required by AGENTS.md; it is authorized by this execution workflow.
- [ ] Find the ci.yml run for that exact head SHA and branch. Prefer available GitHub tools; if they cannot list push-triggered runs, use `gh run list --workflow ci.yml --branch "$QUOTA_CI_BRANCH" --commit "$QUOTA_CANDIDATE"`, then inspect/watch the specific run. Require BOTH Linux behavior/lint and macOS jobs to succeed, including history scan and clean checkout checks.
- [ ] Do not edit tracked handoff to record green CI: that changes the candidate. Store run IDs, URLs and conclusions in the durable ledger. Any tracked change requires a new candidate and CI run.
- [ ] Fetch origin again. Check that both current local main and origin/main are ancestors of the candidate. If either advanced outside it, merge the new main into integration and repeat review/testing/CI. Never rewrite main or force push.
- [ ] Confirm the original main checkout is clean and no other writer owns it. Run `git merge --ff-only CANDIDATE_SHA` there, then `git push origin main`. A concurrent push refusal means fetch/integrate/revalidate, not force. Verify main's ci.yml run for that SHA also succeeds.

### D, CI2, and M2: remove implementation documents AFTER the first push

The user's requested order needs a second validated cleanup candidate. A
documentation deletion changes HEAD just like code; the previous green SHA
does not certify it. Keep the integration worktree until this second cycle.

- [ ] Migrate lasting behavior, supported versions, limitations and user-facing recovery guidance into the permanent docs if not already done. Archive the final handoff/research, human decisions, reviews and CI URLs in the durable local ledger and record the functional commit containing them in Git history.
- [ ] Remove only these task-owned implementation files with apply_patch: this handoff; `docs/superpowers/plans/quota-reserve-research.md`; `docs/superpowers/plans/quota-reserve-probes/codex_signal.py`; `docs/superpowers/plans/quota-reserve-probes/codex_same_backend.py`; and additional task evidence files explicitly recorded in the ledger. Do not recursively delete docs directories or unrelated plans. Retain the design spec and permanent product docs; remove any links to deleted artifacts.
- [ ] Check remaining references, empty task-only directories and temporary outputs; remove exact verified targets only. Commit `docs: remove completed quota reserve implementation handoff` on integration after required hooks. Update progress in the durable ledger from now on; recover old instructions with `git show FUNCTIONAL_SHA:PATH` if needed.
- [ ] Repeat final local checks, whole-diff cleanup review, CI-branch push and exact-SHA ci.yml verification. Repeat ALL M1 concurrency checks: fresh fetch, local/remote main ancestry, clean main checkout with no other writer, and re-integration/revalidation/new CI if main advanced or push is rejected. Only then fast-forward main to the cleanup candidate and push normally; verify main CI again. Fixes require another candidate, never an untested direct main commit.

### Z: verify release-ready state and remove owned branches

- [ ] Confirm local main equals origin/main at the final reviewed SHA, all required final CI jobs are successful, and `git status --porcelain --untracked-files=all` is empty. Verify committed version literals/CHANGELOG match and the release-tag validation command passes; do not tag or publish.
- [ ] Confirm no agent/process uses any remaining task worktree. Check clean status and full ancestry, remove the integration and remaining task worktrees with `git worktree remove`, then delete only merged task branches with `git branch -d`. Delete only this task's remote ci branch after final main CI succeeds.
- [ ] Remove verified task temporary roots/intermediate files and the bootstrap's local exclude entry only if this task added it and the tracked ignore now suffices. Keep the durable local audit archive; it is intentional recovery evidence outside the source tree.
- [ ] Final report: final main SHA/version, review outcomes, exact test commands and skips, Linux/macOS CI links, native capabilities actually demonstrated, manual approvals, removed worktrees/branches/docs, and tag not created. Do not report release-ready with a failed gate, missing manual check, dirty main, unpushed cleanup, or only one green CI job.

## Session-resume checklist and next action

- [ ] Resolve original root and Git common dir; read current AGENTS.md.
- [ ] Read this handoff/spec/research, or recover their last archived versions if cleanup has begun.
- [ ] Reconcile ledger node states with actual branches, SHAs, worktrees, active agents and CI. Do not rerun completed nodes just because the chat context is new.
- [ ] Preserve pending user questions and approval scope. Ask only for the exact blocked design/credential/manual-check decision; continue independent authorized work while waiting.
- [x] B0, PA0, PTP preparation, PA2, PC3 and PC4 research are complete;
  capability gaps remain. Resume the pending HAuth, named-account experiment
  and continuation-scope decisions from the durable ledger, not B0.
  Setup-token-only is preferred; persistent
  initial login is permitted as fallback, with no routine repeated login.
  G0 remains closed; no runtime implementation is authorized. Concrete
  credential design and real-account experiments retain their gates.
