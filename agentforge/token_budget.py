"""Per-model context sizing with explicit tokenizer confidence."""
from __future__ import annotations

from dataclasses import dataclass
import math
import json
from typing import Callable, Iterable, Protocol


class ModelBudgetProfile(Protocol):
    """Minimal immutable metadata required to calculate a request budget."""

    context_window: int | None
    max_output_tokens: int | None
    max_input_tokens: int | None
    reasoning_token_limit: int | None
    provider_max_output_tokens: int | None
    adapter_max_output_tokens: int | None
    output_limit_includes_reasoning: bool | None


OUTPUT_BUDGET_PRESETS = {
    "concise": 4_096,
    "standard": 16_384,
    "long": 65_536,
}


@dataclass(frozen=True)
class TokenCount:
    count: int
    method: str


@dataclass(frozen=True)
class TokenBudget:
    context_window: int
    input_budget: int
    output_reserve: int
    safety_margin: int
    max_output_tokens: int | None
    estimate_kind: str
    max_input_tokens: int | None = None
    reasoning_token_limit: int | None = None
    provider_max_output_tokens: int | None = None
    adapter_max_output_tokens: int | None = None
    capability_source: str | None = None
    capability_verified_at: str | None = None
    estimate_description: str = "conservative Unicode/UTF-8 fallback; not a model tokenizer"


@dataclass(frozen=True)
class OutputBudgetResolution:
    mode: str
    requested_tokens: int
    effective_tokens: int
    model_max_output_tokens: int | None
    provider_max_output_tokens: int | None
    adapter_max_output_tokens: int | None
    context_window: int
    safety_margin: int
    output_limit_includes_reasoning: bool | None
    source: str


class TokenizerAdapter:
    """Use local tokenizer functions when supplied; estimate chat framing explicitly."""
    def __init__(self, tokenizer: Callable[[str], int] | None = None,
                 request_counter: Callable[[Iterable[dict], Iterable[dict]], int] | None = None):
        self.tokenizer = tokenizer
        self.request_counter = request_counter

    def count_text(self, text: str) -> TokenCount:
        if not isinstance(text, str):
            raise ValueError("Tokenizer input must be text.")
        if self.tokenizer is not None:
            count = self.tokenizer(text)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("Tokenizer returned an invalid token count.")
            return TokenCount(count, "exact")
        # One token per Unicode character is deliberately conservative for
        # ordinary text; UTF-8 bytes are also considered for markup/code.
        return TokenCount(max(len(text), math.ceil(_utf8_size(text) / 2)), "estimated")

    def count_messages(self, messages: Iterable[dict]) -> TokenCount:
        values = list(messages)
        if self.request_counter is not None:
            count = self.request_counter(values, ())
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("Model request tokenizer returned an invalid token count.")
            return TokenCount(count, "exact")
        total = 0
        for message in values:
            if (not isinstance(message, dict) or not isinstance(message.get("role"), str)
                    or message.get("content") is not None and not isinstance(message.get("content"), str)):
                raise ValueError("Tokenizer messages must contain a role and text or null content.")
            role_cost = self.count_text(message["role"])
            content_cost = self.count_text(message.get("content") or "")
            total += role_cost.count + content_cost.count + 4  # conservative chat protocol framing
            extras = {key: value for key, value in message.items() if key not in {"role", "content"}}
            if extras:
                extra_cost = self.count_text(json.dumps(extras, ensure_ascii=False, sort_keys=True,
                                                        separators=(",", ":")))
                total += extra_cost.count
        method = "tokenizer_text_estimated_framing" if self.tokenizer is not None else "estimated"
        return TokenCount(total, method)

    def count_request(self, messages: Iterable[dict], *, tools: Iterable[dict] = ()) -> TokenCount:
        """Count message protocol fields and tool schemas before a provider send.

        The serializer accounts for nested tool-call history and schemas. The
        tokenizer adapter still labels fallback counts as estimated; the model
        safety margin remains responsible for protocol/tokenizer variance.
        """
        message_values, tool_values = list(messages), list(tools)
        if self.request_counter is not None:
            count = self.request_counter(message_values, tool_values)
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError("Model request tokenizer returned an invalid token count.")
            return TokenCount(count, "exact")
        message_cost = self.count_messages(message_values)
        total = message_cost.count + 4  # top-level chat request framing
        if tool_values:
            tool_cost = self.count_text(json.dumps(tool_values, ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":")))
            total += tool_cost.count + 4
        return TokenCount(total, message_cost.method)


