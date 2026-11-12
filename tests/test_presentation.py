import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from agentforge.context import build_context
from agentforge.errors import CitationValidationError
from agentforge.presentation import main, render_ask_result
from agentforge.providers.llama_server import LlamaServerProvider
from tests.test_context import result


def payload(status="limited", provider="llama-server", cited=True):
    return {
        "provider": provider,
        "answer": "实际回答 [E1]" if status != "refused" else None,
        "refused_output": "模型原文，没有引用" if status == "refused" else None,
        "answer_contract": {
            "status": status,
            "claims": [{"citations": ["src/cache.py:1-2"] if cited else []}],
            "evidence": [{"source_id": "E1", "relative_path": "src/cache.py",
                          "start_line": 1, "end_line": 2,
                          "resolution_status": "ambiguous"}],
        },
    }


class FriendlyPresentationTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / ".agentforge" / "test-observability"
        scratch.mkdir(parents=True, exist_ok=True)
        self._log_temp = tempfile.TemporaryDirectory(dir=str(scratch))
        self.addCleanup(self._log_temp.cleanup)
        log_path = Path(self._log_temp.name) / "request-lifecycle.jsonl"
        patcher = patch("agentforge.presentation.default_log_path", return_value=log_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_limited_answer_displays_cited_path_and_uncertainty(self):
        text = render_ask_result(payload())
        self.assertIn("实际回答 [E1]", text)
        self.assertIn("src/cache.py:1-2（回答已引用）", text)
        self.assertIn("引用有效不代表每句话都已证明", text)
        self.assertIn("静态关系存在不确定性", text)

    def test_unconfirmed_native_streaming_is_reported_as_buffered_answer(self):
        value = payload(provider="deepseek")
        value["retrieval"] = {"streaming_status": "not_measured"}
        text = render_ask_result(value)
        self.assertIn("模型原生流式能力未确认", text)
        self.assertIn("完整非流式回答", text)

    def test_refused_model_output_never_becomes_verified_answer(self):
        text = render_ask_result(payload("refused", cited=False))
        self.assertIn("模型原文（未经验证", text)
        self.assertIn("模型原文，没有引用", text)
        self.assertIn("找到的证据，回答未引用", text)
        self.assertNotIn("（回答已引用）", text)

    def test_gate_refusal_without_evidence_does_not_claim_model_ran(self):
        value = payload("refused", provider="evidence_gate_refusal", cited=False)
        value["answer_contract"]["evidence"] = []
        text = render_ask_result(value)
        self.assertIn("本次没有使用模型", text)
        self.assertIn("未找到可用", text)
        self.assertNotIn("模型原文", text)

    def test_degraded_vector_status_is_visible_without_changing_answer_contract(self):
        value = payload()
        value["retrieval"] = {"index_lifecycle": {
            "state": "DEGRADED_LEXICAL_ONLY",
            "reason_code": "embedding_service_unavailable",
        }}

        text = render_ask_result(value)

        self.assertIn("当前使用 SQLite 词法检索", text)
        self.assertIn("embedding_service_unavailable", text)
        self.assertEqual("limited", value["answer_contract"]["status"])

    def test_main_calls_cli_once_and_preserves_nonzero_errors(self):
        def fake_cli(arguments):
            self.assertEqual(arguments, ["ask", "question"])
            print("AgentForge error: connection failed", file=__import__("sys").stderr)
            return 1
        out, err = io.StringIO(), io.StringIO()
        with patch("agentforge.presentation.cli.main", side_effect=fake_cli) as call:
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main(["ask", "question"]), 1)
        call.assert_called_once()
        self.assertIn("connection failed", err.getvalue())
        self.assertNotIn("回答：", out.getvalue())

    def test_main_reports_safe_execution_failure_when_cli_returns_json_and_nonzero(self):
        value = {
            "execution_status": "failed",
            "agent_execution": {
                "status": "failed", "state": "FAILED", "error_code": "planner_error",
                "results": [{"tool_id": "repo.search", "status": "failed",
                             "error_code": "provider_error", "output": "PRIVATE TOOL OUTPUT"}],
            },
        }

        def fake_cli(_arguments):
            print(json.dumps(value))
            return 1

        out, err = io.StringIO(), io.StringIO()
        with patch("agentforge.presentation.cli.main", side_effect=fake_cli):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main(["ask", "question"]), 1)
        self.assertIn("执行未完成（状态：failed）", err.getvalue())
        self.assertIn("planner_error", err.getvalue())
        self.assertIn("repo.search / failed / provider_error", err.getvalue())
        self.assertNotIn("PRIVATE TOOL OUTPUT", err.getvalue())
        self.assertNotIn("AgentForge 请求失败", err.getvalue())
        self.assertEqual("处理中…\n", out.getvalue())

    def test_waiting_approval_is_reported_as_pause_not_failure_detail(self):
        value = {"execution_status": "waiting_approval",
                 "agent_execution": {"status": "waiting_approval", "state": "WAITING_APPROVAL",
                                     "error_code": "approval_required", "results": []}}
        from agentforge.presentation import render_execution_failure
        text = render_execution_failure(value)
        self.assertIn("执行已暂停，等待审批", text)
        self.assertIn("approval_required", text)

    def test_plan_validation_rejection_is_not_mislabeled_as_tool_execution_failure(self):
        value = {"execution_status": "failed", "agent_execution": {
            "status": "failed", "state": "FAILED", "failure_phase": "plan_validation",
            "error_code": "DUPLICATE_CALL",
            "results": [{"tool_id": "read", "status": "failed",
                         "error_code": "DUPLICATE_CALL", "failure_phase": "plan_validation"}],
        }}
        from agentforge.presentation import render_execution_failure
        text = render_execution_failure(value)
        self.assertIn("失败阶段：plan_validation", text)
        self.assertIn("read / 计划校验拒绝 / DUPLICATE_CALL", text)
        self.assertNotIn("read / failed", text)

    def test_failure_diagnostic_omits_untrusted_metadata(self):
        value = {"execution_status": "failed", "agent_execution": {
            "status": "failed", "state": "FAILED", "error_code": "bad code with secret",
            "results": [{"tool_id": "bad tool id", "status": "failed",
                         "error_code": "token=secret", "output": "do not show"}],
        }}
        from agentforge.presentation import render_execution_failure
        text = render_execution_failure(value)
        self.assertNotIn("secret", text)
        self.assertNotIn("do not show", text)
        self.assertNotIn("bad tool id", text)
        self.assertIn("unknown", text)

    def test_main_displays_real_cli_json_and_rejects_malformed_json(self):
        for raw, expected in ((json.dumps(payload(), ensure_ascii=False), 0), ("not json", 1)):
            def fake_cli(_arguments):
                print(raw)
                return 0
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(raw=raw), patch("agentforge.presentation.cli.main", side_effect=fake_cli):
                with redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(main(["ask", "q"]), expected)
            self.assertIn("回答：" if expected == 0 else "未显示为成功",
                          out.getvalue() if expected == 0 else err.getvalue())

    def test_streaming_console_displays_deltas_before_completion_and_does_not_duplicate_answer(self):
        answer = "Hello there!"
        value = {
            "provider": "deepseek", "execution_status": "completed", "answer": answer,
            "answer_contract": {"status": "limited", "claims": [], "evidence": []},
            "retrieval": {"retrieval_status": "no_evidence"},
            "provider_request_stats": {"last_usage": {"prompt_tokens": 4, "completion_tokens": 3}},
        }
        out, err = io.StringIO(), io.StringIO()

        def fake_cli(_arguments, *, stream_callback=None):
            self.assertIsNotNone(stream_callback)
            stream_callback("Hello")
            self.assertIn("Hello", out.getvalue(), "first delta must be visible before the next arrives")
            stream_callback(" there!")
            print(json.dumps(value))
            return 0

        with patch("agentforge.presentation.cli.main", side_effect=fake_cli):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(0, main(["ask", "question", "--stream"]))

        visible = out.getvalue()
        self.assertIn("草稿（尚未通过引用验证）", visible)
        self.assertIn("回答已通过合同校验并保存", visible)
        self.assertIn("回答内容已在上方实时显示", visible)
        self.assertEqual(1, visible.count(answer))
        self.assertEqual("", err.getvalue())

    def test_streaming_console_marks_partial_output_when_cli_fails(self):
        out, err = io.StringIO(), io.StringIO()

        def fake_cli(_arguments, *, stream_callback=None):
            stream_callback("incomplete draft")
            print("provider disconnected", file=__import__("sys").stderr)
            return 1

        with patch("agentforge.presentation.cli.main", side_effect=fake_cli):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(1, main(["ask", "question", "--stream"]))

        self.assertIn("incomplete draft", out.getvalue())
        self.assertIn("草稿未完成", out.getvalue())
        self.assertIn("provider disconnected", err.getvalue())
        self.assertNotIn("回答已通过合同校验", out.getvalue())

    def test_buffered_recovery_displays_complete_answer_after_invalidating_partial_draft(self):
        value = {
            "provider": "deepseek", "execution_status": "completed",
            "answer": "Complete buffered answer.",
            "answer_contract": {"status": "limited", "claims": [], "evidence": []},
            "retrieval": {"retrieval_status": "no_evidence",
                          "streaming_status": "degraded_to_buffered"},
        }
        out, err = io.StringIO(), io.StringIO()

        def fake_cli(_arguments, *, stream_callback=None):
            stream_callback("partial draft")
            print(json.dumps(value))
            return 0

        with patch("agentforge.presentation.cli.main", side_effect=fake_cli):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(0, main(["ask", "question", "--stream"]))

        visible = out.getvalue()
        self.assertIn("partial draft", visible)
        self.assertIn("上方未验证草稿已作废", visible)
        self.assertIn("回答：\nComplete buffered answer.", visible)
        self.assertEqual(1, visible.count("Complete buffered answer."))
        self.assertNotIn("回答内容已在上方实时显示", visible)
        self.assertEqual("", err.getvalue())

    def test_buffered_for_contract_displays_only_the_complete_sanitized_answer(self):
        answer = "A complete general answer."
        value = {
            "provider": "deepseek", "execution_status": "completed", "answer": answer,
            "answer_contract": {"status": "limited", "claims": [], "evidence": []},
            "retrieval": {"retrieval_status": "no_evidence",
                          "streaming_status": "buffered_for_contract",
                          "invalid_source_markers_removed": 1,
                          "invalid_source_marker_ids_removed": ["E1"]},
        }
        out, err = io.StringIO(), io.StringIO()

        def fake_cli(_arguments, *, stream_callback=None):
            self.assertIsNotNone(stream_callback)
            print(json.dumps(value))
            return 0

        with patch("agentforge.presentation.cli.main", side_effect=fake_cli):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(0, main(["ask", "question", "--stream"]))

        visible = out.getvalue()
        self.assertEqual(1, visible.count(answer))
        self.assertNotIn("[E1]", visible)
        self.assertIn("回答已缓冲至合同校验完成后完整展示", visible)
        self.assertNotIn("草稿（尚未通过引用验证）", visible)
        self.assertEqual("", err.getvalue())

    def test_streaming_console_warns_if_result_is_not_structured(self):
        out, err = io.StringIO(), io.StringIO()

        def fake_cli(_arguments, *, stream_callback=None):
            stream_callback("unverified draft")
            print("not-json")
            return 0

        with patch("agentforge.presentation.cli.main", side_effect=fake_cli):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(1, main(["ask", "question", "--stream"]))

        self.assertIn("unverified draft", out.getvalue())
        self.assertIn("草稿未完成或未通过回答校验", out.getvalue())
        self.assertIn("未显示为成功", err.getvalue())

    def test_streaming_off_keeps_one_shot_answer_render(self):
        value = {
            "provider": "deepseek", "execution_status": "completed", "answer": "Buffered answer.",
            "answer_contract": {"status": "limited", "claims": [], "evidence": []},
        }
        out, err = io.StringIO(), io.StringIO()

        def fake_cli(arguments):
            self.assertNotIn("--stream", arguments)
            print(json.dumps(value))
            return 0

        with patch("agentforge.presentation.cli.main", side_effect=fake_cli):
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(0, main(["ask", "question"]))

        self.assertEqual(1, out.getvalue().count("Buffered answer."))
        self.assertIn("回答：", out.getvalue())
        self.assertEqual("", err.getvalue())


