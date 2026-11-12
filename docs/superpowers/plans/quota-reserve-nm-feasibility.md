# NM: macOS whole-invocation outer boundary and native-store feasibility

Status: **finite feasibility evidence complete on one macOS host; full A, K and
G0 remain unproved**. One Seatbelt profile, applied with the stock
`/usr/bin/sandbox-exec` before the native starts, confined every tested process
of the invocation (native root, shell and tool children, `env -i` clients run
by absolute path, and setsid double-forked children reparented to launchd) to
one loopback TCP gate port. Real Claude Code and Codex TUIs worked inside it,
reached the fake gate with fabricated bearers, and their tool children could
not reach anything else. Probing found three holes in the first profile, each
closed by a small rule change and then rechecked with both natives:
terminal input injection (TIOCSTI) into the unconfined parent shell, UDP to the
gate's port number, and signals to unrelated same-UID processes. Inherited
sockets stay usable after confinement, so the launcher must close them. Native
tool sandboxes cannot nest inside the profile, and direct native refresh is
impossible inside it. A refresh tunnelled through the gate has an extra TLS
cost for Codex: under the profile the macOS system roots that Codex loads are
unavailable, so a tunnelled Codex refresh would fail closed unless trustd is
admitted. These are G0 decisions, recorded below as user questions. No product
runtime changed.

Base: `0c5b492` (branch `ci/quota-reserve`). Captured November 12, 2026 UTC on
macOS 27.2, arm64. Natives: Claude Code `2.1.322` (SHA-256 `a9d23794...a3450`)
and `codex-cli 0.166.0` (SHA-256 `39acf979...db7c1`); the full digests are in
every native evidence file. Probe interpreter: Homebrew Python 3.15; Node
24.11.1 for the Node client and npm. All tokens, accounts and model responses
are fabricated. No login, real provider request, real credential read,
Keychain item change, installation, privilege, launchd job or system change
was made.

Labels: N = native observation in this node; S = inspected source, header,
man page or documentation; I = inference; U = unproved.

## Decision and user questions

Recommended macOS adapter for G0 review, called M1 here: the launcher renders
the profile below, closes every descriptor except the terminal, and execs
`/usr/bin/sandbox-exec -p PROFILE NATIVE ARGS...`. Every descendant inherits
the profile; a confined process cannot apply another one (N, `sandbox_apply:
Operation not permitted`), so it can neither loosen nor replace it.

```text
(version 1)
(allow default)
(deny network* (with message TAG))
(allow network-outbound (remote tcp "localhost:GATE_PORT"))
(deny job-creation (with message TAG))
(deny lsopen (with message TAG))
(deny appleevent-send (with message TAG))
(deny mach-lookup (with message TAG))
(allow mach-lookup (global-name "com.apple.FSEvents"))
(allow mach-lookup (global-name "com.apple.PowerManagement.control"))
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
(deny file-write* (subpath "HOME/Library/LaunchAgents") (subpath "/private/var/at") (with message TAG))
```

This exact shape (with the probe's anchor directory standing in for extra
write denials) ran both natives end to end in
[m1-natives.json](quota-reserve-probes/nm/evidence/m1-natives.json), and the
whole boundary matrix in [m1.json](quota-reserve-probes/nm/evidence/m1.json)
(N). An earlier candidate also allowed binding and connecting Unix sockets
under one private directory; both natives passed with it too
([recommended.json](quota-reserve-probes/nm/evidence/recommended.json)), but
no native bound or connected anything there, so M1 drops it. Symbolic-link and
hard-link aliases of an outside daemon socket placed in that directory were
denied (N, m1.json: the symbolic link resolved to the outside path, and
creating the hard link failed with `EPERM`), so the allowance could come back
later, for example as the invocation's TMPDIR, if in-invocation Unix-socket IPC
is wanted (see compatibility); other aliasing forms stay U.
Paths must be resolved (`/private/tmp`, not `/tmp`), because Seatbelt matches
the resolved vnode path; the renderer refuses unresolved paths (S, offline test).

The profile is a network-egress and delegation boundary, not a confidentiality
or filesystem boundary: `(allow default)` leaves file reads and most writes
open. That matches PO's threat model (no malicious same-UID attacker). It
depends on the gate binding each admitted request to the selected account and
bearer, because a confined process can still read other accounts' files.

User questions (also in the node status):

