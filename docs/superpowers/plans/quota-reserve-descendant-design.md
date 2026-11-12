# PO: invocation-owned descendant boundary

Status: finite architecture exploration complete; **full A and G0 remain
unproved**. This report changes no runtime. It supplements the
[accepted requirements](../specs/2026-11-11-account-quota-reserve-design.md)
and [implementation handoff](2026-11-11-quota-reserve-handoff.md).
Research date: November 12, 2026; base `f0158df`.

## Decision and remaining user choice

Recommend, conditionally, an outer per-invocation OS confinement boundary
around the entire native client/backend and its local descendants, with one
lease-controlled network exit. Keep the existing model-aware gate for quota,
credential binding and manual continuation. A network sandbox alone supplies
none of those semantics. Select a concrete adapter only after finite platform
tests and G0 review; this report does not select or install a library.

The user explicitly includes shell/tool-launched `claude`, `codex`, SDKs and
other model clients in full A, as well as native agents and self-spawns.
Disabling shell subprocesses or relabeling them independent jobs is not an
acceptable substitute. This supersedes narrower options in the routing audit.
The user has now accepted optional platform isolation dependencies for protected
mode. Missing or unproved capability must refuse protected startup. This
resolves the original no-extra-runtime constraint for this mode; do not ask
again. No portable standard-library-only solution was demonstrated.

The concrete component and its compatibility cost remain for finite tests and
G0 review. Dependency approval does not approve installation in this research
turn, privileged services, system firewall changes, or an exemption from G0.

## Ownership and boundary semantics

Ownership begins before the first native process can dispatch model work.
It includes descendants through fork/exec, shells, nested native clients,
hooks, local MCP/plugin helpers, ordinary native relaunch, and work this
invocation explicitly adopts. `setsid`, double-fork or reparenting does not
make work independent. Inherited kernel restrictions are a better boundary
than a periodically sampled PPID tree or process group.

Separately launched user jobs remain outside the boundary, even on the same
account; do not stop them by process name, UID or credential. Defending against
a malicious unrelated same-UID attacker is not required. Normal IPC delegation
to an existing daemon is still relevant: it can escape ownership without an
attacker. Docker sockets, SSH control masters, desktop launch services and
remote job queues are concrete examples, not automatically permitted exits.

Local detached children must retain their boundary and loss latch. Adopting
an already running process cannot be assumed to retrofit confinement. Remote
work can continue after its local connection closes; safe adoption requires
remote admission/cancellation ownership, not just a local proxy. Until proved,
refuse those launch/adoption modes before model work and expose the limitation.
Do not forbid ordinary shells wholesale. If transparent remote delegation is
required, the proposed local boundary is insufficient and G0 stays closed.

## Evidence: what is established and what is not

Labels: N = prior native observation; S = inspected source/documentation;
I = conditional inference; U = unproved integrated behavior. Sources establish
mechanisms, not that this product works. No new native matrix ran for PO.

