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

The Token Budget Manager uses each selected model's discovered context and output limits to reserve output and a safety margin. It measures messages, protocol fields, tool schemas, and tool-call history. When an exact model tokenizer is unavailable, output is explicitly marked `estimated`; UTF-8 bytes are not called tokens. API usage shown after an answer describes that request only, not the model's remaining context capacity. The existing 3,500 UTF-8-byte conversation planner remains a separate safeguard.

## Errors and recovery

- A failed Qwen identity check keeps the prior session binding.
- An invalid or missing DeepSeek credential prevents discovery and chat calls; there is no Qwen fallback for a selected DeepSeek session.
- HTTP 400/401/402/429 and service/network/timeouts are returned as bounded, secret-safe errors. Requests are not retried automatically.
- Unsupported Thinking, effort, or streaming options are rejected before sending.
- Return to local Qwen with `/model default`; the conversation history, summary, memory, and Chroma index are not reset by model switching.
- New conversations default to Qwen. Existing conversation bindings are restored from PostgreSQL by the additive `af012mm_004` migration.

## Current acceptance status

Offline contract tests cover Registry parsing/cache behavior, credential handling, model binding, capability checks, the DeepSeek protocol/SSE path, citations, read-only tools, and dynamic budget calculations. They do not prove a live DeepSeek account, billing availability, a live Qwen response, or a fresh PostgreSQL/Chroma restart. The bounded host entry point is `AF012_MULTIMODEL_HOST_ACCEPTANCE.ps1`; it uses only synthetic source and requires an explicit confirmation before any cloud request. It is a smoke test and does not replace the full multi-model/RAG acceptance suite.
