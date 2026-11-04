# 学习专题：多模型注册、切换与 Provider 协议

这篇讲解 AgentForge 1.2 分支如何从“一个模型地址”走到“一个会话绑定一种模型”。它介绍当前实现，不代表本地模型、PostgreSQL 或 DeepSeek API 已在本轮真实验收。

## 1. Provider 为什么要统一

上层问答需要完成同一件事：接收带证据的上下文，生成回答，再校验引用。Qwen 和 DeepSeek 的网络协议不同，但不应该让检索、引用和 Answer Contract 分裂成两套业务路径。因此 Registry 用 `provider_id` 和 `model_id` 表达模型身份，Provider 负责具体请求，CLI 和交互层负责显式选择。

主要源码：

- `agentforge/model_registry.py`：模型描述、DeepSeek `/models` 解析、缓存和 Qwen 身份检查。
- `agentforge/providers/llama_server.py`：loopback Qwen 客户端和回答 Provider。
- `agentforge/providers/deepseek.py`：固定 DeepSeek HTTPS 主机、Chat Completions、错误计数、SSE、Usage 与引用校验。
- `agentforge/providers/deepseek_agent.py`：可选的受限只读工具规划器。
- `agentforge/conversation_console.py`：交互命令与用户确认。
- `agentforge/conversation_store.py`、`agentforge/af012_schema.py`：会话模型绑定及持久化。

## 2. “模型存在”与“模型可连接”是两回事

`ModelDescriptor` 记录模型 ID、展示名、上下文窗口、最大输出、Tokenizer ID，以及 Thinking、streaming、tool calling 和模态能力。能力字段可以是 `None`，表示服务没有提供可验证信息。缓存只保存发现信息，不能证明服务当前在线、用户账户有权限或余额可用。

Qwen 是静态的本地配置项，但切换到 Qwen 时仍要检查 `/health` 和运行时暴露的模型 ID。DeepSeek 模型只在显式 `/model refresh` 后从服务端发现。旧模型不能因为曾经写在代码里就被认为仍可用。

## 3. 会话级绑定

`conversations` 表保存 `provider_id`、`model_id`、`thinking_enabled`、`reasoning_effort` 和云端许可时间。迁移 `af012mm_004` 是追加式的；为旧会话填入与旧产品一致的 Qwen 默认值。切换只更新当前会话的模型绑定，不清空消息、摘要或 Memory。

切换云模型会先说明可能发送的数据，然后等待用户确认。用户拒绝时不调用模型发现、不更新数据库绑定。选中云模型之后，CLI 还会核验会话的许可状态；没有许可就阻止请求。Provider 失败不会偷偷回退 Qwen。

## 4. DeepSeek 响应的两类文本

响应可能同时含 `reasoning_content` 和 `content`。前者是服务端协议内容，不是用户答案；最终回答只取 `content`。普通日志保存回答长度、Usage、request ID 和有限诊断，不把 reasoning 正文或 Authorization 头写出来。遇到工具调用时，为了遵守 API 的后续消息协议，当前待完成调用的内部状态会暂存必要的 assistant/tool 消息；它不作为普通回答显示。

工具调用仍然要通过项目原有安全边界：只接受白名单中的只读 `search` 和 `list-symbols`，验证 JSON 和参数 schema，检查工具审计记录、次数、时间和结果大小。模型不能指定 shell、Python 执行、文件写入或网络工具。

## 5. SSE 流式输出

SSE 解析器按行读取 `data:` 事件，分别累积回答、推理片段和工具参数。遇到 `[DONE]` 才算完整；非法 JSON、断流、超时、超大响应和不完整尾帧都会失败关闭。推理片段不传给普通答案回调。界面显示的流式内容属于草稿，最终仍需经过证据引用校验。

请求统计定义事件边界：`attempted` 是客户端即将处理一次模型请求；`sent` 是 HTTP 请求已写出；`succeeded` 是解析得到合法的协议响应；`failed` 记录网络、状态码或响应失败；`retries` 必须为 0。应用层引用错误单独记录，不把协议成功伪装为答案合同成功。

## 6. 交互命令的实际语义

```text
/modellist                         查看本地默认与已缓存模型能力
/model                             查看当前会话的模型绑定
/model refresh                    明确请求最新云端模型元数据
/model <ID>                       选择已发现模型，云端选择需要确认
/model default                    检查并切回 Qwen Local
/thinking on                      仅当当前模型声明支持时启用
/effort high                      仅当当前模型声明支持时发送
/model auth status                查看凭据来源，不显示内容
/model auth set deepseek          隐藏输入并用 DPAPI 保存新密钥
/model auth remove deepseek       删除本机 DPAPI 密钥
/cloud-consent status             查看本会话云端数据许可
/cloud-consent revoke             撤销本会话许可
```

`/modellist` 不主动联网，只显示本地模型与缓存元数据；这是为了避免用户只是查看列表就触发外部请求。注意该列表中的 DeepSeek 状态不等同于连接成功。

## 7. 当前边界

- Qwen Local 仍为默认模型；默认启动不会因为 API key 存在而自动选择 DeepSeek。
- 本机没有对应精确 Tokenizer 时，Budget Manager 标注 `estimated`。
- 离线协议模拟只能验证应用处理，不等于真实 API 可用或模型语义正确。
- 本轮受网络限制，没有执行 DeepSeek discovery、DeepSeek/Qwen 推理或真实数据库/Chroma 联合验收。

下一篇配套讲解：[Token Budget 与上下文窗口](TOKEN_BUDGET.md)。操作步骤见 [模型管理手册](../MODEL_MANAGEMENT.md)。
