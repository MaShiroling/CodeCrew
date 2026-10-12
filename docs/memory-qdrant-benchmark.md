# 第二阶段第 7/7 步：配对质量、延迟与规模对照

2026-10-12 完成，[真实结果与默认决策](../evals/baselines/memory-qdrant-benchmark-v1-2026-10-12.md)。
**本阶段 7/7 完成，但不等于记忆已生产就绪**：自动后台 worker、完整多租户授权、
ANN/扩大候选、并发负载和独立盲测仍不在当前验收中。默认 SQLite/旧词项保留，Qdrant/BM25 可选。

## 实验设计

新增 [`benchmark_memory_qdrant.py`](../evals/benchmark_memory_qdrant.py)，三臂保持相同
BM25、gold facts、房间/讨论范围、向量缓存、limit=3、threshold=0.65、RRF=60：

- `bm25`：真实 FTS5 关键词对照。
- `sqlite_hybrid`：原 MemoryVectorIndex 精确余弦 + RRF；查询时 backfill=0，提前缓存齐全。
- `qdrant_hybrid`：QdrantMemoryVectorIndex 版本化 scoped exact 搜索 + RRF + 规范/缓存复验。

质量部分重放用户已审核的 24 题和原百炼真实缓存，检查数据/审核锁/缓存 SHA、
完整性/模型/维度/provenance；cache miss 硬失败，不隐式采集、不改阈值/金标准。
固定记忆隔离检索，不调 Agent，不评价自动写入或最终答案。它是已见回归，不冒充新盲测。

规模部分使用独立模型指纹、固定 seed 的 1024-D 合成向量，四档 `(候选,总点数)`：
`(10,10)`、`(50,50)`、`(200,200)`、`(200,1000)`。后者包含其他房间干扰，单房间
从未超过 200；三个固定查询含词项命中、纯语义路径、成功空结果。不把它当模型效果数据。

预热 3 次、测量 20 次，逐题/轮反转臂顺序；原始 samples 保存。统计 p50 median、
p95 nearest-rank，质量结果按来源 TP/FP/FN、Recall/Precision/MRR@3 与空 gold 误召回。
单列候选为空/非空的延迟，不把短路请求混作实际向量性能。质量两混合结果一致；
Qdrant 应用接线更慢，因此没有默认切换依据。

## 有效性检查

计时仅 `retrieve` 调用，包含 SQLite/FTS/RRF/规范复验、缓存 lookup 和 Qdrant HTTP；
不包含云 query embedding、Agent、初始化/seed/缓存建立、outbox 投递、audit。
这是本机热检索阶段，不是整轮聊天 SLA，也不是裸存储适配器延迟。

捕获 FTS search 是否成功与 Scoped/普通向量异常，防止业务吞掉异常降级后冒充有效
hybrid。成功空结果是真实成绩，允许记录退化；重复查询结果不稳定或规范快照变化
则拒绝该实验。两个分支允许真正质量差异，不修改结果强求一致。

另做未计时的 SQLiteMemoryVectorBackend 与 Qdrant full-vector 排名对照：记录顺序、
遗漏 ID、共同命中最大 score 差、阈值邻近点。后端 cosine 的微小浮点差不是新语义增益。
质量库只准备查询房间缓存（历史其他房间不自动补算或宣称审计完成）；四档规模库
则真正投递全部点并完成双向 audit，outbox 未清空则失败，不以 scan_complete 代替一致。

官方精确搜索语义通过 context7-mcp 核对 [Qdrant 1.19.0 exact 说明](https://github.com/qdrant/qdrant/blob/v1.19.0/lib/edge/tools/shard_query/SKILL.md)
及本项目实际请求；`exact=true` 穷尽搜索，不声称 ANN。当前性能包含额外规范守卫和
元数据 HTTP，不能简单归因于向量引擎速度。没有做统计显著性或冷缓存/并发负载。

## 运行方法与安全边界

```bash
.venv/bin/python -m evals.benchmark_memory_qdrant  # 只打印计划，无 Docker/模型/配置读取
.venv/bin/python -m evals.benchmark_memory_qdrant --live --cache \
  /Users/mashiro/CodeCrew/evals/results/memory-fts-ab-v1/20261011T113351Z-8f218a82/embedding-cache.json
```

需已有本机 Docker 和 Qdrant 1.19.0 镜像；缓存被 gitignore，在其他机器不存在时必须
先获得完整合规缓存，工具不会自动付费采集。默认 repeats=20、warmups=3；可显式
指定 `--repeats 1..100` / `--warmups 0..10` 做工具检查，但小重复不作为正式 p95 结论。

授权后仅创建一个自有 UUID/label、image ID 固定、loopback 端口的临时容器。复用
[第 6 步容器归属保护](memory-qdrant-faults.md)，不连现有 6333、不读 `.env`/Settings、
不操作真实聊天库、不下载镜像。各规模使用独立 collection/SQLite，避免旧点污染本档。
最终只删除验证归属的临时容器，保留专属结果目录内的 bind 数据/SQLite/JSON，不删卷。

manifest 记录环境、参数、数据/审核锁/缓存/代码 SHA，quality.json、scale-0..3.json、
summary.json 保存分组成绩及样本。失败逐步落清单，错误只存类别，必尝试归属安全
清理；只有全部测量和清理成功才 complete=true。失败不是记忆漏召回，未完成的
summary 不能当完整成绩。零新增模型调用不等于整个线上记忆零成本，不核对实际账单。

## 验证与交付

新增 [`test_memory_qdrant_benchmark.py`](../tests/test_memory_qdrant_benchmark.py) **45 项**：
真实 SQLite/FTS/编排，假远端仅作离线有效性测试，覆盖范围、数量、缓存/审核前置、
降级识别、稳定性/规范变化、完整排名/浮点漂移、统计分母、失败 checkpoint/清理及
脱敏、默认计划无副作用。扩大定向 **783 passed**，两条既有依赖警告，Ruff/diff/冻结通过。

真实基准的结论是保留当前默认：24/24 有序来源一致，12/12 召回、零 FP；200 候选
合成 workload 下 SQLite/Qdrant 的本地 p95 为 22.549/92.393 ms。本轮不改生产默认。
后续优先候选是**Qdrant 读路径 profiling/批量校验优化**，保留安全语义后重新配对；
若扩大检索窗口或用 ANN，则是新阶段而非补改本阶段已冻结的成绩。

2026-10-12 后续已开始 [读路径优化第 1/5 步](memory-qdrant-optimization.md)：
单独增加分段诊断与第四臂开销对照，不覆盖本页历史基准或宣称优化已完成。
