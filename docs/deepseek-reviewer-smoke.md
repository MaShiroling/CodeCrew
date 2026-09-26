# DeepSeek Reviewer 真实冒烟：分步验收

已完成 **子步骤 1～4**。用户在线运行首次因 Markdown JSON 包装失败；格式兼容
修复后，用户在 2026-09-26 重跑并提供单条测试通过的输出。实际远端模型版本仍未确认。

## 子步骤 1：本地预检（已完成）

```bash
.venv/bin/pytest -q tests/test_claude_adapter.py tests/test_deepseek_cli_preflight.py
```

- 检查已安装的 `claude --help` 是否仍提供 Adapter 使用的命令选项；若 CLI 未安装，
  该本机兼容性用例跳过，不能视作已验证。2026-09-25 本机版本为 `2.1.246`。
- 模拟子进程测试固定 Reviewer 角色、`Read/Glob/Grep` 工具、`plan` 权限、
  safe mode、禁用斜杠命令、空 MCP 配置，以及新建会话不携带 `--resume`。
- 模拟环境测试仅将 `DEEPSEEK_API_KEY` 映射为子进程的 `ANTHROPIC_AUTH_TOKEN`；
  命令行不含密钥，其他供应商的 API Key 不会传入。没有密钥时启动失败。
- Adapter 使用的地址和模型变量与
  [DeepSeek 官方 Claude Code 接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)
  一致。官方文档可能更新，真实冒烟前需再次核对。

这些是**配置和 CLI 参数检查**，不是模型身份、文件系统强隔离或在线调用证明。
`--tools` 限制 Agent 可用的内置工具；当前 Reviewer 没有独立的操作系统只读沙箱，
不能据此断言子进程绝对无法写文件。在线测试仍须使用临时仓库、检查实际工具事件与
测试前后的文件状态。CLI 仍继承受限列表中的 `HOME` 等基础环境变量；预检不检查
用户认证文件，更不把本地已有的 Claude 账户当成 DeepSeek 鉴权证据。

当前 Codex 任务进程没有 `DEEPSEEK_API_KEY`。真实测试应由用户在同一个终端导出
**DeepSeek Platform API Key** 后显式运行；只检查变量是否非空，不打印或提交密钥。

## 子步骤 2：固定证据与离线失败关闭（已完成）

```bash
.venv/bin/pytest -q tests/test_deepseek_reviewer_smoke.py
```

测试在临时 Git 仓库中放入有缺陷的 `total()`、公开 pytest 用例和测试驱动内的额外
断言。先确认种子版本的 Diff、公开测试与额外断言失败，再在独立 Worktree 中修复一处
源文件，由确定性 Verifier 生成真实 Diff、权限报告及静态/公开/额外检查证据；Plan 和
证据均保存为 Artifact。额外断言只是不在仓库内的测试，**并非安全隔离的隐藏测试**。

模拟 Claude Code 流式结果经真实 `DeepSeekClaudeReviewerAdapter` 和
`AgentReviewerRunner` 跑两次，验证新会话、只读命令、证据引用和批准/拒绝 JSON 解析。
无效 JSON、未知结论、非法 issue 字段、CLI 报错与异常退出均产生
`ReviewerExecutionError`，不会变成批准。本阶段的批准/拒绝文本是预置测试数据，
不是 DeepSeek 的判断。证据文件位于 Worktree 外的 ArtifactStore；路径在 prompt 中，
实际证据读取已由子步骤 3 的用户本机在线断言验证。

## 子步骤 3 在线测试入口（用户本机运行通过）

`tests/integration/test_deepseek_reviewer_live.py` 默认跳过。它最多发起两次真实
Reviewer 回合，每次 180 秒超时：有效 Diff/全部验证通过时要求批准；无有效 Diff、
公开及额外断言失败时要求拒绝。测试复用子步骤 2 的临时仓库与 Artifact，用独立
临时 `HOME` 避免依赖本机已有 Claude 登录状态，检查实际 `Read/Glob/Grep` 调用、
每份证据的读取路径、两次不同的原生会话 ID，以及 Worktree/Artifact 前后哈希。
临时 `HOME` 在测试结束（包括断言失败）后清理；其他 pytest 临时夹具按 pytest
自身策略保留若干轮。它不证明操作系统级只读隔离，也不调用 CompletionGuard。

先运行不会消费额度的检查：

```bash
.venv/bin/pytest -q tests/test_deepseek_reviewer_smoke.py tests/test_deepseek_reviewer_live_checks.py tests/integration/test_deepseek_reviewer_live.py
```

真实运行前，在**同一个 zsh 终端**安全输入 DeepSeek Platform API Key（不要把
密钥写在命令行、仓库文件或聊天中）：

```zsh
read -rs 'DEEPSEEK_API_KEY?DeepSeek API Key: '
echo
export DEEPSEEK_API_KEY
[[ -n "$DEEPSEEK_API_KEY" ]] && echo '密钥已设置'
CODECREW_RUN_DEEPSEEK_REVIEWER_LIVE=1 .venv/bin/pytest -q tests/integration/test_deepseek_reviewer_live.py
```

