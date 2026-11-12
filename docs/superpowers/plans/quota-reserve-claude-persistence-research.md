# PA2: persistent Claude login and shared device-state boundary

Research checkpoint, November 12, 2026; base `796847a6`. No runtime change.
The user's latest clarification permits an initial web login fallback while
preferring setup-token-only support. Routine account selection must retain
credentials and must not invoke login. This extends PA0; it does not approve
HAuth, substitute for PT/PA1, or open G0.

## Result

Persistent per-account selection is documented and reproduced locally with
fabricated records. Shared device-state safety for real login and ordinary
sessions remains unproved. A narrowly scoped status/logout result cannot
justify a claim that every native operation leaves device identity untouched.

The [native authentication documentation](https://code.claude.com/docs/en/authentication#log-in-with-multiple-accounts)
says separate `CLAUDE_CONFIG_DIR` directories retain separate claude.ai logins,
settings and history. Console sign-in without an API key is excluded. Existing
sign-in can skip login; expiration without successful refresh or managed login
requirements can require signing in again. Thus switching itself need not
reauthenticate, but perpetual login-free operation cannot be guaranteed.
The [CLI reference](https://code.claude.com/docs/en/cli-reference#cli-commands)
documents `auth status` and `auth logout`. Both pages were checked on the
research date. No actual login, browser, account file, refresh or model call
was used in this research.

## Reproduced Linux matrix

Tested native Claude Code `2.1.322`, SHA-256
`bfb3703ad5380242328459bc62ada4bde8ae7ba57c6e1eeadb9c00e08874c478`,
the same binary as PA0. This is an observation, not a minimum-version claim.

Run the [probe](quota-reserve-probes/claude_persistence/probe.py):

```sh
python3 -I -B docs/superpowers/plans/quota-reserve-probes/claude_persistence/probe.py "$(command -v claude)"
```

It reuses PA0's bounded runner unchanged, extending only strace observation
with `%file`; all original network syscall denials remain active. Each case
has an owned 0700 temporary HOME, two private config directories, a separate
Anthropic profile root, XDG roots, and an empty workspace beneath `/tmp`.
Only an environment allowlist reaches native code. Fabricated OAuth records
have distinct access/refresh strings, future expiry and local identity fields;
these scope and identity strings are test inputs, not granted permissions.
Device sentinels are schema-shaped strings, not usable private keys.

Every native child uses independent syscall denial of socket creation,
connect, datagram/message sending and io_uring. The reviewed runner closes
inherited descriptors, bounds output and execution to 45 seconds per child,
and cleans up its own process group. This is a cooperative research fixture,
not a malicious-binary filesystem sandbox. Native also probes two fixed
hosted credential paths under `/home/claude/.claude/remote/`; the fixture
refuses if either exists, and the retained trace shows ENOENT for both.
System files and machine policy may still be inspected. No settings source is
disabled to manufacture a production authorization claim.

[Sanitized observations](quota-reserve-probes/claude_persistence/observed.json)
record these three cases. In each, five separate native processes run
`A status`, `B status`, `A status`, `A logout`, `B status` in that order.
Only `CLAUDE_CONFIG_DIR` changes between A and B; no login is invoked.

| Initial device state | A/B status and return | A logout | Shared device path |
| --- | --- | --- | --- |
| Absent shared device file | Distinct local emails; credential bytes unchanged | Removes A OAuth; B unchanged | No file created; zero path calls |
| Shared device sentinel | Same | Same | Bytes unchanged; zero path calls |
| Shared sentinel plus A legacy `coworkRemoteDevice` | Same | Removes A OAuth; preserves A legacy device record; B unchanged | Bytes unchanged; zero path calls |

All 15 operations exit 0. Status reports `authMethod: claude.ai` and
`loggedIn: true` for these deliberately invalid credentials, confirming that
status is not server identity/permission proof. File traces contain no path
access to the other account's config directory. No prompts or stream/model
operations were submitted. Denied network attempts are retained as counts;
logout attempts cannot establish successful server-side revocation offline.

Raw stdout/stderr, network/file traces, exit-status evidence, sanitized JSON
and the temporary-root inventory are retained in the worktree's ignored
`.cache/pa2/`; the exact temporary root is printed. The inventory records
paths, modes, sizes and symlinks without logging credential contents. An
initial exploratory run under the worktree discovered repository ancestors;
the committed evidence comes from the subsequent `/tmp` run. No run opened a real credential file. One `/tmp` attempt was conservatively
rejected because strace recorded `socket(...)=?` during process exit; the
unchanged denial assertion passed on the subsequent complete matrix. The
failed trace is retained, not counted as a passing operation. Reproduction retains its root for inspection;
cleanup is a separate canonical command after evidence retention.

## Static device-store call paths

These are implementation findings from the identified executable's embedded
JavaScript, not a supported API or proof of every reachable call path. The
ignored `.cache/pa2-static/` excerpts retain the inspected code for review.
No proprietary source dump is committed.

- `bCr` in embedded `chunk-xmqv1g2t.js` derives
  `HOME/.claude/.device-keys.json`, independently of `CLAUDE_CONFIG_DIR`.
- In `chunk-ekpfnrhf.js`, `N(accountUuid, credentials, mode)` reads both the
  selected secure store's legacy `coworkRemoteDevice[accountUuid]` and the
  shared store's `keys[accountUuid]`. Keys are account-indexed within one
  shared file, not a single OAuth credential shared by every account.
- `load` is not necessarily read-only: `N -> ke -> M` can copy a legacy
  entry into the shared store and remove the matching legacy entry. The file
  reader can rename an invalid document to `.corrupt-...` before returning
  an empty result. Thus even a lookup may mutate shared state.
- `Fct -> Ce -> loadOrCreate` can generate and persist a P-256 private key
  before posting device registration. A denied or failed network request
  does not prove no local write occurred. Callers found include device
  binding and bridge registration; their policy/account gates matter.
- Store updates take the native `.device-keys` write lock and preserve other
  key entries in the loaded document. `nEi -> W -> drop -> Y -> j` can remove
  the matching key before reenrollment; `nfr` clears a cached row ID.
  Account/key equality guards exist, but races and crash safety were not
  tested. The kit must not reproduce or manipulate these locks or records.
- In `chunk-kc1p064y.js` (logout implementation), `pje` can attempt
  refresh-token revocation, delete selected secure storage, then restore its
  legacy `coworkRemoteDevice` map. This preservation matches the fixture.
  It is not evidence that every imported logout cleanup avoids device state.
- Device-store code contains a `Claude Code-device-keys` Keychain service
  without the config-directory suffix used for OAuth storage. This Linux
  build selects file storage. macOS selection, migration and Keychain
  behavior require native macOS evidence; none is inferred from this run.

## Smallest remaining HAuth decision

Keep PA0's explicit per-account native-login schema as the concrete candidate:
one fixed private config directory, native-owned credentials and refresh,
additive opt-in, existing setup-token accounts untouched, no implicit copying
or mode fallback. Normal selection sets that directory and invokes the
requested native operation; it does not perform login/logout. Recovery after
expiry or revocation is an explicit native reauthentication step, not ordinary
switch behavior. No wrapper implementation was added here.

Before real-HOME enrollment, review whether to accept native access to the
shared device store for this optional mode: account-indexed device private
keys, possible legacy migration/corrupt-file quarantine, native locking,
registration and reenrollment updates, while OAuth credentials remain in the
selected account store. Approval must include the existing default account's
shared-state exposure; approving only a new directory is insufficient.
An isolated-HOME product architecture is not proposed as a workaround.

If that boundary is unacceptable, real-HOME enrollment remains blocked until
a supported isolation mechanism or narrower no-access operation is proved.
If acceptable, the coordinator still needs HAuth's explicit decision and a
separately authorized, named-account, user-performed first-login check before
real credential access. Refresh concurrency, macOS behavior, live account
binding, fresh attributable quota, native dispatch control and full option A
remain unproved. PA2 supplies persistence evidence; G0 stays closed.
