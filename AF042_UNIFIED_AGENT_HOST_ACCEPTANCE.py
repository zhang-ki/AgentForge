"""Real-Qwen acceptance through the same ConversationOrchestrator used by ask.

The host entry point refuses the sandbox network gate before reading credentials.
It owns only one UUID-scoped disposable PostgreSQL database, one temporary Chroma
directory, one synthetic repository, and in-memory synthetic tools.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import traceback
from uuid import uuid4

import AF033_AGENT_LOOP_HOST_ACCEPTANCE as host_infra
from agentforge.agent_loop import MAX_AGENT_STEPS


ROOT = Path(__file__).resolve().parent
CHAT_KEY_ENV = host_infra.CHAT_KEY_ENV
DB_KEY_ENV = host_infra.DB_KEY_ENV
MAX_CHAT_REQUESTS = 128
MAX_DEEPSEEK_CHAT_REQUESTS = 128
DEEPSEEK_CONSENT_PHRASE = "SEND SYNTHETIC TESTS"
AGENT_TOOL_TIMEOUT_SECONDS = 5.0
# Match the product AgentLoop bound. The prior six-call host override rejected
# valid plans that used three repository-search calls before a four-call
# dependency plan, even though the product entry allows eight calls.
AGENT_EXECUTION_MAX_STEPS = MAX_AGENT_STEPS
# The Agent run includes multiple real model-planning calls. Keep its bounded
# end-to-end deadline separate from each individual tool's execution limit.
AGENT_RUN_TIMEOUT_SECONDS = 120.0
SCENARIOS = (
    "general_direct", "repository_auto", "hybrid_knowledge", "single_tool",
    "multi_tool_dependency", "observation_replanning", "tool_failure_recovery",
    "approval_pause_resume", "long_tool_output", "long_answer", "multi_turn", "session_process_restore",
    "streaming_lifecycle", "tool_timeout", "tool_cancel",
)
TARGETABLE_SCENARIOS = tuple(name for name in SCENARIOS if name != "session_process_restore")
DIRECT_ANSWER_SCENARIOS = frozenset({"general_direct", "long_answer"})
AGENT_SCENARIOS = frozenset(set(SCENARIOS) - DIRECT_ANSWER_SCENARIOS)
TEST_DATABASE_RE = host_infra.TEST_DATABASE_RE
RESULT_RE = re.compile(r"AF042_UNIFIED_AGENT_HOST_RESULT_[0-9a-f]{32}\.json\Z")


class AcceptanceBlocked(RuntimeError):
    def __init__(self, stage: str, code: str):
        super().__init__(code)
        self.stage, self.code = stage, code


class CappedConversationProvider:
    """Count every real chat operation and stop before the request ceiling."""

    _CHAT_METHODS = frozenset({"plan_completion", "answer", "answer_general",
                               "answer_hybrid", "summary_text"})

    def __init__(self, delegate, *, limit: int):
        self._delegate = delegate
        self._limit = limit
        self.requests_attempted = 0
        self.request_telemetry = []

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def select_output_budget(self, mode):
        """Use the same verified model budget resolver as the application providers."""
        delegate = self._delegate
        assembler = getattr(delegate, "context_assembler", None)
        descriptor = getattr(delegate, "descriptor", None)
        budgets = getattr(assembler, "budgets", None)
        if budgets is None or descriptor is None:
            raise AcceptanceBlocked("output_budget", "verified_model_budget_unavailable")
        resolution = budgets.resolve_output_budget(descriptor, mode=mode)
        delegate.max_output_tokens = resolution.effective_tokens
        delegate.output_budget = resolution
        return resolution

    def _call(self, name, *args, **kwargs):
        if self.requests_attempted >= self._limit:
            raise AcceptanceBlocked("request_budget", "real_model_request_limit_reached")
        self.requests_attempted += 1
        before = _counts(self._delegate.request_stats())
        succeeded = False
        try:
            response = getattr(self._delegate, name)(*args, **kwargs)
            succeeded = True
            return response
        finally:
            after_stats = self._delegate.request_stats()
            after = _counts(after_stats)
            stats_snapshot = after_stats if isinstance(after_stats, dict) else {}
            sent = max(0, after["sent"] - before["sent"])
            requested_output_tokens = kwargs.get("max_output_tokens")
            if requested_output_tokens is None:
                requested_output_tokens = getattr(self._delegate, "max_output_tokens", None)
            if requested_output_tokens is None:
                requested_output_tokens = getattr(
                    getattr(self._delegate, "client", None), "max_output_tokens", None)
            application_phase = stats_snapshot.get("last_application_failure_phase")
            application_code = stats_snapshot.get("last_application_error_code")
            error_code = stats_snapshot.get("last_error_code")
            self.request_telemetry.append({
                "sequence": self.requests_attempted,
                "operation": name,
                "sent": sent,
                "succeeded": succeeded and after["succeeded"] > before["succeeded"],
                "failed": max(0, after["failed"] - before["failed"]),
                "retries": max(0, after["retries"] - before["retries"]),
                "requested_output_tokens": requested_output_tokens,
                "http_status": stats_snapshot.get("last_http_status") if sent else None,
                "error_code": error_code if isinstance(error_code, str) else None,
                "application_failure_phase": application_phase if isinstance(application_phase, str) else None,
                "application_error_code": application_code if isinstance(application_code, str) else None,
                "usage": _safe_usage(stats_snapshot) if sent else None,
                "finish_reason": stats_snapshot.get("last_finish_reason") if sent else None,
                "response_shape": stats_snapshot.get("last_response_diagnostic") if sent else None,
            })

    def plan_completion(self, *args, **kwargs):
        return self._call("plan_completion", *args, **kwargs)

    def answer(self, *args, **kwargs):
        return self._call("answer", *args, **kwargs)

    def answer_general(self, *args, **kwargs):
        return self._call("answer_general", *args, **kwargs)

    def answer_hybrid(self, *args, **kwargs):
        return self._call("answer_hybrid", *args, **kwargs)

    def summary_text(self, *args, **kwargs):
        return self._call("summary_text", *args, **kwargs)

    def request_stats(self):
        return self._delegate.request_stats()


def _counts(stats):
    stats = stats if isinstance(stats, dict) else {}
    return {key: int(stats.get(key, 0) or 0)
            for key in ("attempted", "sent", "succeeded", "failed", "retries")}


def _delta(after, before):
    return {key: max(0, after.get(key, 0) - before.get(key, 0)) for key in after}


def _digest(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raw = type(value).__name__.encode("ascii", errors="replace")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "utf8_bytes": len(raw)}


def _safe_usage(stats):
    stats = stats if isinstance(stats, dict) else {}
    usage = stats.get("last_usage")
    if not isinstance(usage, dict):
        return None
    allowed = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens",
               "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    return {key: value for key, value in usage.items()
            if key in allowed and isinstance(value, int) and not isinstance(value, bool) and value >= 0}


def _safe_planner_validation_failure(value):
    """Whitelist intent/task and bounded-query diagnostics without plan text."""
    if not isinstance(value, dict):
        return None
    code = value.get("diagnostic_code")
    if not isinstance(code, str) or code not in {
        "intent_task_mismatch", "invalid_bounded_search_queries",
        "task_retrieval_type_mismatch", "agent_execution_task_mismatch",
        "agent_execution_unavailable",
        "planner_correction_request_failed", "plan_validation_failed",
    }:
        return None
    output = {"diagnostic_code": code}
    details = value.get("diagnostic_details")
    if not isinstance(details, dict):
        details = value
    safe = {}
    if code == "intent_task_mismatch":
        intent = details.get("intent")
        if isinstance(intent, str) and intent in {"general", "repository", "hybrid"}:
            safe["intent"] = intent
        counts = details.get("task_type_counts")
        if isinstance(counts, dict):
            safe_counts = {
                task_type: count for task_type, count in counts.items()
                if task_type in {"general", "repository", "external", "tool"}
                and isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 6
            }
            if safe_counts:
                safe["task_type_counts"] = safe_counts
        for key in ("required_task_types", "forbidden_task_types",
                    "missing_task_types", "forbidden_task_types_present"):
            values = details.get(key)
            if isinstance(values, (list, tuple)):
                safe[key] = [item for item in values if isinstance(item, str)
                             and item in {"general", "repository", "external"}]
        for key in ("task_count", "executable_task_count"):
            count = details.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 6:
                safe[key] = count
    elif code == "capability_task_mismatch":
        counts = details.get("task_type_counts")
        if isinstance(counts, dict):
            safe["task_type_counts"] = {
                task_type: count for task_type, count in counts.items()
                if task_type in {"general", "repository", "external", "tool"}
                and isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 6
            }
        for key in ("expected_requires_repository", "provided_requires_repository",
                    "expected_requires_external_data", "provided_requires_external_data"):
            if isinstance(details.get(key), bool):
                safe[key] = details[key]
        invalid_names = details.get("invalid_flag_names")
        if isinstance(invalid_names, (list, tuple)):
            safe["invalid_flag_names"] = [name for name in invalid_names if name in {
                "requires_repository", "requires_external_data",
            }]
        for key in ("task_count", "executable_task_count"):
            count = details.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 6:
                safe[key] = count
    elif code in {"agent_execution_task_mismatch", "agent_execution_unavailable"}:
        intent = details.get("intent")
        if isinstance(intent, str) and intent in {"general", "repository", "hybrid"}:
            safe["intent"] = intent
        counts = details.get("task_type_counts")
        if isinstance(counts, dict):
            safe["task_type_counts"] = {
                task_type: count for task_type, count in counts.items()
                if task_type in {"general", "repository", "external", "tool"}
                and isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 6
            }
        for key in ("task_count", "executable_task_count"):
            count = details.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 6:
                safe[key] = count
        for key in ("expected_requires_agent_execution",
                    "provided_requires_agent_execution", "requires_agent_execution"):
            if isinstance(details.get(key), bool):
                safe[key] = details[key]
    elif code == "invalid_bounded_search_queries":
        for key in ("query_count", "non_string_count", "empty_query_count",
                    "oversized_query_count", "max_query_chars"):
            count = details.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 32_000:
                safe[key] = count
        value_type = details.get("value_type")
        if isinstance(value_type, str) and value_type in {
            "list", "dict", "str", "int", "float", "bool", "NoneType",
        }:
            safe["value_type"] = value_type
    elif code == "task_retrieval_type_mismatch":
        task_id = details.get("task_id")
        if isinstance(task_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", task_id):
            safe["task_id"] = task_id
        task_type = details.get("task_type")
        if isinstance(task_type, str) and task_type in {"general", "repository", "external", "tool"}:
            safe["task_type"] = task_type
        for key in ("requires_retrieval", "expected_requires_retrieval"):
            flag = details.get(key)
            if isinstance(flag, bool):
                safe[key] = flag
    if safe:
        output["diagnostic_details"] = safe
    return output


def _safe_conversation_planner_diagnostic(record):
    """Capture initial/corrected validation states with only bounded metadata."""
    if not isinstance(record, dict):
        return None
    output = {}
    count = record.get("planner_request_count")
    if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 2:
        output["planner_request_count"] = count
    for key in ("task_count", "general_task_count", "repository_task_count",
                "executable_task_count", "correction_attempt"):
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6:
            output[key] = value
    intent = record.get("intent")
    if isinstance(intent, str) and intent in {"general", "repository", "hybrid"}:
        output["intent"] = intent
    task_counts = record.get("task_type_counts")
    if isinstance(task_counts, dict):
        output["task_type_counts"] = {
            name: value for name, value in task_counts.items()
            if name in {"general", "repository", "external", "tool"}
            and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6
        }
    for key in ("requires_agent_execution", "requires_repository", "requires_external_data",
                "execution_started"):
        if isinstance(record.get(key), bool):
            output[key] = record[key]
    tool_count = record.get("available_tool_count")
    if isinstance(tool_count, int) and not isinstance(tool_count, bool) and 0 <= tool_count <= 8:
        output["available_tool_count"] = tool_count
    tool_ids = record.get("available_tool_ids")
    if (isinstance(tool_ids, list) and len(tool_ids) <= 8
            and all(isinstance(item, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item)
                    for item in tool_ids)):
        output["available_tool_ids"] = list(tool_ids)
    for key in ("initial_validation_status", "final_validation_status"):
        value = record.get(key)
        if isinstance(value, str) and value in {
            "pending", "passed", "failed", "passed_after_correction", "failed_after_correction",
        }:
            output[key] = value
    if isinstance(record.get("correction_attempted"), bool):
        output["correction_attempted"] = record["correction_attempted"]
    correction_status = record.get("correction_status")
    if isinstance(correction_status, str) and correction_status in {
        "accepted", "rejected", "request_failed", "blocked_by_context_budget",
    }:
        output["correction_status"] = correction_status
    failure_code = record.get("validation_error_code", record.get("validation_failure_code"))
    if isinstance(failure_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", failure_code):
        output["validation_error_code"] = failure_code
        output["validation_failure_code"] = failure_code
    initial_failure = _safe_planner_validation_failure(record.get("initial_validation_failure"))
    if initial_failure:
        output["initial_validation_failure"] = initial_failure
    final_failure = _safe_planner_validation_failure(record.get("final_validation_failure"))
    if final_failure:
        output["final_validation_failure"] = final_failure
    usage = _safe_usage({"last_usage": record.get("usage")})
    if usage:
        output["usage"] = usage
    return output or None


def _safe_agent_planner_diagnostic(record):
    """Keep bounded budget/error metadata from Agent planning, never prompt data."""
    if not isinstance(record, dict):
        return None
    output = {}
    integer_fields = {
        "context_window", "max_input_tokens", "max_output_tokens", "input_budget",
        "estimated_input_tokens", "required_tokens", "budget_tokens",
        "required_units", "budget_units", "tool_schema_count", "requested_output_tokens",
        "input_budget_tokens", "context_window_tokens", "http_status",
        "planner_catalog_tool_count", "planner_schema_count", "planner_payload_utf8_bytes",
        "request_attempted_delta", "request_sent_delta", "request_succeeded_delta",
        "request_failed_delta", "request_retries_delta",
    }
    string_fields = {"phase", "failure_code", "budget_unit", "estimate_kind", "finish_reason",
                     "exception_type", "provider_failure_code", "provider_failure_phase",
                     "response_validation", "planning_failure_phase", "planning_failure_code",
                     "planning_failure_reason"}
    bool_fields = {"request_sent", "request_succeeded", "content_saved", "tool_schema_included"}
    for key in integer_fields:
        value = record.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000:
            output[key] = value
    for key in string_fields:
        value = record.get(key)
        if (isinstance(value, str) and len(value) <= 80
                and re.fullmatch(r"[A-Za-z0-9_.-]+", value)):
            output[key] = value
    for key in bool_fields:
        value = record.get(key)
        if isinstance(value, bool):
            output[key] = value
    schema_tool_ids = record.get("planner_schema_tool_ids")
    if isinstance(schema_tool_ids, (list, tuple)):
        safe_ids = sorted({value for value in schema_tool_ids[:16]
                           if isinstance(value, str)
                           and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)})
        if safe_ids:
            output["planner_schema_tool_ids"] = safe_ids
    failure_details = record.get("planning_failure_details")
    if isinstance(failure_details, dict):
        allowed_fields = {"action", "tool_ids", "calls", "final_answer", "user_input_request"}
        action = failure_details.get("action")
        details = {"action": action if action in {"load_schemas", "plan", "finish", "ask_user"}
                   else "unknown"}
        for key in ("missing_fields", "unexpected_fields"):
            values = failure_details.get(key)
            if isinstance(values, (list, tuple)):
                details[key] = sorted({value for value in values
                                       if isinstance(value, str) and value in allowed_fields})
        other_count = failure_details.get("other_unexpected_field_count")
        if isinstance(other_count, int) and not isinstance(other_count, bool) and 0 <= other_count <= 100:
            details["other_unexpected_field_count"] = other_count
        for key in ("requested_count", "unique_count", "duplicate_count", "catalog_count", "schema_limit"):
            value = failure_details.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
                details[key] = value
        for key in ("empty_set", "over_limit"):
            value = failure_details.get(key)
            if isinstance(value, bool):
                details[key] = value
        for key in ("missing_tool_ids", "loaded_schema_tool_ids", "undiscovered_tool_ids"):
            values = failure_details.get(key)
            if isinstance(values, (list, tuple)):
                safe_values = sorted({value for value in values[:16]
                                      if isinstance(value, str)
                                      and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)})
                if safe_values:
                    details[key] = safe_values
        schema_round = failure_details.get("schema_round")
        if isinstance(schema_round, int) and not isinstance(schema_round, bool) and 0 <= schema_round <= 16:
            details["schema_round"] = schema_round
        safe_argument_details = _safe_tool_argument_diagnostic(failure_details)
        if safe_argument_details:
            details.update(safe_argument_details)
        output["planning_failure_details"] = details
    components = record.get("components_tokens")
    if isinstance(components, dict):
        safe_components = {
            key: value for key, value in components.items()
            if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key)
            and isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= 10_000_000
        }
        if safe_components:
            output["components_tokens"] = safe_components
    response_shape = record.get("response_shape")
    if isinstance(response_shape, dict):
        safe_shape = {}
        for key in ("body_bytes", "choice_count", "content_chars", "reasoning_content_chars",
                    "body_bytes_bounded"):
            value = response_shape.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2_000_000:
                safe_shape[key] = value
        for key in ("root_type", "choices_type", "message_type", "content_type",
                    "reasoning_content_type", "finish_reason", "error_category", "body_parse"):
            value = response_shape.get(key)
            if isinstance(value, str) and len(value) <= 80 and re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                safe_shape[key] = value
        if safe_shape:
            output["response_shape"] = safe_shape
    memory = record.get("memory")
    if isinstance(memory, dict):
        safe_memory = {}
        for key in ("candidate_count", "injected_count", "omitted_invalid_count", "injected_bytes"):
            value = memory.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
                safe_memory[key] = value
        for key in ("status", "error_type"):
            value = memory.get(key)
            if (isinstance(value, str) and len(value) <= 64
                    and re.fullmatch(r"[A-Za-z0-9_.-]+", value)):
                safe_memory[key] = value
        if isinstance(memory.get("fallback_used"), bool):
            safe_memory["fallback_used"] = memory["fallback_used"]
        if safe_memory:
            output["memory"] = safe_memory
    return output or None


def _safe_tool_argument_diagnostic(value):
    """Retain argument shape and schema metadata, never argument values."""
    if not isinstance(value, dict):
        return None
    safe = {}
    safe_id_pattern = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
    field_pattern = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\[\]-]{0,127}$")
    type_names = {"object", "array", "string", "integer", "number", "boolean", "null", "unknown"}
    for key in ("tool_id", "call_id", "task_id"):
        item = value.get(key)
        if isinstance(item, str) and safe_id_pattern.fullmatch(item):
            safe[key] = item
    session_id = value.get("session_id")
    if isinstance(session_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session_id):
        safe["session_id"] = session_id
    for key in ("planning_round", "call_index", "call_count"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 10_000:
            safe[key] = item
    code = value.get("schema_error_code")
    allowed_codes = {"arguments_not_object", "type_mismatch", "missing_required_field",
                     "unexpected_field", "constraint_violation", "schema_validation_error",
                     "handler_rejected_arguments", "invalid_tool_call_schema", "invalid_dependencies"}
    if isinstance(code, str) and code in allowed_codes:
        safe["schema_error_code"] = code
    for key in ("argument_fields", "missing_fields", "unexpected_fields", "schema_fields"):
        fields = value.get(key)
        if isinstance(fields, (list, tuple)):
            safe[key] = sorted({item for item in fields[:32]
                                if isinstance(item, str) and field_pattern.fullmatch(item)})
    for key in ("argument_types", "schema_field_types"):
        types = value.get(key)
        if isinstance(types, dict):
            safe_types = {field: item_type for field, item_type in list(types.items())[:32]
                          if isinstance(field, str) and field_pattern.fullmatch(field)
                          and item_type in type_names}
            if safe_types:
                safe[key] = safe_types
    for key in ("argument_type", "dependency_type", "response_type"):
        item = value.get(key)
        if item in type_names:
            safe[key] = item
    mismatch_rows = value.get("type_mismatches")
    if isinstance(mismatch_rows, (list, tuple)):
        rows = []
        for row in mismatch_rows[:32]:
            if (isinstance(row, dict) and isinstance(row.get("field"), str)
                    and field_pattern.fullmatch(row["field"])
                    and row.get("expected_type") in type_names
                    and row.get("actual_type") in type_names):
                rows.append({key: row[key] for key in ("field", "expected_type", "actual_type")})
        if rows:
            safe["type_mismatches"] = rows
    dependency_types = value.get("dependency_item_types")
    if isinstance(dependency_types, (list, tuple)):
        safe["dependency_item_types"] = [item for item in dependency_types[:16]
                                          if item in type_names]
    return safe or None


def _safe_planning_failure_diagnostic(exc, provider):
    """Keep the Planner's fixed validation reason and response metadata, never model text."""
    if type(exc).__name__ != "PlanningError":
        return None
    message = " ".join(str(exc).split())[:240]
    stats = provider.request_stats() if callable(getattr(provider, "request_stats", None)) else {}
    if not isinstance(stats, dict):
        stats = {}
    counters = ("attempted", "sent", "succeeded", "failed", "retries",
                "last_http_status", "last_failure_phase", "last_elapsed_seconds",
                "last_requested_output_tokens", "last_response_chars",
                "last_finish_reason", "last_error_code",
                "last_application_failure_phase", "last_application_error_code")
    provider_state = {}
    for key in counters:
        value = stats.get(key)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            provider_state[key] = value
        elif (key == "last_elapsed_seconds" and isinstance(value, float)
              and math.isfinite(value) and value >= 0):
            provider_state[key] = value
    response = stats.get("last_response_diagnostic")
    shape_keys = ("body_bytes", "root_type", "choices_type", "choice_count",
                  "message_type", "content_type", "content_chars",
                  "reasoning_content_type", "reasoning_content_chars", "finish_reason")
    response_shape = ({key: response[key] for key in shape_keys if key in response}
                      if isinstance(response, dict) else None)
    diagnostic = {"phase": "conversation_planner", "exception_type": "PlanningError",
                  "reason": message, "provider": provider_state,
                  "response_shape": response_shape, "usage": _safe_usage(stats)}
    failure_code = getattr(exc, "diagnostic_code", None)
    if isinstance(failure_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", failure_code):
        diagnostic["planning_failure_code"] = failure_code
    details = getattr(exc, "diagnostic_details", None)
    if isinstance(details, dict):
        safe_failure = _safe_planner_validation_failure({
            "diagnostic_code": failure_code,
            "diagnostic_details": details,
        })
        if safe_failure and safe_failure.get("diagnostic_details"):
            diagnostic["planning_failure_details"] = safe_failure["diagnostic_details"]
        for key in ("planner_request_count", "correction_attempted", "correction_status"):
            value = details.get(key)
            if key == "planner_request_count" and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2:
                diagnostic[key] = value
            elif key == "correction_attempted" and isinstance(value, bool):
                diagnostic[key] = value
            elif key == "correction_status" and isinstance(value, str) and value in {
                    "accepted", "rejected", "request_failed", "blocked_by_context_budget"}:
                diagnostic[key] = value
        for key in ("correction_attempt", "task_count", "general_task_count",
                    "repository_task_count", "executable_task_count"):
            value = details.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 6:
                diagnostic[key] = value
        safe_plan_metrics = details.get("plan_metrics")
        if isinstance(safe_plan_metrics, dict):
            safe_metrics = _safe_conversation_planner_diagnostic(safe_plan_metrics)
            if safe_metrics:
                diagnostic["plan_metrics"] = safe_metrics
        initial_failure = _safe_planner_validation_failure(details.get("initial_validation_failure"))
        if initial_failure:
            diagnostic["initial_validation_failure"] = initial_failure
        final_failure = _safe_planner_validation_failure(details.get("final_validation_failure"))
        if final_failure:
            diagnostic["final_validation_failure"] = final_failure
    return diagnostic


def _safe_scenario_failure_diagnostic(exc, provider):
    """Capture failure categories and response shape without model or user content."""
    planning = _safe_planning_failure_diagnostic(exc, provider)
    if planning is not None:
        location = _safe_exception_location(exc)
        if location:
            planning["exception_location"] = location
        host_diagnostics = _safe_host_failure_diagnostics(
            getattr(exc, "agentforge_host_diagnostics", None))
        if host_diagnostics:
            planning["host_diagnostics"] = host_diagnostics
        return planning
    diagnostic = {"exception_type": type(exc).__name__}
    location = _safe_exception_location(exc)
    if location:
        diagnostic["exception_location"] = location
    for output_key, attribute in (("failure_code", "diagnostic_code"),
                                  ("failure_phase", "phase")):
        value = getattr(exc, attribute, None)
        if (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)):
            diagnostic[output_key] = value
    code = getattr(exc, "code", None)
    code = getattr(code, "value", code)
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
        diagnostic.setdefault("failure_code", code)
    try:
        stats = provider.request_stats() if callable(getattr(provider, "request_stats", None)) else {}
    except Exception:
        stats = {}
    if not isinstance(stats, dict):
        stats = {}
    provider_diagnostic = {}
    numeric_fields = ("attempted", "sent", "succeeded", "failed", "retries",
                      "last_http_status", "last_response_chars", "last_requested_output_tokens")
    for key in numeric_fields:
        value = stats.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            provider_diagnostic[key] = value
    for key in ("last_failure_phase", "last_error_code", "last_finish_reason",
                "last_application_failure_phase", "last_application_error_code"):
        value = stats.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
            provider_diagnostic[key] = value
    if provider_diagnostic:
        diagnostic["provider"] = provider_diagnostic
    citation = getattr(exc, "diagnostic", None)
    if isinstance(citation, dict):
        safe_citation = {}
        rule_id = citation.get("rule_id")
        if isinstance(rule_id, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", rule_id):
            safe_citation["rule_id"] = rule_id
        for key in ("matched_source_ids", "validated_source_ids"):
            values = citation.get(key)
            if isinstance(values, (list, tuple)):
                ids = list(dict.fromkeys(value for value in values[:32]
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", value)))
                if ids:
                    safe_citation[key] = ids
        validation_result = citation.get("validation_result")
        if validation_result in {"accepted", "rejected"}:
            safe_citation["validation_result"] = validation_result
        if safe_citation:
            diagnostic["citation_validation"] = safe_citation
    response = stats.get("last_response_diagnostic")
    if isinstance(response, dict):
        shape_keys = ("body_bytes", "choice_count", "content_chars", "reasoning_content_chars")
        shape = {key: value for key in shape_keys
                 if isinstance((value := response.get(key)), int)
                 and not isinstance(value, bool) and 0 <= value <= 2_000_000}
        for key in ("root_type", "choices_type", "message_type", "content_type",
                    "reasoning_content_type", "finish_reason"):
            value = response.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                shape[key] = value
        if shape:
            diagnostic["response_shape"] = shape
    usage = _safe_usage(stats)
    if usage:
        diagnostic["usage"] = usage
    host_diagnostics = _safe_host_failure_diagnostics(
        getattr(exc, "agentforge_host_diagnostics", None))
    if host_diagnostics:
        diagnostic["host_diagnostics"] = host_diagnostics
    return diagnostic


def _safe_exception_location(exc):
    """Retain only the last source frame, never local paths or frame values."""
    try:
        frames = traceback.extract_tb(exc.__traceback__)
    except Exception:
        return None
    if not frames:
        return None
    frame = frames[-1]
    module = Path(frame.filename).name
    function = frame.name
    if (not isinstance(module, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", module)
            or not isinstance(function, str)
            or not re.fullmatch(r"[A-Za-z0-9_.<>-]{1,120}", function)
            or isinstance(frame.lineno, bool) or not isinstance(frame.lineno, int)
            or frame.lineno < 1):
        return None
    return {"module": module, "function": function, "line": frame.lineno}


def _safe_host_failure_diagnostics(record):
    """Whitelist retry-free execution metadata captured when an orchestrated turn raises."""
    if not isinstance(record, dict):
        return None
    output = {}
    telemetry = record.get("request_telemetry")
    if isinstance(telemetry, (list, tuple)):
        operations = {"plan_completion", "answer", "answer_general", "answer_hybrid", "summary_text"}
        safe_rows = []
        for item in telemetry[-32:]:
            if not isinstance(item, dict):
                continue
            row = {}
            sequence = item.get("sequence")
            if isinstance(sequence, int) and not isinstance(sequence, bool) and 1 <= sequence <= 10_000:
                row["sequence"] = sequence
            operation = item.get("operation")
            if isinstance(operation, str) and operation in operations:
                row["operation"] = operation
            for key in ("sent", "failed", "retries"):
                value = item.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
                    row[key] = value
            for key in ("requested_output_tokens", "http_status"):
                value = item.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                    row[key] = value
            for key in ("error_code", "application_failure_phase", "application_error_code"):
                value = item.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                    row[key] = value
            for key in ("succeeded",):
                if isinstance(item.get(key), bool):
                    row[key] = item[key]
            reason = item.get("finish_reason")
            if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", reason):
                row["finish_reason"] = reason
            response = item.get("response_shape")
            if isinstance(response, dict):
                shape = {}
                for key in ("body_bytes", "choice_count", "content_chars",
                            "reasoning_content_chars"):
                    value = response.get(key)
                    if (isinstance(value, int) and not isinstance(value, bool)
                            and 0 <= value <= 2_000_000):
                        shape[key] = value
                for key in ("root_type", "choices_type", "message_type", "content_type",
                            "reasoning_content_type", "finish_reason"):
                    value = response.get(key)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                        shape[key] = value
                if shape:
                    row["response_shape"] = shape
            usage = item.get("usage")
            safe_usage = _safe_usage({"last_usage": usage}) if isinstance(usage, dict) else None
            if safe_usage:
                row["usage"] = safe_usage
            if row:
                safe_rows.append(row)
        if safe_rows:
            output["request_telemetry"] = safe_rows

    conversation = record.get("conversation_planner")
    if isinstance(conversation, dict):
        safe = {}
        for key in ("phase", "estimate_kind", "finish_reason"):
            value = conversation.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                safe[key] = value
        for key in ("context_window", "max_input_tokens", "max_output_tokens", "input_budget",
                    "estimated_input_tokens"):
            value = conversation.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000:
                safe[key] = value
        for key in ("request_sent", "request_succeeded"):
            if isinstance(conversation.get(key), bool):
                safe[key] = conversation[key]
        components = conversation.get("components_tokens")
        if isinstance(components, dict):
            safe_components = {
                key: value for key, value in components.items()
                if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", key)
                and isinstance(value, int) and not isinstance(value, bool)
                and 0 <= value <= 10_000_000
            }
            if safe_components:
                safe["components_tokens"] = safe_components
        usage = conversation.get("usage")
        safe_usage = _safe_usage({"last_usage": usage}) if isinstance(usage, dict) else None
        if safe_usage:
            safe["usage"] = safe_usage
        validation = _safe_conversation_planner_diagnostic(conversation)
        if validation:
            safe.update(validation)
        memory = conversation.get("memory")
        if isinstance(memory, dict):
            safe_memory = {}
            for key in ("candidate_count", "injected_count"):
                value = memory.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
                    safe_memory[key] = value
            status = memory.get("status")
            if isinstance(status, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", status):
                safe_memory["status"] = status
            if isinstance(memory.get("fallback_used"), bool):
                safe_memory["fallback_used"] = memory["fallback_used"]
            if safe_memory:
                safe["memory"] = safe_memory
        if safe:
            output["conversation_planner"] = safe

    planners = record.get("agent_planners")
    if isinstance(planners, (list, tuple)):
        safe_planners = []
        for item in planners[-8:]:
            if not isinstance(item, dict):
                continue
            planner = {}
            count = item.get("request_count")
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 10_000:
                planner["request_count"] = count
            diagnostics = item.get("request_diagnostics")
            if isinstance(diagnostics, (list, tuple)):
                rows = [safe for raw in diagnostics[-16:]
                        if (safe := _safe_agent_planner_diagnostic(raw)) is not None]
                if rows:
                    planner["request_diagnostics"] = rows
            decisions = item.get("decisions")
            if isinstance(decisions, (list, tuple)):
                safe_decisions = []
                for decision in decisions[-16:]:
                    if not isinstance(decision, dict):
                        continue
                    row = {}
                    for key in ("planner_round", "observation_count"):
                        value = decision.get(key)
                        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000:
                            row[key] = value
                    calls = decision.get("selected_calls")
                    if isinstance(calls, (list, tuple)):
                        safe_calls = []
                        for call in calls[:16]:
                            if not isinstance(call, dict):
                                continue
                            safe_call = {}
                            for key in ("call_id", "task_id", "tool_id"):
                                value = call.get(key)
                                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                                    safe_call[key] = value
                            dependencies = call.get("dependencies")
                            if isinstance(dependencies, (list, tuple)):
                                safe_call["dependency_count"] = min(len(dependencies), 100)
                            if safe_call:
                                safe_calls.append(safe_call)
                        row["selected_calls"] = safe_calls
                    catalog_ids = decision.get("catalog_tool_ids")
                    if isinstance(catalog_ids, (list, tuple)):
                        safe_catalog_ids = sorted({value for value in catalog_ids[:16]
                            if isinstance(value, str)
                            and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)})
                        if safe_catalog_ids:
                            row["catalog_tool_ids"] = safe_catalog_ids
                    failure = decision.get("failure")
                    if isinstance(failure, dict):
                        safe_failure = {}
                        for key in ("code", "phase", "reason"):
                            value = failure.get(key)
                            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                                safe_failure[key] = value
                        details = failure.get("details")
                        if isinstance(details, dict):
                            safe_ids = {}
                            for key in ("missing_tool_ids", "loaded_schema_tool_ids",
                                        "undiscovered_tool_ids"):
                                values = details.get(key)
                                if isinstance(values, (list, tuple)):
                                    ids = sorted({value for value in values[:16]
                                        if isinstance(value, str)
                                        and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)})
                                    if ids:
                                        safe_ids[key] = ids
                            if safe_ids:
                                safe_failure["details"] = safe_ids
                        if safe_failure:
                            row["failure"] = safe_failure
                    if isinstance(decision.get("done"), bool):
                        row["done"] = decision["done"]
                    if row:
                        safe_decisions.append(row)
                if safe_decisions:
                    planner["decisions"] = safe_decisions
            if planner:
                safe_planners.append(planner)
        if safe_planners:
            output["agent_planners"] = safe_planners

    events = record.get("events")
    if isinstance(events, (list, tuple)):
        safe_events = []
        for event in events[-64:]:
            if not isinstance(event, dict):
                continue
            row = {}
            for key in ("type", "error_code", "phase"):
                value = event.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                    row[key] = value
            for key in ("sequence",):
                value = event.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    row[key] = value
            mode = event.get("mode")
            if isinstance(mode, str) and mode in {"general", "repository", "hybrid"}:
                row["mode"] = mode
            task_count = event.get("task_count")
            if isinstance(task_count, int) and not isinstance(task_count, bool) and 0 <= task_count <= 6:
                row["task_count"] = task_count
            if isinstance(event.get("requires_agent_execution"), bool):
                row["requires_agent_execution"] = event["requires_agent_execution"]
            if row:
                safe_events.append(row)
        if safe_events:
            output["event_metadata"] = safe_events
    return output or None


def _attach_scenario_failure_diagnostics(exc, *, provider, telemetry_before,
                                         orchestrator, recorders, events):
    """Attach safe-to-filter metadata so an error cannot erase its execution stage."""
    try:
        telemetry = getattr(provider, "request_telemetry", ())
        conversation_planner = getattr(getattr(orchestrator, "planner", None),
                                       "last_request_diagnostics", None)
        agent_planners = []
        for recorder in recorders:
            delegate = getattr(recorder, "delegate", None)
            agent_planners.append({
                "request_count": getattr(delegate, "request_count", None),
                "request_diagnostics": getattr(delegate, "request_diagnostics", ()),
                "decisions": getattr(recorder, "decisions", ()),
            })
        context = {
            "request_telemetry": list(telemetry[telemetry_before:]) if isinstance(telemetry, (list, tuple)) else (),
            "conversation_planner": conversation_planner,
            "agent_planners": agent_planners,
            "events": events,
        }
        setattr(exc, "agentforge_host_diagnostics", context)
    except Exception:
        # Diagnostics must never mask the original failure.
        return


def _scenario_stop_failure_code(scenario, request_stats):
    """Do not label an assertion/operation failure as a transport failure."""
    if request_stats.get("failed", 0) or request_stats.get("retries", 0):
        return scenario.get("failure_stage") or scenario.get("scenario"), "model_request_failed_or_retried"
    return (scenario.get("failure_stage") or scenario.get("scenario"),
            scenario.get("failure_code") or "scenario_assertion_failed")


def _completed_agent_checkpoint(scenario):
    """Retain a successful execution checkpoint even if an unrelated assertion fails."""
    trace = scenario.get("trace") if isinstance(scenario, dict) else None
    if not isinstance(trace, dict) or trace.get("execution_status") != "completed":
        return None
    conversation_id = trace.get("session_id")
    execution_id = trace.get("execution_id")
    if (not isinstance(conversation_id, str) or not conversation_id.strip()
            or not isinstance(execution_id, str) or not execution_id.strip()):
        return None
    return {"conversation_id": conversation_id, "execution_id": execution_id}


def _tool_timeout_was_observed(trace):
    """Compare stable tool error codes case-insensitively across providers."""
    if not isinstance(trace, dict) or trace.get("execution_status") != "tool_timeout":
        return False
    return any(
        isinstance(call, dict)
        and call.get("tool_id") == "slow_synthetic_read"
        and call.get("status") == "failed"
        and isinstance(call.get("error_code"), str)
        and call["error_code"].casefold() == "timeout"
        for call in trace.get("tool_calls", ())
    )


def _agent_engine_status(scenarios):
    tool_calls = [call for item in scenarios.values()
                  for trace in (item.get("trace"), item.get("resume_trace"))
                  if isinstance(trace, dict)
                  for call in trace.get("tool_calls", ())]
    if tool_calls:
        return "PASS"
    agent_scenario_measured = any(
        name in AGENT_SCENARIOS and isinstance(item.get("trace"), dict)
        for name, item in scenarios.items()
    )
    return "FAIL" if agent_scenario_measured else "NOT_MEASURED"


def _lifecycle_assertions(result, trace):
    """Report completed-turn checks as N/A only for deliberate non-answer stops."""
    status = getattr(result, "execution_status", None)
    expected_no_answer = status in {
        "waiting_approval", "waiting_user_input", "cancelled", "tool_timeout",
    }
    contract_observed = (getattr(result, "contract", None) is not None
                         if status == "completed" else
                         "NOT_APPLICABLE" if expected_no_answer else False)
    messages_persisted = (trace.get("persisted_message_count", 0) >= 2
                          if status == "completed" else
                          "NOT_APPLICABLE" if expected_no_answer else False)
    return {
        "answer_contract_observed": contract_observed,
        "session_messages_persisted": messages_persisted,
    }


def _read_result_pages(result_store, result_ref, *, expected_sha256, page_chars=4_000):
    """Read every bounded page and verify the byte-exact stored result without logging it."""
    from agentforge.agent_execution import PostgresToolResultStore

    if (not isinstance(result_ref, str) or not result_ref.startswith("agent-result:")
            or isinstance(page_chars, bool) or not isinstance(page_chars, int)
            or not 1 <= page_chars <= 10_000
            or not isinstance(expected_sha256, str) or len(expected_sha256) != 64):
        raise ValueError("Long-result paging verification parameters are invalid.")
    max_chars = PostgresToolResultStore.MAX_RESULT_CHARS
    max_pages = (max_chars + page_chars - 1) // page_chars
    offset = 0
    total_chars = None
    characters_read = 0
    page_count = 0
    digest = hashlib.sha256()
    first_page_chars = 0
    while page_count < max_pages:
        page = result_store.page(result_ref, offset=offset, limit=page_chars)
        if (not isinstance(page, dict) or page.get("result_ref") != result_ref
                or page.get("offset") != offset
                or isinstance(page.get("total_chars"), bool)
                or not isinstance(page.get("total_chars"), int)
                or page.get("total_chars") < 0
                or isinstance(page.get("next_offset"), bool)
                or not isinstance(page.get("next_offset"), int)
                or not isinstance(page.get("complete"), bool)
                or not isinstance(page.get("text"), str)):
            raise ValueError("Long-result page metadata is invalid.")
        if total_chars is None:
            total_chars = page["total_chars"]
        if page["total_chars"] != total_chars:
            raise ValueError("Long-result page total changed during reconstruction.")
        text = page["text"]
        next_offset = page["next_offset"]
        if (len(text) > page_chars or next_offset != offset + len(text) or next_offset > total_chars
                or (not page["complete"] and next_offset <= offset)
                or page["complete"] != (next_offset == total_chars)):
            raise ValueError("Long-result pages contain a gap, overlap, or invalid completion marker.")
        if page_count == 0:
            first_page_chars = len(text)
        digest.update(text.encode("utf-8"))
        characters_read += len(text)
        page_count += 1
        if page["complete"]:
            break
        offset = next_offset
    else:
        raise ValueError("Long-result paging exceeded the durable result bound.")

    actual_sha256 = digest.hexdigest()
    return {
        "complete": total_chars is not None and characters_read == total_chars,
        "total_chars": total_chars,
        "characters_read": characters_read,
        "pages_read": page_count,
        "first_page_chars": first_page_chars,
        "sha256": actual_sha256,
        "matches_expected": actual_sha256 == expected_sha256,
    }


def _long_tool_answer_is_supported(result):
    """Require a usable final contract and a cited excerpt proving the synthetic fact."""
    contract = getattr(result, "contract", None)
    answer = getattr(getattr(result, "answer", None), "text", None)
    if (contract is None or getattr(contract, "status", None) not in {"answered", "limited"}
            or not isinstance(answer, str) or not answer.strip()):
        return False
    normalized = answer.casefold()
    if not all(term in normalized for term in ("compute_total", "value", "delta")):
        return False
    if not any(term in normalized for term in ("+", "sum", "add", "addition", "plus")):
        return False
    evidence_by_citation = {
        getattr(item, "citation", None): item for item in getattr(contract, "evidence", ())
    }
    for claim in getattr(contract, "claims", ()):
        if (getattr(claim, "origin", None) != "repository"
                or getattr(claim, "kind", None) not in {"confirmed", "inference"}
                or not isinstance(getattr(claim, "text", None), str)
                or "compute_total" not in claim.text.casefold()
                or not {"value", "delta"}.issubset(set(re.findall(r"[a-z_]+", claim.text.casefold())))):
            continue
        for citation in getattr(claim, "citations", ()):
            item = evidence_by_citation.get(citation)
            excerpt = getattr(item, "excerpt", "") if item is not None else ""
            if "def compute_total(value, delta):" in excerpt and "return value + delta" in excerpt:
                return True
    return False


def _single_synthetic_tool_call_completed(trace, tool_id):
    """Count the requested synthetic action separately from required RAG tools."""
    if not isinstance(trace, dict) or trace.get("execution_status") != "completed":
        return False
    calls = trace.get("tool_calls", ())
    if not isinstance(calls, (list, tuple)):
        return False
    matching = [item for item in calls
                if isinstance(item, dict) and item.get("tool_id") == tool_id]
    return len(matching) == 1 and matching[0].get("status") == "completed"


def _direct_general_answer_without_tools(trace):
    """Require general-only acceptance scenarios to avoid tool execution."""
    return (isinstance(trace, dict)
            and trace.get("selected_route") == "general"
            and not trace.get("tool_calls"))


def _required_tools_ran_in_order(observed_order, required_order):
    """Check a dependency subsequence without prescribing total calls or planner rounds."""
    if not isinstance(observed_order, (list, tuple)) or not isinstance(required_order, (list, tuple)):
        return False
    cursor = 0
    for tool_id in required_order:
        try:
            cursor = observed_order.index(tool_id, cursor) + 1
        except ValueError:
            return False
    return bool(required_order)


def _parse_runner_args(argv):
    parser = argparse.ArgumentParser(description="Run the Phase 4.2 real-model host acceptance.")
    parser.add_argument("--scenario", choices=TARGETABLE_SCENARIOS,
                        help="Run one isolated scenario; omit to run the full scenario suite.")
    parser.add_argument("--provider", choices=("llama-server", "deepseek"),
                        default="llama-server")
    parser.add_argument("--model", help="Explicit model ID; DeepSeek ID is checked against a fresh /models response.")
    parser.add_argument("--approve-synthetic-actions", action="store_true",
                        help="Explicitly authorize the runner's isolated in-memory approval scenario.")
    args = parser.parse_args(argv)
    if args.provider == "deepseek" and not args.model:
        parser.error("DeepSeek host acceptance requires an explicit --model ID.")
    if args.provider == "llama-server" and args.model not in (None, host_infra.CHAT_MODEL):
        parser.error("Qwen host acceptance only supports the configured local model.")
    return args


def _selected_scenarios(argv):
    args = _parse_runner_args(argv)
    return (args.scenario,) if args.scenario else _full_suite_scenarios(args.provider)


def _full_suite_scenarios(provider_id):
    """Qwen and DeepSeek full suites share scenarios except DeepSeek's long answer."""
    if provider_id == "deepseek":
        return SCENARIOS
    return tuple(name for name in SCENARIOS if name != "long_answer")


def _stop_after_scenario(*, full_suite, scenario_status, request_stats):
    """Continue independent full-suite checks after assertions, but stop on model transport faults."""
    if request_stats.get("failed", 0) or request_stats.get("retries", 0):
        return True
    return not full_suite and scenario_status != "PASS"


def _deepseek_streaming_enabled_for_scenario(provider_id, scenario_name, capability):
    return (provider_id == "deepseek" and scenario_name == "streaming_lifecycle"
            and capability is True)


def _selected_scenario_status(selected_scenarios, scenarios):
    statuses = [scenarios.get(name, {}).get("status", "NOT_MEASURED")
                for name in selected_scenarios]
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    if any(status != "PASS" for status in statuses):
        return "PARTIAL"
    return "PASS"


def _usage_summary(requests):
    totals = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                                  "reasoning_tokens", "prompt_cache_hit_tokens",
                                  "prompt_cache_miss_tokens")}
    measured = 0
    sanitized = []
    for request in requests:
        if not isinstance(request, dict):
            continue
        usage = request.get("usage")
        safe_usage = ({key: value for key, value in usage.items()
                       if key in totals and isinstance(value, int)
                       and not isinstance(value, bool) and value >= 0}
                      if isinstance(usage, dict) else None)
        sanitized.append({key: request.get(key) for key in (
            "sequence", "operation", "sent", "succeeded", "failed", "retries",
            "finish_reason") if key in request} | {"usage": safe_usage})
        if not isinstance(usage, dict):
            continue
        measured += 1
        for key, value in safe_usage.items():
            totals[key] += value
    return {"requests_with_usage": measured, "totals": totals,
            "per_request": sanitized}


def _safe_output_budget(provider):
    """Return resolved budget numbers only; prompts and answers are excluded."""
    budget = getattr(provider, "output_budget", None)
    if budget is None:
        return None
    fields = (
        "mode", "requested_tokens", "effective_tokens", "model_max_output_tokens",
        "provider_max_output_tokens", "adapter_max_output_tokens", "context_window",
        "safety_margin", "output_limit_includes_reasoning",
    )
    result = {name: getattr(budget, name) for name in fields
              if getattr(budget, name, None) is not None}
    return result or None


def _safe_event(event):
    from agentforge.streaming import StreamingEvent
    if not isinstance(event, StreamingEvent):
        return {"type": "INVALID_EVENT"}
    result = {"type": event.type.value, "sequence": event.sequence,
              "execution_id": event.execution_id, "task_id": event.task_id}
    payload = event.payload if isinstance(event.payload, dict) else {}
    code = payload.get("error_code")
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code):
        result["error_code"] = code
    mode = payload.get("mode")
    if isinstance(mode, str) and mode in {"general", "repository", "hybrid"}:
        result["mode"] = mode
    task_count = payload.get("task_count")
    if isinstance(task_count, int) and not isinstance(task_count, bool) and 0 <= task_count <= 6:
        result["task_count"] = task_count
    if isinstance(payload.get("requires_agent_execution"), bool):
        result["requires_agent_execution"] = payload["requires_agent_execution"]
    return result


