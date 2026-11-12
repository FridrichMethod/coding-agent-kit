# PC: Codex quota reserve capability evidence

Status: **blocked-user**. The accepted full option A has no demonstrated native
persistent dispatch gate. Do not begin the Codex runtime adapter or count G0 as
passed. This is an evidence result, not a released feature or a proposal to
weaken the agreed semantics.

Base: `0a79911ea0039ac8ab33fe8a7d13f0f0cae6fdcb`. Research date: 2026-11-11.
Tested Linux x86_64, `codex-cli 0.167.2`, binary SHA-256
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b`.
No real credentials, real model requests, account enrollment, or shared daemon
were used. macOS was not tested. Original probe scripts remain unchanged.

## Main finding

The private foreground App Server and native remote TUI work together. Another
client can interrupt an active turn. Neither that interruption nor a paused
goal prevents future model dispatch. Controller loss leaves the backend able
to accept work. The native protocol and official documentation did not expose
a session-wide, fail-closed dispatch gate that survives controller loss while
preserving the native interface.

The current experimental schema lists 180 client methods. Relevant methods are
`turn/interrupt`, goal mutations, queue operations, and archive/unsubscribe;
none establishes the required dispatch lease. Goal mutation has
`origin: user|automatic`, but that caller-supplied field authorizes goal changes,
not every model request. The normal `turn/start` schema contains no equivalent
verified human-resume event. This is bounded discovery in the tested version,
not proof that every possible future native interface is impossible.

Official references checked after local inspection:

- [App Server API](https://learn.chatgpt.com/docs/app-server): interruption ends
  a current turn; goal state is separately persisted. Unsubscribe is a
  subscription change with idle unloading, not a dispatch lock. Archive moves
  history and only attempts descendant archival. Those mechanisms do not
  establish the required persistent pause.
- [Hooks](https://learn.chatgpt.com/docs/hooks): model-request hooks only report
  diagnostics; their output cannot block requests. `SubagentStart` cannot stop
  subagent creation. `UserPromptSubmit` exposes prompt and turn fields without
  establishing a trusted distinction between human continuation and queued or
  background work.

## Native control observations

Raw sanitized terminal and protocol evidence is in
[pc/evidence](quota-reserve-probes/pc/evidence/).

| Experiment | Observed result | Scope of conclusion |
| --- | --- | --- |
| Preserved OS SIGINT probe | Native exit `-2`; no interruption banner | Exits; fails the UI requirement |
| Preserved PTY Ctrl-C probe | Native remains alive; interruption banner | Interrupts the active turn only |
| Preserved shared-backend probe | Interrupt RPC succeeds; TUI alive; new prompt after controller disconnect sends another Responses request | Controller loss does not prohibit dispatch |
| Goal paused, queue retained, active turn interrupted | Goal read reports `paused`; TUI says `Goal paused (/goal resume)` | Goal state does not establish a general lock |
| Controller closes its socket | Request count remains 2 during a bounded idle observation | No automatic dispatch during that short interval; not fail-closed proof |
| Separate fixture driver calls `thread/queue/start` for the old queued message | Count increases 2 -> 3 with the goal still paused | Queued work bypasses the candidate gate without a verified manual continuation |
| `thread/resume`, then programmatic `turn/start` | Goal still paused; count increases 3 -> 4 | Same-thread resume plus new programmatic input also bypasses it |
| Native `/status` entered after interruption | Full status card renders; model, session and remote server shown; count unchanged | Native UI remains usable after current-turn interruption |
| Owned backend killed during active work | Both held streams disconnect; TUI alive after 0.8 seconds; count remains 4 | Backend crash stops its requests but is not a persistent usable pause architecture |
| Two Ctrl-C bytes after backend crash | TUI exits 0; PTY termios exactly matches prelaunch state | Terminal restoration observed for this exit path |

The control run's four requests were: initial user prompt, automatic native
task-title generation, old queued prompt, and programmatic post-resume prompt.
The title request began independently of the main turn. Disconnect events are
anonymous: their ordering suggests its held stream survived root interruption
until backend death, but the log does not directly identify that stream. The
title request is counted separately from the interrupted root request, rather
than misclassified as a retry or ignored.
Requests 3 and 4 are new dispatches, not pre-pause in-flight requests.

The final preserved shared-backend run captured three Responses requests; its
request log does not store bodies, so it cannot classify the extra request.
The targeted fixture captures input bodies and identifies title generation.
An artifact check that assumed exactly two requests was corrected to allow
additional native work while still requiring the post-disconnect dispatch.

The preserved probes' `usable_after_status` heuristic returned false. Their
single write of `/status` plus Enter was insufficient evidence of UI behavior.
The targeted fixture separates typing from Enter to accommodate native paste
detection and records the complete rendered status card. It also uses a
single-threaded exec helper for controlling-terminal setup, avoiding the
preserved scripts' multithreaded `preexec_fn` hazard.

A preliminary targeted run waited five seconds for SIGTERM shutdown while held
requests remained and hit its bound. This was harness failure, not successful
backend-failure evidence. The final experiment explicitly crashes only its
owned backend with SIGKILL and records the resulting terminal behavior.

## Model-free quota fixture

The final `quota` run creates no threads or turns and observes **zero** Responses
requests. A fabricated native file credential selects `fixture-account-a`; the
native usage HTTP request includes that account header. An isolated loopback
service supplies the response to `/api/codex/usage`.

| Fixture case | Native result |
| --- | --- |
| Integer windows | Five-hour duration 300 minutes, used 25; weekly duration 10080 minutes, used 60; reset timestamps preserved; family key `codex`; response `accountId=fixture-account-a` |
| Immediate identical reread | One new HTTP usage request, same values; no model request |
| Fractional `used_percent=25.5` | Decode error; native expects `i32`, not a fractional percentage |
| Response account changed to `fixture-account-b` while request uses account A | Successful RPC retains account B in `accountId`; consumers must reject mismatched attribution |
| HTTP 503 after successful reads | Error, not a successful cached quota reply; one attempted usage HTTP request |

This proves the native parsing and no-model query path against fabricated local
inputs. It does not prove server permissions, real quota precision, or real
account validation. The fractional fixture establishes an input restriction
of this native version, not that the real provider sends fractions or silently
rounds them. Preserve the policy's unrounded comparison requirement; do not
invent extra precision unavailable from the native API.

The tested repeated reads each contacted the fixture. The response schema has
no observation timestamp or cache-origin field. Do not generalize these two
reads into a version-independent freshness guarantee or refresh a watchdog
solely because an RPC returned successfully. The test deliberately follows
successful observations with a failed HTTP read; native failure was observable.

Only the `codex` family was exercised. Multiple family mapping, missing windows,
window resets, production freshness, and account-switch races remain unproved.
`account/read` attempted native workspace-routing discovery, which the minimal
fixture rejects; it returned `workspace routing discovery failed`. Therefore
startup account verification is not demonstrated end to end, despite explicit
account identifiers in usage requests and replies. No fake claim of production
authentication success is made.

## Full A capability matrix

| Required capability | Result |
| --- | --- |
| Model-free query and local window parsing | Demonstrated locally; real authorization untested |
| Verified selected account and applicable quota family | Partial: request/reply IDs visible; mismatches retained; account discovery and family changes unproved |
| Observation freshness | Each tested read fetches; no native freshness metadata or universal guarantee |
| Current root turn interruption with usable native UI | Demonstrated |
| Persistent prevention of future owned dispatch | **Failed**: queued and programmatic counterexamples |
| Fail-closed controller loss | **Failed**: original TUI and targeted queue counterexamples |
| Root/child/grandchild activity and ownership | Root and native title activity observed; delegated child/grandchild matrix blocked by absent persistent gate |
| Goal continuation across pause/recovery | Paused goal state exercised; ordinary dispatch bypasses it; full automatic goal continuation blocked |
| Verified manual continuation after fresh quota | **Not established**; `/goal resume` is goal control, not a reserve gate |
| Background reports versus manual continuation | Not established; no general origin guard |
| In-session identity/session changes and concurrent accounts | Same-thread RPC resume only; identity switches and full TUI session switching blocked/unproved |
| Already-running local tools and cancellation | Not exercised; native interruption text says tools may survive, but that text is not empirical proof |
| Backend failure and terminal restoration | Crash and clean TUI exit observed; reconnect/history usability after failure unproved |
| Linux/macOS behavior | Linux only |

The coordinator requested stopping deeper descendant exploration once the
persistent-gate counterexample was concrete. Those cells remain blocked; they
are not silently skipped or reported as passes. Running tools, in-session
switches, full descendants and trustworthy resume would still require native
conformance work if a new gate becomes available.

## Containment and reproducibility

The research-only [network guard](quota-reserve-probes/pc/network_guard.py)
uses Linux x86_64 seccomp/ptrace to allow Unix sockets and IPv4 TCP to literal
127.0.0.1. Other socket families/types, external connect destinations,
sendmsg/sendmmsg and io_uring network paths are denied. Descendants inherit the
filter; ptrace EXITKILL prevents them from surviving tracer death. Only tracked
child PIDs/process groups are cleaned. It does not enumerate or signal native
processes belonging to the user.

This guard is for trusted binaries with fixed fabricated fixture configuration,
not adversarial sandboxing: pointer-based sockaddr inspection has a TOCTOU
limitation. It blocks some Unix sendmsg behavior too; findings are explicitly
limited to this controlled harness. No application runtime imports it. A
private network namespace was unavailable (`unshare -Urn` failed writing
`uid_map`), so proxy variables alone were not accepted as containment.

Before native runs, the guard self-check demonstrated loopback TCP and Unix
socket traffic, denied external TCP/UDP and IPv6, and denied external TCP in
an exec descendant that created a new session. Each native research subprocess
has a 40-45 second timeout owned directly by its tracer supervisor; no unguarded
intermediate reproducer process sits between that timeout and the tracer. A
focused test kills a tracer with two running owned tracees, including a child
in a new session, and confirms both cease running within the cleanup bound.
No hang or setup failure counts as behavior. Failed preserved probes save
their artifacts/root inventory and propagate a nonzero result.

Run the contained, bounded suite from the worktree root:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc/check_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc/test_supervision.py
pre-commit run --all-files
```

