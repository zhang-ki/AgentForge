# 架构

## 数据流

用户显式 --repo → scanner 受限枚举 → safeio 只读句柄 → indexer；行切块继续进入词法索引，同时已允许的 Python 文本进入 AST Parser → 保守仓库内引用解析 → store SQLite 事务。

query → retriever 词法评分与稳定排序；可选地由显式注入的 EmbeddingProvider 产生查询向量，经 VectorStore 取得独立向量候选。AF-009 不做自动融合，现有 CLI/回答仍使用原词法/上下文链路。评测脚本将词法和符号查询分别对照人工 ground truth。词法结果 → context 去重、预算、来源标识 → 显式 Provider 选择：默认 OfflineProvider，或用户配置的 llama-server → 当前请求引用成员校验。显式 `agent` 命令则将同一安全扫描、索引和检索能力包装为确定性离线规划器驱动的有界白名单调用；它不改变默认 `ask` 数据流。

源仓库没有写接口。应用的持久化操作集中在 store，目标固定为项目自身的数据目录。合成测试有独立的写入/清理代码，产品包不包含测试文件写入功能。

## 模块职责

| 模块 | 职责 |
|---|---|
| cli / __main__ | 参数解析、退出码、命令输出；ask 默认离线，也可显式选择本机 llama-server |
| agent_loop | 绑定单一仓库的 `index/search/read/list-symbols` 白名单、强类型参数、步数/时间预算和逐步审计；当前规划器为确定性离线替身 |
| config / errors / models | 固定配置、预期错误、检索/证据/回答数据结构 |
| scanner | 排除规则、资源上限、路径归属、链接拒绝、跳过原因 |
| safeio | 只读打开；Windows 在读取前验证最终句柄路径与 reparse 属性 |
| chunker | 稳定行窗口，准确行号和 SHA-256 |
| indexer | 收集允许的文档，检查完整性，再提交索引 |
| parsers/base | Parser-neutral 符号/引用数据合同及可插拔 Parser 接口 |
| parsers/python_ast | 标准库 AST 的模块、定义、导入、调用和行范围提取；不导入或执行被索引模块 |
| parsers/resolver | 仅凭本仓库已解析文件保守解析本地导入和可静态确定调用；无法唯一确定时保留 unresolved/ambiguous |
| store | 固定位置 SQLite、Schema 迁移、参数化查询、事务、变化与删除更新 |
| retriever | 关键词与路径评分、稳定排序、内容去重 |
| embeddings/base | 兼容旧 `embed(text)` 的 EmbeddingProvider、可选 query/document 角色合同、模型元信息和数值向量校验/归一化 |
| embeddings/vector_store | Schema v3 float32 向量的参数化 SQLite 存取、版本校验、余弦候选排序与来源信息 |
| embeddings/retriever | 显式向量化当前已索引分块/符号，并生成独立向量候选；不自动融合或改写词法结果 |
| evaluation / eval_retrieval | 校验人工合成标签，离线计算词法、精确符号和可选向量检索指标 |
| context | 来源编号、完整片段预算、精确引用成员校验 |
| providers/base | 可替换 Provider 协议 |
| providers/offline | 无网络、无模型的证据摘要 |
| providers/llama_server | 显式回环 HTTP 聊天接口及返回引用校验 |

## 只读 Agent 边界

`AgentLoop` 最多执行 8 个结构化调用。`ReadOnlyToolbox` 在构造时固定规范化仓库根，后续 `index` 不能切换根目录；`read` 每次重新使用 scanner 枚举允许候选，再由 safeio 读取，并限制到 200 行。参数对象拒绝缺失、多余和错误类型字段，路径拒绝绝对路径、盘符、空组件和 `..`。所有调用带 `origin` 并写入内存审计记录。工具超时是协作式墙钟检查：现有扫描器和索引器本身有资源上限，调用超时后循环失败且不重试；它不宣称能够安全抢占任意第三方阻塞代码。

