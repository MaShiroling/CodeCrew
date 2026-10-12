# 第二阶段第 3/7 步：向量 outbox 可靠同步

2026-10-11 完成。新增事务内变更捕获、显式 worker、有限重试/死信/租约接管、
Qdrant 服务端单调版本和持久删除墓碑；真实合成旅程通过。聊天仍走旧 SQLite
MemoryVectorIndex，没有启用生产 Qdrant 召回或自动后台进程。当时未做历史回填；
后续第 4 步[历史维护工具](memory-vector-maintenance.md)已完成，需显式操作。

## 事务与版本

迁移 **27** 在新建或下一次显式初始化聊天室数据库时安装；本轮只迁移临时测试库，
没有打开真实聊天库、重启聊天服务或修改私有 `.env`。
[`vector_outbox_schema.py`](../app/chat/vector_outbox_schema.py)创建三个表：

| 表 | 责任 |
| --- | --- |
| `standalone_chat_vector_revisions` | 每个记忆身份的单调整数版本、房间与删除状态；事实物理删除后仍保留 |
| `standalone_chat_vector_targets` | 显式订阅的目标身份、模型指纹和维度；不存 URL、API key 或原文 |
| `standalone_chat_vector_outbox` | `(target_id, memory_id, revision)` 唯一事件、状态、尝试次数、租约、下次时间与安全错误码 |

规范记忆增/改/退役/物理删除，缓存向量增/改/删，以及来源消息/讨论归属变更，
由 SQLite 触发器在原事务内更新版本并给已注册目标入队。纠正时旧记忆与新版各自
有事件；待办完成/取消改变 active 状态，走同一删除路径。版本不是内容哈希，
重复 API 操作没有 SQL 变更时不产生新事件。outbox 入队失败使原事务一起回滚，
不会“事实提交成功但事件丢失”。远端故障不会进入规范事务。

迁移为既有记忆建立初始版本但不投递旧数据。未注册目标不会产生投递行；注册只
订阅未来变更，**不隐式扫描历史**。历史回填由第 4 步显式工具完成。记忆/缓存的身份字段禁止
原地变更；新身份应建立新记录。版本限在 1–2^53−1，超界使事务失败而非回绕。

## Worker：至少一次，而非双库原子提交

[`MemoryVectorOutbox`](../app/chat/vector_outbox.py)每次最多处理 32 条，可显式设为
1–200。短 `BEGIN IMMEDIATE` 事务完成 claim，网络调用在事务外，不持有规范写锁。
多连接不会同时 claim 同一有效事件；默认租约 60 秒，接管生成新随机 token，
旧 token 无法确认新 claim。超时/崩溃不等于远端一定没写入。

- 投递前读取当前规范状态、来源与缓存，旧事件被合并为 `superseded`，不重放旧事实快照。
- 缓存必须与模型/维度、内容哈希相符，BLOB 长度、有限值、非零模长合法。
  不调用 embedding。缺失/过期/损坏时先投递删除墓碑，再记 `waiting`，不记成功活跃同步。
  后续合法缓存写入产生新版本并恢复投递。
- 来源必须同房间、本地 Human；来源/根讨论为 external 或内容明显敏感时仅投递墓碑，
  不发送原文或旧语义向量。这沿用保守守卫，不是完整 DLP 或多租户权限系统。
- 默认最多 5 次尝试，指数退避 2 秒起、60 秒封顶；耗尽为 `dead`，包含 claim 后
  崩溃导致的租约耗尽。仅显式 `retry_dead()` 批量重投当前版本，非无限循环。
- 远端已写但本地 ACK 前崩溃，租约过期后重复投递；远端相同版本幂等，状态可收敛。
  规范在发送期间变化，旧事件最终标记 superseded，新事务已经保留后续事件。

状态为 `pending / processing / delivered / superseded / waiting / dead`。`status()`
只返回数量；失败仅保存固定错误码，不保存异常正文、URL、凭据或事实。
`delivered` 不是现实待办完成证明，也不是双库同一时刻一致。当前只提供显式单批次
运行，不会替用户安排自动周期任务；租约恢复/退避需要后续运行 worker。

## 远端版本栅栏和墓碑

