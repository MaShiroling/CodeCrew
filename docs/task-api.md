# 任务 API 契约与运行时服务（阶段七）

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
| `GET` | `/api/v1/tasks/{task_id}/room` | UUID 路径参数 | `200 TaskRoomView` |
| `GET` | `/api/v1/tasks/{task_id}/messages` | `after_sequence`（默认 0）, `limit`（1～100） | `200 RoomMessagePage` |
| `GET` | `/api/v1/tasks/{task_id}/plans` | UUID 路径参数 | `200 PlanPage` |
| `GET` | `/api/v1/tasks/{task_id}/artifacts/{artifact_id}` | 两个 UUID 路径参数 | `200 ArtifactDetail` |
| `POST` | `/api/v1/tasks/{task_id}/cancel` | `expected_revision`, `reason?` | `200 TaskView` |
| `GET` | `/api/v1/tasks/{task_id}/events` | `after_sequence?` 或 `Last-Event-ID` | `200 text/event-stream` |

`TaskView` 包含 `task_id`、`trace_id`、Issue、仓库路径、状态、返工轮次、乐观锁
`revision` 和创建/更新时间。列表响应返回 `items`、`limit`、`offset` 和可选的
`next_offset`。取消请求必须提供调用方最近看到的 revision；服务可用 `409` 拒绝过期
版本或不允许取消的状态。

错误响应为 `{"error":{"code":"...","message":"..."}}`。请求校验失败使用 `422`
和 `validation_error`，并附带字段位置、消息和错误类型；不存在的任务返回 `404`
`task_not_found`；状态或版本冲突返回 `409 task_state_conflict`；未配置任务服务返回
`503 task_service_unavailable`。

任务详情接口是阶段八前端的只读数据基础。Room 返回成员与角色；Messages 按房间内
`sequence` 升序分页，包含发送者、收件人、消息类型与 Artifact 引用，不含本地文件路径。
若 `next_after_sequence` 非空，可据此继续取下一页；增量刷新也可使用最后一条消息的
`sequence`。Plans 返回已保存的版本记录，具体 Plan 内容通过关联 Artifact 读取。
Artifact 接口同时校验 `task_id` 与 `trace_id` 归属；其他任务的 Artifact 返回 404。
小于等于 128 KiB 的 UTF-8 文本/JSON 提供 `preview`，大文件或二进制只给元数据及
`preview_unavailable_reason`，不开放任意路径或文件下载。尚无用户身份认证，仍仅适合
可信本地环境。

本地只读工作台位于 `/ui/`，由同一个 FastAPI 进程提供静态 HTML/CSS/JavaScript；
不需要额外的前端构建或服务。它使用上述 API 展示任务列表、状态、团队消息、Plan
版本与 Artifact 预览，并可手动刷新、筛选与分页。当前**没有**实时推送、创建任务表单、
登录或远程访问控制；默认未装配运行时的应用会显示任务 API 不可用提示。使用
`codecrew serve --config <JSON>` 显式装配运行时后，在本机打开
`http://127.0.0.1:8000/ui/`。界面不把 Agent 的自然语言结论当作成功证据；任务状态
仍由后端完成守卫决定。

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

事件流按 TraceStore 的全局递增 `sequence` 发送，事件帧包含 `id`、事件类型和 JSON
`data`（含序号与完整的小型 trace 事件）。客户端断线后带 `Last-Event-ID` 重连，或首次
连接使用 `?after_sequence=N`；两者同时存在时以请求头为准。序号可能因其他任务的事件
而跳号，客户端不应假设连续。路由先校验任务存在，再回放历史事件并轮询新事件；任务
终结且积压事件已发送完毕后关闭连接。空闲时使用 SSE 注释心跳，心跳不占用游标。
这只是持久化事件投递，不是成功判定，也不提供未经授权的对外访问控制；当前 API 应
仅在可信本地环境使用。

端到端测试使用 FakeAgentAdapter 通过 HTTP 提交任务，覆盖有效 Diff 经三类确定性检查、
Reviewer 审批和 CompletionGuard 后完成，以及无有效 Diff 时不得完成。
本地 CLI 入口为 `python -m app.cli serve --config <JSON> --port 8000`（安装后也可
使用 `codecrew serve`），只绑定 `127.0.0.1` 且固定单 worker。JSON 显式配置
Planner 适配器、验证计划、允许写入路径与命令白名单；示例见
`examples/server-config.python.json`。当前 CLI 只能使用已有 Claude Code / Codex CLI
适配器，不能配置尚未实现的 Kimi K3 / DeepSeek Flash。示例中的 `tests/hidden` 只是
占位，不能保证测试对 Agent 保密；真正的隐藏测试隔离仍属于后续工作。
