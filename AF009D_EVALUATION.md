# AF-009D 检索评测与无证据拒答实验

日期：2026-10-25

## 数据集身份与审核状态

- `agentforge-retrieval-v1` 保持逐字节不变，SHA-256 为 `B3276E4309C08FAA8B8961DB8DDD457A6ABA7491C134E606FC037B0B1342AF87`。它包含 9 条既有人工标签：8 条有证据、1 条无证据。
- `agentforge-retrieval-v1-expansion-candidates` 是独立候选文件，包含 12 条新查询：8 条有证据、4 条无证据。每条候选均有纳入/排除理由；正例还有仓库相对路径和起止行证据。全部标签状态均为 `pending_human_review`，不能并入已审核基线或用于生产阈值决策。
- 两个集合使用同一份 8 文件合成仓库。Lexical 与 Vector 在同一组已索引 chunk 上返回文件路径；Symbol 在同一仓库的结构索引上返回“路径 + 限定符号名”。因此 Symbol 的检索单位和适用查询数与另外两者不同，不直接解释为同一种候选排序。

## 指标协议

对每个有非空 ground truth 的查询 `q`，先按返回顺序对重复身份去重：

- `Recall@K(q) = |TopK(q) ∩ Relevant(q)| / |Relevant(q)|`。
- 总体 Recall@K 是上述查询级 Recall 的宏平均；多相关文档查询不会因标签更多而获得更高权重。
- `RR(q) = 1 / rank(first relevant)`；未返回任何相关项时为 0。MRR 是所有有标签查询 RR 的宏平均。
- 无证据查询不进入 Recall/MRR，单独统计候选返回、最高余弦分数、第一名与第二名分数差及阈值误接受。
- Symbol 只统计具有 `symbol_query` 和符号标签的查询；自然语言业务概念和纯文档查询标为不适用。

评测器现在在结果中明确记录审核状态、检索单位、候选范围、每条向量结果的分数、top score 和 top-2 margin。没有更改 v1 的标签或历史指标含义。

## 真实 Qwen3 Embedding 对比

模型固定为 `Qwen/Qwen3-Embedding-0.6B-GGUF` revision `370f27d7550e0def9b39c1f16d3fbaa13aa67728` 的 Q8_0，文件 SHA-256 `06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439`，输出 1024 维。Query 使用既有 `Instruct/Query` 格式，document 使用原始 chunk。所有向量在 SQLite `:memory:` 中生成与检索。

### 已审核 v1

| 检索器 | 有标签查询 | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---:|---:|---:|---:|---:|
| Lexical | 8 | 0.7500 | 0.9375 | 0.9375 | 0.9375 |
| Symbol | 7 | 0.928571 | 1.0000 | 1.0000 | 1.0000 |
| Vector | 8 | 0.6875 | 1.0000 | 1.0000 | 0.8750 |

Vector 在跨文件引用和重名符号类别的 Recall@1 均为 0、MRR 均为 0.5；业务概念查询 Recall@1 为 0.5、MRR 为 1；精确名称、限定名称和相似名称负例在该集合中首位命中。

### 待人工审核扩展候选

以下数字是探索性结果，不是已审核测试集成绩。

| 检索器 | 有标签候选 | Recall@1 | Recall@5 | Recall@10 | MRR |
|---|---:|---:|---:|---:|---:|
| Lexical | 8 | 0.9375 | 1.0000 | 1.0000 | 1.0000 |
| Symbol | 6 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| Vector | 8 | 0.8125 | 1.0000 | 1.0000 | 0.9375 |

Vector 对跨文件调用、重名符号和相似名称候选均首位命中。业务概念连续性查询的两个相关文件在 Top-5 内，Recall@1 为 0.5。业务概念到实现的查询把调用方 `pkg/handlers.py` 排在实现 `pkg/session.py` 前，首个标注相关文件位于第 2 名，MRR 为 0.5。这个差异也说明 relevance 定义需要人工复核：调用方是否也应纳入标签不能由模型输出决定。

## 无证据候选与阈值实验

当前 VectorRetriever 对每个查询都会返回候选。以下实验只定义 `top_score >= threshold` 为接受，不组合 margin，也不改变生产行为。

| 数据 | 有证据 top score | 无证据 top score | 有证据 margin | 无证据 margin |
|---|---|---|---|---|
| v1 | 0.385751–0.786229，均值 0.669110 | 0.314689（仅 1 条） | 0.006768–0.232071，均值 0.112241 | 0.025881 |
| 扩展候选 | 0.612283–0.769674，均值 0.690310 | 0.293590–0.496057，均值 0.374062 | 0.002411–0.214916，均值 0.096141 | 0.011559–0.119397，均值 0.043315 |

扩展候选的部分固定阈值结果：

| 阈值 | 覆盖率 | 误接受率 | 误拒绝率 | 接受结果精确率 |
|---:|---:|---:|---:|---:|
| 0.35 | 0.833333 | 0.50 | 0.00 | 0.80 |
| 0.40 | 0.750000 | 0.25 | 0.00 | 0.888889 |
| 0.45 | 0.750000 | 0.25 | 0.00 | 0.888889 |
| 0.50 | 0.666667 | 0.00 | 0.00 | 1.00 |
| 0.60 | 0.666667 | 0.00 | 0.00 | 1.00 |
| 0.65 | 0.583333 | 0.00 | 0.125 | 1.00 |

阈值 0.50 在扩展候选上形成完全分离，但不能选为生产阈值：标签尚未人工审核，只有 4 条无证据候选，而且同一阈值在已审核 v1 上会误拒绝 1/8 的有证据查询。Margin 分布也重叠明显，例如有证据最小 margin 低于无证据最小值，因此不能单独作为可靠拒答信号。

## 复现命令

离线词法/符号评测：

```powershell
python -X utf8 -B -m agentforge.eval_retrieval --dataset baseline
python -X utf8 -B -m agentforge.eval_retrieval --dataset expansion
```

在用户已经安全启动兼容的 loopback Embedding 服务后：

```powershell
python -X utf8 -B -m agentforge.eval_local_embeddings --base-url http://127.0.0.1:<port> --model-id <fixed-model-identity> --api-model <server-alias> --dimension 1024 --dataset baseline
python -X utf8 -B -m agentforge.eval_local_embeddings --base-url http://127.0.0.1:<port> --model-id <fixed-model-identity> --api-model <server-alias> --dimension 1024 --dataset expansion
```

## 结论

评测协议和观测字段已足以支持下一阶段设计讨论，但无证据阈值仍缺少人工审核和规模。进入 AF-010 前应先由人审核 12 条候选，尤其确认调用方/实现方的相关性边界；随后增加更多不同措辞、近域困难负例和多个合成模块，目标至少分别积累 20 条有证据与 20 条无证据样本，再使用保留验证集校准。AF-009D 没有实现或选择生产拒答规则，也没有实现融合或重排。