单靠 worker 租约无法阻止“旧 HTTP 请求已发出，之后才到达”。按照 Context7 核对的
[Qdrant 1.19 条件 upsert](https://api.qdrant.tech/api-reference/points/upsert-points)，
[`QdrantVersionedVectorBackend`](../app/chat/qdrant_vector_sync.py)使用服务端
`update_filter` 限定旧点版本小于本次版本，`wait=true&ordering=strong` 等待确认。
已有点条件不满足时不覆盖；新点会插入。因此删除不是物理删点，而是写入更高版本
的 `status=tombstone`，保留其栅栏。否则旧请求遇到“点不存在”仍会重新插入。

墓碑用固定单位向量替换旧语义向量、不保存原文；查询的 active filter 在排名前排除。
同版本不同 payload 被拒绝；`completed` 也可能是条件不满足的 no-op，因此追加
[单点读取](https://api.qdrant.tech/api-reference/points/get-point)，只有相同 payload 的
相同版本或同范围的更高版本才允许 ACK。原向量的 float32 哈希随 payload 保存，
用于同版本重试冲突检查；它不独立证明服务端没有被外部修改。

版本化 collection 为原模型 collection 名加 `_sync_v1`，另加 integer 版本索引。
普通 upsert/delete 在新适配器中禁止，避免绕过栅栏；第 2 步旧 adapter 与探针仍保留。
目标身份为 endpoint/collection 的 SHA-256，换 API key 不改变投递目标；不含密钥。
每个已注册模型目标独立收到变更/墓碑；不会擅自发现或删除未注册的旧 collection。

当前保证限定：本机单节点 Qdrant 1.19.0、受信应用写者、每个 namespace 对应一份
规范 SQLite。人工直接写点、物理删墓碑、清空卷、共享 namespace 给不同规范库或
把规范库回滚到旧快照都可能破坏版本前提。后续第 4 步已提供保守审计/修复和新空间
重建；墓碑 GC/旧模型清理仍未实现。本轮没有分布式集群或大规模性能证明。

## 显式操作入口

[`python -m app.chat.vector_sync`](../app/chat/vector_sync.py)默认只打印计划，不读配置/
数据库、不联网。运行要求用户显式给已有且迁移到 27 的数据库、已确认的模型指纹
（不是仅模型名称）与维度；不会迁移库或触发 embedding。模型配置不从私有聊天
设置中自动猜测。以下占位参数不能直接照抄成真实值：

```bash
.venv/bin/python -m app.chat.vector_sync
.venv/bin/python -m app.chat.vector_sync --status \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions 1024
# 首次显式建目标并订阅未来变更（不回填既有记忆）：
.venv/bin/python -m app.chat.vector_sync --run --initialize-target \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions 1024
# 后续有界投递；仅需人工恢复死信时加 --retry-dead：
.venv/bin/python -m app.chat.vector_sync --run --limit 32 \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions 1024
```

它使用既有 `CODECREW_MEMORY_QDRANT_*` 连接配置，但不改变 chat-serve 后端。
第 5 步已接[可选查询编排](memory-qdrant-recall.md)：embedding on + VECTOR_STORE=qdrant
才读版本化集合；默认不改、不自动启动 worker，仍需上述显式准备/投递。

## 验收证据

新增 **65 项离线测试**：事务回滚/入队失败、各生命周期/物理删除、缓存与来源漂移、
模型目标、租约并发接管/崩溃、退避/死信/人工重投、延迟旧请求、REST 条件/回执、
旧库迁移、无网操作入口及探针碰撞保护。扩大记忆/评测/聊天/CLI 定向
**576 passed**，两条既有依赖警告；Ruff、diff 与旧 24 题冻结复验通过。不是全仓测试。

[真实旅程报告](../evals/baselines/memory-vector-sync-v1-2026-10-11.md)使用独立临时
SQLite 与随机 Qdrant collection，四组检查通过：事务捕获→缓存投递→读取，
远端成功 ACK 前崩溃→重启重试，撤销→墓碑→延迟旧写入拒绝，缺向量等待→新版本恢复。
0 Agent/embedding；只清理自有测试 collection，业务库与持久化卷未操作。
不是自动写入质量、ANN、真实用户旅程或生产调度验收。

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_memory_vector_outbox.py tests/test_qdrant_vector_sync.py
.venv/bin/python -m evals.check_memory_vector_sync         # plan only
.venv/bin/python -m evals.check_memory_vector_sync --live  # 显式合成数据库与测试集合写入/清理
```

第 4/7 [历史维护](memory-vector-maintenance.md)与第 5/7 [可选混合召回](memory-qdrant-recall.md)
随后均已完成；第 6/7 [实际故障/恢复矩阵](memory-qdrant-faults.md)与第 7/7
[质量/延迟/规模对照](memory-qdrant-benchmark.md)也已完成，当前 7/7；
两混合质量一致、Qdrant 应用接线更慢，仍不切默认召回。
