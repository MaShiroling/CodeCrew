# 真实模型记忆运行验收（5/5：组合验收与同库重开完成）

2026-10-13 当前结论：第11/12条真实补验及两个不同PID的同库只读重开已通过。
[实际证据与边界](../evals/baselines/memory-persistent-restart-v1-2026-10-13.md)、
[重启检查入口](memory-persistent-restart.md)。五项完成的口径是前序真实运行＋单条
恢复＋针对性补验＋同库重开，不是新的12条全通过或生产/灾备验收。

当前不要重复下方历史 V2 续跑命令：用户已运行后半程，累计 Kimi starts=12，
最后查询误写偏好而停止，并发现已取消状态回复的误判。V3 修复会使旧代码指纹
校验失败，且原运行已无 Kimi start 预算；没有开放强制重跑或重置预算入口。
原报告保留，误写记忆已在单独授权后撤销、积压归零并审计通过；针对性回复补验
已由用户运行，实际回复经Codex核对，无误写或隔离泄漏。原补验报告仍保留
full_lifecycle_passed=false、pending_human_review，未伪造用户签审或改写原失败成绩。

2026-10-12。本阶段不同于已完成的后台同步隔离验收 5/5；本页记录真实使用五步，
不把 Fake 页面、已有隔离报告或“配置能读取密钥”当作真实模型服务验收。

## 五个子步骤

| 子步骤 | 当前状态 | 范围 |
| --- | --- | --- |
| 1. 确认目标与备份 | 已完成；用户选择新建独立、持久化的真实验收库 | 明确真实库、服务、Qdrant/embedding 设置，保存恢复点 |
| 2. 准备同步条件 | 已完成；新库/专属集合/目标注册/只读预检及备份通过 | 历史同指纹真实缓存确认 1024 维；新目标缓存为空，不导入历史数据 |
| 3. 启动真实 worker | 已完成；既有目标两次实际启动/诊断/SIGTERM、SIGINT 停止；当前已停止 | 显式前台 watch、有限停止及回收，不默认安装开机服务 |
| 4. 真实聊天验收 | 已按前10项＋恢复＋两条真实补验收尾；旧V1/V2失败报告不改 | 单 Kimi + 实际 embedding/worker，新增/召回/纠正/完成/取消/隔离；不是重跑12项全通过 |
| 5. 重启与收尾 | 已完成；两个新OS读进程同库重开，1活跃/4退休、双向audit一致，模型0/0 | 持久化、无复活、只读audit、运行说明；未重启容器/在用服务，非灾备恢复 |

以下按时间保留前序过程记录；其中3/5等数字是当时状态，以本页顶部结论为准。

## 第 1 步实际发现

本机 Settings 的非秘密白名单核对（未修改 `.env`）：

- database：`/Users/mashiro/CodeCrew/codecrew.db`。
- lexical=legacy、vector_store=sqlite；worker_enabled=false。
- embedding_mode=on，model=text-embedding-v4，revision=pilot-2026-10，密钥存在。
  HTTPS 百炼北京业务空间的兼容 API 根地址格式已读取；未请求服务商，不证明密钥有效。
- Qdrant：`http://127.0.0.1:6333`，collection_prefix=codecrew_memory，无配置 API key。
  第 1 步只读 GET /collections 成功，当时集合列表为空，未创建集合。

运行服务 `127.0.0.1:8772` /health 成功，本轮只读取房间 ID（不取消息正文）。
它的两个房间 ID 与旧 Fake 浏览器验收库完全一致，与配置的 codecrew.db 八个房间不同。
因此**不能把当前浏览器页说成真实模型/embedding 已运行**，更不能将当前 .env
当作该长期运行进程已经加载的配置。未重启、停止或替换该服务。

