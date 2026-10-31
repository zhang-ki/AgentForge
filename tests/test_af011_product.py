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
from agentforge.errors import ProviderError
from agentforge.indexer import index_repository
from agentforge.models import Answer, SearchResult
from agentforge.product_retrieval import ProductIntentRouter, ProductRetriever
from agentforge.providers.offline import OfflineProvider
from agentforge.store import IndexStore
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
            self.assertIn("launch_job(item)", app.text)

            docs = retriever.search("Where is the validation policy documented?", top_k=3)
            self.assertEqual(docs.candidates[0].relative_path, "docs/policy.md")
            self.assertEqual(docs.unavailable_channels, ("vector",))

    def test_stable_dedup_keeps_higher_priority_excerpt_and_all_channels(self):
        with IndexStore(":memory:") as store:
            retriever = self._retriever(store)
            first = retriever.search("start_job", top_k=5)
            second = retriever.search("start_job", top_k=5)
        self.assertEqual(first, second)
        self.assertEqual(len({item.relative_path for item in first.candidates}), len(first.candidates))
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
        self.assertEqual(outcome.decision.channels, ("lexical",))

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
