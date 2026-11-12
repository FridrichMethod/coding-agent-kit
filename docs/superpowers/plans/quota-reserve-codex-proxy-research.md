# PC2: Codex per-invocation request-gate research

Status: **blocked-user**. A subscription-authenticated model-provider gateway
is a supported routing option, and the native client reaches the fabricated
endpoint. This does not yet satisfy full option A. A model endpoint cannot
recognize a new human continuation by itself; subscription TUI startup and
complete delegated-model routing also remain unproved. No runtime adapter,
credential migration, production proxy, or public UX change was implemented.

Base: `9c660f8af4ad98c79d3c01d4f74b14bb1d40b29e`. Research date: 2026-11-11
(local date; captured UTC timestamps cross into November 12). Tested Linux
x86_64, `codex-cli 0.167.2`, binary SHA-256
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b`.
Only newly generated, deliberately invalid credentials and temporary homes
were used. No actual subscription permission, production refresh, or real
account validation was tested. Earlier PC fixtures and evidence are unchanged.

## Routing architecture supported by the evidence

Use a foreground private App Server for the invocation and configure a custom
model provider whose `base_url` points at its private request gate and whose
`requires_openai_auth` is true. Leave file-token loading and refresh in native
Codex. A forwarding implementation would pass the native bearer and account
header to the subscription backend without implementing OAuth itself.

This is documented, rather than inferred from an API-key-only fixture:
[OpenAI gateway documentation](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway)
specifies forwarding `/models` and `/responses` to
`https://chatgpt.com/backend-api/codex`, preserving `Authorization` and
`ChatGPT-Account-ID`. The same page explicitly says that voice sessions use
another route; workspace sign-in, plugins, and MCP can use other paths.
Consequently, a provider gateway alone is not an established exhaustive
model-work boundary.

The fixture's relevant provider settings are:

```toml
model_provider = "gate"

[model_providers.gate]
name = "gate"
base_url = "http://127.0.0.1:PORT/backend-api/codex"
requires_openai_auth = true
wire_api = "responses"
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
```

`PORT` is a generated fixture port, not production configuration. The fixture
uses a separate `chatgpt_base_url` for fabricated account/usage responses;
that is not a recommendation to route production authentication through the
model gate. No API key is supplied. Logs retain only bearer-equality booleans,
account identifiers, header names, and bounded fabricated input summaries.
The service holds or rejects model requests locally; it is an admission and
transport-loss fixture, **not an implemented upstream forwarding proxy**.

## Native observations

Raw sanitized evidence and exact RPC responses are in
[pc_proxy/evidence](quota-reserve-probes/pc_proxy/evidence/).

| Probe | Observed behavior | Limit of the evidence |
| --- | --- | --- |
| Subscription provider | POST `/backend-api/codex/responses` includes the fabricated native bearer and account A | Proves native auth routing; not provider acceptance of real credentials |
| Model-free preflight | Native usage read reaches separate `/api/codex/usage`; five-hour and weekly windows returned before any model POST | No thread or model is needed for this read; existing PC freshness/identity limitations remain |
| WebSocket enabled | Two WebSocket upgrade attempts receive fixture 426, followed by HTTP POST to the same configured model endpoint | Establishes this fallback path, not successful WebSocket frame forwarding or cancellation |
| HTTP 401 | Native makes two model POST attempts and attempts CONNECT to `auth.openai.com:443`, which fixture rejects | Native retains a refresh-attempt path even with configured retry counts zero; successful refresh/storage is untested |
| Closed admission, controller socket disconnected, old queued prompt started | New request reaches fixture and is rejected; admitted count remains 1 | Closed latch blocks this request; controller disconnect did not itself close the latch |
| Loaded-thread resume with alternate provider | Native keeps the gate provider and rejects the subsequent request at the closed fixture | Does not establish all resume/config paths |
| New thread with alternate provider | Thread metadata says `bypass`, but `turn/start` fails `modelProviderMismatch` with current provider `gate`; no bypass model request | Positive evidence for this backend's current-provider check, not universal provider immutability |
| Gate listener/held connection removed, controller disconnected, queued work started | Queued turn remains in progress during the 15-second bound; no new accepted model request; native quota read still succeeds | Simulates transport disappearance, not an OS crash or controller/monitor watchdog implementation |
| Subscription native remote TUI | Exits during bootstrap with `workspace routing discovery failed`, zero model requests | The minimal account-discovery fixture returns 503; subscription UI usability is not established |

The earlier PC TUI success used a custom API-key provider. It must not be
presented as proof that this ChatGPT-authenticated gateway can retain a usable
TUI after pausing. Here, native `/api/codex/accounts/check` is required during
`account/read`; the fixture intentionally has no invented successful response
for that undocumented discovery contract. The new terminal evidence records
whether the PTY settings were restored on this bootstrap-error exit.

