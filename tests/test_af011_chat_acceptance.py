import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout
import io

import AF011_CHAT_HOST_ACCEPTANCE as runner
from tests.support import LocalTestCase


def _stats(attempted, sent, succeeded, failed):
    return {
        "attempted": attempted, "sent": sent, "succeeded": succeeded,
        "failed": failed, "retries": 0,
    }


class AF011ChatAcceptanceTests(LocalTestCase):
    def test_worker_exercises_real_cli_path_with_memory_index_and_one_mocked_chat(self):
        shutil.copytree(runner.SOURCE_FIXTURE, self.root, dirs_exist_ok=True)
        real_client = Mock()
        real_client.chat.return_value = (
            "It returns `offline-{session_id}` [E1]."
        )
        real_client.request_stats.return_value = _stats(1, 1, 1, 0)
        gated_client = Mock()
        gated_client.chat.side_effect = AssertionError("no-evidence case must not call chat")
        gated_client.request_stats.return_value = _stats(0, 0, 0, 0)
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"AGENTFORGE_LLAMA_API_KEY": "synthetic-test-key"}),
            patch("agentforge.cli.LlamaServerClient", side_effect=(real_client, gated_client)) as factory,
            redirect_stdout(output),
        ):
            code = runner._ask_worker(
                self.root, "http://127.0.0.1:32123/v1/chat/completions", "synthetic-model"
            )
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("completed", payload["worker_status"])
        cases = payload["cases"]
        self.assertEqual("llama-server", cases["evidence_question"]["payload"]["provider"])
        self.assertEqual(
            "evidence_gate_refusal",
            cases["no_evidence_question"]["payload"]["provider"],
        )
        real_client.chat.assert_called_once()
        gated_client.chat.assert_not_called()
        self.assertEqual(512, factory.call_args_list[0].kwargs["max_output_tokens"])

    def test_network_disabled_gate_writes_blocked_result_without_model_actions(self):
        result = self.sandbox / "result.json"
        report = self.sandbox / "report.md"
        with (
            patch.object(runner, "RESULT_PATH", result),
            patch.object(runner, "REPORT_PATH", report),
            patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}),
            patch.object(runner, "_host_attempt", side_effect=AssertionError("host path forbidden")),
        ):
            with redirect_stdout(io.StringIO()):
                code = runner.main([])
        self.assertEqual(3, code)
        payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual("BLOCKED", payload["latest_status"])
        latest = payload["attempts"][-1]
        self.assertFalse(latest["service"]["started"])
        self.assertEqual(0, latest["request_counts"]["sent"])
        self.assertIn("Windows PowerShell", report.read_text(encoding="utf-8"))

    def test_case_evaluation_requires_real_answer_exact_evidence_and_no_evidence_gate(self):
        evidence_payload = {
            "provider": "llama-server",
            "answer": (
                "It returns the f-string `offline-{session_id}` "
                "[E1]."
            ),
            "answer_contract": {
                "status": "limited",
                "claims": [{
                    "text": "answer", "kind": "inference",
                    "citations": [runner.EXPECTED_CITATION],
                }],
                "evidence": [{
                    "relative_path": "src/cache.py", "start_line": 4,
                    "end_line": 6, "excerpt": runner.EXPECTED_EXCERPT,
                }],
            },
            "provider_request_stats": _stats(1, 1, 1, 0),
            "retrieval": {"route": "exact_symbol"},
        }
        absent_payload = {
            "provider": "evidence_gate_refusal", "answer": None,
            "refused_output": "evidence only",
            "answer_contract": {"status": "refused"},
            "provider_request_stats": _stats(0, 0, 0, 0),
            "retrieval": {"confidence": "low"},
        }
        worker = {
            "exit_code": 0, "timed_out": False,
            "payload": {"cases": {
                "evidence_question": {"payload": evidence_payload},
                "no_evidence_question": {"payload": absent_payload},
            }},
        }
        result = runner._evaluate_cases(worker)
        self.assertTrue(result["passed"])
        self.assertEqual("PASS", result["evidence_question"]["semantic_status"])
        self.assertEqual("PASS", result["no_evidence_question"]["semantic_status"])
        self.assertEqual(_stats(1, 1, 1, 0), runner._sum_request_counts(worker))

        worker["payload"]["cases"]["evidence_question"]["payload"]["answer"] = (
            "It returns a network token [E1]."
        )
        failed = runner._evaluate_cases(worker)
        self.assertFalse(failed["passed"])
        self.assertEqual("FAIL", failed["evidence_question"]["semantic_status"])

    def test_history_is_append_only_and_a_pass_prevents_another_real_run(self):
        result = self.sandbox / "result.json"
        report = self.sandbox / "report.md"
        first = {
            "attempt_id": "one", "status": "BLOCKED", "summary": "blocked",
            "execution_environment": {}, "service": {}, "request_counts": {}, "cases": {},
        }
        second = {
            "attempt_id": "two", "status": "PASS", "summary": "passed",
            "execution_environment": {}, "service": {}, "request_counts": {}, "cases": {},
        }
        with patch.object(runner, "RESULT_PATH", result), patch.object(runner, "REPORT_PATH", report):
            runner._append_attempt(first)
            runner._append_attempt(second)
            history = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(["one", "two"], [row["attempt_id"] for row in history["attempts"]])
            with patch.dict(os.environ, {}, clear=True), patch.object(
                runner, "_host_attempt", side_effect=AssertionError("must not run twice")
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(2, runner.main([]))

    def test_request_counter_never_treats_missing_stats_as_success(self):
        worker = {"payload": {"cases": {
            "evidence_question": {"payload": {}},
            "no_evidence_question": {"payload": {"provider_request_stats": _stats(0, 0, 0, 0)}},
        }}}
        self.assertEqual(_stats(0, 0, 0, 0), runner._sum_request_counts(worker))

    def test_failed_cli_response_retains_real_request_counts_and_failure_stage(self):
        shutil.copytree(runner.SOURCE_FIXTURE, self.root, dirs_exist_ok=True)
        failed_client = Mock()
        # 使用项目真实的错误类型；本测试不会发起网络请求。
        from agentforge.errors import ProviderError
        failed_client.chat.side_effect = ProviderError(
            "Local server returned an invalid chat response (empty_final_content_with_reasoning)."
        )
        failed_client.request_stats.return_value = {
            **_stats(1, 1, 0, 1),
            "last_error_code": "empty_final_content_with_reasoning",
            "last_failure_phase": "response validation",
        }
        gated_client = Mock()
        gated_client.request_stats.return_value = _stats(0, 0, 0, 0)
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"AGENTFORGE_LLAMA_API_KEY": "synthetic-test-key"}),
            patch("agentforge.cli.LlamaServerClient", side_effect=(failed_client, gated_client)),
            redirect_stdout(output),
        ):
            code = runner._ask_worker(
                self.root, "http://127.0.0.1:32123/v1/chat/completions", "synthetic-model"
            )
        self.assertEqual(1, code)
        payload = json.loads(output.getvalue())
        evidence = payload["cases"]["evidence_question"]
        self.assertEqual("PROVIDER_RESPONSE", evidence["failure"]["stage"])
        self.assertEqual("empty_final_content_with_reasoning", evidence["failure"]["code"])
        self.assertEqual((1, 1, 0, 1), tuple(
            evidence["provider_request_stats"][key]
            for key in ("attempted", "sent", "succeeded", "failed")
        ))
        worker = {"payload": payload}
        self.assertEqual(_stats(1, 1, 0, 1), runner._sum_request_counts(worker))
        evaluated = runner._evaluate_cases({**worker, "exit_code": 1, "timed_out": False})
        self.assertFalse(evaluated["passed"])
        self.assertEqual("FAIL", evaluated["evidence_question"]["status"])
        self.assertEqual("PASS", evaluated["no_evidence_question"]["status"])

    def test_citation_failure_is_classified_after_successful_http_response(self):
        shutil.copytree(runner.SOURCE_FIXTURE, self.root, dirs_exist_ok=True)
        from agentforge import cli
        from agentforge.indexer import index_repository
        from agentforge.providers.llama_server import LlamaServerClient
        from agentforge.store import IndexStore

        client = LlamaServerClient(
            "http://127.0.0.1:32123/v1/chat/completions",
            api_key="synthetic-test-key",
        )
        client.chat = Mock(return_value="It returns a value from invented.py:90-99.")
        client._request_stats.update({
            "attempted": 1, "sent": 1, "succeeded": 1, "failed": 0,
            "last_http_status": 200,
        })
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            with (
                patch.dict(os.environ, {"AGENTFORGE_LLAMA_API_KEY": "synthetic-test-key"}),
                patch("agentforge.cli.LlamaServerClient", return_value=client),
            ):
                result = runner._invoke_cli(cli, store, [
                    "ask", "What does function refresh_access_token return for session_id?",
                    "--provider", "llama-server",
                    "--base-url", "http://127.0.0.1:32123/v1/chat/completions",
                    "--model", "synthetic-model",
                    "--mode", "repository",
                ])
        self.assertEqual(1, result["exit_code"])
        self.assertEqual("ANSWER_CONTRACT", result["failure"]["stage"])
        self.assertEqual(
            "model_supplied_path_location_forbidden", result["failure"]["code"]
        )
        stats = result["provider_request_stats"]
        self.assertEqual((1, 1, 1, 0), tuple(
            stats[key] for key in ("attempted", "sent", "succeeded", "failed")
        ))
        self.assertEqual("rejected", stats["last_citation_diagnostic"]["validation_result"])
        self.assertEqual("answer_contract", stats["last_application_failure_phase"])

    def test_answer_contract_failure_does_not_reclassify_provider_success(self):
        shutil.copytree(runner.SOURCE_FIXTURE, self.root, dirs_exist_ok=True)
        from agentforge import cli
        from agentforge.errors import ProviderError
        from agentforge.indexer import index_repository
        from agentforge.providers.llama_server import LlamaServerClient
        from agentforge.store import IndexStore

        client = LlamaServerClient(
            "http://127.0.0.1:32123/v1/chat/completions",
            api_key="synthetic-test-key",
        )
        client.chat = Mock(return_value="It returns `offline-{session_id}` [E1].")
        client._request_stats.update({
            "attempted": 1, "sent": 1, "succeeded": 1, "failed": 0,
            "last_http_status": 200,
        })
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            with (
                patch.dict(os.environ, {"AGENTFORGE_LLAMA_API_KEY": "synthetic-test-key"}),
                patch("agentforge.cli.LlamaServerClient", return_value=client),
                patch("agentforge.conversation_orchestrator.verify_generated_answer",
                      side_effect=ProviderError("synthetic contract failure")),
            ):
                result = runner._invoke_cli(cli, store, [
                    "ask", "What does function refresh_access_token return for session_id?",
                    "--provider", "llama-server",
                    "--base-url", "http://127.0.0.1:32123/v1/chat/completions",
                    "--model", "synthetic-model",
                    "--mode", "repository",
                ])
        self.assertEqual("ANSWER_CONTRACT", result["failure"]["stage"])
        stats = result["provider_request_stats"]
        self.assertEqual((1, 1, 1, 0), tuple(
            stats[key] for key in ("attempted", "sent", "succeeded", "failed")
        ))
        self.assertEqual("answer_contract_failed", stats["last_application_error_code"])

    def test_failed_evidence_case_preserves_completed_no_evidence_result(self):
        absent_payload = {
            "provider": "evidence_gate_refusal", "answer": None,
            "refused_output": "evidence only",
            "answer_contract": {"status": "refused"},
            "provider_request_stats": _stats(0, 0, 0, 0),
            "retrieval": {"confidence": "low"},
        }
        worker = {
            "exit_code": 1, "timed_out": False,
            "payload": {"cases": {
                "evidence_question": {
                    "exit_code": 1, "payload": None,
                    "provider_request_stats": {
                        **_stats(1, 1, 1, 0),
                        "last_application_failure_phase": "citation_validation",
                        "last_application_error_code": "unknown_source_id",
                    },
                    "failure": {"stage": "CITATION_VALIDATION", "code": "unknown_source_id"},
                },
                "no_evidence_question": {"exit_code": 0, "payload": absent_payload},
            }},
        }
        result = runner._evaluate_cases(worker)
        self.assertFalse(result["passed"])
        self.assertEqual("FAIL", result["evidence_question"]["status"])
        self.assertEqual("PASS", result["no_evidence_question"]["status"])
        self.assertEqual("refused", result["no_evidence_question"]["contract_status"])

    def test_provider_configuration_failure_is_distinct_and_never_opens_store(self):
        from agentforge import cli
        store = Mock()
        result = runner._invoke_cli(cli, store, [
            "ask", "example", "--provider", "llama-server", "--model", "synthetic-model",
        ])
        self.assertEqual(2, result["exit_code"])
        self.assertIsNone(result["provider_request_stats"])
        self.assertEqual(
            "PROVIDER_CONFIGURATION_OR_CLI_PREFLIGHT", result["failure"]["stage"]
        )
        store.metadata.assert_not_called()

    def test_evidence_gate_keeps_request_count_zero_with_explicit_audit(self):
        shutil.copytree(runner.SOURCE_FIXTURE, self.root, dirs_exist_ok=True)
        from agentforge import cli
        from agentforge.indexer import index_repository
        from agentforge.store import IndexStore
        client = Mock()
        client.request_stats.return_value = _stats(0, 0, 0, 0)
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            with (
                patch.dict(os.environ, {"AGENTFORGE_LLAMA_API_KEY": "synthetic-test-key"}),
                patch("agentforge.cli.LlamaServerClient", return_value=client),
            ):
                result = runner._invoke_cli(cli, store, [
                    "ask", "What does function quantum_flux_override_zz9 return?",
                    "--provider", "llama-server",
                    "--base-url", "http://127.0.0.1:32123/v1/chat/completions",
                    "--model", "synthetic-model",
                    "--mode", "repository",
                ])
        self.assertEqual(0, result["exit_code"])
        self.assertEqual(_stats(0, 0, 0, 0), result["provider_request_stats"])
        self.assertEqual("evidence_gate_refusal", result["payload"]["provider"])
        client.chat.assert_not_called()

    def test_host_failure_after_model_ready_still_stops_owned_process_and_removes_temp(self):
        runtime = self.sandbox / "runtime-source"
        runtime.mkdir()
        (runtime / "llama-server.exe").write_bytes(b"synthetic")
        fake_process = Mock()
        fake_process.pid = 12345
        fake_process.poll.return_value = None
        fake_process.stdout = io.BytesIO()
        fake_process.stderr = io.BytesIO()
        fake_thread = Mock()
        manifest = [
            {"path": f"file-{index}.dll", "bytes": index, "sha256": str(index)}
            for index in range(51)
        ]
        failed_worker = {
            "exit_code": 1, "timed_out": False, "payload": {
                "cases": {"evidence_question": {
                    "exit_code": 1,
                    "provider_request_stats": {
                        **_stats(1, 1, 0, 1),
                        "last_error_code": "empty_final_content",
                    },
                    "failure": {
                        "stage": "PROVIDER_RESPONSE", "code": "empty_final_content",
                    },
                }}
            },
        }
        expected = {"placeholder": {"bytes": 1, "sha256": "A"}}
        verified = {"count": 1, "missing": [], "changed": [], "passed": True, "current": {}}
        with (
            patch.object(runner, "RUNTIME_PATH", runtime),
            patch.object(runner, "TEMP_ROOT", self.sandbox / "tmp"),
            patch.object(runner, "EXPECTED_HASHES", {}),
            patch.object(runner, "_historical_expected", return_value=expected),
            patch.object(runner, "_verify_expected", return_value=verified),
            patch.object(runner, "_existing_llama_servers", return_value=[]),
            patch.object(runner, "_owned_listeners", return_value=[{
                "LocalAddress": "127.0.0.1", "LocalPort": 32123, "OwningProcess": 12345,
            }]),
            patch.object(runner.host, "_runtime_manifest", return_value=manifest),
            patch.object(runner.host, "_loopback_preflight", return_value={"status": "passed"}),
            patch.object(runner.host, "_reserve_ephemeral_port", return_value=32123),
            patch.object(runner.subprocess, "Popen", return_value=fake_process),
            patch.object(runner.threading, "Thread", return_value=fake_thread),
            patch.object(runner.host, "_wait_for_tcp", return_value={"status": "passed"}),
            patch.object(runner.host, "_wait_for_model_ready", return_value={"status": "passed"}),
            patch.object(runner, "_run_worker", return_value=failed_worker),
            patch.object(runner.host, "_process_memory_record", return_value={}),
            patch.object(runner.host, "_stop_owned_process", return_value={
                "termination": "terminated", "exit_code": 1, "owned_process_stopped": True,
            }) as stop,
            patch.object(runner.host, "_port_released", return_value=True),
        ):
            attempt, code = runner._host_attempt()
        self.assertEqual(("FAIL", "FAILED", 1), (attempt["status"], attempt["state"], code))
        self.assertEqual(_stats(1, 1, 0, 1), attempt["request_counts"])
        self.assertEqual("runner_initiated_termination", attempt["service"]["exit_semantics"])
        self.assertTrue(attempt["runtime_copy"]["removed"])
        stop.assert_called_once_with(fake_process)


if __name__ == "__main__":
    unittest.main()
