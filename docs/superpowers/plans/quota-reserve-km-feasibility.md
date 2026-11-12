# KM: composed protected invocations for Claude and Codex on macOS

Status: **all eleven decisive K cases pass for both natives on one macOS host,
under fixtures; nothing here is a G0 approval, a production capability or a
release claim.** One launcher process owns the user's terminal and the gate
port, a separate monitor sends heartbeats, a gate process composes the
reviewed QC (Claude) and QX (Codex) admission contracts, and the real native
runs under the stock `/usr/bin/sandbox-exec` with NM's M1 profile plus
`com.apple.SecurityServer`. With fabricated accounts and loopback fixtures,
every latch (threshold, monitor SIGKILL, monitor EOF, monitor stall, identity
change, token expiry, gate restart) left zero upstream model requests and zero
admitted decisions after it and left the native TUI usable. Where a stream
was held open at the latch (the threshold and the three monitor-loss cases)
the gate closed it within a millisecond; the SDK-like child and its detached
grandchild were refused after the threshold latch, and a child queued across
the A -> B identity change was refused. A fresh typed confirmation on the
trusted terminal plus a fresh same-bearer check reopened the same invocation;
nothing else did. Native refresh worked through an exact-host CONNECT tunnel
while admission was open (fixture CA given explicitly) and was refused after
the pause. The boundary self-test runs before any native process under the
profile, so a failing self-test refuses with nothing native run and nothing
sent upstream. No product runtime changed.

Base: `afffc23` (branch `ci/quota-reserve`, contains the reviewed QC, QX, NL
and NM nodes). Captured November 12, 2026 UTC on macOS 27.2, arm64. Natives:
Claude Code `2.1.322` (SHA-256 `a9d23794...a3450`) and `codex-cli 0.166.0`
(SHA-256 `39acf979...db7c1`); full digests are in every evidence file. Probe
interpreter: Homebrew Python 3.15 on the Mac, Python 3 on Linux for the
offline tests. All tokens, accounts, usage data and model replies are
fabricated.

Labels: N = native observation in this node; S = inspected source or
documentation; I = inference; U = unproved.

## Provisional decisions used (each remains a G0 choice)

From `L/nodes/K/brief.md` and `L/pending-user-questions.md` (unanswered):

- macOS mechanism: stock `sandbox-exec` with a kit SBPL renderer (NM M1) and a
  startup self-test (question 5a).
- `com.apple.SecurityServer` admitted for the native Keychain (question 6a).
  The Claude runs used NM's PATH shim for `/usr/bin/security`, so no real
  Keychain item was ever read or written; the allowance is in the profile but
  was not exercised by a native (see "Native credential store").
- Claude: memory-only accessToken read from the native credential file, same
  bearer usage GET plus same-bearer `/api/oauth/profile` identity (QC option
  3). On macOS the real store is the Keychain (QC option 4); KM used the
  fabricated file fallback exactly as NM did.
- Codex: gate-observed correlation plus hold-and-rebind, always `--no-daemon`,
  no `auth.json` read by the kit.
- Same-identity rotation with a fresh passing check keeps admitted streams;
  any failed revalidation, threshold, monitor loss or stale lease closes all.
- Native sandboxes off in protected mode (question 8a); outer write rules
  evaluated as a replacement.
- Refresh: both (a) native refresh before launch and (b) the exact-host
  CONNECT tunnel while admission is open were measured (question 7).

## Composed architecture (as run)

```text
user terminal (outer PTY)
   |
launcher.py  (outside; trusted terminal owner, port holder)
   |  1. pure refusals: host, sandbox-exec, native version (minimum), args, env, quota policy
   |  2. holder: listen 127.0.0.1:P and [::1]:P; bind (no listen, SO_REUSEADDR)
   |     0.0.0.0:P, [::]:P and every interface address from `ifconfig -a`
   |  3. render profile; monitor.py (outside) <- socketpair -> gate.py (outside)
   |     with the holder's listening sockets, a control socketpair to the launcher
   |       Claude: gate reads the bearer memory-only, usage + profile preflight
   |               (wrapper code outside the boundary)
   |  4. self-test under the profile (gate ok, other port EPERM, bind EPERM,
   |     TIOCSTI EPERM), refuse on any mismatch; no native code has run under
   |     the profile before this point
   |       Codex:  only then one fresh `codex app-server` under the profile; its
   |               account/rateLimits/read reaches the gate, which binds epoch 1
   |  5. inner PTY; exec sandbox-exec -p PROFILE native ... with fds 0-2 only;
   |     relay through PC5 TerminalControl (F12 + fresh typed challenge)
   v
native (inside Seatbelt) --TCP 127.0.0.1:P only--> gate --loopback--> fake upstream
```

