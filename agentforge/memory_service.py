"""Application boundary for approved-memory lifecycle and retrieval."""

from __future__ import annotations

from typing import Protocol


class MemoryRetrievalUnavailable(RuntimeError):
    """A memory lookup failed; callers may continue without optional memory."""

    def __init__(self, failure_type="Unavailable"):
        safe_type = failure_type if isinstance(failure_type, str) and failure_type.isalnum() else "Unavailable"
        self.failure_type = safe_type[:64]
        super().__init__("Approved memory retrieval is unavailable.")


class MemoryRepository(Protocol):
    def register_repository(self, repository_id, canonical_path, display_name=None): ...
    def list(self, repository_id, *, status=None, limit=100): ...
    def get(self, memory_id, repository_id): ...
    def propose(self, repository_id, conversation_id, content, source_message_ids, **options): ...
    def approve(self, memory_id, repository_id): ...
    def reject(self, memory_id, repository_id): ...
    def update(self, memory_id, repository_id, content, **options): ...
    def revoke(self, memory_id, repository_id): ...
    def expire_due(self, **options): ...
    def approved(self, repository_id, **options): ...
    def approved_by_id(self, memory_id, repository_id): ...
    def source_records(self, memory_id, repository_id): ...
    def sync_outbox(self, vector_store, embedding_provider, **options): ...


class MemoryService:
    """Coordinate memory lifecycle while leaving persistence and vector I/O behind ports."""

    def __init__(self, repository: MemoryRepository):
        self.repository = repository

    def register_repository(self, repository_id, canonical_path, display_name=None):
        return self.repository.register_repository(repository_id, canonical_path, display_name)

    def list(self, repository_id, *, status=None, limit=100):
        return self.repository.list(repository_id, status=status, limit=limit)

    def get(self, memory_id, repository_id):
        return self.repository.get(memory_id, repository_id)

    def propose(self, repository_id, conversation_id, content, source_message_ids, **options):
        return self.repository.propose(
            repository_id, conversation_id, content, source_message_ids, **options,
        )

    def approve(self, memory_id, repository_id):
        return self.repository.approve(memory_id, repository_id)

    def reject(self, memory_id, repository_id):
        return self.repository.reject(memory_id, repository_id)

    def update(self, memory_id, repository_id, content, **options):
        current = self.repository.get(memory_id, repository_id)
        options.setdefault("expires_at", current["expires_at"])
        return self.repository.update(memory_id, repository_id, content, **options)

    def revoke(self, memory_id, repository_id):
        return self.repository.revoke(memory_id, repository_id)

    def expire_due(self, **options):
        return self.repository.expire_due(**options)

    def approved(self, repository_id, **options):
        return self.repository.approved(repository_id, **options)

    def retrieve_approved(self, retriever, query, repository_id, *, limit=4):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Memory retrieval query is required.")
        if not isinstance(repository_id, str) or not repository_id.strip():
            raise ValueError("Memory retrieval repository scope is required.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
            raise ValueError("Memory retrieval limit must be between 1 and 8.")
        search = getattr(retriever, "search", None)
        if not callable(search):
            raise ValueError("Memory retrieval port is unavailable.")
        authoritative_lookup = getattr(self.repository, "approved_by_id", None)
        source_lookup = getattr(self.repository, "source_records", None)
        if not callable(authoritative_lookup) or not callable(source_lookup):
            raise ValueError("Memory authority verification port is unavailable.")
        result = search(query, repository_id=repository_id, limit=limit)
        approved = []
        seen_ids = set()
        for candidate in result or ():
            memory_id = getattr(candidate, "memory_id", None)
            sources = getattr(candidate, "sources", ())
            if (getattr(candidate, "approved", False) is not True
                    or getattr(candidate, "repository_id", None) != repository_id
                    or getattr(candidate, "scope", None) not in {"global", "repository"}
                    or not isinstance(memory_id, str) or not memory_id.strip()
                    or memory_id in seen_ids or not isinstance(sources, (tuple, list)) or not sources):
                continue
            record = authoritative_lookup(memory_id, repository_id)
            if (record is None or record.get("id") != memory_id
                    or record.get("approval_status") != "approved"
                    or record.get("scope") != getattr(candidate, "scope", None)
                    or record.get("version") != getattr(candidate, "version", None)
                    or record.get("memory_type") != getattr(candidate, "memory_type", None)
                    or record.get("content") != getattr(candidate, "content", None)):
                continue
            authority_sources = source_lookup(memory_id, repository_id)
            if not _same_memory_sources(sources, authority_sources):
                continue
            approved.append(candidate)
            seen_ids.add(memory_id)
            if len(approved) >= limit:
                break
        return tuple(approved)

    def sync_outbox(self, vector_store, embedding_provider, **options):
        return self.repository.sync_outbox(vector_store, embedding_provider, **options)

    def outbox_status(self, repository_id):
        return self.repository.outbox_status(repository_id)

    def source_records(self, memory_id, repository_id):
        return self.repository.source_records(memory_id, repository_id)

    def events(self, memory_id, repository_id):
        return self.repository.events(memory_id, repository_id)

    def requeue_approved(self, repository_id):
        return self.repository.requeue_approved(repository_id)


__all__ = ["MemoryRepository", "MemoryRetrievalUnavailable", "MemoryService"]


def _same_memory_sources(candidates, authoritative):
    if not isinstance(authoritative, (tuple, list)) or not authoritative:
        return False
    def identity(source, *, mapping):
        if mapping:
            message_id = source.get("message_id")
            sequence = source.get("source_sequence")
            role = source.get("source_role")
            digest = source.get("source_hash")
        else:
            message_id = getattr(source, "message_id", None)
            sequence = getattr(source, "source_sequence", None)
            role = getattr(source, "source_role", None)
            digest = getattr(source, "source_hash", None)
        if (message_id is not None and not isinstance(message_id, str)
                or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1
                or role not in {"user", "assistant"}
                or not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest.lower())):
            return None
        return message_id, sequence, role, digest.lower()
    candidate_ids = [identity(item, mapping=False) for item in candidates]
    authority_ids = [identity(item, mapping=True) for item in authoritative]
    return (None not in candidate_ids and None not in authority_ids
            and len(candidate_ids) == len(authority_ids)
            and sorted(candidate_ids) == sorted(authority_ids))
