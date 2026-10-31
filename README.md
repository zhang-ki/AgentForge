# AgentForge — 只读 RAG MVP

AgentForge 使用 Python 标准库和 SQLite，对用户明确指定的单个本地仓库进行受限扫描、按行切块、词法检索、Python 代码符号/引用索引和离线证据摘要，并提供有限步数的只读 Agent 工具循环。没有第三方运行依赖，不需要安装。离线功能不需要网络、API 密钥或模型文件。

要求 Python 3.10 或以上。本轮实际环境为 Windows、Python 3.12.7。项目直接位于 D:\agent\AgentForge，包位于项目根目录。

## 双击启动（Windows）

1. 双击 `Start-Qwen.cmd`，保持窗口打开，等待显示 `model loaded` 和 `listening on http://127.0.0.1:56572`。首次启动会生成随机 API key，并使用 Windows 当前用户加密后保存在 `.agentforge/launcher/qwen-key.dpapi`。
2. 双击 `Start-AgentForge.cmd`，输入问题并回车；输入 `exit` 退出提问窗口。每次提问调用已有 `ask --provider llama-server`，使用当前索引，各问题互不保留对话上下文。检索或证据不足时仍可能拒答。
3. 在千问服务窗口按 Ctrl+C 停止模型。关闭提问窗口不会停止模型。

启动器只监听 `127.0.0.1:56572`，不会自动更换端口或终止其他进程。如果此前手动启动的服务还在占用该端口，先在原窗口按 Ctrl+C 停止，再运行 `Start-Qwen.cmd`；旧服务使用的手动密钥不能和启动器生成的密钥混用。加密密钥属于当前 Windows 账户及本机，换账户或机器不能直接使用；不要公开或手工复制该文件。

双击提问使用既有索引，不会自动切换或重建仓库；需要更换仓库时按下文使用 `index --repo`。该窗口是模型问答入口，并不将离线 `agent` 工具循环改成模型驱动。启动器没有改变 PowerShell 执行策略。

## 直接运行

在 PowerShell 中运行：

~~~powershell
cd D:\agent\AgentForge
python -X utf8 -B -m agentforge --help
python -X utf8 -B -m agentforge index --repo .\tests\fixtures\synthetic_repo
python -X utf8 -B -m agentforge search refresh_access_token --top-k 3
python -X utf8 -B -m agentforge search src/cache.py
python -X utf8 -B -m agentforge ask refresh_access_token --context-budget 6000
python -X utf8 -B -m agentforge agent refresh_access_token --repo .\tests\fixtures\synthetic_repo --max-steps 4
python -X utf8 -B -m agentforge status
python -X utf8 -B -m unittest discover -s tests -v
~~~

-X utf8 使输出编码一致；-B 禁止生成字节码缓存。也可使用 python -m agentforge --help，包无需安装。

index 必须提供 --repo；没有默认扫描目录。search、ask 和 status 只使用已有索引。index、search、status 输出 JSON；ask 输出明确标注的离线摘录与来源。

退出码：0 表示命令成功（包括检索无结果），1 表示文件系统、索引或运行错误，2 表示参数/配置错误。参数帮助使用 --help。

## 有限步数只读 Agent

`agent` 使用确定性的离线规划器依次调用白名单工具 `index`、`search`、`read` 和 `list-symbols`。它不是自主联网或真实模型推理：最多 8 步，默认每个工具有 30 秒协作式墙钟预算，总预算由步数和工具预算共同限定；超时不重试。每次调用记录来源、强类型参数、结果或拒绝原因，并输出 JSON 审计记录。

工具绑定到 `--repo` 指定的唯一仓库根目录；`read` 只能读取当前安全扫描允许的相对路径，单次最多 200 行。未知工具、多余参数、路径遍历、秘密文件、跨仓库切换和超预算调用均被拒绝。`index` 只写 AgentForge 固定 SQLite 索引，源仓库保持只读；循环没有 shell、网络、文件创建、补丁或源仓库写入工具。检索到的 README、注释和模型输出均按不可信数据处理，不能扩大工具白名单。

