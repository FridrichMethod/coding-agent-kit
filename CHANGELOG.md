# Changelog

## 0.8.0

Usage reserves: both launchers can keep a percentage of an account's 5-hour
and weekly limits unused, enforced by a new protected mode, and `claude-as`
can enroll accounts with Claude Code's own login. A minor release: it adds
launch flags, an own option, configuration tables and public paths, and
changes the help text. A launch with both reserves off, as by default, is
unchanged. A managed install takes it as a channel switch:
`"${XDG_DATA_HOME:-$HOME/.local/share}/coding-agent-kit/current/install.sh" --update --channel 0.8`.

Breaking changes:

- New public paths, which a copied install must include beside the 0.7
  ones: `lib/coding-agent-kit/reserve.py`, `gate.py`, `monitor.py`,
  `supervise.py`, `sandbox_linux.py` and `sandbox_macos.py`;
  `lib/codex-as/protect.py`, `gate.py` and `reader.py`; and
  `lib/claude-as/native.py`, `policy.py`, `bearer.py`, `provider.py` and
  `protect.py`. `codex-as` loads `reserve.py` and `protect.py`, and
  `claude-as` `reserve.py`, on every launch, so a copy without them prints
  `incomplete install at ROOT` for any launch; `claude-as` needs `native.py`
  once `<secrets_dir>/native-claude-accounts` exists (the first
  `--enroll-native` creates it), and a launch with a reserve needs the
  others and refuses the same way without them. Linked and managed installs
  and checkouts are unaffected, and no path moved.
- The configuration tables `[usage_reserve]` and `[claude_auth]` are read
  now. A 0.7 kit only warned about them as unknown keys; an invalid value
  in them (for example `five_hour = true`) now stops every launch, listing
  and completion with its error, as any other invalid key does.

Added:

- Usage reserves, per window and never combined: `--reserve-5h V` and
  `--reserve-weekly V` before `NAME`, in either order, each at most once,
  with the value as the next argument (`V` a number from 0 through 100, or
  `off`; no `=` form), for one launch; `[usage_reserve] five_hour, weekly`
  globally and `[usage_reserve.codex_accounts.NAME]` and
  `[usage_reserve.claude_accounts.NAME]` per account, in the configuration
  file. Each window resolves from the flag, then the account, then the
  global value, then off; an omitted value inherits and an explicit `off`
  overrides. Flags are never saved. 0 pauses at exhaustion and 100 never
  starts model work. A file meant for reserves warns
  `unknown key usage_reserve ignored` and protects nothing on an older kit.
- Protected mode, which a launch with a reserve on uses: the native's
  interactive TUI runs on a terminal of the launcher's own inside an OS
  boundary whose only intended network exit is a per-invocation request gate
  on a random loopback port (on Linux, Landlock limits the port number, not
  the address: the gate's port number is reachable at every address, remote
  ones included, and a process inside can accept outside connections on a
  port the system picks; on macOS only the gate's `localhost` port is
  reachable and every bind is denied). The native never runs outside the
  boundary: its version check, the pre-launch refresh and the session all
  run confined. Before the session starts, the native
  refreshes its credential behind a refresh gate that passes only the
  provider's sign-in host (and, for Codex, the workspace configuration
  bundle), and one model-free quota read of both windows of this account
  must pass. The gate admits a model request only with the account's
  credential, a reviewed model of its quota family and a fresh reading above
  both reserves, and reads quota about every 30 seconds (15 near a reserve).
  It connects through the proxy the native itself would use for HTTPS
  (`HTTPS_PROXY`, then, on macOS, the system HTTPS proxy and then
  `ALL_PROXY` for Codex, or `HTTP_PROXY` and then `ALL_PROXY` for Claude
  Code), captured once and never passed to the
  native; it goes direct only to a host that both natives' `NO_PROXY`
  readings exempt, and a proxy it cannot use (anything but a plain
  `http://` one) refuses the launch.
  It pauses, refusing new model requests and closing the admitted streams,
  when a window reaches its reserve, and also when it cannot tell: a reading
  older than 120 seconds, a missing or unattributed window, another account,
  a lost monitor or a stopped gate (restarted paused, at most 5 times in 10
  minutes). The native interface stays usable. Nothing resumes by itself:
  while paused the launcher says when the latest quota check passes, F12
  shows a one-use code, and typing it asks for a fresh check that must
  pass. At exit every process left inside the boundary is ended.
- Protected mode requires Linux on x86_64 with Landlock ABI 6 or newer and
  seccomp (not WSL), or macOS 27.2 (any 27.2.x update) on Apple silicon with
  `/usr/bin/sandbox-exec`, a root-owned `/bin/ps` and a passing startup
  self-test, an interactive terminal, and exactly the reviewed native
  release: codex-cli 0.167.2, or Claude Code 2.1.322 with a `native-login`
  account. Only these reviewed releases are accepted: an older release
  (macOS 15, for example) is refused as unreviewed, and a newer native or
  macOS release refuses protected launches until a kit patch reviews it.
  Anything else is refused before the session starts, with one line, exit 1:
  `codex-as: usage reserve protection refused: REASON. (--reserve-5h off --reserve-weekly off launches unprotected)`
  or `claude-as: protected mode refused: REASON.` The boundary is the
  kit's own code (Landlock and seccomp through `ctypes`, a Seatbelt
  profile); nothing is installed.
