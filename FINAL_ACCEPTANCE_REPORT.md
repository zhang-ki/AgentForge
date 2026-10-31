# AgentForge 最终落地验收

日期：2026-10-26  
工作区：`D:\agent\AgentForge`

工作区不是 Git 仓库，因而没有可报告的 Git working-tree 状态；本轮没有执行 Git 写操作。

## 结论

第一版只读 RAG MVP 已达到可运行、可复现的离线交付标准；有限步数只读 Agent 工具循环也已实现并通过测试。当前会话设置 `CODEX_SANDBOX_NETWORK_DISABLED=1` 且没有平台支持的 loopback 宿主入口，因此本轮没有再次探测 TCP、访问模型资产、启动模型或发送 HTTP 请求。真实 Qwen 代码问答以及最新 120 查询的四通道 Vector 联合验收仍是环境阻塞，不能记为通过。

产品决定：MVP `PASS`；AF-010 `DEFERRED`；Reranker `DEFERRED`；离线只读 Agent `PASS`，真实模型工具选择 `BLOCKED`。默认 Retriever、RRF 产品配置和生产拒答阈值均未改变。

## 验收矩阵

| 项目 | 总状态 | 代码/离线证据 | 真实运行与产品准入 |
|---|---|---|---|
| 阶段 0：边界、威胁模型、数据保护 | `PASS` | scanner/safeio、秘密排除、路径和资源限制测试通过 | 5 个需 Windows 特权的物理链接测试仍跳过 |
| 阶段 1：只读 RAG MVP | `PASS` | index/search/ask/status、切块、SQLite、引用校验及 OfflineProvider 端到端通过 | 离线 MVP 可交付；不冒充真实模型 |
| 阶段 2：本地 Qwen 聊天 | `BLOCKED` | Provider、认证、超时、代理隔离、重定向拒绝和引用校验 Mock 通过 | 历史 Health/模型元数据通过；一次生成超时，2B-3S 结果未知；没有完整可校验回答 |
| 阶段 3：代码感知索引 | `PASS` | Python AST 符号、引用、稳定 ID、解析失败词法回退通过 | 仅静态 Python 关系，不声称运行时调用 |
| 阶段 4：Vector/混合检索 | `DEFERRED` | Provider、SQLite VectorStore、候选检索和 RRF 原型通过 | AF009GR5/AF010PRE 有历史真实向量证据；最新四通道联合运行受环境阻塞，产品指标不足 |
| 阶段 5：Reranker | `DEFERRED` | 未实现 | 没有经独立 Validation 证明的收益，也无已核验 Cross-Encoder/许可/运行时证据 |
| 阶段 6：有限只读 Agent | `PASS` | 白名单、强类型参数、路径、预算、审计和离线端到端 7/7 通过 | 真实模型工具调用未验收，保持 `BLOCKED` |
| AF-009G | `PASS`（实验） | 评测和失败处理通过 | R5 实际完成 93/93 Embedding；Vector/RRF 未优于 Lexical，拒答失败，不获产品准入 |
| AF-010 | `DEFERRED` | PackageRelation、路由、增量恢复和 5,000 模块合成流水线通过 | PRE 拒答目标失败；S5 最新四通道真实运行受环境阻塞；不产品化 |

状态列与“实现完成、离线测试、真实运行、产品准入”分开记录，详见机器结果 `FINAL_ACCEPTANCE_RESULTS.json`。

## 本轮实现

- 新增 `agentforge/agent_loop.py`：只允许 `index`、`search`、`read`、`list-symbols`。
- 新增 `agent` CLI；仓库根必须显式指定，不提供默认扫描位置。
- 参数字段和类型严格校验；拒绝未知工具、多余字段、路径遍历、秘密文件、跨仓库切换和超过 200 行的读取。
- 最多 8 步，工具与总运行有墙钟预算；失败或超时不重试。工具调用记录来源、参数、状态、耗时、结果或错误。
- 离线规划器固定执行索引、检索、可选符号查询和首条证据读取；最终回答仍由 OfflineProvider 生成。检索文本中的提示指令不能新增工具或权限。
- `index` 只写 AgentForge 固定 SQLite；没有 shell、任意网络、补丁、源仓库写入或子进程工具。

第一次定向运行发现：计划恰好用完允许步数时被错误标记为 `step_budget_exhausted`。修复后只预看是否还有下一步，不执行额外工具；回归测试通过。

## 可交付用法

