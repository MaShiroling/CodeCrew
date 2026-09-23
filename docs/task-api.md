# 任务 API 契约与运行时服务（阶段七第 1～3 步）

所有任务路由以 `/api/v1/tasks` 为前缀。当前已实现路由、Pydantic 请求/响应模型、
OpenAPI 描述和统一错误响应。`PersistentTaskService` 可通过
`create_app(task_service=...)` 注入；完整装配使用 `create_app(runtime=...)`。
默认应用尚未配置 Agent 绑定和验证计划，因此
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

取消必须匹配 revision。若任务在本进程运行，服务会先取消事件循环、通知当前 Agent
适配器终止会话（验证子进程也会清理），等待执行停下，再写入 `cancelled` 与 trace；
版本变化或任务已终结会返回 `409 task_state_conflict`。停机只停止本地执行，不将任务
伪装成用户主动取消。

`build_task_runtime(...)` 是显式装配入口：调用方必须提供三角色 `AgentRegistry` 绑定、
静态/编译、公开与隐藏测试命令、路径权限及命令白名单。绑定能力不匹配、验证类别缺失或
命令不在白名单内时拒绝构建。将结果传给 `create_app(runtime=...)`，FastAPI lifespan
会在对外服务前扫描持久化任务，只派发恢复协调器判定为 `resumable` 的未处理事件；
停机时停止本进程任务。存在歧义的历史执行不会自动重放，需人工处理。

当前默认 `app.main:app` 仍未注入运行时，任务路由返回 503；部署方需要显式提供上述
配置，不能仅靠启动 Uvicorn 获得真实 Agent 执行。运行时当前仅支持**单进程/单 worker**：
尚无跨进程租约与派发锁；也没有真正的 Kimi K3 / DeepSeek Flash 适配器。

下一步是 SSE 事件流与断线续接，之后再补端到端 API 与 CLI 入口测试。
