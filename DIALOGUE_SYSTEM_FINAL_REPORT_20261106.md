# AgentForge 对话系统重构 — 第二阶段最终报告

日期：2026-11-06  
状态：**PARTIAL**  
分支：feature/1.1-core  
基线 HEAD：09bc079ad17b1a2560199eadd679b72c7c779224

## 结论

工作树已加入 Auto/Hybrid 的结构化 LLM Planner、对话编排器、候选与证据评估边界、Session/Memory 服务协议、统一流事件管理器及模型感知 Token Budget。项目虚拟环境中的完整离线回归通过。

整体仍为 **PARTIAL**：全包行覆盖率 87.2883%、分支覆盖率 78.8844%，低于 90%/85% 门槛；本轮没有执行真实 Qwen、DeepSeek、PostgreSQL 或 Chroma 联合宿主验收。也没有外部 ToolRegistry、检索后迭代重规划或语义证据裁判。离线通过不等于真实验收通过。

## Git 与数据保护

- 当前分支 feature/1.1-core，HEAD 为 09bc079ad17b1a2560199eadd679b72c7c779224。
- main 为 e6f4312d8b6372bc44701f1d04d37f08bdf7a896；v1.0.0-mvp 为 c1b76bca72b1ffe2cec859bf383fa35410b79540。
- stash@{0} 和 backup/pre-remote-rollback-20261106 保留。
- 没有提交、推送、重置或标签操作；没有连接正式数据库、启动/停止服务或发送模型请求。
- 测试只使用离线合成数据。清理范围仅为本轮 AST 编译产生、按路径与时间确认的 99 个 fixture .pyc 文件和空 __pycache__ 目录；历史报告及其他既有缓存保留。没有执行正式数据的哈希前后比较。

## 实际调用链

用户输入 → CLI 依赖装配 → ConversationOrchestrator → Auto/Hybrid 时调用当前会话绑定 Provider 的 LLMPlanner → 严格解析计划 → 仓库子任务查询 ProductRetriever → 候选筛选与源码快照校验 → Evidence Evaluator 结构性评估 → 请求本地 Evidence ID 和模型上下文预算 → Qwen/DeepSeek 生成 → Answer Contract 校验引用身份、路径、行号、角色和结构 → SessionService 持久化已完成回合 → ConversationResult。

显式 General 和 Repository 模式遵守用户指定模式，不额外调用 Planner。Auto 与 Hybrid 使用当前所选模型进行一次有界规划。Planner 的仓库查询参与实际检索，任务轮廓会传递给最终回答生成器。

## Planner Schema 与限制

计划字段：intent、tasks、requires_repository、requires_external_data、answer_strategy、clarification_required、confidence、reason。任务字段：task_id、task_type（general/repository/external）、question、requires_retrieval、search_queries、dependencies。解析器拒绝未知/缺失字段、依赖顺序错误、标志不一致、非法值、坏 JSON 和超长结果。

限制：最多 6 个任务；每个仓库任务最多 3 个查询、每条 512 字符；读取最多 6 条历史且每条至多 1,200 字符；规划输出预留 256 tokens、超时 20 秒、输出上限 32,000 UTF-8 字节。预算依据所选模型 descriptor 计算；规划失败不会静默回退词法路由。Qwen 计划响应必须正常 stop。

未完成：没有检索后的迭代 replan；一般知识子任务没有分别执行后再聚合；没有 ToolRegistry 或实时外部工具。外部数据任务会被识别并安全停止，不伪装成已查询。

## 证据和回答合同

ProductRetriever 返回候选；Evidence Evaluator 依据路由置信度、候选信号、来源状态和必要证据角色作结构性判断，不证明语义相关或结论蕴含。Context 为本次请求建立 Evidence ID。Answer Contract 校验引用归属、路径/行号、证据角色及回答结构。Hybrid 可保留通用部分，同时限制没有来源支持的仓库部分。

