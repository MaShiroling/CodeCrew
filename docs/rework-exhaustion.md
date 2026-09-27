# 第 4 项：真实团队两轮返工耗尽转人工

目标：验证控制器在两轮返工后仍被拒绝时停止派发，发布有完整证据的人工请求，
而不是误报成功。真实验收已通过：Trace `1af3642a-9ddf-4552-ba02-1ef93873497b`，
9 回合、3 次独立评审拒绝、2 轮返工后正确转人工。归档数据库与 83 个 Artifact
只读核验通过，人工请求证据及关联匹配，无完成 Trace，不代表自然任务评测成功率。

## 场景与预算

白金规划 → 月见只读澄清 → 白金 Plan v2 → 月见实现 → 鲸鲸拒绝 →
月见返工 1 → 鲸鲸拒绝 → 月见返工 2 → 鲸鲸拒绝 → 人工等待。

最多 9 个 Agent 回合（2 Planner / 4 Implementer / 3 Reviewer），不额外重试。
夹具在三次实现后主动注入错误，真实 Verifier 随后执行测试。这是控制流故障注入实验，
不是模型自然错误率或成功率评测。每轮鲸鲸必须使用独立会话并读取历史评审证据。

仅在同一终端已有 Codex 登录、`KIMI_MODEL_API_KEY` 和 `DEEPSEEK_API_KEY` 时运行。
不读取 `.env`；不要把密钥写入命令。CLI 需在 PATH 上，macOS 需支持 Seatbelt。
如 Codex 不在 PATH，先按本机实际安装位置配置，不另行安装或修改登录。

```bash
CODECREW_PLANNER_TIMEOUT_SECONDS=360 CODECREW_REVIEWER_STRUCTURED_OUTPUT=1 \
  CODECREW_RUN_THREE_AGENT_BUDGET_LIVE=1 \
  .venv/bin/pytest -q -s tests/integration/test_three_agent_rework_live.py::test_live_three_agent_rework_budget_exhaustion
```

这会消耗真实模型额度；一个 Agent 回合可能包含多次模型请求。
Planner 360 秒、其他角色 180 秒，总回合和返工预算不变；原生格式最多一次尝试。

## 通过条件

- pytest 通过且 `scenario_acceptance_passed=true`；`task_success=false` 才是正确预期。
- `task_state=needs_human`、`rework_rounds=2`，共 9 回合，收到三份真实拒绝报告。
- 暂停原因为 `rework budget exhausted`，不是超时、Token 或事件上限。
- 人类收件箱只有一条待处理人工请求，由 Orchestrator 发出，关联最后一次拒绝。
- 请求引用最新 ReviewReport、Plan、Verifier 报告和 Diff，报告保留未解决问题 ID。
- 未生成批准/完成消息、CompletionDecision 或完成 Trace；未派发第三轮返工。
- 长期归档完整性验证通过；最终报告包含 `rework_exhaustion_acceptance` 明细。

仅显示 `needs_human` 或仅归档成功不能算通过。格式错误、读取不足、错误批准等均停止
并保存失败证据，不修补原始输出、不继续付费回合。最终验收检查本身不派发 Agent。

归档：`evals/results/three-agent-rework_exhaustion-live/<trace_id>-*/`。
旧失败归档保留不变；失败后先定位当前节点，不重复运行整条链路。
Reviewer OS 只读和隐藏测试保密仍未建立，本测试不能证明这些边界。

## 本次失败与修复

Trace `4d6a635f-2645-49d6-9983-8540de9c3930` 的数据库及 41 个 Artifact 已只读核验。
首轮 Reviewer 原生 `request_rework` 符合当前契约，但漏读公开 pytest 命令审计
（退出码 1）；读取 stdout 不能替代该记录。旧流程已 ACK 并发布返工消息后才审计，
控制器尚未处理返工；任务停在 `reviewing`，不是耗尽验收成功。旧归档不修改。

现给 Reviewer 提供全量去重必读清单，并将受控入口的工具/读取/会话/快照及注入缺陷
结论检查移至路由和 ACK 前。原始结果仍保存；失败不创建评审报告或路由评审动作，
不修补 JSON、不放宽读证据要求、不增加预算或模型重试。通用运行器提供可选可信回调，
不冒充生产已普遍启用严格工具审计，更不代表 OS 级权限检查。

离线模拟重建本次“读日志但漏读命令审计”的失败类别（不是原回复逐字回放）。
下一次先单独运行上文链接中的 Reviewer 两回合用例，再考虑九回合链路；提示词改进
并不保证真实模型绝不漏读。不能因离线通过就把真实两轮耗尽标记为完成。

## 离线验证

`tests/test_three_agent_rework.py` 使用模拟 CLI、真实 Git/Verifier 验证文本/原生三条
协议路径，并覆盖缺失人工交接、错误关联、缺失证据、错误状态/轮次/回合数、假完成
及事件上限冒充返工耗尽。全量运行：

```bash
.venv/bin/python scripts/check_offline.py
```

真实耗尽通过后进入第 5 项：受控人工消息 API。本项不实现人工回复或任务恢复。
