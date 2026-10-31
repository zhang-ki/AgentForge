"""Explicit, optional Reciprocal Rank Fusion over existing candidate retrievers."""

from dataclasses import dataclass
import math

from .config import MAX_TOP_K
from .retriever import search as lexical_search


@dataclass(frozen=True)
class RankContribution:
    channel: str
    rank: int
    original_score: float | None
    rrf_score: float


@dataclass(frozen=True)
class FusedCandidate:
    identity: str
    relative_path: str
    start_line: int
    end_line: int
    text: str
    score: float
    channels: tuple[RankContribution, ...]

    @property
    def citation(self) -> str:
        return f"{self.relative_path}:{self.start_line}-{self.end_line}"


def _field(result, name, default=None):
    return result.get(name, default) if isinstance(result, dict) else getattr(result, name, default)


def _score(result):
    value = _field(result, "score")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _identity(result) -> str:
    path = _field(result, "relative_path")
    if not isinstance(path, str) or not path:
        raise ValueError("Fusion candidates need a non-empty repository-relative path.")
    return "file:" + path.replace("\\", "/")


def fuse_ranked_results(channels: dict[str, list], *, k: int = 60, top_k: int = 10) -> list[FusedCandidate]:
    if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 10_000:
        raise ValueError("RRF k must be an integer from 1 to 10000.")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}.")
    if not isinstance(channels, dict) or any(
        not isinstance(name, str) or not name or not isinstance(results, list)
        for name, results in channels.items()
    ):
        raise ValueError("Fusion channels must map non-empty names to ranked lists.")

    accumulated = {}
    representatives = {}
    contributions = {}
    best_rank = {}
    for channel in sorted(channels):
        seen = set()
        rank = 0
        for result in channels[channel]:
            identity = _identity(result)
            if identity in seen:
                continue
            seen.add(identity)
            rank += 1
            contribution = 1.0 / (k + rank)
            accumulated[identity] = accumulated.get(identity, 0.0) + contribution
            contributions.setdefault(identity, []).append(RankContribution(
                channel=channel, rank=rank, original_score=_score(result), rrf_score=contribution,
            ))
            if identity not in representatives or rank < best_rank[identity]:
                representatives[identity] = result
                best_rank[identity] = rank

    ordered = sorted(accumulated, key=lambda identity: (
        -accumulated[identity], best_rank[identity], identity.casefold(),
    ))
    fused = []
    for identity in ordered[:top_k]:
        result = representatives[identity]
        path = _field(result, "relative_path")
        fused.append(FusedCandidate(
            identity=identity,
            relative_path=path,
            start_line=int(_field(result, "start_line", 1)),
            end_line=int(_field(result, "end_line", _field(result, "start_line", 1))),
            text=str(_field(result, "text", "")),
            score=accumulated[identity],
            channels=tuple(sorted(contributions[identity], key=lambda item: item.channel)),
        ))
    return fused


class RrfRetriever:
    """File-level RRF; absent channels are empty, while channel failures propagate."""

    def __init__(self, store, *, vector_retriever=None, k: int = 60, source_top_k: int = 10):
        if isinstance(source_top_k, bool) or not isinstance(source_top_k, int) or not 1 <= source_top_k <= MAX_TOP_K:
            raise ValueError(f"source_top_k must be between 1 and {MAX_TOP_K}.")
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 10_000:
            raise ValueError("RRF k must be an integer from 1 to 10000.")
        self.store = store
        self.vector_retriever = vector_retriever
        self.k = k
        self.source_top_k = source_top_k

    def search(self, query: str, *, top_k: int = 10, symbol_query: dict | None = None):
        channels = {"lexical": lexical_search(self.store, query, self.source_top_k)}
        channels["symbols"] = (
            self.store.search_symbols(**symbol_query, limit=self.source_top_k)
            if symbol_query else []
        )
        channels["vector"] = (
            self.vector_retriever.search(query, top_k=self.source_top_k)
            if self.vector_retriever is not None else []
        )
        return fuse_ranked_results(channels, k=self.k, top_k=top_k)
