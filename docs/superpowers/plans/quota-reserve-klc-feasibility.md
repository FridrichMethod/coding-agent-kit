# KLC: composed protected Claude invocation on Linux

Status: **finite composition evidence complete on one Linux host; G0, full A
and every live-provider question remain open**. One protected invocation of
real Claude Code 2.1.322 was composed from the reviewed QC admission
contract, the NL L2a boundary and the PC5/PA1C trusted-terminal
continuation, and all eleven K decisive cases ran end to end with real
processes and loopback fixtures. Every case met its stated outcome; the
residual gaps, costs and human gates are listed below. Research only: no
launcher, completion, helper, installer or suite change.

Base `10fc4dc` (branch `task/quota-klc`). Captured November 12, 2026 UTC on
Ubuntu 24.04.5 LTS, HWE kernel `7.0.0-42-generic`, x86_64, Landlock ABI 8,
`dev.tty.legacy_tiocsti=0`, Python 3.15 harness. Native: Claude Code
`2.1.322`, SHA-256 `bfb3703a...c478`, the binary QC, NL, PA1C and PA1T used.
Code and evidence: [quota-reserve-probes/klc/](quota-reserve-probes/klc/).

No real credential, login, account, Keychain item or provider endpoint was
used. Every token, account, quota window, refresh grant and model response is
fabricated; tokens never appear in evidence, only fixture labels (A, A2, B).
The native and all of its descendants ran inside L2a, where only the gate
port is reachable; no egress attempt beyond loopback was observed (section
11). Nothing was installed and no system setting was changed.

Labels: **N** native observation in this node; **S** inspected source,
binary text or documentation; **I** inference; **U** unproved.

## Verdicts

| Case | Verdict (N unless marked) | Evidence |
| --- | --- | --- |
| 1 Threshold pause | Latch on the next poll after the fake usage crosses the reserve; root, native child and grandchild SSE streams closed; zero upstream bytes after the latch; TUI history view (banner checked) and later prompt work, later prompt refused; local slash commands not exercised (U) | `threshold.json` |
| 2 Queued child request | SDK-like tool child and its `setsid` grandchild: admitted before, refused (403) after the latch, never forwarded after it; their direct attempts EACCES | `threshold.json` |
| Native subagent | `Agent` child and grandchild requests go through the gate and are closed and refused like the root | `threshold.json` |
| 3 Monitor loss | SIGKILL and EOF latch in under 1 ms; stall latches at the 4 s lease deadline via the gate's own timer; zero upstream bytes after; TTY usable; a later continuation is refused (`monitor_lost`, `monitor_stale`) before any store read or quota fetch | `loss-*.json` |
| 4 Gate death, port reuse | Gate death closes streams; the launcher's held listening socket keeps the port (in-boundary and outside impostor binds EADDRINUSE, requests wait in the backlog, zero upstream bytes); a restarted gate starts paused and refuses the backlog until a fresh verified continuation. Without the holder both impostors bind and each receives the confined client's request (gap shown) | `gate-death.json` |
| 5 Fresh manual continuation | Stale typed input, bracketed paste, bytes on the native's output side and background wakeups grant nothing; a typed challenge with a failing check stays paused with the deterministic message; a fresh two-window same-bearer check reopens the same gate and native | `continuation.json` |
| 6 Independent session | Second invocation of the same account with a lower reserve keeps forwarding when the first pauses; outside consumption pauses it later | `independent.json` |
| 7 Rotation | Native refresh A to A2 (same account) is held, re-checked with A2 and admitted; A2 to B with a child request queued pauses with zero B bytes; old quota never authorized a new bearer | `rotation.json` |
| 8a Refresh tunnel | Native refreshes through `CONNECT platform.claude.com:443` at the gate while the lease is open; other hosts and ports refused; closed on pause | `rotation.json` |
| 8b No refresh path | Native keeps sending the expired token A (bearer label recorded on its refused requests); the first failed same-bearer revalidation pauses (0.42 s after server-side expiry) | `norefresh.json` |
| 8 option (a) | `claude auth status` inside a refresh-only tunnel refreshes a near-expiry or expired token model-free in about 0.2 s | `prelaunch.json` |
| Long pause past token expiry | Neither design can continue the same invocation once the token expires during a pause (I, not exercised); G0 options in section 3 | - |
| 9 Running tools | Foreground and background local work continue through the pause; their gate requests get 403 and direct egress EACCES; one Ctrl-C ends the foreground shell, not the background task; native exit stops background tasks, not a detached grandchild (case 1) | `tools.json` |
| 10 Git and package | git over HTTP and a pip-style download fail inside L2a before and after the pause (EACCES); HTTPS through the inherited proxy gets the gate's 403; outside control succeeds for git and the download (the HTTPS check has no outside control) | `tools.json` |
| 11 Startup refusals | Quota, identity, clock and expired-token refusals by the real gate process, and launcher refusals (older version, `-p`, `-cp`, `--bare`, caller `ANTHROPIC_BASE_URL` or `CLAUDE_CODE_OAUTH_TOKEN`) on the composed launch path, start no native; zero model requests. Token window and host checks: offline (pure-function) evidence | `refusals.json` |
| Native sandbox | `sandbox.enabled` fails to initialize inside L2a (AF_UNIX listen denied), the command is refused and native disables sandboxing for the session | `sandbox.json` |
| Outer write rule | Landlock write rule (workspace, temp, config, native state) plus L2a: native works, writes outside are denied | `fsrules.json` |

