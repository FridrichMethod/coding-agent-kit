# Documentation

These guides describe how to install and use coding-agent-kit, and every
rule its launchers and helpers follow.

| Guide | What it covers |
| --- | --- |
| [Overview](../README.md) | What the kit does, its requirements, a quick start and the commands |
| [Installation](install.md) | Requirements, the managed install (channels, update, repair, lock, exit codes), a checkout install, linking or copying the files |
| [Account launchers](account-launchers.md) | Adding accounts, own options, usage reserves and the reserve guard, account layout, security boundaries, account metadata, completion, concurrency and manual repair |
| [codex-as](codex-as.md) | Codex argument rules, daemon takeover, version compatibility, the Codex details of usage reserves and the remaining differences from native Codex |
| [Configuration](configuration.md) | The configuration file: location, keys, `[usage_reserve]`, file permissions, messages, `--show-config`, moving `codex_home` and `secrets_dir` |
| [Claude Code helpers](helpers.md) | The status line, session tokens, notifications, the Git hook checks, wiring them into `settings.json`, and the colour policy of the launchers and the status line |
| [Development](development.md) | Tests, CI and versioning |
| [Changelog](../CHANGELOG.md) | Every release, with its breaking changes and moved paths |
| [Agent guide](../AGENTS.md) | Repository rules: the public contract, dependencies, conventions and releases |

The launchers' `--help` points at [account launchers](account-launchers.md).
