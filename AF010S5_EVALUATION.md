# AF-010-S5：真实 Vector 与 PackageRelation 联合评测

日期：2026-10-26  
总体结论：`blocked_by_environment`  
离线工作：已完成；真实 Vector：未测量

## 执行结论

本轮执行时为 Windows/Python 3.12.7，但环境变量 `CODEX_SANDBOX_NETWORK_DISABLED=1`。当前会话没有提供另一个平台明确支持、允许 loopback 的宿主执行入口。依据任务门禁，本轮没有重复 TCP 自检，没有读取模型目录或核对模型/运行时资产哈希，没有启动服务，也没有发送 Health、Embedding 或其它 HTTP 请求。

继续使用冻结的 AF-010-S2 数据完成了本地离线评测。Lexical、Symbol、PackageRelation、三通道离线 RRF 和当前查询路由都有结果；Vector 与包含 Vector 的完整 RRF 均为 `not_measured`。`top_score >= 0.70` 仅适用于 Vector 余弦分数，本轮没有向量分数，因此也未测量该规则。

本轮没有修改产品 Retriever、PackageRelation、RRF 或拒答默认行为，也未实施产品接入。阶段结论为 `blocked_by_environment`。

## 冻结输入与完整性

冻结集版本为 `agentforge-package-relations-multirepo-v1`：3 个合成仓库，每仓库 15 个 fixture 文件；查询共 120 条，Calibration/Validation 各 60 条、各 30 条有证据和 30 条无证据。split 校验通过，leakage group 没有跨 split。标签来自合成 fixture 设计，`human_reviewed=false`。

| 输入 | 大小 | SHA-256 |
|---|---:|---|
| `tests/fixtures/package_s2_eval_v1.json` | 146,043 字节 | `AF8485FA2A62F976332E19D74E8E07DA6FE717A419C41286FF878A80C93CAC79` |
| `tests/fixtures/package_s2_split_v1.json` | 4,429 字节 | `D7357C244446EFF933C4F3D1F6E252B571FD7283AEED9ACFFE5DFEF4F000D5BA` |
| `tests/fixtures/package_s2_design_v1.json` | 3,374 字节 | `6EA0F900693B7EA96BB4EE7B708D2072E213EB4FE8CEC3DA4F187B8E725EC7B1` |

| 仓库 | 文件数 | 文件清单 SHA-256 |
|---|---:|---|
| `clinic_gateway` | 15 | `D7D6A3A08672375B9C470FED34B1DC7CB163E6086814662EEF98DF9349C379C5` |
| `harbor_registry` | 15 | `CEFB8238C470118ACE3F9033A977A0A804AC023D70B5F0F24363F9AE0BB4463C` |
| `observatory_sdk` | 15 | `4EF0DEF570104DF249DCABBBB282E1DC649321312E6BA1A9CFDB528E945EEDF7` |

每个仓库清单哈希由按相对路径排序的 15 个文件路径、大小和 SHA-256 组成。机器结果中保存了逐文件清单。

主要冻结结果核验值：

| 文件 | 大小 | SHA-256 |
|---|---:|---|
| `AF010S2_OFFLINE_FINAL_RESULTS.json` | 564,765 字节 | `6D0E57A18D2BAE7E87E70B87324EC14A8260C4C23D113E37743DBD9BA970CC4D` |
| `AF010S2_EVALUATION.md` | 11,018 字节 | `4665FE9C18A88066BFC53A2981D7A4D7833D1162D35F35A247B9CD9DE25B6330` |
| `AF010S3_SCALE_RESULTS.json` | 64,062 字节 | `0465F54A83E7FC4C1C17B5E12A90D6CD5915E817F3E40966E0FA91CA91B88B31` |
| `AF010S4_SCALE_RESULTS.json` | 86,019 字节 | `9298446F881E5E239DAD6972EDFD4F65B19EFAB8A27E32AD3CD756D4EA71640B` |
| `.agentforge/data/index.sqlite3` | 32,768 字节 | `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296` |
| `.agentforge/data/read_only_evidence.json` | 2,885 字节 | `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733` |

S5 运行器同时固定 AF-009FA、AF-009G、AF-010-PRE、AF-010-S2/S3/S4 历史文件及全部历史 fixture；169 个受保护路径的运行前、运行后及当前 SHA-256/大小逐项一致。完整逐项快照保存在 `AF010S5_RESULTS.json`。

## 门禁与真实模型状态

