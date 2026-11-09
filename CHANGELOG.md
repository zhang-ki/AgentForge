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
