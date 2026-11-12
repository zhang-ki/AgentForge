# MVP 1.0 Release File Manifest

## Phase 5.1 — automatic Chroma lifecycle and degraded retrieval (2026-11-10; feature branch only)

- `agentforge/index_lifecycle.py`, `store.py`, `indexer.py`, `embeddings/chroma_index.py`, and `product_retrieval.py`: explicit lifecycle states, authoritative SQLite snapshot comparison, incremental derived-vector reconciliation, candidate verification, and visible lexical-only degradation.
- `agentforge/cli.py`, `conversation_orchestrator.py`, `conversation_console.py`, `presentation.py`, `launchers/start-all.ps1`, and `Start-AgentForge.cmd`: default vector retrieval, Embedding readiness in the standard launcher, automatic repository registration/first-use refresh, General isolation from repository retrieval, and user-visible degraded state. Chroma remains embedded; no service process is started.
- `tests/test_index_lifecycle.py`, `tests/test_launcher_index_startup.py`, and the updated repository, CLI, conversation, and presentation tests: state transitions, incremental updates, hash mismatch, fallback, launch behavior, and general-without-repository regressions.
- `PHASE5_1_IMPLEMENTATION_REPORT.md`, `PHASE5_1_ACCEPTANCE_RESULTS.json`, and `PHASE5_1_TEST_LOG.txt`: Phase 5.1 scope, hashes, offline verification, and explicit real-host NOT_MEASURED status.
- `PHASE5_1_6_R1_FINAL_ACCEPTANCE_REPORT.md`, `PHASE5_1_6_R1_ACCEPTANCE_RESULTS.json`, `PHASE5_1_6_R1_SCENARIO_MATRIX.md`, and `PHASE5_1_6_R1_EXECUTION_LOG.txt`: R1-01..07 historical PASS evidence; the original R1-08 same-process Runner failure and R1-09 NOT_MEASURED status are preserved.
- `PHASE5_1_6_R2_PERSISTENT_REPORT_20261110_152503.md`, `PHASE5_1_6_R2_PERSISTENT_RESULTS_20261110_152503.json`, `PHASE5_1_6_R2_PERSISTENT_SCENARIO_MATRIX_20261110_152503.md`, and `PHASE5_1_6_R2_PERSISTENT_TEST_LOG_20261110_152503.txt`: current R1-08/R1-09 persistent-Chroma evidence. The original host execution log is unchanged; cleanup status is derived from existing subchecks.
- `PHASE5_1_6_R2_PERSISTENT_RESULTS_20261110_152503_RAW.json` and `PHASE5_1_6_R2_PERSISTENT_REPORT_20261110_152503_RAW.md`: byte-preserved pre-correction artifacts and hashes, retained to audit the cleanup summary correction.
- `PHASE5_1_FINAL_CLOSURE_REPORT.md` and `PHASE5_1_FINAL_CLOSURE_RESULTS.json`: final historical/current evidence mapping, production-code baseline hash reconciliation, formal Collection integrity, and cleanup closure. Decision: `PASS_WITH_HISTORICAL_EVIDENCE`; no Phase 5.2 work was started.

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



## Phase 5.2.2 artifacts (2026-11-10)

- PHASE5_2_2_IMPLEMENTATION_REPORT.md — implementation boundary, call flow, test results, and limitations.
- PHASE5_2_2_ACCEPTANCE_RESULTS.json — machine-readable acceptance matrix, hashes, and NOT_MEASURED items.
- PHASE5_2_2_TEST_LOG.txt — focused, forward/reverse regression, static AST, compile, and diff-check logs, including resolved initial test issues.
- PHASE5_2_2_ARCHITECTURE_CHANGELOG.md — architecture before/after and unchanged security boundaries.

## Phase 5.2.2.1 runtime observability artifacts (2026-11-10)

- `agentforge/observability.py`: request lifecycle tracker, privacy allowlist, JSONL rotation, stderr progress, provider timing/usage projection, bounded stage outcome summary.
- Instrumented integration points: `agentforge/presentation.py`, `agentforge/cli.py`, `agentforge/planner.py`, `agentforge/conversation_orchestrator.py`, `agentforge/product_retrieval.py`, `agentforge/embeddings/base.py`, `agentforge/embeddings/chroma_index.py`, `agentforge/memory_retrieval.py`, `agentforge/providers/deepseek.py`, `agentforge/providers/llama_server.py`.
- Tests: `tests/test_observability.py`, plus presentation log isolation in `tests/test_presentation.py`.
- `PHASE5_2_2_1_IMPLEMENTATION_REPORT.md`, `PHASE5_2_2_1_ACCEPTANCE_RESULTS.json`, `PHASE5_2_2_1_TEST_LOG.txt`, `PHASE5_2_2_1_RUNTIME_OBSERVABILITY_DESIGN.md`, `PHASE5_2_2_1_ARCHITECTURE_CHANGELOG.md`: scope, outcome, checks, design, and retained test output.

## Phase 5.2.2.2 artifacts

- `PHASE5_2_2_2_IMPLEMENTATION_REPORT.md` — implementation boundary, trace diagnosis, offline results, host limitation, and hash ledger.
- `PHASE5_2_2_2_ACCEPTANCE_RESULTS.json` — machine-readable checks, statuses, measurements, and source/test hashes.
- `PHASE5_2_2_2_PERFORMANCE_REPORT.md` — historical timing evidence and post-change metrics that remain unmeasured.
- `PHASE5_2_2_2_TEST_LOG.txt` — preserved targeted, ordered regression, compile/static checks, and final complete offline run.
- `PHASE5_2_2_2_HOST_ACCEPTANCE_REPORT.md` — read-only host preflight and three blocked live scenarios.
- `PHASE5_2_2_2_ARCHITECTURE_CHANGELOG.md` — before/after responsibilities and preserved boundaries.
- `tests/test_phase5_2_2_2.py` — bounded behavior tests for index reuse/batches, request retrieval independence, context overflow fallback, and concise/debug output.

## Phase 5.2.2.2 artifacts

- `PHASE5_2_2_2_IMPLEMENTATION_REPORT.md` — implementation boundary, trace diagnosis, offline results, host limitation, and hash ledger.
- `PHASE5_2_2_2_ACCEPTANCE_RESULTS.json` — machine-readable checks, statuses, measurements, and source/test hashes.
- `PHASE5_2_2_2_PERFORMANCE_REPORT.md` — historical timing evidence and post-change metrics that remain unmeasured.
- `PHASE5_2_2_2_TEST_LOG.txt` — preserved targeted, ordered regression, compile/static checks, and final complete offline run.
- `PHASE5_2_2_2_HOST_ACCEPTANCE_REPORT.md` — read-only host preflight and three blocked live scenarios.
- `PHASE5_2_2_2_ARCHITECTURE_CHANGELOG.md` — before/after responsibilities and preserved boundaries.
- `tests/test_phase5_2_2_2.py` — bounded behavior tests for index reuse/batches, request retrieval independence, context overflow fallback, and concise/debug output.

## Phase 5.2.2.3 — hot index and reliable answer (2026-11-10)

