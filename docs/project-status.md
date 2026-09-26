# 项目状态与后续路线（2026-09-26）

本页是当前开发基线；历史阶段编号只记录实现顺序，不代表真实模型或安全边界已经验收。

## 已实现

- 任务状态机、结构化 Agent 动作与 TeamRoom 消息、Plan 版本链、Reviewer 返工预算。
- 独立 Git Worktree、路径/命令策略、确定性 Verifier、CompletionGuard 和证据 Artifact。
- SQLite 持久化、Trace/SSE、保守的启动恢复，以及可显式配置的本地单进程任务 API。
- `/ui/` 工作台：任务列表、聊天室、Plan、Artifact 预览、实时状态，以及创建/取消任务操作。页面尚未接入人工消息操作。
- Codex CLI、Claude Code、Kimi Code CLI 与 Fake Agent 适配路径；DeepSeek Reviewer 已通过离线测试和用户本机独立在线冒烟。
- 三 Agent 联调第 1 子步骤：显式白金/Codex、月见/Kimi、鲸鲸/DeepSeek 配置；角色动作提示与严格 JSON 包装兼容；生产接线的离线聊天协议测试。
- 第 2 子步骤开发、离线和用户本机在线验收通过：本轮 Artifact 逐文件只读授权、完整性复核，以及 Planner → 澄清 → Plan v2 → Implementer → Verifier 的固定夹具。
- 第 3 子步骤已开发：生产事件循环驱动五回合成功路径、Reviewer 显式证据引用、独立评审及 CompletionGuard；已加入模拟失败反例和显式开启的真实用例，三模型在线验收已尝试但尚未通过。
- 聊天动作受控输出归一化：唯一 JSON 代码块可带外围说明，歧义输出仍拒绝；原始 AgentResult 保存为诊断 Artifact 并通过 Trace 引用，未增加模型重试。
- 后续路线第一部分第 1 步：Reviewer 提示明确区分输入证据和输出 ReviewReport，推荐新报告仅用 `artifact_content`，附批准/返工示例；保留已有报告引用路径及来源二选一校验，不自动删除冲突字段。

## 验证基线

- 后续路线第一部分第 1 步定向回归、全量 `scripts/check_offline.py` 与 Ruff 均通过。
  批准/返工的提示示例可由生产解析器接受；新报告与已有报告来源的动作结构各自合法，
  同时提供或都缺失仍拒绝。生产事件循环模拟本次 Reviewer 双来源失败：保持输入未 ACK，
  不发布评审消息、不进入完成守卫，原始输出可通过 Trace/Artifact 追查。
  本次没有调用真实模型，三 Agent 在线成功路径仍待第 2 步验收。

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
- 三 Agent 联调第 1 子步骤的定向测试及全量 `scripts/check_offline.py` 已通过（全量在允许 Seatbelt 的环境重跑）；真实适配器解析模拟 CLI 输出，验证人格、角色权限、会话、Artifact、路由与 ACK。未调用模型，也未运行完整任务闭环。
- 第 2 子步骤定向用例及最终全量离线入口均通过，Ruff 通过；新增 Seatbelt 文件级读取/写入拒绝测试通过。在线用例显式跳过，尚无真实双 Agent 联调结果。
- 用户在线联调首次受 CLI PATH 影响，修正后在 Planner 回合遇到重连错误误判。独立 Codex 诊断显示 HTTPS 回退后有 `turn.completed`；已修复适配器并加入离线回归，明确失败/缺少完成/非零退出/超时/取消仍拒绝。该次独立诊断不等于双 Agent 联调通过，在线验收仍需重跑。
- 本次 Codex 重连修复的定向测试、全量 `scripts/check_offline.py` 和 Ruff 通过；全量在允许 macOS Seatbelt 的环境运行，所有真实模型测试标志关闭。保留现有依赖弃用警告，未调用模型或变更网络设置。
- 后续用户在线运行已进入 Kimi 澄清回合，但混合说明文字与 JSON 的最终回复被拒绝。已加强 Kimi 系统提示和聊天室末尾输出契约，未放宽解析/ACK/权限规则；提示注入的定向测试、全量离线测试和 Ruff 通过，真实模型遵守情况及完整交接仍待验收。
- Kimi 混合输出专项离线回归、全量 `scripts/check_offline.py` 和 Ruff 已通过：合法 JSON/完整 JSON 代码块可交接；混合文字、多代码块、纯文字提问声明和未知字段被拒绝，消息保留未 ACK，无新增路由/Plan/Artifact，也无自动重试。真实双 Agent 联调仍需在已配置终端重跑。
- 上述历史待验收状态已更新：用户重跑第 2 子步骤并提供通过输出，trace_id 为 `5f646b43-1d20-4278-825e-016c22398d60`。四回合双 Agent 交接及 Verifier 已通过；`task_success=false` 为该用例预期，不代表三模型完整任务成功。
- 第 3 子步骤定向回归和全量 `scripts/check_offline.py` 通过，Ruff 通过；全量在支持 Seatbelt 的环境运行，六个在线标志全部关闭。完成、假批准、拒绝、只读/证据/工具/格式异常以及 ACK 队列回归均为模拟 CLI 输出；真实五回合结果仍待验收。
- 用户已尝试五回合在线验收，但 Kimi 澄清回合仍返回说明加 JSON，尚未到达鲸鲸。现加入受控包装归一化，定向模拟测试验证混合包装可执行、歧义不 ACK、原文审计以及权限/高优先级守卫未放宽；全量 `scripts/check_offline.py` 和 Ruff 通过，六个在线标志关闭，三模型在线验收仍待重跑。

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

每一步只合入一个可独立验证的增量，运行对应测试，并保留小步 Git 提交。
三真实 Agent 联调第 2 子步骤已通过用户本机在线验收。
最新用户在线运行已到 Reviewer，但其审批动作同时提供了输入证据 `artifact_ids` 和新报告
`artifact_content`，被来源二选一校验拒绝，未进入 CompletionGuard。这说明该次 Kimi 输出
归一化已不再阻挡流程，不代表整个任务成功。原始输出仍保存为诊断 Artifact。

下一行动按统一后续路线为 **第一部分第 2 步：验收真实成功闭环**：在配置密钥的同一
终端重跑五回合在线成功路径，确认新 Reviewer 契约在真实模型中的遵守情况并归档证据。
通过后进入返工/预算联调；真实模型结果不能用离线模拟替代。
本任务进程没有 Kimi 密钥，未代为调用模型。详见[联调说明](three-agent-integration.md)。
