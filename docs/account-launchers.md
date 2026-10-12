# Codex／Claude 多账号启动

两个可执行命令由工具各自 Stow package 安装至 `~/.local/bin`，无需 source。小型 Python 标准库辅助程序在工具自己的 `~/.local/lib`；需要 Python 3.11+，不安装依赖、不自建 OAuth、不运行后台服务。

## 日常使用与新增账号

```sh
codex-as ayting
codex-as work resume --last
codex-as ayting exec --json "检查测试"
claude-as work
claude-as work -p "检查测试"
```

已有有效本地凭据直接启动。缺少账号时显示已有账号、询问 `[y/N]`，默认否，不会自动纠正近似名字。确认后原生登录、保存成功，再继续原命令。损坏文件、断链、权限异常不是“账号不存在”，不会覆盖重登。

- **Codex**：在私密空临时 `CODEX_HOME` 中调用 `codex -c 'cli_auth_credentials_store="file"' login --device-auth`。账号 Security 或 workspace admin 必须开启 device login；失败、取消、超时不保存或自动转浏览器。使用官方 URL/code 完成本机发起的授权。[官方认证说明](https://learn.chatgpt.com/docs/auth)
- **Claude**：在临时 `CLAUDE_CONFIG_DIR` 中调用 `claude setup-token`，没有 `generate` 子命令或相同 device code 流程。临时禁用自动浏览器打开，展示原生手工授权链接；若网页返回 code，完整粘贴 `code#state` 并 Enter。浏览器 Cookie 不随 CLI 配置目录隔离，应在浏览器中选择目标账号。
- Claude 没有机器可读的 token 导出接口。本机 `2.1.296` 适配器捕获完整成功 section、去重、等待成功退出后保存，不回放原始输出。流式处理 ANSI／OSC／分块／换行，缓冲有界。版本未知、捕获异常或失败时，另询问是否运行官方命令并无回显粘贴，默认否。**人工回退中官方命令会展示 token，wrapper 的粘贴输入不回显。**

Claude setup-token token 通常有效一年，不自动续期；适用于模型请求，不承诺 Remote Control 或 claude.ai connectors，本地 MCP 仍可使用。[官方说明](https://code.claude.com/docs/en/authentication)

确认与登录 I/O 使用独立 `/dev/tty`，保留最终 stdin/stdout/stderr、cwd、参数、退出码。无控制终端时缺少账号直接拒绝，不提供 `--yes`。wrapper 的 `--help` 及 `NAME --help`／根级版本查询不触发初始化或登录。

## Codex 首次初始化

**先退出当前 Codex，关闭其他终端、挂起进程、桌面端和 IDE 扩展，再运行：**

```sh
codex-as ayting
```

普通 `~/.codex/auth.json` 触发单独初始化确认。与唯一已存账号逐字节匹配时提示该别名；否则询问当前文件应归档到哪个本地别名。文件不能证明 Keychain／IDE 当前身份，不会自行猜测。

若指定别名已有不同快照，额外确认更新并先备份两份。备份在私密 `~/.secrets/.codex-as-backup-*`，不参与补全。自己拥有的 `.codex` 仅在这次明确初始化时收紧至 0700；已有 `.secrets` 权限异常会拒绝自行 chmod。

初始化创建 `~/.codex/codex-as.initialized`（0600）及受管理链接；标记在替换前写入，中断会安全停止。它不是锁或事务日志。之后共享原有 config、skills、MCP、sessions、history；凭据为完整 `~/.secrets/codex-NAME.json`，固定共享 home、强制文件认证。

已初始化后 auth.json 变成普通文件或消失，一律停止并保留现场。**不要删标记重启，也不要用旧快照覆盖它。**先关闭客户端，把最新普通文件另存为私密恢复备份，核实归属后再恢复账号文件和链接；归属不明则停止。源码核实与真实测试限制见 [Codex 核实记录](codex-as.md)。

## Claude 配置与安全边界

token 保存为 `~/.secrets/claude-NAME`，0600、当前用户拥有的普通文件、单 hardlink。日常只向子进程设置 `CLAUDE_CODE_OAUTH_TOKEN`，继承原有 `CLAUDE_CONFIG_DIR`；不强设为 `~/.claude`，保留 `.claude.json`、MCP、配置和信任状态的既有来源。

显式 API key／OAuth token／认证 FD／替代 provider／endpoint／profile 覆盖，以及 settings 的认证 helper／组织限制会拒绝，只报告变量名或来源，不输出值。禁止透传 login/logout/setup-token、`--bare`、remote／SDK／managed-settings／project-config-root 等认证配置来源覆盖，以及未审核选项。只按原生参数边界识别首个命令操作数，不扫描提示词中的单词；原生 Claude 即使带 `-p` 或 `--`，精确的首个 `auth` 操作数仍会调度认证子命令，因此拒绝。required／optional／variadic 参数值保持其原生角色。这是有限的本地 CLI 选项检查，不承诺兼容所有命令。

不绕过 managed policy：自动注册遇到非空 managed cache、系统策略、policy helper、认证绑定或无法保留的 profile 加载链就停止。空／缺失 cache 不因此封锁；本机检查的 cache 为空对象，运行时仍重新检查。

新账号文件独占创建，权限／所有者／symlink／hardlink 异常均拒绝。账号名仅允许非空 ASCII 字母、数字、下划线、连字符。本地格式检查不是服务端认证，不自动删除过期凭据或重新登录。

## 补全、安装与并发

zsh 使用工具 package 中的 OMZ custom completions、现有 compinit；Bash 使用标准 bash-completion 用户目录自动加载。首参仅枚举安全文件名与 metadata，不读取凭据、不运行 CLI、不联网；损坏内容由启动校验。后续先补全支持范围，有既有原生补全时再委托。

fzf-tab 使用同一候选；两个账号命令的预览只打印文字，不读文件。无 fzf 仍能普通 Tab。新终端加载新补全；不向 rc 追加 source 或 PATH。安装沿用 Stow，不会触发真实初始化／登录／模型请求。

启动器自己的步骤／帮助标题用青色，确认／警告用黄色，保存／初始化成功用绿色，错误用红色。默认仅对实际接收消息的终端启用颜色，重定向日志保持纯文本；独立 `/dev/tty` 上的确认仍可着色，不影响重定向的 Codex／Claude 输出。沿用 `DOTFILES_COLOR=auto|always|never`，非空 `NO_COLOR` 或 `TERM=dumb` 优先禁用颜色。原生命令输出、补全账号列表与内部机器输出不经着色。

**Codex 的全局软链接没有并发保护。** A 运行或挂起时不能切 B；其他终端、桌面端和 IDE 可能访问同一目录。不会自动杀进程、切额度、后台保活或续期 Claude token。

## 验证

```sh
tests/codex-as.sh
tests/account-enrollment.sh
pre-commit run --all-files
```

测试全部使用临时 HOME、虚构凭据和 fake CLI，覆盖 Bash/zsh、TTY、重定向 stdin／JSON stdout、登录失败、人工回退、刷新写回及原子替换、异常权限及认证来源、补全和预览不泄密。Mock 测试通过、版本实现已核实、真实账号切换已验证是三种不同结论；本次验证前两种。