Phase reports and final hashes:
- PHASE5_2_2_3_IMPLEMENTATION_REPORT.md — SHA-256 2E530EC2DB8AF0AAC08A4F6FDED4D093F8F6C166E38B3AF1381FD6033DE45624
- PHASE5_2_2_3_ACCEPTANCE_RESULTS.json — SHA-256 F9B9844E8B4B7507D81803C084F605B4D1BD08A92B615124B99FE3564C191330
- PHASE5_2_2_3_PERFORMANCE_REPORT.md — SHA-256 4C4F010A94B0F555C823F71142F9D4989A4015C215505D2B2247564036C6EEE0
- PHASE5_2_2_3_TEST_LOG.txt — SHA-256 5F9B48415940FEDAFFDD149C41689C80B3CC54EDDE64FE39E03809925981F2A6
- PHASE5_2_2_3_HOST_ACCEPTANCE_REPORT.md — SHA-256 A803223FF6DE11FE10F4B186C858B265B82967E39789533B680B1514BFF3D718
- STATUS.md — SHA-256 1E4DCFFBD1691BB4F41AB262CF0F8A210340DFE25240F33C0605DD56780382B3
- ROADMAP.md — SHA-256 309F4FED056249DF3AAB460F838E99829F745027E341CBE26E7EAEF8794FD8F1
- ARCHITECTURE.md — SHA-256 22C2809B02EC6E1D534101BD394B7FCB6D5AEB9A447E600253845F42F60EE596

Test behavior changes in the pre-existing dirty worktree:
- tests/test_conversation_agent.py: phase-start SHA-256 5814468C0E9AE4265588C7BFD09998EB8999B3EBBA8B2A367E47B417A8CC372C; final SHA-256 EBE644491A2FD9CC0848711F3028AA5A3F0DCF65766E7402F24DA2855F4572AC.
- tests/test_phase5_2_2_2.py: phase-start SHA-256 3CFDED1D40B7EC818586C635EF737F4FAA70D2C340745228C80BA2BB773BEA2A; final SHA-256 5EA82C567CF0B0544BC6332F0BC3C8963495D6D6FD1679902E941A673EEE01A3.

Production files reviewed in this phase were not modified. The Phase 5.2.2.3 JSON records their unchanged hashes and the historical raw transcript/lifecycle evidence digests. This manifest entry does not include a self-referential hash for FILE_MANIFEST.md.

## Phase 5.2.2.3 final evidence addendum (2026-11-11)

This final addendum supersedes the earlier pre-final Phase 5.2.2.3 report hashes below; the pre-final files remain archived and unchanged.

- `PHASE5_2_2_3_IMPLEMENTATION_REPORT.md`: SHA-256 9C3681DD3899C0DC01BE4C223D28DE29354FB6C292A1DCFB6C6C7CB76939B626
- `PHASE5_2_2_3_ACCEPTANCE_RESULTS.json`: SHA-256 6DE1719758F991CAAC5FDE95D0FA66A6483D03C5D6A4AE4C5837058681846F7F
- `PHASE5_2_2_3_PERFORMANCE_REPORT.md`: SHA-256 1763044EB0FEE0476A80F913B63F0EA2A60EB4A1AE5016A263F4FD3E74E1D824
- `PHASE5_2_2_3_TEST_LOG.txt`: SHA-256 03D3B61CBE7CB0D1719116851C87AAAC3BA0F631BEE28138706BCB15668616E2
- `PHASE5_2_2_3_HOST_ACCEPTANCE_REPORT.md`: SHA-256 7029EBB6953EA02E6B03452E6EDF0E9C4A1D42700AD31A3F36BFC97DD68FD213
- `PHASE5_2_2_3_STATIC_DEPENDENCY_CHECK.json`: SHA-256 3EEAC6A84DEC24B718C10BE717B735239E55D6A8D7C94BF492EDED93A140C2EE
- `PHASE5_2_2_3_POSTFIX_LOCAL_RETRIEVAL_20261111.json`: SHA-256 921072E539C7AA291F4048A05B24B98C95D63FFDD9C8C244B7D591C0E5A51A94
- `PHASE5_2_2_3_FULL_OFFLINE_20261111.txt`: SHA-256 5B7C5CDA93457AB75B3EA9AF1A4CF07D5E87215737CB3FEC9EDCAD9CDAEAFFDC
- `PHASE5_2_2_3_RELATED_FORWARD_FIXED_20261111.txt`: SHA-256 36FAC08EAD22EEE52310CB4E0A14C633C2150891F9354287C1E02FCAA475CDEB
- `PHASE5_2_2_3_RELATED_REVERSE_FIXED_20261111.txt`: SHA-256 8B4D2AF28ACEE8C44775EA67ED803B50F801DE3A1F70B3AF03A225856D729EDB
- `PHASE5_2_2_3_COMPILE_CHECK.txt`: SHA-256 CC115581AEDD15E985F8369F3DDC939FD0EA476FC40C670514FF7A541584B6C0
- `PHASE5_2_2_3_HOST_RUN_af72223bd8964907a6612f32986e5481.json`: SHA-256 83699EDE838C3A4884292930C868FD2D8BFC4245AAB277E4A99270ADB891E1DB
- `PHASE5_2_2_3_HOST_TRACE_af72223bd8964907a6612f32986e5481.jsonl`: SHA-256 DE6AF0D992B14CB99EF388A5B463F3870C198E9410AC5FDAB298618176A19C07

Final outcome: PARTIAL; full offline regression has six environment-permission skips, the post-fix local index reports one stale SQLite file snapshot, and the DeepSeek host Runner failed in post-lifecycle result extraction. No Phase 5.2.3 work was started.
## Phase 5.2.2.3-R1 final delivery (2026-11-11)

- PHASE5_2_2_3_R1_FINAL_REPORT.md — final stage decision and verified fixes.
- PHASE5_2_2_3_R1_ACCEPTANCE_RESULTS.json — SHA-256 21b7ecc4085e2ccaad1213cd564c2eb696bafa62241f63ec7f0dc9c9fad0aab9; machine-readable offline/host results, limits and evidence digests.
- PHASE5_2_2_3_R1_HOST_ACCEPTANCE_REPORT.md — one synthetic DeepSeek Flash host run; A PARTIAL, B FAIL, C NOT_MEASURED.
- PHASE5_2_2_3_R1_PERFORMANCE_REPORT.md — measured cold/warm preparation, embedding, retrieval and historical 202-call attribution caveat.
- PHASE5_2_2_3_R1_SCENARIO_MATRIX.md — scenario statuses without inheriting historical passes.
- PHASE5_2_2_3_R1_TEST_LOG.txt — full offline run, targeted orders, static checks and the single host result.
- Raw host evidence remains in PHASE5_2_2_3_HOST_RUN_6446655cbd9d4bf2a44f75d554071f38.json and PHASE5_2_2_3_HOST_TRACE_6446655cbd9d4bf2a44f75d554071f38.jsonl; original logs/results were not overwritten.

The phase remains PARTIAL; Phase 5.2.3 has not started. The JSON does not self-embed its hash; its final hash is recorded here.

