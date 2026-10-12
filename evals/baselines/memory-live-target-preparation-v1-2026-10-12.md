# 真实记忆验收：独立持久目标准备 v1

2026-10-12，第 2/5 个子步骤。只准备，不启动 worker/chat，不计入模型质量/速度评测。

原始私有记录：`.codecrew/memory-live-acceptance/20261012T093544Z-5295c08a/prepare.json`。
运行 `.venv/bin/python -m scripts.prepare_memory_live_target --prepare` 经授权执行。
工具默认计划无副作用；显式准备新库/集合，失败保留资源和固定类型，未删除旧目标。

## 实际准备结果

| 项目 | 实际观察 |
| --- | --- |
| 独立数据库 | 同目录 chat.sqlite3，schema 29，重开后统计不变 |
| 词项模式 | 新实例 BM25，FTS usable，空索引 |
| 同步目标 | 注册 1，模型指纹 `adf175dd…11e6cf`、1024 维 |
| Qdrant namespace | `codecrew_live_real_9485ed6d69e14fbd`，模型隔离 sync_v1 collection |
| Qdrant 服务 | 127.0.0.1:6333，version 1.19.0，原 Docker 命名卷持久挂载 |
| 集合 | green，1024/Cosine，9 项 payload 索引含 sync_revision，0 points |
| 规范记录/缓存/队列 | rooms/messages/memories/embeddings/outbox 全 0 |
| 只读启动预检 | 目标/schema 通过；关闭 SQLite 连接后 fresh metadata GET 通过 |
| 启动/调用 | worker/chat 未启动；embedding/Agent 0/0 |

集合完整名和 target_id 留私有 manifest。新目标不复用已有集合；UUID 命名碰撞
先拒绝，不修复/删除另一个目标。不执行 claim、run_once、重投、扫描或导入历史事实。

## 维度依据与边界

复用缓存 `evals/results/memory-fts-ab-v1/20261011T113351Z-8f218a82/embedding-cache.json`。
文件 SHA-256 `f4e2e6619980bf14f567b2658491bef59fb780f065fa9800ccb975c5dd85020e`。
SharedEmbeddings.load 的 checksum、cloud_api/complete、题库摘要、向量有限数/一致维度
检查通过；34 条均 1024 维。模型指纹（含 API 根地址/model/revision）与当前配置一致，
model=text-embedding-v4、revision=pilot-2026-10。只提取结构证据，不复制向量/标注记忆。

**没有请求百炼，不能证明当前密钥有效或供应商后端未变。** 第 4 步真实运行前仍需
获准使用真实模型；异常维度由现有索引/投递守卫拒绝，不混写集合。
Context7 用于核对 Qdrant 1.19.0 集合内统一向量大小，未按文档示例删除任何既有集合。

## 私有配置与备份

prepare.json 只保存非秘密 runtime_overrides：新 DB、BM25/Qdrant、专属 prefix、
新 workspace/runtime 目录和 worker false。未复制 API key、未改 `.env`，也未将这些
设置加载到当前 Fake 页面；以后启动须显式消费**这个既有 manifest**，不能重新 prepare。
新实例目录 0700、数据库/manifest 0600，Git 忽略；SQLite WAL/SHM 增补忽略防误提交。

新库在线备份位于同目标 `backups/20261012T093545Z-6fdc340c/manifest.json`，complete=true，
完整性/外键通过、文件 SHA-256 重算一致。未执行覆盖源库恢复或停机恢复演练。
现有 Qdrant 使用 `codecrew-memory_qdrant-data` 卷挂载 `/qdrant/storage`；只读检查挂载，
没有重启/停止 Docker 或删除容器/集合/卷。新目标保留供后续验收。

旧 codecrew.db 保持 schema 19、8 rooms/36 messages；既有 Fake 演示库保持 schema 25、
2 rooms/20 messages/3 memories/0 embeddings。全局 SQLite/legacy、worker false 不变。
备份/新目标都不是向旧库写入，未迁移原聊天历史。

## 测试与状态

12 项新增工具控制测试：无副作用计划、空库/权限/无密钥、独立 namespace、缓存摘要/
指纹不匹配拒绝、缺缓存、外部服务拒绝、命名碰撞不复用、部分失败保留、非空远端不通过、
旧库不打开。它们用临时 SQLite/假远端/合成证据，不冒充真实云测试。
与 13 项备份测试合计 **25 passed**；扩大定向 **1151 passed**，两条既有警告、非全仓。
Ruff/diff 与旧 24 题 ordered sources 冻结通过（TP/FP/FN=10/0/2），历史 memory.py 哈希
提示保留；本步未改生产记忆代码或历史 manifest。

当前真实运行阶段 **2/5**，下一步是显式启动新目标 worker。真实模型聊天、重启闭环、
无人值守托管、并发 SLA 均不由本步宣称已完成。
