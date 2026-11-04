# Day 26｜如何读性能与质量数字

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 每个数字都需要一个测量范围

“查询用了 50 毫秒”要知道测的是词法检索、完整问答还是模型生成；“覆盖率 90%”要知道测量了哪些生产文件、是否启用分支统计；“Recall@5 80%”要知道数据集、正例定义、查询数量和标签来源。数字没有这些上下文就无法复核，也不能直接推广到另一个仓库或环境。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 测量、推断和未知

真实测量是按给定流程观察到的数据；推断是根据实现推测的行为；未测量表示没有足够数据。三者应分别表述。一次小型合成仓库实验只能说明该样本，不足以说明大型真实仓库的延迟、内存或并发表现。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 先验证资源配置再讨论性能
源码原文：[agentforge/scanner.py，第 51–55 行](../../agentforge/scanner.py#L51-L55)
```python
    def __post_init__(self) -> None:
        # 【学习区补充】资源上限必须为正数，否则扫描器会失去可预测的边界。
        if self.max_file_bytes <= 0 or self.max_total_bytes <= 0:
            raise ValueError("byte limits must be greater than zero")
        if self.max_files <= 0 or self.max_entries <= 0:
            raise ValueError("file and entry limits must be greater than zero")
```

**逐段讲解。** 无效上限在工作开始前明确失败，不会把配置问题误判为扫描慢或结果差。

### 2. 性能和质量数字必须有共同统计口径
源码原文：[agentforge/evaluation.py，第 305–331 行](../../agentforge/evaluation.py#L305-L331)
```python
def _metrics(rows: list[tuple[list[str], set[str]]]) -> dict:
    # 【学习区补充】无有效样本时指标应为未测量，而不是返回看似有意义的零。
    if not rows:
        return {
            "scored_query_count": 0,
            "valid_label_count": 0,
            "recall_at": {str(k): None for k in RECALL_CUTOFFS},
            "mrr": None,
            "aggregation": "macro average over queries with non-empty ground truth",
        }
    recall_sums = {k: 0.0 for k in RECALL_CUTOFFS}
    reciprocal_ranks = []
    label_count = 0
    for ranked, relevant in rows:
        label_count += len(relevant)
        unique_ranked = list(dict.fromkeys(ranked))
        for cutoff in RECALL_CUTOFFS:
            recall_sums[cutoff] += len(set(unique_ranked[:cutoff]) & relevant) / len(relevant)
        # 【学习区补充】MRR 看第一个相关结果的倒数排名；未命中时该查询贡献零。
        first = next((rank for rank, key in enumerate(unique_ranked, 1) if key in relevant), None)
        reciprocal_ranks.append(0.0 if first is None else 1.0 / first)
    count = len(rows)
    return {
        "scored_query_count": count,
        "valid_label_count": label_count,
        "recall_at": {str(k): round(recall_sums[k] / count, 6) for k in RECALL_CUTOFFS},
        "mrr": round(sum(reciprocal_ranks) / count, 6),
        "aggregation": "macro average over queries with non-empty ground truth",
    }
```

**逐段讲解。** 指标依赖固定数据、相关性规则和排名；报告分母、范围、软件版本与排除条件才可以比较。

### 3. 缓存是否可复用看内容和模型身份
源码原文：[agentforge/embeddings/chroma_index.py，第 66–80 行](../../agentforge/embeddings/chroma_index.py#L66-L80)
```python
    def _has_current(self, source):
        from .chroma_store import _identity
        existing = self.store.collection.get(ids=[_identity(source)], include=["metadatas"])
        metadata = existing.get("metadatas") or []
        if not metadata:
            return False
        row = metadata[0]
        # 【学习区补充】缓存只有在仓库、来源类型、内容哈希和模型身份等都匹配时才可复用。
        return (row.get("repository_id") == source.repository_id
                and row.get("source_type") == source.source_type
                and row.get("chunk_id") == source.chunk_id
                and row.get("file_path") == source.file_path
                and row.get("content_hash") == source.content_hash
                and row.get("model_id") == source.model_id
                and row.get("start_line") == source.start_line
                and row.get("end_line") == source.end_line)
```

**逐段讲解。** 来源块哈希、仓库和 Embedding Provider 身份都要匹配。源码或模型一变，旧向量不代表当前内容，应该重新生成。

## 先确认测了什么，再看数值大小

扫描上限是安全配置，不能为了让大仓库跑得更快或覆盖更多文件而随意放宽。性能结果还需要说明仓库大小、硬件、缓存状态、是否包含模型推理以及重复次数；没有这些条件，单次耗时不适合横向比较。Recall 和 MRR 衡量检索排序，覆盖率衡量执行路径，延迟衡量耗时，它们不可相互代替。

缓存只有在仓库身份、文件内容、向量模型和相关配置都匹配时才有复用价值。对照命中率时，也应检查缓存是否只是隐藏了向量服务调用或保留了过期结果。对于样本不足、服务未实际调用或测试被跳过的指标，状态应是“未测量”，而不是填一个看起来完整的数字。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `ScanLimits` 配置字段 | 扫描可用的文件数量、深度和字节资源上限。 | 它们是安全边界，不只是性能开关；调大可能让任务耗时和资源占用上升。 |
| `rows` | 指标计算使用的逐查询排名和标签记录。 | 统计前要确认 split、适用查询和正例来源一致。 |
| `source` | 要判断是否仍为当前版本的向量来源记录。 | 通过仓库、文件、内容哈希和模型身份等检查后才可复用缓存。 |
| `content_hash`、`model_id`、维度等字段 | 描述向量对应的输入和生成模型。 | 任一关键身份改变都可能使旧向量失效，需要重新计算而非静默复用。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