离线 Agent 已用 SQLite 内存索引完成确定性端到端测试。真实模型驱动的工具选择尚未验收，不能把该离线规划器描述为真实模型 Agent。

## 索引与更新

磁盘数据库固定为 D:\agent\AgentForge\.agentforge\data\index.sqlite3。没有数据库路径参数，数据库不会写进被扫描仓库。SQLite 临时表使用内存，事务日志留在同一数据目录。该目录已加入 .gitignore。

一个数据库保存一个仓库的索引。再次索引同一根目录时，保留未变化记录、重建变化分块、移除已删除或不再允许索引的文件。切块参数变化也会重建分块。空文件保留文件元数据，产生零个分块。

默认行窗口为 80 行、重叠 10 行，可配置：

~~~powershell
python -X utf8 -B -m agentforge index --repo .\tests\fixtures\synthetic_repo --window-lines 40 --overlap-lines 5
~~~

仓库根目录改变时，默认拒绝覆盖索引。用户确实要切换时，额外提供 --replace-repository。它只在新仓库扫描完整、内容准备完成后，事务性替换本项目的索引记录；不修改两个源仓库。本轮交付的磁盘索引仅来自合成仓库。

## 检索和回答

评分由关键词频率（每词最多计 5 次）、路径词匹配（+3）、完整词/标识符匹配（+4）、简单声明行匹配（+8）、完整路径查询子串（+8）组成。中文连续关键词支持子串计分（每次 +2，最多 5 次）。`search` 仍是确定性的词法检索；代码符号查询由独立的结构化 Parser 索引提供，不改变原有检索排序。

结果按分数降序，再按相对路径、起止行排序。同内容哈希的重复片段保留排序最前的一项。Top-K 范围为 1–100，查询最多 512 字符。重叠但内容不同的窗口仍可能同时出现。

引用形式为 [[相对路径:起始行-结束行]]，行号从 1 开始。上下文对候选数、内容重复和预算做独立限制。预算按序列化证据的 UTF-8 字节数作保守 Token 估算；不是精确 Token 数，也不包含问题和未来模型系统消息的开销。过大的完整片段会跳过，不裁出虚假行范围。

`ask` 默认使用离线 Provider，只生成确定性的证据摘录，不执行模型推理，也不保证能直接回答所有语义问题。无证据或预算装不下证据时明确表示无法确定。

可以显式选择本机 llama-server Provider。必须同时给出该服务器的明确回环 HTTP URL 和模型标识；CLI 不猜端口、不启动服务，配置错误不会退回离线模式。URL 只接受回环 IP，不接受主机名、局域网/公网地址、代理或重定向。示例如下，其中尖括号内容必须由用户替换成实际已配置的值；示例本身不是服务配置：

使用 `llama-server` Provider 前，还必须在当前 Python 进程环境中设置 `AGENTFORGE_LLAMA_API_KEY`。AgentForge 将其作为 Bearer 认证头发送；密钥不接受命令行参数，也不会写入项目配置或索引。

~~~powershell
python -X utf8 -B -m agentforge ask "refresh access token" --provider llama-server --base-url "http://<loopback-ip>:<port>/v1/chat/completions" --model "<local-model-id>"
~~~

该选项会把本次检索上下文交给本机服务，并对返回引用执行当前证据成员校验。连接失败、HTTP 错误、无效响应或伪造引用会报错，不会显示成成功回答。若没有检索证据，Provider 会说明未调用模型。真实模型生成需要用户另行启动并配置本机服务；AgentForge 不管理服务进程。历史阶段 2B-3S 的结果未知；后续独立的 AF-011 本地真实聊天和引用验收已通过，具体范围见下方 V1.0 记录。

## Python 代码感知索引

`index` 会在保留原有词法分块的同时，用标准库 `ast` 为已允许扫描的 `.py`/`.pyi` 文件提取模块、类、函数/方法、嵌套定义、签名、文档字符串、行范围、导入和可静态识别的调用。索引一个 Python 仓库后，可用以下命令查询；如果要切换当前固定索引中的仓库，按 index 帮助显式使用 `--replace-repository`：

