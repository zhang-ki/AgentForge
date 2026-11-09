# MVP 1.0 Release File Manifest

## Phase 4.3 tool discovery and execution boundary (2026-11-08; development branch only)

- `agentforge/conversation_agent.py`, `conversation_orchestrator.py`, `planner.py`, `agent_execution.py`: request-scoped authorized tool catalog, separate answer intent and execution strategy, bounded unknown-schema correction, and refreshed fallback discovery after `TOOL_UNAVAILABLE`.
- `AF042_UNIFIED_AGENT_HOST_ACCEPTANCE.py`: sanitized approval checkpoint restoration diagnostics and resumed execution trace accounting.
- `tests/test_conversation_agent.py`, `test_planner.py`, `test_agent_execution.py`, `test_phase4_2_unified_host_acceptance.py`: planner catalog, explicit authorization, General tool dispatch, unavailable-tool fallback visibility, schema correction, fail-closed validation, and approval restore regressions.
- `PHASE4_3_ARCHITECTURE_AND_DEPENDENCY_GRAPH_20261108.md`, `PHASE4_3_TOOL_BOUNDARY_REPORT_20261108.md`, `PHASE4_3_TOOL_BOUNDARY_RESULTS_20261108.json`: reviewed call graph, responsibilities, routing rules, offline/host evidence, and remaining limits.
- `PHASE4_3_TARGETED_TEST_LOG_20261108_195637.txt`, `PHASE4_3_FULL_OFFLINE_TEST_LOG_20261108_194822_final.txt`, `PHASE4_3_COVERAGE_TEST_LOG_20261108_195034_final.txt`, and the corresponding coverage report/JSON: final targeted/full test transcripts and package coverage measurement.
- Four selected DeepSeek Flash host results remain separately preserved: `AF042_UNIFIED_AGENT_HOST_RESULT_3bd5457172084621bf3da692d96d44b8.json`, `..._28557e11588b4c21abfbce79c9c617f9.json`, `..._90bf708372a543e18392324c3fce1d84.json`, and `..._bd51d64c0c304096a25681ecd24e5986.json`. Their individual selected-scenario assertions passed; each overall Runner result remains PARTIAL because other scenarios were not selected.

This manifest describes the portable source tree at the repository root.

## Latest AF042 host follow-up evidence (2026-11-08)

- `AF042_HOST_FOLLOWUP_REPORT_20261108.md` and `AF042_HOST_FOLLOWUP_RESULTS_20261108.json`: preserved DeepSeek host-run diagnosis, offline fixes, scenario outcomes, and remaining NOT_MEASURED work.
- `AF042_HOST_FOLLOWUP_TARGETED_TEST_LOG_20261108_posthostfix.txt` and `AF042_HOST_FOLLOWUP_FULL_TEST_LOG_20261108_posthostfix.txt`: post-fix 109-test targeted and 808-test full offline logs.
- Original host evidence remains in `AF042_UNIFIED_AGENT_HOST_RESULT_19899740e51c404e9e933648fd143458.json` and its matching Markdown report; the original JSON hash is recorded in the follow-up results.
- Targeted DeepSeek follow-up `AF042_UNIFIED_AGENT_HOST_RESULT_86949181e64948af8886bd21af0f023c.json`: `multi_tool_dependency` passed; all other scenarios are NOT_MEASURED. The Chroma check only reopens an empty collection and makes no embedding request.

## Latest local stabilization evidence (2026-11-08)

- `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_REPORT_20261108.md` and `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_RESULTS_20261108.json`: root-cause evidence, code changes, offline/host status, and machine-readable PARTIAL result for schema discovery and multi-model output budgets.
- `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_COVERAGE_REPORT_20261108.md`, `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_COVERAGE_20261108_afterrunner.json`, and `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_COVERAGE_REPORT_20261108_afterrunner.txt`: all-production-package line/branch coverage summary and raw exports.
- `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_TARGETED_TEST_LOG_20261108_post_runner.txt` and `AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_FULL_TEST_LOG_20261108_afterrunner.txt`: targeted and complete offline regression logs.
- `AF042_UNIFIED_AGENT_HOST_ACCEPTANCE.py` and `.ps1`: unified real-host runner with shared output-budget resolution, a synthetic DeepSeek-only long-answer scenario, and the product's bounded Agent step count. The preserved live run predates the latest fixes; re-verification remains pending.

## Application

- `agentforge/`: CLI, repository scanner, safe reader, chunker, SQLite indexer/store, Python AST and static relationship parsers, retrieval/routing, context and citation contracts, bounded Agent loop, local chat/embedding providers, PostgreSQL conversation and memory modules, and Chroma adapters.
- `alembic.ini` and `alembic/`: PostgreSQL schema migration configuration and revisions.
- `pyproject.toml`: package metadata and optional persistence dependencies.

## Windows entry points

- `Start-AgentForge-All.cmd`: checks local PostgreSQL and local model readiness, then starts the conversation console.
- `Start-AgentForge.cmd`: starts the conversation console without managing model services.
- `Start-Qwen.cmd` and `Start-Qwen-Embedding.cmd`: start the configured local llama-server model processes.
- `launchers/`: shared Windows credential handling and startup scripts. Model assets are external and are not part of this repository.

## Tests and learning material

- `tests/`: unit, integration, security-boundary, persistence, retrieval, and acceptance-contract tests.
- `tests/fixtures/`: synthetic source repositories and frozen synthetic evaluation data; no user repository is intended to be stored here.
- `learning/`: 28 standalone daily lessons with embedded exact source excerpts and step-by-step explanations, plus optional operation, progress, interview, and conversation/memory reference guides.
- `README.md`, `ARCHITECTURE.md`, `SECURITY.md`, `ROADMAP.md`: user, architecture, security, and release-scope documentation.
- `RELEASE_NOTES.md`, `GITHUB_RELEASE_CHECKLIST.md`, and `GITHUB_RELEASE_RESULTS.json`: this release's sanitized notes and publication record.

