# 记忆检索延迟与 API 用量基准（2026-10-10）

在用户复核的 24 条合成用例上，用北京地域百炼 `text-embedding-v4`、生产相似度阈值 0.65 测量。每题关键词检索 3 次，已建索引后的混合检索 3 次，另用独立临时 SQLite 数据库测 1 次缺少向量索引时的首次读取。未调用 Agent，也未使用真实聊天数据。结果位于本机 Git 忽略目录 `evals/results/memory-retrieval-benchmark-v1/20261010T015622Z-b391d345/`，`manifest.json` 记录数据/代码指纹，`summary.json` 记录聚合数字，不含密钥。

复现命令（显式联网，最多 160 次 embedding 请求）：

```bash
.venv/bin/python -m evals.benchmark_memory_retrieval --live --repeats 3 --max-calls 160 --price-per-million-cny 0.5
```

| 阶段 | 样本数 | p50 | p95 |
| --- | ---: | ---: | ---: |
| 纯关键词召回 | 72 | 0.661 ms | 1.128 ms |
| 单条记忆建立云向量索引 | 22 | 357.917 ms | 629.160 ms |
| 已建索引、实际调用云向量的混合召回 | 42 | 372.627 ms | 719.494 ms |
| 已建索引、未触发云调用的混合召回 | 30 | 0.839 ms | 2.614 ms |
| 缺少向量索引、首次读取并回填的云召回 | 14 | 788.360 ms | 1095.329 ms |

这里的“冷读”是**派生向量索引缺失时的首次查询回填**，不是应用进程启动；正常路径会在记忆写入时建索引。“热读”仍会对每个查询调用云 embedding，项目目前没有查询向量缓存。p95 采用 nearest-rank 样本分位数；冷读只有 14 个实际发起云调用的样本，不能视作线上 SLA。测试环境、网络和并发负载也不代表生产流量。未触发云调用的本地路径单独统计，避免稀释云请求延迟。逐题混合召回结果与 24 条标准来源一致。

本次修正后的正式基准共 **92 次 embedding 请求、96 段输入文本、API 返回 1,140 tokens**；92 次响应全部返回用量。按照[百炼北京地域 `text-embedding-v4` 公开原价 ¥0.5/百万输入 token](https://help.aliyun.com/zh/model-studio/text-embedding-v4)，本次请求的**原价估算**是 **¥0.00057**。[兼容接口文档](https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai/)示例确实返回 `usage.total_tokens`，本工具据此统计，而不是按字符猜测 token。估算不含免费额度、优惠、资源包、账单舍入，也**不是实际扣款**；`actual_billed_cny` 留空。用户已确认本轮不要求核对实际账单。估算仅覆盖本次正式基准，不包括之前的检索 A/B、早期 pilot 或本次被替代的首次基准。

第一次基准运行 `20261010T015313Z-bc5c09ff/` 在复核后发现只统计了查询房间的记忆建索引，漏算跨房间用例在原房间的写入索引，故不作为正式用量/索引延迟证据；修正后另加了跨房间回归测试并完整重跑。
