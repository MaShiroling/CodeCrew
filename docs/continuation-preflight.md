# 第 6 项第 1 子步骤：继续任务契约与只读预检

`POST /api/v1/tasks/{task_id}/continue/preflight` 只检查已有人工意图、角色与当前预算。
这不是“继续执行”接口；`POST .../continue` 尚未实现，任务仍为 `needs_human`。
不迁移状态、不 ACK、不发布消息/Trace、不预留任务、不恢复会话、不运行模型/测试。
响应携带原任务 trace_id，便于关联；不创建执行回执或幂等派发记录。

## 请求与响应

先通过第 5 项消息 API 保存人工消息，取得 `message.message_id`；重新读取 task revision。

```json
{
  "expected_revision": 3,
  "message_id": "7b25b775-2318-4ca7-90d4-2b124516b6da",
  "target_role": "planner"
}
```

示例 ID/修订号应替换为当前服务中的真实值，不能使用历史 smoke 归档。
消息必须来自该任务唯一 Human 的受控消息 API，并为待处理 `message`/`answer`。
`target_role` 只能为 `planner`、`implementer`、`reviewer`：发给具体 Agent 的消息
只能预检同一个目标；发给 Orchestrator 的人工意图可显式选择一个 Agent，不广播。
客户端不能提交新正文、角色身份、Artifact、预算重置、状态或执行幂等键。

HTTP 200 响应包括：task_id、trace_id、task/runtime revision、message_id、
correlation_id、解析出的 target_member_id/role、rework_rounds/max_rework_rounds、
当前 budget_usage，以及三个显式标志：

```json
{
  "scope": "continuation-preflight",
  "checks_passed": true,
  "execution_ready": false,
  "agent_dispatched": false
}
```

200 只代表本子步骤的检查通过，不代表可立即执行或已恢复。预检不占用预算或锁定
消息，重复预检当前状态不变时结果相同；其他操作改变任务/消息/预算后，结果可以改变。
未来真正的继续接口必须重新校验并原子认领，不能信任客户端携带旧预检结果。

## 前置条件与错误

- 任务为 `needs_human`，聊天室 Active，无本地执行/取消，修订号匹配；否则 409。
- 第 3 子步骤加入持久化占用检查：Claimed/NeedsHuman 请求阻止新预检；Pending 仅
  允许同消息/目标预检，实际继续仍须匹配幂等命令并认领。占用记录不可读取则 503。
- 消息属于当前 task/room/trace，Human 身份与目标身份唯一；缺失或跨任务消息 404，
  非受控 Human 意图/目标不匹配 422，已 ACK 的消息 409。
- 已持久化的目标 Agent binding 必须与当前服务配置一致；不匹配 409。
- 使用实际 WorkflowController 返工上限及实际 Executor 的 ConversationBudgetGuard；
  不从默认设置创建备用 Guard。Guard 未配置或账本不可读取则 503，不能当作零用量。
- 累计返工轮数达到上限就拒绝任何目标的预检（包括上限为零的保守禁用配置）；
  人工消息不能隐式增加轮次/预算。预算已耗尽时仍可发消息，但不会自动恢复代码返工。
- 沿用当前服务配置的回合、已报告 Token、执行时长、聊天室消息、重复消息和提问预算。
  持久化用量不删除、不归零；缺失 Token 数据保留 `turns_without_token_usage`，
  不将已报告 Token 总数当作实际总成本。历史预算策略冻结还需要后续配置版本管理。
- 额外字段、无效 UUID、非严格整数 revision 或非 Agent 目标返回 422；未配置服务 503。

## 本子步骤没有覆盖的内容

Worktree 重新核验、Artifact 证据恢复/完整性、注册表实际适配器/会话准备与权限接线
已由[第 2 子步骤内部内核](continuation-runtime.md)实现；本 HTTP 预检仍不执行这些准备。
证据时效性与正式评审仍需后续受控验证。此处不提供已批准的执行授权，不运行
CompletionGuard，不建立远程身份认证或多 worker 锁；仅限可信本地单进程。
`needs_human` 的原状态转换规则保持不变；现有启动恢复仍不会自动恢复此等待态。

后续顺序：

1. 已完成本步：契约、前置条件和只读预检。
2. 已完成内部 Runtime/Artifact 恢复与单个目标回合；任务仍停在人工等待，不开放通用状态回归。
3. 已完成[持久化继续请求、幂等认领/消费](continuation-claims.md)，处理重复点击与独立连接争抢。
4. 保持预算边界、故障后的安全暂停与恢复。
5. Fake 完整闭环验收，再接第 7 项 UI 输入/继续按钮。

离线验证（真实 SQLite/API，模拟任务循环，无模型）：

```bash
.venv/bin/pytest -q tests/test_continuation_preflight.py tests/test_human_message_api.py tests/test_task_api_contract.py tests/test_persistent_task_service.py
.venv/bin/python scripts/check_offline.py
```
