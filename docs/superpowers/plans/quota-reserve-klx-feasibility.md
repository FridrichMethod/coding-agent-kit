# KLX: Codex composed protected invocation on Linux

Status: **finite composition evidence complete on one Linux host; G0 and every
live-account question remain open**. One protected invocation composed the
reviewed F1 parts: the QX lease and gate admission, the NL L2a boundary, the
PC4 loss latch and the PC5 trusted-terminal continuation. It ran the real
`codex --no-daemon` TUI in a PTY with fabricated credentials, and every
decisive case 1-11 of the K brief passed against loopback fixtures. The K
brief's added items ran too: Codex's own tool sandbox inside the outer
boundary, outer filesystem write rules as its replacement, and a native
refresh outside the gate before launch. The composition also exposed design
facts that the F1 parts did not show alone. They change the recommended Codex
rebind mechanism, add launcher requirements and add a filesystem cost, so they
are G0 items (section "Composition findings"). No product runtime changed.
This is not a G0 pass and not production capability.

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
- Codex always runs `--no-daemon` as the interactive TUI. Every subcommand
  (`resume`, `fork`, `remote-control` and the rest), every option outside a
  small allowlist (`--remote`, profiles, feature toggles, the sandbox flags)
  and user overrides of routing, feature or sandbox keys are refused.
- Native tool sandbox off (`danger-full-access`) inside the outer boundary.
  KLX recorded what Codex's own sandbox does there and evaluated outer
  filesystem write rules as the replacement (K brief, added items).
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
version, arguments, environment or host preconditions; with refresh option
(a), refresh natively outside the gate (case 8c); bind the gate port
(`127.0.0.1`, random ephemeral); start the gate with that descriptor; start
the monitor; run the model-free preflight through a fresh in-boundary App
Server; refuse unless the gate reports a fresh passing observation of both
windows for the expected account and has received the monitor's first
heartbeat; start the TUI inside L2a in its own PTY. The gate's heartbeat lease
starts at that first heartbeat, so a slow preflight cannot latch
`monitor-stale` before the monitor exists. Every preflight recorded zero model
requests (checker).

Native configuration (`launcher.py`, injected as `config.toml` here; X would
inject `-c` overrides): `model_provider` at the gate with
`requires_openai_auth = true`, `supports_websockets = false`, zero retries;
`chatgpt_base_url = <gate>/backend-api/` (the QX path allowlist);
`experimental_realtime_ws_base_url` and
`experimental_realtime_webrtc_call_base_url` at the gate (PC6); file
credential store; web search, plugins, apps, memories and analytics off;
`sandbox_mode = "danger-full-access"`, because Codex's own sandbox cannot run
inside L2a (N, case "Native sandbox inside L2a"). Stage `confine` adds a KLX
Landlock filesystem layer (`klx/fsrules.py`) under L2a for the TUI tree as the
replacement (case "Outer filesystem write rules"); the other stages ran
without it. Only the TUI gets proxy
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
  the route and the lease are re-checked and the request bytes are sent under
  the lock. This holds for model POSTs, forwarded GETs and the gate's own
  rebind read; model streams are also registered there. A latch shuts down
  every registered client and upstream socket under the same lock.
- A tick thread drives `Lease.tick()` and a heartbeat lease that starts at the
  first heartbeat. Monitor EOF, a stalled heartbeat or launcher EOF latches
  that gate instance for good.
