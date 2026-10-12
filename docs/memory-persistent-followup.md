# 持久记忆第11/12条针对性补验入口

入口：`evals/run_memory_persistent_followup.py`。这是对已恢复持久目标的两条追加查询，
不是重新跑12条，不是新盲测，不会覆盖原失败报告或把原12/19用量重置。
开发阶段只做离线控制和只读预检。随后用户已运行真实补验，
`followup-runs/20261012T153115Z-78c39e8f/report.json`两条通过，实际回复经Codex核对；
新增Kimi/embedding=1/0，原报告不改。后续[同库重开](memory-persistent-restart.md)也已通过，
见[实际证据](../evals/baselines/memory-persistent-restart-v1-2026-10-13.md)。

**不要重复下方旧`--live`命令**：查询已写入，恢复点快照不再是当前库，此入口原
`--check`/`--live`均应拒绝。下方命令保留作运行前使用说明；当前只读预检使用重启入口。

## 固定范围与预算

- 第11条 `closed_personal_query`：同房间牙科待办取消状态，要求SQLite的精确取消回执、
  无Agent session、无普通记忆上下文。若意外进入Agent路径，启动前就拦截，不能
  消耗本次唯一的Kimi启动预算。
- 第12条 `other_room_isolation`：通过单月见/Kimi的生产讨论调度回答网关决定是否未知。
  要求普通记忆上下文为空、表达未知/无记录、不给出秒/ms数值、不新增或改写任何记忆。
  原误写偏好提案若再次出现，仍走真实生产守卫拒绝，不在评测器中伪造null或手动撤销。
- 新预算最多 **1次Kimi启动、每回合最多3个内部步骤**，embedding API **0次**。
  没有构造云embedding client；意外走到向量化会计为blocked attempt并让验收失败。
  Kimi启动/步骤不是供应商实际LLM请求数、token或现金费用硬上限。
- 沿用已恢复房间、现有BM25/Qdrant索引及隔离规则，但该个人房间须无活跃记忆，
  没有合格向量候选，因此本次不测语义排序或新的embedding模型效果。
- 不运行前10条、不创建房间/种记忆、不启动worker、不投递/修复队列，不自动付费重试。
  两条都通过后做有界audit，核对既有目标仍一致。失败保留回复/提案/规范状态，不清洗成绩。

## 运行前检查与用户命令

在CodeCrew目录运行。不需要复制密钥到聊天，默认沿用本机保存的Kimi key；
临时输入替代key可在真实命令末尾加`--prompt-for-kimi-key`，输入隐藏。

```bash
# 默认计划：不读配置、密钥或目标，不写数据，不请求网络：
.venv/bin/python -m evals.run_memory_persistent_followup

# 本地只读预检：不请求Qdrant/模型、不创建报告或新消息：
.venv/bin/python -m evals.run_memory_persistent_followup --check \
  --manifest /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json \
  --recovery /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/recovery-runs/20261012T141749Z-6e39a1bc/report.json

# 历史真实运行命令（已经完成，不要重复）：
.venv/bin/python -m evals.run_memory_persistent_followup --live \
  --manifest /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json \
  --recovery /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/recovery-runs/20261012T141749Z-6e39a1bc/report.json
```

`--check`会读取本地目标配置、已完成恢复报告、原报告和SQLite，但不展示配置/密钥。
校验报告路径必须属于同目标的recovery-runs，恢复状态/audit为完成且一致、manifest/
target身份一致、原报告已审阅哈希匹配、当前完整库快照等于恢复后快照、误写偏好已
退休、个人待办确实已取消且关闭来源正确、个人房间活跃记忆为0、全库同步积压为0。

`--live`再做本机Qdrant元数据预检后才读取/提示Kimi凭据，单次执行放在仓库外私有
READ_ONLY工作区。post第一条查询时，在生产写事务中再核对完整快照；发生漂移拒绝。
不要同时使用该验收库、启动其他worker或修改关键源码；advisory锁只协调本工具与
既有验收/恢复入口，不能阻止任意其他程序写库。

## 报告与停止条件

新报告在同目标`followup-runs/<UTC时间-随机ID>/report.json`，私有且Git忽略。
记录两条消息/实际回复/提案/动作、取消回执来源、模型启动预留、禁止embedding路径的
尝试数、来源报告哈希、关键源码及题目哈希、audit与最终清理检查。
新调用前持久化预算预留，原历史12次Kimi/19次embedding仍单列；成功后累计应13/19，
不是宣称原12次上限被重置或原完整运行突然通过。

内存/候选/FTS/cache/revision/outbox全状态必须不变；旧消息/turn/delivery和房间身份也
受保护，只新增两条Human查询与两条回复。退出清理之后再核对状态/计数/证据/源码，
不能在最后清理发生变化后仍标为通过。取消/超时保持未完成并保存预算；本地清理结束
不独立证明供应商侧任务已停止。原数据和报告不覆盖，脚本不会自动撤销新失败写入。

成功标记`complete=true`、`followup_passed=true`，但`full_lifecycle_passed=false`；
free-text仍`semantic_review=pending_human_review`，需要结合前10条、恢复报告和实际
回答审阅决定第4项是否可收尾。它本身不完成第5项重启验收。

若正常回合、回答、记忆不变、budget、audit或清理任何一项失败，会停止并打印
`Follow-up incomplete`。只发新`Results:`路径/终端输出给协作者，不要发送密钥。
不要直接再次运行：两条查询一旦写入，当前快照不再等于恢复点，旧基线会被拒绝；
新的补验/恢复需明确新证据基线与预算，不能编辑报告或清空库来绕过。

当前预检已通过，开发控制与限制见
[入口离线记录](../evals/baselines/memory-persistent-followup-entry-v1-2026-10-12.md)。
