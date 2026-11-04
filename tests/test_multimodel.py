import io
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from sqlalchemy import create_engine

from agentforge.credential_store import CredentialStore
from agentforge.af012_schema import Base
from agentforge.af012_runtime import DATA_ROOT
from agentforge.model_registry import ModelRegistry, parse_deepseek_models
from agentforge.providers.deepseek import DeepSeekClient, DeepSeekProvider, _read_sse
from agentforge.token_budget import TokenBudgetManager, TokenizerAdapter
from agentforge.conversation_store import ConversationStore


def discovery_payload():
    return {"object": "list", "data": [{
        "id": "deepseek-flash", "object": "model", "owned_by": "deepseek",
        "name": "Flash", "context_window": 65536, "max_output_tokens": 8192,
        "input_modalities": ["text"], "output_modalities": ["text"],
        "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
    }]}


class Response:
    def __init__(self, status=200, payload=None, body=None, headers=None):
        self.status = status
        self._body = body if body is not None else json.dumps(payload).encode()
        self.headers = headers or {}
    def read(self, limit=-1):
        value, self._body = self._body[:limit], self._body[limit:]
        return value
    def readline(self, limit=-1):
        stream = io.BytesIO(self._body)
        line = stream.readline(limit)
        self._body = stream.read()
        return line
    def getheader(self, name):
        return self.headers.get(name)


class Connection:
    def __init__(self, response):
        self.response, self.sock, self.request_args, self.closed = response, None, None, False
    def request(self, method, path, body=None, headers=None):
        self.request_args = (method, path, body, headers)
    def getresponse(self):
        return self.response
    def close(self):
        self.closed = True


