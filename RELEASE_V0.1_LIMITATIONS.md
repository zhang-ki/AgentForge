# AgentForge DeepSeek Agent v0.1 — Supported Scope and Limitations

## Release decision

**RELEASE_CANDIDATE.** The targeted current-worktree DeepSeek Flash `multi_turn` host scenario passed. The overall runner status is still PARTIAL because it intentionally ran one scenario only.

## Intended supported scope

- DeepSeek Flash conversation after explicit per-session cloud-data consent.
- General questions and local Repository Q&A through Auto planning.
- Local SQLite code indexing, symbol/keyword retrieval, and citation validation.
- Existing registered, authorized read-only tools, including bounded single- and multi-tool execution.
- Bounded observation/replanning, tool failure handling, timeout/cancellation controls, and Answer Contract validation.
- PostgreSQL-backed sessions, message persistence, and process restoration.
- Explicit Qwen Local selection where its local model, credential, and service are configured.

General, Repository, read-only tool execution, process restoration, timeout, cancellation, multi-tool dependencies, and failure fallback have historical host evidence in `AF042_UNIFIED_AGENT_HOST_RESULT_61a51a1240cf4630bee17e328ad71343.json`. The current worktree also passed a targeted real DeepSeek Flash multi-turn run (`AF042_UNIFIED_AGENT_HOST_RESULT_0ddc7ed43968491abc918e28d14dc70f.json`): 12/12 Chat requests succeeded; one tool call, one Observation, one successful replan, and session messages persisted/reloaded across three turns.

## Excluded or not verified for v0.1

- **Hybrid complete answer formatting:** excluded; the latest full host scenario failed.
- **Approval pause/resume for protected write operations:** excluded; not a supported write workflow in v0.1.
- **Automatic long-tool-result paging into the final answer:** excluded; the latest full host scenario failed. Oversized results must remain visibly bounded; do not treat a partial result as complete.
- **Provider-native token streaming:** not measured in the latest evidence. Application lifecycle events do not prove provider streaming support.
- **Agent multi-turn replan after malformed JSON:** targeted live run passed. Two malformed Agent Planner JSON responses were rejected and followed by valid plans; no invalid plan was executed. This verifies the tested synthetic case, not every model response.
- **Interactive new-session default/consent host flow:** offline behavior tests pass, but the latest targeted runner bypassed the interactive launcher. Run a manual first-launch smoke before broad distribution.
- **Qwen host acceptance in this release preparation:** not measured.
- **Vector retrieval in the latest general/repository host path:** not established by the latest targeted multi-tool run; symbol/keyword retrieval is supported separately from optional Chroma vectors.

## Startup clarification

The reported missing path `models\qwen3.5-4b\.runtime\llama-b11424\llama-server.exe` belongs to the optional local Qwen launcher. For DeepSeek-only use, start `Start-AgentForge.cmd`; do not start `Start-Qwen.cmd` or `Start-AgentForge-All.cmd` without separately supplied local model assets. The release bundle intentionally omits those binaries and weights.

## Safety boundaries

- No shell, arbitrary Python, unrestricted SQL, or general network tool is exposed by default.
- Tool discovery, authorization, argument schemas, dependencies, and Answer Contract validation remain enforced.
- Invalid plans and failed correction are rejected; malformed JSON is never mined for executable tool calls.
- Cloud data is sent only after explicit per-session consent. There is no silent Qwen/DeepSeek fallback.
- This release preparation did not start/stop services, access formal data, or make a model request.

## Known operational limitations

- PostgreSQL must be prepared and started by the operator; the AgentForge launcher only applies forward migrations and opens the console.
- The release bundle contains no Python virtual environment, PostgreSQL data, Chroma data, local model weights, or model server binaries.
- Exact full-regression and coverage values are historical Phase 4.4.4.4 measurements: 957 tests run, 951 passed, 0 failed/errors, 6 Windows link-permission skips; 87.7462% line and 79.6241% branch coverage across 80 production modules. The accepted coverage exception remains in effect; these numbers were not remeasured for this bundle.
- The project metadata still reports Python package version `1.0.0`; the release artifact folder is labeled `AgentForge-v0.1`. No tag or commit was created.

## Release candidate checks

- The tested core v0.1 scope is eligible for final review; the 15-scenario matrix is not claimed as passed.
- Before broad distribution, manually launch the packaged app and confirm first-session consent/default selection and an ordinary General or Repository question.
- The targeted run's Answer Contract status was `limited` on each turn. Treat those answers as partial conclusions; citation or contract status alone does not prove semantic entailment.
- Release package metadata still reports Python package version `1.0.0`; bundle name is `AgentForge-v0.1`.