当前没有独立 LLM Evidence Judge 或形式化语义蕴含验证。引用身份正确不等于自然语言结论正确，limited 状态仍然必要。

## 模块边界

| 模块 | 当前职责 | 尚存限制 |
|---|---|---|
| CLI / Console | 参数、依赖装配、命令交互与展示 | CLI 仍处理多个仓库、会话和 Memory 生命周期命令 |
| ConversationOrchestrator | 单轮路由、检索、评估、上下文、生成、合同与完成回合提交 | 仍协调多个应用阶段，并依赖具体 Retriever/回调 |
| LLMPlanner / Dialogue Router | 规划意图与任务、校验自动/显式模式 | 无迭代重规划或工具执行 |
| ProductRetriever / Evidence Evaluator | 检索候选、结构性证据评估 | 无语义裁判 |
| SessionService / MemoryService | 通过 repository Protocol 委托持久化和记忆生命周期 | Protocol 与服务同置；CLI 仍装配和管理其他生命周期 |
| StreamingManager | 生命周期事件、缓冲、取消检查和完成状态 | 内容在验证后一次性发布；不能中止正在传输的网络请求 |
| ContextBudgetManager | 按模型窗口和输出预留进行预算 | 当前测试没有配置精确 tokenizer，使用明确标记的估算 |

服务 Protocol 分散在各模块，不是独立 Ports 包；CLI 保持组合根，但尚未完全退出业务生命周期流程。静态 AST 检查覆盖 77 个 agentforge 模块：0 语法错误、0 静态导入环，不代表运行时依赖完全解耦。

## 测试矩阵

| 能力 | 离线结果 | 真实宿主 |
|---|---|---|
| Planner Schema、预算、失败关闭 | PASS，离线套件通过 | NOT_MEASURED |
| Auto/Hybrid 规划与任务传递 | PASS，测试 Provider 和合成输入 | NOT_MEASURED |
| General/Repository/Hybrid/Auto 编排 | PASS，当前离线路径通过 | NOT_MEASURED |
| 候选筛选、引用身份与 Answer Contract | PASS，离线合同测试通过 | 真实模型语义 NOT_MEASURED |
| Session/Memory 服务委托 | PASS，离线服务/状态测试通过 | PostgreSQL 联合验收 NOT_MEASURED |
| Chroma/Embedding 适配 | 隔离离线测试通过 | 真实 Embedding 与持久化联合验收 NOT_MEASURED |
| Streaming 生命周期和合同前缓冲 | PASS，离线事件测试通过 | 真实 Provider 流式/取消 NOT_MEASURED |
| Context Budget | PASS，模型预算路径有测试 | 真实 tokenizer/usage NOT_MEASURED |

## 完整离线回归

项目虚拟环境执行：

python -X utf8 -B -m coverage run --branch --source=agentforge -m unittest discover -s tests -v

582 项运行，576 通过，0 失败、0 错误、6 跳过，退出码 0，耗时 47.629 秒。六项跳过均是当前 Windows 权限无法创建符号链接、硬链接或 junction；跳过不计入通过。逐项原因见 DIALOGUE_SYSTEM_COVERAGE_TESTS_a837b2ba2f204d8f9ca334442c348bd4.log。

## 覆盖率与交付状态

Python 3.12.7、Coverage.py 7.17.0、branch=true、source=agentforge，覆盖 77 个生产模块。10,274 条语句覆盖 8,968 条，行/语句覆盖率 87.2883%；3,926 个分支覆盖 3,097 个，分支覆盖率 78.8844%。未增加 omit/exclusion 规则；22 条 excluded lines 来自 Coverage 默认模式。

覆盖缺口集中在 cli.py（62.65% 行、42.86% 分支）、conversation_console.py（61.27%、57.87%）、af012_runtime.py（22.73%、0%）、memory_store.py（82.61%、58.65%）及评测入口。详情见 DIALOGUE_SYSTEM_COVERAGE_REPORT_20261106.md。最终整体状态 **PARTIAL**；没有实时模型或数据库验收证据。