class MultiModelTests(unittest.TestCase):
    def test_registry_parses_discovered_models_and_keeps_unknown_capabilities_unknown(self):
        models = parse_deepseek_models(discovery_payload())
        self.assertEqual("deepseek-flash", models[0].model_id)
        self.assertEqual(65536, models[0].context_window)
        self.assertEqual(("low", "high", "max"), models[0].supported_efforts)
        self.assertTrue(models[0].supports_thinking)
        self.assertIsNone(models[0].supports_streaming)
        self.assertIsNone(models[0].supports_tool_calling)

    def test_registry_rejects_bad_or_duplicate_entries_and_empty_list_is_valid(self):
        self.assertEqual((), parse_deepseek_models({"object": "list", "data": []}))
        for payload in ({}, {"object": "list", "data": [{"id": "x"}, {"id": "x"}]},
                        {"object": "list", "data": [{"id": "x", "context_window": True}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_deepseek_models(payload)

    def test_registry_cache_is_marked_stale_and_local_default_is_present(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / "models.json"
            created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            registry = ModelRegistry(path, now=lambda: created_at)
            registry.refresh_deepseek(Mock(list_models=Mock(return_value=discovery_payload())))
            loaded = ModelRegistry(path, now=lambda: created_at + timedelta(seconds=2), max_age_seconds=0)
            self.assertEqual("stale", loaded.cache_status)
            self.assertEqual("llama-server", loaded.get("qwen-local").provider_id)
            self.assertEqual("deepseek-flash", loaded.get("deepseek-flash").model_id)

    def test_token_budget_is_per_model_bounded_and_marks_fallback_estimate(self):
        registry = ModelRegistry(Path("missing-model-cache"))
        qwen = registry.get("qwen-local")
        deepseek = parse_deepseek_models(discovery_payload())[0]
        manager = TokenBudgetManager(minimum_safety_tokens=100)
        local = manager.budget(qwen, requested_output_tokens=300)
        cloud = manager.budget(deepseek, requested_output_tokens=300)
        self.assertEqual(4096 - 512 - 410, local.input_budget)
        self.assertGreater(cloud.input_budget, local.input_budget)
        self.assertEqual("estimated", manager.measure_messages([
            {"role": "system", "content": "中文 source code"}]).method)
        exact = TokenBudgetManager(TokenizerAdapter(lambda text: len(text.split())))
        self.assertEqual("exact", exact.measure_messages([
            {"role": "user", "content": "hello world"}]).method)

    def test_token_budget_counts_nested_tool_protocol_and_schema(self):
        from agentforge.providers.deepseek_agent import _TOOLS
        manager = TokenBudgetManager(TokenizerAdapter(lambda text: len(text.encode("utf-8"))))
        messages = [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function", "function": {
                "name": "search", "arguments": '{"query":"alpha"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "source [E1]"}]
        message_only = manager.measure_request(messages)
        with_tools = manager.measure_request(messages, tools=_TOOLS)
        self.assertGreater(with_tools.count, message_only.count)
        self.assertEqual("exact", with_tools.method)

    def test_credential_store_prefers_environment_and_never_returns_secret_in_status(self):
        with tempfile.TemporaryDirectory(dir=DATA_ROOT) as directory:
            store = CredentialStore(Path(directory) / "cred.dpapi")
            self.assertEqual({"configured": True, "source": "environment", "stored_credential_present": False},
                             store.status({"DEEPSEEK_API_KEY": "synthetic-test-secret"}))
            with patch("agentforge.credential_store._dpapi", side_effect=lambda value, protect: value):
                store.set("synthetic-test-secret")
                self.assertEqual("synthetic-test-secret", store.get())
                self.assertTrue(store.remove())
                self.assertIsNone(store.get())
            self.assertFalse(store.status({})["configured"])

    def test_deepseek_request_is_fixed_host_uses_bearer_and_parses_usage_without_reasoning_leak(self):
        payload = {"id": "request-1", "choices": [{"message": {
            "content": "The return is cached [E1].", "reasoning_content": "private reasoning", "tool_calls": []},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 21, "completion_tokens": 8,
            "total_tokens": 29, "completion_tokens_details": {"reasoning_tokens": 3}}}
        connection = Connection(Response(payload=payload, headers={"x-request-id": "request-1"}))
        with patch("agentforge.providers.deepseek.http.client.HTTPSConnection", return_value=connection) as factory:
            client = DeepSeekClient("synthetic-key")
            completion = client.chat([{"role": "user", "content": "q"}], model="deepseek-flash",
                                     max_output_tokens=128, thinking=True, effort="high")
        factory.assert_called_once()
        args, kwargs = factory.call_args
        self.assertEqual("api.deepseek.com", args[0])
        self.assertEqual("Bearer synthetic-key", connection.request_args[3]["Authorization"])
        body = json.loads(connection.request_args[2])
        self.assertEqual({"type": "enabled"}, body["thinking"])
        self.assertEqual("high", body["reasoning_effort"])
        self.assertEqual("The return is cached [E1].", completion.content)
        self.assertEqual("private reasoning", completion.reasoning_content)
        self.assertEqual(21, client.request_stats()["last_usage"]["prompt_tokens"])
        self.assertEqual(3, client.request_stats()["last_usage"]["reasoning_tokens"])
        self.assertEqual(1, client.request_stats()["succeeded"])
        self.assertTrue(connection.closed)

    def test_stream_usage_preserves_reasoning_tokens_after_normalization(self):
        from agentforge.providers.deepseek import _usage_metadata
        usage = {"completion_tokens": 40, "completion_tokens_details": {"reasoning_tokens": 31}}
        normalized = _usage_metadata(usage)
        self.assertEqual({"completion_tokens": 40, "reasoning_tokens": 31}, normalized)
        self.assertEqual(normalized, _usage_metadata(normalized))

    def test_unknown_thinking_capability_omits_parameter_and_streaming_requires_confirmation(self):
        from agentforge.providers.deepseek import DeepSeekProvider
        connection = Connection(Response(payload={"choices": [{"message": {
            "content": "Supported [E1]", "tool_calls": []}, "finish_reason": "stop"}]}))
        client = DeepSeekClient("synthetic-key", connection_factory=lambda *a, **kw: connection)
        client.chat([{"role": "user", "content": "q"}], model="deepseek-flash", max_output_tokens=32,
                    thinking=None)
        body = json.loads(connection.request_args[2])
        self.assertNotIn("thinking", body)
        unknown = parse_deepseek_models(discovery_payload())[0]
        with self.assertRaisesRegex(ValueError, "Streaming capability"):
            DeepSeekProvider(DeepSeekClient("synthetic-key"), unknown.model_id,
                             descriptor=unknown, stream=True)

    def test_reasoning_only_response_never_becomes_user_answer(self):
        from agentforge.context import build_context
        from agentforge.models import SearchResult
        from agentforge.providers.deepseek import DeepSeekCompletion, DeepSeekProvider
        descriptor = parse_deepseek_models(discovery_payload())[0]
        client = DeepSeekClient("synthetic-key")
        client.chat = Mock(return_value=DeepSeekCompletion("", "private reasoning", "stop", (), {}, None))
        provider = DeepSeekProvider(client, descriptor.model_id, descriptor=descriptor)
        context = build_context("What is returned?", [SearchResult(
            "repo", "src/value.py", 1, 1, "return 42", "hash-value", 1.0)])
        with self.assertRaisesRegex(Exception, "empty final content"):
            provider.answer(context)

    def test_deepseek_error_classification_is_safe_and_never_retries(self):
        connection = Connection(Response(status=401, body=b'{"error":{"message":"redacted"}}'))
        client = DeepSeekClient("synthetic-key", connection_factory=lambda *a, **kw: connection)
        with self.assertRaisesRegex(Exception, "configured API key"):
            client.chat([{"role": "user", "content": "q"}], model="deepseek-flash", max_output_tokens=64)
        stats = client.request_stats()
        self.assertEqual((1, 1, 0, 1, 0), tuple(stats[key] for key in ("attempted", "sent", "succeeded", "failed", "retries")))
        self.assertEqual("authentication", stats["last_error_code"])
        self.assertNotIn("redacted", json.dumps(stats))

    def test_deepseek_http_error_classes_and_invalid_json_are_bounded_without_retry(self):
        from agentforge.errors import ProviderError
        expected = {400: "bad_request", 402: "billing", 429: "rate_limited", 503: "server_error"}
        for status, code in expected.items():
            connection = Connection(Response(status=status, body=b'{"error":{"message":"secret body"}}'))
            client = DeepSeekClient("synthetic-key", connection_factory=lambda *a, _c=connection, **kw: _c)
            with self.subTest(status=status), self.assertRaises(ProviderError):
                client.chat([{"role": "user", "content": "q"}], model="deepseek-flash",
                            max_output_tokens=64)
            stats = client.request_stats()
            self.assertEqual((1, 1, 0, 1, 0), tuple(stats[key] for key in (
                "attempted", "sent", "succeeded", "failed", "retries")))
            self.assertEqual(code, stats["last_error_code"])
            self.assertNotIn("secret body", json.dumps(stats))
            self.assertTrue(connection.closed)

        bad_json = Connection(Response(body=b"not-json"))
        client = DeepSeekClient("synthetic-key", connection_factory=lambda *a, **kw: bad_json)
        with self.assertRaisesRegex(ProviderError, "invalid JSON"):
            client.chat([{"role": "user", "content": "q"}], model="deepseek-flash",
                        max_output_tokens=64)
        self.assertEqual("invalid_json", client.request_stats()["last_error_code"])
        self.assertTrue(bad_json.closed)

    def test_deepseek_stream_cancel_closes_owned_connection(self):
        body = b'data: {"choices":[{"delta":{"content":"draft"},"finish_reason":null}]}\n\n'
        connection = Connection(Response(body=body))
        client = DeepSeekClient("synthetic-key", connection_factory=lambda *a, **kw: connection)
        def cancel(_piece):
            raise KeyboardInterrupt("synthetic user cancellation")
        with self.assertRaises(KeyboardInterrupt):
            client.chat([{"role": "user", "content": "q"}], model="deepseek-flash",
                        max_output_tokens=64, stream=True, on_content=cancel)
        stats = client.request_stats()
        self.assertTrue(connection.closed)
        self.assertEqual((1, 1, 0, 1, 0), tuple(stats[key] for key in (
            "attempted", "sent", "succeeded", "failed", "retries")))

    def test_deepseek_stream_keeps_reasoning_separate_and_requires_done(self):
        stream = (b'data: {"id":"x","choices":[{"delta":{"reasoning_content":"hidden"},"finish_reason":null}]}\n\n'
                  b'data: {"id":"x","choices":[{"delta":{"content":"final"},"finish_reason":"stop"}]}\n\n'
                  b'data: {"id":"x","choices":[],"usage":{"prompt_tokens":4,"completion_tokens":1,"total_tokens":5}}\n\n'
                  b'data: [DONE]\n\n')
        result = _read_sse(Response(body=stream))
        self.assertEqual("final", result["choices"][0]["message"]["content"])
        self.assertEqual("hidden", result["choices"][0]["message"]["reasoning_content"])
        self.assertEqual(4, result["usage"]["prompt_tokens"])
        with self.assertRaises(Exception):
            _read_sse(Response(body=b'data: {"choices":[]}\n\n'))

    def test_deepseek_stream_deadline_is_checked_before_each_read(self):
        calls = []
        def prepare_read():
            calls.append(True)
            if len(calls) == 2:
                raise TimeoutError("synthetic deadline")
        body = (b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
                b'data: [DONE]\n\n')
        with self.assertRaises(TimeoutError):
            _read_sse(Response(body=body), prepare_read=prepare_read)
        self.assertEqual(2, len(calls))

    def test_conversation_model_binding_defaults_persists_and_requires_cloud_consent(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        try:
            store = ConversationStore(engine)
            first = store.create_conversation("repo-one", "local by default")
            self.assertEqual(("llama-server", "qwen3.5-4b"),
                             (first["provider_id"], first["model_id"]))
            with self.assertRaisesRegex(ValueError, "explicit user consent"):
                store.update_model_binding(first["id"], "repo-one", provider_id="deepseek",
                    model_id="deepseek-flash")
            consent = datetime.now(timezone.utc)
            selected = store.update_model_binding(first["id"], "repo-one",
                provider_id="deepseek", model_id="deepseek-flash",
                thinking_enabled=True, reasoning_effort="high", cloud_consent_at=consent)
            self.assertEqual(consent, selected["cloud_consent_at"])
            changed = store.update_model_binding(first["id"], "repo-one",
                provider_id="deepseek", model_id="deepseek-flash",
                thinking_enabled=False)
            self.assertEqual(consent, changed["cloud_consent_at"])
            self.assertFalse(changed["thinking_enabled"])
            store.append_turn(first["id"], "repo-one", "q", "a", model_name="deepseek-flash",
                provider_id="deepseek", model_id="deepseek-flash",
                usage={"prompt_tokens": 11, "completion_tokens": 5, "reasoning_tokens": 2},
                latency_ms=123, request_id="request-1")
            assistant = store.get_messages(first["id"], "repo-one")[1]
            self.assertEqual("deepseek", assistant["metadata"]["provider_id"])
            self.assertEqual(11, assistant["metadata"]["usage"]["prompt_tokens"])
            self.assertEqual(123, assistant["metadata"]["latency_ms"])
            second = store.create_conversation("repo-one", "independent")
            self.assertEqual(("llama-server", "qwen3.5-4b"),
                             (second["provider_id"], second["model_id"]))
            self.assertEqual("deepseek-flash", store.get_conversation(first["id"], "repo-one")["model_id"])
            revoked = store.revoke_cloud_consent(first["id"], "repo-one")
            self.assertIsNone(revoked["cloud_consent_at"])
            with self.assertRaisesRegex(ValueError, "explicit user consent"):
                store.update_model_binding(first["id"], "repo-one", provider_id="deepseek",
                    model_id="deepseek-flash", thinking_enabled=True, reasoning_effort="high")
        finally:
            engine.dispose()

    def test_tool_call_protocol_preserves_reasoning_only_with_tool_state(self):
        from agentforge.providers.deepseek import _validate_messages
        messages = _validate_messages([
            {"role": "assistant", "content": None, "reasoning_content": "private internal state",
             "tool_calls": [{"id": "call-1", "type": "function", "function": {
                 "name": "search", "arguments": '{"query":"sample"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"status":"ok"}'},
        ])
        self.assertEqual("private internal state", messages[0]["reasoning_content"])
        self.assertEqual("call-1", messages[1]["tool_call_id"])
        with self.assertRaises(ValueError):
            _validate_messages([{"role": "assistant", "content": "ordinary answer",
                                 "reasoning_content": "must not leak"}])

    def test_tool_call_protocol_rejects_orphan_missing_and_incomplete_results(self):
        from agentforge.providers.deepseek import _validate_messages
        call = {"id": "call-1", "type": "function", "function": {
            "name": "search", "arguments": '{"query":"sample"}'}}
        with self.assertRaisesRegex(ValueError, "matching tool-call ID"):
            _validate_messages([{"role": "tool", "content": "orphan"}])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            _validate_messages([{"role": "assistant", "content": None, "tool_calls": [call]}])
        with self.assertRaisesRegex(ValueError, "tool response reference is invalid"):
            _validate_messages([{"role": "assistant", "content": None, "tool_calls": [call]},
                                {"role": "tool", "tool_call_id": "other", "content": "x"}])
        with self.assertRaisesRegex(ValueError, "before another chat message"):
            _validate_messages([{"role": "assistant", "content": None, "tool_calls": [call]},
                                {"role": "user", "content": "continue"}])

    def test_agent_token_budget_rejects_before_model_request(self):
        from agentforge.providers.deepseek_agent import DeepSeekAgentPlanner
        from agentforge.errors import ProviderError
        descriptor = parse_deepseek_models({"object": "list", "data": [{
            **discovery_payload()["data"][0],
            "context_window": 2048,
            "max_output_tokens": 1024,
            "api_capabilities": {"chat_completions": {"tool_calling": True}},
        }]})[0]
        client = Mock()
        planner = DeepSeekAgentPlanner(client, descriptor.model_id, descriptor, max_output_tokens=256)
        with self.assertRaisesRegex(ProviderError, "no request was sent"):
            planner.next_call("x" * 100_000, ())
        client.chat.assert_not_called()
        self.assertTrue(planner.last_token_budget_diagnostics["tool_schema_included"])

    def test_deepseek_agent_dispatches_only_existing_read_only_tools_and_keeps_protocol_state_ephemeral(self):
        from types import SimpleNamespace
        from agentforge.agent_loop import ToolAuditRecord
        from agentforge.providers.deepseek_agent import DeepSeekAgentPlanner

        payload = discovery_payload()
        payload["data"][0]["api_capabilities"] = {"chat_completions": {
            "streaming": True, "tool_calling": True}}
        descriptor = parse_deepseek_models(payload)[0]
        first = SimpleNamespace(content="", reasoning_content="private", finish_reason="tool_calls", tool_calls=(
            {"id": "call-1", "name": "search", "arguments": {"query": "cache_lookup"},
             "raw_arguments": '{"query":"cache_lookup"}'},),)
        final = SimpleNamespace(content="The cache lookup reads an existing entry [E1].",
                                reasoning_content=None, finish_reason="stop", tool_calls=())
        client = Mock(chat=Mock(side_effect=(first, final)))
        planner = DeepSeekAgentPlanner(client, descriptor.model_id, descriptor)
        call = planner.next_call("Explain cache_lookup", ())
        self.assertEqual(("search", {"query": "cache_lookup"}), (call.name, call.arguments))
        record = ToolAuditRecord(1, "search", "deepseek", call.arguments, "ok", 0.01,
                                 result={"results": [{"relative_path": "src/cache.py"}]})
        self.assertIsNone(planner.next_call("Explain cache_lookup", (record,)))
        self.assertEqual("The cache lookup reads an existing entry [E1].", planner.final_answer)
        followup = client.chat.call_args.args[0]
        self.assertEqual("private", followup[-2]["reasoning_content"])
        self.assertEqual("tool", followup[-1]["role"])
        self.assertIn("cache.py", followup[-1]["content"])
        self.assertEqual(2, client.chat.call_count)

    def test_deepseek_agent_refuses_unknown_tool_capability_or_tool_name_before_dispatch(self):
        from types import SimpleNamespace
        from agentforge.errors import ProviderError
        from agentforge.providers.deepseek_agent import DeepSeekAgentPlanner

        unknown = parse_deepseek_models(discovery_payload())[0]
        with self.assertRaisesRegex(ValueError, "no confirmed tool-calling"):
            DeepSeekAgentPlanner(Mock(), unknown.model_id, unknown)
        payload = discovery_payload()
        payload["data"][0]["api_capabilities"] = {"chat_completions": {"tool_calling": True}}
        descriptor = parse_deepseek_models(payload)[0]
        client = Mock(chat=Mock(return_value=SimpleNamespace(content="", reasoning_content=None,
            finish_reason="tool_calls",
            tool_calls=({"id": "call-1", "name": "shell", "arguments": {}, "raw_arguments": "{}"},))))
        planner = DeepSeekAgentPlanner(client, descriptor.model_id, descriptor)
        with self.assertRaisesRegex(ProviderError, "outside the read-only"):
            planner.next_call("inspect", ())
        self.assertEqual(1, client.chat.call_count)


class MultiModelBoundaryTests(unittest.TestCase):
    """Boundary tests for metadata, credential, and token-budget contracts."""

    def test_model_registry_rejects_malformed_metadata_fields(self):
        invalid_entries = [
            None,
            {"id": ""}, {"id": " name"}, {"id": "x\n"}, {"id": "x" * 257},
            {"id": "x", "name": 3}, {"id": "x", "name": "n" * 257},
            {"id": "x", "owned_by": []}, {"id": "x", "owned_by": "o" * 129},
            {"id": "x", "effort": []}, {"id": "x", "effort": {"supported_levels": "high"}},
            {"id": "x", "effort": {"supported_levels": ["medium"]}},
            {"id": "x", "effort": {"supported_levels": ["low"], "default_level": "high"}},
            {"id": "x", "input_modalities": "text"}, {"id": "x", "input_modalities": [1]},
            {"id": "x", "output_modalities": [None]}, {"id": "x", "api_capabilities": []},
            {"id": "x", "api_capabilities": {"chat_completions": []}},
            {"id": "x", "api_capabilities": {"chat_completions": {"streaming": 1}}},
            {"id": "x", "api_capabilities": {"chat_completions": {"tool_calling": "yes"}}},
            {"id": "x", "tokenizer_id": " "}, {"id": "x", "tokenizer_id": "t" * 257},
            {"id": "x", "context_window": 0}, {"id": "x", "max_output_tokens": True},
        ]
        for entry in invalid_entries:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                parse_deepseek_models({"object": "list", "data": [entry]})
        for payload in (None, {"object": "other", "data": []}, {"object": "list", "data": None}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_deepseek_models(payload)
        with self.assertRaisesRegex(ValueError, "model count"):
            parse_deepseek_models({"object": "list", "data": [{"id": f"m-{i}"} for i in range(201)]})

    def test_model_registry_cache_rejects_bad_files_and_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / "models.json"
            for text in ("not-json", json.dumps({"version": 2, "models": [], "saved_at": "2026-01-01T00:00:00+00:00"}),
                         json.dumps({"version": 1, "models": [None], "saved_at": "2026-01-01T00:00:00+00:00"}),
                         json.dumps({"version": 1, "models": [{"provider_id": "offline"}], "saved_at": "2026-01-01T00:00:00+00:00"}),
                         json.dumps({"version": 1, "models": [], "saved_at": "bad-time"})):
                path.write_text(text, encoding="utf-8")
                registry = ModelRegistry(path)
                self.assertEqual("invalid", registry.cache_status)
                self.assertEqual((), registry._remote)
            path.write_bytes(b"x" * 1_048_577)
            self.assertEqual("invalid", ModelRegistry(path).cache_status)
            descriptor = parse_deepseek_models(discovery_payload())[0]
            from dataclasses import asdict
            path.write_text(json.dumps({"version": 1, "saved_at": "2026-01-01T00:00:00+00:00",
                                        "models": [asdict(descriptor)] * 201}), encoding="utf-8")
            self.assertEqual("invalid", ModelRegistry(path).cache_status)
            path.write_text(json.dumps({"version": 1, "saved_at": "2026-01-01T00:00:00+00:00",
                                        "models": [asdict(descriptor), asdict(descriptor)]}), encoding="utf-8")
            self.assertEqual("invalid", ModelRegistry(path).cache_status)
            path.unlink()
            self.assertEqual("missing", ModelRegistry(path).cache_status)

    def test_model_registry_rejects_invalid_cached_descriptor_capabilities(self):
        from dataclasses import asdict
        from agentforge.model_registry import LOCAL_QWEN, _validate_cached_descriptor
        base = parse_deepseek_models(discovery_payload())[0]
        invalid = [
            base.__class__(**{**asdict(base), "provider_id": "offline"}),
            base.__class__(**{**asdict(base), "model_type": "local"}),
            base.__class__(**{**asdict(base), "model_id": " "}),
            base.__class__(**{**asdict(base), "display_name": " "}),
            base.__class__(**{**asdict(base), "context_window": False}),
            base.__class__(**{**asdict(base), "supported_efforts": ("invalid",)}),
            base.__class__(**{**asdict(base), "default_effort": "medium"}),
            base.__class__(**{**asdict(base), "supports_streaming": 1}),
            base.__class__(**{**asdict(base), "availability": "CONNECTED"}),
        ]
        for descriptor in invalid:
            with self.subTest(descriptor=descriptor), self.assertRaises(ValueError):
                _validate_cached_descriptor(descriptor)
        with self.assertRaises(ValueError):
            ModelRegistry(Path("unused"), max_age_seconds=True)
        with self.assertRaises(ValueError):
            ModelRegistry(Path("unused")).get("retired-model")
        self.assertEqual("qwen-local", LOCAL_QWEN.key)

    def test_model_registry_refresh_preserves_cache_on_client_failure_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory)
            path = root / "models.json"
            stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
            registry = ModelRegistry(path, now=lambda: stamp)
            registry.refresh_deepseek(Mock(list_models=Mock(return_value=discovery_payload())))
            before = path.read_bytes()
            with self.assertRaises(RuntimeError):
                registry.refresh_deepseek(Mock(list_models=Mock(side_effect=RuntimeError("offline"))))
            self.assertEqual(before, path.read_bytes())
            self.assertEqual("deepseek-flash", registry.get("deepseek-flash").model_id)
            with patch.object(Path, "is_symlink", autospec=True,
                              side_effect=lambda candidate: candidate == path):
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    registry.refresh_deepseek(Mock(list_models=Mock(return_value=discovery_payload())))
            self.assertEqual("deepseek-flash", registry.get("deepseek-flash").model_id)

    def test_tokenizer_validates_input_and_counts_protocol_fields(self):
        adapter = TokenizerAdapter()
        with self.assertRaises(ValueError):
            adapter.count_text(None)
        with self.assertRaises(ValueError):
            adapter.count_messages([{"role": "user", "content": 1}])
        with self.assertRaises(ValueError):
            adapter.count_messages(["invalid"])
        exact_bad = TokenizerAdapter(lambda _text: True)
        with self.assertRaises(ValueError):
            exact_bad.count_text("x")
        exact_negative = TokenizerAdapter(lambda _text: -1)
        with self.assertRaises(ValueError):
            exact_negative.count_text("x")
        exact = TokenizerAdapter(lambda text: len(text))
        counted = exact.count_messages([{"role": "assistant", "content": None,
                                        "reasoning_content": "internal", "tool_calls": []}])
        self.assertEqual("exact", counted.method)
        self.assertGreater(counted.count, len("assistant"))

    def test_token_budget_rejects_invalid_configuration_and_handles_output_cap(self):
        from dataclasses import replace
        qwen = ModelRegistry(Path("nonexistent-cache")).get("qwen-local")
        for kwargs in ({"safety_ratio": -0.01}, {"safety_ratio": 0.5},
                       {"minimum_safety_tokens": -1}, {"minimum_safety_tokens": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                TokenBudgetManager(**kwargs)
        manager = TokenBudgetManager(minimum_safety_tokens=0, safety_ratio=0)
        for value in (0, True, -1):
            with self.subTest(output=value), self.assertRaises(ValueError):
                manager.budget(qwen, requested_output_tokens=value)
        no_metadata = replace(qwen, context_window=None)
        with self.assertRaisesRegex(ValueError, "no verified context"):
            manager.budget(no_metadata)
        too_small = replace(qwen, context_window=512, max_output_tokens=256)
        with self.assertRaisesRegex(ValueError, "too small"):
            TokenBudgetManager(minimum_safety_tokens=300, safety_ratio=0).budget(too_small)
        capped = manager.budget(qwen, requested_output_tokens=10_000)
        self.assertEqual(qwen.max_output_tokens, capped.output_reserve)
        self.assertGreater(manager.require_fit([{"role": "user", "content": "short"}], capped).count, 0)
        with self.assertRaisesRegex(ValueError, "over the selected model"):
            manager.require_fit([{"role": "user", "content": "x" * 5000}], capped)

    def test_credential_validation_env_precedence_and_interactive_input(self):
        from agentforge.credential_store import _valid_key, _dpapi
        for invalid in ("", " padded ", "tab\tkey", "line\nkey", "☃", "x" * 4097):
            with self.subTest(value=repr(invalid)), self.assertRaises(ValueError):
                _valid_key(invalid)
        with patch("agentforge.credential_store.os.name", "posix"):
            with self.assertRaisesRegex(RuntimeError, "only on Windows"):
                _dpapi(b"secret", protect=True)
        with tempfile.TemporaryDirectory(dir=DATA_ROOT) as directory:
            store = CredentialStore(Path(directory) / "cred.dpapi")
            with patch("agentforge.credential_store._dpapi", side_effect=lambda value, protect: value):
                store.set_interactive(input_fn=lambda _prompt: "synthetic-hidden-key")
                self.assertEqual(("synthetic-hidden-key", "dpapi"), store.resolve({}))
                self.assertEqual(("env-key", "environment"), store.resolve({"DEEPSEEK_API_KEY": "env-key"}))
                self.assertEqual("environment", store.status({"DEEPSEEK_API_KEY": "env-key"})["source"])
                self.assertTrue(store.status({})["stored_credential_present"])
                self.assertTrue(store.remove())
                self.assertFalse(store.remove())
            with self.assertRaises(ValueError):
                store.status({"DEEPSEEK_API_KEY": "bad key"})

    def test_dpapi_protect_unprotect_and_failure_are_isolated_with_fake_windows_api(self):
        import ctypes
        import agentforge.credential_store as module

        class Function:
            def __init__(self, callback):
                self.callback = callback
                self.argtypes = None
                self.restype = None
            def __call__(self, *args):
                return self.callback(*args)

        keepalive = []
        def copy_blob(source_ptr, _description, _entropy, _reserved, _prompt, _flags, output_ptr):
            source = ctypes.cast(source_ptr, ctypes.POINTER(module._DataBlob)).contents
            output = ctypes.cast(output_ptr, ctypes.POINTER(module._DataBlob)).contents
            value = ctypes.string_at(source.pbData, source.cbData)
            buffer = ctypes.create_string_buffer(value)
            keepalive.append(buffer)
            output.cbData = len(value)
            output.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
            return 1

        crypt32 = type("Crypt32", (), {})()
        crypt32.CryptProtectData = Function(copy_blob)
        crypt32.CryptUnprotectData = Function(copy_blob)
        kernel32 = type("Kernel32", (), {})()
        kernel32.LocalFree = Function(lambda _pointer: None)
        def load_library(name, **_kwargs):
            return crypt32 if name == "crypt32" else kernel32

        with patch.object(module.os, "name", "nt"), patch.object(module.ctypes, "WinDLL", side_effect=load_library):
            self.assertEqual(b"synthetic-key", module._dpapi(b"synthetic-key", protect=True))
            self.assertEqual(b"synthetic-key", module._dpapi(b"synthetic-key", protect=False))
            with self.assertRaisesRegex(ValueError, "payload is empty"):
                module._dpapi(b"", protect=True)
            with self.assertRaisesRegex(ValueError, "payload is empty"):
                module._dpapi(b"x" * 16_385, protect=False)
            crypt32.CryptProtectData = Function(lambda *_args: 0)
            with self.assertRaisesRegex(RuntimeError, "could not protect"):
                module._dpapi(b"synthetic-key", protect=True)

    def test_credential_store_rejects_bad_paths_payloads_and_never_echoes_secret(self):
        from agentforge.credential_store import _dpapi
        with tempfile.TemporaryDirectory(dir=DATA_ROOT) as directory:
            root = Path(directory)
            outside = DATA_ROOT.parent / (root.name + "-outside")
            with self.assertRaisesRegex(ValueError, "private data directory"):
                CredentialStore(outside / "secret.dpapi")
            path = root / "cred.dpapi"
            store = CredentialStore(path)
            with self.assertRaisesRegex(ValueError, "cannot be read"):
                path.write_bytes(b"")
                store.get()
            path.write_bytes(b"x" * 16_385)
            with self.assertRaisesRegex(ValueError, "cannot be read"):
                store.get()
            path.write_bytes(b"cipher")
            with patch("agentforge.credential_store._dpapi", side_effect=UnicodeDecodeError("ascii", b"\xff", 0, 1, "bad")):
                with self.assertRaisesRegex(ValueError, "cannot be read") as caught:
                    store.get()
                self.assertNotIn("cipher", str(caught.exception))
            with patch("agentforge.credential_store._dpapi", side_effect=RuntimeError("dpapi unavailable")):
                with self.assertRaises(RuntimeError):
                    store.set("synthetic-key")
            self.assertFalse(any(root.glob(".deepseek-*.tmp")))
            path.unlink()
            path.write_bytes(b"cipher")
            store = CredentialStore(path)
            with patch.object(Path, "is_symlink", autospec=True,
                              side_effect=lambda candidate: candidate == path):
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    CredentialStore(path)
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    store.get()
                with self.assertRaisesRegex(ValueError, "symbolic link"):
                    store.remove()
            with patch("agentforge.credential_store._dpapi", return_value=b" bad "):
                path.write_bytes(b"cipher")
                with self.assertRaises(ValueError):
                    store.get()

    def test_model_registry_qwen_identity_health_and_error_boundaries_are_offline(self):
        from agentforge.model_registry import check_qwen_identity
        class FakeHTTPConnection:
            replies = []
            def __init__(self, host, port=None, timeout=0, **_kwargs):
                self.host, self.port, self.timeout, self.closed, self.paths = host, port, timeout, False, []
                self.reply = self.replies.pop(0)
            def request(self, method, path, headers=None):
                self.paths.append((method, path, headers))
            def getresponse(self):
                if isinstance(self.reply, Exception):
                    raise self.reply
                return self.reply
            def close(self): self.closed = True
        def response(status, body):
            return Response(status=status, body=body)
        FakeHTTPConnection.replies = [response(200, b'{"status":"ok"}'),
                                      response(200, b'{"data":[{"id":"qwen3.5-4b"}]}')]
        with patch("http.client.HTTPConnection", FakeHTTPConnection):
            self.assertEqual({"health": "ready", "model_id": "qwen3.5-4b"},
                             check_qwen_identity("http://127.0.0.1:56572/v1/chat/completions", "local-key", "qwen3.5-4b"))
        for first, second, message in (
            (response(503, b"{}"), response(200, b'{"data":[{"id":"qwen3.5-4b"}]}'), "health check failed"),
            (response(200, b"bad-json"), response(200, b'{"data":[{"id":"qwen3.5-4b"}]}'), "health response was invalid"),
            (response(200, b'{"status":"loading"}'), response(200, b'{"data":[{"id":"qwen3.5-4b"}]}'), "not ready"),
            (response(200, b'{"status":"ok"}'), response(404, b"{}"), "identity could not be confirmed"),
            (response(200, b'{"status":"ok"}'), response(200, b"bad-json"), "identity response was invalid"),
            (response(200, b'{"status":"ok"}'), response(200, b'{"data":[]}'), "does not expose"),
        ):
            FakeHTTPConnection.replies = [first, second]
            with patch("http.client.HTTPConnection", FakeHTTPConnection), self.assertRaisesRegex(ValueError, message):
                check_qwen_identity("http://127.0.0.1:56572/v1/chat/completions", "local-key", "qwen3.5-4b")
        FakeHTTPConnection.replies = [OSError("private socket detail")]
        with patch("http.client.HTTPConnection", FakeHTTPConnection), self.assertRaisesRegex(ValueError, "request failed") as caught:
            check_qwen_identity("http://127.0.0.1:56572/v1/chat/completions", "local-key", "qwen3.5-4b")
        self.assertNotIn("private socket detail", str(caught.exception))


class DeepSeekProtocolBoundaryTests(unittest.TestCase):
    def _completion_payload(self, **message):
        base = {"content": "answer", "tool_calls": []}
        base.update(message)
        return {"id": "req-1", "choices": [{"message": base, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}

    def _client_for_response(self, response):
        connection = Connection(response)
        return DeepSeekClient("synthetic-key", connection_factory=lambda *_args, **_kwargs: connection), connection

    def test_client_rejects_bad_key_timeouts_model_flags_and_message_shapes_before_sending(self):
        for key in (None, "", " spaced ", "key\n", "x" * 4097):
            with self.subTest(key=repr(key)), self.assertRaises(ValueError):
                DeepSeekClient(key)
        for kwargs in ({"connect_timeout": 0}, {"connect_timeout": 121}, {"read_timeout": 4},
                       {"read_timeout": 121}, {"total_timeout": 9}, {"total_timeout": 301}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DeepSeekClient("key", **kwargs)
        client = DeepSeekClient("key")
        with self.assertRaises(ValueError):
            client.set_credential_source("unknown")
        for kwargs in (
            {"model": "", "max_output_tokens": 20},
            {"model": "x" * 257, "max_output_tokens": 20},
            {"model": "m", "max_output_tokens": 20, "thinking": "yes"},
            {"model": "m", "max_output_tokens": 20, "effort": "medium"},
            {"model": "m", "max_output_tokens": 20, "effort": "low"},
            {"model": "m", "max_output_tokens": 20, "thinking": True, "effort": "low", "on_content": 1},
        ):
            arguments = {"messages": [{"role": "user", "content": "x"}], **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client.chat(**arguments)
        self.assertEqual(0, client.request_stats()["attempted"])

    def test_client_model_discovery_is_separately_counted_and_failures_do_not_claim_chat(self):
        connection = Connection(Response(payload={"object": "list", "data": []}))
        client = DeepSeekClient("key", connection_factory=lambda *_a, **_k: connection)
        self.assertEqual([], client.list_models()["data"])
        stats = client.request_stats()
        self.assertEqual((1, 1, 0, 0), tuple(stats[key] for key in (
            "discovery_attempted", "discovery_succeeded", "attempted", "sent")))
        self.assertEqual(0, stats["discovery_failed"])
        connection = Connection(Response(status=503, body=b"{}"))
        client = DeepSeekClient("key", connection_factory=lambda *_a, **_k: connection)
        with self.assertRaises(Exception):
            client.list_models()
        stats = client.request_stats()
        self.assertEqual((1, 0, 1, 0), tuple(stats[key] for key in (
            "discovery_attempted", "discovery_succeeded", "discovery_failed", "attempted")))
        self.assertTrue(connection.closed)

    def test_request_rejects_redirects_oversized_invalid_utf8_and_nonobject_responses(self):
        from agentforge.errors import ProviderError
        cases = [
            (Response(status=302, body=b"{}"), "redirect"),
            (Response(body=b"\xff"), "invalid JSON"),
            (Response(body=b"[]"), "invalid response object"),
            (Response(body=b"x" * 2_000_001), "bounded response size"),
        ]
        for response, expected in cases:
            client, connection = self._client_for_response(response)
            with self.subTest(expected=expected), self.assertRaisesRegex(ProviderError, expected):
                client.chat([{"role": "user", "content": "q"}], model="m", max_output_tokens=20)
            stats = client.request_stats()
            self.assertEqual((1, 1, 0, 1, 0), tuple(stats[key] for key in (
                "attempted", "sent", "succeeded", "failed", "retries")))
            self.assertTrue(connection.closed)

    def test_request_timeout_network_and_write_failures_report_real_boundaries(self):
        import http.client
        from agentforge.errors import ProviderError
        class RaisingConnection(Connection):
            def __init__(self, response=None, *, request_error=None, response_error=None, sock=None):
                super().__init__(response)
                self.request_error, self.response_error, self.sock = request_error, response_error, sock
            def request(self, *args, **kwargs):
                super().request(*args, **kwargs)
                if self.request_error:
                    raise self.request_error
            def getresponse(self):
                if self.response_error:
                    raise self.response_error
                return self.response
        for connection, phase, code, counts in (
            (None, "connect", "network_error", (1, 0, 0, 1)),
            (RaisingConnection(request_error=OSError("write")), "request_write", "network_error", (1, 0, 0, 1)),
            (RaisingConnection(response_error=http.client.HTTPException("headers")), "response_headers", "network_error", (1, 1, 0, 1)),
            (RaisingConnection(response=Response(payload=self._completion_payload()), sock=type("Sock", (), {"settimeout": lambda *_: None})()), "response_body", "timeout", (1, 1, 0, 1)),
        ):
            if connection is None:
                factory = Mock(side_effect=OSError("connect"))
            else:
                factory = Mock(return_value=connection)
            client = DeepSeekClient("key", connection_factory=factory, total_timeout=10)
            if code == "timeout":
                with patch("agentforge.providers.deepseek.time.monotonic", side_effect=(0, 1, 0, 0, 12, 13)):
                    with self.assertRaisesRegex(ProviderError, "timed out"):
                        client.chat([{"role": "user", "content": "q"}], model="m", max_output_tokens=20)
            else:
                with self.assertRaises(ProviderError):
                    client.chat([{"role": "user", "content": "q"}], model="m", max_output_tokens=20)
            stats = client.request_stats()
            self.assertEqual(code, stats["last_error_code"])
            self.assertEqual(phase, stats["last_failure_phase"])
            self.assertEqual(counts, tuple(stats[key] for key in ("attempted", "sent", "succeeded", "failed")))
            if connection is not None:
                self.assertTrue(connection.closed)

    def test_request_deadline_stops_before_write_after_write_and_inside_stream(self):
        from agentforge.errors import ProviderError
        class Socket:
            def __init__(self): self.timeouts = []
            def settimeout(self, value): self.timeouts.append(value)
        class SocketConnection(Connection):
            def __init__(self, response):
                super().__init__(response)
                self.sock = Socket()
        cases = (
            ((0, 1, 12, 13), False, "connect", 0),
            ((0, 0, 1, 11, 12), False, "request_write", 1),
            ((0, 0, 1, 11, 12), True, "response_body", 1),
        )
        for times, streaming, phase, sent in cases:
            response = Response(body=b"data: [DONE]\n\n") if streaming else Response(payload=self._completion_payload())
            connection = SocketConnection(response)
            if streaming:
                connection.sock = None
            client = DeepSeekClient("key", connection_factory=lambda *_a, **_k: connection, total_timeout=10)
            with patch("agentforge.providers.deepseek.time.monotonic", side_effect=times):
                with self.assertRaisesRegex(ProviderError, "timed out"):
                    client.chat([{"role": "user", "content": "q"}], model="m", max_output_tokens=20,
                                stream=streaming)
            stats = client.request_stats()
            self.assertEqual(phase, stats["last_failure_phase"])
            self.assertEqual((1, sent, 0, 1), tuple(stats[key] for key in ("attempted", "sent", "succeeded", "failed")))
            self.assertTrue(connection.closed)
        response = Response(body=b"data: [DONE]\n\n")
        connection = SocketConnection(response)
        client = DeepSeekClient("key", connection_factory=lambda *_a, **_k: connection)
        client.chat([{"role": "user", "content": "q"}], model="m", max_output_tokens=20, stream=True)
        self.assertGreaterEqual(len(connection.sock.timeouts), 3)

    def test_discovery_timeout_and_provider_error_are_not_chat_requests(self):
        from agentforge.errors import ProviderError
        for exception, code in ((TimeoutError(), "timeout"), (OSError("connect"), "network_error"),
                                (ProviderError("synthetic"), "provider_error")):
            client = DeepSeekClient("key", connection_factory=Mock(side_effect=exception))
            with self.assertRaises(ProviderError):
                client.list_models()
            stats = client.request_stats()
            self.assertEqual((1, 0, 1, 0), tuple(stats[key] for key in (
                "discovery_attempted", "discovery_succeeded", "discovery_failed", "attempted")))
            self.assertEqual(0, stats["failed"])
        client = DeepSeekClient("key")
        client.set_credential_source("dpapi")
        self.assertEqual("dpapi", client.request_stats()["credential_source"])
        client.set_credential_source("environment")
        self.assertEqual("environment", client.request_stats()["credential_source"])

    def test_request_validation_before_send_and_invalid_completion_counts_as_failure(self):
        from agentforge.errors import ProviderError
        client = DeepSeekClient("key")
        with self.assertRaisesRegex(ProviderError, "bounded request size"):
            client._request("POST", "/chat/completions", body={"large": "x" * 2_000_001})
        self.assertEqual(0, client.request_stats()["attempted"])
        client, connection = self._client_for_response(Response(payload={"choices": []}))
        with self.assertRaisesRegex(ProviderError, "choices list"):
            client.chat([{"role": "user", "content": "q"}], model="m", max_output_tokens=20)
        stats = client.request_stats()
        self.assertEqual((1, 1, 0, 1), tuple(stats[key] for key in ("attempted", "sent", "succeeded", "failed")))
        self.assertEqual("invalid_completion", stats["last_error_code"])
        self.assertTrue(connection.closed)

    def test_optional_tools_request_id_fallback_provider_error_and_null_message_diagnostic(self):
        from agentforge.errors import ProviderError
        tool = {"type": "function", "function": {"name": "lookup", "description": "Find",
                "parameters": {"type": "object", "properties": {}}}}
        payload = self._completion_payload()
        connection = Connection(Response(payload=payload))
        client = DeepSeekClient("key", connection_factory=lambda *_a, **_k: connection)
        result = client.chat([{"role": "user", "content": "q"}], model="m", max_output_tokens=20,
                             tools=[tool])
        body = json.loads(connection.request_args[2])
        self.assertEqual([tool], body["tools"])
        self.assertEqual("req-1", result.request_id)
        self.assertEqual("req-1", client.request_stats()["last_request_id"])

        connection = Connection(Response(payload={"choices": [{"message": None}]}))
        client = DeepSeekClient("key", connection_factory=lambda *_a, **_k: connection)
        with self.assertRaisesRegex(ProviderError, "assistant message"):
            client.chat([{"role": "user", "content": "q"}], model="m", max_output_tokens=20)
        self.assertIsNone(client.request_stats()["last_response_diagnostic"]["tool_call_count"])

        class WriteErrorConnection(Connection):
            def request(self, *_args, **_kwargs):
                raise ProviderError("safe failure")
        connection = WriteErrorConnection(Response(payload=payload))
        client = DeepSeekClient("key", connection_factory=lambda *_a, **_k: connection)
        with self.assertRaisesRegex(ProviderError, "safe failure"):
            client.chat([{"role": "user", "content": "q"}], model="m", max_output_tokens=20)
        self.assertEqual("provider_error", client.request_stats()["last_error_code"])
        self.assertEqual("request_write", client.request_stats()["last_failure_phase"])
        self.assertEqual(1, client.request_stats()["failed"])
        self.assertTrue(connection.closed)

    def test_response_usage_headers_and_diagnostics_reject_malformed_values(self):
        from agentforge.providers.deepseek import _bounded_header, _response_diagnostic, _usage_metadata
        self.assertEqual({}, _usage_metadata(None))
        self.assertEqual({"prompt_cache_miss_tokens": 3}, _usage_metadata({"prompt_tokens": True,
                                              "completion_tokens": -1, "total_tokens": 1.5,
                                              "completion_tokens_details": {"reasoning_tokens": False},
                                              "prompt_cache_hit_tokens": -1,
                                              "prompt_cache_miss_tokens": 3}))
        self.assertEqual({"prompt_cache_miss_tokens": 3}, _usage_metadata({"prompt_cache_miss_tokens": 3}))
        self.assertEqual({"root_type": "dict", "choice_count": None, "content_chars": None,
                          "reasoning_chars": None, "tool_call_count": 0}, _response_diagnostic({}))
        self.assertEqual({"root_type": "dict", "choice_count": 1, "content_chars": None,
                          "reasoning_chars": None, "tool_call_count": None},
                         _response_diagnostic({"choices": [{"message": None}]}))
        self.assertIsNone(_bounded_header(Response(headers={"x": "bad\nheader"}), "x"))
        self.assertEqual("a" * 256, _bounded_header(Response(headers={"x": "a" * 300}), "x"))
        self.assertIsNone(_bounded_header(Response(), "missing"))

    def test_message_protocol_rejects_invalid_roles_fields_and_tool_state(self):
        from agentforge.providers.deepseek import _validate_messages
        valid_call = {"id": "call-1", "type": "function", "function": {
            "name": "search", "arguments": '{"query":"x"}'}}
        cases = [
            ([], "non-empty bounded list"), ([{"role": "user", "content": "x"}] * 257, "non-empty bounded list"),
            ([None], "role is invalid"), ([{"role": "unknown", "content": "x"}], "role is invalid"),
            ([{"role": "user", "content": 3}], "content must be text"),
            ([{"role": "assistant", "content": None}], "requires final content"),
            ([{"role": "system", "content": None}], "require text content"),
            ([{"role": "assistant", "content": None, "tool_calls": "x"}], "tool call history is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": []}], "tool call history is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [None]}], "protocol state is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [{**valid_call, "type": "other"}]}], "protocol state is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [{**valid_call, "id": ""}]}], "identity is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [{**valid_call, "id": "bad\nid"}]}], "identity is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [{**valid_call, "function": None}]}], "identity is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [{**valid_call, "function": {"name": "", "arguments": "{}"}}]}], "request is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [{**valid_call, "function": {"name": "search", "arguments": "no-json"}}]}], "arguments are invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [{**valid_call, "function": {"name": "search", "arguments": "[]"}}]}], "must be an object"),
            ([{"role": "assistant", "content": None, "tool_calls": [valid_call]}, {"role": "user", "content": "x"}], "before another chat message"),
            ([{"role": "tool", "content": None}], "requires its matching"),
            ([{"role": "tool", "tool_call_id": "x", "content": "r"}], "reference is invalid"),
            ([{"role": "assistant", "content": "x", "reasoning_content": "bad"}], "reasoning protocol state is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [valid_call], "reasoning_content": 3}], "reasoning protocol state is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [valid_call], "reasoning_content": "x" * 64_001}], "reasoning protocol state is invalid"),
            ([{"role": "tool", "tool_call_id": "call-1", "content": None}], "reference is invalid"),
            ([{"role": "assistant", "content": None, "tool_calls": [valid_call]},
              {"role": "tool", "tool_call_id": "call-1", "content": None}], "tool results require text"),
        ]
        for messages, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                _validate_messages(messages)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            _validate_messages([{"role": "assistant", "content": None, "tool_calls": [valid_call]}])
        duplicated = [{"role": "assistant", "content": None, "tool_calls": [valid_call, valid_call]}]
        with self.assertRaisesRegex(ValueError, "identity is invalid"):
            _validate_messages(duplicated)
        ordered = _validate_messages([{"role": "assistant", "content": None, "tool_calls": [valid_call]},
                                      {"role": "tool", "tool_call_id": "call-1", "content": "ok"}])
        self.assertEqual("ok", ordered[-1]["content"])

    def test_completion_parser_validates_shape_tool_calls_and_reasoning_separately(self):
        from agentforge.errors import ProviderError
        from agentforge.providers.deepseek import _parse_completion
        invalid = [
            ({}, "choices list"), ({"choices": "x"}, "choices list"),
            ({"choices": [None]}, "choices list"), ({"choices": [{"message": None}]}, "assistant message"),
            ({"choices": [{"message": {"content": 1}}]}, "invalid final content"),
            ({"choices": [{"message": {"reasoning_content": 1}}]}, "invalid reasoning"),
            ({"choices": [{"message": {"tool_calls": "x"}}]}, "invalid tool-call data"),
            ({"choices": [{"message": {"tool_calls": [None]}}]}, "malformed tool"),
            ({"choices": [{"message": {"tool_calls": [{"function": {}}]}}]}, "malformed tool"),
            ({"choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": "x", "arguments": "no"}}]}}]}, "non-JSON"),
            ({"choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": "x", "arguments": "[]"}}]}}]}, "JSON object"),
            ({"choices": [{"message": {"content": "x"}, "finish_reason": 7}]}, "finish reason"),
        ]
        for payload, expected in invalid:
            with self.subTest(expected=expected), self.assertRaisesRegex(ProviderError, expected):
                _parse_completion(payload)
        valid = _parse_completion({"id": "r", "choices": [{"delta": {
            "content": None, "reasoning_content": "private", "tool_calls": [
                {"id": "c", "function": {"name": "search", "arguments": '{"q":"x"}'}}]},
            "finish_reason": "tool_calls"}], "usage": None})
        self.assertEqual("", valid.content)
        self.assertEqual("private", valid.reasoning_content)
        self.assertEqual({"q": "x"}, valid.tool_calls[0]["arguments"])
        no_id = _parse_completion({"id": 3, "choices": [{"message": {"content": "ok"}}]})
        self.assertIsNone(no_id.request_id)

    def test_tool_schema_validation_is_bounded_and_deduplicated(self):
        from agentforge.providers.deepseek import _validate_tool_schemas
        valid = {"type": "function", "function": {"name": "search", "description": "Find code",
                 "parameters": {"type": "object", "properties": {}}}}
        self.assertEqual([valid], _validate_tool_schemas([valid]))
        invalid_cases = [
            ("x", "bounded limit"), ([{}] * 9, "bounded limit"),
            ([None], "schema is invalid"), ([{"type": "other", "function": {}}], "schema is invalid"),
            ([{"type": "function", "function": None}], "schema is invalid"),
            ([{"type": "function", "function": {"name": "", "description": "x", "parameters": {}}}], "schema fields"),
            ([{"type": "function", "function": {"name": "x", "description": 2, "parameters": {}}}], "schema fields"),
            ([{"type": "function", "function": {"name": "x", "description": "x", "parameters": []}}], "schema fields"),
        ]
        for tools, expected in invalid_cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                _validate_tool_schemas(tools)
        with self.assertRaisesRegex(ValueError, "schema fields"):
            _validate_tool_schemas([valid, valid])
        oversized = {"type": "function", "function": {"name": "x", "description": "d",
                      "parameters": {"schema": "x" * 17_000}}}
        with self.assertRaisesRegex(ValueError, "schema fields"):
            _validate_tool_schemas([oversized])

    def test_sse_parser_handles_incremental_tool_calls_and_rejects_invalid_frames(self):
        from agentforge.providers.deepseek import _DeepSeekFailure, _read_sse
        first = {"id": "stream-1", "choices": [{"delta": {
            "content": "Hi", "reasoning_content": "thinking", "tool_calls": [
                {"index": 0, "id": "call-", "function": {"name": "sea", "arguments": '{"q"'}}]},
            "finish_reason": None}]}
        second = {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "1", "function": {"name": "rch", "arguments": ':"x"}'}}]},
            "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 4}}
        body = (f"data: {json.dumps(first)}\n\n".encode() + f"data: {json.dumps(second)}\n\n".encode()
                + b": keepalive\n\n" + b"event: ignored\n\n" + b"data: [DONE]\n\n")
        chunks = []
        parsed = _read_sse(Response(body=body), on_content=chunks.append)
        self.assertEqual(["Hi"], chunks)
        self.assertEqual("thinking", parsed["choices"][0]["message"]["reasoning_content"])
        self.assertEqual("call-1", parsed["choices"][0]["message"]["tool_calls"][0]["id"])
        self.assertEqual("search", parsed["choices"][0]["message"]["tool_calls"][0]["function"]["name"])
        self.assertEqual(4, parsed["usage"]["prompt_tokens"])
        invalid_streams = [
            (b"data: not-json\n\n", "invalid_stream_json"),
            (b"data: []\n\n", "invalid_stream_chunk"),
            (b"data: {}\n\n", "invalid_stream_chunk"),
            (b'data: {"choices":[{"delta":[]}]}\n\n', "invalid_stream_delta"),
            (b'data: {"choices":[{"delta":{"content":1}}]}\n\n', "invalid_stream_content"),
            (b'data: {"choices":[{"delta":{"reasoning_content":1}}]}\n\n', "invalid_stream_reasoning"),
            (b'data: {"choices":[{"delta":{"tool_calls":"x"}}]}\n\n', "invalid_stream_tool_call"),
            (b'data: {"choices":[{"delta":{"tool_calls":[null]}}]}\n\n', "invalid_stream_tool_call"),
            (b'data: {"choices":[{"delta":{"tool_calls":[{"index":"x"}]}}]}\n\n', "invalid_stream_tool_call"),
            (b"data: \xff\n\n", "invalid_stream_utf8"),
        ]
        for stream, code in invalid_streams:
            with self.subTest(code=code), self.assertRaises(_DeepSeekFailure) as caught:
                _read_sse(Response(body=stream + b"data: [DONE]\n\n"))
            self.assertEqual(code, caught.exception.code)
        with self.assertRaises(_DeepSeekFailure) as caught:
            _read_sse(Response(body=b"data: [DONE]\n\n"), max_bytes=2)
        self.assertEqual("stream_too_large", caught.exception.code)
        with self.assertRaises(_DeepSeekFailure) as caught:
            _read_sse(Response(body=b"data: x" * 10_000), max_bytes=100_000)
        self.assertEqual("stream_too_large", caught.exception.code)
        with self.assertRaises(_DeepSeekFailure) as caught:
            _read_sse(Response(body=b"data: {\"choices\":[]}\n\n"))
        self.assertEqual("incomplete_stream", caught.exception.code)
        trailing_data = b'data: {"choices":[]} '
        with self.assertRaises(_DeepSeekFailure) as caught:
            _read_sse(Response(body=trailing_data))
        self.assertEqual("incomplete_stream", caught.exception.code)
        optional_tool_fields = (b'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call","function":null}]},'
                                b'"finish_reason":"tool_calls"}]}\n\ndata: [DONE]\n\n')
        self.assertEqual("call", _read_sse(Response(body=optional_tool_fields))["choices"][0]["message"]["tool_calls"][0]["id"])
        with self.assertRaises(TimeoutError):
            _read_sse(Response(body=b"data: [DONE]\n\n"), deadline=1, monotonic=lambda: 2)
        ticks = iter((0, 11))
        with self.assertRaises(TimeoutError):
            _read_sse(Response(body=b"data: [DONE]\n\n"), deadline=10, monotonic=lambda: next(ticks))


