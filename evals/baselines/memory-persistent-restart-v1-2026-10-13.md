# 真实补验与同库重启收尾（2026-10-13，本机时区）

## 结论和口径

独立持久目标`20261012T093544Z-5295c08a`的真实运行验收五项完成。
证据组合为原前10项真实运行、误写偏好撤销/同步、针对性第11/12条补验，以及
两个新OS进程同库只读重开；不是一次新的12步全通过，也不是新的未见样本评测。
原V1/V2失败、恢复和补验报告的字节/哈希都保持不变，不把历史失败抹成成功。

## 第4项两条真实补验（用户自行运行）

- `followup-runs/20261012T153115Z-78c39e8f/report.json`。
- SHA256：`7bbcf51f4fd58353f956540cda4ca343fbd5903a9e9bba7b78f1008fd21f299b`。
- `complete=true`、`followup_passed=true`，`full_lifecycle_passed=false`保持两条补验口径。
- 第11条读取SQLite的已取消记录：准确说明“不再属于未完成待办”、不独立证明
  现实结果，原始消息保留，本轮无修改。无Agent session、无普通记忆上下文。
- 第12条真实Kimi回复明确“本房间还没有…已确认口径”“尚未确定”，未泄漏8/12秒
  或编造其他超时值；后半段候选比较仅为条件式建议，不冒充已确认决定。
- Codex已核对这两条实际回复并说明可收尾；没有替用户填写逐条human签审表。
  原`semantic_review=pending_human_review`不改，在新重启report中说明此审阅方式。
- 无提案/动作，记忆/缓存/FTS/revision/outbox不变。新增Kimi启动1、embedding API0，
  历史12/19分别保留，累计13/19；不是供应商token/现金账单，按用户要求未核对实际账单。
- 原生产audit完成一致，本地/远端各5条、findings0；delivered8/superseded10，积压0。
  清理成功，完整库快照与报告一致。

## 第5项实际不同PID重开

最终入口：[check_memory_persistent_restart.py](../check_memory_persistent_restart.py)，
[操作说明](../../docs/memory-persistent-restart.md)。

- 新最终report：`restart-runs/20261012T160652Z-815d4d9a/report.json`。
- SHA256：`e464f277506a75fa199b42c0e36f56e99155c1ca590714612558ae3d35441030`。
- 两个真实PID **49384 / 49387**，按顺序启动并退出，returncode均0；所有已启动
  子进程回收，未留下worker，`complete=true`、`restart_passed=true`。
- 两个进程都重新读取同一持久化SQLite和既有真实Qdrant，生产BM25/RRF/Qdrant读
  适配器重新构造。SQLite不是复制库，不经Fake API、不改时钟/SQL状态、不重放消息。
- 12秒纠正active1；旧8秒决定retired/replaced、开发待办retired/completed、个人
  待办retired/cancelled、误写偏好retired/manual。替换沿革/原来源/关闭来源保存。
- BM25只召回有效纠正`6b354bd2-6a7f-475c-ad30-6f47d4958db6`，关闭待办不进入
  普通召回，个人房间未出现开发决定；既有个人查询重新得到同一SQLite取消回执。
- cache/FTS各1，规范revision5；双向只读audit本地5/current5/远端5，完整一致。
  远端**4条current墓碑**保留版本围栏，不以物理删点冒充不复活；无孤儿或额外点。
- 每进程2次**既有文档向量回放**，直接自匹配和RRF均命中，房间/当前来源讨论排除
  均为空。不是新文本语义embedding，不使用合成向量，不测新的语义质量。
- outbox仍delivered8/superseded10，pending/processing/waiting/dead均0；只读audit
  不插maintenance记录、不认领/投递/重试。Agent/embedding API新增**0/0**。
- 退出后再次全库检查：before/after均
  `e5b2d7239679b37d520d7a49ea023eb2cc1c47e373eb313bb246028a3e1bb37a`。
  28messages/14turns/5memories、来源/候选/FTS/cache/队列/审计历史及schema全部不变；
  原prepare/V1/V2/recovery/followup和关键源码哈希保持。未读取或输出私有密钥正文。

## 失败保留与回归

首次沙箱进程无法通过本机Qdrant元数据预检，保留
`restart-runs/20261012T155840Z-e6898ff6/report.json`，complete=false、数据库未变；
不是记忆召回miss，且当时已创建的唯一读进程已回收。早版报告的cleanup字段还要求
“创建满2个”，因此为false，不代表进程仍在；最终入口把已创建进程清理与观察满2个
分开判定。申请本机访问授权后实际通过，未用其他工具绕过沙箱。

中间成功报告`20261012T160255Z-79c788a8`也保留；补齐统计/审阅说明后，以最终
代码重新显式运行生成以上最终报告，无自动付费重试或数据修复。

新增 **34项**离线控制通过：只读写入/缺失库拒绝，漂移/越界/旧报告拒绝，无网络
预检，规范状态/缺缓存/FTS脏/关闭依据/积压拒绝，远端缺失/漂移/额外点/不完整扫描/
停服，缓存只准已知文本，子进程身份/结果不一致/超时/输出异常拒绝，取消留档、
退出清理后状态变化不得通过。Fake launcher/remote测试不冒充实际OS证据；另有真实
Python子进程重新打开临时只读SQLite测试。扩大定向 **933 passed，2条既有依赖警告**，
非全仓。Ruff/diff通过；旧24题有序召回冻结仍匹配TP/FP/FN=10/0/2、Recall@3=83.33%，
不改标签或重冻结历史指标，新增模型0/0。

采用Context7的Python3.11官方文档核对只读URI、连接显式关闭、干净子进程env和
有界wait/回收。本机运行Python3.11.16；复用既有应用读接口，不触发初始化/迁移。

## 不扩大结论

只读新进程重开不是重启Docker/Qdrant/浏览器服务、真实模型UI旅程、硬崩溃或联合
灾备恢复，不承诺高并发SLA、任意长对话/多Agent准确率或线上通用质量。已有隔离
Qdrant故障证据单独保留，不能代替本目标的容器重启证明。全局仍SQLite/legacy、
worker disabled；旧两库、`.env`和Fake服务不改，不把专属验收库通过等同自动切默认。