当前 `OfflineAgentPlanner` 只执行固定的索引、检索、可选符号查询和首条证据读取，最终答案仍由 `OfflineProvider` 生成。检索内容不能生成新工具名或参数。产品包没有 shell、任意网络、补丁、仓库写入或子进程工具；真实模型工具选择尚未接入或验收。

## 索引格式

当前索引 Schema 版本为 3。metadata 保存版本、唯一仓库规范化根路径、最近索引 UTC 时间、切块配置和 Parser 版本。files 保存相对路径、内容哈希、大小、修改时间和切块签名。chunks 保存文件关联、原始起止行、检索文本和内容哈希；外键级联删除，文件路径和分块范围有唯一约束。symbols 保存稳定符号 ID、来源 ID、文件、种类、名称/限定名称、父符号、范围、签名、文档字符串及结构摘要；symbol_references 保存导入/调用的来源位置、引用类型、目标、解析状态和候选 ID；parse_status 记录每个 Python 文件的解析状态。`vector_embeddings` 保存 model_id、来源类型、文件/source ID、chunk 或 symbol 关联、内容版本哈希、维度、float32 BLOB 与更新时间；来源键按模型唯一。

v1 只读打开时仍可使用词法查询、不迁移。显式可写 `index` 才迁移 v1→v2→v3 并在完整索引事务中填充结构数据。v2 只读时继续提供词法和符号查询，并明确向量索引不可用；显式可写打开才进行 v2→v3 增量迁移。迁移保留既有词法文件/分块及可用符号记录，不会自动生成 Embedding。AF-009 的迁移测试使用测试专用 SQLite，不打开或修改正式数据库。

文件哈希基于解码后的 UTF-8 文本，UTF-8 BOM 不进入检索内容。块哈希基于换行规范化后的片段。原始 CRLF/LF 差异不影响引用行数。索引保存检索所需内容，不保存跳过文件内容或模型回答。

源文件内容或切块配置变化会使旧分块失效；完整扫描中消失的文件被删除。更新使用一个 SQLite 事务，失败回滚。目录不可读或整体资源上限触发时不提交部分结果；单个文件被排除/不可读时移除该文件的旧索引，防止返回过期片段。

结构化解析只接收 scanner 已允许且 safeio 已读取的文件文本。Python 文件使用 `.py`/`.pyi` AST Parser；解析失败不影响同一文件的词法切块。单文件 AST 节点、符号、引用及仓库总符号/引用均有上限；达到结构化上限时记录失败状态并保留词法索引。其他语言暂未提供 Parser。

Python 符号 ID 由仓库相对路径、符号种类、限定名称、签名和同作用域重名序号确定，不使用整个文件内容哈希；因此无关文件内容修改不会改变定义 ID。重复定义和不同模块由路径、限定名及序号区分。调用/导入解析只匹配仓库内唯一候选；多个候选标记 ambiguous，没有候选、动态导入或受局部绑定遮蔽的引用标记 unresolved。它不执行代码、不加载目标模块，也不证明运行时实际调用。

Embedding 是独立于聊天 Provider 的可插拔接口。兼容既有 `EmbeddingProvider.embed(text)`；可选 `RoleAwareEmbeddingProvider` 暴露 `embed_query(text)` 和 `embed_document(text)`，由向量 Retriever 在查询和索引文本处分别调用。没有角色方法的旧 Provider 继续回退到 `embed(text)`。`LlamaServerEmbeddingProvider` 使用标准库直连已启动的 `/v1/embeddings`，只接受显式 loopback IP，禁用环境代理并拒绝重定向；它不启动、发现或管理模型服务。Qwen3 Embedding query 按官方格式加 `Instruct: ...\nQuery: ...`，document 使用原始文本，服务以 last pooling/归一化输出；公共层继续校验有限数值/维度/非零并归一化。测试实证固定的 Qwen3-Embedding-0.6B-GGUF Q8_0 本地版本生成 1024 维向量。不将 Qwen 聊天 GGUF 假定为 Embedding 模型。归一化后以小端 IEEE float32 保存，余弦定义为点积除以向量范数乘积；返回值只代表候选相似度，不证明事实相关或正确。向量候选器当前无置信阈值，因此无证据 query 也可能返回候选；空结果不能视为内建的证据充分性判断。零查询向量产生空候选，余弦任一输入为零则定义为 0。维度上限 4096、向量记录最多 10,000、数据最多 128 MiB、Top-K 最多 100。