class CitationPromptTests(unittest.TestCase):
    def test_prompt_requires_supplied_evidence_per_claim_without_changing_citations(self):
        from agentforge.answer_contract import verify_generated_answer
        client = Mock()
        client.chat.return_value = "Synthetic answer [E1]"
        context = build_context("what is this?", [result()], budget=6000)
        answer = LlamaServerProvider(client, "model").answer(context)
        messages = client.chat.call_args.args[0]
        self.assertIn("Cite every repository fact immediately", messages[0]["content"])
        self.assertIn("applicable current evidence ID", messages[0]["content"])
        self.assertIn("[E1]", messages[1]["content"])
        self.assertEqual(answer.citations, ())
        contract = verify_generated_answer(answer, context, route="repository",
                                           route_confidence="high")
        self.assertEqual(contract.claims[0].citations, (context.evidence[0].citation,))
        client.chat.assert_called_once()

    def test_forged_marker_still_rejected_without_retry(self):
        from agentforge.answer_contract import verify_generated_answer
        client = Mock()
        client.chat.return_value = "Invented [E99]"
        context = build_context("what is this?", [result()], budget=6000)
        with self.assertRaisesRegex(CitationValidationError, "source ID"):
            answer = LlamaServerProvider(client, "model").answer(context)
            verify_generated_answer(answer, context, route="repository",
                                    route_confidence="high")
        client.chat.assert_called_once()

    def test_reminder_lists_only_current_request_ids_after_evidence(self):
        for sources, allowed in (([result()], "[E1]"),
                                 ([result(), result("other.py", "return 2", 1, 1)], "[E1], [E2]")):
            with self.subTest(allowed=allowed):
                client = Mock()
                client.chat.return_value = "Supported [E1]"
                context = build_context("question", sources, budget=6000)
                LlamaServerProvider(client, "model").answer(context)
                user_message = client.chat.call_args.args[0][1]["content"]
                reminder = user_message.split("\n\nCitation reminder: ")[-1]
                self.assertIn("Use only these evidence IDs: " + allowed + ".", reminder)
                self.assertIn("Cite the applicable IDs", reminder)
                self.assertNotIn("[E99]", reminder)
                client.chat.assert_called_once()

    def test_repository_provider_requires_evidence_and_never_calls_model(self):
        from agentforge.errors import ProviderError
        client = Mock()
        context = build_context("question", [], budget=6000)
        with self.assertRaisesRegex(ProviderError, "requires request-local evidence"):
            LlamaServerProvider(client, "model").answer(context)
        client.chat.assert_not_called()
