"""Bounded real-Qwen AgentExecutionEngine acceptance using synthetic tools only.

This runner never starts/stops services, never opens a real repository, and only
connects to a UUID-named disposable PostgreSQL database plus a per-run Chroma
directory. It is safe to run as a host acceptance entry point; a local offline
environment is rejected before credential access or resource creation.
"""
from __future__ import annotations

from contextlib import suppress
import getpass
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import threading
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
TEST_TEMP_ROOT = ROOT / "tests" / ".tmp"
CHAT_ENDPOINT = "http://127.0.0.1:56572/v1/chat/completions"
CHAT_HOST = "127.0.0.1"
CHAT_PORT = 56572
CHAT_MODEL = "qwen3.5-4b"
CHAT_KEY_ENV = "AGENTFORGE_LLAMA_API_KEY"
DB_KEY_ENV = "AGENTFORGE_DB_PASSWORD"
MAX_CHAT_REQUESTS = 64
TEST_DATABASE_RE = re.compile(r"af033_agent_[0-9a-f]{20}\Z")
TEMP_ROOT_RE = re.compile(r"af033-agent-host-[0-9a-f]{32}\Z")
SCENARIO_NAMES = (
    "single_tool", "multi_tool_dependency", "observation_replanning",
    "failure_replanning", "approval_resume", "large_tool_result",
    "timeout_recovery", "cancel_partial_result",
)


class HostAcceptanceBlocked(RuntimeError):
    def __init__(self, stage: str, code: str):
        super().__init__(code)
        self.stage, self.code = stage, code


def _is_link_or_junction(path: Path) -> bool:
    junction_check = getattr(path, "is_junction", None)
    return path.is_symlink() or (callable(junction_check) and junction_check())


def _validate_temp_scope(database: str, temp_root: Path) -> tuple[str, Path]:
    if not isinstance(database, str) or not TEST_DATABASE_RE.fullmatch(database):
        raise HostAcceptanceBlocked("isolation_preflight", "disposable_database_name_required")
    if not isinstance(temp_root, Path):
        raise HostAcceptanceBlocked("isolation_preflight", "owned_temp_directory_required")
    try:
        resolved_test_root = TEST_TEMP_ROOT.resolve(strict=True)
        if (_is_link_or_junction(TEST_TEMP_ROOT) or _is_link_or_junction(temp_root)
                or not temp_root.is_dir()):
            raise ValueError
        resolved_temp = temp_root.resolve(strict=True)
        if (resolved_temp.parent != resolved_test_root
                or not TEMP_ROOT_RE.fullmatch(resolved_temp.name)):
            raise ValueError
    except (OSError, RuntimeError, ValueError):
        raise HostAcceptanceBlocked("isolation_preflight", "owned_temp_directory_required") from None
    return database, resolved_temp


