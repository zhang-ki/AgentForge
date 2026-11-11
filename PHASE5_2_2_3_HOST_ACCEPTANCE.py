from __future__ import annotations

"""Single-run Phase 5.2.2.3 host acceptance.

The real repository is scanned/retrieved locally only. DeepSeek turns use a
uniquely identified synthetic repository and synthetic session history.
All persistent Chroma writes are isolated by the synthetic repository ID and
are removed before the final data-integrity comparison.
"""

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import traceback
import uuid
from dataclasses import replace
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import patch

from phase5_1_6_r2_acceptance import (
    apply_launcher_embedding_defaults,
    canonical_sha256,
    persistent_collection_snapshot,
)


ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".agentforge" / "af012-venv" / "Scripts" / "python.exe"
RUN_ID = uuid.uuid4().hex
SHORT_ID = RUN_ID[:12]
RAW_RESULT = ROOT / f"PHASE5_2_2_3_HOST_RUN_{RUN_ID}.json"
RAW_TRACE = ROOT / f"PHASE5_2_2_3_HOST_TRACE_{RUN_ID}.jsonl"
SYNTHETIC_PARENT = ROOT / ".agentforge" / "data" / f"phase5_2_2_3_host_{RUN_ID}"
SYNTHETIC_ROOT = SYNTHETIC_PARENT / "repository"
OWNER_MARKER = SYNTHETIC_PARENT / ".agentforge-acceptance-owner.json"
MAX_DEEPSEEK_CHAT_REQUESTS = 27
C2_CHAT_REQUEST_CAP = 26
DEFAULT_SYNTHETIC_SCENARIOS = ("A", "B", "C1", "C2")
SYNTHETIC_CLOUD_CONSENT = "SEND SYNTHETIC TESTS"
CAPABILITY_EVIDENCE_PATTERN = re.compile(
    r"^PHASE5_2_2_3_R4_2_1_CAPABILITY_PROBE_EVIDENCE_\d{8}_\d{2}\.json$"
)
CURRENT_CAPABILITY_PROBE_EVIDENCE_SHA256 = (
    "8ffbc9e69b1f57faf5b6fad1068ad5f27f6ecfde31cbda98518cd79ccb166634"
)


def _canonical_sha256(value) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_capability_probe_evidence(path, *, root=ROOT, now=None,
                                    expected_file_sha256=None):
    """Validate the single, synthetic runtime probe without changing model metadata."""
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError("Capability probe evidence path is required.")
    candidate = Path(path)
    if candidate.is_symlink() or not CAPABILITY_EVIDENCE_PATTERN.fullmatch(candidate.name):
        raise ValueError("Capability probe evidence path is not an accepted R4.2.1 artifact.")
    root_path = Path(root).resolve(strict=True)
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root_path)
    except ValueError:
        raise ValueError("Capability probe evidence must be inside the AgentForge project.") from None
    if not resolved.is_file() or resolved.stat().st_size > 16_384:
        raise ValueError("Capability probe evidence is not a bounded regular file.")
    raw = resolved.read_bytes()
    file_sha256 = hashlib.sha256(raw).hexdigest()
    if (expected_file_sha256 is not None
            and file_sha256 != expected_file_sha256.casefold()):
        raise ValueError("Capability probe evidence file hash does not match the verified probe.")
    try:
        evidence = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Capability probe evidence is not valid UTF-8 JSON.") from None
    if not isinstance(evidence, dict):
        raise ValueError("Capability probe evidence must be a JSON object.")
    expected = {
        "schema_version": 1,
        "status": "VERIFIED",
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "method": "DeepSeekClient.chat normalized response",
        "request_count": 1,
        "automatic_retries": 0,
        "http_status": 200,
        "finish_reason": "tool_calls",
        "tool_call_count": 1,
        "tool_name": "agentforge_capability_probe",
        "arguments": {"status": "ok"},
        "tool_executed": False,
        "source": "single explicit synthetic capability probe",
    }
    for key, value in expected.items():
        if type(evidence.get(key)) is not type(value) or evidence.get(key) != value:
            raise ValueError(f"Capability probe evidence failed validation: {key}.")
    if evidence.get("arguments_sha256") != _canonical_sha256(expected["arguments"]):
        raise ValueError("Capability probe evidence argument hash is invalid.")
    request_id = evidence.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise ValueError("Capability probe evidence request identity is invalid.")
    for key in ("evidence_sha256",):
        digest = evidence.get(key)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"Capability probe evidence {key} is invalid.")
    try:
        verified_at = datetime.fromisoformat(evidence["verified_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        raise ValueError("Capability probe evidence timestamp is invalid.") from None
    if verified_at.tzinfo is None:
        raise ValueError("Capability probe evidence timestamp must include a timezone.")
    current = now or datetime.now(timezone.utc)
    age = current.astimezone(timezone.utc) - verified_at.astimezone(timezone.utc)
    if age.total_seconds() < -300 or age.total_seconds() > 86_400:
        raise ValueError("Capability probe evidence is outside its 24-hour validity window.")
    return {
        "status": "PASS",
        "provider": "deepseek",
        "model_id": "deepseek-flash",
        "verification": "runtime_tool_call_probe",
        "metadata_supports_tool_calling": None,
        "finish_reason": "tool_calls",
        "tool_call_count": 1,
        "tool_executed": False,
        "request_count": 1,
        "automatic_retries": 0,
        "verified_at": verified_at.astimezone(timezone.utc).isoformat(),
        "request_id": request_id,
        "evidence_sha256": evidence["evidence_sha256"],
        "evidence_file_sha256": file_sha256,
        "evidence_file": str(resolved),
    }


def _tool_capability_preflight(descriptor, evidence_path=None, *, root=ROOT, now=None,
                               expected_file_sha256=None):
    """Use official capability metadata, or one validated probe if metadata is unknown."""
    if (getattr(descriptor, "provider_id", None) != "deepseek"
            or getattr(descriptor, "model_id", None) != "deepseek-flash"):
        raise ValueError("Tool capability evidence is pinned to deepseek-flash.")
    capability = getattr(descriptor, "supports_tool_calling", None)
    if capability is False:
        raise ValueError("Official model metadata explicitly reports tool calling unsupported.")
    if evidence_path is not None:
        evidence = _load_capability_probe_evidence(
            evidence_path, root=root, now=now,
            expected_file_sha256=expected_file_sha256,
        )
        evidence["metadata_supports_tool_calling"] = capability
        if capability is True:
            evidence["verification"] = "official_metadata_and_runtime_tool_call_probe"
        return evidence
    if capability is True:
        return {
            "status": "PASS", "provider": "deepseek", "model_id": "deepseek-flash",
            "verification": "official_metadata", "metadata_supports_tool_calling": True,
            "tool_executed": False,
        }
    raise ValueError("Tool calling metadata is unknown and no valid capability probe evidence was provided.")


def _confirm_synthetic_cloud_requests(input_fn=input, output_fn=print,
                                      max_chat_requests=MAX_DEEPSEEK_CHAT_REQUESTS):
    """Require a fresh, exact user confirmation before any cloud request."""
    output_fn(
        "Only synthetic AgentForge acceptance data will be sent to DeepSeek. "
        f"Up to {max_chat_requests} DeepSeek chat requests may be sent; "
        "this runner does not refresh model discovery or retry requests. "
        f"Type {SYNTHETIC_CLOUD_CONSENT} to continue:",
        flush=True,
    )
    try:
        response = input_fn()
    except (EOFError, OSError):
        return False
    return isinstance(response, str) and response.strip() == SYNTHETIC_CLOUD_CONSENT


def _ensure_database_password(*, environ=None, is_tty=None, getpass_fn=None):
    """Use an existing process-local credential or request one without echoing it."""
    env = os.environ if environ is None else environ
    if env.get("AGENTFORGE_DB_PASSWORD"):
        return True
    interactive = sys.stdin.isatty() if is_tty is None else bool(is_tty)
    if not interactive:
        return False
    if getpass_fn is None:
        from getpass import getpass
        getpass_fn = getpass
    try:
        password = getpass_fn("PostgreSQL application password (hidden): ")
    except (EOFError, OSError):
        return False
    if not isinstance(password, str) or not password:
        return False
    env["AGENTFORGE_DB_PASSWORD"] = password
    return True


def _chat_request_cap(*, capability_probe_used, prior_c2_chat_requests=0):
    """Bound a C2 attempt by the phase-wide request budget, including earlier attempts."""
    if type(prior_c2_chat_requests) is not int or prior_c2_chat_requests < 0:
        raise ValueError("Prior C2 request count must be a non-negative integer.")
    prior_probe_requests = 1 if capability_probe_used else 0
    remaining = MAX_DEEPSEEK_CHAT_REQUESTS - prior_probe_requests - prior_c2_chat_requests
    if remaining < 1:
        raise ValueError("No DeepSeek chat request budget remains for this phase.")
    invocation_cap = C2_CHAT_REQUEST_CAP if capability_probe_used else MAX_DEEPSEEK_CHAT_REQUESTS
    return min(invocation_cap, remaining)


def _c_scenario_status(c1, c2):
    statuses = (c1.get("status"), c2.get("status"))
    if "FAIL" in statuses:
        return "FAIL"
    if "PARTIAL" in statuses:
        return "PARTIAL"
    if all(status == "PASS" for status in statuses):
        return "PASS"
    if all(status in {"NOT_MEASURED", "BLOCKED"} for status in statuses):
        return "NOT_MEASURED"
    return "PARTIAL"


def _simple_timed_factory(metrics, target, shape=None):
    """Build a method wrapper factory for request-local host instrumentation."""
    def wrapper_factory(original):
        def wrapped(self, *args, **kwargs):
            started = time.perf_counter()
            error = None
            value = None
            try:
                value = original(self, *args, **kwargs)
                return value
            except Exception as exc:
                error = type(exc).__name__
                raise
            finally:
                event = {"phase": metrics["phase"],
                         "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
                if error:
                    event["error_type"] = error
                if shape and error is None:
                    try:
                        event.update(shape(value))
                    except Exception:
                        pass
                metrics[target].append(event)
        return wrapped
    return wrapper_factory


def _digest(value) -> str:
    return canonical_sha256(value)


def _only_counts(rows) -> dict:
    safe = [{key: row.get(key) for key in
             ("relative_path", "content_hash", "size_bytes", "mtime_ns", "chunk_signature")}
            for row in rows]
    safe.sort(key=lambda row: str(row.get("relative_path", "")))
    return {"count": len(safe), "sha256": _digest(safe)}


def _repository_registry_snapshot_value(active_repository_id, rows):
    """Hash registry identity/metadata without copying user repository paths to output."""
    safe_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Repository registry snapshot contains an invalid row.")
        safe_rows.append({key: row.get(key) for key in (
            "repository_id", "canonical_path", "display_name", "index_path",
            "indexed_at", "registered_at", "active",
        )})
    safe_rows.sort(key=lambda item: str(item.get("repository_id", "")))
    return {
        "repository_count": len(safe_rows),
        "active_repository_id_sha256": _digest(active_repository_id),
        "registry_sha256": _digest(safe_rows),
    }


def _repository_registry_snapshot():
    from agentforge.repositories import RepositoryRegistry

    with RepositoryRegistry(readonly=True) as registry:
        return _repository_registry_snapshot_value(registry.active_id(), registry.list())


def _postgres_primary_key_snapshot(engine, tables=None):
    """Read-only, content-free identity snapshot of AgentForge PostgreSQL rows."""
    from sqlalchemy import select, text
    from agentforge.af012_schema import Base

    tables = Base.metadata.tables if tables is None else tables
    snapshot = {}
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            for table_name, table in sorted(tables.items()):
                primary_key = tuple(table.primary_key.columns)
                if not primary_key:
                    raise ValueError("PostgreSQL protection snapshot requires primary keys.")
                rows = connection.execute(
                    select(*primary_key).order_by(*primary_key),
                ).all()
                identity_rows = [tuple(str(value) for value in row) for row in rows]
                snapshot[table_name] = {
                    "row_count": len(identity_rows),
                    "primary_key_sha256": _digest(identity_rows),
                }
    return {
        "table_count": len(snapshot),
        "rows": snapshot,
        "snapshot_sha256": _digest(snapshot),
    }


def _jsonable(value):
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _context_evidence_summary(context):
    values = []
    for item in getattr(context, "evidence", ()) or ():
        source = getattr(item, "result", None)
        content_hash = getattr(source, "content_hash", None)
        text = getattr(source, "text", None)
        values.append({
            "source_id": getattr(item, "source_id", None),
            "citation": getattr(item, "citation", None),
            "relative_path": getattr(source, "relative_path", None),
            "start_line": getattr(source, "start_line", None),
            "end_line": getattr(source, "end_line", None),
            "content_hash": content_hash if isinstance(content_hash, str) else None,
            "content_hash_valid": isinstance(content_hash, str) and bool(
                re.fullmatch(r"[0-9a-f]{64}", content_hash)
            ),
            "excerpt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()
            if isinstance(text, str) else None,
        })
    return values


def _evidence_identity_sha256(evidence):
    """Hash source identity and integrity fields without retaining source text."""
    identity = []
    for item in evidence if isinstance(evidence, (tuple, list)) else ():
        if not isinstance(item, dict):
            continue
        identity.append({key: item.get(key) for key in (
            "source_id", "citation", "relative_path", "start_line", "end_line",
            "content_hash", "excerpt_sha256",
        )})
    identity.sort(key=lambda item: (str(item.get("source_id")), str(item.get("citation"))))
    return _digest(identity)


def _contract_evidence_summary(contract, context_evidence=()):
    """Extract only fields the contract actually exposes; never invent provenance."""
    context_by_identity = {
        (item.get("source_id"), item.get("citation")): item
        for item in context_evidence if isinstance(item, dict)
    }
    values = []
    missing_hashes = 0
    for item in getattr(contract, "evidence", ()) or ():
        source_id = getattr(item, "source_id", None)
        citation = getattr(item, "citation", None)
        direct_hash = getattr(item, "content_hash", None)
        direct_hash_valid = isinstance(direct_hash, str) and bool(
            re.fullmatch(r"[0-9a-f]{64}", direct_hash)
        )
        linked = context_by_identity.get((source_id, citation))
        linked_hash = linked.get("content_hash") if linked else None
        linked_hash_valid = bool(linked and linked.get("content_hash_valid")
                                 and isinstance(linked_hash, str))
        hash_valid = direct_hash_valid or linked_hash_valid
        missing_hashes += not hash_valid
        values.append({
            "source_id": source_id if isinstance(source_id, str) else None,
            "citation": citation if isinstance(citation, str) else None,
            "content_hash": direct_hash if direct_hash_valid else linked_hash if linked_hash_valid else None,
            "content_hash_source": "contract" if direct_hash_valid else (
                "built_context" if linked_hash_valid else None
            ),
            "content_hash_available_on_contract": direct_hash_valid,
            "content_hash_linked_to_built_context": linked_hash_valid,
            "provenance": _jsonable(getattr(item, "provenance", None)),
        })
    return {
        "items": values,
        "content_hash_status": (
            "NOT_APPLICABLE" if not values else
            "PARTIAL" if missing_hashes else "PASS"
        ),
        "missing_content_hash_count": missing_hashes,
    }


def _deepseek_chat_wrapper(original, metrics):
    def wrapped(self, messages, *args, **kwargs):
        phase = metrics.get("phase", "unknown")
        in_synthetic_scenario = phase.startswith("scenario_")
        total_attempts = int(metrics.setdefault("deepseek_chat_attempts", 0))
        total_maximum = int(metrics.get("max_deepseek_chat_requests", MAX_DEEPSEEK_CHAT_REQUESTS))
        if total_attempts >= total_maximum:
            raise RuntimeError("DeepSeek chat request budget exceeded; request not sent.")
        metrics["deepseek_chat_attempts"] = total_attempts + 1
        scenario_attempts = metrics.setdefault("scenario_chat_attempts", 0)
        maximum = int(metrics.get("max_scenario_chat_requests", 9))
        if in_synthetic_scenario and scenario_attempts >= maximum:
            raise RuntimeError("Synthetic DeepSeek chat request budget exceeded; request not sent.")
        if in_synthetic_scenario:
            metrics["scenario_chat_attempts"] = scenario_attempts + 1

        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        combined = "\n".join(
            item.get("content", "") for item in messages
            if isinstance(item, dict) and isinstance(item.get("content"), str)
        )
        expectation = metrics.get("scenario_expectations", {}).get(phase, {})
        scenario_key = phase.removeprefix("scenario_").removesuffix("_execution")
        context_batches = metrics.get("scenario_built_context_evidence", {}).get(scenario_key, ())
        context_evidence = context_batches[-1] if context_batches else []
        active_context = metrics.get("active_answer_context")
        context_source_ids = [item.get("source_id") for item in context_evidence
                              if isinstance(item.get("source_id"), str)]
        context_citations = [item.get("citation") for item in context_evidence
                             if isinstance(item.get("citation"), str)]
        event = {
            "phase": phase,
            "request_messages_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "message_count": len(messages),
            "roles": [item.get("role") for item in messages if isinstance(item, dict)],
            "contains_current_question": bool(expectation.get("question")
                                               and expectation["question"] in combined),
            "contains_synthetic_source_path": bool(expectation.get("source_path")
                                                    and expectation["source_path"] in combined),
            "contains_synthetic_source_formula": bool(expectation.get("source_formula")
                                                       and expectation["source_formula"] in combined),
            "contains_synthetic_function_name": "calculate_weighted_total" in combined,
            "built_context_evidence_count": len(context_evidence),
            "contains_all_built_context_source_ids": bool(context_source_ids)
            and all(source_id in combined for source_id in context_source_ids),
            "contains_all_built_context_citations": bool(context_citations)
            and all(citation in combined for citation in context_citations),
            "contains_no_built_context_evidence": not context_evidence,
            "answer_context_evidence_count": (
                active_context.get("evidence_count") if isinstance(active_context, dict) else None
            ),
            "answer_context_evidence_sha256": (
                active_context.get("evidence_identity_sha256")
                if isinstance(active_context, dict) else None
            ),
            "request_content_recorded": False,
        }
        events = metrics.setdefault("deepseek_chat_events", [])
        events.append(event)
        try:
            completion = original(self, messages, *args, **kwargs)
        except Exception as exc:
            event.update({"call_status": "FAILED", "error_type": type(exc).__name__})
            raise
        event.update({
            "call_status": "RETURNED",
            "finish_reason": getattr(completion, "finish_reason", None),
            "usage": _jsonable(getattr(completion, "usage", {})),
        })
        return completion
    return wrapped


def _deepseek_network_request_wrapper(original, metrics):
    """Aggregate request outcomes at DeepSeekClient's HTTP request boundary."""
    def wrapped(self, method, path, *args, **kwargs):
        before = self.request_stats()
        discovery = method == "GET"
        event = {"kind": "model_discovery" if discovery else "chat",
                 "method": method, "phase": metrics.get("phase", "unknown"),
                 "attempted": 1, "sent": 0, "succeeded": 0, "failed": 0,
                 "retries": 0}
        error_type = None
        metrics["active_network_request_event"] = event
        try:
            value = original(self, method, path, *args, **kwargs)
            event["succeeded"] = 1
            return value
        except Exception as exc:
            error_type = type(exc).__name__
            after = self.request_stats()
            event["sent"] = max(event["sent"],
                                after.get("sent", 0) - before.get("sent", 0))
            event["failed"] = 1
            raise
        finally:
            metrics.pop("active_network_request_event", None)
            after = self.request_stats()
            if not discovery:
                event["sent"] = max(event["sent"],
                                    after.get("sent", 0) - before.get("sent", 0))
                event["succeeded"] = max(0, after.get("succeeded", 0) - before.get("succeeded", 0))
                event["failed"] = max(event["failed"],
                                      after.get("failed", 0) - before.get("failed", 0))
                event["retries"] = max(0, after.get("retries", 0) - before.get("retries", 0))
            event["error_type"] = error_type
            counters = metrics.setdefault("network_request_counts", {
                "chat": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0},
                "model_discovery": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0},
            })
            bucket = counters["model_discovery" if discovery else "chat"]
            for key in bucket:
                bucket[key] += event.get(key, 0)
            metrics.setdefault("network_request_events", []).append(event)
    return wrapped


def _deepseek_connect_wrapper(original, metrics):
    """Mark the actual HTTP send invocation for chat and discovery requests."""
    def wrapped(self, *args, **kwargs):
        connection = original(self, *args, **kwargs)
        request = connection.request

        def tracked_request(method, path, *request_args, **request_kwargs):
            event = metrics.get("active_network_request_event")
            if isinstance(event, dict):
                event["sent"] += 1
            return request(method, path, *request_args, **request_kwargs)

        connection.request = tracked_request
        return connection
    return wrapped


def _tool_discovery_wrapper(original, metrics):
    """Record only the authorized catalog identities, never query text or schemas."""
    def wrapped(self, objective, *args, **kwargs):
        catalog = original(self, objective, *args, **kwargs)
        phase = metrics.get("phase", "unknown")
        if phase.startswith("scenario_C"):
            scopes = kwargs.get("permission_scopes")
            metrics.setdefault("tool_discovery", []).append({
                "phase": phase,
                "permission_scopes": sorted(scopes) if isinstance(scopes, frozenset) else None,
                "tools": [{
                    "tool_id": item.tool_id,
                    "permission_scope": item.permission_scope,
                    "side_effect": item.side_effect,
                    "requires_approval": item.requires_approval,
                } for item in catalog],
            })
        return catalog
    return wrapped


def _tool_schema_wrapper(original, metrics):
    """Prove registered schemas were loaded without logging parameter values."""
    def wrapped(self, tool_ids, *args, **kwargs):
        ids = tuple(tool_ids)
        try:
            schemas = original(self, ids, *args, **kwargs)
        except Exception as exc:
            if metrics.get("phase", "").startswith("scenario_C"):
                metrics.setdefault("tool_schema_loads", []).append({
                    "phase": metrics["phase"], "tool_ids": list(ids),
                    "status": "FAILED", "error_type": type(exc).__name__,
                })
            raise
        if metrics.get("phase", "").startswith("scenario_C"):
            metrics.setdefault("tool_schema_loads", []).append({
                "phase": metrics["phase"], "tool_ids": list(ids),
                "status": "PASS",
                "input_output_schema_loaded": all(
                    isinstance(value, dict) and set(value) == {"input", "output"}
                    for value in schemas.values()
                ),
            })
        return schemas
    return wrapped


def _plan_validation_wrapper(original, metrics):
    """Record validator execution, permission checks, and dependency counts safely."""
    def wrapped(self, plan, *args, **kwargs):
        calls = tuple(getattr(plan, "calls", ()) or ())
        call_summaries = []
        for call in calls:
            definition = self.registry.get(getattr(call, "tool_id", ""))
            scope = getattr(definition, "permission_scope", None)
            arguments = getattr(call, "arguments", None)
            safe_fields = sorted(key for key in arguments
                                 if isinstance(key, str)
                                 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\[\]-]{0,127}", key))[:32] \
                if isinstance(arguments, dict) else []
            type_name = lambda item: (
                "null" if item is None else "boolean" if isinstance(item, bool)
                else "integer" if isinstance(item, int) else "number" if isinstance(item, float)
                else "string" if isinstance(item, str) else "array" if isinstance(item, list)
                else "object" if isinstance(item, dict) else "unknown"
            )
            safe_types = {key: type_name(arguments[key]) for key in safe_fields} \
                if isinstance(arguments, dict) else {}
            raw_call_id = getattr(call, "call_id", None)
            call_summaries.append({
                "tool_id": getattr(call, "tool_id", None),
                "task_id": getattr(call, "task_id", None),
                "call_id_hash": (hashlib.sha256(raw_call_id.encode("utf-8")).hexdigest()[:16]
                                 if isinstance(raw_call_id, str) else None),
                "argument_fields": safe_fields,
                "argument_types": safe_types,
                "dependency_count": len(getattr(call, "dependencies", ()) or ()),
                "permission_scope": scope,
                "permission_granted": bool(
                    definition is not None and scope in self.authorization.permission_scopes
                ),
                "side_effect": getattr(definition, "side_effect", None),
                "requires_approval": getattr(definition, "requires_approval", None),
            })
        try:
            value = original(self, plan, *args, **kwargs)
        except Exception as exc:
            if metrics.get("phase", "").startswith("scenario_C"):
                from agentforge.agent_execution import _safe_argument_diagnostic_details
                diagnostic_details = getattr(exc, "diagnostic_details", None)
                metrics.setdefault("plan_validations", []).append({
                    "phase": metrics["phase"], "status": "FAILED",
                    "error_type": type(exc).__name__,
                    "diagnostic_code": getattr(exc, "diagnostic_code", None),
                    "diagnostic_details": (_safe_argument_diagnostic_details(diagnostic_details)
                                           if isinstance(diagnostic_details, dict) else None),
                    "calls": call_summaries,
                })
            raise
        if metrics.get("phase", "").startswith("scenario_C"):
            metrics.setdefault("plan_validations", []).append({
                "phase": metrics["phase"], "status": "PASS",
                "calls": call_summaries,
            })
        return value
    return wrapped


def _provider_answer_wrapper(original, metrics):
    def wrapped(self, context, *args, **kwargs):
        phase = metrics.get("phase", "unknown")
        if phase.startswith("scenario_") and phase.endswith("_execution"):
            key = phase.removeprefix("scenario_").removesuffix("_execution")
            context_summary = _context_evidence_summary(context)
            metrics.setdefault("scenario_built_context_evidence", {}).setdefault(key, []).append(context_summary)
            observations = tuple(getattr(context, "tool_observations", ()) or ())
            expectation = metrics.get("scenario_expectations", {}).get(phase, {})
            serialized = json.dumps(_jsonable(observations), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":"))
            markers = tuple(expectation.get("tool_observation_markers", ()))
            metrics.setdefault("scenario_tool_observations", {}).setdefault(key, []).append({
                "count": len(observations),
                "tool_ids": sorted({item.get("tool_id") for item in observations
                                    if isinstance(item, dict)
                                    and isinstance(item.get("tool_id"), str)}),
                "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "contains_expected_marker": bool(markers) and all(
                    marker.casefold() in serialized.casefold() for marker in markers
                ),
                "call_ids": [item.get("call_id") for item in observations
                             if isinstance(item, dict) and isinstance(item.get("call_id"), str)],
                "observation_ids": [item.get("observation_id") for item in observations
                                    if isinstance(item, dict)
                                    and isinstance(item.get("observation_id"), str)],
                "verified_evidence_source_ids": [source_id for item in observations
                    if isinstance(item, dict)
                    for source_id in item.get("verified_evidence_source_ids", ())
                    if isinstance(source_id, str)],
            })
            previous = metrics.get("active_answer_context")
            metrics["active_answer_context"] = {
                "evidence_count": len(context_summary),
                "evidence_identity_sha256": _evidence_identity_sha256(context_summary),
            }
            try:
                return original(self, context, *args, **kwargs)
            finally:
                if previous is None:
                    metrics.pop("active_answer_context", None)
                else:
                    metrics["active_answer_context"] = previous
        return original(self, context, *args, **kwargs)
    return wrapped


