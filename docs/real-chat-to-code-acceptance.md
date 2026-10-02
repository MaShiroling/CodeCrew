# P3.5 真实三 Agent 小修复验收

此步观察**一次**真实模型执行：Codex CLI（白金/Planner）、Kimi Code CLI
（月见/Implementer）、Claude Code 接 DeepSeek（鲸鲸/Reviewer）。它不是成功率评测。
任务使用自动创建的临时 Git 仓库，仅修改 `src/app.py` 的 `value = 1` 为
`value = 2`；默认只读聊天，Human 必须单独预检并确认一次授权。Verifier
独立运行语法、公开 pytest、占位隐藏断言和权限检查。占位隐藏断言对本机操作者
不保密，不能作为真正抗作弊隐藏测试。

## 运行准备

仅在可信本机 macOS 运行，Kimi 实现边界需要 Seatbelt。先确认 `codex`、`kimi`、
`claude` 三个 CLI 可用；如 Codex CLI 在 ChatGPT App 中，可在当前终端设置：

```bash
export CODECREW_CODEX_CLI_PATH="/Applications/ChatGPT.app/Contents/Resources/codex"
```

如果默认路径不同，用 `CODECREW_KIMI_CLI_PATH` 和 `CODECREW_CLAUDE_CLI_PATH`
指定本机实际可执行文件。Kimi Code **会员密钥**放在当前终端的
`KIMI_MODEL_API_KEY`，DeepSeek 密钥放在 `DEEPSEEK_API_KEY`；不要把密钥写入
仓库、命令参数、截图或聊天内容。例如 zsh 可逐个安全输入：

```zsh
read -rs 'KIMI_MODEL_API_KEY?Kimi Code 密钥: '; echo
export KIMI_MODEL_API_KEY
read -rs 'DEEPSEEK_API_KEY?DeepSeek 密钥: '; echo
export DEEPSEEK_API_KEY
```

只检查是否已设置，不打印值：

```zsh
[[ -n "$KIMI_MODEL_API_KEY" && -n "$DEEPSEEK_API_KEY" ]] && echo "密钥环境变量已设置"
```

## 一次自动验收

这条命令是显式的真实模型开关，可能消耗多个模型回合；不要在未准备好时运行：

```bash
CODECREW_RUN_CHAT_TO_CODE_LIVE=1 .venv/bin/pytest -q -s tests/integration/test_chat_to_code_live.py
```

测试通过同一 HTTP 应用创建独立房间并让真实白金只读回复；检查聊天未创建编码任务，
然后以本房间 Human 消息为来源预检、单次授权固定目标，等待真实三角色完成受控
任务。成功判据同时要求：`src/app.py` 是唯一变更；语法、公开测试、占位隐藏检查
与权限检查通过；Reviewer 批准；CompletionGuard 通过；最终 Patch 可下载；
原始 Git 仓库保持干净。自然语言“已完成”不计入判据。失败也不会伪装成成功。

测试结束时在被 Git 忽略的 `evals/results/chat-to-code-live/` 保存一个权限为 0600 的
脱敏 JSON：trace ID、角色、状态、检查结果、证据 Artifact ID、Patch SHA-256 与字节数，
不保存原始 Agent 回复、CLI 日志或密钥。测试临时仓库和完整 Artifact 会被清理，
因此 JSON 是验收摘要，不是可恢复运行的完整证据包；分享前仍应人工核查。

## 浏览器手动演示

同一终端也可运行：

```bash
.venv/bin/python -m app.cli live-demo --port 8000
```

打开 `http://127.0.0.1:8000/ui/chat/`，新建房间，发送
`@白金 请只读讨论：把 src/app.py 的 value 从 1 改为 2；不要修改文件`。
待聊天回复后，在这条 Human 消息旁打开受控编码面板；临时仓库路径和固定目标已预填。
先预检，核对 Git 基线和 `src` 写入范围，再勾选确认并授权。点击任务链接，在工作台
查看 Diff、测试、Review、完成守卫与 Patch。该入口只允许本次临时仓库和固定目标，
禁用直接创建任务；不会因为 Agent 在聊天里表示“可以改”而授予写权限。停止服务后
临时数据删除，请先下载 Patch 或留存必要截图。浏览器操作不能代替上述自动验收。

2026-10-02 用户已在配置好密钥的本机终端运行在线测试。任务
`faac33fe-5198-42e0-bbb5-d453eb9a1a0b` 对应 trace
`9b01b794-65fb-428a-9bd3-53326a859468`；脱敏摘要为 `accepted`，任务
`completed`。三角色、确定性检查、独立 Review、完成守卫、Patch 和原始仓库干净
状态均符合本页判据。该结果仅是一次 HTTP 自动验收；真实浏览器手动演示、其它任务
类型和统计成功率尚未验收。