A preliminary assertion expected an alternate-provider dispatch. The native
wire error disproved that assumption, so the final experiment asserts the
actual refusal. Another preliminary UI assertion expected root/title dispatch
but instead observed the account-discovery error. Neither failed setup was
counted as pause or cancellation success. A listener-loss fixture also needed
to match the returned queued-turn ID because an immediate history read can
still show the previous interrupted turn. After fixing that race, the native
queued turn remained in progress for the 15-second observation. The final
suite records this bounded result, not a successful permanent pause or a
completed native failure. A recovering endpoint must stay latched: pending
retries cannot be treated as fresh human continuation.

## Coverage and remaining gaps

| Required full-A behavior | Current evidence |
| --- | --- |
| Root request routing | Demonstrated with native ChatGPT auth selection and fabricated credentials |
| Native title work | Prior API-key PC observed it; subscription TUI does not bootstrap in this fixture |
| Children and grandchildren | Unproved; provider inheritance, role config, account attribution and all dispatch paths need a conformance matrix |
| Goals | Active goal state coexists with closed-admission queue rejection; autonomous goal dispatch is not exercised |
| Old queue and programmatic same-thread continuation | Closed fixture rejects both; no automatic opening on quota recovery is implemented |
| Account/session/provider/profile changes | Loaded resume and new-thread alternate-provider cases tested; native account changes, profile reloads, role overrides and remote-session switches remain unproved |
| In-flight cancellation | Native root interruption disconnects its held HTTP response; listener-loss fixture explicitly closes its owned transport; server-side cancellation is not proven |
| Fail-closed monitor/controller loss | Not implemented: a lease/watchdog independent of monitor liveness would be required; deliberate latch closure before disconnect is not that proof |
| Fail-closed proxy loss | No new accepted request during bounded listener loss; queued turn remains in progress; eventual failure, alternate routes and surviving WebSocket streams unproved |
| Unrelated sessions | Only owned processes/temporary config are touched; simultaneous independent account/session behavior was not tested |
| Native UI and new human continuation | Subscription bootstrap blocked; no trusted human-origin event established |
| Running local tools and macOS | Not tested |

The preserved PC evidence already proves that a goal's caller-supplied
`origin=user` and a paused goal are not general dispatch authorization.
Likewise, a model request's text, thread ID, headers, or timing cannot attest
that a person deliberately resumed after a pause. Previously queued work can
produce ordinary model requests. A passing quota poll must not unlock the
latch. Merely holding a request until quota recovers would violate full A.

## Concrete options for the next decision

1. **Native dispatch lease and native resume event.** Prefer an upstream
   primitive that scopes a fail-closed lease to the entire invocation and
   exposes a distinct manual resume action. Keep the native UI and refresh
   ownership. This capability was not found in PC; no availability claim is
   made here.
2. **Small HTTP-only gateway plus an explicit continuation control.** This is
   a research candidate, not approved product UX. Codex documents
   `supports_websockets` in the
   [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
   Disabling WebSockets could reduce relay complexity, but compatibility and
   performance implications need review. A distinct out-of-band human control
   would need a defined trust boundary, a NEW-action epoch after pause, a fresh
   quota read, and healthy control before granting another dispatch lease.
   An ordinary agent-callable command or forged origin field is insufficient.
   The design must also close all active upstream connections, keep the latch
   closed on controller/monitor loss, and prevent alternate model routes.
3. **Transparent multi-transport interception.** Would need audited HTTP/SSE
   and WebSocket forwarding, stream lifetime accounting, credential redaction,
   alternate endpoint control, account/family binding, and a separate trusted
   manual control. This is substantially more code and maintenance than the
   bounded gateway experiment; do not implement it by default.

Before further implementation, the user must decide whether to review a new
explicit continuation control and HTTP-only protected-mode restrictions, or
retain the original UX and wait for a suitable native primitive. The current
scope specifically requires such a decision before adding a resume command
or PTY mediation. Approval of further proxy research alone does not decide
those UX changes or establish safety.

Independent of that choice, subscription account discovery/TUI operation,
complete delegated/voice/model routing, cancellation, native account/family
changes, ownership isolation, and monitor-loss behavior still need evidence.
Real-account testing remains a separate authorization gate naming the account,
commands, and data. A fake discovery success cannot replace it. Full option A
and G0 remain blocked; no production runtime work should start.

## Reproduction and validation

Run from the worktree root with the optional native binary installed:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_proxy/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_proxy/check_evidence.py
pre-commit run --all-files
```

The runner reuses PC's reviewed Linux x86_64 loopback/Unix-only seccomp/ptrace
containment and self-check without modifying them. Each stage directly owns a
tracer with an outer timeout; EXITKILL bounds tracees if the tracer dies.
This remains trusted-binary research containment with PC's documented pointer
inspection/TOCTOU limitations, not a production security sandbox. External
CONNECT attempts are rejected locally. Failed stages exit nonzero and are
never silently reclassified as behavioral success.

The native runner writes a sanitized manifest and mode evidence, and retains
exact temporary-root inventory and exit status in `.cache/pc_proxy/` for the
coordinator. Useful evidence is committed; temporary roots are not its sole
copy. Native absence produces a clear skip, not a capability pass. The report
and new fixtures are the entire write scope; runtime modules, prior evidence,
central handoff/spec/ledger and product interfaces are unchanged.