| 项目 | 状态 | 证据 |
|---|---|---|
| 当前执行平台 | `measured` | `win32`，Python 3.12.7，AMD64 |
| 网络受限标记 | `measured` | `CODEX_SANDBOX_NETWORK_DISABLED=1` |
| Loopback 自检 | `not_run` | 未在已知受限进程中重复 S2 的 TCP 测试 |
| llama-server/模型资产哈希 | `not_measured` | 门禁失败后未访问模型目录 |
| 服务进程与 Health | `not_measured` | 未启动服务、未发请求 |
| Embedding 请求 | `blocked_by_environment` | 尝试 0、发送 0、成功 0、失败 0、重试 0 |
| 完整四通道 RRF | `not_measured` | Vector 通道不可用，不以空 Vector 结果冒充完整 RRF |

没有因错误或超时发生模型请求；也没有 TCP readiness 或模型 readiness 观测。本轮无服务进程或端口需要清理。

## 返回合同与离线评测方法

- Lexical、Symbol、PackageRelation 和路由均按仓库相对文件路径去重；Recall/MRR 沿用 S2 evaluator 的统一分母和路径相关标签。
- Symbol 仅对带精确 `symbol_query` 的适用查询计分；PackageRelation 仅在 `package_applicable=true` 的子集计分。两者的适用集不能与全体 Lexical 指标混为一谈。
- PackageRelation 原始分数是静态关系匹配分数，候选保留 `relation_type` 与 `resolution_status`；它是静态证据，不证明运行时导入或调用。
- RRF 使用现有 `fuse_ranked_results()`，文件路径作为候选身份，公式为各通道 `1/(60+rank)` 之和。没有直接相加不同通道的原始分数。详细候选记录保存排名、原始分数、行范围、关系解析状态及逐通道 RRF 贡献。
- 本轮可测的 RRF 只包含 `lexical`、`symbols`、`package_relations`。机器结果将它命名为 `partial_offline_rrf`；请求的四通道 RRF 明确标记为 `not_measured`。
- 路由只依据 query 文本选择候选通道，详细记录每条 query 的规则、原因、置信状态、回退和可用通道。Vector 不可用时，含 `vector` 的路由选择不会被伪装成已执行该通道。
- `AF010S5_RESULTS.json` 中的逐查询详细候选列表与 metric evaluator 的候选路径顺序校验一致（`rankings_match_metric_evaluation=true`）。

## 离线检索指标

Recall@K/MRR 只对有相关证据的适用查询宏平均。无证据查询单独统计是否返回候选；表中无证据为“非空数/适用无证据数”。

### Calibration

| 通道 | 有效正例 | 适用查询 | Recall@1 | Recall@5 | Recall@10 | MRR | 无证据非空 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Lexical | 30 | 60 | 0.666667 | 1.000000 | 1.000000 | 0.816667 | 30/30 |
| Symbol | 6 | 12 | 1.000000 | 1.000000 | 1.000000 | 1.000000 | 0/6 |
| PackageRelation | 15 | 30 | 0.533333 | 1.000000 | 1.000000 | 0.766667 | 15/15 |
| 部分离线 RRF（无 Vector） | 30 | 60 | 0.633333 | 1.000000 | 1.000000 | 0.805556 | 30/30 |
| 查询路由（无 Vector） | 30 | 60 | 0.666667 | 1.000000 | 1.000000 | 0.822222 | 30/30 |
| Vector / 完整 RRF | — | — | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |

路由正确 `51/60`（0.85），回退 `30/60`。

### Validation

| 通道 | 有效正例 | 适用查询 | Recall@1 | Recall@5 | Recall@10 | MRR | 无证据非空 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Lexical | 30 | 60 | 0.800000 | 1.000000 | 1.000000 | 0.877778 | 30/30 |
| Symbol | 6 | 9 | 1.000000 | 1.000000 | 1.000000 | 1.000000 | 0/3 |
| PackageRelation | 18 | 39 | 0.888889 | 1.000000 | 1.000000 | 0.944444 | 21/21 |
| 部分离线 RRF（无 Vector） | 30 | 60 | 0.700000 | 0.900000 | 1.000000 | 0.804444 | 30/30 |
| 查询路由（无 Vector） | 30 | 60 | 0.833333 | 1.000000 | 1.000000 | 0.916667 | 30/30 |
| Vector / 完整 RRF | — | — | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |

Validation 路由正确 `48/60`（0.80），回退 `18/60`。PackageRelation 在其适用正例子集内首位补救 Lexical 4 次；相对 Lexical 与适用 Symbol 通道，Top-10 独有相关命中为 0。正例查询中 PackageRelation 返回的非相关候选共 159 条。它改善部分适用关系查询的排名，但没有增加本评测的 Top-10 相关证据覆盖。

部分离线 RRF 的 Validation Recall@1/MRR 低于 Lexical（0.70/0.804444 对 0.80/0.877778），Recall@5 也较低（0.90 对 1.00）。不含 Vector 的三通道 RRF 没有呈现可支持产品融合的增益。它对 30/30 无证据查询仍返回候选。

