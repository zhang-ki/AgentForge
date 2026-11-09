"""Provider-neutral model capability metadata."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelDescriptor:
    provider_id: str
    model_id: str
    display_name: str
    model_type: str
    context_window: int | None
    max_output_tokens: int | None
    tokenizer_id: str | None = None
    supports_streaming: bool | None = None
    supports_tool_calling: bool | None = None
    supports_thinking: bool | None = None
    supported_efforts: tuple[str, ...] = ()
    default_effort: str | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    availability: str = "UNKNOWN"
    # Model limits are separate from limits imposed by an AgentForge adapter.
    # None means unknown; it must never be inferred from the model ID.
    max_input_tokens: int | None = None
    reasoning_token_limit: int | None = None
    # Whether the protocol's output token limit is shared by visible and
    # reasoning output. None means the selected protocol/model has not verified it.
    output_limit_includes_reasoning: bool | None = None
    provider_max_output_tokens: int | None = None
    # AgentForge's request-size guard is not a model or provider capability.
    adapter_max_output_tokens: int | None = None
    capability_source: str | None = None
    capability_verified_at: str | None = None

    @property
    def key(self) -> str:
        return "qwen-local" if self.provider_id == "llama-server" else self.model_id


LOCAL_QWEN = ModelDescriptor(
    provider_id="llama-server", model_id="qwen3.5-4b", display_name="Qwen Local",
    model_type="local", context_window=4096, max_output_tokens=None,
    tokenizer_id=None, supports_streaming=False, supports_tool_calling=False,
    supports_thinking=False, input_modalities=("text",), output_modalities=("text",),
    availability="CONFIGURED", adapter_max_output_tokens=512,
    capability_source=("launchers/start-qwen.ps1 --ctx-size 4096; AgentForge llama-server adapter "
                       "has no configured streaming, thinking or tool-calling path; request output ceiling 512"),
    capability_verified_at="2026-11-06",
)
OFFLINE_PROVIDER = ModelDescriptor(
    provider_id="offline", model_id="offline", display_name="Offline Evidence",
    model_type="offline", context_window=None, max_output_tokens=None,
    availability="CONFIGURED",
)
