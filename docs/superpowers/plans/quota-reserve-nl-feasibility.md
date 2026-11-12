# NL: Linux whole-invocation outer boundary feasibility

Status: **finite feasibility evidence complete on one Linux host; full A, G0
and WSL2 remain unproved**. One unprivileged composition confined the
complete tested invocation's egress to a single gate port: Landlock (TCP
connect to one port, abstract-UNIX and signal scopes) plus a small seccomp
socket filter. Landlock alone did not. Every other candidate refused on this
host, was not enforced, or left a demonstrated gap. Review round 1 found an
AF_UNIX datagram channel that the first filter left open; the filter now
closes it, and all evidence was regenerated with the corrected code. The
working composition is a new kit-owned sandbox component, the category PO
asked not to adopt silently. It also has real compatibility costs.
Both are G0 decisions, recorded below as user questions. No product runtime
changed.

Base: `0c5b492` (branch `ci/quota-reserve`). Captured November 12, 2026 UTC on
Ubuntu 24.04.5 LTS, HWE kernel `7.0.0-42-generic`, x86_64, not WSL. Natives:
Claude Code `2.1.322` and `codex-cli 0.167.2`; binary SHA-256 values are in
[the manifest](quota-reserve-probes/nl/evidence/manifest.json). All tokens,
accounts and model responses are fabricated. No login, real provider request,
real credential read, installation, privilege or system change was used.

Labels: N = native observation in this node; S = inspected source,
header or documentation; I = inference; U = unproved.

## Decision and user questions

Recommended Linux adapter for G0 review, called L2a here. It is applied by a
launcher before the native starts and inherited by every descendant:

1. Refuse if stdio (fd 0-2) is a socket, then close every other inherited
   descriptor.
2. Set `no_new_privs`.
3. Enter a Landlock domain that handles TCP connect only. It allows exactly
   the invocation's gate port and scopes abstract UNIX sockets and signals.
4. Add a seccomp filter. It allows `socket()` only for IPv4/IPv6
   `SOCK_STREAM` with protocol 0 or TCP, plus netlink route. It denies UDP,
   raw, MPTCP, SCTP, every other family, every AF_UNIX `socket()` and
   `io_uring`. It allows `socketpair(AF_UNIX)` only for `SOCK_STREAM` and
   `SOCK_SEQPACKET`, whose connected ends cannot be re-addressed; a datagram
   or raw (datagram alias) pair could `sendto()` or `connect()` any outside
   socket. It also denies TCP Fast Open sends.

N: L2a denied every direct attempt in the decisive matrix. It also denied each
tested IPC escape, including AF_UNIX datagrams from `socketpair()` and
`systemd-run --user`, while the gate stayed reachable. Real Claude and real
Codex (`--no-daemon`) ran in a PTY inside it. Their model requests reached
the fake gate, and their tool children and detached grandchildren were denied
direct egress. The boundary governs egress only: in-boundary processes can
still listen and accept connections that outside parties start (see Ingress).

It is not ready for adoption. The user questions in status.json ask for
decisions on:

- Q1 dependency: a kit-owned ctypes Landlock and seccomp launcher (about 270
  research lines, roughly 160 of them core, plus per-architecture syscall
  tables), against refusing Linux protected mode or waiting for newer kernels.
- Q2 AF_UNIX policy: denying AF_UNIX `socket()` and datagram `socketpair()`
  closes the host-daemon and datagram escapes. It breaks forkserver
  multiprocessing (the Python 3.14+ default), local UNIX IPC, datagram
  socketpairs and Codex daemon mode. Allowing it reopens the escapes proven
  below.
- Q3 local TCP: every connect to a port other than the gate's is denied,
  including loopback test and dev servers.
- Q4 Codex: protected mode must pass `--no-daemon` or refuse, because a TUI
  attaches to an existing daemon outside the boundary.

## Host facts and refusal conditions