def _safe_listener_probe(host: str = CHAT_HOST, port: int = CHAT_PORT, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _verify_temporary_chroma_reopen(create_client, path, collection_name: str) -> dict:
    """Create, close, and reopen an isolated Chroma collection without leaking handles."""
    client = None
    collection = None
    try:
        client = create_client(path=path)
        collection = client.get_or_create_collection(
            name=collection_name, metadata={"purpose": "synthetic-agent-host"})
        created_name = collection.name
    finally:
        collection = None
        if client is not None:
            client.close()
            client = None

    reopened_client = None
    reopened_collection = None
    try:
        reopened_client = create_client(path=path)
        reopened_collection = reopened_client.get_collection(collection_name)
        reopened_name = reopened_collection.name
    finally:
        reopened_collection = None
        if reopened_client is not None:
            reopened_client.close()
            reopened_client = None
    return {"collection_name": created_name, "reopened": reopened_name == collection_name}


def _health_ready(timeout_seconds: float = 3.0) -> dict:
    started = time.monotonic()
    connection = http.client.HTTPConnection(CHAT_HOST, CHAT_PORT, timeout=timeout_seconds)
    try:
        connection.request("GET", "/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise HostAcceptanceBlocked("qwen_preflight", "health_response_too_large")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HostAcceptanceBlocked("qwen_preflight", "health_response_invalid") from None
        if response.status != 200 or not isinstance(payload, dict) or payload.get("status") != "ok":
            raise HostAcceptanceBlocked("qwen_preflight", "qwen_health_contract_not_ready")
        return {"status": "ready", "http_status": response.status,
                "elapsed_seconds": round(time.monotonic() - started, 4)}
    except HostAcceptanceBlocked:
        raise
    except (OSError, http.client.HTTPException):
        raise HostAcceptanceBlocked("qwen_preflight", "qwen_health_transport_unavailable") from None
    finally:
        connection.close()


def _safe_request_counts(stats: dict | None) -> dict:
    stats = stats if isinstance(stats, dict) else {}
    return {key: int(stats.get(key, 0) or 0) for key in
            ("attempted", "sent", "succeeded", "failed", "retries")}


def _safe_budget(record: dict) -> dict:
    allowed = ("phase", "context_window", "max_input_tokens", "max_output_tokens",
               "input_budget", "estimated_input_tokens", "estimate_kind",
               "request_sent", "request_succeeded", "usage", "finish_reason")
    return {key: record[key] for key in allowed if key in record}


def _safe_output(value, *, max_bytes: int = 2048) -> dict:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return {"output_type": type(value).__name__, "serialization": "rejected"}
    summary = {"output_type": type(value).__name__, "utf8_bytes": len(raw),
               "sha256": hashlib.sha256(raw).hexdigest()}
    if len(raw) <= max_bytes:
        summary["value"] = value
    else:
        summary["truncated"] = True
        summary["preview"] = raw[:max_bytes].decode("utf-8", errors="ignore")
    return summary


class RecordingRealPlanner:
    """Observes decisions while delegating every decision to real Qwen planner."""
    def __init__(self, delegate, *, scenario: str):
        from agentforge.agent_execution import LLMExecutionPlanner
        if not isinstance(delegate, LLMExecutionPlanner):
            raise TypeError("A real LLMExecutionPlanner is required.")
        self.delegate = delegate
        self.scenario = scenario
        self.decisions: list[dict] = []

    @property
    def request_diagnostics(self):
        return self.delegate.request_diagnostics

    def plan(self, objective, observations, catalog, schema_loader):
        obs = []
        for item in observations:
            obs.append({"tool_id": item.tool_id, "status": item.status,
                        "error_code": item.error_code,
                        "output": _safe_output(item.output) if item.output is not None else None})
        try:
            plan = self.delegate.plan(objective, observations, catalog, schema_loader)
        except Exception as exc:
            diagnostic = getattr(self.delegate, "last_request_diagnostics", {})
            if not isinstance(diagnostic, dict):
                diagnostic = {}
            code = getattr(getattr(exc, "code", None), "value", None)
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code):
                code = "PLANNER_ERROR"
            failure = {"code": code}
            for target, source in (("phase", "planning_failure_phase"),
                                   ("reason", "planning_failure_reason")):
                value = diagnostic.get(source)
                if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
                    failure[target] = value
            details = diagnostic.get("planning_failure_details")
            if isinstance(details, dict):
                failure["details"] = details
            self.decisions.append({
                "planner_round": len(self.decisions) + 1,
                "observation_count": len(observations),
                "observations": obs,
                "catalog_tool_ids": [item.tool_id for item in catalog],
                "selected_calls": [],
                "done": False,
                "failure": failure,
            })
            raise
        self.decisions.append({
            "planner_round": len(self.decisions) + 1,
            "observation_count": len(observations),
            "observations": obs,
            "catalog_tool_ids": [item.tool_id for item in catalog],
            "selected_calls": [{"call_id": call.call_id, "task_id": call.task_id,
                                "tool_id": call.tool_id, "arguments": call.arguments,
                                "dependencies": list(call.dependencies)} for call in plan.calls],
            "done": plan.done,
            "final_answer": plan.final_answer[:1500] if isinstance(plan.final_answer, str) else None,
            "user_input_request": plan.user_input_request,
        })
        return plan


class CappedLocalProvider:
    """No-retry Qwen facade enforcing a total request ceiling for this run."""
    def __init__(self, provider, *, limit: int = MAX_CHAT_REQUESTS):
        self._provider = provider
        self._limit = limit
        self._requests = 0
        self.descriptor = provider.descriptor
        self.context_assembler = provider.context_assembler
        self.max_output_tokens = provider.client.max_output_tokens
        self.model = provider.model
        self.provider_id = provider.provider_id

    @property
    def requests_attempted(self) -> int:
        return self._requests

    def plan_completion(self, messages, *, max_output_tokens, timeout_seconds):
        if self._requests >= self._limit:
            raise HostAcceptanceBlocked("request_budget", "real_chat_request_limit_reached")
        self._requests += 1
        return self._provider.plan_completion(
            messages, max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds,
        )

    def request_stats(self):
        return self._provider.request_stats()


def _object_schema(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


def _register_tool(registry, tool_id: str, description: str, properties: dict,
                   handler, *, required=(), side_effect=False, approval=False,
                   timeout=5.0, parallel_safe=False):
    from agentforge.agent_execution import ToolDefinition
    scope = "synthetic:action" if side_effect else "synthetic:read"
    definition = ToolDefinition(
        tool_id=tool_id, name=tool_id.replace("_", " ").title(),
        description=description, version="1", capabilities=frozenset({"synthetic", "acceptance"}),
        input_schema=_object_schema(properties, tuple(required)),
        output_schema={"type": "object"}, permission_scope=scope,
        risk_level="high" if approval else ("medium" if side_effect else "low"),
        side_effect=side_effect, idempotent=not side_effect, timeout=timeout,
        concurrency_group="synthetic-phase33", requires_approval=approval,
        max_retries=0, parallel_safe=parallel_safe, worker_safe=False,
    )
    registry.register(definition, handler)


def _base_registry():
    from agentforge.agent_execution import ToolRegistry
    return ToolRegistry()


def _planner(provider, scenario: str, *, history=(), summary=None, repository_id):
    from agentforge.agent_execution import LLMExecutionPlanner
    delegate = LLMExecutionPlanner(
        provider, max_output_tokens=min(LLMExecutionPlanner.MAX_OUTPUT_TOKENS, provider.max_output_tokens),
        timeout_seconds=30.0, history=history, summary=summary,
        repository_id=repository_id,
    )
    return RecordingRealPlanner(delegate, scenario=scenario)


def _streaming(event_log: list[dict], *, cancel_event=None):
    from agentforge.streaming import StreamingManager

    def collect(event):
        event_log.append({"type": event.type.value, "sequence": event.sequence,
                          "execution_id": event.execution_id, "task_id": event.task_id,
                          "payload": event.payload})

    manager = StreamingManager(on_event=collect, enabled=True, cancel_event=cancel_event)
    manager.start()
    return manager


def _make_engine(registry, planner, state_store, result_store, repo_id, conversation_id,
                 event_sink, *, cancel_event=None, approval_handler=None,
                 max_tool_calls=8, run_timeout=90.0):
    from agentforge.agent_execution import AgentExecutionEngine, ToolAuthorization
    scopes = frozenset({"synthetic:read", "synthetic:action"})
    authorization = ToolAuthorization(permission_scopes=scopes, approval_handler=approval_handler)
    return AgentExecutionEngine(
        registry, planner, authorization=authorization,
        state_store=state_store, result_store=result_store,
        max_rounds=8, max_tool_calls=max_tool_calls, max_tasks_per_plan=4,
        max_concurrency=2, run_timeout_seconds=run_timeout,
        on_event=event_sink, cancel_event=cancel_event,
    )


def _build_report() -> dict:
    run_id = uuid4().hex
    return {
        "schema": "agentforge-phase3-3-real-agent-host-v1",
        "run_id": run_id,
        "status": "PRECHECK",
        "failure_stage": None,
        "failure_code": None,
        "services_started_or_stopped": False,
        "cloud_requests": 0,
        "model": {"provider": "llama-server", "model_id": CHAT_MODEL,
                  "endpoint": CHAT_ENDPOINT, "streaming_tokens_supported": False},
        "architecture": {
            "call_chain": ["LLMExecutionPlanner", "ToolRegistry discovery and schema validation",
                           "AgentExecutionEngine", "synthetic tool handler", "Observation",
                           "LLMExecutionPlanner replanning", "Answer Contract",
                           "StreamingManager", "SessionService / PostgreSQL"],
            "roles": {
                "LLMExecutionPlanner": "real Qwen planning; decisions are recorded, not replaced",
                "ToolRegistry": "only per-scenario in-memory synthetic tools",
                "AgentExecutionEngine": "plan validation, authorization, scheduling, checkpoint and observation lifecycle",
                "StreamingManager": "ordered execution lifecycle events; Qwen planner itself is non-streaming",
                "SessionService": "restore conversation context and persist validated completed turns",
            },
        },
        "not_measured": list(SCENARIO_NAMES),
        "chat_requests": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0},
        "checks": {}, "scenarios": {},
        "database_provision": {"status": "not_started", "operation": "not_started",
                               "database_absence_confirmed": False},
        "temporary_resources": {"database_created": False, "chroma_created": False,
                                 "synthetic_repository_created": False},
        "cleanup": {"database": "not_created", "chroma_directory": "not_created",
                    "synthetic_repository": "not_created"},
    }


def _write_report(result: dict) -> tuple[Path, Path]:
    run_id = result["run_id"]
    json_path = ROOT / f"AF033_AGENT_LOOP_HOST_RESULT_{run_id}.json"
    md_path = ROOT / f"AF033_AGENT_LOOP_HOST_REPORT_{run_id}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Phase 3.3 Real Agent Execution Host Acceptance", "",
             f"- Run: `{run_id}`", f"- Status: **{result['status']}**",
             f"- Failure: `{result.get('failure_stage') or 'none'}` / `{result.get('failure_code') or 'none'}`",
             f"- Qwen requests: `{result['chat_requests']}`", "",
             "## Checks", ""]
    for name, status in result.get("checks", {}).items():
        lines.append(f"- {name}: **{status}**")
    lines.extend(["", "## Scenario status", ""])
    for name, scenario in result.get("scenarios", {}).items():
        lines.append(f"- {name}: **{scenario.get('status', 'NOT_MEASURED')}**")
        for assertion, passed in scenario.get("assertions", {}).items():
            lines.append(f"  - {assertion}: `{bool(passed)}`")
        for decision in scenario.get("planner_decisions", []):
            selected = ", ".join(call.get("tool_id", "?") for call in decision.get("selected_calls", [])) or "finish"
            lines.append(f"  - Planner round {decision.get('planner_round')}: observations="
                         f"{decision.get('observation_count')}, selected={selected}")
    lines.extend(["", "## Execution architecture", ""])
    lines.extend(f"- {item}" for item in result.get("architecture", {}).get("call_chain", []))
    provision = result.get("database_provision", {})
    lines.extend(["", "## Database provisioning", "",
                  f"- Status: `{provision.get('status', 'NOT_MEASURED')}`",
                  f"- Last operation: `{provision.get('operation', 'NOT_MEASURED')}`"])
    if isinstance(provision.get("diagnostic"), dict):
        diagnostic = provision["diagnostic"]
        lines.append(f"- Error: `{diagnostic.get('exception_type')}` / SQLSTATE `{diagnostic.get('sqlstate') or 'unknown'}`")
    if result.get("not_measured"):
        lines.extend(["", "## Not measured", "", *[f"- {name}" for name in result["not_measured"]]])
    lines.extend(["", "## Cleanup", ""])
    for name, status in result.get("cleanup", {}).items():
        lines.append(f"- {name}: `{status}`")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _blocked(result: dict, stage: str, code: str) -> int:
    result.update(status="BLOCKED", failure_stage=stage, failure_code=code)
    result["chat_requests"] = {"attempted": 0, "sent": 0, "succeeded": 0,
                                "failed": 0, "retries": 0}
    result["checks"].setdefault("host_preflight", "blocked")
    json_path, md_path = _write_report(result)
    print(json.dumps({"status": result["status"], "failure_stage": stage,
                      "failure_code": code, "result_file": str(json_path),
                      "report_file": str(md_path), "chat_requests": result["chat_requests"],
                      "checks": result["checks"]}, ensure_ascii=False, indent=2))
    return 2


