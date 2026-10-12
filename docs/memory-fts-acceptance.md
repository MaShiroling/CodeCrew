# FTS5/BM25 边界与恢复验收（第 5/6 步）

2026-10-11，完成这轮检索升级约定范围内的边界、故障和恢复验收矩阵。
新增 [`test_memory_fts_acceptance.py`](../tests/test_memory_fts_acceptance.py)
**74 项**测试，默认配置仍是 `legacy`。本轮只使用临时数据库及 fake/stub
embedding，不改题库/金标准、阈值、私有 `.env` 或在用服务，不调用真实模型。
不是 BM25 的质量 A/B、盲测、浏览器验收或生产负载测试。

## 验收矩阵

多数场景对 `legacy` 和 `bm25` 都运行；下表的“混合”使用真实
`MemoryVectorIndex` 配合记录调用的 fake embedder。常量向量只验证接线和状态，
不能作为语义质量证据。

| 场景组 | 配置/故障组合 | 必须验证的结果 |
| --- | --- | --- |
| 完整生命周期 | 两后端 × 有/无向量 × 完成/取消，8 项 | 手动/自动幂等写入、讨论排除、替换、撤销、关闭证据、关闭后提议重放、新实例启动与强制重建；历史不变、已退休项不复活、恢复后还能写 |
| 自有触发器恢复 | 两后端 × 增/删/改 × 缺失/同名空实现，12 项 | 启动后新增、删除与更新倒排条目都有效，不只验证当前快照 |
| 数据漂移审计 | 两后端 × 7 种漂移，14 项 | 仅倒排清空、缺文档、多出退休文档、房间/讨论/版本/hash 错误均可启动重建；规范事实与候选历史不改 |
| 重建中途失败 | 两后端 × 回填中途/完整性校验/触发器恢复失败，6 项 | 保存点回滚投影及自有触发器，留下 dirty，旧召回可用；移除故障后可重建并继续写 |
| 外部讨论边界 | 两后端 × 飞书根消息/外部讨论中的本地回复，4 项 | `chat_context` 根本不调用 retrieve；不能手动/自动写入或关闭本地待办 |
| 同快照读取 | BM25 × 并行撤销/重建，2 项 | 候选、状态、FTS 在同一读快照；最后规范复验剔除已被撤销项 |
| 并行读写 | 两后端 × 撤销/重建，4 项 | 检索线程等待向量时另一连接可提交写入；撤销后不返回旧对象，重建不改规范结果 |
| 向量版本与维度 | 两后端，2 项 | 换模型版本重建对应向量；同版本维度变化不混用，所选关键词分支仍可用 |
| 写同步故障闭环 | 两后端 × 有/无向量 × 新增/替换/撤销/完成/取消，20 项 | 部分派生变化回滚，规范变化与 dirty 一起提交；降级只读当前规范事实，重建后结果一致；日志无异常私有正文 |
| 缺少 FTS5 的兼容 | 两后端，2 项；模拟缺模块 | 普通表能迁移，写/替换/关闭/旧召回可用；恢复模块后仅回填活跃规范事实 |

第 3–4 步既有测试继续覆盖 schema 25→26、200 候选/3 返回限制、合法/非法 limit、
查询字面化与超限、有效空结果、不信任索引状态/版本、SQL/云独立失败、敏感模式
不送云、完整关键词排名参与 RRF、向量阈值、外层事务回滚及失效标记写失败。
这些契约测试不是新增人工审核的答案质量题库。

## 找到并修复的恢复缺口

旧安装逻辑使用 `CREATE TRIGGER IF NOT EXISTS`。当自有触发器存在但定义失效时，
启动会保留它；当时投影与倒排一致，完整性检查也可能通过。随后增/改写入会更新
文档表却不更新倒排，而同步函数仍认为成功。这是**恢复后未来维护**的缺口，不是
已经证明真实用户库曾发生此故障。

