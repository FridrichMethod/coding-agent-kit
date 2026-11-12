# Account Quota Reserve Implementation Plan and Handoff

> **For the next Claude session:** Use the user's selected ultracode
> multi-agent workflow with separate worktrees. Discover and read its installed
> instructions; this session did not establish an ultracode command. The
> coordinator owns integration and central progress. Independent writers own
> separate worktrees; each node requires tests, independent review and commits.

**Goal:** Implement independent five-hour and weekly subscription reserves in
both launchers, then complete both publication/cleanup cycles below.

**Architecture:** Shared policy consumes fresh provider observations. Protected
launches need model-free preflight, credential-bound admission and persistent
control of all invocation-owned model work while preserving the native UI.
An outer platform isolation boundary plus per-invocation request gate is the
conditional direction; G0 must approve a tested composition.

**Tech Stack:** Existing Python 3.11+ standard library, shells and Node helpers;
native interfaces and local fixtures. Optional platform isolation dependencies
are accepted for protected mode. No component is selected or installed here;
ordinary unprotected dependencies remain unchanged.

**Spec:** [Permanent design](../specs/2026-11-11-account-quota-reserve-design.md).
**Research:** [Original evidence](quota-reserve-research.md),
[auth next steps](quota-reserve-auth-next-session.md), and
[descendant boundary](quota-reserve-descendant-design.md).

## Current checkpoint and recovery

The user changed this session's deliverable to completed exploration and an
adaptable handoff. A NEW Claude session will implement. Product runtime is
unchanged; G0 is not passed; neither publication cycle has started. Do not
report the feature implemented or release-ready from research reviews/hooks.

- [x] B0 and research nodes in the evidence map are committed and reviewed.
- [x] HAuth persistence/schema/shared-device design and optional isolation
  dependency research decisions are accepted.
- [x] Both individually approved setup-token requests are consumed: count
  returned 200 without quota headers; usage returned 403 `permission_error`.
- [ ] Q/N/K feasibility and concrete human G0 acceptance remain before runtime.
- [ ] C/X/A/U/I/R/V and CI1/M1/D/CI2/M2/Z remain unstarted.

Resume `.worktrees/quota-integration`, branch `ci/quota-reserve`. The original
root intentionally has no duplicate handoff. Run `git worktree list` and
`git rev-parse --git-common-dir`; inspect `coding-agent-kit-handoffs/quota-reserve/`
under that common directory. Its `identity.json`, `gate-decisions.md`,
`latest-checkpoint.json`, `progress.md`, reviews and archives contain exact
heads, approvals and source/evidence provenance. Reconcile them with Git and
active processes before writing. Do not initialize B0 again or overwrite a
concurrent agent's work. Unrelated worktrees are not this task's cleanup targets.

The pre-consolidation chronological handoff is preserved at `f0158df` and in
the durable archive. This file supersedes its stale pending questions and
rigid internal signatures. Frozen source/evidence remains at its recorded
commit; corrections add provenance instead of silently rewriting captures.

## Authorizations and hard requirements

The user already authorizes incremental commits, ordinary integration,
`ci/**` pushes, exact-green-commit fast-forward of main and normal pushes,
and verified task worktree/branch/temporary-file cleanup. Do not repeatedly
confirm these. No force push, tag creation/publication or existing-tag change.

- Resolve each window independently: invocation override, selected account,
  global, then off. CLI: `--reserve-5h VALUE`, `--reserve-weekly VALUE`, before
  NAME; values are finite 0..100 or `off`. Omitted inherits; duplicate errors;
  zero is enabled; equality pauses; native tail stays unchanged. The spec pins
  TOML fields/examples. Do not combine percentages or substitute token budgets.
- Fresh model-free quota precedes model dispatch. Cache-only, missing, malformed,
  stale, wrongly attributed or unsupported-family data is unavailable. Repeated
  failures cannot renew freshness. Independent resets never auto-resume.
- Full A includes native agents, shell/tool model clients, hooks/local helpers,
  detached descendants and explicitly adopted work. Independent user sessions
  remain outside. Environment/PATH pins and process-name killing are inadequate.
