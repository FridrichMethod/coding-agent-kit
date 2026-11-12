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
| 1 Threshold pause | Latch on the next poll after the fake usage crosses the reserve; root, native child and grandchild SSE streams closed; zero upstream bytes after the latch; TUI history view and later prompt work, later prompt refused | `threshold.json` |
| 2 Queued child request | SDK-like tool child and its `setsid` grandchild: admitted before, refused (403) after the latch, never forwarded after it; their direct attempts EACCES | `threshold.json` |
| Native subagent | `Agent` child and grandchild requests go through the gate and are closed and refused like the root | `threshold.json` |
| 3 Monitor loss | SIGKILL and EOF latch in under 1 ms; stall latches at the 4 s lease deadline via the gate's own timer; zero upstream bytes after; TTY usable | `loss-*.json` |
| 4 Gate death, port reuse | Gate death closes streams; the launcher's held listening socket keeps the port (in-boundary and outside impostor binds EADDRINUSE, requests wait in the backlog, zero upstream bytes); a restarted gate starts paused and refuses the backlog until a fresh verified continuation. Without the holder both impostors bind and receive the confined client's request (gap shown) | `gate-death.json` |
| 5 Fresh manual continuation | Stale typed input, bracketed paste, bytes on the native's output side and background wakeups grant nothing; a typed challenge with a failing check stays paused with the deterministic message; a fresh two-window same-bearer check reopens the same gate and native | `continuation.json` |
| 6 Independent session | Second invocation of the same account with a lower reserve keeps forwarding when the first pauses; outside consumption pauses it later | `independent.json` |
| 7 Rotation | Native refresh A to A2 (same account) is held, re-checked with A2 and admitted; A2 to B with a child request queued pauses with zero B bytes; old quota never authorized a new bearer | `rotation.json` |
| 8a Refresh tunnel | Native refreshes through `CONNECT platform.claude.com:443` at the gate while the lease is open; other hosts and ports refused; closed on pause | `rotation.json` |
| 8b No refresh path | Native keeps sending the expired token; the first failed same-bearer revalidation pauses (about 0.4 s after server-side expiry) | `norefresh.json` |
| 8 option (a) | `claude auth status` inside a refresh-only tunnel refreshes a near-expiry or expired token model-free in about 0.2 s | `prelaunch.json` |
| 9 Running tools | Foreground and background local work continue through the pause; their gate requests get 403 and direct egress EACCES; one Ctrl-C ends the foreground shell, not the background task; native exit stops background tasks | `tools.json` |
| 10 Git and package | git over HTTP and a pip-style download fail inside L2a before and after the pause (EACCES); HTTPS through the inherited proxy gets the gate's 403; outside control succeeds | `tools.json` |
| 11 Startup refusals | Unsupported version and mode, conflicting environment, token window, missing/stale/malformed/unavailable/unsupported-family/threshold quota, expired token and socket stdio refuse before any native start, zero model requests | `refusals.json` |
| Native sandbox | `sandbox.enabled` fails to initialize inside L2a (AF_UNIX listen denied), the command is refused and native disables sandboxing for the session | `sandbox.json` |
| Outer write rule | Landlock write rule (workspace, temp, config, native state) plus L2a: native works, writes outside are denied | `fsrules.json` |

## 1. Composed architecture

```text
 launcher (trusted; here the harness)                        outside the boundary
  |- refusal checks (host, version, mode, env, token window)   compose.py
  |- bind 127.0.0.1:0 -> gate port P; keep the listening fd    port holder
  |- spawn gate(fd P, monitor socketpair, control socketpair)  gate.py
  |     gate: read bearer memory-only from the native store;
  |           same-bearer usage + profile preflight (both windows); else exit 3
  |- spawn monitor(socketpair end): "poll" every interval     monitor.py
  |- PTY: boundary.py --gate-port P --seccomp inet -- claude  NL L2a, inherited
        claude --safe-mode --permission-mode dontAsk --settings
          '{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:P"},...}'
        env HTTPS_PROXY=http://127.0.0.1:P NO_PROXY=127.0.0.1,localhost
 gate -> fake upstream (Messages), fake usage/profile, refresh fixture (tunnel)
```

