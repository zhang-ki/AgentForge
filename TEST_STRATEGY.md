# AgentForge V1 测试策略

2026-10-27。专项状态：`PARTIAL`；自动化回归通过，但覆盖率工具不可用，尚无法核验覆盖率门槛。

生产模块为 `agentforge/`；`tests/` 包含单元、协议 Mock、隔离 SQLite 和合成仓库集成测试。真实 Qwen 验收是历史宿主结果，见 `AF011_CHAT_ACCEPTANCE_RESULTS.json` 的 attempt `3bb475f1ee2b4a98be6fccd308a7f06d`，本专项没有重跑。

| 范围 | 可复核测试 | 当前边界 |
|---|---|---|
| Provider | `tests.test_providers`、`tests.test_local_embedding_provider`、`tests.test_af009gr3_host_runner` | 通过 Mock 检验认证、非回环、代理/重定向、响应/超时、无重试和就绪状态；不构成新真实服务结果 |
| 引用与回答 | `tests.test_context`、`tests.test_af011_product`、`tests.test_quality_boundaries` | 请求内 ID、错误路径/范围、过期数据、独立合同和真实临时文件 CRLF/Unicode 读取；引用有效不证明语义蕴含 |
| 索引与路径 | `tests.test_scanner`、`tests.test_store`、`tests.test_symbols`、`tests.test_package_s2` | 隔离 SQLite、路径、文件变更、错误和增量全量一致性；真实 Windows 链接权限相关 5 项仍跳过，模拟不是 OS 实测 |
| 检索与关系 | `tests.test_retriever`、`tests.test_embeddings`、`tests.test_package_relations`、`tests.test_package_s2`、`tests.test_provisional_fusion` | Lexical、Symbol、模拟向量、保守关系解析及离线融合；不启用生产融合或阈值 |
| 只读 Agent | `tests.test_agent_loop`、`tests.test_quality_boundaries` | 白名单、路径、错误、步数与合作式超时；通用外部取消未单独测量 |
| CLI 集成 | `tests.test_integration`、`tests.test_cli`、`tests.test_af011_chat_acceptance` | 合成仓库、内存数据库、Mock Provider、证据门禁与清理；真实案例沿用历史 PASS |

核心定向执行：`python -X utf8 -B -m unittest tests.test_providers tests.test_context tests.test_af011_product tests.test_agent_loop tests.test_quality_boundaries -v`。全量执行：`python -X utf8 -B -m unittest discover -s tests -v`。测试不得依赖正式索引；参见 `tests/support.py` 的专用临时目录和内存 SQLite。性能小样本、清理和覆盖率限制分别见本专项其他报告。

端到端场景核验（均为本轮离线测试，全部属于完整回归结果）：

| 场景 | 测试证据和结果 |
|---|---|
| 建立索引并检索代码、确认路径与行范围 | `test_full_cli_pipeline_preserves_synthetic_source`：通过，内存 SQLite，验证来源快照不变 |
| 精确符号查询 | `test_symbol_relation_reference_and_document_evidence_keep_provenance`：通过，验证符号优先及来源身份 |
| Mock Provider 有效引用回答 | `test_explicit_llama_provider_receives_retrieved_context_and_validates_answer` 和 `test_worker_exercises_real_cli_path_with_memory_index_and_one_mocked_chat`：通过，非真实推理 |
| 非法响应、伪造引用和失败分类 | `test_invalid_response_diagnostics_distinguish_shape_and_empty_final_content`、`test_citation_failure_is_classified_after_successful_http_response`：通过；模拟服务 |
| 无证据时请求前拒答 | `test_low_confidence_missing_symbol_is_refused_before_model_request`、`test_evidence_gate_keeps_request_count_zero_with_explicit_audit`：通过 |
| 跨仓库和路径越界 | `test_product_retrieval_rejects_index_bound_to_another_repository`、`test_read_rejects_traversal_sensitive_files_and_large_ranges`：通过 |
| Agent 步数、超时和禁止工具 | `test_step_budget_and_timeout_stop_without_retry`、本轮 `test_exact_step_budget_completes_without_an_extra_tool`、`test_forbidden_tool_failure_is_audited_without_followup`：通过。外部取消并非当前已验证功能，`NOT_MEASURED` |
| 失败后正式数据 | 全量测试后的 135 个冻结保护路径及正式数据库/证据 SHA-256 与基线一致；隔离测试验证源仓库未修改 |

覆盖率目标为全生产行/分支 90%/85%、核心安全模块 95%/90%；只有可复现的真实覆盖率工具实测达标才能将覆盖率门槛标为通过。对平台特定链接行为保留真实测试与明确跳过，不以模拟结果替代。