- Stop current/future owned model dispatch on threshold or monitor/control loss;
  preserve usable native history/UI. New deliberate continuation plus fresh
  passing quota is required. Old queued input, paste, hooks or recovery cannot
  unlock. No final inference for a summary/commit. No exact overshoot guarantee.
- Retain setup-token mode. Prefer token-only protection if proved. Otherwise
  use one initial user web login per account and persist native credentials;
  ordinary switching must not login/logout again. Native alone refreshes.
- Accepted HAuth: `claude-as --enroll-native NAME`,
  `[claude_auth.accounts.NAME] mode="native-login"`, fixed private
  `<secrets_dir>/native-claude-accounts/NAME/config/`. Native shared
  `HOME/.claude/.device-keys.json` effects are accepted. No default OAuth reuse,
  automatic migration, native lock management or wrapper refresh client.
- Optional isolation dependencies are accepted for protected mode; unsupported
  capability refuses protected startup. This is the task-specific exception
  to AGENTS.md's dependency baseline. It does not authorize installation,
  privileged services, global firewall changes or weakened protection.
- Both-off behavior, offline metadata, native arguments, terminal/exit semantics,
  credential repair and independent sessions retain existing contracts. Unknown
  versions/modes refuse enabled reserves instead of silently falling back.
- Preserve the permanent spec and product docs. Both publication cycles are
  required after implementation, with no release tag.

An access-bearer research-boundary question is pending at this checkpoint:
whether to explore read-only short-lived access-token acquisition from the
selected native file/Keychain, in memory, without persistence, refresh or
default-store copying. Existing HAuth does not grant that access. Check the
latest ledger/user reply before dependent work; research approval would not
authorize actual credential reads or an unreviewed live experiment. Continue
independent isolation/offline work while awaiting it. The exact original
prohibitions and candidate alternatives are in the auth report.

## What implementation may change

The implementer may revise internal types/signatures, file splits, adapter or
library choice, node granularity, fixture structure and measured timeout values
as evidence develops. Agree a small shared contract before parallel consumers;
update consumers, tests and central progress together when it changes. Old
example dataclasses, F12/challenge UX and research timings are not mandatory
product APIs. Ordinary implementation/refactoring needs no new user approval.

Evidence contradicting a candidate calls for a bounded adjustment and test,
not another unbounded exploration program. Raise material auth, ownership,
platform, dependency or compatibility changes immediately with concrete options;
continue independent nodes. No flexibility waives HAuth/G0, native UI, full A,
freshness, manual continuation or the two publication cycles. Do not silently
ship only one provider/platform or replace persistent pause with process exit.

## Evidence map: reuse before adding tests

All entries are bounded evidence, not a complete production proof. Native
versions were Codex 0.167.2 and Claude 2.1.322; they are not supported floors.
Exact binary/source hashes, checks and reviews are in the reports and ledger.
Re-run only for a changed mechanism/version or unresolved assertion.

| Nodes | Accepted result and remaining limit |
| --- | --- |
| [PC](quota-reserve-codex-evidence.md), [PC2](quota-reserve-codex-proxy-research.md) | Native interrupt preserves UI; paused goal/controller disconnect do not prevent queued/new requests. HTTP/SSE custom-provider gate is a candidate. |
| [PC3](quota-reserve-codex-ui-research.md), [PC4](quota-reserve-codex-loss-research.md) | Actual TUI, child/grandchild routing; monitor EOF/death/staleness and gate death close owned streams, independent session survives. No arbitrary-shell containment proof. |
| [PC5](quota-reserve-codex-continuation-research.md) | Trusted terminal confirmation rejects old input/paste/native output/recovery; fresh two-window check. Local command can finish after turn interruption. |
| [PA0](quota-reserve-claude-auth-discovery.md), [PA2](quota-reserve-claude-persistence-research.md) | Native persistence schema and fake A/B/A reuse. Real refresh and macOS isolation still need proof. |
| [PT](quota-reserve-claude-token-research.md), [count result](quota-reserve-claude-count-result.md), [usage result](quota-reserve-claude-usage-result.md) | Native setup-token usage unavailable; live count has no windows; one live usage GET is 403. These paths cannot enable protection; not universal impossibility for every token interface. |
| [PA1F](quota-reserve-claude-freshness-research.md) | Usage may return old cache after errors/fieldless success. Native fetch timestamp lacks bearer/organization binding. |
| [PA1C](quota-reserve-claude-control-research.md), [PA1T](quota-reserve-claude-tools-research.md) | Claude native UI/agents/loss/continuation tested with fake OAuth. Gate-only lets Bash finish; Ctrl-C interrupts the measured shell. No universal tool termination guarantee. |
| [PR](quota-reserve-routing-audit.md), [PC6](quota-reserve-codex-realtime-research.md) | Claude inline settings close tested URL reload escape; Codex two realtime paths reach a denying gate. Not audio/voice-UI support or arbitrary-client containment. |
| [PN](quota-reserve-auth-next-session.md), [PO](quota-reserve-descendant-design.md) | Reviewed finite auth and outer-boundary directions; no native correlated quota/bearer interface or cross-platform composition established. |