~~~powershell
python -X utf8 -B -m agentforge symbols --name "refresh_access_token"
python -X utf8 -B -m agentforge symbols --qualified-name "pkg.helpers.Worker.run" --kind method
python -X utf8 -B -m agentforge symbols --path "src/cache.py"
python -X utf8 -B -m agentforge references --path "pkg/api.py" --status unresolved
~~~

`symbols` 可按名称、限定名称、类型和相对路径筛选；`references` 可按路径、目标、解析状态和引用类型筛选。

导入与调用只在本次索引仓库内做保守静态匹配；唯一目标标记为 resolved，多个候选标记为 ambiguous，不能确定的目标保留为 unresolved。直接的 `__import__` 和 `importlib.import_module` 形式会单独记录为未解析的动态导入；别名、反射等动态形式不保证都能识别。不会导入或执行源代码，也不声称静态关系等于运行时调用关系。语法错误或解析资源上限不会删除该文件的词法索引。当前不支持其他语言的结构化 Parser、类型推断、反射或动态代码跟踪。

SQLite Schema v3 包含词法分块、`symbols`、`symbol_references`、`parse_status` 和可选的 `vector_embeddings`。v1 索引只读打开时继续提供旧词法查询；v2 只读时继续提供词法与符号查询。显式可写 `index --repo PATH` 才执行增量迁移 v1→v2→v3 或 v2→v3；迁移不会自动调用 Embedding 模型。数据库固定留在 AgentForge 的 `.agentforge/data/index.sqlite3`，不会写入被索引仓库。

## 离线检索评测与可选向量候选

使用以下命令在人工合成仓库上重现检索基线：

~~~powershell
python -X utf8 -B -m agentforge.eval_retrieval
python -X utf8 -B -m agentforge.eval_retrieval --dataset expansion
python -X utf8 -B -m agentforge.eval_retrieval --dataset review-pool
~~~

评测集 `agentforge-retrieval-v1` 包含 9 条人工定义的查询和文件/符号 ground truth。真实 Qwen3-Embedding-0.6B Q8_0 的小规模合成结果：词法 Recall@1/5/10 为 0.75/0.9375/0.9375、MRR 为 0.9375；精确结构化符号查询为 0.928571/1.0/1.0、MRR 为 1.0；向量检索为 0.6875/1.0/1.0、MRR 为 0.875。向量 Top-1/MRR 低于词法基线。无证据查询单独报告；当前向量候选器没有置信阈值，该查询仍会返回候选。9 条查询只能作为小规模回归数据，不能证明一般仓库检索质量。评测数据库使用 SQLite 内存模式，不访问其他仓库。

AF-009D 另有 12 条独立扩展候选（8 条有证据、4 条无证据），每条带行范围和纳入/排除理由，但全部保持 `pending_human_review`，不会覆盖或并入 v1。评测输出保存向量 top score、top-2 margin 和固定阈值的覆盖率、误接受率、误拒绝率及精确率。当前样本不足以选定生产阈值，VectorRetriever 默认行为没有改变。完整协议和探索性结果见 `AF009D_EVALUATION.md`。

AF-009E 的 `agentforge-retrieval-v2-candidates` 独立扩充为 20 条有证据和 20 条无证据，保留原 12 个稳定 ID；全部仍为待人工审核。逐条证据、短摘录、相关性边界和审核选项见 `AF009E_HUMAN_REVIEW.md`。建议校准/验证清单各 20 条并按 `leakage_group` 隔离；审核完成前不得用于正式阈值选择。

AF-009F-A 另建 `agentforge-retrieval-provisional-v3` manifest，从冻结候选池、独立 AI 裁定和原 split plan 校验后在内存中派生 40 条暂定查询；3 条错误前提查询使用证据支持的改写，同时保留原查询。全部记录为 `provisional_ai`、`human_reviewed: false`。它只用于工程实验，不能替代人工 ground truth。

