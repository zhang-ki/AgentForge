import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from agentforge import conversation_console


class ConversationConsoleRepositoryTests(unittest.TestCase):
    def test_console_starts_when_no_repository_is_registered(self):
        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": None, "result": None}
            if args == ("repo", "list"):
                return {"repositories": []}
            raise AssertionError(args)

        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("builtins.input", side_effect=["/repo list", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())

        self.assertIn("当前没有活动仓库", output.getvalue())
        self.assertIn("注册时会自动建立 SQLite 与 Chroma 索引", output.getvalue())

    def test_plain_question_uses_auto_planning_without_manual_mode_selection(self):
        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-1", "title": "session", "provider_id": "llama-server",
            "model_id": "qwen3.5-4b", "thinking_enabled": False,
        }}

        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            raise AssertionError(args)

        output = io.StringIO()
        with (patch.dict("os.environ", {}, clear=True),
              patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("agentforge.conversation_console.presentation.main") as ask,
              patch("builtins.input", side_effect=["Explain attention in a transformer", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())

        arguments = ask.call_args.args[0]
        self.assertEqual("ask", arguments[0])
        self.assertIn("--mode", arguments)
        self.assertEqual("auto", arguments[arguments.index("--mode") + 1])
        self.assertIn("--use-chroma", arguments)
        self.assertIn("由 Planner 自动选择", output.getvalue())

    def test_vector_retrieval_can_be_disabled_only_by_explicit_environment_or_command(self):
        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-1", "title": "session", "provider_id": "llama-server",
            "model_id": "qwen3.5-4b", "thinking_enabled": False,
        }}

        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            raise AssertionError(args)

        with (patch.dict("os.environ", {"AGENTFORGE_CHROMA_ENABLED": "0"}, clear=True),
              patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("agentforge.conversation_console.presentation.main") as ask,
              patch("builtins.input", side_effect=["explain this", "/exit"]),
              redirect_stdout(io.StringIO())):
            self.assertEqual(0, conversation_console.main())

        arguments = ask.call_args.args[0]
        self.assertIn("--no-chroma", arguments)
        self.assertNotIn("--use-chroma", arguments)

    def test_stream_on_and_off_toggle_provider_streaming_without_changing_session(self):
        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-stream", "title": "session", "provider_id": "deepseek",
            "model_id": "deepseek-flash", "thinking_enabled": False,
        }}

        class Descriptor:
            supports_streaming = True
            display_name = "DeepSeek Flash"

        class Registry:
            def get(self, model_id):
                self_model.assertEqual("deepseek-flash", model_id)
                return Descriptor()

            def get_for_request(self, model_id):
                self_model.assertEqual("deepseek-flash", model_id)
                return Descriptor()

        self_model = self

        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            raise AssertionError(args)

        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("agentforge.conversation_console._registry", return_value=Registry()),
              patch("agentforge.conversation_console.presentation.main") as ask,
              patch("builtins.input", side_effect=["/stream on", "first", "/stream off", "second", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())

        requests = [call.args[0] for call in ask.call_args_list]
        self.assertEqual(2, len(requests))
        self.assertIn("--stream", requests[0])
        self.assertNotIn("--stream", requests[1])
        self.assertTrue(all(args[args.index("--conversation-id") + 1] == "conversation-stream"
                            for args in requests))
        self.assertIn("草稿显示已启用", output.getvalue())
        self.assertIn("草稿显示已关闭", output.getvalue())

    def test_stream_on_with_unknown_capability_keeps_answer_path_buffered(self):
        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-stream-unknown", "title": "session", "provider_id": "deepseek",
            "model_id": "deepseek-flash", "thinking_enabled": False,
        }}

        class Descriptor:
            supports_streaming = None
            display_name = "DeepSeek Flash"

        class Registry:
            def get(self, _model_id):
                return Descriptor()

            def get_for_request(self, _model_id):
                return Descriptor()

        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            raise AssertionError(args)

        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("agentforge.conversation_console._registry", return_value=Registry()),
              patch("agentforge.conversation_console.presentation.main") as ask,
              patch("builtins.input", side_effect=["/stream on", "Explain TCP.", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())

        arguments = ask.call_args.args[0]
        self.assertIn("--stream", arguments)
        self.assertEqual("conversation-stream-unknown",
                         arguments[arguments.index("--conversation-id") + 1])
        self.assertIn("原生 Streaming 能力尚未确认", output.getvalue())

    def test_debug_mode_override_keeps_same_session_and_can_return_to_auto(self):
        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-keep", "title": "session", "provider_id": "llama-server",
            "model_id": "qwen3.5-4b", "thinking_enabled": False,
        }}

        def fake_call(*args):
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": state["current"]}
            raise AssertionError(args)

        output = io.StringIO()
        with (patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("agentforge.conversation_console.presentation.main") as ask,
              patch("builtins.input", side_effect=[
                  "/mode repository", "inspect this source", "/mode auto", "explain this idea", "/exit",
              ]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())

        requests = [call.args[0] for call in ask.call_args_list]
        self.assertEqual(2, len(requests))
        self.assertEqual("conversation-keep", requests[0][requests[0].index("--conversation-id") + 1])
        self.assertEqual("conversation-keep", requests[1][requests[1].index("--conversation-id") + 1])
        self.assertEqual("repository", requests[0][requests[0].index("--mode") + 1])
        self.assertEqual("auto", requests[1][requests[1].index("--mode") + 1])
        self.assertIn("执行状态保持不变", output.getvalue())

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

    def test_model_default_selects_deepseek_only_after_consent_and_discovery(self):
        from datetime import datetime, timezone
        from agentforge.model_registry import ModelDescriptor

        state = {"repository": r"D:\synthetic\repo", "current": {
            "id": "conversation-1", "title": "session", "provider_id": "llama-server",
            "model_id": "qwen3.5-4b", "thinking_enabled": False,
        }}
        descriptor = ModelDescriptor("deepseek", "deepseek-flash", "Flash", "cloud",
                                     65536, 8192, capability_source="DeepSeek official /models API response",
                                     capability_verified_at=datetime.now(timezone.utc).isoformat())
        calls = []

        def fake_call(*args):
            calls.append(args)
            if args == ("session", "current"):
                return {"repository": state["repository"], "result": dict(state["current"])}
            if args[:2] == ("session", "set-model"):
                state["current"].update(provider_id="deepseek", model_id="deepseek-flash",
                                         cloud_consent_at=datetime.now(timezone.utc))
                return {"result": dict(state["current"])}
            raise AssertionError(args)

        class Registry:
            def __init__(self):
                self.lookups = []

            def get(self, key):
                if key == "deepseek-flash":
                    return descriptor
                raise ValueError("unknown model")

            def refresh_deepseek(self, client):
                self.client = client
                return (descriptor,)

        registry = Registry()
        client = object()
        output = io.StringIO()
        with (patch("agentforge.conversation_console._registry", return_value=registry),
              patch("agentforge.credential_store.CredentialStore.status", return_value={"configured": True}),
              patch("agentforge.conversation_console._deepseek_client", return_value=(client, "environment")),
              patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("builtins.input", side_effect=["/model default", "YES", "/exit"]),
              redirect_stdout(output)):
            self.assertEqual(0, conversation_console.main())
        self.assertIs(client, registry.client)
        self.assertTrue(any(call == ("session", "set-model", "conversation-1", "deepseek",
                                     "deepseek-flash", "--thinking", "off", "--cloud-consent")
                            for call in calls))
        self.assertEqual("deepseek", state["current"]["provider_id"])

    def test_failed_qwen_health_check_keeps_previous_binding(self):
        from agentforge.model_registry import LOCAL_QWEN
        current = {"id": "conversation-1", "title": "session", "provider_id": "deepseek",
                   "model_id": "deepseek-flash", "thinking_enabled": False}
        with (patch("agentforge.conversation_console._registry") as registry,
              patch("agentforge.providers.local_health.check_qwen_identity",
                    side_effect=ValueError("service unavailable")),
              patch("agentforge.conversation_console._save_model_binding") as save,
              patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "synthetic-key"}, clear=True)):
            registry.return_value.get.return_value = LOCAL_QWEN
            with self.assertRaisesRegex(ValueError, "service unavailable"):
                conversation_console._switch_model("qwen-local", current)
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
              patch("agentforge.credential_store.CredentialStore.status", return_value={"configured": False}),
              patch("agentforge.providers.local_health.check_qwen_identity", return_value={"health": "ready"}),
              patch("agentforge.conversation_console._save_model_binding", side_effect=lambda current, descriptor, **kwargs: current),
              patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "synthetic-key"}, clear=True),
              patch("builtins.input", side_effect=["/repo switch 2", "/new new repository session", "LOCAL", "/exit"]),
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

    def test_new_default_session_cancelled_consent_creates_nothing_and_makes_no_request(self):
        calls = []

        def fake_call(*args):
            calls.append(args)
            raise AssertionError(f"No session command is allowed: {args!r}")

        with (patch("agentforge.credential_store.CredentialStore.status", return_value={"configured": True}),
              patch("agentforge.conversation_console._registry", side_effect=AssertionError("no discovery")),
              patch("agentforge.conversation_console._deepseek_client", side_effect=AssertionError("no request")),
              patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch("builtins.input", side_effect=["NO", "CANCEL"]),
              redirect_stdout(io.StringIO())):
            self.assertIsNone(conversation_console._create_default_conversation("synthetic"))
        self.assertEqual([], calls)

    def test_new_default_session_without_cloud_credential_requires_explicit_local_choice(self):
        from agentforge.model_registry import LOCAL_QWEN

        state = {"current": {"id": "session-1", "title": "synthetic"}}
        calls = []

        def fake_call(*args):
            calls.append(args)
            if args == ("session", "create", "synthetic"):
                return {"result": state["current"]}
            if args == ("session", "switch", "session-1"):
                return {"result": state["current"]}
            raise AssertionError(args)

        output = io.StringIO()
        with (patch("agentforge.credential_store.CredentialStore.status", return_value={"configured": False}),
              patch("agentforge.providers.local_health.check_qwen_identity", return_value={"health": "ready"}) as health,
              patch("agentforge.conversation_console._save_model_binding", return_value=state["current"]) as save,
              patch("agentforge.conversation_console._call", side_effect=fake_call),
              patch.dict("os.environ", {"AGENTFORGE_LLAMA_API_KEY": "synthetic-key"}, clear=True),
              patch("builtins.input", return_value="LOCAL"),
              redirect_stdout(output)):
            result = conversation_console._create_default_conversation("synthetic")

        self.assertEqual(state["current"], result)
        health.assert_called_once()
        save.assert_called_once_with(state["current"], LOCAL_QWEN)
        self.assertEqual([("session", "create", "synthetic"), ("session", "switch", "session-1")], calls)
        self.assertIn("DeepSeek 尚未配置", output.getvalue())

    def test_new_default_session_without_credentials_cancels_without_creating_session(self):
        with (patch("agentforge.credential_store.CredentialStore.status", return_value={"configured": False}),
              patch("agentforge.providers.local_health.check_qwen_identity", side_effect=AssertionError("local not selected")),
              patch("agentforge.conversation_console._call", side_effect=AssertionError("must not create session")),
              patch("builtins.input", return_value="CANCEL"),
              redirect_stdout(io.StringIO())):
            self.assertIsNone(conversation_console._create_default_conversation("synthetic"))

    def test_new_default_local_choice_requires_configured_qwen_before_creating_session(self):
        with (patch("agentforge.credential_store.CredentialStore.status", return_value={"configured": False}),
              patch("agentforge.providers.local_health.check_qwen_identity", side_effect=AssertionError("no key")),
              patch("agentforge.conversation_console._call", side_effect=AssertionError("must not create session")),
              patch.dict("os.environ", {}, clear=True),
              patch("builtins.input", return_value="LOCAL")):
            with self.assertRaisesRegex(ValueError, "本地 Qwen 认证未配置"):
                conversation_console._create_default_conversation("synthetic")


if __name__ == "__main__":
    unittest.main()