路由在本 Validation 上的 R@1/MRR 高于 Lexical，但仍有 `12/60` 类别判断不匹配、`18/60` 回退；全部 `30/30` 无证据查询仍返回候选。它不证明拒答能力。

## 与 AF-010-S2 历史重跑差异

本轮 Lexical、Symbol、PackageRelation、三通道 RRF 的 Calibration/Validation 指标均与冻结 S2 离线结果相同；Validation 各通道逐查询候选排序也一致。历史和本轮综合 ranking digest 不同，来源是 3 条 Calibration 路由判断和相应 routed tie 顺序：

- `S2-CLINIC_GATEWAY-020`
- `S2-HARBOR_REGISTRY-020`
- `S2-OBSERVATORY_SDK-020`

这些查询询问动态导入是否静态解析。当前 `query_routing.py` 的 S3 保守规则命中 `dynamic_relation_uncertainty`，选择低置信度 `uncertain`/lexical 回退；历史 S2 结果使用 `dependency_terms` 路由。当前路由 Calibration 正确数从历史 `54/60` 变为 `51/60`，Validation 保持 `48/60`。路由 `version` 字符串仍为 `package-router-calibration-v1`，没有随着这项实现变化升级，因此不能把两版路由指标称为完全同版复现。S5 未根据 Validation 调整规则或标签。

## 固定阈值与产品决策

`top_score >= 0.70` 在本轮为 `not_measured`。原因是它是 Vector 余弦相似度门槛，而 Vector 请求数为 0；Lexical、Symbol、PackageRelation 或 RRF 分数尺度不同，不能套用该阈值。生产拒答规则继续关闭。

PackageRelation 的适用子集指标较高，但依赖显式适用查询；它没有增加 Top-10 独有相关命中，且候选噪声明显。路由只在此小型 AI/合成标签集上评估。缺少 Vector 联合结果，且完整四通道 RRF 和真实向量拒答均未测量。因此 S5 决策为 `blocked_by_environment`，不建议进入产品实现评审；生产接入仍须另行审批。

## 测试和执行记录

真实模型门禁观察：

```text
CODEX_SANDBOX_NETWORK_DISABLED=1
```

未运行任何 TCP 自检或模型命令。

离线评测命令：

```powershell
python -X utf8 -B AF010S5_OFFLINE_RUNNER.py
```

退出码 0；总体状态 `blocked_by_environment`，离线状态 `measured`，冻结输入 169 个受保护路径一致。补充详细得分/融合贡献并核对指标排名：

```powershell
python -X utf8 -B AF010S5_OFFLINE_RUNNER.py --enrich-existing
```

最终退出码 0；排名候选与 metric evaluator 顺序一致，169 个受保护路径不变。开发期间此细节核验曾发现并修复两个 runner 问题：映射详情字段错误，以及未对 `package_applicable=false` 查询按评测协议清空结果。最终结果已重新核验。

定向回归测试：

```powershell
python -X utf8 -B -m unittest tests.test_package_s2 tests.test_package_relations tests.test_provisional_fusion tests.test_embeddings tests.test_local_embedding_provider -v
```

运行 68 项，68 通过、0 失败、0 跳过，退出码 0。文档更新后的完整测试命令为：

```powershell
python -X utf8 -B -m unittest discover -s tests -v
```

运行 181 项，176 通过、0 失败、0 错误、5 跳过，退出码 0。5 项跳过均因当前 Windows 权限无法创建符号链接、junction 或硬链接；未计为通过。

## 变更文件、限制与下一步

本轮新增 `AF010S5_OFFLINE_RUNNER.py`、`AF010S5_RESULTS.json` 和本报告；并按实际门禁更新 `STATUS.md`、`ROADMAP.md`、`ARCHITECTURE.md`、`README.md`、`FILE_MANIFEST.md`。未修改产品 Retriever、SQLite Schema、正式索引或冻结 fixture。

没有访问或修改 Agentexpr。没有启动模型服务、没有 HTTP/Embedding 请求、没有下载或安装依赖、没有 Git 写操作。RRF/PackageRelation 默认行为与拒答阈值保持不变。

最终完整测试后重新比对机器结果中的 169 个保护路径，大小及 SHA-256 均保持一致（`protected_unchanged=true`）。正式索引为 32,768 字节，SHA-256 `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296`；`read_only_evidence.json` 为 2,885 字节，SHA-256 `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733`。本轮没有启动进程或创建模型服务临时目录；测试临时目录已由测试夹具清理。

恢复真实 Vector 验收的最小前置条件：在平台明确支持且允许本地 loopback 的正常 Windows 宿主执行环境中运行 S2 已核验的受控 Health/Embedding-only Runner；先核对固定模型与运行时哈希，再完成严格 Health 状态机后发起一次冻结评测。不要修改或绕过当前安全限制。AF-010 产品实现、Vector/RRF 产品配置和生产拒答阈值在此之前继续暂缓。
