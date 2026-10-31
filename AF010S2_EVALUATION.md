# AF-010-S2 多仓库泛化评测与结构化检索报告

日期：2026-10-26  
结论：`blocked_by_environment`

## 结论摘要

三个独立合成仓库、固定 split、离线检索比较、可解释路由和关系图规模/生命周期实验均已完成。真实本地 Vector 评测没有执行：当前执行环境设置了 `CODEX_SANDBOX_NETWORK_DISABLED=1`；本轮唯一一次本机 loopback 自检中 `bind` 与 `listen` 成功，但客户端 `connect` 因 `PermissionError` / Windows 错误 10013 被拒，故按安全边界没有启动 llama-server、没有发 Health 或 Embedding 请求。具体拦截组件未知。

因此，Vector 指标与包含 Vector 的完整 RRF 指标均为**未测量**。本文中的 RRF 是仅融合现有 Lexical、适用的 Symbol 和 PackageRelation 离线通道的对照结果，不能称为完整四通道结果。AI/合成标签不是人工审核 ground truth。生产拒答阈值、默认 Retriever、生产 RRF 均未改变。

## 数据、隔离与复现

- 新建三个结构不同的合成仓库：`harbor_registry`、`clinic_gateway`、`observatory_sdk`；每个仓库 15 个源代码/文档文件，共 45 个文件。内容覆盖包导出与多层重导出、别名、相对导入、同名异义、循环、星号导入、动态 `__all__` / 导入、文档策略及近域负例。目标代码只被静态扫描和 AST 解析，没有执行或导入。
- 查询集 `package_s2_eval_v1` 共 120 条，每仓库 40 条。Calibration 与 Validation 各 60 条，均为 30 条有证据与 30 条无证据；三个仓库各 split 的样本按设计规格分层。每个查询恰有一个 split，leakage group 不跨 split，路径及正例行范围由验证器核查。
- 标签来源为 `synthetic_fixture_design`，`human_reviewed=false`。无证据标签只指冻结合成仓库中缺少满足意图的证据。
- 固定配置：路由规则 `package-router-calibration-v1`，RRF `k=60`；阈值及路由只依据 Calibration 预先固定，Validation 只运行一次。结果文件记录 `validation_evaluations=1`。未使用 Validation 调整配置。
- 主要复现命令：

  ```powershell
  python -X utf8 -B AF010S2_BUILD_FIXTURES.py
  python -X utf8 -B AF010S2_OFFLINE_FINAL_RUNNER.py
  python -X utf8 -B AF010S2_SCALE_RUNNER.py
  python -X utf8 -B -m unittest tests.test_package_s2 tests.test_package_relations tests.test_local_embedding_provider tests.test_embeddings tests.test_provisional_fusion -v
  python -X utf8 -B -m unittest discover -s tests -v
  ```

  构建脚本只用于确定性合成 fixture；两个结果 runner 使用独立内存 SQLite。较早的 `AF010S2_OFFLINE_RESULTS.json` 是首次流水线预检：其中一次规模更新写入了相同内容，不能用于说明“文件已修改”。修正后的规模结论以 `AF010S2_SCALE_RESULTS.json` 为准；准确离线指标以 `AF010S2_OFFLINE_FINAL_RESULTS.json` 为准。

## 检索结果

指标是对有相关标签查询的宏平均；无证据查询不进入 Recall/MRR 分母，并单独统计候选是否非空。Symbol 和 PackageRelation 只对适用查询计分。`n+` 为有证据查询数，`n0` 为无证据查询数。数值为比例。

### Calibration（配置选择集）

| 通道 | 适用正例 n+ | Recall@1 | Recall@5 | Recall@10 | MRR | 无证据非空 |
|---|---:|---:|---:|---:|---:|---:|
| Lexical | 30 | 0.666667 | 1.000000 | 1.000000 | 0.816667 | 30/30 |
| Symbol（适用子集） | 6 | 1.000000 | 1.000000 | 1.000000 | 1.000000 | 0/6（适用无证据） |
| PackageRelation（适用子集） | 15 | 0.533333 | 1.000000 | 1.000000 | 0.766667 | 15/15 |
| 离线 RRF（无 Vector） | 30 | 0.633333 | 1.000000 | 1.000000 | 0.805556 | 30/30 |
| 查询路由（无 Vector；缺少通道时按原型回退） | 30 | 0.666667 | 1.000000 | 1.000000 | 0.822222 | 30/30 |
| Vector | — | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |

