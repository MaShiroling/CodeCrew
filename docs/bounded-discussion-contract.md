# P6.1～P6.6：独立聊天室有界自由接话

P6.1 定义纯数据与状态迁移；P6.2 增加 SQLite 持久化和**内部调用的顺序调度器**。
P6.3 补齐运行中总时限与失败围栏；P6.4 增加本地 HTTP 显式启动与人工控制；
P6.5 接入独立聊天室网页。网页默认仍走原来的一次性回复模式，须 Human 明确
勾选“开启有界接话”才使用本契约。
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
设置同一个运行中截止；P6.4 补充人工暂停、继续、取消。
P6.1 的纯函数本身不启动任何 Agent。

## 状态与停止原因

| 状态 | 含义 | 后续 |
| --- | --- | --- |
| `created` | 批次已建但未启动 | 可启动、暂停、取消或标记中断 |
| `running` | 可以预留 Agent 回合 | 可暂停、待人工、结束、失败、取消、触限或中断 |
| `paused` | Human 暂停；原预算不重置 | 当前控制 API 可继续或取消；契约另保留结束/中断迁移 |
| `awaiting_human` | Agent 需要 Human 输入 | 本批终止；Human 回复后开新批 |
| `finished` | Agent 或 Human 结束讨论 | 终态，不代表编码成功 |
| `failed` / `cancelled` / `limit_reached` / `interrupted` | 失败、取消、回合或时长触限、结果不确定 | 终态，不自动重派 |

每个暂停或终态必须带匹配的 `stop_reason`。`turn_limit` 只有已预留回合数等于
上限时才有效；`time_limit` 只有从首次启动起经过完整时限后才有效。非法迁移、
时间倒退和停止原因不匹配均被模型拒绝。服务重启的不确定活动结果只能记为
`interrupted`，不能假装成功。重启会围栏旧的 `created/running` 批次，不自动
重派；已到达安全回合边界的 `paused` 批次保留在 SQLite，必须由 Human 显式继续。
首回合尚未开始时也可暂停，此时 `started_at` 为空；显式继续使批次进入运行态并
开始计时，随后预留首回合。已有回合的暂停不会重置原始截止时间。

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

总时限从首次进入运行态算起（通常就是首回合预留），不能靠模型自报剩余时间。调度器用
`asyncio.timeout` 对启动、事件流和等待结果统一计时；若总时限先到，就尝试
取消 CLI，不采纳迟到回复，也不再唤醒队友。只有取消或终态结果得到确认时，
批次才记为 `limit_reached/time_limit`；若取消确认丢失，记为
`interrupted/uncertain_result`，不能假称 Agent 已停止。单回合超时先于总时限
时，确认取消后记 `failed/agent_failed`；未确认则仍是 `interrupted`。
SQLite 接受回复前再次检查时限，避免迟到的 `finish` 绕过硬截止。

调度器异常也会围栏未认领的批次、确认未执行邀请；进程重启围栏未确认的
活动调用，不自动重派。

## P6.4 本地人工控制 API

在可信本机单进程服务中，Human 可以使用下列**显式**入口。普通
`POST /api/v1/chats/{room_id}/messages` 仍使用旧的一次性回复逻辑；有界批次
不会因为普通聊天的 `@` 或 Agent 正文而自动创建。

| 方法与路径（前缀 `/api/v1/chats/{room_id}`） | 用途 |
| --- | --- |
| `POST /discussion-runs` | 新建 Human 起始消息并启动批次；提交 `idempotency_key`、三角色之一的 `opening_role`、不含 `@` 的 `content`，可选 `limits` |
| `GET /discussion-runs`、`GET /discussion-runs/{run_id}` | 读取持久化批次、已用回合、原始时间与控制请求标记 |
| `POST /discussion-runs/{run_id}/pause` | 尚未启动则立即暂停；Agent 正在运行时，先保存暂停请求，等当前回合确认完成后暂停 |
| `POST /discussion-runs/{run_id}/resume` | 仅从已暂停批次继续，或撤回尚未生效的暂停请求；原回合数与总时限不重置 |
| `POST /discussion-runs/{run_id}/cancel` | 取消待执行队列并请求停止当前 Agent；停止确认后记 `cancelled`，否则记 `interrupted` |

暂停后的邀请与投递保持待处理，不提前 ACK；继续时只处理剩余邀请。取消、到时
或失败则确认未执行邀请，不自动重派。跨房间的批次 ID 返回 404，非法状态操作
返回 409。当前服务只监听本机，没有公网身份认证；不要将控制端点直接暴露公网。
若当前 Agent 明确 `finish` 或 `await_human`，其终态优先于尚未生效的暂停请求。

## P6.5 网页入口

独立聊天室的普通“发送消息”仍调用 `/messages`。勾选“开启有界接话”后，Human
选择首位 Agent、回合上限与总秒数，提交不含 `@` 的讨论目标；UI 才调用
`/discussion-runs`。它不接受关联回复或旧话题背景，也不产生编码授权。提交后
自动退回普通模式，避免下一条消息误启动新批次。网络结果未确认时保留原文和
同一幂等键，重试不会创建第二个相同批次。

右栏按房间重读持久化批次，显示 Agent 回合数和剩余/原始时限；运行中可请求
暂停，已暂停可继续，两者都可取消。活动 Agent 的暂停请求会显示“当前回合结束
后生效”；取消无法确认时显示 `interrupted`，不是“成功取消”。有界回合不暴露
旧的一次性 `/turns/{id}/cancel` 按钮，避免两套取消语义混用。服务未配置有界
控制器时开关禁用，普通聊天不受影响。SSE 仍仅提示重读 SQLite；断线后 10 秒
轮询兜底，不是逐 Token 流输出。

## 当前验收范围

`tests/test_bounded_discussion_contract.py` 覆盖纯契约；
`tests/test_bounded_discussion_dispatch.py` 用 Fake Agent 覆盖顺序接话、同一 Agent
重复发言、FIFO、触限、重启围栏、无效决策、旧模式隔离及只读目录清理，
并验证运行中总时限、单回合超时、取消结果不确定和调度器异常。
`tests/test_bounded_discussion_control.py` 验证人工控制、跨重启保持、本地 HTTP
显式入口、旧模式隔离和 Fake 三角色顺序接话。`tests/ui_chat.test.cjs` 验证网页
模式、预算、幂等重试和控制；本机 `chat-demo` 实际页面验证 Fake 三角色接话、
批次终态与刷新恢复。P6.6 增加
[`test_bounded_discussion_live.py`](../tests/integration/test_bounded_discussion_live.py)
的同路径 Fake 对照和默认跳过的三模型在线用例；运行方法见
[P6.6 在线验收](bounded-discussion-live.md)。真实模型连续接话与真实 CLI 人工控制
在取得真实运行结果前**仍未验收**。