如果第一回合失败，测试不会继续消耗第二回合；不要因失败而移除只读限制或改用
本机 Claude 账户。当前 CodeCrew 桌面任务进程没有此密钥，因此没有代替用户运行。
Claude Code 的流式结果若不暴露实际远端模型 ID，不能仅凭环境变量宣称底层模型
身份已证实。Artifact 位于 Worktree 外，在线测试断言确认 CLI 确实尝试读取每份证据。

2026-09-26，用户提供的在线失败输出包含“没有有效代码变更”的结构化拒绝结论，
但 CLI 将其包裹在 ` ```json ` 代码块中，导致原解析器抛出
`ReviewerExecutionError`。现仅兼容完整响应中的单个显式 JSON 代码块；前后解释、
多个代码块、无效 JSON 和不符合 schema 的内容仍然拒绝，不从任意文本中抽取 JSON。
模拟 CLI 回归用例已覆盖此包装。该修复不改变审批结论或 CompletionGuard 条件，
不能把这次失败记录成在线验收通过。

用户重跑结果为 `. [100%]`，单条用例通过，意味着两个真实评审回合均满足测试断言：
有效证据批准、无 Diff/失败检查拒绝，原生会话 ID 不同，工具调用只含允许项，每份
证据均有可观察的 Read 调用，Worktree/Artifact 文件哈希未改变。该结果由用户提供，
不是桌面任务进程重复运行所得。它证明本夹具的 Reviewer 在线冒烟通过，不证明
所有不可信输入下的只读性，不确认远端具体模型版本，也不代表三 Agent 或
CompletionGuard 在线闭环已通过。

## 子步骤 4：验收记录与收尾（已完成）

| 项目 | 已知结果与证据来源 |
| --- | --- |
| 验收范围 | 独立 `DeepSeekClaudeReviewerAdapter` → `AgentReviewerRunner`；不是聊天室完整运行时 |
| 在线日期与来源 | 2026-09-26，用户同终端重跑的 pytest 输出 `. [100%]` |
| 正例/负例 | 单条测试内两个回合：有效 Diff/通过的验证报告批准；无 Diff/失败报告拒绝 |
| 工具与文件检查 | 允许工具事件、证据 Read 路径、不同原生会话 ID、Worktree/Artifact 哈希断言通过 |
| 输出可靠性 | 裸 JSON 或整个响应中的单个 JSON 代码块；其余非法/歧义输出失败关闭 |
| 可复现代码基线 | `d46b39d` 包含格式兼容修复；`ef9355c` 记录用户报告的在线通过结果 |
| 未保留的原始证据 | 用户仅提供 pytest 摘要；原始响应、具体 trace/session ID 未留存，不补造 |
| 实际远端模型版本 | 未验证；请求模型别名不作为返回版本证明 |
| 成本与 Token | 未形成可核实的供应商计费/Token 记录，不能记为零或采用 CLI 的 Claude 估价 |

统一离线验收入口（开发依赖必须安装，使用项目虚拟环境）：

```bash
.venv/bin/python scripts/check_offline.py
```

入口用当前解释器运行全部 pytest 和 Ruff，关闭目前所有四个在线测试开关，并从
子进程环境移除已知供应商认证变量；不修改父终端环境。失败时返回原失败码并停止。
这是当前测试套件的保护入口，不是断网沙箱，也不承诺未来未注册的在线用例自动受保护。
本机 CLI `--help`/`--version` 等不调用模型的检查仍可执行。macOS Seatbelt 测试须在
允许启动 Seatbelt 的环境运行，不能用跳过该边界的方式取得通过。

此轮收尾没有额外真实模型调用。现阶段仅能宣称独立 Reviewer 的本夹具在线冒烟通过：

- 文件哈希未变和工具白名单不等于操作系统级只读隔离或主动攻击测试。
- 额外断言不是对 Agent 安全隔离的隐藏测试。
- 该入口不覆盖 `AgentTurnRunner` 的聊天室动作 JSON、人格注入、ACK 或返工预算。
- Reviewer 批准不赋予任务成功；Verifier 和 CompletionGuard 仍须在完整运行时中验收。

### 下一阶段入口：三真实 Agent 联调

先完成团队配置与聊天协议预检，显式绑定 Codex Planner、Kimi Code Implementer、
DeepSeek Claude Reviewer；保持密钥仅从环境注入、macOS Kimi 边界和受管 Worktree。
沿现有 Task API/`WorkflowEventLoop`/`AgentTurnRunner` 路径核对三角色结构化动作，
不能复用本独立 Reviewer 用例就宣布聊天室闭环成功。之后分别验证完整成功路径、
拒绝返工和预算耗尽；每条路径都检查持久化状态、trace、Patch、验证/评审证据与
CompletionGuard 决定。正式多语言评测与 UI 人工消息不在本轮收尾范围内。
