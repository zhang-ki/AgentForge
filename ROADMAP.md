# AgentForge Roadmap

## Phase 4.3 — tool discovery and generic execution boundary (2026-11-08)

- [x] Build conversation-Planner tool metadata from the same ToolRegistry and explicit authorization policy used by execution; do not expose schemas or permissions merely because a tool is registered.
- [x] Keep Answer Intent independent from Direct/Agent execution strategy; explicitly requested, authorized tools can be used for General turns without requiring a repository.
- [x] Refresh the authorized catalog after `TOOL_UNAVAILABLE`, excluding the failed tool while preserving permitted alternatives; stop safely when none remain.
- [x] Add bounded correction for unknown selected tool/schema IDs and preserve fail-closed PlanValidator behavior.
- [x] Fix the host approval restore probe to validate a pending checkpoint without requiring uncommitted turn messages.
- [x] Pass all four requested real DeepSeek Flash synthetic lifecycle scenarios: failure fallback, approval pause/resume, long-result reference paging, and tool cancellation.
- [x] Run 254 focused tests and the complete 866-test suite; both had zero failures/errors.
- [x] Measure all `agentforge/*` coverage without adding exclusions: 86.59% statements / 77.17% branches / 83.96% combined.
- [ ] Reach the existing 90% statement / 85% branch coverage goals; remaining coverage work should be driven by meaningful untested behavior.
- [ ] Run the broader host matrix, Qwen, DeepSeek V4 Pro, and provider-native streaming; these remain `NOT_MEASURED` by this scoped phase.

Phase 4.3's four-scenario scope is **PASS**. Overall release readiness remains **PARTIAL** because the broader host matrix and coverage goals are incomplete. Evidence: `PHASE4_3_TOOL_BOUNDARY_REPORT_20261108.md`, `PHASE4_3_TOOL_BOUNDARY_RESULTS_20261108.json`, and `PHASE4_3_ARCHITECTURE_AND_DEPENDENCY_GRAPH_20261108.md`.

## AF042 host follow-up (2026-11-08)

- [x] Fix bounded persistence of oversized safe request-diagnostics metadata without changing the 8,192-byte cap.
- [x] Align the synthetic host Agent step budget with the product's existing eight-step bound after the real trace showed a 3+4 call plan rejected by the runner's six-step override.
- [x] Re-run DeepSeek `multi_tool_dependency`: 10/10 requests succeeded; four calls and three replanning rounds completed; selected-scenario assertions passed. This did not exercise embedding/vector retrieval.
- [x] Run focused regression (109/109 passed) and complete offline regression (808 run, 798 passed, 0 failed/errors, 10 skipped).
- [ ] Diagnose the remaining route/plan-contract scenarios before running another full host matrix. The preserved full run remains 2/15 scenarios passed; the targeted rerun does not change that full-run result.
- [ ] Measure post-fix line/branch coverage; the prior 86.2779% / 76.7955% measurement predates these changes.

## Tool discovery and model-aware output budgets (2026-11-08)

- [x] Separate ToolRegistry discovery/schema batching and revision-aware cache from Planner selection, PlanValidator checks, and AgentExecutionEngine execution.
- [x] Bound discovery by the finite catalog and a limited no-progress correction; keep schema rounds independent from tool execution and replanning.
- [x] Route CLI, ordinary conversation, providers, streaming, and the host runner through shared output-budget resolution; remove the host runner's fixed 256-token answer override.
- [x] Add `--output-budget concise|standard|long|max` and console `/output-budget ...`; apply model, provider, adapter, and context ceilings without letting Qwen inherit DeepSeek capacity.
- [x] Add a synthetic DeepSeek long-answer host case and offline coverage for multi-tool schema discovery and budget boundaries.
- [x] Run full branch-enabled regression: 806 run / 800 passed / 0 failed / 0 errors / 6 Windows link-permission skips. Measure all 80 production modules: 86.2779% line and 76.7955% branch; 90%/85% goals remain unmet.
- [ ] Re-run DeepSeek multi-tool and long-answer scenarios with fresh `/model refresh` metadata on an ordinary Windows host; run the independent Qwen and PostgreSQL/Chroma host checks. Current environment blocks local network access, so live acceptance is BLOCKED/NOT_MEASURED.

