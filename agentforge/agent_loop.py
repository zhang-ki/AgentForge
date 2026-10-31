"""Bounded, auditable, read-only agent tools with a deterministic offline planner.

The loop never executes repository code.  Its only write is the existing AgentForge
SQLite index when the explicit ``index`` tool runs; source repositories stay read-only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import re
import sqlite3
import time
from typing import Any, Protocol

from .answer_contract import VerifiedAnswerContract, build_offline_contract
from .chunker import split_source_lines
from .context import build_context
from .errors import AgentForgeError
from .indexer import index_repository
from .providers.offline import OfflineProvider
from .product_retrieval import ProductRetriever
from .retriever import search
from .scanner import ScanLimits, read_candidate, scan_repository


ALLOWED_AGENT_TOOLS = frozenset({"index", "search", "read", "list-symbols"})
MAX_AGENT_STEPS = 8
MAX_AGENT_READ_LINES = 200
_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$", re.UNICODE)


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    origin: str


@dataclass(frozen=True)
class ToolAuditRecord:
    step: int
    name: str
    origin: str
    arguments: dict[str, Any]
    status: str
    elapsed_seconds: float
    result: Any = None
    error: str | None = None


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


class AgentPlanner(Protocol):
    def next_call(
        self, objective: str, audit: tuple[ToolAuditRecord, ...]
    ) -> ToolCall | None:
        """Return the next structured call or None when planning is complete."""


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
    """Execute the four allowed tools against one explicitly authorized repository."""

    def __init__(self, repository: str | os.PathLike[str], store, *, limits: ScanLimits | None = None):
        report = scan_repository(repository, limits)
        if not report.complete:
            raise ValueError("Repository scan is incomplete; read-only agent is refused.")
        self.repository = report.root
        self.store = store
        self.limits = limits or ScanLimits()
        self.last_search_results = []
        self.last_retrieval_outcome = None
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
        self.last_search_results = [item.as_search_result() for item in outcome.candidates]
        return {
            "query": query,
            "route": outcome.decision.route,
            "route_rule": outcome.decision.rule,
            "route_reason": outcome.decision.reason,
            "route_confidence": outcome.decision.confidence,
            "unavailable_channels": list(outcome.unavailable_channels),
            "uncertainty": outcome.uncertainty,
            "results": [asdict(item) for item in outcome.candidates],
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
    """Deterministic test and local fallback planner; it is not model reasoning."""

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
    """Run planner calls with strict budgets and a complete in-memory audit trail.

    Timeout enforcement is cooperative: bounded repository operations run synchronously,
    and a call that exceeds its wall-clock allowance fails the run without retry.
    """

    def __init__(
        self, toolbox: ReadOnlyToolbox, planner: AgentPlanner, *,
        max_steps: int = 4, tool_timeout_seconds: float = 30.0,
        run_timeout_seconds: float = 120.0, monotonic=time.monotonic,
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

    def run(self, objective: str) -> AgentRunReport:
        objective = _nonempty_text(objective, "objective")
        started = self.monotonic()
        audit: list[ToolAuditRecord] = []
        status = "completed"
        for step in range(1, self.max_steps + 1):
            if self.monotonic() - started >= self.run_timeout_seconds:
                status = "run_timeout"
                break
            call = self.planner.next_call(objective, tuple(audit))
            if call is None:
                break
            if not isinstance(call, ToolCall):
                audit.append(ToolAuditRecord(
                    step, "invalid", "unknown", {}, "rejected", 0.0,
                    error="Planner output must be a ToolCall.",
                ))
                status = "failed"
                break
            call_started = self.monotonic()
            try:
                result = self.toolbox.execute(call)
                elapsed = self.monotonic() - call_started
                if elapsed > self.tool_timeout_seconds:
                    audit.append(ToolAuditRecord(
                        step, call.name, call.origin, dict(call.arguments), "timeout",
                        round(elapsed, 6), error="Tool exceeded its wall-clock budget; no retry was attempted.",
                    ))
                    status = "tool_timeout"
                    break
                audit.append(ToolAuditRecord(
                    step, call.name, call.origin, dict(call.arguments), "ok",
                    round(elapsed, 6), result=result,
                ))
            except (AgentForgeError, ValueError, OSError, sqlite3.Error) as exc:
                elapsed = self.monotonic() - call_started
                audit.append(ToolAuditRecord(
                    step, call.name if isinstance(call, ToolCall) else "invalid",
                    call.origin if isinstance(call, ToolCall) else "unknown",
                    dict(call.arguments) if isinstance(call, ToolCall) and isinstance(call.arguments, dict) else {},
                    "rejected", round(elapsed, 6), error=str(exc),
                ))
                status = "failed"
                break
        else:
            # Reaching the loop bound is not itself a failure: a plan may use
            # exactly the permitted number of tools.  Peek at the planner once
            # more (without executing another tool) to distinguish a completed
            # plan from one that still requires work.
            status = (
                "completed"
                if self.planner.next_call(objective, tuple(audit)) is None
                else "step_budget_exhausted"
            )

        elapsed = self.monotonic() - started
        if elapsed > self.run_timeout_seconds and status == "completed":
            status = "run_timeout"
        answer, citations, contract, retrieval = self._offline_answer(objective)
        return AgentRunReport(
            objective=objective, status=status, max_steps=self.max_steps,
            completed_steps=len(audit), elapsed_seconds=round(elapsed, 6),
            tool_timeout_seconds=self.tool_timeout_seconds,
            run_timeout_seconds=self.run_timeout_seconds,
            allowed_tools=tuple(sorted(ALLOWED_AGENT_TOOLS)), audit=tuple(audit),
            answer=answer, citations=citations,
            retrieval=retrieval, answer_contract=contract,
        )

    def _offline_answer(
        self, objective: str,
    ) -> tuple[str, tuple[str, ...], VerifiedAnswerContract, dict[str, Any]]:
        results = getattr(self.toolbox, "last_search_results", [])
        context = build_context(objective, results, max_candidates=5, budget=6000)
        answer = OfflineProvider().answer(context)
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
        contract = build_offline_contract(
            answer, context, route=route, route_confidence=confidence,
            candidate_by_citation=candidates, uncertainty=uncertainty,
        )
        return answer.text, answer.citations, contract, retrieval
