# PC4: native streams on monitor and gate process loss

Status: **bounded loss evidence complete; full A and G0 remain blocked**.
This Linux experiment forwards native root/title and default delegated model
requests through one owned gate process to a distinct loopback fake upstream.
Monitor transport EOF, monitor process death and heartbeat expiry latch the
gate closed and disconnect every already owned client/upstream stream.
Quota recovery and a later heartbeat do not reopen it. Killing the gate also
closes its transports. Native UI/history access survives these experiments,
but a dead endpoint leaves native turns in progress during the observation
window. No production runtime, continuation UX or transport policy is added.

Base: `ab417382602dd09142b7048ba4743c49b4ed2472` (reviewed PC3).
Native: `codex-cli 0.167.2`, Linux x86_64, SHA-256
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b`.
Captured November 12, 2026 UTC. All accounts, tokens, quota, prompts and model
responses are fabricated. No actual credentials, remote provider or real
quota observation is used. PC, PC2 and PC3 files remain unchanged.

## Experiment and ownership

The fixture adds a small standard-library HTTP/SSE relay and monitor process,
not a reusable production gateway. Model admission begins closed; a synthetic
POST before native startup receives 503 and produces no upstream request.
The native model-free quota RPC then returns both fabricated passing windows.
Only afterward does the harness start a separate monitor, which sends one-byte
heartbeats through an inherited socket. The experiment lease is 0.8 seconds,
with a 20 ms watchdog poll. These timings are not product recommendations.

A single lock covers lease checks, initial upstream connection/send and active
client/upstream socket registration. The watchdog latches closed and shuts
down all registered sockets under that lock. Admission checks also enforce
expiry, so a delayed watchdog alone cannot authorize a new expired request.
The latch has no reconnect or continuation path. A heartbeat after a latched
loss is recorded and ignored. Local `/models` metadata returns an empty list;
the relay forwards only the fixed `/backend-api/codex/responses` route to its
fixed loopback upstream. It does not forward OAuth, CONNECT or WebSockets.

The native settings, sourced discovery shape and fabricated SSE spawn events
come from [PC3](quota-reserve-codex-ui-research.md). The native provider selects
ChatGPT authentication and sends fabricated bearer/account headers through the
relay. Root, child and grandchild genealogy is established by completed native
`collabAgentToolCall` items, not manually created child threads. The TUI's title
request is classified by its native title prompt. The unrelated control is a
second native app-server with separate temporary HOME, CODEX_HOME, Unix socket
and direct fake-provider endpoint. Its held model stream remains active and
its native turn remains `inProgress` throughout each loss observation.

## Four distinct losses

| Case | Native work already forwarded | Trigger and observed result |
| --- | --- | --- |
| Monitor transport EOF | Root and native title | Monitor closes only its gate socket and stays alive; gate observes EOF, latches and closes both streams |
| Monitor process death | Root, child and grandchild | SIGKILL to the owned monitor produces EOF; gate closes all three held streams |
| Lease expiry | Root, child and grandchild | Monitor stays alive with the socket open but stops heartbeats; gate expires the lease and closes all three streams; a later pulse is ignored |
| Gate process death | Root and native title | SIGKILL to the owned relay closes its listener and transports; both fake upstream streams disconnect |

All four cases queue a native prompt before triggering loss. The native backend
automatically consumes that queue after the original stream fails. In the
three live-gate cases, recorded requests containing the old queued prompt and
the later programmatic prompt reach the closed gate and receive 503; neither
reaches the fake upstream. An explicit `thread/queue/start` then reports
`queue is empty`. This was initially a failed harness expectation and is
preserved in the ignored audit cache. It is evidence that automatic queue
dispatch matters, not evidence of deliberate human continuation.

After loss, the harness performs another successful fabricated native quota
read and starts a new programmatic thread/turn. Upstream request counts remain
unchanged. With a live closed gate, the observed turns are `failed`. With the
gate process dead, both the automatically queued root turn and the new
programmatic turn remain `inProgress` after the three-second observation.
This is bounded absence of upstream admission, not proof of eventual failure,
retry exhaustion, stable paused state or a permanent no-work guarantee.

Native subscription TUI cases remain alive after EOF and gate death. Native
RPC history reads succeed, and the existing Ctrl-C exit shortcut returns zero
and restores termios. No new continuation key is implemented. RPC history
readability and an attached live TUI do not establish every keyboard/history
browsing operation or a polished paused-state user experience.

## Timing and evidence limits

The frozen run measured these intervals from the harness loss trigger:

| Case | Gate latch or confirmed process exit | All fake-upstream disconnects observed |
| --- | --- | --- |
| Monitor transport EOF | 0.2 ms | 90.2 ms |
| Monitor process death | 1.1 ms | 91.6 ms |
| Lease expiry | 716.4 ms | 814.5 ms |
| Gate process death | 2.9 ms (process exit) | 93.1 ms |

Lease expiry is measured from the command to stop sending heartbeats, not from
the last heartbeat. Its remaining lease at that command depends on heartbeat
phase, so 716.4 ms is compatible with the 0.8-second experiment lease and must
not be presented as a new lease duration or a maximum detection bound.

Every loss trigger, gate latch/exit and fake-upstream disconnect is timestamped
with the same host monotonic clock in the
[case evidence](quota-reserve-probes/pc_loss/evidence/). The gate timestamps
are created inside the gate, before its stdout reader receives them. Upstream
disconnect detection uses a 50 ms heartbeat writer; detection timing therefore
includes polling, write buffering and scheduler delay. The final report's
`all_owned_closed` event records the harness observation, after every expected
fake-upstream stream has reported a socket error.

Closing the client/upstream TCP connections is not evidence that a real remote
provider cancels computation or billing. Admission may already have happened
before loss detection; in-flight bytes and requests can cross that boundary.
The lock establishes a local fixture ordering, not zero overshoot or a latency
guarantee. There is no authenticated monitor protocol, monitor restart policy,
production supervisor, crash recovery or restart/rebind test here. Gate death
means an OS process exit, while monitor EOF may occur with its process alive;
neither is merely the earlier PC2 controller-RPC disconnect.

Successful WebSockets, voice, arbitrary role/profile/provider/account changes,
compaction, goals, alternative model families, running local tools and macOS
remain untested. The inherited network guard independently denies non-loopback
native networking; a rejected external attempt is not evidence that a model
gateway alone blocks all alternate routes. Native successful subscription
auth selection under fabricated credentials does not establish real account
permission, current quota semantics, refresh or storage correctness. Manual
continuation and HTTP-only product restrictions remain pending user decisions.
Full A and G0 remain blocked.

## Reproduction and validation

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_loss/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_loss/check_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_loss/test_evidence.py
pre-commit run --all-files
```

