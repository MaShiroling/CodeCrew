# 两条持久补验入口准备（2026-10-12，未运行付费补验）

用户仅要求开发入口。本轮没有真实Kimi/embedding调用，没有启动worker/HTTP/browser，
没有在真实库新增消息、修改记忆或运行audit。当前真实阶段仍3/5。

`evals/run_memory_persistent_followup.py`固定原第11/12条，只追加两个生产Human查询。
新cap=1次Kimi启动/每回合3步，embedding API禁止；没有云client或维度探测，也不把
缓存查询当作新的语义排名评测。保留原两份失败报告、12/19历史用量和恢复证据。
默认plan无副作用；--check本地只读；--live需明确新增调用授权。预算预留在start前持久化。

## 当前真实目标只读预检

- manifest：`20261012T093544Z-5295c08a/prepare.json`，原哈希未变。
- recovery：`recovery-runs/20261012T141749Z-6e39a1bc/report.json`。
- recovery SHA256：`6310b40f3d5a62be082c4d98307bafb773372094fb2b31c6f2af53b1fd6e4b97`。
- validated=true；cases=closed_personal_query/other_room_isolation；new_kimi_cap=1，
  embedding_api_cap=0，new_model_calls=0。当前数据/schema快照仍等于恢复后快照。
- 没有新真实followup报告或回答，不能提前称已通过补验或完成第4项。

## 离线覆盖

27项临时SQLite/Fake Agent/假远端控制测试通过：两个生产查询只启动一次Fake，
SQLite取消回执不启动模型；错误原偏好提案经生产边界拒绝，故意用其他kind误写则
保留真实失败并停止，任何embedding路径也在provider调用前阻止。
覆盖原报告/恢复/数据漂移及越界拒绝、密钥/远端调用之前的live拒绝、只读CLI、
先预留再启动、启动预算耗尽、未知回答/编造秒值/否定未知拒绝、取消留档、清理错误
和清理后修改旧消息不得通过；静态检查无启动worker、投递、初始化/种记忆/修复行为。
真实模型/OS清理证据未产生，Fake adapter不能代替它们。

扩大定向 **1313 passed**（非全仓，两条既有Starlette/httpx和anyio依赖警告），
Ruff/diff通过；旧24条有序召回来源冻结仍匹配，TP/FP/FN=10/0/2、Recall@3=83.33%，
模型0/0。本轮未改变生产守卫/检索或旧标签，关键旧证据哈希均保持不变。

采用Context7 Python3.11 asyncio文档核对wait_for取消和finally清理行为；取消继续抛出，
同时留未完成报告及预算记录，不将取消当成正常完成或保证远端任务已停止。
当前关键源码和题目指纹单独保存，不重新冻结旧标签或把调试回归当作未见样本成绩。

后续运行方式及预算/拒绝处理见[补验说明](../../docs/memory-persistent-followup.md)。
