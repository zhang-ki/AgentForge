# AgentForge 对话系统架构审计

日期：2026-11-06  
审计基线：`feature/1.1-core`，`09bc079ad17b1a2560199eadd679b72c7c779224` (`feat: add AF-012 multi-model support`)  
范围：当前 HEAD 中的产品调用链和生产模块；不把工作区外的历史验收结果视为当前代码能力证明。

## 基线与保护项

- 当前分支为 `feature/1.1-core`，工作区在审计开始时干净，HEAD 与 `origin/feature/1.1-core` 一致。
- `main` 为 `e6f4312d8b6372bc44701f1d04d37f08bdf7a896`；`v1.0.0-mvp` 指向冻结的发布历史，未触碰。
- `stash@{0}` 和 `backup/pre-remote-rollback-20261106` 均保留；本次不恢复 stash。
- 没有提交、推送或创建标签。

## 审计结论

当前代码不是一个有独立对话编排层的多能力问答系统。实际 `ask` 业务路径由 `cli.py::main` 直接完成：加载当前仓库和会话、绑定 Provider、可选初始化 Chroma 与记忆检索、调用 `ProductRetriever`、挑选候选证据、分配证据预算、执行证据角色门禁、准备历史摘要、调用 Provider、验证引用和 Answer Contract、持久化消息并构造响应 JSON。

当前 `ask` 只有仓库问答路径：命令要求当前 SQLite 仓库索引，检索为空时走离线拒答；Qwen 和 DeepSeek Provider 在空证据时也会回到离线拒答。CLI 参数和交互控制台均未提供 General、Repository、Hybrid 或 Auto 对话模式。因此 README/ARCHITECTURE/历史宿主结果中出现的 General/Hybrid/Auto 描述，不能证明当前 HEAD 已实现这些模式。它们是历史记录，必须保留，但需标记其适用代码快照。

### 主要结构问题

1. **CLI 是组合根，也承担用例编排。** `cli.py` 有 1,632 行，混合参数解析、会话状态、检索选择、证据门禁、模型调用、持久化、诊断和多个管理命令。`conversation_console.py` 又管理 UI 循环、仓库/会话/模型状态，并通过捕获标准输出的方式在进程内调用 `cli.main`。
2. **通用 Agent Loop 与产品问答相互独立。** `agent_loop.py::AgentLoop` 是受限只读工具循环；`agent` 命令使用它，普通 `ask` 不使用它。它不提供 General 问答，也不是 `ask` 的统一 Orchestrator。
3. **路由层次重叠。** `query_routing.py::ExplainableQueryRouter` 提供仓库检索通道意图；`product_retrieval.py::ProductIntentRouter` 对它包装并插入精确符号规则。二者都是仓库检索路由，尚无面向 General/Repository/Hybrid 的对话能力路由。
4. **候选与证据在类型上未分层到底。** `ProductCandidate` 和 `ProductRetrievalOutcome` 能保留检索通道、关系、角色和不确定性；但 `_context_candidates` 直接将一部分候选转换为 `SearchResult`，`context.py::build_context` 只做边界、去重和字节预算，不判断语义相关性或证据充分性。CLI 用角色覆盖和低置信度门禁近似判断充分性。系统没有单独的 `RelevantEvidence` / `SufficiencyDecision` 类型，也不应把召回本身当成相关或充分。
5. **Context Builder 实际分为两个边界。** `context.py` 负责源码 Evidence ID、摘录及引用输入；`conversation_context.py::plan_context` 负责系统消息、摘要、事实账本、获批 Memory、近期对话和对话字节预算。Provider 私有函数 `_model_messages` / `_conversation_messages` 再将模型预算和这两类上下文拼装，形成第三个协调点。
6. **回答校验不是语义蕴含验证。** `context.py` 验证模型引用 ID 属于本次请求并映射到应用掌握的路径、行号和原文；`answer_contract.py` 校验证据身份、来源和角色覆盖，并把生成结论标记为 `limited/inference`。它明确不能证明结论被引用原文蕴含，这是正确的安全边界，不应通过放宽合同弥补。
7. **Provider 仍理解产品证据策略。** Qwen/DeepSeek 共用 `_ANSWER_SYSTEM_PROMPT`、`_model_messages` 和 `_model_evidence_text` 等位于 `llama_server.py` 的私有函数。DeepSeek 直接导入这些 Qwen 模块内部帮助函数，造成提供方之间的实现耦合；回答提示也固定为“只基于仓库证据”，不支持 General/Hybrid 的不同策略。
8. **持久化职责基本可辨，但用例入口耦合。** `ConversationStore` 负责 PostgreSQL 会话/消息/摘要；`MemoryStore` 负责 PostgreSQL Memory 与其事件/outbox；`ChromaMemoryRetriever` 查询候选后回 PostgreSQL 校验；SQLite `IndexStore` 保存源码快照；Chroma 保存可重建向量。CLI 直接构造并协调这些对象。
9. **Streaming 尚无独立生命周期管理器。** DeepSeek 客户端处理 SSE，Provider 做最终内容/引用检查，CLI 将回调交给 Provider。没有统一的取消、超时、部分内容隔离和跨 Provider 流生命周期接口；当前代码只承诺已支持的 DeepSeek 流式路径，不能推断 Qwen 流式能力。
10. **存在两组静态依赖环。** `store.py` 导入 `repositories.repository_identity`，而 `repositories.py` 的索引打开函数延迟导入 `IndexStore`；`model_registry.py` 延迟调用 Qwen Client，`llama_server.py` 延迟使用 ModelRegistry 和 TokenBudgetManager，`token_budget.py` 又在运行时导入 ModelDescriptor。它们当前依靠函数内延迟导入工作，未必是模块初始化故障，但表示共享身份/模型元数据与服务适配器边界不清。
11. **当前 HEAD 的凭据模块为空且阻断多模型测试。** `agentforge/credential_store.py` 在提交 `09bc079` 中只有两个字节（字面量 `\\n`），因此 `tests.test_multimodel` 导入时发生 `SyntaxError`，多模型 Provider 与交互控制台无法正常导入。首次相关基线命令没有运行测试用例，退出码为 1。该缺陷已按当前公开接口、测试和凭据安全约束重新实现；没有从 stash 或旧分支恢复内容。凭据专项回归现为 14/14 通过，后续完整套件仍需执行。
12. **一个 Provider 模块也含字面换行尾缀。** `agentforge/providers/deepseek_agent.py` 最后一行在 HEAD 中以 `return None\\n` 结束，导致 Python 语法解析失败。现已只移除非法尾缀；没有改变工具范围或 Agent 行为。对 `agentforge/` 下全部 Python 文件做 AST 解析后为 0 个语法错误。

