# Kimi 真实记忆忠实性难例：16 条单回合重放

运行目录：`evals/results/memory-fidelity-v1/20261009T034128Z-f93bc866/`（本机 Git 忽略）。每例使用独立临时 SQLite 房间、Kimi Code implementer 与正式聊天室自动写入路径；评测只追加单 Agent `handoff_to=[]` 约束，不调用 embedding。原始逐例 JSON、`summary.json` 和包含数据集/代码 SHA-256 的 `manifest.json` 保留在该目录。密钥不在结果中。

机器原始汇总：**16/16 回合完成，15/16 写入有无与 v1 标签一致**；共写入 15 条记忆，15 条均为当前 Human 原消息的连续原文，且类型与实际写入意图相符。唯一标签不符的是 `dev_unresolved_decision`：v1 预期“不写”，Kimi 保存了 `open_loop`「我们还没决定采用 SQLite，先比较两种方案。」。它没有误记成已决定。用户在本次审阅中明确裁决：**“先比较两种方案”算待办，允许自动写入**。因此按用户裁决，16 条写入行为均符合预期；原始 v1 标签与机器的 15/16 数字不追溯改写，以保留可审计性。后续如需自动评分，应另建 v2 标签，不能悄悄覆盖本次结果。

| 用例 | 实际记忆的内容复核 |
| --- | --- |
| `dev_exact_decision` | 只保存 `test_` 命名决定。 |
| `dev_added_work` | 只保存 `test_` 命名决定；回复中的存量迁移、CI 建议未写入。 |
| `dev_unresolved_decision` | 以待办保存“还没决定、先比较”，没有宣称已采用 SQLite；用户已允许。 |
| `dev_scope_dropped` | 保存“仅限本地演示版”范围。 |
| `dev_scope_preserved` | 保存“仅在本地演示版”范围。 |
| `dev_negation_dropped` | 保存“不采用 Redis”，未反转否定。 |
| `dev_relative_time_invented` | 保存“下次发布前”和“所有链接”，没有换算日期。 |
| `dev_relative_time_preserved` | 同上，没有缩小“所有链接”范围。 |
| `personal_third_party` | 没有把朋友口味写成用户偏好。**但普通回复把“这不是我的偏好”说成用户“明确不喜欢”，属于回答语义过度推断**；未污染记忆。 |
| `personal_self_in_mixed_message` | 只保存用户本人“不喜欢香菜”，没有混入朋友偏好。 |
| `personal_negation_dropped` | 保存“不喜欢香菜”，未反转否定。 |
| `personal_condition_dropped` | 保存“只喝无乳糖的”限制。 |
| `personal_condition_preserved` | 同上，限制没有丢失。 |
| `personal_relative_time_invented` | 保存“下次周末”，没有换算成绝对日期。 |
| `personal_relative_time_preserved` | 同上。 |
| `personal_added_advice` | 只保存“不加香菜”，没有添加点餐建议。 |

本表是 Codex 对原消息、写入内容和回复的逐例**Agent 审阅**，不是独立人工盲审。用户只裁决了 `dev_unresolved_decision` 的写入政策；其余内容尚未由用户逐项签字。重复的相同来源样本验证了模型采样下的稳定性，但 16 条仍是合成短消息，不能推出真实长对话或记忆长期有效性的通过率。相对时间待办没有自动到期机制。下一步应将普通回复的第三方偏好推断加入回答质量测试，并在新的标签版本中纳入用户裁决；不要修改冻结的 v1 结果。
