# 项目文件清单（初始基线至最终落地验收）

以下路径均相对于 D:\agent\AgentForge。初始阶段 0/1 基线为 42 个文件；后续阶段增量在对应阶段记录。AF-008 新增文件列在本文末尾。正式数据库与证据文件仍在固定数据目录；本轮临时运行时副本已清理。预先存在的 `tests/.tmp/af009c-llama-server` 未触碰。

## 项目配置和文档

| 路径 | 用途 |
|---|---|
| .gitignore | 忽略运行数据、Python 缓存和测试临时目录 |
| pyproject.toml | Python 项目元数据，无第三方依赖 |
| README.md | 使用方法、命令、评分和当前限制 |
| ARCHITECTURE.md | 模块职责、数据流和 Provider 接口 |
| SECURITY.md | 扫描、存储、网络和不可信内容边界 |
| ROADMAP.md | 未实现阶段与后续验收目标 |
| STATUS.md | 实际测试、命令、只读证据和未验证项 |
| FILE_MANIFEST.md | 本文件，逐项新增文件用途 |

## 产品模块

| 路径 | 用途 |
|---|---|
| agentforge/__init__.py | 包与版本 |
| agentforge/__main__.py | python -m 入口 |
| agentforge/cli.py | index/search/ask/symbols/references/agent/status 命令和错误处理 |
| agentforge/agent_loop.py | 有限步数只读工具白名单、参数/路径约束、预算、审计和确定性离线规划器 |
| agentforge/config.py | 固定数据目录、索引版本和资源限制 |
| agentforge/errors.py | 预期错误类型 |
| agentforge/models.py | 结果、证据、上下文与回答结构 |
| agentforge/scanner.py | 受限文件扫描与文本筛选 |
| agentforge/safeio.py | 源文件只读打开及 Windows 句柄验证 |
| agentforge/chunker.py | 行范围切块与哈希 |
| agentforge/store.py | SQLite Schema 迁移、结构化查询、事务与固定路径存储 |
| agentforge/indexer.py | 扫描、切块、Parser 与索引更新的完整性协调 |
| agentforge/parsers/__init__.py | Parser 数据合同、注册入口与跨文件解析导出 |
| agentforge/parsers/base.py | Parser-neutral 符号/引用数据模型和 Parser 协议 |
| agentforge/parsers/python_ast.py | 标准库 AST 的 Python 模块、定义、导入和调用提取 |
| agentforge/parsers/resolver.py | Parser 注册与保守的仓库内导入/调用解析 |
| agentforge/retriever.py | 确定性词法评分与排序 |
| agentforge/context.py | 预算、来源与引用校验 |
| agentforge/providers/__init__.py | Provider 包 |
| agentforge/providers/base.py | 可替换 Provider 协议 |
| agentforge/providers/offline.py | 确定性离线摘要 |
| agentforge/providers/llama_server.py | 回环 HTTP 客户端与模型 Provider 接口 |

## 测试模块

| 路径 | 用途 |
|---|---|
| tests/__init__.py | 测试包 |
| tests/support.py | 项目内临时目录、清单哈希与 junction 构造辅助 |
| tests/test_boundaries.py | 固定路径、忽略规则和禁止执行工具审查 |
| tests/test_chunker.py | 行范围、稳定性、空文本、参数与分块上限 |
| tests/test_cli.py | 帮助、错误码、空状态和数据库错误 |
| tests/test_context.py | 预算、去重、引用与离线模式 |
| tests/test_integration.py | 合成仓库完整 CLI 流程与只读证据 |
| tests/test_providers.py | 无真实网络的 HTTP Mock 测试 |
| tests/test_retriever.py | 标识符、路径、词法、中文、稳定排序和无结果 |
| tests/test_scanner.py | 排除、资源、路径、真实/模拟链接、编码和拒读 |
| tests/test_store.py | 重复、更新、删除、原子切换、回滚和版本 |
| tests/test_symbols.py | Python AST、符号/引用解析、资源上限、Schema v1/v2/v3 兼容与 CLI 查询 |
| tests/test_agent_loop.py | 只读 Agent 白名单、参数/路径拒绝、步数/超时、审计、提示注入惰性和内存索引端到端测试 |

## 合成仓库

| 路径 | 用途 |
|---|---|
| tests/fixtures/synthetic_repo/README.md | 人工合成仓库说明 |
| tests/fixtures/synthetic_repo/src/cache.py | 人工编写的函数检索样例，未执行 |
| tests/fixtures/synthetic_repo/docs/cache.md | 英文/中文关键词和函数文档 |
| tests/fixtures/synthetic_repo/docs/untrusted.md | 不可信指令文本样例，作为惰性数据 |

