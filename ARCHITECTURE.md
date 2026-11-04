# AgentForge Architecture

The `feature/1.1-core` branch adds development-only multi-repository indexing, bounded history summarization, and cross-file evidence improvements. Published MVP 1.0 remains frozen on `main`/`v1.0.0-mvp` until these changes are separately reviewed and released.

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
        ├── Local llama-server: Qwen chat and embedding endpoints
        └── Explicit DeepSeek HTTPS provider: optional cloud chat, no automatic fallback
```

The standard-library CLI and SQLite code-index path can run without a model. Persistent conversations and memory require the optional persistence dependencies and a local PostgreSQL service. Semantic retrieval requires Chroma, the configured local embedding model, and an available embedding service. Chat generation requires an explicitly selected provider.

On the `feature/1.1-core` development branch, `model_registry.py` holds the local model default and explicitly refreshed DeepSeek capability metadata. Registry entries describe model capabilities; health checks describe connectivity. Session records persist provider, model, Thinking/effort settings, and cloud-data consent. Qwen Local remains the default for new sessions. A conversation only uses DeepSeek after the user selects a discovered model and confirms the cloud data notice. There is no silent fallback or automatic cloud routing.

## Retrieval and evidence

`scanner.py` discovers allowed source/document files; `safeio.py` reopens and verifies file identity before reading. `chunker.py` creates line-addressable chunks. `indexer.py` coordinates scanning, parsing and a transactional SQLite snapshot. On the 1.1 branch, `store.py` opens one index at `.agentforge/data/repositories/<repository-id>.sqlite3`; the older `.agentforge/data/index.sqlite3` is retained as a compatibility index for its original owner.

`product_retrieval.py` and `query_routing.py` choose a channel based on query intent. Exact symbols use structured symbol retrieval; import/call/public API questions may use PackageRelation; ordinary concepts and documents use lexical retrieval and optional Vector candidates. Resolved static references can add caller/target evidence across files. Candidate paths and content are checked against the current repository/index snapshot before they become context. The offline RRF research path does not replace the default retriever.

`context.py` assigns request-local evidence IDs and validates citations against those exact records. `answer_contract.py` distinguishes answered, limited and refused outcomes. A valid citation establishes source identity and location, not universal semantic entailment. The application therefore keeps semantic certainty limited where it has no independent proof.

## Persistence responsibilities

| Store | Authoritative data | Scope |
|---|---|---|
| SQLite | Indexed source snapshots, chunks, Python symbols and static references | One file per registered repository identity; a legacy index can remain attached to its original repository |
| PostgreSQL | Repository identities, conversations, messages, history excerpts, memory candidates/approvals/sources/events and vector-sync outbox | Persistent, repository-scoped user data |
| Chroma | Embeddings and searchable metadata for code and approved memories | Rebuildable local candidate index |

PostgreSQL is the source of truth for conversation and memory state. Chroma does not share a transaction with PostgreSQL; an outbox records vector work. Retrieved memories are rechecked against PostgreSQL for repository scope, approval, revocation, expiry, source, and content identity. Code vectors are checked against the SQLite snapshot. Chroma is not the sole source of either code or memory facts.

Database migrations are in `alembic/`; the CLI exposes session migration and management commands. Runtime credentials are supplied through process environment by the Windows launcher; secrets and local stores are excluded from the repository. The additive model-binding revision is `af012mm_004`, with Qwen-compatible defaults for existing conversations. `CredentialStore` keeps the optional DeepSeek key in a Windows-user-bound DPAPI file or reads `DEEPSEEK_API_KEY` as an override; keys are not stored in PostgreSQL or ordinary project configuration. The model-list cache is local metadata, not a connection-status record.

## Conversations and context

`repositories.py` maintains stable canonical-path identities, the active repository setting, and one isolated SQLite index location per repository. CLI and console support registration, listing, switching, indexing, rebuilding, and removal. Removing a repository refuses the active one and removes only the AgentForge index, registration, and repository-filtered Chroma vectors; source files and PostgreSQL conversation/memory history are retained.

`conversation_store.py` implements conversation/message persistence and repository-scoped selection. `conversation_console.py` is the interactive entry point. Each turn loads the selected conversation, current repository identity, bounded recent history/summary, relevant approved memory when Chroma is enabled, current RAG evidence, and the current question.

History is not unlimited. When the user explicitly selects the local Qwen provider and the request passes the evidence gate, older complete turns can be summarized and persisted with the last covered source message ID. Each update gives the summarizer the prior untrusted summary plus newly covered complete turns, then stores a bounded replacement summary (up to 1,800 UTF-8 bytes) instead of nesting all prior versions. Oversized legacy summaries are compacted before reuse when semantic summarization is available; if compaction fails, the previous summary and original messages remain intact. Original messages remain in PostgreSQL. If semantic summarization is unavailable, a clearly labeled deterministic extract is used. The 3,500-byte conversation planning guard is distinct from model token budgets. For explicitly selected model requests, `TokenBudgetManager` calculates `context_window - output_reserve - safety_margin` and accounts for message framing, nested protocol fields, and tool schemas before sending. Exact tokenizer counts require a configured model tokenizer; this checkout labels fallback counts `estimated`. API usage is recorded for the response and is not treated as remaining context capacity. Repository switching selects the corresponding independent SQLite index and repository-bound conversations; it does not re-index a previously indexed repository.

## Local model boundary

The chat and embedding providers use the explicitly configured loopback llama-server endpoints. The Windows launcher verifies health and model identity before starting the console. Model processes are separate from the AgentForge product process; AgentForge does not provide shell execution, arbitrary repository writes, network tools, or model management through its read-only Agent loop.

The release does not include model weights or runtime binaries. Launchers use `AGENTFORGE_MODEL_ROOT` or a `models` directory next to the project directory. They bind services to loopback and do not download assets.

The DeepSeek provider sends requests only to the fixed official HTTPS host. It separates `reasoning_content` from user-visible `content`, validates request-local evidence IDs, enforces connection/read/total timeouts, and does not retry. Its optional Agent planner exposes only existing read-only search and symbol-list tools; it does not expose shell, arbitrary Python, writes, or network tools. Tool schemas and tool-call protocol history are included in the request budget.

## Deliberate MVP limits

- Repository identity is derived from the canonical local path; moving a repository changes its identity and requires registering/indexing it again.
- Removing a repository keeps PostgreSQL conversations and Memory for audit/recovery; `repo remove` does not delete that history.
- Python static analysis cannot prove dynamic imports or runtime behavior.
- Chroma is optional and must be explicitly indexed/enabled.
- RRF, reranking, and production similarity-threshold refusal are not enabled by default.
- Citation validation checks evidence identity and source text but does not prove arbitrary semantic entailment.
- The delivered host acceptance demonstrates specific local PostgreSQL/Qwen/Chroma scenarios, not general reliability for every repository or query.
- 1.1 offline tests use temporary SQLite and synthetic/test-double Chroma; the new branch has not undergone a fresh real PostgreSQL/Chroma/Qwen acceptance run.
- DeepSeek discovery and cloud inference have not been performed from the current network-disabled environment. Offline protocol tests do not demonstrate live availability, account access, billing, or model answer quality.


## 1.1 conversation summary fact ledger (offline-verified)

`conversation_summaries.fact_ledger` stores conservative source-linked statements extracted from completed user messages. Each record binds a stable fact ID and exact statement text to source message UUIDs, sequence numbers, and full-message SHA-256. These are records of user statements, not verified code facts or model ground truth. Summary writes merge prior ledger entries with newly covered messages and fail closed if any protected statement is absent, the 1,800-byte persisted summary cap is exceeded, or the write transaction fails. The summary cursor advances only with a successful summary save. Migration `af012ctx_003_summary_fact_ledger` is additive. Offline isolated-database tests passed; the formal PostgreSQL migration and a fresh real Qwen summary run remain unmeasured. Input, Prompt, model output, and persisted summary each have separate UTF-8 byte limits; none is an exact Tokenizer budget.
