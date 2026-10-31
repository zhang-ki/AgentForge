import math
from pathlib import Path
import unittest
from unittest.mock import patch

from agentforge.embeddings import (
    EmbeddingError,
    VectorCandidateRetriever,
    VectorStore,
    cosine_similarity,
    embed_text,
    normalize_vector,
)
from agentforge.indexer import index_repository
from agentforge.retriever import search
from agentforge.store import IndexStore
from tests.support import LocalTestCase


class FixedEmbeddingProvider:
    model_id = "synthetic-fixed-3d-v1"
    dimension = 3

    def embed(self, text):
        lowered = text.casefold()
        if "car" in lowered or "vehicle" in lowered:
            return (0.0, 1.0, 0.0)
        if "theme" in lowered or "appearance" in lowered:
            return (0.0, 0.0, 1.0)
        return (1.0, 0.0, 0.0)


class BrokenEmbeddingProvider(FixedEmbeddingProvider):
    def embed(self, text):
        raise RuntimeError("synthetic offline failure")


class ExplicitEmbeddingFailureProvider(FixedEmbeddingProvider):
    def embed_query(self, text):
        raise EmbeddingError("Local embedding server returned HTTP 400.")


class InvalidEmbeddingProvider(FixedEmbeddingProvider):
    def __init__(self, output, dimension=3):
        self.output = output
        self.dimension = dimension

    def embed(self, text):
        return self.output


class RoleAwareTestProvider:
    model_id = "synthetic-role-aware-v1"
    dimension = 3

    def __init__(self):
        self.calls = []

    def embed(self, text):
        raise AssertionError("role-aware methods should be selected")

    def embed_document(self, text):
        self.calls.append(("document", text))
        return (1.0, 0.0, 0.0)

    def embed_query(self, text):
        self.calls.append(("query", text))
        return (1.0, 0.0, 0.0)