## Phase 5.2.2.3-R2 final evidence (2026-11-11)
- PHASE5_2_2_3_R2_FINAL_REPORT.md — SHA-256 2DD35D8B76A45BC676871CEC1053A9F33F67E9B8A5B2DB2DDBC960BD32D8B053
- PHASE5_2_2_3_R2_ACCEPTANCE_RESULTS.json — SHA-256 651188636EFB57916386BCC939726A8FC15175C697FBF02A9BF7628CF63E5B56
- PHASE5_2_2_3_R2_SCENARIO_MATRIX.md — SHA-256 C74DF5EC8448752087A4C1C28A5A7F52F9628281EAFC13A38F978AE62653E3CD
- PHASE5_2_2_3_R2_TEST_LOG_20261111_FINAL.txt — SHA-256 EB8A975D2783A49EF46010C6356DFDE5FD691F31BE84CE29417080B16A8AEFA2
- PHASE5_2_2_3_R2_HOST_TRACE_LOG.txt — SHA-256 049B20B600FD9E96ADB82F8BE293B75E453D3142B2FEFDD647C7A099AA3F3C9E
- PHASE5_2_2_3_R2_ARCHITECTURE_CHANGELOG.md — SHA-256 8448ACDC7FFB7D37D3F90176E9A8A9DD74664CC373E7F24B4A4FA51E112AC98A
- PHASE5_2_2_3_HOST_RUN_824df395e1a5455ca1a3c5c5f5d51494.json — SHA-256 D789D50FFE3C3C201FC414FFEE77B77CE5128261CAA1A26B8A93525D83155ACF
- PHASE5_2_2_3_HOST_TRACE_824df395e1a5455ca1a3c5c5f5d51494.jsonl — SHA-256 75A6377589F7F18D65B917A19EA18AAE03C632EC7EA19FA8D655F84EE9F9D6B3
- PHASE5_2_2_3_HOST_RUN_3b581a79b1d74113b21b0ce7288d94e2.json — SHA-256 3D359A16C195CD186F5360C76D8FBC763AE6F686A0029C1763289EC874825157
- STATUS.md — SHA-256 47CAC125B3364562C6039613F59CAF83F3DABD0DCDE1759E4B7C9113EFB25CF8
- ROADMAP.md — SHA-256 D98BAD5009AECF32C281C031C43D7D6290368FAB38D8EBEAC22401E366D7F261
- ARCHITECTURE.md — SHA-256 9BB9DF60B159AD6A35D35900E5B04E063985FA96B01C3806D0C29B9B1EE242ED

R2-related current code/test SHA-256 (captured after the host run; not pre-change hashes):
- agentforge/conversation_orchestrator.py — SHA-256 3F3BA57827C342CA054D6A6959482B137D72E560C71E9AA6EFEEDA067C777F95
- agentforge/providers/deepseek.py — SHA-256 59970D908880374E868010EE8E33B60207B1995E66C925865F9366A007295A30
- agentforge/providers/llama_server.py — SHA-256 FC1E02A12BD4BCACC5BDC27BC655F4B4806B7D04D19D7CEB89A72EA31670D35B
- PHASE5_2_2_3_HOST_ACCEPTANCE.py — SHA-256 9CC9747826933AA5D4D67816DD63DE6E5D2E094AEDEFB1633153A2A0730656A4
- tests/test_conversation_agent.py — SHA-256 ABC5E1EF893A7619C44F977889EF80DEE848880CEF5156E2AF262A72B600CFA6
- tests/test_dialogue_router.py — SHA-256 9A84C48B3C00E4A3C0A81F502FFD2B86881DA563C8A1FBA5222E46B8C7719C97
- tests/test_phase5_2_2_3_runner.py — SHA-256 2471F71B7A4A768AA729CA7841E86EEEE420AF025C505BF5EFCBA6AFCF7BCF13

FILE_MANIFEST.md does not contain a self-hash. Existing R2 test log and raw host evidence were preserved unchanged.

## Phase 5.2.2.3-R3 closure artifacts (2026-11-11)

- `PHASE5_2_2_3_R3_FINAL_REPORT.md`
- `PHASE5_2_2_3_R3_ACCEPTANCE_RESULTS.json`
- `PHASE5_2_2_3_R3_SCENARIO_MATRIX.md`
- `PHASE5_2_2_3_R3_TEST_LOG.txt`
- `PHASE5_2_2_3_R3_HOST_TRACE_LOG.txt`
- `PHASE5_2_2_3_R3_ARCHITECTURE_CHANGELOG.md`

The R3 report and machine results record current SHA-256 values for changed source/test files and R3 evidence artifacts where captured. No pre-R3 hashes are claimed; the initial worktree already contained user changes.

R3 final artifact and code hashes (SHA-256):
- `PHASE5_2_2_3_R3_ACCEPTANCE_RESULTS.json` — SHA-256 `f89b96e8f20b7d093ad4a3c556c8a6d31478a74242b08d39f059fa25822a74ec`
- `agentforge\conversation_orchestrator.py` — SHA-256 `f8116364f1a29d0942cffb298f01b39befd8b9e7747f9892745e0044886aadd2`
- `agentforge\answer_contract.py` — SHA-256 `33459305362c1a23bbe005535e8d5072f492539a612ae13b18cc0e26aa5d697c`
- `agentforge\models.py` — SHA-256 `3dd0cae31d468cf742c9c6809971f43ef84cdad38801997f2c876cb45b45ff4c`
- `PHASE5_2_2_3_HOST_ACCEPTANCE.py` — SHA-256 `90cc9450e2e245718c609dd77795508d83b2c3cc9ef1d7fd3bd5111a3a9367cd`
- `tests\test_conversation_agent.py` — SHA-256 `7a12cc43eb1139b220ee29b52eb5d568611367398207e1a3d4488852e9d72f68`
- `tests\test_phase5_2_2_3_runner.py` — SHA-256 `3dd4f2e2652067ab732eff89378e568593d804697a0c5d7d4f4cecc524457afd`
- `PHASE5_2_2_3_R3_FINAL_REPORT.md` — SHA-256 `8873a0abe0828c727129b54cbfd6f6246c812b77a682d014c7c92cd801f136a5`
- `PHASE5_2_2_3_R3_SCENARIO_MATRIX.md` — SHA-256 `6e45923b2b7347cea16e61d241c0e36eeba9bacc66ffc9924eaab48f0378d5d3`
- `PHASE5_2_2_3_R3_TEST_LOG.txt` — SHA-256 `5a7de2e7a61d145a832284dd12c8614b168aa4fee0099ec63f6689bab9ad5515`
- `PHASE5_2_2_3_R3_HOST_TRACE_LOG.txt` — SHA-256 `2494ab6141a5cf7663e6b7ebf718bc3c3c7f20996069386fae2b6f318d09e739`
- `PHASE5_2_2_3_R3_ARCHITECTURE_CHANGELOG.md` — SHA-256 `b2f83e4be18910f4dd291627c68896a6ac649b60258d78249ab8b4c20543be80`

## Phase 5.2.2.3-R3.1 final acceptance artifacts (2026-11-11)

R3.1 source/test change and deliverable SHA-256 values:
- `tests\test_phase5_2_2_2.py` — `e5728d63fa59886738e05a66bafe57312759d81d3541fea5638ed96a9016857a`
- `PHASE5_2_2_3_R3_1_PRECHANGE_HASHES.json` — `1db79d29a22526fd087e1e8e8d912cc815da01b9e54f51524ea95f67fcfbf01a`
- `PHASE5_2_2_3_R3_1_FINAL_REPORT.md` — `e040c3c084bce4a80c2d01721cfb18f2aad96777f19649b2e5586c9c704f3015`
- `PHASE5_2_2_3_R3_1_ACCEPTANCE_RESULTS.json` — `f46339b2ad7d0e876e01bc12f00db47850eb4ed91cfa324b1bfa18e5c4ceb710`
- `PHASE5_2_2_3_R3_1_SCENARIO_MATRIX.md` — `dd264e9f69c95b03b92f446336b1809fa3ae1a6b1cdf942af19aac9776d5b248`
- `PHASE5_2_2_3_R3_1_TEST_LOG.txt` — `446ad9b1968b7ffe657eb947a2f0b013d71d763b07af09950ff1e562b8f7e4c1`
- `PHASE5_2_2_3_R3_1_HOST_TRACE_LOG.txt` — `8df569abae186d3f128e1b542d3d2606de068ee026048c58a6313ec34e03c0b2`
- `PHASE5_2_2_3_R3_1_CHANGELOG.md` — `de2b6358ed885af62cdefdaa9d30f949ded1191536faf81961214fd2106dcd1c`

