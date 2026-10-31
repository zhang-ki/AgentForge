# AF-010-S2 真实宿主验收审计（2026-10-26）

## 审计结论

本审计只读取既有真实运行产物并执行离线回归测试；没有再次运行 `AF010S2_HOST_RUNNER.py`，没有启动模型，也没有发送 Health、Embedding、聊天、补全或生成请求。

- **真实宿主执行：完成。** 状态机依次记录 `PROCESS_STARTED → TCP_LISTENING → MODEL_LOADING → MODEL_READY → EVALUATING → COMPLETED`。本次真实执行使用正常 Windows Python 3.12.7 宿主，运行记录中的网络禁用标记为空。
- **真实 Vector：执行验收通过、产品质量验收不通过。** 45 个文档向量和 120 个查询向量均由真实本地模型生成；165 次尝试、165 次发送、165 次成功、0 次失败。Vector 的 Calibration/Validation 排名、逐查询结果和汇总指标均已保存。
- **完整四通道 RRF：已完成并有独立 Validation 指标。** 融合通道是 Lexical、Symbol、PackageRelation、Vector，固定 `k=60`。但 Validation 的 RRF 低于 Lexical，因此结果不支持产品接入。
- **固定拒答阈值：本次未测量。** `AF010S2_RESULTS.json` 没有保存向量 `top_score`，也没有覆盖率、误接受、误拒绝或精确率字段。不得用 AF-009G-R5 或 AF-010-PRE 的历史结果填补本次数据。
- **AF-010：不批准产品化。** RRF 没有稳定增益、无证据查询仍普遍返回候选、阈值未测量、标签不是人工 ground truth。
- **第一版离线功能：可以冻结交付。** 交付范围是已通过测试的离线只读 RAG、代码感知索引、显式研究接口和受限只读 Agent；不包括 AF-010 产品接入、生产拒答、Reranker 或真实聊天验收。

## 1. 输入和数据身份

主要证据是 `AF010S2_RESULTS.json`，690,258 字节，SHA-256：

`02C877EAEBA5A882668CBF1E02A7BAA7E843B67D922A6DAAF842364464C7655C`

冻结数据版本为 `agentforge-package-relations-multirepo-v1`，包含 3 个独立合成仓库、120 条查询。Calibration 和 Validation 各 60 条，均为 30 条有证据查询和 30 条无证据查询；leakage group 不交叉。机器结果记录 `validation_evaluations=1`、`rules_frozen_before_validation=true`、`validation_used_for_tuning=false`。标签来自合成 fixture 设计，`human_reviewed=false`。

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| `tests/fixtures/package_s2_eval_v1.json` | 146,043 | `AF8485FA2A62F976332E19D74E8E07DA6FE717A419C41286FF878A80C93CAC79` |
| `tests/fixtures/package_s2_split_v1.json` | 4,429 | `D7357C244446EFF933C4F3D1F6E252B571FD7283AEED9ACFFE5DFEF4F000D5BA` |
| `tests/fixtures/package_s2_design_v1.json` | 3,374 | `6EA0F900693B7EA96BB4EE7B708D2072E213EB4FE8CEC3DA4F187B8E725EC7B1` |

历史 `AF010S2_OFFLINE_FINAL_RESULTS.json` 和 `AF010S2_BLOCKED_RESULTS.json` 只作为运行条件对照，没有用于补齐真实结果。

## 2. 模型、预处理和请求计数

- 模型 ID：`Qwen/Qwen3-Embedding-0.6B-GGUF@370f27d7550e0def9b39c1f16d3fbaa13aa67728:Q8_0#06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439`
- GGUF：Qwen3-Embedding-0.6B Q8_0，SHA-256 `06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439`
- 输出维度：1,024。
- 服务参数：Embedding 模式、`pooling=last`、`embd-normalize=2`、CPU (`n-gpu-layers=0`)、禁用 Web UI，只监听 `127.0.0.1:57577`。
- Query 预处理：`Instruct: <固定检索指令>\nQuery: <查询>`；Document 路径使用原文。客户端对 1,024 维、非空、有限数值进行校验并归一化。
- 运行时：`llama-b11424` 的 51 文件临时副本；EXE SHA-256 `F8DF891A5E4D10FA8CFC0F86C8D99617A06EDD18D046E18B59EC3354F17FC12D`，实现 DLL SHA-256 `F020D94DF5E91840F2C9626B86F9AFE414C16D17ED5E1F41FE9CB614D3CDCDCB`。