Profile (NM M1 plus SecurityServer; `TAG`, `P` and the real-home path are
filled per invocation; resolved paths only):

```text
(version 1)
(allow default)
(deny network* (with message TAG))
(allow network-outbound (remote tcp "localhost:P"))
(deny job-creation (with message TAG))
(deny lsopen (with message TAG))
(deny appleevent-send (with message TAG))
(deny mach-lookup (with message TAG))
(allow mach-lookup (global-name "com.apple.FSEvents"))
(allow mach-lookup (global-name "com.apple.PowerManagement.control"))
(allow mach-lookup (global-name "com.apple.SecurityServer"))
(allow mach-lookup (global-name "com.apple.SystemConfiguration.DNSConfiguration"))
(allow mach-lookup (global-name "com.apple.SystemConfiguration.configd"))
(allow mach-lookup (global-name "com.apple.cfprefsd.agent"))
(allow mach-lookup (global-name "com.apple.cfprefsd.daemon"))
(allow mach-lookup (global-name "com.apple.logd"))
(allow mach-lookup (global-name "com.apple.system.notification_center"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.libinfo"))
(allow mach-lookup (global-name "com.apple.system.opendirectoryd.membership"))
(deny signal (with message TAG))
(allow signal (target same-sandbox))
(deny file-ioctl (ioctl-command TIOCSTI) (with message TAG))
(deny file-write* (subpath "ANCHOR") (subpath "/private/var/at") (subpath "HOME/Library/LaunchAgents") (with message TAG))
```