新增测试先复现该失败，再将
[`MemoryFTSIndex._create_triggers`](../app/chat/fts_memory.py) 改为在启动/重建
事务内重新安装三个应用自有派生触发器。只替换固定名称的自有触发器，不触碰
规范表触发器、事实内容或 schema 26 的既有迁移 checksum；失败仍受保存点保护。
健康重启不因此重插文档或改变其 rowid。

删除验证额外使用**直接 FTS MATCH rowid**，不只联结文档表：即使倒排残留孤儿
条目，联结也可能看不见它，不能把这种空结果当作已正确删除的证据。
恢复后还测试再次新增、更新与删除，避免只验一次重建快照。

[SQLite 官方文档](https://www.sqlite.org/fts5.html#the_integrity_check_command)
说明 external-content 的 `integrity-check` 需要 `rank=1` 才对比内容源。
测试通过 FTS 的 `delete-all` 仅清除临时库倒排，保留投影，验证该检查确实失败，
随后启动重建恢复匹配。没有破坏真实数据库、也没有做硬盘文件字节级腐败注入。

## 复验与本轮结果

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_memory_fts_acceptance.py
.venv/bin/python -m pytest -o addopts='' -q tests/test_memory*.py tests/test_chat_memory.py tests/test_vector_memory.py tests/test_standalone_chat_store.py tests/test_standalone_chat_dispatch.py tests/test_bounded_discussion_control.py tests/test_bounded_discussion_dispatch.py tests/test_cli.py
.venv/bin/ruff check app/chat/fts_memory.py app/chat/memory.py app/chat/store.py app/config.py app/cli.py tests/test_memory_fts_acceptance.py tests/test_memory_fts_index.py tests/test_memory_bm25_retrieval.py
.venv/bin/python -m evals.check_memory_fts_baseline
```

本机 Python 3.11.16 / SQLite 3.53.4，新增矩阵 **74 passed**；扩大定向套件
**406 passed**，两条既有依赖弃用警告；Ruff 通过。旧词项的 24 条冻结有序来源
不变，TP/FP/FN **10/0/2**、Recall@3 **83.33%**。基线工具继续报告此前
`memory.py` 指纹变化，未重写冻结数据；该冻结指纹清单不覆盖所有 FTS 新文件，
所以不是本轮新实现的完整签名或质量证据。

## 仍保留的边界

- 矩阵只覆盖列出的故障模型，不是“任意磁盘/SQL/进程故障都自动恢复”。缺模块、
  中途失败采用注入；实际 MATCH、倒排/投影不一致和跨连接读写在本机 SQLite 验证。
- 启动审计不是每次查询的全量完整性扫描。运行中绕过应用直接改库造成的静默
  漂移可能直到重启/显式重建才被发现；不承诺持续防篡改。规范表或普通元数据表
  的物理损坏、不兼容虚拟表定义、任意 DDL、跨版本服务竞争不在自动恢复承诺内。
- 并行测试用事件协调有限线程与连接，只验证指定交错，不是多进程崩溃恢复、
  并发压测或 SLA。返回前复验之后仍可发生状态变化，不能保证生成答案期间无竞态。
- 全表 BM25 统计、房间级返回过滤、OR 词项的语义局限及敏感词过滤局限仍见
  [接线说明](memory-bm25-recall.md)，不是生产级多租户统计隔离或完整 DLP。
- 本轮没有改召回策略、分词规则、RRF 常数或默认配置；没有新准确率/成本/延迟
  结论，也没有运行全仓测试或真实模型/浏览器旅程。

第 **6/6** 步的[工具、真实四分支对照与缓存复跑](memory-fts-ab.md)现已完成；向量
补回两条语义改写，BM25 相对旧关键词没有额外质量/速度增益证据，保留旧默认。
未调用 Kimi 或核对实际账单。本页上述 406 项为第 5 步当时计数，不覆盖新增实验器回归。
