# AF-009G-R4：模型就绪竞态修复与真实 Vector 验收报告

日期：2026-10-26  
结论：执行器已实现并测试 `PROCESS_STARTED → TCP_LISTENING → MODEL_LOADING → MODEL_READY → EVALUATING → COMPLETED/FAILED` 状态机，TCP 不再被当作模型就绪。唯一一次真实启动在 `MODEL_LOADING` 阶段收到可解析但不含顶层 `status` 的 `/health` JSON，按规则以 `MODEL_HEALTH_INVALID_RESPONSE` 失败；没有进入 `MODEL_READY` 或 `EVALUATING`，Embedding 请求为 0。真实 Vector、完整 RRF 和阈值泛化仍未完成。

## 1. 实现修复

- `AF009GR3_HOST_RUNNER.py` 现在为 AF-009G-R4 写入独立的 `AF009GR4_RESULTS.json`，不会覆盖 R3 或更早结果。
- TCP readiness 后进入 `MODEL_LOADING`；只有 `/health` 返回 HTTP 200 且 JSON `status` 严格为 `ok` 才进入 `MODEL_READY`。
- `/health` 使用标准库直连 `127.0.0.1`，不读取代理、不跟随重定向；单次超时 2 秒、轮询间隔 1 秒、monotonic 总上限 120 秒。
- HTTP 503 仅在响应明确声明 `loading` 或 `loading model` 时继续。支持顶层 `status` 和明确的嵌套 `error.message`；其他状态、非法 JSON、缺少可解释状态、子进程退出或超时均分类失败。
- `MODEL_READY` 是调用评测器的硬门；未通过时不能发送 `/v1/embeddings`。
- Provider 新增非敏感请求计数：attempts、sent、successes、failures。每次文本只发一次；HTTP 503 不重试。
- 原 stdout/stderr 继续并行排空，拥有的子进程退出码、终止方式、端口释放和临时运行时清理由统一 finally 路径记录。

真实执行后又补充了安全的嵌套错误结构识别及响应键记录，用于避免未来丢失响应形状证据。该补充只经过 Mock 验证；本轮没有再次启动服务。

## 2. Mock 和回归测试

覆盖了：

- 503 `loading model` 后 200 `ok` 才允许评测。
- 连续 503 到 120 秒边界时产生 `MODEL_READINESS_TIMEOUT`，评测回调不执行。
- 子进程提前退出、非预期 HTTP 状态、非法 JSON、缺少状态字段和明确的嵌套加载错误。
- Embedding HTTP 503 仅请求一次，不自动重试。
- stdout/stderr 排空、真实退出码、terminate 超时后的自有 PID kill 路径。

最终测试：

- 定向：`python -X utf8 -B -m unittest tests.test_af009gr3_host_runner tests.test_local_embedding_provider tests.test_generalization tests.test_evaluation tests.test_provisional_fusion -v`；47 项通过、0 失败、0 错误、0 跳过，退出码 0。
- 完整：`python -X utf8 -B -m unittest discover -s tests -v`；133 项运行、128 项通过、0 失败、0 错误、5 项跳过，退出码 0。跳过项仍为当前 Windows 权限无法构造的符号链接、junction 或硬链接，不计入通过。

## 3. 唯一一次真实启动

- 模型、原 EXE 和实现 DLL SHA-256 均与固定值匹配；原运行时为 51 文件，临时副本逐项一致。
- 正常宿主 loopback 自检通过。
- 本轮 PID `32212`，只绑定 `127.0.0.1:61245`，TCP readiness 成功。
- 状态序列实际为 `PROCESS_STARTED → TCP_LISTENING → MODEL_LOADING → FAILED`。
- 首次 `/health` 返回了可解析 JSON，但当前执行版本没有保存 HTTP 状态与 JSON 键；它因缺少非空顶层 `status` 被分类为 `MODEL_HEALTH_INVALID_RESPONSE`。stderr 同时仍只有 `load_model: loading model`，没有 `model loaded`。
- 因健康响应没有形成可解释的 503/loading 或 200/ok 观测，本轮没有记录可用的 health 503/200 计数。不得把 R3 的 Embedding 503 混写为 R4 的 health 结果。
- `MODEL_READY`：未通过；等待约 0 秒即在首次健康响应上明确失败。
- Embedding 请求：attempts 0、sent 0、successes 0、failures 0。没有聊天、补全或生成请求。
- 资源早期样本：Working Set 265,121,792 字节，Peak Working Set 265,121,792 字节；这是加载早期失败点，不是完整模型或评测峰值。
- 本轮 PID 由 finally 路径停止；退出码 1 是显式 terminate 后返回值，不是服务自行崩溃证据。端口已释放，临时运行时已删除。

机器结果：`AF009GR4_RESULTS.json`，11,151 字节，SHA-256 `9107956E5818A4D8B88A822E8D07AB1855607E4815F78564F547BE7647DA7D27`。

## 4. Vector、RRF 和阈值

- 没有有效向量，Vector Calibration/Validation 均未测量。
- 没有完整 RRF Calibration/Validation 指标；历史不含 Vector 的结果没有冒充完整融合结果。
- `top_score >= 0.70` 没有新分数，覆盖率、误接受率、误拒绝率和精确率均未测量。
- 生产阈值保持关闭；默认 Retriever、RRF 产品配置、冻结查询、ground truth、split plan 和 leakage group 均未修改。

## 5. 完整性和阶段结论

- 正式 SQLite：32,768 字节，SHA-256 `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296`。
- `read_only_evidence.json`：2,885 字节，SHA-256 `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733`。
- AF-009G fixture/split、R3 结果、AF-009G 离线结果和 AF-009FA 结果哈希均保持不变；模型和原运行时关键哈希也不变。
- 当前 `llama-server` 进程数为 0，`af009gr4-runtime-*` 临时目录数为 0。未访问 Agentexpr。

AF-009G 仍未完成。新的真实阻塞已经从 TCP 权限问题前移到 llama-server 加载期 `/health` 响应合同：本次响应形状没有被旧探针识别和保留。执行器现在能够识别明确的顶层状态或嵌套加载错误并记录响应键，但这部分真实行为尚未再次验证。AF-010 不具备基于独立真实 Vector Validation 的进入条件；不得启用生产阈值或产品化 RRF。
