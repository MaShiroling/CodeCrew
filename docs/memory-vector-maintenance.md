# 第二阶段第 4/7 步：历史回填、审计与非破坏式重建

2026-10-11 完成。新增迁移 **28** 的持久游标/发现记录、每页 1–16 条的显式维护
工具；复用缓存，不调用 embedding/Agent。真实 Qdrant 合成旅程通过，两测试集合已清理。
没有迁移真实聊天库、修改私有配置、重启聊天服务或切换生产检索。

## 三种模式及完成语义

| 模式 | 本地扫描 | 远端扫描 | 写入 |
| --- | --- | --- | --- |
| `backfill` | 已知身份的当前版本，包括退役/删除墓碑 | 无 | 当前版本入 outbox，唯一键去重 |
| `audit` | 批量检查期望 payload **和实际向量** | 无 active 过滤的分页身份检查 | 仅维护游标/报告，不改事实、投递或远端 |
| `repair` | 同 audit | 同 audit | 仅 missing/drift 提升派生版本并入 outbox |

`scan_complete=true` 只表示扫描结束，**不等于已同步**。backfill/repair 不报告一致，
需要显式投递、处理 waiting/dead，再新建 audit。审计只有在两向均完成、没有差异/
缓存等待/未知点、期间没有该目标新 outbox 事件且没有未完成投递时，才返回
`consistent_at_observation=true`。它是分批观察结果，不是双库原子快照；远端被其他写者
在已读页面后修改仍可能逃过本次检查。共享 namespace、规范库回滚等需要人工介入。

## 断点与事务

[`MemoryVectorMaintenance`](../app/chat/vector_maintenance.py)新建 run 时捕获 UUID 排序
高水位和该目标 outbox 事件水位。本地 `memory_id > cursor AND <= high_water` 分页，
远端按 Qdrant 返回的 `next_page_offset` 原样续读，支持 UUID/整数（包含 0）。
迁移 28 只新增两张普通表；不修改已应用的迁移 27、不创建远端资源或触发模型请求。

- 每页入队/版本修复、计数、发现记录、游标在同一短事务提交。页失败完整回滚，
  固定错误码落库；同一 run ID 可重试，不能重新生成 run ID 来假装续跑。
- HTTP 在 SQLite 写事务外。回到写事务再核对规范版本/房间；期间改变的记录跳过，
  标为 `changed_during_page`，由已订阅的变更事件继续处理。
- 并发续跑允许重复读取远端，但 checkpoint CAS 只让一个调用提交页面，避免重复
  增版本/统计。提交后崩溃再执行同 run 不会重复入队；完成 run 再读没有 HTTP。
- 开始前显式注册目标，因此扫描中新增的小 UUID 即使落在已扫游标前，也会由实时
  outbox 捕获。扫描中产生事件使 audit 不宣称干净，需要投递后重新审计。
- 单页限制的是记录/网络响应量，不是恒定查询耗时或全库查询复杂度证明；响应仍有
  2 MiB 上限，高维过大响应应缩小页长重试。没有大规模性能结论。

## 比较与保守修复

[`QdrantVersionedVectorBackend`](../app/chat/qdrant_vector_sync.py)批量取点时要求
payload/vector，检查确定性点 ID、归属、模型/维度、版本、内容/来源锚点、向量哈希、
以及 Qdrant Cosine 归一化后的实际向量（逐维容差 1e-5）。返回异常、重复/越界点或
不前进的游标使当前页失败，不能忽略后报告成功。

`missing/drift` 的修复不是重投旧版本：规范版本事务内 +1，向**所有已注册目标**
入新事件，worker 再读取当前规范/缓存并使用既有远端版本栅栏写入。不会改记忆原文。
即使旧 HTTP 在修复后才到达，也不能用低版本覆盖修复值。

`ahead`（远端更高版本）、`scope_conflict`、`invalid_revision`、远端 `orphan` 或
`unmanaged` 只报告，**不自动覆盖、认领或删除**；不能假设规范库回滚后仍可信。
身份双向扫描包括 tombstone，不把已知物理删除身份误判为孤儿。finding 仅保留
ID/安全类别/版本，不持久化远端原文、向量、URL、密钥或 provider 响应。