The runner reuses the unchanged PC loopback/Unix-only Linux network guard and
self-check. Guard self-check is bounded to ten seconds; each native case to
50 seconds. Each outer subprocess owns its tracer, and the guard retains
EXITKILL behavior and the prior documented trusted-binary/sockaddr TOCTOU
limitations. Native children have sanitized environments, private temporary
homes and closed inherited file descriptors. Only the intended monitor socket
is passed to the corresponding Python fixture process. Cleanup kills and waits
for owned process groups; no unrelated process or real daemon is signaled.

The manifest binds the fixture sources and sanitized case JSON by SHA-256 and
records every stage exit. The offline evidence checker requires all four
native cases, complete native genealogy, exact closed-stream sets, post-latch
queue/programmatic rejection, no later forwarding, ignored heartbeat recovery,
and surviving independent native sessions. Native absence prints a skip and
does not constitute capability success. Failed runs, exit records and exact
temporary-root inventories survive in ignored `.cache/pc_loss/`; the coordinator
archives them before removing this worktree. Tests and documentation add no
public interface and modify no runtime modules.

The final native capture passes the guard and four case stages. Seven offline
checks verify the valid capture and reject missing stream closure, wrong
native genealogy, heartbeat reopening, missing old-queue rejection, forwarding
after the latch, and a failed native stage. The earlier empty-evidence check
failed as expected before the experiment was implemented.
