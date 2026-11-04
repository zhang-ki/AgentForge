# Day 7｜把一条问答主链路连起来

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 现在从头到尾读一次

前六课各看了一段。现在把它们理解为一条完整请求：用户从 CLI 或桌面控制台输入问题，程序确定活动仓库和会话；检索器选择候选；应用核对候选是否仍属于该仓库和当前文件版本；上下文构造器为证据分配本轮 ID 并安排预算；Provider 生成回答；回答合同检查引用和主张；成功结果最后写入会话存储。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 出错时怎样定位层次

没有答案不一定是模型故障。可能是仓库没索引、路由没选对通道、候选内容过期、证据没有预算空间、Provider 请求失败、响应为空、引用校验失败，或回答虽然引用有效却只得到有限结论。每层有自己的输入和失败标记；要先确定在哪个阶段停止，才知道该看哪份代码。

记住：索引不是回答，候选不是证据，引用有效也不自动等于语义正确。这是后续所有课程的共同基础。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 先核验会话与当前仓库绑定
源码原文：[agentforge/cli.py，第 985–1003 行](../../agentforge/cli.py#L985-L1003)
```python
def _load_conversation(engine, conversation_id, repository):
    from .conversation_store import ConversationStore
    from .conversation_context import ConversationTurn
    from sqlalchemy.exc import SQLAlchemyError
    store = ConversationStore(engine)
    repo_id = _repository_id(repository)
    try:
        # 【学习区补充】用会话与仓库身份共同读取，先挡住跨仓库历史混入。
        store.get_conversation(conversation_id, repo_id)
        rows = store.get_messages(conversation_id, repo_id)
        summary = store.latest_summary(conversation_id, repo_id)
    except SQLAlchemyError:
        raise ValueError("PostgreSQL conversation storage is unavailable or not migrated.") from None
    # 【学习区补充】没有摘要时从序号 0 开始；有摘要则用其游标区分摘要与近期原文。
    covered = summary["covered_through"] if summary else 0
    conversation = {"conversation_id": conversation_id, "repository_id": repo_id,
        "history": tuple(ConversationTurn(conversation_id, repo_id, row["sequence"], row["role"],
                                          row["content"], row["status"])
                         # 【学习区补充】摘要游标前的消息已被摘要覆盖；这里只补入游标之后的近期原文。
                         for row in rows if row["sequence"] > covered),
        "summary": summary["content"] if summary else None}
    return store, repo_id, rows, conversation
```

**逐段讲解。** 加载历史时检查会话的 repository_id；若作用域不一致就失败关闭，而不是悄悄把另一仓库的消息带给模型。

### 2. 用户和助手消息作为一轮原子保存
源码原文：[agentforge/conversation_store.py，第 138–158 行](../../agentforge/conversation_store.py#L138-L158)
```python
    def append_turn(self, conversation_id, repository_id, question, answer, *, model_name=None):
        question = _text(question, "question", 100_000)
        answer = _text(answer, "answer", 100_000)
        if model_name is not None:
            model_name = _text(model_name, "model", 512)
        # 【学习区补充】同一事务写入用户与助手消息；中途失败时整轮一起回滚。
        with self.sessions.begin() as session:
            # 【学习区补充】行锁让并发消息按顺序写入，避免生成重复序号。
            row = self._get(session, conversation_id, repository_id, lock=True)
            if row.status != "active":
                raise ValueError("Cannot append to an archived conversation.")
            highest = session.scalar(select(func.max(Message.sequence_number)).where(
                Message.conversation_id == row.id)) or 0
            # 【学习区补充】两条消息共享同一 turn_id，之后可以还原成一轮问答。
            turn_id = uuid4()
            for offset, role, content in ((1, "user", question), (2, "assistant", answer)):
                session.add(Message(id=uuid4(), conversation_id=row.id, turn_id=turn_id,
                                    sequence_number=highest + offset, role=role,
                                    content=content, request_status="completed",
                                    model_name=model_name if role == "assistant" else None,
                                    message_metadata={}))
            row.last_message_at = row.updated_at = datetime.now(timezone.utc)
            session.flush()
            return str(turn_id)
```

**逐段讲解。** 同一 turn_id 和事务保证轮次完整，序号让恢复顺序确定。

### 3. 先核引用合同再进入保存流程
源码原文：[agentforge/answer_contract.py，第 172–199 行](../../agentforge/answer_contract.py#L172-L199)
```python
def verify_generated_answer(answer: Answer, context: BuiltContext, *, route: str,
                            route_confidence: str, candidate_by_citation=None,
                            uncertainty: str | None = None) -> VerifiedAnswerContract:
    evidence = _contract_evidence(context, candidate_by_citation)
    # 【学习区补充】先把模型提交的证据标签映射回本次请求，模型不能自行指定路径。
    citations = validate_model_citations(answer.text, context)
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
        # 【学习区补充】来源校验不等于语义蕴含证明，因此有引用的回答仍标记为有限结论。
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

**逐段讲解。** 模型给出的 E 标签映射回真实检索证据，再检查 claims 和角色覆盖；通过引用验证不等于语义已被形式证明。

### 4. 持久化是独立于生成和验证的阶段
源码原文：[agentforge/cli.py，第 1099–1105 行](../../agentforge/cli.py#L1099-L1105)
```python
def _persist_conversation(store, arguments, repo_id, answer, provider_mode):
    from sqlalchemy.exc import SQLAlchemyError
    try:
        store.append_turn(arguments.conversation_id, repo_id, arguments.query, answer.text,
                          model_name=arguments.model if provider_mode == "llama-server" else None)
    # 【学习区补充】持久化失败会显式报错，不能让用户以为回答已保存进历史。
    except SQLAlchemyError:
        raise ValueError("Could not persist the completed turn; no successful response was committed.") from None
```

**逐段讲解。** 该 helper 把当前问题、回答文本和可选模型名交给 `append_turn`；当前实现没有把 Answer Contract 对象保存到会话消息中。持久化是否成功要看会话存储调用结果，不能只看回答内容。

## 一轮问答包含多个可独立失败的阶段

一次完整请求先确定会话与仓库，再恢复这段会话的历史，随后检索当前代码证据、构造上下文并调用 Provider。模型返回后，引用验证和 Answer Contract 仍然可能拒绝或限制答案。只有通过这些检查后，程序才把用户问题和助手结果写入 PostgreSQL。数据库事务让消息顺序和本轮状态保持一致，但它并不能把此前的模型请求也回滚；因此日志需要保留“模型答了什么”和“持久化是否成功”两个结果。

从失败定位角度看，应该沿着这些边界逐层检查：没有证据可能是路由或索引问题；Provider 计数为零说明尚未发送请求；HTTP 成功但回答被拒绝，说明问题发生在内容或引用校验阶段；回答显示成功但历史没出现，才应追踪数据库写入。把整条链路拆成阶段状态，比只看最终退出码更容易找出真正原因。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `engine`、`conversation_id`、`repository` | 数据库连接、会话身份和当前仓库身份。 | 三者共同确定加载范围；会话仓库不匹配时不能拿另一仓库历史补充上下文。 |
| `question`、`answer` | 本轮用户输入和经检验后要保存的回答。 | 它们作为一轮消息写入；生成失败或回答未通过合同，不应伪装成已验证回答。 |
| `model_name` | 记录回答使用的模型身份，可选。 | 方便追溯回答来源；它是审计元数据，不会改变已生成的文本。 |
| `route`、`route_confidence`、`uncertainty` | 说明问题走了什么路由、路由置信状态和不确定原因。 | Answer Contract 可将结论限制为有限回答；这些字段不能替代源码证据。 |
| `candidate_by_citation` | 将引用标记映射回候选证据的查找表。 | 映射缺失或不一致时应拒绝该引用，不能信任回答正文中的路径字符串。 |
| `store`、`arguments`、`repo_id`、`provider_mode` | 分别是会话存储、CLI 选项、仓库身份和 Provider 模式。 | 持久化阶段据此保存正确会话及来源信息；Provider 模式本身不代表请求一定成功。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
