# 第二阶段第 5/7 步：可选 Qdrant 混合召回

2026-10-12 完成。聊天服务增加可选的版本化 Qdrant 查询编排，沿用关键词排名与
RRF=60，返回前规范复验；旧 SQLite 默认不变。真实 Qdrant 合成上下文探针通过，
自有集合已清理，真实 embedding/Agent 调用 0。未修改私有 `.env`、真实聊天库或
重启在用服务；这不是默认切换或真实模型质量/ANN/性能验收。

后续 [读路径优化第 3/5 步](memory-qdrant-optimization.md)已批量化前后规范读取，
200 候选两快照 SELECT=1204→10，来源/根消息/缓存/版本校验与前后独立快照均保留。
重复元数据读取也已合并为非空 2 GET+1 POST；向量前和查询前分别检查，不跨请求缓存。
完整校验保留；本页第二阶段历史编号与验收不覆盖。

## 查询路径

[`build_memory_vector_index`](../app/chat/vector_backend_factory.py)在
`memory_embedding_mode=on` 时按 `memory_vector_store` 选择：`sqlite` 仍返回原
MemoryVectorIndex；`qdrant` 返回
[`QdrantMemoryVectorIndex`](../app/chat/qdrant_memory_index.py)。关闭 embedding 时
两配置都不建立向量编排。构造/启动不连接、初始化 Qdrant，也不注册投递目标。

1. ChatMemoryStore 的规范快照限定同房间 active、排除当前讨论、最新最多 **200**
   条；关键词采用已选择的 legacy/BM25，保持完整排名而不是先截成最终三条。
2. 新编排按规范库重新读取候选、来源/讨论和模型缓存，复用 outbox materialization
   的本地 Human、来源/根讨论非外部、敏感词和有效 float32 非零向量检查。
   不允许跨房间或从过期调用对象构造锚点。当前讨论过滤在此再次执行。
3. 模型指纹来自配置的 embedder，维度来自该指纹的 SQLite meta；不硬编码 1024，
   不从查询响应悄悄重定义模型。要求该版本化目标**已显式注册**。
   无匹配缓存/无合格候选不发查询 embedding；目标未注册或服务检查失败走关键词。
4. 先检查远端集合/版本索引，再生成查询向量。查询不补算历史向量、不写缓存、不
   claim/deliver outbox、不初始化集合。embedding HTTP 与 Qdrant HTTP 不持 SQLite 写锁。
5. [`search_current`](../app/chat/qdrant_vector_sync.py)要求传入规范 active mutations；
   服务端在排名/limit **之前**过滤房间、状态、模型/维度、讨论排除，以及每个允许点
   的 ID、内容/来源锚点、**精确当前版本和向量哈希**。返回 payload 也必须完整相等。
   不能仅凭远端“有个合法版本”认作当前；同内容重算缓存也会递增版本。
6. exact=true 查询取全部合格候选（最多 200），按 cosine 降序、UUID 升序确定排序，
   0.65 门槛沿用现有配置；RRF=60 与关键词融合后最多三条。
7. 云/远端调用后重新读取规范版本、来源与缓存，移除变化的语义命中。融合后仍走
   ChatMemoryStore 原有 `_validate_recall`，剔除撤销、纠正、待办关闭、内容/讨论变化；
   `chat_context` 使用 SQLite 的原文/来源，**不使用远端文本作为事实**。

一次读取中的规范复验不等于双库原子快照；复验之后的并发变化仍有窗口。
候选最多 200 的边界没扩大，未启用 ANN；不能因此宣称大语料召回/速度提高。

## 写入与积压

新路径 `index()` 只对规范与可信来源仍匹配且缓存缺失/无效的最多 32 条生成向量，
生成后再验状态，SQLite 参考适配器在事务里再次验证锚点并写**本地缓存**。
第一次生成的实际维度写 meta，同指纹改变维度失败；派生失败不影响已提交事实。
没有直接双写 Qdrant：缓存触发器在原事务内更新版本并给已注册目标入 outbox。

远端建集合、订阅、[历史回填/审计](memory-vector-maintenance.md)与
[worker 投递](memory-vector-outbox.md)仍是显式操作，chat-serve 不自动启动 worker。
积压时低版本点被查询过滤，即使内容相同也不会返回旧向量；少召回是最终一致期间
的可见限制，不用旧 SQLite 语义结果偷偷补齐来冒充 Qdrant。远端成功、本地 ACK 尚未
提交的点若与当前版本/hash 完全匹配，可以安全被读；无需依赖 delivered 状态判断事实。

当前“一记忆一模型缓存”限制不变，缺少新模型缓存会等待；不支持多模型并行预热或
自动热切换。真实查询调用已配置 embedding 服务会消耗额度/发送查询文本；本轮
真实探针的查询向量由本地合成对象提供，**没有**真实云端请求。

## 降级与失败区别