| Fact | Value and label |
| --- | --- |
| Landlock | ABI 8, errata bitmask 15 (N, `landlock_create_ruleset` version and errata queries) |
| Accepted access bits | FS bits 0..15, net bits 0..1 (TCP bind/connect), scope bits 0..1 (N, ruleset creation probe) |
| uapi header 7.0 | ABI 8 adds `LANDLOCK_RESTRICT_SELF_TSYNC`; no UDP, no pathname-UNIX right (S) |
| Older headers present | 6.8 libc headers: net rights only (ABI 4); 6.14 HWE headers: plus ioctl and scopes (ABI 6) (S) |
| [Upstream docs](https://docs.kernel.org/userspace-api/landlock.html) (7.4-rc2, read on the capture date) | ABI 9 adds `LANDLOCK_ACCESS_FS_RESOLVE_UNIX`; ABI 10 adds UDP bind/connect-send; erratum 1 leaves MPTCP, SMC and SCTP outside TCP rights (S) |
| User namespaces | `kernel.apparmor_restrict_unprivileged_userns=1`; `unshare -U` succeeds without capabilities; uid map and netns setup fail (N) |
| MPTCP / TFO | `net.mptcp.enabled=1`, `tcp_fastopen=1` (N) |
| Terminal injection | `dev.tty.legacy_tiocsti=0`: unprivileged TIOCSTI is disabled (N) |
| Present / absent tools | bwrap 0.9.0, slirp4netns, socat, systemd 255, strace present; firejail, nsjail, srt, pasta and Landlock-capable setpriv absent (N) |

Refusal conditions for an L2a adapter. Each refuses protected startup before
the native starts; none falls back silently:

- Not Linux, or Landlock unavailable: not built, or not in the boot `lsm=` list.
- Landlock ABI below 4, so no TCP port rules. The Ubuntu 24.04 GA 6.8 kernel
  is ABI 4 (S, headers). L2a then works without signal scope (I): descendants
  could signal outside processes. L2a needs no abstract-UNIX scope, because
  it leaves no AF_UNIX socket that can address an outside one: `socket()` is
  denied and the allowed stream and seqpacket pairs cannot be re-addressed (N
  on this kernel, I for ABI 4 and 5). The first filter allowed datagram pairs,
  which on ABI 4 and 5 could also have reached abstract datagram sockets.
  Landlock-only (L1) and L2b would refuse there, because they need the ABI 6
  abstract-UNIX scope.
- stdio is a socket, for example a launcher started by a service whose output
  goes to the journal socket. Landlock and seccomp do not police existing
  sockets, so the research launcher refuses (offline unit test of the
  detection; refusal with a socket stdin seen in a scratch run, N, not in the
  evidence set).
- `dev.tty.legacy_tiocsti=1`, or a kernel without that switch (before 6.2, I).
  A descendant that shares the user's controlling terminal can then push
  input with TIOCSTI that the user's shell runs after the native exits,
  outside the boundary. This host reads 0 (N). The native test used its own
  `setsid` PTY, so the product arrangement (the native on the user's
  terminal) was not exercised (U). G0 must refuse on such hosts or deny
  `ioctl(TIOCSTI)` in seccomp.
- seccomp unavailable, or an unvalidated architecture. Only x86_64 ran here;
  the arm64 table is review data only (U).
- A kernel that fixes Landlock erratum 1 lets MPTCP bypass TCP rights. The
  seccomp layer is therefore mandatory on such kernels, not optional (N on this
  kernel, S for the erratum).
- WSL2: not available here; U. Microsoft's WSL2 kernel version and Landlock
  configuration must be checked on a real host; a 6.6-based kernel would
  predate ABI 4 (I). Two WSL-specific delegation classes must be checked
  there as well (U). Windows interop: an in-boundary process can start a
  Windows `.exe` through binfmt_misc, which reaches the Windows side through
  the WSL interop socket or AF_VSOCK; L2a's AF_UNIX and family denials should
  block that path (I), and Windows processes are outside every Linux
  boundary. drvfs: files under `/mnt/c` that Windows-side processes read are
  file-mediated delegation that network rules do not cover.

## Candidate verdicts

| Candidate | Verdict on this host | Evidence |
| --- | --- | --- |
| L1: Landlock only (TCP connect = gate port, scopes) | **Gap.** Denies direct TCP v4/v6/v4-mapped and TFO to other ports. Leaves MPTCP, UDP, pathname-UNIX host daemons and datagram sockets, the D-Bus session bus and `systemd-run --user` escapes open | N matrix |
| L2a: L1 + seccomp denying AF_UNIX `socket()`, datagram `socketpair()`, UDP, MPTCP/other, io_uring | **Works** for egress, at the compatibility costs below | N matrix, natives, compat |
| L2b: like L2a but AF_UNIX allowed | **Gap.** Closes UDP and MPTCP but not host daemons, pathname-UNIX datagrams, D-Bus, `systemd-run`, or the Codex outside-daemon attachment | N matrix, natives |
| bubblewrap / user namespaces | **Refuses.** `--unshare-net`: `loopback: Failed RTM_NEWADDR: Operation not permitted`; `--unshare-user`: uid map denied | N host |
| Codex sandbox helper (`codex sandbox`) | **Refuses** in all three tried modes; no gate-only mode exists | N host, strings of the pinned binary |
| `systemd-run --user` properties | **Not enforced / gap.** `IPAddressDeny=any` and `PrivateNetwork=yes` silently unenforced; `RestrictAddressFamilies` works only for services, cannot split UDP from TCP, and reparents the process under the user manager | N host |
| firejail, nsjail, srt, setpriv Landlock | Absent; installation not authorized | N |

bubblewrap would work on hosts without the AppArmor userns restriction. It
would also work with a profile that grants bwrap userns capabilities, or on a
distribution that permits unprivileged user namespaces (I; Ubuntu 24.04's
default refuses). Such a change is a system configuration change outside this
node's authorization. bwrap would still need a gate bridge into the empty
network namespace (for example slirp4netns or a socket relay), which was not
testable here (U). The Codex helper failed three ways. Default: bwrap
`RTM_NEWADDR`. Legacy Landlock: panic, `filesystem-restricted execution
requires bubblewrap to isolate app-server sockets`. Network enabled: bwrap
uid map. strace saw no non-loopback attempt in any mode. Its proxy mode also
needs a network namespace, and its domain policy cannot classify model
traffic (PO, S).

## Decisive matrix

[probe.py](quota-reserve-probes/nl/probe.py) runs actors from
[attempt.py](quota-reserve-probes/nl/attempt.py). The actors are a Python
root, an `sh` child, an `env -i` absolute-path Python, a Node client,
absolute-path `curl` under `env -i`, and a `setsid` double-forked child that
acts only after the root exits. Fixture listeners run outside every boundary
and record each received marker. That record decides the result, not the
actor's report. L0 is an unrelated unconfined process that runs concurrently
with L2a.

| Attempt (all actors) | L0 | L1 | L2a | L2b |
| --- | --- | --- | --- | --- |
| TCP v4, v6, v4-mapped to the alternate port | reached | EACCES | EACCES | EACCES |
| TCP Fast Open send to the alternate port | reached | EACCES | EOPNOTSUPP (seccomp) | EOPNOTSUPP (seccomp) |
| MPTCP to the alternate port | reached | **reached** | EPROTONOSUPPORT | EPROTONOSUPPORT |
| UDP v4, v6, connected UDP | reached | **reached** | EPROTONOSUPPORT | EPROTONOSUPPORT |
| TCP v4 and v6 to the gate | reached | reached | reached | reached |
| Gate port on another address (127.0.0.2) | reached | **reached** | **reached** | **reached** |
| Pathname-UNIX host daemon that relays out | relayed | **relayed** | EAFNOSUPPORT | **relayed** |
| Abstract UNIX socket outside | reached | EPERM (scope) | EAFNOSUPPORT | EPERM (scope) |
| AF_UNIX datagram `socket()` sendto an outside pathname datagram socket | reached | **reached** | EAFNOSUPPORT | **reached** |
| Datagram `socketpair()`, then sendto or connect to that outside socket | reached | **reached** | EPROTONOSUPPORT | **reached** |
| Datagram `socketpair()` sendto an outside abstract datagram socket | reached | EPERM (scope) | EPROTONOSUPPORT | EPERM (scope) |
| Stream or seqpacket `socketpair()` sendto that outside path | never reached | never | never | never |
| User D-Bus session bus, SASL AUTH | OK | **OK** | EAFNOSUPPORT | **OK** |
| Signal to an outside process | delivered | EPERM (scope) | EPERM | EPERM |
| `systemd-run --user` transient unit connects out | n/a | **reached** | bus connect EAFNOSUPPORT | **reached** |

Node and curl match the Python rows they exercise. The detached child kept
the boundary after its parent exited and it was reparented. Ground truth
showed no marker from any L2a actor at any fixture. The transient units
were collected, `LoadState=not-found` (N).

The datagram rows come from review round 1. The reviewer showed, with a copy
of the first launcher, that `socketpair(AF_UNIX, SOCK_DGRAM)` stayed allowed
while AF_UNIX `socket()` was denied, and that `sendto()` or `connect()` on it
delivered to an outside pathname datagram server. The filter now allows only
stream and seqpacket pairs. Stream pairs give EISCONN, and a seqpacket pair
ignores the address and sends to its peer. The L0 control reached both
datagram fixtures, and no L2a actor reached either (N). The abstract row also
shows that Landlock's abstract scope covers datagram sends (N). On this host
the visible pathname datagram endpoints are journald, the systemd notify
sockets and chronyd's command socket; none is known to delegate execution or
egress (reviewer observation, I).

Port-only rule (N): Landlock names a port, not an address. A host on the gate
port, modeled by `127.0.0.2:P`, is reachable from every layer. Mitigation is
inference only. Give the gate a random ephemeral loopback port per invocation,
never 80/443 or a proxy port, so a non-malicious client is not configured for
it. No ABI 8 mechanism restricts the address (S). A UNIX-socket gate would not
help (I). The natives were configured with HTTP base URLs over TCP, and
whether they accept a UNIX-socket transport was not examined. Pathname-UNIX
connects are not Landlock-controlled before ABI 9 (S), so L2a would have to
allow AF_UNIX again.

Ingress (egress-only scope). Landlock handles only TCP connect here, so
in-boundary processes can bind and listen on any port and interface and
accept connections that outside parties start (S for the handled right, I for
the consequence; not tested end to end). Codex itself listened on a
127.0.0.1 port (N, observer). Handling `LANDLOCK_ACCESS_NET_BIND_TCP` would
limit explicit `bind()`, but not `listen()` on an unbound socket, which
autobinds an ephemeral port on every interface. In one scratch check on this
kernel, with that right handled and no rule, `bind()` to port 0 and to a
fixed port gave EACCES while `listen()` autobound `0.0.0.0` (N, scratch run,
not in the evidence set). Ingress needs a cooperating outside party, so it is
not a demonstrated escape. "Only the gate exit is reachable" means egress.

## Descriptors, latch and port reuse

- Inherited descriptors (N). A faulty launcher model kept a connected TCP
  socket, a connected UDP socket and a listening socket. All three stayed
  usable inside L2a: Landlock and seccomp do not police existing sockets. The
  default launcher closed them before confinement. Each became EBADF, and an
  outside connect to the old listener was refused. Closing descriptors is
  therefore a required launcher step, not an optional hardening.
- Latch (N). After the gate shut an admitted stream, the in-boundary actor's
  further send saw EOF. The gate recorded nothing after the latch. Reconnect
  was refused, the alternate port gave EACCES and UDP gave EPROTONOSUPPORT. The
  boundary offers no bypass. Closing streams remains the gate's job (PC4).
  The fixture had to `shutdown()` its listener: `close()` from another thread
  left a blocked `accept()` serving one more connection.
- Port reuse (N). While a holder kept the gate's listening socket, an outside
  bind of the same address failed with EADDRINUSE. The in-boundary connect sat
  unanswered in the backlog. After release, an outside impostor bound the port
  and the confined actor reached it. A gate restart or death must keep the
  port held by an invocation-owned process for the invocation's lifetime. K
  must test this with the real gate (I).

## Real natives inside L2a

[natives.py](quota-reserve-probes/nl/natives.py) starts the actual TUI on its
own PTY inside the boundary. Outside, an observer `strace -ff` records socket,
socketpair, connect, send, bind, listen, exec and fork calls and each
process's exit status; it denies nothing. The fake gate returns one shell
tool call that runs the attempt actor and a `setsid -f` detached child, then
ends the turn. Natives get a recording stand-in session bus, not the real
one.

| Native and mode | Result (N) |
| --- | --- |
| Claude, L2a | Model requests with the fabricated bearer reach the gate; the Bash tool and its detached child are denied direct egress, including AF_UNIX datagrams; the TUI answers, exits 0 and restores termios. Apart from the gate connection, netlink route sockets and 47 stream socketpairs, Claude and its helpers (`env`, `gh`, `git-remote-https`, `ps`, `ssh`, `zsh`) created only UDP sockets (for DNS by inference, I: `socket()` was denied before any send) and AF_UNIX sockets. All were denied, and none was needed for the turn |
| Claude, L2b | Same, but its tool can use the outside UNIX daemon relay and reach the outside datagram socket (gap), and its `gh` helper connects to the session bus (recorded by the stand-in bus) |
| Codex default, L2a | Refuses before any model request: the native app-server daemon control socket needs AF_UNIX (fail closed) |
| Codex `--no-daemon`, L2a | Works like Claude: model requests with fabricated auth reach the gate, tool and detached child denied, exit 0. A Codex process listened on an ephemeral 127.0.0.1 port and Codex then connected to that port; the port rule denied the self-connect (EACCES). Its purpose is unidentified (U). The turn completed. The TUI showed the same warning count (1, then 2) in the no-daemon, L2b and outside-daemon runs. In the last run no traced process made that connect, but the outside daemon was not traced, so no effect of the denial was seen and none is excluded (U) |
| Codex default, L2b | Its self-started daemon is a confined descendant; the tool is denied. Codex itself connects to the session bus. The TUI exits 0, but the daemon outlives it ("Any running work continues") and was killed by cleanup |
| Codex default, L2b, daemon already running outside | **Escape**: the in-boundary TUI attaches to the outside daemon, and the tool runs in the daemon's domain. Its direct TCP reaches the alternate listener |

Natives used only stream socketpairs (Codex also seqpacket), so the datagram
pair denial cost them nothing in these runs (N). The PTY helper gives the
native its own session, so cleanup does not rely on the observer's process
group: it first kills every live process whose environment, cwd, command line
or executable names the private root, or whose PID the observer recorded and
that started after the probe with `no_new_privs`, then the observer. The
native's own exit status comes from the observer's record of the root
process. In every working run the native exited 0 on its own, and the checker
requires it (N). Before review round 1, cleanup matched only command lines
and executables, so a hung Claude TUI would have been missed.

The outside daemon ran in a sibling research domain that allows only the gate
and alternate ports. That makes the escape observable without real network
access. The domain causes the reachability, never a denial. The Codex daemon
also exposes `enable-remote-control` (S, help text): a remote delegation path
that protected mode must refuse or prove absent (U).

## Compatibility cost

| Workflow (loopback fixtures) | L0 | L2a | L2b |
| --- | --- | --- | --- |
| git clone over HTTP, curl and urllib download, Node fetch | works | fails (EACCES) | fails |
| ssh and git over ssh | reaches banner | `connect ... Permission denied` | same |
| Local TCP server plus client on any non-gate port | works | fails | fails |
| Local UNIX socket server | works | fails (EAFNOSUPPORT) | works |
| Datagram `socketpair(AF_UNIX)` | works | fails (EPROTONOSUPPORT) | works |
| Stream `socketpair(AF_UNIX)`, asyncio event loop | works | works | works |
| `multiprocessing.Pool`, default forkserver (Python 3.15) | works | **fails** | works |
| `multiprocessing` fork context, local git commit, `localhost` lookup | works | works | works |

In practice, while protection is on, every non-gate exit is gone. That covers
package installs, git remotes, ssh, remote notebooks, local databases or dev
servers, and Jupyter-style kernels on TCP. It also covers a process that
connects to its own loopback listener, as Codex did. L2a also breaks
UNIX-socket IPC: forkserver pools, gpg and ssh agents, tmux, X11, datagram
socketpairs and Codex daemon mode.
Pause/loss closure (PO item 4) adds nothing new at the boundary: it is already
closed for everything except the gate.

## Code size, dependency and review cost

- [boundary.py](quota-reserve-probes/nl/boundary.py), research only: 272
  lines. About 60 are Landlock, about 55 are the seccomp program and its
  assembler, about 10 are the stdio check, and the rest is parsing and
  refusal. The seccomp program is 42 classic-BPF instructions on x86_64. It
  needs per-architecture syscall numbers: x86_64 validated, arm64 table
  untested.
- Offline [unit tests](quota-reserve-probes/nl/test_boundary.py) evaluate the
  BPF program with a small interpreter over synthetic `seccomp_data`.
- Run-time dependencies: none beyond Python 3.11+ ctypes and the kernel. It is
  still a kit-owned sandbox. Reviewers must check BPF correctness, syscall
  tables, errno choices and new kernel families. The ABI evolution (9, 10, and
  MPTCP coverage) must be tracked. AGENTS.md proportionality and PO's
  preference require G0 to accept that explicitly.
- Kernels with ABI 9 and 10 could replace the AF_UNIX denial with
  domain-scoped pathname-UNIX control, and the UDP filter with Landlock UDP
  rules (S). On ABI 10 that would keep forkserver and self-spawned daemons
  working (I). MPTCP would still need seccomp, unless a later ABI covers it
  (U).

## Remaining gaps for K and G0

- Port-only Landlock rule; random gate port mitigation only (I).
- Egress only: in-boundary processes can listen and accept inbound
  connections (I). Codex listened on 127.0.0.1 (N); `listen()` autobinds
  even with TCP bind handled (scratch N).
- TIOCSTI on hosts with `dev.tty.legacy_tiocsti=1` or older kernels: refuse or
  deny the ioctl in seccomp. The product terminal arrangement is untested (U).
- Gate death and port reuse need an invocation-owned port holder. Monitor
  loss, queued children and continuation with the real gate belong to K.
- Filesystem-mediated delegation is not covered: FIFOs read by outside
  daemons, spool or crontab files and later-loaded shell rc files. Only
  setuid helpers are neutralized by `no_new_privs` (I, not tested).
- Native refresh and model-free quota under the boundary are not tested. Both
  must go through the gate port or an owned exit (Q, K).
- Codex daemon remote control, and session-bus use by Codex and by Claude's
  `gh` helper when AF_UNIX is allowed, need native-specific decisions. A
  credential store reached over the bus would be outside the boundary (U).
- Codex's own loopback listener and its denied self-connect: purpose
  unidentified (U).
- WSL2 actual capability, including Windows interop and drvfs delegation:
  outstanding platform gate (U).

## Containment and real-daemon contact

Every listener is loopback and every provider is a fixture. The native TUI
sessions ran only under L2a or L2b, so the seccomp layer denied their UDP
sockets. The observer recorded no non-loopback connect from any native. Some
native calls ran outside any boundary and unobserved: `run.py` ran
`claude --version` and `codex --version`, and `hostfacts.py` ran
`codex --version`, each with a scratch or temporary HOME and a minimal PATH
(version queries only, low risk). Two contacts with real user daemons were
deliberate, because the brief requires those escape tests. The matrix sent
one D-Bus SASL `AUTH` line (no method call) to the session bus. The matrix and host checks created
transient `systemd-run --user` units named `quota-nl-*`, which were collected
afterwards. Native runs received a recording stand-in bus instead of the real
one. The `codex sandbox` checks ran inside the L1 domain (TCP connect only to
port 9) with the strace observer, which saw no non-loopback attempt. Their
refusals are attributed to bubblewrap because unwrapped `bwrap` gives the
same `RTM_NEWADDR` and uid-map errors on this host (N); the L1 wrapping itself
was not ruled out as a contributor.

## Reproduction and validation

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nl/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nl/check_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nl/test_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nl/test_boundary.py
```

The runner gives each stage a process group and a timeout. It sanitizes the
evidence, records temporary roots under `.cache/nl/`, and fails if a probe
process (matched by command line, environment or cwd), temporary root or
`quota-nl-*` transient unit remains. The checker
needs the controls to succeed and binds every evidence digest to the
manifest, and the manifest to the probe code that produced it. The
regression tests show that a weakened capture, a missing control or changed
probe code fails.