## 模块职责审计

| 当前模块 | 实际职责 | 输入 → 输出 | 审计判断 |
|---|---|---|---|
| `conversation_console.py` | 交互循环、模型/会话/仓库操作菜单、展示结果 | 用户文本/命令 → CLI 调用和终端输出 | UI 层；通过 `cli.main` 的输出捕获与 CLI 耦合 |
| `cli.py` | 参数解析、应用组合、所有命令处理；完整 `ask` 用例 | CLI 参数 → Provider、存储操作、JSON/退出码 | 过度承担用例编排；当前最应先拆分的边界 |
| `agent_loop.py` | 有界迭代式只读工具调用、审计、离线结论 | Objective + Planner + 工具 → AgentRunReport | 独立 Agent 工作流；不是对话问答编排器 |
| `query_routing.py` | 按文本选择仓库检索路由/通道 | Query → RouteDecision | 仅检索路由；名字易被误作总对话 Router |
| `product_retrieval.py` | 结构符号/静态关系/词法/可选向量检索并验证源码快照 | Query + index + scanner snapshot → ProductRetrievalOutcome/Candidate | Retriever 与轻量检索 Router 同模块，候选仍非已判定证据 |
| `context.py` | 构造行级源码上下文、分配请求本地 Evidence ID、校验引用格式/成员 | Query + SearchResult → BuiltContext；answer text → canonical citations | 引用身份层；不做语义相关性/蕴含判断 |
| `conversation_context.py` | 作用域校验、历史/摘要/事实/记忆选择、字节预算 | 当前请求和持久化上下文 → ContextPlan | 对话上下文策略；与源码证据上下文概念不同 |
| `answer_contract.py` | 检查引用来源、原文、角色覆盖和回答状态 | Answer + BuiltContext + provenance → VerifiedAnswerContract | 适合做证据合同；不是检索器，也不证明语义蕴含 |
| `models.py` | SearchResult、Evidence、BuiltContext、Answer 共享结构 | 不适用 | 结构紧凑，但 BuiltContext/Answer 当前以仓库证据场景为中心 |
| `providers/base.py` | Provider Protocol | BuiltContext → Answer | 接口过窄且隐含 Repository-only 合同 |
| `providers/llama_server.py` | 回环 HTTP 客户端、Qwen 回答/摘要、提示、上下文消息和 Token 检查 | BuiltContext/Conversation → Answer/summary | Client、Provider、模型消息策略有混合；被 DeepSeek 复用私有内部实现 |
| `providers/deepseek.py` | 固定云端端点客户端、响应/SSE/Usage 校验、DeepSeek 回答适配 | messages + model config → completion/Answer | 客户端和 Provider 同文件，但网络安全与不重试边界明确 |
| `providers/deepseek_agent.py` | DeepSeek 有界只读工具规划器 | Agent audit → ToolCall | Agent Planner 适配器；不得赋予任意工具权限 |
| `model_registry.py` | 静态 Qwen/离线定义、DeepSeek 能力元数据缓存/解析、Qwen identity 检查 | API metadata/cache → ModelDescriptor | Registry 与 Provider 健康检查混合，并依赖 Qwen Client |
| `token_budget.py` | token 估算/Tokenizer adapter、输入输出预算 | ModelDescriptor + request → TokenBudget | 概念职责单一；默认值标示为估算而非精确 tokenization |
| `conversation_store.py` | PostgreSQL 会话、消息、摘要、摘要游标事务 | Use-case operation → persisted rows | 持久层；不是 Session Manager/use-case orchestrator |
| `memory_store.py` / `memory_retrieval.py` / `memory_extraction.py` | Memory 权威状态、向量检索候选、候选提取与审批生命周期 | DB/Chroma/model → approved memory/candidates | 存储和检索已分开；CLI 将生命周期流程拼装 |
| `repositories.py` / `store.py` | 仓库注册/活动选择/索引打开；SQLite 索引和静态数据 | Repo path/source → per-repo SQLite index | 互相反向依赖共享身份函数，存在可解除的依赖环 |
| `embeddings/*` | Embedding Provider、Chroma code/memory index/retriever/adapter | Source/query → vector records/results | 可选向量基础设施；不是最终证据来源，结果仍须回读校验 |

