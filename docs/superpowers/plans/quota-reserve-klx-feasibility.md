# KLX: Codex composed protected invocation on Linux

Status: **finite composition evidence complete on one Linux host; G0 and every
live-account question remain open**. One protected invocation composed the
reviewed F1 parts: the QX lease and gate admission, the NL L2a boundary, the
PC4 loss latch and the PC5 trusted-terminal continuation. It ran the real
`codex --no-daemon` TUI in a PTY with fabricated credentials, and every
decisive case 1-11 of the K brief passed against loopback fixtures. The
composition also exposed three design facts that the F1 parts did not show
alone. They change the recommended Codex rebind mechanism and add launcher
requirements, so they are G0 items (section "Composition findings"). No
product runtime changed. This is not a G0 pass and not production capability.

Base `af3aa65` (branch `ci/quota-reserve`), captured November 12, 2026 UTC.
Ubuntu 24.04.5 LTS, HWE kernel `7.0.0-42-generic`, x86_64, Landlock ABI 8,
`dev.tty.legacy_tiocsti=0`. Native `codex-cli 0.167.2`, binary SHA-256
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b` (the
binary QX, NL and PC used). Source facts refer to `codex-rs/` at the 0.167.2
release commit `ce21072c90d32c40af7a55b60b2fb35c84a43a6a`, from the PR
archive; KLX read that source and did not rebuild it. macOS and WSL2 are out
of scope (KM covers macOS; WSL2 stays U).

Labels: **N** native observation in this node, **S** inspected source or
documentation, **I** inference, **U** unproved.

## Provisional decisions used

The K brief's provisional decisions were applied as research defaults. Each
remains a G0 choice:

- Linux boundary: NL L2a (Landlock TCP connect to the gate port only, plus the
  seccomp filter that denies AF_UNIX `socket()`, datagram `socketpair()`,
  UDP, MPTCP and other families, io_uring and TCP Fast Open).
- Codex always runs `--no-daemon`; `--remote`, `resume`, `fork`,
  `remote-control`, profiles and user overrides of routing keys are refused.
- Codex binding: gate-observed correlation plus hold-and-rebind. KLX ran the
  brief's App Server read as the main variant and the QX "gate-initiated
  read" alternative as a second variant (case 7).
- Wrapper-originated upstream connections use the launcher's `HTTPS_PROXY`
  captured once at startup (fixtures: none, plus one case with a loopback
  CONNECT proxy, case 6).
- A same-identity rotation with a fresh passing check keeps admitted streams;
  any failed revalidation, threshold, monitor loss or stale lease closes all.

## Composed architecture

```text
 outside the boundary                                   inside the boundary (L2a)
 +--------------------------------------------+
 | launcher (trusted terminal owner)          |
 |  - owns the user's terminal, raw mode      |   PTY   +-----------------------------+
 |  - PC5 TerminalControl (F12 + challenge)   |-------->| codex --no-daemon TUI       |
 |  - holds the gate's listening socket       |         |  in-process App Server      |
 |  - starts gate and monitor                 |         |  tools, children,           |
 |  - controller reads: a fresh native        |  pipes  |  detached grandchildren     |
 |    `codex app-server` per read  ---------->|-------->| codex app-server (stdio)    |
 +----+-------------------+-------------------+         +-------------+---------------+
      | socketpair        | listening fd                              |
      | (labels, continue,| (inherited; the launcher keeps a copy)    | TCP to the gate
      |  status, rebind)  v                                           | port only
 +----+-------------------------------+   socketpair   +---------+    |
 | gate process                       |<---------------| monitor |    |
 |  QX Lease + K additions            |  (heartbeats)  | process |    |
 |  admission, route table, latch,    |                +---------+    |
 |  CONNECT allowlist, rebind         |<------------------------------+
 +----------------+-------------------+
                  | plain HTTP (loopback fixture; production: TLS to chatgpt.com)
                  v
            fake ChatGPT backend, TLS refresh host, static git/pip server