def _sanitize_decisions(decisions):
    output = []
    for decision in decisions:
        calls = []
        for call in decision.get("selected_calls", ()):
            arguments = call.get("arguments", {})
            calls.append({"call_id": call.get("call_id"), "task_id": call.get("task_id"),
                          "tool_id": call.get("tool_id"),
                          "dependencies": list(call.get("dependencies", ())),
                          "argument_keys": sorted(arguments) if isinstance(arguments, dict) else [],
                          "arguments_digest": _digest(arguments)})
        observations = [{"tool_id": item.get("tool_id"), "status": item.get("status"),
                         "error_code": item.get("error_code"),
                         "output_digest": _digest(item.get("output"))}
                        for item in decision.get("observations", ())]
        item = {"planner_round": decision.get("planner_round"),
                "observation_count": decision.get("observation_count"),
                "observations": observations, "selected_calls": calls,
                "done": decision.get("done")}
        catalog_ids = decision.get("catalog_tool_ids")
        if isinstance(catalog_ids, (list, tuple)):
            safe_catalog_ids = sorted({value for value in catalog_ids[:16]
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value)})
            if safe_catalog_ids:
                item["catalog_tool_ids"] = safe_catalog_ids
        failure = decision.get("failure")
        if isinstance(failure, dict):
            safe_failure = {key: value for key, value in failure.items()
                            if key in {"code", "phase", "reason"}
                            and isinstance(value, str)
                            and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value)}
            if safe_failure:
                item["failure"] = safe_failure
            details = failure.get("details")
            if isinstance(details, dict):
                allowed_fields = {"action", "tool_ids", "calls", "final_answer", "user_input_request"}
                safe_details = {"action": details.get("action") if details.get("action") in
                                {"load_schemas", "plan", "finish", "ask_user"} else "unknown"}
                for key in ("missing_fields", "unexpected_fields"):
                    values = details.get(key)
                    if isinstance(values, (list, tuple)):
                        safe_details[key] = sorted({value for value in values
                                                    if isinstance(value, str) and value in allowed_fields})
                count = details.get("other_unexpected_field_count")
                if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 100:
                    safe_details["other_unexpected_field_count"] = count
                for key in ("requested_count", "unique_count", "duplicate_count", "catalog_count", "schema_limit"):
                    value = details.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
                        safe_details[key] = value
                for key in ("empty_set", "over_limit"):
                    value = details.get(key)
                    if isinstance(value, bool):
                        safe_details[key] = value
                item["failure"]["details"] = safe_details
        output.append(item)
    return output


