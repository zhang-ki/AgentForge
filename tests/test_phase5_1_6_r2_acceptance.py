"""Offline behavior tests for the Phase 5.1.6-R2 acceptance harness."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agentforge.af012_runtime import collection_for_model, create_persistent_chroma
from agentforge.embeddings.base import EmbeddingError
from agentforge.embeddings.chroma_store import ChromaCandidateStore, ChromaSource, _identity
from phase5_1_6_r2_acceptance import (
    PROJECT_ROOT,
    _synthetic_repository,
    apply_launcher_embedding_defaults,
    collection_manifest,
    launcher_embedding_configuration,
    launch_reopen_child,
    run_child_process,
    run_degradation_and_recovery,
)


class _DeterministicProvider:
    model_id = "phase5_1_6_r2_test_embedding"
    dimension = 8

    def __init__(self):
        self._counts = {"attempts": 0, "sent": 0, "successes": 0, "failures": 0}

    @property
    def request_counts(self):
        return dict(self._counts)

    def _embed(self, _text):
        self._counts["attempts"] += 1
        self._counts["successes"] += 1
        self._counts["sent"] += 1
        return (1.0, 0.25, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def embed(self, text):
        return self._embed(text)

    def embed_document(self, text):
        return self._embed(text)

    def embed_query(self, text):
        return self._embed(text)


class _UnavailableProvider:
    model_id = _DeterministicProvider.model_id
    dimension = _DeterministicProvider.dimension

    def __init__(self):
        self._counts = {"attempts": 0, "sent": 0, "successes": 0, "failures": 0}

    @property
    def request_counts(self):
        return dict(self._counts)

    def _fail(self, _text):
        self._counts["attempts"] += 1
        self._counts["failures"] += 1
        raise EmbeddingError("synthetic temporary Embedding outage")

    def embed(self, text):
        return self._fail(text)

    def embed_document(self, text):
        return self._fail(text)

    def embed_query(self, text):
        return self._fail(text)


class EmbeddingLauncherConfigurationTests(unittest.TestCase):
    def test_process_defaults_match_project_launcher_without_mutating_parent_environment(self):
        process_environment = {}
        before = os.environ.get("AGENTFORGE_EMBEDDING_BASE_URL")

        configuration = launcher_embedding_configuration()
        applied = apply_launcher_embedding_defaults(process_environment)

        self.assertEqual(configuration["AGENTFORGE_EMBEDDING_BASE_URL"],
                         process_environment["AGENTFORGE_EMBEDDING_BASE_URL"])
        self.assertEqual(configuration["AGENTFORGE_EMBEDDING_MODEL_ID"],
                         process_environment["AGENTFORGE_EMBEDDING_MODEL_ID"])
        self.assertEqual("1024", process_environment["AGENTFORGE_EMBEDDING_DIMENSION"])
        self.assertEqual("configured", applied["status"])
        self.assertTrue(applied["endpoint_local"])
        self.assertEqual(before, os.environ.get("AGENTFORGE_EMBEDDING_BASE_URL"))

    def test_conflicting_process_override_is_rejected_before_any_request(self):
        process_environment = {"AGENTFORGE_EMBEDDING_BASE_URL": "https://example.invalid"}

        with self.assertRaisesRegex(RuntimeError, "EMBEDDING_CONFIGURATION_OVERRIDE_CONFLICT"):
            apply_launcher_embedding_defaults(process_environment)

        self.assertEqual("https://example.invalid",
                         process_environment["AGENTFORGE_EMBEDDING_BASE_URL"])


@unittest.skipUnless(importlib.util.find_spec("chromadb"), "isolated Chroma dependency unavailable")
class R2CrossProcessPersistenceTests(unittest.TestCase):
    def test_child_reopens_collection_and_compares_vector_metadata_hash(self):
        with tempfile.TemporaryDirectory(prefix=".phase5_1_6_r2_reopen_", dir=PROJECT_ROOT) as temp:
            root = Path(temp)
            chroma_path = root / "chroma"
            client = create_persistent_chroma(chroma_path)
            collection = collection_for_model(client, "phase5_1_6_r2_test_model", 3)
            source = ChromaSource(
                repository_id="phase5_1_6_r2_synthetic_repo",
                source_type="code",
                chunk_id="src/worker.py:1-3",
                file_path="src/worker.py",
                content_hash="a" * 64,
                model_id="phase5_1_6_r2_test_model",
                start_line=1,
                end_line=3,
            )
            ChromaCandidateStore(collection, dimension=3).upsert(source, [1.0, 0.1, 0.0])
            expected = collection_manifest(collection, source.repository_id)
            client.close()

            request = {
                "mode": "raw_test",
                "chroma_path": str(chroma_path),
                "repository_id": source.repository_id,
                "collection": collection.name,
                "expected_manifest": expected,
                "parent_pid": os.getpid(),
                "dimension": 3,
                "model_id": source.model_id,
                "query_vector": [1.0, 0.1, 0.0],
                "expected_content_hash": source.content_hash,
                "expected_path": source.file_path,
            }
            process = launch_reopen_child(request, request_directory=root)

            self.assertFalse(process["timed_out"], process)
            self.assertIsNone(process["launch_error"], process)
            self.assertEqual(0, process["returncode"], process["stderr"])
            result = process["result"]
            self.assertEqual("PASS", result["status"])
            self.assertNotEqual(os.getpid(), result["pid"])
            self.assertEqual(expected, result["manifest"])
            self.assertEqual(1, result["manifest"]["repository_vector_count"])
            self.assertEqual([_identity(source)], result["manifest"]["ids"])
            self.assertEqual(source.content_hash,
                             result["manifest"]["records"][0]["metadata"]["content_hash"])
            self.assertTrue(result["manifest"]["records"][0]["vector_sha256"])
            self.assertEqual(source.file_path, result["semantic_hits"][0]["path"])

    def test_child_nonzero_exit_is_reported_with_captured_stderr(self):
        result = run_child_process(
            [sys.executable, "-c", "import sys; print('synthetic-child-error', file=sys.stderr); sys.exit(23)"],
            cwd=Path(__file__).resolve().parents[1], timeout_seconds=10,
        )

        self.assertEqual(23, result["returncode"])
        self.assertFalse(result["timed_out"])
        self.assertIn("synthetic-child-error", result["stderr"])


@unittest.skipUnless(importlib.util.find_spec("chromadb"), "isolated Chroma dependency unavailable")
class R2DegradedRetrievalTests(unittest.TestCase):
    def test_embedding_and_chroma_failures_keep_hash_verified_lexical_results_then_recover(self):
        with tempfile.TemporaryDirectory(prefix=".phase5_1_6_r2_degraded_", dir=PROJECT_ROOT) as temp:
            root = Path(temp)
            repository = root / "synthetic_repo"
            _synthetic_repository(repository, name="offline degradation test")

            result = run_degradation_and_recovery(
                repository,
                root / "chroma",
                embedding_provider=_DeterministicProvider(),
                unavailable_provider=_UnavailableProvider(),
            )

        self.assertEqual("PASS", result["status"])
        self.assertEqual("DEGRADED_LEXICAL_ONLY", result["embedding_unavailable"]["state"])
        self.assertEqual("vector_search_unavailable", result["embedding_unavailable"]["reason_code"])
        self.assertGreater(result["embedding_unavailable"]["lexical_candidates_checked_against_sqlite"], 0)
        self.assertTrue(result["embedding_unavailable"]["user_message_visible"])
        self.assertEqual("DEGRADED_LEXICAL_ONLY", result["chroma_unavailable"]["state"])
        self.assertEqual("vector_search_unavailable", result["chroma_unavailable"]["reason_code"])
        self.assertGreater(result["chroma_unavailable"]["lexical_candidates_checked_against_sqlite"], 0)
        self.assertEqual("READY", result["recovery"]["state"])
        self.assertTrue(result["recovery"]["sqlite_authoritative_snapshot_unchanged"])
        self.assertFalse(result["recovery"]["duplicate_or_stale_vectors"])
        self.assertEqual(result["sqlite_chunk_count"], result["recovery"]["chroma_count_after_recovery"])


if __name__ == "__main__":
    unittest.main()