- When the launcher reports a failed continuation check, it sends `disarm`,
  so no later passing read can answer that continuation after the user was
  told the invocation is still paused.
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
2. **A disk-reading rebind can bind only the bearer on disk.** In both
   committed rotation stages the server revoked A1 while the turn and the
   title request were both in flight. Both got 401 and each refreshed, so
   native held two new bearers, A1r1 in the turn and A1r2 in the title, and
   the file held A1r2 (N, `rotation`, `rotation_gate`). The controller rebind
   read A1r2, so the turn's A1r1 request was refused after the 5 s hold and
   that turn failed (N, `rotation`). The **gate-read variant** (the gate sends
   one usage GET upstream with the held request's own `Authorization` and
   `ChatGPT-Account-ID`) bound each bearer by its own read, A1r1 and A1r2 in
   under a millisecond each, and the turn completed (N, `rotation_gate`). The
   first committed capture (`276a370`) saw a single refresh, where both
   variants carried the turn, and a development run had seen the same double
   refresh (unretained). It occurred in two of the four committed
   rotation-stage runs across both captures; its live frequency is U. KLX recommends the gate-read for Codex on
   Linux (I): it needs no controller round trip, and it binds whatever bearer
   native actually sent, where a controller read can only see the one bearer
   in the file. Its cost: the gate becomes a usage client that uses the
   in-transit bearer, and the gate needs knowledge of the usage path (section
   "Costs").
3. **Recovering from an identity latch needs a binding read before the
   continuation is armed.** After `identity-changed`, the lease holds no
   credential. The check's own read therefore starts a new epoch, and the QX
   rule "any epoch change clears the continuation" uses it up (N,
   unretained: in the first rotation development run switching back to A
   stayed paused; I from the QX rule). The composed continuation check reads
   once to bind the epoch, arms the continuation, then reads again (N,
   `switch_back` resumed in both rotation stages).
4. **The monitor must be a separate process from the terminal owner.** Case 3
   kills the monitor while the TTY must stay usable. The launcher owns the
   native PTY and the trusted input, so it cannot also be the process that
   case 3 kills (I, N that the TTY survived each loss).
5. **Detached children survive the tool; plain background children may
   not.** In a development run, background `&` children of an
   `exec_command` shell that exited at once died before they wrote anything
   (**N, unretained**: no archive or retained log holds that run). `setsid -f`
   children survived and later sent requests through the gate (N, committed
   `pause` and `rotation` stages). That native ends the tool's process group
   is I. In the committed `pause` stage the `&` children of a shell that kept
   running (its loop step) stayed alive until the TUI shut down (case 9). This
   matters for SDK children that outlive a tool call.
6. **Codex's own tool sandbox cannot run inside L2a, so protected mode loses
   native filesystem confinement unless an outer layer replaces it.** With
   `sandbox_mode = "workspace-write"` inside L2a, the tool call failed before
   running: exit 1, `bwrap: loopback: Failed to look up lo: No such file or
   directory`, and the file it should have written was absent (N, `confine`).
   The TUI stayed usable and the next turn completed. That bubblewrap fails
   because L2a's seccomp filter denies the sockets it needs to configure the
   loopback interface of its new network namespace is I (NL saw bubblewrap
   refuse under L1 with a different error). A KLX Landlock layer that leaves
   only the workspace, temporary and Codex home directories writable restored
   the main property for the TUI tree: tool writes inside them worked, writes
   to the home directory and to an outside directory got EACCES, `git commit`
   in the workspace worked, and the TUI completed two turns and `/status` with
   its home read-only (N, `confine`). Its gaps are listed under "Costs".
7. **A native refresh outside the gate before launch works with native's own
   App Server and no wrapper refresh client.** One fresh `codex app-server`
   outside the gate answered `account/read` with `refreshToken: true` by
   refreshing (N, case 8c). When the token was already inside native's
   five-minute window, that one launch step refreshed twice (startup plus the
   forced refresh). The protected run that followed needed no refresh path. A
   rejected refresh left the credential file unchanged, and the launcher
   refused to start.

## Per-case verdicts

All rows are N on this host with fixtures only, except where a row says
otherwise (the case 11 host preconditions are offline checks of injected
values). Each stage ran under an outer timeout. `check_evidence.py` verifies
every claimed property and fails on tampered evidence (`test_evidence.py`).

