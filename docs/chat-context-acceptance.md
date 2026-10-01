# P2.4.3：独立聊天室长聊验收

日期：2026-10-01。本步验证 P2.4.2 的显式 Human 背景引用；不扩展为自动摘要或
长期记忆，也不授权编码。

## 已完成的验收

1. **Fake 浏览器**：在临时 `chat-demo` 房间先发送旅游目标，再发送独立的日志话题。
   点击最初 Human 消息的“以此为背景继续”后，新的月见消息及她邀请的鲸鲸消息
   都显示同一背景引用。刷新页面后，消息、背景引用和成功回合仍在。该房间没有
   编码任务入口，临时目录只生成了 SQLite 文件。
2. **真实 Planner、HTTP**：可选测试
   `tests/integration/test_standalone_chat_context_live.py` 使用临时 SQLite，
   在一条目标消息与新提问之间插入 8 条无关消息，最多执行一次 Codex 回合。
   授权本机环境执行通过，trace 为 `249e97b6-aced-5eb3-bbcf-af2fa2b06795`，
   证据保存在 `evals/results/chat-context-live/`（被 Git 忽略）。模型输入包含
   早期验收代号，不包含无关话题；回复包含该代号。`/api/v1/tasks` 返回 503，
   运行目录在回合后清空。首次在本任务沙箱内尝试时 CLI 立即以 exit=1 失败，
   不计为通过；随后在授权本机环境重跑通过。
3. **真实 Planner、浏览器**：`tests/manual_chat_context_live.py` 启动临时真实房间，
   预置一条原始目标和 8 条无关 Human 消息，不为预置消息调用模型。浏览器选择
   第一条消息作背景后，仅发送一次 `@白金`；页面显示真实回复
   `ANCHOR_BLUE_73`、成功回合和背景引用。刷新后仍可读取。
   房间 ID 为 `c41db665-8a06-5734-93b9-50355b49b0d1`；该临时房间在服务
   停止后删除，ID 用于记录本次过程，不是持久演示链接。

浏览器验收中发现已成功的回合旁仍可能保留“Agent 正在处理”提示；本步修正为
成功时显示“Agent 已回复，可以继续讨论”，并补了前端脚本断言。

## 可复验命令

在配置好 Codex CLI 的本机终端运行：

```bash
CODECREW_RUN_CHAT_CONTEXT_LIVE=1 .venv/bin/pytest -q -s tests/integration/test_standalone_chat_context_live.py
.venv/bin/python tests/manual_chat_context_live.py --port 8768
```

第二条命令会打印临时房间地址及一条浏览器测试消息。只点击第一条 Human 消息的
背景按钮，再发送该消息；按 Ctrl-C 结束并清除临时数据库。不要在测试文本中填写
密钥。若本机端口已被占用，可选另一空闲端口。

## 边界

本步真实浏览器只复测了 Codex Planner；Kimi 与 DeepSeek 的真实接话在此前 P2
最小验收中完成，这里没有再次消耗其模型回合。Fake 场景和离线定向测试覆盖
月见→鲸鲸的背景传递，但**不**宣称本步验证了三模型真实长聊。
当前只引用一条 Human 消息的最多 240 字符，不会自动提炼“已确认条件”；超过
截断范围的约束仍需要 Human 在当前消息中重述或另选合适背景。
