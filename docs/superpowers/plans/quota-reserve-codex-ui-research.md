# PC3: sourced subscription discovery and native routing observations

Status: **bounded evidence complete; full A and G0 remain blocked**. The
subscription-authenticated native remote TUI now starts using a fixture
derived from the matching official source. Root, native title, child and
grandchild model requests reach the configured provider. Interrupting only
the root leaves the title or descendant streams open in these experiments.
No production proxy, continuation control, runtime adapter, credential
migration, or HTTP-only product restriction was implemented.

Base: `796847a6e9cb3cbef8b02eabf092740f9fc61279`. Native executable:
`codex-cli 0.167.2`, Linux x86_64; SHA-256
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b`.
The run captured November 12, 2026 UTC. Credentials, account identifiers,
prompts and responses are fabricated. Native children use fresh temporary
homes and the unchanged PC loopback/Unix-only network guard. Real account
permission, upstream generation and actual cancellation are not tested.

## Discovery contract has a source

The official `rust-v0.167.2` tag resolves through tag object
`08a3b12b3298577534a28b62ed976acf8de66eda` to source commit
`ce21072c90d32c40af7a55b60b2fb35c84a43a6a`. Source URLs and downloaded
SHA-256 values are recorded in
[sources.json](quota-reserve-probes/pc_ui/evidence/sources.json).

- [Backend client](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/backend-client/src/client.rs):
  `get_accounts_check` requests `/api/codex/accounts/check` for the Codex API
  path style and deserializes `AccountsCheckResponse`.
- [Response types](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/backend-client/src/types.rs):
  the response accepts a list of account entries. List entries retain
  `workspace_backend_origin` and `account_routing_override`; the alternative
  map conversion supplies neither routing field.
- [Workspace routing](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/app-server/src/request_processors/account_processor/workspace_routing.rs)
  and its
  [tests](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/app-server/src/request_processors/account_processor/workspace_routing_tests.rs):
  discovery selects exactly one matching account ID. A discovered backend
  must be an HTTPS origin without credentials, query, fragment or non-root
  path. Supported routing overrides include `NO_CONSTRAINT`, `us` and
  `us_cr`. The special backend value `NO_CONSTRAINT` resolves against the
  effective or required HTTPS backend, so it does not make the fixture's
  HTTP quota origin valid.

The fixture therefore returns this sourced shape, with invented values:

```json
{
  "accounts": [{
    "id": "fixture-account-a",
    "workspace_backend_origin": "https://fixture.invalid",
    "account_routing_override": "NO_CONSTRAINT",
    "name": "Fixture"
  }]
}
```

`fixture.invalid` is not contacted. The custom authenticated model provider
still points to loopback; other outbound attempts are independently denied.
This proves that native discovery can parse this contract and boot the TUI.
It does not prove that a real subscription returns these values, permits
gateway access, or would route identically under workspace policy.

## Native matrix and interruption boundaries

The same reviewed PC2 provider settings select native ChatGPT authentication
with `requires_openai_auth = true`. No API key is present. HTTP/SSE is the
existing observation transport. Successful WebSocket handling remains outside
this experiment; no protected-mode transport policy is adopted.

| Case | Native observation | Boundary |
| --- | --- | --- |
| Model-free preflight | Native rate-limit read returns both windows before any model POST | Fabricated quota only |
| Subscription remote TUI | Starts successfully and sends root request to gate with fabricated bearer/account A | Replaces PC2's discovery-503 bootstrap blocker for this fixture only |
| Native title | A distinct request with the native title-generation prompt reaches the same gate | Not a synthetic request counter or manually requested title |
| Root interruption while TUI attached | Root held stream disconnects; thread/read reports interrupted; TUI remains alive and history is RPC-readable | Does not establish all keyboard/history browsing interactions |
| Title after root interruption | Title held stream remains in the fixture's active set one second later | Root turn interruption is not whole-invocation cancellation |
| TUI exit | Native Ctrl-C exit returns 0 and restores original termios | This is the existing exit shortcut, not a new continuation control |
| Default child and grandchild | Native executes two namespaced spawn calls; five total model POSTs include root, child and grandchild | Fixed provider/model, multi-agent v1, max_depth 2, max_threads 4 only |
| Root interruption with descendants | Root held stream disconnects; child and grandchild streams remain active after two seconds | Not a family-wide stop |
| Individual descendant interruption | Interrupting each actual descendant turn disconnects the remaining held streams | Local transport observation; no proof of upstream computation cancellation |

The delegation fixture emits fabricated `response.output_item.done`
function-call events followed by `response.completed`, using the envelope in
the official
[SSE test helper](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/core/tests/common/responses.rs).
The offered native `multi_agent_v1.spawn_agent` schema determines the actual
arguments (`message` and `fork_context = false`). The harness does not issue
child `thread/start` calls. Native thread/read results contain completed
`collabAgentToolCall` items with distinct sender/receiver IDs forming the
root-child-grandchild graph. Requests carry the corresponding unique fixture
prompts. This establishes actual native delegated dispatch under fabricated
model responses, not autonomous model decision-making or synthetic counters.

All five delegation requests use the root `session-id` header, although the
three native thread IDs differ. An initial harness assertion incorrectly
expected distinct per-thread headers; the captured requests disproved it.
The failed run is retained in the ignored local audit cache. Final checks use
native spawn history for genealogy and explicitly verify the shared root
header. Do not generalize this observation to all native dispatch families.

## Remaining routing and full-A gaps

The gateway documentation explicitly identifies voice as using a separate
route and says other services, including sign-in, plugins and MCP, may bypass
the model gateway. This is a documented routing exception, not a voice
experiment performed here. See
[Connect to a gateway](https://learn.chatgpt.com/docs/enterprise/connect-to-a-gateway).

The native v1 spawn schema offered in this run has no `agent_type` field.
Role/provider overrides were not exercised. Profile reload, native account
switching, remote-session switching, alternative model families, goal-driven
dispatch, compaction, image generation, and v2 delegation remain proof gaps.
PC2's loaded/new-thread alternate-provider observations remain narrower
evidence and are unchanged. The fixed-provider results here cannot establish
an exhaustive model-work boundary.

Root interruption and a closed admission latch are insufficient to stop
already admitted title or descendant work. A future approved design would
need to account for every active stream as well as prevent new admission.
This experiment implements neither upstream forwarding/cancellation nor the
independent fail-closed lease/watchdog needed for monitor/controller loss.
Fresh human continuation, native account/family changes, refresh/storage,
macOS and real-account acceptance remain unresolved. User decisions about a
dedicated continuation action and HTTP-only restrictions are still pending;
none is inferred from permission to run this research. G0 stays closed.

## Reproduction and validation

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_ui/run.py
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_ui/check_evidence.py
pre-commit run --all-files
```

The native runner bounds the guard self-check to 10 seconds and each of two
native cases to 40 seconds. A timeout or assertion failure is not a passing
capability result. The guard, its self-check, and the existing RPC helper are
reused unchanged. The guard retains PC's trusted-binary and sockaddr TOCTOU
limitations; it is not a production sandbox. The fixture proxy rejects all
CONNECT requests and has no forwarding implementation.

Committed evidence contains sanitized events, terminal output, native RPC
results, binary hash, source provenance and stage exit codes. Temporary-root
inventory, failed exploratory logs and downloaded source copies remain in
ignored `.cache/pc_ui/` for coordinator archival before worktree removal.
Native absence is a skip, never a capability pass. Only the new report and
`pc_ui/` subtree changed; PC, PC2 and product files remain unchanged.