| Case | Verdict | Evidence (stage) |
| --- | --- | --- |
| 1 Threshold pause | **Pass.** A monitor poll saw 85% used (15% remaining against a 20% five-hour reserve). The lease latched `threshold:five_hour` and closed both registered streams, the held native title stream and the SDK child's held stream. The backend saw both disconnect within 0.1 s. Between the latch and the continuation it received no model request and no request other than usage reads (checker). The TUI stayed usable while paused: the native transcript view (Ctrl+T, then PageUp and Home inside it) showed the first turn's prompt, which was absent from the output after the mark set before the key; `/status` rendered its card; `/ps` listed one running background terminal; and the refusal text was visible. Lock to first byte is covered by offline tests that latch or lose the gate between the route decision and the first byte (model POST and forwarded GET). In one development run a request admitted 1 ms before a latch had already sent its bytes, and the latch closed it (N, unretained) | `pause` |
| 1 WebSocket route | **Not exercised (U).** With `supports_websockets = false` and voice not started, the TUI made no WebSocket upgrade in any stage. The gate refuses upgrades (offline test). PC6's realtime refusal remains the only realtime evidence | all |
| 2 Queued child request | **Pass.** An SDK-like child (Python, bearer read from `auth.json`) was admitted before the latch and its held stream was closed by it. Its request after the latch, a `setsid` detached grandchild's request (reparented to PID 1) and the queued native follow-up (sent by Esc, 0.74 s after the latch) all got 503 before upstream. Direct TCP to the backend gave EACCES and UDP EPROTONOSUPPORT (boundary). A test-like process got 200 from the gate before the latch and 503 after | `pause` |
| 3 Monitor loss | **Pass.** Monitor socket close (process alive): `monitor-eof` 0.2 ms after the trigger. SIGKILL of the monitor: `monitor-eof` after 1.4 ms. A stalled monitor (alive, socket open, no heartbeats): `monitor-stale` after 1524 ms, bounded by the 1.5 s heartbeat lease (all three bound in the manifest metrics). Each closed the held stream, refused the next turn with the visible message, left `/status` working and the same TUI process alive, and no request of any kind reached upstream between the latch and the replacement gate (checker). Recovery needed a restarted gate and a continuation | `loss` |
| 4 Gate death and port reuse | **Pass, with NL's port-only gap.** SIGKILL of the gate closed its streams. While the gate was dead, the launcher-held socket made outside and in-boundary binds of `127.0.0.1:P` and `0.0.0.0:P` fail with EADDRINUSE. An in-boundary connect to the gate port sat unanswered in the backlog (timeout), and direct egress got EACCES. No request of any kind reached upstream until the restarted gate started. The restarted gate started latched; the queued requests then got `paused:...`, and passing monitor reads did not reopen it. A continuation reopened the same TUI. Gap: `127.0.0.2:P` was bindable from inside and outside (Landlock names a port, not an address; NL) | `restart` |
| 5 Fresh manual continuation | **Pass.** F12 and a typed answer that were buffered before the pause were drained (no challenge, no check). A pasted challenge, native-output-side key bytes and passing background reads after quota recovered did not resume. A check whose usage read got 503 stayed paused, the launcher sent `disarm`, and the TUI showed `quota reserve gate refused: paused:...`. A fresh two-window check then reopened the same gate process and the same TUI, and the next turn completed. That a disarmed continuation is not answered by a later passing read is an offline test (the native check's own failed read had already used it up) | `pause` (also `loss`, `restart`, `rotation`) |
| 6 Independent session | **Pass.** Two invocations shared one linked credential file with five-hour reserves of 20% and 10%. An unprotected client used the same account directly, which raised usage to 85%: the first invocation paused and the second kept completing turns. More outside use (92%) paused the second. The second gate's upstream used a loopback CONNECT proxy captured at startup: the proxy recorded 35 allowed tunnels to the backend, equal to the 35 requests that gate forwarded (N, checker requires at least as many tunnels as forwarded requests). That every upstream connection of that gate used the proxy holds by construction (I: the gate tunnels whenever a proxy was captured) | `sessions` |
| 7 Rotation across a pending request | **Pass on the safety property; the controller variant failed one turn.** After the server revoked A1, the turn and the title request both got 401 and each refreshed through the CONNECT allowlist, so native held two new bearers: A1r1 in the turn and A1r2 in the title, with A1r2 in the file. This double refresh happened in both committed rotation stages. Controller variant: its rebind read bound A1r2 after 0.10 s and admitted the title; A1r1 could never be bound by a disk read, was refused after the 5 s hold, and that turn failed with the gate's refusal. Gate-read variant: the gate bound A1r1 and A1r2 each by its own usage read (0.67 ms and 0.92 ms holds) and the turn completed. Old quota never authorized a new bearer in either variant (checker over every admission). The old-bearer child request: controller variant, queued during the hold, moved the epoch back to A1 and was refused after the 5 s hold (503); gate-read variant, it reached the gate before native's first A1r1 request, was admitted under A1's own still-fresh observation and got the server's 401. A child that re-read the file adopted A1r2 only after A1r2's own read. A long-lived controller App Server read with its stale A1 (401, epoch moved away), as finding 1 describes. A -> B on disk: a B-bearer child request latched `identity-changed` before upstream; the TUI (still A1r2 in memory) was refused; a continuation with B on disk stayed paused; with A1r2 back, a continuation resumed | `rotation`, `rotation_gate` |
| 8a Refresh via CONNECT allowlist | **Pass, opaque tunnel.** A token inside native's five-minute refresh window made the TUI refresh at startup through `CONNECT auth.openai.com:443` to the gate. The gate tunneled to a loopback TLS host with a fixture CA (`CODEX_CA_CERTIFICATE`), and the next request was rebound to the new bearer. While running, an in-boundary actor used the same tunnel to POST `/oauth/revoke` (reached the fixture: logout revocation is also permitted) and `/backend-api/codex/responses` (reached the refresh host, 404 there). The gate cannot see inside the TLS tunnel. While paused, both CONNECTs got 403 | `refresh` |
| 8b No refresh path | **Pass.** With no CONNECT allowed, the TUI made 61 refused `CONNECT auth.openai.com:443` attempts over the run, kept its old token and completed a turn. After the server revoked that token, monitor reads failed (401), freshness was not renewed, and the lease paused `stale` 8.0 s later (max age 8 s). The next turn was refused; nothing reached upstream after the latch | `refresh` |
| 8c Native refresh before launch (added item, option a) | **Pass.** With a token inside native's window (120 s left), one fresh `codex app-server` outside the gate (confined here to the fixture CONNECT proxy port, standing in for the user's network) answered `account/read` with `refreshToken: true`. It refreshed twice, once on its own at startup and once on the forced request, both through the proxy (all four proxy tunnels went to `auth.openai.com:443`). Its account read itself returned an RPC error (`workspace routing discovery failed`; the gate port is unreachable from that domain), but the credential file's `last_refresh` changed, so the launcher started. The step took 1.21 s, mostly App Server start on a loaded host. The protected run that followed completed a turn and made no refresh CONNECT and no refresh in the next 6 s, against 61 refused attempts in 8b. With a refresh token the server no longer accepted, native's two attempts were rejected, `last_refresh` stayed unchanged, native's account read answered with no account, and the launcher refused `prelaunch-refresh:failed` before any gate, TUI or model request. The expiry half of option (a) is 8b's stale pause | `refresh` |
| 9 Running-tool effects | **Pass; the next turn does not stop tools, TUI shutdown does.** A 60 s loop (inside a tool call with a 30 s yield) and a background test-like process (90 s) kept running through the pause. Their network to anything but the gate stayed denied, and the gate refused their model and non-usage requests while paused. Esc (native interrupt, which also sent the queued follow-up, refused) left both running. Polling their PIDs and start times through `/proc`: both were alive after the continuation (15.8 s after the latch), right after the next turn's reply (16.9 s) and 6 s later with the TUI still open (22.9 s), when native still showed `1 background terminal running`. The harness then pressed Ctrl+C; the TUI exited by 23.5 s and both tools were gone at the first poll after that. Mechanism (S): session shutdown calls `terminate_all_processes` on the unified exec manager (`core/src/session/handlers.rs`); the other paths that end a background terminal are `/stop`, the 64-process cap and a network-denial termination. Neither the pause, Esc nor the next turn ended them | `pause` |
| 10 git and package workflow | **Pass (breakage as expected).** `git clone` over dumb HTTP and a pip-style simple-index GET from a loopback server failed with "Couldn't connect" and EACCES during normal running and after the pause (non-gate port). The same tools reaching the gate port got the gate's answer (200 running, 503 paused) | `pause` |
| 11 Startup refusals | **Pass.** Native: a native that reports 0.150.0 was refused. Arguments: `--remote`, `resume`, `fork`, `remote-control`, `--profile`, `--enable`, `-s`, and user overrides of `chatgpt_base_url` or `model_providers.*` in both the separate and the attached (`-cKEY=VALUE`) spelling were refused; a plain prompt was accepted. `codex ARGV --help` run inside L2a showed what clap 0.167.2 selects: `-m gpt-5.4 resume`, `-mgpt-5.4 resume`, `-c=model=x resume`, `--model=x fork`, `-C DIR fork`, `hello resume`, `--image=a.png fork` and `e` each selected a subcommand, and the screen refused every one; `-i a.png fork` and `-m resume`, where clap takes the word as an option value, are refused too (stricter); `-m gpt-5.4 "fix the resume parser"` parsed as a prompt and was accepted (checker: no probe that selects a subcommand passes). Offline tests cover more spellings (attached and `=` forms, quoted keys, missing values, combined flags, unknown options, `--` prompts). `CODEX_REFRESH_TOKEN_URL_OVERRIDE` was refused. Host preconditions, offline checks of injected values, not native observations: Landlock ABI 3 or none, TIOCSTI enabled, an unvalidated architecture and socket stdio were each refused, and the actual host passed. The real boundary refused socket stdin (exit 2) and started nothing (N). Quota preflights for 503, non-JSON, `rate_limit: null`, an unknown plan, a missing weekly window, a past reset, a foreign account, a crossed reserve, a 1 s slow answer against 0.5 s max age and a missing monitor heartbeat each refused with a named reason and zero model requests. A passing control was accepted | `startup` |
| Native sandbox inside L2a (added item) | **Codex's own sandbox does not work inside the outer boundary.** With `sandbox_mode = "workspace-write"`, the tool call returned exit 1 with `bwrap: loopback: Failed to look up lo: No such file or directory` and did not write its file. The model got that failure as the tool result, the TUI stayed usable, the next turn completed and the TUI exited 0 (finding 6) | `confine` |
| Outer filesystem write rules (added item) | **Pass for the TUI tree, with the gaps under "Costs".** `danger-full-access` inside L2a plus the KLX Landlock layer (writable: workspace, invocation temp, Codex home, the actor evidence directory and the terminal and null devices). A tool call created files in the workspace, the temp directory and Codex's home; the home directory and an outside directory got EACCES and stayed empty; `git init` and `git commit` in the workspace worked. The TUI, with its home directory read-only, completed the tool turn and a second turn, rendered `/status` and exited 0 | `confine` |

