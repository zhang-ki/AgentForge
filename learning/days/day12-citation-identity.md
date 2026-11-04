# Day 12｜Evidence ID 如何指向真实证据

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 为什么让模型只写 `[E1]`

如果模型直接写 `src/example.py:12`，它可能生成一个看起来合理却不存在的路径或行号。AgentForge 改为由应用为本轮证据分配 `[E1]`、`[E2]` 等编号，并在应用侧保存编号与路径、起止行、原文和来源身份的对应关系。模型只能引用本次请求给它的 ID；应用再把 ID 映射回真实证据。

这个编号只在当前请求有效。下一次请求中的 `[E1]` 可能代表另一段代码，因此不能把它当成全局稳定 ID，也不能接受跨请求引用。引用文字正确、ID存在、原文仍有效，是不同检查步骤。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 安全属性

路径和行号由应用证据映射提供，模型只负责声明“我依据哪个证据 ID”。这样可避免模型自己决定要引用哪个仓库文件。即便身份验证通过，仍须检查引用是否支持自然语言主张；下一课会说明这一层边界。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 证据 ID 是请求局部身份
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
        # 【学习区补充】在分配 E 编号前去重，因此标签只代表最终进入上下文的证据。
        if result.content_hash in seen:
            continue
        seen.add(result.content_hash)
        source = Evidence(f"E{len(evidence) + 1}", result)
        block = f"[{source.source_id}] [[{source.citation}]]\n{result.text}\n"
        cost = len(block.encode("utf-8"))
        # 【学习区补充】预算不够时省略整条证据，不把被截断的代码冒充完整来源。
        if used + cost > budget:
            continue
        used += cost
        evidence.append(source)
        blocks.append(block)
    return BuiltContext(question, tuple(evidence), "".join(blocks), used, budget)
```

**逐段讲解。** E1 由进入本轮上下文的次序产生；下一次请求可指向完全不同来源，因此不能将 E1 当作数据库里的永久 ID。

### 2. 应用映射路径，模型只提交证据标签
源码原文：[agentforge/context.py，第 49–145 行](../../agentforge/context.py#L49-L145)
```python
def validate_model_citations(text: str, context: BuiltContext) -> tuple[str, ...]:
    """将请求内的 ``[E#]`` 标记映射到规范化证据引用。

    模型不能决定路径或行范围；这些身份只能来自
    当前请求的 :class:`BuiltContext`。明显的路径/行号引用尝试会被
    拒绝；普通文字（如 ``line 12``）或类型注解则不会被
    当作引用处理。
    """
    if not isinstance(text, str):
        raise ProviderError("Model answer must be text.")

    # 【学习区补充】E1 等标签只在本次请求的映射表中有效，不能跨请求复用。
    by_id = {source.source_id: source.citation for source in context.evidence}
    citations: list[str] = []
    matched_source_ids: list[str] = []
    validated_source_ids: list[str] = []

    # 提示词会展示 [E1] 这类来源标签。必须验证每个以 E 开头的标记，
    # 也要检查宽松正则表达式可能悄悄漏掉的格式错误标记。
    cursor = 0
    while True:
        # 【学习区补充】先找大写标签，同时也找小写标签，确保格式错误的引用不会漏检。
        upper_start = text.find("[E", cursor)
        lower_start = text.find("[e", cursor)
        if upper_start < 0:
            start = lower_start
        elif lower_start < 0:
            start = upper_start
        else:
            start = min(upper_start, lower_start)
        if start < 0:
            break
        end = text.find("]", start + 2)
        # 【学习区补充】未闭合或跨行的标签会明确失败，不靠宽松解析猜测边界。
        if end < 0 or "\n" in text[start:end] or "\r" in text[start:end]:
            _citation_failure(
                text, code="malformed_source_id",
                message="Model output contains a malformed source ID.",
                position=start, matched_source_ids=matched_source_ids,
                validated_source_ids=validated_source_ids,
            )
        # 【学习区补充】先提取模型写出的标签，接下来还要验证格式并确认它属于当前请求。
        source_id = text[start + 1:end]
        recorded_source_id = source_id if len(source_id) <= 32 else source_id[:32] + "..."
        matched_source_ids.append(recorded_source_id)
        # 【学习区补充】格式合法还不够；标签必须属于当前请求的证据映射。
        if not re.fullmatch(r"E[1-9]\d*", source_id) or source_id not in by_id:
            _citation_failure(
                text, code="unknown_source_id",
                message="Model output contains a source ID absent from this request.",
                position=start, matched_source_ids=matched_source_ids,
                validated_source_ids=validated_source_ids,
            )
        citations.append(by_id[source_id])
        validated_source_ids.append(source_id)
        # 【学习区补充】将扫描位置移到当前标签之后，逐一检查一条回答里的多个引用。
        cursor = end + 1

    # 绝不接受模型提供的旧式路径标记；仓库路径和行号范围
    # 由应用程序提供，而不是由模型生成。
    # 【学习区补充】旧式路径标记一律拒绝，路径与行号应由应用从证据生成。
    legacy_positions = [position for position in (text.find("[["), text.find("]]")) if position >= 0]
    if legacy_positions:
        position = min(legacy_positions)
        code = "legacy_path_citation_forbidden"
        marker = re.search(r"\[\[([^\r\n\[\]]+)\]\]", text[position:])
        if marker:
            parsed = re.fullmatch(r"([^:]+):([1-9]\d*)-([1-9]\d*)", marker.group(1))
            if parsed:
                if len(parsed.group(2)) > 10 or len(parsed.group(3)) > 10:
                    code = "out_of_range_line_number"
                elif int(parsed.group(3)) < int(parsed.group(2)):
                    code = "invalid_line_range"
        _citation_failure(
            text, code=code,
            message="Model output must cite request evidence by source ID only.",
            position=position, matched_source_ids=matched_source_ids,
            validated_source_ids=validated_source_ids,
        )

    # 只拒绝明确的“路径加位置”引用。
    # “line 12”、时间、类型注解、文件名和 Markdown 都仍是
    # 普通技术文本，不能被误判为引用。
    # 【学习区补充】检查裸路径加行号，但普通“line 12”等技术文字不会仅凭数字触发。
    path_location = _BARE_PATH_LOCATION.search(text)
    if path_location:
        starts = [path_location.group(name) for name in (
            "colon_start", "hash_start", "word_start",
        ) if path_location.group(name)]
        ends = [path_location.group(name) for name in (
            "colon_end", "hash_end", "word_end",
        ) if path_location.group(name)]
        code = "model_supplied_path_location_forbidden"
        if starts and any(len(value) > 10 for value in starts + ends):
            code = "out_of_range_line_number"
        elif starts and ends and int(ends[0]) < int(starts[0]):
            code = "invalid_line_range"
        _citation_failure(
            text, code=code,
            message="Model output must not supply repository paths or line locations.",
            position=path_location.start(), matched_source_ids=matched_source_ids,
            validated_source_ids=validated_source_ids,
        )

    return tuple(dict.fromkeys(citations))
