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
