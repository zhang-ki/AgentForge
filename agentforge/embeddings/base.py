"""可替换的轻量 Embedding Provider 合同与输出校验。"""

from math import fsum, isfinite, sqrt
from numbers import Real
from typing import Protocol, Sequence

from ..config import MAX_VECTOR_DIMENSION

MAX_EMBEDDING_BATCH_ITEMS = 16


class EmbeddingError(ValueError):
    """Embedding Provider 返回了无效元数据或向量数据。"""


class EmbeddingProvider(Protocol):
    """将文本映射为数值向量；Provider 不得执行返回数据。"""

    model_id: str
    dimension: int

    def embed(self, text: str) -> Sequence[float]:
        """为文本返回一个有限值向量；否则抛出明确的 Provider 错误。"""


class RoleAwareEmbeddingProvider(Protocol):
    """供查询与文档预处理方式不同的模型实现的可选接口。"""

    model_id: str
    dimension: int

    def embed_query(self, text: str) -> Sequence[float]:
        """使用模型的查询侧预处理为搜索问题生成向量。"""

    def embed_document(self, text: str) -> Sequence[float]:
        """使用模型的文档侧预处理为已索引内容生成向量。"""


def provider_identity(provider: EmbeddingProvider) -> tuple[str, int]:
    model_id = getattr(provider, "model_id", None)
    dimension = getattr(provider, "dimension", None)
    if (not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 512
            or any(ord(char) < 32 for char in model_id)):
        raise EmbeddingError("Embedding provider model_id must be a non-empty, bounded string.")
    if (isinstance(dimension, bool) or not isinstance(dimension, int)
            or not 1 <= dimension <= MAX_VECTOR_DIMENSION):
        raise EmbeddingError(
            f"Embedding dimension must be an integer from 1 to {MAX_VECTOR_DIMENSION}."
        )
    return model_id.strip(), dimension


def normalize_vector(
    vector, *, expected_dimension: int | None = None, allow_zero: bool = False,
) -> tuple[float, ...]:
    if isinstance(vector, (str, bytes, bytearray)):
        raise EmbeddingError("Embedding output must be a non-empty sequence of numbers.")
    try:
        values = tuple(vector)
    except Exception:
        raise EmbeddingError("Embedding output must be a non-empty sequence of numbers.") from None
    if not values:
        raise EmbeddingError("Embedding output cannot be empty.")
    if len(values) > MAX_VECTOR_DIMENSION:
        raise EmbeddingError(f"Embedding dimension exceeds {MAX_VECTOR_DIMENSION}.")
    if expected_dimension is not None and len(values) != expected_dimension:
        raise EmbeddingError(
            f"Embedding dimension mismatch: expected {expected_dimension}, received {len(values)}."
        )
    checked = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise EmbeddingError("Embedding values must be real numbers.")
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            raise EmbeddingError("Embedding values must be finite real numbers.") from None
        if not isfinite(number):
            raise EmbeddingError("Embedding values must be finite.")
        checked.append(number)
    try:
        norm = sqrt(fsum(value * value for value in checked))
    except (OverflowError, ValueError):
        raise EmbeddingError("Embedding vector norm is not finite.") from None
    if not isfinite(norm):
        raise EmbeddingError("Embedding vector norm is not finite.")
    if norm == 0.0:
        if allow_zero:
            return tuple(0.0 for _ in checked)
        raise EmbeddingError("Zero vectors cannot be stored as embeddings.")
    return tuple(value / norm for value in checked)


