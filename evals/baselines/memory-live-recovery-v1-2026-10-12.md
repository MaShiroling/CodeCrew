# 单条误写记忆真实恢复与审计（2026-10-12，非真实聊天补验）

用户明确批准撤销已确认的误写偏好、处理相关积压并无模型审计。只操作独立验收目标
`20261012T093544Z-5295c08a/chat.sqlite3` 及其既有 Qdrant 同步 namespace；不操作默认库、
旧 Fake 库、全局配置、消息/回复或原 V1/V2 失败报告。不调用 Kimi/embedding。

## 恢复点与真实结果

- 恢复报告：目标下 `recovery-runs/20261012T141749Z-6e39a1bc/report.json`，complete=true、
  phase=recovered_and_audited，live_acceptance_passed=false。
- 撤销前在线 SQLite 备份：该运行下 `backup/20261012T141749Z-e44d00e3/database.sqlite3`；
  integrity/foreign key 检查通过，备份逻辑数据/schema 指纹与原失败报告快照一致。
- 仅撤销记忆 `1722e891-ca99-4a49-96e9-8675d7494083`：status=retired、reason=manual，
  原记忆行/原文/来源消息仍在，规范回滚是可恢复历史，不直接删除记忆或消息。
  生产 retire 接口同步清除该条 cache/FTS，源消息、房间、turn、其他规范记忆/缓存/版本
  与所有候选记录的保护指纹不变。
- 仅1次有界 outbox delivery：旧指针合并，发送最新 tombstone revision=4，远端检查current。
  结束 delivered8/superseded10，pending/processing/waiting/dead 全0；不手动篡改 ACK 状态，
  不启动常驻 worker 或改重试/租约配置，不重建/删除集合。
- audit mode 双向扫描：local_scanned5/current5、remote_scanned5、checkpoint2，
  scan_complete=true、consistent_at_observation=true、changed_since_start=false，findings0。
- 24条消息、12条turn、2个房间、5条规范记忆保留；活跃cache/FTS各从2降为1，
  唯一活跃业务记忆仍是新版12秒网关决定，开发/个人待办仍分别completed/cancelled。
- Agent/embedding调用0/0；真实恢复读写是本机SQLite/Qdrant，不是Fake远端证据。

原 prepare SHA仍 `68dfe614b7436ee8470e87db3cea49767cbea2233ff7b97fc96d210549d264fe`。
原V1报告SHA仍 `6cbbbc5f88492de68e9919d8a60acc2e3d2540052f3317ddb0ebdb13d337aad5`，
原V2报告SHA仍 `56b324fc7e453fa3bea15148948f6d737aecd1eb7a95804e22d9a88ba79ee28d`。
两个原报告complete=false不改，模型历史12/19也不重置；该恢复报告不冒充新live通过。

## 入口与保护

`scripts/recover_memory_live_miswrite.py` 默认只展示计划；`--check`只读核对固定审阅报告SHA、
目标manifest、完整库快照、误写UUID/来源/原文/类型与两条唯一pending，并预检本机远端。
`--apply`须明确授权，先保留备份；生产退休事务内用BEGIN IMMEDIATE再次核对完整快照，
漂移即回滚拒绝。进程锁协调本工具和验收器，不按PID文件/名称杀进程。
audit上限8页、单次delivery、无自动修复/重试/模型调用；失败保留恢复点和阶段报告。
该一次性修复已执行，库快照已变化；再次运行应拒绝，不能把它当通用删除工具。

Context7核对Python3.11 sqlite3在线备份/事务行为，复用既有已测试的备份与生产撤销/投递API，
不用文件拷贝代替WAL一致备份，也不直接改FTS或outbox状态制造成功。
新增7项临时库/假远端控制：计划无副作用、备份后单条退休/同步/审计/父报告保留、
报告/库/积压漂移拒绝、备份失败不退休、写事务内漂移拒绝。真实执行前7+13备份测试20通过。

扩大定向 **1286 passed**（非全仓，两条既有依赖警告），Ruff/diff通过。

## 下一步

第4项仍欠修复后的针对性验收：第11取消状态查询与第12房间隔离/不误写行为。
状态路径设计上不需要模型；隔离查询需新的明确受限真实Agent回合，新增目标/额度另确认。
原报告/预算不能强制重放，也不能以数据修复与audit成功取代真实回复复验。
随后才是第5项同库重启与不复活验收。整体仍3/5。
