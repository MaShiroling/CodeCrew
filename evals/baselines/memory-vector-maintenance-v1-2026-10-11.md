# Qdrant 历史回填/审计/修复/重建真实合成验收 v1

日期 2026-10-11；第二阶段第 4/7 子步骤。工具
[`check_memory_vector_maintenance.py`](../check_memory_vector_maintenance.py)，运行显式
`--live`。获授权连接既有本机 Qdrant 1.19.0，不启动新服务、不迁移真实聊天库。

成功原始归档（本机 Git 忽略，不随仓库发布）：
`evals/results/memory-vector-maintenance-v1/20261011T135636Z-ece64dbd/`。
`report.json` 含代码 SHA-256、每轮 run ID/计数/状态和自有集合清理结果，
`synthetic.sqlite3` 是独立合成规范库（schema 28）。最初沙箱连接失败的归档
`20261011T134212Z-acf74b47/` 保留，不算通过或质量失误；没有创建集合。

## 检查结果

| 检查 | 真实观察 |
| --- | --- |
| 历史回填→持久续跑→双向审计 | 目标订阅前创建 4 条缓存事实，1 条退役；每页 1 条，新 worker 同 run 续跑，入队/投递 4；审计 local/remote/current=4/4/4，观察一致 |
| 缺点/向量损坏→高版本修复→旧写拒绝 | 自有点物理删 1、另 1 实际向量修改但 payload/hash 不变；audit missing/drift=1/1；repair 新事件 2，投递后旧请求收到更高版本，fresh audit 一致；原文不变 |
| 远端孤儿 | 自有测试身份只存在远端；repair 报 orphan=1、无新增投递；远端点仍存在，未认领或自动删除；由探针显式清理该测试点 |
| 新 namespace 重建 | 同模型缓存回填/投递 4；新/旧两集合 fresh audit 均一致；没有默认切换、旧集合删除或模型请求 |

`complete=true`、`cleanup_complete=true`；两个随机自有 collection 已删除且 GET
确认消失，Qdrant 服务及持久数据卷保留。embedding/Agent 请求均 **0**。

## 离线与回归

新增 66 项：维护/操作入口/探针 40、REST inspection/scroll 26。连同既有 outbox
定向 **131 passed**。扩大记忆/评测/聊天/讨论控制/CLI 定向 **642 passed**，两条
既有依赖警告；Ruff、git diff --check 与旧 24 题有序来源冻结复验通过。
不是全仓测试，没有重跑付费自动提炼/回答质量或检索 A/B。

## 解释边界

本轮只验证合成历史维护和被显式注入的损坏，不是实际用户规模、生产后台调度、
ANN、分布式数据库、检索质量或延迟/成本数据。审计是分批观察，不是跨库原子证明。
新模型缓存缺失等待、远端超前/归属冲突不覆盖、并发提交与事务失败主要由离线测试
覆盖；真实 probe 不可扩张解释为所有故障均已端到端验证。

运行时仍使用原 MemoryVectorIndex；私有 `.env`、真实聊天库、阈值和默认不变。
新模型独立目标可以创建但不会自动生成其向量或完成热切换；见
[操作与限制](../../docs/memory-vector-maintenance.md)。下一步第 5/7 才是聊天混合召回接线。