def embed_text(
    provider: EmbeddingProvider, text: str, *, role: str | None = None,
) -> tuple[float, ...]:
    if not isinstance(text, str) or not text.strip():
        raise EmbeddingError("Text to embed must not be empty.")
    if role not in (None, "query", "document"):
        raise EmbeddingError("Embedding role must be 'query', 'document', or None.")
    _, dimension = provider_identity(provider)
    from ..observability import lifecycle_stage
    counts = getattr(provider, "request_counts", None)
    before = dict(counts) if isinstance(counts, dict) else {}
    with lifecycle_stage("embedding_request", metadata={
        "provider": type(provider).__name__, "model": getattr(provider, "model_id", None),
        "role": role or "unspecified",
    }, console=role != "document") as embedding_stage:
        try:
            method_name = {"query": "embed_query", "document": "embed_document"}.get(role)
            method = getattr(provider, method_name, None) if method_name else None
            output = method(text) if callable(method) else provider.embed(text)
            vector = normalize_vector(output, expected_dimension=dimension)
        except EmbeddingError as exc:
            if embedding_stage is not None:
                embedding_stage.finish("DEGRADED", error_code="embedding_unavailable",
                                       metadata={"error_type": type(exc).__name__})
            # Provider error text is already bounded by its contract. It is not written to lifecycle logs.
            raise EmbeddingError(f"Embedding provider failed: {exc}") from None
        except Exception as exc:
            if embedding_stage is not None:
                embedding_stage.finish("DEGRADED", error_code="embedding_unavailable",
                                       metadata={"error_type": type(exc).__name__})
            raise EmbeddingError(f"Embedding provider failed ({type(exc).__name__}).") from None
        after = getattr(provider, "request_counts", None)
        if embedding_stage is not None:
            current = dict(after) if isinstance(after, dict) else {}
            embedding_stage.finish("COMPLETED", metadata={
                "embedding_attempts": _counter_delta(before, current, "attempts"),
                "embedding_sent": _counter_delta(before, current, "sent"),
                "embedding_successes": _counter_delta(before, current, "successes"),
                "embedding_failures": _counter_delta(before, current, "failures"),
            })
        return vector


def embed_texts(
    provider: EmbeddingProvider, texts: Sequence[str], *, role: str = "document",
) -> tuple[tuple[float, ...], ...]:
    """Embed a bounded document batch when the provider explicitly supports it.

    Providers without a batch API retain the existing single-item behavior. The
    batch path validates every vector before returning any of them to a caller.
    """
    if role != "document":
        raise EmbeddingError("Batch embeddings are currently limited to document inputs.")
    if isinstance(texts, (str, bytes, bytearray)):
        raise EmbeddingError("Embedding batch must be a sequence of non-empty texts.")
    try:
        values = tuple(texts)
    except Exception:
        raise EmbeddingError("Embedding batch must be a sequence of non-empty texts.") from None
    if not 1 <= len(values) <= MAX_EMBEDDING_BATCH_ITEMS:
        raise EmbeddingError(
            f"Embedding batches must contain 1 to {MAX_EMBEDDING_BATCH_ITEMS} texts."
        )
    if any(not isinstance(text, str) or not text.strip() for text in values):
        raise EmbeddingError("Embedding batch contains an empty text.")
    _, dimension = provider_identity(provider)
    method = getattr(provider, "embed_documents", None)
    if not callable(method):
        return tuple(embed_text(provider, text, role=role) for text in values)

    from ..observability import lifecycle_stage
    counts = getattr(provider, "request_counts", None)
    before = dict(counts) if isinstance(counts, dict) else {}
    with lifecycle_stage("embedding_request", metadata={
        "provider": type(provider).__name__, "model": getattr(provider, "model_id", None),
        "role": role, "embedding_item_count": len(values),
    }, console=False) as embedding_stage:
        try:
            outputs = method(values)
            if isinstance(outputs, (str, bytes, bytearray)):
                raise EmbeddingError("Embedding provider returned an invalid batch.")
            outputs = tuple(outputs)
            if len(outputs) != len(values):
                raise EmbeddingError("Embedding provider returned an incomplete batch.")
            vectors = tuple(normalize_vector(value, expected_dimension=dimension)
                            for value in outputs)
        except EmbeddingError as exc:
            if embedding_stage is not None:
                embedding_stage.finish("DEGRADED", error_code="embedding_unavailable",
                                       metadata={"error_type": type(exc).__name__})
            raise EmbeddingError(f"Embedding provider failed: {exc}") from None
        except Exception as exc:
            if embedding_stage is not None:
                embedding_stage.finish("DEGRADED", error_code="embedding_unavailable",
                                       metadata={"error_type": type(exc).__name__})
            raise EmbeddingError(
                f"Embedding provider failed ({type(exc).__name__})."
            ) from None
        after = getattr(provider, "request_counts", None)
        if embedding_stage is not None:
            current = dict(after) if isinstance(after, dict) else {}
            embedding_stage.finish("COMPLETED", metadata={
                "embedding_attempts": _counter_delta(before, current, "attempts"),
                "embedding_sent": _counter_delta(before, current, "sent"),
                "embedding_successes": _counter_delta(before, current, "successes"),
                "embedding_failures": _counter_delta(before, current, "failures"),
            })
        return vectors


def _counter_delta(before, after, key):
    first, last = before.get(key), after.get(key)
    if isinstance(first, int) and isinstance(last, int) and last >= first:
        return last - first
    return None