def _replanning_counts(events):
    """Count attempts and only plans the engine accepted after an Observation."""
    attempts = successful = 0
    awaiting_engine_validation = False
    rejected_or_failed = {"PLAN_REJECTED", "PLANNER_FAILED", "FAILED", "CANCELLED"}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "REPLANNING":
            attempts += 1
            awaiting_engine_validation = True
        elif awaiting_engine_validation and event_type == "PLAN_READY":
            successful += 1
            awaiting_engine_validation = False
        elif awaiting_engine_validation and event_type in rejected_or_failed:
            awaiting_engine_validation = False
    return attempts, successful


def _result_template(selected_scenarios=None, *, provider_id="llama-server", model_id=None):
    if selected_scenarios is None:
        selected_scenarios = _full_suite_scenarios(provider_id)
    run_id = uuid4().hex
    selected_scenarios = tuple(selected_scenarios)
    model_id = model_id or (host_infra.CHAT_MODEL if provider_id == "llama-server" else None)
    return {
        "schema": "agentforge-phase4-2-unified-host-acceptance-v1",
        "run_id": run_id, "status": "PRECHECK", "failure_stage": None,
        "failure_code": None, "services_started_or_stopped": False,
        "formal_database_accessed": False, "formal_repository_accessed": False,
        "acceptance_scope": "full" if selected_scenarios == _full_suite_scenarios(provider_id) else "targeted",
        "selected_scenarios": list(selected_scenarios),
        "provider": {"provider_id": provider_id, "model_id": model_id,
                     "mode": "real_model", "deepseek":
                         "NOT_MEASURED_SEPARATE_PHASE" if provider_id == "llama-server" else "SELECTED"},
        "model_discovery": {"status": "NOT_APPLICABLE", "requests": 0},
        "checks": {}, "scenarios": {name: {"status": "NOT_MEASURED"} for name in SCENARIOS},
        "chat_requests": {"attempted": 0, "sent": 0, "succeeded": 0,
                           "failed": 0, "retries": 0},
        "request_limit": (MAX_DEEPSEEK_CHAT_REQUESTS if provider_id == "deepseek"
                           else MAX_CHAT_REQUESTS),
        "temporary_resources": {"database_created": False, "chroma_created": False,
                                 "synthetic_repository_created": False},
        "database_provision": {"status": "not_started", "operation": "not_started",
                                "database_absence_confirmed": False,
                                "created_by_run": False},
        "cleanup": {"database": "not_created", "chroma_directory": "not_created",
                    "synthetic_repository": "not_created"},
    }