计数可由实现独立解释：3 个仓库各有 15 个文件分块，索引阶段生成 45 个 document embedding；120 条查询各执行一次 query embedding，共 165 次。Provider 只在 HTTP 200、JSON 结构有效、向量非空且 1,024 维有限值校验成功后增加 `successes`。实现没有自动重试路径。机器结果的 `attempts=sent=successes=165`、`failures=0` 与该调用图一致，未发现重复计数或把失败计为成功的证据。

## 3. 模型 readiness

TCP readiness 用时 0.623651 秒、2 次尝试。随后模型 Health 探针得到一次 HTTP 200，Content-Type 为 `application/json; charset=utf-8`，响应结构为顶层 `status`，受限正文是 `{"status":"ok"}`。机器状态在此之后才从 `MODEL_LOADING` 进入 `MODEL_READY`，随后进入 `EVALUATING`。本次没有实际观察到 503；但就绪门满足了“200 且 status=ok 后才评测”的合同。

## 4. 检索指标

Recall/MRR 只对有非空相关集合的查询做宏平均；无证据返回率单独统计。

### Calibration（30 正例 / 30 无证据）

| 通道 | 适用正例 | R@1 | R@5 | R@10 | MRR | 无证据非空 |
|---|---:|---:|---:|---:|---:|---:|
| Lexical | 30 | 0.666667 | 1.000000 | 1.000000 | 0.816667 | 30/30 |
| Symbol | 6 | 1.000000 | 1.000000 | 1.000000 | 1.000000 | 0/6 |
| PackageRelation | 15 | 0.533333 | 1.000000 | 1.000000 | 0.766667 | 15/15 |
| Vector | 30 | 0.266667 | 0.966667 | 1.000000 | 0.556984 | 30/30 |
| 四通道 RRF | 30 | 0.666667 | 1.000000 | 1.000000 | 0.833333 | 30/30 |
| Route | 30 | 0.600000 | 1.000000 | 1.000000 | 0.788889 | 30/30 |

### Validation（30 正例 / 30 无证据）

| 通道 | 适用正例 | R@1 | R@5 | R@10 | MRR | 无证据非空 |
|---|---:|---:|---:|---:|---:|---:|
| Lexical | 30 | 0.800000 | 1.000000 | 1.000000 | 0.877778 | 30/30 |
| Symbol | 6 | 1.000000 | 1.000000 | 1.000000 | 1.000000 | 0/3 |
| PackageRelation | 18 | 0.888889 | 1.000000 | 1.000000 | 0.944444 | 21/21 |
| Vector | 30 | 0.533333 | 0.933333 | 1.000000 | 0.706667 | 30/30 |
| 四通道 RRF | 30 | 0.633333 | 0.900000 | 1.000000 | 0.775397 | 30/30 |
| Route | 30 | 0.833333 | 1.000000 | 1.000000 | 0.916667 | 30/30 |

四通道 RRF 相对 Lexical 的 Validation R@1 下降 0.166667、R@5 下降 0.10、MRR 下降 0.102381；R@10 相同。PackageRelation 对 Lexical/Vector 分别有 4/10 个 Top-1 补救，但严格 Top-10 独有相关命中为 0，并记录了 159 个错误候选。它说明结构通道能重排部分查询，但没有证明新增不可替代证据。

真实评测总耗时 25.001224 秒，其中向量索引 4.510973 秒；Validation 的 60 次 Vector 查询共 9.843226 秒。服务采样峰值工作集 1,299,787,776 字节（约 1,239.6 MiB）。

## 5. 固定拒答阈值

本次结果只保存每个查询的路径排名，没有保存 Vector 原始分数或 top-2 margin。`top_score >= 0.70` 所需的覆盖率、误接受率、误拒绝率和精确率均为 `not_measured`。因此：

- 不能从现有结果重算该阈值；
- 不能用其他数据版本的历史阈值结果填补；
- 生产阈值继续关闭；
- 无阈值时 Vector 和 RRF 对 Validation 的 30/30 个无证据查询都返回候选，拒答能力没有建立。

## 6. `service_exit_code=1` 的解释

该退出码已有明确代码和执行证据支持其为受控终止：

