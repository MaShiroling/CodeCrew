# P3 聊天转编码：授权边界

P3.1 的只读预检与 P3.2 的独立授权端点已实现。聊天室中的 `@Agent` 消息、Agent 的建议和预检结果都不授予写入权限；只有 Human 显式提交确认命令，且服务器已装配任务运行时和权限策略，才会创建一个 Task。

## P3.1 合同

Human 从一个活跃聊天室选择一条**本房间 Human 消息**作为需求来源，另行填写本次代码变更目标、Git 仓库绝对路径和不包含仓库根目录的允许写入路径。HTTP 请求：

```http
POST /api/v1/chats/{room_id}/coding-task-preflight
Content-Type: application/json

{
  "source_message_id": "<本房间 Human 消息 UUID>",
  "repository_path": "/absolute/path/to/example-repo",
  "issue": "只修改 src/app.py，将 value 改为 2，并运行测试",
  "allowed_paths": ["src"]
}
```

预检检查来源和房间、仓库路径确为 Git 工作区根目录、存在 HEAD 提交、工作区无已修改或未跟踪文件，以及写入范围为仓库内的相对路径。它拒绝整个仓库 `.`、`.git`、`.codecrew`、`.env` 等敏感路径和指向仓库外的符号链接。成功响应包含标准化仓库路径、`base_commit` 和原请求范围，且固定为 `execution_authorized: false`、`task_created: false`。预检不会启动 Agent、创建 Task/Worktree、改动仓库或保存一次授权。

这一步只适用于可信本机部署；当前独立聊天室没有用户身份认证，不能作为对外暴露的多用户授权接口。预检时看到的 HEAD 和干净状态只是**快照**，不是执行期保证。

## P3.2 一次性授权与建任务

仅在显式配置的 `serve --config ...` 服务中可用；`chat-serve` 和 `chat-demo` 仍返回 503。Human 在单独 HTTP 操作中提交：

```http
POST /api/v1/chats/{room_id}/coding-tasks
Content-Type: application/json

{
  "source_message_id": "<本房间 Human 消息 UUID>",
  "repository_path": "/absolute/path/to/example-repo",
  "issue": "只修改 src/app.py，将 value 改为 2，并运行测试",
  "allowed_paths": ["src"],
  "idempotency_key": "<新 UUID>",
  "expected_base_commit": "<预检响应的 Git SHA>",
  "confirmation": "authorize_one_coding_task"
}
```

服务端重新预检来源、Git HEAD、工作区干净状态和路径解析，并要求本次写入范围**与服务端配置的 PermissionPolicy 允许范围完全一致**。这是当前运行时只支持全局策略时的保守限制：不会把 Human 选择的更窄范围伪装成已经生效。所有检查通过后先在 SQLite 写入单次授权占位，再以指定 SHA 创建 Worktree/Task 并启动既有受控流程。响应包含 `task_id`、`task_trace_id`、Git 基线和允许路径。同一幂等键/相同请求返回同一任务；同一 Human 来源不能用另一键再次授权。若创建途中进程崩溃或失败，占位保留为 `pending`，自动重试返回 409，需人工核查，避免重复执行。

## P3.3 本地 UI 操作

启动显式配置的任务服务（`python -m app.cli serve --config <JSON> --port 8000`），再打开 `/ui/chat/`。页面从 `GET /api/v1/chats/coding-capability` 读取是否开放编码和允许路径；该只读提示不是授权。与队友讨论后，在一条 Human 消息下点击“以此发起受控编码任务”；右侧面板显示服务端允许写入的路径。填写目标 Git 仓库绝对路径和本次变更目标，点击“先预检，不授权”。仔细核对仓库、目标、范围和完整 SHA 后勾选确认框，再点击“确认授权并创建任务”。创建后点击链接进入任务工作台；只有任务页面的 Verifier、Reviewer 与 CompletionGuard 证据能说明交付结果。`chat-serve`/`chat-demo` 不显示编码入口，也不执行授权。

当前 UI 已通过模拟浏览器脚本测试；Fake 后端集成测试从聊天来源走到任务完成、验证报告和 Patch 下载。**真实模型在线浏览器交付演示尚未复测**，不能把 Fake 结果当作真实三模型成功率。预检与授权都只适用于可信本机服务：**没有用户身份认证**；HTTP 的调用方被视为本机 Human，不能直接对外公开。仓库在预检与 Worktree 创建之间由外部进程改变的极端竞态尚未提供跨进程锁；Worktree 仍固定在 Human 确认的 SHA，而不是漂移到新 HEAD。