1. **Mechanism.** (a) M1: stock `sandbox-exec` plus a kit-owned renderer of
   about 80 lines and a startup self-test. It needs no new dependency, but
   `sandbox-exec` is marked DEPRECATED in its man page (N) and SBPL is an
   undocumented Apple interface, so any macOS update can change it. (b) The
   Anthropic sandbox-runtime wrapper. It adds a Node/npm launcher dependency
   while running on the same Seatbelt primitive (S, per PO), so the stability
   cost remains and its proxy policy needs its own review. (c) No macOS
   protected mode: refuse it on macOS.
2. **Native credential store.** (a) Admit `com.apple.SecurityServer`. Native
   Keychain read and refresh write-back would work (reachability N, real item
   U), and every descendant could query keychain items as the user can today.
   securityd is not a network path (I). (b) Keep it denied and require file
   credentials for protected accounts. This changes the macOS HAuth storage
   to 0600 plaintext files. (c) Refuse protected mode for Keychain-stored
   accounts.
3. **Native refresh.** (a) Refresh natively outside the boundary before launch,
   and pause or refuse on mid-session expiry. (b) The gate tunnels CONNECT to
   one exact OAuth host while a lease is held (Claude `platform.claude.com:443`,
   Codex `auth.openai.com:443`). This is an opaque tunnel and needs G0 to accept
   that these hosts cannot serve inference; PO forbids unconditional
   provider-host exceptions. It also has a TLS trust cost under M1. Both
   natives completed TLS through such a tunnel to a fabricated-CA fixture
   under M1 (N), but only because each was given the fixture CA explicitly.
   Claude verifies with roots bundled in its runtime and looked up no trustd
   (N, S), so its tunnelled refresh should not need trustd (I). Codex loads the
   macOS platform roots (S: `rustls-native-certs` and `security-framework` in
   the binary, no bundled root set found) and looked up `trustd.agent` (N).
   Under M1, with `trustd.agent` and `com.apple.SecurityServer` denied, the
   system trust-settings domain returns no certificates instead of 150 (N), so
   a tunnelled Codex refresh to the real host would fail closed (I). Making it
   work means admitting `trustd.agent` (and probably SecurityServer), which M1
   leaves out because trustd fetches OCSP, CRL and AIA URLs on a client's
   behalf, or handing Codex an explicit CA bundle (`CODEX_CA_CERTIFICATE` or
   `SSL_CERT_FILE`), which makes the kit choose Codex's trust roots. A real
   refresh under either change is U. (c) Codex's refresh-URL override pointed
   at the gate puts the refresh token in kit hands; not recommended.
4. **Native tool sandboxes.** (a) Protected mode turns them off (Codex
   `danger-full-access`, Claude `sandbox.enabled=false`) and the outer
   boundary takes over network confinement. Native filesystem confinement is
   lost unless outer write rules are added (U). (b) Refuse protected mode when
   one is configured. (c) Leave them on: the probed Codex tool command failed
   with exit 71, and Claude disabled its sandbox with an in-band warning (N;
   that every later command behaves the same is I).

## Host facts and refusal conditions

- `/usr/bin/sandbox-exec` is present and executes inline `-p` profiles for an
  unprivileged user (N). Its man page says "sandbox-exec - execute within a
  sandbox (DEPRECATED)" and points at App Sandbox (N, S).
- Denials carry the profile's `(with message TAG)` into the unified log, and
  `log show --predicate 'eventMessage CONTAINS "TAG"'` returns them per process
  and operation (N). Signal denials did not appear in the log on this release
  (N, absence), so the exit code is their only evidence.
- A confined process cannot exec setuid binaries: `/bin/ps` (mode 4755) fails
  with `Operation not permitted`, exit 71 from `sandbox-exec`, 126 from a shell
  child (N). `crontab`, `at`, `batch` and `sudo` are setuid too (N, modes).
- Refuse protected startup when `sandbox-exec` is missing or not executable,
  when it rejects the rendered profile, or when a startup self-test under the
  actual profile does not show gate success, direct denial and TIOCSTI denial
  before the native starts (I; each check is a single bounded child).
- Untested hosts: other macOS releases, Intel Macs, MDM or endpoint-security
  products that restrict `sandbox-exec` (U). Do not infer them from this run.

## Decisive matrix

Each row ran under both the census profile (all mach lookups allowed with
report, escape services denied) and the enforced profile, unless noted. The
boundary rows (first five) were then rerun under the exact M1 text with the
same results, except that M1 also denies the private-directory Unix socket
(N, [m1.json](quota-reserve-probes/nm/evidence/m1.json)). M1 denials for the
other rows follow from M1 being narrower than the profile they ran under (I),
except where the second table below shows an M1 run.

