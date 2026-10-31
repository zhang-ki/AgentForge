# AgentForge AF-010-PRE：独立评测、拒答策略与产品进入审查

日期：2026-10-26  
结论：P1–P4 均已完成。全新的 Northstar 冷链合成仓库、80 条 AI 暂定查询和固定 group-isolated split 已通过验证；真实 Qwen3-Embedding 评测完成。RRF 在独立 Validation 的 Recall@1/MRR 相对 Lexical 有小幅改善且 Top-5/10 无退化，但冻结的拒答策略误拒绝 11/20 个有证据查询，未达到预先声明的工程目标。因此 AF-010 产品实现继续暂缓；默认 Retriever、生产阈值和产品配置均未改变。

## P1：冻结与完整性基线

- 读取并核对 AF-009G-R4/R5 报告与结果、当前状态/路线图/架构/清单、评测器、Calibration 和 RRF 实现。
- 启动前与结束后对 43 个受保护路径建立字节数与 SHA-256 快照，包括全部历史 fixture/split/result、AF-009G-R5 结果、正式 SQLite、只读证据，以及本轮新 fixture 和 20 文件仓库。机器可读前后映射保存在 `AF010PRE_RESULTS.json`，两者完全一致。
- 关键冻结项：

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `AF009GR5_RESULTS.json` | 434,032 | `DABAA96701259C6AA21FE95BE3F113906CDED0A3F671AED33F419067B6F1E3A2` |
| `retrieval_eval_generalization_v1.json` | 50,993 | `607C0BE6DB9CAB0003E6D57A14E028758B27C248F79F125F6E818861F4D8CBF1` |
| `retrieval_eval_generalization_split_v1.json` | 3,819 | `78E367FCEC916C0BF062C8433367ADF5FC41E3BD4713CB4541AC9C8B69D12FDD` |
| `.agentforge/data/index.sqlite3` | 32,768 | `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296` |
| `.agentforge/data/read_only_evidence.json` | 2,885 | `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733` |

没有覆盖或改写任何历史评测结果。

## P2：独立评测集

- 新仓库 `tests/fixtures/retrieval_eval_preproduct_repo/` 包含 20 个文件：15 个 Python、4 个 Markdown 政策文档及 README。领域为 Northstar 冷链物流，覆盖舱单、码头、库存、路线、气象、传感器、告警、通知、计费、审计、交接、维护及政策；未复制 Lumen Gallery 的查询、标签或模块结构。
- 数据集 `agentforge-retrieval-preproduct-v1` 包含 80 条稳定查询：40 正例、40 个“完整冻结快照内无足够证据”负例。Calibration/Validation 各 40 条且各为 20 正例/20 负例。
- 20 个 leakage group 按预先固定的奇偶组规则完整分配，各组含 2 正例和 2 负例；Calibration 和 Validation 各 10 个 group，交集为零。Validation 没有用于标签、分组或参数修改。
- 覆盖精确符号、跨文件调用/import、业务概念到实现、caller/implementation/document 边界、同名 `hold` 的不同语义、文档规则、困难近域负例和 1 个域外负例。
- 40 个正例均有真实路径、准确行范围、相关性理由和 evidence type；40 个负例均记录完整 20 文件检查范围和排除理由。验证器拒绝重复 ID、越界/链接路径、空证据、provenance 更改、split 遗漏和 group 泄漏。
- 所有标签为 `synthetic_fixture_design` + `ai_evidence_review`，`human_reviewed: false`；不是人工 ground truth，也不是依据 Retriever 排名反推的标签。

新数据哈希：

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `retrieval_eval_preproduct_v1.json` | 89,053 | `DE1F62EB971B82E2346605E8B0CA808778F3D9AFA96C7E6319F94A0B8C14883A` |
| `retrieval_eval_preproduct_split_v1.json` | 6,925 | `E5759214E13EF95B6F7CA529E592057514C26277BAA6E4FFF3FFD8368003C8EF` |

## P3：真实检索评测

### 安全执行证据

- 模型、EXE 和实现 DLL 的 SHA-256 分别匹配固定值 `06507C...3E439`、`F8DF89...FC12D`、`F020D9...CDCDCB`。
- 使用 51 文件临时运行时副本，不复制 GGUF；只绑定 `127.0.0.1:55411`，CPU Embedding-only、禁用 Web UI、限制 CORS。标准库客户端不使用代理、不跟随重定向。
- 状态序列：`PROCESS_STARTED → TCP_LISTENING → MODEL_LOADING → MODEL_READY → EVALUATING → COMPLETED`。Health 第一次为 HTTP 503、嵌套 `Loading model`；第二次为 HTTP 200、`{"status":"ok"}`。共 2 次轮询，1.125 秒后就绪；加载期间没有 Embedding 请求。
- 20 个文档向量和两套 80 查询评测（Vector 与 RRF 各自调用 Vector 通道）共 180 次 Embedding，请求 attempts/sent/successes/failures=`180/180/180/0`，没有请求或整轮重试。
- 向量维度 1024；内存 SQLite 共 20 files/chunks、39 symbols、29 references、20 vectors，向量 BLOB 81,920 字节。文档嵌入 5.614669 秒；完整评测 39.840165 秒。服务 Peak Working Set 约 1,426.9 MiB。

### 排序指标

Calibration（20 个有证据查询）：

| Retriever | 适用正例 | Recall@1 | Recall@5 | Recall@10 | MRR | 耗时 |
|---|---:|---:|---:|---:|---:|---:|
| Lexical | 20 | 0.90 | 1.00 | 1.00 | 0.933333 | 0.286983 s |
| Symbol | 13 | 1.00 | 1.00 | 1.00 | 1.00 | 0.001771 s |
| Vector | 20 | 0.90 | 1.00 | 1.00 | 0.929167 | 8.047229 s |
| RRF (`k=60`) | 20 | 0.95 | 1.00 | 1.00 | 0.962500 | 8.293755 s |

