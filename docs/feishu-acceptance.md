# 飞书 v1 实现与验收记录

日期：2026-10-05。开发副本基于 upstream `54d4767ae73aa7de5f40a0fbf00f3a525bfdd602` 的源码快照；未修改原下载快照，未推送 GitHub。当前机器为 Windows、Python 3.12.14。真实凭据未提供，没有调用真实模型或飞书网络接口。

## 实现与架构决定

D3：DM 和白名单群真实 @当前 bot；D4：单应用机器人、逐条角色标签；D5：external Human 只读、即使本地网页选择也不能直接授权编码；D6：`(app_id, chat_id)` 稳定单房间。现有固定四成员模型、有界调度、私有 Agent runtime 和隔离检查保留。

新增 migration 19：bindings、ingress、ingress_events、outbox。双平台身份去重，稳定 Human/correlation/run，busy 不进上下文。扫描游标和插入同事务，按 chat 序列投递，失败重试只作用于消息。SDK 为可选官方 lark-oapi 1.7.3，长连接收包进程没有模型调度权。CLI 双开关、FastAPI 生命周期、只读状态和外部身份 UI 已接入。

## 变更文件

| 范围 | 文件 |
| --- | --- |
| 飞书实现 | app/feishu/{models,adapter,store,bridge,outbox,sender,transport,runtime,privacy,__init__}.py |
| Chat 与权限 | app/chat/{models,store,service,dispatch,bounded_dispatch,discussion_store,coding_intent,coding_authorization}.py |
| 装配 | app/config.py、app/cli.py、app/main.py、pyproject.toml、.env.example、scripts/check_offline.py |
| UI | app/web/chat.html、chat.js、feishu.js |
| 文档 | README.md、docs/architecture.md、feishu-integration-{requirements,development,exec-plan}.md、feishu-setup.md、本记录 |
| 测试 | tests/feishu_helpers.py、test_feishu_{adapter,bridge,outbox,runtime,transport,sdk,ui}.py、ui_feishu.test.cjs、integration/test_feishu_live.py |

## 验证环境与命令

当前宿主没有项目 .venv；依赖安装在工作区的 `work/review-deps`，SDK 单独在 `work/sdk-deps`，没有修改用户全局 Python。`work/dev_check.py` 加载 review-deps，清除模型凭据，关闭所有 LIVE 开关，清除 Feishu 配置环境，仅为测试加入已捆绑 Node 的 PATH。SDK runner 另加 sdk-deps。这两个本机辅助 runner 位于仓库外，不属于仓库发布内容，作用相当于隔离环境中的 `python -m pytest` / `python -m ruff`。

以下为本次实际命令形式，从包含 work/ 与 outputs/ 的工作目录执行；PowerShell 中 `$py` 指向 `C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe`：

```powershell
$py = 'C:\Users\DELL\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py work/dev_check.py work/source/CodeCrew-54d4767ae73aa7de5f40a0fbf00f3a525bfdd602 -q --tb=short --junitxml=../../baseline.xml
& $py work/dev_check.py outputs/CodeCrew -q --tb=short --junitxml=../../work/final-full.xml
& $py work/dev_check.py outputs/CodeCrew -q tests/test_standalone_chat_api.py tests/test_demo.py tests/test_chat_to_code_live_fixture.py --tb=short --junitxml=../../work/key-regressions.xml
& $py work/dev_check.py outputs/CodeCrew -q tests/test_feishu_adapter.py tests/test_feishu_bridge.py tests/test_feishu_outbox.py tests/test_feishu_runtime.py tests/test_feishu_transport.py tests/test_feishu_sdk.py tests/test_feishu_ui.py tests/integration/test_feishu_live.py tests/test_standalone_chat_api.py tests/test_standalone_chat_store.py tests/test_chat_coding_preflight.py --tb=short --junitxml=../../work/final-focused.xml
& $py work/sdk_check.py outputs/CodeCrew -q tests/test_feishu_sdk.py --tb=short --junitxml=../../work/sdk.xml
& $py work/dev_check.py outputs/CodeCrew -q tests/test_feishu_outbox.py --tb=short --junitxml=../../work/privacy-final.xml
& $py work/dev_check.py outputs/CodeCrew -q tests/test_feishu_adapter.py tests/test_feishu_bridge.py tests/test_feishu_outbox.py tests/test_feishu_runtime.py tests/test_feishu_transport.py tests/test_feishu_sdk.py tests/test_feishu_ui.py tests/integration/test_feishu_live.py --tb=short --junitxml=../../work/feishu-final.xml
& $py work/dev_check.py work/source/CodeCrew-54d4767ae73aa7de5f40a0fbf00f3a525bfdd602 -q tests/test_continuation_workflow.py::test_rework_is_bounded_and_never_self_approves --tb=short --junitxml=../../baseline-timeout-recheck.xml
& $py work/dev_check.py outputs/CodeCrew -q tests/test_continuation_workflow.py::test_rework_is_bounded_and_never_self_approves --tb=short --junitxml=../../work/final-timeout-recheck.xml
& $py work/dev_check.py outputs/CodeCrew ruff
```

