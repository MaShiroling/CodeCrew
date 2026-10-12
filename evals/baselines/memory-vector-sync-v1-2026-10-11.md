# 向量 outbox 真实合成旅程（2026-10-11）

第二阶段第 3/7 完成。实际本机 Qdrant 1.19.0，Docker 部署版本/摘要见
[第 2 步存储验收](memory-qdrant-storage-v1-2026-10-11.md)。本轮不调用模型、
不读取真实聊天库，使用每轮独立的合成 SQLite 与 3 维向量。

最终代码运行：
`evals/results/memory-vector-sync-v1/20261011T131645Z-46f76140/report.json`。
前一轮成功记录：`evals/results/memory-vector-sync-v1/20261011T131312Z-fc891562/report.json`。
两轮均获授权运行 `python -m evals.check_memory_vector_sync --live`，测试集合清理成功。
每轮报告单独保留；不将两轮拼成更大样本的质量指标。

| 检查组 | 最终轮结果 |
| --- | --- |
| 规范事务内事件、缓存写入新版本、worker 投递与真实精确读 | 通过 |
| 远端成功后故意不 ACK、租约过期、另一连接模拟重启接管并重复投递 | 通过 |
| 撤销投递高版本墓碑，随后直接发送旧版本 upsert，不能复活 | 通过 |
| 缺向量先墓碑并 waiting；缓存补入新版本后恢复 active 检索 | 通过 |

`complete / sync_checks_passed / cleanup_complete` 均为 true。
最终 outbox 为 delivered=4、superseded=3；这只是该合成旅程事件计数，不是用例成功率。
Agent/embedding 调用 **0/0**。报告记录六个实现/探针文件 SHA-256；同目录保存合成
数据库便于核查，Git 忽略结果目录，不是随仓库公开的原始归档。

探针只删除自己随机创建的 collection，并 GET 确认不存在；先查到已存在同名集合
时不采用、不删除。没有删除 Qdrant 持久化卷，服务保持运行。没有操作业务 collection、
私有 `.env` 或真实聊天数据库。模拟重启是新 SQLite 连接/worker，不是强杀用户容器。

新增离线 outbox/版本化 REST 契约 **65 项**通过，扩大定向 **576 passed**，
两条既有依赖弃用警告；Ruff 与旧 24 题有序来源冻结复验通过。
没有运行全仓、云模型质量/延迟 A/B、分布式 Qdrant 或并发负载基准。

语义是至少一次投递、版本化幂等效果和最终一致，非双库原子事务/恰好一次。
聊天默认仍使用原 SQLite 向量编排，历史回填与生产 worker/召回接线未实现。
机制和前提见[可靠同步说明](../../docs/memory-vector-outbox.md)。
