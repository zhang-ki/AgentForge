# AgentForge：28 天项目学习与面试规划

制定日期：2026-10-29。面向：能独立写 Python 项目、了解一些 Agent/RAG，希望用 AgentForge 准备求职面试的学习者。

## 学习目标与节奏

沿用 Agent-100-Days 的「每周主题 → 每日原理与实战 → 周末复盘」形式，缩短为 **4 周、28 个学习日，每天约 2 小时，总计约 56 小时**。这是工作量估算；Day 表示学习顺序，不要求连续自然日完成。第 7、14、21、28 天以复盘和讲解为主。

完成后应能：

1. 用 3 分钟解释 AgentForge 的问题、用户流程、模块职责和能力边界。
2. 独立写出简化版切块、检索、上下文、指标计算和有界工具循环。
3. 用实验解释：为什么选词法/符号/向量检索，为什么引用有效不等于答案正确。
4. 展示一次端到端流程、一份失败案例分析和一份本人复现的证据记录。
5. 区分已有项目实现、自己读懂的部分、自己复现或修改的部分，准确回答面试追问。

当前目录包含完整 Python 源码和本地 Git 元数据；学习计划不依赖远端历史，也不假定 AgentForge 是 Agent-100-Days 的 fork。这里只借鉴其教学安排，把当前本地 AgentForge 作为贯穿案例。

每日建议：20 分钟补概念、35 分钟读代码、45 分钟实验、20 分钟整理与口头讲解。超过时间的任务先留下最小可验证结果，不用通读所有历史报告。基础熟悉就直接做自测；卡住再回看参考课程。

## 四周路线

| 周 | 学习日 | 主题 | 本周产出 | 面试要能回答 |
|---|---|---|---|---|
| [Week 1](week1.md) | 1–7 | 从问题到离线 RAG 主链路 | 架构图、最小离线流水线、3 分钟介绍 | 一个问题如何变成带来源的回答？ |
| [Week 2](week2.md) | 8–14 | 代码理解、路由与回答可信度 | AST 小实验、路由案例表、证据验证反例 | 为什么不把所有问题都交给向量检索？ |
| [Week 3](week3.md) | 15–21 | 向量、评测与有界 Agent | 手算指标、检索对照、工具循环实验 | 如何证明改进有效，如何阻止 Agent 失控？ |
| [Week 4](week4.md) | 22–28 | 模型接入、工程质量与面试交付 | 故障分析、一个小改进、演示与项目讲稿 | 你解决了什么难题，证据是什么？ |

推荐阅读顺序：README → 当周文件 → 当日指定函数与测试。学习产出记录在 [进度表](PROGRESS.md)，面试整理使用 [面试准备单](INTERVIEW.md)。

## 从原课程保留什么、略过什么

| Agent-100-Days 内容 | 本计划安排 | 使用方式 |
|---|---|---|
| Week 1：LLM、Token、Embedding | Day 1、15、22 | 按需复习；不重复系统学习 Transformer 推导 |
| Week 3：Prompt、结构化输出 | Day 12–13、22 | 围绕证据、引用和输出合同学习 |
| Week 4、8：工具与 Agent | Day 19–21 | 先理解本项目的确定性循环，再设计模型 Planner |
| Week 5：RAG | Day 3–6、15–18 | 用真实项目模块替代框架教程 |
| Week 6：上下文工程 | Day 6、12–13 | 关注预算、来源、过期证据和拒答 |
| Week 9–11：反思、任务分解、HITL | Day 20–21、26 | 学停止条件、失败恢复、人何时介入；不强做新系统 |
| Week 15：评估、监控、安全、性能 | Day 16–18、23–28 | 以已有测试、报告和本人小实验为依据 |
| Week 2 的英语 Agent、Week 7 的长期记忆理论、Week 12–14 的完整平台 | 只学习与当前实现有关的部分 | 项目已有受审批的长期 Memory；以本地实现为案例，不扩展为通用记忆平台 |

参考课程总目录：Agent-100-Days README（外部参考资料，本仓库不包含）。本计划不要求迁移到 LangChain，也不要求安装原课程的全部依赖。

## 当前项目事实：读源码时采用的起点

以下对照当前源码和 2026-10-31 保存的 AF-012-MVP 验收记录整理，**不是本轮重新执行的验收**。