def _confirm_deepseek_consent(reader=None):
    """Require an explicit local confirmation before any DeepSeek network call."""
    reader = reader or input
    try:
        value = reader(
            f"Up to {MAX_DEEPSEEK_CHAT_REQUESTS} DeepSeek chat requests may be sent "
            "(model discovery is counted separately). Only synthetic AgentForge "
            "acceptance data will be sent to DeepSeek. "
            f"Type {DEEPSEEK_CONSENT_PHRASE} to continue: "
        )
    except (EOFError, KeyboardInterrupt):
        return False
    return isinstance(value, str) and value == DEEPSEEK_CONSENT_PHRASE


def _create_real_provider(provider_id, model_id, *, temp_root, result, stream=False):
    """Construct the explicitly selected host model; DeepSeek metadata is run-local."""
    if provider_id == "llama-server":
        from agentforge.cli import create_provider
        provider = create_provider(
            "llama-server", base_url=host_infra.CHAT_ENDPOINT, model=model_id,
            api_key=os.environ[CHAT_KEY_ENV], output_budget_mode="standard",
        )
        result["provider"].update({"model_id": model_id, "metadata_status": "configured_local"})
        return provider

    if provider_id != "deepseek":
        raise AcceptanceBlocked("provider_setup", "unsupported_host_provider")
    from agentforge.credential_store import CredentialStore
    from agentforge.model_registry import ModelRegistry
    from agentforge.providers.deepseek import DeepSeekClient, DeepSeekProvider

    key, source = CredentialStore().resolve()
    if not key:
        raise AcceptanceBlocked("provider_preflight", "deepseek_credential_missing")
    client = DeepSeekClient(key)
    client.set_credential_source(source)
    registry = ModelRegistry(cache_path=temp_root / "model_registry" / "deepseek.json")
    try:
        discovered = registry.refresh_deepseek(client)
        result["model_discovery"].update({
            "status": "PASS", "requests": int(client.request_stats().get("discovery_attempted", 0)),
            "models_returned": len(discovered), "requested_model_found": any(
                item.provider_id == "deepseek" and item.model_id == model_id for item in discovered),
        })
    except Exception:
        stats = client.request_stats()
        result["model_discovery"].update({
            "status": "FAIL", "requests": int(stats.get("discovery_attempted", 0)),
            "error_code": stats.get("last_error_code") or "model_discovery_failed",
        })
        raise
    try:
        descriptor = registry.get_for_request(model_id)
    except ValueError:
        result["model_discovery"]["requested_model_found"] = False
        raise AcceptanceBlocked("provider_setup", "deepseek_model_not_in_verified_catalog") from None
    provider = DeepSeekProvider(client, model_id, output_budget_mode="standard",
                                stream=stream, descriptor=descriptor)
    result["provider"].update({
        "provider_id": "deepseek", "model_id": descriptor.model_id,
        "metadata_status": "verified_for_this_run",
        "context_window": descriptor.context_window,
        "max_input_tokens": descriptor.max_input_tokens,
        "max_output_tokens": descriptor.max_output_tokens,
        "supports_thinking": descriptor.supports_thinking,
        "supports_tool_calling": descriptor.supports_tool_calling,
        "supports_streaming": descriptor.supports_streaming,
        "capability_source": descriptor.capability_source,
        "capability_verified_at": descriptor.capability_verified_at,
        "credential_source": source,
    })
    return provider


def _write_result(result):
    run_id = result["run_id"]
    json_path = ROOT / f"AF042_UNIFIED_AGENT_HOST_RESULT_{run_id}.json"
    report_path = ROOT / f"AF042_UNIFIED_AGENT_HOST_REPORT_{run_id}.md"
    if not RESULT_RE.fullmatch(json_path.name):
        raise RuntimeError("refusing_unscoped_result_path")
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Phase 4.2 Unified Agent Host Acceptance", "",
             f"- Status: **{result['status']}**",
             f"- Scope: **{result.get('acceptance_scope', 'full')}**",
             f"- Selected scenarios: {', '.join(result.get('selected_scenarios', ())) }",
             f"- Run ID: `{run_id}`",
             f"- Failure: `{result.get('failure_stage') or 'none'}` / `{result.get('failure_code') or 'none'}`",
             f"- Real {result['provider']['provider_id']} requests: `{result['chat_requests']}`", "",
             "## Scenario results", ""]
    for name, scenario in result.get("scenarios", {}).items():
        lines.append(f"- {name}: **{scenario.get('status', 'NOT_MEASURED')}**")
        if scenario.get("partial_reason"):
            lines.append(f"  - Partial reason: `{scenario['partial_reason']}`")
        if scenario.get("provider_token_streaming"):
            lines.append(f"  - Provider token streaming: `{scenario['provider_token_streaming']}`")
        if scenario.get("failure_code"):
            lines.append(f"  - Failure: `{scenario.get('failure_stage') or name}` / `{scenario['failure_code']}`")
        for key, value in scenario.get("assertions", {}).items():
            lines.append(f"  - {key}: `{value}`")
        trace = scenario.get("trace")
        conversation_planner = (trace.get("conversation_planner_diagnostics")
                                if isinstance(trace, dict) else None)
        if isinstance(conversation_planner, dict):
            lines.append("  - Conversation Planner validation: "
                         f"initial={conversation_planner.get('initial_validation_status')}, "
                         f"correction={conversation_planner.get('correction_status', 'not_needed')}, "
                         f"final={conversation_planner.get('final_validation_status')}, "
                         f"requests={conversation_planner.get('planner_request_count')}")
        diagnostics = (trace.get("agent_planner_request_diagnostics", ())
                      if isinstance(trace, dict) else ())
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            lines.append("  - Agent Planner request: "
                         f"phase={item.get('phase')}, failure={item.get('failure_code')}, "
                         f"required={item.get('required_tokens')}, budget={item.get('budget_tokens')}, "
                         f"sent={item.get('request_sent')}, exception={item.get('exception_type')}, "
                         f"provider_phase={item.get('provider_failure_phase')}, "
                         f"provider_code={item.get('provider_failure_code')}, "
                         f"response_validation={item.get('response_validation')}, "
                         f"planning_phase={item.get('planning_failure_phase')}, "
                     f"planning_reason={item.get('planning_failure_reason')}, "
                     f"planning_details={item.get('planning_failure_details')}")
        diagnostic = scenario.get("failure_diagnostic")
        if isinstance(diagnostic, dict):
            lines.append(f"  - Diagnostic: {diagnostic.get('phase')} / {diagnostic.get('reason')}")
            provider_state = diagnostic.get("provider") or {}
            lines.append("  - Planner response: "
                         f"finish={provider_state.get('last_finish_reason')}, "
                         f"application_error={provider_state.get('last_application_error_code')}")
            encoded = json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, indent=2)
            lines.extend(["  - Safe diagnostic details:", "", "```json",
                          *["  " + line for line in encoded.splitlines()], "```"])
    lines.extend(["", "## Checks", ""])
    lines.extend(f"- {key}: **{value}**" for key, value in result.get("checks", {}).items())
    provision = result.get("database_provision", {})
    lines.extend(["", "## Database provisioning", "",
                  f"- Status: `{provision.get('status', 'NOT_MEASURED')}`",
                  f"- Last operation: `{provision.get('operation', 'NOT_MEASURED')}`",
                  f"- Database created by this run: `{provision.get('created_by_run', 'NOT_RECORDED')}`",
                  f"- Database currently present: `{result.get('temporary_resources', {}).get('database_created', False)}`"])
    diagnostic = provision.get("diagnostic")
    if isinstance(diagnostic, dict):
        lines.append(f"- Diagnostic: `{diagnostic.get('exception_type')}` / SQLSTATE `{diagnostic.get('sqlstate') or 'unknown'}`")
        if diagnostic.get("message"):
            lines.append(f"- Safe error: {diagnostic['message']}")
    lines.extend(["", "## Cleanup", ""])
    lines.extend(f"- {key}: `{value}`" for key, value in result.get("cleanup", {}).items())
    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, report_path