`agentforge.embeddings.EmbeddingProvider` 独立于聊天 Provider。`VectorCandidateRetriever` 和 `VectorStore` 提供显式向量候选索引/查询 API；`RoleAwareEmbeddingProvider` 支持 `embed_query(text)` 与 `embed_document(text)` 不同预处理，旧 `embed(text)` Provider 仍兼容。`LlamaServerEmbeddingProvider` 通过标准库调用已经运行的 loopback llama-server `/v1/embeddings`，不启动或发现服务；只接受明确的 loopback IP，不使用环境代理、不跟随重定向。真实验证使用官方 Qwen3-Embedding-0.6B Q8_0，revision、SHA-256、输入格式和验收证据见 STATUS.md。该 Provider 目前没有 API key 支持；只适合严格短时、loopback-only 的本地验证，CORS 限制不是认证。向量候选不会自动与词法或符号候选融合，也不代表事实正确性。

若已由用户自行启动并配置兼容的 loopback Embedding 服务，可以复现真实模型评测：

~~~powershell
python -X utf8 -B -m agentforge.eval_local_embeddings --base-url "http://127.0.0.1:<port>" --model-id "Qwen/Qwen3-Embedding-0.6B-GGUF@<revision>:Q8_0#<sha256>" --api-model "<server-alias>" --dimension 1024
python -X utf8 -B -m agentforge.eval_local_embeddings --base-url "http://127.0.0.1:<port>" --model-id "Qwen/Qwen3-Embedding-0.6B-GGUF@<revision>:Q8_0#<sha256>" --api-model "<server-alias>" --dimension 1024 --dataset expansion
python -X utf8 -B -m agentforge.eval_local_embeddings --base-url "http://127.0.0.1:<port>" --model-id "Qwen/Qwen3-Embedding-0.6B-GGUF@<revision>:Q8_0#<sha256>" --api-model "<server-alias>" --dimension 1024 --dataset review-pool
~~~

尖括号必须替换为实际配置；命令本身不会启动服务、猜测端口或加载模型。模型的 query 侧使用官方格式 `Instruct: <task>\nQuery: <text>`；document 输入为原文。当前评测脚本把向量索引建立在内存 SQLite，不修改正式索引或合成源文件。

AF-009F/AF-010A 的显式实验入口如下；调用方必须先自行安全启动已经核验的 loopback Embedding 服务：

~~~powershell
python -X utf8 -B -m agentforge.eval_provisional --base-url "http://127.0.0.1:<port>" --model-id "<fixed-model-identity>" --api-model "<server-alias>" --dimension 1024 --rrf-k 60
~~~

该命令只访问 `/v1/embeddings`，在 SQLite `:memory:` 中建立向量，使用 Calibration 选择探索性规则，再用固定规则评估一次 Validation，并把完整网格与 Lexical/Symbol/Vector/RRF 对比写入 `AF009FA_RRF_RESULTS.json`。当前实验选择 `top_score >= 0.70`，Validation 误拒绝率为 0.80，因此没有启用生产阈值。RRF 是显式、文件级候选融合，不改变默认 Retriever；详情见 `AF009FA_RRF_EVALUATION.md`。

AF-009G 的第二个、结构不同的合成仓库和独立 AI 暂定评测集位于 `tests/fixtures/retrieval_eval_generalization_repo/`、`retrieval_eval_generalization_v1.json` 与 `retrieval_eval_generalization_split_v1.json`。离线比较命令如下；不提供模型参数时只运行 Lexical、Symbol 和不含 Vector 的 RRF：

~~~powershell
python -X utf8 -B -m agentforge.eval_generalization
~~~

如果用户已在本机安全启动兼容的 Embedding 服务，可显式指定回环地址运行完整向量实验：

~~~powershell
python -X utf8 -B -m agentforge.eval_generalization --base-url "http://127.0.0.1:<port>" --model-id "<fixed-model-identity>" --api-model "<server-alias>" --dimension 1024
~~~

