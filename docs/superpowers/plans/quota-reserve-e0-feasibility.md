# E0: local key-name readers for the stanford policy caches

Node E0, base `f01b4d3`. Research only: no launcher, completion, helper,
installer or suite change. Code and tests live in
[quota-reserve-probes/e0/](quota-reserve-probes/e0/). This is not production
capability or release evidence.

This node never ran the reader against the real files. The coordinator runs
each subcommand once under the ledger's `g0-approval.json` "e0" permission
(key names and verdicts only, credential files never touched). Every cache in
this node is fabricated.

Evidence labels: **N** observation in this node (Linux x86_64, Python 3.15.2
and 3.12.3, temporary HOME, fabricated files); **S** inspected source (codex-rs
`ce21072c`, Cargo 0.167.2, from the PR evidence archive; the Claude Code
2.1.322 binary, SHA-256 `bfb3703a...`, by byte offset); **I** inference;
**U** unproved.

## 1. On-disk shapes (S)

Codex, `$CODEX_HOME/cloud-config-bundle-cache.json` (`cloud-config/src/cache.rs`):

```text
{ "signed_payload": { "version": 1, "cached_at": RFC3339, "expires_at": RFC3339,
                      "chatgpt_user_id": str|null, "account_id": str|null,
                      "bundle": { "config_toml":       { "enterprise_managed": [FRAGMENT] },
                                  "requirements_toml": { "enterprise_managed": [FRAGMENT] } } },
  "signature": base64 HMAC-SHA256 }
FRAGMENT = { "id": str, "name": str, "contents": TOML text }
```

- Native writes it only after a successful bundle fetch, through a temporary
  file renamed into place; the TTL is 1 h. The HMAC key is a constant in the
  source, so the signature proves integrity against accidental edits only.
  Config-server sources use `managed-config-NAMESPACE.json` instead.
- Requirements fragments are `ConfigRequirementsToml` documents. Unknown
  top-level names are ignored (no `deny_unknown_fields`), but
  `[application]` and `[application.network]` deny unknown fields, and each
  layer is validated on its own, so one invalid fragment makes native reject
  the bundle (`application_requirements.rs`;
  `invalid_application_policy_is_rejected_before_layer_overrides`).
- Before validating a cloud layer, native drops `allowed_login_methods`,
  `allowed_chatgpt_workspaces`, `cli_auth_credentials_store` and
  `chatgpt_base_url` (local-only auth requirements), rejects `config_server`,
  and folds the aliases `feature_requirements` and `experimental_network`
  into `features` and `network_proxy` (`requirements_layers/layer.rs`;
  `cloud_auth_requirements_do_not_override_local_or_discard_other_policy`).
- Composition: bundle fragments arrive highest priority first. Native
  reverses them and deep-merges lowest priority first with
  `merge_toml_values` (tables extend recursively, scalars and arrays
  replace), then deserializes the merged table
  (`cloud_config_bundle.rs` `into_layers`, `requirements_layers/stack.rs`,
  `merge.rs`; `application_network_uses_regular_toml_precedence`,
  `cloud_bundle_application_network_uses_managed_precedence`). So
  `[application.network]` without `enabled` means enabled only when no
  fragment sets `enabled`. The whole-table rule (`merge_unset_fields`) is
  called only from tests at this commit.
- Config fragments are `EnterpriseManaged` layers, precedence 15, also
  reversed into lowest-first order (`cloud_config_layers.rs`). Session
  flags (`-c`, precedence 30) override them, so a config-layer key never
  defeats a codex-as or gate override; requirements constrain instead.

Claude Code 2.1.322, in the account's `CLAUDE_CONFIG_DIR`:

- `remote-settings.json` is a bare settings object. The disk loader (near
  offset 284092448) reads up to 8 MiB, needs a non-array object and drops
  keys that start with `$` except `$schema`. A trust gate, an organization
  pin and a separate helper-consent sidecar decide whether the stored copy
  applies; the reader opens none of them.