## AF-008 合成代码仓库

| 路径 | 用途 |
|---|---|
| tests/fixtures/symbol_repo/README.md | 合成符号仓库说明 |
| tests/fixtures/symbol_repo/app.py | 顶层符号与调用样例 |
| tests/fixtures/symbol_repo/pkg/__init__.py | 包入口 |
| tests/fixtures/symbol_repo/pkg/helpers.py | 类、方法、嵌套定义、重名和调用样例 |
| tests/fixtures/symbol_repo/pkg/api.py | 绝对/相对导入、别名、动态导入、遮蔽及未解析引用样例 |
| tests/fixtures/symbol_repo/pkg/sub/__init__.py | 子包入口 |
| tests/fixtures/symbol_repo/pkg/sub/use.py | 包内相对导入和调用样例 |
| tests/fixtures/symbol_repo/broken.py | 语法错误及词法回退样例 |
| tests/fixtures/symbol_repo/empty.py | 空文件样例 |
| tests/fixtures/symbol_repo/comments.py | 注释/文档字符串样例 |

## AF-009 检索评测与向量候选

| 路径 | 用途 |
|---|---|
| agentforge/evaluation.py | 版本化人工标签验证及 Recall@K/MRR 离线评测 |
| agentforge/eval_retrieval.py | 对合成仓库运行离线词法/符号评测，使用内存 SQLite |
| agentforge/embeddings/__init__.py | 独立 Embedding 与向量检索公共接口 |
| agentforge/embeddings/base.py | EmbeddingProvider 与可选角色感知协议、模型元信息和向量有限值/维度/范数校验 |
| agentforge/embeddings/vector_store.py | Schema v3 向量存取、内容版本检查、余弦相似度与候选结果 |
| agentforge/embeddings/retriever.py | 分别以 document/query 角色生成当前索引内容与查询向量，并检索候选 |
| agentforge/embeddings/llama_server.py | loopback-only 标准库客户端，调用已启动的 llama-server `/v1/embeddings`，执行角色化输入和向量响应验证 |
| agentforge/eval_local_embeddings.py | 使用用户已启动的本地服务，在内存 SQLite 上对固定合成集执行 lexical/symbol/vector 对照评测 |
| tests/test_evaluation.py | Ground truth 完整性、指标计算、重复性和可选向量评测钩子 |
| tests/test_embeddings.py | 合成 Provider、角色感知分流、向量验证/排序/幂等、更新删除、错误细节和迁移兼容 |
| tests/test_local_embedding_provider.py | llama-server Embedding Provider 的 Mock 安全、格式、超时、异常和内存评测测试 |
| tests/fixtures/retrieval_eval.json | agentforge-retrieval-v1 人工编写查询及文件/符号标签 |
| tests/fixtures/retrieval_eval_expansion_candidates.json | AF-009D 的 12 条独立待人工审核候选、行证据及排除理由；不覆盖 v1 |
| tests/fixtures/retrieval_eval_review_pool_v2.json | AF-009E 的 40 条待审池：20 正例、20 快照内无证据；保留原 12 个 ID |
| tests/fixtures/retrieval_eval_split_plan_v2.json | group-isolated 的 Calibration/Validation 建议清单及预期分布 |
| tests/fixtures/retrieval_eval_repo/README.md | 合成评测代码仓库说明 |
| tests/fixtures/retrieval_eval_repo/pkg/__init__.py | 合成包入口 |
| tests/fixtures/retrieval_eval_repo/pkg/session.py | 会话函数/类/方法与业务概念实现 |
| tests/fixtures/retrieval_eval_repo/pkg/handlers.py | 跨文件导入/调用点样例 |
| tests/fixtures/retrieval_eval_repo/pkg/token_cache.py | token 相关重名及相似命名样例 |
| tests/fixtures/retrieval_eval_repo/pkg/preferences.py | 外观偏好相似命名干扰样例 |
| tests/fixtures/retrieval_eval_repo/docs/continuity.md | 不与实现函数共享关键词的业务概念文档 |
| tests/fixtures/retrieval_eval_repo/docs/storage.md | 合成存储说明文本 |

## AF-009B 增量

