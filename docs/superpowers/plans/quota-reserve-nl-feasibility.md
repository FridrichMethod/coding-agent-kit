# NL: Linux whole-invocation outer boundary feasibility

Status: **finite feasibility evidence complete on one Linux host; full A, G0
and WSL2 remain unproved**. One unprivileged composition confined the
complete tested invocation to a single gate port: Landlock (TCP connect to one
port, abstract-UNIX and signal scopes) plus a small seccomp socket filter.
Landlock alone did not. Every other candidate refused on this host, was not
enforced, or left a demonstrated gap. The working composition is a new kit-owned sandbox component, the
category PO asked not to adopt silently. It also has real compatibility costs.
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

1. Close every inherited descriptor except stdio.
2. Set `no_new_privs`.
3. Enter a Landlock domain that handles TCP connect only. It allows exactly
   the invocation's gate port and scopes abstract UNIX sockets and signals.
4. Add a seccomp filter. It allows `socket()` only for IPv4/IPv6
   `SOCK_STREAM` with protocol 0 or TCP, plus netlink route. It denies UDP,
   raw, MPTCP, SCTP, AF_UNIX, every other family and `io_uring`. It allows
   `socketpair(AF_UNIX)` and denies TCP Fast Open sends.

N: L2a denied every direct attempt in the decisive matrix. It also denied each
tested IPC escape, including `systemd-run --user`, while the gate stayed
reachable. Real Claude and real Codex (`--no-daemon`) ran in a PTY inside it.
Their model requests reached the fake gate, and their tool children and
detached grandchildren were denied direct egress.

It is not ready for adoption. The user questions in status.json ask for
decisions on:

- Q1 dependency: a kit-owned ctypes Landlock and seccomp launcher (about 250
  research lines, roughly 150 of them core, plus per-architecture syscall
  tables), against refusing Linux protected mode or waiting for newer kernels.
- Q2 AF_UNIX policy: denying it closes the host-daemon escapes. It breaks
  forkserver multiprocessing (the Python 3.14+ default), local UNIX IPC and
  Codex daemon mode. Allowing it reopens the escapes proven below.
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
| Present / absent tools | bwrap 0.9.0, slirp4netns, socat, systemd 255, strace present; firejail, nsjail, srt, pasta and Landlock-capable setpriv absent (N) |

Refusal conditions for an L2a adapter. Each refuses protected startup before
the native starts; none falls back silently:

- Not Linux, or Landlock unavailable: not built, or not in the boot `lsm=` list.
- Landlock ABI below 4, so no TCP port rules. The Ubuntu 24.04 GA 6.8 kernel
  is ABI 4 (S, headers). L2a then works without signal scope (I): descendants
  could signal outside processes. Landlock-only (L1) would refuse there,
  because it needs the ABI 6 abstract-UNIX scope.
- seccomp unavailable, or an unvalidated architecture. Only x86_64 ran here;
  the arm64 table is review data only (U).
- A kernel that fixes Landlock erratum 1 lets MPTCP bypass TCP rights. The
  seccomp layer is therefore mandatory on such kernels, not optional (N on this
  kernel, S for the erratum).
- WSL2: not available here; U. Microsoft's WSL2 kernel version and Landlock
  configuration must be checked on a real host; a 6.6-based kernel would
  predate ABI 4 (I).

## Candidate verdicts

| Candidate | Verdict on this host | Evidence |
| --- | --- | --- |
| L1: Landlock only (TCP connect = gate port, scopes) | **Gap.** Denies direct TCP v4/v6/v4-mapped and TFO to other ports. Leaves MPTCP, UDP, pathname-UNIX host daemons, the D-Bus session bus and `systemd-run --user` escapes open | N matrix |
| L2a: L1 + seccomp denying AF_UNIX, UDP, MPTCP/other, io_uring | **Works**, at the compatibility costs below | N matrix, natives, compat |
| L2b: like L2a but AF_UNIX allowed | **Gap.** Closes UDP and MPTCP but not host daemons, D-Bus, `systemd-run`, or the Codex outside-daemon attachment | N matrix, natives |
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
| User D-Bus session bus, SASL AUTH | OK | **OK** | EAFNOSUPPORT | **OK** |
| Signal to an outside process | delivered | EPERM (scope) | EPERM | EPERM |
| `systemd-run --user` transient unit connects out | n/a | **reached** | bus connect EAFNOSUPPORT | **reached** |

Node and curl match the Python rows they exercise. The detached child kept
the boundary after its parent exited and it was reparented. Ground truth
showed no alternate-listener marker from any L2a actor. The transient units
were collected, `LoadState=not-found` (N).

