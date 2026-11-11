"""Derived repository-index lifecycle states; no status is duplicated in storage."""

from dataclasses import asdict, dataclass
from enum import Enum
import re


class IndexLifecycleState(str, Enum):
    UNREGISTERED = "UNREGISTERED"
    NOT_INITIALIZED = "NOT_INITIALIZED"
    INDEXING = "INDEXING"
    READY = "READY"
    STALE = "STALE"
    DEGRADED_LEXICAL_ONLY = "DEGRADED_LEXICAL_ONLY"
    LEXICAL_ONLY = "LEXICAL_ONLY"
    VECTOR_HEALTH_UNKNOWN = "VECTOR_HEALTH_UNKNOWN"
    NOT_REQUIRED = "NOT_REQUIRED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class IndexLifecycle:
    state: IndexLifecycleState
    repository_id: str | None
    sqlite_status: str
    vector_status: str
    reason_code: str | None = None
    expected_vectors: int | None = None
    stored_vectors: int | None = None
    invalid_vectors: int | None = None

    def to_dict(self) -> dict:
        value = asdict(self)
        value["state"] = self.state.value
        return value


def derive_index_lifecycle(
    *,
    repository_id: str | None,
    sqlite_available: bool = True,
    indexed: bool = True,
    stale: bool = False,
    vector_requested: bool = False,
    vector_available: bool = False,
    vector_complete: bool | None = None,
    reason_code: str | None = None,
    expected_vectors: int | None = None,
    stored_vectors: int | None = None,
    invalid_vectors: int | None = None,
) -> IndexLifecycle:
    """Derive one non-contradictory state from current authoritative observations."""
    if any(not isinstance(value, bool) for value in (
        sqlite_available, indexed, stale, vector_requested, vector_available,
    )):
        raise ValueError("Index lifecycle flags must be boolean.")
    if vector_complete is not None and not isinstance(vector_complete, bool):
        raise ValueError("vector_complete must be boolean or None.")
    for name, value in (("expected_vectors", expected_vectors),
                        ("stored_vectors", stored_vectors),
                        ("invalid_vectors", invalid_vectors)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{name} must be a non-negative integer or None.")
    if reason_code is not None and (
        not isinstance(reason_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason_code)
    ):
        raise ValueError("reason_code must be a bounded lowercase identifier.")

    if repository_id is None:
        return IndexLifecycle(IndexLifecycleState.UNREGISTERED, None,
                              "not_applicable", "not_applicable", reason_code or "no_repository")
    if not isinstance(repository_id, str) or not repository_id.strip():
        raise ValueError("repository_id must be non-empty when provided.")
    if not sqlite_available:
        return IndexLifecycle(IndexLifecycleState.FAILED, repository_id,
                              "unavailable", "not_checked", reason_code or "sqlite_unavailable")
    if not indexed:
        return IndexLifecycle(IndexLifecycleState.NOT_INITIALIZED, repository_id,
                              "not_initialized", "not_checked", reason_code or "index_not_initialized")
    if stale:
        return IndexLifecycle(IndexLifecycleState.STALE, repository_id,
                              "stale", "incomplete" if vector_complete is False else "not_current",
                              reason_code or "index_snapshot_stale", expected_vectors,
                              stored_vectors, invalid_vectors)
    if not vector_requested:
        return IndexLifecycle(IndexLifecycleState.LEXICAL_ONLY, repository_id,
                              "available", "disabled", reason_code or "vector_not_requested")
    if not vector_available:
        return IndexLifecycle(IndexLifecycleState.DEGRADED_LEXICAL_ONLY, repository_id,
                              "available", "unavailable", reason_code or "vector_unavailable")
    if vector_complete is False:
        return IndexLifecycle(IndexLifecycleState.STALE, repository_id,
                              "available", "incomplete", reason_code or "vector_index_incomplete",
                              expected_vectors, stored_vectors, invalid_vectors)
    if vector_complete is None:
        return IndexLifecycle(IndexLifecycleState.VECTOR_HEALTH_UNKNOWN, repository_id,
                              "available", "unknown", reason_code or "vector_integrity_unverified",
                              expected_vectors, stored_vectors, invalid_vectors)
    return IndexLifecycle(IndexLifecycleState.READY, repository_id,
                          "available", "ready", reason_code, expected_vectors,
                          stored_vectors, invalid_vectors)


class UnavailableVectorRetriever:
    """Explicit marker used when optional Embedding/Chroma setup fails."""

    def __init__(self, reason_code: str, error_type: str):
        if not isinstance(reason_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason_code):
            raise ValueError("Vector failure reason must be a bounded identifier.")
        if not isinstance(error_type, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
            raise ValueError("Vector failure type must be a bounded class name.")
        self.reason_code = reason_code
        self.error_type = error_type

    def search(self, *_args, **_kwargs):
        raise RuntimeError("Optional vector retrieval is unavailable.")
