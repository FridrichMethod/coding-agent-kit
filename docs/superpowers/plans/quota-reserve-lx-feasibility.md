# LX: reviewed live probe for one Codex usage read

Status: **implemented and tested offline only; never run live**. LX is the
probe the coordinator may run once against the codex-as account `stanford`
under the codex section of `live-permissions-approval.json`. That run starts one
owned `codex app-server` and makes one or two `account/rateLimits/read` calls.
A loopback gate forwards only the usage GET over verified TLS to the production
backend, and the probe sends no thread, turn or model request. This node ran
nothing against a real account, credential, login, backend or network. No
product runtime changed. This is not a G0 pass, a live result or production
capability.

Base `afffc23`. Linux x86_64, Python 3.15.2 with OpenSSL 4.1.0, `codex-cli
0.167.2`, binary SHA-256
`a9672d0fa0dd8fcf8cad136fa0d056b914bc046f5ac3d78f4a5cf390655af56b` (the binary
QX, PC and PR used). macOS was not tested; the probe refuses there.

Labels: **N** native observation in this node, **S** inspected source or
documentation, **I** inference, **U** unproved.

## 1. Evidence boundary

- Every test uses a temporary HOME holding the kit's `config.toml` and a
  codex-as account `lxfixture`, created by the unchanged `lib/codex-as` layout
  code. The account holds fabricated credentials: unsigned JWTs that name
  `fixture-account-lx-*`. Nothing was read from the real home, Keychain or
  secrets directory.
- Tests forward to a loopback TLS server whose temporary CA is trusted only
  through the test-only context hook (`testkit.test_upstream`). Refresh
  CONNECTs reach a loopback sink or are refused. Only `live_codex.main()`
  builds the production upstream and tunnel, and no test calls it unmocked.
- The real native ran only in `test_native.py` and `run_offline.py`, under PC's
  guard. The guard allows only loopback TCP and Unix sockets, follows forks and
  execs, and passed its self-check first. Each app-server made one `socket()`
  call (syscall 41) that the guard denied, as in QX. The family of that call is
  unknown (U).
- Every output file and stream is scanned for each fabricated secret, the
  temporary home, the host name, and JWT-, UUID- or token-shaped text. The
  scan found nothing. Temporary roots (`quota-lx-*`) are listed in
  `.cache/lx/temp-roots.txt` and removed, and none was left.

## 2. The probe: `lx/live_codex.py`

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/lx/live_codex.py \
    --account stanford --out DIR [--delay SECONDS] [--single-read]
```

The probe uses only the standard library. It loads `lib/coding-agent-kit/config.py`
and `lib/codex-as/{check,layout}.py` by path from its own checkout and calls
them read-only. Exit codes: `0` means the planned reads ran and the result was
written. `2` means the probe refused before spawn and the attempt was not used.
`3` means the attempt was used but did not complete. `130` means it was
interrupted after the attempt file existed.

**Refusals before spawn (exit 2).** Each prints a fixed reason code:

| Check | Reason code |
| --- | --- |
| Account other than `stanford`; `--delay` outside (0, 120]; not Linux x86_64 | `account-not-approved`, `delay-out-of-range`, `platform-unsupported` |
| System trust store holds no CA | `system-trust-store-empty` |
| `SSL_CERT_FILE`, `SSL_CERT_DIR`, `SSLKEYLOGFILE`, `OPENSSL_CONF`, `OPENSSL_MODULES`, `OPENSSL_ENGINES` set | `tls-environment` |
| `--out` not absolute; inside a Git work tree; inside `codex_home`, `secrets_dir` or the repository; existing but not a 0700 directory owned by the caller, or not empty; unsafe parent | `out-*` |
| codex-as preflight (endpoint and auth variables, `CODEX_HOME`, config sources) | `codex-as-preflight` |
| Account unknown, setup pending, layout or link not as codex-as left it, not a ChatGPT login | `account-unknown`, `setup-pending`, `layout-unexpected`, `not-chatgpt-login` |
| Account lease cannot be taken exclusively without blocking, so a codex-as client is running | `lease-held` |
| Guard self-check fails; `codex --version` is not 0.167.2 | `guard-self-check-failed`, `version-mismatch` |

The probe never repairs, imports, copies or edits anything. Layout damage is
refused as found. Like codex-as, it downgrades the lease to shared after
validation and validates the owner again.

**One attempt.** Before spawning, the probe creates `DIR/lx-attempt.json`
(0600, `O_EXCL`). A non-empty `DIR` refuses, so each out directory allows one
attempt.

**Spawn.** The probe starts `codex app-server --listen stdio://` under the
guard in a new process group. The child gets this environment:

