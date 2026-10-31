import json
import io
import socket
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, call, patch

from agentforge.embeddings import EmbeddingError, embed_text
from agentforge.embeddings.llama_server import LlamaServerEmbeddingProvider


def _success_body(vector=None):
    if vector is None:
        vector = [1.0] + [0.0] * 1023
    return json.dumps({"data": [{"embedding": vector}]}).encode("utf-8")


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self.body = _success_body() if body is None else body
        self.headers = headers or {}

    def getheader(self, name):
        return self.headers.get(name)

    def read(self, limit):
        return self.body[:limit]


class LlamaServerEmbeddingProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = LlamaServerEmbeddingProvider(
            "http://127.0.0.1:45678", model_id="qwen3-embedding-q8@rev1",
            api_model="qwen3-embedding-q8", connect_timeout=2, read_timeout=60,
        )

    def _connection(self, response=None, *, getresponse_error=None):
        connection = Mock()
        connection.sock = Mock()
        connection.getresponse.return_value = response or FakeResponse()
        if getresponse_error is not None:
            connection.getresponse.side_effect = getresponse_error
        return connection

    def test_query_and_document_inputs_use_distinct_qwen_preprocessing(self):
        connection = self._connection()
        with patch("agentforge.embeddings.llama_server.HTTPConnection", return_value=connection) as factory:
            query = embed_text(self.provider, "Where is token refresh implemented?", role="query")
            document = embed_text(self.provider, "def refresh_token(): pass", role="document")

        self.assertEqual(factory.call_args_list[0].args, ("127.0.0.1", 45678))
        self.assertEqual(factory.call_args_list[0].kwargs, {"timeout": 2.0})
        self.assertEqual(connection.request.call_count, 2)
        first_body = json.loads(connection.request.call_args_list[0].kwargs["body"])
        second_body = json.loads(connection.request.call_args_list[1].kwargs["body"])
        self.assertIn("Instruct: Given a question about a local source repository", first_body["input"])
        self.assertTrue(first_body["input"].endswith("\nQuery: Where is token refresh implemented?"))
        self.assertEqual(second_body["input"], "def refresh_token(): pass")
        self.assertEqual(first_body["model"], "qwen3-embedding-q8")
        self.assertEqual(connection.request.call_args_list[0].args[:2], ("POST", "/v1/embeddings"))
        self.assertEqual(connection.sock.settimeout.call_args_list, [call(60.0)] * 2)
        self.assertEqual(query, document)
        self.assertEqual(len(query), 1024)
        self.assertEqual({
            "attempts": 2,
            "sent": 2,
            "successes": 2,
            "failures": 0,
        }, self.provider.request_counts)

    def test_legacy_embed_uses_document_path(self):
        connection = self._connection()
        with patch("agentforge.embeddings.llama_server.HTTPConnection", return_value=connection):
            vector = self.provider.embed("stored source")
        sent = json.loads(connection.request.call_args.kwargs["body"])
        self.assertEqual(sent["input"], "stored source")
        self.assertEqual(len(vector), 1024)

    def test_rejects_non_loopback_non_http_and_ambiguous_urls(self):
        for url in (
            "http://example.com:1234", "http://192.168.1.4:1234",
            "https://127.0.0.1:1234", "http://127.0.0.1",
            "http://127.0.0.1:1234/v1/embeddings", "http://user@127.0.0.1:1234",
            "http://127.0.0.1:1234/?x=1",
        ):
            with self.subTest(url=url), self.assertRaises(EmbeddingError):
                LlamaServerEmbeddingProvider(url, model_id="m", api_model="m")
        ipv6 = LlamaServerEmbeddingProvider("http://[::1]:45678", model_id="m", api_model="m")
        self.assertEqual(ipv6._host, "::1")

    def test_redirect_http_error_and_connection_timeout_are_explicit(self):
        for response, expected in (
            (FakeResponse(302, b"redirect"), "redirect"),
            (FakeResponse(401, b"secret error body"), "HTTP 401"),
        ):
            connection = self._connection(response)
            with patch("agentforge.embeddings.llama_server.HTTPConnection", return_value=connection):
                with self.assertRaisesRegex(EmbeddingError, expected) as raised:
                    self.provider.embed_query("query")
            self.assertNotIn("secret error body", str(raised.exception))
            connection.request.assert_called_once()

        connection = self._connection(getresponse_error=socket.timeout())
        with patch("agentforge.embeddings.llama_server.HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(EmbeddingError, "timed out"):
                self.provider.embed_query("query")

    def test_embedding_503_is_counted_once_and_never_retried(self):
        connection = self._connection(FakeResponse(503, b'{"error":"not ready"}'))
        with patch("agentforge.embeddings.llama_server.HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(EmbeddingError, "HTTP 503"):
                self.provider.embed_query("one request only")
        connection.request.assert_called_once()
        self.assertEqual({
            "attempts": 1,
            "sent": 1,
            "successes": 0,
            "failures": 1,
        }, self.provider.request_counts)

    def test_health_contract_distinguishes_loading_and_ready_without_embedding(self):
        loading = self._connection(FakeResponse(503, b'{"error":{"message":"Loading model"}}'))
        ready = self._connection(FakeResponse(200, b'{"status":"ok"}'))
        with patch("agentforge.embeddings.llama_server.HTTPConnection", side_effect=[loading, ready]):
            self.assertEqual("loading", self.provider.health())
            self.assertEqual("ready", self.provider.health())
        self.assertEqual([call("GET", "/health", headers={"Accept": "application/json"})],
                         loading.request.call_args_list)
        self.assertEqual([call("GET", "/health", headers={"Accept": "application/json"})],
                         ready.request.call_args_list)
        self.assertEqual(0, self.provider.request_counts["attempts"])
        loading.close.assert_called_once()
        ready.close.assert_called_once()

    def test_wait_ready_polls_loading_but_unknown_health_fails_closed(self):
        with patch.object(self.provider, "health", side_effect=["loading", "ready"]), \
             patch("agentforge.embeddings.llama_server.time.sleep") as sleep:
            elapsed = self.provider.wait_ready(timeout=2, poll_interval=0.1)
        self.assertGreaterEqual(elapsed, 0)
        sleep.assert_called_once()
        self.assertEqual(0, self.provider.request_counts["attempts"])
        with patch.object(self.provider, "health", side_effect=EmbeddingError("unknown health")):
            with self.assertRaisesRegex(EmbeddingError, "unknown health"):
                self.provider.wait_ready(timeout=1, poll_interval=0.1)

    def test_invalid_json_shape_empty_nonfinite_and_wrong_dimension_are_rejected(self):
        invalid = (
            FakeResponse(body=b"not-json"),
            FakeResponse(body=b'{"data": []}'),
            FakeResponse(body=_success_body([])),
            FakeResponse(body=_success_body([float("nan")] + [0.0] * 1023)),
            FakeResponse(body=_success_body([1.0, 0.0])),
        )
        for response in invalid:
            with self.subTest(body=response.body[:40]):
                connection = self._connection(response)
                with patch("agentforge.embeddings.llama_server.HTTPConnection", return_value=connection):
                    with self.assertRaises(EmbeddingError):
                        embed_text(self.provider, "query", role="query")

    def test_timeout_and_identifier_configuration_are_bounded(self):
        for value in (0, -1, 120.1, float("nan"), float("inf"), "5"):
            with self.subTest(value=value), self.assertRaisesRegex(EmbeddingError, "timeout"):
                LlamaServerEmbeddingProvider(
                    "http://127.0.0.1:5", model_id="m", api_model="m", read_timeout=value,
                )
        with self.assertRaisesRegex(EmbeddingError, "dimension"):
            LlamaServerEmbeddingProvider(
                "http://127.0.0.1:5", model_id="m", api_model="m", dimension=0,
            )

    def test_invalid_model_instruction_and_empty_text_fail_before_network(self):
        for changes in ({"model_id": ""}, {"api_model": "bad\nmodel"},
                        {"query_instruction": " "}, {"connect_timeout": 10**10000}):
            args = {"model_id": "m", "api_model": "m", **changes}
            with self.subTest(field=next(iter(changes))), self.assertRaises(EmbeddingError):
                LlamaServerEmbeddingProvider("http://127.0.0.1:5", **args)
        with patch("agentforge.embeddings.llama_server.HTTPConnection") as factory:
            for text in (None, "", "  "):
                with self.subTest(text=text), self.assertRaises(EmbeddingError):
                    self.provider.embed_query(text)
            factory.assert_not_called()
        self.assertEqual(self.provider.request_counts["attempts"], 0)

    def test_response_length_validation_and_transport_error_do_not_retry(self):
        for response, expected in (
            (FakeResponse(headers={"Content-Length": "not-a-number"}), "invalid Content-Length"),
            (FakeResponse(headers={"Content-Length": "999999999"}), "size limit"),
            (FakeResponse(body=b"x" * 2000001), "size limit"),
        ):
            connection = self._connection(response)
            with self.subTest(expected=expected), patch(
                "agentforge.embeddings.llama_server.HTTPConnection", return_value=connection
            ):
                with self.assertRaisesRegex(EmbeddingError, expected):
                    self.provider.embed_query("query")
                connection.request.assert_called_once()
                connection.close.assert_called_once()
        connection = self._connection(getresponse_error=ConnectionResetError("private detail"))
        with patch("agentforge.embeddings.llama_server.HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(EmbeddingError, "unavailable") as caught:
                self.provider.embed_query("query")
            self.assertNotIn("private detail", str(caught.exception))
            connection.request.assert_called_once()
            connection.close.assert_called_once()

    def test_local_evaluation_runner_uses_the_existing_memory_benchmark(self):
        from agentforge.eval_local_embeddings import run_local_benchmark

        class FixedLocalProvider:
            model_id = "mock-qwen-embedding-q8@rev"
            dimension = 1024

            def embed_query(self, text):
                return (1.0,) + (0.0,) * 1023 if "token" in text.lower() else (0.0, 1.0) + (0.0,) * 1022

            def embed_document(self, text):
                return (1.0,) + (0.0,) * 1023 if "token" in text.lower() else (0.0, 1.0) + (0.0,) * 1022

            def embed(self, text):
                return self.embed_document(text)

        with patch("agentforge.eval_local_embeddings.LlamaServerEmbeddingProvider", return_value=FixedLocalProvider()):
            report = run_local_benchmark(
                base_url="http://127.0.0.1:45678",
                model_id="mock-qwen-embedding-q8@rev",
                api_model="mock-qwen-embedding-q8",
            )
        self.assertEqual(report["dataset_version"], "agentforge-retrieval-v1")
        self.assertEqual(report["benchmark"]["retrievers"]["vector"]["grounded_query_count"], 8)
        self.assertEqual(report["vector_index"]["database"], "SQLite :memory:")
        self.assertGreater(report["vector_index"]["blob_bytes"], 0)
        self.assertEqual(report["benchmark"]["vector_abstention"]["status"], "exploratory_only")

    def test_local_evaluation_can_use_pending_review_expansion_without_changing_v1(self):
        from agentforge.eval_local_embeddings import CANDIDATE_DATASET_PATH, run_local_benchmark

        class FixedLocalProvider:
            model_id = "mock-qwen-embedding-q8@rev"
            dimension = 2

            def embed_query(self, text):
                return (1.0, 0.0)

            def embed_document(self, text):
                return (1.0, 0.0)

            def embed(self, text):
                return self.embed_document(text)

        with patch("agentforge.eval_local_embeddings.LlamaServerEmbeddingProvider", return_value=FixedLocalProvider()):
            report = run_local_benchmark(
                base_url="http://127.0.0.1:45678",
                model_id="mock-qwen-embedding-q8@rev",
                api_model="mock-qwen-embedding-q8",
                dimension=2,
                dataset_path=CANDIDATE_DATASET_PATH,
            )
        self.assertEqual(report["query_total"], 12)
        self.assertEqual(report["benchmark"]["review_status"], "pending_human_review")

    def test_local_evaluation_reports_sanitized_provider_error_detail(self):
        from agentforge.eval_local_embeddings import main

        stderr = io.StringIO()
        with patch(
            "agentforge.eval_local_embeddings.run_local_benchmark",
            side_effect=EmbeddingError("Local embedding server returned HTTP 400."),
        ), redirect_stderr(stderr):
            result = main([
                "--base-url", "http://127.0.0.1:45678",
                "--model-id", "mock-qwen-embedding-q8@rev",
                "--api-model", "mock-qwen-embedding-q8",
            ])
        self.assertEqual(result, 1)
        self.assertIn("HTTP 400", stderr.getvalue())
        self.assertNotIn("Authorization", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