- Bearer custody: only the gate process holds the bearer (QC option 3). The
  monitor holds none; the launcher never reads the store. Any in-boundary
  process of the same user can read the store too (by design: SDK children
  use it); it gains nothing beyond the native's own quota, because every
  request still passes the same lease (N, case 2).
- Admission invariant: QC `AdmissionController` decides each Messages request
  before any upstream byte. The gate then writes the request upstream while
  holding the admission lock and only if the stream is still registered
  under an open lease, and emits `forwarded` inside that lock. In every case
  each upstream arrival matches one `forwarded` event and none follows the
  latch (N, checked by `check_evidence.py`).
- K-owned additions to QC (QC section 3 gates): an independent 20 ms timer
  closes everything at the lease deadline without waiting for `admit()`;
  monitor EOF latches; a restarted gate starts `paused` with no lease; a
  startup lease that holds an unproved row refuses at startup instead of per
  request; CONNECT is admitted only for an exact allowlisted `host:port`
  while open and registered like a stream, so a pause closes tunnels.
- Pause closes every registered client and upstream socket; nothing passive
  reopens it; only a one-use continuation from the trusted control channel
  followed by a fresh same-bearer check does (QC continuation, unchanged).
- Wrapper-originated connections: the gate's upstream connections use the
  launcher's `HTTPS_PROXY` captured once at startup when one is configured
  (provisional decision; one case through a counting loopback proxy, all
  forwards tunneled, N). QC's usage client has no proxy support (S), so a
  proxy-only network needs that added (I, cost).
- Native configuration (N): inline `--settings` `env.ANTHROPIC_BASE_URL`
  pins Messages to the gate; `HTTPS_PROXY` is honored for every HTTPS call;
  `--safe-mode` disables hooks, plugins, MCP and custom agents but keeps the
  built-in `Agent` tool; `dontAsk` with `allow: [Bash, Agent]` avoids the
  default auto mode's extra classifier traffic and prompts (I for the
  classifier).

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
five-hour use against a 20% reserve. The next poll (782 ms after the change,
interval 1 s) latched `threshold_five_hour`; the three held streams
disconnected 160 to 196 ms later (fixture ping cadence 100 ms). Within 70 ms
of the latch native sent four requests (one per subagent, two from the root
conversation), all refused 403, and then stopped (no retry storm on 403);
the later prompt sent two more (a tool-less auxiliary request and the root
turn), both refused. All 129 SDK child and grandchild requests after the
latch got 403. Ctrl-O history view, a new prompt via native send-now
(refused at the gate) and normal exit worked; termios restored. Exit was
slow in this case only: native confirmed `Exit and stop tasks` and printed
its resume hint promptly, but its process ended 39 s after the first exit
key, against 4 s in every other case (N); the cause (subagent or background
shell teardown, or flushing refused telemetry) is U. Native renders the refusal as
`Please run /login - API Error: 403 <gate message>` (N; native's separator
is a non-ASCII middle dot, written `-` here): the login hint is misleading.
A scratch run (not in the evidence set) showed 400 renders without the hint
and 429 makes native retry with backoff up to 10 attempts; the refusal
status is a G0 UX item.

**2 Queued child.** The child and grandchild report 200 before and 403 after
the latch, never 200 after; gate ground truth agrees; their direct attempts
to the upstream port were EACCES (N). Background tool children have no
controlling terminal: `/dev/tty` gives ENXIO (N, case 5).

**3 Monitor loss.** SIGKILL: latch 0.6 ms after the kill (socketpair EOF).
EOF with the process alive: 0.2 ms. Stall with the channel open: the gate's
timer latched `freshness_expired` 3.97 s after polling stopped (lease 4 s).
Each closed the held root stream, refused the later prompt, sent no upstream
byte after the latch, and left the TUI usable. In production the monitor is
the launcher and PTY owner, so its death also hangs up the native (I); the
split here only makes the triggers separable.

