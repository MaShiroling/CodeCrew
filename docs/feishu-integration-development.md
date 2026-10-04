# 飞书接入开发说明（v1 已实现）

D3～D6 已冻结，见[需求](feishu-integration-requirements.md)。部署见[配置指南](feishu-setup.md)，实际检查和平台限制见[验收记录](feishu-acceptance.md)。真实飞书长连接烟雾测试尚未执行。

## 模块和数据流

```text
官方 SDK receiver 子进程（解析 + 有界队列）
  → 父进程 asyncio FeishuRuntime
  → FeishuBridge（白名单、幂等、固定绑定、busy）
  → StandaloneChatService.post_external_message
  → 原有 BoundedDiscussionDispatcher / AgentRuntime
  → 持久化的 Agent 消息
  → FeishuOutbox（关联过滤、游标与插入同事务）
  → OfficialFeishuSender（官方回复 API、有限重试）
```

`app/feishu/` 按 models、adapter、store、bridge、outbox、sender、transport、runtime、privacy 分工，没有新的模型调度器。只有主进程访问 SQLite、调度 Agent 和发送消息。子进程不访问任务服务、数据库或 Agent。

## SDK 与生命周期

可选 extra 固定官方 `lark-oapi==1.7.3`。普通安装、普通 chat-serve、chat-demo 都不导入 SDK。只有显式 `chat-serve --feishu` 和 `CODECREW_FEISHU_ENABLED=true` 才装配。缺少 SDK、凭据或任一白名单时直接报固定错误。

已检查安装源码并离线验证事件对象、Create/Reply 请求对象和 reconnect hooks；这不是线上连通证明。SDK 的 `Client.start()` 使用模块全局 loop，阻塞运行，连接地址发现含同步请求，没有公共 stop API。因此一个可终止的 receiver 子进程负责长连接；退出时 terminate/join，必要时 kill/join 并确认，不对 SDK 私有连接做关闭操作。仅观察版本固定的 `_conn` 来报告首次连接。SDK logger 禁用，以免其错误日志泄露 URL、token、完整 ID 或正文。

主进程发送用官方同步 API 放入 `asyncio.to_thread`，HTTP timeout 15 秒；避免 SDK 异步入口内部同步获取 token 阻塞 FastAPI loop。只有固定错误码能进入状态和日志。

FastAPI 启动顺序为 chat store → legacy dispatcher → bounded dispatcher 启动栅栏 → Feishu 恢复和连接；退出顺序相反。停止收新消息，排空现有入队消息（最多 5 秒），给发送任务 35 秒收尾，再停止 bounded 工作。运行中未完成推理由原有 dispatcher 中断；下次启动不自动恢复推理。进程启动失败、异常队列和启动途中失败均清理资源。

SDK callback 只解析并放入容量 128 的跨进程队列；父端通过线程安全 `call_soon_threadsafe` 交接到 asyncio 队列（容量 128）。队列满会记录固定诊断；SDK 子队列满会抛固定错误供 SDK error ACK。**ACK 不等于 SQLite 已持久化**：内存交接期间崩溃或主队列溢出可能丢事件，不能宣称无损接收。已进入 ingress 的消息才受持久化幂等保护。

## Migration 19 与一致性

原仓库最高 migration 为 18，新增 19 `create_feishu_bridge`，与 chat 15/16/18、coding authorization 17 兼容。没有改已有表结构；旧消息 JSON 缺 external_source 时仍有效。

| 表 | 约束和用途 |
| --- | --- |
| feishu_bindings | 主键 `(app_id, chat_id)`，room_id 唯一；status、start_sequence、scan_cursor、时间戳。 |
| feishu_ingress | `(app_id,event_id)`、`(app_id,message_id)` 双唯一；发言者、指纹、内部消息/correlation/run、opening_role、状态。 |
| feishu_ingress_events | `(app_id,event_id)` 唯一，关联 ingress；同一消息被新 event ID 重投时保留所有别名，防止这些 event ID 再指向另一条消息。 |
| feishu_outbox | `(app_id,source_kind,source_key)` 唯一；Agent 源消息、run 状态或 ingress 通知分别去重；sequence、重试时间、次数、回执、安全错误。 |

