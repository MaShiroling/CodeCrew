# 独立聊天室：真实三模型在线验收（P2）

当前状态：真实三模型 HTTP 冒烟已经通过；浏览器已看到三者接话，但曾出现
重复交接后的白金超时。P2.1 去重、P2.2 有限上下文已离线测试；P2.3 已完成
Fake 浏览器验收和实时刷新回归，**修改后的真实浏览器复测待运行**。
`chat-demo` 只使用 Fake Agent；真实模型需 `chat-serve`。

## 准备

仅在可信的本机 macOS 环境运行；Kimi 只读执行依赖 Seatbelt。确保 `codex`、
`kimi`、`claude` 三个 CLI 可执行。若 CLI 不在 PATH，可在启动服务的**同一终端**
设置 `CODECREW_CODEX_CLI_PATH`、`CODECREW_KIMI_CLI_PATH`、
`CODECREW_CLAUDE_CLI_PATH`。不要把密钥写进命令、聊天消息、仓库或截图。

Kimi Code 会员密钥通过 `KIMI_MODEL_API_KEY`、DeepSeek 审批模型密钥通过
`DEEPSEEK_API_KEY` 注入服务进程环境。Codex CLI 使用它自己的登录状态。
如果当前终端没有密钥，请在本机使用无回显输入，再导出环境变量；示例为 zsh：

```zsh
read -rs 'KIMI_MODEL_API_KEY?Kimi Code 密钥: '; echo
export KIMI_MODEL_API_KEY
read -rs 'DEEPSEEK_API_KEY?DeepSeek 密钥: '; echo
export DEEPSEEK_API_KEY
```

不要把真实密钥粘贴到本项目的 Issue、聊天页面或测试命令里。

## 先运行可复查的 HTTP 在线冒烟

```bash
CODECREW_RUN_STANDALONE_CHAT_LIVE=1 .venv/bin/pytest -q -s tests/integration/test_standalone_chat_live.py
```

测试最多消耗六个真实模型回合。它会创建临时无仓库房间、同时提及三位 Agent，
核对三种角色均回复、回合成功、没有编码 Task 或遗留工作目录，并把房间/消息/
回合快照保存到忽略 Git 的 `evals/results/standalone-chat-live/`。若失败，保留的
状态和安全错误分类可用于归因；不要把原始密钥加入证据文件。

## 再做浏览器验收

在同一终端运行：

```bash
.venv/bin/python -m app.cli chat-serve --port 8000
```

打开 `http://127.0.0.1:8000/ui/chat/`，新建一个房间，发送：

```text
@白金 @月见 @鲸鲸 请各讨论给 Python 函数增加输入校验时的兼容性和测试边界；只讨论，不修改文件。
```

验收时逐一检查：右侧回合从排队/运行进入成功或明确失败，三位 Agent 的回复出现在
中间时间线；所有回合已回复时顶部不再显示“聊天中”；刷新页面后消息仍在；
没有要求 Git 路径，也没有编码 Task。当前页面的
“实时”指 SSE 通知后读取持久化消息/状态，**不是逐 Token 输出**。断线时页面
会提示并每 10 秒轮询；取消不确定时应显示中断而非成功。

真实浏览器验收结果应更新[精简路线](portfolio-roadmap.md)；只有在线测试和浏览器
核对都通过，才把 P2 标为完成。
