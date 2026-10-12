# Qdrant 读路径优化：5/5，同期配对验收完成

这是外部向量存储第二阶段 7/7 之后的新阶段，不修改之前的基准成绩。
默认仍为 SQLite/旧词项；Qdrant/BM25 保持可选。第 1 步只增加 eval 诊断；
第 2 步已经批量读取规范数据，没有删减校验、改变阈值/候选上限或缓存远端元数据。
第 3 步合并单次操作内的重复元数据读取，保留查询向量前/查询前两次独立新鲜检查；
不做跨请求缓存、TTL 或 ready 状态复用。
第 4 步已在 legacy/BM25 两条路径完成扩展回归；本步只增验收工具和证据，没有再改业务逻辑。
第 5 步六臂同期 A/B 已完成：来源/排名一致、SQL/HTTP 下降；200/200 档尾延迟
未稳定改善。保留 SQLite 默认，[完整实测与取舍](../evals/baselines/memory-qdrant-optimization-ab-v1-2026-10-12.md)。

## 五个子步骤

| 子步骤 | 状态 | 交付目标 |
| --- | --- | --- |
| 1. 分段测量瓶颈 | 已完成；离线测试和隔离真实测量通过 | 规范前后快照、SQL/HTTP 次数、查询向量、BM25、复验及诊断开销 |
| 2. 批量读取规范数据 | 已完成；离线及隔离真实复跑通过 | 每快照最多 5 次 SELECT，来源/版本/hash/缓存守卫和前后快照保留 |
| 3. 减少重复远端检查 | 已完成；离线边界与隔离真实复跑通过 | 非空召回 2 GET+1 POST；向量生成前/查询前独立检查，无跨请求缓存 |
| 4. 正确性与故障回归 | 已完成；两条关键词路径与隔离真实故障矩阵通过 | 46 组检查、14 对实际停启，查询后独立线程修改、索引缺失及双向审计 |
| 5. 重新配对测量 | 已完成；六臂同期真实 A/B、守卫次数与定向回归通过 | 相同输入/points 逐条×批量、重复×当前检查；均值/尾延迟/不确定性分报，默认不变 |

## 第 5 步：同期配对与默认决策

[`benchmark_memory_qdrant_optimization.py`](../evals/benchmark_memory_qdrant_optimization.py)
复用同一 fixture/service，四个 Qdrant 组合外加 BM25/SQLite，共六臂。
保留的 rowwise/repeated-read 控制使用当前共用校验器，不是历史二进制；仅 eval
可选，业务无入口。预热 3 次、测量 24 次，循环移位/反转平衡位置，原始按轮配对。
正式测速不加 Capture；完整语义排名与计数诊断单独采集，不算入正式时长。
默认只打印计划，`--live --cache` 使用独立自有容器/本地缓存；安全边界同下文。

[实际证据](../evals/baselines/memory-qdrant-optimization-ab-v1-2026-10-12.md)：
24 已见 gold-seeded 题与四档合成 exact 工作负载，全部对照有序来源/完整排名
一致；200 候选快照 SELECT=1204→10、非空 HTTP=5+1→2+1。
非空题旧形状→当前均值节省 2.933 ms，簇 CI95 [2.080,4.004]，P95=18.778→12.968 ms；
200/200 均值差 CI 跨零，P95=126.140→139.669 ms，不能宣称稳定尾延迟收益。
200/1000 当前 P95=90.615 ms，SQLite=26.474 ms；无召回增量，保持 SQLite 默认。
各档只 3 个合成查询簇，CI 描述性、不等于生产 SLA。所有原始长尾保留，未挑选复跑。
新增 24 项测试，扩大定向 943 passed（两条既有警告），0 模型，自有容器清理通过。
5/5 是本轮范围完成，不是自动 worker/多租户授权/盲测/ANN/并发 SLA 已验收。

## 诊断如何工作

[`profile_memory_qdrant.py`](../evals/profile_memory_qdrant.py)复用上轮真实 SQLite/FTS/
outbox/审计 fixture。原三臂之外增加 `qdrant_profiled`，逐题/轮交替反转四臂顺序。
只对隔离 fixture 的实例临时包裹方法，退出或异常后恢复原有实例覆盖/类绑定方法；
不修改类、业务源文件、真实库或服务。只适用于单线程 eval，不是线上遥测设施。

各次 profiled retrieve 收集原始样本，嵌套计时分为：

