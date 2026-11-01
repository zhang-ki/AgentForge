# AgentForge

> `feature/1.1-core` development snapshot. The published MVP 1.0 remains frozen on `main` and tag `v1.0.0-mvp`; the changes described below are not yet released.

The latest 1.1 real-host attempt is preserved in `AF011_1_FULL_HOST_RESULT_d1adc4d20278413a808b465c3eeade4d.json`. It completed 19 of 30 answer turns; 19 answer-generation requests and 7 semantic-summary requests all returned HTTP 200, then turn 20 stopped before its answer request because the accumulated summary exceeded the conversation context budget. The offline code now stores a compact rolling replacement summary, compacts oversized legacy summaries when semantic summarization is available, and raises the bounded context budget from 3,000 to 3,500 UTF-8 bytes. Offline tests pass; this change still needs real-host verification. Earlier failed attempts remain preserved. The branch remains **PARTIAL** and is not a completed 1.1 release.

AgentForge is a local, read-only code assistant. It indexes one explicitly selected repository, retrieves source evidence, and can ask a locally hosted Qwen model to explain that evidence. It also provides persistent conversations and approval-based cross-session memory.

## Architecture

- **Python** scans repositories, parses Python symbols and static relationships, retrieves evidence, builds bounded context, validates citations, and coordinates the CLI and conversation console.
- **Project SQLite** remains the authoritative local code index for file snapshots, chunks, symbols, and static references. The 1.1 development branch keeps one independent SQLite index per registered repository, with a compatibility path for the legacy index.
- **PostgreSQL** stores repository-scoped conversations, messages, history excerpts, approved memories, provenance, audit events, and vector-sync outbox records.
- **Chroma** stores rebuildable code and approved-memory vectors. A vector candidate is checked against the authoritative SQLite or PostgreSQL record before use.
- **Qwen through llama-server** provides local chat and embedding APIs. AgentForge sends requests only to explicitly configured loopback endpoints; the model files and runtime are not included.

Retrieval follows: repository snapshot → intent-aware lexical/symbol/relationship retrieval and optional Chroma candidates → source identity and content checks → bounded context → local Qwen → request-local citation validation and Answer Contract. Historical experiments did not establish that RRF improves retrieval, so RRF, reranking, and a production similarity-based refusal threshold are not enabled by default.

## Requirements

- Windows for the included double-click launchers; Python 3.10 or newer.
- A local PostgreSQL server with an `agentforge` database and least-privilege `agentforge_app` role for persistent conversations and memory.
- Local llama.cpp `llama-server` runtime and the chat and embedding GGUF assets described below. They must be obtained and configured by the operator; they are not distributed here.
- The `persistence` optional dependency group for PostgreSQL, migrations, and Chroma.

## Install

From the cloned project directory, create an isolated environment and install the persistence features:

```powershell
py -3 -m venv .agentforge\af012-venv
& .\.agentforge\af012-venv\Scripts\python.exe -m pip install -e ".[persistence]"
```

Prepare PostgreSQL separately: bind it to loopback, create the dedicated database and application role, and permit that role only the access needed by the migrations and application. Do not put the password in source, command history, or this repository. The Windows launcher asks for it securely on first use and stores it encrypted for the current Windows account.

## Local model assets

By default, launchers look for a `models` directory next to the project directory. Set the user environment variable `AGENTFORGE_MODEL_ROOT` if your assets are elsewhere. Expected layout:

```text
<model-root>/
  qwen3.5-4b/
    Qwen3.5-4B-Q4_K_M.gguf
    .runtime/llama-b11424/llama-server.exe
    .runtime/llama-b11424/llama-server-impl.dll
  qwen3-embedding-0.6b-gguf/
    Qwen3-Embedding-0.6B-Q8_0.gguf
```

The launcher verifies the pinned embedding model/runtime assets before starting them. Both services bind to `127.0.0.1`. Keep the model windows open while using AgentForge. The launcher does not download models, change Windows security settings, or stop an unknown process.

## Start on Windows

1. Start the local PostgreSQL service.
2. Double-click `Start-AgentForge-All.cmd`. It checks or opens the local chat and embedding model windows, waits for model readiness, applies database migrations, then opens the conversation console.
3. In the console, use `/new Project question`, `/list`, `/switch 1`, `/history`, or `/exit`. `/switch` takes the number shown by `/list`. Use `/rename New title` and `/delete` for the current conversation.
4. To build or refresh vectors for the currently indexed repository, use `/chroma index`; `/chroma on` enables Chroma retrieval for the current console session. `/chroma rebuild` recreates vectors from the current SQLite snapshot.
5. For long-term memory, use `/memory suggest`, review the proposal, `/memory save 1`, then explicitly `/memory approve <id>` and `/memory sync`. A model proposal is not saved as active memory until approved. Revocation and expiry remove it from retrieval after synchronization/validity checks. If a removed repository is registered and indexed again, use `/memory rebuild` to explicitly requeue its approved repository-scoped memories for vector synchronization.

`Start-AgentForge.cmd` starts the console without starting the models. `Start-Qwen.cmd` and `Start-Qwen-Embedding.cmd` can start the services individually. PostgreSQL is not started or stopped by AgentForge.

## Index and storage