Overall: **PARTIAL** pending real-host validation and coverage targets. See `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_REPORT_20261108.md` and `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_RESULTS_20261108.json`.

## Phase 4.2 — Unified real-host Agent acceptance (2026-11-07)

- [x] Add a single host Runner through `ConversationOrchestrator.run`, reusing the existing `ConversationAgentExecutor`, `AgentLoop`, and `AgentExecutionEngine`.
- [x] Keep the real Qwen Planner, inject only a run-scoped synthetic ToolRegistry, and add sanitized planner/tool/session/stream/usage diagnostics.
- [x] Separate strict timeout and running-tool cancellation scenarios; verify long results resolve through the durable result reference.
- [x] Run 76 targeted tests and the complete branch-enabled offline suite (706 run / 700 pass / 0 fail / 0 errors / 6 Windows link-permission skips).
- [x] Measure all 80 production modules: 85.9595% line/statement, 76.5784% branch, and 83.3522% combined coverage; line/branch goals of 90% / 85% remain unmet.
- [ ] Run the real Qwen, PostgreSQL and temporary Chroma scenarios on the ordinary Windows host. Current execution is BLOCKED before listener checks and credentials by `CODEX_SANDBOX_NETWORK_DISABLED=1`; Qwen sent 0 requests.
- [ ] Run DeepSeek Flash/V4 Pro separately under the existing cloud-consent process.

Overall: **BLOCKED** pending host execution. Start the required local services yourself, then run `powershell -NoProfile -File "D:\agent\AgentForge\AF042_UNIFIED_AGENT_HOST_ACCEPTANCE.ps1"`. See `PHASE4_2_ACCEPTANCE_REPORT.md` and `PHASE4_2_ACCEPTANCE_RESULTS.json`.

## Phase 2 final integration snapshot (2026-11-06)

**Status: PARTIAL; not released.** General, Repository, Hybrid and Auto share `ConversationOrchestrator`; Agent tool runs use `AgentExecutionEngine` with bounded planning, validation, scheduling, observation-driven replanning, recovery and lifecycle events. Model-aware request assembly covers the known planner, answer, summary and memory-extraction calls. Durable messages are not deleted by request compression.

The latest complete offline regression ran 643 tests: 637 passed, 0 failed/errors, 6 Windows link-permission skips. Coverage across all 79 `agentforge` production modules is 86.5906% statements and 77.0132% branches; both targets remain unmet. Real Qwen/DeepSeek/PostgreSQL/persistent-Chroma acceptance is blocked or not measured because this environment has no network and the local service ports were not listening. The Agent planning path still does not inject approved long-term Memory; semantic entailment is not proven; CLI has some direct Store access. See `AGENTFORGE_PHASE2_FINAL_INTEGRATION_REPORT_20261106.md` and its JSON/coverage companion reports.

## Earlier dialogue-system implementation snapshot (2026-11-06; superseded)

**Status: PARTIAL; not released.** Implemented in this worktree: bounded structured LLM planning for Auto/Hybrid, explicit plan validation, planned repository-query execution, task-outline propagation to answer generation, ConversationOrchestrator, structural evidence assessment, SessionService/MemoryService repository protocols, event-based validation-gated StreamingManager, and model-aware ContextBudgetManager. Planner uses the selected conversation model and fails closed.

Latest project-venv full regression: 582 run, 576 pass, 0 fail/error, 6 Windows link/junction privilege skips, exit 0. Coverage across all 77 agentforge modules is 87.2883% lines and 78.8844% branches, below the 90% / 85% gate. See DIALOGUE_SYSTEM_COVERAGE_REPORT_20261106.md for exact evidence.

No real model or database host acceptance was performed. External live data is refused because there is no ToolRegistry; iterative post-retrieval replan is not implemented; evidence assessment is structural, not semantic; visible streaming waits for Answer Contract validation. CLI still owns composition and several lifecycle commands. See DIALOGUE_SYSTEM_FINAL_REPORT_20261106.md.
## AF-012 1.2 Multi-Model Support (`feature/1.1-core`, 2026-11-04)

