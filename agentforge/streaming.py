"""Provider-neutral streaming events with a validation-before-publication gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from .errors import AgentForgeError


class StreamEventType(str, Enum):
    STARTED = "STARTED"
    PLANNER_STARTED = "PLANNER_STARTED"
    PLANNER_FAILED = "PLANNER_FAILED"
    PLAN_REJECTED = "PLAN_REJECTED"
    PLAN_READY = "PLAN_READY"
    TOOL_DISCOVERY = "TOOL_DISCOVERY"
    TOOL_CALL_STARTED = "TOOL_CALL_STARTED"
    TOOL_PROGRESS = "TOOL_PROGRESS"
    TOOL_CALL_COMPLETED = "TOOL_CALL_COMPLETED"
    TOOL_CALL_FAILED = "TOOL_CALL_FAILED"
    OBSERVATION_RECEIVED = "OBSERVATION_RECEIVED"
    REPLANNING = "REPLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_USER_INPUT = "WAITING_USER_INPUT"
    STATE_CHANGED = "STATE_CHANGED"
    RETRIEVAL_PROGRESS = "RETRIEVAL_PROGRESS"
    CONTENT_DELTA = "CONTENT_DELTA"
    FINAL_ANSWER = "FINAL_ANSWER"
    USAGE = "USAGE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class StreamingEvent:
    type: StreamEventType
    payload: dict = field(default_factory=dict)
    execution_id: str | None = None
    task_id: str | None = None
    sequence: int | None = None


class ConversationCancelled(AgentForgeError):
    """A conversation was cancelled at an application stage boundary."""


class StreamingProtocolError(AgentForgeError):
    """A provider's streamed deltas do not match its completed response."""


class StreamingEventSink(Protocol):
    def publish(self, event: StreamingEvent) -> None: ...


