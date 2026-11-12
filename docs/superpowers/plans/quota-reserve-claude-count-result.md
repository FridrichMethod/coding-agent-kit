# PT live count permission result

One explicitly authorized request was completed on November 12, 2026 using
one selected existing setup-token account. The account name, exact local path,
authorization, request ID, timestamps and sanitized response are preserved in
the private Git common-directory ledger, not in this tracked report.

## Procedure and result

The coordinator used the independently reviewed PTP probe unchanged from
`d6ce140b6526e479cbb707e4a2400049f85dd29a`, as integrated at `bb195717`.
Metadata-only configuration resolution identified the selected token file;
the file was an owned regular mode-0600 file with no symlink path components.
No account enumeration, default-login access or credential copy was performed.
The harness read the selected token only in its owned child process and sent
one fixed direct HTTPS request to the first-party count endpoint. See the
[reviewed operation and limits](quota-reserve-claude-permission-probe.md).

| Observation | Result |
| --- | --- |
| Endpoint | `POST https://api.anthropic.com/v1/messages/count_tokens` |
| Model and text | `claude-sonnet-5-5`; `quota metadata probe` |
| HTTP status | 200 |
| Parsed `input_tokens` field | Present; value not retained |
| Five-hour utilization/reset/status headers | All missing |
| Seven-day utilization/reset/status headers | All missing |
| Other unified quota or scope headers | None observed |
| Date and request-id headers | Present and format-valid; values private |
| Age, Cache-Control and Retry-After | Missing |
| `windows_complete` | False |
| Probe exit | 0, meaning a result was recorded |
| Model generation, retry, redirect or native login | None |

The one-shot authorization is consumed. The durable attempt record was created
before execution and completed afterward; this request must not be repeated
automatically, even to collect additional fields. No token, token hash, raw
body, arbitrary headers or free-text error was retained. The reviewed harness
and its 21 offline tests were already accepted; they were not modified for
this account.

## Consequence and limits

The request establishes count-endpoint access for this token and payload.
Its response provides no subscription-window observations, so this hypothesis
does not supply the required model-free startup quota check. Token counts are
not quota percentages. HTTP 200, a fresh HTTP Date or a successful parser
cannot replace missing quota windows.

This single result does not prove that every possible setup-token-compatible
quota interface is unavailable. It does not authorize another endpoint probe,
an authentication change or a generation request. Native full-login freshness
and control research may proceed under the user's separately accepted design,
but a real first login still requires a named account and user-performed
authentication. G0 remains dependent on fresh attributable quota, complete
native control, loss behavior and verified manual continuation.
