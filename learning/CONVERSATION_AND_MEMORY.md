# 会话、上下文、Chroma 与长期 Memory：代码导读

更新基线：`feature/1.1-core` 的当前源码。本文是会话与 Memory 的深入源码导读；按顺序读概念和代码即可，不要求照着执行其中的命令。命令片段仅供以后实际操作时查阅。AF-012-MVP 有历史真实宿主验收；1.1 的离线测试、单次手工问答与完整联合验收需要分别记录，不能互相替代。

## 先记住：三种存储各自负责什么

| 存储 | 当前职责 | 权威性与限制 |
|---|---|---|
| 项目 SQLite：1.1 分支 `.agentforge/data/repositories/<repository_id>.sqlite3`；旧版 `.agentforge/data/index.sqlite3` | 仓库文件、代码/文档片段、Python 符号和静态关系；是代码证据的权威快照 | 1.0 只有一个固定索引；1.1 开发分支按规范化仓库路径生成稳定 ID，并为每个仓库使用独立 SQLite 文件。旧数据库仍保留给其登记的仓库 |
| SQLite 仓库目录 `.agentforge/data/repositories.sqlite3` | 本机仓库登记、索引路径与活动仓库 | 按规范化路径生成身份；与每个仓库的代码索引分开 |
| PostgreSQL | 会话与 Memory 所需的仓库身份、会话、消息、摘要、当前会话选择、Memory 来源/审批/事件及向量同步 outbox | Memory 状态、来源和内容以 PostgreSQL 为准；事务、外键和迁移由 SQLAlchemy/Alembic 管理 |
| 本地 Chroma | 代码/文档片段和已批准 Memory 的向量候选 | 可重建的搜索副本，不是事实来源。候选必须回查 SQLite 或 PostgreSQL 的当前内容、作用域、状态和哈希；本地 Chroma 实现内部可能使用自己的 SQLite 文件 |

所以，“项目有 Chroma”与“项目有 SQLite”并不冲突：两者负责不同数据。代码索引仍在项目 SQLite；语义向量保存在 Chroma；会话和记忆的权威记录保存在 PostgreSQL。

## 1.1 分支的仓库切换

每个仓库通过 `repo register` 登记，并由规范化后的本地路径得到稳定身份。`repo index` 或 `index --repo PATH` 为其创建/更新独立 SQLite 索引并激活它。之后 `repo list` 可查看已登记仓库，`repo switch ID-or-path` 切换当前仓库；再次切回已有索引的仓库时会复用原索引，不必重新扫描，除非代码变化后需要 `repo index` 更新。

桌面控制台支持 `/repo list`、`/repo register PATH`、`/repo switch 编号或ID`、`/repo index [ID或路径]`、`/repo rebuild [ID或路径]` 和 `/repo remove ID`。`register` 只登记；`index` 建立或更新索引并激活目标仓库。活动仓库必须先切换出去才能删除；删除需要输入 `DELETE`，只移除 AgentForge 自己的索引、登记和该仓库 Chroma 向量，不删除源代码，也不删除 PostgreSQL 中保留的会话和 Memory。旧版固定索引受到额外保护，不能直接通过此操作删除。仓库路径改变后当前实现会得到新的 repository ID。

## 一次会话问答如何流转

```text
当前仓库索引 → 识别当前 repository_id → 读取当前会话历史/摘录
    →（启用 Chroma 时）检索并核验代码向量与已批准 Memory
    → 产品路由与 SQLite 证据校验 → 构造有预算的上下文
    → 本地 Qwen Chat → 引用/Answer Contract 校验
    → 成功回答与用户问题写入 PostgreSQL
```

- `agentforge/conversation_console.py` 是交互入口；它不负责启动、停止模型或 PostgreSQL。
- `agentforge/cli.py` 中的会话命令和 `ask` 路径连接 `ConversationStore`、产品检索、上下文、Provider 与持久化。
- `agentforge/conversation_store.py` 负责会话 CRUD、按序读消息、选中会话和历史摘录。
- `agentforge/conversation_context.py` 检查会话/仓库范围、历史顺序、Memory 来源，并按 UTF-8 字节预算选择最近完整问答轮。
- `agentforge/providers/llama_server.py` 使用本地 Chat API；系统提示、历史、检索证据与 Memory 是不同来源。历史和 Memory 被标记为不可信上下文，不能代替本轮代码证据。
- 只有一次问答成功完成后，`_persist_conversation` 才将用户问题和助手文本作为一个 turn 写入 PostgreSQL；模型请求失败不能伪装成成功回答。

