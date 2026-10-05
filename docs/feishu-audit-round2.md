# Feishu 第二轮独立审计

2026-10-05，Windows / Python 3.12.14。起点 ae8d47c，实际检查了 upstream 快照基线 2574132 到 HEAD 的 diff、三份规格、实现、测试和 SDK 1.7.3 的本地源码。没有依赖第一轮完成声明，没有连接真实飞书、调用真实模型或 push。

## 本轮确认并修复的缺陷

1. **出站顺序错误**：busy 先入 outbox，而更早的 Agent 回复尚未扫描时，busy 会先送出；同 sequence 下按插入 ID 排序也会让 busy 越过最后一条回复和终态。两个确定性用例先失败。修复为发送前补扫、cursor 覆盖检查、同序号按 Agent → run 状态 → 入站通知排序。等待重试的头项仍阻塞同 chat 后续投递。
2. **入站异常后永久 busy**：run 已保存但 finish_ingress 或 start 抛异常，重放只补映射，CREATED run 永远占房。两个故障注入用例先失败。修复为异常时中断 run，以及 received 重放中断未调度的 CREATED run；不重跑 Agent，产生一次中断状态，允许新消息。
3. **取消发送不停止同步 HTTP**：`asyncio.to_thread` 的协程取消不会终止 SDK 线程。受控阻塞替身证明取消后线程仍活着，测试释放替身后才退出。改为每次 HTTP 调用使用可终止子进程，30 秒总等待上限，取消/超时后确认 terminate/join 或 kill/join。新增真实 spawn 子进程的超时、取消、再次调用测试；接收端 stop 同样保护完整清理不被取消中途打断。
4. **修复实现复读时发现的异常链泄漏**：新发送子进程在 SDK 初始化异常后向已关闭的管道报告失败，会抛 BrokenPipeError 并串出原始 SDK 异常。先注入失败复现，再把 SDK 异常丢弃与 IPC 上报分离，管道关闭静默收尾；此问题在本轮交付前修复，不归因于旧线程实现。

没有改旧 migration、Agent 隔离策略或任何既有测试断言/超时。官方请求契约测试改为直接检查子进程内的同步请求函数；生产发送生命周期另有真实进程测试。

## 25 项检查记录

下表“未复现”仅指所列静态检查与离线测试范围，不代表 live 验证。