```

Startup order (N, every TUI stage): resolve the fixture policy; refuse on
version, arguments, environment or host preconditions; bind the gate port
(`127.0.0.1`, random ephemeral); start the gate with that descriptor; run the
model-free preflight through a fresh in-boundary App Server; refuse unless the
gate reports a fresh passing observation of both windows for the expected
account; start the monitor; start the TUI inside L2a in its own PTY. Every
preflight recorded zero model requests (checker).

Native configuration (`launcher.py`, injected as `config.toml` here; X would
inject `-c` overrides): `model_provider` at the gate with
`requires_openai_auth = true`, `supports_websockets = false`, zero retries;
`chatgpt_base_url = <gate>/backend-api/` (the QX path allowlist);
`experimental_realtime_ws_base_url` and
`experimental_realtime_webrtc_call_base_url` at the gate (PC6); file
credential store; web search, plugins, apps, memories and analytics off;
`sandbox_mode = "danger-full-access"`, because the boundary is the sandbox and
the native helper refuses on this host (NL). Only the TUI gets proxy
variables (the gate URL) and, for case 8a, `CODEX_CA_CERTIFICATE` (documented
enterprise CA knob, S `http-client/src/custom_ca.rs`). Controller readers get
neither.

The gate (`klx/gate.py`) is the QX `Lease` plus the requirements the QX report
assigned to K:

- Default-deny routes. While running it forwards the model-free reads native
  sent in QX (usage, reset credits, models, settings, accounts/check) and
  admits `POST /backend-api/codex/responses` through the lease. While paused it
  forwards only the usage read of the expected account. Once the monitor is
  lost it forwards nothing. Unlisted paths, other POSTs (memories, search,
  images, reset-credit consumption) and WebSocket upgrades are refused before
  any upstream byte.
- Lock to first byte: the upstream connection opens outside the admission lock;
  the lease is re-checked, the request bytes are sent and both sockets are
  registered under the lock. A latch shuts down every registered client and
  upstream socket under the same lock.
- A tick thread drives `Lease.tick()` and a heartbeat lease. Monitor EOF, a
  stalled heartbeat or launcher EOF latches that gate instance for good.
- A restarted gate starts latched (`gate-restarted`).
- Foreign-account requests latch `identity-changed` without forwarding.
- CONNECT is refused except to `auth.openai.com:443` when case 8a allows it,
  and then only while running.

## Composition findings (new in K; G0 items)

1. **A `--no-daemon` TUI cannot serve the controller's reads, and a long-lived
   controller App Server goes stale.** With `--no-daemon` the TUI uses an
   in-process App Server (S, `tui/src/lib.rs` `InProcessAppServerClient`), so
   nothing outside can send it `account/rateLimits/read`. A second App Server
   keeps the bearer it loaded: `AuthManager` "will NOT" observe external
   credential changes "until `reload()` is called explicitly", and in the App
   Server only login and logout call it (S, `login/src/auth/manager.rs`,
   `account_processor.rs`). N: after the TUI refreshed, a long-lived reader
   started before the refresh still sent the revoked bearer (upstream 401),
   and its read moved the gate's epoch away from the TUI (both rotation
   stages). The composition therefore uses a **fresh in-boundary App Server
   per controller read**. It loads the current `auth.json`, runs with no proxy
   variables, and its own refresh attempts fail inside the boundary (N:
   `Failed to refresh token: error sending request` in reader stderr, and no
   reader refresh reached any fixture).
2. **A disk-reading rebind can bind only the bearer on disk.** In one
   development run (N, not retained in the committed evidence), the server
   revoked A1 while the turn and the title request were both in flight. Both
   got 401 and each refreshed, so native held two new bearers, A1r9 in the
   turn and A1r12 in the title, and the file held A1r12. The controller rebind
   read A1r12, so the turn's A1r9 request was refused after the 5 s hold and
   that turn failed. In the committed capture native refreshed once and both
   variants carried the turn across the refresh. The **gate-read variant**
   (the gate sends one usage GET upstream with the held request's own
   `Authorization` and `ChatGPT-Account-ID`) binds each bearer by its own read
   (N, `rotation_gate`). It needs no controller round trip and holds about a
   millisecond on loopback. KLX recommends it for Codex on Linux (I). Its cost:
   the gate becomes a usage client that uses the in-transit bearer, and the
   gate needs knowledge of the usage path (section "Costs").
3. **Recovering from an identity latch needs a binding read before the
   continuation is armed.** After `identity-changed`, the lease holds no
   credential. The check's own read therefore starts a new epoch, and the QX
   rule "any epoch change clears the continuation" uses it up (N, first
   rotation development run: switching back to A stayed paused). The composed
   continuation check reads once to bind the epoch, arms the continuation,
   then reads again (N, `switch_back` resumed in both rotation stages).
4. **The monitor must be a separate process from the terminal owner.** Case 3
   kills the monitor while the TTY must stay usable. The launcher owns the
   native PTY and the trusted input, so it cannot also be the process that
   case 3 kills (I, N that the TTY survived each loss).
5. **Detached children survive the tool; plain background children do not.**
   Background `&` children of an `exec_command` shell died when that shell
   exited, before they wrote anything (N, development run). `setsid -f`
   children survived and later sent requests through the gate (N). Native
   ends the tool's process group (I). This matters for SDK children that
   outlive a tool call.

## Per-case verdicts

All rows are N on this host with fixtures only. Each stage ran under an outer
timeout. `check_evidence.py` verifies every claimed property and fails on
tampered evidence (`test_evidence.py`).

| Case | Verdict | Evidence (stage) |
| --- | --- | --- |
| 1 Threshold pause | **Pass.** A monitor poll saw 85% used (15% remaining against a 20% five-hour reserve). The lease latched `threshold:five_hour` and closed both registered streams, the held native title stream and the SDK child's held stream. The backend saw both disconnect within 0.1 s. It received no model request between the latch and the continuation. The TUI stayed usable: `/status` rendered its card, PageUp and PageDown worked, and the refusal text was visible. Lock to first byte is covered by an offline test that latches between the decision and the first byte. In one development run a request admitted 1 ms before a latch had already sent its bytes, and the latch closed it | `pause` |
| 1 WebSocket route | **Not exercised (U).** With `supports_websockets = false` and voice not started, the TUI made no WebSocket upgrade in any stage. The gate refuses upgrades (offline test). PC6's realtime refusal remains the only realtime evidence | all |
| 2 Queued child request | **Pass.** An SDK-like child (Python, bearer read from `auth.json`) was admitted before the latch and its held stream was closed by it. Its request after the latch, a `setsid` detached grandchild's request (reparented to PID 1) and the queued native follow-up (sent by Esc, 0.68 s after the latch) all got 503 before upstream. Direct TCP to the backend gave EACCES and UDP EPROTONOSUPPORT (boundary). A test-like process got 200 from the gate before the latch and 503 after | `pause` |
| 3 Monitor loss | **Pass.** Monitor socket close (process alive): `monitor-eof`. SIGKILL of the monitor: `monitor-eof`. A stalled monitor (alive, socket open, no heartbeats): `monitor-stale` after the 1.5 s heartbeat lease. Each closed the held stream, refused the next turn with the visible message, left `/status` working and the same TUI process alive, and sent no upstream byte until recovery. Recovery needed a restarted gate and a continuation | `loss` |
| 4 Gate death and port reuse | **Pass, with NL's port-only gap.** SIGKILL of the gate closed its streams. While the gate was dead, the launcher-held socket made outside and in-boundary binds of `127.0.0.1:P` and `0.0.0.0:P` fail with EADDRINUSE. An in-boundary connect to the gate port sat unanswered in the backlog (timeout), and direct egress got EACCES. No model request reached upstream. The restarted gate started latched; the queued requests then got `paused:...`, and passing monitor reads did not reopen it. A continuation reopened the same TUI. Gap: `127.0.0.2:P` was bindable from inside and outside (Landlock names a port, not an address; NL) | `restart` |
| 5 Fresh manual continuation | **Pass.** F12 and a typed answer that were buffered before the pause were drained (no challenge, no check). A pasted challenge, native-output-side key bytes and passing background reads after quota recovered did not resume. A check whose usage read got 503 stayed paused and the TUI showed `quota reserve gate refused: paused:...`. A fresh two-window check then reopened the same gate process and the same TUI, and the next turn completed | `pause` (also `loss`, `restart`, `rotation`) |
| 6 Independent session | **Pass.** Two invocations shared one linked credential file with five-hour reserves of 20% and 10%. An unprotected client used the same account directly, which raised usage to 85%: the first invocation paused and the second kept completing turns. More outside use (92%) paused the second. The second gate reached the backend only through a loopback CONNECT proxy captured at startup (all tunnels to the backend) | `sessions` |
| 7 Rotation across a pending request | **Pass.** After the server revoked A1, native refreshed through the CONNECT allowlist. Its A1r1 request was held; the rebind admitted it, and the turn completed. Controller variant: the hold ended after about 0.1 s. Gate-read variant: about 1 ms. An old-bearer child request queued during the hold never rode on A1r1's quota. In the controller variant it moved the epoch back to A1 and was refused after the 5 s hold. In the gate-read variant the gate's own read with A1 got the server's 401, so A1 was never rebound and the request was refused at once. Both variants answered it 503. (In a development run of the gate-read variant the child arrived before native's first A1r1 request. It was admitted under A1's own still-fresh observation and got the server's 401.) A child that re-read the file adopted A1r1 only after A1r1's own read. A -> B on disk: a B-bearer child request latched `identity-changed` before upstream; the TUI (still A1r1 in memory) was refused; a continuation with B on disk stayed paused; with A1r1 back, a continuation resumed | `rotation`, `rotation_gate` |
| 8a Refresh via CONNECT allowlist | **Pass, opaque tunnel.** A token inside native's five-minute refresh window made the TUI refresh at startup through `CONNECT auth.openai.com:443` to the gate. The gate tunneled to a loopback TLS host with a fixture CA (`CODEX_CA_CERTIFICATE`), and the next request was rebound to the new bearer. While running, an in-boundary actor used the same tunnel to POST `/oauth/revoke` (reached the fixture: logout revocation is also permitted) and `/backend-api/codex/responses` (reached the refresh host, 404 there). The gate cannot see inside the TLS tunnel. While paused, both CONNECTs got 403 | `refresh` |
| 8b No refresh path | **Pass.** With no CONNECT allowed, the TUI made 61 refused `CONNECT auth.openai.com:443` attempts over the run, kept its old token and completed a turn. After the server revoked that token, monitor reads failed (401), freshness was not renewed, and the lease paused `stale` 8.0 s later (max age 8 s). The next turn was refused; nothing reached upstream after the latch | `refresh` |
| 9 Running-tool effects | **Pass; native interruption does not stop tools.** A 20 s loop (inside a tool call with a 30 s yield) and a background test-like process kept running through the pause. Their network to anything but the gate stayed denied, and the gate refused their model and non-usage requests while paused. Esc (native interrupt, which also sent the queued follow-up, refused) left both running. Both ended within about 0.3 s of the next turn's submission after the continuation, 12.6 s after the latch (I: native cleaned up the interrupted turn's background terminal; mechanism U). The loop did not finish its 20 s | `pause` |
| 10 git and package workflow | **Pass (breakage as expected).** `git clone` over dumb HTTP and a pip-style simple-index GET from a loopback server failed with "Couldn't connect" and EACCES during normal running and after the pause (non-gate port). The same tools reaching the gate port got the gate's answer (200 running, 503 paused) | `pause` |
| 11 Startup refusals | **Pass.** A native that reports 0.150.0, `--remote`, `resume`, `fork`, `remote-control`, `--profile`, user overrides of `chatgpt_base_url` or `model_providers.*`, `CODEX_REFRESH_TOKEN_URL_OVERRIDE`, Landlock ABI 3 or none, TIOCSTI enabled, an unvalidated architecture and socket stdio were each refused. The real boundary refused socket stdin (exit 2) and started nothing. Quota preflights for 503, non-JSON, `rate_limit: null`, an unknown plan, a missing weekly window, a past reset, a foreign account, a crossed reserve and a 1 s slow answer against 0.5 s max age each refused with a named reason and zero model requests. A passing control was accepted | `startup` |

Other observations (N): at startup the TUI tried `CONNECT
raw.githubusercontent.com:443` for its announcement tip (S
`tui/src/tooltips.rs`), which the gate refused. Enter during a tool call
queues input "to be submitted after next tool call". In every TUI run the strace
observer saw Codex connect to one other loopback port and get EACCES. That is
consistent with the self-connect to its own listener that NL reported; KLX did
not record listeners, and the purpose stays U. Every turn still completed. All
other inet connects went to the gate or were denied. No traced process made a
non-loopback connect.

## Measurements for G0

From the committed capture (`evidence/manifest.json` `metrics`; loopback,
fixtures, one host). These are not live numbers.

| Quantity | Median | Max | n | Note |
| --- | --- | --- | --- | --- |
| Preflight: fresh in-boundary App Server, initialize + usage read | 291 ms | 317 ms | 9 | Includes binary start inside the boundary |
| Usage GET round trip at the gate (forwarded) | 0.7 ms | 11.5 ms | 93 | Loopback upstream |
| Admission overhead, gate receipt to upstream receipt | 0.8 ms | 1.6 ms | 63 | Requests without a rebind hold |
| Rotation hold, controller-reader rebind | 121 ms | 5002 ms | 6 | Max is the 5 s refusal of the unbindable old bearer |
| Rotation hold, gate-read rebind | 1.0 ms | 1.0 ms | 4 | One usage GET with the held bearer |
| Latch to upstream close (held streams) | 84 ms | 97 ms | 4 | Includes the fixture's 50 ms heartbeat detection |
| Monitor loss to latch | EOF 0.3 ms, SIGKILL 1.4 ms | stall 1325 ms | 3 | Stall is bounded by the 1.5 s heartbeat lease |

Proposed product values (I; none measurable offline against a real server):
query timeout 10 s for a usage read; controller poll every 60 s while
running (the TUI's own cadence, S), 15 s when either window is within 10
points of its reserve; exponential backoff from 5 s to 60 s on failures, never
renewing freshness; maximum observation age 180 s (three missed polls);
heartbeat interval 0.25 s with a 1.5 s heartbeat lease; rebind hold 5 s with
the controller variant, 3 s with gate-read (one usage RTT plus margin); and a
revalidation budget of one fresh read per rotation, plus one binding read and
one check read per continuation.

## Refusal conditions (startup, before any model request)

- NL host preconditions: not Linux, Landlock unavailable or ABI below 4,
  seccomp architecture not validated (only x86_64 ran), stdio is a socket,
  `dev.tty.legacy_tiocsti` not 0 or unreadable (a G0 alternative is a seccomp
  `ioctl(TIOCSTI)` denial).
- Native: version output unparseable or below the minimum (0.167.2 here; a
  minimum, not a pin, per AGENTS.md, but every internal it relies on is best
  effort). Arguments that leave the single gate-bound process: `--remote`,
  `resume`, `fork`, `remote-control`, `app-server`, `exec-server`, `cloud`,
  `login`, `logout`, `queue`, `sandbox`, `--profile`, `--oss`; `-c` overrides
  of routing or credential-store keys. Environment: refresh or revoke URL
  overrides, `OPENAI_BASE_URL`, API-key variables.
- Quota preflight: gate unavailable; no fresh passing observation (failure,
  malformed, `no-codex-family`, reset anomaly); stale; unsupported plan;
  missing enabled window; identity mismatch; reserve already crossed.

## Costs

- Code (research sizes): the gate is about 580 lines including its process
  wrapper. The launcher policy is about 170, the reader about 140 and the
  monitor about 85. This comes on top of NL's boundary (272) and the QX lease
  (about 330). A product needs TLS upstream, a maintained route table and
  connection reuse.
- Processes per invocation: launcher, gate, monitor, the TUI tree, and one
  short-lived App Server per controller read (about 0.1-0.3 s each, about
  100-150 per hour at the proposed cadence; I).
- Compatibility: L2a's costs from NL apply unchanged (cases 10 and 9 confirm
  them); every non-gate exit is gone while protected.
- Gate-read rebind (recommended, I): the gate originates usage GETs with the
  in-transit bearer. That makes it a second client on the inference host, but
  it uses only the bearer native just sent and never persists or exports it.
  It also ties the gate to the usage path and JSON (already the case for
  observation).
- CONNECT allowlist (8a): an opaque TLS tunnel to `auth.openai.com:443`. The
  gate cannot tell refresh from revocation or from any other path on that
  host. Whether that host serves model traffic is U (S shows native uses it
  for OAuth only). Option 8b avoids the tunnel. Its cost: a stale pause at
  every token expiry (about hourly in practice, U), resolved by an
  unprotected native refresh and a continuation.
- Continuation UX: F12 plus an eight-hex-character challenge is PC5's research
  choice, not a product UX.

## Shared contract C/X/A need (I; names may change)

```text
Policy evaluator inputs (pure, at startup and on every observation):
  account, user (expected identity from the credential claims at launch),
  reserves {five_hour, weekly} (None = off), max_age, plans, models allowlist,
  platform facts {system, machine, landlock_abi, legacy_tiocsti, socket_stdio},
  native {version, argv, env}  -> start | Refusal(reason)

