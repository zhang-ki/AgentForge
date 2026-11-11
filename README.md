# AgentForge

## DeepSeek Agent v0.1 release preparation (2026-11-09)

Current worktree release status: **RELEASE_CANDIDATE** for the declared v0.1 scope. A current-worktree targeted DeepSeek Flash `multi_turn` host scenario passed (12/12 Chat requests, one tool, one Observation, one successful replan, three persisted turns). The runner overall remains PARTIAL because the 15-scenario matrix was not run. Historical scenarios are recorded separately; results are not merged into a full-matrix PASS.

See [final report](RELEASE_V0.1_REPORT.md), [limitations](RELEASE_V0.1_LIMITATIONS.md), [Windows quick start](RELEASE_V0.1_QUICKSTART.md), and [machine-readable acceptance](RELEASE_V0.1_ACCEPTANCE.json). The prepared package is under `release/AgentForge-v0.1`; it excludes credentials, model weights, formal databases, virtual environments, and historical host-run files.

## Latest AF042 follow-up (2026-11-08)

The user-host DeepSeek run sent 62/62 requests successfully and passed `general_direct` plus the 65,536-token `long_answer`, but the full 15-scenario matrix failed (2 passed, 13 failed). Six completed turns hit an 8 KB diagnostics-metadata persistence limit; bounded metadata compaction is fixed and passes offline tests. The host runner's six-step override rejected a seven-call dependency plan; it now follows the product's bounded eight-step limit, and the targeted `multi_tool_dependency` host rerun passed. The oversized-metadata compaction still needs a live case that exceeds the bound. Offline tests pass: 109 targeted and 808 full-suite (798 passed, 10 skipped). See [follow-up report](AF042_HOST_FOLLOWUP_REPORT_20261108.md) and [machine results](AF042_HOST_FOLLOWUP_RESULTS_20261108.json). The earlier 86.2779% line / 76.7955% branch coverage predates these fixes; current coverage is not measured.

The subsequent targeted DeepSeek `multi_tool_dependency` run passed (10/10 requests, four completed tools, three replanning rounds); other scenarios were not measured. This scenario uses a SQLite lexical index and an empty Chroma close/reopen check, so it does not start Qwen Embedding or verify vector search. The Answer Contract result was `limited`.

## Latest stabilization result (2026-11-08)

**PARTIAL.** The bounded tool-schema discovery path and shared model-aware output budgets have been repaired and passed offline regression (806 run, 800 passed, 0 failed/errors, 6 Windows link-permission skips). Coverage across all 80 production modules is 86.2779% line and 76.7955% branch, below the 90%/85% targets. Real DeepSeek/Qwen and PostgreSQL/Chroma host checks were blocked in this execution environment; no live requests or service operations occurred. See [the stabilization report](AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_REPORT_20261108.md), [machine results](AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_RESULTS_20261108.json), and [coverage summary](AF042_TOOL_DISCOVERY_OUTPUT_BUDGET_COVERAGE_REPORT_20261108.md). Historical Phase 4.2 snapshots below are retained as dated records.

> main and v1.0.0-mvp remain the frozen MVP 1.0 release. The local development branch feature/1.1-core is not released.

**Latest Phase 4.2 status: BLOCKED for real host acceptance.** A unified real-Qwen Agent runner now enters through `ConversationOrchestrator` and reuses the existing Agent execution engine with only synthetic tools. The current environment blocks local network access, so the runner stopped before checking services or reading credentials and sent 0 model requests. The latest offline regression passed 706 tests (700 passed, 0 failed/errors, 6 Windows link-permission skips); all-package coverage is 85.96% line, 76.58% branch, and 83.35% combined. These offline results do not establish real Qwen/PostgreSQL/Chroma acceptance. See [Phase 4.2 acceptance report](PHASE4_2_ACCEPTANCE_REPORT.md), [architecture and matrix](PHASE4_2_ARCHITECTURE_AND_MATRIX.md), and [machine results](PHASE4_2_ACCEPTANCE_RESULTS.json).

The current development worktree adds an Auto-planned route from ordinary conversation into the shared bounded Agent execution engine when iterative repository tools are needed. The Phase 4 offline regression ran 692 tests: 686 passed, 0 failed, and 6 were skipped for Windows link-permission limits. Real Qwen/DeepSeek planning and live PostgreSQL acceptance were not run in this phase; this is not a release approval. See [the Phase 4 integration report](PHASE4_UNIFIED_AGENT_INTEGRATION_REPORT.md).

New interactive conversations select DeepSeek Flash by default only after per-session cloud-data consent. If declined, local Qwen is an explicit alternative when its credential and service are available. Existing sessions retain their persisted model binding; no provider fallback is silent. The bounded `invalid_json` recovery passed the targeted real-host scenario. The interactive first-session default/consent flow has offline tests and remains a recommended manual smoke check.

