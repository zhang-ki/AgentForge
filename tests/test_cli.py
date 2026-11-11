from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
import sqlite3
from dataclasses import replace
from unittest.mock import MagicMock, Mock, patch

from agentforge.cli import (create_provider, main, parser, _provider_failure_diagnostic,
                            _planner_failure_diagnostic, _validate_persisted_model_selection)
from agentforge.errors import ProviderError
from agentforge.models import Answer
from agentforge.model_registry import LOCAL_QWEN
from agentforge.providers.llama_server import LlamaServerProvider
from agentforge.providers.offline import OfflineProvider
from agentforge.planner import PlanningError
from agentforge.product_retrieval import ProductCandidate, ProductRetrievalOutcome
from agentforge.query_routing import RouteDecision
from agentforge.store import IndexStore
from tests.support import LocalTestCase
from tests.test_context import result


def invoke(arguments, **kwargs):
    output, errors = io.StringIO(), io.StringIO()
    def no_repository_setup(store, _request, vector_retriever):
        return {"scan_report": None, "vector_retriever": vector_retriever}
    with (redirect_stdout(output), redirect_stderr(errors),
          patch("agentforge.cli._prepare_repository_index", side_effect=no_repository_setup)):
        code = main(arguments, **kwargs)
    return code, output.getvalue(), errors.getvalue()


