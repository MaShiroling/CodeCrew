# 第二阶段第 6/7 步：故障与恢复验收

后续 [优化读路径第 4/5 步](memory-qdrant-optimization.md)已针对新批量/合并检查实现
扩展并复跑矩阵：[新报告](../evals/baselines/memory-qdrant-optimized-regression-v1-2026-10-12.md)
两关键词各 23 组、14 对实际停启，各 audit 25/25 一致。本页旧成绩和历史文件不覆盖。

2026-10-12 完成。此步增加可重复执行的故障矩阵和真实 Docker 停服探针，不修改
业务实现、默认召回、私有配置或真实聊天数据，也不自动启动后台 worker。
沿用第 3–5 步的事务 outbox、有限重试/租约、版本墓碑、规范复验与关键词降级。

## 验收矩阵

| 故障/边界 | 验收内容 | 实际证据范围 |
| --- | --- | --- |
| Qdrant 不可用 | gateway 查询仅关键词召回；不调用合成查询 embedder | 专属容器实际 stop |
| 故障期间写入与积压 | 新建、缓存新版本、撤销仍提交规范事务；有限退避后进入死信，不无限重试 | 实际 SQLite/outbox + 停服 |
| 双端恢复 | SQLite 新实例保留死信；相同 Qdrant endpoint/storage 重启保留旧点；恢复本身不自动重投 | 实际容器 start；新 store/worker 对象 |
| 显式恢复与乱序 | 显式 retry_dead，版本合并后恢复召回；延迟旧 mutation 不能覆盖新版本/墓碑 | 实际条件 HTTP 写与双向审计 |
| 远端成功但本地未 ACK | 活跃租约不可接管，过期重试；旧 token 不可 ACK | 刻意遗漏 ACK + 新 worker 实例，**未杀 OS 进程** |
| 缺向量缓存 | 先墓碑并标记 waiting；新匹配缓存产生新版本后恢复 | 合成缓存，0 模型 |
| 撤销/替换/完成/取消/物理删除 | 各自停服期间变更；恢复前后的读取都不返回旧项；恢复投递后旧写入不可复活 | 五次额外实际 stop/start |
| 范围隔离 | 同向量的其他房间、当前讨论样本不得混入初始语义结果 | 实际规范候选、服务端过滤与查询编排 |
| 最终一致性 | 新 audit 同时扫描本地/远端，结果一致；无 pending/processing/waiting/dead | 13/13 当前点，outbox 21 delivered / 19 superseded |

物理删除仅在隔离合成 SQLite 中移除依赖及事实，用于证明派生墓碑不会丢失；
不是新增生产删除接口。规范复验仍不提供双库原子提交，audit 是当次观察证据，
不代表运行中的数据库永远一致。此轮不覆盖网络分区/超时分布、磁盘损坏、真实
worker kill、集群 failover、持续负载或生产 SLA；不把合成向量当成真实模型质量成绩。

## 可重复运行

[`check_memory_qdrant_faults.py`](../evals/check_memory_qdrant_faults.py)默认只打印计划：

```bash
.venv/bin/python -m evals.check_memory_qdrant_faults
# 明确允许专属临时容器创建、六次停启和清理后：
.venv/bin/python -m evals.check_memory_qdrant_faults --live
```

前置：本机 Docker 运行，已经存在 `qdrant/qdrant:v1.19.0` 镜像；当前工具使用本机
`/Users/mashiro/.docker/bin/docker`。不调用 docker pull，不借用 `.env` 或现有 6333
服务。以 image inspect 的本地 image ID + `--pull=never` 固定镜像，随机 UUID 容器名
和 ownership label，选空闲 loopback 端口，唯一结果目录 bind 到 `/qdrant/storage`。
端口选择与 create 存在竞争窗口；若端口被抢占则失败并清理自有资源，不操作占用者。

每次 start/stop/清理前复验完整 ID、名称、标签、镜像、bind source/destination
及 loopback 端口。不匹配就拒绝控制；失败也不能凭名称删容器。inspect 错误还需检查
Docker daemon 与容器列表，不能把检查失败记为清理完成。最终仅 `rm --force` 经验证
的专属容器，不删 mount/volume、不 prune；**结果目录的 report、SQLite 和 Qdrant
bind 数据保留**。探针专属资源而非生产库，不需要清空现有 collection。

Docker 控制语法按[官方 Docker 文档](https://docs.docker.com/reference/cli/docker/container/create/)
核对；就绪检查 `/readyz`，持久化挂载按 [Qdrant 1.19.0 文档](https://github.com/qdrant/qdrant/blob/v1.19.0/docs/DEVELOPMENT.md)
与官方源码核对，并经本机真实服务验证。子命令超时 30 秒，单次 readiness 等待预算
25 秒，worker 时钟在测试内推进以验证退避/租约，不必真实等待 60 秒。

报告只写检查名、统计、错误类别、模型调用计数和代码 SHA；不持久化原始 Docker/
provider 错误、密钥或真实对话。失败返回非零，成功必须同时满足旅程与自有清理完成。

## 结果与下一步

新增 [`test_memory_qdrant_faults.py`](../tests/test_memory_qdrant_faults.py) **38 项离线测试**：
真实 SQLite/FTS/outbox/召回编排，假远端；legacy/BM25 两完整旅程、数据丢失拒绝验收、
容器归属保护、失败清理/脱敏、镜像固定/禁止隐式下载、默认 plan 无副作用。
扩大定向 **738 passed**，两条既有依赖警告；Ruff、diff 与旧冻结基线通过，不是全仓验收。

[实际验收记录](../evals/baselines/memory-qdrant-faults-v1-2026-10-12.md)：九组检查通过，
六次实际 stop/start，最终 audit 一致、积压清零，自有容器已清理，0 真实 embedding/Agent。
原 6333 服务和数据卷保留；结果目录保留合成证据。随后 **第 7/7 [质量、延迟与规模
对照](memory-qdrant-benchmark.md)**也已完成，当前第二阶段 7/7；两混合质量一致、
Qdrant 应用接线更慢，默认仍 SQLite。当前仍限最多 200 个规范候选，无 ANN/大库收益结论。