Offline implementation is present for a Qwen-default model registry, explicit DeepSeek discovery and chat, per-conversation provider/model/Thinking/effort settings, cloud consent, protected credential storage, and model-aware input/output budgeting. No silent model fallback is enabled. See `MODEL_MANAGEMENT.md` and `AF012_MULTIMODEL_ACCEPTANCE_REPORT_20261104.md`.

The current state is **PARTIAL**: the full offline suite passed 520 tests with 6 Windows filesystem-permission skips, but whole-package coverage is 87.20% lines and 79.49% branches (targets: 90%/85%). Real DeepSeek discovery/chat, fresh Qwen acceptance, formal PostgreSQL migration, desktop restart, 11-case live RAG, and 30-turn live context remain unmeasured because the current execution environment blocks network access. The provided ordinary-Windows runner is a bounded synthetic DeepSeek smoke test, not full 1.2 acceptance. Keep MVP 1.0 frozen; do not push or change its release tag as part of this development work.

## 1.1 core development branch: `feature/1.1-core`

Current work implements three user-facing areas without changing the frozen 1.0 release:

- **Multi-repository lifecycle:** path-stable repository IDs, an isolated SQLite index per repository, active repository register/list/switch/index/rebuild/remove commands, console commands, Chroma repository filtering, and repository-bound PostgreSQL conversation/memory scope.
- **Long context:** preserve complete PostgreSQL message history, persist source-anchored summaries, keep a recent complete-turn window, use a bounded UTF-8-byte budget, and use the explicitly selected local chat model as summarizer only when the request passes the evidence gate. A deterministic labeled extract remains the fallback.
- **RAG evidence:** exact symbol-first routing, resolved cross-file caller/target evidence, mixed Chinese/English symbol matching, and regression cases for similar symbols, stale evidence, static uncertainty, and citation provenance.

Offline synthetic tests are being used for implementation validation. This branch has not received a new end-to-end live PostgreSQL, persistent Chroma, or Qwen acceptance for its changes. Summary semantics and exact token accounting remain unverified; model-generated claims still require user judgment. Do not enable RRF, reranking, or a score-based production refusal threshold.

## Frozen release scope

The current public release is MVP 1.0. It includes read-only repository indexing and evidence retrieval, a local Qwen chat path, PostgreSQL-backed repository-scoped conversations and approved long-term memory, and optional local Chroma vectors. Release evidence is summarized in `RELEASE_NOTES.md`.

The release is frozen for publication. No new retrieval algorithm or production threshold is enabled as part of this release.

## Known limits

- The published MVP 1.0 tag represents one selected repository snapshot at a time. The 1.1 branch adds one local SQLite index per registered repository.
- The 1.1 branch can ask the explicitly configured local model to summarize older turns when evidence gates allow it; otherwise it retains a labeled deterministic extract. The byte budget is not an exact model-token count.
- Static Python analysis has limits around dynamic imports and runtime behavior.
- Citation validation proves that citations map to current supplied evidence, but does not establish that every natural-language claim is semantically entailed.
- Chroma is optional, local, and rebuildable; vectors require explicit indexing and are validated against authoritative records.
- AF-010 research found no stable reason to make RRF a default. RRF, reranking, and production score-based refusal remain deferred.
- Host acceptance covers bounded scenarios. Long-context quality and broad production reliability are not certified.

## Future work policy

The 1.1 integrated acceptance remains partial. A real Windows host run has exercised isolated PostgreSQL, persistent Chroma, Qwen Chat/Embedding, 30 turns, and four fixed RAG cases; it stopped on an HTTP 400 during the cross-file case. The follow-up code now incrementally summarizes old history with bounded input and records request purpose, but this fix has only offline evidence so far. Full RAG completion, desktop lifecycle, PostgreSQL service restart on this branch, and the 90% line / 85% branch coverage targets remain outstanding.

The latest recorded host run (`AF011_1_FULL_HOST_RESULT_d1adc4d20278413a808b465c3eeade4d.json`) stopped at answer turn 20 because the cumulative summary exceeded the 3,000-byte context budget; all 26 requests made before the stop returned HTTP 200. Offline code now replaces rather than nests summaries, compacts oversized legacy summaries when Qwen summarization is available, and raises the default byte budget to 3,500. The full offline suite passes, but the real 30-turn Qwen check must be rerun before claiming this defect is resolved in production. Do not raise the budget further without model-context evidence; UTF-8 bytes are not exact tokens.

