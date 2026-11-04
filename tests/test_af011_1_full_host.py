"""真实宿主验收工具的离线安全与证据检查。"""
from contextlib import redirect_stdout
import io
import json
import os
import hashlib
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import AF011_1_JOINT_HOST as joint
import AF011_1_FULL_HOST_ACCEPTANCE as full


class FullHostAcceptanceTests(unittest.TestCase):
    def test_summary_audit_counts_loaded_messages_without_future_lifecycle_state(self):
        message_id = "11111111-2222-4333-8444-555555555555"
        summary = {
            "content": "Synthetic summary ALPHA731.",
            "covered_message_id": message_id,
            "covered_through": 2,
            "fact_ledger": [{
                "fact_id": "a" * 24,
                "kind": "user_fact",
                "sources": [{"message_id": message_id, "sequence": 1,
                             "source_sha256": "b" * 64}],
            }],
        }
        persisted = [{"id": str(index), "role": "user"} for index in range(60)]

        audit = full._build_summary_audit(summary, persisted)

        self.assertEqual(60, audit["persisted_message_count"])
        self.assertEqual(2, audit["covered_through"])
        self.assertEqual(message_id, audit["fact_sources"][0]["sources"][0]["message_id"])
        self.assertEqual("synthetic_seeded_turns_not_real_model_turns", audit["history_origin"])

    def test_generic_host_failure_records_source_location_without_locals(self):
        def trigger():
            raise UnboundLocalError("cannot access local variable 'rows'")

        try:
            trigger()
        except UnboundLocalError as exc:
            location = joint._exception_location(exc)

        self.assertEqual("trigger", location["function"])
        self.assertEqual("test_af011_1_full_host.py", location["file"])
        self.assertIsInstance(location["line"], int)
        self.assertNotIn("locals", location)

    def test_ask_nonzero_exit_preserves_stage_call_and_redacted_error(self):
        class FailingCli:
            @staticmethod
            def main(_args):
                print("Invalid configuration: password=private-value", file=sys.stderr)
                print("Authorization: Bearer secret-token", file=sys.stderr)
                print('{"private":"stdout must not be saved"}')
                return 2

        result = {"stage": "thirty_real_chat_turns"}
        with self.assertRaisesRegex(joint.CheckFailure, "ask_exit_2"):
            full._invoke(FailingCli, ["ask", "synthetic question"], result=result,
                         operation="real_chat_turn_20")
        diagnostic = result["cli_failure_diagnostic"]
        self.assertEqual("thirty_real_chat_turns", diagnostic["stage"])
        self.assertEqual("real_chat_turn_20", diagnostic["operation"])
        self.assertEqual(2, diagnostic["exit_code"])
        self.assertEqual("cli_value_error", diagnostic["failure_kind"])
        self.assertIn("Invalid configuration", diagnostic["stderr_redacted"])
        self.assertIn("password=[REDACTED]", diagnostic["stderr_redacted"])
        self.assertIn("Authorization: Bearer [REDACTED]", diagnostic["stderr_redacted"])
        self.assertNotIn("private-value", diagnostic["stderr_redacted"])
        self.assertNotIn("secret-token", diagnostic["stderr_redacted"])
        self.assertNotIn("stdout must not be saved", json.dumps(diagnostic))
        self.assertGreater(diagnostic["stdout_chars"], 0)

    def test_cli_diagnostic_is_bounded_and_truncation_is_reported(self):
        value = "error detail " + ("x" * (full._CLI_DIAGNOSTIC_LIMIT + 50))
        sanitized, truncated = full._sanitize_cli_diagnostic(value)
        self.assertEqual(full._CLI_DIAGNOSTIC_LIMIT, len(sanitized))
        self.assertTrue(truncated)

    def test_provider_citation_failure_diagnostic_is_parsed_without_model_text(self):
        class FailingCli:
            @staticmethod
            def main(_args):
                print("AgentForge error: missing required evidence role", file=sys.stderr)
                print("AF_PROVIDER_DIAGNOSTIC=" + json.dumps({
                    "attempted": 1, "sent": 1, "succeeded": 1, "failed": 0, "retries": 0,
                    "last_application_failure_phase": "citation_validation",
                    "last_application_error_code": "required_evidence_role_not_cited",
                    "citation": {"rule_id": "required_evidence_role_not_cited",
                                 "missing_roles": ["target_definition"]},
                    "response": {"content_chars": 120},
                    "api_key": "do-not-preserve",
                    "content": "private model text",
                }), file=sys.stderr)
                return 1

        result = {"stage": "fixed_rag_cases"}
        with self.assertRaisesRegex(joint.CheckFailure, "ask_exit_1"):
            full._invoke(FailingCli, ["ask", "synthetic"], result=result, operation="rag")
        diagnostic = result["cli_failure_diagnostic"]["provider_diagnostic"]
        self.assertEqual(1, diagnostic["sent"])
        self.assertEqual("required_evidence_role_not_cited",
                         diagnostic["citation"]["rule_id"])
        self.assertNotIn("do-not-preserve", json.dumps(diagnostic))
        self.assertNotIn("private model text", json.dumps(diagnostic))

    def test_argument_parser_exit_records_failure_without_saving_query(self):
        class ExitingCli:
            @staticmethod
            def main(_args):
                print("usage: ask [options]", file=sys.stderr)
                raise SystemExit(2)

        result = {"stage": "fixed_rag_cases"}
        with self.assertRaisesRegex(joint.CheckFailure, "ask_argument_exit_2"):
            full._invoke(ExitingCli, ["ask", "private question text"], result=result,
                         operation="rag_case_parameters")
        diagnostic = result["cli_failure_diagnostic"]
        self.assertEqual("argument_parser_exit", diagnostic["failure_kind"])
        self.assertEqual("rag_case_parameters", diagnostic["operation"])
        self.assertNotIn("private question text", json.dumps(diagnostic))

    def test_successful_ask_output_is_not_copied_into_failure_diagnostics(self):
        class SuccessfulCli:
            @staticmethod
            def main(_args):
                print('{"answer":"synthetic answer"}')
                return 0

        result = {"stage": "thirty_real_chat_turns"}
        response = full._invoke(SuccessfulCli, ["ask", "synthetic question"], result=result)
        self.assertEqual("synthetic answer", response["answer"])
        self.assertNotIn("cli_failure_diagnostic", result)

    def test_fixed_rag_failure_is_not_reported_as_postgres_reconnect_failure(self):
        result = {
            "stage": "postgres_reconnect",
            "stage_results": {"postgres_reconnect": {
                "status": "completed", "restored_message_count": 60,
            }, "fixed_rag_cases": {"status": "failed", "failed_cases": ["cross_file_call"]}},
        }
        with self.assertRaisesRegex(joint.CheckFailure, "fixed_acceptance_assertions_failed"):
            full._raise_fixed_assertions_if_needed(result, True)
        self.assertEqual("fixed_rag_cases", result["stage"])
        self.assertEqual("completed", result["stage_results"]["postgres_reconnect"]["status"])
        self.assertEqual("failed", result["stage_results"]["fixed_rag_cases"]["status"])

    def test_fixed_assertion_success_does_not_raise(self):
        result = {"stage": "postgres_reconnect", "stage_results": {}}
        full._raise_fixed_assertions_if_needed(result, False)
        self.assertEqual("postgres_reconnect", result["stage"])
        self.assertEqual({}, result["stage_results"])

    def test_advancing_stage_marks_postgres_reconnect_completed(self):
        result = {"stage": "postgres_reconnect",
                  "stage_results": {"postgres_reconnect": {"status": "running"}}}
        full._start_stage(result, "completed")
        self.assertEqual("completed", result["stage_results"]["postgres_reconnect"]["status"])
        self.assertEqual("running", result["stage_results"]["completed"]["status"])

    def test_host_request_purpose_is_classified_without_saving_prompt(self):
        self.assertEqual("semantic_summary", full._request_purpose([
            {"role": "system", "content": "Summarize prior conversation messages for continuity."},
            {"role": "user", "content": "private transcript"},
        ]))
        self.assertEqual("answer_generation", full._request_purpose([
            {"role": "system", "content": "Answer only from supplied evidence."},
            {"role": "user", "content": "question"},
        ]))

    def test_prompt_audit_keeps_only_fact_and_evidence_identities(self):
        source_id = "12345678-1234-4234-8234-123456789abc"
        fact_id = "a" * 24
        audit = full._audit_chat_prompt([
            {"role": "user", "content": (
                "Untrusted source-linked conversation facts; context only, not source evidence:\n"
                f"- [fact_id={fact_id}; seq=31 message_id={source_id}] PRIVATE_SYNTHETIC_FACT\n"
                "Current question:\nWhat is it?\nUntrusted current evidence:\n[E1] code excerpt")}
        ])
        self.assertEqual([fact_id], audit["context_fact_ids_in_prompt"])
        self.assertEqual([source_id], audit["context_fact_source_message_ids_in_prompt"])
        self.assertEqual(["E1"], audit["evidence_source_ids_in_prompt"])
        self.assertNotIn("PRIVATE_SYNTHETIC_FACT", str(audit))
        self.assertNotIn("code excerpt", str(audit))

    def test_cross_file_case_requires_caller_callsite_and_target_definition_roles(self):
        case = next(item for item in full.CASES if item["id"] == "cross_file_call")
        manifest = full._verify_fixture(self.repo)
        response = {"provider": "llama-server",
            "answer": "run_queue calls start_job [E1] [E2] [E3].",
            "answer_contract": {"status": "limited", "evidence": [
                {"source_id": "E1", "relative_path": "app.py", "start_line": 2, "end_line": 3,
                 "excerpt": "def run_queue(item):\n    return launch_job(item)",
                 "evidence_roles": ["caller_definition", "definition"]},
                {"source_id": "E2", "relative_path": "app.py", "start_line": 3, "end_line": 3,
                 "excerpt": "    return launch_job(item)", "evidence_roles": ["callsite"]},
                {"source_id": "E3", "relative_path": "pkg/impl.py", "start_line": 1, "end_line": 1,
                 "excerpt": "def start_job(item):", "evidence_roles": ["target_definition", "definition"]},
            ]}}
        outcome = full._evaluate_case(case, response, self.repo, manifest)
        self.assertEqual("passed", outcome["status"])
        self.assertTrue(outcome["checks"]["required_evidence_role_coverage"])
        response["answer"] = "run_queue calls start_job [E1] [E2]."
        outcome = full._evaluate_case(case, response, self.repo, manifest)
        self.assertEqual("failed", outcome["status"])
        self.assertFalse(outcome["checks"]["required_evidence_role_coverage"])

    def setUp(self):
        root = Path(__file__).resolve().parent / ".tmp"
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="af0111-full-test-", dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        full._write_fixture(self.repo)

    def test_frozen_case_manifest_has_eleven_distinct_supported_categories(self):
        manifest = full._verify_fixture(self.repo)
        self.assertEqual(11, len(full.CASES))
        self.assertEqual(len(full.CASES), len({case["id"] for case in full.CASES}))
        self.assertEqual(2, len(manifest["cross_file_call"]))
        self.assertEqual([], manifest["missing"])
        self.assertTrue(all(item["line"] > 0 for values in manifest.values() for item in values))

    def test_negative_fixture_symbol_is_verified_before_evaluation(self):
        (self.repo / "invented.py").write_text("def nonexistent_zephyr_941(): pass\n", encoding="utf-8")
        with self.assertRaisesRegex(joint.CheckFailure, "fixture_negative_symbol_exists"):
            full._verify_fixture(self.repo)

    def test_citation_must_match_actual_source_line_and_expected_fact(self):
        manifest = full._verify_fixture(self.repo)
        case = full.CASES[0]
        response = {"provider": "llama-server", "answer": "It adds value and amount [E1].",
            "answer_contract": {"status": "limited", "evidence": [{"source_id": "E1",
                "relative_path": "shared.py", "start_line": 1, "end_line": 2,
                "excerpt": "def shared(value, amount):\n    return value + amount  # alpha"}]}}
        self.assertEqual("passed", full._evaluate_case(case, response, self.repo, manifest)["status"])
        response["answer"] = "It subtracts the amount [E1]."
        self.assertEqual("failed", full._evaluate_case(case, response, self.repo, manifest)["status"])
        response["answer"] = "It adds the amount [E1]."
        response["answer_contract"]["evidence"][0]["start_line"] = 2
        self.assertEqual("failed", full._evaluate_case(case, response, self.repo, manifest)["status"])

    def test_citation_path_outside_repository_is_rejected(self):
        manifest = full._verify_fixture(self.repo)
        outside = self.repo.parent / "outside.py"
        outside.write_text("return value + amount\n", encoding="utf-8")
        response = {"provider": "llama-server", "answer": "It adds values [E1].",
            "answer_contract": {"status": "limited", "evidence": [{"source_id": "E1",
                "relative_path": "../outside.py", "start_line": 1, "end_line": 1,
                "excerpt": "return value + amount"}]}}
        outcome = full._evaluate_case(full.CASES[0], response, self.repo, manifest)
        self.assertFalse(outcome["checks"]["source_identity"])
        self.assertEqual("failed", outcome["status"])

    def test_protected_hash_snapshot_verifies_model_and_detects_change(self):
        import AF012_MVP_HOST_ACCEPTANCE as mvp
        root = Path(self.temporary.name) / "home"
        data = root / ".agentforge" / "data"
        data.mkdir(parents=True)
        (data / "index.sqlite3").write_bytes(b"index")
        (data / "read_only_evidence.json").write_bytes(b"evidence")
        (root / "tests" / "fixtures").mkdir(parents=True)
        modelroot = Path(self.temporary.name) / "models"
        modelroot.mkdir()
        model = modelroot / "model.gguf"
        model.write_bytes(b"model")
        digest = hashlib.sha256(b"model").hexdigest()
        with patch.object(mvp, "MODEL_ROOT", modelroot), patch.object(mvp, "EXPECTED_ASSETS", {str(model): digest}):
            before = full._file_snapshot(root, deadline=time.monotonic() + 5)
            self.assertEqual(digest, before["model_assets/model.gguf"]["sha256"])
            model.write_bytes(b"changed")
            with self.assertRaisesRegex(joint.HostBlocked, "MODEL_ASSET_HASH_MISMATCH"):
                full._file_snapshot(root, deadline=time.monotonic() + 5)

    def test_no_evidence_requires_refusal_not_model_text(self):
        case = next(item for item in full.CASES if item["id"] == "missing")
        manifest = full._verify_fixture(self.repo)
        refused = {"provider": "evidence_gate_refusal", "answer": None,
                   "answer_contract": {"status": "refused", "evidence": []}}
        self.assertEqual("passed", full._evaluate_case(case, refused, self.repo, manifest)["status"])
        refused["answer"] = "An invented result."
        self.assertEqual("failed", full._evaluate_case(case, refused, self.repo, manifest)["status"])

    def test_disabled_network_writes_blocked_json_and_markdown_without_requests(self):
        with patch.object(joint, "ROOT", Path(self.temporary.name)), patch.dict(
                os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}), redirect_stdout(io.StringIO()):
            self.assertEqual(2, full.main())
        files = list(Path(self.temporary.name).glob("AF011_1_FULL_HOST_RESULT_*.json"))
        self.assertEqual(1, len(files))
        result = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual("NETWORK_RESTRICTED", result["failure_code"])
        self.assertEqual([], result["request_observations"]["chat"])
        self.assertEqual([], result["request_observations"]["embedding"])
        self.assertEqual({"attempted": 0, "sent": 0, "succeeded": 0,
                          "failed": 0, "retries": 0, "request_count": 0},
                         result["chat_requests"])
        self.assertTrue(files[0].with_suffix(".md").is_file())