Lease (QX binding.Lease, unchanged API) plus:
  latch(reason)                    # gate-observed loss, restart, identity
  status() -> {paused, blocking, lost, epoch, ready, streams, last_outcome}

Gate control messages (JSON lines on private socketpairs):
  launcher -> gate: label{bearer,label} (research only), continue{} -> accepted,
                    status{} -> status, shutdown{}
  gate -> launcher: ready{port,pid}, paused{reason,at}, rebind{credential}
  monitor -> gate:  hb{seq}  (EOF or a stalled heartbeat latches; lost is final)
Launcher duties: hold the gate's listening socket for the invocation's life;
  restart a lost gate latched; continuation = binding read, continue, check
  read (fresh in-boundary App Server, or the gate-read variant for rebinds).
```

## Coexistence with codex-as (composition-specific effects only)

QX's analysis of account leases and post-run repair stands. Composition
adds:

- Every controller read is another native client of the account. It must
  inherit the account lease descriptor as `run_native` does (I; not
  exercised).
- Controller readers can attempt a proactive refresh when a token is within
  five minutes of expiry. Inside the boundary, with no proxy, that attempt
  fails (N), so readers never compete with the TUI's refresh. If X gave
  readers the proxy, two natives could refresh with one refresh token (I).
- A disk switch to another account mid-run (`codex login` elsewhere) latches
  `identity-changed` on the next file reader, whether a child or the
  controller. That holds while the TUI keeps its old bearer in memory (N).

## Remaining human gates and unproved items

- G0 choices: L2a adoption and its compatibility costs; gate-read versus
  controller-reader rebind; refresh option 8a versus 8b; the continuation
  key and message; a separate monitor process; proposed timings.
- Live behavior (U): real usage semantics and identity fields, the real
  refresh cadence and the double-refresh frequency, whether the real server
  answers a gate-initiated usage read with the in-transit bearer like a native
  read, and what `auth.openai.com` serves.
- WebSocket and realtime in the TUI, compaction, image generation, memories,
  search and goals: refused or disabled here, not exercised (U).
- The port-only Landlock rule (`127.0.0.2:P`), ingress (in-boundary
  listeners) and file-mediated delegation remain NL gaps.
- Interrupted tool cleanup: what ends a background terminal is unproved (U).
- WSL2, arm64 and macOS (KM) are not covered here.

## Containment and real-daemon contact

Every native process got a temporary `HOME`, `CODEX_HOME`, workspace and
fabricated `auth.json`. The TUI and every controller reader ran inside L2a
(gate port only). The TUI tree also ran under an outside strace observer that
denies nothing. The gate, monitor, launcher and fixtures ran outside, all on
loopback. CONNECT attempts other than the fixture-mapped refresh host were
refused and recorded. No real credential, account, login, DNS answer or
provider was used. No real user daemon was contacted: natives got no
session bus. `codex --version` ran once per run with a scratch home. The
runner kills any process naming a stage's scratch root, archives each
`quota-klx-*` root under `.cache/klx/roots/`, removes it and lists it in
`.cache/klx/temp-roots.txt`. The checker requires zero remaining processes.

## Reproduction

From the worktree root, on Linux x86_64 with `codex` on PATH:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/klx/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/klx/check_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/klx/test_evidence.py
pre-commit run --all-files
```

`run.py` runs the offline launcher and gate tests, then the eight native
stages (`startup`, `pause`, `loss`, `restart`, `sessions`, `rotation`,
`rotation_gate`, `refresh`), each in its own session with a 120-210 s outer
timeout. It writes sanitized `evidence/<stage>.json` and a manifest that binds
each evidence file, the KLX sources and the imported QX, NL and PC5 sources by
SHA-256, plus the G0 measurements. It then runs the checker. A timeout or
failure is recorded and is never evidence. Without a native binary it prints
SKIP after the offline tests.
