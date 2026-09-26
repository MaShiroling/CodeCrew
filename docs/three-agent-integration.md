# 三真实 Agent 联调

本阶段分 5 个子步骤。本页记录的是接线和协议验证，不把离线进程模拟当作真实模型验收。

## 1. 显式团队配置与聊天协议预检（已完成离线验证）

配置：`examples/server-config.codecrew-team.python.json`。保留原有通用示例，不改变默认团队。

| 人格 | 适配器 | 注册角色 / 权限 | 聊天输出 |
| --- | --- | --- | --- |
| 白金 | Codex CLI | Planner / read-only | `share_plan`，或回复澄清 |
| 月见 | Kimi Code CLI | Implementer / workspace-write | 向白金提问，或向 Orchestrator `request_review` |
| 鲸鲸 | Claude Code + DeepSeek | Reviewer / read-only | 向 Orchestrator `approve_review` 或 `request_rework` |

三角色仍共用 `AgentTurnRunner`，不是三个互不相干的独立冒烟脚本。
每轮注入自己的职责、行为校准、限制和团队关系原则；外观不进入执行提示。
动作按结构化 recipient 路由，单独写 `@名字` 不能绕过路由规则。
月见的限制工具不含 Bash；测试交给确定性 Verifier，不允许编造已执行测试。

输入/输出协议要点：

- 返回 `{"actions": [...]}`，最后且仅有一个 `finish_turn`；它只结束回合，不代表任务成功。
- Plan/Review 小对象通过 `artifact_content` 写入 ArtifactStore；已有大文件通过 `artifact_ids` 引用。
- 鲸鲸在聊天室不能返回独立冒烟的 `{"verdict": ...}`；必须返回审批/返工动作。
- 聊天室兼容原始 JSON 或唯一明确标记为 `json` 的代码块（可有外围说明）；多代码块、额外 JSON 候选、未知字段和无效动作仍拒绝。外围说明不触发动作。
- 格式错误、越权审批、含未解决高优先级问题的批准均被拒绝；对应输入保留未 ACK。
- 角色注册严格分离：白金没有 Implementer 写权限，鲸鲸只注册 Reviewer。

离线预检：

```bash
.venv/bin/pytest -q tests/test_three_agent_chat_preflight.py tests/test_chat_actions.py tests/test_reviewer_runner.py tests/test_cli.py
.venv/bin/python scripts/check_offline.py
```

测试通过生产配置入口构建运行时，创建临时真实 Git Worktree，再分别用模拟 Codex JSONL、
Kimi stream-json 和 Claude stream-json 执行聊天室回合。校验角色绑定、人格、CLI 参数、
环境隔离、会话标识、Plan 版本、Review Artifact、路由和 ACK。
**不启动真实 CLI，不调用模型 API，不运行完整事件循环；模拟审批不是有效完成证据。**
Kimi 的模拟边界只证明接线，不代替现有 Seatbelt 系统测试。

## 2. Planner → Implementer（开发、离线与用户本机在线验收通过）

### 受控 Artifact 读取

`AgentRequest.artifact_inputs` 使用不可变的逐文件契约；根据 Pydantic 校验要求限制字段、
绝对路径、唯一 ID 和任务/Trace 归属。只有可信 Orchestrator 能构造授权，Agent 动作和
公共任务 API 都不能提交文件路径来获得权限。

`AgentTurnRunner` 从本轮待处理消息的引用查询 ArtifactStore，校验类型、哈希和归属，
从可信存储计算路径；启动前和回合结束后流式复核 SHA-256/大小，并拒绝符号链接。
重复引用按 ID 去重，未收到的 Artifact 不授权。失败不 ACK，不路由后续成功动作。

Kimi Seatbelt 对单个文件增加读取例外和明确写入拒绝，不增加目录读取权限；文件必须
位于可写 Worktree/runtime 之外。系统级测试验证授权文件能读、相邻文件不能读、原文件
不能改写或删除。保留的 Read 工具事件只记录文件路径，不记录任意工具参数。

这是可信本地文件系统下的受控授权与篡改检测，**不是对恶意并发文件系统所有者的原子快照**，
也不意味着 home 之外的所有读取已被隔离。旧 Review history 的路径不会自动获得授权。

### 小任务联调

