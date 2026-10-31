import copy
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest

from agentforge.evaluation import evaluate_abstention, evaluate_benchmark, validate_split_plan
from agentforge.indexer import index_repository
from agentforge.store import IndexStore
from tests.support import LocalTestCase, snapshot


DATASET_PATH = Path(__file__).parent / "fixtures" / "retrieval_eval.json"
CANDIDATE_DATASET_PATH = (
    Path(__file__).parent / "fixtures" / "retrieval_eval_expansion_candidates.json"
)
REVIEW_POOL_PATH = Path(__file__).parent / "fixtures" / "retrieval_eval_review_pool_v2.json"
SPLIT_PLAN_PATH = Path(__file__).parent / "fixtures" / "retrieval_eval_split_plan_v2.json"
CORPUS_PATH = Path(__file__).parent / "fixtures" / "retrieval_eval_repo"


class EvaluationTests(LocalTestCase):
    def setUp(self):
        super().setUp()
        with DATASET_PATH.open("r", encoding="utf-8") as stream:
            self.dataset = json.load(stream)
        self.store = IndexStore(":memory:")
        self.addCleanup(self.store.close)
        self.fixture_before = snapshot(CORPUS_PATH)
        index_repository(CORPUS_PATH, self.store)

    def test_manual_ground_truth_metrics_and_report_are_reproducible(self):
        first = evaluate_benchmark(self.store, self.dataset)
        second = evaluate_benchmark(self.store, self.dataset)
        self.assertEqual(first, second)
        self.assertEqual(first["dataset_version"], "agentforge-retrieval-v1")
        self.assertEqual(first["query_total"], 9)
        self.assertEqual(first["no_evidence_query_count"], 1)
        query_words = set(re.findall(
            r"[a-z]+", next(item["query"] for item in self.dataset["queries"]
                            if item["id"] == "business-concept").lower()
        ))
        implementation_words = set(re.findall(
            r"[a-z]+", (CORPUS_PATH / "pkg" / "session.py").read_text(encoding="utf-8").lower()
        ))
        stop_words = {"how", "an", "a", "in", "the", "is", "for", "to", "of"}
        self.assertFalse((query_words & implementation_words) - stop_words)
        lexical = first["retrievers"]["lexical"]
        self.assertEqual((lexical["grounded_query_count"], lexical["valid_label_count"]), (8, 10))
        self.assertEqual(lexical["recall_at"], {"1": 0.75, "5": 0.9375, "10": 0.9375})
        self.assertEqual(lexical["mrr"], 0.9375)
        self.assertEqual(
            lexical["by_category"]["cross_file_import_reference"]["recall_at"]["1"], 0.0
        )
        symbols = first["retrievers"]["symbols"]
        self.assertEqual((symbols["grounded_query_count"], symbols["valid_label_count"]), (7, 8))
        self.assertEqual(symbols["recall_at"], {"1": 0.928571, "5": 1.0, "10": 1.0})
        self.assertEqual(symbols["mrr"], 1.0)
        concept = symbols["by_category"]["business_concept_without_shared_terms"]
        self.assertEqual(concept["mrr"], None)
        check = first["no_evidence_checks"][0]
        self.assertTrue(check["lexical_returned_empty"])
        self.assertTrue(check["symbols_returned_empty"])
        self.assertEqual(first["retrievers"]["vector"]["status"], "not_measured")
        self.assertEqual(snapshot(CORPUS_PATH), self.fixture_before)

    def test_reviewed_v1_fixture_is_byte_for_byte_unchanged(self):
        self.assertEqual(
            hashlib.sha256(DATASET_PATH.read_bytes()).hexdigest().upper(),
            "B3276E4309C08FAA8B8961DB8DDD457A6ABA7491C134E606FC037B0B1342AF87",
        )

    def test_expansion_candidates_are_separate_pending_and_line_addressable(self):
        with CANDIDATE_DATASET_PATH.open("r", encoding="utf-8") as stream:
            candidates = json.load(stream)
        report = evaluate_benchmark(self.store, candidates)
        self.assertEqual(report["dataset_version"], "agentforge-retrieval-v1-expansion-candidates")
        self.assertEqual(report["review_status"], "pending_human_review")
        self.assertEqual(report["query_total"], 12)
        self.assertEqual(report["no_evidence_query_count"], 4)
        self.assertTrue(all(
            item["review_status"] == "pending_human_review"
            for item in candidates["queries"]
        ))
        self.assertEqual(snapshot(CORPUS_PATH), self.fixture_before)

    def test_expansion_candidate_cannot_claim_review_or_use_invalid_evidence_lines(self):
        with CANDIDATE_DATASET_PATH.open("r", encoding="utf-8") as stream:
            candidates = json.load(stream)
        broken = copy.deepcopy(candidates)
        broken["queries"][0]["review_status"] = "human_reviewed"
        with self.assertRaisesRegex(ValueError, "pending_human_review"):
            evaluate_benchmark(self.store, broken)
        broken = copy.deepcopy(candidates)
        broken["queries"][0]["evidence"][0]["end_line"] = 999
        with self.assertRaisesRegex(ValueError, "outside the indexed fixture"):
            evaluate_benchmark(self.store, broken)

    def test_v2_review_pool_retains_parent_and_has_balanced_pending_labels(self):
        with CANDIDATE_DATASET_PATH.open("r", encoding="utf-8") as stream:
            parent = json.load(stream)
        with REVIEW_POOL_PATH.open("r", encoding="utf-8") as stream:
            pool = json.load(stream)
        parent_by_id = {item["id"]: item for item in parent["queries"]}
        pool_by_id = {item["id"]: item for item in pool["queries"]}
        self.assertEqual(len(pool_by_id), 40)
        self.assertTrue(set(parent_by_id) <= set(pool_by_id))
        for query_id, old in parent_by_id.items():
            new = pool_by_id[query_id]
            for field in ("query", "category", "relevant_paths", "relevant_symbols", "no_evidence"):
                self.assertEqual(new.get(field), old.get(field), (query_id, field))
        positives = [item for item in pool["queries"] if not item.get("no_evidence", False)]
        negatives = [item for item in pool["queries"] if item.get("no_evidence", False)]
        self.assertEqual((len(positives), len(negatives)), (20, 20))
        self.assertTrue(all(item["review_status"] == "pending_human_review" for item in pool["queries"]))
        self.assertTrue(all(item["evidence"] for item in positives))
        self.assertTrue(all(item["scope_checked"] for item in negatives))
        report = evaluate_benchmark(self.store, pool)
        self.assertEqual(report["query_total"], 40)
        self.assertEqual(report["no_evidence_query_count"], 20)

    def test_v2_review_pool_and_split_hashes_and_group_isolation(self):
        self.assertEqual(
            hashlib.sha256(CANDIDATE_DATASET_PATH.read_bytes()).hexdigest().upper(),
            "877B30B0F7827243CA04C79450C3116059033557F2DBFA3F408CDA53EB6154C1",
        )
        self.assertEqual(
            hashlib.sha256(REVIEW_POOL_PATH.read_bytes()).hexdigest().upper(),
            "0FBB43BF71B2A6BCD79733670C2D87B6C43FA767135EB2008091F4B291D15D4D",
        )
        with REVIEW_POOL_PATH.open("r", encoding="utf-8") as stream:
            pool = json.load(stream)
        with SPLIT_PLAN_PATH.open("r", encoding="utf-8") as stream:
            plan = json.load(stream)
        self.assertEqual(plan["dataset_sha256"], hashlib.sha256(REVIEW_POOL_PATH.read_bytes()).hexdigest().upper())
        report = validate_split_plan(pool, plan)
        self.assertTrue(report["groups_are_disjoint"])
        self.assertEqual(
            report["summary"],
            {
                "calibration": {"query_count": 20, "evidence_count": 10, "no_evidence_count": 10,
                                "near_domain_negative_count": 8, "leakage_group_count": 12},
                "validation": {"query_count": 20, "evidence_count": 10, "no_evidence_count": 10,
                               "near_domain_negative_count": 10, "leakage_group_count": 13},
            },
        )

    def test_split_rejects_group_leakage_and_missing_assignments(self):
        with REVIEW_POOL_PATH.open("r", encoding="utf-8") as stream:
            pool = json.load(stream)
        with SPLIT_PLAN_PATH.open("r", encoding="utf-8") as stream:
            plan = json.load(stream)
        broken = copy.deepcopy(plan)
        moved = broken["splits"]["calibration"].pop(0)
        broken["splits"]["validation"].append(moved)
        with self.assertRaisesRegex(ValueError, "Leakage group"):
            validate_split_plan(pool, broken)
        broken = copy.deepcopy(plan)
        broken["splits"]["validation"].pop()
        with self.assertRaisesRegex(ValueError, "assign every candidate"):
            validate_split_plan(pool, broken)

    def test_abstention_metrics_have_explicit_denominators(self):
        report = evaluate_abstention([
            {"top_score": 0.8, "top_2_margin": 0.2, "no_evidence": False},
            {"top_score": 0.4, "top_2_margin": 0.05, "no_evidence": False},
            {"top_score": 0.7, "top_2_margin": 0.01, "no_evidence": True},
            {"top_score": 0.2, "top_2_margin": 0.02, "no_evidence": True},
        ], thresholds=(0.5,))
        row = report["thresholds"][0]
        self.assertEqual(row["coverage"], 0.5)
        self.assertEqual(row["precision"], 0.5)
        self.assertEqual(row["false_accept_rate"], 0.5)
        self.assertEqual(row["false_reject_rate"], 0.5)
        self.assertIn("Too few", report["calibration_warning"])

    def test_vector_evaluator_hook_uses_same_file_ground_truth(self):
        def fake_vector(query, *, top_k):
            path = "pkg/session.py" if "session" in query.lower() else "pkg/handlers.py"
            return [SimpleNamespace(relative_path=path, score=0.75)][:top_k]

        report = evaluate_benchmark(self.store, self.dataset, vector_search=fake_vector)
        self.assertIn("mrr", report["retrievers"]["vector"])
        self.assertEqual(report["retrievers"]["vector"]["grounded_query_count"], 8)
        self.assertEqual(report["vector_abstention"]["sample_counts"]["no_evidence"], 1)

    def test_missing_manual_labels_are_rejected(self):
        broken = copy.deepcopy(self.dataset)
        broken["queries"][0]["relevant_paths"] = ["../outside.py"]
        with self.assertRaisesRegex(ValueError, "ground-truth path is absent"):
            evaluate_benchmark(self.store, broken)
        broken = copy.deepcopy(self.dataset)
        broken["queries"][0]["relevant_symbols"][0]["qualified_name"] = "made.up.symbol"
        with self.assertRaisesRegex(ValueError, "ground-truth symbol is absent"):
            evaluate_benchmark(self.store, broken)

    def test_duplicate_query_ids_and_malformed_symbols_are_rejected(self):
        broken = copy.deepcopy(self.dataset)
        broken["queries"][1]["id"] = broken["queries"][0]["id"]
        with self.assertRaisesRegex(ValueError, "Duplicate evaluation query id"):
            evaluate_benchmark(self.store, broken)
        broken = copy.deepcopy(self.dataset)
        broken["queries"][0]["relevant_symbols"] = [{"path": "pkg/session.py"}]
        with self.assertRaisesRegex(ValueError, "path and qualified_name"):
            evaluate_benchmark(self.store, broken)

    def test_unlabelled_queries_and_duplicate_manual_labels_are_rejected(self):
        broken = copy.deepcopy(self.dataset)
        broken["queries"][0]["relevant_paths"] = []
        broken["queries"][0]["relevant_symbols"] = []
        with self.assertRaisesRegex(ValueError, "manual relevance label"):
            evaluate_benchmark(self.store, broken)
        broken = copy.deepcopy(self.dataset)
        broken["queries"][0]["relevant_paths"].append("pkg/session.py")
        with self.assertRaisesRegex(ValueError, "duplicate ground-truth path"):
            evaluate_benchmark(self.store, broken)


if __name__ == "__main__":
    unittest.main()