稳定 UUID 从有长度前缀的 app/chat/message 身份派生；消息和 run 沿用现有稳定根消息语义。先白名单和路由验证，再创建/复用 room 和 binding，然后 claim ingress，持久化 external Human 和 run，完成 ingress 映射，最后 schedule。存在 run 的不确定重放只补记录，不再次 schedule。活跃 CREATED/RUNNING/PAUSED 期间新 ingress 只落 busy 状态和一次通知，不写 Human，不污染上下文。bound room 的本地 bounded 新 run 也检查同一事务中的活跃状态。

重启先 fence 未完成 bounded，再恢复 received：已有 Human → 补 run 并中断；没有 Human → interrupted 通知；已完成或被中断的 run 从不重跑。房间创建后绑定前、Human 保存后完成 ingress 前等崩溃窗口靠稳定键收敛。

出站扫描同时要求：active binding、绑定起点之后、同 app/chat/room、已 admitted ingress 的 correlation、有真实 bounded turn 对应的 Agent 消息幂等键。只扫 room 或只按 Agent 身份去重均不成立。A→B→A 三条均可投递。游标推进与 outbox 插入同一事务；Agent 消息已落库但未扫描时可补扫。普通本地讨论、Human 消息和历史消息均不导出。网页向外部链追加本地 reply 被拒绝；可创建新本地讨论，显式背景引用仍不改变外发范围。

每个 chat 的最早未终结 outbox 项阻塞后续项，跨 chat 可继续发送。状态 pending → sending → sent 或 retry_wait/failed；默认最多 5 次，指数退避 2 秒起、60 秒封顶。单实例启动将旧 sending 标为结果未确认并重试。每次发送前复查白名单和 binding 状态。耗尽后 failed 可见，不自动无限重试，也不重跑模型。

这是一种持久化、有限重试的至少一次投递策略，**不是保证最终送达**，更不是跨系统 exactly-once。远端发送成功但本地回执尚未提交时崩溃可能重复发出；Agent 与本地 outbox 去重不能消除该窗口。

## 身份、权限与隐私

`ExternalChatSource` 仅能附在 Human 消息，platform 固定 feishu、ID 有格式限制、显示名不允许控制符/格式控制符。显示名不是身份，也不参与 replay 指纹。旧 local 消息的空 external_source 不参与指纹，保留原重试行为。API 保留来源，UI 显示姓名/匿名标签和 sender 哈希短码（同名用户可区分），Agent 的当前发送者与有界历史也使用外部标签。

preflight 和 authorize 都拒绝外部来源；authorize 在读取旧授权回执前检查，防止借 replay 绕过。HTTP 发消息不能自行传 external_source。没有把外部事件映射到 Task、Worktree、Verifier、shell 或 Git。只读 runtime 的目录权限、CLI 工具限制和凭据隔离保持原样，Windows 不能运行的原安全边界未被放宽。

日志只含固定分类和 ID 的 SHA256 短码，状态接口只返回 enabled/state/counts/固定 error，不含配置、正文和平台身份。外发仅使用持久化最终 Agent 回复和固定状态；发现已知凭据原文、疑似密钥、绝对路径、stderr、隐藏测试等则整条拦截。此过滤是保守保护，可能误拦普通技术讨论，不是通用语义 DLP。SQLite 保存原始聊天和必要平台身份，需受本机访问控制保护。

## 离线与真实验收

Fake 验收覆盖解析、白名单、同名用户、双身份重放、并发 busy、双 chat 隔离、A→B→A、回合限额、出站顺序/重试/封顶/恢复、事务回滚、coding 拦截、CLI、UI 和生命周期。新 Fake 测试注入只供 FakeAgent 使用的空目录管理器，不改变生产 Unix 权限检查。

`tests/test_feishu_sdk.py` 在未装 extra 时 skip；安装 extra 后只使用内存 Fake HTTP client，不触网。`tests/integration/test_feishu_live.py` 要求显式 opt-in 和凭据，检查真实 DM、可选 group、回复和 stop，不调用模型。默认测试不运行 live。Windows 全量结果与未修改快照逐项对照，见验收记录；macOS 与真实 Feishu 尚待实机验收。
