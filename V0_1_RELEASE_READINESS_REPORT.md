# AgentForge v0.1 Release Readiness

## Decision

V0_1_FREEZE_ELIGIBLE_WITH_KNOWN_LIMITATIONS

The declared v0.1 scope is ready for freeze review. This does not claim every Phase 4.4 boundary scenario or Provider-native streaming path was measured.

## Verified scope

- Historical R4.2.3 synthetic DeepSeek Flash C2 completed Search, bounded Read recovery, verified SQLite-backed evidence, final DeepSeek answer, and Session restore.
- SQLite remains authoritative for repository text and metadata; Chroma supplies vector candidates.
- Source path, line range, content SHA-256, and evidence-context linkage were verified.
- Tool authorization and schema validation remained active; an out-of-range Read was refused, then a bounded replan used a distinct call ID.
- Session persistence and reload passed in the final attempt.
- Phase 5.1 index lifecycle, persistence, mixed retrieval, and degradation evidence remains historical; current read-only data matches its official-data reference.
- Current offline regression: 1,073 run, 1,070 passed, 0 failed, 0 errors, 3 Windows symlink permission skips.
- Current static dependency tests, compilation, and diff checks passed.

## Accepted limitations

- Answer Contract remains LIMITED. Source identity, path, lines, hash, and request-local mapping are checked, but semantic entailment is not independently proven for every natural-language claim.
- Provider-native token streaming is NOT_MEASURED.
- A complete 15-scenario live DeepSeek matrix was not run in this closure.
- Cloud use with real user repository source is NOT_MEASURED and was not part of the synthetic C2.
- Three symbolic-link tests could not run because Windows returned WinError 1314.

## Release posture

Keep product claims within the verified v0.1 scope. Distinguish source facts from model inference and evidence gaps. Do not describe the Answer Contract as full semantic verification.
