# PC6: bounded native realtime refusal routing

Status: research evidence only. The owned App Server's two realtime endpoint
overrides sent actual native WebRTC and WebSocket requests to the same rejecting
gate. Each received HTTP 503; the distinct alternate listener received nothing.
This establishes a narrow core-routing refusal candidate, not full option A,
working audio, TUI behavior, or a production architecture. G0 remains open.

## Scope and result

The native binary was `codex-cli 0.167.2`, SHA-256
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b`.
The matching release source inspected for this experiment was commit
`ce21072c90d32c40af7a55b60b2fb35c84a43a6a` (`rust-v0.167.2`).
[source-lock.json](quota-reserve-probes/pc_realtime/source-lock.json) binds the
ten inspected files by SHA-256. The runner rejects a different binary or source
file set before native execution; this is a research reproducibility pin, not
a proposed launcher version pin or a claim about future binaries.

| Native start | Authentication fixture | Actual gate request | Result |
| --- | --- | --- | --- |
| Core RPC, WebRTC v3 | Fabricated ChatGPT subscription credentials | `POST /live` | One HTTP 503; native realtime error contains that status |
| Core RPC, WebSocket v3 | Fabricated ChatGPT credentials plus fabricated provider API key | `GET /v1/live?model=gpt-live-1-codex`, WebSocket Upgrade | One HTTP 503; native reports handshake refusal |

Both cases held the ordinary Responses gate lease open for 20 seconds, so the
refusal comes from its unsupported-route check, rather than an expired lease.
Both native backends remained alive for three seconds after the error; there
were no additional realtime requests, alternate-listener hits, successful
upgrades, forwarded Responses requests, or generation responses in that bound.
The backend process was then terminated and reaped. This is a bounded
observation of retries/fallback, not a proof that no future timer can dispatch.

The configured provider retained HTTP/SSE Responses routing with
`supports_websockets = false`. The private configuration deliberately assigned
both realtime URLs to a second, distinct loopback listener. The owned
App Server startup used these CLI overrides, with `GATE` being the invocation's
loopback gate origin:

```text
-c experimental_realtime_ws_base_url="GATE"
-c experimental_realtime_webrtc_call_base_url="GATE"
```

The gate origin here has no `/backend-api` suffix. Native v3 therefore builds
the exact paths in the table. This tests rejection before any successful call
response. It does not prove backend-shaped request compatibility or successful
media negotiation. The WebRTC SDP was synthetic and never accepted by a peer;
the observed HTTP request, not SDP validity, is the routing evidence.

The initial subscription-only direct WebSocket attempt failed locally with
`realtime conversation requires API key auth`, with no realtime HTTP request.
The checker rejected that run. Its retained private evidence must not be
counted as routing success. The key-configured WebSocket case is a distinct,
explicitly narrower authentication condition than the subscription WebRTC case.

## Source linkage and limits

All links below identify the exact inspected commit:

- [prepare_realtime_start, lines 1369-1490](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/core/src/realtime_conversation.rs#L1369)
  applies the WebSocket override and creates the WebRTC provider override.
  The direct WebSocket authentication requirement is in `realtime_api_key`,
  lines 1973-1997 of that same file. `handle_start`, lines 1307-1340, reports
  preparation/start errors; it does not test the removed realtime feature flag.
- [create_realtime_call_with_headers, lines 812-844](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/core/src/client.rs#L812)
  installs the WebRTC provider override before native call creation.
- [TUI thread_realtime_start, lines 20-60](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/tui/src/app_server_session/realtime.rs#L20)
  sends `thread/realtime/start`, WebRTC, v3, audio output, client-managed handoffs,
  and no startup context. The probe uses those same relevant core RPC fields.
  [Protocol types, lines 197-299](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/app-server-protocol/src/protocol/v2/realtime.rs#L197)
  define the start request and transport variants.
- [Native call path selection, lines 62-79](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/codex-api/src/endpoint/realtime_call.rs#L62)
  uses `live` for v3 without a backend-shaped base URL.
  [WebSocket normalization, lines 1308-1319](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/codex-api/src/endpoint/realtime_websocket/methods.rs#L1308)
  converts a bare origin to `/v1/live` for v3.
- [TUI availability, lines 109-112](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/tui/src/chatwidget/session_flow.rs#L109)
  depends on the native WebRTC package. [TUI startup](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/tui/src/chatwidget/realtime.rs#L313)
  owns local WebRTC initialization. Package availability and device readiness
  are separate in [RealtimeWebrtcSession](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/realtime-webrtc/src/session.rs#L83)
  and [VoiceHost](https://github.com/openai/codex/blob/ce21072c90d32c40af7a55b60b2fb35c84a43a6a/codex-rs/realtime-webrtc/src/client.rs#L58).

TUI `/voice` was not run. The installed package includes a voice helper/runtime,
and audio device nodes exist; neither fact proves usable audio or safe synthetic
capture. This experiment deliberately did not open a microphone, record audio,
or connect to the user's PulseAudio/PipeWire session. Therefore TUI startup,
audio errors, retries, recovery, and terminal behavior remain unverified here.
No hardware absence is claimed as a pass or a skip result.

The WebRTC create call was refused before any sideband WebSocket could be
created. The separate direct-WebSocket case proves its configured route, not
the subscription sideband sequence after a successful call. Existing-call
attachment, arbitrary RPC clients, all native modes, account/session switches,
resumption, and all invocation-owned work are not newly covered by PC6.
Earlier native UI, descendants, quota/control and monitor-loss evidence remains
separate. Disabling `features.realtime_conversation` is not the candidate.

## Fixture and containment

Only the new `pc_realtime` research subtree and this report changed. The probe
loads the unchanged PC `Client` and JWT helper, reuses the unchanged PC3
workspace-discovery response, and instantiates the unchanged PC5 `FixtureGate`.
A derived handler records actual status/method/path/Upgrade fields and uses
HTTP/1.1 so the native WebSocket client can parse the refusal. No routing or
grant behavior in the shared fixtures changed. HTTP/1.0 had caused an earlier
native protocol error before status interpretation; that failed run is retained.

The unchanged independent Linux x86_64 syscall guard runs before every native
exec and is self-checked first. It permits loopback TCP and Unix sockets, denies
external TCP and unsupported network operations, and follows exec descendants.
It is the same trusted-native research containment used by earlier nodes, not
a new security boundary against malicious same-UID clients. Proxy environment
variables are additional local defaults, not the containment mechanism.

Every case uses a private generated HOME, CODEX_HOME, workspace and fake auth.
The runner has 10-second guard and 40-second per-case bounds; the RPC wrapper
has its existing five-second request deadline. Native realtime failure is
awaited for at most ten seconds and followed by three seconds of observation.
Cleanup sends SIGTERM, waits three seconds, then uses SIGKILL/reap if necessary.
The outer guard's EXITKILL covers tracees if its timeout is reached. No real
credentials, login, remote model, audio stream, or user daemon is used.

## Reproduction and validation

From this worktree, set `NATIVE_BINARY` to the tested executable and
`CODEX_SOURCE` to the already fetched pinned source checkout. No download or
live account is needed:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_realtime/run.py \
  "$NATIVE_BINARY" "$CODEX_SOURCE"
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_realtime/check_evidence.py \
  "$CODEX_SOURCE"
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_realtime/test_evidence.py
pre-commit run --all-files
```