Preserved R3/R2 historical acceptance evidence hashes (historical only; not promoted to current-host PASS):
- R3 report: `8873a0abe0828c727129b54cbfd6f6246c812b77a682d014c7c92cd801f136a5`
- R3 results: `f89b96e8f20b7d093ad4a3c556c8a6d31478a74242b08d39f059fa25822a74ec`
- R3 scenario matrix: `6e45923b2b7347cea16e61d241c0e36eeba9bacc66ffc9924eaab48f0378d5d3`
- R3 test log: `5a7de2e7a61d145a832284dd12c8614b168aa4fee0099ec63f6689bab9ad5515`
- R3 host trace: `2494ab6141a5cf7663e6b7ebf718bc3c3c7f20996069386fae2b6f318d09e739`
- R2 report: `2dd35d8b76a45bc676871cec1053a9f33f67e9b8a5b2db2ddbc960bd32d8b053`
- R2 results: `651188636efb57916386bcc939726a8fc15175c697fbf02a9bf7628cf63e5b56`
- R2 host trace: `049b20b600fd9e96adb82f8be293b75e453d3142b2fefdd647c7a099aa3f3c9e`

R3.1 is `PARTIAL_HOST_BLOCKED`: the current offline regression passed with six disclosed platform skips; current DeepSeek C2 and formal Chroma digest comparison were blocked/not measured. The JSON result contains the full scenario status and pre-change hash comparison. No production source was changed.

## Phase 5.2.2.3-R4 diagnostic artifacts (2026-11-11; BLOCKED before implementation)
- `PHASE5_2_2_3_R4_ROOT_CAUSE_REPORT.md` — `397eef82516a74a124fff0289eebbb4e2ab4c120f100e681ae64de87613b5acd`
- `PHASE5_2_2_3_R4_IMPLEMENTATION_REPORT.md` — `61fc8f1d2bae725fcf6e2085a6187010cb28966eece94453d068b5a6d39676e4`
- `PHASE5_2_2_3_R4_SCENARIO_MATRIX.md` — `d5662efd5d4bd60b25e1461344ff622823fcdb79c189d25114cd98fede263544`
- `PHASE5_2_2_3_R4_TEST_LOG.txt` — `2f12b0c8fbca2dd34c2e0ce4fe39e16162f266dd7c87d05ed80909a028c6a277`
- `PHASE5_2_2_3_R4_HOST_TRACE_LOG.txt` — `a8344cd312e37566f8eebe6911df008a429d7b5973e4d5d9a5f7b52dd50b1d76`
- `PHASE5_2_2_3_R4_DATA_PROTECTION_REPORT.md` — `e599596bcefa506ce1ffce3404ded532b3a1b824b29d337ff51a42fab56118f7`
- `PHASE5_2_2_3_R4_ACCEPTANCE_RESULTS.json` — `6cde8548acbb87ca2aece7a17108789e964e098252ce3cb00303951e1f0e3bae`
R4 stopped at the root-cause gate; no production code, tests, or host resources were changed.


## Phase 5.2.2.3-R4.1 host attempt (2026-11-11)

- PHASE5_2_2_3_R4_1_ROOT_CAUSE_REPORT_20261111_202059_130550ad.md — 4e43c104cc8abe713722c957cbf67af5af1b01c5511e60c64231cd1008eb65e1
- PHASE5_2_2_3_R4_1_IMPLEMENTATION_REPORT_20261111_202059_130550ad.md — 77dedc6351c7b3de62c830d557fd08a277f24438a13cb145c3c0e694943373fd
- PHASE5_2_2_3_R4_1_ACCEPTANCE_RESULTS_20261111_202059_130550ad.json — 0a4aa241f90224395d08a0e86fad19320e3d13d94e05771ac498adee2869dd80
- PHASE5_2_2_3_R4_1_SCENARIO_MATRIX_20261111_202059_130550ad.md — 73a1e390d5b1ac956ccec10a90e1ee0b3ede17c13bb75608cb53ae2b9b2018f3
- PHASE5_2_2_3_R4_1_TEST_LOG_20261111_202059_130550ad.txt — 99e3a8ce3b4ddc2fa58c6e5353cd79c3e021b355c3ef5ac803d4a5d0cacb5900
- PHASE5_2_2_3_R4_1_HOST_TRACE_LOG_20261111_202059_130550ad.txt — 98a3a031933fc122c6d4081a32f30752dac1e3a18d20ac2fa8ff3d73e1829091
- PHASE5_2_2_3_R4_1_DATA_PROTECTION_REPORT_20261111_202059_130550ad.md — fe4d74cd651c63d325b506c2ede46fea06404ee2803713fb70212c440d908488
- PHASE5_2_2_3_HOST_RUN_130550ad9b274d4c907d31cf92f4a949.json — 6d52235fa415c25e78386b92638edec7b813f003945e5f0a5a15ba21f1b4dd17


## Phase 5.2.2.3-R4.2 reports (2026-11-11)

- PHASE5_2_2_3_R4_2_FINAL_REPORT_20261111_01.md — ee1532d9fc0f138f4dd1e371b0c5743347bf3a45ff9d2e50405e23e794e17ada
- PHASE5_2_2_3_R4_2_ACCEPTANCE_RESULTS_20261111_01.json — f08ad3651ab10abeaf130016c6a218724bc467d73046f37f422e85b040a12513
- PHASE5_2_2_3_R4_2_SCENARIO_MATRIX_20261111_01.md — 0ddb2944bd54147450dbf6a44c3c6a872615ed33d3ed7c1237cff7d858c406a2
- PHASE5_2_2_3_R4_2_HOST_TRACE_LOG_20261111_01.txt — 61655bec550261da94c73b4276d7ef3922de7b78f0d66faaa3155d03fb9372f2
- PHASE5_2_2_3_R4_2_MODEL_CAPABILITY_REPORT_20261111_01.md — 68b198a85c1efbe9ae17bdfac75bfacc6bc775fe5870fbfc5eb1d97f9683cb22
- PHASE5_2_2_3_R4_2_DATA_PROTECTION_REPORT_20261111_01.md — 1f14b10f297bbe310b548ce3b4b0b891988335a3d7cc5e30ef0ffeda06631adf

## Phase 5.2.2.3-R4.2.1 final acceptance (2026-11-11)

