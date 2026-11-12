# PTU: one-shot setup-token usage permission experiment

This is an offline preparation artifact, not authorization to execute. No real
credential was read, no account was enumerated and no provider was contacted.
The earlier count-request permission is consumed and cannot authorize this GET.
Setup-token-only remains a hypothesis; G0 and full-A capability remain unproved.

## Exact proposal for separate approval

After independent review, the coordinator must resolve the chosen account's
configured directory without enumerating other accounts, and obtain explicit
approval for that named account, exact absolute file, request and direct HTTPS
network policy. The script does not read configuration or prove this mapping.
The execution flag alone is not permission. Replace both placeholders only
after those steps:

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_usage_permission/permission_probe.py \
  --usage-once --account APPROVED_NAME --token-file EXACT_APPROVED_ABSOLUTE_PATH
```

The basename must be `claude-APPROVED_NAME`. No arguments, `--describe` and
`--help` read no credential and open no connection. There is no configurable
URL, method, model, body, query, fallback or scope override. Abbreviated execution
flags and the old `--count-once` option are rejected.

One GET goes to `https://api.anthropic.com/api/oauth/usage`, without query or
body. Its explicit headers are:

| Header | Fixed value |
| --- | --- |
| Authorization | `Bearer <selected setup token>` |
| Accept | `application/json` |
| Content-Type | `application/json` |
| anthropic-beta | `oauth-2025-04-20` |
| User-Agent | `claude-cli/2.1.322 (external, cli)` |
| Cache-Control | `no-cache` |
| Connection | `close` |

Python's HTTP library additionally supplies `Host: api.anthropic.com` and
`Accept-Encoding: identity`; a bodyless GET has no Content-Length header.
Normal platform TLS certificate and hostname validation remain enabled. This
standalone probe sends native-compatible headers; it does not start the native
CLI, manufacture OAuth scope metadata or claim a different application grant.
There are no redirects, retries, refreshes, logins, generation requests or
native-command fallbacks, including after 401, 403, 429, 501 or timeouts.

## Header provenance and interpretation

The [PA1F report](quota-reserve-claude-freshness-research.md) established the
model-free endpoint in a local HTTPS fixture using fabricated credentials.
Static inspection here read only the installed Linux Claude Code `2.1.322`
executable, identified in PA1F by SHA-256
`bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478`.
These are native implementation observations, not a public stable API promise:

| Source location (approximate byte offset) | Observation |
| --- | --- |
| `KH`/`re`/`ae`, 292559975-292562000 | Plain usage path, GET, 5000 ms timeout, explicit JSON Content-Type |
| `jt`/`uo`, 287208000-287217400 | Shared transport uses selected authentication and adds `Hj()` User-Agent |
| `Yte`, 286980555 | Selected OAuth contributes bearer and beta header |
| `au`, 283184059 | Beta literal `oauth-2025-04-20` |
| `Hj`, 286978351 | Default CLI User-Agent above; native may add entrypoint, SDK, client-app and workload decorations |

This probe fixes the observed default CLI User-Agent, without discovering or
copying any local session decorations. Accept, no-cache and close are deliberate
probe headers. No `anthropic-version` header is added: that count-request header
is not shown in this native usage call. Python transport, compression, header
ordering, policy and refresh behavior differ from native; this is not exact
wire equivalence. A negative response applies only to the selected token,
instant and fixed request. It cannot establish that every setup-token usage
mechanism is impossible. Any further experiment needs its own approval.

## Credential, transport and retention boundaries

The unchanged [PTP helper](quota-reserve-probes/claude_token/permission_probe.py)
is loaded by physical path for its reader, bounded response and typed header
validators. It is never called through its count-request worker or CLI.
The new owned child reenters this GET script with `--usage-once`.

The child suppresses core dumps before opening the explicit file. The shared
reader rejects symlinks in every component, non-normalized or relative paths,
wrong basename/account association, nonregular files, wrong owner, mode other
than 0600, multiple hard links, oversized content, invalid setup-token syntax
and detected mutation across one bounded fd read. No token enters argv, logs,
hashes, copies, cache files or parent memory. It exists in child memory and the
TLS Authorization header. Same-UID adversaries, privileged debugging, swap and
a compromised interpreter are outside this boundary.

