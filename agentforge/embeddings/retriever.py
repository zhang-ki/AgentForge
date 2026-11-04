"""基于当前 SQLite 数据的显式 Embedding 索引与向量候选检索。"""

from ..config import MAX_VECTOR_RECORDS, MAX_VECTOR_TOP_K, STRUCTURED_INDEX_VERSION, VECTOR_INDEX_VERSION
from ..errors import StoreError
from .base import embed_text, provider_identity
from .vector_store import VectorCandidate, VectorStore


class VectorCandidateRetriever:
    """可选的向量检索路径；词法和符号检索不依赖它。"""

    def __init__(self, index_store, embedding_provider):
        self.index_store = index_store
        self.provider = embedding_provider
        self.model_id, self.dimension = provider_identity(embedding_provider)
        self.vector_store = VectorStore(index_store)

    def index_current_index(self, *, include_symbols: bool = True) -> dict:
        if not isinstance(include_symbols, bool):
            raise ValueError("include_symbols must be boolean.")
        if include_symbols and self.index_store.schema_version < STRUCTURED_INDEX_VERSION:
            raise StoreError("Symbol vectors require a structured index; run an explicit repository index first.")
        chunk_count = self.index_store.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        symbol_count = (
            self.index_store.connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            if include_symbols else 0
        )
        total = chunk_count + symbol_count
        if total > MAX_VECTOR_RECORDS:
            raise ValueError(
                f"Vector source limit ({MAX_VECTOR_RECORDS}) exceeded; no embeddings were generated."
            )
        changed = unchanged = 0
        with self.index_store.connection:
            for row in self.index_store.iter_chunks():
                did_change = self.vector_store.upsert_chunk(
                    model_id=self.model_id,
                    relative_path=row["relative_path"],
                    start_line=row["start_line"], end_line=row["end_line"],
                    content_hash=row["content_hash"],
                    vector=embed_text(self.provider, row["text"], role="document"),
                )
                changed += int(did_change)
                unchanged += int(not did_change)
            if include_symbols:
                for row in self.index_store.iter_symbols():
                    text = "\n".join(part for part in (
                        f"{row['kind']} {row['qualified_name']}",
                        row["signature"], row["docstring"],
                    ) if part)
                    did_change = self.vector_store.upsert_symbol(
                        model_id=self.model_id,
                        relative_path=row["relative_path"], symbol_id=row["symbol_id"],
                        content_hash=row["structure_hash"],
                        vector=embed_text(self.provider, text, role="document"),
                    )
                    changed += int(did_change)
                    unchanged += int(not did_change)
        return {
            "model_id": self.model_id,
            "dimension": self.dimension,
            "sources_considered": total,
            "changed": changed,
            "unchanged": unchanged,
            "vectors": self.index_store.connection.execute(
                "SELECT COUNT(*) FROM vector_embeddings WHERE model_id = ?", (self.model_id,)
            ).fetchone()[0],
        }

    def search(self, query: str, *, top_k: int = 5) -> list[VectorCandidate]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Vector query must not be empty.")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_VECTOR_TOP_K:
            raise ValueError(f"top_k must be from 1 to {MAX_VECTOR_TOP_K}.")
        query_vector = embed_text(self.provider, query, role="query")
        return self.vector_store.search(query_vector, model_id=self.model_id, top_k=top_k)
