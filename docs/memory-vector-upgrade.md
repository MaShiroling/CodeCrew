# 第二阶段：外部向量存储升级（7/7）

2026-10-11，第 1–4 子步骤完成：存储/编排契约、SQLite 参考、Qdrant 本机存储、
[outbox 可靠同步](memory-vector-outbox.md)。迁移 27 捕获事务内事件，显式 worker
复用缓存、有限重试与租约接管，远端版本/墓碑拒绝延迟旧写入；真实合成旅程通过。
第 4 步[历史回填、断点审计与非破坏式重建](memory-vector-maintenance.md)也已完成：
迁移 28、分批 checkpoint、复用缓存、高版本 outbox 修复与新 namespace 重建。
2026-10-12 第 5 步[可选 Qdrant 混合召回](memory-qdrant-recall.md)也已接线：规范
版本/hash 服务端过滤、沿用 RRF、规范复验/降级。没有调用真实 embedding/Agent、
迁移真实聊天库或切换生产默认。第 6 步[故障与恢复验收](memory-qdrant-faults.md)
随后完成：专属容器六次停启、积压恢复/乱序/生命周期与 fresh audit。第 7 步
[配对质量/延迟/规模](memory-qdrant-benchmark.md)也已完成，当前 **7/7**：24 题两
混合来源一致、四档规模真实服务对照；Qdrant 无额外质量或应用速度收益，保留 SQLite 默认。
自动后台 worker 未启动，阶段完成也不等于生产 SLA、ANN 或独立盲测通过。
第一阶段 [FTS5/BM25 升级](memory-fts-upgrade.md)已完成 6/6；本页另起阶段计数。

2026-10-12 后续另开 [Qdrant 读路径优化](memory-qdrant-optimization.md)，当前 **5/5**：
分段测量与批量规范读取完成，200 候选前后快照 SELECT 从 1204 降到 10，校验保留；
重复元数据读取合并后非空 HTTP 从 5 GET+1 POST 降为 2+1，向量前/查询前仍独立检查。
扩展故障回归已完成：两关键词共 46 组、实际 14 对停启，最终各 audit 25/25 一致。
六臂同期性能配对已完成，来源/完整排名一致；小候选延迟改善，200/200 尾延迟
未稳定改善，Qdrant 仍无相对 SQLite 质量/速度优势，默认不变。
[实测与边界](../evals/baselines/memory-qdrant-optimization-ab-v1-2026-10-12.md)保留全部长尾。
不更改本页 7/7 或历史基准，也未启动后台 worker。

后续 [后台同步与运行可观测性](memory-vector-worker.md)另起 **5/5**：契约/watch/诊断已实现，
隔离真实进程恢复 7 组及 legacy/BM25 自动写入→缓存→worker→召回各 7 组通过。
写入使用确定性 Agent 提案/动作，不代表真实模型/回复派发验收；仅测试库运行 child，
用户真实库未迁移/worker 未启用，未安装系统服务。
其编号不替代本页 7/7 或读路径优化 5/5。

## 七个子步骤

| 子步骤 | 状态 | 验收目标 |
| --- | --- | --- |
| 1. 定义接口和边界 | 已完成 | 显式范围与来源锚点、SQLite 参考实现、契约测试、旧基线不变 |
| 2. 接入向量数据库 | 已完成；离线与真实存储探针通过 | [Qdrant 接入及操作](memory-qdrant-storage.md) |
| 3. 可靠同步 | 已完成；离线和真实合成旅程通过 | [outbox、重试/租约与版本/墓碑](memory-vector-outbox.md) |
| 4. 历史回填和重建 | 已完成；离线和真实合成旅程通过 | [分批/断点、缓存复用、模型目标隔离、双向审计/修复与新空间重建](memory-vector-maintenance.md)；不含自动模型热切换 |
| 5. 接入混合召回 | 已完成；离线和真实合成上下文通过 | [scoped 版本/hash 查询、RRF、规范复验/降级](memory-qdrant-recall.md)，旧后端仍默认 |
| 6. 故障与边界验收 | 已完成；离线及专属真实停服旅程通过 | [实际服务恢复、死信重投、租约/乱序与生命周期矩阵](memory-qdrant-faults.md)；模拟 ACK 丢失不等于 OS kill |
| 7. 性能/质量对照 | 已完成；缓存真实质量与合成规模分开留档 | [SQLite/Qdrant 三臂、24 已见题与四档 exact 规模](memory-qdrant-benchmark.md)；默认不变 |

