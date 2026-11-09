"""持久化 Chroma 索引，以及经过验证的代码/文档向量检索。"""
from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
import time

from .base import embed_text, normalize_vector, provider_identity
from .chroma_store import ChromaCandidateStore, ChromaSource
from .vector_store import VectorCandidate
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
            self.store.upsert(source, vector)
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

        def get_current(source):
            key = _chunk_key(source.file_path, source.start_line, source.end_line)
            if key not in current:
                current[key] = self.index_store.get_chunk(
                    source.file_path, source.start_line, source.end_line)
            return current[key]

        found = []
        for source_type in ("code", "document"):
            candidates = self.store.search_with_documents(query_vector,
                repository_id=repo_id, model_id=self.model_id, source_type=source_type,
                limit=top_k, verify_source=lambda source: self._verify_source(source, get_current),
                include_documents=False)
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