| Check | Result | Label |
| --- | --- | --- |
| Gate TCP over 127.0.0.1, ::1 and ::ffff:127.0.0.1 from root, shell child, `env -i` Python and Node by absolute path, detached child | connected; Node fetch status 200 | N |
| Same clients to an alternate loopback listener, TCP v4, v6, v4-mapped; UDP v4 and v6 | `PermissionError` or `EPERM` on every attempt; listener hits 0 | N |
| `curl` and `nc` to the alternate port | curl exit 7, nc failure; nc to the gate succeeds | N |
| setsid double-fork, reparented to launchd (ppid 1) | still confined: alternate TCP, UDP and Unix denied | N |
| Unrelated process outside the sandbox | reaches every alternate listener (TCP, UDP, Unix) | N |
| Pathname Unix stream socket of a fixture daemon outside the private directory | connect and bind denied; daemon hits 0 | N |
| Unix datagram to the outside fixture socket | denied; hits 0. Private-directory datagram to itself works | N |
| UDP to the gate's port number, `(remote ip ...)` rule | **allowed**: an outside process holding UDP on that port received 2 datagrams | N |
| Same with `(remote tcp ...)` rule | UDP denied, TCP to the gate still connects | N |
| DNS | `localhost` and numeric names resolve; the mDNSResponder socket is denied, so real names cannot resolve | N |
| `open` of an unclaimed URL scheme | denied at LaunchServices lookups; control reaches LaunchServices (-10814) | N |
| `launchctl submit` of a marker job | denied (`job-creation`); label never listed, marker never written | N |
| Write under a LaunchAgents-shaped anchor directory | denied (`file-write-create`); workspace writes allowed | N |
| Escape mach services: launchservicesd, appleevents, nsurlsessiond, dnssd, lsd mapdb and modifydb, SecurityServer | lookup returns 1100 (denied); all found outside | N |
| Nested `sandbox-exec` inside the profile | exit 71, `sandbox_apply: Operation not permitted` | N |
| Inherited connected TCP, UDP and Unix sockets passed across `sandbox-exec` | **still writable**: all three listeners received the write | N |
| Same descriptors closed before confinement | `EBADF` on all three | N |
| TIOCSTI from a confined child on the controlling terminal | **injected**: the unconfined session leader read `NMINJECT` after the child exited | N |
| Same with `(deny file-ioctl (ioctl-command TIOCSTI))`, symbolic or numeric | `EPERM`, nothing injected; `tcsetattr` still works | N |
| `kill -0` of an unrelated outside process | allowed under `(allow default)`; denied with `(deny signal)` plus `(allow signal (target same-sandbox))`, while same-sandbox kill works | N |

Evidence: [boundary](quota-reserve-probes/nm/evidence/boundary.json),
[ipc](quota-reserve-probes/nm/evidence/ipc.json),
[fds](quota-reserve-probes/nm/evidence/fds.json),
[dgram](quota-reserve-probes/nm/evidence/dgram.json),
[tty](quota-reserve-probes/nm/evidence/tty.json). The `.invalid` lookup failure
alone does not prove DNS denial; the logged `network-outbound` denial of
`/private/var/run/mDNSResponder` does (N).

Further checks under the exact M1 text, each with an unconfined control (N,
[m1.json](quota-reserve-probes/nm/evidence/m1.json)). Where no listener
exists, the errno tells the profile's decision apart: `EPERM` means the
profile denied the connect, while "connection refused" or "no route" means it
admitted the connect and the network stack then failed it.

