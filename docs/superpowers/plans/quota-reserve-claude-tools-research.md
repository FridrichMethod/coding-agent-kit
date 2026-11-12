# PA1T: actual Claude local Bash behavior during model pause

Status: **the two bounded local-tool observations are complete**. Closing the
model gate alone let the fixed Bash command finish. Closing it and sending
one native Ctrl-C after the command started ended the observed shell before
its completion marker. Both cases retained the native TUI and rejected later
model admission. These findings close PA1C's explicit local-tool evidence
gap; they do not establish full A or open G0.

Base: `8c87497fee502b72c4323ab86cddaa95868e182f`. Captured November 12, 2026
UTC on Linux x86_64 using Claude Code `2.1.322`, binary SHA-256
`bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478`.
All credentials, account fields and model responses were fabricated. No real
account, login, provider endpoint, usage query, model generation or audio
device was used. No launcher, configuration contract or runtime changed.

## The fixed native experiment

Each case launches actual interactive Claude on its own controlling PTY,
without print/headless mode. HOME, Claude config, XDG directories, temporary
directory and working directory are private. Only the native Bash tool is
enabled. The loopback Messages SSE fixture supplies exactly this fixed call:

```sh
printf "%s\n" "$$" > tool.pid; printf "started\n" > started; sleep 4; printf "finished\n" > finished; printf "PA1T command finished\n"
```

The harness requires `started` before requesting pause, verifies that
`finished` is absent, and records the shell PID, Linux process start ticks,
session/group identity and its observed sleep child. It closes the same
model gate in both cases. Only the second sends one native Ctrl-C while the
shell's start identity is still present. The five-second observation window
ends before any UI cleanup shortcuts or process cleanup. Later Ctrl-C presses
cancel native retries or exit the interface; they are not part of the
single-interrupt experiment.

PA1C's unchanged `claude_control/gate.py` is imported by physical path. Its
existing PC4 utility import remains unchanged. The PA1C private stored-OAuth
fixture shape and Messages SSE protocol are reused in a small dedicated
driver. The gate admits a single fake model response, then monitor EOF
latches it closed. There is no quota query or continuation grant experiment
here. PA1C's root/child/grandchild, controller-loss and continuation matrices
were not rerun, and PC5's Codex observations are not treated as Claude facts.

The runner installs the unchanged PC syscall network guard before the probe,
gate, native auth-status/version checks or TUI execute. Its independent
self-check confirms loopback TCP and Unix sockets, rejection of external and
unsupported sockets, and inheritance through exec/new sessions. All native
launches close inherited descriptors and use the explicit private environment.
The known hosted credential paths must be absent. The runner has a 45-second
case bound, uses the guard's EXITKILL on timeout, and acts as a subreaper to
collect adopted descendants, including after guard failure. Cleanup signals
target only owned process groups. This retains the guard's documented
trusted-binary and sockaddr TOCTOU limits; it is not arbitrary-code isolation.

## Observed results

| Observation | Gate closure only | Gate closure plus one Ctrl-C |
| --- | --- | --- |
| Command started before pause | Yes; live shell and sleep child | Yes; live shell and sleep child |
| Ctrl-C relative to pause request | None during local observation | 25.27 ms later, after gate latch |
| Finished marker | Present; marker timestamps differ by 4.001 s | Absent throughout the five-second window |
| Shell lifecycle | Gone when completion was sampled | Gone 25.20 ms after Ctrl-C was sent |
| Observed sleep child after five seconds | Gone | Zombie, no longer executing; collected by runner afterward |
| Native tool history | Successful result, stdout `PA1T command finished`, `interrupted=false` | Error result, `toolUseResult` is the string `User rejected tool use` |
| Model requests forwarded after pause | Zero | Zero |
| New prompt submitted through native TUI | Rejected by closed gate | Rejected by closed gate |
| History UI, normal exit, terminal | Ctrl-O detailed view, exit 0, termios restored | Ctrl-O detailed view, exit 0, termios restored |

Times are observations from one final run, with monotonic sampling around
25 ms. Marker elapsed time uses the two files' modification timestamps. The
gate-only follow-up after tool completion also reached the closed gate and
was rejected. The later explicit prompt tests usable native input separately.
No request after the initial fixed response reached the upstream fixture.
Stored fake credential files were byte-for-byte unchanged in both cases.

The interruption case establishes that this command had already performed
its first side effect, then its observed shell ended without the completion
marker. Native history's generic rejection wording must not be read as proof
that no command ran. The probe does not obtain the shell's wait status or
termination signal, so its evidence deliberately does not claim proven
signal termination. A missing marker alone is never the basis of the result:
the original process identity is gone and native history contains the error.
The observed sleep descendant was a zombie at the end of the window, not a
running command. No harness cleanup caused either lifecycle observation.

These captures support neither a universal Ctrl-C termination guarantee nor
a promise that local tools always continue. Different timing, native versions,
background tools or arbitrary subprocess trees can behave differently. Model
admission pause and local side effects therefore require separate semantics.
In particular, closing network admission alone does not cancel a running
local command. Effects already performed are not undone by interruption.

## Evidence, failures and verification

The [probe subtree](quota-reserve-probes/claude_tools/) contains sanitized
terminal output, native transcript tool results, marker/process observations,
gate events and a manifest binding the exact native binary, source files and
both evidence files. The native transcript is read only from private config;
no user history is consulted. Exact temporary-root lists, fake native logs,
raw output and regular-file inventories are retained in ignored
`.cache/claude_tools/` for the coordinator to archive before worktree removal.

The initial absent-capture test failed as intended. The first native run
observed the same qualitative difference, normal exits and restored termios,
but its final classifier incorrectly expected every `toolUseResult` to be a
dictionary. The cancellation path returned a string, causing an AttributeError.
A focused regression failed before the parser was corrected. That failed
capture remains archived; it is not silently reported as a passing run.
A subsequent attempt hit the unchanged PC guard's `GETEVENTMSG`
`ProcessLookupError` before native launch. It remains archived too; the guard
was neither changed nor bypassed. A fresh guarded attempt passed both cases.
No expected native completion or cancellation outcome was weakened to pass.

Reproduce only these two optional native cases, then check offline evidence:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_tools/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_tools/check_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_tools/test_evidence.py
pre-commit run --all-files
```

Native absence prints a skip, which supplies no capability evidence. The
offline suite has three tests, including 26 capture mutation checks rejecting
missing started/result evidence, pause-before-start, false completion/end or
termination claims, later forwarding, missing rejection, missing native UI,
bad exit/termios and cleanup that invalidates the observation. Source and
capture hashes are checked separately. The fixtures add no ordinary CI
native-account or network dependency.

The source-to-test scope is confined to this research subtree: `probe.py`'s
cancellation result classification is tested directly, its two native
captures are checked by `check_evidence.py`, and `test_evidence.py` mutates
those captures to verify rejection. The shared project coverage matrix and
reviewed PA1C/PC fixture sources are unchanged under this node's ownership.
Real quota permission/freshness, alternate routes, microphone/realtime work,
general process supervision, identity races and macOS remain outside PA1T.
