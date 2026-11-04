import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import AF012_MULTIMODEL_HOST_ACCEPTANCE as runner


class MultiModelHostRunnerGateTests(unittest.TestCase):
    def test_restricted_environment_blocks_before_credential_access_or_network(self):
        credential_module = types.ModuleType("agentforge.credential_store")
        credential_module.CredentialStore = lambda: (_ for _ in ()).throw(
            AssertionError("credential store must not be opened in the restricted environment"))
        with (patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}, clear=True),
              patch.dict(sys.modules, {"agentforge.credential_store": credential_module}),
              patch.object(runner, "_finish", side_effect=lambda result, code: code),
              patch("builtins.input", side_effect=AssertionError("no prompt expected"))):
            self.assertEqual(2, runner.run())

    def test_thinking_budget_and_per_request_usage_are_saved_even_on_truncation(self):
        from agentforge.errors import ProviderError
        from agentforge import credential_store, model_registry
        from agentforge.providers import deepseek

        descriptors = [SimpleNamespace(model_id="first", context_window=8192, max_output_tokens=8192,
                         supports_thinking=True, supports_streaming=False, default_effort="high",
                         supported_efforts=("high",)),
                       SimpleNamespace(model_id="second", context_window=8192, max_output_tokens=256,
                         supports_thinking=True, supports_streaming=False, default_effort="high",
                         supported_efforts=("high",))]
        calls = []

        class Client:
            def __init__(self, *args, **kwargs):
                self.stats = {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0}
            def set_credential_source(self, source): pass
            def list_models(self): return {}
            def request_stats(self): return dict(self.stats)

        class Provider:
            def __init__(self, client, model, max_output_tokens, thinking=False, **kwargs):
                self.client, self.model, self.limit, self.thinking = client, model, max_output_tokens, thinking
            def answer(self, context, **kwargs):
                calls.append((self.model, self.thinking, self.limit))
                self.client.stats.update(attempted=len(calls), sent=len(calls), succeeded=len(calls),
                    last_usage={"completion_tokens": 22 if self.thinking else 5,
                                "reasoning_tokens": 17 if self.thinking else 0},
                    last_finish_reason="length" if self.model == "second" and self.thinking else "stop",
                    last_application_error_code="truncated_final_content" if self.model == "second" and self.thinking else None)
                if self.model == "second" and self.thinking:
                    raise ProviderError("DeepSeek stopped at the output limit; the partial answer was rejected.")
                return SimpleNamespace(text="7 [E1]", citations=("src/example.py:1-2",))

        saved = []
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(credential_store, "CredentialStore", return_value=SimpleNamespace(resolve=lambda: ("synthetic", "dpapi"))),
              patch.object(model_registry, "parse_deepseek_models", return_value=descriptors),
              patch.object(deepseek, "DeepSeekClient", Client), patch.object(deepseek, "DeepSeekProvider", Provider),
              patch.object(runner, "_finish", side_effect=lambda result, code: saved.append(result) or code),
              patch("builtins.input", return_value="SEND SYNTHETIC TESTS")):
            self.assertEqual(1, runner.run())
        self.assertEqual([("first", False, 48), ("first", True, 512),
                          ("second", False, 48), ("second", True, 256)], calls)
        first, second = saved[0]["deepseek"]["models_smoke_tested"]
        self.assertEqual("passed", first["thinking"])
        self.assertEqual(17, first["thinking_diagnostics"]["usage"]["reasoning_tokens"])
        self.assertEqual("failed", second["thinking"])
        self.assertEqual("length", second["thinking_diagnostics"]["finish_reason"])
        self.assertEqual("truncated_final_content", second["thinking_diagnostics"]["application_error_code"])
        self.assertEqual(4, saved[0]["deepseek"]["chat_requests"]["succeeded"])
        self.assertEqual(0, saved[0]["deepseek"]["chat_requests"]["retries"])
        self.assertEqual(["normal", "thinking", "normal", "thinking"],
                         [entry["case"] for entry in saved[0]["usage_by_case"]])
        self.assertEqual(17, saved[0]["usage_by_case"][-1]["usage"]["reasoning_tokens"])

    def test_diagnostics_do_not_reuse_previous_request_when_no_request_sent(self):
        client = SimpleNamespace(request_stats=lambda: {"attempted": 1, "last_usage": {"completion_tokens": 8},
                                                       "last_finish_reason": "stop"})
        self.assertEqual({"status": "not_sent", "usage": {}, "finish_reason": None,
                          "application_error_code": None}, runner._case_diagnostics(client, 1))

    def test_missing_new_credential_blocks_before_cloud_consent_or_request(self):
        credential_module = types.ModuleType("agentforge.credential_store")

        class Credentials:
            def resolve(self): return None, "none"

        credential_module.CredentialStore = Credentials
        with (patch.dict(os.environ, {}, clear=True),
              patch.dict(sys.modules, {"agentforge.credential_store": credential_module}),
              patch.object(runner, "_finish", side_effect=lambda result, code: code),
              patch("builtins.input", side_effect=AssertionError("no consent prompt expected"))):
            self.assertEqual(2, runner.run())

    def test_cloud_smoke_decline_does_not_construct_or_call_provider(self):
        credential_module = types.ModuleType("agentforge.credential_store")

        class Credentials:
            def resolve(self): return "synthetic-key-never-used", "dpapi"

        credential_module.CredentialStore = Credentials
        with (patch.dict(os.environ, {}, clear=True),
              patch.dict(sys.modules, {"agentforge.credential_store": credential_module}),
              patch.object(runner, "_finish", side_effect=lambda result, code: code),
              patch("builtins.input", return_value="NO"),
              patch("agentforge.providers.deepseek.DeepSeekClient",
                    side_effect=AssertionError("no cloud client may be constructed after decline"))):
            self.assertEqual(2, runner.run())


if __name__ == "__main__":
    unittest.main()