- PHASE5_2_2_3_R4_2_1_FINAL_REPORT.md — e5c10b268de16c1c3718d2c3c6988bcc2ad333c28456e3609a368ea75c564ef5
- PHASE5_2_2_3_R4_2_1_ACCEPTANCE_RESULTS.json — 67f7700e1ef9d8cd2cd26fd6356d4e6edb1576ae9f438e1c0d4ccc80ee921bfc
- PHASE5_2_2_3_R4_2_1_SCENARIO_MATRIX.md — 9c327735c16a19421335f8af31ddc573c85e717d2839866feedfb0bba94c4c91
- PHASE5_2_2_3_R4_2_1_HOST_TRACE_LOG.txt — b2c31613e13962db79c6108085384cc7173826a25e297e654b4ae390c1a18a3d
- PHASE5_2_2_3_R4_2_1_CAPABILITY_REPORT.md — 1779902ebe7ba795ce0871fe9c5f2864f7aa71ddc02e7f4b89db8d19f9ba07a1
- PHASE5_2_2_3_R4_2_1_DATA_PROTECTION_REPORT.md — 67c0288af918678c7ce66b9491a2c7731c3204f717898c824394e55fb54d43b7
- PHASE5_2_2_3_HOST_ACCEPTANCE.py — 8b18db8e800cd84942c40e197e62a40135d05a23d37acf43faac4a71718cfe37
- tests/test_phase5_2_2_3_runner.py — 97297ffbbdca60b017cbbe1151e11df83e75237e4c218b9c98a7ed80197b534a
- PHASE5_2_2_3_R4_2_1_CAPABILITY_PROBE_EVIDENCE_20261111_01.json — 8ffbc9e69b1f57faf5b6fad1068ad5f27f6ecfde31cbda98518cd79ccb166634
- PHASE5_2_2_3_HOST_RUN_19b90cdb0fcd47858f7cd2f49410de31.json — 3a15d6b73db150b1a164d8070e8d5275f9b1ad873c23e0b5d43597810161b4ff
- PHASE5_2_2_3_HOST_TRACE_19b90cdb0fcd47858f7cd2f49410de31.jsonl — c23ffb5179168b25c8eb39da1bd7e9dfed95cbe87827dcb2a11053ba7574fccf
- PHASE5_2_2_3_R4_2_1_QUICK_TESTS_FINAL_8b33a60875c24b3188820aedcaba7bfc.txt — ea1d34db6b97156bddfe82bf56f8718e519a252c11cfd2ade529b5dcbb253fc8
- PHASE5_2_2_3_R4_2_1_RUNNER_STDOUT_cce57ffca2934bc28086decf777b97f7.txt — 6dcf921e5682c2bbcb7a8c28da180d754721baa58fa45da09c74f43b119211a9
- PHASE5_2_2_3_R4_2_1_RUNNER_STDERR_cce57ffca2934bc28086decf777b97f7.txt — e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
- .agentforge/data/model_registry/deepseek.json — d59c463dd96f0fedba3961929752feaad2006f8b650f6f8dee9bba5f1ac39238
- .agentforge/data/model_registry/deepseek.json.r4_2_1_before_refresh_20261111_205620_47adcc41.bak — 0c5ce4b3cf979f767948cf0a65a7ffd9cfbcba400c390df6ca0325ae6672af91

## Phase 5.2.2.3-R4.2.2 final acceptance (2026-11-11; overall FAIL)

- PHASE5_2_2_3_R4_2_2_ROOT_CAUSE_REPORT.md — e63b1da1cb48bcf2b9b0c737e8caf8fcf1ef2991f43d68d2a513787734717ebd
- PHASE5_2_2_3_R4_2_2_IMPLEMENTATION_REPORT.md — 13e4061f2d64df77c8e3befbd8981e728f55c764f89023eb9d3fde3869d0ff27
- PHASE5_2_2_3_R4_2_2_ACCEPTANCE_RESULTS.json — 9c57aea31e55da12d857cf37ab47e518ca6be1398578ebd3a130fcf42a33d5d9
- PHASE5_2_2_3_R4_2_2_SCENARIO_MATRIX.md — 46a984a6279e5ab03ad0354d776c70506e7b3066f63a3eaf995366208a36743f
- PHASE5_2_2_3_R4_2_2_TEST_LOG.txt — 329873079c54afa24ee97a46a75b9b4283cae0c34dda1a866b76d4a717dd331d
- PHASE5_2_2_3_R4_2_2_HOST_TRACE_LOG.txt — 95ce900bba484d5922ae6f21fd7aba04239d60defedf84bef3b3bb42f622de67
- PHASE5_2_2_3_R4_2_2_DATA_PROTECTION_REPORT.md — 5f3658315e75116c5e1e3701fd68f75bb818e4c9352cf17109aa35b3e6f0779b
- PHASE5_2_2_3_HOST_RUN_0328addc1e444c27999984669321aa22.json — d4c59346655a5d346fe575da0de6175cf109edb4b66144fd88bac31132fe3eab
- PHASE5_2_2_3_HOST_TRACE_0328addc1e444c27999984669321aa22.jsonl — 04cebf48df554172fbf8103fd06d6a12359b8405a96f2ccbe05fe26e9518db59
- PHASE5_2_2_3_HOST_RUN_acce0b2622b2497d8cc4c318f5c1fd03.json — 692053589a965b8f393bec97df4d5dd9f9fe400d3eb9e67d3feecb732aa7fd89
- PHASE5_2_2_3_HOST_TRACE_acce0b2622b2497d8cc4c318f5c1fd03.jsonl — 596677d02e06db7d61eb117bb9cac104a58bb6ae3b8f3b22721a2d1766949787
- PHASE5_2_2_3_R4_2_2_POSTFIX_FORWARD_20261111.log — 67a58f91ae672b1d656d2837d0bb6b551d94e890f1338bfab56eaa6c302efab1
- PHASE5_2_2_3_R4_2_2_POSTFIX_REVERSE_20261111.log — 2bf9724e5764cc831c3fc99bd900977e0e4f0b5f19f85af9ee9d3e51670d8be0
- PHASE5_2_2_3_R4_2_2_POSTFIX_DIFFCHECK_20261111.log — 5a8464fdf1368d7e4e7a8075e17c7c88a53adbfe6f06f3f2eb4c017d1939a581
- `agentforge/conversation_agent.py` — aa667e633dba474f9b3d03557ce2e9c943664c8ba9c190af74a195e4815ab49e
- `agentforge/conversation_orchestrator.py` — d5708c6d6e62bb3fc6c189f924e1497508e08a58af17ee379f70ef6982946ad1
- `agentforge/context_assembler.py` — ff510bf449a21af07206faaf6bac7bf9892c460ef0be9a8dbe77a9e52c4ad9d2
- `PHASE5_2_2_3_HOST_ACCEPTANCE.py` — b15bc3fe42873e426bf8f15122f2faf9f76efd9b263d1b724a4f087b106d1fcf
- `tests/test_conversation_agent.py` — c4c98291afc2cd80aa5e17140fcbaaf9235c0dd3a9d45ad3f0d9fcbce93fe1ee
- `tests/test_context_assembler.py` — f7c41906a3c80ed2c22295b3a8b47a2970aba1884c5825061d426c3741244e3e
- `tests/test_phase5_2_2_3_runner.py` — d2055d2878ada92ca774808d3bb5d1719d359d7355524f97cb4cf3964770fa95






## Phase 5.2.2.3-R4.2.3 deliverables and source hashes (20261112_011027)