def _safe_failure_diagnostics(exc, stage):
    """Keep useful failure location without recording secrets or request bodies."""
    message = str(exc)
    message = re.sub(
        r"(?i)(password|api[_-]?key|secret|access[_-]?token)(\s*[=:]\s*)[^\s,;]+",
        r"\1\2[redacted]", message,
    )
    message = re.sub(
        r"(?i)(postgres(?:ql)?(?:\+\w+)?://)[^/@\s]+:[^/@\s]+@",
        r"\1[redacted]@", message,
    )
    frames = [
        {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
        for frame in traceback.extract_tb(exc.__traceback__)
    ]
    return {
        "stage": stage,
        "error_type": type(exc).__name__,
        "error_code": getattr(exc, "diagnostic_code", None) or type(exc).__name__,
        "safe_message": message[:600],
        "traceback_frames": frames[-12:],
        "request_or_source_content_recorded": False,
    }


def _public_metrics(metrics):
    """Remove ephemeral query/source markers before machine-readable persistence."""
    return {key: value for key, value in metrics.items()
            if key not in {"scenario_expectations"}}


def _scenario_status(checks, contract_status):
    if not checks or not all(value is True for value in checks.values()):
        return "FAIL"
    if contract_status == "limited":
        return "PARTIAL"
    if contract_status == "answered":
        return "PASS"
    return "FAIL"


def _effective_evidence_count(retrieval, built_context_evidence):
    """Prefer the actual final context collection; tolerate older payloads safely."""
    if isinstance(built_context_evidence, (tuple, list)):
        return len(built_context_evidence)
    if isinstance(retrieval, dict):
        if retrieval.get("retrieval_status") in {"no_evidence", "unavailable", "not_required"}:
            return 0
        value = retrieval.get("evidence_count")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _safe_retrieval_summary(retrieval):
    """Retain bounded retrieval decisions and stable evidence rejection codes."""
    if not isinstance(retrieval, dict):
        return {}
    fields = (
        "retrieval_status", "retrieval_failure_code", "candidate_count", "evidence_count",
        "retrieval_query_count", "unavailable_channels", "index_lifecycle",
    )
    result = {name: retrieval[name] for name in fields if name in retrieval}

    assessment = retrieval.get("evidence_assessment")
    if isinstance(assessment, dict):
        safe_assessment = {
            key: assessment[key] for key in (
                "candidate_count", "relevant_candidate_count", "relevance", "sufficiency",
                "required_roles", "available_roles", "missing_roles",
                "semantic_entailment_verified",
            ) if key in assessment
        }
        decisions = assessment.get("candidate_decisions")
        if isinstance(decisions, list):
            safe_assessment["candidate_decisions"] = [
                {key: item[key] for key in ("status", "reason_code")
                 if isinstance(item.get(key), str) and len(item[key]) <= 64}
                for item in decisions[:64] if isinstance(item, dict)
            ]
        result["evidence_assessment"] = safe_assessment

    validation = retrieval.get("agent_read_validation")
    if isinstance(validation, dict):
        safe_validation = {
            key: value for key, value in validation.items()
            if key in {"verified_read_count", "accepted_read_count"}
            and type(value) is int and value >= 0
        }
        route_confidence = validation.get("user_query_route_confidence")
        if isinstance(route_confidence, str) and route_confidence in {"low", "medium", "high"}:
            safe_validation["user_query_route_confidence"] = route_confidence
        for key in ("source_rejection_reason_codes", "relevance_rejection_reason_codes"):
            value = validation.get(key)
            if isinstance(value, list):
                safe_validation[key] = [
                    item for item in value[:64]
                    if isinstance(item, str) and 0 < len(item) <= 64
                ]
        confidences = validation.get("search_route_confidences")
        if isinstance(confidences, list):
            safe_validation["search_route_confidences"] = [
                item for item in confidences[:64]
                if isinstance(item, str) and item in {"low", "medium", "high"}
            ]
        result["agent_read_validation"] = safe_validation
    return result


def _c1_structured_context_check(scenario):
    """Verify provider context identity without requiring path/citation prompt strings."""
    built = scenario.get("built_context_evidence")
    markers = scenario.get("final_answer_request_evidence_markers")
    if not isinstance(built, list) or not built or not isinstance(markers, dict):
        return False
    return (
        markers.get("answer_context_evidence_count") == len(built)
        and markers.get("answer_context_evidence_sha256") == _evidence_identity_sha256(built)
        and all(isinstance(item, dict) for item in built)
        and all(isinstance(item.get("relative_path"), str)
                and item.get("citation") == (
                    f"{item.get('relative_path')}:{item.get('start_line')}-{item.get('end_line')}"
                )
                and isinstance(item.get("source_id"), str)
                and re.fullmatch(r"E[1-9]\d*", item["source_id"]) is not None
                and isinstance(item.get("start_line"), int)
                and isinstance(item.get("end_line"), int)
                and item["start_line"] >= 1 and item["end_line"] >= item["start_line"]
                and item.get("content_hash_valid") is True
                for item in built if isinstance(item, dict))
    )


def _run_synthetic_answer_scenario(
    key, query, *, repository_id, repository_root, index_store, vector_retriever,
    conversation_store, conversation_ids, orchestrator, provider, metrics, run_id,
    tool_observation_markers=(),
):
    """Run one isolated synthetic session through the product conversation entry."""
    from datetime import datetime, timezone
    from agentforge.conversation_orchestrator import ConversationRequest, ConversationSession
    from agentforge.observability import RequestLifecycleTracker, bind_lifecycle
    from agentforge.session_service import SessionService

    conversation = conversation_store.create_conversation(
        repository_id, f"Synthetic Phase 5.2.2.3 {key} {run_id[:8]}",
    )
    conversation_id = conversation["id"]
    conversation_ids.append(conversation_id)
    conversation_store.update_model_binding(
        conversation_id, repository_id, provider_id="deepseek", model_id="deepseek-flash",
        cloud_consent_at=datetime.now(timezone.utc),
    )
    rows_before = conversation_store.get_messages(conversation_id, repository_id)
    conversation_value = conversation_store.get_conversation(conversation_id, repository_id)
    conversation_value.update({"conversation_id": conversation_id, "repository_id": repository_id})
    session = ConversationSession(
        provider=provider, store=conversation_store, repository_id=repository_id,
        rows=tuple(rows_before), conversation=conversation_value,
    )
    request = ConversationRequest(
        query=query, repository=str(repository_root), provider_id="deepseek",
        model_id="deepseek-flash", conversation_id=conversation_id,
        use_chroma=True, mode="auto", cloud_data_authorized=True,
    )
    planning_phase = f"scenario_{key}_planning"
    execution_phase = f"scenario_{key}_execution"
    for phase in (planning_phase, execution_phase):
        metrics["scenario_expectations"][phase] = {
            "question": query,
            "source_path": "synthetic_calculation.py",
            "source_formula": "return round(amount * (1 + fee_rate), 2)",
            "tool_observation_markers": tuple(tool_observation_markers),
        }
    tracker = RequestLifecycleTracker(
        request_id=f"{run_id[:8]}{key}", log_path=ROOT / f"PHASE5_2_2_3_HOST_TRACE_{run_id}.jsonl",
    )
    tracker.start_request(provider="deepseek", model="deepseek-flash",
                          session_id=conversation_id, mode="auto", streaming_enabled=False)
    provider_before = provider.request_stats()
    with bind_lifecycle(tracker):
        metrics["phase"] = planning_phase
        plan_started = time.perf_counter()
        plan = orchestrator.plan(request, session=session, index_store=index_store)
        plan_elapsed = time.perf_counter() - plan_started
        metrics["phase"] = execution_phase
        execution_started = time.perf_counter()
        try:
            value = orchestrator.run(
                request, index_store=index_store, vector_retriever=vector_retriever,
                session=session, planned_plan=plan,
            )
            tracker.finish("COMPLETED")
        except Exception as exc:
            tracker.finish("FAILED", error_code=type(exc).__name__)
            raise
        execution_elapsed = time.perf_counter() - execution_started

    metrics["phase"] = f"scenario_{key}_result_extraction"
    persisted_rows = conversation_store.get_messages(conversation_id, repository_id)
    restored = SessionService().load_context(
        conversation_store, conversation_id, repository_id,
    )
    saved_turn = bool(
        len(persisted_rows) == len(rows_before) + 2
        and restored.rows
        and restored.rows[-2].get("content") == query
        and restored.rows[-1].get("content") == value.answer.text
    )
    context_batches = metrics["scenario_built_context_evidence"].get(key, [])
    context_evidence = context_batches[-1] if context_batches else []
    contract_evidence = _contract_evidence_summary(value.contract, context_evidence)
    searches = [item for item in metrics["product_retriever_calls"]
                if item.get("phase") == execution_phase]
    candidates = [candidate for item in searches
                  for candidate in item.get("candidate_summaries", ())]
    context_matches_candidates = bool(context_evidence) and all(any(
        candidate.get("citation") == evidence.get("citation")
        and candidate.get("relative_path") == evidence.get("relative_path")
        and candidate.get("start_line") == evidence.get("start_line")
        and candidate.get("end_line") == evidence.get("end_line")
        and candidate.get("content_hash") == evidence.get("content_hash")
        and candidate.get("excerpt_sha256") == evidence.get("excerpt_sha256")
        for candidate in candidates
    ) for evidence in context_evidence)
    chat_events = [item for item in metrics["deepseek_chat_events"]
                   if item.get("phase") == execution_phase]
    final_request = chat_events[-1] if chat_events else {}
    retrieval = value.retrieval if isinstance(value.retrieval, dict) else {}
    citations = sorted({citation for claim in getattr(value.contract, "claims", ())
                        for citation in claim.citations})
    answer_digest = hashlib.sha256(value.answer.text.encode("utf-8")).hexdigest()
    outcome = {
        "provider": "deepseek",
        "model": "deepseek-flash",
        "repository_id": repository_id,
        "conversation_id": conversation_id,
        "question_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "plan": {
            "intent": getattr(getattr(plan, "mode", None), "value", None),
            "planner_source": getattr(plan, "planner_source", None),
            "task_types": [task.task_type for task in getattr(plan, "tasks", ())],
            "requires_repository_retrieval": getattr(plan, "use_repository", None),
            "requires_tool_execution": getattr(plan, "requires_agent_execution", None),
            "planner_diagnostics": _jsonable(session.phase_diagnostics.get("planner")),
        },
        "retrieval": _safe_retrieval_summary(retrieval),
        "retrieval_search_count": len(searches),
        "orchestrator_retrieval_search_count": max(
            0, len(searches) - len([item for item in metrics.get("agent_tool_search_calls", ())
                                     if item.get("phase") == execution_phase])
        ),
        "candidate_evidence": candidates,
        "built_context_evidence": context_evidence,
        "retrieval_to_context_hashes_match": context_matches_candidates,
        "final_answer_request_evidence_markers": {
            name: final_request.get(name) for name in (
                "contains_current_question", "contains_synthetic_source_path",
                "contains_synthetic_source_formula", "contains_synthetic_function_name",
                "contains_all_built_context_source_ids", "contains_all_built_context_citations",
                "contains_no_built_context_evidence",
                "answer_context_evidence_count", "answer_context_evidence_sha256",
                "request_messages_sha256", "message_count", "roles",
            )
        },
        "answer_contract_status": getattr(value.contract, "status", None),
        "answer_contract_reason": getattr(value.contract, "reason", None),
        "contract_evidence": contract_evidence,
        "answer_citations": citations,
        "answer_nonempty": bool(value.answer.text.strip()),
        "answer_sha256": answer_digest,
        "answer_text_recorded": False,
        "execution_status": value.execution_status,
        "session_restore_status": "PASS" if saved_turn else "FAIL",
        "persisted_message_count_before": len(rows_before),
        "persisted_message_count_after": len(persisted_rows),
        "planner_elapsed_seconds": round(plan_elapsed, 6),
        "execution_elapsed_seconds": round(execution_elapsed, 6),
        "provider_request_stats_before": provider_before,
        "provider_request_stats_after": provider.request_stats(),
        "request_count": len([item for item in metrics["deepseek_chat_events"]
                              if item.get("phase") in {planning_phase, execution_phase}]),
        "agent_execution": _safe_agent_execution_summary(
            _agent_execution_summary_source(value),
        ),
        "final_answer_tool_observations": _tool_observation_summary(
            metrics.get("scenario_tool_observations", {}).get(key, ()),
        ),
        "effective_evidence_count": _effective_evidence_count(retrieval, context_evidence),
    }
    return outcome, value