class DeepSeekApplicationBoundaryTests(unittest.TestCase):
    def setUp(self):
        from agentforge.context import build_context
        from agentforge.models import SearchResult
        payload = discovery_payload()
        payload["data"][0]["api_capabilities"] = {"chat_completions": {
            "streaming": True, "tool_calling": True}}
        self.descriptor = parse_deepseek_models(payload)[0]
        self.client = DeepSeekClient("synthetic-key")
        self.context = build_context("What value is returned?", [SearchResult(
            "repo", "src/value.py", 4, 4, "return 42", "content-hash", 1.0)])

    def completion(self, content="The function returns 42 [E1].", *, reasoning=None,
                   finish="stop", tools=(), usage=None):
        from agentforge.providers.deepseek import DeepSeekCompletion
        return DeepSeekCompletion(content, reasoning, finish, tuple(tools), usage or {}, "req")

    def provider(self, **kwargs):
        from agentforge.providers.deepseek import DeepSeekProvider
        descriptor = kwargs.pop("descriptor", self.descriptor)
        return DeepSeekProvider(self.client, self.descriptor.model_id,
                                descriptor=descriptor, **kwargs)

    def test_thinking_length_is_rejected_with_usage_and_next_complete_answer_succeeds(self):
        from agentforge.errors import ProviderError
        payload = {"choices": [{"message": {"content": "The function returns 42 [E1].",
                    "reasoning_content": "private"}, "finish_reason": "length"}],
                   "usage": {"completion_tokens": 48, "completion_tokens_details": {"reasoning_tokens": 40}}}
        complete = {"choices": [{"message": {"content": "The function returns 42 [E1].",
                     "reasoning_content": "private"}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 18, "completion_tokens_details": {"reasoning_tokens": 9}}}
        connections = iter([Connection(Response(payload=payload)), Connection(Response(payload=complete))])
        self.client.connection_factory = lambda *a, **kw: next(connections)
        provider = self.provider(thinking=True, effort="high", max_output_tokens=512)
        with self.assertRaisesRegex(ProviderError, "output limit"):
            provider.answer(self.context)
        stats = self.client.request_stats()
        self.assertEqual(("length", 48, 40, "truncated_final_content"),
                         (stats["last_finish_reason"], stats["last_usage"]["completion_tokens"],
                          stats["last_usage"]["reasoning_tokens"], stats["last_application_error_code"]))
        answer = provider.answer(self.context)
        self.assertIn("42", answer.text)
        stats = self.client.request_stats()
        self.assertEqual(("stop", 18, 9, None),
                         (stats["last_finish_reason"], stats["last_usage"]["completion_tokens"],
                          stats["last_usage"]["reasoning_tokens"], stats["last_application_error_code"]))
        self.assertEqual((2, 2, 0), (stats["attempted"], stats["succeeded"], stats["retries"]))

    def test_provider_construction_summary_and_stats_cover_supported_options(self):
        from agentforge.errors import ProviderError
        from agentforge.providers.deepseek import DeepSeekProvider
        provider = self.provider(thinking=True, effort="high", stream=True, max_output_tokens=32)
        self.assertTrue(provider.thinking_request)
        self.assertEqual(provider.request_stats(), self.client.request_stats())
        self.client.chat = Mock(return_value=self.completion("compact summary"))
        self.assertEqual("compact summary", provider.summary_text([], max_output_tokens=20))
        self.assertEqual(20, self.client.chat.call_args.kwargs["max_output_tokens"])
        self.client.chat = Mock(return_value=self.completion("", tools=({"id": "c"},)))
        with self.assertRaisesRegex(ProviderError, "plain summary"):
            provider.summary_text([], max_output_tokens=20)
        self.client.chat = Mock(return_value=self.completion(" "))
        with self.assertRaisesRegex(ProviderError, "plain summary"):
            provider.summary_text([], max_output_tokens=20)
        with self.assertRaisesRegex(ValueError, "client is required"):
            DeepSeekProvider(object(), self.descriptor.model_id, descriptor=self.descriptor)
        with self.assertRaisesRegex(ValueError, "thinking must be a boolean"):
            DeepSeekProvider(self.client, self.descriptor.model_id, thinking=1, descriptor=self.descriptor)
        with self.assertRaisesRegex(ValueError, "effort is invalid"):
            DeepSeekProvider(self.client, self.descriptor.model_id, effort="high", descriptor=self.descriptor)
        with self.assertRaisesRegex(ValueError, "metadata"):
            DeepSeekProvider(self.client, "other", descriptor=self.descriptor)
        from dataclasses import replace
        with self.assertRaisesRegex(ValueError, "not supported by this DeepSeek model"):
            DeepSeekProvider(self.client, self.descriptor.model_id, thinking=True, effort="high",
                             descriptor=replace(self.descriptor, supported_efforts=()))

    def test_provider_answer_validates_capabilities_response_and_citations(self):
        from agentforge.errors import CitationValidationError, ProviderError
        from agentforge.context import build_context
        from agentforge.providers.deepseek import DeepSeekProvider
        provider = self.provider(thinking=True, effort="high", stream=True)
        self.client.chat = Mock(return_value=self.completion())
        answer = provider.answer(self.context, settings={"thinking_enabled": True, "reasoning_effort": "high"},
                                 on_content=lambda _text: None)
        self.assertIn("E1", answer.text)
        kwargs = self.client.chat.call_args.kwargs
        self.assertEqual((True, "high", True), (kwargs["thinking"], kwargs["effort"], kwargs["stream"]))
        self.assertIsNotNone(kwargs["on_content"])
        from dataclasses import replace
        unsupported = self.provider(descriptor=replace(self.descriptor, supports_thinking=None))
        self.client.chat = Mock()
        with self.assertRaisesRegex(ProviderError, "Thinking capability"):
            unsupported.answer(self.context, settings={"thinking_enabled": True})
        self.client.chat.assert_not_called()
        no_evidence = build_context("unknown", [])
        self.client.chat = Mock()
        refused = unsupported.answer(no_evidence)
        self.assertFalse(refused.citations)
        self.client.chat.assert_not_called()
        for result, expected, code in (
            (self.completion(finish="length"), "output limit", "truncated_final_content"),
            (self.completion(tools=({"id": "c"},)), "requested a tool call", "model_tools_not_enabled_for_rag_ask"),
            (self.completion(" "), "empty final content", "empty_final_content"),
            (self.completion(finish="content_filter"), "unexpected final-response", "unexpected_finish_reason"),
        ):
            self.client.chat = Mock(return_value=result)
            with self.subTest(expected=expected), self.assertRaisesRegex(ProviderError, expected):
                unsupported.answer(self.context)
            self.assertEqual(code, self.client.request_stats()["last_application_error_code"])
        self.client.chat = Mock(return_value=self.completion("Fabricated [E99]."))
        with self.assertRaises(CitationValidationError):
            unsupported.answer(self.context)
        self.assertEqual("citation_validation", self.client.request_stats()["last_application_failure_phase"])

    def test_provider_stream_and_thinking_settings_must_be_confirmed(self):
        from dataclasses import replace
        from agentforge.errors import ProviderError
        from agentforge.providers.deepseek import DeepSeekProvider
        unknown = replace(self.descriptor, supports_streaming=None, supports_thinking=None)
        for kwargs in ({"stream": True}, {"thinking": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DeepSeekProvider(self.client, unknown.model_id, descriptor=unknown, **kwargs)
        disabled = replace(self.descriptor, supports_thinking=False)
        provider = DeepSeekProvider(self.client, disabled.model_id, descriptor=disabled)
        self.client.chat = Mock()
        with self.assertRaisesRegex(ProviderError, "no request was sent"):
            provider.answer(self.context, settings={"thinking_enabled": True})
        self.client.chat.assert_not_called()

    def test_provider_rejects_runtime_effort_and_stream_downgrade_before_request(self):
        from dataclasses import replace
        from agentforge.errors import ProviderError
        provider = self.provider(thinking=True, effort="high", stream=True)
        self.client.chat = Mock()
        with self.assertRaisesRegex(ProviderError, "effort is unsupported"):
            provider.answer(self.context, settings={"thinking_enabled": False, "reasoning_effort": "high"})
        self.client.chat.assert_not_called()
        provider = self.provider(stream=True)
        provider.descriptor = replace(self.descriptor, supports_streaming=False)
        with self.assertRaisesRegex(ProviderError, "Streaming capability"):
            provider.answer(self.context)
        self.client.chat.assert_not_called()

    def test_provider_summary_uses_safe_fallback_when_model_maximum_is_unknown(self):
        from dataclasses import replace
        provider = self.provider(descriptor=replace(self.descriptor, max_output_tokens=None))
        self.client.chat = Mock(return_value=self.completion("summary"))
        self.assertEqual("summary", provider.summary_text([], max_output_tokens=30))
        self.assertEqual(30, self.client.chat.call_args.kwargs["max_output_tokens"])


class DeepSeekAgentPlannerBoundaryTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace
        payload = discovery_payload()
        payload["data"][0]["api_capabilities"] = {"chat_completions": {"tool_calling": True,
                                                                           "streaming": True}}
        self.descriptor = parse_deepseek_models(payload)[0]
        self.SimpleNamespace = SimpleNamespace

    def _tool(self, name="search", call_id="c1"):
        return {"id": call_id, "name": name, "arguments": {"query": "value"},
                "raw_arguments": '{"query":"value"}'}

    def _completion(self, *, content="answer", tools=(), finish="stop", reasoning=None):
        return self.SimpleNamespace(content=content, tool_calls=tuple(tools),
                                    finish_reason=finish, reasoning_content=reasoning)

    def _planner(self, client=None, **kwargs):
        from agentforge.providers.deepseek_agent import DeepSeekAgentPlanner
        return DeepSeekAgentPlanner(client or Mock(chat=Mock(return_value=self._completion())),
                                    self.descriptor.model_id, self.descriptor, **kwargs)

    def test_planner_rejects_bad_identity_options_and_deadline(self):
        from agentforge.errors import ProviderError
        from agentforge.providers.deepseek_agent import DeepSeekAgentPlanner
        for kwargs, expected in (({"model": "other"}, "does not match"),
                                 ({"thinking": 1}, "setting is invalid"),
                                 ({"thinking": True, "descriptor": None}, ""),
                                 ({"stream": True, "descriptor": None}, ""),
                                 ({"run_timeout_seconds": 0}, "timeout is outside"),
                                 ({"run_timeout_seconds": 601}, "timeout is outside")):
            if "model" in kwargs:
                with self.assertRaisesRegex(ValueError, expected):
                    DeepSeekAgentPlanner(Mock(), kwargs["model"], self.descriptor)
            elif kwargs.get("descriptor") is None and kwargs.get("thinking") is True:
                from dataclasses import replace
                unknown = replace(self.descriptor, supports_thinking=None)
                with self.assertRaisesRegex(ValueError, "capability is unknown"):
                    DeepSeekAgentPlanner(Mock(), self.descriptor.model_id, unknown, thinking=True)
            elif kwargs.get("descriptor") is None and kwargs.get("stream") is True:
                from dataclasses import replace
                unknown = replace(self.descriptor, supports_streaming=None)
                with self.assertRaisesRegex(ValueError, "capability is unknown"):
                    DeepSeekAgentPlanner(Mock(), self.descriptor.model_id, unknown, stream=True)
            else:
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, expected):
                    DeepSeekAgentPlanner(Mock(), self.descriptor.model_id, self.descriptor, **kwargs)
        class Clock:
            value = 0
            def __call__(self): return self.value
        clock = Clock()
        planner = self._planner(monotonic=clock, run_timeout_seconds=1)
        clock.value = 1
        with self.assertRaisesRegex(ProviderError, "run deadline"):
            planner.next_call("question", ())

    def test_planner_handles_repeated_audit_pending_tools_and_final_protocol(self):
        from agentforge.providers.deepseek_agent import DeepSeekAgentPlanner
        first = self._completion(content="", tools=(self._tool("search", "c1"),
                                                       self._tool("list-symbols", "c2")),
                                 finish="tool_calls", reasoning="internal")
        final = self._completion(content="Found the definition.")
        client = Mock(chat=Mock(side_effect=(first, final)))
        planner = self._planner(client)
        first_call = planner.next_call("inspect", ())
        self.assertEqual("search", first_call.name)
        repeated = planner.next_call("inspect", ())
        self.assertEqual(first_call, repeated)
        audit1 = self.SimpleNamespace(name="search", status="ok", result={"source": "E1"}, error=None)
        second_call = planner.next_call("inspect", [audit1])
        self.assertEqual("list-symbols", second_call.name)
        audit2 = self.SimpleNamespace(name="list-symbols", status="ok", result={"symbol": "f"}, error=None)
        self.assertIsNone(planner.next_call("inspect", [audit1, audit2]))
        self.assertEqual("Found the definition.", planner.final_answer)
        self.assertEqual(2, planner.request_count)
        followup = client.chat.call_args_list[1].args[0]
        self.assertEqual("internal", followup[-3]["reasoning_content"])
        self.assertEqual(["tool", "tool"], [item["role"] for item in followup[-2:]])

    def test_planner_rejects_audit_mismatch_oversized_result_and_bad_tool_response(self):
        from agentforge.errors import ProviderError
        client = Mock(chat=Mock(return_value=self._completion(content="", tools=(self._tool(),), finish="tool_calls")))
        planner = self._planner(client)
        planner.next_call("inspect", ())
        wrong = self.SimpleNamespace(name="list-symbols", status="ok", result={}, error=None)
        with self.assertRaisesRegex(ProviderError, "audit did not match"):
            planner.next_call("inspect", [wrong])
        client = Mock(chat=Mock(return_value=self._completion(content="", tools=(self._tool(),), finish="tool_calls")))
        planner = self._planner(client)
        planner.next_call("inspect", ())
        huge = self.SimpleNamespace(name="search", status="ok", result={"text": "x" * 65_000}, error=None)
        with self.assertRaisesRegex(ProviderError, "bounded follow-up"):
            planner.next_call("inspect", [huge])
        for completion, expected in (
            (self._completion(content="", tools=(self._tool("shell"),), finish="tool_calls"), "outside the read-only"),
            (self._completion(content="", tools=(self._tool("search", "same"), self._tool("list-symbols", "same")), finish="tool_calls"), "duplicate tool-call IDs"),
            (self._completion(content="", tools=(self._tool(),), finish="stop"), "unexpected finish reason"),
            (self._completion(content=""), "no final content"),
            (self._completion(content="partial", finish="length"), "truncated"),
            (self._completion(content="x", finish="content_filter"), "unexpected final finish"),
        ):
            planner = self._planner(Mock(chat=Mock(return_value=completion)))
            with self.subTest(expected=expected), self.assertRaisesRegex(ProviderError, expected):
                planner.next_call("inspect", ())

    def test_planner_metadata_tool_calling_and_effort_validation(self):
        from dataclasses import replace
        from agentforge.providers.deepseek_agent import DeepSeekAgentPlanner
        from agentforge.errors import ProviderError
        with self.assertRaisesRegex(ValueError, "no confirmed tool-calling"):
            DeepSeekAgentPlanner(Mock(), self.descriptor.model_id,
                                 replace(self.descriptor, supports_tool_calling=None))
        with self.assertRaisesRegex(ValueError, "thinking capability"):
            DeepSeekAgentPlanner(Mock(), self.descriptor.model_id,
                                 replace(self.descriptor, supports_thinking=None), thinking=True)
        with self.assertRaisesRegex(ValueError, "streaming capability"):
            DeepSeekAgentPlanner(Mock(), self.descriptor.model_id,
                                 replace(self.descriptor, supports_streaming=None), stream=True)
        with self.assertRaisesRegex(ValueError, "effort is unsupported"):
            self._planner(Mock(), effort="max")
        planner = self._planner(Mock(chat=Mock(return_value=self._completion())), thinking=True, effort="max")
        planner.next_call("inspect", ())
        self.assertEqual("max", planner.client.chat.call_args.kwargs["effort"])


if __name__ == "__main__":
    unittest.main()
