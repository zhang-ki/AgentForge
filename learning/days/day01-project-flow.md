# Day 1｜AgentForge 解决什么问题

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 先把问题说清楚

普通聊天模型回答“某个函数做什么”时，依赖的是模型训练时学到的知识和当前提示词。它看不到你电脑里的代码，也不知道你刚切换到哪个仓库，更不能保证记得的是当前版本。AgentForge 要解决的是：针对用户选定的本地代码仓库找到当前源码，把可追溯的片段交给模型，再检查回答引用的确是这次找到的证据。

这就是 RAG（检索增强生成）的基本思想：先检索外部资料，再根据资料生成回答。AgentForge 的“外部资料”不是云端知识库，而是本地仓库的索引与源码。检索让模型有材料可看；引用和回答合同则约束模型怎样表达结论。它不能保证模型每句话都正确，所以系统仍须明确表示证据不足或语义未完全验证。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 把第一张心智图记下来

一次问答可以先理解成：

```text
本地仓库 → 索引 → 查询 → 候选证据 → 有限上下文 → Provider → 引用与回答校验
```

`search` 停在“候选证据”；`ask` 才继续走向回答。默认 Provider 可以是离线实现，只有显式选择本地聊天 Provider 才是真实模型生成。区分这些阶段，是读懂后面代码和验收记录的基础。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 入口如何选择真实模型或离线实现
源码原文：[agentforge/cli.py，第 55–89 行](../../agentforge/cli.py#L55-L89)
```python
def create_provider(
    provider_name: str = "offline",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    connect_timeout: float = 2.0, read_timeout: float = 60.0,
    total_timeout: float = 180.0, max_output_tokens: int = 256,
):
    """创建用户明确选择的 ask Provider；构造客户端本身不会发起请求。"""
    if provider_name == "offline":
        # 【学习区补充】离线 Provider 不接受真实服务参数，避免配置看似成功、实际却没有调用模型。
        if (base_url is not None or model is not None or connect_timeout != 2.0
                or read_timeout != 60.0 or total_timeout != 180.0 or max_output_tokens != 256):
            raise ValueError(
                "llama-server endpoint, model, timeout and output-limit options require --provider llama-server."
            )
        return OfflineProvider()
    # 【学习区补充】Provider 名称采用白名单；拼错或未知值不会自动回退到离线模式。
    if provider_name != "llama-server":
        raise ValueError("Provider must be offline or llama-server.")
    if base_url is None or not base_url.strip():
        raise ValueError("--base-url is required for --provider llama-server; no endpoint is guessed.")
    if model is None or not model.strip():
        raise ValueError("--model is required for --provider llama-server.")
    # 【学习区补充】密钥只在明确使用本地聊天服务时必需；缺失时在发请求前报配置错误。
    if api_key is None or not api_key.strip():
        raise ValueError("AGENTFORGE_LLAMA_API_KEY is required for --provider llama-server.")
    try:
        # 【学习区补充】这里只创建客户端并校验配置；真正的 HTTP 请求发生在后续 answer 调用。
        client = LlamaServerClient(base_url, api_key=api_key, connect_timeout=connect_timeout,
                                   read_timeout=read_timeout, total_timeout=total_timeout,
                                   max_output_tokens=max_output_tokens)
    except ValueError as exc:
        if "API key" in str(exc):
            raise ValueError(f"Invalid AGENTFORGE_LLAMA_API_KEY: {exc}") from None
        if "timeout" in str(exc).lower():
            raise ValueError(str(exc)) from None
        raise ValueError(f"Invalid --base-url for llama-server: {exc}") from None
    return LlamaServerProvider(client, model.strip())
```

**逐段讲解。** 读参数分支和返回值：Provider 的选择是显式配置边界。离线回答与真实模型调用都能返回 Answer，但只有后者会走本地 HTTP 服务。

### 2. 索引命令把仓库送入索引流程
源码原文：[agentforge/cli.py，第 593–614 行](../../agentforge/cli.py#L593-L614)
```python
def _index_command(arguments):
    from .repositories import RepositoryRegistry, canonical_repository, open_index_for_repository, repository_identity
    from .chunker import chunk_signature
    chunk_signature(arguments.window_lines, arguments.overlap_lines)
    try:
        # 【学习区补充】先规范化并验证仓库路径，再用同一身份打开它的索引。
        root = canonical_repository(arguments.repo)
    except ValueError as exc:
        from .errors import ScanError
        raise ScanError(str(exc)) from None
    repo_id = repository_identity(root)
    with RepositoryRegistry() as registry:
        item = registry.register(root)
        index = open_index_for_repository(root, readonly=False)
        try:
            # 【学习区补充】索引命令把扫描、切块、解析和写库委托给索引器，CLI 本身不实现这些步骤。
            result = index_repository(root, index, window_lines=arguments.window_lines,
                overlap_lines=arguments.overlap_lines, replace_repository=arguments.replace_repository)
            registry.set_index_path(repo_id, index.database)
        # 【学习区补充】无论索引成功还是报错，都关闭 SQLite 连接，避免句柄泄漏。
        finally:
            index.close()
        registry.mark_indexed(repo_id)
        registry.activate(repo_id)
    return {**result, "repository_id": repo_id, "active": True}
```

**逐段讲解。** 这个入口解析仓库与索引选项，委托给 indexer，最后报告文件/块数量。索引准备与 ask 回答分开，所以“已建立索引”不等于“已经产生答案”。

## 把入口与后续阶段连起来

从用户视角看，`index` 和 `ask` 都是命令；从数据流看，它们是两条不同的路径。`index` 负责读取授权仓库、解析源码并更新 SQLite 快照，回答问题时不会顺便重建整个索引。`ask` 则读取当前仓库身份和已有索引，先准备检索结果，再按配置选择离线或本地聊天 Provider。把二者分开，能避免每次提问都重复扫描，也让“代码已经入库”和“模型已经回答”成为两个可以分别检查的状态。

Provider 的创建与 Provider 真正发请求也要分开理解。配置阶段会验证选择了哪个实现、地址和凭证是否合法；只有后续进入回答方法，真实聊天客户端才发送 HTTP 请求。证据不足时可能在请求模型之前就返回拒答，所以看到 `provider=llama-server` 的配置并不能单独证明本次请求已经发送。请求统计、HTTP 状态和回答合同分别提供不同阶段的证据。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `provider_name` | 选择离线实现或本地 llama-server 实现。 | 只选择 Provider；选择本地服务后还要通过配置校验，真正请求发生在后续回答阶段。 |
| `base_url`、`model`、`api_key` | 分别指定服务地址、服务端识别的模型名和认证凭证。 | 地址或模型名不匹配会导致请求失败；凭证不能写入源码或日志。 |
| `connect_timeout`、`read_timeout`、`total_timeout` | 分别限制建立连接、等待响应数据和整次操作的时间。 | 本地模型可能生成较慢，读取等待通常长于连接等待；总时间仍负责给整次调用设上限。 |
| `max_output_tokens` | 限制模型最多生成多少 Token。 | 太小可能让回答被截断，太大则增加等待时间和上下文资源消耗。 |
| `arguments` | CLI 解析出来的一组命令选项，例如仓库路径和索引模式。 | 索引命令通过它读取用户选项，不应把整个参数对象误当成仓库内容。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
