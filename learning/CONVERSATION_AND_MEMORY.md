# 会话、上下文、Chroma 与长期 Memory：代码导读

更新基线：2026-10-31。本文描述当前 AF-012-MVP 实现和有范围的验收结果，不代表所有模型回答都正确，也不代表完整生产规模已验证。

## 先记住：三种存储各自负责什么

| 存储 | 当前职责 | 权威性与限制 |
|---|---|---|
| 项目 SQLite：`.agentforge/data/index.sqlite3` | 当前索引仓库的文件、代码/文档片段、Python 符号和静态关系；是代码证据的权威快照 | 当前固定索引同一时刻服务一个仓库；切换仓库时重新索引会替换当前 SQLite 快照。历史会话仍按仓库身份保存在 PostgreSQL |
| PostgreSQL | 仓库元数据、会话、消息、会话摘要/摘录、当前选择、Memory、Memory 来源/审批/事件，以及 Chroma 同步 outbox | Memory 状态、来源和内容以 PostgreSQL 为准；事务、外键和迁移由 SQLAlchemy/Alembic 管理 |
| 本地 Chroma | 代码/文档片段和已批准 Memory 的向量候选 | 可重建的搜索副本，不是事实来源。候选必须回查 SQLite 或 PostgreSQL 的当前内容、作用域、状态和哈希；本地 Chroma 实现内部可能使用自己的 SQLite 文件 |

所以，“项目有 Chroma”与“项目有 SQLite”并不冲突：两者负责不同数据。AgentForge 正式代码索引仍在项目 SQLite；语义向量保存在 Chroma；会话和记忆的权威记录保存在 PostgreSQL。

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

会话和默认选择按仓库身份隔离。重新索引另一个仓库后，控制台只列出该仓库的会话；切回旧仓库并恢复相应索引后，旧会话仍保存在 PostgreSQL。更换索引仓库不是把旧会话从数据库删除。

## 多轮上下文和预算

模型收到的上下文包括系统规则、当前仓库标识、可用的历史摘要/摘录、最近完整问答、当前问题、当前检索证据，以及在适用时通过状态核验的已批准 Memory。完整历史保存在 PostgreSQL，但不会无限塞进提示词。

当前“摘要”是**确定性、不完整的历史摘录**，不是 Qwen 生成的语义摘要。生成摘录不会删除原始消息；超过安全字节上限时程序会报错，并提示缩短上下文或开新会话。预算按 UTF-8 字节估算，不等于模型 tokenizer 的精确 token 数。

代词追问会尝试从本会话前文解析唯一符号，再重新检索；它不是通用共指模型。名称歧义、静态解析不确定或当前证据不足时，回答仍可能受限或拒答。历史对话不能证明代码事实，代码结论仍需本轮有效证据。

## Chroma 代码索引

1. 确认当前索引仓库正确，并启动本地 Embedding 服务。
2. 在控制台输入 `/chroma index` 建立/同步当前 SQLite 分块的向量。
3. 输入 `/chroma on` 才在当前控制台运行期间启用 Chroma 候选及 Memory 检索；重开控制台后应检查是否需要重新启用。
4. 普通 SQLite 索引更新后再次执行 `/chroma index`；`/chroma rebuild` 会强制重新嵌入当前分块并清理失效向量。
5. `/chroma status` 查看本机持久目录和 Collection。

`agentforge/embeddings/chroma_index.py` 把路径、行范围、内容哈希、仓库身份和模型身份写入候选元数据。搜索结果还要与当前 SQLite 快照逐项核对；过期或跨仓库候选不会因向量相似度高就变成有效证据。Chroma 位于 `.agentforge/data/chroma`，按 Embedding 模型区分 Collection。首次建立或同步需要已配置且就绪的本地 Embedding 服务。

## 长期 Memory：提案不等于已保存

Memory 按当前仓库隔离。提取模型只能建议候选；它不能自动写入生效的长期记忆。基本人工流程：

1. 在有历史消息的会话中输入 `/memory suggest`。
2. 查看建议后运行 `/memory save 1`。这只创建 `pending` 记录。
3. 在确认内容和来源后运行 `/memory approve <记忆UUID>`。
4. 运行 `/memory sync`，将 PostgreSQL outbox 中的待同步项写入 Chroma。
5. 新会话提出相关但措辞不同的问题。只有 Chroma 找到候选，且 PostgreSQL 再次确认它属于当前仓库、已批准、未撤销/过期、版本与内容哈希和来源有效，它才可能进入上下文。

其他可用操作包括 `/memory list`、`/memory show <UUID>`、`/memory update <UUID> 新内容`、`/memory revoke <UUID>`、`/memory reject <UUID>`、`/memory status` 和 `/memory expire`。更新会使内容回到待审批；撤销或到期会将向量删除任务写入 outbox。`/memory sync` 是显式运行，不表示有后台常驻 worker。

Memory 是用户批准的个性化上下文，不是源码证据；它不会证明当前代码的行为。每条记录要保留来源消息身份和哈希。仓库 A 的 Memory 不进入仓库 B 的提示词。

## 启动与依赖

完整入口是项目根目录的 `Start-AgentForge-All.cmd`。先确保本机 PostgreSQL Windows 服务已运行，再双击该入口。它会复用或启动本地聊天与 Embedding 模型窗口，严格检查 loopback Health 和模型别名，然后启动交互控制台；它不会启动/停止 PostgreSQL。保持两个模型窗口和 PostgreSQL 服务运行。首次使用时，数据库密码在本机安全输入并由当前 Windows 用户保护。

## 已有验收证据与边界

- AF-012-MVP 最终验收报告记录 24/24 个联合宿主检查通过：真实 PostgreSQL、Qwen Chat/Embedding、Chroma 持久化/生命周期、代码证据、会话隔离、跨会话获批 Memory 和清理。真实请求为 Chat 5/5、Embedding 19/19，且无重试。证据来源：[最终验收报告](../RELEASE_NOTES.md) 与 [机器结果](../RELEASE_NOTES.md)。
- PostgreSQL 在 `af012mvp_002` 迁移后的服务重启恢复另有独立宿主证据；它验证会话、消息、Memory 来源和 Outbox 状态恢复。
- 最新完整离线回归在该报告中记录 344 项运行、338 通过、0 失败、0 错误、6 项 Windows 链接权限跳过。跳过不计通过。
- Answer Contract 在真实合成案例中为 `limited`；语义断言限于该合成验收，不是通用语义质量证明。真实桌面进程在最新 Memory 迁移后的完整关闭/重启单独验收仍标为未测量。
- 不要把 AF-010 的 Vector/RRF 研究结果说成产品默认检索收益。当前产品路由只在关系类意图下选择 PackageRelation 通道；RRF、生产拒答阈值和 Reranker 未启用。
