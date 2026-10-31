from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sqlite3
from unittest.mock import Mock, patch

from agentforge.cli import create_provider, main
from agentforge.errors import ProviderError
from agentforge.providers.llama_server import LlamaServerProvider
from agentforge.providers.offline import OfflineProvider
from agentforge.product_retrieval import ProductCandidate, ProductRetrievalOutcome
from agentforge.query_routing import RouteDecision
from agentforge.store import IndexStore
from tests.support import LocalTestCase
from tests.test_context import result


def invoke(arguments):
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = main(arguments)
    return code, output.getvalue(), errors.getvalue()


class CliTests(LocalTestCase):
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
        self.assertEqual(provider.client.max_output_tokens, 256)

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
            patch("agentforge.cli.IndexStore", return_value=store_scope),
            patch("agentforge.cli.ProductRetriever", return_value=product_retriever),
            patch("agentforge.cli.LlamaServerClient", return_value=fake_client) as client_factory,
        ):
            code, _, errors = invoke([
                "ask", "example", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123", "--model", "synthetic-model",
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
            ("--max-output-tokens", ("invalid", "0", "513", "1.5")),
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
            with self.subTest(args=args), patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": api_key}), patch("agentforge.cli.IndexStore") as store_factory:
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
            patch("agentforge.cli.IndexStore", return_value=store_scope),
            patch("agentforge.cli.ProductRetriever", return_value=product_retriever),
            patch("agentforge.cli.LlamaServerClient", return_value=fake_client) as client_factory,
        ):
            code, output, errors = invoke([
                "ask", "example", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123/v1/chat/completions",
                "--model", "synthetic-model",
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
            total_timeout=180.0, max_output_tokens=256,
        )
        messages = fake_client.chat.call_args.args[0]
        self.assertIn("Question:\nexample", messages[1]["content"])
        self.assertIn("[E1]", messages[1]["content"])
        self.assertNotIn("src/example.py:4-5", messages[1]["content"])
        self.assertIn("Cite only supplied IDs", messages[0]["content"])
        self.assertEqual(fake_client.chat.call_args.kwargs["model"], "synthetic-model")

    def test_low_confidence_missing_symbol_is_refused_before_model_request(self):
        store = Mock()
        store.metadata.return_value = {"repository": str(self.root)}
        store_scope = Mock()
        store_scope.__enter__ = Mock(return_value=store)
        store_scope.__exit__ = Mock(return_value=False)
        source = result()
        candidate = ProductCandidate(
            source.repository, source.relative_path, source.start_line, source.end_line,
            source.text, source.content_hash, source.score, ("lexical",),
            (("lexical", source.score),), resolution_status="not_applicable",
        )
        outcome = ProductRetrievalOutcome(
            "missing_symbol", "test-route", RouteDecision(
                "exact_symbol", ("symbols", "lexical"), "bare_identifier",
                "No exact symbol is present; lexical matches remain unverified.", "low",
            ), (candidate,), (), (), "No exact symbol is present.",
        )
        retriever = Mock()
        retriever.search.return_value = outcome
        fake_client = Mock()
        with (
            patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "test-only-key"}),
            patch("agentforge.cli.IndexStore", return_value=store_scope),
            patch("agentforge.cli.ProductRetriever", return_value=retriever),
            patch("agentforge.cli.LlamaServerClient", return_value=fake_client),
        ):
            code, output, errors = invoke([
                "ask", "missing_symbol", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123", "--model", "synthetic-model",
            ])
        self.assertEqual((code, errors), (0, ""))
        payload = json.loads(output)
        self.assertEqual(payload["provider"], "evidence_gate_refusal")
        self.assertEqual(payload["answer_contract"]["status"], "refused")
        self.assertIsNone(payload["answer"])
        fake_client.chat.assert_not_called()

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
            patch("agentforge.cli.IndexStore", return_value=store_scope),
            patch("agentforge.cli.ProductRetriever", return_value=product_retriever),
            patch("agentforge.cli.LlamaServerClient", return_value=fake_client),
        ):
            code, output, errors = invoke([
                "ask", "example", "--provider", "llama-server",
                "--base-url", "http://127.0.0.1:32123", "--model", "synthetic-model",
            ])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Could not communicate", errors)
        self.assertNotIn("离线", errors)
        self.assertNotIn("test-only-key", errors)

    def test_empty_status_does_not_create_database(self):
        path = self.root / "missing.sqlite3"
        with patch("agentforge.cli.DATABASE_PATH", path):
            code, output, errors = invoke(["status"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["exists"])
        self.assertEqual(errors, "")
        self.assertFalse(path.exists())

    def test_invalid_configuration_and_missing_repository(self):
        with IndexStore(":memory:") as store, patch("agentforge.cli.IndexStore") as factory:
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
        with IndexStore(":memory:") as store, patch("agentforge.cli.IndexStore") as factory:
            factory.return_value.__enter__.return_value = store
            store.connection.execute("DROP TABLE metadata")
            code, _, errors = invoke(["search", "example"])
            self.assertEqual(code, 1)
            self.assertIn("OperationalError", errors)
            self.assertNotIn("Traceback", errors)