| 路径 | 用途 |
|---|---|
| agentforge/embeddings/base.py | 增加可选 `RoleAwareEmbeddingProvider`，为 query 与 document 预处理差异提供可测试合同，同时保留旧 `embed(text)` 回退 |
| agentforge/embeddings/retriever.py | 索引分块/符号时以 document 角色嵌入，检索时以 query 角色嵌入 |
| agentforge/embeddings/__init__.py | 导出角色感知 Provider 协议 |
| tests/test_embeddings.py | 验证角色方法分流、结果检索和非法角色处理 |

## AF-009C 增量

| 路径 | 用途 |
|---|---|
| agentforge/embeddings/llama_server.py | 真实本地 Embedding API Provider；无 API key 支持，仅限短时 loopback 使用 |
| agentforge/eval_local_embeddings.py | 真实本地 Embedding 的可复现小规模评测入口，数据库使用 SQLite `:memory:` |
| tests/test_local_embedding_provider.py | Mock 验证真实 Provider 接口、query/document 输入、安全约束和错误报告 |
| agentforge/embeddings/base.py | 保留 Provider 抛出的受控 `EmbeddingError` 详情，继续压制任意 Provider 异常文本 |
| STATUS.md / ROADMAP.md / ARCHITECTURE.md / README.md | 记录本地模型身份、真实向量指标、边界与 AF-010 未实施状态 |

## AF-009D 增量

| 路径 | 用途 |
|---|---|
| agentforge/evaluation.py | 保留现有 Recall/MRR 语义，新增审核状态、向量 top score/margin 和探索性阈值混淆指标 |
| agentforge/eval_retrieval.py | 可显式选择固定 v1 或独立扩展候选做离线词法/符号评测 |
| agentforge/eval_local_embeddings.py | 可显式选择固定 v1 或扩展候选做真实本地向量评测 |
| tests/fixtures/retrieval_eval_expansion_candidates.json | 12 条待人工审核候选；8 条有证据、4 条无证据 |
| tests/test_evaluation.py / tests/test_local_embedding_provider.py | 固定 v1 哈希、候选审核状态/行证据、阈值公式和数据集选择回归测试 |
| AF009D_EVALUATION.md | 评测公式、候选审核边界、真实模型分类别结果和阈值实验报告 |

## AF-009E 增量

| 路径 | 用途 |
|---|---|
| tests/fixtures/retrieval_eval_review_pool_v2.json | 40 条待人工审核查询、正例行证据、负例快照范围和泄漏分组 |
| tests/fixtures/retrieval_eval_split_plan_v2.json | 两个各 20 条、10 正/10 负且 group 不交叉的建议划分 |
| AF009E_HUMAN_REVIEW.md | 逐条人工审核表、证据摘录、相关性边界和审核选项 |
| agentforge/evaluation.py | 支持严格 v2 候选验证和建议划分的完整性/泄漏检查 |
| agentforge/eval_retrieval.py / agentforge/eval_local_embeddings.py | 增加显式 `review-pool` 数据选择；默认仍为 baseline |
| tests/test_evaluation.py | 冻结哈希、原 12 条保留、20/20 分布、行证据和 group 隔离回归测试 |

## AF-009E-F 增量

| 路径 | 用途 |
|---|---|
| AF009E_AI_ADJUDICATION.md | 40 条 AI 静态证据裁定、证据摘要、不确定性与边界声明 |
| AF009E_AI_ADJUDICATION_DIFF.md | 原候选标签与 AI 建议的逐条差异及需人工关注项目 |
| tests/fixtures/retrieval_eval_review_pool_v2_ai_adjudicated.json | 独立 AI 裁定数据；保留原查询、分组与 split，明确非人工审核 |
| tests/test_ai_adjudication.py | 冻结输入哈希、完整性、证据路径/行范围、审查状态与组隔离测试 |

## AF-009F-A / AF-010A 增量

| 路径 | 用途 |
|---|---|
| tests/fixtures/retrieval_eval_provisional_v3.json | 固定来源哈希、40 个 ID 和 3 条改写的暂定 AI 数据 manifest |
| agentforge/provisional.py | 从冻结来源安全派生暂定数据及 Calibration/Validation 子集 |
| agentforge/calibration.py | score、margin 和组合规则的探索性校准与固定规则验证 |
| agentforge/fusion.py | 显式文件级 RRF、稳定去重、通道贡献与缺失通道处理 |
| agentforge/eval_provisional.py | 一次性真实 Embedding 校准、Validation、v1 兼容和 RRF 对比入口 |
| tests/test_provisional_fusion.py | 暂定数据、分割边界、阈值分母、RRF、失败传播和清理证据测试 |
| AF009FA_RRF_RESULTS.json | 真实 Qwen Embedding 的完整阈值网格与逐查询对比结果 |
| AF009FA_RRF_EVALUATION.md | 校准、Validation、RRF 指标、服务过程和阶段限制摘要 |
| AF009FA_SERVICE_RUN.json | 本轮 PID、监听、资产哈希、运行时副本和清理证据 |

