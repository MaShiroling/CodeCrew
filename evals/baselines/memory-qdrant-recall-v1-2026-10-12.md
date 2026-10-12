# 可选 Qdrant 混合召回真实合成验收 v1

日期 2026-10-12（Asia/Shanghai），第二阶段第 5/7 步。
工具 [`check_memory_qdrant_recall.py`](../check_memory_qdrant_recall.py)显式 `--live`，
本机已有 Qdrant 1.19.0、独立 schema 28 合成 SQLite、随机自有版本化集合。
不是 paid embedding/Agent 或真实用户数据验证。

最终归档：`evals/results/memory-qdrant-recall-v1/20261011T164845Z-968b90bd/`。
目录时间是 UTC（对应本地 2026-10-12），`report.json` 记录代码 SHA-256、检查项、
合成请求计数/投递状态/清理；`synthetic.sqlite3` 保留合成规范库。原始结果在本机
Git 忽略，不随仓库公开；报告摘要可公开。

## 结果

| 检查 | 观察 |
| --- | --- |
| Scoped 语义、RRF 与聊天室上下文 | 六条合成事实含另一房间/当前讨论/撤销点/低分点；关键词独有和语义独有融合，跨话题 `chat_context.room_memories` 只注入允许的规范事实 |
| 当前版本栅栏 | 同内容重写缓存后旧远端点仍在，但被 current revision/hash 查询排除；显式 worker 投递后恢复语义命中 |
| 三分支故障降级 | 注入 adapter 请求失败保留 BM25 且不发查询向量；合成 embedder 失败保留 BM25；FTS dirty 退 legacy 并保留实际 Qdrant 语义 |
| 规范复验与重启 | 实际远端查询返回后撤销事实，命中被规范复验移除；同合成 SQLite 新 store 初始化后不复活 |

`complete=true`、`cleanup_complete=true`；自有测试集合已删除且 GET 确认消失。
真实 embedding/Agent **0/0**，本地合成 query vector requests **8**，不把后者冒充云调用。
outbox 终态 delivered/pending/superseded=8/2/7：最后撤销刻意未远端投递，用来证明
规范过滤/重启不会读取旧活跃点；不是宣称所有事件已同步或一致性审计干净。
服务未停止，持久卷、业务库及私有配置不动。

## 失败与复跑不隐藏

- `20261011T164304Z-89165967/`：沙箱连接受限，无集合创建；不算质量失误。
- `20261011T164403Z-3d7069fa/`：探针错误地使用 Human 源消息作为 reply target，
  被既有“回复必须针对 Agent”规则拒绝（ChatInvalid）；已创建测试集合清理成功。
  修改探针为合成 Agent 回执后复跑，不放宽业务守卫。
- `20261011T164601Z-531e6f88/`：首次全程通过；之后增加请求模型不匹配时在 HTTP 前
  拒绝的守卫/测试，最终同工具复跑并重新记录指纹，上述最终归档为当前依据。

## 离线回归和边界

新增 58 项，49 编排/CLI/上下文/探针，9 REST 当前版本查询；两文件 **98 passed**。
扩大记忆/评测/聊天/讨论控制/CLI 定向 **700 passed**、两条既有依赖警告；Ruff/diff/
旧 24 题来源冻结复验通过。不是全仓、云端模型质量、BM25 收益或延迟/成本 A/B。

故障是受控注入，非实际停服/重启进程；模型向量为人工合成，不是百炼质量评测。
没有 ANN/分布式/大语料性能、长期真实用户或自动后台 worker 证明。
默认仍 sqlite + 原 MemoryVectorIndex；Qdrant 只有显式选择并准备目标后才消费。
参见[编排、操作和限制](../../docs/memory-qdrant-recall.md)，下一步第 6/7 故障矩阵。
