# Account quota reserves for codex-as and claude-as

Status: accepted as the basis for implementation-method exploration and handoff.
This document describes a proposed feature, not existing launcher behavior.

The user selected a new-session, multi-agent DAG workflow with separate
worktrees, incremental commits and progress records, manual design gates,
CI-verified integration into main, push, and cleanup to a release-ready state.
The user also approved exploring native full OAuth login for protected Claude
accounts while retaining the existing setup-token mode. Credential isolation
and migration require a separate design review before implementation. These
decisions do not waive the native-control feasibility requirements below.

The latest clarification makes setup-token-only support the preferred path.
If it cannot satisfy protection, the user permits an initial web login per
account with persistent credentials. Routine account switching must select
the saved account without another login. Native credential refresh stays with
Claude; revoked or unrefreshable credentials may require explicit recovery.
This supersedes the earlier hard token-only constraint. The user subsequently
accepted the explicit per-account native-login schema and Claude's native
shared device-state boundary. Credentials and refresh remain native-owned;
there is no silent migration or default-account credential reuse. Real-account
experiments remain individually scoped. Ordinary setup-token behavior remains
intact.

The user separately authorized research of a foreground, per-invocation model
request gate proxy after native control counterexamples were reproduced.
The user also accepted research of HTTP/SSE protected routing with a dedicated
continuation control outside the native UI. This expands research scope only;
the concrete production architecture still needs the required evidence and
G0 review.
All startup, freshness, owned-work, native-UI, monitor-loss and manual-resume
requirements remain unchanged.

## Intent and agreed behavior

Protect an account's remaining subscription allowance when using it through
`codex-as` or `claude-as`. The user prefers interrupting agent work while
keeping the native interactive interface available for inspecting history.

- Set separate remaining-percentage reserves for the five-hour and weekly
  windows. Never combine them into one percentage or average them.
- Resolve each window independently: invocation override, then the selected
  account's setting, then the global default.
- Invocation overrides affect only that launch and never change saved settings.
- Query account quota before allowing model work, then monitor during the run.
- Pause when either protected window reaches its reserve. Include model work
  delegated by this invocation; do not control unrelated sessions.
- On persistent inability to obtain valid quota, pause instead of silently
  continuing without protection. Allow bounded retries for transient failures.
- Reset or successful monitoring recovery never resumes work automatically.
  Require deliberate user continuation and a fresh successful quota check.

These reserves are account-level observations, not reservations enforced by
the provider. In-flight requests, other clients, and reporting or cancellation
delays can cross a threshold before the launcher reacts. No exact remaining
percentage or fixed maximum overshoot is promised.

## Policy and proposed interface

For each enabled window, compute `remaining = 100 - used_percentage` from the
provider's unrounded value. Pause when `remaining <= reserve`. Continue only
when every enabled window has valid current data and `remaining > reserve`.
Token totals, context occupancy, and estimated dollar cost are not substitutes
for subscription quota percentages.

The proposed command-line spelling is identical for both launchers:

```sh
codex-as --reserve-5h 10 --reserve-weekly 40 borrowed
claude-as --reserve-5h 10 --reserve-weekly 40 borrowed
```

Wrapper flags precede the account name. Arguments after the account name keep
their native meaning. There is no ambiguous combined `--reserve` flag.

Each value is a finite percentage from 0 through 100, or `off` to disable
protection for that window explicitly. Zero remains an enabled check that
pauses at exhaustion; 100 prevents starting model work even at zero usage.
Omitting a flag inherits that window's setting; it does not disable it.
Duplicate flags for the same window are configuration errors.

The following TOML layout is proposed for review, in the existing kit config:

```toml
[usage_reserve]
five_hour = 20
weekly = 30

[usage_reserve.codex_accounts.borrowed]
weekly = 40

[usage_reserve.claude_accounts.borrowed]
weekly = 40
```

Account settings are keyed by launcher and account name: identical names in
Codex and Claude do not imply the same provider account. A missing account
field inherits the global field. A missing global field defaults to `off`.
Explicit `"off"` in a more specific layer overrides an inherited percentage.
The factory default disables both checks, preserving existing behavior.

For example, the TOML above plus `--reserve-5h 10` resolves to five-hour 10%
and weekly 40% for `borrowed`. Its next ordinary launch resolves to five-hour
20% and weekly 40%. The example percentages are not factory defaults.

Validate percentages without accepting booleans, non-finite values, or values
outside the range. Configuration loading stays centralized in
`lib/coding-agent-kit/config.py`; the launchers consume the frozen result.
Show the effective per-window policy when a protected session starts, without
changing existing machine-readable metadata query output.

## Runtime states and recovery

