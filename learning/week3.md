# Week 3｜向量检索、质量评测与有界 Agent

本周目标：能用数据判断一个方案是否有用，并理解工具循环的执行边界。先修：候选、证据、路由与回答验证。

参考课程：[Day 32 检索技术](D:/agent/Agent-100-Days/week5/32.检索技术.md)、[Day 23 工具调用](D:/agent/Agent-100-Days/week4/23.工具调用的基本机制.md)、[Day 51 Agent 组成](D:/agent/Agent-100-Days/week8/51.Agent%20基本组成.md)、[Day 100 评估与测试](D:/agent/Agent-100-Days/week15/100.评估与测试.md)。

## Day 15｜Embedding 与相似度

- **学习内容**：向量维度、归一化、余弦、query/document 角色差异；Embedding 模型与聊天模型的分工。
- **读哪里**：[Embedding 合同](D:/agent/AgentForge/agentforge/embeddings/base.py)、[向量存储](D:/agent/AgentForge/agentforge/embeddings/vector_store.py)、[向量测试](D:/agent/AgentForge/tests/test_embeddings.py)。
- **动手任务**：手算三个二维向量的余弦相似度，再写标准库函数核对；阅读零向量、维度错误、NaN 和内容版本失效的测试。人工向量只用于理解数学，不标记为真实语义实验。
- **产出与验收**：手算表、简化相似度函数、输入异常表；解释当前 SQLite 顺序向量扫描与 ANN 索引的差异。
- **面试问题**：相似度高就代表答案正确吗？为什么模型或预处理改变后不能直接混用旧向量？

## Day 16｜Recall@K、MRR 与无证据问题

- **学习内容**：相关集合、排名、宏平均、无证据查询的独立统计。
- **读哪里**：[evaluation](D:/agent/AgentForge/agentforge/evaluation.py) 的 `_metrics`、`evaluate_benchmark`、[评测测试](D:/agent/AgentForge/tests/test_evaluation.py)。
- **动手任务**：手写 3 个有证据问题，其中一个有两个相关文件，另设 2 个无证据问题；手算 Recall@1/3 与 MRR，再写独立函数核对。先确认项目采用的分母和去重单位。
- **产出与验收**：带排名和公式的计算表；有多相关文件时 Recall@1 不能被误当成命中率；无证据问题不强塞进 Recall/MRR。
- **面试问题**：为什么 Recall@5 很高，用户仍可能觉得不好用？Top-1、MRR 和拒答指标分别反映什么？

## Day 17｜评测集、人工审核与数据泄漏

- **学习内容**：合成标签、AI 暂定标签、人工审核；Calibration/Validation、leakage group、冻结规则。
- **读哪里**：[evaluation](D:/agent/AgentForge/agentforge/evaluation.py) 的 `validate_split_plan`、[人工审核表](D:/agent/AgentForge/AF009E_HUMAN_REVIEW.md) 的少量样例、[provisional](D:/agent/AgentForge/agentforge/provisional.py)。
- **动手任务**：自己写 8 个学习用问题，含 4 个有证据、4 个无证据，并包含至少两组同义改写；给出来源或无证据搜索范围，按组划分。不要改冻结数据，也不要把 8 条小样本包装成可靠质量结论。
- **产出与验收**：学习样本表，含问题、标签、证据范围、标注人、group、split；同组问题不跨集。
- **面试问题**：为什么不能看到验证集结果后反复调阈值？AI 生成的 ground truth 有什么风险？

## Day 18｜比较词法、向量与融合

- **学习内容**：基线、适用查询、RRF 排名融合、排名与拒答的区别。
- **读哪里**：[fusion](D:/agent/AgentForge/agentforge/fusion.py)、[融合测试](D:/agent/AgentForge/tests/test_provisional_fusion.py)、[真实泛化报告](D:/agent/AgentForge/AF009GR5_EVALUATION.md)、[产品进入评测](D:/agent/AgentForge/AF010PRE_EVALUATION.md)。报告只抓方法、结果和决定。
- **动手任务**：用两个各含 3 个文件的排名手算 `1/(k+rank)`，对照 RRF 实现；运行 `python -X utf8 -B -m agentforge.eval_retrieval` 复现离线基线。真实向量对照引用历史报告，明确自己本日未运行模型。
- **产出与验收**：一页技术选择记录：基线、备选、实际证据、限制和决定。标明 RRF 是按文件融合，并解释为什么不直接相加不同通道的原始分数。
- **面试问题**：为什么做了向量和 RRF 却没默认上线？一个方案在小样本上改善，是否足够进入产品？

## Day 19｜工具协议与执行器

- **学习内容**：ToolCall、工具白名单、参数校验、执行结果；模型提议与实际执行的分离。
- **读哪里**：[agent_loop](D:/agent/AgentForge/agentforge/agent_loop.py) 的 `ToolCall`、`ReadOnlyToolbox`、`_strict_arguments`、[Agent 测试](D:/agent/AgentForge/tests/test_agent_loop.py)。
- **动手任务**：为 index/search/read/list-symbols 写一张参数表；跟踪一个合法 read 和未知工具、多余参数、越界路径三个拒绝案例。实验使用测试中的内存库。
- **产出与验收**：工具合同表和拒绝记录；能指出拒绝发生在哪一层，而不只说「Prompt 要求模型不要做」。
- **面试问题**：工具调用返回合法 JSON，就能执行吗？工具执行器需要独立验证哪些条件？

## Day 20｜有界循环、审计与停止条件

- **学习内容**：Planner → Tool → Observation，步数预算、总预算、协作式超时、终态与审计。
- **读哪里**：[agent_loop](D:/agent/AgentForge/agentforge/agent_loop.py) 的 `OfflineAgentPlanner`、`AgentLoop.run`、[Agent 测试](D:/agent/AgentForge/tests/test_agent_loop.py)。
- **动手任务**：用假 Planner 和假工具写一个 30–60 行学习版循环；覆盖正常结束、刚好耗尽步数、仍有后续步骤、工具失败四种情况。对照现有实现的停止方式。
- **产出与验收**：最小循环与四条执行轨迹；区分「已完成且用了全部步骤」和「尚未完成但预算耗尽」。说明协作式超时不能强行中断任意阻塞函数。
- **面试问题**：如何避免 Agent 无限循环？超时后为什么不一定适合直接重试？

## Day 21｜周复盘：模型 Planner 的设计题

- **学习内容**：把确定性流程扩展为模型决策时，哪些合同必须保持稳定。
- **复习范围**：Day 15–20；参考 [Day 68 任务分解失败模式](D:/agent/Agent-100-Days/week10/68.任务分解的失败模式与修复.md)、[Day 74 HITL 触发条件](D:/agent/Agent-100-Days/week11/74.HITL%20的触发条件设计.md)。
- **动手任务**：只写一页设计，不接真实模型：Planner 输入/输出、可用工具、停止信号、无效参数处理、证据不足处理、用户介入点。用两段模拟模型响应走过 Day 20 的学习版循环。
- **产出与验收**：设计图、合法与越界两条轨迹、5 分钟技术取舍讲解；明确当前生产 Planner 是确定性的，模拟响应不算真实模型 Agent 验收。
- **面试问题**：接入 function calling 后，系统就一定更自主、更可靠吗？你会先验证什么？

本周检查：能手算指标与 RRF；能发现数据泄漏；能实现有停止条件的循环。真实 Embedding 服务或聊天服务不可用时，仍可完成本周必做任务。
