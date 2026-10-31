# AF-009G：独立仓库泛化评测报告

日期：2026-10-26  
结论：离线 Lexical/Symbol/RRF 评估完成；真实 Vector、阈值迁移和含 Vector 的 RRF 未测量。保留 RRF 原型，不进入产品配置。

## 数据与可复现性

- 语料：`tests/fixtures/retrieval_eval_generalization_repo/`，Lumen Gallery 预约、访客、藏品保护业务的独立人工合成快照；13 个文件，Python 代码未执行或导入。语料清单 SHA-256：`f187d406263d2a3a44cf234cbed128d1fbecc4cf973a187f0b837e4c1b7ce449`。
- 查询集：`tests/fixtures/retrieval_eval_generalization_v1.json`，40 个唯一 ID；20 个带证据正例，20 个“该快照中没有足够相关证据”的负例（19 个困难近域、1 个域外）。25 个正例行范围由程序针对冻结语料核验。标签均为 `review_method=ai_evidence_review`、`human_reviewed=false`、`status=provisional_ai`，不是人工 ground truth。
- 评测集 SHA-256：`607C0BE6DB9CAB0003E6D57A14E028758B27C248F79F125F6E818861F4D8CBF1`；split plan SHA-256：`78E367FCEC916C0BF062C8433367ADF5FC41E3BD4713CB4541AC9C8B69D12FDD`。
- Split：`tests/fixtures/retrieval_eval_generalization_split_v1.json`。10 个 leakage group 整组分配，Calibration/Validation 各 20 条、各 10 正例与 10 负例；Calibration 10 个近域负例，Validation 9 个近域和 1 个域外负例。没有跨组或跨 split 重复，也没有根据检索结果重分组。
- 固定设置：RRF `k=60`；Validation 只评估一次且不用于调参。使用相同的 13 文件内存 SQLite 索引、40 条查询、标签及评测入口。Symbol 仅在存在精确符号过滤的问题上适用。无证据查询不进入 Recall/MRR。
- 复现离线结果：`python -X utf8 -B -m agentforge.eval_generalization --vector-unavailable-reason "Server log reported loopback listening, but the local TCP readiness probe could not connect; no embedding HTTP request was sent. The test environment also denied OS listener-table queries."`。输出保存于独立的 `AF009G_GENERALIZATION_RESULTS.json`，索引为 SQLite `:memory:`。

指标按每条有非空相关标签的正例查询计算宏平均：Recall@K 是该查询前 K 个去重结果中相关文件数除以该查询相关文件数，再对适用查询取平均；MRR 是首个相关文件倒数排名的宏平均。无证据返回率分母是该检索器适用的负例数；它描述候选返回，不等同于拒答正确率。查询耗时只代表本次极小内存合成语料单次测量，不是稳定性能基准。

## 结果

### Calibration（20 条：10 正、10 负）

| 检索器 | 适用正例查询数 | Recall@1 | Recall@5 | Recall@10 | MRR | 检索总耗时 |
|---|---:|---:|---:|---:|---:|---:|
| Lexical | 10 | 0.95 | 1.00 | 1.00 | 1.00 | 0.063386 秒 |
| Symbol | 8 | 1.00 | 1.00 | 1.00 | 1.00 | 0.000682 秒（13 次适用符号查询计时） |
| Vector | 未测量 | — | — | — | — | — |
| RRF（Lexical + Symbol 可用通道，k=60） | 10 | 0.95 | 1.00 | 1.00 | 1.00 | 0.046989 秒 |

负例非空候选：Lexical 10/10、RRF 10/10；Symbol 仅适用 5 条精确缺失符号查询，返回 0/5。Vector 未测量。

### Validation（20 条：10 正、10 负）

| 检索器 | 适用正例查询数 | Recall@1 | Recall@5 | Recall@10 | MRR | 检索总耗时 |
|---|---:|---:|---:|---:|---:|---:|
| Lexical | 10 | 0.80 | 1.00 | 1.00 | 1.00 | 0.060885 秒 |
| Symbol | 7 | 1.00 | 1.00 | 1.00 | 1.00 | 0.000441 秒（12 次适用符号查询计时） |
| Vector | 未测量 | — | — | — | — | — |
| RRF（Lexical + Symbol 可用通道，k=60） | 10 | 0.80 | 1.00 | 1.00 | 1.00 | 0.049048 秒 |

Validation 负例候选返回：Lexical 和 RRF 均为 9/10；近域负例 9/9，域外负例 0/1。Symbol 在 5 条适用的精确缺失符号查询中返回 0/5，其他负例不适用。RRF 与 Lexical 的 Recall@1、Recall@5、Recall@10 和 MRR 完全相同；没有观察到独立 Validation 排序改善。