**4 Gate death and port reuse.** SIGKILL of the gate closed the held stream
(upstream saw the disconnect in about 150 ms). While the launcher kept the
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
request with its bearer. Landlock does not handle bind here (NL), and TIME_WAIT
gives no protection against a binder with SO_REUSEADDR. The holder must live
as long as the invocation; if the launcher dies the native should die with it
(I).

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
through the outer terminal reached the upstream (N). Trusted-terminal
assumption as in PA1C: the driver itself types; physical human action is not
attested.

**6 Independent session.** Two invocations, one account and config
directory, reserves 30% and 10%, each with its own gate, port, monitor and
boundary. External consumption to 75% paused the first (25% left) while the
second forwarded 12 more requests; consumption to 93% paused the second (N).
Each invocation polls separately: two GETs per interval each (usage and
profile), a server-load cost (U for real rate limits).

**7 and 8a Rotation through the refresh tunnel.** Store token A expiring in
150 s (inside native's refresh window, outside the kit reader's 30 s skew):
native refreshed before its first model request through
`CONNECT platform.claude.com:443` at the gate (fixture CA trusted only by the
child through `NODE_EXTRA_CA_CERTS`) and replaced the store by rename. Its
first request carried A2; the gate held it, fetched usage and profile with A2
and admitted it after a 1.8 to 2.0 ms hold; the upstream never saw A. The
harness then wrote B (another account) into the store while the SDK child
looped: its next request carried B; the gate fetched with B, the profile
mismatched, the session paused `bearer_identity_mismatch`; zero B bytes
upstream (N). CONNECT from inside: `platform.claude.com:443` 200 while open,
403 while paused; `api.anthropic.com:443` and `platform.claude.com:80`
always 403 (N).

**8b No refresh path.** No proxy, token expiring 45 s after start. Native's
refresh attempts failed at name resolution (UDP and AF_UNIX sockets denied by
seccomp; strace counted 50 denied UDP sockets from native) and it kept
sending A, after expiry too (N). At server-side expiry the fake usage server
revoked A; the next poll's profile GET failed and the gate paused
`server_identity_unavailable` 0.36 s later, before the lease deadline. The
later prompt reached the gate and was refused (N).

**Option (a), refresh before launch.** `claude auth status` in a boundary
whose only reachable port is an exact-host tunnel refreshed a token expiring
in 150 s and an already expired one, each with one token request and no model
request, in about 0.2 s; it also tried `api.anthropic.com:443`, refused (N).
So a launcher can refresh natively, model-free, before the preflight, and a
session can then run without any in-session CONNECT; sessions longer than
the token lifetime end in a stale pause (I).

**Refresh host and model traffic.** Native uses `platform.claude.com` only
for `TOKEN_URL` (`/v1/oauth/token`) and console/documentation links (S,
binary text). An open tunnel lets any in-boundary process send arbitrary TLS
to that host while the lease is open; whether that host serves Messages or
other quota-consuming APIs is U. This is a G0 cost of design 8a; design (a)
avoids it.

**9 Running tools.** A foreground native Bash loop and a background
test-like process both kept writing ticks after the latch (6 to 9 and 5 to 9
ticks in 2 s). The background process's gate requests got 200 before and 403
after; its direct attempts were EACCES throughout. One Ctrl-C during the
foreground loop ended that shell before its completion marker (`Interrupted
- What should Claude do instead?`), consistent with PA1T; the background
task kept running. On exit native offered `Exit and stop tasks`, `Move to
background and exit` or `Stay`; exit stopped the background task (N). The
`Move to background` path was not exercised: protected mode must refuse it
or prove the session stays inside the invocation (U, gate).

**10 Git and package workflows.** Inside L2a, before and after the pause:
`git clone http://127.0.0.1:PORT/repo.git` failed (`Couldn't connect to
server`, EACCES), the pip-style index download failed EACCES, and curl to an
HTTPS index through the inherited `HTTPS_PROXY` got `CONNECT tunnel failed,
response 403` from the gate. The same workflows outside the boundary
succeeded (N). Expected L2a cost (NL): no non-gate exit at any time, not only
during a pause.

