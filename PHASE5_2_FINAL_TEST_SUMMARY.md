# Phase 5.2 Final Offline Test Summary

Date: 2026-11-12

## Current full regression

The full unittest discovery run used the repository virtual environment.

- Run: 1,073
- Passed: 1,070
- Failed: 0
- Errors: 0
- Skipped: 3
- Result: PASS_WITH_SKIPS

The skips are not counted as passes:

| Test | Environmental reason |
|---|---|
| test_scanner.ScannerTests.test_directory_symbolic_link_escape_and_link_root | WinError 1314 prevented directory symbolic-link creation. |
| test_scanner.ScannerTests.test_file_symbolic_link_escape | WinError 1314 prevented file symbolic-link creation. |
| test_scanner.ScannerTests.test_link_substitution_after_scan_is_rejected | WinError 1314 prevented substitution symbolic-link creation. |

Full current log: PHASE5_2_FINAL_OFFLINE_VERBOSE_20261112_01.txt, SHA-256 b93497891abd09d2f5ae7002cde93e9572ab43e2b986bc7e6517707ae65b167b. It is retained locally and excluded from the project commit.

## Current checks

- Static dependency and quality-boundary tests: 21/21 passed.
- Python compilation: 155 application, runner, and non-fixture test files compiled.
- git diff --check: passed.
- No DeepSeek, Qwen Chat, or Embedding requests were made during closure.

## Historical related tests

R4.2.3 related modules passed 348/348 forward and 348/348 reverse. These are HISTORICAL_PASS and were not rerun as separate ordered runs during this closure.

## Interpretation

The full suite has no failures or errors. The three Windows symbolic-link cases remain unmeasured at runtime because the process lacks OS permission to create those links. Their names and reasons came from the current verbose log.