class EmbeddingTests(LocalTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/apple.py", '"""apple fruit source"""\n')
        self.write("src/car.py", '"""car vehicle source"""\n')
        self.store = IndexStore(":memory:")
        self.addCleanup(self.store.close)
        index_repository(self.root, self.store)
        self.provider = FixedEmbeddingProvider()
        self.retriever = VectorCandidateRetriever(self.store, self.provider)

    def test_normalization_repeatability_dimension_finite_empty_and_zero_rules(self):
        first = embed_text(self.provider, "apple fruit")
        self.assertEqual(first, embed_text(self.provider, "apple fruit"))
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in first)), 1.0)
        self.assertEqual(normalize_vector([0.0, 0.0], allow_zero=True), (0.0, 0.0))
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 0.0]), 0.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [2.0, 0.0]), 1.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertAlmostEqual(cosine_similarity([1.0, 0.0], [-1.0, 0.0]), -1.0)

        for output in (
            (), [1.0, float("nan")], [1.0, float("inf")], [1.0, float("-inf")],
            [True, 0.0], [0.0, 0.0], [10**10000, 1.0],
        ):
            with self.subTest(output=output):
                with self.assertRaises(EmbeddingError):
                    embed_text(InvalidEmbeddingProvider(output), "query")
        with self.assertRaisesRegex(EmbeddingError, "dimension mismatch"):
            embed_text(InvalidEmbeddingProvider([1.0, 0.0], dimension=3), "query")
        with self.assertRaisesRegex(EmbeddingError, "dimension"):
            embed_text(InvalidEmbeddingProvider([1.0], dimension=4097), "query")
        with self.assertRaisesRegex(EmbeddingError, "must not be empty"):
            embed_text(self.provider, "  ")

    def test_vector_sorting_provenance_and_repeat_index_are_deterministic(self):
        first = self.retriever.index_current_index(include_symbols=False)
        second = self.retriever.index_current_index(include_symbols=False)
        self.assertEqual(first["sources_considered"], 2)
        self.assertEqual(first["changed"], 2)
        self.assertEqual(second["changed"], 0)
        self.assertEqual(second["unchanged"], 2)
        self.assertEqual(second["vectors"], 2)

        results = self.retriever.search("apple", top_k=2)
        self.assertEqual([item.relative_path for item in results], ["src/apple.py", "src/car.py"])
        self.assertEqual(results[0].source_kind, "chunk")
        self.assertEqual(results[0].source_id, "F" + __import__("hashlib").sha256(
            "src/apple.py".encode("utf-8")
        ).hexdigest()[:20])
        self.assertEqual(results[0].citation, "src/apple.py:1-1")
        self.assertAlmostEqual(results[0].score, 1.0)
        self.assertAlmostEqual(results[1].score, 0.0)
        self.assertEqual(results, self.retriever.search("apple", top_k=2))
        self.assertEqual(VectorStore(self.store).search([0.0, 0.0, 0.0], model_id=self.provider.model_id), [])

    def test_role_aware_provider_receives_document_and_query_inputs_separately(self):
        provider = RoleAwareTestProvider()
        retriever = VectorCandidateRetriever(self.store, provider)

        indexed = retriever.index_current_index(include_symbols=False)
        self.assertEqual(indexed["changed"], 2)
        self.assertEqual([role for role, _ in provider.calls], ["document", "document"])

        results = retriever.search("find the apple implementation", top_k=1)
        self.assertEqual(results[0].relative_path, "src/apple.py")
        self.assertEqual(provider.calls[-1], ("query", "find the apple implementation"))

        with self.assertRaisesRegex(EmbeddingError, "role must be"):
            embed_text(provider, "query", role="other")

    def test_upserts_are_idempotent_and_stale_paths_or_content_are_rejected(self):
        row = next(self.store.iter_chunks())
        vector = embed_text(self.provider, row["text"])
        args = {
            "model_id": self.provider.model_id, "relative_path": row["relative_path"],
            "start_line": row["start_line"], "end_line": row["end_line"],
            "content_hash": row["content_hash"], "vector": vector,
        }
        vectors = VectorStore(self.store)
        self.assertTrue(vectors.upsert_chunk(**args))
        self.assertFalse(vectors.upsert_chunk(**args))
        self.assertEqual(self.store.status()["vectors"], 1)
        with self.assertRaisesRegex(ValueError, "normalized repository-relative"):
            vectors.upsert_chunk(**{**args, "relative_path": "../outside.py"})
        with self.assertRaisesRegex(ValueError, "absent or stale"):
            vectors.upsert_chunk(**{**args, "content_hash": "forged-content-version"})

    def test_file_change_and_delete_invalidate_old_vectors(self):
        self.retriever.index_current_index(include_symbols=False)
        old_hash = next(self.store.iter_chunks())["content_hash"]
        self.write("src/apple.py", '"""theme appearance source"""\n')
        index_repository(self.root, self.store)
        self.assertEqual(self.store.status()["vectors"], 1)
        remaining = self.retriever.search("car", top_k=5)
        self.assertEqual([item.relative_path for item in remaining], ["src/car.py"])
        with self.assertRaisesRegex(ValueError, "absent or stale"):
            VectorStore(self.store).upsert_chunk(
                model_id=self.provider.model_id, relative_path="src/apple.py", start_line=1,
                end_line=1, content_hash=old_hash, vector=(1.0, 0.0, 0.0),
            )

        self.retriever.index_current_index(include_symbols=False)
        self.assertEqual(self.store.status()["vectors"], 2)
        (self.root / "src" / "car.py").unlink()
        index_repository(self.root, self.store)
        self.assertEqual(self.store.status()["vectors"], 1)
        self.assertTrue(all(item.relative_path != "src/car.py" for item in self.retriever.search("car", top_k=5)))

    def test_symbols_are_associated_and_stale_symbol_vectors_are_removed(self):
        first = self.retriever.index_current_index(include_symbols=True)
        self.assertEqual(first["vectors"], 4)  # two file chunks and two module symbols
        symbol = self.store.search_symbols(qualified_name="src.apple")[0]
        results = self.retriever.search("apple", top_k=10)
        self.assertTrue(any(
            item.source_kind == "symbol" and item.symbol_id == symbol["symbol_id"]
            and item.relative_path == "src/apple.py" for item in results
        ))
        self.write("src/apple.py", '"""theme appearance source"""\n')
        index_repository(self.root, self.store)
        self.assertFalse(any(
            item.source_kind == "symbol" and item.symbol_id == symbol["symbol_id"]
            for item in self.retriever.search("apple", top_k=10)
        ))

    def test_embedding_failure_and_schema_unavailability_do_not_break_lexical_search(self):
        broken = VectorCandidateRetriever(self.store, BrokenEmbeddingProvider())
        with self.assertRaisesRegex(EmbeddingError, "provider failed") as raised:
            broken.index_current_index(include_symbols=False)
        self.assertNotIn("synthetic offline failure", str(raised.exception))
        self.assertEqual(self.store.status()["vectors"], 0)
        self.assertTrue(search(self.store, "apple")[0].relative_path.endswith("apple.py"))

        explicit = ExplicitEmbeddingFailureProvider()
        with self.assertRaisesRegex(EmbeddingError, "HTTP 400"):
            embed_text(explicit, "query", role="query")

    def test_vector_source_resource_caps_are_enforced_before_embedding(self):
        with patch("agentforge.embeddings.retriever.MAX_VECTOR_RECORDS", 1):
            with self.assertRaisesRegex(ValueError, "source limit"):
                self.retriever.index_current_index(include_symbols=False)
        self.assertEqual(self.store.status()["vectors"], 0)
        self.assertEqual(len(search(self.store, "apple")), 1)

    def test_temporary_database_reopens_with_vector_candidates_intact(self):
        self.store.close()
        sandbox_project = self.sandbox / "disk-vector-project"
        data_dir = sandbox_project / ".agentforge" / "data"
        data_dir.mkdir(parents=True)
        db_path = data_dir / "index.sqlite3"
        with (
            patch("agentforge.store.PROJECT_ROOT", sandbox_project),
            patch("agentforge.store.DATA_DIR", data_dir),
            patch("agentforge.store.DATABASE_PATH", db_path),
        ):
            with IndexStore(db_path) as disk_store:
                index_repository(self.root, disk_store)
                retriever = VectorCandidateRetriever(disk_store, self.provider)
                retriever.index_current_index(include_symbols=False)
                self.assertEqual(disk_store.status()["vectors"], 2)
            with IndexStore(db_path, readonly=True) as reopened:
                self.assertEqual(reopened.status()["vectors"], 2)
                self.assertEqual(
                    VectorCandidateRetriever(reopened, self.provider).search("apple", top_k=1)[0].relative_path,
                    "src/apple.py",
                )


if __name__ == "__main__":
    unittest.main()
