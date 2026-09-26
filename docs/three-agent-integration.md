# 三真实 Agent 联调

本页保留历史子步骤，同时按后续统一路线记录成功、返工和预算验收。
不把离线进程模拟当作真实模型验收。

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

### Reviewer 报告来源契约

`approve_review` / `request_rework` 的 `artifact_ids` 和 `artifact_content` 是
**两种互斥的评审报告来源**，不是“引用证据 + 输出报告”组合：

- 生成新报告：使用 `artifact_content`，省略 `artifact_ids`。最小报告内容为
  `{"issues": []}`；是否批准由动作类型决定，不需要额外的 `decision` 字段。
- 使用已有报告：使用 `artifact_ids` 引用当前任务/Trace 下的 `ReviewReport`，省略
  `artifact_content`。运行时仍检查报告类型、归属、完整性及评审结论。
- Plan、Diff、Verifier 和日志属于收到的输入证据，不能填成评审动作的报告来源。
  可在动作 `content` 中解释支持结论的证据；文字引用不授予新的读取权限。
- 同时提供两种来源，或两种都没有，均拒绝；不自动删字段、不自动补报告，也不增加模型重试。

批准示例（仅演示格式，实际批准必须以读取的证据为依据）：

```json
{"actions":[{"action":"approve_review","recipient":{"kind":"role","role":"orchestrator"},"content":"根据已读取的 Diff 和测试证据批准，等待完成守卫。","artifact_content":{"issues":[]}},{"action":"finish_turn","content":"评审回合结束，不代表任务成功。"}]}
```

返工示例（新问题生成自己的 UUID；历史问题沿用原 ID）：

```json
{"actions":[{"action":"request_rework","recipient":{"kind":"role","role":"orchestrator"},"content":"测试证据存在未解决的问题，需要返工。","artifact_content":{"issues":[{"issue_id":"00000000-0000-4000-8000-000000000001","priority":"high","summary":"示例问题：实际应填写有证据支持的缺陷。","resolved":false}]}},{"action":"finish_turn","content":"等待返工，不代表任务成功。"}]}
```

Reviewer 回合提示优先展示新报告形状与上述两类示例，不再把两个来源并列在推荐
格式中。已有报告路径的代码支持不变；未解决高优先级问题及 CompletionGuard 的校验不变。
格式示例不保证真实模型遵守，仍需在线验收。

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
真实三模型成功路径已尝试但尚未通过；最新运行到达 Reviewer 后因报告来源冲突失败。
使用：

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

### 第一部分第 2 步：真实成功闭环与长期证据

上述命令保持不变，不增加自动重试或额外模型回合。开始真实运行前，需在同一终端
确认三个 CLI、Codex 登录以及两个环境变量已经配置；不要把密钥放进命令、页面或仓库。
本轮桌面任务进程只检查了凭证是否存在：三个 CLI 可用，两个密钥均未继承，因此没有
代跑真实用例。Reviewer 契约修复后的真实成功验收仍待用户终端执行。

一旦进入已创建的任务夹具，不论工作流成功或失败，测试都会尝试把证据归档到
`evals/results/three-agent-live/<trace_id>-<随机后缀>/`；此路径已有 Git 忽略规则，
不会被 pytest 的临时目录清理。每次创建独立目录，不覆盖此前运行。

- `trace.sqlite3`：SQLite backup 快照，包括聊天室、Plan 版本、Trace 与 Artifact 元数据。
- `artifacts/sha256/`：仅复制已登记的内容寻址 Blob，按归档元数据核验 SHA-256 和大小。
- `manifest.json`：任务快照、任务/Trace ID、数据库哈希、Artifact 清单及使用限制。

不复制 CLI HOME、Kimi runtime、原仓库、Worktree 或父进程环境。目录权限为 `0700`，
完成归档的数据库、Blob 和清单为 `0600`。原始回复、聊天及日志仍可能包含敏感信息，
分享前必须检查；不承诺对任意日志内容自动脱敏，也不把文件权限当作恶意用户隔离。
清单记录的是归档完整性，**不是任务成功判定**。目录中的旧工作区路径不用于重新执行
命令，该归档不是可续跑工作区。