## 候选、证据与合同状态

当前可确认的层次是：检索通道生成 `ProductCandidate` → CLI 候选选择 → `SearchResult` → `build_context` 生成 `Evidence(E1..)` → Provider 获得不可信原文 → Provider 检查引用 ID → `verify_generated_answer` 创建 `ContractEvidence/AnswerClaim`。缺口在于“相关性/充分性”并没有独立业务对象或评估器，角色覆盖是静态完整性检查，不是语义相关判断。

重构应显式保留四种状态：Retrieved Candidate、Relevant Evidence、Sufficient Evidence Decision、Cited Evidence。证据 evaluator 可以依据明示问题意图、通道/关系解析状态、证据角色和来源完整性给出可解释的结构性结论；不能伪称自动证明语义蕴含。独立语义验证未实现前，最终回答继续标记为有限结论。

## 初步目标依赖图

见 [DIALOGUE_SYSTEM_DEPENDENCY_GRAPH_20261106.md](DIALOGUE_SYSTEM_DEPENDENCY_GRAPH_20261106.md)。目标是保持 UI/CLI → Conversation Orchestrator → Router/Planner → Retriever/Evidence Evaluator → Context Builder → Provider → Answer Validator 的单向依赖，并由 Orchestrator 通过端口协调 Session/Memory/Persistence；Provider 仅依赖中立消息/上下文结构，存储实现不回调 UI、Provider 或 Orchestrator。

## 分阶段重构方案

### 阶段 0：基线与审计（已完成）

- 以 Git HEAD 为基线核对分支、状态、远端关系、stash 和备份。
- 建立本报告、依赖图和实施顺序；随后基线测试发现空凭据模块这一硬阻塞，并在下一阶段按当前接口修复。

### 阶段 1：解除基础依赖环、定义对话合同（进行中）

- 将 canonical repository identity 提取为纯领域函数，令 SQLite Store 不依赖 RepositoryRegistry。
- 将模型元数据与 Provider 健康检查解耦；TokenBudget 对 descriptor 使用协议/结构类型，避免运行时反向导入 Registry。
- 已提取纯 `repository_identity.py`，SQLite Store 不再依赖 RepositoryRegistry；Chroma 复用同一仓库身份函数。
- 已将 Qwen health/identity 探测从 ModelRegistry 移到本地 Provider health adapter；TokenBudget 不再运行时导入 ModelRegistry。
- 已实现被当前 HEAD 遗漏的 DPAPI 凭据模块，并通过 14 项凭据/控制台定向测试。
- 已修复 DeepSeek Agent 源文件的非法字面换行；对生产包 65 个 Python 模块做 AST 解析，当前 0 个语法错误。
- 增加明确的 `DialogueMode`、`DialogueRequest/Plan/Result` 和来源类型/证据判定对象；默认 ask 行为兼容地保持 Repository 模式。
- 定向测试现有 Provider、注册表、仓库索引、Answer Contract。