| Evidence | Established fact and limit |
| --- | --- |
| N: [PC5](quota-reserve-codex-continuation-research.md), [PC4](quota-reserve-codex-loss-research.md), [PA1C](quota-reserve-claude-control-research.md) | Native UI, default owned streams, loss latches and trusted-terminal continuation have bounded evidence. Arbitrary descendant egress was not proved. |
| N/S: [PR routing audit](quota-reserve-routing-audit.md) | Inline Claude settings close the tested env-reload escape; provider pins route tested native traffic. Shell clients can resolve their own endpoints. |
| N: [PC6](quota-reserve-codex-realtime-research.md), [PA1T](quota-reserve-claude-tools-research.md) | Reuse the actual realtime and running-tool results and limitations; do not repeat them as a new matrix or infer OS confinement from them. |
| S: [Claude sandbox documentation](https://code.claude.com/docs/en/sandboxing#what-runs-outside-the-sandbox) | Built-in sandbox scope is shell tools and their children. Hooks, MCP and other native activity run outside; excluded commands and retries matter. Its documentation recommends an outer boundary to cover the process itself. |
| S: [Codex sandbox helper](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/linux-sandbox/README.md) | Pinned source describes network namespaces and a managed proxy bridge. Tool sandboxing does not prove the parent TUI/backend is confined. `codex sandbox` is a candidate outer entry point, not an accepted cross-provider adapter. |
| S: [Codex proxy](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/network-proxy/README.md) | Limited HTTP-method policy needs MITM for encrypted CONNECT traffic. Domain filtering is not model-request classification. |
| S: [Anthropic sandbox runtime](https://github.com/anthropics/sandbox-runtime#platform-support) | An arbitrary-process wrapper uses Linux bubblewrap and macOS Seatbelt; Linux also needs relays/helpers. It is an optional dependency candidate, not a standard-library feature or a proved quota controller. |

Codex source was reused from PR's archived release tree, commit
`ce21072c90d32c40af7a55b60b2fb35c84a43a6a`, with provenance in
[PR sources](quota-reserve-probes/routing_audit/sources.json). New primary
documentation was read on the research date; moving upstream documentation
must not be mistaken for evidence about the pinned installed executables.
The research syscall guard remains containment only, with its recorded limits;
it is not a production implementation or proof of descendant protection.

## Candidate comparison and platform cost

| Candidate | Coverage and disposition |
| --- | --- |
| Native URL/env/PATH pins and process-wrapper hooks | Useful cooperative routing. An absolute executable, SDK or cleared environment bypasses that convention. Process-name killing misses dispatch races and harms unrelated jobs. Retain pins as integration aids, never the enforcement boundary. |
| Built-in command sandbox plus native route gate | Covers some shell descendants, but leaves root/native-helper gaps and configurable escape paths. Could qualify only after proving every relevant native exit and immutable confinement; not the minimal robust cross-provider candidate today. |
| Outer per-invocation OS boundary plus gate | Recommended conditional direction. Establish restrictions before native startup; allow only controlled exits, retain native TTY, close streams on pause/loss, preserve independent sessions. Actual composition and platform support remain U. |

On Linux, prefer a reviewed existing namespace/seccomp helper over a new
Python syscall/ptrace implementation. Bubblewrap's
[upstream design](https://github.com/containers/bubblewrap#sandbox-security)
provides primitives; the caller remains responsible for policy and IPC holes.
User-namespace availability, container nesting and host policy can prevent
startup. The Codex package may bundle bubblewrap, but its presence and usable
outer proxy interface are U; requiring Codex for Claude-only installs adds a
dependency. Do not install tools, change sysctls/AppArmor, or fall back silently.
[Landlock](https://www.kernel.org/doc/html/latest/userspace-api/landlock.html)
inherits restrictions and has ABI-dependent network controls; it does not by
itself provide this dynamic cross-platform quota gate. Port rules are not
provider/credential or request classification.

On macOS, Seatbelt through an existing wrapper is the finite candidate;
the [runtime documentation](https://github.com/anthropics/sandbox-runtime#how-it-works)
describes proxy-port confinement. Availability, nested native sandboxing,
Unix IPC, PTY/history access and native credential-store access need local
proof. A CLI profile is not a promise of a stable Apple SDK contract.
No macOS run occurred here. A Node/npm wrapper adds a runtime dependency to
the launchers even if macOS already has its OS primitive. Reimplementing that
wrapper would transfer policy and maintenance costs to this small project.

WSL2 follows Linux only if its actual kernel/namespace checks pass. The pinned
Codex helper explicitly rejects the required bubblewrap path on WSL1. Do not
claim all WSL support from a Linux result; native Windows remains unsupported.
Any privileged helper, system filter or persistent service is a separate
reviewed architecture, outside this recommendation and current authorization.

## Minimum enforceable composition and user impact

1. Before launch, close inherited network/control descriptors and confine the
   entire invocation; permit only reviewed IPC needed for native operation.
   Ordinary credentials remain native-owned. Block host-daemon escape paths,
   including local/abstract sockets and platform launch facilities as relevant.
   The runtime documents [IPC escape risks](https://github.com/anthropics/sandbox-runtime#security-limitations);
   granting a local socket is not inherently harmless. Prove required exceptions.
2. Keep one model-aware exit whose admitted request binds to the selected
   account, actual bearer, quota family and fresh observation, as PR requires.
   Supported child clients can use that same exit; unknown direct clients must
   receive a clear refusal instead of unrestricted network access. This preserves
   shell execution but does not promise every SDK works unchanged. Review that
   visible restriction before adoption; no silent narrowing of full A.
3. Deny opaque tunnels to model-capable/unknown endpoints unless an equivalent
   enforceable binding is proved. A domain allowlist, CONNECT proxy or TLS SNI
   cannot distinguish `/usage`, auth refresh and inference sharing a TLS host.
   Do not solve this by unconditional provider-host exceptions or TLS interception.
   A small explicit model-free metadata path may live outside the sandbox only
   after exact method/endpoint/credential semantics and absence of a generic
   relay capability are reviewed. Native refresh must remain native-owned.
4. Recommended default: pause closes all invocation network egress, including
   non-model streams, while keeping the local UI and computation available where
   native interruption permits. Disclose this cost in protected-mode startup and
   documentation. Git/package downloads, SSH, tests,
   remote notebooks and arbitrary HTTPS can fail or be interrupted. The minimal
   conservative candidate denies unclassified egress; any allowed opaque channel
   must be closed on pause/loss and cannot submit independent remote model jobs.
   Domain names alone do not prove that condition. Broader ordinary connectivity
   is a user-visible compatibility decision, not permission to weaken full A.
5. The trusted exit owns every admitted stream, including persistent HTTP,
   WebSocket and CONNECT streams if any are supported. Pausing closes existing
   streams as well as refusing new connections; never rely solely on a connect
   filter. No inherited upstream socket or host relay may bypass this ownership.
   Remote computation already accepted can outlive transport cancellation; retain
   the agreed bounded-observation/overshoot limitation, without promising rollback.
6. Monitor EOF, stale lease or controller failure must latch the exit closed.
   Exit-process death leaves the static OS boundary denying direct networking;
   bridge restart or port reuse must not reopen admission. Establish descriptor
   cleanup and invocation identity before allowing restart. Independent terminal
   inspection should survive; do not use whole-process-tree termination as pause.
   Recovery requires new deliberate continuation and a fresh bound quota result.

No process-tree sampler, LD_PRELOAD shim, PATH interception or system-wide
firewall is proposed. Pure local computation may survive the network latch,
but native interruption and any sandbox helper lifecycle can affect tools;
combine the accepted PC5/PA1T observations with one composition test before
disclosing behavior for long-running training or tests.

## Finite prerequisite DAG for the new Claude implementation session

```text
Accepted spec + PC/PA/PR evidence + latest user decisions
 |-- D: dependency permission accepted; select concrete adapter for feasibility
 |-- Q: attributable model-free quota / native-refresh binding proof
 D -> L: Linux/WSL2 outer-boundary feasibility
 D -> M: macOS outer-boundary feasibility
 L + M + Q -> G0: concrete scope, cost, interfaces and human architecture review
 G0 -> C: pure policy/config -> X/A/U: provider adapters and completions
 X/A/U -> I/R/V: integration, independent review, full checks
 -> original exact-SHA CI/main publication and scoped cleanup gates
```

L and M are finite feasibility nodes; this report installs no dependencies.
If blocked by host policy, record that platform unavailable and ask for the
smallest actual dependency/setup decision. No production node begins before G0.
Q is independent: isolation does not repair the separately reported setup-token
usage GET 403 or establish full-login quota permission. Do not retry a consumed
live permission, seed quota with inference, or reopen the accepted HAuth schema.

Use one small fake-server fixture per platform to decide the following:

| Check | Decisive pass condition |
| --- | --- |
| Boundary and ownership | Root plus shell SDK/native child, cleared proxy env, absolute path and detached child attempt direct IPv4/IPv6 and permitted-gate traffic. Direct attempts never reach the alternate listener; approved traffic works; unrelated external job remains usable. No research guard may be the cause of the successful product assertion. |
| Escape and persistent streams | A controlled host socket/daemon delegation attempt is denied; an already-open allowed stream cannot send after latch closure. Test inherited descriptor rejection and one ordinary relaunch/adoption attempt. Unsupported remote adoption refuses before dispatch. |
| Loss and continuation | Kill the monitor, then the exit in separate bounded cases; include a queued child retry and gate restart/port reuse. No new upstream model request; native history/TTY remains usable; only fresh verified continuation reopens the same bound invocation. |
| Identity and compatibility | A pending child request crosses bearer/account rotation: old quota never authorizes the new identity. Show one supported native refresh path and one ordinary git/package/tool workflow; disclose any blocked path. Reuse accepted native matrices rather than rerun all of them. |

Keep immutable: ownership, both independent quota windows, preflight, freshness,
loss closure, manual continuation, native UI and off-mode compatibility. Allow
node partition, adapter/tool choice, internal interfaces and measured timeouts
to change from test evidence. Review material architecture changes at G0;
do not require a speculative exact library or timing before feasibility.
The user selected a new Claude ultracode multi-agent/worktree workflow; discover
its actually available skill instructions there rather than inventing commands.
Give writers separate worktrees and reviewers read-only access.

## Validation and handoff boundary

PO adds this ASCII report only. Local link targets and `git diff --check` are
checked; required all-files hooks and independent review must finish before
its atomic commit. No prose-mirroring tests, product runtime, native model call,
credential operation, installation, firewall change or remote publication was
performed. Preserve the exact final commit and check results in the coordinator
ledger. This artifact completes exploration; it does not mark full A/G0 passed.
