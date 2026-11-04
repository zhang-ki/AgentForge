# Day 15｜Embedding 如何产生向量候选

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 聊天模型与 Embedding 模型做不同工作

聊天模型根据上下文生成文本；Embedding 模型把文本编码成固定长度的数值向量。向量之间可以计算距离或相似度，因此系统能按语义相近程度找到候选片段。高相似度只说明模型表示空间中的接近，不证明候选准确，更不等于最终答案正确。

查询和文档通常应使用同一个向量模型及兼容预处理。模型版本、维度、归一化方式或 query/document 角色不同，都可能使比较失去意义。项目将模型身份和向量维度一起管理；向量来源内容改变后，旧向量也不能继续代表新源码。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 怎样理解“持久化向量库”

SQLite 将代码块原文、行号和内容哈希保存在磁盘上，它是代码证据的权威副本。Chroma 保存代码块的向量和来源元数据，用来快速找到“可能相关”的候选；代码向量记录不再重复保存整段源码。长期记忆走另一条路径，向量记录仍携带记忆文本，以便记忆检索器读取后再向 PostgreSQL 核验。

这也区分了“磁盘保存”和“运行时占用”：SQLite 文件里的代码原文不会因为建立索引就全部常驻 Python 内存。检索时，Chroma 返回有限数量的候选身份，程序再按仓库相对路径和起止行号从 SQLite 逐条读取并核对内容哈希。这样避免为了验证少量候选而先把全部代码块文本装进内存。旧版 Chroma 记录可能已有重复源码；下一次同步时会清空这些旧文本并保留原向量，不需要重新请求 Embedding。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. Embedding 输出要通过有限值和维度核验
源码原文：[agentforge/embeddings/base.py，第 51–89 行](../../agentforge/embeddings/base.py#L51-L89)
```python
def normalize_vector(
    vector, *, expected_dimension: int | None = None, allow_zero: bool = False,
) -> tuple[float, ...]:
    if isinstance(vector, (str, bytes, bytearray)):
        raise EmbeddingError("Embedding output must be a non-empty sequence of numbers.")
    try:
        values = tuple(vector)
    except Exception:
        raise EmbeddingError("Embedding output must be a non-empty sequence of numbers.") from None
    if not values:
        raise EmbeddingError("Embedding output cannot be empty.")
    if len(values) > MAX_VECTOR_DIMENSION:
        raise EmbeddingError(f"Embedding dimension exceeds {MAX_VECTOR_DIMENSION}.")
    # 【学习区补充】文档向量和查询向量维度必须一致，否则相似度没有可比基础。
    if expected_dimension is not None and len(values) != expected_dimension:
        raise EmbeddingError(
            f"Embedding dimension mismatch: expected {expected_dimension}, received {len(values)}."
        )
    checked = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise EmbeddingError("Embedding values must be real numbers.")
        try:
            # 【学习区补充】先统一数值类型，随后才能一致地检查有限值、范数和维度。
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            raise EmbeddingError("Embedding values must be finite real numbers.") from None
        # 【学习区补充】拒绝 NaN 或无穷数，否则相似度排序可能得到不可用结果。
        if not isfinite(number):
            raise EmbeddingError("Embedding values must be finite.")
        checked.append(number)
    try:
        norm = sqrt(fsum(value * value for value in checked))
    except (OverflowError, ValueError):
        raise EmbeddingError("Embedding vector norm is not finite.") from None
    if not isfinite(norm):
        raise EmbeddingError("Embedding vector norm is not finite.")
    if norm == 0.0:
        if allow_zero:
            return tuple(0.0 for _ in checked)
        raise EmbeddingError("Zero vectors cannot be stored as embeddings.")
    # 【学习区补充】归一化到单位长度，让后续比较更关注方向而非向量模长。
    return tuple(value / norm for value in checked)
```

**逐段讲解。** 向量维数不符、含非有限数或全零都不能进入相似度计算。合法向量只是可比较的数值表示，不意味着检索命中正确。

### 2. 统一包装查询与文档向量生成
源码原文：[agentforge/embeddings/base.py，第 92–111 行](../../agentforge/embeddings/base.py#L92-L111)
```python
def embed_text(
    provider: EmbeddingProvider, text: str, *, role: str | None = None,
) -> tuple[float, ...]:
    if not isinstance(text, str) or not text.strip():
        raise EmbeddingError("Text to embed must not be empty.")
    if role not in (None, "query", "document"):
        raise EmbeddingError("Embedding role must be 'query', 'document', or None.")
    _, dimension = provider_identity(provider)
    try:
        method_name = {"query": "embed_query", "document": "embed_document"}.get(role)
        # 【学习区补充】有 query/document 专用接口就按角色调用；没有时才使用通用 embed。
        method = getattr(provider, method_name, None) if method_name else None
        output = method(text) if callable(method) else provider.embed(text)
    except EmbeddingError as exc:
            # 项目级 EmbeddingError 消息由各个 Provider 控制长度并脱敏；
            # 同时保留本地 HTTP 状态或超时等有用细节，
            # 并继续屏蔽 Provider 代码抛出的任意异常内容。
        raise EmbeddingError(f"Embedding provider failed: {exc}") from None
    except Exception as exc:
        raise EmbeddingError(f"Embedding provider failed ({type(exc).__name__}).") from None
    return normalize_vector(output, expected_dimension=dimension)
```

**逐段讲解。** 输入和提供者维度先验证，随后按 query/document 角色调用，并将结果归一化。两侧必须处于兼容向量空间。

### 3. 同步代码向量时不复制源码全文
源码原文：[agentforge/embeddings/chroma_index.py，第 113–141 行](../../agentforge/embeddings/chroma_index.py#L113-L141)
```python
            vector = embed_text(self.provider, document, role="document")
            # 【学习区补充】省略源码正文；适配层会在 Chroma 中明确写入空文档，避免旧文本残留。
            self.store.upsert(source, vector)
            embedded += 1
```

**逐段讲解。** Embedding 仍然要读取 SQLite 中的完整代码块来生成向量，但写入 Chroma 时仅保留向量和元数据。SQLite 文件大小不会因此消失，因为它仍需作为原文证据库；变化是 Chroma 不再保存第二份代码文本。

### 4. 只回查向量命中的 SQLite 代码块
源码原文：[agentforge/embeddings/chroma_index.py，第 143–188 行](../../agentforge/embeddings/chroma_index.py#L143-L188)
```python
        def get_current(source):
            key = _chunk_key(source.file_path, source.start_line, source.end_line)
            if key not in current:
                # 【学习区补充】只按候选身份查这一块，不再遍历并缓存整仓库的所有正文。
                current[key] = self.index_store.get_chunk(
                    source.file_path, source.start_line, source.end_line)
            return current[key]

        found = []
        for source_type in ("code", "document"):
            candidates = self.store.search_with_documents(query_vector,
                repository_id=repo_id, model_id=self.model_id, source_type=source_type,
                limit=top_k, verify_source=lambda source: self._verify_source(source, get_current),
                include_documents=False)
            for source, distance, _document in candidates:
                row = get_current(source)
                if row is None:
                    continue
                score = max(-1.0, min(1.0, 1.0 - distance))
                source_id = "F" + hashlib.sha256(source.file_path.encode("utf-8")).hexdigest()[:20]
                found.append(VectorCandidate(source_id, source.file_path, "chunk",
                    source.start_line, source.end_line, row["text"], source.content_hash,
                    self.model_id, None, score))
```

**逐段讲解。** Chroma 只返回有限个候选的身份、元数据和距离，不传回重复源码。`get_chunk` 再从当前仓库 SQLite 精确读取对应行范围；路径、行号和内容哈希仍由应用核验。因此，向量候选不能取代当前源码，也不能仅凭相似度成为证据。

## 向量检索的结果仍然是待核验候选

Embedding Provider 把文本转换为固定维度的数字向量；文档和查询必须使用兼容的模型、预处理和维度，距离计算才有意义。向量值还要检查长度、有限数值和资源边界，不能接受 NaN、无穷值或异常维度后继续写入。缓存也要绑定模型身份和内容哈希，否则换了模型或文本却复用旧向量，会造成难以发现的检索错误。

Chroma 根据向量返回相似记录，但向量库不是源码权威。候选需通过 repository_id、路径、chunk_id 和内容哈希回到 SQLite 当前快照核验。文件被更新或删除时，旧向量即使暂时存在，也不能绕过当前内容检查成为有效证据。关闭向量检索时，词法和符号路径仍可工作，系统不应假装向量搜索成功。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `vector` | 待校验和规范化的数字序列。 | 每个值需要是有限数字，且长度满足模型维度要求。 |
| `expected_dimension` | 期望向量长度，可选。 | 与模型输出维度不符时拒绝写入，避免不同空间的向量混用。 |
| `allow_zero` | 是否允许全零向量，默认不允许。 | 零向量没有有意义的方向信息；只有特定场景才应显式允许。 |
| `provider`、`text` | Embedding 服务实现和待向量化文本。 | 查询和文档应使用兼容的 Provider、模型与预处理。 |
| `role` | 文本用途，例如 query 或 document，可选。 | 有些模型会按用途添加不同前缀；必须按模型约定传值。 |
| `top_k` | 向量检索最多返回候选数，示例默认 10。 | 只控制返回数量，不代表相关性门槛或拒答阈值。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