`scripts/planner_kimi_smoke.py` 是测试夹具，不是生产调度器或正式 EvalRunner。
它创建临时 Git 仓库及独立 Worktree，复用生产 `AgentTurnRunner`、Mailbox、ArtifactStore、
Plan 版本链和 Verifier。最多四个 Agent 回合：

1. 白金只读分析 `total(items)` 漏算最后一项的 Bug，发布 Plan v1。
2. 月见读取 Plan，先询问测试执行者，不改代码。
3. 白金按问题 ID 回答并发布 Plan v2，说明 Verifier 执行测试。
4. 月见读取 Plan v2，修改 `src/pricing.py`，向 Orchestrator 请求验证/评审。

夹具检查精确 Plan 路径的 Read 工具事件、问题关联、版本链、各轮 ACK、Planner/澄清阶段
没有源文件变更、主仓库未改变、最终只有指定源文件 Diff，以及语法/公开测试/额外断言。
Read 事件证明工具调用路径，不是独立的模型理解证明。额外断言依旧不具备保密隔离。
离线用例模拟真实 CLI 协议，分别验证成功交接、Plan 篡改和假完成被拒绝。
回合证据、会话标识、Token 缺失值和测试报告存入临时 ArtifactStore，关键操作有 trace_id。

不执行 Reviewer、CompletionGuard 或完整任务状态机；报告的 `task_success=false` 是刻意的，
交接与 Verifier 检查通过并不等于任务完成。测试后删除的是临时 Worktree，证据仍在 pytest
临时目录，未来可能被 pytest 清理；不要把它作为长期评测归档。

### 在线验证命令

本次桌面任务进程可找到两个 CLI，但未继承 `KIMI_MODEL_API_KEY`，因此**没有运行在线联调**。
在你已配置 Kimi Code 会员密钥、Codex CLI 登录的同一终端中运行：

```bash
CODECREW_RUN_PLANNER_KIMI_LIVE=1 .venv/bin/pytest -q -s tests/integration/test_planner_kimi_live.py
```

预计使用 2 个 Codex 回合和 2 个 Kimi 回合，各回合超时 180 秒；不自动重试、不调用 DeepSeek。
用例通过后打印 trace_id、证据目录及数据库路径，不打印密钥。若失败，请提供失败断言，
不要粘贴凭证；协议或 Read 路径证据不符合要求时必须修复，不能改成跳过检查。
全量离线入口已追加关闭 `CODECREW_RUN_PLANNER_KIMI_LIVE`，避免误触发付费调用。

### Codex 重连诊断（2026-09-26）

用户首次运行联调在 Planner 回合遇到重连超时；随后独立 CLI 诊断出现多条 `error`，
但回退 HTTPS 后返回指定消息和 `turn.completed`。这证明该次独立请求最终完成，
**不代表 Planner → Kimi 联调已经通过**。

