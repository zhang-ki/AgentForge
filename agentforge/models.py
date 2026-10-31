"""Small data objects shared by retrieval, context, and providers."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    repository: str
    relative_path: str
    start_line: int
    end_line: int
    text: str
    content_hash: str
    score: float

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


@dataclass(frozen=True)
class Answer:
    text: str
    citations: tuple[str, ...]
    offline: bool