| # | 检查结论及证据 |
| --- | --- |
| 1 | 未复现第二次推理。`claim` 的事件别名/消息双身份、稳定 root/run；`test_complete_three_messages_aba_and_both_identity_dedupes` 检查模型次数和别名冲突。 |
| 2 | 未复现重复 run，但发现永久 CREATED/busy，已修。原 root/run crash 测试加本轮两处 admission 断点；重启和重放不调度已存在 run。 |
| 3 | 未复现重复 binding。稳定 room UUID、数据库主键；本轮两个线程、两个独立 store 竞争首次创建，最终只有一个 binding。 |
| 4 | 未复现两个 active bounded run。create 的同一 SQLite IMMEDIATE 事务检查 CREATED/RUNNING/PAUSED；本轮两线程竞争不同 root 只有一个 run 成功。既有本地 UI run 竞争测试仍覆盖。 |
| 5 | 未复现丢回复。本轮在 Agent append 后、complete_turn 前故障，重启补扫出一条回复及一次中断通知；无新模型调用。既有游标/插入事务回滚测试覆盖扫描中断。 |
| 6 | **跨系统 exactly-once 不成立**。本轮实际模拟远端成功后 receipt 提交失败，重启再次发送同一条；attempt_count=2，模型次数不变。文档明确有限重试与可能重复，没有把它写成绝对只送一次。 |
| 7 | 未复现重试触发 Agent。sender/outbox 不引用模型调度器；两次发送失败、耗尽、重启、回执断点均检查请求数。 |
| 8 | 未复现 A→B→A 丢发。按 message_id/turn_id 去重而非角色；既有完整三条及八回合循环测试。 |
| 9 | 未复现本地消息外发。scan 同时校验 binding 起点、app/chat/room/correlation、admitted ingress、Agent 和真实 bounded turn；既有历史/本地 root/reply 排除测试。 |
| 10 | 未复现 chat 串上下文。稳定独立 room，chat_context 仅当前链；双 chat 并发测试逐条检查模型 prompt 和出站目的地。 |
| 11 | 未复现两真人显示为“我”。source 保留 sender ID、匿名/姓名加短码；Python context 测试和实际 Node UI 脚本区分同名用户。 |
| 12 | 未复现纯文本 @CodeCrew 放行。adapter 必须收到当前 bot open_id 的平台 mention 元数据；Fake 文本测试拒绝。 |
| 13 | 未复现 @all/其他 bot 触发。精确 bot ID，非平台占位符不授予 mention；未知 Agent 别名也拒绝。 |
| 14 | 未复现 self echo。sender_type 非 user、发送者等于 bot open_id 都丢弃，已有参数测试。 |
| 15 | 未复现空白名单 allow-all。require_feishu 缺任一白名单拒绝启动；allowed 同时检查 chat 和 sender；发送前复查。 |
| 16 | 本轮新增发送子进程的关闭管道异常链风险已复现并修复（缺陷 4）。其余配置路径使用 SecretStr、固定错误、哈希日志、禁用 SDK logger；配置不进业务表或状态 API，Agent 环境白名单不传飞书密钥。SQLite 原始用户/Agent 正文仍可能含用户主动提交的敏感内容，规格明确未承诺通用脱敏。 |
| 17 | 未复现外部文本创建 Task/Worktree。bridge 只调用 external chat + bounded dispatcher；“批准/改代码”测试禁止 subprocess/Task 调用且保持仓库哨兵文件不变。 |
| 18 | 未复现授权绕过。preflight 拒绝 external_source；authorize 在查既有 receipt 前拒绝，API 不接受客户端伪造 provenance。 |
| 19 | 普通 chat/demo 未装配 SDK 的测试通过；全量和原始 Windows 失败逐项比较见下。没有因本轮修改放松原平台隔离。 |
| 20 | 无 migration 冲突。全仓版本 1–19，17 属于授权、18 属于 bounded、19 独属 Feishu；本轮未改已发布 migration，旧 JSON/指纹及重复初始化测试保留。 |
| 21 | 未安装 SDK 的普通启动路径通过。模块只在显式 Feishu 装配调用 require_sdk；缺 SDK 的提示测试与普通 CLI/runtime 测试通过。 |
| 22 | **确认并修复发送线程残留**。真实子进程 timeout/cancel/restart 测试检查 active_children 和已关闭句柄；receiver stop 取消测试确认 pump/process/queue 均清理。真实 SDK 长连接重连仍须 live。 |
| 23 | **确认并修复 outbox 顺序**，详见缺陷 1 的两个红→绿用例。 |
| 24 | 未复现重复创建 failed/budget/busy 通知。run ID/ingress ID source key 唯一，重复 scan/replay 测试；远端回执不确定造成的重复仍按 #6 说明。 |
| 25 | 未发现将未跑 live 写为通过；本轮保留 PENDING，并把第一轮验收明确标为历史记录，避免误用旧测试数和 clean 状态。 |

## 新增回归测试

`tests/test_feishu_audit.py` 共 11 个参数化后用例：未扫描回复前 busy、同 sequence busy、finish_ingress/start 两种异常、发送进程 timeout/cancel 两种路径并重复启动、远端成功后回执提交失败、回复已落库但 turn 未完成、首次 binding/active run 的数据库并发、receiver stop 被取消、SDK 异常与关闭管道同时发生。

红阶段：顺序 2 项、入站异常 2 项全部按预期失败；发送线程取消诊断 1 项、SDK 异常链用例 1 项按预期失败。另有 4 个真实 spawn IPC 探针（成功、失败、管道提前关闭、再次成功）通过，未遗留子进程。绿阶段与全量结果记录如下。

## 测试结果

| 检查 | 总数 | 通过 | 失败 | 错误 | 跳过 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 未修改 upstream 基线 | 1351 | 1108 | 208 | 11 | 24 |
| 本轮全量 pytest | 1454 | 1209 | 207 | 11 | 27 |
| 最终飞书专项 | 104 | 101 | 0 | 0 | 3 |
| 安装 SDK 后的离线契约 | 2 | 2 | 0 | 0 | 0 |
| 从本轮全量提取的 chat API/demo/live fixture | 10 | 6 | 4 | 0 | 0 |

