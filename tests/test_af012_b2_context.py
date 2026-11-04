"""会话消息构造与作用域检查；不发送模型请求。"""
import unittest
from unittest.mock import Mock

from agentforge.conversation_context import ConversationTurn
from agentforge.context import build_context
from agentforge.providers.llama_server import LlamaServerProvider
from tests.test_context import result
from unittest.mock import patch


class ConversationProviderTests(unittest.TestCase):
    def test_semantic_summary_prompt_marks_prior_summary_as_untrusted(self):
        from types import SimpleNamespace
        from agentforge.cli import _semantic_summary_callback
        client = Mock()
        client.chat.return_value = "Preserved decision and correction."
        client.request_stats.side_effect = [
            {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0},
            {"attempted": 1, "sent": 1, "succeeded": 1, "failed": 0, "retries": 0,
             "last_http_status": 200, "last_finish_reason": "stop"},
        ]
        callback = _semantic_summary_callback(SimpleNamespace(client=client, model="test-model"))
        callback([{"id": "m1", "sequence": 8, "role": "summary", "content": "old summary"},
                  {"id": "m2", "sequence": 9, "role": "user", "content": "Correction: use new value."}])
        sent = client.chat.call_args.args[0]
        self.assertIn("fallible summary", sent[0]["content"])
        self.assertIn("corrections and conflicts", sent[0]["content"])
        self.assertIn('"role":"summary"', sent[1]["content"])
        self.assertIn("Correction: use new value.", sent[1]["content"])
        self.assertEqual(1, len(callback.diagnostics))
        self.assertTrue(callback.diagnostics[0]["request_attempted"])
        self.assertGreater(callback.diagnostics[0]["input_json_bytes"], 0)
        self.assertEqual(9, callback.diagnostics[0]["input_sequence_start"])
        self.assertEqual(9, callback.diagnostics[0]["input_sequence_end"])
        self.assertEqual(len(client.chat.return_value.encode("utf-8")),
                         callback.diagnostics[0]["output_bytes"])
        self.assertEqual({"attempted": 1, "sent": 1, "succeeded": 1,
                          "failed": 0, "retries": 0},
                         callback.diagnostics[0]["provider_request_counts"])
        self.assertEqual(200, callback.diagnostics[0]["http_status"])
        self.assertEqual(512, client.chat.call_args.kwargs["max_output_tokens"])
        self.assertEqual(512, callback.diagnostics[0]["requested_output_tokens"])

    def test_semantic_summary_prompt_size_is_rejected_before_provider(self):
        from types import SimpleNamespace
        from agentforge.cli import _semantic_summary_callback
        client = Mock()
        callback = _semantic_summary_callback(SimpleNamespace(client=client, model="test-model"))
        with self.assertRaisesRegex(ValueError, "bounded prompt size"):
            callback([{"id": "m1", "sequence": 1, "role": "user", "content": "x" * 5000}])
        client.chat.assert_not_called()
        self.assertFalse(callback.diagnostics[0]["request_attempted"])
        self.assertEqual("prompt_validation", callback.diagnostics[0]["failure_stage"])

    def test_semantic_summary_rejects_model_length_truncation(self):
        from types import SimpleNamespace
        from agentforge.cli import _semantic_summary_callback
        client = Mock()
        client.chat.return_value = "An incomplete summary..."
        client.request_stats.return_value = {"last_finish_reason": "length"}
        callback = _semantic_summary_callback(SimpleNamespace(client=client, model="test-model"))
        with self.assertRaisesRegex(ValueError, "truncated"):
            callback([{"sequence": 1, "role": "user", "content": "important"}])
        self.assertEqual(1, client.chat.call_count)
        self.assertEqual(512, client.chat.call_args.kwargs["max_output_tokens"])
        self.assertEqual("semantic_summary_response_truncated",
                         callback.diagnostics[0]["failure_code"])

    def test_console_call_parses_success_and_reports_errors(self):
        from agentforge import conversation_console as console
        def success(_):
            print('{"result": 1}')
            return 0
        with patch.object(console.cli, "main", side_effect=success):
            self.assertEqual(1, console._call("session", "list")["result"])
        with patch.object(console.cli, "main", return_value=1), self.assertRaisesRegex(ValueError, "会话操作失败"):
            console._call("session", "list")
        with patch.object(console.cli, "main", side_effect=SystemExit(2)), self.assertRaises(ValueError):
            console._call("session", "list")

    def test_console_management_commands_and_confirmation(self):
        from agentforge import conversation_console as console
        from contextlib import redirect_stdout
        from io import StringIO
        state = {"current": None, "deleted": False}
        item = {"id": "a", "title": "original"}
        def call(*args):
            if args == ("session", "current"):
                return {"repository": "synthetic", "result": state["current"]}
            if args[:2] == ("session", "create"):
                return {"result": dict(item)}
            if args == ("session", "list"):
                return {"result": [item]}
            if args[:2] == ("session", "switch"):
                state["current"] = dict(item)
                return {"result": state["current"]}
            if args[:2] == ("session", "rename"):
                state["current"] = {"id": "a", "title": args[-1]}
                return {"result": state["current"]}
            if args[:2] == ("session", "show"):
                return {"result": {"messages": [{"role": "user", "content": "hello"}]}}
            if args[:2] == ("session", "delete"):
                state.update(current=None, deleted=True)
                return {"result": {}}
            raise ValueError("injected")
        inputs = ["", "unscoped question", "/new original", "/list", "/rename changed", "/history",
                  "/switch 99", "/unknown", "/delete", "no", "/delete", "DELETE", "/exit"]
        output = StringIO()
        with (patch.object(console, "_call", side_effect=call),
              patch("builtins.input", side_effect=inputs), redirect_stdout(output)):
            self.assertEqual(0, console.main())
        self.assertTrue(state["deleted"])
        self.assertIn("user: hello", output.getvalue())
        self.assertIn("会话编号无效", output.getvalue())

    def test_console_start_errors_and_eof(self):
        from agentforge import conversation_console as console
        from contextlib import redirect_stderr
        from io import StringIO
        with patch.object(console, "_call", side_effect=ValueError("database unavailable")), redirect_stderr(StringIO()):
            self.assertEqual(1, console.main())
        with patch.object(console, "_call", side_effect=EOFError()), redirect_stderr(StringIO()):
            self.assertEqual(0, console.main())
    def test_history_reaches_provider_in_order_and_ids_remain_request_local(self):
        client = Mock()
        client.chat.return_value = "The return value is 3 [E1]."
        context = build_context("And what does it return?", [result()], budget=6000)
        history = (ConversationTurn("c", "r", 1, "user", "What does foo do?"),
                   ConversationTurn("c", "r", 2, "assistant", "Earlier answer [E1]."))
        answer = LlamaServerProvider(client, "local").answer(context, conversation={
            "conversation_id": "c", "repository_id": "r", "history": history,
            "summary": "Prior extract."})
        self.assertIn("[E1]", answer.text)
        messages = client.chat.call_args.args[0]
        self.assertEqual(["system", "user", "user", "assistant", "user"],
                         [message["role"] for message in messages])
        self.assertIn("prior IDs expire", messages[0]["content"])
        self.assertIn("Untrusted prior conversation summary", messages[1]["content"])
        self.assertEqual("What does foo do?", messages[2]["content"])
        self.assertIn("And what does it return?", messages[-1]["content"])

    def test_cross_repository_history_never_reaches_provider(self):
        client = Mock()
        context = build_context("question", [result()], budget=6000)
        history = (ConversationTurn("c", "different-repo", 1, "user", "question"),)
        with self.assertRaises(ValueError):
            LlamaServerProvider(client, "local").answer(context, conversation={
                "conversation_id": "c", "repository_id": "r", "history": history})
        client.chat.assert_not_called()

    def test_pronoun_fallback_requires_one_unique_existing_symbol(self):
        from agentforge.cli import _followup_symbol
        store = Mock()
        store.search_symbols.side_effect = lambda **kw: ([{"name": "Conv2d"}]
                                                          if kw["name"] == "Conv2d" else [])
        rows = [{"role": "user", "content": "Conv2d 的作用？"},
                {"role": "assistant", "content": "此前回答"}]
        self.assertEqual("Conv2d", _followup_symbol("它的第二个参数是什么？", rows, store))
        self.assertIsNone(_followup_symbol("别的问题", rows, store))
        store.search_symbols.return_value = [{}, {}]
        store.search_symbols.side_effect = None
        self.assertIsNone(_followup_symbol("它的参数？", rows, store))

    def test_english_followup_does_not_confuse_function_word_with_identifier(self):
        from types import SimpleNamespace
        from agentforge.cli import _conversation_retrieval
        store = Mock()
        def symbols(**kwargs):
            return [{"name": kwargs["name"]}] if kwargs["name"].casefold() in ("function", "sync") else []
        store.search_symbols.side_effect = symbols
        rows = [{"role": "user", "content": "What does function sync do? Cite evidence."},
                {"role": "assistant", "content": "The function sync performs an operation [E1]."}]
        retriever = Mock()
        retriever._symbol_name.return_value = "and"
        retriever.search.return_value = "resolved-followup"
        outcome = SimpleNamespace(decision=SimpleNamespace(route="exact_symbol", confidence="high"))
        arguments = SimpleNamespace(query="What does it return? Identify the function and cite evidence.", top_k=5)
        resolved, name = _conversation_retrieval(outcome, retriever, {}, arguments, store, rows)
        self.assertEqual(("resolved-followup", "sync"), (resolved, name))
        retriever.search.assert_called_once_with("sync", top_k=5)

    def test_exact_symbol_context_excludes_unrelated_lexical_evidence_with_or_without_session(self):
        from types import SimpleNamespace
        from agentforge.cli import _context_candidates
        symbol = SimpleNamespace(symbol_ids=("symbol-id",))
        unrelated = SimpleNamespace(symbol_ids=())
        outcome = SimpleNamespace(decision=SimpleNamespace(route="exact_symbol"), candidates=(symbol, unrelated))
        self.assertEqual((symbol,), _context_candidates(outcome, {}))
        self.assertEqual((symbol,), _context_candidates(outcome, None))
        outcome.candidates = (unrelated,)
        self.assertEqual((unrelated,), _context_candidates(outcome, {}))

    def test_host_runners_fail_closed_when_network_is_disabled(self):
        import AF012_B2_HOST_DATABASE_CHECK as db_runner
        import AF012_B2_HOST_MULTITURN_CHECK as chat_runner
        import AF012_B2_PG_RESTART_CHECK as restart_runner
        import AF012_B2_FINAL_LONG_HOST as long_runner
        import AF012_B2_FINAL_DESKTOP_HOST as desktop_runner
        from contextlib import redirect_stdout
        from io import StringIO
        with patch.dict("os.environ", {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}), redirect_stdout(StringIO()):
            self.assertEqual(2, db_runner.main())
            self.assertEqual(2, chat_runner.main())
            self.assertEqual(2, restart_runner.main())
            self.assertEqual(2, long_runner.main())
            self.assertEqual(2, desktop_runner.main())

    def test_console_switches_sessions_and_passes_selected_id_to_ask(self):
        from agentforge import conversation_console as console
        from contextlib import redirect_stdout
        from io import StringIO
        a = {"id": "a", "title": "first"}
        b = {"id": "b", "title": "second"}
        selected = [a]
        def call(*args):
            if args == ("session", "current"):
                return {"repository": "synthetic", "result": selected[0]}
            if args == ("session", "list"):
                return {"result": [a, b]}
            if args == ("session", "switch", "b"):
                selected[0] = b
                return {"result": b}
            raise AssertionError(args)
        with (patch.object(console, "_call", side_effect=call),
              patch.object(console.presentation, "main", return_value=0) as ask,
              patch("builtins.input", side_effect=["/switch 2", "question", "/exit"]),
              redirect_stdout(StringIO())):
            self.assertEqual(0, console.main())
        self.assertEqual("b", ask.call_args.args[0][3])

    def test_console_detects_external_repository_change_and_forgets_old_selection(self):
        from agentforge import conversation_console as console
        from contextlib import redirect_stdout
        from io import StringIO
        previous = {"repository": "one", "result": {"id": "a", "title": "old"}}
        current = {"repository": "two", "result": None}
        output = StringIO()
        with (patch.object(console, "_call", side_effect=[previous, current, current]),
              patch.object(console.presentation, "main") as ask,
              patch("builtins.input", side_effect=["a question", "/exit"]), redirect_stdout(output)):
            self.assertEqual(0, console.main())
        ask.assert_not_called()
        self.assertIn("索引仓库已切换：two", output.getvalue())
