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

当前目录有完整 Python 源码，但不是 Git 工作区；不依赖远程原始仓库，也不假定它是 Agent-100-Days 的 fork。这里只借鉴其教学安排，把本地 AgentForge 作为贯穿案例。

每日建议：20 分钟补概念、35 分钟读代码、45 分钟实验、20 分钟整理与口头讲解。超过时间的任务先留下最小可验证结果，不用通读所有历史报告。基础熟悉就直接做自测；卡住再回看参考课程。

## 四周路线

| 周 | 学习日 | 主题 | 本周产出 | 面试要能回答 |
|---|---|---|---|---|
| [Week 1](D:/agent/AgentForge/learning/week1.md) | 1–7 | 从问题到离线 RAG 主链路 | 架构图、最小离线流水线、3 分钟介绍 | 一个问题如何变成带来源的回答？ |
| [Week 2](D:/agent/AgentForge/learning/week2.md) | 8–14 | 代码理解、路由与回答可信度 | AST 小实验、路由案例表、证据验证反例 | 为什么不把所有问题都交给向量检索？ |
| [Week 3](D:/agent/AgentForge/learning/week3.md) | 15–21 | 向量、评测与有界 Agent | 手算指标、检索对照、工具循环实验 | 如何证明改进有效，如何阻止 Agent 失控？ |
| [Week 4](D:/agent/AgentForge/learning/week4.md) | 22–28 | 模型接入、工程质量与面试交付 | 故障分析、一个小改进、演示与项目讲稿 | 你解决了什么难题，证据是什么？ |

推荐阅读顺序：README → 当周文件 → 当日指定函数与测试。学习产出记录在 [进度表](D:/agent/AgentForge/learning/PROGRESS.md)，面试整理使用 [面试准备单](D:/agent/AgentForge/learning/INTERVIEW.md)。

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
| Week 2 的英语 Agent、Week 7 的长期记忆、Week 12–14 的完整平台 | 本轮不设必做任务 | 与当前只读代码问答主线关系较弱；岗位明确要求时再补 |

参考课程总目录：[Agent-100-Days README](D:/agent/Agent-100-Days/README.md)。本计划不要求迁移到 LangChain，也不要求安装原课程的全部依赖。

## 当前项目事实：读源码时采用的起点

以下来自本地代码和截至 2026-10-27 的最新记录，**不是本次规划重新执行的验收**。

| 能力 | 学习时应采用的表述 | 主要依据 |
|---|---|---|
| 离线扫描、切块、SQLite、词法检索 | 已有可运行实现，默认离线摘要不进行模型推理 | `scanner.py`、`chunker.py`、`store.py`、`retriever.py`、`providers/offline.py` |
| Python 符号、导入/调用与包关系 | 静态分析；部分结果可能不确定，不能代表运行时关系 | `parsers/`、`package_relations.py` |
| 产品问答路由 | `search` 保持词法检索；`ask` / 只读 `agent` 使用产品检索链路，可选择符号/关系等通道 | `cli.py`、`product_retrieval.py` |
| 本地真实问答 | AF-011 记录一次真实 Qwen 请求及引用、独立语义断言通过；范围是该验收案例 | [真实聊天报告](D:/agent/AgentForge/AF011_CHAT_ACCEPTANCE_REPORT.md) |
| 有限步工具循环 | 已有确定性离线 Planner；真实模型自主选择工具尚未验收 | `agent_loop.py`、[路线记录](D:/agent/AgentForge/ROADMAP.md) |
| Embedding / RRF | 有向量实现、真实历史实验与 RRF 原型；没有证明普遍优于词法，RRF 产品化仍暂缓 | [泛化结果](D:/agent/AgentForge/AF009GR5_EVALUATION.md)、[产品进入评测](D:/agent/AgentForge/AF010PRE_EVALUATION.md) |
| 测试质量 | 历史覆盖率为行 91.41%、分支 86.00%；该批次有 3 跳过。后续 265/265 通过来自用户宿主确认，未保存原始日志 | [STATUS 末尾记录](D:/agent/AgentForge/STATUS.md)、[覆盖率报告](D:/agent/AgentForge/TEST_COVERAGE_REPORT.md) |

README、ARCHITECTURE、ROADMAP 和验收报告中有历史阶段内容。遇到矛盾时，把「哪一天、哪个阶段、实现/实验/产品状态」列出来，再核对当前源码与最新专项记录，不把旧失败直接当成当前结论。

## 动手约定

学习笔记和个人练习建议放在 `learning/notes/`、`learning/labs/`；这些目录由你开始练习时再建。先写最小版本，再对照生产实现。一次只改一个变量，记录输入、输出和观察，避免只抄代码。

初期复现使用现有隔离测试与 `IndexStore(":memory:")`。默认 CLI 的 `index` 和 `agent` 会更新项目固定索引，因此它们不作为入门练习命令。需要改动文件的实验使用自建小样本或测试临时目录，不改原有冻结 fixture 和历史结果。

真实模型不是这 28 天的完成前提。可以先读历史报告、使用 Mock 响应学习协议；后续确需运行时再核对模型、服务配置和实验入口。生产配置不因学习实验而自动调整。

## 今天即可开始

在项目目录运行以下命令，然后完成 Week 1 的 Day 1：

```powershell
Set-Location D:\agent\AgentForge
python -X utf8 -B -m agentforge --help
python -X utf8 -B -m unittest tests.test_integration.IntegrationTests.test_full_cli_pipeline_preserves_synthetic_source -v
```

第二条命令复现现有测试中的 CLI 流程，索引使用内存数据库。自己记录实际结果；本规划没有代替你执行这次学习验收。

## 完成标准与时间调整

每个学习日满足三个条件才标记完成：能用自己的话解释原理、留下指定的实验或文档产物、回答当日面试问题。运行了测试但不懂为什么通过，只记为「已复现」，不要记成「已掌握」。

第 7、14、21 天未达到周验收时，用下一个学习时段补薄弱项；可以延长自然周期，但保留先修顺序。每天只有 1 小时时，把一个 Day 分成两次；每天有 3 小时且基础扎实时，可合并熟悉的概念日，仍保留 4 次复盘与最终演示。进阶包图增量算法、Reranker、长期记忆、Web UI 和部署留作下一轮，不挤占本轮项目主线。