## 会话：新建、列出和切换

桌面控制台先显示当前索引仓库和当前会话。可以输入：

```text
/new 登录模块分析
/list
/switch 2
/rename 权限检查
/history
/delete
/exit
```

控制台的 `/switch` 参数是 `/list` 输出中的**编号**。例如列表第二项用 `/switch 2`；不能把标题或 UUID 放在这个交互命令后面。CLI 管理命令则用 UUID：

```powershell
& .\.agentforge\af012-venv\Scripts\python.exe -X utf8 -B -m agentforge session list
& .\.agentforge\af012-venv\Scripts\python.exe -X utf8 -B -m agentforge session create "新会话"
$ConversationId = '替换为 /list 输出中的会话 UUID'
& .\.agentforge\af012-venv\Scripts\python.exe -X utf8 -B -m agentforge session switch $ConversationId
& .\.agentforge\af012-venv\Scripts\python.exe -X utf8 -B -m agentforge session show $ConversationId
```

会话和默认选择按仓库身份隔离。切换仓库后，控制台读取该仓库索引及对应会话；如果该仓库没有当前会话，会显示“未选择”，可用 `/new 标题` 或 `/list` 后 `/switch 编号`。切回旧仓库会复用现有索引和 PostgreSQL 中的旧会话。更换当前仓库不是把旧会话从数据库删除。

## 多轮上下文和预算

模型收到的上下文包括系统规则、当前仓库标识、可用的历史摘要/摘录、最近完整问答、当前问题、当前检索证据，以及在适用时通过状态核验的已批准 Memory。完整历史保存在 PostgreSQL，但不会无限塞进提示词。

1.0 的历史“摘要”是确定性、不完整的摘录。1.1 分支在选择本地 Qwen Provider、检索上下文通过证据门禁且旧消息完整时，会用本地模型摘要完整的旧问答对，并保存覆盖到的消息 ID。每批摘要输入最多 3,000 UTF-8 字节，摘要请求最多 4,500 字节；这些是字节上限，不是精确 Token 限额。后续摘要输入包含之前的不可信摘要和新覆盖消息；模型被要求生成完整的精简替代摘要，而不是只追加新内容。替代摘要最多 1,800 UTF-8 字节，避免历史摘要逐次嵌套增长；旧版本留下的超大摘要会在启用语义摘要时先尝试压缩。摘要失败时旧摘要及原始消息保持不变。没有语义摘要器时使用明确标记的确定性摘录。

会话请求按当前绑定模型计算预算，不再把 Qwen 窗口套用给所有模型。配置的 Qwen Local 窗口是 4,096 Token（来自 `launchers/start-qwen.ps1` 的 `--ctx-size 4096`）；DeepSeek 必须使用近期、带来源时间的 `/models` 能力信息，旧缓存只能展示，不能参与请求预算。当前 DeepSeek 本机缓存已过期且没有来源时间戳，因此这轮没有把缓存中的大窗口数值当成已验证能力。

计算顺序是：模型上下文窗口 − 本次请求的输出预留 − 安全余量；随后再取已知 `max_input_tokens` 和用户显式输入上限中较小者。模型 `max_output_tokens` 是允许的上限，不代表每轮都要预留这么多输出。当前本机请求使用单独的请求输出值；`--context-budget` 是可选输入 Token 上限，不能覆盖模型实际限制。

如果注册了模型专属 Tokenizer，消息角色、内容和工具协议会按其计数；没有本地 Tokenizer 时，界面和诊断必须标为保守估算，不能说成精确 Token 数。估算按 Unicode 字符数和 UTF-8 字节代理计算，并包含对话格式开销。ContextAssembler 只构造本次请求，不删改 PostgreSQL 原始消息；完整历史放不进当前模型窗口时，才按完整问答轮次选择摘要和最近消息。必需内容仍放不下时，在请求发送前返回 `CONTEXT_BUDGET_EXCEEDED`，不会静默截断。

