"""Persistent Chroma indexing and verified code/document vector retrieval."""
from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
import time

from .base import embed_text, provider_identity
from .chroma_store import ChromaCandidateStore, ChromaSource
from .vector_store import VectorCandidate


_DOCUMENT_SUFFIXES = {".md", ".rst", ".txt"}


def repository_identity(path: str) -> str:
    import os
    from pathlib import Path
    canonical = str(Path(path).resolve(strict=True))
    return hashlib.sha256(os.path.normcase(canonical).encode("utf-8")).hexdigest()


def _chunk_key(path, start, end):
    return f"{path}:{start}-{end}"


@dataclass(frozen=True)
class ChromaIndexStats:
    repository_id: str
    model_id: str
    dimension: int
    sources: int
    embedded: int
    unchanged: int
    removed: int
    elapsed_seconds: float


class ChromaCodeIndex:
    """Mirror the current SQLite chunk snapshot into Chroma.

    SQLite remains authoritative. Stale Chroma rows are pruned only after every
    current source was read, hashed by the authoritative index, embedded and
    upserted successfully. A failed run may leave idempotent upserts, but does
    not delete records needed by the previous complete snapshot.
    """

    def __init__(self, index_store, collection, embedding_provider):
        self.index_store = index_store
        self.provider = embedding_provider
        self.model_id, self.dimension = provider_identity(embedding_provider)
        self.store = ChromaCandidateStore(collection, dimension=self.dimension)

    def _sources(self):
        for row in self.index_store.iter_chunks():
            path = row["relative_path"].replace("\\", "/")
            suffix = PurePosixPath(path).suffix.casefold()
            kind = "document" if suffix in _DOCUMENT_SUFFIXES else "code"
            source = ChromaSource(
                repository_id=self.repository_id, source_type=kind,
                chunk_id=_chunk_key(path, row["start_line"], row["end_line"]),
                file_path=path, content_hash=row["content_hash"], model_id=self.model_id,
                start_line=row["start_line"], end_line=row["end_line"],
            )
            yield source, row["text"]

    def _has_current(self, source):
        from .chroma_store import _identity
        existing = self.store.collection.get(ids=[_identity(source)], include=["metadatas"])
        metadata = existing.get("metadatas") or []
        if not metadata:
            return False
        row = metadata[0]
        return (row.get("repository_id") == source.repository_id
                and row.get("source_type") == source.source_type
                and row.get("chunk_id") == source.chunk_id
                and row.get("file_path") == source.file_path
                and row.get("content_hash") == source.content_hash
                and row.get("model_id") == source.model_id
                and row.get("start_line") == source.start_line
                and row.get("end_line") == source.end_line)

    def _repo_existing(self):
        where = {"$and": [{"repository_id": self.repository_id}, {"model_id": self.model_id}]}
        identities, metadata = [], []
        offset = 0
        while True:
            data = self.store.collection.get(where=where, limit=500, offset=offset,
                                             include=["metadatas"])
            identities.extend(data.get("ids", []))
            metadata.extend(data.get("metadatas", []))
            batch = data.get("ids", [])
            if len(batch) < 500:
                break
            offset += len(batch)
        return identities, metadata

    def sync(self, *, force=False) -> ChromaIndexStats:
        if not isinstance(force, bool):
            raise ValueError("force must be boolean.")
        repository = self.index_store.metadata().get("repository")
        if not repository:
            raise ValueError("No indexed repository is available for Chroma synchronization.")
        self.repository = repository
        self.repository_id = repository_identity(repository)
        started = time.monotonic()
        active = set()
        embedded = unchanged = 0
        for source, document in self._sources():
            from .chroma_store import _identity
            identity = _identity(source)
            active.add(identity)
            if not force and self._has_current(source):
                unchanged += 1
                continue
            vector = embed_text(self.provider, document, role="document")
            self.store.upsert(source, vector, document=document)
            embedded += 1
        old_ids, old_metadata = self._repo_existing()
        stale = [identity for identity, meta in zip(old_ids, old_metadata)
                 if meta.get("source_type") in ("code", "document") and identity not in active]
        if stale:
            self.store.collection.delete(ids=stale)
        return ChromaIndexStats(self.repository_id, self.model_id, self.dimension,
                                len(active), embedded, unchanged, len(stale),
                                round(time.monotonic() - started, 6))

    def search(self, query: str, *, top_k=10) -> list[VectorCandidate]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Chroma query must not be empty.")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ValueError("top_k must be from 1 to 100.")
        repository = self.index_store.metadata().get("repository")
        if not repository:
            raise ValueError("No indexed repository is available for Chroma retrieval.")
        repo_id = repository_identity(repository)
        query_vector = embed_text(self.provider, query, role="query")
        current = {}
        for row in self.index_store.iter_chunks():
            key = _chunk_key(row["relative_path"].replace("\\", "/"),
                             row["start_line"], row["end_line"])
            current[key] = row

        found = []
        for source_type in ("code", "document"):
            candidates = self.store.search_with_documents(query_vector,
                repository_id=repo_id, model_id=self.model_id, source_type=source_type,
                limit=top_k, verify_source=lambda source: self._verify_source(source, current))
            for source, distance, document in candidates:
                row = current[source.chunk_id]
                if document is None or document != row["text"]:
                    continue
                score = max(-1.0, min(1.0, 1.0 - distance))
                source_id = "F" + hashlib.sha256(source.file_path.encode("utf-8")).hexdigest()[:20]
                found.append(VectorCandidate(source_id, source.file_path, "chunk",
                    source.start_line, source.end_line, row["text"], source.content_hash,
                    self.model_id, None, score))
        found.sort(key=lambda item: (-item.score, item.relative_path.casefold(),
                                     item.start_line, item.end_line))
        unique = []
        seen = set()
        for item in found:
            key = (item.relative_path, item.start_line, item.end_line)
            if key not in seen:
                seen.add(key)
                unique.append(item)
            if len(unique) >= top_k:
                break
        return unique

    @staticmethod
    def _verify_source(source, current):
        if source.start_line is None or source.end_line is None:
            return False
        expected_key = _chunk_key(source.file_path, source.start_line, source.end_line)
        row = current.get(expected_key)
        if row is None:
            return False
        return (row["content_hash"] == source.content_hash
                and row["text"] is not None)