## Local-only data excluded from release

`.agentforge/`, PostgreSQL credentials/backups, SQLite and Chroma user data, model weights and runtimes, Python caches, coverage output, raw host-runner logs/results, and historical diagnostic reports are local artifacts. Historical originals are retained locally where present; this manifest does not assert that old commits have been rewritten.

## AF-012 Multi-Model development additions (not released)

- `agentforge/conversation_orchestrator.py`, `dialogue_router.py`, `dialogue_retrieval.py`, and `evidence_evaluator.py`: current worktree turn orchestration, General/Repository/Hybrid/lexical Auto plans, follow-up retrieval helpers, and structural (not semantic) evidence sufficiency assessment.
- `agentforge/model_types.py`, `repository_identity.py`, `providers/dialogue_protocol.py`, and `providers/local_health.py`: shared model/identity types, provider-neutral dialogue framing, and local model health checks.
- `agentforge/answer_contract.py`, `cli.py`, `conversation_console.py`, and Qwen/DeepSeek providers: current mode-aware path and application-owned citation identity validation. Provider output remains untrusted text; semantic entailment is not proven.
- `tests/test_dialogue_router.py` and related changes in `tests/test_cli.py`, `tests/test_providers.py`, `tests/test_multimodel.py`, `tests/test_af011_chat_acceptance.py`, `tests/test_af012_b2_context.py`, `tests/test_conversation_console.py`, and `tests/test_presentation.py`: current worktree mode, provider, contract, context, and entry-point regression coverage.
- `DIALOGUE_SYSTEM_ARCHITECTURE_AUDIT_20261106.md`, `DIALOGUE_SYSTEM_ARCHITECTURE_DESIGN_20261106.md`, `DIALOGUE_SYSTEM_DEPENDENCY_GRAPH_20261106.md`, `DIALOGUE_SYSTEM_COVERAGE_REPORT_20261106.md`, `DIALOGUE_SYSTEM_FINAL_REPORT_20261106.md`, and `DIALOGUE_SYSTEM_ACCEPTANCE_RESULTS_20261106.json`: audit baseline, current worktree design/dependency view, measured offline results, and machine-readable PARTIAL decision. These are local development artifacts, not a release claim.
- `DIALOGUE_SYSTEM_FULL_TEST_FINAL_563117cfb1a74bce911b9ac3c3d50645.log`, `DIALOGUE_SYSTEM_FULL_TEST_16cf306558124a2cb88c2072f02a3f1f.log`, `DIALOGUE_SYSTEM_CORE_REVERSE_1035a027513c4a03973a930e31f13e9e.log`, and `.agentforge/coverage/dialogue-final-563117cfb1a74bce911b9ac3c3d50645.json`: final full/reverse test evidence and machine coverage data, plus the separate initial failure transcript.
- `DIALOGUE_SYSTEM_COVERAGE_16cf306558124a2cb88c2072f02a3f1f.json`, `DIALOGUE_SYSTEM_COVERAGE_99e68788cfd949a8ba78fa48e1bc2e0b.json`, and `DIALOGUE_SYSTEM_COVERAGE_REPORT_99e68788cfd949a8ba78fa48e1bc2e0b.txt`: prior diagnostic coverage exports retained separately; the final decision uses the later final coverage JSON identified above.

- `agentforge/model_registry.py`: Qwen/Offline descriptors, DeepSeek `/models` metadata parsing/cache, Qwen health and model identity verification.
- `agentforge/providers/deepseek.py`: explicit fixed-host HTTPS Chat Completions transport, bounded errors/timeouts, SSE, reasoning separation, usage and request statistics. Request-local citation validation is owned by the application Answer Contract.
- `agentforge/providers/deepseek_agent.py`: optional DeepSeek planner constrained to the existing read-only search and symbol tools.
- `agentforge/credential_store.py`: current-Windows-user DPAPI credential storage and `DEEPSEEK_API_KEY` precedence; no credential contents are included in result files.
- `agentforge/token_budget.py`: per-model context/output budget calculation and exact-or-estimated request measurement including tool protocol fields.
- `alembic/versions/af012mm_004_model_binding.py`: additive provider/model/Thinking/effort/cloud-consent columns with legacy Qwen defaults.
- `agentforge/conversation_console.py`, `agentforge/cli.py`, `agentforge/conversation_store.py`, and `agentforge/af012_schema.py`: explicit model commands, cloud consent gates, per-session persistence, and usage metadata.
- `AF012_MULTIMODEL_HOST_ACCEPTANCE.ps1` and `.py`: one-command, bounded ordinary-Windows smoke entry point. It never starts services, asks before synthetic DeepSeek requests, makes no retries, and explicitly reports the broader host suite as not measured.
- `tests/test_multimodel.py`, `tests/test_multimodel_host_runner.py`, and model command cases in `tests/test_conversation_console.py`: offline protocol, cache, credentials, session, token budget, security gate, and CLI interaction tests.
- `MODEL_MANAGEMENT.md`, `learning/MULTI_MODEL_PROVIDERS.md`, `learning/TOKEN_BUDGET.md`: current model setup, user operations, provider boundaries, and budget teaching material.
- `AF012_MULTIMODEL_ACCEPTANCE_REPORT_20261104.md` and `AF012_MULTIMODEL_ACCEPTANCE_RESULTS_20261104.json`: this stage's test, coverage, environment and remaining real-host acceptance evidence. Generated host results and raw coverage logs remain local artifacts.
- `AF012_THINKING_BUDGET_FIX_REPORT_20261104.md`: audit of the preserved V4 Pro Thinking truncation, bounded budget fix, offline regression, and real-host revalidation blocker.