## 运行数据

| 路径 | 用途 |
|---|---|
| .agentforge/data/index.sqlite3 | 仅含合成仓库的磁盘索引 |
| .agentforge/data/read_only_evidence.json | 索引前后源文件清单、哈希和修改时间 |

单元测试中的 SQLite :memory: 不产生数据库文件。AF-009C 经授权在项目外单独保存一个官方模型资产：`D:\agent\models\qwen3-embedding-0.6b-gguf\Qwen3-Embedding-0.6B-Q8_0.gguf`；其 revision 和哈希见 `STATUS.md`。未在项目外创建 AgentForge 替代项目，也未创建 Git 元数据、虚拟环境、依赖目录或持久化服务配置。

## AF-009G 增量

| 路径 | 用途 |
|---|---|
| tests/fixtures/retrieval_eval_generalization_repo/ | 13 文件 Lumen Gallery 独立合成仓库；未执行或导入其中代码 |
| tests/fixtures/retrieval_eval_generalization_v1.json | 40 条 AI 暂定标签、25 个正例证据范围及查询类别 |
| tests/fixtures/retrieval_eval_generalization_split_v1.json | 10 个泄漏组的固定 Calibration/Validation 分配 |
| agentforge/generalization.py | 校验证据、路径、数据版本和 split；在内存索引上协调泛化评测 |
| agentforge/eval_generalization.py | AF-009G 可复现 CLI，结果写入独立结果文件 |
| agentforge/evaluation.py | 增加 AF-009G 数据版本和显式可选检索计时；默认基线仍确定性 |
| tests/test_generalization.py | 新 fixture 完整性、标签、行范围、分组、分母与 RRF 测试 |
| AF009G_GENERALIZATION_RESULTS.json | 分 split 排名、类别、耗时、负例返回情况和语料哈希清单 |
| AF009G_EVALUATION.md | AF-009G 运行过程、可复现指标、局限与产品化结论 |

## AF-009G-R 增量

| 路径 | 用途 |
|---|---|
| AF009GR_DIAGNOSTIC_RESULTS.json | 标准库 loopback 分层结果、Windows 错误码、资产/测试/清理和受保护文件证据 |
| AF009GR_DIAGNOSTIC.md | TCP 阻塞诊断、模型停止条件、未完成的 Vector 验收及后续进入条件 |

## AF-009G-R2 增量

| 路径 | 用途 |
|---|---|
| AF009GR2_DECISION.md | WSA 10013 证据边界、Windows 策略/日志调查、进程内 Embedding 静态可行性、路线比较与最小恢复条件 |

## AF-009G-R3 增量

| 路径 | 用途 |
|---|---|
| AF009GR3_HOST_RUNNER.py | 单次宿主 loopback、自有临时运行时、服务输出排空、冻结评测和清理编排；不属于产品 CLI |
| AF009GR3_RESULTS.json | R3 回环、资产、唯一服务启动、HTTP 503、清理和受保护文件前后快照的机器可读证据 |
| AF009GR3_EVALUATION.md | R3 实际结果、未测量指标、测试和后续模型 readiness 进入条件 |

## AF-009G-R4 增量

| 路径 | 用途 |
|---|---|
| AF009GR3_HOST_RUNNER.py | 修订为 R4 状态机：TCP/模型 readiness 分离、健康轮询、分类失败、资源采样和统一清理 |
| agentforge/embeddings/llama_server.py | 增加非敏感 Embedding 请求 attempts/sent/successes/failures 计数；请求仍不重试 |
| agentforge/generalization.py | 将真实 Provider 请求计数写入独立评测结果 |
| tests/test_af009gr3_host_runner.py | 健康 503→200、超时、提前退出、非法响应、硬门、输出和清理 Mock 回归 |
| tests/test_local_embedding_provider.py | Embedding 503 单次失败及成功请求计数回归 |
| AF009GR4_RESULTS.json | R4 状态迁移、实际健康失败、0 Embedding、资源、清理和完整性机器证据 |
| AF009GR4_EVALUATION.md | R4 代码修复、测试、唯一真实启动、新阻塞及阶段结论 |

## AF-009G-R5 增量