Port-only rule (N): Landlock names a port, not an address. A host on the gate
port, modeled by `127.0.0.2:P`, is reachable from every layer. Mitigation is
inference only. Give the gate a random ephemeral loopback port per invocation,
never 80/443 or a proxy port, so a non-malicious client is not configured for
it. No ABI 8 mechanism restricts the address (S). A UNIX-socket gate does not
help: the natives need a TCP base URL, and pathname-UNIX connects are not
Landlock-controlled before ABI 9.

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
connect, send, exec and fork calls; it denies nothing. The fake gate returns
one shell tool call that runs the attempt actor and a `setsid -f` detached
child, then ends the turn. Natives get a recording stand-in session bus, not
the real one.

| Native and mode | Result (N) |
| --- | --- |
| Claude, L2a | Model requests with the fabricated bearer reach the gate; the Bash tool and its detached child are denied direct egress; the TUI answers, exits 0 and restores termios. Apart from the gate connection and netlink route sockets, Claude and its startup helpers (`gh`, `git-remote-https`, `ssh`, `zsh`) attempted only UDP DNS and AF_UNIX sockets. All were denied, and none was needed for the turn |
| Claude, L2b | Same, but its tool can use the outside UNIX daemon relay (gap), and its `gh` helper connects to the session bus (recorded by the stand-in bus) |
| Codex default, L2a | Refuses before any model request: the native app-server daemon control socket needs AF_UNIX (fail closed) |
| Codex `--no-daemon`, L2a | Works like Claude: model requests with fabricated auth reach the gate, tool and detached child denied, exit 0. Codex also made one connect to an ephemeral non-gate loopback port, which was denied (EACCES); its purpose is unidentified (U) and the turn completed |
| Codex default, L2b | Its self-started daemon is a confined descendant; the tool is denied. Codex itself connects to the session bus. The daemon outlives the TUI ("Any running work continues") and was killed by cleanup |
| Codex default, L2b, daemon already running outside | **Escape**: the in-boundary TUI attaches to the outside daemon, and the tool runs in the daemon's domain. Its direct TCP reaches the alternate listener |

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
| `multiprocessing.Pool`, default forkserver (Python 3.15) | works | **fails** | works |
| `multiprocessing` fork context, local git commit, `localhost` lookup | works | works | works |

In practice, while protection is on, every non-gate exit is gone. That covers
package installs, git remotes, ssh, remote notebooks, local databases or dev
servers, and Jupyter-style kernels on TCP. L2a also breaks UNIX-socket IPC:
forkserver pools, gpg and ssh agents, tmux, X11 and Codex daemon mode.
Pause/loss closure (PO item 4) adds nothing new at the boundary: it is already
closed for everything except the gate.

## Code size, dependency and review cost

- [boundary.py](quota-reserve-probes/nl/boundary.py), research only: 249
  lines. About 60 are Landlock, about 50 are the seccomp program and its
  assembler, and the rest is parsing and refusal. The seccomp program is 38
  classic-BPF instructions on x86_64. It needs per-architecture syscall
  numbers: x86_64 validated, arm64 table untested.
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
  working. MPTCP would still need seccomp, unless a later ABI covers it (U).

## Remaining gaps for K and G0

- Port-only Landlock rule; random gate port mitigation only (I).
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
- WSL2 actual capability: outstanding platform gate (U).

## Containment and real-daemon contact

Every listener is loopback and every provider is a fixture. The natives ran
only under L2a or L2b, never under L1 or unconfined, so the seccomp layer
denied their DNS. The observer recorded no non-loopback connect from any
native. Two contacts with real user daemons were deliberate, because the
brief requires those escape tests. The matrix sent one D-Bus SASL `AUTH` line
(no method call) to the session bus. The matrix and host checks created
transient `systemd-run --user` units named `quota-nl-*`, which were collected
afterwards. Native runs received a recording stand-in bus instead of the real
one. The Codex sandbox checks ran under L1 with the strace observer, which
saw no non-loopback attempt.

## Reproduction and validation

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nl/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nl/check_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nl/test_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/nl/test_boundary.py
```

The runner gives each stage a process group and a timeout. It sanitizes the
evidence, records temporary roots under `.cache/nl/`, and fails if a probe
process, temporary root or `quota-nl-*` transient unit remains. The checker
needs the controls to succeed and binds every evidence digest to the
manifest, and the manifest to the probe code that produced it. The
regression tests show that a weakened capture, a missing control or changed
probe code fails.