## 1. Composed architecture

```text
 launcher (trusted; here the harness)                        outside the boundary
  |- bind 127.0.0.1:0 -> gate port P; keep the listening fd    port holder
  |- refusal checks (host, version, mode, caller env)          compose.py
  |- spawn gate(fd P, monitor socketpair, control socketpair)  gate.py
  |     gate: read bearer memory-only from the native store;
  |           same-bearer usage + profile preflight (both windows); else exit 3
  |- spawn monitor(socketpair end): "poll" every interval     monitor.py
  |- PTY: boundary.py --gate-port P --seccomp inet -- claude  NL L2a, inherited
        proposed: claude --settings '{"env":{"ANTHROPIC_BASE_URL":
                    "http://127.0.0.1:P"}}' [user arguments]
                  env HTTPS_PROXY=http://127.0.0.1:P NO_PROXY=127.0.0.1,localhost
                    (refresh design 8a only)
        harness only: --safe-mode --model claude-sonnet-4-6
                      --permission-mode dontAsk, permissions.allow [Bash, Agent]
 gate -> fake upstream (Messages), fake usage/profile, refresh fixture (tunnel)
```

The harness builds the port holder when it creates an invocation, so a
launcher refusal leaves only that listening socket; the order of the first
two steps does not matter to any result here.

- Bearer custody: only the gate process holds the bearer (QC option 3). The
  monitor holds none; the launcher never reads the store. Any in-boundary
  process of the same user can read the store too (by design: SDK children
  use it); it gains nothing beyond the native's own quota, because every
  request still passes the same lease (N, case 2). Gate events carry only a
  16-hex SHA-256 fingerprint of the presented bearer, which the harness maps
  to the fixture label before writing evidence.
- Admission invariant: QC `AdmissionController` decides each Messages request
  before any upstream byte. The gate opens the upstream connection (TCP, or
  CONNECT through the launcher proxy) without the lock and without sending a
  request byte, then writes the request while holding the admission lock and
  only if the stream is still registered under an open lease whose deadline
  has not passed (`live()` pauses `freshness_expired` itself at a passed
  deadline), and emits `forwarded` inside that lock. In each of the 12
  native stages every upstream arrival matches one `forwarded` event, and no
  forward follows the latch (N, checked by `check_evidence.py`).
- K-owned additions to QC (QC section 3 gates): an independent 20 ms timer
  closes everything at the lease deadline without waiting for `admit()`;
  monitor EOF latches and stays lost; a restarted gate starts `paused` with
  no lease and a new monitor channel; a startup lease that holds an unproved
  row refuses at startup instead of per request; CONNECT is admitted only for
  an exact allowlisted `host:port` while open and registered like a stream,
  so a pause closes tunnels.
- Pause closes every registered client and upstream socket; nothing passive
  reopens it. Only a one-use continuation from the trusted control channel
  followed by a fresh same-bearer check does (QC continuation), and only while
  control remains healthy: after monitor EOF the gate answers `monitor_lost`
  until a new gate process with a new monitor channel replaces it, and while
  the monitor sent no poll within the maximum observation age it answers
  `monitor_stale`. Both refusals come before any store read or quota fetch,
  and a refused attempt voids its token (K rule; N, case 3).
- Wrapper-originated connections: the gate's upstream connections use the
  launcher's `HTTPS_PROXY` captured once at startup when one is configured
  (provisional decision; one case through a counting loopback proxy, all
  forwards tunneled, N). QC's usage client has no proxy support (S), so a
  proxy-only network needs that added (I, cost).