| 已识别库 | 类型/证据 | 备份时统计 |
| --- | --- | --- |
| `/Users/mashiro/CodeCrew/codecrew.db` | 当前默认配置目标，尚无记忆 schema | schema 19；8 rooms、36 messages |
| `/private/tmp/codecrew-memory-browser-SOfQWd/chat.sqlite3` | 既有 Fake 演示记录，房间 ID 与 8772 一致 | schema 25；2 rooms、20 messages、3 memories、0 embeddings；无 outbox |

没有调用 Store.initialize、migrate、embedding、Agent 或 worker；上述源数据库
均以 URI mode=ro/query_only 读取。读取 WAL 可能涉及 sidecar，不承诺文件系统零写。
没有扫描用户环境/进程参数、打印密钥或把完整 Settings/.env 存入报告。
本步尚无当前模型维度/缓存可用性证明，留给第 2 步。

## 已完成的一致性备份

新增 [备份工具](../scripts/backup_memory_live_database.py)，使用 SQLite Online Backup API，
只读源连接，包含已提交 WAL；不会用裸文件复制代替在线一致性备份。
按 Context7 的 Python 3.11 sqlite3 文档核对 Connection.backup，分批复制及 progress
异常中止；预算约束备份循环，不声称整个工具是严格实时 SLA。
工具默认只读汇总，只有显式 --backup 才创建新私有目录；不会迁移、启动服务或加载 Settings。

```bash
.venv/bin/python -m scripts.backup_memory_live_database \
  --database /Users/mashiro/CodeCrew/codecrew.db
# 显式备份（每次新 UUID/时间目录，不覆盖旧备份）：
.venv/bin/python -m scripts.backup_memory_live_database \
  --database /Users/mashiro/CodeCrew/codecrew.db --backup
```

实际备份位于 `.codecrew/backups/memory-live-target-v1/`，整个 .codecrew Git 忽略。
运行目录 0700、备份和 manifest 0600。Manifest 只含路径、时间、checksum 和汇总，
不含正文/向量/密钥；**备份数据库本身包含原数据，仍须按私有资料保护**。

- 配置库：`20261012T092228Z-7abf7df8/database.sqlite3`，同目录 manifest.json。
- Fake 库：`20261012T092228Z-8b006540/database.sqlite3`，同目录 manifest.json。

两份 complete=true、integrity_check=ok、foreign_key_check=ok，SHA-256 留档。
失败时保留 partial.sqlite3 且 complete=false，不冒充可恢复备份；不删除源库/证据。
未执行覆盖源库的恢复演练，不宣称在线恢复已验收。
新增 13 项临时 SQLite 测试通过（含 WAL、只读写入拒绝、权限、不覆盖、超时、不泄漏）；
Ruff/diff 检查通过，未跑全仓回归。真实模型/云 embedding 调用 0/0。

## 已确认的目标选择与下一步

2026-10-12 用户选择第 1 项，以下保留两种方案及取舍：

1. **已选：新建独立、持久化的真实验收库**。沿用当前私有凭据配置，但隔离数据库、
   Qdrant namespace 与端口，不迁移已有聊天历史，不动 Fake 服务。
2. 使用现有 codecrew.db。先在备份副本验证 schema 19→当前迁移；真正迁移源库、
   切换运行配置/检索后端前须再次明确授权，不能凭本步开始就直接执行。

第 1 步选择确认后为 1/5；用户随后要求执行第 2 步，准备结果如下。

## 第 2 步：新持久目标准备完成（未运行）

[准备工具](../scripts/prepare_memory_live_target.py)默认只打印计划，不读 Settings 或分配资源；
显式 `--prepare` 才读取当前凭据但不打印/复制密钥，创建新私有目标。

```bash
.venv/bin/python -m scripts.prepare_memory_live_target
.venv/bin/python -m scripts.prepare_memory_live_target --prepare
```

**准备命令每次创建新 UUID 目标，不是启动/恢复现有目标的命令。** 下一步应使用
已有 prepare.json，不重复 prepare，也不把新的库误当作原验收库。

本轮实际目标：

