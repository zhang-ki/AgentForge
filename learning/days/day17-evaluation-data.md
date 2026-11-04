# Day 17｜评测标签、数据来源与 Validation

> 阅读方式：本篇已直接嵌入关键源码和逐段解释。先读讲解，再看[本页源码摘录](#本页源码原文与逐段讲解)；源码文件链接只是可选的完整上下文，不需要来回切换。

## 评测集也需要可信来源

评测集不是一组随意问题。每个问题要有预期相关路径或明确无证据范围，标签要说明是从 AST/源代码生成、AI 暂定，还是人工审核。自动生成的标签可能不完整；若把它写成“人工 ground truth”，数字就会误导使用者。

Calibration 与 Validation 的作用也不同。开发者可以用 Calibration 集调整方法；Validation 应在方法固定后独立评估。若看了 Validation 错例就改规则，再用同一个 Validation 宣布通过，相当于让答案泄露给了调参过程。相似改写或同一事实的多个问题也应作为一个组隔离，避免一份内容同时出现在两侧。

## 这部分实现已直接摘录在本页

先读上方概念说明，再按本页后半的源码摘录编号阅读。每段代码后都有逐段讲解，说明关键输入、判断、返回结果以及它与前后模块的关系。原文件链接只用于需要查看函数外围完整上下文时；不点击链接也能完成本课阅读。

## 为什么冻结很重要

当结果不理想时，首先应记录失败、检查实现和理解指标；不能改 Validation 问题、正例或阈值来让百分比变好看。冻结数据让前后对比具有意义，也使另一个人能按相同输入复核结论。
## 本页源码原文与逐段讲解

当天真正需要读的关键代码已直接摘录在本课中。代码语句摘自当前工作区源码；带有“【学习区补充】”标记的行是贴近关键语句的教学注释，不属于源码。路径和行号定位原文件，先读本页就能理解主要实现，不必来回切换。

### 1. 固定计划阻止训练与验证串题
源码原文：[agentforge/evaluation.py，第 130–192 行](../../agentforge/evaluation.py#L130-L192)
```python
def validate_split_plan(dataset: dict, plan: dict) -> dict:
    """验证按组隔离的拆分计划，不把待复核标签视为已批准标签。"""
    queries = _validate_dataset(dataset)
    if dataset["dataset_version"] != REVIEW_POOL_DATASET_VERSION:
        raise ValueError("Split plans require the v2 review candidate pool.")
    if not isinstance(plan, dict) or plan.get("dataset_version") != dataset["dataset_version"]:
        raise ValueError("Split plan dataset version does not match the candidate pool.")
    # 【学习区补充】待人工审核的拆分不能伪装成已审核 ground truth。
    if plan.get("status") != "proposed_pending_human_review":
        raise ValueError("Split plan must remain proposed_pending_human_review.")
    splits = plan.get("splits")
    if not isinstance(splits, dict) or set(splits) != {"calibration", "validation"}:
        raise ValueError("Split plan needs calibration and validation lists.")
    query_by_id = {item["id"]: item for item in queries}
    assigned = []
    group_splits = {}
    summary = {}
    for split_name in ("calibration", "validation"):
        ids = splits[split_name]
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            raise ValueError(f"{split_name} split must be a list of query IDs.")
        # 【学习区补充】每个拆分先拒绝重复 ID，避免同一查询被重复计入指标。
        if len(ids) != len(set(ids)):
            raise ValueError(f"{split_name} split contains duplicate query IDs.")
        unknown = set(ids) - set(query_by_id)
        if unknown:
            raise ValueError(f"{split_name} split contains unknown query IDs.")
        assigned.extend(ids)
        rows = [query_by_id[query_id] for query_id in ids]
        for row in rows:
            # 【学习区补充】同一泄漏组固定在一个 split，防止相似查询同时进入校准与验证。
            previous = group_splits.setdefault(row["leakage_group"], split_name)
            if previous != split_name:
                raise ValueError("Leakage group appears in both calibration and validation splits.")
        summary[split_name] = {
            "query_count": len(rows),
            "evidence_count": sum(not row.get("no_evidence", False) for row in rows),
            "no_evidence_count": sum(row.get("no_evidence", False) for row in rows),
            "near_domain_negative_count": sum(
                row.get("no_evidence", False) and row.get("difficulty") == "near_domain"
                for row in rows
            ),
            "leakage_group_count": len({row["leakage_group"] for row in rows}),
        }
    if len(assigned) != len(set(assigned)):
        raise ValueError("A query appears in both calibration and validation splits.")
    # 【学习区补充】要求冻结池中的每条查询恰好分配一次，不能静默丢弃难例。
    if set(assigned) != set(query_by_id):
        raise ValueError("Split plan must assign every candidate exactly once.")
    expected = plan.get("expected_distribution")
    if not isinstance(expected, dict):
        raise ValueError("Split plan must record its expected distribution.")
    for split_name, values in summary.items():
        recorded = expected.get(split_name)
        if not isinstance(recorded, dict) or recorded != {
            "query_count": values["query_count"],
            "evidence": values["evidence_count"],
            "no_evidence": values["no_evidence_count"],
            "near_domain_negative": values["near_domain_negative_count"],
        }:
            raise ValueError(f"{split_name} expected distribution does not match assignments.")
    return {
        "status": plan["status"],
        "dataset_version": dataset["dataset_version"],
        "groups_are_disjoint": True,
        "summary": summary,
    }
```

**逐段讲解。** 查询和语义分组映射必须完整一致，同组不可跨 split；固定哈希用于确认输入没有在评测后被改写。

### 2. 标签来源需通过 manifest 和哈希回查
源码原文：[agentforge/provisional.py，第 36–42 行](../../agentforge/provisional.py#L36-L42)
```python
def _verified_source(manifest: dict, path_field: str, hash_field: str) -> tuple[Path, dict]:
    path = _source_path(manifest.get(path_field))
    expected = manifest.get(hash_field)
    actual = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    # 【学习区补充】用哈希检查冻结来源是否改变，避免评测意外读取被替换的数据。
    if not isinstance(expected, str) or actual != expected.upper():
        raise ValueError(f"Frozen provisional source hash mismatch: {path.name}.")
    return path, _read_json(path)
```

**逐段讲解。** 来源文件缺失或内容变了，候选就不能作为已核实标签。自动候选、人工复核和冻结 ground truth 是不同状态。

### 3. 评测拆分来自显式计划
源码原文：[agentforge/provisional.py，第 152–161 行](../../agentforge/provisional.py#L152-L161)
```python
def dataset_split(dataset: dict, split_name: str) -> dict:
    # 【学习区补充】只接受固定的两个数据拆分名称，阻止临时查询混入评估集。
    if split_name not in {"calibration", "validation"}:
        raise ValueError("split_name must be calibration or validation.")
    subset = copy.deepcopy(dataset)
    subset["dataset_version"] = PROVISIONAL_DATASET_VERSION
    subset["queries"] = [item for item in dataset["queries"] if item.get("split") == split_name]
    if not subset["queries"]:
        raise ValueError(f"Provisional {split_name} split is empty.")
    subset["evaluation_split"] = split_name
    return subset
```

**逐段讲解。** 运行时不随机重分题目，保证不同版本用相同查询比较；不能因某些题结果不好就挪到另一集合。

## 评测结果依赖标签和隔离方式

每条查询要有明确的目标证据、查询类型和标签来源；否则同一个 Recall 数字无法解释。冻结 split plan 把相近语义或同一功能族分到 Calibration 与 Validation 的固定集合，减少模型或规则通过记住相似样本而显得更好。清单哈希则用于证明评测运行读的是原来那批问题和标签。

Calibration 可以用于按预先规则选择研究参数，Validation 应留到选择完成后作一次独立检查。若看了 Validation 失败案例后再改规则并反复报告同一个 Validation 分数，它就不再是独立验证。AI 暂定标签、自动 AST 标签和人工审核标签也必须分开注明，样本数量多不能替代标签质量。

## 参数和字段说明

| 名称 | 它控制什么 | 改动后的影响 |
|---|---|---|
| `dataset` | 冻结的查询、正例、负例和来源标签集合。 | 它定义要测什么；不能为了通过而修改既定 Validation 标签。 |
| `plan` | 固定的拆分和哈希计划。 | 验证它能否与数据集相符；不相符时不应继续混合计算。 |
| `manifest` | 记录来源文件、哈希和标签出处的清单。 | 用于核对文件是否仍是同一版本，证明运行读取的是哪份数据。 |
| `path_field`、`hash_field` | manifest 中路径字段名和哈希字段名。 | 让校验器按指定字段读取来源，不用猜测结构。 |
| `split_name` | 要取出的 Calibration 或 Validation 名称。 | 明确选择一组查询；两组结果应分开报告。 |

**把这一课放回主流程。** 阅读每段时依次确认它接收什么、做了哪些允许/拒绝判断、产出的数据交给哪一层。这样你是在本页跟着真实实现走，而不是看到函数名后自己去仓库里找答案。