| 路径 | 用途 |
|---|---|
| AF009GR3_HOST_RUNNER.py | R5 Health 探针：读取 `urllib` HTTPError 状态/正文、严格 503 loading 与 200 ok 合同、脱敏诊断和模型就绪硬门 |
| tests/test_af009gr3_host_runner.py | HTTPError 503、503→200、超时、提前退出、未知响应、脱敏、输出排空和清理回归测试 |
| AF009GR5_RESULTS.json | 唯一真实启动、Health 观测、93 个 Embedding 请求、Vector/RRF 指标、资源、清理及完整性机器证据 |
| AF009GR5_EVALUATION.md | R5 修复、真实健康合同、独立 Validation、阈值迁移、测试和阶段结论 |
| STATUS.md / ROADMAP.md | 记录 AF-009G 实验完成、负面产品化结论及仍关闭的生产边界 |

## AF-010-PRE 增量

| 路径 | 用途 |
|---|---|
| tests/fixtures/retrieval_eval_preproduct_repo/ | 20 文件 Northstar 冷链独立合成仓库；只作为未执行的解析/检索数据 |
| tests/fixtures/retrieval_eval_preproduct_v1.json | 80 条自动构建 AI 暂定查询、行证据、负例范围和标签来源 |
| tests/fixtures/retrieval_eval_preproduct_split_v1.json | 20 个 leakage group 的固定 40/40 Calibration/Validation 计划及工程目标 |
| agentforge/preproduct.py | 数据/证据验证、通道互补性、拒答验收及产品进入判定 |
| agentforge/generalization.py | 可选 Calibration-only 规则冻结及 Validation 一次性评估；原 AF-009G 默认行为保持不变 |
| agentforge/evaluation.py | 注册独立 `agentforge-retrieval-preproduct-v1` AI 暂定数据版本 |
| tests/test_preproduct.py | 80 条完整性、证据、group 隔离、Mock 向量流程、历史哈希和默认边界测试 |
| AF010PRE_HOST_RUNNER.py | 一次性 loopback/Health/真实 Embedding 验收编排；不属于产品 CLI |
| AF010PRE_RESULTS.json | 真实 180 请求、四通道指标、阈值、互补性、资源、清理及完整性机器结果 |
| AF010PRE_EVALUATION.md | P1–P4、独立数据、真实指标、拒答结果和产品进入结论 |

## AF-010-S1 增量

| 路径 | 用途 |
|---|---|
| agentforge/package_relations.py | 扫描器约束下的内存 package import/export 关系图、依赖感知增量更新与显式研究 Retriever |
| agentforge/package_evaluation.py | 数据/证据/split 验证及 Lexical、Symbol、PackageRelation、离线 RRF 对比 |
| agentforge/evaluation.py | 仅登记隔离的 `agentforge-package-relations-v1` 评测数据版本 |
| tests/fixtures/package_relation_repo/ | 17 文件独立 Python package 合成仓库，含 reexport、alias、cycle、star 和 dynamic 场景 |
| tests/fixtures/package_relation_eval.json | 40 条 synthetic fixture-design / AI 暂定查询及路径行证据 |
| tests/fixtures/package_relation_split.json | 固定 20/20、leakage-group 隔离的 Calibration/Validation 清单 |
| tests/test_package_relations.py | 关系解析、稳定 ID、安全边界、数据隔离和可复现评测回归测试 |
| AF010S1_EVALUATE.py | 使用内存 SQLite 生成独立 AF-010-S1 机器结果的研究入口 |
| AF010S1_RESULTS.json | 逐查询排名、通道贡献、关系状态、指标和受保护文件哈希 |
| AF010S1_EVALUATION.md | 实现范围、指标、局限、测试与产品进入决定 |

## AF-010-S2 多仓库验证产物

