# 记忆检索升级：FTS5/BM25 准备与对照基线

2026-10-11，第一阶段 **6/6** 个子步骤完成：含第 6 步的离线两分支、真实四分支和缓存复跑。**默认仍为旧词项**，可配置 `bm25`；没有修改题库/阈值或接入 Qdrant。本轮只验证临时库，没有打开用户真实数据库、修改私有配置或重启在用服务。用户确认后采集真实百炼向量，成功 34 请求、0 Agent；缓存复跑不联网。

## 进度

| 子步骤 | 状态 |
| --- | --- |
| 1. 确认接口、FTS5 能力和对照基线 | 已完成 |
| 2. 设计中文处理方式 | 已完成；[规则与离线验证](memory-fts-text.md) |
| 3. 建立索引、回填与生命周期同步 | 已完成；[实现、故障语义与验证](memory-fts-index.md) |
| 4. 接入 BM25 与现有向量/RRF | 已完成；[接线、配置与降级规则](memory-bm25-recall.md) |
| 5. 补检索边界、重建及降级测试 | 已完成；[矩阵、恢复修复与剩余边界](memory-fts-acceptance.md) |
| 6. 检索对照、延迟及结论 | 已完成；[实验与报告](memory-fts-ab.md)，真实四分支与缓存复跑，无 BM25 增量收益证据，不切默认 |

## 1. 本机能力确认

使用项目 `.venv/bin/python`，而非系统 `sqlite3` 命令；结果只说明该 Python 绑定的 SQLite 运行时：

| 检查 | 本次结果 |
| --- | --- |
| Python | 3.11.16 |
| SQLite | 3.53.4 |
| 编译标记 `ENABLE_FTS5` | 存在，仅作辅助证据 |
| 内存库创建 `USING fts5(content)`、插入和 MATCH | 成功，只匹配目标测试行 |
| `bm25()` 查询排序 | 成功，目标行返回有限分数 |