- Protected `codex-as` runs only the interactive TUI with an allowlist of
  options parsed as codex-cli 0.167.2 parses them; every other subcommand,
  `resume` and `fork` included, is refused, while `/new`, `/resume` and
  `/fork` inside the TUI stay protected. It refuses API-key and FedRAMP
  logins, unreviewed plans and models, profiles and overrides of the keys
  it sets, and, before any model request, accounts whose managed
  requirements forbid what it sets (application network restrictions,
  network controls, provider, residency, base URL, credential store or
  `sqlite_home` pins, among others), naming the requirement. The gate
  forwards the workspace configuration bundle; one that forbids protected
  mode pauses a running session until a later bundle lifts it, also across
  a restart of the request gate. Protected codex-as never runs the native
  outside the boundary: `codex --version` runs inside one, and the boundary
  writes only the account home, the shared session state and the release's
  databases in `<codex_home>`, never `packages/`, `config.toml`, hooks,
  MCP, skills, rules, agents, memories or the plugin staging in `.tmp`. A
  `codex` the boundary could replace, a writable or relative `PATH` entry
  before the `python3`, `codex` or `#!/usr/bin/env` interpreter it would
  shadow, a shared state entry in `<codex_home>` that is a link, an account
  without a saved login and missing native databases refuse. On macOS the
  gate uses the system HTTPS proxy Codex uses, and PAC or WPAD refuses; an
  empty `NO_PROXY` exempts nothing. Codex's limit
  flags (`limit_reached`, `allowed` false) are attributed to the windows at
  100%: an exhausted window whose reserve is off neither refuses nor
  pauses, and flags that no window accounts for refuse the start and pause
  a run. Concurrent protected launches of one account take turns at the
  pre-launch refresh. Codex, its tools and its quota readers keep the
  caller's umask, and `NAME update` and `completion` stay plain `codex`.
- Protected `claude-as` supports Pro and Max accounts only: Claude Code
  applies no organization policy while its requests go through the gate, so
  an account whose recorded subscription is not Pro or Max, and every
  account on a machine that pins the sign-in organization, is refused with a
  message that names it and says to launch without reserves. Print, resume,
  settings, remote and background forms and every command are refused;
  hooks, stdio MCP servers and plugins run inside the boundary. As in
  protected `codex-as`, the gate and the monitor run the base interpreter of
  the launcher's Python, and a launch is refused when that Python, the
  launcher's own `python3` or the kit lies inside a path the boundary can
  write (a kit checked out inside the project, or a workspace virtual
  environment first on `PATH`, for example); while the run lasts, the
  launcher runs commands by name only from the system directories.
  Protected claude-as never runs Claude Code outside the boundary: its
  version check runs confined, and a Claude Code (or its `#!` interpreter)
  that the session could replace refuses the launch. It refuses a launch
  when the `PATH` entry that supplies `dirname`, `readlink`, `python3`,
  `claude` or the command of its `#!/usr/bin/env` line, or an entry
  searched before it, is empty, relative or boundary-writable, or when that
  line sets `PATH`. After another account signs in, a protected Claude
  session stops reading the signed-out login's quota: a quota read stops
  before its usage request once the profile shows another account, and the
  passing-check line appears only once the enrolled account is back. Rows of
  credits, extra usage and other applications in the usage report are
  ignored, its `limits` list counts with the five-hour and weekly windows,
  and the refusal for a row the kit has not reviewed names the rows. An
  idle usage window (0% used, no reset time) counts as available. Local
  settings files, a native-login account's `.claude.json` and the
  directories above the parent of `<secrets_dir>` may be group-writable
  when the group is your user private group without an access ACL; other
  refusals of a file other users can write name it and suggest
  `chmod go-w`. A launch
  is refused when the account's token, after the pre-launch refresh,
  expires within fifteen minutes, and the start line names the time it
  expires, when the session needs a relaunch. The status line keeps working
  with `CODING_AGENT_KIT_CLAUDE_PID` set to the native's pid, and
  `CODING_AGENT_KIT_USAGE_FILE` is not passed.
- `claude-as --enroll-native NAME`: Claude Code's own login for an account,
  in its own configuration directory
  `<secrets_dir>/native-claude-accounts/NAME/config` with the registration
  `account.json` beside it. The first run prints the two commands to sign
  in (`CLAUDE_CONFIG_DIR=... claude auth login`, then `auth status`); the
  next shows the login and asks for confirmation. Select the account with
  `[claude_auth.accounts.NAME] mode = "native-login"` (the default mode is
  `"setup-token"`). Claude Code keeps and refreshes the credential; the
  launcher never reads, copies or stores it (only the protected-mode gate
  reads it, in memory), injects no token and inherits nothing of the daily
  `~/.claude`.
- Completion of the reserve flags, their values and the account after them,
  and of `claude-as --enroll-native NAME`, in Bash and zsh.
- Test suites for the new modules, each with its entry point in
  `tests/run.sh` and a pre-commit hook scoped to its sources:
  `tests/reserves.sh`, `reserve-completion.sh`, `sandbox.sh`, `gate.sh`,
  `supervise.sh`, `codex-reserve.sh` and `claude-reserve.sh`. Cases that need
  the boundary, a PTY or a reviewed native skip with the reason where the
  host lacks them. `tests/test-entrypoints.sh` also checks that every entry
  point is in the suite list of `tests/run.sh` and that every
  `tests/test_*.py` has an entry point.

Changed:

- Both help texts describe the reserve flags, and the `claude-as` help
  `--enroll-native`; the `codex-as` help says that the absent upper version
  limit holds for launches without reserves.
