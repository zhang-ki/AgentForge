"""Per-model context sizing with explicit tokenizer confidence."""
from __future__ import annotations

from dataclasses import dataclass
import math
import json
from typing import Callable, Iterable

from .model_registry import ModelDescriptor


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
    max_output_tokens: int
    estimate_kind: str


class TokenizerAdapter:
    """Use a configured exact tokenizer; otherwise over-estimate and label it."""
    def __init__(self, tokenizer: Callable[[str], int] | None = None):
        self.tokenizer = tokenizer

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
        return TokenCount(max(len(text), math.ceil(len(text.encode("utf-8")) / 2)), "estimated")

    def count_messages(self, messages: Iterable[dict]) -> TokenCount:
        total = 0
        exact = self.tokenizer is not None
        for message in messages:
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
        return TokenCount(total, "exact" if exact else "estimated")

    def count_request(self, messages: Iterable[dict], *, tools: Iterable[dict] = ()) -> TokenCount:
        """Count message protocol fields and tool schemas before a provider send.

        The serializer accounts for nested tool-call history and schemas. The
        tokenizer adapter still labels fallback counts as estimated; the model
        safety margin remains responsible for protocol/tokenizer variance.
        """
        message_cost = self.count_messages(messages)
        total = message_cost.count + 4  # top-level chat request framing
        tool_values = list(tools)
        if tool_values:
            tool_cost = self.count_text(json.dumps(tool_values, ensure_ascii=False, sort_keys=True,
                                                  separators=(",", ":")))
            total += tool_cost.count + 4
        return TokenCount(total, message_cost.method)


class TokenBudgetManager:
    def __init__(self, tokenizer: TokenizerAdapter | None = None, *, safety_ratio: float = 0.1,
                 minimum_safety_tokens: int = 128):
        if not 0 <= safety_ratio < 0.5:
            raise ValueError("safety_ratio must be in [0, 0.5).")
        if isinstance(minimum_safety_tokens, bool) or not isinstance(minimum_safety_tokens, int) or minimum_safety_tokens < 0:
            raise ValueError("minimum_safety_tokens must be a non-negative integer.")
        self.tokenizer = tokenizer or TokenizerAdapter()
        self.safety_ratio = float(safety_ratio)
        self.minimum_safety_tokens = minimum_safety_tokens

    def budget(self, model: ModelDescriptor, *, requested_output_tokens: int | None = None) -> TokenBudget:
        context_window = model.context_window
        model_output = model.max_output_tokens
        if context_window is None or model_output is None:
            raise ValueError("Selected model has no verified context or output limit; refresh its metadata first.")
        requested = None
        if requested_output_tokens is not None:
            if isinstance(requested_output_tokens, bool) or not isinstance(requested_output_tokens, int) or requested_output_tokens < 1:
                raise ValueError("Requested output tokens must be a positive integer.")
            requested = min(requested_output_tokens, model_output)
        # Keep the prompt reserve independent from the model's maximum output
        # capability. 512 is the product default reserve; explicit larger
        # responses reserve at least their requested amount.
        output = min(model_output, max(512, requested or 0))
        safety = max(self.minimum_safety_tokens, math.ceil(context_window * self.safety_ratio))
        input_budget = context_window - output - safety
        if input_budget < 1:
            raise ValueError("Model context window is too small after output and safety reserves.")
        return TokenBudget(context_window, input_budget, output, safety, model_output,
                           "exact" if self.tokenizer.tokenizer is not None else "estimated")

    def measure_messages(self, messages: Iterable[dict]) -> TokenCount:
        return self.tokenizer.count_messages(messages)

    def measure_request(self, messages: Iterable[dict], *, tools: Iterable[dict] = ()) -> TokenCount:
        return self.tokenizer.count_request(messages, tools=tools)

    def require_fit(self, messages: Iterable[dict], budget: TokenBudget) -> TokenCount:
        count = self.measure_messages(messages)
        if count.count > budget.input_budget:
            raise ValueError(f"Prompt is over the selected model input budget ({count.count}>{budget.input_budget}; {count.method}).")
        return count