Future changes should begin from a reproducible user-visible defect or a clearly scoped product requirement. Preserve the existing security model, keep local data out of version control, and add a focused regression test for any behavior change. Further work is not implicitly authorized by this release.

## AF011.1 / AF012 final defect repair status (2026-11-01)

Offline repairs now require request-local callsite and final-definition evidence roles for cross-file call questions, and the answer contract rejects missing citations for required roles. Recent unsummarized user facts are merged with the persisted source-linked summary ledger and audited against the actual request using IDs only. The old host artifact does not localize the first loss point for the recent marker; the new path is offline-tested only. Targeted tests and the full 437-test offline suite pass (6 Windows link-privilege skips). Full-package coverage remains below target at 87.16% lines and 79.36% branches. The real host run is pending; current status remains `PARTIAL`. Do not change the main branch, publish, or treat the offline gate as host acceptance.

## 最新摘要修复回归（2026-11-01）

- 最新用户宿主记录 `AF011_1_FULL_HOST_RESULT_0a2e4e6806334782914cab8f6ef2c726.json` 在 `semantic_summary` 阶段失败：ValueError，通用错误码 `operation_failed`，Chat 请求 0，真实 Embedding 7/7 成功。隔离数据库、Chroma 客户端、临时目录清理通过；Runner 完整性状态通过（312 项，变化 0）。异常原文未保存。
- 失败路径指向一次性预置历史超过摘要输入上限。已改为按完整问答对进行有界分批摘要，并为摘要阶段增加固定错误码；没有降低模型或证据门禁。
- 当前离线定向测试 47/47 通过；完整测试 389 项、383 通过、0 失败/错误、6 项 Windows 链接权限跳过。覆盖率 87.14% 行、79.47% 分支，既定覆盖率目标仍未达到。
- 待办：普通 Windows 宿主重新运行一次完整 `AF011_1_FULL_HOST_ACCEPTANCE.ps1`，确认真实语义摘要和后续 RAG/Memory/恢复门禁。此前 HTTP 400 的原始宿主失败仍作为历史证据保留；当前分支不得标记完成。

## 最新真实摘要保留缺陷（2026-11-01）

- `AF011_1_FULL_HOST_RESULT_ba5d8c3bbe724c79a33b95f74fa00d1b.json` 证明摘要输入大小问题已越过：Qwen 摘要请求 3/3 成功，且第三次请求输入仍包含早期和中期标记；模型生成的新摘要却遗漏这些信息，验收在 `semantic_summary_missing_facts` 失败。
- 已将摘要存储改为保留先前 checkpoint，再追加当前批次的模型摘要；每段记录消息范围并继续按不可信上下文处理，合并大小超限则保留旧状态并失败关闭。回归测试明确让后续摘要丢弃先前事实，仍要求先前 checkpoint 留存。
- 定向 48/48、完整 390 项（384 通过、6 跳过、0 失败/错误）退出码 0。覆盖率 87.15% 行、79.47% 分支，门槛未达成。需再运行一次真实宿主验证；本次离线结果不等于真实模型通过。
- 详细证据见 `AF011_1_SUMMARY_RETENTION_REPAIR_REPORT_20261101.md` 和同名 JSON 结果文件；原始宿主结果保留为独立文件。

## AF-011.1 cross-file RAG repair (2026-11-01)

The preserved host result `AF011_1_FULL_HOST_RESULT_9bf480b5d3a84a65bf70cb45bfbc85c0.json` remains a real `FAIL` at 9/11 fixed RAG assertions. Offline repairs now search the queried callable rather than incidental trailing query text, preserve separate line ranges for imports/calls/definitions, apply exact-symbol evidence selection without requiring a conversation, and instruct Qwen to cite the actual call/use and definition while describing static uncertainty. Runner stage accounting also distinguishes successful PostgreSQL reconnect from later RAG assertion failures.

Offline targeted, retrieval-order, and full-suite checks pass. The measured production-package coverage is 87.15% lines and 79.42% branches, below the 90%/85% target. The prior two failing cases have not been re-run against real Qwen; do not claim the host acceptance is fixed until that run. See `AF011_1_RAG_FIX_REPORT_20261101.md` and `AF011_1_RAG_FIX_RESULTS_20261101.json`.

