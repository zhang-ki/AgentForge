# Day 25｜怎样理解一项代码改动

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 从用户可见问题追到责任层

“答案没有引用”是现象，不是根因。可能是路由没找到证据、上下文预算没装入证据、模型没有遵循请求内 ID，或者引用解析器拒绝了格式。改错层会造成新的漏洞：如果问题是模型输出身份无效，关闭校验虽然让文本显示出来，却失去证据保证。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 为什么要保留失败证据

修复应针对可复现的失败输入和明确根因；历史报告、原始结果与新结果要分开。改变 Validation 标签、降低合同要求或把失败计数改成零，都会破坏对比。阅读一项改动时，重点看它改变了哪个行为、维持了哪些合同、有没有给相邻失败路径带来影响。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 摘要更新要携带已有摘要与事实
源码原文：[agentforge/conversation_store.py，第 24–40 行](../../agentforge/conversation_store.py#L24-L40)
```python
def _merge_semantic_summary(previous, update, *, previous_through, current_from, current_through):
    """保存有界的替换摘要，而不是层层嵌套所有旧版本。"""
    if previous is not None and (not isinstance(previous, str) or not previous.strip()):
        raise ValueError("Existing semantic summary is invalid; previous summary was retained.")
    if not isinstance(update, str) or not update.strip():
        raise ValueError("Semantic summary update is empty; previous summary was retained.")
    if current_from > current_through:
        coverage = (f"Compacted prior checkpoint through message {previous_through}; "
                    "no newer messages were added.")
    else:
        coverage = (f"Replacement through message {current_through}; the summarizer received the prior "
                    f"checkpoint through message {previous_through} and new messages "
                    f"{current_from}-{current_through}.")
    # 【学习区补充】摘要更新合并已有检查点与新增内容，不是用最近一批消息覆盖早期信息。
    merged = f"{_SUMMARY_PREFIX}\n{coverage}\n{update.strip()}"
    # 【学习区补充】摘要按 UTF-8 字节设硬上限；超限就拒绝更新，不静默截短事实。
    if len(merged.encode("utf-8")) > _SUMMARY_MAX_BYTES:
        raise ValueError("Merged semantic summary exceeds its safe limit; existing summary and messages were retained.")
    return merged
```

**逐段讲解。** 这个 helper 本身不把旧摘要正文重新嵌套进去；它给替换摘要加上覆盖范围说明并检查字节上限。增量输入在 prepare_history 中组装：旧摘要作为一条背景记录，加上摘要游标之后的新消息。事实账本独立用于检查更新后是否漏掉来源事实。

### 2. 事实账本帮助压缩同时保留来源
源码原文：[agentforge/conversation_facts.py，第 91–117 行](../../agentforge/conversation_facts.py#L91-L117)
```python
def merge_fact_ledgers(*ledgers):
    """按规范化事实身份合并，同时保留全部来源 ID。"""
    merged = {}
    for ledger in ledgers:
        for fact in ledger or ():
            if not isinstance(fact, dict):
                raise ValueError("Conversation fact ledger is malformed.")
            fact_id, text = fact.get("fact_id"), fact.get("text")
            if not isinstance(fact_id, str) or not isinstance(text, str) or not text.strip():
                raise ValueError("Conversation fact ledger is malformed.")
            existing = merged.get(fact_id)
            # 【学习区补充】事实账本按稳定 fact_id 去重，重复摘要不会无限复制同一事实。
            if existing is None:
                existing = {"fact_id": fact_id, "text": text, "kind": fact.get("kind", "user_fact"), "sources": []}
                merged[fact_id] = existing
            elif existing["text"].casefold() != text.casefold():
                raise ValueError("Conversation fact identity collision; summary was retained.")
            for source in fact.get("sources", ()):
                if not isinstance(source, dict):
                    raise ValueError("Conversation fact provenance is malformed.")
                # 【学习区补充】同一消息来源不重复追加，重复合并仍保留完整但去重后的来源链。
                if source not in existing["sources"]:
                    existing["sources"].append(dict(source))
    result = list(merged.values())
    for fact in result:
        fact["sources"].sort(key=lambda source: (source.get("sequence", 0), source.get("message_id", "")))
    result.sort(key=lambda fact: (fact["sources"][0].get("sequence", 0) if fact["sources"] else 0,
                                  fact["fact_id"]))
    return result
```

**逐段讲解。** 事实有稳定身份、类别及来源消息；合并会避免重复并处理纠正。摘要可缩短内容，但事实的来处仍能追溯。

### 3. 摘要和游标通过检查后一起提交
源码原文：[agentforge/conversation_store.py，第 167–203 行](../../agentforge/conversation_store.py#L167-L203)
```python
    def save_summary(self, conversation_id, repository_id, covered_message_id, text, *,
                     model_name=None, fact_ledger=None):
        text = _text(text, "summary", 100_000)
        if len(text.encode("utf-8")) > _SUMMARY_MAX_BYTES:
            raise ValueError("Summary exceeds the bounded storage budget; existing summary was retained.")
        normalized_ledger = None if fact_ledger is None else validate_fact_ledger(fact_ledger)
        # 【学习区补充】摘要、来源事实和游标在同一事务内提交，避免状态彼此不一致。
        with self.sessions.begin() as session:
            row = self._get(session, conversation_id, repository_id, lock=True)
            covered = session.scalar(select(Message).where(Message.id == _id(covered_message_id),
                                                           Message.conversation_id == row.id))
            # 【学习区补充】摘要游标必须停在已完成的助手消息，不能把半轮对话标记为已覆盖。
            if covered is None or covered.role != "assistant" or covered.request_status != "completed":
                raise ValueError("Summary must end at a completed assistant message in this conversation.")
            summary = session.scalar(select(ConversationSummary).where(
                ConversationSummary.conversation_id == row.id,
                ConversationSummary.covered_through == covered.sequence_number))
            persisted_ledger = validate_fact_ledger(summary.fact_ledger or []) if summary else []
            # 【学习区补充】从当前会话原始消息提取来源账本，事实可追溯到具体消息。
            source_rows = [_message(message) for message in session.scalars(
                select(Message).where(
                    Message.conversation_id == row.id,
                    Message.sequence_number <= covered.sequence_number,
                ).order_by(Message.sequence_number))]
            normalized_ledger = merge_fact_ledgers(
                persisted_ledger, normalized_ledger or [],
                extract_message_facts(source_rows))
            # 【学习区补充】摘要漏掉受保护事实就拒绝保存；异常使事务回滚，旧摘要游标不前进。
            if missing_fact_ids(text, normalized_ledger):
                raise ValueError("Summary omitted a protected source fact; existing summary and cursor were retained.")
            if summary is None:
                summary = ConversationSummary(id=uuid4(), conversation_id=row.id,
                    covered_through=covered.sequence_number, covered_message_id=covered.id,
                    content=text, fact_ledger=normalized_ledger, model_name=model_name)
                session.add(summary)
            else:
                summary.content, summary.updated_at = text, datetime.now(timezone.utc)
                summary.model_name = model_name
                summary.fact_ledger = normalized_ledger
            session.flush()
            return str(summary.id)
```

**逐段讲解。** 摘要更新在验证来源和事实后进入事务；只有提交成功才推进覆盖消息位置。若失败，旧摘要和游标应该保持完整供后续重试。

## 摘要更新必须同时保留内容和来源

长对话压缩会减少重复发送的历史，但压缩不能让重要事实失去来源。旧摘要、已有事实账本和本批新增消息共同参与更新；每条受保护事实带有来源消息 ID，生成结果需要检查是否仍然覆盖这些事实。若模型漏掉关键事实、输出超限或存储失败，应保留旧摘要和原游标，让下一次有机会重试，而不是把不完整摘要标记为已完成。

摘要正文和近期完整消息解决不同问题：摘要承接更早的背景，近期窗口保留原始上下文细节，已总结到的消息不能再重复占预算。摘要长度使用 UTF-8 字节上限不等于模型 Token 数，也不能证明总结语义准确。真实模型摘要仍需对固定事实进行独立核对。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `previous`、`update` | 已有摘要检查点和本次新增摘要内容。 | 新摘要以二者为输入增量合并；若只传 `update` 就会丢失早期背景。 |
| `previous_through`、`current_from`、`current_through` | 上一摘要覆盖游标、本批消息起点和本批终点。 | 它们防止消息范围重叠或跳过；游标只应在摘要成功保存后前进。 |
| `*ledgers` | 一组事实来源账本。 | 可合并多批事实；按稳定身份去重，同时保留每条事实的消息来源。 |
| `conversation_id`、`repository_id` | 摘要所属会话和仓库。 | 防止跨会话或跨仓库复用摘要。 |
| `covered_message_id`、`text` | 摘要覆盖到的消息位置和摘要正文。 | 二者一同确定检查点状态；正文超限或事实检查失败时不能推进覆盖游标。 |
| `model_name`、`fact_ledger` | 摘要来源模型及受保护事实记录，可选。 | 供审计和事实核验使用；不改变旧对话的原始消息。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
