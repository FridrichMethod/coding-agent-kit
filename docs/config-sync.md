# AI configuration sync runtime and contract

Installation and login orchestration remain shell and PowerShell. The three
existing `*-sync` entrypoints delegate configuration/file operations to the
small `lib/config_sync.py` backend. It uses Python's standard-library JSON
implementation and `tomlkit` for syntax-aware TOML edits; no AWK/regular-expression
TOML parser or alternate jq merge implementation remains.

## Explicit provisioning

AI sync requires Python 3.11 or newer and the exact pure-Python `tomlkit` version
and wheel hash in `requirements-sync.txt`. On each checkout/device, run one of:

```sh
./setup-sync.sh
# Or select a particular installed interpreter, including a path with spaces:
./setup-sync.sh --python /path/to/python3
```

```powershell
./setup-sync.ps1
# Or:
./setup-sync.ps1 -Python 'C:\path with spaces\python.exe'
```

Setup creates the ignored, checkout-local `.venv-sync/`, installs only the
pinned wheel, and validates the runtime. It does not require activation, root,
global pip installs, conda activation, or a new shell-startup PATH entry. It
refuses an unrelated directory or a linked virtual environment. It does not
install Python itself; select an already installed supported interpreter.

Profiles, installers, and automatic updates **never** install or download
dependencies. A missing runtime, or a changed required parser version after a
pull, fails with a request to rerun explicit setup. An offline host can provision
from a previously downloaded matching wheel using pip's normal offline options
(for example `PIP_NO_INDEX=1` and `PIP_FIND_LINKS=/path/to/wheels`).

The shell wrappers resolve their own Stow symlink chains to find the checkout
and its `lib/` directory. They use `.venv-sync/bin/python` on Unix or
`.venv-sync/Scripts/python.exe` under native Windows/Git Bash. An explicit
`DOTFILES_SYNC_PYTHON` **executable path** overrides this lookup, useful for CI
or a separately provisioned host-local environment. An invalid override fails;
it never falls back silently to another interpreter. Native Windows paths are
converted for Git Bash's executable lookup. Backend paths and arguments remain
quoted, and Python runs with `-I -B -X utf8` (no user-site/PYTHONPATH injection or
bytecode cache writes, and UTF-8 diagnostics for Windows paths).

The clone must remain present for its stowed wrappers to work, just as for other
Stow-managed files. Re-provision rather than copying virtual environments to a
different OS, Python installation, or checkout location.

## Helper API

```text
codex-config-sync [--check | --migrate-portable] PORTABLE LIVE
claude-settings-sync [--check] PORTABLE LIVE
codex-rules-sync [--check] PORTABLE LIVE
```

All paths are explicit; helpers do not infer a user's real home. `--check`
validates the runtime, both selected inputs and the complete serialized merge
without creating target directories, temporary files, or changing any input.
It permits an absent live file and validates a legacy live symlink's contents
without replacing it. This is the installers' preflight interface. It is not a
promise that a later write will succeed: permissions or files can change after
preflight, and a filesystem can run out of space.

Missing portable input, unreadable/nonregular input, dangling live symlinks,
malformed JSON/TOML, and invalid required portable fields fail closed. Empty live
JSON is treated as a fresh host; nonempty malformed JSON is rejected. Empty TOML
is a valid fresh document. Empty portable rules are rejected. Duplicate JSON
keys and non-JSON constants such as `NaN` are rejected rather than normalized.

Ordinary sync treats the portable source as read-only: bytes, permissions and
mtime are untouched. Passing the same regular file or two hardlinks as portable
and live is rejected. A live symlink to the portable source is supported for
migration to a mutable regular file.

## Merge ownership

Codex ownership, required types, and retired paths are centralized in
`CODEX_POLICY` and `CODEX_RETIRED` in `lib/config_sync.py`. The current owned
fields are:

| TOML path | Owned required keys |
| --- | --- |
| Top level | `model`, `model_reasoning_effort`, `plan_mode_reasoning_effort`, `personality`, `default_permissions`, `approval_policy`, `approvals_reviewer`, `project_doc_fallback_filenames` |
| `features` | `network_proxy`, `multi_agent`, `memories` |
| `permissions.workspace-net` | `description`, `extends` |
| `permissions.workspace-net.network` | `enabled`, `allow_local_binding` |
| `permissions.workspace-net.network.domains` | `"*"` |
| `memories` | `generate_memories`, `use_memories` |

Portable values replace these paths only. Other live keys, tables, comments,
multiline strings and arrays of tables are retained by TOMLKit's document model.
Formatting of edited values can change; the library may also normalize some
unusual out-of-order array-of-tables layouts. Semantic no-ops do not replace the
file. Retired `sandbox_mode`, the whole `sandbox_workspace_write` table, and
`features.js_repl` are removed. Unknown portable keys are not silently deployed
as shared policy.

Claude deep-merges objects with portable leaf values winning. Arrays, including
permissions and hooks, replace wholesale, even when empty. Live-only fields such
as `model` and `permissions.additionalDirectories` remain. `false`, `null`, and
numeric values remain distinct. Portable `permissions.allow` and `permissions.ask`
must be string arrays, and portable `model` is rejected to protect per-host model
selection. This refactor does not change the contents of the portable policies
or the separate Node-based Claude hooks.

Codex rules remain an opaque, authoritative byte copy: the backend does not
parse, reformat or edit rule syntax. Siblings such as `default.rules` are untouched.

## Legacy portable cleanup

Older Stow links may have allowed runtime state to accumulate in tracked Codex
input. Ordinary sync now materializes the live file without cleaning its source.
To explicitly clean that known legacy state:

```sh
common/codex/.local/bin/codex-config-sync --migrate-portable \
    common/codex/.codex/config.toml /path/to/live/config.toml
```

Migration first retains portable-only runtime state in the live document;
existing live runtime values win conflicts. It then applies owned portable
policy and removes retired paths. Only after the live snapshot is safely
materialized does it rewrite the portable baseline to owned keys. This also
preserves source-only runtime state when live is already a regular file or is
absent, not just when it remains a legacy symlink. The portable migration target
must itself be a regular file, not a symlink. Review the resulting tracked diff;
the command intentionally modifies the source and never runs automatically.

## File safety and test boundaries

Each changed file is serialized and validated before writes, staged in an
adjacent temporary file, flushed, and replaced with `os.replace`. There is no
preliminary unlink of an existing live symlink. Failed temporary creation,
writing or replacement leaves the prior file/link in place and cleans the
temporary file. Codex config is mode `0600`, Claude settings/rules are `0644`
on Unix. Windows uses native replacement behavior and inherited directory ACLs;
Unix permission bits are not an ACL policy there.

Unchanged regular files keep their inode and mtime; Unix mode drift may still be
corrected. The backend checks for observed content changes before replacement
and asks for retry instead of overwriting them. This is not an atomic
compare-and-swap and cannot eliminate a simultaneous application's final
read/replace race. Close the configuration editor during manual migrations.

Safety is per file, not a transaction across all AI files. Explicit migration
writes live first, then portable; failure of the second write leaves the original
portable source intact and can be retried. Installer state must only acknowledge
a completely successful install.

Tests use the standard library, disposable targets, and injected write failures:

```sh
.venv-sync/bin/python -I -B -m unittest discover -s tests -p test_config_sync.py -v
./tests/codex-config-sync.sh
./tests/ai-config-sync.sh
```

```powershell
& ./.venv-sync/Scripts/python.exe -I -B -m unittest discover -s tests -p test_config_sync.py -v
```

Native backend tests run on all three OSes; tests specific to POSIX permission
bits/FIFOs are explicitly marked. Windows symlink tests require the CI runner's
symlink privilege. These tests do not establish OpenSSH network-logon trust or
Task Scheduler behavior; those are separate installer/update integration checks.
