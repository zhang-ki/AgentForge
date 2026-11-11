"""持久化 Chroma 索引，以及经过验证的代码/文档向量检索。"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
from pathlib import Path
import time

from .base import (
    MAX_EMBEDDING_BATCH_ITEMS, embed_text, embed_texts, normalize_vector,
    provider_identity,
)
from .chroma_store import ChromaCandidateStore, ChromaSource, _identity, _metadata
from .vector_store import VectorCandidate
from ..observability import lifecycle_stage
from ..repository_identity import repository_identity


_DOCUMENT_SUFFIXES = {".md", ".rst", ".txt"}


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
    """将当前 SQLite 代码块快照同步到 Chroma。

    SQLite 仍是权威数据源。只有在每个当前来源都已
    读取、经权威索引计算哈希、生成嵌入并
    成功写入后才会清理过期 Chroma 记录。失败运行可能留下幂等写入项，但不会
    删除上一次完整快照仍需使用的记录。
    """

    def __init__(self, index_store, collection, embedding_provider):
        self.index_store = index_store
        self.provider = embedding_provider
        self.model_id, self.dimension = provider_identity(embedding_provider)
        self.store = ChromaCandidateStore(collection, dimension=self.dimension)
        self._consistency_snapshot = None
        self._consistency_result = None

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
        identity = _identity(source)
        existing = self.store.collection.get(ids=[identity], include=["metadatas", "documents"])
        metadata = existing.get("metadatas") or []
        if not metadata:
            return False
        row = metadata[0]
        metadata_matches = (row.get("repository_id") == source.repository_id
                and row.get("source_type") == source.source_type
                and row.get("chunk_id") == source.chunk_id
                and row.get("file_path") == source.file_path
                and row.get("content_hash") == source.content_hash
                and row.get("model_id") == source.model_id
                and row.get("start_line") == source.start_line
                and row.get("end_line") == source.end_line)
        if not metadata_matches:
            return False

        # 旧版 Chroma 记录可能仍有完整代码文本；用已有向量原位清空文本，不重新请求 Embedding。
        documents = existing.get("documents") or []
        if documents and documents[0] not in (None, ""):
            vector_result = self.store.collection.get(ids=[identity], include=["embeddings"])
            vectors = vector_result.get("embeddings")
            # Chroma returns a NumPy array here; do not use ``or []`` because
            # NumPy deliberately rejects truth-value checks for multi-item arrays.
            if vectors is None or len(vectors) != 1:
                return False
            vector = normalize_vector(vectors[0], expected_dimension=self.dimension)
            self.store.collection.update(ids=[identity], embeddings=[list(vector)], documents=[""])
        return True

    def _repo_existing(self, *, include_embeddings: bool = False,
                       include_documents: bool = False):
        where = {"$and": [{"repository_id": self.repository_id}, {"model_id": self.model_id}]}
        identities, metadata = [], []
        embeddings = []
        documents = []
        offset = 0
        include = ["metadatas"]
        if include_embeddings:
            include.append("embeddings")
        if include_documents:
            include.append("documents")
        while True:
            data = self.store.collection.get(where=where, limit=500, offset=offset,
                                             include=include)
            identities.extend(data.get("ids", []))
            batch_metadata = data.get("metadatas")
            if batch_metadata is None:
                batch_metadata = []
            metadata.extend(batch_metadata)
            if include_embeddings:
                batch_embeddings = data.get("embeddings")
                if batch_embeddings is None:
                    embeddings.extend([None] * len(data.get("ids", [])))
                else:
                    embeddings.extend(batch_embeddings)
            if include_documents:
                batch_documents = data.get("documents")
                if batch_documents is None:
                    documents.extend([None] * len(data.get("ids", [])))
                else:
                    documents.extend(batch_documents)
            batch = data.get("ids", [])
            if len(batch) < 500:
                break
            offset += len(batch)
        if include_embeddings and include_documents:
            return identities, metadata, embeddings, documents
        if include_embeddings:
            return identities, metadata, embeddings
        if include_documents:
            return identities, metadata, documents
        return identities, metadata

    def inspect_consistency(self) -> dict:
        """Compare Chroma code/document vectors with the authoritative SQLite snapshot.

        The check is read-only. It reports counts only, never source text or IDs.
        Memory records sharing the collection are deliberately outside this code-index check.
        """
        repository = self.index_store.metadata().get("repository")
        if not repository:
            return {
                "complete": False, "expected_vectors": 0, "stored_vectors": 0,
                "missing_vectors": 0, "stale_vectors": 0, "invalid_vectors": 0,
                "reason_code": "sqlite_index_not_initialized",
            }
        self.repository = repository
        self.repository_id = repository_identity(repository)
        cache_key = self._consistency_cache_key(repository)
        if (cache_key is not None and self._consistency_snapshot == cache_key
                and isinstance(self._consistency_result, dict)):
            return dict(self._consistency_result)

        from .chroma_store import _identity

        expected = {}
        for source, _document in self._sources():
            identity = _identity(source)
            expected[identity] = {
                "repository_id": source.repository_id,
                "source_type": source.source_type,
                "chunk_id": source.chunk_id,
                "file_path": source.file_path,
                "content_hash": source.content_hash,
                "model_id": source.model_id,
                "start_line": source.start_line,
                "end_line": source.end_line,
            }

        identities, metadata, embeddings, documents = self._repo_existing(
            include_embeddings=True, include_documents=True,
        )
        lengths_match = (len(identities) == len(metadata) == len(embeddings)
                         == len(documents))
        stored = {}
        invalid = 0 if lengths_match else 1
        for position, identity in enumerate(identities):
            row = metadata[position] if position < len(metadata) else None
            vector = embeddings[position] if position < len(embeddings) else None
            if not isinstance(row, dict):
                invalid += 1
                continue
            if row.get("source_type") not in ("code", "document"):
                continue
            stored[identity] = row
            try:
                normalize_vector(vector, expected_dimension=self.dimension)
            except (TypeError, ValueError, OverflowError):
                invalid += 1

        missing = sum(identity not in stored for identity in expected)
        stale = sum(
            identity not in expected or any(
                stored_metadata.get(key) != value
                for key, value in expected[identity].items()
            )
            for identity, stored_metadata in stored.items()
        )
        legacy_document_copies = sum(document not in (None, "") for document in documents)
        complete = (lengths_match and missing == 0 and stale == 0 and invalid == 0
                    and len(stored) == len(expected) and legacy_document_copies == 0)
        result = {
            "complete": complete,
            "expected_vectors": len(expected),
            "stored_vectors": len(stored),
            "missing_vectors": missing,
            "stale_vectors": stale,
            "invalid_vectors": invalid,
            "legacy_document_copies": legacy_document_copies,
            "reason_code": None if complete else "vector_snapshot_mismatch",
        }
        if complete:
            if cache_key is not None:
                self._consistency_snapshot = cache_key
                self._consistency_result = dict(result)
        return result

    def _consistency_cache_key(self, repository: str) -> tuple | None:
        metadata = self.index_store.metadata()
        snapshot_reader = getattr(self.index_store, "file_snapshot", None)
        if not callable(snapshot_reader):
            # Adapters without an authoritative SQLite snapshot are usable,
            # but their integrity result must not be cached.
            return None
        database = getattr(self.index_store, "database", None)
        try:
            database_key = str(Path(database).resolve())
        except (TypeError, OSError, RuntimeError, ValueError):
            database_key = str(database)
        snapshot = snapshot_reader()
        digest = hashlib.sha256(json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        collection = self.store.collection
        count_method = getattr(collection, "count", None)
        if not callable(count_method):
            # Lightweight test adapters and older collection wrappers may not
            # expose count. In that case we deliberately disable the cache.
            return None
        count = count_method()
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("Vector collection size is invalid.")
        return (
            database_key, self.repository_id, repository, metadata.get("indexed_at"),
            metadata.get("chunk_signature"), metadata.get("parser_version"),
            self.model_id, self.dimension, getattr(collection, "name", None), digest, count,
        )

    def sync(self, *, force=False) -> ChromaIndexStats:
        if not isinstance(force, bool):
            raise ValueError("force must be boolean.")
        repository = self.index_store.metadata().get("repository")
        if not repository:
            raise ValueError("No indexed repository is available for Chroma synchronization.")
        self.repository = repository
        self.repository_id = repository_identity(repository)
        self._consistency_snapshot = None
        self._consistency_result = None
        started = time.monotonic()
        sources = list(self._sources())
        active = {_identity(source) for source, _document in sources}
        existing_ids, existing_metadata, existing_embeddings, existing_documents = self._repo_existing(
            include_embeddings=True, include_documents=True
        )
        existing = {}
        for position, identity in enumerate(existing_ids):
            existing[identity] = (
                existing_metadata[position] if position < len(existing_metadata) else None,
                existing_embeddings[position] if position < len(existing_embeddings) else None,
                existing_documents[position] if position < len(existing_documents) else None,
            )

        pending = []
        unchanged = 0
        for source, document in sources:
            identity = _identity(source)
            current = existing.get(identity)
            metadata = current[0] if current else None
            vector = current[1] if current else None
            document_copy = current[2] if current else None
            metadata_matches = isinstance(metadata, dict) and metadata == _metadata(source)
            try:
                normalized = normalize_vector(vector, expected_dimension=self.dimension)
            except (TypeError, ValueError, OverflowError):
                normalized = None
            if not force and metadata_matches and normalized is not None:
                # Remove legacy plaintext copies without re-embedding the source.
                if document_copy not in (None, ""):
                    self.store.collection.update(
                        ids=[identity], embeddings=[list(normalized)], documents=[""]
                    )
                unchanged += 1
                continue
            pending.append((source, document))

        embedded = 0
        for start in range(0, len(pending), MAX_EMBEDDING_BATCH_ITEMS):
            batch = pending[start:start + MAX_EMBEDDING_BATCH_ITEMS]
            vectors = embed_texts(
                self.provider, [document for _source, document in batch], role="document"
            )
            self.store.upsert_many(
                [source for source, _document in batch], vectors
            )
            embedded += len(batch)
        stale = [identity for identity, meta in zip(existing_ids, existing_metadata)
                 if isinstance(meta, dict)
                 and meta.get("source_type") in ("code", "document") and identity not in active]
        if stale:
            self.store.collection.delete(ids=stale)
        cache_key = self._consistency_cache_key(repository)
        if cache_key is not None:
            self._consistency_snapshot = cache_key
            self._consistency_result = {
            "complete": True,
            "expected_vectors": len(active),
            "stored_vectors": len(active),
            "missing_vectors": 0,
            "stale_vectors": 0,
            "invalid_vectors": 0,
            "legacy_document_copies": 0,
            "reason_code": None,
            }
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
        with lifecycle_stage("repository_embedding", metadata={"role": "query"}):
            query_vector = embed_text(self.provider, query, role="query")
        current = {}

        def get_current(source):
            key = _chunk_key(source.file_path, source.start_line, source.end_line)
            if key not in current:
                current[key] = self.index_store.get_chunk(
                    source.file_path, source.start_line, source.end_line)
            return current[key]

        found = []
        for source_type in ("code", "document"):
            with lifecycle_stage("chroma_query", metadata={"role": source_type}) as query_stage:
                candidates = self.store.search_with_documents(query_vector,
                    repository_id=repo_id, model_id=self.model_id, source_type=source_type,
                    limit=top_k, verify_source=lambda source: self._verify_source(source, get_current),
                    include_documents=False)
                if query_stage is not None:
                    query_stage.finish("COMPLETED", metadata={"vector_result_count": len(candidates)})
            for source, distance, _document in candidates:
                row = get_current(source)
                if row is None:
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
    def _verify_source(source, get_current):
        if source.start_line is None or source.end_line is None:
            return False
        row = get_current(source)
        if row is None:
            return False
        return (row["relative_path"] == source.file_path
                and row["start_line"] == source.start_line
                and row["end_line"] == source.end_line
                and row["content_hash"] == source.content_hash
                and row["text"] is not None)
