# Codex/Claude account launchers

`install.sh` links both commands into `~/.local/bin` (or `PREFIX/bin`); no sourcing is required. Each launcher follows its own symlinks to the kit and runs its Python standard-library modules from the kit's `lib/`. They require Python 3.11+, install no dependencies, implement no OAuth flow, and run no background service.

Paths below use the configured values: `<codex_home>` (default `~/.codex`) and `<secrets_dir>` (default `~/.secrets`). `codex-as --show-config` and `claude-as --show-config` print the values in effect; [configuration](configuration.md) describes the file that changes them.

## Daily use and adding accounts

```sh
codex-as ayting
codex-as work resume --last
codex-as ayting exec --json "Check the tests"
claude-as work
claude-as work -p "Check the tests"
codex-as --reserve-5h 10 --reserve-weekly 40 work
claude-as --reserve-weekly 30 personal
```

Valid local credentials launch immediately. A missing account lists existing accounts and asks `[y/N]`, defaulting to no, without correcting similar names. After confirmation, successful native login, and saving, the original command continues. Corrupt files, broken links, and permission errors are not treated as missing accounts and are never overwritten for a fresh login.

- **Codex**: runs `codex -c 'cli_auth_credentials_store="file"' login --device-auth` in a private, empty temporary `CODEX_HOME`. Device login must be enabled in the account's Security settings or by the workspace admin. Failure, cancellation, or timeout does not save credentials or automatically switch to a browser flow. Use the official URL/code to complete authorization initiated on this machine. [Official authentication documentation](https://learn.chatgpt.com/docs/auth)
- **Claude**: runs `claude setup-token` in a temporary `CLAUDE_CONFIG_DIR`; there is no `generate` subcommand or equivalent device-code flow. Automatic browser opening is temporarily disabled, and the native manual authorization link is displayed. If the page returns a code, paste the complete `code#state` and press Enter. Browser cookies are not isolated by the CLI configuration directory; choose the intended account in the browser.
- Claude has no machine-readable token export interface. The local `2.1.296` adapter captures the complete success section, deduplicates it, and saves only after a successful exit, without replaying raw output. It streams ANSI/OSC sequences, chunks, and line breaks with bounded buffering. An unknown version, capture error, or failure prompts separately to run the official command and paste without echo; the default is no. **In the manual fallback, the official command displays the token; the wrapper's paste input does not echo it.**

