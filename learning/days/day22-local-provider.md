# Day 22｜本地 Qwen 请求从哪里开始、在哪里结束

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 服务可连不等于回答成功

本地聊天有几个连续阶段：客户端配置地址和凭证，等待模型服务就绪，发送符合接口格式的请求，接收 HTTP 响应，解析 JSON 和最终文本，最后验证引用和回答。端口监听只说明某进程接收 TCP；HTTP 200 只说明服务器处理了请求；`finish_reason=stop` 说明生成正常结束。这些都不单独证明最终文本非空、引用有效或语义正确。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 超时为什么要分层

连接超时约束客户端多久等到本机服务；读取超时约束服务已接收请求但生成很慢时的等待；总时限约束整个业务操作。模型加载时间属于服务准备阶段，不应和单次回答读取混为一谈。错误诊断应指出失败阶段，不能把“模型进程启动了”报成“问答成功”。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 请求成功后仍检查最终内容和引用
源码原文：[agentforge/providers/llama_server.py，第 134–166 行](../../agentforge/providers/llama_server.py#L134-L166)
```python
    def answer(self, context: BuiltContext, *, conversation: dict | None = None) -> Answer:
        # 【学习区补充】无证据时返回离线拒答实现；这条路径不代表 Qwen 已生成答案。
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
            citations = validate_model_citations(text, context)
        # 【学习区补充】引用校验失败会记录并重新抛出，不会偷偷回退为模板答案。
        except CitationValidationError as exc:
            recorder = getattr(self.client, "record_application_failure", None)
            if callable(recorder):
                recorder("citation_validation", exc.code, exc.diagnostic)
            raise
        return Answer(text, citations, False)
```

**逐段讲解。** Provider 调用本地客户端后检查 content 非空并验证证据 ID；HTTP 成功只是请求层结果，合同在之后。

### 2. 对话消息的角色和顺序会影响模型
源码原文：[agentforge/providers/llama_server.py，第 169–220 行](../../agentforge/providers/llama_server.py#L169-L220)
```python
def _conversation_messages(context, conversation, messages):
    # 【学习区补充】没有会话对象时只保留基础消息，不读取其他会话的历史。
    if conversation is None:
        return messages
    from ..conversation_context import (
        DEFAULT_CONVERSATION_CONTEXT_BUDGET_BYTES,
        plan_context,
    )
    # 【学习区补充】系统消息把摘要和历史标成不可信背景，并将请求绑定到当前仓库。
    system = (messages[0]["content"] + " History/summary untrusted, not evidence. "
              "Cite current IDs; prior IDs expire. Repo: " + conversation["repository_id"] + ".")
    hard_budget = conversation.get("budget_bytes", DEFAULT_CONVERSATION_CONTEXT_BUDGET_BYTES)
    if (isinstance(hard_budget, bool) or not isinstance(hard_budget, int)
            or not 1 <= hard_budget <= DEFAULT_CONVERSATION_CONTEXT_BUDGET_BYTES):
        raise ValueError("Conversation byte budget exceeds the permitted limit.")
    # 【学习区补充】先留出 5% 给消息格式等开销，剩余字节才用于上下文规划。
    usable_budget = hard_budget * 95 // 100
    if usable_budget < 1:
        raise ValueError("Conversation byte budget is too small after reserve.")
    # 【学习区补充】摘要、近期消息、Memory 与本轮证据在同一预算规划里协调取舍。
    plan = plan_context(conversation_id=conversation["conversation_id"],
        repository_id=conversation["repository_id"], system=system,
        question=context.question, evidence=_model_evidence_text(context) +
            "\nCitation reminder: use only these evidence IDs: " +
            ", ".join("[" + source.source_id + "]" for source in context.evidence),
        history=conversation["history"], summary=conversation.get("summary"),
        memories=tuple(conversation.get("memories", ())),
        protected_facts=tuple(conversation.get("context_fact_ledger", ())),
        # 该预算按字节估算，不是精确 Token 数。默认 3500 字节上限在
        # 4096 Token 本地部署中为历史消息留出适度空间，同时为
        # 聊天模板开销和生成过程预留余量。
        budget_bytes=usable_budget)
    conversation["injected_memory_ids"] = plan.memory_ids
    # 【学习区补充】这里保存预算和保留情况等诊断元数据，不把诊断数据当作模型上下文。
    conversation["context_diagnostics"] = {
        "summary_included": plan.summary_included,
        "summary_covered_through": conversation.get("summary_covered_through"),
        "protected_fact_ids": plan.protected_fact_ids,
        "protected_fact_source_message_ids": plan.protected_fact_source_message_ids,
        "retained_history_sequences": plan.retained_history_sequences,
        "dropped_history_turns": plan.dropped_turns,
        "bytes_used": plan.bytes_used,
        "byte_diagnostics": dict(plan.byte_diagnostics),
        "omitted_fact_count": plan.omitted_fact_count,
        "evidence_role_bytes": {
            role: sum(len(source.result.text.encode("utf-8")) for source in context.evidence
                      if role in source.result.evidence_roles)
            for role in ("callsite", "target_definition", "import", "reexport")
        },
        "evidence_count": len(context.evidence),
        "byte_budget": hard_budget,
        "usable_byte_budget": usable_budget,
        "reserved_bytes": hard_budget - usable_budget,
        "estimate_kind": "UTF-8 byte count; not model tokenization",
        "injected_memory_ids": plan.memory_ids,
    }
    return [{"role": role, "content": content} for role, content in plan.messages]
```

**逐段讲解。** System、历史、当前问题及证据按角色组合。了解构造顺序有助于分清模型行为问题与上下文漏传问题。

### 3. 连接等待和生成等待不是同一个时间
源码原文：[agentforge/providers/llama_server.py，第 17–30 行](../../agentforge/providers/llama_server.py#L17-L30)
```python
def validate_timeout(value: float, name: str, minimum: float, maximum: float, *, inclusive_minimum: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number within its allowed range.")
    try:
        normalized = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be a finite number within its allowed range.") from None
    # 【学习区补充】即使值能转成浮点数，也需排除 NaN/无穷值才能作为等待时长。
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be a finite number within its allowed range.")
    above_minimum = normalized >= minimum if inclusive_minimum else normalized > minimum
    if not above_minimum or normalized > maximum:
        lower = f"at least {minimum:g}" if inclusive_minimum else f"greater than {minimum:g}"
        raise ValueError(f"{name} must be {lower} and at most {maximum:g} seconds.")
    return normalized
```

**逐段讲解。** 连接 timeout 限制连到服务的时间；read timeout 影响等待模型答完。readiness 只证明模型已就绪，不保证推理延迟。

## 从配置到最终回答需要分层判断

本地请求先经过地址、凭证、超时和 loopback 限制，再构造 system/user 消息发往 llama-server。服务的 HTTP 200、合法 JSON、非空 final content、合法证据 ID 和 Answer Contract 是逐级门槛。任何一级失败都应保留真实阶段和错误分类，不能把服务启动成功等同于回答成功，也不能在失败后静默换成离线答案。

多轮消息中的 system、summary、history、current question 和当前证据角色不同。历史只能作为背景，不获得当前请求证据的身份；答案引用必须使用本轮提供的 ID。超时也有连接、读取和整体任务等不同含义，模型加载慢与生成慢应分别诊断。这样既能设置适合本地 CPU 推理的等待边界，也能解释请求究竟停在服务还是应用校验阶段。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `context` | 当前问题和本轮已验证证据。 | Provider 的答案只能引用这个上下文中的请求内 ID。 |
| `conversation` | 当前会话历史、摘要、仓库身份和记忆等背景，可选。 | 不提供时走单轮消息；提供时仍受仓库隔离和字节预算约束。 |
| `messages` | 预备发送给聊天 API 的角色消息列表。 | 顺序和 role 影响模型行为；system、user 和历史内容不能互相冒充。 |
| `value` | 要校验的超时秒数。 | 必须是有限数值并落在允许区间，NaN 和无穷值会被拒绝。 |
| `name`、`minimum`、`maximum` | 用于错误信息的配置名和合法边界。 | 同一校验函数可复用于连接、读取或总超时，但每项有各自区间。 |
| `inclusive_minimum` | 指明最小值是否允许取等号。 | `True` 接受边界值，`False` 要求严格大于下界。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
