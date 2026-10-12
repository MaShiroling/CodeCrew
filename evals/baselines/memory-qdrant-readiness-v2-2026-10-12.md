# Qdrant 元数据检查合并 v2：读路径优化第 3/5 步

2026-10-12 完成，[实现/边界与路线](../../docs/memory-qdrant-optimization.md)。
batch 快照版本 `codecrew-canonical-batch-v1` 不变，新增远端检查版本
`codecrew-qdrant-readiness-v2`。只合并同一操作重复读取；没有跨请求缓存或删安全检查。

## 真实证据与输入

原始 gitignored 目录：`evals/results/memory-qdrant-profile-v1/20261012T021809Z-b3ad99d7/`。
manifest、quality、scale-0..3、summary JSON 和独立 SQLite/bind 数据保留。
`complete/measurements_complete/cleanup_complete=true`，embedding calls/errors=0、Agent=0。
自有容器 `codecrew-memory-fault-be0b6dbbd93e4de7a21c66832b6bc68a` 已安全清理，证据不删。
现有 6333 只读检查可访问、集合列表仍为空；真实库/私有配置/服务/默认不动。

环境：macOS 27 arm64、Python 3.11.16、SQLite 3.53.4、Qdrant 1.19.0；image ID
`sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`。
输入复用 24 已见人审 gold-seeded 回归题，百炼 text-embedding-v4/pilot-2026-10/1024-D
缓存；四档为合成规模，每档三查询。预热 3、测量 20、四臂交替反转。
exact、最多 200 规范候选、最终三条、threshold=0.65、RRF=60 不变。
非盲测、非云模型/Agent 答案评测、非 ANN/生产 SLA；0 新调用不等于线上无成本。

- dataset SHA：`0155a7c8c724ab2ebd391a16fe0f163b13eaf79d56df282d6713d653c3187c2c`
- label lock SHA：`b74e8ce1cc231c33dac0b561c1b084c560e457be43d4d618e16caa6d0b988782`
- cache SHA：`f4e2e6619980bf14f567b2658491bef59fb780f065fa9800ccb975c5dd85020e`
- 基础 REST 后端 SHA：`448fa1bebc0aefba55b037881be232c7b3a435a22aef230303a6fa4eb6abc378`
- 版本化后端 SHA：`263be0c5836cb08107d457a3bf41667c755baa77d28238c5e6e4924f4b852034`
- profiler SHA：`bdcd003bb368befef2c08073575550af49ece8e409ea1ac365fd7c438c4f6782`

manifest 保存 16 个相关运行文件 SHA，交付前再次核对匹配。eval-only 重复检查控制
SHA=`6c225e60bd1aa4004346dae1f98d94e8f2da84d11d9617ded678d47928e1cd63`，本轮只离线
对照命中/请求形状，未参与真实性能计时；不覆盖任何历史成绩。

## 检查与次数

基础 `_require_ready` 从一个新鲜 collection 响应检查维度/Cosine 和全部必需 payload
索引/类型，再返回 schema。版本化 `_require_sync_ready` 直接在这份 schema 检查
`sync_revision` 整数索引，不取第二份响应。缺失/畸形版本索引给出安全类型错误。
搜索使用私有 `_require_search_ready` 操作钩子：普通后端保持基础检查，版本化后端
同时检查版本索引；`search_current` 不再在入口和底层各做一遍 readiness。

编排的 embedding 前检查不变；生成向量后、底层查询前仍独立读取元数据。每次新
请求都重新检查，故障后不沿用“曾经 ready”，恢复后也不被失败缓存卡住。
未添加 ready token、TTL、持久状态、自动初始化/修复、重试或语义兜底。

| 路径 | 之前 metadata GET | 当前 metadata GET | 当前 query POST |
| --- | --- | --- | --- |
| 非空完整召回 | 5 | 2（向量前、查询前各一） | 1 |
| 独立 `search_current` | 3 | 1 | 1 |
| 空候选/不可信候选短路 | 0 | 0 | 0 |

整个非空召回远端 HTTP 由 6→3，**请求数减少不等于整轮聊天耗时减半**。
第一道失败不生成查询向量；第二道失败（此时已有向量）不查询；POST 失败不重试。
写入/审计等共享 readiness 也一次完整读取，条件写、版本墓碑、ACK/worker 语义不改。
初始化仍先建立基础 schema 再建版本索引，基础检查不会提前要求版本索引。

这不是 collection 元数据与 point 查询的原子事务。第二次 GET 到 POST 仍有窗口；
严格 payload/当前 revision/vector hash、SQLite post/final 复验与故障降级保留。
不宣称能够发现任何同配置重建或消除远端并发变化，也不把房间过滤当多租户认证。

## 质量与规模

BM25 来源 TP/FP/FN=10/0/2，两混合=12/0/0；24/24 有序来源和 full-vector 排名一致，
共同 cosine 最大差 `1.1700584967577754e-7`。诊断/无诊断结果一致。
每臂 480 测量，非空候选 14 题/280 次、空候选 10 题/200 次；后者零 HTTP/查询向量。
四档初始 audit 为 10/10、50/50、200/200、1000/1000 一致，投递完成，
无 pending/processing/waiting/dead；两规范快照仍 **5+5 SELECT**，实际非空 HTTP **2 GET+1 POST**。

下表是**当前已经优化的同一代码**有无诊断对照，不是优化前后性能 A/B；每合成档每臂 60 样本。

| 分组 | 无诊断 p50 / p95 ms | 带诊断 p50 / p95 ms | 带/不带诊断均值比 |
| --- | --- | --- | --- |
| 缓存质量非空（280） | 5.697 / 8.594 | 5.643 / 8.496 | 0.982 |
| 空候选（200） | 0.493 / 0.620 | 0.506 / 0.654 | 1.043 |
| 10 / 10 | 8.071 / 10.468 | 8.045 / 10.821 | 1.013 |
| 50 / 50 | 21.045 / 24.457 | 21.064 / 22.623 | 0.992 |
| 200 / 200 | 75.745 / 81.209 | 74.847 / 83.266 | 1.007 |
| 200 / 1000 | 76.662 / 84.071 | 75.806 / 83.787 | 0.992 |

200/200 同轮 SQLite hybrid 无诊断 p95=22.081 ms，Qdrant 仍无速度优势；默认不切换。
第 2 步该档 p95=89.130、本轮=81.209 ms，非同期环境/调度/HTTP/GC 波动未消除，
没有显著性/置信区间检验，因此不作为确定收益。独占计时可加总，inclusive 与阶段 p95 不可相加。
第 5 步才用 rowwise/repeated-read 控制做同期配对，历史记录保留。

## 验证与进度

新增 [`test_memory_qdrant_readiness.py`](../../tests/test_memory_qdrant_readiness.py) **52 项**，
使用真实 SQLite/REST 适配器、假 HTTP：两道检查前后顺序；九项必需索引在两道
分别缺失；维度/距离/payload 类型变化、畸形版本索引、集合删除；下一请求恢复、
embedding 错误、POST 连接/503 且不重试；独立/普通后端、空/非法查询及旧控制等价性。
不是实际停服或 OS kill 验收，第 4 步仍要针对新代码复跑完整故障矩阵。

扩大定向 **901 passed**（两条既有依赖警告，非全仓），Ruff/diff/旧词项冻结通过。
冻结提示的历史 memory.py SHA 变化非本轮新增；本轮未改规范/SQL批量/FTS/SQLite默认/
schema/配置。当前 **3/5**，下一步完整正确性与故障回归，再做同期性能配对。
