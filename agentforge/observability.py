"""Request-scoped, privacy-bounded lifecycle events for the interactive app."""

from __future__ import annotations

from contextvars import ContextVar, Token
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid


_CURRENT_TRACKER: ContextVar[RequestLifecycleTracker | None] = ContextVar(
    "agentforge_request_lifecycle", default=None,
)
_ACTIVE_STAGE: ContextVar[str | None] = ContextVar("agentforge_active_stage", default=None)
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,96}$")
_SAFE_OPERATIONS = {"initial_plan", "plan_correction", "search", "rerank_and_select",
                    "planner_started", "replanning"}
_STATUSES = {"PENDING", "RUNNING", "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED",
             "SKIPPED", "DEGRADED", "WAITING_APPROVAL", "WAITING_USER_INPUT"}
_EVENT_TYPES = {"STARTED", "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "SKIPPED",
                "DEGRADED", "OBSERVED", "SUMMARY", "RESPONSE_HEADERS", "FIRST_DELTA",
                "WAITING_APPROVAL", "WAITING_USER_INPUT", "FINAL_ANSWER"}
_SAFE_METADATA = {
    "provider", "model", "session_fingerprint", "mode", "route", "operation",
    "provider_request_kind", "status", "reason_code", "error_type", "error_code",
    "degraded_reason", "candidate_count", "result_count", "evidence_count",
    "request_attempted", "request_sent", "request_succeeded", "request_failed",
    "sqlite_result_count", "vector_result_count", "memory_result_count", "corrections",
    "plan_valid", "requires_repository_retrieval", "requires_tool_execution",
    "repository_retrieval", "tool_execution", "http_status", "finish_reason",
    "prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
    "prompt_cache_hit_tokens", "elapsed_seconds", "timeout_seconds", "role",
    "tool_name", "tool_call_id_hash", "approval_status", "observation_produced",
    "answer_contract_status", "answer_produced", "execution_status", "index_state",
    "history_message_count", "summary_present",
    "fallback_used", "index_unchanged", "files_read", "files_reused",
    "chunk_count", "parsed_file_count", "added_count", "changed_count",
    "removed_count", "embedding_item_count", "file_count",
    "degraded_count", "failed_count", "skipped_count", "completed_count",
    "stage_count", "stage_outcomes", "stage_outcomes_truncated",
    "failure_stage", "streaming_enabled", "embedding_attempts", "embedding_sent",
    "embedding_successes", "embedding_failures", "stage_elapsed_ms", "first_response_ms",
    "first_delta_ms", "correction_status", "final_validation_status", "channels",
    "tool_call_count", "observation_count", "planner_request_count", "retrieval_query_count",
}

_STAGE_LABELS = {
    "request": "Request",
    "memory_retrieval": "Memory",
    "memory_vector_search": "Memory vector search",
    "embedding_configuration": "Embedding configuration",
    "embedding_readiness": "Embedding",
    "planner": "Planner",
    "planner_validation": "Plan validation",
    "planner_provider_request": "Planner model request",
    "agent_planner": "Agent planner",
    "repository_preparation": "Repository index",
    "repository_file_scan": "Repository file scan",
    "repository_file_content_processing": "Repository files",
    "repository_reference_resolution": "Repository references",
    "repository_sqlite_sync": "SQLite index",
    "chroma_index_synchronization": "Chroma index sync",
    "vector_index_integrity": "Vector index integrity",
    "repository_index_check": "Repository index",
    "repository_retrieval": "Repository retrieval",
    "repository_search_query": "Repository query",
    "sqlite_lexical_search": "SQLite search",
    "sqlite_symbol_search": "SQLite symbol search",
    "sqlite_relation_search": "SQLite relation search",
    "embedding_request": "Embedding",
    "repository_embedding": "Repository embedding",
    "chroma_query": "Chroma",
    "vector_retrieval": "Vector retrieval",
    "retrieval_merge": "Retrieval merge and rerank",
    "retrieval_refinement": "Retrieval refinement",
    "source_hash_validation": "Source and hash validation",
    "evidence_validation": "Evidence evaluation",
    "context_build": "Context assembly",
    "agent_execution": "Agent execution",
    "tool_execution": "Tool",
    "provider_request": "Model request",
    "streaming_completion": "Streaming",
    "final_answer": "DeepSeek answer",
    "answer_contract": "Answer Contract",
    "session_restore": "Session restore",
    "session_context_preparation": "Session context",
    "session_persistence": "Session persistence",
    "request_summary": "Request",
}