AgentForge is a local-first code assistant with General, Repository, Hybrid and Auto modes, repository evidence retrieval, persistent conversations, and approval-based long-term memory.


## Architecture

- **Python** parses repositories, plans Auto/Hybrid turns with the selected LLM, retrieves and assesses source candidates, builds bounded context, validates citations, and coordinates the CLI and conversation console.
- **Project SQLite** remains the authoritative local code index for file snapshots, chunks, symbols, and static references. The 1.1 development branch keeps one independent SQLite index per registered repository, with a compatibility path for the legacy index.
- **PostgreSQL** stores repository-scoped conversations, messages, history excerpts, approved memories, provenance, audit events, and vector-sync outbox records.
- **Chroma** is an embedded, local persistent store for rebuildable code and approved-memory vectors; it does not run as a separate service. It is enabled by default for repository retrieval. Every code candidate is resolved back to the authoritative SQLite chunk and content hash before use.
- **Qwen through llama-server** provides local chat and embedding APIs. AgentForge sends requests only to explicitly configured loopback endpoints; the model files and runtime are not included.
- **DeepSeek** is an optional, explicitly selected cloud chat provider. Model metadata is fetched only by `/model refresh`; switching a conversation to a cloud model requires a separate confirmation that its question, relevant code evidence, and necessary history or approved memory may be sent to the cloud.
- **Model Registry and Token Budget Manager** keep model capability metadata separate from connection state and calculate input budgets from the selected model's context and output limits. Counts are labeled `estimated` unless an exact tokenizer is configured; UTF-8 bytes are not presented as token counts.

Retrieval follows: the Auto/Hybrid LLM plan or an explicit Repository request → symbol/relationship/lexical retrieval and Chroma vector candidates when relevant → source identity and content-hash checks → bounded context → selected Qwen or DeepSeek provider → request-local citation validation and Answer Contract. If Embedding or Chroma is unavailable, repository answers continue with SQLite lexical retrieval and show the degraded state. Historical experiments did not establish that RRF improves retrieval, so RRF, reranking, and a production similarity-based refusal threshold are not enabled by default.

### Conversation routing

The interactive conversation console defaults every ordinary question to Auto. The selected model's bounded, schema-validated LLM Planner uses the current question and restored conversation context to choose General, Repository or Hybrid; users do not need to issue a mode command. Planner failures are reported instead of silently switching to lexical routing. `/mode` and `ask --mode` remain advanced overrides for development and acceptance. General does not require repository retrieval. Repository requires source evidence for repository claims. Hybrid can retain its general-knowledge portion when repository evidence is missing. Live external-data requests are recognized and safely refused because no authorized live-data tool is configured. Answer Contract validates citation identity, path, line and structure, but does not prove semantic entailment; conclusions may remain limited.

When the validated conversation plan requests iterative repository inspection, `ConversationOrchestrator` invokes `ConversationAgentExecutor`, which uses the existing `AgentLoop` compatibility facade and `AgentExecutionEngine`. The engine replans from bounded tool observations and the result then returns to the normal retrieval, evidence evaluation, and Answer Contract path. Tool-search observations sent to the replanner contain source IDs and path/line references rather than full excerpts; the later evidence path reloads and validates source content. The conversational route currently exposes only scoped, read-only repository tools. The explicit `agent` command remains available for execution controls; neither route enables shell, arbitrary code, SQL, or network tools by default.


```powershell
python -X utf8 -B -m agentforge ask "Explain attention in a transformer" --provider llama-server --base-url http://127.0.0.1:56572/v1/chat/completions --model qwen3.5-4b
python -X utf8 -B -m agentforge ask "Where is refresh_access_token defined?" --provider llama-server --base-url http://127.0.0.1:56572/v1/chat/completions --model qwen3.5-4b
python -X utf8 -B -m agentforge ask "Explain FIFO generally and show how this repository implements it" --provider llama-server --base-url http://127.0.0.1:56572/v1/chat/completions --model qwen3.5-4b
```

## Requirements

- Windows for the included double-click launchers; Python 3.10 or newer.
- A local PostgreSQL server with an `agentforge` database and least-privilege `agentforge_app` role for persistent conversations and memory.
- A configured DeepSeek Flash credential for the primary cloud model. Interactive credentials are stored using Windows DPAPI for the current user; `DEEPSEEK_API_KEY` may be used as a process-environment override.
- Local llama.cpp `llama-server` runtime and GGUF assets are optional and are not distributed here. They are required only when choosing local Qwen or local embedding features.
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

