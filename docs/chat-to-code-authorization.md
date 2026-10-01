# P3 聊天转编码：授权边界

当前只实现 **P3.1 只读预检**，尚未实现从聊天室创建编码任务。聊天室中的 `@Agent` 消息、Agent 的建议和预检结果都不授予写入权限。

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

## P3.2 的执行门槛（未实现）

后续若实现“授权一次编码任务”，必须由 Human 在单独操作中提交 `idempotency_key`、预检得到的 `expected_base_commit` 和精确确认值 `authorize_one_coding_task`。服务端必须重新验证来源、仓库 HEAD/干净状态及路径解析，并把 Human 选择的范围与服务端权限策略取交集；不允许 Agent 或聊天文本代替 Human 发起。只有所有检查通过，才可创建一个受控 Task，随后按现有 Worktree/Verifier/Reviewer 流程执行。P3.1 只定义该请求模型，没有开放执行端点，也没有承诺 UI 流程已完成。
