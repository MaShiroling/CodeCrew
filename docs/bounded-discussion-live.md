# P6.6 有界自由接话：真实三模型验收

当前状态：自动验收入口和 Fake 同路径对照已就绪；真实三模型连续批次尚未在
已配置密钥的终端运行通过。不要把旧的一次性聊天室在线测试当成本项结果。

## 前提与运行

仅在可信的本机 macOS 运行；Kimi 的独立只读环境依赖 Seatbelt。确保 `codex`、
`kimi`、`claude` CLI 可执行。Codex 若不在 PATH，可在当前终端设置：

```zsh
export CODECREW_CODEX_CLI_PATH="/Applications/ChatGPT.app/Contents/Resources/codex"
```

同一终端还须有 `KIMI_MODEL_API_KEY`（Kimi Code 会员密钥）和 `DEEPSEEK_API_KEY`。
不要把密钥写入命令、仓库、聊天消息或截图；无回显设置示例见
[独立聊天室在线验收](standalone-chat-live.md#准备)。验收最多花费三个真实 Agent
回合，最长 600 秒，不要与其它在线测试并行运行：

```zsh
CODECREW_RUN_BOUNDED_DISCUSSION_LIVE=1 .venv/bin/pytest -q -s tests/integration/test_bounded_discussion_live.py::test_three_real_agents_handoff_in_bounded_room
```

脚本通过真实本地 HTTP API 创建无仓库房间，明确开启三回合有界批次，首位是白金。
消息请白金按顺序邀请月见、鲸鲸讨论一个无文件的轻量话题。验收读取 SQLite/API
的实际批次、消息和回合，而不是模型自报：三角色按顺序各成功一回合，回复的
`correlation_id`、`reply_to` 与因果链相符，批次以结束、待人工或回合上限安全停机；
编码任务 API 不可用，私有执行目录已清空。结构化交接若未发生、格式错误、CLI
超时或触发总时限，测试应失败并显示实际状态，不把它算作通过。

无论通过还是断言失败，只要房间已经创建，脚本都在 Git 忽略的
`evals/results/bounded-discussion-live/<trace_id>.json` 保存房间、批次、消息、回合
快照。该文件可能含模型原始回复，不要上传或公开分享；需要排查时先检查并脱敏。
预检发现 CLI 或环境变量缺失时不会启动模型，也不会生成批次证据。

## 浏览器手动复核

自动测试通过后，仍需单独验证网页交互。在同一已配置终端运行
`.venv/bin/python -m app.cli chat-serve --port 8000`，打开
`http://127.0.0.1:8000/ui/chat/`。创建新房间，勾选“开启有界接话”，首位选白金，
设置 3 回合、600 秒，在内容里写不含 `@` 的讨论目标并要求白金依次邀请月见、
鲸鲸。检查右栏预算和停止原因、中间三位回复的顺序、刷新后仍能读到持久化结果；
不要在批次运行中重复发送同一目标。暂停/继续/取消属于独立控制分支，自动脚本
不会用一次正常结束的结果宣称这些真实 CLI 分支已验收。
