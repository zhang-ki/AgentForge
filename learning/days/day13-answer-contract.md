# Day 13｜Answer Contract：事实、推断和未知

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 合法引用不等于正确结论

假设源码写着 `return f"offline-{session_id}"`。回答“函数返回一个按 session_id 拼出的字符串”与源码相符；回答“函数安全刷新了用户的认证令牌”则可能是错误推断，即使引用指向同一行。引用校验能确认来源身份，无法单独证明自然语言结论蕴含于来源。

Answer Contract 把回答组织成主张与引用，并区分 confirmed、inference、unknown 等类别；整体状态可以是 answered、limited 或 refused。`limited` 并非失败，它表示回答有限、语义支持或解析能力仍有边界。系统不应为了让结果看起来确定而把推断改称事实。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 合同能做什么、不能做什么

确定性校验器可以发现缺失证据、非法 ID、证据角色不够或合同字段不合法；它无法可靠判断任意自然语言的全部蕴含关系。语义验收还需要独立事实断言或人工核对。因此输出应准确标明 `limited`、拒答原因和未验证部分，而不是夸大保证。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 证据角色缺少时合同不应放行
源码原文：[agentforge/answer_contract.py，第 74–96 行](../../agentforge/answer_contract.py#L74-L96)
```python
def _validate_required_role_coverage(question, evidence, citations) -> dict:
    by_citation = {item.citation: item for item in evidence}
    # 【学习区补充】先看检索结果包含哪些必需证据角色，再与回答实际引用的角色分开比较。
    available = {role for item in evidence for role in item.evidence_roles}
    cited = {role for citation in citations
             for role in getattr(by_citation.get(citation), "evidence_roles", ())}
    coverage = role_coverage(question, available, cited)
    # 【学习区补充】所需材料都没检索到时，问题出在证据阶段，回答文本不能补造证据。
    if coverage["missing_available_roles"]:
        code = "required_evidence_role_unavailable"
        message = "The request lacks a source excerpt for a required evidence role."
    elif coverage["missing_cited_roles"]:
        code = "required_evidence_role_not_cited"
        message = "The generated answer did not cite all evidence roles required by the question."
    else:
        return coverage
    raise CitationValidationError(message, code=code, diagnostic={
        "rule_id": code,
        "required_roles": coverage["required_roles"],
        "available_roles": coverage["available_roles"],
        "cited_roles": coverage["cited_roles"],
        "missing_roles": (coverage["missing_available_roles"]
                          or coverage["missing_cited_roles"]),
        "validation_result": "rejected",
    })
```

**逐段讲解。** 问题需要的 callsite/definition 等角色与实际引用角色比较；缺少任何必需项都会显式返回缺失信息。

### 2. 合同将回答文本变成可审计声明
源码原文：[agentforge/answer_contract.py，第 99–144 行](../../agentforge/answer_contract.py#L99-L144)
```python
def validate_answer_contract(contract: VerifiedAnswerContract, context: BuiltContext) -> None:
    if not isinstance(contract, VerifiedAnswerContract):
        raise ProviderError("Answer contract has an invalid type.")
    # 【学习区补充】拒绝未知版本和状态，避免不兼容合同绕过现有校验规则。
    if contract.version != ANSWER_CONTRACT_VERSION or contract.status not in _STATUSES:
        raise ProviderError("Answer contract version or status is invalid.")
    context_by_citation = {item.citation: item for item in context.evidence}
    seen = set()
    for evidence in contract.evidence:
        source = context_by_citation.get(evidence.citation)
        # 【学习区补充】逐条核对来源身份，防止伪造标签或引用另一轮请求的证据。
        if source is None or source.source_id != evidence.source_id:
            raise ProviderError("Answer evidence is absent from this request context.")
        # 【学习区补充】来源文本需与当前证据完整一致，避免截短或改写后仍冒充原文。
        if (evidence.excerpt not in source.result.text
                or evidence.excerpt != source.result.text
                or not evidence.channels
                or evidence.resolution_status not in _RESOLUTION
                or evidence.evidence_roles != source.result.evidence_roles):
            raise ProviderError("Answer evidence text or provenance is invalid.")
        # 【学习区补充】同一引用只能在合同证据中出现一次，便于稳定审计。
        if evidence.citation in seen:
            raise ProviderError("Answer evidence contains a duplicate citation.")
        seen.add(evidence.citation)
    if context.evidence and not contract.evidence and contract.status != "refused":
        raise ProviderError("A non-refused answer must retain request evidence.")
    if not context.evidence and contract.status != "refused":
        raise ProviderError("An answer without evidence must be refused.")
    for claim in contract.claims:
        if claim.kind not in _CLAIM_KINDS or not claim.text.strip():
            raise ProviderError("Answer claim type or text is invalid.")
        if any(citation not in seen for citation in claim.citations):
            raise ProviderError("Answer claim cites evidence outside its contract.")
        if claim.kind in {"confirmed", "inference"} and not claim.citations:
            raise ProviderError("A factual claim must cite request-local evidence.")
        if claim.kind in {"confirmed", "inference"} and claim.text not in contract.content:
            raise ProviderError("A factual claim must appear in the answer content.")
        # 【学习区补充】confirmed 主张必须能在被引用摘录中逐字找到；推断仍保留较低保证等级。
        if claim.kind == "confirmed" and not any(
                claim.text in item.excerpt
                for item in contract.evidence if item.citation in claim.citations):
            raise ProviderError("A confirmed claim must be an exact excerpt from cited evidence.")
        if claim.kind in {"confirmed", "inference"}:
            _validate_required_role_coverage(context.question, contract.evidence,
                                             claim.citations)
    # 【学习区补充】answered 状态必须至少有一个 confirmed 主张，不能只看模型自报状态。
    if contract.status == "answered" and not any(
            claim.kind == "confirmed" for claim in contract.claims):
        raise ProviderError("An answered contract requires a confirmed cited claim.")
    if contract.status == "refused" and any(
            claim.kind == "confirmed" for claim in contract.claims):
        raise ProviderError("A refused answer cannot contain confirmed claims.")
```

**逐段讲解。** 它连接 claims、证据和状态，检查引用身份与覆盖条件。结构化合同改善可追溯性，但不等同任意自然语言的逻辑证明。

### 3. 回答校验有明确的保证上限
源码原文：[agentforge/answer_contract.py，第 172–199 行](../../agentforge/answer_contract.py#L172-L199)
```python
def verify_generated_answer(answer: Answer, context: BuiltContext, *, route: str,
                            route_confidence: str, candidate_by_citation=None,
                            uncertainty: str | None = None) -> VerifiedAnswerContract:
    evidence = _contract_evidence(context, candidate_by_citation)
    citations = validate_model_citations(answer.text, context)
    # 【学习区补充】没有证据或没有有效引用时明确拒答，不把格式相似的回答当作已证实。
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

**逐段讲解。** E 标签先映射为真实来源，再做合同检查。Citation provenance 可以验证；语义蕴含仍需独立断言或人工判断。

## 合同让回答状态比一段文本更明确

Answer Contract 把内容、声明类型、引用、证据和路由状态组合起来。`fact` 表示直接可由源码核对的陈述，`inference` 表示根据静态关系或多段材料作出的推断，`unknown` 则表达当前证据不足。结构化状态便于 CLI、桌面界面和测试知道系统是正常回答、有限结论还是拒答，而不是猜测自然语言措辞的语气。

合同主要验证声明与本次证据的关系、引用身份和必要证据角色；它并没有一个通用机制能证明所有自然语言句子的逻辑蕴含。比如引用了正确函数，但把条件分支解释反了，引用格式仍可能完全有效。因此合同状态不能被包装成“语义百分之百正确”，需要结合源码事实断言和人工判断。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `question` | 决定回答需要哪些证据角色。 | 同一组证据对“定义在哪里”和“谁调用它”可能有不同充分性。 |
| `evidence`、`citations` | 当前可用证据对象及回答真正引用的来源。 | 合同检查引用是否属于已提供材料，以及所需角色有没有被引用。 |
| `contract` | 待验证的结构化回答合同。 | 包含状态、声明和引用；字段之间不一致时必须拒绝。 |
| `context` | 本轮问题、证据和引用 ID 的权威映射。 | 合同不能只凭模型提供的文本建立证据身份。 |
| `route`、`route_confidence` | 路由名称和置信程度。 | 低置信或静态不确定性会影响回答能声称到什么程度。 |
| `uncertainty` | 需要随回答保留的不确定说明，可选。 | 有值时应限定结论；空值不能自动证明结论已验证。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