Native configuration (N, as run): Claude gets `ANTHROPIC_BASE_URL` pinned to
the gate through inline `--settings` env, `HTTPS_PROXY` set to the gate, a temp
`CLAUDE_CONFIG_DIR`, `--tools Bash` placed before another option (it is
variadic and otherwise swallows the positional prompt, N), and
`sandbox.enabled=false`. Native agents were not exercised on macOS:
`--tools Bash` removes Claude's agent tool and no Codex sub-agent ran. A
native subagent is the same native process or a child of it, so its model
requests take the same pinned gate route under the same profile (I, by
construction; PA1C and PC3 saw native child and grandchild agents send their
model requests to the native's configured base URL on Linux, S). Codex gets
`-c chatgpt_base_url=...` and `-c model_providers.gate.base_url=...`
overrides (so two invocations can share one `CODEX_HOME`), `--no-daemon`,
file credential store,
`danger-full-access`, `supports_websockets=false`, zero request and stream
retries, and `HTTPS_PROXY` set to the gate.

Gate behavior (as run): default-deny route table (Claude `POST /v1/messages`;
Codex `POST /backend-api/codex/responses`, usage GETs, and the model-free
reads native sends, the latter only while running); every upstream byte is
written under the admission lock for a still-registered stream; a latch
closes every registered client and upstream socket under the same lock;
WebSocket upgrades refused; a Codex request or usage read with more than one
`Authorization` or `ChatGPT-Account-ID` header is refused before admission
(model requests also latch `auth_selector_conflict`, as QC does for Claude;
offline test); CONNECT only to an exact allowlisted `host:port` while
admission is open and the monitor alive. Refusals: Claude `403
permission_error`, Codex `503` (never 401, which would start native refresh).

## Decisive cases

Each cell is one native run (the refusal row is ten launcher runs per
provider); all ran under the profile above. Evidence files are in
[quota-reserve-probes/km/evidence/](quota-reserve-probes/km/evidence/).

| Case | Claude 2.1.322 | Codex 0.166.0 | Label |
| --- | --- | --- | --- |
| 1 Threshold pause | latch 301 ms after the fixture crossed the reserve (next 0.5 s poll); held SSE stream closed 0.3 ms after; 0 upstream POSTs after; `/help` works; later turn refused `threshold_five_hour`; history scroll not exercised | latch 62 ms after; stream closed 0.6 ms after; 0 POSTs after; `/status` works; later turn refused; history scroll not exercised | N |
| 2 Queued child request | SDK-like child and detached (setsid, double fork) grandchild: 200 before, 403 after the latch; direct connects to upstream and usage ports EPERM; UDP to the gate port EPERM | same with 503 | N |
| 3 Monitor loss | SIGKILL: latch 3.5 ms; EOF: 0.3 ms; stall: 2.6 s (heartbeat max age 3 s); stream closed, 0 POSTs after, TTY usable | SIGKILL 3.9 ms; EOF 0.7 ms; stall 2.6 s; same | N |
| 4 Gate death and port reuse | gate SIGKILLed; boundary still denies direct egress; in-boundary bind of the gate port EPERM on 127.0.0.1, ::1, 0.0.0.0, ::; every outside bind of the port EADDRINUSE; the native request typed during the death waited in the holder's backlog and the restarted gate refused it (`gate_restart`); continuation reopened | same (`paused:gate_restart`) | N |
| 5 Fresh manual continuation | typed text without F12, a stale challenge, the right challenge inside bracketed paste, recovery of the quota with polls running, and native output all left it paused; a failing fresh check stayed paused with `still paused: threshold_five_hour`; a passing check reopened the same native and gate | same (`threshold:five_hour`) | N |
| 6 Independent session | second invocation (reserve 5) kept working when the first (reserve 30) paused at 20% left; outside consumption to 4% left paused the second | same | N |
| 7 Rotation across a pending request | native refresh A -> A2 (same account, at startup): rotation hold, same-bearer usage plus profile, admitted; store swap to B with the child queued: hold, `bearer_identity_mismatch`, latched; 0 POSTs after | refresh A -> A2 (at startup): native's own `/wham/usage` read with A2 (fresh at +1.067 s) bound epoch 2 before the first A2 model request (+1.152 s), every A2 admission under epoch 2 (startup epoch 1); the gate's hold-and-rebind read did not run (offline test only); B with the child queued: `identity-changed`, latched | N |
| 8a Refresh tunnel | CONNECT `platform.claude.com:443` tunnelled while open (native refresh succeeded, fixture CA via `NODE_EXTRA_CA_CERTS`), 403 after the pause; the native's other CONNECTs (`api.anthropic.com`, `downloads.claude.ai`, `github.com`) refused; a confined client's CONNECTs to other hosts refused | CONNECT `auth.openai.com:443` tunnelled while open (`CODEX_CA_CERTIFICATE`), 403 after the pause; the native's `raw.githubusercontent.com` refused; a confined client's other CONNECTs refused | N |
| 8b No refresh path | refresh CONNECTs refused; at expiry (usage and profile then 401) the next poll latched `server_identity_unavailable` 0.38 s later | 63 refresh CONNECTs refused (mostly at startup) plus one to `raw.githubusercontent.com`; at expiry polls failed and the lease went `stale` after 6.0 s (max age 6 s) | N |
| 9 Running tools | background file writer kept writing through the pause and after native exit; its network to non-gate ports EPERM; foreground loop stopped by Esc (native interrupt killed it) | background writer the same; foreground loop kept running after Esc ("1 background terminal running") | N |
| 10 git and pip | `git clone` over http and `pip download` from loopback fixtures fail under the boundary before and after the pause (git "Failed to connect", pip misleadingly "No matching distribution"); `pip download` with the gate as its proxy fails too (git was not tried through the gate); both work outside | same | N |
| 11 Startup refusals | old version, `-p`, `ANTHROPIC_API_KEY`, unsupported family, revoked quota, one-hour-old server Date, malformed usage, threshold at start, missing sandbox-exec, failing self-test: each refused with a message, exit 3, native never started, 0 model requests | same set (`resume`, `OPENAI_API_KEY`, ...): 0 model requests; a failing self-test refuses before the in-boundary reader (no native process, no upstream request); quota refusals come after the self-test and the reader's model-free GETs | N |

Per-case details:

- Case 1. Claude, after its stream was cut, printed "Streaming response ended
  before any complete data was received. Retrying without streaming." and the
  retry was refused (N). The 403 refusal makes Claude prefix "Please run
  /login" to the gate's message (N): a user-visible cost of 403; a status that
  Claude retries (5xx, 529) would multiply requests instead (I). Codex shows
  "unexpected status 503 Service Unavailable: coding-agent-kit quota gate ..."
  (N). Codex sent no WebSocket (`supports_websockets=false`); the WebSocket
  route is refused by the gate but was not exercised (U).
- Case 2. The child read the bearer from the native store on every request,
  as an SDK child following the store would; the grandchild was reparented by
  `setsid` and a second fork and still went only to the gate (N).
- Case 3. The monitor is a separate process from the terminal owner; killing
  it never touched the TTY (N). Loss latches the gate instance for good; only
  a restarted gate plus continuation could reopen (design; the restart path
  is case 4).
- Case 4. The launcher keeps the listening sockets, so while the gate is dead
  the native's connection waits in the kernel backlog instead of failing, and
  the restarted gate (same port, same descriptors) answers it with a refusal
  (N). The launcher relays the terminal during the restart delay and pauses
  the trusted control at once (a first version blocked the relay during the
  delay, and the later pause flushed the typed input; fixed, N).
- Case 5. PC5's `TerminalControl` was reused unchanged: the launcher alone
  reads the trusted terminal; native output never reaches it. Ctrl-C cancels
  a pending challenge but is also forwarded to the native (twice exits Codex,
  N); Ctrl-U cancels without forwarding. With the gate dead (restart delay,
  or no restart configured) a confirmation answers `still paused:
  gate_unavailable` and the relay keeps running (offline test of the
  launcher's control round trip; not exercised natively). Continuation
  latency from the launcher: Claude 9 to 13 ms (gate-side usage plus
  profile); Codex 64 to 72 ms including one fresh in-boundary
  `codex app-server` read (N).
- Case 7. Both natives refreshed A -> A2 at startup, before any child
  existed, so a child queued across a same-account refresh was not
  exercised; only the A -> B swap had a queued child. For Codex the native's
  own usage read bound A2 first, so the gate's hold-and-rebind read (a new
  same-account bearer arriving with no forwarded read) never ran on macOS; it
  is covered by the offline adapter test only. The checker requires, from the
  rows, an A2 usage GET upstream and a fresh gate-observed A2 read before the
  first A2 admission and every A2 admission under a newer epoch than the
  startup one; moving that read after the admission, or the admission back to
  the startup epoch, fails it (offline mutation tests). Claude wrote the
  refreshed credential to `.credentials.json` after the shimmed Keychain
  write failed: the shim saw `security -i` (commands on stdin, argv only
  recorded), 12 `find-generic-password` lookups of
  `Claude Code-credentials-<8 hex>` and one of `Claude Code-<8 hex>` (N).
  Codex refreshed twice at startup in the prelaunch probe (second grant
  refused, single-use fixture), as KLX saw on Linux (N).
- Case 8. Each native refresh tunnel closed by itself right after the
  exchange, so no native run held a tunnel open across a pause. That the
  latch and monitor loss cut an established tunnel is shown only offline: a
  loopback test drives the real gate handler, opens an allowlisted CONNECT
  tunnel to a byte-counting sink, then latches (and, separately, loses the
  monitor) and finds no further upstream byte, the sink's side closed and a
  new CONNECT refused (offline, Linux; S for the macOS gate, which is the same
  code). Whether `platform.claude.com` or `auth.openai.com` could serve model
  traffic is U: the tunnel is opaque TLS and KM has no evidence either way.
  With the real hosts, Codex's platform roots are empty under M1 (NM), so a
  tunnelled Codex refresh would also need `trustd.agent` admitted or a
  kit-chosen CA bundle (I, carried from NM; not rerun). With
  SecurityServer now admitted, whether Codex loads system roots is U.

## Refresh option (a): native refresh before launch

Run under a profile whose only exit is a CONNECT proxy for exactly the refresh
host (N, [claude-refresh](quota-reserve-probes/km/evidence/claude-refresh.json),
[codex-refresh](quota-reserve-probes/km/evidence/codex-refresh.json)):

- `claude auth status` refreshed both a near-expiry (150 s) and an expired
  token through the tunnel and updated the store; it also tried
  `api.anthropic.com:443` (refused) and still exited 0.
- `codex login status` refreshed nothing. A fresh `codex app-server` with
  `account/read {"refreshToken": true}` refreshed through the tunnel and
  updated `auth.json` (and sent a second refresh with the spent token).

So (a) is feasible for both natives as a model-free pre-launch step; the
launcher would still have to pause or refuse at mid-session expiry, which is
case 8b's path (I).

## Native sandboxes and outer write rules

| Check | Claude | Codex | Label |
| --- | --- | --- | --- |
| Native sandbox on inside the outer profile | "Sandbox is enabled but failed to initialize: EPERM ... listen TMPDIR/srt-mux-*.sock. Sandboxing is disabled for the rest of this session"; the command did not run; Claude told the model later commands run unsandboxed | tool command exit 71, `sandbox-exec: sandbox_apply: Operation not permitted` | N |
| Outer write rules: workspace, account dirs, HOME and TMPDIR writable, everything else read-only | native works and turns are admitted, but the Bash tool fails: `EPERM ... mkdir '/private/tmp/claude-<uid>/...'` (Claude's fixed per-uid temp directory, independent of TMPDIR) | native and tool work; a write outside fails "operation not permitted" | N |
| Same plus `/private/tmp/claude-<uid>` | tool works; the write outside is denied | not needed | N |

The rules are one `(deny file-write* (subpath "/"))` followed by subpath
allowances and the device nodes the natives and the self-test need
(`/dev/null`, `/dev/tty`, `/dev/ptmx`, `/dev/ttys*`, `/dev/fd`,
`/dev/dtracehelper`). They replace native filesystem confinement only
coarsely: no per-command approval, no read restriction, and every writable
directory is writable by every descendant (I). Hooks, MCP servers and other
tools that write elsewhere (caches under `~/Library`, `/private/var/folders`)
were not exercised and would break (U).

## Port holder on macOS (K4 mapping)

Linux used an invocation-owned port holder and Landlock; on macOS the gate
port exit is SBPL `(remote tcp "localhost:P")`, whose `localhost` admits more
than loopback (NM). Measured here (N,
[portmatrix](quota-reserve-probes/km/evidence/portmatrix.json),
[resume](quota-reserve-probes/km/evidence/claude-resume.json)):

| Holder shape | Outside binds of port P that still succeed (same uid) | Confined connect to the host's interface address on P |
| --- | --- | --- |
| listen 127.0.0.1:P and [::1]:P only | 0.0.0.0 and :: with SO_REUSEADDR; the interface address plain, with SO_REUSEADDR, with SO_REUSEPORT | reaches the squatter (accepted) |
| plus non-listening 0.0.0.0:P and [::]:P (SO_REUSEADDR) | the interface address with SO_REUSEADDR (and SO_REUSEPORT) | reaches the squatter |
| plus non-listening binds of every interface address (KM default) | none of the ten tried | no listener answers (connect timed out) |

- SBPL accepts only `*` or `localhost` as the host in a network address
  (sandbox-exec: "host must be * or localhost in network address", N). Of the
  forms that parse, `localhost`, `*` and `tcp4 localhost` all admit the
  interface IPv4 address on the gate port (connect reached the stack and was
  refused, not EPERM); `tcp4` denies ::1, and `tcp6 localhost` denies every
  IPv4 destination, the interface address included (N, `sbpl_forms` in
  portmatrix). A gate on `[::1]` only with a `tcp6` rule would therefore not
  admit the interface IPv4 address; whether it admits the host's global or
  link-local IPv6 addresses on other interfaces, and whether both natives
  accept an `http://[::1]:P` base URL, is U.
- The full holder reserved 21 addresses on this host in the recorded run
  (the wildcards plus one per interface address, link-local ones included;
  the count follows the interfaces present at launch). Addresses that appear after launch (VPN, new
  interface) are not held (U), and a process of another uid was not tried
  (U; outside PO's threat model).
- Inside the boundary every bind is denied (M1 has no network-bind allowance),
  so no confined process can serve the gate port (N).

## Proxy-required networks (provisional decision 3a)

The launcher reads its own `HTTPS_PROXY` once at startup (plain
`http://HOST:PORT` only; anything else refuses) and hands it to the gate for
wrapper-originated upstream connections; the native keeps the gate as its own
proxy (N, [proxy](quota-reserve-probes/km/evidence/proxy.json)). With a
loopback CONNECT proxy fixture in the launcher's environment, every gate
upstream connection went through it as a tunnel to the fake upstream (Claude:
the admitted model request; Codex: model requests and the gate's usage
reads), the native's own traffic never reached it, and the threshold latch
behaved as in case 1 (0 model POSTs after, later turn refused). QC's usage and
profile client does not take a proxy, so the Claude quota reads went direct to
the loopback usage fixture; a production usage client needs the same proxy
(I).

## Codex 0.166.0 notes (versus Linux 0.167.2)

- `codex app-server` on 0.166.0 answers `initialize` and
  `account/rateLimits/read`, through `/backend-api/wham/usage` when
  `chatgpt_base_url` ends in `/backend-api` (N); KM used one fresh process per
  read for the preflight and for continuation (48 to 100 ms each in 16 of
  17 reads, one 0.53 s outlier, N).
- The TUI refuses to start ("account/read failed during TUI bootstrap:
  selected workspace missing from routing discovery") unless
  `/backend-api/wham/accounts/check` lists the account (N in a development
  run, fixture detail).
- Under the profile the TUI needs `--no-daemon` (NM: the daemon path runs
  setuid `ps`, denied). `--no-daemon` behaved as on Linux in every case (N).
- Global `-c` overrides before the subcommand apply to `app-server` too (N).

## Measurements (loopback, this host; not live numbers)

From the committed evidence (`check_evidence.py --measure`):

| Measurement | Claude | Codex | Label |
| --- | --- | --- | --- |
| Preflight (gate start to lease) | 3.9 ms (usage plus profile) | 81 ms (fresh in-boundary app-server read) | N |
| Gate quota GET on loopback (median of the gate's fetches) | 2.6 ms (usage and profile GETs together) | 2.3 ms (usage GETs) | N |
| Admission decision (median, admitted) | 0.30 ms | 0.28 ms | N |
| Decision to first upstream byte (median) | 0.9 ms | 0.6 ms | N |
| Rotation hold to decision | 1.9 ms (A2 admitted), 10.3 ms (B refused) | no hold ran: native's own A2 usage read bound epoch 2 85 ms before the first A2 request; the gate's rebind read is offline-tested only | N |
| Threshold crossing to latch (poll every 0.5 s) | 301 ms | 62 ms | N |
| Latch to stream close | 0.2 to 0.5 ms | 0.5 to 0.7 ms | N |
| Monitor EOF / SIGKILL / stall to latch | 0.3 ms / 3.5 ms / 2.6 s | 0.7 ms / 3.9 ms / 2.6 s | N |
| Self-test under the profile (median) | 40 ms | 43 ms | N |
| Continuation check | 9 to 13 ms | 64 to 72 ms | N |

The crossing-to-latch figure is bounded by where the crossing falls in the
poll cycle, not by the gate (I).

Proposed bounds for G0 (I; none measured against real services):

- Usage query: socket timeout 5 s, total deadline 10 s (QC's values), no retry
  inside one poll; a failed poll never extends the observation age.
- Poll interval 30 s while running, with jittered exponential backoff on
  transient failures up to the interval; maximum observation age 120 s, so
  three consecutive failed polls pause.
- Heartbeat every 1 s, heartbeat maximum age 3 s. The research gate runs one
  usage poll per monitor heartbeat, so these bounds need the liveness
  heartbeat and the usage poll decoupled (a poll timer of its own in the gate
  or the monitor); as run, a 1 s heartbeat would poll every second.
- Revalidation budget (rotation holds) 4 per minute, as QC; a native double
  refresh costs two.
- Startup self-test bounded at 20 s; preflight bounded by the usage deadline.

## Refusal conditions (macOS)

Refuse before any model request when: not macOS; `/usr/bin/sandbox-exec`
missing or not executable; the rendered profile is rejected or the self-test
under it does not show gate connect, other-port EPERM, bind EPERM and TIOCSTI
EPERM; the native version is unrecognized or below the minimum (Claude
2.1.322, Codex 0.166.0 on this host); Claude print (`-p`, `--print`) or
remote modes or a user `--settings`; Codex `resume`, `fork`, `app-server`,
the headless model modes `exec` (alias `e`) and `review`, `--profile` or a
`-c` override of a protected key; any store, provider or base-URL selector in
the environment; the quota policy names an unsupported family or no reserve;
the usage or profile read fails, is malformed, carries a server Date skewed
past 5 minutes, has resets in the past or already crosses the reserve; the
account identity does not match (N for each listed refusal in case 11 except
the `fork`, `exec`, `e`, `review`, `--profile` and `-c` arguments, which are
offline tests only).

Order (N, `refusals`): host, version, argument, environment and policy
refusals come first; apart from `NATIVE --version`, which runs outside any
boundary (a production launcher could run it under the profile after the
self-test, I), they start no process; the gate then starts (the Claude gate
runs its usage plus profile preflight outside the boundary); the self-test is
the first process under the profile, and a failing self-test refuses with no
native process run and no upstream request (both natives). Only after it
passes does Codex run its in-boundary `codex app-server` reader, so Codex
quota refusals follow that reader's model-free GETs (`/backend-api/codex/models`,
`/backend-api/wham/accounts/check`, `/backend-api/wham/usage`). The checker
fails the stage if a non-quota refusal shows an upstream request or a reader
run, or a Codex quota refusal shows no self-test before its reader.

Headless modes are refused rather than gated because a pause there has no
trusted terminal from which to continue; the gate would refuse their
requests after a pause just the same, so supporting them is a later product
choice (I).
A Codex subcommand word counts as one wherever every earlier word is an
option or may be an option's value (`-m gpt resume` refuses), because the kit
does not track which native options take a value; this can refuse a prompt
that follows a value-less flag and starts with a refused word (I, offline
test).

## Small shared contract for C/X/A

- Policy evaluator inputs: provider, quota family, reserves (`five_hour`,
  `weekly`, each a percentage or off), maximum observation age, allowed models
  (Claude), revalidations per minute, heartbeat maximum age, the exact refresh
  `host:port` allowlist (empty for refresh option a), native minimum version,
  and the upstream proxy captured from the launcher's environment.
- Lease interface (gate-internal, one per invocation): `startup()`,
  `admit(headers, body, close) -> (ok, reason, stream)`, `poll()`,
  `latch(reason)`, `restart_paused()`, `continue_check()`, `status()`,
  `is_open()`, `release(stream)`; streams register a close callback that the
  latch calls under the admission lock.
- Gate control messages, JSON lines on a launcher socketpair:
  `{"op":"status"}` -> `{"op":"reply:status","state","reason","paused","epoch","streams","ready","lost"}`;
  `{"op":"continue"}` -> `{"op":"reply:continue","ok","reason"}` (Codex adds
  `"needs_read": true`, answered by one fresh reader read); gate to launcher
  `{"op":"latched","reason"}` and `{"op":"resumed"}`; the launcher treats a
  dead or restarting gate (no control channel) as still paused. Monitor channel as run:
  one `poll` line per heartbeat that is both the liveness signal and the poll
  trigger; EOF or staleness latches. The contract C/X/A need separates the
  two: a liveness line per heartbeat and a usage poll on its own interval.
- Process interface: `gate.py CONFIG --listen-fds A,B --monitor-fd N
  --control-fd N`; the launcher keeps the listening sockets.

## Costs

- Code: research composition of about 2,170 lines (gate 787, launcher 666,
  monitor 49, child 178, fixtures 493) on top of QC, QX, NM's renderer and
  PC5's control, plus a 1,600-line probe driver, a 260-line remote runner, a
  440-line checker and 590 lines of tests. A production version would need
  the gate, launcher relay, monitor and renderer, roughly 800 to 1,000 lines
  (I).
- Dependencies: none beyond Python 3 standard library and host tools
  (`sandbox-exec`, `ifconfig`); Codex additionally needs its own
  `app-server` for reads.
- Stability: SBPL and `sandbox-exec` are undocumented and deprecated (NM);
  the self-test turns a semantic change into a refusal only for what it
  checks.
- User-visible: the TUI stays up but shows the gate's message after a 403 or
  503 ("Please run /login" for Claude); git, pip, ssh, dev servers and local
  TCP or Unix-socket IPC do not work inside protected runs (NM, case 10);
  native sandboxes are off; Claude's Bash tool needs its fixed temp directory
  if outer write rules are used; Codex keeps a foreground tool running after
  Esc; detached background tools keep running locally after a pause and after
  the native exits (their network stays confined).

## Remaining gaps and human gates

- G0 choices: questions 5 to 8 above, the Codex rebind mechanism (KM used
  gate-observed reads plus gate reads for polls and rebind, and one fresh
  reader for preflight and continuation), the refusal status codes and their
  UX, and the poll and age bounds.
- U: real Keychain reads and write-back with SecurityServer admitted; any real
  provider or refresh host; whether refresh hosts can serve model traffic;
  Codex public-root TLS under the profile; WebSocket routes; addresses that
  appear after launch for the port holder; other uids; native agents and
  subagents (Claude's agent tool, Codex sub-agents), hooks, MCP servers,
  plugins and Claude `!` shell mode one by one; other macOS releases, Intel
  Macs, MDM hosts, sleep and wake; repeat-run statistics beyond the runs
  recorded below.
- Covered only by offline tests, not on the Mac: the Codex gate's
  hold-and-rebind read (a new same-account bearer with no forwarded read
  yet), the cut of an established refresh tunnel at a latch or monitor loss,
  a continuation while the gate is dead, duplicate account or bearer headers,
  and the Codex `exec`, `e` and `review` refusals.
- Not exercised: a child request queued across a same-account refresh (both
  natives refreshed A -> A2 at startup, before any child existed; only A -> B
  had a queued child); history scroll in the paused TUI (only `/help` and
  `/status`); a native held tunnel across a pause.
- Human gates: HLogin/N1 for any live credential, a live usage read with real
  accounts, and G0 itself.

## Containment and real-daemon contact

- One remote root per run created with `mktemp -d /tmp/quota-km-XXXXXX`,
  recorded in the ignored worktree cache; every case used its own HOME,
  config dirs, XDG dirs and TMPDIR inside it. Claude writes outside that
  TMPDIR: its Bash tool uses a fixed per-uid directory,
  `/private/tmp/claude-<uid>/<slug of the cwd>/<session uuid>` (N, case 9
  write rules), shared with any other Claude session of the same user. Each
  Claude case that ran Bash therefore left one
  `-<root-slug>-cases-<case>-workspace` entry there. `run.py cleanup` removes
  exactly the entries whose name starts with the root's slug, after `lsof`
  shows no process using them, then the root itself once no process names it;
  `run.py claude-tmp-cleanup ROOT` does the first part for an earlier root.
  The removals are recorded in the ignored cache (see the node status). This
  is also a product cost: protected-mode write rules and cleanup must cover
  that directory, which TMPDIR does not move (I).
- Claude's `security` calls went to the PATH shim (lookups exit 44, every
  other command exit 1); no Keychain item was read, created or changed, and
  the login keychain and search list were never touched.
- Natives reached only loopback fixtures. Their attempts to reach real hosts
  were CONNECTs to the gate (`api.anthropic.com`, `downloads.claude.ai`,
  `github.com`, `raw.githubusercontent.com`, and the refresh hosts when not
  allowlisted), all refused (N). The fabricated CA and the leaf naming the two
  refresh hosts lived only in the temp root and were trusted only through one
  process's `NODE_EXTRA_CA_CERTS` or `CODEX_CA_CERTIFICATE`.
- Evidence is sanitized: the remote root and its bare name (a TUI can wrap
  the path across lines), Claude's project slug of it, the remote home,
  username and uid, the local home and the interface address are
  placeholders; an offline test fails on leftovers.

## Reproduction and validation

```sh
python3 -I -B -m unittest discover -s docs/superpowers/plans/quota-reserve-probes/km -p test_km.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/km/check_evidence.py [--dir DIR] [--measure]
python3 -I -B docs/superpowers/plans/quota-reserve-probes/km/run.py [--dev] [--only CASE,...] STAGE... cleanup
python3 -I -B docs/superpowers/plans/quota-reserve-probes/km/run.py claude-tmp-cleanup /tmp/quota-km-XXXXXX
```

Stages: `claude-loss`, `codex-loss` (cases 1 to 3), `claude-resume`,
`codex-resume` (4 to 6), `claude-refresh`, `codex-refresh` (7, 8, prelaunch),
`claude-tools`, `codex-tools` (9, 10, nested sandbox, write rules),
`refusals` (11), `portmatrix` (holder shapes and SBPL address forms),
`proxy` (provisional decision 3a). The Mac host alias defaults to `mac`
(`KM_MAC_HOST`). The offline tests cover the launcher's pure refusals,
profile shape and control round trip without a gate, the gate route table and
selector rules, both adapters against loopback fixtures, an established
CONNECT tunnel through the real gate handler cut by a latch and by monitor
loss, the driver's sanitizing and Claude temp-entry matching, and the
checker: every committed stage passes and each targeted mutation of a
verdict-bearing row fails it (among them the Codex case-7 order: an A2 usage
read moved after the first A2 admission, an A2 admission under the startup
epoch). They start no native code.

All eleven stage files come from one round of runs with the committed code
on one remote root (two `run.py` invocations; the second resumed after an ssh
discovery timeout). Each stage file records its wall and monotonic elapsed
time; `time.monotonic()` stops while a Mac sleeps, and the checker refuses a
stage whose two differ by 2 s or more, so no recorded stage spans a sleep
(the difference is 0.0 s in every file; a clamshell sleep did interrupt one
discarded attempt). Each case also passed in development runs (ignored
cache); timing-dependent values (latch delays, poll counts) vary between runs
and only the bounds the checker enforces are claimed.