| 情况 | 行为 |
| --- | --- |
| Qdrant 不可用、缺集合/版本索引、未注册或不兼容 | 向量分支失败，保留所选 legacy/BM25；能提前发现时不发查询 embedding |
| embedding 失败、维度漂移、非法响应 | 向量分支失败，保留关键词；日志只写异常类型 |
| 成功空结果、低于阈值、积压版本未匹配 | 语义排名为空，非存储错误；不偷偷使用旧精确语义后端 |
| BM25 不可信/失败，向量正常 | 关键词退 legacy，保留 Qdrant 语义与 RRF |
| 两分支失败 | 退规范 legacy，FTS 原有失效/恢复规则不变 |
| 敏感问题/无合格候选 | 不调用 embedding/Qdrant；现有关键词规则与复验不变 |
| 飞书/外部根讨论及其本地回复 | dispatcher 仍完全跳过本地记忆，不进入任何检索分支 |

敏感词只是现有启发式，不是完整 DLP；公开 API 仍没有生产级多租户认证。
网络超时分别有界，但同步读取不是总请求时限、熔断器或异步后台队列。

## 显式启用与退回

在确认同一规范库、endpoint/prefix、**实际模型指纹和维度**并完成迁移 28 后，
按已有操作入口初始化/注册、回填、投递、审计。维度/指纹应依据缓存的 meta，而不是
凭模型名猜测。以下均为占位示例；本轮没有在真实库执行：

```bash
.venv/bin/python -m app.chat.vector_maintain --run --initialize-target --mode backfill \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions YOUR_DIM
# 同一 run ID 逐页续跑，可显式 --deliver；先确认投递，再新建 audit 并续跑至完成。
.venv/bin/python -m app.chat.vector_maintain --run --run-id RUN_UUID --deliver \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions YOUR_DIM
.venv/bin/python -m app.chat.vector_maintain --run --mode audit \
  --database /path/to/migrated-chat.sqlite3 --model-fingerprint YOUR_FINGERPRINT --dimensions YOUR_DIM
```

私有配置仅由使用者明确选择时设置，保持原 embedding 的 URL/model/revision/key：

```dotenv
CODECREW_MEMORY_EMBEDDING_MODE=on
CODECREW_MEMORY_VECTOR_STORE=qdrant
CODECREW_MEMORY_QDRANT_URL=http://127.0.0.1:6333
CODECREW_MEMORY_QDRANT_COLLECTION_PREFIX=your_prepared_prefix
# BM25 也是独立 opt-in；它没有质量增量证据，不自动设为默认：
CODECREW_MEMORY_LEXICAL_BACKEND=bm25
```

准备、续跑和 worker 要使用与查询一致的 prefix（非默认时传 `--collection-prefix`）。
只有操作者确认后启动/重启选中配置的聊天进程；不自动创建或切换 collection。
新写入的缓存还需后续显式 worker 批次投递。退回 `MEMORY_VECTOR_STORE=sqlite` 即使用
原精确路径，不删除集合、事实或缓存；关闭 `MEMORY_EMBEDDING_MODE` 则只读关键词。

## 验证与下一步

新增 **58 项离线测试**（49 编排/CLI/上下文/探针，9 版本化 REST 查询），扩大记忆/
评测/聊天/讨论控制/CLI 定向 **700 passed**，两条既有依赖警告；Ruff、diff 和旧 24
题有序来源冻结复验通过，不是全仓验收。覆盖模型维度、缓存损坏、版本积压、查询
期间改变事实/来源/讨论/cache、结果失真、外部讨论、两种关键词/RRF 与无副作用启动。

通过 context7-mcp 对齐官方 [query-points](https://api.qdrant.tech/api-reference/search/query-points)
和[过滤约定](https://qdrant.tech/documentation/search/filtering/)，并以实际 Qdrant 1.19.0
验证版本/哈希服务端过滤，而非仅相信假服务的实现。
[真实合成探针](../evals/baselines/memory-qdrant-recall-v1-2026-10-12.md)四组通过：
scoped 语义/RRF/上下文，积压→显式投递恢复，注入三类故障降级，查询时撤销/同库重启。
一个随机自有集合删除并确认消失，0 真实 embedding/Agent；服务与卷保留。

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_memory_qdrant_recall.py tests/test_qdrant_vector_sync.py
.venv/bin/python -m evals.check_memory_qdrant_recall         # plan only
.venv/bin/python -m evals.check_memory_qdrant_recall --live  # 隔离合成库、向量及自有集合清理
```

此步完成时为 5/7；第 6/7 [实际故障/恢复矩阵](memory-qdrant-faults.md)随后完成，
专属容器六次停启、积压/乱序、生命周期及 fresh audit 通过；worker ACK 丢失仍为
实例重启模拟。第 7/7 [质量/延迟/规模对照](memory-qdrant-benchmark.md)也已完成，
当前 **7/7**，默认仍 SQLite；不把合成向量探针作为新检索质量或生产稳定性结论。