## Latest 1.1 host follow-up: summary gate blocked RAG validation

The latest user-run host result `AF011_1_FULL_HOST_RESULT_4c4027afb7f44070a84258c4af916693.json` failed at `semantic_summary_missing_facts`: three semantic-summary calls returned HTTP 200, but the stored checkpoint omitted the designated early/middle/correction facts. Cleanup and integrity checks passed. The 30 answer turns and all fixed RAG cases were not reached. Therefore the cross-file RAG patch is still only offline-verified; resolve summary fact retention before the next full host run. The failed result and previous 9/11 result remain unchanged.


## AF012-CONTEXT-RELIABILITY status (2026-11-01)

Offline code now protects source-linked user statements through rolling summary updates, rejects oversized or incomplete candidates before persistence, and records bounded per-attempt diagnostics. This closes the deterministic data-integrity gap in local tests; it does not prove that Qwen produces semantically complete summaries. The 90% statement / 85% branch coverage gates remain unmet (86.9699% / 79.2236%). Next validation is one full real-host run after the user starts the existing PostgreSQL and Qwen services; do not repeat runs blindly or revise thresholds based on host Validation results.


## Host preflight follow-up (2026-11-01)

An earlier host preflight was blocked before evaluation because the formal PostgreSQL database was at `af012mvp_002`, while the local Alembic head was `af012ctx_003`. No model request or disposable resource was created during that blocked attempt. The user's later host run observed the migration at `af012ctx_003`; its outcome is recorded in the follow-up below.

## Latest context reliability host run (2026-11-01)

The migration preflight is now current: the user-run host result observed `af012ctx_003`. Prechecks and three summary updates passed. The run completed 23 real answer turns, then stopped before answer 24 because the semantic-summary request for messages 33–40 was capped at 256 output tokens and returned `finish_reason=length`. The application retained the existing summary checkpoint and failed closed. A code fix now gives summary calls a bounded 512-token output limit while preserving the ordinary answer limit and truncation guard. Targeted tests (83) and the full suite (424; 418 pass, 6 permission skips, 0 failures/errors) pass. Coverage remains below goal at 86.97% statements and 79.22% branches. The new budget has not yet been verified with Qwen; all fixed RAG cases and the remaining full host stages are still outstanding. See the appended latest-attempt section in `AF012_CONTEXT_RELIABILITY_REPORT_20261101.md`.

## AF011.1 host audit runner follow-up (2026-11-01)

The latest user-run result `AF011_1_FULL_HOST_RESULT_f46f057e6d4e41f7b96e1fb64534fb6e.json` completed 30 real answer turns and all 39 Chat / 51 Embedding requests successfully, then failed while assembling the summary audit. The runner read `len(rows)` before assigning that local variable for a later Chroma query. This bookkeeping defect is fixed and covered offline; source-location telemetry was added without recording exception text or locals. The preserved real-host run remains `FAIL`, later gates remain unmeasured, and overall acceptance remains `PARTIAL`. No model request was repeated during the repair. See `AF011_1_HOST_RUN_DIAGNOSIS_f46f057e6d4e41f7b96e1fb64534fb6e.md`.

## Budget hardening acceptance follow-up (2026-11-01)

The provider reserves 5% of its hard UTF-8 plan-byte budget, with rejection when required content cannot fit. The current sandbox cannot run the authorized host check. Complete the remaining relevance, deduplication, coverage and real-host checks before declaring 1.1 delivery PASS; see `AF011_1_HARDENING_ACCEPTANCE_20261101.md`.

## LLM Planner and iterative Agent execution (2026-11-06)

- Implemented offline: a provider-neutral structured Planner, strict registry/schema validation, bounded capability discovery, DAG scheduling, replanning from actual tool observations, explicit approval and permission handling, interruption-safe checkpoint recovery, result paging, and sequenced execution events. The old AgentLoop is adapted to this engine without exposing arbitrary shell, network, SQL, or repository writes.
- Targeted regression: 60/60 passed. Full offline regression: 615 run, 609 passed, 0 failed/errors, 6 Windows link/junction permission skips; exit 0.
- Coverage is still below the 90% line / 85% branch goals (86.6906% / 77.1817% across all 78 production files). Real Qwen/DeepSeek, PostgreSQL migration and restart, and Chroma integration remain unmeasured. Do not declare the refactor released until host acceptance and the agreed coverage gate are addressed.

