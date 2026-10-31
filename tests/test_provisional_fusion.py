import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from agentforge.calibration import calibrate_abstention, evaluate_fixed_rule
from agentforge.evaluation import evaluate_benchmark
from agentforge.fusion import RrfRetriever, fuse_ranked_results
from agentforge.indexer import index_repository
from agentforge.provisional import dataset_split, load_provisional_dataset
from agentforge.retriever import search
from agentforge.store import IndexStore


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests" / "fixtures" / "retrieval_eval_repo"


def candidate(path, score, start=1):
    return SimpleNamespace(
        relative_path=path, start_line=start, end_line=start,
        text=path, score=score,
    )


class ProvisionalDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_provisional_dataset()

    def test_all_ids_splits_and_ai_markers_are_preserved(self):
        queries = self.dataset["queries"]
        self.assertEqual(40, len(queries))
        self.assertEqual(40, len({item["id"] for item in queries}))
        self.assertEqual(20, len(dataset_split(self.dataset, "calibration")["queries"]))
        self.assertEqual(20, len(dataset_split(self.dataset, "validation")["queries"]))
        self.assertTrue(all(item["review_status"] == "provisional_ai" for item in queries))
        self.assertTrue(all(item["review_method"] == "ai_evidence_review" for item in queries))
        self.assertTrue(all(item["human_reviewed"] is False for item in queries))

        calibration_groups = {
            item["leakage_group"] for item in queries if item["split"] == "calibration"
        }
        validation_groups = {
            item["leakage_group"] for item in queries if item["split"] == "validation"
        }
        self.assertFalse(calibration_groups & validation_groups)

    def test_three_queries_are_rewritten_and_traceable(self):
        rewritten = [item for item in self.dataset["queries"] if item["ai_decision"] == "rewrite_query"]
        self.assertEqual(
            {item["id"] for item in rewritten},
            {"candidate-coordinator-renew", "candidate-theme-save", "candidate-token-policy-save"},
        )
        for item in rewritten:
            self.assertNotEqual(item["query"], item["original_query"])
            self.assertTrue(item["evidence"])
            self.assertTrue(item["relevant_paths"])

    def test_provisional_dataset_is_evaluable_without_changing_sources(self):
        with IndexStore(":memory:") as store:
            index_repository(CORPUS, store)
            report = evaluate_benchmark(store, self.dataset)
        self.assertEqual("agentforge-retrieval-provisional-v3", report["dataset_version"])
        self.assertEqual("provisional_ai", report["review_status"])
        self.assertEqual(40, report["query_total"])
        self.assertEqual(20, report["no_evidence_query_count"])

    def test_real_experiment_artifacts_remain_provisional_and_cleanup_is_recorded(self):
        result_path = ROOT / "AF009FA_RRF_RESULTS.json"
        service_path = ROOT / "AF009FA_SERVICE_RUN.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        service = json.loads(service_path.read_text(encoding="utf-8"))
        self.assertEqual("provisional_ai_experiment_only", result["status"])
        self.assertIs(result["human_reviewed"], False)
        self.assertIs(result["production_threshold_approved"], False)
        self.assertEqual((20, 20), (
            result["dataset"]["calibration_count"], result["dataset"]["validation_count"],
        ))
        self.assertEqual(
            result["calibration"]["selected"]["rule"], result["validation"]["rule"]
        )
        self.assertEqual("completed_and_cleaned", service["status"])
        self.assertTrue(service["cleanup"]["process_stopped"])
        self.assertEqual(0, service["cleanup"]["port_listener_count_for_pid_after_stop"])
        self.assertTrue(service["cleanup"]["temporary_runtime_removed"])
        self.assertFalse((ROOT / "tests" / ".tmp" / "af009fa-runtime").exists())


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"query_id": "p1", "top_score": 0.9, "top_2_margin": 0.2, "no_evidence": False},
            {"query_id": "p2", "top_score": 0.6, "top_2_margin": 0.1, "no_evidence": False},
            {"query_id": "n1", "top_score": 0.5, "top_2_margin": 0.3, "no_evidence": True},
            {"query_id": "n2", "top_score": 0.2, "top_2_margin": 0.01, "no_evidence": True},
        ]

    def test_calibration_uses_explicit_zero_false_accept_protocol(self):
        report = calibrate_abstention(self.rows, thresholds=(0.0, 0.5, 0.75))
        selected = report["selected"]
        self.assertEqual("exploratory_provisional_ai_only", report["status"])
        self.assertEqual(0.0, selected["false_accept_rate"])
        self.assertEqual(0.25, selected["coverage"])
        self.assertEqual("top_score", selected["rule"]["family"])
        self.assertEqual(0.75, selected["rule"]["top_score_threshold"])

    def test_fixed_validation_rule_does_not_select_or_mutate(self):
        rule = {"family": "combined", "top_score_threshold": 0.5,
                "top_2_margin_threshold": 0.1}
        before = json.dumps(rule, sort_keys=True)
        result = evaluate_fixed_rule(self.rows, rule)
        self.assertEqual(before, json.dumps(rule, sort_keys=True))
        self.assertEqual({"total": 4, "evidence": 2, "no_evidence": 2}, result["sample_counts"])
        self.assertEqual(0.5, result["false_accept_rate"])
        self.assertEqual(0.0, result["false_reject_rate"])

    def test_invalid_or_duplicate_calibration_rows_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            calibrate_abstention([self.rows[0], self.rows[0]])
        with self.assertRaisesRegex(ValueError, "finite"):
            evaluate_fixed_rule(
                [{**self.rows[0], "top_score": float("nan")}],
                {"family": "top_score", "top_score_threshold": 0.5},
            )


