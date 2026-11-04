# AgentForge Roadmap

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