## Model-aware context management (2026-11-06)

Offline implementation and regression are complete for per-model capability-driven budgeting, immutable durable history, request-only compression, Planner tool-schema accounting, content-free request/usage diagnostics, and DeepSeek stale-metadata rejection. Full offline results are 635 run / 629 passed / 0 failed / 0 errors / 6 Windows permission skips; coverage over all 79 production modules is 86.6993% lines and 77.0690% branches. This branch remains **PARTIAL**: the current DeepSeek metadata cache is stale and has no provenance, so a user-initiated `/model refresh` is required before a real DeepSeek request can use its window. No live Qwen/DeepSeek/PostgreSQL/Chroma or 30/100-turn model acceptance was performed. See `MODEL_AWARE_CONTEXT_REPORT_20261106.md`.
## Phase 3 core capability completion — current snapshot (2026-11-06)

- **Implemented offline:** both Planner paths can receive bounded approved Memory with current-input precedence; repository/source authority is rechecked; budget overflow removes optional Memory before refusing; retrieval errors remain visible in diagnostics.
- **Implemented offline:** SessionService exposes a read-only context loader separate from summary compaction; the CLI no longer compacts history during initial loading of an evidence-refusal path.
- **Implemented offline:** AgentExecutionEngine lifecycle events reach StreamingManager, including planner failures/rejections, tool calls, observations, replanning, and validated final answer events.
- **Validation:** 657 tests run, 651 passed, 0 failed/errors, 6 Windows link-permission skips. Whole-package coverage is 86.24% statements / 76.78% branches, below 90% / 85% goals.
- **Not complete:** real PostgreSQL/Qwen/Embedding listeners were absent in the execution environment, so live persistence, real model switching, long-context, Chroma recovery, and end-to-end streaming are NOT_MEASURED. DeepSeek live requests were not made. The two application flows (`ask` RAG and `agent` tool loop) remain distinct rather than being collapsed into one execution engine.

See `AGENTFORGE_PHASE3_ACCEPTANCE_REPORT_20261106.md` for dependency graph, call paths, capability matrix, exact results, and remaining gates.

## Phase 3.3 — Real Agent Execution host acceptance

- [x] Add isolated real-Qwen host Runner, synthetic-only tool registry scenarios, request cap, cleanup ownership checks, and report output.
- [x] Repair the persistence helper to accept the scenario objective explicitly; close temporary Chroma clients before directory cleanup.
- [x] Add Runner regression tests and run the full suite: 676 run / 670 passed / 0 failed / 0 errors / 6 Windows permission skips.
- [ ] Rerun the real Qwen + PostgreSQL + temporary Chroma Agent scenarios. The first user-host run reached single-tool session persistence, where the Runner failed with `AttributeError`; the defect is fixed and offline verified, but the real host has not been rerun.
- [ ] Record real Planner decisions, dependency and Observation-driven replanning, approval recovery, Session restoration, Streaming lifecycle, timeout/cancellation, and Chroma results.

See `PHASE3_3_AGENT_EXECUTION_HOST_ACCEPTANCE_REPORT.md` and `PHASE3_3_AGENT_EXECUTION_ARCHITECTURE.md`.

## Phase 4 — Unified conversation and Agent execution (2026-11-07)

- [x] Keep Auto as the normal conversation entry and let the validated LLM conversation plan select whether iterative repository tooling is needed.
- [x] Dispatch selected turns from `ConversationOrchestrator` through `ConversationAgentExecutor` to the existing `AgentExecutionEngine`; retain direct General answering and existing retrieval/evidence validation.
- [x] Compact search observations for replanning without replacing the checked source used by the final evidence path.
- [x] Add integration regressions for direct Auto General, Auto tool dispatch, Observation-driven replan, Answer Contract handoff, and bounded source identity.
- [x] Run targeted and full offline regression.
- [ ] Run host acceptance with synthetic repository/session data and real Qwen Planner; DeepSeek requires current metadata and explicit cloud-data consent.
- [ ] Verify live PostgreSQL conversation/execution checkpoint recovery and host-level Streaming/cancellation.
- [ ] Decide whether a trusted application-level tool registration/approval extension is required before any side-effect tools are added. The current conversational tool set is read-only.

