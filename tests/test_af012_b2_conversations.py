"""仅为离线 SQLite 检查，不构成 PostgreSQL 集成验收证据。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from tests.support import LocalTestCase, FIXTURE, TMP_ROOT


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

    def test_semantic_summary_is_bounded_scoped_and_source_anchored(self):
        item = self.store.create_conversation("repo-a", "Semantic history")
        for i in range(7):
            self.store.append_turn(item["id"], "repo-a", f"decision {i}", f"response {i}")
        captured = []
        def summarize(rows):
            captured.extend(rows)
            return "Decisions: 0-3. Open task: continue with 4."
        remaining, summary = self.store.prepare_history(item["id"], "repo-a",
            summarizer=summarize, summary_model="local-qwen-test")
        self.assertEqual(8, summary["covered_through"])
        self.assertEqual("local-qwen-test", summary["model_name"])
        self.assertEqual("assistant", captured[-1]["role"])
        self.assertEqual(summary["covered_message_id"], captured[-1]["id"])
        self.assertIn("Untrusted model-generated", summary["content"])
        self.assertEqual(6, len(remaining))

    def test_semantic_summary_failure_does_not_persist_partial_state(self):
        item = self.store.create_conversation("repo-a", "Failed semantic history")
        for i in range(7):
            self.store.append_turn(item["id"], "repo-a", f"question {i}", f"answer {i}")
        def fail(_rows):
            raise RuntimeError("injected summary failure")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.store.prepare_history(item["id"], "repo-a", summarizer=fail,
                                       summary_model="local-qwen-test")
        self.assertIsNone(self.store.latest_summary(item["id"], "repo-a"))
        self.assertEqual(14, len(self.store.get_messages(item["id"], "repo-a")))

    def test_semantic_summary_updates_incrementally_with_bounded_source(self):
        import json
        item = self.store.create_conversation("repo-a", "Incremental semantic summary")
        for i in range(7):
            self.store.append_turn(item["id"], "repo-a", f"early decision {i}", f"ack {i}")
        calls = []

        def summarize(rows):
            calls.append(rows)
            return "EARLY: keep the first decision. MID: preserve the later correction."

        _, first = self.store.prepare_history(
            item["id"], "repo-a", summarizer=summarize, summary_model="local-qwen-test")
        self.assertEqual(1, len(calls))
        for i in range(5):
            self.store.append_turn(item["id"], "repo-a", f"new decision {i}", f"follow-up {i}")

        _, second = self.store.prepare_history(
            item["id"], "repo-a", summarizer=summarize, summary_model="local-qwen-test")
        self.assertEqual(2, len(calls))
        source = calls[1]
        self.assertEqual("summary", source[0]["role"])
        self.assertEqual(first["content"], source[0]["content"])
        self.assertEqual(first["covered_through"], source[0]["sequence"])
        self.assertTrue(all(row["sequence"] > first["covered_through"]
                            for row in source[1:]))
        self.assertEqual(second["covered_through"], source[-1]["sequence"])
        self.assertLessEqual(len(json.dumps(source, ensure_ascii=False,
                                            separators=(",", ":")).encode("utf-8")), 3000)
        self.assertIn("EARLY", second["content"])
        self.assertIn("Untrusted model-generated", second["content"])

    def test_oversized_incremental_summary_input_preserves_previous_summary(self):
        item = self.store.create_conversation("repo-a", "Oversized incremental summary")
        for i in range(7):
            self.store.append_turn(item["id"], "repo-a", f"early {i}", f"answer {i}")
        _, first = self.store.prepare_history(item["id"], "repo-a")
        for i in range(6):
            self.store.append_turn(item["id"], "repo-a", "later " + ("x" * 3500), f"answer {i}")
        calls = []
        with self.assertRaisesRegex(ValueError, "bounded semantic summary input"):
            self.store.prepare_history(item["id"], "repo-a",
                                       summarizer=lambda rows: calls.append(rows) or "summary",
                                       summary_model="local-qwen-test")
        self.assertEqual([], calls)
        self.assertEqual(first, self.store.latest_summary(item["id"], "repo-a"))
        self.assertEqual(26, len(self.store.get_messages(item["id"], "repo-a")))

    def test_preseeded_thirty_turn_history_is_summarized_in_bounded_complete_batches(self):
        import json
        item = self.store.create_conversation("repo-a", "Preseeded thirty turns")
        for index in range(30):
            question = {0: "EARLY_FACT ALPHA731", 14: "MIDDLE_DECISION BETA842",
                        29: "RECENT_TASK GAMMA953"}.get(index, f"ordinary turn {index}")
            self.store.append_turn(item["id"], "repo-a", question, f"acknowledged turn {index}")

        batches = []

        def summarize(rows):
            batches.append(rows)
            material = " ".join(row["content"] for row in rows)
            markers = [marker for marker in ("ALPHA731", "BETA842", "GAMMA953")
                       if marker in material]
            return "Preserved markers: " + ", ".join(markers)

        remaining, summary = self.store.prepare_history(
            item["id"], "repo-a", summarizer=summarize, summary_model="local-qwen-test")
        self.assertGreater(len(batches), 1)
        for batch in batches:
            self.assertLessEqual(len(json.dumps(batch, ensure_ascii=False,
                                                separators=(",", ":")).encode("utf-8")), 3000)
            self.assertEqual("assistant", batch[-1]["role"])
        self.assertEqual(54, summary["covered_through"])
        self.assertIn("ALPHA731", summary["content"])
        self.assertIn("BETA842", summary["content"])
        self.assertEqual(6, len(remaining))
        self.assertIn("GAMMA953", remaining[-2]["content"])

    def test_later_semantic_summary_cannot_erase_earlier_checkpoint(self):
        item = self.store.create_conversation("repo-a", "Retain earlier summary")
        for index in range(7):
            question = "DECISION_ALPHA731: keep the repository read-only" if index == 0 else f"early turn {index}"
            self.store.append_turn(item["id"], "repo-a", question, f"ack {index}")

        _, first = self.store.prepare_history(
            item["id"], "repo-a",
            summarizer=lambda _rows: "DECISION_ALPHA731 remains in effect.",
            summary_model="local-qwen-test",
        )
        self.assertIn("DECISION_ALPHA731", first["content"])

        for index in range(7, 11):
            self.store.append_turn(item["id"], "repo-a", f"later update {index}", f"ack {index}")
        captured = []
        def summarize_replacement(rows):
            captured.extend(rows)
            material = " ".join(row["content"] for row in rows)
            facts = []
            if "DECISION_ALPHA731" in material:
                facts.append("DECISION_ALPHA731 remains in effect")
            facts.append("Later updates are included")
            return "; ".join(facts)

        _, second = self.store.prepare_history(
            item["id"], "repo-a", summarizer=summarize_replacement,
            summary_model="local-qwen-test")

        self.assertGreater(second["covered_through"], first["covered_through"])
        self.assertIn("DECISION_ALPHA731", second["content"])
        self.assertIn("Later updates are included", second["content"])
        self.assertEqual(first["content"], captured[0]["content"])
        self.assertNotIn("Prior untrusted summary retained through message", second["content"])

    def test_repeated_semantic_updates_replace_instead_of_growing_nested_summary(self):
        from agentforge.conversation_store import _SUMMARY_MAX_BYTES

        item = self.store.create_conversation("repo-a", "Bounded rolling summary")
        markers = ("EARLY_ALPHA731", "MIDDLE_BETA842", "RECENT_GAMMA953")

        def summarize(rows):
            source = " ".join(row["content"] for row in rows)
            retained = [marker for marker in markers if marker in source]
            return "Retained facts: " + ", ".join(retained)

        summary = None
        for index in range(32):
            fact = {0: markers[0], 15: markers[1], 29: markers[2]}.get(index, f"ordinary {index}")
            self.store.append_turn(item["id"], "repo-a", fact, f"acknowledged {index}")
            remaining, summary = self.store.prepare_history(
                item["id"], "repo-a", summarizer=summarize,
                summary_model="local-qwen-test")

        encoded = summary["content"].encode("utf-8")
        self.assertLessEqual(len(encoded), _SUMMARY_MAX_BYTES)
        for marker in markers[:2]:
            self.assertIn(marker, summary["content"])
        self.assertIn(markers[2], str(remaining))
        self.assertEqual(1, summary["content"].count("Untrusted model-generated"))
        self.assertNotIn("Prior untrusted summary retained through message", summary["content"])
        self.assertEqual(32, len(self.store.get_messages(item["id"], "repo-a")) // 2)

    def test_oversized_legacy_summary_is_compacted_before_context_reuse(self):
        from agentforge.conversation_store import _SUMMARY_MAX_BYTES

        item = self.store.create_conversation("repo-a", "Legacy oversized summary")
        self.store.append_turn(item["id"], "repo-a", "DECISION_OLD731 keep read-only", "ack")
        assistant = self.store.get_messages(item["id"], "repo-a")[-1]
        old_content = "Untrusted model-generated conversation summary; not repository evidence.\n" + "x" * 2_000
        self.store.save_summary(item["id"], "repo-a", assistant["id"],
                                "DECISION_OLD731 keep read-only", model_name="legacy-qwen")
        from sqlalchemy import update
        from agentforge.af012_schema import ConversationSummary
        from uuid import UUID
        with self.engine.begin() as connection:
            connection.execute(update(ConversationSummary)
                .where(ConversationSummary.conversation_id == UUID(item["id"]))
                .values(content=old_content, fact_ledger=None))
        captured = []

        def compact(rows):
            captured.extend(rows)
            return "DECISION_OLD731 remains read-only."

        remaining, replacement = self.store.prepare_history(
            item["id"], "repo-a", summarizer=compact, summary_model="current-qwen")
        self.assertEqual([], remaining)
        self.assertEqual(2, replacement["covered_through"])
        self.assertEqual("current-qwen", replacement["model_name"])
        self.assertEqual("summary", captured[0]["role"])
        self.assertTrue(captured[0]["content"].startswith(old_content))
        self.assertIn("DECISION_OLD731", captured[0]["content"])
        self.assertIn("DECISION_OLD731", replacement["content"])
        self.assertLessEqual(len(replacement["content"].encode("utf-8")), _SUMMARY_MAX_BYTES)
        self.assertNotIn("x" * 100, replacement["content"])

    def test_failed_legacy_summary_compaction_keeps_previous_record(self):
        item = self.store.create_conversation("repo-a", "Failed legacy compaction")
        self.store.append_turn(item["id"], "repo-a", "question", "answer")
        assistant = self.store.get_messages(item["id"], "repo-a")[-1]
        # 模拟预算限制引入前的历史记录。当前写入会拒绝
        # 超长摘要；旧数据仍可读取，以便安全压缩。
        from sqlalchemy import update
        from agentforge.af012_schema import ConversationSummary
        from uuid import UUID
        self.store.save_summary(item["id"], "repo-a", assistant["id"], "legacy summary",
                                model_name="legacy-qwen")
        with self.engine.begin() as connection:
            connection.execute(update(ConversationSummary)
                .where(ConversationSummary.conversation_id == UUID(item["id"]))
                .values(content="x" * 2_000))
        previous = self.store.latest_summary(item["id"], "repo-a")
        with self.assertRaisesRegex(RuntimeError, "compaction failure"):
            self.store.prepare_history(item["id"], "repo-a",
                summarizer=lambda _rows: (_ for _ in ()).throw(RuntimeError("compaction failure")),
                summary_model="current-qwen")
        self.assertEqual(previous, self.store.latest_summary(item["id"], "repo-a"))

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

    def test_thirty_two_incremental_summaries_keep_source_linked_facts(self):
        from agentforge.conversation_store import _SUMMARY_MAX_BYTES
        item = self.store.create_conversation("repo-a", "Repeated fact protection")
        facts = {
            0: "Project fact ALPHA731 is active.",
            11: "Decision BETA842: use the local PostgreSQL store.",
            23: "约束：retry_limit 必须保持为 7。",
            31: "Open task GAMMA953 remains unfinished.",
        }
        latest = None
        for index in range(38):
            self.store.append_turn(item["id"], "repo-a",
                                   facts.get(index, f"ordinary progress update {index}"),
                                   f"acknowledged progress {index}")
            # 故意遗漏早期事实，模拟有损的模型摘要。
            _, latest = self.store.prepare_history(
                item["id"], "repo-a",
                summarizer=lambda _rows, i=index: f"Recent update {i}; no earlier details.",
                summary_model="offline-test-only",
            )
        self.assertIsNotNone(latest)
        self.assertLessEqual(len(latest["content"].encode("utf-8")), _SUMMARY_MAX_BYTES)
        for fact in facts.values():
            self.assertIn(fact, latest["content"])
        self.assertEqual(4, len(latest["fact_ledger"]))
        source_ids = {source["message_id"] for fact in latest["fact_ledger"]
                      for source in fact["sources"]}
        rows = self.store.get_messages(item["id"], "repo-a")
        self.assertTrue(source_ids.issubset({row["id"] for row in rows}))
        self.assertTrue(all(diag["committed"] and diag["fact_check"] == "passed"
                            for diag in self.store.last_summary_diagnostics))
        self.assertTrue(all(diag["model_output_missing_fact_count"] >= 0
                            for diag in self.store.last_summary_diagnostics))

    def test_summary_rejects_missing_fact_without_advancing_cursor(self):
        from agentforge.conversation_facts import extract_message_facts, render_fact_section
        item = self.store.create_conversation("repo-a", "Reject omitted fact")
        for index in range(7):
            self.store.append_turn(item["id"], "repo-a",
                "Constraint: keep worker_limit at 9." if index == 0 else f"ordinary {index}",
                f"ack {index}")
        _, before = self.store.prepare_history(item["id"], "repo-a")
        rows = self.store.get_messages(item["id"], "repo-a")
        ledger = extract_message_facts(rows[:before["covered_through"]])
        next_assistant = rows[before["covered_through"] + 1]
        candidate = "Replacement summary that omits the constraint."
        with self.assertRaisesRegex(ValueError, "omitted a protected source fact"):
            self.store.save_summary(item["id"], "repo-a", next_assistant["id"],
                                    candidate, fact_ledger=ledger)
        self.assertEqual(before, self.store.latest_summary(item["id"], "repo-a"))

    def test_summary_failure_keeps_previous_content_and_cursor_and_records_diagnostic(self):
        item = self.store.create_conversation("repo-a", "Failure keeps checkpoint")
        for index in range(7):
            self.store.append_turn(item["id"], "repo-a",
                "Decision DELTA913: retain the old checkpoint." if index == 0 else f"ordinary {index}",
                f"ack {index}")
        _, before = self.store.prepare_history(
            item["id"], "repo-a", summarizer=lambda rows: " ".join(row["content"] for row in rows),
            summary_model="offline-test-only")
        for index in range(6):
            self.store.append_turn(item["id"], "repo-a", f"new ordinary {index}", f"answer {index}")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.store.prepare_history(item["id"], "repo-a",
                summarizer=lambda _rows: (_ for _ in ()).throw(RuntimeError("injected summary failure")))
        self.assertEqual(before, self.store.latest_summary(item["id"], "repo-a"))
        diagnostic = self.store.last_summary_diagnostics[-1]
        self.assertFalse(diagnostic["committed"])
        self.assertEqual(before["covered_through"], diagnostic["cursor_before"])
        self.assertEqual(before["covered_through"], diagnostic["cursor_after"])
        self.assertEqual("summarizer", diagnostic["failure_stage"])
        self.assertEqual("RuntimeError", diagnostic["failure_exception_type"])

    def test_summary_database_failure_rolls_back_candidate_and_cursor(self):
        from sqlalchemy import event
        item = self.store.create_conversation("repo-a", "Summary transaction rollback")
        for index in range(7):
            self.store.append_turn(item["id"], "repo-a",
                "Decision EPSILON912: keep the first checkpoint." if index == 0 else f"old {index}",
                f"ack {index}")
        _, first = self.store.prepare_history(
            item["id"], "repo-a", summarizer=lambda _rows: "Decision EPSILON912 stays.")
        for index in range(6):
            self.store.append_turn(item["id"], "repo-a", f"new {index}", f"ack new {index}")
        def fail_summary_write(connection, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("INSERT INTO CONVERSATION_SUMMARIES"):
                raise RuntimeError("injected summary write failure")
        event.listen(self.engine, "before_cursor_execute", fail_summary_write)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected summary write"):
                self.store.prepare_history(item["id"], "repo-a",
                    summarizer=lambda _rows: "Recent summary only.")
        finally:
            event.remove(self.engine, "before_cursor_execute", fail_summary_write)
        self.assertEqual(first, self.store.latest_summary(item["id"], "repo-a"))
        diagnostic = self.store.last_summary_diagnostics[-1]
        self.assertFalse(diagnostic["committed"])
        self.assertEqual(first["covered_through"], diagnostic["cursor_before"])
        self.assertEqual(first["covered_through"], diagnostic["cursor_after"])
        self.assertEqual("persistence", diagnostic["failure_stage"])
        self.assertEqual("RuntimeError", diagnostic["failure_exception_type"])
        self.assertEqual("semantic_summary_persistence_failed", diagnostic["failure_code"])

    def test_oversized_model_summary_has_diagnostic_and_preserves_checkpoint(self):
        item = self.store.create_conversation("repo-a", "Oversized model summary")
        for index in range(7):
            self.store.append_turn(item["id"], "repo-a", f"old {index}", f"ack {index}")
        _, first = self.store.prepare_history(item["id"], "repo-a",
            summarizer=lambda _rows: "Initial checkpoint.")
        for index in range(6):
            self.store.append_turn(item["id"], "repo-a", f"new {index}", f"ack {index}")
        with self.assertRaisesRegex(ValueError, "response is empty or exceeds"):
            self.store.prepare_history(item["id"], "repo-a",
                summarizer=lambda _rows: "x" * 6_001)
        self.assertEqual(first, self.store.latest_summary(item["id"], "repo-a"))
        diagnostic = self.store.last_summary_diagnostics[-1]
        self.assertEqual(6_001, diagnostic["output_bytes"])
        self.assertFalse(diagnostic["committed"])
        self.assertEqual(first["covered_through"], diagnostic["cursor_after"])

    def test_mixed_chinese_english_code_and_numeric_facts_keep_message_provenance(self):
        from agentforge.conversation_facts import extract_message_facts
        item = self.store.create_conversation("repo-a", "Mixed fact extraction")
        self.store.append_turn(item["id"], "repo-a",
            "项目事实：retry_limit is 7. 必须保持 read_only=True。", "Acknowledged.")
        rows = self.store.get_messages(item["id"], "repo-a")
        extracted = extract_message_facts(rows)
        retained = " ".join(fact["text"] for fact in extracted)
        self.assertIn("retry_limit is 7", retained)
        self.assertIn("必须保持 read_only=True", retained)
        self.assertTrue(all(fact["sources"][0]["message_id"] == rows[0]["id"]
                            for fact in extracted))
        self.assertFalse(any(fact["text"] == rows[1]["content"] for fact in extracted))

    def test_fact_ledger_does_not_cross_conversations_or_repositories(self):
        first = self.store.create_conversation("repo-a", "Fact in A")
        second = self.store.create_conversation("repo-a", "Other conversation")
        third = self.store.create_conversation("repo-b", "Other repository")
        for index in range(7):
            self.store.append_turn(first["id"], "repo-a",
                "Project fact SESSION_A731 is private." if index == 0 else f"a {index}", f"ack {index}")
        _, first_summary = self.store.prepare_history(first["id"], "repo-a")
        for item, repo in ((second, "repo-a"), (third, "repo-b")):
            for index in range(7):
                self.store.append_turn(item["id"], repo, f"ordinary {index}", f"ack {index}")
        _, second_summary = self.store.prepare_history(second["id"], "repo-a")
        _, third_summary = self.store.prepare_history(third["id"], "repo-b")
        self.assertIn("SESSION_A731", first_summary["content"])
        self.assertNotIn("SESSION_A731", second_summary["content"])
        self.assertNotIn("SESSION_A731", third_summary["content"])

    def test_protected_fact_overflow_does_not_replace_old_checkpoint(self):
        item = self.store.create_conversation("repo-a", "Fact budget")
        for index in range(7):
            fact = f"Project fact ITEM_{index:03d} is " + ("x" * 180) + "."
            self.store.append_turn(item["id"], "repo-a", fact if index < 3 else f"ordinary {index}",
                                   f"ack {index}")
        _, prior = self.store.prepare_history(
            item["id"], "repo-a", summarizer=lambda _rows: "Initial compact checkpoint.")
        for index in range(7):
            self.store.append_turn(item["id"], "repo-a",
                f"Constraint: preserve setting KEY_{index:03d} = {index}; " + ("x" * 160)
                if index < 7
                else "ordinary latest", f"ack later {index}")
        previous = self.store.latest_summary(item["id"], "repo-a")
        with self.assertRaisesRegex(ValueError, "byte budget|safe limit"):
            self.store.prepare_history(item["id"], "repo-a",
                summarizer=lambda _rows: "A tiny replacement.")
        self.assertEqual(previous, self.store.latest_summary(item["id"], "repo-a"))
        self.assertEqual(prior["covered_through"], previous["covered_through"])

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

    def test_summary_rejects_changed_source_identity(self):
        from sqlalchemy import update
        from agentforge.af012_schema import Message
        from uuid import UUID
        item = self.store.create_conversation("repo-a", "Corrupt source")
        self.store.append_turn(item["id"], "repo-a", "Project fact ZETA921 is fixed.", "ack")
        rows = self.store.get_messages(item["id"], "repo-a")
        summary = "Project fact ZETA921 is fixed."
        self.store.save_summary(item["id"], "repo-a", rows[-1]["id"], summary)
        with self.engine.begin() as connection:
            connection.execute(update(Message).where(Message.id == UUID(rows[0]["id"]))
                               .values(content="Project fact ZETA921 changed."))
        with self.assertRaisesRegex(ValueError, "source no longer matches"):
            self.store.latest_summary(item["id"], "repo-a")

    def test_summary_source_identity_allows_whitespace_normalization_only(self):
        item = self.store.create_conversation("repo-a", "Whitespace source")
        self.store.append_turn(item["id"], "repo-a",
            "Project fact: RETRY_LIMIT   is\n  fixed at 7.", "ack")
        for index in range(6):
            self.store.append_turn(item["id"], "repo-a", f"ordinary turn {index}", f"ack {index}")
        rows = self.store.get_messages(item["id"], "repo-a")
        self.store.prepare_history(item["id"], "repo-a", summarizer=lambda _rows: "Latest update.")
        summary = self.store.latest_summary(item["id"], "repo-a")
        self.assertIsNotNone(summary)
        self.assertIn("RETRY_LIMIT", summary["content"])

    def test_summary_rejects_fact_ledger_id_not_derived_from_text(self):
        from agentforge.conversation_facts import extract_message_facts
        item = self.store.create_conversation("repo-a", "Tampered fact identity")
        self.store.append_turn(item["id"], "repo-a", "Project fact OMEGA941 is stable.", "ack")
        rows = self.store.get_messages(item["id"], "repo-a")
        ledger = extract_message_facts(rows)
        ledger[0]["fact_id"] = "0" * 24
        with self.assertRaisesRegex(ValueError, "does not match its source text"):
            self.store.save_summary(item["id"], "repo-a", rows[-1]["id"],
                                    "Project fact OMEGA941 is stable.", fact_ledger=ledger)

    def test_oversized_summary_storage_is_rejected_before_insert(self):
        item = self.store.create_conversation("repo-a", "Bounded direct summary")
        self.store.append_turn(item["id"], "repo-a", "question", "answer")
        rows = self.store.get_messages(item["id"], "repo-a")
        with self.assertRaisesRegex(ValueError, "bounded storage budget"):
            self.store.save_summary(item["id"], "repo-a", rows[-1]["id"], "x" * 2_000)
        self.assertIsNone(self.store.latest_summary(item["id"], "repo-a"))

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
class LongContextLifecycleTests(unittest.TestCase):
    def test_thirty_turn_summary_switch_and_restart_recovery(self):
        from sqlalchemy import create_engine, event
        from agentforge.conversation_migrations import upgrade
        from agentforge.conversation_store import ConversationStore
        from agentforge.conversation_context import ConversationTurn, plan_context

        TMP_ROOT.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="af011-long-context-", dir=TMP_ROOT)
        self.addCleanup(temporary.cleanup)
        db_path = Path(temporary.name) / "conversations.sqlite3"
        url = "sqlite+pysqlite:///" + db_path.as_posix()

        def make_engine():
            engine = create_engine(url)
            @event.listens_for(engine, "connect")
            def enable_foreign_keys(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")
            return engine

        engine = make_engine()
        self.addCleanup(engine.dispose)
        upgrade(engine)
        store = ConversationStore(engine)
        first = store.create_conversation("repo-a", "30-turn continuity")
        other = store.create_conversation("repo-b", "isolated history")
        store.switch_conversation(first["id"], "repo-a")
        store.switch_conversation(other["id"], "repo-b")
        captured = []

        def summarize(rows):
            captured.append(tuple(rows))
            texts = " ".join(row["content"] for row in rows)
            facts = [marker for marker in (
                "EARLY_FACT: processor token cap is 37",
                "MIDDLE_DECISION: keep explicit repository scope",
                "RECENT_FACT: keep the active code index read-only",
            ) if marker in texts]
            return "Preserved facts: " + "; ".join(facts)

        remaining, first_summary = [], None
        for index in range(30):
            if index == 0:
                question = "EARLY_FACT: processor token cap is 37"
            elif index == 14:
                question = "MIDDLE_DECISION: keep explicit repository scope"
            elif index == 29:
                question = "RECENT_FACT: keep the active code index read-only"
            else:
                question = f"synthetic question {index}"
            store.append_turn(first["id"], "repo-a", question, f"synthetic answer {index}")
            remaining, first_summary = store.prepare_history(
                first["id"], "repo-a", summarizer=summarize, summary_model="synthetic-only",
            )
        store.append_turn(other["id"], "repo-b", "OTHER_REPO_SECRET_MARKER", "isolated")
        self.assertEqual(60, len(store.get_messages(first["id"], "repo-a")))
        self.assertEqual(48, first_summary["covered_through"])
        self.assertEqual(12, len(remaining))
        self.assertIn("RECENT_FACT", str(remaining))
        self.assertNotIn("OTHER_REPO_SECRET_MARKER", str(captured))
        self.assertTrue(first_summary["covered_message_id"])

        for index in range(30, 37):
            store.append_turn(first["id"], "repo-a", f"followup question {index}",
                              f"followup answer {index}")
        remaining, latest_summary = store.prepare_history(
            first["id"], "repo-a", summarizer=summarize, summary_model="synthetic-only",
        )
        self.assertEqual(68, latest_summary["covered_through"])
        self.assertIn("EARLY_FACT", latest_summary["content"])
        self.assertIn("MIDDLE_DECISION", latest_summary["content"])
        self.assertTrue(latest_summary["fact_ledger"])
        source_ids = {source["message_id"] for fact in latest_summary["fact_ledger"]
                      for source in fact["sources"]}
        all_ids = {row["id"] for row in store.get_messages(first["id"], "repo-a")}
        self.assertTrue(source_ids.issubset(all_ids))
        self.assertEqual(["user", "assistant"] * 3,
                         [row["role"] for row in remaining])
        self.assertIn("RECENT_FACT", latest_summary["content"])

        engine.dispose()
        reopened_engine = make_engine()
        self.addCleanup(reopened_engine.dispose)
        recovered = ConversationStore(reopened_engine)
        self.assertEqual(first["id"], recovered.current_conversation("repo-a")["id"])
        self.assertEqual(other["id"], recovered.current_conversation("repo-b")["id"])
        recovered_messages = recovered.get_messages(first["id"], "repo-a")
        self.assertEqual(74, len(recovered_messages))
        recovered_summary = recovered.latest_summary(first["id"], "repo-a")
        self.assertEqual(latest_summary, recovered_summary)
        history = tuple(ConversationTurn(
            first["id"], "repo-a", row["sequence"], row["role"], row["content"], row["status"]
        ) for row in remaining)
        plan = plan_context(
            conversation_id=first["id"], repository_id="repo-a", system="system",
            question="What did we decide about it?", evidence="[E1] current source",
            history=history, summary=recovered_summary["content"], budget_bytes=20_000,
        )
        prompt = "\n".join(content for _role, content in plan.messages)
        self.assertIn("EARLY_FACT", prompt)
        self.assertIn("MIDDLE_DECISION", prompt)
        self.assertIn("RECENT_FACT", prompt)
        self.assertNotIn("OTHER_REPO_SECRET_MARKER", prompt)


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy") and importlib.util.find_spec("alembic"),
                     "AF-012 SQLAlchemy/Alembic dependencies unavailable")
class OfflineConsoleIntegrationTests(LocalTestCase):
    def test_no_evidence_refusal_does_not_compact_long_history(self):
        import json
        from sqlalchemy import create_engine
        from agentforge.conversation_migrations import upgrade
        from agentforge.conversation_store import ConversationStore
        from agentforge.indexer import index_repository
        from agentforge.store import IndexStore
        from tests.test_cli import invoke
        sqlite_url = "sqlite+pysqlite:///" + (self.sandbox / "refusal.sqlite").as_posix()
        engine = create_engine(sqlite_url)
        upgrade(engine)
        engine.dispose()
        index = IndexStore(":memory:")
        self.addCleanup(index.close)
        index_repository(FIXTURE, index)
        @contextmanager
        def index_scope(*_args, **_kwargs):
            yield index
        with (patch("agentforge.cli._conversation_engine", side_effect=lambda: create_engine(sqlite_url)),
              patch("agentforge.cli._open_current_index", side_effect=index_scope),
              patch.object(ConversationStore, "prepare_history", side_effect=AssertionError("unexpected compaction"))):
            cid = json.loads(invoke(["session", "create", "refusal"])[1])["result"]["id"]
            code, output, error = invoke(["ask", "What does function nonexistent_zephyr_941 return?", "--conversation-id", cid])
            self.assertEqual((0, ""), (code, error))
            self.assertEqual("refused", json.loads(output)["answer_contract"]["status"])

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
              patch("agentforge.cli._open_current_index", side_effect=index_scope)):
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
              patch("agentforge.cli._open_current_index", side_effect=index_scope),
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
