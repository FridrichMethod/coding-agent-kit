# codex-as

日常用法、首次初始化、新账号登录与补全见 [账号启动脚本](account-launchers.md)。

```sh
codex-as ayting
codex-as work resume --last
codex-as ayting exec --json "检查测试"
```

已有账号直接启动；缺少账号先确认，在空临时 `CODEX_HOME` 中原生 device login，保存完整 JSON 后继续原命令。无需 source 或旧的 /tmp 初始化示例。

## 软链接写回核实

本机审核版本 `codex-cli 0.162.1` 对应官方 tag `rust-v0.162.1`、commit `092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`。

- [storage.rs](https://github.com/openai/codex/blob/092d3acd6bec3e3a14bdc7e7a2810ab628ab759d/codex-rs/login/src/auth/storage.rs#L214) 使用 OpenOptions write/truncate/create 打开 auth.json，随后写入、flush；此路径没有 rename/原子替换或 O_NOFOLLOW，因此沿软链接更新目标。
- AuthManager 的刷新保存调用该 storage.save。导入与备份保存完整原字节，实际刷新格式由 Codex 自己管理；wrapper 不实现 OAuth refresh。
- CLI 支持单次 `-c 'cli_auth_credentials_store="file"'`。交互、resume、fork 添加 `--no-daemon`，避免复用长驻 app-server 的旧认证；exec 保持独立调用。
- 原生 login 会清除已有认证并可能 revoke，所以只在无旧凭据的临时 home 登录，不在共享 home logout/login。
- 根命令的 prompt 后仍可能出现原生子命令；wrapper 同样拒绝这些位置的 login/logout。exec、resume、fork 中的 prompt 与选项值保持原样。
- 切换前按原生 JSON 格式检查完整凭据：拒绝 NaN/Infinity、非 null 的无效 tokens，以及任何已提供但无效的 last_refresh。

这是版本对应实现的静态核实。fake Codex 的写入／原子替换测试只证明 wrapper 逻辑，不能证明真实 Codex 写回；本次没有做真实授权、切换或刷新验证。版本改变会拒绝启动，直到重新核实。

全局 auth 链接没有锁，不能在其他客户端仍运行或挂起时切换。