依据 [SQLite FTS5 官方文档](https://www.sqlite.org/fts5.html)，使用实际建表、MATCH 和 BM25 验证，不只依据版本或编译标记判断。检测不加载外部扩展，仅使用两条英文合成文本；**未评价中文分词、相关性提升或迁移兼容性**。

新工具 [`check_memory_fts_baseline.py`](../evals/check_memory_fts_baseline.py) 可复验：

```bash
.venv/bin/python -m evals.check_memory_fts_baseline
.venv/bin/python -m evals.check_memory_fts_baseline --json
```

默认打印摘要，`--json` 把运行时、代码指纹和逐题结果打印到标准输出。工具不读取 `.env`、不实例化 Settings、不创建 Agent/云 embedder，不接受用户数据库路径；能力检测只用内存库，检索重放在临时文件库中进行并自动清理。没有自动重写基线或隐式联网选项。FTS5/BM25 不可用、题库/审核锁变化或旧检索输出不一致时返回非零状态。

## 2. 当前接口和必须保留的边界

[`ChatMemoryStore.retrieve`](../app/chat/memory.py) 的入口保持：

```python
retrieve(room_id, query, *, limit=3, exclude_correlation_id=None)
```

- 返回有序 `tuple[ChatMemory, ...]`，保留规范记忆 ID、来源 ID 和原文，不用全文索引行取代事实记录。
- 候选限定同房间、`active`；排除来源属于当前讨论的记忆，最多 200 个候选；`limit` 只允许 1–3。
- 旧词项算法：casefold 后提取 ASCII 词项与中文双字片段，以集合交集数量降序排；同分按创建时间降序，再保留候选输入顺序。
- 本步不改混合排序：语义候选先过相似度门槛，再与词项排名按 RRF 常数 60 融合；后续配对使用阈值 0.65，不按这批已见题调参。
- 外部讨论的“不读取本地记忆”由 [`chat_context`](../app/chat/dispatch.py) 控制，不是 `retrieve` 的参数。新检索后端不能绕过这个上层限制。
- 替换、撤销、完成/取消已退休的记忆不得重新进入活跃召回；历史与状态记录仍需保留。

第 4 子步骤已让旧、新关键词分支可分别选用，默认保留旧路径作对照，不把冻结输出重新定义为新 BM25 的预期。新关键词先 BM25 升序排序，再与向量按 RRF 融合；有索引状态与范围检查及返回前规范复验。向量库与双库同步属于后续独立阶段，当前不动。

## 3. 固定对照数据与输出

本轮复用用户已确认的 24 条合成题 [`memory-unseen-v1.jsonl`](../evals/memory-unseen-v1.jsonl)，开发/个人各 12 条。文件名虽然有 unseen，这批题已被运行和观察，**现在是已见对照/回归集，不是新盲测**。

[`memory-fts-legacy-v1.json`](../evals/memory-fts-legacy-v1.json) 随仓库保存：

- 题库 SHA-256、原人工审核锁文件 SHA-256、冻结日期。
- 旧检索接口、范围和排序约定，以及本次 Python/SQLite 探测结果。
- 生产记忆/向量/上下文及实验脚本的代码指纹。
- 24 题的有序召回来源、来源级 TP/FP/FN 与汇总指标。

题库哈希仍为 `0155a7c8c724ab2ebd391a16fe0f163b13eaf79d56df282d6713d653c3187c2c`；没有修改原金标准或审核锁。代码哈希是追溯证据，不禁止后续开发；工具会报告代码指纹变化，但只在冻结数据/约定或旧路径结果变化时判定基线失配。已见数据如需修改应另建版本，不自动覆盖本次基线。

复用既有配对实验的注入夹具：在独立临时库中用手动 API 保存金标准原文，不调用 Agent 做写入；该注入路径的 `kind` 为 `fact`，不评价自动提炼的种类或忠实性。查询前后规范活跃记忆快照必须一致。

| 来源级指标 | 当前旧词项重放 |
| --- | ---: |
| TP / FP / FN | 10 / 0 / 2 |
| Recall@3 | 10/12 = 83.33% |
| Precision@3 | 10/10 = 100% |
| 空金标准题误召回 | 0 |
| Agent / embedding 调用 | 0 / 0 |

旧词项仍漏掉 `dev_async_queue_distractor` 和 `personal_return_library_book`，与 [2026-10-10 检索 A/B](../evals/baselines/memory-retrieval-ab-v1-2026-10-10.md)的旧词项结果一致。第 6 步的[新对照](memory-fts-ab.md)确认 BM25 也未补回这两条，24 题有序输出一致。随后真实四分支实测中，两混合分支都用向量补回两条、召回 12/12，缓存复跑一致。该成绩有独立本轮证据，不用历史混合结果替代，也不归因于 BM25。

只有 12 个应召回来源；指标不是 24 个答案的通过率。零 FP 不能外推到线上。第 1 步只测旧冻结，第 6 步新增本机热检索延迟；都不测真实模型写入/回复、并发负载或总体记忆准确率。

## 4. 无付费测试

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_memory_fts_baseline.py tests/test_memory_retrieval_ab.py tests/test_chat_memory.py tests/test_vector_memory.py
.venv/bin/ruff check evals/check_memory_fts_baseline.py tests/test_memory_fts_baseline.py
```

第 1 子步骤新增测试覆盖缺少编译标记仍按实际能力判断、缺少 FTS5/BM25 的明确失败、连接关闭、题库/审核锁/约定变化、有序结果漂移，以及离线重放不访问私有配置/云端且临时库已清理。既有记忆与向量定向测试继续保护当前路径；当时未实现 FTS 索引维护，不等于全仓全绿。

本次上述 Python 定向套件 **81 passed**，有两条既有依赖弃用警告；Ruff、基线复验和文档本地链接检查通过。没有运行全仓测试，没有测新增 BM25 召回质量，也没有付费调用。

第 2 子步骤的[中文与混合文本 v1](memory-fts-text.md)采用确定性预分词后交给 FTS5 ASCII tokenizer；原文保持不变。第 3 步完成[派生索引维护](memory-fts-index.md)，第 4 步完成[可选 BM25 召回](memory-bm25-recall.md)。第 5 步[边界与恢复矩阵](memory-fts-acceptance.md)新增 74 项测试，修复同名失效触发器在启动时被保留的缺口，当时扩大定向套件 **406 passed**。第 6 步新增评测器 33 项测试，扩大定向 **439 passed**，Ruff 通过；真实四分支与缓存复跑完成，BM25 相对旧关键词无增量质量/速度证据，默认仍为旧路径。不调用 Agent，不核对账单；向量数据库属于下一阶段。
