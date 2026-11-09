import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CORPUS = FIXTURES / "retrieval_eval_repo"
SOURCE = FIXTURES / "retrieval_eval_review_pool_v2.json"
SPLIT = FIXTURES / "retrieval_eval_split_plan_v2.json"
ADJUDICATED = FIXTURES / "retrieval_eval_review_pool_v2_ai_adjudicated.json"

FROZEN_HASHES = {
    "retrieval_eval.json": "B3276E4309C08FAA8B8961DB8DDD457A6ABA7491C134E606FC037B0B1342AF87",
    "retrieval_eval_expansion_candidates.json": "877B30B0F7827243CA04C79450C3116059033557F2DBFA3F408CDA53EB6154C1",
    "retrieval_eval_review_pool_v2.json": "0FBB43BF71B2A6BCD79733670C2D87B6C43FA767135EB2008091F4B291D15D4D",
    "retrieval_eval_split_plan_v2.json": "69E5F08C36FCC22A2BDA9BE5FA4E1A27493DFD09C43A06E624C8CDC696F66D6A",
}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class AiAdjudicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = load(SOURCE)
        cls.plan = load(SPLIT)
        cls.artifact = load(ADJUDICATED)

    def test_frozen_input_hashes_are_unchanged(self):
        for name, expected in FROZEN_HASHES.items():
            actual = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest().upper()
            self.assertEqual(expected, actual, name)

    def test_all_candidates_are_preserved_once_with_ai_only_status(self):
        source_by_id = {item["id"]: item for item in self.source["queries"]}
        records = self.artifact["records"]
        record_ids = [item["id"] for item in records]
        self.assertEqual(40, len(records))
        self.assertEqual(40, len(set(record_ids)))
        self.assertEqual(set(source_by_id), set(record_ids))
        self.assertEqual("ai_review_completed_pending_human_review", self.artifact["status"])
        self.assertEqual("ai_evidence_review", self.artifact["review_method"])
        self.assertIs(self.artifact["human_reviewed"], False)

        split_by_id = {}
        for split_name, ids in self.plan["splits"].items():
            for candidate_id in ids:
                self.assertNotIn(candidate_id, split_by_id)
                split_by_id[candidate_id] = split_name

        allowed = {
            "accept_current", "modify_relevant_set", "insufficient_evidence",
            "no_evidence", "rewrite_query",
        }
        for record in records:
            original = source_by_id[record["id"]]
            self.assertEqual(original["query"], record["query"])
            self.assertEqual(original["category"], record["category"])
            self.assertEqual(original["leakage_group"], record["leakage_group"])
            self.assertEqual(split_by_id[record["id"]], record["split"])
            self.assertIn(record["decision"], allowed)
            self.assertIn(record["confidence"], {"high", "medium", "low"})
            self.assertEqual("ai_evidence_review", record["review_method"])
            self.assertIs(record["human_reviewed"], False)

            original_kind = "no_evidence" if original.get("no_evidence", False) else "evidence"
            self.assertEqual(original_kind, record["original_label"]["kind"])
            self.assertEqual(original.get("relevant_paths", []), record["original_label"]["relevant_paths"])
            original_symbols = [item["qualified_name"] for item in original.get("relevant_symbols", [])]
            self.assertEqual(original_symbols, record["original_label"]["relevant_symbols"])

    def test_evidence_paths_ranges_scope_and_decision_counts_are_valid(self):
        corpus_root = CORPUS.resolve()
        actual_scope = sorted(
            path.relative_to(CORPUS).as_posix()
            for path in CORPUS.rglob("*")
            if path.is_file() and "__pycache__" not in path.relative_to(CORPUS).parts
            and path.suffix.casefold() not in {".pyc", ".pyo"}
        )
        self.assertEqual(actual_scope, sorted(self.artifact["frozen_scope"]))

        counts = {key: 0 for key in self.artifact["decision_counts"]}
        for record in self.artifact["records"]:
            counts[record["decision"]] += 1
            label_paths = set(record["suggested_label"]["relevant_paths"])
            evidence_paths = set()
            for evidence in record["suggested_evidence"]:
                candidate = (CORPUS / evidence["path"]).resolve()
                self.assertTrue(candidate.is_relative_to(corpus_root), record["id"])
                self.assertTrue(candidate.is_file(), record["id"])
                lines = candidate.read_text(encoding="utf-8").splitlines()
                self.assertGreaterEqual(evidence["start_line"], 1)
                self.assertGreaterEqual(evidence["end_line"], evidence["start_line"])
                self.assertLessEqual(evidence["end_line"], len(lines), record["id"])
                self.assertTrue(evidence["excerpt"].strip())
                evidence_paths.add(evidence["path"])

            if record["decision"] == "no_evidence":
                self.assertEqual("frozen_scope", record["scope_reference"])
                self.assertEqual(set(), label_paths)
                self.assertEqual([], record["suggested_evidence"])
            else:
                self.assertTrue(label_paths)
                self.assertEqual(label_paths, evidence_paths)
            if record["decision"] == "rewrite_query":
                self.assertTrue(record["suggested_query"].strip())
                self.assertNotEqual(record["query"], record["suggested_query"])

        self.assertEqual(self.artifact["decision_counts"], counts)
        self.assertEqual(17, counts["accept_current"])
        self.assertEqual(20, counts["no_evidence"])
        self.assertEqual(3, counts["rewrite_query"])

    def test_split_ids_and_leakage_groups_remain_isolated(self):
        source_by_id = {item["id"]: item for item in self.source["queries"]}
        calibration = set(self.plan["splits"]["calibration"])
        validation = set(self.plan["splits"]["validation"])
        self.assertFalse(calibration & validation)
        self.assertEqual(set(source_by_id), calibration | validation)

        calibration_groups = {source_by_id[item]["leakage_group"] for item in calibration}
        validation_groups = {source_by_id[item]["leakage_group"] for item in validation}
        self.assertFalse(calibration_groups & validation_groups)

        adjudicated = {record["id"]: record for record in self.artifact["records"]}
        for candidate_id in calibration:
            self.assertEqual("calibration", adjudicated[candidate_id]["split"])
        for candidate_id in validation:
            self.assertEqual("validation", adjudicated[candidate_id]["split"])

    def test_uncertain_or_rewritten_items_are_not_formal_approvals(self):
        attention = [
            record for record in self.artifact["records"]
            if record["decision"] in {"rewrite_query", "insufficient_evidence"}
            or record["confidence"] in {"medium", "low"}
        ]
        self.assertEqual(5, len(attention))
        self.assertTrue(all(record["human_reviewed"] is False for record in attention))
        self.assertNotIn("human_reviewed", self.artifact["status"].replace("pending_human_review", ""))


if __name__ == "__main__":
    unittest.main()