**11 Startup refusals.** With the real gate process and fake usage server:
`threshold_five_hour`, `window_unavailable` (five-hour row null),
`quota_200` (malformed body), `quota_503`, `quota_429`,
`quota_row_unproved` (a `limits[]` row), `server_identity_unavailable` (no
account for the bearer), `clock_anomaly` (server clock one hour behind) and
`expired_native_refresh_required` (store token expired) each exited 3 before
the native was started. Launcher checks refused version `2.0.0`, `-p` and
`--bare`, a caller `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_BASE_URL`, a token
inside native's 300 s refresh window when no tunnel is configured, and socket
stdio (NL's boundary also refuses it, exit 2). Upstream saw zero requests (N).

**Native auxiliary traffic.** Profile, bootstrap, feature, telemetry and
update calls ignore `ANTHROPIC_BASE_URL` (QC). With `HTTPS_PROXY` at the gate
they arrive as CONNECT and are refused: `api.anthropic.com:443` eight or
nine times within 0.5 s of startup, then every 0.5 to 5 s (10 to 17 per
invocation in these 6 to 48 s runs), plus one each to
`raw.githubusercontent.com`, `downloads.claude.ai` and `github.com` (a
`git-remote-https` helper). Without
a proxy they fail at resolution. Native showed no user-visible error and kept
working (N). Startup also sends two model requests before the user's turn: a
non-streaming `max_tokens: 1` request and a title request (N); both are
admitted and counted like any other.

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
| Gate spawn to ready (python start, bearer read, preflight) | median 190 ms (n 13) | budget 2 s locally plus the live GET bound |
| Preflight (usage and profile GETs) | median 10.1 ms | live: QC 10 s bound per GET |
| Usage GET / profile GET | median 1.1 / 0.7 ms | live latency U |
| Poll (usage and profile, revalidate) | median 2.2 ms | interval 15 to 30 s live (U for server limits) |
| Admission decision, no rotation | median 0.08 ms, max 0.69 ms (n 232) | negligible |
| Request head to upstream send | median 0.22 ms, max 1.9 ms (n 233) | negligible |
| Rotation hold (two GETs) | 1.8 to 2.0 ms | live: up to 2 x 10 s; lock held meanwhile |
| Latch to upstream stream close | 122 to 196 ms (fixture ping 100 ms) | close is immediate at the gate |
| Monitor loss to latch | kill 0.6 ms, EOF 0.2 ms, stall 3.97 s | stall bound = max observation age |

Proposed bounds (I): query timeout 10 s per GET (QC); poll interval 15 s
with jittered backoff to 60 s after failures, never extending the lease;
maximum observation age 60 s, so a stalled monitor or failing endpoint pauses
within one minute; revalidation budget 4 per minute (QC) and a pause when it
is spent. These trade pause latency against two requests per interval per
invocation; live server limits and latency are U.

## 5. Refusal conditions (before the native starts)

- Host: not Linux; Landlock unavailable or ABI below 4; seccomp architecture
  other than x86_64; socket stdio; `legacy_tiocsti` not 0 or unknown (NL).
- Native: version other than the reviewed set (2.1.322 here; AGENTS.md wants a
  minimum, but the QC policy review and this composition are
  version-specific, a G0 tension); options `-p/--print`, `--bare`,
  `--bg/--background`, `--remote-control`, `--cloud`, `--teleport`,
  `--desktop`, `--environment`, `--chrome`, `--ide`, `--plugin-url`,
  `--plugin-dir`, `--file`, `--mcp-config`, `--settings`,
  `--setting-sources`, `--from-pr` (research list, S from `--help`).
- Environment: caller provider, token, base-URL or store selectors (QC list
  plus `ANTHROPIC_BASE_URL`, `ANTHROPIC_CUSTOM_HEADERS`); a different
  `CLAUDE_CONFIG_DIR` spelling.
- Credential: store unreadable, not private, rotating, malformed or expired
  (QC reader); without a refresh tunnel, a token inside native's 300 s refresh
  window (option a: refresh first).
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
`restart_paused(reason)`, `issue_continuation()` and
`continue_manually(token, bearer)` from the control channel only,
`release(id)`; plus the rule that upstream writes happen under the lock for a
live stream.

Gate channels: inherited listening descriptor (owned by the launcher for the
invocation's lifetime); monitor socketpair carrying `poll` lines, EOF means
latch; control socketpair carrying JSON `{"op":"continue"}` and
`{"op":"status"}` with JSON replies; JSON event lines on stdout (`gate_ready`,
`startup_refused`, `decision`, `forwarded`, `connect`, `poll`,
`controller` with QC event kinds). Exit 3 means startup refusal.

## 7. Costs

- Kit-owned code: the composition here is about 1,000 research lines on top
  of QC (admission, usage, bearer) and NL (boundary); a product version adds
  per-version policy review, syscall tables and a supervisor. This is the
  kit-owned sandbox and gateway category PO asked G0 to accept explicitly.
- Network: everything except the gate is closed for the whole session (NL
  Q3), including git remotes, package installs, ssh and local servers; AF_UNIX
  IPC breaks (NL Q2).
- Requests: two extra GETs per poll per invocation (option 3), two more per
  rotation; native's own startup probe and title requests count against the
  quota like any request.
- Refresh: design 8a opens a TLS tunnel to `platform.claude.com:443` the
  gate cannot inspect; design (a) needs a pre-launch native run and pauses
  long sessions at expiry.
- UX: refusals render as `Please run /login - API Error: 403 ...`; the
  continuation key and challenge live outside native's UI; `Move to
  background` must be refused.

## 8. Proved, inferred, unproved

- **N (fixtures, this host, 2.1.322):** all verdicts above; the admission
  invariant across the 12 native stages (13 invocations); native refresh through the tunnel and model-free
  pre-launch refresh; native keeps using an expired token without a refresh
  path; background tool children lack a controlling terminal; native sandbox
  failure inside L2a; the outer write rule works for a short session.
- **S:** native refresh threshold `now + 300000 >= expiresAt` (binary offset
  286681302); `platform.claude.com` used only for the token endpoint and
  links; option list from `--help`.
- **I:** production launcher-as-monitor semantics; proposed bounds; that a
  holder-less design needs the native to die with the launcher; classifier
  traffic in auto mode.
- **U:** live usage/profile availability and limits for a full-login bearer
  (QC); live latency; whether `platform.claude.com` serves model traffic;
  `Move to background and exit`; long sessions under the write rule; other
  native versions; macOS (KM) and WSL2; multiple monitors per account; the
  native's behavior after many hours of 403s.

## 9. Remaining human gates

G0: accept L2a and the kit-owned gate (NL Q1 to Q4); QC option 3 read surface;
refresh design 8a (tunnel) versus (a) (pre-launch refresh with stale pause)
or both; refusal status and message; version pinning policy; the write rule
as the native sandbox replacement; `Move to background` refusal; poll
interval, maximum age and budget. Live gates: N1-bearer usage and profile
for a real account (QC draft), then a live K run. Platform gates: KM (macOS)
and WSL2.

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
and the code digests (this subtree and the imported QC, NL and PC5 files).

## 11. Containment

The native, its helpers and its tool children ran only inside L2a (Landlock
connect to the gate port, seccomp socket filter). An outside `strace -ff`
observer recorded every socket and connect call of the native tree: no
non-loopback connect; every non-gate connect was EACCES; UDP and AF_UNIX
sockets were denied. Fixtures listened only on 127.0.0.1. Outside the
boundary the harness ran `claude --version` with a scratch HOME (version
query only). The refresh fixture's CA was disposable and trusted only by the
child. No real user daemon was contacted; no transient unit was created.
