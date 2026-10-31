"""Offline SQLite checks only: they are not PostgreSQL integration evidence."""
import importlib.util
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from tests.support import LocalTestCase, FIXTURE


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy") and importlib.util.find_spec("alembic"),
                     "AF-012 SQLAlchemy/Alembic dependencies unavailable")
class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine, event
        from agentforge.conversation_migrations import upgrade
        from agentforge.conversation_store import ConversationStore
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
        upgrade(self.engine)
        self.store = ConversationStore(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_create_rename_list_and_scope(self):
        first = self.store.create_conversation("repo-a", "First")
        second = self.store.create_conversation("repo-b", "Other")
        self.assertEqual([first["id"]], [r["id"] for r in self.store.list_conversations("repo-a")])
        self.assertEqual("New title", self.store.rename_conversation(first["id"], "repo-a", "New title")["title"])
        with self.assertRaises(ValueError):
            self.store.get_conversation(second["id"], "repo-a")
        self.assertIsNone(self.store.current_conversation("repo-a"))
        self.store.switch_conversation(first["id"], "repo-a")
        self.assertEqual(first["id"], self.store.current_conversation("repo-a")["id"])
        same_repo = self.store.create_conversation("repo-a", "Next")
        self.store.switch_conversation(same_repo["id"], "repo-a")
        self.assertEqual(same_repo["id"], self.store.current_conversation("repo-a")["id"])
        self.store.switch_conversation(first["id"], "repo-a")
        with self.assertRaises(ValueError):
            self.store.switch_conversation(second["id"], "repo-a")
        self.assertEqual(first["id"], self.store.current_conversation("repo-a")["id"])

    def test_atomic_ordered_turns_and_summary(self):
        first = self.store.create_conversation("repo-a", "First")
        other = self.store.create_conversation("repo-a", "Second")
        self.store.append_turn(first["id"], "repo-a", "one", "answer one")
        self.store.append_turn(first["id"], "repo-a", "two", "answer two")
        rows = self.store.get_messages(first["id"], "repo-a")
        self.assertEqual([1, 2, 3, 4], [r["sequence"] for r in rows])
        self.assertEqual(["user", "assistant"] * 2, [r["role"] for r in rows])
        self.assertEqual([], self.store.get_messages(other["id"], "repo-a"))
        self.store.save_summary(first["id"], "repo-a", rows[1]["id"], "earlier summary")
        self.assertEqual(2, self.store.latest_summary(first["id"], "repo-a")["covered_through"])
        self.store.save_summary(first["id"], "repo-a", rows[1]["id"], "updated summary")
        self.assertEqual("updated summary", self.store.latest_summary(first["id"], "repo-a")["content"])
        with self.assertRaises(ValueError):
            self.store.save_summary(other["id"], "repo-a", rows[1]["id"], "wrong owner")
        with self.assertRaises(ValueError):
            self.store.save_summary(first["id"], "repo-a", rows[0]["id"], "partial turn")

    def test_failed_write_rolls_back_both_messages(self):
        from sqlalchemy import event, select, func
        from agentforge.af012_schema import Message
        first = self.store.create_conversation("repo-a", "First")
        @event.listens_for(self.engine, "before_cursor_execute")
        def fail_insert(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("INSERT INTO MESSAGES"):
                raise RuntimeError("injected storage failure")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.store.append_turn(first["id"], "repo-a", "question", "answer")
        event.remove(self.engine, "before_cursor_execute", fail_insert)
        self.assertEqual([], self.store.get_messages(first["id"], "repo-a"))
        self.store.append_turn(first["id"], "repo-a", "recovered", "answer")
        self.assertEqual(2, len(self.store.get_messages(first["id"], "repo-a")))

    def test_delete_cascades_and_is_scoped(self):
        item = self.store.create_conversation("repo-a", "First")
        self.store.append_turn(item["id"], "repo-a", "question", "answer")
        rows = self.store.get_messages(item["id"], "repo-a")
        self.store.save_summary(item["id"], "repo-a", rows[-1]["id"], "summary")
        with self.assertRaises(ValueError):
            self.store.delete_conversation(item["id"], "repo-b")
        self.store.delete_conversation(item["id"], "repo-a")
        self.assertEqual([], self.store.list_conversations("repo-a"))
        from sqlalchemy import select, func
        from agentforge.af012_schema import Message, ConversationSummary
        with self.engine.connect() as connection:
            self.assertEqual(0, connection.scalar(select(func.count()).select_from(Message)))
            self.assertEqual(0, connection.scalar(select(func.count()).select_from(ConversationSummary)))

    def test_long_history_summarizes_old_complete_turns_without_deleting_them(self):
        item = self.store.create_conversation("repo-a", "Long")
        for i in range(7):
            self.store.append_turn(item["id"], "repo-a", f"question {i}", f"answer {i}")
        remaining, summary = self.store.prepare_history(item["id"], "repo-a")
        self.assertEqual(14, len(self.store.get_messages(item["id"], "repo-a")))
        self.assertEqual(6, len(remaining))
        self.assertEqual(8, summary["covered_through"])
        self.assertIn("incomplete", summary["content"])
        self.assertEqual("question 4", remaining[0]["content"])

    def test_repeated_summaries_preserve_early_and_middle_dialogue(self):
        item = self.store.create_conversation("repo-a", "Long")
        for i in range(20):
            self.store.append_turn(item["id"], "repo-a", f"question {i} factual-marker-{i}", f"answer {i}")
        _, first = self.store.prepare_history(item["id"], "repo-a")
        self.assertIn("factual-marker-0", first["content"])
        self.assertIn("factual-marker-10", first["content"])
        for i in range(20, 27):
            self.store.append_turn(item["id"], "repo-a", f"question {i}", f"answer {i}")
        _, second = self.store.prepare_history(item["id"], "repo-a")
        self.assertGreater(second["covered_through"], first["covered_through"])
        self.assertIn("factual-marker-0", second["content"])
        self.assertIn("factual-marker-10", second["content"])

    def test_oversized_summary_fails_without_overwriting_existing_summary(self):
        item = self.store.create_conversation("repo-a", "Oversize")
        for i in range(7):
            self.store.append_turn(item["id"], "repo-a", f"early {i}", f"answer {i}")
        _, first = self.store.prepare_history(item["id"], "repo-a")
        for i in range(7):
            self.store.append_turn(item["id"], "repo-a", "x" * 2000, f"later {i}")
        with self.assertRaisesRegex(ValueError, "summary exceeds"):
            self.store.prepare_history(item["id"], "repo-a")
        self.assertEqual(first, self.store.latest_summary(item["id"], "repo-a"))
        self.assertEqual(28, len(self.store.get_messages(item["id"], "repo-a")))

    def test_corrupt_summary_is_rejected_instead_of_used(self):
        from sqlalchemy import update
        from agentforge.af012_schema import ConversationSummary
        from uuid import UUID
        item = self.store.create_conversation("repo-a", "Corrupt")
        self.store.append_turn(item["id"], "repo-a", "question", "answer")
        row = self.store.get_messages(item["id"], "repo-a")[-1]
        summary_id = self.store.save_summary(item["id"], "repo-a", row["id"], "valid")
        with self.engine.begin() as conn:
            conn.execute(update(ConversationSummary).where(ConversationSummary.id == UUID(summary_id))
                         .values(content=" "))
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.store.prepare_history(item["id"], "repo-a")

    def test_invalid_inputs_archived_and_missing_conversation_fail_closed(self):
        from sqlalchemy import update
        from uuid import UUID
        from agentforge.af012_schema import Conversation
        with self.assertRaises(ValueError):
            self.store.create_conversation(" ", "title")
        with self.assertRaises(ValueError):
            self.store.get_conversation("bad-id", "repo-a")
        item = self.store.create_conversation("repo-a", "title")
        with self.assertRaises(ValueError):
            self.store.rename_conversation(item["id"], "repo-a", "")
        with self.assertRaises(ValueError):
            self.store.append_turn(item["id"], "repo-a", "q", "a", model_name="")
        with self.assertRaises(ValueError):
            self.store.save_summary(item["id"], "repo-a", item["id"], "")
        with self.engine.begin() as conn:
            conn.execute(update(Conversation).where(Conversation.id == UUID(item["id"]))
                         .values(status="archived"))
        with self.assertRaisesRegex(ValueError, "archived"):
            self.store.append_turn(item["id"], "repo-a", "q", "a")
        self.assertEqual([], self.store.get_messages(item["id"], "repo-a"))


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy") and importlib.util.find_spec("alembic"),
                     "AF-012 SQLAlchemy/Alembic dependencies unavailable")
class OfflineConsoleIntegrationTests(LocalTestCase):
    def test_cli_session_lifecycle_health_and_unavailable_database(self):
        from sqlalchemy import create_engine
        from sqlalchemy.exc import OperationalError
        from agentforge.indexer import index_repository
        from agentforge.store import IndexStore
        from tests.test_cli import invoke
        import json
        sqlite_url = "sqlite+pysqlite:///" + (self.sandbox / "session-db.sqlite").as_posix()
        index = IndexStore(":memory:")
        self.addCleanup(index.close)
        index_repository(FIXTURE, index)
        @contextmanager
        def index_scope(*_args, **_kwargs):
            yield index
        with (patch("agentforge.cli._conversation_engine", side_effect=lambda: create_engine(sqlite_url)),
              patch("agentforge.cli.IndexStore", side_effect=index_scope)):
            self.assertEqual(0, invoke(["session", "health"])[0])
            self.assertEqual(0, invoke(["session", "migrate"])[0])
            cid = json.loads(invoke(["session", "create", "first"])[1])["result"]["id"]
            self.assertEqual(1, len(json.loads(invoke(["session", "list"])[1])["result"]))
            self.assertEqual(0, invoke(["session", "rename", cid, "renamed"])[0])
            self.assertEqual("renamed", json.loads(invoke(["session", "show", cid])[1])["result"]["conversation"]["title"])
            self.assertEqual(0, invoke(["session", "delete", cid, "--confirm"])[0])
            self.assertEqual([], json.loads(invoke(["session", "list"])[1])["result"])
        from unittest.mock import Mock
        unavailable = Mock()
        unavailable.connect.side_effect = OperationalError("statement", {}, Exception("hidden"))
        with patch("agentforge.cli._conversation_engine", return_value=unavailable):
            code, _, error = invoke(["session", "health"])
        self.assertEqual(2, code)
        self.assertNotIn("hidden", error)
    def test_cli_session_and_followup_use_scoped_history(self):
        from unittest.mock import Mock
        from sqlalchemy import create_engine
        from agentforge.conversation_migrations import upgrade
        from agentforge.indexer import index_repository
        from agentforge.providers.llama_server import LlamaServerProvider
        from agentforge.store import IndexStore
        from tests.test_cli import invoke
        sqlite_url = "sqlite+pysqlite:///" + (self.sandbox / "conversations.sqlite").as_posix()
        engine = create_engine(sqlite_url)
        upgrade(engine)
        engine.dispose()
        index = IndexStore(":memory:")
        self.addCleanup(index.close)
        index_repository(FIXTURE, index)
        @contextmanager
        def index_scope(*args, **kwargs):
            yield index
        client = Mock()
        client.chat.return_value = "It returns a deterministic placeholder [E1]."
        client.request_stats.return_value = {}
        provider = LlamaServerProvider(client, "test-model")
        with (patch("agentforge.cli._conversation_engine", side_effect=lambda: create_engine(sqlite_url)),
              patch("agentforge.cli.IndexStore", side_effect=index_scope),
              patch("agentforge.cli.create_provider", return_value=provider)):
            code, output, errors = invoke(["session", "create", "first"])
            self.assertEqual((0, ""), (code, errors))
            import json
            cid = json.loads(output)["result"]["id"]
            self.assertEqual(0, invoke(["session", "switch", cid])[0])
            args = ["--conversation-id", cid, "--provider", "llama-server", "--base-url", "http://127.0.0.1", "--model", "test"]
            self.assertEqual(0, invoke(["ask", "refresh_access_token", *args])[0])
            code, output, errors = invoke(["ask", "它返回什么？", *args])
            self.assertEqual((0, ""), (code, errors))
            self.assertEqual("refresh_access_token", json.loads(output)["retrieval"]["conversation_lookup_symbol"])
            self.assertEqual(2, client.chat.call_count)
            messages = client.chat.call_args.args[0]
            self.assertIn("refresh_access_token", messages[-3]["content"])
            self.assertIn("它返回什么", messages[-1]["content"])
            self.assertEqual(4, len(json.loads(invoke(["session", "show", cid])[1])["result"]["messages"]))
            self.assertEqual(cid, json.loads(invoke(["session", "current"])[1])["result"]["id"])

    def test_restart_probe_creates_and_cleans_only_its_own_data(self):
        from datetime import datetime, timezone
        from unittest.mock import MagicMock, Mock
        import AF012_B2_PG_RESTART_CHECK as probe
        (self.sandbox / ".agentforge" / "data").mkdir(parents=True)
        marker = self.sandbox / ".agentforge" / "data" / "af012-restart-test.json"
        engine = MagicMock()
        connection = engine.connect.return_value.__enter__.return_value
        connection.scalar.side_effect = [datetime(2026, 1, 1, tzinfo=timezone.utc),
                                        datetime(2026, 1, 2, tzinfo=timezone.utc)]
        store = Mock()
        store.create_conversation.return_value = {"id": "temporary-id"}
        store.current_conversation.return_value = {"id": "temporary-id"}
        store.get_messages.return_value = [{"content": "temporary question"}, {"content": "temporary answer"}]
        from io import StringIO
        from contextlib import redirect_stdout
        with (patch.object(probe, "__file__", str(self.sandbox / "probe.py")),
              patch.dict("os.environ", {"CODEX_SANDBOX_NETWORK_DISABLED": "0", "AGENTFORGE_DB_PASSWORD": "synthetic"}),
              patch("agentforge.af012_connection.create_local_engine", return_value=engine),
              patch("agentforge.conversation_store.ConversationStore", return_value=store),
              redirect_stdout(StringIO())):
            with patch("sys.argv", ["probe", "prepare", str(marker)]):
                self.assertEqual(0, probe.main())
            self.assertTrue(marker.exists())
            with patch("sys.argv", ["probe", "verify", str(marker)]):
                self.assertEqual(0, probe.main())
        self.assertFalse(marker.exists())
        store.delete_conversation.assert_called_once()
        self.assertTrue(list(self.sandbox.glob("AF012_B2_RESTART_VERIFY_*.json")))
