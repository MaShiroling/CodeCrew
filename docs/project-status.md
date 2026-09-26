# 项目状态与后续路线（2026-09-26）

本页是当前开发基线；历史阶段编号只记录实现顺序，不代表真实模型或安全边界已经验收。

## 已实现

- 任务状态机、结构化 Agent 动作与 TeamRoom 消息、Plan 版本链、Reviewer 返工预算。
- 独立 Git Worktree、路径/命令策略、确定性 Verifier、CompletionGuard 和证据 Artifact。
- SQLite 持久化、Trace/SSE、保守的启动恢复，以及可显式配置的本地单进程任务 API。
- `/ui/` 工作台：任务列表、聊天室、Plan、Artifact 预览、实时状态，以及创建/取消任务操作。页面尚未接入人工消息操作。
- Codex CLI、Claude Code、Kimi Code CLI 与 Fake Agent 适配路径；DeepSeek Reviewer 已通过离线测试和用户本机独立在线冒烟。

## 验证基线

- 本次在不启用付费真实模型测试的条件下运行以下命令：离线测试通过，显式启用的在线用例跳过；Ruff 通过。

  ```bash
  env CODECREW_RUN_KIMI_LIVE=0 CODECREW_RUN_KIMI_VERIFIER_LIVE=0 CODECREW_RUN_CLI_INTEGRATION=0 CODECREW_RUN_DEEPSEEK_REVIEWER_LIVE=0 .venv/bin/pytest -q
  .venv/bin/ruff check .
  ```
- 受限执行环境不允许嵌套启动 macOS Seatbelt，直接运行时有 3 个系统级测试报 `sandbox_apply: Operation not permitted`；在允许 Seatbelt 的环境重跑相同离线测试全部通过。这不是忽略测试失败的理由，后续改动仍需在可运行 Seatbelt 的环境验证。
- 用户在自己的终端运行 `CODECREW_RUN_KIMI_LIVE=1` 的单文件真实冒烟测试并报告 `8 passed`；另运行 `CODECREW_RUN_KIMI_VERIFIER_LIVE=1` 的小型 Bug 修复及 Verifier 用例并报告 `3 passed`。这两项不等于三 Agent 端到端验收，也不能证明 `kimi-for-coding` 别名背后的具体模型版本。
- UI 创建/取消已通过同进程 API 回归测试；真实浏览器分别检查了默认应用的 `503` 提示和临时受控工作流中的创建、详情、取消。该浏览器检查未调用真实模型。
- DeepSeek Reviewer 第 1～2 子步骤预检和固定证据夹具已通过。工具配置不等于操作系统级只读保证，额外断言不具备隐藏测试隔离。
- DeepSeek Reviewer 在线用例首次因 Markdown JSON 包装失败；有限格式兼容修复后，2026-09-26 用户在同一终端重跑并报告单条用例通过。有效证据获批、无 Diff 被拒绝、独立原生会话、允许工具和文件哈希断言均通过。桌面任务进程没有代为调用，也未确认实际远端模型版本。
- 第 4 子步骤已收尾：统一离线入口 `scripts/check_offline.py`、验收来源/缺失原始证据/成本限制及下一阶段聊天运行时条件已记录。独立 Reviewer 冒烟不覆盖聊天室动作协议或三 Agent 完整闭环。

## 尚未验证或尚未实现

- 实际远端模型版本确认；三真实 Agent 的完整任务、拒绝返工及完成守卫联调。
- UI 人工发消息/回答澄清与继续任务；后端尚无面向用户的消息写入 API。
- 对 Agent 真正不可见的隐藏测试隔离；现有 `hidden_tests` 检查类别和示例 `tests/hidden` 路径不代表保密。
- 主动诱导禁止命令时的真实拒绝验证；Kimi 的工具清单不含 `Bash`，但尚不能将其当作该测试的替代。
- 正式多语言任务集、单 Agent/团队对照实验及可靠的成本报告。Kimi CLI 当前未提供可靠的原生 Token 用量；缺失值不能记为零。
- 多 worker 派发锁、远程身份认证和运行不可信仓库所需的额外隔离。当前只支持可信本地、单进程使用。

## 从本基线继续

1. DeepSeek Reviewer 四个子步骤已完成，详见独立冒烟验收记录；保持实际模型版本和强只读隔离限制。
2. 三 Agent 端到端：先显式团队配置与 `AgentTurnRunner` 聊天协议预检，再验证完成、返工、预算耗尽三条路径，均由确定性证据判定。
3. UI 人工干预：增加受控的用户消息/回复/继续接口，再接聊天室输入框。
4. 安全与证据收口：禁止命令主动拒绝、隐藏测试隔离及越权检查。
5. 本地演示验收：从 UI 发起到 Patch/验证/审批报告，覆盖异常与重启恢复。
6. 固定团队预设、版本和预算元数据，为实验提供可追溯配置。
7. 实现同任务、同限制的单 Agent 与团队评测执行器及原始 JSONL。
8. 构建 12～15 条多语言、可自动判分的编码任务。
9. 运行对照实验，计算成功率、假完成率、回归率、越权率、返工和时延；成本只统计可核实数据。
10. 完善中文 README、演示、部署边界与简历材料。

每一步只合入一个可独立验证的增量，运行对应测试，并保留小步 Git 提交。下一步是 **三真实 Agent 联调的第 1 子步骤：显式团队配置与聊天协议预检**。