| 路径 | 用途 |
|---|---|
| agentforge/query_routing.py | 可解释、离线的 Package API/依赖/符号/业务/文档查询路由原型；动态结构查询低置信回退，不改变默认 Retriever |
| agentforge/package_s2.py | 三仓库数据与 split 验证、离线通道对比、路由分析和规模基准 |
| tests/test_package_s2.py | S2 fixture、关系图、增量生命周期、故障恢复、路由、离线评测与规模回归测试 |
| tests/fixtures/package_s2_repos/ | harbor_registry、clinic_gateway、observatory_sdk 三个互相独立的合成仓库 |
| tests/fixtures/package_s2_eval_v1.json | 120 条合成设计标签查询，AI/合成来源，非人工审核 |
| tests/fixtures/package_s2_split_v1.json | 固定 60/60 Calibration/Validation split 和 leakage group 分配 |
| tests/fixtures/package_s2_design_v1.json | 合成仓库设计规格及关系/标签验证预期 |
| AF010S2_BUILD_FIXTURES.py | 确定性生成合成仓库和评测 fixture 的脚本 |
| AF010S2_OFFLINE_RUNNER.py | 初始离线管线预检；其规模更新示例未改变内容，不作为修改更新成本依据 |
| AF010S2_OFFLINE_FINAL_RUNNER.py | 冻结离线检索、路由与 RRF（无 Vector）评测 |
| AF010S2_SCALE_RUNNER.py | 100/1000/5000 模块及关系快照刷新规模测量 |
| AF010S2_HOST_RUNNER.py | 受限的一次性宿主 Vector runner；本轮未执行，因 loopback 前置检查阻塞 |
| AF010S2_OFFLINE_RESULTS.json | 初始管线预检结果，Vector 未测量 |
| AF010S2_OFFLINE_FINAL_RESULTS.json | 最终离线通道、路由与 Validation 排名结果 |
| AF010S2_SCALE_RESULTS.json | 修正后的规模和关系快照更新测量 |
| AF010S2_BLOCKED_RESULTS.json | 本轮 loopback 环境阻塞、未启动服务及未测量 Vector 的结果 |
| AF010S2_EVALUATION.md | S2 数据、分母、离线指标、规模限制、安全阻塞与阶段结论 |

## AF-010-S3 增量关系图验证产物

| 路径 | 用途 |
|---|---|
| AF010S3_SCALE_RUNNER.py | 隔离的 100/1,000 完整扫描生命周期与 5,000 内存图层合成规模基准；支持只读验证已有结果 |
| AF010S3_SCALE_RESULTS.json | 单次规模基准指标、覆盖范围、临时清理及受保护输入 SHA-256 前后比较 |
| AF010S3_EVALUATION.md | 增量图设计、全量回退、失败恢复、规模指标、测试证据和阶段决策 |

## AF-010-S4 扫描成本与完整流水线验证产物

| 路径 | 用途 |
|---|---|
| agentforge/package_relations.py | 单次读取/哈希快照与 AST 缓存复用；失败原子保留和无效缓存全量回退 |
| tests/test_package_s2.py | 扫描缓存、一致性、元数据边界、读取中断恢复与查询路由回归 |
| AF010S4_SCALE_RUNNER.py | 独立 100/1,000/5,000 模块完整扫描、解析、关系构建、变更更新与全量 oracle 基准 |
| AF010S4_SCALE_RESULTS.json | 实测时间、文件读/哈希/解析数、关系状态、内存、完整性及临时清理结果 |
| AF010S4_EVALUATION.md | S3/S4 可比基线、5,000 模块完整流水线、缓存假设、成本限制和阶段结论 |

## AF-010-S5 联合评测产物

| 路径 | 用途 |
|---|---|
| AF010S5_OFFLINE_RUNNER.py | 冻结 AF-010-S2 数据上的离线通道评测、逐查询证据采集、保护文件核验；不启动模型服务 |
| AF010S5_RESULTS.json | 独立 S5 机器结果；离线指标为 measured，Vector/四通道 RRF 因环境门禁为 not_measured |
| AF010S5_EVALUATION.md | 本轮环境门禁、离线结果、路由差异、完整性与阶段结论 |

## AF-010-S5-FINAL 最终验收产物

| 路径 | 用途 |
|---|---|
| AF010S5_FINAL_RESULTS.json | S5-FINAL 最终门禁、测试、保护文件核验及复用离线结果状态；真实 Vector 明确标记 blocked_by_environment |
| AF010S5_FINAL_EVALUATION.md | 最终 Runner 审查、环境阻塞、测试结果、数据完整性和阶段决定 |

## 最终落地验收增量

| 路径 | 用途 |
|---|---|
| agentforge/agent_loop.py | 受限只读 Agent 工具循环、确定性离线规划器与审计数据模型 |
| tests/test_agent_loop.py | 白名单、参数/路径/预算、只读性、提示注入和 CLI 回归测试 |
| FINAL_ACCEPTANCE_RESULTS.json | 最终验收矩阵、测试、完整性、环境门禁和唯一恢复入口的机器结果 |
| FINAL_ACCEPTANCE_REPORT.md | 可交付能力、真实运行边界、产品准入与唯一下一步 |

## AF-010-S2 真实宿主验收审计增量

