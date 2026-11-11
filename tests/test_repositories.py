from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agentforge.embeddings.chroma_index import ChromaCodeIndex
from agentforge.embeddings.chroma_store import delete_repository_vectors
from agentforge.cli import _prepare_repository_index, _repository_command, parser
from agentforge.index_lifecycle import IndexLifecycleState, UnavailableVectorRetriever
from agentforge.indexer import index_repository
from agentforge.product_retrieval import ProductRetriever
from agentforge.repositories import RepositoryRegistry, open_index_for_repository, repository_identity
from agentforge.store import IndexStore
from tests.support import LocalTestCase


class _InMemoryChromaCollection:
    def __init__(self):
        self.records = {}

    @staticmethod
    def _matches(metadata, where):
        if "$and" in where:
            return all(_InMemoryChromaCollection._matches(metadata, item)
                       for item in where["$and"])
        return all(metadata.get(key) == value for key, value in where.items())

    def upsert(self, *, ids, embeddings, metadatas, documents=None):
        for index, identity in enumerate(ids):
            self.records[identity] = {
                "embedding": list(embeddings[index]), "metadata": dict(metadatas[index]),
                "document": "" if documents is None else documents[index],
            }

    def get(self, *, ids=None, where=None, limit=None, offset=0, include=None):
        if ids is not None:
            identities = [identity for identity in ids if identity in self.records]
        else:
            identities = [identity for identity, item in self.records.items()
                          if self._matches(item["metadata"], where or {})]
            identities = identities[offset:None if limit is None else offset + limit]
        result = {"ids": identities}
        if include is None or "metadatas" in include:
            result["metadatas"] = [dict(self.records[identity]["metadata"]) for identity in identities]
        if include is not None and "documents" in include:
            result["documents"] = [self.records[identity]["document"] for identity in identities]
        if include is not None and "embeddings" in include:
            result["embeddings"] = [list(self.records[identity]["embedding"]) for identity in identities]
        return result

    def update(self, *, ids, embeddings=None, documents=None):
        for index, identity in enumerate(ids):
            if embeddings is not None:
                self.records[identity]["embedding"] = list(embeddings[index])
            if documents is not None:
                self.records[identity]["document"] = documents[index]

    def query(self, *, query_embeddings, n_results, where, include):
        self.query_includes = list(include)
        query = query_embeddings[0]
        selected = [(identity, item) for identity, item in self.records.items()
                    if self._matches(item["metadata"], where)]
        selected.sort(key=lambda pair: (
            sum((left - right) ** 2 for left, right in zip(query, pair[1]["embedding"])),
            pair[0],
        ))
        selected = selected[:n_results]
        result = {
            "ids": [[identity for identity, _ in selected]],
            "metadatas": [[dict(item["metadata"]) for _, item in selected]],
            "distances": [[sum((left - right) ** 2 for left, right in zip(query, item["embedding"]))
                           for _, item in selected]],
        }
        if "documents" in include:
            result["documents"] = [[item["document"] for _, item in selected]]
        return result

    def delete(self, *, ids=None, where=None):
        if ids is not None:
            for identity in ids:
                self.records.pop(identity, None)
        else:
            for identity in [key for key, item in self.records.items()
                             if self._matches(item["metadata"], where or {})]:
                self.records.pop(identity, None)


class _SyntheticEmbeddingProvider:
    model_id = "synthetic-two-repository-test"
    dimension = 2

    @staticmethod
    def _vector(text):
        return (1.0, 0.0) if "alpha" in text.casefold() else (0.0, 1.0)

    def embed_document(self, text):
        return self._vector(text)

    def embed_query(self, text):
        return self._vector(text)


