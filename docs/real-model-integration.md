# 真实模型接入与权限边界（2026-09-24）

本页记录接口事实、已实现的离线边界及未完成的接入工作；**尚未使用真实 Kimi Code
会员模型 / DeepSeek Flash 执行或消费 API 额度**。角色仍是白金（Codex/GPT）规划、
月见（Kimi Code CLI 接会员模型）实现、鲸鲸（Claude Code 接入 DeepSeek Flash）评审。
这里的 CLI 是 Agent 执行环境，
模型是其背后的推理服务，两者不可混同。

## 现有代码边界

- `AgentAdapter` 规定 `start`、`stream`、`wait`、`cancel`、`resume`；每次请求含
  `task_id`、`trace_id`、角色、工作目录、权限模式、超时和可选原生会话 ID。
- `ClaudeCodeAdapter` 当前只读，限制为 `Read/Glob/Grep`；独立的
  `DeepSeekClaudeReviewerAdapter` 已支持 Reviewer 子进程的专属地址、模型和密钥环境。
- `CodexCliAdapter` 已可作为白金的 Planner；当前 CLI 装配需要显式指定 Planner，
  Implementer 默认仍为 Codex CLI，可显式选择 Kimi Code CLI；Reviewer 可显式切换到
  DeepSeek Claude 变体。
- `AsyncProcessRunner` 支持向单个子进程传递完整环境变量映射；这是隔离不同供应商密钥的
  可复用入口。不要把密钥写进 JSON 配置、命令行参数、Artifact 或 Trace。
- 本机只验证了 `kimi --version` / `kimi --help`，检测到 Kimi Code CLI 2.0.0；
  Claude Code 与 Codex 可执行文件也在 `PATH`。未读取本地账户配置或检查密钥值。

## 供应商契约与接入选择

| 角色 | 官方入口与模型 | 鉴权及输出 | 工具边界 |
| --- | --- | --- | --- |
| 月见 / Implementer | Kimi Code 会员 API：`https://api.kimi.com/coding/v1`，`kimi-for-coding`；Kimi Code CLI 使用 `kimi -p ... --output-format stream-json` | 会员密钥只经子进程 `KIMI_MODEL_API_KEY` 传入；CLI 的 `KIMI_MODEL_*` 变量在内存中创建临时模型配置。`kimi-for-coding` 是别名，实际后端版本可能变化 | CLI 受限 Agent 只暴露文件工具，Seatbelt 限制写入；测试由 CodeCrew 的命令白名单执行器运行 |
| 鲸鲸 / Reviewer | Claude Code CLI 接 DeepSeek Anthropic 兼容地址 `https://api.deepseek.com/anthropic`；明确选择 `deepseek-flash` | DeepSeek 官方 Claude Code 配置使用 `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL` 与模型环境变量；供应商 API Key 只能进入 Reviewer 子进程环境 | 复用 Claude Code Adapter 的只读工具限制，仍需验证实际 provider、模型及最终 JSON 评审结果 |

**Kimi Code 会员密钥与 Moonshot Platform API Key 不是同一种接入**：后者的
`https://api.moonshot.ai/v1` / `kimi-k3` 组合不适用于当前会员密钥 Adapter。
官方会员 API 文档要求使用 `kimi-for-coding` 模型别名；这不等于能证明底层固定为 K3。
Kimi Code CLI 的 `--prompt` 非交互模式默认使用 auto 权限策略，
且不能与 `--yolo`、`--auto` 组合；它**不等于** CodeCrew 的命令白名单。
官方 CLI 文档也明确其普通模式能改文件和运行 shell 命令。用户已确定月见采用本机
Kimi Code CLI。不能仅凭独立 Worktree 和事后 Diff/命令审计就声称“禁止命令不会
执行”。官方 [Hooks 文档](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html)
确认 `PreToolUse` 可阻止调用，但 Hook 出错或超时会 fail-open，不能单独承担安全边界。
Kimi Adapter 已采用严格工具列表加 macOS 写入沙箱，但真实模型会话对 `Bash` 禁用的
验证仍未完成；这一路径只允许显式选择，不能纳入正式评测。工具不可用或拒绝时应失败
并转人工，不得退回开放执行。

