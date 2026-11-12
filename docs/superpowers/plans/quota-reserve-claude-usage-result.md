# PTU live usage permission result

One separately authorized request completed on November 12, 2026 using one
selected existing setup-token account. Exact account/path, authorization,
request ID and timestamps remain in the private Git common-directory ledger.
This tracked report contains no credential, account name or machine path.

## Procedure and observation

The coordinator executed the unchanged independently reviewed PTU harness from
`22490bce1fb43235f2137a2d40979d1c1cce9b72`. Metadata-only configuration resolution
confirmed the selected token path. Preflight confirmed an owned regular 0600
single-link file without symlink path components; it did not read the token.
The owned isolated child then read that one token and sent the fixed request.
See the [reviewed operation and limits](quota-reserve-claude-usage-permission.md).

The usage probe SHA-256 was
`5aa0d2464bfa70d14fd312e81c29d739f377e26704ce493d089d6cdcdfbcbf5a`;
the unchanged PTP helper SHA-256 was
`a7ed059dc443ec3ae961f83f26ead9efe1fa602739d1b7b02c8d6e7091e50f63`.
Both matched the reviewed proposal before execution. No real native login,
default-account access, enumeration or credential copy was performed.

| Observation | Result |
| --- | --- |
| Endpoint | `GET https://api.anthropic.com/api/oauth/usage` |
| Body / query string | None |
| HTTP status | 403 |
| Allowlisted error type | `permission_error` |
| Five-hour / seven-day windows | Both missing |
| Other model-window fields | Not observed |
| `windows_complete` | False |
| Date / request-id headers | Present and format-valid; values private |
| Age / Cache-Control / Retry-After | Missing |
| Monotonic request duration | 0.17481 seconds |
| Probe exit | 0: a result was recorded, not quota success |
| Retry / redirect / refresh / login / generation | None |

The exclusive durable attempt record was created before the request and then
marked completed with one request and permission consumed. Do not retry it
automatically. No raw body, free-text error, token/hash, arbitrary headers or
credential record was retained. The reviewed 22 offline tests were already
accepted; the harness was not changed for this account. Reporting this result
does not require another live request.

## Consequence and limits

The selected token and fixed request did not obtain quota permission/windows.
Together with the [count result](quota-reserve-claude-count-result.md), these
tested paths do not satisfy model-free startup. This single 403 does not prove
that every setup-token-compatible interface or account is incapable of quota
reads. It does not authorize other endpoints, retries, login or generation.

The permitted persistent-native-login fallback still needs a separately
reviewed, named-account experiment, actual fresh quota/account/family/bearer
binding and full-A composition. Native owns refresh and ordinary switching must
reuse saved credentials without relogin. G0 remains unpassed; see the
[next-session auth nodes](quota-reserve-auth-next-session.md).
