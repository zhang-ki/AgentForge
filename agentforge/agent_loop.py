"""有界、可审计的只读 Agent 工具，以及确定性离线 Planner。

循环绝不执行仓库代码。唯一的写入操作是显式运行 ``index`` 工具，
用于更新 AgentForge 管理的 SQLite 索引；仓库源码始终保持只读。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import re
import sqlite3
import time
from typing import Any, Protocol

from .answer_contract import VerifiedAnswerContract, build_offline_contract, verify_generated_answer
from .agent_execution import ToolAuditRecord, ToolCall
from .chunker import split_source_lines
from .context import build_context, validate_model_citations
from .errors import AgentForgeError
from .indexer import index_repository
from .models import Answer
from .providers.offline import OfflineProvider
from .product_retrieval import ProductRetriever
from .retriever import search
from .scanner import ScanLimits, read_candidate, scan_repository


ALLOWED_AGENT_TOOLS = frozenset({"index", "search", "read", "list-symbols"})
MAX_AGENT_STEPS = 8
MAX_AGENT_READ_LINES = 200
_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$", re.UNICODE)


@dataclass(frozen=True)
class AgentRunReport:
    objective: str
    status: str
    max_steps: int
    completed_steps: int
    elapsed_seconds: float
    tool_timeout_seconds: float
    run_timeout_seconds: float
    allowed_tools: tuple[str, ...]
    audit: tuple[ToolAuditRecord, ...]
    answer: str
    citations: tuple[str, ...]
    offline: bool = True
    retrieval: dict[str, Any] | None = None
    answer_contract: VerifiedAnswerContract | None = None
    execution_id: str | None = None
    execution_state: str | None = None
    execution_rounds: int = 0
    execution_results: tuple[dict[str, Any], ...] = ()
    user_input_request: str | None = None
    pending_approval: dict[str, Any] | None = None
    planner_request_diagnostics: tuple[dict[str, Any], ...] = ()


class AgentPlanner(Protocol):
    def next_call(
        self, objective: str, audit: tuple[ToolAuditRecord, ...]
    ) -> ToolCall | None:
        """返回下一条结构化调用；规划完成时返回 None。"""


def _strict_arguments(
    arguments: Any, *, required: set[str], optional: set[str]
) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be an object.")
    keys = set(arguments)
    missing = required - keys
    unexpected = keys - required - optional
    if missing:
        raise ValueError("Missing tool arguments: " + ", ".join(sorted(missing)) + ".")
    if unexpected:
        raise ValueError("Unexpected tool arguments: " + ", ".join(sorted(unexpected)) + ".")
    return arguments


def _nonempty_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string.")
    value = value.strip()
    if len(value) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    if any(ord(character) < 32 and character not in "\t" for character in value):
        raise ValueError(f"{label} must not contain control characters.")
    return value


def _bounded_integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}.")
    return value


def _normalized_relative_path(value: Any) -> str:
    path = _nonempty_text(value, "path", maximum=1024).replace("\\", "/")
    parts = path.split("/")
    if path.startswith("/") or ":" in path or any(part in ("", ".", "..") for part in parts):
        raise ValueError("path must be a normalized repository-relative path.")
    return path


class ReadOnlyToolbox:
    """只对一个明确获准的仓库执行四种允许的工具。"""

    def __init__(self, repository: str | os.PathLike[str], store, *, limits: ScanLimits | None = None):
        report = scan_repository(repository, limits)
        if not report.complete:
            raise ValueError("Repository scan is incomplete; read-only agent is refused.")
        self.repository = report.root
        self.store = store
        self.limits = limits or ScanLimits()
        self.last_search_results = []
        self.last_retrieval_outcome = None
        self._agent_search_candidates = []
        self._agent_search_hashes = set()
        self.last_evidence_source_ids = {}
        self.last_candidate_by_citation = {}
        self.product_retriever = ProductRetriever(
            store, self.repository, limits=self.limits, scan_report=report,
        )

    def execute(self, call: ToolCall) -> Any:
        if not isinstance(call, ToolCall):
            raise ValueError("Planner output must be a ToolCall.")
        if call.name not in ALLOWED_AGENT_TOOLS:
            raise ValueError(f"Unknown or forbidden tool: {call.name!r}.")
        _nonempty_text(call.origin, "tool origin", maximum=128)
        return getattr(self, "_tool_" + call.name.replace("-", "_"))(call.arguments)

    def _tool_index(self, raw: Any) -> dict:
        arguments = _strict_arguments(raw, required={"repository"}, optional={"replace_repository"})
        repository = Path(_nonempty_text(arguments["repository"], "repository", maximum=4096))
        try:
            requested = repository.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ValueError("repository must identify the authorized repository root.") from None
        if os.path.normcase(str(requested)) != os.path.normcase(str(self.repository)):
            raise ValueError("index cannot switch outside the authorized repository root.")
        replace_repository = arguments.get("replace_repository", False)
        if not isinstance(replace_repository, bool):
            raise ValueError("replace_repository must be a boolean.")
        return index_repository(
            self.repository, self.store, limits=self.limits,
            replace_repository=replace_repository,
        )

    def _tool_search(self, raw: Any) -> dict:
        arguments = _strict_arguments(raw, required={"query"}, optional={"top_k"})
        query = _nonempty_text(arguments["query"], "query")
        top_k = _bounded_integer(arguments.get("top_k", 5), "top_k", 1, 100)
        outcome = self.product_retriever.search(query, top_k=top_k)
        self.last_retrieval_outcome = outcome
        for candidate in outcome.candidates:
            if candidate.content_hash in self._agent_search_hashes:
                continue
            if len(self._agent_search_candidates) >= 20:
                break
            self._agent_search_hashes.add(candidate.content_hash)
            self.last_evidence_source_ids[candidate.content_hash] = f"E{len(self._agent_search_candidates) + 1}"
            self._agent_search_candidates.append(candidate)
        cumulative = self._agent_search_candidates
        context = build_context(query, [item.as_search_result() for item in cumulative],
                                max_candidates=20, budget=6000,
                                evidence_source_ids=self.last_evidence_source_ids)
        by_hash = {item.content_hash: item for item in cumulative}
        self.last_search_results = [source.result for source in context.evidence]
        self.last_candidate_by_citation.update({
            source.citation: by_hash[source.result.content_hash] for source in context.evidence
        })
        return {
            "query": query,
            "route": outcome.decision.route,
            "route_rule": outcome.decision.rule,
            "route_reason": outcome.decision.reason,
            "route_confidence": outcome.decision.confidence,
            "unavailable_channels": list(outcome.unavailable_channels),
            "uncertainty": outcome.uncertainty,
            "results": [{**asdict(by_hash[source.result.content_hash]),
                         "source_id": source.source_id, "citation": source.citation}
                        for source in context.evidence],
        }

    def _tool_read(self, raw: Any) -> dict:
        arguments = _strict_arguments(raw, required={"path"}, optional={"start_line", "end_line"})
        relative_path = _normalized_relative_path(arguments["path"])
        report = scan_repository(self.repository, self.limits)
        if not report.complete:
            raise ValueError("Repository scan is incomplete; read is refused.")
        candidates = {item.relative_path: item for item in report.candidates}
        candidate = candidates.get(relative_path)
        if candidate is None:
            raise ValueError("path is not an allowed readable source file.")
        text = read_candidate(candidate, report.root, self.limits.max_file_bytes)
        lines = split_source_lines(text)
        if not lines:
            return {"path": relative_path, "start_line": 0, "end_line": 0, "text": ""}
        start = _bounded_integer(arguments.get("start_line", 1), "start_line", 1, len(lines))
        default_end = min(len(lines), start + MAX_AGENT_READ_LINES - 1)
        end = _bounded_integer(arguments.get("end_line", default_end), "end_line", start, len(lines))
        if end - start + 1 > MAX_AGENT_READ_LINES:
            raise ValueError(f"read cannot exceed {MAX_AGENT_READ_LINES} lines.")
        return {
            "path": relative_path,
            "start_line": start,
            "end_line": end,
            "text": "\n".join(lines[start - 1:end]),
        }

    def _tool_list_symbols(self, raw: Any) -> dict:
        arguments = _strict_arguments(
            raw, required=set(), optional={"name", "qualified_name", "kind", "path", "limit"}
        )
        if not any(key in arguments for key in ("name", "qualified_name", "kind", "path")):
            raise ValueError("list-symbols requires at least one symbol filter.")
        name = _nonempty_text(arguments["name"], "name") if "name" in arguments else None
        qualified = (
            _nonempty_text(arguments["qualified_name"], "qualified_name")
            if "qualified_name" in arguments else None
        )
        kind = arguments.get("kind")
        if kind is not None and kind not in ("module", "class", "function", "method"):
            raise ValueError("kind must be module, class, function or method.")
        path = _normalized_relative_path(arguments["path"]) if "path" in arguments else None
        limit = _bounded_integer(arguments.get("limit", 100), "limit", 1, 500)
        return {"results": self.store.search_symbols(
            name=name, qualified_name=qualified, kind=kind,
            relative_path=path, limit=limit,
        )}


class OfflineAgentPlanner:
    """用于测试和本地回退的确定性 Planner；它不代表模型推理。"""

    def __init__(self, repository: Path, *, top_k: int = 5, replace_repository: bool = False):
        self.repository = repository
        self.top_k = _bounded_integer(top_k, "top_k", 1, 100)
        self.replace_repository = replace_repository

    def next_call(self, objective: str, audit: tuple[ToolAuditRecord, ...]) -> ToolCall | None:
        successful = [item for item in audit if item.status == "ok"]
        if not successful:
            return ToolCall(
                "index",
                {"repository": str(self.repository), "replace_repository": self.replace_repository},
                "offline_planner",
            )
        if len(successful) == 1:
            return ToolCall("search", {"query": objective, "top_k": self.top_k}, "offline_planner")
        search_record = next((item for item in successful if item.name == "search"), None)
        if len(successful) == 2 and _IDENTIFIER.fullmatch(objective.strip()):
            return ToolCall("list-symbols", {"name": objective.strip(), "limit": self.top_k}, "offline_planner")
        if not any(item.name == "read" for item in successful) and search_record:
            results = search_record.result.get("results", []) if isinstance(search_record.result, dict) else []
            if results:
                first = results[0]
                return ToolCall(
                    "read",
                    {"path": first["relative_path"], "start_line": first["start_line"], "end_line": first["end_line"]},
                    "offline_planner",
                )
        return None


class AgentLoop:
    """在严格预算下执行 Planner 调用，并保留完整的内存审计记录。

    超时控制采用协作式机制：有界仓库操作会同步执行，
    调用一旦超过允许的总耗时，本次运行就会失败，不会重试。
    """

    def __init__(
        self, toolbox: ReadOnlyToolbox, planner: AgentPlanner, *,
        max_steps: int = 4, tool_timeout_seconds: float = 30.0,
        run_timeout_seconds: float = 120.0, monotonic=time.monotonic,
        state_store=None, result_store=None, authorization=None, max_concurrency: int = 4,
        on_event=None, cancel_event=None, repository_id: str | None = None,
        conversation_id: str | None = None, include_index_tool: bool = True,
        tool_registry_factory=None,
    ):
        self.toolbox = toolbox
        self.planner = planner
        self.max_steps = _bounded_integer(max_steps, "max_steps", 1, MAX_AGENT_STEPS)
        for value, label, upper in (
            (tool_timeout_seconds, "tool_timeout_seconds", 120.0),
            (run_timeout_seconds, "run_timeout_seconds", 600.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= upper:
                raise ValueError(f"{label} must be greater than zero and at most {upper:g}.")
        if tool_timeout_seconds > run_timeout_seconds:
            raise ValueError("tool_timeout_seconds cannot exceed run_timeout_seconds.")
        self.tool_timeout_seconds = float(tool_timeout_seconds)
        self.run_timeout_seconds = float(run_timeout_seconds)
        self.monotonic = monotonic
        self.state_store, self.result_store, self.authorization = state_store, result_store, authorization
        self.max_concurrency, self.on_event = max_concurrency, on_event
        self.cancel_event = cancel_event
        self.repository_id, self.conversation_id = repository_id, conversation_id
        if not isinstance(include_index_tool, bool):
            raise ValueError("include_index_tool must be a boolean.")
        self.include_index_tool = include_index_tool
        if tool_registry_factory is not None and not callable(tool_registry_factory):
            raise ValueError("tool_registry_factory must be callable.")
        self.tool_registry_factory = tool_registry_factory

    def run(self, objective: str, *, execution_id: str | None = None,
            user_input: str | None = None) -> AgentRunReport:
        objective = _nonempty_text(objective, "objective")
        started = self.monotonic()
        from .agent_execution import (
            AgentExecutionEngine, LegacyPlannerAdapter, ToolAuthorization,
            ToolRegistry, toolbox_registry,
        )
        legacy = callable(getattr(self.planner, "next_call", None))
        execution_planner = LegacyPlannerAdapter(self.planner) if legacy else self.planner
        if self.tool_registry_factory is None:
            registry = toolbox_registry(
                self.toolbox, include_index=self.include_index_tool,
                timeout_seconds=self.tool_timeout_seconds,
            )
        else:
            registry = self.tool_registry_factory(self.toolbox)
            if not isinstance(registry, ToolRegistry):
                raise ValueError("tool_registry_factory must return a ToolRegistry.")
        authorization = self.authorization or ToolAuthorization(
            permission_scopes=frozenset(item.permission_scope for item in registry.definitions()))
        execution = AgentExecutionEngine(
            registry, execution_planner, authorization=authorization,
            state_store=self.state_store, result_store=self.result_store,
            max_rounds=min(16, self.max_steps + 1),
            max_tool_calls=self.max_steps, max_tasks_per_plan=min(12, self.max_steps),
            max_concurrency=self.max_concurrency, run_timeout_seconds=self.run_timeout_seconds,
            on_event=self.on_event, cancel_event=self.cancel_event,
            monotonic=self.monotonic,
        ).run(objective, repository_id=self.repository_id, conversation_id=self.conversation_id,
              execution_id=execution_id, user_input=user_input, started_at=started)
        elapsed = self.monotonic() - started
        audit = []
        for step, result in enumerate(execution.results, 1):
            call = (execution_planner.calls.get(result.call_id)
                    if legacy and isinstance(execution_planner, LegacyPlannerAdapter) else None)
            origin = getattr(call, "origin", "llm_planner")
            arguments = dict(getattr(call, "arguments", {}))
            status = "ok" if result.status == "completed" else (
                "timeout" if result.error_code == "TIMEOUT" else "rejected")
            error = result.error_code
            if result.error_code == "TOOL_NOT_FOUND":
                error = "Unknown or forbidden tool request."
            elif result.error_code == "INVALID_ARGUMENTS":
                error = "Tool arguments were rejected by the registered schema."
            elif result.error_code == "PERMISSION_DENIED":
                error = "Tool permission was denied."
            audit.append(ToolAuditRecord(step, result.tool_id, origin, arguments, status,
                float(result.execution_metadata.get("elapsed_seconds", 0.0)), result.output, error))
        status = execution.status
        if any(item.status == "timeout" for item in audit):
            status = "tool_timeout"
        elif status == "failed" and execution.error_code == "PARTIAL_RESULT" and len(audit) >= self.max_steps:
            status = "step_budget_exhausted"
        elif status == "failed" and execution.error_code == "TIMEOUT":
            status = "run_timeout" if elapsed >= self.run_timeout_seconds else "tool_timeout"
        answer, citations, contract, retrieval = self._offline_answer(
            objective, generated_override=execution.final_answer)
        return AgentRunReport(
            objective=objective, status=status, max_steps=self.max_steps,
            completed_steps=len(audit), elapsed_seconds=round(elapsed, 6),
            tool_timeout_seconds=self.tool_timeout_seconds,
            run_timeout_seconds=self.run_timeout_seconds,
            allowed_tools=tuple(item.tool_id for item in registry.definitions()), audit=tuple(audit),
            answer=answer, citations=citations,
            offline=(execution.final_answer is None and getattr(self.planner, "final_answer", None) is None),
            retrieval=retrieval, answer_contract=contract,
            execution_id=execution.execution_id, execution_state=execution.state,
            execution_rounds=execution.rounds,
            execution_results=tuple({"call_id": item.call_id, "task_id": item.task_id,
                "tool_id": item.tool_id, "status": item.status, "error_code": item.error_code,
                "output": item.output, "execution_metadata": item.execution_metadata,
                "provenance": item.provenance}
                for item in execution.results),
            user_input_request=execution.user_input_request,
            pending_approval=execution.pending_approval,
            planner_request_diagnostics=execution.planner_request_diagnostics,
        )

    def _offline_answer(
        self, objective: str, *, generated_override: str | None = None,
    ) -> tuple[str, tuple[str, ...], VerifiedAnswerContract, dict[str, Any]]:
        results = getattr(self.toolbox, "last_search_results", [])
        context = build_context(objective, results, max_candidates=20, budget=6000,
                                evidence_source_ids=getattr(self.toolbox, "last_evidence_source_ids", None))
        generated = generated_override or getattr(self.planner, "final_answer", None)
        answer = (Answer(generated, (), False) if isinstance(generated, str) and generated.strip()
                  else OfflineProvider().answer(context))
        outcome = getattr(self.toolbox, "last_retrieval_outcome", None)
        if outcome is None:
            route = "uncertain"
            confidence = "low"
            uncertainty = "No product retrieval outcome was produced."
            candidates = {}
            retrieval = {
                "route": route, "confidence": confidence,
                "uncertainty": uncertainty, "unavailable_channels": [],
            }
        else:
            route = outcome.decision.route
            confidence = outcome.decision.confidence
            uncertainty = outcome.uncertainty
            candidates = {item.citation: item for item in outcome.candidates}
            retrieval = {
                "route": route,
                "rule": outcome.decision.rule,
                "reason": outcome.decision.reason,
                "confidence": confidence,
                "uncertainty": uncertainty,
                "unavailable_channels": list(outcome.unavailable_channels),
            }
        candidates.update(getattr(self.toolbox, "last_candidate_by_citation", {}))
        if generated and context.evidence:
            citations = validate_model_citations(generated, context)
            answer = Answer(generated, citations, False)
            contract = verify_generated_answer(
                answer, context, route=route, route_confidence=confidence,
                candidate_by_citation=candidates, uncertainty=uncertainty,
            )
        else:
            # No request-local evidence means the model text is never surfaced.
            answer = OfflineProvider().answer(context)
            contract = build_offline_contract(
                answer, context, route=route, route_confidence=confidence,
                candidate_by_citation=candidates, uncertainty=uncertainty,
            )
        return answer.text, answer.citations, contract, retrieval
