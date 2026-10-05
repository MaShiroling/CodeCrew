# 飞书应用机器人配置与本地运行

v1 已实现只读文本桥接。离线 Fake/集成测试已完成；真实飞书长连接烟雾测试尚未执行。详细证据和 Windows 基线见[验收记录](feishu-acceptance.md)。生产 Agent 继续使用项目原有 macOS/Unix 隔离条件，本次没有把真实 Agent 运行时移植到 Windows。

## 飞书控制台

1. 在[飞书开发者后台](https://open.feishu.cn/app)创建企业自建应用，启用应用的**机器人**能力。此处需要可收事件的应用机器人。
2. 在凭证页获取该应用的 App ID/App Secret，仅注入本机进程环境。不要将凭据放入聊天、截图、Git 或测试结果。
3. 按[官方 Python SDK 事件说明](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events)选择长连接接收事件。配置过程中若控制台要求建立连接，可先完成下方安装/环境配置并启动一次单实例服务。不要同时运行 smoke 和服务。
4. 订阅[接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive) `im.message.receive_v1`。事件订阅名与权限 scope 是不同字段。
5. 只申请本版本需要的应用身份消息权限；以下名称**需按当前飞书控制台确认**，本次没有登录真实控制台核验：

   | 能力 | 待核对的权限名称 |
   | --- | --- |
   | 接收用户给机器人的单聊文本 | `im:message.p2p_msg:readonly` |
   | 接收群内真实 @机器人的消息 | `im:message.group_at_msg:readonly` |
   | 以应用机器人发送和回复文本 | `im:message:send_as_bot`；并核对[消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/create)及回复接口当前要求 |

   不要为了让群消息“有反应”而扩大到读取所有群消息，不需要通讯录同步、文件、云文档或卡片权限。
6. 发布应用版本或配置开发测试范围，确保测试成员可见且有权使用；把机器人加入指定测试群。权限修改是否需重新发布，以当前控制台提示为准。
7. 通过官方[获取机器人信息](https://open.feishu.cn/document/client-docs/bot-v3/obtain-bot-info) API 调试器，用**本应用身份**确认机器人的 `open_id`。不要将 App ID、普通成员 open_id 或其他机器人 ID 填成 BOT_OPEN_ID。本版不自动查询；未配置时所有群消息拒绝，DM 仍按白名单工作。
8. 在应用官方调试/事件页面确认测试 DM 的 chat_id、测试群 chat_id，以及测试用户在本应用下的 open_id。以 `chat_id` 和 `open_id` 精确填白名单，显示名不是授权身份。不要为获取 ID 而临时开放通配白名单，也不要复制完整原始事件到日志。

## 安装与配置

在仓库根目录，使用原项目支持的 Python 3.11+ 环境：

```bash
python -m pip install -e '.[feishu,dev]'
```

普通 `.[dev]` 不依赖 SDK；extra 固定 `lark-oapi==1.7.3`。示例以下只含占位符，替换后在终端环境中设置；不要把真实值保存到仓库文件。

```bash
export CODECREW_FEISHU_ENABLED=true
export CODECREW_FEISHU_APP_ID='cli_REPLACE'
export CODECREW_FEISHU_APP_SECRET='REPLACE_FROM_SECRET_STORE'
export CODECREW_FEISHU_BOT_OPEN_ID='ou_REPLACE_BOT'
export CODECREW_FEISHU_ALLOWED_CHAT_IDS='["oc_REPLACE_DM","oc_REPLACE_GROUP"]'
export CODECREW_FEISHU_ALLOWED_SENDER_OPEN_IDS='["ou_REPLACE_USER_A","ou_REPLACE_USER_B"]'
export CODECREW_FEISHU_MAX_OUTBOX_ATTEMPTS=5
export CODECREW_FEISHU_RETRY_BASE_SECONDS=2
export CODECREW_FEISHU_RETRY_CAP_SECONDS=60
python -m app.cli chat-serve --feishu --port 8000
```

必须同时有 CLI `--feishu` 和 enabled=true。两个白名单均不能空，不支持 `*`；允许条件是 chat 和 sender **同时**匹配。BOT_OPEN_ID 为空只关闭群入口。模型 CLI 与凭据仍按[独立聊天室配置](standalone-chat-live.md)单独准备；飞书 App Secret 不传给 Agent CLI。

仅运行一个父服务进程、一个实例、一个数据库；CLI 固定 localhost 和 workers=1。SDK 长连接使用一个仅做事件接收的子进程，这不代表支持多 worker。不要用 reload、多 uvicorn workers、两个服务或与 smoke 同时连接同一应用。普通 `chat-serve`、`chat-demo`、`demo-serve` 不开启飞书，即使环境 enabled=true。

## 状态与手动验收

打开本机 [聊天室](http://127.0.0.1:8000/ui/chat/)和[只读状态](http://127.0.0.1:8000/api/v1/feishu/status)。状态只有 enabled、connection_state、binding_count、pending_outbox_count、retry_count、failed_count 和固定 last_error 分类，不回显凭据/ID/正文。

1. 白名单成员 DM 机器人发送“讨论输入校验方案”；默认白金开场。要指定其他角色，正文用 `@月见` 或 `@鲸鲸`；也支持现有 `@codex`、`@kimi`、`@deepseek` 等别名。未知 @ 会拒绝。
2. 在白名单群中从飞书提及选择器**真正选择当前机器人**，然后输入“@白金 讨论接口边界”。单纯输入字符串 `@CodeCrew`、@all 或选择其他 bot 都不触发。
3. 确认网页外部消息标签不是“我”；同群两名测试成员即使同名也有不同短标识。身份可见不等于授权。
4. 检查多个 Agent 回复按序分条发送，含角色和短讨论标识；A→B→A 不得漏掉第二条 A。实际接话由原 bounded 决策控制，不保证每次一定三人参与。
5. 当前 run 为 created/running/paused 时再发一条，收到一次 busy 提示；新文本不加入当前上下文。run 终止后发送新消息开始新讨论。
6. 在网页另开本地讨论，确认不外发。外部链不接受本地 reply 追加；可以用外部 Human 作背景开启独立本地讨论。飞书消息没有编码按钮，服务端 preflight/authorize 同样拒绝它；飞书里“批准/改代码/执行/merge/push”仍只是只读讨论文本。

## 独立真实烟雾测试（不调用模型）

先停止 chat-serve。测试只在显式开关与凭据齐全时运行，否则 skip；它不读 .env：

```bash
export CODECREW_RUN_FEISHU_LIVE=1
# 如需验群，同时设置已在 chat 白名单中的群 ID：
export CODECREW_FEISHU_LIVE_GROUP_CHAT_ID='oc_REPLACE_GROUP'
export CODECREW_FEISHU_LIVE_TIMEOUT=120
python -m pytest -s -q tests/integration/test_feishu_live.py
```

测试打印一次性 `codecrew-smoke-...` 文本。在允许的 DM 发送它；配置 group 时还要在指定群真实 @bot 并发送它。两处各会收到固定测试回复。测试只打印 SDK 版本、会话类型、哈希 trace/回执，不保存原文 ID 或 secret。超时会失败；用 30～300 秒范围内的 timeout。真实断网重连仍需控制台与本机联合验收，Fake 重连通过不等于实网通过。

测试结束清除 `CODECREW_RUN_FEISHU_LIVE`，再按需恢复服务。未配置凭据或未实际收到/发送消息，不得把 skip 当作 live PASS。

## 持久化、恢复与删除

绑定、外部身份、原始 Human/Agent 消息、correlation/run、outbox 文本与回执都保存在本机 `CODECREW_DATABASE_URL` 指定的 SQLite（默认 codecrew.db）。App ID 用于命名空间，App Secret/access token 不作为业务配置保存；SDK 原始事件不保存。用户自己输入敏感信息仍会进入聊天记录。文件、备份、WAL/SHM 和机器访问权限都需按本地私人数据管理。

重启不自动重新调用旧 Agent；已有 Agent 回复补扫到 outbox，旧 sending 结果按未确认重试。默认 5 次尝试、2 秒起指数退避、60 秒上限。failed 计数在状态和 UI 可见；本版没有管理面板、手动重发接口或自动无限补偿。修好配置/网络后，新消息可开始新 run，旧 failed 不偷偷重跑模型。

本地去重避免重复推理和重复建 outbox，但飞书成功、本地回执未提交的崩溃窗口可能再次发送。有限重试的至少一次策略不能保证最终送达，更不能保证跨系统 exactly-once。SDK ACK 与 SQLite claim 之间也存在内存交接窗口；突发满队列/崩溃可能丢入站事件，应检查固定诊断再人工重发新消息。

删除前停止服务并备份。共享数据库可能也包含本地聊天或编码任务，**不要直接删除整个数据库来清理一个飞书会话**。本版没有会话删除/重绑 API；需由操作者在确认关联范围后，按外键先处理 outbox、ingress_events、ingress、binding，再处理该房间的消息/turn/run/delivery 等关联记录，并检查备份和 SQLite 空闲页/WAL 中的残留。仅清空绑定会失去去重证据。若数据库专用于此次实验且确认可丢弃，才整体删除数据库及其附属备份；本地删除不会撤回飞书上的消息。

## 常见诊断

| 表现 | 核对 |
| --- | --- |
| disabled | 启动模式是否为 chat-serve --feishu，enabled 是否 true。 |
| starting/failed | 应用发布、凭据、网络和官方控制台；日志不打印远端原始错误。 |
| DM 有效但群无效 | bot open_id、真实 mentions、两类白名单、群权限与机器人是否入群。 |
| 收到但没有 Agent | busy、unknown alias、原模型 CLI 运行条件、bounded 状态。 |
| 网页有回复但飞书未送达 | outbox pending/retry/failed、发送权限、原消息是否仍可回复。 |
| 回复变成敏感内容提示 | 保守过滤器拦截了已知凭据、路径、stderr/隐藏测试等；本地查看原回复。 |

本服务面向可信本机，状态 API 和聊天 API 都没有公网认证。不要将 localhost 服务直接暴露到公网。