- 持久目录：`/Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a`。
- 数据库：该目录 `chat.sqlite3`；manifest：`prepare.json`，phase=ready_not_started、complete=true。
- Qdrant：原有本机 6333 服务上的新专属 namespace，prefix=`codecrew_live_real_9485ed6d69e14fbd`。
  模型隔离且有 `_sync_v1` 后缀；完整集合名/目标 SHA-256 留在私有 manifest。
- 新实例运行模式：BM25 + Qdrant + embedding on；只存无密钥的 runtime_overrides，
  workspace/runtime 也指向新目录。**未加载到运行服务、未修改全局 .env。**
- worker/chat_started 均 false，无模型调用，未安装系统服务或改 Fake 页面。

源证据为此前真实缓存 `evals/results/memory-fts-ab-v1/20261011T113351Z-8f218a82/embedding-cache.json`。
复用既有 SharedEmbeddings.load 校验内容摘要、来源/complete、冻结题库身份、34 个
向量的有限数与统一维度；指纹和 model/revision 与当前配置一致，历史维度 **1024**。
只是推导集合所需结构，不把评测向量复制进新库，也不把合成值冒充真实缓存。
本步 0 新 embedding/Agent，**当前密钥/供应商模型是否仍可调用尚未验证**，留给真实调用阶段。

新数据库 schema 29，FTS5/BM25 usable；rooms/messages/memories/embeddings/outbox 全 0，
注册目标 1。远端 fresh GET 显示 1024/Cosine、全部 9 项 payload 索引（含 sync_revision）、
green、points_count=0。check_vector_worker_target 关闭只读 SQLite 快照后 fresh GET 通过。
无 claims/投递/历史回填/重投；空缓存是预期状态，下一步 worker 不能替 writer 调云模型。

读取 Docker 挂载确认现有 Qdrant 使用 `codecrew-memory_qdrant-data` 命名卷、
挂至 `/qdrant/storage`；本步没有新建/停止/重启容器或删除任何集合/卷。
新 SQLite 和集合有持久存储依据，但没有以停容器/重启来冒充持久恢复验收（该项在第 5 步）。
按 Context7 核对 Qdrant 1.19.0 同一 collection 的一致维度要求，复用现有适配器的模型隔离命名。

新库另外在线备份至目标内 `backups/20261012T093545Z-6fdc340c/`，完整性/外键及哈希通过。
重开库统计与 manifest 一致；目录 0700、库/manifest 0600，整个目标 Git 忽略。
同时补充 SQLite WAL/SHM 忽略规则，避免 sidecar 含数据时被误提交；未删除 sidecar。

12 项新离线工具测试 + 13 项既有备份测试 **25 passed**；扩大定向 **1151 passed**，
两条既有 Starlette/anyio 警告，非全仓；Ruff/diff 与旧 24 题来源冻结通过。
实际记录见[准备证据](../evals/baselines/memory-live-target-preparation-v1-2026-10-12.md)。
旧 codecrew.db 仍 schema 19/8 rooms/36 messages，Fake 库仍 schema 25/2 rooms/20 messages/3 memories。
全局仍 SQLite/legacy、worker false，不把新实例的 BM25/Qdrant 意图说成全局已切换。

第 2 步完成时为 2/5；用户随后要求执行第 3 步，结果如下。

## 第 3 步：既有目标 worker 启动、诊断与停止

[运行入口](../scripts/run_memory_live_worker.py)使用既有 manifest，不重复 prepare。
无参数仍只打印计划；三个显式动作互斥且必须选择 --manifest：

```bash
memory_live_manifest="/Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json"
.venv/bin/python -m scripts.run_memory_live_worker
# 仅 SQLite 诊断，不是进程 heartbeat，也不请求 Qdrant：
.venv/bin/python -m scripts.run_memory_live_worker --status --manifest "$memory_live_manifest"
# 两次实际启动、分别 SIGTERM/SIGINT 停止，结束后不常驻：
.venv/bin/python -m scripts.run_memory_live_worker --check --manifest "$memory_live_manifest"
# 手动前台持续运行；本轮 check 结束没有留下此类进程：
.venv/bin/python -m scripts.run_memory_live_worker --watch --manifest "$memory_live_manifest"
```