def _create_test_database(run_id: str, admin_password: str, *, on_stage=None,
                          on_database_absent=None, on_created=None) -> str:
    import psycopg
    from psycopg import sql
    database = "af033_agent_" + run_id[:20]
    if not TEST_DATABASE_RE.fullmatch(database):
        raise HostAcceptanceBlocked("database_provision", "disposable_database_name_invalid")
    if on_stage:
        on_stage("admin_connection")
    connection = psycopg.connect(host="127.0.0.1", port=5432, dbname="postgres", user="postgres",
                                 password=admin_password, connect_timeout=3, autocommit=True)
    try:
        if on_stage:
            on_stage("database_existence_check")
        exists = connection.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
        if exists:
            raise HostAcceptanceBlocked("database_provision", "generated_database_already_exists")
        if on_database_absent:
            on_database_absent(database)
        if on_stage:
            on_stage("create_database")
        connection.execute(sql.SQL("CREATE DATABASE {} OWNER agentforge_app").format(sql.Identifier(database)))
        # Mark ownership immediately after PostgreSQL confirms CREATE DATABASE.
        # A subsequent connection-close exception must still trigger cleanup.
        if on_created:
            on_created(database)
    except BaseException as primary:
        try:
            connection.close()
        except Exception as close_error:
            if hasattr(primary, "add_note"):
                primary.add_note(f"admin connection close also failed ({type(close_error).__name__})")
        raise
    if on_stage:
        on_stage("admin_connection_close")
    connection.close()
    return database


def _drop_test_database(database: str, admin_password: str) -> None:
    import psycopg
    from psycopg import sql
    if not isinstance(database, str) or not TEST_DATABASE_RE.fullmatch(database):
        raise RuntimeError("refusing_to_drop_unowned_database")
    with psycopg.connect(host="127.0.0.1", port=5432, dbname="postgres", user="postgres",
                         password=admin_password, connect_timeout=3, autocommit=True) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (database,),
        )
        connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def _database_sqlstate(exc) -> str | None:
    current = exc
    seen = set()
    while id(current) not in seen:
        seen.add(id(current))
        original = getattr(current, "orig", None)
        if original is None:
            break
        current = original
    diag = getattr(current, "diag", None)
    value = (getattr(current, "sqlstate", None) or getattr(current, "pgcode", None)
             or getattr(diag, "sqlstate", None))
    return value if isinstance(value, str) and re.fullmatch(r"[0-9A-Z]{5}", value) else None


def _database_create_outcome_ambiguous(sqlstate: str | None) -> bool:
    """Connection loss/server shutdown can hide a committed CREATE result."""
    return (sqlstate is None or sqlstate.startswith("08")
            or sqlstate in {"57P01", "57P02", "57P03"})


def _record_database_provision_failure(result: dict, exc) -> None:
    provision = result["database_provision"]
    provision["diagnostic"] = {"operation": provision.get("operation"),
                                "exception_type": type(exc).__name__,
                                "sqlstate": _database_sqlstate(exc)}
    if result["temporary_resources"]["database_created"]:
        provision["status"] = "created_cleanup_required"
        result["cleanup"]["database"] = "pending"
    elif (provision.get("operation") == "create_database"
          and provision["database_absence_confirmed"]
          and _database_create_outcome_ambiguous(provision["diagnostic"]["sqlstate"])):
        provision["status"] = "creation_outcome_unknown"
        result["cleanup"]["database"] = "unverified"
    else:
        provision["status"] = "not_created"
        result["cleanup"]["database"] = "not_created"


def _create_test_engine(database: str, password: str):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import URL
    if not TEST_DATABASE_RE.fullmatch(database):
        raise HostAcceptanceBlocked("database_preflight", "disposable_database_name_invalid")
    url = URL.create("postgresql+psycopg", username="agentforge_app", password=password,
                     host="127.0.0.1", port=5432, database=database)
    return create_engine(url, connect_args={"connect_timeout": 3}, pool_size=2,
                         max_overflow=0, pool_timeout=5, pool_pre_ping=True,
                         hide_parameters=True)


def _database_identity(engine, expected_database: str) -> dict:
    from sqlalchemy import text
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT current_user AS role, current_database() AS database, "
            "inet_server_addr()::text AS address, inet_server_port() AS port"
        )).mappings().one()
    identity = {"role": row["role"], "database": row["database"],
                "address": row["address"], "port": row["port"]}
    if (identity["role"] != "agentforge_app" or identity["database"] != expected_database
            or identity["address"] not in {"127.0.0.1", "127.0.0.1/32"}
            or identity["port"] != 5432):
        raise HostAcceptanceBlocked("database_preflight", "database_identity_or_scope_mismatch")
    return identity


def _register_synthetic_repository(engine, repo_path: Path, run_id: str) -> str:
    from agentforge.memory_store import MemoryStore
    from agentforge.repository_identity import repository_identity
    repo_id = repository_identity(repo_path)
    MemoryStore(engine).register_repository(repo_id, str(repo_path.resolve()),
                                           "Phase 3.3 synthetic Agent tools")
    return repo_id


def _new_conversation(conversation_store, repo_id: str, title: str) -> str:
    return conversation_store.create_conversation(repo_id, title)["id"]


def _planner_from_session(provider, conversation_store, repo_id, conversation_id, label):
    from agentforge.session_service import SessionService
    restored = SessionService().load_context(conversation_store, conversation_id, repo_id)
    return _planner(provider, label, history=restored.history, summary=restored.summary,
                    repository_id=repo_id), restored


def _finish_streaming(manager, report, provider, event_log):
    from agentforge.answer_contract import build_general_contract, validate_answer_contract
    from agentforge.models import Answer, BuiltContext
    if report.status == "completed" and isinstance(report.final_answer, str):
        contract = build_general_contract(Answer(report.final_answer, (), False))
        validate_answer_contract(contract, BuiltContext("general", (), "", 0, 1))
        usage = provider.request_stats().get("last_usage")
        manager.complete_validated(contract.content, usage=usage)
        return {"status": "validated_limited_general_contract", "answer_chars": len(contract.content)}
    if report.status == "waiting_approval":
        manager.suspend("waiting_approval")
        return {"status": "waiting_approval"}
    if report.status == "waiting_user_input":
        manager.suspend("waiting_user_input")
        return {"status": "waiting_user_input"}
    if report.status == "cancelled":
        manager.cancel()
        return {"status": "cancelled"}
    manager.fail(report.error_code or report.status)
    return {"status": "failed", "error_code": report.error_code}