def _finish(result, status, stage=None, code=None):
    result.update(status=status, failure_stage=stage, failure_code=code)
    json_path, report_path = _write_result(result)
    failed_scenarios = [
        {"scenario": name, "diagnostic": scenario.get("failure_diagnostic")}
        for name, scenario in result.get("scenarios", {}).items()
        if scenario.get("status") == "FAIL" and scenario.get("failure_diagnostic")
    ][:3]
    print(json.dumps({"result_file": str(json_path), "report_file": str(report_path),
                      "status": status, "failure_stage": stage, "failure_code": code,
                      "checks": result["checks"], "chat_requests": result["chat_requests"],
                      "scenario_failure_diagnostics": failed_scenarios,
                      "cleanup": result["cleanup"]}, ensure_ascii=False, indent=2))
    targeted_pass = (status == "PARTIAL"
                     and result.get("checks", {}).get("selected_scenarios") == "PASS")
    return 0 if status == "PASS" or targeted_pass else 2 if status == "BLOCKED" else 1


def _event_callback(events):
    return lambda event: events.append(_safe_event(event))


def _agent_failure_code(events):
    """Prefer the originating stage error over the execution's generic FAILED event."""
    for event in reversed(events):
        if (isinstance(event, dict) and event.get("type") in {"PLANNER_FAILED", "PLAN_REJECTED"}
                and isinstance(event.get("error_code"), str)):
            return event["error_code"]
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "FAILED":
            code = event.get("error_code")
            if isinstance(code, str):
                return code
    return None


def _execution_tool_results(results):
    """Exclude synthetic plan-validation audit rows from actual tool outcomes."""
    return [item for item in results if not (
        isinstance(item, dict)
        and isinstance(item.get("execution_metadata"), dict)
        and item["execution_metadata"].get("stage") == "plan_validation"
    )]


def _safe_tool_call_trace(item):
    if not isinstance(item, dict):
        return None
    output = {}
    for key in ("call_id", "task_id", "tool_id"):
        value = item.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
            output[key] = value
    for key in ("status", "error_code"):
        value = item.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
            output[key] = value
    metadata = item.get("execution_metadata")
    if isinstance(metadata, dict):
        diagnostic = _safe_tool_argument_diagnostic(metadata.get("argument_diagnostic"))
        if diagnostic:
            output["argument_diagnostic"] = diagnostic
    return output or None


def _safe_plan_validation_trace(results):
    rows = []
    for item in results:
        if not isinstance(item, dict):
            continue
        metadata = item.get("execution_metadata")
        if not isinstance(metadata, dict) or metadata.get("stage") != "plan_validation":
            continue
        row = {}
        for key in ("call_id", "task_id", "tool_id", "status", "error_code"):
            value = item.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                row[key] = value
        code = metadata.get("diagnostic_code")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            row["diagnostic_code"] = code
        details = _safe_tool_argument_diagnostic(metadata.get("diagnostic_details"))
        if details:
            row["diagnostic_details"] = details
        if row:
            rows.append(row)
    return rows


def _result_page_consumption(results):
    """Summarize Agent-consumed result pages without persisting page contents."""
    calls = []
    pages_by_ref = {}
    for item in results:
        if not isinstance(item, dict) or item.get("tool_id") != "agent.read_result_page":
            continue
        output = item.get("output") if isinstance(item.get("output"), dict) else {}
        row = {
            "call_id": item.get("call_id"),
            "status": item.get("status"),
            "error_code": item.get("error_code"),
            "result_ref": output.get("result_ref"),
            "source_call_id": output.get("source_call_id"),
            "page_number": output.get("page_number"),
            "offset": output.get("offset"),
            "next_offset": output.get("next_offset"),
            "total_chars": output.get("total_chars"),
            "complete": output.get("complete"),
            "text_chars": len(output.get("text", "")) if isinstance(output.get("text"), str) else 0,
            "source_sha256": output.get("source_sha256"),
            "page_sha256": output.get("page_sha256"),
        }
        calls.append(row)
        if (item.get("status") == "completed" and isinstance(output.get("result_ref"), str)
                and isinstance(output.get("text"), str)):
            pages_by_ref.setdefault(output["result_ref"], []).append(output)
    groups = []
    for result_ref, pages in pages_by_ref.items():
        expected_offset = 0
        complete = True
        reconstructed = []
        seen_offsets = set()
        total_chars = pages[0].get("total_chars")
        source_hashes = {page.get("source_sha256") for page in pages}
        for page_number, page in enumerate(pages, 1):
            text = page.get("text", "")
            offset = page.get("offset")
            next_offset = page.get("next_offset")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if (offset in seen_offsets or offset != expected_offset
                    or page.get("page_number") != page_number
                    or next_offset != offset + len(text)
                    or page.get("page_sha256") != digest
                    or page.get("total_chars") != total_chars
                    or (page.get("complete") is True) != (next_offset == total_chars)):
                complete = False
            if isinstance(offset, int):
                seen_offsets.add(offset)
            if isinstance(next_offset, int):
                expected_offset = next_offset
            reconstructed.append(text)
        text = "".join(reconstructed)
        complete = complete and isinstance(total_chars, int) and len(text) == total_chars and bool(
            pages and pages[-1].get("complete") is True)
        groups.append({
            "result_ref": result_ref,
            "source_call_id": pages[0].get("source_call_id"),
            "page_count": len(pages),
            "total_chars": total_chars,
            "characters_read": len(text),
            "complete": complete,
            "contiguous": complete,
            "source_sha256": next(iter(source_hashes)) if len(source_hashes) == 1 else None,
            "reconstructed_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "matches_source_digest": (len(source_hashes) == 1
                                       and next(iter(source_hashes)) is not None
                                       and hashlib.sha256(text.encode("utf-8")).hexdigest()
                                       == next(iter(source_hashes))),
        })
    return {"tool_calls": calls, "groups": groups,
            "page_count": sum(item["page_count"] for item in groups),
            "failed_page_calls": sum(item.get("status") != "completed" for item in calls)}


def _plan_validity(events, *, answer_contract=None):
    """Report planner validation from lifecycle events, not execution status."""
    if any(item.get("type") in {"PLAN_REJECTED", "PLANNER_FAILED"}
           for item in events if isinstance(item, dict)):
        return "failed"
    if answer_contract is not None or any(
            item.get("type") == "PLAN_READY" for item in events if isinstance(item, dict)):
        return "passed"
    return "NOT_MEASURED"


def _streaming_lifecycle_assertions(result, trace):
    """Validate the application event sequence and the observed synthetic answer."""
    events = trace.get("streaming_events", ())
    event_types = [item.get("type") for item in events if isinstance(item, dict)]
    required = ("STARTED", "PLAN_READY", "TOOL_CALL_STARTED", "TOOL_CALL_COMPLETED",
                "OBSERVATION_RECEIVED", "REPLANNING", "FINAL_ANSWER", "COMPLETED")
    positions = []
    cursor = 0
    for expected in required:
        try:
            position = event_types.index(expected, cursor)
        except ValueError:
            positions = []
            break
        positions.append(position)
        cursor = position + 1
    tool_completed = any(
        item.get("tool_id") == "stream_synthetic_status" and item.get("status") == "completed"
        for item in trace.get("tool_calls", ()) if isinstance(item, dict)
    )
    contract = getattr(result, "contract", None)
    answer = getattr(getattr(result, "answer", None), "text", "")
    return {
        "application_event_lifecycle_order": len(positions) == len(required),
        "synthetic_tool_observation_completed": tool_completed,
        "final_answer_uses_observation": (
            isinstance(answer, str) and "ready" in answer.casefold()
            and getattr(contract, "status", None) == "answered"
        ),
    }


def _restore_probe() -> int:
    """Read session history and a durable execution snapshot in a fresh process."""
    try:
        database = os.environ["AGENTFORGE_AF042_TEST_DATABASE"]
        conversation_id = os.environ["AGENTFORGE_AF042_CONVERSATION_ID"]
        repository_id = os.environ["AGENTFORGE_AF042_REPOSITORY_ID"]
        execution_id = os.environ["AGENTFORGE_AF042_EXECUTION_ID"]
        if not TEST_DATABASE_RE.fullmatch(database) or not os.environ.get(DB_KEY_ENV):
            raise ValueError
        engine = host_infra._create_test_engine(database, os.environ[DB_KEY_ENV])
        try:
            from agentforge.conversation_store import ConversationStore
            from agentforge.session_service import SessionService
            from agentforge.agent_execution import PostgresExecutionStateStore
            store = ConversationStore(engine)
            restored = SessionService().load_context(store, conversation_id, repository_id)
            checkpoint = PostgresExecutionStateStore(engine).load(execution_id)
            identity_verified = (checkpoint is not None
                                 and checkpoint.conversation_id == conversation_id
                                 and checkpoint.repository_id == repository_id)
            session_rows_scoped = all(row.get("conversation_id") == conversation_id
                                      for row in restored.rows)
            payload = {"status": "passed" if identity_verified and session_rows_scoped else "failed",
                       "message_count": len(restored.rows),
                       "history_turn_count": len(restored.history),
                       "checkpoint_state": getattr(checkpoint, "state", None),
                       "checkpoint_version": getattr(checkpoint, "version", None),
                       "conversation_identity_verified": identity_verified,
                       "session_rows_scoped": session_rows_scoped,
                       "session_history_status": ("reloaded" if restored.rows
                                                  else "pending_turn_not_committed")}
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if payload["status"] == "passed" and payload["conversation_identity_verified"] else 1
        finally:
            engine.dispose()
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1


def _run_restore_probe(database, conversation_id, repository_id, execution_id):
    environment = os.environ.copy()
    environment.update({
        "AGENTFORGE_AF042_TEST_DATABASE": database,
        "AGENTFORGE_AF042_CONVERSATION_ID": conversation_id,
        "AGENTFORGE_AF042_REPOSITORY_ID": repository_id,
        "AGENTFORGE_AF042_EXECUTION_ID": execution_id,
    })
    try:
        child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                "--restore-probe"], env=environment, capture_output=True,
                               text=True, timeout=30, check=False)
        payload = json.loads(child.stdout.strip().splitlines()[-1]) if child.stdout.strip() else {}
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
        return {"status": "failed", "error_code": "restore_child_unavailable"}
    payload["status"] = "passed" if child.returncode == 0 and payload.get("status") == "passed" else "failed"
    payload.pop("error_type", None) if payload.get("status") == "passed" else None
    return payload


def _register_definition(registry, tool_id, description, properties, handler, *, required=(),
                         side_effect=False, approval=False, timeout=5.0):
    host_infra._register_tool(registry, tool_id, description, properties, handler,
                              required=required, side_effect=side_effect,
                              approval=approval, timeout=timeout)


def _scenario_registry(name, state, toolbox=None):
    """Build the per-scenario registry, bound to this run's synthetic repository."""
    if name in DIRECT_ANSWER_SCENARIOS:
        # These scenarios measure direct general answers. An empty registry
        # makes that expectation explicit even when the isolated repository
        # toolbox is available to other scenarios in the same run.
        from agentforge.agent_execution import ToolRegistry
        registry = ToolRegistry()
    elif name == "long_tool_output":
        # The outer Conversation Planner handles repository retrieval and citation.
        # Keep this execution registry focused so bounded page reads fit the scenario
        # step budget without duplicating search/read work inside the Agent loop.
        from agentforge.agent_execution import ToolRegistry
        registry = ToolRegistry()
    elif name == "streaming_lifecycle":
        # This scenario measures application events from a synthetic tool observation.
        # Repository browsing is outside this assertion and is not exposed here.
        from agentforge.agent_execution import ToolRegistry
        registry = ToolRegistry()
    elif toolbox is None:
        registry = host_infra._base_registry()
    else:
        # Mirror the product's safe repository tools, but bind them to the
        # runner's isolated synthetic repository. Index mutation remains off.
        from agentforge.agent_execution import ToolRegistry, toolbox_registry
        repository_registry = toolbox_registry(
            toolbox, include_index=False, timeout_seconds=AGENT_TOOL_TIMEOUT_SECONDS,
        )
        if name in {"repository_auto", "hybrid_knowledge"}:
            # These assertions cover evidence retrieval, not symbol browsing.
            # Keep the live Planner catalog/task schemas narrow enough for the
            # selected Qwen window while preserving the real search/read handlers.
            registry = ToolRegistry()
            allowed_tools = {"search", "read"}
            for definition in repository_registry.definitions():
                if definition.tool_id in allowed_tools:
                    registry.register(definition, repository_registry.handler(definition.tool_id))
            if {item.tool_id for item in registry.definitions()} != allowed_tools:
                raise ValueError("The isolated repository scenario is missing a required read-only tool.")
        else:
            registry = repository_registry
    if name in DIRECT_ANSWER_SCENARIOS:
        pass
    elif name == "single_tool":
        _register_definition(registry, "read_synthetic_fact",
            "Read one synthetic acceptance fact without accessing files or services.", {},
            lambda _args, _ctx: {"fact": "phase4-two-local-only"})
    elif name == "multi_turn":
        _register_definition(registry, "read_synthetic_fact",
            "Read one synthetic acceptance fact without accessing files or services.", {},
            lambda _args, _ctx: {"fact": "phase4-two-local-only"})
    elif name == "multi_tool_dependency":
        def open_bundle(_args, _ctx):
            state["bundle_open"] = True
            state["call_order"].append("open_synthetic_bundle")
            return {"opened": True}
        def inspect_bundle(_args, _ctx):
            state["call_order"].append("inspect_synthetic_bundle")
            if not state.get("bundle_open"):
                raise ValueError("dependency_not_satisfied")
            return {"inspection": "synthetic-bundle-ready"}
        _register_definition(registry, "open_synthetic_bundle", "Open a synthetic in-memory bundle.", {}, open_bundle)
        _register_definition(registry, "inspect_synthetic_bundle", "Inspect the opened synthetic bundle.", {}, inspect_bundle)
    elif name == "observation_replanning":
        def read_challenge(_args, _ctx):
            return {"challenge": state["challenge"]}
        def verify_challenge(args, _ctx):
            state["verified_challenge"] = args["challenge"]
            return {"verified": args["challenge"] == state["challenge"]}
        _register_definition(registry, "read_synthetic_challenge",
            "Read the unpredictable run-local challenge value. The value is not in the request.", {}, read_challenge)
        _register_definition(registry, "verify_synthetic_challenge",
            "Verify the exact challenge value returned by read_synthetic_challenge; only call after observing it.",
            {"challenge": {"type": "string"}}, verify_challenge, required=("challenge",))
    elif name == "tool_failure_recovery":
        def fail_primary(_args, _ctx):
            from agentforge.agent_execution import ToolErrorCode, ToolExecutionFailure
            raise ToolExecutionFailure(ToolErrorCode.TOOL_UNAVAILABLE)
        def read_fallback(_args, _ctx):
            state["fallback_called"] = True
            return {"recovered": True}
        _register_definition(registry, "primary_synthetic_read", "Read the primary synthetic source; it may report unavailable.", {}, fail_primary)
        _register_definition(registry, "fallback_synthetic_read", "Read a safe synthetic fallback after the primary read fails.", {}, read_fallback)
    elif name == "approval_pause_resume":
        def approved_action(_args, _ctx):
            state["approved_action_ran"] = True
            return {"completed": True}
        _register_definition(registry, "approved_synthetic_action",
            "Perform one reversible in-memory synthetic action and require explicit user approval.", {},
            approved_action, side_effect=True, approval=True)
    elif name == "long_tool_output":
        _register_definition(registry, "read_large_synthetic_report",
            "Read the large synthetic report and return a result reference when the output is compacted.", {},
            lambda _args, _ctx: {"report": "synthetic-result-" + ("x" * 24_000)})
    elif name == "streaming_lifecycle":
        _register_definition(registry, "stream_synthetic_status",
            "Read the synthetic status required by this repository task.", {},
            lambda _args, _ctx: {"status": "ready"})
    elif name == "tool_timeout":
        def slow_read(_args, _ctx):
            import time
            time.sleep(0.35)
            return {"status": "late"}
        _register_definition(registry, "slow_synthetic_read",
            "Read a synthetic delayed source with a short timeout.", {}, slow_read, timeout=0.12)
    elif name == "tool_cancel":
        started = state["tool_started"]
        def cancellable_read(_args, context):
            started.set()
            while not context.cancel_event.is_set():
                threading.Event().wait(0.01)
            return {"status": "cooperatively_cancelled"}
        _register_definition(registry, "cancel_synthetic_read",
            "Read synthetic state until the host cancels this running tool.", {}, cancellable_read,
            timeout=5.0)
    elif name in {"repository_auto", "hybrid_knowledge"}:
        if toolbox is None:
            raise ValueError("Repository scenarios require an isolated synthetic read-only toolbox.")
        # Search/read are sufficient for these evidence assertions; exposing
        # browsing tools here would enlarge the real-Qwen planner catalog.
    else:
        raise ValueError("No scenario-specific synthetic tool plan is registered.")
    return registry