路由类别准确率为 54/60（90%），回退 27/60。PackageRelation 在其 15 个适用正例上的 Top-1 为 8/15；同一子集 Lexical 为 7/15。此差异仅是小型合成 Calibration 观察，不能外推。

### Validation（固定后的一次独立运行）

| 通道 | 适用正例 n+ | Recall@1 | Recall@5 | Recall@10 | MRR | 无证据非空 |
|---|---:|---:|---:|---:|---:|---:|
| Lexical | 30 | 0.800000 | 1.000000 | 1.000000 | 0.877778 | 30/30 |
| Symbol（适用子集） | 6 | 1.000000 | 1.000000 | 1.000000 | 1.000000 | 0/3（适用无证据） |
| PackageRelation（适用子集） | 18 | 0.888889 | 1.000000 | 1.000000 | 0.944444 | 21/21 |
| 离线 RRF（无 Vector） | 30 | 0.700000 | 0.900000 | 1.000000 | 0.804444 | 30/30 |
| 查询路由（无 Vector；规则冻结） | 30 | 0.833333 | 1.000000 | 1.000000 | 0.916667 | 30/30 |
| Vector | — | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |

路由类别准确率为 48/60（80%），回退 18/60。PackageRelation 的 18 个适用正例中 Top-1 命中 16/18；同一子集 Lexical 命中 12/18，MRR 分别为 0.944444 与 0.796296。PackageRelation 在 30 个有证据查询中补救 Lexical 的首位错误 4 次，但其 Top-10 独有相关命中为 0：Lexical 已覆盖这些正例相关路径。该通道因此改善部分关系型查询排序，但没有扩展 Top-10 相关覆盖。

在全部 30 个 Validation 无证据查询中，Lexical、离线 RRF 与路由均返回非空候选（30/30）；PackageRelation 在其适用的 21 条无证据查询中也全部返回候选（21/21）。这说明当前实验没有建立拒答能力。Symbol 子集上的 0/3 不可与全体查询比较，样本也过少。

### 时间与互补性

Validation 本地离线检索时间（不含启动、模型加载或 Embedding）：Lexical 60 次总计 0.071154 秒；PackageRelation 60 次 0.032305 秒；离线 RRF 60 次 0.003731 秒；路由 60 次 0.002726 秒；Symbol 9 次 0.000287 秒。它们是当前 Windows 运行的单次小型合成数据计时，不是性能承诺。真实 Embedding 调用数为 0，Vector 建索引成本及真实向量耗时未知。

离线 RRF 的 Validation Recall@1/MRR 低于 Lexical（0.70/0.804444 对 0.80/0.877778）。没有证据支持将 RRF 产品化。路由提升了本样本的首位命中，但有 12/60 Validation 路由判断错误，且无证据候选仍普遍非空；它只是离线原型，未进入产品默认路径。

## 路由与关系图

离线路由使用可解释词面/意图规则，不调用 LLM。固定路由在 Calibration 为 54/60，Validation 为 48/60；混淆矩阵及每条查询的命中规则、通道、理由和置信状态保存在结果 JSON。无法确定的查询会保留不确定/回退路径；Vector 不可用时不伪称使用 Vector。

关系图静态解析基于安全扫描器许可文件及 AST，不导入源代码。关系结果是静态证据，不证明运行时行为。增量原型在变化快照上采用原子全量重建，而非局部更新；文件数超出既有 2000 上限时明确失败。

规模测量使用 `tracemalloc` 的 Python 分配峰值，不代表进程总内存：