1. **Checking:** resolve policy, verify that this native version and launch
   mode can enforce it, and actively query all enabled windows. Do not start
   model work before these checks pass. Previously saved usage may explain a
   refusal but never authorizes a protected start by itself.
2. **Running:** consume attributable quota updates and refresh as needed.
   A threshold observation requests a pause immediately. A transient query
   failure permits only a bounded recovery interval, measured from the last
   successful fresh observation; repeated failures cannot extend it forever.
3. **Paused:** stop further model work from the invocation, keep the native
   interface available, and retain the reason. Continue bounded monitoring so
   the user can see when continuation is possible; bound each request and
   retry rate, not the lifetime of the paused state. No background event,
   queued prompt, hook, timer, or subagent report may restart model work.
4. **Manual continuation:** a deliberate user action requests a new quota
   check. Resume only if all enabled windows pass and control remains healthy.
   The control mechanism must distinguish this action from previously queued
   prompts and background wakeups. Otherwise stay paused and explain the
   remaining condition.

Failure during startup produces a clear refusal with no model request.
Failure after work begins enters the same persistent paused state used for
thresholds. Missing, malformed, stale, ambiguously attributed, or unsupported
window data is unavailable, never zero usage or unlimited allowance. A fresh
response that confirms an unchanged percentage can refresh observation age;
re-reading an old cache cannot.

Window resets are independent. A five-hour reset cannot clear a weekly
violation. Passing a cached reset timestamp does not prove a window is now
empty; obtain new server data. An unchanged enabled reserve applies to the
next window too. A reset while paused only makes manual continuation eligible.

The first version changes reserves at launch. To lower a reserve after a
pause, exit and resume using new invocation flags. No new in-session settings
editor or custom slash command is required.

## Stop scope and user feedback

Two protected sessions of the same account can have different reserves. If
one reserves 20% and the other 10%, the first pauses at 20% while the second
can continue until 10%. Consumption outside either session still contributes
to the shared account percentage. This feature is not an account-wide lock.

Pausing must cover active and future model work delegated by the invocation,
not merely the top-level prompt. Do not infer ownership from a process name
or kill every native CLI running under the user. Independent sessions and
independently launched jobs remain outside the stop scope.

Cancel in-flight model work where the native interface permits it. Preserve
already written files and conversation history without promising rollback or
a complete checkpoint. Do not spend more model allowance on a final summary,
commit, or handoff. A deterministic local message identifies the account,
triggering window, observed remaining percentage, reserve, reset time, and
how to request continuation. Query failures identify the monitoring problem
and last successful observation instead of inventing a remaining percentage.

The exact effect on an already running tool command must be established in
the native-control probe below. Neither uninterrupted local tools nor their
automatic termination is assumed. The implementation must disclose that
behavior, particularly for long-running training and tests, before adoption.

## Data sources and control boundaries

The current Codex usage collector scans rollouts after the native child exits;
its account attribution has documented gaps. It is not sufficient for live
enforcement. A fresh startup query is possible through native App Server
`account/rateLimits/read`, after initialization, without starting a thread or
model turn. Use the selected account's native credential configuration and
preserve existing credential leases and post-run repair behavior.

Normalize windows by the provider's stated duration or explicit window name.
Do not assume `primary` always means five-hour or `secondary` always means
weekly, and do not mistake another model's quota family for the intended one.
If the native authentication identity or applicable quota family changes,
revalidate its association with the selected account and protection policy
before permitting further model work. Pause when that association cannot be
verified; do not keep checking the old identity's quota after a switch.

A separate query App Server does not control a turn owned by the existing
native TUI. The current wrapper launches interactive Codex without a daemon.
Codex's diagnostic model-request hooks cannot block requests, and sending an
OS signal is not proven equivalent to the UI's interrupt key. Reading quota
and pausing the correct native session require separate verified mechanisms.

Claude's status line already supplies window observations during supported
interactive runs, but cannot alone supply the startup preflight. Native
`/usage` and experimental programmatic usage interfaces are investigation
paths. The current wrapper uses long-lived `setup-token` credentials, which
the official authentication documentation describes as model-request-only.
Independent usage-query compatibility with those credentials remains to be
verified. Do not assume a full login's capabilities apply to this token.
The native profile-scope gate blocks the tested setup-token usage query; that
local rejection alone does not prove every server endpoint rejects the token.
Any alternative must still provide fresh, attributable subscription windows
without a model request. Token counting, missing data, cached response headers
or a first inference used to seed usage cannot satisfy startup preflight.
If no such token-only mechanism can be proved, the permitted fallback is an
explicit, persistent full-login account mode after its credential design is
reviewed. It must satisfy the same model-free startup and freshness checks;
login itself is not evidence of quota capability.

