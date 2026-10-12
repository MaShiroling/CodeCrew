# 记忆后台同步与运行可观测性（5/5：隔离自动同步验收完成）

2026-10-12 第 1 个子步骤完成配置、策略/停止窗口、只读启动检查；第 2 步现已
实现独立前台 `app.chat.vector_sync --watch`、持续循环与 SIGINT/SIGTERM 停止控制。
第 3 步已增加事务时间元数据与只读诊断，扩展 --status/--run/watch 退出结果。
第 4 步隔离真实进程/信号/60 秒租约恢复已通过；只运行本轮测试库的 child。
第 5 步确定性 Agent 提案/动作→自动缓存→独立 worker→Qdrant→召回/上下文通过，
legacy/BM25 各 7 组检查；不含真实 LLM 提炼或回复派发，不代表用户库已上线。
**没有启用用户真实库的 worker、安装系统服务或增加 chat-serve 启动 hook。**
一次性 `--run`/`--status` 保留；SQLite/旧词项默认及私有配置不变。

## 五个子步骤与范围

| 子步骤 | 当前状态 | 完成目标 |
| --- | --- | --- |
| 1. 后台运行契约 | 已完成；离线配置/预检/停止窗口通过 | 双重 opt-in、有限参数、显式目标/只读预检、停止与故障边界 |
| 2. 持续同步 worker | 实现/离线测试及第 4 步实际进程验证通过 | 独立进程 watch，逐事件停领检查、可中断轮询、复用 outbox/租约/退避 |
| 3. 运行可观测性 | 实现与临时库/假后端回归通过 | 事务时间、目标内积压/年龄/错误类别/最近 delivered；旧时间未知，读取失败不改变投递 |
| 4. 真实进程恢复 | 7 组隔离真实检查及最终审计通过 | 实际 SIGINT/SIGTERM/SIGKILL、不同 PID 重启、自然 60 秒租约接管、停服/默认退避/dead/显式重投 |
| 5. 自动同步端到端 | 隔离写入边界验收通过；两关键词各 7 组 | 确定性提案/动作进入生产 capture；自动缓存/独立进程投递，规范/远端/召回/上下文一致 |

独立前台进程消费已选定 Qdrant 目标，与聊天 HTTP/Agent 进程解耦；不 daemonize，
不替用户安装系统服务/定时任务，不自动管理 Docker。以后若要服务托管须另行接线。
不含云 embedding 生产、模型热切换、自动修复、全局 ANN、多租户权限或持续并发 SLA。

## 1. 配置与双重启用

[`Settings`](../app/config.py)新增四项；[示例配置](../.env.example)默认关闭：

| 环境变量（前缀 CODECREW_） | 默认 | 有界值 |
| --- | --- | --- |
| MEMORY_VECTOR_WORKER_ENABLED | false | 标准 settings 布尔值；仅显式 watch 启动时检查 |
| MEMORY_VECTOR_WORKER_BATCH_SIZE | 32 | 整数 1–200；一轮最多尝试数，不是一次占用 32 个租约 |
| MEMORY_VECTOR_WORKER_POLL_INTERVAL_SECONDS | 2 | 有限数 0.1–60 秒；轮询等待必须可中断 |
| MEMORY_VECTOR_WORKER_SHUTDOWN_GRACE_SECONDS | 10 | 有限数 0.1–60 秒；首次停止请求起的宽限预算 |

数值拒绝 bool、NaN、Infinity 和越界；环境变量合法数字字符串由 settings 解析。
直接构造 `VectorWorkerPolicy` 则严格接收整数 batch 和有限 int/float 时长，不接受字符串。
启用开关为 true **本身无副作用**：不会让 settings/get_settings/chat-serve 运行 worker。
还须显式 watch 请求；请求了但配置未开启，在构造后端/联网前拒绝。

[`requested_vector_worker_policy`](../app/chat/vector_worker_contracts.py)是纯双重开关，
未请求返回 None，连设置字段也不读取。不加载 `.env`、打开库、创建后端或联网。
已请求+已启用只返回不可变策略；watch CLI 负责显式选择/构造并预检目标。

后台投递**不要求** `memory_vector_store=qdrant` 或 `memory_embedding_mode=on`。
聊天可仍用 SQLite，独立 worker 用显式 Qdrant 目标投递已有缓存；不会悄悄切换检索。