该入口不启动服务；客户端只请求 `/v1/embeddings`，并要求显式 loopback IP。Calibration 与 Validation 按 leakage group 隔离，Validation 固定参数后只评估一次。结果写入独立文件 `AF009G_GENERALIZATION_RESULTS.json`，使用 `:memory:` SQLite。标签是 AI 证据审查，不是人工 ground truth；没有测量 Vector 时不报告向量或阈值指标，也不据此批准生产拒答策略。当前 RRF 未在含 Vector 的独立 Validation 上证明改进，因此没有接入产品配置。

AF-010-PRE 增加第三个、完全独立的 Northstar 冷链合成仓库及 80 条 AI 暂定查询，路径为 `tests/fixtures/retrieval_eval_preproduct_repo/`、`retrieval_eval_preproduct_v1.json` 和 `retrieval_eval_preproduct_split_v1.json`。它只用于产品进入审查：Calibration 选择实验拒答规则，Validation 固定后只评估一次；RRF 固定 `k=60`。实际结果见 `AF010PRE_EVALUATION.md`。本阶段没有提供产品 CLI 开关，也没有改变默认 Retriever 或启用阈值。

向量产品存储位于 AgentForge 固定 SQLite 数据库的 Schema v3 `vector_embeddings` 表，采用归一化 float32，按余弦相似度排序；维度最多 4096、记录最多 10,000、向量数据最多 128 MiB、Top-K 最多 100。向量与文件、来源、模型及内容版本关联；文件变化或删除后旧候选失效。真实质量评测只使用 `:memory:`，SQLite 迁移测试使用隔离临时库，正式索引不由评测或本轮测试重建。

## 扫描边界和当前限制

默认上限：单文件 1,000,000 字节、总内容 10,000,000 字节、2,000 个候选文件、20,000 个目录条目、100,000 个分块；Python 结构化解析另受单文件 AST 节点 100,000、符号 10,000、引用 25,000，以及仓库总符号 100,000、引用 200,000 的限制。整体扫描预算或不完整目录扫描会使本次更新失败并保留原索引；单个超大、不可读、非 UTF-8、疑似秘密或二进制文件会被跳过。结构化解析遇到语法错误或自身上限时记录失败，但保留词法数据。命令仅输出跳过原因计数，不输出秘密内容。

扫描器拒绝符号链接、Windows reparse point 和检测到的硬链接，排除版本控制、依赖、构建、缓存、数据库、模型权重以及常见凭证文件。秘密识别是保守启发式，不能保证发现所有内嵌秘密。详细规则和限制见 SECURITY.md。

agentforge/providers/llama_server.py 提供可显式选择的本机聊天客户端。客户端仅接受明确的回环 IP 和 HTTP，拒绝主机名、代理、重定向。它不会管理服务或创建进程。历史阶段 2B-3S 结果未知；后续 AF-011 的独立真实聊天和引用验收已通过，具体状态见后文和 STATUS.md。

## 测试说明

合成仓库位于 `tests/fixtures/synthetic_repo` 和 `tests/fixtures/symbol_repo`。所有可变测试数据位于 `tests/.tmp`，测试结束清理；测试数据库使用 SQLite 内存数据库或测试专用临时 SQLite。测试不会打开或写入正式 `.agentforge/data/index.sqlite3`，也不会改写正式 `read_only_evidence.json`。

实际链接测试会尝试在测试目录内构造链接；权限不足时明确跳过，不将模拟测试当作真实链接验证。完整结果与未验证事项见 STATUS.md；逐项文件用途见 FILE_MANIFEST.md。

## Package 关系研究原型

AF-010-S1 增加了仅供离线研究的 Python package import/export 关系图，识别显式 import、符号 import、别名、静态 `__all__`、重导出链和唯一解析终点；循环、star import 和动态形式保守保留为未解析或不确定。它不导入被索引代码、不写正式数据库、不改变默认检索，也没有产品 CLI。

可复现实验命令：

~~~powershell
python -X utf8 -B AF010S1_EVALUATE.py
python -X utf8 -B -m unittest tests.test_package_relations -v
~~~

