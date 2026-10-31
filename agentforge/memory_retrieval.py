"""Semantic retrieval of approved memory with PostgreSQL source verification."""
import hashlib

from .conversation_context import MemoryCandidate, MemorySourceReference
from .embeddings.base import embed_text, provider_identity
from .embeddings.chroma_store import ChromaCandidateStore


class ChromaMemoryRetriever:
    def __init__(self, collection, embedding_provider, memory_store):
        self.provider = embedding_provider
        self.model_id, self.dimension = provider_identity(embedding_provider)
        self.memory_store = memory_store
        self.vectors = ChromaCandidateStore(collection, dimension=self.dimension)

    def search(self, query, *, repository_id, limit=8):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Memory search query is required.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("Memory search limit must be from 1 to 50.")
        vector = embed_text(self.provider, query, role="query")
        active = {}

        def verify(source):
            if source.source_type != "memory" or source.file_path:
                return False
            record = self.memory_store.approved_by_id(source.chunk_id, repository_id)
            if record is None or record["repository_id"] != repository_id:
                return False
            digest = hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
            if digest != source.content_hash:
                return False
            sources = self.memory_store.source_records(record["id"], repository_id)
            if not sources:
                return False
            record["sources"] = tuple(MemorySourceReference(
                message_id=item["message_id"],
                source_sequence=item["source_sequence"],
                source_role=item["source_role"],
                source_hash=item["source_hash"],
            ) for item in sources)
            active[source.chunk_id] = record
            return True

        matches = self.vectors.search_with_documents(vector, repository_id=repository_id,
            model_id=self.model_id, source_type="memory", verify_source=verify, limit=limit)
        results = []
        for source, distance, document in matches:
            record = active.get(source.chunk_id)
            if record is None:
                continue
            expected = f"[{record['memory_type']}] {record['content']}"
            if document != expected:
                continue
            results.append((MemoryCandidate(
                repository_id=repository_id,
                content=record["content"],
                approved=True,
                memory_id=record["id"],
                memory_type=record["memory_type"],
                version=record["version"],
                sources=record["sources"],
            ), distance))
        results.sort(key=lambda pair: (pair[1], pair[0].content))
        return [item for item, _distance in results]