第 1 步未选择服务；第 2 步用户确认 **本机 Docker + Qdrant**。首次安装授权尝试失败后，
用户手动安装并启动 Docker，获授权启动 Qdrant 1.19.0；真实探针及自有集合清理通过。
服务仅监听 loopback，数据卷保留；没有开启云资源或切换聊天召回。

## 两层接口，不把 embedding 当作存储

[`vector_contracts.py`](../app/chat/vector_contracts.py)定义：

- `MemoryVectorIndexPort`：保留现有 `index(memories)` /
  `search(query, memories)` 调用形状。ChatMemoryStore/StandaloneChatStore 的
  依赖注解由具体实现改为这个协议；运行时仍采用原 MemoryVectorIndex。
- `MemoryVectorBackend`：`upsert(records) -> accepted IDs`、
  `delete(room_id, memory_ids)`、`search(VectorSearch) -> VectorHit[]`。
  只接收已算出的向量和元数据；不能调用 embedding、生成文本或修改规范事实。
- `VectorModel`：模型指纹、2–4096 维、cosine 距离；同指纹换维度必须失败，换模型
  或修订号须换指纹。不把不同模型混合排序。
- `VectorAnchor`：记忆 UUID、房间 UUID、来源消息 UUID、来源讨论 UUID、规范原文
  SHA-256、active 状态。来自规范读取，不由模型推测；哈希不是事件顺序版本。
- `VectorRecord`：锚点、模型、有限 float32 可表示的不可变向量。
- `VectorSearch`：房间/讨论排除、模型/维度、查询向量、最多 200 个去重候选锚点、
  阈值和排名上限。混合 RRF 默认要保留完整 200 候选排名，不先裁成最终 3 条。
- `VectorHit`：来源锚点、模型和 [-1,1] cosine 相似度；不返回事实原文。

查询构造阶段拒绝跨房间候选、当前讨论候选、重复 ID、错误维度、非有限向量和
越界参数。候选必须不可变，避免适配器处理过程中被修改。参数错误为
ValueError/TypeError；存储失败为安全类别 VectorBackendError。成功空结果与
后端不可用不能混淆，错误不得含密钥、URL 或 provider 原始响应。

这些是应用内受信接口，不是额外的用户权限认证层。房间 UUID 过滤也不等于
完整多租户授权；外部讨论禁止读取本地记忆仍由 dispatcher 的守卫负责。

## 查询和一致性边界

1. 规范 SQLite 选择同房间 active、排除当前讨论的最多 200 个候选，敏感内容和
   查询的云调用保护继续在编排层负责。
2. 适配器在排名/limit **之前**应用房间、状态、讨论、模型/维度及候选锚点过滤。
   外部适配器不支持这些过滤时必须拒绝，不能检索全库再静默扩大范围。
3. 返回 cosine 降序、UUID 字符串升序的有序候选；随后与关键词按现有 RRF=60 融合。
4. 返回最终记忆前，编排层仍必须再次读取 SQLite，剔除被撤销、替换、完成/取消或
   内容变化的记录。不能用向量库 payload 充当事实。复验后发生的并发变化不在原子保证内。

本阶段仍限定这 200 个规范候选，不承诺外部 ANN 自动提高大语料召回；以后若扩大
候选窗口，需显式修改契约并重新对照。对服务故障的关键词降级在既有编排中已有，
新外部后端接线/降级现由第 5 步及真实合成探针验证，不把协议或探针当作生产可靠性证明。

## SQLite 精确参考适配器

[`SQLiteMemoryVectorBackend`](../app/chat/sqlite_vector_backend.py)使用现有迁移 24 的
embedding/meta 表，复用精确余弦计算；不改表结构，不加载扩展，不需要新依赖。
它是第 1 步可执行契约参考，其精确 search 仍不是 CLI 检索默认；第 5 步 Qdrant
编排复用其 upsert 写本地缓存。原 MemoryVectorIndex 不改，仍为已测的默认对照；
可选后端接线不等于默认上线或质量收益。