def _agent_execution_summary_source(value):
    """Prefer the detailed, already-sanitized execution result for either outcome."""
    retrieval = getattr(value, "retrieval", None)
    if isinstance(retrieval, dict):
        product_summary = retrieval.get("agent_execution")
        if isinstance(product_summary, dict) and isinstance(product_summary.get("tool_results"), list):
            return product_summary
        failed_execution = retrieval.get("tool_execution")
        if isinstance(failed_execution, dict) and isinstance(failed_execution.get("results"), list):
            return failed_execution
    direct_summary = getattr(value, "agent_execution", None)
    return direct_summary if isinstance(direct_summary, dict) else None


def _safe_agent_execution_summary(value):
    if not isinstance(value, dict):
        return None
    raw_results = value.get("tool_results")
    if not isinstance(raw_results, (tuple, list)):
        raw_results = value.get("results", ())
    if not isinstance(raw_results, (tuple, list)):
        raw_results = ()
    results = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key) for key in (
            "call_id", "task_id", "tool_id", "status", "error_code", "provenance",
        )}
        failure_phase = item.get("failure_phase")
        if isinstance(failure_phase, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", failure_phase):
            row["failure_phase"] = failure_phase
        metadata = item.get("execution_metadata")
        if isinstance(metadata, dict):
            safe_metadata = {key: metadata[key] for key in (
                "stage", "diagnostic_code", "elapsed_seconds", "attempts", "timeout_seconds",
            ) if key in metadata}
            retry = metadata.get("safe_replanning_retry")
            if isinstance(retry, dict):
                safe_metadata["safe_replanning_retry"] = {
                    key: retry[key] for key in (
                        "status", "reason_code", "signature_hash", "retry_attempts_remaining",
                    ) if key in retry
                }
            argument_diagnostic = metadata.get("argument_diagnostic")
            if isinstance(argument_diagnostic, dict):
                from agentforge.agent_execution import _safe_argument_diagnostic_details
                safe_diagnostic = _safe_argument_diagnostic_details(argument_diagnostic)
                if safe_diagnostic:
                    safe_metadata["argument_diagnostic"] = safe_diagnostic
            diagnostic_details = metadata.get("diagnostic_details")
            if isinstance(diagnostic_details, dict):
                from agentforge.agent_execution import _safe_argument_diagnostic_details
                safe_details = _safe_argument_diagnostic_details(diagnostic_details)
                if safe_details:
                    safe_metadata["diagnostic_details"] = safe_details
            if safe_metadata:
                row["execution_metadata"] = safe_metadata
        results.append(row)
    return {
        "execution_id": value.get("execution_id"),
        "status": value.get("status"),
        "state": value.get("state"),
        "rounds": value.get("rounds"),
        "tool_call_count": value.get("tool_call_count"),
        "observation_count": value.get("observation_count"),
        "planner_request_count": value.get("planner_request_count"),
        "tool_results": results,
    }


def _tool_observation_summary(items):
    output = []
    for item in items:
        if isinstance(item, dict):
            output.append({key: item.get(key) for key in (
                "count", "sha256", "tool_ids", "contains_expected_marker",
                "call_ids", "observation_ids", "verified_evidence_source_ids",
            )})
    return output


def _required_c2_tool_results_succeeded(tool_results):
    """Accept one bounded read-argument correction only when it later succeeds."""
    if not isinstance(tool_results, (tuple, list)) or not tool_results:
        return False
    if any(not isinstance(item, dict) for item in tool_results):
        return False
    call_ids = [item.get("call_id") for item in tool_results]
    if any(not isinstance(value, str) or not value for value in call_ids):
        return False
    if len(set(call_ids)) != len(call_ids):
        return False
    if not any(item.get("tool_id") == "search" and item.get("status") == "completed"
               for item in tool_results):
        return False
    completed_reads = [index for index, item in enumerate(tool_results)
                       if item.get("tool_id") == "read" and item.get("status") == "completed"]
    if not completed_reads:
        return False
    failures = [(index, item) for index, item in enumerate(tool_results)
                if item.get("status") != "completed"]
    if not failures:
        return True
    # The C2 scenario permits exactly one safe Read correction. Search failures,
    # other error classes, repeated invalid reads and unrecovered calls remain FAIL.
    if len(failures) != 1:
        return False
    failed_index, failed = failures[0]
    if failed.get("tool_id") != "read" or failed.get("error_code") != "INVALID_ARGUMENTS":
        return False
    failed_task_id = failed.get("task_id")
    failed_call_id = failed.get("call_id")
    return any(
        index > failed_index
        and item.get("tool_id") == "read"
        and item.get("status") == "completed"
        and item.get("call_id") != failed_call_id
        and item.get("task_id") != failed_task_id
        for index, item in enumerate(tool_results)
    )


def _c2_execution_checks(scenario, metrics):
    """Validate that the real read-only Agent path executed through its guards."""
    execution = scenario.get("agent_execution") or {}
    tool_results = execution.get("tool_results", ())
    used_tool_ids = {item.get("tool_id") for item in tool_results
                     if isinstance(item, dict) and isinstance(item.get("tool_id"), str)}
    discovery = [item for item in metrics.get("tool_discovery", ())
                 if item.get("phase") in {"scenario_C2_planning", "scenario_C2_execution"}]
    schemas = [item for item in metrics.get("tool_schema_loads", ())
               if item.get("phase") == "scenario_C2_execution" and item.get("status") == "PASS"
               and item.get("input_output_schema_loaded") is True]
    validations = [item for item in metrics.get("plan_validations", ())
                   if item.get("phase") == "scenario_C2_execution" and item.get("status") == "PASS"]
    validation_calls = [call for item in validations for call in item.get("calls", ())]
    authorized_ids = {item.get("tool_id") for item in validation_calls
                      if item.get("permission_granted") is True}
    catalog_ids = {tool.get("tool_id") for item in discovery
                   for tool in item.get("tools", ())
                   if isinstance(tool, dict) and tool.get("permission_scope") in
                   set(item.get("permission_scopes") or ())}
    discovered_safe_ids = {tool.get("tool_id") for item in discovery
                           for tool in item.get("tools", ())
                           if isinstance(tool, dict) and tool.get("side_effect") is False
                           and tool.get("requires_approval") is False}
    loaded_schema_ids = {tool_id for item in schemas for tool_id in item.get("tool_ids", ())}
    c2_observations = scenario.get("final_answer_tool_observations", ())
    completed_results = [item for item in tool_results
                         if isinstance(item, dict) and item.get("status") == "completed"]
    search_call_ids = {item.get("call_id") for item in completed_results
                       if item.get("tool_id") == "search" and isinstance(item.get("call_id"), str)}
    read_call_ids = {item.get("call_id") for item in completed_results
                     if item.get("tool_id") == "read" and isinstance(item.get("call_id"), str)}
    observation_ids = {value for item in c2_observations if isinstance(item, dict)
                       for value in item.get("observation_ids", ()) if isinstance(value, str)}
    verified_source_ids = {value for item in c2_observations if isinstance(item, dict)
                           for value in item.get("verified_evidence_source_ids", ())
                           if isinstance(value, str)}
    contract_items = scenario.get("contract_evidence", {}).get("items", ())
    execution_id = execution.get("execution_id")
    repository_id = scenario.get("repository_id")
    searched_source_ids = {source_id for item in metrics.get("agent_tool_search_calls", ())
                           if item.get("phase") == "scenario_C2_execution"
                           for source_id in item.get("source_ids", ())
                           if isinstance(source_id, str)}
    provenance_chain_valid = bool(contract_items) and all(isinstance(item, dict) for item in contract_items)
    if provenance_chain_valid:
        provenance_chain_valid = all(
            isinstance(item.get("provenance"), dict)
            and item["provenance"].get("source_kind") == "verified_agent_read"
            and item["provenance"].get("repository_id") == repository_id
            and item["provenance"].get("execution_id") == execution_id
            and item["provenance"].get("search_call_id") in search_call_ids
            and item["provenance"].get("tool_call_id") in read_call_ids
            and item["provenance"].get("observation_id") == item["provenance"].get("tool_call_id")
            and item["provenance"].get("observation_id") in observation_ids
            and item["provenance"].get("tool_source_id") in searched_source_ids
            and item["provenance"].get("content_hash") == item.get("content_hash")
            and item.get("source_id") in verified_source_ids
            for item in contract_items
        )
    return {
        "planner_requested_tool_execution": scenario.get("plan", {}).get(
            "requires_tool_execution") is True,
        "agent_engine_completed": scenario.get("execution_status") == "completed"
        and execution.get("status") == "completed",
        "authorized_tool_executed": bool(used_tool_ids) and used_tool_ids <= authorized_ids
        and used_tool_ids <= catalog_ids,
        "read_only_scope_preserved": bool(used_tool_ids)
        and used_tool_ids <= discovered_safe_ids,
        "registered_schema_loaded": bool(used_tool_ids)
        and used_tool_ids <= loaded_schema_ids,
        "plan_validator_ran": bool(validation_calls)
        and all(isinstance(item.get("dependency_count"), int) for item in validation_calls),
        "required_tools_completed_after_bounded_recovery": (
            _required_c2_tool_results_succeeded(tool_results)
        ),
        "observation_reached_final_answer_context": bool(c2_observations)
        and any((item.get("count") or 0) > 0
                and item.get("contains_expected_marker") is True
                for item in c2_observations),
        "repository_retrieval_ran_once": scenario.get("orchestrator_retrieval_search_count") == 1,
        "effective_repository_evidence_found": _effective_evidence_count(
            scenario.get("retrieval"), scenario.get("built_context_evidence"),
        ) > 0,
        "retrieval_evidence_reached_context": scenario.get(
            "retrieval_to_context_hashes_match") is True,
        "contract_evidence_hash_linked": (
            scenario.get("contract_evidence", {}).get("content_hash_status") == "PASS"
            and all(item.get("content_hash_linked_to_built_context")
                    for item in scenario.get("contract_evidence", {}).get("items", ()))
        ),
        "agent_read_provenance_chain_valid": provenance_chain_valid,
        "answer_consistent_with_synthetic_evidence": scenario.get(
            "answer_consistent_with_synthetic_evidence") is True,
        "session_reloaded": scenario.get("session_restore_status") == "PASS",
        "synthetic_repository_registration_used": (
            scenario.get("synthetic_repository_registration_status") == "PASS"
        ),
    }


def _collection_summary(snapshot, collection_name, repository_id):
    item = next((row for row in snapshot["collections"] if row["name"] == collection_name), None)
    if item is None:
        return None
    repo_ids = [row for row in item["record_summaries"]
                if row.get("repository_id") == repository_id]
    return {
        "name": item["name"],
        "collection_id": item["collection_id"],
        "collection_count": item["count"],
        "repository_vector_count": len(repo_ids),
        "repository_ids_sha256": _digest(item["repository_ids"]),
        "existing_ids_sha256": _digest(item["existing_ids"]),
        "record_summary_sha256": item["record_summary_sha256"],
        "metadata_sha256": item["metadata_sha256"],
        "resolved_configuration": item["resolved_configuration"],
        "metadata_keys": item["metadata_keys"],
        "repository_record_sha256": _digest(repo_ids),
    }


def _git_state():
    import subprocess
    def call(*args):
        return subprocess.check_output(args, cwd=ROOT, text=True, encoding="utf-8").strip()
    status = call("git", "status", "--porcelain=v1")
    return {
        "branch": call("git", "branch", "--show-current"),
        "head": call("git", "rev-parse", "HEAD"),
        "tag_commit": call("git", "rev-parse", "v0.1.0-rc1^{commit}"),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "status_lines": len(status.splitlines()),
    }


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Phase 5.2.2.3 real host acceptance")
    parser.add_argument(
        "--scenario", choices=("C2",), default=None,
        help="Run only the C2 tool-observation provenance scenario; default runs A/B/C1/C2.",
    )
    parser.add_argument(
        "--capability-probe-evidence", default=None,
        help="Validated synthetic deepseek-flash tool-capability probe evidence for a C2-only run.",
    )
    parser.add_argument(
        "--prior-c2-chat-requests", type=int, default=0,
        help="Number of DeepSeek C2 chat requests already sent in this phase, used to enforce the phase cap.",
    )
    args = parser.parse_args(argv)
    if args.capability_probe_evidence and args.scenario != "C2":
        parser.error("--capability-probe-evidence is allowed only with --scenario C2.")
    if args.prior_c2_chat_requests < 0:
        parser.error("--prior-c2-chat-requests must be non-negative.")
    if args.prior_c2_chat_requests and (
        args.scenario != "C2" or not args.capability_probe_evidence
    ):
        parser.error("--prior-c2-chat-requests requires a C2 run with validated capability evidence.")
    if args.prior_c2_chat_requests + int(bool(args.capability_probe_evidence)) >= MAX_DEEPSEEK_CHAT_REQUESTS:
        parser.error("No DeepSeek chat request budget remains for this phase.")
    return args


def _should_run_active_repository_diagnostics(selected_scenarios):
    """Keep mutating active-repository probes out of synthetic C2-only runs."""
    return selected_scenarios is None


def run(argv=None):
    args = _parse_args(argv)
    selected_scenarios = None if args.scenario is None else (args.scenario,)
    capability_probe_used = bool(args.capability_probe_evidence)
    run_chat_cap = _chat_request_cap(
        capability_probe_used=capability_probe_used,
        prior_c2_chat_requests=args.prior_c2_chat_requests,
    )
    prior_probe_requests = int(capability_probe_used)
    # Display the bounded request maximum before asking for cloud authorization.
    print(
        f"MAX_DEEPSEEK_CHAT_REQUESTS={run_chat_cap}; "
        f"PRIOR_CAPABILITY_PROBE_CHAT_REQUESTS={prior_probe_requests}; "
        f"PRIOR_C2_CHAT_REQUESTS={args.prior_c2_chat_requests}; "
        f"PRIOR_PHASE_CHAT_REQUESTS={prior_probe_requests + args.prior_c2_chat_requests}; "
        f"PHASE_TOTAL_CHAT_CAP={MAX_DEEPSEEK_CHAT_REQUESTS}; "
        "MODEL_DISCOVERY_REQUESTS=0; AUTO_RETRY=0; DATA_SCOPE=synthetic_only",
        flush=True,
    )
    result = {
        "phase": "5.2.2.3",
        "run_id": RUN_ID,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED",
        "git": _git_state(),
        "host_preflight": {},
        "historical_root_cause": {},
        "current_repository_performance": {},
        "current_repository_retrieval": {},
        "synthetic_end_to_end": {"status": "NOT_MEASURED"},
        "chroma_integrity": {"status": "NOT_MEASURED"},
        "data_protection": {"status": "NOT_MEASURED"},
        "cleanup": {"status": "PENDING"},
        "provider_requests": {"deepseek_chat": {}, "embedding": {}},
        "request_budget": {
            "max_deepseek_chat_requests": run_chat_cap,
            "prior_capability_probe_chat_requests": 1 if capability_probe_used else 0,
            "prior_c2_chat_requests": args.prior_c2_chat_requests,
            "prior_phase_chat_requests": prior_probe_requests + args.prior_c2_chat_requests,
            "phase_total_chat_cap": MAX_DEEPSEEK_CHAT_REQUESTS,
            "remaining_phase_chat_requests": run_chat_cap,
            "model_discovery_requests": 0,
            "automatic_retry_limit": 0,
            "data_scope": "synthetic_only",
        },
        "scenario_selection": {
            "requested": list(selected_scenarios) if selected_scenarios else None,
            "default_runs": list(DEFAULT_SYNTHETIC_SCENARIOS),
        },
        "instrumentation": {},
        "failure": None,
    }
    if (result["git"]["branch"] != "feature/auto-chroma-hybrid"
            or result["git"]["head"] != "026bd2978ed315d7fc39b8c3556aedc0f5cd3e37"
            or result["git"]["tag_commit"] != result["git"]["head"]):
        result["failure"] = {"stage": "baseline", "code": "BASELINE_MISMATCH"}
        _write_result(result, [])
        return 2
    if Path(sys.executable).resolve() != VENV.resolve():
        result["failure"] = {"stage": "environment", "code": "PROJECT_VENV_MISMATCH"}
        _write_result(result, [])
        return 2
    if not os.environ.get("AGENTFORGE_DB_PASSWORD"):
        result["failure"] = {"stage": "database_preflight", "code": "DATABASE_CREDENTIAL_NOT_AVAILABLE"}
        _write_result(result, [])
        return 2
    try:
        from agentforge.model_registry import ModelRegistry
        descriptor = ModelRegistry().get_for_request("deepseek-flash")
        result["tool_calling_capability"] = _tool_capability_preflight(
            descriptor, args.capability_probe_evidence,
            expected_file_sha256=(CURRENT_CAPABILITY_PROBE_EVIDENCE_SHA256
                                  if args.capability_probe_evidence else None),
        )
    except Exception as exc:
        result["failure"] = {
            "stage": "capability_preflight",
            "code": "TOOL_CALLING_CAPABILITY_UNVERIFIED",
            "exception_type": type(exc).__name__,
        }
        _write_result(result, [])
        return 2
    if not _confirm_synthetic_cloud_requests(max_chat_requests=run_chat_cap):
        result["status"] = "BLOCKED"
        result["failure"] = {
            "stage": "cloud_authorization",
            "code": "SYNTHETIC_CLOUD_CONSENT_NOT_PROVIDED",
        }
        result["cleanup"] = {"status": "NOT_STARTED"}
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_result(result, [])
        return 2

    import agentforge.scanner as scanner_module
    import agentforge.indexer as indexer_module
    from agentforge.af012_connection import check_database_health
    from agentforge.af012_runtime import (
        chroma_path, collection_for_model, create_persistent_chroma,
        embedding_provider_from_environment,
    )
    from agentforge.cli import (
        _conversation_engine, _model_input_budget, _prepare_repository_index,
        _semantic_summary_callback,
    )
    from agentforge.conversation_orchestrator import (
        ConversationDependencies, ConversationOrchestrator, ConversationRequest,
        ConversationSession,
    )
    from agentforge.conversation_store import ConversationStore
    from agentforge.embeddings.chroma_index import ChromaCodeIndex
    from agentforge.embeddings.chroma_store import ChromaCandidateStore
    from agentforge.indexer import index_repository
    from agentforge.model_registry import ModelRegistry
    from agentforge.product_retrieval import ProductRetriever
    from agentforge.repositories import (
        RepositoryRegistry, open_index_for_repository,
    )
    from agentforge.session_service import SessionService
    from agentforge.store import IndexStore
    from agentforge.embeddings.llama_server import LlamaServerEmbeddingProvider
    from agentforge.agent_loop import ReadOnlyToolbox
    from phase5_1_6_r2_acceptance import apply_launcher_embedding_defaults
    from agentforge.cli import create_provider
    from agentforge.observability import RequestLifecycleTracker, bind_lifecycle

    engine = None
    conversation_store = None
    conversation_id = None
    conversation_ids = []
    repository_id = None
    synthetic_index = None
    client = None
    collection = None
    embedding = None
    trace_lines = []
    metrics = {
        "phase": "preflight",
        "scanner_calls": [],
        "indexer_calls": [],
        "sqlite_file_snapshot_calls": [],
        "sqlite_status_calls": [],
        "sqlite_sync_calls": [],
        "sqlite_chunk_iteration_calls": [],
        "sqlite_symbol_search_calls": [],
        "index_cache": {"hits": 0, "misses": 0, "phases": {}},
        "chroma_consistency_calls": [],
        "chroma_sync_calls": [],
        "chroma_upsert_batches": [],
        "chroma_search_calls": [],
        "embedding_requests": [],
        "product_retriever_calls": [],
        "agent_tool_search_calls": [],
        "deepseek_chat_events": [],
        "network_request_counts": {
            "chat": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0},
            "model_discovery": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0},
        },
        "network_request_events": [],
        "tool_discovery": [],
        "tool_schema_loads": [],
        "plan_validations": [],
        "scenario_tool_observations": {},
        "scenario_built_context_evidence": {},
        "scenario_expectations": {},
        "scenario_chat_attempts": 0,
        "max_scenario_chat_requests": run_chat_cap,
        "deepseek_chat_attempts": 0,
        "max_deepseek_chat_requests": run_chat_cap,
    }
    patchers = []
    pre_snapshot = None
    post_snapshot = None
    active_index_before = None
    active_index_after = None
    active_registry_before = None
    formal_registry_snapshot_before = None
    formal_postgres_snapshot_before = None
    active_repository = None
    active_root = None
    synthetic_registry_owned = False
    synthetic_directory_created = False
    synthetic_index_path = None
    conversation_rows_before = None
    request_tracker = None
    deepseek = None
    query_text = (
        "In this synthetic repository, what formula does function "
        "calculate_weighted_total use, and what result does it produce for "
        "amount 100 and fee rate 0.05?"
    )

    def install(cls, name, wrapper_factory):
        original = getattr(cls, name)
        wrapped = wrapper_factory(original)
        patcher = patch.object(cls, name, wrapped)
        patcher.start()
        patchers.append(patcher)

    try:
        embedding_config = apply_launcher_embedding_defaults()
        embedding = embedding_provider_from_environment()
        ready_seconds = embedding.wait_ready(timeout=8.0, poll_interval=0.5)
        engine = _conversation_engine()
        db_healthy = check_database_health(engine)
        from agentforge.credential_store import CredentialStore
        credential, credential_source = CredentialStore().resolve()
        credential_available = bool(credential)
        del credential
        deepseek = create_provider("deepseek", model="deepseek-flash")
        if not db_healthy or not credential_available:
            raise RuntimeError("Host dependency preflight failed.")

        with RepositoryRegistry(readonly=True) as registry:
            active_repository = registry.active()
            active_registry_before = {
                "active_repository_id": registry.active_id(),
                "repositories": sorted((row["repository_id"], row["canonical_path"],
                                         row["index_path"])
                                        for row in registry.list()),
            }
            formal_registry_snapshot_before = _repository_registry_snapshot_value(
                registry.active_id(), registry.list(),
            )
        if not active_repository:
            raise RuntimeError("No active repository is registered.")
        active_root = Path(active_repository["canonical_path"]).resolve(strict=True)
        with open_index_for_repository(active_root, readonly=True) as store:
            if str(Path(store.database).resolve()) != str(Path(active_repository["index_path"]).resolve()):
                raise RuntimeError("Active repository index path does not match its registry entry.")
            active_meta = store.metadata()
            active_status = store.status()
            active_index_before = _only_counts(store.file_snapshot())
            active_repository_id = active_repository["repository_id"]
        formal_postgres_snapshot_before = _postgres_primary_key_snapshot(engine)
        result["data_protection"] = {
            "status": "PENDING",
            "formal_repository_registry_before": formal_registry_snapshot_before,
            "active_sqlite_before": active_index_before,
            "formal_postgresql_before": formal_postgres_snapshot_before,
        }

        client = create_persistent_chroma()
        collection = collection_for_model(client, embedding.model_id, embedding.dimension)
        initial_consistency = None
        with open_index_for_repository(active_root, readonly=True) as store:
            initial_consistency = ChromaCodeIndex(store, collection, embedding).inspect_consistency()
        if not initial_consistency.get("complete"):
            raise RuntimeError("Active repository Chroma is incomplete; refusing to repair official data during acceptance.")
        pre_snapshot = persistent_collection_snapshot(client)
        official_before = _collection_summary(pre_snapshot, collection.name, active_repository_id)
        if official_before is None:
            raise RuntimeError("Active production Chroma collection was absent from the snapshot.")
        if int(official_before["repository_vector_count"]) != int(active_status.get("chunks", -1)):
            raise RuntimeError("Active SQLite and Chroma counts differ; refusing persistent-store writes.")

        result["host_preflight"] = {
            "status": "HOST_CONFIRMED_PASS",
            "postgresql_app_health": "PASS" if db_healthy else "FAIL",
            "qwen_embedding_health": "PASS",
            "embedding_ready_seconds": ready_seconds,
            "embedding_config": {
                "endpoint_local": embedding_config.get("endpoint_local"),
                "model_id": embedding.model_id,
                "api_model": embedding.api_model,
                "dimension": embedding.dimension,
            },
            "deepseek_provider": "deepseek",
            "deepseek_model": descriptor.model_id,
            "deepseek_context_window": descriptor.context_window,
            "deepseek_max_output_tokens": descriptor.max_output_tokens,
            "deepseek_capability_verified_at": descriptor.capability_verified_at,
            "deepseek_capability_source": descriptor.capability_source,
            "deepseek_credential_available": credential_available,
            "deepseek_credential_source": credential_source,
            "project_venv": str(VENV),
            "chroma_path": str(chroma_path().resolve()),
            "collection_name": collection.name,
            "collection_metadata": dict(collection.metadata or {}),
            "active_repository_id": active_repository_id,
            "active_repository_path": str(active_root),
            "active_sqlite": {"files": active_status.get("files"),
                              "chunks": active_status.get("chunks"),
                              "snapshot": active_index_before,
                              "index_metadata": {
                                  "indexed_at_present": bool(active_meta.get("indexed_at")),
                                  "parser_version": active_meta.get("parser_version"),
                                  "chunk_signature": active_meta.get("chunk_signature"),
                              }},
            "active_chroma_consistency": initial_consistency,
            "official_collection_before": official_before,
            "persistent_collections_before": pre_snapshot["collection_count"],
            "deepseek_chat_before_run": deepseek.request_stats(),
            "embedding_requests_before_run": embedding.request_counts,
            "no_service_start_stop": True,
            "no_database_migration": True,
        }

        def scanner_wrapper(original):
            def wrapped(*args, **kwargs):
                started = time.perf_counter()
                try:
                    report = original(*args, **kwargs)
                    metrics["scanner_calls"].append({
                        "phase": metrics["phase"],
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                        "candidate_count": len(report.candidates),
                        "issue_count": len(report.issues),
                        "complete": report.complete,
                    })
                    return report
                except Exception as exc:
                    metrics["scanner_calls"].append({"phase": metrics["phase"],
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                        "error_type": type(exc).__name__})
                    raise
            return wrapped

        def cache_wrapper(original):
            def wrapped(key):
                value = original(key)
                kind = "hits" if value is not None else "misses"
                metrics["index_cache"][kind] += 1
                phases = metrics["index_cache"]["phases"].setdefault(metrics["phase"],
                    {"hits": 0, "misses": 0})
                phases[kind] += 1
                return value
            return wrapped

        simple_timed = lambda target, shape=None: _simple_timed_factory(metrics, target, shape)

        install(scanner_module, "scan_repository", scanner_wrapper)
        install(indexer_module, "_cache_get", cache_wrapper)
        install(IndexStore, "file_snapshot", simple_timed(
            "sqlite_file_snapshot_calls", lambda value: {"row_count": len(value)}))
        install(IndexStore, "status", simple_timed(
            "sqlite_status_calls", lambda value: {"files": value.get("files"),
                                                    "chunks": value.get("chunks")}))
        install(IndexStore, "sync", simple_timed(
            "sqlite_sync_calls", lambda value: {key: value.get(key)
                                                  for key in ("added", "changed", "removed")}))
        install(IndexStore, "iter_chunks", simple_timed("sqlite_chunk_iteration_calls"))
        install(IndexStore, "search_symbols", simple_timed(
            "sqlite_symbol_search_calls", lambda value: {"result_count": len(value)}))
        def indexer_wrapper(original):
            def wrapped(*args, **kwargs):
                started = time.perf_counter()
                try:
                    value = original(*args, **kwargs)
                    metrics["indexer_calls"].append({
                        "phase": metrics["phase"],
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                        **{key: value.get(key) for key in
                           ("index_unchanged", "added", "changed", "unchanged", "removed",
                            "files_read", "files_reused", "chunks_reused")},
                    })
                    return value
                except Exception as exc:
                    metrics["indexer_calls"].append({
                        "phase": metrics["phase"],
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                        "error_type": type(exc).__name__,
                    })
                    raise
            return wrapped
        install(indexer_module, "index_repository", indexer_wrapper)
        install(ChromaCodeIndex, "inspect_consistency", simple_timed(
            "chroma_consistency_calls", lambda value: {key: value.get(key) for key in
                ("complete", "expected_vectors", "stored_vectors", "missing_vectors", "stale_vectors")}))
        install(ChromaCodeIndex, "sync", simple_timed(
            "chroma_sync_calls", lambda value: {"embedded": value.embedded,
                "unchanged": value.unchanged, "removed": value.removed,
                "elapsed_seconds": value.elapsed_seconds}))
        install(ChromaCandidateStore, "upsert_many", simple_timed("chroma_upsert_batches"))
        install(ChromaCandidateStore, "search_with_documents", simple_timed(
            "chroma_search_calls", lambda value: {"result_count": len(value)}))
        install(LlamaServerEmbeddingProvider, "_request_embeddings",
                lambda original: _embedding_wrapper(original, metrics))
        install(ProductRetriever, "search",
                lambda original: _product_search_wrapper(original, metrics))
        install(ReadOnlyToolbox, "_tool_search",
                lambda original: _agent_tool_search_wrapper(original, metrics))
        from agentforge.providers.deepseek import DeepSeekClient, DeepSeekProvider
        from agentforge.agent_execution import PlanValidator, ToolDiscovery
        install(DeepSeekClient, "_request",
                lambda original: _deepseek_network_request_wrapper(original, metrics))
        install(DeepSeekClient, "_connect",
                lambda original: _deepseek_connect_wrapper(original, metrics))
        install(DeepSeekClient, "chat",
                lambda original: _deepseek_chat_wrapper(original, metrics))
        install(ToolDiscovery, "discover",
                lambda original: _tool_discovery_wrapper(original, metrics))
        install(ToolDiscovery, "schemas_for",
                lambda original: _tool_schema_wrapper(original, metrics))
        install(PlanValidator, "validate",
                lambda original: _plan_validation_wrapper(original, metrics))
        for answer_method_name in ("answer", "answer_general", "answer_hybrid"):
            install(DeepSeekProvider, answer_method_name,
                    lambda original: _provider_answer_wrapper(original, metrics))

        if _should_run_active_repository_diagnostics(selected_scenarios):
            # Actual current repository hot path. The first pass warms process-local
            # fingerprints; the second measures a steady-state unchanged repository.
            metrics["phase"] = "active_repository_prepare_cold_cache"
            with open_index_for_repository(active_root, readonly=False) as active_store:
                active_vector = ChromaCodeIndex(active_store, collection, embedding)
                cold_start = time.perf_counter()
                cold_prepared = _prepare_repository_index(active_store,
                    _request_for(active_root, "local-prep", cloud=False), active_vector)
                cold_elapsed = time.perf_counter() - cold_start
                metrics["phase"] = "active_repository_prepare_warm_cache"
                warm_start = time.perf_counter()
                warm_prepared = _prepare_repository_index(active_store,
                    _request_for(active_root, "local-prep", cloud=False), active_vector)
                warm_elapsed = time.perf_counter() - warm_start
                active_index_after = _only_counts(active_store.file_snapshot())
                active_status_after = active_store.status()

                metrics["phase"] = "active_repository_local_hybrid_search"
                scan_started = time.perf_counter()
                active_scan = scanner_module.scan_repository(active_root)
                active_scan_elapsed = time.perf_counter() - scan_started
                active_retriever = ProductRetriever(active_store, active_root,
                    vector_retriever=active_vector, scan_report=active_scan)
                active_search_started = time.perf_counter()
                active_outcome = active_retriever.search(
                    "Where is semantic_summary_turn_input_too_large detected and handled?", top_k=5)
                active_search_elapsed = time.perf_counter() - active_search_started
                active_rows = [{
                    "citation": item.citation,
                    "channels": list(item.channels),
                    "content_hash": item.content_hash,
                    "score": round(item.score, 6),
                } for item in active_outcome.candidates[:8]]
                active_vector_count = sum("vector" in item.channels for item in active_outcome.candidates)
                active_lexical_count = sum("lexical" in item.channels for item in active_outcome.candidates)
                active_symbol_count = sum("symbols" in item.channels for item in active_outcome.candidates)

            with RepositoryRegistry(readonly=True) as registry:
                active_registry_after = {
                    "active_repository_id": registry.active_id(),
                    "repositories": sorted((row["repository_id"], row["canonical_path"],
                                             row["index_path"])
                                            for row in registry.list()),
                }
            active_prepare_embedding_events = [item for item in metrics["embedding_requests"]
                if item["phase"] in {"active_repository_prepare_cold_cache",
                                     "active_repository_prepare_warm_cache"}]
            active_prepare_document_items = sum(item["item_count"] for item in active_prepare_embedding_events
                                                if item["kind"] == "documents")
            result["current_repository_performance"] = {
                "status": "PASS" if (warm_prepared["index_result"].get("index_unchanged")
                                      and active_index_before == active_index_after
                                      and active_prepare_document_items == 0
                                      and active_registry_before == active_registry_after) else "FAIL",
                "repository_path": str(active_root),
                "sqlite_files": active_status_after.get("files"),
                "sqlite_chunks": active_status_after.get("chunks"),
                "cold_process_cache_prepare_seconds": round(cold_elapsed, 6),
                "warm_unchanged_prepare_seconds": round(warm_elapsed, 6),
                "cold_index_result": _jsonable(cold_prepared["index_result"]),
                "warm_index_result": _jsonable(warm_prepared["index_result"]),
                "sqlite_snapshot_before_after_equal": active_index_before == active_index_after,
                "active_registry_structure_before_after_equal": active_registry_before == active_registry_after,
                "active_registry_indexed_at_refreshed_by_normal_prepare": True,
                "active_repository_scan_count": sum(1 for item in metrics["scanner_calls"]
                    if item["phase"].startswith("active_repository_prepare_")),
                "active_repository_indexer_call_count": sum(1 for item in metrics["indexer_calls"]
                    if item["phase"].startswith("active_repository_prepare_")),
                "sqlite_file_snapshot_checks_during_active_prepare": sum(1 for item in metrics["sqlite_file_snapshot_calls"]
                    if item["phase"].startswith("active_repository_prepare_")),
                "sqlite_status_checks_during_active_prepare": sum(1 for item in metrics["sqlite_status_calls"]
                    if item["phase"].startswith("active_repository_prepare_")),
                "document_embedding_items_during_active_prepare": active_prepare_document_items,
                "active_hybrid_query_chroma_calls": sum(1 for item in metrics["chroma_search_calls"]
                    if item["phase"] == "active_repository_local_hybrid_search"),
                "active_hybrid_query_sqlite_symbol_calls": sum(1 for item in metrics["sqlite_symbol_search_calls"]
                    if item["phase"] == "active_repository_local_hybrid_search"),
                "active_hybrid_query_sqlite_chunk_iterations": sum(1 for item in metrics["sqlite_chunk_iteration_calls"]
                    if item["phase"] == "active_repository_local_hybrid_search"),
                "cache_counts": metrics["index_cache"],
                "embedding_requests_during_current_repo_prepare_and_search": embedding.request_counts,
                "embedding_request_events": list(metrics["embedding_requests"]),
                "chroma_sync_events": list(metrics["chroma_sync_calls"]),
                "chroma_upsert_batches": list(metrics["chroma_upsert_batches"]),
                "chroma_integrity_events": list(metrics["chroma_consistency_calls"]),
                "expected_unchanged_reembedding": 0,
            }
            result["current_repository_retrieval"] = {
                "status": "PASS" if (active_outcome.candidates and
                                      "vector" not in active_outcome.unavailable_channels and
                                      "lexical" not in active_outcome.unavailable_channels) else "FAIL",
                "search_count": 1,
                "elapsed_seconds": round(active_search_elapsed, 6),
                "scan_elapsed_seconds": round(active_scan_elapsed, 6),
                "candidate_count": len(active_outcome.candidates),
                "vector_channel_candidate_count": active_vector_count,
                "lexical_channel_candidate_count": active_lexical_count,
                "symbol_channel_candidate_count": active_symbol_count,
                "unavailable_channels": list(active_outcome.unavailable_channels),
                "verified_candidate_summaries": active_rows,
                "source_text_recorded": False,
                "query_sent_to_cloud": False,
            }

        else:
            result["current_repository_performance"] = {
                "status": "NOT_MEASURED",
                "reason": "C2-only run skips the mutating active-repository preparation probe.",
            }
            result["current_repository_retrieval"] = {
                "status": "NOT_MEASURED",
                "reason": "C2-only run uses only the synthetic repository for end-to-end retrieval.",
            }

        # Fault-inject only in memory and only for the synthetic repository.
        # The live embedding service and persistent collection are untouched.
        metrics["phase"] = "synthetic_setup"
        SYNTHETIC_PARENT.mkdir(parents=True, exist_ok=False)
        synthetic_directory_created = True
        OWNER_MARKER.write_text(json.dumps({"run_id": RUN_ID, "purpose": "synthetic acceptance"}),
                                encoding="utf-8")
        SYNTHETIC_ROOT.mkdir()
        source = (
            "\"\"\"Synthetic source for a local RAG acceptance; no real project data.\"\"\"\n"
            "\n"
            "def calculate_weighted_total(amount: float, fee_rate: float) -> float:\n"
            "    \"\"\"Apply a proportional fee and round the result to cents.\"\"\"\n"
            "    return round(amount * (1 + fee_rate), 2)\n"
        )
        (SYNTHETIC_ROOT / "synthetic_calculation.py").write_text(source, encoding="utf-8")
        from agentforge.repositories import repository_identity
        repository_id = repository_identity(SYNTHETIC_ROOT)
        synthetic_index_path = RepositoryRegistry.index_path(repository_id)
        if synthetic_index_path.exists() or synthetic_index_path.is_symlink():
            raise RuntimeError("Synthetic repository index path already exists; refusing collision.")
        with RepositoryRegistry(readonly=True) as registry:
            try:
                registry.get(repository_id)
            except ValueError:
                pass
            else:
                raise RuntimeError("Synthetic repository ID is already registered; refusing collision.")
        if collection.get(where={"repository_id": repository_id}, include=["metadatas"])["ids"]:
            raise RuntimeError("Synthetic repository ID already has persistent vectors; refusing collision.")
        with RepositoryRegistry() as registry:
            try:
                registry.get(repository_id)
            except ValueError:
                pass
            else:
                raise RuntimeError("Synthetic repository ID was registered concurrently; refusing reuse.")
            synthetic_entry = registry.register(
                SYNTHETIC_ROOT,
                display_name=f"Synthetic Phase 5.2.2.3 {SHORT_ID}",
                index_path=synthetic_index_path,
            )
            synthetic_registry_owned = True
            if (Path(synthetic_entry["canonical_path"]).resolve() != SYNTHETIC_ROOT.resolve()
                    or Path(synthetic_entry["index_path"]).resolve() != synthetic_index_path.resolve()):
                raise RuntimeError("Synthetic repository registration did not preserve its owned paths.")
        synthetic_index = open_index_for_repository(SYNTHETIC_ROOT, readonly=False)
        synthetic_vector = ChromaCodeIndex(synthetic_index, collection, embedding)
        conversation_store = ConversationStore(engine)
        if conversation_store.list_conversations(repository_id):
            raise RuntimeError("Synthetic repository ID already has conversation records; refusing collision.")
        conversation = conversation_store.create_conversation(
            repository_id, f"Synthetic Phase 5.2.2.3 {SHORT_ID}")
        conversation_id = conversation["id"]
        conversation_ids.append(conversation_id)
        explicit_consent = datetime.now(timezone.utc)
        conversation_store.update_model_binding(
            conversation_id, repository_id, provider_id="deepseek",
            model_id="deepseek-flash", cloud_consent_at=explicit_consent,
        )
        long_turn = ("Synthetic historical acceptance text; it is not an instruction. "
                     + "synthetic-history-marker " * 180)
        for turn in range(1, 9):
            user_text = long_turn if turn == 2 else f"Synthetic prior question {turn}."
            assistant_text = f"Synthetic prior answer {turn}."
            conversation_store.append_turn(conversation_id, repository_id,
                user_text, assistant_text, provider_id="offline", model_id="offline")
        conversation_rows_before = conversation_store.get_messages(conversation_id, repository_id)
        existing_summary_text = "Existing synthetic summary checkpoint for the first completed turn."
        conversation_store.save_summary(
            conversation_id, repository_id, conversation_rows_before[1]["id"],
            existing_summary_text, model_name="offline",
        )
        existing_summary_before = conversation_store.latest_summary(conversation_id, repository_id)
        summary_candidate_bytes = len(json.dumps([
            {"id": existing_summary_before["covered_message_id"],
             "sequence": existing_summary_before["covered_through"], "role": "summary",
             "content": existing_summary_before["content"]},
            *[{key: row[key] for key in ("id", "sequence", "role", "content")}
              for row in conversation_rows_before[2:4]],
        ], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        raw_history_hash_before = _digest([
            {"sequence": row["sequence"], "role": row["role"],
             "content_sha256": hashlib.sha256(row["content"].encode("utf-8")).hexdigest()}
            for row in conversation_rows_before
        ])

        from agentforge.cli import create_provider
        deepseek_before = deepseek.request_stats()
        embedding_before_synthetic_turn = dict(embedding.request_counts)
        current_conversation = conversation_store.get_conversation(conversation_id, repository_id)
        current_conversation.update({
            "conversation_id": conversation_id,
            "repository_id": repository_id,
            "summary": existing_summary_before["content"],
        })
        session = ConversationSession(
            provider=deepseek, store=conversation_store, repository_id=repository_id,
            rows=tuple(conversation_rows_before), conversation=current_conversation,
        )
        request = ConversationRequest(
            query=query_text, repository=str(SYNTHETIC_ROOT), provider_id="deepseek",
            model_id="deepseek-flash", conversation_id=conversation_id,
            use_chroma=True, mode="auto", cloud_data_authorized=True,
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: deepseek,
            model_budget_resolver=_model_input_budget,
            summary_callback_factory=_semantic_summary_callback,
            session_service=SessionService(),
            repository_preparer=_prepare_repository_index,
        )
        orchestrator = ConversationOrchestrator(dependencies)
        from agentforge.conversation_agent import ConversationAgentExecutor
        tool_orchestrator = ConversationOrchestrator(replace(
            dependencies, agent_executor=ConversationAgentExecutor(engine=engine),
        ))
        tracker = RequestLifecycleTracker(
            request_id=RUN_ID[:12], log_path=RAW_TRACE,
        )
        tracker.start_request(provider="deepseek", model="deepseek-flash",
                              session_id=conversation_id, mode="auto", streaming_enabled=False)
        scenario_b = {"status": "NOT_MEASURED", "scenario": "B"}
        scenario_b_result = None
        scenario_c1 = {"status": "NOT_MEASURED", "scenario": "C1"}
        scenario_c1_result = None
        scenario_c2 = {"status": "NOT_MEASURED", "scenario": "C2"}
        scenario_c2_result = None
        if selected_scenarios is None or "A" in selected_scenarios:
            metrics["scenario_expectations"]["scenario_A_planning"] = {
                "question": query_text,
            }
            metrics["scenario_expectations"]["scenario_A_execution"] = {
                "question": query_text,
                "source_path": "synthetic_calculation.py",
                "source_formula": "return round(amount * (1 + fee_rate), 2)",
            }
            metrics["phase"] = "scenario_A_planning"
            started_plan = time.perf_counter()
            scenario_a_diagnostic = None
            plan = None
            conversation_result = None
            plan_elapsed = 0.0
            execution_elapsed = 0.0
            with bind_lifecycle(tracker):
                try:
                    plan = orchestrator.plan(request, session=session, index_store=synthetic_index)
                    plan_elapsed = time.perf_counter() - started_plan
                    metrics["phase"] = "scenario_A_execution"
                    run_started = time.perf_counter()
                    conversation_result = orchestrator.run(
                        request, index_store=synthetic_index, vector_retriever=synthetic_vector,
                        session=session, planned_plan=plan,
                    )
                    execution_elapsed = time.perf_counter() - run_started
                    tracker.finish("COMPLETED")
                except Exception as exc:
                    scenario_a_diagnostic = _safe_failure_diagnostics(exc, metrics["phase"])
                    tracker.finish("FAILED", error_code=type(exc).__name__)
                    if plan is None:
                        plan = SimpleNamespace(
                            mode=None, planner_source="failed", tasks=(), use_repository=True,
                            requires_agent_execution=False,
                        )
                    conversation_result = SimpleNamespace(
                        summary_diagnostics=(), contract=SimpleNamespace(
                            status="refused", evidence=(), claims=(),
                        ), answer=SimpleNamespace(text=""), retrieval={},
                        execution_status="failed",
                    )
                plan_summary = {
                    "intent": getattr(plan, "mode", None).value if getattr(plan, "mode", None) else None,
                    "planner_source": getattr(plan, "planner_source", None),
                    "task_types": [task.task_type for task in getattr(plan, "tasks", ())],
                    "requires_repository": getattr(plan, "use_repository", None),
                    "requires_tool_execution": getattr(plan, "requires_agent_execution", None),
                    "planner_diagnostics": _jsonable(session.phase_diagnostics.get("planner")),
                }
            metrics["phase"] = "scenario_A_result_extraction"
            after_rows = conversation_store.get_messages(conversation_id, repository_id)
            restored_context = SessionService().load_context(
                conversation_store, conversation_id, repository_id,
            )
            restored_history_hash = _digest([
                {"sequence": row["sequence"], "role": row["role"],
                 "content_sha256": hashlib.sha256(row["content"].encode("utf-8")).hexdigest()}
                for row in after_rows[:len(conversation_rows_before)]
            ])
            summary_diagnostics = _jsonable(conversation_result.summary_diagnostics)
            existing_summary_after = conversation_store.latest_summary(conversation_id, repository_id)
            existing_summary_hash_unchanged = hashlib.sha256(
                existing_summary_before["content"].encode("utf-8")).hexdigest() == hashlib.sha256(
                existing_summary_after["content"].encode("utf-8")).hexdigest()
            summary_failure_seen = any(
                item.get("failure_code") == "semantic_summary_turn_input_too_large"
                for item in summary_diagnostics if isinstance(item, dict)
            )
            contract_status = getattr(conversation_result.contract, "status", None)
            context_batches = metrics["scenario_built_context_evidence"].get("A", [])
            built_context_evidence = context_batches[-1] if context_batches else []
            contract_evidence = _contract_evidence_summary(
                conversation_result.contract, built_context_evidence,
            )
            retrieval_payload = conversation_result.retrieval
            scenario_searches = [item for item in metrics["product_retriever_calls"]
                                 if item.get("phase") == "scenario_A_execution"]
            candidate_summaries = [candidate for item in scenario_searches
                                   for candidate in item.get("candidate_summaries", ())]
            scenario_chat_events = [item for item in metrics["deepseek_chat_events"]
                                    if item.get("phase") == "scenario_A_execution"]
            final_request_event = scenario_chat_events[-1] if scenario_chat_events else {}
            channels = sorted({channel for candidate in candidate_summaries
                               for channel in candidate.get("channels", ())})
            context_matches_candidates = bool(built_context_evidence) and all(any(
                candidate.get("citation") == evidence.get("citation")
                and candidate.get("content_hash") == evidence.get("content_hash")
                and candidate.get("excerpt_sha256") == evidence.get("excerpt_sha256")
                for candidate in candidate_summaries
            ) for evidence in built_context_evidence)
            candidate_provenance_valid = bool(candidate_summaries) and all(
                isinstance(item.get("content_hash"), str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", item["content_hash"]))
                and isinstance(item.get("excerpt_sha256"), str)
                and bool(re.fullmatch(r"[0-9a-f]{64}", item["excerpt_sha256"]))
                and bool(re.fullmatch(r"[^:]+:\d+-\d+", item.get("citation", "")))
                for item in candidate_summaries
            )
            answer_text_for_checks = conversation_result.answer.text.casefold()
            answer_has_expected_result = bool(re.search(r"(?<!\d)105(?:[.,]0{1,2})?(?!\d)", answer_text_for_checks))
            answer_has_formula = (
                "amount" in answer_text_for_checks
                and any(value in answer_text_for_checks for value in ("fee_rate", "0.05", "5%"))
                and any(value in answer_text_for_checks for value in ("round", "rounded", "decimal"))
            )
            history_hash_unchanged = raw_history_hash_before == restored_history_hash
            persisted_turn_count_ok = len(after_rows) == len(conversation_rows_before) + 2
            session_reloaded = bool(
                len(restored_context.rows) == len(after_rows)
                and restored_context.rows[-2].get("content") == query_text
                and restored_context.rows[-1].get("content") == conversation_result.answer.text
            )
            scenario_a_checks = {
                "execution_completed": conversation_result.execution_status == "completed",
                "planner_did_not_request_tools": not plan.requires_agent_execution,
                "repository_retrieval_ran_once": len(scenario_searches) == 1,
                "vector_candidates_present": any("vector" in item.get("channels", ()) for item in candidate_summaries),
                "lexical_or_symbol_candidates_present": any(
                    "lexical" in item.get("channels", ())
                    for item in candidate_summaries
                ),
                "candidate_path_line_and_hash_valid": candidate_provenance_valid,
                "verified_evidence_reached_built_context": context_matches_candidates,
                "contract_evidence_linked_to_context_hash": (
                    bool(contract_evidence["items"])
                    and contract_evidence["content_hash_status"] == "PASS"
                    and all(item["content_hash_linked_to_built_context"]
                            for item in contract_evidence["items"])
                ),
                "evidence_present_in_final_deepseek_request": bool(
                    final_request_event.get("contains_current_question")
                    and final_request_event.get("contains_synthetic_source_formula")
                    and final_request_event.get("contains_all_built_context_source_ids")
                    and contract_evidence["content_hash_status"] == "PASS"
                    and all(item["content_hash_linked_to_built_context"]
                            for item in contract_evidence["items"])
                ),
                "answer_matches_synthetic_formula_and_result": (
                    answer_has_expected_result and answer_has_formula
                ),
                "summary_overflow_fell_back": summary_failure_seen,
                "history_and_session_persisted": (
                    history_hash_unchanged and persisted_turn_count_ok and session_reloaded
                ),
            }
            result["synthetic_end_to_end"] = {
                "status": _scenario_status(scenario_a_checks, contract_status),
                "provider": "deepseek",
                "model": "deepseek-flash",
                "repository_id": repository_id,
                "conversation_id": conversation_id,
                "synthetic_question_sha256": hashlib.sha256(query_text.encode("utf-8")).hexdigest(),
                "oversized_turn_summary_candidate_bytes": summary_candidate_bytes,
                "existing_summary_before_sha256": hashlib.sha256(
                    existing_summary_before["content"].encode("utf-8")).hexdigest(),
                "existing_summary_preserved": existing_summary_hash_unchanged,
                "preloaded_turns": 8,
                "raw_history_messages_before": len(conversation_rows_before),
                "raw_history_messages_after": len(after_rows),
                "raw_history_content_hashes_unchanged": raw_history_hash_before == restored_history_hash,
                "session_persistence_status": "PASS" if session_reloaded and persisted_turn_count_ok else "FAIL",
                "session_reloaded": session_reloaded,
                "planner_elapsed_seconds": round(plan_elapsed, 6),
                "execution_elapsed_seconds": round(execution_elapsed, 6),
                "planner": plan_summary,
                "retrieval": {
                    **{key: retrieval_payload[key] for key in (
                        "mode", "route", "rule", "confidence", "planner_source",
                        "answer_strategy", "requires_agent_execution",
                    ) if key in retrieval_payload},
                    **_safe_retrieval_summary(retrieval_payload),
                },
                "product_retriever_call_count_during_synthetic_turn": len(scenario_searches),
                "summary_overflow_failure_observed": summary_failure_seen,
                "summary_diagnostics": summary_diagnostics,
                "answer_contract_status": contract_status,
                "answer_nonempty": bool(conversation_result.answer.text.strip()),
                "answer_sha256": hashlib.sha256(conversation_result.answer.text.encode("utf-8")).hexdigest(),
                "answer_citations": sorted({citation for claim in getattr(
                    conversation_result.contract, "claims", ()) for citation in claim.citations}),
                "contract_evidence": contract_evidence,
                "candidate_evidence": candidate_summaries,
                "built_context_evidence": built_context_evidence,
                "retrieval_to_context_hashes_match": context_matches_candidates,
                "final_answer_request_evidence_markers": {
                    key: final_request_event.get(key) for key in (
                        "contains_current_question", "contains_synthetic_source_path",
                        "contains_synthetic_source_formula", "contains_all_built_context_source_ids",
                        "contains_all_built_context_citations", "contains_no_built_context_evidence",
                        "message_count", "roles", "request_messages_sha256",
                    )
                },
                "checks": scenario_a_checks,
                "diagnostic": scenario_a_diagnostic,
                "provider_request_stats_before": deepseek_before,
                "provider_request_stats_after": deepseek.request_stats(),
                "embedding_request_counts_before": embedding_before_synthetic_turn,
                "embedding_request_counts_after": dict(embedding.request_counts),
                "request_trace_file": RAW_TRACE.name,
                "answer_text_recorded": False,
            }
        else:
            result["synthetic_end_to_end"] = {"status": "NOT_MEASURED", "scenario": "A"}
        result["provider_requests"]["deepseek_chat"] = deepseek.request_stats()
        result["provider_requests"]["embedding"] = {
            "counts": dict(embedding.request_counts),
            "events": list(metrics["embedding_requests"]),
            "document_requests": sum(1 for item in metrics["embedding_requests"]
                                      if item["kind"] == "documents"),
            "document_items": sum(item["item_count"] for item in metrics["embedding_requests"]
                                   if item["kind"] == "documents"),
            "query_requests": sum(1 for item in metrics["embedding_requests"]
                                  if item["kind"] == "query"),
            "elapsed_ms": round(sum(item["elapsed_ms"] for item in metrics["embedding_requests"]), 3),
        }
        if selected_scenarios is None or "B" in selected_scenarios:
            try:
                scenario_b, scenario_b_result = _run_synthetic_answer_scenario(
                    "B", "In general, what is the difference between mitosis and meiosis?",
                    repository_id=repository_id, repository_root=SYNTHETIC_ROOT,
                    index_store=synthetic_index, vector_retriever=synthetic_vector,
                    conversation_store=conversation_store, conversation_ids=conversation_ids,
                    orchestrator=orchestrator, provider=deepseek, metrics=metrics, run_id=RUN_ID,
                )
            except Exception as exc:
                scenario_b = {"status": "FAIL", "scenario": "B",
                              "diagnostic": _safe_failure_diagnostics(exc, "scenario_B")}
                scenario_b_result = None
            b_retrieval = scenario_b.get("retrieval", {})
            b_markers = scenario_b.get("final_answer_request_evidence_markers", {})
            b_checks = {
                "execution_completed": scenario_b.get("execution_status") == "completed",
                "planner_did_not_request_tools": scenario_b.get("plan", {}).get("requires_tool_execution") is False,
                "repository_retrieval_ran_once": scenario_b.get("orchestrator_retrieval_search_count") == 1,
                "no_effective_repository_evidence": _effective_evidence_count(
                    b_retrieval, scenario_b.get("built_context_evidence"),
                ) == 0,
                "built_context_has_no_repository_evidence": not scenario_b.get("built_context_evidence"),
                "general_answer_generated": scenario_b.get("answer_nonempty") is True,
                "no_repository_citations": not scenario_b.get("answer_citations"),
                "no_synthetic_source_in_final_request": not (
                    b_markers.get("contains_synthetic_source_path")
                    or b_markers.get("contains_synthetic_source_formula")
                ) and b_markers.get("contains_no_built_context_evidence") is True,
                "session_reloaded": scenario_b.get("session_restore_status") == "PASS",
            }
            scenario_b["checks"] = b_checks
            if scenario_b_result is not None:
                scenario_b["status"] = _scenario_status(
                    b_checks, scenario_b.get("answer_contract_status"),
                )

        else:
            scenario_b = {"status": "NOT_MEASURED", "scenario": "B"}
            scenario_b_result = None
        if selected_scenarios is None or "C1" in selected_scenarios:
            try:
                scenario_c1, scenario_c1_result = _run_synthetic_answer_scenario(
                    "C1", "In this synthetic repository, what formula does calculate_weighted_total use, "
                    "and what result does it produce for amount 100 and fee rate 0.05?",
                    repository_id=repository_id, repository_root=SYNTHETIC_ROOT,
                    index_store=synthetic_index, vector_retriever=synthetic_vector,
                    conversation_store=conversation_store, conversation_ids=conversation_ids,
                    orchestrator=tool_orchestrator, provider=deepseek, metrics=metrics, run_id=RUN_ID,
                )
            except Exception as exc:
                scenario_c1 = {"status": "FAIL", "scenario": "C1",
                               "diagnostic": _safe_failure_diagnostics(exc, "scenario_C1")}
                scenario_c1_result = None
            c1_retrieval = scenario_c1.get("retrieval", {})
            c1_markers = scenario_c1.get("final_answer_request_evidence_markers", {})
            c1_answer_text = (scenario_c1_result.answer.text.casefold()
                              if scenario_c1_result is not None else "")
            c1_answer_consistent = (
                bool(re.search(r"(?<!\d)105(?:[.,]0{1,2})?(?!\d)", c1_answer_text))
                and "amount" in c1_answer_text
                and any(value in c1_answer_text for value in ("fee_rate", "0.05", "5%"))
            )
            c1_checks = {
                "execution_completed": scenario_c1.get("execution_status") == "completed",
                "planner_did_not_request_tools": scenario_c1.get("plan", {}).get("requires_tool_execution") is False,
                "no_agent_execution_report": scenario_c1.get("agent_execution") is None,
                "repository_retrieval_ran_once": scenario_c1.get("orchestrator_retrieval_search_count") == 1,
                "effective_repository_evidence_found": (c1_retrieval.get("evidence_count", 0) or 0) > 0,
                "retrieval_evidence_reached_context": scenario_c1.get("retrieval_to_context_hashes_match") is True,
                "contract_evidence_linked_to_context_hash": (
                    scenario_c1.get("contract_evidence", {}).get("content_hash_status") == "PASS"
                    and all(item.get("content_hash_linked_to_built_context")
                            for item in scenario_c1.get("contract_evidence", {}).get("items", ()))
                ),
                "evidence_present_in_final_deepseek_request": _c1_structured_context_check(scenario_c1),
                "answer_consistent_with_synthetic_evidence": c1_answer_consistent,
                "session_reloaded": scenario_c1.get("session_restore_status") == "PASS",
            }
            scenario_c1["checks"] = c1_checks
            scenario_c1["status"] = _scenario_status(
                c1_checks, scenario_c1.get("answer_contract_status"),
            )

        else:
            scenario_c1 = {"status": "NOT_MEASURED", "scenario": "C1"}
            scenario_c1_result = None
        if selected_scenarios is None or "C2" in selected_scenarios:
            scenario_c2 = None
            scenario_c2_result = None
            try:
                scenario_c2, scenario_c2_result = _run_synthetic_answer_scenario(
                    "C2", "Use an authorized read-only tool to open synthetic_calculation.py in this "
                    "repository, then report the formula in calculate_weighted_total and its result "
                    "for amount 100 with fee rate 0.05.",
                    repository_id=repository_id, repository_root=SYNTHETIC_ROOT,
                    index_store=synthetic_index, vector_retriever=synthetic_vector,
                    conversation_store=conversation_store, conversation_ids=conversation_ids,
                    orchestrator=tool_orchestrator, provider=deepseek, metrics=metrics, run_id=RUN_ID,
                    tool_observation_markers=("calculate_weighted_total", "round(amount * (1 + fee_rate), 2)"),
                )
                with RepositoryRegistry(readonly=True) as registry:
                    try:
                        registered = registry.get(repository_id)
                    except ValueError:
                        registered = None
                registration_matches = bool(
                    registered
                    and Path(registered["canonical_path"]).resolve() == SYNTHETIC_ROOT.resolve()
                    and Path(registered["index_path"]).resolve() == synthetic_index_path.resolve()
                    and registered.get("indexed_at")
                )
                scenario_c2["synthetic_repository_registration_status"] = (
                    "PASS" if registration_matches and synthetic_registry_owned else "FAIL"
                )
                c2_text = scenario_c2_result.answer.text.casefold()
                scenario_c2["answer_consistent_with_synthetic_evidence"] = (
                    bool(re.search(r"(?<!\d)105(?:[.,]0{1,2})?(?!\d)", c2_text))
                    and "amount" in c2_text
                    and any(value in c2_text for value in ("fee_rate", "0.05", "5%"))
                )
                scenario_c2["checks"] = _c2_execution_checks(scenario_c2, metrics)
                scenario_c2["status"] = _scenario_status(
                    scenario_c2["checks"], scenario_c2.get("answer_contract_status"),
                )
            except Exception as exc:
                scenario_c2 = {
                    "status": "FAIL", "scenario": "C2",
                    "diagnostic": _safe_failure_diagnostics(exc, "scenario_C2"),
                }

        else:
            scenario_c2 = {"status": "NOT_MEASURED", "scenario": "C2"}
            scenario_c2_result = None
        scenario_c = {
            "status": _c_scenario_status(scenario_c1, scenario_c2),
            "subscenarios": {"C1": scenario_c1, "C2": scenario_c2},
        }

        result["synthetic_scenarios"] = {"A": dict(result["synthetic_end_to_end"]),
                                         "B": scenario_b, "C": scenario_c,
                                         "C1": scenario_c1, "C2": scenario_c2}
        result["scenario_selection"]["executed"] = (
            list(selected_scenarios) if selected_scenarios else list(DEFAULT_SYNTHETIC_SCENARIOS)
        )
        scenario_statuses = [result["synthetic_scenarios"][key].get("status")
                             for key in (selected_scenarios or ("A", "B", "C"))]
        if any(status == "FAIL" for status in scenario_statuses):
            combined_scenario_status = "FAIL"
        elif any(status == "PARTIAL" for status in scenario_statuses):
            combined_scenario_status = "PARTIAL"
        elif all(status == "PASS" for status in scenario_statuses):
            combined_scenario_status = "PASS"
        else:
            combined_scenario_status = "NOT_MEASURED"
        result["selected_scenario_status"] = combined_scenario_status
        result["synthetic_end_to_end"]["status"] = (
            combined_scenario_status if selected_scenarios is None else "NOT_MEASURED"
        )
        result["provider_requests"]["deepseek_chat"] = deepseek.request_stats()
        result["provider_requests"]["network_boundary"] = metrics["network_request_counts"]
        result["provider_requests"]["network_boundary_events"] = metrics["network_request_events"]
        result["instrumentation"] = _public_metrics(metrics)

        # Inject failures only into the synthetic repository's request path.
        # No service is stopped and the persistent Collection is not changed.
        from agentforge.embeddings.base import EmbeddingError
        from agentforge.index_lifecycle import UnavailableVectorRetriever
        from agentforge.scanner import scan_repository as scan_for_synthetic
        synthetic_scan = scan_for_synthetic(SYNTHETIC_ROOT)
        metrics["phase"] = "synthetic_chroma_failure_injection"
        failed_vector = ProductRetriever(
            synthetic_index, SYNTHETIC_ROOT,
            vector_retriever=UnavailableVectorRetriever(
                "acceptance_injected_chroma_failure", "RuntimeError"),
            scan_report=synthetic_scan,
        ).search(query_text, top_k=5)
        result["synthetic_end_to_end"]["chroma_failure_fallback"] = {
            "status": "PASS" if ("vector" in failed_vector.unavailable_channels
                                  and "lexical" not in failed_vector.unavailable_channels
                                  and bool(failed_vector.candidates)
                                  and all(bool(item.content_hash) for item in failed_vector.candidates)) else "FAIL",
            "unavailable_channels": list(failed_vector.unavailable_channels),
            "candidate_count": len(failed_vector.candidates),
            "source_hashes_present": all(bool(item.content_hash) for item in failed_vector.candidates),
            "service_stopped": False,
        }
        metrics["phase"] = "synthetic_embedding_failure_injection"
        def fail_query_embedding(_text):
            raise EmbeddingError("Synthetic acceptance fault injection.")
        with patch.object(embedding, "embed_query", fail_query_embedding):
            failed_embedding = ProductRetriever(
                synthetic_index, SYNTHETIC_ROOT,
                vector_retriever=synthetic_vector,
                scan_report=synthetic_scan,
            ).search(query_text, top_k=5)
        result["synthetic_end_to_end"]["embedding_failure_fallback"] = {
            "status": "PASS" if ("vector" in failed_embedding.unavailable_channels
                                  and "lexical" not in failed_embedding.unavailable_channels
                                  and bool(failed_embedding.candidates)
                                  and all(bool(item.content_hash) for item in failed_embedding.candidates)) else "FAIL",
            "unavailable_channels": list(failed_embedding.unavailable_channels),
            "candidate_count": len(failed_embedding.candidates),
            "source_hashes_present": all(bool(item.content_hash) for item in failed_embedding.candidates),
            "service_stopped": False,
        }
        metrics["phase"] = "synthetic_chroma_recovery"
        recovered = ProductRetriever(
            synthetic_index, SYNTHETIC_ROOT,
            vector_retriever=synthetic_vector,
            scan_report=synthetic_scan,
        ).search(query_text, top_k=5)
        result["synthetic_end_to_end"]["chroma_recovery"] = {
            "status": "PASS" if ("vector" not in recovered.unavailable_channels
                                  and any("vector" in item.channels for item in recovered.candidates)) else "FAIL",
            "unavailable_channels": list(recovered.unavailable_channels),
            "vector_candidate_count": sum("vector" in item.channels for item in recovered.candidates),
        }
        result["historical_root_cause"] = {
            "request_id": "0ebb32c65865",
            "request_total_ms": 529579,
            "repository_preparation_ms": 510516,
            "document_embedding_requests": 201,
            "document_embedding_items": 201,
            "document_embedding_elapsed_ms": 487943,
            "memory_query_embedding_requests": 1,
            "memory_query_embedding_elapsed_ms": 781,
            "old_repository_query_iterations": 3,
            "old_query_loop_is_present_in_current_turn": False,
            "current_default_query_count": 1,
            "current_additional_refinement": "at most one, only for low-confidence/follow-up symbol resolution",
            "historical_vector_before_after_state": "NOT_MEASURED; old trace lacks per-repo vector snapshot, so first-build vs repair cannot be proven",
            "historical_terminal_failure": "semantic_summary_turn_input_too_large",
        }

    except Exception as exc:
        result["failure"] = _safe_failure_diagnostics(exc, metrics.get("phase"))
        if result["synthetic_end_to_end"].get("status") == "NOT_MEASURED":
            result["synthetic_end_to_end"] = {"status": "FAIL", **result["failure"]}
    finally:
        for patcher in reversed(patchers):
            try:
                patcher.stop()
            except Exception:
                pass
        if synthetic_index is not None:
            try:
                synthetic_index.close()
            except Exception:
                pass
        if conversation_store is not None and conversation_ids and repository_id:
            try:
                for owned_conversation_id in tuple(dict.fromkeys(conversation_ids)):
                    owned = conversation_store.get_conversation(owned_conversation_id, repository_id)
                    if owned.get("id") != owned_conversation_id:
                        raise RuntimeError("Synthetic conversation ownership check failed.")
                    conversation_store.delete_conversation(owned_conversation_id, repository_id)
                remaining = conversation_store.list_conversations(repository_id)
                if remaining:
                    raise RuntimeError("Synthetic conversation remains after exact-ID deletion.")
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_conversation": "removed_and_verified",
                                      "synthetic_conversation_count_removed": len(set(conversation_ids))}
            except Exception as exc:
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_conversation": f"FAILED:{type(exc).__name__}"}
        if client is not None and collection is not None and repository_id:
            try:
                owned = collection.get(where={"repository_id": repository_id}, include=["metadatas"])
                ids, metadata = owned.get("ids") or [], owned.get("metadatas") or []
                if len(ids) != len(metadata) or any(
                        not isinstance(row, dict) or row.get("repository_id") != repository_id
                        or row.get("model_id") != embedding.model_id for row in metadata):
                    raise RuntimeError("Synthetic Chroma ownership check failed.")
                if ids:
                    collection.delete(where={"repository_id": repository_id})
                verify = collection.get(where={"repository_id": repository_id}, include=["metadatas"])
                if verify.get("ids"):
                    raise RuntimeError("Synthetic Chroma vectors remain after exact repo-ID deletion.")
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_chroma_vectors": "removed_and_verified",
                                      "synthetic_chroma_vectors_removed": len(ids)}
            except Exception as exc:
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_chroma_vectors": f"FAILED:{type(exc).__name__}"}
        if repository_id:
            try:
                with RepositoryRegistry() as registry:
                    try:
                        entry = registry.get(repository_id)
                    except ValueError:
                        entry = None
                    if synthetic_registry_owned and entry is None:
                        raise RuntimeError("Owned synthetic repository registration disappeared before cleanup.")
                    if entry is not None:
                        if (not synthetic_registry_owned
                                or Path(entry["canonical_path"]).resolve() != SYNTHETIC_ROOT.resolve()
                                or Path(entry["index_path"]).resolve() != synthetic_index_path.resolve()
                                or registry.active_id() == repository_id):
                            raise RuntimeError("Synthetic repository registration ownership check failed.")
                        registry.remove_registration(repository_id)
                        try:
                            registry.get(repository_id)
                        except ValueError:
                            pass
                        else:
                            raise RuntimeError("Synthetic repository registration remains after exact-ID removal.")
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_repository_registration": "removed_and_verified"}
            except Exception as exc:
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_repository_registration": f"FAILED:{type(exc).__name__}"}
        if synthetic_index_path and synthetic_index_path.exists():
            try:
                if (synthetic_index_path.resolve() != RepositoryRegistry.index_path(repository_id).resolve()
                        or synthetic_index_path.name != f"{repository_id}.sqlite3"
                        or synthetic_index_path.is_symlink()):
                    raise RuntimeError("Synthetic index path ownership check failed.")
                synthetic_index_path.unlink()
                for suffix in ("-journal", "-wal", "-shm"):
                    sidecar = synthetic_index_path.with_name(synthetic_index_path.name + suffix)
                    if sidecar.exists():
                        if sidecar.is_symlink() or not sidecar.is_file():
                            raise RuntimeError("Synthetic index sidecar ownership check failed.")
                        sidecar.unlink()
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_sqlite_index": "removed_and_verified"}
            except Exception as exc:
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_sqlite_index": f"FAILED:{type(exc).__name__}"}
        if synthetic_directory_created:
            try:
                owner = json.loads(OWNER_MARKER.read_text(encoding="utf-8"))
                if (owner.get("run_id") != RUN_ID or SYNTHETIC_ROOT.parent != SYNTHETIC_PARENT
                        or SYNTHETIC_PARENT.parent.resolve() != (ROOT / ".agentforge" / "data").resolve()
                        or SYNTHETIC_PARENT.is_symlink()):
                    raise RuntimeError("Synthetic repository ownership check failed.")
                shutil.rmtree(SYNTHETIC_PARENT)
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_repository_directory": "removed_and_verified"}
            except Exception as exc:
                result["cleanup"] = {**result.get("cleanup", {}),
                                      "synthetic_repository_directory": f"FAILED:{type(exc).__name__}",
                                      "synthetic_repository_path": str(SYNTHETIC_PARENT)}
        if engine is not None and formal_postgres_snapshot_before is not None:
            try:
                formal_postgres_snapshot_after = _postgres_primary_key_snapshot(engine)
                postgres_unchanged = formal_postgres_snapshot_before == formal_postgres_snapshot_after
                result["data_protection"]["formal_postgresql_after"] = formal_postgres_snapshot_after
                result["data_protection"]["formal_postgresql_unchanged"] = postgres_unchanged
            except Exception as exc:
                result["data_protection"]["formal_postgresql_after_status"] = (
                    f"FAILED:{type(exc).__name__}"
                )
        if formal_registry_snapshot_before is not None:
            try:
                formal_registry_snapshot_after = _repository_registry_snapshot()
                registry_unchanged = formal_registry_snapshot_before == formal_registry_snapshot_after
                result["data_protection"]["formal_repository_registry_after"] = (
                    formal_registry_snapshot_after
                )
                result["data_protection"]["formal_repository_registry_unchanged"] = registry_unchanged
            except Exception as exc:
                result["data_protection"]["formal_repository_registry_after_status"] = (
                    f"FAILED:{type(exc).__name__}"
                )
        if active_root is not None and active_index_before is not None:
            try:
                with open_index_for_repository(active_root, readonly=True) as active_store_after:
                    active_index_after = _only_counts(active_store_after.file_snapshot())
                sqlite_unchanged = active_index_before == active_index_after
                result["data_protection"]["active_sqlite_after"] = active_index_after
                result["data_protection"]["active_sqlite_unchanged"] = sqlite_unchanged
            except Exception as exc:
                result["data_protection"]["active_sqlite_after_status"] = f"FAILED:{type(exc).__name__}"
        protection_checks = [
            result.get("data_protection", {}).get("formal_postgresql_unchanged"),
            result.get("data_protection", {}).get("formal_repository_registry_unchanged"),
            result.get("data_protection", {}).get("active_sqlite_unchanged"),
        ]
        result["data_protection"]["status"] = (
            "PASS" if protection_checks and all(value is True for value in protection_checks)
            else "FAIL" if any(value is False for value in protection_checks)
            or any(str(key).endswith("_after_status") for key in result["data_protection"])
            else "NOT_MEASURED"
        )
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                pass
        if client is not None and pre_snapshot is not None:
            try:
                post_snapshot = persistent_collection_snapshot(client)
                after_official = _collection_summary(
                    post_snapshot, collection.name, active_repository["repository_id"])
                result["chroma_integrity"] = {
                    "status": "PASS" if pre_snapshot == post_snapshot else "FAIL",
                    "persistent_collection_snapshot_equal_before_after_cleanup": pre_snapshot == post_snapshot,
                    "official_repository_before": official_before,
                    "official_repository_after": after_official,
                    "all_collection_data_digest_before": _digest(pre_snapshot),
                    "all_collection_data_digest_after": _digest(post_snapshot),
                    "synthetic_repository_id_absent_after_cleanup": (
                        repository_id is None or all(repository_id not in row["repository_ids"]
                                                     for row in post_snapshot["collections"])),
                }
            except Exception as exc:
                result["chroma_integrity"] = {
                    "status": "FAIL", "error_type": type(exc).__name__,
                }
        if RAW_TRACE.exists():
            trace_lines = RAW_TRACE.read_text(encoding="utf-8", errors="replace").splitlines()
        cleanup_values = result.get("cleanup", {})
        cleanup_checks = [value for key, value in cleanup_values.items()
                          if key.startswith("synthetic_") and isinstance(value, str)
                          and (value.endswith("verified") or value.startswith("FAILED:"))]
        result["cleanup"]["status"] = (
            "PASS" if cleanup_checks and all(value in {"removed_and_verified"} for value in cleanup_checks)
            else "FAIL" if any(isinstance(value, str) and value.startswith("FAILED:") for value in cleanup_checks)
            else "PARTIAL")
        if selected_scenarios:
            selected_scenario_checks = all(
                result.get("synthetic_scenarios", {}).get(key, {}).get("status") == "PASS"
                for key in selected_scenarios
            )
            all_core = (
                result.get("host_preflight", {}).get("status") == "HOST_CONFIRMED_PASS"
                and selected_scenario_checks
                and result.get("chroma_integrity", {}).get("status") == "PASS"
                and result.get("data_protection", {}).get("status") == "PASS"
                and result.get("cleanup", {}).get("status") == "PASS"
            )
        else:
            all_core = (
                result.get("host_preflight", {}).get("status") == "HOST_CONFIRMED_PASS"
                and result.get("current_repository_performance", {}).get("status") == "PASS"
                and result.get("current_repository_retrieval", {}).get("status") == "PASS"
                and result.get("synthetic_end_to_end", {}).get("status") == "PASS"
                and result.get("synthetic_end_to_end", {}).get("chroma_failure_fallback", {}).get("status") == "PASS"
                and result.get("synthetic_end_to_end", {}).get("embedding_failure_fallback", {}).get("status") == "PASS"
                and result.get("synthetic_end_to_end", {}).get("chroma_recovery", {}).get("status") == "PASS"
                and result.get("chroma_integrity", {}).get("status") == "PASS"
                and result.get("cleanup", {}).get("status") == "PASS"
            )
        result["status"] = "PASS" if all_core else "PARTIAL" if result.get("host_preflight", {}).get("status") == "HOST_CONFIRMED_PASS" else "BLOCKED"
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["instrumentation"] = _public_metrics(metrics)
        _write_result(result, trace_lines)
    return 0 if result["status"] == "PASS" else 1


def _request_for(repository, query, *, cloud):
    from types import SimpleNamespace
    return SimpleNamespace(repository=str(repository), query=query, cloud_data_authorized=cloud)


def _embedding_wrapper(original, metrics):
    def wrapped(self, values):
        item_count = len(values) if isinstance(values, list) else 1
        kind = "documents" if isinstance(values, list) else "query"
        started = time.perf_counter()
        try:
            return original(self, values)
        finally:
            metrics["embedding_requests"].append({
                "phase": metrics["phase"],
                "kind": kind,
                "item_count": item_count,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            })
    return wrapped


def _product_search_wrapper(original, metrics):
    def wrapped(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            outcome = original(self, *args, **kwargs)
            event = {
                "phase": metrics["phase"],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "candidate_count": len(outcome.candidates),
                "unavailable_channels": list(outcome.unavailable_channels),
                "channels": sorted({channel for candidate in outcome.candidates
                                    for channel in candidate.channels}),
            }
            if metrics["phase"].startswith("scenario_"):
                event["candidate_summaries"] = [{
                    "citation": candidate.citation,
                    "relative_path": candidate.relative_path,
                    "content_hash": candidate.content_hash,
                    "excerpt_sha256": hashlib.sha256(candidate.text.encode("utf-8")).hexdigest(),
                    "channels": list(candidate.channels),
                    "start_line": candidate.start_line,
                    "end_line": candidate.end_line,
                } for candidate in outcome.candidates]
            metrics["product_retriever_calls"].append(event)
            return outcome
        except Exception as exc:
            metrics["product_retriever_calls"].append({
                "phase": metrics["phase"],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error_type": type(exc).__name__,
            })
            raise
    return wrapped


def _agent_tool_search_wrapper(original, metrics):
    """Separate an explicitly planned Agent search from the mandatory turn lookup."""
    def wrapped(self, *args, **kwargs):
        started = time.perf_counter()
        try:
            value = original(self, *args, **kwargs)
        except Exception as exc:
            metrics.setdefault("agent_tool_search_calls", []).append({
                "phase": metrics.get("phase", "unknown"),
                "status": "FAILED", "error_type": type(exc).__name__,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            })
            raise
        metrics.setdefault("agent_tool_search_calls", []).append({
            "phase": metrics.get("phase", "unknown"),
            "status": "COMPLETED",
            "candidate_count": len(value.get("results", ())) if isinstance(value, dict) else None,
            "source_ids": [item.get("source_id") for item in value.get("results", ())
                           if isinstance(item, dict) and isinstance(item.get("source_id"), str)]
            if isinstance(value, dict) and isinstance(value.get("results"), list) else [],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        })
        return value
    return wrapped


def _write_result(result, trace_lines):
    result["raw_result_file"] = RAW_RESULT.name
    RAW_RESULT.write_text(json.dumps(_jsonable(result), ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    result_sha256 = hashlib.sha256(RAW_RESULT.read_bytes()).hexdigest()
    print(json.dumps({
        "status": result.get("status"), "raw_result_file": RAW_RESULT.name,
        "raw_result_sha256": result_sha256,
        "raw_trace_file": RAW_TRACE.name if RAW_TRACE.exists() else None,
        "failure": result.get("failure"),
        "deepseek_chat": result.get("provider_requests", {}).get("deepseek_chat"),
        "current_repository_performance": result.get("current_repository_performance", {}).get("status"),
        "current_repository_retrieval": result.get("current_repository_retrieval", {}).get("status"),
        "synthetic_end_to_end": result.get("synthetic_end_to_end", {}).get("status"),
        "chroma_integrity": result.get("chroma_integrity", {}).get("status"),
        "cleanup": result.get("cleanup", {}).get("status"),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    _ensure_database_password()
    raise SystemExit(run())