The successful native run returned zero for guard, WebSocket and WebRTC.
Offline evidence/source checks passed. Thirteen focused tests check both saved
native cases and mutations for absent requests, alternate hits, successful
upgrades/calls, wrong path/transport, premature or missing refusal, uncollected
processes, endpoint overrides, lease/observation bounds and source hash drift.
They also verify worktree/ancestor instruction-source redaction, reject private
paths, and scan every public evidence JSON artifact for those leaks.
The initial permissive validator failed the expected negative cases; after
validation was implemented those mutations were rejected. Required all-files
hooks are recorded in the local completion record. The previously completed
native matrices were not repeated.

Sanitized evidence is under
[pc_realtime/evidence](quota-reserve-probes/pc_realtime/evidence).
The manifest binds native version/binary, source files, probe/shared-fixture
hashes, captured run exits, and case evidence. Private raw logs, generated
homes, failed attempts and root inventory remain in `.cache/pc_realtime/`
until coordinator archival and verified cleanup. There are no PC6 `/tmp`
scratch roots, and no shared source checkout was modified or removed.

Review fix R1 regenerated only the public JSON from the preserved successful
raw capture; it did not rerun native Codex or change the measured events.
The original capture source tree is
`f2c66bddeff5f410086897420df17b6e5e69a64b`, whose `probe.py` SHA-256 was
`8a8277b94bf19efefcd4ed347e2d2112d68119929240f6ec7b785898135aa21d`.
The manifest retains the original `capture_fixture_sha256` separately from
the current `fixture_sha256` and records the original manifest/raw hashes and
final transformation hashes under `resanitization`. Original capture sources
were hash-verified and preserved under `.cache/pc_realtime/capture-source/`.
Thus current probe-source hashes identify the repaired sanitizer; they do not
claim it generated the earlier native capture. The offline repair command was:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/pc_realtime/resanitize.py \
  .cache/pc_realtime/1794484017114579712 \
  f2c66bddeff5f410086897420df17b6e5e69a64b
```

The repair checks that the supplied source tree matches the capture's recorded
source hashes, redacts known roots before worktree ancestors, anonymizes other
absolute instruction-source paths, and validates both regenerated cases before
writing the publication artifacts. The original raw archive is unchanged.
