# 优化读路径完整回归：第 4/5 步验收

2026-10-12 完成，[工具及边界](../../docs/memory-qdrant-optimization.md)。
`codecrew-canonical-batch-v1` + `codecrew-qdrant-readiness-v2`；本步只增回归工具/证据，
不再改业务实现、默认或阈值，也不把故障验收当成性能收益。

## 证据

gitignored 目录：`evals/results/memory-qdrant-optimized-regression-v1/20261012T025344Z-d0fbd67a/`。
report.json、legacy/bm25 独立 SQLite、自有 bind 数据保留。
`matrix_passed/cleanup_complete/complete=true`，外部 embedding/Agent **0/0**。
各旅程合成查询向量 29 次，共 58 次，本地三维单元向量，不是云请求/真实模型质量。

自有容器 `codecrew-memory-fault-1f749cfcf4c24c3e9c09f05e7c0c6da9` 已安全清理。
实际停止 **14 次**，启动 **15 次含首次**，即 14 次停服/恢复；随机 loopback 端口。
现有 6333 只读检查可访问、集合列表仍为空；真实库/私有配置/服务/默认不动。
不删证据/卷、不下载镜像、不调用模型；零新调用不等于线上记忆零成本。

Qdrant 1.19.0 本地 image ID：
`sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`。
两个空间前缀分别 codecrew_optimized_legacy / codecrew_optimized_bm25，各自完整旅程。
工具 SHA：`2f5ad782dc1bc656086611eb4988abda7ea1779852e42852cd96a1792104f941`。
测试 SHA：`576363c6c42551a00cbec87e5c247633cb89178c5b317d3c7dd5cd24249e20e1`。
报告 16 个相关运行 SHA 再次核对匹配，与第 3 步共有 app SHA 一致，本轮未改业务文件。
历史报告和原始成绩不覆盖。

## 矩阵与分母

| 内容（每条关键词路径） | 检查组数 | 验收要点 |
| --- | --- | --- |
| 原恢复/生命周期旅程 | 9 | 停服便宜降级、死信/退避/持久恢复、显式重投/乱序、租约/ACK 模拟、五生命周期、fresh audit |
| 作用域和健康形状 | 1 | 同向量的其他房间、当前讨论不混入；2 GET+1 POST、SQL=5+5 |
| 查询响应后独立线程写入 | 7 | 撤销/替换/完成/取消/物理删、缓存变化、外部标记：旧语义命中剔除 |
| 实际索引失效/恢复 | 4 | room_id/sync_revision 在向量前/两道之间缺失，拒绝查询、保留关键词；显式恢复后重新召回 |
| 两道间停服/恢复 | 1 | 已生成合成向量但无 query POST；恢复后不复用失败状态 |
| 扩展后最终审计/积压 | 1 | 两端一致，无 pending/processing/waiting/dead |

每条 **23/23**、共 **46 组**，不是 46 个真实模型答案全部正确。
原初次+五生命周期停服为六次，再加第二检查前一次；每条七次。
索引故障用实际 `DELETE .../index/... ?wait=true`，只删自有测试空间的索引；
随后显式 initializer 恢复，**生产读取仍不自动初始化/修复**。

## 关键观察

| 边界 | metadata GET / query POST | 两规范快照 SELECT |
| --- | --- | --- |
| 健康作用域 | 2 / 1 | 5 / 5 |
| 查询后撤销/替换/完成/取消 | 2 / 1 | 5 / 4（无需活跃根材料） |
| 查询后物理删 | 2 / 1 | 5 / 3（候选不存在） |
| 查询后缓存更新/外部标记 | 2 / 1 | 5 / 5 |
| 索引向量前缺失 | 1 / 0 | 5 / 无 post |
| 索引两道之间缺失，或中途停服 | 2 / 0 | 5 / 无 post |

前道失败不调用合成 embedder，后道失败增加一次本地向量生成但不发 query POST。
恢复断言要求新请求 2+1；没有 ready/失败缓存、隐式重试。SELECT 不含初始候选/
FTS/最终复验/PRAGMA，也不表示内部扫描/处理量常数。

并发是**实际 Qdrant 响应返回后，主线程尚未 post 复验时，独立 Python 线程/
SQLite 连接提交变更，join 后再继续读取**。这是可重现受控交错，不是 OS 进程、
worker kill、持续压力或所有窗口原子化。join 最多 5 秒，错误/超时不验收。
ACK 丢失仍是故意遗漏 ACK 与新 worker 模拟，不杀 OS 进程。

## 最终状态

| 模式 | fresh audit 本地/远端 current | delivered / superseded | 故障期间 dead | 最终待处理 |
| --- | --- | --- | --- | --- |
| legacy | 25 / 25，一致 | 40 / 37 | 3 | pending/processing/waiting/dead 均 0 |
| bm25 | 25 / 25，一致 | 40 / 37 | 3 | pending/processing/waiting/dead 均 0 |

25 包括活跃/墓碑对应点，不等于 25 条可召回事实；audit 只证明当次观察一致。
死信仍显式 retry_dead，不因恢复就隐式重投；旧 mutation 不覆盖关闭的高版本墓碑。

## 验证与下一步

新增 [`test_memory_qdrant_optimized.py`](../../tests/test_memory_qdrant_optimized.py) **18 项**：
两假远端/真实 SQLite 旅程、实例 hook 恢复、线程错误/超时、plan、部分报告/清理/
停启预算；故意禁用 post-cache 复验或忽略缺索引时，矩阵必须失败，防止仅看相同
关键词答案掩盖语义降级退化。Fake 测试不冒充上述真实故障证据。
扩大定向 **919 passed**（两条既有 Starlette/httpx、anyio 警告，非全仓），
Ruff/diff/旧词项冻结通过。冻结的历史 memory.py SHA 提示非本轮新增。
本轮不测速度收益、云账单、盲测、ANN、集群/磁盘故障、生产授权或 SLA。
当前 **4/5**，下一步旧形状控制与优化实现同期性能配对，再决定默认。
