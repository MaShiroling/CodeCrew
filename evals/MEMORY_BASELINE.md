# 记忆评测基线 v1：固定用例与人工标注

数据位于 [`memory-baseline-v1.jsonl`](memory-baseline-v1.jsonl)，一行一个独立用例；[`memory-baseline-v1.schema.json`](memory-baseline-v1.schema.json) 定义字段结构。20 条全部是合成对话，不含真实用户秘密，开发协作和个人日常各 10 条。Kimi / 词项检索的首轮真实结果已经完成，冻结摘要见 [`baselines/kimi-memory-v1-2026-10-08.md`](baselines/kimi-memory-v1-2026-10-08.md)；百炼云 embedding 的真实混合检索 pilot 见 [`baselines/kimi-hybrid-memory-pilot-2026-10-09.md`](baselines/kimi-hybrid-memory-pilot-2026-10-09.md)。这些历史 pilot 不是严格 A/B；后续另用 24 条冻结题完成了[配对检索 A/B](baselines/memory-retrieval-ab-v1-2026-10-10.md)，不覆盖本页原始结果。

当前全部证据的统一索引见[记忆模块 v1 交付说明](../docs/memory-v1-delivery.md)，其中区分检索 A/B、首次端到端评测、已见回归和性能基准。

归属守卫后的 Kimi 调试集 12 条与留出集 8 条重跑结果见 [`baselines/kimi-attribution-guard-2026-10-08.md`](baselines/kimi-attribution-guard-2026-10-08.md)：写入误报从 1 条降到 0，正确写入 16 条全部保留；检索仍有 1 条漏召回。两次是非确定性模型重放，不应把检索分数变化归因于写入守卫。

[`memory-baseline-v1.splits.json`](memory-baseline-v1.splits.json) 将用例固定分为调试集 12 条和留出集 8 条，两组都平衡开发/个人场景。首次 20 条全量运行已观察过留出集，所以它只能降低直接按样本调参的风险，不能称作未见过的盲测集。后续严格泛化评测需要新采集、未公开给调试过程的用例。

## 用例语义

- `messages` 按顺序提供前置消息；`id` 是用例内部的稳定来源坐标。`room` 和 `discussion` 是符号标识，重放器应分别映射到真实房间和 `correlation_id`。`external: true` 表示外部来源。
- `query` 是稍后提出的检索问题，**不计入本用例的写入评分**。相同 `discussion` 用于检查当前讨论不重复注入刚写的记忆；不同 `room` 用于检查隔离。
- `gold.writes` 是应自动保存的记忆。`fact` 是语义标准答案，不要求 Agent 逐字生成；`replaces` 指向应被纠正的旧来源。未列出的消息都不应产生自动记忆。
- `gold.active_sources` 是全部前置消息结束后的有效版本；`gold.recall_sources` 是回答 `query` 时应被召回的来源，可能为空，即使房间内存在不相关的有效记忆。
- `answer_must_include` / `answer_must_not_include` 是人工评审要点，而非简单的字符串包含测试。否定句、同义表达和上下文必须由评审者判断。
- `tags` 用于按风险与检索类型切片，不作为运行时输入。

运行静态校验：

```bash
.venv/bin/python evals/validate_memory_baseline.py
.venv/bin/pytest tests/test_memory_eval_dataset.py
```

校验器检查 JSON Schema、用例 ID、开发/个人分类、来源角色、外部讨论、替换链、有效版本、跨房间/跨讨论召回边界，以及两组划分是否无重叠且覆盖全部 20 条。修改用例后要重新运行，并由人复核 `fact` 与回答要点是否忠实于原消息；结构校验不能判断语义标注是否正确。

## 重放与评分

[`run_memory_baseline.py`](run_memory_baseline.py) 已实现逐条重放：每条用例使用独立临时 SQLite 数据库，走现有聊天室持久化、Agent 调度、自动记忆写入和 `chat_context()` 召回路径。`--agent codex` 选择白金，`--agent kimi` 选择月见；评测提示词额外要求 `handoff_to=[]`，合成对话中的 @提及统一指向所选 Agent。若出现其他角色回合或 Agent 失败，该用例标记 incomplete，不计入质量分数。角色变换及提示词约束是评测条件，不能冒充未经修改的真实聊天流量。

