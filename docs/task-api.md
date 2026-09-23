# 任务 API 契约与持久化服务（阶段七第 1～2 步）

所有任务路由以 `/api/v1/tasks` 为前缀。当前已实现路由、Pydantic 请求/响应模型、
OpenAPI 描述和统一错误响应。阶段七第 2 步新增 `PersistentTaskService`，通过
`create_app(task_service=...)` 注入；默认应用尚未配置 Agent 绑定和验证计划，因此
任务路由仍返回 `503 task_service_unavailable`，不会假装自动执行任务。

| 方法 | 路径 | 请求 | 成功响应 |
| --- | --- | --- | --- |
| `POST` | `/api/v1/tasks` | `issue`, `repository_path` | `201 TaskView` |
| `GET` | `/api/v1/tasks` | `state?`, `limit`（1～100）, `offset` | `200 TaskPage` |
| `GET` | `/api/v1/tasks/{task_id}` | UUID 路径参数 | `200 TaskView` |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | `expected_revision`, `reason?` | `200 TaskView` |

`TaskView` 包含 `task_id`、`trace_id`、Issue、仓库路径、状态、返工轮次、乐观锁
`revision` 和创建/更新时间。列表响应返回 `items`、`limit`、`offset` 和可选的
`next_offset`。取消请求必须提供调用方最近看到的 revision；服务可用 `409` 拒绝过期
版本或不允许取消的状态。

错误响应为 `{"error":{"code":"...","message":"..."}}`。请求校验失败使用 `422`
和 `validation_error`，并附带字段位置、消息和错误类型；不存在的任务返回 `404`
`task_not_found`；状态或版本冲突返回 `409 task_state_conflict`；未配置任务服务返回
`503 task_service_unavailable`。

默认应用未配置任务服务时，路由会先返回 503。注入持久化服务后，创建任务会先创建
独立 Git Worktree、保存 Task/TeamRoom/RuntimeContext 和首条 Issue 消息（同时投递给
Planner 与 Orchestrator），然后在本进程异步启动事件循环。查询和分页读取 SQLite；
无效 Git 仓库返回 `422 invalid_repository`。运行完成或暂停后回写 Task 与运行上下文；
运行异常会留下 `needs_human` 状态和 trace 事件。

当前取消只支持**本进程未运行且状态允许取消**的任务，必须匹配 revision；正在运行的
任务返回 `409 task_state_conflict`。这避免把仍在写代码的 Agent 标记为已取消。尚未实现
活跃 Agent 取消、默认应用启动装配和跨进程派发；这些属于下一步的生命周期控制工作。

下一步将配置默认应用的安全装配和启动恢复，并完善运行中的任务控制。真实模型适配器、
测试命令和权限策略须由部署方显式配置；`VerificationPlan()` 的空命令配置不会通过
完成守卫。
