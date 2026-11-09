"""Behavioral coverage for model-aware budgets and evidence candidate selection."""

from dataclasses import replace
from types import SimpleNamespace
import unittest

from agentforge.dialogue_retrieval import (
    _context_candidates,
    _focus_large_class_symbol,
    _narrow_explicit_symbol_candidates,
)
from agentforge.model_registry import parse_deepseek_models
from agentforge.product_retrieval import ProductCandidate
from agentforge.token_budget import ContextBudgetManager, TokenizerAdapter


def _deepseek():
    return parse_deepseek_models({"object": "list", "data": [{
        "id": "deepseek-synthetic", "context_window": 1_048_576,
        "max_output_tokens": 393_216,
        "api_capabilities": {"chat_completions": {"streaming": True}},
    }]})[0]


class ModelBudgetBoundaryBehaviorTests(unittest.TestCase):
    def test_registered_model_counter_includes_tools_and_rejects_invalid_exact_counts(self):
        manager = ContextBudgetManager()
        observed = []

        def count(messages, tools):
            observed.append((tuple(messages), tuple(tools)))
            return len(messages) * 11 + len(tools) * 23

        manager.register_tokenizer("synthetic-exact", lambda text: len(text), request_counter=count)
        profile = replace(_deepseek(), tokenizer_id="synthetic-exact")
        messages = [{"role": "user", "content": "synthetic"}]
        tools = [{"type": "function", "function": {"name": "read_synthetic"}}]
        measurement = manager.measure_request(messages, tools=tools, model=profile)
        self.assertEqual(34, measurement.count)
        self.assertEqual("exact", measurement.method)
        self.assertEqual((tuple(messages), tuple(tools)), observed[0])

        invalid = ContextBudgetManager()
        invalid.register_tokenizer("bad", lambda _text: 0,
                                   request_counter=lambda _messages, _tools: True)
        with self.assertRaisesRegex(ValueError, "invalid token count"):
            invalid.measure_request(messages, model=replace(profile, tokenizer_id="bad"))

    def test_utf8_estimator_counts_multibyte_text_and_rejects_surrogate_code_points(self):
        tokenizer = TokenizerAdapter()
        chinese = tokenizer.count_text("中文A")
        self.assertEqual(4, chinese.count)
        self.assertEqual("estimated", chinese.method)
        with self.assertRaisesRegex(ValueError, "invalid Unicode surrogate"):
            tokenizer.count_text("invalid-\ud800")

    def test_selected_model_input_and_output_caps_remain_independent(self):
        manager = ContextBudgetManager(safety_ratio=0, minimum_safety_tokens=0)
        profile = replace(_deepseek(), context_window=8192, max_input_tokens=3000,
                          provider_max_output_tokens=1024, adapter_max_output_tokens=768)
        budget = manager.budget(profile, requested_output_tokens=512)
        self.assertEqual(3000, budget.input_budget)
        self.assertEqual(512, budget.output_reserve)
        self.assertEqual(768, budget.max_output_tokens)
        self.assertEqual(768, manager.resolve_output_budget(profile, mode="max").effective_tokens)


class RetrievalCandidateSelectionBehaviorTests(unittest.TestCase):
    def _large_class(self):
        source = (
            "class Conv2d:\n"
            "    def __init__(self, channels):\n"
            "        self.channels = channels\n"
            "    def forward(self, value):\n"
            "        return value\n"
            + "    # synthetic documentation padding\n" * 70
        )
        self.assertGreater(len(source.encode("utf-8")), 1400)
        return ProductCandidate(
            "synthetic-repo", "src/conv.py", 7, 180, source, "a" * 64, 1.0,
            ("symbols",), (("symbols", 1.0),), symbol_ids=("class-conv2d",),
            resolution_status="resolved", evidence_roles=("definition",),
        )

    def test_large_class_query_selects_the_method_matching_user_intent(self):
        symbol = self._large_class()
        methods = [
            {"symbol_id": "init-id", "parent_symbol_id": "class-conv2d", "name": "__init__",
             "start_line": 8, "end_line": 9},
            {"symbol_id": "forward-id", "parent_symbol_id": "class-conv2d", "name": "forward",
             "start_line": 10, "end_line": 11},
        ]

        class Store:
            def search_symbols(self, **kwargs):
                self.kwargs = kwargs
                return methods

        class Retriever:
            _candidates = ["src/conv.py"]

            def _indexed_file_is_current(self, _path):
                return True

            def _excerpt(self, _path, start, end):
                return {7: "class Conv2d:", 8: "    def __init__(self, channels):",
                        9: "        self.channels = channels", 10: "    def forward(self, value):",
                        11: "        return value"}[start] if start == end else "    def forward(self, value):\n        return value"

        store, retriever = Store(), Retriever()
        behavior = _focus_large_class_symbol("How does the forward method work?", symbol,
                                             store, retriever)
        self.assertIsNotNone(behavior)
        self.assertEqual("class Conv2d:", behavior[0].text)
        self.assertEqual(("forward-id",), behavior[1].symbol_ids)
        self.assertEqual((10, 11), (behavior[1].start_line, behavior[1].end_line))

        parameters = _focus_large_class_symbol("What parameters initialize Conv2d?", symbol,
                                               store, retriever)
        self.assertIsNotNone(parameters)
        self.assertEqual(("init-id",), parameters[1].symbol_ids)
        self.assertIsNone(_focus_large_class_symbol("Explain the repository generally.",
                                                    symbol, store, retriever))

    def test_explicit_qualified_symbol_selection_does_not_merge_same_named_candidates(self):
        left = ProductCandidate("repo", "pkg/a.py", 1, 2, "class Conv2d: pass", "a"*64,
            1.0, ("symbols",), (("symbols", 1.0),), symbol_ids=("a",),
            resolution_status="resolved", evidence_roles=("definition",))
        right = ProductCandidate("repo", "pkg/b.py", 1, 2, "class Conv2d: pass", "b"*64,
            1.0, ("symbols",), (("symbols", 1.0),), symbol_ids=("b",),
            resolution_status="resolved", evidence_roles=("definition",))

        class Store:
            def search_symbols_by_ids(self, ids, *, limit):
                self.last_ids = tuple(ids)
                return [{"name": "Conv2d", "qualified_name": "pkg.A.Conv2d",
                         "relative_path": "pkg/a.py"}] if "a" in ids else [
                            {"name": "Conv2d", "qualified_name": "pkg.Conv2d",
                             "relative_path": "pkg/b.py"}]

        store = Store()
        outcome = SimpleNamespace(query="What does pkg.Conv2d do?", candidates=(left, right),
                                  decision=SimpleNamespace(route="exact_symbol"))
        chosen = _context_candidates(outcome, None)
        self.assertEqual((left, right), chosen, "without a store, the retrieval path keeps source identity")
        selected = _narrow_explicit_symbol_candidates(
            "What does pkg.Conv2d do?", (left, right), store)
        self.assertEqual((right,), selected)
        self.assertEqual(("b",), store.last_ids)


if __name__ == "__main__":
    unittest.main()