首版在 SQLite 中顺序读取向量并用标准库计算相似度，单次搜索复杂度为 O(记录数×向量维度)，没有 ANN/专用向量索引。资源上限把首版控制在小型本地索引规模；当前没有大规模性能基准，超过该范围时应先对真实工作负载测量，再比较专用向量索引/数据库及其依赖、许可和迁移成本。

评测层将无证据策略保持为实验：每条真实向量查询记录最高余弦分数与第一、第二名差值，再在预先固定的 0.00–1.00 网格上报告覆盖率、误接受率、误拒绝率和接受结果精确率。无证据查询不进入 Recall/MRR。该分析不向 `VectorCandidateRetriever` 注入阈值，也不改变产品默认候选返回；扩展候选标签被强制保持 `pending_human_review`，越界行证据或伪装成人工审核会被验证器拒绝。

AF-009E 的 v2 review pool 进一步要求每条候选有稳定 ID、`leakage_group`、纳入/排除理由和待审状态。正例必须有可索引的路径/行范围；负例必须声明完整快照范围并保持空正例证据。建议 split 以 group 为原子分配，覆盖全部候选且不重复：Calibration 只用于选择候选阈值，Validation 在规则冻结前封存。`validate_split_plan` 校验 group 交集、ID 完整性、正负/近域负例分布和清单自报分布，但不会把待审标签提升为正式 ground truth。

AF-009F-A 的 provisional loader 通过固定 SHA-256 解析原候选池、AI 裁定和 split plan，不复制或覆盖来源。它只接受 `accept_current`、`no_evidence` 与具有新查询的 `rewrite_query`，保留原查询、split 和 leakage group，并输出 `provisional_ai`/`human_reviewed=false` 的内存数据集。Calibration 纯函数在固定 0.05 网格比较 top score、top-2 margin 与 AND 组合；选择规则只读取 Calibration，Validation 函数只评估已冻结规则。规则不会写回 Retriever。

`agentforge.fusion.RrfRetriever` 是显式可选的文件级 Reciprocal Rank Fusion。每个通道先按仓库相对文件路径去重，再累加 `1/(k+rank)`；结果保存每个通道的原始排名、原始分数及 RRF 贡献。缺失通道为空列表，已配置通道错误直接传播，不自动重试或伪装成功。默认 lexical、symbol 和 vector API 均未改变。

每条向量绑定 file/source ID、模型 ID、chunk/symbol 身份和内容哈希；仅与当前活动分块内容哈希或符号结构哈希一致的向量可检索。完整索引更新/删除会使旧分块向量级联清理；失效符号向量会在结构重建后清理。向量化操作由调用方显式触发且与 SQLite 更新同事务；Embedding 不可用不影响词法/符号 API。当前未实现 CLI 向量命令、后台/自动嵌入或 lexical-symbol-vector 融合。

离线评测数据 `tests/fixtures/retrieval_eval.json` 是人工写定标签的 `agentforge-retrieval-v1`，代码仓库 `tests/fixtures/retrieval_eval_repo` 为人工合成内容。`python -X utf8 -B -m agentforge.eval_retrieval` 在内存 SQLite 建立 lexical/symbol baseline；`python -X utf8 -B -m agentforge.eval_local_embeddings --base-url http://127.0.0.1:<port> --model-id <fixed-model-identity> --api-model <server-alias> --dimension 1024` 仅调用用户已启动的本地服务，不启动服务，使用内存 SQLite 建向量索引并比较三种检索器。真实 Q8_0 基线（9 个 query，8 个有标签）：lexical R@1/5/10=0.75/0.9375/0.9375、MRR=0.9375；structured symbols=0.928571/1/1、MRR=1；vector=0.6875/1/1、MRR=0.875。自然语言语义查询不适用于精确符号筛选。v1 只有 9 条合成 query，不能外推为一般仓库质量结论；vector no-evidence query 返回候选，尚无 abstention 阈值。