PC6's offline path-redaction provenance is separate from capture provenance.
PA1T's earlier failed guard attempt stopped before native TUI launch; a guarded
version-helper fork may have occurred. Failed runs and test-only network guards
do not prove the eventual product mechanism.

## Remaining DAG

```text
Accepted evidence and current user decisions
  |-- Q0: offline auth/quota binding harness -- HLogin/Q1: scoped live proof
  |-- N-L: Linux/WSL2 outer-boundary feasibility
  |-- N-M: macOS outer-boundary/native-store feasibility
  Q + N-L + N-M -> K: composed admission/loss/continuation -> human G0
  G0 -> C: shared policy/config/contract
  C -> X: Codex adapter -----+
    -> A: Claude adapter ---+-> I -> R/V -> CI1 -> M1
    -> U: completions ------+                   -> D -> CI2 -> M2 -> Z
```

Q0/N-L/N-M can run in parallel worktrees. Split or combine nodes where their
tests justify independent review; preserve dependencies and writer ownership.
Feasibility prototypes and fake tests are allowed before G0, product runtime
C/X/A is not. A platform test unavailable on this host is an outstanding gate,
not a skip that proves support. Choose the smallest next experiment that can
reject or validate a candidate, then stop or advance on its result.

### Q: attributable quota and persistent auth

Own research fixtures/reports under `docs/superpowers/plans/`, not runtime.
Follow PN's N0/HLogin/N1 outline; native login is a human node, not a shell
command silently authorized by this plan. No real request is authorized here.

- [ ] Review one bounded offline harness: secure fixed paths, pending/ready
  registration, policy preservation, incompatible auth selectors, sanitized
  output, deadlines, child cleanup and explicitly bounded auxiliary traffic.
- [ ] Resolve the credential-interface boundary. Prefer a native correlated
  observation/handle if available. Otherwise present the concrete memory-only
  access-bearer alternative under its separate permission. Do not send inference
  to obtain a bearer or assume local UUID/cache polling closes the race.
- [ ] Only after review, request exact named-account/login/query permission.
  User performs any first login. Reuse the saved directory without relogin;
  do not retry consumed setup-token requests or add accounts automatically.
- [ ] Prove fresh enabled windows before inference, selected account/organization
  and applicable family association, and same-credential admission. A pending B
  request after A quota/refresh must never use A's lease; stale cache and A/B/A
  races refuse. Include concurrent native refresh and unchanged-but-fresh data.
- [ ] Apply the same attributable preflight/rotation contract to Codex; preserve
  native auth leases. Fixtures do not prove live permissions. Record actual
  capability and any separately scoped human acceptance needed.

### N and K: mandatory boundary and native compatibility

PO supplies candidate comparison, platform costs and four finite acceptance
groups. Existing env/URL routing is not the mandatory boundary. Do not implement
a Python syscall sandbox or install a service by default.

- [ ] Select a reviewable existing platform adapter; test root, shell/SDK/native
  descendants, cleared env, absolute paths, detached/reparented children,
  IPv4/IPv6, inherited descriptors and one host-daemon IPC escape. The product
  boundary must cause denial; an external test guard must not mask an escape.
- [ ] Prove exactly controlled exits/streams, no direct fallback, and refusal of
  unproved remote/adopted work. Domain/CONNECT rules cannot distinguish auth
  and inference on the same TLS host. No unconditional provider-host exception,
  speculative TLS interception or generic metadata relay.