- Both launchers start `python3 -I -B -S`, so no site-packages or `.pth`
  file of the `python3` found on `PATH` (a virtual environment's included)
  runs in the launcher.
- Bash completion after `NAME` hands the native completion the command line
  from `NAME` on, with the cursor and the current and previous words adjusted
  to it.
- AGENTS.md records protected mode's scoped exceptions to the dependency
  rules: per-invocation gate and monitor processes, the gate as a protocol
  client, the kit's own boundary code, refusing instead of falling back, and
  accepting only the reviewed native and macOS releases.

Known limits: with live accounts, protected `codex-as` has been exercised
only on the refusal of a workspace account whose requirements set network
controls; protected sessions are covered by the suites with real Codex
against local fixtures. Protected mode confines the invocation, not later
use of what it wrote: everything a protected session could write (the
workspace, the natives' account homes and shared session stores, the
private temporary directory) is untrusted afterwards, since Git hooks,
direnv, editor settings or a sync root can run or carry it outside any
boundary.

## 0.7.7

- `codex-as` takeover recovery no longer stays pending when a thread's rollout is
  gone but its metadata is left, as after removing a rollout by hand or a native
  delete interrupted after removing it. The daemon still reads such a thread and
  lists it under its session, but refuses to archive or unarchive it, because
  both move the rollout. A record whose session is in that state is removed with
  a warning, as for a deleted session (`session UUID was deleted or has no saved
  history left`), after the spawned agents still listed under the session get
  their saved visibility back. A spawned agent in that state is left as it is,
  with a warning, and recovery completes. The deletion test still needs no
  rollout of the thread left in shared storage, now searched through linked
  folders too, and also accepts a daemon that reads the thread from metadata
  whose rollout path no longer exists. A refusal for any other reason, or a path
  that cannot be checked, still keeps the record.
- A pending takeover's message now includes its reason when codex-as wrote it,
  such as a refused daemon request or a folder of shared storage that cannot be
  searched, named by its path. Other errors still show no text of their own.

## 0.7.6

- `codex-as` takeover recovery no longer stays pending after a deletion. A saved
  agent deleted since the takeover was interrupted made recovery archive a thread
  that no longer exists. The daemon refused, so that session could not be resumed
  and the picker warned on every launch. Native delete removes a thread with its
  spawned subtree. Recovery now skips a saved agent that is gone from the
  session's subtree and passes the same deletion test as a session, below. An
  agent merely missing from the listing still keeps the record.
- A recovery record whose session was deleted is removed with a warning
  (`session UUID no longer exists`). This requires both that the root daemon
  refuses to read the session and that no rollout of it is left in shared
  storage, which native delete removes before it succeeds. A refused read alone,
  or a folder that cannot be searched, keeps the record.

## 0.7.5

- Fix `codex-as` session takeover with a root daemon from Codex 0.166.0 or
  newer. There, `thread/unarchive` also restores the session's archived spawned
  agents, even one archived on its own. Taking over a session with such an agent
  left that agent visible and the takeover pending. Every retry failed again, and
  only removing the recovery record let the session launch. Restore now archives
  or unarchives each spawned agent until the subtree matches what its record
  saved, under both the old and the new native behavior. Re-running the same
  command completes a takeover that an earlier release left pending. Recovery
  never archives an agent that the root daemon has loaded, together with any of
  its agents, because that would end its work. Such an agent stays visible with a
  warning.
- The real-Codex test suite accepts Codex 0.167.0's atomic `auth.json`
  replacement. Tokens still land in the linked secret, which may get a new inode
  but stays an owned 0600 file with one link; the native lock files
  (`auth.lock`, `codex-NAME.json.lock`) are allowed. Releases 0.163.0, 0.166.0
  and 0.167.0 pass, including takeover between a 0.163.0 and a 0.167.0 daemon or
  client.

## 0.7.4

- Show the tokens the session has used in the Claude status line, counted as
  Codex's `used-tokens` counts its thread: `13.7M used` is the input not read
  from the prompt cache plus the output of every response in the session's
  transcript and its subagents' transcripts. Claude Code reports no such
  total, so the helper reads the transcripts itself, incrementally, and keeps
  offsets and sums in one private cache file per session under
  `${XDG_CACHE_HOME:-~/.cache}/coding-agent-kit/statusline/`. A long session
  seen for the first time catches up 16 MiB per refresh and shows
  `13.7M+ used` meanwhile; any failure omits the segment.
- Show the 5-hour limit left beside the weekly one, and label both as Codex
  does: `5h 88% left | weekly 58% left` (0.7.3 printed `week 58% left`).
- Drop the context token counts that 0.7.3 added: the context segment is
  `ctx 38%` again. Token counts use Codex's compact form (`1.23K`, `13.7M`).

## 0.7.3

- Show the weekly rate limit left in the Claude status line, as Codex does:
  `week N% left` is 100 minus the rounded `rate_limits.seven_day` usage, so
  `week 42%` becomes `week 58% left`. Its colour still follows the share used
  (yellow at 70%, red at 90%), and absent or invalid windows are still omitted.
- Show the tokens in the context window and its size beside the context share:
  `ctx 75k/200k 38%`, from Claude Code's `context_window.total_input_tokens`
  and `context_window_size`. The share alone is shown when either count is
  missing or invalid, and `ctx ?` before the first response as before. Claude
  Code reports no running session total, so none is shown.

## 0.7.2

- Add optional semantic ANSI colours to the Claude status line using the existing
  `CODING_AGENT_KIT_COLOR=auto|always|never` policy. Captured output stays plain in
  `auto`; a non-empty `NO_COLOR` or `TERM=dumb` disables colours even in `always`.
  Context and weekly usage turn yellow at 70% and red at 90%. Displayed text and
  the default plain formatter output are unchanged.

## 0.7.1

- Show Claude Code's native seven-day rate-limit usage as `week N%` in the
  status line, alongside the existing context usage and estimated session cost.
  Missing or invalid weekly windows are omitted; valid zero usage is displayed.
  The helper reuses its existing window validation and makes no network calls.

## 0.7.0

Shorter launcher options and a version, and the kit becomes Unix-only. A
minor release: it adds options and changes the help text and a hint, which
are public, and it removes `install.ps1`, which breaks native Windows only.
Every 0.6 spelling still works. A managed install takes it as a channel
switch: `"${XDG_DATA_HOME:-$HOME/.local/share}/coding-agent-kit/current/install.sh" --update --channel 0.7`.

Breaking changes:

- Native Windows is no longer supported: `install.ps1`,
  `tests/managed-install.ps1`, `tests/run.ps1` and the Windows CI job are
  removed, and `tests/contract.sh` no longer pins `install.ps1`. There the
  kit installed only the three Claude Code helpers, with no account
  switching. v0.6.x is the last line with `install.ps1`: its installs keep
  working, frozen at their release, and
  `pwsh -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\coding-agent-kit\install.ps1" -Uninstall -Purge`
  removes one. Its `-Update -Channel 0.7` fails, since no 0.7 release has
  an `install.ps1`. Linux and macOS, WSL included, keep `install.sh`, its
  managed root and its commands unchanged.
- The bootstrap URL of `install.ps1` on `main`,
  `https://raw.githubusercontent.com/FridrichMethod/coding-agent-kit/main/install.ps1`,
  is gone for every channel, 0.6 included, from the commit on `main` that
  removed the file, before any 0.7 tag: a new 0.6 install, or a
  `-Reinstall` repair from a fresh download, as the 0.6 README shows them,
  fails with HTTP 404. Download the file from the `v0.6.0` tag instead,
  `https://raw.githubusercontent.com/FridrichMethod/coding-agent-kit/v0.6.0/install.ps1`,
  and run it as before with `-Channel 0.6`; it installs the newest 0.6
  patch. The channel is needed once a 0.7 tag exists, because the default
  is the newest release's line, which has no `install.ps1`. A script that downloads `install.ps1`
  from `main` fails as soon as `main` has this change, so point it at that
  tag or drop the Windows install first.

Added:

- Shorter names and short forms of the launchers' own options, still only
  as the first argument, by exact match and with the same arguments: `-h`
  for `--help`, `-l` and `--list` for `--list-accounts`, `--labels` for
  `--account-labels`, `-i NAME` and `--info NAME` for
  `--account-info NAME`, and `claude-as --label NAME [LABEL]` for
  `--set-label`. The older spellings stay accepted for good. No short form
  can collide with a native option: an account name never starts with
  `-`, and everything after `NAME` goes to the native CLI as before.
- `--version`, also `-V` and `-v`, as the only argument prints the kit's
  release and how it is installed on stdout, never styled:
  `coding-agent-kit X.Y.Z (FORM)`. FORM is `managed vX.Y.Z, channel X.Y`
  for a release of a managed root, judged from the directory the launcher
  resolves to and the root's `channel` file (the channel is left out when
  that is missing or malformed), `checkout ROOT` when `ROOT/.git` exists,
  `files ROOT` otherwise, or `unknown`. It is plain shell like `--help`:
  no `python3`, no `git`, no native CLI and no write. `NAME --version`
  still prints the native version. The release is a literal in each
  launcher, `_codex_as_version` and `_claude_as_version`; a test, run by
  the new `release-version` hook whenever `CHANGELOG.md` or a launcher
  changes, keeps both equal to the first heading of this file. The same
  test keeps a release tag equal to them:
  `KIT_RELEASE_TAG=vX.Y.Z tests/config.sh -k ReleaseVersion` checks a tag
  before `git tag vX.Y.Z` puts it on HEAD, against the `CHANGELOG.md` and
  both launchers committed in HEAD, so a bump that is not committed yet
  fails it. CI, which now also runs on pushed `v*` tags, checks each
  `vX.Y.Z` tag on the commit against its `CHANGELOG.md`; only that run
  (`GITHUB_REF_TYPE=tag`) checks tags already on HEAD, so a wrong tag
  never refuses the commit of the patch that fixes it.