AF-009C 在 `STATUS.md` 记录模型来源与本地实测：官方 Qwen Q8_0 GGUF 文件 SHA 与本地文件匹配；现有 `llama-b11424` 已实际加载并提供 pooled `/v1/embeddings`。本次服务绑定 loopback、禁用 Web UI、限制 CORS，但没有 API key；CORS 不是认证。执行环境拒绝 TCP socket 表枚举，loopback 端点以实际服务参数/监听日志及本地嵌入请求确认。测试结果仅证明此固定模型/运行时/输入配置可工作，不证明长期服务安全或普遍检索质量。真实模型错误、输入前处理、精度/延迟资源和检索类别结果按阶段日志保留。

## Provider 边界

Provider 接收 BuiltContext，返回 Answer。离线 Provider 使用来源对象生成引用；本机模型 Provider 将检索上下文发送到用户指定的回环服务，并检查返回文本中的所有来源 ID 和行范围引用必须属于本次提供的证据。连接、HTTP、响应解析或引用校验失败会作为错误返回，CLI 不会静默切换到离线模式。无检索证据时不发送模型请求，而是明确报告离线无证据结果。

客户端基于 http.client，直接连接经过 ipaddress 验证的回环字面地址；连接超时和读取超时分开设置。http.client 不读取环境代理，也不自动跟随重定向。响应体有大小上限，错误不回显服务端响应内容。

CLI 要求用户显式选择 `--provider llama-server`，并提供 `--base-url` 与 `--model`。构造客户端不会发请求；只有 `ask` 在存在检索证据时才会调用客户端。没有默认端点或模型标识。

引用校验能够证明引用属于给定检索证据，不能证明任意模型的自然语言结论必然被证据蕴含。此前完成本机健康/元数据检查，但真实生成未取得可验证回答，端到端 RAG 验收仍未通过。

## 引用标记与行号

检索上下文把来源写成 [E1]，随后提供 [[相对路径:起始行-结束行]] 及证据文本。校验针对本次 BuiltContext 建立来源 ID 与路径范围映射；不从全局状态或旧请求恢复来源。输出中的每个 [E…] 标记必须属于当前证据集合。每个双中括号路径引用都必须完整解析，路径和起止行必须与当前证据完全相符；孤立的行号范围、损坏标记和混合输出中的任何无效引用均会拒绝。若输出同时包含来源 ID 和路径引用，两组引用必须指向同一组证据。

只含普通自然语言、没有来源标记的回答允许通过，返回的 citation 列表为空。验证结果只表示引用属于当前检索证据，不能证明回答中的每个断言都由证据蕴含。

文件行边界只由 CRLF、LF 或 CR 定义。换页符、垂直制表符、NEL、U+2028 和 U+2029 保留在原行内容内。末尾行结束符不会虚构额外行；单独一个行结束符表示一条空行；连续行结束符保留中间空行。块哈希仍对块内换行做 LF 规范化。

切块签名以 line-breaks-v2 开头。该行语义升级最初不改变当时的 Schema v1；AF-008 后 Schema 为 v2，AF-009 后当前代码 Schema 为 v3。已存在的 v1 数据库仍可只读词法查询，下一次用户显式执行 index 时会按 v1→v2→v3 迁移并按当前签名重新切块；v2 只读保持词法/符号查询，向量表需显式可写迁移。本轮没有重新索引或写入正式持久化数据库。

## 测试隔离

集成测试通过可复用的 SQLite :memory: 索引运行 CLI，并把 CLI 的数据库存在检查指向测试专用标记文件。测试证据写入 LocalTestCase 创建的 tests/.tmp 独立目录；所有测试临时源码及可构造链接也只位于该目录。清理前核对临时路径位于 AgentForge/tests/.tmp 中。集成测试不会调用默认持久化数据库，也不会写入项目的 read_only_evidence.json。

