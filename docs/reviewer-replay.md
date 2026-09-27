# Reviewer 历史输出离线回放

统一路线第 2 项：用当前 Reviewer 契约检查历史输出，不重新运行 Agent。
此功能不修补回复、不续跑任务、不写入原归档，不产生审批或成功证据。

## 可重复的离线回归

在项目根目录执行：

```bash
.venv/bin/python -m scripts.replay_reviewer
.venv/bin/pytest -q tests/test_reviewer_replay.py tests/test_reviewer_contract.py
```

默认读取版本化的 `tests/fixtures/reviewer_replay.json`，共 6 个案例：

- 历史原生返工遗漏报告来源；历史文本报告括号错位。
- 历史合法返工及问题已解决的批准。
- 人工构造的双报告来源、原生对象缺失但备用文本合法两个反例。

这些是**人工最小化的协议样例，不是逐字原始模型回复**。大段证据说明已替换，
问题列表可缩减；`provenance` 记录来源 Trace、原 Artifact ID/哈希及改动。
来源哈希属于原 AgentResult Blob，不是缩减样例的哈希。两个人工反例单独标注。
它们可随开源仓库运行，不依赖本机私有归档，也不能用于模型质量评测。

输出 JSON 包含各案例的 `decision`、稳定的 `reason` 和 `expectation_met`；
`replay_passed=true` 仅表示全部判断符合样例预期，拒绝错误输出也是回归通过。
测试还把这些输出送入生产 `AgentTurnRunner`，使用 Fake Agent 验证：
失败保留原文、输入不 ACK、不创建 ReviewReport、不新增消息、无自动重试；
合法输出可路由，但回合结束本身不将任务标记成功。

## 逐字回放本机真实归档

```bash
.venv/bin/python -m scripts.replay_reviewer \
  --archive evals/results/three-agent-rework_exhaustion-live/6613fe69-be35-4282-b10e-9f5afcb5b276-tawidrf5 \
  --source native
```

`--source text|native` 必须显式指定，因为旧归档未完整记录原生输出配置。
原生模式不会因缺少 `structured_output` 而改读 `result`，也不会推测文本回退。

读取步骤：

1. 检查归档布局和数据库 SHA-256；不采信 manifest 的“已验证”标记。
2. 用只读、immutable SQLite 连接检查数据库、全部登记 Artifact 的元数据/长度/哈希。
3. 从 `agent_output_recorded` 事件定位 Reviewer 输出，检查任务、Trace、会话及 Artifact 引用。
4. 读取归档聊天室成员，用当前生产解析器逐字检查输出；失败 Agent 不因 JSON 合法而通过。

禁止初始化数据库、开启 WAL、清理归档、ACK、写 Trace 或调用适配器。
拒绝符号链接、缺失/损坏文件、非空 WAL/事务日志；已有空 WAL 和 SHM 可保留，
immutable 读取忽略它们，不删除或修改。只支持静止的可信本地冒烟归档；
SHA-256 完整性检查不是签名或来源真实性认证，不能把活动数据库当作归档。
CLI 报告只输出 ID、哈希、判断及语法坐标，不回显原回复、stderr、密钥或私有说明。

### 2026-09-27 本机回放结果

| 原 Trace | 完整性核验 | 当前契约判断 |
| --- | --- | --- |
| `6613fe69-be35-4282-b10e-9f5afcb5b276` | 数据库和 40 个 Artifact | 原生返工遗漏来源，拒绝 |
| `bf0b59b8-6e00-4ab6-a755-c53993f244bc` | 数据库和 40 个 Artifact | 文本 JSON 错误，拒绝；原文第 1 行第 1200 列、偏移 1199 |
| `5bb1332e-666f-4d34-8a0a-57998df8c43f` | 数据库和 62 个 Artifact | 原始返工及批准两份输出均通过格式契约 |

原归档保持不变；本次未调用真实模型。这不新增在线成功或两轮耗尽验收结论。

## 判断范围与退出码

- `accepted` 只说明**当前输出格式和静态规则**通过，不代表 Reviewer 结论正确。
- 不重新检查历史问题 ID 是否完整结转、证据是否实际读取、工作区权限或测试结果；
  这些仍由生产路由、Verifier 和 CompletionGuard 判断。
- `archive_integrity_verified=true` 只表示所读取归档通过完整性检查，不意味着任务成功。
- 退出码 `0`：样例预期全部满足，或真实归档已成功读取并回放（允许输出被拒绝）。
- 退出码 `1`：样例判断不符合预期，表示回归失败。
- 退出码 `2`：参数/输入/归档无效，不能形成可信回放结果。

下一项的[仅 Reviewer 低成本在线验收入口](reviewer-chat-smoke.md)现已开发，在线验收仍待运行；
不是直接重跑最多 9 回合的完整链路。
