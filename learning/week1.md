# Week 1｜一次代码问答如何变成有来源的回答

这一周从用户输入开始，完整讲清 AgentForge 的数据流：命令入口、文件扫描、切块、SQLite 索引、检索、上下文与回答。请按 Day 1 到 Day 7 阅读；每一天是单独课文，后半都嵌入当天关键源码并逐段讲解，不需要切去源码文件。

| 课程 | 当天主题 |
|---|---|
| [Day 1：AgentForge 解决什么问题](days/day01-project-flow.md) | 代码问答、RAG 和项目主流程 |
| [Day 2：系统中的数据对象](days/day02-data-contracts.md) | SearchResult、Evidence、BuiltContext、Answer 和 Provider |
| [Day 3：扫描与安全读取](days/day03-scanning.md) | 仓库边界、文件身份和安全打开 |
| [Day 4：切块与行号](days/day04-chunking.md) | 代码片段、重叠窗口和可追溯位置 |
| [Day 5：索引与三种存储](days/day05-storage.md) | 仓库 SQLite、PostgreSQL、Chroma 的职责 |
| [Day 6：从检索候选到模型上下文](days/day06-retrieval-context.md) | 产品路由、证据选择和字节预算 |
| [Day 7：把主链路连起来](days/day07-end-to-end.md) | 从 CLI 到回答合同和会话保存 |

本周读完后，你会知道每一段代码接收什么、产生什么，以及为什么检索结果不能直接当成答案。