适配器已区分中间诊断和最终失败：`error` / `item.completed.error` 保留在事件流中，
后续 `turn.completed` 可清除此前诊断；明确的 `turn.failed` 不可清除。回合完成还要求
进程退出码为零，缺少完成事件、非零退出、超时或取消都不能视为完成。
依据 [Codex 非交互 JSONL 生命周期](https://learn.chatgpt.com/docs/non-interactive-mode)
和本机观察序列添加离线回归测试；没有修改网络配置、自动重试或放宽超时预算。

若终端找不到桌面应用附带的 CLI，可先执行：

```bash
export PATH="/Applications/ChatGPT.app/Contents/Resources:$PATH"
codex --version
```

修复后需在原配置终端重跑上方在线联调命令；独立 CLI 成功不是完整任务成功证据。

### Kimi 最终输出约束（提示加强，在线待验证）

后续用户运行已进入 Kimi 澄清回合，但返回说明文字加 JSON 代码块，严格解析拒绝了
该输出；没有把自然语言中的“已提问”当作已路由动作。尚未完成 Plan v2 和最终验证。

已在 Kimi 限制 Agent 的系统提示和聊天室回合提示末尾加强格式要求：最终只输出
原始 `{"actions": [...]}`，说明、进度、问题和人格表达放进动作的 `content`；
澄清必须发送实际 `ask_question`，最后仅有一个 `finish_turn`。独立冒烟没有聊天室
动作契约时仍遵循其自己的输出要求。未修改解析器、增加付费重试或放宽权限。
离线提示接线测试只能证明规则已注入，不能证明真实模型一定遵守；在线验收仍待重跑。

此前严格包装策略的专项离线回归通过生产 Kimi 适配器和 `AgentTurnRunner`，模拟 CLI 的真实输出协议：
原始 JSON 和完整单个 JSON 代码块均走完四回合交接及确定性验证；前置/尾部说明文字、
多代码块、纯文字“已提问”和未知字段均被拒绝。异常回合后所有成员的待处理消息、
聊天室消息、Plan 版本、Artifact 和夹具源文件保持不变；不 ACK、不路由、不自动重试。
这些模拟结果不代替真实模型验收，也不保证异常回复前没有发生模型工具操作。

```bash
.venv/bin/pytest -q tests/test_planner_kimi_handoff.py tests/test_chat_actions.py tests/test_agent_turn_runner.py
```

2026-09-26 用户在原配置终端重跑，提供单条用例通过输出；
`trace_id=5f646b43-1d20-4278-825e-016c22398d60`，验证 Artifact ID 为
`4e9801b7-58ad-46e1-a0fc-164a0c0a4571`。验收范围为四回合交接及 Verifier；
输出 `task_success=false` 是预期，因为该用例不执行 Reviewer/完成守卫。
来源是用户提供的终端结果，非本桌面任务代为调用；原始证据仍在 pytest 临时目录。

## 3. 完整成功路径（开发与离线验证完成，三模型在线待验收）

`scripts/three_agent_smoke.py` 复用临时仓库/Worktree，但从 Issue 起使用生产
`WorkflowEventLoop`、`WorkflowController`、`WorkflowDirectiveExecutor` 派发所有回合，
不是把前三个独立冒烟结果拼接，也不是手动设置 `completed`。

1. 白金生成 Plan v1；月见读取并提出结构化澄清。
2. 白金回复并生成 Plan v2；月见读取新版 Plan 并修复代码。
3. Verifier 执行语法、公开测试、额外断言、目录权限和命令策略检查。
4. 生产执行器把最新版 Plan、Diff、变更清单、验证报告、权限报告和命令/测试日志
   作为去重后的 Artifact 引用发送给独立鲸鲸会话。超出 50 个引用则明确失败，不静默截断。
5. 鲸鲸通过 `Read` 留下每个证据文件的路径记录，输出审批动作；只有
   CompletionGuard 全部条件通过后，控制器处理 `completion_passed` 并完成任务。

本夹具保留四回合澄清设计，外加一个 Reviewer 回合：最多 5 回合，每回合 180 秒，
事件上限 20、报告 Token 预算 200,000。每轮规划使用新会话，Kimi 仍是新会话，
鲸鲸从不继承其他角色会话；DeepSeek CLI 使用临时私有 HOME。正常生产会话恢复能力
没有被改变。此成功路径夹具把返工预算设为 **0**，拒绝或守卫失败则转人工，不自动
消耗第三轮 Kimi/第二轮 Reviewer；项目生产返工默认上限仍为两轮，下一子步骤验收。

夹具在只读/澄清回合检查 Worktree 文件快照，Reviewer 回合校验已有证据未改变、
只有 Read/Glob/Grep 工具事件且每个共享证据有可见 Read 路径。完成判定前检查这些事实；
可见调用并不证明模型理解，也不是操作系统级只读沙箱。检查是夹具的防线，并不代表
任意生产任务都具有同样的快照保护。工具执行后再发现改动不能撤销其副作用。

此次测试暴露并修复队列事件与 ACK 快照问题：Answer 回合一次 ACK 同时到达的 Plan v2
后，控制器使用最新投递状态，仍严格核对不可变消息、序号及接收人，拒绝伪造事件。

离线回归覆盖完整成功、假批准但无 Diff/测试失败、评审拒绝，以及漏读、禁止工具、
改代码、篡改 Artifact、歧义回复、高优先级未解决问题。所有进程输出为模拟；
真实三模型成功路径尚未运行。使用：

```bash
.venv/bin/pytest -q tests/test_three_agent_success.py tests/test_workflow_controller.py tests/test_offline_check.py
.venv/bin/python scripts/check_offline.py
```

真实验收需要同一终端已配置 Codex 登录、三个 CLI 的 PATH、`KIMI_MODEL_API_KEY`
和 `DEEPSEEK_API_KEY`。只在愿意消耗至多五个真实模型回合时执行：

```bash
CODECREW_RUN_THREE_AGENT_LIVE=1 .venv/bin/pytest -q -s tests/integration/test_three_agent_live.py
```

通过后输出 trace_id、SQLite/Artifact 路径、任务报告与完成决策 Artifact ID 和
`task_success=true`。报告引用最终 Patch、Plan 版本、审批、验证和完成决策，记录会话、
时延及缺失 Token 的 null；每回合原始规范化事件保存为 Artifact。失败不能通过改断言
或自动批准消除。全量离线入口显式关闭新的 `CODECREW_RUN_THREE_AGENT_LIVE` 标志。
测试退出会移除临时 Worktree/Reviewer HOME，证据可能被 pytest 清理。

此处的 `hidden_tests` 类别仍为可见额外断言，没有保密隔离；成功仅针对该受控夹具，
不代表正式隐藏测试评测、实际模型版本确认、UI 驱动验收或不可信仓库安全性。

### 受控输出归一化（在线仍待重跑）

用户运行五回合用例，在 Kimi 澄清回合再次遇到“说明 + JSON 块”，尚未运行独立鲸鲸。
单靠提示没有消除此波动，因此当前聊天室解析加入确定性包装兼容：接受原始 JSON，
或唯一且完整的 ```json 代码块，可带外围说明。不会从裸露文字的花括号里猜测答案；
多个/其他/不平衡代码块、块外其他对象或数组候选、重复字段和 NaN/Infinity 均拒绝。
外围说明最长 16,000 字符、至多 100 个可能对象/数组起点；超限拒绝。

提取的对象仍须通过动作 Schema 和原有路由/归属/评审规则。说明中的“已提问”“已批准”
不产生动作；Implementer 即使写了审批动作仍没有 Reviewer 权限，高优先级问题不会
因说明宣称批准而消失。独立 `AgentReviewerRunner` 的旧 verdict 接口未开启此包装兼容。
执行提示继续要求原始 JSON，而非鼓励模型追加说明。

`AgentTurnRunner` 在获得同 Trace 的 AgentResult 后，将完整结果存为标记
`purpose=raw-agent-output` 的 Generic Artifact，再通过 `agent_output_recorded` Trace
引用 ID/哈希。成功和被拒绝的输出原文均保留，不替换原始 `result.output`。
异常格式仍不 ACK/不路由/不发布动作 Artifact，但会新增诊断 Artifact/Trace；
这更新了此前“异常后 Artifact 总数完全不变”的测试约定。诊断记录不能作为完成证据。

离线测试覆盖 Kimi/鲸鲸混合包装的完整成功路径，以及歧义输出不 ACK、原文保留、
角色越权仍被拒绝、带说明的高优先级批准仍失败、旧 Reviewer 严格接口不变。
没有增加任何模型纠错回合或自动重试。真实五回合命令不变，仍待用户重跑。

## 后续子步骤（尚未完成）

4. 返工与预算：验证明确拒绝、问题 ID 延续、修复再审以及最多两轮后的人工接管。
5. UI 演示与验收记录：从页面发起任务，展示聊天、证据、Patch 和报告，记录异常与限制。

## 开始在线联调前的边界

- 本步不需要新增密钥；测试只使用非真实占位值。在线运行时，启动服务的同一 shell 必须
  已配置 Codex CLI 登录、`KIMI_MODEL_API_KEY` 和 `DEEPSEEK_API_KEY`。密钥不要写入 JSON。
- Kimi 写入隔离仍要求 macOS Seatbelt；Worktree 根目录必须位于目标仓库之外。
- 新示例只允许修改 `src`，不允许修改验收测试；命令和路径必须按目标夹具调整。
  示例 `tests/hidden` 是占位路径，**没有隐藏测试保密隔离**，不能用于正式可靠性评测。
- Kimi 默认阻止读取真实 home 下、Worktree/私有 runtime 以外的文件；现在只为已校验的
  本轮 Artifact 添加逐文件只读例外，未共享文件和整个 Artifact 根目录都不授权。
- Reviewer 的工具只读配置不是 OS 级只读沙箱；真实远端版本仍未确认。
  `kimi-for-coding` 别名不能当作 K3 版本证据，缺失 Token 统计不能记为零。
- 本步未在线验收这个团队配置；不要将启动成功或协议测试通过称为三模型任务成功。