- PHASE5_2_2_3_R4_2_3_ROOT_CAUSE_REPORT_20261112_011027.md — 146269679c9e3bceecfa03bf8bda8befdf9e210b8edf9d2999bd534fab4b8d70
- PHASE5_2_2_3_R4_2_3_IMPLEMENTATION_REPORT_20261112_011027.md — 1275ecfe57a09cc18ce22cd3e531743d5fb17f25464b80c9dcacd1a7253fc55e
- PHASE5_2_2_3_R4_2_3_ACCEPTANCE_RESULTS_20261112_011027.json — c2e7aa106fafcf406d5e81c7d746ad909b458f330e66ca4522f2742019ec0607
- PHASE5_2_2_3_R4_2_3_SCENARIO_MATRIX_20261112_011027.md — db7c1dd788fd9ff1714c5fb6b65c8927ca6c2d7ea7f4fa4ebfeff7fe3fa1376c
- PHASE5_2_2_3_R4_2_3_TEST_LOG_20261112_011027.txt — 9c5cf03089de4c799ffa3368ff2bad1f2049708cff99a3020f10bd51e54b9cee
- PHASE5_2_2_3_R4_2_3_HOST_TRACE_LOG_20261112_011027.txt — 0a3bc8009a583d7afb52c3f3883d1e70a9f02c3895ce4c50cdc3421c52a3030d
- PHASE5_2_2_3_R4_2_3_DATA_PROTECTION_REPORT_20261112_011027.md — 0a8244b34cd7bbeccddcd70f43afd8bb3deddc77f4e40bbfad08e58bd279edbf
- PHASE5_2_2_3_HOST_RUN_3de6d934ea19477fa24d7dac3e85adca.json — 8621ad076c130a4fb2c0db05c932094086c90ad1bafeeb97bd7fa0ac0b219d09
- PHASE5_2_2_3_HOST_RUN_b20160750dad498283f5a6e4bcb1fe53.json — c3bdce833ee70cf7e112ab6623c642673c76fec87162494d057774474b273d7e
- PHASE5_2_2_3_HOST_RUN_e502f6bfd9c04e7ea877e42c36315b1c.json — 887cf4a6e69b7586a5b0f3e30cdac8256099459e1b2c98e5a568c40c6b43e64a
- PHASE5_2_2_3_HOST_TRACE_e502f6bfd9c04e7ea877e42c36315b1c.jsonl — 612ebb4b8d09e225b8b069be8c4647974e53c917c3f1dce8a328d3d799ba22c1
- PHASE5_2_2_3_R4_2_3_FULL_OFFLINE_20261112_07.txt — 29338ce712a2ab863f643e94ec706a8d9154f3847d45b24abad01c2594408491
- PHASE5_2_2_3_R4_2_3_RELATED_FORWARD_20261112_07.log — 399381ccc846ca0150905c5e4ebb9f7c24836a36cdd1aecc187553c10bdd9963
- PHASE5_2_2_3_R4_2_3_RELATED_REVERSE_20261112_07.log — 5ae648424acdc37b5fc0a1da8808ad988b5724c7281e909f37b7e18056f723cc
- agentforge/product_retrieval.py — 6f9bb9ab5a99b99eeb7eab8bd5bd1a303fa8da2b8854752997fc21cad4028d8e
- agentforge/conversation_orchestrator.py — 0f8ea1b6afe8d058d246d18ae8fb2b37aa07170b8e9807db8bd318fe5535968d
- agentforge/evidence_evaluator.py — 221e6886cae329ddefa94257fd5b8c1c9a74023be74d892dcda61caa9e26780c
- agentforge/dialogue_router.py — 6f9579111af06d095e8b29c909095f59436f72ea2d829b29a5aa0b6f2960c63f
- PHASE5_2_2_3_HOST_ACCEPTANCE.py — 8f2cfa541531ce60abc26d67417c5cffc0a9dd80caac4b100750f8cbce0172d0
- tests/test_conversation_agent.py — b78d4f722540907d145f123de5feaa2b87237b1577c5608857496f12c5c4dbea
- tests/test_phase5_2_2_3_runner.py — 6ecce720dc1872063f6e5009768ffd658d5725c2ab409176e78d94c8fb4d26a0
- STATUS.md — f2dc7d5ef57e9d2443bff04988150cd03e87e35ac46154da7afd722fa36e293f
- ROADMAP.md — 572910a0d0d46bc00d8e8002db797f2a2d9ad3bdb60a471e00d522984b1305f2
- ARCHITECTURE.md — 524c3335a27ac4cc64e331db12116ebdd05f2743643ec7c20ba3a0424e30f4fc

## Phase 5.2 closure and v0.1 delivery artifacts (2026-11-12)

Versioned closure records:
- PHASE5_2_FINAL_CLOSURE_REPORT.md
- PHASE5_2_FINAL_ACCEPTANCE_RESULTS.json
- PHASE5_2_FINAL_TEST_SUMMARY.md
- PHASE5_2_FINAL_DELIVERY_REPORT.md
- V0_1_RELEASE_READINESS_REPORT.md
- V0_1_KNOWN_LIMITATIONS.md

The selected R4.2.3 evidence bundle includes its root-cause, implementation, acceptance, scenario, test, host-trace, and data-protection reports; the three raw synthetic C2 result JSON files; and the final raw synthetic C2 trace. The final closure report records the evidence hashes through the machine result. Full local test output and live database snapshots remain local-only and are not included in the project commit. Temporary directories, .env/credentials, local databases, Chroma persistence data, model assets, caches, and unrelated historical host traces are excluded.

## Phase 5.2 closure and v0.1 delivery artifacts (2026-11-12)

Versioned closure records:
- PHASE5_2_FINAL_CLOSURE_REPORT.md
- PHASE5_2_FINAL_ACCEPTANCE_RESULTS.json
- PHASE5_2_FINAL_TEST_SUMMARY.md
- PHASE5_2_FINAL_DELIVERY_REPORT.md
- V0_1_RELEASE_READINESS_REPORT.md
- V0_1_KNOWN_LIMITATIONS.md

The selected R4.2.3 evidence bundle includes its root-cause, implementation, acceptance, scenario, test, host-trace, and data-protection reports; the three raw synthetic C2 result JSON files; and the final raw synthetic C2 trace. Full local test output and live database snapshots remain local-only. Temporary directories, .env/credentials, local databases, Chroma persistence data, model assets, caches, and unrelated historical host traces are excluded.


Selected R4.2.3 evidence filenames:
- PHASE5_2_2_3_R4_2_3_ROOT_CAUSE_REPORT_20261112_011027.md
- PHASE5_2_2_3_R4_2_3_IMPLEMENTATION_REPORT_20261112_011027.md
- PHASE5_2_2_3_R4_2_3_ACCEPTANCE_RESULTS_20261112_011027.json
- PHASE5_2_2_3_R4_2_3_SCENARIO_MATRIX_20261112_011027.md
- PHASE5_2_2_3_R4_2_3_TEST_LOG_20261112_011027.txt
- PHASE5_2_2_3_R4_2_3_HOST_TRACE_LOG_20261112_011027.txt
- PHASE5_2_2_3_R4_2_3_DATA_PROTECTION_REPORT_20261112_011027.md
- PHASE5_2_2_3_HOST_RUN_3de6d934ea19477fa24d7dac3e85adca.json
- PHASE5_2_2_3_HOST_RUN_b20160750dad498283f5a6e4bcb1fe53.json
- PHASE5_2_2_3_HOST_RUN_e502f6bfd9c04e7ea877e42c36315b1c.json
- PHASE5_2_2_3_HOST_TRACE_e502f6bfd9c04e7ea877e42c36315b1c.jsonl

## v0.1 no-evidence / streaming verification (2026-11-12)