The runner skips clearly when Codex is absent. It prints exact temporary roots
for later canonical cleanup and writes sanitized logs into `pc/evidence/`.
Native temporary homes are intentionally retained until evidence review;
`.cache/pc/` contains local schema/inspection scratch. Neither is the only copy
of useful evidence. Temporary-root inventory is reported to the coordinator.

## Progress and next decision

- Read the accepted spec, handoff brief, launcher modules and native sharing
  tests; verified base and clean task worktree.
- Reproduced preserved signal/shared-backend outcomes under explicit containment.
- Generated native experimental schema; checked official API/hook documentation.
- Rejected paused-goal/interrupt/controller candidates with concrete native
  queue and programmatic continuation counterexamples.
- Completed model-free local quota parsing and mismatch/error/fractional cases.
- Focused artifact checks: five checks passed; optional native-binary absence
  returns an explicit skip. The complete contained research runner passed.
- Required pre-commit: the initial and final reviewed-fix full runs passed.
  The final reviewed tree was committed as `9c660f8`; validation logs and
  exit statuses are archived with the coordinator handoff.
- Independent review accepted the blocker conclusion and requested supervisor
  failure/timeout fixes plus evidence/path wording corrections. Focused tests
  reproduced both supervision defects before the fixes; the fix round removes
  the unguarded parent process and propagates preserved-probe failure. Scoped
  re-review accepted all four fixes. Three supervision tests and five
  evidence checks passed; the native rerun and final hooks also passed.

G0 needs a user decision: retain full A and wait for a native fail-closed
session/descendant dispatch gate, or explicitly reopen the scope. No request
proxy, replacement UI, persistent daemon, process-exit fallback, or weakened
stop guarantee has been implemented or approved here.
