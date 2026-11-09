"""Behavior tests for Chroma snapshot safety using isolated in-memory adapters."""

import hashlib
from pathlib import Path
import tempfile
import unittest

from agentforge.embeddings.chroma_index import ChromaCodeIndex
from agentforge.embeddings.chroma_store import ChromaCandidateStore, ChromaSource
from agentforge.embeddings.base import EmbeddingError
from agentforge.repository_identity import repository_identity
from tests.support import TMP_ROOT


class InMemoryCollection:
    def __init__(self):
        self.rows = {}
        self.get_offsets = []
        self.deleted_batches = []

    @staticmethod
    def _matches(metadata, where):
        if "$and" in where:
            return all(InMemoryCollection._matches(metadata, item) for item in where["$and"])
        return all(metadata.get(key) == value for key, value in where.items())

    def upsert(self, *, ids, embeddings, metadatas, documents=None):
        for position, identity in enumerate(ids):
            self.rows[identity] = {
                "metadata": dict(metadatas[position]),
                "embedding": list(embeddings[position]),
                "document": "" if documents is None else documents[position],
            }

    def get(self, *, ids=None, where=None, limit=None, offset=0, include=None):
        if where is not None and limit is not None:
            self.get_offsets.append(offset)
        if ids is not None:
            selected = [identity for identity in ids if identity in self.rows]
        else:
            selected = [identity for identity, row in self.rows.items()
                        if self._matches(row["metadata"], where or {})]
            selected = selected[offset:None if limit is None else offset + limit]
        result = {"ids": selected}
        include = include or []
        if "metadatas" in include:
            result["metadatas"] = [dict(self.rows[item]["metadata"]) for item in selected]
        if "documents" in include:
            result["documents"] = [self.rows[item]["document"] for item in selected]
        if "embeddings" in include:
            result["embeddings"] = [list(self.rows[item]["embedding"]) for item in selected]
        return result

    def update(self, *, ids, embeddings=None, documents=None):
        for position, identity in enumerate(ids):
            if embeddings is not None:
                self.rows[identity]["embedding"] = list(embeddings[position])
            if documents is not None:
                self.rows[identity]["document"] = documents[position]

    def delete(self, *, ids=None, where=None):
        batch = list(ids or ())
        self.deleted_batches.append(batch)
        for identity in batch:
            self.rows.pop(identity, None)


class SyntheticIndex:
    def __init__(self, repository, chunks=()):
        self.repository = str(repository) if repository else None
        self.chunks = list(chunks)

    def metadata(self):
        return {"repository": self.repository}

    def iter_chunks(self):
        return iter(self.chunks)

    def get_chunk(self, path, start, end):
        return next((row for row in self.chunks
                     if row["relative_path"] == path and row["start_line"] == start
                     and row["end_line"] == end), None)


class SyntheticEmbeddingProvider:
    model_id = "coverage-test-embedding"
    dimension = 2

    def __init__(self, *, fail=False):
        self.fail = fail
        self.document_calls = []
        self.query_calls = []

    def embed_document(self, text):
        self.document_calls.append(text)
        if self.fail:
            raise RuntimeError("synthetic embedding failure")
        return (1.0, 0.0)

    def embed_query(self, text):
        self.query_calls.append(text)
        return (1.0, 0.0)


class ChromaSnapshotSafetyTests(unittest.TestCase):
    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="agentforge-coverage-chroma-", dir=TMP_ROOT,
        )
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name) / "synthetic-repository"
        self.repository.mkdir()
        self.repository_id = repository_identity(self.repository)
        self.collection = InMemoryCollection()

    def _stale_source(self, number=0):
        path = f"old/{number:04}.py"
        return ChromaSource(
            self.repository_id, "code", f"{path}:1-1", path, "a" * 64,
            "coverage-test-embedding", 1, 1,
        )

    def _seed_stale(self, count=1):
        store = ChromaCandidateStore(self.collection, dimension=2)
        for number in range(count):
            store.upsert(self._stale_source(number), (1.0, 0.0))

    def _current_chunk(self):
        text = "def fresh(): return 'current snapshot'"
        return {
            "relative_path": "src/current.py", "start_line": 1, "end_line": 1,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text,
        }

    def test_failed_embedding_keeps_previous_chroma_snapshot_intact(self):
        self._seed_stale()
        stale_ids = set(self.collection.rows)
        provider = SyntheticEmbeddingProvider(fail=True)
        service = ChromaCodeIndex(SyntheticIndex(self.repository, [self._current_chunk()]),
                                  self.collection, provider)

        with self.assertRaisesRegex(EmbeddingError, "provider failed"):
            service.sync()

        self.assertEqual(stale_ids, set(self.collection.rows))
        self.assertEqual([], self.collection.deleted_batches)
        self.assertEqual([self._current_chunk()["text"]], provider.document_calls)

    def test_successful_snapshot_replacement_embeds_current_chunk_then_removes_stale_rows(self):
        self._seed_stale()
        provider = SyntheticEmbeddingProvider()
        service = ChromaCodeIndex(SyntheticIndex(self.repository, [self._current_chunk()]),
                                  self.collection, provider)

        stats = service.sync()

        self.assertEqual((1, 1, 1), (stats.sources, stats.embedded, stats.removed))
        self.assertEqual(1, len(self.collection.rows))
        current_metadata = next(iter(self.collection.rows.values()))["metadata"]
        self.assertEqual("src/current.py", current_metadata["file_path"])
        self.assertEqual("", next(iter(self.collection.rows.values()))["document"])
        self.assertEqual(1, len(self.collection.deleted_batches[0]))

    def test_large_repository_stale_cleanup_reads_every_page(self):
        self._seed_stale(501)
        service = ChromaCodeIndex(SyntheticIndex(self.repository), self.collection,
                                  SyntheticEmbeddingProvider())

        stats = service.sync()

        self.assertEqual(501, stats.removed)
        self.assertEqual([0, 500], self.collection.get_offsets)
        self.assertEqual(501, len(self.collection.deleted_batches[0]))
        self.assertEqual({}, self.collection.rows)

    def test_missing_repository_and_invalid_search_inputs_fail_before_embedding(self):
        provider = SyntheticEmbeddingProvider()
        service = ChromaCodeIndex(SyntheticIndex(None), self.collection, provider)
        with self.assertRaisesRegex(ValueError, "No indexed repository"):
            service.sync()
        for query, top_k in ((" ", 10), ("valid query", True)):
            with self.subTest(query=query, top_k=top_k), self.assertRaises(ValueError):
                service.search(query, top_k=top_k)
        with self.assertRaisesRegex(ValueError, "No indexed repository"):
            service.search("valid query")
        self.assertEqual([], provider.document_calls)
        self.assertEqual([], provider.query_calls)


if __name__ == "__main__":
    unittest.main()