| Check | Under M1 | Control | Label |
| --- | --- | --- | --- |
| Gate port at `0.0.0.0` and `::` | connected (reaches the loopback gate) | connected | N |
| Gate port at the host's own interface IPv4 address (no listener there) | refused, so admitted by the profile; the alternate port at that address gets `EPERM` | refused | N |
| Gate port at `fe80::1%lo0` | "no route" (errno 65), so admitted; the alternate port there gets `EPERM` | errno 65 | N |
| Gate port at `127.0.0.2` | `EPERM` | timeout | N |
| UDP to the gate port | `EPERM` | sent | N |
| Symbolic link in the private directory to an outside daemon socket, with the private allowance (earlier candidate) and under M1 | connect denied; the log names the resolved outside path; daemon hits 0 | connected | N |
| Hard link in the private directory to the same socket | with the allowance, creating the link fails with `EPERM`; under M1 the connect is denied; daemon hits 0 | connected | N |
| Unprivileged ICMP echo (datagram socket) to 127.0.0.1 and ::1 | `EPERM` | reply | N |
| `launchctl bootstrap gui/UID` and legacy `launchctl load` of a marker plist | both fail ("Bootstrap failed: 5", "Load failed: 5"; `load` still exits 0), `job-creation` denials logged, label never present, marker never written | not run | N |
| `launchctl asuser UID` running a probe | refused: "Could not switch to audit session ... Operation not permitted"; the probe never ran | not run | N |
| `launchctl kickstart` of an absent label | same "Could not find service" (113) as the control: the request reaches launchd | 113 | N |
| Ctrl-Z on the PTY while a confined job is in the foreground | job stops on `SIGTSTP` and resumes on the shell's `SIGCONT`; inside, stop, continue and terminate of its own child and `SIGCONT` to its own group all work | same | N |
| Copy the system and admin trust-settings certificates (the macOS root loading that `rustls-native-certs` performs) | system domain: `errSecNoTrustSettings`, 0 certificates; admin domain: `errSecNotAvailable`; `trustd.agent` and SecurityServer lookups denied | system: 150 roots; admin: none configured | N |

Consequences for the launcher (I, from the rows above):

- Close every descriptor except the terminal before `sandbox-exec`. Seatbelt
  checks connect and send, not writes on sockets that are already connected.
- Use `(remote tcp "localhost:PORT")`, never `remote ip`. With `remote ip`,
  any same-host process that binds UDP on the gate's port number gets a
  datagram channel out of the boundary.
- Deny TIOCSTI. Without that rule, any confined process that still holds the
  terminal can queue a command line that the user's unconfined shell runs
  after the native exits.
- Hold the gate port for the whole invocation, on every local address the
  `localhost` filter admits, not only on 127.0.0.1 and ::1. The filter
  admitted the wildcard addresses, the host's own interface address and
  `fe80::1%lo0` on that port (N, above), so an unrelated process listening on
  the port at the interface address, or on the wildcard, would be reachable
  (I). The filter's exact definition is undocumented, and addresses that
  appear later (VPN, a new interface) were not tested (U). The profile cannot
  change after it is applied, so a restarted gate must take the same port,
  and an unrelated listener that grabs it in between would be reachable.
  Binding the port in the launcher and handing the listening sockets to the
  gate would close the restart gap; how to hold it on every admitted address
  without listening on external interfaces is open (I, U; K's port-reuse
  case).

## Mach services the natives need

Census runs reported every mach lookup (N). Seen from the natives and their
tools: Claude (`2.1.322` process) FSEvents, DNSConfiguration, cfprefsd.agent,
logd, notification_center, opendirectoryd.libinfo; Claude-spawned
`caffeinate` PowerManagement.control; Codex CoreAuthentication.agent,
CoreServices.coreservicesd, FSEvents, SystemConfiguration.configd, cfprefsd
agent and daemon, logd, notification_center, libinfo. Their tool children
(git, curl, ssh, Python) together looked up configd, cfprefsd, logd,
notification_center, libinfo, membership and (ssh) usymptomsd. The plain-HTTP
census runs looked up no trustd, diagnosticd, system.logger or bsd.dirhelper.

Under the enforced, recommended and M1 profiles these were denied and both
natives still completed every step (N): Claude SecurityServer and securityd.xpc,
Codex CoreAuthentication.agent and its libxpc variant, coreservicesd and
DiskArbitration, ssh dnssd and usymptomsd, and a Claude-spawned `lsregister`
(LaunchServices registration, 97 modifydb lookups). The recommended list
drops trustd because trustd fetches OCSP, CRL and AIA URLs on a client's
behalf, a plausible daemon-mediated egress path (I, U). Allowed services that
do work outside the sandbox: cfprefsd writes preference domains,
notification_center posts Darwin notifications, PowerManagement holds power
assertions. None is a network path (I).

TLS adds trustd, for Codex only. In the TLS census runs, where each native
reached a fabricated-CA fixture through the gate's CONNECT tunnel, Claude
looked up `securityd.xpc` (allowed) and SecurityServer (denied by the census),
and no trustd service; Codex looked up `trustd.agent` (allowed) and
SecurityServer (denied) (N,
[tls.json](quota-reserve-probes/nm/evidence/tls.json)). Both handshakes also
succeeded under M1 with those lookups denied, because each native trusted the
fixture CA given through `NODE_EXTRA_CA_CERTS` or `CODEX_CA_CERTIFICATE` (N).
Codex's verification of public hosts under M1 is the open cost described in
user question 3.