DeepSeek 官方当前模型 ID 是 `deepseek-flash`（当前对应 V4.1-Flash），并支持
Anthropic 兼容接口、流式和工具调用。官方 Claude Code 指南使用
`ANTHROPIC_MODEL=deepseek-flash[1m]` 等设置；接入时应显式设定模型，不能依赖
Claude 模型名的自动映射，避免评测中无意使用不同模型。Reviewer 只读是 CodeCrew
进程工具限制与独立会话的要求，不是模型 API 自带的文件系统隔离。

## 下一步实现验收

1. 为每角色定义显式供应商/模型绑定，启动时检查所需环境变量**是否存在**，不打印值；
   单独启动 Reviewer 子进程，并清除与目标供应商冲突的继承环境变量。
2. 月见 Adapter 对齐现有生命周期：启动、流式、超时、取消、退出结果、Token 使用量和
   原生会话 ID；工具执行须先检查工作目录、允许路径和命令规则。多轮状态不得隐式复制
   其他 Agent 的聊天历史。
3. 鲸鲸 Adapter 验证只读工具、DeepSeek 端点/模型、独立会话、结构化 JSON 审批、
   无有效证据时拒绝审批。Reviewer 批准仍不绕过 Verifier 与 CompletionGuard。
4. 先用 Fake/录制响应覆盖错误、取消、越权与超时；真实调用仅在用户配置密钥、
   明确启用集成测试后做最小只读冒烟，再做一条小型 Worktree 编码任务。
5. Trace/报告记录供应商、请求模型、实际返回模型、CLI 版本和 Token 使用量，
   不记录密钥或原始认证头；无法确认实际模型时标为“未验证”，不纳入对照评测。

当前进度：已实现 `DeepSeekClaudeReviewerAdapter` 的本地只读装配和子进程环境隔离，
用模拟进程测试；未做付费 API 请求。Kimi 侧新增了只暴露
`Read/Grep/Glob/Write/Edit` 的独立 Agent 文件（没有 `Bash` 或子 Agent），以及
`KimiWriteBoundary`：在 macOS 上用 Seatbelt 限制 CLI 及其子进程只能写授权目录和
独立运行目录，明确拒绝 `.git`、`.codecrew`、`.env` 等路径。系统测试验证了授权写入、
工作区外写入、`.git`/`.env` 写入和符号链接逃逸；缺少 Seatbelt 或非 macOS 时拒绝
启动，不回退为裸 CLI。`sandbox-exec` 已被标记为弃用，后续需保留失败关闭行为并持续
做平台兼容测试。

`KimiCodeAdapter` 已把两者固定到启动路径，并接入可选的 Implementer 角色配置。
它仅接受任务专属 Worktree，使用每回合新建的私有运行目录，通过
`KIMI_MODEL_API_KEY` 注入 `kimi-for-coding` + `https://api.kimi.com/coding/v1`
临时会员模型配置；没有该环境变量或没有 macOS Seatbelt 即拒绝
启动。离线测试覆盖 JSONL、拒绝异常工具、退出码、超时、取消和环境隔离；本机实际 CLI
仅验证了在 Seatbelt 内运行 `--version`。**尚未用真实模型验证 `Bash` 拒绝，也未做
端到端编码任务**。由于 `--agent-file` 不能和 `--session` 同用，原生恢复明确禁用，
后续回合靠结构化消息开启新会话；JSONL 不提供可靠的原生会话 ID / Token 用量，
当前不推测这两个字段。
默认服务器配置继续使用原 Claude Code Reviewer，必须显式配置
`reviewer_adapter: "deepseek-claude-reviewer"` 才切换，且启动时需要
`DEEPSEEK_API_KEY`。真实模型身份仍待联网冒烟验证。

## 官方资料

- [Kimi Code 会员 API 模型与地址](https://www.kimi.com/en/help/kimi-code/membership-guide)
- [Kimi CLI 临时模型环境变量](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/env-vars)
- [Kimi Code CLI 命令参考](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command.html)
- [Kimi Code CLI 工具能力](https://www.kimi.com/code/docs/en/kimi-code-cli/guides/getting-started.html)
- [Kimi Code CLI 自定义 Agent 工具白名单](https://github.com/moonshotai/kimi-code/blob/main/docs/en/customization/agents.md)
- [macOS sandbox-exec 手册（已弃用）](https://man.freebsd.org/cgi/man.cgi?manpath=macOS+13.6.5&query=sandbox-exec&sektion=1)
- [DeepSeek 模型与定价](https://api-docs.deepseek.com/quick_start/pricing/)
- [DeepSeek Anthropic API 兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)
- [DeepSeek 接入 Claude Code](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)
