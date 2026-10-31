import json
from pathlib import Path
import shutil
import tempfile
import unittest

from agentforge.indexer import index_repository
from agentforge.package_evaluation import evaluate_package_relations, validate_package_dataset
from agentforge.package_relations import (
    PackageRelationRetriever,
    build_package_relation_graph,
)
from agentforge.store import IndexStore


ROOT = Path(__file__).parent / "fixtures" / "package_relation_repo"
DATASET = Path(__file__).parent / "fixtures" / "package_relation_eval.json"
SPLIT = Path(__file__).parent / "fixtures" / "package_relation_split.json"


class PackageRelationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.graph = build_package_relation_graph(ROOT)

    def matching(self, kind, module, name=None):
        return [relation for relation in self.graph.relations if
                relation.relation_type == kind and relation.source_module == module and
                (name is None or name in (relation.target_name, relation.local_name))]

    def test_static_local_export(self):
        rows = self.matching("exports_symbol", "atlas", "PackageInfo")
        self.assertEqual(1, len(rows))
        self.assertEqual("resolved", rows[0].resolution_status)
        self.assertTrue(rows[0].source_symbol_id.startswith("S"))
        self.assertGreaterEqual(rows[0].end_line, rows[0].start_line)

    def test_alias_and_multilevel_reexport(self):
        alias = self.matching("import_alias", "atlas", "send")
        resolved = self.matching("resolves_to", "atlas", "send")
        self.assertEqual(1, len(alias))
        self.assertEqual("atlas/impl.py", resolved[0].target_path)

    def test_consumer_alias_resolves_through_public_reexport(self):
        rows = self.matching("resolves_to", "consumer", "ship")
        self.assertEqual(1, len(rows))
        self.assertEqual("resolved", rows[0].resolution_status)
        self.assertEqual("atlas/impl.py", rows[0].target_path)

    def test_relative_import(self):
        rows = self.matching("imports_symbol", "plain", "hidden")
        self.assertEqual("plain.internal", rows[0].target_module)
        self.assertEqual("resolved", rows[0].resolution_status)

    def test_ordinary_import_is_not_export(self):
        self.assertFalse(self.matching("exports_symbol", "plain", "hidden"))
        self.assertFalse(self.matching("reexports_symbol", "plain", "hidden"))

    def test_ambiguous_same_name(self):
        rows = self.matching("reexports_symbol", "ambiguous", "dispatch")
        self.assertEqual("ambiguous", rows[0].resolution_status)
        self.assertEqual(2, len(rows[0].candidate_symbol_ids))

    def test_cycle_is_not_resolved(self):
        rows = self.matching("reexports_symbol", "cycle", "pivot")
        self.assertTrue(rows[0].cycle_detected)
        self.assertEqual("unresolved", rows[0].resolution_status)

    def test_star_import_is_uncertain(self):
        rows = self.matching("imports_symbol", "stars", "*")
        self.assertEqual("uncertain", rows[0].resolution_status)
        self.assertFalse(self.matching("resolves_to", "stars", "alpha"))

    def test_dynamic_all_and_import_are_uncertain(self):
        exports = self.matching("exports_symbol", "dynamic", "<dynamic __all__>")
        imports = self.matching("imports", "dynamic.items")
        self.assertEqual("uncertain", exports[0].resolution_status)
        self.assertTrue(any(row.resolution_status == "uncertain" for row in imports))

    def test_same_name_different_packages_stays_distinct(self):
        atlas = self.matching("resolves_to", "atlas", "send")[0]
        beacon = self.matching("resolves_to", "beacon", "dispatch")[0]
        self.assertNotEqual(atlas.target_symbol_id, beacon.target_symbol_id)
        self.assertNotEqual(atlas.target_path, beacon.target_path)

    def test_relation_ids_ignore_unrelated_text_change(self):
        before = {row.relation_id for row in self.graph.relations if row.source_path == "consumer.py"}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent / ".tmp") as directory:
            copy = Path(directory) / "repo"
            shutil.copytree(ROOT, copy)
            path = copy / "docs" / "api.md"
            path.write_text(path.read_text(encoding="utf-8") + "\nUnrelated note.\n", encoding="utf-8")
            after_graph = build_package_relation_graph(copy)
        after = {row.relation_id for row in after_graph.relations if row.source_path == "consumer.py"}
        self.assertEqual(before, after)

    def test_retriever_filters_and_stable_order(self):
        retriever = PackageRelationRetriever(self.graph)
        kwargs = dict(relation_type="reexports_symbol", source_module="atlas", symbol_name="send")
        first = retriever.search("atlas send reexport", **kwargs)
        second = retriever.search("atlas send reexport", **kwargs)
        self.assertEqual(first, second)
        self.assertEqual("atlas/__init__.py", first[0].relative_path)

    def test_retriever_rejects_invalid_inputs(self):
        retriever = PackageRelationRetriever(self.graph)
        with self.assertRaises(ValueError):
            retriever.search("")
        with self.assertRaises(ValueError):
            retriever.search("x", relation_type="runtime_magic")

    def test_root_must_be_directory(self):
        with self.assertRaises(Exception):
            build_package_relation_graph(DATASET)

    def test_dataset_rejects_evidence_path_traversal(self):
        dataset = json.loads(DATASET.read_text(encoding="utf-8"))
        split = json.loads(SPLIT.read_text(encoding="utf-8"))
        dataset["queries"][0]["evidence"][0]["path"] = "../outside.py"
        with self.assertRaises(ValueError):
            validate_package_dataset(dataset, split, ROOT)

    def test_fixture_and_split_validation(self):
        dataset = json.loads(DATASET.read_text(encoding="utf-8"))
        split = json.loads(SPLIT.read_text(encoding="utf-8"))
        report = validate_package_dataset(dataset, split, ROOT)
        self.assertEqual(40, report["query_count"])
        self.assertTrue(report["leakage_groups_disjoint"])
        self.assertEqual({20}, {report["splits"][name]["query_count"] for name in report["splits"]})

    def test_offline_evaluation_is_reproducible(self):
        dataset = json.loads(DATASET.read_text(encoding="utf-8"))
        store = IndexStore(":memory:")
        try:
            index_repository(ROOT, store)
            first = evaluate_package_relations(store, dataset, self.graph)
            second = evaluate_package_relations(store, dataset, self.graph)
        finally:
            store.close()
        for name in ("lexical", "symbols", "package_relations", "rrf_lexical_symbol_package"):
            left = first["retrievers"][name]
            right = second["retrievers"][name]
            self.assertEqual(left["recall_at"], right["recall_at"])
            self.assertEqual(left["mrr"], right["mrr"])
        self.assertEqual("not_measured", first["retrievers"]["vector"]["status"])


if __name__ == "__main__":
    unittest.main()