Validation 类别结果：两个 `business_to_implementation` 查询和两个 `cross_file_call` 查询的 Lexical/RRF Recall@1 均为 0.50、Recall@5/10 与 MRR 为 1.00；一个文档规则查询和五个精确符号查询 Recall@1 均为 1.00。Symbol 对业务概念及文档规则不适用。小样本的类别比例不支持一般化结论。

## 本地 Embedding 预检与阻塞

复核了既有 Qwen3-Embedding-0.6B Q8_0 模型（本地 SHA-256 `06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439`）以及既有 llama.cpp EXE/DLL 哈希；运行时复制到 AgentForge `tests/.tmp` 下的隔离目录，未复制 GGUF。副本包含 51 个文件且 SHA/大小清单与源运行时一致。

受控启动尝试中，PID 32924 的首次监听观察未取得可恢复的输出；后续 PID 33232 的内存日志报告模型加载完成并报告 `127.0.0.1:58067` 监听，但本地 TCP readiness 连接失败。执行环境同时拒绝查询操作系统监听表。没有发送 `/v1/embeddings` 或任何其他 HTTP 请求，因此真实 Vector、真实向量召回、阈值分数及含 Vector 的 RRF 均为**未测量**。两个本轮 PID 均已停止，`Get-Process -Name llama-server` 未发现进程，临时运行时副本已清理；本次最终 TCP 检查被权限错误 10013 阻止，不能独立确认端口状态。未发送聊天或生成请求。模型日志中的监听文字不等于客户端可用。

## 阈值与产品决策

AF-009F-A 的历史 provisional Validation 在 `top_score >= 0.70` 时为 20 个样本（10 正、10 负）：覆盖率 0.10、误接受率 0.00、误拒绝率 0.80、接受结果精确率 1.00。该数字属于先前数据集，仅作历史参照。

AF-009G 没有真实向量分数，Calibration 和 Validation 的 0.70 覆盖率、误接受率、误拒绝率及精确率全部未测量；不存在本轮可比较的阈值差异。未选择或启用生产阈值。由于 Vector 通道缺失，RRF 只组合 Lexical 与适用 Symbol 结果；独立 Validation 未改善 Lexical，因此保留既有显式 RRF 原型，不增加产品配置或改变默认 Retriever。

## 测试与完整性

- 定向命令 `python -X utf8 -B -m unittest tests.test_generalization tests.test_evaluation tests.test_provisional_fusion -v`：29 项通过、0 失败、0 跳过，退出码 0。
- 完整命令 `python -X utf8 -B -m unittest discover -s tests -v`：124 项运行、119 项通过、0 失败、5 项跳过，退出码 0。5 项因当前 Windows 权限不能创建物理符号链接、junction 或硬链接而跳过，不计为通过。首次完整运行发现 1 项旧错误消息断言回归；仅恢复兼容的错误消息措辞后，定向与完整套件均通过。
- 正式 `.agentforge/data/index.sqlite3`：32,768 字节，SHA-256 `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296`。
- `read_only_evidence.json`：2,885 字节，SHA-256 `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733`。
- 新结果文件 `AF009G_GENERALIZATION_RESULTS.json`：178,223 字节，SHA-256 `E6161199EC0CF13C8A5E1A122B943D766556A730AFDEC1937083F4A9635861C0`。
- 原 v1、AF-009D/009E/009E-F/009F-A fixtures 与 AF009FA 结果/服务记录在本轮前后 SHA-256 一致；完整哈希见执行记录。临时运行时副本已清理，测试临时数据已清理；测试目录中原已存在的 `tests/.tmp/af009c-llama-server` 未触碰。
- 未访问 Agentexpr；未启动真实聊天推理、未访问网络、未安装依赖、未执行 Git 操作。

## 结论与后续

AF-009G 的独立 fixture、AI 暂定数据、group-isolated split 以及 Lexical/Symbol/离线 RRF 对比已完成，但由于本地 TCP 阻塞，完整向量泛化验收未完成。标签不是人工审核结果，40 条样本仍只是小规模合成集。生产拒答阈值不通过；RRF 不值得在本轮进入显式产品配置；reranker 未实施。下一步应先修复或另行排除受限环境中的 loopback TCP 通信阻塞，在确认仍使用同一已核验模型/运行时后，对固定数据执行一次真实向量评估，并保留 Vector/RRF 和阈值结果；若要作正式产品决策，还需人工审核标签和更广泛、独立的仓库数据。AF-010 仍需单独审批。阶段 2B-3S 的真实 RAG 回答与引用验收仍为未知、未通过且未重试。
