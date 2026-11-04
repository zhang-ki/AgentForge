# Day 18｜RRF 为什么融合排名而不是分数

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 不同通道的分数不在同一把尺子上

词法通道的分数可能来自关键词覆盖，向量通道的分数来自距离或相似度，符号通道可能用结构匹配优先级。把这些原始分数直接相加，相当于把摄氏度和米数相加，数值虽然算得出来却没有统一意义。

RRF（Reciprocal Rank Fusion）只看每个通道的排序位置。某个文档在一个通道排得越靠前，就从该通道获得越大的排名贡献；各通道贡献相加后得到融合顺序。这样避免比较不同通道分数尺度，但没有解决“通道是否相关”或“融合是否增加噪声”的问题。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 为什么保留但不默认使用

融合可能提高某些查询的排名，也可能增加错误候选、挤占证据预算或损害无证据问题的拒答。是否适合产品，要看独立 Validation、互补证据、错误候选和实际用户目标。某个合成案例变好不能证明整体收益；这就是研究实现与产品配置之间的区别。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. RRF 融合排名，不把不同量纲硬加
源码原文：[agentforge/fusion.py，第 51–100 行](../../agentforge/fusion.py#L51-L100)
```python
def fuse_ranked_results(channels: dict[str, list], *, k: int = 60, top_k: int = 10) -> list[FusedCandidate]:
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 10_000:
        raise ValueError("RRF k must be an integer from 1 to 10000.")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}.")
    if not isinstance(channels, dict) or any(
        not isinstance(name, str) or not name or not isinstance(results, list)
        for name, results in channels.items()
    ):
        raise ValueError("Fusion channels must map non-empty names to ranked lists.")

    accumulated = {}
    representatives = {}
    contributions = {}
    best_rank = {}
    # 【学习区补充】固定遍历顺序，让同一组通道输入得到可复现的融合结果。
    for channel in sorted(channels):
        seen = set()
        rank = 0
        for result in channels[channel]:
            identity = _identity(result)
            # 【学习区补充】同一候选在一个通道里只算一次，重复结果不能人为抬高融合分。
            if identity in seen:
                continue
            seen.add(identity)
            rank += 1
            # 【学习区补充】每个通道按名次贡献倒数分值，不把原始分数跨通道相加。
            contribution = 1.0 / (k + rank)
            accumulated[identity] = accumulated.get(identity, 0.0) + contribution
            contributions.setdefault(identity, []).append(RankContribution(
                channel=channel, rank=rank, original_score=_score(result), rrf_score=contribution,
            ))
            if identity not in representatives or rank < best_rank[identity]:
                representatives[identity] = result
                best_rank[identity] = rank

    ordered = sorted(accumulated, key=lambda identity: (
        -accumulated[identity], best_rank[identity], identity.casefold(),
    ))
    fused = []
    for identity in ordered[:top_k]:
        result = representatives[identity]
        path = _field(result, "relative_path")
        fused.append(FusedCandidate(
            identity=identity,
            relative_path=path,
            start_line=int(_field(result, "start_line", 1)),
            end_line=int(_field(result, "end_line", _field(result, "start_line", 1))),
            text=str(_field(result, "text", "")),
            score=accumulated[identity],
            channels=tuple(sorted(contributions[identity], key=lambda item: item.channel)),
        ))
    return fused
```

**逐段讲解。** 每个通道先贡献排名位置，再用 rank 公式汇总；结果保留每个通道的 rank 和贡献，可追查候选为什么靠前。

### 2. 显式研究类不等于默认产品功能
源码原文：[agentforge/fusion.py，第 116–126 行](../../agentforge/fusion.py#L116-L126)
```python
    def search(self, query: str, *, top_k: int = 10, symbol_query: dict | None = None):
        channels = {"lexical": lexical_search(self.store, query, self.source_top_k)}
        channels["symbols"] = (
            self.store.search_symbols(**symbol_query, limit=self.source_top_k)
            if symbol_query else []
        )
        channels["vector"] = (
            self.vector_retriever.search(query, top_k=self.source_top_k)
            if self.vector_retriever is not None else []
        )
        # 【学习区补充】只有显式放入 channels 的通道会参与融合；缺失通道不会被伪装成成功。
        return fuse_ranked_results(channels, k=self.k, top_k=top_k)
```

**逐段讲解。** 它接收多个通道并调用融合函数。是否默认启用须检查产品构造路径，不能因类存在就推断 ask 已经采用 RRF。

## RRF 解决的是不同分数难以直接比较

词法分数、向量距离和符号命中通常不在同一量纲。RRF 不把这些原始分数直接相加，而是按每个通道的排名给贡献，常见形式为 `1 / (k + rank)`，然后把同一文件来自多个通道的贡献累加。这样可将“在多个列表中都靠前”的候选提到前面，但结果会受通道、候选深度、去重身份和常数 k 影响。

RRF 只改变候选排序，不会补出某个通道根本没找到的证据，也不会判断候选是否支持答案。对于无证据问题，它可能照样排出看似靠前的结果。项目保留显式研究实现和评测入口，但历史评测没有证明它稳定优于默认路由，因此不能把“代码存在”说成“产品默认启用”或“效果已证明”。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `channels` | 通道名到各自排名结果列表的映射。 | 缺失通道按空列表处理；每条列表里的排名会产生融合贡献。 |
| `k` | RRF 公式里的平滑常数，默认 60。 | 它调节排名差距对贡献的影响；改变它会改变融合排序，不会自动提高准确率。 |
| `top_k` | 融合后最多保留的候选数，默认 10。 | 与各通道输入列表深度不同；它只截定最终输出长度。 |
| `query`、`symbol_query` | 原始查询和可选的结构化符号查询。 | 符号查询用于符号通道；没有它时该通道可能没有可用结果。 |
| `original_score`、`rank`、`channel` | 候选在来源通道的分数、名次和通道身份。 | RRF 主要使用名次；保存原分数和通道是为了审计来源而非混加分数。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