1. Start PostgreSQL and confirm the `agentforge` database and least-privilege `agentforge_app` role exist.
2. Start PostgreSQL, then double-click `Start-AgentForge.cmd`. It checks PostgreSQL and the local Embedding endpoint, opening the Embedding launcher only when needed, then applies migrations and opens the console. It does not start the chat model or PostgreSQL. Chroma is initialized inside AgentForge when a repository is registered or first used. The project environment must already exist at `.agentforge\af012-venv`.
3. Run `/model auth set deepseek` and complete the hidden credential prompt. Then use `/new Project question`; type `YES` at the separate per-session cloud-data notice. If capability metadata is stale, run `/model refresh` after consent.
4. Register a codebase with `/repo register <path>`. Registration automatically scans and builds its SQLite index, then synchronizes Chroma vectors when Embedding and Chroma are available. Later Repository/Hybrid turns rescan for changes and incrementally update both indexes; `/repo index`, `/repo rebuild`, `/chroma index`, and `/chroma rebuild` remain available for maintenance. Ordinary questions are planned as General, Repository, or Hybrid automatically; repository claims require source evidence and citations.
5. Use `/list`, `/switch 1`, `/history`, `/rename New title`, `/delete`, and `/exit` to manage conversations. Existing sessions retain their saved model selection.
6. For this DeepSeek-first release, use `Start-AgentForge.cmd`; it starts/checks the Embedding service but does not start the optional Qwen chat model. `Start-AgentForge-All.cmd` checks both Qwen Chat and Embedding. Local model binaries and weights remain operator-supplied.
7. Vector retrieval is on by default. If Embedding or Chroma is unavailable, the app falls back to SQLite lexical search and reports that status. `/chroma on|off` is a maintenance/debug override, not a normal setup step. Long-term Memory still requires explicit review and approval before activation.

`Start-AgentForge.cmd` may open the local Embedding model window when that service is not ready; Chroma remains embedded in the Python process. `Start-AgentForge-All.cmd` may also open the Qwen Chat window. PostgreSQL is never started or stopped by AgentForge. No repository watcher or background indexing queue is installed: updates happen synchronously at registration and before each repository-oriented turn, using the existing incremental indexer.


## Model management (1.2 development branch)

New interactive sessions default to DeepSeek Flash after explicit cloud-data consent. Existing sessions keep their model binding. In the interactive console:

```text
/modellist
/model
/model refresh
/model auth status
/model auth set deepseek
/model <model-id shown by /modellist>
/thinking on
/effort high
/model default
```

`/model refresh` explicitly contacts DeepSeek to retrieve public model metadata. `/model auth set deepseek` asks for the key without echoing it and stores it using DPAPI for the current Windows account. `DEEPSEEK_API_KEY` is supported as an environment override; `/model auth status` shows only the source, never the key. A cloud model switch displays the data-sharing notice and requires typing `YES`; declining leaves the session binding unchanged. Use `/model default` to select DeepSeek Flash for the current session; use `/model qwen-local` to explicitly select Qwen Local. See [Model Management](MODEL_MANAGEMENT.md) for configuration, capability limits, token estimates, and failure handling.

No live model request was made during this release-preparation turn. See `RELEASE_V0.1_REPORT.md` for the separate host evidence and the pending authorized validation.

## Index and storage

```powershell
$Python = '.\.agentforge\af012-venv\Scripts\python.exe'
& $Python -X utf8 -B -m agentforge --help
& $Python -X utf8 -B -m agentforge index --repo .\path\to\repository
& $Python -X utf8 -B -m agentforge search SomeSymbol --top-k 5
```

Register a repository once; registration builds its initial index automatically. Later Repository/Hybrid turns detect repository changes and incrementally refresh SQLite and Chroma. The explicit index/rebuild commands below remain available for maintenance and debugging:

```powershell
& $Python -X utf8 -B -m agentforge repo register .\path\to\repository
& $Python -X utf8 -B -m agentforge repo index .\path\to\repository
& $Python -X utf8 -B -m agentforge repo list
& $Python -X utf8 -B -m agentforge repo switch .\path\to\repository
& $Python -X utf8 -B -m agentforge repo rebuild .\path\to\repository
```

