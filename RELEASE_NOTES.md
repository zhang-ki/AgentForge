# AgentForge MVP 1.0

AgentForge MVP 1.0 freezes a local, evidence-first code assistant with repository indexing, optional local Qwen generation, PostgreSQL-backed conversations, approval-based long-term memory, and optional Chroma vector retrieval.

## Included

- Read-only scanning, line-addressable chunks, SQLite code index, Python symbol/static relationship extraction, and intent-aware retrieval.
- Local chat and embedding providers with explicit loopback configuration, bounded requests, and no automatic generation retry or offline answer substitution.
- Request-scoped evidence IDs, source/path/line validation, and an Answer Contract that distinguishes answered, limited, and refused responses.
- Repository-scoped sessions and ordered message history in local PostgreSQL, managed through migrations.
- Optional persistent Chroma code and approved-memory vectors, checked against SQLite/PostgreSQL authority before use.
- Windows double-click launchers and CLI/session management.

## Acceptance evidence carried into the freeze

These are bounded historical host runs, not executions performed during the GitHub release task:

- **AF-011:** real local Qwen chat passed its specified source-answer and no-evidence cases. The result supports that tested flow only; it is not a general semantic correctness guarantee.
- **AF-012-B2:** real PostgreSQL CRUD, transaction rollback, reconnect, session history, multi-turn Qwen use, conversation isolation, and a PostgreSQL service-restart restoration check were reported as passed.
- **AF-012-MVP:** the latest host result reported 24/24 checks passed, 5/5 chat requests and 19/19 embedding requests successful with zero retries. The checks included PostgreSQL, Chroma persistence/update/delete/rebuild/reopen, repository/session isolation, and cross-session approved-memory use. Temporary test records and repository were reported cleaned.
- **Desktop lifecycle:** the latest supplied host result reported session identity, ordered history, process close/reopen, and resumed answering checks passed.
- **Long-context:** the latest supplied run is `PARTIAL`: early, middle, recent, reference, isolation, and post-summary cases passed across six model requests, with temporary data cleaned. It is not a full long-context PASS.
- **Offline regression baseline:** the prior reported suite was 344 run, 338 passed, 0 failed, 0 errors, and 6 skipped. The release task reran the suite; current run counts are recorded in `GITHUB_RELEASE_RESULTS.json`. Skips are not counted as passes.

## Limits

- The SQLite code index stores one active repository snapshot; switching the indexed repository requires re-indexing that repository.
- Chroma vectors are optional and must be built/enabled; they are rebuildable candidates, not authoritative records.
- History compression is a deterministic excerpt with a byte-based budget, not semantic summarization.
- Citations are structurally checked against supplied evidence. This does not prove that every generated statement is semantically entailed.
- Static analysis cannot establish arbitrary runtime behavior. Long-context quality and broad production reliability remain unproven.
- RRF, reranking, and a production score-based refusal threshold are not part of the default product configuration.
- No model weights, local database, credential, or runtime binary is distributed.

## Data handling

Runtime databases, Chroma data, credentials, model assets, raw host-runner outputs, and coverage artifacts are local-only and excluded from the release snapshot. Earlier Git history is retained as required; this release does not rewrite old commits.
