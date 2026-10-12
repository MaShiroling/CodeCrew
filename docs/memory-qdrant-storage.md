# 第二阶段第 2 步：本机 Qdrant 存储接入

2026-10-11：用户选择 **Docker + Qdrant 本机服务**。适配器、可选配置、Compose 和
真实服务探针已开发；**离线测试与真实 Qdrant 存储联调均通过，第 2/7 步完成**。
首次安装尝试失败后，用户手动安装/启动 Docker；获授权启动容器并完成合成向量探针。
这仅验存储协议，不代表 outbox、聊天召回接线、ANN 或检索质量已经验收。

## 当前实现

[`QdrantMemoryVectorBackend`](../app/chat/qdrant_vector_backend.py)实现上一子步骤的
upsert/delete/search 存储协议，采用薄 REST 客户端，不增加 Python SDK 依赖。
构造不联网、不创建集合；`initialize()` 是显式建集合/索引操作，绝不重建或删除
已经存在的业务集合。按照 Context7 核对的官方 API 选择以下协议：

- [集合配置](https://api.qdrant.tech/api-reference/collections/create-collection)：模型指纹
  SHA-256 对应独立 collection，固定维度和 Cosine；读取现有配置，不兼容则报错。
- [payload 索引](https://api.qdrant.tech/api-reference/indexes/create-field-index)：来源、
  房间、状态、内容哈希和模型指纹用 keyword，维度用 integer；缺失才补建，冲突不覆盖。
- [写入](https://api.qdrant.tech/api-reference/points/upsert-points)与
  [删除](https://api.qdrant.tech/api-reference/points/delete-points)：使用 `wait=true`，
  只有操作返回 completed 才报告成功；相同点幂等覆盖，没有隐式自动重试。
- [query 接口](https://api.qdrant.tech/api-reference/search/query-points)：所有候选锚点
  带进服务端 filter，房间/active/讨论排除/模型/维度与每条来源/内容哈希在 limit 前
  限定。返回 metadata 再严格检查，异常或范围越界结果整体失败，不静默当成功空结果。

Point UUID 由 `(room_id, memory_id)` 确定性生成，避免另一房间的同 ID 覆盖。
payload 只保存八个检索字段，不保存记忆原文。默认 collection 前缀
`codecrew_memory`；每次实例只绑定一个模型 collection，不实现跨模型删除或切换。
旧模型 collection 清理仍属后续回填/恢复步骤；第 3 步现已增加
[独立版本化 namespace 的 outbox/墓碑](memory-vector-outbox.md)。本页旧适配器自身
仍没有投递或乱序栅栏，不与版本化目标混用。

为验证前一步完整候选和稳定同分排序，本步 `params.exact=true`，取回全部允许候选
（最多 200）后按 cosine 降序/记忆 UUID 升序，再应用调用方 limit；不先裁成 3 条。
**使用 Qdrant 服务不代表这一轮已经测试或启用了 ANN。** 后续性能对照再决定策略。

Qdrant adapter 拒绝零模长向量，不默默把它转换成有效方向；原 SQLite 对照的 -1
规则保留。错误向量是参数错误，不是成功空召回。外部适配器不能在 SQLite 同库
事务中验证事实，调用方的规范写前验证和最终召回复验仍必需；第 5 步现由
[版本化召回编排](memory-qdrant-recall.md)实现，不使用本页无版本写入端口接聊天。

## 配置与默认

[`.env.example`](../.env.example)只增加占位配置，未修改本机私有 `.env`：

```dotenv
CODECREW_MEMORY_VECTOR_STORE=sqlite
CODECREW_MEMORY_QDRANT_URL=http://127.0.0.1:6333
CODECREW_MEMORY_QDRANT_COLLECTION_PREFIX=codecrew_memory
CODECREW_MEMORY_QDRANT_API_KEY=
CODECREW_MEMORY_QDRANT_TIMEOUT_SECONDS=5
```

`build_memory_vector_backend(settings, database, model)` 只在显式调用时选用新存储。
第 5 步已增加独立的 `build_memory_vector_index`：embedding on + VECTOR_STORE=qdrant
明确选择版本化召回，默认仍 sqlite/原 MemoryVectorIndex。使用前必须显式准备/投递
正确目标；构造不自动初始化或订阅，详见[接线操作](memory-qdrant-recall.md)。
构造仅创建客户端；初始化集合是另一个明确动作。查询 collection 缺失/维度冲突
会报 VectorBackendError，不当作成功空结果，也不读取时自动创建。

HTTP 仅允许 loopback，其他地址要求 HTTPS；禁止 URL 内凭据、非根路径、query 与
fragment。API key 使用 SecretStr 配置、endpoint repr 不含 key/URL；请求不跟随
重定向、不继承环境代理，不打印 provider 错误正文。2 MB 响应上限、单请求 5 秒
默认超时（可设到 30 秒），失败不自行重试。总操作有多个请求，不是整体 5 秒 SLA。

## Docker 部署与历史环境问题

[`compose.memory-qdrant.yml`](../compose.memory-qdrant.yml)固定镜像
`qdrant/qdrant:v1.19.0`（[对应发布](https://github.com/qdrant/qdrant/releases/tag/v1.19.0)），
REST 仅绑定 127.0.0.1:6333，不暴露 gRPC；独立命名卷保留数据，关闭遥测，不设置
自动重启。不运行 `down -v`，不会清空既有业务卷。没有默认公网暴露或云账单。

首次本机检查时没有 docker 命令或 Docker.app。获授权执行 `brew install --cask docker`
未成功：Homebrew 还报告 Command Line Tools 过旧，Docker Desktop 下载发生连接重置。
没有执行日志里的系统目录删除建议、修改 SDK 或代用户接受 Docker 服务条款。
安装尝试包含 Homebrew 自更新/工具下载，不等于宿主环境完全无修改。

当时真实 probe 在沙箱及获授权宿主环境都未完成：
`evals/results/memory-qdrant-probe-v1/20261011T123055Z-baa464ae/report.json`
记录 `complete=false`、0 成功检查、0 embedding/Agent；在首次 collection GET 即失败，
没有建测试集合或删除任何集合；当时没有创建 Qdrant 卷或启动容器。

用户随后安装 Docker Desktop 4.94.0，Engine 29.8.2，Compose v5.5.1。
Codex shell 未包含 Docker CLI/凭据工具路径，启动命令临时补入
`/Applications/Docker.app/Contents/Resources/bin` 与 `/Users/mashiro/.docker/bin`，
没有改系统 PATH。获授权拉取并启动 `qdrant/qdrant:v1.19.0`（linux/arm64），
容器 `codecrew-memory-qdrant-1` 仅发布 `127.0.0.1:6333`。
真实服务根接口确认版本 1.19.0；独立持久化卷 `codecrew-memory_qdrant-data` 保留。
镜像摘要及成功报告见[真实存储验收记录](../evals/baselines/memory-qdrant-storage-v1-2026-10-11.md)。

其他机器需要先安装并首次打开 Docker Desktop，完成条款/系统确认；
可以走 [Docker 官方 Mac 安装页](https://docs.docker.com/desktop/setup/install/mac-install/)
的 Apple silicon 下载入口，避开当前失败的 Homebrew 下载。安装后检查：

```bash
docker version
docker compose version
```

若提示命令不存在，按安装器提示重新打开终端或检查用户 Docker CLI 路径；
`docker version` 要有 Server 信息，只有 Client 不代表 engine 已运行。
此后可由用户明确启动或让 Codex 请求运行授权：

```bash
docker compose -f compose.memory-qdrant.yml up -d
.venv/bin/python -m evals.check_memory_qdrant --live
```

## 验证工具和当前证据

[`check_memory_qdrant.py`](../evals/check_memory_qdrant.py)默认只打印计划，不读 Settings/
私有配置、不联网。显式 `--live` 创建随机 `codecrew_probe_<uuid>` collection，
检查建集合/索引、重复写、精确 cosine、过期锚点过滤先于 limit、房间隔离和幂等删除；
finally 只清理该随机测试集合并确认消失。碰到已存在的同名集合不采用、不删除。
业务 collection 和数据卷不碰。使用人工合成向量，只验存储连接，不算模型召回评测。
清理失败会记录 collection 名，`complete=false`，不得继续宣称验收通过。

新增 **36 项离线测试**通过（fake REST/默认与失败探针），覆盖集合/索引兼容、
过滤生成及复验、确定性排序、范围删除、错误响应/超时、隐私、缺集合、构造无副作用、
probe 不误删既有集合等。开发轮扩大记忆/评测/聊天/CLI 定向套件
**511 passed**，两条既有依赖警告；Ruff、旧词项冻结复验和 diff 检查通过。
本轮真实探针报告：
`evals/results/memory-qdrant-probe-v1/20261011T125430Z-d9f071d6/report.json`。
四组检查通过，`complete=true`、`cleanup_complete=true`；测试 collection 已删除，
服务 `/collections` 返回空列表。沙箱首次连接失败单独保留为失败记录，不计成功。
本轮复跑两组存储契约测试 **72 passed**。未运行全仓测试或新的质量/延迟 A/B，
embedding/Agent 调用 0 次；没有操作真实聊天库或修改私有 `.env`。

```bash
.venv/bin/python -m pytest -o addopts='' -q tests/test_qdrant_vector_backend.py
.venv/bin/python -m evals.check_memory_qdrant  # plan only
```

真实服务 probe（含清理）通过后，第 2/7 步完成。随后第 3/7 的
[outbox 可靠同步](memory-vector-outbox.md)也已完成，需显式注册/运行 worker；
第 4/7 [历史回填/审计/非破坏式重建](memory-vector-maintenance.md)也已完成。
第 5/7 [可选混合召回](memory-qdrant-recall.md)已接线，但 chat-serve 不自动投递或
切默认；第 6/7 [实际故障/恢复矩阵](memory-qdrant-faults.md)随后完成，当前总体为
7/7：[质量/延迟/规模对照](memory-qdrant-benchmark.md)随后完成，保留 SQLite 默认，
未开启 ANN 或生产自动 worker。
