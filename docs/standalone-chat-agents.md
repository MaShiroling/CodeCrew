# 无仓库只读 Agent 运行边界（8.4c）

本步只建立独立聊天室的内部 Agent 运行环境，**不**从 HTTP 消息自动唤醒模型，
也不在现有 `/ui/` 显示独立聊天回复。聊天室消息仍只持久化；消息驱动调度属于 8.4d。

## 三位成员

`build_standalone_chat_agent_runtime(settings)` 将白金绑定到 Codex CLI、月见绑定到
Kimi Code CLI、鲸鲸绑定到 Claude Code 内的 DeepSeek 模型。每次明确启动的回合使用
新的执行 UUID 和私有工作目录，不要求 `.git`，不关联编码 Task 或 Worktree 表。
适配器原有 `task_id` 字段在此仅作为单次执行的内部命名空间，不会创建 Task。

目录默认位于用户主目录的 `.codecrew/chat-workspaces` 和 `.codecrew/chat-runtime`；
可分别用 `CODECREW_STANDALONE_CHAT_WORKSPACE_ROOT`、
`CODECREW_STANDALONE_CHAT_RUNTIME_ROOT` 调整。两个根目录不能互相包含、不能是符号链接、
不能处在 Git 仓库内，且只有当前用户可访问。每个已获得终态结果的回合会清理其私有目录；
若启动或等待状态不明，则保留目录供人工检查，不假定外部进程已停止。

- Codex：`read-only` 沙箱、禁审批；无 Git 工作区时明确使用 `--skip-git-repo-check`。
  保留用户现有 Codex 认证目录，不向进程传入 Kimi/DeepSeek/Anthropic 凭据。
- Kimi：使用仅有 Read/Grep/Glob 的独立聊天 Agent 文件；macOS Seatbelt 禁止写入
  工作目录，仅允许其独立私有运行目录。原编码任务仍必须提供受管 Git Worktree。
  原编码任务适配器默认拒绝独立聊天标记；只有专门绑定到私有聊天目录的实例才允许。
  非 macOS 或缺少 Seatbelt 时拒绝启动，不会退化为无沙箱运行。
- 鲸鲸：沿用 Claude Code 安全模式和只读工具白名单，DeepSeek 子进程的 HOME/TMPDIR
  定向到私有运行目录。提供者密钥只通过环境变量传入，不写入仓库。

目前 **只读** 指不授权代码写入；Codex 的认证/会话目录仍按其 CLI 自身机制管理，
Claude/Codex 的文件读取也尚无与 Kimi Seatbelt 等价的系统级读隔离。只在可信本机
使用，不把它描述为容器级隔离或“绝无主机读权限”。没有真实模型在线验收，
真实三模型和浏览器验收属于 8.4f。

`tests/test_standalone_chat_agents.py` 覆盖无 Git、私有目录、三 CLI 参数/环境、
流式事件、取消与超时；`tests/test_kimi_boundary.py` 增加真实 Seatbelt 写入反例。
另以已安装的 Kimi CLI `--version` 检查无 Git、无模型调用的真实只读沙箱启动。