### 阶段 2：提取 Conversation Orchestrator

- 从 `cli.py::main` 移出单轮/会话问答用例协调；CLI 保留参数解析、依赖装配、终端协议。
- 将检索、上下文、Session/Memory 端口和 Provider 作为依赖传入；编排器返回稳定 `DialogueResult`，不直接打印或捕获 stdout。
- UI 通过 CLI facade 或明确的应用接口调用同一用例，不再复制/推导业务流程。
- 保持 repository 注册/索引、凭证确认和只读边界。

### 阶段 3：实现 General / Repository / Hybrid

- Repository：继续使用真实证据通道、来源身份、角色覆盖和现有失败关闭合同。
- General：不打开或要求仓库索引；通用回答不要求源码引用，也不把一般文本伪装成仓库证据。
- Hybrid：保留通用部分与仓库部分的独立来源；仓库部分只引用真实证据，证据缺失时标注不能由当前仓库确认；通用部分不强制引用源码。
- 增加显式用户模式和 Auto 的可解释路由；Auto 只选择能力，不提升证据可信度或授权云端 Provider。
- Qwen/DeepSeek 共享中立请求接口；云端授权、会话模型绑定、无静默回退规则保持不变。

### 阶段 4：统一证据与上下文验证

- 引入显式 Candidate → RelevantEvidence → SufficiencyDecision 转换，保留审计理由和源身份。
- Context Builder 同时接收能力计划与来源分区；历史、Memory 仍是不可信上下文，不成为仓库证据。
- Answer Validator 独立验证来源、引用、结构和合同状态；语义蕴含仍清楚标记未证明。
- 将消息预算装配从 Provider 私有函数移入中立 Context Builder/预算接口，Provider 只序列化请求和处理网络结果。

### 阶段 5：会话生命周期、Streaming 与故障边界

- 让 Session Manager 协调模型绑定、仓库绑定、消息写入/恢复；Repository/Memory/Conversation 数据仍由各自 Repository/Persistence 实现负责。
- 定义 provider-neutral streaming 生命周期（事件、结束、失败、取消）；只有 Provider 明确支持时才暴露，未实现的能力保持拒绝。
- 针对超时、取消、重复提交、数据库失败、Provider 非法响应和预算超限补充边界测试。

### 阶段 6：回归与验收

- 每阶段运行相应定向测试；最后运行完整离线套件、顺序/逆序核心测试和 coverage.py。
- 覆盖率目标按用户要求保留 90% 行、85% 分支，不通过排除模块达到。
- 真实 PostgreSQL、Chroma、Qwen、DeepSeek、流式和多轮宿主测试分别记录；未执行不能由离线测试替代。
- 只更新本轮报告和状态文件，不覆盖历史失败/通过记录；不提交、不推送。

## 审计基线与后续实施的关系

本报告前文、模块审计表和基线调用图记录的是提交 `09bc079ad17b1a2560199eadd679b72c7c779224` 的原始状态，不代表最终工作树。后续代码实施和验证结果追加在 [DIALOGUE_SYSTEM_FINAL_REPORT_20261106.md](DIALOGUE_SYSTEM_FINAL_REPORT_20261106.md)；新调用图见 [DIALOGUE_SYSTEM_DEPENDENCY_GRAPH_20261106.md](DIALOGUE_SYSTEM_DEPENDENCY_GRAPH_20261106.md) 的“实施后工作树”一节。

## 尚未完成（实施后）

实施后仍未完成：真实 PostgreSQL/Qwen/Embedding/DeepSeek 宿主验收、90%/85% 覆盖率门槛、语义蕴含验证、语义 Auto Planner、统一 Streaming 生命周期，以及独立 Session/Memory 应用端口。离线通过不代表这些目标已完成。

## Phase 2 implementation verification (2026-11-06)

The audit above is the pre-implementation baseline. Current worktree additions address parts of it: ConversationOrchestrator; bounded LLM Planner for Auto/Hybrid; application-owned citation identity validation; SessionService and MemoryService repository protocols; StreamingManager; and model-aware context budgets. A static AST scan parsed 77 agentforge modules and found no static import cycle.

The architecture is not fully cleanly layered: CLI still composes and handles lifecycle operations, protocols are co-located with services, and the orchestrator coordinates several stages. Evidence evaluation is structural, Planner has no iterative replan or ToolRegistry, and streaming does not publish content incrementally before validation. These are remaining gaps, not completed targets. See the final report and acceptance JSON.
