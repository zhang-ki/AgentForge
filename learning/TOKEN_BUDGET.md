# 学习专题：模型上下文窗口与 Token Budget

## 2026-11-08 补充：回答预算与能力上限是两回事

本次统一后，CLI 的 `--output-budget` 和交互控制台的 `/output-budget concise|standard|long|max` 只选择请求档位，实际数字由同一个 `ContextBudgetManager` 根据当前模型能力、Provider/Adapter 限制和上下文余量解析。默认 `standard` 请求 16,384 tokens；DeepSeek 在能力信息有效且上限允许时可解析为 16,384，`long` 为 65,536，`max` 才尝试使用该模型已验证的最大能力。Qwen 本地 Adapter 当前最多允许 512，因此不会因为 DeepSeek 能生成长回答就继承其上限。

要特别区分下面这些数字：

| 数值 | 代表什么 | 不代表什么 |
|---|---|---|
| 4,096 | Planner 的结构化输出目标 | 最终回答的统一上限 |
| 512 | 当前 Qwen Adapter 的输出上限；也是部分摘要任务的独立上限 | DeepSeek 普通回答上限 |
| 16,384 / 65,536 | `standard` / `long` 请求档位 | 模型一定能提供的能力；它们仍受已验证能力和 Provider 限制 |
| 2 MB 起、64 MB 封顶 | HTTP 请求/响应的字节安全限制 | Token 数；字节数不能直接换算成精确 Token 数 |

DeepSeek 能力缓存过期时，应用必须先要求刷新模型能力；历史结果中的 1,048,576 上下文和 393,216 输出上限不自动视为当前有效。Thinking 与可见回答使用共享的 `max_tokens`；只有 Provider 返回独立 reasoning usage 时，统计才将其单独列出。测试与本轮覆盖率见 `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_REPORT_20261108.md`。

本篇解释为什么 3,500 字节限制不能直接当成所有模型的 Token 上限，以及 AgentForge 1.2 如何按选定模型计算预算。以下是代码行为讲解；没有本地 tokenizer 文件或真实模型使用量时，估算不会标成精确值。

## 1. 上下文窗口同时容纳输入和输出

模型的 `context_window` 是整次生成可用的总窗口。输入消息和生成的回答都占窗口，因此不能把全部窗口交给输入。项目还需要留出安全余量，以承担聊天模板、协议字段和估算偏差。

```text
input_budget = context_window - output_reserve - safety_margin
```

公式中的数值来自当前模型描述和请求设置。输出预留会限制在模型声明的最大输出范围内；安全余量是单独计算的，不等于可生成的回答长度。

## 2. TokenizerAdapter 和 estimated

`agentforge/token_budget.py` 中的 `TokenizerAdapter` 接受可注入的精确 tokenizer 函数。若未配置，它会按字符与 UTF-8 编码大小取一个偏保守的估算，结果的 `method` 标为 `estimated`。中文、代码标识符、JSON 和模板可能有不同切分方式，这个估算只能用作防止明显超限的保护，不可宣称是模型真实 token 数。

在当前工作区，没有经核实的 Qwen tokenizer 文件，也没有集成 DeepSeek 离线 tokenizer。因此默认两者均为 `estimated`。模型响应提供 `prompt_tokens`、`completion_tokens`、`total_tokens` 等 Usage 时，AgentForge 保存本次 Usage 元数据，作为真实请求计量；它不是实时“剩余窗口”计算器。

## 3. 不只数用户问题

Budget Manager 会序列化并计算：

- system/user/assistant/tool 消息的角色和正文。
- `content: null`、工具调用 ID、工具名称和参数等协议附加字段。
- 顶层请求包装开销。
- Agent 发出的工具 schema。
- 尚待完成的 assistant tool-call 历史和每个工具结果。

其目标是让发送前的检查与真正请求接近。它仍不是模型服务端 tokenizer，所以最终服务返回的 Usage 才是这次请求的实际用量证据。

## 4. 有保护的拒绝

如果估算输入 Token 超过预算，Provider 在 HTTP 请求前失败，记录估算值、窗口、安全余量和输出预留，不自动缩短必要源码、引用 ID、路径或行号。调用点和最终定义等必要角色不能被砍掉后继续伪称完整引用。短缺时应明确拒绝或限定，而不是默默截断证据。

历史规划器仍有独立的 UTF-8 字节上限。这个限制衡量具体序列化上下文的大小，解决的是硬边界与用户偏好；Model Budget 衡量估计 Token 数，解决的是单个 Provider 的上下文窗口。两者不能混称。

## 5. 工具协议为什么也要计数

DeepSeek Agent 的 `tools` schema 会随请求传输；后续 assistant tool_calls 和 tool 结果也会留在下一次请求的消息列表里。只数自然语言消息会低估输入，也可能让模型返回超出上下文窗口。`count_request(..., tools=...)` 将这些结构一并估算。

工具返回仍有自身大小上限，Agent 运行也有时间和请求边界。即使工具数只有两个，schema 和历史调用仍然是上下文的一部分。

## 6. 相关源码阅读顺序

1. `agentforge/model_registry.py`：找 `ModelDescriptor.context_window`、`max_output_tokens`。
2. `agentforge/token_budget.py`：看 `TokenizerAdapter.count_messages()`、`count_request()` 和 `TokenBudgetManager.budget()`。
3. `agentforge/providers/deepseek.py`：看模型请求前如何检查配置、如何计量响应 Usage。
4. `agentforge/providers/deepseek_agent.py`：看 tool schema 和协议历史如何一起计数，超限时如何在调用之前拒绝。
5. `agentforge/presentation.py`：看界面如何区分本次 API Usage 与 `estimated` 输入估算。

## 7. 当前验收限制

自动化测试以固定 tokenizer 函数、合成消息和模拟 HTTP 响应覆盖边界；这些测试不是模型 tokenizer 实测。新的宿主 smoke 只使用合成代码，并要用户明确确认才会发请求；完整的 Qwen/DeepSeek、多仓库、11 个 RAG 案例、长对话和重启验收仍需在普通 Windows 宿主单独完成。