- 写：每批最多 32 个不同 ID，规范 active/来源/内容锚点在 SQLite 写事务中校验，
  跳过不匹配记录；元数据维度冲突使整批回滚；相同记录重复写幂等。
- 删除：必须提供房间，最多 200 IDs；只删除该房间的派生向量，不修改事实。
  重复删除安全；元数据模型维度锚点仍保留。
- 查：同一读快照联结规范记忆/来源，先限定范围再比对内容锚点及 BLOB；非法
  BLOB、非有限向量、过期记录不返回，剩余记录精确排序后取 limit。
  沿用原实现零模长时得分 -1 的行为，浮点舍入越界夹到 [-1,1]。
- 现有表每个 memory_id 只保存一个模型版本向量，不支持同 ID 多版本并存。
  外部后端须保持这一逻辑，模型集合切换/回填与删除全部版本策略在后续步骤设计。

外部纯存储服务不能自行得知规范 SQLite 的并发变化：调用方必须先验证写入锚点，
最终读取必须规范复验。只有本地参考适配器能在同库事务内额外拒绝过期写。
第 1 步当时没有可靠投递或乱序保护；第 3 步现已增加独立的版本化投递端口与
outbox、单调版本/墓碑，防止旧 upsert 在删除后重放。旧存储端口本身仍不提供版本栅栏，
且整个方案**不是双库原子提交**；规范复验继续是第 5 步必要边界。

## 验证与证据

新增 [`test_vector_backend_contract.py`](../tests/test_vector_backend_contract.py)
**36 项**通过，覆盖范围/来源锚点、内容及派生索引漂移、替换后不能复活、维度冲突
原子回滚、幂等写/删、过滤先于 limit、确定性同分排序、与原精确实现的分数/顺序一致、
非法契约、存储失败与成功空结果区分；测试禁止 HTTP 调用，仅使用临时数据库。
扩大记忆/评测/聊天室/讨论控制/CLI 定向套件 **475 passed**，两条既有依赖警告；
Ruff 与冻结基线通过，不是全仓验收。

使用第一阶段真实缓存无网复跑 24 题四分支：
`evals/results/memory-fts-ab-v1/20261011T114437Z-1c0e803c/`。
两关键词 10/12、两混合 12/12，有序来源与第一阶段采集轮完全一致，embedding/Agent
调用 0/0。这只保护当时既有编排行为，不把该复跑当作后续新外部库的成绩。
复跑与测试并行执行，延迟不作为新的性能结论。

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_vector_backend_contract.py
.venv/bin/python -m evals.check_memory_fts_baseline
```

第 2/7 的 [Qdrant 适配代码、离线测试和真实联调](memory-qdrant-storage.md)现已完成。
[真实验收](../evals/baselines/memory-qdrant-storage-v1-2026-10-11.md)四组检查通过，
测试集合已清理，0 embedding/Agent；本轮复跑存储契约 **72 passed**。
第 3/7 的[可靠同步实现与验收](memory-vector-outbox.md)也已完成：新增 65 项，扩大
定向 576 passed；真实合成旅程含崩溃后重试/延迟旧写入，0 模型调用且测试集合已清理。
worker 需显式执行，尚未由 chat-serve 启动。第 4/7 历史维护也已完成，新增 66 项，
扩大定向 642 passed；真实合成回填/审计/修复/重建通过，0 模型且两个自有集合已清理。
第 5/7 可选混合召回现已完成，新增 58 项、扩大定向 700 passed；真实合成上下文
探针通过，0 真实模型且自有集合已清理。第 6/7 故障/恢复矩阵随后完成，新增 38 项、
扩大定向 738 passed；专属容器六次停启、九组检查与最终 audit 通过，0 模型且自有
容器已清理（隔离证据保留）。第 7/7 随后完成，新增 45 项、扩大定向 783 passed；
真实缓存 24 题两混合有序来源一致、四档规模 audit/对照通过，0 新模型调用且自有
容器已清理。默认仍 SQLite/旧词项；后续 profiling 优化、ANN/扩大候选需另设实验。
