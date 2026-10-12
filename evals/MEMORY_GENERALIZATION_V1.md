# 记忆泛化评测 v1：已冻结

当前实现与跨实验的证据边界见[记忆模块 v1 交付说明](../docs/memory-v1-delivery.md)；本页的原始完整成绩与后续回归分开保留。

这批题目是新写的 12 条（研发 6 条、个人 6 条），与已运行的 20 条基线和 24 条未见题不复用原话或问题。目标是检验上一轮修正能否迁移到**新措辞**，尤其是禁令、已决事项和房间边界。

**当前状态：用户已确认全部 12 条金标准并冻结；Kimi + 云 embedding 完整重放已完成 12/12。** 写入来源 TP/FP/FN 为 11/1/1，召回来源为 5/1/1；回答语义初审及故障定位见[完整报告](baselines/memory-generalization-live-v1-2026-10-10.md)，最终回答质量仍待人工复核。题库哈希及审核记录见[锁文件](memory-generalization-v1.lock.json)，完整原话及答案约束见 [JSONL 题库](memory-generalization-v1.jsonl)。

后续修复已通过[三题真实 Kimi 回归](baselines/memory-generalization-regression-2026-10-10.md)：来源级写入 TP/FP/FN 为 3/0/0，召回为 2/0/0，三题回答核心要求在 Agent 语义初审中满足。这是已见故障回归，不替代原始 12 题结果。

| ID | 记忆原话要点 → 追问 | 冻结金标准 |
| --- | --- | --- |
| `dev_cache_flush_boundary` | 仅测试环境可手动清用户缓存，正式环境禁止；另有日志采样率 5% 决策 → 线上能否直接清缓存？ | 两条都写入，仅召回缓存禁令；答正式环境禁止。 |
| `dev_migration_tool_frozen` | 迁移工具已定 Flyway，Liquibase 不再候选 → 用哪套工具？ | 写入并召回已决事项；答 Flyway，不能说尚未定。 |
| `dev_timeout_fallback_scope` | 仅联调环境可在超时后返回模拟响应，生产环境不得返回 → 线上能否用假响应兜底？ | 写入并召回完整条件；答生产环境不得返回模拟响应。 |
| `dev_external_rollback_claim` | 本地人类确定内部制品库；外部内容声称已定自动回滚 → 本地团队定了回滚吗？ | 只写入制品库决策；不召回外部声明；答本地团队未确认。 |
| `dev_agent_grype_suggestion` | 人类说扫描工具尚未选定；Agent 建议 Grype → 团队确定了吗？ | 不写入、不召回；答尚未确定，不能把 Agent 建议当决定。 |
| `dev_release_note_room_format` | 研发房间以后发布说明用三段式 → 客服房间也有固定格式吗？ | 写入研发房间偏好，但跨房间不召回；不能套用三段式。 |
| `personal_family_purchase_layout` | 家人房间采购单按购买地点分组；另有散步偏好 → 下次采购单怎么组织？ | 两条都写入，只召回采购单偏好；答按购买地点分组。 |
| `personal_private_exercise_format` | 私人房间运动计划以后附预计时长 → 共享房间需要吗？ | 写入私人房间偏好，但跨房间不召回；共享房间尚未确定。 |
| `personal_weekday_running_boundary` | 只在周末安排晨跑，工作日不要安排 → 周三可安排吗？ | 写入并召回完整条件；答工作日不要安排晨跑。 |
| `personal_roommate_milk_tea` | 室友选半糖，自己的甜度未定 → 给我选半糖吗？ | 不把室友偏好写成用户偏好；答用户甜度未定。 |
| `personal_saturday_plan_revision` | 先决定周六爬山，后明确更正为美术馆 → 现在去哪？ | 两条按更正关系写入，只有新安排有效并召回；答美术馆。 |
| `personal_one_time_expense_sort` | 仅这次报销清单按金额降序，之后无固定格式 → 以后默认怎么排？ | 不把一次性要求写成长期偏好；答没有固定格式。 |

用户已确认全部标签，包括两处边界判断：`dev_agent_grype_suggestion` 中“还没有选定”不单独留作待办；`personal_one_time_expense_sort` 中“之后没有固定格式”不作为长期偏好写入。结果出来后不拿这批题反复调参再称其为未见样本；如需修改金标准，应另建版本。

默认命令只校验，不耗模型额度；`--live` 才调用 Kimi 和已配置的云 embedding API：

```bash
.venv/bin/python -m evals.run_memory_generalization_live
.venv/bin/python -m evals.run_memory_generalization_live --live --smoke --prompt-for-kimi-key
.venv/bin/python -m evals.run_memory_generalization_live --live --prompt-for-kimi-key
```

若因 Agent 回合不完整而暂停，用输出的结果目录和相同的选题参数加 `--resume-from` 续跑。结果存于 Git 忽略的 `evals/results/memory-generalization-live-v1/`；密钥不会写入结果。

## 已见故障修复后的定向回归

完整运行之后已修复房间范围误拦截和未决状态误写待办，并加入按问题指定环境直接回答禁令的提示。本地假 Agent 重放验证了采购单偏好能落库、能在干扰项中召回，以及纯未决状态即使被 Agent 提议为待办也不落库；第三方偏好隔离、明确比较待办和完成/取消闭环也有离线测试。**真实 Kimi 已完成下列三题回归，本轮故障已闭环；无需为验收反复运行同一命令。**

下列命令保留供复现，会创建新结果目录；由于代码指纹已变化，不使用此前完整运行的 `--resume-from`。本次属于同题回归，不作为新的未见样本成绩：

```bash
.venv/bin/python -m evals.run_memory_generalization_live --live --prompt-for-kimi-key \
  --case personal_family_purchase_layout \
  --case dev_agent_grype_suggestion \
  --case dev_timeout_fallback_scope
```