独立 Validation（20 个有证据查询）：

| Retriever | 适用正例 | Recall@1 | Recall@5 | Recall@10 | MRR | 耗时 |
|---|---:|---:|---:|---:|---:|---:|
| Lexical | 20 | 0.80 | 1.00 | 1.00 | 0.900 | 0.284125 s |
| Symbol | 11 | 1.00 | 1.00 | 1.00 | 1.000 | 0.001240 s |
| Vector | 20 | 0.85 | 1.00 | 1.00 | 0.925 | 8.259248 s |
| RRF (`k=60`) | 20 | 0.85 | 1.00 | 1.00 | 0.925 | 8.401900 s |

Validation 上 RRF 相对 Lexical 的 Recall@1 增加 0.05、MRR 增加 0.025，Top-5/10 未下降；但它与 Vector 完全相同。RRF 只修复一个 Lexical 首位失败 `pre02-p2`，没有修复 `pre10-p2`、`pre20-p1` 和 `pre20-p2`。后两条是 package import/export 边界，表明文件级 Lexical/Vector/RRF 对导出关系仍有明确缺口；精确 Symbol 对其不适用。

Lexical/Vector Validation Top-10 候选平均 Jaccard 为 0.309776，存在候选差异；但 Vector 的独有首位相关命中只有 `pre02-p2`。RRF 没有造成 Lexical 首位命中的退化。该互补性是可解释但很小的单仓库信号。

### 无证据返回

- Calibration：Lexical、Vector、RRF 均对 20/20 负例返回候选；Symbol 对 9 个适用的精确缺失符号返回 0/9。
- Validation：Lexical、Vector、RRF 均对 20/20 负例返回候选，包括 19/19 近域和 1/1 域外；Symbol 对 7 个适用负例返回 0/7。
- 因此排序提升不能作为拒答能力证据。

### 拒答策略

工程目标在查看结果前固定为 Validation 误接受率 ≤0.05、误拒绝率 ≤0.25。Calibration 使用 0.05 网格，分别检查 score、top-2 margin 和 AND 组合，要求零误接受后最大化覆盖并优先简单规则：

| Calibration 规则族的最佳零误接受候选 | 覆盖率 | 误接受率 | 误拒绝率 | 精确率 |
|---|---:|---:|---:|---:|
| `top_score >= 0.70` | 0.30（12/40） | 0/20 = 0 | 8/20 = 0.40 | 12/12 = 1.00 |
| `top_2_margin >= 0.25` | 0.025（1/40） | 0/20 = 0 | 19/20 = 0.95 | 1/1 = 1.00 |
| score/margin 组合 | 0.30（12/40） | 0/20 = 0 | 8/20 = 0.40 | 12/12 = 1.00 |

按预定规则冻结 `top_score >= 0.70`，在 Validation 只评估一次：接受 9 个有证据、0 个无证据，共 9/40；覆盖率 0.225，误接受率 `0/20=0`，误拒绝率 `11/20=0.55`，精确率 `9/9=1.0`。虽然误接受目标通过，但误拒绝目标失败；不得启用生产阈值。Vector 阈值没有应用于 RRF 分数。

## P4：产品进入决策

决策：**继续暂缓 AF-010 产品实现**。

- 通过：独立数据、split 隔离、真实向量、完整测试和正式数据完整性；RRF 在 Validation 对 Lexical 有小幅且可解释的首位改善，Top-5/10 无退化。
- 未通过：拒答工程目标；所有无阈值的 Lexical/Vector/RRF 仍对全部负例返回候选；冻结阈值通过拒绝 11/20 个正例换取零误接受；RRF 没有优于 Vector，增益只涉及一个查询；标签仍为自动构建的合成 AI 标签。
- 产品默认、RRF 配置与生产阈值均保持关闭。未实施 Reranker。

若继续工程准备，应针对 package import/export 关系补充独立的结构化候选通道实验，并取得人工审核标签和新的保留 Validation；本数据集不得继续用于调参后再称为独立验证。

## 测试、清理和结果文件

- 真实运行前定向测试：44 项通过、0 失败、0 错误、0 跳过，退出码 0。
- 真实运行前完整测试：142 项运行、137 项通过、0 失败、0 错误、5 项跳过，退出码 0。跳过项为当前 Windows 权限无法构造的符号链接、junction 或硬链接，不计入通过数。
- 文档更新后的最终定向复跑仍为 44/44 通过、0 失败、0 错误、0 跳过，退出码 0。最终完整命令 `python -X utf8 -B -m unittest discover -s tests -v` 运行 142 项：137 通过、0 失败、0 错误、5 项链接权限跳过，退出码 0。
- 自有 PID 37344 已停止；退出码 1 是显式 terminate 后结果，不是自行崩溃。端口已释放。临时运行时仍为 51 文件且无新增，随后已删除。
- 没有访问 Agentexpr，没有聊天/补全/生成请求，没有下载、安装、云端访问、Git 操作或系统安全配置变更。
- 最终独立复核 43 个受保护路径的当前大小/SHA 与启动前快照：0 项不一致；`llama-server` 进程数 0，本轮临时运行时目录数 0。
- 机器结果 `AF010PRE_RESULTS.json`：1,174,255 字节，SHA-256 `37756A988EF084DBB6A2510F6907AFFBA14A0CD807AF55795A0344BFEA2ED52D`。