- Completion of the own options: a first argument that starts with `-`
  offers `--help`, `--version`, `--list`, `--info`, `--labels`,
  `--show-config` and, for `claude-as`, `--label`. After `-i`, `--info` or
  `--account-info`, and `claude-as --label` or `--set-label`, the next
  argument completes account names, in zsh in the group `Codex accounts`
  or `Claude accounts`, so a preview keyed on those groups applies. The
  completions keep calling `--list-accounts` and `--account-labels`, so
  they also work against an older launcher.
- A dash word in the first argument that is not an own option is refused
  with a hint,
  `codex-as: -c is not a codex-as option; native options go after NAME (codex-as NAME -c ...)`,
  with the exit status of an invalid account name as before (2 for
  `codex-as`, 1 for `claude-as`); nothing starts. The hint echoes only
  the part before the first `=`, since a value or a secret may follow it.
  A word with characters other than ASCII letters, digits and
  `_=.,:/@+-`, or whose part before `=` holds `sk-` in any case or names
  an own option, keeps the plain invalid-name message, so it is never
  echoed.

Changed:

- The help lists the new names, under `Own options, only as the first
  argument`, and says that `NAME --version` prints the native version.
- `-h`, `--version`, `-V` and `-v` as the first argument were invalid
  account names; now they print the help or the kit's version. With more
  arguments a version flag is still an invalid account name, while `-h`,
  like `--help`, ignores them.
- The `--info` line of a Claude account without a label reads
  `not set (claude-as --label NAME LABEL)`, and the enrolment messages
  about a label point at `claude-as --label`.
- After an own option and its arguments the completions offer nothing,
  where they offered the native arguments before.

## 0.6.0

