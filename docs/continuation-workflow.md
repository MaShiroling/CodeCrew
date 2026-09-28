# 第 4 步：人工回复后的受控工作流

这一增量新增后端显式入口，使用 Fake Agent 验收从人工回复到确定性完成守卫的完整链路。
它不改变[第 3 步单回合接口](continuation-execution.md)；不开放 UI 人工输入，也不声称
真实模型已通过这个新入口。

## 调用与查询

1. Task 因请求人工输入停在 `needs_human`。Human 通过已有
   `POST /api/v1/tasks/{task_id}/messages` 发出关联回复。
2. 以第 3 步相同的严格命令调用
   `POST /api/v1/tasks/{task_id}/continue/workflow`。目标只允许 Planner 或
   Implementer；Reviewer 不能基于旧验证直接启动审批。首次人工等待可直接认领；同一
   Task 之后的新意图仍须显式授权，并携带 `authorization_id`。
3. `202` 仅表示入队/认领。用已有 `GET .../continuations/{request_id}` 查询选定首回合
   的回执；`receipt.state=succeeded` 仍**不是**任务成功。
4. 工作流成功提交后，`GET .../continuations/{request_id}/workflow` 返回单独的
   `ContinuationWorkflowOutcome`。它包括最终状态、成功标志、新验证/评审/守卫 Artifact ID、
   本链路会话 ID、返工轮数及等待原因。执行中、失败或取消没有已提交结果时返回 404；
   查询普通回执可区分 `claimed` 与 `needs_human`。任务状态另由 `GET .../tasks/{task_id}` 查询。

同一幂等键与完整命令只重放已存结果；单回合与整条工作流是不同作用域，交叉重放 409。
两者都不把未知或失败 Claim 重新出租。

## 安全与提交边界

- 首个选定 Agent 只消费这条 Human 消息及专属 Handoff；其输出进入现有
  `WorkflowEventLoop`，由控制器依次推进 Plan、实现、Verifier、Reviewer、返工和完成守卫。
  没有 Plan/实现事件或只有自然语言“完成”，最终回到 `needs_human`。
- 启动前清空当前 Runtime 中的旧验证/完成结果和 native session。历史 Artifact 仅供阅读；
  成功必须在此次 HTTP admission 之后产生新的验证、审批和守卫 Trace，顺序正确，
  且恢复出的 Artifact 与内存结果一致。完成守卫通过才允许 `completed`。
- 首次继续在内存中恢复到安全的 planning/implementing；持久化 Task 在最终提交前仍是
  `needs_human`。已授权的后续继续在授权消费时原子恢复活动状态。完成时 Task、Runtime、
  选定输入 ACK、首回合回执与工作流结果 Trace 同事务提交；故障不留下假成功或假 ACK。
- Reviewer 拒绝后，原控制器最多允许两轮返工。预算耗尽转人工，不得自行批准。
  失败/取消仍保留占用，不自动重跑；已写文件、已路由消息及尝试费用是跨事务副作用，
  不能因 SQLite 回滚而宣称已回滚。重启扫描不接管未知 Claim。
- 服务级取消围栏同第 3 步；对下游回合取消只证明本地任务中断及防止成功提交，
  仍不证明全部 OS 派生进程/远端请求停止。本阶段仍限可信本地单进程单 worker。

## 验收范围

`tests/test_continuation_workflow.py` 使用真实 Git Worktree、SQLite、Verifier、Artifact
和 Trace，Agent 全为 Fake。覆盖人工澄清→新 Plan→代码修改→静态/公开/隐藏检查→独立
Reviewer→完成守卫；授权后的 Implementer 继续；纯文字假完成；Planner/Reviewer 失败；
一次返工成功与两轮耗尽；首回合和下游取消；成功/取消后的重启；最终 Trace 写入故障回滚。
不调用真实模型，隐藏检查仍是当前配置类别，尚不等于 Agent 不可见的保密测试隔离。
