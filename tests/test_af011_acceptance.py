import json
from pathlib import Path
import unittest

from agentforge.product_acceptance import evaluate_product_tasks, load_and_validate_tasks


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "tests" / "fixtures" / "af011_product_tasks_v4.json"


class AF011AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temp_parent_created = not (ROOT / "tests" / ".tmp").exists()
        (ROOT / "tests" / ".tmp").mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        if cls._temp_parent_created:
            try:
                (ROOT / "tests" / ".tmp").rmdir()
            except OSError:
                pass

    def test_task_dataset_is_valid_and_split_groups_are_disjoint(self):
        data = load_and_validate_tasks(DATASET, ROOT)
        self.assertEqual(data["dataset_version"], "af011-agentforge-source-tasks-v4")
        self.assertEqual(data["label_source"], "ai_evidence_review")
        self.assertFalse(data["human_reviewed"])
        self.assertEqual(len(data["tasks"]), 20)
        self.assertTrue(all(task["review_status"] == "ai_reviewed_not_human"
                            and task["human_reviewed"] is False for task in data["tasks"]))
        groups = {}
        for task in data["tasks"]:
            groups.setdefault(task["leakage_group"], set()).add(task["split"])
        self.assertTrue(all(len(value) == 1 for value in groups.values()))
        self.assertEqual(
            {split: sum(task["split"] == split for task in data["tasks"])
             for split in ("calibration", "validation")},
            {"calibration": 10, "validation": 10},
        )

    def test_offline_acceptance_is_reproducible_and_does_not_use_rrf_or_vector(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT / "tests" / ".tmp") as temp:
            temp_root = Path(temp)
            repository = temp_root / "repo"
            repository.mkdir()
            (repository / "mod.py").write_text(
                "def stable_symbol():\n    return 1\n", encoding="utf-8"
            )
            dataset = temp_root / "tasks.json"
            dataset.write_text(json.dumps({
                "dataset_version": "af011-test-v1",
                "human_reviewed": False,
                "label_source": "deterministic_test",
                "tasks": [
                    {"id": "C1", "split": "calibration", "leakage_group": "g1",
                     "category": "exact_symbol", "query": "stable_symbol",
                     "expected_route": "exact_symbol",
                     "relevant": [{"path": "mod.py", "start_line": 1, "end_line": 2}]},
                    {"id": "V1", "split": "validation", "leakage_group": "g2",
                     "category": "no_evidence_exact", "query": "missing_symbol",
                     "expected_route": "exact_symbol", "relevant": []},
                ],
            }), encoding="utf-8")
            first = evaluate_product_tasks(repository, dataset)
            second = evaluate_product_tasks(repository, dataset)
        for result in (first, second):
            self.assertFalse(result["rrf_used"])
            self.assertFalse(result["production_vector_used"])
            self.assertFalse(result["threshold_used"])
            self.assertEqual(result["task_count"], 2)
            self.assertEqual(result["splits"]["validation"]["evidence_contract_valid_rate"], 1.0)
            self.assertEqual(result["splits"]["validation"]["strict_refusal_rate_on_no_evidence"], 1.0)
            self.assertEqual(result["splits"]["validation"]["model_citation_validation"],
                             "not_measured_offline_provider_does_not_generate_claims")
        scrub = lambda value: [
            {key: item[key] for key in item if key != "latency_seconds"}
            for item in value["query_results"]
        ]
        self.assertEqual(scrub(first), scrub(second))

    def test_invalid_evidence_and_split_leakage_fail_closed(self):
        data = json.loads(DATASET.read_text(encoding="utf-8"))
        # Validate through isolated copies without touching the frozen task fixture.
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT / "tests" / ".tmp") as temp:
            path = Path(temp) / "bad.json"
            bad = json.loads(json.dumps(data))
            bad["tasks"][0]["relevant"][0]["path"] = "../outside.py"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_and_validate_tasks(path, ROOT)
            bad = json.loads(json.dumps(data))
            bad["tasks"][1]["leakage_group"] = bad["tasks"][-1]["leakage_group"]
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_and_validate_tasks(path, ROOT)


if __name__ == "__main__":
    unittest.main()