默认命令**只验证用例，不调用模型**：

```bash
.venv/bin/python evals/run_memory_baseline.py
```

单条真实预演或完整 20 条真实运行必须显式加 `--live`，会消耗 Codex 额度：

```bash
.venv/bin/python evals/run_memory_baseline.py --live --case dev_pr_prefix
.venv/bin/python evals/run_memory_baseline.py --live
```

若使用月见（Kimi）独立建立 20 条基线，先在运行环境配置 Kimi Code 会员密钥
`KIMI_MODEL_API_KEY`，然后运行：

```bash
.venv/bin/python evals/run_memory_baseline.py --live --agent kimi
.venv/bin/python evals/run_memory_baseline.py --live --agent kimi --split tuning
.venv/bin/python evals/run_memory_baseline.py --live --agent kimi --split holdout
```

也可追加 `--prompt-for-kimi-key`，在交互式终端隐藏输入密钥；密钥不会写入
仓库文件或命令行参数。

Kimi 使用现有独立聊天室的只读隔离适配器，20 条均由月见回答。不同 Agent 的结果
必须分别成组，不能将 Codex 已完成的 7 条与 Kimi 后续用例拼接计算指标。
若额度或基础设施故障中断，可用 `--resume-from <上次运行目录>` 创建新运行，
仅复用同一 Agent、同一数据集与同一生产记忆代码下连续完成的用例；原运行保留。