def _scenario_shell(name: str) -> dict:
    return {"status": "NOT_MEASURED", "planner_decisions": [], "tool_results": [],
            "streaming_events": [], "assertions": {}, "request_diagnostics": [],
            "session_restore": "NOT_MEASURED", "scenario": name}


def _record_execution(scenario: dict, report, planner: RecordingRealPlanner,
                      event_log: list[dict], stream_result: dict) -> None:
    scenario["planner_decisions"].extend(planner.decisions)
    scenario["request_diagnostics"].extend(
        _safe_budget(item) for item in planner.request_diagnostics if isinstance(item, dict))
    scenario["tool_results"] = [{"call_id": item.call_id, "task_id": item.task_id,
        "tool_id": item.tool_id, "status": item.status, "error_code": item.error_code,
        "output": _safe_output(item.output) if item.output is not None else None,
        "execution_metadata": item.execution_metadata, "provenance": item.provenance}
        for item in report.results]
    scenario["streaming_events"] = event_log
    scenario["stream_result"] = stream_result
    scenario["execution"] = {"execution_id": report.execution_id, "status": report.status,
        "state": report.state, "rounds": report.rounds, "tool_calls": report.tool_calls,
        "error_code": report.error_code}


def _persist_completed_turn(session_service, conversation_store, conversation_id, repo_id,
                            report, objective, provider, planner):
    from agentforge.models import Answer
    from agentforge.answer_contract import build_general_contract, validate_answer_contract
    from agentforge.models import BuiltContext
    if report.status != "completed" or not report.final_answer:
        return {"persisted": False, "reason": "execution_not_completed"}
    answer = Answer(report.final_answer, (), False)
    contract = build_general_contract(answer)
    validate_answer_contract(contract, BuiltContext("general", (), "", 0, 1))
    session_service.commit_execution_turn(
        conversation_store, conversation_id, repo_id, report.execution_id,
        objective, Answer(contract.content, (), False), provider,
        request_diagnostics={f"planner_round_{i}": item
                             for i, item in enumerate(planner.request_diagnostics, 1)},
    )
    restored = session_service.load_context(conversation_store, conversation_id, repo_id)
    messages = conversation_store.get_messages(conversation_id, repo_id)
    ok = (len(messages) >= 2 and messages[-2]["role"] == "user"
          and messages[-1]["role"] == "assistant"
          and messages[-1]["metadata"].get("agent_execution_id") == report.execution_id
          and restored.history and restored.history[-1].content == contract.content)
    return {"persisted": bool(ok), "message_count": len(messages),
            "roles": [row["role"] for row in messages[-2:]],
            "execution_id_restored": messages[-1]["metadata"].get("agent_execution_id") if messages else None,
            "assistant_sha256": hashlib.sha256(contract.content.encode("utf-8")).hexdigest()}


def _run_single_tool(provider, state_store, result_store, conversation_store,
                     session_service, repo_id, scenario_name="single_tool") -> dict:
    from agentforge.agent_execution import ToolAuthorization
    scenario = _scenario_shell(scenario_name)
    registry = _base_registry()
    _register_tool(registry, "read_counter", "Read the synthetic acceptance counter; no filesystem or external access.",
                   {}, lambda _args, _ctx: {"counter": 17, "source": "synthetic_fixture"})
    conversation_id = _new_conversation(conversation_store, repo_id, scenario_name)
    planner, _ = _planner_from_session(provider, conversation_store, repo_id, conversation_id, scenario_name)
    events = []
    manager = _streaming(events)
    engine = _make_engine(registry, planner, state_store, result_store, repo_id, conversation_id, manager)
    objective = ("Use the read_counter synthetic tool exactly once to obtain the fixture value. "
                 "Then finish with a short answer that reports the value and says it came from the synthetic tool.")
    report = engine.run(objective, repository_id=repo_id, conversation_id=conversation_id)
    stream_result = _finish_streaming(manager, report, provider, events)
    _record_execution(scenario, report, planner, events, stream_result)
    used = any(item.tool_id == "read_counter" and item.status == "completed" for item in report.results)
    scenario["assertions"] = {"completed": report.status == "completed", "read_counter_called": used,
                              "answer_mentions_fixture_value": bool(report.final_answer and "17" in report.final_answer),
                              "real_planner_requests": planner.delegate.request_count > 0,
                              "contract_validated_before_stream_completion": stream_result.get("status") == "validated_limited_general_contract"}
    if all(scenario["assertions"].values()):
        scenario["session_restore"] = _persist_completed_turn(session_service, conversation_store,
            conversation_id, repo_id, report, objective, provider, planner)
        scenario["assertions"]["session_persisted_and_restored"] = scenario["session_restore"]["persisted"]
    scenario["status"] = "PASS" if all(scenario["assertions"].values()) else "FAIL"
    return scenario


def _run_dependency_scenario(provider, state_store, result_store, conversation_store,
                             repo_id, scenario_name="multi_tool_dependency") -> dict:
    scenario = _scenario_shell(scenario_name)
    registry = _base_registry()
    state = {"stage_open": False, "order": []}
    def open_stage(_args, _ctx):
        state["stage_open"] = True
        state["order"].append("open")
        return {"ready": True}
    def inspect_stage(_args, _ctx):
        if not state["stage_open"]:
            raise RuntimeError("synthetic_dependency_not_satisfied")
        state["order"].append("inspect")
        return {"inspected": True}
    _register_tool(registry, "open_stage", "Open the synthetic test stage; required prerequisite for inspect_stage.", {}, open_stage)
    _register_tool(registry, "inspect_stage", "Inspect the synthetic test stage only after open_stage succeeds.", {}, inspect_stage)
    conversation_id = _new_conversation(conversation_store, repo_id, scenario_name)
    planner, _ = _planner_from_session(provider, conversation_store, repo_id, conversation_id, scenario_name)
    events = []
    manager = _streaming(events)
    engine = _make_engine(registry, planner, state_store, result_store, repo_id, conversation_id, manager)
    objective = ("Use both synthetic tools in a single plan. Create task_id 'open_task' for open_stage, "
                 "then task_id 'inspect_task' for inspect_stage with dependency ['open_task']; "
                 "the second tool must only execute after the first. Finish after both succeed.")
    report = engine.run(objective, repository_id=repo_id, conversation_id=conversation_id)
    stream_result = _finish_streaming(manager, report, provider, events)
    _record_execution(scenario, report, planner, events, stream_result)
    plan_calls = [call for decision in planner.decisions for call in decision["selected_calls"]]
    dependency_declared = any(call["tool_id"] == "inspect_stage" and "open_task" in call["dependencies"]
                              for call in plan_calls)
    scenario["assertions"] = {"completed": report.status == "completed",
        "both_tools_called": set(state["order"]) == {"open", "inspect"},
        "dependency_declared_by_real_planner": dependency_declared,
        "runtime_order_respected": state["order"] == ["open", "inspect"],
        "real_planner_used": planner.delegate.request_count > 0}
    if all(scenario["assertions"].values()):
        scenario["session_restore"] = _persist_completed_turn(
            __import__("agentforge.session_service", fromlist=["SessionService"]).SessionService(),
            conversation_store, conversation_id, repo_id, report, objective, provider, planner)
        scenario["assertions"]["session_persisted_and_restored"] = scenario["session_restore"]["persisted"]
    scenario["status"] = "PASS" if all(scenario["assertions"].values()) else "FAIL"
    return scenario