`index --repo PATH` remains supported as an explicit maintenance command. Each repository has a stable path-derived identity and its own SQLite file under `.agentforge/data/repositories/`; unchanged indexes are reused. `/repo list`, `/repo register PATH`, `/repo switch ID-or-number`, `/repo index`, `/repo rebuild`, and `/repo remove ID` are also available in the desktop console. Removing an active repository is refused until another repository is selected. Removal deletes only AgentForge's repository index and its Chroma vectors/registration; source files and PostgreSQL conversations/memories are retained.

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
- Long conversation history remains in PostgreSQL. `ContextAssembler` builds a request-only view from the selected model's registered capabilities; compression never removes or overwrites durable messages. Qwen uses the configured 4,096-token `llama-server` window. DeepSeek uses fresh per-model `/models` metadata, and stale or unverified metadata is display-only until `/model refresh` succeeds.
- Input budgets reserve the requested output size and a safety margin, then apply any verified provider input cap and optional `--context-budget` cap. The model's maximum output is a ceiling, not the size automatically requested for every turn. A tokenizer is used only when one is available and registered for that model; otherwise the request records a conservative estimate, not an exact token count. Conversation summaries retain their separate bounded storage limit (1,800 UTF-8 bytes); this is not a model context-window limit.
- Model capability-aware context assembly is offline-tested, including Qwen's launcher-configured 4,096 window, 30/100-turn request views, model switching without mutating stored history, and fail-closed overflow. The current DeepSeek capability cache is stale and has no provenance, so no DeepSeek request was made in this run. See [Model-aware context report](MODEL_AWARE_CONTEXT_REPORT_20261106.md) and [machine results](MODEL_AWARE_CONTEXT_RESULTS_20261106.json).
- The 1.1 branch has deterministic synthetic coverage for repository isolation and a 30-turn context lifecycle. Those checks use temporary SQLite/test doubles; they do not replace live PostgreSQL, Chroma, or Qwen acceptance for the new branch changes.
- A bounded real-service entry point is available as `AF011_1_FULL_HOST_ACCEPTANCE.ps1`; it has only passed offline checks in the current environment and does not yet verify desktop or PostgreSQL service restart. It must not be treated as complete 1.1 acceptance.
- Phase 5.1 default behavior: Chroma retrieval is enabled in the console; SQLite remains authoritative and degraded lexical retrieval is visible if vectors are unavailable. Repository synchronization is synchronous and incremental at registration/first use and before repository-oriented turns. RRF, reranking, background watchers, and a production score threshold remain deferred.
- AF-011 chat and AF-012-MVP database/Chroma/model acceptance covered bounded scenarios; they are not a guarantee for arbitrary repositories, questions, or model outputs. See [release notes](RELEASE_NOTES.md).

### Phase 3 integration status (2026-11-06)

Both Planner paths can receive a bounded set of approved memories scoped to the current repository; current user input has priority. Agent streaming distinguishes planning, tool execution, observations, replanning, and validated final output. The full offline suite passed with six Windows link-permission skips; whole-package coverage remains below the 90% line and 85% branch goals. Live host acceptance is blocked because PostgreSQL, Qwen Chat, and Embedding had no local listener. See [the Phase 3 acceptance report](AGENTFORGE_PHASE3_ACCEPTANCE_REPORT_20261106.md) for evidence and limits.

## License

No license file is included in this repository snapshot. Add a license only after choosing and approving the project’s distribution terms.


### Long conversation summaries

The 1.1 branch keeps original messages in PostgreSQL and uses a bounded rolling summary for older turns. A source-linked ledger helps retain important statements from user messages; it does not establish that a user's statement is true of the code. If a protected statement does not fit or a summary update fails, the old checkpoint is retained. Summary and context limits are UTF-8 byte estimates, not exact model tokens. The latest summary repair is offline-tested only; its real Qwen acceptance is still pending. See `AF012_CONTEXT_RELIABILITY_REPORT_20261101.md`.

### 1.1 development acceptance note

The current 1.1 development branch uses a bounded conversation plan with a 5% byte reserve. This is not a model-token guarantee. The latest reserve and prompt changes have offline tests only; full host acceptance and some memory/context behaviors remain pending. See `AF011_1_HARDENING_ACCEPTANCE_20261101.md`.

### Iterative Agent execution (development branch)

The `agent` command has a shared execution engine that can discover registered tools, load only selected tool schemas, validate a bounded dependency plan, execute authorized tools, pass observations back to the planner, and resume a saved execution when explicitly given its execution ID. The current built-in repository tools remain read-oriented except for the explicitly scoped index update. No shell, arbitrary code, SQL, or network tool is registered by default.

Example offline invocation:

```powershell
python -X utf8 -B -m agentforge agent --repo .\path\to\repository "Find the implementation and summarize it"
```

For Qwen, select `--provider llama-server` and provide the explicit loopback endpoint/model settings used by the local launcher. For DeepSeek, select `--provider deepseek` and pass `--allow-cloud-data` only after reviewing the prompt/data-sharing notice; the objective and read-only tool results can be sent to the cloud model. Saved executions require `--conversation-id` and `--execution-id`; `WAITING_APPROVAL` and `WAITING_USER_INPUT` have separate explicit resume options. See [the implementation report](AGENT_EXECUTION_REFACTOR_REPORT_20261106.md) for limits. The current result is offline-verified only; live Planner, PostgreSQL migration/restart, and native token streaming are not yet verified.
