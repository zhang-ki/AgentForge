# AgentForge 多模型感知型长上下文管理专项报告

日期：2026-11-06  
分支：`feature/1.1-core`  
总体状态：**PARTIAL**

## 结果摘要

已完成离线实现和回归：上下文预算由当前模型的 `ModelDescriptor` 驱动；Qwen 与 DeepSeek 不再共用固定 4,096 Token 窗口；持久化会话记录与本次请求组装分开；请求压缩不会删除原始消息；模型切换会重新组装请求视图；超限时在发送前返回 `CONTEXT_BUDGET_EXCEEDED`。Planner 和最终回答保存各自的请求预算与 Usage，工具执行本身是本地调用，不单独消耗模型 Token。

当前本机 DeepSeek 能力缓存为 **stale**，年龄 154,766 秒，超过 86,400 秒有效期；Flash 与 V4 Pro 条目的 `capability_source` 和 `capability_verified_at` 均为空。缓存里可见的数字因此不用于预算或发送请求；需要在 AgentForge 内执行显式 `/model refresh` 后才能选择和调用 DeepSeek。本轮没有刷新远端元数据，也没有发送 Qwen 或 DeepSeek 请求。

## 模型能力矩阵

| 字段 | Qwen Local | DeepSeek Flash / V4 Pro（当前本机状态） |
|---|---|---|
| `context_window` | 4,096；来自 `launchers/start-qwen.ps1 --ctx-size 4096` 配置。未对当前服务做健康或窗口探测。 | 未验证。当前缓存为 stale 且无字段来源/验证时间；请求被拒绝。 |
| `max_input_tokens` | 未知。 | 未知；需新鲜 Provider 元数据提供。 |
| 模型 `max_output_tokens` | 未知。 | 未验证。官方 `/models` 文档示例给出 1,048,576 窗口及 393,216 输出上限，但示例不是本机账号实时结果。 |
| Provider / AgentForge 输出上限 | Provider 模型上限未知；AgentForge adapter ceiling 512，普通请求默认使用 256。 | Provider 能力须刷新后确认；AgentForge adapter ceiling 512，单次请求默认使用 256。 |
| Tokenizer | 未配置；Unicode/UTF-8 保守估算，明确标记为估算。 | 未知；不按模型名称推断。 |
| `reasoning_token_limit` | 未知/不适用；Qwen adapter 当前不启用 Thinking。 | 未知；能力缺失时保持 `None`。Usage 有 `reasoning_tokens` 时与回答 Token 分开记录。 |
| Streaming / Thinking / Tool Calling | AgentForge 当前本地 adapter 均不支持。 | 当前缓存能力未经验证。缓存刷新并返回能力字段前视为未知，禁止启用相应参数。 |
| 能力来源 / 验证时间 | 启动器参数与 adapter 配置，记录日期 2026-11-06；不是实时服务证明。 | 当前本机缓存缺失。刷新成功后记录官方 `/models` 来源与拉取时间。 |