def _run_observation_scenario(provider, state_store, result_store, conversation_store,
                              repo_id, scenario_name="observation_replanning") -> dict:
    scenario = _scenario_shell(scenario_name)
    registry = _base_registry()
    signal = "sig-" + uuid4().hex[:18]
    state = {"signal": signal, "verified": False}
    def reveal(_args, _ctx):
        return {"challenge": state["signal"], "note": "Use this exact value in the next planning round."}
    def confirm(args, _ctx):
        if args.get("challenge") != state["signal"]:
            raise ValueError("synthetic_observation_value_mismatch")
        state["verified"] = True
        return {"verified": True, "challenge_suffix": state["signal"][-6:]}
    _register_tool(registry, "reveal_signal", "Reveal a random run-scoped challenge in its tool observation.", {}, reveal)
    _register_tool(registry, "confirm_signal", "Confirm a challenge using the exact value returned by reveal_signal.",
        {"challenge": {"type": "string", "minLength": 8, "maxLength": 64}}, confirm, required=("challenge",))
    conversation_id = _new_conversation(conversation_store, repo_id, scenario_name)
    planner, _ = _planner_from_session(provider, conversation_store, repo_id, conversation_id, scenario_name)
    events = []
    manager = _streaming(events)
    engine = _make_engine(registry, planner, state_store, result_store, repo_id, conversation_id, manager)
    objective = ("Perform the dynamic synthetic signal check. First call reveal_signal. "
                 "Do not guess the challenge and do not call confirm_signal until a later planning round "
                 "after observing reveal_signal's actual result. In that later round pass its exact challenge "
                 "to confirm_signal, then finish.")
    report = engine.run(objective, repository_id=repo_id, conversation_id=conversation_id)
    stream_result = _finish_streaming(manager, report, provider, events)
    _record_execution(scenario, report, planner, events, stream_result)
    calls_by_round = [decision["selected_calls"] for decision in planner.decisions]
    first_round = calls_by_round[0] if calls_by_round else []
    followup = calls_by_round[1] if len(calls_by_round) > 1 else []
    exact_value_used = any(call["tool_id"] == "confirm_signal"
                           and call["arguments"].get("challenge") == signal for call in followup)
    scenario["assertions"] = {"completed": report.status == "completed",
        "first_round_only_reveals": bool(first_round) and all(call["tool_id"] == "reveal_signal" for call in first_round),
        "followup_round_has_observation": len(planner.decisions) > 1 and planner.decisions[1]["observation_count"] > 0,
        "followup_selected_confirmation": any(call["tool_id"] == "confirm_signal" for call in followup),
        "exact_unseen_value_was_replanned_from_observation": exact_value_used and state["verified"],
        "replanning_event_emitted": any(event["type"] == "REPLANNING" for event in events),
        "real_planner_used": planner.delegate.request_count > 0}
    if all(scenario["assertions"].values()):
        from agentforge.session_service import SessionService
        scenario["session_restore"] = _persist_completed_turn(SessionService(), conversation_store,
            conversation_id, repo_id, report, objective, provider, planner)
        scenario["assertions"]["session_persisted_and_restored"] = scenario["session_restore"]["persisted"]
    scenario["synthetic_challenge_proof"] = {"value_sha256": hashlib.sha256(signal.encode()).hexdigest(),
                                             "length": len(signal),
                                             "value_not_in_first_prompt": (
                                                 signal not in objective and signal not in json.dumps(first_round))}
    scenario["assertions"]["challenge_was_unavailable_to_initial_plan"] = (
        scenario["synthetic_challenge_proof"]["value_not_in_first_prompt"])
    scenario["status"] = "PASS" if all(scenario["assertions"].values()) else "FAIL"
    return scenario


def _run_failure_replan_scenario(provider, state_store, result_store, conversation_store,
                                 repo_id, scenario_name="failure_replanning") -> dict:
    scenario = _scenario_shell(scenario_name)
    from agentforge.agent_execution import ToolErrorCode, ToolExecutionFailure
    registry = _base_registry()
    state = {"primary_attempts": 0, "fallback_used": False}
    def primary(_args, _ctx):
        state["primary_attempts"] += 1
        raise ToolExecutionFailure(ToolErrorCode.TOOL_UNAVAILABLE, retryable=False)
    def fallback(_args, _ctx):
        state["fallback_used"] = True
        return {"recovered": True, "source": "synthetic_fallback"}
    _register_tool(registry, "primary_probe", "Attempt the primary synthetic read-only probe; this tool will report unavailable.", {}, primary)
    _register_tool(registry, "fallback_probe", "Use this synthetic read-only fallback after primary_probe reports unavailable.", {}, fallback)
    conversation_id = _new_conversation(conversation_store, repo_id, scenario_name)
    planner, _ = _planner_from_session(provider, conversation_store, repo_id, conversation_id, scenario_name)
    events = []
    manager = _streaming(events)
    engine = _make_engine(registry, planner, state_store, result_store, repo_id, conversation_id, manager)
    objective = ("Try primary_probe once. If its Observation says unavailable, do not retry it; "
                 "replan to fallback_probe and report whether fallback recovered.")
    report = engine.run(objective, repository_id=repo_id, conversation_id=conversation_id)
    stream_result = _finish_streaming(manager, report, provider, events)
    _record_execution(scenario, report, planner, events, stream_result)
    decisions = [decision["selected_calls"] for decision in planner.decisions]
    saw_failure = any(item.tool_id == "primary_probe" and item.status == "failed"
                      and item.error_code == ToolErrorCode.TOOL_UNAVAILABLE.value for item in report.results)
    fallback_round = any(call["tool_id"] == "fallback_probe" for group in decisions[1:] for call in group)
    scenario["assertions"] = {"completed": report.status == "completed",
        "primary_failed_once_without_retry": state["primary_attempts"] == 1 and saw_failure,
        "fallback_selected_after_observation": fallback_round and state["fallback_used"],
        "replanning_event_emitted": any(event["type"] == "REPLANNING" for event in events),
        "real_planner_used": planner.delegate.request_count > 0}
    if all(scenario["assertions"].values()):
        from agentforge.session_service import SessionService
        scenario["session_restore"] = _persist_completed_turn(SessionService(), conversation_store,
            conversation_id, repo_id, report, objective, provider, planner)
        scenario["assertions"]["session_persisted_and_restored"] = scenario["session_restore"]["persisted"]
    scenario["status"] = "PASS" if all(scenario["assertions"].values()) else "FAIL"
    return scenario