def _scenario_question(name):
    prompts = {
        "general_direct": "In one short paragraph, explain why deterministic tests are useful.",
        "repository_auto": "Inspect the current synthetic repository's compute_total function, explain its return behavior, and cite the source.",
        "hybrid_knowledge": "Explain in general what a pure function is, then separately describe whether synthetic compute_total is pure using repository evidence and a citation.",
        "single_tool": "Use the read_synthetic_fact tool once. Then inspect synthetic compute_total and answer with its source citation.",
        "multi_tool_dependency": "Use open_synthetic_bundle first, then inspect_synthetic_bundle only after the opening observation. Also cite compute_total from the synthetic repository.",
        "observation_replanning": "Read the synthetic challenge first. After observing its unpredictable value, call verify_synthetic_challenge with exactly that observed value. Then explain compute_total with a source citation.",
        "tool_failure_recovery": (
            "Try primary_synthetic_read once. If its observation reports TOOL_UNAVAILABLE, "
            "replan and use fallback_synthetic_read. Report whether recovery succeeded."
        ),
        "approval_pause_resume": "Use approved_synthetic_action, which requires explicit approval, then explain compute_total with a source citation.",
        "long_tool_output": "Use read_large_synthetic_report and rely on the returned result reference rather than repeating the entire report. Cite compute_total.",
        "long_answer": (
            "Write a detailed guide of at least 900 words with ten numbered sections about the fictional "
            "Amber Ledger scheduler. Its complete specification is: jobs have an ID, priority (urgent or "
            "normal), and submission sequence; urgent jobs run before normal jobs; within a priority, lower "
            "submission sequence runs first; duplicate IDs are rejected; completed jobs are never requeued; "
            "empty input produces an empty schedule. Explain the rules, invariants, edge cases, and examples "
            "using only this specification. Do not add features or claim external facts."
        ),
        "multi_turn": "Recall information from earlier turns in this conversation.",
        "session_process_restore": "The previously completed synthetic Agent task is being restored; no new tool call is requested.",
        "streaming_lifecycle": "Use stream_synthetic_status to read the synthetic status and report the observed status in one sentence.",
        "tool_timeout": "Call slow_synthetic_read once, observe its timeout, then explain compute_total with a source citation.",
        "tool_cancel": "Call cancel_synthetic_read once and stop when the host cancels the running tool.",
    }
    return prompts[name]


def _multi_turn_queries(run_id):
    """Keep each multi-turn request within one answer intent and test persisted history."""
    marker = "AF42-" + run_id[:8]
    return (
        f"For the next turn, reply with exactly this synthetic label and no other text: {marker}.",
        "Call read_synthetic_fact exactly once. Reply with the exact fact returned by the tool and no other text.",
        "Without calling any tools, repeat the exact synthetic label from the first turn and the exact fact read by the tool in the second turn, one value per line.",
    )


def _fresh_session(conversation_store, session_service, conversation_id, repository_id, provider):
    conversation = conversation_store.get_conversation(conversation_id, repository_id)
    restored = session_service.load_context(conversation_store, conversation_id, repository_id)
    # ConversationStore exposes the persistent primary key as "id". The
    # ContextAssembler's request scope uses "conversation_id"; provide both
    # explicitly so restored ConversationTurn rows are not treated as stateless.
    conversation["conversation_id"] = conversation_id
    return __import__("agentforge.conversation_orchestrator", fromlist=["ConversationSession"]).ConversationSession(
        provider=provider, store=conversation_store, repository_id=repository_id,
        rows=restored.rows, conversation={**conversation,
            "summary": restored.summary.get("content") if isinstance(restored.summary, dict) else None},
    )


def _orchestrated_turn(*, name, query, provider, engine, index_store, conversation_store,
                       session_service, repository, repository_id, conversation_id,
                       cancel_event=None, stream=False, execution_id=None,
                       approval_decision=None, challenge=None, cancel_on_tool_start=False,
                       output_budget_mode="standard"):
    from agentforge.agent_execution import LLMExecutionPlanner, ToolAuthorization
    from agentforge.cli import _model_input_budget
    from agentforge.conversation_agent import ConversationAgentExecutor
    from agentforge.conversation_orchestrator import (
        ConversationDependencies, ConversationOrchestrator, ConversationRequest,
    )
    from agentforge.planner import LLMPlanner

    state = {"challenge": challenge, "call_order": [], "tool_started": threading.Event()}
    run_cancel_event = cancel_event or threading.Event()
    recorders = []
    def planner_factory(selected_provider, **kwargs):
        delegate = LLMExecutionPlanner(selected_provider, **kwargs)
        recorder = host_infra.RecordingRealPlanner(delegate, scenario=name)
        recorders.append(recorder)
        return recorder
    def registry_factory(toolbox):
        return _scenario_registry(name, state, toolbox)
    def authorization_factory(request, _repository_id):
        # This runner intentionally grants only repository reads and the two
        # synthetic test scopes. The registry contains no indexing tool.
        scopes = frozenset({"repository:read", "synthetic:read", "synthetic:action"})
        def approve(definition, _call):
            if not definition.requires_approval:
                return True
            return True if request.approval_decision == "approve" else (
                False if request.approval_decision == "deny" else None)
        return ToolAuthorization(permission_scopes=scopes, approval_handler=approve)

    executor = ConversationAgentExecutor(
        engine=engine, max_steps=AGENT_EXECUTION_MAX_STEPS,
        tool_timeout_seconds=AGENT_TOOL_TIMEOUT_SECONDS,
        run_timeout_seconds=AGENT_RUN_TIMEOUT_SECONDS,
        tool_registry_factory=registry_factory, planner_factory=planner_factory,
        authorization_factory=authorization_factory,
    )
    dependencies = ConversationDependencies(
        provider_factory=lambda *_args, **_kwargs: provider,
        model_budget_resolver=_model_input_budget,
        planner=LLMPlanner(), session_service=session_service, agent_executor=executor,
    )
    provider_id = getattr(provider, "provider_id", "llama-server")
    model_id = getattr(provider, "model", host_infra.CHAT_MODEL)
    request = ConversationRequest(
        query=query, repository=str(repository), provider_id=provider_id,
        model_id=model_id,
        base_url=host_infra.CHAT_ENDPOINT if provider_id == "llama-server" else None,
        conversation_id=conversation_id, mode="auto", stream=stream,
        max_output_tokens=None, output_budget_mode=output_budget_mode,
        execution_id=execution_id,
        approval_decision=approval_decision,
        cloud_data_authorized=(provider_id == "deepseek"),
    )
    events = []
    session = _fresh_session(conversation_store, session_service, conversation_id,
                             repository_id, provider)
    stats_before = _counts(provider.request_stats())
    telemetry_before = len(provider.request_telemetry)
    orchestrator = ConversationOrchestrator(dependencies)
    watchdog_done = threading.Event()
    watchdog = None
    if cancel_on_tool_start:
        def cancel_running_tool():
            while not watchdog_done.is_set():
                if state["tool_started"].wait(0.05):
                    run_cancel_event.set()
                    return
        watchdog = threading.Thread(target=cancel_running_tool,
                                    name="af042-tool-cancel-watchdog", daemon=True)
        watchdog.start()
    try:
        result = orchestrator.run(request, index_store=index_store, session=session,
                                  on_event=_event_callback(events), cancel_event=run_cancel_event)
    except Exception as exc:
        _attach_scenario_failure_diagnostics(
            exc, provider=provider, telemetry_before=telemetry_before,
            orchestrator=orchestrator, recorders=recorders, events=events,
        )
        raise
    finally:
        watchdog_done.set()
        if watchdog is not None:
            watchdog.join(timeout=1.0)
    stats_after = _counts(provider.request_stats())
    plan = result.retrieval if isinstance(result.retrieval, dict) else {}
    execution = plan.get("agent_execution") or plan.get("tool_execution") or {}
    tools = execution.get("tool_results", execution.get("results", []))
    if not isinstance(tools, list):
        tools = []
    all_tool_results = tools
    plan_validation_diagnostics = _safe_plan_validation_trace(all_tool_results)
    tools = _execution_tool_results(all_tool_results)
    planner_decisions = [decision for recorder in recorders for decision in recorder.decisions]
    planner_requests = sum(len(recorder.request_diagnostics) for recorder in recorders)
    planner_diagnostics = [safe for recorder in recorders
                           for item in recorder.request_diagnostics
                           if (safe := _safe_agent_planner_diagnostic(item)) is not None]
    agent_failure_code = _agent_failure_code(events)
    replanning_attempts, successful_replans = _replanning_counts(events)
    current = conversation_store.get_messages(conversation_id, repository_id)
    stats = provider.request_stats()
    conversation_planner = _safe_conversation_planner_diagnostic(
        getattr(orchestrator.planner, "last_request_diagnostics", None))
    trace = {
        "provider": provider_id, "model": model_id,
        "session_id": conversation_id,
        "planner_request_count": (
            conversation_planner.get("planner_request_count", 1 if plan.get("planner_source") == "llm" else 0)
            if conversation_planner else (1 if plan.get("planner_source") == "llm" else 0)
        ) + planner_requests,
        "conversation_planner_diagnostics": conversation_planner,
        "selected_route": plan.get("mode") or plan.get("route"),
        "requires_agent_execution": plan.get("requires_agent_execution"),
        "planned_task_types": sorted({item.get("task_type") for item in plan.get("tasks", ())
                                       if isinstance(item, dict)
                                       and item.get("task_type") in {"general", "repository", "external", "tool"}}),
        "task_count": len(plan.get("tasks", ())) if isinstance(plan.get("tasks"), (list, tuple)) else 0,
        "task_type_counts": {
            task_type: sum(1 for item in plan.get("tasks", ())
                           if isinstance(item, dict) and item.get("task_type") == task_type)
            for task_type in ("general", "repository", "external", "tool")
        } if isinstance(plan.get("tasks"), (list, tuple)) else {},
        "executable_task_count": sum(1 for item in plan.get("tasks", ())
                                     if isinstance(item, dict) and item.get("task_type") == "tool")
                                if isinstance(plan.get("tasks"), (list, tuple)) else 0,
        "execution_started": bool(planner_requests or tools
                                   or any(item.get("type") == "TOOL_DISCOVERY" for item in events)),
        "plan_validity": _plan_validity(events, answer_contract=result.contract),
        "tool_calls": [safe for item in tools
                        if (safe := _safe_tool_call_trace(item)) is not None],
        "plan_validation_diagnostics": plan_validation_diagnostics,
        "result_page_consumption": _result_page_consumption(tools),
        "observation_count": execution.get("observation_count", len(tools)),
        "replanning_count": replanning_attempts,
        "successful_replanning_count": successful_replans,
        "execution_status": result.execution_status,
        "answer_contract_status": getattr(result.contract, "status", None),
        "contract_failure_code": getattr(result.contract, "failure_code", None),
        "session_restore_status": "messages_reloaded" if current is not None else "failed",
        "persisted_message_count": len(current),
        "token_usage": _usage_summary(provider.request_telemetry[telemetry_before:]),
        "finish_reason": stats.get("last_finish_reason") if isinstance(stats, dict) else None,
        "request_counts": _delta(stats_after, stats_before),
        "planner_decisions": _sanitize_decisions(planner_decisions),
        "plan_rejections": [{"sequence": item.get("sequence"),
                             "error_code": item.get("error_code")}
                            for item in events if item.get("type") == "PLAN_REJECTED"],
        "agent_planner_request_diagnostics": planner_diagnostics,
        "agent_failure_code": agent_failure_code,
        "streaming_events": events,
        "answer_digest": _digest(result.answer.text),
        "answer_characters": len(result.answer.text),
        "output_budget": _safe_output_budget(provider),
        "answer_request_output_tokens": next((item.get("requested_output_tokens")
            for item in reversed(provider.request_telemetry[telemetry_before:])
            if item.get("operation") in {"answer", "answer_general", "answer_hybrid"}
            and item.get("sent", 0) > 0), None),
        "execution_id": execution.get("execution_id") or (result.agent_execution or {}).get("execution_id"),
        "state": state,
    }
    return result, trace


def _scenario_shell(name):
    return {"scenario": name, "status": "NOT_MEASURED", "assertions": {},
            "trace": None, "failure_stage": None, "failure_code": None}