## AF-009G 独立泛化评测

`tests/fixtures/retrieval_eval_generalization_repo/` 是与既有 fixture 不同结构的第二个纯合成仓库。`retrieval_eval_generalization_v1.json` 保存 40 条 AI 暂定查询和路径/行证据；`retrieval_eval_generalization_split_v1.json` 按完整 leakage group 固定 Calibration/Validation。`agentforge.generalization` 校验输入版本、证据范围、链接/越界路径和分组完整性，再在 `IndexStore(":memory:")` 中索引并调用公共 `evaluate_benchmark`。

`agentforge.eval_generalization` 顺序评估 Calibration 与冻结参数后的 Validation，统一使用现有 Recall@K/MRR 公式；无证据返回率仅以实际适用的负例数为分母。`include_timings` 默认关闭以保留既有基线报告确定性，AF-009G runner 显式开启。RRF 固定 `k=60`，Symbol 指标只适用于精确符号查询；Vector 未接入时报告 `not_measured`，RRF 只融合当前可用通道。报告保留 AI-only 审查标志，不把向量排序或 RRF 分数视为证据充分性，也不改变生产 Retriever 默认行为。

## AF-010-PRE 独立产品进入评测

`agentforge.preproduct` 在通用评测器上增加第三个独立数据版本。它严格验证 80 个 `synthetic_fixture_design` 查询、证据类型、40/40 split、20/20 正负平衡、完整 group 分配和预先声明的工程目标。Northstar fixture 与历史数据分别存储，评测只使用 SQLite `:memory:`。

通用 `evaluate_generalization` 新增可选 Calibration-only 拒答选择钩子：在进入 Validation 前，用固定 0.05 网格比较 vector top score、top-2 margin 和 AND 组合，保存已冻结规则，并在 Validation 只应用一次。该功能只生成实验报告，不修改 `VectorCandidateRetriever`。`preproduct` 另计算 Lexical/Symbol/Vector 首位相关命中集合、通道独有命中、RRF 相对 Lexical 的改善/退化和 Top-10 候选 Jaccard；产品进入判定同时要求 RRF 排名增益和拒答工程目标。

`AF010PRE_HOST_RUNNER.py` 是一次性验收编排器而非产品入口。它复用 R5 的严格 Health 状态机、无代理/重定向客户端、内存输出排空和自有进程清理，写入独立结果文件。默认 Retriever、CLI、正式索引和产品阈值均不受该 runner 影响。

## AF-010-S1 Package 关系研究层

`agentforge.package_relations` 从既有安全扫描器取得已批准文本，在内存中建立独立关系图。节点沿用模块与符号的稳定身份；关系 ID 由来源路径、关系类型、模块/名称/别名和同语义序号构成，不依赖整文件哈希或无关行移动。静态 `__all__` 决定 public export：本地定义产生 `exports_symbol`，从其他模块绑定产生 `reexports_symbol`，唯一链终点另产生 `resolves_to`。普通 import 本身不构成 export。

重复同名绑定保留候选并标成 ambiguous；循环链不产生 resolved 终点；star import、动态 `__all__` 和非字面 dynamic import 标成 uncertain。该层不执行目标代码、不修改 Schema，也不描述运行时导入结果。

`PackageRelationRetriever` 是显式过滤研究接口，按关系类型、来源/目标模块、符号名和解析状态查询；它没有接入默认 Retriever 或 CLI。`agentforge.package_evaluation` 在内存索引中比较 Lexical、Symbol、PackageRelation 及不含 Vector 的离线 RRF，并保留通道贡献、适用性和无证据分母。显式过滤的分数不能代表自然语言路由质量或生产拒答能力。

## AF-010-S3：增量 PackageRelation 生命周期

`IncrementalPackageRelationIndex` 对安全扫描器提供的当前仓库快照重新校验内容哈希，并按相对路径复用未变化的 AST。对确定的静态 import/re-export 依赖图，变化模块的旧版与新版依赖闭包用于定位传递反向依赖来源；仅这些来源的关系记录被重新计算，再与未受影响来源的关系合并。新增/删除/重命名作为相对路径与模块身份的增删处理；关系 ID 和最终排序保持确定。