最后的 --watch 应在需要时手动执行；Ctrl+C 请求停止。它不安装服务/自启/重启策略，
不创建 Docker、collection、schema/target，不 backfill/redrive，不调用 embedding/Agent。
目标必须在私有持久根下，路径/数据库/allowlist/namespace/model/collection/target_id 一致；
数据库 symlink 指向其他文件、额外覆盖密钥字段、旧 prefix 或不完整 manifest 都拒绝。
这些是可信本机操作防误用守卫，不是攻击者可写整个目录时的签名认证机制。

进程内显式 enable + watch 请求才运行；worker 选项为默认 batch 32/poll 2s/grace 10s。
这三个值在该运行入口中固定，不覆盖原 `.env`；运行 embedding mode 强制 off，不构造
云客户端。底层仍使用 watch_vector_worker/现有信号与租约守卫，不加生产 ready hook。
子进程环境只有 PATH/PYTHONPATH/PYTHONUNBUFFERED，独立新 cwd，不继承父 CODECREW_* 或密钥；
明确从项目既有 .env 读取 Settings，但仅消费 Qdrant 凭据，未打印或复制凭据到记录/参数。

--check 仅适用于尚无记忆/outbox 的验收库；不会对有待处理工作做空队列启停验收。
父进程只做预检、只读诊断、进程控制；--watch 子进程实际使用现有运行循环。
启动时 preflight_passed 收据**不是 delivery ready 证明**；父进程观察 child 存活至少
4.2 秒，再请求停止，最终 reason=stopped、diagnostics_status=observed 才判通过。
时间窗口不等于严格保证恰好执行了两轮轮询，也不是持续服务 SLA。
按 Context7 核对 Python 3.11 Popen：仅本轮 handle 发信号/回收；超时不自动杀 child，
失败清理另做有限 terminate→必要时 kill→reap，不按程序名/旧 PID 文件/进程组操作。

实际报告位于该目标 `worker-checks/20261012T095254Z-7d300cbb/report.json`：

| 实际 child | 停止请求 | 结果 |
| --- | --- | --- |
| PID 8667 | SIGTERM | exit 0/stopped，attempted 0，observed 诊断、积压 0 |
| PID 8674 | SIGINT | exit 0/stopped，attempted 0，observed 诊断、积压 0 |

complete/checks_passed/children_cleanup_complete 全 true，worker_running_after_check=false，
模型 0/0；源码与准备 manifest SHA-256 复验匹配。准备 manifest 保留第 2 步历史快照，
worker_started=false 表示当时未启动，不用改写旧记录来表达本轮当前状态。
新库前后统计相同，最近 delivered 时间为 null（没有投递，不伪造成功）。
现有 Fake /health 正常，旧两库 schema/数量和全局 SQLite/legacy/worker false 不变。
本轮没有暂停/重启 Qdrant，准备好的持久库/集合/备份全部保留。

19 项新工具控制测试，真实运行三步相关工具共 **44 passed**；扩大定向 **1170 passed**，
两条既有警告、非全仓；Ruff/diff/旧来源冻结通过。
[实际启停记录与限制](../evals/baselines/memory-live-worker-start-stop-v1-2026-10-12.md)。
当前 **3/5**；下一步第 4 步经确认调用额度后，在此同一目标运行 Kimi + 真实 embedding
聊天生命周期验收，并同时运行前台 worker。本步不证明真实模型写入、云凭据有效或实际投递质量。

## 第 4 步工具准备与首轮真实运行

[真实持久旅程入口](../evals/run_memory_persistent_live.py)已完成，不新增 HTTP 服务或浏览器
动作。使用现有生产 service/dispatcher、单月见/Kimi、一回合讨论上限；其他角色不调用
Codex/DeepSeek，提案/动作须通过原自动写入/关闭守卫，父进程不手动保存/投递。
它是已定义的合成生命周期验收，不是新盲测，也不把单 Agent 调度说成多 Agent 或浏览器验证。

