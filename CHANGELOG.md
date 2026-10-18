# Changelog

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
