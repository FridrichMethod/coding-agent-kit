# PA1C: Claude native TUI, nested model control and continuation

Status: **bounded native control evidence complete; full A and G0 remain
blocked**. An actual interactive Claude TUI, including native child and
grandchild agents, sends Messages/SSE through one invocation-owned loopback
gate. Monitor EOF, monitor death, lease expiry and gate death close its held
streams while a separate native TUI continues streaming. New terminal
confirmation can reopen the shared research gate after a new fabricated quota
check. This is research only, with no launcher or production implementation.

Base: `bb195717d901246eb51ce1b4fec02be7baec529a`. Captured November 12, 2026
UTC on Linux x86_64 with Claude Code `2.1.322`, binary SHA-256
`bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478`.
All credentials, accounts, model responses and quota observations are
fabricated. No real provider endpoint, login, credential or default home was
used. The previously authorized real count request is not repeated here.

## Native mode and authentication selection

Every case launches the interactive CLI on a controlling PTY, without `-p`,
an SDK session or a second headless controller. Prompts and shortcuts enter
that TUI's input. Persisted transcripts live in a private temporary config.
The fake upstream supplies valid Messages SSE and native Agent tool calls;
the CLI itself creates the children and produces their tool results.

The official [gateway connection guide](https://code.claude.com/docs/en/llm-gateway-connect)
documents `ANTHROPIC_BASE_URL` and gateway bearer configuration. The fixture
separately tests which native authentication source reaches that URL:

| Case | Only selected credential source | Native auth status / TUI | Observed request |
| --- | --- | --- | --- |
| Gateway baseline | Fabricated `ANTHROPIC_AUTH_TOKEN` | API usage billing | Fixture bearer at local Messages endpoint |
| Setup token | Fabricated `CLAUDE_CODE_OAUTH_TOKEN` | `oauth_token` / Claude API | Fixture bearer at local Messages endpoint |
| Full stored OAuth | Private `.credentials.json` and account metadata, no auth environment token | `claude.ai`, `firstParty`, `storedClaudeAiLogin=true`, Max / Claude Max | Fixture store bearer at local Messages endpoint |

Thus changing the base URL alone does not switch this native version away
from its stored OAuth source. All four loss cases and continuation use the
full stored OAuth setup, with a future fabricated expiry and profile/inference
scope strings. Native credential files remain byte-for-byte unchanged.
The setup-token case does not add profile scopes or establish startup quota.
Native status and successful fake responses prove local selection, not real
server permission, subscription entitlement, refresh, revocation handling,
device signing or production compatibility. No first-party TLS interception
is used for these Messages experiments.

## Owned work, interruption and loss

The fake root response calls native `Agent` for a general-purpose child;
the child's response calls it for a grandchild. Requests contain actual
native tool results naming two distinct agent IDs, and all three native
contexts issue subsequent held model requests. This version backgrounds
the agents without a `run_in_background` argument. The current
[subagent documentation](https://code.claude.com/docs/en/sub-agents)
describes nested agents and their separate model requests. Evidence records
the observed Agent schema fields rather than assuming an older Task schema.

In the direct delegation case, one native Ctrl-C disconnects the root stream
while child and grandchild streams remain active. The TUI stays alive. This
is a concrete counterexample to using only current-turn interruption for
invocation-wide protection. Later cleanup stops the background agents and
exits normally; it is not the protected pause operation.

The loss fixture adapts PC4's fixed-route relay to Claude Messages, reusing
the unchanged PC4 monitor. A single lock covers lease checks, forwarding and
registration of client/upstream sockets. It admits only the two tested
Messages path spellings and forwards a minimal fixed authentication/header
set. Initial admission is closed; monitor heartbeats give a 0.8-second lease.
EOF or expiry latches closed and shuts all registered transports. A later
heartbeat cannot reopen it. This small research relay is not a complete
Anthropic gateway and supplies no refresh service or public control API.

All four loss cases begin with root, child and grandchild streams active.
A second native TUI has its own temporary HOME/config/workspace, uses a
separate direct fake endpoint route, and keeps streaming through the loss.

| Trigger | Gate latch or confirmed exit | Last owned upstream disconnect |
| --- | --- | --- |
| Monitor transport EOF, process still alive | 0.2 ms | 162.8 ms |
| Owned monitor SIGKILL | 1.3 ms | 190.9 ms |
| Heartbeats stop, monitor and socket stay alive | 743.2 ms | 933.3 ms |
| Owned gate SIGKILL | 1.9 ms | 178.6 ms |

These are observations from one run, measured from the trigger with the same
host monotonic clock. The lease measurement starts when heartbeats are told
to stop, not at the last heartbeat. Upstream closure detection includes the
fixture's 100 ms ping writes and scheduler delay. Disconnecting local
transports does not prove cancellation of computation or billing upstream.

Before each loss the harness queues a native prompt. Following interruption,
Claude automatically dispatches it; the live closed gate rejects it. Stopping
background agents also creates a native notification turn and more model
attempts. An injected later prompt uses native send-now to dispatch during
those retries and is rejected too. No owned request reaches the upstream
after any loss trigger. With the gate dead, upstream counts remain fixed;
there is no live rejection log or proof of eventual native retry exhaustion.
The gate prevents local admission while the UI may still display retries.

Every case retains native terminal output. Ctrl-O opens the native detailed
transcript after interruption/loss, the TUI stays alive, and its later native
exit returns zero and restores termios. These are bounded UI checks, not
complete keyboard, scrollback, accessibility or terminal-emulator coverage.
The [interactive reference](https://code.claude.com/docs/en/interactive-mode)
documents interruption, queued messages, send-now and background-agent stop
shortcuts used here. Cleanup shortcuts must not be confused with pause UX.

## Shared continuation experiment

The continuation case imports PC5's `pc_continue/control.py` and `gate.py`
unchanged, through a read-only shared directory. Their SHA-256 values are
pinned before import and recorded in the capture. The dependency is PC5
commit `517a2a75336ee98f72f820de2a6c43fc01d13c30`; integrate that reviewed
commit first, then reproduce this node with the same bytes:

```
control.py 8b98eee39bd83b7769356db3fb49bcfefe8423da28cb4942430a9bad5e218dad
gate.py    045fafde6a9472e044fb747a8a81fabc40a31df79710216ee6d47c6912a6e8cb
```

The native TUI runs on the inner PTY. Its output and the controller's notices
reach the outer terminal; only reads from the outer terminal's input feed
the shared parser. The harness simulates terminal input. F12 is a research
key, not a proposed public shortcut, and requests a new one-use typed
challenge. Pause drains old input. No separate Claude parser or command
endpoint is introduced.

The initial lease follows a fabricated passing preflight. After pause, a
prebuffered F12, bracketed-pasted challenge, old native queue, a correctly
typed challenge with failing fake quota, and passive quota recovery grant
nothing. A subsequent newly typed challenge performs another loopback quota
GET, checks the account marker, sequence, receive interval and both windows,
and grants the second lease. Native child/grandchild retries and the explicit
later native prompt then reach the upstream. A second pause closes them.

This establishes a source-separated terminal path under a trusted terminal
assumption. It does not attest physical human action: the test driver itself
writes the terminal, and malicious same-UID input injection remains outside
the proof. The quota server is wholly fabricated, not native `get_usage` or
an Anthropic endpoint. It cannot establish real quota freshness, account
attribution or supported polling cadence. The shared continuation gate and
the independent process-loss gate are separate bounded experiments; their
composition into a crash-safe production supervisor has not been proved.

## Reproduction and remaining boundaries

After PC5 is integrated beside this subtree:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_control/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_control/check_evidence.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_control/test_evidence.py
pre-commit run --all-files
```

Before integration, `--shared-control-dir PATH` selects the frozen PC5
research subtree. It must match both pinned digests. Native absence prints a
skip, not success. The runner applies PC's unchanged loopback/Unix-only Linux
guard and self-check; every native case has a 55-second outer bound. The
guard retains EXITKILL and its documented trusted-binary/sockaddr TOCTOU
limits. Children receive explicit private environments and closed inherited
descriptors. The known hosted credential paths are checked for absence before
native execution. The fixture kills and waits only its owned process groups.

The manifest binds fixture/helper hashes and all nine sanitized captures,
and records each stage exit. Its evidence hash map was reformatted as
file/digest records to avoid treating OAuth-named file keys as credentials;
the original capture runner digest is retained separately. Native probe and
capture bytes were unchanged by that metadata-only correction.
Ten offline tests accept the captures and reject
missing TUI, genealogy, closure, unrelated-session survival, queue rejection,
closed-gate ordering, fake-quota failure, visible notices and truthful human
provenance. The initial absent-evidence check failed before capture existed.
Failed queue/cleanup assumptions and a corrected archival failure on native
Unix socket files are retained in ignored `.cache/claude_control/`. Each run
archives regular files, exact root lists and an inventory including special
files; native socket contents are not copied. These artifacts must be
preserved by the coordinator before removing the worktree.

Alternate provider/profile/model configuration, arbitrary native tool
subprocesses, local tool cancellation, OAuth refresh, real server responses,
arbitrary alternate routes, remote sessions, daemon restart/rebind and macOS
are not covered. The independent network guard is essential experiment
containment; it does not prove the Messages gate alone covers those routes.
Real model-free fresh quota and complete protected-route ownership remain
prerequisites. No production migration, token refresh change or TLS proxy is
authorized by these results, and full A/G0 must stay closed.
