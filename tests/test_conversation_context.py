import unittest
import hashlib

from agentforge.conversation_context import (
    ConversationTurn, MemoryCandidate, MemorySourceReference, plan_context,
)


class ContextPlanTests(unittest.TestCase):
    def base(self, **kwargs):
        return plan_context(conversation_id="c1", repository_id="r1", system="trusted system",
                            question="它是什么？", evidence="[E1] source", **kwargs)

    def memory(self, content, *, approved=True, repository_id="r1", memory_id="m1"):
        return MemoryCandidate(repository_id, content, approved, memory_id,
            "preference", 2, (MemorySourceReference("message-1", 3, "user",
                hashlib.sha256(b"source message").hexdigest()),))

    def test_recent_complete_turns_are_structured_and_ordered(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "Conv2d?"),
                 ConversationTurn("c1", "r1", 2, "assistant", "layer [E1]"))
        plan = self.base(history=turns)
        self.assertEqual([r for r, _ in plan.messages], ["system", "user", "assistant", "user"])
        self.assertIn("它是什么？", plan.messages[-1][1])
        self.assertEqual(plan.dropped_turns, 0)

    def test_cross_session_and_repository_fail_closed(self):
        for turn in (ConversationTurn("c2", "r1", 1, "user", "secret"),
                     ConversationTurn("c1", "r2", 1, "user", "secret")):
            with self.subTest(turn=turn), self.assertRaisesRegex(ValueError, "scope"):
                self.base(history=(turn,))
        with self.assertRaisesRegex(ValueError, "different repository"):
            self.base(memories=(self.memory("secret", repository_id="r2"),))

    def test_budget_keeps_complete_recent_turns_and_no_partial_history(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "a" * 100),
                 ConversationTurn("c1", "r1", 2, "assistant", "b" * 100),
                 ConversationTurn("c1", "r1", 3, "user", "new"),
                 ConversationTurn("c1", "r1", 4, "assistant", "reply"))
        plan = self.base(history=turns, budget_bytes=160)
        self.assertEqual(plan.dropped_turns, 2)
        self.assertIn(("user", "new"), plan.messages)
        self.assertLessEqual(plan.bytes_used, 160)
        with self.assertRaisesRegex(ValueError, "exceed"):
            self.base(budget_bytes=5)

    def test_failed_and_incomplete_turns_not_reused(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "q"),
                 ConversationTurn("c1", "r1", 2, "assistant", "partial", "failed"))
        self.assertEqual(self.base(history=turns).dropped_turns, 2)

    def test_only_approved_memory_in_same_repository(self):
        plan = self.base(memories=(self.memory("approved"),
                                   self.memory("unapproved", approved=False, memory_id="m2")))
        text = str(plan.messages)
        self.assertIn("approved", text)
        self.assertNotIn("unapproved", text)
        self.assertNotIn(("system", "approved"), plan.messages)
        self.assertEqual(("m1",), plan.memory_ids)
        injected = next(content for role, content in plan.messages if role == "user" and "Memory ID" in content)
        self.assertIn("Memory ID: m1", injected)
        self.assertIn("message_id=message-1", injected)
        self.assertIn("sequence=3", injected)
        self.assertIn("sha256=" + hashlib.sha256(b"source message").hexdigest(), injected)

    def test_approved_memory_without_identity_and_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.base(memories=(MemoryCandidate("r1", "approved", True),))

    def test_cli_audit_metadata_contains_only_memories_really_injected(self):
        from agentforge.cli import _injected_memory_metadata
        injected = self.memory("do not echo in audit metadata")
        omitted = self.memory("too large for context", memory_id="m2")
        conversation = {"injected_memory_ids": ("m1",), "memories": (injected, omitted)}
        rows = _injected_memory_metadata(conversation)
        self.assertEqual(1, len(rows))
        self.assertEqual("m1", rows[0]["memory_id"])
        self.assertEqual("message-1", rows[0]["sources"][0]["message_id"])
        self.assertNotIn("content", rows[0])
        self.assertNotIn("do not echo", str(rows))

    def test_summary_is_not_silently_dropped_to_keep_recent_messages(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "x" * 50),
                 ConversationTurn("c1", "r1", 2, "assistant", "y" * 50))
        plan = self.base(history=turns, summary="important earlier fact", budget_bytes=190)
        self.assertIn("important earlier fact", str(plan.messages))
        self.assertEqual(plan.dropped_turns, 2)
        with self.assertRaisesRegex(ValueError, "summary"):
            self.base(summary="z" * 1000, budget_bytes=150)

    def test_invalid_budget_empty_summary_and_oversized_memory(self):
        for value in (True, 0, 1_000_001, "120"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.base(budget_bytes=value)
        with self.assertRaisesRegex(ValueError, "summary"):
            self.base(summary="  ")
        plan = self.base(memories=(self.memory("x" * 500),), budget_bytes=150)
        self.assertNotIn("x" * 500, str(plan.messages))
        self.assertEqual((), plan.memory_ids)

    def test_bad_roles_and_out_of_order_messages_rejected(self):
        with self.assertRaises(ValueError):
            self.base(history=(ConversationTurn("c1", "r1", 1, "system", "bad"),))
        with self.assertRaises(ValueError):
            self.base(history=(ConversationTurn("c1", "r1", 2, "user", "a"),
                               ConversationTurn("c1", "r1", 1, "assistant", "b")))