## Real natives inside the profile

Each case ran the real TUI under `sandbox-exec` in a private PTY with a
fabricated credential, a temporary HOME, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
XDG directories and TMPDIR, and the fake gate as the only admitted port. The
gate answered the first marked turn with one native shell tool call. That
call ran the egress clients in the foreground, under `env -i` by absolute
path, as a detached double fork, and with `curl`.

| Case | Result | Label |
| --- | --- | --- |
| Claude, fake OAuth file, census and enforced | model POSTs reach the gate with the fixture bearer; tool runs; every tool, `env -i` and detached egress denied, LaunchServices lookup denied, curl `000`; second typed turn reaches the gate; `/exit` exit 0; termios restored; history and transcript written | N |
| Claude, `CLAUDE_CODE_OAUTH_TOKEN`, enforced | same | N |
| Claude, real `/usr/bin/security`, enforced | `security` denied SecurityServer; Claude used the fake file credential and completed | N |
| Codex `--no-daemon`, file auth, custom provider, census and enforced | discovery, usage, models and responses reach the gate with the fixture bearer; tool and second turn work; Ctrl-C exit 0; rollout written | N |
| Codex with its default app-server daemon, enforced | refuses before any model request: `failed to invoke ps for pid-managed app server: Operation not permitted`; Codex suggests `--no-daemon` | N |
| Both natives, hardened, recommended and exact M1 profiles | all of the above still pass; tool `kill -0` of an outside process denied (enforced: allowed) | N |
| Both natives, census and M1, model API over TLS through the gate's CONNECT tunnel to a fabricated-CA fixture | handshakes succeed, model requests reach the TLS fixture with the fixture bearer, tool and second turn work, the exit gate sees only CONNECT; other CONNECTs (`api.anthropic.com`, `downloads.claude.ai`, `github.com`, `raw.githubusercontent.com`) refused | N |
| Tool-side TIOCSTI through `/dev/tty`, both natives | `ENXIO`: tool commands run without a controlling terminal in these versions | N |

Evidence: [natives](quota-reserve-probes/nm/evidence/natives.json),
[natives-extra](quota-reserve-probes/nm/evidence/natives-extra.json),
[hardened](quota-reserve-probes/nm/evidence/hardened.json),
[recommended](quota-reserve-probes/nm/evidence/recommended.json),
[m1-natives](quota-reserve-probes/nm/evidence/m1-natives.json),
[tls](quota-reserve-probes/nm/evidence/tls.json).

The tool-side ENXIO means these native tool paths cannot inject terminal
input even without the TIOCSTI rule. The native root, hooks or helpers that
inherit the terminal, and future versions are not covered by that (I), so
the rule stays mandatory. It costs nothing observed.

Native attempts to reach real hosts, all denied by the boundary and none
forwarded (N):

- The Claude process tried to reach mDNSResponder (DNS) in every run without
  `HTTPS_PROXY` except the `CLAUDE_CODE_OAUTH_TOKEN` run, despite
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and `DISABLE_AUTOUPDATER=1`.
  Claude-spawned `git-remote-http` and `ssh` did the same in every Claude run
  (`ssh` only, with a proxy), consistent with a background plugin-marketplace
  fetch (I).
- With `HTTPS_PROXY` set to the gate, Claude sent CONNECT for
  `downloads.claude.ai:443`, `github.com:443` and `api.anthropic.com:443`, plus
  `platform.claude.com:443` (twice) when its OAuth token was expired; the
  fixture refused each.
- Claude tried to bind `/private/tmp/cc-socks/PID.sock` in every run except
  the `CLAUDE_CODE_OAUTH_TOKEN` run and the unproxied refresh run (denied;
  purpose not identified; no visible effect).
- Codex made one DNS attempt (without a proxy) and one denied
  `network-bind local:*:0`; with `HTTPS_PROXY` set it sent one CONNECT for
  `raw.githubusercontent.com:443`, refused. Its backend GETs
  (`/api/codex/settings/user`, `/api/codex/rate-limit-reset-credits`) followed
  `chatgpt_base_url` to the gate, which refused them without harm.

## Native credential store

