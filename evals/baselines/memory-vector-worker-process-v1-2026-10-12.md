# 后台同步第 4 步：真实独立进程恢复验收（2026-10-12，4/5）

本轮 **7/7 组真实检查通过**，11 个自有 Python 子进程及一个隔离 Qdrant 容器已清理。
这是实际 OS 进程/信号/独立重启证据，不再把“新 worker 实例”当作进程恢复。
没有启用用户真实库的 worker、改私有配置/默认后端、安装系统服务或调用模型。

## 原始证据与复跑

目录：`evals/results/memory-vector-worker-process-v1/20261012T082012Z-b8acc93f/`。
`report.json`、各 fixture SQLite、子进程 spec/UUID phase markers、Qdrant bind 数据保留。
报告 complete/matrix_passed/children_cleanup_complete/container_cleanup_complete 均 true；
所有记录的代码 SHA 与本轮文件匹配。没有改写前面阶段的成绩或 provenance。

```bash
.venv/bin/python -m evals.check_memory_vector_worker_processes # 仅计划
# 仅在明确授权隔离 Docker/子进程操作后：
.venv/bin/python -m evals.check_memory_vector_worker_processes --live
```

Python 3.11.16；本机 Qdrant 1.19.0，固定已有 image ID：
`sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`。
自有容器 `codecrew-memory-fault-74d82bd6be7948c292405ec2990febf5` 已移除；没有删 bind/SQLite。
既有 6333 只做只读核对，collection 列表仍为空。

## 实验边界与进程归属

工具复用 OwnedDocker UUID/label/image/bind/loopback 归属检查，不下载镜像、不操作共享
服务。每个 case 单独规范库/namespace，先由 parent 显式初始化/注册测试目标。
向量为手工合成 3-D `(1,0,0)`，不是云模型缓存或真实用户文本；embedding/Agent 调用 0/0。

子进程实际运行 `app.chat.vector_sync.main(--watch...)`，而非假 runtime：
eval-only [`memory_vector_worker_child.py`](../memory_vector_worker_child.py)在进程内包裹
准备完成/应用前/远端 ACK 后的确定性 checkpoint，不改应用源文件。
gate 不在 SQLite 事务里，也不在信号 handler 里做文件 IO；stop marker 从 supervisor
observe 处写入。实际远端 inspect 验证 missing/current，不能只看一个 marker 猜提交结果。

启动使用新会话和 Popen handle，不 shell、不按名字/全组杀进程、不发信号给任意 PID。
仅对本轮记录的仍活跃 Popen 发送信号；结束 communicate/reap。清理有界 terminate→kill
并逐个尝试所有已创建 child，再按归属清理容器。目录/marker nonce 阻止串用旧 gate。
子进程 env 为白名单，不复制父进程 os.environ，不带 Kimi/embedding/Qdrant 密钥。
cwd 为本轮新建 fixture 子目录且拒绝 `.env`，不读取项目私有文件。模型/库显式选择。

生产默认 SyncPolicy **lease=60 秒、max_attempts=5、retry_base=2/cap=60 秒**不变，
没有改 wall clock、直接改 lease_until 或用更短 eval lease 来冒充自然过期。
测试只将 poll interval 设 .1 秒，正常停止 grace=2 秒、超时 case grace=.2 秒；
这不证明默认 grace=10 秒的严格实时上限。子进程及等待均有超时；等待真实租约
期间多个重启 child 独立存活，不把同步阻塞时间误称为 Agent 执行成本。

## 七组实际结果

| 检查 | 实际退出/恢复证据 |
| --- | --- |
| 空闲 SIGINT | 完成测试投递后发送实际 SIGINT，退出 0/stopped，最近 delivered 时间可读 |
| 在途 SIGTERM | 远端已确认时停住，加入下一条 sentinel；supervisor 收到停止后释放 gate，当前事件完成、下一条 attempts=0；另一个进程补齐 sentinel |
| 停服启动拒绝 | Qdrant 实际停服时启动新 child，退出 1/startup_failed、attempted=0，无自动创建/投递 |
| 停服退避与死信 | 活跃 child 对新事件真实失败 5 次进入 dead；服务恢复后仍 dead/attempts=5，显式 parent 重投后 child 自动投递并退出 0 |
| SIGTERM 宽限超时 | 远端已 current、尚未本地 ACK 的 gate 上真实 SIGTERM，退出 1/shutdown_timeout/非 clean；SQL 仍 processing/token/lease，最近成功未知；重启提前等待原租约，到期后第 2 次尝试完成 |
| 提交前 SIGKILL | gate 在 apply 前，远端 inspect=missing，实际 SIGKILL 退出 -9；lease 保留；新 PID 在未过期时不抢占，自然过期后 attempts=2、远端 current |
| ACK 后 SIGKILL | gate 在真实远端 ACK 后/本地 finish 前，inspect=current，实际 SIGKILL 退出 -9；新 PID 到期后重复投递同一版本，attempts=2、远端不漂移，本地 delivered |

宽限超时原 child `attempted=0` 是“尚未返回完整 run_once”的计数，**不是没发生
远端提交**。其 diagnostics_status=stale，旧快照仍可能显示 pending；实际 processing
由独立 SQLite 查询验证。不能用这个旧快照抹掉未知提交/租约状态。
两次 SIGKILL 的旧进程确实被 OS 终止并回收；重启是不同 PID。阶段 2/3 的线程
假后端测试仍作为历史记录，不替换为本轮成绩。

## 最终双向审计

| 隔离目标 | local/current/remote | outbox |
| --- | --- | --- |
| graceful_idle | 1/1/1 | delivered 1 / superseded 1 |
| graceful_inflight | 2/2/2 | delivered 2 / superseded 2 |
| timeout_after_ack | 1/1/1 | delivered 1 / superseded 1 |
| kill_before_remote | 1/1/1 | delivered 1 / superseded 1 |
| kill_after_ack | 1/1/1 | delivered 1 / superseded 1 |
| outage | 1/1/1 | delivered 1 / superseded 1 |
| startup_refusal | 空/空/空，consistent=true | 空 |

总计 7 个当前规范/远端记录一致（startup_refusal 没有事件/点），所有目标无
pending/processing/waiting/dead。非空目标最近 delivered 可读，空目标不造成功时间。
这不是仅靠出站 ACK 的自证：最终是独立维护审计读取规范及真实服务。

## 验证与未覆盖项

新增 **14 项**归属/干净环境/marker/清理/CLI 工具有效性测试，不把假进程测试计为
实际 OS 成绩；扩大定向 **1118 passed**，两条既有 Starlette/anyio 弃用警告，非全仓。
Ruff、diff、24 条旧来源冻结通过；冻结工具的历史 memory.py SHA 差异提示仍保留。
通过 context7-mcp 核对 Python 3.11 Popen 信号/回收/timeout 语义，据此只持有自有 handle，
不用 process-group 杀进程，timeout 后显式回收，不把 wait 超时误当已终止。

本轮覆盖 **POSIX/本机、确定性 checkpoint 上的真实强杀**。不证明任意机器指令
时刻/断电、磁盘损坏、Qdrant 集群、长时间压力或 GIL/stdio 下严格实时 SLA。
门闩是 eval 注入的 Python 等待，不等于所有真实 socket/C 扩展阻塞都可安全中断。
仍为至少一次投递，不是 exactly-once；不自动重投 dead，也不回滚未知远端提交。
本阶段 **4/5 完成**；下一步是新增/修改/完成/取消后的自动同步与召回端到端旅程。
