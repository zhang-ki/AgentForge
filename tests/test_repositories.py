from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agentforge.embeddings.chroma_index import ChromaCodeIndex
from agentforge.embeddings.chroma_store import delete_repository_vectors
from agentforge.cli import _repository_command, parser
from agentforge.indexer import index_repository
from agentforge.repositories import RepositoryRegistry, open_index_for_repository, repository_identity
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
                "document": None if documents is None else documents[index],
            }

    def get(self, *, ids=None, where=None, limit=None, offset=0, include=None):
        if ids is not None:
            identities = [identity for identity in ids if identity in self.records]
        else:
            identities = [identity for identity, item in self.records.items()
                          if self._matches(item["metadata"], where or {})]
            identities = identities[offset:None if limit is None else offset + limit]
        return {"ids": identities,
                "metadatas": [dict(self.records[identity]["metadata"]) for identity in identities]}

    def query(self, *, query_embeddings, n_results, where, include):
        query = query_embeddings[0]
        selected = [(identity, item) for identity, item in self.records.items()
                    if self._matches(item["metadata"], where)]
        selected.sort(key=lambda pair: (
            sum((left - right) ** 2 for left, right in zip(query, pair[1]["embedding"])),
            pair[0],
        ))
        selected = selected[:n_results]
        return {
            "ids": [[identity for identity, _ in selected]],
            "metadatas": [[dict(item["metadata"]) for _, item in selected]],
            "distances": [[sum((left - right) ** 2 for left, right in zip(query, item["embedding"]))
                           for _, item in selected]],
            "documents": [[item["document"] for _, item in selected]],
        }

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
        # The path must remain under this test's unique temporary directory.
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
        self.assertEqual(1, len(first_chroma.search("alpha", top_k=3)))
        self.assertIn("alpha marker", first_chroma.search("alpha", top_k=3)[0].text)
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

    def test_registered_unindexed_repository_can_be_target_indexed_from_cli(self):
        self.write("same.py", "def identical_name():\n    return 'first repository'\n")
        with self._isolated_storage():
            registered = _repository_command(SimpleNamespace(
                repo_action="register", path=str(self.root), name="First"))
            repository_id = registered["repository"]["repository_id"]
            parsed = parser().parse_args(["repo", "index", repository_id])
            self.assertEqual(repository_id, parsed.repository)
            indexed = _repository_command(SimpleNamespace(
                repo_action="index", repository=repository_id))
            self.assertTrue(indexed["active"])
            self.assertEqual(1, indexed["files"])
            with RepositoryRegistry(readonly=True) as registry:
                self.assertEqual(repository_id, registry.active_id())
                self.assertTrue(registry.get(repository_id)["indexed_at"])

            # A forced rebuild reparses the target while leaving its identity and
            # the current registration stable.
            rebuilt = _repository_command(SimpleNamespace(
                repo_action="rebuild", repository=repository_id))
            self.assertEqual("rebuild", rebuilt["operation"])
            self.assertEqual(repository_id, rebuilt["repository_id"])

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
