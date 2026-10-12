# 真实验收库 worker：实际启停 v1

2026-10-12，第 3/5 步；空队列进程启停/诊断验收，不是模型质量、投递或并发实验。

准备 manifest：`.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json`。
实际报告：该目标 `worker-checks/20261012T095254Z-7d300cbb/report.json`。
运行 `.venv/bin/python -m scripts.run_memory_live_worker --check --manifest <上述绝对路径>`，
经授权启动两个真实子进程，观察后分别发出 SIGTERM/SIGINT 请求，只针对本轮 handle。
两次启动按顺序执行，不是两个 worker 同时工作，也不是多 worker 并发测试。

## 操作与证据

入口复验 private root/路径、完整 manifest、override allowlist、独占 namespace、
typed model、重新计算 collection/target identity；不创建/迁移/初始化新目标。
--status 是只读数据库诊断，不是运行进程 heartbeat；--watch 显式前台运行，复用
生产 watch/signal/runtime。进程内启用，embedding off，无聊天/模型适配器。
每次使用实际默认 batch32/poll2s/grace10s，与既有独立目标一致。

| child PID | 停止信号 | OS 退出 | runtime 结果 | 最终诊断 |
| --- | --- | --- | --- | --- |
| 8667 | SIGTERM | 0 | stopped / clean_shutdown=true / attempted=0 | observed，backlog 0 |
| 8674 | SIGINT | 0 | stopped / clean_shutdown=true / attempted=0 | observed，backlog 0 |

每次在预检收据后观察实际 child 存活至少 4.2 秒，再发送停止请求；预检收据不是
delivery ready 的原子证明，只有最终 observed 诊断证明 runtime 已建立 worker。
只读诊断没有伪造成功：last_delivered_at_unix=null。没有入队/投递，不能讲成同步性能收益。
before/after inventory 完全相同：schema29、target1，其余 rooms/messages/memories/cache/outbox 均0。

check complete/checks_passed/children_cleanup_complete=true，worker_running_after_check=false。
源码与原 prepare.json SHA-256 均匹配；旧 prepare.json 是历史快照，不改它的 worker_started。
检查结束时没有常驻 worker，第 4 步真实联调再使用同一 manifest 启动。

子进程新 cwd、白名单 env，仅 PATH/PYTHONPATH/PYTHONUNBUFFERED；没有继承父密钥或
CODECREW target，未覆盖 HOME/CODEX_HOME。显式读取项目 .env Settings，只有 Qdrant key
被后端消费，不向日志/参数/manifest 复制密钥，无 embedding/Agent 调用（0/0）。
日志目录 0700、stdout/stderr/report 为0600，整个 .codecrew Git 忽略；留存原始收据与最终结果。

进程回收限本轮 Popen handle，有限 wait/terminate/必要时 kill/reap，不操作未知 PID、
进程组或名称。Context7 的 Python 3.11 subprocess 文档用于核对：超时不会自动杀进程。
本轮正常停止无需强杀；失败分支由离线控制测试覆盖，不冒称实际 timeout/kill 重测。

## 回归与未覆盖项

19 项新增控制测试使用临时 SQLite/假后端/假 OS handle，涵盖 target 篡改拒绝、
无副作用计划、按进程启用、干净环境、无密钥输出、有限清理、信号恢复和早退不计通过。
假进程不是上述实际 OS 信号证据。与前两步工具合计 **44 passed**。
扩大定向 **1170 passed**，两条既有 Starlette/anyio 警告，非全仓；Ruff/diff 通过。
旧24题有序来源冻结不变，TP/FP/FN=10/0/2；历史 memory.py 哈希提示保留。

Fake 8772 /health 正常，旧默认库 schema19/8 rooms/36 messages、Fake库 schema25/
2 rooms/20 messages/3 memories 均不变；全局 SQLite/legacy/worker false，未改 .env。
没有停止容器、移除集合/持久卷、导入数据、调用云端或安装托管服务。
Kimi/embedding 当前凭据有效性、真实聊天闭环与模型质量待第4步；重启旅程待第5步。
