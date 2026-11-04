# DeepSeek Thinking 输出预算修复记录

## 真实失败证据

原始结果 `AF012_MULTIMODEL_HOST_RESULT_e10daf56dd6e4dd5999671ba79546b2c.json` 保留不变：发现 2 个模型，4 次聊天请求均 HTTP 成功、无重试。Flash 普通和 Thinking、V4 Pro 普通问答通过；V4 Pro Thinking 被拒绝，错误是输出长度上限导致部分答案不完整。Provider 的 `finish_reason == "length"` 分支产生该错误，拒绝行为正确。

修复前 Runner 对普通问答和 Thinking 均使用 `min(48, 模型最大输出)`。原始结果只保存每个模型普通问答的 usage：Flash completion 5，V4 Pro completion 9；它没有保存两次 Thinking 的 usage 或原始 finish_reason 字段，因此无法还原 Thinking 的实际推理 token 数，也无法证明模型需要恰好多少 token。

## 修改

- 普通和流式测试保留 `min(48, 模型最大输出)`；Thinking 使用 `min(512, 模型最大输出)`。发现模型数最多 2，聊天请求总数最多 6，仍无自动重试。
- 每个案例分别记录输出上限、usage、finish reason 和应用错误码；没有发出请求时不沿用上一请求的数据。总结果附 `usage_by_case`。只有 API 实际返回 reasoning token 数时才记录，不从回答长度推算。
- 修正流式 usage 二次规范化丢失 `reasoning_tokens` 的问题，并在新请求开始时清理上一请求的应用错误状态。
- 保留 `length` 时拒绝部分答案、证据引用校验和合成数据范围；没有修改正式数据库。

## 离线验证

项目虚拟环境中，`python -X utf8 -B -m unittest tests.test_multimodel tests.test_multimodel_host_runner -q`：59 项通过，退出码 0。覆盖 Model Registry、Token Budget、Provider 与 Runner，包含 Thinking 正常结束、`length` 截断拒绝、单独 usage、模型输出上限和流式推理 token 统计。

`python -X utf8 -B -m unittest discover -s tests -q`：524 项运行，518 通过、0 失败、0 错误、6 跳过，退出码 0；日志为 `coverage/AF012_THINKING_FIX_FULL_FINAL_20261104.log`。跳过项不计入通过数。上述结果均为离线测试，不代表真实模型复验。

## 真实宿主复验状态

本次 Codex 环境设置了 `CODEX_SANDBOX_NETWORK_DISABLED=1`；直接运行授权的 PowerShell 脚本返回 `AuthorizationManager check failed`、退出码 1。未绕过限制，未读取凭据或发送真实模型请求，也没有生成新的真实模型结果。

修复后两个模型普通与 Thinking 的真实结果、usage 和推理 token 数均为 **NOT_MEASURED**。可在普通 Windows PowerShell 中执行一次：

```powershell
powershell -NoProfile -File "D:\agent\AgentForge\AF012_MULTIMODEL_HOST_ACCEPTANCE.ps1"
```

脚本会要求用户确认发送合成测试，不自动重试。即使 Smoke Test 全部通过，也不表示 AF-012 1.2 的 PostgreSQL、Qwen、Chroma、完整 RAG 和多轮验收已完成。