DeepSeek Thinking 的可见回答和 `reasoning_tokens` 使用量分别记录。DeepSeek Chat API 对单次生成公开的是一个 `max_tokens` 总上限；如果 `/models` 没有给出独立 reasoning 限额，AgentForge 保持未知，不自行推测。官方字段定义可看 [DeepSeek `/models`](https://api-docs.deepseek.com/api/list-models/)、[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) 和 [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) 文档。

离线测试验证了 30/100 轮请求视图、Qwen 4,096 窗口下的请求压缩、DeepSeek 大窗口描述符下保留完整历史，以及 DeepSeek→Qwen→DeepSeek 重新组装时原消息不变。大窗口用例使用官方 `/models` 文档示例构造预算输入，只验证预算算法，不代表当前 DeepSeek 账号或服务的实时验收。真实服务容量与 30/100 轮模型回答仍需宿主验证。

精确符号问题会检查索引中的符号身份；对大型类可选类声明和相关方法的完整行范围。若同名定义中只有一个被包的 `__init__.py` 明确重导出，当前代码优先选择它；不能唯一确定时保留不确定性。模型输入包含证据摘录和 `[E1]` 等请求内编号；回答可以只引用编号，应用再映射到真实路径与行号。不能只给模型编号而不给它需要的源码。相关实现与实验见 [当前版本实践](CURRENT_PRACTICE.md)。

代词追问会尝试从本会话前文解析唯一符号，再重新检索；它不是通用共指模型。名称歧义、静态解析不确定或当前证据不足时，回答仍可能受限或拒答。历史对话不能证明代码事实，代码结论仍需本轮有效证据。

## Chroma 代码索引

1. 确认当前索引仓库正确，并启动本地 Embedding 服务。
2. 在控制台输入 `/chroma index` 建立/同步当前 SQLite 分块的向量。
3. 输入 `/chroma on` 可在当前控制台运行期间启用 Chroma 候选及 Memory 检索；也可由启动环境的 `AGENTFORGE_CHROMA_ENABLED=1` 初始化启用。重开控制台后应检查实际状态。
4. 普通 SQLite 索引更新后再次执行 `/chroma index`；`/chroma rebuild` 会强制重新嵌入当前分块并清理失效向量。
5. `/chroma status` 查看本机持久目录和 Collection。

`agentforge/embeddings/chroma_index.py` 把路径、行范围、内容哈希、仓库身份和模型身份写入候选元数据。搜索结果还要与当前 SQLite 快照逐项核对；过期或跨仓库候选不会因向量相似度高就变成有效证据。Chroma 位于 `.agentforge/data/chroma`，按 Embedding 模型区分 Collection。首次建立或同步需要已配置且就绪的本地 Embedding 服务。

## 长期 Memory：提案不等于已保存

Memory 支持仓库专属和全局两种作用域。提取模型只能建议候选；它不能自动写入生效的长期记忆。以下控制台流程创建的是当前仓库的记忆：

1. 在有历史消息的会话中输入 `/memory suggest`。
2. 查看建议后运行 `/memory save 1`。这只创建 `pending` 记录。
3. 在确认内容和来源后运行 `/memory approve <记忆UUID>`。
4. 运行 `/memory sync`，将 PostgreSQL outbox 中的待同步项写入 Chroma。
5. 启用 `/chroma on` 后，在新会话提出相关但措辞不同的问题。只有 Chroma 找到候选，且 PostgreSQL 再次确认作用域、审批、有效期、版本、内容哈希和来源有效，它才可能进入上下文；预算也可能使其未被注入。

其他可用操作包括 `/memory list`、`/memory show <UUID>`、`/memory update <UUID> 新内容`、`/memory revoke <UUID>`、`/memory reject <UUID>`、`/memory status` 和 `/memory expire`。更新会使内容回到待审批；撤销或到期会将向量删除任务写入 outbox。`/memory sync` 是显式运行，不表示有后台常驻 worker。删除仓库并重新登记、建立索引后，可使用 `/memory rebuild` 将该仓库保留的已批准 Memory 重新排队并同步；不会把全局或其他仓库的记忆当作该仓库的重建任务。

Memory 是用户批准的个性化上下文，不是源码证据；它不会证明当前代码的行为。仓库 A 的专属 Memory 不能进入仓库 B；全局 Memory 经同样的审批和有效性检查后可以跨仓库检索。控制台提供 `/memory global list`、`/memory global approve <UUID>` 和 `/memory global revoke <UUID>`；创建全局提案可使用 CLI `memory propose ... --scope global`，并提供来源消息 ID。完整参数请查看 `python -m agentforge memory propose --help`。

## 启动与依赖

完整入口是项目根目录的 `Start-AgentForge-All.cmd`。先确保本机 PostgreSQL Windows 服务已运行，再双击该入口。它会复用或启动本地聊天与 Embedding 模型窗口，严格检查 loopback Health 和模型别名，然后启动交互控制台；它不会启动/停止 PostgreSQL。保持两个模型窗口和 PostgreSQL 服务运行。首次使用时，数据库密码在本机安全输入并由当前 Windows 用户保护。

## 已有验收证据与边界

- AF-012-MVP 最终验收报告记录 24/24 个联合宿主检查通过：真实 PostgreSQL、Qwen Chat/Embedding、Chroma 持久化/生命周期、代码证据、会话隔离、跨会话获批 Memory 和清理。真实请求为 Chat 5/5、Embedding 19/19，且无重试。证据来源：[最终验收报告](../RELEASE_NOTES.md) 与 [机器结果](../RELEASE_NOTES.md)。
- PostgreSQL 在 `af012mvp_002` 迁移后的服务重启恢复另有独立宿主证据；它验证会话、消息、Memory 来源和 Outbox 状态恢复。
- 最新完整离线回归在该报告中记录 344 项运行、338 通过、0 失败、0 错误、6 项 Windows 链接权限跳过。跳过不计通过。
- Answer Contract 在真实合成案例中为 `limited`；语义断言限于该合成验收，不是通用语义质量证明。真实桌面进程在最新 Memory 迁移后的完整关闭/重启单独验收仍标为未测量。
- 不要把 AF-010 的 Vector/RRF 研究结果说成产品默认检索收益。当前产品路由只在关系类意图下选择 PackageRelation 通道；RRF、生产拒答阈值和 Reranker 未启用。


## AF012：摘要事实账本与失败保留

2026-11-01 的离线修复为会话摘要增加了源消息账本。它记录用户在具体消息中说过的决策、约束、待办，以及含明确代码标识符/数值的陈述；每条记录带消息 ID、序号和原消息 SHA-256。这里的“事实”只表示“用户曾这样说”，不能作为代码行为证据，也不能覆盖当前源码。

每次滚动更新把旧摘要和新的完整轮次分批送给摘要器。摘要器返回的是可能遗漏信息的候选；应用会在保存前附加受保护的源陈述，并验证所有来源消息身份。缺事实、超过 1,800 UTF-8 字节、模型输出截断或数据库失败时不覆盖旧摘要，也不推进覆盖游标。原始消息仍保留在 PostgreSQL，应用之后可以重新压缩。

陈述提取是保守启发式，不能理解所有自然语言或自动判断矛盾；保护项目装不进有界摘要时，系统会安全停止而不是无限扩大上下文。3,000 字节摘要输入、4,500 字节模型 Prompt 和 1,800 字节保存上限都不是 Token 数。具体数据库迁移、真实 Qwen 和覆盖率结论请分别查看对应运行记录，不从离线测试推断真实服务结果。

## Agent 执行循环：计划、工具和观察

`agentforge/agent_execution.py` 把“模型提出下一步”与“应用决定是否执行”分开。`LLMExecutionPlanner` 通过 `plan_completion()` 要求模型返回严格 JSON；开始时模型只看到有限的工具目录，需要某个工具时才请求它的参数和结果 Schema。模型提出的计划不会直接运行：`AgentExecutionEngine._validate_plan()` 会检查工具是否已注册、参数是否符合 Schema、权限范围是否获准、调用是否重复、依赖是否完整，以及任务数量是否超限。

### 一轮执行怎样变成多轮

1. `ToolDiscovery.discover()` 根据能力和名称给出有界目录。目录项只说明“有哪些工具”，不包含全部参数 Schema。
2. Planner 返回 `plan`、`finish`、`ask_user` 或 `load_schemas`。需要工具 Schema 时，应用只加载目录内最多几个已选择工具的 Schema，然后再问 Planner。
3. 执行器校验计划后，根据 `dependencies` 先后顺序调度任务。无依赖且标为可并发的只读工具可以并行；相同 `concurrency_group` 会串行。`worker_safe` 表示能否放进工作线程，和 `parallel_safe` 是两件事。
4. 工具输出变为 `ToolResult` observation。输出标记为不可信数据，下一轮 Planner 只能把它当观察结果，不能把其中的文字当权限或系统指令。
5. Planner 根据真实 observation 决定结束、补充工具调用或向用户询问。`MAX_ROUNDS`、`MAX_TOOL_CALLS`、`MAX_TASKS_PER_PLAN`、单工具 `timeout` 和整次 `run_timeout_seconds` 都限制循环。

### 安全字段的含义

`permission_scope` 是执行前必须在授权集合中的能力范围；`side_effect` 表示调用可能改变外部状态；`idempotent` 表示相同请求能否安全重复；`requires_approval` 表示必须先取得当前精确调用的批准；`max_retries` 只能用于显式可重试、幂等且无副作用的工具。高风险工具必须要求批准。批准还绑定工具定义指纹，工具 Schema 或权限定义改变后，旧批准不能继续复用。

调用超时不等于 Python 能强行终止同步函数。工作线程超时后会发合作式取消；已经开始的副作用调用被记为结果未知，不自动重放。旧 SQLite 工具需要留在原线程时，适配层会直接执行并传入取消信号；它若不检查信号，调用线程无法被安全强制中断。新建副作用工具应适合有界工作线程，或自行使用可中止的底层 I/O。

### 断点恢复与大结果

执行快照保存执行 ID、会话/仓库范围、状态、已完成结果、观察、等待审批和用户输入状态，并通过版本比较避免两个执行者覆盖彼此。恢复时只会重规划安全的只读中断；如果检查点显示副作用调用在运行中被中断，执行结束为“结果未知”，不会再执行一次。超过快照预算的工具结果存入独立结果表，以 SHA-256 校验并通过当前 `execution_id` 绑定的分页引用读取。

新增的 `af013ae_001` 数据库迁移只为执行检查点和大结果建表，目前只通过离线 Schema/SQLite 适配测试，尚未在正式 PostgreSQL 上执行。Qwen/DeepSeek 的真实 Planner 请求也未在本次离线验证中发送。具体实现、测试跳过项和覆盖率见 [Agent 执行循环专项报告](../AGENT_EXECUTION_REFACTOR_REPORT_20261106.md)。
## Phase 3：Planner 如何读取 Memory 与 Streaming 事件

### Planner 读取记忆的边界

普通对话 Planner 和工具 Agent Planner 都只接收本轮任务相关的少量记忆，不会读取整张 Memory 表。对话路径由 CLI 在显式启用 Chroma 时检索；工具 Agent 通过 `--use-memory` 显式开启。候选先经 Chroma 相关性查询，再由 PostgreSQL 核验批准状态、仓库/全局作用域、版本、内容和来源消息身份。传给 Planner 的记忆仍标记为历史上下文和不可信数据，它不是当前指令，也不是源码证据。

工具 Planner 优先用最新的 `user_input` observation 检索，其次用最初目标；每轮最多四条且有字节上限。当前用户输入优先于记忆。若查询出错，本次请求不带 Memory，但诊断会记录 `unavailable` 和安全的错误类别；若上下文超限，先移除可选 Memory，再判断必要上下文能否放入窗口。Memory 在 PostgreSQL 中仍保留，省略只作用于这一轮 Prompt。

### 历史读取和摘要压缩不是同一件事

`SessionService.load_context()` 只读取消息和已保存摘要，不触发压缩；`restore_context()` 可以调用存储层 `prepare_history()`，后者才有权按预算生成或更新摘要。CLI 提问开始时先只读载入；只有证据检查通过、确定需要调用回答模型后，才进入可压缩的恢复路径。这样无证据拒答不会因为“加载会话”而意外触发摘要写入。原始消息始终保留。

### Agent Streaming 事件顺序

一次工具 Agent 执行的主要事件是：`PLANNER_STARTED` → `PLAN_READY` 或 `PLAN_REJECTED` → `TOOL_CALL_STARTED` → `TOOL_PROGRESS` → `TOOL_CALL_COMPLETED` / `TOOL_CALL_FAILED` → `OBSERVATION_RECEIVED` → `REPLANNING` → 后续 Planner 轮次 → `FINAL_ANSWER` → `USAGE` → `COMPLETED`。模型产生的原始 chunk 不会立即显示；通过 Answer Contract 的最终文本才作为 `FINAL_ANSWER` 和可选 `CONTENT_DELTA` 发布。等待审批/补充输入会 `suspend`，失败或取消会清除未验证缓冲内容。

当前真实宿主尚未测量这些调用。离线全量回归为 657 项，其中 651 通过、6 项因 Windows 链接创建权限跳过；全生产包覆盖率 86.24% 行、76.78% 分支。详见 [Phase 3 验收报告](../AGENTFORGE_PHASE3_ACCEPTANCE_REPORT_20261106.md)。
