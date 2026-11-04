# Day 14｜从问题走到可信回答

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 把本周内容组成一条路线

以“哪个模块调用 `run_queue`，函数实现在哪里”为例。符号解析先确认 `run_queue` 的定义身份；PackageRelation 搜索调用表达式；候选需要通过当前源码身份检查；上下文必须同时包含调用点和最终定义；模型回答后引用必须映射回这两段原文；Answer Contract 再检查问题需要的证据角色是否都齐全。任何一步缺失，结论都要收窄。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 两种常见错误要分开

如果没有找到调用点，这是检索或静态解析问题；如果调用点和定义都已进入上下文，但模型仍把导入说成调用，这是回答语义问题。若预算只容纳一条必要证据，应安全停止或限定回答，不要偷偷丢掉另一条后宣称完成。分清故障发生层，才能准确理解项目为何拒答或只给有限结论。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 不同问题需要不同证明材料
源码原文：[agentforge/evidence_roles.py，第 31–42 行](../../agentforge/evidence_roles.py#L31-L42)
```python
def required_evidence_roles(question: str) -> tuple[str, ...]:
    """返回回答问题各个明确部分所需的证据角色。"""
    text = question if isinstance(question, str) else ""
    # 【学习区补充】同时问调用关系和定义位置时，需要调用点与目标定义两类证据。
    if _CALL.search(text) and _DEFINITION.search(text):
        roles = ["callsite", "target_definition"]
        if _CALLER_IDENTITY.search(text):
            roles.insert(0, "caller_definition")
        return tuple(roles)
    # 【学习区补充】配置问题需要使用位置和值的定义共同支持，单独找到变量名不够。
    if (_CONFIG_USE.search(text) and _CONFIG_VALUE.search(text)
            and re.search(r"\b[A-Z][A-Z0-9_]{2,}\b", text)):
        return ("configuration_use", "configuration_definition")
    return ()
```

**逐段讲解。** 调用问题可能需要调用点和定义；配置问题可能需要定义、使用点及导入。角色规则使检索不只追求分数高，也检查证据是否够回答。

### 2. coverage 返回已覆盖和缺失角色
源码原文：[agentforge/evidence_roles.py，第 45–56 行](../../agentforge/evidence_roles.py#L45-L56)
```python
def role_coverage(question: str, available_roles, cited_roles=None) -> dict:
    """描述必需、可用及已引用的证据角色，不保留源码正文。"""
    required = required_evidence_roles(question)
    available = set(available_roles or ())
    # 【学习区补充】未传实际引用列表时用于生成概览；正式答案校验会传入真实引用角色。
    cited = available if cited_roles is None else set(cited_roles or ())
    return {
        "required_roles": required,
        "available_roles": tuple(sorted(available)),
        "cited_roles": tuple(sorted(cited)),
        # 【学习区补充】分别报告缺少检索材料和回答未引用材料，方便定位是召回还是引用问题。
        "missing_available_roles": tuple(role for role in required if role not in available),
        "missing_cited_roles": tuple(role for role in required if role not in cited),
    }
```

**逐段讲解。** 上层可以用 missing 结果在生成前拒绝，避免模型用旁边材料补出没有证据的结论。

### 3. 合并时去重但保留通道来源
源码原文：[agentforge/product_retrieval.py，第 476–491 行](../../agentforge/product_retrieval.py#L476-L491)
```python
    def _merge(existing: ProductCandidate, incoming: ProductCandidate) -> ProductCandidate:
        channels = tuple(dict.fromkeys(existing.channels + incoming.channels))
        scores = dict(existing.channel_scores)
        scores.update(dict(incoming.channel_scores))
        statuses = {existing.resolution_status, incoming.resolution_status}
        # 【学习区补充】合并候选时优先保留较不确定状态；另一条确定边不会抹去歧义。
        status = next((item for item in ("ambiguous", "unresolved", "uncertain")
                       if item in statuses), existing.resolution_status)
        return replace(
            existing,
            channels=channels,
            channel_scores=tuple((name, scores[name]) for name in channels),
            symbol_ids=tuple(dict.fromkeys(existing.symbol_ids + incoming.symbol_ids)),
            relation_ids=tuple(dict.fromkeys(existing.relation_ids + incoming.relation_ids)),
            resolution_status=status,
            evidence_roles=tuple(dict.fromkeys(existing.evidence_roles + incoming.evidence_roles)),
        )
```

**逐段讲解。** 同一证据可来自多个检索通道，合并后保留来源角色和解析状态；不同源码职责不应被压成不可分辨的一段。

## 证据充分性取决于问题要证明什么

问题不同，答案需要的证据也不同。定位定义通常需要一个符号定义范围；解释调用关系则至少要找到调用点和目标定义；解释配置如何影响实现，可能需要配置定义、传递路径和使用位置。`coverage` 结果把已找到和仍缺少的证据角色分开，回答组装器才能知道当前材料是否足以覆盖问题，而不只是看到“有一个相关片段”。

多通道合并时，去重应按稳定的来源身份进行，同时保留每个候选来自哪些通道、承担什么证据角色。否则可能把两个路径相同但仓库不同的文件合成一个结果，或在去重后丢掉唯一的调用点。排序分数帮助选择候选，但最终是否回答仍取决于证据角色和内容核验。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `question` | 要判断证据是否充分的问题。 | 规则据此推断需不需要定义、调用点、目标等证据角色。 |
| `available_roles` | 检索候选已经覆盖的角色集合。 | 表示系统找到了什么，不表示回答已经引用这些角色。 |
| `cited_roles` | 回答引用实际覆盖的角色，可选。 | 与可用角色对比可发现“证据已找到但模型没有引用”的缺口。 |
| `existing`、`incoming` | 已有候选与新通道送来的候选。 | 合并时应去重内容身份，同时保留两个候选的通道来源和角色信息。 |
| `evidence_roles` | 候选承担的语义位置，例如 callsite 或 target definition。 | 不同角色不能只因路径相同就折叠成一条证据。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