Preflight refuses inherited proxy/bypass, custom CA, keylog and known provider
auth/routing environment settings. The fresh isolated Python child receives
only `LANG=C` and `LC_ALL=C`, closes inherited descriptors, and has null stdin
and stderr. If organizational routing/trust is required, stop for a separately
reviewed transport; these checks do not determine organizational policy.

A five-second socket timeout and ten-second parent deadline bound the ordinary
run, including DNS and credential validation. On timeout or exception unwinding
the parent kills and reaps only its owned child. As in PTP, abrupt parent death
(including default SIGTERM or SIGKILL) bypasses cleanup: the total deadline
requires a living parent. No production supervisor or process-group killer is
added. Header, chunk-size and trailer lines share a 64 KiB limit; chunk-data CRLF
bytes are excluded. The decoded body has a separate 64 KiB limit plus one
overflow-detection byte. The parent accepts at most 16 KiB of sanitized output
after collection; this is a cooperative child, not a hostile-output sandbox.

Only HTTP status; local UTC start/receive and monotonic elapsed timing; typed
Date, Age, Cache-Control, request-id and Retry-After; allowlisted error type;
and validated `five_hour`/`seven_day` utilization and `resets_at` are retained.
Each allowed header/window has a missing/valid/malformed state. Duplicate JSON
keys fail the response; duplicate allowed headers fail that field. No raw body,
free text, cookies, profile/account identifiers, token echo or input count is
retained. Other `five_hour_*`/`seven_day_*` fields produce one presence boolean
only, never their names or contents. Unrelated fields are discarded.

Utilization is a finite numeric percentage in [0, 100], excluding booleans and
strings. Decimal bounds are checked before float conversion; values that cannot
round-trip unchanged through the emitted JSON number are rejected, not rounded.
Reset strings require a valid calendar time, explicit timezone and at most six
fractional second digits. Unusual valid provider representations can therefore
be refused. The script does not substitute local time or infer a reset.

`windows_complete` requires both valid windows in an HTTP 200 response without
an error. It is syntactic evidence only: no server computation timestamp,
freshness guarantee, account/organization identity, polling cadence, pause
control or full-A protected-launch capability follows. A successful JSON
response alone is insufficient. Exit zero records a result, including failures;
exit two means preflight refusal. The provider may log this authenticated read;
no quota mutation or billable generation is requested, but provider-side effects
cannot be established by offline testing.

## Offline verification and coverage

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_usage_permission/test_permission_probe.py
pre-commit run --all-files
```

The standalone suite uses only fabricated token files and fake transports.
It reuses PTP reader, wire-budget, timeout/reaping and descriptor-isolation tests
without modifying PTP. Actual GET child entries have a test-only socket audit
guard and an independent backstop before any probe import. A valid fake token
reaches an attempted GET and is denied at DNS, proving denial independent of
validation. Removing the primary guard is detected while the backstop still
denies network. The tests also pin the new script path and GET method so reentry
into the count probe fails. Other synthetic process fixtures only sleep or
check a closed descriptor and never run a provider entrypoint.

| Source boundary | Coverage |
| --- | --- |
| Fixed request and response | Exact GET/headers/bodyless shape, verified TLS, errors/redirects/501, no retry, socket timeout, malformed/oversized JSON |
| Retained fields | Numeric endpoints, precision, NaN/infinity/boolean rejection, reset validation, duplicate keys/headers, token echoes, account/free-text exclusion |
| Credential reader | One read, basename/account association, ownership/mode, symlinks/hardlinks/FIFO, size and syntax |
| Owned child | Actual entry plus valid/invalid fake token under independent network denial, exact script/flag, core suppression, sanitized exceptions, deadline/reaping, unrelated child survival, threaded-parent fd closure |
| Offline defaults | No-argument/help/describe cannot read or connect; exact execution flag and unsafe-environment refusal |

Focused RED/GREEN logs and exit records are in ignored `.cache/ptu/`, alongside
the all-files hook log. This report is the research coverage matrix; runtime
test entrypoints and `tests/COVERAGE.md` are unchanged within this node's scope.
Independent review and concrete named-account permission remain outstanding.
