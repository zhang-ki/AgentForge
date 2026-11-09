import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agentforge.cli import create_provider, parser
from agentforge.model_types import LOCAL_QWEN
from agentforge.model_registry import parse_deepseek_models
from agentforge.models import BuiltContext
from agentforge.planner import LLMPlanner
from agentforge.providers.deepseek import (
    DeepSeekClient, DeepSeekCompletion, DeepSeekProvider, _chat_body_limits,
    _json_utf8_size, _usage_metadata,
)
from agentforge.providers.dialogue_protocol import _conversation_messages
from agentforge.token_budget import ContextBudgetManager


def _deepseek_descriptor(**changes):
    descriptor = parse_deepseek_models({"object": "list", "data": [{
        "id": "deepseek-synthetic", "context_window": 1_048_576,
        "max_output_tokens": 393_216,
        "effort": {"supported_levels": ["low", "high", "max"]},
        "api_capabilities": {"chat_completions": {"streaming": True}},
    }]})[0]
    return replace(descriptor, **changes)


class _Response:
    status = 200
    headers = {}

    def __init__(self, payload):
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self, limit=-1):
        value, self.body = self.body[:limit], self.body[limit:]
        return value

    def getheader(self, _name):
        return None


class _Connection:
    def __init__(self, response):
        self.response = response
        self.sock = None
        self.request_args = None

    def request(self, method, path, body=None, headers=None):
        self.request_args = method, path, body, headers

    def getresponse(self):
        return self.response

    def close(self):
        pass


