# Qdrant 批量规范读取 v1：第 2/5 步验收

2026-10-12 完成，[实现与路线](../../docs/memory-qdrant-optimization.md)。
版本 `codecrew-canonical-batch-v1`；减少逐候选 SQL，不减少安全校验。
SQLite/旧词项默认保留，阈值/RRF/范围/远端 metadata HTTP 不变。

## 证据与输入

gitignored 原始目录：`evals/results/memory-qdrant-profile-v1/20261012T014620Z-f7c497b5/`。
manifest、quality、scale-0..3、summary JSON 和独立 SQLite/bind 数据保留。
`complete/measurements_complete/cleanup_complete=true`，`optimization_applied=true`
表示被测应用已批量化，不是诊断工具自行修改应用。新增 embedding/Agent **0/0**，
embedding errors=0。只清理自有容器 `codecrew-memory-fault-cf58f2b835a04e3aa032d80ed0460a8f`，
保留证据 bind/SQLite；现有 6333 read-only 检查可访问、集合列表仍为空。
未改真实库、私有配置/默认、启动用户服务或下载镜像。

macOS 27 arm64、Python 3.11.16、SQLite 3.53.4、Qdrant 1.19.0，image ID：
`sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`。
输入同 [第 1 步](memory-qdrant-profile-v1-2026-10-12.md)：24 已见、人审、gold-seeded
题；百炼 text-embedding-v4/pilot-2026-10/1024-D 缓存。四档为合成规模，每档三查询；
预热 3、测量 20、四臂顺序反转；exact、200 候选、最终三条、threshold=0.65、RRF=60。
不是盲测、云/Agent 回答评测、ANN 或生产 SLA。

- dataset SHA：`0155a7c8c724ab2ebd391a16fe0f163b13eaf79d56df282d6713d653c3187c2c`
- label lock SHA：`b74e8ce1cc231c33dac0b561c1b084c560e457be43d4d618e16caa6d0b988782`
- cache SHA：`f4e2e6619980bf14f567b2658491bef59fb780f065fa9800ccb975c5dd85020e`
- Qdrant index SHA：`5b1f2e31c37069e72f5b7b10b0465e48c6b3c56d2dda019b18e507c2bcf676f7`
- outbox SHA：`8a2a808e6656e470a7b8d0d415ddf653dd8c0abd2f7561b2c7a5e939b6479e72`
- profiler SHA：`6881dfda721c98c0ace30e333aaa9a61dbcf73cfce76d22903af654011929e9c`

manifest 16 个相关运行代码 SHA 再次核对匹配；eval-only rowwise 控制另存 SHA
`aa2c9bb40583e6c2fadd8d2a62cbb4eeddfa83c42c4bd0b24e5467d6a6ca096e`，只离线测试使用，
没有参与本轮真实计时。历史报告和原始文件不覆盖。

## SQL 与正确性

每次快照保持 `BEGIN`，取 meta/target/revision/规范缓存/全局最早根消息。
根查询 `MIN(sequence)` 保留原全局第一条语义，不先筛 room 而隐藏不可信根。
按 UUID 恢复候选输入顺序，过滤先于材料校验，后者与单条 outbox/维护共用纯函数。
Human/成员房间/外部根、敏感内容、模型/维度/hash、BLOB/有限性/非零向量和契约均保留。
HTTP 前后快照独立，RRF 后最终复验保留；不新增订阅、写入、embedding 或远端初始化。

| 合成档（候选/总点数） | 第 1 步前/后 SELECT | 本轮前/后 SELECT | 实际非空 HTTP |
| --- | --- | --- | --- |
| 10 / 10 | 32 + 32 | 5 + 5 | 5 GET + 1 POST |
| 50 / 50 | 152 + 152 | 5 + 5 | 5 GET + 1 POST |
| 200 / 200 | 602 + 602 | 5 + 5 | 5 GET + 1 POST |
| 200 / 1000 | 602 + 602 | 5 + 5 | 5 GET + 1 POST |

只算两次快照的应用层 SELECT，不含初始候选/FTS/最终复验和连接 PRAGMA；内部
扫描/聚合工作不因一次 SELECT 就变常数。200 候选两快照 SELECT 1204→10，减少
99.17%，**不是总检索时间减少 99.17%**。两次逐条 1024-D 解码/校验仍在，批量
材料同时驻留内存；长历史/峰值内存/并发需另测，本步不加索引/迁移。

24 条回归：BM25 来源 TP/FP/FN=10/0/2，两混合=12/0/0，24/24 有序来源及完整
向量排名一致，最大共同 cosine 差 `1.1700584967577754e-7`；诊断/无诊断来源一致。
每臂 480 样本；候选非空 14 题/280 次、空候选 10 题/200 次；空候选无 HTTP/查询向量。
四档初始 audit 分别 10/10、50/50、200/200、1000/1000，一致且投递完成，
无 pending/processing/waiting/dead；所有诊断前后来源与 full-vector 顺序一致。

## 延迟：诊断复跑，不是优化前后同期 A/B

下表两列均为**已经批量化**的代码，区分是否安装诊断计时。每个合成档每臂 60 样本。

| 分组 | 无诊断 p50 / p95 ms | 带诊断 p50 / p95 ms | 带/不带诊断均值比 |
| --- | --- | --- | --- |
| 质量非空（280） | 7.002 / 9.801 | 7.111 / 10.449 | 1.029 |
| 质量空候选（200） | 0.484 / 0.612 | 0.508 / 0.673 | 1.067 |
| 10 / 10 | 9.246 / 17.099 | 9.146 / 12.380 | 1.000 |
| 50 / 50 | 22.270 / 26.151 | 22.556 / 24.645 | 1.001 |
| 200 / 200 | 77.414 / 89.130 | 76.411 / 85.522 | 0.992 |
| 200 / 1000 | 81.035 / 92.750 | 77.288 / 88.542 | 0.982 |

200/200 诊断前后快照各 p50=21.865/22.262 ms，独占占比合计 58.02%，根均值
77.621 ms。阶段 p95 不能相加；SQL 大幅减少后仍有 Python 校验等工作，不能把
整个阶段当 SQL 时间。该档同轮 SQLite hybrid 无诊断 p95=22.322 ms，Qdrant 仍无速度优势。

第 1 步该档无诊断 p95=104.728 ms、本轮=89.130 ms，但非同期：调度、HTTP、GC
和诊断扰动未消除，也未检验显著性/置信区间。**不能宣称确定的加速比例**，第 5 步
才做 rowwise 控制/当前实现同期配对。零新模型调用也不等于线上记忆免费。

## 验证与下一步

新增 [`test_memory_qdrant_batch.py`](../../tests/test_memory_qdrant_batch.py) **37 项**：
1/32/200 候选、变量上限/SQL 数、反向输入、rowwise 等价性、scope/失效缓存、
来源/成员/根、损坏 JSON、墓碑/缺行/UUID、空/重复/超限、排除候选不解析损坏
证据，以及两个查询间实际独立 WAL writer 提交。原 outbox/维护/召回/故障继续通过。
扩大定向 **849 passed**（两条既有依赖警告，非全仓），Ruff/diff/旧词项冻结通过。
冻结仍提示历史 memory.py SHA 变化；本轮未改 memory.py、FTS、远端后端/守卫、
SQLite 默认向量实现、schema 或私有配置。

当前 **2/5**；下一步减少重复远端元数据检查，再做完整故障回归、同期性能配对。
自动 worker、生产多租户授权、独立盲测、ANN/扩大候选和并发 SLA 不在本步范围。
