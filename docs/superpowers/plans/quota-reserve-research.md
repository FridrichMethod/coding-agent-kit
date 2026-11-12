# Quota reserve feasibility evidence

Temporary implementation research, recorded November 11, 2026 against
`ed7e401be12d0972762915f17d86acc0f3ff2629`. Preserve these conclusions in the
handoff until the implementation gates close. None of the probes used real
credentials or a real model service. They do not prove production quota
permissions, complete pause behavior, or macOS compatibility.

## Codex: observed capabilities and counterexamples

Tested Linux `codex-cli 0.167.2`; binary SHA-256:
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b`.

| Probe | Observed result | Consequence |
| --- | --- | --- |
| Active native TUI, OS SIGINT to native PID | Return code -2 | Not a UI-preserving interrupt |
| Active native TUI, Ctrl-C byte through its PTY | Interruption banner, process alive | Current-turn interruption only |
| Foreground App Server on a private Unix socket, native TUI using `--remote` | TUI attaches and runs a fabricated turn | Shared-backend architecture is a real candidate |
| Second controller on that backend, `turn/interrupt` | Empty success result, interruption banner, TUI alive | Controller can interrupt the correct active turn |
| Disconnect controller, then enter another prompt | A new loopback Responses request occurs | Controller loss does not close model dispatch; no persistent pause |

The checked local protocol schema had 180 methods, including interrupt,
queue, goal and descendant operations. No general persistent dispatch gate
was established. A TUI RPC proxy would not automatically intercept backend
subagent model requests or internal continuation. Goal state is not a
session-wide model-dispatch guard.

The [App Server API](https://learn.chatgpt.com/docs/app-server) documents
`account/rateLimits/read`, independent of thread/turn creation. Its successful
use under actual account credentials has not been tested here. The
[hook documentation](https://learn.chatgpt.com/docs/hooks) makes model-request
hooks diagnostic-only; `SubagentStart` cannot stop creation. No trusted
human-versus-queued origin was established for `UserPromptSubmit`.

Portable exploration scripts are preserved verbatim:

- [Signal and keyboard probe](quota-reserve-probes/codex_signal.py), SHA-256
  `758d39dd619ecb74bb879607f5dc87696350dfe4da2fc1f02d91ecadd26543e2`.
- [Same-backend probe](quota-reserve-probes/codex_same_backend.py), SHA-256
  `a70b2235408d06fb089b408118f9142ad9351b44f21f26e9cf21db7fab3cf766`.

These are disposable research scripts, not runtime modules or CI tests.
From the repository root, with the tested CLI available:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/codex_signal.py sigint "$(command -v codex)"
python3 -I -B docs/superpowers/plans/quota-reserve-probes/codex_signal.py keyboard "$(command -v codex)"
python3 -I -B docs/superpowers/plans/quota-reserve-probes/codex_same_backend.py samebackend "$(command -v codex)" "$PWD"
```

Each creates and prints its own temporary root, uses a fabricated API key,
routes the Responses stream to loopback, and points proxy variables at a
loopback reject proxy. The observed startup attempt to contact GitHub was
rejected locally. A reject proxy is not an OS network firewall; before using
an unfamiliar native version, verify containment or provide an external
network-deny mechanism. The scripts stop only their own process groups and
leave their temporary output for inspection. Capture the useful evidence in
durable progress records, then remove only those exact verified temporary
roots through canonical cleanup commands. The native sandbox warning on this
host means these probes do not establish running shell-tool cancellation.

## Claude: startup query and authentication

Tested Linux `2.1.322 (Claude Code)`; binary SHA-256:
`bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478`.

A temporary HOME/config/cache/state, empty workspace, fabricated OAuth token,
and `strace` injection blocked every connect/send network syscall. Native
stream-json control accepted initialize, usage, interrupt, and background-task
control in an idle session without a user prompt. The result had zero model cost, zero API duration,
empty model usage, and no assistant or stream events. Its quota fields were
`rate_limits_available: false` and `rate_limits: null`. All 62 observed network
syscall attempts were blocked. Initialization took about 18 seconds in this
fault-injected environment; that is not a production timeout recommendation.
The `background_tasks` request backgrounds foreground work; it is not an
enumeration API. Its empty success response in this idle probe does not prove
that descendants can be listed or that any active work was stopped.

The reproducible control request sequence is:

```json
{"type":"control_request","request_id":"init","request":{"subtype":"initialize","hooks":{}}}
{"type":"control_request","request_id":"quota","request":{"subtype":"get_usage","skip_behaviors":true}}
{"type":"control_request","request_id":"interrupt","request":{"subtype":"interrupt"}}
{"type":"control_request","request_id":"tasks","request":{"subtype":"background_tasks"}}
```

Send each request only after its predecessor's result, with bounded waits.
Use the following command only inside the isolated, credential-free harness;
do not run it against the user's normal HOME or config:

```sh
strace -f -e trace=connect,sendto,sendmsg \
  -e inject=connect:error=ENETUNREACH \
  -e inject=sendto:error=ENETUNREACH \
  -e inject=sendmsg:error=ENETUNREACH \
  -o TRACE_PATH \
  CLAUDE_BINARY -p --input-format stream-json --output-format stream-json \
  --verbose --safe-mode --no-session-persistence
```

`TRACE_PATH` and `CLAUDE_BINARY` above are harness parameters, not literal
production commands. No-session-persistence still allowed native config and
backup files inside the temporary config directory. The inline probe was
not retained; its protocol, isolation requirements, and results are recorded
here so a future conformance fixture can reproduce it.

Static inspection of the same binary established that the environment-token
credential constructor defaults to `user:inference`, setup-token requests
inference-only scopes, and the usage handler requires a `user:profile` check
before fetching quota. This supports the
[official long-lived token limitation](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token).
Do not manufacture scope metadata to pretend a token has additional grants.
The user permits researching native full login while retaining setup-token;
storage, account isolation and migration still need a manual design gate.

Another required probe is freshness: static inspection found cached/seeded
quota paths, including a 60-second cache in this version. The inspected
response builder does not forward native fetch-time or cache-origin metadata.
The offline probe returned null quota and did not exercise populated cached
quota. RPC success alone therefore cannot reset a freshness watchdog.
Prove new observation time or a conservative bound with a documented query
mechanism; otherwise mark freshness unavailable.

## Control options and unresolved proof

Claude [hooks](https://code.claude.com/docs/en/hooks) can stop a loop at specific
boundaries, but have not established interruption of all active and future
subagents, fail-closed monitor loss, or reliable manual-resume provenance.
Headless control owns its own session, not another native TUI. Investigating
background-session attachment is an option, not an established architecture.
The [SDK changelog](https://github.com/anthropics/claude-agent-sdk-typescript/blob/main/CHANGELOG.md)
labels its programmatic usage interface experimental.

For both providers, full option A remains unproved. Preserve a hard gate:
do not replace it with process exit, repeated interruption, warning-only
behavior, an unreviewed request proxy, or a replacement UI. A new authenticated
probe, changed credential model, weakened stop semantics, or substantial
native protocol layer needs the review described in the handoff.
