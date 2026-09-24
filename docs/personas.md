# 团队人格系统：后端第一步

当前内置三位成员：白金（Planner）、月见（Implementer）、鲸鲸（Reviewer）。
`PersonaProfile` 按角色保存展示名、`@` 称呼、职责简介，以及三层信息：

| 字段 | 用途 | 当前接入位置 |
| --- | --- | --- |
| `personality` | 面向用户的简短性格简介 | 已保存，供后续 UI 使用；不放入每轮 prompt |
| `caution` | 队友需要知道的工作短板或提醒 | Agent 回合的成员名单 |
| `l0_self_description`、`restrictions` | 本人行为校准 | 对应角色的 Agent 回合 prompt |

团队共同原则保存在 `TeamPersonaCatalog.team_principles`：白金负责澄清与计划，月见负责
实现并在计划不清时提问，鲸鲸独立评审并说明证据；Verifier 和 CompletionGuard 才有
验证与最终判定权。这些原则会进入每个 Agent 回合。新建任务的 TeamRoom 成员名称使用
三人的展示名；此前创建的任务不会被自动改名。

`mention_patterns` 目前只是角色资料，**尚未解析自由文本 `@称呼` 来派发消息**。
实际收件人仍必须使用结构化 `MessageRecipient`，避免一句聊天文本绕过路由权限。
人格本身不绑定模型厂商：CLI 已提供可选的 Kimi Code 会员 API Implementer 和
DeepSeek Flash Reviewer 离线适配路径，但尚未通过真实模型端到端验收。
`kimi-for-coding` 是会员模型别名，不能把实际后端固定称为 K3。具体头像和图案等待
用户提供素材。

`restrictions` 是给模型的自然语言行为提示，不是安全沙箱。真正的边界仍由代码执行：
AgentTurnRunner/Registry 按角色确定只读或工作区写入权限；ConversationRouter 限制
消息类型和收件人；Verifier 执行确定性检查；CompletionGuard 独立决定能否成功。
人格文本即使要求跳过验证，也不能更改这些代码层规则。模型仍可能产出错误动作，
这种情况应被拒绝、返工或转人工，不能理解为 prompt 能保证模型永不违规。
