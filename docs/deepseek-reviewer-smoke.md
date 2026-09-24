# DeepSeek Reviewer 真实冒烟：分步验收

目前已完成 **子步骤 1～2**，均不发起模型请求；离线检查不能算作 DeepSeek Flash
已接通。后续子步骤每次独立验证和提交。

## 子步骤 1：本地预检（已完成）

```bash
.venv/bin/pytest -q tests/test_claude_adapter.py tests/test_deepseek_cli_preflight.py
```

- 检查已安装的 `claude --help` 是否仍提供 Adapter 使用的命令选项；若 CLI 未安装，
  该本机兼容性用例跳过，不能视作已验证。2026-09-25 本机版本为 `2.1.246`。
- 模拟子进程测试固定 Reviewer 角色、`Read/Glob/Grep` 工具、`plan` 权限、
  safe mode、禁用斜杠命令、空 MCP 配置，以及新建会话不携带 `--resume`。
- 模拟环境测试仅将 `DEEPSEEK_API_KEY` 映射为子进程的 `ANTHROPIC_AUTH_TOKEN`；
  命令行不含密钥，其他供应商的 API Key 不会传入。没有密钥时启动失败。
- Adapter 使用的地址和模型变量与
  [DeepSeek 官方 Claude Code 接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)
  一致。官方文档可能更新，真实冒烟前需再次核对。

这些是**配置和 CLI 参数检查**，不是模型身份、文件系统强隔离或在线调用证明。
`--tools` 限制 Agent 可用的内置工具；当前 Reviewer 没有独立的操作系统只读沙箱，
不能据此断言子进程绝对无法写文件。在线测试仍须使用临时仓库、检查实际工具事件与
测试前后的文件状态。CLI 仍继承受限列表中的 `HOME` 等基础环境变量；预检不检查
用户认证文件，更不把本地已有的 Claude 账户当成 DeepSeek 鉴权证据。

当前 Codex 任务进程没有 `DEEPSEEK_API_KEY`。真实测试应由用户在同一个终端导出
**DeepSeek Platform API Key** 后显式运行；只检查变量是否非空，不打印或提交密钥。

## 子步骤 2：固定证据与离线失败关闭（已完成）

```bash
.venv/bin/pytest -q tests/test_deepseek_reviewer_smoke.py
```

测试在临时 Git 仓库中放入有缺陷的 `total()`、公开 pytest 用例和测试驱动内的额外
断言。先确认种子版本的 Diff、公开测试与额外断言失败，再在独立 Worktree 中修复一处
源文件，由确定性 Verifier 生成真实 Diff、权限报告及静态/公开/额外检查证据；Plan 和
证据均保存为 Artifact。额外断言只是不在仓库内的测试，**并非安全隔离的隐藏测试**。

模拟 Claude Code 流式结果经真实 `DeepSeekClaudeReviewerAdapter` 和
`AgentReviewerRunner` 跑两次，验证新会话、只读命令、证据引用和批准/拒绝 JSON 解析。
无效 JSON、未知结论、非法 issue 字段、CLI 报错与异常退出均产生
`ReviewerExecutionError`，不会变成批准。本阶段的批准/拒绝文本是预置测试数据，
不是 DeepSeek 的判断。证据文件位于 Worktree 外的 ArtifactStore；路径在 prompt 中，
真实 Claude Code 是否能读取仍须在在线冒烟时验证。

## 后续子步骤

3. 显式启用真实 DeepSeek 回合：先测证据充分的审批，再测证据不足时的拒绝；核查
   实际工具事件、仓库是否被改动，以及能够核实的 provider/模型信息。无法确认的
   实际模型版本必须标记为“未验证”。这一步会消耗 API 额度。
4. 运行离线回归与静态检查，记录结果和限制，更新项目状态。Reviewer 批准始终不
   替代 Verifier 与 CompletionGuard 的确定性成功判定。

只有子步骤 3 的真实调用成功，才能声称 DeepSeek Reviewer 已通过在线冒烟；
三真实 Agent 的完整任务仍是后续独立验收。
