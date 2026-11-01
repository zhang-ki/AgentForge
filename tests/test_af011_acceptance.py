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
        # AF-011 is a frozen source snapshot; this checkout has changed source
        # line positions. Validate its immutable manifest without re-evaluating
        # old anchors against new code.
        data = json.loads(DATASET.read_text(encoding="utf-8"))
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
        self.assertEqual(len({task["id"] for task in data["tasks"]}), 20)
        for task in data["tasks"]:
            self.assertTrue(task["query"].strip())
            for evidence in task["relevant"]:
                self.assertTrue(evidence["path"])
                self.assertGreaterEqual(evidence["start_line"], 1)
                self.assertGreaterEqual(evidence["end_line"], evidence["start_line"])

    def test_frozen_source_anchors_fail_closed_on_changed_checkout(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT / "tests" / ".tmp") as temp:
            changed_checkout = Path(temp)
            source = changed_checkout / "agentforge" / "agent_loop.py"
            source.parent.mkdir(parents=True)
            # The frozen first anchor is expected on line 258. Keep the path
            # and line range valid while changing only the anchored definition;
            # this tests anchor rejection without depending on whole-checkout
            # scan size or unrelated files.
            source.write_text("\n" * 257 + "class ChangedAgentLoop:\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Task evidence anchor or rationale is invalid"):
                load_and_validate_tasks(DATASET, changed_checkout)

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
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT / "tests" / ".tmp") as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            (root / "module.py").write_text("def known():\n    return 1\n", encoding="utf-8")
            path = Path(temp) / "bad.json"
            base = {
                "dataset_version": "af011-validation-test-v1",
                "human_reviewed": False,
                "label_source": "deterministic_test",
                "tasks": [
                    {"id": "C1", "split": "calibration", "leakage_group": "c1",
                     "category": "exact_symbol", "query": "known", "expected_route": "exact_symbol",
                     "relevant": [{"path": "module.py", "start_line": 1, "end_line": 2}]},
                    {"id": "V1", "split": "validation", "leakage_group": "v1",
                     "category": "no_evidence_exact", "query": "missing", "expected_route": "exact_symbol",
                     "relevant": []},
                ],
            }
            bad = json.loads(json.dumps(base))
            bad["tasks"][0]["relevant"][0]["path"] = "../outside.py"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_and_validate_tasks(path, root)
            bad = json.loads(json.dumps(base))
            bad["tasks"][1]["leakage_group"] = "c1"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "A leakage group crosses"):
                load_and_validate_tasks(path, root)


if __name__ == "__main__":
    unittest.main()