~~~powershell
cd D:\agent\AgentForge
python -X utf8 -B -m agentforge --help
python -X utf8 -B -m agentforge index --repo <repository>
python -X utf8 -B -m agentforge search "<query>" --top-k 5
python -X utf8 -B -m agentforge ask "<query>"
python -X utf8 -B -m agentforge agent "<query>" --repo <repository> --max-steps 4
python -X utf8 -B -m agentforge status
~~~

`ask` 和 `agent` 默认都是离线行为。`agent` 会主动索引，因此可能更新 AgentForge 自己的固定数据库，但不会写源仓库。切换已持久化索引的仓库必须显式使用 `--replace-repository`。

## 测试证据

实际执行：

~~~powershell
python -X utf8 -B -m unittest tests.test_agent_loop -v
python -X utf8 -B -m unittest tests.test_agent_loop tests.test_cli tests.test_integration tests.test_context tests.test_scanner tests.test_symbols -v
python -X utf8 -B -m unittest discover -s tests -v
python --version
python -X utf8 -B -m agentforge --help
python -X utf8 -B -m agentforge agent --help
~~~

- Agent 首次定向测试：7 运行、4 通过、3 失败、0 跳过，退出码 1；三项均由同一个终态误判造成。修复后重跑 7 运行、7 通过、0 失败、0 跳过，退出码 0。
- 相关定向测试：47 运行、42 通过、0 失败、5 跳过，退出码 0。
- 完整测试：188 运行、183 通过、0 失败、0 错误、5 跳过，退出码 0。
- 5 个跳过项是当前 Windows 权限无法创建文件/目录符号链接、扫描后替换链接、junction 和硬链接；没有计入通过数。
- Python 为 3.12.7；主帮助和 Agent 帮助均退出码 0。

## 真实模型与检索证据边界

- Qwen 聊天：历史上模型加载、loopback 监听、`/health`、`/v1/models` 和认证拒绝/成功曾通过；真实生成一次读取超时，2B-3S 结果未知且没有重试。完整真实回答及引用验收未完成。
- AF009GR5：真实 Qwen3 Embedding 93/93 请求成功、1024 维。Validation 的 Lexical R@1/MRR 为 0.80/1.00，Vector 和 RRF 为 0.75/0.95；0.70 阈值误接受 0.10、误拒绝 0.80。
- AF010PRE：独立 Validation 的 Lexical/Vector/RRF R@1 为 0.80/0.85/0.85；固定规则误接受 0、误拒绝 0.55，未达到预定不高于 0.05/0.25 的目标。
- 最新 S5 的 120 条多仓库数据只完成了离线通道；Vector、四通道 RRF 和余弦阈值未测量，原因是当前执行环境门禁。历史实测不能冒充该新验收。
- 评测标签仍有 `human_reviewed=false` 数据。人工复核入口为 `AF009E_HUMAN_REVIEW.md`，AI 裁定为 `AF009E_AI_ADJUDICATION.md`，两者没有被伪装为已人工批准。

因此 RRF/PackageRelation 保持研究接口，拒答阈值关闭；Reranker 没有足够收益证据，不实现、不安装依赖。

## 完整性与清理

- 按 S5 基线核对 169 个冻结历史、fixture、split、结果和正式数据路径：0 个差异。
- 正式索引：32,768 字节，SHA-256 `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296`。
- 只读证据：2,885 字节，SHA-256 `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733`。
- 测试独立 case 目录已清理；任务前已有的 `tests/.tmp/af009c-llama-server` 保留未动。
- 本轮没有模型进程、端口或运行时副本；没有 HTTP 请求；没有访问 Agentexpr。

## 唯一下一步

唯一外部前置是由平台提供正式支持、允许 loopback 的普通 Windows 宿主执行上下文；不得修改防火墙、沙箱标志、ACL 或安全软件。进入该上下文后只运行现有单一入口：

~~~powershell
cd D:\agent\AgentForge
python -X utf8 -B AF010S2_HOST_RUNNER.py
~~~

预期唯一成功终态：生成新的 `AF010S2_RESULTS.json`，其中 `status=completed`、`state=COMPLETED`，所有必要 Embedding 请求均有计数，自有服务退出、端口释放，受保护哈希不变。当前受限会话不得执行该命令。该入口只完成冻结 120 查询的 Vector/完整 RRF 验收，不会把 RRF、PackageRelation 或阈值自动接入产品；真实 Qwen 聊天回答仍需用户控制的本机服务和独立的一次请求授权。
