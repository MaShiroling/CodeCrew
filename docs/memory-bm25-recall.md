# BM25 召回与向量 RRF 接线（第 4/6 步）

2026-10-11，BM25 已作为可选关键词后端接入 `ChatMemoryStore.retrieve`。
默认仍为 `legacy`，保留旧接口与冻结输出，尚未用配对质量/延迟数据决定默认切换。
本轮不是向量数据库接入，也没有新增 Agent 或 embedding 调用步骤。

## 如何选用

在私有 `.env` 配置后，按原方式重启聊天服务：

```dotenv
CODECREW_MEMORY_LEXICAL_BACKEND=bm25
```

改回 `legacy` 即恢复旧词项排序。仅加载 Settings 不会修改运行中服务的配置。
两个取值之外的配置会被拒绝；代码直接构造时可传
`StandaloneChatStore(database, lexical_backend="bm25", vector_index=...)`。
本次未改你的私有 `.env`、未重启在用服务、未打开真实数据库。
选择 BM25 不会自动开启云向量；原 `CODECREW_MEMORY_EMBEDDING_MODE=off/on`
及供应商配置保持独立。

## 候选、排序与融合

先按规范数据生成同房间、`active`、排除当前讨论的候选快照，最多 200 条。
索引状态、版本和全文查询在同一个 SQLite 读事务快照内读取。
[`MemoryFTSIndex.search`](../app/chat/fts_memory.py) 使用版本化文本规则构造参数化
MATCH，再联结派生文档、规范记忆与来源消息。WHERE 同时限制房间、活跃状态、
当前讨论、文档范围和版本，以及调用方给定的候选 ID；不先取全库 top-k 再过滤。
索引只返回规范 ID，不把编码词项作为事实或回传给 Agent。

[SQLite 官方 BM25 文档](https://www.sqlite.org/fts5.html#the_bm25_function)规定分数
越小匹配越好，故使用 `ORDER BY bm25(...) ASC`。同分按规范创建时间降序、记忆 ID
降序确定排序。这里评分的是预分词词项，不是原文字符数，也不是语义证明。

关键词排名保留最多 200 个候选，**不先截成 3 条再融合**；向量分支保持现有模型/
版本校验、精确余弦和相似度门槛（默认 0.65）。共享
[`fuse_memory_rankings`](../app/chat/memory.py) 按排名贡献 `1/(60+r)` 做 RRF，
不混加 BM25 分数与余弦相似度。关键词独有、向量独有和双分支命中均可进入结果。
RRF 常数与既有同分策略不变，最终限制 1–3 条，仍返回有序规范 `ChatMemory`。

云工作在读事务外执行，避免跨网络等待持有快照。返回前再次检查规范状态、房间、
讨论与记录一致性；期间被退休/替换/关闭的条目会被剔除，不以旧内存对象返回。
此时可能少于 limit，不自动补位或重跑云查询。检查之后发生的状态变化不能被
完全消除，不能宣传“回答生成期间绝无竞态”。外部讨论不读本地记忆的 dispatcher
上层守卫继续保留；记忆仍不是执行授权。

## 降级约定

- 索引 `dirty` / `unavailable`、元数据缺失或版本不匹配：不执行 MATCH，关键词分支
  使用旧词项排序；向量分支仍可使用。
- FTS SQLite 查询错误：释放读快照，再持久化 `dirty` 与错误类型，退回旧关键词。
  标记本身失败会记录安全错误类别并继续降级；下次查询仍重新读状态并处理错误，
  不缓存可信标记。恢复使用[第 3 步启动审计/重建](memory-fts-index.md)。
- 查询文本/词项超限：使用旧关键词，不截断意图，也不把健康索引标为损坏。
- 健康 FTS 查询零命中是有效空结果，不借机追加旧匹配。空词项不执行 `MATCH ''`
  或无条件全文查询；有向量时，仍允许语义独有候选。
- 云向量失败：保留所选关键词分支结果；两路均故障时回到旧关键词。
  敏感模式查询不送往云，敏感模式记忆不进入云候选。日志不打印原文或异常正文。

## 证据与边界

[`test_memory_bm25_retrieval.py`](../tests/test_memory_bm25_retrieval.py) 新增 **43 项**
离线测试，包括真实 BM25 方向与词频/长度排序、字面化输入、空查询、有效零命中、
房间/讨论/退休过滤、完整排名融合、真实向量模块配合 fake embedder 的阈值检查、
两路独立降级、缺表失效与重启恢复、日志不含私有文本、200 候选/3 返回边界、
并发变更时返回前校验（确定性回调模拟）、Settings 和 CLI 配置接线。

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_memory_bm25_retrieval.py tests/test_memory_fts_index.py tests/test_memory_fts_text.py tests/test_memory_fts_baseline.py tests/test_memory_retrieval_ab.py tests/test_chat_memory.py tests/test_vector_memory.py tests/test_standalone_chat_store.py tests/test_memory_browser_demo.py tests/test_memory_open_loop_journey.py tests/test_standalone_chat_dispatch.py tests/test_bounded_discussion_control.py tests/test_bounded_discussion_dispatch.py tests/test_cli.py
# 更广的记忆评测器兼容回归：
.venv/bin/python -m pytest -o addopts='' -q tests/test_memory*.py tests/test_chat_memory.py tests/test_vector_memory.py tests/test_standalone_chat_store.py tests/test_standalone_chat_dispatch.py tests/test_bounded_discussion_control.py tests/test_bounded_discussion_dispatch.py tests/test_cli.py
.venv/bin/ruff check app/chat/fts_memory.py app/chat/memory.py app/chat/store.py app/config.py app/cli.py tests/test_memory_bm25_retrieval.py
.venv/bin/python -m evals.check_memory_fts_baseline
```

接线定向套件 **271 passed**；扩展全部记忆测试文件与上述聊天室/CLI 等套件后
**332 passed**，两条既有依赖警告；Ruff 与旧检索冻结复验通过。
旧词项 TP/FP/FN 仍为 **10/0/2**，24 题有序来源一致；没有改冻结文件、金标准或
阈值。所有运行时数据库为临时测试库，向量测试使用 fake/stub，没有真实模型调用。
官方文档核对的网络访问不属于 Agent/embedding 请求。

这不是新 BM25 配对 A/B 或盲测，不提供本轮召回提升、延迟或成本结论，也不是
全仓、浏览器或负载验收。FTS 使用一个共享倒排表，SQLite BM25 的文档频率和平均
长度统计覆盖整个表；虽然返回候选严格按房间过滤，其他房间的活跃文档仍可能影响
分数。它不是按租户隔离的评分统计，不能声称生产级多租户隔离。
未做 reranker/ANN，词项 OR 命中不证明主体、范围、否定与条件一致。

第 **5/6** 步[检索故障/重建/边界验收矩阵](memory-fts-acceptance.md)随后已完成，
本页保留第 4 步接线记录及当时测试计数。下一步第 6 步固定数据与配置运行配对
对照和延迟测量，依据证据决定是否切换默认。现在无需重跑 Kimi。