失败时也输出 `evidence_archive` 与实际 `task_state`，不能把目录存在视为验收通过。
归档异常会输出 `archive_error` 类别，不打印异常内容或凭证；若工作流本来已失败，
保留原失败，不用归档异常替换它。校验不通过的部分目录没有有效清单，不可作为完整证据。
若工作流通过但归档失败，用例仍失败。CLI/凭证预检或夹具创建前的失败没有可归档的任务。

验收需要同时满足：pytest 用例通过、`task_success=true`、五个规定 Agent 回合，以及
可核验的 Plan v1/v2、Diff、Verifier、ReviewReport 与 CompletionGuard 证据。通过后请
保留输出中的归档路径；失败时提供失败断言和路径，不必粘贴全部原始回复或任何凭证。
离线模拟已覆盖成功证据恢复、双来源审批失败归档、缺失/篡改/跨任务/符号链接拒绝；
这些结果不替代真实模型验收。

最新一次用户运行（Trace `cfb23e0f-ee47-4dfa-8581-79f38da2088a`）在首个 Planner
回合约 180 秒后超时，停在 `planning`；归档数据库和所有 Blob 的哈希匹配，但没有
Plan/Review/Completion 证据。旧归档只有最终结果，没有失败回合的事件快照，因此
不能据此断定是网络、CLI 启动还是模型处理耗时。本轮新增 `agent-event-stream`
诊断 Artifact 和 `agent_stream_recorded` Trace，用于下一次运行定位 stderr、重连诊断
及已收到的事件进度；旧运行的缺失事件不可追溯补造。详见[回合诊断](tracing.md)。
保持上方真实命令与 180 秒超时不变，不自动重试、不改成忽略超时。成功或失败输出的
`evidence_archive` 路径可供后续读取诊断，无需先粘贴所有日志。

### 第 2 步在线成功验收记录

用户本机后续运行 `5c67b36b-0aa0-4703-9226-03bd254eaca5` 通过，状态 `completed`。
已只读核对长期归档的数据库及全部 Blob 哈希，并用证据恢复服务重新核验：五回合
角色顺序正确，Plan v1/v2 存在，Verifier 全部检查通过，Reviewer 批准且无问题，
CompletionGuard 十项条件通过，无失败条件。任务报告为
`457c8947-a126-485c-8d61-b317db2500f5`，完成决策为
`33a2d3c9-0e87-4a34-8cf5-dfd2ab42b482`。五份事件诊断也已归档。
这只验收受控成功夹具，不证明此前超时已根治、真实返工、模型具体版本、隐藏测试
保密隔离、Reviewer OS 沙箱或 UI 端到端体验。

## 第一部分第 3 步：返工与两轮预算（离线通过，在线待验收）

复用生产控制器、指令执行器、Mailbox、Verifier 和 CompletionGuard，不手工设置成功状态。
修复了只发给 Orchestrator 的拒绝无法唤醒无待处理消息的 Implementer 的接线缺口：
有预算时由 Orchestrator 发布受控 `system_event`，携带原 ReviewReport/完成决策、最新
Plan、验证及日志引用，再唤醒 Implementer。保留 correlation/causation、Artifact 归属和
哈希校验、路由/读取授权及引用数量上限，不复制整段聊天历史。预算耗尽时发布带证据的
`human_input_request`，不启动第三轮返工。

两个场景是**显式故障注入实验，不是自然出错率或 Reviewer 发现率评测**：
测试在临时 Worktree 中、实现回合结束后且 Verifier 开始前，把 `src/pricing.py` 改成
`return sum(items) + 1`。真实测试必须据此失败；Reviewer 必须基于实际 Diff 和日志独立
给出拒绝。不会由脚本编造、改写或替代真实 Reviewer 结论。错误批准会令该场景立即失败，
不会追加模型纠错回合。每次注入都有 Artifact 与 `test_fault_injected` Trace，明确区别
于 Agent 生成的结果；注入器不属于生产任务执行路径，不修改原仓库或测试。

