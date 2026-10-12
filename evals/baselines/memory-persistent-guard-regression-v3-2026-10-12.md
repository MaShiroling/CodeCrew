# 持久验收后半程失败与 V3 离线修复（2026-10-12）

这是失败留档和修复验证，不是新模型运行通过，也不替换原 V1/V2 成绩。

## 用户真实 V2 后半程证据

报告：`.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/live-runs/20261012T123657Z-c4c65c5c/report.json`。
SHA256：`56b324fc7e453fa3bea15148948f6d737aecd1eb7a95804e22d9a88ba79ee28d`。
prepare 与原 V1 报告哈希保留；没有清空库或删除原消息。

- 复用前6条，实际新增第7～12条；12个 turn 都 succeeded，最后一步 passed=false。
- 累计 Kimi starts=12、embedding attempts=19、errors=0；新增6/7，并非预算拒绝导致停止。
- 开发待办 completed、个人待办 cancelled，关闭来源消息正确。
- 第9个人语义召回目标命中0.7055199，阈值0.65。
- 第12 Agent 提议 preference「没有记录就说明未知」，来源是本轮隔离问答，误被接受；
  新记录 `1722e891-ca99-4a49-96e9-8675d7494083` 活跃。
- 第11原回复否认有关闭记录，虽然字面出现取消，却不是依据 SQLite 的取消回执。
  原评测 passed=true 是关键词误判，不能算该回答语义合格。
- worker exit0/stopped、clean_shutdown=true、attempted4；pending2/delivered7/superseded7，
  pending 都属于误写偏好。最终 audit 未执行，complete=false 保留。

本次只读定位核对来源/提案/记忆、完整源码哈希与数据库快照；没有追加模型或远端投递。

## V3 改动与离线证明

1. preference 自动提案必须携带可核对的个人习惯、未来/默认或房间要求；检查原句的
   一次性前缀、问句、未决状态。保留独立句子的明确本人/未来/条件性偏好；既有第三方、
   来源、敏感词和原文片段守卫保留。这是保守有限句式，不是完整意图分类器。
2. 唯一关闭待办状态问法覆盖“还在未完成待办里吗”等变体；通用词不算目标证据，
   同房间、Human、外部来源、条件/操作请求与歧义边界仍守住。SQLite回执不启动Agent，
   不注入关闭记录为普通召回，也不宣称现实任务真的完成。
3. 评测版本 persistent-hybrid-v3：该关闭查询要求cancelled规范记录、精确服务端回执、
   无Agent session并留sqlite来源。原答案里的取消字样不能通过。失败分类补充固定代码，
   不泄露原始异常/供应商信息，也不篡改旧report。

新增48项临时SQLite/Fake适配器/假远端控制测试：一次性提议拒绝且不写候选/cache/outbox，
明确长期偏好保留、问题/一次性与稳定表态混合消息、关闭查询变体与重开数据库、
无模型状态回复、条件/操作/无关/歧义拒绝、原误判回复及其他否定/假设文本均不通过，
新V3续跑来源校验，以及复用原误写提案的完整离线生产旅程/终态audit。
原错误回复只将首段合成业务内容固化为回归，测试不读取用户私有密钥或验收库。

当前私有真实报告全文只读复核：original_spurious_preference_now_accepted=false；
original_closed_answer_recheck=closed_state_receipt_missing；数据库快照与原报告仍一致。
新Agent/embedding请求0/0。回放证明本地边界拒绝旧失败，不证明未来Kimi会生成正确提案。

扩大定向1279项通过（两条既有Starlette/httpx、anyio警告，非全仓）；Ruff/diff通过。
旧24条有序召回来源仍匹配，TP/FP/FN=10/0/2、Recall@3=83.33%，模型0/0；
memory.py/dispatch.py代码指纹变化如实报告，冻结标签/来源/历史指标没有重写。
修复一项单文件provenance控制测试的环境假设：仅在内存中构造独立模拟指纹，
验证磁盘freeze未变，避免把其他合法源码变化混入该测试；不以重冻结掩盖变化。

## 未完成事项

真实误写记录、其缓存/FTS和2条pending仍原样保留，本轮没有操作真实worker、投递、
撤销或audit。旧库、global配置、阈值、题目和旧失败报告不改。
旧报告已12条且Kimi starts达原cap，不支持继续/强制重放；V3也拒绝旧代码指纹。
下一步需明确授权进行该单条误写的可恢复撤销与同目标无模型同步/审计；真实小范围
复验需另定预算/目标。真实阶段仍3/5，不能称本轮已完成完整生命周期或新盲测。
