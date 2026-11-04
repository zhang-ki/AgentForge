"""与 Provider 无关的 Embedding 接口和本地向量候选接口。"""

from .base import (
    EmbeddingError,
    EmbeddingProvider,
    RoleAwareEmbeddingProvider,
    embed_text,
    normalize_vector,
)
from .retriever import VectorCandidate, VectorCandidateRetriever
from .llama_server import LlamaServerEmbeddingProvider
from .vector_store import VectorStore, cosine_similarity

__all__ = [
    "EmbeddingError", "EmbeddingProvider", "RoleAwareEmbeddingProvider",
    "LlamaServerEmbeddingProvider",
    "VectorCandidate", "VectorCandidateRetriever",
    "VectorStore", "cosine_similarity", "embed_text", "normalize_vector",
]