- [ ] Prove native refresh/model-free quota can coexist with containment. Native
  owns refresh; losing that path refuses/pauses instead of bypassing the gate.
  Test Linux/macOS credential/PTY/IPC needs and actual WSL capability.
- [ ] K combines Q/N with a queued child request, monitor loss, gate death/restart
  and port reuse, persistent-stream closure, fresh manual continuation and an
  independent session. Native history/TTY stays usable. Reuse PC/PA fixtures.
- [ ] Measure running-tool effects and one ordinary git/package/tool workflow.
  The conservative candidate closes all invocation egress on pause and denies
  unclassified clients; disclose this compatibility cost at G0. It is not yet
  accepted that arbitrary remote tools will work transparently.

### G0: human review of the concrete composition

- [ ] Present exact tested architecture, auth boundaries, supported/rejected
  versions/modes/platforms, dependency and network costs, owned-work coverage,
  tool effects, quota attribution, loss closure and continuation evidence.
- [ ] Choose bounded query/poll/backoff/max-age values from measurements. Confirm
  immutable policy semantics and a small shared contract before C/X/A dispatch.
- [ ] Obtain human architecture acceptance. A missing mechanism is a blocker
  with concrete alternatives, not permission to weaken A or ship partially.

### C, X, A, U: implementation after G0

Suggested files and test ownership follow existing layout; adjust coherently.

| Node | Write ownership | Required acceptance |
| --- | --- | --- |
| C | `lib/coding-agent-kit/config.py`, new shared reserve module, `tests/test_reserves.py`, config tests | Per-window/provider precedence, off vs omitted, 0/100/equality, invalid bool/nonfinite/range/duplicates; native tail; no persisted overrides. Deterministic freshness/reset/pause/manual-resume evaluator with injected clock. |
| X | `bin/codex-as`, `lib/codex-as/launch.py`, new quota/control modules, `tests/test_codex_reserve.py`, Codex fixtures | G0 mechanisms, model-free start, account/family/rotation, owned routing/loss/continuation, mode refusal; auth leases/repair, exit/signals/TTY and disabled path unchanged. |
| A | `bin/claude-as`, `lib/claude-as/launch.py`, approved auth/quota/control modules, `tests/test_claude_reserve.py`, required statusline changes | Persistent enrollment, setup-token compatibility, secure failure/recovery and identity isolation; complete protection matrix; actual Claude PID lineage and unprotected exec preserved. |
| U | four Bash/zsh completion files, `tests/test_reserve_completion.py` | Both flag orders/values/off/missing values, account completion after prefix, correct native argv/cursor delegation after NAME; offline completion never logs in or queries quota. |

Each node writes focused behavioral tests first, observes the intended failure,
implements the smallest coherent change, then runs its focused suite and
`pre-commit run --all-files`. Use isolated unittest discovery; X also runs
`tests/codex-as.sh`, A relevant Node helper tests, U installed Bash/zsh cases
with explicit optional skips. No tests that merely mirror prose. Shared
entrypoint/enrollment/packaging tests belong to I to avoid conflicting writers.

### I and R/V: integration and final local candidate

- [ ] Merge reviewed nodes serially, retaining ancestry and testing affected
  behavior. Register new paths in `tests/contract.sh`, a reserve suite in
  `tests/run.sh`/hooks, `tests/COVERAGE.md`, config/enrollment/link-farm and
  incomplete/linked/copied/managed-install fixtures. Synchronize every entrypoint
  unset list and Python environment sanitization for any new kit variables.
- [ ] Update permanent docs and applicable AGENTS.md dependency guidance,
  supported modes/versions, auth limitations, manual recovery and tool effects.
  Keep machine output stable. Recompute the next minor (expected 0.8.0 from
  the original base); update both version literals and CHANGELOG together.
- [ ] Obtain independent whole-feature spec and quality/security review,
  including concurrency and untrusted inputs. Resolve substantive findings
  with incremental commits/re-review. Complete required human native/macOS/auth
  acceptance; skipped tests cannot replace these gates.
- [ ] Integrate current local/fetched remote main without rewriting. Stage final
  progress before validation. Run `./tests/run.sh --ci` with Stow/zsh prerequisites,
  `pre-commit run --all-files`, and `git diff --check`. If local prerequisites
  are absent, record local-suite/skip results; hosted Linux/macOS CI is mandatory.
  Commit the validated candidate.