计划为 12 条 Human 步骤：决定写入→跨讨论问答→纠正→再次问答→开发待办写入/召回/
完成→个人待办写入/召回/取消→已关闭状态问答→跨房间未知决定问答。
检查生产 prompt 的实际记忆、直接向量分支、远端 revision/墓碑及最终双向 audit。
已关闭状态回执若由服务端生成则不启动 Kimi，不能为凑数追加模型回合。
答案断言只是指定词面/数值检查，记录 semantic_review=pending_human_review，不是通用语义裁判。

```bash
# 无副作用计划，不读配置/密钥/运行目标：
.venv/bin/python -m evals.run_memory_persistent_live
# 用户首轮实际使用的运行方式；已有数据时不要直接重复：
.venv/bin/python -m evals.run_memory_persistent_live --live \
  --manifest /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json
# 可选先做前两条冒烟：增加 --smoke（不是完整第4步）。
```

完整模式 cap：最多 **12 个 Kimi start**、**40 次 embedding 请求尝试**，失败请求也
计入 embedding 预算；Kimi 每回合最多 3 个内部步骤。start/steps 不等于供应商实际
LLM请求数或 token/费用硬上限，不能据此保证精确账单。冒烟上限为 2 starts/10 embeddings。
Fresh 目标预检、指纹和本机 Kimi CLI/密钥检查后才开始；百炼先作一条合成维度探测，
计入请求上限，成功才能将当前 embedding 凭据验证标为通过。
正常回合未完成或 embedding 失败，不当作记忆漏召回；失败即停止，未自动付费重试。
不带 `--resume-from` 的首次入口要求库尚无聊天记录；已有数据不能盲跑覆盖。
现已支持严格校验后的续跑，见下方 V2 说明，不清空库、不重复支付已完成前缀。

本机 Kimi CLI 和 `.env` 的 KIMI_MODEL_API_KEY **存在**（只检查存在，不展示内容），
读取密钥传入既有受限 Kimi adapter；没有启动 CLI 或证明其凭据有效。
需要输入替代密钥时仅 `--prompt-for-kimi-key` 隐藏终端输入，不在聊天/参数/报告中写密钥。
所有调用和 worker 都需显式 --live；全局 `.env` 和旧 Fake 服务不变。

发现第 2 步的 workspace/runtime 占位目录位于 Git 仓库内，不能用于受限只读 Agent。
本步**不放宽工作区边界、不改旧 prepare.json**，运行时另选持久私有执行根：
`/Users/mashiro/.codecrew/memory-live-runtime/<既有目标ID>/<本轮运行ID>/`。
真正创建该目录时须经文件系统授权；当前未创建。数据库仍在原已准备的持久位置。
每次 Agent 执行遵守既有非 Git/READ_ONLY/Seatbelt 边界，成功清理由原 workspace manager 处理，
失败留证据。实际脚本中的新 `live-runs` 报告与合成回复留在现有目标的 Git 忽略私有目录。

使用 Context7 核对 Python 3.11 asyncio.wait_for 取消行为；shutdown 在 finally 中 await，
Popen 创建采用 shield 保留取消交错下的子进程 handle，再由最终路径停止/回收。
不把 await 超时当作远端任务已确定终止。当前离线新测试 **12 passed**，Ruff/diff 通过；
扩大定向 **1182 passed**（两条既有警告、非全仓）；假适配器/远端结果不是实际
Kimi/百炼/OS 证据。以上是运行前工具准备记录；实际调用结果见下方，进度仍 **3/5**。

### 用户首轮实际结果：第 6 步停止，原始失败保留