| 能力 | 学习时应采用的表述 | 主要依据 |
|---|---|---|
| 扫描、切块与代码索引 | 文件、分块、符号和静态关系保存在项目 SQLite；词法/符号检索可离线运行 | `scanner.py`、`chunker.py`、`store.py`、`retriever.py` |
| 持久化会话 | PostgreSQL 保存仓库元数据、会话、消息、会话选择及有界历史摘录；按仓库身份隔离 | `af012_schema.py`、`conversation_store.py`、`conversation_console.py` |
| 向量检索 | 本地 Chroma 持久化代码/文档与 Memory 向量；候选必须回查 SQLite 或 PostgreSQL 权威数据 | `af012_runtime.py`、`embeddings/chroma_index.py`、`memory_retrieval.py` |
| 长期 Memory | 模型只提出候选；用户审批后 PostgreSQL 才标记生效，再由 outbox 显式同步 Chroma；查询按仓库、状态、来源和哈希复核 | `memory_extraction.py`、`memory_store.py`、`memory_retrieval.py` |
| Python 符号、导入/调用与包关系 | 静态分析；部分结果可能不确定，不能代表运行时关系 | `parsers/`、`package_relations.py` |
| 检索与 Agent 入口 | `search` 是词法查询；`ask` 使用产品意图路由，可按意图选择符号、PackageRelation、词法及可选 Vector；`agent` 是独立的确定性只读工具循环，不调用聊天 Provider | `cli.py`、`product_retrieval.py`、`agent_loop.py` |
| 本地真实问答 | AF-011 有真实问答验收；AF-012-MVP 另记录真实 Qwen Chat/Embedding 与 PostgreSQL/Chroma 联合验收。结论均限于各自案例 | [AF-011 报告](../RELEASE_NOTES.md)、[AF-012-MVP 报告](../RELEASE_NOTES.md) |
| 有限步工具循环 | 已有确定性离线 Planner；真实模型自主选择工具尚未验收 | `agent_loop.py`、[路线记录](../ROADMAP.md) |
| Embedding / RRF | 有向量实现、真实历史实验与 RRF 原型；没有证明普遍优于词法，RRF 产品化仍暂缓 | [泛化结果](../RELEASE_NOTES.md)、[产品进入评测](../RELEASE_NOTES.md) |
| AF-012-MVP 回归 | 保存的最新完整回归为 344 项运行、338 通过、0 失败、0 错误、6 项 Windows 链接权限跳过；另有 24/24 项真实宿主检查通过 | [最终验收报告](../RELEASE_NOTES.md)、[机器结果](../RELEASE_NOTES.md) |

当前默认行为仍有明确边界：控制台只有输入 `/chroma on` 后才在本次运行中启用 Chroma；`/chroma index` 负责同步当前索引快照。结构化 PackageRelation 由产品路由在导入、调用和公开 API 等关系意图下选择，不是对所有查询做融合；RRF 与生产拒答阈值没有启用。Memory 要经过用户批准，并由 `/memory sync` 同步向量。Reranker 未实现。

README、ARCHITECTURE、ROADMAP 和验收报告中有历史阶段内容。遇到矛盾时，把「哪一天、哪个阶段、实现/实验/产品状态」列出来，再核对当前源码与最新专项记录，不把旧失败直接当成当前结论。

## 动手约定

学习笔记和个人练习建议放在 `learning/notes/`、`learning/labs/`；这些目录由你开始练习时再建。先写最小版本，再对照生产实现。一次只改一个变量，记录输入、输出和观察，避免只抄代码。

初期复现使用现有隔离测试与 `IndexStore(":memory:")`。默认 CLI 的 `index` 和 `agent` 会更新项目固定索引，因此它们不作为入门练习命令。需要改动文件的实验使用自建小样本或测试临时目录，不改原有冻结 fixture 和历史结果。

真实模型不是这 28 天的完成前提。可以先读历史报告、使用 Mock 响应学习协议；后续确需运行时再核对模型、服务配置和实验入口。生产配置不因学习实验而自动调整。

## 今天即可开始

在项目目录使用 AF-012 隔离环境运行以下命令，然后完成 Week 1 的 Day 1：

```powershell
Set-Location <AgentForge project directory>
$Python = '.\.agentforge\af012-venv\Scripts\python.exe'
& $Python -X utf8 -B -m agentforge --help
& $Python -X utf8 -B -m unittest tests.test_integration.IntegrationTests.test_full_cli_pipeline_preserves_synthetic_source -v
```

第二条命令复现现有测试中的 CLI 流程，索引使用内存数据库。自己记录实际结果；本规划没有代替你执行这次学习验收。

## 完成标准与时间调整

每个学习日满足三个条件才标记完成：能用自己的话解释原理、留下指定的实验或文档产物、回答当日面试问题。运行了测试但不懂为什么通过，只记为「已复现」，不要记成「已掌握」。

第 7、14、21 天未达到周验收时，用下一个学习时段补薄弱项；可以延长自然周期，但保留先修顺序。每天只有 1 小时时，把一个 Day 分成两次；每天有 3 小时且基础扎实时，可合并熟悉的概念日，仍保留 4 次复盘与最终演示。AF-012 的会话、Chroma 和审批式 Memory 已纳入当前源码案例；PackageRelation 产品接入、Reranker、语义摘要与 Web UI 仍不属于本计划的默认实践目标。

AF-012 会话与记忆设计笔记：[CONVERSATION_AND_MEMORY.md](CONVERSATION_AND_MEMORY.md)。