要评估混合检索，先按[聊天室长期记忆配置](../docs/chat-memory.md#可选的云端向量检索)在私有环境设置 `CODECREW_MEMORY_EMBEDDING_*`（包括 `MODE=on`），再显式运行：

```bash
.venv/bin/python evals/run_memory_baseline.py --live --agent kimi --memory-mode hybrid
```

`--memory-mode lexical` 是默认值，不调用 embedding 服务；`hybrid` 会将合成记忆和问题发送到配置的云服务，可能产生费用。为减少污染，应使用同一 Agent、相同数据集与划分分别运行 lexical / hybrid；不能用旧运行直接续跑另一变体。`--resume-from` 还会核对变体、模型身份和相似度门槛。门槛应先在调试集固定，再看留出集；这些用例已被首轮全量运行观察过，留出集不等于真正盲测。

### 离线阈值评测器

[`sweep_memory_thresholds.py`](sweep_memory_thresholds.py) 读取冻结的 Kimi 关键词基线结果，保留模型实际提炼出的记忆文本与来源边界，先检查重新计算的关键词召回是否与冻结结果逐题一致。它不重跑 Kimi，也不评分回答质量。默认命令只验证输入、报告缺少的向量数量；加 `--embed` 才会用当前 `CODECREW_MEMORY_EMBEDDING_*` 配置向云服务发送**合成**记忆和问题，按模型指纹缓存向量。同一文本只向量化一次，之后可纯离线调整阈值：

```bash
.venv/bin/python evals/sweep_memory_thresholds.py --run evals/results/memory-baseline-v1/20261008T035513Z-7be7ea17
.venv/bin/python evals/sweep_memory_thresholds.py --run evals/results/memory-baseline-v1/20261008T035513Z-7be7ea17 --embed
.venv/bin/python evals/sweep_memory_thresholds.py --run evals/results/memory-baseline-v1/20261008T035513Z-7be7ea17 --thresholds 0.48,0.52,0.56,0.60
```

上面是 **12 条调试集**。候选阈值应先写入锁定文件；留出集要求 `--candidate`，不接受阈值列表，避免看完留出结果再扫参数。已锁定并单次重放的 0.50 候选见 [`memory-threshold-candidate-v1.json`](memory-threshold-candidate-v1.json)，结果见[留出集报告](baselines/embedding-threshold-holdout-2026-10-09.md)：

```bash
.venv/bin/python evals/sweep_memory_thresholds.py \
  --run evals/results/memory-baseline-v1/20261008T061157Z-6f2bffa6 \
  --candidate evals/memory-threshold-candidate-v1.json --embed
```

这些原始结果位于 Git 忽略目录，其他电脑需先取得对应的冻结结果文件。缓存只存文本 SHA-256 与浮点向量，不存密钥或原文；报告会分别给出原 20 条用例的来源级 TP/FP/FN、以及 [`memory-threshold-controls-v1.json`](memory-threshold-controls-v1.json) 中“同房间无关记忆”反事实对照的误召回数。后者是新增的合成压力测试，**不是**原 20 条标准答案，也需人工复核其语义无关性。向量均预先算好，因此此脚本不测运行时首次补索引延迟或 Agent 答案效果；留出集也已被此前真实基线观察过，不是严格盲测。

已完成的百炼调试集扫描见 [`baselines/embedding-threshold-tuning-2026-10-09.md`](baselines/embedding-threshold-tuning-2026-10-09.md)。它提供候选范围，不自动改写生产阈值。

锁定 0.50 的 20 条 Kimi 真实混合检索结果见 [`baselines/kimi-hybrid-memory-pilot-2026-10-09.md`](baselines/kimi-hybrid-memory-pilot-2026-10-09.md)。生产默认门槛仍为 0.65；历史关键词运行与新混合运行不是同代码、同模型输出的严格 A/B。

用户委托的 10 组无关记忆负例与 16 条实际写入内容的逐项 Agent 裁决见 [`baselines/memory-controls-fidelity-audit-2026-10-09.md`](baselines/memory-controls-fidelity-audit-2026-10-09.md)。负例保留，自动提炼内容的来源忠实性仍需改进；该审阅不是独立人类标注。

审阅后的自动写入已改为原文片段校验；最终版完整 20 条 Kimi 混合检索回归与离线边界测试见 [`baselines/kimi-extractive-memory-pilot-2026-10-09.md`](baselines/kimi-extractive-memory-pilot-2026-10-09.md)。旧关键词/混合运行使用自由改写记忆，不能与新运行直接比较阈值效果。

内容忠实性难例另放在 [`memory-fidelity-v1.json`](memory-fidelity-v1.json)，不修改上述 20 条冻结基线。运行 `.venv/bin/python evals/check_memory_fidelity.py`，可离线检查 16 条标注提议在真实 SQLite 自动写入路径中的结果；开发和个人场景各 8 条。新增防线会拒绝“还没决定”却标为决定、以及截掉紧邻“只/仅/限”条件的片段。此评测没有真实 Agent 输出或云费用，只是写入边界回归，不能衡量模型选择记忆的准确率。

同一批难例可用 `.venv/bin/python evals/run_memory_fidelity_live.py --live --prompt-for-kimi-key` 做**单 Agent、每例一回合**的真实 Kimi 重放。密钥只在本机终端隐藏输入，不要贴进聊天或提交到仓库；若当前 shell 已安全设置 `KIMI_MODEL_API_KEY`，可省略提示参数。先用 `--case dev_scope_dropped` 做单例探测，再运行全部 16 条；每轮可能消耗 Kimi 额度，但不调用 embedding 服务。结果保存在 Git 忽略的 `evals/results/memory-fidelity-<labels>/<run-id>/`，包括代码/数据集指纹、逐例写入和滚动汇总；失败回合不计作质量漏写并停止本次运行。`live_expected_write` 指真实 Agent 是否应提炼原消息，和离线的 `expected_content`（针对**给定反事实提议**的接受/拒绝）是两个不同标签。机器只检查是否写入、类型和原文包含关系；保存内容是否完整表达否定、范围、条件和时间，仍须逐例语义复核，结果不能称为人工验收前的通过率。

本机 Kimi 全量 16 条真实重放及逐例 Agent 审阅见 [`baselines/kimi-memory-fidelity-live-2026-10-09.md`](baselines/kimi-memory-fidelity-live-2026-10-09.md)。原始标签下机器结果 15/16；用户已裁决唯一争议项可作为 `open_loop` 写入，报告保留原始计数与裁决过程，不追溯改写 v1 数据集。

用户裁决单独固定在 [`memory-fidelity-v2-adjudications.json`](memory-fidelity-v2-adjudications.json)，含 v1 数据集 SHA-256 和 `dev_unresolved_decision` 的 `open_loop` 标签。`run_memory_fidelity_live.py` 现在默认采用 v2 标签；`--labels v1` 可复现旧评测语义，结果目录和 manifest 均区分版本。离线 `check_memory_fidelity.py` 仍测试原 v1 **反事实提议**的写入守卫，不因为真实 Agent 选择了另一种合法提议而改变。

针对普通回复把“这不是我的偏好”误说成“我不喜欢”，新增 [`memory-answer-quality-v1.json`](memory-answer-quality-v1.json) 的 4 条平衡难例（第三方偏好未归属、本人明确偏好各有开发/个人场景），并在聊天室 Agent 提示词中要求将未表态的本人立场保持未知。运行 `.venv/bin/python evals/run_memory_answer_quality.py` 仅验证输入；真实 Kimi 回归使用 `.venv/bin/python evals/run_memory_answer_quality.py --live --prompt-for-kimi-key`，每例一回合、不调用 embedding，结果写入 Git 忽略的 `evals/results/memory-answer-quality-v1/<run-id>/`。此运行器**不自动给回答打语义分**；需要对照每例 `expected_answer` 和 `forbidden_inference` 逐项复核。假 Agent 测试只验证提示词和重放路径，不代表真实 Kimi 已通过。

首轮真实 Kimi 结果与逐项 Agent 审阅见 [`baselines/kimi-memory-answer-quality-pilot-2026-10-09.md`](baselines/kimi-memory-answer-quality-pilot-2026-10-09.md)：4/4 回合完成、4/4 记忆边界正确，但回答语义仅 3/4；`personal_friend_not_mine` 的具体点餐建议仍暗含用户不吃香菜。加固提示词后，该失败用例单独重跑已通过；修订版尚未在同一提示词下重跑另外 3 条，因此不能称为新的全量 4/4 回归。

随后在同一提示词下全量重跑仍发现：Kimi 虽承认用户口味未知，却提出“先不主动加或另放”的具体操作。用户明确裁决：**替本人点餐前应先询问，不能预设不加**。该轮回答语义仍是 3/4，详见同一[评测报告](baselines/kimi-memory-answer-quality-pilot-2026-10-09.md)。裁决另存 [`memory-answer-quality-v2-adjudications.json`](memory-answer-quality-v2-adjudications.json)，v1 数据集和原始运行不改；`run_memory_answer_quality.py` 现默认 v2，`--labels v1` 可重放原始标注。新提示词下失败用例的 v2 单例重跑已通过，剩余 3 条尚未做同版全量回归。

每次运行在被 Git 忽略的 `evals/results/memory-baseline-v1/<run-id>/` 新建目录，保存代码与数据集 SHA-256、逐题 JSON 和滚动 `summary.json`。混合变体还记录 embedding 模型、版本、身份指纹和相似度门槛，不记录密钥。当前 Codex 适配器未锁定具体模型，因此 manifest 会明确标记“CLI 默认模型”；若要做严格可复现的 A/B 比较，应先增加显式模型选择。无记忆变体目前只提供离线控制接口，真实命令支持 lexical 与 hybrid。

机器汇总只计算**来源级**写入 precision/recall 与 Recall@3，不判断提炼内容是否忠实、回答是否正确。逐题 `answer_review` 保持 `pending_human_review`；冻结报告中的语义预审也不能代替用户人工验收。`fact`、纠正沿革和回答要点需要人复核，不能用字符串命中或模型自评分替代。Agent 执行失败和超时不算作记忆漏写。不要修改标准答案迎合模型输出。

第一批 20 条是评测程序和标注口径的 pilot，不足以证明总体效果。扩容前建议由用户复核场景代表性，并对争议样本保留人工裁决记录。

## `open_loop` 完成/取消真实回归

[`memory-open-loop-v1.json`](memory-open-loop-v1.json) 固定 6 条合成对话：开发/个人场景各 3 条，明确完成、明确取消各 1 条；另外 4 条检验否定、条件、疑问和同房间相似待办歧义。每条先在临时 SQLite 房间预置经来源校验的 `open_loop`，再让月见（Kimi）处理一条新的 Human 消息。它只测**已有待办的关闭**，不把最初的记忆提炼是否成功混入闭环指标。评测不调用 embedding；Agent 仍走正式聊天室提示词、解析、服务端关闭守卫与持久化路径。

运行 `.venv/bin/python evals/run_memory_open_loop_live.py` 只校验数据集；可先用 `.venv/bin/python evals/run_memory_open_loop_live.py --live --prompt-for-kimi-key --case dev_explicit_complete` 单例探测，再移除 `--case` 跑全部 6 条。密钥在终端隐藏输入，不要粘贴到聊天或提交仓库。结果保存到 Git 忽略的 `evals/results/memory-open-loop-v1/<run-id>/`，包括数据集/代码指纹、逐例状态、Agent 闭环提议是否被服务端接受、额外写入及滚动汇总；不保存密钥。回合失败不计质量错误并停止后续用例。

`--case` 可重复指定多个用例；只复验两条自由回复时可用 `--case dev_not_completed --case personal_question_complete`，一次隐藏输入密钥即可运行这两条。

优先验收 **0 条误关**；明确完成/取消的漏关可通过页面手动处理，但要记录是 Agent 未提议还是服务端拒绝。机器状态匹配只是第一层，仍须逐例审阅模型回复和关闭依据。单次 6 条是功能 pilot，不足以声称线上误关率为零。

首次真实 Kimi 全量结果及审阅见 [`baselines/kimi-open-loop-pilot-2026-10-09.md`](baselines/kimi-open-loop-pilot-2026-10-09.md)。当时状态 6/6 匹配、误关 0，但回复中有未说明歧义和虚构关闭提醒的问题。服务端结果确认逻辑已在运行后修订，必须重新运行才能验收新回复；不要沿用旧的 6/6 作为新代码的通过率。

第二轮真实 Kimi 全量重跑也记录在同一[报告](baselines/kimi-open-loop-pilot-2026-10-09.md)：状态仍 6/6 匹配，服务端确认回复已解决首轮两处问题，但自由回复仍把“未完成”推断成“未开始”，并错误承诺只说“完成了”即可自动关闭。现已进一步收紧提示词，第二轮运行发生在该收紧**之前**，需重新做语义复核。

两条针对性重跑后，第三轮同版完整 6 条仍出现“只说已完成/已取消即可关闭”的不可靠建议。因此现在对唯一匹配活跃待办的未完成、条件和状态询问使用服务端回复，并机械预检所给示例；详见同一[报告](baselines/kimi-open-loop-pilot-2026-10-09.md)。该修订后的三条风险用例已由真实 Kimi 定向重跑，3/3 状态与回复复核通过；**尚未做同版完整 6 条重跑**，不能沿用第三轮 6/6 作为新代码的全量验收结果。

## `open_loop` 多轮端到端旅程

[`memory-open-loop-journey-v1.json`](memory-open-loop-journey-v1.json) 增加开发/个人各 1 条合成旅程，分别测试完成与取消。与上面的 6 条关闭测试不同，这里**不预置记忆**：第一轮由月见从 Human 原话自动提炼 `open_loop`，第二轮以独立话题发送 Human 关闭声明，先验证待办确实进入 Agent 的跨话题上下文，再由月见提出关闭。随后重建存储对象读取同一 SQLite，检查状态、原始来源、关闭来源和新话题检索中不再出现已关闭待办。查询只检查检索上下文，不再调用第三次模型；全程不调用 embedding。回复语义仍需人工审阅。

`.venv/bin/python evals/run_memory_open_loop_journey.py` 只校验数据集。真实单例探测：`.venv/bin/python evals/run_memory_open_loop_journey.py --live --prompt-for-kimi-key --case dev_created_then_completed`；通过后移除 `--case` 跑两例。密钥只在本机终端隐藏输入，不要贴进聊天或提交仓库。结果在 Git 忽略的 `evals/results/memory-open-loop-journey-v1/<run-id>/`，包括逐例写入与关闭提议、状态及重启后检索、代码/数据指纹和滚动汇总。模型回合失败不计作质量失败；若 Agent 成功但没写出 `open_loop`，记录为旅程失败，不伪造关闭阶段。

两条分别运行的 Kimi 真实旅程及后续确定性状态问答的版本边界已冻结在[旅程验收报告](baselines/kimi-open-loop-journey-2026-10-10.md)。不要把旧旅程的“重启后不召回”混同为“已让真实 Kimi 回答第三轮状态问题”。
