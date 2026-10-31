import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
    def test_limited_answer_displays_cited_path_and_uncertainty(self):
        text = render_ask_result(payload())
        self.assertIn("实际回答 [E1]", text)
        self.assertIn("src/cache.py:1-2（回答已引用）", text)
        self.assertIn("引用有效不代表每句话都已证明", text)
        self.assertIn("静态关系存在不确定性", text)

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


class CitationPromptTests(unittest.TestCase):
    def test_prompt_requires_supplied_evidence_per_claim_without_changing_citations(self):
        client = Mock()
        client.chat.return_value = "Synthetic answer [E1]"
        context = build_context("what is this?", [result()], budget=6000)
        answer = LlamaServerProvider(client, "model").answer(context)
        messages = client.chat.call_args.args[0]
        self.assertIn("For every factual claim", messages[0]["content"])
        self.assertIn("at least one applicable supplied evidence ID", messages[0]["content"])
        self.assertIn("[E1]", messages[1]["content"])
        self.assertEqual(answer.citations, (context.evidence[0].citation,))
        client.chat.assert_called_once()

    def test_forged_marker_still_rejected_without_retry(self):
        client = Mock()
        client.chat.return_value = "Invented [E99]"
        context = build_context("what is this?", [result()], budget=6000)
        with self.assertRaisesRegex(CitationValidationError, "source ID"):
            LlamaServerProvider(client, "model").answer(context)
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

    def test_no_evidence_never_calls_model(self):
        client = Mock()
        context = build_context("question", [], budget=6000)
        answer = LlamaServerProvider(client, "model").answer(context)
        self.assertTrue(answer.offline)
        client.chat.assert_not_called()
