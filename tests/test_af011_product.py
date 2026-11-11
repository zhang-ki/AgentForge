from dataclasses import replace
import hashlib
import unittest

from agentforge.answer_contract import (
    ANSWER_CONTRACT_VERSION,
    AnswerClaim,
    build_offline_contract,
    validate_answer_contract,
    verify_generated_answer,
)
from agentforge.context import build_context
from agentforge.model_types import LOCAL_QWEN
from agentforge.errors import ProviderError
from agentforge.indexer import index_repository
from agentforge.models import Answer, SearchResult
from agentforge.product_retrieval import ProductIntentRouter, ProductRetriever
from agentforge.providers.offline import OfflineProvider
from agentforge.store import IndexStore
from agentforge.token_budget import TokenBudgetManager
from tests.support import LocalTestCase


class AF011ProductRetrievalTests(LocalTestCase):
    def setUp(self):
        super().setUp()
        self.write("pkg/__init__.py", (
            "from .impl import start_job as launch_job\n"
            "__all__ = ['launch_job']\n"
        ))
        self.write("pkg/impl.py", (
            "def start_job(item: str) -> str:\n"
            "    \"\"\"Start a queued job.\"\"\"\n"
            "    return 'started:' + item\n"
        ))
        self.write("app.py", (
            "from pkg import launch_job\n\n"
            "def run_queue(item: str) -> str:\n"
            "    return launch_job(item)\n"
        ))
        self.write("docs/policy.md", "Queued jobs must be started only after validation.\n")

    def _retriever(self, store):
        index_repository(self.root, store)
        return ProductRetriever(store, self.root)

    def test_oversized_class_behavior_query_uses_exact_header_and_method_ranges(self):
        from agentforge.cli import _context_candidates
        from agentforge.providers.llama_server import (
            _ANSWER_SYSTEM_PROMPT, _conversation_messages,
        )
        from agentforge.providers.dialogue_protocol import _model_evidence_text

        lines = [
            "class Conv2d(Module):",
            "    def __init__(self, weight):",
            "        self.weight = weight",
            "    def _configure_kernels(self):",
        ]
        lines.extend(
            f"        self.kernel_{index:02d} = configure_kernel(self.weight, axis={index}, mode='spatial')"
            for index in range(58)
        )
        lines.extend((
            "    def forward(self, input):",
            "        output = input",
        ))
        lines.extend(
            f"        output = convolution(output, self.weight, stride={index % 4 + 1})"
            for index in range(28)
        )
        lines.extend(("        return output", ""))
        full_source = "\n".join(lines)
        self.write("nn/module.py", full_source)
        query = "what do the function Conv2d do?"

        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            outcome = retriever.search(query, top_k=5)
            selected = _context_candidates(outcome, None, store=store, retriever=retriever)
            context = build_context(
                query, [item.as_search_result() for item in selected],
                max_candidates=5, budget=6000,
            )
            prompt = (f"Question:\n{query}\n\nUntrusted evidence:\n"
                      f"{_model_evidence_text(context)}\n\nUse only these evidence IDs: "
                      f"{', '.join('[' + source.source_id + ']' for source in context.evidence)}. "
                      "Cite the applicable IDs.")
            messages = _conversation_messages(context, {
                "conversation_id": "conversation-1", "repository_id": "repo-1", "history": (),
            }, [
                {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ], descriptor=LOCAL_QWEN)

        total_tokens = TokenBudgetManager().measure_messages(messages).count
        joined = "\n".join(item["content"] for item in messages)
        self.assertGreater(len(full_source.encode("utf-8")), 3500)
        self.assertLessEqual(total_tokens, TokenBudgetManager().budget(
            LOCAL_QWEN, requested_output_tokens=256).input_budget)
        self.assertIn("class Conv2d(Module):", joined)
        self.assertIn("def forward(self, input):", joined)
        self.assertIn("convolution(output, self.weight", joined)
        self.assertNotIn("configure_kernel(self.weight", joined)
        self.assertEqual(2, len(context.evidence))
        self.assertEqual((1, 1), (context.evidence[0].result.start_line,
                                 context.evidence[0].result.end_line))
        self.assertLess(context.evidence[1].result.start_line,
                        context.evidence[1].result.end_line)

    def test_unqualified_same_name_symbol_prefers_unique_public_export_and_case_exact_match(self):
        from agentforge.cli import _context_candidates
        from agentforge.providers.llama_server import (
            _ANSWER_SYSTEM_PROMPT, _conversation_messages,
        )
        from agentforge.providers.dialogue_protocol import _model_evidence_text

        large_body = "\n".join(
            f"        self.kernel_{index:02d} = configure_kernel(input, axis={index})"
            for index in range(60)
        )
        public_source = (
            "class Conv2d(Layer):\n"
            "    def _configure_kernels(self, input):\n"
            f"{large_body}\n"
            "    def forward(self, input):\n"
            "        return convolution(input)\n"
        )
        internal_source = (
            "class Conv2d(Function):\n"
            "    def forward(self, input):\n"
            "        return low_level_convolution(input)\n"
            + "\n".join(
                f"    # internal note {index:02d} about convolution dispatch"
                for index in range(100)
            )
        )
        self.write("pkg/layers.py", public_source)
        self.write("pkg/__init__.py", "from .layers import Conv2d\n")
        self.write("internal.py", internal_source)
        self.write("pkg/ops.py", "def conv2d(input):\n    return input\n")
        query = "what do the function Conv2d do?"

        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            outcome = retriever.search(query, top_k=5)
            selected = _context_candidates(
                outcome, {"conversation_id": "conversation-1", "repository_id": "repo-1"},
                store=store, retriever=retriever,
            )
            context = build_context(
                query, [item.as_search_result() for item in selected],
                max_candidates=5, budget=6000,
            )
            prompt = (f"Question:\n{query}\n\nUntrusted evidence:\n"
                      f"{_model_evidence_text(context)}\n\nUse only these evidence IDs: "
                      f"{', '.join('[' + source.source_id + ']' for source in context.evidence)}. "
                      "Cite the applicable IDs.")
            messages = _conversation_messages(context, {
                "conversation_id": "conversation-1", "repository_id": "repo-1", "history": (),
            }, [
                {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ], descriptor=LOCAL_QWEN)

        total_tokens = TokenBudgetManager().measure_messages(messages).count
        joined = "\n".join(item["content"] for item in messages)
        self.assertGreater(len(public_source.encode("utf-8")), 1400)
        self.assertEqual(
            [("pkg/layers.py", 1, 1),
             ("pkg/layers.py", len(public_source.splitlines()) - 1,
              len(public_source.splitlines()))],
            [(item.relative_path, item.start_line, item.end_line) for item in selected],
        )
        self.assertLessEqual(total_tokens, TokenBudgetManager().budget(
            LOCAL_QWEN, requested_output_tokens=256).input_budget)
        self.assertIn("return convolution(input)", joined)
        self.assertNotIn("low_level_convolution", joined)
        self.assertNotIn("def conv2d(", joined)

    def test_intent_routes_are_deterministic_and_research_router_is_not_changed(self):
        router = ProductIntentRouter()
        cases = {
            "start_job": ("exact_symbol", ("symbols", "lexical")),
            "What does function start_job return?": (
                "exact_symbol", ("symbols", "lexical")
            ),
            "Which caller calls launch_job?": (
                "dependency_relation", ("package_relations", "symbols", "lexical")
            ),
            "Which package reexports launch_job as a public API?": (
                "package_api", ("package_relations", "lexical")
            ),
            "Where is the validation policy documented?": (
                "documentation", ("lexical", "vector")
            ),
            "How does the queue implementation work?": (
                "business_implementation", ("lexical", "vector")
            ),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                decision = router.route(query)
                self.assertEqual((decision.route, decision.channels), expected)
        uncertain = router.route("Can a dynamic import resolve this plugin at runtime?")
        self.assertEqual((uncertain.route, uncertain.confidence), ("uncertain", "low"))

    def test_formula_question_with_code_identifiers_is_relevant_but_still_requires_source_match(self):
        from types import SimpleNamespace
        from agentforge.evidence_evaluator import assess_evidence

        query = (
            "Use an authorized read-only tool to open synthetic_calculation.py in this "
            "repository, then report the formula in calculate_weighted_total and its result "
            "for amount 100 with fee rate 0.05."
        )
        router = ProductIntentRouter()
        decision = router.route(query)
        self.assertEqual(("business_implementation", "medium"),
                         (decision.route, decision.confidence))
        self.assertEqual("formula_for_code_identifier", decision.rule)

        matching = SimpleNamespace(
            score=1.0, evidence_roles=("definition",),
            relative_path="synthetic_calculation.py",
            text=("def calculate_weighted_total(amount, fee_rate):\n"
                  "    return round(amount * (1 + fee_rate), 2)"),
        )
        unrelated = SimpleNamespace(
            score=1.0, evidence_roles=("definition",), relative_path="src/unrelated.py",
            text="def normalize_color(value):\n    return value.casefold()",
        )
        self.assertEqual((matching,), assess_evidence(query, decision, (matching,)).relevant_candidates)
        rejected = assess_evidence(query, decision, (unrelated,))
        self.assertEqual((), rejected.relevant_candidates)
        self.assertEqual("NO_DETERMINISTIC_RELEVANCE", rejected.candidate_results[0]["reason_code"])

    def test_symbol_relation_reference_and_document_evidence_keep_provenance(self):
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            symbol = retriever.search("start_job", top_k=3)
            self.assertEqual(symbol.decision.route, "exact_symbol")
            self.assertEqual(symbol.candidates[0].relative_path, "pkg/impl.py")
            self.assertIn("symbols", symbol.candidates[0].channels)
            self.assertTrue(symbol.candidates[0].symbol_ids)

            exported = retriever.search(
                "Which package reexports launch_job as a public API?", top_k=3
            )
            self.assertEqual(exported.candidates[0].relative_path, "pkg/__init__.py")
            self.assertIn("package_relations", exported.candidates[0].channels)
            self.assertEqual(exported.candidates[0].resolution_status, "resolved")
            self.assertIn("launch_job", exported.candidates[0].text)

            caller = retriever.search("Which caller calls launch_job?", top_k=5)
            app = next(item for item in caller.candidates if item.relative_path == "app.py")
            self.assertIn("references", app.channels)
            self.assertIn("def run_queue", app.text)
            self.assertIn("launch_job(item)", app.text)
            implementation = next(
                item for item in caller.candidates if item.relative_path == "pkg/impl.py"
            )
            self.assertIn("def start_job", implementation.text)
            self.assertEqual("resolved", implementation.resolution_status)
            self.assertTrue(implementation.symbol_ids)

            docs = retriever.search("Where is the validation policy documented?", top_k=3)
            self.assertEqual(docs.candidates[0].relative_path, "docs/policy.md")
            self.assertEqual(docs.unavailable_channels, ("vector",))

    def test_vector_results_are_not_starved_when_lexical_channel_fills_top_k(self):
        from agentforge.embeddings.vector_store import VectorCandidate
        from agentforge.scanner import scan_repository

        for number in range(4):
            self.write(
                f"lexical_{number}.py",
                f"# calc_unique_marker appears in lexical note {number}\nVALUE = {number}\n",
            )
        self.write(
            "semantic.py",
            "def describe_shipping_fee(base, rate):\n    return base * (1 + rate)\n",
        )

        class Vectors:
            def __init__(self, chunk):
                self.chunk = chunk

            def inspect_consistency(self):
                return {"complete": True, "expected_vectors": 1,
                        "stored_vectors": 1, "invalid_vectors": 0}

            def search(self, _query, *, top_k):
                return [VectorCandidate(
                    "Fsynthetic", self.chunk["relative_path"], "code",
                    self.chunk["start_line"], self.chunk["end_line"],
                    self.chunk["text"], self.chunk["content_hash"],
                    "synthetic-embedding-model", None, 0.9,
                )][:top_k]

        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            chunk = next(row for row in store.iter_chunks()
                         if row["relative_path"] == "semantic.py")
            outcome = ProductRetriever(
                store, self.root,
                vector_retriever=Vectors(dict(chunk)),
                scan_report=scan_repository(self.root),
            ).search("calc_unique_marker", top_k=2)

        self.assertEqual(len(outcome.candidates), 2)
        self.assertTrue(any("lexical" in item.channels for item in outcome.candidates))
        vector_candidates = [item for item in outcome.candidates if "vector" in item.channels]
        self.assertEqual(len(vector_candidates), 1)
        self.assertEqual(vector_candidates[0].relative_path, "semantic.py")
        self.assertEqual(vector_candidates[0].content_hash, chunk["content_hash"])

    def test_cross_file_call_includes_exact_callsite_and_final_definition(self):
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            outcome = retriever.search(
                "Which caller calls launch_job and where is its implementation?", top_k=1,
            )
            context = build_context(outcome.query,
                                    [item.as_search_result() for item in outcome.candidates],
                                    max_candidates=5, budget=6000)
        self.assertTrue(any(item.relative_path == "app.py" and item.start_line <= 3 <= item.end_line
                            and "launch_job(item)" in item.text for item in outcome.candidates))
        self.assertTrue(any(item.relative_path == "pkg/impl.py" and item.start_line <= 1 <= item.end_line
                            and "def start_job" in item.text for item in outcome.candidates))
        self.assertTrue(any(item.result.relative_path == "app.py" and
                            item.result.start_line <= 3 <= item.result.end_line
                            for item in context.evidence))
        self.assertTrue(any(item.result.relative_path == "pkg/impl.py" and
                            item.result.start_line <= 1 <= item.result.end_line
                            for item in context.evidence))
        required_roles = {role for item in outcome.candidates for role in item.evidence_roles}
        self.assertTrue({"callsite", "target_definition"} <= required_roles)
        self.assertEqual("caller_definition", outcome.candidates[0].evidence_roles[0])

    def test_role_contract_rejects_import_and_reexport_as_substitutes_for_call_and_definition(self):
        from agentforge.errors import CitationValidationError
        from agentforge.providers.llama_server import _model_evidence_text
        question = "Which caller calls launch_job and where is its implementation?"
        values = [
            SearchResult("synthetic", "app.py", 2, 3,
                         "def run_queue(item):\n    return launch_job(item)", "a" * 64, 5,
                         ("caller_definition", "definition")),
            SearchResult("synthetic", "app.py", 3, 3, "return launch_job(item)", "b" * 64, 4,
                         ("callsite",)),
            SearchResult("synthetic", "pkg/__init__.py", 1, 1,
                         "from .impl import start_job as launch_job", "c" * 64, 3, ("reexport",)),
        ]
        context = build_context(question, values, max_candidates=3)
        with self.assertRaises(CitationValidationError) as caught:
            verify_generated_answer(Answer("run_queue calls the exported function [E1] [E2] [E3].", (), False),
                                    context, route="dependency_relation", route_confidence="high")
        self.assertEqual("required_evidence_role_unavailable", caught.exception.code)
        self.assertIn("[application evidence role: actual callsite]", _model_evidence_text(context))

    def test_callsite_and_target_definition_must_both_be_cited(self):
        from agentforge.errors import CitationValidationError
        question = "Which caller calls launch_job and where is its implementation?"
        values = [
            SearchResult("synthetic", "app.py", 2, 3,
                         "def run_queue(item):\n    return launch_job(item)", "a" * 64, 5,
                         ("caller_definition", "definition")),
            SearchResult("synthetic", "app.py", 3, 3, "return launch_job(item)", "b" * 64, 4,
                         ("callsite",)),
            SearchResult("synthetic", "pkg/impl.py", 1, 2,
                         "def start_job(item):\n    return item", "c" * 64, 3,
                         ("target_definition", "definition")),
        ]
        context = build_context(question, values, max_candidates=3)
        with self.assertRaises(CitationValidationError) as caught:
            verify_generated_answer(Answer("run_queue calls start_job [E2] [E3].", (), False), context,
                                    route="dependency_relation", route_confidence="high")
        self.assertEqual("required_evidence_role_not_cited", caught.exception.code)
        verified = verify_generated_answer(
            Answer("run_queue calls start_job [E1] [E2] [E3].", (), False),
            context, route="dependency_relation", route_confidence="high")
        self.assertEqual("limited", verified.status)
        forged = replace(verified, evidence=(
            replace(verified.evidence[0], evidence_roles=("target_definition",)),
            *verified.evidence[1:],
        ))
        with self.assertRaises(ProviderError):
            validate_answer_contract(forged, context)

    def test_insufficient_budget_cannot_silently_drop_a_required_evidence_role(self):
        from agentforge.errors import CitationValidationError
        question = "Which caller calls launch_job and where is its implementation?"
        values = [
            SearchResult("synthetic", "app.py", 2, 3,
                         "def run_queue(item):\n    return launch_job(item)", "a" * 64, 5,
                         ("caller_definition", "definition")),
            SearchResult("synthetic", "app.py", 3, 3, "return launch_job(item)", "b" * 64, 4,
                         ("callsite",)),
            SearchResult("synthetic", "pkg/impl.py", 1, 2,
                         "def start_job(item):\n    return item", "c" * 64, 3,
                         ("target_definition", "definition")),
        ]
        first_only = build_context(question, values[:1], max_candidates=1)
        context = build_context(question, values, max_candidates=3,
                                budget=first_only.estimated_tokens + 1)
        self.assertEqual(1, len(context.evidence))
        with self.assertRaises(CitationValidationError) as caught:
            verify_generated_answer(Answer("run_queue calls start_job [E1].", (), False), context,
                                    route="dependency_relation", route_confidence="high")
        self.assertEqual("required_evidence_role_unavailable", caught.exception.code)

    def test_configuration_use_and_definition_have_distinct_evidence_roles(self):
        from agentforge.evidence_roles import required_evidence_roles
        from agentforge.errors import CitationValidationError
        question = "Where does function max_attempts get RETRY_LIMIT and what is its value?"
        self.assertEqual(("configuration_use", "configuration_definition"),
                         required_evidence_roles(question))
        context = build_context(question, [
            SearchResult("synthetic", "worker.py", 3, 3, "return RETRY_LIMIT", "a" * 64, 5,
                         ("configuration_use",)),
            SearchResult("synthetic", "settings.py", 1, 1, "RETRY_LIMIT = 3", "b" * 64, 4,
                         ("configuration_definition", "definition")),
        ], max_candidates=2)
        with self.assertRaises(CitationValidationError) as caught:
            verify_generated_answer(Answer("It returns the configured value [E1].", (), False), context,
                                    route="exact_symbol", route_confidence="high")
        self.assertEqual("required_evidence_role_not_cited", caught.exception.code)
        verified = verify_generated_answer(
            Answer("It uses RETRY_LIMIT, defined as 3 [E1] [E2].", (), False), context,
            route="exact_symbol", route_confidence="high")
        self.assertEqual("limited", verified.status)

    def test_import_and_actual_callsite_remain_distinct_evidence_ranges(self):
        with IndexStore(":memory:") as store:
            outcome = self._retriever(store).search("Which caller calls launch_job?", top_k=5)
        app = [item for item in outcome.candidates if item.relative_path == "app.py"]
        self.assertTrue(any(item.start_line == 1 and "from pkg import launch_job" in item.text
                            for item in app))
        self.assertTrue(any(item.start_line == 3 and "return launch_job(item)" in item.text
                            for item in app))
        self.assertNotEqual(
            next(item.citation for item in app if item.start_line == 1),
            next(item.citation for item in app if item.start_line == 3),
        )

    def test_reexport_evidence_does_not_replace_the_final_definition(self):
        with IndexStore(":memory:") as store:
            outcome = self._retriever(store).search("Which caller calls launch_job?", top_k=5)
        self.assertTrue(any(item.relative_path == "pkg/__init__.py"
                            and "from .impl import start_job as launch_job" in item.text
                            for item in outcome.candidates))
        self.assertTrue(any(item.relative_path == "pkg/impl.py" and "def start_job" in item.text
                            for item in outcome.candidates))

    def test_multilevel_import_and_call_chain_keeps_each_relation_kind(self):
        self.write("api.py", "from pkg import launch_job as api_launch\n")
        self.write("app.py", "from api import api_launch\ndef run_queue(item):\n    return api_launch(item)\n")
        with IndexStore(":memory:") as store:
            outcome = self._retriever(store).search(
                "Which caller calls api_launch and where is its implementation?", top_k=10,
            )
        ranges = {(item.relative_path, item.start_line, item.text) for item in outcome.candidates}
        self.assertTrue(any(path == "app.py" and "return api_launch(item)" in text
                            for path, _line, text in ranges))
        self.assertTrue(any(path == "api.py" and "from pkg import launch_job" in text
                            for path, _line, text in ranges))
        self.assertTrue(any(path == "pkg/__init__.py" and "as launch_job" in text
                            for path, _line, text in ranges))
        self.assertTrue(any(path == "pkg/impl.py" and "def start_job" in text
                            for path, _line, text in ranges))

    def test_configuration_query_returns_use_import_and_constant_without_session(self):
        from agentforge.cli import _context_candidates
        self.write("settings.py", "RETRY_LIMIT = 3\n")
        self.write("worker.py", "from settings import RETRY_LIMIT\ndef max_attempts():\n    return RETRY_LIMIT\n")
        self.write("other.py", "RETRY_LIMIT = 99\n")
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            outcome = retriever.search(
                "Where does function max_attempts get RETRY_LIMIT and what is its value?", top_k=5,
            )
            candidates = _context_candidates(outcome, None, store=store, retriever=retriever)
        expected = {
            ("worker.py", 1, "from settings import RETRY_LIMIT"),
            ("worker.py", 2, "def max_attempts():\n    return RETRY_LIMIT"),
            ("settings.py", 1, "RETRY_LIMIT = 3"),
        }
        found = {(item.relative_path, item.start_line, item.text) for item in candidates}
        self.assertTrue(expected <= found)
        self.assertFalse(any(item.relative_path == "other.py" for item in candidates))
        self.assertTrue(any("configuration_use" in item.evidence_roles
                            for item in candidates if item.relative_path == "worker.py"))
        self.assertTrue(any("configuration_definition" in item.evidence_roles
                            for item in candidates if item.relative_path == "settings.py"))

    def test_function_return_statement_is_selected_as_source_evidence(self):
        from agentforge.cli import _context_candidates
        self.write("settings.py", "RETRY_LIMIT = 3\n")
        self.write("worker.py", "from settings import RETRY_LIMIT\ndef max_attempts():\n    return RETRY_LIMIT\n")
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            outcome = retriever.search("Where does function max_attempts get RETRY_LIMIT?", top_k=5)
            selected = _context_candidates(outcome, None, store=store, retriever=retriever)
        self.assertTrue(any(item.relative_path == "worker.py" and item.start_line <= 3 <= item.end_line
                            and "return RETRY_LIMIT" in item.text for item in selected))

    def test_evidence_budget_does_not_make_omitted_id_citable(self):
        from agentforge.errors import CitationValidationError
        first_text, second_text = "first", "second"
        results = [
            SearchResult("synthetic", "first.py", 1, 1, first_text,
                         hashlib.sha256(first_text.encode()).hexdigest(), 2.0),
            SearchResult("synthetic", "second.py", 1, 1, second_text,
                         hashlib.sha256(second_text.encode()).hexdigest(), 1.0),
        ]
        one = build_context("question", results[:1], max_candidates=1)
        budget = one.estimated_tokens + 1
        context = build_context("question", results, max_candidates=2, budget=budget)
        self.assertEqual((1, "E1"), (len(context.evidence), context.evidence[0].source_id))
        with self.assertRaises(CitationValidationError):
            verify_generated_answer(Answer("Unsupported claim [E2]", (), False), context,
                                    route="exact_symbol", route_confidence="high")

    def test_correct_citation_does_not_upgrade_unsupported_conclusion(self):
        context = build_context("What does this function do?", [AF011AnswerContractTests._result()])
        contract = verify_generated_answer(
            Answer("It sends a network request [E1].", (), False), context,
            route="exact_symbol", route_confidence="high",
        )
        self.assertEqual("limited", contract.status)
        self.assertEqual("inference", contract.claims[0].kind)
        self.assertIn("semantic entailment has not been verified", contract.reason)

    def test_static_relationship_is_not_promoted_to_runtime_fact(self):
        context = build_context("Does this import execute at runtime?", [AF011AnswerContractTests._result()])
        contract = verify_generated_answer(
            Answer("The source includes a call expression [E1].", (), False), context,
            route="dependency_relation", route_confidence="high",
            uncertainty="Static source relationships do not prove runtime binding or execution.",
        )
        self.assertEqual("limited", contract.status)
        self.assertIn("do not prove runtime", contract.reason)

    def test_same_named_symbol_in_two_repositories_keeps_evidence_isolated(self):
        self.write("same.py", "def shared():\n    return 'repository-alpha'\n")
        other = self.sandbox / "other-repository"
        other.mkdir()
        (other / "same.py").write_text(
            "def shared():\n    return 'repository-beta'\n", encoding="utf-8",
        )
        with IndexStore(":memory:") as alpha_store, IndexStore(":memory:") as beta_store:
            alpha = ProductRetriever(alpha_store, self.root)
            beta = ProductRetriever(beta_store, other)
            index_repository(self.root, alpha_store)
            index_repository(other, beta_store)
            alpha_result = alpha.search("What does function shared return?", top_k=3)
            beta_result = beta.search("What does function shared return?", top_k=3)
        self.assertTrue(any("repository-alpha" in item.text for item in alpha_result.candidates))
        self.assertFalse(any("repository-beta" in item.text for item in alpha_result.candidates))
        self.assertTrue(any("repository-beta" in item.text for item in beta_result.candidates))
        self.assertFalse(any("repository-alpha" in item.text for item in beta_result.candidates))

    def test_stable_dedup_keeps_higher_priority_excerpt_and_all_channels(self):
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            first = retriever.search("start_job", top_k=5)
            second = retriever.search("start_job", top_k=5)
        self.assertEqual(first, second)
        self.assertEqual(len({item.citation for item in first.candidates}), len(first.candidates))
        item = first.candidates[0]
        self.assertEqual(item.start_line, 1)
        self.assertIn("symbols", item.channels)
        self.assertIn("lexical", item.channels)

    def test_dynamic_relation_is_exposed_as_uncertain(self):
        self.write("dynamic.py", "import importlib\nplugin = importlib.import_module('pkg.impl')\n")
        with IndexStore(":memory:") as store:
            outcome = self._retriever(store).search(
                "Can a dynamic import resolve this plugin at runtime?", top_k=3
            )
        self.assertEqual(outcome.decision.route, "uncertain")
        self.assertIsNotNone(outcome.uncertainty)
        self.assertEqual(outcome.decision.channels, ("lexical", "symbols"))

    def test_chinese_and_mixed_language_exact_symbol_queries(self):
        self.write("nn.py", (
            "class Conv2d:\n"
            "    def forward(self, input_tensor, weight):\n"
            "        return apply_kernel(input_tensor, weight)\n"
        ))
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            class_result = retriever.search("Conv2d 的作用是什么？", top_k=3)
            method_result = retriever.search("方法 forward 的第二个参数是什么？", top_k=3)
            false_premise = retriever.search("类 MissingLayer 的输出是什么？", top_k=3)

        self.assertEqual("exact_symbol", class_result.decision.route)
        self.assertEqual("nn.py", class_result.candidates[0].relative_path)
        self.assertIn("class Conv2d", class_result.candidates[0].text)
        self.assertEqual("exact_symbol", method_result.decision.route)
        self.assertIn("def forward(self, input_tensor, weight)", method_result.candidates[0].text)
        self.assertEqual("exact_symbol", false_premise.decision.route)
        self.assertEqual("low", false_premise.decision.confidence)
        self.assertFalse(false_premise.candidates)

    def test_symbol_comparison_returns_both_exact_definitions(self):
        self.write("shared.py", "def shared(value, amount):\n    return value + amount\n\ndef shared_backup(value, amount):\n    return value - amount\n")
        with IndexStore(":memory:") as store:
            outcome = self._retriever(store).search("函数 shared_backup 与 shared 不同在哪里？")
        texts = [candidate.text for candidate in outcome.candidates if candidate.symbol_ids]
        self.assertTrue(any("def shared(" in text and "value + amount" in text for text in texts))
        self.assertTrue(any("def shared_backup(" in text and "value - amount" in text for text in texts))

    def test_session_keeps_referenced_configuration_definition_for_exact_symbol(self):
        from agentforge.cli import _context_candidates
        self.write("settings.py", "RETRY_LIMIT = 3\n")
        self.write("worker.py", "from settings import RETRY_LIMIT\n"
                   "def max_attempts():\n    return RETRY_LIMIT\n")
        self.write("other.py", "RETRY_LIMIT = 99\n")
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            outcome = retriever.search("Where does function max_attempts get RETRY_LIMIT and what is its value?", top_k=10)
            candidates = _context_candidates(outcome, {"conversation_id": "synthetic"},
                                             store=store, retriever=retriever)
        paths = {item.relative_path for item in candidates}
        self.assertIn("worker.py", paths)
        self.assertIn("settings.py", paths)
        self.assertNotIn("other.py", paths)
        self.assertTrue(any(item.relative_path == "worker.py" and item.start_line == 1 and
                            "from settings import RETRY_LIMIT" in item.text and
                            item.resolution_status == "unresolved" for item in candidates))
        self.assertTrue(any(item.relative_path == "settings.py" and item.resolution_status == "uncertain"
                            for item in candidates))
        context = build_context(outcome.query, [item.as_search_result() for item in candidates], max_candidates=10)
        contract = build_offline_contract(OfflineProvider().answer(context), context,
            route="exact_symbol", route_confidence="high",
            candidate_by_citation={item.citation: item for item in candidates})
        self.assertTrue(any(item.relative_path == "worker.py" and item.start_line == 1 and
                            item.resolution_status == "unresolved" for item in contract.evidence))
        self.assertTrue(any(item.relative_path == "settings.py" and item.resolution_status == "uncertain"
                            for item in contract.evidence))

    def test_unimported_similar_configuration_is_not_added_to_session_context(self):
        from agentforge.cli import _context_candidates
        self.write("settings.py", "RETRY_LIMIT = 3\n")
        self.write("worker.py", "def max_attempts():\n    return RETRY_LIMIT\n")
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            outcome = retriever.search("Where does function max_attempts get RETRY_LIMIT?", top_k=10)
            selected = _context_candidates(outcome, {"conversation_id": "synthetic"},
                                           store=store, retriever=retriever)
        self.assertEqual({"worker.py"}, {item.relative_path for item in selected})

    def test_followup_resolves_explicit_function_when_module_has_same_name(self):
        from agentforge.cli import _followup_symbol
        self.write("shared.py", "def shared(value, amount):\n    return value + amount\n")
        rows = [{"role": "user", "content": "What does function shared return? Cite evidence."},
                {"role": "assistant", "content": "It adds its arguments [E1]."}]
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            self.assertGreaterEqual(len(store.search_symbols(name="shared", limit=2)), 2)
            self.assertEqual("shared", _followup_symbol("What does its second parameter represent?", rows, store))
            self.write("other.py", "def shared(value, amount):\n    return value - amount\n")
            index_repository(self.root, store)
            self.assertIsNone(_followup_symbol("What does its second parameter represent?", rows, store))

    def test_ambiguous_module_and_package_do_not_supply_configuration(self):
        from agentforge.cli import _context_candidates
        self.write("settings.py", "RETRY_LIMIT = 3\n")
        self.write("settings/__init__.py", "RETRY_LIMIT = 99\n")
        self.write("worker.py", "from settings import RETRY_LIMIT\ndef max_attempts():\n    return RETRY_LIMIT\n")
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            outcome = retriever.search("Where does function max_attempts get RETRY_LIMIT?", top_k=10)
            selected = _context_candidates(outcome, {"conversation_id": "synthetic"},
                                           store=store, retriever=retriever)
        self.assertEqual({"worker.py"}, {item.relative_path for item in selected})

    def test_stale_index_evidence_is_excluded_until_reindex(self):
        self.write("pkg/impl.py", "def start_job(item: str) -> str:\n    return 'old:' + item\n")
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            retriever = ProductRetriever(store, self.root)
            self.write("pkg/impl.py", "def start_job(item: str) -> str:\n    return 'new:' + item\n")
            result = retriever.search("old_marker", top_k=5)
            self.assertFalse(result.candidates)
            self.assertIn("changed since indexing", result.uncertainty)

    def test_product_retrieval_rejects_index_bound_to_another_repository(self):
        self.write("pkg/impl.py", "def start_job():\n    return 'ok'\n")
        other = self.sandbox / "other"
        other.mkdir()
        (other / "other.py").write_text("def unrelated(): pass\n", encoding="utf-8")
        with IndexStore(":memory:") as store:
            index_repository(other, store)
            retriever = ProductRetriever(store, self.root)
            with self.assertRaisesRegex(ValueError, "different repository"):
                retriever.search("start_job")


class AF011AnswerContractTests(unittest.TestCase):
    @staticmethod
    def _result():
        text = "def start_job(item):\n    return 'started:' + item"
        return SearchResult(
            "synthetic", "pkg/impl.py", 1, 2, text,
            hashlib.sha256(text.encode()).hexdigest(), 10.0,
        )

    def test_offline_contract_is_limited_and_evidence_is_exact(self):
        context = build_context("start_job", [self._result()])
        answer = OfflineProvider().answer(context)
        contract = build_offline_contract(
            answer, context, route="exact_symbol", route_confidence="high",
        )
        self.assertEqual((contract.version, contract.status),
                         (ANSWER_CONTRACT_VERSION, "limited"))
        self.assertEqual(contract.evidence[0].citation, "pkg/impl.py:1-2")
        validate_answer_contract(contract, context)

    def test_contract_rejects_shortened_or_unsubstantiated_confirmed_claim(self):
        context = build_context("start_job", [self._result()])
        answer = OfflineProvider().answer(context)
        contract = build_offline_contract(
            answer, context, route="exact_symbol", route_confidence="high",
        )
        shortened = replace(
            contract,
            evidence=(replace(contract.evidence[0], excerpt="def start_job(item):"),),
        )
        with self.assertRaises(ProviderError):
            validate_answer_contract(shortened, context)
        fabricated = replace(
            contract,
            status="answered",
            claims=(AnswerClaim(
                "This function sends a network request.", "confirmed", ("pkg/impl.py:1-2",)
            ),),
        )
        with self.assertRaises(ProviderError):
            validate_answer_contract(fabricated, context)

    def test_no_evidence_and_uncited_generated_text_are_refused(self):
        empty = build_context("missing", [])
        offline = OfflineProvider().answer(empty)
        refused = build_offline_contract(
            offline, empty, route="uncertain", route_confidence="low",
        )
        self.assertEqual(refused.status, "refused")
        context = build_context("start_job", [self._result()])
        generated = verify_generated_answer(
            Answer("The function starts work.", (), False), context,
            route="exact_symbol", route_confidence="high",
        )
        self.assertEqual(generated.status, "refused")

    def test_cited_generated_answer_is_provenance_checked_but_not_semantically_verified(self):
        context = build_context("start_job", [self._result()])
        answer = Answer("It returns a started marker [E1]", (), False)
        contract = verify_generated_answer(
            answer, context, route="exact_symbol", route_confidence="high",
        )
        self.assertEqual(contract.status, "limited")
        self.assertEqual(contract.claims[0].kind, "inference")
        tampered = replace(
            contract,
            evidence=(replace(contract.evidence[0], relative_path="invented.py"),),
        )
        with self.assertRaises(ProviderError):
            validate_answer_contract(tampered, context)
        with self.assertRaises(ProviderError):
            verify_generated_answer(
                Answer("Invented [E99]", (), False), context,
                route="exact_symbol", route_confidence="high",
            )

    def test_static_uncertainty_downgrades_cited_model_text(self):
        context = build_context("dynamic", [self._result()])
        answer = Answer("Static evidence is limited [E1]", (), False)
        contract = verify_generated_answer(
            answer, context, route="uncertain", route_confidence="low",
            uncertainty="Runtime behavior cannot be proven statically.",
        )
        self.assertEqual((contract.status, contract.claims[0].kind),
                         ("limited", "inference"))


if __name__ == "__main__":
    unittest.main()