- `policy-limits.json` is a bare object parsed by a zod schema (near offset
  287881255) with top-level names `restrictions` (a map of name to
  `{allowed}`), `compliance_taints`, `monitoring_notice`, `defaults`,
  `core_skills`, `artifact_create_notice` and `upcoming_minimum_version`.
  Unknown names are dropped (I: zod's default object behaviour). A separate
  stamp file records identity and HIPAA evidence; the reader does not open it.

## 2. The reader

[read_keys.py](quota-reserve-probes/e0/read_keys.py) (`python3 -I -B`,
stdlib, Python 3.11+) has two subcommands and loads its modules by path: the
kit loader, codex-as `check.py` and `layout.py`, and the reviewed QC harness
`qc/n1_harness.py`.

| Step | Resolves | Opens | Retains |
| --- | --- | --- | --- |
| `codex` | `codex_home/accounts/NAME` via `Layout`; `root_check`, owned private directories; no preflight, lease or credential | the cache only; lists the home to count `managed-config-*.json` | key paths (array items `[]`, id-like names `{id}`) with types and counts; shape; identity-field presence; age, TTL, expiry; per fragment: TOML parses, requirement names (others counted), `[application.network]` state (unset, true and false kept apart), forced keys named, cloud-ignored auth names, alias conflict, `features.use_agent_identity` state; D14 summary on the composed fragments |
| `claude` | `<secrets_dir>/native-claude-accounts/NAME/config` via QC `AccountPaths`, walked from `/` with `O_NOFOLLOW` | `remote-settings.json`, `policy-limits.json` | top-level key names, QC classification per scope, verdicts; env names as two counts; `restrictions` and `defaults` names (with a truncation count) |

Besides the files in the table, only the kit loader's own configuration file
is read, as every launcher reads it; it holds no secret.

Never retained: values, fragment ids, names and contents, user or account
ids, the signature, timestamps, env names; for Claude also no value type,
count or array length (the approval is key names and verdicts only). The
result is scanned before it is written: JWT-, UUID- and token-shaped runs in
its text, and known values (every document string of 12 or more characters,
nested TOML names, env names of 8 or more characters and the resolved paths,
minus the fixed vocabulary the result is built from) matched against whole
result strings and whole key-path segments, paths also as substrings. Names
are the only document data the reader places in its result, always whole or
as a path segment, so a requirement name that contains a nested feature name
(`allow_remote_control` beside `[features] remote_control`) is not a finding.
On a finding the matches are redacted, the findings are recorded and the
exit code is 3; if anything remains, only the findings are written.

File checks (N): lstat first; a symlink, non-regular file, file of another
owner, second hard link, group or other permission bits (Claude: anything but
0600, per D15) or a file over 1 MiB is reported and never opened. The open
uses `O_NOFOLLOW | O_NONBLOCK`, plus `O_NOATIME` on Linux so the access time
stays. The opened inode must match the checked one, the file is read once
with a bound, and it must be unchanged afterwards by fstat and a second lstat.
Duplicate JSON keys, non-finite numbers and invalid UTF-8 count as parse
failures, never echoed.

One-shot: `--out DIR` must exist, be owned with no group or other bits, not be
a symlink, and lie outside every Git work tree, the repository, `codex_home`
and `secrets_dir`. `e0-STEP.attempt` (0600, `O_EXCL`) is written before the
first target file is looked at, then `e0-STEP.result.json`. Either present
refuses (exit 4). Earlier refusals write nothing (exit 2); a marker that
could not be created at all (for example `ENOSPC`) is one of them. Exit 0
means a clean result was written; exit 3 means the marker exists but no clean
result does (a marker or result write that failed part way, an error during
the read, or a sanitizer finding). No `OSError` escapes as a traceback. The
marker is per directory only: the reader writes nothing in account state, so
a second directory could repeat a step, and the permission's "once" is the
coordinator's procedure (section 5).

## 3. Verdict rules

Claude, per top-level key of `remote-settings.json` (the QC review, unchanged):

| Key | native | bearer | QC reason |
| --- | --- | --- | --- |
| `env` not an object, or a name in `REFUSED_ENV` / `SETTINGS_ENV_REFUSED` | refused | refused | `policy_conflicting_env` |
| `policyHelper`, `policyHelpers` (present) | refused | refused | `policy_helper_unevaluable` |
| `SETTINGS_REFUSED` (present) | refused | refused | `policy_conflicting_auth` |
| `SETTINGS_ACTIVE` with a non-empty value | active | accepted | `policy_command_unevaluable` |
| `forceLoginMethod` other than `claudeai` | refused | refused | `policy_login_method_incompatible` |
| `forceLoginOrgUUID` not null | unknown | unknown | `policy_organization_not_compared` |
| any other key whose object value directly holds a key of the rows above (not `$`-prefixed) | unknown | unknown | `server_policy_possible_envelope` |
| anything else | unknown | unknown | none (check_policy accepts it) |

A scope is refused when any key is refused (native: or active); unevaluated
when only the organization pin (comparing it needs the registration file,
which the permission excludes) or a possible envelope is open; otherwise
accepted. The envelope row exists because the traced loader applies the bare
object (section 1): a wrapper around reviewed keys would mean the on-disk
shape differs from that trace, which the reader does not guess at. QC
reviewed no `policy-limits.json` key, so any key refuses both scopes
(`policy_limits_unreviewed_keys`, D15) and `{}` is accepted. Each key carries
`in_native_2_1_322_schema` for the later review. An unreadable, unsafe,
unparsable or non-object file refuses both scopes (`server_policy_*`). An
absent file is accepted.

Codex, D14 for this cached bundle only. The fragments are composed as native
0.167.2 composes them (section 1: cloud-ignored auth names dropped, aliases
folded, reverse bundle order, recursive table merge), and the summary is
read from the composed tables; the per-fragment states are kept so the
composition can be recomputed.

- `refuse` when the composed `[application.network]` is enabled (present,
  `enabled` true or unset), the composed requirements set `model_provider`,
  `model_providers` or `enforce_residency`, a fragment sets `config_server`
  (native rejects the bundle: `config-server-in-cloud-layer`), the composed
  `sqlite_home` or `check_for_update_on_startup` differs from codex-as's
  value (`codex_home`, false), the composed requirements set
  `features.use_agent_identity = true` (`forces-agent-identity`), or the
  composed config fragments do (`enables-agent-identity`; codex-as refuses
  that feature in the user configuration);
- `chatgpt_base_url` and `cli_auth_credentials_store` in a cloud fragment are
  listed in `cloud_ignored_names` and do not refuse, because native 0.167.2
  drops them from cloud layers (S). A native that honored them would make
  both blockers (I);
- `unevaluable` for an unrecognized shape, a cache version other than 1
  (native ignores the cache), an unparsable fragment, both spellings of an
  aliased field in one fragment, an invalid `[application]` table or
  `use_agent_identity` value in any fragment or in the composition, or an
  absent, unsafe or unparsable cache;
- otherwise `no-blocker-in-cache`.

Limits of the Codex rule: `sqlite_home` is compared with `codex_home` only.
codex-as forces `shared_sqlite()`, which is the user `config.toml`
`sqlite_home` when that is set; the permission excludes `config.toml`, so a
pin equal to a user-configured path reads as `pins-sqlite-home`
(over-refusal, I). Domain names under `[application.network.domains]` are not
validated (native rejects malformed ones per layer); an enabled network
refuses either way.

## 4. Offline evidence (N)

`python3 -I -B docs/superpowers/plans/quota-reserve-probes/e0/test_read_keys.py`:
41 tests pass under Python 3.15.2 and `/usr/bin/python3` 3.12.3. Every
fabricated value contains `seed`, and every result is checked to contain no
`seed` in any case. Covered:

- Codex: key paths and types; each D14 reason, compatible pins, cloud-ignored
  auth names; composition (`[application]` over `[application.network]`
  refuses, higher domains over lower `enabled = false` finds no blocker,
  explicit true over false, the higher-priority pin wins either way) and the
  per-fragment unset/true/false states; invalid `[application]` tables;
  `use_agent_identity` in requirements (both spellings, overridden), in
  config, invalid, and an alias conflict; stale and expired caches; unknown
  and id-like keys; unrecognized shapes, version 2, bad TOML and bad JSON
  (malformed, duplicate key, NaN, invalid UTF-8).
- Scan: requirement names beside nested feature names
  (`allow_remote_control` with `[features] remote_control`;
  `allow_browser_and_computer_use` and `[computer_use]` with config
  `[features] computer_use`) exit 0 with the names intact; a document value
  that reaches a key path is redacted and exits 3.
- Marker or result write failures: nothing written and exit 2 when the
  marker was not created, exit 3 when it was.
- Claude: `{}`; every rule in both scopes; env counts; organization pin; a
  wrapper around reviewed keys; unreviewed policy-limits keys; restriction
  and defaults names without values, types or counts; non-object and
  malformed files; absent files.
- Both: symlink (target never opened), FIFO, wrong mode, hard link,
  oversize; append and replacement during the read; inode, mtime and atime
  unchanged.
- An audit hook shows that only the target files, DIR, the account
  directories and the kit configuration path are opened, never `auth.json`,
  the codex-as credential,
  `config.toml`, `.credentials.json`, `account.json` or `.claude.json`.
- No socket is created; unsafe homes, directories and out directories, used
  markers and other aliases refuse before anything is written; the command
  line runs end to end in a child process.

## 5. Coordinator procedure

With the reviewed checkout, once each:

1. Confirm that no codex-as or claude-as session of `stanford` is running
   (no native Codex or Claude Code process with that account's home). A
   session that rewrites a cache during the read consumes the step with
   `changed-during-read` or `replaced-during-read`.
2. Use one directory for both steps and never a second one, since the marker
   is per directory:

```sh
mkdir -m 0700 "$HOME/quota-e0-live"
/usr/bin/python3 -I -B <checkout>/docs/superpowers/plans/quota-reserve-probes/e0/read_keys.py codex --alias stanford --out "$HOME/quota-e0-live"
/usr/bin/python3 -I -B <checkout>/docs/superpowers/plans/quota-reserve-probes/e0/read_keys.py claude --alias stanford --out "$HOME/quota-e0-live"
```

Each prints one summary line (outcome, verdict, findings, result file name;
no path). Record both exit codes. Archive the directory into the ledger at
once, then remove it.

## 6. Limits and open items

- U: whether the real cache matches the traced v1 shape; the reader reports
  `unrecognized` or `cache-v1-extra-keys` instead of guessing.
- The bundle is a snapshot about a day old (live diagnosis): the live bundle
  may differ. Local `/etc/codex` requirements and MDM are not read; native
  composes them around the cloud layers (`loader/application.rs`), so they
  can change the effective values (S), and they are where native honors the
  cloud-ignored auth names.
- Not checked: the signature, and whether the cache identity equals the
  codex-as owner. Both need either the HMAC constant or `auth.json`, and
  neither changes the D14 answers.
- `forceLoginOrgUUID` is reported, not compared. The N1 rerun's evaluator,
  which reads the registration, must compare it.
- Policy-limits semantics stay unreviewed: E0 supplies the key and
  restriction names that node C/A's reviewed list needs. Restriction values
  (`allowed`) are not retained, by the permission.
- Which requirement pins actually conflict with a running native is I. The
  verdict accepts `sqlite_home` and `check_for_update_on_startup` pinned to
  codex-as's value (section 3) and refuses every other effective forced key,
  even one equal to the value the gate would force.
- `sqlite_home` is compared with `codex_home`, not with a user-configured
  shared SQLite path (section 3), so a matching pin can over-refuse (I).
- The verdicts predict LX2 and N1-bearer feasibility for this account class.
  Refusing protected mode for edu or enterprise accounts remains a user
  decision (G0 section 1).
