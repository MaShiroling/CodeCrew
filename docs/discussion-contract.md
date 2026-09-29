# 团队讨论消息契约与只读调度（8.1～8.2）

8.1 为聊天室增加独立的 Human→Agent 讨论消息；8.2 在显式装配的单进程服务中
增加只读讨论回合，不改变既有受控编码工作流。目前仅支持可信本地、单用户服务；
API 尚未接入前端输入框。

## 发送

`POST /api/v1/tasks/{task_id}/messages/discussion`，请求示例：

```json
{
  "expected_revision": 3,
  "idempotency_key": "6ef0ccfb-e1e5-412c-a602-797f6e0279f9",
  "content": "@白金 @月见 请讨论这个边界条件",
  "reply_to": null
}
```

`content` 必须有实际讨论内容，且包含已知 Agent 的 `@` 别名；若 `reply_to`
指向本任务聊天室中的 Agent 消息，可不写 `@`，默认回复该 Agent。回复中另写
`@` 会将被提及队友一起加入收件人，同时始终包含被回复消息的原发送者。
回复 Human 或其他任务的消息、未知 `@` 别名及无目标消息均被拒绝。
可用别名来自现有三个人格配置：白金 `@白金/@codex/@platinum`，
月见 `@月见/@kimi/@yuejian`，鲸鲸 `@鲸鲸/@jingjing/@whale/@deepseek`。
匹配完整别名，不进行模糊或前缀推断。

返回 `201`，包含保存后的 Room 消息、`target_roles`、`task_revision`，以及固定的
`scope="discussion"`、`execution_authorized=false`、`agent_dispatched=false`。
8.2 新增 `discussion_queued`：在显式装配了讨论执行器的服务上，新消息为 `true`，
表示后台异步回合已排队；它不是 Agent 回复成功的证明。幂等重放不重复排队。
重复提交相同幂等键与内容返回同一消息；相同键不同内容返回冲突。
终态任务和已关闭房间不接受新讨论。请求不允许指定发送者、消息类型、权限或执行标志。

## 安全边界

讨论消息的类型独立于现有 `human-api:` 工作流意图，不能用于预检/授权/继续。
普通执行回合不会读取它，显式把其 ID 当作执行输入也会拒绝。讨论回合只读，
只接受发给 Human 或其他 Agent 的普通聊天动作；最多接话六轮，超限停止。
Agent 失败、取消或执行结果不明时不自动重试该线程；后续可以发起新线程。
进程重启不会自动重放此前未处理的讨论投递，避免重复发起未知模型调用。
8.3 才会在 UI 提供 `@` 和回复输入；目前可用 API 发起并通过消息列表/SSE 看结果。
任何聊天内容都不能取代显式工作流授权、确定性 Verifier、Reviewer 和 CompletionGuard。
