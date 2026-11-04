import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agentforge import conversation_console


class ConversationConsoleRepositoryTests(unittest.TestCase):
    def test_model_list_command_renders_cached_capabilities_without_refreshing_remote_models(self):
        from agentforge.model_registry import ModelDescriptor, LOCAL_QWEN, OFFLINE_PROVIDER

        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-1", "title": "session", "provider_id": "llama-server",
            "model_id": "qwen3.5-4b", "thinking_enabled": False,
        }}
        cloud = ModelDescriptor("deepseek", "deepseek-flash", "Flash", "cloud", 65536, 8192,
            supports_streaming=True, supports_tool_calling=True, supports_thinking=True,
            supported_efforts=("low", "high", "max"), availability="UNKNOWN")

        class Registry:
            cache_status = "stale"
            cache_age_seconds = 90000
            def __init__(self): self.refresh_calls = 0
            def all(self): return (LOCAL_QWEN, OFFLINE_PROVIDER, cloud)
            def refresh_deepseek(self, client):
                self.refresh_calls += 1
                raise AssertionError("/modellist must not cause a cloud request")

        registry = Registry()
        calls = []
        def fake_call(*args):
            calls.append(args)
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            raise AssertionError(args)

        output = io.StringIO()
        with (patch("agentforge.conversation_console._registry", return_value=registry),
              patch("agentforge.credential_store.CredentialStore.status", return_value={
                  "configured": False, "source": "none", "stored_credential_present": False}),
              patch.dict("os.environ", {}, clear=True),
              patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("builtins.input", side_effect=["/modellist", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())

        text = output.getvalue()
        self.assertIn("Qwen Local", text)
        self.assertIn("deepseek-flash", text)
        self.assertIn("Context: 65536", text)
        self.assertIn("stale model list", text)
        self.assertEqual(0, registry.refresh_calls)

    def test_model_command_displays_current_session_binding(self):
        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-1", "title": "session", "provider_id": "llama-server",
            "model_id": "qwen3.5-4b", "thinking_enabled": False,
        }}
        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            raise AssertionError(args)
        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("builtins.input", side_effect=["/model", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())
        self.assertIn("当前模型：Qwen Local", output.getvalue())

    def test_model_default_checks_qwen_before_persisting_binding(self):
        from agentforge.model_registry import LOCAL_QWEN
        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-1", "title": "session", "provider_id": "deepseek",
            "model_id": "deepseek-flash", "thinking_enabled": False,
        }}
        calls = []
        def fake_call(*args):
            calls.append(args)
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": dict(state["current"])}
            if args[:2] == ("session", "set-model"):
                state["current"].update(provider_id="llama-server", model_id="qwen3.5-4b")
                return {"result": dict(state["current"])}
            raise AssertionError(args)
        output = io.StringIO()
        class Registry:
            def get(self, key):
                if key in {"qwen-local", "qwen3.5-4b"}: return LOCAL_QWEN
                raise ValueError("unknown model")
        with (patch("agentforge.conversation_console._registry", return_value=Registry()),
              patch("agentforge.model_registry.check_qwen_identity", return_value={"health": "ready"}) as check,
              patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "synthetic-key"}, clear=True),
              patch("builtins.input", side_effect=["/model default", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())
        check.assert_called_once()
        self.assertIn(("session", "set-model", "conversation-1", "llama-server", "qwen3.5-4b", "--thinking", "off"), calls)
        self.assertEqual("llama-server", state["current"]["provider_id"])

    def test_failed_qwen_health_check_keeps_previous_binding(self):
        from agentforge.model_registry import LOCAL_QWEN
        current = {"id": "conversation-1", "title": "session", "provider_id": "deepseek",
                   "model_id": "deepseek-flash", "thinking_enabled": False}
        with (patch("agentforge.conversation_console._registry") as registry,
              patch("agentforge.model_registry.check_qwen_identity",
                    side_effect=ValueError("service unavailable")),
              patch("agentforge.conversation_console._save_model_binding") as save,
              patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "synthetic-key"}, clear=True)):
            registry.return_value.get.return_value = LOCAL_QWEN
            with self.assertRaisesRegex(ValueError, "service unavailable"):
                conversation_console._switch_model("default", current)
        save.assert_not_called()
        self.assertEqual(("deepseek", "deepseek-flash"),
                         (current["provider_id"], current["model_id"]))

    def test_model_refresh_is_explicit_and_reports_only_metadata_and_credential_source(self):
        from agentforge.model_registry import ModelDescriptor
        descriptor = ModelDescriptor("deepseek", "deepseek-flash", "Flash", "cloud", 65536, 8192,
                                     supports_thinking=True, supported_efforts=("low", "high"))
        class Registry:
            def refresh_deepseek(self, client):
                self.client = client
                return (descriptor,)
        registry = Registry()
        client = object()
        state = {"repository": r"D:\synthetic\repo", "current": None}
        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": None}
            raise AssertionError(args)
        output = io.StringIO()
        with (patch("agentforge.conversation_console._registry", return_value=registry),
              patch("agentforge.conversation_console._deepseek_client", return_value=(client, "dpapi")),
              patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("builtins.input", side_effect=["/model refresh", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())
        self.assertIs(client, registry.client)
        self.assertIn("已从 DeepSeek 刷新 1 个模型", output.getvalue())
        self.assertIn("密钥来源：dpapi", output.getvalue())

    def test_thinking_and_effort_commands_dispatch_to_model_capability_gate(self):
        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-1", "title": "session", "provider_id": "deepseek",
            "model_id": "deepseek-flash", "thinking_enabled": False,
        }}
        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            raise AssertionError(args)
        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("agentforge.conversation_console._update_thinking", side_effect=lambda current, **kw: current) as update,
              patch("builtins.input", side_effect=["/thinking on", "/effort high", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())
        self.assertEqual([((state["current"],), {"thinking": True}),
                          ((state["current"],), {"effort": "high"})], update.call_args_list)

    def test_auth_status_and_set_do_not_echo_or_log_key_contents(self):
        class Credentials:
            def __init__(self): self.set_calls, self.remove_calls = 0, 0
            def status(self):
                return {"configured": True, "source": "dpapi", "stored_credential_present": True}
            def set_interactive(self): self.set_calls += 1
            def remove(self): self.remove_calls += 1; return True
        credentials = Credentials()
        state = {"repository": r"D:\synthetic\repo", "current": None}
        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": None}
            raise AssertionError(args)
        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("agentforge.credential_store.CredentialStore", return_value=credentials),
              patch("builtins.input", side_effect=["/model auth status", "/model auth set deepseek",
                                                    "/model auth remove deepseek", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())
        self.assertEqual(1, credentials.set_calls)
        self.assertEqual(1, credentials.remove_calls)
        self.assertIn("来源：dpapi", output.getvalue())
        self.assertIn("已移除本机 DPAPI 凭据", output.getvalue())
        self.assertNotIn("synthetic-test-secret", output.getvalue())

    def test_repository_switch_changes_following_session_commands(self):
        state = {
            "repository": r"D:\synthetic\first",
            "current": {"id": "conversation-first", "title": "first session"},
        }
        repositories = [
            {"repository_id": "repo-one", "display_name": "First",
             "canonical_path": state["repository"], "active": True},
            {"repository_id": "repo-two", "display_name": "Second",
             "canonical_path": r"D:\synthetic\second", "active": False},
        ]
        calls = []

        def fake_call(*args):
            calls.append(args)
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            if args == ("repo", "list"):
                return {"repositories": repositories}
            if args == ("repo", "switch", "repo-two"):
                state["repository"] = repositories[1]["canonical_path"]
                state["current"] = None
                return {"repository": repositories[1]}
            if args == ("session", "create", "new repository session"):
                state["current"] = {"id": "conversation-second", "title": "new repository session"}
                return {"result": state["current"]}
            if args == ("session", "switch", "conversation-second"):
                return {"result": state["current"]}
            raise AssertionError(f"Unexpected console command: {args!r}")

        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("builtins.input", side_effect=["/repo switch 2", "/new new repository session", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())

        self.assertIn(("repo", "switch", "repo-two"), calls)
        self.assertIn(("session", "create", "new repository session"), calls)
        self.assertIn(("session", "switch", "conversation-second"), calls)
        self.assertIn(r"D:\synthetic\second", output.getvalue())
        self.assertEqual("D:\\synthetic\\second", state["repository"])

    def test_cloud_switch_requires_explicit_consent_and_commits_only_after_discovery(self):
        from datetime import datetime, timezone
        from agentforge.model_registry import ModelDescriptor, LOCAL_QWEN

        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-1", "title": "session", "provider_id": "llama-server",
            "model_id": "qwen3.5-4b", "thinking_enabled": False,
        }}
        descriptor = ModelDescriptor("deepseek", "deepseek-flash", "Flash", "cloud",
            65536, 8192, supports_streaming=True, supports_tool_calling=True,
            supports_thinking=True, supported_efforts=("low", "high", "max"), availability="UNKNOWN")
        class Registry:
            cache_status = "fresh"
            cache_age_seconds = 0
            def get(self, key):
                if key in {"qwen-local", "qwen3.5-4b"}: return LOCAL_QWEN
                if key in {"deepseek-flash"}: return descriptor
                raise ValueError("not found")
            def refresh_deepseek(self, client):
                client.list_models()
                return (descriptor,)
        class Client:
            def __init__(self): self.calls = 0
            def list_models(self): self.calls += 1; return {"object": "list", "data": []}
        client = Client()
        calls = []
        def fake_call(*args):
            calls.append(args)
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": dict(state["current"])}
            if args[:2] == ("session", "set-model"):
                state["current"].update(provider_id="deepseek", model_id="deepseek-flash",
                    cloud_consent_at=datetime.now(timezone.utc), thinking_enabled=False)
                return {"result": dict(state["current"])}
            raise AssertionError(args)
        output = io.StringIO()
        with (patch("agentforge.conversation_console._registry", return_value=Registry()),
              patch("agentforge.conversation_console._deepseek_client", return_value=(client, "dpapi")),
              patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("builtins.input", side_effect=["/model deepseek-flash", "YES", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())
        self.assertEqual(1, client.calls)
        self.assertTrue(any(call[:3] == ("session", "set-model", "conversation-1") for call in calls))
        self.assertEqual("deepseek", state["current"]["provider_id"])

    def test_declined_cloud_consent_leaves_session_binding_unchanged(self):
        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-1", "title": "session", "provider_id": "llama-server",
            "model_id": "qwen3.5-4b", "thinking_enabled": False,
        }}
        calls = []
        def fake_call(*args):
            calls.append(args)
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": dict(state["current"])}
            raise AssertionError(args)
        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("agentforge.conversation_console._deepseek_client", side_effect=AssertionError("network must not start")),
              patch("builtins.input", side_effect=["/model deepseek-flash", "NO", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())
        self.assertFalse(any(call[1] == "set-model" for call in calls if len(call) > 1))
        self.assertEqual("llama-server", state["current"]["provider_id"])
        self.assertIn("已取消", output.getvalue())


if __name__ == "__main__":
    unittest.main()