def _run_approval_scenario(provider, state_store, result_store, conversation_store,
                           repo_id, reconnect, scenario_name="approval_resume") -> dict:
    scenario = _scenario_shell(scenario_name)
    registry = _base_registry()
    state = {"executed": 0}
    def gated_action(_args, _ctx):
        state["executed"] += 1
        return {"completed": True, "effect": "in-memory synthetic marker only"}
    _register_tool(registry, "approve_synthetic_action",
        "Run an approval-gated in-memory synthetic marker action; no real side effect.", {}, gated_action,
        side_effect=True, approval=True)
    conversation_id = _new_conversation(conversation_store, repo_id, scenario_name)
    planner, _ = _planner_from_session(provider, conversation_store, repo_id, conversation_id, scenario_name)
    initial_events = []
    initial_stream = _streaming(initial_events)
    initial_engine = _make_engine(registry, planner, state_store, result_store, repo_id,
                                  conversation_id, initial_stream)
    objective = ("Run approve_synthetic_action. The action is in-memory synthetic only, "
                 "but it is approval gated. Wait for the explicit approval checkpoint before execution, "
                 "then finish after its Observation.")
    pending = initial_engine.run(objective, repository_id=repo_id, conversation_id=conversation_id)
    initial_stream_result = _finish_streaming(initial_stream, pending, provider, initial_events)
    if pending.status != "waiting_approval" or state["executed"] != 0:
        _record_execution(scenario, pending, planner, initial_events, initial_stream_result)
        scenario["assertions"] = {"approval_checkpoint_before_handler": False}
        scenario["status"] = "FAIL"
        return scenario
    reconnected = reconnect()
    if not isinstance(reconnected, tuple) or len(reconnected) != 4:
        raise HostAcceptanceBlocked("postgres_reconnect", "reconnect_adapter_contract_invalid")
    _, conversation_store, state_store, result_store = reconnected
    from agentforge.session_service import SessionService
    # Adapters returned by reconnect() use the new Engine and read the checkpoint from PostgreSQL.
    restored_planner, _ = _planner_from_session(
        provider, conversation_store, repo_id, conversation_id, scenario_name + "_resume")
    resume_events = []
    resume_stream = _streaming(resume_events)
    approve = lambda definition, call: (definition.tool_id == "approve_synthetic_action")
    resumed_engine = _make_engine(registry, restored_planner, state_store, result_store,
        repo_id, conversation_id, resume_stream, approval_handler=approve)
    resumed = resumed_engine.resume(pending.execution_id, objective=objective,
        repository_id=repo_id, conversation_id=conversation_id)
    resumed_stream_result = _finish_streaming(resume_stream, resumed, provider, resume_events)
    combined_events = initial_events + resume_events
    _record_execution(scenario, resumed, restored_planner, combined_events, resumed_stream_result)
    scenario["planner_decisions"] = planner.decisions + restored_planner.decisions
    scenario["request_diagnostics"] = [
        *(_safe_budget(item) for item in planner.request_diagnostics if isinstance(item, dict)),
        *(_safe_budget(item) for item in restored_planner.request_diagnostics if isinstance(item, dict)),
    ]
    scenario["approval_pause"] = {"status": pending.status, "tool_not_executed_before_approval": state["executed"] == 0,
                                  "pending_execution_id": pending.execution_id,
                                  "initial_stream": initial_stream_result}
    scenario["assertions"] = {"approval_checkpoint_persisted": pending.pending_approval is not None,
        "database_reconnected_before_resume": True,
        "new_engine_resumed_checkpoint": resumed.execution_id == pending.execution_id,
        "handler_ran_exactly_once_after_approval": state["executed"] == 1,
        "resume_completed": resumed.status == "completed",
        "streaming_suspended_then_completed": initial_stream_result.get("status") == "waiting_approval"
            and resumed_stream_result.get("status") == "validated_limited_general_contract",
        "planner_used_actual_observation_after_approval": any(
            decision["observation_count"] > 0 for decision in restored_planner.decisions)}
    if all(scenario["assertions"].values()):
        scenario["session_restore"] = _persist_completed_turn(SessionService(), conversation_store,
            conversation_id, repo_id, resumed, objective, provider, restored_planner)
        scenario["assertions"]["session_persisted_and_restored"] = scenario["session_restore"]["persisted"]
    scenario["status"] = "PASS" if all(scenario["assertions"].values()) else "FAIL"
    return scenario


def _run_large_result_scenario(provider, state_store, result_store, conversation_store,
                               repo_id, scenario_name="large_tool_result") -> dict:
    scenario = _scenario_shell(scenario_name)
    registry = _base_registry()
    body = "synthetic-result-" * 1800
    _register_tool(registry, "read_large_report", "Read a large synthetic report from an in-memory fixture.", {},
        lambda _args, _ctx: {"report": body, "origin": "synthetic_fixture"})
    conversation_id = _new_conversation(conversation_store, repo_id, scenario_name)
    planner, _ = _planner_from_session(provider, conversation_store, repo_id, conversation_id, scenario_name)
    events = []
    manager = _streaming(events)
    engine = _make_engine(registry, planner, state_store, result_store, repo_id, conversation_id, manager)
    objective = ("Call read_large_report. Do not copy its large text into the final response; "
                 "report only that the synthetic report was retrieved and summarized.")
    report = engine.run(objective, repository_id=repo_id, conversation_id=conversation_id)
    stream_result = _finish_streaming(manager, report, provider, events)
    _record_execution(scenario, report, planner, events, stream_result)
    partial = next((item for item in report.results if item.tool_id == "read_large_report"), None)
    reference = partial.output.get("result_ref") if partial and isinstance(partial.output, dict) else None
    page = result_store.page(reference, offset=0, limit=128) if reference else None
    scenario["assertions"] = {"completed": report.status == "completed",
        "large_output_compacted_to_reference": bool(reference) and partial.status == "partial",
        "durable_page_retrieval_verified": bool(page and page.get("text", "").startswith('{"report":"synthetic-result-')),
        "observation_retained_reference_not_full_text": bool(partial and len(json.dumps(partial.output)) < 5_000),
        "real_planner_used": planner.delegate.request_count > 0}
    if all(scenario["assertions"].values()):
        scenario["session_restore"] = _persist_completed_turn(
            __import__("agentforge.session_service", fromlist=["SessionService"]).SessionService(),
            conversation_store, conversation_id, repo_id, report, objective, provider, planner)
        scenario["assertions"]["session_persisted_and_restored"] = scenario["session_restore"]["persisted"]
    scenario["large_result"] = {"reference": reference, "stored_chars": page.get("total_chars") if page else None,
                                "page_chars": len(page.get("text", "")) if page else None}
    scenario["status"] = "PASS" if all(scenario["assertions"].values()) else "FAIL"
    return scenario


def _run_timeout_scenario(provider, state_store, result_store, conversation_store,
                          repo_id, scenario_name="timeout_recovery") -> dict:
    scenario = _scenario_shell(scenario_name)
    registry = _base_registry()
    def slow_read(_args, context):
        deadline = time.monotonic() + 2.0
        while not context.cancel_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        return {"finished_after_timeout": True}
    _register_tool(registry, "slow_synthetic_read", "Run a synthetic read that exceeds its short tool timeout.", {},
        slow_read, timeout=0.15)
    conversation_id = _new_conversation(conversation_store, repo_id, scenario_name)
    planner, _ = _planner_from_session(provider, conversation_store, repo_id, conversation_id, scenario_name)
    events = []
    manager = _streaming(events)
    engine = _make_engine(registry, planner, state_store, result_store, repo_id, conversation_id, manager)
    objective = ("Call slow_synthetic_read once. It is expected to time out. "
                 "After the timeout Observation, do not retry it; finish with the timeout status.")
    report = engine.run(objective, repository_id=repo_id, conversation_id=conversation_id)
    stream_result = _finish_streaming(manager, report, provider, events)
    _record_execution(scenario, report, planner, events, stream_result)
    timed_out = any(item.tool_id == "slow_synthetic_read" and item.error_code == "TIMEOUT"
                    for item in report.results)
    replanned = any(decision["observation_count"] > 0 for decision in planner.decisions)
    scenario["assertions"] = {"tool_timeout_observed": timed_out, "planner_replanned_from_timeout": replanned,
        "no_retry": sum(1 for item in report.results if item.tool_id == "slow_synthetic_read") == 1,
        "real_planner_used": planner.delegate.request_count > 0,
        "request_failed_closed_or_finished": report.status in {"completed", "failed"}}
    scenario["status"] = "PASS" if all(scenario["assertions"].values()) else "FAIL"
    return scenario


