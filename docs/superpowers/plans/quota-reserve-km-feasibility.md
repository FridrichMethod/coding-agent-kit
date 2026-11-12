# KM: composed protected invocations for Claude and Codex on macOS

Status: **all eleven decisive K cases pass for both natives on one macOS host,
under fixtures; nothing here is a G0 approval, a production capability or a
release claim.** One launcher process owns the user's terminal and the gate
port, a separate monitor sends heartbeats, a gate process composes the
reviewed QC (Claude) and QX (Codex) admission contracts, and the real native
runs under the stock `/usr/bin/sandbox-exec` with NM's M1 profile plus
`com.apple.SecurityServer`. With fabricated accounts and loopback fixtures,
every latch (threshold, monitor SIGKILL, monitor EOF, monitor stall, identity
change, token expiry, gate restart) left zero upstream model requests after
it, closed the open SSE stream within a millisecond, refused the SDK-like
child and its detached grandchild, and left the native TUI usable. A fresh
typed confirmation on the trusted terminal plus a fresh same-bearer check
reopened the same invocation; nothing else did. Native refresh worked through
an exact-host CONNECT tunnel while admission was open (fixture CA given
explicitly), and stopped at the pause. No product runtime changed.

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
   |  3. monitor.py (outside) <- socketpair -> gate.py (outside) with the holder's
   |     listening sockets, a control socketpair to the launcher
   |       Claude: gate reads the bearer memory-only, usage + profile preflight
   |       Codex:  launcher runs one fresh `codex app-server` under the profile;
   |               its account/rateLimits/read reaches the gate, which binds epoch 1
   |  4. render profile, self-test under it (gate ok, other port EPERM,
   |     bind EPERM, TIOCSTI EPERM), refuse on any mismatch
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
`sandbox.enabled=false`. Codex gets `-c chatgpt_base_url=...` and `-c
model_providers.gate.base_url=...` overrides (so two invocations can share one
`CODEX_HOME`), `--no-daemon`, file credential store,
`danger-full-access`, `supports_websockets=false`, zero request and stream
retries, and `HTTPS_PROXY` set to the gate.

Gate behavior (as run): default-deny route table (Claude `POST /v1/messages`;
Codex `POST /backend-api/codex/responses`, usage GETs, and the model-free
reads native sends, the latter only while running); every upstream byte is
written under the admission lock for a still-registered stream; a latch
closes every registered client and upstream socket under the same lock;
WebSocket upgrades refused; CONNECT only to an exact allowlisted `host:port`
while admission is open and the monitor alive. Refusals: Claude `403
permission_error`, Codex `503` (never 401, which would start native refresh).

## Decisive cases

Each cell is one native run (the refusal row is ten launcher runs per
provider); all ran under the profile above. Evidence files are in
[quota-reserve-probes/km/evidence/](quota-reserve-probes/km/evidence/).

