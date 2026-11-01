"""Chroma candidate adapter for explicit local RAG and memory retrieval.

Callers must verify every candidate against authoritative, current source data.
This module does not execute models, manage migrations or synchronize outbox
records; those responsibilities belong to the embedding/index and memory layers.
"""
from dataclasses import dataclass
import hashlib
import math
from pathlib import PurePosixPath
import re

from .base import EmbeddingError, normalize_vector


@dataclass(frozen=True)
class ChromaSource:
    repository_id: str
    source_type: str
    chunk_id: str
    file_path: str
    content_hash: str
    model_id: str
    start_line: int | None = None
    end_line: int | None = None


def _metadata(source: ChromaSource) -> dict:
    for value in (source.repository_id, source.chunk_id, source.model_id):
        if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(x) < 32 for x in value):
            raise ValueError("Source identity is invalid.")
    if source.source_type not in ("code", "document", "memory"):
        raise ValueError("Source type is invalid.")
    if not isinstance(source.content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source.content_hash):
        raise ValueError("Content hash must be lowercase SHA-256.")
    if source.source_type != "memory":
        if not isinstance(source.file_path, str) or not source.file_path or "\\" in source.file_path:
            raise ValueError("File path must be a normalized repository-relative path.")
        path = PurePosixPath(source.file_path)
        if path.is_absolute() or ":" in source.file_path or any(x in ("", ".", "..") for x in source.file_path.split("/")):
            raise ValueError("File path must be a normalized repository-relative path.")
    elif source.file_path != "":
        raise ValueError("Memory sources do not have a file path.")
    if source.source_type == "memory":
        if source.start_line is not None or source.end_line is not None:
            raise ValueError("Memory records cannot have source line ranges.")
    elif ((source.start_line is None) != (source.end_line is None)
          or source.start_line is not None and (
              isinstance(source.start_line, bool) or not isinstance(source.start_line, int)
              or isinstance(source.end_line, bool) or not isinstance(source.end_line, int)
              or source.start_line < 1 or source.end_line < source.start_line)):
        raise ValueError("Source line range is invalid.")
    result = dict(repository_id=source.repository_id, source_type=source.source_type,
                  chunk_id=source.chunk_id, file_path=source.file_path,
                  content_hash=source.content_hash, model_id=source.model_id)
    if source.start_line is not None:
        result.update(start_line=source.start_line, end_line=source.end_line)
    return result


def _identity(source: ChromaSource) -> str:
    # Content hash intentionally excluded so updates replace the same record.
    fields = (source.repository_id, source.source_type, source.chunk_id, source.model_id)
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


class ChromaCandidateStore:
    """Operate on a caller-created, local collection with no embedding function."""
    def __init__(self, collection, *, dimension: int):
        if isinstance(dimension, bool) or not isinstance(dimension, int) or not 1 <= dimension <= 4096:
            raise ValueError("Invalid embedding dimension.")
        self.collection, self.dimension = collection, dimension

    def upsert(self, source: ChromaSource, vector, *, document: str | None = None) -> None:
        meta = _metadata(source)
        normalized = normalize_vector(vector, expected_dimension=self.dimension)
        kwargs = {"ids": [_identity(source)], "embeddings": [list(normalized)], "metadatas": [meta]}
        if document is not None:
            if not isinstance(document, str) or not document.strip() or len(document.encode("utf-8")) > 1_000_000:
                raise ValueError("Chroma source document must be non-empty and at most 1 MB.")
            kwargs["documents"] = [document]
        self.collection.upsert(**kwargs)

    def delete(self, source: ChromaSource) -> None:
        _metadata(source)
        self.collection.delete(ids=[_identity(source)])

    def search(self, vector, *, repository_id: str, model_id: str, verify_source,
               limit: int = 10, source_type: str | None = None):
        return [(source, distance) for source, distance, _document in self.search_with_documents(
            vector, repository_id=repository_id, model_id=model_id,
            verify_source=verify_source, limit=limit, source_type=source_type)]

    def search_with_documents(self, vector, *, repository_id: str, model_id: str,
                              verify_source, limit: int = 10, source_type: str | None = None):
        if not callable(verify_source):
            raise ValueError("Authoritative source verification is required.")
        if not isinstance(repository_id, str) or not repository_id or not isinstance(model_id, str) or not model_id:
            raise ValueError("Repository and model identity are required.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Invalid result limit.")
        if source_type not in (None, "code", "document", "memory"):
            raise ValueError("Invalid source type filter.")
        filters = [{"repository_id": repository_id}, {"model_id": model_id}]
        if source_type is not None:
            filters.append({"source_type": source_type})
        normalized = normalize_vector(vector, expected_dimension=self.dimension)
        result = self.collection.query(query_embeddings=[list(normalized)], n_results=limit,
            where={"$and": filters}, include=["metadatas", "distances", "documents"])
        ids, metas, distances = result["ids"][0], result["metadatas"][0], result["distances"][0]
        documents = result.get("documents")
        if documents is None:
            documents = [[None] * len(ids)]
        documents = documents[0]
        if len(ids) != len(metas) or len(ids) != len(distances) or len(ids) != len(documents):
            raise EmbeddingError("Malformed Chroma query response.")
        found = []
        for identity, meta, distance, document in zip(ids, metas, distances, documents):
            try:
                values = {key: meta[key] for key in (
                    "repository_id", "source_type", "chunk_id", "file_path", "content_hash", "model_id")}
                values.update(start_line=meta.get("start_line"), end_line=meta.get("end_line"))
                source = ChromaSource(**values)
                _metadata(source)
                if (source.repository_id != repository_id or source.model_id != model_id
                        or source_type is not None and source.source_type != source_type
                        or identity != _identity(source) or not math.isfinite(float(distance))):
                    raise ValueError
            except (KeyError, TypeError, ValueError, OverflowError):
                raise EmbeddingError("Invalid Chroma candidate identity or metadata.") from None
            if verify_source(source) is True:
                found.append((source, float(distance), document))
        return found


def delete_repository_vectors(client, repository_id: str) -> list[str]:
    """Delete only records tagged with one repository ID across local collections."""
    if not isinstance(repository_id, str) or not repository_id.strip() or len(repository_id) > 512:
        raise ValueError("Repository ID is invalid.")
    names = []
    for item in client.list_collections():
        name = item.name if hasattr(item, "name") else str(item)
        collection = client.get_collection(name=name)
        collection.delete(where={"repository_id": repository_id})
        names.append(name)
    return sorted(names)
