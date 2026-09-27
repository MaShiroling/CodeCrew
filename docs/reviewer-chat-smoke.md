# 仅 Reviewer 的原生聊天室验收

统一路线第 3 项：单独验证鲸鲸（Claude Code 接 DeepSeek）的增强聊天室契约。
这不同于旧 `AgentReviewerRunner` 的 `{verdict, summary, issues}` 独立评审接口。
当前入口已开发并离线模拟验证，**真实提供商兼容性尚未在线验收**。

## 用例与调用预算

| 用例 | 证据与预期 | Reviewer 回合数 |
| --- | --- | --- |
| `test_live_reviewer_native_approval` | 夹具修正求和函数；真实 Verifier 通过；独立评审批准 | 1 |
| `test_live_reviewer_native_rework_then_approval` | 夹具先写入 `sum(items) + 1`，真实测试失败；评审拒绝；夹具修正并重新验证；新会话批准且显式解决原问题 ID | 2 |

代码和 Plan 均由受控夹具生成，不调用 Codex/Kimi，也不表示它们实际完成了规划或返工。
Verifier 使用真实 Diff、路径检查、语法检查、公开 pytest 和额外断言，不编造测试结果。
Reviewer 结论必须来自真实输出，不能由夹具替换或修补。

每回合最多 180 秒、原生格式化最多一次尝试，不增加重试或文本回退。
正常 Read/格式化工具交互可能包含多次模型请求；一个 Agent 回合不等于一次 API 请求，
也不保证固定 Token 或费用。建议先运行单回合批准，再单独运行两回合用例。

## 前置条件

- 在项目根目录和虚拟环境中运行；具备 Git、项目依赖和支持所需参数的 Claude Code CLI。
- 同一个终端通过环境变量设置 `DEEPSEEK_API_KEY`；不要放进命令或提交到仓库。
- 无需 Codex/Kimi CLI、登录或密钥。入口只读取 shell 环境，不加载 `.env`。
- 入口先运行 `claude --help` 检查参数，不启动模型；不兼容则直接失败。

仅先验证批准（消耗一个 Reviewer 回合）：

```bash
CODECREW_RUN_REVIEWER_CHAT_LIVE=1 \
  .venv/bin/pytest -q -s tests/integration/test_reviewer_chat_live.py::test_live_reviewer_native_approval
```

批准用例通过后，再验证返工及问题延续（消耗两个 Reviewer 回合）：

```bash
CODECREW_RUN_REVIEWER_CHAT_LIVE=1 \
  .venv/bin/pytest -q -s tests/integration/test_reviewer_chat_live.py::test_live_reviewer_native_rework_then_approval
```

不需要额外设置 `CODECREW_REVIEWER_STRUCTURED_OUTPUT`；此入口始终启用增强原生契约。
直接运行整个文件会执行两个用例、最多三个 Reviewer 回合；任一失败不会自动重试。
不要在这两个用例通过前反复重跑整条最多九回合的团队链路。

## 验收与失败证据

复用生产 `AgentTurnRunner`、Verifier 指令执行与当前 Reviewer Schema，检查：

- 原生 `structured_output` 有效，报告来源、收件人、问题字段符合契约；不采用备用文本。
- 会话标识存在，两个回合独立且无 `--resume`；不复用原生会话。
- 观察到对全部交付 Artifact 的 Read 调用，包括第二回合的历史 ReviewReport。
- 工具限于 Read/Glob/Grep；StructuredOutput 仅在输入与最终原生对象一致时视为格式化事件，
  不代替读取证据。两个冒烟入口共享同一组工具/读取审计规则。
- Worktree、原仓库以及既有 Artifact 在 Reviewer 回合前后未改变。
- 正确代码获批，错误代码被拒绝；第二次批准完整结转并解决原未解决问题 ID。

静态格式错误在生产解析边界拒绝，不路由、不 ACK；工具、会话、文件快照和预期结论
由冒烟审计在回合后检查，失败不生成验收报告，**不是操作系统层面的实时阻断**。

成功/失败均尝试长期归档 SQLite、原始 AgentResult、规范化事件和证据 Artifact：

- `evals/results/reviewer-chat-approval-live/<trace_id>-*/`
- `evals/results/reviewer-chat-rework-live/<trace_id>-*/`

归档失败不掩盖原失败，亦不打印通过结论。CLI HOME 和环境变量不归档；原回复/日志
仍可能有敏感内容，分享前应检查。归档完整性不代表评审验收成功。
可用[离线回放](reviewer-replay.md)的 `--archive ... --source native` 检查归档格式。

通过后输出 `reviewer_acceptance_passed=true`、报告 Artifact ID 及
`task_completion_evaluated=false`。任务仍为 `reviewing`；没有运行完成守卫，
不能把本用例当作完整任务成功、真实 Implementer 返工或两轮预算耗尽验收。
Reviewer 尚无 OS 级只读隔离，额外断言尚无保密隔离，实际远端模型版本未确认。

## 离线验证

```bash
.venv/bin/pytest -q tests/test_reviewer_chat_smoke.py tests/test_reviewer_chat_live_checks.py tests/test_offline_check.py
.venv/bin/python scripts/check_offline.py
```

默认跳过在线用例；全量离线入口强制关闭新增开关、移除模型凭据。
离线测试使用模拟 CLI 输出，但执行真实 Verifier，覆盖成功、错误结论、漏读、禁止工具、
文件/证据篡改、会话复用、遗漏旧问题、原生缺失、非零退出和归档失败。

下一项：先取得这两个在线用例的原始证据，再验收真实三 Agent 两轮返工耗尽转人工。