结果见 `AF010S1_EVALUATION.md` 和 `AF010S1_RESULTS.json`。40 条标签来自合成 fixture 设计并由 AI 证据审查，`human_reviewed=false`；Vector 本轮未运行，离线 RRF 不含 Vector。这些数据不能作为产品接入或拒答阈值批准依据。


## AF-010-S2 多仓库结构化检索研究

AF-010-S2 增加三个独立的合成 package 仓库（共 120 条固定 Calibration/Validation 查询）、解释型离线路由、关系快照生命周期原型和可重复的离线通道评测。标签为自动合成 fixture 设计（`human_reviewed=false`），不能视为人工审核 ground truth。真实 Vector 评测受当前执行环境的 loopback connect 限制，未发送 Embedding 请求；离线 RRF 不含 Vector。默认 Retriever、生产拒答阈值和产品 RRF 配置保持原样。

可复现离线结果与规模实验：

~~~powershell
python -X utf8 -B AF010S2_OFFLINE_FINAL_RUNNER.py
python -X utf8 -B AF010S2_SCALE_RUNNER.py
python -X utf8 -B -m unittest tests.test_package_s2 -v
~~~

结果详见 `AF010S2_EVALUATION.md` 与对应 JSON。该小规模合成研究不构成生产质量保证；不要把 `AF010S2_HOST_RUNNER.py` 当作当前受限环境下可安全运行的通用入口。

## AF-010-S3 增量关系图研究

`IncrementalPackageRelationIndex` 是离线原型，可检测文件新增、修改、删除和重命名，并在静态依赖图可确定时重算变化模块及受影响的反向依赖来源。每次 refresh 仍扫描并哈希所有候选文件；动态 import、动态 `__all__`、星号导入、无法解析的相对导入或解析失败会全量重建，失败更新保留旧快照。它不写 SQLite 或被扫描仓库，也没有接入默认 Retriever。

`python -X utf8 -B AF010S3_SCALE_RUNNER.py` 在项目目录中运行隔离合成基准，写入独立的 `AF010S3_SCALE_RESULTS.json`；若结果已存在则拒绝覆盖。`python -X utf8 -B AF010S3_SCALE_RUNNER.py --validate-existing` 只读验证并汇总已有结果。基准 100/1,000 模块覆盖测试专用扫描限制下的完整扫描/哈希/AST/关系更新，5,000 模块只覆盖内存 AST 与关系图层，不代表完整的 5,000 文件扫描流水线。结果和限制见 `AF010S3_EVALUATION.md`。本阶段结论为 `continue_to_defer`；关系图、路由、RRF 和拒答规则均未接入产品默认行为。

## AF-010-S4 扫描成本验证

S4 将安全扫描发现、Python 文件读取/哈希和 AST 缓存判定合并为单次快照读取。每次刷新仍读取并哈希整个已允许的 Python 文件集合；相同摘要才复用 AST，不能仅凭大小或修改时间认定内容未变。扫描不完整或读取失败时保留旧关系图；缓存缺失、损坏、版本不符或仓库根变化时全量重建。生产扫描默认仍限制 2,000 个文件。

复现本轮完整 100/1,000/5,000 模块合成端到端扫描、增量更新和全量 oracle 对照：

~~~powershell
python -X utf8 -B AF010S4_SCALE_RUNNER.py
python -X utf8 -B -m unittest tests.test_package_s2 tests.test_package_relations -v
~~~

规模 runner 使用显式测试上限，结果只在 `AF010S4_SCALE_RESULTS.json` 不存在时创建，不覆盖已有记录。当前实测 5,000 模块即 5,001 个 Python 文件，冷构建为 55.23 秒；重复刷新约 49.24 秒，因为全量扫描与哈希仍然占用成本。增量图每次均与全量图 oracle 一致。具体分项指标、单次测量局限、Python 分配峰值与进程工作集采样见 `AF010S4_EVALUATION.md`。该研究没有改变默认 Retriever、正式索引或生产扫描限制；真实 Vector 联合验证仍未完成。