def _run_scenario(name, *, provider, engine, index_store, conversation_store,
                  session_service, repository, repository_id, run_id,
                  cancel_event=None, approve_synthetic_actions=False):
    scenario = _scenario_shell(name)
    if name == "long_answer" and getattr(provider, "provider_id", None) != "deepseek":
        scenario.update(status="NOT_APPLICABLE",
                        reason="Long-answer host acceptance targets DeepSeek; Qwen keeps its verified local cap.")
        return scenario
    output_budget_mode = "long" if name == "long_answer" else "standard"
    output_budget = provider.select_output_budget(output_budget_mode)
    conversation = conversation_store.create_conversation(repository_id, f"AF042 {name}")
    conversation_id = conversation["id"]
    state_nonce = uuid4().hex[:16]
    cancel_event = cancel_event or threading.Event()
    multi_turn_queries = _multi_turn_queries(run_id) if name == "multi_turn" else ()
    query = multi_turn_queries[0] if multi_turn_queries else _scenario_question(name)
    if name == "observation_replanning":
        query = query + " The challenge value is unpredictable and unknown until the read tool returns."
    try:
        if getattr(provider, "provider_id", None) == "deepseek":
            conversation_store.update_model_binding(
                conversation_id, repository_id, provider_id="deepseek",
                model_id=provider.model,
                cloud_consent_at=datetime.now(timezone.utc),
            )
        turn_results = []
        if name == "multi_turn":
            for turn_number, turn_query in enumerate(multi_turn_queries, start=1):
                scenario["active_turn"] = turn_number
                turn_results.append(_orchestrated_turn(
                    name=name, query=turn_query, provider=provider, engine=engine,
                    index_store=index_store, conversation_store=conversation_store,
                    session_service=session_service, repository=repository,
                    repository_id=repository_id, conversation_id=conversation_id,
                    cancel_event=cancel_event,
                    output_budget_mode=output_budget_mode,
                ))
            scenario.pop("active_turn", None)
            first_result, first_trace = turn_results[0]
            result, trace = turn_results[1]  # The tool turn is the primary execution trace.
            followup_result, followup_trace = turn_results[2]
            marker = "AF42-" + run_id[:8]
            fact = "phase4-two-local-only"
            scenario["turn_traces"] = [
                {"turn": 1, "trace": {key: value for key, value in first_trace.items()
                                         if key != "state"}},
                {"turn": 2, "trace": {key: value for key, value in trace.items()
                                         if key != "state"}},
                {"turn": 3, "trace": {key: value for key, value in followup_trace.items()
                                         if key != "state"}},
            ]
            scenario["assertions"]["first_turn_stored_label_in_session"] = (
                first_result.execution_status == "completed"
                and first_trace.get("selected_route") == "general"
                and not first_trace.get("tool_calls")
                and marker in first_result.answer.text
                and first_trace.get("persisted_message_count") == 2)
            scenario["assertions"]["second_turn_used_tool_result"] = (
                _single_synthetic_tool_call_completed(trace, "read_synthetic_fact")
                and fact in result.answer.text
                and trace.get("selected_route") == "general"
                and trace.get("persisted_message_count") == 4)
            scenario["assertions"]["followup_restored_session_context"] = (
                followup_result.execution_status == "completed"
                and followup_trace.get("selected_route") == "general"
                and not followup_trace.get("tool_calls")
                and marker in followup_result.answer.text
                and fact in followup_result.answer.text
                and followup_trace.get("persisted_message_count") == 6)
            turn_traces = (first_trace, trace, followup_trace)
            scenario["assertions"]["same_session_history_restored_each_turn"] = (
                [item.get("session_id") for item in turn_traces] == [conversation_id] * 3
                and [item.get("persisted_message_count") for item in turn_traces] == [2, 4, 6]
                and all(item.get("session_restore_status") == "messages_reloaded"
                        and item.get("plan_validity") == "passed"
                        for item in turn_traces))
        else:
            result, trace = _orchestrated_turn(
                name=name, query=query, provider=provider, engine=engine, index_store=index_store,
                conversation_store=conversation_store, session_service=session_service,
                repository=repository, repository_id=repository_id, conversation_id=conversation_id,
                cancel_event=cancel_event, stream=(name == "streaming_lifecycle"),
                challenge=state_nonce,
                cancel_on_tool_start=(name == "tool_cancel"),
                output_budget_mode=output_budget_mode,
            )
        scenario["trace"] = {key: value for key, value in trace.items() if key != "state"}
        if name == "multi_turn":
            scenario["trace"]["conversation_turn_count"] = 3
            scenario["trace"]["final_persisted_message_count"] = (
                followup_trace.get("persisted_message_count"))
        scenario["assertions"]["unified_orchestrator_completed_turn"] = (
            result.execution_status == "completed" or name in {
                "approval_pause_resume", "tool_timeout", "tool_cancel"})
        scenario["assertions"].update(_lifecycle_assertions(result, trace))
        if name == "general_direct":
            scenario["assertions"]["general_route_without_tools"] = (
                _direct_general_answer_without_tools(trace))
        elif name == "repository_auto":
            scenario["assertions"]["repository_route_with_citation"] = (
                trace["selected_route"] == "repository" and result.contract is not None
                and bool(result.contract.claims))
        elif name == "hybrid_knowledge":
            scenario["assertions"]["hybrid_route_and_contract"] = (
                trace["selected_route"] == "hybrid" and result.contract is not None)
        elif name == "long_answer":
            scenario["assertions"]["deepseek_long_budget_applied"] = (
                output_budget.mode == "long"
                and output_budget.effective_tokens >= 65_536
                and trace.get("answer_request_output_tokens") == output_budget.effective_tokens)
            scenario["assertions"]["complete_long_response"] = (
                _direct_general_answer_without_tools(trace)
                and trace["execution_status"] == "completed"
                and trace.get("finish_reason") == "stop"
                and trace.get("answer_characters", 0) >= 4_000)
        elif name == "single_tool":
            scenario["assertions"]["one_synthetic_tool_executed"] = (
                _single_synthetic_tool_call_completed(trace, "read_synthetic_fact"))
        elif name == "multi_tool_dependency":
            call_order = trace["state"].get("call_order", [])
            scenario["assertions"]["both_tools_ran_in_dependency_order"] = (
                _required_tools_ran_in_order(
                    call_order, ("open_synthetic_bundle", "inspect_synthetic_bundle")))
        elif name == "observation_replanning":
            verify_seen = any(call.get("tool_id") == "verify_synthetic_challenge"
                              for decision in trace["planner_decisions"]
                              for call in decision.get("selected_calls", []))
            scenario["assertions"]["observation_changed_next_plan"] = (
                trace["replanning_count"] >= 1 and verify_seen
                and trace["state"].get("verified_challenge") == trace["state"].get("challenge"))
        elif name == "tool_failure_recovery":
            statuses = [(call.get("tool_id"), call.get("status"), call.get("error_code"))
                        for call in trace["tool_calls"]]
            scenario["assertions"]["failure_observed_then_fallback"] = (
                any(tool == "primary_synthetic_read" and status == "failed"
                    for tool, status, _ in statuses)
                and trace["state"].get("fallback_called") is True
                and trace["replanning_count"] >= 1)
        elif name == "approval_pause_resume":
            scenario["assertions"]["initial_approval_pause"] = result.execution_status == "waiting_approval"
            if result.execution_status == "waiting_approval":
                execution_id = (result.agent_execution or {}).get("execution_id")
                if not execution_id:
                    execution_id = result.retrieval.get("tool_execution", {}).get("execution_id")
                if not execution_id:
                    raise AcceptanceBlocked(name, "approval_checkpoint_identity_missing")
                probe = _run_restore_probe(_database_name(run_id), conversation_id,
                                           repository_id, execution_id)
                scenario["checkpoint_restore_probe"] = probe
                scenario["assertions"]["approval_checkpoint_restored"] = (
                    probe.get("status") == "passed"
                    and probe.get("checkpoint_state") == "WAITING_APPROVAL"
                    and probe.get("conversation_identity_verified") is True)
                if approve_synthetic_actions:
                    resumed, resumed_trace = _orchestrated_turn(
                        name=name, query=query, provider=provider, engine=engine,
                        index_store=index_store, conversation_store=conversation_store,
                        session_service=session_service, repository=repository,
                        repository_id=repository_id, conversation_id=conversation_id,
                        execution_id=execution_id, approval_decision="approve",
                    )
                    scenario["resume_trace"] = {
                        key: value for key, value in resumed_trace.items() if key != "state"
                    }
                    scenario["assertions"]["approved_execution_resumed"] = (
                        resumed.execution_status == "completed")
                    scenario["assertions"]["explicit_approval_ran_action"] = (
                        resumed_trace["state"].get("approved_action_ran") is True)
                else:
                    scenario["approval_resume_status"] = "NOT_MEASURED"
                    scenario["partial_reason"] = "explicit_user_approval_not_granted"
                    scenario["assertions"]["action_not_run_without_approval"] = (
                        trace["state"].get("approved_action_ran") is not True)
            else:
                scenario["assertions"]["approved_execution_resumed"] = False
        elif name == "long_tool_output":
            large_call = next((item for item in trace["tool_calls"]
                               if item.get("tool_id") == "read_large_synthetic_report"
                               and item.get("status") == "partial"), None)
            result_page = None
            if large_call and trace.get("execution_id"):
                from agentforge.agent_execution import PostgresToolResultStore
                result_ref = (f"agent-result:{trace['execution_id']}:{large_call['call_id']}")
                expected_output = json.dumps(
                    {"report": "synthetic-result-" + ("x" * 24_000)},
                    ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                )
                expected_digest = hashlib.sha256(expected_output.encode("utf-8")).hexdigest()
                result_page = _read_result_pages(
                    PostgresToolResultStore(engine), result_ref,
                    expected_sha256=expected_digest,
                )
                result_page["reference_resolved"] = True
            scenario["result_store_probe"] = result_page or {"reference_resolved": False}
            agent_page_groups = trace.get("result_page_consumption", {}).get("groups", [])
            consumed_source = next((item for item in agent_page_groups
                                   if large_call is not None
                                   and item.get("source_call_id") == large_call.get("call_id")), None)
            scenario["assertions"]["long_tool_result_bounded"] = (
                large_call is not None and result_page is not None
                and isinstance(result_page.get("total_chars"), int)
                and result_page["total_chars"] > 16_000
                and 0 < result_page.get("first_page_chars", 0) < result_page["total_chars"]
                and trace["execution_status"] == "completed")
            scenario["assertions"]["long_tool_result_reconstructed_without_loss"] = (
                result_page is not None and result_page.get("complete") is True
                and result_page.get("characters_read") == result_page.get("total_chars")
                and result_page.get("pages_read", 0) > 1
                and result_page.get("matches_expected") is True)
            scenario["assertions"]["agent_consumed_all_result_pages"] = (
                consumed_source is not None
                and consumed_source.get("complete") is True
                and consumed_source.get("contiguous") is True
                and consumed_source.get("characters_read") == consumed_source.get("total_chars")
                and consumed_source.get("matches_source_digest") is True
                and consumed_source.get("page_count", 0) > 1)
            scenario["assertions"]["long_tool_final_answer_supported"] = (
                _long_tool_answer_is_supported(result))
        elif name == "streaming_lifecycle":
            scenario["assertions"].update(_streaming_lifecycle_assertions(result, trace))
            if getattr(provider, "provider_id", None) == "deepseek":
                scenario["provider_token_streaming"] = (
                    "VALIDATED_FOR_THIS_RUN" if getattr(provider, "stream", False) else
                    "NOT_MEASURED")
            else:
                scenario["provider_token_streaming"] = "NOT_MEASURED"
        elif name == "tool_timeout":
            scenario["assertions"]["tool_timeout_was_observed"] = _tool_timeout_was_observed(trace)
        elif name == "tool_cancel":
            scenario["assertions"]["running_tool_was_cancelled"] = (
                trace["execution_status"] == "cancelled"
                and any(call.get("tool_id") == "cancel_synthetic_read"
                        and call.get("status") == "cancelled"
                        for call in trace["tool_calls"]))
        assertions_passed = bool(scenario["assertions"]) and all(
            value is True or value == "NOT_APPLICABLE"
            for value in scenario["assertions"].values())
        provider_streaming_unmeasured = (
            name == "streaming_lifecycle"
            and not getattr(provider, "stream", False)
        )
        approval_resume_unmeasured = (
            name == "approval_pause_resume"
            and scenario.get("approval_resume_status") == "NOT_MEASURED"
        )
        scenario["status"] = (
            "FAIL" if not assertions_passed else
            "PARTIAL" if provider_streaming_unmeasured or approval_resume_unmeasured else "PASS"
        )
        if scenario["status"] == "PARTIAL":
            if provider_streaming_unmeasured:
                scenario["partial_reason"] = "provider_token_streaming_not_measured"
        if scenario["status"] == "FAIL":
            scenario["failure_stage"] = name
            scenario["failure_code"] = trace.get("agent_failure_code") or "scenario_assertion_failed"
        return scenario
    except AcceptanceBlocked:
        raise
    except Exception as exc:
        failure_diagnostic = _safe_scenario_failure_diagnostic(exc, provider)
        scenario.update(status="FAIL", failure_stage=name,
                        failure_code="planning_error" if type(exc).__name__ == "PlanningError"
                        else "operation_failed",
                        error_type=type(exc).__name__)
        if failure_diagnostic:
            scenario["failure_diagnostic"] = failure_diagnostic
        return scenario


def _database_name(run_id):
    return "af033_agent_" + run_id[:20]


def _safe_database_error(exc, *, secrets=()):
    """Return a bounded, credential-redacted PostgreSQL diagnostic."""
    current = exc
    seen = set()
    while id(current) not in seen:
        seen.add(id(current))
        original = getattr(current, "orig", None)
        if original is None:
            break
        current = original
    diag = getattr(current, "diag", None)
    message = getattr(diag, "message_primary", None) or str(current)
    if not isinstance(message, str):
        message = type(current).__name__
    for secret in secrets:
        if isinstance(secret, str) and secret:
            message = message.replace(secret, "[REDACTED]")
    message = re.sub(r"(?i)(postgres(?:ql)?(?:\+\w+)?://)[^\s/@]+(?::[^\s/@]*)?@",
                     r"\1[REDACTED]@", message)
    message = re.sub(
        r"(?i)\b(password|passfile|sslkey)\s*=\s*(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
        lambda match: f"{match.group(1)}=[REDACTED]", message)
    message = " ".join(message.split())[:400]
    sqlstate = (getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
                or getattr(diag, "sqlstate", None))
    if not isinstance(sqlstate, str) or not re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
        sqlstate = None
    return {"exception_type": type(exc).__name__, "sqlstate": sqlstate,
            "message": message or type(current).__name__}


def _safe_runner_error(exc, operation, *, secrets=()):
    """Describe a runner failure without persisting credentials or model content."""
    return {"operation": operation, **_safe_database_error(exc, secrets=secrets)}


def _provision_database(result, run_id, admin_password, *, provisioner=None):
    """Create one run-scoped DB and record ownership before any later failure."""
    provisioner = provisioner or host_infra._create_test_database
    database = _database_name(run_id)
    if not TEST_DATABASE_RE.fullmatch(database) or database == "agentforge":
        raise AcceptanceBlocked("database_provision", "disposable_database_name_invalid")
    state = result.setdefault("database_provision", {})
    state.update(status="running", operation="admin_connection",
                 database_name=database, database_absence_confirmed=False)
    result["temporary_resources"]["database_name"] = database

    def record_stage(operation):
        state["operation"] = operation
        if operation == "create_database":
            state["create_attempted"] = True

    def record_absent(confirmed_database):
        if confirmed_database != database:
            raise RuntimeError("database_provision_identity_mismatch")
        state["database_absence_confirmed"] = True

    def record_created(created_database):
        if created_database != database:
            raise RuntimeError("database_provision_identity_mismatch")
        result["temporary_resources"]["database_created"] = True
        result["cleanup"]["database"] = "pending"
        state.update(status="created", created_by_run=True)

    try:
        created = provisioner(run_id, admin_password, on_stage=record_stage,
                              on_database_absent=record_absent, on_created=record_created)
        if created != database:
            raise RuntimeError("database_provision_identity_mismatch")
        if not result["temporary_resources"]["database_created"]:
            # Keep compatibility with a provisioner that returns only after the
            # CREATE command was acknowledged; production helper always calls back.
            record_created(created)
        state.update(status="created", operation="complete")
        return created
    except Exception as exc:
        safe_error = _safe_database_error(exc, secrets=(admin_password, os.environ.get(DB_KEY_ENV)))
        state["diagnostic"] = {"operation": state.get("operation"), **safe_error}
        if result["temporary_resources"]["database_created"]:
            state["status"] = "created_cleanup_required"
            result["cleanup"]["database"] = "pending"
        elif (state.get("operation") == "create_database"
              and state.get("database_absence_confirmed")
              and host_infra._database_create_outcome_ambiguous(safe_error["sqlstate"])):
            # The connection may have been lost after the server committed CREATE.
            # Do not claim absence or drop anything without host reconciliation.
            state["status"] = "creation_outcome_unknown"
            result["cleanup"]["database"] = "unverified"
        else:
            state["status"] = "not_created"
            result["cleanup"]["database"] = "not_created"
        if isinstance(exc, host_infra.HostAcceptanceBlocked):
            raise AcceptanceBlocked(exc.stage, exc.code) from exc
        raise