```

**逐段讲解。** 校验器拒绝当前请求不存在的 ID、路径行号伪造和旧式格式，最终返回的 canonical citations 从 BuiltContext 获取。

### 3. 来源身份从应用结构数据产生
源码原文：[agentforge/answer_contract.py，第 54–58 行](../../agentforge/answer_contract.py#L54-L58)
```python
def _provenance(candidate_by_citation, citation: str):
    # 【学习区补充】只从本轮候选表补回通道和解析状态，模型文本本身不携带权威来源身份。
    candidate = candidate_by_citation.get(citation) if candidate_by_citation else None
    channels = tuple(getattr(candidate, "channels", ("lexical",)))
    resolution = getattr(candidate, "resolution_status", "not_applicable")
    return channels, resolution
```

**逐段讲解。** 这个 helper 只从候选取回来源通道和解析状态；没有候选时采用明确默认值。路径、行范围和原文是在后面的 _contract_evidence 中从 SearchResult 复制的，不由模型提供。

## 引用标签是一次请求里的短编号

模型上下文只要求回答使用 `[E1]` 这样的标签；应用保存着 E1 到真实证据对象的映射，其中包括仓库、路径、行号和原文。验证时先确认 ID 属于本次请求，再从应用映射中构造规范引用。这样模型不需要自行编写可信路径或行号，系统也能拒绝过期证据、未知 ID 和伪造位置。

编号是请求局部的：下一次问答中的 E1 可能指向另一段源码，因此不能把上一轮标签原样带进当前回答。即使 ID 合法，也只证明模型引用了实际提供过的材料，不证明这段材料能支持它旁边的所有句子。引用真实性与语义蕴含是两项不同检查。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `question`、`results` | 当前问题和检索到的候选集合。 | 上下文构造只从这些候选挑选本轮证据。 |
| `max_candidates` | 证据候选数量上限。 | 限制条目数，不保证总字节数一定合适。 |
| `budget` | 格式化上下文可使用的字节上限。 | 超限时应安全拒绝或移除可选内容，不能截断路径和证据正文。 |
| `text`、`context` | 模型实际返回文本以及本次请求上下文。 | Validator 只接受能映射到该 Context 的证据 ID；其他请求中的 E1 无效。 |
| `candidate_by_citation`、`citation` | 已核验候选映射和待解析的引用字符串。 | 用来找到对应真实路径、行号和原文；模型自写的位置不作为权威来源。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