## `feature/1.1-core` development additions (not yet part of the 1.0 release)

- `agentforge/repositories.py`: canonical repository identities, local registration/active selection, and isolated SQLite index routing.
- `tests/test_repositories.py`, `tests/test_conversation_console.py`: synthetic repository isolation, vector filtering, repository commands, and removal safety.
- 1.1 edits in `agentforge/cli.py`, `conversation_console.py`, `indexer.py`, `store.py`, `product_retrieval.py`, `package_relations.py`, `conversation_store.py`, `conversation_context.py`, `providers/llama_server.py`, `memory_store.py`, `memory_retrieval.py`, and `embeddings/chroma_store.py`: repository lifecycle, compact rolling summaries, bounded long-context preparation, cross-file evidence, and scoped memory/vector behavior.
- `AF011_1_*` reports/results: offline acceptance evidence and explicit live-host limitations for this branch.
- `AF011_1_JOINT_HOST.py` and `.ps1`: bounded, partial host check using a uniquely named disposable PostgreSQL database and temporary Chroma directory; does not manage model services.
- `tests/test_af011_1_joint.py`: two-repository and global/private Memory isolation using real local Chroma, synthetic embeddings and isolated SQLite.
- `AF011_1_FULL_HOST_ACCEPTANCE.py` and `.ps1`: bounded real-service acceptance entry point using isolated resources. It records a bounded, redacted CLI diagnostic on failure. The latest recorded host attempt completed 19 answer turns and then stopped before answer request 20 because the summary exceeded the context budget; the offline compact-summary fix awaits real-host verification. Earlier attempts reached 30 real Qwen turns and RAG cases. Desktop restart and PostgreSQL service restart for 1.1 remain unverified.
- `tests/test_af012_b2_conversations.py` and `tests/test_providers.py`: regression coverage for rolling semantic-summary replacement, legacy summary compaction, preservation on compaction failure, and the raised but bounded context budget.
- `tests/test_af011_1_full_host.py`: synthetic fixture, citation identity, integrity snapshot and environment-gate checks for the entry point.
- `AF011_1_HOST_FIX_REPORT.md`: separate audit of the first real 1.1 host attempt, subsequent fixes and remaining unmeasured gates; the original host result is preserved locally.
- `AF011_1_SECOND_HOST_DIAGNOSTIC_REPORT.md`: audit of the second host attempt and the response-socket/cleanup diagnostics fix; raw host results remain unchanged and local.
- `AF011_1_THIRD_HOST_DIAGNOSTIC_REPORT.md`: audit of the third host attempt and bounded context/summary safeguards; live post-fix validation remains outstanding.
- `AF011_1_HOST_CONTINUATION_REPORT_20261101.md` and `AF011_1_HOST_CONTINUATION_RESULTS_20261101.json`: historical snapshot of the earlier 611243... host failure and incremental-summary repair; superseded for current status by the summary-batch repair report below.
- `AF011_1_SUMMARY_BATCH_REPAIR_REPORT_20261101.md` and `AF011_1_SUMMARY_BATCH_REPAIR_RESULTS_20261101.json`: audit of the later host `semantic_summary` failure, offline batched-summary repair, regression results, current coverage measurement, and required real-host revalidation; original host JSON is preserved.
- `AF011_1_CONTINUATION_COVERAGE_20261101.json` and `AF011_1_CONTINUATION_COVERAGE_TEST_LOG_20261101.txt`: current branch-coverage JSON and raw coverage-instrumented full-suite log for the summary-repair verification.
- `AF011_1_SUMMARY_RETENTION_REPAIR_REPORT_20261101.md` and `AF011_1_SUMMARY_RETENTION_REPAIR_RESULTS_20261101.json`: audit of the latest real Qwen summary dropping earlier facts, append-only bounded checkpoint repair, offline regression and remaining host gate.
- `AF011_1_RETENTION_COVERAGE_20261101.json` and `AF011_1_RETENTION_COVERAGE_TEST_LOG_20261101.txt`: latest branch-coverage JSON and raw coverage-instrumented full-suite log for the summary-retention fix.
- `AF011_1_COVERAGE_FINAL_20261101.json`: raw branch-coverage JSON from the full offline suite; the 90% line and 85% branch gates remain unmet.

- `AF011_1_RAG_FIX_REPORT_20261101.md` and `AF011_1_RAG_FIX_RESULTS_20261101.json`: audit of the frozen 9/11 host RAG failure, offline callsite/configuration evidence repair, runner stage-accounting fix, test results, coverage and remaining real-host gate.
- `AF011_1_RAG_FIX_FULL_TEST_20261101.log` and `AF011_1_RAG_FIX_COVERAGE_TEST_20261101.log`: exact system-Python and project-venv test transcripts for this repair; the project-venv run is coverage-instrumented.
- `AF011_1_RAG_FIX_COVERAGE_RESULTS_20261101.json`: unabridged coverage.py 7.17.0 branch report for the full `agentforge/` production package; coverage gates remain unmet.

- `AF011_1_FULL_HOST_RESULT_4c4027afb7f44070a84258c4af916693.json`: latest preserved real-host failure at semantic summary fact preservation; no fixed RAG case was reached.


## AF012-CONTEXT-RELIABILITY additions (2026-11-01)