用户运行目录 `live-runs/20261012T105311Z-d0aa397b/report.json`，complete=false，
failure_category=direct_semantic_target_missing。已启动 Kimi 6 次、embedding 尝试 12 次，
embedding_errors=0，实际维度探测通过；不是额度上限/鉴权失败。
前五步新增决定、召回、替换、召回新版、开发待办创建均 passed=true。
第六步“这次发布前还有什么未完成的工作？”正常回合 succeeded，实际 prompt 含待办，
回复依据记录说明该待办仍开启；随后直接 vector branch 未返回目标而触发额外断言。
因此不能把此例描述成混合召回漏记/未完成 Agent 回合，也不能标为全部通过。

本轮 worker exit0/stopped、observed 诊断、backlog0，清理完成。后续完成/取消/个人房间
及最终双向 audit 尚未执行，不能以此说第4步或整条生命周期已完成。
原 report 和源码/prepare 哈希均匹配，原结果不改写。

额外复核只用本地缓存和本机 Qdrant，无新 Kimi/百炼请求：三条 remote mutation 均 current
（旧决定 tombstone rev4、新决定 rev2、待办 rev2），活跃缓存都是1024维；
BM25 命中待办，用待办自身缓存向量远端 scoped search 命中且 similarity=1.0。
这些排除了当前缓存/远端版本失配，不能还原失败瞬间的查询向量/相似度。
原工具没保存该查询向量或分数，低于0.65只是可能原因，不能给出具体分数或断言阈值根因。

原评测同时要求混合 prompt 命中和每条查询纯向量单独命中，是更强的交集条件；
本例 BM25 已补回目标，混合链路并未失败。拟将业务混合验收与纯向量诊断分栏，
保留原语义分支失败；不降低阈值、不换题或改写旧 report 来制造通过。
以上为首轮 V1 诊断时的状态；后续 V2 工具准备见下方。没有 `--resume-from` 的入口
仍会拒绝已有数据；不要删除库或盲重复 `--live`。
本轮只诊断/记录，未修改判定脚本、追加调用、迁移或清空目标。
[首轮实际记录](../evals/baselines/memory-persistent-live-partial-v1-2026-10-12.md)。

### V2：混合验收与向量诊断分离，严格断点续跑（历史留档）

[实现与验证记录](../evals/baselines/memory-persistent-resume-v2-2026-10-12.md)。
业务验收仍检查实际生产 prompt 命中目标、指定回复、自动写入/纠正/完成/取消、
房间/讨论隔离、远端版本/墓碑和最终 audit；纯向量单支未命中单独记录，不再将
BM25 已补回的混合成功判为失败。题目、生产检索与 0.65 阈值未改变。
新查询记录返回的 memory ID/相似度、阈值和 embedding 尝试数；未返回目标时分数
保持未知。embedding/传输失败仍停止，不能当作普通 miss 隐去。

`--check-resume` 只读取本地配置、原报告和 SQLite：不请求远端/模型、不启动 worker、
不提示密钥、不新建运行报告。V2 开发时曾在该真实目标检查通过：复用 6 条 Human 步骤，
下一条第 7 步，历史 Kimi/embedding 用量 6/12，剩余上限 6/28，新调用 0。

```bash
# V2 历史检查命令；当前状态/代码已变化，不是新的续跑指令：
.venv/bin/python -m evals.run_memory_persistent_live --check-resume \
  --manifest /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json \
  --resume-from /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/live-runs/20261012T105311Z-d0aa397b

# 用户已经执行过的 V2 付费后半程命令，当前不要重复：
.venv/bin/python -m evals.run_memory_persistent_live --live \
  --manifest /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json \
  --resume-from /Users/mashiro/CodeCrew/.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/live-runs/20261012T105311Z-d0aa397b
```

续跑写入新私有运行目录，保留父报告路径/哈希和累计用量；原失败报告不覆盖。
只有本次已审阅且哈希固定的 V1 报告允许迁移：在新报告明确标记第 6 步按已保存
成功回合/混合 prompt 复核，保留 `original_v1_passed=false` 和历史纯向量 miss，
不重新调用模型、不伪造旧查询分数，也不将原报告改称全通过。
其他 V1 报告拒绝自动迁移；V2 必须题目/阈值/评测与生产代码一致、前缀全部成功、
worker 已干净停止，且完整 SQLite 数据及 schema 快照一致。另核对来源/回复、
历史上下文、记忆内容/替换/关闭链、接受提案/动作、房间集合、turn/session 计数、
outbox 状态和模型调用预算。失败回合、未清理 worker、未完成步骤不自动重试。

