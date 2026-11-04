"""本地持久化配置与显式的仅回环 Embedding 设置。"""
import hashlib
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / ".agentforge" / "data"
CHROMA_ROOT = DATA_ROOT / "chroma"


def chroma_path() -> Path:
    return CHROMA_ROOT


def embedding_provider_from_environment():
    from .embeddings.llama_server import LlamaServerEmbeddingProvider

    endpoint = os.environ.get("AGENTFORGE_EMBEDDING_BASE_URL")
    model_id = os.environ.get("AGENTFORGE_EMBEDDING_MODEL_ID")
    api_model = os.environ.get("AGENTFORGE_EMBEDDING_API_MODEL")
    raw_dimension = os.environ.get("AGENTFORGE_EMBEDDING_DIMENSION", "1024")
    if not endpoint or not model_id or not api_model:
        raise ValueError(
            "Set AGENTFORGE_EMBEDDING_BASE_URL, AGENTFORGE_EMBEDDING_MODEL_ID, "
            "and AGENTFORGE_EMBEDDING_API_MODEL for the local embedding service."
        )
    try:
        dimension = int(raw_dimension)
    except (TypeError, ValueError):
        raise ValueError("AGENTFORGE_EMBEDDING_DIMENSION must be an integer.") from None
    return LlamaServerEmbeddingProvider(endpoint, model_id=model_id,
        api_model=api_model, dimension=dimension)


def create_persistent_chroma(path=None):
    """创建本地持久化客户端；绝不重置或删除其数据目录。"""
    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError:
        raise RuntimeError("Chroma is unavailable; install the project's 'chroma' extra in its isolated environment.") from None
    target = Path(path) if path is not None else CHROMA_ROOT
    target.mkdir(parents=True, exist_ok=True)
    settings = Settings(anonymized_telemetry=False, allow_reset=False)
    return chromadb.PersistentClient(path=str(target.resolve()), settings=settings)


def collection_for_model(client, model_id: str, dimension: int):
    if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 512:
        raise ValueError("Embedding model identity is required.")
    if isinstance(dimension, bool) or not isinstance(dimension, int) or not 1 <= dimension <= 4096:
        raise ValueError("Embedding dimension is invalid.")
    suffix = hashlib.sha256(model_id.strip().encode("utf-8")).hexdigest()[:20]
    collection = client.get_or_create_collection(
        name="af012_vectors_" + suffix,
        embedding_function=None,
        metadata={"hnsw:space": "cosine", "agentforge_model_id": model_id.strip(),
                  "agentforge_dimension": str(dimension)},
        configuration={"hnsw": {"space": "cosine"}},
    )
    metadata = collection.metadata or {}
    stored_model = metadata.get("agentforge_model_id")
    stored_dimension = metadata.get("agentforge_dimension")
    if ((stored_model is not None and stored_model != model_id.strip())
            or (stored_dimension is not None and str(stored_dimension) != str(dimension))):
        raise ValueError("Existing Chroma collection model metadata does not match this embedding configuration.")
    return collection