- `agentforge/conversation_facts.py`: conservative source-linked user-statement extraction, stable IDs, ledger validation, and bounded fact rendering.
- `alembic/versions/af012ctx_003_summary_fact_ledger.py`, `agentforge/af012_schema.py`, `agentforge/conversation_store.py`: additive summary ledger, rolling summary guards, source identity validation, output budget, transactional cursor protection, and per-update diagnostics.
- `agentforge/cli.py`, `AF011_1_JOINT_HOST.py`: bounded summary request metrics and safe failure classification.
- `tests/test_af012_b2_conversations.py`, `tests/test_af012_b2_context.py`, `tests/test_af011_1_joint.py`: repeated fact retention, source/provenance integrity, failure/cursor rollback, budgets, and diagnostic mapping regression tests.
- `AF012_CONTEXT_RELIABILITY_REPORT_20261101.md`, `AF012_CONTEXT_RELIABILITY_RESULTS_20261101.json`, `AF012_CONTEXT_RELIABILITY_INTEGRITY_20261101.json`: this stage's root-cause analysis, results, and protected-file comparison.
- `AF012_CONTEXT_RELIABILITY_COVERAGE_FINAL2_20261101.json`, `AF012_CONTEXT_RELIABILITY_COVERAGE_REPORT_FINAL2_20261101.log`, `AF012_CONTEXT_RELIABILITY_COVERAGE_TEST_FINAL2_20261101.log`, `AF012_CONTEXT_RELIABILITY_FULL_TEST_FINAL_20261101.log`, `AF012_CONTEXT_RELIABILITY_TARGETED_FINAL_20261101.log`, `AF012_CONTEXT_RELIABILITY_CORE_FORWARD_FINAL_20261101.log`, `AF012_CONTEXT_RELIABILITY_CORE_REVERSE_FINAL_20261101.log`: machine coverage, raw coverage/full test transcripts, and order-sensitivity logs. Coverage data `.coverage.af012ctxrelfinalv2_20261101` is local run evidence, not a product dependency.
- The preserved host result remains a real failure; this stage did not run a model or connect to formal PostgreSQL. Coverage thresholds remain unmet; see the report before treating the branch as accepted.


- `AF011_1_FULL_HOST_RESULT_b60a48c0043c47629292bdeec03a4c85.json`: user-run host preflight blocked because formal PostgreSQL is at `af012mvp_002` while the required Alembic head is `af012ctx_003`; no model requests or temporary resources were created. SHA-256: `f4cd9ea403101371552059747974588819e07349ede9561ad2f8125808779e38`.

## AF012-CONTEXT-RELIABILITY follow-up (2026-11-01)

- `AF011_1_FULL_HOST_RESULT_39a35497f3c44a3091b64afeaecec276.json`: preserved user-run result after the formal DB reached `af012ctx_003`; 23 answer turns completed, then a semantic-summary response for messages 33–40 ended with `finish_reason=length` at the 256-token cap. No fixed RAG case was reached. SHA-256: `dad1f5af564bf9d6d5cd61b280473c2b95236825336379dd88877ff905b60889`.
- `agentforge/providers/llama_server.py`, `agentforge/cli.py`: validated per-request output-token limit; summary calls request 512 tokens, while normal answers retain their configured limit. Truncation remains a hard failure without summary/cursor update.
- `AF011_1_FULL_HOST_ACCEPTANCE.py`: records the actual per-request output-token override in host telemetry.
- `tests/test_providers.py`, `tests/test_af012_b2_context.py`: cover bounded per-request override, unchanged provider default, 512-token summary request diagnostics and continued rejection of truncated summaries.
- `AF012_CONTEXT_RELIABILITY_COVERAGE_OUTPUT512_20261101.json`: full branch-coverage JSON for 424 tests and all 59 `agentforge` production files; thresholds remain unmet.
- `AF012_CONTEXT_RELIABILITY_REPORT_20261101.md`, `AF012_CONTEXT_RELIABILITY_RESULTS_20261101.json`, `STATUS.md`, `ROADMAP.md`: append the latest real-host evidence, offline fix/test results and remaining real-Qwen gate without rewriting earlier attempts.
- Raw coverage-run test log and coverage data are in `.agentforge/coverage/af012ctx003-output512-20261101-*`; these are local test artifacts, not runtime dependencies.

## AF011.1 RAG citation and AF012 context fact-trace repair (2026-11-01)

- `agentforge/evidence_roles.py`: application-defined evidence role requirements for cross-file call and configuration questions.
- `agentforge/product_retrieval.py`, `agentforge/answer_contract.py`, `agentforge/providers/llama_server.py`, `agentforge/cli.py`: preserve complementary source ranges, label evidence roles, require cited-role coverage, and preserve limited/static uncertainty.
- `agentforge/conversation_facts.py`, `agentforge/conversation_context.py`, `agentforge/conversation_store.py`, `AF011_1_FULL_HOST_ACCEPTANCE.py`, `AF011_1_JOINT_HOST.py`: source-linked conversation fact ledger, recent-fact context injection, transactional summary guards, and ID-only request audit.
- Related regression tests: `tests/test_af011_product.py`, `tests/test_conversation_context.py`, `tests/test_af012_b2_conversations.py`, `tests/test_providers.py`, `tests/test_cli.py`, `tests/test_af011_1_full_host.py`, and `tests/test_af011_1_joint.py`.
- `AF011_1_FINAL_DEFECT_REPAIR_20261101.md` and `AF011_1_FINAL_DEFECT_REPAIR_RESULTS_20261101.json`: root-cause evidence, offline test results, integrity checks, outstanding host gate and final status.
- `AF011_1_CITATION_CONTEXT_COVERAGE_20261101.json`, `coverage/AF011_1_CITATION_CONTEXT_20261101.coverage`, `AF011_1_CITATION_CONTEXT_COVERAGE_TESTS_20261101.log`, `AF011_1_CITATION_CONTEXT_FULL_TESTS_20261101_FINAL.log`, `AF011_1_CITATION_CONTEXT_CORE_FORWARD_FINAL_20261101.log`, and `AF011_1_CITATION_CONTEXT_CORE_REVERSE_FINAL_20261101.log`: complete-package branch coverage and reproducible test transcripts. Coverage remains below the 90% line / 85% branch target.
- `AF011_1_CITATION_CONTEXT_FULL_TESTS_20261101.log`: preserved first full-run error transcript; its failure was a test-double compatibility issue fixed before the final passing run.