def default_log_path() -> Path:
    """Return the dedicated runtime log path without opening or inspecting data."""
    from .config import DATA_DIR
    return DATA_DIR.parent / "logs" / "request-lifecycle.jsonl"


def current_lifecycle() -> RequestLifecycleTracker | None:
    return _CURRENT_TRACKER.get()


def current_stage() -> str | None:
    return _ACTIVE_STAGE.get()


@contextmanager
def bind_lifecycle(tracker: RequestLifecycleTracker):
    token = _CURRENT_TRACKER.set(tracker)
    try:
        yield tracker
    finally:
        _CURRENT_TRACKER.reset(token)


@contextmanager
def lifecycle_stage(stage: str, *, metadata=None, correlation_id=None, console=True):
    """Track an optional stage; no-op when the caller is outside an interactive request."""
    tracker = current_lifecycle()
    if tracker is None:
        yield None
        return
    with tracker.stage(stage, metadata=metadata, correlation_id=correlation_id,
                       console=console) as span:
        yield span


class _StageSpan:
    def __init__(self, tracker, stage, *, metadata, correlation_id, console):
        self.tracker = tracker
        self.stage = stage
        self.metadata = metadata or {}
        self.correlation_id = correlation_id
        self.console = bool(console)
        self.closed = False
        self._tracking = True
        self._active_token: Token | None = None

    def __enter__(self):
        try:
            self.tracker._start_stage(self.stage, self.metadata, self.correlation_id, self.console)
        except Exception:
            self._tracking = False
        self._active_token = _ACTIVE_STAGE.set(self.stage)
        return self

    def finish(self, status="COMPLETED", *, error_code=None, metadata=None):
        if self.closed:
            return
        self.closed = True
        if self._tracking:
            try:
                self.tracker._end_stage(
                    self.stage, self.correlation_id, status=status, error_code=error_code,
                    metadata=metadata, console=self.console,
                )
            except Exception:
                return

    def __exit__(self, exc_type, exc, traceback):
        if not self.closed:
            if exc is None:
                self.finish()
            else:
                error_code = _exception_code(exc)
                error_type = type(exc).__name__
                status = ("CANCELLED" if error_type in {"ConversationCancelled", "CancelledError"}
                          else "TIMEOUT" if isinstance(exc, TimeoutError)
                          or "timeout" in error_code.casefold() else "FAILED")
                self.finish(status, error_code=error_code,
                            metadata={"error_type": error_type})
        if self._active_token is not None:
            _ACTIVE_STAGE.reset(self._active_token)
            self._active_token = None
        return False