New reports:
- V0_1_NO_EVIDENCE_ANSWER_FIX_REPORT.md
- V0_1_NO_EVIDENCE_ACCEPTANCE_RESULTS.json
- V0_1_STREAMING_AUDIT_REPORT.md
- V0_1_STREAMING_ACCEPTANCE_RESULTS.json
- V0_1_REGRESSION_REPORT.md
- V0_1_RUNTIME_TRACE_LOG.txt
- V0_1_CHANGELOG.md
- V0_1_FULL_OFFLINE_TEST_LOG_20261112.txt
- V0_1_REGRESSION_TEST_LOG_20261112.txt (interrupted attempt; not a completed test result)

Updated status and architecture references: STATUS.md, ROADMAP.md, ARCHITECTURE.md. Source/test changes for this task are limited to conversation orchestration, streaming presentation/provider parsing, and their regression tests. Historical host traces and existing untracked files were retained.

## v0.1 Fast Track final verification artifacts — 2026-11-12

- `V0_1_FAST_FINAL_REPORT.md`
- `V0_1_FAST_ACCEPTANCE_RESULTS.json`
- `V0_1_NO_EVIDENCE_HOST_RESULT.md`
- `V0_1_STREAMING_RESULT.md`
- `V0_1_DATA_PROTECTION_REPORT.md`
- `V0_1_GIT_DELIVERY_REPORT.md`
- `V0_1_RUNTIME_TRACE_LOG.txt` (append-only; historical offline trace retained)

This run did not commit, push, start/stop services, send model requests, or create/clean synthetic persistent resources. Host acceptance is BLOCKED; release readiness is NOT_READY. Full details and local-only data hashes are in the report and machine results.

## v0.1 Fast Track continuation artifacts — 2026-11-12

- V0_1_FAST_RESUME_20261112_REPORT.md
- V0_1_FAST_RESUME_20261112_RESULTS.json

These record the resumed host preflight, unresolved live answer/Session result, native Streaming status, and current data-protection comparison. User-local STATUS.md is ignored by Git and was not modified.


## v0.1 No-Evidence Answer Verification — 2026-11-12

This verification added reports and machine results only; no production source or tests were modified.
- V0_1_NO_EVIDENCE_ROOT_CAUSE_REPORT.md — SHA-256 876addb51496a0d8782a6fe2bac6bb5f832184a82f048aa5fa4874b8d6218e61
- V0_1_DEEPSEEK_DIRECT_CALL_REPORT.md — SHA-256 76754ab2309fdfeb18dd0dfdbe23b58d01946d584563be94b36fe0f042847cbf
- V0_1_APPLICATION_E2E_REPORT.md — SHA-256 69d630a4d58f6e107288c3a8cfd09023f17d2c91b2f82942c9de3a9b2d8d2a25
- V0_1_ANSWER_CONTRACT_AUDIT.md — SHA-256 9ea264cc46ac15092d94ada03eea7e491b570a9f187641af5523a6481686ab6b
- V0_1_SESSION_PERSISTENCE_REPORT.md — SHA-256 939bf1f1ed131bc36f84583381380586c46cd9dd31483c2197390e62748637a3
- V0_1_FINAL_ACCEPTANCE_RESULTS.json — SHA-256 3659cbbebc794b68c7a888aa1ebaedee01d9cb087127dd240f3a04dd0777aa7f

## v0.1 Final Closure artifacts — 2026-11-12

| File | SHA-256 |
|---|---|
| V0_1_FINAL_CLOSURE_REPORT.md | d50c1c8f979a4f4d70ad1131801fc41899faac61b7446c7e26f560a7fd26c68c |
| V0_1_FINAL_CLOSURE_RESULTS.json | ed5d86f7215d11cc00af37aef8bd4ae7fb281ccfb9cd1479b4d72d0fdabec59d |
| V0_1_FINAL_DATA_PROTECTION_REPORT.md | 0f41d41a50b68af8144e2cabb6f3bd102926772ec315584df1532505eecd3007 |
| V0_1_STREAMING_FINAL_REPORT.md | 227c8df99522f87d29d5f0317166255339bdb6e413adcdbfc29ad6998b6c29d3 |
| V0_1_FINAL_GIT_DELIVERY_REPORT.md | d270d2df78c7870f98b16201929d07a9f7ea553760e301c3b5a47cbc58693bc0 |
| V0_1_FINAL_RUNTIME_TRACE.jsonl | 114601ebf7721c20285bc5098f79075f248e7bae88e18da291714d193643aa96 |
| V0_1_FINAL_CLOSURE_R0_INVENTORY_20261112.json | d09ff7fd1fa4c8cc3b5200e6c7cba254efb4332a6a452e5e2ed5ce5c0a9def99 |
| .tmp/V0_1_FINAL_CLOSURE_DB_AUDIT_20261112.py | e6d1b80b13338634120ef4681382ce88015d3cf2ef45c67f5977c8e5b5712ada |
| .tmp/V0_1_FINAL_CLOSURE_CHROMA_SNAPSHOT_20261112.py | 330712719694b475958fb7b8060b0e608dad3c71c978d7da2de3d58831051ffa |

The final status remains DATA_PROTECTION PARTIAL and Git delivery NOT_PUSHED. Existing user files and the R0 baseline were preserved. This manifest is append-only and does not include a self-hash.


## Phase 5.2.4 Forensic Deliverables — 2026-11-12

| File | SHA-256 |
|---|---|
| V0_1_FINAL_DATA_FORENSICS_REPORT.md | 9f9cdb4c17c253d72fdbcead348c92879a7700ac942ff13a3105b0d7059b016b |
| V0_1_CHROMA_PHYSICAL_FORENSICS.md | 3983da4a5749122e3ecc14cca8eb50993e9351ab764ffd23fbe54f7130eefca0 |
| V0_1_REGISTRY_HISTORY_RECONSTRUCTION.md | 68c44fcfd94574007e1d66a09865db2712514b6975a8eeb294cb68557e1c52d5 |
| V0_1_SQLITE_SESSION_INTEGRITY_REPORT.md | d64d487a98b4959e049db577706c8b9cedeae59e1da880036a533534f1ecfbd3 |
| V0_1_CURRENT_DATA_BASELINE.json | 3606db4d1d2206e017002e2d341446351d962d1f6a5a496428454d4179e6b3bb |
| V0_1_DATA_PROTECTION_FINAL_RESULTS.json | 588520656b28c707483c1ef7ea8bb72703b568894c48c57e89b0078988d20212 |
| STATUS.md (Phase 5.2.4 append) | 37e6d6a84e06dd11b5d07887cc3eb75b65ddd9061be1670612c77c87c83cea48 |
| ROADMAP.md (Phase 5.2.4 append) | 21ff0794fd4a44c1da7534d14bedd030b35eaaf7a3d1c027f0cb4d623f28f3b6 |
| ARCHITECTURE.md (Phase 5.2.4 append) | d7eac886e51e9a1ae9ee7d3b75ad9fb9f801a00b471145d06df6253abc155a4e |

The forensic inventory hash is listed after generation. This manifest omits its own hash to avoid self-reference.
| V0_1_FORENSIC_FILE_INVENTORY.json | 8ba4c3120cc496522a53263bb0243dd1492ad3cfc5713fce5574e06af578cb7f |


## V0.1 Unknown Source ID / No-Evidence Answer Deliverables — 2026-11-12