## AF011.1 host runner bookkeeping follow-up (2026-11-01)

- `AF011_1_HOST_RUN_DIAGNOSIS_f46f057e6d4e41f7b96e1fb64534fb6e.md` and `.json`: audit of the later 30-turn host run, confirmed runner `rows` read-before-assignment defect, offline repair evidence, and remaining live-host gate. Original result SHA-256 is recorded in the JSON; raw result is retained unchanged.
- `AF011_1_FULL_HOST_ACCEPTANCE.py`, `AF011_1_JOINT_HOST.py`, and `tests/test_af011_1_full_host.py`: summary audit now uses persisted messages; generic failure records contain only source location; offline regressions cover the behavior. Full real-host revalidation remains required.

- `AF011_1_CONTEXT_BUDGET_REPORT_20261101.md` and `AF011_1_BUDGET_COVERAGE_20261101.json`: synthetic budget reproduction, limitations, offline test counts and full-package coverage for the context-budget follow-up. The real host failure remains preserved and unverified after this change.

## AF011.1 budget hardening (2026-11-01)

- `AF011_1_HARDENING_ACCEPTANCE_20261101.md` and `AF011_1_HARDENING_RESULTS_20261101.json`: current offline evidence, restricted-host blocker and remaining limitations.
- `AF011_1_HARDENING_COVERAGE_20261101.json`, `AF011_1_HARDENING_COVERAGE_REPORT_20261101.txt`, `AF011_1_HARDENING_FINAL_TEST_20261101.log`, `AF011_1_HARDENING_INTEGRITY_20261101.json`: coverage, test and integrity evidence. The earlier failed full test log is retained separately.

- `AF011_1_HARDENING_FINAL2_TEST_20261101.log`, `AF011_1_HARDENING_COVERAGE_FINAL2_20261101.json`, and `AF011_1_HARDENING_COVERAGE_FINAL2_20261101.txt`: final offline run including the synthetic 50-turn preference test.

## AF-012 Phase 2 LLM Planner worktree additions (2026-11-06; not released)

- New modules: agentforge/planner.py, dialogue_router.py, conversation_orchestrator.py, dialogue_retrieval.py, evidence_evaluator.py, session_service.py, memory_service.py, streaming.py, model_types.py, repository_identity.py, providers/dialogue_protocol.py, and providers/local_health.py.
- New regression tests: tests/test_planner.py, test_dialogue_router.py, test_session_service.py, test_memory_service.py, and test_streaming.py, plus changes to existing provider, CLI, conversation, and presentation tests.
- Updated implementation also touches Qwen/DeepSeek adapters, token budget, context, Answer Contract, PostgreSQL migration and composition-root code. It remains uncommitted development work.
- Current reports: DIALOGUE_SYSTEM_ARCHITECTURE_AUDIT_20261106.md, DIALOGUE_SYSTEM_ARCHITECTURE_DESIGN_20261106.md, DIALOGUE_SYSTEM_DEPENDENCY_GRAPH_20261106.md, DIALOGUE_SYSTEM_FINAL_REPORT_20261106.md, DIALOGUE_SYSTEM_ACCEPTANCE_RESULTS_20261106.json, and DIALOGUE_SYSTEM_COVERAGE_REPORT_20261106.md.
- Final raw evidence: DIALOGUE_SYSTEM_COVERAGE_TESTS_a837b2ba2f204d8f9ca334442c348bd4.log, DIALOGUE_SYSTEM_COVERAGE_a837b2ba2f204d8f9ca334442c348bd4.coverage, DIALOGUE_SYSTEM_COVERAGE_a837b2ba2f204d8f9ca334442c348bd4.json, and DIALOGUE_SYSTEM_COVERAGE_REPORT_a837b2ba2f204d8f9ca334442c348bd4.txt.
- Offline suite: 582 run, 576 passed, six Windows link-permission skips; 87.2883% line and 78.8844% branch coverage. Real host acceptance is NOT_MEASURED; overall PARTIAL.

## Agent execution loop refactor (2026-11-06; uncommitted development work)

- `agentforge/agent_execution.py`: shared tool protocol, immutable registry/discovery, LLM planner, bounded DAG engine, authorization, persistence adapters, recovery, result paging, and event integration.
- `agentforge/agent_loop.py`, `agentforge/cli.py`, `agentforge/streaming.py`: compatibility composition, provider/resume CLI, and sequenced Agent events.
- `agentforge/af012_schema.py`, `alembic/versions/af013ae_001_agent_execution_state.py`: durable execution/result schema and additive migration (not applied to live PostgreSQL).
- `tests/test_agent_execution.py`, `tests/test_agent_loop.py`, `tests/test_af012_schema.py`: shared engine/adapter/schema regression coverage.
- `AGENT_EXECUTION_REFACTOR_REPORT_20261106.md`, `AGENT_EXECUTION_REFACTOR_RESULTS_20261106.json`: implementation boundaries, exact offline test/coverage results, unmeasured host checks and remaining limits.
- `AGENT_EXECUTION_FULL_TESTS_2033c0f1324f44ce86ce7a77edc43711.log`, `.coverage.agent-execution-final-2033c0f1324f44ce86ce7a77edc43711`, `AGENT_EXECUTION_COVERAGE_2033c0f1324f44ce86ce7a77edc43711.json`, `AGENT_EXECUTION_COVERAGE_2033c0f1324f44ce86ce7a77edc43711.txt`: final raw full test transcript and all-package branch coverage evidence. Earlier run artifacts were retained.
- Offline full suite: 615 run, 609 passed, 0 failed/errors, 6 Windows link/junction permission skips; 86.6906% statements and 77.1817% branches. Live Qwen/DeepSeek/PostgreSQL/Chroma acceptance was not performed.

