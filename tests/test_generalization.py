import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

from agentforge.generalization import (
    DEFAULT_CORPUS,
    DEFAULT_SPLIT_PLAN,
    _no_evidence_summary,
    corpus_manifest,
    evaluate_generalization,
    load_generalization_dataset,
    validate_evidence,
    validate_generalization_split,
)
from agentforge.fusion import fuse_ranked_results
from agentforge.indexer import index_repository
from agentforge.store import IndexStore


ROOT = Path(__file__).resolve().parents[1]


def _candidate(path, score, start=1):
    return SimpleNamespace(
        relative_path=path, start_line=start, end_line=start,
        text=path, score=score,
    )


class GeneralizationFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_generalization_dataset()
        cls.plan = __import__("json").loads(DEFAULT_SPLIT_PLAN.read_text(encoding="utf-8"))

    def test_independent_fixture_has_40_unique_ai_provisional_candidates(self):
        queries = self.dataset["queries"]
        self.assertEqual(40, len(queries))
        self.assertEqual(40, len({item["id"] for item in queries}))
        self.assertEqual(20, sum(not item["no_evidence"] for item in queries))
        self.assertEqual(20, sum(item["no_evidence"] for item in queries))
        self.assertEqual(19, sum(item["difficulty"] == "near_domain" for item in queries if item["no_evidence"]))
        self.assertEqual(1, sum(item["difficulty"] == "out_of_domain" for item in queries if item["no_evidence"]))
        self.assertTrue(all(item["review_method"] == "ai_evidence_review" for item in queries))
        self.assertTrue(all(item["human_reviewed"] is False for item in queries))
        self.assertTrue(all(item["review_status"] == "provisional_ai" for item in queries))

    def test_all_positive_evidence_paths_and_line_ranges_are_inside_snapshot(self):
        result = validate_evidence(self.dataset, DEFAULT_CORPUS)
        self.assertEqual(40, result["query_count"])
        self.assertGreaterEqual(result["verified_evidence_ranges"], 20)
        manifest = corpus_manifest(DEFAULT_CORPUS)
        self.assertEqual(13, manifest["file_count"])
        self.assertEqual(manifest, corpus_manifest(DEFAULT_CORPUS))

    def test_invalid_evidence_range_is_rejected(self):
        altered = copy.deepcopy(self.dataset)
        positive = next(item for item in altered["queries"] if not item["no_evidence"])
        positive["evidence"][0]["end_line"] = 999
        with self.assertRaisesRegex(ValueError, "exceeds"):
            validate_evidence(altered, DEFAULT_CORPUS)

    def test_fixed_group_split_is_balanced_complete_and_disjoint(self):
        result = validate_generalization_split(self.dataset, self.plan)
        self.assertTrue(result["groups_are_disjoint"])
        self.assertEqual(
            {"query_count": 20, "evidence": 10, "no_evidence": 10,
             "near_domain_negative": 10, "out_of_domain_negative": 0, "leakage_group_count": 5},
            result["summary"]["calibration"],
        )
        self.assertEqual(
            {"query_count": 20, "evidence": 10, "no_evidence": 10,
             "near_domain_negative": 9, "out_of_domain_negative": 1, "leakage_group_count": 5},
            result["summary"]["validation"],
        )
        altered = copy.deepcopy(self.plan)
        altered["splits"]["validation"].append(altered["splits"]["calibration"].pop())
        with self.assertRaises(ValueError):
            validate_generalization_split(self.dataset, altered)

    def test_lexical_symbol_and_rrf_use_same_split_labels_without_vectors(self):
        with IndexStore(":memory:") as store:
            indexed = index_repository(DEFAULT_CORPUS, store)
            self.assertEqual(13, indexed["files"])
            report = evaluate_generalization()
        self.assertEqual(20, report["calibration"]["query_count"])
        self.assertEqual(20, report["validation"]["query_count"])
        self.assertEqual("not_measured", report["calibration"]["retrievers"]["vector"]["status"])
        for split in ("calibration", "validation"):
            self.assertIn("lexical", report[split]["retrievers"])
            self.assertIn("symbols", report[split]["retrievers"])
            self.assertIn("rrf", report[split]["retrievers"])
            self.assertEqual(10, report[split]["retrievers"]["lexical"]["grounded_query_count"])
            self.assertEqual(20, report[split]["retrievers"]["lexical"]["timing"]["measured_call_count"])
            self.assertIsNone(report[split]["no_evidence_returns"]["vector"]["non_empty_return_rate"])
        self.assertEqual("not_decidable_without_real_vector_measurement", report["rrf_decision"]["status"])
        self.assertFalse(report["production_threshold_enabled"])

    def test_no_evidence_rate_uses_only_applicable_query_denominator(self):
        dataset = {"queries": [
            {"id": "n1", "no_evidence": True, "difficulty": "near_domain", "symbol_query": {"name": "missing"}},
            {"id": "n2", "no_evidence": True, "difficulty": "near_domain"},
        ]}
        report = {"retrievers": {
            "lexical": {"query_results": [{"query_id": "n1", "ranked": ["a.py"]}, {"query_id": "n2", "ranked": []}]},
            "symbols": {"query_results": [{"query_id": "n1", "ranked": []}]},
            "vector": {"status": "not_measured", "reason": "test"},
            "rrf": {"query_results": [{"query_id": "n1", "ranked": ["a.py"]}, {"query_id": "n2", "ranked": ["a.py"]}]},
        }}
        summary = _no_evidence_summary(report, dataset)
        self.assertEqual((1, 2), (
            summary["lexical"]["non_empty_returns"],
            summary["lexical"]["applicable_no_evidence_queries"],
        ))
        self.assertEqual((0, 1, 1), (
            summary["symbols"]["non_empty_returns"],
            summary["symbols"]["applicable_no_evidence_queries"],
            summary["symbols"]["not_applicable_no_evidence_queries"],
        ))
        self.assertEqual("not_measured", summary["vector"]["status"])

    def test_rrf_missing_channels_deduplicates_and_orders_stably(self):
        first = fuse_ranked_results({
            "lexical": [_candidate("a.py", 5), _candidate("a.py", 4), _candidate("b.py", 3)],
        }, k=60, top_k=10)
        second = fuse_ranked_results({
            "lexical": [_candidate("a.py", 5), _candidate("a.py", 4), _candidate("b.py", 3)],
            "vector": [],
            "symbols": [],
        }, k=60, top_k=10)
        self.assertEqual([item.relative_path for item in first], ["a.py", "b.py"])
        self.assertEqual([item.relative_path for item in first], [item.relative_path for item in second])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