- the allowlisted caller variables (`HOME`, `USER`, `LOGNAME`, `PATH`, locale
  and `TZ`);
- `CODEX_HOME` and `CODEX_SQLITE_HOME`, set exactly as codex-as sets them;
- every proxy variable pointing at the gate, with `NO_PROXY` for loopback;
- `TMPDIR` and the working directory set to a private workspace inside `DIR`,
  so no project config layer applies (I).

The account lease fd is inherited (shared), as codex-as's `run_native` passes
it. The probe edits no file. Its `-c` overrides keep codex-as's fixed keys
(`cli_auth_credentials_store="file"`, `sqlite_home`,
`check_for_update_on_startup=false`). They point `chatgpt_base_url` and a
forced provider `lxgate` (`requires_openai_auth`, no WebSockets, zero retries)
at the gate. They also turn off analytics, OTel export, web search, plugins,
apps, MCP apps, memories, the network proxy feature and the code-mode prewarm.

**RPC.** The probe sends `initialize` (client `coding-agent-kit-lx-probe/1`),
then `initialized`. It then calls `account/rateLimits/read` once with
`excludeResetCreditDetails: true`, which skips the reset-credits GET (QX
section 1). It makes a second read only after `--delay` (default 30 s), and
only if the first returned `ok` and its single forward came back 200. The probe
answers server requests with an error and records them by method name.
Notifications are only counted. Nothing is retried. Initialize times out after
20 s, each read after 40 s, and the whole run after 200 s.

**Finish.** The probe closes stdin and waits 5 s. If the group is still alive,
it sends SIGTERM and then SIGKILL to the whole group and checks that the group
is empty. It releases the lease only after that. A read-only post-check then
records three booleans:

- whether the credential's (device, inode, mtime, size) changed;
- whether the account link still points at the secret;
- whether the secret is still a 0600 single-link regular file owned by the
  caller.

The workspace is removed. Native stderr is discarded, except for its line count
and the guard's syscall summary. The probe sanitizes the result and writes
`DIR/lx-result.json` (0600, `O_EXCL`). If the sanitizer finds anything token-,
id- or path-shaped, it redacts it and records the finding. The run then exits 3
instead of 0. codex-as's post-run repair does not run here; the next codex-as
launch of the account runs it.

## 3. The gate: `lx/gate.py` (based on `qx/gate.py`)

- **Forwarded:** only `GET /backend-api/wham/usage` with a Bearer and no query,
  body or upgrade. A forward also needs the controller to have armed the gate
  for the current read: each read arms one forward, and the whole run allows at
  most 2. A failed forward counts toward the cap.
- **Upstream:** `chatgpt.com:443` (QX section 5). Each forward opens a new
  connection using `ssl.create_default_context()`: the system trust store,
  `CERT_REQUIRED` and a hostname check. A weaker context is refused before
  connecting. Resolution is bounded and tries at most 2 addresses. Each phase
  has a 5 s timeout, a watchdog ends the forward after 15 s, and the body is
  capped at 64 KiB. A 3xx is neither followed nor relayed. The module has no
  plain-HTTP or unverified upstream, so it has nothing to fall back to.
- **Headers sent:** `authorization`, `chatgpt-account-id`, `accept` and
  `user-agent`, plus `Host: chatgpt.com`, `Accept-Encoding: identity` and
  `Connection: close`.
- **Refused locally:** every other path and method, every model POST and every
  unarmed or over-cap read gets a 503 before any upstream byte. The gate never
  answers 401, because that would start a native refresh. Refusals are counted
  by fixed categories. A failed forward gives native a 502.
- **CONNECT:** the gate tunnels only `auth.openai.com:443`, which is native's
  refresh host and is approved. It allows at most 3 tunnels, relays bytes
  without inspecting them, and closes a tunnel after 30 s idle or 60 s in
  total. Every other destination gets a 403 and is recorded by host:port. The
  gate cannot see the path inside a tunnel, so it admits whatever native sends
  to that host (I, as QX section 5 notes).
- **Identity:** the gate decodes the bearer's JWT claims in memory. It compares
  them with the request header, codex-as's expected owner (from the login
  record), and the response `account_id`/`user_id`, keeping only booleans.
  Bearers are compared across forwards through a per-run random HMAC key. Ids
  seen in transit stay in memory, only as secrets for the output scan.