## Model-aware context management (2026-11-06)

- `agentforge/model_types.py`, `model_registry.py`, `token_budget.py`, `context_assembler.py`: per-model capability metadata, provenance freshness gates, dynamic request input/output budgets, optional model tokenizer adapters, and request-only context assembly.
- `agentforge/cli.py`, `conversation_console.py`, `conversation_orchestrator.py`, `conversation_store.py`, `session_service.py`, `agent_execution.py`, and provider adapters: selected-model budget use, stale DeepSeek metadata rejection, Planner/final-answer budget diagnostics, and bounded content-free Usage persistence. Durable message history remains uncompressed.
- `tests/test_context_assembler.py`, `tests/test_multimodel.py`, `tests/test_cli.py`, and updated context/provider/session/execution tests: 30/100-turn request views, model-switch history integrity, unknown/stale capability fail-closed behavior, tokenizer-estimate labeling, output/reasoning usage separation, and persistence checks.
- `MODEL_AWARE_CONTEXT_REPORT_20261106.md` and `MODEL_AWARE_CONTEXT_RESULTS_20261106.json`: model capability matrix, request assembly rules, offline-vs-live evidence, limitations and acceptance state.
- `MODEL_AWARE_CONTEXT_TARGETED_20261106_FINAL.log`, `MODEL_AWARE_CONTEXT_FULL_TESTS_20261106_FINAL2.log`, `.coverage.model-aware-context-20261106-final2`, `MODEL_AWARE_CONTEXT_COVERAGE_20261106_FINAL2.json`, and `MODEL_AWARE_CONTEXT_COVERAGE_20261106_FINAL2.txt`: final focused/full test and whole-package branch-coverage evidence. Full run: 635 run, 629 passed, 0 failed/errors, 6 Windows permission skips; 86.6993% statements and 77.0690% branches. Live provider/database acceptance remains NOT_MEASURED.

## Phase 2 final integration snapshot (2026-11-06; development branch only)

- `AGENTFORGE_PHASE2_FINAL_INTEGRATION_REPORT_20261106.md` and `AGENTFORGE_PHASE2_FINAL_INTEGRATION_RESULTS_20261106.json`: current call chains, responsibility boundaries, acceptance matrix, environment blockers, and final PARTIAL decision.
- `AGENTFORGE_PHASE2_COVERAGE_REPORT_20261106.md`, `PHASE2_COVERAGE_20261106_5f079c6107554d85986117d10d56534c.json`, and `.txt`: all-package measurement using Python 3.12.7 / Coverage.py 7.17.0; 79 production modules, 86.5906% statements and 77.0132% branches.
- `PHASE2_FINAL_TESTS_5f079c6107554d85986117d10d56534c.log`: final branch-enabled full regression transcript, 643 tests run, 637 passed, and 6 permission-related skips.
- The current snapshot is **PARTIAL**: host Qwen/DeepSeek/PostgreSQL/Chroma acceptance is blocked or unmeasured, coverage goals remain unmet, and Agent planner Memory injection plus semantic entailment verification remain incomplete. No commit or push was performed.
## Phase 3 core capability completion (2026-11-06; development branch only)

- `agentforge/memory_service.py`, `agent_execution.py`, `planner.py`, `conversation_orchestrator.py`, `session_service.py`, `streaming.py`, `cli.py`: bounded Planner Memory injection, scoped authority validation, safe memory degradation diagnostics, read-only context restoration, and sequenced Agent stream lifecycle handling.
- `tests/test_memory_service.py`, `test_agent_execution.py`, `test_planner.py`, `test_session_service.py`, `test_streaming.py`, `test_cli.py`: Memory scope/authority, input precedence, budget fallback, orchestrator-to-Planner wiring, refusal without compaction, and final event lifecycle regressions.
- `AGENTFORGE_PHASE3_ACCEPTANCE_REPORT_20261106.md`, `AGENTFORGE_PHASE3_ACCEPTANCE_RESULTS_20261106.json`, `AGENTFORGE_PHASE3_COVERAGE_REPORT_20261106.md`: Phase 3 architecture, implementation, test/coverage evidence, and host status.
- `AGENTFORGE_PHASE3_TEST_LOG_20261106.txt`: first full run, retaining the compaction regression failure found before the fix. `AGENTFORGE_PHASE3_TEST_LOG_20261106_RERUN.txt`: final full run, 657 run / 651 passed / 0 failed / 0 errors / 6 permission skips.
- `AGENTFORGE_PHASE3_COVERAGE_20261106_RERUN.json`, `.coverage_phase3_20261106_rerun`: full `agentforge` branch-enabled coverage data; 86.24% statements / 76.78% branches. Real-host status is BLOCKED by absent local listeners; no services or real data were touched.

## Phase 3.3 real Agent Execution host acceptance (2026-11-06; uncommitted)

