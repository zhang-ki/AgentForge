# AgentForge MVP 1.0

AgentForge is a local, read-only code assistant. It indexes one explicitly selected repository, retrieves source evidence, and can ask a locally hosted Qwen model to explain that evidence. It also provides persistent conversations and approval-based cross-session memory.

## Architecture

- **Python** scans repositories, parses Python symbols and static relationships, retrieves evidence, builds bounded context, validates citations, and coordinates the CLI and conversation console.
- **Project SQLite** remains the authoritative local code index for file snapshots, chunks, symbols, and static references. It currently represents one selected repository at a time.
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
5. For long-term memory, use `/memory suggest`, review the proposal, `/memory save 1`, then explicitly `/memory approve <id>` and `/memory sync`. A model proposal is not saved as active memory until approved. Revocation and expiry remove it from retrieval after synchronization/validity checks.

`Start-AgentForge.cmd` starts the console without starting the models. `Start-Qwen.cmd` and `Start-Qwen-Embedding.cmd` can start the services individually. PostgreSQL is not started or stopped by AgentForge.

## Index and storage

```powershell
$Python = '.\.agentforge\af012-venv\Scripts\python.exe'
& $Python -X utf8 -B -m agentforge --help
& $Python -X utf8 -B -m agentforge index --repo .\path\to\repository
& $Python -X utf8 -B -m agentforge search SomeSymbol --top-k 5
```

Indexing requires an explicit repository path. Switching the active code-index snapshot requires running `index --repo` for that repository; conversation history remains in PostgreSQL and is scoped by repository identity. The existing SQLite index is not a multi-repository index.

Local data is kept under `.agentforge/data/`:

| Data | Store |
|---|---|
| Code files/chunks/symbols/static references | `.agentforge/data/index.sqlite3` |
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
- Long conversation history is bounded; the current history summary is a deterministic excerpt, not a model-generated semantic summary.
- Chroma retrieval is optional and explicitly enabled in the console. RRF, reranking, and a production score threshold remain deferred.
- AF-011 chat and AF-012-MVP database/Chroma/model acceptance covered bounded scenarios; they are not a guarantee for arbitrary repositories, questions, or model outputs. See [release notes](RELEASE_NOTES.md).

## License

No license file is included in this MVP snapshot. Add a license only after choosing and approving the project’s distribution terms.