def _run_cancel_scenario(provider, state_store, result_store, conversation_store,
                         repo_id, scenario_name="cancel_partial_result") -> dict:
    scenario = _scenario_shell(scenario_name)
    registry = _base_registry()
    cancel_event = threading.Event()
    def cancel_read(_args, _ctx):
        cancel_event.set()
        return {"completed_before_cancel": False}
    _register_tool(registry, "cancel_synthetic_read", "Read a synthetic item and trigger the runner's cancellation checkpoint.", {},
        cancel_read)
    conversation_id = _new_conversation(conversation_store, repo_id, scenario_name)
    planner, _ = _planner_from_session(provider, conversation_store, repo_id, conversation_id, scenario_name)
    events = []
    manager = _streaming(events, cancel_event=cancel_event)
    engine = _make_engine(registry, planner, state_store, result_store, repo_id, conversation_id,
                          manager, cancel_event=cancel_event)
    objective = "Call cancel_synthetic_read once. The harness will cancel immediately after its result."
    report = engine.run(objective, repository_id=repo_id, conversation_id=conversation_id)
    if report.status == "cancelled":
        manager.cancel()
        stream_result = {"status": "cancelled"}
    else:
        stream_result = _finish_streaming(manager, report, provider, events)
    _record_execution(scenario, report, planner, events, stream_result)
    scenario["assertions"] = {"planner_selected_synthetic_cancel_tool": any(
            call["tool_id"] == "cancel_synthetic_read" for decision in planner.decisions
            for call in decision["selected_calls"]),
        "cancelled_state_reported": report.status == "cancelled",
        "partial_observation_retained": any(item.tool_id == "cancel_synthetic_read" for item in report.results),
        "streaming_cancelled_without_final_answer": stream_result.get("status") == "cancelled"
            and not any(event["type"] == "FINAL_ANSWER" for event in events),
        "real_planner_used": planner.delegate.request_count > 0}
    scenario["status"] = "PASS" if all(scenario["assertions"].values()) else "FAIL"
    return scenario


