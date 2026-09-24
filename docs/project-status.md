# 项目状态与后续路线（2026-09-24）

本页是当前开发基线；历史阶段编号只记录实现顺序，不代表真实模型或安全边界已经验收。

## 已实现

- 任务状态机、结构化 Agent 动作与 TeamRoom 消息、Plan 版本链、Reviewer 返工预算。
- 独立 Git Worktree、路径/命令策略、确定性 Verifier、CompletionGuard 和证据 Artifact。
- SQLite 持久化、Trace/SSE、保守的启动恢复，以及可显式配置的本地单进程任务 API。
- `/ui/` 只读工作台：任务列表、聊天室、Plan、Artifact 预览和实时状态。后端已有创建与取消任务 API；页面尚未接入写操作。
- Codex CLI、Claude Code、Kimi Code CLI 与 Fake Agent 适配路径；DeepSeek Flash Reviewer 适配路径已通过离线测试。

## 验证基线

- 本次在不启用付费真实模型测试的条件下运行以下命令：离线测试通过，显式启用的在线用例跳过；Ruff 通过。

  ```bash
  env CODECREW_RUN_KIMI_LIVE=0 CODECREW_RUN_KIMI_VERIFIER_LIVE=0 CODECREW_RUN_CLI_INTEGRATION=0 .venv/bin/pytest -q
  .venv/bin/ruff check .
  ```
- 受限执行环境不允许嵌套启动 macOS Seatbelt，直接运行时有 3 个系统级测试报 `sandbox_apply: Operation not permitted`；在允许 Seatbelt 的环境重跑相同离线测试全部通过。这不是忽略测试失败的理由，后续改动仍需在可运行 Seatbelt 的环境验证。
- 用户在自己的终端运行 `CODECREW_RUN_KIMI_LIVE=1` 的单文件真实冒烟测试并报告 `8 passed`；另运行 `CODECREW_RUN_KIMI_VERIFIER_LIVE=1` 的小型 Bug 修复及 Verifier 用例并报告 `3 passed`。这两项不等于三 Agent 端到端验收，也不能证明 `kimi-for-coding` 别名背后的具体模型版本。

## 尚未验证或尚未实现

- DeepSeek Reviewer 的真实在线调用、独立会话和审批输出；三真实 Agent 的完整任务、拒绝返工及完成守卫联调。
- UI 创建/取消任务、人工发消息/回答澄清与继续任务；后端尚无面向用户的消息写入 API。
- 对 Agent 真正不可见的隐藏测试隔离；现有 `hidden_tests` 检查类别和示例 `tests/hidden` 路径不代表保密。
- 主动诱导禁止命令时的真实拒绝验证；Kimi 的工具清单不含 `Bash`，但尚不能将其当作该测试的替代。
- 正式多语言任务集、单 Agent/团队对照实验及可靠的成本报告。Kimi CLI 当前未提供可靠的原生 Token 用量；缺失值不能记为零。
- 多 worker 派发锁、远程身份认证和运行不可信仓库所需的额外隔离。当前只支持可信本地、单进程使用。

## 从本基线继续

1. UI 创建/取消任务：调用现有 API，固定服务端团队配置，测试成功、冲突和不可用状态。
2. DeepSeek Reviewer 真实冒烟：验证只读、结构化结论与失败关闭。
3. 三 Agent 端到端：完成、返工、预算耗尽三条路径均由确定性证据判定。
4. UI 人工干预：增加受控的用户消息/回复/继续接口，再接聊天室输入框。
5. 安全与证据收口：禁止命令主动拒绝、隐藏测试隔离及越权检查。
6. 本地演示验收：从 UI 发起到 Patch/验证/审批报告，覆盖异常与重启恢复。
7. 固定团队预设、版本和预算元数据，为实验提供可追溯配置。
8. 实现同任务、同限制的单 Agent 与团队评测执行器及原始 JSONL。
9. 构建 12～15 条多语言、可自动判分的编码任务。
10. 运行对照实验，计算成功率、假完成率、回归率、越权率、返工和时延；成本只统计可核实数据。
11. 完善中文 README、演示、部署边界与简历材料。

每一步只合入一个可独立验证的增量，运行对应测试，并保留小步 Git 提交。下一步是 **1：UI 创建/取消任务**。
