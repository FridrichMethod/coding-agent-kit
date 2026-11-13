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
  names are ignored (no `deny_unknown_fields`). `[application.network]`
  without `enabled` means enabled (`application_requirements.rs`).
  `[application]` merges as a whole table, highest priority first, and bundle
  fragments arrive highest priority first (`cloud_config_bundle.rs`,
  `application_requirements_tests.rs`).
- Config fragments are `EnterpriseManaged` layers, precedence 15. Session
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
| `codex` | `codex_home/accounts/NAME` via `Layout`; `root_check`, owned private directories; no preflight, lease or credential | the cache only; lists the home to count `managed-config-*.json` | key paths (array items `[]`, id-like names `{id}`) with types and counts; shape; identity-field presence; age, TTL, expiry; per fragment: TOML parses, requirement names (others counted), `[application.network]` state, pinned forced keys; D14 summary |
| `claude` | `<secrets_dir>/native-claude-accounts/NAME/config` via QC `AccountPaths`, walked from `/` with `O_NOFOLLOW` | `remote-settings.json`, `policy-limits.json` | top-level key names and types, QC classification per scope, verdicts; env names as two counts; `restrictions` and `defaults` names |

Besides the files in the table, only the kit loader's own configuration file
is read, as every launcher reads it; it holds no secret.

Never retained: values, fragment ids, names and contents, user or account
ids, the signature, timestamps, env names. The result is scanned before it is
written (JWT-, UUID- and token-shaped runs; every value of 12 or more
characters from both documents, nested TOML names, env names of 8 or more
characters and the resolved paths). On a finding the matches are redacted,
the findings are recorded and the exit code is 3; if anything remains, only
the findings are written.

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
refuses (exit 4). Earlier refusals write nothing (exit 2). Exit 0 means a
clean result was written. The marker is per directory only: the reader writes
nothing in account state, so a second directory could repeat a step, and the
permission's "once" is the coordinator's procedure.

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
| anything else | unknown | unknown | none (check_policy accepts it) |

A scope is refused when any key is refused (native: or active); unevaluated
when only the organization pin is open, since comparing it needs the
registration file, which the permission excludes; otherwise accepted. QC
reviewed no `policy-limits.json` key, so any key refuses both scopes
(`policy_limits_unreviewed_keys`, D15) and `{}` is accepted. Each key carries
`in_native_2_1_322_schema` for the later review. An unreadable, unsafe,
unparsable or non-object file refuses both scopes (`server_policy_*`). An
absent file is accepted.

Codex, D14 for this cached bundle only:

- `refuse` when the effective `[application.network]` (first fragment that
  has an `[application]` table) is enabled, or a requirement pins
  `chatgpt_base_url`, `model_provider(s)`, `enforce_residency` or
  `config_server`, or pins `cli_auth_credentials_store`,
  `check_for_update_on_startup` or `sqlite_home` to a value other than
  codex-as's (`file`, false, `codex_home`);
- `unevaluable` for an unrecognized shape, an unparsable fragment, an invalid
  `enabled`, or an absent, unsafe or unparsable cache;
- otherwise `no-blocker-in-cache`.

## 4. Offline evidence (N)

`python3 -I -B docs/superpowers/plans/quota-reserve-probes/e0/test_read_keys.py`:
33 tests pass under Python 3.15.2 and `/usr/bin/python3` 3.12.3. Every
fabricated value contains `seed`, and every result is checked to contain no
`seed` in any case. Covered:

- Codex: key paths and types; each D14 reason, compatible pins, whole-table
  precedence, invalid `enabled`; stale and expired caches; unknown and
  id-like keys; unrecognized shapes, bad TOML and bad JSON (malformed,
  duplicate key, NaN, invalid UTF-8).
- Claude: `{}`; every rule in both scopes; env counts; organization pin;
  unreviewed policy-limits keys; restriction names without values;
  non-object and malformed files; absent files.
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
  may differ. Local `/etc/codex` requirements and MDM are not read; they can
  override bundle precedence (I).
- Not checked: the signature, and whether the cache identity equals the
  codex-as owner. Both need either the HMAC constant or `auth.json`, and
  neither changes the D14 answers.
- `forceLoginOrgUUID` is reported, not compared. The N1 rerun's evaluator,
  which reads the registration, must compare it.
- Policy-limits semantics stay unreviewed: E0 supplies the key and
  restriction names that node C/A's reviewed list needs. Restriction values
  (`allowed`) are not retained, by the permission.
- Which requirement pins actually conflict with a running native (for
  example a pin equal to the forced value) is I; the verdict treats any
  gate-forced pin as a refusal.
- The verdicts predict LX2 and N1-bearer feasibility for this account class.
  Refusing protected mode for edu or enterprise accounts remains a user
  decision (G0 section 1).