Other observations (N): at startup the TUI tried `CONNECT
raw.githubusercontent.com:443` for its announcement tip (S
`tui/src/tooltips.rs`), which the gate refused. Enter during a tool call
queues input "to be submitted after next tool call". In every TUI run the strace
observer saw Codex connect to one other loopback port and get EACCES. That is
consistent with the self-connect to its own listener that NL reported; KLX did
not record listeners, and the purpose stays U. Every turn still completed. All
other inet connects went to the gate or were denied with EACCES (the checker
accepts no other result for a non-gate connect, except a connect to the gate
port on `127.0.0.2`, NL's known gap, which did not occur). No traced process
made a non-loopback connect.

## Measurements for G0

From the committed capture (`evidence/manifest.json` `metrics`; loopback,
fixtures, one host). These are not live numbers.

| Quantity | Median | Max | n | Note |
| --- | --- | --- | --- | --- |
| Preflight: fresh in-boundary App Server, initialize + usage read | 704 ms | 1776 ms | 12 | Includes binary start inside the boundary. The host was under concurrent load during this capture; the first committed capture (`276a370`) measured 291 ms median, 317 ms max (n = 9) |
| Usage GET round trip at the gate (forwarded) | 0.7 ms | 12.3 ms | 127 | Loopback upstream |
| Admission overhead, gate receipt to upstream receipt | 0.9 ms | 1.8 ms | 78 | Requests without a rebind hold |
| Rotation hold, controller-reader rebind | 120 ms | 5017 ms | 6 | Max is the 5 s refusal of a bearer the file did not hold |
| Rotation hold, gate-read rebind | 0.8 ms | 0.9 ms | 3 | One usage GET with the held bearer, recorded with microsecond resolution |
| Latch to upstream close (held streams) | 70 ms | 97 ms | 4 | Includes the fixture's 50 ms heartbeat detection |
| Monitor loss to latch | EOF 0.2 ms, SIGKILL 1.4 ms | stall 1524 ms | 3 | Stall is bounded by the 1.5 s heartbeat lease |
| Refresh before launch (8c), App Server start + forced refresh | 1210 ms | 1210 ms | 1 | Two refreshes, the token being inside native's window; the forced refresh itself took 34 ms |

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
  effort). Arguments (`launcher.check_args`, which follows clap's grammar for
  0.167.2): any word clap could take as a subcommand, that is every bare word
  before `--` that names one of the 0.167.2 subcommands or aliases (`resume`,
  `fork`, `e`, `help` and the rest), also after an option value or a prompt;
  any option outside the allowlist `-c/--config`, `-m/--model`, `-C/--cd`,
  `-i/--image`, `--no-alt-screen`, `--no-daemon`, `--strict-config`, `-h`,
  `-V`, with attached forms (`-mVALUE`, `-c=VALUE`, `--model=VALUE`) parsed
  as clap parses them; `-c` overrides whose key is not a plain dotted name or
  whose root is a routing, credential-store, profile, feature, sandbox, web
  search, tools or MCP key. It is stricter than clap in two places: a
  subcommand name as an option value (`-m resume`, `-i a.png fork`) and
  combined short flags (`-hV`) also refuse. Environment: refresh or revoke URL
  overrides, `OPENAI_BASE_URL`, API-key variables.
