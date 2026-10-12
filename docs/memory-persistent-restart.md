# 持久记忆同库重启检查（第5项）

入口：[check_memory_persistent_restart.py](../evals/check_memory_persistent_restart.py)。
2026-10-13 已在独立真实验收库运行通过，结果见
[补验与重启证据](../evals/baselines/memory-persistent-restart-v1-2026-10-13.md)。

这里的“重启”指两个真实、不同 PID 的短生命周期 Python 进程先后重新打开同一
持久化 SQLite，重建生产读对象并访问既有 Qdrant 集合。不是只重建一个内存对象，
也不停止浏览器服务、重启 Docker/Qdrant、模拟系统掉电或覆盖数据库。

## 检查范围

- 绑定已协作审阅的第11/12条补验报告及其 SHA，沿链核对原 V1/V2、恢复报告和
  manifest；关键生产源码须与补验时一致，当前数据库全数据/schema快照须等于补验后状态。
- 1条活跃的12秒纠正仍存在；旧8秒决定退休且替换沿革完整。开发待办已完成、个人
  待办已取消，关闭来源保持；误写偏好仍为人工撤销退休，不删除历史来掩盖失败。
- 生产 BM25 召回仅返回有效决定，完成/取消事项不进入普通待办召回，个人房间不
  召回开发决定。重开后复用既有取消状态查询，经生产 SQLite 状态回执函数得到原回复；
  不新增消息、turn或调用Agent。
- FTS元数据必须ready、文档投影与规范记忆一致；FTS文档/embedding缓存各只有有效
  决定1条。生产outbox规范物化检查5条revision，必须1条有效向量、4条墓碑，无缺失缓存。
- 真实 Qdrant 做规范→远端 payload/revision/向量核对，以及远端→本地的有界完整
  身份扫描（最多8页，每页16条）；多余/缺失/漂移/超前版本/错误范围均失败。
  此audit只读，不在SQLite插入maintenance记录，不注册目标、不认领队列或做修复。
- 复用已持久化的有效决定向量做**同文本自匹配**，要求直接向量命中（相似度≥0.99）
  和生产RRF混合召回命中。仍走Qdrant读适配器的规范候选/版本复验，并验证房间和
  来源讨论排除。它不是新的查询embedding，也不评价改写查询的语义召回质量；不调用
  云client，不生成合成向量。缓存回放器拒绝任何其他文本。
- 两个子进程观察结果须相同；退出/回收后再次核对完整库快照、证据和源码不变，
  才能标记`restart_passed=true`。任何失败只留新报告，不自动修复或抹去原失败。

## 操作命令

无参数只打印计划，不读配置或目标。`--check`读取既有目标配置但不打印凭据，
只检查本地，不连接Qdrant/模型，不启动子进程或创建报告。`--run`有界运行两个
新进程，访问本机Qdrant；**Agent/embedding API均0次**。

```bash
.venv/bin/python -m evals.check_memory_persistent_restart

.venv/bin/python -m evals.check_memory_persistent_restart --check \
  --manifest /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json \
  --followup /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/followup-runs/20261012T153115Z-78c39e8f/report.json

.venv/bin/python -m evals.check_memory_persistent_restart --run \
  --manifest /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json \
  --followup /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/followup-runs/20261012T153115Z-78c39e8f/report.json
```

每个子进程最多45秒，父进程只清理自己创建的Popen句柄，有限terminate/kill/reap，
不按进程名/PID文件杀进程。干净子进程环境不继承云密钥、HOME或父进程目标覆盖；
必要的本机Qdrant配置由经过验证的manifest和既有配置读取。SQLite用`mode=ro`+
`query_only`，写事务入口直接拒绝，不调用初始化/迁移/FTS重建。

新证据在目标内`restart-runs/<时间-随机ID>/`，目录0700、JSON和输出0600、Git忽略。
同库无变更时可另行显式运行此只读检查；脚本不会自动重复。任何聊天/维护/代码变化
会使旧基线失效，需新的证据基线，不提供`--force`、清库或偷偷更新报告的绕过方式。
不要同时操作验收库；advisory锁仅协调现有验收工具，不能拦截任意第三方写库。

当前五项完成是“前序真实运行＋单条恢复＋两条补验＋同库重开”的组合验收，
**不是重新跑12条全通过，不是新盲测，不是生产/灾备上线证明**。旧补验仍保留
`full_lifecycle_passed=false`和`semantic_review=pending_human_review`；协作者核对实际
回复的说明另记在新报告，不伪造用户逐条签审或改写原成绩。

## 使用与恢复边界

全局配置仍为SQLite/legacy、worker disabled；没有自动切换全局默认、迁移旧聊天库、
配置自启动服务或让Fake页面冒充真实模型UI。只需观测时用`--check`及既有worker
`--status`；日常使用该目标、启用前台worker或切换服务需另行明确选择。

本检查不写库，因此无需恢复或覆盖。若故障发生，应先保留当前证据和在线备份，再
明确具体目标和修复授权；不能直接覆盖本机备份而忽略远端revision墓碑。恢复点是
单独的SQLite在线备份，**不等于已经验收SQLite/Qdrant联合灾备恢复**。