class FusionTests(unittest.TestCase):
    def test_rrf_deduplicates_by_stable_file_identity_and_preserves_channels(self):
        fused = fuse_ranked_results({
            "lexical": [candidate("a.py", 9), candidate("a.py", 8, 2), candidate("b.py", 7)],
            "vector": [candidate("b.py", 0.9), candidate("a.py", 0.8)],
            "symbols": [],
        }, k=10, top_k=5)
        self.assertEqual([item.relative_path for item in fused], ["a.py", "b.py"])
        self.assertEqual("file:a.py", fused[0].identity)
        self.assertEqual({item.channel for item in fused[0].channels}, {"lexical", "vector"})
        self.assertEqual(2, len(fused[0].channels))
        self.assertAlmostEqual(fused[0].score, 1 / 11 + 1 / 12)
        self.assertEqual([], fuse_ranked_results({}, k=60, top_k=5))
        self.assertEqual(
            ["a.py"],
            [item.relative_path for item in fuse_ranked_results(
                {"lexical": [candidate("a.py", 1.0)]}, top_k=1
            )],
        )

    def test_explicit_rrf_does_not_change_default_and_channel_failure_propagates_once(self):
        with IndexStore(":memory:") as store:
            index_repository(CORPUS, store)
            before = search(store, "rotate session", top_k=3)
            rrf = RrfRetriever(store, vector_retriever=None)
            results = rrf.search("rotate session", top_k=3)
            after = search(store, "rotate session", top_k=3)
            self.assertEqual(before, after)
            self.assertTrue(results)

            class FailingVector:
                calls = 0

                def search(self, query, *, top_k):
                    self.calls += 1
                    raise RuntimeError("offline vector failure")

            failing = FailingVector()
            with self.assertRaisesRegex(RuntimeError, "offline vector failure"):
                RrfRetriever(store, vector_retriever=failing).search("rotate session", top_k=3)
            self.assertEqual(1, failing.calls)

    def test_evaluator_reports_rrf_separately(self):
        dataset = load_provisional_dataset()
        with IndexStore(":memory:") as store:
            index_repository(CORPUS, store)
            rrf = RrfRetriever(store, vector_retriever=None)
            report = evaluate_benchmark(
                store, dataset,
                rrf_search=lambda query, *, top_k, symbol_query=None: rrf.search(
                    query, top_k=top_k, symbol_query=symbol_query
                ),
            )
        self.assertIn("rrf", report["retrievers"])
        self.assertEqual(20, report["retrievers"]["rrf"]["grounded_query_count"])
        self.assertEqual("not_measured", report["retrievers"]["vector"]["status"])


if __name__ == "__main__":
    unittest.main()