| Case | Claude 2.1.322 | Codex 0.166.0 | Label |
| --- | --- | --- | --- |
| 1 Threshold pause | latch 466 ms after the fixture crossed the reserve (next 0.5 s poll); held SSE stream closed 0.4 ms after; 0 upstream POSTs after; `/help` works; later turn refused `threshold_five_hour` | latch 81 ms after; stream closed 0.6 ms after; 0 POSTs after; `/status` works; later turn refused | N |
| 2 Queued child request | SDK-like child and detached (setsid, double fork) grandchild: 200 before, 403 after the latch; direct connects to upstream and usage ports EPERM; UDP to the gate port EPERM | same with 503 | N |
| 3 Monitor loss | SIGKILL: latch 3.5 ms; EOF: 0.6 ms; stall: 2.7 s (heartbeat max age 3 s); stream closed, 0 POSTs after, TTY usable | SIGKILL 3.5 ms; EOF 0.5 ms; stall 2.6 s; same | N |
| 4 Gate death and port reuse | gate SIGKILLed; boundary still denies direct egress; in-boundary bind of the gate port EPERM on 127.0.0.1, ::1, 0.0.0.0, ::; every outside bind of the port EADDRINUSE; the native request typed during the death waited in the holder's backlog and the restarted gate refused it (`gate_restart`); continuation reopened | same (`paused:gate_restart`) | N |
| 5 Fresh manual continuation | typed text without F12, a stale challenge, the right challenge inside bracketed paste, recovery of the quota with polls running, and native output all left it paused; a failing fresh check stayed paused with `still paused: threshold_five_hour`; a passing check reopened the same native and gate | same (`threshold:five_hour`) | N |
| 6 Independent session | second invocation (reserve 5) kept working when the first (reserve 30) paused at 20% left; outside consumption to 4% left paused the second | same | N |
| 7 Rotation across a pending request | native refresh A -> A2 (same account): rotation hold, same-bearer usage plus profile, admitted; store swap to B with the child queued: hold, `bearer_identity_mismatch`, latched; 0 POSTs after | refresh A -> A2: native's own usage read with A2 bound the new epoch before its model request; B with the child queued: `identity-changed`, latched | N |
| 8a Refresh tunnel | CONNECT `platform.claude.com:443` tunnelled while open (native refresh succeeded, fixture CA via `NODE_EXTRA_CA_CERTS`), 403 after the pause; the native's other CONNECTs (`api.anthropic.com`, `downloads.claude.ai`, `github.com`) refused; a confined client's CONNECTs to other hosts refused | CONNECT `auth.openai.com:443` tunnelled while open (`CODEX_CA_CERTIFICATE`), 403 after the pause; the native's `raw.githubusercontent.com` refused; a confined client's other CONNECTs refused | N |
| 8b No refresh path | refresh CONNECTs refused; at expiry (usage and profile then 401) the next poll latched `server_identity_unavailable` 0.26 s later | 64 refresh CONNECTs refused (mostly at startup); at expiry polls failed and the lease went `stale` after 5.7 s (max age 6 s) | N |
| 9 Running tools | background file writer kept writing through the pause and after native exit; its network to non-gate ports EPERM; foreground loop stopped by Esc (native interrupt killed it) | background writer the same; foreground loop kept running after Esc ("1 background terminal running") | N |
| 10 git and pip | `git clone` over http and `pip download` from loopback fixtures fail under the boundary before and after the pause (git "Failed to connect", pip misleadingly "No matching distribution"); also through the gate as proxy; both work outside | same | N |
| 11 Startup refusals | old version, `-p`, `ANTHROPIC_API_KEY`, unsupported family, revoked quota, one-hour-old server Date, malformed usage, threshold at start, missing sandbox-exec, failing self-test: each refused with a message, exit 3, native never started, 0 model requests | same set (`resume`, `OPENAI_API_KEY`, ...): 0 model requests; quota refusals come after the in-boundary reader's model-free GETs | N |

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
  N); Ctrl-U cancels without forwarding. Continuation latency from the
  launcher: Claude 7 to 15 ms (gate-side usage plus profile); Codex 77 to 90
  ms including one fresh in-boundary `codex app-server` read (N).
- Case 7. Claude wrote the refreshed credential to `.credentials.json` after
  the shimmed Keychain write failed: the shim saw `security -i` (commands on
  stdin, argv only recorded) and 13 `find-generic-password` lookups of
  `Claude Code-credentials-<8 hex>` (N). Codex refreshed twice at startup in
  the prelaunch probe (second grant refused, single-use fixture), as KLX saw
  on Linux (N).
