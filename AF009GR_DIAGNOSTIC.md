# AF-009G-R：本机 TCP 诊断与真实 Vector 验收状态

日期：2026-10-26  
结论：基础回环测试在客户端连接阶段被 Windows 以 `WinError 10013` 拒绝。按阶段停止条件，本轮没有启动 llama-server、没有发送 Embedding 请求，也没有生成 Vector 或完整 RRF 指标。

## 分层诊断

使用 Python 标准库在同一进程中完成独立回环测试，不涉及模型或 HTTP：

| 步骤 | 结果 | 耗时 | 证据 |
|---|---|---:|---|
| bind `127.0.0.1:0` | 成功 | 0.000036 秒 | Windows 分配端口 54586 |
| listen | 成功 | 0.000014 秒 | listener 进入监听状态 |
| connect `127.0.0.1:54586` | 失败 | 0.000125 秒 | `PermissionError`，errno 13，`WinError 10013` |
| accept | 超时 | 3.008925 秒 | 客户端未能到达 listener |
| ping/pong | 未执行 | — | connect 未建立 |

所有 socket 均已关闭，总耗时 3.011225 秒。该证据把问题定位到本机 TCP connect 权限路径：bind 和 listen 本身可用，客户端连接被操作系统拒绝。这不是 llama-server 或模型实测故障，因为本轮尚未执行运行时。10013 的具体策略来源仍未知；没有把它归因于防火墙、杀毒软件或某一安全产品。

监听表查询是另一条独立限制：`Get-NetTCPConnection` 因无法访问 CIM 资源失败，HRESULT `0x80131501`；`netstat -ano -p tcp` 只返回表头。监听表读取受限不能解释 connect 失败，二者分别记录。

## 资产预检与停止决定

- Qwen3-Embedding-0.6B Q8_0：639,150,592 字节，SHA-256 `06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439`。
- `llama-server.exe`：9,216 字节，SHA-256 `F8DF891A5E4D10FA8CFC0F86C8D99617A06EDD18D046E18B59EC3354F17FC12D`。
- `llama-server-impl.dll`：9,160,704 字节，SHA-256 `F020D94DF5E91840F2C9626B86F9AFE414C16D17ED5E1F41FE9CB614D3CDCDCB`。
- 原运行时仍为 51 个文件；三项关键哈希均与既有记录一致。

由于基础 loopback connect 未通过，本轮在模型启动前停止。没有创建临时运行时副本，没有复制 GGUF，没有启动服务，没有调用 `/v1/embeddings`，也没有调用聊天、补全或生成接口。

## 评测状态

AF-009G 的冻结 40 条查询、split plan 和离线结果保持不变。真实 Vector、含 Vector 的 RRF `k=60`、向量维度/有效性、向量耗时和独立 Validation 指标均未测量。历史 `top_score >= 0.70` 没有在 AF-009G 数据上评估；生产阈值保持关闭，默认 Retriever 未改变，RRF 未接入产品配置。

已完成的 AF-009G 离线指标继续有效，但没有因本轮诊断而升级为完整四通道结果。AI 标签仍为 `human_reviewed: false`，不能表述成人工 ground truth。

## 测试与完整性

- 定向测试：`python -X utf8 -B -m unittest tests.test_generalization tests.test_local_embedding_provider tests.test_evaluation tests.test_provisional_fusion -v`；38 项通过、0 失败、0 错误、0 跳过，退出码 0。
- 完整测试：`python -X utf8 -B -m unittest discover -s tests -v`；124 项运行、119 项通过、0 失败、0 错误、5 项跳过，退出码 0。5 项为当前 Windows 权限无法构造的物理符号链接、junction 和硬链接测试，不计入通过。
- 正式索引保持 32,768 字节、SHA-256 `AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296`。
- `read_only_evidence.json` 保持 2,885 字节、SHA-256 `CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733`。
- 泛化 fixture、split plan、历史 AF-009G 结果、AF009FA 历史结果及其他受保护评测文件的大小和 SHA-256 均与本轮开始时一致。
- 没有本轮启动的 llama-server 进程或临时运行时。既有 `tests/.tmp/af009c-llama-server` 未触碰。未访问或修改 Agentexpr。

结构化证据保存在 `AF009GR_DIAGNOSTIC_RESULTS.json`。

## 阶段判断

AF-009G-R 没有达到真实 Vector 完成标准。已确认的阻塞是当前执行环境中标准库 loopback connect 收到 Windows 10013；产生该拒绝的具体系统策略仍未知。AF-010 暂不具备依据独立 Vector Validation 进入产品化的条件。下一次尝试应先在不改变系统安全设置的前提下，让同一标准库回环自检完整通过，再按原冻结配置执行一次受控服务启动；不应在当前失败环境中反复启动模型。
