# 28 天源码导读进度

本表完全可选，只用于标记读到哪里。课程本身是概念讲解和源码导读，不要求运行程序、做练习或提交产物。

状态可填写：未开始 / 阅读中 / 已读懂 / 想再看一次。遇到不熟悉的术语，可以记在“想再看”一栏；按周文件给出的源码顺序继续阅读即可。

| 状态 | Day | 主题 | 想再看的一点 |
|---|---:|---|---|
| [ ] | 1 | [项目问题与数据流](days/day01-project-flow.md) | |
| [ ] | 2 | [数据对象与 Provider](days/day02-data-contracts.md) | |
| [ ] | 3 | [扫描与安全读取](days/day03-scanning.md) | |
| [ ] | 4 | [切块与行范围](days/day04-chunking.md) | |
| [ ] | 5 | [SQLite、PostgreSQL 与 Chroma](days/day05-storage.md) | |
| [ ] | 6 | [检索、上下文与证据](days/day06-retrieval-context.md) | |
| [ ] | 7 | [一次请求的完整链路](days/day07-end-to-end.md) | |
| [ ] | 8 | [AST 与符号索引](days/day08-ast-symbols.md) | |
| [ ] | 9 | [导入解析与不确定性](days/day09-name-resolution.md) | |
| [ ] | 10 | [PackageRelation 关系](days/day10-package-relations.md) | |
| [ ] | 11 | [查询意图与产品路由](days/day11-query-routing.md) | |
| [ ] | 12 | [Evidence ID 与来源核验](days/day12-citation-identity.md) | |
| [ ] | 13 | [Answer Contract 与拒答](days/day13-answer-contract.md) | |
| [ ] | 14 | [从查询到可核查回答](days/day14-trustworthy-answer.md) | |
| [ ] | 15 | [Embedding 与向量候选](days/day15-embeddings.md) | |
| [ ] | 16 | [Recall@K 与 MRR](days/day16-retrieval-metrics.md) | |
| [ ] | 17 | [标签来源与数据隔离](days/day17-evaluation-data.md) | |
| [ ] | 18 | [RRF 与产品取舍](days/day18-rrf.md) | |
| [ ] | 19 | [工具合同与只读执行器](days/day19-tool-contracts.md) | |
| [ ] | 20 | [有界循环与停止条件](days/day20-agent-loop.md) | |
| [ ] | 21 | [Agent 决策边界](days/day21-agent-boundaries.md) | |
| [ ] | 22 | [本地聊天 Provider](days/day22-local-provider.md) | |
| [ ] | 23 | [安全信任边界](days/day23-security-boundaries.md) | |
| [ ] | 24 | [测试类型与证据范围](days/day24-testing-evidence.md) | |
| [ ] | 25 | [阅读和判断代码改动](days/day25-code-change.md) | |
| [ ] | 26 | [指标与性能数字](days/day26-metrics-performance.md) | |
| [ ] | 27 | [沿请求链理解架构](days/day27-explain-project.md) | |
| [ ] | 28 | [合并四周的系统心智图](days/day28-system-map.md) | |

## 项目基线提示

本学习目录描述 AgentForge 当前分支代码。历史验收记录说明的是对应批次和环境，不自动代表个人已读懂，也不代表当前环境重新运行过。SQLite、PostgreSQL、Chroma 的职责和当前实现边界见 [README](README.md)；会话与 Memory 的详细源码路线见 [CONVERSATION_AND_MEMORY.md](CONVERSATION_AND_MEMORY.md)。