- Case 8. Whether `platform.claude.com` or `auth.openai.com` could serve model
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
- The full holder took 19 sockets on this host (one per interface address,
  including link-local ones). Addresses that appear after launch (VPN, new
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
  read for the preflight and for continuation (about 70 to 90 ms each, N).
- The TUI refuses to start ("account/read failed during TUI bootstrap:
  selected workspace missing from routing discovery") unless
  `/backend-api/wham/accounts/check` lists the account (N, fixture detail).
- Under the profile the TUI needs `--no-daemon` (NM: the daemon path runs
  setuid `ps`, denied). `--no-daemon` behaved as on Linux in every case (N).
- Global `-c` overrides before the subcommand apply to `app-server` too (N).

## Measurements (loopback, this host; not live numbers)

From the committed evidence (`check_evidence.py --measure`):

| Measurement | Claude | Codex | Label |
| --- | --- | --- | --- |
| Preflight (gate start to lease) | 3.2 ms (usage plus profile) | 86 ms (fresh in-boundary app-server read) | N |
| Usage GET on loopback (median) | 2.5 ms | 2.4 ms | N |
| Admission decision (median, admitted) | 0.31 ms | 0.36 ms | N |
| Decision to first upstream byte (median) | 0.9 ms | 0.9 ms | N |
| Rotation hold to decision | 1.9 ms (A2 admitted), 8.7 ms (B refused) | not needed: native's own usage read rebound first | N |
| Threshold crossing to latch (poll every 0.5 s) | 466 ms | 81 ms | N |
| Latch to stream close | 0.4 to 0.6 ms | 0.2 to 0.6 ms | N |
| Monitor EOF / SIGKILL / stall to latch | 0.6 ms / 3.5 ms / 2.7 s | 0.5 ms / 3.5 ms / 2.6 s | N |
| Self-test under the profile | 38 ms | 36 ms | N |
| Continuation check | 7 to 15 ms | 77 to 90 ms | N |

The crossing-to-latch figure is bounded by where the crossing falls in the
poll cycle, not by the gate (I).

Proposed bounds for G0 (I; none measured against real services):

- Usage query: socket timeout 5 s, total deadline 10 s (QC's values), no retry
  inside one poll; a failed poll never extends the observation age.
- Poll interval 30 s while running, with jittered exponential backoff on
  transient failures up to the interval; maximum observation age 120 s, so
  three consecutive failed polls pause.
- Heartbeat every 1 s, heartbeat maximum age 3 s.
- Revalidation budget (rotation holds) 4 per minute, as QC; a native double
  refresh costs two.
- Startup self-test bounded at 20 s; preflight bounded by the usage deadline.

## Refusal conditions (macOS)

Refuse before any model request when: not macOS; `/usr/bin/sandbox-exec`
missing or not executable; the rendered profile is rejected or the self-test
under it does not show gate connect, other-port EPERM, bind EPERM and TIOCSTI
EPERM; the native version is unrecognized or below the minimum (Claude
2.1.322, Codex 0.166.0 on this host); Claude print or remote modes or a user
`--settings`; Codex `resume`, `fork`, `app-server`, `--profile` or a `-c`
override of a protected key; any store, provider or base-URL selector in the
environment; the quota policy names an unsupported family or no reserve; the
usage or profile read fails, is malformed, carries a server Date skewed past
5 minutes, has resets in the past or already crosses the reserve; the account
identity does not match (N for each listed refusal in case 11 except the
`fork`, `--profile` and `-c` arguments, which are offline tests only).

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
  `{"op":"latched","reason"}` and `{"op":"resumed"}`. Monitor channel: one
  `poll` line per heartbeat; EOF or staleness latches.
- Process interface: `gate.py CONFIG --listen-fds A,B --monitor-fd N
  --control-fd N`; the launcher keeps the listening sockets.

## Costs

- Code: research composition of about 2,100 lines (gate 775, launcher 645,
  monitor 49, child 178, fixtures 493) on top of QC, QX, NM's renderer and
  PC5's control, plus a 1,600-line driver, a 390-line checker and 400 lines of
  tests. A production version would need the gate, launcher relay, monitor
  and renderer, roughly 800 to 1,000 lines (I).
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
  appear after launch for the port holder; other uids; hooks, MCP servers,
  plugins and Claude `!` shell mode one by one; other macOS releases, Intel
  Macs, MDM hosts, sleep and wake; repeat-run statistics beyond the runs
  recorded below.
- Human gates: HLogin/N1 for any live credential, a live usage read with real
  accounts, and G0 itself.

## Containment and real-daemon contact

- One remote root created with `mktemp -d /tmp/quota-km-XXXXXX`, recorded in
  the ignored worktree cache; every case used its own HOME, config dirs,
  XDG dirs and TMPDIR inside it. It is removed after the final run once no
  process names it (see the node status for the removal record).
- Claude's `security` calls went to the PATH shim (lookups exit 44, every
  other command exit 1); no Keychain item was read, created or changed, and
  the login keychain and search list were never touched.
- Natives reached only loopback fixtures. Their attempts to reach real hosts
  were CONNECTs to the gate (`api.anthropic.com`, `downloads.claude.ai`,
  `github.com`, `raw.githubusercontent.com`, and the refresh hosts when not
  allowlisted), all refused (N). The fabricated CA and the leaf naming the two
  refresh hosts lived only in the temp root and were trusted only through one
  process's `NODE_EXTRA_CA_CERTS` or `CODEX_CA_CERTIFICATE`.
- Evidence is sanitized: the remote root, Claude's project slug of it, the
  remote home, username and uid, the local home and the interface address are
  placeholders; an offline test fails on leftovers.

## Reproduction and validation

```sh
python3 -I -B -m unittest discover -s docs/superpowers/plans/quota-reserve-probes/km -p test_km.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/km/check_evidence.py [--dir DIR] [--measure]
python3 -I -B docs/superpowers/plans/quota-reserve-probes/km/run.py [--dev] [--only CASE,...] STAGE... cleanup
```

Stages: `claude-loss`, `codex-loss` (cases 1 to 3), `claude-resume`,
`codex-resume` (4 to 6), `claude-refresh`, `codex-refresh` (7, 8, prelaunch),
`claude-tools`, `codex-tools` (9, 10, nested sandbox, write rules),
`refusals` (11), `portmatrix` (holder shapes and SBPL address forms),
`proxy` (provisional decision 3a). The Mac host alias defaults to `mac`
(`KM_MAC_HOST`). The offline tests cover the launcher's pure refusals and
profile shape, the gate route table, both adapters against loopback fixtures,
the driver's sanitizing, and the checker: every committed stage passes and
each targeted mutation of a verdict-bearing row fails it. They start no
native code.

The eight native stage files and `refusals` come from one final run of
those stages; `portmatrix` and `proxy` were run once more after the SBPL-form
check and the proxy case were added. The only launcher change after the final
run is the proxy inheritance, which acts only when the launcher's own
environment has `HTTPS_PROXY`; no other stage set it (I, from the code). Each
case also passed at least once in development runs before that (ignored
cache); timing-dependent values (latch delays, poll counts) vary between runs
and only the bounds the checker enforces are claimed.