class ContextBudgetManager:
    def __init__(self, tokenizer: TokenizerAdapter | None = None, *, safety_ratio: float = 0.1,
                 minimum_safety_tokens: int = 128, default_output_reserve: int = 512):
        if not 0 <= safety_ratio < 0.5:
            raise ValueError("safety_ratio must be in [0, 0.5).")
        if isinstance(minimum_safety_tokens, bool) or not isinstance(minimum_safety_tokens, int) or minimum_safety_tokens < 0:
            raise ValueError("minimum_safety_tokens must be a non-negative integer.")
        if (isinstance(default_output_reserve, bool) or not isinstance(default_output_reserve, int)
                or default_output_reserve < 1):
            raise ValueError("default_output_reserve must be a positive integer.")
        self.tokenizer = tokenizer or TokenizerAdapter()
        self._tokenizers: dict[str, TokenizerAdapter] = {}
        self.safety_ratio = float(safety_ratio)
        self.minimum_safety_tokens = minimum_safety_tokens
        self.default_output_reserve = default_output_reserve

    def register_tokenizer(self, tokenizer_id: str, tokenizer: Callable[[str], int], *,
                           request_counter: Callable[[Iterable[dict], Iterable[dict]], int] | None = None) -> None:
        """Register an already available tokenizer without downloading model assets."""
        if not isinstance(tokenizer_id, str) or not tokenizer_id.strip() or len(tokenizer_id) > 256:
            raise ValueError("Tokenizer ID is invalid.")
        if not callable(tokenizer):
            raise ValueError("Tokenizer must be callable.")
        if request_counter is not None and not callable(request_counter):
            raise ValueError("Model request counter must be callable.")
        self._tokenizers[tokenizer_id] = TokenizerAdapter(tokenizer, request_counter)

    def _adapter_for(self, model: ModelBudgetProfile | None = None) -> TokenizerAdapter:
        tokenizer_id = getattr(model, "tokenizer_id", None) if model is not None else None
        if isinstance(tokenizer_id, str):
            return self._tokenizers.get(tokenizer_id, self.tokenizer)
        return self.tokenizer

    def budget(self, model: ModelBudgetProfile, *, requested_output_tokens: int | None = None) -> TokenBudget:
        context_window = model.context_window
        model_output = model.max_output_tokens
        if context_window is None:
            raise ValueError("Selected model has no verified context window; refresh its capability metadata first.")
        if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window <= 0:
            raise ValueError("Selected model context window is invalid.")
        provider_output = getattr(model, "provider_max_output_tokens", None)
        adapter_output = getattr(model, "adapter_max_output_tokens", None)
        known_output_caps = [value for value in (model_output, provider_output, adapter_output) if value is not None]
        output_cap = min(known_output_caps) if known_output_caps else None
        for value in known_output_caps:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Selected model output capability is invalid.")
        requested = None
        if requested_output_tokens is not None:
            if isinstance(requested_output_tokens, bool) or not isinstance(requested_output_tokens, int) or requested_output_tokens < 1:
                raise ValueError("Requested output tokens must be a positive integer.")
            if output_cap is not None and requested_output_tokens > output_cap:
                raise ValueError("Requested output tokens exceed the selected model or provider limit.")
            requested = requested_output_tokens
        # Reserve the requested output, not the model's maximum capability.
        # The configured default applies only when a request supplies no size.
        output = requested if requested is not None else self.default_output_reserve
        if output_cap is not None and output > output_cap:
            output = output_cap
        safety = max(self.minimum_safety_tokens, math.ceil(context_window * self.safety_ratio))
        input_budget = context_window - output - safety
        model_input = getattr(model, "max_input_tokens", None)
        if model_input is not None:
            if isinstance(model_input, bool) or not isinstance(model_input, int) or model_input <= 0:
                raise ValueError("Selected model input capability is invalid.")
            input_budget = min(input_budget, model_input)
        if input_budget < 1:
            raise ValueError("Model context window is too small after output and safety reserves.")
        adapter = self._adapter_for(model)
        estimate_kind = ("exact" if adapter.request_counter is not None else
                         "tokenizer_text_estimated_framing" if adapter.tokenizer is not None else
                         "conservative_estimated")
        estimate_description = (
            "model-specific message and tool protocol counter"
            if adapter.request_counter is not None else
            "registered text tokenizer plus conservative chat/tool framing"
            if adapter.tokenizer is not None else
            "max Unicode character count and ceil(UTF-8 bytes/2); conservative proxy, not exact tokens"
        )
        return TokenBudget(context_window, input_budget, output, safety, output_cap,
                           estimate_kind, model_input, getattr(model, "reasoning_token_limit", None),
                           provider_output, adapter_output,
                           getattr(model, "capability_source", None),
                           getattr(model, "capability_verified_at", None), estimate_description)

    def resolve_output_budget(self, model: ModelBudgetProfile, *, mode: str = "standard",
                              explicit_output_tokens: int | None = None) -> OutputBudgetResolution:
        """Resolve one request's output allowance against verified model and context limits."""
        if mode not in {*OUTPUT_BUDGET_PRESETS, "max"}:
            raise ValueError("Output budget mode must be concise, standard, long or max.")
        context_window = getattr(model, "context_window", None)
        if isinstance(context_window, bool) or not isinstance(context_window, int) or context_window < 1:
            raise ValueError("Selected model has no verified context window.")
        caps = {}
        for field_name, label in (("max_output_tokens", "model"),
                                  ("provider_max_output_tokens", "provider"),
                                  ("adapter_max_output_tokens", "adapter")):
            value = getattr(model, field_name, None)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Selected model {label} output capability is invalid.")
            caps[label] = value
        if not caps:
            raise ValueError("Selected model has no verified output limit; refresh model capabilities first.")
        model_cap = getattr(model, "max_output_tokens", None)
        safety = max(self.minimum_safety_tokens, math.ceil(context_window * self.safety_ratio))
        context_cap = context_window - safety - 1
        if context_cap < 1:
            raise ValueError("Model context window cannot reserve input, output and safety space.")
        cap = min([*caps.values(), context_cap])
        if explicit_output_tokens is not None:
            if (isinstance(explicit_output_tokens, bool)
                    or not isinstance(explicit_output_tokens, int) or explicit_output_tokens < 1):
                raise ValueError("Explicit output token override must be a positive integer.")
            if explicit_output_tokens > cap:
                raise ValueError("Explicit output token override exceeds the selected model or context limit.")
            requested = explicit_output_tokens
            source = "explicit request override"
        elif mode == "max":
            requested = min(caps.values())
            source = ("verified model maximum" if model_cap is not None
                      else "verified provider/adapter maximum; model maximum unknown")
        else:
            requested = OUTPUT_BUDGET_PRESETS[mode]
            source = f"application {mode} preset"
        effective = min(requested, cap)
        if effective < 1:
            raise ValueError("Resolved output budget is empty after model and context limits.")
        return OutputBudgetResolution(
            mode, requested, effective, model_cap,
            getattr(model, "provider_max_output_tokens", None),
            getattr(model, "adapter_max_output_tokens", None), context_window, safety,
            getattr(model, "output_limit_includes_reasoning", None), source,
        )

    def measure_text(self, text: str, *, model: ModelBudgetProfile | None = None) -> TokenCount:
        return self._adapter_for(model).count_text(text)

    def measure_messages(self, messages: Iterable[dict], *, model: ModelBudgetProfile | None = None) -> TokenCount:
        return self._adapter_for(model).count_messages(messages)

    def measure_request(self, messages: Iterable[dict], *, tools: Iterable[dict] = (),
                        model: ModelBudgetProfile | None = None) -> TokenCount:
        return self._adapter_for(model).count_request(messages, tools=tools)

    def require_fit(self, messages: Iterable[dict], budget: TokenBudget, *,
                    model: ModelBudgetProfile | None = None) -> TokenCount:
        count = self.measure_messages(messages, model=model)
        if count.count > budget.input_budget:
            raise ValueError(f"Prompt is over the selected model input budget ({count.count}>{budget.input_budget}; {count.method}).")
        return count


def _utf8_size(text: str) -> int:
    """Count UTF-8 bytes without allocating a second copy of large prompt text."""
    total = 0
    for char in text:
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise ValueError("Tokenizer input contains invalid Unicode surrogate data.")
        total += (1 if codepoint <= 0x7F else 2 if codepoint <= 0x7FF
                  else 3 if codepoint <= 0xFFFF else 4)
    return total


# Kept as a narrow source-compatibility name for existing provider code.
TokenBudgetManager = ContextBudgetManager