Current status: **PARTIAL** until real host checks are recorded. See `PHASE4_UNIFIED_AGENT_INTEGRATION_REPORT.md` and `PHASE4_UNIFIED_AGENT_INTEGRATION_RESULTS.json`.

## Phase 4.1 — Real-model Agent end-to-end acceptance

- [x] Audit the Phase 4 call graph and existing host runners; confirm the current Auto path uses the existing Engine and remains read-only.
- [x] Run the full offline regression with branch coverage: 692 run, 686 passed, 0 failures/errors, 6 Windows permission skips.
- [x] Measure all `agentforge` production modules: 85.94% statement coverage / 76.55% branch coverage; targets remain unmet.
- [ ] Run Qwen and DeepSeek through the real unified conversation route, including tool decision, Observation-driven replanning, and final Answer Contract.
- [ ] Run live PostgreSQL Session/checkpoint recovery and Qwen Embedding/Chroma validation.
- [ ] Run host streaming and cancellation; keep approval-resume through Auto marked not measured while the Auto tool set remains read-only.

Current result: **BLOCKED** for real host acceptance because this execution environment disables network access and no required local service listeners are present. See `PHASE4_1_ACCEPTANCE_REPORT.md` and its machine-readable results.

## Phase 4.4 — DeepSeek Flash full host stability acceptance (2026-11-08)

- [x] Verify baseline branch/HEAD and preserve the pre-existing dirty worktree.
- [x] Verify the host Runner defaults to the complete 15-scenario matrix; execute the full matrix once without a scenario filter.
- [x] Preserve the original host JSON and Runner report, record SHA-256, derive a metadata-only trace log, and verify temporary resource cleanup status.
- [ ] Pass the full 15-scenario matrix. Current result: **FAIL** — 6 PASS, 1 PARTIAL, 8 FAIL. The Runner's raw results are 7 PASS / 8 FAIL because long-tool pagination passed while the final answer was refused by Answer Contract.
- [ ] Resolve the two confirmed Runner fixture omissions and separately evaluate Planner validation, tool fallback selection, approval routing, and strict citation-contract failures. No code changes were made during Phase 4.4.
- [ ] Measure Provider-native streaming and rerun offline regression/coverage in a later authorized phase; they were NOT_MEASURED here.

DeepSeek Flash request transport succeeded 51/51 with zero retries; captured Usage totals are 51,637 tokens. The result is **not release-ready**. See `PHASE4_4_FULL_ACCEPTANCE_REPORT_20261108.md`, `PHASE4_4_FULL_ACCEPTANCE_RESULTS_20261108.json`, `PHASE4_4_SCENARIO_MATRIX_20261108.md`, and `PHASE4_4_HOST_TRACE_LOG_20261108.txt`.

## Phase 4.4.2.6 — Core business branch coverage reinforcement (2026-11-09)

- [x] Add 32 behavior tests in three bounded batches covering Planner/Execution, Conversation/Evidence/Answer Contract, and Session/Memory/Context/Streaming.
- [x] Run focused tests and related module suites in forward and reverse order; all final targeted runs passed.
- [x] Run full offline regression: 912 / 902 pass / 0 fail / 0 error / 10 skipped. The 4 isolated Chroma dependency skips and 6 Windows link-permission skips remain explicitly unpassed.
- [x] Confirm static dependency scan has no reported cycle or prohibited layer edge (80 modules; current scanner counted 241 internal edges, not directly comparable to historical 239).
- [ ] Re-measure final all-80-module line and branch coverage with the repository's intended `coverage.py` available. Current full percentage is NOT_MEASURED; historical Stage 5 remains 83.97% / 77.20%, below 90% / 85%.
- [ ] Revisit remaining missing arcs from a fresh coverage export, prioritizing reachable high-value behavior and leaving dominated/unreachable arcs unforced.

See `PHASE4_4_2_6_COVERAGE_REMEDIATION_REPORT.md` and `.json` for exact evidence and hashes.

## Phase 4.4.3 — Final offline acceptance and coverage remeasurement (2026-11-09)

