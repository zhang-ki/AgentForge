# Day 28｜形成 AgentForge 的完整心智图

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 四层主链路

把 AgentForge 记成四层：

1. **本地数据层**：扫描仓库、切块、建立每仓库独立 SQLite 索引；PostgreSQL 保存会话和 Memory 的权威状态；Chroma 提供可重建向量候选。
2. **检索层**：产品路由根据问题选择符号、PackageRelation、词法和可用向量通道；来源核验确保候选仍对应当前仓库和内容。
3. **上下文与生成层**：读取当前会话摘要和最近历史，检索适用的获批 Memory，把当前问题与必要代码证据放入预算内，再由 Provider 返回答案。
4. **回答与状态层**：Evidence ID 映射到来源，Answer Contract 区分回答、限定和拒答；成功问答再持久化到会话。

安全和评测横跨四层：路径和工具权限限制程序能访问什么；评测数据限制我们能对质量作出什么声明。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 最重要的三个区别

- **索引与模型记忆**：索引是外部当前数据，模型参数不是本地仓库的权威来源。
- **候选与证据**：搜索/向量结果要核验身份和原文后，才能成为证据。
- **证据有效与语义正确**：引用确实指向源码，不代表回答推论一定成立。

理解这三点，就掌握了读 AgentForge 源码时最重要的判断方式：先看数据从哪里来，再看代码怎样传递它，最后确认程序对结论作了什么程度的保证。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 仓库身份选择后续索引与检索范围
源码原文：[agentforge/repositories.py，第 228–242 行](../../agentforge/repositories.py#L228-L242)
```python
def open_index_for_repository(path: str | os.PathLike[str], *, readonly: bool = False):
    """打开仓库专属索引；仅当旧版索引确属该仓库时才复用。"""
    from .store import IndexStore

    root = canonical_repository(path)
    repo_id = repository_identity(root)
    dedicated = RepositoryRegistry.index_path(repo_id)
    # 【学习区补充】优先打开仓库专属索引，切换仓库时不会把另一仓库 SQLite 当作当前数据。
    if dedicated.exists() or dedicated.is_symlink():
        return IndexStore(dedicated, readonly=readonly)
    if DATABASE_PATH.exists() or DATABASE_PATH.is_symlink():
        with IndexStore(DATABASE_PATH, readonly=True) as legacy:
            owner = legacy.metadata().get("repository")
        # 【学习区补充】兼容旧索引时也核验所有者，归属不一致就使用专属数据库。
        if owner and os.path.normcase(owner) == os.path.normcase(str(root)):
            return IndexStore(DATABASE_PATH, readonly=readonly)
    return IndexStore(dedicated, readonly=readonly)
```

**逐段讲解。** 活动仓库解析到独立索引，向量和会话也按 repository_id 限定，切换并不覆盖其它仓库。

### 2. 模型上下文仍保留多种数据来源
源码原文：[agentforge/conversation_context.py，第 53–63 行](../../agentforge/conversation_context.py#L53-L63)
```python
class ContextPlan:
    messages: tuple[tuple[str, str], ...]
    bytes_used: int
    dropped_turns: int
    memory_ids: tuple[str, ...] = ()
    summary_included: bool = False
    protected_fact_ids: tuple[str, ...] = ()
    protected_fact_source_message_ids: tuple[str, ...] = ()
    retained_history_sequences: tuple[int, ...] = ()
    # 【学习区补充】按组成部分记录字节用量，帮助定位超预算来源，不必把正文写进诊断日志。
    byte_diagnostics: tuple[tuple[str, int], ...] = ()
    omitted_fact_count: int = 0
```

**逐段讲解。** 摘要、近期消息、Memory、偏好及诊断分别记录，便于解释本轮实际送入模型的背景。

### 3. 生成处在检索与校验之间
源码原文：[agentforge/providers/llama_server.py，第 134–166 行](../../agentforge/providers/llama_server.py#L134-L166)
```python
    def answer(self, context: BuiltContext, *, conversation: dict | None = None) -> Answer:
        # 【学习区补充】无证据分支返回离线拒答；不能把它误读为已经请求聊天模型。
        if not context.evidence:
            return OfflineProvider().answer(context)
        text = self.client.chat(_conversation_messages(context, conversation, [
            {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Question:\n{context.question}\n\nUntrusted evidence:\n"
                                        f"{_model_evidence_text(context)}\n\nUse only these evidence IDs: "
                                        f"{', '.join('[' + source.source_id + ']' for source in context.evidence)}. "
                                        "Cite the applicable IDs."},
        ]), model=self.model)
        # Provider 边界合同：
        #
        # HTTP 客户端返回的是不可信模型文本。
        # 它不会将该文本升级为可信证据。
        # 它不会从生成内容中推断权限。
        # 它不会执行模型提到的工具。
        # 引用校验仅对当前请求有效。
        # 每个标记都必须映射到当前 BuiltContext。
        # 路径、行号和摘录均从本次请求上下文中解析，而不是从
        # 生成文本。有效 ID 与无效 ID 混杂时，整个结果都会失败。
        # 随后由独立的回答合同层检查来源与状态；
        # 仅有格式正确的 ID，并不能证明语义蕴含关系成立。
        # 在 Provider 和产品边界保留失败信息，不进行回退。
        # 这是将模型文本转换为在线 Answer 的唯一入口；
        # 必须先完成引用校验。
        try:
            # 【学习区补充】校验把模型引用映射回本轮证据，路径和行号由应用掌握。
            citations = validate_model_citations(text, context)
        except CitationValidationError as exc:
            recorder = getattr(self.client, "record_application_failure", None)
            if callable(recorder):
                recorder("citation_validation", exc.code, exc.diagnostic)
            raise
        return Answer(text, citations, False)
```

**逐段讲解。** Provider 接收已经构造的上下文并请求 Qwen；上游负责选证据，下游仍需检查引用合同并保存消息。

### 4. 持久化完成系统闭环
源码原文：[agentforge/conversation_store.py，第 138–158 行](../../agentforge/conversation_store.py#L138-L158)
```python
    def append_turn(self, conversation_id, repository_id, question, answer, *, model_name=None):
        question = _text(question, "question", 100_000)
        answer = _text(answer, "answer", 100_000)
        if model_name is not None:
            model_name = _text(model_name, "model", 512)
        # 【学习区补充】一轮消息在单个事务里写入，保证用户问题和助手回答一起成功或回滚。
        with self.sessions.begin() as session:
            row = self._get(session, conversation_id, repository_id, lock=True)
            # 【学习区补充】归档会话不能再追加消息，避免状态变化后仍被当作当前对话。
            if row.status != "active":
                raise ValueError("Cannot append to an archived conversation.")
            highest = session.scalar(select(func.max(Message.sequence_number)).where(
                Message.conversation_id == row.id)) or 0
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

**逐段讲解。** 一轮用户/助手消息按会话和顺序写入 PostgreSQL，重启后恢复时再检查仓库绑定，防止历史串用。

## 用数据的权威来源理解整套架构

问答流程围绕几种不同的数据身份运行：SQLite 是当前仓库代码快照和静态符号关系的权威来源；PostgreSQL 保存会话、消息和记忆生命周期；Chroma 保存可重建的向量候选。索引和向量提供“在哪里可能相关”的线索，当前源码及关系记录才决定候选能否成为证据。Provider 生成的语言文本则仍是不可信输出，必须通过请求局部引用映射和回答合同。

当问题需要追踪调用关系时，路由可以选择结构关系通道；普通概念问题可走词法检索，向量通道只有在已配置和启用时参加。上下文规划把当前问题、必要证据和会话背景按预算组装，完成生成后再持久化消息。理解这个顺序后，可以把故障分成仓库/索引、路由/检索、上下文、模型、引用合同和数据库六类，不必把所有错误都归结为“模型答错了”。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `path`、`readonly` | 目标仓库路径及索引是否以只读模式打开。 | 切换 `path` 选择另一仓库；只读模式避免搜索过程意外写索引。 |
| `ContextPlan.question`、`evidence`、`messages` | 当前问题、实际选中证据和最终消息序列。 | 规划结果明确哪些内容进入模型；被检索但未选中的候选不会自动成为引用来源。 |
| `summary`、`history`、`memories` | 较早会话背景、近期消息和获批记忆。 | 都受上下文预算与作用域过滤；它们是背景，不取代本轮代码证据。 |
| `conversation` | Provider 可选接收的当前会话数据。 | 没有时是单轮请求；有时要验证会话和仓库绑定并构造有界多轮消息。 |
| `conversation_id`、`repository_id`、`question`、`answer` | PostgreSQL 中一轮消息所属会话、仓库、用户问题和助手回答。 | 共同确定持久化归属和顺序，确保恢复时不会串到其他仓库或会话。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
