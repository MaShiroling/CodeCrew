# 同持久库真实记忆旅程：首轮未完成

2026-10-12，用户本机运行 --live，单月见/Kimi + 实际百炼 embedding + 独立 worker。
原报告 `.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/live-runs/20261012T105311Z-d0aa397b/report.json`。
所有已记录源码 SHA-256 和 prepare.json 摘要复验一致，原报告不修改。

## 原结果

| 步骤 | 原判定 | 实际观察 |
| --- | --- | --- |
| create_decision | 通过 | Human 8秒决定自动提炼、缓存、同步 |
| recall_decision | 通过 | 实际 prompt 和答案含8秒，直接向量检查通过 |
| replace_decision | 通过 | 新12秒记录指向旧记录，旧版墓碑 |
| recall_replacement | 通过 | 实际 prompt 和答案含12秒，直接向量检查通过 |
| create_dev_todo | 通过 | 自动写入“发布前完成 gateway loadtest 压测。”并同步 |
| recall_dev_todo | 未通过，停止 | 正常回合/混合prompt/回复正确找到待办；直接向量断言缺目标 |
| 第7–12步 | 未执行 | 完成/个人待办/取消/隔离及最终audit不能视为通过 |

原 complete=false、full_lifecycle_passed=false，failure_category=direct_semantic_target_missing。
Kimi starts=6，embedding requests=12，embedding errors=0；探测成功证明本轮百炼凭据可用，
不是对未来可用性/实际账单的承诺。失败发生在向量检索附加断言，不是额度上限或鉴权错误。
答案仍是 Agent/规则初审，semantic_review=pending_human_review，不冒称独立人审或新盲测。

第6原 prompt 存在目标 memory_id，实际 reply 依据记录说明“待办记录仍开启”，
并没有修改状态或宣称现实任务已完成。此时混合召回已经成功，原脚本还要求每条查询
纯向量也独立命中，因此它判的是更强交集，不能把该失败笼统说成记忆丢失。

worker exit0/stopped，attempted6，observed诊断，积压0，cleanup_complete=true；
保留 outbox delivered4/superseded4 是保留行状态，不是恰好一次/终生成功数。
数据库保留1个房间与3条记忆身份（旧决定退休、新决定和待办活跃）。无最终旅程audit。

## 不耗模型额度的只读复核

只查SQLite/本机Qdrant，并用已缓存待办向量做 exact scoped search，没有新百炼/Kimi请求：

- BM25 对原第6查询命中待办。
- 三条远端 mutation 复验均 current：旧决定rev4/tombstone、新决定rev2、待办rev2。
- 两个活跃缓存均1024维，待办自身向量查询命中自身，similarity=1.0。

这证明当前索引/版本检查正常，不是原查询的分数测试。原脚本没有留存该查询向量/分数，
所以“可能低于0.65”只能是推测，不能写成已测得阈值根因，也不证明失败瞬间无竞争。

## 下一步建议与限制

拆分混合生命周期判据和纯向量诊断，保留单支缺失，而不是临时降低阈值或换题。
支持严格核对现有状态/原报告的断点续跑，原先已支付的前缀不重放。
第6回合已有真实回复，若改变评测口径应另记重判证据，不覆盖 v1 的失败；
需要补查询分数时单独报告请求与成本，不冒充本轮原始分数。
当前入口要求全新聊天数据，没有续跑功能；**不要直接重复命令或清空库**。
本轮未修改工具/生产检索/阈值，未追加真实调用。第4步未完成，整个真实运行阶段仍3/5。
