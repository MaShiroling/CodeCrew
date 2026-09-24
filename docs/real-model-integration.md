# 真实模型接入：第 1 步技术核对（2026-09-24）

本页只记录已核对的接口事实和接入决策；**尚未实现 Kimi K3 / DeepSeek Flash Adapter，
也未使用真实模型执行或消费 API 额度**。角色仍是白金（Codex/GPT）规划、月见（Kimi K3）
实现、鲸鲸（Claude Code 接入 DeepSeek Flash）评审。这里的 CLI 是 Agent 执行环境，
模型是其背后的推理服务，两者不可混同。

## 现有代码边界

- `AgentAdapter` 规定 `start`、`stream`、`wait`、`cancel`、`resume`；每次请求含
  `task_id`、`trace_id`、角色、工作目录、权限模式、超时和可选原生会话 ID。
- `ClaudeCodeAdapter` 当前只读，限制为 `Read/Glob/Grep`，但固定使用进程继承的环境，
  尚不能为 Reviewer 单独注入 DeepSeek 地址、模型和密钥。
- `CodexCliAdapter` 已可作为白金的 Planner；当前 CLI 装配需要显式指定 Planner，
  Implementer 固定为 Codex CLI、Reviewer 固定为 Claude Code。
- `AsyncProcessRunner` 支持向单个子进程传递环境变量覆盖；这是隔离不同供应商密钥的
  可复用入口。不要把密钥写进 JSON 配置、命令行参数、Artifact 或 Trace。
- 本机只验证了 `kimi --version` / `kimi --help`，检测到 Kimi Code CLI 2.0.0；
  Claude Code 与 Codex 可执行文件也在 `PATH`。未读取本地账户配置或检查密钥值。

## 供应商契约与接入选择

| 角色 | 官方入口与模型 | 鉴权及输出 | 工具边界 |
| --- | --- | --- | --- |
| 月见 / Implementer | Kimi Chat Completions：`https://api.moonshot.ai/v1`，`kimi-k3`；或 Kimi Code CLI `kimi -p ... --output-format stream-json` | API 使用 `MOONSHOT_API_KEY`；K3 流式区分 `reasoning_content` 与 `content`，多轮工具调用须保留完整 assistant 消息。CLI 支持 JSONL 和 `--session <id>` 恢复 | API 只生成工具调用，文件编辑/命令执行必须由 CodeCrew 实现并授权；CLI 自带文件与 shell 工具 |
| 鲸鲸 / Reviewer | Claude Code CLI 接 DeepSeek Anthropic 兼容地址 `https://api.deepseek.com/anthropic`；明确选择 `deepseek-flash` | DeepSeek 官方 Claude Code 配置使用 `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL` 与模型环境变量；供应商 API Key 只能进入 Reviewer 子进程环境 | 复用 Claude Code Adapter 的只读工具限制，仍需验证实际 provider、模型及最终 JSON 评审结果 |

Kimi K3 的官方文档支持 `reasoning_effort=low/high/max`，并给出工具调用、流式和
`kimi-k3` 模型 ID。Kimi Code CLI 的 `--prompt` 非交互模式默认使用 auto 权限策略，
且不能与 `--yolo`、`--auto` 组合；它**不等于** CodeCrew 的命令白名单。
官方 CLI 文档也明确其普通模式能改文件和运行 shell 命令。用户已确定月见采用本机
Kimi Code CLI。不能仅凭独立 Worktree 和事后 Diff/命令审计就声称“禁止命令不会
执行”。官方 [Hooks 文档](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html)
确认 `PreToolUse` 可阻止调用，但 Hook 出错或超时会 fail-open，不能单独承担安全边界。
在引入执行前可强制的工具/OS 沙箱并通过禁止命令测试前，不将 CLI 注册为无人值守
Implementer。工具不可用或拒绝时应失败并转人工，不得退回开放执行。

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
用模拟进程测试；未做付费 API 请求。Kimi CLI 的工作区写入路径因上述 fail-open
边界仍未启用。默认服务器配置继续使用原 Claude Code Reviewer，必须显式配置
`reviewer_adapter: "deepseek-claude-reviewer"` 才切换，且启动时需要
`DEEPSEEK_API_KEY`。真实模型身份仍待联网冒烟验证。

## 官方资料

- [Kimi K3 官方快速开始](https://platform.kimi.ai/docs/guide/kimi-k3-quickstart)
- [Kimi Code CLI 命令参考](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command.html)
- [Kimi Code CLI 工具能力](https://www.kimi.com/code/docs/en/kimi-code-cli/guides/getting-started.html)
- [DeepSeek 模型与定价](https://api-docs.deepseek.com/quick_start/pricing/)
- [DeepSeek Anthropic API 兼容说明](https://api-docs.deepseek.com/guides/anthropic_api/)
- [DeepSeek 接入 Claude Code](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)
