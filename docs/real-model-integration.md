# 真实模型接入与权限边界（2026-09-24）

本页记录接口事实、已验证的边界及未完成的接入工作。**Kimi Code 会员模型的单任务
真实冒烟已通过；DeepSeek Flash 尚未进行真实调用**。角色仍是白金（Codex/GPT）规划、
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
- 本机检测到 Kimi Code CLI 2.0.0；用户随后在自己的终端运行了真实会员模型冒烟测试，
  结果为 `8 passed`。Claude Code 与 Codex 可执行文件也在 `PATH`；未读取本地账户配置
  或检查密钥值。

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
Kimi Adapter 已采用严格工具列表加 macOS 写入沙箱。真实冒烟核对了 CLI 会话的工具
声明和实际调用，均未出现 `Bash`；但尚未做主动诱导执行禁止命令的拒绝测试，也未完成
三真实 Agent 的完整任务。这一路径只允许显式选择，不能纳入正式评测。工具不可用或
拒绝时应失败并转人工，不得退回开放执行。

DeepSeek 官方当前模型 ID 是 `deepseek-flash`（当前对应 V4.1-Flash），并支持
Anthropic 兼容接口、流式和工具调用。官方 Claude Code 指南使用
`ANTHROPIC_MODEL=deepseek-flash[1m]` 等设置；接入时应显式设定模型，不能依赖
Claude 模型名的自动映射，避免评测中无意使用不同模型。Reviewer 只读是 CodeCrew
进程工具限制与独立会话的要求，不是模型 API 自带的文件系统隔离。

## 剩余真实接入验收

1. 对鲸鲸进行 DeepSeek Flash 在线冒烟，核对只读工具、独立会话、结构化 JSON 审批，
   并验证无有效证据时拒绝审批。Reviewer 批准仍不能绕过 Verifier 与 CompletionGuard。
2. 对白金 → 月见 → Verifier → 鲸鲸 → CompletionGuard 跑一条完整真实任务，再覆盖
   Reviewer 拒绝返工和预算耗尽。
3. 主动诱导月见执行禁止命令，验证边界确实拒绝；独立 Verifier 小任务通过不能替代该测试。
4. Trace/报告记录可核实的供应商、请求模型、实际返回模型、CLI 版本和 Token 用量；
   不记录密钥或认证头。Kimi JSONL 当前没有可靠的原生会话 ID / Token 用量，不能推测
   或把未知值记为零；无法确认实际模型时标为“未验证”，不纳入对照评测。

当前进度：已实现 `DeepSeekClaudeReviewerAdapter` 的本地只读装配和子进程环境隔离，
用模拟进程测试；DeepSeek 仍未做真实 API 请求。Kimi 侧新增了只暴露
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
还验证了在 Seatbelt 内运行 `--version`。Seatbelt 还阻止 CLI 读取真实 Home 的文件内容，
只对 Worktree、私有运行目录以及指定的 CLI/Agent 文件做例外；这不是完整的只读隔离，
Home 之外的系统路径仍可能可读。**真实冒烟验证了工具清单不含 `Bash`，随后一个
小型 Bug 修复任务也通过独立 Verifier；但尚未主动尝试禁止命令，且未做三 Agent
端到端任务**。由于 `--agent-file` 不能和
`--session` 同用，原生恢复明确禁用，
后续回合靠结构化消息开启新会话；JSONL 不提供可靠的原生会话 ID / Token 用量，
当前不推测这两个字段。

Kimi Code 会员真实冒烟测试是显式选择的单次短任务。先在**已经导出新密钥的同一个终端**运行
`test -n "$KIMI_MODEL_API_KEY"`（不要输出密钥），再运行：

```bash
CODECREW_RUN_KIMI_LIVE=1 .venv/bin/pytest -q tests/integration/test_kimi_code_live.py
```

2026-09-24，用户在已配置会员密钥的本机终端执行上述命令，提供的结果为 `8 passed`
（7 条离线检查、1 条真实模型测试）。真实测试在独立 Worktree 中观察到模型返回完成
标记、通过文件工具创建目标文件；还核对了仅有该文件变更、实际工具调用和会话工具声明
均在允许集内。这是 **Kimi Implementer 单任务冒烟**，不是三角色工作流或
CompletionGuard 的验收，也不能据此确认底层模型固定为 K3。

该测试会创建临时仓库和独立 Worktree，配置 8 步预算与 120 秒超时；检查目标文件、唯一变更、
允许工具集及 CLI 会话内的工具声明，之后删除临时运行目录。它会消耗一次会员额度。
若本地 Seatbelt 不可用、未配置密钥或工具声明无法核验，测试会失败而非宣布成功；
不要把密钥放到命令行、聊天或仓库中。桌面 Codex 进程通常不会继承其他终端导出的密钥，
因此请从原终端执行。

小型 Bug 修复任务位于 `tests/integration/test_kimi_verifier_live.py`。临时仓库只含
有缺陷的 `chunked` 实现与公开 pytest 用例；额外边界断言留在测试驱动代码中，直到
Kimi 回合结束才由 Verifier 运行。它们只是**未交给 Agent 的额外断言**，目前没有
独立容器/用户隔离，不能当作正式评测的保密隐藏测试。离线用例已验证：种子 Bug 使
公开测试和额外断言失败；修复后 Verifier 全通过；Fake Agent 自称成功时，即使使用
合成的批准评审，CompletionGuard 仍拒绝无有效 Diff 和失败验证。

只做不消费会员额度的夹具检查：

```bash
.venv/bin/pytest -q tests/integration/test_kimi_verifier_live.py
```

要在已配置新密钥的同一终端显式运行一次真实 Kimi → Verifier 任务：

```bash
CODECREW_RUN_KIMI_VERIFIER_LIVE=1 .venv/bin/pytest -q tests/integration/test_kimi_verifier_live.py
```

真实用例最多配置 8 步和 120 秒，随后由独立 Verifier 执行 Python 语法、公开 pytest、
额外断言及目录权限检查；它会消耗会员额度。2026-09-24，用户在本机运行该命令，
提供的结果为 `3 passed`。即使 Verifier 通过，也不等于三角色工作流已获 Reviewer
批准或 CompletionGuard 已作成功判定。
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