class StreamingManager:
    """Buffer provider deltas until the orchestrator validates the answer contract."""

    MAX_BUFFER_CHARS = 200_000

    def __init__(self, *, on_event=None, on_content=None, enabled=False, cancel_event=None):
        self.on_event = on_event
        self.on_content = on_content
        self.requested_enabled = bool(enabled)
        self.enabled = bool(enabled)
        self.native_streaming = bool(enabled)
        self.status = "requested" if enabled else "disabled"
        self.cancel_event = cancel_event
        self._buffer = []
        self._buffer_chars = 0
        self._defer_content_until_validated = False
        self._deferred_streaming_status = None
        self._state = "new"
        self.execution_id = None
        self._event_sequence = 0
        self._last_agent_sequence = 0

    def configure_provider_streaming(self, capability: bool | None):
        """Use buffered answers when native provider streaming is unavailable or unknown."""
        self._require_active()
        if not self.requested_enabled:
            self.enabled = False
            self.native_streaming = False
            self.status = "disabled"
            return
        if capability is True:
            self.enabled = True
            self.native_streaming = True
            self.status = "streaming"
        else:
            self.enabled = False
            self.native_streaming = False
            self.status = "not_supported" if capability is False else "not_measured"
        self._emit(StreamEventType.STATE_CHANGED, {"streaming_status": self.status})

    def begin_buffered_fallback(self, error_code: str):
        """Invalidate any partial draft before the single non-streaming recovery request."""
        self._require_active()
        if not self.native_streaming or self.status != "streaming":
            raise RuntimeError("Buffered fallback requires an active native stream.")
        self._buffer.clear()
        self._buffer_chars = 0
        self.enabled = False
        self.native_streaming = False
        self.status = "degraded_to_buffered"
        self._emit(StreamEventType.STATE_CHANGED, {
            "streaming_status": self.status,
            "stream_error_code": _safe_code(error_code),
        })

    def defer_content_until_validated(self):
        """Keep General/no-evidence provider deltas internal until the contract passes."""
        self._require_active()
        if not self.requested_enabled:
            return
        self._defer_content_until_validated = True
        if self.enabled and self.status == "streaming":
            self._deferred_streaming_status = "buffered_for_contract"
            self._emit(StreamEventType.STATE_CHANGED, {
                "streaming_status": self._deferred_streaming_status,
                "reason_code": "general_answer_contract_validation",
            })

    def discard_deferred_draft(self):
        """Clear a no-evidence draft before the single bounded answer correction."""
        self._require_active()
        if not self._defer_content_until_validated:
            raise RuntimeError("Only a deferred answer draft can be discarded.")
        self._buffer.clear()
        self._buffer_chars = 0

    def start(self):
        if self._state != "new":
            raise RuntimeError("Streaming lifecycle has already started.")
        self._state = "started"
        self._emit(StreamEventType.STARTED)

    def bind_execution(self, execution_id: str):
        if not isinstance(execution_id, str) or not execution_id.strip() or len(execution_id) > 64:
            raise ValueError("Streaming execution ID is invalid.")
        if self.execution_id is not None and self.execution_id != execution_id:
            raise ValueError("Streaming manager is already bound to another execution.")
        self.execution_id = execution_id

    def publish_agent_event(self, event: dict):
        """Accept a bounded execution event while preserving task identity and ordering."""
        self._require_active()
        if not isinstance(event, dict):
            raise ValueError("Agent stream event must be an object.")
        try:
            event_type = StreamEventType(event.get("type"))
        except (ValueError, TypeError):
            raise ValueError("Unknown Agent stream event type.") from None
        execution_id = event.get("execution_id")
        task_id = event.get("task_id")
        sequence = event.get("sequence")
        if (not isinstance(execution_id, str) or not execution_id.strip() or len(execution_id) > 64
                or (task_id is not None and (not isinstance(task_id, str) or len(task_id) > 64))
                or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= self._last_agent_sequence):
            raise ValueError("Agent stream event identity or sequence is invalid.")
        if self.execution_id is not None and self.execution_id != execution_id:
            raise ValueError("Agent stream event belongs to another execution.")
        if self.execution_id is None:
            self.execution_id = execution_id
        self._last_agent_sequence = sequence
        payload = {key: value for key, value in event.items()
                   if key not in {"type", "execution_id", "task_id", "sequence"}}
        payload["agent_sequence"] = sequence
        sequence = max(self._event_sequence + 1, sequence)
        self._emit(event_type, payload, execution_id=execution_id,
                   task_id=task_id, sequence=sequence)

    def plan_ready(self, plan):
        self._require_active()
        self._state = "planned"
        self._emit(StreamEventType.PLAN_READY, {
            "mode": plan.mode.value,
            "task_count": len(plan.tasks),
            "planner_source": plan.planner_source,
            "requires_agent_execution": bool(getattr(plan, "requires_agent_execution", False)),
        })

    def retrieval_progress(self, completed: int, total: int):
        self._require_active()
        if (isinstance(completed, bool) or not isinstance(completed, int)
                or isinstance(total, bool) or not isinstance(total, int)
                or not 0 <= completed <= total or total < 1):
            raise ValueError("Invalid retrieval progress values.")
        self._emit(StreamEventType.RETRIEVAL_PROGRESS, {"completed": completed, "total": total})

    def capture_provider_delta(self, text: str):
        """Buffer provider deltas and, when enabled, publish them as unverified draft text."""
        self._require_active()
        if not isinstance(text, str):
            raise ValueError("Provider stream content must be text.")
        # Empty SSE content deltas are legal (for example, when a provider
        # sends a role-only chunk). They are not user-visible output and must
        # not make an otherwise valid final completion fail the equality gate.
        if not text:
            return
        next_size = self._buffer_chars + len(text)
        if next_size > self.MAX_BUFFER_CHARS:
            raise ValueError("Provider stream exceeded the bounded response buffer.")
        self._buffer.append(text)
        self._buffer_chars = next_size
        if self.enabled and text and not self._defer_content_until_validated:
            self._emit(StreamEventType.CONTENT_DELTA, {
                "characters": len(text), "draft": True, "contract_validated": False,
            })
            if callable(self.on_content):
                self.on_content(text)

    def validate_provider_answer(self, answer: str):
        """Fail closed if streamed text differs from the completion to be validated."""
        if not isinstance(answer, str):
            raise ValueError("Provider completion must be text.")
        if self.enabled and self._buffer and "".join(self._buffer) != answer:
            raise StreamingProtocolError(
                "Provider stream content did not match its completed response."
            )

    def check_cancelled(self):
        event = self.cancel_event
        if event is not None and callable(getattr(event, "is_set", None)) and event.is_set():
            raise ConversationCancelled("Conversation was cancelled before completion.")

    def complete_validated(self, answer: str, *, usage=None, provider_answer=None):
        self._require_active()
        self.check_cancelled()
        if not isinstance(answer, str):
            raise ValueError("Validated answer must be text.")
        comparison = provider_answer if self._defer_content_until_validated else answer
        if comparison is None:
            comparison = answer
        self.validate_provider_answer(comparison)
        self._emit(StreamEventType.FINAL_ANSWER, {
            "characters": len(answer), "contract_validated": True,
        })
        if usage is not None:
            self._emit(StreamEventType.USAGE, {"usage": _safe_usage(usage)})
        self._buffer.clear()
        self._buffer_chars = 0
        self._state = "completed"
        if self.status == "streaming" and self._deferred_streaming_status is not None:
            self.status = self._deferred_streaming_status
        self._emit(StreamEventType.COMPLETED)

    def suspend(self, state: str):
        """End this request at a persisted approval or user-input checkpoint."""
        self._require_active()
        event_type = {
            "waiting_approval": StreamEventType.WAITING_APPROVAL,
            "waiting_user_input": StreamEventType.WAITING_USER_INPUT,
        }.get(state)
        if event_type is None:
            raise ValueError("Only an approval or user-input checkpoint can suspend a stream.")
        self._state = "suspended"
        self._emit(event_type, {"state": state})

    def fail(self, code: str):
        if self._state in {"completed", "failed", "cancelled"}:
            return
        self._buffer.clear()
        self._buffer_chars = 0
        self._state = "failed"
        self._emit(StreamEventType.FAILED, {"error_code": _safe_code(code)})

    def cancel(self):
        if self._state in {"completed", "failed", "cancelled"}:
            return
        self._buffer.clear()
        self._buffer_chars = 0
        self._state = "cancelled"
        self._emit(StreamEventType.CANCELLED)

    def _require_active(self):
        if self._state in {"new", "completed", "failed", "cancelled", "suspended"}:
            raise RuntimeError("Streaming event occurred outside an active conversation.")

    def _emit(self, event_type, payload=None, *, execution_id=None, task_id=None, sequence=None):
        if self.on_event is None:
            return
        if execution_id is None:
            execution_id = self.execution_id
        if sequence is None:
            self._event_sequence += 1
            sequence = self._event_sequence
        else:
            self._event_sequence = max(self._event_sequence, sequence)
        event = StreamingEvent(event_type, payload or {}, execution_id, task_id, sequence)
        publish = getattr(self.on_event, "publish", None)
        if callable(publish):
            publish(event)
        elif callable(self.on_event):
            self.on_event(event)
        else:
            raise ValueError("Streaming event sink must be callable or implement publish().")


def _safe_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens",
                       "reasoning_tokens", "prompt_cache_hit_tokens"}
            and isinstance(item, int) and not isinstance(item, bool) and item >= 0}


def _safe_code(value):
    if not isinstance(value, str):
        return "request_failed"
    return value[:64] if value.replace("_", "").isalnum() else "request_failed"


__all__ = ["ConversationCancelled", "StreamEventType", "StreamingEvent",
           "StreamingEventSink", "StreamingManager", "StreamingProtocolError"]
