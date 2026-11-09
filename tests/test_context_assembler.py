"""Model-aware request assembly never edits the durable conversation history."""
import unittest
from dataclasses import replace

from agentforge.context import build_context
from agentforge.conversation_context import ConversationTurn, ContextBudgetExceeded
from agentforge.context_assembler import ContextAssembler
from agentforge.model_types import LOCAL_QWEN
from agentforge.model_registry import parse_deepseek_models
from tests.test_multimodel import discovery_payload


def _history(turns: int, chars: int = 240):
    rows = []
    for turn in range(turns):
        rows.append(ConversationTurn("conversation-a", "repository-a", turn * 2 + 1,
                                     "user", f"Turn {turn + 1} question " + "q" * chars))
        rows.append(ConversationTurn("conversation-a", "repository-a", turn * 2 + 2,
                                     "assistant", f"Turn {turn + 1} answer " + "a" * chars))
    return tuple(rows)


def _deepseek_descriptor():
    return replace(parse_deepseek_models(discovery_payload())[0],
                   capability_source="official DeepSeek /models documentation example fixture",
                   capability_verified_at="2026-11-06T00:00:00+00:00")


class ContextAssemblerTests(unittest.TestCase):
    def setUp(self):
        self.assembler = ContextAssembler()
        self.context = build_context("What should the next step use?", [])
        self.base_messages = [
            {"role": "system", "content": "Answer the current request using the supplied context."},
            {"role": "user", "content": "What should the next step use?"},
        ]

    def _conversation(self, history, summary=None):
        return {"conversation_id": "conversation-a", "repository_id": "repository-a",
                "persisted_history": history, "summary": summary}

    def test_qwen_4096_window_compresses_only_request_view(self):
        history = _history(30)
        durable = tuple((row.sequence, row.content) for row in history)
        conversation = self._conversation(history, "Early decision: preserve source identities.")
        messages = self.assembler.assemble_context(
            self.context, conversation, self.base_messages, descriptor=LOCAL_QWEN,
            requested_output_tokens=256, mode="general")
        diagnostic = conversation["token_budget_diagnostics"]
        self.assertEqual(4096, diagnostic["context_window_tokens"])
        self.assertEqual("estimated", diagnostic["tokenizer_method"])
        self.assertGreater(diagnostic["dropped_history_messages"], 0)
        self.assertTrue(diagnostic["summary_included"])
        self.assertLessEqual(diagnostic["estimated_input_tokens"], diagnostic["input_budget_tokens"])
        self.assertEqual(durable, tuple((row.sequence, row.content) for row in conversation["persisted_history"]))
        self.assertEqual("What should the next step use?", messages[-1]["content"])

    def test_deepseek_large_window_keeps_full_100_turn_history(self):
        history = _history(100, chars=300)
        descriptor = _deepseek_descriptor()
        conversation = self._conversation(history, "Summary covering all previous turns.")
        messages = self.assembler.assemble_context(
            self.context, conversation, self.base_messages, descriptor=descriptor,
            requested_output_tokens=256, mode="general")
        diagnostic = conversation["token_budget_diagnostics"]
        self.assertEqual(1_048_576, diagnostic["context_window_tokens"])
        self.assertEqual(200, diagnostic["persisted_history_messages"])
        self.assertEqual(200, diagnostic["retained_history_sequences"].__len__())
        self.assertEqual(0, diagnostic["dropped_history_messages"])
        self.assertTrue(diagnostic["summary_suppressed_as_duplicate"])
        self.assertFalse(diagnostic["summary_included"])
        self.assertLessEqual(diagnostic["estimated_input_tokens"], diagnostic["input_budget_tokens"])
        self.assertIn("Turn 1 question", "\n".join(message["content"] for message in messages))
        self.assertIn("Turn 100 answer", "\n".join(message["content"] for message in messages))

    def test_switch_deepseek_to_qwen_and_back_reassembles_without_mutating_history(self):
        history = _history(100, chars=300)
        durable = tuple((row.sequence, row.role, row.content) for row in history)
        conversation = self._conversation(history, "Source-linked summary for the full session.")
        deepseek = _deepseek_descriptor()
        self.assembler.assemble_context(self.context, conversation, self.base_messages,
            descriptor=deepseek, requested_output_tokens=256, mode="general")
        full_diagnostic = dict(conversation["token_budget_diagnostics"])
        qwen_messages = self.assembler.assemble_context(self.context, conversation,
            self.base_messages, descriptor=LOCAL_QWEN,
            requested_output_tokens=256, mode="general")
        qwen_diagnostic = dict(conversation["token_budget_diagnostics"])
        self.assembler.assemble_context(self.context, conversation, self.base_messages,
            descriptor=deepseek, requested_output_tokens=256, mode="general")
        restored_diagnostic = conversation["token_budget_diagnostics"]
        self.assertEqual(200, full_diagnostic["retained_history_sequences"].__len__())
        self.assertGreater(qwen_diagnostic["dropped_history_messages"], 0)
        self.assertLess(len(qwen_messages), 200)
        self.assertEqual(200, len(restored_diagnostic["retained_history_sequences"]))
        self.assertEqual(durable, tuple((row.sequence, row.role, row.content)
                                       for row in conversation["persisted_history"]))

    def test_30_and_100_turn_request_views_preserve_recent_turns(self):
        descriptor = _deepseek_descriptor()
        for count in (30, 100):
            with self.subTest(turns=count):
                conversation = self._conversation(_history(count, chars=180))
                messages = self.assembler.assemble_context(self.context, conversation,
                    self.base_messages, descriptor=descriptor,
                    requested_output_tokens=256, mode="general")
                joined = "\n".join(message["content"] for message in messages)
                self.assertIn(f"Turn {count} answer", joined)
                self.assertEqual(count * 2,
                    conversation["token_budget_diagnostics"]["persisted_history_messages"])

    def test_unknown_window_and_overflow_fail_before_provider_use(self):
        unknown = replace(LOCAL_QWEN, context_window=None)
        with self.assertRaisesRegex(ValueError, "no verified context window"):
            self.assembler.budget(unknown, requested_output_tokens=128)
        with self.assertRaises(ContextBudgetExceeded) as caught:
            self.assembler.fit_messages(
                [{"role": "system", "content": "s"},
                 {"role": "user", "content": "required " + "x" * 10_000}],
                descriptor=LOCAL_QWEN, requested_output_tokens=256,
                phase="overflow-test")
        self.assertEqual("CONTEXT_BUDGET_EXCEEDED", caught.exception.diagnostics["failure_code"])
        self.assertGreater(caught.exception.diagnostics["required_tokens"],
                           caught.exception.diagnostics["budget_tokens"])
        self.assertNotIn("budget_bytes", caught.exception.diagnostics)
        self.assertNotIn("x" * 32, str(caught.exception))

    def test_provider_input_cap_and_output_cap_are_independent(self):
        descriptor = replace(_deepseek_descriptor(), max_input_tokens=12_000,
                             provider_max_output_tokens=1_024,
                             adapter_max_output_tokens=512)
        budget = self.assembler.budget(descriptor, requested_output_tokens=256)
        self.assertEqual(12_000, budget.input_budget)
        self.assertEqual(256, budget.output_reserve)
        self.assertEqual((1_024, 512), (budget.provider_max_output_tokens,
                                       budget.adapter_max_output_tokens))
        with self.assertRaisesRegex(ValueError, "exceed.*limit"):
            self.assembler.budget(descriptor, requested_output_tokens=1_025)

    def test_tool_observations_are_separate_from_repository_evidence_and_budgeted(self):
        context = replace(self.context, tool_observations=({
            "tool_id": "synthetic_read", "status": "completed",
            "output_preview": '{"value":42}', "result_ref": "agent-result:run:call",
            "error_code": None, "truncated": False,
        },))
        conversation = self._conversation(())
        messages = self.assembler.assemble_context(
            context, conversation, self.base_messages, descriptor=LOCAL_QWEN,
            requested_output_tokens=256, mode="general")
        prompt = messages[-1]["content"]

        self.assertIn("Tool execution observations", prompt)
        self.assertIn("agent-result:run:call", prompt)
        self.assertIn('\\"value\\":42', prompt)
        self.assertNotIn("[E1]", prompt)
        self.assertLessEqual(conversation["token_budget_diagnostics"]["estimated_input_tokens"],
                             conversation["token_budget_diagnostics"]["input_budget_tokens"])

    def test_requested_input_cap_preserves_model_capability_provenance(self):
        descriptor = _deepseek_descriptor()
        budget = self.assembler.budget(descriptor, requested_output_tokens=256,
                                       requested_input_cap=10_000)
        self.assertEqual(10_000, budget.input_budget)
        self.assertEqual(descriptor.capability_source, budget.capability_source)
        self.assertEqual(descriptor.capability_verified_at, budget.capability_verified_at)

    def test_registered_model_tokenizer_is_used_by_request_assembly(self):
        from agentforge.token_budget import ContextBudgetManager
        descriptor = replace(LOCAL_QWEN, tokenizer_id="test-qwen-tokenizer")
        manager = ContextBudgetManager(minimum_safety_tokens=64, safety_ratio=0)
        manager.register_tokenizer(
            "test-qwen-tokenizer", lambda text: len(text.split()),
            request_counter=lambda messages, tools: 23,
        )
        assembler = ContextAssembler(manager)
        conversation = self._conversation(())
        assembler.assemble_context(
            self.context, conversation, self.base_messages, descriptor=descriptor,
            requested_output_tokens=256, mode="general")
        diagnostics = conversation["token_budget_diagnostics"]
        self.assertEqual("exact", diagnostics["tokenizer_method"])
        self.assertEqual(23, diagnostics["estimated_input_tokens"])
        self.assertIn("model-specific message", diagnostics["token_estimate_description"])

    def test_context_assembly_rejects_invalid_message_and_conversation_shapes(self):
        with self.subTest("missing required system role"):
            with self.assertRaisesRegex(ValueError, "system message and current user message"):
                self.assembler.assemble_context(
                    self.context, self._conversation(()),
                    [{"role": "user", "content": "question"},
                     {"role": "user", "content": "question"}],
                    descriptor=LOCAL_QWEN, requested_output_tokens=256, mode="general",
                )
        with self.subTest("conversation must be a mapping"):
            with self.assertRaisesRegex(ValueError, "mapping or absent"):
                self.assembler.assemble_context(
                    self.context, ["not", "a", "session"], self.base_messages,
                    descriptor=LOCAL_QWEN, requested_output_tokens=256, mode="general",
                )

    def test_oversized_required_observation_fails_without_returning_partial_context(self):
        from agentforge.conversation_context import ContextBudgetExceeded

        context = replace(self.context, tool_observations=({
            "tool_id": "synthetic_read", "status": "completed",
            "output_preview": "x" * 2_001, "result_ref": "result:synthetic:1",
            "error_code": None, "truncated": True,
        },))
        with self.assertRaisesRegex(ValueError, "bounded data shape"):
            self.assembler.assemble_context(
                context, self._conversation(()), self.base_messages, descriptor=LOCAL_QWEN,
                requested_output_tokens=256, mode="general",
            )

        required_context = replace(self.context, question="required " + "x" * 10_000)
        with self.assertRaises(ContextBudgetExceeded) as caught:
            self.assembler.assemble_context(
                required_context, self._conversation(()),
                [{"role": "system", "content": "system"},
                 {"role": "user", "content": required_context.question}],
                descriptor=LOCAL_QWEN, requested_output_tokens=256, mode="general",
            )
        self.assertEqual("required_context_exceeds_budget",
                         caught.exception.diagnostics["failure_code"])
        self.assertIsNone(self.assembler.last_diagnostics)


if __name__ == "__main__":
    unittest.main()
