# 本机 Qdrant 真实存储验收（2026-10-11）

第二阶段第 2/7 步完成；仅验证适配器与真实服务的存储协议，不评价模型召回质量，
不代表聊天已切换 Qdrant、可靠同步已实现或 ANN 已启用。

## 环境和部署

- 用户手动安装并启动 Docker Desktop 4.94.0；Engine 29.8.2，Compose v5.5.1。
- 使用 `compose.memory-qdrant.yml` 获授权启动，镜像 `qdrant/qdrant:v1.19.0`，linux/arm64。
- 本机实际镜像 RepoDigest：`qdrant/qdrant@sha256:057ee3a8da769fe7310dd3537b4dc7583bf87a95ce8ac43c0af5a46bc580d1fc`。
- 根接口确认 version 1.19.0，commit `74f3e85b9473c62560006c043e13737ce6b48412`。
- 容器 `codecrew-memory-qdrant-1` 仅发布 `127.0.0.1:6333`；卷 `codecrew-memory_qdrant-data` 保留。
- Codex shell 缺少 Docker 凭据工具路径，启动命令临时添加 Docker 安装目录，未修改系统 PATH。

## 真实证据

获授权运行 `.venv/bin/python -m evals.check_memory_qdrant --live`：
`evals/results/memory-qdrant-probe-v1/20261011T125430Z-d9f071d6/report.json`。
报告 `complete=true`、`storage_checks_passed=true`、`cleanup_complete=true`。

| 检查组 | 结果 |
| --- | --- |
| 显式创建集合、八项 payload 索引、重复 upsert | 通过 |
| 候选范围内精确 cosine（1.0、约 0.8） | 通过 |
| 内容哈希过期的候选在 limit=1 前过滤 | 通过 |
| 同逻辑 ID 不同房间隔离、房间限定重复删除 | 通过 |

合成 3 维向量；embedding/Agent 调用 **0/0**。不读取规范聊天数据库，不改私有 `.env`。
探针 finally 只删除本次随机 collection，并 GET 确认不存在；随后 `/collections`
返回空列表。没有删除持久化卷，容器保持运行。实际请求 `params.exact=true`，不是 ANN 基准。
源代码三个 SHA-256 随报告保存；原始报告在 Git 忽略的本机结果目录，不是公开原始归档。

本轮存储契约测试：`tests/test_qdrant_vector_backend.py` 与
`tests/test_vector_backend_contract.py`，**72 passed**。上一开发轮扩大定向 **511 passed**，
本轮未重新运行全仓或质量/性能对照，不能将这些测试等同为端到端上线验收。

## 失败历史保留

初次 Homebrew 安装失败与服务未启动报告保留，见[部署记录](../../docs/memory-qdrant-storage.md)。
容器启动后，沙箱 probe 首次 GET 仍因连接权限失败：
`evals/results/memory-qdrant-probe-v1/20261011T125406Z-43b7e611/report.json`，
`complete=false`、检查为空；未创建测试集合。获授权宿主运行才完成上述验收。
不覆盖失败记录或将失败尝试计入成功检查。

下一步是 outbox、幂等重试与版本/墓碑保护；当前生产召回继续使用原 SQLite 向量路径。