没有当前模型/内容匹配的缓存时，worker 先投墓碑消除旧语义点，再标 `waiting`；
维护工具不补算向量或把等待视为成功。同一 materialization helper 由 worker 与
维护器复用，避免历史回填绕过来源、敏感信息或状态检查。

## 显式工具与重建

[`python -m app.chat.vector_maintain`](../app/chat/vector_maintain.py)默认仅计划，
不打开配置/数据库、不联网。显式运行要求已有且迁移到 **28** 的库、确认过的
模型指纹（不是仅 `text-embedding-v4` 名称）和维度。CLI 不自动迁移库。
以下是占位示例，不能直接用于真实数据库：

```bash
.venv/bin/python -m app.chat.vector_maintain
# 新建 backfill run，每次只提交一页；已有注册目标不用 --initialize-target：
.venv/bin/python -m app.chat.vector_maintain --run --mode backfill --initialize-target \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions 1024 --limit 16
# 使用返回的 run_id 继续；--deliver 是可选、显式的一批 worker 投递：
.venv/bin/python -m app.chat.vector_maintain --run --run-id RUN_UUID --deliver \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions 1024
# 不联网的进度/有限发现摘要：
.venv/bin/python -m app.chat.vector_maintain --status --run-id RUN_UUID --findings \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions 1024
# 投递后新建 audit（后续仍用其自身 run_id 续跑），有差异再显式新建 --mode repair：
.venv/bin/python -m app.chat.vector_maintain --run --mode audit --findings \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions 1024
# 非破坏式重建：新 prefix，新 run，新目标；旧 collection 不删除、不切换配置：
.venv/bin/python -m app.chat.vector_maintain --run --new-target --collection-prefix YOUR_FRESH_PREFIX \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions 1024
```

续跑、状态和投递必须保持相同 endpoint/prefix/model/dimensions；不同目标或模式会被
拒绝。使用非默认 prefix 时，每次维护及 `app.chat.vector_sync` 投递都要传相同的
`--collection-prefix`。`--new-target` 在本地已注册或远端同名集合存在时拒绝，
不会接管/清空它；schema 28 检查在远端创建之前执行。

重建先复用同模型缓存填新 namespace，再投递/审计，**没有自动 promotion/cutover**。
换模型/维度需要新指纹与对应缓存；错配只会等待，不触发云端重算。当前 SQLite 每个
memory_id 仅存一个模型缓存，不支持多模型同时预热/无缝切换；改缓存也会向旧目标
传播事件，不能把这一步说成完整蓝绿模型热切换。旧模型/墓碑 GC 仍未实现。

## 验证与下一步

新增 **66 项离线测试**（40 维护/操作入口/探针，26 REST 读取与游标），覆盖中断
续跑、重复入队、事务回滚、并发 checkpoint、版本再验、实际向量损坏、墓碑/删除、
孤儿报告、模型隔离/等待、目标碰撞与无副作用计划。扩大定向 **642 passed**，两条
既有依赖警告；Ruff/diff/旧 24 题冻结检索复验通过，不是全仓测试或质量 A/B。

[真实合成报告](../evals/baselines/memory-vector-maintenance-v1-2026-10-11.md)四组通过：
四条历史记录（含一墓碑）回填续跑与双向审计；缺点/实际向量损坏→高版本修复→
延迟旧写拒绝；孤儿不认领/删除；新 namespace 缓存重建保留旧集合。0 embedding/
Agent。探针人为损坏/删除的只有自有合成点，最终两个自有测试集合删除并确认消失；
Qdrant 服务/持久卷保留，真实聊天库未操作。

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_memory_vector_maintenance.py tests/test_qdrant_vector_sync.py
.venv/bin/python -m evals.check_memory_vector_maintenance         # plan only
.venv/bin/python -m evals.check_memory_vector_maintenance --live  # 显式隔离合成验证及自有集合清理
```

此步完成时为 4/7；2026-10-12 第 5/7 [可选混合召回](memory-qdrant-recall.md)随后完成，
第 6/7 [实际故障/恢复矩阵](memory-qdrant-faults.md)与第 7/7
[质量/延迟/规模对照](memory-qdrant-benchmark.md)也已完成；当前 **7/7**。
后台调度仍未自动启动；
没有切换真实用户服务或默认，不能把存储维护探针当作召回质量证据。
