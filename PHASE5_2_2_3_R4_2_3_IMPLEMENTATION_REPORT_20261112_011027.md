# Phase 5.2.2.3-R4.2.3 实施与验收报告 (20261112_011027)

## 结果

**PARTIAL**。三次 DeepSeek Flash C2 已执行完毕，达到本轮最大尝试次数。第 3 次完成完整产品执行链并保护正式数据，但严格 Answer Contract 为 `LIMITED`，所以不能标记 `PHASE_5_2_2_3_R4_2_3 = PASS`，也不支持冻结 v0.1。

## 修改及其目的

| 文件 | 修改 |
|---|---|
| `agentforge/product_retrieval.py` | 为代码标识符/公式类仓库问题提供受问题文本支持的检索相关性信号；来源、哈希及候选验证不变。 |
| `agentforge/conversation_orchestrator.py` | 为 Read/source 拒绝输出稳定、脱敏的错误码和字段级诊断，帮助区分输入范围错误与来源映射失败。 |
| `agentforge/evidence_evaluator.py` | 为候选拒绝增加稳定 reason code；保持原 relevance/sufficiency 门槛。 |
| `agentforge/dialogue_router.py` | 独立采用 Planner 的 repository-retrieval 决策，而非只从 intent 推导检索。 |
| `PHASE5_2_2_3_HOST_ACCEPTANCE.py` | 读取实际嵌套执行结果；把单次被拒 Read 后由不同调用 ID 成功恢复视为有界恢复；仍拒绝未恢复/重复错误。 |
| `tests/test_conversation_agent.py` | 验证失败诊断不泄漏参数值。 |
| `tests/test_phase5_2_2_3_runner.py` | 验证恢复路径通过，重复失败/无成功 Read 路径拒绝。 |

## 验证结果

- 定向测试：最新覆盖修改的 focused assertions PASS（见测试日志）。
- 相关正序/逆序：348/348、348/348 PASS。
- 完整离线：1073 运行，1070 通过，0 失败，0 错误，3 跳过。
- 静态依赖：21/21 PASS；编译检查（排除故意语法错误 fixture）PASS；`git diff --check` PASS。
- 真实宿主：三次 C2 合计 21/21 DeepSeek Chat 成功，0 失败、0 重试；第三次真实执行完成。严格合同 LIMITED，故 C2 不通过。

## C2 证据链

Planner（repository + tool）→ SQLite/Chroma 混合检索（1 次 Orchestrator 查询）→ 1 个可信来源 Evidence → Agent Search Observation → 越界 Read 被拒 → 有界 replan → Read 成功 → 3 Observations → Evidence 上下文注入（hash/source/citation 均匹配）→ DeepSeek 最终答案非空且一致 → Session 保存/恢复 PASS。

最终引用：`synthetic_calculation.py:1-5`；权威内容 SHA-256：`ab0cd7c85abf3ccb0ec3d27dd3fd05442aa95cf169a07f9111bf0d134c6a68ed`。回答原文未写入验收结果，保留 SHA-256 摘要。

合同结果 `LIMITED`，说明 citations 是 request-local，未执行独立语义蕴含验证；没有调整 Answer Contract。

## 数据与仓库安全

正式 Chroma 599 向量及全 Collection digest 前后相同；正式 PostgreSQL 12 张表 PK/行数快照前后相同；仓库登记 7 条及摘要未变；活动 SQLite 195 条及摘要未变。合成对话、1 个向量、仓库登记、SQLite 索引和目录均删除并逐项验证。数据保护/清理均 PASS。

## 保留限制

- 不把 `LIMITED` 写成 PASS。
- Runner 原始宿主 JSON仍保留运行时旧断言给出的 FAIL；本轮仅用修复后 Runner 离线重评执行条件，并未篡改原始文件。离线重评的执行子检查全部 true，合同状态仍 LIMITED。
- C2 3/3 尝试已用完；不再发请求。
- 3 项 skip 的单项名称/原因未记录于 quiet 完整日志，按 NOT_MEASURED 说明。
