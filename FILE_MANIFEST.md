# MVP 1.0 Release File Manifest

This manifest describes the portable source tree at the repository root.

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
- `learning/`: code-reading and conversation/memory learning guides.
- `README.md`, `ARCHITECTURE.md`, `SECURITY.md`, `ROADMAP.md`: user, architecture, security, and release-scope documentation.
- `RELEASE_NOTES.md`, `GITHUB_RELEASE_CHECKLIST.md`, and `GITHUB_RELEASE_RESULTS.json`: this release's sanitized notes and publication record.

## Local-only data excluded from release

`.agentforge/`, PostgreSQL credentials/backups, SQLite and Chroma user data, model weights and runtimes, Python caches, coverage output, raw host-runner logs/results, and historical diagnostic reports are local artifacts. Historical originals are retained locally where present; this manifest does not assert that old commits have been rewritten.

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