class RepositoryLifecycleTests(LocalTestCase):
    @contextmanager
    def _isolated_storage(self):
        project = self.sandbox / "agentforge-home"
        data = project / ".agentforge" / "data"
        indexes = data / "repositories"
        indexes.mkdir(parents=True)
        with (
            patch("agentforge.repositories.PROJECT_ROOT", project),
            patch("agentforge.repositories.DATA_DIR", data),
            patch("agentforge.repositories.DATABASE_PATH", data / "index.sqlite3"),
            patch("agentforge.repositories.REPOSITORY_INDEX_DIR", indexes),
            patch("agentforge.repositories.REPOSITORY_CATALOG_PATH", data / "repositories.sqlite3"),
            patch("agentforge.store.PROJECT_ROOT", project),
            patch("agentforge.store.DATA_DIR", data),
            patch("agentforge.store.DATABASE_PATH", data / "index.sqlite3"),
            patch("agentforge.store.REPOSITORY_INDEX_DIR", indexes),
        ):
            yield

    def _cleanup_index(self, path):
        # 该路径必须始终位于本测试独有的临时目录中。
        self.assertTrue(path.resolve().is_relative_to(self.sandbox.resolve()))
        for suffix in ("", "-journal", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)

    def test_two_repositories_with_same_relative_names_have_isolated_indexes(self):
        self.write("src/shared.py", "def shared_value():\n    return 'alpha'\n")
        second = self.sandbox / "second"
        second.mkdir()
        (second / "src").mkdir()
        (second / "src" / "shared.py").write_text(
            "def shared_value():\n    return 'beta'\n", encoding="utf-8")

        first_id = repository_identity(self.root)
        second_id = repository_identity(second)
        self.assertNotEqual(first_id, second_id)
        with self._isolated_storage():
            first_store = open_index_for_repository(self.root)
            second_store = open_index_for_repository(second)
        self.addCleanup(self._cleanup_index, Path(second_store.database))
        self.addCleanup(second_store.close)
        self.addCleanup(self._cleanup_index, Path(first_store.database))
        self.addCleanup(first_store.close)

        index_repository(self.root, first_store)
        index_repository(second, second_store)
        first_row = list(first_store.iter_chunks())[0]
        second_row = list(second_store.iter_chunks())[0]
        self.assertEqual(first_row["relative_path"], second_row["relative_path"])
        self.assertNotEqual(first_row["content_hash"], second_row["content_hash"])
        self.assertIn("alpha", first_row["text"])
        self.assertIn("beta", second_row["text"])
        self.assertEqual(first_store.metadata()["repository_id"], first_id)
        self.assertEqual(second_store.metadata()["repository_id"], second_id)
        self.assertEqual(first_store.status()["files"], 1)
        self.assertEqual(second_store.status()["files"], 1)

    def test_chroma_vectors_are_isolated_and_updated_per_repository(self):
        self.write("src/shared.py", "def shared_value():\n    return 'alpha marker'\n")
        second = self.sandbox / "second"
        second.mkdir()
        (second / "src").mkdir()
        second_source = second / "src" / "shared.py"
        second_source.write_text(
            "def shared_value():\n    return 'beta marker'\n", encoding="utf-8")
        with self._isolated_storage():
            first_store = open_index_for_repository(self.root)
            second_store = open_index_for_repository(second)
        first_path, second_path = Path(first_store.database), Path(second_store.database)
        self.addCleanup(self._cleanup_index, second_path)
        self.addCleanup(second_store.close)
        self.addCleanup(self._cleanup_index, first_path)
        self.addCleanup(first_store.close)
        index_repository(self.root, first_store)
        index_repository(second, second_store)

        collection = _InMemoryChromaCollection()
        provider = _SyntheticEmbeddingProvider()
        first_chroma = ChromaCodeIndex(first_store, collection, provider)
        second_chroma = ChromaCodeIndex(second_store, collection, provider)
        first_chroma.sync()
        second_chroma.sync()
        self.assertTrue(all(item["document"] == "" for item in collection.records.values()))
        with patch.object(first_store, "iter_chunks", side_effect=AssertionError("full chunk scan")):
            first_results = first_chroma.search("alpha", top_k=3)
        self.assertEqual(1, len(first_results))
        self.assertIn("alpha marker", first_results[0].text)
        self.assertNotIn("documents", collection.query_includes)
        self.assertEqual(1, len(second_chroma.search("beta", top_k=3)))
        self.assertIn("beta marker", second_chroma.search("beta", top_k=3)[0].text)

        self.write("src/shared.py", "def shared_value():\n    return 'alpha revised'\n")
        index_repository(self.root, first_store)
        stats = first_chroma.sync()
        self.assertEqual(1, stats.embedded)
        self.assertIn("alpha revised", first_chroma.search("alpha", top_k=3)[0].text)
        self.assertIn("beta marker", second_chroma.search("beta", top_k=3)[0].text)
        rebuilt = index_repository(self.root, first_store, force_rebuild=True)
        self.assertEqual((1, 0, 0), (rebuilt["added"], rebuilt["changed"], rebuilt["unchanged"]))
        rebuilt_chunks = [row for row in first_store.iter_chunks()
                          if row["relative_path"] == "src/shared.py"]
        self.assertEqual(1, len(rebuilt_chunks))
        self.assertIn("alpha revised", rebuilt_chunks[0]["text"])

    def test_sync_clears_legacy_chroma_code_text_without_reembedding(self):
        self.write("src/shared.py", "def shared_value():\n    return 'legacy text'")
        with self._isolated_storage():
            store = open_index_for_repository(self.root)
        self.addCleanup(self._cleanup_index, Path(store.database))
        self.addCleanup(store.close)
        index_repository(self.root, store)
        collection = _InMemoryChromaCollection()
        provider = _SyntheticEmbeddingProvider()
        service = ChromaCodeIndex(store, collection, provider)
        service.repository = str(self.root)
        service.repository_id = repository_identity(self.root)
        source, document = next(service._sources())
        service.store.upsert(source, provider.embed_document(document), document=document)
        self.assertEqual(document, next(iter(collection.records.values()))["document"])

        stats = service.sync()

        self.assertEqual((0, 1), (stats.embedded, stats.unchanged))
        self.assertEqual("", next(iter(collection.records.values()))["document"])
        self.assertEqual(document, service.search("legacy", top_k=1)[0].text)

    def test_repository_registration_indexes_sqlite_and_chroma_without_external_service(self):
        self.write("same.py", "def identical_name():\n    return 'first repository'\n")
        with self._isolated_storage():
            unavailable = {
                "embedding_provider": None, "collection": None,
                "vector_retriever": UnavailableVectorRetriever(
                    "embedding_service_unavailable", "TimeoutError"),
                "readiness_seconds": None,
                "failure": {"reason_code": "embedding_service_unavailable",
                            "error_type": "TimeoutError"},
            }
            with patch("agentforge.cli._initialize_optional_chroma", return_value=unavailable), \
                    patch("agentforge.cli._synchronize_optional_chroma",
                          side_effect=lambda _store, retriever: retriever):
                registered = _repository_command(SimpleNamespace(
                    repo_action="register", path=str(self.root), name="First"))
                repository_id = registered["repository"]["repository_id"]
                self.assertTrue(registered["indexed"])
                self.assertEqual("DEGRADED_LEXICAL_ONLY",
                                 registered["index_lifecycle"]["state"])
                parsed = parser().parse_args(["repo", "index", repository_id])
                self.assertEqual(repository_id, parsed.repository)
                indexed = _repository_command(SimpleNamespace(
                    repo_action="index", repository=repository_id))
                self.assertTrue(indexed["active"])
                self.assertEqual(1, indexed["files"])
                with RepositoryRegistry(readonly=True) as registry:
                    self.assertEqual(repository_id, registry.active_id())
                    self.assertTrue(registry.get(repository_id)["indexed_at"])

                # 强制重建会重新解析目标仓库，同时保留其身份和
                # 当前注册信息不变。
                rebuilt = _repository_command(SimpleNamespace(
                    repo_action="rebuild", repository=repository_id))
                self.assertEqual("rebuild", rebuilt["operation"])
                self.assertEqual(repository_id, rebuilt["repository_id"])

    def test_repository_preparation_indexes_first_use_and_refreshes_changed_files(self):
        source = self.write("queue.py", "def read_queue():\n    return 'before'\n")
        with self._isolated_storage():
            store = open_index_for_repository(self.root, readonly=False)
            self.addCleanup(self._cleanup_index, Path(store.database))
            self.addCleanup(store.close)
            request = SimpleNamespace(repository=str(self.root))

            prepared = _prepare_repository_index(store, request, None)
            first = list(store.iter_chunks())
            self.assertTrue(prepared["scan_report"].complete)
            self.assertIn("before", "\n".join(row["text"] for row in first))

            source.write_text("def read_queue():\n    return 'after'\n", encoding="utf-8")
            _prepare_repository_index(store, request, None)
            second = list(store.iter_chunks())
            self.assertIn("after", "\n".join(row["text"] for row in second))
            self.assertNotIn("before", "\n".join(row["text"] for row in second))
            with RepositoryRegistry(readonly=True) as registry:
                registration = registry.get(repository_identity(self.root))
                self.assertTrue(registration["indexed_at"])

    def test_embedding_failure_keeps_verified_lexical_results_and_exposes_degradation(self):
        self.write("queue.py", "def queue_state():\n    return 'durable queue'\n")
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            retriever = ProductRetriever(
                store, self.root,
                vector_retriever=UnavailableVectorRetriever(
                    "embedding_service_unavailable", "TimeoutError"),
            )
            outcome = retriever.search("How does the queue implementation work?", top_k=4)

        self.assertEqual(IndexLifecycleState.DEGRADED_LEXICAL_ONLY.value,
                         outcome.index_lifecycle["state"])
        self.assertIn("vector", outcome.unavailable_channels)
        self.assertTrue(outcome.candidates)
        self.assertTrue(all("lexical" in item.channels for item in outcome.candidates))
        self.assertTrue(all(item.content_hash for item in outcome.candidates))

    def test_chroma_hash_mismatch_marks_derived_index_stale(self):
        self.write("queue.py", "def queue_state():\n    return 'durable queue'\n")
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            collection = _InMemoryChromaCollection()
            service = ChromaCodeIndex(store, collection, _SyntheticEmbeddingProvider())
            service.sync()
            self.assertTrue(service.inspect_consistency()["complete"])

            record = next(iter(collection.records.values()))
            record["metadata"]["content_hash"] = "0" * 64
            state = service.inspect_consistency()

        self.assertFalse(state["complete"], state)
        self.assertEqual(1, state["stale_vectors"])

    def test_registry_keeps_stable_identity_and_requires_completed_index_to_switch(self):
        with self._isolated_storage():
            with RepositoryRegistry(":memory:") as registry:
                item = registry.register(self.root, display_name="First")
                again = registry.register(self.root)
                self.assertEqual(item["repository_id"], again["repository_id"])
                self.assertEqual(item["repository_id"], repository_identity(self.root))
                with self.assertRaisesRegex(ValueError, "no completed index"):
                    registry.activate(item["repository_id"])

                store = open_index_for_repository(self.root)
                try:
                    index_repository(self.root, store)
                    registry.set_index_path(item["repository_id"], store.database)
                    registry.mark_indexed(item["repository_id"])
                    registry.activate(item["repository_id"])
                    self.assertEqual(registry.active()["repository_id"], item["repository_id"])
                    self.assertTrue(registry.list()[0]["active"])
                finally:
                    path = Path(store.database)
                    store.close()
                    self._cleanup_index(path)

    def test_registry_rejects_out_of_boundary_index_path(self):
        with RepositoryRegistry(":memory:") as registry:
            with self.assertRaisesRegex(ValueError, "outside AgentForge storage"):
                registry.register(self.root, index_path=self.sandbox / "outside.sqlite3")

    def test_active_repository_removal_is_rejected_before_cleanup(self):
        with RepositoryRegistry(":memory:") as registry:
            item = registry.register(self.root)
            with registry.connection:
                registry.connection.execute(
                    "INSERT INTO settings(key, value) VALUES ('active_repository_id', ?)",
                    (item["repository_id"],),
                )
            with self.assertRaisesRegex(ValueError, "Switch to another indexed repository"):
                registry.remove_registration(item["repository_id"])

    def test_inactive_repository_can_pass_removal_guard(self):
        with RepositoryRegistry(":memory:") as registry:
            item = registry.register(self.root)
            self.assertEqual(item, registry.remove_registration(item["repository_id"]))

    def test_chroma_cleanup_is_repository_filtered(self):
        collection_a = Mock()
        collection_a.name = "codes-a"
        collection_b = Mock()
        collection_b.name = "memories-b"
        client = Mock()
        client.list_collections.return_value = [collection_a, collection_b]
        client.get_collection.side_effect = lambda name: {"codes-a": collection_a,
                                                           "memories-b": collection_b}[name]
        removed = delete_repository_vectors(client, "repo-one")
        self.assertEqual(removed, ["codes-a", "memories-b"])
        collection_a.delete.assert_called_once_with(where={"repository_id": "repo-one"})
        collection_b.delete.assert_called_once_with(where={"repository_id": "repo-one"})
