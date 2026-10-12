# 记忆模块新增挑战集 v1

当前交付与各轮实验的结论总览见[记忆模块 v1](../docs/memory-v1-delivery.md)；本页保留这一批 24 条题的运行方法和历史结果。

这一批 24 条用例是为下一轮检索 A/B 新写的，不复用旧 20 条的用例 ID 或消息原文；开发协作和个人场景各 12 条。每个领域包含至少 3 条零写入负例、3 条“已有其他记忆但本题不应召回”的负例、2 条多候选干扰检索题。还覆盖纠正、条件/否定、来源边界、同一讨论边界和待办写入。数据不含真实用户秘密；所谓验证码是虚构测试串。

数据与预期来源标注在 [`memory-unseen-v1.jsonl`](memory-unseen-v1.jsonl)，沿用旧基线的 JSON Schema。字节 SHA-256 锁在 [`memory-unseen-v1.lock.json`](memory-unseen-v1.lock.json)。校验命令：

```bash
.venv/bin/python -m evals.validate_memory_unseen
.venv/bin/python -m pytest -q tests/test_memory_unseen_dataset.py
```

**证据边界：**这些题未进入旧模型运行或阈值调参。标注由开发 Agent 起草，用户于 2026-10-10 明确确认了全部 24 条，复核记录在锁文件；这提供了独立人工确认，但仍不是第三方出题、真实用户分布或生产准确率。若改题，另开 v2 并重新锁定哈希，不要悄悄覆盖 v1。检索 A/B 应在相同记忆文本、相同查询、相同代码和阈值下比较；旧运行不能作对照。

## 配对检索 A/B

[`run_memory_retrieval_ab.py`](run_memory_retrieval_ab.py) 默认只验证题库，不调用任何模型：

```bash
.venv/bin/python -m evals.run_memory_retrieval_ab
```

每条题在独立临时 SQLite 数据库中注入**同一份人工标注的活跃记忆原文**，先走生产关键词召回，再为同一快照建立向量索引并走生产混合召回。它报告来源级 TP/FP/FN、Recall@3、Precision@3、空答案题误召回数和逐题胜负；不调用 Agent，也不评价自动写入或最终回复。注入使用手动记忆接口，因此 `kind` 在数据库中是 `fact`；当前召回排序不使用 `kind`，这不能代表真实 Agent 的写入类型准确率。

用户的复核已记录为 `label_provenance: "independently_human_reviewed"`、`reviewer`、`reviewed_at`。显式运行 `--embed` 会调用本机配置的云 embedding API，可能产生费用；输出写入 Git 忽略的 `evals/results/memory-retrieval-ab-v1/`，不记录密钥。若标注需要修改，先创建 v2，不在已经观察结果的 v1 上调参。工具只记录请求次数和文本数量，不把这些数量冒充供应商账单金额。

本批正式配对运行已完成，结果、逐题差异和证据边界见[冻结报告](baselines/memory-retrieval-ab-v1-2026-10-10.md)。

延迟、API 返回 token 用量与公开原价估算见[性能与成本报告](baselines/memory-retrieval-latency-cost-2026-10-10.md)。用户确认本轮不需要核对实际账单，故不将其列为待完成项。

## 真实 Agent 端到端回放

[`run_memory_unseen_live.py`](run_memory_unseen_live.py) 在已冻结的同一批 24 条题上，调用单个 Kimi Agent 和已配置的云 embedding，逐题走聊天室消息持久化、自动写入、生产混合召回、Agent 最终回答。它与上面的“人工注入记忆”A/B 是不同实验，结果目录也独立。默认只校验，不耗模型额度；固定 4 条冒烟题覆盖两个领域的正反例和干扰项：

```bash
.venv/bin/python -m evals.run_memory_unseen_live --smoke
.venv/bin/python -m evals.run_memory_unseen_live --live --smoke --prompt-for-kimi-key
.venv/bin/python -m evals.run_memory_unseen_live --live --prompt-for-kimi-key
```

也可用多个 `--case CASE_ID` 只跑选中的题。中断后以 `--resume-from 结果目录` 继续同一组题；仅在题库、代码、embedding 配置、超时和选题完全一致时复用已完成的连续前缀。结果保存于被 Git 忽略的 `evals/results/memory-unseen-live-v1/`。密钥通过终端隐藏输入，不写入结果；不要把密钥粘贴进聊天或上传结果目录前忘记检查内容。

`summary.json` 的写入和召回计数是按来源 ID 自动计算的；`answer_review` 仍标记 `pending_human_review`。人工复核时尤其检查否定、条件、纠正后旧值、第三方归属、跨房间和同讨论边界。用例 `complete=false` 或基础设施异常时会停止，不能把它算成记忆质量失误。保持生产阈值 0.65，不用这批已观察结果回调阈值；如要修改系统或题库，重新跑并保留原始结果。这个实验仍不能代表真实用户流量或多 Agent 场景。

四条真实模型冒烟运行和人工回答复核见[2026-10-10 冒烟报告](baselines/memory-unseen-live-smoke-2026-10-10.md)。

完整 24 条的 Kimi 端到端运行、续跑记录、来源级统计和回答复核见[2026-10-10 全量报告](baselines/memory-unseen-live-v1-2026-10-10.md)。

全量复核后，聊天室提示词增加了房间内长期格式偏好、已确认决定与明确禁令的边界说明；离线回归确认提议若被 Agent 发出，服务端可保存该偏好并保持房间隔离。三条已看过的故障题已按以下命令真实复测，结果见[三题回归报告](baselines/memory-unseen-regression-2026-10-10.md)，不能把结果再称为未见样本：

```bash
.venv/bin/python -m evals.run_memory_unseen_live --live --prompt-for-kimi-key \
  --case personal_private_room_format \
  --case dev_sql_log_scope \
  --case dev_async_queue_distractor
```

复核时除了自动来源统计，还要逐题检查：私人房间偏好是否实际写入且共享房间不默认采用；生产 SQL 日志是否保留“不要开启”的禁令；RabbitMQ 是否作为已记录决定回答、没有无根据地重开决策。三题中前后两项已改善，SQL 回复仍主动给了生产临时开启的路线，故进一步收紧了“只是问现行规则时不主动提供冲突例外”的提示词。追加修订后的 SQL 单题回归已完成：核心禁令被正确回答，但仍附带一个明确需新决策的采样设想；详情见同一[回归报告](baselines/memory-unseen-regression-2026-10-10.md)。不再对同题反复调参；泛化评价需要另备并冻结新题。