DeepSeek 官方 [`/models` 文档](https://api-docs.deepseek.com/api/list-models/)说明 `context_window` 包含输入和输出，`max_output_tokens` 是一次响应可接受的上限，而且部分能力字段可能缺失。其当前示例中的数值只用于理解字段格式，不能代替本机刷新结果。官方 [Chat Completions 文档](https://api-docs.deepseek.com/api/create-chat-completion/)说明 `max_tokens` 是本次生成上限且输入加输出受总窗口限制；[Thinking Mode 文档](https://api-docs.deepseek.com/guides/thinking_mode/)说明推理内容通过独立 `reasoning_content` 返回。

## 请求预算和组装

`ModelRegistry` 为每个模型提供不可互换的能力描述；`ContextBudgetManager` 接收描述并计算单次请求预算；`ContextAssembler` 根据预算生成消息列表。Provider 执行请求，不负责选择窗口或压缩历史。

计算规则：

```text
输入预算 = min(上下文窗口 - 本次请求输出预留 - 安全余量,
               已知 Provider 输入上限（若有），
               用户显式输入上限（若有）)
```

目前默认安全余量为上下文窗口的 10%，最低 128 Token。每次请求使用自己请求的输出预留；模型允许的最大输出仅作为 ceiling，不会被当作本次必须使用的输出长度。Qwen 配置窗口为 4,096 时，默认请求输出 256、余量 410，输入预算为 3,430 Token（使用估算计数时该数只是预算单位，不是精确 Token）。仅用于公式演示：若假设官方 `/models` 示例中的 1,048,576 窗口可用，且请求输出 256，算法会给出 943,462 的输入预算；当前本机缓存未经验证，所以这个演示值没有用于任何真实请求。

当模型专属 Tokenizer 被本地注册时按其计算；当前两个模型均没有注册 Tokenizer。后备估算按 `max(Unicode 字符数, ceil(UTF-8 字节数 / 2))` 加消息角色、协议格式开销，并在诊断中标为 `conservative_estimated`。字节值不会冒充 Token 数。

`ContextAssembler` 只生成当前模型请求视图。若完整消息适合窗口，保留完整历史并抑制重复摘要；不适合时按完整问答轮次选择源链接摘要和近期消息，不切断消息正文。必需证据/问题仍超限时不调用 Provider，返回 `CONTEXT_BUDGET_EXCEEDED`。模型切换只重新组装视图，持久消息、摘要和 Memory 不被清除。

Planner 的 `fit_messages` 把工具 Schema、工具调用协议和历史观察一并计入预算；计划请求与 Replan 请求分别记预算和实际 Usage，保存在 Agent 执行检查点。执行器调用的是本地受限工具，不是模型请求；大工具输出通过结果引用/分页供后续 Planner 使用。最终回答的预算、估算方法和真实 Provider Usage 作为有界、无 Prompt 正文的消息元数据保存。DeepSeek 若返回推理 Token，则独立记录 `reasoning_tokens`；Provider 的单个 `max_tokens` 仍是总生成上限，当前元数据没有独立 reasoning 上限时系统不会猜测。

## 测试与证据

### 定向回归

- 命令：`python -X utf8 -B -m unittest tests.test_multimodel tests.test_cli tests.test_conversation_console tests.test_context_assembler tests.test_providers tests.test_session_service tests.test_af012_b2_conversations tests.test_agent_execution -v`
- 结果：197 项运行、197 通过、0 失败、0 错误、0 跳过；退出码 0。
- 日志：`MODEL_AWARE_CONTEXT_TARGETED_20261106_FINAL.log`。

### 完整离线回归

- 命令：`python -X utf8 -B -m coverage run --branch --source=agentforge --data-file=.coverage.model-aware-context-20261106-final2 -m unittest discover -s tests -v`
- 结果：635 项运行、629 通过、0 失败、0 错误、6 跳过；退出码 0。跳过项未计入通过数。
- 跳过项及原因：
  - `test_directory_symbolic_link_escape_and_link_root`：WinError 1314，当前账户无目录符号链接创建权限。
  - `test_file_symbolic_link_escape`：WinError 1314，当前账户无文件符号链接创建权限。
  - `test_hard_link_added_after_scan_is_rejected_before_read`：WinError 5，当前账户无硬链接创建权限。
  - `test_hard_link_is_excluded`：WinError 5，当前账户无硬链接创建权限。
  - `test_link_substitution_after_scan_is_rejected`：WinError 1314，当前账户无链接替换权限。
  - `test_windows_junction_escape_and_opened_handle_boundary`：WinError 5，当前账户无 Junction 创建权限。
- 完整日志：`MODEL_AWARE_CONTEXT_FULL_TESTS_20261106_FINAL2.log`。

### 全生产包覆盖率

- 工具：coverage.py 7.17.0；Python 3.12.7。
- 测量范围：显式 `--source=agentforge --branch`，共 79 个生产模块；没有按低覆盖模块缩小统计范围。项目 `pyproject.toml` 没有 coverage omit/exclude 配置。
- 行语句：12,052 总数，10,449 已覆盖，1,603 未覆盖，**86.6993%**。
- 分支：4,640 总数，3,576 已覆盖，1,064 未覆盖，**77.0690%**。
- 覆盖率门槛状态：既定 90% 行 / 85% 分支目标均未达成。本专项不为覆盖率写空测试，也不更改分母。
- 原始覆盖率文件：`.coverage.model-aware-context-20261106-final2`；JSON：`MODEL_AWARE_CONTEXT_COVERAGE_20261106_FINAL2.json`；文本：`MODEL_AWARE_CONTEXT_COVERAGE_20261106_FINAL2.txt`。

### 模型窗口和模型切换用例的边界

离线用例实际使用 Qwen Launcher 中 4,096 的配置，覆盖 30 轮压缩和 100 轮请求视图、必需内容超限、安全停止、Tokenizer 注册、Usage 字段及 DeepSeek→Qwen→DeepSeek 重新组装后原始消息不变。DeepSeek 大窗口数学用例使用官方 `/models` 文档示例构造描述符，仅验证预算算法如何响应给定能力值；它**不是**实际 API 元数据读取或真实 DeepSeek 100 轮验收。当前缓存因过期且无来源而被 fail closed。

## 真实宿主状态与限制

本轮没有启动或停止任何服务，没有发送 Qwen、DeepSeek、Embedding 请求，也没有访问 PostgreSQL 或持久化 Chroma。因此以下均为 **NOT_MEASURED**：

- DeepSeek 官方 `/models` 的本机实时刷新、账号实际可用模型及能力。
- Qwen 当前运行进程真实窗口、真实模型 Tokenizer 及 30/100 轮聊天行为。
- DeepSeek 实际长上下文对话与真实模型切换。
- PostgreSQL 服务中的消息历史恢复以及 Chroma 联合上下文。
- DeepSeek Streaming / Thinking 实际 Token Usage 和服务限制。

本轮未执行数据库迁移、未请求模型服务、未更改正式索引。没有记录覆盖正式数据的独立前后哈希清单，因此“全量保护数据哈希审计”状态是 **NOT_MEASURED**，不作完整性 PASS 声明。当前 DeepSeek 用户体验上的下一步是由用户在 AgentForge 交互环境显式执行 `/model refresh`，之后再安排授权的真实宿主长上下文验收。

## 交付判定

| 门禁 | 状态 |
|---|---|
| 离线实现与预算/历史边界回归 | PASS |
| 完整离线测试 | PASS（6 项平台权限跳过，不计通过） |
| 全生产包覆盖率目标 | PARTIAL（86.6993% 行、77.0690% 分支，未达 90%/85%） |
| 当前 DeepSeek 能力可信度 | PARTIAL（缓存陈旧、无 provenance；使用已阻止） |
| 真实 Qwen/DeepSeek/PostgreSQL/Chroma 长上下文验收 | NOT_MEASURED |
| 本专项总体 | **PARTIAL** |

机器结果见 [`MODEL_AWARE_CONTEXT_RESULTS_20261106.json`](MODEL_AWARE_CONTEXT_RESULTS_20261106.json)。