| 路径 | 用途 |
|---|---|
| AF010S2_RESULTS.json | 已存在的正常 Windows 宿主真实运行结果；165 个成功 Embedding 请求、Vector/四通道 RRF 排名、资源、清理与完整性证据（本审计未覆盖） |
| AF010S2_HOST_ACCEPTANCE_AUDIT_20261026.md | 独立审计报告：readiness、请求计数、真实指标、退出码 1、完整性、阶段与第一版冻结结论 |
| AF010S2_HOST_ACCEPTANCE_AUDIT_20261026.json | 审计结论、指标、阈值未测量状态、完整性与测试的机器可读记录 |
| STATUS.md / ROADMAP.md | 将真实执行与产品质量分开记录；解除宿主执行阻塞但继续延期 AF-010 产品化 |

## AF-011 产品可靠性与冻结产物

| 路径 | 用途 |
|---|---|
| agentforge/product_retrieval.py | 意图驱动的产品候选检索、稳定去重、provenance 保留、索引仓库与文件新鲜度校验 |
| agentforge/answer_contract.py | 独立回答合同；验证路径、行号、原文及来源状态，区分确认、推断和未知 |
| agentforge/product_acceptance.py | AF-011 fixture 校验、内存索引和可复现离线验收指标 |
| agentforge/agent_loop.py | 受限 Agent 复用产品路由并拒绝不完整扫描 |
| agentforge/cli.py | `ask` 输出结构化回答合同与路由；直接 `search` 行为保持不变 |
| agentforge/store.py | 提供索引文件内容哈希读取，用于证据新鲜度核对 |
| tests/test_af011_product.py | 路由、去重、过期/跨仓库证据、回答合同及安全边界回归 |
| tests/test_af011_acceptance.py | v3 数据、split、证据行范围和验收指标回归 |
| tests/fixtures/af011_product_tasks_v1.json | AF-011 初始任务草案；保留用于审计，不作为最终验收输入 |
| tests/fixtures/af011_product_tasks_v2.json | 补充证据元数据后的中间数据版本；保留用于审计 |
| tests/fixtures/af011_product_tasks_v3.json | 20 条 AgentForge 源码任务，AI 证据审查、非人工标签、10/10 分组隔离 |
| AF011_OFFLINE_ACCEPTANCE_RUNNER.py | 建立 47 文件隔离快照并使用内存 SQLite 的一次性离线验收入口 |
| AF011_OFFLINE_ACCEPTANCE_RESULTS.json | 初始离线验收记录；不覆盖，非最终基线 |
| AF011_OFFLINE_ACCEPTANCE_RESULTS_V2.json | 第二次修订验收记录；不覆盖，非最终基线 |
| AF011_OFFLINE_ACCEPTANCE_RESULTS_V3.json | 第三次修订验收记录；不覆盖，非最终基线 |
| AF011_OFFLINE_ACCEPTANCE_RESULTS_V4.json | 最终离线逐查询结果、指标、快照与数据哈希；历史 V1–V3 结果保留 |
| AF011_PRODUCT_EXECUTION_PLAN.md | P0–P6 执行范围、保护边界和停止条件 |
| AF011_ACCEPTANCE_MATRIX.md | PASS/FAIL/BLOCKED/NOT_MEASURED 验收矩阵 |
| AF011_ACCEPTANCE_REPORT.md | 第一版能力、指标、限制和冻结决定 |
| AF011_ACCEPTANCE_RESULTS.json | 最终测试、完整性、聊天门禁和阶段决定的机器可读摘要 |

## AF-011 V1.0 真实聊天验收增量

