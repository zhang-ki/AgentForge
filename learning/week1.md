# Week 1｜读懂离线 RAG 的完整数据流

本周目标：从命令入口追到最终证据摘录，独立完成一个最小离线流水线。先理解数据怎么走，再讨论模型。

参考课程：[Day 29 RAG](D:/agent/Agent-100-Days/week5/29.RAG是怎么工作的.md)、[Day 31 文本分块](D:/agent/Agent-100-Days/week5/31.文本分块.md)、[Day 32 检索技术](D:/agent/Agent-100-Days/week5/32.检索技术.md)。每天只补与任务相关的小节。

## Day 1｜项目解决什么问题

- **学习内容**：代码问答的输入、证据与输出；离线检索、模型问答和 Agent 工具循环的区别。
- **读哪里**：[README](D:/agent/AgentForge/README.md)、[CLI](D:/agent/AgentForge/agentforge/cli.py)、[集成测试](D:/agent/AgentForge/tests/test_integration.py)。README 先读直接运行、检索回答和 Agent 三部分。
- **动手任务**：运行学习总览中的帮助和单个集成测试；选择 `refresh_access_token`，画出 index/search/ask 各自经过的模块。标记数据何时写入索引、何时送给 Provider。
- **产出与验收**：一页项目卡片，包含用户、输入输出、5 个核心模块、3 个当前限制；能指出离线回答为什么无需模型。
- **面试问题**：这个项目为什么需要 RAG？与直接把整个仓库塞进提示词相比，解决了什么问题、增加了什么成本？

## Day 2｜数据对象与接口边界

- **学习内容**：dataclass、不可变对象、Protocol；把检索结果与生成接口分开。
- **读哪里**：[数据对象](D:/agent/AgentForge/agentforge/models.py)、[Provider 接口](D:/agent/AgentForge/agentforge/providers/base.py)、[离线 Provider](D:/agent/AgentForge/agentforge/providers/offline.py)。
- **动手任务**：在个人练习中手动创建一个 `SearchResult`，走 `build_context` 和 `OfflineProvider.answer`；另做空证据案例。用一张表列出 SearchResult → Evidence → BuiltContext → Answer 的字段流转。
- **产出与验收**：两个案例输出及字段流转表；指出 repository、行范围、content_hash、source_id 各自解决什么问题。
- **面试问题**：为什么不把检索结果直接用字符串传遍系统？Provider 抽象的收益和过度抽象的风险是什么？

## Day 3｜文件扫描与读取边界

- **学习内容**：允许扫描的范围、排除规则、资源上限；路径检查与真正打开文件之间的区别。
- **读哪里**：[scanner](D:/agent/AgentForge/agentforge/scanner.py)、[safeio](D:/agent/AgentForge/agentforge/safeio.py)、[扫描测试](D:/agent/AgentForge/tests/test_scanner.py)。先看 `scan_repository` 与 `read_candidate`，Windows 句柄细节留到 Day 23。
- **动手任务**：阅读并运行扫描测试；整理普通 `.py`、`.env`、非 UTF-8、超大文件、链接五类输入的预期处理。修改实验仅在自己的临时样本里做。
- **产出与验收**：输入→接受/跳过/失败→原因表；区分单文件跳过与整个扫描不完整。权限导致的跳过原样记录。
- **面试问题**：文件路径检查通过后，为什么读取仍可能有风险？为什么不能不加限制地遍历仓库？

## Day 4｜切块、行号与重叠

- **学习内容**：窗口大小、overlap、换行处理、内容哈希；检索覆盖与证据可定位性的关系。
- **读哪里**：[chunker](D:/agent/AgentForge/agentforge/chunker.py)、[切块测试](D:/agent/AgentForge/tests/test_chunker.py)。
- **动手任务**：自己生成 20 行文本，用窗口 6/重叠 2 和窗口 10/重叠 2 切块；先手算行范围，再运行。补空文本、CRLF、末尾换行三个小案例。独立写一个只处理普通换行的简化切块器。
- **产出与验收**：两组窗口对照及简化实现；所有片段能反查原始行范围，解释简化实现与生产版的差距。
- **面试问题**：为什么 chunk 越大不一定越好？重叠会怎样影响检索结果、上下文和成本？

## Day 5｜SQLite 与增量索引

- **学习内容**：文件/分块数据表、事务、内容版本、更新和删除、Schema 迁移。
- **读哪里**：[indexer](D:/agent/AgentForge/agentforge/indexer.py)、[store](D:/agent/AgentForge/agentforge/store.py) 的 `sync`、[存储测试](D:/agent/AgentForge/tests/test_store.py)。今天不逐行研究全部迁移代码。
- **动手任务**：通过存储测试或自建临时仓库与内存库，观察首次索引、未变化、修改一个文件、删除一个文件四种情况；找出事务失败保留旧状态的测试或代码路径。
- **产出与验收**：四次操作前后 files/chunks/changed/unchanged 对照；能解释哪些记录失效、为什么不能提交半个扫描结果。
- **面试问题**：为什么这里用 SQLite？文件内容变化后，如何避免返回旧索引？数据库事务保护了什么？

## Day 6｜词法检索、上下文与离线回答

- **学习内容**：查询分词、关键词与路径权重、稳定排序、去重、上下文预算、无证据处理。
- **读哪里**：[retriever](D:/agent/AgentForge/agentforge/retriever.py)、[context](D:/agent/AgentForge/agentforge/context.py) 的 `build_context`、[检索测试](D:/agent/AgentForge/tests/test_retriever.py)、[上下文测试](D:/agent/AgentForge/tests/test_context.py)。
- **动手任务**：在内存索引上分别检索函数名、文件路径、中文词和不存在的词；对同一组候选使用 100 与 6000 的预算，比较最终证据。串起 index → lexical search → context → offline answer 学习版流程。
- **产出与验收**：四类查询表和预算对照；说明这里的预算是 UTF-8 字节代理量，不是精确 tokenizer 计数。明确这个学习版流程不包含完整产品路由。
- **面试问题**：为什么检索 Top-K 不等于最终上下文片段数？有候选但预算装不下时应该怎么回答？

## Day 7｜周复盘与第一次项目讲解

- **学习内容**：把模块知识组织成问题→方案→结果→限制。
- **复习范围**：Day 1–6 产物；对照 [ARCHITECTURE](D:/agent/AgentForge/ARCHITECTURE.md) 的基础数据流，并注意后续 AF-011 对产品路由的扩展。
- **动手任务**：脱离源码画一次链路图；演示有结果、无结果、预算不足三类输入；录制 3 分钟项目介绍，再检查是否把离线模板说成模型推理。
- **产出与验收**：架构图、可运行学习版流程、3 分钟讲稿；能从任一输出引用追到文件和行范围。
- **面试问题**：如果回答错误，你先查检索、上下文还是模型？给出分层定位步骤。

本周检查：能追踪一次请求；能独立解释切块和增量更新；能完成离线演示。未通过时优先补 Day 4–6，不增加新的框架。
