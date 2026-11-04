# Day 16｜Recall@K 与 MRR 衡量什么

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 排名指标各自回答一个问题

假设一个查询的相关文件有三个。若前五名找到了其中两个，Recall@5 就反映“相关目标有多少被找回”；它不关心这两个是否排在第一、第二还是第五。MRR（Mean Reciprocal Rank）看第一个相关目标的位置：第一名贡献 1，第二名贡献 1/2，第三名贡献 1/3，再对查询求平均。它对用户先看到什么更敏感。

指标依赖数据定义。正例是文件、符号还是片段？同一文件多个块如何去重？一个查询能有多个正例吗？若分母不一致，百分比不能直接比较。Recall@K 和 MRR 只评价检索排序，不直接评价模型回答的语义正确性、引用质量或无证据拒答。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 结果应怎样解释

Recall@10 很高但 Recall@1 低，可能意味着相关内容能找到却排得靠后；MRR 低也会反映用户需要翻找。指标要连同数据集版本、仓库、标签来源和 Calibration/Validation 一起看。它们提供检索行为证据，不是“所有问答都正确”的证明。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. Recall@K 和 MRR 都以查询为单位
源码原文：[agentforge/evaluation.py，第 305–331 行](../../agentforge/evaluation.py#L305-L331)
```python
def _metrics(rows: list[tuple[list[str], set[str]]]) -> dict:
    # 【学习区补充】没有可计分查询时返回 None，避免把未测量误写成 0 分。
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
    # 【学习区补充】每个查询单独计算，再对查询做宏平均，避免大样本查询支配指标。
    for ranked, relevant in rows:
        label_count += len(relevant)
        unique_ranked = list(dict.fromkeys(ranked))
        for cutoff in RECALL_CUTOFFS:
            recall_sums[cutoff] += len(set(unique_ranked[:cutoff]) & relevant) / len(relevant)
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

**逐段讲解。** 函数对相关身份和排序逐查询计数；解释数字时要说明样本数、相关项定义与无证据查询处理方式。

### 2. 覆盖率与拒答错误不能只报一个比例
源码原文：[agentforge/evaluation.py，第 254–302 行](../../agentforge/evaluation.py#L254-L302)
```python
def evaluate_abstention(query_results: list[dict], thresholds=ABSTENTION_THRESHOLDS) -> dict:
    """描述仅按分数计算的拒答候选规则，不选择生产阈值。"""
    # 【学习区补充】只有有分数的查询进入阈值分母；报告需同时看有效样本数。
    rows = [row for row in query_results if row.get("top_score") is not None]
    # 【学习区补充】正例和无证据负例分开统计，才能区分误拒绝与误接受。
    positives = [row for row in rows if not row.get("no_evidence", False)]
    negatives = [row for row in rows if row.get("no_evidence", False)]
    threshold_rows = []
    for threshold in thresholds:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("Abstention thresholds must be numeric.")
        threshold = float(threshold)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("Abstention thresholds must be from 0 to 1.")
        accepted_positive = sum(row["top_score"] >= threshold for row in positives)
        # 【学习区补充】这个值是无证据样本中被阈值误接受的数量，与正例单独统计。
        accepted_negative = sum(row["top_score"] >= threshold for row in negatives)
        accepted = accepted_positive + accepted_negative
        threshold_rows.append({
            "threshold": round(threshold, 6),
            # 【学习区补充】coverage 的分母是全部有分数查询；precision 的分母则是被接受的查询。
            "coverage": round(accepted / len(rows), 6) if rows else None,
            "precision": round(accepted_positive / accepted, 6) if accepted else None,
            "false_accept_rate": round(accepted_negative / len(negatives), 6) if negatives else None,
            "false_reject_rate": round((len(positives) - accepted_positive) / len(positives), 6)
            if positives else None,
            "accepted_positive": accepted_positive,
            "accepted_no_evidence": accepted_negative,
        })
    return {
        "status": "exploratory_only",
        "decision_rule": "accept when top cosine similarity is greater than or equal to threshold",
        "sample_counts": {
            "total_with_scores": len(rows), "evidence": len(positives), "no_evidence": len(negatives),
        },
        "top_score": {
            "evidence": _signal_summary([row["top_score"] for row in positives]),
            "no_evidence": _signal_summary([row["top_score"] for row in negatives]),
        },
        "top_2_margin": {
            "evidence": _signal_summary([
                row["top_2_margin"] for row in positives if row.get("top_2_margin") is not None
            ]),
            "no_evidence": _signal_summary([
                row["top_2_margin"] for row in negatives if row.get("top_2_margin") is not None
            ]),
        },
        "thresholds": threshold_rows,
        "calibration_warning": (
            "Too few evidence/no-evidence examples for a production threshold; do not select or deploy one."
            # 【学习区补充】样本太少时给出限制提示，不把少量验证结果当成生产阈值依据。
            if len(positives) < 20 or len(negatives) < 20 else None
        ),
    }
```

**逐段讲解。** 固定规则下分别数覆盖、误接受、误拒绝和精确率。它们对应不同产品风险，不能以单个最好看的指标代替全部结果。

## Recall 与排序位置回答不同问题

Recall@K 关注相关结果是否进入前 K 名；MRR 关注第一个相关结果排得多靠前。一个系统可能 Recall@10 很高，却把最相关结果排在第九位，用户仍然要翻很多内容。指标通常以适用查询为单位统计，所以报告还要说明哪些查询有正例、哪些查询纳入分母，以及标签由什么方式产生。

无证据查询要单独观察候选返回和最终回答。检索器为“尽量找相关片段”而返回候选，不表示模型应该回答；相似度高也可能只是词语相似。覆盖率、误接受、误拒绝和精确率的分子分母要写清楚。排序质量和拒答能力是两条不同的验收线。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `rows` | 每条查询的排名列表与相关目标集合。 | 列表反映系统排序，目标集合来自标签；指标逐查询计算后再汇总。 |
| `query_results` | 每个查询的预测分数、候选和标签结果。 | 拒答指标依赖查询标签与实际是否回答，缺少任一项都不能可靠统计。 |
| `thresholds` | 要评估的一组固定判断阈值。 | 每个阈值会产生不同覆盖率和错误数；不能在看过 Validation 后挑最好看的一个。 |
| `Recall@K` 中的 `K` | 检查前 K 名是否出现相关结果。 | K 增大通常更容易包含正确候选，但不说明第一名排序好。 |
| `MRR` 的首个相关排名 | 每条查询第一个相关结果的位置。 | 排名越靠前贡献越大；无相关命中则该查询贡献为零。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