累计上限仍 **12 Kimi starts / 40 embedding 尝试**，不是追加一套 12/40；
新的维度探测和诊断查询也消耗剩余额度。每次请求前持久化计数预留。
最终完成条件同时要求 audit 完整且一致、积压清空、无失败、worker 正常退出并有
干净停止回执；仅旅程成功不能绕过审计错误标成完成。

进程锁采用本地非阻塞 advisory flock，拒绝同时运行的本工具并拒绝锁文件软链接；
不按 PID 文件杀进程。它不能阻止任意其他程序写库，请勿同时编辑该验收库或启动
其他 worker。V1 当时没有完整库快照：迁移能核对原证据和当前规范状态，但不能
证明历史缓存每个字节从未变化；V2 从本次验证后建立完整快照。不把本地使用量
记录当作供应商账单证明，答案语义仍待人工复核。

新增49项续跑控制测试，与原持久旅程合计 **61 passed**；扩大定向 **1231 passed**
（两条既有依赖警告，非全仓）。Ruff/diff 与24条旧召回有序来源冻结通过，模型0/0。
以上为 V2 运行前的离线/只读准备记录，不能当作真实通过；后半程实际失败与 V3 修复见下方。

### 当前：V2 后半程失败留档，V3 防误写/状态回复修复

[失败与离线修复记录](../evals/baselines/memory-persistent-guard-regression-v3-2026-10-12.md)。
用户实际后半程报告为 `live-runs/20261012T123657Z-c4c65c5c/report.json`。
12 条正常回合均 succeeded，但最后 `other_room_isolation` 没通过：Agent 将本轮回答
要求“没有记录就说明未知”提议成 preference，生产边界接受，评测拒绝查询新增记忆。
终端留下的 category 是泛化的 acceptance_checkpoint_failed；已用现存提案/来源/库状态
和断言顺序定位到误写，不将它归因于额度、向量 miss 或 Agent incomplete。

第11步 closed_personal_query 原报告 passed=true 也不能作为真实语义通过：回复否认
有取消记录，仅碰巧出现“已完成还是已取消”而通过关键词检查。实际待办已取消；
原句“还在未完成待办里吗”未被原状态查询正则覆盖。完成/取消本身正确落库；
个人召回纯向量命中 similarity=0.7055199（阈值0.65），不是另一条向量失败。
本次新增 Kimi/embedding=6/7，累计12/19、embedding_errors=0，worker exit0/stopped、
clean shutdown，停止时存在误写偏好的2条 pending；最终双向 audit 未执行。

V3 只改变未来处理和判定，不清洗原成绩：

- 自动 preference 须有明确个人习惯、以后/默认等持续意图，或房间范围内明确要求；
  查询、未决想法和本轮/本次要求保守拒绝。检查提议周边完整句子，不能靠截掉疑问
  标记/一次性前缀获得通过；独立句子的明确长期偏好仍可保留。
- 扩展“还在未完成/未关闭待办（清单/列表）里吗”的服务端状态路径，仍只匹配同房间
  本地 Human 的唯一相关目标；通用待办/状态/预约词不计作目标证据。条件/操作请求
  不触发已关闭回执，歧义仍请指定目标，关闭记录不进入普通召回。
- closed_personal_query 要求实际已取消记录、精确服务端回执、turn 无 Agent session，
  并记录 sqlite_status_receipt 来源；仅包含“取消”不够。安全失败分类单独留档。
  评测版本变为 persistent-hybrid-v3，禁止按新标准悄悄改写旧 V2 passed 字段。

