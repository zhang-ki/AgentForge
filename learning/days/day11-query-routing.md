# Day 11｜查询意图如何选择检索通道

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 不同问题需要不同的“查找方式”

精确问“`refresh_access_token` 定义在哪”，最可靠的线索是符号名称；问“哪个文件调用它”，需要调用点关系；问“为什么缓存只读”，可能要同时看函数实现和文档。词法搜索擅长字面内容，符号搜索擅长代码定义，PackageRelation 擅长静态结构，向量搜索提供语义候选。通道的排名和分数含义不同，不能因为某个通道分数更大就认为它更可靠。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 为什么不是默认融合所有通道

将所有通道混在一起，可能引入无关候选并挤占上下文；只用一个通道又会漏掉其它类型证据。AgentForge 因此先判断问题更需要什么。RRF 是显式研究实现，但没有因存在代码就成为默认产品策略；是否启用要由独立质量证据决定。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 按问题意图决定搜索通道
源码原文：[agentforge/query_routing.py，第 30–91 行](../../agentforge/query_routing.py#L30-L91)
```python
    def route(self, query: str) -> RouteDecision:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            raise ValueError("Routing query must be non-empty and within the query limit.")
        # 【学习区补充】统一英文大小写后再匹配规则，避免大小写变化影响路由。
        folded = query.casefold()
        words = set(terms(query))
        # 【学习区补充】先处理明确询问证据充分性或不确定性的措辞，避免后续关键词把置信度抬高。
        if any(phrase in folded for phrase in (
            "is there evidence", "cannot determine", "unknown relationship", "unclassified request",
        )):
            return RouteDecision(
                "uncertain", ("lexical",), "uncertainty_phrase",
                "The wording asks about evidence sufficiency or lacks a classifiable target.", "low",
            )
        structural_terms = {
            "import", "imports", "alias", "caller", "callee", "call", "calls",
            "export", "exports", "reexport", "reexports", "resolve", "resolved",
            "relationship", "relationships", "relation", "relations",
        }
        # 【学习区补充】动态行为与关系词同时出现时降为低置信；静态图无法证明运行时绑定。
        if words & {"dynamic", "runtime", "reflective", "reflection"} and words & structural_terms:
            return RouteDecision(
                "uncertain", ("lexical",), "dynamic_relation_uncertainty",
                "Dynamic or reflective behavior cannot be established by the static relation graph; "
                "retain ambiguity and fall back to lexical evidence.", "low",
            )
        if any(word in words for word in (
            # 【学习区补充】这些词触发结构关系路由；这一步选择通道，并不代表通道已经检索成功。
            "import", "imports", "imported", "alias", "aliased", "caller", "callers",
            "callee", "callees", "call", "calls", "called", "invoke", "invokes",
            "invoked", "invoking", "dependency", "dependencies", "depends",
        )):
            return RouteDecision(
                "dependency_relation", ("package_relations", "symbols", "lexical"),
                "dependency_terms", "Import, alias, dependency or caller terms request a static relationship.",
                "high",
            )
        if ("__all__" in query or "re-export" in folded
                or any(word in words for word in ("reexport", "reexports", "export", "exports", "public"))):
            return RouteDecision(
                "package_api", ("package_relations", "lexical"), "public_api_terms",
                "Public API, export or __all__ terms request a package-surface relationship.", "high",
            )
        if (re.search(r"\b(?:where|which file)\b.*\b(?:class|function|module|defined|definition)\b", folded)
                or "qualified name" in folded):
            return RouteDecision(
                "exact_symbol", ("symbols", "lexical"), "exact_symbol_shape",
                "The query explicitly asks for a class/function/module definition.", "high",
            )
        if any(word in words for word in ("document", "documentation", "policy", "rule", "guide")):
            return RouteDecision(
                "documentation", ("lexical", "vector"), "documentation_terms",
                "Documentation or policy terms request textual evidence.", "high",
            )
        if any(word in words for word in ("implement", "implementation", "behavior", "workflow",
                                          "business", "handles", "processes", "how")):
            return RouteDecision(
                "business_implementation", ("lexical", "vector"), "implementation_terms",
                "The query asks for implementation or business behavior rather than a named relation.",
                "medium",
            )
        return RouteDecision(
            # 【学习区补充】所有高置信规则都未命中时回到 uncertain，而不是猜一个业务意图。
            "uncertain", ("lexical",), "no_specific_rule",
            "No high-confidence structural or document intent rule matched; use lexical fallback only.",
            "low",
        )
```

**逐段讲解。** 规则依次检查关系、符号、文档和业务概念线索，返回选择、理由与置信度；未命中的问题显式标记 uncertain。

### 2. 路由判断与实际搜候选分层
源码原文：[agentforge/product_retrieval.py，第 90–97 行](../../agentforge/product_retrieval.py#L90-L97)
```python
    def route(self, query: str) -> RouteDecision:
        stripped = query.strip() if isinstance(query, str) else query
        # 【学习区补充】先识别明确符号，再交给通用意图路由，避免精确查询被宽泛规则抢走。
        if _is_explicit_symbol_reference(stripped):
            return RouteDecision(
                "exact_symbol", ("symbols", "lexical"), "explicit_symbol_reference",
                "An explicit Python symbol reference is treated as an exact lookup.", "high",
            )
        return self._base.route(query)
```

**逐段讲解。** 产品路由器记录规则决定，ProductRetriever 再执行相应通道。调试时应先看意图判断，再看选中通道给了什么候选。

## 路由是在选择检索方法，不是在提前回答

精确函数名更适合符号查找；导入和调用关系问题需要结构关系证据；一般概念或文档问题可以用词法检索，并在启用时补充向量候选。路由器读取的是问题中的意图线索，输出通道、理由和置信状态；Retriever 才负责访问索引并生成候选。把这两层拆开，能分别检查“选错了搜索方法”与“搜索方法没有找到内容”。

当查询意图不明确时，系统可以使用较保守的词法回退，但回退结果仍然只是候选。回退不能伪装成精确符号解析，向量相似度也不等于证据充分。当前产品路由不会因为多个通道存在就默认融合全部排名；是否使用 Chroma 还受运行配置影响，RRF 原型与默认问答链路是两回事。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `query` | 用户提出的原始问题。 | 路由器根据其中的符号、关系词、文档词等线索选择通道；它不负责生成答案。 |
| `RouteDecision.route` | 选中的检索方向。 | 决定后续调用符号、关系、词法或可选向量检索中的哪些路径。 |
| `rule`、`reason` | 命中的规则标识和人可读理由。 | 用于解释为什么这样路由，便于定位误分类，而不是模型相关性分数。 |
| `confidence`、`uncertainty` | 路由置信状态及剩余不确定性。 | 低置信路由仍需要保守回退；不能把规则匹配说成关系已证明。 |
| `unavailable_channels` | 本次不能使用的通道列表。 | 明确报告缺失能力，防止把未运行的 Vector 等通道当成已参与检索。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