| 请求模块数 | Python 文件数 | 关系数 | 全量图构建秒 | Python 分配峰值 | 初次刷新秒 | 单文件修改后刷新秒 |
|---:|---:|---:|---:|---:|---:|---:|
| 100 | 101 | 204 | 0.751125 | 1,977,595 B | 0.350955 | 0.375300 |
| 1,000 | 1,001 | 2,004 | 9.111255 | 6,746,315 B | 3.406719 | 10.926863 |
| 5,000 | 5,001 | 未生成 | 未完成 | 9,626,017 B（受限扫描阶段） | 未运行 | 未运行 |

5,000 模块场景被扫描器 2,000 文件上限拒绝，属于预期资源保护边界，不是解析器性能测量。1,000 模块下单文件变更仍触发全量重建，说明目前的“增量”接口仅正确处理快照生命周期，尚未提供按文件增量计算。

## 环境阻塞与安全结果

- `CODEX_SANDBOX_NETWORK_DISABLED=1`。唯一一次本地回环预检在 `127.0.0.1:58430` 成功 `bind()`、`listen()`，但同一环境的 `connect()` 返回 `PermissionError`，`errno=13`、`winerror=10013`；`accept` 与 ping/pong 未到达，所有测试 socket 已关闭。不得据此归因于防火墙、模型服务或特定过滤组件。
- 当前进程查询未返回 `llama-server` 条目；`tasklist` 退出码 1 且没有匹配行，故**不能据此证明整台主机不存在其他服务**。本轮没有启动服务，也没有拥有的 PID 或临时运行时副本。
- 预检时模型、EXE、实现 DLL 的 SHA 与既有固定值一致，运行时清单为 51 个文件；未复制 GGUF。
- 未发 `/health` 或 `/v1/embeddings` 请求；Embedding 成功/失败数均为 0。没有聊天/生成请求。
- 没有改动 Windows 安全设置。正式索引与只读证据当前哈希仍为：索引 `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296`（32,768 字节）；证据文件 `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733`（2,885 字节）。离线 runner 保存的 127 项保护快照与当前文件逐项比较无差异。
- 生产拒答阈值关闭；默认 Retriever 未改变；RRF 未产品化；Reranker 未实施。

## 结果与后续条件

P2（合成数据与 split）和 P3 的离线部分可复现；P1 的既有代码审查与数据保护检查已记录；P4 暂不批准产品实现。AF-010-S2 的独立 Vector 验收被当前执行环境阻塞，不能声称完成真实 Vector、完整四通道 RRF 或跨通道向量联合评测。

继续前的最小条件：在平台明确支持且允许回环通信的正常 Windows 执行环境中运行受控宿主 runner，先完成一次 TCP 自检与严格 Health 检查，再进行一次 Embedding-only 冻结评测；不得移除或修改当前安全限制。之后仍需依据结果和标签限制进行独立产品设计评审。当前阶段判定：**`blocked_by_environment`**。

测试结果以最终报告所列本轮真实执行记录为准。机器可读数据：`AF010S2_OFFLINE_FINAL_RESULTS.json`、`AF010S2_SCALE_RESULTS.json`、`AF010S2_BLOCKED_RESULTS.json`。

## 测试结果

- 定向命令 `python -X utf8 -B -m unittest tests.test_package_s2 tests.test_package_relations tests.test_local_embedding_provider tests.test_embeddings tests.test_provisional_fusion -v`：58 项通过、0 失败、0 跳过，退出码 0。
- 完整命令 `python -X utf8 -B -m unittest discover -s tests -v`：运行 171 项，166 通过、0 失败、0 错误、5 跳过，耗时 15.869 秒，退出码 0。5 项跳过为 Windows 未授予创建 symbolic link、junction、hard link 的权限；不计入通过。
- 结果目录中的测试临时内容已清理；预存的 `tests/.tmp/af009c-llama-server` 未触碰。本轮创建的 `tests/__pycache__/test_package_s2.cpython-312.pyc`（先前显式编译产生）已按准确路径移除；`-B` 测试运行未再生成该文件。
- 因 Vector 服务未启动，`top_score >= 0.70` 的真实向量拒答阈值没有在本轮校准或验证。不能将历史阈值结果外推到新的关系评测集。
