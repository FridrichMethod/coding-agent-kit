# Configuration

`codex-as` and `claude-as` read one optional TOML file. Without it they use
the defaults below. Its top-level keys move paths and name executables. Two
tables add opt-in behavior: [usage reserves](#usage-reserves)
(`[usage_reserve]`) and the [Claude login mode](#claude-login-mode)
(`[claude_auth]`). A file whose reserves all resolve off and whose Claude
accounts all use `setup-token` launches exactly as no file.

`lib/coding-agent-kit/config.py` is the only code that reads it, and the only
launcher or completion code that reads `HOME`. Each run loads it once, after
the Python version check: every launch, `--list`, `--show-config` and
therefore every completion. `--help`, `--version` and a run without
arguments are plain shell and never read it, nor does a `codex-as` account
name that fails the name check.

## Location

The first rule that applies wins:

1. `CODING_AGENT_KIT_CONFIG`, when set and non-empty. It must be an absolute
   path and the file must exist.
2. `$XDG_CONFIG_HOME/coding-agent-kit/config.toml`, when `XDG_CONFIG_HOME` is
   an absolute path. An empty or relative value is ignored.
3. `$HOME/.config/coding-agent-kit/config.toml`.

For rules 2 and 3 a file that does not exist means the defaults, and so does a
path component that is a regular file (for example a file named `~/.config`),
because no file can exist below it. A dangling symlink, at the file or at a
directory on the way to it, is an error, and so is any other failure to open
it, such as a permission denial (an unreadable file or an unsearchable
directory) or a symlink loop: the launchers never fall back to the defaults
when a file may be there. Such a failure therefore stops every launch,
listing and completion until it is fixed, even when no file exists. `HOME`
must be set to an absolute path in every case.

Neither the launchers nor `install.sh` ever create the file.

The account metadata cache (see the account launchers documentation) is not
configured here: it lives in `$XDG_STATE_HOME/coding-agent-kit/accounts/` when
`XDG_STATE_HOME` is an absolute path, else in
`$HOME/.local/state/coding-agent-kit/accounts/`. A `state_dir` key is an
unknown key and only warns.

## Format and keys

The keys are top-level strings, all optional:

```toml
codex = "codex"
claude = "claude"
codex_home = "~/.codex"
secrets_dir = "~/.secrets"
```

This file, with the defaults written out, behaves exactly like no file. Put
the keys before any `[table]` header: TOML assigns the keys that follow a
header to that table. Any table but `[usage_reserve]` and `[claude_auth]`
counts as one unknown key, and the keys in it are not read; a key that lands
in one of those two tables by mistake is an unknown key there.

| Key | Default | Used by | Value |
| --- | --- | --- | --- |
| `codex` | `"codex"` | `codex-as` | the Codex executable |
| `claude` | `"claude"` | `claude-as` | the Claude Code executable |
| `codex_home` | `"~/.codex"` | `codex-as` | the root Codex home; accounts live in `codex_home/accounts` |
| `secrets_dir` | `"~/.secrets"` | both | the directory of `codex-NAME.json` and `claude-NAME` credentials |

Both launchers validate every key, so an invalid `codex_home` also stops
`claude-as`.

Rules for every key:

- The value is a non-empty string without control characters (U+0000 to
  U+001F and U+007F to U+009F, so C1 controls such as CSI are refused too),
  which would reach the terminal raw in `--show-config` and in error
  messages.
- `~/` at the start expands from `$HOME`. `~` alone and `~user` are not
  accepted.

`codex` and `claude`:

- A bare name made only of `A-Z a-z 0-9 . _ + -` is looked up on `PATH` when
  the launcher runs.
- Anything else must be an absolute path or start with `~/`. The path names
  one executable file; it is not a command line and takes no arguments.

`codex_home` and `secrets_dir`:

- Absolute, or starting with `~/`.
- No `..` component.
- Normalized only lexically: repeated slashes (a leading `//` included, which
  Linux and macOS read as `/`), `.` components and a trailing slash are
  dropped. Symlinks in the value are kept as written and never resolved,
  because the launchers store these paths in absolute account links and
  compare the links' text on every launch.
- They must differ, and neither may lie inside the other. This check is
  lexical too, so it does not see two spellings of one directory through a
  symlink, or through case on a case-insensitive file system.
- `secrets_dir` must also differ from the account metadata directory below
  (`coding-agent-kit/accounts` in the state directory), whose files carry the
  Codex credentials' names; above or below it is fine. The launchers also
  never read or write that directory when it is `secrets_dir` through a link.
- The parent of each must be a directory you own that neither group nor others
  can write: `codex-as` checks both parents, `claude-as` the parent of
  `secrets_dir`. With the defaults both parents are `$HOME`. A parent that
  does not exist, also one below a regular file, is named in the error; the
  launchers never create it.
- The directories themselves follow the rules in
  [account launchers](account-launchers.md): `codex_home` owned and not
  group/other writable, `secrets_dir` owned with mode 0700.
- The account root is always `codex_home/accounts`; it is not configurable.

An unknown key prints a warning and the run continues, so a file written for
a newer kit still works with an older one. `python3` is always found on
`PATH`; the interpreter is not configurable.

## Usage reserves

A usage reserve keeps a percentage of an account's 5-hour or weekly limit
unused: a launch with a reserve on runs the native CLI in protected mode,
which pauses model work when the remaining percentage of either window falls
to its reserve. The [account launchers](account-launchers.md#usage-reserves)
documentation describes protected mode, what it supports and what it costs.
The two windows are set and resolved separately, never combined:

```toml
[usage_reserve]
five_hour = 20
weekly = 30

[usage_reserve.codex_accounts.borrowed]
weekly = 40

[usage_reserve.claude_accounts.borrowed]
five_hour = "off"
```

- `five_hour` and `weekly` take a number from 0 through 100 (a TOML integer
  or float) or the string `"off"`. Booleans, NaN, infinities, other strings
  and other types, and numbers outside the range are refused. A float is
  read as the shortest decimal that gives the same float, so `29.9` stays
  29.9; `-0` is 0.
- `[usage_reserve]` holds the global values.
  `[usage_reserve.codex_accounts.NAME]` and
  `[usage_reserve.claude_accounts.NAME]` hold one account's values, keyed by
  launcher: a Codex and a Claude account with the same name are different
  accounts. `NAME` follows the account-name rule (ASCII letters, digits, `_`
  or `-`, no leading `-`); the account need not exist.
- Each window resolves on its own, and the first layer that sets it wins:
  the invocation's `--reserve-5h` or `--reserve-weekly` flag, then the
  account's table, then `[usage_reserve]`, then off. A missing field
  inherits; an explicit `"off"` turns the window off even when a broader
  layer sets a percentage. The flags change only that launch and are never
  written anywhere.
- 0 is an enabled reserve that pauses at exhaustion; 100 never lets model
  work start. With both windows off the launch is unchanged.

With the file above, `codex-as --reserve-5h 10 borrowed` resolves to a 5-hour
reserve of 10% (the flag) and a weekly one of 40% (the account table), and a
plain `codex-as borrowed` to 20% and 40%. `claude-as borrowed` resolves to
5-hour off and weekly 30%.

Reserves need coding-agent-kit 0.8.0 or newer. An older release does not
know these tables: it warns `unknown key usage_reserve ignored` and launches
without any protection, so a shared file does not protect a host with an
older kit. It also ignores `[claude_auth]` and treats every Claude account
as a `setup-token` one.

## Claude login mode

Each Claude account uses a `claude setup-token` token by default. An account
can instead use Claude Code's own login, which usage reserves need:

```toml
[claude_auth.accounts.personal]
mode = "native-login"
```

- `mode` is `"setup-token"` (the default, also when the field, the account
  table or `[claude_auth]` is missing) or `"native-login"`. `NAME` follows the
  account-name rule.
- A `native-login` account keeps Claude Code's login in its own fixed
  configuration directory, `<secrets_dir>/native-claude-accounts/NAME/config`,
  which `claude-as` passes as `CLAUDE_CONFIG_DIR`, next to its registration
  `<secrets_dir>/native-claude-accounts/NAME/account.json`. The location is
  not configurable. `claude-as --enroll-native NAME` creates both (see
  [native login](account-launchers.md#claude-native-login)); the registration
  records the directory, so moving `secrets_dir` later needs a new
  enrollment.
- Setting the mode changes no files. A `native-login` account that is not
  enrolled refuses to launch with the enrollment command. An account switched
  back to `setup-token` uses its `<secrets_dir>/claude-NAME` token when it
  has one; an enrolled native account without one is refused with the
  setting that selects it, and is never offered a setup-token.

## File permissions

The file decides which executable runs with your credentials and where your
tokens are written, so anyone who can change it can capture them. The
launchers therefore open it once, following symlinks, and check the opened
file itself:

- It is a regular file. A symlink that resolves is fine; the checks apply to
  its target.
- Its owner is you or root.
- Others cannot write it.
- The group can write it only when the group is your user private group: your
  login group (the group ID in the password database, which `id -g` shows in
  a login shell; `newgrp` and `sg` do not change it), named exactly like your
  user, with no other member, neither listed in the group nor an account
  whose primary group it is. Debian and Ubuntu create such a group for each
  user and then commonly use umask 002, which makes new files 0664. Any other
  group-writable file is refused, because the other members of that group
  could edit it.
- A group-writable file that carries an access ACL (POSIX or NFSv4) is
  refused even in your private group: with an ACL the group bits show the ACL
  mask, which named users and groups can use. Remove the ACL with
  `setfacl -b FILE`, or the group write with `chmod g-w FILE`. A directory
  default ACL gives every new file such an ACL. On NFSv4 every group-writable
  file and directory counts as having one; see below.
- It holds at most 64 KiB of UTF-8 text.

Whoever can write a directory on the way can replace the file, so the
launchers also check, once each, every directory from the file's parent up
to and including `$HOME` (or its resolved path, when `$HOME` is a symlink)
when the file lies below `$HOME`, otherwise up to `/`, both on the path as
the location rules give it and on its resolved path (after symlinks):

- Each directory is owned by you or root.
- Others cannot write it, and the group only under the same private-group
  and ACL rule as the file, unless it is sticky like `/tmp`, where other
  users cannot rename or remove what they do not own. An Ubuntu `~/.config`
  with mode 0775 therefore passes, also in a `newgrp` or `sg` shell (not on
  NFSv4; see below), and so does a stowed link into a checkout you own.
- A symlink on the path as given, the file's own included, is owned by you or
  root.

Limits: the private-group rule sees only the accounts the host can list
(`getent passwd`), so users of a directory service that does not enumerate
(sssd or LDAP with enumeration off, the default) are not seen. A run lists
them at most once, and only when a group-writable file or directory is in
your login group. ACLs on macOS (`chmod +a`) are not checked, nor NFSv4
grants that the mode bits do not show. Only the path as given and the
resolved path are checked, not intermediate symlinks or their directories.

On an NFSv4 mount, the Linux client reports an ACL on every file and
directory of a server that supports ACLs, whatever their mode. There every
group-writable file or directory on the way to the configuration file is
refused, even in your private group, with the
`group-writable with an access ACL` message: an Ubuntu home with umask 002
is refused on the file, then on `~/.config/coding-agent-kit`, then on
`~/.config`. `setfacl -b` does not work on NFSv4; remove the group write with
`chmod g-w` on each path the message names, for example
`chmod g-w ~/.config ~/.config/coding-agent-kit ~/.config/coding-agent-kit/config.toml`.

The launchers never change any mode or owner; fix it yourself, for example
with `chmod go-w FILE`.

## Messages

`TOOL` is `codex-as` or `claude-as`, and `FILE` the path from the location
rules. Warnings are yellow and errors red under the launchers' colour policy
(see the README). Every message is one line on stderr. No error prints a
traceback or any of the file's contents; a TOML syntax error names only its
position.

Warning, the run continues:

```
TOOL: FILE: unknown key KEY ignored
```

A key that is not a bare TOML key is shown JSON-quoted, for example
`unknown key "a b" ignored`. A key inside `[usage_reserve]` or
`[claude_auth]` is shown dotted, each part that is not bare JSON-quoted, for
example `unknown key usage_reserve.codex_accounts.work."five hour" ignored`.

Errors, exit 1, nothing launched:

```
TOOL: HOME must be set to an absolute path.
TOOL: VALUE: CODING_AGENT_KIT_CONFIG must be an absolute path.
TOOL: FILE: not found (CODING_AGENT_KIT_CONFIG).
TOOL: FILE: dangling symlink.
TOOL: FILE: dangling symlink COMPONENT.
TOOL: FILE: cannot be opened (REASON).
TOOL: FILE: not a regular file.
TOOL: FILE: owned by another user.
TOOL: FILE: writable by other users.
TOOL: FILE: group-writable, and the group is not your private group.
TOOL: FILE: group-writable with an access ACL; remove the ACL (setfacl -b) or the group write (chmod g-w).
TOOL: FILE: directory DIR: REASON
TOOL: FILE: symlink LINK: owned by another user.
TOOL: FILE: cannot be checked (REASON).
TOOL: FILE: larger than 64 KiB.
TOOL: FILE: not UTF-8 text.
TOOL: FILE: invalid TOML at line L, column C.
TOOL: FILE: KEY: must be a string.
TOOL: FILE: KEY: must not be empty.
TOOL: FILE: KEY: must not contain control characters.
TOOL: FILE: KEY: must be a command name ([A-Za-z0-9._+-]+) or an absolute or ~/ path.
TOOL: FILE: KEY: must be an absolute or ~/ path.
TOOL: FILE: KEY: must not contain a .. component.
TOOL: FILE: secrets_dir: must differ from codex_home.
TOOL: FILE: secrets_dir: must not be inside codex_home.
TOOL: FILE: codex_home: must not be inside secrets_dir.
TOOL: FILE: secrets_dir: must differ from the account metadata directory DIR.
TOOL: FILE: TABLE: must be a table.
TOOL: FILE: KEY: must be a number from 0 through 100, or "off".
TOOL: FILE: TABLE: invalid account name (ASCII letters, digits, _ or -; no leading -).
TOOL: FILE: claude_auth.accounts.NAME.mode: must be "setup-token" or "native-login".
```

A syntax error at the very end reads `invalid TOML at end of document.`, and
one the parser reports without a position `invalid TOML.`, as do nesting
deeper than Python's parser can follow and an integer longer than Python
converts (4300 digits). The first `dangling symlink` form is the file
itself, the second a directory on the way to it. The `directory` form names
the directory and one of the file's reasons (`owned by another user.`,
`writable by other users.` or one of the two `group-writable` ones), and
`cannot be checked` a directory or ACL that could not be read. In the
reserve and login-mode errors `TABLE` and `KEY` are dotted as in the warning:
`TABLE` is `usage_reserve`, `usage_reserve.codex_accounts`,
`usage_reserve.claude_accounts`, `claude_auth`, `claude_auth.accounts`, or one
account's table below them, and `KEY` names the field, for example
`usage_reserve.codex_accounts.work.weekly`. The invalid account name itself
is never printed.

Some launch errors name configured values, and only appear with a
non-default configuration:

```
codex-as: the configured codex executable VALUE was not found.
claude-as: the configured claude executable VALUE was not found.
claude-as: PARENT must be an owned, real directory without group/other write access.
claude-as: SECRETS_DIR must be an owned, real directory with permissions 0700.
```

With the defaults the same errors read:

```
codex-as: Codex is not installed on PATH.
claude-as: real Claude executable not found on PATH.
claude-as: HOME must be an owned, real user directory without group/other write access.
claude-as: .secrets must be an owned, real directory with permissions 0700.
```

The directory errors of `codex-as` already name the full path. A parent that
a launcher checks (above) and that does not exist, `$HOME` included, or that
lies below a regular file, stops it with:

```
TOOL: PARENT does not exist; create it, owned by you and not group or other writable.
```

## --show-config, --list and the account queries

These are the launcher's own queries: `--show-config`, `--list` (`-l`) and
`--labels` accepted only as the single argument, `--info NAME` (`-i NAME`)
only with one account name. The aliases `--list-accounts`,
`--account-labels` and `--account-info NAME` are accepted too; the account
launchers documentation lists every own option. They read metadata only: no
environment or argument checks, no directory is created, no native CLI
starts, no credential is opened and nothing touches the network.

`TOOL --show-config` prints the effective values and exits 0:

```
$ codex-as --show-config
config = /home/u/.config/coding-agent-kit/config.toml (not found; defaults)
codex = codex (/usr/bin/codex)
codex_home = /home/u/.codex
secrets_dir = /home/u/.secrets
$ claude-as --show-config
config = /home/u/.config/coding-agent-kit/config.toml (not found; defaults)
claude = claude (/home/u/.local/bin/claude)
secrets_dir = /home/u/.secrets
```

- The `config` line shows the file that was looked for and one of `found`,
  `not found; defaults` or `from CODING_AGENT_KIT_CONFIG`.
- The executable line shows the value, then the file `PATH` lookup finds, or
  `not found on PATH` for a bare name, or `not an executable file` for a
  configured path that is missing or not executable.
- Paths are shown expanded and normalized, exactly as the launchers use them.
- Reserves and Claude login modes are not shown; a protected launch prints
  the reserves in effect and where each comes from when it starts.
- Warnings go to stderr first. An invalid file prints its error and exits 1.

`TOOL --list` prints one account name per line, sorted, from
`secrets_dir`: the `codex-NAME.json` or `claude-NAME` files that are regular,
owned by you, mode 0600 and not hard-linked. The Bash completions run
`command TOOL --list-accounts 2>/dev/null` for an account argument and offer
nothing else there; a first argument that starts with `-` completes the own
options instead. An invalid configuration or an unsafe `secrets_dir` prints
nothing on stdout and an error on stderr, and exits 1, so completion offers
no names.

`TOOL --labels` prints the same accounts as `NAME<TAB>LABEL` lines (the
email for Codex, the label for Claude), or a bare `NAME` when there is none;
the zsh completions run it instead of `--list-accounts` and show the label as
the description. `TOOL --info NAME` prints the last-seen details of one
account, or exits 1 with `TOOL: no account NAME.` when the listing has no such
account. The account launchers documentation describes both outputs and where
the data comes from.

## Moving codex_home and secrets_dir

Choose both before the first `codex-as NAME`. Each account view under
`codex_home/accounts/NAME` holds absolute links: `auth.json` to
`secrets_dir/codex-NAME.json`, and the shared configuration, sessions,
history and other shared state to `codex_home`. Every launch checks each link's text
and refuses one that differs (`managed link PATH is missing/replaced; preserve
it and restore the link before launch.`), and the launchers never rewrite
them. Changing either value after enrolment therefore stops every existing
Codex account until its links are recreated by hand. `claude-as` keeps no
links; its tokens are the plain files `secrets_dir/claude-NAME`.

To start with other locations:

1. Create the parent directories, owned by you and not group or other
   writable. A missing one stops the launch with `PARENT does not exist`.
2. Write the configuration file, and check it with `codex-as --show-config`
   and `claude-as --show-config`.
3. Enroll with `codex-as NAME` and `claude-as NAME`. The launchers create
   `codex_home`, `codex_home/accounts` and `secrets_dir` (0700) as needed.

Native Codex knows nothing of this file. After moving `codex_home`, export
`CODEX_HOME=<codex_home>` for plain `codex`, the desktop app and IDE clients,
so that the root login, its configuration and its daemon live where
`codex-as` shares them from. `codex-as` accepts an inherited `CODEX_HOME`
equal to `codex_home` or to an initialized account home below
`codex_home/accounts`, and refuses any other. Root help, version, `update` and
`completion` run with `CODEX_HOME=<codex_home>`. Daemon takeover talks only to
a root daemon whose Codex home is `codex_home`; any other daemon is not used,
and native Codex shows its conflict screen instead.