class OutputBudgetTests(unittest.TestCase):
    def setUp(self):
        self.manager = ContextBudgetManager()
        self.deepseek = _deepseek_descriptor()

    def test_presets_and_max_resolve_against_the_selected_model(self):
        expected = {"concise": 4_096, "standard": 16_384, "long": 65_536, "max": 393_216}
        for mode, tokens in expected.items():
            with self.subTest(mode=mode):
                result = self.manager.resolve_output_budget(self.deepseek, mode=mode)
                self.assertEqual(tokens, result.effective_tokens)
        self.assertEqual(512, self.manager.resolve_output_budget(
            LOCAL_QWEN, mode="standard").effective_tokens)
        qwen_max = self.manager.resolve_output_budget(LOCAL_QWEN, mode="max")
        self.assertEqual(512, qwen_max.effective_tokens)
        self.assertIsNone(qwen_max.model_max_output_tokens)
        self.assertIn("adapter maximum", qwen_max.source)

    def test_model_specific_limits_and_explicit_override_are_never_guessed(self):
        smaller = replace(self.deepseek, max_output_tokens=32_768)
        provider_capped = replace(self.deepseek, provider_max_output_tokens=8_192)
        self.assertEqual(32_768, self.manager.resolve_output_budget(smaller, mode="max").effective_tokens)
        self.assertEqual(8_192, self.manager.resolve_output_budget(provider_capped, mode="long").effective_tokens)
        self.assertEqual(20_000, self.manager.resolve_output_budget(
            self.deepseek, explicit_output_tokens=20_000).effective_tokens)
        with self.assertRaisesRegex(ValueError, "exceeds.*limit"):
            self.manager.resolve_output_budget(self.deepseek, explicit_output_tokens=393_217)
        with self.assertRaisesRegex(ValueError, "exceeds.*limit"):
            self.manager.resolve_output_budget(LOCAL_QWEN, explicit_output_tokens=513)
        with self.assertRaisesRegex(ValueError, "no verified output limit"):
            self.manager.resolve_output_budget(replace(LOCAL_QWEN, adapter_max_output_tokens=None))

    def test_cli_parser_uses_standard_without_an_implicit_token_override(self):
        arguments = parser().parse_args(["ask", "synthetic question"])
        self.assertEqual("standard", arguments.output_budget)
        self.assertIsNone(arguments.max_output_tokens)

    def test_cli_provider_and_direct_provider_share_budget_resolution(self):
        registry = Mock()
        registry.get_for_request.return_value = self.deepseek
        registry.get.return_value = LOCAL_QWEN
        client = DeepSeekClient("synthetic-key")
        with patch("agentforge.model_registry.ModelRegistry", return_value=registry):
            provider = create_provider(
                "deepseek", model=self.deepseek.model_id, deepseek_api_key="synthetic-key",
            )
            long_provider = create_provider(
                "deepseek", model=self.deepseek.model_id, deepseek_api_key="synthetic-key",
                output_budget_mode="long",
            )
        direct = DeepSeekProvider(client, self.deepseek.model_id, descriptor=self.deepseek)
        self.assertEqual(16_384, provider.max_output_tokens)
        self.assertEqual(16_384, direct.max_output_tokens)
        self.assertEqual(65_536, long_provider.max_output_tokens)
        self.assertEqual(16_384, provider.request_stats()["output_budget"]["effective_tokens"])

    def test_model_switch_recalculates_output_budget_for_each_provider(self):
        registry = Mock()
        registry.get_for_request.return_value = self.deepseek
        registry.get.return_value = LOCAL_QWEN
        with patch("agentforge.model_registry.ModelRegistry", return_value=registry):
            deepseek = create_provider(
                "deepseek", model=self.deepseek.model_id, deepseek_api_key="synthetic-key",
            )
            qwen = create_provider(
                "llama-server", base_url="http://127.0.0.1:32123/v1/chat/completions",
                model=LOCAL_QWEN.model_id, api_key="synthetic-key",
            )
            deepseek_again = create_provider(
                "deepseek", model=self.deepseek.model_id, deepseek_api_key="synthetic-key",
            )
        self.assertEqual([16_384, 512, 16_384], [
            deepseek.max_output_tokens, qwen.max_output_tokens, deepseek_again.max_output_tokens,
        ])

    def test_local_qwen_stays_within_its_adapter_limit_for_every_preset(self):
        client = SimpleNamespace(max_output_tokens=512, request_stats=lambda: {})
        for mode in ("concise", "standard", "long", "max"):
            with self.subTest(mode=mode):
                provider = create_provider(
                    "llama-server", base_url="http://127.0.0.1:32123/v1/chat/completions",
                    model=LOCAL_QWEN.model_id, api_key="synthetic-key", output_budget_mode=mode,
                )
                self.assertEqual(512, provider.max_output_tokens)
        with self.assertRaisesRegex(ValueError, "exceeds.*limit"):
            create_provider(
                "llama-server", base_url="http://127.0.0.1:32123/v1/chat/completions",
                model=LOCAL_QWEN.model_id, api_key="synthetic-key", max_output_tokens=513,
            )

    def test_route_planner_budget_is_independent_from_final_answer_budget(self):
        self.assertEqual(4_096, LLMPlanner.OUTPUT_TOKENS)
        self.assertEqual(4_096, LLMPlanner.output_tokens_for(SimpleNamespace(descriptor=self.deepseek)))
        self.assertEqual(512, LLMPlanner.output_tokens_for(SimpleNamespace(descriptor=LOCAL_QWEN)))
        qwen_execution = replace(self.deepseek, context_window=4_096, adapter_max_output_tokens=512)
        self.assertEqual(512, LLMPlanner.output_tokens_for(SimpleNamespace(descriptor=qwen_execution)))

    def test_compatibility_context_builder_defaults_to_model_standard_budget(self):
        context = BuiltContext("synthetic question", (), "", 0, 1_000)
        conversation = {"conversation_id": "synthetic", "repository_id": "synthetic"}
        messages = _conversation_messages(
            context, conversation,
            [{"role": "system", "content": "system"},
             {"role": "user", "content": "question"}],
            descriptor=self.deepseek,
        )
        self.assertEqual(16_384, conversation["context_diagnostics"]["requested_output_tokens"])
        self.assertTrue(messages)

    def test_thinking_limit_is_shared_and_usage_splits_reasoning_when_available(self):
        payload = {"id": "synthetic", "choices": [{"message": {"content": "answer"},
                   "finish_reason": "stop"}], "usage": {
                       "prompt_tokens": 10, "completion_tokens": 40, "total_tokens": 50,
                       "completion_tokens_details": {"reasoning_tokens": 13},
                   }}
        connection = _Connection(_Response(payload))
        client = DeepSeekClient("synthetic-key", connection_factory=lambda *_args, **_kwargs: connection)
        result = client.chat([{"role": "user", "content": "synthetic"}],
                             model=self.deepseek.model_id, max_output_tokens=16_384,
                             thinking=True)
        request_body = json.loads(connection.request_args[2])
        self.assertEqual(16_384, request_body["max_tokens"])
        self.assertTrue(self.deepseek.output_limit_includes_reasoning)
        self.assertEqual(13, result.usage["reasoning_tokens"])
        self.assertEqual(27, result.usage["visible_completion_tokens"])
        self.assertEqual(16_384, client.request_stats()["last_requested_output_tokens"])

    def test_stream_and_nonstream_provider_use_identical_answer_allowance(self):
        descriptor = self.deepseek
        plain = DeepSeekProvider(DeepSeekClient("synthetic-key"), descriptor.model_id,
                                 descriptor=descriptor, stream=False)
        streaming = DeepSeekProvider(DeepSeekClient("synthetic-key"), descriptor.model_id,
                                     descriptor=descriptor, stream=True)
        self.assertEqual(16_384, plain.max_output_tokens)
        self.assertEqual(plain.max_output_tokens, streaming.max_output_tokens)

    def test_dynamic_http_response_limit_scales_and_never_silently_truncates(self):
        _, long_response = _chat_body_limits([{"role": "user", "content": "synthetic"}], 65_536)
        _, max_response = _chat_body_limits([{"role": "user", "content": "synthetic"}], 393_216)
        self.assertGreater(long_response, 2_000_000)
        self.assertGreater(max_response, long_response)
        self.assertLessEqual(max_response, 64_000_000)
        value = {"messages": [{"role": "user", "content": "中文\n\"\\"}], "n": [True, None, 7]}
        self.assertEqual(len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
                         _json_utf8_size(value, stop_after=1000))

    def test_oversized_request_is_rejected_before_serializing_or_connecting(self):
        factory = Mock(side_effect=AssertionError("oversized request must not connect"))
        client = DeepSeekClient("synthetic-key", connection_factory=factory)
        with self.assertRaisesRegex(Exception, "bounded request size"):
            client._request("POST", "/chat/completions", body={"content": "x" * 10_000},
                            max_request_bytes=128)
        factory.assert_not_called()
        self.assertEqual("request_too_large", client.request_stats()["last_error_code"])

    def test_tokenizer_and_model_capability_failures_are_explicit(self):
        manager = ContextBudgetManager()
        for tokenizer_id, tokenizer, counter in (
            (" ", lambda text: len(text), None),
            ("valid", None, None),
            ("valid", lambda text: len(text), object()),
        ):
            with self.subTest(tokenizer_id=tokenizer_id, tokenizer=tokenizer, counter=counter):
                with self.assertRaises(ValueError):
                    manager.register_tokenizer(tokenizer_id, tokenizer, request_counter=counter)

        invalid_counter = ContextBudgetManager()
        invalid_counter.register_tokenizer("bad-counter", lambda _text: True)
        with self.assertRaisesRegex(ValueError, "invalid token count"):
            invalid_counter.measure_text("synthetic", model=replace(
                self.deepseek, tokenizer_id="bad-counter"))
        bad_profile = replace(self.deepseek, provider_max_output_tokens=True)
        with self.assertRaisesRegex(ValueError, "output capability is invalid"):
            manager.budget(bad_profile, requested_output_tokens=128)
        bad_input_profile = replace(self.deepseek, max_input_tokens=0)
        with self.assertRaisesRegex(ValueError, "input capability is invalid"):
            manager.budget(bad_input_profile, requested_output_tokens=128)

    def test_output_budget_rejects_unknown_modes_and_impossible_windows(self):
        with self.assertRaisesRegex(ValueError, "Output budget mode"):
            self.manager.resolve_output_budget(self.deepseek, mode="unbounded")
        tiny = replace(self.deepseek, context_window=1, max_input_tokens=None)
        with self.assertRaisesRegex(ValueError, "cannot reserve input"):
            self.manager.resolve_output_budget(tiny, mode="max")
        no_output_limit = replace(self.deepseek, max_output_tokens=None,
                                  provider_max_output_tokens=None,
                                  adapter_max_output_tokens=None)
        with self.assertRaisesRegex(ValueError, "no verified output limit"):
            self.manager.resolve_output_budget(no_output_limit)


if __name__ == "__main__":
    unittest.main()
