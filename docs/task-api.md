# 任务 API 契约（阶段七第 1 步）

所有任务路由以 `/api/v1/tasks` 为前缀。当前已实现路由、Pydantic 请求/响应模型、
OpenAPI 描述和统一错误响应。任务服务通过 `create_app(task_service=...)` 注入；默认应用
尚未连接真实工作流服务，任务路由会返回 `503 task_service_unavailable`。

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

默认应用未配置任务服务时，路由会先返回 503；注入任务服务后，输入校验错误按下述
422 契约返回。

下一步将实现 `TaskService` 的持久化工作流适配：创建任务时建立完整运行上下文并启动
TeamRoom 流程，查询读取持久化快照，取消协调正在运行的 Agent。届时默认应用才会执行
任务路由的实际操作。