class RequestLifecycleTracker:
    """Small synchronous request tracker; logging and progress failures never escape."""

    MAX_LOG_BYTES = 2 * 1024 * 1024
    LOG_BACKUPS = 3

    def __init__(self, *, request_id=None, console_stream=None, log_path=None,
                 monotonic_ns=time.monotonic_ns, wall_clock=None):
        if request_id is None:
            request_id = uuid.uuid4().hex[:12]
        if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Fa-f0-9-]{8,64}", request_id)):
            raise ValueError("Request lifecycle ID must be a bounded hexadecimal identifier.")
        self.request_id = request_id.lower()
        self.console_stream = console_stream
        self.log_path = Path(log_path) if log_path is not None else None
        self._monotonic_ns = monotonic_ns
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._started_ns = self._monotonic_ns()
        self._stage_starts: dict[tuple[str, str | None], list[int]] = {}
        self._stage_states: list[str] = []
        self._stage_outcomes: list[str] = []
        self._stage_count = 0
        self._stage_failures: list[tuple[str, str | None]] = []
        self._stream_stage_tokens: list[Token] = []
        self._lock = threading.RLock()
        self._request_started = False
        self._finished = False
        self._last_terminal_event = None

    def start_request(self, *, provider=None, model=None, session_id=None, mode=None,
                      streaming_enabled=False):
        with self._lock:
            if self._request_started:
                return self.request_id
            self._request_started = True
        fingerprint = None
        if isinstance(session_id, str) and session_id:
            fingerprint = hashlib.sha256(session_id.encode("utf-8", errors="replace")).hexdigest()[:12]
        metadata = {
            "provider": provider,
            "model": model,
            "session_fingerprint": fingerprint,
            "mode": mode,
            "streaming_enabled": bool(streaming_enabled),
        }
        try:
            self._record("request", "STARTED", "RUNNING", metadata=metadata, console=True)
        except Exception:
            pass
        return self.request_id

    def stage(self, stage, *, metadata=None, correlation_id=None, console=True):
        return _StageSpan(self, _safe_stage(stage), metadata=metadata,
                          correlation_id=_safe_correlation(correlation_id), console=console)

    def skip(self, stage, reason_code):
        stage = _safe_stage(stage)
        self._track_stage_outcome(stage, "SKIPPED")
        try:
            self._record(stage, "SKIPPED", "SKIPPED",
                         metadata={"reason_code": reason_code}, console=True)
        except Exception:
            pass

    def degrade(self, stage, reason_code, *, metadata=None):
        stage = _safe_stage(stage)
        self._track_stage_outcome(stage, "DEGRADED")
        values = dict(metadata or {})
        values["degraded_reason"] = reason_code
        try:
            self._record(stage, "DEGRADED", "DEGRADED",
                         error_code=reason_code, metadata=values, console=True)
        except Exception:
            pass

    def observe(self, stage, event_type="OBSERVED", *, status="RUNNING", metadata=None,
                error_code=None, console=False):
        try:
            self._record(_safe_stage(stage), event_type, status, error_code=error_code,
                         metadata=metadata, console=console)
        except Exception:
            pass

    def finish(self, status="COMPLETED", *, error_code=None, metadata=None):
        status = _safe_status(status)
        with self._lock:
            if self._finished:
                return
        with self._lock:
            open_stages = [(key, len(stamps)) for key, stamps in self._stage_starts.items() if stamps]
        in_flight_status = status if status in {"CANCELLED", "TIMEOUT", "FAILED"} else "FAILED"
        in_flight_code = (f"request_{status.casefold()}" if in_flight_status != "FAILED"
                          else "stage_not_closed")
        for (stage, correlation_id), count in reversed(open_stages):
            for _ in range(count):
                try:
                    self._end_stage(stage, correlation_id, status=in_flight_status,
                                    error_code=in_flight_code, console=True)
                except Exception:
                    pass
        with self._lock:
            if self._finished:
                return
            self._finished = True
            states = tuple(self._stage_states)
            failure = next((stage for stage, _code in reversed(self._stage_failures)), None)
        summary = {
            "completed_count": states.count("COMPLETED"),
            "skipped_count": states.count("SKIPPED"),
            "degraded_count": states.count("DEGRADED"),
            "failed_count": states.count("FAILED") + states.count("TIMEOUT"),
            "stage_count": self._stage_count,
            "stage_outcomes": tuple(self._stage_outcomes),
            "stage_outcomes_truncated": self._stage_count > len(self._stage_outcomes),
            "failure_stage": failure,
            "request_status": status,
        }
        summary.update(metadata or {})
        try:
            self._record("request_summary", "SUMMARY", status, error_code=error_code,
                         metadata=summary, console=True, force=True)
        except Exception:
            pass

    def record_stream_event(self, event):
        """Project only safe fields from application stream events into diagnostics."""
        event_type = getattr(getattr(event, "type", None), "value", None)
        payload = getattr(event, "payload", None)
        if not isinstance(event_type, str) or not isinstance(payload, dict):
            return
        if event_type in {"PLANNER_STARTED", "REPLANNING"}:
            self._start_stage("agent_planner", {"operation": event_type.lower()}, None, True)
            self._stream_stage_tokens.append(_ACTIVE_STAGE.set("agent_planner"))
        elif event_type in {"PLAN_READY", "PLANNER_FAILED", "PLAN_REJECTED"}:
            status = "FAILED" if event_type != "PLAN_READY" else "COMPLETED"
            metadata = {
                "plan_valid": event_type == "PLAN_READY",
                "requires_repository_retrieval": payload.get("requires_repository_retrieval"),
                "requires_tool_execution": payload.get("requires_agent_execution"),
            }
            self._end_stage("agent_planner", None, status=status,
                            error_code=_safe_error_code(payload.get("error_code"))
                            if status == "FAILED" else None,
                            metadata=metadata, console=True)
            if self._stream_stage_tokens:
                _ACTIVE_STAGE.reset(self._stream_stage_tokens.pop())
        elif event_type == "TOOL_DISCOVERY":
            self.observe("tool_discovery", metadata={
                "result_count": payload.get("tool_count"),
                "status": payload.get("status"),
            }, console=True)
        elif event_type == "TOOL_CALL_STARTED":
            call_id = payload.get("call_id")
            call_hash = _hash_identifier(call_id)
            tool_name = _safe_label_value(payload.get("tool_id"))
            self._start_stage("tool_execution", {"tool_name": tool_name,
                               "tool_call_id_hash": call_hash}, call_hash, True)
        elif event_type in {"TOOL_CALL_COMPLETED", "TOOL_CALL_FAILED"}:
            call_hash = _hash_identifier(payload.get("call_id"))
            status = "COMPLETED" if event_type == "TOOL_CALL_COMPLETED" else "FAILED"
            self._end_stage("tool_execution", call_hash, status=status,
                            error_code=_safe_error_code(payload.get("error_code"))
                            if status == "FAILED" else None,
                            metadata={"tool_name": _safe_label_value(payload.get("tool_id")),
                                      "tool_call_id_hash": call_hash,
                                      "observation_produced": status == "COMPLETED"},
                            console=True)
        elif event_type == "TOOL_PROGRESS":
            self.observe("tool_execution", metadata={
                "tool_name": _safe_label_value(payload.get("tool_id")),
                "status": "RUNNING",
            }, console=False)
        elif event_type == "OBSERVATION_RECEIVED":
            self.observe("observation", metadata={"observation_count": payload.get("observation_count")},
                         console=True)
        elif event_type in {"WAITING_APPROVAL", "WAITING_USER_INPUT"}:
            name = "approval" if event_type == "WAITING_APPROVAL" else "user_input"
            self.observe(name, event_type, status=event_type, metadata={"approval_status": "waiting"}
                         if name == "approval" else None, console=True)
        elif event_type == "CANCELLED":
            self.observe("request", event_type, status="CANCELLED", console=True)
        elif event_type == "FAILED":
            self.observe("request", event_type, status="FAILED",
                         error_code=_safe_error_code(payload.get("error_code")), console=True)
        elif event_type == "FINAL_ANSWER":
            self.observe("streaming_completion", event_type, status="COMPLETED",
                         metadata={"answer_produced": True}, console=True)
        elif event_type == "COMPLETED":
            self.observe("streaming_completion", event_type, status="COMPLETED", console=True)
        elif event_type == "USAGE":
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self.observe("provider_request", metadata={key: usage.get(key) for key in (
                    "prompt_tokens", "completion_tokens", "total_tokens",
                    "reasoning_tokens", "prompt_cache_hit_tokens",
                )}, console=False)
        elif event_type == "RETRIEVAL_PROGRESS":
            self.observe("repository_retrieval", metadata={
                "completed_count": payload.get("completed"),
                "result_count": payload.get("total"),
            }, console=False)

    def _start_stage(self, stage, metadata, correlation_id, console):
        key = (stage, correlation_id)
        now = self._monotonic_ns()
        with self._lock:
            self._stage_starts.setdefault(key, []).append(now)
        self._record(stage, "STARTED", "RUNNING", metadata=metadata,
                     console=console, correlation_id=correlation_id)

    def _end_stage(self, stage, correlation_id, *, status, error_code=None,
                   metadata=None, console=True):
        status = _safe_status(status)
        key = (stage, correlation_id)
        now = self._monotonic_ns()
        with self._lock:
            starts = self._stage_starts.get(key, [])
            started = starts.pop() if starts else None
            if not starts:
                self._stage_starts.pop(key, None)
            duration = max(0, (now - started) // 1_000_000) if started is not None else None
            self._track_stage_outcome(stage, status)
            if status in {"FAILED", "TIMEOUT"}:
                self._stage_failures.append((stage, error_code))
        values = dict(metadata or {})
        if duration is not None:
            values["stage_elapsed_ms"] = int(duration)
        self._record(stage, status, status, error_code=error_code, metadata=values,
                     console=console, stage_elapsed_ms=duration,
                     correlation_id=correlation_id)

    def _record(self, stage, event_type, status, *, error_code=None, metadata=None,
                console=True, stage_elapsed_ms=None, correlation_id=None, force=False):
        if self._finished and not force:
            return
        now_ns = self._monotonic_ns()
        elapsed_ms = max(0, (now_ns - self._started_ns) // 1_000_000)
        try:
            wall = self._wall_clock()
            if wall.tzinfo is None:
                wall = wall.replace(tzinfo=timezone.utc)
            timestamp = wall.astimezone(timezone.utc).isoformat(timespec="milliseconds")
        except Exception:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        record = {
            "request_id": self.request_id,
            "stage": _safe_stage(stage),
            "event_type": event_type if event_type in _EVENT_TYPES else "OBSERVED",
            "timestamp": timestamp,
            "elapsed_ms": int(elapsed_ms),
            "status": _safe_status(status),
            "error_code": _safe_error_code(error_code) if error_code is not None else None,
            "metadata": _safe_metadata(metadata),
        }
        if stage_elapsed_ms is not None:
            record["stage_elapsed_ms"] = int(max(0, stage_elapsed_ms))
        if correlation_id is not None:
            record["correlation_id_hash"] = correlation_id
        self._write_jsonl(record)
        if console:
            self._write_console(record)
        self._last_terminal_event = record

    def _write_jsonl(self, record):
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            encoded_size = len(line.encode("utf-8"))
            if self.log_path.exists() and self.log_path.stat().st_size + encoded_size > self.MAX_LOG_BYTES:
                for index in range(self.LOG_BACKUPS, 0, -1):
                    source = self.log_path.with_name(self.log_path.name + f".{index - 1}") if index > 1 else self.log_path
                    target = self.log_path.with_name(self.log_path.name + f".{index}")
                    if source.exists():
                        if index == self.LOG_BACKUPS and target.exists():
                            target.unlink()
                        os.replace(source, target)
            with self.log_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
        except Exception:
            # Diagnostics must never change model, tool, or session behavior.
            return

    def _track_stage_outcome(self, stage, status):
        with self._lock:
            self._stage_states.append(status)
            self._stage_count += 1
            self._stage_outcomes.append(f"{stage}:{status}")
            if len(self._stage_outcomes) > 128:
                del self._stage_outcomes[0]

    def _write_console(self, record):
        stream = self.console_stream
        if stream is None:
            return
        try:
            stage = record["stage"]
            event_type = record["event_type"]
            metadata = record["metadata"]
            if stage == "request" and event_type == "STARTED":
                lines = [f"[AgentForge] Request: {self.request_id}", "[Request] Received"]
            elif stage == "request_summary" and event_type == "SUMMARY":
                total = _format_duration(record["elapsed_ms"])
                status = record["status"]
                label = "Request completed" if status == "COMPLETED" else f"Request {status.lower()}"
                lines = [f"[AgentForge] {label} | {total}"]
                if record.get("error_code"):
                    lines.append(f"[Request] Error code: {record['error_code']}")
                if metadata.get("failure_stage"):
                    lines.append(f"[Request] Failed stage: {metadata['failure_stage']}")
            elif event_type == "SKIPPED":
                lines = [f"[{_STAGE_LABELS.get(stage, stage)}] Skipped | {metadata.get('reason_code', 'not_requested')}"]
            else:
                title = _STAGE_LABELS.get(stage, stage)
                if event_type == "STARTED":
                    text = _start_message(stage, metadata)
                elif event_type == "RESPONSE_HEADERS":
                    first = metadata.get("first_response_ms")
                    text = "First response received"
                    if isinstance(first, int):
                        text += f" | {_format_duration(first)}"
                    if isinstance(metadata.get("http_status"), int):
                        text += f" | HTTP {metadata['http_status']}"
                elif event_type == "FIRST_DELTA":
                    first = metadata.get("first_delta_ms")
                    text = "First streamed delta received"
                    if isinstance(first, int):
                        text += f" | {_format_duration(first)}"
                elif event_type == "WAITING_APPROVAL":
                    text = "Waiting for explicit approval"
                elif event_type == "WAITING_USER_INPUT":
                    text = "Waiting for user input"
                elif stage == "streaming_completion" and event_type in {"FINAL_ANSWER", "COMPLETED"}:
                    text = ("Validated answer published" if event_type == "FINAL_ANSWER"
                            else "Lifecycle completed")
                elif record["status"] in {"FAILED", "TIMEOUT", "CANCELLED"}:
                    text = f"{record['status']}"
                    if record.get("error_code"):
                        text += f" | {record['error_code']}"
                    if record.get("stage_elapsed_ms") is not None:
                        text += f" | {_format_duration(record['stage_elapsed_ms'])}"
                elif record["status"] == "DEGRADED":
                    text = f"DEGRADED | {metadata.get('degraded_reason') or record.get('error_code') or 'fallback'}"
                elif event_type == "OBSERVED":
                    text = _observed_message(stage, metadata)
                    if not text:
                        return
                else:
                    text = f"Completed | {_format_duration(record.get('stage_elapsed_ms'))}"
                    extras = _format_safe_extras(metadata)
                    if extras:
                        text += " | " + extras
                lines = [f"[{title}] {text}"]
            for line in lines:
                stream.write(line + "\n")
            stream.flush()
        except Exception:
            return


def _safe_metadata(values):
    if not isinstance(values, dict):
        return {}
    result = {}
    for key, value in values.items():
        if key not in _SAFE_METADATA or value is None:
            continue
        if isinstance(value, bool):
            result[key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            result[key] = max(-1_000_000_000, min(1_000_000_000, value))
        elif isinstance(value, float) and math.isfinite(value):
            result[key] = round(value, 6)
        elif isinstance(value, str):
            if key == "operation" and value not in _SAFE_OPERATIONS:
                continue
            safe_value = (_safe_error_code(value) if key in {
                "reason_code", "degraded_reason", "error_code",
            } else _safe_label_value(value))
            if safe_value is not None:
                result[key] = safe_value
        elif isinstance(value, (tuple, list)) and key == "channels":
            result[key] = [_safe_label_value(item) for item in value[:12]
                           if _safe_label_value(item) is not None]
        elif isinstance(value, (tuple, list)) and key == "stage_outcomes":
            result[key] = [_safe_label_value(item) for item in value[:128]
                           if _safe_label_value(item) is not None]
    return result


def _safe_label_value(value):
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
        return None
    folded = value.casefold()
    if (folded.startswith(("sk-", "ds-", "bearer"))
            or any(marker in folded for marker in ("secret", "password", "credential", "api_key", "token="))):
        return None
    return value


def _safe_stage(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        return "unknown"
    return value


def _safe_status(value):
    return value if isinstance(value, str) and value in _STATUSES else "FAILED"


def _safe_error_code(value):
    if not isinstance(value, str) or not _SAFE_CODE.fullmatch(value):
        return "request_failed"
    folded = value.casefold()
    if (folded.startswith(("sk-", "ds-", "bearer"))
            or any(marker in folded for marker in ("secret", "password", "credential", "api_key", "token="))):
        return "request_failed"
    return value


def _safe_correlation(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{12,64}", value) else None


def _hash_identifier(value):
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _exception_code(exc):
    value = getattr(exc, "code", None)
    if value is None:
        value = getattr(exc, "diagnostic_code", None)
    return _safe_error_code(value) if value is not None else type(exc).__name__[:64]


def finish_provider_stage(span, *, provider, model, before, after, error=None,
                          first_delta_ms=None, parent_stage=None):
    """Finish a provider span using only bounded, content-free transport metadata."""
    if span is None:
        return
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    metadata = {
        "provider": provider,
        "model": model,
        "provider_request_kind": _provider_request_kind(
            parent_stage if parent_stage is not None else current_stage()),
        "request_attempted": _counter_delta(before, after, "attempted"),
        "request_sent": _counter_delta(before, after, "sent"),
        "request_succeeded": _counter_delta(before, after, "succeeded"),
        "request_failed": _counter_delta(before, after, "failed"),
        "http_status": after.get("last_http_status"),
        "finish_reason": after.get("last_finish_reason"),
        "elapsed_seconds": after.get("last_elapsed_seconds"),
        "first_response_ms": after.get("last_first_response_ms"),
        "first_delta_ms": (first_delta_ms if first_delta_ms is not None
                           else after.get("last_first_delta_ms")),
    }
    usage = after.get("last_usage")
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
                    "prompt_cache_hit_tokens"):
            metadata[key] = usage.get(key)
    if error is None:
        try:
            span.finish("COMPLETED", metadata=metadata)
        except Exception:
            pass
        return

    error_code = after.get("last_error_code")
    if not isinstance(error_code, str):
        error_code = type(error).__name__
    error_type = type(error).__name__
    failure_phase = after.get("last_failure_phase")
    detail = str(error).casefold()
    if (error_type in {"TimeoutError", "ReadTimeout", "ConnectTimeout"}
            or "timeout" in error_code.casefold()
            or "timeout" in str(failure_phase).casefold()
            or "timed out" in detail):
        status = "TIMEOUT"
    elif error_type in {"ConversationCancelled", "CancelledError", "KeyboardInterrupt"}:
        status = "CANCELLED"
    else:
        status = "FAILED"
    metadata["error_type"] = error_type
    try:
        span.finish(status, error_code=error_code, metadata=metadata)
    except Exception:
        pass


def _provider_request_kind(stage):
    return {
        "planner_provider_request": "planner",
        "agent_planner": "agent_replanner",
        "agent_execution": "agent_planner_or_tool",
        "final_answer": "final_answer",
        "memory_retrieval": "memory_task",
    }.get(stage, "application")


def _counter_delta(before, after, key):
    first, last = before.get(key), after.get(key)
    if isinstance(first, int) and isinstance(last, int) and last >= first:
        return last - first
    return None


def _format_duration(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "unknown duration"
    if value < 1_000:
        return f"{int(value)} ms"
    return f"{value / 1000:.2f} s"


def _start_message(stage, metadata):
    messages = {
        "memory_retrieval": "Retrieving approved memory...",
        "memory_vector_search": "Searching approved memory vectors...",
        "embedding_configuration": "Loading Embedding configuration...",
        "embedding_readiness": "Checking local Embedding readiness...",
        "planner": "Planning...",
        "planner_provider_request": "Waiting for Planner model response...",
        "planner_validation": "Validating plan...",
        "agent_planner": "Planning next Agent action...",
        "repository_preparation": "Checking repository index...",
        "repository_index_check": "Checking repository identity...",
        "repository_retrieval": "Searching repository...",
        "sqlite_lexical_search": "SQLite lexical search...",
        "sqlite_symbol_search": "Searching SQLite symbols...",
        "sqlite_relation_search": "Searching SQLite relations...",
        "embedding_request": "Generating embedding...",
        "chroma_query": "Chroma vector search...",
        "retrieval_merge": "Merging and reranking candidates...",
        "source_hash_validation": "Validating source and content hash...",
        "evidence_validation": "Evaluating retrieved evidence...",
        "context_build": "Building model context...",
        "agent_execution": "Executing authorized Agent plan...",
        "tool_execution": f"Executing tool {metadata.get('tool_name') or 'authorized tool'}...",
        "provider_request": "Waiting for model response...",
        "final_answer": "Generating answer...",
        "answer_contract": "Validating Answer Contract...",
        "session_restore": "Restoring conversation history...",
        "session_context_preparation": "Preparing session history and summary...",
        "session_persistence": "Saving conversation...",
    }
    title = messages.get(stage, "Running...")
    if stage in {"planner", "planner_provider_request", "agent_planner", "final_answer", "provider_request"}:
        provider, model = metadata.get("provider"), metadata.get("model")
        if provider or model:
            title += " " + "/".join(str(item) for item in (provider, model) if item)
    return title


def _observed_message(stage, metadata):
    if stage == "tool_discovery":
        count = metadata.get("result_count")
        return f"Tool catalog loaded | {count} tools" if isinstance(count, int) else "Authorized tool catalog loaded"
    if stage == "observation":
        count = metadata.get("observation_count")
        return f"Observation received | {count}" if isinstance(count, int) else "Observation received"
    if stage == "approval":
        return "Waiting for explicit approval"
    if stage == "user_input":
        return "Waiting for user input"
    if stage == "request" and metadata.get("status") == "CANCELLED":
        return "Request cancellation observed"
    return ""


def _format_safe_extras(metadata):
    pieces = []
    if isinstance(metadata.get("requires_repository_retrieval"), bool):
        pieces.append("Repository retrieval: " + ("YES" if metadata["requires_repository_retrieval"] else "NO"))
    if isinstance(metadata.get("requires_tool_execution"), bool):
        pieces.append("Tool execution: " + ("YES" if metadata["requires_tool_execution"] else "NO"))
    for key, label in (("evidence_count", "evidence"), ("result_count", "results"),
                       ("corrections", "corrections"), ("http_status", "HTTP"),
                       ("finish_reason", "finish"), ("answer_contract_status", "contract")):
        value = metadata.get(key)
        if value is not None:
            pieces.append(f"{label}={value}")
    return "; ".join(pieces)


__all__ = ["RequestLifecycleTracker", "bind_lifecycle", "current_lifecycle",
           "current_stage", "default_log_path", "finish_provider_stage", "lifecycle_stage"]
