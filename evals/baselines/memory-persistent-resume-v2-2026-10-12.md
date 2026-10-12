# 持久记忆验收 V2 续跑准备（2026-10-12，非付费后半程结果）

## 改动范围

仅评测工具、测试和文档。保持生产记忆/召回、题目、0.65 阈值、全局配置、
持久验收数据库、旧两库、Qdrant namespace/容器/卷和原始失败报告不变。
本轮没有 Kimi/embedding 请求，没有启动真实 worker。

混合生命周期门槛和纯向量诊断分栏；新向量查询留返回排名/分数，普通 miss
可由 BM25 补回，真实异常仍停止。新增校验后的 `--resume-from` 和本地只读
`--check-resume`，不重放 Human/Agent/提案/关闭前缀，不追加预算、不覆盖父报告。
补强最终完成判据：audit 错误、积压或停止回执异常均不能标为 complete。

## 当前真实目标只读结果

- 目标：`.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json`。
- 父报告：`live-runs/20261012T105311Z-d0aa397b/report.json`。
- 父报告 SHA256：`6cbbbc5f88492de68e9919d8a60acc2e3d2540052f3317ddb0ebdb13d337aad5`，原字节保留。
- validated=true；completed_hybrid_steps=6；next_step=7。
- 历史 Kimi starts=6、embedding attempts=12；剩余 cap=6/28；new_model_calls=0。
- legacy_re_adjudication=true：只在后续新报告留复核说明，原 report complete=false 不改。
- 第 6 步已保存成功回复和混合上下文用于复核；历史向量 miss 保留，旧分数未知。

## 离线覆盖及边界

测试使用临时 SQLite、Fake adapter/embedding/远端，网络与实际进程调用均被禁止。
覆盖新 Journey 重开并继承前缀/预算、完整后半程、父报告不可变、固定 V1 迁移、
题目/代码/阈值/回复/上下文/提案/动作篡改、库数据/schema 漂移、拒绝额外房间、
清理与积压条件、双进程锁竞争/软链接、请求前预算预留、只读 CLI、向量全 miss
而 BM25 补回的完整旅程，以及最终审计失败不得伪装完成。

Context7 核对 Python 3.11 sqlite3 URI `mode=ro`、显式 BEGIN 与关闭连接，
以及非阻塞 advisory lock 的失败行为；实现使用一致只读快照及不按 PID 杀进程的锁。
这不是实际模型/Qdrant 故障或双 OS 进程证明，不能代替真实后半程验收。

新增49项控制测试，加上原12项持久旅程，合计61项通过；扩大定向1231项通过，
仅两条既有 Starlette/httpx 和 anyio 警告（非全仓）。Ruff、diff 检查通过。
旧来源冻结24条有序来源匹配，TP/FP/FN=10/0/2、Recall@3=83.33%，无模型调用；
历史 memory.py provenance 差异是已有信息提示，本轮未改该生产文件。

V1 无历史完整 SQLite 指纹，只有固定报告哈希和逐项规范状态复核，不能证明历史
缓存字节未改变；V2 快照包含 schema/记忆/消息/cache/revision/outbox/telemetry/FTS/
maintenance。锁仅协调本工具，不防任意外部写库；预算是本地调用记录而非账单。
原 V1 失败记录仍见 [首轮证据](memory-persistent-live-partial-v1-2026-10-12.md)。

真实阶段仍 **3/5**。下一步在同一库显式付费续跑第 7～12 步，之后才做重启验收；
不清空库、不重跑前 6 步、不降低阈值、不把工具准备当作整个记忆模块全通过。