新增48项离线控制与现存失败文本回放；完整临时旅程拒绝原误写提案、状态问答
不启动 Agent、无新增个人活跃记忆/同步积压、终态 Fake 远端 audit 一致。
扩大定向 **1279 passed**（两条既有依赖警告，非全仓）；旧24条有序召回冻结不变。
实际报告全文的只读复核：原提案现在拒绝，原第11回复被新判据拒绝，原数据库
数据/schema 快照仍与报告一致；新模型调用0/0。Fake/离线修复通过不代表真实重验通过。

真实库里那条误写偏好仍存在，本轮没有撤销它、投递 pending、启动 worker 或补写 audit。
下一步需明确授权后，仅撤销该误写派生记忆并处理同目标同步/无模型审计，保持原失败
报告和来源消息不变。真实小范围复验需另定目标及额度，不能继续旧12/40预算或清空库。
当前真实阶段仍 **3/5**，不能因为第12条消息已有成功回复就标为完整生命周期完成。

### 后续：已授权的单条真实恢复完成，积压清空

[真实恢复证据](../evals/baselines/memory-live-recovery-v1-2026-10-12.md)。
用户明确授权后，先保留原状态在线备份，再通过生产撤销接口将该误写记录退休，
不删原消息/记忆行/候选，也不改其他规范记忆。清除该条派生cache/FTS后，
只做1次有界投递发送rev4墓碑，既有指针自然supersede，不手动写ACK或强制修复。
真实outbox积压0，delivered8/superseded10；双向audit本地/远端各5条、current5、
findings0，完整且一致。Agent/embedding调用0/0，未启动常驻worker、未改配置或旧两库。
恢复report位于 `recovery-runs/20261012T141749Z-6e39a1bc/report.json`，complete=true
但live_acceptance_passed=false；原两份失败报告和prepare哈希保持不变。

该恢复不是可反复运行的清库命令：固定旧报告/完整库快照现在已变化，再次apply会拒绝。
如果需要恢复备份，须另确认本机SQLite与远端墓碑的联合恢复方案，不能直接覆盖库导致
旧派生记忆复活。当前第4项剩第11/12两条针对性补验，第5项重启验收未开始，仍3/5。

### 下一入口：只补第11/12条，原预算/失败报告不重置

[使用说明和确切命令](memory-persistent-followup.md)、
[离线准备记录](../evals/baselines/memory-persistent-followup-entry-v1-2026-10-12.md)。
只增加两条查询和正常生产回复，不启动常驻worker、不投递/修复或回放前10条。
第11若意外走模型路径会在启动前拒绝；第12经单Kimi限1次启动、每回合3步。
embedding provider不构造/不调用，意外向量化路径会失败，不靠假向量制造通过。
当前恢复报告/完整库快照预检通过，新调用0/0；新report目录尚未在真实库生成。
独立报告完整留历史12/19及新增最多1/0，任何数据漂移/失败/取消拒绝自动重复。
完成两条和audit也只标followup_passed，不自动改原report或跳过真实回答审阅/第5步重启。

### 当前收尾：两条实际补验与同库重开均通过

补验report为`followup-runs/20261012T153115Z-78c39e8f/report.json`，两条实际回复核对
通过，新增Kimi/embedding=1/0，历史累计13/19。没有新提案/动作或派生记忆变化。
第5项最终report为`restart-runs/20261012T160652Z-815d4d9a/report.json`，PID49384/49387
实际重开同一SQLite并读既有真实Qdrant，均退出0并回收；两次结果一致。
有效12秒纠正仍能BM25和既有缓存自匹配/RRF召回，旧决定/完成/取消/误写偏好不复活；
来源/沿革/取消回执不变。远端5条current含4条墓碑，双向只读audit一致，积压0。
完整库schema/data快照与补验后一致；新模型0/0，不修改配置/旧两库。

本阶段5/5可收尾，具体安全命令与重启含义见[操作说明](memory-persistent-restart.md)。
不需要再跑旧付费命令；日常启用服务/切换默认/容器重启或联合灾备恢复是另外的授权范围。