- `AF033_AGENT_LOOP_HOST_ACCEPTANCE.py` / `.ps1`: standalone host entry; real Qwen Planner, existing Engine/Registry, synthetic-only tools, isolated disposable PostgreSQL and temporary Chroma, request cap and cleanup ownership checks. Latest user-host run exposed a Runner persistence defect; it is repaired and a post-fix real-host rerun is pending. Exact `.gitignore` exceptions keep these two deliverable files from being hidden by the broad runner-artifact ignore rules.
- `tests/test_af033_agent_host.py`: 15 offline checks for preflight, scope, synthetic permissions, real Planner boundary, bounded requests, objective persistence and Chroma handle cleanup.
- `PHASE3_3_AGENT_EXECUTION_ARCHITECTURE.md`, `PHASE3_3_AGENT_EXECUTION_HOST_ACCEPTANCE_REPORT.md`, `PHASE3_3_AGENT_EXECUTION_HOST_ACCEPTANCE_RESULTS.json`: architecture, test/host result and remaining acceptance matrix.
- `PHASE3_3_TARGETED_TESTS_20261106.log`, `PHASE3_3_TARGETED_TESTS_20261106_REPAIR.log`, `PHASE3_3_CHROMA_HANDLE_CLEANUP_20261106.log`, `PHASE3_3_TEST_AGENT_EXECUTION_20261106.log`, `PHASE3_3_TEST_STREAMING_20261106.log`, `PHASE3_3_TEST_SESSION_SERVICE_20261106.log`, `PHASE3_3_FULL_OFFLINE_TESTS_20261106_FINAL.log`, `PHASE3_3_FULL_OFFLINE_TESTS_20261106_FINAL2.log`, `PHASE3_3_FULL_OFFLINE_TESTS_20261106_FINAL3.log`: targeted and successive full test transcripts; latest suite 676 run / 670 passed / 0 failed / 0 errors / 6 Windows link-permission skips.
- Preserved user-host failure `AF033_AGENT_LOOP_HOST_RESULT_e5589e42667642e6ad6a57a27174d558.json`: PostgreSQL migration and Chroma create/reopen passed; Qwen requests 3/3 succeeded; first scenario failed because the Runner referenced nonexistent `report.objective`. The Runner now accepts the scenario objective explicitly and closes Chroma clients before temporary-directory cleanup. The run-owned temporary resources were cleaned afterward; other `tests/.tmp` entries were preserved.

## Phase 4 unified conversation and Agent execution (2026-11-07; uncommitted)

- `agentforge/planner.py`, `dialogue_router.py`, `conversation_orchestrator.py`, `conversation_agent.py`: validated Auto plans can request bounded Agent tool execution; direct General remains on the regular answer path; completed tool runs rejoin normal retrieval and evidence validation.
- `agentforge/agent_execution.py`: tools-only planner mode returns a completion decision without drafting an answer; tool search observations are reduced to source IDs and repository path/line references before replanning so full excerpts do not crowd the selected model context. Execution records remain the Engine's responsibility.
- `agentforge/agent_loop.py`, `agentforge/streaming.py`, `agentforge/cli.py`: conversational execution reuses the existing Engine facade and lifecycle events; CLI composes the service but does not own planning/execution logic.
- `tests/test_conversation_agent.py`, `tests/test_agent_execution.py`, `tests/test_planner.py`, `tests/test_dialogue_router.py`, `tests/test_cli.py`: Auto direct/general and tool-dispatch integration, replanning observation bounds, plan schema, and CLI behavior regressions.
- `PHASE4_UNIFIED_AGENT_INTEGRATION_REPORT.md` and `.json`: current audit, call graph, PASS/PARTIAL/NOT_MEASURED matrix, and full regression evidence.
- `PHASE4_FINAL_TARGETED_TEST_LOG_20261107_d8c1a555f83743eb9c14c7ba2b0183df.txt`: final 196/196 targeted tests passed. `PHASE4_TARGETED_TEST_LOG_20261107_532f033a4932401fa497d26982da3d86.txt` is the retained first run; its two failures were outdated test plan fixtures missing the new required flag and passed after fixture correction.
- `PHASE4_FINAL_FULL_TEST_LOG_20261107_d153527112004068aaf277653295a1a0.txt`: final full offline run, 692 total, 686 passed, 0 failed/errors, 6 Windows link-permission skips. Real Qwen/DeepSeek, host PostgreSQL, host approval recovery, and full desktop lifecycle remain NOT_MEASURED.

## Phase 4.1 real-model Agent acceptance (2026-11-07)

- `PHASE4_1_ACCEPTANCE_REPORT.md` / `PHASE4_1_ACCEPTANCE_RESULTS.json`: Phase 4 architecture and host-runner audit, offline-vs-real test matrix, exact environment blocker, and preserved Git/data status. Real model and PostgreSQL acceptance is BLOCKED; no service or credential value was accessed.
- `PHASE4_1_COVERAGE_REPORT.md` / `PHASE4_1_COVERAGE_20261107.json` / `.coverage.phase4_1_8238c6dfca654bf09153a701630b4292`: coverage.py results for all 80 `agentforge` modules, with 85.94% executable-statement and 76.55% branch coverage.
- `PHASE4_1_FULL_TEST_LOG_20261107_8238c6dfca654bf09153a701630b4292.txt`: full suite run under coverage, 692 run / 686 passed / 0 failed / 0 errors / 6 Windows symlink, hard-link and junction permission skips. SHA-256 is recorded in the result JSON and coverage report.

## Phase 4.2 Unified Agent host acceptance (2026-11-07)

- `AF042_UNIFIED_AGENT_HOST_ACCEPTANCE.py` and `.ps1`: unified synthetic-only host Runner through `ConversationOrchestrator`, with real Qwen Planner use when host readiness permits, disposable PostgreSQL/Chroma setup, bounded requests, sanitized traces, and owned-resource cleanup.
- `tests/test_phase4_2_unified_host_acceptance.py`: offline Runner gate, trace redaction, request budget/usage, process-lifecycle, timeout, cancellation and durable-result-reference checks.
- `PHASE4_2_ARCHITECTURE_AND_MATRIX.md`, `PHASE4_2_ACCEPTANCE_REPORT.md`, `PHASE4_2_ACCEPTANCE_RESULTS.json`: call graph, scope/acceptance matrix, measured results, and remaining host blockers.
- `PHASE4_2_FULL_TEST_LOG_20261107_d45eb236fe2b4700b37bdb8653958cc6.txt`: final complete suite, 706 run / 700 pass / 0 failed / 0 errors / 6 Windows link-permission skips.
- `PHASE4_2_COVERAGE_20261107_d45eb236fe2b4700b37bdb8653958cc6.json` and `PHASE4_2_COVERAGE_REPORT_20261107_d45eb236fe2b4700b37bdb8653958cc6.txt`: all 80 production modules, 85.9595% line/statement, 76.5784% branch, and 83.3522% combined coverage.
- `AF042_UNIFIED_AGENT_HOST_RESULT_d5396fc2bf2e4e309f84dd149b132289.json` and corresponding Markdown report: final real-host attempt was blocked before any listener check, credential access, service request, or disposable resource creation.

