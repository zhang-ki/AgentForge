# AF-010-S1 Package Import/Export 结构化检索研究

日期：2026-10-26

## 结论

本轮完成了独立静态关系图、显式 `PackageRelationRetriever` 原型、17 文件合成 package 仓库、40 条暂定查询（Calibration/Validation 各 20，且各 10 条正例/10 条无证据）及离线评测。原型不进入正式 SQLite Schema、CLI 或默认 Retriever。

独立 Validation 中，Lexical 在 10 条正例上的 Recall@1/5/10 为 `0.70/0.90/1.00`、MRR 为 `0.814286`；Symbol 只适用于 3 条精确符号查询，均为 `1/1/1`、MRR `1`；PackageRelation 在 18 条适用查询中的 8 条正例上为 `1/1/1`、MRR `1`，并在 10 条适用无证据查询上全部返回空。它将 3 条 Lexical/Symbol 非首位命中的关系查询提升为首位，但没有产生“两个既有通道 Top-10 都遗漏而结构通道独有命中”的样本。三通道离线 RRF 为 `1/1/1`、MRR `1`，但仍对 10/10 无证据查询返回候选。

这些结果证明显式 package 关系过滤在这个合成 fixture 中具有排序和负例过滤价值，但**不足以批准 AF-010 产品实现**：标签是自动构建而非人工审核；PackageRelation 通过显式结构过滤器调用，不证明自然语言查询可稳定路由；数据只有一个小型合成仓库；Vector 本轮按边界未启动、未测量；RRF 仍没有拒答能力。

## P0：冻结与复用

- 复用了现有 `ParsedFile` / `ParsedReference` 的模块、包、相对 import、import alias 和 resolved/ambiguous/unresolved 证据。
- 没有修改 `python_ast.py`、`resolver.py`、`indexer.py`、`store.py`、`retriever.py`、`preproduct.py` 或 `fusion.py`；`evaluation.py` 只登记新的隔离评测版本。
- 正式数据库与证据文件保持：
  - `.agentforge/data/index.sqlite3`：32,768 字节，SHA-256 `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296`
  - `.agentforge/data/read_only_evidence.json`：2,885 字节，SHA-256 `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733`
- `AF010PRE_RESULTS.json` 仍为 1,174,255 字节，SHA-256 `37756A988EF084DBB6A2510F6907AFFBA14A0CD807AF55795A0344BFEA2ED52D`。

## P1：关系模型

`agentforge.package_relations` 生成六类记录：`imports`、`imports_symbol`、`exports_symbol`、`reexports_symbol`、`import_alias` 和 `resolves_to`。每条记录保留稳定 relation ID、来源模块/路径/行、目标模块/名称/路径、目标或候选 symbol ID、解析状态和循环标志。

确定解析范围：普通模块 import、特定符号 import、绝对/相对 import、显式 alias、静态字符串集合 `__all__`、包内本地定义 export、单层和多层 reexport，以及唯一终点的 `resolves_to`。

保守边界：重复同名绑定为 ambiguous；循环 reexport 不产生 resolved 终点；star import 和动态 `__all__` 标为 uncertain；动态 import 的非字面目标不猜测；普通 import 在没有静态 `__all__` 时不提升为 public export。全部分析只读取扫描器批准的文本，不导入或执行 fixture。

关系图共 47 条：resolved 30、ambiguous 2、uncertain 3、unresolved 12；4 条关系带循环标志；0 个解析失败。`resolves_to` 同时覆盖 package export 及 consumer import binding，能把 `consumer.ship` 穿过 `atlas.send` 重导出链定位到 `atlas/impl.py`。

## P2：独立数据

- 仓库：`tests/fixtures/package_relation_repo/`，17 个 Python/Markdown 文件。
- 数据：`tests/fixtures/package_relation_eval.json`，40 条查询，SHA-256 `6B980E3B5F5857E8CB20BDB1457D825F9E5D5D321276DCBA977E14342EACE417`。
- split：`tests/fixtures/package_relation_split.json`，SHA-256 `A7BF7A2A60F4600805AA024C4A93194130B8F4CDB6B7883F5618015A47EA4EC7`。
- Calibration 与 Validation 各 20 条、各 10 正例/10 无证据，5 个完整 leakage group；ID 完整唯一，组无交叉，所有正例路径和行范围通过验证。
- 标签来源为 `synthetic_fixture_design`，`review_method=ai_evidence_review`、`human_reviewed=false`；不宣称人工 ground truth。