The kit installs and updates itself. A minor release: it adds the managed
install with its commands, paths and exit code 75, `install.ps1`, and the
environment variable `CODING_AGENT_KIT_REPO_URL`, and an `install.sh` with
no checkout next to it, as when piped to `sh`, now installs instead of
refusing. A checkout's `install.sh` links that checkout as in 0.5.0.

Added:

- A managed install that follows a channel, a `MAJOR.MINOR` line:
  `curl -fsSL https://raw.githubusercontent.com/FridrichMethod/coding-agent-kit/main/install.sh | sh -s -- --channel 0.6`
  installs the highest `v0.6.PATCH` release, and
  `"${XDG_DATA_HOME:-$HOME/.local/share}/coding-agent-kit/current/install.sh" --update`
  later moves to a newer patch of the remembered channel only, never to
  another minor line; `--uninstall` removes its links and `--purge` also
  the managed root. These commands are the stable interface for repositories
  that install the kit, so they no longer need to carry it as a submodule.
- `install.sh` runs as a managed install when no checkout is next to it,
  with `--managed`, `--channel MAJOR.MINOR`, `--update` or `--purge`, and
  always in a release of its managed root,
  `${XDG_DATA_HOME:-~/.local/share}/coding-agent-kit/`: `repo.git`, a bare
  clone; `releases/vX.Y.Z/`, exported release trees, the current one and the
  one before it; `current`, a link to one of them, replaced by a rename;
  `channel` and `prefix`, the remembered channel and `--prefix`; and
  `.lock`. It links `PREFIX/bin/{codex-as,claude-as}`, the bash and zsh
  completions and `~/.claude/coding-agent-kit/{statusline,notify,check-git-hooks}.cjs`,
  the home-relative helper paths, to `current/`, with the ownership and
  Stow-folding refusals of a checkout install, and prints the home-relative
  helper commands for `settings.json` when it places a link.
- The channel: `--channel` picks the highest `vMAJOR.MINOR.PATCH` tag and is
  remembered; without it a first install takes the newest release's line;
  a different `--channel` switches, also to an older line. `0.6` is the
  first channel with managed installs; `--channel` refuses older ones.
- A run lists the tags with `git ls-remote`, exports the release it chose
  and hands over to that release's own `install.sh --activate`, which checks
  every destination before anything changes, renames `current` over to its
  release, keeps the previous release, removes older ones and places the
  links that are not yet right. Once installed, a bootstrap or checkout
  `install.sh` hands every run to `current/install.sh`, so only released
  code changes an install. A launcher resolves to its physical release, so
  a switch leaves running sessions on the release they started with.
- A run with nothing to change writes nothing and prints one line,
  `coding-agent-kit: vX.Y.Z is already current (channel X.Y).`
- The lock: one run at a time holds `flock` on `.lock`; another exits 75
  with nothing changed. Where the file system has no `flock` (some NFS and
  Lustre mounts), the lock is the directory `.lock.d` with its host, pid
  and time; one left behind counts as gone when its pid is gone on its host
  or after an hour.
- Every network git call is bounded: a transfer below 1000 bytes/s for 60 s
  fails, ssh gives up after 30 s without a connection or a minute without an
  answer unless git is told how to run ssh, and each call ends after 300 s
  where `timeout` exists, so a stalled repository cannot keep the lock.
- `--reinstall` (`-Reinstall`) repairs a broken installed release: it runs
  without handing over to `current/install.sh`, exports the newest patch of
  the channel again from a fresh clone and hands that release's
  `--activate` the switch.