- Native configuration. Proposed: inline `--settings` with
  `env.ANTHROPIC_BASE_URL` pins Messages to the gate (N); `HTTPS_PROXY` at the
  gate is honored for every HTTPS call (N) and is needed only for refresh
  design 8a; the native sandbox stays off (section 3). Harness only, not
  proposed: `--safe-mode`, `--model`, and `--permission-mode dontAsk` with
  `permissions.allow: [Bash, Agent]`, which let the scripted tool calls run
  without prompts and avoid the default auto mode's extra classifier traffic
  (I for the classifier). Auto-allowing Bash changes permission semantics and
  is not part of the composition. `--safe-mode` disables CLAUDE.md, skills,
  plugins, hooks, MCP servers and custom agents (native banner, N; managed
  hooks stay, QC S) and keeps the built-in `Agent` tool. So hooks, MCP servers
  and plugins never ran under the composed boundary and gate here (U, section
  8); making safe mode part of protected mode would cost the user those
  customizations (section 7).

## 2. Provisional decisions used

All are research choices from L/pending-user-questions.md and remain G0
choices: L2a boundary; memory-only `accessToken` read plus same-bearer usage
and profile identity (QC option 3); launcher `HTTPS_PROXY` inherited once by
wrapper connections; same-identity rotation with a fresh passing check keeps
admitted streams while any failed revalidation, threshold, monitor loss or
stale lease closes all; only the gate port reachable; native sandboxes off in
protected mode with an outer write rule evaluated as the replacement; both
refresh designs measured.

## 3. Case results

Times are from one final run on the host monotonic clock; loopback only.

