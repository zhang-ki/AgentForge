import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agentforge.evaluation import PREPRODUCT_DATASET_VERSION
from agentforge.generalization import load_generalization_dataset, validate_generalization_split
from agentforge.preproduct import (
    DEFAULT_CORPUS,
    DEFAULT_DATASET,
    DEFAULT_SPLIT_PLAN,
    evaluate_preproduct,
    validate_preproduct_fixture,
)


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest().upper()


class DeterministicMockEmbeddingProvider:
    """No-network test double; its rankings are not real-model evidence."""

    def __init__(self, base_url, *, model_id, api_model, dimension,
                 connect_timeout, read_timeout):
        self.base_url = base_url
        self.model_id = model_id
        self.api_model = api_model
        self.dimension = dimension
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.query_instruction = "mock-only"
        self._counts = {"attempts": 0, "sent": 0, "successes": 0, "failures": 0}

    @property
    def request_counts(self):
        return dict(self._counts)

    def _embed(self, text, role):
        self._counts["attempts"] += 1
        self._counts["sent"] += 1
        payload = hashlib.sha256((role + "\0" + text).encode("utf-8")).digest()
        self._counts["successes"] += 1
        return tuple((payload[index] + 1) / 256 for index in range(self.dimension))

    def embed_query(self, text):
        return self._embed(text, "query")

    def embed_document(self, text):
        return self._embed(text, "document")

    def embed(self, text):
        return self.embed_document(text)


class PreproductFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_generalization_dataset(
            DEFAULT_DATASET, expected_version=PREPRODUCT_DATASET_VERSION
        )
        cls.plan = json.loads(DEFAULT_SPLIT_PLAN.read_text(encoding="utf-8"))

    def test_fixture_has_exact_independent_80_query_contract(self):
        result = validate_preproduct_fixture()
        rows = self.dataset["queries"]
        self.assertEqual(80, len(rows))
        self.assertEqual(80, len({row["id"] for row in rows}))
        self.assertEqual(40, sum(not row["no_evidence"] for row in rows))
        self.assertEqual(40, sum(row["no_evidence"] for row in rows))
        self.assertEqual(40, result["evidence"]["verified_evidence_ranges"])
        self.assertEqual(20, result["corpus_manifest"]["file_count"])
        self.assertTrue(all(row["human_reviewed"] is False for row in rows))
        self.assertTrue(all(row["review_method"] == "ai_evidence_review" for row in rows))
        self.assertTrue(all(row["label_origin"] == "synthetic_fixture_design" for row in rows))
        corpus_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(DEFAULT_CORPUS.rglob("*")) if path.is_file()
        ).lower()
        self.assertIn("northstar cold-chain", corpus_text)
        self.assertNotIn("lumen gallery", corpus_text)

    def test_split_is_exact_balanced_complete_and_group_isolated(self):
        result = validate_generalization_split(
            self.dataset, self.plan, expected_version=PREPRODUCT_DATASET_VERSION
        )
        self.assertTrue(result["groups_are_disjoint"])
        self.assertEqual(40, result["summary"]["calibration"]["query_count"])
        self.assertEqual(20, result["summary"]["calibration"]["evidence"])
        self.assertEqual(20, result["summary"]["calibration"]["no_evidence"])
        self.assertEqual(40, result["summary"]["validation"]["query_count"])
        self.assertEqual(20, result["summary"]["validation"]["evidence"])
        self.assertEqual(20, result["summary"]["validation"]["no_evidence"])
        calibration_groups = {
            row["leakage_group"] for row in self.dataset["queries"]
            if row["split"] == "calibration"
        }
        validation_groups = {
            row["leakage_group"] for row in self.dataset["queries"]
            if row["split"] == "validation"
        }
        self.assertFalse(calibration_groups & validation_groups)

    def test_invalid_provenance_and_group_leakage_are_rejected(self):
        altered_dataset = copy.deepcopy(self.dataset)
        altered_dataset["queries"][0]["label_origin"] = "retriever_output"
        with patch("agentforge.preproduct.load_generalization_dataset", return_value=altered_dataset):
            with self.assertRaisesRegex(ValueError, "label origin"):
                validate_preproduct_fixture()
        altered_plan = copy.deepcopy(self.plan)
        moved = altered_plan["splits"]["calibration"].pop()
        altered_plan["splits"]["validation"].append(moved)
        with self.assertRaises(ValueError):
            validate_generalization_split(
                self.dataset, altered_plan, expected_version=PREPRODUCT_DATASET_VERSION
            )

    def test_offline_run_keeps_vectors_unmeasured_and_defaults_unchanged(self):
        report = evaluate_preproduct(vector_unavailable_reason="mock preflight: no service")
        self.assertEqual(40, report["calibration"]["query_count"])
        self.assertEqual(40, report["validation"]["query_count"])
        self.assertEqual("not_measured", report["validation"]["retrievers"]["vector"]["status"])
        self.assertFalse(report["production_threshold_enabled"])
        self.assertFalse(report["product_entry_decision"]["default_retriever_modified"])
        self.assertEqual("not_decidable_without_real_vectors", report["product_entry_decision"]["decision"])

    def test_mock_vector_flow_freezes_calibration_before_validation(self):
        with patch(
            "agentforge.embeddings.LlamaServerEmbeddingProvider",
            DeterministicMockEmbeddingProvider,
        ):
            report = evaluate_preproduct(
                base_url="http://127.0.0.1:45678",
                model_id="mock-preproduct",
                api_model="mock-preproduct",
                dimension=8,
            )
        fixed = report["fixed_experiment_configuration"]
        self.assertFalse(fixed["validation_consulted_for_selection"])
        self.assertEqual(1, fixed["validation_evaluations"])
        self.assertIsNotNone(report["calibration_selected_abstention"])
        self.assertEqual(
            fixed["calibration_selected_rule"],
            report["frozen_abstention_validation"]["rule"],
        )
        self.assertEqual(40, report["validation"]["retrievers"]["vector"]["timing"]["measured_call_count"])
        self.assertIn("calibration", report["retrieval_complementarity"])
        self.assertIn("validation", report["retrieval_complementarity"])
        self.assertFalse(report["production_threshold_enabled"])
        self.assertFalse(report["product_entry_decision"]["production_threshold_enabled"])
        self.assertFalse(report["product_entry_decision"]["default_retriever_modified"])

    def test_frozen_historical_and_formal_files_keep_known_hashes(self):
        expected = {
            "tests/fixtures/retrieval_eval.json": "B3276E4309C08FAA8B8961DB8DDD457A6ABA7491C134E606FC037B0B1342AF87",
            "tests/fixtures/retrieval_eval_generalization_v1.json": "607C0BE6DB9CAB0003E6D57A14E028758B27C248F79F125F6E818861F4D8CBF1",
            "tests/fixtures/retrieval_eval_generalization_split_v1.json": "78E367FCEC916C0BF062C8433367ADF5FC41E3BD4713CB4541AC9C8B69D12FDD",
            "AF009GR5_RESULTS.json": "DABAA96701259C6AA21FE95BE3F113906CDED0A3F671AED33F419067B6F1E3A2",
            ".agentforge/data/index.sqlite3": "AF2E8DDB18E78E8AC1B2B9BB48E32173E22B5933456B7AAF15C453D3175D4296",
            ".agentforge/data/read_only_evidence.json": "CAAD5839D41198D7DB0F84813E204E427781723016F4F268868CF2F7F5268733",
        }
        self.assertEqual(expected, {path: _sha256(ROOT / path) for path in expected})


if __name__ == "__main__":
    unittest.main()