- `candidate_snapshot`：最初候选快照/连接；独占计时不重复算内部 BM25。
- `bm25_search`：关键词检索。
- `canonical_pre_snapshot` / `canonical_post_snapshot`：远端查询前后规范/缓存快照。
- `cached_query_vector`：缓存或合成查询向量；不是云 embedding 时间。
- `remote_ready_guard` / `remote_search_local`：本地远端检查/搜索编排，独占计时剔除其 HTTP 子阶段。
- `http_metadata` / `http_query`：HTTP 客户端完整调用。
- `final_canonical_revalidation`：融合后的规范复验。
- `index_orchestration` / `retrieve_other`：其余编排及未包裹工作，避免把残余硬归因给 SQL。

`exclusive_ms` 的和逐次等于根 retrieve 时长；`inclusive_ms` 用来观察包含子阶段的
总开销，**不能相加**。报告中的独占占比以各阶段样本总耗时/根总耗时计算；
不同阶段的 p95 不能相加成总 p95。计时/trace 开销不会凭空消失，因此另报无诊断
与带诊断的延迟、均值比，验证同一输入的有序来源一致，否则实验失败。

SQL trace 只保留当前阶段的 SELECT/BEGIN/COMMIT/ROLLBACK/OTHER 次数，原始 SQL、
参数、事实、向量、URL/密钥不进诊断样本。trace 装在连接打开之后，所以不计打开
连接时的三条 PRAGMA；FTS 内部带注释语句落 OTHER。**次数不等于 SQL 执行耗时**：
规范快照同时含 SQLite、JSON/BLOB 解码、哈希、1024-D 向量与契约校验等 Python 工作。

