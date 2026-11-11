"""检索、上下文与 Provider 之间共享的小型数据对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EvidenceProvenance:
    """Request-scoped origin for source evidence verified from an Agent read."""

    source_kind: str
    repository_id: str
    content_hash: str
    execution_id: str
    search_call_id: str
    tool_call_id: str
    observation_id: str
    tool_source_id: str


@dataclass(frozen=True)
class SearchResult:
    repository: str
    relative_path: str
    start_line: int
    end_line: int
    text: str
    content_hash: str
    score: float
    evidence_roles: tuple[str, ...] = ()
    provenance: EvidenceProvenance | None = None

    @property
    def citation(self) -> str:
        return f"{self.relative_path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class Evidence:
    source_id: str
    result: SearchResult

    @property
    def citation(self) -> str:
        return self.result.citation


@dataclass(frozen=True)
class BuiltContext:
    question: str
    evidence: tuple[Evidence, ...]
    text: str
    estimated_tokens: int
    budget: int
    estimate_kind: str = "UTF-8 byte upper-bound proxy; not a tokenizer count"
    planned_tasks: tuple[dict, ...] = ()
    tool_observations: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Answer:
    text: str
    citations: tuple[str, ...]
    offline: bool