| 场景 | 控制条件 | 预期与回合上限 |
| --- | --- | --- |
| 一次返工成功 | 仅在初次实现后注入；返工后的代码保持 Agent 实际输出 | 2 Planner + 3 Implementer + 2 独立 Reviewer，最多 7 回合；1 轮返工，最终守卫通过 |
| 两轮耗尽转人工 | 初次实现及两次返工后都重新注入；不要求 Agent 故意保留错误 | 2 Planner + 4 Implementer + 3 独立 Reviewer，最多 9 回合；2 轮后再次拒绝，转人工且无成功结论 |

保持每回合 180 秒超时，已报告 Token 总量预算 400,000；缺失用量不按真实消耗为零解释。
每次评审使用新的原生会话，结构化历史保留此前问题 ID；批准不能把未解决问题从列表
直接删掉。每次修复后重新运行 Verifier，只读取当前证据。证据和注入记录继续长期归档。
这不改变生产默认最多两轮预算，也不证明 UI、服务重启或不可信仓库隔离。

离线回归：

```bash
.venv/bin/pytest -q tests/test_three_agent_rework.py tests/test_three_agent_success.py tests/test_workflow_execution.py tests/test_offline_check.py
.venv/bin/python scripts/check_offline.py
```

在线验收分开开启，**先运行一次返工成功**（最多 7 个真实回合）：

```bash
CODECREW_RUN_THREE_AGENT_REWORK_LIVE=1 .venv/bin/pytest -q -s tests/integration/test_three_agent_rework_live.py::test_live_three_agent_rework_success
```

通过后再运行两轮耗尽（最多 9 个真实回合）：

```bash
CODECREW_RUN_THREE_AGENT_BUDGET_LIVE=1 .venv/bin/pytest -q -s tests/integration/test_three_agent_rework_live.py::test_live_three_agent_rework_budget_exhaustion
```

两个开关在全量离线入口中显式关闭，不自动重试。在线用例未由当前桌面任务运行。
预算用例预期 pytest 通过且 `scenario_acceptance_passed=true`，但 `task_success=false`、
`task_state=needs_human`、`rework_rounds=2`：它证明正确停止，而不是完成代码任务。
归档分别位于 `evals/results/three-agent-rework_success-live/` 和
`evals/results/three-agent-rework_exhaustion-live/`；失败同样尝试归档，不覆盖旧记录。

此处的 `hidden_tests` 类别仍为可见额外断言，没有保密隔离；成功仅针对该受控夹具，
不代表正式隐藏测试评测、实际模型版本确认、UI 驱动验收或不可信仓库安全性。

### 受控输出归一化（尾部对象兼容在线仍待重跑）

用户运行五回合用例，在 Kimi 澄清回合再次遇到“说明 + JSON 块”，尚未运行独立鲸鲸。
单靠提示没有消除此波动，因此当前聊天室解析加入确定性包装兼容：接受原始 JSON，
或唯一且完整的 ```json 代码块，可带外围说明。另允许有限说明后、从新行开始的
唯一完整尾部 JSON 对象（可缩进或跨行，后面仅能有空白）。只考虑首个以 `{` / `[` 开头
的候选行，不跳过坏候选挑选后续答案，不接受行内 JSON、尾部数组或对象后追加说明。
说明中存在其他可解码对象/数组，或整段含代码围栏时，不适用尾部对象路径；
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
没有增加任何模型纠错回合或自动重试。真实五回合成功路径后续已通过，详见第 2 步
在线记录；当前待运行的是第 3 步两个返工/预算用例。

返工首次在线尝试 `82aa437d-ca4d-49ef-9411-db3288267c7f` 在初次月见澄清回合停止：
CLI 正常退出，但回复是说明文字加裸 JSON，旧解析器要求明确代码围栏而拒绝。
仅生成 Plan v1，未进入 Reviewer、故障注入或返工；归档数据库和全部 16 个 Artifact
哈希已只读核验。尾部对象兼容通过离线回归不代表真实返工验收已完成，也不增加付费重试。

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
