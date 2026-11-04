# Day 27｜沿一次请求解释整个项目

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 为什么从用户问题讲架构

目录清单只能告诉听者项目有很多模块；请求流能说明这些模块为何存在。以“某函数做什么”为例：先确认当前仓库和索引，再按意图找符号或文本证据，核验路径和原文，结合会话上下文把证据交给 Provider，检查模型引用，最后保存成功对话。每个模块出现都有明确理由。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 要诚实说明的边界

项目具备符号检索，不等于所有名字歧义都已解决；有 PackageRelation，不等于动态运行关系已证明；有 Chroma，不等于向量永远优于词法；有引用合同，也不等于所有自然语言蕴含都能自动验证。历史验收只支持它实际包含的输入、模型和配置。若准备面试，可额外阅读 [INTERVIEW.md](../INTERVIEW.md)，但它不是课程要求。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 追问把历史代词解析成当前源码查询
源码原文：[agentforge/cli.py，第 731–741 行](../../agentforge/cli.py#L731-L741)
```python
def _conversation_retrieval(outcome, retriever, conversation, arguments, store, rows):
    symbol = None
    missing_named_symbol = False
    # 【学习区补充】仅在会话追问被识别为符号查询时，才从历史中补回省略的符号名。
    if conversation is not None and outcome.decision.route == "exact_symbol" and _is_followup(arguments.query):
        named = retriever._symbol_name(arguments.query)
        missing_named_symbol = bool(named and not store.search_symbols(name=named, limit=1))
    # 【学习区补充】明确查询不被旧话题改写；只在低置信或符号缺失时使用历史消歧。
    if conversation is not None and (outcome.decision.confidence == "low" or missing_named_symbol):
        # 【学习区补充】先取当前问题中的唯一符号；找不到时再从近期对话解析代词指向。
        symbol = _unique_mentioned_symbol(arguments.query, store) or _followup_symbol(arguments.query, rows, store)
        if symbol:
            outcome = retriever.search(symbol, top_k=arguments.top_k)
    return outcome, symbol
```

**逐段讲解。** 会话让系统辨认“它”指什么，随后仍通过当前仓库检索符号证据；历史不是源码证明，也不能绕过仓库范围。

### 2. 请求开头恢复会话和仓库作用域
源码原文：[agentforge/cli.py，第 985–1003 行](../../agentforge/cli.py#L985-L1003)
```python
def _load_conversation(engine, conversation_id, repository):
    from .conversation_store import ConversationStore
    from .conversation_context import ConversationTurn
    from sqlalchemy.exc import SQLAlchemyError
    store = ConversationStore(engine)
    repo_id = _repository_id(repository)
    try:
        store.get_conversation(conversation_id, repo_id)
        rows = store.get_messages(conversation_id, repo_id)
        summary = store.latest_summary(conversation_id, repo_id)
    except SQLAlchemyError:
        raise ValueError("PostgreSQL conversation storage is unavailable or not migrated.") from None
    covered = summary["covered_through"] if summary else 0
    conversation = {"conversation_id": conversation_id, "repository_id": repo_id,
        "history": tuple(ConversationTurn(conversation_id, repo_id, row["sequence"], row["role"],
                                          row["content"], row["status"])
                         # 【学习区补充】恢复时跳过摘要已覆盖的消息，避免同一历史重复占用预算。
                         for row in rows if row["sequence"] > covered),
        "summary": summary["content"] if summary else None}
    return store, repo_id, rows, conversation
```

**逐段讲解。** 加载器返回会话与顺序消息，并确认 repository_id 匹配。错误在生成前出现，避免错误历史污染回答。

### 3. 回答阶段之后单独持久化
源码原文：[agentforge/cli.py，第 1099–1105 行](../../agentforge/cli.py#L1099-L1105)
```python
def _persist_conversation(store, arguments, repo_id, answer, provider_mode):
    from sqlalchemy.exc import SQLAlchemyError
    try:
        store.append_turn(arguments.conversation_id, repo_id, arguments.query, answer.text,
                          model_name=arguments.model if provider_mode == "llama-server" else None)
    # 【学习区补充】数据库保存失败必须显式返回错误，不能把未持久化的一轮算作成功。
    except SQLAlchemyError:
        raise ValueError("Could not persist the completed turn; no successful response was committed.") from None
```

**逐段讲解。** 只在完成合同处理后保存问题、回答及元数据；“屏幕打印了”与“写入 PostgreSQL”是不同事实。

### 4. 全链路最后要说明保证的边界
源码原文：[agentforge/answer_contract.py，第 172–199 行](../../agentforge/answer_contract.py#L172-L199)
```python
def verify_generated_answer(answer: Answer, context: BuiltContext, *, route: str,
                            route_confidence: str, candidate_by_citation=None,
                            uncertainty: str | None = None) -> VerifiedAnswerContract:
    evidence = _contract_evidence(context, candidate_by_citation)
    citations = validate_model_citations(answer.text, context)
    # 【学习区补充】引用属于当前请求且确有证据才继续；否则进入拒答合同。
    if not evidence or not citations:
        contract = VerifiedAnswerContract(
            ANSWER_CONTRACT_VERSION, "refused", answer.text,
            "Generated text did not contain a request-local verifiable citation.",
            (AnswerClaim("模型回答缺少可验证引用，不能作为有证据答案。", "unknown", ()),),
            evidence, route, route_confidence,
        )
    else:
        _validate_required_role_coverage(context.question, evidence, citations)
    # 引用属于证据集合只能证明来源，不代表语义蕴含成立。
    # 在具备独立蕴含检查前，生成内容只能标记为有限结论。
        kind = "inference"
        status = "limited"
        contract = VerifiedAnswerContract(
            ANSWER_CONTRACT_VERSION, status, answer.text,
            uncertainty or (
                "Citations are request-local; semantic entailment has not been verified."
            ),
            (AnswerClaim(answer.text, kind, citations),),
            evidence, route, route_confidence,
        )
    validate_answer_contract(contract, context)
    return contract
```

**逐段讲解。** 引用身份、证据角色和合同状态可以验证；任意自然语言的语义蕴含不一定可自动证明。

## 用一个具体问题串起模块责任

假设用户追问“它的第二个参数是什么”。CLI 先从当前会话恢复前文指代，再用当前活动仓库重新查找符号；历史消息帮助理解“它”指什么，却不是本轮源码证据。检索器产出当前定义，context builder 决定哪些证据、摘要和近期消息进入预算，Provider 收到结构化的请求消息后才调用 Qwen。

回答返回后，应用把 `[E1]` 映射到当次真实证据，生成路径和行范围，并交给 Answer Contract 标注事实、推断或未知。通过验证后，消息才进入 PostgreSQL。这个顺序解释了为什么会话历史可以恢复，但旧引用不能跨请求复用；也解释了为什么回答格式正确之后仍需看语义是否符合源码。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `outcome`、`retriever` | 初步路由结果和当前仓库的检索器。 | 前者说明应走什么路径，后者实际查询索引；路由成功不代表检索命中。 |
| `conversation`、`arguments` | 当前会话背景和 CLI 本次命令选项。 | 它们决定历史和预算等上下文；会话必须与活动仓库身份匹配。 |
| `store`、`rows` | 当前索引访问接口和已加载数据。 | 它们提供本轮检索来源，不能混用另一仓库数据库。 |
| `engine`、`conversation_id`、`repository` | PostgreSQL 连接、目标会话与当前仓库。 | 用来恢复正确历史并验证会话作用域。 |
| `answer`、`context`、`route`、`route_confidence` | 生成结果、当前证据和检索状态。 | 共同决定 Answer Contract 的类别与限制；任何一项都不能单独证明语义正确。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
