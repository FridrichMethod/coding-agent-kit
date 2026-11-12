# PTP: one-shot count-header permission probe

This is a review artifact, not permission to execute with a real account. At
preparation time no real token was read and no provider was contacted. One
later explicitly authorized count request returned HTTP 200 without quota
headers; see the [result](quota-reserve-claude-count-result.md). That one-shot
permission is consumed and does not authorize another request. Setup-token-only support
is preferred; persistent full login is permitted as a reviewed fallback if
needed, but generation never substitutes for startup quota. The experiment
implements the exact endpoint hypothesis in the reviewed
[PT report](quota-reserve-claude-token-research.md).

## Separate approval required

The coordinator must first obtain independent security review, resolve the
selected account's actual configured token path without enumerating accounts,
and obtain explicit user permission for that named account and exact file.
The user must also confirm direct first-party HTTPS is allowed by their network
policy. The execution flag is not that authorization. If organizational routing
or custom trust is required, stop for a separately reviewed transport.

Future command shape only, with placeholders replaced after those steps:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_token/permission_probe.py \
  --count-once --account APPROVED_NAME --token-file EXACT_APPROVED_ABSOLUTE_PATH
```

No arguments, `--help`, and `--describe` read no credential and open no network
connection. There is no URL, model, retry, native-helper or fallback option.
The exact path must end in `claude-APPROVED_NAME`; the harness does not read
configuration and cannot independently establish which directory is configured.
That association remains part of the coordinator's approval record.

## Operation and data

The child reads only the explicit token file once, after fd-relative traversal
rejects symlinks in every component. It requires an absolute normalized path,
owned regular file, exact mode 0600, one hard link, bounded size and launcher
setup-token syntax. It rejects mutation detected across the read. It never
reads a default account, login store or profile, and never hashes or copies the
token to disk. The token exists only in child memory and the TLS Authorization
header; it is absent from argv, retained output and exception messages. Core
dumps are disabled in the child. This is not protection against an adversarial
same-UID process, privileged debugger, memory swap or a compromised interpreter.

One `POST` goes to fixed host `api.anthropic.com`, fixed path
`/v1/messages/count_tokens`, with the exact PT body:

```json
{"model":"claude-sonnet-5-5","messages":[{"role":"user","content":"quota metadata probe"}]}
```

Headers are `Authorization: Bearer <selected setup token>`,
`Content-Type: application/json`, `anthropic-version: 2023-06-01`,
`anthropic-beta: oauth-2025-04-20`, `Cache-Control: no-cache`, and
`Connection: close`. The HTTP library also supplies standard Host and
Content-Length framing. TLS uses normal platform certificate/hostname
verification. There are no redirects, retries or `/v1/messages` calls, including
on 401, 403, 429, 501, malformed data or timeout. No native CLI is invoked.

Preflight refuses inherited proxy/bypass, custom CA, TLS key logging and known
provider-auth/routing environment settings. The subprocess receives only
`LANG=C` and `LC_ALL=C`, closes inherited descriptors, and uses no preexec hook.
Default platform trust is still used. Environment checks cannot establish an
organization's policy; approval must resolve that separately.

The socket timeout is five seconds. A parent deadline of ten seconds includes
worker startup, credential validation, DNS, connection and response handling.
The deadline requires a living parent. On timeout or Python exception unwinding
(including KeyboardInterrupt), the parent kills and waits only its owned child.
Abrupt parent termination, including default SIGTERM or SIGKILL, bypasses this
cleanup and removes the parent deadline; the child may continue. The parent
does not kill process groups or unrelated processes. Header, chunk-size and
trailer lines share a 64 KiB budget, including interim headers. Each chunk-data
trailing CRLF is outside that budget, so total wire metadata can exceed 64 KiB.
The separate decoded-body limit is 64 KiB (one extra byte is read to detect
overflow). The parent receives only bounded sanitized JSON. This is a small
cooperative research process, not a hostile-child execution sandbox.

Retained JSON contains HTTP status; local UTC request-start/receive times and
monotonic elapsed duration; validated Date, Age, Cache-Control, request-id and
retry-after; and the explicit unified 5h/7d utilization/reset/status values.
Missing and malformed allowed headers are distinguished. Header formats are
intentionally restrictive: unusual valid formats can be rejected. Other
scope/model-family headers contribute booleans only, not names or values.
The body contributes only `input_tokens` presence and an allowlisted error type;
no count, free-text error, cookie, account/profile identifier or arbitrary header
is retained. Token echoes are rejected even in nominally permitted fields.
Malformed bodies produce a sanitized invalid-response result.

`windows_complete` means both utilization/reset pairs are syntactically present
in an HTTP 200 response without an error; optional status must be valid when
present. Missing/malformed windows are unusable. Completeness does not establish
server-side freshness, quota family, account UUID, safe polling cadence or a
protected-launch capability. Request and receive timestamps are local timing,
not provider computation times. Errors cannot justify changing the model,
endpoint or authentication and repeating the request. Exit zero means a result
was recorded, including negative results; exit two means preflight refusal.

## Offline verification

Run only the fabricated test suite:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_token/test_permission_probe.py
```

The 21 tests pass. Behavioral RED evidence was captured against inert
placeholders; additional RED/GREEN cycles cover cumulative chunk-trailer
limits and rejection of abbreviated execution flags. Transport fakes verify exactly one count call, fixed payload/headers,
verified TLS, no generation, redirects/errors/501, redaction and malformed data.
Temporary fabricated files cover unsafe ownership/modes, symlinks, hard links,
FIFOs, size, syntax, account/path association and one-read behavior. Real local
subprocess tests cover timeout/reaping, preservation of an unrelated child,
and a threaded parent with an inheritable loopback listener. The unchanged actual child
entry is exercised with both syntactically valid and invalid fabricated tokens under
a test-only bootstrap audit guard that denies every socket event, including
DNS and connect. A second fail-closed guard keeps the primary-guard-removal
RED/mutation test offline. The valid fixture proves that network denial is
independent of token validation. A small-chunk wire fixture pins the documented
exclusion of chunk-data CRLF from line accounting. Fixture temporary directories
are automatically removed.

Ignored `permission-*.log` files beside the probe retain focused RED/GREEN and
all-files hook logs with matching exit records. This standalone research test
is intentionally outside runtime suites; no production behavior, shared
specification or coverage ledger changes are included.
