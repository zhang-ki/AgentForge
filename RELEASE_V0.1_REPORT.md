# AgentForge DeepSeek Agent v0.1 — Release Preparation Report

**Release decision: `RELEASE_CANDIDATE`**  
**Worktree:** `feature/1.1-core` at `09bc079ad17b1a2560199eadd679b72c7c779224` (uncommitted changes retained)

## Decision

The declared v0.1 scope is a **release candidate**. A post-fix ordinary Windows PowerShell run passed the targeted real DeepSeek Flash `multi_turn` scenario: 12/12 Chat requests succeeded, with no failed requests or retries. All three turns reloaded the same session history (2, 4, and 6 persisted messages); the tool turn executed the authorized synthetic read tool, received an Observation, replanned successfully, and completed. The Agent Planner rejected two malformed JSON responses; valid subsequent plans completed the scenario. No invalid plan was executed.

This was a targeted run, not a full 15-scenario PASS. The runner's overall status is `PARTIAL` / `targeted_scenario_only`; all unselected scenarios are `NOT_MEASURED` in that run. The Answer Contract was observed for every turn, but its status was `limited` each time and does not prove semantic entailment. The earlier full matrix remains 10 PASS / 5 FAIL; four failed capabilities are excluded from v0.1, and the prior multi-turn failure has now been rechecked successfully against the current worktree. The new-session consent/default path is offline-tested but was not separately exercised through the interactive launcher on the host.

## Project and runtime shape

The current interactive path is:

```text
Windows launcher → conversation console → CLI application services
 → ConversationOrchestrator → LLM Planner / repository retrieval
 → plan validation → AgentExecutionEngine → ToolRegistry tools
 → observations / bounded replanning → Answer Contract → PostgreSQL Session
```

Project SQLite remains the authoritative local code index. PostgreSQL stores conversations and approved Memory. Chroma is an optional, rebuildable vector candidate store. DeepSeek Flash is the default for newly created interactive sessions only after explicit per-session consent; a user can explicitly select local Qwen when configured. Existing sessions keep their saved model. No silent provider fallback is used.

## Changes in this preparation

- `agentforge/agent_execution.py`: the existing single response-protocol correction now handles `invalid_json` as well as schema/dependency errors. It supplies the diagnostic category and safe validation details, never reuses malformed model text, reruns complete parsing and validation, and stops after one failed correction. Tool authorization, schemas, dependencies, IDs, and execution rules are unchanged.
- `tests/test_agent_execution.py`: positive and negative regressions verify corrected finish succeeds, a second malformed response fails closed, and no new invalid tool call executes.
- `agentforge/conversation_console.py`: newly created conversations select DeepSeek Flash after an explicit cloud-data consent. Missing credentials or declined consent never lead to a cloud request. Local Qwen requires an explicit local choice and a successful configured local health check. `/model default` selects DeepSeek; `/model qwen-local` selects local Qwen.
- `launchers/start-agentforge.ps1`: makes the Qwen key optional for the DeepSeek-first console while preserving an inherited Qwen environment value if no DPAPI key file exists. It does not start model or PostgreSQL services.
- `tests/test_conversation_console.py`: verifies consent, cancellation, missing credentials, explicit Qwen choice, and unchanged conversation/model binding behavior.
- README and Architecture documentation now describe the DeepSeek-first, consent-gated interactive path.

## Verification in this preparation

- Focused regression: **107/107 passed** (`test_agent_execution`, `test_conversation_console`).
- Core release suite, forward order: **443/443 passed**, zero failures/errors/skips.
- Same core release suite, reverse order: **443/443 passed**, zero failures/errors/skips.
- In-memory Python compile check: **151 source/test files passed**. Intentionally malformed parser fixtures under `tests/fixtures` were excluded from compilation.
- Windows PowerShell AST parse for `launchers/start-agentforge.ps1`: **PASS**.
- `git diff --check`: **PASS**; Git printed only existing LF/CRLF conversion warnings.
- The release-preparation checks themselves made no network or database calls. A subsequent user-authorized ordinary Windows PowerShell host run is recorded below; no service was started or stopped and only disposable/synthetic resources were used.
- No full regression or coverage measurement was run. The latest accepted full offline baseline remains the Phase 4.4.4.4 record: 957 run, 951 passed, 0 failed/errors, 6 Windows link-permission skips. Accepted coverage exception remains 87.7462% line and 79.6241% branch across 80 production modules.

## Real-host evidence, kept by run

### Full 15-scenario historical run

Source: `AF042_UNIFIED_AGENT_HOST_RESULT_61a51a1240cf4630bee17e328ad71343.json` and matching report.

