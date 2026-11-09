# Model Management (1.2 development branch)

This guide describes the current `feature/1.1-core` worktree, not the frozen MVP 1.0 release. Qwen Local remains the default. DeepSeek is a separate, explicitly selected cloud provider; AgentForge does not switch to it automatically when Qwen is unavailable.

## Console commands

| Command | Effect |
|---|---|
| `/modellist` | Show local defaults and cached DeepSeek model metadata. This command does not refresh the cloud model list. |
| `/model` | Show the active conversation's saved provider and model. |
| `/model refresh` | Make one explicit DeepSeek `/models` request and refresh local capability metadata. |
| `/model <id>` | Select an ID returned by `/modellist` or the last successful refresh. Cloud selection first displays a data notice and asks for `YES`. |
| `/model default` | Select Qwen Local after its loopback health and model identity have been checked. |
| `/thinking on` or `/thinking off` | Set Thinking for the current DeepSeek conversation only when the model reports support. |
| `/effort low`, `/effort high`, `/effort max` | Set a supported reasoning effort. An unsupported value is rejected before a provider request. |
| `/cloud-consent status` | Show whether this conversation has cloud-data consent. |
| `/cloud-consent revoke` | Revoke this conversation's cloud-data consent. Its provider binding is not silently changed. |
| `/model auth status` | Show only whether a credential is configured and whether it comes from the environment or DPAPI. |
| `/model auth set deepseek` | Prompt without echo and store the new key in a Windows-user-bound DPAPI file. |
| `/model auth remove deepseek` | Remove the local DPAPI credential. This does not clear an environment override. |

## Credential setup

Do not reuse a key previously disclosed in chat. Create a new key through the provider's official account interface, then enter it locally in AgentForge with `/model auth set deepseek`. The key is never a command argument and is not stored in PostgreSQL or project configuration. `DEEPSEEK_API_KEY` can be set in the current process as an override; status shows its source but never its value. Keep process environments and local DPAPI data out of screenshots, logs, and Git.

`/model refresh` is an outbound metadata request. It sends the credential to the fixed official HTTPS host but does not include the current repository or conversation. A subsequent cloud model selection is a separate choice: AgentForge explains that the current question, relevant source evidence, and necessary conversation history or approved memories may be sent, and requires explicit confirmation. Refusing the prompt leaves the current binding unchanged.

## Model and token boundaries

The Registry stores discovered model capabilities separately from connection status. Unknown metadata stays unknown. A cached list can be stale; it does not prove a model is currently connected or available to the account. Use `/model refresh` to request current metadata and then select the discovered ID.

`ContextBudgetManager` uses the selected model's verified context capability, the requested output size, and a safety margin. It then applies a model-specific `max_input_tokens` and any explicit user cap when those are present. The maximum output capability is only a ceiling; it is not automatically reserved for every request. The assembler counts messages, chat framing, tool schemas, and tool-call history. A model tokenizer is used only when its tokenizer ID and a local tokenizer implementation are available; otherwise diagnostics say `conservative_estimated`, not exact.

Agent planning has its own request budget because a structured multi-tool plan can be much larger than a normal chat answer. The current Agent planner allows up to 4096 output tokens on verified large-context models, further capped by the model descriptor and adapter. Qwen Local remains at 384 planner output tokens because its configured context window is 4096. Normal chat answers keep their separate configured budget (256 by default, bounded to 512); the Agent planner budget does not increase ordinary answers.

DeepSeek's official [`/models` metadata](https://api-docs.deepseek.com/api/list-models/) defines `context_window` as the combined input-plus-output capacity and `max_output_tokens` as the per-response output ceiling. Metadata fields may be omitted. AgentForge therefore leaves unknown values unset. A cached entry is request-usable only while the cache and the entry's own verification timestamp are fresh and its source is the official metadata response. A stale or provenance-free entry can be listed, but cannot configure a request; run `/model refresh` explicitly. The current local DeepSeek cache was stale and had no provenance at the time of this report, so its displayed capacities were not used for a request.

DeepSeek exposes a single `max_tokens` generation ceiling for the request. Its Thinking response may separately report `reasoning_tokens`; AgentForge records those apart from visible-answer tokens, but does not invent a per-model reasoning-token limit when the model metadata does not provide one. See the official [Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/) and [Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) documentation. API usage after a response describes that request only, not the model's remaining context capacity.

## Errors and recovery

- A failed Qwen identity check keeps the prior session binding.
- An invalid or missing DeepSeek credential prevents discovery and chat calls; there is no Qwen fallback for a selected DeepSeek session.
- HTTP 400/401/402/429 and service/network/timeouts are returned as bounded, secret-safe errors. Requests are not retried automatically.
- Unsupported Thinking, effort, or streaming options are rejected before sending.
- Return to local Qwen with `/model default`; the conversation history, summary, memory, and Chroma index are not reset by model switching.
- New conversations default to Qwen. Existing conversation bindings are restored from PostgreSQL by the additive `af012mm_004` migration.

## Current acceptance status

Offline contract tests cover Registry parsing/cache behavior, credential handling, model binding, capability checks, the DeepSeek protocol/SSE path, citations, read-only tools, and dynamic budget calculations. They do not prove a live DeepSeek account, billing availability, a live Qwen response, or a fresh PostgreSQL/Chroma restart. The bounded host entry point is `AF012_MULTIMODEL_HOST_ACCEPTANCE.ps1`; it uses only synthetic source and requires an explicit confirmation before any cloud request. It is a smoke test and does not replace the full multi-model/RAG acceptance suite.