- `install.ps1`, the managed install on native Windows, where only the
  helpers run: the same channel, update, lock and exit-code rules with
  `-Channel`, `-Update`, `-Uninstall` and `-Purge` (also spelled
  `--channel`, `--update`, `--uninstall` and `--purge`), a managed root in
  `$env:LOCALAPPDATA\coding-agent-kit\` whose `current` is a file naming the
  release and whose `install.ps1` is the current release's copy and the
  entry point for `-Update`, and copies of the three helpers in
  `$HOME\.claude\coding-agent-kit\`. A copy there is its own when it equals
  the helper of a release in the managed root. It stages all copies before
  renaming them, retries a rename for 1.5 s while another process has the
  file open, and reports exit 75 only for a sharing or lock violation that
  lasts a second. It needs PowerShell 7 and Git.
- `CODING_AGENT_KIT_REPO_URL` replaces the repository a managed install
  clones and lists, otherwise the one its managed root was cloned from.
- `tests/managed-install.sh` and `tests/managed-install.ps1` drive both
  managed installs against a local repository with release tags, with no
  network: bootstrap, channels, updates, the lock, a running launcher across
  a switch, refusals, uninstall and purge.

Changed:

- `install.sh` piped to `sh`, or run from a directory without the kit, is
  the managed bootstrap; 0.5.0 refused it as `not a complete
  coding-agent-kit checkout`. Its work now starts on its last line, so a
  truncated download runs nothing.
- `install.sh --help` adds the managed forms after its unchanged first line.
- Every test entry point also unsets `CODING_AGENT_KIT_REPO_URL`.
- `tests/contract.sh` also pins `install.ps1` and its `#Requires -Version
  7.0` line.
- `tests/run.ps1` also runs `tests/managed-install.ps1` and needs `pwsh`.

## 0.5.0

The kit stops naming its consumers and narrows its public contract. A minor
release: the CI job names and the two helper test files leave the contract.
No behavior change in the launchers, libraries, completions, helpers and
`install.sh`, which behave as in 0.4.0, and no file moved.

Breaking changes:

- The CI job names `Linux behavior and lint`, `macOS system Bash behavior`
  and `Windows native PowerShell behavior` are no longer part of the public
  contract, and `tests/contract.sh` no longer checks them. A consumer that
  gates on them checks them itself.
- `tests/claude-customizations.cjs` and `tests/fixtures/claude-settings.json`
  are no longer public paths, and `tests/contract.sh` no longer pins them.
  They are the kit's own tests; a consumer tests its own Claude settings
  itself.

Changed:

- `tests/fixtures/claude-settings.json` holds only the Claude Code wiring of
  the three helpers instead of a byte copy of one consumer's settings. Each
  command has the home-relative form
  `node -e "require(require('os').homedir() + '/.claude/coding-agent-kit/NAME.cjs').main()"`,
  which README.md now describes for a `settings.json` shared by machines with
  different home directories, with each helper's exported `main`.
- The location and `install.sh` refusal tests that modeled one consumer's
  layout use a generic link farm, `HOME/pkgs/coding-agent-kit/` with the kit
  checked out at its root, with the same assertions.
- The colour tests no longer check that the colour variable 0.2.0 replaced
  with `CODING_AGENT_KIT_COLOR` is ignored.
- AGENTS.md and README.md describe the public contract without naming a
  consumer: the linked paths, the help text, the `--account-labels` and
  `--account-info` formats and the rest of the contract change only in a
  minor release, and since consumers may track patch tags automatically, a
  patch never changes one.

## 0.4.0

Claude usage is recorded at last. A minor release: `claude-as` passes its child
one more environment variable.

Fixed:

- `helpers/statusline.cjs` recorded no Claude usage at all: it required
  `CLAUDE_CODE_OAUTH_TOKEN` to hash to `CODING_AGENT_KIT_TOKEN_ID`, and Claude
  Code (2.1.307 checked) passes that token to none of the commands it starts,
  the status line included.

Changed:

- `claude-as` also passes `CODING_AGENT_KIT_CLAUDE_PID`, its own pid, which
  stays the session's because it execs claude, and drops an inherited value
  first. The status line records only when the claude with that pid runs it,
  directly or behind one shell; a claude started inside the session has a
  pid of its own and records nothing. A visible token must still hash to the
  identifier.
- Every test entry point also unsets `CODING_AGENT_KIT_CLAUDE_PID`.
- `ci.yml` also runs on pushes to `ci/**` branches, and AGENTS.md lets agents
  land a change that way, with no pull request, once every check is green.

## 0.3.0

Account details for previews and people: each launcher keeps a small,
offline metadata cache per account, and new queries print it without opening
a credential. A minor release: it adds environment variables, state files,
an enrolment prompt, help lines and queries.

Added:

- `codex-as --account-info NAME` and `claude-as --account-info NAME` print an
  account's last-seen details: the Codex email and plan, the Claude label, and
  the 5-hour and weekly usage with their reset times and the age of the
  snapshot, or `not recorded yet`. `--account-labels` prints `NAME<TAB>LABEL`
  lines (the email for Codex, the label for Claude). Both are metadata only:
  no directory is created, no preflight or native CLI runs, no credential is
  opened, and only allow-listed fields are printed, with control characters
  removed.
- The cache lives in `${XDG_STATE_HOME:-~/.local/state}/coding-agent-kit/accounts/`
  (0700): `codex-NAME.json` (written by `codex-as`), `claude-NAME.json`
  (`claude-as`) and `claude-NAME.usage.json` (`helpers/statusline.cjs`), each
  0600, replaced atomically and only when it changes. No token is stored.
  `Config.state_dir` in `lib/coding-agent-kit/config.py` names it; it is not a
  configuration key.
- `codex-as` records the email and plan from the account's `id_token` at each
  launch and, after each run, the last rate-limit snapshot of the run's own
  rollouts (the thread it resumes, never a fork's source, or one this account created),
  best effort and without changing the exit code. New module
  `lib/codex-as/usage.py`.
- `claude-as` asks for an optional label when it creates an account, after
  the confirmation and before any token exists, and saves it after the token;
  control characters, more than 100 characters and anything containing
  `sk-ant-` are refused. `claude-as --set-label NAME [LABEL]` sets, asks for
  or (empty) clears the label of an existing account.
- `claude-as` passes its child `CODING_AGENT_KIT_USAGE_FILE` and
  `CODING_AGENT_KIT_TOKEN_ID` (the first 16 hex digits of the token's
  SHA-256), dropping inherited values. `helpers/statusline.cjs` exports
  `recordUsage` and records the session's `rate_limits` there only while
  `CLAUDE_CODE_OAUTH_TOKEN` hashes to that identifier, into an owned 0700
  directory, never on Windows; its printed status line is unchanged.
- New shared module `lib/coding-agent-kit/accounts.py`, the cache.
- A configuration whose `secrets_dir` is the cache directory is refused,
  since the cache's `codex-NAME.json` would replace the credential of that
  name; a cache directory that is `secrets_dir` through a link is never read
  or written.

Changed:

- The zsh completions run `--account-labels` and show each account's email
  or label as its description; Bash still completes bare names.
- Both `--help` texts gain two lines for the new queries.
- `bin/codex-as` accepts `--account-labels` alone and `--account-info NAME`;
  other arities stay invalid account names.
- Every test entry point also unsets `CODING_AGENT_KIT_USAGE_FILE`,
  `CODING_AGENT_KIT_TOKEN_ID` and `XDG_STATE_HOME`.
- The tests that model a dotfiles host follow its layout from October 2026:
  the kit checked out at the root of dotfiles' own `coding-agent-kit` package.

Known limits: Codex usage comes from Codex's internal rollout format, misses
a session switched inside Codex, skips compressed rollouts, can miss this
account's new session past 64 other accounts' rollouts of the run, and can
count a thread this account created that another account resumed during the
run.
Claude usage exists only for Pro and Max subscribers, after the first API
response, in sessions that render a status line.

## 0.2.0

coding-agent-kit becomes a standalone install. The launchers find their own
files, an optional configuration file replaces the hard-coded paths and
executables, and `install.sh` links the commands. Without a configuration file
`codex-as` and `claude-as` pass the same arguments, environment, files and
links to Codex and Claude Code as in 0.1.0.

Added:

- `install.sh [--prefix DIR] [--uninstall]` links `bin/codex-as` and
  `bin/claude-as` into `DIR/bin` (default `~/.local`), the Bash completions into
  `${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-~/.local/share}/bash-completion}/completions`
  and the zsh completions into `DIR/share/zsh/site-functions`. It replaces or
  removes only its own links, whose text starts with the checkout's physical
  path as `pwd -P` prints it (also after their target moved within it); a
  link made through another spelling of that path is not its own. Before
  changing anything it refuses every other existing path, including relative
  links such as Stow's, any destination reached through a relative directory
  link below `$HOME` or the parent of `DIR` (a Stow fold or a hand-made
  relative alias; absolute aliases, `$HOME` itself and the links above it
  are accepted), and any destination whose nearest existing parent is not a
  writable directory. `--uninstall` changes nothing when one of its own links
  is in a directory it cannot write or a directory on the way cannot be
  searched. It prints the zsh `fpath` line and the Claude Code helper
  commands, and never writes a configuration file.
- An optional configuration file, `CODING_AGENT_KIT_CONFIG` or
  `${XDG_CONFIG_HOME:-~/.config}/coding-agent-kit/config.toml`, with the keys
  `codex`, `claude`, `codex_home` (default `~/.codex`) and `secrets_dir`
  (default `~/.secrets`). It must be owned by you or root and not writable by
  others (group-writable only for your user private group, which is your
  login group in the password database, also in a `newgrp` or `sg` shell,
  and then without an access ACL; on NFSv4 every group-writable file or
  directory counts as having one), and so must the directories on its way,
  up to `$HOME` (or its resolved path) or `/`; a symlink on its way must be
  yours or root's. Values with control characters (C0, DEL and C1) are
  refused; unknown keys only warn. See `docs/configuration.md`.
- `codex-as --show-config` and `claude-as --show-config` print the effective
  values and where they came from. `codex-as --list-accounts` prints the
  account names, like `claude-as --list-accounts`. Neither starts a native CLI.
- The launchers resolve their own symlinks (at most 40) to the kit root and run
  `lib/<tool>/launch.py` there, so linked, copied and Stow-managed installs all
  work. An incomplete install prints `incomplete install at ROOT` instead of a
  traceback.
- Tests: `tests/test_config.py` (runner `tests/config.sh`) for the loader and
  the launchers' location, `tests/standalone-e2e.sh` for an install from a
  plain clone, and `tests/contract.sh` for the public contract.

Changed:

- `--help` names the configurable `<secrets_dir>` and `<codex_home>` with their
  defaults, adds a `Configuration:` line, and ends with
  `Details and manual repair: ROOT/docs/account-launchers.md`, or with the
  GitHub URL of that file when the launcher has no kit around it.
- With a configured `codex_home`, root help, version, `update` and
  `completion` run with `CODEX_HOME=<codex_home>`, and an inherited `CODEX_HOME`
  equal to it is accepted.
- `codex-as` checks the parents of `codex_home` and of `secrets_dir`,
  `claude-as` the parent of `secrets_dir`. Both are `$HOME` by default, checked
  as before. A missing parent, `$HOME` included, or one below a regular file,
  is now named (`PARENT does not exist; ...`) instead of the generic
  `missing, unreadable or malformed ...` message.
- The configuration location is looked up even without a file. A missing file,
  or a path component that is a regular file, means the defaults; any other
  failure to open it (an unreadable file, an unsearchable `~/.config` or
  `$XDG_CONFIG_HOME`, a symlink loop) is an error, so it stops every launch,
  `--list-accounts` and completion, where 0.1.0 never looked there.
- `claude-as` catches the same errors at the top level as `codex-as`, so none
  ends in a traceback; for example `claude-as NAME --settings ~unknown/x` now
  prints the one-line `missing, unreadable or malformed ...` message.
- An unset or relative `HOME` fails with `TOOL: HOME must be set to an
  absolute path.`, for `--list-accounts` too.
- `claude-as` with a missing `lib/claude-as/launch.py` prints the
  incomplete-install message and exits 1 (it exited 2 with Python's error).
- `tests/run.sh --ci` requires zsh, and the Linux CI job installs it and GNU
  Stow and fails if the folded Stow cases of `tests/standalone-e2e.sh` did
  not run. The Windows job no longer sets up Python.
- `tests/claude-customizations.cjs` takes the helper directory from each
  fixture command instead of assuming `~/.claude/dotfiles`.

Breaking changes:

- Every launcher, library, completion and helper file moved; see the table
  below. A consumer that links files must use the new paths.
- `DOTFILES_COLOR` is no longer read. Use
  `CODING_AGENT_KIT_COLOR=auto|always|never`; `NO_COLOR` and `TERM=dumb` work
  as before.
- The launchers no longer run `~/.local/lib/<tool>/`. They run `lib/<tool>/`
  beside their own `bin/` after resolving symlinks, so a copied launcher needs
  `PREFIX/lib/{codex-as,claude-as,coding-agent-kit}/` beside `PREFIX/bin/`.
- The `--help` pointer `~/dotfiles/docs/account-launchers.md` is gone (see
  Changed).
- The completions get account names from `codex-as --list-accounts` and
  `claude-as --list-accounts`, so the launchers must be on `PATH`. The old
  listing `check.py accounts` still works for shells opened before an update.
- `codex-as --list-accounts` and `codex-as --show-config` are no longer
  refused as invalid account names, and `claude-as --show-config` is no longer
  taken as an account name.
- `claude-as` refuses account names that start with `-`, as `codex-as` does:
  `claude-as -x`, `claude-as -h` or `claude-as --version` prints `claude-as:
  invalid account name (ASCII letters, digits, _ or -; no leading -).` and
  exits 1 instead of offering to enrol an account of that name. Every invalid
  name now gets this message, which `codex-as` already printed.
- The AI config sync engine moved back to dotfiles: `lib/config_sync.py`, the
  four `*-sync` launchers, `lib/sync-runtime.sh`, `setup-sync.*`,
  `requirements-sync.txt`, their suites and fixtures and `docs/config-sync.md`
  are removed. The kit needs no `tomlkit`, only the Python 3.11+ standard
  library and Node.js 22+.
- The test entry points unset `CODING_AGENT_KIT_CONFIG`,
  `CODING_AGENT_KIT_COLOR`, `XDG_CONFIG_HOME` and `NO_COLOR` instead of the
  dotfiles knobs, and the knob-hygiene check covers `CODING_AGENT_KIT_*` names.

Paths, 0.1.0 to 0.2.0:

| 0.1.0 | 0.2.0 |
| --- | --- |
| `common/codex/.local/bin/codex-as` | `bin/codex-as` |
| `common/claude/.local/bin/claude-as` | `bin/claude-as` |
| `common/codex/.local/lib/codex-as/launch.py` | `lib/codex-as/launch.py` |
| `common/codex/.local/lib/codex-as/check.py` | `lib/codex-as/check.py` |
| `common/codex/.local/lib/codex-as/layout.py` | `lib/codex-as/layout.py` |
| `common/codex/.local/lib/codex-as/handoff.py` | `lib/codex-as/handoff.py` |
| `common/claude/.local/lib/claude-as/launch.py` | `lib/claude-as/launch.py` |
| `common/codex/.local/share/bash-completion/completions/codex-as` | `completions/bash/codex-as` |
| `common/claude/.local/share/bash-completion/completions/claude-as` | `completions/bash/claude-as` |
| `common/codex/.oh-my-zsh/custom/completions/_codex-as` | `completions/zsh/_codex-as` |
| `common/claude/.oh-my-zsh/custom/completions/_claude-as` | `completions/zsh/_claude-as` |
| `common/claude/.claude/dotfiles/statusline.cjs` | `helpers/statusline.cjs` |
| `common/claude/.claude/dotfiles/notify.cjs` | `helpers/notify.cjs` |
| `common/claude/.claude/dotfiles/check-git-hooks.cjs` | `helpers/check-git-hooks.cjs` |
| `common/claude/.local/bin/claude-settings-sync` | removed |
| `common/codex/.local/bin/codex-config-sync` | removed |
| `common/codex/.local/bin/codex-instructions-sync` | removed |
| `common/codex/.local/bin/codex-rules-sync` | removed |
| `lib/config_sync.py` | removed |
| `lib/sync-runtime.sh` | removed |
| `setup-sync.sh`, `setup-sync.ps1` | removed |
| `requirements-sync.txt` | removed |
| `docs/config-sync.md` | removed |
| `tests/test_config_sync.py`, `tests/config-sync.sh`, `tests/codex-config-sync.sh`, `tests/ai-config-sync.sh`, `tests/inspect_codex_hooks.py` | removed |
| `tests/fixtures/codex-config.toml`, `tests/fixtures/portable.rules` | removed |
| (new) | `lib/coding-agent-kit/config.py`, `install.sh`, `docs/configuration.md`, `tests/config.sh`, `tests/test_config.py`, `tests/contract.sh`, `tests/standalone-e2e.sh` |

Every other 0.1.0 path is unchanged: `.gitattributes`,
`.github/workflows/ci.yml`, `.gitignore`, `.gitleaks.toml`,
`.pre-commit-config.yaml`, `AGENTS.md`, `CHANGELOG.md`, `CLAUDE.md`,
`LICENSE`, `README.md`, `docs/account-launchers.md`, `docs/codex-as.md`,
`tests/COVERAGE.md`, `tests/account-enrollment.sh`,
`tests/claude-customizations.cjs`, `tests/codex-as.sh`,
`tests/fixtures/bash_aliases`, `tests/fixtures/claude-settings.json`,
`tests/fixtures/zsh_aliases`, `tests/run.ps1`, `tests/run.sh`,
`tests/test-entrypoints.sh`, `tests/test_account_enrollment.py`,
`tests/test_codex_as.py` and `tests/test_codex_sharing_native.py`.

## 0.1.0

- Extracted from dotfiles 6fe50bc, no behavior change. The `codex-as` and
  `claude-as` launchers, the AI config sync engine `lib/config_sync.py` and
  the Claude status line, notification and Git-hook helpers keep their
  dotfiles paths and bytes. The `*-sync` launchers, `lib/sync-runtime.sh`,
  `setup-sync.*` and `requirements-sync.txt` are reference twins of the
  dotfiles binding: byte-identical, except that the dotfiles
  `lib/sync-runtime.sh` and `setup-sync.*` name the engine inside the
  submodule. The imported history keeps the commits that touched these files.
- The moved suites read frozen copies of the dotfiles settings in
  `tests/fixtures/`; the tests of dotfiles content stay in dotfiles.
  `docs/config-sync.md` keeps the helper half of the dotfiles guide.
