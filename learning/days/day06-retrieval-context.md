# Day 6｜检索候选怎样变成模型上下文

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 检索不是“把最高分结果直接发给模型”

词法检索按名称和文字匹配；符号检索寻找定义；PackageRelation 处理静态导入、调用和导出关系；向量检索提供语义相似候选。不同问题需要不同来源。产品 Retriever 会先判断意图，再选择适用通道；通道分数的单位和含义可能不同，不能直接混加。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 预算到底限制什么

AgentForge 的会话上下文使用 UTF-8 字节数作为保守代理，不等于模型 tokenizer 的精确 token 数，也不等于回答的输出长度。问题和必要源码证据优先；历史、Memory 与摘要只能在预算允许时加入。必要证据放不下，应在 Provider 调用前停止，不能删掉调用点或定义后继续生成。模型收到的必须是完成回答所需的源码，不是只有引用编号。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 完整标识符和分词线索同时保留
源码原文：[agentforge/retriever.py，第 13–20 行](../../agentforge/retriever.py#L13-L20)
```python
def terms(text: str) -> list[str]:
    result = []
    for word in _WORDS.findall(text):
        result.append(word.casefold())
        # 【学习区补充】拆出 camelCase/下划线子词，同时保留完整标识符用于精确匹配。
        pieces = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word).replace("_", " ").split()
        if len(pieces) > 1:
            result.extend(piece.casefold() for piece in pieces)
    return result
```

**逐段讲解。** 下划线和大小写边界拆分提高问法与代码名的词面重叠，但不会宣称理解了自然语言语义。

### 2. 分数来自可解释的词法信号
源码原文：[agentforge/retriever.py，第 23–75 行](../../agentforge/retriever.py#L23-L75)
```python
def search(store, query: str, top_k: int = 5) -> list[SearchResult]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query must not be empty.")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"Query cannot exceed {MAX_QUERY_LENGTH} characters.")
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}.")
    # 【学习区补充】先规范化、去重并排序查询词，避免重复词多次抬高分数。
    query_terms = sorted(set(terms(query)) - _STOP)
    if not query_terms:
        raise ValueError("Query has no searchable terms.")
    # 【学习区补充】保留原始标识符形态，后面可匹配完整名称和 def/class 声明。
    raw_terms = sorted({word.casefold() for word in _WORDS.findall(query)} - _STOP)
    repository = store.metadata().get("repository", "")
    results = []
    for row in store.iter_chunks():
        content_terms = Counter(terms(row["text"]))
        path_terms = set(terms(row["relative_path"]))
        # 每个词项按有限词频与路径命中计分；完整查询词项另有
        # 会额外获得精确标识符匹配加分。
        score = sum(min(content_terms[term], 5) + (3 if term in path_terms else 0)
                    for term in query_terms)
        # 【学习区补充】完整词项命中正文时加分，提升精确标识符查询的排序。
        score += sum(4 for term in raw_terms if re.search(
            r"(?<!\w)" + re.escape(term) + r"(?!\w)", row["text"], re.IGNORECASE
        ))
        # 【学习区补充】命中函数或类声明行时额外加分，让定义优先于普通提及。
        score += sum(8 for term in raw_terms if re.search(
            r"(?m)^\s*(?:async\s+)?(?:def|class|function)\s+" + re.escape(term) + r"\b",
            row["text"], re.IGNORECASE,
        ))
        # 支持中文关键词子串匹配，但不声称完成了中文分词。
        score += sum(2 * min(row["text"].casefold().count(term), 5)
                     for term in query_terms if re.search(r"[\u3400-\u9fff]", term))
        if query.strip().casefold() in row["relative_path"].casefold():
            score += 8
        # 【学习区补充】没有任何匹配信号的片段不会进入候选集。
        if score:
            results.append(SearchResult(
                repository=repository,
                relative_path=row["relative_path"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                text=row["text"],
                content_hash=row["content_hash"],
                score=float(score),
            ))
    # 【学习区补充】同分时再按路径和行号排序，保证结果顺序稳定可复现。
    results.sort(key=lambda item: (-item.score, item.relative_path, item.start_line, item.end_line))
    unique = []
    seen = set()
    for result in results:
        if result.content_hash in seen:
            continue
        seen.add(result.content_hash)
        unique.append(result)
        if len(unique) == top_k:
            break
    return unique
```

**逐段讲解。** 函数将词频、路径、标识符边界、定义行和中文子串作为不同奖励，再排序与去重。分值是排序工具，不是概率或事实可信度。

### 3. E 编号仅分配给实际放进上下文的证据
源码原文：[agentforge/context.py，第 10–40 行](../../agentforge/context.py#L10-L40)
```python
def build_context(
    question: str,
    results: list[SearchResult],
    *,
    max_candidates: int = 5,
    budget: int = 6000,
) -> BuiltContext:
    if not 1 <= max_candidates <= 100 or not 1 <= budget <= 1_000_000:
        raise ValueError("Context candidate count or estimated budget is out of range.")
    evidence = []
    blocks = []
    seen = set()
    used = 0
    for result in results[:max_candidates]:
        path = PurePosixPath(result.relative_path)
        if (path.is_absolute() or ".." in path.parts or "\\" in result.relative_path
                or ":" in result.relative_path or any(c in result.relative_path for c in "\r\n[]")
                or result.start_line < 1 or result.end_line < result.start_line):
            raise ValueError("Invalid evidence path or line range.")
        # 【学习区补充】相同内容只保留一次，避免重复证据占掉上下文名额。
        if result.content_hash in seen:
            continue
        seen.add(result.content_hash)
        # 【学习区补充】这里先形成候选标签；通过预算检查并追加后才成为实际上下文证据。
        source = Evidence(f"E{len(evidence) + 1}", result)
        block = f"[{source.source_id}] [[{source.citation}]]\n{result.text}\n"
        cost = len(block.encode("utf-8"))
        # 【学习区补充】证据块只能完整加入或省略；不截断正文、路径或行号。
        if used + cost > budget:
            continue
        # 【学习区补充】只累计已接受证据的完整 UTF-8 字节数，供后续候选计算剩余预算。
        used += cost
        evidence.append(source)
        blocks.append(block)
    return BuiltContext(question, tuple(evidence), "".join(blocks), used, budget)
```

**逐段讲解。** 路径和行范围先校验，内容哈希用于去重，证据按顺序编号并纳入预算。未进入 BuiltContext 的候选不能被模型合法引用。

### 4. 历史相关性判断防止背景挤占问题
源码原文：[agentforge/conversation_context.py，第 66–81 行](../../agentforge/conversation_context.py#L66-L81)
```python
def _relevant_facts_for_self_contained_code_query(ledger, question, evidence):
    """仅对明确的静态代码问题保守省略无关事实。"""
    # 【学习区补充】仅自包含的代码问题才筛掉无关事实；追问或代词场景会保留完整账本。
    if not re.search(r"\b(?:caller|callee|calls|implementation|function|method)\b|调用|函数|方法", question, re.I):
        return ledger
    if re.search(r"\b(?:previous|earlier)\b|之前|刚才|上面", question, re.I):
        return ledger
    if (re.search(r"\b(?:it|its|they|their)\b|它", question, re.I)
            and not re.search(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b", question)):
        return ledger
    terms = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", question))
    terms = {term.casefold() for term in terms if term.casefold() not in {
        "the", "and", "for", "where", "which", "caller", "calls", "function", "implementation", "return", "def"}}
    # 【学习区补充】约束和偏好即使不含查询关键词也会保留，避免相关性筛选抹掉用户规则。
    constraints = re.compile(r"\b(?:must|never|always|required|constraint|do not|don't|prefer|preference)\b|必须|不得|禁止|约束|偏好", re.I)
    return [fact for fact in ledger if fact.get("kind") in {"constraint", "decision", "correction", "open_task"}
            or constraints.search(fact["text"]) or
            terms.intersection(term.casefold() for term in re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", fact["text"]))]
```

**逐段讲解。** 完整的符号/代码问题可以不带无关旧消息；这只影响本轮上下文，不删除历史存储。

## 检索结果如何变成有限的模型输入

检索器先根据问题中的完整标识符、词语线索或结构关系找出候选；路由选择哪个通道，并不代表候选已经充分证明了答案。随后应用检查候选的仓库和来源身份，按角色去重，给实际选入的片段分配本次请求的证据 ID。只有进入上下文的证据才会出现在模型可引用列表中，未选入的搜索结果不能在回答阶段被模型引用。

上下文预算约束最终发出的整组消息，而不是终端显示窗口的行数。系统要同时计算系统指令、问题、证据、历史摘要和会话消息的 UTF-8 字节数；它不是模型 tokenizer 的精确 Token 数。必要证据放不下时应停止并说明原因，不能先删掉关键定义或调用点，再让模型猜缺失部分。可选历史应先让位给当前问题和必要证据。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `text` | `terms()` 要处理的查询或文本。 | 分词会生成用于匹配的词项；代码标识符还要保留完整形式，避免只按自然语言切词。 |
| `store`、`query` | `store` 提供当前仓库的索引，`query` 是要检索的问题。 | Store 决定数据范围；问题决定匹配信号，不能用另一个仓库的 Store 混入结果。 |
| `top_k` | 最多返回多少个检索候选，示例默认 5。 | 太小可能漏掉互补证据；太大则增加后续筛选和上下文压力。 |
| `question`、`results` | 上下文构造所需的问题和候选列表。 | 构造器会从候选中挑选证据；检索到不代表一定会放入模型上下文。 |
| `max_candidates` | 候选最多保留数量。 | 与 `budget` 不同：一个限制条目数，一个限制格式化内容大小。 |
| `budget` | 本函数可使用的上下文字节预算。 | 示例默认 6000 是此构造接口的默认值；会话 Provider 可再传入更严格的请求预算，字节数不等于 Token 数。 |
| `ledger`、`question`、`evidence` | 事实账本、当前问题和当前证据。 | 相关性判断应同时参考当前问题与证据，只将相关事实加入本轮，不改变永久保存的账本。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