## 4. What `lx-result.json` retains

The result keeps the following:

- status codes, phases and outcomes per forward;
- request, forwarded and response header names (never values);
- the TLS version;
- plan; window durations, percentages, reset times and reset-after seconds,
  for both the backend and the native view;
- `allowed` and `limit_reached`;
- the presence, never the value, of optional fields and additional rows;
- the identity booleans;
- the QX lease verdict code;
- for two reads: elapsed time, whether the two responses were equal, and
  per-window deltas;
- RPC states and fixed error classes (code, kind, HTTP status, decode error);
- method names sent; notification and server-request counts;
- forwarded and refused counts, and CONNECT counts and bytes;
- the native version, SHA-256, user-agent version, guard summary and reap
  record;
- the credential booleans and platform versions.

It never keeps tokens, ids, emails, raw bodies, header values, error message
text, native stderr or paths.

## 5. Offline results

`run_offline.py` exited 0. `test_gate` ran 21 tests, OK; `test_live_codex` ran
25, OK; `test_native` ran 5 with the real 0.167.2, OK. All five native cases
completed with no leak findings, and no temporary root was left
(`lx/evidence/manifest.json`).

| Brief requirement | Covered by |
| --- | --- |
| Forwarding allowlist, 2-request cap | `test_gate` armed, unarmed and cap, path, query, body, missing bearer; `test_live_codex` extra reads; native: 2 reads give 2 upstream requests |
| Model POST refused | `test_gate` 503-never-401; `test_live_codex` fake model POSTs (0 reach upstream) |
| Identity booleans present, absent, mismatch | `test_gate` three cases plus another login; native `identity-absent` and `identity-mismatch` |
| One-shot guard | second attempt in the same out refuses with nothing spawned; attempt file exists when the app-server dies |
| Lease-held and layout refusals | shared lock held refuses before spawn; replaced link, missing link and marker refuse with nothing written or repaired; unknown account, pending setup, preflight |
| Private out dir | relative, 0755, in a Git tree, in `codex_home`, symlinked out, in the repository |
| Token and id never in outputs | every probe test scans all outputs and streams; sanitizer units |
| Process-group cleanup | fake that ignores EOF and SIGTERM and spawns a grandchild: SIGTERM, then SIGKILL, group empty, grandchild gone |
| TLS failure refuses, no fallback | untrusted CA, hostname mismatch, unverified context refused before connect; native `tls-failure`: 0 requests reach the server, TLS or plain |
| Live path uses the system store | `main()` passes `production_upstream()` (`chatgpt.com:443`, `CERT_REQUIRED`, hostname check), `production_tunnel` and the guard; an empty store refuses before configuration is read |

Native 0.167.2 behavior seen through the gate (N, fixtures):

- At startup the app-server sends `GET /backend-api/wham/accounts/check`
  twice and `GET /backend-api/codex/models` once before any read. The gate
  refuses all three (503) and the reads still succeed. In a live run these
  requests never leave the host.
- A usage request carries exactly `accept`, `authorization`,
  `chatgpt-account-id`, `host` and `user-agent`. The initialize `userAgent`
  names 0.167.2.
- Each RPC caused one upstream GET. The second read, 1 s later, returned the
  new fixture response (`reset_at` delta 1), so native did not cache it.
- When the response omits `account_id` and `user_id`, native still succeeds,
  with no `accountId` and `ordinaryUsageAllowed: null`, and the QX verdict is
  `unavailable:account-mismatch`. With a foreign identity, native succeeds and
  reports the foreign account (equality false, `ordinaryUsageAllowed: null`).
- TLS verification failure: native gets the gate's 502, and the read fails
  (`fetch-failed`, HTTP 502). The probe skips the second read.
- Token that expires within 120 s: native tried 7 CONNECTs to
  `auth.openai.com:443`. Three were tunnelled to the loopback sink (4638 bytes
  up, 0 down) and four were refused by the tunnel cap. Refresh failed and
  native still read usage with the old token (200), so the credential did not
  change. A refresh through the link is detected as a boolean (fake test).
- Reap: native exited on stdin close, no signal was needed, and the group was
  empty.

## 6. Running it live (coordinator only, once)

Preconditions:

- Linux x86_64, with the kit-configured `codex` at 0.167.2.
- No codex-as session of `stanford` running.
- The TLS and endpoint variables above unset.
- The run uses the same HOME and kit configuration that codex-as uses for the
  account.

From the root of a checkout of this branch:

```sh
OUT="$(mktemp -d /tmp/quota-lx-live-XXXXXX)"
python3 -I -B docs/superpowers/plans/quota-reserve-probes/lx/live_codex.py --account stanford --out "$OUT"
```

- Exit 2 refused before spawn and used nothing. Fix the named condition and run
  again with the same empty out directory.
- Exit 0, 3 or 130 used the approved attempt. Do not run again: the attempt
  guard sees only its own out directory, so not retrying is the operator's
  duty (I).
- Stdout prints one sanitized line: outcome, sanitizer findings, forwarded
  count and statuses.

Check in `lx-result.json`:

- `sanitizer_findings` is absent.
- `gate.forwarded` <= 2.
- `rpc.methods_sent` contains only `initialize`, `initialized` and
  `account/rateLimits/read`.
- `gate.refused_by_category` lists what native tried and the gate refused.
- `gate.connect` shows refresh tunnels and any refused destinations.
- `native.reap.process_group_empty` is true.
- `credential.link_intact_after` and `credential.still_private_regular_file`
  are true.
- `credential.changed_during_run` says whether native refreshed.

Then archive only the sanitized result and remove the out directory.

The run answers QX's open questions for one real bearer:

- whether `/backend-api/wham/usage` answers 200, with which response header
  names;
- whether `account_id` and `user_id` are present and equal the credential
  claims;
- plan, integer percentages, the 300 and 10080 minute windows, reset times;
- whether a read 30 s later is a new response;
- whether native refreshed at startup.

## 7. Proved, inferred, unproved

**Proved (N; Linux x86_64, 0.167.2, fixtures only):** everything in section 5.
The probe refuses every listed precondition before spawning and writes nothing
for it. One owned app-server with command-line overrides only performs model-free
reads at the gate. Only the usage GET is forwarded, at most twice, over
verified TLS with no fallback. All startup and model requests are refused
locally. Identity booleans are correct for present, absent and mismatched
fixtures. Outputs carry no fabricated secret or id. The process group is
reaped.

**Inferred (I):** the live path verifies with the system store, because the
unit test checks what `main()` passes and the gate refuses a weaker context.
`chatgpt.com` is the backend host (QX section 5). Native's refresh can only
use the `auth.openai.com:443` tunnel. No descendant can reach the network,
because the guard follows forks.

**Unproved (U):** everything a real server does (status, fields, identity,
caching); whether this host reaches `chatgpt.com` and gets the API rather than
an HTML challenge page (that would show up as a non-JSON 403 and a failed
read); real refresh traffic inside the tunnel; the family of the denied
socket; macOS.

**Risks to weigh before the live run:**

- The account shares the user's real `config.toml`. The `-c` overrides win for
  every routing key, and codex-as's `config_sources` check runs. MCP servers
  and hooks are session-scoped and the probe starts no thread (I). This was not
  observed with a real config, and the guard confines any descendant anyway.
- `system-trust-store-empty` uses `cert_store_stats()`. A store that has only a
  CA directory loads lazily and counts 0, so the probe would refuse rather than
  weaken TLS. This host's default store is a bundle file, and a unit test
  asserts that it is loaded (N).
- The guard is research containment with a ptrace TOCTOU, not a security
  boundary (PC).
- A real 401 does not make native refresh (QX, S). The probe records HTTP 401
  and skips the second read.

## 8. Reproduction

From the worktree root, on Linux x86_64 with `codex` 0.167.2 on PATH (or
`LX_NATIVE_CODEX`):

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/lx/run_offline.py
pre-commit run --all-files
```

`run_offline.py` runs each suite in its own process with an outer timeout of
300 s or 600 s. A timeout or failure is recorded and is never evidence. Then,
with the guard and the reviewed native present, it runs the five native cases
and scans each result again. It writes `evidence/native-*.json` and
`evidence/manifest.json`. It exits 0 only when every suite passed, the native
cases ran and no temporary root is left. Without the native binary it prints
SKIP and exits 1, which proves nothing.

| File | Role |
| --- | --- |
| `lx/live_codex.py` | probe (refusals, spawn, RPC, reap, sanitizer) |
| `lx/gate.py` | loopback gate and verified-TLS forwarder |
| `lx/testkit.py`, `lx/fake_codex.py` | fixtures: temporary CA, TLS server, fake codex-as home, fake app-server |
| `lx/test_gate.py`, `lx/test_live_codex.py`, `lx/test_native.py` | the three suites |
| `lx/run_offline.py`, `lx/evidence/` | runner and sanitized evidence |
