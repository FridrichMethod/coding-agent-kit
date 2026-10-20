# Configuration

`codex-as` and `claude-as` read one optional TOML file. Without it they use
the defaults below, which give the same arguments, environment, files and
links as 0.1.0, which had no configuration. The file only moves paths and
names executables; it adds no behavior.

`lib/coding-agent-kit/config.py` is the only code that reads it, and the only
launcher or completion code that reads `HOME`. Each run loads it once, after
the Python version check: every launch, `--list-accounts`, `--show-config`
and therefore every completion. `--help` and a run without arguments are
plain shell and never read it, nor does a `codex-as` account name that fails
the name check.

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
header to that table, which then counts as one unknown key, and the keys in it
are not read.

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
  the launcher runs, as before.
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
- The parent of each must be a directory you own that neither group nor others
  can write: `codex-as` checks both parents, `claude-as` the parent of
  `secrets_dir`. With the defaults both parents are `$HOME`, checked as
  before. A parent that does not exist, also one below a regular file, is
  named in the error; the launchers never create it.
- The directories themselves follow the rules in
  [account launchers](account-launchers.md): `codex_home` owned and not
  group/other writable, `secrets_dir` owned with mode 0700.
- The account root is always `codex_home/accounts`; it is not configurable.

An unknown key prints a warning and the run continues, so a file written for
a newer kit still works with an older one. `python3` is always found on
`PATH`; the interpreter is not configurable.

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
`unknown key "a b" ignored`.

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
```

A syntax error at the very end reads `invalid TOML at end of document.`, and
one the parser reports without a position `invalid TOML.`, as do nesting
deeper than Python's parser can follow and an integer longer than Python
converts (4300 digits). The first `dangling symlink` form is the file
itself, the second a directory on the way to it. The `directory` form names
the directory and one of the file's reasons (`owned by another user.`,
`writable by other users.` or one of the two `group-writable` ones), and
`cannot be checked` a directory or ACL that could not be read.

Some launch errors name configured values, and only appear with a
non-default configuration:

```
codex-as: the configured codex executable VALUE was not found.
claude-as: the configured claude executable VALUE was not found.
claude-as: PARENT must be an owned, real directory without group/other write access.
claude-as: SECRETS_DIR must be an owned, real directory with permissions 0700.
```

With the defaults the texts are unchanged (`Codex is not installed on PATH.`,
`real Claude executable not found on PATH.`, `HOME must be an owned, real
user directory without group/other write access.` and `.secrets must be an
owned, real directory with permissions 0700.`). The directory errors of
`codex-as` already name the full path. A parent that a launcher checks
(above) and that does not exist, `$HOME` included, or that lies below a
regular file, stops it with:

```
TOOL: PARENT does not exist; create it, owned by you and not group or other writable.
```

## --show-config and --list-accounts

Both are the launcher's own queries, accepted only as the single argument.
They read metadata only: no environment or argument checks, no directory is
created, no native CLI starts and nothing touches the network.

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
- Warnings go to stderr first. An invalid file prints its error and exits 1.

`TOOL --list-accounts` prints one account name per line, sorted, from
`secrets_dir`: the `codex-NAME.json` or `claude-NAME` files that are regular,
owned by you, mode 0600 and not hard-linked. The Bash and zsh completions run
`command TOOL --list-accounts 2>/dev/null` and offer nothing else for the
account argument. An invalid configuration or an unsafe `secrets_dir` prints
nothing on stdout and an error on stderr, and exits 1, so completion offers
no names.

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