def _cleanup_database(result, database, admin_password, *, dropper=None):
    """Clean only a database explicitly acknowledged as created by this run."""
    cleanup = result["cleanup"]
    if cleanup.get("database") == "removed":
        return "removed"
    if not result.get("temporary_resources", {}).get("database_created"):
        provision_status = result.get("database_provision", {}).get("status")
        if provision_status == "creation_outcome_unknown":
            cleanup["database"] = "unverified"
        elif cleanup.get("database") not in {"failed", "unverified"}:
            cleanup["database"] = "not_created"
        return cleanup["database"]
    if (not isinstance(database, str) or database == "agentforge"
            or not TEST_DATABASE_RE.fullmatch(database)
            or database != result.get("temporary_resources", {}).get("database_name")):
        cleanup["database"] = "failed"
        cleanup["database_diagnostic"] = {"code": "refusing_unowned_database"}
        return "failed"
    try:
        (dropper or host_infra._drop_test_database)(database, admin_password)
    except Exception as exc:
        cleanup["database"] = "failed"
        cleanup["database_diagnostic"] = _safe_database_error(
            exc, secrets=(admin_password, os.environ.get(DB_KEY_ENV)))
        return "failed"
    cleanup["database"] = "removed"
    result["temporary_resources"]["database_created"] = False
    return "removed"


def _main() -> int:
    if sys.argv[1:] == ["--restore-probe"]:
        return _restore_probe()
    args = _parse_runner_args(sys.argv[1:])
    selected_scenarios = _selected_scenarios(sys.argv[1:])
    provider_id = args.provider
    model_id = args.model or host_infra.CHAT_MODEL
    result = _result_template(selected_scenarios, provider_id=provider_id, model_id=model_id)
    run_id = result["run_id"]
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        result["checks"]["host_network"] = "BLOCKED"
        return _finish(result, "BLOCKED", "environment_gate", "network_disabled_by_environment")
    if provider_id == "llama-server":
        if not host_infra._safe_listener_probe():
            result["checks"]["qwen_chat"] = "BLOCKED"
            return _finish(result, "BLOCKED", "qwen_preflight", "qwen_chat_listener_absent")
        try:
            result["qwen_health"] = host_infra._health_ready()
            result["checks"]["qwen_chat"] = "PASS"
        except host_infra.HostAcceptanceBlocked as exc:
            return _finish(result, "BLOCKED", exc.stage, exc.code)
    if not host_infra._safe_listener_probe("127.0.0.1", 5432):
        result["checks"]["postgresql"] = "BLOCKED"
        return _finish(result, "BLOCKED", "postgres_preflight", "postgresql_listener_absent")
    result["checks"]["postgresql_listener"] = "PASS"
    missing = [DB_KEY_ENV] if not os.environ.get(DB_KEY_ENV) else []
    if provider_id == "llama-server" and not os.environ.get(CHAT_KEY_ENV):
        missing.append(CHAT_KEY_ENV)
    if provider_id == "deepseek":
        try:
            from agentforge.credential_store import CredentialStore
            if not CredentialStore().status().get("configured"):
                missing.append("DeepSeek credential (DPAPI or DEEPSEEK_API_KEY)")
        except Exception:
            return _finish(result, "BLOCKED", "provider_preflight", "deepseek_credential_unavailable")
    if missing:
        result["missing_configuration_names"] = missing
        return _finish(result, "BLOCKED", "configuration", "local_runtime_credential_missing")
    if provider_id == "deepseek":
        if not _confirm_deepseek_consent():
            return _finish(result, "BLOCKED", "cloud_consent", "synthetic_cloud_consent_not_confirmed")
        result["cloud_data_consent"] = {
            "confirmed": True, "scope": "synthetic_acceptance_data_only",
            "phrase": DEEPSEEK_CONSENT_PHRASE,
        }
    admin_password = getpass.getpass("PostgreSQL administrator password for disposable database (hidden): ")
    if not admin_password:
        return _finish(result, "BLOCKED", "database_provision", "admin_credential_missing")

    database = _database_name(run_id)
    temp_root = ROOT / "tests" / ".tmp" / f"af033-agent-host-{run_id}"
    engine = index_store = provider = None
    result["temporary_resources"].update({"database_name": database,
        "chroma_path_isolated": True, "synthetic_repository_created": False})
    result["cleanup"].update({"database": "pending", "chroma_directory": "pending",
                              "synthetic_repository": "pending"})
    status, failure_stage, failure_code = "FAIL", "host_setup", "runner_failed"
    failure_operation = "import_acceptance_dependencies"
    try:
        from agentforge.af012_connection import check_database_health
        from agentforge.af012_runtime import create_persistent_chroma
        from agentforge.conversation_migrations import upgrade
        from agentforge.conversation_store import ConversationStore
        from agentforge.indexer import index_repository
        from agentforge.memory_store import MemoryStore
        from agentforge.repository_identity import repository_identity
        from agentforge.session_service import SessionService
        from agentforge.store import IndexStore
        from agentforge.agent_execution import PostgresExecutionStateStore, PostgresToolResultStore
        from sqlalchemy import text

        failure_operation = "create_temporary_root_parent"
        (ROOT / "tests" / ".tmp").mkdir(parents=True, exist_ok=True)
        failure_stage = "isolation_preflight"
        failure_operation = "create_isolated_run_directory"
        if temp_root.exists() or temp_root.is_symlink():
            raise AcceptanceBlocked("isolation_preflight", "run_directory_already_exists")
        temp_root.mkdir()
        host_infra._validate_temp_scope(database, temp_root)
        failure_stage = "database_provision"
        failure_operation = "provision_disposable_database"
        _provision_database(result, run_id, admin_password)
        failure_stage = "database_connection"
        failure_operation = "connect_to_disposable_database"
        engine = host_infra._create_test_engine(database, os.environ[DB_KEY_ENV])
        failure_stage = "database_migration"
        failure_operation = "check_disposable_database_health"
        if not check_database_health(engine):
            raise AcceptanceBlocked("database_preflight", "disposable_database_health_failed")
        failure_operation = "verify_disposable_database_identity"
        host_infra._database_identity(engine, database)
        failure_operation = "migrate_disposable_database"
        upgrade(engine)
        failure_operation = "read_disposable_migration_revision"
        with engine.connect() as connection:
            revisions = connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars().all()
        result["database"] = {"migration_revisions": revisions,
                               "identity": "unique_disposable_database"}
        result["checks"]["isolated_database_and_migration"] = "PASS"

        failure_stage = "chroma_setup"
        failure_operation = "create_and_reopen_temporary_chroma"
        chroma_path = temp_root / "chroma"
        collection = "af042_unified_" + run_id[:20]
        chroma = host_infra._verify_temporary_chroma_reopen(
            create_persistent_chroma, chroma_path, collection)
        result["chroma"] = {"collection": collection, **chroma}
        result["temporary_resources"]["chroma_created"] = True
        result["checks"]["temporary_chroma_reopen"] = "PASS" if chroma.get("reopened") else "FAIL"

        failure_stage = "synthetic_repository_setup"
        failure_operation = "create_synthetic_repository"
        repository = temp_root / "repository"
        repository.mkdir()
        source = repository / "src" / "metrics.py"
        source.parent.mkdir(parents=True)
        source.write_text(
            "def compute_total(value, delta):\n"
            "    return value + delta\n\n"
            "def compute_offset(value, delta):\n"
            "    return value - delta\n", encoding="utf-8",
        )
        repository_id = repository_identity(repository)
        MemoryStore(engine).register_repository(repository_id, str(repository.resolve()),
                                                "AF042 synthetic acceptance repository")
        result["temporary_resources"]["synthetic_repository_created"] = True
        result["synthetic_repository"] = {"repository_id": repository_id,
                                           "source_file_count": 1,
                                           "source_digest": hashlib.sha256(source.read_bytes()).hexdigest()}
        conversation_store = ConversationStore(engine)
        session_service = SessionService()
        state_store = PostgresExecutionStateStore(engine)
        result_store = PostgresToolResultStore(engine)

        failure_stage = "provider_setup"
        failure_operation = f"create_real_{provider_id}_provider"
        provider_delegate = _create_real_provider(
            provider_id, model_id, temp_root=temp_root, result=result,
            stream=False)
        provider = CappedConversationProvider(provider_delegate, limit=result["request_limit"])
        result["checks"]["qwen_provider_constructed" if provider_id == "llama-server"
                         else "deepseek_provider_constructed"] = "PASS"
        if provider_id == "deepseek":
            result["checks"]["deepseek_model_discovery"] = result["model_discovery"]["status"]
        checkpoint_anchor = None
        # IndexStore deliberately confines on-disk indexes to the managed
        # repository index directory. This host runner needs only an isolated
        # index for its synthetic repository, so use real SQLite in-memory
        # storage instead of writing a forbidden ad-hoc file under tests/.tmp.
        failure_stage = "synthetic_index_setup"
        failure_operation = "open_isolated_sqlite_memory_index"
        with IndexStore(":memory:") as active_index:
            failure_stage = "synthetic_index_build"
            failure_operation = "index_synthetic_repository"
            index_repository(repository, active_index)
            result["checks"]["synthetic_sqlite_index"] = "PASS"
            result["synthetic_repository"]["index_storage"] = "isolated_sqlite_memory"
            failure_stage = "scenarios"
            first_scenario_failure = None
            for name in selected_scenarios:
                if name == "long_answer" and provider_id != "deepseek":
                    scenario = _scenario_shell(name)
                    scenario.update(status="NOT_APPLICABLE",
                        reason="Long-answer host acceptance targets DeepSeek; Qwen keeps its verified local cap.")
                    result["scenarios"][name] = scenario
                    continue
                if provider_id == "deepseek":
                    provider_delegate.stream = _deepseek_streaming_enabled_for_scenario(
                        provider_id, name, result["provider"].get("supports_streaming"))
                if name == "session_process_restore":
                    scenario = _scenario_shell(name)
                    if checkpoint_anchor is None:
                        scenario.update(status="FAIL", failure_stage=name,
                                        failure_code="no_completed_agent_checkpoint")
                    else:
                        probe = _run_restore_probe(database, checkpoint_anchor["conversation_id"],
                                                   repository_id, checkpoint_anchor["execution_id"])
                        scenario["checkpoint_restore_probe"] = probe
                        scenario["assertions"]["new_process_restored_history_and_checkpoint"] = (
                            probe.get("status") == "passed" and probe.get("message_count", 0) >= 2)
                        scenario["trace"] = {"session_id": checkpoint_anchor["conversation_id"],
                            "execution_id": checkpoint_anchor["execution_id"],
                            "session_restore_status": probe.get("status"),
                            "persisted_message_count": probe.get("message_count"),
                            "execution_state": probe.get("checkpoint_state")}
                        scenario["status"] = "PASS" if all(scenario["assertions"].values()) else "FAIL"
                else:
                    failure_stage = name
                    failure_operation = f"run_scenario:{name}"
                    cancel_event = threading.Event()
                    scenario = _run_scenario(name, provider=provider, engine=engine,
                        index_store=active_index, conversation_store=conversation_store,
                        session_service=session_service, repository=repository,
                        repository_id=repository_id, run_id=run_id,
                        cancel_event=cancel_event,
                        approve_synthetic_actions=args.approve_synthetic_actions)
                result["scenarios"][name] = scenario
                if scenario.get("status") == "FAIL" and first_scenario_failure is None:
                    first_scenario_failure = (
                        scenario.get("failure_stage") or name,
                        scenario.get("failure_code") or "scenario_assertion_failed",
                    )
                if name in AGENT_SCENARIOS:
                    checkpoint = _completed_agent_checkpoint(scenario)
                    if checkpoint is not None:
                        checkpoint_anchor = checkpoint
                stats = _counts(provider.request_stats())
                if _stop_after_scenario(
                        full_suite=selected_scenarios == _full_suite_scenarios(provider_id),
                        scenario_status=scenario.get("status", "FAIL"),
                        request_stats=stats):
                    failure_stage, failure_code = _scenario_stop_failure_code(scenario, stats)
                    break
                if (scenario.get("status") != "PASS"
                        and selected_scenarios != _full_suite_scenarios(provider_id)):
                    failure_stage = scenario.get("failure_stage") or name
                    failure_code = scenario.get("failure_code") or "scenario_assertion_failed"
                    break
            result["chat_requests"] = _counts(provider.request_stats())
            if first_scenario_failure is not None and failure_code == "runner_failed":
                failure_stage, failure_code = first_scenario_failure
            result["checks"]["request_budget_and_no_retry"] = (
                "PASS" if result["chat_requests"]["sent"] <= result["request_limit"]
                and result["chat_requests"]["retries"] == 0 else "FAIL")
            selected_status = _selected_scenario_status(selected_scenarios, result["scenarios"])
            result["checks"]["selected_scenarios"] = selected_status
            result["checks"]["all_scenarios"] = (
                selected_status if selected_scenarios == _full_suite_scenarios(provider_id) else "NOT_MEASURED")
            result["checks"]["conversation_entry_used"] = "PASS"
            result["checks"]["real_agent_engine_used"] = _agent_engine_status(result["scenarios"])
            if selected_scenarios == _full_suite_scenarios(provider_id) and selected_status == "PASS":
                status, failure_stage, failure_code = "PASS", None, None
            elif selected_scenarios == _full_suite_scenarios(provider_id) and selected_status == "PARTIAL":
                status, failure_stage, failure_code = "PARTIAL", None, "capability_not_fully_verified"
            elif selected_scenarios != _full_suite_scenarios(provider_id) and selected_status == "PASS":
                status, failure_stage, failure_code = "PARTIAL", None, "targeted_scenario_only"
            elif selected_scenarios != _full_suite_scenarios(provider_id) and selected_status == "PARTIAL":
                status, failure_stage, failure_code = "PARTIAL", None, "scenario_capability_not_fully_verified"
            else:
                status = "FAIL"
    except AcceptanceBlocked as exc:
        status, failure_stage, failure_code = "BLOCKED", exc.stage, exc.code
        if provider is not None:
            result["chat_requests"] = _counts(provider.request_stats())
    except Exception as exc:
        status, failure_stage, failure_code = "FAIL", failure_stage or "host_runner", type(exc).__name__
        result["failure_diagnostic"] = _safe_runner_error(
            exc, failure_operation or failure_stage,
            secrets=(admin_password, os.environ.get(DB_KEY_ENV),
                     os.environ.get(CHAT_KEY_ENV)))
        if failure_stage == "database_provision":
            provision = result.get("database_provision", {})
            if not provision.get("diagnostic"):
                provision["diagnostic"] = {
                    "operation": provision.get("operation"),
                    **_safe_database_error(exc, secrets=(admin_password, os.environ.get(DB_KEY_ENV))),
                }
        if provider is not None:
            result["chat_requests"] = _counts(provider.request_stats())
    finally:
        if index_store is not None:
            try:
                index_store.close()
            except Exception:
                pass
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        database_cleanup = _cleanup_database(result, database, admin_password)
        if database_cleanup == "failed" and status != "FAIL":
            status, failure_stage, failure_code = "FAIL", "cleanup_database", "database_cleanup_failed"
        if temp_root.exists():
            try:
                host_infra._validate_temp_scope(database, temp_root)
                if temp_root.is_symlink() or (callable(getattr(temp_root, "is_junction", None)) and temp_root.is_junction()):
                    raise ValueError
                shutil.rmtree(temp_root)
                result["cleanup"]["chroma_directory"] = "removed"
                result["cleanup"]["synthetic_repository"] = "removed"
            except Exception as exc:
                result["cleanup"]["chroma_directory"] = "failed"
                result["cleanup"]["synthetic_repository"] = "failed"
                result["cleanup"]["error_type"] = type(exc).__name__
                status, failure_stage, failure_code = "FAIL", "cleanup_temp_resources", type(exc).__name__
        admin_password = None
    return _finish(result, status, failure_stage, failure_code)


if __name__ == "__main__":
    raise SystemExit(_main())
