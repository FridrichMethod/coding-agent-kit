# PC5: trusted-terminal continuation and native tool boundaries

Status: **bounded continuation evidence complete; full A and G0 remain blocked**.
A dedicated experimental terminal action can distinguish new continuation
from old native queues, quota recovery and native output under an explicit
trusted-terminal boundary. Real Codex TUI, title and default descendant work
were exercised behind an owned HTTP/SSE gate. This is research only: no
launcher, production proxy, account storage or default shortcut changes.

Base: `bb195717d901246eb51ce1b4fec02be7baec529a`. Native executable:
`codex-cli 0.167.2`, Linux x86_64; SHA-256
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b`.
Captured November 12, 2026 UTC. Authentication, quota, responses and account
identifiers are fabricated. Every native case runs under the unchanged PC
syscall network guard with private HOME, CODEX_HOME and workspace directories.
No real credentials, login, remote model service or real-account query is used.

## Input ownership and the research control

[control.py](quota-reserve-probes/pc_continue/control.py) is the shared small
terminal-control fixture available to the parallel Claude research. It owns
one raw outer terminal input fd. Ordinary input goes to the native TUI's PTY;
native output and local notices go in the opposite direction to the outer
terminal. Native output, RPC messages, hooks and model requests never enter
the continuation parser. The fixture exposes no network continuation route
and does not use an agent-supplied `origin` field as evidence of intent.

A pause first closes model admission and owned streams, then invalidates the
control epoch and drains pending outer terminal input. The experimental F12
sequence (`ESC [ 24 ~`) displays a new random eight-hex-character challenge.
Typing that challenge and Enter requests a fresh quota RPC. Repeated F12
while a challenge is displayed does not replace or confirm it. A challenge
is consumed on every check attempt; another attempt needs another challenge.
Bracketed paste cancels confirmation, including split markers and paste that
started before pause. A new pause invalidates any previous answer. A partial
escape prefix is retained for at most the next idle pump after 50 ms, so
ordinary Escape and other native keys remain usable.

The outer terminal is trusted to deliver normal input and correctly mark
bracketed paste; raw unmarked paste is indistinguishable from typed bytes.
The experiment does not claim physical-human attestation or resistance to an
arbitrary malicious process with the same UID and terminal access. Those are
not added requirements for this research. Terminal-emulator replies to native
cursor/device queries pass through the input path and cannot confirm the
challenge. Tests also write a valid key and answer to the output side of a
PTY and verify that neither triggers a quota check.

F12, the challenge format and notice placement are research choices, not a
proposed frozen product UX. The harness types the challenge through its outer
PTY to simulate a deliberate user action; it is not a real human acceptance
test. It then submits a new native turn by RPC to observe admission. The
shared component requires a serialized owner loop, a bounded fresh-check
callback and an owner-only grant callback. It is not a complete supervisor.

## Native observations

All three cases run the actual subscription-authenticated remote native TUI,
using the sourced PC3 discovery response and a custom provider with
`requires_openai_auth = true`, `supports_websockets = false` and no API key.
The gate forwards to a distinct loopback fake upstream. The first quota read
precedes every model request. Its passing five-hour and weekly observations
permit one initial research lease. The lease lasts 20 seconds; this fixture
value is not a product policy or maximum observation age recommendation.

| Case | Observation |
| --- | --- |
| Default delegation | Native spawn history proves root, child and grandchild. Root, title, child and grandchild have four active streams at pause, and all disconnect. |
| Buffered input | F12 written before pause is drained; no challenge, check or new grant occurs. |
| Held key and paste | Repeated F12 creates no grant; pasting the displayed answer creates no check or grant. |
| Old native queue | Previously queued input automatically dispatches after stream failure and receives 503. It never opens the gate. |
| Quota recovery | A successful native quota RPC while paused does not grant a lease or forward work. |
| New confirmation | Each new typed confirmation triggers a new native model-free quota read. HTTP 503, injected stale receipt time, five-hour equality and weekly equality all remain paused. |
| Both windows pass | One new confirmation grants exactly one healthy lease, and a subsequent native turn reaches upstream. |
| Second pause | Replaying an earlier confirmation grants nothing. |
| Native UI | `/status` still displays the native account, provider, context and both quota windows while admission stays closed; history remains RPC-readable. |
| Terminal behavior | Ordinary Ctrl-C reaches native exit; native and outer termios restore. Owned SIGWINCH and a PTY resize preserve the live TUI. |

The research reserves are 20% for five hours and 30% for the week. Passing
fixture observations are 75% and 40% remaining. Separate equality cases use
20% and 30% remaining respectively. Native window durations are mapped as
300 and 10080 minutes, rather than relying on primary/secondary position.
The stale case deliberately assigns an old receipt time to a newly returned
native response. It tests adapter freshness rejection, not native cache or
server timestamp semantics. This fixture is not a general quota normalizer;
real account identity/family validation remains required by the spec.

The gate's lock covers lease state, upstream admission and registered sockets.
Every captured interval between pause and the successful second grant has no
forwarded request. Endpoint requests do not issue grants. The second grant
authorizes invocation work; the gate does not classify every later request as
human-origin work. Old queues and native wakeups cannot issue that grant.

## Already running local commands

Two cases return a fabricated native `exec_command` call. Its harmless command
writes `started`, sleeps four seconds, then writes `finished`, all inside its
private workspace. The started marker is required before pausing.

- With only gate closure, the command writes `finished` after the pause.
  Its later model request and the old queued prompt are rejected. Network
  admission control therefore does not imply local-tool interruption.
- With gate closure plus the native `turn/interrupt` RPC, the final capture
  reports the turn as `interrupted`, but the command still writes `finished`
  after four seconds. Native history records `commandExecution` as completed
  with exit code zero. An earlier capture had no finished marker during six
  seconds and no completed command item. That variation must not be turned
  into a termination guarantee: the final counterexample proves that turn
  interruption can leave a running local command alive. The precise native
  lifecycle race is unresolved. The old queue is retained instead of
  automatically starting after interruption; an explicit fixture
  `thread/queue/start` reaches the closed gate and is rejected.

The first interrupt experiment expected automatic queue dispatch and failed.
The retained failed capture documents why the final experiment distinguishes
auto-dispatch after stream failure from explicit old-queue dispatch after
native interruption. The first evidence checker also incorrectly assumed interruption guaranteed
no finished marker; the final native capture falsified that assumption. The
checker now requires marker/history agreement and records the counterexample.
No test weakens the requirement that old work cannot
unlock model admission. No unrelated session is signaled; independent-session
and monitor/gate process-loss evidence remains the earlier reviewed PC4 work.

## Remaining capability boundaries

The fixture gate rejects WebSocket Upgrade, CONNECT and an alternate POST
route. These are explicitly synthetic endpoint probes; they do not prove
that every native route reaches the gate. The current official
[gateway documentation](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway)
states that voice uses another route and that sign-in, MCP and plugins can
use other paths. The native fixture disables plugins/apps and web search;
its independent network guard blocks other outbound traffic. A future
protected mode must reject unsupported routes or prove their routing and
control. The syscall guard is research containment, not that product proof.

This work does not establish native voice disabling, successful WebSockets,
provider/profile/account/session switching, alternative model families,
compaction, image generation, goals or v2 delegation. Full A stays blocked
on these applicable routing/ownership gaps and real-account acceptance, not
on an invented physical-human attestation requirement. HTTP/SSE restriction
and dedicated-control research are authorized, but production adoption and
G0 remain separate decisions.

PC4's independent process-loss latch is reused as prior evidence, not silently
combined into a claim that this same-process continuation harness has a
production loss/restart protocol. Controller death, restart, lease renewal,
identity changes during checks, arbitrary signal forwarding, suspend/resume,
macOS and a polished terminal overlay remain outside PC5. Closing upstream
TCP transports still does not prove remote computation or billing stopped.

## Reproduction, coverage and evidence

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_continue/test_control.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_continue/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_continue/check_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_continue/test_evidence.py
pre-commit run --all-files
```

Nine PTY control checks cover buffered input, held keys, split paste, fresh
one-use confirmation, failed-check retry, old-answer replay, ordinary input,
output direction, terminal replies and Escape forwarding. Nine offline
checks validate captures and reject recovery grants, paused forwarding,
missing queue evidence, stale acceptance, surviving streams, false genealogy
unusable UI and disagreement between tool markers and native history. The native runner bounds the unchanged guard self-check to
ten seconds and each of three native stages to fifty seconds; native absence
is a skip, never capability success. An invalid or failed stage cannot pass
the offline checker.

The manifest binds fixture sources and sanitized evidence by SHA-256 and
records all stage exits. Native process exits, termios results, visible
terminal output, quota reads and forwarding events are captured. Exact
scratch-root inventories, failed captures and logs remain durably under the
worktree's ignored `.cache/pc_continue/` for coordinator archival before
worktree removal. Owned children are bounded and reaped. PC, PC2, PC3, PC4
and all runtime modules remain unchanged. This report's coverage list applies
only to these research files; no runtime coverage or public contract is added.
