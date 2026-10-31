"""Small, replaceable embedding provider contract and output validation."""

from math import fsum, isfinite, sqrt
from numbers import Real
from typing import Protocol, Sequence

from ..config import MAX_VECTOR_DIMENSION


class EmbeddingError(ValueError):
    """An embedding provider returned invalid metadata or vector data."""


class EmbeddingProvider(Protocol):
    """Maps text to a numeric vector; providers must not execute returned data."""

    model_id: str
    dimension: int

    def embed(self, text: str) -> Sequence[float]:
        """Return one finite vector for text, or raise a clear provider error."""


class RoleAwareEmbeddingProvider(Protocol):
    """Optional contract for models with different query/document preprocessing."""

    model_id: str
    dimension: int

    def embed_query(self, text: str) -> Sequence[float]:
        """Embed a search query using the model's query-side preprocessing."""

    def embed_document(self, text: str) -> Sequence[float]:
        """Embed indexed content using the model's document-side preprocessing."""


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
    try:
        method_name = {"query": "embed_query", "document": "embed_document"}.get(role)
        method = getattr(provider, method_name, None) if method_name else None
        output = method(text) if callable(method) else provider.embed(text)
    except EmbeddingError as exc:
        # Project EmbeddingError messages are bounded and sanitized by each
        # provider; retain useful local HTTP status/timeout details while
        # continuing to suppress arbitrary exceptions from provider code.
        raise EmbeddingError(f"Embedding provider failed: {exc}") from None
    except Exception as exc:
        raise EmbeddingError(f"Embedding provider failed ({type(exc).__name__}).") from None
    return normalize_vector(output, expected_dimension=dimension)