- Refresh option (a): the native refresh outside the gate did not change the
  credential file's `last_refresh` (`prelaunch-refresh:failed`).
- Outer filesystem rules, where used: Landlock unavailable or a writable path
  missing (`fsrules.py` exits 2 and starts nothing).
- Quota preflight: gate unavailable; no fresh passing observation (failure,
  malformed, `no-codex-family`, reset anomaly); stale; unsupported plan;
  missing enabled window; identity mismatch; reserve already crossed; no
  heartbeat from the monitor yet (`monitor-not-started`).

## Costs

- Code (research sizes): the gate is about 620 lines including its process
  wrapper. The launcher policy is about 270 (half of it the argument screen),
  the reader about 165, the monitor about 85 and the filesystem layer about
  140. This comes on top of NL's boundary (272) and the QX lease (about 360).
  A product needs TLS upstream, a maintained route table and connection reuse.
- Native filesystem confinement (G0 cost). Protected mode must turn Codex's
  own sandbox off, because inside L2a it fails every tool call (finding 6). A
  protected invocation without an outer filesystem layer therefore runs tools
  with no filesystem confinement, a regression against a default native run
  (workspace-write). The KLX layer restores "workspace and temporary
  directories writable, the rest read-only" for the TUI tree, with these gaps
  and costs: reads stay unrestricted, as in Codex's workspace-write mode;
  Codex's home must stay writable for Codex itself, so tools can also write
  it, `auth.json` included (the layer cannot separate a tool from its parent);
  tools that write elsewhere (caches under the home directory, package
  managers, `--add-dir`, which the screen refuses) break with EACCES (I beyond
  the probes run); the writable list is per invocation and must name the
  devices the TUI needs (`/dev/null`, `/dev/tty`, `/dev/ptmx`, `/dev/pts`,
  `/dev/shm` here); rights depend on the Landlock ABI (REFER needs ABI 2,
  TRUNCATE ABI 3; device ioctl is left unhandled); controller readers ran
  without the layer. It adds one exec and one Landlock domain per TUI start.
  This is a kit-owned filesystem sandbox component and needs the same review
  as L2a.