全量退出码 1，耗时 1668.657 秒（约 27.8 分钟）。**全量不绿**。本轮所有失败/错误都已存在于未修改基线，没有新增失败。唯一相对基线的状态变化是 `test_team_room_store.test_room_and_members_persist_across_restart` 从失败变通过；该模块未改，不能归功于本轮修复。上一轮额外的 `test_rework_is_bounded_and_never_self_approves[True]` 超时本轮也通过。比较只归一化 collection-time 随机 UUID，保留函数与其余参数身份。

Windows 的既有问题包括 os.getuid、POSIX 权限、symlink、macOS sandbox-exec、可执行文件/路径/换行与部分任务工作流。本轮 4 个 demo/live fixture 失败均属原基线，5 个 standalone chat API 测试全部通过；普通模式不载入 SDK 的专项测试也通过。没有改为 skip，没有放宽隔离或既有超时。

Ruff `check .` 与 `git diff --check` 通过；Node UI 已实际执行。最终专项的 3 个 skip 是无 SDK 环境中的 2 个契约用例、未启用 live 的 1 项；安装 SDK 后两个契约均通过，仍未触网。11 个新审计用例全部通过。完整统计与差异清单保存在交付目录 `feishu-round2-validation.json`，红/绿证据在 `round2-evidence/`。

本机复用仓库外隔离依赖 runner，清除模型凭据和全部 live 开关；没有改全局 Python。实际命令从工作区根目录运行（`$py` 为捆绑的 Python 3.12.14）：

```powershell
& $py work/dev_check.py outputs/CodeCrew -q --tb=short --junitxml=../../work/round2-full.xml
& $py work/dev_check.py outputs/CodeCrew -q tests/test_feishu_audit.py tests/test_feishu_runtime.py tests/test_feishu_transport.py tests/test_feishu_adapter.py tests/test_feishu_bridge.py tests/test_feishu_outbox.py tests/test_feishu_ui.py tests/test_feishu_sdk.py tests/integration/test_feishu_live.py --tb=short --junitxml=../../work/round2-feishu.xml
& $py work/sdk_check.py outputs/CodeCrew -q tests/test_feishu_sdk.py --tb=short --junitxml=../../work/round2-sdk.xml
& $py work/dev_check.py outputs/CodeCrew ruff
```

长输出重定向至 work/ 对应日志。全量收集发生在最后一个关闭管道用例及其小范围防护之前；最后这项修复由最终完整 104 项飞书专项与单独复验覆盖，未把补充用例虚增进全量数量。

## 仍无法验证的 live 项目与合并门槛

- 真实应用权限、发布范围、bot open_id、长连接连通/重连、DM、群内真实 @、真实 reply API 回执、SDK 实际网络下的停止。
- 原项目支持的 macOS 上完整回归及真实 CLI 隔离；当前 Windows 全量结果不能代替这一门槛。
- 入站 SDK ACK 与 SQLite 之间仍有已记录的内存交接窗口；有限重试不保证最终送达，成功但无回执可能重复。

本轮本地可复现缺陷已修复，未发现新增失败或剩余已确认的代码 blocker。**验收 blocker 仍在**：支持的 macOS 环境尚无本轮全量绿色结果，真实 Feishu 尚未验收。不能把当前结果作为“已完成生产验收”直接合并发布。

## Git 交付范围

本轮基于本地 ae8d47c，分支 feishu-v1；审计结束时先保留未提交 diff，随后按用户请求作独立本地提交，提交标识见 git log；没有 push。13 个文件包含 6 个实现文件、2 个测试文件、5 个文档文件。增量补丁 `CodeCrew-feishu-round2.patch` 适用于第一轮提交，完整源码为 `CodeCrew-feishu-round2.zip`；旧 v1 包保留作历史证据。审计时的 `git diff --stat` 和包哈希另存于交付目录，补丁用上一轮源码快照执行 `git apply --check` 验证。
