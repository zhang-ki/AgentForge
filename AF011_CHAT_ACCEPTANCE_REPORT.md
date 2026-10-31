# AF-011 真实聊天验收报告

当前状态：`PASS`

本报告由有界 Host Runner 更新；历史尝试保存在机器结果的 `attempts` 数组中。
API key、Authorization 头和完整环境变量不会写入结果。

## 最近一次尝试

- attempt_id：`3bb475f1ee2b4a98be6fccd308a7f06d`
- 执行环境：`windows_host_candidate`
- 模型服务启动：`True`
- 模型就绪：`True`
- 请求计数 attempted/sent/succeeded/failed/retries：`1/1/1/0/0`
- 服务退出码：`1`
- 端口释放：`True`
- 结论：One real Qwen chat request completed through AgentForge ask; the cited source, deterministic semantic assertion, and no-evidence refusal all passed.

## 最终证据审计

- 状态链为 `PROCESS_STARTED → TCP_LISTENING → MODEL_LOADING → MODEL_READY → ASKING → COMPLETED`。
- Health 先返回一次结构合法的 HTTP 503 `Loading model`，随后返回 HTTP 200 `{"status":"ok"}`；只有就绪后才发送聊天请求。
- 唯一真实请求由 `llama-server` Provider 发送并收到 HTTP 200；`finish_reason=stop`，final content 149 字符，无 reasoning-only 或截断迹象。
- 模型回答引用 `[E1]`。应用将该请求内 ID 映射为 `src/cache.py:4-6`，原文为 `def refresh_access_token(session_id)` 及返回 `f"offline-{session_id}"` 的函数体；路径、范围、原文和 `resolved` 来源身份均来自检索证据，而非模型自报。
- Answer Contract 状态为 `limited`、claim 类型为 `inference`，因为合同本身不把引用成员关系当作自然语言蕴含证明；独立确定性语义检查的 8 个断言全部为 true，因此案例语义状态为 `PASS`。
- 无证据案例在 Provider 前返回 `refused`，无回答暴露，聊天请求计数为零，语义状态为 `PASS`。

## 应用层案例

### evidence_question

- 状态：`PASS`
- Provider：`llama-server`
- 引用/回答合同：`limited`
- 语义检查：`PASS`

实际回答：

The function `refresh_access_token` returns the string `"offline-{session_id}"`, where `{session_id}` is the input argument. This is defined in [E1].

### no_evidence_question

- 状态：`PASS`
- Provider：`evidence_gate_refusal`
- 引用/回答合同：`refused`
- 语义检查：`PASS`

## 历史离线故障修复（保留）

- 对应真实尝试：`c9913d5ceaaa4da9b8d7e8d6fd556df6`（仍为 `FAIL`）。
- 已确认根因：The prior model contract required and exposed application-owned path/line syntax while response validation also treated broad path-like line text as an invalid citation. The real response reached HTTP 200 with 163 final characters and then triggered that validator. The exact triggering token cannot be recovered because the legacy result intentionally did not retain the response text or offending snippet.
- 响应子类型：`not_measured`。
- 该修复记录当时状态为 `OFFLINE_FIX_VERIFIED/NOT_EXECUTED`；其后的真实 attempt `3bb475f1ee2b4a98be6fccd308a7f06d` 已完成并通过。历史失败和原始证据未被覆盖。

## 完整性

- 正式索引：`{"bytes": 32768, "sha256": "AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296"}`
- 只读证据：`{"bytes": 2885, "sha256": "CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733"}`
- Agentexpr 访问：`False`
- 135 个冻结保护路径：0 个变化、0 个缺失。
- GGUF、原始运行时 EXE 与实现 DLL 的运行前后 SHA-256 一致。
- 隔离运行时复制 51 个文件，运行后仍匹配来源，且任务临时目录已删除。
- 自有服务在清理前仍运行；Runner 随后主动 `terminate()`，记录 `termination=terminated`、`owned_process_stopped=true`、`exit_semantics=runner_initiated_termination` 和 `port_released=true`。因此退出码 1 是 Windows 主动终止该自有进程的结果，没有服务自行崩溃证据。
- 收尾审计时没有 `llama-server` 进程或 `af011-chat-*` 临时目录。

## 离线回归

- 命令：`python -X utf8 -B -m unittest discover -s tests -v`
- 运行/通过/失败/错误/跳过：`219/214/0/0/5`
- 退出码：`0`
- 跳过原因：Windows 当前权限无法创建符号链接、junction 或硬链接；跳过未计入通过。