- With SecurityServer admitted, `security find-generic-password` for a random
  nonexistent service under the profile reached securityd ("could not be found
  in the keychain"). With it denied, the same call failed with "One or more
  parameters passed to a function were not valid" and a logged
  SecurityServer denial (N,
  [keychain](quota-reserve-probes/nm/evidence/keychain.json)). Only a
  nonexistent name was queried; no item was read, created or changed.
- Claude 2.1.322 reads its macOS credential by running
  `security find-generic-password -a USER -w -s NAME`, resolved through PATH:
  a PATH shim recorded every call (N). Names: `Claude Code-credentials-H` and
  the legacy `Claude Code-H`, where H is the first 8 hex digits of SHA-256 over
  the NFC-normalized `CLAUDE_CONFIG_DIR`, the account is `$USER`, and the fixed
  `Claude Code-device-keys` service has no suffix (S, prior static research in
  [auth discovery](quota-reserve-claude-auth-discovery.md) and
  [persistence](quota-reserve-claude-persistence-research.md)). The observed
  credential and legacy names match the source rule for the probe directory
  (N, offline test). The device-key service was not looked up in these runs.
- Because Claude resolves `security` through PATH, the kit must keep PATH
  native and must never place a `security` shim in protected mode (I). The
  shim was a probe device only.
- A real native Keychain read or refresh write-back under a profile that admits
  SecurityServer stays U. Testing it needs an item in the login keychain or a
  search-list change, both outside this node's authority.

## Native refresh

| Case | Observation | Label |
| --- | --- | --- |
| Claude, expired OAuth file, no proxy | refresh could not resolve DNS (denied); 12 credential reads; Claude kept sending model requests with the expired bearer to the gate; nothing else reached any listener | N |
| Claude, expired, `HTTPS_PROXY` = gate | CONNECT `platform.claude.com:443` twice plus the hosts above; no local DNS attempt by Claude itself; all refused | N |
| Codex, expired, `CODEX_REFRESH_TOKEN_URL_OVERRIDE` = gate | 41 plaintext `POST /oauth/token` to the gate within about 0.4 s at startup, all refused; Codex then used the expired bearer | N |

Evidence: [refresh](quota-reserve-probes/nm/evidence/refresh.json). Without
the override, Codex refresh goes to `auth.openai.com:443` (S: observed by an
earlier node's Linux
[proxy research](quota-reserve-codex-proxy-research.md), not in this node).
The override is an undocumented knob (S); 0.166.0 honors it (N).

So refresh inside the boundary fails closed: no attempt bypassed the gate (N).
A working native refresh needs:

- TCP 443 to the OAuth host, through DNS (mDNSResponder) or a CONNECT proxy
  (N for the routing observed above);
- TLS verification of that host's public certificate. Claude uses roots
  bundled in its runtime by default (S: the bundle's CA-store code, with a
  system-store option through `CLAUDE_CODE_CERT_STORE=system` or
  `NODE_USE_SYSTEM_CA`) and looked up no trustd during TLS (N), so it should
  verify under M1 (I; public-root verification U). Codex loads the macOS
  platform roots (S), and under M1 those come back empty (N, trust-settings
  row above), so it would fail closed unless `trustd.agent` and probably
  SecurityServer are admitted or an explicit CA bundle is configured (I; U
  with the real host). Claude's system-store option would hit the same denial
  (I);
- write-back to the Keychain or the account's credential file (S for
  write-back; U under the profile).

Every option is user question 3. A refused refresh also makes both natives
keep using an expired bearer, so the real gate must refuse it explicitly (I).

## Nested native sandboxes

| Native | Inside the outer profile | Label |
| --- | --- | --- |
| Claude `sandbox.enabled=true` | its sandbox runtime could not listen on `TMPDIR/srt-mux-*.sock` (denied); the tool result said "Sandbox is enabled but failed to initialize: EPERM ... Sandboxing is disabled for the rest of this session", and the command did not run (N). Later commands would then run without Claude's sandbox but still inside the outer profile (I, from the message; only one tool call was made) | N, I |
| Codex `workspace-write` | the tool command failed: `sandbox-exec: sandbox_apply: Operation not permitted`, exit 71, with no fallback (N). Every later tool command would fail the same way (I; only one tool call was made) | N, I |

Evidence: [nested](quota-reserve-probes/nm/evidence/nested.json). Admitting
Claude's socket binds would not help: its runtime then applies its own
Seatbelt profile, and applying a second profile inside one is refused (N,
ipc nested case). The user-visible cost is user question 4.

## Compatibility cost