```powershell
$Python = '.\.agentforge\af012-venv\Scripts\python.exe'
& $Python -X utf8 -B -m agentforge --help
& $Python -X utf8 -B -m agentforge index --repo .\path\to\repository
& $Python -X utf8 -B -m agentforge search SomeSymbol --top-k 5
```

Register a repository once, index it, then switch among repositories without rebuilding an unchanged repository index:

```powershell
& $Python -X utf8 -B -m agentforge repo register .\path\to\repository
& $Python -X utf8 -B -m agentforge repo index .\path\to\repository
& $Python -X utf8 -B -m agentforge repo list
& $Python -X utf8 -B -m agentforge repo switch .\path\to\repository
& $Python -X utf8 -B -m agentforge repo rebuild .\path\to\repository
```

`index --repo PATH` remains supported and registers, indexes, and activates that repository. Each repository has a stable path-derived identity and its own SQLite file under `.agentforge/data/repositories/`; unchanged indexes are reused. `/repo list`, `/repo register PATH`, `/repo switch ID-or-number`, `/repo index`, `/repo rebuild`, and `/repo remove ID` are also available in the desktop console. Removing an active repository is refused until another indexed repository is selected. Removal deletes only AgentForge's repository index and its Chroma vectors/registration; source files and PostgreSQL conversations/memories are retained.

Local data is kept under `.agentforge/data/`:

| Data | Store |
|---|---|
| Code files/chunks/symbols/static references | `.agentforge/data/repositories/<repository-id>.sqlite3` (legacy `.agentforge/data/index.sqlite3` remains readable for its owner) |
| Code and memory vectors | `.agentforge/data/chroma/` |
| Conversations, messages, summaries/excerpts, approvals, provenance, audit, outbox | Local PostgreSQL |
| Windows launcher credentials | `.agentforge/launcher/`, encrypted for the current Windows user |

These are local runtime data and are excluded from Git. Chroma is a rebuildable vector store, not the source of truth. Local SQLite and PostgreSQL backups remain the operator's responsibility.

## Tests

Install only the project extras needed for the feature under test, then run the offline suite:

```powershell
$Python = '.\.agentforge\af012-venv\Scripts\python.exe'
& $Python -X utf8 -B -m unittest discover -s tests -v
```

Tests use synthetic repositories, temporary directories, and in-memory SQLite where applicable. Run them with the same environment where `.[persistence]` was installed. The suite may skip real Chroma adapter checks if the persistence extra is absent, and may skip Windows link tests if the current account lacks link-creation rights. A passing offline suite does not replace a real local database/model acceptance run.

## Boundaries

- Repository scanning is constrained and read-only; the Agent tool loop has a finite read-only tool allowlist.
- Citations are checked against request-local evidence identity, paths, line ranges, and source text. This does not prove every natural-language claim is entailed; generated answers can remain `limited` and need user judgment.
- Static Python relationships do not prove dynamic runtime behavior.
- Long conversation history remains in PostgreSQL and is bounded in the prompt. With an explicitly selected local chat provider and usable evidence, older complete turns can be summarized by Qwen and stored with their source message identity; otherwise the system uses a labeled deterministic extract. The context budget is a UTF-8 byte proxy, not an exact tokenizer count.
- For 1.1 conversation requests the default context planning limit is 3500 UTF-8 bytes. This is a bounded byte proxy, not an exact tokenizer count or a guarantee for every model/template. Semantic summaries use a compact replacement capped at 1800 UTF-8 bytes; oversized legacy summaries are compacted before reuse when the local semantic summarizer is available. Failed compaction preserves the prior summary and original messages.
- The 1.1 branch has deterministic synthetic coverage for repository isolation and a 30-turn context lifecycle. Those checks use temporary SQLite/test doubles; they do not replace live PostgreSQL, Chroma, or Qwen acceptance for the new branch changes.
- A bounded real-service entry point is available as `AF011_1_FULL_HOST_ACCEPTANCE.ps1`; it has only passed offline checks in the current environment and does not yet verify desktop or PostgreSQL service restart. It must not be treated as complete 1.1 acceptance.
- Chroma retrieval is optional and explicitly enabled in the console. RRF, reranking, and a production score threshold remain deferred.
- AF-011 chat and AF-012-MVP database/Chroma/model acceptance covered bounded scenarios; they are not a guarantee for arbitrary repositories, questions, or model outputs. See [release notes](RELEASE_NOTES.md).

## License

No license file is included in this repository snapshot. Add a license only after choosing and approving the project’s distribution terms.


### Long conversation summaries

The 1.1 branch keeps original messages in PostgreSQL and uses a bounded rolling summary for older turns. A source-linked ledger helps retain important statements from user messages; it does not establish that a user's statement is true of the code. If a protected statement does not fit or a summary update fails, the old checkpoint is retained. Summary and context limits are UTF-8 byte estimates, not exact model tokens. The latest summary repair is offline-tested only; its real Qwen acceptance is still pending. See `AF012_CONTEXT_RELIABILITY_REPORT_20261101.md`.

### 1.1 development acceptance note

The current 1.1 development branch uses a bounded conversation plan with a 5% byte reserve. This is not a model-token guarantee. The latest reserve and prompt changes have offline tests only; full host acceptance and some memory/context behaviors remain pending. See `AF011_1_HARDENING_ACCEPTANCE_20261101.md`.
