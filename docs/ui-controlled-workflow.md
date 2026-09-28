# 第 6 步：本地 UI 受控继续、取消与预算

任务详情新增“受控工作流”面板。服务端只读 `GET /api/v1/tasks/{task_id}/control`
返回当前 Task/Runtime 修订号、预算策略与实际用量、确定性预算阻塞、最新继续回执、
工作流结果及取消观察。该快照仅供展示，**不是执行许可证**；不暴露 Claim token，
也不根据 Agent 自述显示任务成功。

Human 消息列表的 `pending_for_continuation` 来自真实待投递状态。页面只把仍待处理、
由人工消息 API 保存，且可交给 Planner/Implementer 的消息作为完整工作流候选；
Reviewer 消息不能直接启动整条工作流。用户选中消息和目标后明确点击“预检并继续”。
第 7.2 步也在符合条件的 Human 消息旁提供同一操作入口，减少返回右侧面板的滚动；
它复用既有确认、预检、授权及继续逻辑，而非新的一条执行路径。消息发送本身仍不启动
Agent；Reviewer 定向消息不显示此入口，阻塞原因会显示在消息旁。
浏览器先调用 `POST .../continue/preflight`，再调用 `POST .../continue/workflow`；
先前已有成功继续回合时，要求填写原因，并使用同一幂等键先调用
`POST .../continuations/{previous_request_id}/authorize`。所有请求仍由服务端检查最新
修订号、授权、预算、权限、证据和未解决占用。`202` 只是受理，页面轮询只读控制视图；
只有最新 `ContinuationWorkflowOutcome` 和任务状态能展示完成，首回合 `succeeded`
不等于任务成功。冲突或网络结果不明不会自动重发写请求。

运行中最新回合为 `claimed` 时，页面显示“请求取消当前回合”。用户填写原因并确认后，
页面先读取 scoped 回执的 Task/Runtime 修订号与 Claim 更新时间，再调用原有 scoped
取消端点。取消回执和后续观察会显示在控制视图；它们不释放未知 Claim、不重置预算，
**也不证明全部 OS 派生进程或远端请求已停止**。浏览器请求中断不是 Agent 取消证据。
活跃或失败的占用会阻止再次继续和追加人工消息。

预算面板显示 Agent 回合、已报告 Token、未知 Token 回合数、Agent 耗时、房间消息和
返工轮数。`budget_violation`、未解决占用、返工耗尽、缺少可继续的人工消息和预检
错误均有明确提示。未知 Token 不记作零成本。页面仅适合可信本机单进程服务，
未增加远程身份认证、跨进程续租或真实模型 UI 验收。

第 7.3 步把 Task 阶段、阶段责任方、阻塞/下一动作和最近操作放进右侧概览。
阶段责任方根据 Task 状态与房间成员推断，**不是实时 Agent 会话证明**；继续回执的
目标只是本回合首个 Agent，后续执行者可能已变化。预算仍来自只读控制快照；
缺失、过期或读取失败的快照不能作为继续/取消操作依据。人工消息仍由自己的服务端
校验，控制快照读取失败不改变消息输入规则。

离线验证：`tests/test_task_control_api.py` 使用真实 SQLite/Git/Fake Agent 检查快照、
预算和成功结果；`tests/test_continuation_cancellation.py` 检查取消观察回显；
`tests/ui_control.test.cjs` 检查预算阻塞、预检拒绝、首次继续、取消、再次授权和
切换任务后的迟到预检。全量套件仍需单独对照既有 continuation 测试基线。
本次 `scripts/check_offline.py` 实际运行后仍有同 9 项基线失败，没有新增失败，
因此本步仅标记定向离线验收，不标记全量通过。
