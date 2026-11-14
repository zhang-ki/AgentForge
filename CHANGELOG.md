# Changelog

## 2026-11-09 — v0.1 release candidate verification

- Targeted real DeepSeek Flash `multi_turn` scenario passed on the current worktree: 12/12 Chat requests succeeded, three turns were persisted/reloaded, one synthetic read tool completed, and one Observation-driven replan completed.
- The targeted runner reports PARTIAL because only one scenario was selected; the full 15-scenario matrix is not claimed as passed. Hybrid formatting, protected-write approval, automatic long-result paging, and streaming remain outside v0.1.
- Answer Contract was observed and returned `limited` on all three turns; this is not a semantic-entailment proof.
- Release state: **RELEASE_CANDIDATE** within the declared scope.

Changes relevant to the prepared DeepSeek Agent v0.1 bundle. This file does not imply that the bundle is released.

## 2026-11-09 — v0.1 release preparation (NOT_READY)

- Make DeepSeek Flash the default for new interactive sessions, gated by explicit per-session cloud-data consent; retain an explicit, checked local Qwen choice.
- Apply one bounded correction when the Agent Planner returns invalid JSON after an Observation; re-run full parsing/validation and fail closed if the correction is invalid.
- Preserve inherited Qwen environment credentials when no DPAPI Qwen key file is present.
- Add offline behavior tests and prepare Windows quick-start, limitations, acceptance report, machine results, and a clean review package.
- No post-change real DeepSeek run, full-suite run, coverage rerun, commit, or push was performed. Release remains **NOT_READY**.

## 2026-11-13 — Phase 5.2.8-R2.1 verification closure

- Current DeepSeek Flash S9T1–S9T3 recheck passed, including the prior S9T2 failure; the original pre-send exception was not retained and was not reproduced, so no speculative production change was made.
- Cross-repository Session isolation and T1/T2/T3 context-budget checks passed. Related offline regression passed 153/153; production-package compilation and diff checks passed. The full suite was not rerun because no production or test code changed.
- Current PostgreSQL, SQLite and Chroma logical checks passed without formal data writes. Nine acceptance Sessions were cleaned and the prior selection restored.
- Status: `PASS_WITH_KNOWN_LIMITATIONS`. Answer Contract remains LIMITED; Provider-native SSE, tokenizer/provider input limits, historical PostgreSQL row-level attribution and Chroma physical-file attribution remain limited or unmeasured.
- Detailed reports and raw evidence remain in the timestamped AgentForge-acceptance Phase 5.2.8-R2.1 bundle; the full 15-scenario matrix is not claimed as a single current run.

## 2026-11-14 — Phase 5.3.2-E1-CLOSE

- Reviewed the T1–T7 contract states and retained `LIMITED` without upgrading it to PASS; confirmed the previous stale-process T1 failure remains separately recorded.
- Created a current read-only PostgreSQL/SQLite/Registry snapshot and opened Chroma only from an isolated copy. Current logical integrity passed; formal data files were unchanged during this audit. Historical E1 physical-file attribution remains PARTIAL.
- Preserved the existing mixed worktree. Commit and push were not performed because the E1 starting contents of previously dirty files were represented only by hashes, so the patch cannot be safely isolated.

## 2026-11-15 — Phase 5.3.3 E1 delivery audit

- Rechecked the 46-entry worktree against the E1-close snapshot; all 45 present files matched their archived hashes, and the previously deleted historical trace has an archived copy matching its original SHA-256.
- Four E1-touched code/test files were already modified before E1. The E1 start preserved only hashes, so their prior contents cannot be reconstructed or safely separated. They remain uncommitted; no production or test code is included in this delivery.
- E1 remains PARTIAL with its original LIMITED and historical data-attribution limitations. This record does not upgrade acceptance or claim the implementation is shipped.