- [x] Use the launcher-selected venv and verify the 80-module Stage 5 coverage scope/configuration.
- [x] Run all offline tests and branch coverage in one invocation: 912 / 906 pass / 0 fail / 0 errors / 6 Windows link-permission skips. Four Chroma persistence tests execute and pass.
- [x] Static AST dependency gate passes: no cycles or prohibited orchestration edges.
- [ ] Reach 90% line statements / 85% branch arcs. Measured: 87.2794% / 78.6278%; still short by 373 statements / 339 arcs.
- [ ] Run six skipped link-boundary security tests under a Windows identity with symlink/hardlink/junction permissions.
- [ ] Phase 4.4.4 full host acceptance remains gated; no real model or formal service was contacted in this phase.

See `PHASE4_4_3_FINAL_OFFLINE_ACCEPTANCE_REPORT.md` and `.json`.

## Phase 4.4.3.2 — final bounded coverage remediation (2026-11-09)

- [x] Add and run 23 behavior-focused tests in four bounded batches; merged targeted regression: 384 passed.
- [x] Full offline regression: 935 run / 929 pass / 0 fail / 0 error / 6 Windows link-permission skips.
- [x] Static dependency check passed across 80 modules and 241 AST import edges.
- [ ] Coverage gates remain unmet: 87.7462% line and 79.6241% branch against 90% / 85%; six Windows link tests need a permission-enabled lane.
- [ ] Phase 4.4.4 is not ready under these gates. This bounded pass stops here.

See `PHASE4_4_3_2_FINAL_COVERAGE_REMEDIATION_REPORT.md` and `.json`.


## Phase 4.4.4.2 — concentrated remediation and final acceptance (2026-11-09)

- [x] Align Planner prompt/validator intent and executable-task rules; retain one bounded correction and fail closed.
- [x] Add run-scoped authorized result paging and verify continuity, integrity, observation retention, and final-context flow offline.
- [x] Focused 288/288 and full offline 942 run / 936 pass / 0 fail / 0 error / 6 link-permission skips.
- [x] Measure all 80 production modules and run AST dependency scan; coverage exception remains 87.5433% line / 79.3072% branch, not 90%/85%.
- [ ] DeepSeek Flash full 15-scenario live matrix: **BLOCKED** by the current process network gate and PowerShell AuthorizationManager failure before Runner start. All scenarios remain NOT_MEASURED. Native Provider Streaming remains NOT_MEASURED.
- [ ] Add a user-confirmed approval-forwarding path to the PowerShell launcher before claiming live approval resume; never auto-approve.

See `PHASE4_4_4_2_FINAL_IMPLEMENTATION_REPORT.md` and its results JSON.

## DeepSeek Agent v0.1 final preparation (2026-11-09)

- [x] Freeze the worktree at `feature/1.1-core` / `09bc079ad17b1a2560199eadd679b72c7c779224`; preserve all existing modifications and host evidence.
- [x] Add offline-tested one-shot correction for invalid JSON in Agent Planner responses; strict parsing, schema, permission, dependency, and fail-closed rules remain intact.
- [x] Make new interactive sessions DeepSeek Flash-first only after explicit consent; retain an explicit, checked Qwen option.
- [x] Run scoped core verification forward and reverse (443 passed in each order), Python compile, PowerShell syntax parse, and `git diff --check`.
- [x] Prepare `AgentForge-v0.1` review bundle and release documentation; omit local credentials, model assets, databases, Chroma, and virtual environments.
- [x] Initial Codex preflight was blocked before credentials/requests; later ordinary Windows PowerShell targeted `multi_turn` run passed (12/12 Chat requests, cleanup PASS). Evidence: `AF042_UNIFIED_AGENT_HOST_RESULT_0ddc7ed43968491abc918e28d14dc70f.json`.
- [x] Reconsider release decision after the targeted result: **RELEASE_CANDIDATE** for the declared v0.1 scope; the 15-scenario matrix is not claimed as passed.

Historical full-host results remain separate and are not rolled into a new combined PASS. Full regression and coverage were not repeated; the accepted Phase 4.4.4.4 measurements are preserved as historical baselines. The interactive first-session launch flow has offline tests and remains a recommended manual smoke before broad distribution.