HTTP 只允许已知 collection GET 和 points/query POST 的形状；不保存路径/body/响应。
本项目 `_request` 包含序列化、网络、服务端处理、响应读取/解码。Qdrant 官方
[query API](https://api.qdrant.tech/api-reference/search/query-points)的服务端 `time`
与本工具测量的客户端 wall time 不是同一指标；当前不采集服务端 `time`、不声称裸引擎耗时。
本轮用 context7-mcp 核对查询接口，实际服务版本固定 Qdrant 1.19.0。

## 运行及安全边界

```bash
.venv/bin/python -m evals.profile_memory_qdrant  # 只打印计划
.venv/bin/python -m evals.profile_memory_qdrant --live --cache \
  /Users/mashiro/CodeCrew/evals/results/memory-fts-ab-v1/20261011T113351Z-8f218a82/embedding-cache.json
```

预热 3 次、测量 20 次；24 已见且人工审核题只测 gold-seeded 检索，缓存复用百炼
text-embedding-v4/1024-D；四档 `(10,10)`、`(50,50)`、`(200,200)`、`(200,1000)`
仅是合成规模。exact、200 候选、limit=3、threshold=0.65、RRF=60 不变。
不是新盲测、模型提炼/答案评测、全局 ANN、大库收益或并发 SLA。

授权后使用已存在的本机镜像创建独立自有 label/UUID 临时容器，随机 loopback 端口；
归属保护复用 [故障矩阵](memory-qdrant-faults.md)。不读 Settings/`.env`，不下载镜像，
不操作现有 6333 或真实聊天库。最终只删除自有容器，保留结果目录下 JSON/SQLite/bind。
审核/冻结/缓存身份和 checksum 必须通过；不隐式调用云/Agent，缺缓存硬失败。
全部测量及归属安全清理成功且 provider calls/errors 都为零才 `complete=true`。
任何吞掉的 FTS/向量故障由实验观察器识别，失败不是记忆漏召回。

manifest 保存环境、参数、数据/审核锁/缓存与相关代码 SHA；原始 phase 样本存入
quality.json 和 scale-0..3.json，summary.json 报独占占比、计数及诊断开销。
默认、规范事实前后不变，旧 benchmark 运行默认仍为三臂，新增回调仅供 eval 诊断复用。

## 第 1 步历史结果

批量读取前的 [分段测量报告](../evals/baselines/memory-qdrant-profile-v1-2026-10-12.md)保留。

2026-10-12 实际测量已完成：200 候选/200 点合成档，前后规范快照独占占比 59.39%，
两快照各 602 次 SELECT；实际非空请求 5 元数据 GET + 1 查询 POST。诊断前后
来源一致；新增 29 项测试，扩大定向 812 passed（两条既有警告），无新增模型调用，
自有容器清理通过。当时剩余四步待开发；快照包含 SQL 和 Python 校验，不承诺批量 SQL
可消除全部耗时，也不把诊断开销与测量波动当加速收益。

## 第 2 步实现与结果

[`QdrantMemoryVectorIndex._snapshot`](../app/chat/qdrant_memory_index.py)保持原 `BEGIN`
读快照，在 meta/目标检查之后一次取 revision/规范锚点，按输入顺序先做范围及
内容检查，再调用 [`desired_many_in_snapshot`](../app/chat/vector_outbox.py)：

1. 最多 200 个去重 UUID，绑定参数 `IN` 批量取规范记忆、来源、成员与 embedding 缓存。
2. 最多 200 个相关讨论，`MIN(sequence) GROUP BY correlation_id` 取全局最早根消息。
3. 与单条 outbox 投递/维护共用无 SQL 的 `_desired_from_rows` 校验；逐条生成不可变 mutation。

根消息不先按 room 筛选，否则可能隐藏跨房间/外部根消息；之后仍按原逻辑拒绝。
SQL 返回顺序不作排名依据；UUID 映射后恢复候选输入顺序，最终 cosine/UUID 排名、
RRF=60、最终规范复验不变。前/后快照不合并，跨网络期间发生的源/缓存/生命周期
变化仍可剔除。共用 `_snapshot` 的本地缓存 indexing 前后校验也批量化，但仍不会
查询时补算缓存、初始化远端、注册目标、写 outbox 或调用模型。

完整有效候选时，每次 cached 快照为 meta/target/revision/canonical/root **5 次 SELECT**，
200 候选前后合计 **10 次**，原为 602+602=1204；过滤/短路时可更少。这只统计两次
快照，不包括初始候选、FTS、最终复验或连接 PRAGMA，也不是数据库内部工作量常数。
仍有 `O(N × dimensions)` 的两次向量解码/校验；批量根查询可能扫描相关历史，本步
未加索引/迁移，长历史、峰值内存和并发需以后单独测量，不声称消除全部瓶颈。

本轮 context7-mcp 未给出充分的 SQLite 查询细节，回到
[SQLite 官方参数限制](https://www.sqlite.org/limits.html)核对并用本机 SQLite 3.53.4
验证。每条 SQL 最多 200 个绑定参数；测试将连接变量上限设为 200 验证边界。
不拼接 ID 原文，仅拼固定数量的 `?`；部署进一步降低参数限制时仍失败降级。

保留 eval-only [`rowwise_snapshot`](../evals/qdrant_snapshot_reference.py)供等价性对照和
第 5 步同期配对。它使用当前共用校验器，**不是字节完全相同的历史部署**，无 app 配置入口。
本轮只做记录/顺序对照，未将它当成已完成的真实性能 A/B。

[批量读取实际验收](../evals/baselines/memory-qdrant-batch-v1-2026-10-12.md)复跑 24 已见
缓存题和四档合成 Qdrant；来源、完整向量排名一致，实际前后 SELECT=5+5，元数据
HTTP 仍 5 GET+1 POST。新增 **37 项**，扩大定向 **849 passed**（两条既有警告），
旧冻结/Ruff/diff 通过，0 新 embedding/Agent，自有容器清理完成，真实服务/库/配置未动。
profiling 清单新增 `read_path_revision=codecrew-canonical-batch-v1`，明确当前实现已批量化；
工具本身不修改默认或应用代码。历史清单不覆盖。

第 2 步完成时 **2/5**。之后减少重复元数据检查、完整故障回归与同期性能配对；
本轮与历史非同期延迟差不能包装成确定收益或切换默认的依据。

## 第 3 步实现与边界

[实际验收报告](../evals/baselines/memory-qdrant-readiness-v2-2026-10-12.md)。
context7-mcp 核对了 Qdrant 官方 [Get collection](https://api.qdrant.tech/api-reference/collections/get-collection)
接口；本机固定 Qdrant 1.19.0，以一份响应完成配置/索引兼容检查，不缓存跨操作结果。

- 基础 `_require_ready` 仍读取新鲜 `_collection`，检查维度/Cosine、payload 类型和必需索引，
  现在返回已验过的 schema；版本化 `_require_sync_ready` 从该份 schema 检查 `sync_revision=integer`，不再 GET 第二遍。
- 基础 `_search` 只调用一次操作专用 `_require_search_ready`。普通后端检查基础 schema；
  版本化后端检查基础 schema+版本索引，`search_current` 不再先额外检查然后让底层重复检查。
- `QdrantMemoryVectorIndex` 的 **embedding 前检查保留**。向量生成后，底层搜索仍独立
  发一次 GET 重新确认兼容性，再 POST；不传 ready token、绕过开关或共享“已检查”状态。
- 默认后端、SQL 5+5、规范版本/hash/来源/讨论过滤、exact/RRF/最终复验都不变。
  版本化普通 `search` 也必须检查版本索引；普通未版本化后端仍无需它。初始化的基础
  schema 检查不提前要求版本索引，以便显式 initializer 能按原流程建立该索引。

一次非空 recall 从 **5 metadata GET + 1 query POST** 变为 **2 GET + 1 POST**；
独立版本化 `search_current` 是 1 GET+1 POST。写入/审计等原本调用 `_require_sync_ready`
的单次检查也从两次读取变为一次；条件写、墓碑、ACK 验证和 worker 重试策略未改。
每个新请求重新读取元数据：无失效 TTL、持久 ready 缓存、自动重建、网络重试或降级后偷偷语义补齐。

第一道失败不生成查询向量，第二道失败不发查询 POST，查询 HTTP 失败仍安全降级。
两道之间删除集合、改维度/距离/索引会被第二道发现；下一请求可恢复。
**这不是远端原子快照**：第二次 GET 到 POST 之间仍有变更窗口；查询错误、严格结果
payload/fence 校验和 SQLite post/final 复验仍负责保守处理，不能宣称缓存解决了所有并发竞态。

保留 eval-only [`RepeatedReadinessBackend`](../evals/qdrant_readiness_reference.py)作为第 5 步
重复检查控制，与 rowwise 控制组合可复现原 SQL/HTTP 形状；无 app 配置入口。
它共用当前请求/结果校验，不是字节完全相同的历史代码，本轮只离线验证同命中/请求和 5 GET 形状，尚未同期性能 A/B。

新增 **52 项**，扩大定向 **901 passed**（两条既有警告）；24 缓存已见题与四档合成
真实复跑来源/完整排名一致，HTTP 实际 2+1、SQL 5+5，0 新模型调用，自有容器已清理。
manifest/summary 增加 `remote_readiness_revision=codecrew-qdrant-readiness-v2`，历史成绩不覆盖。
第 3 步完成时 **3/5**；后续做完整正确性/故障回归与同期性能配对，默认配置仍不变。

## 第 4 步验收与复跑

[`check_memory_qdrant_optimized.py`](../evals/check_memory_qdrant_optimized.py)复用原九组
恢复旅程，增加查询边界，依次验证 **legacy/BM25**；每条 23 组，共 46 组。
两个独立 SQLite 和 collection 位于同一个自有临时 Docker，全部为合成数据/3-D 向量。

```bash
.venv/bin/python -m evals.check_memory_qdrant_optimized  # 只打印计划
# 获授权后，仅停启自有容器、删除/重建自有测试索引：
.venv/bin/python -m evals.check_memory_qdrant_optimized --live
```

- 原九组：停服便宜降级、有限重试/死信、SQLite 新实例和远端持久恢复、显式重投/
  乱序墓碑、模拟 ACK 丢失/租约接管、缺缓存继续、五种生命周期停服/恢复与最终 audit。
- 健康读跨房间/讨论过滤，HTTP=2 GET+1 POST、SQL=5+5。
- 实际 query 响应返回后、post 快照之前，独立线程/SQLite 连接提交撤销/替换/完成/
  取消/物理删/缓存更新/外部标记；旧语义命中必须剔除。join 最多 5 秒，失败/超时不验收。
- room_id/sync_revision 索引分别在向量前/两道之间缺失；前道不生成向量，后道不查询，
  保留关键词，测试显式重建后新请求恢复。
- 两道 metadata 检查间实际 stop/start，无 query POST，恢复后仍 2+1。
- 扩展后 fresh 双向 audit 和无积压。原六次停启+中途一次，每条七次，总 14 对，
  启动 15 次含首次；不碰现有 6333。

context7-mcp 核对官方 [Delete field index](https://api.qdrant.tech/api-reference/indexes/delete-field-index)，
`wait=true` 等待完成再读取。删除前复验自有归属、固定字段名；仅合成夹具故障注入，
不增加生产删除/自动修复接口。临时实例观察器退出恢复，不保存 SQL/URL/body。
报告逐模式 checkpoint，失败只存类别/部分检查，完整旅程/停启预算/安全清理都通过才 complete。

[实际报告](../evals/baselines/memory-qdrant-optimized-regression-v1-2026-10-12.md)：两条各
23/23，audit 各 **25/25**，outbox 各 40 delivered/37 superseded，无 pending/processing/
waiting/dead；自有容器已清理、证据数据保留，0 新模型调用。新增 **18 项**，扩大定向
**919 passed**（两条既有警告），Ruff/diff/旧冻结通过，业务代码与共有 app SHA 不变。

当前 **4/5**，下一步旧形状控制与当前实现的**同期性能配对**。本轮不宣称速度收益、
生产 SLA、OS worker kill、集群恢复/磁盘损坏/持续负载或所有并发窗口原子化。
