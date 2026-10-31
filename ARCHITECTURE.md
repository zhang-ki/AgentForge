# AgentForge MVP 1.0 Architecture

## Runtime components

```text
Windows launcher / CLI
        │
        ├── Python orchestration
        │     ├── bounded repository scanner and safe file reader
        │     ├── chunker, Python AST parser, SQLite indexer
        │     ├── intent router and lexical/symbol/static-relation retrieval
        │     ├── optional Chroma candidate retrieval
        │     ├── evidence/context builder and Answer Contract
        │     └── conversation and approval workflows
        │
        ├── Local PostgreSQL: conversation and approved-memory authority
        ├── Local Chroma: rebuildable code/memory vector candidates
        └── Local llama-server: Qwen chat and embedding endpoints
```

The standard-library CLI and SQLite code-index path can run without a model. Persistent conversations and memory require the optional persistence dependencies and a local PostgreSQL service. Semantic retrieval requires Chroma, the configured local embedding model, and an available embedding service. Chat generation requires an explicitly selected local chat provider.

## Retrieval and evidence

`scanner.py` discovers allowed source/document files; `safeio.py` reopens and verifies file identity before reading. `chunker.py` creates line-addressable chunks. `indexer.py` coordinates scanning, parsing and a transactional SQLite snapshot. `store.py` keeps the code index at `.agentforge/data/index.sqlite3` and binds that snapshot to one repository at a time.

`product_retrieval.py` and `query_routing.py` choose a channel based on query intent. Exact symbols use structured symbol retrieval; import/call/public API questions may use PackageRelation; ordinary concepts and documents use lexical retrieval and optional Vector candidates. Candidate paths and content are checked against the current repository/index snapshot before they become context. The offline RRF and PackageRelation research paths do not replace the default retriever.

`context.py` assigns request-local evidence IDs and validates citations against those exact records. `answer_contract.py` distinguishes answered, limited and refused outcomes. A valid citation establishes source identity and location, not universal semantic entailment. The application therefore keeps semantic certainty limited where it has no independent proof.

## Persistence responsibilities

| Store | Authoritative data | Scope |
|---|---|---|
| SQLite | Indexed source snapshots, chunks, Python symbols and static references | One active repository snapshot |
| PostgreSQL | Repository identities, conversations, messages, history excerpts, memory candidates/approvals/sources/events and vector-sync outbox | Persistent, repository-scoped user data |
| Chroma | Embeddings and searchable metadata for code and approved memories | Rebuildable local candidate index |

PostgreSQL is the source of truth for conversation and memory state. Chroma does not share a transaction with PostgreSQL; an outbox records vector work. Retrieved memories are rechecked against PostgreSQL for repository scope, approval, revocation, expiry, source, and content identity. Code vectors are checked against the SQLite snapshot. Chroma is not the sole source of either code or memory facts.

Database migrations are in `alembic/`; the CLI exposes session migration and management commands. Runtime credentials are supplied through process environment by the Windows launcher; secrets and local stores are excluded from the repository.

## Conversations and context

`conversation_store.py` implements conversation/message persistence and repository-scoped selection. `conversation_console.py` is the interactive entry point. Each turn loads the selected conversation, current repository identity, bounded recent history/history excerpt, relevant approved memory when enabled, current RAG evidence, and the current question.

History is not unlimited. The current persisted history summary is a deterministic excerpt associated with source messages, not a semantic summary produced by Qwen. The prompt builder applies a bounded UTF-8-size estimate rather than a tokenizer-exact budget. A repository switch selects a different repository-bound conversation set; it does not create a second SQLite code index.

## Local model boundary

The chat and embedding providers use the explicitly configured loopback llama-server endpoints. The Windows launcher verifies health and model identity before starting the console. Model processes are separate from the AgentForge product process; AgentForge does not provide shell execution, arbitrary repository writes, network tools, or model management through its read-only Agent loop.

The release does not include model weights or runtime binaries. Launchers use `AGENTFORGE_MODEL_ROOT` or a `models` directory next to the project directory. They bind services to loopback and do not download assets.

## Deliberate MVP limits

- One active SQLite repository snapshot; switching requires indexing the selected repository.
- Python static analysis cannot prove dynamic imports or runtime behavior.
- Chroma is optional and must be explicitly indexed/enabled.
- RRF, reranking, and production similarity-threshold refusal are not enabled by default.
- Citation validation checks evidence identity and source text but does not prove arbitrary semantic entailment.
- The delivered host acceptance demonstrates specific local PostgreSQL/Qwen/Chroma scenarios, not general reliability for every repository or query.