全量与长输出命令还以 `*> work/<name>.log` 重定向，随后打印退出码。早期阶段针对 adapter/bridge/outbox/runtime/UI 的小批命令用于定位和修复问题；下表以最终有效结果为准。

在正常安装环境可复验：`python -m pytest -q`、`python -m ruff check .`；额外安装 `.[feishu,dev]` 后 SDK 契约不再跳过。真实 smoke 必须按[操作指南](feishu-setup.md)显式启用，不能当作默认离线测试执行。

## 测试结果

<!-- RESULTS:START -->
| 检查 | 总数 | 通过 | 失败 | 错误 | 跳过 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 未修改 upstream 快照（全量） | 1351 | 1108 | 208 | 11 | 24 |
| 开发副本（全量） | 1434 | 1187 | 209 | 11 | 27 |
| 最终飞书专用测试（无 SDK） | 93 | 90 | 0 | 0 | 3 |
| 官方 SDK 离线契约 | 2 | 2 | 0 | 0 | 0 |
| 用户指定的三个关键回归文件 | 10 | 6 | 4 | 0 | 0 |
| 最终受影响聊天/预检/飞书范围 | 111 | 107 | 1 | 0 | 3 |
| 最后的隐私/出站复验 | 23 | 23 | 0 | 0 | 0 |
| 原始快照返工测试复测 | 2 | 1 | 1 | 0 | 0 |
| 开发副本返工测试复测 | 2 | 1 | 1 | 0 | 0 |

Ruff `check .`：PASS；Git whitespace check：PASS。Node UI 脚本已实际执行，通过；没有因缺 Node 跳过。

无 SDK 的最终 93 项中，2 项官方 SDK 契约按预期跳过、1 项真实 smoke 按 opt-in 规则跳过；单独 SDK 环境的 2 项均通过。真实 smoke 的 skip 不是线上 PASS。

全量比较保留了 208 个原始失败和 11 个原始错误，另一次性多出 `test_rework_is_bounded_and_never_self_approves[True]` 的 5 秒 TimeoutError。未改测试或放宽超时；在相同隔离环境中先后单独复测两个参数分支，原始快照与开发副本均为 True 通过、False 因已有 5 秒超时失败。新增全量超时未在复测中复现，两侧复测结果一致，符合时间敏感测试波动；**不将全量失败改记为通过，也不声称全量绿色**。

原有参数 ID 中 4 个 collection-time uuid4 值每次不同，对比时只将 UUID 替换为占位符，保留测试函数和其他参数；归一化后只有上述 1 项结果差异。用户指定的 4 个回归失败都已出现在基线。受影响范围中唯一失败为 Windows 不允许创建 symlink（WinError 1314），也已出现在基线。

全量测试收集于最后一批 Feishu 审查补充前；最后的功能代码由完整 93 项 Feishu 测试、受影响范围和隐私复验覆盖。全量命令运行了约 30.8 分钟，没有在收到失败后提前停止。紧凑机器可读证据在交付目录的 `feishu-validation-summary.json`，保留全量差异及两侧复测结果。
<!-- RESULTS:END -->

新增 Fake 测试覆盖用户 A～L 验收矩阵：解析和 mentions、白名单、双 identity 去重、并发首次绑定/busy、双 chat 和群发送者身份、角色路由、三条 A→B→A、回合/时间上限、顺序扫描和不外泄本地话题、重试/封顶/stale sending、Human 保存前后崩溃、编码权限、CLI/生命周期/UI/日志隐私。另验证游标事务回滚、关闭房间时恢复、进程启动失败和错误队列清理。

