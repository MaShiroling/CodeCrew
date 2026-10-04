# 飞书接入开发文档（实现方案草案）

状态：**待 D3～D6 决策确认后实施**；本仓库当前没有飞书 Connector。配套产品边界见[需求草案](feishu-integration-requirements.md)。此文档是交接规格，不代表以下接口或测试已经存在。

## 1. 现有接入点与参考边界

CodeCrew 已有 `StandaloneChatService`（创建房间与 Human 消息）、`StandaloneChatStore`（SQLite）、`StandaloneChatDispatcher`（单次接话）、`BoundedDiscussionDispatcher`（Agent 有界接话）、`build_chat_app()` 和网页 `/ui/chat/`。它们与编码 Task 分离。现有 `StandaloneChatRoom` 固定一位 Human 加三位 Agent，消息只有内部 `sender_id`，**不能直接表达飞书群里多个真人**；不得把所有外部发言都显示成“我”。现有 `post_message()` 也只接受 CodeCrew Agent 别名，不能把飞书 `@机器人` 占位符原样送进去。

参考项目在飞书适配器之外有路由、会话绑定和出站投递层。CodeCrew 只借鉴这种职责划分及已暴露的故障类别，不照搬通用网关。尤其要避免“Agent A→B→A 只送出第一条回复”、机器人回声触发、群内不同用户身份混淆等问题。对照入口：[Clowder 网关设计](https://github.com/MaShiroling/clowder-ai/blob/main/docs/features/F088-multi-platform-chat-gateway.md)、[群聊设计](https://github.com/MaShiroling/clowder-ai/blob/main/docs/features/F134-feishu-group-chat.md)、[飞书适配器源码](https://github.com/MaShiroling/clowder-ai/blob/main/packages/api/src/infrastructure/connectors/im-connectors/feishu/FeishuAdapter.ts)。参考快照仅用于架构核对，CodeCrew 的协议和代码自行设计。

飞书侧优先用[官方 Python SDK](https://github.com/larksuite/oapi-sdk-python)和[事件处理文档](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events)。首先做一次最小长连接探针，确认当前安装版本在 Python 3.11/macOS 上可收 `im.message.receive_v1`、可在 FastAPI 生命周期内安全启动/停止；把**实测** SDK 版本固定到可选依赖。不要在方案里预先声称 SDK 自动重连能覆盖所有网络故障。发送消息按[官方发送消息接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create)实现；使用飞书应用身份，不用“自定义机器人 incoming webhook”替代接收消息的应用机器人。

## 2. 首版数据流

```text
飞书长连接事件
  → FeishuTransport（SDK 生命周期 / 连接状态）
  → FeishuAdapter（校验类型、真正的 @bot、文本解码、bot 自回声过滤）
  → FeishuBridge（群+成员白名单、去重、会话绑定、发言者归属）
  → StandaloneChatService / BoundedDiscussionDispatcher（原有持久化与有界调度）
  → StandaloneChatStore 中的 Agent 消息
  → FeishuOutbox（按关联链扫描、按序记录待发送）
  → FeishuSender（应用机器人文本消息）
```

外部平台只能进入 `FeishuBridge` 的受限入口；**不能**透传到 `POST /coding-tasks` 或创建任务 API。SDK 回调内只做解析与有界入队，不运行模型、不执行同步 SQLite 长操作；跨线程/事件循环交接必须是线程安全的。若 SDK 有合适的异步 API 可直接用，但需先用探针证实其生命周期行为。

## 3. 建议的最小接口

接口名字可在实现时调整，语义与边界必须保留：

```python
@dataclass(frozen=True)
class FeishuInbound:
    app_id: str
    event_id: str
    message_id: str
    chat_id: str
    chat_type: Literal["p2p", "group"]
    sender_open_id: str
    text: str                 # 解码并去除真正的 @bot 占位符
    mentions_bot: bool        # 按 mentions[].id.open_id 判断，不扫纯文字

class FeishuTransport(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...

class FeishuSender(Protocol):
    async def send_text(self, chat_id: str, text: str,
                        reply_to_message_id: str | None) -> str: ...

class FeishuBridge:
    async def receive(self, event: FeishuInbound) -> None: ...
    async def deliver_pending(self) -> None: ...
```

不要在 `FeishuInbound` 或日志里保存 App Secret、access token、完整 SDK 原始事件。Adapter 应将“非文本/未 @/自身消息/字段不完整”分类为可诊断的忽略结果，不应默默当成功。

## 4. 存储与一致性

建议增加专用 SQLite migration，至少维护下列记录（表名可调整）：

| 记录 | 关键约束 | 作用 |
| --- | --- | --- |
| `feishu_bindings` | `UNIQUE(app_id, chat_id)`、`UNIQUE(room_id)` | 一个飞书会话对应一个 CodeCrew 房间；包括状态与绑定起点序号。 |
| `feishu_ingress` | `UNIQUE(app_id, event_id)`、`UNIQUE(app_id, message_id)` | 记录内部消息、外部发言者与 `correlation_id`；重复投递不能再启模型。 |
| `feishu_outbox` | `UNIQUE(app_id, internal_message_id)` | 记录目标 `chat_id`、消息序号、发送状态、尝试次数、最后安全错误与飞书回执 ID。 |

- 创建房间使用由 `app_id + chat_id` 导出的稳定 UUID 幂等键，并在 binding 里保存对应 `room_id`。并发首次消息、创建房间后崩溃、重放均需能收敛到同一绑定。
- 入站幂等键由**平台消息身份**稳定导出，不能随机生成。先校验白名单，再调用模型。内部消息持久化成功但 `feishu_ingress` 尚未写入时重试，也必须依靠稳定内部幂等键避免重复 Agent 回合；run 由根消息唯一约束复用。
- 在 `StandaloneChatMessage` 增加**可选、受校验的外部发言来源**（平台、外部 chat ID、发送者 open ID、可选显示名快照），或者使用等价的只读投影；API/UI 与 Agent 上下文都必须能区分同群真人，不能仅在 Connector 私有表里存而网页仍显示“我”。旧消息的字段缺省不变。显示名不作为身份凭据，也不应使相同消息重放的幂等指纹变化。
- 出站仅选取与 `feishu_ingress.correlation_id` 相关、绑定创建后的 **Agent 消息**；网页里另开的话题和 Human 消息不能误推送到飞书。按房间 `sequence` 从原存储扫描并将 outbox 行落库，扫描游标推进与建 outbox 同事务。Agent 消息先写入、桥接进程再崩溃也可补扫。
- 投递状态建议 `pending → sending → sent` 或 `retry_wait/failed`；重启时过期 `sending` 可恢复。限次指数退避，失败不重跑 Agent。发送接口成功而尚未保存回执时崩溃可能造成**重复飞书消息**：文档应如实说明，不能宣称跨系统 exactly-once；若官方 API 提供可验证的幂等机制可再增强。
- `event_id`、`message_id`、`chat_id`、`open_id` 可能属于敏感元数据；业务库最小保留，日志使用截断/哈希标识，输出给开发者的测试样例一律用虚构 ID。

## 5. 入站路由细则

1. `im.message.receive_v1` 事件结构先做 Pydantic/显式校验；只认明确的 `p2p`/`group`、`text`，缺字段拒绝。解析 `content` 的 JSON 文本，不使用 `eval`。
2. 群聊需在 `mentions` 中匹配**本应用机器人**的 `open_id`，仅剥离匹配到的占位符；纯文字“@机器人”、`@所有人`、@其他机器人都不触发。私聊无需 @bot。若机器人身份无法可靠获取，群聊入口 fail closed。
3. 检查 `chat_id` 和发送者 `open_id` 均在配置白名单。拒绝事件在 Agent 调度之前结束。多个群用同一机器人，但每群独立房间、上下文和预算。
4. 用户若明确写了 CodeCrew Agent 别名，就遵守现有路由；未写则由桥接层显式选白金为 opening role。把“平台 @bot”与“内部 @Agent”分开，不能简单把前者当成 `@白金` 原文。每条外部输入只启动一次有界 run。
5. 若已有活跃 run，按产品决定给出“忙碌/稍后重试”状态或显式队列，不能把新消息并入当前 `correlation_id`。草案采用忙碌反馈，待验收时确定。

## 6. 配置与进程边界

建议可选配置均采用 `CODECREW_FEISHU_*` 环境变量：

- `CODECREW_FEISHU_ENABLED=false`（默认）；只有显式开启才接入。
- `CODECREW_FEISHU_APP_ID`、`CODECREW_FEISHU_APP_SECRET`（Secret 在日志和模型中不可见）。
- `CODECREW_FEISHU_BOT_OPEN_ID`（或经官方接口可靠查询并缓存，失败时不开放群聊）。
- `CODECREW_FEISHU_ALLOWED_CHAT_IDS`、`CODECREW_FEISHU_ALLOWED_SENDER_OPEN_IDS`：解析为集合；任一为空均拒绝启动群聊监听，不用“空代表所有”。如私聊放行策略与群聊不同，必须单独命名配置而非模糊复用。
- `CODECREW_FEISHU_MAX_OUTBOX_ATTEMPTS` 等非秘密参数使用有界默认值。

只在 `.venv/bin/python -m app.cli chat-serve --feishu`（建议 CLI 形式）单实例启动时挂接飞书生命周期；普通 `chat-serve`、`chat-demo` 和编码任务服务行为不变。首版不部署公网入口，也不提供 Webhook。飞书应用后台需启用机器人、消息事件订阅、必要的消息收发权限，并发布/测试到指定成员可见；按[官方平台文档](https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-events)实际界面核对，权限不要申请“读取群内所有消息”来替代 @机器人事件。具体权限名以开发时控制台为准。

## 7. 开发拆分与交付物

| 小步 | 交付 | 最低验证 |
| --- | --- | --- |
| F0 决策/探针 | 冻结 D3～D6；官方 SDK 长连接收/发探针与版本记录 | 本地假凭据不触网；真实探针独立开关，不进 CI。 |
| F1 契约/解析 | `FeishuInbound`、Adapter、假 Transport/Sender、配置 | 文本、真正 @bot、非文本、自回声、缺字段、密钥缺失测试。 |
| F2 绑定/权限/入站 | migration、白名单、外部身份投影、服务接入 | 私聊/群隔离、两真人可区分、重复事件/重启不重跑。 |
| F3 有界调度 | 外部消息触发独立 bounded run，忙碌冲突处理 | A→B→A、预算停止、第二消息不污染第一链。 |
| F4 出站/恢复 | 按序 outbox、文本发送、失败重试与状态 | 三条 Agent 消息顺序、断网/重启补投、不泄露网页私聊。 |
| F5 UI/文档/实测 | 网页外部来源、连接/投递状态、操作指南 | 离线回归 + 显式开关真实飞书 DM/群烟雾测试；留脱敏证据。 |

每小步单独测试、小提交。开发者先交付测试结果和风险列表，不自动推送 GitHub；是否合并/上线由项目维护者决定。

## 8. 验收测试矩阵

| 场景 | 预期 |
| --- | --- |
| 允许的 DM 或允许群 `@bot` | 只建一个绑定房间和一个有界 run；回复回到原会话。 |
| 群未 @bot、@all、@其他 bot | 不调用模型；可诊断地忽略。 |
| 未在白名单的群/成员 | 不建房间、不调用模型、不泄露已有消息。 |
| 同事件/消息重复与进程重启重放 | 同内部 Human 消息、同 run，无第二次模型调用。 |
| 同一群 A/B 两位真人 | 网页与模型上下文可区分发言者，不误认为本地“我”。 |
| Agent A→B→A | 飞书收到三条独立、按序、身份清楚的回复。 |
| 出站 API 失败、断线、重启 | SQLite 中留状态与重试证据；不重新运行 Agent。 |
| 飞书文本要求改代码/批准 | 仍只是讨论，不创建 Task/Worktree。 |
| 网页对绑定房间另发私有讨论 | 不因 `room_id` 相同而泄露到飞书。 |

## 9. 提交给维护者的材料

开发人员最终应提供：变更文件清单、配置样例（空值，无真实密钥）、最小飞书应用配置步骤、离线测试命令与结果、真实烟雾测试的脱敏 trace、已知限制、需人工复核的权限项。测试未覆盖的能力不得写入 README 的“已实现”段落。
