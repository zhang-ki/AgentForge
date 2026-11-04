"""Runtime model metadata for explicit local and DeepSeek model selection."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import http.client
import json as _json

from .af012_runtime import DATA_ROOT


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

    @property
    def key(self) -> str:
        return "qwen-local" if self.provider_id == "llama-server" else self.model_id


LOCAL_QWEN = ModelDescriptor(
    provider_id="llama-server", model_id="qwen3.5-4b", display_name="Qwen Local",
    model_type="local", context_window=4096, max_output_tokens=512,
    tokenizer_id=None, supports_streaming=None, supports_tool_calling=False,
    supports_thinking=False, input_modalities=("text",), output_modalities=("text",),
    availability="CONFIGURED",
)
OFFLINE_PROVIDER = ModelDescriptor(
    provider_id="offline", model_id="offline", display_name="Offline Evidence",
    model_type="offline", context_window=None, max_output_tokens=None,
    availability="CONFIGURED",
)


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"DeepSeek model metadata has an invalid {name}.")
    return value


def parse_deepseek_models(payload: Any) -> tuple[ModelDescriptor, ...]:
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

    def refresh_deepseek(self, client) -> tuple[ModelDescriptor, ...]:
        payload = client.list_models()
        models = parse_deepseek_models(payload)
        saved_at = self.now().astimezone(timezone.utc)
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
    if any(value not in {"low", "high", "max"} for value in descriptor.supported_efforts):
        raise ValueError("Invalid cached reasoning effort.")
    if descriptor.default_effort is not None and descriptor.default_effort not in descriptor.supported_efforts:
        raise ValueError("Invalid cached default reasoning effort.")
    if any(value is not None and not isinstance(value, bool) for value in (
            descriptor.supports_streaming, descriptor.supports_tool_calling, descriptor.supports_thinking)):
        raise ValueError("Invalid cached model capability.")
    if descriptor.availability != "UNKNOWN":
        raise ValueError("Cached model availability cannot be trusted as a connection status.")


def check_qwen_identity(endpoint: str, api_key: str, expected_model: str, *, timeout: float = 3.0) -> dict:
    """Verify the explicitly configured loopback Qwen service and model alias."""
    from .providers.llama_server import LlamaServerClient
    client = LlamaServerClient(endpoint, api_key=api_key, connect_timeout=min(timeout, 2.0),
                               read_timeout=max(5.0, timeout), total_timeout=max(10.0, timeout))
    connection = http.client.HTTPConnection(client.host, client.port, timeout=timeout)
    try:
        connection.request("GET", "/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read(8193)
        if response.status != 200 or len(raw) > 8192:
            raise ValueError("Local Qwen health check failed; model selection was not changed.")
        try:
            health = _json.loads(raw.decode("utf-8"))
        except (UnicodeError, _json.JSONDecodeError):
            raise ValueError("Local Qwen health response was invalid; model selection was not changed.") from None
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise ValueError("Local Qwen model is not ready; model selection was not changed.")
        connection.close()
        connection = http.client.HTTPConnection(client.host, client.port, timeout=timeout)
        connection.request("GET", "/v1/models", headers={"Accept": "application/json",
                            "Authorization": "Bearer " + api_key})
        response = connection.getresponse()
        raw = response.read(65_537)
        if response.status != 200 or len(raw) > 65_536:
            raise ValueError("Local Qwen model identity could not be confirmed; model selection was not changed.")
        try:
            payload = _json.loads(raw.decode("utf-8"))
        except (UnicodeError, _json.JSONDecodeError):
            raise ValueError("Local Qwen model identity response was invalid; model selection was not changed.") from None
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not any(isinstance(item, dict) and item.get("id") == expected_model for item in entries):
            raise ValueError("The running local service does not expose the expected Qwen model alias.")
        return {"health": "ready", "model_id": expected_model}
    except (OSError, http.client.HTTPException):
        raise ValueError("Local Qwen readiness or model identity request failed; selection was not changed.") from None
    finally:
        connection.close()