class CliTests(LocalTestCase):
    def test_ask_defaults_to_planner_routing_and_keeps_explicit_debug_override(self):
        default_args = parser().parse_args(["ask", "explain this project question"])
        repository_override = parser().parse_args([
            "ask", "inspect this source", "--mode", "repository",
        ])

        self.assertEqual("auto", default_args.mode)
        self.assertTrue(default_args.use_chroma)
        self.assertEqual("repository", repository_override.mode)
        no_vector_args = parser().parse_args([
            "ask", "inspect this source", "--no-chroma",
        ])
        self.assertFalse(no_vector_args.use_chroma)

    def test_agent_memory_flag_requires_an_explicit_model_provider(self):
        from agentforge.cli import _run_selected_agent
        arguments = parser().parse_args([
            "agent", "inspect the repository", "--repo", "D:\\synthetic-repo", "--use-memory",
        ])
        self.assertTrue(arguments.use_memory)
        with self.assertRaisesRegex(ValueError, "explicit model provider"):
            _run_selected_agent(arguments, Mock())

    def test_deepseek_agent_consent_covers_selected_memory(self):
        from agentforge.cli import _run_selected_agent
        arguments = parser().parse_args([
            "agent", "inspect the repository", "--repo", "D:\\synthetic-repo",
            "--provider", "deepseek", "--model", "deepseek-chat", "--use-memory",
        ])
        with self.assertRaisesRegex(ValueError, "selected Memory"):
            _run_selected_agent(arguments, Mock())

    def test_agent_cli_streaming_tracks_execution_and_suspends_approval(self):
        from agentforge.agent_loop import AgentRunReport

        def report(status):
            return AgentRunReport(
                objective="inspect the synthetic repository", status=status,
                max_steps=4, completed_steps=1, elapsed_seconds=0.01,
                tool_timeout_seconds=30, run_timeout_seconds=120,
                allowed_tools=("read",), audit=(), answer="validated answer",
                citations=(), offline=False,
            )

        def run_with_agent_event(_arguments, _store, *, on_event, cancel_event):
            self.assertIsNone(cancel_event)
            on_event.publish_agent_event({
                "type": "STATE_CHANGED", "execution_id": "execution-1",
                "task_id": "task-1", "sequence": 1, "state": "PLANNING",
            })
            return report("completed"), None

        events = []
        with (
            patch("agentforge.repositories.open_index_for_repository",
                  return_value=nullcontext(Mock())),
            patch("agentforge.cli._run_selected_agent", side_effect=run_with_agent_event),
        ):
            code, _output, errors = invoke(
                ["agent", "inspect the synthetic repository", "--repo", str(self.root)],
                event_callback=events.append,
            )

        self.assertEqual((code, errors), (0, ""))
        self.assertEqual("execution-1", next(
            event.execution_id for event in events if event.type.value == "STATE_CHANGED"))
        self.assertIn("CONTENT_DELTA", [event.type.value for event in events])
        self.assertIn("COMPLETED", [event.type.value for event in events])

        events.clear()
        with (
            patch("agentforge.repositories.open_index_for_repository",
                  return_value=nullcontext(Mock())),
            patch("agentforge.cli._run_selected_agent", return_value=(report("waiting_approval"), None)),
        ):
            code, _output, errors = invoke(
                ["agent", "inspect the synthetic repository", "--repo", str(self.root)],
                event_callback=events.append,
            )

        types = [event.type.value for event in events]
        self.assertEqual((code, errors), (1, ""))
        self.assertIn("WAITING_APPROVAL", types)
        self.assertNotIn("COMPLETED", types)

    def test_deepseek_binding_rejects_stale_capability_metadata(self):
        from types import SimpleNamespace
        args = SimpleNamespace(
            provider_id="deepseek", model_id="deepseek-flash", cloud_consent=True,
            thinking="off", effort=None,
        )
        with patch("agentforge.model_registry.ModelRegistry") as registry_type:
            registry_type.return_value.get_for_request.side_effect = ValueError(
                "DeepSeek capability metadata is stale or unavailable.")
            with self.assertRaisesRegex(ValueError, "stale or unavailable"):
                _validate_persisted_model_selection(args)
            registry_type.return_value.get_for_request.assert_called_once_with("deepseek-flash")
            registry_type.return_value.get.assert_not_called()

    def test_general_and_auto_general_do_not_open_repository_index(self):
        class GeneralProvider:
            provider_id = "llama-server"
            model = "qwen3.5-4b"
            descriptor = LOCAL_QWEN

            def plan_completion(self, messages, **kwargs):
                return json.dumps({
                    "intent": "general",
                    "tasks": [{"task_id": "g1", "task_type": "general",
                               "question": "Explain attention", "requires_retrieval": False,
                               "search_queries": [], "dependencies": []}],
                    "requires_repository": False, "requires_external_data": False,
                    "requires_agent_execution": False,
                    "answer_strategy": "direct", "clarification_required": False,
                    "confidence": 0.9, "reason": "General question.",
                })

            def answer_general(self, context, **kwargs):
                self.context = context
                return Answer("Attention relates token representations.", (), False)

        for mode in ("general", "auto"):
            with self.subTest(mode=mode):
                provider = GeneralProvider()
                with (
                    patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "test-only-key"}),
                    patch("agentforge.cli.create_provider", return_value=provider),
                    patch("agentforge.cli._open_current_index") as open_index,
                ):
                    code, output, errors = invoke([
                        "ask", "Explain attention in a transformer.", "--mode", mode,
                        "--provider", "llama-server", "--base-url", "http://127.0.0.1:32123",
                        "--model", "qwen3.5-4b",
                    ])
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual("limited", json.loads(output)["answer_contract"]["status"])
                payload = json.loads(output)
                self.assertEqual("general", payload["retrieval"]["mode"])
                self.assertEqual("UNREGISTERED", payload["retrieval"]["index_lifecycle"]["state"])
                self.assertEqual((), provider.context.evidence)
                open_index.assert_not_called()

    def test_auto_repository_plan_without_active_index_still_answers_without_source_claims(self):
        class RepositoryQuestionProvider:
            provider_id = "llama-server"
            model = "qwen3.5-4b"
            descriptor = LOCAL_QWEN

            def plan_completion(self, _messages, **_kwargs):
                return json.dumps({
                    "intent": "general",
                    "tasks": [{"task_id": "r1", "task_type": "repository",
                               "question": "Inspect this project's implementation.",
                               "requires_retrieval": True,
                               "search_queries": ["project implementation"],
                               "dependencies": []}],
                    "requires_repository": True, "requires_external_data": False,
                    "requires_agent_execution": False,
                    "answer_strategy": "direct", "clarification_required": False,
                    "confidence": 0.9, "reason": "The question refers to project behavior.",
                })

            def answer_hybrid(self, context, **_kwargs):
                self.context = context
                return Answer(
                    "General knowledge:\nA general explanation remains available.\n\n"
                    "Repository evidence:\nI cannot confirm this project's implementation "
                    "from the current evidence.", (), False,
                )

            def answer_general(self, context, **_kwargs):
                self.context = context
                return Answer(
                    "A general explanation remains available; the current project implementation "
                    "cannot be confirmed without a bound repository.", (), False,
                )

        provider = RepositoryQuestionProvider()
        with (
            patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "test-only-key"}),
            patch("agentforge.cli.create_provider", return_value=provider),
            patch("agentforge.cli._has_active_repository_index", return_value=False),
            patch("agentforge.cli._open_current_index") as open_index,
        ):
            code, output, errors = invoke([
                "ask", "How does this project implement the feature?",
                "--provider", "llama-server", "--base-url", "http://127.0.0.1:32123",
                "--model", "qwen3.5-4b",
            ])

        self.assertEqual((code, errors), (0, ""))
        payload = json.loads(output)
        self.assertEqual("not_required", payload["retrieval"]["retrieval_status"])
        self.assertEqual("no_repository_bound_to_session",
                         payload["retrieval"]["retrieval_reason_code"])
        self.assertIsNone(payload["retrieval"]["retrieval_failure_code"])
        self.assertEqual((), provider.context.evidence)
        self.assertEqual("limited", payload["answer_contract"]["status"])
        open_index.assert_not_called()

    def test_session_current_is_available_before_any_repository_is_registered(self):
        from types import SimpleNamespace
        from agentforge.cli import _session_command

        with (
            patch("agentforge.cli._conversation_engine", return_value=Mock()),
            patch("agentforge.cli.REPOSITORY_CATALOG_PATH",
                  self.sandbox / "not-created" / "repositories.sqlite3"),
            patch("agentforge.cli.DATABASE_PATH", self.sandbox / "not-created" / "index.sqlite3"),
        ):
            result = _session_command(SimpleNamespace(session_action="current"))

        self.assertEqual({"repository": None, "result": None}, result)

    def test_hybrid_cli_keeps_general_answer_when_repository_evidence_exists(self):
        store = Mock()
        store.metadata.return_value = {"repository": str(self.root)}
        scope = MagicMock()
        scope.__enter__.return_value = store
        scope.__exit__.return_value = False
        product_retriever = self._mock_product_retriever()
        candidate = replace(
            product_retriever.search.return_value.candidates[0],
            text="def fifo_pop(queue):\n    return queue.pop(0)  # FIFO removes the first entry",
        )

        class HybridProvider:
            provider_id = "llama-server"
            model = "qwen3.5-4b"
            descriptor = LOCAL_QWEN

            def plan_completion(self, messages, **kwargs):
                return json.dumps({
                    "intent": "hybrid",
                    "tasks": [
                        {"task_id": "g1", "task_type": "general", "question": "Explain FIFO",
                         "requires_retrieval": False, "search_queries": [], "dependencies": []},
                        {"task_id": "r1", "task_type": "repository",
                         "question": "Find FIFO implementation", "requires_retrieval": True,
                         "search_queries": ["FIFO implementation"], "dependencies": ["g1"]},
                    ],
                    "requires_repository": True, "requires_external_data": False,
                    "requires_agent_execution": False,
                    "answer_strategy": "compose", "clarification_required": False,
                    "confidence": 0.9, "reason": "Combined general and repository question.",
                })

            def answer_hybrid(self, context, **kwargs):
                self.context = context
                return Answer(
                    "General knowledge:\nFIFO removes the oldest item first.\n"
                    "Repository evidence:\nThe current source handles the requested symbol [E1].",
                    tuple(item.citation for item in context.evidence), False,
                )

        provider = HybridProvider()
        with (
            patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "test-only-key"}),
            patch("agentforge.cli.create_provider", return_value=provider),
            patch("agentforge.cli._open_current_index", return_value=scope),
            patch("agentforge.cli.ProductRetriever", return_value=product_retriever),
            patch("agentforge.cli._context_candidates", return_value=(candidate,)),
        ):
            code, output, errors = invoke([
                "ask", "Explain FIFO generally and how this project implements it.",
                "--mode", "hybrid", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123", "--model", "qwen3.5-4b",
            ])
        self.assertEqual((code, errors), (0, ""))
        payload = json.loads(output)
        self.assertEqual("hybrid", payload["retrieval"]["mode"])
        self.assertEqual("limited", payload["answer_contract"]["status"])
        self.assertEqual(["general", "repository"],
                         [item["origin"] for item in payload["answer_contract"]["claims"]])
        self.assertTrue(provider.context.evidence)

    def test_provider_failure_diagnostic_is_bounded_to_codes_and_request_counts(self):
        provider = Mock()
        provider.request_stats.return_value = {
            "attempted": 1, "sent": 1, "succeeded": 1, "failed": 0, "retries": 0,
            "last_application_failure_phase": "citation_validation",
            "last_application_error_code": "required_evidence_role_not_cited",
            "last_citation_diagnostic": {
                "rule_id": "required_evidence_role_not_cited",
                "required_roles": ("callsite", "target_definition"),
                "available_roles": ("callsite", "target_definition"),
                "cited_roles": ("callsite",),
                "missing_roles": ("target_definition",),
                "validation_result": "rejected",
                "snippet": "private source text must not be emitted",
            },
            "last_response_diagnostic": {"content_chars": 123, "finish_reason": "stop",
                                          "content": "private model output"},
            "api_key": "never-copy-this",
        }
        diagnostic = _provider_failure_diagnostic(provider)
        encoded = json.dumps(diagnostic)
        self.assertEqual(1, diagnostic["attempted"])
        self.assertEqual("required_evidence_role_not_cited",
                         diagnostic["citation"]["rule_id"])
        self.assertNotIn("private source text", encoded)
        self.assertNotIn("private model output", encoded)
        self.assertNotIn("never-copy-this", encoded)

    def test_planner_failure_diagnostic_contains_shape_counts_not_query_text(self):
        error = PlanningError(
            "LLM Planner returned invalid bounded search queries.",
            diagnostic_code="invalid_bounded_search_queries",
            diagnostic_details={
                "query_count": 4, "empty_query_count": 0, "non_string_count": 0,
                "oversized_query_count": 0, "max_query_chars": 17,
                "planner_request_count": 2, "correction_attempted": True,
                "correction_status": "rejected",
                "initial_query_shape": {"query_count": 4, "max_query_chars": 17},
                "query": "PRIVATE_QUERY_MUST_NOT_BE_LOGGED",
            },
        )

        diagnostic = _planner_failure_diagnostic(error)
        encoded = json.dumps(diagnostic)

        self.assertEqual("invalid_bounded_search_queries", diagnostic["failure_code"])
        self.assertEqual(2, diagnostic["planner_request_count"])
        self.assertEqual("rejected", diagnostic["correction_status"])
        self.assertEqual(4, diagnostic["final_query_shape"]["query_count"])
        self.assertNotIn("PRIVATE_QUERY_MUST_NOT_BE_LOGGED", encoded)

    def test_planner_schema_rejection_is_not_reported_as_invalid_configuration(self):
        class InvalidPlannerProvider:
            descriptor = LOCAL_QWEN
            provider_id = "llama-server"
            model = "synthetic-qwen"

            def __init__(self):
                self.calls = 0

            def plan_completion(self, _messages, **_kwargs):
                self.calls += 1
                payload = {
                    "intent": "repository",
                    "tasks": [{
                        "task_id": "r1", "task_type": "repository",
                        "question": "Find the requested implementation",
                        "requires_retrieval": True,
                        "search_queries": ["PRIVATE_INTERNAL_SEARCH_PHRASE"] * 4,
                        "dependencies": [],
                    }],
                    "requires_repository": True, "requires_external_data": False,
                    "requires_agent_execution": False, "answer_strategy": "evidence_based",
                    "clarification_required": False, "confidence": 0.9,
                    "reason": "Synthetic invalid search query shape.",
                }
                return json.dumps(payload)

        provider = InvalidPlannerProvider()
        with patch("agentforge.cli.create_provider", return_value=provider):
            code, output, errors = invoke([
                "ask", "what does conv2d do", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123", "--model", "synthetic-qwen",
            ])

        self.assertEqual(1, code)
        self.assertEqual("", output)
        self.assertIn("AgentForge planning failed:", errors)
        self.assertNotIn("Invalid configuration:", errors)
        self.assertIn("AF_PLANNER_DIAGNOSTIC=", errors)
        self.assertEqual(2, provider.calls)
        self.assertNotIn("PRIVATE_INTERNAL_SEARCH_PHRASE", errors)

    def test_conversation_budget_failure_is_not_mislabeled_as_configuration_error(self):
        source = result(text="Explain this symbol\nPRIVATE_SYNTHETIC_SOURCE_" + "x" * 5000)
        candidate = ProductCandidate(
            source.repository, source.relative_path, source.start_line, source.end_line,
            source.text, source.content_hash, source.score, ("symbols",),
            (("symbols", source.score),), symbol_ids=("symbol-1",),
            resolution_status="resolved", evidence_roles=("definition",),
        )
        outcome = ProductRetrievalOutcome(
            "Explain this symbol", "test-route", RouteDecision(
                "exact_symbol", ("symbols",), "test", "test route", "high",
            ), (candidate,), (), (), None,
        )
        store = Mock()
        store.metadata.return_value = {"repository": str(self.root)}
        store_scope = MagicMock()
        store_scope.__enter__.return_value = store
        store_scope.__exit__.return_value = False
        conversation_store = Mock()
        conversation_store.prepare_history.return_value = ((), None)
        engine = Mock()
        with (
            patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "test-only-key"}),
            patch("agentforge.cli._open_current_index", return_value=store_scope),
            patch("agentforge.cli.ProductRetriever", return_value=Mock()),
            patch("agentforge.cli._conversation_engine", return_value=engine),
            patch("agentforge.cli._load_conversation", return_value=(
                conversation_store, "repo-id", (), {
                    "conversation_id": "conversation-id", "repository_id": "repo-id",
                    "history": (),
                },
            )),
            patch("agentforge.cli._conversation_retrieval", return_value=(outcome, None)),
            patch("agentforge.cli._context_candidates", return_value=(candidate,)),
            patch("agentforge.cli.LlamaServerClient") as client_factory,
        ):
            code, output, errors = invoke([
                "ask", "Explain this symbol", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123", "--model", "synthetic",
                "--conversation-id", "conversation-id", "--mode", "repository",
            ])
        self.assertEqual(1, code)
        self.assertEqual("", output)
        self.assertIn("Context budget exceeded:", errors)
        self.assertIn("AF_CONTEXT_BUDGET_DIAGNOSTIC=", errors)
        self.assertNotIn("Invalid configuration:", errors)
        self.assertNotIn("PRIVATE_SYNTHETIC_SOURCE", errors)
        client_factory.return_value.chat.assert_not_called()
        engine.dispose.assert_called_once()

    def test_global_memory_scope_is_explicit_in_cli(self):
        arguments = parser().parse_args(["memory", "approve", "memory-id", "--scope", "global"])
        self.assertEqual("global", arguments.scope)

    def _mock_product_retriever(self):
        source = result()
        candidate = ProductCandidate(
            source.repository, source.relative_path, source.start_line, source.end_line,
            source.text, source.content_hash, source.score, ("symbols",),
            (("symbols", source.score),), symbol_ids=("symbol-1",),
            resolution_status="resolved",
        )
        outcome = ProductRetrievalOutcome(
            "example", "test-route", RouteDecision(
                "exact_symbol", ("symbols", "lexical"), "test", "test route", "high",
            ), (candidate,), (), (), None,
        )
        retriever = Mock()
        retriever.search.return_value = outcome
        return retriever

    def test_help_and_missing_required_repository(self):
        for args, expected in [(["--help"], 0), (["index"], 2), ([], 2), (["search", "x", "--top-k", "bad"], 2)]:
            with self.subTest(args=args):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as exit_info:
                        main(args)
                self.assertEqual(exit_info.exception.code, expected)

    def test_offline_is_the_default_provider(self):
        self.assertIsInstance(create_provider(), OfflineProvider)

    def test_explicit_loopback_configuration_constructs_provider_without_request(self):
        provider = create_provider(
            "llama-server", "http://127.0.0.1:32123/v1/chat/completions", "synthetic-model", "test-only-key"
        )
        self.assertIsInstance(provider, LlamaServerProvider)
        self.assertEqual((provider.client.host, provider.client.port), ("127.0.0.1", 32123))
        self.assertEqual(provider.model, "synthetic-model")
        self.assertEqual(provider.client.api_key, "test-only-key")
        self.assertEqual(provider.client.connect_timeout, 2.0)
        self.assertEqual(provider.client.read_timeout, 60.0)
        self.assertEqual(provider.client.total_timeout, 180.0)
        self.assertEqual(provider.client.max_output_tokens, 512)

    def test_llama_timeout_options_are_independent_and_passed_to_client(self):
        store = Mock()
        store.metadata.return_value = {"repository": str(self.root)}
        store_scope = Mock()
        store_scope.__enter__ = Mock(return_value=store)
        store_scope.__exit__ = Mock(return_value=False)
        fake_client = Mock()
        fake_client.chat.return_value = "Mock answer [E1]"
        product_retriever = self._mock_product_retriever()
        with (
            patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "test-only-key"}),
            patch("agentforge.cli._open_current_index", return_value=store_scope),
            patch("agentforge.cli.ProductRetriever", return_value=product_retriever),
            patch("agentforge.cli.LlamaServerClient", return_value=fake_client) as client_factory,
        ):
            code, _, errors = invoke([
                "ask", "example", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123", "--model", "synthetic-model",
                "--mode", "repository",
                "--connect-timeout", "3.5", "--read-timeout", "75",
                "--total-timeout", "210", "--max-output-tokens", "128",
            ])
        self.assertEqual((code, errors), (0, ""))
        client_factory.assert_called_once_with(
            "http://127.0.0.1:32123", api_key="test-only-key",
            connect_timeout=3.5, read_timeout=75.0,
            total_timeout=210.0, max_output_tokens=128,
        )

    def test_read_timeout_cli_rejects_invalid_values_and_help_documents_bound(self):
        for value in ("invalid", "4.999", "120.001", "nan", "inf", "-inf"):
            with self.subTest(value=value):
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as exit_info:
                        main(["ask", "example", "--read-timeout", value])
                self.assertEqual(exit_info.exception.code, 2)
                self.assertIn("read-timeout", errors.getvalue())
        for option, values in (
            ("--total-timeout", ("invalid", "9.999", "300.001", "nan", "inf")),
            ("--max-output-tokens", ("invalid", "0", "1.5")),
        ):
            for value in values:
                with self.subTest(option=option, value=value):
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        with self.assertRaises(SystemExit) as exit_info:
                            main(["ask", "example", option, value])
                    self.assertEqual(exit_info.exception.code, 2)
                    self.assertIn(option.removeprefix("--"), errors.getvalue())
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            with self.assertRaises(SystemExit) as exit_info:
                main(["ask", "example", "--help"])
        self.assertEqual(exit_info.exception.code, 0)
        self.assertIn("--connect-timeout", output.getvalue())
        self.assertIn("--read-timeout", output.getvalue())
        self.assertIn("default: 60", output.getvalue())
        self.assertIn("--total-timeout", output.getvalue())
        self.assertIn("--max-output-tokens", output.getvalue())

    def test_llama_provider_requires_explicit_valid_configuration_before_store_access(self):
        cases = [
            (["ask", "example", "--provider", "llama-server", "--model", "synthetic"], "--base-url is required"),
            (["ask", "example", "--provider", "llama-server", "--base-url", "http://127.0.0.1:32123"], "--model is required"),
            (["ask", "example", "--provider", "llama-server", "--base-url", "http://localhost:32123", "--model", "synthetic"], "Invalid --base-url"),
            (["ask", "example", "--base-url", "http://127.0.0.1:32123", "--model", "synthetic"], "require --provider llama-server"),
            (["ask", "example", "--read-timeout", "75"], "require --provider llama-server"),
            (["ask", "example", "--provider", "llama-server", "--base-url", "http://127.0.0.1:32123", "--model", "synthetic"], "AGENTFORGE_LLAMA_API_KEY is required"),
        ]
        for args, expected in cases:
            api_key = "" if "AGENTFORGE_LLAMA_API_KEY" in expected else "test-only-key"
            with self.subTest(args=args), patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": api_key}), patch("agentforge.cli._open_current_index") as store_factory:
                code, output, errors = invoke(args)
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn(expected, errors)
                store_factory.assert_not_called()

    def test_explicit_llama_provider_receives_retrieved_context_and_validates_answer(self):
        store = Mock()
        store.metadata.return_value = {"repository": str(self.root)}
        store_scope = Mock()
        store_scope.__enter__ = Mock(return_value=store)
        store_scope.__exit__ = Mock(return_value=False)
        fake_client = Mock()
        fake_client.chat.return_value = "Mock retrieval summary [E1]"
        product_retriever = self._mock_product_retriever()
        with (
            patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "test-only-key"}),
            patch("agentforge.cli._open_current_index", return_value=store_scope),
            patch("agentforge.cli.ProductRetriever", return_value=product_retriever),
            patch("agentforge.cli.LlamaServerClient", return_value=fake_client) as client_factory,
        ):
            code, output, errors = invoke([
                "ask", "example", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123/v1/chat/completions",
                "--model", "synthetic-model", "--mode", "repository",
            ])
        self.assertEqual((code, errors), (0, ""))
        payload = json.loads(output)
        self.assertEqual(payload["provider"], "llama-server")
        self.assertEqual(payload["answer_contract"]["status"], "limited")
        self.assertIn("Mock retrieval summary", payload["answer"])
        product_retriever.search.assert_called_once_with("example", top_k=5)
        client_factory.assert_called_once_with(
            "http://127.0.0.1:32123/v1/chat/completions", api_key="test-only-key",
            connect_timeout=2.0, read_timeout=60.0,
            total_timeout=180.0, max_output_tokens=512,
        )
        messages = fake_client.chat.call_args.args[0]
        self.assertIn("Question:\nexample", messages[1]["content"])
        self.assertIn("[E1]", messages[1]["content"])
        self.assertNotIn("src/example.py:4-5", messages[1]["content"])
        self.assertIn("applicable current evidence ID", messages[0]["content"])
        self.assertEqual(fake_client.chat.call_args.kwargs["model"], "synthetic-model")

    def test_low_confidence_missing_symbol_allows_safe_model_answer_without_repository_claim(self):
        store = Mock()
        store.metadata.return_value = {"repository": str(self.root)}
        store_scope = Mock()
        store_scope.__enter__ = Mock(return_value=store)
        store_scope.__exit__ = Mock(return_value=False)
        outcome = ProductRetrievalOutcome(
            "missing_symbol", "test-route", RouteDecision(
                "exact_symbol", ("symbols", "lexical"), "bare_identifier",
                "No exact symbol is present; lexical matches remain unverified.", "low",
            ), (), (), (), "No exact symbol is present.",
        )
        retriever = Mock()
        retriever.search.return_value = outcome
        fake_client = Mock()
        fake_client.chat.return_value = (
            "General knowledge:\nA function with this name would depend on its implementation.\n\n"
            "Repository evidence:\nI cannot confirm a definition in the current source evidence."
        )
        with (
            patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "test-only-key"}),
            patch("agentforge.cli._open_current_index", return_value=store_scope),
            patch("agentforge.cli.ProductRetriever", return_value=retriever),
            patch("agentforge.cli.LlamaServerClient", return_value=fake_client),
        ):
            code, output, errors = invoke([
                "ask", "missing_symbol", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123", "--model", "synthetic-model",
                "--mode", "repository",
            ])
        self.assertEqual((code, errors), (0, ""))
        payload = json.loads(output)
        self.assertEqual(payload["provider"], "llama-server")
        self.assertEqual(payload["answer_contract"]["status"], "limited")
        self.assertIn("A function with this name", payload["answer"])
        self.assertIn("cannot confirm", payload["answer"])
        self.assertEqual([], payload["answer_contract"]["evidence"])
        fake_client.chat.assert_called_once()

    def test_llama_configuration_or_connection_errors_never_fall_back_to_success(self):
        store = Mock()
        store.metadata.return_value = {"repository": str(self.root)}
        store_scope = Mock()
        store_scope.__enter__ = Mock(return_value=store)
        store_scope.__exit__ = Mock(return_value=False)
        fake_client = Mock()
        fake_client.chat.side_effect = ProviderError("Could not communicate with the configured local server.")
        product_retriever = self._mock_product_retriever()
        with (
            patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "test-only-key"}),
            patch("agentforge.cli._open_current_index", return_value=store_scope),
            patch("agentforge.cli.ProductRetriever", return_value=product_retriever),
            patch("agentforge.cli.LlamaServerClient", return_value=fake_client),
        ):
            code, output, errors = invoke([
                "ask", "example", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123", "--model", "synthetic-model",
                "--mode", "repository",
            ])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Could not communicate", errors)
        self.assertNotIn("离线", errors)
        self.assertNotIn("test-only-key", errors)

    def test_empty_status_does_not_create_database(self):
        path = self.root / "missing.sqlite3"
        with patch("agentforge.cli.DATABASE_PATH", path), patch("agentforge.cli.REPOSITORY_CATALOG_PATH", self.root / "no-catalog.sqlite3"):
            code, output, errors = invoke(["status"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["exists"])
        self.assertEqual(errors, "")
        self.assertFalse(path.exists())

    def test_invalid_configuration_and_missing_repository(self):
        with IndexStore(":memory:") as store, patch("agentforge.cli._open_current_index") as factory:
            factory.return_value.__enter__.return_value = store
            for args, expected in [
                (["index", "--repo", str(self.root), "--window-lines", "0"], 2),
                (["index", "--repo", str(self.root / "missing")], 1),
                (["search", "example", "--top-k", "0"], 2),
                (["ask", "example", "--context-budget", "0"], 2),
            ]:
                code, _, errors = invoke(args)
                self.assertEqual(code, expected)
                self.assertTrue(errors)

    def test_real_sqlite_error_is_reported_without_traceback(self):
        with IndexStore(":memory:") as store, patch("agentforge.cli._open_current_index") as factory:
            factory.return_value.__enter__.return_value = store
            store.connection.execute("DROP TABLE metadata")
            code, _, errors = invoke(["search", "example"])
            self.assertEqual(code, 1)
            self.assertIn("OperationalError", errors)
            self.assertNotIn("Traceback", errors)