## Phase 4.4.2.6 coverage remediation evidence (2026-11-09)

- `tests/test_planner.py`, `test_agent_execution.py`, `test_dialogue_router.py`, `test_conversation_agent.py`, `test_session_service.py`, `test_memory_service.py`, `test_conversation_context.py`, `test_context_assembler.py`, `test_output_budget.py`, `test_streaming.py`: 32 new behavior-focused tests in three batches; production modules and coverage configuration were not modified by this phase.
- `PHASE4_4_2_6_COVERAGE_REMEDIATION_REPORT.md` and `PHASE4_4_2_6_COVERAGE_REMEDIATION_RESULTS.json`: batch evidence, skip accounting, incremental baseline-hit trace, static scan, hashes, and honest PARTIAL result.
- `PHASE4_4_2_6_TARGETED_TEST_LOG.txt`: focused tests, forward/reverse regression, and supplemental trace; includes initial test/tracer attempts as preserved diagnostic history.
- `PHASE4_4_2_6_FULL_TEST_LOG.txt` and `PHASE4_4_2_6_FULL_TEST_LOG_RUN1.txt`: two complete suite invocations retained; final-designated second run is 912 tests / 902 pass / 0 fail / 0 errors / 10 skips.
- `PHASE4_4_2_6_COVERAGE_REPORT.md`: Stage 5 historical baseline and 93-line / 69-arc supplemental delta. Current all-package coverage is NOT_MEASURED because coverage.py is unavailable; no exclusion or denominator changes were made.

## Phase 4.4.3 final offline acceptance evidence (2026-11-09)

- `PHASE4_4_3_FINAL_OFFLINE_ACCEPTANCE_REPORT.md` and `.json`: final status, runtime/coverage scope, tests, skips, static dependency results and evidence hashes.
- `PHASE4_4_3_FULL_TEST_LOG.txt`: full unittest suite under branch coverage in the launcher venv; 912 run / 906 pass / 0 fail / 0 errors / 6 Windows link permission skips.
- `PHASE4_4_3_COVERAGE_REPORT.md`, `PHASE4_4_3_COVERAGE_DATA.json`, `PHASE4_4_3_COVERAGE_REPORT_RAW.txt`, `.coverage_phase4_4_3_final_20261109`: full 80-module coverage data, JSON and text reports. Statement lines 87.2794%, branches 78.6278%, below gate.
- `PHASE4_4_3_PROJECT_CONDA_DIAGNOSTIC_LOG.txt`: retained preliminary run using the wrong Conda env; not the final regression result.

## Phase 4.4.3.2 final bounded coverage remediation (2026-11-09)

- Test additions: `tests/test_phase4_4_3_2_batch_a.py`, `tests/test_phase4_4_3_2_batch_b.py`, `tests/test_phase4_4_3_2_batch_c.py`, `tests/test_phase4_4_3_2_batch_d.py` (23 behavior tests; no production-code changes).
- `PHASE4_4_3_2_TARGETED_TEST_LOG.txt`: merged targeted regression, 384 passed.
- `PHASE4_4_3_2_FULL_TEST_LOG.txt`: 935 run / 929 passed / 0 failed / 0 errors / 6 skipped.
- `PHASE4_4_3_2_COVERAGE_DATA.json` and `PHASE4_4_3_2_COVERAGE_REPORT.md`: all 80 modules; 87.7462% statement lines / 79.6241% branches; +64 lines / +53 arcs from prior baseline.
- `PHASE4_4_3_2_FINAL_COVERAGE_REMEDIATION_REPORT.md` and `PHASE4_4_3_2_FINAL_COVERAGE_REMEDIATION_RESULTS.json`: full outcome and remaining gates.


## Phase 4.4.4.2 concentrated remediation (2026-11-09)

- Production: `agentforge/planner.py`, `agentforge/agent_execution.py`, `agentforge/conversation_orchestrator.py`; unified host Runner: `AF042_UNIFIED_AGENT_HOST_ACCEPTANCE.py`.
- Tests: `tests/test_planner.py`, `tests/test_agent_execution.py`, `tests/test_conversation_agent.py`, `tests/test_phase4_2_unified_host_acceptance.py`, and `tests/test_dialogue_router.py`.
- Reports: `PHASE4_4_4_2_FINAL_IMPLEMENTATION_REPORT.md`, `PHASE4_4_4_2_FINAL_ACCEPTANCE_RESULTS.json`, `PHASE4_4_4_2_ARCHITECTURE_CHANGELOG.md`, `PHASE4_4_4_2_TARGETED_TEST_LOG.txt`, `PHASE4_4_4_2_FULL_OFFLINE_TEST_LOG.txt`, `PHASE4_4_4_2_INITIAL_TEST_DIAGNOSTIC.txt`, `PHASE4_4_4_2_HOST_TRACE_LOG.txt`, `PHASE4_4_4_2_COVERAGE_REPORT.md`, `PHASE4_4_4_2_COVERAGE_DATA.json`, `PHASE4_4_4_2_COVERAGE_RAW.txt`, `PHASE4_4_4_2_STATIC_DEPENDENCY_CHECK.json`.
- Offline: 288 targeted passed; full 942 run / 936 passed / 0 failed / 0 errors / 6 link-permission skips. Coverage 87.5433% statements / 79.3072% branches across 80 modules. Full host matrix BLOCKED before Runner startup; no live requests or temporary resources.
