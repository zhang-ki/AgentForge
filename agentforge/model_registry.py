"""Runtime model metadata for explicit local and DeepSeek model selection."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .af012_runtime import DATA_ROOT
from .model_types import (
    LOCAL_QWEN, OFFLINE_PROVIDER, ModelDescriptor,
)



def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"DeepSeek model metadata has an invalid {name}.")
    return value


def parse_deepseek_models(payload: Any, *, capability_verified_at: str | None = None) -> tuple[ModelDescriptor, ...]:
    """Parse the official `/models` response; absent capabilities remain unknown."""
    if not isinstance(payload, dict) or payload.get("object") != "list" or not isinstance(payload.get("data"), list):
        raise ValueError("DeepSeek model discovery returned an invalid list response.")
    if len(payload["data"]) > 200:
        raise ValueError("DeepSeek model discovery exceeded the bounded model count.")
    descriptors = []
    seen: set[str] = set()
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise ValueError("DeepSeek model discovery returned an invalid model entry.")
        model_id = item.get("id")
        if (not isinstance(model_id, str) or not model_id.strip() or model_id != model_id.strip() or len(model_id) > 256
                or any(ord(ch) < 32 for ch in model_id) or model_id in seen):
            raise ValueError("DeepSeek model discovery returned a missing or duplicate model ID.")
        seen.add(model_id)
        name = item.get("name")
        owner = item.get("owned_by")
        if name is not None and (not isinstance(name, str) or len(name) > 256):
            raise ValueError("DeepSeek model display name is invalid.")
        if owner is not None and (not isinstance(owner, str) or len(owner) > 128):
            raise ValueError("DeepSeek model owner is invalid.")
        effort = item.get("effort")
        if effort is not None and not isinstance(effort, dict):
            raise ValueError("DeepSeek effort metadata is invalid.")
        levels = [] if effort is None else effort.get("supported_levels", [])
        if not isinstance(levels, list) or any(level not in {"low", "high", "max"} for level in levels):
            raise ValueError("DeepSeek supported effort levels are invalid.")
        default_effort = effort.get("default_level") if effort else None
        if default_effort is not None and default_effort not in levels:
            raise ValueError("DeepSeek default effort is not in the supported levels.")
        inputs, outputs = item.get("input_modalities", []), item.get("output_modalities", [])
        if (not isinstance(inputs, list) or any(not isinstance(v, str) for v in inputs)
                or not isinstance(outputs, list) or any(not isinstance(v, str) for v in outputs)):
            raise ValueError("DeepSeek modality metadata is invalid.")
        caps = item.get("api_capabilities")
        if caps is not None and not isinstance(caps, dict):
            raise ValueError("DeepSeek API capability metadata is invalid.")
        chat = caps.get("chat_completions", {}) if caps else {}
        if chat is not None and not isinstance(chat, dict):
            raise ValueError("DeepSeek Chat Completions capability metadata is invalid.")
        for flag in ("streaming", "tool_calling"):
            if isinstance(chat, dict) and chat.get(flag) is not None and not isinstance(chat[flag], bool):
                raise ValueError("DeepSeek API capability metadata contains an invalid boolean.")
        tokenizer_id = item.get("tokenizer_id")
        if tokenizer_id is not None and (not isinstance(tokenizer_id, str) or not tokenizer_id.strip()
                                         or len(tokenizer_id) > 256):
            raise ValueError("DeepSeek tokenizer metadata is invalid.")
        output_includes_reasoning = item.get("output_limit_includes_reasoning", True)
        if output_includes_reasoning is not None and not isinstance(output_includes_reasoning, bool):
            raise ValueError("DeepSeek output/reasoning limit metadata is invalid.")
        descriptors.append(ModelDescriptor(
            provider_id="deepseek", model_id=model_id, display_name=name or model_id,
            model_type="cloud", context_window=_optional_positive_int(item.get("context_window"), "context_window"),
            max_output_tokens=_optional_positive_int(item.get("max_output_tokens"), "max_output_tokens"),
            tokenizer_id=tokenizer_id,
            supports_streaming=chat.get("streaming") if isinstance(chat, dict) and isinstance(chat.get("streaming"), bool) else None,
            supports_tool_calling=chat.get("tool_calling") if isinstance(chat, dict) and isinstance(chat.get("tool_calling"), bool) else None,
            supports_thinking=bool(levels) if effort is not None else None,
            supported_efforts=tuple(levels), default_effort=default_effort,
            input_modalities=tuple(inputs), output_modalities=tuple(outputs),
            availability="UNKNOWN",
            max_input_tokens=_optional_positive_int(item.get("max_input_tokens"), "max_input_tokens"),
            reasoning_token_limit=_optional_positive_int(item.get("reasoning_token_limit"), "reasoning_token_limit"),
            output_limit_includes_reasoning=output_includes_reasoning,
            provider_max_output_tokens=_optional_positive_int(
                item.get("provider_max_output_tokens"), "provider_max_output_tokens"),
            adapter_max_output_tokens=None,
            capability_source=("DeepSeek official /models API response; Chat Completions API "
                               "documents max_tokens as the generated completion limit"),
            capability_verified_at=capability_verified_at,
        ))
    return tuple(descriptors)


class ModelRegistry:
    """Local Qwen plus explicitly refreshed, cached remote model metadata."""
    def __init__(self, cache_path: Path | None = None, *, now=None, max_age_seconds: int = 86_400):
        self.cache_path = Path(cache_path) if cache_path else DATA_ROOT / "model_registry" / "deepseek.json"
        self.now = now or (lambda: datetime.now(timezone.utc))
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or max_age_seconds < 0:
            raise ValueError("Model registry cache age is invalid.")
        self.max_age_seconds = max_age_seconds
        self._remote: tuple[ModelDescriptor, ...] = ()
        self.cache_status = "missing"
        self.cache_age_seconds: int | None = None
        self._load_cache()

    def all(self) -> tuple[ModelDescriptor, ...]:
        return (LOCAL_QWEN, OFFLINE_PROVIDER, *self._remote)

    def get(self, model_key: str) -> ModelDescriptor:
        for item in self.all():
            if item.key == model_key or item.model_id == model_key:
                return item
        raise ValueError("Unknown model ID. Refresh the model list before selecting a remote model.")

    def get_for_request(self, model_key: str) -> ModelDescriptor:
        """Return request-usable capabilities; stale cloud metadata stays display-only."""
        descriptor = self.get(model_key)
        if descriptor.provider_id != "deepseek":
            return descriptor
        if self.cache_status != "fresh":
            raise ValueError(
                "DeepSeek capability metadata is stale or unavailable; explicitly run /model refresh before use."
            )
        if (not isinstance(descriptor.capability_source, str)
                or not descriptor.capability_source.startswith("DeepSeek official /models API response")
                or not descriptor.capability_verified_at
                or descriptor.context_window is None
                or descriptor.max_output_tokens is None):
            raise ValueError(
                "DeepSeek model capabilities lack current verified provenance; refresh model information."
            )
        try:
            verified_at = datetime.fromisoformat(descriptor.capability_verified_at)
            if verified_at.tzinfo is None:
                raise ValueError
            age = (self.now().astimezone(timezone.utc)
                   - verified_at.astimezone(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "DeepSeek model capability verification timestamp is invalid; refresh model information."
            ) from None
        if age < -300 or age > self.max_age_seconds:
            raise ValueError(
                "DeepSeek model capabilities are stale; explicitly run /model refresh before use."
            )
        return descriptor

    def refresh_deepseek(self, client) -> tuple[ModelDescriptor, ...]:
        payload = client.list_models()
        saved_at = self.now().astimezone(timezone.utc)
        models = parse_deepseek_models(payload, capability_verified_at=saved_at.isoformat())
        record = {"version": 1, "saved_at": saved_at.isoformat(),
                  "models": [asdict(model) for model in models]}
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        if self.cache_path.is_symlink():
            raise ValueError("Model registry cache cannot be a symbolic link.")
        temp = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        try:
            temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.cache_path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        # Publish metadata in memory only after the cache write has committed.
        self._remote = models
        self.cache_status, self.cache_age_seconds = "fresh", 0
        return models

    def _load_cache(self) -> None:
        try:
            if self.cache_path.is_symlink() or self.cache_path.stat().st_size > 1_048_576:
                raise ValueError
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("models"), list):
                raise ValueError
            models = []
            for raw in data["models"]:
                if not isinstance(raw, dict):
                    raise ValueError
                raw = dict(raw)
                if raw.get("provider_id") == "deepseek":
                    # Older AgentForge caches persisted a generic 512-token
                    # adapter cap on DeepSeek descriptors. The current
                    # DeepSeek adapter has no such ceiling; retaining that
                    # obsolete field would clamp every preset despite fresh
                    # official model metadata.
                    raw["adapter_max_output_tokens"] = None
                raw["supported_efforts"] = tuple(raw.get("supported_efforts", ()))
                raw["input_modalities"] = tuple(raw.get("input_modalities", ()))
                raw["output_modalities"] = tuple(raw.get("output_modalities", ()))
                descriptor = ModelDescriptor(**raw)
                _validate_cached_descriptor(descriptor)
                models.append(descriptor)
            if len({item.model_id for item in models}) != len(models) or len(models) > 200:
                raise ValueError
            timestamp = datetime.fromisoformat(data["saved_at"])
            age = max(0, int((self.now().astimezone(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()))
            self._remote = tuple(models)
            self.cache_age_seconds = age
            self.cache_status = "fresh" if age <= self.max_age_seconds else "stale"
        except FileNotFoundError:
            self.cache_status = "missing"
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            self.cache_status = "invalid"
            self._remote = ()


def _validate_cached_descriptor(descriptor: ModelDescriptor) -> None:
    if (descriptor.provider_id != "deepseek" or descriptor.model_type != "cloud"
            or not isinstance(descriptor.model_id, str) or not descriptor.model_id.strip()
            or descriptor.model_id != descriptor.model_id.strip() or len(descriptor.model_id) > 256
            or not isinstance(descriptor.display_name, str) or not descriptor.display_name.strip()
            or len(descriptor.display_name) > 256):
        raise ValueError("Invalid cached model identity.")
    _optional_positive_int(descriptor.context_window, "context_window")
    _optional_positive_int(descriptor.max_output_tokens, "max_output_tokens")
    _optional_positive_int(descriptor.max_input_tokens, "max_input_tokens")
    _optional_positive_int(descriptor.reasoning_token_limit, "reasoning_token_limit")
    if (descriptor.output_limit_includes_reasoning is not None
            and not isinstance(descriptor.output_limit_includes_reasoning, bool)):
        raise ValueError("Invalid cached reasoning output-limit semantics.")
    _optional_positive_int(descriptor.provider_max_output_tokens, "provider_max_output_tokens")
    _optional_positive_int(descriptor.adapter_max_output_tokens, "adapter_max_output_tokens")
    for value in (descriptor.capability_source, descriptor.capability_verified_at):
        if value is not None and (not isinstance(value, str) or len(value) > 512):
            raise ValueError("Invalid cached capability provenance.")
    if descriptor.capability_verified_at is not None:
        try:
            datetime.fromisoformat(descriptor.capability_verified_at)
        except ValueError:
            raise ValueError("Invalid cached capability verification timestamp.") from None
    if any(value not in {"low", "high", "max"} for value in descriptor.supported_efforts):
        raise ValueError("Invalid cached reasoning effort.")
    if descriptor.default_effort is not None and descriptor.default_effort not in descriptor.supported_efforts:
        raise ValueError("Invalid cached default reasoning effort.")
    if any(value is not None and not isinstance(value, bool) for value in (
            descriptor.supports_streaming, descriptor.supports_tool_calling, descriptor.supports_thinking)):
        raise ValueError("Invalid cached model capability.")
    if descriptor.availability != "UNKNOWN":
        raise ValueError("Cached model availability cannot be trusted as a connection status.")
