# Qdrant 故障恢复验收（2026-10-12）

本机日期 2026-10-12，结果目录采用执行环境 UTC 时间，不混用两者。
最终代码的实际探针：
`evals/results/memory-qdrant-faults-v1/20261011T170809Z-3cd3c79d/report.json`。
本页保留摘要；报告含代码 SHA，结果目录被 gitignore，复现须重新执行探针。

- `complete=true`、`cleanup_complete=true`、九组检查通过。
- 专属容器六次实际 stop/start。本地镜像 `qdrant/qdrant:v1.19.0`，image ID：
  `sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`。
- 首轮停服后 3 dead / 5 delivered / 7 superseded；恢复不自动重投，显式重投后恢复。
- 最终 outbox 21 delivered / 19 superseded；pending/processing/waiting/dead 均零。
- 最终 fresh audit：local_scanned=13、current=13、remote_scanned=13；
  `scan_complete=true`、`consistent_at_observation=true`、`changed_since_start=false`。
- 合成查询向量请求 13 次；真实 embedding/Agent 0/0。缓存也是合成，不评价模型质量。
- ACK 丢失通过主动跳过 ACK 和新 worker 实例模拟；不宣称实际 OS worker kill。
- 五种生命周期：retire、replace、complete、cancel、synthetic physical_delete，
  都验证故障时与重启投递前的规范读取，投递后墓碑和延迟旧写入不复活。
- 自有容器 `codecrew-memory-fault-9be718d0537845b38f4252d369b13ec0` 已删除；
  只保留该结果目录的合成 bind storage、SQLite、report。没有删卷、prune 或改真实库。
- 探针之后原 `127.0.0.1:6333/collections` 仍正常响应且为空；原服务未停启。

新增离线 38 项；扩大定向 738 passed、两条既有依赖警告；Ruff/diff/旧冻结通过。
真实停服探针仅运行一轮即通过。生产 SLA、故障持续时间统计、云成本、真实提炼/
回答质量、集群 failover 与规模性能未在此步验收，详见[边界和运行说明](../../docs/memory-qdrant-faults.md)。