## AF-010-S5 受限联合评测

`AF010S5_OFFLINE_RUNNER.py` 可在项目根目录复核冻结多仓库评测，并写入新的 `AF010S5_RESULTS.json`（存在结果时拒绝覆盖）。本轮离线报告见 `AF010S5_EVALUATION.md`。当前执行环境禁止网络，未启动模型、未发送 Embedding 请求；Vector 与完整四通道 RRF 均未测量。离线 RRF 只融合本轮可用的 Lexical、Symbol、PackageRelation 通道。该研究结果不改变默认 Retriever，也不启用生产拒答阈值。

## AF-011 证据优先第一版

`ask` 与受限 `agent` 现在使用确定性的产品查询路由：精确标识符优先 Symbol，导入、调用、公开 API 与重导出问题优先静态关系证据，文档和普通业务问题保留词法检索；Vector 只有显式提供时才作为候选来源。直接 `search` 命令继续保持原有词法行为。RRF、Reranker 和生产相似度拒答均未默认启用。

回答以结构化 JSON 返回，并区分 `answered`、`limited` 和 `refused`。证据包含当前仓库内的路径、行范围、精确原文、来源通道和静态解析状态；当前文件内容必须与索引哈希一致。引用属于当前证据并不自动证明模型的自然语言结论成立，因此生成式文本在没有独立蕴含证明时保持 `limited/inference`。离线 Provider 只生成可验证的证据摘录，不冒充真实模型推理。

AF-011 的 20 条 AgentForge 源码任务使用隔离快照和内存 SQLite：Validation 路由正确率与证据合同有效率均为 100%，端到端为 9/10；唯一离线失败是英文查询无法稳定找到中文 `SECURITY.md`。另有一次独立的真实 Qwen3.5 4B Host Runner 验收通过，范围和证据见下节。完整离线指标和限制见 `AF011_ACCEPTANCE_REPORT.md` 与 `AF011_ACCEPTANCE_MATRIX.md`。

## V1.0 真实 Qwen 聊天验收

聊天 CLI 现在额外支持 `--total-timeout`（默认 180 秒，10–300）和 `--max-output-tokens`（默认 256，1–512）。连接默认 2 秒，响应读取默认 60 秒；四个边界彼此独立。客户端只接受显式 loopback URL，通过 `AGENTFORGE_LLAMA_API_KEY` 发送 Bearer 认证，禁用代理和重定向，不重试，也不会在失败后切换到离线回答。

真实验收现为 `PASS`：普通 Windows Host Runner 成功加载既有 Qwen3.5 4B，通过严格 Health readiness，并经真实 `ask` 路径发送恰好一次聊天请求。回答引用 `[E1]`，由应用映射并核对为 `src/cache.py:4-6` 的真实代码；Answer Contract 保持 `limited/inference`，独立确定性语义断言全部通过。不存在符号的问题在 Provider 前安全拒答，没有产生第二次聊天请求。

Runner 已停止其自有服务、释放端口并删除临时运行时；正式索引、只读证据、冻结历史、GGUF 和原始运行时保持不变。`service_exit_code=1` 来自 Runner 在进入 `COMPLETED` 后主动终止仍运行的 Windows 子进程，并有 `runner_initiated_termination`、进程停止和端口释放记录支持。完整证据见 `AF011_CHAT_ACCEPTANCE_RESULTS.json` 与 `AF011_CHAT_ACCEPTANCE_REPORT.md`。无需再次运行 Host Runner。

### V1 质量验收记录
用户确认具备符号链接权限的 Windows 宿主完整回归 265/265 通过且无跳过；本次记录未包含该次运行的原始日志。独立的既有覆盖率测量为全生产包行 91.41%、分支 86.00%，核心安全模块达到目标；该覆盖率批次曾有 3 项权限跳过，后续宿主测试结果为单独的用户确认。历史真实本地聊天 PASS 与本次离线质量门禁分别记录，详见 `TEST_COVERAGE_REPORT.md` 和 `TEST_ACCEPTANCE_RESULTS.json`。