依赖地图不完整时（动态 import、动态 `__all__`、star import、无法解析的相对导入或 AST/读取失败）回退全量关系构建。仓库扫描不完整会拒绝更新；发生更新异常时旧 graph、manifest 和 AST cache 一起保留，保证下次可重试。该原型没有持久化存储、SQLite schema 变更或正式索引写入。它仍在每次 refresh 全量扫描并计算内容哈希，因此关系生成虽可局部更新，端到端变更检测成本仍为 O(仓库候选文件数)。

ExplainableQueryRouter 针对同时含动态/反射/运行时语义与结构关系意图的 query 返回 `uncertain`，仅选择 lexical 通道并给低置信说明；路由由 query 规则决定，不根据检索结果反向认定关系存在。所有新增关系图、路由和 RRF 组合仍仅限离线研究层。

## AF-010-S4：扫描快照与 AST 缓存

`_snapshot_and_parse()` 将安全扫描、受批准候选文件读取、内容摘要计算和 AST 解析组合为一条快照路径。每次刷新都会读取并哈希所有已允许的 Python 文件；缓存仅在仓库根、相对路径、缓存版本及 SHA-256 均匹配时复用 AST。文件大小和时间戳不用于跳过内容哈希，因此同大小且恢复时间戳的内容改动仍能被检测。当前成本仍为 O(候选 Python 文件数) 的发现、读取与哈希。

扫描不完整或任何已批准 Python 文件读取失败时，本次快照失败关闭，`IncrementalPackageRelationIndex` 保留先前 graph、manifest 和 AST cache。缓存缺失、损坏、版本不匹配或根目录变化会清空 AST 复用并执行完整关系图重建。只有旧/新静态依赖图完整时才按受影响的反向依赖更新；动态 import、动态 `__all__`、star import、解析失败及不完整依赖继续全量重建。该逻辑没有持久化缓存、Schema 迁移或正式索引写入。

`AF010S4_SCALE_RUNNER.py` 提供隔离的完整端到端合成规模实验，使用显式测试扫描限制，不改变生产 2,000 文件默认上限。它对比保留的 S3 两阶段读取/解析路径与 S4 单次快照路径，并在重复刷新、低/高扇入修改、删除/重命名及缓存丢失恢复后，以独立全量关系图重新构建作为 oracle。单次基准用于量化本机工程成本，不能外推为其他机器的性能保证；更激进的元数据缓存需要额外的并发一致性和完整哈希审计设计。

## AF-010-S5：联合评测边界

`AF010S5_OFFLINE_RUNNER.py` 复用 `evaluate_s2()` 在每个合成仓库的内存 SQLite 中计算离线通道，并将逐查询的通道候选、原始分数、路径/行范围、关系解析状态、RRF 贡献和路由解释保存到新的 S5 产物。RRF 采用倒数排名融合并按文件身份去重；离线结果只含 Lexical、Symbol、PackageRelation 三个可用通道。由于当前运行环境不提供合规 loopback 执行入口，Vector 和含 Vector 的 RRF 明确不测量，不能用离线 RRF 冒充完整通道。AI/合成标签、离线排序指标和拒答能力互不等同；本实验不接入默认 Retriever 或生产拒答规则。


## AF-010-S2 离线研究层

`agentforge.query_routing` 提供基于可解释规则的查询意图路由，返回识别类别、命中规则、候选通道、原因及置信状态；不确定查询保持回退，不调用模型。`agentforge.package_s2` 在独立合成仓库上校验固定数据/split，使用每仓库临时内存 SQLite 比较 Lexical、Symbol、PackageRelation、显式离线路由和只融合可用通道的实验性 RRF。真实 Vector Provider 可作为可选接口注入，但本轮因 loopback 前置检查失败未调用。