- [ ] Run `pre-commit run --hook-stage manual gitleaks-history`, clean-tree checks
  and `KIT_RELEASE_TAG=vNEXT_MINOR tests/config.sh -k ReleaseVersion` against
  committed HEAD with the actual version. Do not create a tag. Record SHA/results
  in the ledger; any tracked change creates a new candidate.

## Review focus and node protocol

Review bypass-prone inputs: inherited enabled policy with unsupported mode
(C/X/A), account/refresh/family races (Q/K/X/A), detached/IPC/network escape
(N/K), buffered continuation/background recovery (K/X/A), and missing files
or changed environment in copied installs (I/R/V).

Reviewers receive requirements, base/head or frozen tree, full diff, checks and
limitations. They are not the implementer. Fix important findings before
integration. Use deliberate staging and atomic commits without AI attribution.
Only the coordinator writes central handoff/ledger. Update after each accepted
node; preserve incomplete work as incomplete. Keep a process owner active until
checks finish and record the exit status; interrupted checks are not PASS.

After integration/validation, confirm no worker/fixture process uses its
worktree, inspect status/diffs/untracked files, archive useful evidence and prove
the worker head is an ancestor. Then use `git worktree remove EXACT_PATH` and
`git branch -d EXACT_BRANCH` from a checkout containing it. Never force cleanup
or remove another task's checkout. Retain integration until final publication.

## Mandatory two-cycle completion

### CI1/M1: publish the functional candidate

- [ ] Push the recorded `ci/**` branch normally. Find the exact branch/SHA's
  `ci.yml` run with GitHub tools or `gh run list --workflow ci.yml --branch
  BRANCH --commit SHA`. Require every Linux/macOS job, history scan and clean
  checkout check green. Record URLs/results only in the ledger; recording CI
  must not itself change the tracked candidate.
- [ ] Fetch again. Current local main and origin/main must both be ancestors of
  the candidate. If either advanced, integrate/review/test/CI the new candidate.
  Check main checkout clean and not owned by another writer; fast-forward main
  to the exact green SHA and push normally. A push race requires revalidation,
  never force. Verify main CI for that SHA is green too.

### D/CI2/M2: remove task documents, validate and publish again

- [ ] After M1, ensure lasting behavior/limitations live in permanent docs.
  Archive research, approvals, reviews and CI links in the common-dir ledger;
  record the functional SHA for recovery with `git show SHA:PATH`.
- [ ] Derive exact task-owned deletion targets from the ledger: this handoff,
  research reports and disposable probe/evidence files. Migrate any required
  regression fixtures into permanent tests first. Delete exact tracked files
  with apply_patch; do not delete directories broadly. Keep the permanent
  design spec/product docs and repair every link/reference.
- [ ] Review cleanup's entire diff; repeat required local suites/hooks/history
  and release checks against the new cleanup commit. Push to `ci/**` and require
  all jobs green for this new SHA. Old functional CI cannot certify deletion.
  Repeat all M1 fetch/ancestry/clean-checkout/concurrency checks, fast-forward
  main, push, and verify main CI again. Fixes require another candidate.

### Z: release-ready verification and task cleanup

- [ ] Local main equals origin/main at the final reviewed SHA; all final CI
  jobs pass, versions match and status including untracked files is empty.
- [ ] Confirm task worktrees unused/clean/integrated; remove them and merged
  branches, then only this task's remote ci branch. Remove exact verified task
  temporary outputs. The preexisting `.worktrees/` local exclude is not ours
  to delete; retain the durable audit archive and unrelated worktrees.
- [ ] Report final SHA/version, reviews, tests/skips, CI links, demonstrated
  capabilities/limitations, approvals and cleanup. No tag was created. Do not
  claim release-ready with unresolved gates, dirty/unsynced main or one cycle.

## Next-session entry

Read AGENTS.md, recover integration/ledger, then read spec, PN and PO. Reconcile
the pending access-bearer reply and dispatch independent Q0/N nodes under
ultracode. Reuse accepted evidence; raise specific gaps immediately. Ordinary
implementation choices stay with the implementer; human auth/G0 decisions
remain with the user.