Claude hooks can stop an agent loop at documented boundaries. That does not
establish cancellation of every active subagent or in-flight model request.
The wrapper currently execs Claude; adding a supervisor would also require
preserving the status line's validated native-PID association.

## Required feasibility evidence before implementation

The product semantics above are agreed; the following technical evidence is
required before choosing an implementation architecture:

- For each provider, demonstrate a model-free startup query under the
  launcher's supported authentication mode. Record native version, window
  identity, reset semantics, and supported freshness signals. If Claude needs
  different credentials, explain why setup-token-only cannot satisfy the
  requirement and review the persistent-login fallback with the user. Prove
  account isolation and reuse across switches before changing enrollment or
  adding a credential store. Real-account checks require separate approval.
- Demonstrate that the native TUI stays usable while current model work,
  delegated agents, automatic continuation, and new model submissions remain
  stopped. Demonstrate deliberate manual continuation after rechecking quota.
- Record what interrupting does to already running tool commands, background
  agents, terminal state, signals, and saved history. Verify ownership across
  resume, in-session session switching, and concurrent accounts.
- Define measured, bounded query timeouts, polling intervals, retry backoff,
  and maximum observation age. Respect usage-endpoint rate limits and avoid
  repeatedly authenticating or running unbounded background processes.
- Establish how loss of the monitor or native control channel is detected and
  stops further model work. If preserving the interface cannot be combined
  with that guarantee, report the limitation; do not substitute process exit
  or warning-only behavior without revisiting the design with the user.

Use isolated homes, fictitious credentials, and local fixtures for probes.
Real-account permissions cannot be established by those fixtures; label that
limit explicitly and obtain authorization for any later credential-bearing
integration experiment. No real-account experiment has been performed during
this design discussion.

Unsupported native versions or launch modes must reject any launch with at
least one enabled reserve, including an inherited account or global setting.
In particular, headless mode cannot claim protection from
an interactive-only status line or TUI control mechanism. When protection is
disabled, the current launch and forwarding behavior remains unchanged.
Non-inference commands such as help, version, and offline account queries
retain their current behavior and do not acquire quota-query requirements.

## Implementation shape, compatibility, and validation

Keep policy resolution and the window/state evaluator separate from native
quota adapters and session control. Prefer documented native interfaces and
existing standard-library infrastructure. Do not add a persistent service,
replacement terminal UI, third-party runtime dependency, or account-global
coordination protocol. Any additional protocol client must justify its scope
under the repository's dependency and maintenance rules.

Public flags, configuration, completion, and help changes require a minor
release with matching documentation and CHANGELOG updates. The design document
alone does not implement or release these changes. Preserve existing offline
`--info` and `--labels` behavior. Older kits may ignore unknown configuration
keys; documentation must state the minimum feature version, and must not
promise protection when running an older release.

Validation for the eventual implementation must cover:

- Per-window, per-provider precedence; omitted versus explicit `off`; numeric
  boundaries and invalid values; no persistence of invocation overrides.
- Either-window threshold crossing, exact equality, independent resets,
  missing windows, invalid snapshots, freshness, finite retries, and manual
  continuation without automatic restart.
- Model-free startup queries; selected-account attribution across concurrent
  sessions and resumes; no reliance on context or raw-token percentages.
- Real native control through isolated local fixtures where available:
  interruption, TUI survival, subagents, queued work, tool-command behavior,
  monitor loss, and supported versus rejected modes and versions.
- Existing stdin/stdout, terminal, credential repair, signal, completion, and
  no-configuration compatibility contracts. Update `tests/COVERAGE.md`, run
  focused suites, the full suite, and `pre-commit run --all-files`.

Per-run token or dollar budgets, automatic account switching, automatic reset
resume, dynamic reserve adjustment, and provider-enforced reservations are
outside this first version.

## Evidence consulted

Sources were checked during the November 11, 2026 design discussion. Documented
interfaces establish capabilities, not proof of a working integrated guard.

- [Existing account metadata and usage limitations](../../account-launchers.md)
- [Existing configuration contract](../../configuration.md)
- [Codex App Server, including account quota reads and turn interruption](https://learn.chatgpt.com/docs/app-server)
- [Codex hook control and diagnostic model-request hooks](https://learn.chatgpt.com/docs/hooks)
- [Claude commands, including usage](https://code.claude.com/docs/en/commands)
- [Claude long-lived token authentication](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)
- [Claude hook control](https://code.claude.com/docs/en/hooks)
- [Claude status line data](https://code.claude.com/docs/en/statusline)
- [Claude Agent SDK experimental usage interfaces](https://github.com/anthropics/claude-agent-sdk-typescript/blob/main/CHANGELOG.md)
