# 当前版本：从启动到证据回答

本文对应 `feature/1.1-core` 的代码，适合配合四周学习计划操作。离线测试、人工使用真实模型和完整宿主验收是不同的验证：记录自己实际执行的步骤，不把一种结果替代另一种。

## 1. 先分清三条入口

| 入口 | 做什么 | 对应代码 |
|---|---|---|
| 桌面控制台 | 选择仓库和会话，连续向本地模型提问 | [conversation_console.py](../agentforge/conversation_console.py)、`Start-AgentForge-All.cmd` |
| `python -m agentforge ask` | 对当前索引提问；默认是离线 Provider，指定 `--provider llama-server` 才使用真实聊天服务 | [cli.py](../agentforge/cli.py)、[llama_server.py](../agentforge/providers/llama_server.py) |
| `python -m agentforge agent` | 确定性、有限步的只读工具循环 | [agent_loop.py](../agentforge/agent_loop.py) |

桌面入口需要项目持久化依赖、已配置的本地 PostgreSQL 和模型资产。先启动 PostgreSQL，再双击 `Start-AgentForge-All.cmd`；入口会检查或启动聊天和 Embedding 服务，等到模型就绪后启动控制台。它不会启动 PostgreSQL。数据库凭证在本机安全输入，不写进练习笔记。

## 2. 仓库与会话操作

下面是**桌面控制台内**的命令，不是 PowerShell 命令。先用 `/repo list` 查看活动仓库，列表中的 `*` 标记活动项。`/repo switch 1` 中的数字来自这份仓库列表；它只切换到已有索引的仓库，不重新扫描。

```text
/repo list
/repo switch 1
/list
/new 代码学习
/history
```

`/list` 显示的是**当前仓库的会话**；`/switch 1` 使用这份会话列表的编号，不是仓库编号，也不是 UUID。切换仓库后若显示“当前会话：未选择”，可 `/new 标题`，或先 `/list` 再 `/switch 编号`。切回旧仓库时可继续使用它已有的索引和会话。

登记一个练习仓库可用 `/repo register 路径`，然后用 `/repo index 路径` 建立索引并切换到它；`/repo index` 更新当前仓库。源码变化后，切换回来不会自动重新扫描，需要再次索引。上述命令会写入本地仓库目录和索引，学习时请使用自己创建的临时仓库。`/repo rebuild` 会重建目标索引；`/repo remove ID` 需要切换离开目标仓库并输入 `DELETE` 确认。它不删除源代码，也保留 PostgreSQL 会话和记忆；旧版固定索引有额外保护。

对应的 PowerShell 帮助命令可安全查看参数：

```powershell
$Python = '.\.agentforge\af012-venv\Scripts\python.exe'
& $Python -X utf8 -B -m agentforge repo --help
& $Python -X utf8 -B -m agentforge session --help
& $Python -X utf8 -B -m agentforge ask --help
```

## 3. 提问、证据与预算

在包含目标函数的仓库中，先问一个明确的问题，例如：

```text
What does function refresh_access_token return?
```

程序先路由和检索，再把必要源码摘录及 `[E1]` 这类本次请求专用编号送入模型。回答中的编号由应用映射回文件路径和行范围。模型不必复述源码，但必须有足够源码才能回答；单独的编号不能让它知道代码内容。`limited` 表示回答和引用有一定依据，不能当成每句话都已通过语义证明。回答如果在句中断开，也不要当成完整答案；需要检查实际响应及输出长度限制。

遇到 `Context budget exceeded` 时，看诊断中的 `system`、`question`、`evidence` 和 `framing` 字节数。会话输入默认硬上限 3,500 字节，保留 5% 后可用 3,325 字节；这是 UTF-8 字节代理，不是模型精确 Token 数，也不是输出窗口大小。大类查询可以保留类声明与相关方法的完整行范围。同名符号若有唯一明确包重导出，会优先该公共定义；仍有歧义时可在问题中写完整限定名，例如 `package.module.ClassName`。必要证据放不下时应拒绝或限定，不应伪造引用。

## 4. Chroma 和 Memory

SQLite 保存仓库索引，PostgreSQL 保存会话和长期 Memory 的权威记录，Chroma 保存可重建的向量候选。代码向量需要当前仓库先有 SQLite 索引、Embedding 服务可用，再在控制台执行：

```text
/chroma status
/chroma index
/chroma on
```

`/chroma on` 只启用当前控制台运行中的向量检索；`/chroma index` 是同步，不等于启用。源码重新索引后应再次同步 Chroma。向量候选仍须回查索引内容和仓库身份。

长期记忆先由 `/memory suggest` 产生建议，`/memory save 1` 保存待审批项，确认后 `/memory approve <记忆UUID>`，再 `/memory sync`。启用 Chroma 后，相关且有效的已批准记忆才可能进入后续上下文。仓库专属记忆与全局记忆的范围不同；控制台可用 `/memory global list` 查看全局记忆。记忆不是当前源码证据。

## 5. 用隔离测试理解代码

在项目目录执行以下离线测试；它们使用合成数据或临时存储，不需要启动真实模型：

```powershell
$Python = '.\.agentforge\af012-venv\Scripts\python.exe'
& $Python -X utf8 -B -m unittest tests.test_repositories -v
& $Python -X utf8 -B -m unittest tests.test_af011_product -v
& $Python -X utf8 -B -m unittest tests.test_conversation_context -v
```

阅读 `tests.test_af011_product` 中的 `test_unqualified_same_name_symbol_prefers_unique_public_export_and_case_exact_match`，记录：查询是什么、有哪些同名候选、哪一个被包重导出、最终保留哪些证据行。再阅读 `tests.test_af012_b2_conversations` 的摘要失败和游标测试，解释旧摘要为什么在失败时保留。测试替身无法证明真实 PostgreSQL、Chroma 或 Qwen 当前可用。