The SHA-256 inventory for this task's reports and machine result is appended below. The test log was created before the final host check and is preserved as-is. This manifest does not hash itself.
| File | SHA-256 |
|---|---|
| V0_1_UNKNOWN_SOURCE_ID_ROOT_CAUSE_REPORT.md | 064e4714a5b6b7d0b8f64598cb300f643c829dce42d7b1b0c16d2998b5c6d1ce |
| V0_1_UNKNOWN_SOURCE_ID_FIX_REPORT.md | 49541f6a11c83dfb90b94fed31fec8aa243e3ec5b47b5105a7bcdc7f5be79cf8 |
| V0_1_UNKNOWN_SOURCE_ID_TEST_RESULTS.json | e1cb60a518fb8cd2f267b2ecaa7666b159536be83f674b98f91f0360b13b06cd |
| V0_1_UNKNOWN_SOURCE_ID_TEST_LOG.txt | e8563314ea4157890cd3925deb58e8a96a5761b277a5020d3d76d956bfa7b473 |
| V0_1_UNKNOWN_SOURCE_ID_HOST_ACCEPTANCE_REPORT.md | 35ff38b2b59629fc99a69faeaf13986e43800df15ac3f8529b9908f923540b38 |
| V0_1_UNKNOWN_SOURCE_ID_CHANGELOG.md | a1315e1dfc1030a1efd064d4794043771710e4b6b199b6a3034b2ba37c15044d |
| STATUS.md | edd3dc5b93d88aa21f3fb720655622444bc0d5fbbe8c7108fce0acc4c5307303 |
| ROADMAP.md | 4eba27781bea503bb5483fc57e1865ed53fb998bb62873d2158ed98b4c542954 |
| ARCHITECTURE.md | d8e4e9bbe4b98fed91017567b414f5b97f7606cde192d4776a27b82625572fa1 |
| agentforge/answer_contract.py | 9666162e0f73a8c2d63fa1c800287c066098de8ccba40e52ef8162641ffa1c05 |
| agentforge/conversation_orchestrator.py | 1dac9c251e7e6f0da1fbcdbf8869726ebb2e581a0d246150cbbcbf9ecd11530d |
| agentforge/providers/dialogue_protocol.py | d5e2731ffe3976428e1d6344dafaa176419dd267e4c9ca981a907ea287179f73 |
| agentforge/streaming.py | abd8db4c42bf0d79eb333085c47a0d16bfc13f4ee72c608b61fcc25ff0cdc8d3 |
| agentforge/presentation.py | 93a93e94ee3f33cf736b054da1d285730c8f665a5cc1c66c9b6b056cd7dca7ec |
| tests/test_dialogue_router.py | a39a1bc1b38106424a34d1f3a6c927f61176404c6c791a400248751d9670096b |
| tests/test_streaming.py | e919eddd254b08cad9c15740b6b7669064a8ac4bf2d06a1d0dfd97f0ccc8ff7e |
| tests/test_presentation.py | 7b64e2ae65bc413856c45306fbb18e63b8823d431d967f098ecca7f6a3055159 |
| tests/test_session_service.py | 69a695509461a8c82195a374bddd8b43d96519c9f8ce1ce7ad122ceb423cdd71 |

## Phase 5.2.5 Final Acceptance & Release Closure — 2026-11-12

This closure is PARTIAL_BLOCKED / NOT_READY. Existing same-named historical acceptance JSON was preserved; the timestamped Phase 5.2.5 machine result is authoritative for this run. Hashes below are SHA-256. This append does not hash FILE_MANIFEST.md itself.

| File | SHA-256 |
|---|---|
| V0_1_FINAL_ACCEPTANCE_REPORT.md | 62861829e7262a957f422b31b3c5670dbd4f17b8ca2b801e8d8acaf8dc281376 |
| V0_1_FINAL_ACCEPTANCE_RESULTS_20261112T125533Z.json | 614921e85e64ad25d0b12e8ca1143868bf30355f8b6e91f7f6f1461b5130e02d |
| V0_1_FINAL_ACCEPTANCE_RESULTS.json (pre-existing historical, preserved) | 3659cbbebc794b68c7a888aa1ebaedee01d9cb087127dd240f3a04dd0777aa7f |
| V0_1_FINAL_DATA_AUDIT_REPORT.md | 08d060c78c641200ade5b97e886b147297aca454ecd5017b45b666e392bd3beb |
| V0_1_FINAL_REGISTRY_AUDIT_REPORT.md | 9d069c2b73fde10da6ee712328fabe9f7cae6a8c47c074b3272bccca58d7947e |
| V0_1_FINAL_CHROMA_AUDIT_REPORT.md | 1c1f662f26089e9f6be2c0c260781223a47baad98e5ad20dea19bcee5be466df |
| V0_1_FINAL_KNOWN_LIMITATIONS.md | 4822af155242cfd9574faae2c1b41daaa6e6c700c2b0bf40e77e2b4510d54576 |
| V0_1_FINAL_RELEASE_READINESS.md | 172ed5be23c204b3cbfa628f4f6659596c7c8e242255cea5e3e49e34da78e950 |
| V0_1_FINAL_DELIVERY_REPORT.md | b008faca192657ec53973ef5578037ca0f030040b02a284980f537416cce860e |
| STATUS.md (local ignored status log) | 402012b1bbf832a6ba4985d2ccf24bd3a2d3adde7af36ca48e0fe7debb8d10b5 |
| ROADMAP.md | 9914711623666c189dd6885a7133c9fcef81c084a1a96c4a8ba3bb42907c8503 |
| ARCHITECTURE.md | 1d64e6081d1faa082c51c117089a04554a28e330e34e60dc0fc1cc39ff890d0c |

## Phase 5.2.6 Final Acceptance and Delivery — 2026-11-12

| File | SHA-256 |
|---|---|
V0_1_PHASE_5_2_6_FINAL_ACCEPTANCE_REPORT.md | 0d37914efd26b29b052c82ae12aa79a9c5bc8fa2a1f54b9004bb8915a5bab3bc |
V0_1_PHASE_5_2_6_DATABASE_DIAGNOSTIC.md | c5f1089adec3e4263d7f0a0bc0c4dac520a213937b0ba435fd198cb9e005ca09 |
V0_1_PHASE_5_2_6_SESSION_AUDIT.md | ba0f7f0b53405ee48387983ff6a8f0fef2cdc2a354bbfbdd5b80adf7cc69ed97 |
V0_1_PHASE_5_2_6_DATA_PROTECTION_REPORT.md | 45283e7ba835cf7ad4a1e85f166d5fa792e7ca832ee321c56665182f35d7b839 |
V0_1_PHASE_5_2_6_GITHUB_CONNECTIVITY_REPORT.md | 90c0ca5aac1fdd25f3813560c5c530a9b89753701a437e60e4daa094a6845030 |
V0_1_PHASE_5_2_6_FINAL_RESULTS.json | 0b341c26eff10ed97dc388619122007eeb045b9d970051fd4aaf1da6f1bab113 |
V0_1_PHASE_5_2_6_DELIVERY_REPORT.md | 29288261b695d5f84f82f0919807a0543db82cea8c55b3ac0a66e9278b6863f9 |
ARCHITECTURE.md | 533935a5f8defb95a683270b83d5a392a3ff2a7aec062a4438a0ae186ede14ae |
ROADMAP.md | 0d50ddbc3c57df6ec21f06d83746b10d9be2b55376ed9a19bf10f291a793dce4 |
STATUS.md (ignored local status) | facb478eb058cc959f303ca876872ce30bcd9c4f23e8fcf6cfe180bb925d764d |

R0 external inventory SHA-256: 477bb940ac63947fad206d0f65f466e2d65f78140ec5d63b05884b2681de4cc0. This manifest omits its own hash.