- PASS: `general_direct`, `repository_auto`, `single_tool`, `multi_tool_dependency`, `observation_replanning`, `tool_failure_recovery`, `long_answer`, `session_process_restore`, `tool_timeout`, `tool_cancel`.
- FAIL: `hybrid_knowledge`, `approval_pause_resume`, `long_tool_output`, `multi_turn`, `streaming_lifecycle`.
- DeepSeek Flash: 75/75 Chat requests succeeded, 0 failed, 0 retries.
- Temporary database, Chroma directory, and synthetic repository were reported removed.

### Latest targeted real multi-turn run before this preparation's fix

Source: `AF042_UNIFIED_AGENT_HOST_RESULT_20d013ef82cf4e3eba1b14524ea0122d.json` and matching report.

- DeepSeek model discovery passed; 9/9 Chat requests succeeded, 0 failed, 0 retries.
- First-turn data persisted; next-turn history reloaded; the Conversation Planner's one bounded correction passed; the Agent Engine executed one authorized synthetic read tool and received one Observation.
- Observation-after replanning failed strict parsing with `invalid_json` (21 response characters, `finish_reason=stop`). No invalid plan executed. Final task answer and Answer Contract were not reached.
- Temporary database, Chroma, and synthetic repository were reported removed.

These are historical facts, not post-fix acceptance. No real run against the current modified worktree has been performed.


### Latest authorized real host run (targeted)

Source: `AF042_UNIFIED_AGENT_HOST_RESULT_0ddc7ed43968491abc918e28d14dc70f.json` and matching report.

- Runner status: **PARTIAL**, scope `targeted_scenario_only`; selected `multi_turn`: **PASS**. The full 15-scenario matrix was not run.
- DeepSeek Flash capability metadata was refreshed for the run; the reported context window was 1,048,576 tokens. Chat requests: 12 attempted / 12 sent / 12 succeeded / 0 failed / 0 retries.
- Three turns used one session. Persisted message counts were 2 → 4 → 6, with history reloaded each turn. Turn 2 completed `read_synthetic_fact`, received one Observation, and completed one successful replan. All seven scenario assertions passed.
- Two Agent Planner replies were rejected as `invalid_json`; the subsequent valid plans allowed execution to complete. The Conversation Planner also made one bounded correction for `intent_task_mismatch` and passed final validation.
- Usage across 12 requests: 6,326 prompt tokens + 719 completion tokens = 7,045 total.
- Answer Contract was observed on all turns; each status was `limited`, with no contract failure code. This is not a semantic-entailment proof.
- Temporary PostgreSQL database, Chroma directory, and synthetic repository were reported removed. The run's report and JSON SHA-256 values are stored in `RELEASE_V0.1_ACCEPTANCE.json`.

## Supported v0.1 scope decision

The intended v0.1 scope is DeepSeek Flash General and Repository Q&A, Auto planning, local repository indexing/retrieval, read-only tools already registered and authorized, bounded tool execution, basic timeout/cancellation, Answer Contract, and PostgreSQL-backed session persistence. Historical host runs support General, Repository, read-only tools, and execution behavior; the current-worktree targeted multi-turn host run now passes. Evidence remains split by run, not combined into a synthetic full-matrix result.

Hybrid's complete response format, write/approval workflows, automatic long-result page consumption, and Provider-native token streaming are excluded from v0.1. Agent multi-turn replanning after malformed JSON passed the targeted current-worktree host scenario after bounded correction. Hybrid formatting, write/approval workflows, automatic long-result paging, and streaming are excluded from v0.1.

## Candidate scope and remaining checks

No blocking failure remains within the declared v0.1 scope, so the worktree is marked **RELEASE_CANDIDATE** for final review. This is not a claim that the full 15-scenario matrix passed. The full matrix was not rerun; Hybrid, protected-write approval, automatic long-result paging, and streaming remain excluded. Qwen host acceptance remains unmeasured.

The DeepSeek new-session consent/default flow has offline tests, but the interactive launcher was not separately exercised in the latest host run. A manual first-launch smoke check is recommended before broad distribution. The package metadata still says version `1.0.0` while the bundle label is `v0.1`; this is disclosed in limitations. No Git commit or push was made.

### Startup-path clarification

The user-reported missing `llama-server.exe` path is from the optional local Qwen launch path. The DeepSeek release entry point is `Start-AgentForge.cmd`, which does not start Qwen or Embedding. The package intentionally contains no local model runtime/weights. README and Quick Start now distinguish the DeepSeek entry point from `Start-Qwen.cmd` and `Start-AgentForge-All.cmd`. This diagnosis does not claim a fresh application launch was executed in this environment.

### Package installation clarification

`AgentForge-v0.1.zip` is a clean source bundle, not a self-contained executable. It omits the Python virtual environment and local model assets by design. The operator must create the package-local virtual environment and install the persistence extra once. This machine has Python 3.12.7 available as `python` and an existing development virtual environment under `D:\agent\AgentForge\.agentforge`; continuing to use the development checkout does not require creating a second environment. The Windows `py` launcher is not installed, so Quick Start now uses `python -m venv`.
