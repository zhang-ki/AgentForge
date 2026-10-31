# AgentForge MVP 1.0 Roadmap

## Frozen release scope

The current public release is MVP 1.0. It includes read-only repository indexing and evidence retrieval, a local Qwen chat path, PostgreSQL-backed repository-scoped conversations and approved long-term memory, and optional local Chroma vectors. Release evidence is summarized in `RELEASE_NOTES.md`.

The release is frozen for publication. No new retrieval algorithm or production threshold is enabled as part of this release.

## Known limits

- The SQLite code index represents one selected repository snapshot at a time; re-index the desired repository when switching the active codebase.
- Current history compression is a deterministic excerpt, not a semantic model summary.
- Static Python analysis has limits around dynamic imports and runtime behavior.
- Citation validation proves that citations map to current supplied evidence, but does not establish that every natural-language claim is semantically entailed.
- Chroma is optional, local, and rebuildable; vectors require explicit indexing and are validated against authoritative records.
- AF-010 research found no stable reason to make RRF a default. RRF, reranking, and production score-based refusal remain deferred.
- Host acceptance covers bounded scenarios. Long-context quality and broad production reliability are not certified.

## Future work policy

Future changes should begin from a reproducible user-visible defect or a clearly scoped product requirement. Preserve the existing security model, keep local data out of version control, and add a focused regression test for any behavior change. Further work is not implicitly authorized by this release.