Under the enforced profile (N,
[compat](quota-reserve-probes/nm/evidence/compat.json)): `git clone` of a
local path works; `git clone` over HTTP to a loopback fixture fails ("Failed to
connect"); `ssh` to a loopback port fails (`Operation not permitted`, 255);
`curl` to `127.0.0.1` and to `localhost` fails (7); `pip download` fails but
misleadingly reports "No matching distribution"; `npm view` fails with
`connect EPERM`. An outside `curl` reached the same fixture. M1 is narrower
than the enforced profile, so the same breakage applies under it (I).

Also disclosed at G0 (N unless marked):

- No loopback TCP or UDP server inside the invocation: M1 denies every
  `network-bind`. This breaks dev servers, test HTTP fixtures, TCP language
  servers and local databases reached over TCP (I from the rule; N for Node
  and Codex bind denials).
- No Unix-socket IPC inside the invocation either: binding or connecting any
  pathname Unix socket is denied under M1, including in TMPDIR (N: the
  private-directory self test in m1.json, Claude's `srt-mux` socket in
  TMPDIR, Claude's `cc-socks` socket, and outside daemon sockets). This breaks
  tools that talk to themselves or to a daemon over a socket file, such as
  Python multiprocessing managers and forkserver, tmux, an in-invocation ssh
  `ControlMaster`, git's fsmonitor daemon, Docker or ssh-agent sockets, and
  language servers over Unix sockets (I; none was run). An allowance for the
  invocation's TMPDIR would restore the in-invocation cases; the aliasing
  checks above passed with such an allowance present, but other aliasing
  forms are U.
- No ICMP (ping-style datagram sockets are denied, N).
- No setuid tools: `ps` (N), and `sudo`, `crontab`, `at` by their setuid
  modes (I). Codex must run `--no-daemon`.
- No DNS for real names, no LaunchServices `open`, no Apple Events, no new
  launchd jobs (`submit`, `bootstrap`, `load` denied; `asuser` refused), no
  Keychain unless question 2 admits it. Job control in the terminal (Ctrl-Z,
  `fg`, a confined shell stopping its own jobs) keeps working (N).
- Native tool sandboxes do not work (question 4).
- The profile does not stop deferred effects that run later outside it:
  shell startup files, git hooks, preference domains written through cfprefsd,
  or files a sync client (iCloud Drive, OneDrive, Dropbox) uploads (I). A
  confined process can also ask launchd to `kickstart` an already defined
  service (the request reaches launchd, N), which then runs outside the
  profile; whether launchd honors it from inside is U, and only existing
  services can be started that way. These are not owned model work under PO's
  threat model, but the boundary is not a confidentiality boundary.

## Code size, dependency and review cost

- Probe renderer [sbpl.py](quota-reserve-probes/nm/sbpl.py): 227 lines,
  including log parsing, sanitizing and a Claude name mirror. A production
  renderer for the fixed shape above needs about 60 to 80 lines plus a startup
  self-test (I). No package, compiler, service or privilege.
- The real cost is stability. SBPL operation names, filters such as
  `ioctl-command TIOCSTI` and `remote tcp`, and denial logging were observed on
  one release only. Apple documents neither, and `sandbox-exec` is deprecated
  (N, S). Each supported macOS release needs the matrix rerun; an unknown
  release should refuse or require an explicit opt-in (I).
- The startup self-test turns a silent semantic change into a refusal rather
  than an escape, but only for the cases it checks (I).

## Remaining gaps for K and G0

- K: gate death and restart, port reuse, loss latch, closing persistent
  streams, queued child retry and bearer rotation inside M1 (U).
- Real Keychain read and refresh write-back under an admitted SecurityServer,
  and any native refresh success (U; question 2 and 3).
- Hooks, stdio MCP servers, plugins, background agents and Claude `!` shell
  mode are inside by inheritance (I) but were not exercised one by one.
- Terminal escape sequences that make an emulator answer into the input
  stream, OSC 52 clipboard writes and similar emulator features (U).
- trustd as an escape path (U); M1 simply does not admit it.
- The natives' TLS trust evaluation of real public certificates under M1 (U).
  Shown: both natives verify a fabricated CA given explicitly, under M1 (N);
  Codex's platform roots are unavailable under M1 (N), so its public-host
  TLS, including a tunnelled refresh, would fail closed (I); Claude's bundled
  roots need no trustd (I).
- A TLS gate, rather than a loopback HTTP gate or a CONNECT tunnel through
  it, and the Unix-socket gate exit the renderer supports (U).
- `launchctl kickstart` of an existing service from inside, and XPC services
  of app bundles started by launchd for a caller (U). The natives are CLI
  binaries without bundle-scoped XPC services, and global service names fall
  under the default mach-lookup deny (I).
- Ctrl-Z handling by the native TUIs themselves under M1 (U; terminal job
  control for a confined foreground job works, N).
- Unix-socket aliasing beyond one symbolic and one hard link, if an allowance
  is ever added (U).
- Other macOS releases, Intel, MDM-managed hosts, sleep and wake during long
  sessions (U).

## Containment and real-daemon contact

- Every probe ran in a private temp root created with `mktemp -d
  /tmp/quota-nm-XXXXXX`, with HOME, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, XDG
  directories and TMPDIR inside it. Three roots were used in sequence (the
  second for the datagram and recommended stages, the third for the M1, TLS
  and repeat runs); each is recorded in the ignored worktree cache and was
  removed after its evidence was copied, with no process naming it left (N).
- Real daemons contacted: securityd for one nonexistent random service name
  (control and admitted cases), LaunchServices for one unclaimed URL scheme
  (control), launchd for the denied submit, bootstrap, load and asuser calls,
  one kickstart of an absent label (inside and control), and the cleanup
  `list`, `print`, `bootout` and `remove` of the unique labels (never
  created), trust settings for the system and admin domains (certificate
  counts only; the user domain was never queried), the system log for tagged
  denials, and in census runs the mach services listed above. No Keychain item
  or launchd job was created (N: only nonexistent names were looked up, and
  every job label was absent after each attempt). Preference domains and GUI
  state were not checked; cfprefsd was admitted, so a native or tool could
  have written its own preference domain (I; none was expected).
- The TLS fixture used a CA and certificate fabricated with the host's
  `/usr/bin/openssl` inside the temp root, for the reserved name
  `nm-tls.test`, reached only through the gate's CONNECT tunnel; it was never
  added to any keychain or trust store.
- Natives were pointed only at loopback fixtures; every attempt to reach a
  real host was denied by the profile or refused by the fixture and is listed
  above. No real-host connection succeeded (N).
- Evidence was sanitized before it entered the tree: the remote root, Claude's
  project slug of it, the remote home and username, the local home and the
  host's interface address are placeholders, and the checker fails on any
  leftover (S, test).

## Reproduction and validation

Probe code and offline tests:
[quota-reserve-probes/nm/](quota-reserve-probes/nm/). `run.py` creates the
remote root, copies the probes, runs one stage under a remote stage deadline
(the probe aborts, records the stage as incomplete and still cleans up) and a
local ssh timeout 60 s longer, copies and sanitizes the result, and removes
the root on `cleanup` after checking for leftover processes. Stages:
boundary, ipc, fds, keychain, natives, natives-extra, nested, refresh, compat,
tty, hardened, dgram, recommended, m1, m1-natives, tls. `--rerun` writes a
repeat run to the ignored cache instead of the committed evidence.

```sh
python3 -I -B -m unittest discover -s docs/superpowers/plans/quota-reserve-probes/nm -p 'test_nm.py'
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nm/check_evidence.py [--dir DIR]
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nm/run.py [--rerun] STAGE... cleanup   # Mac host alias in NM_MAC_HOST
```

The offline tests cover profile rendering and its refusals, the Claude name
rule, report parsing, sanitizing, the root pattern, the ICMP message builder
and the driver's refusal of incomplete remote discovery. They also check every
committed evidence file, fail when one is missing, and require each targeted
mutation of a verdict to fail the checker with an assertion. `check_evidence.py`
is the gate for an evidence directory; it exits nonzero on a missing stage.
The tests start no native code.

Repeat runs: every committed file comes from one run of its stage. Earlier
runs of the same stages during harness development (stage-runs log: boundary
4, of which 2 stopped on a harness error; natives 5; ipc 4; others 1 to 2)
were overwritten, so their agreement is not recorded. After the M1 and TLS
stages were added, all 16 stages were run once more into the ignored cache
(November 12, 2026, same host, same native binaries by SHA-256, current probe
code), and every repeat passes the same checker (N; `check_evidence.py --dir`,
exit 0). The TLS repeat was taken a second time after a harness fix that
records the fixture when a root already holds one. So each committed verdict
agreed in at least one independent repeat; nothing beyond two runs per stage
is claimed. Verdicts that depend on timing windows are the fds listener counts
(a 2 s receive window), the Ctrl-Z case and the native turn waits; each agreed
in the repeat.

In the native files, `at` offsets are measured from fake-gate creation. In
three stages the first gate start took about 70 s on the harness side before
the native launched (N: the native's first request still arrived within the
40 s wait); a loopback reverse lookup in `http.server` is the likely cause
(I). These offsets are not native latency.