Claude setup-token tokens typically last one year and do not renew automatically. They support model requests, with no guarantee for Remote Control or claude.ai connectors; local MCP remains usable. [Official documentation](https://code.claude.com/docs/en/authentication)

Confirmation and login I/O use a separate `/dev/tty`, preserving the final stdin/stdout/stderr, cwd, arguments, and exit code. A missing account is refused without a controlling terminal; there is no `--yes`. The wrapper's [own options](#own-options), and `codex-as NAME` with a root help or version flag, `help`, `update` or `completion`, run without the account and never start a login.

## Own options

Each launcher has a few options of its own. They count only as the first argument, by exact match and with exactly the arguments shown; everything after `NAME` goes to the native CLI. An account name never starts with `-`, so an own option never meets a native one.

| Option | Alias | Output |
| --- | --- | --- |
| `-h`, `--help` | | the help, also for a run without arguments (which exits 2); later arguments are ignored |
| `-V`, `-v`, `--version` | | the kit's release and install form; `NAME --version` is the native CLI's |
| `-l`, `--list` | `--list-accounts` | the account names, one per line |
| `--labels` | `--account-labels` | `NAME<TAB>LABEL` lines, the descriptions of the zsh completions |
| `-i NAME`, `--info NAME` | `--account-info NAME` | the last-seen details of one account ([account metadata](#account-metadata)) |
| `--show-config` | | the configuration in effect ([configuration](configuration.md)) |
| `claude-as --label NAME [LABEL]` | `claude-as --set-label NAME [LABEL]` | sets a Claude label, asks for it, or clears it with an empty one |

The aliases are accepted too; the help lists only the primary spellings. `-h` and `--help` ignore any later arguments; any other own option with arguments beyond its own is an invalid account name. Any other dash word in the first argument is refused with a hint and the exit status of an invalid account name (2 for `codex-as`, 1 for `claude-as`), and nothing starts or is written. The hint echoes only the part before the first `=`, since a value or a secret may follow it; a word with characters other than ASCII letters, digits and `_=.,:/@+-`, or whose part before `=` holds `sk-` in any case or names an own option, gets the plain invalid-name message instead, so it is never echoed:

```text
$ codex-as -c model=o3
codex-as: -c is not a codex-as option; native options go after NAME (codex-as NAME -c ...)
```

`--reserve-5h V` and `--reserve-weekly V` are launch flags, not queries: they set this launch's [usage reserves](#usage-reserves) and come before `NAME`, in either order, each at most once, with the value as the next argument (`V` is a number from 0 through 100, or `off`). They are exact words: `--reserve-5h=10` is refused, and after `NAME` both go to the native CLI like any other argument. Their errors exit 1 and start nothing:

```text
codex-as: --reserve-5h takes a number from 0 through 100, or off, as the next argument.
codex-as: --reserve-5h is given twice.
codex-as: --reserve-5h=VALUE is not accepted; write --reserve-5h VALUE.
codex-as: an account name must follow --reserve-5h and --reserve-weekly.
```

`--version` is plain shell, like the help: it needs no `python3` or `git`, starts no native CLI, writes nothing and is never styled. It prints `coding-agent-kit X.Y.Z (FORM)` on stdout, where `X.Y.Z` is the release written into the launcher and `FORM` comes from the directory the launcher resolves to after its links:

| FORM | When |
| --- | --- |
| `managed vX.Y.Z, channel X.Y` | the directory is `vX.Y.Z` in a `releases` directory, as in a managed install; the channel comes from the managed root's `channel` file and is left out when that is missing or malformed |
| `checkout ROOT` | `ROOT/.git` exists: a clone, a worktree or a submodule |
| `files ROOT` | anything else, such as copied files |
| `unknown` | the launcher cannot resolve its own location |

A session started before a managed update keeps its release, and that release's launchers keep printing it. A checkout between tags prints the release its launchers name, the first heading of its CHANGELOG, and no commit.

```text
$ codex-as --version
coding-agent-kit 0.7.0 (managed v0.7.0, channel 0.7)
```

## Usage reserves

A usage reserve keeps part of an account's subscription allowance unused. Each of the two windows, the 5-hour and the weekly limit, has its own reserve, a percentage of the window that should stay left; they are never combined. Set them for one launch with `--reserve-5h V` and `--reserve-weekly V` before `NAME`, or for every launch, globally or per account, in the [configuration file](configuration.md#usage-reserves). For each window the flag wins, then the account's table, then the global value; the default is off. A flag changes only that launch.

```sh
codex-as --reserve-5h 10 --reserve-weekly 40 work   # keep 10% of the 5-hour and 40% of the weekly limit
claude-as --reserve-weekly 30 personal              # the 5-hour window inherits, or stays off
codex-as --reserve-5h off --reserve-weekly off work # no reserves for this launch, whatever the file says
```

With a reserve on, the launcher gives the native CLI hooks for that one launch, and the native runs the kit's reserve guard (`lib/coding-agent-kit/guard.py`) at each new prompt and each tool call: once an enabled window is at or below its reserve, the guard refuses them, until that window resets or a relaunch turns its flag off. Before the native starts, the launch prints its policy on stderr, each window with the place its value comes from:

```text
claude-as: usage reserves for personal: five-hour off (default), weekly 10% (usage_reserve.claude_accounts.personal); hooks refuse new prompts and tool calls at a reserve.
```

The kit writes nothing to the natives' own configuration: `claude-as` passes the hooks in a `--settings` argument of its own and `codex-as` as `-c` values, only for a launch with a reserve on (Codex itself records your trust of the hooks, see [codex-as with a reserve](#codex-as-with-a-reserve)). The guard is one short process per hook event, run by the launcher's own Python; it needs no Node, no background process and no network. With both windows off, as by default, a launch is exactly what it is without this feature: the same arguments, environment and process. So are the forms that run no model work, listed per launcher below, also with a reserve on.

### What the guard checks

At each prompt and each tool call the guard compares the current reading of every enabled window with its reserve, and refuses when the share left (100 minus the percent used) is at or below the reserve. A reading stays current until its window's reset time, however old it is, since usage never falls inside a window; after the reset the guard allows again by itself, and nothing is latched. A reading above 100% counts as 100%, and a window without a current reading has nothing to check. The guard trusts the host's clock against the server's reset times. The readings come from the usage data the native already receives:

- **Claude Code** hands the rate limits it derives from its replies to one place only, the input of the status line command: no hook input carries them, and the usage endpoint refuses a setup-token. A launch with a reserve therefore makes the guard its status line. When that input carries a rate-limit window, the guard merges it into the account's usage file, `claude-NAME.usage.json` in the [account metadata](#account-metadata) directory (the file `--info` reads), and notes that this launch has usage data. Then it runs your own status line command, the one your user, project or local settings name, with the same input, and prints its output with your `padding`, so the footer looks as it does without a reserve; a command that fails, prints nothing or runs longer than 5 seconds leaves it empty. None of this needs Node or the kit's [status line helper](../README.md#claude-code-helpers): on a host without `node` the footer is as empty as it is there without a reserve, and the guard still has its data. The guard reads the account's usage file, so a window that an earlier session of the account recorded at its reserve refuses the first prompt of the next one.
- **Codex** writes the rate limits of each response to the session's rollout. The guard reads the newest snapshot with rate limits in this session's rollout since the launch started, whatever its limit family (a family other than the general one is named in the message as `(limit NAME)`), and tells the windows by their length (300 minutes is the 5-hour window, 10080 the weekly one). Without such a snapshot it reads the usage that codex-as recorded for the account after earlier runs (what `--info` shows), windows without a reset time left out. A sub-agent's hook reads the sub-agent's own rollout, which has no snapshot before the sub-agent's first reply, so it reads the account's record until then.

A session can also be **blind**: it has had replies and still has no usage data. Blindness never refuses a session's first prompt; after that a blind session is refused with a message of its own:

- Claude: the first sign of a reply (the session's first tool call or turn end) was at least 120 seconds ago, and the launch's status line has reported no rate limits since it started. Claude Code runs no status line in a workspace it does not trust yet, and an account whose plan reports no window gives none, so both become blind this way two minutes after their first reply.
- Codex: at a tool call, the rollout holds token counts since the launch started and none has rate limits; at a prompt, also when it shows a model response since the launch started and no rate limits.

An account whose server reports no window at all therefore needs its reserves set to `"off"` in its [account table](configuration.md#usage-reserves).

An error of the guard itself on a prompt or tool event (a launch it cannot read, an unexpected data shape, an exception) refuses with the failure message below, never with a traceback and never by allowing; on the other events it stays silent. A guard that cannot start at all is a failed hook: Claude Code then blocks the prompt or tool call (the guard's two blocking hooks set `"onFailure": "block"`), while Codex, which has no such switch, lets the event through, and the check after the run reports it (below).

### What it does not stop

The guard refuses new prompts and tool calls; it is not a hard limit.

- A reply that is streaming finishes. Usage data trails by one reply, so the request that crosses the reserve completes. A refused tool call ends a Claude Code turn without another request, while Codex makes one more, in which the model reads the refusal and reports it; a Claude sub-agent stopped this way ends its own loop, and its parent makes one more request when that result arrives. Every agent running in parallel when the reserve is reached, sub-agents and other sessions of the account alike, finishes its reply and can make one more request to report the refusal, so the overshoot grows with the number of agents running in parallel. Concurrent Codex sessions of one account each learn of the reserve at their own next reply.
- Requests that no hook sees go through: compaction, Codex's memory consolidation and reviewers, and Codex tool calls outside its `PreToolUse` hook (`write_stdin`, the code-mode `wait`, tool search and hosted tools such as web search).
- A process that calls the provider itself is not stopped, and a `claude` or `codex` that a tool starts inside the session carries no guard.
- Claude's per-model weekly limits (a model family's own weekly row) are not checked: the status line does not carry them.
- Usage elsewhere, on other hosts or by other clients of the account, shows only with the session's next reply, and nothing is known before a session's first reply but what earlier sessions recorded.
- A reading of a Claude account never goes back: a stored window that has not reset stays when the incoming one has the same reset time and a lower percentage, an earlier reset time, or no window at all, and the [status line helper](#account-metadata) writes the file by the same rule. The file has no lock, so two status lines that write it at the same instant can briefly let an older reading replace a newer one, until a status line run with a reading at least as new writes it again.
- Hosts and clients without the launchers are unguarded: plain `claude` and `codex`, IDE and desktop clients and the Codex app-servers they start, the web, and native Windows, where the kit does not run. So is a host whose configuration sets no reserve.
- A claude-as launch under a server-managed or registry policy that sets `disableAllHooks` runs without its guard and says nothing ([claude-as with a reserve](#claude-as-with-a-reserve)).
- There is no way to use the reserve from inside a session: that takes a relaunch.

### Messages

Every refusal has one layout: a line with the launcher, the account and the reason, a line that says what was stopped, and the way out, with the exact command on a line of its own. A window at its reserve:

```text
claude-as: personal: weekly window 8.5% remaining, at or below its 10% reserve; resets in 2d 3h (2027-01-17T11:02:00Z).
This prompt was not sent.
To use the reserve, exit and run:
  claude-as --reserve-weekly off personal --continue
```

A blind session:

```text
codex-as: work: no usage data since this session started, so its usage reserve cannot be checked (the session's rollout shows no rate limits).
This prompt was not sent.
To go on without reserves, exit and run:
  codex-as --reserve-5h off --reserve-weekly off work resume --last
To keep them off for this account, set them to "off" under [usage_reserve.codex_accounts.work].
```

A guard that failed, named by the error's class, or `launch format` for a launch it cannot read:

```text
claude-as: work: the usage reserve guard failed (launch format).
This prompt was not sent.
To go on without reserves, exit and run:
  claude-as --reserve-5h off --reserve-weekly off work --continue
```

Each window at its reserve gets a sentence of its own, with the time to its reset when the reading has one. For `claude-as` the blindness cause reads `the status line has reported no rate limits: the workspace is not trusted yet, or this account reports none`. At a tool call the second line reads `This tool call was stopped. Stop working now and tell the user why.`, which the model reads, and at the end of a Claude turn `The work stops here.`

- Claude Code shows a refused prompt as `UserPromptSubmit operation blocked by hook: [COMMAND]: MESSAGE`. A refused tool call ends the turn, shown as `PreToolUse:TOOL hook stopped continuation: MESSAGE`, and no `Stop` hook runs after such a turn, yours included (a notification, or a loop that starts the next turn). A turn that ends while a window is at its reserve ends the work with the message, so that another `Stop` hook cannot start a new turn.
- Codex does not send a refused prompt. A refused tool call does not run; the model gets the message as the call's output and replies. `codex exec` shows no reason and exits 0, so after a headless run codex-as prints the guard's latest message on stderr.

### The relaunch

The way out is a relaunch: exit the session and run the command the message names. It turns off, for that launch only, the reserve of each window at its reserve (both, for a blind session or a failure) and continues the session: `claude-as ... --continue` the directory's most recent conversation, `codex-as ... resume --last` the directory's newest session; add your other arguments as usual. Nothing is stored: the next launch without the flags has its reserves again, and once the window that refused has reset, a launch with the reserve works as before. To keep a window off for one account, set it to `"off"` in that account's table (see [configuration](configuration.md#usage-reserves)).

### claude-as with a reserve

A launch with a reserve on gives Claude Code one `--settings` argument before your arguments, which holds the `SessionStart`, `UserPromptSubmit`, `PreToolUse` (every tool) and `Stop` hooks and a status line, each running the guard. Settings on the command line rank above user and project settings, so that status line is the launch's, and the hooks run beside your own. The launch also sets `CLAUDE_DISABLE_ADOPT=1` and `CLAUDE_CODE_DISABLE_AGENT_VIEW=1` and removes `CLAUDE_CODE_RESUME_INTERRUPTED_TURN` from Claude Code's environment. It needs Claude Code 2.1.296 or newer, with no upper limit, and runs `claude --version` first to check.

A guarded session cannot be moved to the background. Claude Code runs background sessions in a daemon that later launches share, and those sessions run with the environment and the credential of the launch that started the daemon, not of the launch that moved them there. With a reserve on, the "Move to background and exit" choice, `/background` and the agent view are closed, and the forms that use the daemon are refused (below). Launches with both reserves off keep these features as Claude Code has them: unguarded accounts that share `~/.claude` share one background daemon, whose sessions run with the environment and token of the launch that started it, so a session moved there can run on another account's token.

Launch forms with a reserve on:

- The interactive session, with `-c` (`--continue`), `-r` (`--resume`) or any other supported option, is guarded.
- These launch as without reserves, since they start no session: `NAME --help` and `NAME --version` (`-h`, `-v`), and any help or version option with no operand; the commands `doctor`, `update` (`upgrade`), `install`, `purge`, `project purge`, `import-conversations` and `apply-project-settings`; the `mcp`, `plugin` (`plugins`), `auto-mode` and `sandbox` subcommands that manage servers, plugins and settings, such as `mcp list` and `plugin install` (`mcp serve`, `plugin eval` and `auto-mode critique` are refused); and `logs`, `stop`, `kill` and `rm` as the first argument. A subcommand that a later Claude Code release adds keeps the launch guarded.
- Refused, each with one line that names the reason and the relaunch without reserves:

  ```text
  claude-as: personal: -p runs Claude Code outside its interactive session, where the usage reserve guard cannot stop it; to launch without usage reserves, run claude-as --reserve-5h off --reserve-weekly off personal with the same arguments.
  ```

  - work outside the interactive session: `-p` (`--print`), `--init-only` and a standard output that is not a terminal (a redirect or a pipe), where Claude Code runs headless and shows no status line; `ultrareview`, `auto-mode critique`, `plugin eval`, `mcp serve` and `gateway`;
  - the background daemon: `--bg` (`--background`), `agents`, `attach` and `respawn`, and `daemon` as the first argument or behind nothing but `--dangerously-skip-permissions` and `--allow-dangerously-skip-permissions`;
  - `remote`, `sync` and `bridge` as the first argument, which Claude Code would send to the model as a prompt behind the guard's `--settings`;
  - a `--settings` of your own, which cannot be combined with the guard's;
  - Claude Code's modes that run no hooks: `--safe-mode` or `--bare` anywhere before a `--`, also as an option's value, and `CLAUDE_CODE_SAFE_MODE` or `CLAUDE_CODE_SIMPLE` turned on in the environment or in a settings file's `env`; `CLAUDE_CODE_SHELL_PREFIX` set there, the command Claude Code would run the guard through;
  - settings that keep Claude Code from running the guard: `disableAllHooks` in any settings file; in the machine's managed settings `allowManagedHooksOnly`, `strictPluginOnlyCustomization` (true, or a list that names hooks) and a `statusLine`, which would replace the guard's; and a settings file's `env` that sets `CLAUDE_CODE_RESUME_INTERRUPTED_TURN` or turns `CLAUDE_DISABLE_ADOPT` or `CLAUDE_CODE_DISABLE_AGENT_VIEW` off;
  - a Claude Code older than 2.1.296, or one whose `claude --version` prints no version.

The settings checked are your `settings.json` and `settings.local.json` in the Claude configuration directory, the project and local settings of the launch directory and of every directory above it (and the local settings of a linked worktree's main checkout), the machine's managed settings files and their `managed-settings.d` drop-ins, on macOS the managed preferences of the `com.anthropic.claudecode` domain, and on WSL the Windows managed settings when one of them sets `wslInheritsWindowsSettings`. Server-managed settings, whose cached copy describes whichever account ran last, and on WSL the Windows registry policies are not checked for these keys.

When Claude Code runs the guard's status line but not the injected hooks, as under a server-managed or registry policy that sets `allowManagedHooksOnly`, the status line, once it has rate-limit data (a reply happened) while no hook of the launch has run, prints `reserve guard not running` as the first line of the footer. The next guarded launch of the account also warns once, when an earlier launch shows no hook run and a sign of a reply (its status line saw rate limits, or, for a launch on this host whose Claude Code has exited, the account's usage was recorded after it started):

```text
claude-as: personal: an earlier launch ran without its usage reserve guard: Claude Code did not run the injected hooks.
```

A server-managed or registry policy that sets `disableAllHooks` stops every status line too, the guard's included: such a launch runs without its guard and without a word, with no footer line, no blind refusal and no usage recorded by its sessions, so the warning above follows it, for a launch on this host, only once a later session of the account records its usage. A usage reserve is therefore not enforced on a host whose server-managed settings or registry policies set `disableAllHooks`.

`claude-as` becomes Claude Code (it execs it), so it checks nothing after the session. A guarded launch that Claude Code ends before any hook runs (`--continue` in a directory without a conversation, `--resume` of a missing session, a picker closed with nothing in it) leaves the same trace, so the warning can also follow one of those once a later session of the account records its usage.

### codex-as with a reserve

A launch with a reserve on gives Codex two hooks as `-c` values after the launcher's own, `UserPromptSubmit` and `PreToolUse` for every tool, each running the guard, and turns goals off with `-c features.goals=false` and `--disable goals`. It needs codex-cli 0.161.0 or newer, the minimum of every codex-as launch, with no upper limit. codex-as keeps running while Codex runs and checks the run after Codex exits. [codex-as](codex-as.md#usage-reserves) has the exact values and the Codex details.

Launch forms with a reserve on:

- guarded, interactive: the TUI, `resume` and `fork`;
- guarded, headless: `exec` in every form (`e`, `exec resume`, `exec fork`) and `review`;
- refused: `cloud` (`cloud-tasks`), `app-server`, `exec-server`, `debug app-server` and a foreground `remote-control`, which can run model work where the guard's hooks cannot see or stop it;
- as without reserves: every other subcommand, the help and version forms, `help`, `update` and `completion`, and a subcommand's own help.

Refused, each with one line that names the reason and the relaunch without reserves:

```text
codex-as: work: --ephemeral keeps no session rollout, from which the usage reserve guard reads the session's usage; to launch without usage reserves, run codex-as --reserve-5h off --reserve-weekly off work with the same arguments.
```

- a `-c` or `--config` value under `hooks`, which would meet the guard's own;
- `--disable hooks`, a `-c` value that turns the hooks feature off, or a configuration file that Codex may load for the launch and that turns it off under `[features]` (the shared, profile, system, managed and project files that [codex-as](codex-as.md#usage-reserves) lists); an entry under `[hooks.state]` that turns one of the guard's two hooks off;
- `--enable goals`, a `-c` value that sets `features.goals` to anything but `false` or replaces the `features` table with one that does not set it `false`, or a managed configuration that sets `goals` under `[features]` to anything but `false`, any of which could turn goals on;
- `--ephemeral`, which keeps no rollout; `--ignore-user-config`, which skips the configuration that holds Codex's trust of the hooks; and `--worktree`, which runs the session in a Git worktree that Codex creates under its home, whose project configuration the launch checks do not read and where the check after a headless run does not look;
- a login that names no ChatGPT user and workspace, as an API key login does: it reports no usage windows, and its sessions cannot be told from other accounts';
- a headless launch before Codex has run the guard's tool hook on this host (below).

**Trust.** Codex runs a hook given on its command line only once you trust it. The first guarded interactive launch on a host shows Codex's "Hooks need review" (`2 hooks are new or changed`); choose "Trust all and continue", or review them first. Codex keeps the trust in the shared `<codex_home>/config.toml`, which every account home links, so one trust covers every account on the host; codex-as never reads or writes a trust record. The hook definitions are the same on every launch, account, host and kit release, so the trust lasts. "Continue without trusting" runs that session without the guard, which the check after the run reports. Trust can be lost again, and for one hook alone: Codex keeps it per hook under a key that names the command line as the source, the event and the hook's place, in a store shared by every program on the host, so another program that passes a hook of its own on Codex's command line for the same event and place takes the same key, and a Codex app-server (the desktop app or an IDE extension) may write an older copy of `config.toml` back. An interactive session then asks again, while a headless run skips an untrusted hook without a word. So a headless guarded launch is refused until the guard's tool hook has run under Codex on this host, which the guard records with a marker in its state directory at the first guarded tool call:

```text
codex-as: work: Codex has not run the usage reserve guard's tool hook on this host yet, and a headless run skips hooks that are not trusted without a word: start codex-as work once interactively, trust the hooks in "Hooks need review" and let it run one tool call; to launch without usage reserves, run codex-as --reserve-5h off --reserve-weekly off work with the same arguments.
```

**Shell startup files.** Codex runs each hook command through the session's shell without a login (`zsh -c` runs `~/.zshenv` first, and neither `~/.zprofile` nor `~/.zlogin`): whatever that startup file prints joins the hook's output, which for a prompt the guard allows reaches the model as a developer message, and a startup file that fails makes the hook fail, which Codex lets through. Keep the files a non-interactive shell reads silent and working.

**Goals.** A goal continues on its own, without a prompt, so at a reserve a refused tool call would repeat without end. A guarded launch therefore turns goals off (`/goal` and goal continuation) and refuses what would turn them on. Launches with both reserves off keep goals.

**Hooks that never run.** After Codex exits, codex-as compares what the run's sessions show in their rollouts with what the guard saw. When the run's own sessions show model requests while neither hook ran, or tool calls while the tool hook did not, it warns and removes the marker, so headless launches are refused until an interactive launch has trusted the hooks again:

```text
codex-as: work: the usage reserve was not enforced in this run: Codex did not run the guard's hooks (not trusted, disabled, or managed hooks only). Start codex-as work interactively and trust them in "Hooks need review".
```

It names `the guard's tool hook` when only that one did not run. When the sessions that show it may be another client's, the run gives a notice instead, and the marker stays:

```text
codex-as: work: the usage reserve may not have been enforced in this run: a session of work written during it, by this run or by another client of work, shows that Codex did not run the guard's tool hook (not trusted, disabled, or managed hooks only), and the check after the run cannot tell which client wrote it. If it was this run, start codex-as work interactively and trust them in "Hooks need review".
```

So headless runs beside another client of the account can each go unguarded, each with the notice: at most one headless run goes unguarded after trust is lost only where its sessions can be told apart from other clients'. [codex-as](codex-as.md#which-sessions-are-the-runs-own) describes which sessions count as the run's own. Known false notices, with every hook trusted and running, go to a run that made no tool call while another codex-as launch of the account made some: two guarded TUIs of one account, in one directory or in two, one only chatting and the other making tool calls (the chatting one gets the notice); concurrent `exec` runs of one account in one directory; and a TUI run during which the other launch continues one of the account's older sessions.

## Codex account layout

Each account has a fixed entry point: `<codex_home>/accounts/NAME/auth.json -> <secrets_dir>/codex-NAME.json`, an absolute link. Account directories are 0700; complete credentials are 0600. `codex-as NAME` sets that account's `CODEX_HOME` and enforces file authentication; the first launch creates the account view itself, with no separate initialization command. Other arguments go to native `codex` unchanged after its own file-auth and SQLite flags (every command but `exec` also gets the update-check and `--no-daemon` flags, and a launch with a [usage reserve](#codex-as-with-a-reserve) the guard's hooks and goals off), and the launched Codex keeps the caller's umask. `login` (except `login status` and help), `logout`, the daemon-only `agents`, `queue`, `app` (macOS and Windows), `remote-control` subcommands and `app-server daemon`, and remote or provider/auth settings are refused; `-p NAME` profiles work from `<codex_home>/NAME.config.toml`; root help or version, `help`, `update` and `completion` run as plain `codex`. See the [Codex verification record](codex-as.md) for the exact list and the remaining differences from native Codex. The root `<codex_home>/auth.json` belongs to native `codex`, desktop and IDE clients. codex-as never reads, checks or changes it, so the root login and each account keep independent token chains, even for the same ChatGPT user.

Private backups live at `<secrets_dir>/.codex-as-backup-*` and are excluded from completion. Permission errors name the exact path and are never fixed by an automatic chmod: the parents of `<codex_home>` and `<secrets_dir>` (both `$HOME` by default), `<codex_home>` and `<codex_home>/.tmp` must be owned real directories that are not writable by other users: no other write, and group write only when the group is your user private group and the directory has no access ACL; a refusal names the directory and suggests `chmod go-w`. `<secrets_dir>` must be 0700. The account marker `<codex_home>/accounts/NAME/codex-as.initialized` and a private pending-setup record are fully written to temporary files before atomic publication, so an interrupted first launch can be retried; unknown existing directories are preserved and initialization is refused.

A root `<codex_home>/auth.json` that links into `<secrets_dir>`, and the root-level files `<codex_home>/codex-as.layout.json` and `<codex_home>/codex-as.initialized`, are not part of the codex-as layout. codex-as ignores those two files, so they can be deleted; the account marker in each account directory stays. Such a root link works, but it shares that account's token chain with the root clients, so a root login or logout revokes the account. To separate them, quit every root, desktop and IDE client completely, including the root app-server daemon and IDE extension app-servers, until `pgrep -af 'codex.*app-server'` lists none. Then remove only the link with `rm <codex_home>/auth.json` (the secrets file stays) and log in natively in the root home. A client that is still running keeps the old tokens in memory, and its logout (`account/logout`) revokes those cached tokens even after the link is gone, so quit it rather than logging out.

Credentials renew without a new login. The access token carries its own expiry; native Codex refreshes it when less than five minutes remain, and the server rotates the refresh token, which Codex writes through the account link into `<secrets_dir>/codex-NAME.json`. Only use triggers this refresh; no background keepalive runs. A revoked refresh token cannot be renewed and needs a new device login. **Native `codex login` and `codex logout` revoke the tokens of the current home's `auth.json` before removing it.** In the root home, a desktop or IDE logout included, that affects only the root's own login, unless that file is still a link into `<secrets_dir>` or a still-running client loaded its tokens through such a link; codex-as refuses both inside account views. A logout from inside an account's Codex (`/logout`) is native and still revokes that account's tokens; the launcher warns at exit with the steps below. Add accounts with `codex-as NEWNAME`. To replace a revoked account, move its secrets file aside under a name that does not match `codex-NAME.json`, then run `codex-as NAME`.

There is no removal command; to remove an account, first quit every client of it. Each `codex-as NAME` launch holds a shared lease on `<codex_home>/accounts/.locks/NAME.lock` while it runs, so this check fails with `BlockingIOError` while one does:

```sh
python3 -I -B -c 'import fcntl, sys; fcntl.flock(open(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)' <codex_home>/accounts/.locks/NAME.lock
```

Desktop, IDE and direct clients started with that account's `CODEX_HOME` take no lease, so close those by hand. Then delete the whole directory `<codex_home>/accounts/NAME`, account marker included (`rm -r` removes its links into `<codex_home>`, never what they point to); `<secrets_dir>/codex-NAME.json` and the `codex-NAME.json.lock` that native Codex keeps beside it; the files named for the account in `<codex_home>/accounts/.locks/` (`NAME.lock`, and `.setup-NAME.json` or `.refresh-NAME.lock` where present); and `codex-NAME.json` in the [account metadata](#account-metadata) directory. A private backup left by a repair of that account may still hold its credentials. Sessions, history and the SQLite home are shared, so the account's sessions stay and resume under another account; `UUID.handoff.json` records belong to sessions and stay too. Deleting the files does not end the login: the refresh token stays valid at the server. To revoke it as well, run `/logout` in a `codex-as NAME` session before deleting anything: it revokes the account's tokens and removes the link, so the launcher's exit warning about the missing link is expected.

Config, both AGENTS files, rules, skills, agents, memories, session directories, history, the session index, and the complete SQLite home are shared. Database files and WAL/SHM use the original directory together, rather than individual file links. Configuration path fields must be absolute because native relative paths resolve against the account directory; the launcher reports the field name and never rewrites configuration. An explicit `sqlite_home` retains its absolute directory; otherwise, the root directory is used. Conflicting environment, project, or CLI overrides are refused.

Native thread writer locks and the rollout maintenance lock are shared. An active writer prevents another account from writing the same session. Interactive resume takes sessions over from the root daemon so another account can write them: it ends the old session and spawned agents there, temporarily archives them, restores saved archive visibility, then launches the selected account. An explicit `codex-as OTHER resume UUID` does this without a prompt for an idle session and asks first when it may be running, since takeover interrupts active work. `resume --last` takes over only the session it opens, the newest one native Codex lists, when that one is idle in the root daemon. Before the picker starts, the idle top-level sessions it lists by default (this directory, or the same place in a linked worktree; all of them with `--all`) are taken over and named, because the selection happens inside native Codex; they move to the top of the list. Sessions that may be running stay and are named too. Clients attached to the root daemon, such as Desktop, close a session taken over. The remaining daemon sessions stay loaded. Sessions held by standalone clients are never touched; native Codex shows its conflict screen until they exit. Explicit session names have account-path filtering problems, so the launcher rejects them and requests a UUID. Names can still be displayed and edited in the native UI.

Takeover requires a root daemon at 0.161.0 or newer, and a controlling terminal only to confirm a session that may be running. Unknown identities, unsafe sockets and ambiguous ownership cause refusal, and an unrecognized option in the resume command skips takeover. Recovery metadata stays private under `<codex_home>/accounts/.locks/UUID.handoff.json`; retain it and retry the same command after an interrupted or failed takeover. `fork UUID`, `exec resume UUID` and `exec fork UUID` never take over, but first finish an interrupted takeover of that UUID; `resume UUID` does too, and the picker and `resume --last` finish every interrupted takeover. Recovery only restores visibility. Saved history and original descendant archive visibility are preserved; unfinished runtime progress, SessionEnd hook effects and timestamp changes cannot be rolled back.

Cloud policy, the model catalog/cache, remote plugin installations/cache, MCP/Connector login, daemons, IPC, temporary files, and logs remain separate for each account. Local plugin paths in shared configuration can be shared. Remote plugin cache directories cannot be linked wholesale: one account's sync could delete another account's bundles. These existing root account caches are not imported into new accounts.

If account credentials are atomically replaced with a regular file, the link is restored when there are no other wrapper clients and the file matches secrets. If secrets have not changed during this run and the user identity matches, both copies are backed up, the new original bytes are imported, and the link is restored. If both copies changed, no startup snapshot exists, or identities differ, repair stops without guessing from timestamps. The last exiting client or the next idle startup can perform safe repair; repair errors do not replace the native exit code.

Divergence needs a manual choice while every client of that account is closed; the error prints both paths and the command. Back up `<codex_home>/accounts/NAME/auth.json` and `<secrets_dir>/codex-NAME.json`, keep the wanted copy at `<secrets_dir>/codex-NAME.json`, then restore the link with `ln -sfn <secrets_dir>/codex-NAME.json <codex_home>/accounts/NAME/auth.json`. Use the absolute paths the error or `codex-as --show-config` prints: the launcher compares the link text with them, so a link through a symlinked alias of the same directory is refused. A missing link is restored the same way, unless a logout inside Codex removed it: those tokens are revoked, so move the secrets file aside and run `codex-as NAME` to log in again. **Do not delete the account marker or overwrite either file with an older snapshot.** See the [Codex verification record](codex-as.md) for source and test limitations.

## Claude configuration and security boundaries

Tokens are saved at `<secrets_dir>/claude-NAME`: 0600, a regular file owned by the current user, with a single hard link. Daily startup sets `CLAUDE_CODE_OAUTH_TOKEN` and the three variables of the [usage file](#account-metadata) for the child (a launch with a [usage reserve](#claude-as-with-a-reserve) also what the guard needs) and inherits the existing `CLAUDE_CONFIG_DIR`. It does not force `~/.claude`, preserving existing sources of `.claude.json`, MCP, configuration, and trust state.

Explicit API key/OAuth token/authentication FD/alternative provider/endpoint/profile overrides, authentication helpers in settings, and organization restrictions are refused. Only variable names or sources are reported, never values. Passthrough of login/logout/setup-token, `--bare`, authentication configuration source overrides such as remote/SDK/managed-settings/project-config-root, and unaudited options is prohibited. Only the first command operand is identified using native argument boundaries; words inside prompts are not scanned. Native Claude dispatches an exact first `auth` operand to authentication subcommands even with `-p` or `--`, so it is refused. Required/optional/variadic argument values retain their native roles. This is a bounded local CLI option check, without a promise of compatibility with every command.

Managed policy is respected: automatic enrollment stops for nonempty managed caches, system policy, policy helpers, authentication bindings, or profile loading chains that cannot be preserved. Empty or missing caches do not cause this refusal. The managed cache is checked at every enrollment.

New account files are created exclusively; incorrect permissions, ownership, symlinks, or hard links are refused. Account names must be nonempty, contain only ASCII letters, digits, underscores, and hyphens, and not start with a hyphen, as in codex-as. Local format validation is not server authentication; expired credentials are not automatically deleted or replaced through login.

## Account metadata

Both launchers keep a small cache of what they last saw about each account, so a completion preview or a person can look it up offline: `codex-as --info NAME` and `claude-as --info NAME` (`-i NAME`). It lives in `${XDG_STATE_HOME:-~/.local/state}/coding-agent-kit/accounts/` (an absolute `XDG_STATE_HOME` only; the directory is created 0700 on first use and must stay an owned, real 0700 directory, in a parent that belongs to you or root and that other users cannot write, under the same group rule as `<codex_home>`), never in `secrets_dir`: a configuration whose `secrets_dir` is that directory is refused, and one that reaches it through a link is never read or written. Each file is a 0600 JSON object, replaced atomically and only when its content changes, by the writers below:

| File | Content | Written by |
| --- | --- | --- |
| `codex-NAME.json` | email, plan, and the last usage snapshot | `codex-as`, before and after each run |
| `claude-NAME.json` | the label | `claude-as` enrolment or `claude-as --label` |
| `claude-NAME.usage.json` | the last 5-hour and weekly windows | `helpers/statusline.cjs`, and the reserve guard in a launch with a usage reserve |

No file holds a token, and no time in them comes from the launcher's clock: a Codex snapshot carries the time of the rollout line it came from, a Claude one the time the status line saw it.

```text
$ codex-as --info work
work (Codex)
email   alice@example.com
plan    plus
5h      23% used, resets 14:30 (in 2h 10m)
weekly  41% used, resets Mon 09:00 (in 3d 4h)
seen    12 min ago
```

Times are local. A window whose reset time has passed reads `reset since HH:MM`; missing data reads `not recorded yet`, and an account without a label reads `not set (claude-as --label NAME LABEL)`. `--labels` prints one line per account, `NAME<TAB>LABEL` (the email for Codex, the label for Claude) or the bare name when there is none; the zsh completions use it as descriptions. Both queries, like `--list`, create no directory, run no preflight or native CLI and never open a credential: they read only the state files, accept only an owned regular 0600 file with one link, at most 64 KiB, with unique keys, and print only the fields above with control characters removed. An unsafe file fails `--info` with one line that never quotes it; `--labels` shows that account's bare name.

- **Codex identity**: the email (the `email` claim, else the profile claim's `email`) and the plan (`chatgpt_plan_type`) from the account's `id_token`, decoded locally at each launch and again after the run. An API-key login records the plan `API key` and no email. The plan changes only when Codex refreshes the token.
- **Codex usage**: after native Codex exits, codex-as reads the session rollouts modified during the run, only regular files (never a FIFO, device or link), and keeps the newest `token_count` event with rate limits of the default `codex` family timestamped within the run. A rollout counts when its thread is the one the run resumes (`resume UUID`, `exec resume UUID`; a fork writes a new thread, so its source never counts), or when its `session_meta` names this account's ChatGPT user and workspace as the creator. The resumed thread comes first, then the newest rollouts: codex-as reads the tail (at most 1 MiB) of up to eight of this account's, after the first lines of at most 64 others. Of concurrent runs of one account the newest snapshot normally wins: each run reads the stored record only just before it replaces it. Times from the year 10000 on are ignored. The windows are named by their length (300 minutes is `5h`, 10080 is `weekly`). This is best effort, silent on failure and never changes the exit code. Known gaps: the rollout format is Codex's internal one, so a change leaves usage blank; a session switched inside Codex (`/resume`) is missed; compressed rollouts (`.jsonl.zst`) are skipped; past 64 other accounts' rollouts modified during the run, this account's own new session can be missed; and another account that resumes, during the run, a thread this account created can be counted here.
- **Claude label**: a new account asks `Label for this account (optional, e.g. your email):` after the confirmation and before any token exists, and saves the label only after the token. Control characters, more than 100 characters and anything containing `sk-ant-` are refused, so a pasted token is never stored. `claude-as --label NAME LABEL` sets the label of an existing account, `claude-as --label NAME` asks for it on `/dev/tty`, and an empty label clears it.
- **Claude usage**: Claude Code gives the status line its rate limits only for a claude.ai subscription login, which the setup-token of a `claude-as` account is, and only after the first API response, and leaves out a window the account's plan does not report; headless `claude-as NAME -p` runs render no status line and record nothing. claude-as passes its child `CODING_AGENT_KIT_USAGE_FILE` (the path of `claude-NAME.usage.json`), `CODING_AGENT_KIT_TOKEN_ID` (the first 16 hex digits of the token's SHA-256) and `CODING_AGENT_KIT_CLAUDE_PID` (its own pid, which stays the session's because it execs claude), and drops inherited values first. Claude Code passes `CLAUDE_CODE_OAUTH_TOKEN` to none of the commands it starts, the status line included, so `helpers/statusline.cjs` recognises the session by that pid: it records only when all three are set and the claude with that pid runs it, as its parent or behind one shell (`sh`, `bash`, `zsh`, `dash`, `ksh` or `fish`; read from `/proc` on Linux and with `ps` elsewhere). A claude started inside the session has a pid of its own and records nothing, whatever it inherited. A token that is visible all the same must hash to the identifier. The path must be absolute with a `claude-NAME.usage.json` name in an owned 0700 directory, and the numbers are valid; it writes when the windows change or the record is older than five minutes, never on Windows, and its printed status line is unchanged. A launch with a [usage reserve](#what-the-guard-checks) records through the reserve guard instead, which is that launch's status line and writes the same file by the same rules; the helper, when your settings name it, then runs behind the guard, which is neither claude nor a shell of it, and records nothing. Neither writer lets a reading go back: a stored window that has not reset stays when the incoming one has the same reset time and a lower percentage, an earlier reset time, or no window. The file has no lock, so two status lines that write it at the same instant can briefly let an older reading replace a newer one, until a run with a reading at least as new writes it again.

Launches with a usage reserve keep their state in `guard/` beside `accounts/`, a directory under the same rules: for each launch, files named `TOOL-NAME-PID-START.SUFFIX` that record that the launch has a guard (`launched`), each kind of hook the native ran (`ran.KIND`), Claude's first sign of a reply (`reply`) and first usage data (`seen`), and the guard's latest refusal (`blocked`); and the Codex marker `codex-hooks-FINGERPRINT`. A guarded launch removes the state of launches that started more than 30 days before, and codex-as removes its own launch's state when Codex exits; the markers stay.

Privacy: emails and labels appear in completion previews, screen shares and recordings, and the output of `--info` lands in any agent transcript that runs it. No token is ever printed or stored there.

## Completion, installation, and concurrency

`install.sh` links the zsh completions into `PREFIX/share/zsh/site-functions`, which must be on `fpath` before `compinit` runs (with oh-my-zsh, before it is sourced; the installer prints the line), and the Bash completions into the bash-completion user directory, `${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-~/.local/share}/bash-completion}/completions`, where bash-completion autoloads them. For the first argument Bash runs `codex-as --list-accounts` or `claude-as --list-accounts` and zsh `--account-labels`, which adds each account's email or label as its description, so the launchers must be on `PATH`; the listing enumerates only safe filenames and metadata, without reading credentials, running the native CLI, or using the network, and an invalid configuration offers nothing. Corrupt contents are detected at startup. A first argument that starts with `-` completes the [own options](#own-options) instead. After `-i`, `--info` or `--account-info`, and `claude-as --label` or `--set-label`, the next argument completes account names the same way, in zsh in the group `Codex accounts` or `Claude accounts`; nothing else follows an own option. The completions call `--list-accounts` and `--account-labels`, the spellings every release from 0.3.0 on accepts, so a shell opened before an update or after a rollback keeps completing. Before `NAME`, `--reserve-5h` and `--reserve-weekly` complete their values (`off` and every tenth percentage, then every whole percentage that matches what is typed) and then the remaining flag or the account names. After `NAME`, later arguments first complete the supported scope, then delegate to existing native completion when available; Bash hands the native completion the command line from `NAME` on, with the cursor, the current and the previous word adjusted to it.

fzf-tab, when present, uses the same candidates; normal Tab works without fzf. New terminals load the new completions; nothing appends source or PATH lines to rc files. Installation only creates links and triggers no real initialization, login, or model requests.

The launcher's own step/help headings are cyan, confirmations/warnings yellow, successful saves/initialization green, and errors red. By default, color is enabled only for the terminal receiving the message; redirected logs stay plain. Confirmations on a separate `/dev/tty` can still use color without affecting redirected Codex/Claude output. `CODING_AGENT_KIT_COLOR=auto|always|never` is supported (unset or any other value means `auto`); nonempty `NO_COLOR` or `TERM=dumb` takes priority and disables color. Native command output, completion account lists, and internal machine output are not colorized.

**Codex supports concurrent sessions across accounts and different sessions within one account.** Each wrapper and native child holds a shared account lease; credential repair requires an exclusive lease. Suspended processes or a child surviving an abnormal supervisor exit still block repair. Account lease files persist and are not deleted based on PIDs. Each launch that can write sessions, with reserves or without, also appends one byte to its account's lease file, which tells the check after a guarded run whether another launch of the account arrived during it; a launch that holds the lease alone empties the file once it reaches 4096 bytes. Per-session takeover leases are removed when the takeover ends. A shell inheriting `CODEX_HOME` from account A can launch B directly, but the inherited source must be a verified managed account directory. Before Codex 0.167.0, the native same-account OAuth refresh semaphore is in-process only and saves truncate and rewrite the secret, so several processes can refresh at once or briefly read partial JSON; 0.167.0 and newer serialize their own commits through native lock files and replace the secret atomically (see [symlink writeback verification](codex-as.md#symlink-writeback-verification)). The wrapper implements no additional OAuth. Direct CLI, desktop, and IDE clients do not participate in wrapper leases, and automatic exit repair cannot exclude them. Manual repair requires closing that account's clients. Processes are not automatically killed, quotas are not switched automatically, no background keepalive runs, and Claude tokens are not renewed.

## Validation

```sh
tests/codex-as.sh
tests/account-enrollment.sh
tests/config.sh
tests/account-info.sh
tests/standalone-e2e.sh
tests/reserves.sh tests/reserve-completion.sh
tests/guard.sh tests/claude-guard.sh tests/codex-guard.sh
pre-commit run --all-files
```

Routine tests use temporary HOME directories, fictitious credentials, and fake CLIs. They cover Bash/zsh, TTY, stdin/JSON stdout, link repair and interrupted setup, root auth isolation, concurrent leases, signals, refresh writeback, incorrect permissions, completion secrecy, the configuration file, launcher location and `install.sh`. When Codex 0.161.0 or newer is installed, an additional real Codex offline app-server fixture checks legacy/paginated history, shared SQLite discovery, native writer locks and token refresh writeback through the account link against a local token endpoint, without login or model calls and with zero other requests. A missing or older Codex explicitly skips this check. The usage reserve suites drive the guard's hook answers and status line on files in a temporary directory and both launchers' guarded launches with fake natives on a pseudo-terminal; when Claude Code 2.1.296 or Codex 0.161.0 or newer is installed, they also run its real hook contract offline against a loopback fake of its API, a refused prompt and a stopped tool call included, and skip it otherwise. The status line of a real Claude Code, Codex's trust prompt and a real terminal session are not covered. Native protocol tests do not verify real-account server authorization, quotas or token lifetimes, and do not directly operate the terminal picker/`--last`.