原仓库 Windows 失败涉及 os.getuid、POSIX 目录模式、symlink 权限、macOS sandbox-exec、可执行文件/路径/换行和部分任务工作流。未通过全量绿色验收，不能将这些失败改标为 skip/PASS；本次保留隔离检查并与原始快照比较。

## 安全审查（完成并修复）

| 检查项 | 结果与证据 |
| --- | --- |
| secret 与错误泄漏 | 固定 error 分类、哈希日志、SecretStr，SDK logger 关闭；Fake secret/正文/完整平台 ID 日志断言通过。出站敏感文本整条拦截；不声称语义 DLP。 |
| 重复模型调用 | 先持久化 ingress/Human/run 后 schedule；双唯一及事件别名表；同事件、新事件同消息、重启、崩溃测试通过。 |
| 重复外发 | 本地 source key 去重与游标事务；远端成功未保存回执窗口仍可重复，已记录。 |
| 并发与 busy | 单主进程/每 chat 锁，SQLite 事务与活跃 run 检查；busy 文本不入当前 correlation。 |
| 真正 @bot 与 echo | open_id 精确匹配并剥离平台占位符；@all、假文本、其他 bot、自回声均拒绝。 |
| 本地内容误外发 | binding 起点 + ingress correlation + Agent + 真实 bounded turn；本地 reply 不能追加外部链，独立本地讨论不外发。 |
| 编码越权 | preflight/authorize 拒绝来源，前置于授权 replay；HTTP 不接受伪造来源；真实 Task/Git/shell 路径在 Fake 边界测试中不得被调用。 |
| 默认行为 | 普通 chat/demo 不调用 SDK；新增状态 disabled；旧消息 JSON/指纹保持兼容。 |
| 重启与关闭 | 未完成模型 fenced；旧回复补扫；已禁用绑定不发 pending；关闭房间恢复不阻断服务。 |
| SDK 与事务 | 可选延迟导入；真实 SDK 请求对象离线验证；事务内没有网络/模型调用；接收进程停止确认。 |

审查后修复了禁用绑定仍可能发送 pending、子进程启动失败清理、队列异常可观察性、关闭房间恢复和 Unicode 格式控制符显示名校验，均有测试。没有写入真实 secret，没有 push。

## PENDING / 限制

- **PENDING：真实飞书 smoke。**没有提供真实 App ID/App Secret、bot ID 和允许的测试目标；没有建立连接、收真实 DM/group 或发送真实回复。
- **PENDING：macOS 实机回归。**宿主 Windows，原项目生产 Agent 隔离依赖 Unix/macOS；本次没有声称移植完成。
- 单实例；没有 webhook、多账号、多 IM、通用队列或管理面板。SDK 子进程用于生命周期管理。
- 入站 ACK 到数据库之间有内存窗口，重试有有限次数，不能保证无损接收/最终送达/exactly-once。
- 数据库存原始聊天和必要平台身份；没有自动删除或重新绑定接口。飞书权限名称仍需在真实控制台核验。

## 操作者还需完成

按[配置指南](feishu-setup.md)创建/启用应用机器人，核对最小收发权限、订阅 im.message.receive_v1 长连接、发布到测试成员，确认本应用 bot open_id 和两类白名单，安全注入凭据。在原项目支持的运行环境中执行 opt-in smoke，并分别记录 DM、真实群 @bot、回复和停止结果。用户无需为本次本地代码修改再做确认；这些是外部环境验收尚缺的事实。

## Git 交付

<!-- GIT:START -->
本地基线提交 `2574132c58eb48cb8218a7d16e50796e32bd93af` 导入 upstream 快照。
功能提交 `1cea56af3224df38adc0e55b839c90f9b0ef0512`：Add opt-in read-only Feishu chat bridge with durable ingress and outbox。
最终验收记录另有文档提交，见 `git log -2` 和交付目录 DELIVERY.md。交付完成后工作树 clean，分支 feishu-v1；未设置/推送远端，没有 PR。
<!-- GIT:END -->