- Processes per invocation: launcher, gate, monitor, the TUI tree, and one
  short-lived App Server per controller read (about 0.3 s each on an idle
  host, up to 1.8 s under load; about 100-150 per hour at the proposed
  cadence, I).
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
- Refresh before launch (8c, option (a) of the added items): one extra native
  App Server outside the gate per launch (1.21 s in the committed capture on
  a loaded host, n = 1; 0.38 s in an archived development run of this
  revision) and one forced refresh per launch, two when the token is already
  inside native's window. That App Server is an unprotected native client with the user's
  network (here confined to the fixture proxy); the launcher reads the
  credential file's `last_refresh` field. It does not remove 8b's stale pause
  when the new token expires during the run; it only moves it to the full
  token lifetime (about an hour, U live). Rotating the refresh token at every
  launch may affect other natives that share the file (I, not exercised).
- Argument screen: its subcommand table is per native version. A newer
  native's new subcommand typed as a one-word prompt would pass (I), so X must
  regenerate the table for each supported native version or pass the prompt
  after `--`.
- Continuation UX: F12 plus an eight-hex-character challenge is PC5's research
  choice, not a product UX.

## Shared contract C/X/A need (I; names may change)

```text
Policy evaluator inputs (pure, at startup and on every observation):
  account, user (expected identity from the credential claims at launch),
  reserves {five_hour, weekly} (None = off), max_age, plans, models allowlist,
  platform facts {system, machine, landlock_abi, legacy_tiocsti, socket_stdio},
  native {version, argv, env}, writable paths (outer filesystem rules),
  pre-launch refresh {last_refresh before, after}  -> start | Refusal(reason)

Lease (QX binding.Lease, unchanged API) plus:
  latch(reason)                    # gate-observed loss, restart, identity
  cancel_continuation(reason)      # the launcher reported a failed check
  status() -> {paused, blocking, lost, epoch, ready, streams, last_outcome,
               heartbeat}

Gate control messages (JSON lines on private socketpairs):
  launcher -> gate: label{bearer,label} (research only), continue{} -> accepted,
                    disarm{} -> armed, status{} -> status, shutdown{}
  gate -> launcher: ready{port,pid}, paused{reason,at}, rebind{credential}
  monitor -> gate:  hb{seq}  (EOF or a stalled heartbeat latches; lost is final)
Launcher duties: hold the gate's listening socket for the invocation's life;
  start the monitor before the preflight and the native only after its first
  heartbeat; restart a lost gate latched; continuation = binding read,
  continue, check read, and disarm when the check fails (fresh in-boundary App
  Server, or the gate-read variant for rebinds).
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
  controller-reader rebind; refresh option 8a, 8b or 8c; native sandbox off
  with or without the outer filesystem layer (accepting the lost native
  confinement, finding 6); the continuation key and message; a separate
  monitor process; proposed timings.
- X: keep the argument screen's subcommand and option tables per supported
  native version (clap-faithful screen; see "Costs").
- Live behavior (U): real usage semantics and identity fields, the real
  refresh cadence and the double-refresh frequency, whether the real server
  answers a gate-initiated usage read with the in-transit bearer like a native
  read, and what `auth.openai.com` serves.
- WebSocket and realtime in the TUI, compaction, image generation, memories,
  search, goals, native sub-agents (in-session agent spawning) and hooks: not
  exercised (U). Realtime, search, memories and the `agents` subcommand are
  refused or disabled by the configuration and the screen; hooks need trust,
  and the screen refuses `--dangerously-bypass-hook-trust`. A sub-agent or a
  hook runs inside the same L2a domain as the TUI and reaches only the gate
  (I, from the boundary), but its quota and stream behaviour is U.
- History in the inline view: the native transcript view showed earlier turns
  while paused (N). Scrolling the inline view itself is the outer terminal's
  own scrollback, which the harness does not emulate (U).
- Launcher death: the gate latches `launcher-eof` (N, offline test of the gate
  process). The launcher holds the native's PTY master, so its death closes
  the master and the kernel hangs up the native's terminal (SIGHUP to its
  session); the native loses its TTY and is expected to exit (I, not run
  natively). Nothing is admitted after the latch either way.
- The port-only Landlock rule (`127.0.0.2:P`), ingress (in-boundary
  listeners) and file-mediated delegation remain NL gaps.
- WSL2, arm64 and macOS (KM) are not covered here.

## Containment and real-daemon contact

Every native process got a temporary `HOME`, `CODEX_HOME`, workspace and
fabricated `auth.json`. The TUI and every controller reader ran inside L2a
(gate port only). The TUI tree also ran under an outside strace observer that
denies nothing. The gate, monitor, launcher and fixtures ran outside, all on
loopback. CONNECT attempts other than the fixture-mapped refresh host were
refused and recorded. The pre-launch refresh App Server (case 8c) ran
outside the gate but inside an L2a domain whose only reachable port was the
fixture CONNECT proxy, which mapped `auth.openai.com:443` to the loopback TLS
host; in production that process would use the user's network. The argument
probes (`codex ARGV --help`, case 11) ran inside L2a with a dead gate port.
No real credential, account, login, DNS answer or provider was used. No real
user daemon was contacted: natives got no session bus. `codex --version` ran
once per run with a scratch home. The runner kills any process naming a
stage's scratch root, archives each `quota-klx-*` root under
`.cache/klx/roots/`, removes it and lists it in `.cache/klx/temp-roots.txt`.
The checker requires zero remaining processes. Development runs before the
first committed capture were removed without an archive (a disclosed
deviation from the node protocol; findings 3 and 5 and a case 1 remark mark
what rests on them as unretained).
Development runs of this revision were archived under `.cache/klx/roots/`
with logs in `.cache/klx/dev2-*.log`.

## Reproduction

From the worktree root, on Linux x86_64 with `codex` on PATH:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/klx/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/klx/check_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/klx/test_evidence.py
pre-commit run --all-files
```

`run.py` runs the offline launcher, gate and filesystem-rule tests, then the
nine native stages (`startup`, `pause`, `loss`, `restart`, `sessions`,
`rotation`, `rotation_gate`, `refresh`, `confine`), each in its own session
with a 120-210 s outer timeout. It writes sanitized `evidence/<stage>.json`
and a manifest that binds each evidence file, the KLX sources and the
imported QX, NL and PC5 sources by SHA-256, plus the G0 measurements. It then runs the checker. A timeout or
failure is recorded and is never evidence. Without a native binary it prints
SKIP after the offline tests.