## P3：评测结果

| Split / Retriever | 适用正例 | R@1 | R@5 | R@10 | MRR |
|---|---:|---:|---:|---:|---:|
| Calibration Lexical | 10 | 0.50 | 0.90 | 1.00 | 0.662500 |
| Calibration Symbol | 1 | 1.00 | 1.00 | 1.00 | 1.000000 |
| Calibration PackageRelation | 10 | 1.00 | 1.00 | 1.00 | 1.000000 |
| Calibration RRF（Lexical+Symbol+Package） | 10 | 1.00 | 1.00 | 1.00 | 1.000000 |
| Validation Lexical | 10 | 0.70 | 0.90 | 1.00 | 0.814286 |
| Validation Symbol | 3 | 1.00 | 1.00 | 1.00 | 1.000000 |
| Validation PackageRelation | 8 | 1.00 | 1.00 | 1.00 | 1.000000 |
| Validation RRF（Lexical+Symbol+Package） | 10 | 1.00 | 1.00 | 1.00 | 1.000000 |

- PackageRelation 的 Calibration/Validation 首位补救分别为 4/3 条；严格的 Top-10 独有相关命中均为 0。
- PackageRelation 在两个 split 的 10/10 适用无证据查询上均返回空，错误候选计数为 0；这是显式关系过滤行为，不等于自然语言拒答策略。
- 离线 RRF 在两个 split 都对 10/10 无证据查询返回候选，因为 Lexical 通道仍返回候选。融合改善排序不解决证据充分性。
- Vector：`not_measured`。没有启动模型服务、没有发送 HTTP/Embedding/聊天/生成请求；RRF 结果明确不含 Vector。
- 机器可读逐查询结果为 `AF010S1_RESULTS.json`（132,405 字节，SHA-256 `F50D0856F09163B099BBB01C7038D4878D11C590E71C022E4316E03B8B660D55`）。

## P4：测试与决定

实际命令：

1. `python -X utf8 -B -m unittest tests.test_package_relations -v`：最终 17/17 通过，退出码 0。
2. 首次组合定向命令包含不存在的 `tests.test_fusion`，产生 1 个 unittest loader error；这是命令模块名错误，不是产品测试失败。
3. 修正命令 `python -X utf8 -B -m unittest tests.test_package_relations tests.test_symbols tests.test_provisional_fusion tests.test_generalization tests.test_evaluation -v`：57/57 通过，退出码 0。
4. `python -X utf8 -B AF010S1_EVALUATE.py`：退出码 0，生成独立结果。
5. `python -X utf8 -B -m unittest discover -s tests -v`：159 项运行，154 通过、0 失败、0 错误、5 跳过，退出码 0。跳过项是 Windows 权限不足，无法构造真实符号链接、junction 和硬链接，不计入通过。

阶段决定：保留原型和研究数据，**不接入默认 Retriever、不迁移正式 Schema、不增加产品 CLI、不启用 RRF 或拒答阈值**。进入产品实现前至少需要人工审查标签、更多独立仓库、自然语言到结构过滤器的路由评测、规模/增量更新测试，以及与已验证 Vector 通道在新 Validation 上的联合比较。

## 文件与收尾完整性

- 新增：`agentforge/package_relations.py`、`agentforge/package_evaluation.py`、`AF010S1_EVALUATE.py`、17 文件 fixture、数据/split、`tests/test_package_relations.py`、本报告及机器结果。
- 修改：`agentforge/evaluation.py` 仅登记隔离数据版本；`STATUS.md`、`ROADMAP.md`、`ARCHITECTURE.md`、`README.md`、`FILE_MANIFEST.md` 记录实际结果。
- 未修改：正式 Schema/store/indexer/default retriever/CLI、全部历史评测输入与结果、正式数据库及证据文件。
- 结果内记录的 6 个受保护文件在最终复核时大小和 SHA-256 全部前后一致；完整测试也继续校验更广的冻结历史哈希。
- 没有启动 llama-server；最终 `llama-server` 进程数为 0。没有创建本轮运行时副本或端口，也没有发送网络请求。
- `tests/.tmp` 中只剩本轮开始前已经存在的 `af009c-llama-server` 目录；本轮测试创建的临时目录均已清理，没有擅自删除该历史目录。
- 未访问或修改 Agentexpr。
