"""Isolation checks for the persistent-Chroma acceptance harness; no Chroma I/O."""

import copy
import hashlib
import unittest
from types import SimpleNamespace

from phase5_1_6_r2_acceptance import (
    AcceptanceFailure,
    assert_no_persistent_repository_id_collision,
    cleanup_owned_persistent_collection,
    create_persistent_acceptance_collection,
    persistent_collection_snapshot,
)


MODEL_ID = "synthetic-model-for-r2-tests"
DIMENSION = 3


def _production_name():
    suffix = hashlib.sha256(MODEL_ID.encode("utf-8")).hexdigest()[:20]
    return "af012_vectors_" + suffix


class _FakeCollection:
    def __init__(self, name, metadata, configuration, records=None, collection_id=None):
        self.name = name
        self.metadata = dict(metadata)
        self.configuration = copy.deepcopy(configuration)
        self.id = collection_id or f"id:{name}"
        self.records = copy.deepcopy(records or [])

    def count(self):
        return len(self.records)

    def get(self, *, include, limit=None, offset=0, ids=None):
        records = self.records
        if ids is not None:
            wanted = set(ids)
            records = [record for record in records if record["id"] in wanted]
        page = records[offset: offset + limit] if limit is not None else records[offset:]
        result = {"ids": [record["id"] for record in page]}
        if "metadatas" in include:
            result["metadatas"] = [copy.deepcopy(record["metadata"]) for record in page]
        if "embeddings" in include:
            result["embeddings"] = [list(record["embedding"]) for record in page]
        return result

    def delete(self, *, where):
        key, value = next(iter(where.items()))
        self.records = [record for record in self.records if record["metadata"].get(key) != value]


class _FakeClient:
    def __init__(self, collections):
        self.collections = {collection.name: collection for collection in collections}

    def list_collections(self):
        return [SimpleNamespace(name=name) for name in sorted(self.collections)]

    def get_collection(self, *, name):
        return self.collections[name]

    def create_collection(self, *, name, embedding_function, metadata, configuration):
        if name in self.collections:
            raise ValueError("collection exists")
        resolved = copy.deepcopy(configuration)
        resolved["hnsw"].update({
            "ef_construction": 100, "ef_search": 100, "max_neighbors": 16,
            "resize_factor": 1.2, "sync_threshold": 1000,
        })
        collection = _FakeCollection(name, metadata, resolved)
        self.collections[name] = collection
        return collection

    def delete_collection(self, *, name):
        del self.collections[name]


def _record(identity, repository_id):
    return {"id": identity, "metadata": {
        "repository_id": repository_id,
        "source_type": "code",
        "content_hash": "a" * 64,
        "model_id": MODEL_ID,
        "file_path": "src/sample.py",
        "start_line": 1,
        "end_line": 2,
    }, "embedding": [1.0, 0.25, 0.0]}


def _client():
    production = _FakeCollection(
        _production_name(),
        {"agentforge_model_id": MODEL_ID, "agentforge_dimension": str(DIMENSION), "hnsw:space": "cosine"},
        {"hnsw": {"space": "cosine", "ef_search": 100}},
        [_record("existing-id", "existing-repository")],
    )
    return _FakeClient([production]), production


class PersistentAcceptanceIsolationTests(unittest.TestCase):
    def test_preplanned_owned_repository_id_is_not_a_registered_collision(self):
        planned_repo_id = "planned-for-this-run"

        assert_no_persistent_repository_id_collision(
            {planned_repo_id}, {"previously-registered-repository"},
        )

        with self.assertRaisesRegex(
            AcceptanceFailure, "SYNTHETIC_REPOSITORY_ID_COLLISION",
        ):
            assert_no_persistent_repository_id_collision(
                {planned_repo_id}, {planned_repo_id},
            )

    def test_snapshot_records_ids_and_data_hashes_without_source_text(self):
        client, _production = _client()

        snapshot = persistent_collection_snapshot(client)

        self.assertEqual(1, snapshot["collection_count"])
        collection = snapshot["collections"][0]
        self.assertEqual(["existing-id"], collection["existing_ids"])
        self.assertEqual("existing-repository", collection["repository_ids"][0])
        self.assertEqual(64, len(collection["record_summaries"][0]["vector_sha256"]))
        self.assertNotIn("text", collection["record_summaries"][0])

    def test_acceptance_collection_uses_product_settings_and_cleans_only_owned_data(self):
        client, production = _client()
        before = persistent_collection_snapshot(client)

        info = create_persistent_acceptance_collection(
            client, model_id=MODEL_ID, dimension=DIMENSION, run_id="0123456789abcdef",
        )
        test_collection = client.get_collection(name=info["name"])
        test_collection.records.append(_record("synthetic-id", "owned-repository"))
        cleanup = cleanup_owned_persistent_collection(
            client, name=info["name"], expected_collection_id=info["collection_id"],
            owned_repository_ids={"owned-repository"},
        )
        after = persistent_collection_snapshot(client)

        self.assertTrue(info["configuration_matches"])
        self.assertEqual("cosine", info["product_requested_configuration"]["hnsw"]["space"])
        self.assertTrue(cleanup["collection_removed"])
        self.assertEqual(before, after)
        self.assertEqual(1, production.count())
        self.assertEqual("existing-id", production.records[0]["id"])

    def test_cleanup_refuses_to_delete_collection_with_foreign_repository_id(self):
        client, _production = _client()
        info = create_persistent_acceptance_collection(
            client, model_id=MODEL_ID, dimension=DIMENSION, run_id="fedcba9876543210",
        )
        collection = client.get_collection(name=info["name"])
        collection.records.extend([
            _record("owned-id", "owned-repository"),
            _record("foreign-id", "foreign-repository"),
        ])
        before = copy.deepcopy(collection.records)

        with self.assertRaisesRegex(AcceptanceFailure, "UNOWNED_RECORD_IN_ACCEPTANCE_COLLECTION"):
            cleanup_owned_persistent_collection(
                client, name=info["name"], expected_collection_id=info["collection_id"],
                owned_repository_ids={"owned-repository"},
            )

        self.assertEqual(before, collection.records)
        self.assertIn(info["name"], client.collections)


if __name__ == "__main__":
    unittest.main()
