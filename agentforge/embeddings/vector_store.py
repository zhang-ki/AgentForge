"""有界的 SQLite 向量持久化和确定性余弦相似度候选搜索。"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
from pathlib import PurePosixPath
import sqlite3
import struct

from ..config import MAX_VECTOR_BYTES, MAX_VECTOR_DIMENSION, MAX_VECTOR_RECORDS, MAX_VECTOR_TOP_K, VECTOR_INDEX_VERSION
from ..errors import StoreError
from .base import EmbeddingError, normalize_vector, provider_identity


def _source_id(relative_path: str) -> str:
    return "F" + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]


def _normal_path(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Vector source path must be a repository-relative string.")
    path = value.replace("\\", "/")
    parts = path.split("/")
    if not path or path.startswith("/") or ":" in path or any(part in ("", ".", "..") for part in parts):
        raise ValueError("Vector source path must be a normalized repository-relative path.")
    return PurePosixPath(path).as_posix()


def _model_id(value: str) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(ord(char) < 32 for char in value)):
        raise ValueError("model_id must be a non-empty, bounded string without control characters.")
    return value.strip()


def cosine_similarity(left, right) -> float:
    """返回 dot(a,b)/(||a||*||b||)；零向量的相似度定义为 0。"""
    a = normalize_vector(left, allow_zero=True)
    b = normalize_vector(right, expected_dimension=len(a), allow_zero=True)
    if not any(a) or not any(b):
        return 0.0
    score = math.fsum(x * y for x, y in zip(a, b))
    # 将浮点舍入误差限制在数学定义的余弦范围内。
    return max(-1.0, min(1.0, score))


@dataclass(frozen=True)
class VectorCandidate:
    source_id: str
    relative_path: str
    source_kind: str
    start_line: int
    end_line: int
    text: str
    content_hash: str
    model_id: str
    symbol_id: str | None
    score: float

    @property
    def citation(self) -> str:
        return f"{self.relative_path}:{self.start_line}-{self.end_line}"


class VectorStore:
    """向量记录与当前项目共用 SQLite 事务和资源限制。"""

    def __init__(self, index_store):
        self.index_store = index_store
        if index_store.schema_version != VECTOR_INDEX_VERSION:
            raise StoreError("Vector search requires index Schema v3; use an explicitly writable index to migrate.")

    @property
    def connection(self):
        return self.index_store.connection

    def _check_dimension(self, model_id: str, dimension: int) -> None:
        row = self.connection.execute(
            "SELECT dimension FROM vector_embeddings WHERE model_id = ? LIMIT 1", (model_id,)
        ).fetchone()
        if row is not None and row["dimension"] != dimension:
            raise EmbeddingError(
                f"Model {model_id!r} already has stored dimension {row['dimension']}, not {dimension}."
            )

    def _upsert(
        self, *, model_id: str, source_kind: str, file_id: int, source_id: str,
        content_hash: str, vector: tuple[float, ...], chunk_id: int | None = None,
        symbol_id: str | None = None, relative_path: str, source_key: str,
    ) -> bool:
        model_id = _model_id(model_id)
        if not content_hash:
            raise ValueError("content_hash must not be empty.")
        dimension = len(vector)
        if not 1 <= dimension <= MAX_VECTOR_DIMENSION:
            raise EmbeddingError(f"Embedding dimension must be from 1 to {MAX_VECTOR_DIMENSION}.")
        self._check_dimension(model_id, dimension)
        try:
            blob = struct.pack(f"<{dimension}f", *vector)
        except (OverflowError, struct.error):
            raise EmbeddingError("Embedding values cannot be represented by the local vector format.") from None
        key_material = "\0".join((model_id, source_kind, relative_path, source_key, content_hash))
        embedding_id = "V" + hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:40]
        unique_column = "chunk_id" if source_kind == "chunk" else "symbol_id"
        unique_value = chunk_id if source_kind == "chunk" else symbol_id
        old = self.connection.execute(
            f"SELECT embedding_id, content_hash, vector, dimension FROM vector_embeddings "
            f"WHERE model_id = ? AND {unique_column} = ?",
            (model_id, unique_value),
        ).fetchone()
        if old is not None and (
            old["embedding_id"] == embedding_id and old["content_hash"] == content_hash
            and old["dimension"] == dimension and old["vector"] == blob
        ):
            return False
        replacing = old is not None
        current_count = self.connection.execute(
            "SELECT COUNT(*) FROM vector_embeddings"
        ).fetchone()[0]
        current_bytes = self.connection.execute(
            "SELECT COALESCE(SUM(length(vector)), 0) FROM vector_embeddings"
        ).fetchone()[0]
        old_bytes = 0 if old is None else len(old["vector"])
        if not replacing and current_count >= MAX_VECTOR_RECORDS:
            raise EmbeddingError(f"Vector record limit ({MAX_VECTOR_RECORDS}) exceeded.")
        if current_bytes - old_bytes + len(blob) > MAX_VECTOR_BYTES:
            raise EmbeddingError(f"Vector storage byte limit ({MAX_VECTOR_BYTES}) exceeded.")
        updated_at = datetime.now(timezone.utc).isoformat()
        if old is None:
            self.connection.execute(
                "INSERT INTO vector_embeddings(embedding_id, model_id, source_kind, file_id, source_id, "
                "chunk_id, symbol_id, content_hash, dimension, vector, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (embedding_id, model_id, source_kind, file_id, source_id, chunk_id, symbol_id,
                 content_hash, dimension, blob, updated_at),
            )
        else:
            self.connection.execute(
                f"UPDATE vector_embeddings SET embedding_id = ?, file_id = ?, source_id = ?, "
                f"chunk_id = ?, symbol_id = ?, content_hash = ?, dimension = ?, vector = ?, updated_at = ? "
                f"WHERE model_id = ? AND {unique_column} = ?",
                (embedding_id, file_id, source_id, chunk_id, symbol_id, content_hash,
                 dimension, blob, updated_at, model_id, unique_value),
            )
        return True

    def upsert_chunk(
        self, *, model_id: str, relative_path: str, start_line: int, end_line: int,
        content_hash: str, vector,
    ) -> bool:
        path = _normal_path(relative_path)
        normalized = normalize_vector(vector)
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise ValueError("Chunk start_line must be a positive integer.")
        if isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < start_line:
            raise ValueError("Chunk end_line must be an integer not less than start_line.")
        row = self.connection.execute(
            "SELECT files.id AS file_id, chunks.id AS chunk_id, chunks.content_hash "
            "FROM files JOIN chunks ON chunks.file_id = files.id "
            "WHERE files.relative_path = ? AND chunks.start_line = ? AND chunks.end_line = ?",
            (path, start_line, end_line),
        ).fetchone()
        if row is None or row["content_hash"] != content_hash:
            raise ValueError("Chunk vector source is absent or stale in the current index.")
        return self._upsert(
            model_id=model_id, source_kind="chunk", file_id=row["file_id"],
            source_id=_source_id(path), content_hash=content_hash, vector=normalized,
            chunk_id=row["chunk_id"], relative_path=path,
            source_key=f"{start_line}:{end_line}",
        )

    def upsert_symbol(
        self, *, model_id: str, relative_path: str, symbol_id: str,
        content_hash: str, vector,
    ) -> bool:
        path = _normal_path(relative_path)
        if not isinstance(symbol_id, str) or not symbol_id:
            raise ValueError("symbol_id must not be empty.")
        normalized = normalize_vector(vector)
        row = self.connection.execute(
            "SELECT symbols.symbol_id, symbols.file_id, symbols.source_id, symbols.structure_hash "
            "FROM symbols JOIN files ON files.id = symbols.file_id "
            "WHERE files.relative_path = ? AND symbols.symbol_id = ?",
            (path, symbol_id),
        ).fetchone()
        if row is None or row["structure_hash"] != content_hash:
            raise ValueError("Symbol vector source is absent or stale in the current index.")
        return self._upsert(
            model_id=model_id, source_kind="symbol", file_id=row["file_id"],
            source_id=row["source_id"], content_hash=content_hash, vector=normalized,
            symbol_id=symbol_id, relative_path=path, source_key=symbol_id,
        )

    def remove_model(self, model_id: str) -> int:
        model_id = _model_id(model_id)
        cursor = self.connection.execute(
            "DELETE FROM vector_embeddings WHERE model_id = ?", (model_id,)
        )
        return cursor.rowcount

    def search(self, query_vector, *, model_id: str, top_k: int = 5) -> list[VectorCandidate]:
        model_id = _model_id(model_id)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_VECTOR_TOP_K:
            raise ValueError(f"top_k must be from 1 to {MAX_VECTOR_TOP_K}.")
        query = normalize_vector(query_vector, allow_zero=True)
        if not any(query):
            return []
        rows = self.connection.execute(
            "SELECT vectors.embedding_id, vectors.source_id, vectors.source_kind, vectors.content_hash, "
            "vectors.dimension, vectors.vector, files.relative_path, chunks.start_line AS chunk_start, "
            "chunks.end_line AS chunk_end, chunks.text AS chunk_text, symbols.symbol_id, "
            "symbols.start_line AS symbol_start, symbols.end_line AS symbol_end, symbols.kind, "
            "symbols.qualified_name, symbols.signature, symbols.docstring "
            "FROM vector_embeddings AS vectors "
            "JOIN files ON files.id = vectors.file_id "
            "LEFT JOIN chunks ON chunks.id = vectors.chunk_id "
            "LEFT JOIN symbols ON symbols.symbol_id = vectors.symbol_id "
            " AND symbols.file_id = vectors.file_id "
            "WHERE vectors.model_id = ? AND ("
            "(vectors.source_kind = 'chunk' AND chunks.id IS NOT NULL "
            " AND chunks.content_hash = vectors.content_hash) OR "
            "(vectors.source_kind = 'symbol' AND symbols.symbol_id IS NOT NULL "
            " AND symbols.structure_hash = vectors.content_hash))",
            (model_id,),
        )
        candidates = []
        for row in rows:
            if row["dimension"] != len(query) or len(row["vector"]) != row["dimension"] * 4:
                raise EmbeddingError("Stored vector dimension or byte length is inconsistent.")
            try:
                stored = struct.unpack(f"<{row['dimension']}f", row["vector"])
                stored = normalize_vector(stored)
            except (struct.error, EmbeddingError):
                raise EmbeddingError("Stored vector record is invalid.") from None
            score = cosine_similarity(query, stored)
            if row["source_kind"] == "chunk":
                start, end, text = row["chunk_start"], row["chunk_end"], row["chunk_text"]
                symbol_id = None
            else:
                start, end = row["symbol_start"], row["symbol_end"]
                symbol_id = row["symbol_id"]
                text = "\n".join(part for part in (
                    f"{row['kind']} {row['qualified_name']}", row["signature"], row["docstring"]
                ) if part)
            candidates.append(VectorCandidate(
                source_id=row["source_id"], relative_path=row["relative_path"],
                source_kind=row["source_kind"], start_line=start, end_line=end,
                text=text, content_hash=row["content_hash"], model_id=model_id,
                symbol_id=symbol_id, score=score,
            ))
        candidates.sort(key=lambda item: (
            -item.score, item.relative_path.casefold(), item.start_line, item.end_line,
            item.source_kind, item.symbol_id or "",
        ))
        unique, seen = [], set()
        for item in candidates:
            key = (item.source_kind, item.relative_path, item.start_line, item.end_line, item.symbol_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
            if len(unique) == top_k:
                break
        return unique
