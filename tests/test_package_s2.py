"""AF-010-S2 的多仓库、路由和生命周期回归测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentforge.package_relations import (
    IncrementalPackageRelationIndex,
    build_package_relation_graph,
)
from agentforge.scanner import ScanLimits, ScanReadError, read_candidate
from agentforge.package_s2 import (
    DEFAULT_DATASET,
    DEFAULT_REPOSITORIES,
    DEFAULT_SPLIT,
    _read,
    _write_scale_repository,
    evaluate_s2,
    validate_s2,
)
from agentforge.query_routing import ExplainableQueryRouter


WORKSPACE = Path(__file__).resolve().parents[1]
TEMP_ROOT = WORKSPACE / "tests" / ".tmp"


class DeterministicTestEmbedding:
    """仅用于测试流水线的向量夹具，不代表语义模型结果。"""

    model_id = "test-only-deterministic-vector"
    dimension = 8

    def __init__(self):
        self.calls = 0

    def _vector(self, text):
        import hashlib

        self.calls += 1
        result = [0.0] * self.dimension
        for token in text.casefold().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for index in range(self.dimension):
                result[index] += (digest[index] - 127.5) / 127.5
        if not any(result):
            result[0] = 1.0
        return result

    def embed_document(self, text):
        return self._vector(text)

    def embed_query(self, text):
        return self._vector(text)


class PackageS2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)

    def test_fixture_and_split_are_complete_and_evidence_is_in_bounds(self):
        dataset, split = _read(DEFAULT_DATASET), _read(DEFAULT_SPLIT)
        report = validate_s2(dataset, split, DEFAULT_REPOSITORIES)
        self.assertEqual(report["query_count"], 120)
        self.assertEqual(report["repository_count"], 3)
        self.assertTrue(report["leakage_groups_disjoint"])
        self.assertEqual(report["splits"]["calibration"], {
            "query_count": 60, "evidence": 30, "no_evidence": 30,
        })
        self.assertEqual(report["splits"]["validation"], report["splits"]["calibration"])

    def test_split_leakage_and_traversal_are_rejected(self):
        dataset, split = _read(DEFAULT_DATASET), _read(DEFAULT_SPLIT)
        corrupted = json.loads(json.dumps(split))
        query_id = corrupted["splits"]["validation"][0]
        row = next(item for item in dataset["queries"] if item["id"] == query_id)
        row["split"] = "calibration"
        with self.assertRaises(ValueError):
            validate_s2(dataset, corrupted, DEFAULT_REPOSITORIES)

        dataset, split = _read(DEFAULT_DATASET), _read(DEFAULT_SPLIT)
        dataset["repositories"][0]["path"] = "../outside"
        with self.assertRaises((ValueError, OSError)):
            validate_s2(dataset, split, DEFAULT_REPOSITORIES)

    def test_three_repository_graphs_capture_static_and_uncertain_relations(self):
        for repository in ("harbor_registry", "clinic_gateway", "observatory_sdk"):
            root = DEFAULT_REPOSITORIES / repository
            graph_a = build_package_relation_graph(root)
            graph_b = build_package_relation_graph(root)
            self.assertEqual(graph_a.relations, graph_b.relations)
            self.assertFalse(graph_a.parse_failures)
            types = {relation.relation_type for relation in graph_a.relations}
            self.assertTrue({"imports", "imports_symbol", "exports_symbol",
                             "reexports_symbol", "import_alias", "resolves_to"} <= types)
            self.assertTrue(any(relation.resolution_status == "uncertain"
                                for relation in graph_a.relations))
            self.assertTrue(any(relation.cycle_detected for relation in graph_a.relations))

    def test_ambiguous_import_is_preserved_without_guessing(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text(
                "def marker():\n    return 'function'\n\nfrom . import marker\n",
                encoding="utf-8",
            )
            (root / "pkg" / "marker.py").write_text(
                "value = 'submodule'\n", encoding="utf-8"
            )
            graph = build_package_relation_graph(root)
            ambiguous = [relation for relation in graph.relations
                         if relation.relation_type == "imports_symbol"
                         and relation.target_name == "marker"]
            self.assertEqual(len(ambiguous), 1)
            self.assertEqual(ambiguous[0].resolution_status, "ambiguous")
            self.assertIsNone(ambiguous[0].target_symbol_id)
            self.assertEqual(len(ambiguous[0].candidate_symbol_ids), 2)

    def test_incremental_index_tracks_ambiguity_appearing_and_disappearing(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            package = root / "pkg"
            package.mkdir()
            (package / "__init__.py").write_text(
                "def marker():\n    return 'function'\n\nfrom . import marker\n",
                encoding="utf-8",
            )
            lifecycle = IncrementalPackageRelationIndex()
            lifecycle.refresh(root)
            lookup = lambda: next(
                relation for relation in lifecycle.graph.relations
                if relation.relation_type == "imports_symbol" and relation.target_name == "marker"
            )
            self.assertEqual(lookup().resolution_status, "resolved")
            (package / "marker.py").write_text("value = 'submodule'\n", encoding="utf-8")
            self.assertEqual(lifecycle.refresh(root)["added"], ["pkg/marker.py"])
            self.assertEqual(lookup().resolution_status, "ambiguous")
            (package / "marker.py").unlink()
            self.assertEqual(lifecycle.refresh(root)["deleted"], ["pkg/marker.py"])
            self.assertEqual(lookup().resolution_status, "resolved")

    def test_dynamic_and_star_imports_remain_uncertain_and_syntax_errors_are_reported(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text(
                "from .item import *\nimport importlib\n"
                "module = importlib.import_module(name)\n__all__ = build_names()\n",
                encoding="utf-8",
            )
            (root / "pkg" / "item.py").write_text("def visible():\n    return 1\n", encoding="utf-8")
            (root / "broken.py").write_text("def unfinished(:\n", encoding="utf-8")
            graph = build_package_relation_graph(root)
            self.assertTrue(any(r.relation_type == "imports_symbol" and r.target_name == "*"
                                and r.resolution_status == "uncertain" for r in graph.relations))
            self.assertTrue(any(r.resolution_status == "uncertain" and "dynamic" in r.note
                                for r in graph.relations))
            self.assertIn(("broken.py", "SyntaxError"), graph.parse_failures)

    def test_router_explains_each_supported_intent_and_uncertain_falls_back(self):
        router = ExplainableQueryRouter()
        cases = (
            ("Which caller imports package.api as api?", "dependency_relation"),
            ("Which callee is invoked by the consumer?", "dependency_relation"),
            ("Which package exports this name in __all__?", "package_api"),
            ("Where is the multi-level re-export for the package API defined?", "package_api"),
            ("Where is class Widget defined?", "exact_symbol"),
            ("How does this repository implement a reservation workflow?", "business_implementation"),
            ("Which document states the retention policy?", "documentation"),
            ("Is there evidence for an unclassified relationship?", "uncertain"),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                decision = router.route(query)
                self.assertEqual(decision.route, expected)
                self.assertTrue(decision.rule)
                self.assertTrue(decision.reason)
                self.assertIn(decision.confidence, {"high", "medium", "low"})
        self.assertEqual(router.route("Is there evidence for X?").channels, ("lexical",))

    def test_router_does_not_assume_dynamic_relationships_are_statically_known(self):
        router = ExplainableQueryRouter()
        decision = router.route("Can a dynamic import resolve this plugin relationship at runtime?")
        self.assertEqual(decision.route, "uncertain")
        self.assertEqual(decision.rule, "dynamic_relation_uncertainty")
        self.assertEqual(decision.channels, ("lexical",))
        self.assertEqual(decision.confidence, "low")
        self.assertIn("cannot be established", decision.reason)

    def test_incremental_add_modify_delete_rename_and_failure_rollback(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            package = root / "pkg"
            package.mkdir()
            init = package / "__init__.py"
            module = package / "impl.py"
            init.write_text("from .impl import first\n__all__ = ['first']\n", encoding="utf-8")
            module.write_text("def first():\n    return 1\n", encoding="utf-8")
            lifecycle = IncrementalPackageRelationIndex()
            first = lifecycle.refresh(root)
            old_ids = {relation.relation_id for relation in lifecycle.graph.relations}
            self.assertEqual(first["status"], "updated")

            module.write_text("def second():\n    return 2\n", encoding="utf-8")
            init.write_text("from .impl import second\n__all__ = ['second']\n", encoding="utf-8")
            changed = lifecycle.refresh(root)
            self.assertEqual(changed["modified"], ["pkg/__init__.py", "pkg/impl.py"])
            self.assertFalse(old_ids & {relation.relation_id for relation in lifecycle.graph.relations})

            extra = package / "extra.py"
            extra.write_text("def added():\n    return 3\n", encoding="utf-8")
            self.assertEqual(lifecycle.refresh(root)["added"], ["pkg/extra.py"])
            extra.rename(package / "renamed.py")
            renamed = lifecycle.refresh(root)
            self.assertEqual(renamed["added"], ["pkg/renamed.py"])
            self.assertEqual(renamed["deleted"], ["pkg/extra.py"])
            (package / "renamed.py").unlink()
            self.assertEqual(lifecycle.refresh(root)["deleted"], ["pkg/renamed.py"])

            stable_before = lifecycle.graph
            broken_builder = IncrementalPackageRelationIndex(builder=lambda _root: (_ for _ in ()).throw(RuntimeError()))
            broken_builder.graph = stable_before
            broken_builder._manifest = dict(lifecycle._manifest)
            module.write_text("def third():\n    return 3\n", encoding="utf-8")
            failed = broken_builder.refresh(root)
            self.assertEqual(failed["status"], "failed_preserved")
            self.assertIs(broken_builder.graph, stable_before)
            self.assertEqual(broken_builder.refresh(root)["status"], "failed_preserved")

    def test_incremental_refresh_reads_and_hashes_each_python_file_once(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / "one.py").write_text("import two\n", encoding="utf-8")
            (root / "two.py").write_text("value = 1\n", encoding="utf-8")
            lifecycle = IncrementalPackageRelationIndex()
            initial = lifecycle.refresh(root)
            self.assertEqual(initial["content_read_count"], 2)
            self.assertEqual(initial["content_hash_count"], 2)
            self.assertEqual(initial["reparsed_file_count"], 2)
            self.assertEqual(initial["ast_cache_hit_count"], 0)

            with patch("agentforge.package_relations.read_candidate",
                       wraps=read_candidate) as read_mock:
                unchanged = lifecycle.refresh(root)
            self.assertEqual(read_mock.call_count, 2)
            self.assertEqual(unchanged["status"], "unchanged")
            self.assertEqual(unchanged["content_read_count"], 2)
            self.assertEqual(unchanged["content_hash_count"], 2)
            self.assertEqual(unchanged["ast_cache_hit_count"], 2)
            self.assertEqual(unchanged["reparsed_file_count"], 0)

    def test_same_size_and_restored_mtime_change_is_detected_by_content_hash(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            package = root / "pkg"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from .api import hydrate\n__all__ = ['hydrate']\n", encoding="utf-8"
            )
            api = package / "api.py"
            api.write_text("def hydrate():\n    return 1\n", encoding="utf-8")
            lifecycle = IncrementalPackageRelationIndex()
            lifecycle.refresh(root)
            before = api.stat()

            api.write_text("def hydrate():\n    return 2\n", encoding="utf-8")
            os.utime(api, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = api.stat()
            self.assertEqual(before.st_size, after.st_size)
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)

            updated = lifecycle.refresh(root)
            self.assertEqual(updated["modified"], ["pkg/api.py"])
            self.assertEqual(updated["content_hash_count"], 2)
            self.assertEqual(updated["reparsed_file_count"], 1)
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))

    def test_missing_corrupt_or_wrong_version_cache_forces_full_rebuild(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / "one.py").write_text("import two\n", encoding="utf-8")
            (root / "two.py").write_text("value = 1\n", encoding="utf-8")
            lifecycle = IncrementalPackageRelationIndex()
            lifecycle.refresh(root)

            lifecycle._parsed_cache.clear()
            missing = lifecycle.refresh(root)
            self.assertTrue(missing["cache_invalidated"])
            self.assertTrue(missing["full_rebuild"])
            self.assertEqual(missing["reparsed_file_count"], 2)
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))

            lifecycle._parsed_cache["one.py"] = ("corrupt", None, None)
            corrupt = lifecycle.refresh(root)
            self.assertTrue(corrupt["cache_invalidated"])
            self.assertTrue(corrupt["full_rebuild"])
            self.assertEqual(corrupt["reparsed_file_count"], 2)
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))

            lifecycle._cache_version += 1
            wrong_version = lifecycle.refresh(root)
            self.assertTrue(wrong_version["cache_invalidated"])
            self.assertTrue(wrong_version["full_rebuild"])
            self.assertEqual(wrong_version["reparsed_file_count"], 2)
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))

    def test_cache_is_not_reused_across_repository_roots(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as first_dir, \
                tempfile.TemporaryDirectory(dir=TEMP_ROOT) as second_dir:
            first, second = Path(first_dir), Path(second_dir)
            (first / "api.py").write_text("def first():\n    return 1\n", encoding="utf-8")
            (second / "api.py").write_text("def second():\n    return 2\n", encoding="utf-8")
            lifecycle = IncrementalPackageRelationIndex()
            lifecycle.refresh(first)
            second_result = lifecycle.refresh(second)
            self.assertTrue(second_result["cache_invalidated"])
            self.assertTrue(second_result["full_rebuild"])
            self.assertEqual(second_result["reparsed_file_count"], 1)
            self.assertEqual(lifecycle.graph, build_package_relation_graph(second))

    def test_interrupted_file_read_preserves_snapshot_and_recovers(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            target = root / "target.py"
            target.write_text("def item():\n    return 1\n", encoding="utf-8")
            lifecycle = IncrementalPackageRelationIndex()
            lifecycle.refresh(root)
            original_graph = lifecycle.graph
            original_manifest = dict(lifecycle._manifest)
            original_cache = dict(lifecycle._parsed_cache)
            target.write_text("def item():\n    return 2\n", encoding="utf-8")

            with patch("agentforge.package_relations.read_candidate",
                       side_effect=ScanReadError("synthetic_read_interruption")):
                interrupted = lifecycle.refresh(root)
            self.assertEqual(interrupted["status"], "failed_preserved")
            self.assertEqual(interrupted["error_type"], "PackageSnapshotReadError")
            self.assertIs(lifecycle.graph, original_graph)
            self.assertEqual(lifecycle._manifest, original_manifest)
            self.assertEqual(lifecycle._parsed_cache, original_cache)

            recovered = lifecycle.refresh(root)
            self.assertEqual(recovered["status"], "updated")
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))

    def test_incremental_delta_rebuilds_transitive_dependents_and_matches_full_graph(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            package = root / "pkg"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from .api import create_invoice as invoice\n__all__ = ['invoice']\n",
                encoding="utf-8",
            )
            (package / "api.py").write_text(
                "from .core import assemble\n"
                "def create_invoice():\n    return assemble()\n", encoding="utf-8"
            )
            (package / "core.py").write_text(
                "def assemble():\n    return 'invoice'\n", encoding="utf-8"
            )
            (root / "client.py").write_text(
                "from pkg import invoice as dispatch\n"
                "def submit():\n    return dispatch()\n", encoding="utf-8"
            )
            (root / "unrelated.py").write_text(
                "def local_note():\n    return 'unrelated'\n", encoding="utf-8"
            )
            lifecycle = IncrementalPackageRelationIndex()
            initial = lifecycle.refresh(root)
            self.assertTrue(initial["full_rebuild"])
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))

            # 这次源码变更会使包级重新导出项及其使用方
            # 失效，即使这两个依赖文件本身没有发生磁盘变更。
            (package / "api.py").write_text(
                "from .core import assemble\n"
                "def issue_invoice():\n    return assemble()\n", encoding="utf-8"
            )
            delta = lifecycle.refresh(root)
            self.assertEqual(delta["modified"], ["pkg/api.py"])
            self.assertFalse(delta["full_rebuild"])
            self.assertEqual(delta["reparsed_file_count"], 1)
            self.assertGreaterEqual(delta["affected_source_count"], 3)
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))
            stale_public = [r for r in lifecycle.graph.relations
                            if r.relation_type == "reexports_symbol"
                            and r.source_path == "pkg/__init__.py"
                            and r.target_name == "invoice"]
            self.assertEqual(len(stale_public), 1)
            self.assertEqual(stale_public[0].resolution_status, "unresolved")

            # 静态 __all__ 更新会移除旧的公开导出关系。
            (package / "__init__.py").write_text(
                "from .api import issue_invoice as invoice\n__all__ = []\n",
                encoding="utf-8",
            )
            export_delta = lifecycle.refresh(root)
            self.assertFalse(export_delta["full_rebuild"])
            self.assertEqual(export_delta["reparsed_file_count"], 1)
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))
            self.assertFalse(any(r.relation_type in {"exports_symbol", "reexports_symbol"}
                                 and r.source_path == "pkg/__init__.py"
                                 for r in lifecycle.graph.relations))

    def test_dynamic_or_parse_incomplete_graph_safely_falls_back_to_full_rebuild(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / "dynamic.py").write_text(
                "import importlib\nvalue = importlib.import_module(name)\n", encoding="utf-8"
            )
            (root / "ordinary.py").write_text("value = 1\n", encoding="utf-8")
            lifecycle = IncrementalPackageRelationIndex()
            self.assertTrue(lifecycle.refresh(root)["full_rebuild"])
            (root / "ordinary.py").write_text("value = 2\n", encoding="utf-8")
            dynamic_update = lifecycle.refresh(root)
            self.assertTrue(dynamic_update["full_rebuild"])
            self.assertEqual(dynamic_update["fallback_reason"], "dynamic_import")
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / "broken.py").write_text("def unfinished(:\n", encoding="utf-8")
            (root / "ordinary.py").write_text("value = 1\n", encoding="utf-8")
            lifecycle = IncrementalPackageRelationIndex()
            lifecycle.refresh(root)
            (root / "ordinary.py").write_text("value = 2\n", encoding="utf-8")
            syntax_update = lifecycle.refresh(root)
            self.assertTrue(syntax_update["full_rebuild"])
            self.assertEqual(syntax_update["fallback_reason"], "parse_failure_or_unreadable_file")
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))

    def test_failed_delta_is_atomic_and_next_refresh_recovers(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / "target.py").write_text("def item():\n    return 1\n", encoding="utf-8")
            (root / "consumer.py").write_text(
                "from target import item\ndef call():\n    return item()\n", encoding="utf-8"
            )
            lifecycle = IncrementalPackageRelationIndex()
            lifecycle.refresh(root)
            old_graph = lifecycle.graph
            old_manifest = dict(lifecycle._manifest)
            old_cache = dict(lifecycle._parsed_cache)
            (root / "target.py").write_text("def item():\n    return 2\n", encoding="utf-8")
            with patch("agentforge.package_relations._update_relation_graph_delta",
                       side_effect=RuntimeError("synthetic update failure")):
                failed = lifecycle.refresh(root)
            self.assertEqual(failed["status"], "failed_preserved")
            self.assertIs(lifecycle.graph, old_graph)
            self.assertEqual(lifecycle._manifest, old_manifest)
            self.assertEqual(lifecycle._parsed_cache, old_cache)
            recovered = lifecycle.refresh(root)
            self.assertEqual(recovered["status"], "updated")
            self.assertEqual(lifecycle.graph, build_package_relation_graph(root))

    def test_test_specific_scanner_limits_do_not_change_production_defaults(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / "a.py").write_text("value = 1\n", encoding="utf-8")
            (root / "b.py").write_text("value = 2\n", encoding="utf-8")
            limits = ScanLimits(max_files=10, max_entries=20, max_total_bytes=10000)
            self.assertEqual(len(build_package_relation_graph(root, scan_limits=limits).relations), 0)
            too_small = ScanLimits(max_files=1, max_entries=20, max_total_bytes=10000)
            with self.assertRaises(ValueError):
                build_package_relation_graph(root, scan_limits=too_small)

    def test_duplicate_refresh_is_idempotent_and_failure_is_reported(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / "one.py").write_text("import two\n", encoding="utf-8")
            (root / "two.py").write_text("value = 1\n", encoding="utf-8")
            lifecycle = IncrementalPackageRelationIndex()
            first = lifecycle.refresh(root)
            second = lifecycle.refresh(root)
            self.assertEqual(first["relation_count"], second["relation_count"])
            self.assertEqual(second["status"], "unchanged")
            self.assertFalse(second["full_rebuild"])

    def test_relation_ids_survive_unrelated_module_addition(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            package = root / "pkg"
            package.mkdir()
            (package / "__init__.py").write_text(
                "from .impl import serve\n__all__ = ['serve']\n", encoding="utf-8"
            )
            (package / "impl.py").write_text("def serve():\n    return 1\n", encoding="utf-8")
            before = build_package_relation_graph(root)
            before_ids = {relation.relation_id for relation in before.relations}
            (package / "unrelated.py").write_text("def noise():\n    return 0\n", encoding="utf-8")
            after = build_package_relation_graph(root)
            after_ids = {relation.relation_id for relation in after.relations}
            self.assertTrue(before_ids <= after_ids)

    def test_offline_channels_have_consistent_coverage_and_test_vector_is_explicit(self):
        result = evaluate_s2()
        self.assertFalse(result["vector_measured"])
        self.assertEqual(result["splits"]["validation"]["retrievers"]["vector"]["status"], "not_measured")
        symbols = result["splits"]["validation"]["retrievers"]["symbols"]
        self.assertEqual(symbols["scored_positive_query_count"], 6)
        self.assertEqual(symbols["not_applicable_query_count"], 51)
        self.assertEqual(symbols["recall_at"]["1"], 1.0)
        self.assertEqual(result["splits"]["validation"]["retrievers"]["lexical"]["no_evidence_query_count"], 30)

        provider = DeterministicTestEmbedding()
        mocked = evaluate_s2(embedding_provider=provider)
        self.assertTrue(mocked["vector_measured"])
        self.assertEqual(mocked["embedding_provider"]["model_id"], provider.model_id)
        self.assertEqual(mocked["splits"]["validation"]["retrievers"]["vector"]["scored_positive_query_count"], 30)
        self.assertGreater(provider.calls, 120)
        self.assertIn("rrf", mocked["splits"]["validation"]["retrievers"])

    def test_scale_repository_100_modules_builds_deterministically(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            _write_scale_repository(Path(directory), 100)
            graph_a = build_package_relation_graph(directory)
            graph_b = build_package_relation_graph(directory)
            self.assertEqual(graph_a.relations, graph_b.relations)
            self.assertGreater(len(graph_a.relations), 100)


if __name__ == "__main__":
    unittest.main()