`IncrementalPackageRelationIndex` 保持关系快照生命周期的原子性：对变化快照执行全量重建，并在失败时恢复旧图/清单；它不是按文件局部重算的高效增量引擎。S2 新通道、路由、评测及 RRF 仅供离线研究，不写正式 Schema、不影响 CLI 默认检索。合成仓库标签是 `synthetic_fixture_design`，不代表人工审核或生产检索质量。资源扫描上限目前阻止 5,000 模块场景。

## AF-011 产品检索与回答合同

`agentforge.product_retrieval.ProductIntentRouter` 先识别裸标识符，再复用可解释路由规则。`ProductRetriever` 根据意图建立有序通道计划，而不是把全部通道默认融合：Symbol 用于精确身份，PackageRelation 与既有静态 reference 用于导入/调用/重导出，Lexical 用于文本、业务和文档证据，Vector 仅在调用方显式注入时可用。候选按仓库相对文件路径稳定去重，合并而不丢弃各通道、分数、symbol/relation ID 和解析状态。RRF 不属于这条产品路径。

检索前要求索引记录的仓库根与当前授权根一致，安全扫描必须完整。每个文件候选返回前重新读取已批准文本并计算完整 SHA-256，与 SQLite 的索引哈希不一致即视为过期证据，不使用旧分块、符号或引用。PackageRelation 构图或某个可选通道失败时保存 `unavailable_channels` 和不确定性；不能把失败通道伪装成空但成功的事实来源。

`agentforge.answer_contract.VerifiedAnswerContract` 位于 Provider 输出之后。它只接受本次 `BuiltContext` 的来源对象，要求路径、起止行和证据原文精确一致，并把来源通道和静态解析状态带入最终证据。离线摘录可验证来源但不构成推理；生成式回答即使含合法引用，也只证明引用属于上下文，不能证明断言被证据蕴含。因此生成式文本默认进入 `limited/inference`，无引用文本拒绝进入受支持结论。无证据、低置信路由或相互矛盾证据返回 `refused` 或 `limited`。

CLI `ask` 的数据流为：仓库身份核对 → 产品路由与候选 → 上下文预算 → Provider → 独立回答合同 → JSON 输出。受限 Agent 的 `search` 工具复用同一路由，但工具权限仍固定为只读白名单。直接 CLI `search` 保留既有 Lexical 合同，避免对原 API 产生隐式变化。

## AF-011 真实聊天执行边界

显式 class/function/method/module/symbol 问句与裸标识符均进入 Symbol-first 路由；若结构化索引确认没有该精确身份，Lexical 相似项只能作为未验证候选，低置信证据门禁在 Provider 调用前返回 `refused`。该门禁在 JSON 中使用独立的 `evidence_gate_refusal` 模式，不伪装成 OfflineProvider 或真实模型成功。

`providers/llama_server` 保持仅显式 loopback HTTP、Bearer 认证、无环境代理和无重定向。聊天请求固定 `stream=false`、`temperature=0`，并有独立连接、I/O 读取及整次生成期限；输出 token 上限有界。非空 `message.content` 是成功条件，`reasoning_content` 只记录是否存在而不替代最终内容。请求审计只保存计数、HTTP 状态、失败阶段、耗时、响应字符数和 finish reason，不保存密钥、认证头或提示内容；任何失败均不自动重试或回退。

`AF011_CHAT_HOST_ACCEPTANCE.py` 是独立验收工具，不改变“产品 CLI 不管理模型进程”的边界。宿主 Runner 只管理自己创建的临时服务：复制经核验的 51 文件运行时、直接引用原 GGUF、随机选择空闲 loopback 端口、在子进程环境传递一次性密钥、并行排空输出，并按 `PROCESS_STARTED → TCP_LISTENING → MODEL_LOADING → MODEL_READY → EVALUATING → COMPLETED/FAILED` 记录状态。只有严格 Health ready 后才运行一个子进程 worker；worker 用内存 SQLite 和隔离合成仓库调用实际 CLI `ask`。有证据案例最多发送一个聊天请求，无证据案例必须在请求前拒绝。最终结果还要求自有进程、端口和临时目录清理以及保护文件哈希不变。