| 路径 | 用途 |
|---|---|
| agentforge/providers/llama_server.py | 有界非流式聊天、显式 `enable_thinking=false`、ID-only 模型证据、独立连接/读取/总期限、输出上限、传输/应用失败分层、脱敏诊断与请求计数 |
| agentforge/product_retrieval.py | 显式自然语言符号引用的 Symbol-first 路由；不存在精确身份时保持低置信门禁 |
| agentforge/cli.py | 暴露总期限/输出上限，执行显式证据门禁并输出 Provider 请求审计；默认 Offline 与直接 search 行为不变 |
| agentforge/context.py | 将模型 `[E#]` 映射为应用拥有的规范路径/行号；拒绝未知 ID 与精确位置伪造，避免普通技术文本误报并生成脱敏引用诊断 |
| agentforge/errors.py | `CitationValidationError` 结构化承载引用规则、位置和脱敏诊断 |
| AF011_CHAT_HOST_ACCEPTANCE.py | 一键 Windows 宿主验收；核验资产、管理唯一临时服务、严格 Health、真实 CLI ask、引用/回答合同阶段分类、逐案例保留、清理与完整性核验 |
| tests/test_af011_chat_acceptance.py | 受限环境门禁、追加式结果、真实 CLI 内存索引路径、传输/引用/回答合同阶段、逐案例保留、证据门禁、单次调用及清理测试 |
| tests/test_providers.py | 总期限、输出上限、非流式请求、响应结构分类、ID-only 引用、空 final/reasoning 隔离、统计分层与不重试回归 |
| tests/test_cli.py | 新参数、Provider 传递、显式缺失符号门禁及不回退测试 |
| AF011_CHAT_ACCEPTANCE_RESULTS.json | 追加式机器结果；保留全部历史 `BLOCKED/FAIL` 与修复审计，并保存最终真实 `PASS` 的回答、请求内引用映射、合同、语义、清理和完整性证据 |
| AF011_CHAT_ACCEPTANCE_REPORT.md | 最终真实 Qwen 验收、`[E1] → src/cache.py:4-6` 证据核对、无证据拒答、主动清理退出语义与离线回归摘要 |

## V1 测试专项

| 路径 | 用途 |
|---|---|
| tests/test_quality_boundaries.py | 独立仓库/内存索引隔离、CRLF/Unicode 边界、Agent 步数及禁止工具审计回归 |
| TEST_STRATEGY.md | 测试分层、命令、门槛和模拟/真实验证边界 |
| TEST_COVERAGE_REPORT.md | 历史未测量状态及后续真实行/分支测量、缺口和复核命令 |
| TEST_COVERAGE_RESULTS.json | coverage.py 7.17.0 的生产源码逐文件行、分支和缺失位置原始报告 |
| TEST_COVERAGE_RESULTS_PRE_HARDLINK.json | 硬链接修复前覆盖率机器结果的原样保留副本 |
| tests/test_quality_coverage.py | Provider 响应与请求边界、回答合同拒绝、Agent 参数、扫描竞态和向量来源回归 |
| TEST_ACCEPTANCE_RESULTS.json | 基线、定向、完整回归和保护数据机器记录 |
| TEST_CLEANUP_REPORT.md | 本轮测试临时文件和既有缓存、历史目录的保留依据 |

## V1 最终覆盖率补测产物
- `tests/test_final_coverage.py`：27 项离线安全、恢复、评测与输入边界回归。
- `coverage/TEST_COVERAGE_RESULTS_BEFORE_FINAL.json`：前一批覆盖率报告原样快照。
- `coverage/TEST_COVERAGE_FINAL_BASELINE.json`、`coverage/TEST_COVERAGE_FINAL_ROUND*.json`：本轮基线及逐组测量结果。
- `coverage/TEST_COVERAGE_FINAL.data`：最终覆盖率原始数据；`coverage/TEST_COVERAGE_FINAL_RERUN_LOG.txt`：完整最终覆盖率回归输出；`coverage/TEST_COVERAGE_FINAL_RUN_LOG.txt`：早期失败运行原始输出。
- `TEST_COVERAGE_RESULTS.json`、`TEST_ACCEPTANCE_RESULTS.json`、`TEST_COVERAGE_REPORT.md`、`TEST_CLEANUP_REPORT.md`：最终机器数据、统计、解释与清理。

## 最终宿主确认与完整性清单
- `coverage/TEST_FINAL_INTEGRITY_20261027.json`：135 个冻结保护路径的基线与当前大小、SHA-256，以及模型、运行时关键文件和 AF-011 历史记录核验。
- `TEST_ACCEPTANCE_RESULTS.json`：新增用户提供的零跳过宿主测试确认与最终门禁；保留此前 `PARTIAL` 的实测历史。
- `TEST_COVERAGE_REPORT.md`、`TEST_CLEANUP_REPORT.md`、`STATUS.md`、`ROADMAP.md`、`README.md`：本次确认来源、历史覆盖率批次和资源检查。

## Git 首发与数据库审查（2026-10-31）
- `GIT_RELEASE_AUDIT.md` / `GIT_RELEASE_AUDIT.json`：离线测试、公开发布风险、远端状态与阻塞。
- `DATABASE_ARCHITECTURE_REVIEW.md` / `DATABASE_ARCHITECTURE_REVIEW.json`：现有 SQLite 用途、Chroma 官方资料、A/B/C 决策和可选迁移设计。
- `coverage/GIT_RELEASE_OFFLINE_TEST_LOG.txt`：本轮离线测试原始输出，仅本地保留，含本机路径，不用于发布。
