# 自动写入到后台召回：隔离旅程验收 v1

2026-10-12，后台同步阶段第 5/5 步；不改历史质量/性能成绩。

最终原始报告：`evals/results/memory-vector-worker-journey-v1/20261012T090314Z-f3b88d24/report.json`（本机Git忽略目录，不随仓库发布）。
运行：`.venv/bin/python -m evals.check_memory_vector_worker_journey --live`。
前一轮 `20261012T085650Z-dbe366c7` 也 complete，之后增加了关闭记录的重启后检查，
最终按上述新源码再跑；两轮报告保留，各自 provenance 不覆盖。

## 方法与防假通过

生产 capture 接收人工定义的提案/动作与本地 Human 原文，自动 writer indexing
调用合成 embedder 生成缓存；从缺 embedding meta 起步。独立实际 CLI 进程消费
outbox，父进程不代投递/改租约/改时钟/重投。Qdrant v1.19.0 为新建自有实例，
与原 6333 服务隔离，不拉镜像；Python 3.11.16。

每臂以 `semanticprobe` 查询，先确认关键词无命中，再检查 pure search_scoped、
hybrid retrieve、chat_context；检查远端版本/墓碑，最后双向 audit。
不是拿关键词命中证明向量同步成功；不是实际 LLM/正常回复派发/浏览器验收。

| 检查组 | legacy | BM25 |
| --- | --- | --- |
| 创建→自动缓存→delivery→语义/混合/上下文，重复提案幂等 | 通过 | 通过 |
| 替换沿革，旧墓碑与最新记忆/上下文 | 通过 | 通过 |
| 其他房间/当前讨论不泄漏 | 通过 | 通过 |
| 完成证据、关闭历史/墓碑、无活跃召回，重放幂等 | 通过 | 通过 |
| 取消证据同上 | 通过 | 通过 |
| embedding 失败 waiting，查询不回填，writer 显式重试后自动恢复 | 通过 | 通过 |
| 停机入队、store/不同 PID 重启，替换/关闭记录不复活 | 通过 | 通过 |

实际 PID：legacy 1112→1119；BM25 1132→1140；四者 exit 0/stopped，模式 none，
没有注入提交暂停 gate。重启前后 child diagnostics 均无积压，最近 delivered 有记录。
各臂 7 local/current/remote（4 活跃 + 3 墓碑），audit 一致，无 pending/processing/waiting/dead；
保留 outbox 各 delivered=10、superseded=10，是保留行状态，不是终生成功计数。

每臂合成 embed 文本 30；真实云 embedding/Agent 调用 **0/0**，没有读取 `.env`/密钥。
两次 `memory vector indexing failed: RuntimeError` 日志为每臂一次刻意故障。
缺缓存不会由 worker 自动调用 embedding；本例用 writer.index 显式重试，不声称无人值守云重试。

## 清理与回归

complete/matrix_passed/children_cleanup_complete/container_cleanup_complete/cleanup_complete 全 true，
最终全部源码 SHA-256 与 report 匹配。容器 `codecrew-memory-fault-913c9fa5fc694c54878f237910120efc`
已回收，bind 存储、SQLite、child spec/markers/report 留存。镜像 ID 为
`sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`。
清理只用本轮 Popen handle 和所有权复验后的容器；未按名称强杀用户进程。

新增 8 项离线有效性测试，包含语义查询被关键词污染、缺远端仍有词项命中、
远端漂移及问句不能误关闭；假后端结果不计入实际进程证据。
扩大定向 **1126 passed**（两条既有 Starlette/anyio 警告，非全仓），Ruff/diff 通过。
旧 24 题 ordered sources 冻结不变，TP/FP/FN=10/0/2，Recall@3=83.33%；
冻结工具仍有历史 memory.py 哈希提示，本步未改生产代码或历史 manifest。

本阶段完成的是隔离本机、串行、确定性写入边界验收，不等于新盲测、真实模型准确率、
云服务质量/延迟、ANN、并发负载、多租户安全、集群 SLA 或恰好一次。
用户真实库 worker **未启用**，没有安装系统服务或 chat 启动 hook，默认仍 SQLite/旧词项。
