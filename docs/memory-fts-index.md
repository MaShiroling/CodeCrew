# FTS5 派生索引、回填与生命周期同步

2026-10-11，完成检索升级第 **3/6** 子步骤。入口是
[`fts_memory.py`](../app/chat/fts_memory.py)。已接入应用启动与记忆写事务，
**召回仍走旧词项 + 可选向量/RRF**；本轮不实现 BM25 查询后端，不改题库、金标准、
阈值、向量存储或自动写入审批策略。

以上是第 3 步当时的范围。第 4 步已接入[可选 BM25 召回](memory-bm25-recall.md)，
默认仍是旧路径；本页保留第 3 步的实现与测试记录。

## 数据与启动

规范事实仍在 `standalone_chat_memories`，原文与来源消息保持不变。
迁移 26 只创建普通 SQLite 表：

- `standalone_chat_memory_fts_documents`：活跃记忆的派生文档，保存规范 ID、房间、
  来源讨论、原文 SHA-256、文本规则版本、编码词项；`rowid` 是索引内部 ID。
- `standalone_chat_memory_fts_state`：`ready` / `dirty` / `unavailable`、版本及错误类型。
  `status()` 只返回元数据和文档数量，不返回文本/词项。

应用启动后条件创建 FTS5 external-content 表，内容源是**派生文档表**，不是原文表；
随后安装增删改触发器。规则采用[中文与混合文本 v1](memory-fts-text.md)，Unicode
数据版本进入 revision，规则变化需要重建。词项编码可逆，**不是加密或脱敏**。

第 5 步补充了[恢复验收](memory-fts-acceptance.md)：启动事务重新安装三个应用
自有派生触发器，修复同名失效定义被保留的缺口；不仅修复已有投影，也验证后续
增/改/删仍同步。这里只替换固定自有名称，不承诺修复任意外部 DDL。

FTS5 模块缺失不会阻断普通表迁移和旧检索启动；索引标记 `unavailable`。
其他可预期 SQLite/编码故障标记 `dirty`。这里只记录错误类型，不把异常文本、
私有内容或密钥写进日志。若连失效标记都无法持久化，则不声称降级成功，事务抛错。

启动审计逐项比较所有活跃记忆与投影的 ID、原文 hash、范围、版本及词项；同时进行
FTS 完整性检查。版本旧、数据漂移、缺失投影或 FTS 表、上次失效都会触发重建。
健康重启不删除重插文档，保持现有内部 rowid。

## 写事务与失败语义

手动保存、Agent 自动提炼、兼容候选批准、替换、撤销、完成/取消待办均调用索引同步。
不添加模型调用，不把旧兼容候选流程变成新的人工审批要求。待审核或被拒绝的候选
不是规范活跃记忆，不进入索引。

健康路径在同一个 SQLite 事务内维护规范状态与派生文档，触发器更新 FTS：
替换写入新文档并移除旧文档；撤销与完成/取消删除活跃投影，但保留规范历史与来源
证据。幂等重复写入不增加文档或重复词频，已经关闭的待办重启不会重新回填。

同步有 savepoint：派生更新中途失败会回滚这一批派生变化，并在原事务里标记
`dirty`；规范记忆可正常提交，旧召回继续工作。此时投影可能是旧快照，**不能查询后
把它当作有效事实**。失效标记也失败则整个规范写事务回滚，不能留下“规范已变但
索引仍被标记健康”的提交。外层事务回滚也会同时回滚规范、投影及 FTS 更新。

第 3 步完成时没有 FTS 召回消费者。随后第 4 步已接入可选查询，检查状态/版本、
处理查询故障，并重新校验规范 `active`、同房间、排除当前讨论及数量上限，不能
仅凭索引内容返回记忆。具体接线与测试见[BM25 召回记录](memory-bm25-recall.md)。

## 重建与局限

`store.memory.fts_index.initialize(force_rebuild=True)` 是内部显式重建入口；常规
`store.initialize()` 会按需审计修复。本轮没有新增可打开任意用户数据库的维护 CLI。
重建仅替换派生文档并执行 FTS rebuild；事务中暂时移除自有触发器，重建后恢复并
校验。不改规范内容、状态、来源、替换链或关闭证据。

[SQLite 官方文档](https://www.sqlite.org/fts5.html)说明，external-content 的触发器
不会回填已有数据；完整性检查使用 `rank=1` 才额外对比内容源。因此测试使用实际
`MATCH` 和完整性检查，不把普通 `COUNT(*)` 当作倒排索引已正确建立的证据。

边界：启动审计是全量、内存 O(活跃记忆总量)，并持有 SQLite 写事务；尚未测大规模
启动耗时、并发负载或跨版本多进程服务。只保护应用内写路径，不宣称防止任意外部
DDL/直接改库；启动会修复已覆盖的数据漂移，但不是持续的防篡改审计。
删除索引条目不等于硬盘安全擦除，规范历史依然存在。

## 本轮验证

新增 [`test_memory_fts_index.py`](../tests/test_memory_fts_index.py) 的 28 项测试覆盖
真实 MATCH、投影更新触发器、手动/自动/兼容写入、房间与替换边界、幂等重放、
撤销、四种待办关闭组合、外层回滚、部分同步失败、失效标记失败、模块缺失模拟、
真实 schema 25 到 26 回填、版本/投影/缺表/元数据漂移、重建失败及恢复。
模块缺失用故障注入测试；本机实际运行时支持 FTS5。

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_memory_fts_index.py tests/test_memory_fts_text.py tests/test_memory_fts_baseline.py tests/test_memory_retrieval_ab.py tests/test_chat_memory.py tests/test_vector_memory.py tests/test_standalone_chat_store.py tests/test_memory_browser_demo.py tests/test_memory_open_loop_journey.py tests/test_standalone_chat_dispatch.py
# 扩大定向回归时，把以下三个文件追加到上面的 pytest 命令：
# tests/test_bounded_discussion_control.py tests/test_bounded_discussion_dispatch.py tests/test_cli.py
.venv/bin/ruff check app/chat/fts_memory.py app/chat/memory.py app/chat/store.py tests/test_memory_fts_index.py tests/test_standalone_chat_store.py
.venv/bin/python -m evals.check_memory_fts_baseline
```

全部数据库是测试临时库；没有打开用户真实数据库、重启在用服务或调用 Agent/云
embedding。新代码会在下次正常应用启动时对该应用数据库执行迁移和审计。
上述定向套件 **191 passed**；追加讨论控制、调度及 CLI 后合并运行 **228 passed**，
两条既有依赖弃用警告；Ruff 通过。不是全仓测试。
旧词项冻结输出仍为 TP/FP/FN **10/0/2**、Recall@3 **83.33%**，有序来源一致；
工具如实报告 `memory.py` 指纹变化，冻结文件未重写。测试不是 BM25 质量 A/B，
不能宣称召回提升或生产 SLA。当时下一步为第 **4/6** 子步骤；现已完成接线，见
[BM25 与向量/RRF](memory-bm25-recall.md)。
