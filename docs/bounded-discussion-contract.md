# P6.1～P6.3：独立聊天室有界自由接话

P6.1 定义纯数据与状态迁移；P6.2 增加 SQLite 持久化和**内部调用的顺序调度器**。
P6.3 补齐运行中总时限与失败围栏。
尚未新增启动批次的 HTTP API 或 UI 开关，现有网页仍走原来的一次性回复模式。
任务聊天室和受控编码流程不使用它。纯聊天消息不会获得读写 Git 仓库或创建
编码任务的权限。

## 一个批次是什么

`DiscussionRun` 由 `run_id` 标识，关联一个独立房间、Human 起始消息与
`correlation_id`，并指定首位 Agent。它不是编码 Task，`finished` 只表示本批聊天
结束，不表示需求或代码任务成功。一次“继续讨论”应开启**新的批次**；暂停后在
同一批次恢复则保留原始起始时间与已用回合数。

默认上限为每批 8 个 Agent 回合和 600 秒，契约不允许配置得更高。Agent 回合在
启动前预留计数；到达上限后不能继续预留。P6.2 已在 SQLite 事务中原子预留
下一个回合并保存 FIFO 邀请队列。P6.3 给 Agent 启动、流式输出和等待结果
设置同一个运行中截止；人工暂停/继续与显式取消入口留待 P6.4。
P6.1 的纯函数本身不启动任何 Agent。

## 状态与停止原因

| 状态 | 含义 | 后续 |
| --- | --- | --- |
| `created` | 批次已建但未启动 | 可启动、取消或标记中断 |
| `running` | 可以预留 Agent 回合 | 可暂停、待人工、结束、失败、取消、触限或中断 |
| `paused` | Human 暂停；原预算不重置 | 可恢复、由 Human 结束、取消或标记中断 |
| `awaiting_human` | Agent 需要 Human 输入 | 本批终止；Human 回复后开新批 |
| `finished` | Agent 或 Human 结束讨论 | 终态，不代表编码成功 |
| `failed` / `cancelled` / `limit_reached` / `interrupted` | 失败、取消、回合或时长触限、结果不确定 | 终态，不自动重派 |

每个暂停或终态必须带匹配的 `stop_reason`。`turn_limit` 只有已预留回合数等于
上限时才有效；`time_limit` 只有从首次启动起经过完整时限后才有效。非法迁移、
时间倒退和停止原因不匹配均被模型拒绝。服务重启的不确定结果只能记为
`interrupted`，不能假装成功。P6.2 的调度器启动时会围栏旧的未完成批次和
回合，不自动重派；完成的批次可从 SQLite 读取。

## Agent 的下一步决策

新模式使用 `DiscussionReply`：`content` 加上明确的 `next_action`。

- `handoff`：`handoff_to` 必须指定一到两位其他 Agent；顺序由后续调度器执行。
- `await_human`：要求 Human 补充信息，不允许同时指定 Agent。
- `finish`：结束本批，不允许同时指定 Agent。

正文中的 `@月见` 等文字只属于内容，**不会**自动变成 Agent→Agent 交接；
结构化目标才可路由。模型输出不能指定自身、Human、Verifier、写入路径或权限。
P6.2 的服务端会按成员身份、幂等键和预算重新校验，不能信任模型自报。

## P6.2 内部调度边界

`BoundedDiscussionDispatcher.start(root_message_id, opening_role=...)` 只接受新的、
仅发给首位 Agent 的 Human 消息。该消息不能已有旧模式的 Agent 回合；同一个
`correlation_id` 一旦建了批次，旧的一次性 fanout 也不能并行接管。批次创建
幂等，相同根消息不能以另一角色或另一预算重新创建。

调度器一次只从 SQLite 取一个邀请、预留一个回合；拿到确认回复后保存消息、
再把结构化 `handoff_to` 追加到队尾。Agent 可以在同一批次被再次邀请，
`handoff_to` 中两位成员按顺序而非并发执行。`finish`、`await_human`、触限、
失败或中断会结束队列并确认未执行的邀请，不把它们留作下一次隐式任务。
回复写入后、队列更新前若进程退出，重启会保留可见回复并把批次标记为
`interrupted`；不会重复调用模型或声称批次成功。

内部调度仍通过 `StandaloneChatAgentRuntime` 的独立私有工作目录和 `READ_ONLY`
请求执行。模型输出必须是 `DiscussionReply` JSON；正文 `@` 不路由，携带写入
字段或非法交接目标会失败停机。P6.2 没有给模型任何编码授权。

## P6.3 运行中截止与保守停机

总时限从第一位 Agent 回合预留时算起，不能靠模型自报剩余时间。调度器用
`asyncio.timeout` 对启动、事件流和等待结果统一计时；若总时限先到，就尝试
取消 CLI，不采纳迟到回复，也不再唤醒队友。只有取消或终态结果得到确认时，
批次才记为 `limit_reached/time_limit`；若取消确认丢失，记为
`interrupted/uncertain_result`，不能假称 Agent 已停止。单回合超时先于总时限
时，确认取消后记 `failed/agent_failed`；未确认则仍是 `interrupted`。
SQLite 接受回复前再次检查时限，避免迟到的 `finish` 绕过硬截止。

调度器异常也会围栏未认领的批次、确认未执行邀请；进程重启继续把所有未完成
批次标为 `interrupted/server_restart`，不自动重派。当前仍没有给 Human 暴露
暂停、继续或取消的 HTTP/UI 操作，不能把 P6.3 当作用户可用的自由接话模式。

## 当前验收范围

`tests/test_bounded_discussion_contract.py` 覆盖纯契约；
`tests/test_bounded_discussion_dispatch.py` 用 Fake Agent 覆盖顺序接话、同一 Agent
重复发言、FIFO、触限、重启围栏、无效决策、旧模式隔离及只读目录清理，
并验证运行中总时限、单回合超时、取消结果不确定和调度器异常。
真实模型连续接话、Human 控制 API 与 UI 控件均**尚未验收或实现**。
