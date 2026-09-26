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
- 只兼容完整的单个 `json` 代码块包装，仍拒绝解释文字、多代码块、未知字段和无效动作。
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

## 后续子步骤（尚未完成）

2. 真实 Planner → Implementer：用固定小型 Bug 仓库，验证计划生成、引用读取、修改和澄清。
3. 完整成功路径：接入 Verifier 和真实独立 Reviewer，再由 CompletionGuard 判断完成。
4. 返工与预算：验证明确拒绝、问题 ID 延续、修复再审以及最多两轮后的人工接管。
5. UI 演示与验收记录：从页面发起任务，展示聊天、证据、Patch 和报告，记录异常与限制。

## 开始在线联调前的边界

- 本步不需要新增密钥；测试只使用非真实占位值。在线运行时，启动服务的同一 shell 必须
  已配置 Codex CLI 登录、`KIMI_MODEL_API_KEY` 和 `DEEPSEEK_API_KEY`。密钥不要写入 JSON。
- Kimi 写入隔离仍要求 macOS Seatbelt；Worktree 根目录必须位于目标仓库之外。
- 新示例只允许修改 `src`，不允许修改验收测试；命令和路径必须按目标夹具调整。
  示例 `tests/hidden` 是占位路径，**没有隐藏测试保密隔离**，不能用于正式可靠性评测。
- Kimi 边界会阻止读取真实 home 下、Worktree/私有 runtime 以外的 Artifact 文件。
  当前聊天提示中的 Artifact 路径不意味着 Kimi 一定能读到；第 2 子步骤需提供受控的
  逐文件只读授权或私有上下文投影，并验证完整性，不能为此放开整个 Artifact 根目录。
- Reviewer 的工具只读配置不是 OS 级只读沙箱；真实远端版本仍未确认。
  `kimi-for-coding` 别名不能当作 K3 版本证据，缺失 Token 统计不能记为零。
- 本步未在线验收这个团队配置；不要将启动成功或协议测试通过称为三模型任务成功。