**1 Threshold.** Root prompt, native `Bash(run_in_background)` starting the
SDK-like child (which `setsid`-forks a grandchild), then native `Agent` child
and grandchild; three SSE streams held. The fake usage then reports 85%
five-hour use against a 20% reserve. The next poll (905 ms after the change,
interval 1 s) latched `threshold_five_hour`; the three held streams
disconnected 146 to 161 ms later (fixture ping cadence 100 ms). 527 to 586 ms
after the latch native sent five tool-bearing requests within 59 ms of each
other (two with the subagent tool set, three with the root's), all refused
403 with bearer A, and then stopped (no retry storm on 403); the later prompt
sent two more (a tool-less auxiliary request and the root turn), both
refused. All 128 SDK child and grandchild requests after the latch got 403.
Ctrl-O opened the history view (native's `Showing detailed transcript`
banner recorded), a new prompt via native send-now (refused at the gate) and
normal exit worked; termios restored. Local slash commands while paused were
not exercised, here or in PA1C, whose UI checks are bounded too (U).

Exit was slow in this case only. Native confirmed `Exit and stop tasks` and
printed its resume hint, but the observed process ended 38.5 s after the
first exit key (7.5 s to 46.0 s after start), against 4 s in every other case
(N). It ended at the moment the `setsid` grandchild finished its 45 s loop
(last row at 46.0 s; 46.2 s in the previous run). `Exit and stop tasks`
stopped the SDK child (last row 9.6 s) but not its detached grandchild, which
sent 95 more requests (all 403) after exit began (N). Only this case detaches
a grandchild. The observed process is the `strace -ff` observer that wraps
the native, and it exits only after every traced descendant; so either the
native waited for the detached descendant or only the observer did (I; which
one is U). Either way an in-boundary descendant outlived the native's own
task stop by about 36 s, which bears on the port holder (case 4).

Native renders the refusal as `Please run /login - API Error: 403 <gate
message>` (N; native's separator is a non-ASCII middle dot, written `-` here):
the login hint is misleading. An exploratory run outside the evidence set
(raw rows kept only in the ignored cache, not bound by the manifest) showed
that 400 renders without the hint and that 429 makes native retry with
backoff up to 10 attempts (unretained, treat as I); the refusal status is a
G0 UX item.

**2 Queued child.** The child and grandchild report 200 before and 403 after
the latch, never 200 after; gate ground truth agrees; their direct attempts
to the upstream port were EACCES (N). Background tool children have no
controlling terminal: `/dev/tty` gives ENXIO (N, case 5).

**3 Monitor loss.** SIGKILL: latch 0.5 ms after the kill (socketpair EOF).
EOF with the process alive: 0.2 ms. Stall with the channel open: the gate's
timer latched `freshness_expired` 3.91 s after polling stopped (lease 4 s).
Each closed the held root stream, refused the later prompt, sent no upstream
byte after the latch, and left the TUI usable (history banner recorded).
About 2.5 s after each latch, with quota still under the reserve and the
control channel healthy, a control-channel continuation was refused before
any store read or quota fetch (zero usage GETs): `monitor_lost` after SIGKILL
and after EOF, `monitor_stale` after the stall; the gate stayed paused (N).
Offline tests also show that the stale refusal lifts once polls resume and
that the lost refusal does not. In production the monitor is the launcher and
PTY owner, so its death also hangs up the native (I); the split here only
makes the triggers separable.

**4 Gate death and port reuse.** SIGKILL of the gate closed the held stream
(upstream saw the disconnect 196 ms later). While the launcher kept the
listening descriptor, an in-boundary bind of the port (with SO_REUSEADDR and
SO_REUSEPORT) and an outside one both failed EADDRINUSE; the SDK child's
requests sat in the backlog unanswered; upstream count stayed fixed. A new
gate process on the same descriptor started `paused` (`gate_restart`),
accepted the backlog and refused it (6 refusals), and forwarded nothing until
a control-channel continuation ran a fresh same-bearer check; then requests
were admitted again (N). Gap: after the gate died and the launcher released
the descriptor, an in-boundary process bound the port and received a model
request from the confined child, and then an outside impostor bound it and
received one too (N). The in-boundary listener cannot forward (its own
egress is L2a) but can answer the native; the outside impostor receives the
request with its bearer. L2a does not handle bind (NL), and TIME_WAIT gives
no protection against a binder with SO_REUSEADDR. Handling
`LANDLOCK_ACCESS_NET_BIND_TCP` (ABI 4 and later) with no bind rule would
refuse the in-boundary binder's explicit `bind()` of the gate port (NL
scratch check: a fixed-port bind gave EACCES), while `listen()` on an unbound
socket still autobinds a random ephemeral port (NL). It costs little, because
in-boundary listeners are unreachable from inside L2a anyway, but it was not
exercised here (U) and does not stop the outside impostor, so the holder
stays required. The holder must live as long as the invocation, including
every boundary descendant (case 1); if the launcher dies the native should die
with it (I).

**5 Fresh manual continuation.** PC5 `control.py`, pinned by SHA-256
`8b98eee3...dad`, reads only the outer terminal. After the threshold pause:
an F12 typed before the pause was flushed; a challenge pasted in bracketed
paste granted nothing; the key plus the current challenge written three times
on the native PTY's output side (as if the native printed it) reached the
outer display and never the parser; the background SDK child kept getting
403; quota recovery with two paused polls did not reopen. A correctly typed
challenge while quota was still over the reserve called the gate once,
failed `threshold_five_hour`, displayed `Still paused; request a new
confirmation to retry.` and stayed paused. After recovery a new typed
challenge ran one fresh usage and profile check with the current store
bearer and reopened the same gate process and the same native; a prompt typed
through the outer terminal reached the upstream (N). The monitor kept polling
through the pause, so the monitor-health rule (section 1) allowed both checks.
Trusted-terminal assumption as in PA1C: the driver itself types; physical
human action is not attested.

**6 Independent session.** Two invocations, one account and config
directory, reserves 30% and 10%, each with its own gate, port, monitor and
boundary. External consumption to 75% paused the first (25% left) while the
second forwarded 11 more requests; consumption to 93% paused the second (N).
Each invocation polls separately: two GETs per interval each (usage and
profile), a server-load cost (U for real rate limits).

**7 and 8a Rotation through the refresh tunnel.** Store token A expiring in
150 s (inside native's refresh window, outside the kit reader's 30 s skew):
native refreshed before its first model request through
`CONNECT platform.claude.com:443` at the gate (fixture CA trusted only by the
child through `NODE_EXTRA_CA_CERTS`) and replaced the store by rename. Its
first request carried A2; the gate held it, fetched usage and profile with A2
and admitted it after a 1.9 ms hold; the upstream saw only A2. The harness
then wrote B (another account) into the store while the SDK child looped: its
next request carried B; the gate fetched with B (2.1 ms hold), the profile
mismatched, the session paused `bearer_identity_mismatch`; every refused
request after the switch carried B and zero B bytes reached the upstream (N).
CONNECT from inside: `platform.claude.com:443` 200 while open, 403 while
paused; `api.anthropic.com:443` and `platform.claude.com:80` always 403 (N).

**8b No refresh path.** No proxy, token expiring 45 s after start. Native's
refresh attempts failed at name resolution (UDP and AF_UNIX sockets denied by
seccomp; strace counted 50 denied UDP sockets from native). Native kept
sending A after expiry: its two tool-bearing requests after it (45.8 s, before
the later prompt, and 48.2 s, the later prompt) carried bearer A, and all 33
gate decisions after the expiry carried A (N, bearer labels on refused
requests). At server-side expiry the fake usage server revoked A; the next
poll's profile GET failed and the gate paused `server_identity_unavailable`
0.42 s later, before the lease deadline; every native request after that was
refused (N). This configuration is one the launcher's token-window check
would refuse at startup (45 s left, no tunnel; section 5): the case models a
token that expires mid-session.

**Option (a), refresh before launch.** `claude auth status` in a boundary
whose only reachable port is an exact-host tunnel refreshed a token expiring
in 150 s and an already expired one, each with one token request and no model
request, in 0.17 and 0.18 s; it also tried `api.anthropic.com:443`, refused
(N). So a launcher can refresh natively, model-free, before the preflight,
and a session can then run without any in-session CONNECT; sessions longer
than the token lifetime end in a stale pause (I).

**Pause longer than the token's remaining life (I, not exercised).** Neither
design refreshes during a pause: 8a admits the tunnel only while a lease is
open, and (a) refreshes only before launch. If the access token expires while
the session is paused (for example while it waits hours for the five-hour
reset): the native cannot refresh (CONNECT refused while paused under 8a, no
path under (a)); paused polls use the lease bearer (S, QC `monitor()`), so
once the usage server rejects it they no longer show when quota recovers; and
every typed continuation reads the store, finds the expired token and fails
`expired_native_refresh_required` (QC reader, 30 s skew; the same refusal is
N at startup, case 11). The same invocation then cannot be continued under
either design. The live token lifetime is U. G0 options and costs:
(1) allow the exact refresh tunnel while paused, Messages still refused:
continuation stays possible, but an uninspected TLS path to
`platform.claude.com` stays open for the whole pause, and whether that host
serves model traffic is U (below); (2) a launcher-run option-(a) refresh just
before the continuation check: model-free and shown at startup, but a second
native process then rotates the store the paused native also uses, and
whether the running native adopts a token rotated by another process is U;
(3) exit and relaunch as the explicit recovery: simplest, gives up
in-session continuation after long pauses. None of the three was exercised.

**Refresh host and model traffic.** Native uses `platform.claude.com` only
for `TOKEN_URL` (`/v1/oauth/token`) and console/documentation links (S,
binary text). An open tunnel lets any in-boundary process send arbitrary TLS
to that host while the lease is open; whether that host serves Messages or
other quota-consuming APIs is U. This is a G0 cost of design 8a; design (a)
avoids it.

**9 Running tools.** A foreground native Bash loop and a background
test-like process both kept writing ticks after the latch (6 to 10 and 5 to 9
ticks in 2 s). The background process's gate requests got 200 before and 403
after; its direct attempts were EACCES throughout. One Ctrl-C during the
foreground loop ended that shell (its process was gone 3 s later, before its
completion marker; `Interrupted - What should Claude do instead?`),
consistent with PA1T; the background task kept running and the native stayed
alive. On exit native offered `Exit and stop tasks`, `Move to background and
exit` or `Stay`; exit stopped the background task (N), but not the detached
grandchild of case 1. The `Move to background` path was not exercised:
protected mode must refuse it or prove the session stays inside the
invocation (U, gate).

**10 Git and package workflows.** Inside L2a, before and after the pause:
`git clone http://127.0.0.1:PORT/repo.git` failed (`Couldn't connect to
server`, EACCES), the pip-style index download failed EACCES, and curl to an
HTTPS index through the inherited `HTTPS_PROXY` got `CONNECT tunnel failed,
response 403` from the gate. Outside the boundary the same git clone and
index download succeeded (control, N). The HTTPS check has no outside
control: without a proxy it would need a resolver lookup, so the harness
skips it there. Expected L2a cost (NL): no non-gate exit at any time, not
only during a pause.

**11 Startup refusals.** With the real gate process and fake usage server:
`threshold_five_hour`, `window_unavailable` (five-hour row null),
`quota_200` (malformed body), `quota_503`, `quota_429`,
`quota_row_unproved` (a `limits[]` row), `server_identity_unavailable` (no
account for the bearer), `clock_anomaly` (server clock one hour behind) and
`expired_native_refresh_required` (store token expired) each exited 3 before
the native was started (N). On the composed launch path (`launch()`, which
every native case passes too), the launcher checks refused an older native
(`2.0.0`, a fake binary), `-p`, `-cp`, `--bare`, and a caller
`ANTHROPIC_BASE_URL` or `CLAUDE_CODE_OAUTH_TOKEN`, before the gate or the
native started (N). Offline (pure-function) evidence only: the token-window
check (a token inside native's 300 s refresh window with no tunnel), because
the launcher never reads the store, so in a product it belongs in the gate's
startup next to QC's expired-token refusal; the host checks beyond this
host's own facts; and the launcher's socket-stdio check (NL's boundary also
refuses socket stdio as a real process, exit 2). The mode check refuses any
single-dash option cluster that contains `p`; it fails closed when a short
option's attached value contains `p`. Upstream saw zero requests (N).

**Native auxiliary traffic.** Profile, bootstrap, feature, telemetry and
update calls ignore `ANTHROPIC_BASE_URL` (QC). With `HTTPS_PROXY` at the gate
they arrive as CONNECT and are refused: `api.anthropic.com:443` eight or
nine times within 0.5 s of startup, then at intervals of 0.3 to 5 s (10 to 17
per invocation in these 5 to 46 s runs), plus one each to
`raw.githubusercontent.com`, `downloads.claude.ai` and `github.com` (a
`git-remote-https` helper). Without a proxy they fail at resolution. Native
showed no user-visible error and kept working (N). Startup also sends two
model requests before the user's turn: a non-streaming `max_tokens: 1`
request and a title request (N); both are admitted and counted like any
other.

**Native sandbox inside L2a.** With `sandbox.enabled`, native started
`bwrap`, then reported `Sandbox is enabled but failed to initialize:
EAFNOSUPPORT ... listen .../srt-mux-*.sock. Sandboxing is disabled for the
rest of this session; restart to retry.`; the command did not run (N).
Later commands would run unsandboxed (S, native text). Protected mode should
set the native sandbox off explicitly.

**Outer write rule.** A second Landlock domain (fsrules.py) handling every
write-class right, allowing them only beneath the workspace, temp, native
state directories, the config directory, `/dev/pts`, `/dev/null` and
`/dev/tty`, stacked under L2a: native ran normally; the tool wrote inside the
workspace and temp and got `permission denied` outside (N). Costs: writes to
other repositories, global caches, `~/.gitconfig` or package stores fail;
reads stay unrestricted; device ioctls are not handled; REFER and TRUNCATE
need ABI 2 and 3 (S). It replaces only the native sandbox's write
confinement, not its per-command prompts.

## 4. Measurements and proposals for G0

Loopback values from the final run (`measure.py`); they bound the kit's own
overhead only (N). Proposals are I.

| Quantity | Observed | Proposal |
| --- | --- | --- |
| Gate spawn to ready (python start, bearer read, preflight) | median 188 ms (n 13) | budget 2 s locally plus the live GET bound |
| Preflight (usage and profile GETs) | median 9.9 ms | live: QC 10 s bound per GET |
| Usage GET / profile GET | median 1.4 / 1.0 ms | live latency U |
| Poll (usage and profile, revalidate) | median 2.8 ms | interval 15 to 30 s live (U for server limits) |
| Admission decision, no rotation | median 0.08 ms, max 0.62 ms (n 232) | negligible on loopback |
| Request head to upstream send (connect plus locked write) | median 0.23 ms, max 2.0 ms (n 233) | loopback only; see the lock cost below |
| Rotation hold (two GETs) | 1.9 to 2.1 ms | live: up to 2 x 10 s; lock held meanwhile |
| Latch to upstream stream close | 146 to 185 ms (fixture ping 100 ms) | close is immediate at the gate |
| Monitor loss to latch | kill 0.5 ms, EOF 0.2 ms, stall 3.91 s | stall bound = max observation age |

Lock cost (I, G0 cost). The request head and body (up to 8 MB) are written
upstream under the invocation's single admission lock; that is what makes a
latch leave zero upstream bytes. The connection setup runs before the lock
and sends no request byte, and `live()` re-checks the lease deadline under
the lock, so a forward never follows expiry by a timer tick. In a product the
locked write is TLS through the launcher proxy: a slow upstream write
serializes the invocation's requests and delays latch, tick and poll by up to
the write time, and QC's rotation hold (two live GETs) holds the same lock.
The loopback medians do not show this. Options: a per-write timeout under
the lock, or chunked writes that re-check `live()` per chunk (a latch then
leaves at most a truncated request rather than zero bytes).

Descendant lifetime (N for the observation, U for the mechanism). A detached
in-boundary descendant outlived the native's `Exit and stop tasks` by about
36 s (case 1) and kept talking to the gate. The port holder and the gate must
therefore outlive every boundary descendant, or the launcher must end the
whole boundary process tree when the invocation ends; how (for example a
cgroup, a child subreaper or a pidfd sweep) is U.

Proposed bounds (I): query timeout 10 s per GET (QC); poll interval 15 s
with jittered backoff to 60 s after failures, never extending the lease;
maximum observation age 60 s, so a stalled monitor or failing endpoint pauses
within one minute and a continuation waits for a monitor heard within that
age; revalidation budget 4 per minute (QC) and a pause when it is spent.
These trade pause latency against two requests per interval per invocation;
live server limits and latency are U.

## 5. Refusal conditions (before the native starts)

- Host: not Linux; Landlock unavailable or ABI below 4; seccomp architecture
  other than x86_64; socket stdio; `legacy_tiocsti` not 0 or unknown (NL).
- Native: version other than the reviewed set (2.1.322 here; AGENTS.md wants a
  minimum, but the QC policy review and this composition are
  version-specific, a G0 tension); options `-p/--print` (also inside a
  combined short cluster such as `-cp`), `--bare`, `--bg/--background`,
  `--remote-control`, `--cloud`, `--teleport`, `--desktop`, `--environment`,
  `--chrome`, `--ide`, `--plugin-url`, `--plugin-dir`, `--file`,
  `--mcp-config`, `--settings`, `--setting-sources`, `--from-pr` (research
  list, S from `--help`).
- Environment: caller provider, token, base-URL or store selectors (QC list
  plus `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`); a different
  `CLAUDE_CONFIG_DIR` spelling.
- Credential: store unreadable, not private, rotating, malformed or expired
  (QC reader, in the gate); without a refresh tunnel, a token inside native's
  300 s refresh window (option a: refresh first; a gate startup check in a
  product, offline only here).
- Quota: any preflight failure (status, transport, malformed, missing window,
  unproved row, threshold or equality, identity mismatch, clock anomaly).
- Gate: not ready within the startup bound; the native is never started.

## 6. Shared contract for C, X and A

Policy evaluator inputs: registered identity (account, organization),
reserves per window, allowed models and family mapping, maximum observation
age, clock tolerance, server skew, revalidation budget; observation =
normalized windows with receive times, other and unproved rows, server date.

Lease API (in the gate process): `startup(bearer)`, `admit(headers, model,
close) -> Decision` before any upstream byte, `admit_connect(target, allow,
close)`, `monitor()`, `tick()` (deadline), `latch(reason)`,
`monitor_heard()` (each poll line), `monitor_gone(reason)` (channel EOF),
`restart_paused(reason)`, `issue_continuation()`, `continuation_refusal()`
and `continue_manually(token, bearer)` from the control channel only,
`release(id)`; plus two rules. Upstream writes happen under the lock for a
live stream whose lease deadline has not passed. Continuation resumes only
while control remains healthy: after monitor EOF it answers `monitor_lost`
until a new gate process with a new monitor channel; while the monitor sent
no poll within the maximum observation age it answers `monitor_stale`; both
before any store read or quota fetch, and a refused attempt voids its token.

Gate channels: inherited listening descriptor (owned by the launcher for the
invocation's lifetime, including its descendants); monitor socketpair
carrying `poll` lines, EOF means latch and monitor lost; control socketpair
carrying JSON `{"op":"continue"}` and `{"op":"status"}` with JSON replies
(status includes `monitor_lost`); JSON event lines on stdout (`gate_ready`,
`startup_refused`, `decision` with a bearer fingerprint, `forwarded`,
`aborted_before_upstream`, `connect`, `poll`, `monitor_eof`, `controller`
with QC event kinds plus `continuation_refused`). Exit 3 means startup
refusal.

## 7. Costs

- Kit-owned code: the composition here is about 1,000 research lines on top
  of QC (admission, usage, bearer) and NL (boundary); a product version adds
  per-version policy review, syscall tables and a supervisor that ends the
  boundary tree. This is the kit-owned sandbox and gateway category PO asked
  G0 to accept explicitly.
- Network: everything except the gate is closed for the whole session (NL
  Q3), including git remotes, package installs, ssh and local servers; AF_UNIX
  IPC breaks (NL Q2).
- Requests: two extra GETs per poll per invocation (option 3), two more per
  rotation; native's own startup probe and title requests count against the
  quota like any request.
- Refresh: design 8a opens a TLS tunnel to `platform.claude.com:443` the
  gate cannot inspect; design (a) needs a pre-launch native run and pauses
  long sessions at expiry. Under both, a pause that outlasts the token's
  remaining life cannot be continued in the same invocation (I): recovery
  needs the tunnel while paused, a launcher refresh before the continuation
  check, or exit and relaunch (section 3).
- Native customizations: `--safe-mode` is harness-only. If G0 made it part of
  protected mode, sessions lose CLAUDE.md, skills, plugins, hooks, MCP
  servers and custom agents. Without it they run inside L2a, where any
  network they need beyond the gate fails (NL Q3) and any model request they
  make goes through the gate (I; not exercised, section 8).
- Admission lock: the upstream write of every request holds the
  invocation's single lock (section 4), a latency cost for large bodies and
  slow TLS upstreams that loopback does not show (I).
- Descendants: detached in-boundary descendants outlive the native's task
  stop; the holder and gate, or a tree kill, must cover them (section 4).
- UX: refusals render as `Please run /login - API Error: 403 ...`; the
  continuation key and challenge live outside native's UI; `Move to
  background` must be refused.

## 8. Proved, inferred, unproved

- **N (fixtures, this host, 2.1.322):** all verdicts above; the admission
  invariant across the 12 native stages (13 invocations), with every upstream
  arrival matched to a gate forward; continuation refused after monitor loss
  and after a stall; launcher refusals on the composed launch path; native
  refresh through the tunnel and model-free pre-launch refresh; native keeps
  using an expired token without a refresh path (bearer labels); a detached
  grandchild outlives `Exit and stop tasks`; background tool children lack a
  controlling terminal; native sandbox failure inside L2a; the outer write
  rule works for a short session.
- **S:** native refresh threshold `now + 300000 >= expiresAt` (binary offset
  286681302); `platform.claude.com` used only for the token endpoint and
  links; option list from `--help`; QC paused polls use the lease bearer.
- **I:** production launcher-as-monitor semantics; proposed bounds; that a
  holder-less design needs the native to die with the launcher; classifier
  traffic in auto mode; that a pause past token expiry cannot be continued
  in-session; the cause of the slow exit in case 1 (the native or only the
  `strace` observer waited for the detached grandchild); the lock cost for
  large or slow upstream writes; that hooks, MCP servers and plugins would
  inherit L2a and reach only the gate.
- **U:** live usage/profile availability and limits for a full-login bearer
  (QC); live latency and token lifetime; whether `platform.claude.com` serves
  model traffic; hooks, MCP servers and plugins under the composed boundary
  and gate without `--safe-mode` (PA1C covered native `Agent` subagents, not
  hooks, MCP or plugins); local slash commands while paused; `Move to
  background and exit`; the three long-pause recovery options; whether a
  running native adopts a store token rotated by another process; Landlock
  bind handling against the in-boundary binder; how a launcher ends the
  boundary tree; the token-window check inside the gate; long sessions under
  the write rule; other native versions; macOS (KM) and WSL2; multiple
  monitors per account; the native's behavior after many hours of 403s.

## 9. Remaining human gates

G0: accept L2a and the kit-owned gate (NL Q1 to Q4); QC option 3 read surface;
refresh design 8a (tunnel) versus (a) (pre-launch refresh with stale pause)
or both, and the recovery from a pause that outlasts the token (tunnel while
paused, launcher refresh before continuation, or exit and relaunch);
`--safe-mode` in protected mode (lost customizations) or not (hooks, MCP and
plugins then need their own composed run); refusal status and message;
version pinning policy; the write rule as the native sandbox replacement;
`Move to background` refusal; ending the boundary tree at invocation end;
the lock and write-timeout policy; poll interval, maximum age and budget.
Live gates: N1-bearer usage and profile for a real account (QC draft), then
a live K run. Platform gates: KM (macOS) and WSL2.

## 10. Reproduction

```sh
K=docs/superpowers/plans/quota-reserve-probes/klc
python3 -I -B $K/test_gate.py && python3 -I -B $K/test_compose.py
python3 -I -B $K/run.py            # 14 native stages, about 4 minutes
python3 -I -B $K/check_evidence.py && python3 -I -B $K/test_evidence.py
python3 -I -B $K/measure.py
pre-commit run --all-files
```

`run.py` gives each stage its own process group and timeout, records
temporary roots in the ignored `.cache/klc/temp-roots.txt`, archives raw rows
and terminal transcripts under `.cache/klc/runs/` (the coordinator should
preserve them before removing the worktree), removes every root, and fails
if a root or probe process remains. The manifest binds each evidence digest
and the code digests (this subtree and the imported QC, NL and PC5 files);
the checker also requires that the manifest names exactly the files `run.py`
binds. The retained `loss-kill.json` comes from a second run of that stage
with unchanged code: in the first, NL's strace parser found no single root
process and so recorded no native exit status, which the checker rejects
(the native's exit and termios restore were observed directly). A failed or
interrupted stage never counts as a pass.

## 11. Containment

The native, its helpers and its tool children ran only inside L2a (Landlock
connect to the gate port, seccomp socket filter). An outside `strace -ff`
observer recorded every socket and connect call of the native tree: no
non-loopback connect; every non-gate connect was EACCES; UDP and AF_UNIX
sockets were denied. Fixtures listened only on 127.0.0.1. Outside the
boundary the harness ran `claude --version` with a scratch HOME (version
query only: once per case on the launch path and once in `run.py`); the
outside control phase of case 10 runs only the loopback git clone and index
download, no HTTPS call and no resolver lookup. The refresh fixture's CA was
disposable and trusted only by the child. No real user daemon was contacted;
no transient unit was created.