def _main() -> int:
    result = _build_report()
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        return _blocked(result, "environment_gate", "network_disabled_by_environment")
    if not _safe_listener_probe():
        return _blocked(result, "qwen_preflight", "qwen_chat_listener_absent")
    try:
        result["qwen_health"] = _health_ready()
        result["checks"]["qwen_chat_health"] = "PASS"
    except HostAcceptanceBlocked as exc:
        return _blocked(result, exc.stage, exc.code)
    if not _safe_listener_probe("127.0.0.1", 5432):
        return _blocked(result, "postgres_preflight", "postgresql_listener_absent")
    result["checks"]["postgresql_listener"] = "PASS"
    missing = [name for name in (CHAT_KEY_ENV, DB_KEY_ENV) if not os.environ.get(name)]
    if missing:
        result["missing_configuration_names"] = missing
        return _blocked(result, "configuration", "local_runtime_credential_missing")

    # PostgreSQL administrator password is requested only after Qwen and PostgreSQL
    # have passed readiness checks. getpass never echoes or stores it in reports.
    admin_password = getpass.getpass("PostgreSQL administrator password for disposable database (hidden): ")
    if not admin_password:
        return _blocked(result, "database_provision", "admin_credential_missing")
    run_id = result["run_id"]
    database = "af033_agent_" + run_id[:20]
    engine = None
    temp_root = None
    provider = None
    try:
        from sqlalchemy import text
        from agentforge.af012_connection import check_database_health
        from agentforge.conversation_migrations import upgrade
        from agentforge.conversation_store import ConversationStore
        from agentforge.memory_store import MemoryStore
        from agentforge.session_service import SessionService
        from agentforge.agent_execution import PostgresExecutionStateStore, PostgresToolResultStore
        from agentforge.af012_runtime import create_persistent_chroma
        from agentforge.cli import create_provider

        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        temp_root = TEST_TEMP_ROOT / f"af033-agent-host-{run_id}"
        temp_root.mkdir()
        provision = result["database_provision"]
        provision.update(status="running", operation="admin_connection", database_name=database)
        result["temporary_resources"]["database_name"] = database
        result["cleanup"]["database"] = "pending"

        def record_stage(operation):
            provision["operation"] = operation

        def record_database_absent(confirmed_database):
            if confirmed_database != database:
                raise RuntimeError("database_provision_identity_mismatch")
            provision["database_absence_confirmed"] = True

        def record_database_created(created_database):
            if created_database != database:
                raise RuntimeError("database_provision_identity_mismatch")
            result["temporary_resources"]["database_created"] = True
            result["cleanup"]["database"] = "pending"
            provision["status"] = "created"

        database = _create_test_database(
            run_id, admin_password, on_stage=record_stage,
            on_database_absent=record_database_absent, on_created=record_database_created)
        provision.update(status="complete", operation="complete")
        result["temporary_resources"].update({"database_created": True,
            "database_name": database, "chroma_created": False,
            "chroma_path": str((temp_root / "chroma").resolve()),
            "synthetic_repository_created": True,
            "synthetic_repository_path": str((temp_root / "repository").resolve())})
        result["cleanup"].update({"database": "pending", "chroma_directory": "pending",
                                  "synthetic_repository": "pending"})
        engine = _create_test_engine(database, os.environ[DB_KEY_ENV])
        if not check_database_health(engine):
            raise HostAcceptanceBlocked("database_preflight", "disposable_database_health_failed")
        identity = _database_identity(engine, database)
        upgrade(engine)
        result["database"] = {"identity": identity, "migration": "upgraded_to_head"}
        result["checks"]["isolated_database_and_migration"] = "PASS"

        chroma_path = temp_root / "chroma"
        collection_name = "af033_agent_" + run_id[:20]
        chroma_result = _verify_temporary_chroma_reopen(
            create_persistent_chroma, chroma_path, collection_name)
        result["chroma"] = {"path_is_run_scoped": True, **chroma_result}
        result["temporary_resources"]["chroma_created"] = True
        result["checks"]["isolated_chroma_create_and_reopen"] = "PASS"

        repo_path = temp_root / "repository"
        repo_path.mkdir()
        repo_id = _register_synthetic_repository(engine, repo_path, run_id)
        result["synthetic_repository"] = {"repository_id": repo_id, "files_created": 0,
                                          "source_access": "none; tools are in-memory handlers"}
        conversation_store = ConversationStore(engine)
        state_store = PostgresExecutionStateStore(engine)
        result_store = PostgresToolResultStore(engine)
        session_service = SessionService()

        from agentforge.agent_execution import LLMExecutionPlanner
        provider = create_provider("llama-server", base_url=CHAT_ENDPOINT, model=CHAT_MODEL,
            api_key=os.environ[CHAT_KEY_ENV], max_output_tokens=LLMExecutionPlanner.MAX_OUTPUT_TOKENS)
        provider = CappedLocalProvider(provider, limit=MAX_CHAT_REQUESTS)
        result["checks"]["real_qwen_provider"] = "PASS"

        def reconnect_database():
            nonlocal engine, conversation_store, state_store, result_store
            engine.dispose()
            engine = _create_test_engine(database, os.environ[DB_KEY_ENV])
            if not check_database_health(engine):
                raise HostAcceptanceBlocked("postgres_reconnect", "disposable_database_reconnect_failed")
            _database_identity(engine, database)
            conversation_store = ConversationStore(engine)
            state_store = PostgresExecutionStateStore(engine)
            result_store = PostgresToolResultStore(engine)
            result["approval_resume_reconnected"] = True
            return engine, conversation_store, state_store, result_store

        scenario_functions = [
            lambda: _run_single_tool(provider, state_store, result_store, conversation_store,
                                     session_service, repo_id),
            lambda: _run_dependency_scenario(provider, state_store, result_store, conversation_store, repo_id),
            lambda: _run_observation_scenario(provider, state_store, result_store, conversation_store, repo_id),
            lambda: _run_failure_replan_scenario(provider, state_store, result_store, conversation_store, repo_id),
            lambda: _run_approval_scenario(provider, state_store, result_store,
                                           conversation_store, repo_id, reconnect_database),
            lambda: _run_large_result_scenario(provider, state_store, result_store, conversation_store, repo_id),
            lambda: _run_timeout_scenario(provider, state_store, result_store, conversation_store, repo_id),
            lambda: _run_cancel_scenario(provider, state_store, result_store, conversation_store, repo_id),
        ]
        for run_scenario in scenario_functions:
            current_name = SCENARIO_NAMES[len(result["scenarios"])]
            try:
                scenario = run_scenario()
            except HostAcceptanceBlocked:
                raise
            except Exception as exc:
                scenario = _scenario_shell(current_name)
                scenario.update(status="FAIL", failure_type=type(exc).__name__)
                result["scenarios"][current_name] = scenario
                if current_name in result["not_measured"]:
                    result["not_measured"].remove(current_name)
                result["failure_stage"] = current_name
                result["failure_code"] = "runner_exception_" + type(exc).__name__
                break
            name = scenario["scenario"]
            result["scenarios"][name] = scenario
            if name in result["not_measured"]:
                result["not_measured"].remove(name)
            if scenario["status"] != "PASS":
                result["failure_stage"] = name
                result["failure_code"] = "real_planner_assertion_failed"
                break
            # A request failure or protocol failure stops subsequent model traffic.
            stats = _safe_request_counts(provider.request_stats())
            if stats["failed"]:
                result["failure_stage"] = name
                result["failure_code"] = "qwen_request_failed"
                break
        result["chat_requests"] = _safe_request_counts(provider.request_stats())
        result["chat_request_limit"] = MAX_CHAT_REQUESTS
        result["checks"]["request_budget"] = (
            "PASS" if result["chat_requests"]["sent"] <= MAX_CHAT_REQUESTS
            and result["chat_requests"]["retries"] == 0 else "FAIL")
        result["checks"]["all_agent_scenarios"] = (
            "PASS" if len(result["scenarios"]) == len(scenario_functions)
            and all(item.get("status") == "PASS" for item in result["scenarios"].values()) else "FAIL")
        if result["failure_code"]:
            result["status"] = "FAIL"
        else:
            result["status"] = "PASS"
        persisted_scenarios = [item for item in result["scenarios"].values()
                               if item.get("session_restore") is not None]
        result["checks"]["session_execution_persistence_and_restore"] = (
            "NOT_MEASURED" if not persisted_scenarios else
            "PASS" if all(item["session_restore"].get("persisted") for item in persisted_scenarios) else "FAIL")
        stream_scenarios = [item for item in result["scenarios"].values() if item.get("status") == "PASS"]
        event_types = {item.get("scenario"): {event.get("type") for event in item.get("streaming_events", [])}
                       for item in stream_scenarios}
        def has_expected_terminal(name, values):
            if name == "approval_resume":
                return {"WAITING_APPROVAL", "COMPLETED"}.issubset(values)
            if name == "timeout_recovery":
                return bool({"FAILED", "COMPLETED"} & values)
            if name == "cancel_partial_result":
                return "CANCELLED" in values
            return "COMPLETED" in values
        stream_ok = bool(stream_scenarios) and all(
            has_expected_terminal(name, values) for name, values in event_types.items())
        result["checks"]["streaming_lifecycle"] = "PASS" if stream_ok else (
            "NOT_MEASURED" if not stream_scenarios else "FAIL")
        approval_scenario = result["scenarios"].get("approval_resume")
        result["checks"]["database_reconnect_for_resume"] = (
            "NOT_MEASURED" if approval_scenario is None else
            "PASS" if result.get("approval_resume_reconnected") is True else "FAIL")
    except HostAcceptanceBlocked as exc:
        provisioning_failed = result["database_provision"].get("status") in {"running", "created"}
        if provisioning_failed:
            _record_database_provision_failure(result, exc)
        result.update(status="BLOCKED",
                      failure_stage="database_provision" if provisioning_failed else exc.stage,
                      failure_code=exc.code)
        if provider is not None:
            result["chat_requests"] = _safe_request_counts(provider.request_stats())
    except Exception as exc:
        provisioning_failed = result["database_provision"].get("status") in {"running", "created"}
        if provisioning_failed:
            _record_database_provision_failure(result, exc)
        result.update(status="FAIL",
                      failure_stage="database_provision" if provisioning_failed else "host_runner",
                      failure_code=type(exc).__name__)
        if provider is not None:
            result["chat_requests"] = _safe_request_counts(provider.request_stats())
    finally:
        if engine is not None:
            with suppress(Exception):
                engine.dispose()
        if result["temporary_resources"].get("database_created") and database and admin_password:
            try:
                _drop_test_database(database, admin_password)
                result["cleanup"]["database"] = "removed"
            except Exception as exc:
                result["cleanup"]["database"] = "failed"
                result["cleanup"]["database_error_type"] = type(exc).__name__
                result["status"] = "FAIL"
        elif result["cleanup"].get("database") == "pending":
            provision_status = result["database_provision"].get("status")
            result["cleanup"]["database"] = (
                "unverified" if provision_status == "creation_outcome_unknown" else "not_created")
        if temp_root is not None and temp_root.exists():
            try:
                expected_database = database or "af033_agent_" + run_id[:20]
                owned_db, owned_path = _validate_temp_scope(expected_database, temp_root)
                if owned_db == expected_database and not _is_link_or_junction(temp_root):
                    shutil.rmtree(owned_path)
                    result["cleanup"]["chroma_directory"] = "removed"
                    result["cleanup"]["synthetic_repository"] = "removed"
                else:
                    raise RuntimeError
            except Exception as exc:
                result["cleanup"]["chroma_directory"] = "failed"
                result["cleanup"]["synthetic_repository"] = "failed"
                result["cleanup"]["temp_error_type"] = type(exc).__name__
                result["status"] = "FAIL"
        admin_password = None
        if result["status"] not in {"FAIL", "BLOCKED", "PASS"}:
            result["status"] = "FAIL"
        _write_report(result)
        print(json.dumps({"result_file": str(ROOT / f"AF033_AGENT_LOOP_HOST_RESULT_{run_id}.json"),
                          "report_file": str(ROOT / f"AF033_AGENT_LOOP_HOST_REPORT_{run_id}.md"),
                          "status": result["status"], "failure_stage": result.get("failure_stage"),
                          "failure_code": result.get("failure_code"),
                          "chat_requests": result.get("chat_requests"),
                          "checks": result.get("checks"), "cleanup": result.get("cleanup")},
                         ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