本轮通过 context7-mcp 核对 [Pydantic before validators](https://docs.pydantic.dev/latest/concepts/validators/)
和有限浮点校验，按本机 Pydantic 2.13.5/pydantic-settings 2.15.0 验证。
before 拒绝数值 bool，Field 有界/有限数约束；没有对整个 Settings 开启严格模式，
因此不破坏既有环境变量解析。

## 2. 显式目标和只读启动检查

watch 请求必须显式提供既有 database 文件、model fingerprint、dimensions，
使用版本化后端和选定 namespace。复用既有 Qdrant 连接配置；不得猜测聊天数据库、
从 Agent 名称推断模型或根据空库生成向量维度。

`check_vector_worker_target(path, backend)`现由 watch runtime 使用，本轮只在测试临时库
调用过。先验证 Path/VectorModel 和非秘密 SHA-256 target identity，再做：

1. 文件必须存在且为文件；resolve 成绝对路径，不创建目录/空数据库。
2. `Path.as_uri()` 转义路径，`sqlite3.connect(...?mode=ro, uri=True)` + query_only + BEGIN。
3. 核对 migration 27 名称/checksum、必要 outbox/revision 列和已注册 target 的模型/维度。
4. 如果此模型存在缓存 meta，维度必须匹配；没有 meta 不主动生成，允许运行后将
   缺缓存事件保留 waiting。不会逐条加载事实/向量或 claim 事件。
5. **关闭本地连接后**调用版本化后端的新鲜兼容性检查。真实适配器仅一个 metadata
   GET，核对已有 collection/维度/距离/基础索引/sync_revision 索引，不 PUT/POST/初始化。

本地失败不联网；远端失败返回固定 `remote_not_ready`。不透出 provider body、
数据库路径、SQL 参数/事实/向量。通过后返回不可变目标元数据，repr 隐去库路径。
该函数依赖调用方传入既有版本化后端，不是任意第三方对象的安全沙箱。

只读指不改变规范数据、outbox 状态或 schema；不是文件系统零写保证：
[SQLite URI](https://www.sqlite.org/uri.html)规定 mode=ro，
[WAL 文档](https://www.sqlite.org/wal.html)说明只读 WAL 打开仍可能需要 sidecar 文件。
不使用 immutable/nolock 绕过真实并发变化。不自动迁移/注册/回填/审计/修复；
migration 记录和列检查不是全库/trigger 完整性证明。

启动预检只是时点观察，不是可跨请求复用的 ready token，也不能保证之后库/集合
不变。每次 claim/materialize 继续核对目标，远端 apply 保留原新鲜守卫/版本 fence。
启动失败需人工或未来 supervisor 重启；本步不加无限 startup 重试。

## 3. 运行、停止与故障实现

正常路径：显式请求 → 策略 → 只读目标预检 → running → 停止请求 → stopping → stopped。
启动/循环无法处理的内部错误走 failed 并非零退出；不要把内部失败当成空队列。

- 一次只处理一个事件：每次 `run_once(limit=1)` 前检查停止，单轮总数不超过 batch。
  不直接用不可中断的 `run_once(limit=batch)` 完成整个批次才检查停止。
- 轮次之间用可中断等待，避免空队列/失败 busy spin。首次 SIGINT/SIGTERM 或显式
  stop 后不再领取新事件；没有在途调用时立即退出。信号处理器只记录停止请求，
  不做 SQLite/HTTP、ACK 或信号处理器里的清理投递。
- 第一次 stop 创建不可变 `VectorWorkerStopRequest`；使用单调时钟，后续信号不能
  延长预算。它与 outbox 的持久 wall-clock available_at/lease_until 不能混用。
- 对已在途的一次投递等待正常返回/持久化真实结果。超过宽限预算不能宣称 clean
  shutdown；runtime 报告异常退出，未知结果的 claim 不提前清租约，
  不把 processing 直接改为 delivered/pending。
- **宽限期不是硬 HTTP/单事件时限**：一次投递可能有多个 HTTP 调用及 SQLite 等待。
  主线程监督/投递线程隔离实现退出预算；第 4 步已在确定性 checkpoint 验证实际停止、
  强杀/重启、未知 ACK。不保证所有网络操作在 10 秒内完成。
- 复用现有 `SyncPolicy`，默认最多 5 次尝试、租约 60 秒、退避 base=2/cap=60 秒。
  不再增加外层按事件无上限网络重试；超限仍 dead，显式 retry-dead 仍是独立人工操作。
- 缺缓存仍 waiting；新合法缓存产生高版本事件再投递。worker 不补算 embedding，
  不调用 Agent、不绕过 sensitive/来源/生命周期守卫；至少一次投递，不声称 exactly-once。
- 重启通过原租约/token/版本机制接管，旧 worker 延迟 ACK 不能改新 claim；远端
  高版本/墓碑拒绝旧变更。SIGKILL 等未知远端提交不能用本地事务回滚假装撤销。

## 4. 持续同步实现与运行方式

[`vector_worker.py`](../app/chat/vector_worker.py)只由显式 watch 运行，不在模块导入或
chat-serve 时启动线程。主线程安装并最终恢复两种信号 handler；handler 仅向
CPython 可重入 SimpleQueue 记录停止时间/标志，不在 handler 里锁 Event、访问 SQLite
或 HTTP。主线程以 <=50 ms 的 wait 检查，首次 stop 后在 handler 外唤醒轮询。
本机 CPython 3.11.16 验证，当前显式拒绝非 CPython 监督器，未宣称支持所有 Python 实现。

一个 daemon 投递线程负责预检和实际工作，一次只调用 `run_once(limit=1, should_stop=...)`，
每轮不超过 batch，轮次间等待 poll interval，即使有积压也不 busy spin。
outbox 新增可选停领 checkpoint：进入 claim 前、获取 SQLite 写锁后、租约 UPDATE 前
重新检查。默认 None 维持原一次性行为；停止与最终原子 claim 仍可能交错，一次已
接受的在途 claim 会正常完成，不宣称停止请求与数据库事务是原子操作。

投递使用 mode=rw 的既有库连接，不调用 initialize/apply_migrations；运行期间库
文件消失不会创建空库。保留 SQLite 的 foreign_keys/busy_timeout/WAL 设置与短事务。
库路径被其它进程替换、集合修改等仍需逐次目标/版本校验，不是 inode 级身份绑定。

返回 reason 为固定 `stopped` / `startup_failed` / `worker_loop_failed` / `shutdown_timeout`；
仅 stopped 退出 0。attempted 是已返回的一次性尝试次数，不是成功投递数；超时在途
不计入完成尝试。CLI 输出 category/count/clean_shutdown/model calls 和诊断快照，不输出原文/向量/
URL/密钥/路径。`--watch` 不接受 `--initialize-target`、`--retry-dead` 或 `--limit`，
批次使用 worker 配置；与 --run/--status 互斥。

宽限超时返回非 clean，CLI 立即非零退出该独立进程；没有释放未知租约或伪造 ACK。
**helper 返回不是强杀线程**：若嵌入长寿命 Python 进程，daemon 线程仍可能完成真实
ACK，但停止标志阻止继续领任务。不得把 helper 当服务内线程后台任务；正常 watch
只适合独立 CLI 进程。解释器退出/OS 调度/GIL 不构成严格实时 SLA，第 4 步已有隔离 OS 证据。

本轮通过 context7-mcp 核对 Python 线程/信号边界，检索不足处回到官方
[信号/锁限制](https://docs.python.org/3.11/library/signal.html)、
[SimpleQueue 可重入性](https://docs.python.org/3.11/library/queue.html)、
[daemon 线程退出限制](https://docs.python.org/3.11/library/threading.html)。据此不在 handler
里 set Event，也不用无法中断运行线程且退出可能等待的 ThreadPoolExecutor 包装成强杀。

以下只示范启动方式，本轮没有执行；必须自行选择并完成目标的显式准备。

```bash
# 无参数仍只打印计划；不读取私有配置/库、不联网：
.venv/bin/python -m app.chat.vector_sync

# 选定真实库/模型/已注册 namespace 后才可运行，不能原样照抄占位符：
CODECREW_MEMORY_VECTOR_WORKER_ENABLED=true .venv/bin/python -m app.chat.vector_sync --watch \
  --database /absolute/path/to/prepared-chat.sqlite3 \
  --model-fingerprint YOUR_EXISTING_MODEL_FINGERPRINT --dimensions 1024
```

Ctrl+C/SIGTERM 停止；未准备集合/索引/目标会拒绝启动，不自动创建。该命令不切换
聊天检索后端、不算 embedding；waiting 的缓存生产仍需已有合法写入流程。

## 5. 运行诊断：数据、时间与使用方式

[`vector_worker_diagnostics.py`](../app/chat/vector_worker_diagnostics.py)按明确的 target
读取本地一致性快照，使用 mode=ro/query_only，0 远端/embedding/Agent 调用。
仅查询 outbox、目标注册、迁移记录和两列时间元数据，不选择记忆、消息、缓存
向量、lease_token 或 provider payload。原始 SQL/参数不进入返回值。

迁移 **29** 在新的或下一次显式 `StandaloneChatStore.initialize()` 中安装，当前只在
临时库验证，**没有迁移真实聊天库**。独立 --status/--run/--watch 不自动升级。
旧 27/28 数据库可以继续投递/看状态，但时间字段标为不可用/未知。

`standalone_chat_vector_event_times` 每事件只保存 event_id、enqueued_at、delivered_at。
入队 AFTER INSERT 在原事务中用 `strftime('%s','now')` 记录 Unix 秒；只有状态从非
delivered 真正转为 delivered 的 AFTER UPDATE 才记录 delivered_at。租约 token/版本
拒绝的 ACK 无 UPDATE、superseded/waiting 不算 delivered；事务回滚时观察一起回滚。
保留第 27 步原 trigger/版本机制，不对派生时间使用 detached 日志猜测成功。

迁移**不回填历史时间**：旧事件缺 enqueued_at 就未知；旧事件以后正常 delivered
可记录其新成功时间，但入队时间仍未知。available_at 是重试可用时间，不用于年龄。
显式 retry-dead 不重置原入队时间。新事件时间按 SQLite wall clock，停止宽限期仍
单调时钟，两者不混用。时间精度为秒；时钟回拨/观察边界导致未来时间时，计入
skew/unknown，不能默默显示负年龄或当成 0 秒。

本轮 context7-mcp 对触发器细节不充分，补核对 SQLite 官方
[AFTER triggers](https://www.sqlite.org/lang_createtrigger.html) 与
[Unix 时间函数](https://www.sqlite.org/lang_datefunc.html)，并用本机事务回滚测试验证。
触发器的附加写入属于原投递事务，**不是“诊断实现完全零开销”**；不改写旧性能
artifacts、原有 migration checksum 或宣称已测量这次附加成本。
可降级的是诊断**读取**失败；元数据触发器的写入如果失败，原规范/outbox/ACK
事务同样失败回滚。远端若已提交，本地失败不代表远端撤销：watch 报失败并保留
processing/租约供后续接管，而不是伪造 delivered。附加表/trigger 的运维完整性仍需保护。

诊断字段含义：

| 字段 | 含义/限制 |
| --- | --- |
| observed_at_unix | 本次本地快照的观察时间，不是进程 heartbeat |
| target_registered | 本地目标是否存在；不证明远端/worker 正在运行 |
| status_counts | 六种事件状态的当前保留行数 |
| backlog_total | pending+processing+waiting+dead，包含被阻塞的任务，不等于可执行数 |
| ready_pending_count | pending 中 available_at 到期的数量，旧版本可能待 claim 合并 |
| expired_processing_count | processing 中租约到期的数量，不主动接管/重投 |
| current_error_counts | 当前积压事件的固定错误类别计数；未知文本在 SQL 中映射 other，非终生失败次数 |
| oldest_known_backlog_age_seconds | 仅已知且非未来入队时间的最老年龄；部分未知时不能代表整个积压的真实最老值 |
| backlog_age_known_count / unknown_count | 明确时间覆盖率；未知不是 0 秒 |
| backlog_clock_skew_count | 积压中未来/不可信时间数量，包含于 unknown |
| last_delivered_at_unix | 保留事件中最近可确认 delivered 时间；可包含合法墓碑投递，不包含 waiting |
| seconds_since_last_delivery | 距上述时间的秒数，没有可靠记录则 null |
| delivery_time_unknown_count / clock_skew_count | delivered 时间的未知/未来覆盖率 |
| timing_metadata_available | migration 29 记录匹配且时间查询可用；不等于全库/trigger 完整性审计 |

最后成功从数据库读取，重建 worker 实例仍可见；不是进程启动时猜一个“最近成功”。
保留的历史事件若以后被清理，相关历史计数/最近成功可能减少，因此不是终生 counters。
聚合输出固定有界，但查询随目标保留行数增长，未做大队列/并发延迟 SLA。

现有 --status 外增加 diagnostics/diagnostics_status 字段，原 status/attempted 保留；
可在 watch 运行时**另开终端**显式查询，不需再启用 worker 或联网：

```bash
# 只示例，不要原样照抄库路径/模型占位符；本轮未查询真实库：
.venv/bin/python -m app.chat.vector_sync --status \
  --database /absolute/path/to/prepared-chat.sqlite3 \
  --model-fingerprint YOUR_EXISTING_MODEL_FINGERPRINT --dimensions 1024
```

watch 在启动准备后、批次结束及退出时采集快照，退出 JSON 附带最近一次观察。
这是库级快照，不提供跨进程 heartbeat、HTTP metrics endpoint、UI 或逐条日志流。
diagnostics_status 为 observed、not_observed、unavailable 或超时后的 stale；旧快照
须结合 observed_at 判断，不能当作 HTTP 阻塞时的当前结果。诊断读取失败/缺时间
元数据不撤销业务 ACK，不自动修复或迁移，诊断异常只输出固定 unavailable。
实际超时还保留原 shutdown_timeout/非零退出语义；不把诊断缺失计成模型/召回漏检。
CLI 原 status 和 diagnostics 是独立观察，活跃投递时两组计数可能跨时点不同。

## 6. 真实 OS 进程恢复验收

[`check_memory_vector_worker_processes.py`](../evals/check_memory_vector_worker_processes.py)
默认只打印计划；授权 `--live` 才操作一个新建 OwnedDocker 和自有 Popen children。
清理只作用于本轮 handle/容器，保留 SQLite/bind/report/UUID markers，未操作现有 6333。
child cwd 无 `.env`，env 白名单不继承父进程密钥；实际调用原 --watch CLI。
eval-only wrapper 在应用前/远端 ACK 后暂停，并从 supervisor 写停止 marker，不改业务源文件。

[实际报告](../evals/baselines/memory-vector-worker-process-v1-2026-10-12.md)：7/7 组、11
个实际进程，SIGINT/SIGTERM 正常/在途退出、宽限超时、提交前/ACK 后 SIGKILL、不同
PID 重启及停服恢复通过。没有短租约或改时钟/lease_until：原 60 秒自然过期前
重启不抢占，到期后两次尝试完成。停服原 5 次尝试进入 dead，恢复不偷偷无限重投。
最终六个非空目标共 7 个 current 与远端一致，另一个空目标一致；无积压，模型 0/0。
container/children cleanup_complete，用户真实库/私有配置/默认不变。

这不是任意指令级崩溃/断电或持续负载 SLA；gate 是受控 Python checkpoint，不是所有
socket/C 扩展阻塞。14 个工具有效性测试用假 process，不与真实 OS 检查数混算。

```bash
.venv/bin/python -m evals.check_memory_vector_worker_processes # 仅计划
# 需要独立授权 Docker/自有子进程信号操作：
.venv/bin/python -m evals.check_memory_vector_worker_processes --live
```

## 当前验证与下一步

配置、不可变策略/停止窗口、只读 URI、数据库不自动创建、缺 schema/目标/模型
拒绝、连接在 GET 前关闭、已有 pending/processing/waiting/dead 不动、真实 REST
适配器 fake HTTP 仅一个 GET/缺版本索引拒绝等已测试。本轮只读真实库未操作，
没有启动 Docker、调用模型或变更现有服务。

新增 **84 项**参数化/行为测试（不是 84 个实际进程场景）；契约+既有配置测试
**94 passed**。扩大定向含记忆、向量、聊天派发、CLI 与 Settings，**1037 passed**，
两条既有 Starlette/anyio 弃用警告；非全仓测试。Ruff、git diff --check 与 24 条
legacy 来源冻结通过。冻结工具仍提示历史 `app/chat/memory.py` 代码哈希差异，
本步未修改该文件，也没有用此提示替代来源验收。

第 1 步以上 84/94/1037 的历史记录保留。第 2 步新增 38 项：逐事件 checkpoint、
空队列 60 秒等待被停止唤醒、空轮之后新增事件、waiting/有限退避/dead、不自动重投、
假远端提交后 ACK 阻塞/超时租约保留、启动阻塞预算、异常 ACK、信号 mock 恢复和
partial install、只在主线程安装、既有库 mode=rw、不自动创建库及 CLI 双开关/互斥。
这些是临时 SQLite/假后端/受控线程，不是实际 SIGKILL、Qdrant 故障或生产验收。
worker/outbox/契约/配置定向 **183 passed**；扩大定向 **1075 passed**，两条既有
Starlette/anyio 弃用警告，非全仓测试。Ruff/diff/旧来源冻结、CLI help/无参数计划通过。
本步新增 claim checkpoint 会改变 vector_outbox 文件 hash，历史 A/B provenance 按
当时快照保留，不改历史 manifest 来假装仍是相同代码；没有重测或改写历史延迟成绩。

第 3 步新增 **29 项**诊断/迁移/回滚/隐私/目标隔离/重启实例/错误分组/时钟异常/
读取失败不影响投递测试，现有超时测试增加 stale/not_observed 验证。
worker/诊断/outbox/契约/存储/配置定向 **221 passed**，扩大定向 **1104 passed**，
两条既有弃用警告，非全仓。Ruff/diff/旧来源冻结通过；没有启动 Docker/真实 worker，
没有读取私有密钥、调用模型或迁移真实库。历史阶段编号和性能报告不改。

第 4 步新增 14 项工具测试、扩大定向 **1118 passed**（两条既有警告，非全仓），
Ruff/diff/旧来源冻结通过；实际 7/7 与自有资源清理、provenance 核对通过。
第 4 步当时为 **4/5**，以下补充最后一步，不改历史进程恢复证据。
之前 [读路径优化 5/5](memory-qdrant-optimization.md) 与历史性能成绩单独保留。

## 8. 第 5 步：自动写入到后台召回旅程

[验收脚本](../evals/check_memory_vector_worker_journey.py)默认只打印计划；显式 `--live`
才创建本轮 UUID/label/image/mount/loopback 守卫的临时 Qdrant 和独立 watch child。
复用第 4 步进程所有权、干净 env/cwd、有限退出与清理；不改第 4 步脚本。
按 Context7 的 Python 3.11 subprocess 文档核对：communicate 超时不会自动终止子进程，
清理仍仅针对本轮 Popen handle，终止后回收；不是按程序名或进程组强杀。

```bash
.venv/bin/python -m evals.check_memory_vector_worker_journey
.venv/bin/python -m evals.check_memory_vector_worker_journey --live
```

本步人工定义 AgentMemoryProposal/AgentMemoryAction，走生产 capture/来源守卫，
**不手动写 embedding 缓存**：自动 `_index_if_enabled` 调用合成 embedder，再由
SQLite 缓存触发 outbox；父进程不 claim/run_once/apply/retry_dead，独立 child 自动投递。
尚无缓存 meta 时保留真实索引的 eligibility-only 维度流程，不强行注入缓存。
不调用 LLM 或回复 dispatcher、不启动真实聊天室，不再验收模型提炼或回答质量。

legacy/BM25 各检查以下七组：

1. 自动创建、缓存、delivery、纯向量/混合召回、上下文注入；重复提案无新缓存/事件。
2. 显式替换保留沿革；旧记录远端同版本墓碑，新记录进入召回/上下文。
3. 其他房间与当前讨论隔离，当前 Human 的上下文不带自身讨论的记忆。
4. Human 完成证据→Agent 动作关闭；保留原来源及关闭证据，墓碑与活跃召回一致。
5. Human 取消证据同上；重放关闭动作不增加事件。
6. 合成 embedding 故障后规范仍保存，事件 waiting；查询不补缓存/投递。
   显式调用 writer 的 index 重试后自动恢复；**不声称自动重试云 embedding**。
7. worker 停止期间的新写入 attempts=0，store 重建不改缓存/投递状态；不同 PID
   接管队列。旧替换记录和已完成/取消待办不复活，关闭历史仍在。

每次语义断言先证明 legacy/BM25 对 `semanticprobe` 无词项命中，再独立检查
search_scoped 和最终 retrieve/context；不能用词项降级掩盖向量漏同步。
远端 inspect 核对 desired revision 与 active/tombstone；最终双向 audit 非仅检索命中。

最终 [实测记录](../evals/baselines/memory-vector-worker-journey-v1-2026-10-12.md)：
两臂共 14 组、4 个实际 child，各 7 local/current/remote 一致（含墓碑），积压 0；
全部 stopped/exit 0，子进程和自有容器清理 complete，源码 provenance 匹配。
新增 **8 项**离线工具有效性测试；扩大定向 **1126 passed**，两条既有警告，非全仓。
Ruff/diff/24 题 legacy 来源冻结通过；云 embedding/Agent 调用 0/0。
日志中的 indexing RuntimeError 是每臂刻意模拟一次，非真实 provider 故障。

本阶段 **5/5 完成**，只代表以上隔离范围。用户真实库 worker 仍关闭，SQLite/旧词项
默认、私有配置和现有 6333 服务不变；没有系统服务托管、并发负载/集群 SLA、
新盲测或 exactly-once 保证。模型生产缓存仍由聊天 writer 负责，worker 只消费已有缓存。