1. `AF010S2_HOST_RUNNER.py` 在评测成功后先设置 `status=completed`、状态 `COMPLETED` 和外层退出状态 0；机器状态历史也记录 `EVALUATING → COMPLETED`。
2. 随后的 `finally` 调用共享 `_stop_owned_process()`。该函数在子进程仍运行时调用 `process.terminate()`，等待退出，并写入 `termination="terminated"`。
3. 本机 Windows Python 3.12.7 的 `subprocess.Popen.terminate()` 实现调用 `TerminateProcess(handle, 1)`，因此受控终止后的真实子进程退出码为 1。
4. 机器结果记录 `termination="terminated"`、`owned_process_stopped=true`、`exit_code=1`、`port_released=true`；若服务已自行退出，代码会记录 `exited_without_cleanup_signal`，本次没有出现该状态。
5. 保存的 stderr 摘要包含模型加载、监听和非致命 Embedding 警告，没有崩溃或异常退出证据。完整原始 stderr 没有持久化，因此审计不声称排除了所有不可见日志事件；但现有状态与代码路径足以解释退出码 1。

所以 `status=completed` 表示 Runner 业务流程成功；`service_exit_code=1` 表示完成后主动终止仍在运行的 Windows 子进程。两者语义不同且不冲突。

## 7. 清理和完整性

- 当前没有名为 `llama-server` 的进程。
- 本次临时运行时目录已不存在；机器结果记录 51 个文件复制前后清单一致、无额外文件、随后删除。
- 当前会话查询端口 57577 的监听表时，CIM 访问被拒绝，不能以该只读查询独立确认当前端口状态；Runner 自身在终止后用本地绑定检查记录 `port_released=true`。
- Runner 保护的 135 个文件已重新计算并与 `protected_after` 比较，差异为 0。
- 正式索引：32,768 字节，SHA-256 `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296`。
- 只读证据：2,885 字节，SHA-256 `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733`。
- 原始 51 文件运行时当前 manifest SHA-256 为 `FEE757BB981EB76D4BF9D348BF1E94C95BF99DC46827842950239E0C595A5BD6`，与运行前记录一致。
- GGUF、EXE、实现 DLL 已重新哈希，均与 Runner 的运行后记录一致。
- 结果写入独立的 `AF010S2_RESULTS.json`，没有覆盖历史结果。

`tests/.tmp/af009c-llama-server` 在本任务开始前已经存在，本审计没有删除或修改它。

## 8. 阶段判定

| 判定项 | 结论 | 理由 |
|---|---|---|
| A. 本次真实宿主执行 | **完成** | readiness、165 次真实请求、评测、受控停止和完整性记录齐全 |
| B. AF-009G 真实 Vector / 完整 RRF 证据 | **取得可审计结果** | 本次独立三仓库 Validation 保存了 Vector 与四通道 RRF 的逐查询排名和汇总；这是执行完成，不等于质量通过 |
| C. 固定拒答阈值 | **未通过 / not_measured** | 本次未保存分数，无法计算四项指标；无阈值时无证据返回率为 100% |
| D. AF-010 产品接入 | **DEFERRED，不批准** | RRF 相对 Lexical 退化、无独有 Top-10 命中、错误候选多、拒答未验收、标签非人工 |
| E. AgentForge 第一版离线交付 | **PASS，可冻结** | 现有离线 MVP、代码索引与受限只读 Agent 可独立交付；明确排除 AF-010、生产拒答、Reranker 和真实聊天验收 |

## 9. 回归测试

`python -X utf8 -B -m unittest discover -s tests -v`

- 退出码：0。
- 运行：188。
- 通过：183。
- 失败：0。
- 错误：0。
- 跳过：5。

5 个跳过项没有计入通过数，原因均为当前 Windows 权限不足，无法构造测试需要的文件符号链接、扫描后替换符号链接、目录符号链接、junction 或硬链接。测试没有运行 Host Runner，也没有发送网络或模型请求。

## 10. 最小后续动作

第一版冻结交付不需要用户再手工执行操作。若未来重新申请 AF-010 产品化，最小缺口是：保留每条 Validation 的原始 Vector 分数和 margin，在不复用当前 Validation 调参的前提下完成新的独立阈值验证，并获得人工审核标签；还需先证明融合相对默认 Lexical 有稳定净增益。阶段 2B-3S 的真实聊天回答与引用验收仍是未知状态，本审计没有重试。
